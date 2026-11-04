"""Content-free doorbells and pre-mutation, process-owned notification intents.

An outbox owner is an EventSource, independently of the host's delivery selector.
Prepare waits for that owner's durable acknowledgement, never for a route. The
producer writes completion; kernel process exit covers the uncertain/crash path.
Start, completion, exit, route-up and intent-specific recovery deadlines reconcile
or forward retained work.
"""

from collections import Counter
from dataclasses import dataclass, replace
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import queue
import threading
import time

from .dispatcher import WakeSignal, wait_readable
from .events import Event, digest, sync_directory, write_record
from .route import Receipt
from .source import EventSource
from .sources.intake import native_watch
from .sources.process_exit import native_process_watch, process_stamp
from .targets import MachineTarget, TargetSelector, target_from_record

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows host verification
    fcntl = None

TOKEN = re.compile(r"[A-Za-z0-9_./:@*+-]{1,160}\Z")
MAX_BYTES = 512


def _token(value):
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise ValueError("invalid-doorbell-identifier")
    return value


@dataclass(frozen=True)
class Doorbell:
    source: str
    key: str
    revision: str
    target: TargetSelector
    origin_machine: str
    created_at: int
    signature: str = ""
    schema: int = 1

    def __post_init__(self):
        for value in (self.source, self.key, self.revision, self.origin_machine):
            _token(value)
        if isinstance(self.target, TargetSelector):
            _token(self.target.value)
        if self.signature and not re.fullmatch(r"[0-9a-f]{64}", self.signature):
            raise ValueError("invalid-signature")
        if (
            self.schema != 1
            or type(self.created_at) is not int
            or not isinstance(self.target, TargetSelector)
        ):
            raise ValueError("invalid-doorbell")
        if len(self.encode()) > MAX_BYTES:
            raise ValueError("oversize-doorbell")

    def record(self):
        return {
            "schema": self.schema,
            "source": self.source,
            "key": self.key,
            "revision": self.revision,
            "target": self.target.as_record(),
            "origin_machine": self.origin_machine,
            "created_at": self.created_at,
            "signature": self.signature,
        }

    def encode(self):
        return json.dumps(self.record(), sort_keys=True, separators=(",", ":")).encode()

    @property
    def identity(self):
        return digest([self.source, self.key, self.revision, self.target.as_record()])

    def sign(self, secret):
        unsigned = replace(self, signature="")
        return replace(
            unsigned,
            signature=hmac.new(secret, unsigned.encode(), hashlib.sha256).hexdigest(),
        )

    def verify(self, secret):
        return hmac.compare_digest(self.signature, self.sign(secret).signature)

    @classmethod
    def decode(cls, raw):
        if len(raw) > MAX_BYTES:
            raise ValueError("oversize-doorbell")
        row = json.loads(raw)
        target = target_from_record(row["target"])
        if target.as_record() != row["target"]:
            raise ValueError("invalid-target-fields")
        row["target"] = target
        return cls(**row)


class IntentRefused(RuntimeError):
    pass


class _ReconcileWorker:
    """Read source facts away from the receipt loop; only that loop mutates state.

    One worker bounds concurrency even if a source never returns. Pending work
    remains durable in requests/, and a blocked read cannot delay ownership.
    Queue and result readiness are event driven, with no sampling deadline.
    """

    def __init__(self, reconcile):
        self.reconcile = reconcile
        self.jobs, self.results = queue.Queue(), queue.Queue()
        self.signal = WakeSignal()
        self.stopped = threading.Event()
        self.result_lock = threading.Lock()
        self.thread = threading.Thread(
            target=self._serve, daemon=True, name="wake-source-reconcile"
        )
        self.thread.start()

    def _serve(self):
        while (job := self.jobs.get()) is not None:
            if self.stopped.is_set():
                break
            key, row = job
            try:
                result, reason = self.reconcile(row), ""
            except Exception as exc:
                result, reason = None, "reconcile-unavailable:" + type(exc).__name__
            with self.result_lock:
                if not self.stopped.is_set():
                    self.results.put((key, result, reason))
                    self.signal.notify()

    def close(self):
        with self.result_lock:
            self.stopped.set()
            self.signal.close()
        self.jobs.put(None)
        # A source may still be blocked. It is a daemon worker, owns no durable
        # writes, and the stopped fence above prevents a late result notification.


