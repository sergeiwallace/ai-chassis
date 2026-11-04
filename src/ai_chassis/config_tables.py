"""Read the harness's tracked TOML with the per-machine XDG overlay merged over it.

This is the layering every ``read_config_tables`` caller relies on, kept in a module
with no harness dependencies so a caller that needs one table does not import the
agent-mail notifier to get it.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[2]
    / "projects-wide-session-config"
    / "ai-harness.toml"
)


def overlay_config_path(*, env: Mapping[str, str] | None = None) -> Path:
    """The per-machine XDG overlay, ``$XDG_CONFIG_HOME/ai-harness/ai-harness.local.toml``
    (``~/.config`` when unset). The one place its location is decided."""
    environment = os.environ if env is None else env
    xdg_root = (environment.get("XDG_CONFIG_HOME") or "").strip()
    overlay_base = Path(xdg_root) if xdg_root else Path.home() / ".config"
    return overlay_base / "ai-harness" / "ai-harness.local.toml"


def read_layered_tables(
    tracked_path: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    after_tracked: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Read tracked TOML and deep-merge the XDG overlay when present.

    An absent tracked file returns ``{}``. A malformed overlay raises rather than falls back,
    because a typo would silently disable the rung -- and the operator can fix the file.
    ``after_tracked`` runs on the tracked tables before the overlay is merged, so a value it
    adds sits between the fleet-wide defaults and the local overlay.

    Returns the merged tables with ``_config_source`` naming the layers in force, mirroring
    the ``message_transports.py`` ladder loader.
    """
    environment = os.environ if env is None else env
    overlay_path = overlay_config_path(env=environment)

    # Load tracked config or return empty. A missing file falls back; a file that exists
    # but cannot be read or parsed fails closed (the operator edited it).
    tables: dict[str, Any] = {}
    config_source = "default"
    tracked_path = tracked_path or DEFAULT_CONFIG_PATH
    if tracked_path.exists():
        try:
            tables = tomllib.loads(tracked_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise RuntimeError(
                f"config file {tracked_path} could not be read: {exc}"
            ) from exc
        config_source = str(tracked_path)

    if after_tracked is not None:
        after_tracked(tables)

    # Apply local overlay if present
    if overlay_path.exists():
        try:
            overlay = tomllib.loads(overlay_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise RuntimeError(
                f"overlay file {overlay_path} could not be read: {exc}"
            ) from exc
        # Deep merge: each top-level table in the overlay updates the tracked table
        for table_name, overlay_table in overlay.items():
            if isinstance(overlay_table, dict):
                tables.setdefault(table_name, {})
                if isinstance(tables[table_name], dict):
                    deep_merge_table(tables[table_name], overlay_table)
        config_source = f"{config_source} + overlay:{overlay_path}"

    tables["_config_source"] = config_source
    return tables


def deep_merge_table(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    """Deep merge ``overlay`` into ``base``, one level for nested tables."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge_table(base[key], value)
        else:
            base[key] = value
