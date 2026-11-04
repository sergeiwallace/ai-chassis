"""The notifier's flock-shaped Windows byte lock, shared by daemon and CLI writers."""

from __future__ import annotations

import errno
import os
import time

try:
    import msvcrt
except ImportError:  # Allows the byte-lock contract to be tested on POSIX.
    msvcrt = None

LOCK_EX = 2
LOCK_UN = 8
LOCK_TIMEOUT_S = 30


def flock(fd: int, operation: int) -> None:
    if msvcrt is None:
        raise OSError("Windows byte locking unavailable")
    position = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if operation == LOCK_UN:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return
        if operation != LOCK_EX:
            raise ValueError("unsupported notifier lock operation")
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("notifier state lock timed out") from None
                time.sleep(0.05)
    finally:
        os.lseek(fd, position, os.SEEK_SET)
