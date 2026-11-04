"""Content-free immutable facts and durable, atomic state helpers."""

import hashlib
import json
import math
import os
import tempfile
from abc import ABC
from dataclasses import dataclass
from pathlib import Path

from .targets import TargetSelector, target_from_record


@dataclass(frozen=True)
class Event(ABC):
    source: str
    key: str
    revision: str
    target: TargetSelector
    requires_action: bool = False
    deadline: float | None = None
    origin_machine: str = "local"
    observed_at: float = 0.0
    disposition_hint: str = ""
    schema: int = 1
    turn_generation: int = 0
    lifecycle_sequence: int = 0
    scope: str = "addressed"
    sender: str | None = None

    def __post_init__(self):
        if self.sender is not None and not isinstance(self.sender, str):
            raise ValueError("invalid-event-sender")
        if self.scope not in {"addressed", "machine"}:
            raise ValueError("invalid-event-scope")
        if type(self.turn_generation) is not int or self.turn_generation < 0:
            raise ValueError("invalid-turn-generation")
        if type(self.lifecycle_sequence) is not int or self.lifecycle_sequence < 0:
            raise ValueError("invalid-lifecycle-sequence")
        if self.schema != 1 or not isinstance(self.target, TargetSelector):
            raise ValueError("invalid-event")
        if any(
            not isinstance(value, str) or not value
            for value in (self.source, self.key, self.revision, self.origin_machine)
        ):
            raise ValueError("invalid-event")
        if type(self.requires_action) is not bool or not math.isfinite(
            self.observed_at
        ):
            raise ValueError("invalid-event")
        if self.deadline is not None and not math.isfinite(self.deadline):
            raise ValueError("invalid-deadline")

    def as_record(self):
        return {
            "schema": self.schema,
            "source": self.source,
            "key": self.key,
            "revision": self.revision,
            "target": self.target.as_record(),
            "requires_action": self.requires_action,
            "deadline": self.deadline,
            "origin_machine": self.origin_machine,
            "observed_at": self.observed_at,
            "disposition_hint": self.disposition_hint,
            "turn_generation": self.turn_generation,
            "lifecycle_sequence": self.lifecycle_sequence,
            "scope": self.scope,
            "sender": self.sender,
        }

    @property
    def identity(self):
        # Observation times do not reopen the same source revision after replay.
        return digest([self.source, self.key, self.revision, self.target.as_record()])

    @classmethod
    def from_record(cls, record):
        values = dict(record)
        values["target"] = target_from_record(values["target"])
        return cls(**values)


LIFECYCLE_KINDS = frozenset(
    {"user-prompt", "stop", "idle-prompt", "precompact", "postcompact", "session-end"}
)


LIFECYCLE_INPUT_KINDS = frozenset(
    {
        "stop",
        "session-start",
        "session-end",
        "user-prompt",
        "precompact",
        "postcompact",
        "process-exit",
        "session-evidence",
        "pane-evidence",
        "pane-unavailable",
        "goal-due",
        "save-state-due",
        "compact-expiry",
        "compact-quiet",
        "compact-lifecycle",
        "idle-prompt",
        "fifo-enter",
        "goal-clear-expiry",
        "continuation-banner-expiry",
        "compact-rearm-due",
    }
)
LIFECYCLE_INPUT_KINDS = LIFECYCLE_INPUT_KINDS | {
    "goal-marker-expiry",
    "goal-marker-fallback",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def sync_directory(path: Path):
    # Windows flushes the file before replace; POSIX also persists directory entries.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_record(path: Path, record):
    """Replace a record only when different; never expose partial state."""
    body = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if path.is_symlink():
        raise ValueError("symlink-state")
    if path.exists() and path.read_bytes() == body:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, path)
        sync_directory(path.parent)
    finally:
        if staged.exists():
            staged.unlink()
