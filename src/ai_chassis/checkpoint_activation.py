"""Installer activation state for the trusted aido refresh, using the install envelope."""

from __future__ import annotations

import json
import os
from pathlib import Path

from platformdirs import user_state_path

from .generations import write_json_fsync

RECORD = "checkpoint-authority-activation.json"


def write_activation(
    passed: bool,
    reason: str,
    *,
    capability: str = "checkpoint-authority",
    failing: tuple[str, ...] = ("trusted-aido",),
    declared: bool = True,
) -> None:
    """Record this install attempt separately from the last trusted generation."""
    write_json_fsync(
        Path(user_state_path("ai-harness")) / f"{capability}-activation.json",
        {
            "class": capability,
            "status": ("pass" if passed else "fail") if declared else "not-declared",
            "machine": os.environ.get("AI_HOST", "<unidentified>"),
            "declared": declared,
            "failing": [] if passed else list(failing),
            "reason": reason,
        },
    )


def doctor_activation(
    state_root: Path, *, capability: str = "checkpoint-authority"
) -> tuple[list[str], bool]:
    """Read the last attempt, including a failed refresh that kept a valid old record."""
    path = state_root / f"{capability}-activation.json"
    if not path.exists():
        return [], True
    try:
        record = json.loads(path.read_text())
        healthy = record["status"] in {"pass", "not-declared"}
        reason = record["reason"]
    except (OSError, ValueError, KeyError, TypeError):
        healthy, reason = False, f"invalid activation record {path}"
    return [f"{capability}: status={'pass' if healthy else 'fail'} {reason}"], healthy
