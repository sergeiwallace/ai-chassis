"""Read Claude Code's own view of its background sessions (`claude agents --json`).

Kept apart from the dead-session watchdog so a caller that only needs this one read
does not have to carry the watchdog and everything it imports.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

# Job states Claude Code's own `claude agents` treats as over. Any other state, with no
# `lastTerminalAt`, is a record that says its worker is still running.
TERMINAL_JOB_STATES = frozenset({"stopped", "failed", "killed", "exited", "finished"})


def call_claude_agents(
    all_flag: bool = False,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Call claude agents --json [--all]. Returns (sessions_list, error)."""
    try:
        cmd = ["claude", "agents", "--json"]
        if all_flag:
            cmd.append("--all")
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=10
        )
        sessions = json.loads(result.stdout)
        if not isinstance(sessions, list):
            return None, "claude agents returned non-list"
        return sessions, None
    except FileNotFoundError:
        return None, "claude CLI not found"
    except subprocess.CalledProcessError as e:
        return None, f"claude agents failed: {e}"
    except subprocess.TimeoutExpired:
        return None, "claude agents timed out"
    except json.JSONDecodeError as e:
        return None, f"claude agents output not valid JSON: {e}"
