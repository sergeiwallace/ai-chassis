"""The ``sessions`` adapter: the live interactive sessions every other adapter maps onto.

Reads ``~/.claude/sessions/*.json`` through ``claude_sessions._validated_records``, the
registry reader the goal waker already uses, so a record counts only when it provably
describes a live process (its pid and its kernel start time agree).
"""

from __future__ import annotations

from typing import Any

from ai_chassis.indicators.model import LiveSession


def discover() -> tuple[dict[str, LiveSession], dict[str, Any]]:
    from ai_chassis import goal_waker  # noqa: PLC0415 - the waker imports this package

    inventory = goal_waker._script("claude_sessions")
    records = inventory._validated_records(inventory._proc_root(None), None)
    live: dict[str, LiveSession] = {}
    for pid, record in records.items():
        session_id = record.get("sessionId")
        if record.get("kind") != "interactive" or not isinstance(session_id, str):
            continue
        name = record.get("name")
        cwd = record.get("cwd")
        live[session_id] = LiveSession(
            session_id,
            name if isinstance(name, str) else "",
            pid,
            cwd if isinstance(cwd, str) else "",
        )
    return live, {"ok": True, "live": len(live)}
