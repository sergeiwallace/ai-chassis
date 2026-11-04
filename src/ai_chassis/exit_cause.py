"""Why the previous run of a resume key ended (ISSUE-REF).

Design: ``docs/designs/issue-ref-typing-aware-launch-resume.md``, Exit-cause classification.

Three records decide it: the ISSUE-REF stamp ``boot-stamps/<key>.json`` (boot id, pid,
pane, tty), the ``SessionEnd`` record ``launch/<key>.end.json`` written while the process
still knew why it stopped, and the optional launcher exit stamp
``launch/<key>.launcher-exit.json``. The hook's reason wins over the launcher's, because the
hook ran inside the process; both are kept as evidence when they disagree.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_chassis import host_boot
from ai_chassis.host_boot import BootIdentity, BootIdentityUnavailable

CAUSES = (
    "manual_exit",
    "terminal_quit_or_crash",
    "host_reboot",
    "fresh_launch",
    "unknown",
    "still_running",
)
EXIT_REASONS = frozenset({"prompt_input_exit", "logout", "other"})
NOT_EXITS = frozenset({"clear", "resume"})
STAMP_DIRECTORY = "boot-stamps"
EXIT_RECORDED_STAGE = "CC-LAUNCH-RESUME-EXIT-RECORDED"
CLASSIFIED_STAGE = "CC-LAUNCH-RESUME-CLASSIFIED"
# `record-end` exits with this when the ending claude does not own the pane, so the hook also
# skips the registry call (ISSUE-REF).
NOT_PANE_OWNER_EXIT = 3


@dataclass(frozen=True)
class Classification:
    cause: str
    evidence: dict[str, Any]


def _launch(resume_root: Path) -> Path:
    return resume_root / host_boot.LAUNCH_DIRECTORY


def _stamp_path(key: str, resume_root: Path) -> Path:
    return resume_root / STAMP_DIRECTORY / f"{key}.json"


def _epoch(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def read_end(key: str, *, resume_root: Path) -> dict[str, Any] | None:
    return host_boot.read_json(_launch(resume_root) / f"{key}.end.json")


def read_launcher_exit(key: str, *, resume_root: Path) -> dict[str, Any] | None:
    return host_boot.read_json(_launch(resume_root) / f"{key}.launcher-exit.json")


def _launcher_implies(launcher: dict[str, Any]) -> str:
    if launcher.get("signal") or launcher.get("tty_alive_at_exit") is False:
        return "terminal_quit_or_crash"
    return "manual_exit"


def _current_end(
    end: dict[str, Any] | None, stamp: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The end record of the PREVIOUS run only: one older than the newest stamp is stale."""
    if end is None or end.get("reason") not in EXIT_REASONS:
        return None
    if stamp is None:
        return end
    ended, written = _epoch(end.get("ended_at")), _epoch(stamp.get("written_at"))
    if ended is not None and written is not None and ended < written:
        return None
    return end


def _pane_gone(stamp: dict[str, Any], live_pane_ids: frozenset[str]) -> bool:
    pane_id = stamp.get("pane_id")
    if isinstance(pane_id, str) and pane_id:
        return pane_id not in live_pane_ids
    tty = stamp.get("tty")
    return not (isinstance(tty, str) and tty and Path(tty).exists())


