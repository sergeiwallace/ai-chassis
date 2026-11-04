"""Persisted, event-armed deadlines. Expiry is acknowledged after durable handoff."""

from abc import ABC, abstractmethod
import contextlib
import json
import math
from pathlib import Path
import time

from .events import Event, digest, sync_directory, write_record


class DeadlineSet(ABC):
    def eligible(self, _event):
        """Host recovery gate; retain held timers without waking for them."""
        return True

    @abstractmethod
    def arm(self, key, at, event):
        """Persist one event-armed deadline, replacing the same obligation key."""

    @abstractmethod
    def cancel(self, key):
        """Cancel only this obligation."""

    def acknowledge(self, key):
        """Retire an expiry after durable handoff, without cancelling pending work."""
        self.cancel(key)

    @abstractmethod
    def next_due(self):
        """Earliest eligible wall time, or None for indefinite waiting."""

    @abstractmethod
    def pop_due(self, now):
        """Return eligible due events, retained until acknowledged after handoff."""


class PersistentDeadlineSet(DeadlineSet):
    def __init__(self, state_dir: Path, clock=time.time, monotonic=time.monotonic):
        self.root = Path(state_dir)
        self.clock, self.monotonic = clock, monotonic
        self.entries = {}
        self.status = {}
        self.changed = lambda: None
        self.cancelling = lambda _key, _event: contextlib.nullcontext()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in sorted(self.root.glob("*.json")):
            try:
                record = json.loads(path.read_text())
                Event.from_record(record["event"])
                if not math.isfinite(record["due_at"]):
                    raise ValueError("invalid-time")
                self.entries[record["key"]] = record
            except (OSError, ValueError, KeyError, TypeError):
                self.status[path.name] = "degraded:invalid-deadline"

    def arm(self, key, at, event):
        if not isinstance(key, str) or not key or not math.isfinite(at):
            raise ValueError("invalid-deadline")
        record = {
            "key": key,
            "due_at": at,
            "event": event.as_record(),
            "armed_by": event.identity,
            "wall_anchor": self.clock(),
            "boot_anchor": self.monotonic(),
        }
        write_record(self.root / f"{digest(key)}.json", record)
        self.entries[key] = record
        self.changed()

    def cancel(self, key):
        record = self.entries.get(key)
        event = Event.from_record(record["event"]) if record else None
        # The host journals the terminal state before deleting the timer, under
        # the same writer lock as delivery. A crash can only leave a stale timer.
        with self.cancelling(key, event):
            self.acknowledge(key)

    def acknowledge(self, key):
        if key in self.entries:
            path = self.root / f"{digest(key)}.json"
            # Verify the actual record before deletion; never delete by a pattern.
            if path.exists():
                if json.loads(path.read_text())["key"] != key:
                    raise ValueError("deadline-key-mismatch")
                path.unlink()
                sync_directory(self.root)
            self.entries.pop(key)
            self.changed()

    def next_due(self):
        return min(
            (
                row["due_at"]
                for row in self.entries.values()
                if self.eligible(Event.from_record(row["event"]))
            ),
            default=None,
        )

    def pop_due(self, now):
        return [
            (key, Event.from_record(row["event"]))
            for key, row in sorted(
                self.entries.items(), key=lambda item: item[1]["due_at"]
            )
            if row["due_at"] <= now and self.eligible(Event.from_record(row["event"]))
        ]
