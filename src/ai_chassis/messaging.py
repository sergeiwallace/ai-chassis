"""Agent-mail data placement: the one owner of ``AGENT_MAIL_DATA_ROOT`` (ISSUE-REF Phase 2, IC-2).

``docker compose`` reads the agent-mail stack's data root from ``AGENT_MAIL_DATA_ROOT`` in its
``compose.env``; every data volume of the stack is a bind mount under that root (``fleet``, ``example-client-host``,
``gateway-audit``, ``promotion``). This module is the only code that writes the variable: the
installer's shell messaging step never does (T-2.1's static assertion pins it), and
``ai-harness apply`` reaches it through the storage step as a *placement extra* for the
``agent_mail_data`` placement. The merge preserves every other line of ``compose.env`` byte for
byte, because that file also carries the stack's tokens and is owned by the operator.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .storage import PlacementExtra

COMPOSE_ENV_KEY = "AGENT_MAIL_DATA_ROOT"
AGENT_MAIL_PLACEMENT = "agent_mail_data"
#: The server-owned subdirectories the compose file binds, in the order compose.yaml declares them.
AGENT_MAIL_SUBDIRS: tuple[str, ...] = ("fleet", "example-client-host", "gateway-audit", "promotion")
_COMPOSE_ENV_MODE = 0o600
_STATE_DIR_MODE = 0o700


def default_compose_env_path(home: Path | None = None) -> Path:
    """``~/.local/state/agent-mail/compose.env`` (OPERATIONS.md § State root)."""
    base = Path.home() if home is None else home
    return base / ".local" / "state" / "agent-mail" / "compose.env"


def render_compose_env(existing: str | None, root: Path) -> str:
    """Return ``existing`` with exactly one ``AGENT_MAIL_DATA_ROOT=<root>`` line.

    An existing assignment is replaced in place; otherwise the line is appended. Every other
    byte of ``existing`` is preserved, including comments, blank lines and the final newline
    state, so a token the operator wrote is never touched by this writer.
    """
    line = f"{COMPOSE_ENV_KEY}={root}"
    if not existing:
        return line + "\n"
    lines = existing.split("\n")
    trailing_newline = existing.endswith("\n")
    if trailing_newline:
        lines = lines[:-1]
    replaced = False
    for index, current in enumerate(lines):
        if current.split("=", 1)[0].strip() == COMPOSE_ENV_KEY:
            lines[index] = line
            replaced = True
    if not replaced:
        lines.append(line)
    return "\n".join(lines) + "\n"


def write_compose_env(path: Path, root: Path) -> bool:
    """Merge the key into ``path`` (0600, directory 0700); return whether the file changed."""
    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    rendered = render_compose_env(existing, root)
    if existing == rendered:
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=_STATE_DIR_MODE)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".compose.env.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, _COMPOSE_ENV_MODE)
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return True


def agent_mail_extra(compose_env: Path | None = None) -> PlacementExtra:
    """The ``agent_mail_data`` extra: four server-owned subdirectories, then the compose.env key."""
    target = compose_env if compose_env is not None else default_compose_env_path()

    def export(root: Path) -> list[str]:
        changed = write_compose_env(target, root)
        state = "written" if changed else "unchanged"
        return [
            f"storage apply {AGENT_MAIL_PLACEMENT}: {COMPOSE_ENV_KEY}={root} ({state}: {target})"
        ]

    return PlacementExtra(
        AGENT_MAIL_PLACEMENT,
        subdirs=AGENT_MAIL_SUBDIRS,
        export=export,
        missing_line=(
            f"storage apply {AGENT_MAIL_PLACEMENT}: not declared for this machine; "
            f"{COMPOSE_ENV_KEY} not written (declare the placement to bind the agent-mail data root)"
        ),
    )
