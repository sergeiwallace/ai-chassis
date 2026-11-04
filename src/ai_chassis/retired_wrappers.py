"""Retire wrapper links this repository no longer ships (install.sh's retired Step 5bc).

The link-publishing steps only create, so a renamed wrapper leaves its old link behind, and a
dangling entry is worse than an absent one: ``command -v`` fails on a name the directory listing
plainly shows. Only a DANGLING SYMLINK is ever removed: a resolvable link and a regular file a
user placed by hand both stay. Two bounds decide which dangling links go, as in the shell:

* a retired name (``RETIRED_WRAPPERS``) in ``~/.claude/bin`` or ``~/.local/bin`` is removed when
  dangling, whatever it pointed at;
* the sweep removes a dangling link only when its target was under a ``.claude/bin`` directory,
  the harness's own wrapper location; a dangling link with any other target stays.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import sys
from pathlib import Path

#: Wrappers renamed away (pre-v1 clean slate: no compatibility alias is kept).
RETIRED_WRAPPERS = ("aih-commit",)
#: A dangling link is ours to remove only when it pointed into a ``.claude/bin`` (ISSUE-REF).
_WRAPPER_TARGET = "*/.claude/bin/*"


#: The errors that prove a symlink's target is gone. Anything else (EACCES on a mode-000 parent,
#: say) means the target cannot be checked, and a link that may be valid is kept.
_GONE = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})


def _dangling(link: Path) -> bool:
    """A symlink whose target is provably gone; a path that cannot be inspected is not one.

    Not ``Path.exists()``, which is False for an unreadable target too, exactly as the shell's
    ``[ -e ]`` was, so a valid link under a mode-000 directory read as dangling.
    """
    try:
        if not link.is_symlink():
            return False
        os.stat(link)
    except OSError as exc:
        return exc.errno in _GONE
    return False


def _remove(link: Path, reason: str) -> None:
    try:
        link.unlink()
    except OSError as exc:
        print(f"  [warn] could not remove {link}: {exc}", file=sys.stderr)
        return
    print(f"  [rm]  {link} ({reason})")


def reap_dangling_wrapper_links(directory: Path) -> None:
    """Remove every dangling link in ``directory`` whose target was a ``.claude/bin`` wrapper.

    Bounded by TARGET rather than by name, because a whole-directory publication cannot be
    bounded by a list someone remembered to extend; a dangling link another tool owns stays. A
    directory that cannot be listed is left untouched with a warning, as the shell's glob simply
    failed to expand there.
    """
    if not directory.is_dir():
        return
    try:
        entries = sorted(directory.iterdir())
    except OSError as exc:
        print(f"  [warn] could not list {directory}: {exc}", file=sys.stderr)
        return
    for link in entries:
        if link.name.startswith(".") or not _dangling(link):
            continue
        try:
            target = os.readlink(link)
        except OSError:
            continue
        if fnmatch.fnmatchcase(target, _WRAPPER_TARGET):
            _remove(link, "dangling; wrapper no longer shipped")


def retire_wrappers(home: Path) -> None:
    """Each retired name in ``~/.claude/bin`` and ``~/.local/bin``, then the generic sweep."""
    claude_bin = home / ".claude" / "bin"
    local_bin = home / ".local" / "bin"
    for name in RETIRED_WRAPPERS:
        for directory in (claude_bin, local_bin):
            link = directory / name
            if _dangling(link):
                _remove(link, "dangling; renamed")
    reap_dangling_wrapper_links(local_bin)
    reap_dangling_wrapper_links(claude_bin)
