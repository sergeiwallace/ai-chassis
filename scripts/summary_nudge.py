#!/usr/bin/env python3
"""Fail-open sensor and Stop-hook nudge for long autonomous work batches."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

GENERAL_TOOL_THRESHOLD = 12
AUTONOMOUS_TOOL_THRESHOLD = 6
COUNTED_TOOLS = frozenset(
    {
        "Agent",
        "Bash",
        "Edit",
        "NotebookEdit",
        "Task",
        "TaskCreate",
        "TaskList",
        "TaskOutput",
        "TaskUpdate",
        "Write",
    }
)


def _safe_identifier(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or any(part in value for part in ("/", "\\", ".."))
    ):
        return None
    return value


def _payload() -> dict[str, Any] | None:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _state_path(payload: dict[str, Any]) -> Path | None:
    session_id = _safe_identifier(payload.get("session_id"))
    prompt_id = _safe_identifier(payload.get("prompt_id"))
    if session_id is None or prompt_id is None:
        return None
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    return config / "state" / "summary-nudge" / f"{session_id}-{prompt_id}.json"


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"tool_calls": 0, "autonomous_skill": False, "nudged": False}
    if not isinstance(value, dict):
        return {"tool_calls": 0, "autonomous_skill": False, "nudged": False}
    return {
        "tool_calls": value.get("tool_calls", 0)
        if isinstance(value.get("tool_calls"), int)
        else 0,
        "autonomous_skill": value.get("autonomous_skill") is True,
        "nudged": value.get("nudged") is True,
    }


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _is_autonomous_skill(payload: dict[str, Any]) -> bool:
    if payload.get("tool_name") != "Skill":
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    return any(
        tool_input.get(field) in {"auto-flow", "auto-start"}
        for field in ("skill", "name", "command")
    )


def record_tool(payload: dict[str, Any]) -> None:
    path = _state_path(payload)
    if path is None:
        return
    state = _read_state(path)
    if payload.get("tool_name") in COUNTED_TOOLS:
        state["tool_calls"] += 1
    state["autonomous_skill"] = state["autonomous_skill"] or _is_autonomous_skill(
        payload
    )
    _write_state(path, state)


def should_nudge(state: dict[str, Any]) -> bool:
    if state["nudged"]:
        return False
    if state["autonomous_skill"] and state["tool_calls"] >= AUTONOMOUS_TOOL_THRESHOLD:
        return True
    return state["tool_calls"] >= GENERAL_TOOL_THRESHOLD


def nudge_at_stop(payload: dict[str, Any]) -> None:
    if payload.get("stop_hook_active") is True:
        return
    path = _state_path(payload)
    if path is None:
        return
    state = _read_state(path)
    if not should_nudge(state):
        return
    state["nudged"] = True
    _write_state(path, state)
    print(
        "<system-reminder>\n"
        "A long autonomous work batch just completed. Invoke /summary now, without an "
        "argument, and produce its complete standalone retrospective before ending this batch. "
        "This is an advisory nudge, not a request to invent work or block the Stop event.\n"
        "</system-reminder>"
    )


def main(argv: list[str]) -> int:
    try:
        payload = _payload()
        if payload is None:
            return 0
        if argv == ["pre-tool"]:
            record_tool(payload)
        elif argv == ["stop"]:
            nudge_at_stop(payload)
    except Exception:  # noqa: BLE001 - hook boundaries must fail open
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
