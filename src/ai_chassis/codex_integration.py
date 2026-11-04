"""Wire the checkout's Codex profiles and global instructions into ``~/.codex`` (ISSUE-REF batch 7).

install.sh's retired Step 5c (Codex CLI integration, ISSUE-REF). The role profiles and the lean
global ``AGENTS.md`` are linked from the checkout's ``codex/`` directory; ``local-routing.toml``
is copied once and is the user's from then on, because ``cx`` re-reads it on every local-model
dispatch; and two blocks of keys are prepended to the user-owned ``~/.codex/config.toml`` when it
lacks them, never touching the rest of that file (``mcp_servers`` and the like). The one-shot
research-write profile migration the shell step also ran is the private orchestrator's, ahead
of this step (IN-08: the public core ships no one-shot migrations).

Failure semantics are the shell's: an absent ``codex/`` source, an absent profile glob entry and
an absent Codex binary are silent skips; a ``chmod`` is best effort and its failure ignored;
directory creation, links, the config write and its rename fail the step. The ``npm prefix -g``
probe and the upgrade hint it prints name this fleet's install layout and an install command, so
they are the private orchestrator's `codex-prefix-check` step, which runs next (D-C15).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .links import LinkError
from .shell_startup import publish_link

FALLBACK_MARKER = "project_doc_fallback_filenames"
FALLBACK_BLOCK = """\
# ai-harness (ISSUE-REF): let Codex fall back to a repo's CLAUDE.md when no AGENTS.md is
# present, and allow a slightly larger combined instruction chain.
project_doc_fallback_filenames = ["CLAUDE.md"]
project_doc_max_bytes = 65536

"""
PRICE_GUARD_MARKER = "model_auto_compact_token_limit"
PRICE_GUARD_BLOCK = """\
# Fleet-wide normal-call price guard. Keep model_context_window unset so model metadata
# supplies the context window; long-context exceptions are paired invocation overrides.
model_auto_compact_token_limit = 230000
model_auto_compact_token_limit_scope = "total"

"""


def _chmod(path: Path, mode: int) -> None:
    """``chmod <mode> <path> 2>/dev/null || true``: best effort, a failure ignored."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _make_dir(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LinkError(f"could not create {directory}: {exc}") from exc


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def _replace_config(config: Path, content: str) -> None:
    """Write ``config`` through a temporary file in its own directory and an atomic rename."""
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=config.parent, prefix=f".{config.name}."
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
            os.replace(temporary, config)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise LinkError(f"could not write {config}: {exc}") from exc


def ensure_config_keys(config: Path) -> None:
    """Prepend each key block the user-owned ``config.toml`` lacks, in the shell's order.

    The price-guard block is prepended after the fallback block, so it lands above it. The file is
    written once, and only when a block is missing, so a converged file keeps its inode and mtime.
    """
    try:
        if not config.exists():
            config.touch()
        existing = config.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LinkError(f"could not read {config}: {exc}") from exc
    content = existing
    lines = []
    for marker, block, added, present in (
        (
            FALLBACK_MARKER,
            FALLBACK_BLOCK,
            "prepended CLAUDE.md-fallback keys",
            "fallback keys already present",
        ),
        (
            PRICE_GUARD_MARKER,
            PRICE_GUARD_BLOCK,
            "prepended auto-compact price-guard keys",
            "auto-compact price-guard keys already present",
        ),
    ):
        if marker in content:
            lines.append(f"  [ok]  ~/.codex/config.toml — {present}")
        else:
            content = block + content
            lines.append(f"  [ok]  ~/.codex/config.toml — {added}")
    if content != existing:
        _replace_config(config, content)
        _chmod(config, 0o600)
    for line in lines:
        print(line)


def publish_persistent_codex(
    projects_root: Path, home: Path, environ: Mapping[str, str]
) -> None:
    """Link a Codex binary kept on persistent storage into the on-PATH ``~/.claude/bin``.

    ``cx`` invokes ``codex`` by bare name. Where the binary lives on persistent storage beside the
    projects root while that directory is absent from an agent shell's PATH, ``codex --version``
    failed and read as a missing install (ISSUE-REF, ISSUE-REF). The link is re-made every run because
    ``$HOME`` can be ephemeral while its target is not. A machine without that binary is a silent
    no-op.
    """
    binary = projects_root.parent / "tools" / "npm-global" / "bin" / "codex"
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        return
    if not publish_link(binary, home / ".claude" / "bin" / "codex", environ):
        return
    print(f"  [ok]  ~/.claude/bin/codex → {binary} (on-PATH; survives a space rebuild)")


def install(
    checkout: Path, home: Path, projects_root: Path, environ: Mapping[str, str]
) -> None:
    """``<checkout>/codex`` into ``~/.codex``, as install.sh's ``$SCRIPT_DIR/codex``."""
    source = checkout / "codex"
    if not source.is_dir():
        print(
            "  [skip] no codex/ directory in ai-harness (Codex integration not set up)"
        )
        return
    codex_dir = home / ".codex"
    _make_dir(codex_dir)
    publish_link(source / "AGENTS.global.md", codex_dir / "AGENTS.md", environ)
    for profile in sorted(source.glob("*.config.toml")):
        if profile.is_file():
            publish_link(profile, codex_dir / profile.name, environ)
    routing = codex_dir / "local-routing.toml"
    if routing.is_symlink() and not routing.exists():
        # The shell's `cp` refused to write through a dangling destination link and failed the
        # step. Following it would create or overwrite a file outside ~/.codex; the link is the
        # user's, so it is neither followed nor removed.
        _warn(
            f"  [FAIL] {routing} is a dangling symlink (to {os.readlink(routing)}); "
            "not writing the local-model routing policy through it"
        )
        raise LinkError(f"refusing to write through dangling symlink {routing}")
    if not routing.exists():
        try:
            shutil.copyfile(source / "local-routing.toml", routing)
        except OSError as exc:
            raise LinkError(f"could not install {routing}: {exc}") from exc
        _chmod(routing, 0o600)
        print(
            "  [ok]  ~/.codex/local-routing.toml — installed local-model routing policy"
        )
    else:
        print(
            "  [ok]  ~/.codex/local-routing.toml — preserving user-managed local-model "
            "routing policy"
        )
    publish_link(
        source / "coordinator-pre-spawn-hook.sh",
        codex_dir / "coordinator-pre-spawn-hook.sh",
        environ,
    )
    publish_link(source / "hooks.json", codex_dir / "hooks.json", environ)
    for script in ("coordinator-pre-spawn-hook.sh", "coordinator-watchdog.sh"):
        _chmod(source / script, 0o755)
    ensure_config_keys(codex_dir / "config.toml")
    # Rollout hygiene (ISSUE-REF): Codex writes session rollouts at 0644 under 0755 directories and
    # a rollout retains every tool's stdout verbatim. An owner-only parent denies traversal during
    # the run, whatever the file's own mode; it does not fix retention.
    _chmod(codex_dir, 0o700)
    if (codex_dir / "sessions").is_dir():
        _chmod(codex_dir / "sessions", 0o700)
    print(
        "  [ok]  ~/.codex/ mode 700 (rollouts stay owner-only during a run, not just after — "
        "ISSUE-REF)"
    )
    publish_persistent_codex(projects_root, home, environ)
    print(
        "  [ok]  ~/.codex/ ← ai-harness/codex/ (AGENTS.md + "
        "implement/implement-network/review/research/mechanical profiles)"
    )
