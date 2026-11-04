"""Local directory route. Queue acceptance is never a receiving acknowledgement."""

from collections import OrderedDict
import errno
import json
import re
from pathlib import Path

from ..doorbell import Doorbell
from ..events import Event, sync_directory, write_record
from ..route import Receipt, Route
from ..source import EventSource
from ..sources.intake import native_watch
from ..targets import MachineTarget

# Wake handlers raise fixed kebab-case reason codes (``inbox-fetch-unverified``,
# ``beads-pull-unverified``). Only that shape reaches telemetry: free text,
# paths and payloads in other exception messages stay out.
REASON_CODE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+")
MAX_REASON = 64
MAX_SETTLED_DETAIL = 64


def failure_reason(exc):
    (reason,) = exc.args if len(exc.args) == 1 else (None,)
    if (
        isinstance(reason, str)
        and len(reason) <= MAX_REASON
        and REASON_CODE.fullmatch(reason)
    ):
        return reason
    return None


class LocalIntakeRoute(Route):
    def __init__(self, root, watch_factory=native_watch):
        self.root = Path(root)
        self.watch_factory, self.watch = watch_factory, None

    def open(self):
        for name in ("incoming", "receipts"):
            (self.root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        sync_directory(self.root)
        sync_directory(self.root.parent)
        self.watch = self.watch_factory(self.root / "receipts")

    def available(self):
        return (self.root / "incoming").is_dir()

    def ring(self, doorbell):
        path = self.root / "receipts" / f"{doorbell.identity}.json"
        if path.exists():
            row = json.loads(path.read_text())
            if row.get("identity") == doorbell.identity and row.get("durable") is True:
                return Receipt(True)
        write_record(
            self.root / "incoming" / f"{doorbell.identity}.json", doorbell.record()
        )
        return Receipt(False, "receiving-handoff-pending")

    def readable_handles(self):
        return self.watch.readable_handles() if self.watch else ()

    def drain(self):
        self.watch.drain()

    def close(self):
        if self.watch:
            self.watch.close()
            self.watch = None


class DoorbellSource(EventSource):
    def __init__(self, root, dispatcher, handlers, secret, watch_factory=native_watch):
        self.root, self.dispatcher = Path(root), dispatcher
        self.handlers, self.secret, self.watch_factory = handlers, secret, watch_factory
        self.watch = None
        self.pending = {}
        self.status = {}
        self.doorbell_detail = {}
        self.settled = OrderedDict()
        self.retries = {}
        self.read_attempts = {}

    def bind_deadlines(self, deadlines, _clock):
        self.retries = {
            row["event"]["key"]: row["event"]
            for row in deadlines.entries.values()
            if row["event"]["source"] == "receiver-reconcile"
        }

    def _retry(self, bell, attempt):
        self._arm_retry(bell.identity, attempt)

    def read_ready(self, key):
        """Re-run the attempt whose background read completed.

        Arming attempt 1 here restarted the backoff on every completed read, so a
        doorbell that could never verify retried every 4 s forever.
        """
        self._arm_retry(key, self.read_attempts.get(key, 1) - 1, ready=True)

    def _arm_retry(self, key, attempt, *, catch_up=False, ready=False):
        if key in self.retries:
            return
        event = Event(
            "receiver-reconcile",
            key,
            str(attempt),
            MachineTarget("local"),
            deadline=self.dispatcher.clock()
            + (0 if ready else min(300, 2 ** min(attempt, 8))),
            disposition_hint="catch-up" if catch_up else "",
        )
        self.dispatcher.deadlines.arm(event.identity, event.deadline, event)
        self.retries[key] = event.as_record()

    @property
    def recovery_complete(self):
        return not any(
            row["disposition_hint"] == "catch-up" for row in self.retries.values()
        )

    def _catch_up(self, name, attempt=1):
        key = "catch-up:" + name
        operation = "handle"
        try:
            batch = self.handlers[name].catch_up()
            operation = "accept"
            self.dispatcher.accept(batch)
        except Exception as exc:
            self.status[key] = "receiving-pending:" + type(exc).__name__
            self._failure_detail(key, exc, operation, name)
            self._arm_retry(key, attempt, catch_up=True)
            return []
        retry = self.retries.pop(key, None)
        if retry is not None:
            self.dispatcher.deadlines.acknowledge(Event.from_record(retry).identity)
        self.status[key] = "ok"
        self.doorbell_detail.pop(key, None)
        return batch

    def _failure_detail(self, identity, exc, operation, source):
        self.doorbell_detail[identity] = {
            "class": type(exc).__name__,
            "errno": errno.errorcode.get(getattr(exc, "errno", None)),
            "operation": operation,
            "source": source,
            "reason": failure_reason(exc),
        }

    def _settled_detail(self, handler, bell):
        """Name a terminal non-delivery; the newest MAX_SETTLED_DETAIL are kept."""
        status = self.status[bell.identity]
        if status not in getattr(handler, "detailed_statuses", ()):
            self.doorbell_detail.pop(bell.identity, None)
            return
        self.settled.pop(bell.identity, None)
        self.settled[bell.identity] = None
        while len(self.settled) > MAX_SETTLED_DETAIL:
            self.doorbell_detail.pop(self.settled.popitem(last=False)[0], None)
        self.doorbell_detail[bell.identity] = {
            "class": None,
            "errno": None,
            "operation": "settle",
            "source": bell.source,
            "reason": status,
        }

    def open(self):
        for name in ("incoming", "receipts"):
            (self.root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        sync_directory(self.root)
        sync_directory(self.root.parent)
        self.watch = self.watch_factory(self.root / "incoming")

    def readable_handles(self):
        return self.watch.readable_handles()

    def _read(self, paths, attempt=1, *, source=None):
        events = []
        for path in paths:
            if not path.is_file() or path.suffix != ".json":
                continue
            try:
                bell = Doorbell.decode(path.read_bytes())
                verified = path.stem == bell.identity and bell.verify(self.secret)
            except (ValueError, TypeError, KeyError):
                self.status[path.stem] = "malformed-doorbell"
                continue
            if not verified:
                self.status[path.stem] = "signature-mismatch"
                continue
            if source is not None and bell.source != source:
                continue
            if (self.root / "receipts" / path.name).exists():
                continue
            handler = self.handlers.get(bell.source)
            if handler is None:
                self.status[path.stem] = "unknown-source"
                continue
            operation = "handle"
            self.read_attempts[bell.identity] = attempt
            try:
                batch = handler.handle(bell)
                if getattr(handler, "receiving_deferred", False):
                    self.status[bell.identity] = handler.status
                    continue
                operation = "accept"
                self.dispatcher.accept(batch)
            except Exception as exc:
                self.status[bell.identity] = "receiving-pending:" + type(exc).__name__
                self._failure_detail(bell.identity, exc, operation, bell.source)
                self._retry(bell, attempt)
                continue
            # The host's collection barrier holds effects. Persist each verified
            # batch now so a later terminal receipt in this same drain can cancel
            # it before any effect is released. The host's aggregate accept is
            # idempotent by event identity.
            self.pending[bell.identity] = batch
            self.status[bell.identity] = getattr(handler, "status", "ok")
            self._settled_detail(handler, bell)
            events.extend(batch)
        return events

    def reevaluate_inbox(self):
        return self._read(
            sorted((self.root / "incoming").glob("*.json")), source="git-inbox"
        )

    def catch_up(self, _cursor):
        events = []
        for name, handler in self.handlers.items():
            catch_up = getattr(handler, "catch_up", None)
            if catch_up is not None:
                events.extend(self._catch_up(name))
        return events + self._read(sorted((self.root / "incoming").glob("*.json")))

    def expire(self, event):
        if event.source == "receiver-reconcile":
            self.retries.pop(event.key, None)
            if event.disposition_hint == "catch-up":
                self._catch_up(
                    event.key.removeprefix("catch-up:"), int(event.revision) + 1
                )
                return True
            batch = self._read(
                [self.root / "incoming" / f"{event.key}.json"], int(event.revision) + 1
            )
            self.commit(batch)
            return True
        handler = self.handlers.get(event.source)
        expire = getattr(handler, "expire", None)
        return expire(event) if expire is not None else False

    def drain(self):
        names = self.watch.drain()
        paths = (
            sorted((self.root / "incoming").glob("*.json"))
            if names is None
            else [self.root / "incoming" / name for name in names]
        )
        return self._read(paths)

    def commit(self, _events):
        for key, batch in list(self.pending.items()):
            for event in batch:
                if event.identity not in self.dispatcher.obligations:
                    raise ValueError("receiving-obligation-not-durable")
                if (
                    event.deadline is not None
                    and event.identity not in self.dispatcher.cancelled
                ):
                    self.dispatcher.deadlines.arm(event.identity, event.deadline, event)
            # New obligation/deadline directories must precede receiving success,
            # as must their already-fsynced files.
            sync_directory(self.dispatcher.root)
            sync_directory(self.dispatcher.root.parent)
            write_record(
                self.root / "receipts" / f"{key}.json",
                {"identity": key, "durable": True, "status": self.status[key]},
            )
            retry = self.retries.pop(key, None)
            if retry is not None:
                self.dispatcher.deadlines.acknowledge(Event.from_record(retry).identity)
            self.read_attempts.pop(key, None)
            del self.pending[key]

    def recovery_blocks(self, event):
        if self.watch is None:
            return event.source in {"git-inbox", "assignment", "harness-sync"}
        source = "beads" if event.source == "assignment" else event.source
        return "catch-up:" + source in self.retries

    def close(self):
        if self.watch:
            self.watch.close()
            self.watch = None
