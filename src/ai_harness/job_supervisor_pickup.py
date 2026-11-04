"""Fail-open terminal-job pickup for both fallback and live-wake hooks."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from typing import Any

from ai_harness.job_supervisor import TERMINAL_STATES, Ledger

ADMISSION_MODULE = "ai_harness.quota_traffic.admission"

MAX_JOBS = 5
MAX_DESCRIPTION_CHARS = 120
MAX_WATCH_PATHS = 50


def _description(value: str) -> str:
    compact = " ".join(value.split())
    if len(compact) > MAX_DESCRIPTION_CHARS:
        return f"{compact[: MAX_DESCRIPTION_CHARS - 1]}…"
    return compact or "(no description)"


def _load_admission() -> Any | None:
    """Return the quota admission module, or None when this checkout does not include it.

    The admission component is optional: a source extract may omit it. In that case every
    pending job is admitted and one plain line says why, rather than failing silently.
    """
    try:
        return importlib.import_module(ADMISSION_MODULE)
    except ModuleNotFoundError as error:
        # Whole module names only: a string prefix such as "ai_harness.quota" is a
        # different module, and a typo there must stay loud.
        if error.name in {ADMISSION_MODULE, ADMISSION_MODULE.rpartition(".")[0]}:
            print(
                "quota admission: component not included in this extract; "
                "admitting all pending jobs",
                file=sys.stderr,
            )
            return None
        raise


def _pending(ledger: Ledger) -> tuple[list[dict[str, str]], list[str]]:
    pending: list[dict[str, str]] = []
    watches: list[str] = []
    for candidate in ledger.jobs_root.iterdir():
        try:
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            record = ledger.read(candidate.name)
            job_id = record.get("job_id")
            state = record.get("state")
            description = record.get("command_description")
            if job_id != candidate.name or not isinstance(description, str):
                continue
            if state in TERMINAL_STATES:
                if record.get("acknowledged_at") is None:
                    pending.append(
                        {
                            "job_id": job_id,
                            "state": state,
                            "description": _description(description),
                            "finished_at": str(record.get("finished_at") or ""),
                        }
                    )
            else:
                watches.append(str(ledger.record_path(job_id)))
        except Exception:  # noqa: BLE001, S112 - fail-open hook boundary
            continue
    return pending, watches


def run(event_name: str, *, watch_paths: bool = False) -> int:  # noqa: C901 - watch setup and ordered admission before output
    """Emit advisory context from the ledger, never changing its state."""
    try:
        ledger = Ledger.from_environment()
        if not ledger.jobs_root.is_dir():
            return 0
        pending, watches = _pending(ledger)
    except Exception:  # noqa: BLE001 - fail-open hook boundary
        return 0

    output: dict[str, Any] = {"hookEventName": event_name}
    if watch_paths:
        # Watch active records individually because atomic replacement is the
        # ledger's completion signal.  The directory watch also catches a job
        # submitted after SessionStart on platforms that report child changes. The
        # CwdChanged wiring re-emits this list, because a cd replaces it.
        output["watchPaths"] = [str(ledger.jobs_root), *watches[:MAX_WATCH_PATHS]]
    # A CwdChanged run only re-arms the watches a cd replaced (ISSUE-REF); the pending
    # summary belongs to the prompt and SessionStart pickups, not to every cd.
    total = len(pending)
    if pending and event_name != "CwdChanged":
        pending.sort(key=lambda record: (record["finished_at"], record["job_id"]))
        try:
            payload = json.load(sys.stdin)
        except (OSError, ValueError):
            payload = {}
        session = str(
            payload.get("session_id")
            or os.environ.get("CLAUDE_SESSION_ID", "job-pickup")
        )
        admission = _load_admission()
        admitted = []
        for record in pending:
            if admission is not None and not admission.check(
                "job-pickup", session, record["job_id"]
            ):
                continue
            if len(admitted) < MAX_JOBS:
                admitted.append(record)
                if admission is not None:
                    admission.complete("job-pickup", session, record["job_id"])
        pending = admitted
    if pending and event_name != "CwdChanged":
        shown = pending[:MAX_JOBS]
        context = (
            f"Local job supervisor: {total} terminal job result(s) await review. "
            "The ledger at ~/.claude/job-ledger/ is authoritative."
        )
        context += "\n" + "\n".join(
            f"- {record['job_id']} [{record['state']}] {record['description']}"
            for record in shown
        )
        if total > MAX_JOBS:
            context += f"\nShowing {MAX_JOBS} oldest finished jobs; inspect the ledger for the rest."
        output["additionalContext"] = context
    if len(output) == 1:
        return 0
    print(json.dumps({"hookSpecificOutput": output}))
    return 2 if event_name == "FileChanged" and pending else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("event_name")
    parser.add_argument("--watch-paths", action="store_true")
    args = parser.parse_args()
    return run(args.event_name, watch_paths=args.watch_paths)


if __name__ == "__main__":
    raise SystemExit(main())
