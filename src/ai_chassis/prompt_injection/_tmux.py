"""The tmux calls every module shares. Not public.

The command prefix is injectable (``("tmux", "-S", socket)``) so tests drive an
isolated server; the default bare ``tmux`` reaches the server ``$TMUX`` names,
which is the calling session's own when the caller runs inside tmux.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence

from ai_chassis.tmux_compat import (
    TMUX_FINGERPRINT_FORMAT as TMUX_FINGERPRINT_FORMAT,
    fingerprint_format,
    send_keys_command as send_keys_command,
)

# Copied from context_supervisor (123-124, 172 at bff30e522); see MINIMUM_TMUX_VERSION
# there for why this is a floor on the SERVER and never an allowlist.
FENCE_MISMATCH = "__aih_context_fence_mismatch__"
MINIMUM_TMUX_VERSION = (3, 7)
# Copied from context_supervisor.PANE_WARNING_DISPLAY_MS; tmux's default is 750ms.
PANE_WARNING_DISPLAY_MS = 15000
_TMUX_VERSION_RE = re.compile(r"^(?:next-)?(\d+)\.(\d+)")

Command = tuple[str, ...]
DEFAULT_COMMAND: Command = ("tmux",)


def run(
    command: Command,
    args: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """Run one tmux call; ``None`` when the binary cannot be executed at all."""
    try:
        return subprocess.run(
            [*command, *args],
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def parse_version(version: str | None) -> tuple[int, int] | None:
    # Copied from context_supervisor._parse_tmux_version (725-731).
    if not version:
        return None
    match = _TMUX_VERSION_RE.match(version.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def version_allowed(version: str | None) -> bool:
    # Copied from context_supervisor._tmux_version_allowed (748-762): fails closed.
    parsed = parse_version(version)
    return parsed is not None and parsed >= MINIMUM_TMUX_VERSION


def server_version(command: Command) -> str | None:
    """The running SERVER's ``#{version}`` (ISSUE-REF), never the client's ``-V``."""
    result = run(command, ["display-message", "-p", "#{version}"])
    if result is None or result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value or not re.match(r"^(?:next-)?\d", value):
        return None
    return value


def display(command: Command, pane_id: str, fmt: str) -> str | None:
    result = run(command, ["display-message", "-p", "-t", pane_id, fmt])
    if result is None or result.returncode != 0:
        return None
    return result.stdout.rstrip("\n")


def fingerprint(command: Command, pane_id: str) -> str | None:
    """The pane's identity fingerprint, or None when it does not resolve.

    Copied from TmuxAdapter.fingerprint (5590-5634): a nonexistent target makes
    tmux exit 0 with every field empty (``'|||'``), so the empty first field
    (``#{session_id}``) is the refusal (ISSUE-REF).
    """
    value = display(command, pane_id, fingerprint_format(command))
    if value is None:
        return None
    value = value.strip()
    if not value or not value.split("|", 1)[0]:
        return None
    return value
