"""Configuration-root and projects-root discovery."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from platformdirs import user_config_path

if TYPE_CHECKING:
    from .config import LoadedConfig


class ConfigRootNotFoundError(FileNotFoundError):
    """The resolved configuration root does not exist."""


def config_root(environ: dict[str, str] | None = None) -> Path:
    """Resolve the single supported configuration root without validating it."""
    environment = os.environ if environ is None else environ
    override = environment.get("AI_HARNESS_CONFIG_HOME")
    if override:
        return Path(override).expanduser()
    return Path(user_config_path("ai-harness"))


def discover_config_root(environ: dict[str, str] | None = None) -> Path:
    """Return the configured root or raise an actionable error."""
    root = config_root(environ)
    if not root.is_dir():
        raise ConfigRootNotFoundError(
            f"ai-harness config root does not exist: {root}. "
            "Run 'ai-harness init' to create it."
        )
    # Other tools write under the default root (buzz keys, agent-mail state), so an existing
    # directory is not an initialised one; without this the caller dies on a bare errno.
    if not any((root / name).is_file() for name in ("ai-harness.toml", "config.toml")):
        raise ConfigRootNotFoundError(
            f"ai-harness config root {root} holds no ai-harness.toml or config.toml. "
            "Run 'ai-harness init' to create one, or pass --config <dir> "
            "(install.sh passes <repo>/projects-wide-session-config)."
        )
    return root


def resolve_projects_root(
    flag: Path | None,
    config: LoadedConfig | None,
    environ: Mapping[str, str],
    home: Path,
) -> Path:
    """The projects root, by install.sh ``_resolve_projects_root``'s precedence.

    ``--projects-root`` > ``site.context_install_dir`` > ``$PROJECTS_DIR`` > ``~/projects`` if it is
    a directory > ``~/Projects`` if it is a directory > ``~/projects``. An existing directory is
    canonicalised to its physical path (the shell's ``cd && pwd -P``); a not-yet-created one is
    returned as given, for the context-dir step to create.

    It lives here, not in cli.py, because the agent-mail notifier resolves it at run time
    (wake/handlers/beads.py): importing cli.py would put the whole installer in the notifier's
    restart fingerprint, so any installer edit would restart it.
    """
    if flag is not None:
        root = flag
    elif config is not None and config.context_install_dir is not None:
        root = config.context_install_dir
    elif environ.get("PROJECTS_DIR"):
        root = Path(environ["PROJECTS_DIR"])
    elif (home / "projects").is_dir():
        root = home / "projects"
    elif (home / "Projects").is_dir():
        root = home / "Projects"
    else:
        root = home / "projects"
    return root.resolve() if root.is_dir() else root
