"""Deployment-faithful ask-fidelity support for isolated ``cx`` fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TIER0_REASON = "isolated cx mechanism fixture with no originating human ask"


def provision_ask_fidelity_gate(root: Path) -> None:
    """Copy the live gate and its validator into an isolated harness root."""
    copies = {
        REPO / "scripts" / "ask_fidelity_launch_gate.py": (
            root / "scripts" / "ask_fidelity_launch_gate.py"
        ),
        REPO / "scripts" / "bd_git_safety.py": (root / "scripts" / "bd_git_safety.py"),
        REPO / "skills" / "_shared" / "ask-fidelity" / "ask_fidelity.py": (
            root / "skills" / "_shared" / "ask-fidelity" / "ask_fidelity.py"
        ),
    }
    for source, destination in copies.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def add_tier0_carrier(env: dict[str, str]) -> dict[str, str]:
    """Declare that an isolated mechanism test has no originating human ask."""
    env["CX_FIDELITY_TIER0_REASON"] = TIER0_REASON
    return env


def stage_framing(prompt: str) -> str:
    """Return the caller-owned prompt inside a validated Tier-0 carrier."""
    stage_marker = "## STAGE FRAMING\n\n"
    alignment_marker = "\n\n## ALIGNMENT OBLIGATION"
    assert stage_marker in prompt, "fixture prompt is missing its Tier-0 carrier"
    stage = prompt.split(stage_marker, 1)[1]
    assert alignment_marker in stage, "fixture prompt is missing its alignment block"
    return stage.split(alignment_marker, 1)[0]