def classify(  # noqa: PLR0911, PLR0913 - the binding interface; one return per table row
    key: str,
    *,
    resume_root: Path,
    boot: BootIdentity,
    live_pane_ids: frozenset[str],
    pid_alive: Callable[[int], bool],
    now: datetime,
    ttl_seconds: int,
) -> Classification:
    """The design's decision table; reads only, writes nothing."""
    stamp = host_boot.read_json(_stamp_path(key, resume_root))
    raw_end = read_end(key, resume_root=resume_root)
    end = _current_end(raw_end, stamp)
    if end is not None:
        evidence: dict[str, Any] = {
            "reason": end.get("reason"),
            "ended_at": end.get("ended_at"),
        }
        launcher = read_launcher_exit(key, resume_root=resume_root)
        if launcher is not None:
            implied = _launcher_implies(launcher)
            evidence["launcher_exit"] = launcher
            if implied != "manual_exit":
                evidence.update(
                    end_record=end, launcher_implies=implied, preferred="end_record"
                )
        return Classification("manual_exit", evidence)
    if stamp is None:
        return Classification("fresh_launch", {})
    evidence = {}
    if raw_end is not None and raw_end.get("reason") in EXIT_REASONS:
        evidence["stale_end_record"] = raw_end
    # Row 6 before row 5: an expired stamp is never live evidence, because its pid may
    # since have been reused by an unrelated process that would read as still running.
    written = _epoch(stamp.get("written_at"))
    age = int(now.timestamp()) - written if written is not None else None
    if age is None or age > ttl_seconds:
        return Classification(
            "unknown", {**evidence, "stamp_age_seconds": age, "expired": True}
        )
    pid = stamp.get("pid")
    alive = isinstance(pid, int) and pid > 0 and pid_alive(pid)
    same_boot = stamp.get("boot_id") == boot.boot_id
    if same_boot and alive:
        return Classification(
            "still_running",
            {
                **evidence,
                "pid": pid,
                "pane_id": stamp.get("pane_id"),
                "pid_alive": True,
            },
        )
    if not same_boot:
        return Classification(
            "host_reboot",
            {
                **evidence,
                "previous_boot_id": stamp.get("boot_id"),
                "boot_id": boot.boot_id,
                "downtime_seconds": max(0, boot.boot_time - written),  # type: ignore[operator]
            },
        )
    detail = {
        **evidence,
        "pane_id": stamp.get("pane_id"),
        "tty": stamp.get("tty"),
        "pid": pid,
        "pid_alive": False,
    }
    if _pane_gone(stamp, live_pane_ids):
        return Classification("terminal_quit_or_crash", detail)
    return Classification("unknown", {**detail, "pane_alive": True})


def record_end(  # noqa: PLR0913 - the binding interface, one keyword per record field
    key: str,
    *,
    resume_root: Path,
    reason: str,
    boot_id: str,
    pane_id: str | None,
    pid: int | None,
    session_id: str | None,
    now: datetime,
) -> Path | None:
    """Write the end record and mark the stamp ended; ``clear``/``resume`` are not exits."""
    if reason in NOT_EXITS:
        return None
    if reason not in EXIT_REASONS:
        reason = "other"
    ended_at = host_boot.iso(now)
    path = _launch(resume_root) / f"{key}.end.json"
    host_boot.write_json(
        path,
        {
            "key": key,
            "ended_at": ended_at,
            "reason": reason,
            "boot_id": boot_id,
            "pane_id": pane_id,
            "pid": pid,
            "session_id": session_id,
        },
    )
    stamp_path = _stamp_path(key, resume_root)
    stamp = host_boot.read_json(stamp_path)
    if stamp is not None and not stamp.get("ended_at"):
        host_boot.write_json(stamp_path, {**stamp, "ended_at": ended_at})
    return path


# --- CLI ------------------------------------------------------------------------------


def _claude_ancestor(start: int) -> int:
    """The nearest ``claude`` process at or above ``start``, else ``start`` itself."""
    from ai_chassis import reboot_resume

    source = reboot_resume.process_source()
    pid: int | None = start
    seen: set[int] = set()
    while pid is not None and pid > 1 and pid not in seen:
        seen.add(pid)
        if source.is_claude(pid):
            return pid
        pid = source.parent(pid)
    return start


