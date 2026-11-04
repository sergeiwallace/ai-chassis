#!/usr/bin/env python3
"""Empirically probe bd's conditional-write surface using only disposable state.

This intentionally uses a standalone Dolt SQL server.  ``bd sql`` is unsupported
when bd uses its default embedded engine, while it is callable in server mode.
The probe must not be run from a real Beads workspace or against a shared server.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def command(
    args: list[str], *, cwd: Path, check: bool = True, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_for_server(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError("disposable Dolt SQL server did not start")


def retrying_writer(workspace: Path, issue_id: str) -> None:
    """Write without participating in the contender's predicate or protocol."""
    for _ in range(25):
        result = command(
            ["bd", "update", issue_id, "--title", "non-cooperating-writer"],
            cwd=workspace,
            check=False,
        )
        if result.returncode == 0:
            return
        if "serialization failure" not in result.stdout + result.stderr:
            raise RuntimeError(result.stdout + result.stderr)
        time.sleep(0.02)
    raise RuntimeError(
        "non-cooperating writer exhausted its transient-conflict retries"
    )


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="issue-ref-bd-"))
    data_dir = root / "dolt-data"
    data_dir.mkdir()
    port = free_port()
    server_log = (root / "dolt-server.log").open("w", encoding="utf-8")
    server = subprocess.Popen(
        [
            "dolt",
            "sql-server",
            "--data-dir",
            str(data_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=server_log,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        # bd sql is not a generally available bd primitive: the default embedded
        # backend rejects it before a query can run.
        embedded = root / "embedded"
        embedded.mkdir()
        command(["git", "init", "-q"], cwd=embedded)
        command(
            [
                "bd",
                "init",
                "--non-interactive",
                "--skip-agents",
                "--skip-hooks",
                "--prefix",
                "EMB",
            ],
            cwd=embedded,
        )
        embedded_sql = command(["bd", "sql", "SELECT 1"], cwd=embedded, check=False)
        if embedded_sql.returncode == 0 or "not yet supported in embedded mode" not in (
            embedded_sql.stdout + embedded_sql.stderr
        ):
            raise AssertionError("bd sql unexpectedly worked in embedded mode")

        wait_for_server(port)
        command(["git", "init", "-q"], cwd=root)
        command(
            [
                "bd",
                "init",
                "--non-interactive",
                "--skip-agents",
                "--skip-hooks",
                "--prefix",
                "SPK",
                "--server",
                "--external",
                "--server-port",
                str(port),
            ],
            cwd=root,
        )
        command(
            ["bd", "create", "--type", "task", "--priority", "2", "baseline"],
            cwd=root,
        )
        records = json.loads(command(["bd", "list", "--json"], cwd=root).stdout)
        if len(records) != 1:
            raise AssertionError(
                f"expected exactly one disposable issue, got {records!r}"
            )
        issue_id = records[0]["id"]

        def title() -> str:
            current = json.loads(command(["bd", "list", "--json"], cwd=root).stdout)
            return next(row["title"] for row in current if row["id"] == issue_id)

        def reset() -> None:
            command(["bd", "update", issue_id, "--title", "baseline"], cwd=root)

        def conditional(new_title: str, predicate: str = "title = 'baseline'") -> str:
            return command(
                [
                    "bd",
                    "sql",
                    (
                        "UPDATE issues "
                        f"SET title = '{new_title}' "
                        f"WHERE id = '{issue_id}' AND {predicate}"
                    ),
                ],
                cwd=root,
            ).stdout

        # The normal bd command interface exposes a transaction batch, but no
        # version/predicate field.  It must reject an attempted predicate rather
        # than silently accepting a stale conditional write.
        batch = command(
            ["bd", "batch"],
            cwd=root,
            check=False,
            input_text=f"update {issue_id} title=unsafe if_title=baseline\n",
        )
        if batch.returncode == 0 or title() != "baseline":
            raise AssertionError("bd batch unexpectedly accepted a predicate field")

        # A single raw SQL conditional UPDATE is the only discovered predicate +
        # write primitive.  Its true predicate changes exactly one row.
        if "1 rows affected" not in conditional("conditional-write"):
            raise AssertionError(
                "true conditional UPDATE did not report one changed row"
            )
        if title() != "conditional-write":
            raise AssertionError("true conditional UPDATE was not visible")

        # bd sql starts a fresh client each time: BEGIN / verifier / UPDATE /
        # ROLLBACK cannot retain one transaction across separate CLI invocations.
        reset()
        command(["bd", "sql", "BEGIN"], cwd=root)
        if "1 rows affected" not in conditional("separate-session-write"):
            raise AssertionError("separate-session UPDATE did not execute")
        command(["bd", "sql", "ROLLBACK"], cwd=root)
        if title() != "separate-session-write":
            raise AssertionError(
                "bd sql unexpectedly retained a transaction between invocations"
            )

        # An injected failed verifier, represented as a false SQL predicate in
        # the same statement, changes zero rows: no bad state becomes visible.
        reset()
        verifier_failure = conditional(
            "must-not-be-visible", "title = 'baseline' AND 1 = 0"
        )
        if "0 rows affected" not in verifier_failure or title() != "baseline":
            raise AssertionError("failed in-statement verifier made a write visible")

        # A direct, non-cooperating bd writer changes the row after planning but
        # before the conditional statement.  The stale version predicate rejects
        # the mutation (and therefore cannot expose conditional-write).
        command(
            ["bd", "update", issue_id, "--title", "non-cooperating-writer"], cwd=root
        )
        stale_precondition = conditional("conditional-write")
        if (
            "0 rows affected" not in stale_precondition
            or title() != "non-cooperating-writer"
        ):
            raise AssertionError("stale predicate did not abort before visibility")

        # The writer uses ordinary bd update and no shared lock or protocol.  On
        # every round it races the conditional write.  If it wins first, the
        # predicate must fail; if it loses, its unconditional write is final.
        stale_rejections = 0
        for _ in range(20):
            reset()
            writer = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--retrying-writer",
                    str(root),
                    issue_id,
                ],
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            # Start the contender while the independent writer is active.  The
            # small delay yields stale-predicate rejections while retaining real
            # overlapping client transactions.
            time.sleep(0.02)
            contender = subprocess.Popen(
                [
                    "bd",
                    "sql",
                    (
                        "UPDATE issues "
                        "SET title = 'conditional-write' "
                        f"WHERE id = '{issue_id}' AND title = 'baseline'"
                    ),
                ],
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            writer_stdout, writer_stderr = writer.communicate(timeout=30)
            contender_stdout, contender_stderr = contender.communicate(timeout=30)
            if writer.returncode or contender.returncode:
                raise RuntimeError(
                    "concurrent commands failed:\n"
                    f"writer={writer.returncode} {writer_stdout} {writer_stderr}\n"
                    f"contender={contender.returncode} {contender_stdout} {contender_stderr}"
                )
            if "0 rows affected" in contender_stdout:
                stale_rejections += 1
            if title() != "non-cooperating-writer":
                raise AssertionError(
                    "conditional write overwrote the non-cooperating writer"
                )

        print(
            "SUMMARY: PASS — raw bd sql conditional UPDATE rejected a prechanged "
            f"predicate and preserved the non-cooperating writer in 20/20 races "
            f"({stale_rejections} stale-race rejections); false verifier predicate wrote 0 rows; "
            "bd batch has no predicate and bd sql cannot span CLI invocations."
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        server_log.close()
        shutil.rmtree(root)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--retrying-writer":
        retrying_writer(Path(sys.argv[2]), sys.argv[3])
    else:
        main()
