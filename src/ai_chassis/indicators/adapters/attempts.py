"""The ``attempts`` adapter: the prompt-injection attempt store, library and legacy alike.

Scans ``attempts/`` and ``wake-attempts/`` under the resume root by ``os.scandir`` mtime and
reads only the records modified inside ``attempt_window_seconds``. Legacy and library records
share one shape (library design § Observability); a legacy record carries no ``use_case``, so
its ``attempt_type`` names it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ai_chassis.indicators.model import (
    AdapterContext,
    AdapterResult,
    Entry,
    LiveSession,
    State,
    from_epoch,
)
from ai_chassis.indicators.reasons import WAIT_CLASSES, blocked_on

LEDGERS = ("attempts", "wake-attempts")
RELOAD_USE_CASE = "session-reload"
_LIVE_STATES: dict[str, State] = {
    "ARMED": "armed",
    "BOUND": "firing",
    "READY": "firing",
    "STAGED_UNVERIFIED": "firing",
    "SENT": "fired",
    "QUEUED": "queued",
    "HELD": "held",
}
_LABELS = {
    "compact": "auto-compact",
    "continuation": "post-compact continuation",
    "idle-nudge": "goal check-in nudge",
    "goal-loop-nudge": "goal loop nudge",
    "goal-clear": "goal clear",
    "inbox-nudge": "inbox nudge",
    "reauth-wake": "re-auth wake",
    "launch-resume": "launch resume",
    "wq-nudge": "queue waker",
    "start-goal": "start goal",
    "save-state": "save state",
    "session-restart": "session restart",
}
_COMMANDS = {"compact": "/compact"}


def read_record(path: Path) -> Any:
    """One record body. A module function so a test can count the reads."""
    return json.loads(path.read_text(encoding="utf-8"))


def _recent(directory: Path, floor: float) -> list[Path]:
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    recent = []
    for entry in entries:
        if not entry.name.endswith(".json") or entry.name.startswith("."):
            continue
        try:
            if entry.stat().st_mtime >= floor:
                recent.append(Path(entry.path))
        except OSError:
            continue
    return recent


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _session_for(record: dict[str, Any], context: AdapterContext) -> LiveSession | None:
    """Transcript file name, then ``session_name``, then ``key`` (design § Sources)."""
    transcript = record.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        session = context.sessions.get(Path(transcript).stem)
        if session is not None:
            return session
    for field in ("session_name", "key"):
        value = record.get(field)
        session = context.session_named(value if isinstance(value, str) else None)
        if session is not None:
            return session
    return None


def project(context: AdapterContext) -> AdapterResult:
    floor = context.now.timestamp() - context.config.attempt_window_seconds
    newest: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
    scanned = unmapped = unreadable = 0
    for ledger in LEDGERS:
        for path in _recent(context.resume_root / ledger, floor):
            scanned += 1
            try:
                record = read_record(path)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                unreadable += 1
                continue
            if not isinstance(record, dict):
                unreadable += 1
                continue
            use_case = record.get("use_case") or record.get("attempt_type")
            if not isinstance(use_case, str) or use_case == RELOAD_USE_CASE:
                continue
            session = _session_for(record, context)
            if session is None:
                unmapped += 1
                continue
            armed_at = _number(record.get("armed_at"))
            pair = (session.session_id, use_case)
            if pair not in newest or armed_at >= newest[pair][0]:
                newest[pair] = (armed_at, record)

    result = AdapterResult()
    retrying = set(context.config.retrying_use_cases)
    for (session_id, use_case), (_, record) in sorted(newest.items()):
        entry = _entry(use_case, record, context, retries=use_case in retrying)
        if entry is not None:
            result.add(session_id, entry)
    result.status = {
        "ok": True,
        "scanned": scanned,
        "unmapped": unmapped,
        "unreadable": unreadable,
        "config_dropped": list(context.config.dropped),
    }
    return result


def _command(use_case: str, record: dict[str, Any]) -> str:
    command = record.get("command")
    if isinstance(command, str) and command.startswith("/"):
        return command.split()[0]
    return _COMMANDS.get(use_case, "prompt")


def _entry(
    use_case: str, record: dict[str, Any], context: AdapterContext, *, retries: bool
) -> Entry | None:
    raw = record.get("state")
    reason = record.get("reason") if isinstance(record.get("reason"), str) else None
    retry_at = _number(record.get("retry_not_before"))
    next_check = from_epoch(retry_at) if retry_at > context.now.timestamp() else None
    if raw == "HELD":
        blocked = "boundary"
    elif next_check is not None:
        blocked = "backoff"
    else:
        blocked = blocked_on(reason)

    since_epoch = _number(record.get("armed_at"))
    if raw == "REFUSED":
        state: State = "waiting" if retries and blocked in WAIT_CLASSES else "refused"
        since_epoch = _number(record.get("terminal_at")) or since_epoch
    elif isinstance(raw, str) and raw in _LIVE_STATES:
        state = _LIVE_STATES[raw]
        if state == "armed" and blocked is not None:
            state = "waiting"
    else:
        return None
    detail = {"attempt_id": str(record.get("attempt_id") or ""), "raw_state": raw}
    if isinstance(record.get("key"), str):
        detail["key"] = record["key"]
    return Entry(
        id=f"injection:{use_case}",
        kind="injection",
        mechanism=use_case,
        label=_LABELS.get(use_case, use_case.replace("-", " ")),
        command=_command(use_case, record),
        state=state,
        blocked_on=blocked,
        reason=reason,
        since=from_epoch(since_epoch),
        next_check_at=next_check,
        fires_at=None,
        detail=detail,
    )
