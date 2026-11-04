#!/usr/bin/env python3
# ruff: noqa: E402
"""Align a diverged Dolt ``issues`` table to the remote's cells BEFORE merging, then restore.

A conflicted ``dolt_merge`` blocks every concurrent session on the machine, so this fleet
never enters one (``docs/procedures/beads-dolt-server-migration.md``). The merge is made
conflict-free instead: each row both sides touched is set to the REMOTE's cells in one
update plus one ``dolt_commit`` on the local branch, the merge then has nothing to disagree
about, and ``post`` puts back whatever local content the alignment overwrote. Used by hand on
2026-10-04 to merge AIDO (8 rows), AI_CLI (1), CORE (6), AIH (76) and the maintainer.

``pre``  (dry run unless ``--apply``)  classify with ``bd_dolt_conflict_classify`` and decide
    per row and per cell, refusing the whole run on the first row without a rule:

    * bookkeeping-only (``row_lock``, ``updated_at``, ``content_hash``, ``is_blocked``): take
      the remote's stamps.
    * ``metadata``: ours must be empty and the remote's ``display_id`` must equal our
      ``external_ref``; the display id then survives in the remote's metadata.
    * ``notes`` / ``close_reason``: the remote may already hold everything (nothing to
      restore), may be a subset of ours (restore ours after the merge), or the two may be
      disjoint (restore the remote's text, a marker, then ours, so both survive).
    * ``status``: only where the remote is ahead (the classifier's remote-superset).
    * ``closed_at`` / ``started_at``: only where ours is empty or the remote's status is ahead.
    * anything else: refuse. A rule is a judgement this tool is not entitled to invent.

    Every local value of every touched row is written to ``--backup-dir`` before the first
    mutation, together with the post-merge restore plan, so nothing is dropped. Only the
    cells BOTH sides changed are copied from the remote; every other cell is left to Dolt's
    merge, so a local-only change elsewhere in the row survives. Each row gets ONE update and
    the run ONE commit: a second UPDATE would stamp ``updated_at`` again and recreate the
    conflict this removes. Each update matches a row only on the ``row_lock`` the plan read,
    so a row a peer wrote since is left untouched.

``post``  after the merge commit, apply the saved restore plan in one commit and read every
    restored cell back. It writes a cell only while it still holds the remote value the
    alignment set, refuses a dirty working set before writing, refuses (after restoring the
    rest) when a cell was written since, and is safe to re-run.
"""

from __future__ import annotations

try:
    from py_interpreter_bootstrap import ensure_supported_python
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):
        """Stand in for the bootstrap when this file was copied without it."""
        import sys as _sys

        if _sys.version_info < minimum:
            _sys.stderr.write(
                "warning: {} runs under Python {} without its interpreter bootstrap\n".format(
                    __file__, ".".join(str(p) for p in _sys.version_info[:3])
                )
            )


ensure_supported_python()

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bd_dolt_conflict_classify as classify
from dolt_server import (
    DEFAULT_HOST,
    STORE_WRITE_TIMEOUT_SECONDS,
    DoltWriteTimeout,
    DoltWriteUnknown,
    shared_server_dir,
    shared_server_endpoint,
    shared_server_supervisor,
    statement_kind,
)
from bd_common import LockHeld, bounded_exclusive_file_lock

TABLE = "issues"
CONTENT_ALLOWED = frozenset(
    {"metadata", "notes", "status", "close_reason", "closed_at", "started_at"}
)
TEXT_RESTORE_COLUMNS = ("notes", "close_reason")
TIMESTAMP_COLUMNS = ("closed_at", "started_at")
BACKUP_NAME = "premerge-align.json"
ALIGNED_NAME = "premerge-aligned.json"
_COMMIT_HASH = re.compile(r"\A[0-9a-v]{32}\Z")
MARKER = "\n\n--- local text preserved at merge {when} ---\n"
# A write ended without a verified success: unlike a refusal (exit 2), something may have
# changed.
WRITE_UNKNOWN_EXIT = 4
SCRIPT = "scripts/bd_dolt_premerge_align.py"
# The write-ahead journal marker. /sync-git writes it BEFORE the alignment's first
# statement and removes it only after the restore reads back; every publisher refuses a
# store while one is pending (bd_store_sync, bd_dolt_publish.publish).
PENDING_MARKER = "PENDING-RESTORE"
# Where /sync-git keeps its run records and journals, inside the shared-server directory
# that already holds the per-database publish lock. Every publisher must agree on that
# directory or the lock that serialises them would not, so a journal here is found by each
# of them whatever ``XDG_STATE_HOME`` its process carries.
SYNC_STATE_DIRNAME = "sync-git"
# The write-ahead fence. Written durably BEFORE every statement a Server dispatches and
# cleared only after that statement's verified success. Any other end leaves it standing,
# and then pre, post and every publisher of the store refuse until an operator restarts the
# sql-server and runs ``post --confirm-server-restarted``.
FENCE_NAME = "write-fence.json"
CONFIRM_FLAG = "--confirm-server-restarted"
# A MySQL-protocol error code (`Error 1062 (HY000)`), kept for the message only: the text
# after it can carry row values, and no error, this one included, clears the fence.
_ERROR_CODE = re.compile(r"Error \d{4} \([0-9A-Z]{5}\)")
# Two reads of one process's start time use one formula and agree; a reused pid carries its
# own, later start time.
_SAME_START_SECONDS = 1e-3
# A directory cannot be opened for fsync on Windows; there `os.replace` is the durability
# step (example-client-host-windows is in the fleet).
_IS_WINDOWS = os.name == "nt"
LOCK_WAIT_SECONDS = 30.0


