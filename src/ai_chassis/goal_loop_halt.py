"""Session-scoped Stop suppression after a threshold-qualified no-pane clear failure.

HALTED deliberately leaves the native goal armed. Only a real human prompt removes
the marker; notifications, evaluator feedback and peer messages leave it in place.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


def marker_path(session_id: str) -> Path:
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", session_id)
        or ".." in session_id
    ):
        raise ValueError("invalid session id")
    return Path.home() / ".claude" / "state" / session_id / "goal-loop-halted.json"


def is_halted(session_id: str) -> bool:
    try:
        record = json.loads(marker_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(record, dict)
        and record.get("state") == "HALTED"
        and record.get("session_id") == session_id
    )


def record_halt(session_id: str, **fields: Any) -> None:
    path = marker_path(session_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".goal-loop-halted-",
        delete=False,
    ) as stream:
        json.dump({**fields, "session_id": session_id, "state": "HALTED"}, stream)
        stream.write("\n")
    os.replace(stream.name, path)


def hook_output(payload: dict[str, Any]) -> dict[str, Any] | None:
    if {"agent_id", "agent_type"} & payload.keys():
        return None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not is_halted(session_id):
        return None
    event = payload.get("hook_event_name")
    if event == "Stop":
        return {
            "continue": False,
            "stopReason": "HALTED: evaluator rejection threshold reached without a tmux pane; native goal remains armed until /goal clear. A human prompt releases this halt.",
        }
    if event == "UserPromptSubmit":
        from ai_chassis.context_supervisor import (
            _prompt_origin,
            _scheduled_wakeup_evidence,
        )  # noqa: PLC0415 - only a halted session needs prompt classification

        prompt = payload.get("prompt")
        if (
            isinstance(prompt, str)
            and prompt.strip()
            and not prompt.lstrip().startswith(
                ("<task-notification>", "<system-reminder>")
            )
            and _prompt_origin(prompt) == "human"
            and not payload.get("isMeta")
            and payload.get("promptSource") != "system"
            and _scheduled_wakeup_evidence(prompt, payload.get("transcript_path"))
            is None
        ):
            # The exact session's marker has been read and validated above.
            marker_path(session_id).unlink(missing_ok=True)
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        output = hook_output(payload) if isinstance(payload, dict) else None
        if output is not None:
            print(json.dumps(output))
    except (OSError, ValueError):
        pass  # Malformed input/state must not halt an unrelated session.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
