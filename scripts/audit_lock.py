"""Small cross-platform advisory file-lock primitive for audit state."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import BinaryIO

if os.name == "nt":  # pragma: no cover - exercised on Windows CI
    import msvcrt

    BACKEND = "msvcrt"
else:  # pragma: no cover - branch selection is platform dependent
    import fcntl

    BACKEND = "fcntl"


class LockBusy(RuntimeError):
    """The requested non-blocking advisory lock is already held."""


@contextmanager
def exclusive(handle: BinaryIO, *, blocking: bool = True) -> Iterator[None]:
    """Hold an exclusive lock over the first byte of an opened lock file."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(handle.fileno(), mode, 1)
        except OSError as exc:
            if not blocking:
                raise LockBusy("advisory lock is busy") from exc
            raise
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        operation = fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), operation)
        except BlockingIOError as exc:
            raise LockBusy("advisory lock is busy") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def shared(handle: BinaryIO, *, blocking: bool = True) -> Iterator[None]:
    """Hold a shared lock on an opened lock file, by default blocking until no exclusive holder remains.

    Any number of shared holders coexist, and while one is held an ``exclusive(...,
    blocking=False)`` attempt raises :class:`LockBusy`. With ``blocking=False`` an exclusive
    holder makes this raise :class:`LockBusy` instead of waiting. ``msvcrt`` has no shared
    mode, so on Windows this holds nothing and never reports busy.
    """
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        yield
        return
    operation = fcntl.LOCK_SH if blocking else fcntl.LOCK_SH | fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), operation)
    except BlockingIOError as exc:
        raise LockBusy("advisory lock is busy") from exc
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def fsync_directory(path: os.PathLike[str] | str) -> None:
    """Durably sync a parent directory where the platform supports it."""
    if os.name == "nt":  # pragma: no cover - Windows has no directory file descriptor
        return
    descriptor = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