class Refusal(RuntimeError):
    """The run cannot be aligned under a stated rule; nothing was changed."""


def sync_state_root(state_dir: Path | None = None, home: Path | None = None) -> Path:
    """Where /sync-git keeps its run records and alignment journals.

    ``state_dir`` is a test override only: a journal written under it is invisible to the
    publishers, which all look beside the publish lock.
    """
    if state_dir is not None:
        return state_dir
    return shared_server_dir(home) / SYNC_STATE_DIRNAME


def publish_lock_path(database: str, home: Path | None = None) -> Path:
    """The per-database lock every publisher, /sync-git and this tool's writes share."""
    return shared_server_dir(home) / f"publish-{database}.lock"


def pending_journals(database: str, root: Path) -> list[Path]:
    """Every run directory of ``database`` under ``root`` still awaiting its restore."""
    return sorted(
        marker.parent
        for marker in (root / "beads" / database).glob(f"*/{PENDING_MARKER}")
    )


def recorded_post_command(database: str, backup: Path, *, confirm: bool = False) -> str:
    """The exact ``post`` invocation for ``backup``, ready to paste.

    It runs on the endpoint and home the alignment recorded in the backup, so a copied
    command reaches the server and the publish lock the alignment used.
    """
    try:
        saved = json.loads((backup / BACKUP_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    host, port, home = saved.get("host"), saved.get("port"), saved.get("home")
    parts = [f"python3 {SCRIPT} post --database {shlex.quote(database)}"]
    if isinstance(host, str) and host != DEFAULT_HOST:
        parts.append(f"--host {shlex.quote(host)}")
    if isinstance(port, int):
        parts.append(f"--port {port}")
    if isinstance(home, str):
        parts.append(f"--home {shlex.quote(home)}")
    parts.append(f"--backup-dir {shlex.quote(str(backup))}")
    if confirm:
        parts.append(CONFIRM_FLAG)
    return " ".join(parts)


def restart_instruction(home: Path | None = None) -> str:
    """How to restart the shared Beads sql-server here, from its installed supervisor."""
    supervisor = shared_server_supervisor(home)
    if supervisor is not None:
        how = f"`{' '.join(supervisor.restart_command)}` (its {supervisor.name} unit)"
    else:
        how = (
            "stopping the process that serves it and running "
            "`python3 scripts/dolt_server.py start` (`dolt_server.py stop` refuses while "
            "a working set is dirty, which a late write leaves)"
        )
    return (
        f"restart the shared Beads sql-server first, by {how}, since only a restart ends "
        "a statement whose result never arrived. Restarting it interrupts every "
        "session's Beads access on this host, so a session may run it only after "
        "messaging the chief of staff (the machine's coordinating session) and getting "
        "its go"
    )


def fence_refusal(database: str, backup: Path, home: Path | None = None) -> str:
    """The text every refusal on a standing fence prints: the restart, the fence, the command."""
    return (
        f"a write fence stands at {backup / FENCE_NAME}: a statement was sent without a "
        f"verified success and may still land, so pre, post and every publish of "
        f"{database} refuse until it is cleared. {restart_instruction(home)}; then run "
        f"`{recorded_post_command(database, backup, confirm=True)}`"
    )


def server_evidence(port: int, home: Path | None = None) -> dict | None:
    """The shared server's recorded pid and its start time, as plain evidence, or None.

    One read of bd's own pid file and one start-time read, and only when that file names
    the port being written to. This proves nothing about which process answers the port;
    it is recorded so that ``post --confirm-server-restarted`` can refuse an attestation
    that is plainly false.
    """
    try:
        import psutil
    except ImportError:
        return None
    recorded = shared_server_endpoint(home)
    if recorded is None or recorded.port != port or not recorded.pid:
        return None
    try:
        return {
            "pid": recorded.pid,
            "started": psutil.Process(recorded.pid).create_time(),
        }
    except (psutil.Error, OSError):
        return None


def _recorded_server_running(evidence: dict) -> bool | None:
    """Whether the exact recorded process, same pid and same start time, is alive.

    A reused pid shows a different start time. None when it cannot be checked (no psutil,
    or an unreadable answer), which the caller refuses like a live one.
    """
    try:
        import psutil
    except ImportError:
        return None
    try:
        started = psutil.Process(int(evidence["pid"])).create_time()
    except psutil.NoSuchProcess:
        return False
    except (psutil.Error, OSError, KeyError, TypeError, ValueError):
        return None
    return abs(started - float(evidence["started"])) < _SAME_START_SECONDS


def _sql_text(value: str | None) -> str:
    """A string literal that survives any content: hex-encoded, never interpolated."""
    if value is None:
        return "NULL"
    return f"CAST(UNHEX('{value.encode().hex()}') AS CHAR CHARACTER SET utf8mb4)"


def _id_list(ids: list[str]) -> str:
    return ",".join(_sql_text(i) for i in ids)


def _fsync_directory(directory: Path) -> None:
    """Make a rename or unlink in ``directory`` durable; a no-op on Windows (see above)."""
    if _IS_WINDOWS:
        return
    handle = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _durable_write(path: Path, payload: dict) -> None:
    """Replace ``path`` with ``payload`` so a crash leaves the old file or the new, never half."""
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(payload, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    _fsync_directory(path.parent)


def _durable_unlink(path: Path) -> None:
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


class Server:
    """One Dolt database on the shared server. Reads go through the classifier's guard.

    Every write is fenced: ``write`` refuses unless ``_fenced`` has set a fence, and the
    raw client call is private to ``_dispatch``, which only ``write`` calls
    (``tests/test_dolt_client_query_timeout.py`` fails if any other path reaches it).
    """

    def __init__(
        self, database: str, host: str, port: int, home: Path | None = None
    ) -> None:
        self.database, self.host, self.port = database, host, port
        self.home = home
        self.fence: Path | None = None
        self._evidence: dict | None = None
        self._evidence_read = False

    def read(self, sql: str) -> list[dict]:
        return classify.run_query(
            sql, database=self.database, host=self.host, port=self.port
        )

    def evidence(self) -> dict | None:
        """``server_evidence`` for this endpoint, read once per Server."""
        if not self._evidence_read:
            self._evidence = server_evidence(self.port, self.home)
            self._evidence_read = True
        return self._evidence

    def write(self, sql: str, *, rows: Sequence[str] = ()) -> str:
        """Run one statement under the fence, bounded by ``STORE_WRITE_TIMEOUT_SECONDS``.

        The statement's kind, rows, endpoint and the server evidence are written durably
        BEFORE it is dispatched, and cleared only after its verified success (rc 0). Any
        other end leaves the fence standing: a timeout (``DoltWriteTimeout``), any error,
        a SQL error included (``DoltWriteUnknown``), or this process dying mid-statement.
        ``main`` exits ``WRITE_UNKNOWN_EXIT``; ``bd_store_sync`` refuses and keeps the run's
        journal, which every publisher then refuses on.
        """
        if self.fence is None:
            raise RuntimeError(
                f"refusing an unfenced write to {self.database} "
                f"(`{statement_kind(sql)}`): every Server write runs inside _fenced"
            )
        try:
            _durable_write(
                self.fence,
                {
                    "statement": statement_kind(sql),
                    "rows": list(rows),
                    "database": self.database,
                    "host": self.host,
                    "port": self.port,
                    "server": self.evidence(),
                    "dispatched_at": time.time(),
                },
            )
        except OSError as exc:
            raise RuntimeError(
                f"the write fence {self.fence} could not be written ({exc}); the "
                f"`{statement_kind(sql)}` was not sent"
            ) from exc
        try:
            proc = self._dispatch(sql)
        except subprocess.TimeoutExpired as exc:
            raise DoltWriteTimeout(
                sql, rows, self.host, self.port, STORE_WRITE_TIMEOUT_SECONDS
            ) from exc
        if proc.returncode != 0:
            code = _ERROR_CODE.search(proc.stderr)
            raise DoltWriteUnknown(
                sql,
                rows,
                self.host,
                self.port,
                f"failed (rc={proc.returncode}, "
                f"{code.group(0) if code else 'no server error code'}); no error clears "
                f"the fence at {self.fence}",
            )
        _durable_unlink(self.fence)
        return proc.stdout

    def _dispatch(self, sql: str) -> subprocess.CompletedProcess[str]:
        argv = [
            "dolt", "--host", self.host, "--port", str(self.port), "--user", "root",
            "--password", "", "--no-tls", "--use-db", self.database, "sql",
            "-r", "csv", "-q", sql,
        ]  # fmt: skip
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=STORE_WRITE_TIMEOUT_SECONDS,
        )


def _refuse_while_fenced(server: Server, backup: Path) -> None:
    """Refuse while a fence stands in ``backup``; ``post --confirm-server-restarted`` clears it."""
    if (backup / FENCE_NAME).exists():
        raise Refusal(fence_refusal(server.database, backup, server.home))


@contextmanager
def _fenced(server: Server, backup: Path) -> Iterator[None]:
    """Let ``server`` write inside the block, every statement fenced in ``backup``."""
    _refuse_while_fenced(server, backup)
    server.fence = backup / FENCE_NAME
    try:
        yield
    finally:
        server.fence = None


def confirm_server_restarted(server: Server, backup: Path) -> bool:
    """Accept the operator's attestation that the sql-server restarted; clear the fence.

    One check, never a listener scan: when the fence recorded the server's pid and start
    time and that exact process is still alive, the attestation is false and this refuses.
    With no recorded pid, the attestation alone is enough. Returns whether a fence stood.
    """
    path = backup / FENCE_NAME
    if not path.exists():
        return False
    try:
        evidence = json.loads(path.read_text(encoding="utf-8")).get("server")
    except (OSError, ValueError, AttributeError):
        evidence = None
    running = (
        _recorded_server_running(evidence) if isinstance(evidence, dict) else False
    )
    if running is None:
        raise Refusal(
            f"could not check whether the recorded server (pid {evidence.get('pid')}) "
            f"is still running (psutil is missing or its answer was unreadable), so the "
            f"attestation is not accepted; the fence at {path} stays"
        )
    if running:
        raise Refusal(
            f"the recorded server is still running; restart it first (pid "
            f"{evidence.get('pid')} with its recorded start time is alive). "
            f"{restart_instruction(server.home)}; the fence at {path} stays"
        )
    _durable_unlink(path)
    return True


def _conflict_rows(
    server: Server, local: str, remote: str
) -> list[classify.RowVerdict]:
    summary = server.read(classify.summary_query(local, remote))
    other = [
        str(r["table"])
        for r in summary
        if (int(r["num_data_conflicts"]) or int(r["num_schema_conflicts"]))
        and str(r["table"]) != TABLE
    ]
    if other:
        raise Refusal(f"conflicts outside {TABLE}: {', '.join(other)}")
    if not any(str(r["table"]) == TABLE for r in summary):
        return []
    rows = server.read(classify.preview_query(local, remote, TABLE))
    return classify.classify_rows(rows)


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {str(r["id"]): r for r in rows}


def _text_restore(ours: str, theirs: str) -> str | None:
    """What local text must survive a take-the-remote alignment, or None if nothing is lost."""
    if not ours or ours == theirs:
        return None
    if not theirs or theirs in ours:
        return ours
    if ours not in theirs:
        return theirs + MARKER + ours
    return None


def _check_cell(column: str, v: classify.RowVerdict, l_row: dict, r_row: dict) -> None:
    """Raise Refusal unless taking the remote's value for this cell loses nothing."""
    cells = {c.column: c for c in v.cells}
    status_ahead = (
        "status" not in cells or cells["status"].side == classify.REMOTE_SUPERSET
    )
    if column == "metadata":
        theirs = json.loads(r_row.get("metadata") or "{}")
        ours_empty = (l_row.get("metadata") or "") in ("", "{}")
        if not ours_empty or theirs.get("display_id") != l_row.get("external_ref"):
            raise Refusal(f"row {v.row_id}: metadata fails the display-id rule")
    elif column == "status" and not status_ahead:
        raise Refusal(f"row {v.row_id}: local status is not behind the remote's")
    elif (
        column in TIMESTAMP_COLUMNS
        and l_row.get(column) not in (None, "")
        and not status_ahead
    ):
        raise Refusal(f"row {v.row_id}: {column} set locally, remote not ahead")


def _row_treatment(v: classify.RowVerdict, l_row: dict, r_row: dict) -> dict[str, str]:
    """Validate every content cell of one row; return the local text to restore after merge."""
    unruled = set(v.content_columns) - CONTENT_ALLOWED
    if unruled:
        raise Refusal(f"row {v.row_id}: no rule for column(s) {sorted(unruled)}")
    restore: dict[str, str] = {}
    for column in v.content_columns:
        _check_cell(column, v, l_row, r_row)
        if column in TEXT_RESTORE_COLUMNS:
            kept = _text_restore(l_row.get(column) or "", r_row.get(column) or "")
            if kept is not None:
                restore[column] = kept
    return restore


def plan(server: Server, local: str, remote: str) -> dict:
    """Decide every row's treatment. Raises Refusal on the first row without a rule."""
    verdicts = _conflict_rows(server, local, remote)
    bookkeeping = [v.row_id for v in verdicts if v.verdict == classify.BOOKKEEPING_ONLY]
    content = [v for v in verdicts if v.verdict != classify.BOOKKEEPING_ONLY]
    ids = [v.row_id for v in verdicts]
    if not ids:
        return {
            "bookkeeping": [],
            "content": [],
            "cells": {},
            "restore": {},
            "local": [],
            "remote": [],
        }
    local_rows = server.read(f"select * from {TABLE} where id in ({_id_list(ids)})")
    remote_rows = server.read(
        f"select * from {TABLE} as of '{remote}' where id in ({_id_list(ids)})"
    )
    loc, rem = _by_id(local_rows), _by_id(remote_rows)
    restore: dict[str, dict[str, str]] = {}
    for v in content:
        if v.row_id not in loc or v.row_id not in rem:
            raise Refusal(
                f"row {v.row_id}: not present on both sides ({local} / {remote})"
            )
        kept = _row_treatment(v, loc[v.row_id], rem[v.row_id])
        if kept:
            restore[v.row_id] = kept
    return {
        "bookkeeping": bookkeeping,
        "content": [v.row_id for v in content],
        # Only the cells BOTH sides changed: Dolt merges every other cell by itself, so
        # copying one from the remote would only overwrite a local-only change.
        "cells": {v.row_id: sorted(v.conflict_columns) for v in verdicts},
        "restore": restore,
        "local": local_rows,
        "remote": remote_rows,
    }


def _unchanged_since_plan(decided: dict, ids: list[str]) -> str:
    """A compare-and-swap clause: each row's ``row_lock`` still equals what the plan read.

    bd rewrites ``row_lock`` on every write, so a row a peer wrote between the plan and the
    update no longer matches and is left untouched -- its new value was never backed up, so
    overwriting it would lose it. The merge then still conflicts on that row, which the
    caller reads as "conflicts remain" and refuses on.
    """
    local = _by_id(decided["local"])
    clauses = []
    for row_id in ids:
        lock = local.get(row_id, {}).get("row_lock")
        matches = (
            "l.row_lock is null"
            if lock in (None, "")
            else f"cast(l.row_lock as char) = {_sql_text(str(lock))}"
        )
        clauses.append(f"(l.id = {_sql_text(row_id)} and {matches})")
    return " or ".join(clauses)


def _is_clean(server: Server) -> bool:
    rows = server.read("select count(*) as n from dolt_status")
    return not rows or str(rows[0].get("n")) == "0"


_DIFF_META = frozenset({"commit", "commit_date"})


def _commit_and_read(
    server: Server, message: str, *, rows: Sequence[str] = ()
) -> tuple[dict[str, dict[str, object]], list[str]]:
    """Commit the working set; return ``{issue id: {column: new value}}`` it changed, plus
    anything else it carried.

    ``dolt_commit('-a')`` sweeps in whatever is uncommitted, and a peer's write can land
    between a caller's clean-working-set check and this commit -- in another row, or in a
    cell of a row the caller touched. Nothing is lost that way (the write is committed,
    locally), but it must not travel on as this step's own work, so callers compare every
    changed cell, by VALUE, against what they wrote (``_refuse_foreign``). The diff is read
    against the hash this call's commit returned, never HEAD, so a peer's commit landing
    right after it cannot be read as this one. An empty working set commits nothing.
    """
    if _is_clean(server):
        return {}, []
    output = server.write(
        f"call dolt_commit('-a', '-m', {_sql_text(message)})", rows=rows
    )
    commit = output.strip().splitlines()[-1].strip().strip('"') if output else ""
    if not _COMMIT_HASH.match(commit):
        return {}, [f"a commit whose hash could not be read back ({commit!r})"]
    tables = {
        str(r.get("t"))
        for r in server.read(
            f"select to_table_name as t from dolt_diff_summary('{commit}~1','{commit}')"
        )
    }
    other = sorted(tables - {TABLE})
    changed: dict[str, dict[str, object]] = {}
    for row in server.read(
        f"select * from dolt_diff('{commit}~1','{commit}','{TABLE}')"
    ):
        if row.get("diff_type") != "modified":
            other.append(f"a {row.get('diff_type')} row")
            continue
        changed[str(row.get("to_id"))] = {
            key[3:]: row.get(key)
            for key in set(row) | {"to_" + k[5:] for k in row if k.startswith("from_")}
            if key.startswith("to_")
            and key[3:] not in _DIFF_META
            and row.get(key) != row.get("from_" + key[3:])
        }
    return changed, other


def _refuse_foreign(
    changed: dict[str, dict[str, object]],
    other: list[str],
    expected: dict[str, dict[str, object]],
) -> None:
    """Refuse a commit carrying any cell this step did not write with the value it wrote."""
    foreign = other + sorted(
        f"{row_id}.{column}"
        for row_id, cells in changed.items()
        for column, value in cells.items()
        if column not in expected.get(row_id, {})
        or _cell(value) != _cell(expected[row_id][column])
    )
    if foreign:
        raise Refusal(
            "the commit also carries changes this step did not make "
            f"({', '.join(foreign)}); it stays local, nothing was lost, and it must be "
            "classified before anything publishes it"
        )


def _cell(value: object) -> str | None:
    """A cell as both reads render it (``dolt sql -r json`` omits NULL, so None is NULL)."""
    return None if value is None else str(value)


def apply_pre(
    server: Server, decided: dict, remote: str, backup: Path, label: str
) -> None:
    """Back up both sides, then align each row's two-sided cells in ONE update, ONE commit.

    Only the cells both sides changed are copied from the remote (``decided["cells"]``);
    every other cell is left for Dolt's merge, so a local-only change elsewhere in the row
    survives. Each row gets one UPDATE, because a second one would stamp it again.

    ``remote`` should be an immutable commit hash: the plan read the remote side at one
    revision, and a symbolic ref that moved since would align rows to cells nobody planned.
    Records in ``ALIGNED_NAME`` the ids the commit actually changed, which is how ``post``
    tells a row this step aligned from one its compare-and-swap left alone.
    """
    _refuse_while_fenced(server, backup)
    backup.mkdir(parents=True, exist_ok=True)
    (backup / BACKUP_NAME).write_text(
        json.dumps(
            {
                "database": server.database,
                "host": server.host,
                "port": server.port,
                "home": None if server.home is None else str(server.home),
                "remote_rev": remote,
                "local": decided["local"],
                "remote": decided["remote"],
                "restore": decided["restore"],
                "cells": decided["cells"],
            },
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )
    remote_rows = _by_id(decided["remote"])
    expected: dict[str, dict[str, object]] = {}
    with _fenced(server, backup):
        for row_id in [*decided["content"], *decided["bookkeeping"]]:
            columns = decided["cells"].get(row_id) or []
            if not columns:
                continue
            sets = ", ".join(f"l.{c}=r.{c}" for c in columns)
            server.write(
                f"update {TABLE} l join (select * from {TABLE} as of '{remote}') r "
                f"on l.id=r.id set {sets} where l.id = {_sql_text(row_id)} "
                f"and ({_unchanged_since_plan(decided, [row_id])})",
                rows=[row_id],
            )
            expected[row_id] = {c: remote_rows.get(row_id, {}).get(c) for c in columns}
        changed, other = _commit_and_read(
            server,
            f"{label}: align {len(expected)} rows to {remote} before merge "
            f"(local values saved in {BACKUP_NAME})",
            rows=sorted(expected),
        )
    (backup / ALIGNED_NAME).write_text(
        json.dumps({"aligned": sorted(set(changed) & set(expected))}), encoding="utf-8"
    )
    _refuse_foreign(changed, other, expected)


def _restorable(
    restore: dict, saved: dict, current: dict, aligned: set[str] | None
) -> tuple[dict, list[str]]:
    """Split the saved restore plan by what each cell holds NOW.

    A row ``pre`` never changed (its compare-and-swap left it alone) needs nothing. In a row it
    aligned, a cell holding the saved text is done (an earlier ``post``), and a cell still
    holding the remote value it wrote gets the saved text. Anything else, the original local
    value included, was written by someone after the alignment: restoring over it would
    overwrite that write, so it is returned as moved and left alone. ``aligned`` is ``None``
    when ``pre`` died before recording it; every row then counts as aligned, which fails closed.
    """
    remote = _by_id(saved.get("remote", []))
    pending: dict[str, dict[str, str]] = {}
    moved: list[str] = []
    for row_id, cells in restore.items():
        if aligned is not None and row_id not in aligned:
            continue
        now = current.get(row_id, {})
        for column, value in cells.items():
            held = now.get(column)
            if held == value:
                continue
            if held == remote.get(row_id, {}).get(column):
                pending.setdefault(row_id, {})[column] = value
            else:
                moved.append(f"{row_id}.{column}")
    return pending, moved


def _recorded_alignment(backup: Path) -> set[str] | None:
    try:
        return set(
            json.loads((backup / ALIGNED_NAME).read_text(encoding="utf-8"))["aligned"]
        )
    except FileNotFoundError:
        return None


def apply_post(
    server: Server, backup: Path, label: str, *, confirmed: bool = False
) -> int:
    """Restore the saved local text, cell by cell, only where the alignment's value still sits.

    Refuses before writing anything when the backup belongs to another database, a write
    fence stands (unless ``confirmed``: the operator attests the sql-server restarted, and
    ``confirm_server_restarted`` clears the fence first, even when nothing is left to
    restore), or the working set is dirty. Each UPDATE repeats the remote value in its WHERE clause, so a cell
    written between this read and the write is not overwritten, and the restore commit is
    checked to carry only the restored rows. Safe to re-run: a restored cell counts as done.
    """
    saved = json.loads((backup / BACKUP_NAME).read_text(encoding="utf-8"))
    if saved.get("database") != server.database:
        raise Refusal(
            f"{backup / BACKUP_NAME} was taken from database {saved.get('database')!r}, "
            f"not {server.database!r}; nothing was restored"
        )
    if confirmed:
        fence = backup / FENCE_NAME
        if fence.exists() and not _is_clean(server):
            # The working set may hold the statement the fence stands for, landed late.
            # Nothing here can tell it from another session's write, so a person decides.
            raise Refusal(
                f"the working set is dirty, possibly with the write the fence at {fence} "
                f"stands for, so the fence stays. Inspect it with `select * from "
                f"dolt_diff('HEAD','WORKING','{TABLE}')` and `select * from "
                f"dolt_status`, commit or discard it by hand, then re-run "
                f"`{recorded_post_command(server.database, backup, confirm=True)}`"
            )
        confirm_server_restarted(server, backup)
    _refuse_while_fenced(server, backup)
    restore = saved["restore"]
    if not restore:
        return 0
    if not _is_clean(server):
        raise Refusal("the working set is dirty; nothing was restored, re-run post")
    columns = sorted({c for cells in restore.values() for c in cells})
    current = _by_id(
        server.read(
            f"select id, {', '.join(columns)} from {TABLE} "
            f"where id in ({_id_list(list(restore))})"
        )
    )
    pending, moved = _restorable(restore, saved, current, _recorded_alignment(backup))
    remote = _by_id(saved.get("remote", []))
    with _fenced(server, backup):
        for row_id, cells in pending.items():
            sets = ", ".join(f"{c}={_sql_text(v)}" for c, v in cells.items())
            still = " and ".join(
                f"{c} <=> {_sql_text(remote[row_id].get(c))}" for c in cells
            )
            server.write(
                f"update {TABLE} set {sets} where id={_sql_text(row_id)} and {still}",
                rows=[row_id],
            )
        changed, other = (
            _commit_and_read(
                server,
                f"{label}: restore local text after merge ({len(pending)} rows)",
                rows=sorted(pending),
            )
            if pending
            else ({}, [])
        )
    for row_id, cells in pending.items():
        got = server.read(
            f"select {', '.join(cells)} from {TABLE} where id={_sql_text(row_id)}"
        )
        if not got or any(got[0].get(c) != v for c, v in cells.items()):
            raise Refusal(f"row {row_id}: restored cells did not read back")
    _refuse_foreign(changed, other, pending)
    if moved:
        raise Refusal(
            f"cell(s) written after the alignment were left untouched: {', '.join(moved)}; "
            f"their saved local text is in {backup / BACKUP_NAME}"
        )
    return len(pending)


def resolve_revision(server: Server, revision: str) -> str:
    """``revision`` as the immutable commit hash it names right now.

    The plan, the backup and the update must all read one revision. Passing a branch or a
    remote-tracking ref through would let a fetch between the dry run and ``--apply`` (or
    between the plan and the update) change which remote cells are copied.
    """
    if _COMMIT_HASH.match(revision):
        return revision
    for table in ("dolt_remote_branches", "dolt_branches"):
        rows = server.read(
            f"select hash from {table} where name = {_sql_text(revision)}"
        )
        if rows:
            return str(rows[0]["hash"])
    raise Refusal(f"{revision!r} names no branch or remote-tracking ref")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", choices=("pre", "post"))
    parser.add_argument("--database", required=True)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--local-rev", default="main")
    parser.add_argument("--remote-rev", default="remotes/origin/main")
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument(
        "--apply", action="store_true", help="pre: mutate (default is a dry run)"
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="the home whose ~/.beads/shared-server holds the publish lock (default: $HOME)",
    )
    parser.add_argument(
        CONFIRM_FLAG,
        dest="confirm_server_restarted",
        action="store_true",
        help="post: attest the sql-server was restarted since a write fence was set",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    server = Server(
        args.database, args.host, classify.resolve_port(args.port), args.home
    )
    try:
        if args.mode == "post" or args.apply:
            # The same per-database lock /sync-git and every publisher hold, so two posts
            # cannot interleave on one fence and no publish runs between a write and its result.
            with bounded_exclusive_file_lock(
                publish_lock_path(args.database, args.home), LOCK_WAIT_SECONDS
            ):
                return _locked(args, server)
        return _locked(args, server)
    except LockHeld as exc:
        print(
            f"refusing, nothing changed: the {args.database} publish lock is held by a "
            f"publisher, /sync-git or another post ({exc}); re-run once it finishes",
            file=sys.stderr,
        )
        return 2
    except Refusal as exc:
        print(f"refusing, nothing changed: {exc}", file=sys.stderr)
        return 2
    except DoltWriteUnknown as exc:
        print(
            f"stopped on a write whose outcome is unknown: {exc}. Both sides of every "
            f"touched row are saved in {args.backup_dir / BACKUP_NAME}. "
            f"{fence_refusal(server.database, args.backup_dir, server.home)}, which writes "
            "only cells that still hold the aligned value",
            file=sys.stderr,
        )
        return WRITE_UNKNOWN_EXIT


def _refuse_any_store_fence(args: argparse.Namespace) -> None:
    """Refuse while any /sync-git journal of this database holds a standing fence.

    A fence stops the whole store, not only its own backup: a by-hand ``pre`` into a fresh
    directory, or a ``post`` of another backup, would otherwise write while a statement
    from the fenced run may still land. Only that journal's own confirmed post clears it.
    """
    own = args.backup_dir.resolve()
    for journal in pending_journals(args.database, sync_state_root(home=args.home)):
        if (journal / FENCE_NAME).exists() and journal.resolve() != own:
            raise Refusal(fence_refusal(args.database, journal, args.home))


def _refuse_noncanonical_backup(args: argparse.Namespace) -> None:
    """Refuse a ``pre --apply`` backup outside the canonical journal tree.

    A manual run is a journal like /sync-git's: its backup is a direct child of
    ``sync_state_root()/beads/<DB>/``, the one tree every publisher already scans for
    ``PENDING-RESTORE``, so its fence stops them all without a second discovery path.
    """
    tree = sync_state_root(home=args.home) / "beads" / args.database
    if args.backup_dir.resolve().parent != tree.resolve():
        raise Refusal(
            f"--backup-dir {args.backup_dir} is not a journal directory: pre --apply "
            f"writes only into a direct child of {tree}, which every publisher scans, "
            f"so pass --backup-dir {tree}/<run-name>"
        )


def _refuse_a_moved_remote(
    server: Server, revision: str, planned: str, marker: Path
) -> None:
    """Refuse, removing ``marker``, when ``revision`` moved since the plan read it.

    A publish landing between the planning reads and the marker would leave the plan's
    remote cells stale. So, once the marker is durable and before the first write, the
    revision is resolved again; a moved one, or one that cannot be read, writes nothing.
    """
    try:
        now = resolve_revision(server, revision)
    except BaseException:
        _durable_unlink(marker)
        raise
    if now != planned:
        _durable_unlink(marker)
        raise Refusal(
            f"{revision} moved from the planned {planned} to {now} between the plan and "
            "the journal marker, so the plan's remote cells are stale; nothing was "
            "written and the marker was removed. Re-run pre --apply to plan the new head"
        )


def _locked(args: argparse.Namespace, server: Server) -> int:
    """The work of ``main`` once its lock (when it writes) is held."""
    if args.mode == "pre" and args.apply:
        _refuse_noncanonical_backup(args)
    if args.mode == "post" or args.apply:
        _refuse_any_store_fence(args)
    if args.mode == "post":
        restored = apply_post(
            server,
            args.backup_dir,
            args.database,
            confirmed=args.confirm_server_restarted,
        )
        print(f"restored {restored} row(s), read back")
        return 0
    if args.confirm_server_restarted:
        raise Refusal(f"{CONFIRM_FLAG} is a post option; pre never clears a fence")
    remote_rev = resolve_revision(server, args.remote_rev)
    decided = plan(server, args.local_rev, remote_rev)
    print(
        f"{args.database}: bookkeeping-only={len(decided['bookkeeping'])} "
        f"content={len(decided['content'])} "
        f"restore-after-merge={ {k: sorted(v) for k, v in decided['restore'].items()} }"
    )
    if not args.apply:
        print("dry run; nothing changed (pass --apply)")
        return 0
    # Durably, before its first write (the planning reads come first by design, and a
    # dry run leaves no marker): from here every publisher refuses the store until post
    # restores it and the marker is deleted, exactly as for a /sync-git run.
    _refuse_while_fenced(server, args.backup_dir)
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    marker = args.backup_dir / PENDING_MARKER
    _durable_write(marker, {"written_by": "pre --apply", "remote_rev": remote_rev})
    _refuse_a_moved_remote(server, args.remote_rev, remote_rev, marker)
    apply_pre(server, decided, remote_rev, args.backup_dir, args.database)
    left = server.read(classify.summary_query(args.local_rev, remote_rev))
    remaining = [r for r in left if int(r["num_data_conflicts"])]
    if remaining:
        print(f"conflicts remain after alignment: {remaining}", file=sys.stderr)
        return 3
    print("aligned; a merge now enters no conflicted state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
