"""Link the checkout's hook files into ``~/.claude/hooks`` (ISSUE-REF batch C-B1, IN-32).

install.sh's retired Step 7. Every hook FILE is made available, not only ``*.sh`` (ISSUE-REF);
WHICH ones are wired, and on which machine, is the hooks manifest's business (Step 7b), so an
unwired file never runs. On POSIX each file is a per-file link into the MAIN working tree's
``.claude/hooks`` (D-11), through ``shell_startup.publish_link``; under Git Bash the whole
directory is one junction, queued into the run's single batched ``cmd.exe`` launch. Only a script
gets the execute bit (ISSUE-REF): a ``.sh``/``.py`` file or one opening with ``#!``, so a prompt
template tracked at 100644 is never flipped to 100755 in the read-only main tree.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from .links import LinkError
from .profile import _is_windows_shell, queue_windows_junction
from .shell_startup import publish_link


def _is_hook_script(path: Path) -> bool:
    if path.name.endswith((".sh", ".py")):
        return True
    try:
        with path.open("rb") as stream:
            return stream.read(2) == b"#!"
    except OSError:
        return False


def _entries(directory: Path) -> list[Path]:
    """The directory's entries in name order, without dot-names, as the shell's ``*`` glob."""
    return sorted(
        entry for entry in directory.iterdir() if not entry.name.startswith(".")
    )


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def _make_executable(path: Path, umask: int) -> None:
    """``chmod +x``: a failure fails the step, as the shell's unguarded ``chmod`` did."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        wanted = mode | (0o111 & ~umask)
        if wanted != mode:
            os.chmod(path, wanted)
    except OSError as exc:
        raise LinkError(f"could not make {path} executable: {exc}") from exc


def _prune_dangling(source_dir: Path, hooks_dir: Path) -> None:
    """Remove the links this step ORPHANED (ISSUE-REF): a hook deleted from the repo.

    Only an entry that is a symlink, whose target is gone, and that resolves into the checkout's
    own hooks directory is removed, so a hand-made link elsewhere and any real file are left.
    """
    own = os.path.realpath(source_dir) + os.sep
    try:
        links = _entries(hooks_dir)
    except OSError:
        links = []
    pruned = 0
    for link in links:
        if not link.is_symlink() or link.exists():
            continue
        if not os.path.realpath(link).startswith(own):
            continue
        try:
            link.unlink()
        except OSError:
            continue
        pruned += 1
        print(f"  [rm]  {link} (dangling; its hook script was removed from ai-harness)")
    if pruned == 0:
        print("  [ok]  no dangling ai-harness hook symlinks to prune")


def install(checkout: Path, home: Path, environ: Mapping[str, str]) -> None:
    """Make every file in ``<checkout>/.claude/hooks`` available in ``~/.claude/hooks``.

    A checkout that ships no hooks directory is a ``[skip]``. A ``chmod``, the hooks directory or
    a link that cannot be made raises ``LinkError``; under Git Bash the junction is queued and
    only the batched flush can fail it.
    """
    source_dir = checkout / ".claude" / "hooks"
    if not source_dir.is_dir():
        print("  [skip] .claude/hooks/ is not shipped; hook installation skipped")
        return
    hooks_dir = home / ".claude" / "hooks"
    try:
        entries = _entries(source_dir)
    except OSError as exc:
        raise LinkError(f"could not list {source_dir}: {exc}") from exc
    umask = _umask()
    for entry in entries:
        if entry.is_file() and _is_hook_script(entry):
            _make_executable(entry, umask)
    if _is_windows_shell(environ):
        queue_windows_junction(source_dir, hooks_dir)
        print("  [ok]  ~/.claude/hooks/ ⇒ ai-harness/.claude/hooks/ (junction)")
        return
    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LinkError(f"could not create {hooks_dir}: {exc}") from exc
    for entry in entries:
        if entry.is_file():
            publish_link(entry, hooks_dir / entry.name, environ)
    print(
        "  [ok]  ~/.claude/hooks/ ← ai-harness/.claude/hooks/ (per-file symlinks, every hook file)"
    )
    _prune_dangling(source_dir, hooks_dir)