def _live_pane_ids() -> frozenset[str]:
    tmux = os.environ.get("AIH_TMUX_COMMAND") or "tmux"
    try:
        result = subprocess.run(
            [tmux, "list-panes", "-a", "-F", "#{pane_id}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    return frozenset(
        line.strip() for line in result.stdout.splitlines() if line.strip()
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _not_pane_owner(pane: str, hook_parent: int) -> tuple[int, int] | None:
    """``(pane's claude, hook's claude)`` when both resolve and differ, else None.

    ISSUE-REF: a nested or headless ``claude`` inherits the pane's key and ``TMUX_PANE``, so
    its SessionEnd lands here under the parent's key. This is the ISSUE-REF comparison the
    SessionStart hook makes; an unreadable tree or pane is unknown, which records as before."""
    if not pane or not hook_parent:
        return None
    from ai_chassis import launch_resume  # noqa: PLC0415 - launch_resume imports this module

    try:
        pane_claude = launch_resume._session_pid(pane)
    except Exception:  # noqa: BLE001 - an unreadable pane is "unknown", never a crash
        return None
    own = launch_resume._owner_claude(hook_parent)
    if pane_claude is None or own is None or own == pane_claude:
        return None
    return pane_claude, own


def _record_end_command(args: argparse.Namespace, key: str) -> int:
    foreign = _not_pane_owner(args.pane, args.pid)
    if foreign is not None:
        host_boot.log(
            EXIT_RECORDED_STAGE,
            key,
            f"reason={args.reason} written=no skip=not-pane-owner "
            f"pane_pid={foreign[0]} own_pid={foreign[1]}",
        )
        return NOT_PANE_OWNER_EXIT
    try:
        boot_id = host_boot.identity().boot_id
    except BootIdentityUnavailable:
        boot_id = ""
    pid = _claude_ancestor(args.pid) if args.pid else None
    path = record_end(
        key,
        resume_root=host_boot.default_resume_root(),
        reason=args.reason,
        boot_id=boot_id,
        pane_id=args.pane or None,
        pid=pid,
        session_id=args.session_id or None,
        now=host_boot.current_time(),
    )
    written = "yes" if path is not None else "no"
    cause = " cause=manual_exit" if path is not None else ""
    host_boot.log(
        EXIT_RECORDED_STAGE, key, f"reason={args.reason} written={written}{cause}"
    )
    return 0


def _classify_command(args: argparse.Namespace, key: str) -> int:
    from ai_chassis import context_supervisor as supervisor

    try:
        boot = host_boot.identity()
    except BootIdentityUnavailable:
        result = Classification("unknown", {"boot": "unavailable"})
    else:
        result = classify(
            key,
            resume_root=host_boot.default_resume_root(),
            boot=boot,
            live_pane_ids=_live_pane_ids(),
            pid_alive=_pid_alive,
            now=host_boot.current_time(),
            ttl_seconds=supervisor._launch_resume_stamp_ttl_seconds(),
        )
    host_boot.log(CLASSIFIED_STAGE, key, f"cause={result.cause}")
    if args.json:
        print(
            json.dumps(
                {"key": key, "cause": result.cause, "evidence": result.evidence},
                sort_keys=True,
            )
        )
    else:
        print(result.cause)
    return 0


def main(argv: list[str] | None = None) -> int:
    from ai_chassis.reboot_resume import sanitize_key

    parser = argparse.ArgumentParser(prog="python3 -m ai_chassis.exit_cause")
    commands = parser.add_subparsers(dest="command", required=True)
    end = commands.add_parser("record-end")
    end.add_argument("--key", required=True)
    end.add_argument("--reason", required=True)
    end.add_argument("--pane", default="")
    end.add_argument("--pid", type=int, default=0)
    end.add_argument("--session-id", default="")
    classify_parser = commands.add_parser("classify")
    classify_parser.add_argument("--key", required=True)
    classify_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    key = sanitize_key(args.key)
    if key is None:
        stage = (
            EXIT_RECORDED_STAGE if args.command == "record-end" else CLASSIFIED_STAGE
        )
        host_boot.log(stage, "unknown", "reason=no-key")
        return 0
    if args.command == "record-end":
        return _record_end_command(args, key)
    return _classify_command(args, key)


if __name__ == "__main__":
    raise SystemExit(main())
