"""Install the canonical shared agent profiles into ``~/.claude/agents`` (ISSUE-REF batch 6).

install.sh's retired Step 5ba ("Symlink canonical shared agents") and its two helpers. The
profiles stay at the USER layer: a repository's own ``.claude/agents/`` is deliberately
untouched and keeps Claude Code's higher-precedence override.

Each profile is linked one file at a time through ``shell_startup.publish_link``, so Git Bash
gets its native-symlink, hardlink, copy fallback. A fallback hardlink or copy cannot be told
apart from a user's own file on the next run, so every profile linked is recorded in a manifest;
a profile the source no longer ships is removed only when it is a symlink into the source
directory or its name is in that manifest. A user-authored profile of any other name stays.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .links import LinkError
from .shell_startup import publish_link

MANIFEST_NAME = ".ai-harness-managed-agent-profiles"
_SUFFIX = ".md"


def is_managed_agent_link(link: Path, agents_src: Path) -> bool:
    """A symlink whose target's directory is, physically, the shared-agent source directory.

    The target itself need not exist: a link to a profile the source has since removed is the
    case pruning exists for. A target directory that cannot be resolved is not managed.
    """
    if not link.is_symlink():
        return False
    try:
        target = os.readlink(link)
    except OSError:
        return False
    if not os.path.isabs(target):
        target = os.path.join(os.path.realpath(link.parent), target)
    target_dir = os.path.dirname(target)
    if not os.path.isdir(target_dir) or not agents_src.is_dir():
        return False
    return os.path.realpath(target_dir) == os.path.realpath(agents_src)


def _recorded(manifest: Path) -> frozenset[str]:
    """The manifest's names; an absent or unreadable manifest records none, as ``grep`` did."""
    try:
        return frozenset(manifest.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return frozenset()


def _profiles(directory: Path) -> list[Path]:
    """``<directory>/*.md`` regular files in name order, without dot-names, as the shell glob."""
    try:
        entries = sorted(directory.iterdir())
    except OSError as exc:
        raise LinkError(f"could not list {directory}: {exc}") from exc
    return [
        entry
        for entry in entries
        if entry.name.endswith(_SUFFIX)
        and not entry.name.startswith(".")
        and entry.is_file()
    ]


def _replace_manifest(manifest: Path, content: str) -> None:
    """Write the manifest atomically, and only when its bytes change (ISSUE-REF).

    An unconditional rewrite gave it a new inode and mtime on every merge's install refresh.
    """
    try:
        if manifest.is_file() and manifest.read_bytes() == content.encode("utf-8"):
            return
    except OSError:
        pass
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=manifest.parent, prefix=f"{manifest.name}."
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
            os.replace(temporary, manifest)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise LinkError(f"could not write {manifest}: {exc}") from exc


def install_shared_agents(
    agents_src: Path, agents_dst: Path, manifest: Path, environ: Mapping[str, str]
) -> None:
    """Prune the profiles this installer placed and the source dropped, link each, record them."""
    try:
        agents_dst.mkdir(parents=True, exist_ok=True)
        existing = sorted(agents_dst.iterdir())
    except OSError as exc:
        raise LinkError(f"could not prepare {agents_dst}: {exc}") from exc
    recorded = _recorded(manifest)
    for entry in existing:
        if not entry.name.endswith(_SUFFIX) or entry.name.startswith("."):
            continue
        if (agents_src / entry.name).is_file():
            continue
        name = entry.name.removesuffix(_SUFFIX)
        if is_managed_agent_link(entry, agents_src) or name in recorded:
            try:
                entry.unlink()
            except OSError as exc:
                raise LinkError(f"could not remove {entry}: {exc}") from exc
    profiles = _profiles(agents_src)
    for profile in profiles:
        publish_link(profile, agents_dst / profile.name, environ)
    _replace_manifest(
        manifest,
        "".join(f"{profile.name.removesuffix(_SUFFIX)}\n" for profile in profiles),
    )


def install(config_root: Path, home: Path, environ: Mapping[str, str]) -> None:
    """``<config root>/agents/*.md`` into ``~/.claude/agents``, as install.sh's ``$SESSION_CFG``."""
    agents_src = config_root / "agents"
    if not agents_src.is_dir():
        print(f"  [skip] canonical shared-agent source not found: {agents_src}")
        return
    claude_dir = home / ".claude"
    install_shared_agents(
        agents_src, claude_dir / "agents", claude_dir / MANIFEST_NAME, environ
    )
    print("  [ok]  ~/.claude/agents/ ← projects-wide-session-config/agents/*.md")