def _owner_lock(handle, *, release=False):
    if fcntl is not None:
        fcntl.flock(
            handle.fileno(), fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB
        )
    else:  # pragma: no cover - Windows canary uses a single nonblocking attempt
        import msvcrt

        if not release and os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(
            handle.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1
        )


class Producer:
    """The shared producer boundary; no external mutation precedes prepare()."""

    def __init__(self, root, *, timeout=10, watch_factory=native_watch):
        self.root = Path(root)
        self.timeout, self.watch_factory = timeout, watch_factory

    def prepare(self, operation, bells, *, cursor, candidate, context):
        key = digest(operation)
        requests, acks = self.root / "requests", self.root / "owned"
        requests.mkdir(parents=True, exist_ok=True, mode=0o700)
        acks.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Subscribe before publishing the request, so fast ownership cannot be missed.
        watch = self.watch_factory(acks)
        try:
            row = {
                "operation": operation,
                "cursor": cursor,
                "candidate": candidate,
                "context": context,
                "bells": [
                    {"machine": machine, "doorbell": bell.record()}
                    for machine, bell in bells
                ],
                "pid": os.getpid(),
                "start_stamp": process_stamp(os.getpid()),
            }
            path = requests / f"{key}.json"
            if path.exists():
                old = json.loads(path.read_text())
                if old != row:
                    raise IntentRefused("operation-key-reused")
            write_record(path, row)
            until = time.monotonic() + self.timeout
            ack = acks / f"{key}.json"
            while not ack.exists():
                remaining = until - time.monotonic()
                if remaining <= 0 or not wait_readable(
                    watch.readable_handles(), remaining
                ):
                    raise IntentRefused("owner-ack-timeout")
                watch.drain()
            if json.loads(ack.read_text()).get("request") != digest(row):
                raise IntentRefused("owner-ack-mismatch")
            return key
        except (OSError, ValueError) as exc:
            raise IntentRefused("intent-handoff-failed") from exc
        finally:
            watch.close()

    def complete(self, key):
        write_record(self.root / "requests" / f"{key}.complete", {"key": key})


