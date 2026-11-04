"""Bound temporary attestation copies even when an earlier owner could not unwind.

A ``finally`` cannot run when its process is killed with SIGKILL, so the scratch dirs of
killed runs outlived them (3.6 GB each, 2026-10-15). Each scratch root names its creator's
pid; the next run removes this prefix's old siblings whose creator is gone.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from process_liveness import pid_alive

STALE_SECONDS = 60 * 60
SIGTERM_EXIT = 128 + signal.SIGTERM
# tempfile.mkdtemp appends eight characters from [a-z0-9_].
_SUFFIX = r"[a-z0-9_]{8}"


def _creator_gone(name: str, prefix: str) -> bool:
    """True when ``name`` is one of this prefix's roots and its creator is not running.

    A root named by the older format (no pid) has no creator to check, so only its age
    decides. A ``keep`` root carries a ``kept-`` infix and never matches.
    """
    match = re.fullmatch(re.escape(prefix) + rf"(?:p(\d+)-)?{_SUFFIX}", name)
    if match is None:
        return False
    pid = match.group(1)
    return pid is None or not pid_alive(int(pid))


def _removable(path: Path, parent: Path, prefix: str, cutoff: float) -> bool:
    if not _creator_gone(path.name, prefix) or path.parent != parent:
        return False
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_mtime >= cutoff:
        return False
    # Windows reports st_uid 0 for everyone; its temp dir is already per-user.
    return os.name == "nt" or details.st_uid == os.getuid()


def _tree_bytes(path: Path) -> int:
    return sum(
        (Path(root) / name).lstat().st_size
        for root, _dirs, files in os.walk(path, followlinks=False)
        for name in files
    )


def sweep(parent: Path, prefix: str) -> None:
    """Remove this prefix's dead, old roots directly under ``parent``; never raises."""
    removed = freed = 0
    try:
        cutoff = time.time() - STALE_SECONDS
        for path in parent.iterdir():
            try:
                if not _removable(path, parent, prefix, cutoff):
                    continue
                size = _tree_bytes(path)
                # Re-prove the target after walking it; rmtree does not follow child links.
                if not _removable(path, parent, prefix, cutoff):
                    continue
                shutil.rmtree(path)
                removed += 1
                freed += size
            except OSError:
                continue  # one unreadable, replaced or unremovable sibling cannot block work
        if removed:
            print(
                f"attest-scratch: removed {removed} stale dir(s), {freed} bytes",
                file=sys.stderr,
            )
    except OSError:
        pass  # best effort, including an unreadable temp parent


def make_scratch(prefix: str, *, keep: bool = False) -> Path:
    """Sweep this prefix's stale roots, then create a root that names its creator.

    A ``keep`` root is named so no sweep ever matches it.
    """
    if not prefix or Path(prefix).name != prefix:
        raise ValueError("scratch prefix must be a nonempty basename")
    sweep(Path(tempfile.gettempdir()).resolve(), prefix)
    infix = "kept-" if keep else f"p{os.getpid()}-"
    return Path(tempfile.mkdtemp(prefix=prefix + infix))


@contextmanager
def term_cleanup():
    """Let the entry's ``finally`` run on POSIX TERM; restore the caller's handler."""
    if os.name == "nt" or threading.current_thread() is not threading.main_thread():
        yield
        return

    def terminate(_signum, _frame):
        raise SystemExit(SIGTERM_EXIT)

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
