"""The ``session-reload`` adapter: projects ``session-reload-status/v1`` (reload design).

The only source of reload entries; the attempts adapter skips the ``session-reload`` use case.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_chassis.indicators.model import AdapterContext, AdapterResult, Entry, State
from ai_chassis.indicators.reasons import blocked_on

SCHEMA = "session-reload-status/v1"
_STATES: dict[str, State] = {
    "armed": "armed",
    "waiting-busy": "waiting",
    "waiting-draft": "waiting",
    "waiting-question": "waiting",
    "waiting-boundary": "waiting",
    "fired": "fired",
    "verified": "verified",
    "held-cache": "held",
    "gave-up": "gave-up",
}
_NOTHING_TO_DO = frozenset({"current", "not-loaded"})
_TARGET_FIELDS = ("installed", "last_refresh_at", "last_refresh_result")


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _read(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """The v1 record, or the source error the contract reports for it."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "absent"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, f"unreadable:{type(error).__name__}"
    schema = record.get("schema") if isinstance(record, dict) else None
    if schema != SCHEMA:
        return None, f"schema:{schema}"
    return record, None


def project(context: AdapterContext) -> AdapterResult:
    result = AdapterResult()
    record, error = _read(context.state_home / "session-reload" / "status.json")
    if record is None:
        result.status = {"ok": False, "error": error}
        return result

    for name, target in (record.get("targets") or {}).items():
        if isinstance(target, dict):
            result.targets[name] = {
                key: target[key]
                for key in _TARGET_FIELDS
                if target.get(key) is not None
            }
    unmapped = unknown = 0
    for session_id, session in (record.get("sessions") or {}).items():
        if not isinstance(session, dict):
            continue
        if session_id not in context.sessions:
            unmapped += 1
            continue
        for target_name, row in (session.get("targets") or {}).items():
            if not isinstance(row, dict) or row.get("state") in _NOTHING_TO_DO:
                continue
            entry = _entry(target_name, row, context)
            if entry is None:
                unknown += 1
            else:
                result.add(session_id, entry)
    result.status = {"ok": True, "unmapped": unmapped, "unknown_state": unknown}
    return result


def _entry(target: str, row: dict[str, Any], context: AdapterContext) -> Entry | None:
    raw = row.get("state")
    state = _STATES.get(raw) if isinstance(raw, str) else None
    if state is None:
        return None
    reason = row.get("reason") if isinstance(row.get("reason"), str) else None
    blocked = blocked_on(raw) if state == "waiting" else blocked_on(reason)
    command = row.get("command")
    detail = {
        key: row[key] for key in ("loaded", "wanted") if isinstance(row.get(key), str)
    }
    return Entry(
        id=f"reload:{target}",
        kind="reload",
        mechanism="session-reload",
        label=f"reload {target}",
        command=command if isinstance(command, str) else "/reload-plugins",
        state=state,
        blocked_on=blocked,
        reason=reason,
        since=_time(row.get("since")) or context.now,
        next_check_at=_time(row.get("next_check_at")),
        fires_at=None,
        detail=detail,
    )