class OutboxOwner(EventSource):
    """Durable producer owner; pluggable routes never define receiving success."""

    def __init__(  # noqa: PLR0913 - injected OS identity and subscription capabilities
        self,
        root,
        reconciler,
        routes,
        *,
        watch_factory=native_watch,
        process_watch=native_process_watch,
        identity=process_stamp,
        background_reconcile=False,
    ):
        self.root = Path(root)
        self.reconciler, self.routes = reconciler, routes
        self.watch_factory, self.process_watch, self.identity = (
            watch_factory,
            process_watch,
            identity,
        )
        self.requests, self.states, self.processes = {}, {}, {}
        self.watch = None
        self._route_status = {}
        self.lock = None
        self.deadlines = None
        self.clock = time.time
        self.background_reconcile = background_reconcile
        self.worker = None
        self.reconciling = {}

    @staticmethod
    def _no_route_counts(states):
        return dict(
            Counter(
                machine
                for row in states
                for machine in row.get("no_route", {}).values()
            )
        )

    @property
    def status(self):
        return {
            **self._route_status,
            "no_route": self._no_route_counts(self.states.values()),
            "reconcile_pending": len(self.reconciling),
        }

    @classmethod
    def read_status(cls, root):
        """Read persisted retirements without taking ownership or writing files."""
        states = [
            json.loads(path.read_text())
            for path in (Path(root) / "states").glob("*.json")
        ]
        return {
            "no_route": cls._no_route_counts(states),
            "reconcile_pending": sum(
                row.get("reason") == "reconcile-pending" for row in states
            ),
        }

    def bind_deadlines(self, deadlines, clock):
        self.deadlines, self.clock = deadlines, clock

    def _retry(self, key):
        if self.deadlines is None:
            return  # Standalone callers reconcile explicitly; service owners bind below.
        previous = self.states[key].get("retry")
        if previous and previous in self.deadlines.entries:
            return
        attempt = self.states[key].get("retry_attempt", 0) + 1
        event = Event(
            "producer-reconcile",
            key,
            str(attempt),
            MachineTarget("local"),
            deadline=self.clock() + min(300, 2 ** min(attempt, 8)),
        )
        self._save(key, retry=event.identity, retry_attempt=attempt)
        self.deadlines.arm(event.identity, event.deadline, event)

    def expire(self, event):
        if event.source != "producer-reconcile" or event.key not in self.requests:
            return False
        key = event.key
        if self.states[key].get("retry") == event.identity:
            self._save(key, retry=None)
            self._reconcile(
                key,
                exited=key not in self.processes
                or (self.root / "requests" / f"{key}.complete").exists(),
            )
        return True

    def _clear_retry(self, key):
        retry = self.states[key].get("retry")
        if retry and self.deadlines is not None:
            self.deadlines.acknowledge(retry)
            self._save(key, retry=None)

    def open(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (self.root / "owner.lock").open("a+b")
        try:
            _owner_lock(self.lock)
        except BaseException:
            self.lock.close()
            self.lock = None
            raise
        for name in ("requests", "owned", "states"):
            (self.root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        sync_directory(self.root)
        sync_directory(self.root.parent)
        self.watch = self.watch_factory(self.root / "requests")
        if self.background_reconcile:
            self.worker = _ReconcileWorker(self.reconciler)
        for machine, route in self.routes.items():
            if hasattr(route, "open"):
                try:
                    route.open()
                except Exception as exc:
                    self._route_status[machine] = (
                        "route-unavailable:" + type(exc).__name__
                    )

    def readable_handles(self):
        return (
            tuple(self.watch.readable_handles())
            + tuple(h for w in self.processes.values() for h in w.readable_handles())
            + tuple(
                h
                for r in self.routes.values()
                for h in getattr(r, "readable_handles", lambda: ())()
            )
            + ((self.worker.signal.reader,) if self.worker else ())
        )

    def _save(self, key, **updates):
        row = {**self.states.get(key, {}), **updates}
        write_record(self.root / "states" / f"{key}.json", row)
        self.states[key] = row

    def _unwatch(self, key):
        watch = self.processes.pop(key, None)
        if watch is not None:
            watch.close()

    def _own(self, key):
        row = json.loads((self.root / "requests" / f"{key}.json").read_text())
        if digest(row["operation"]) != key:
            raise ValueError("intent-identity-mismatch")
        self.requests[key] = row
        path = self.root / "states" / f"{key}.json"
        self.states[key] = json.loads(path.read_text()) if path.exists() else {}
        if self.states[key].get("state") in {"handed-off", "retired"}:
            return
        pid, stamp = row["pid"], row["start_stamp"]
        if stamp is None:
            raise ValueError("producer-identity-unavailable")
        if self.identity(pid) == stamp:
            try:
                watch = self.process_watch(pid)
            except OSError:
                # Exit may win the race against subscription. Unknown/denied
                # subscriptions still refuse ownership for a living process.
                if self.identity(pid) == stamp:
                    raise
                watch = None
            if watch is not None and self.identity(pid) == stamp:
                self.processes[key] = watch
            elif watch is not None:
                watch.close()
        self._save(key, state=self.states[key].get("state", "prepared"))
        # Persist only AFTER the process subscription is armed (or exit proven).
        write_record(self.root / "owned" / f"{key}.json", {"request": digest(row)})

    def _reconcile(self, key, *, exited=False):
        if self.states[key].get("state") in {"handed-off", "retired"}:
            self._clear_retry(key)
            return
        if self.states[key].get("state") != "committed":
            if self.worker is not None:
                if key in self.reconciling:
                    self.reconciling[key] |= exited
                else:
                    self.reconciling[key] = exited
                    self._save(key, reason="reconcile-pending")
                    self.worker.jobs.put((key, self.requests[key]))
                return
            try:
                result = self.reconciler(self.requests[key])
            except Exception as exc:
                self._save(key, reason="reconcile-unavailable:" + type(exc).__name__)
                self._retry(key)
                return
            self._reconciled(key, result, exited=exited)
            return
        self._forward(key)

    def _reconciled(self, key, result, *, exited, reason=""):
        if result is not True:
            if result is False and exited:
                self._save(key, state="retired", reason="proven-noncommit")
                self._unwatch(key)
                self._clear_retry(key)
            else:
                self._save(
                    key,
                    reason=reason
                    or ("outcome-unknown" if result is None else "prepared"),
                )
                if exited or reason:
                    self._retry(key)
            return
        self._save(key, state="committed", reason="")
        self._unwatch(key)
        self._forward(key)

    def _forward(self, key):
        accepted = set(self.states[key].get("accepted", []))
        no_route = dict(self.states[key].get("no_route", {}))
        for entry in self.requests[key]["bells"]:
            bell = Doorbell.decode(json.dumps(entry["doorbell"]).encode())
            identity = digest([entry["machine"], bell.identity])
            if identity in accepted or identity in no_route:
                continue
            route = self.routes.get(entry["machine"])
            if route is None:
                # The source's poll still delivers this item. Persist retirement
                # per bell so a later route cannot resurrect it or count it twice.
                no_route[identity] = entry["machine"]
                self._save(key, no_route=no_route, reason="no-route")
                continue
            receipt = Receipt(False, "route-down")
            try:
                if route.available():
                    receipt = route.ring(bell)
            except Exception as exc:
                receipt = Receipt(False, "route-unavailable:" + type(exc).__name__)
            if receipt.accepted:
                accepted.add(identity)
            self._save(key, accepted=sorted(accepted), reason=receipt.reason)
            if not receipt.accepted and not (
                receipt.reason == "receiving-handoff-pending"
                and getattr(route, "readable_handles", lambda: ())()
            ):
                # A queued local handoff has a receipt watch. A failed write,
                # unavailable route or refusal may produce no further input;
                # retain recovery on this intent's existing durable deadline.
                self._retry(key)
        if len(accepted) + len(no_route) == len(self.requests[key]["bells"]):
            retired = bool(no_route) and not accepted
            self._save(
                key,
                state="retired" if retired else "handed-off",
                reason="no-route" if retired else "",
            )
            self._clear_retry(key)

    def route_up(self, machine):
        for key, row in self.requests.items():
            if self.states[key].get("state") not in {"handed-off", "retired"} and any(
                item["machine"] == machine for item in row["bells"]
            ):
                self._reconcile(
                    key,
                    exited=key not in self.processes
                    or (self.root / "requests" / f"{key}.complete").exists(),
                )

    def catch_up(self, _cursor):
        for path in sorted((self.root / "requests").glob("*.json")):
            self._own(path.stem)
            self._reconcile(
                path.stem,
                exited=path.stem not in self.processes
                or path.with_suffix(".complete").exists(),
            )
        return []

    def _collect_reconciled(self, ready):
        if self.worker and self.worker.signal.reader in ready:
            self.worker.signal.consume()
            while not self.worker.results.empty():
                key, result, reason = self.worker.results.get_nowait()
                self._reconciled(
                    key,
                    result,
                    exited=self.reconciling.pop(key),
                    reason=reason,
                )

    def drain(self):
        ready = set(wait_readable(self.readable_handles(), 0))
        self._collect_reconciled(ready)
        if ready.intersection(self.watch.readable_handles()):
            names = self.watch.drain()
            if names is None:
                names = {p.name for p in (self.root / "requests").iterdir()}
            keys = {name.split(".")[0] for name in names if not name.startswith(".")}
            for key in keys:
                if key not in self.requests:
                    self._own(key)
                if (self.root / "requests" / f"{key}.complete").exists():
                    self._reconcile(key, exited=True)
        for key, watch in list(self.processes.items()):
            if ready.intersection(watch.readable_handles()):
                watch.close()
                del self.processes[key]
                self._reconcile(key, exited=True)
        for machine, route in self.routes.items():
            if ready.intersection(getattr(route, "readable_handles", lambda: ())()):
                route.drain()
                self.route_up(machine)
        return []

    def recovery_blocks(self, _event):
        return False

    def close(self):
        if self.worker:
            self.worker.close()
            self.worker = None
            self.reconciling.clear()
        if self.watch:
            self.watch.close()
            self.watch = None
        for watch in self.processes.values():
            watch.close()
        self.processes.clear()
        for route in self.routes.values():
            if hasattr(route, "close"):
                route.close()
        if self.lock:
            _owner_lock(self.lock, release=True)
            self.lock.close()
            self.lock = None
