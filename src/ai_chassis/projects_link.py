"""``~/projects`` as a link to the projects root (IN-127; install.sh's header-less block, ISSUE-REF).

A committed pointer needs one string that resolves on every machine, and ``/workspace/projects is
that string: Ruff expands a literal ``~`` when it loads an ``extend``. On a machine whose repos
are not under ``$HOME`` nothing created that path, so this step makes it a link to the root.

Deliberately narrow, because it writes under ``$HOME``:

- nothing to do when the root already is ``~/projects``;
- written only when the path is absent or already a symlink (repointed when it aims elsewhere),
  so a real directory of somebody's repos is never touched;
- anything else at the path is reported and left alone, and the lint pointer step then falls back
  to the absolute pointer on that machine.

Every arm returns normally, as the shell block never failed the install.
"""

from __future__ import annotations

import os
from pathlib import Path


def _same(first: Path, second: Path) -> bool:
    """The shell's ``[[ first -ef second ]]``: both exist and are one file."""
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def ensure(root: Path, home: Path) -> None:
    link = home / "projects"
    if _same(root, link) or os.path.abspath(root) == os.path.abspath(link):
        return
    if link.is_symlink() or not os.path.lexists(link):
        try:
            if link.is_symlink():
                link.unlink()
            link.symlink_to(root, target_is_directory=True)
            pointed = _same(root, link)
        except OSError:
            pointed = False
        if pointed:
            print(f"  [ok]  ~/projects -> {root}")
        else:
            print(
                f"  [warn] ~/projects could not be pointed at {root}; "
                "the lint pointer stays absolute"
            )
        return
    print(
        "  [skip] ~/projects is a real directory and not the projects root; "
        "leaving it untouched"
    )
