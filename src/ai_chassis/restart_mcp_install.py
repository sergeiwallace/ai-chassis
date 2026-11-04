"""Machine-install entry points for the restart-gate MCP allowlist."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from platformdirs import user_state_path

from . import restart_mcp as mcp
from .machine_registry import machine_tables


def supported(tables: dict, machine: str | None) -> bool:
    """No step is declared on Windows, including Windows profiles on Unix hosts."""
    profile = machine_tables(tables).get(machine or "", {})
    return sys.platform in {"darwin", "linux"} and profile.get("platform") in {
        "darwin",
        "linux",
    }


def run(
    action: str, tables: dict, machine: str | None, cwd: Path
) -> tuple[list[str], bool]:
    """Plan and doctor never launch a process or change the allowlist."""
    if not supported(tables, machine):
        return [], True
    home = Path.home()
    target = mcp.allowlist_path(home, os.environ)
    state = Path(
        tables.get("install", {}).get("state_root") or user_state_path("ai-harness")
    )
    if not state.is_absolute():
        state = cwd / "projects-wide-session-config" / state
    core_state = state / "core-mcp"
    try:
        if action == "apply":
            _, messages = mcp.generate(home, cwd, target, core_state)
            return messages, True
        messages, healthy = mcp.check(target, mcp.discover(home, cwd, core_state))
    except (mcp.Unverified, OSError):
        return [
            "restart-mcp: configuration or allowlist unreadable; generation refused"
        ], False
    if action == "plan":
        messages.append(f"restart-mcp plan: {int(not healthy)} intended mutations")
    return messages, healthy
