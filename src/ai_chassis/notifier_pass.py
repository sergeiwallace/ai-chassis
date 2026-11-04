"""An observation scope, discarded at the next notifier pass."""

import os
import time
from contextvars import ContextVar
from dataclasses import dataclass

current: ContextVar[object | None] = ContextVar("notifier_pass", default=None)


@dataclass
class WriteCounts:
    """One pass's durable writes (ISSUE-REF): files written, fsyncs and time in fsync."""

    files_written: int = 0
    fsyncs: int = 0
    fsync_ms: float = 0.0


writes: ContextVar[WriteCounts | None] = ContextVar(
    "notifier_pass_writes", default=None
)


def fsync(fd: int) -> None:
    """``os.fsync``, counted and timed into the running pass's ``writes``, if any."""
    started = time.perf_counter()
    os.fsync(fd)
    counts = writes.get()
    if counts is not None:
        counts.fsyncs += 1
        counts.fsync_ms += (time.perf_counter() - started) * 1000


def wrote_file() -> None:
    """Count one file the running pass wrote, if any."""
    counts = writes.get()
    if counts is not None:
        counts.files_written += 1
