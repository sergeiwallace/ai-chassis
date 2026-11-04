"""Single-writer event dispatch: source readiness or the earliest armed deadline."""

from abc import ABC, abstractmethod
import contextlib
import json
import logging
import os
from pathlib import Path
import select
import socket
import time

from . import pass_telemetry
from .authority import FACTS
from .channel import Disposition
from .events import Event, LIFECYCLE_INPUT_KINDS, digest, write_record
from .targets import MachineTarget


class HostDispatcher(ABC):
    @abstractmethod
    def run(self):
        """Own source lifecycle and wait without a periodic timeout."""

    @abstractmethod
    def stop(self):
        """Interrupt the blocking wait, including from a signal handler."""


class WriterBusy(BlockingIOError):
    """Retained event work must retry after a bounded marker-lock acquisition."""


def wait_readable(handles, timeout):
    return select.select(handles, [], [], timeout)[0]


class WakeSignal:
    """Interrupt a native wait. POSIX pipes also work with network-denied sandboxes."""

    def __init__(self):
        self.sockets = os.name == "nt"
        if self.sockets:
            self.reader, self.writer = socket.socketpair()
            self.reader.setblocking(False)
            self.writer.setblocking(False)
        else:
            self.reader, self.writer = os.pipe()
            os.set_blocking(self.reader, False)
            os.set_blocking(self.writer, False)

    def notify(self):
        with contextlib.suppress(BlockingIOError, OSError):
            if self.sockets:
                self.writer.send(b"1")
            else:
                os.write(self.writer, b"1")

    def consume(self):
        if self.sockets:
            self.reader.recv(65536)
        else:
            os.read(self.reader, 65536)

    def close(self):
        for handle in (self.reader, self.writer):
            if self.sockets:
                handle.close()
            else:
                os.close(handle)


class Dispatcher(HostDispatcher):
    def __init__(  # noqa: PLR0913 - injected host capabilities, no global state
        self,
        state_dir,
        directory,
        writer_lock,
        *,
        sources=None,
        deadlines=None,
        admission=None,
        channels=None,
        registry=None,
        clock=time.time,
        wait=wait_readable,
        report=None,
        publish_presence=None,
        tasks=(),
        delivery=None,
        read_work=None,
    ):
        self.root = Path(state_dir)
        self.directory, self.writer_lock = directory, writer_lock
        self.sources = sources or {}
        self.disabled_sources = {}
        self.source_detail = {}
        self.deadlines, self.admission = deadlines, admission
        self.channels = channels or {}
        self.registry, self.clock, self.wait = registry, clock, wait
        self.report = report or (lambda _word: None)
        self.publish_presence = publish_presence or (lambda _record: True)
        self.status = {}
        self.obligations = {}
        self.cancelled = set()
        self.signal = WakeSignal()
        self.reader = self.signal.reader
        self.config_watch = None
        self.config_body = None
        self.started = False
        self.stopped = False
        self.failed = False
        self.cursors = {}
        self.catching_up = False
        self.catch_up_pending = set()
        self.admitting = False
        self.deferred = []
        self.fold_held = set()
        self.inbox_permissions = None
        self.permission_rechecks = {}
        self.delivery_retries = {}
        self.delivery_attempts = {}
        self.resume_pending = []
        self.resume_generation = 0
        self.resume_retry = None
        self.resume_until = None
        self.recover_restarts = lambda: None
        self.report_failed = False
        self.tasks = tasks
        self.delivery = delivery or self._channel_delivery
        from .read_work import ReadWork

        self.read_work = ReadWork() if read_work is None else read_work
        self._phase_started = None
        self._stuck = self._stuck_idle = None
        self.phase = "startup"
        self.watchdog_seconds = None
        self.lock_retry_at = None
        self.telemetry = {
            "pass_kind": "dispatch",
            "max_tick_gap_ms": 0.0,
            "watchdog_overruns": 0,
            "skipped": {},
        }
        self.last_tick = None
        self.host = os.environ.get("AI_HOST") or socket.gethostname()
        # Call-time settings, the last pass's read, the restore-page sink, resume units.
        self.read_settings = self.pass_settings = self.page = self._units = None

    @property
    def phase(self):
        return self._phase

    @phase.setter
    def phase(self, value):
        if self._phase_started is not None:
            now = time.monotonic()
            phases = self.telemetry["phases_ms"]
            phases[self._phase] = (
                phases.get(self._phase, 0.0) + (now - self._phase_started) * 1000
            )
            self._phase_started = now
        if self._stuck is not None:
            self._stuck.progress()
        self._phase = value

    @contextlib.contextmanager
    def _watch_tick(self):
        """Report an overrun on pass completion, without waking or waiting to sample."""
        if self.watchdog_seconds is None:
            yield
            return
        # One call-time config read per pass, outside the timed window.
        settings, counters, started = self._open_pass()
        at = self.clock()
        kind = "pass" if self.started else "startup"
        if self.last_tick is not None:
            self.telemetry["max_tick_gap_ms"] = max(
                self.telemetry["max_tick_gap_ms"], (started - self.last_tick) * 1000
            )
        self.last_tick = started
        bound_ms = max(0.001, self.watchdog_seconds()) * 1000
        self.telemetry["phases_ms"] = {}
        self.resume_until = self._resume_deadline()
        self._phase_started = started
        self._arm_pass(settings, at, kind)
        try:
            yield
        except WriterBusy:
            skipped = self.telemetry["skipped"]
            skipped["writer-lock"] = skipped.get("writer-lock", 0) + 1
            self._degraded("writer-lock", "timeout;work-retained")
            # Replay retained facts once this specific failed write can retry.
            # No retry exists on an idle host with no failed work.
            self.lock_retry_at = self.clock() + 1
        finally:
            # Flush the last phase, including a lock phase restored during unwind.
            self.phase = self.phase
            self._settle_pass()
            # Lifecycle delivery can reconcile subscriptions after source intake.
            # Include failures from those call sites in the same tick's telemetry.
            for name, source in self.sources.items():
                self._sync_source_detail(name, source)
            elapsed = (time.monotonic() - started) * 1000
            self.telemetry["tick_ms"] = elapsed
            phases = self.telemetry["phases_ms"]
            top = dict(sorted(phases.items(), key=lambda row: row[1], reverse=True)[:8])
            lock_wait_ms = phases.get("writer-lock-wait", 0.0)
            self.telemetry["lock_wait_ms"] = lock_wait_ms
            worst = self.telemetry.get("worst_tick")
            if worst is None or elapsed > worst["elapsed_ms"]:
                self.telemetry["worst_tick"] = {
                    "elapsed_ms": elapsed,
                    "at": at,
                    "phases_ms": top,
                    "lock_wait_ms": lock_wait_ms,
                    "kind": kind,
                    "resume_top": self._units.top(),
                }
                detail = self.source_detail.get("buzz", {})
                if detail.get("reason") == "connect-deadline":
                    self.telemetry["worst_tick"]["buzz-connect-deadline"] = dict(detail)
            if elapsed > bound_ms:
                self.telemetry["watchdog_overruns"] += 1
                phase = next(iter(top))
                summary = ",".join(
                    f"{name}:{ms:.1f}" for name, ms in list(top.items())[:3]
                )
                with contextlib.suppress(Exception):
                    self.report(
                        f"tick watchdog phase={phase} top={summary} "
                        f"lock_wait_ms={lock_wait_ms:.1f} kind={kind}"
                    )
            self._source_telemetry()
            self.telemetry["max_tick_gap_ms"] = max(
                self.telemetry["max_tick_gap_ms"], elapsed
            )
            with contextlib.suppress(OSError):
                write_record(self.root / "telemetry.json", self.telemetry)
            self._record_pass(
                {
                    "at": at,
                    "kind": kind,
                    "elapsed_ms": round(elapsed, 3),
                    "phases_ms": {name: round(ms, 3) for name, ms in phases.items()},
                    "lock_wait_ms": round(lock_wait_ms, 3),
                    "alarm_ms": bound_ms,
                    "slow": elapsed > bound_ms,
                },
                settings,
                counters,
            )

    def _open_pass(self):
        self.pass_settings = self._settings()
        return self.pass_settings, pass_telemetry.start_counters(), time.monotonic()

    def _arm_pass(self, settings, at, kind):
        self._units = pass_telemetry.ResumeUnits()
        self._stuck = pass_telemetry.StuckWatch(
            self._setting(settings, "dispatch_stuck_limit_s"),
            lambda idle: self._restore_page(
                "stuck", phase=self.phase, idle_s=round(idle, 3), pass_at=at, kind=kind
            ),
        )

    def _source_telemetry(self):
        sources = {**self.disabled_sources, **self.sources}
        self.telemetry["source_status"] = {
            name: getattr(source, "status", "ok") for name, source in sources.items()
        }
        self.telemetry["source_detail"] = dict(self.source_detail)
        self.telemetry["doorbell_detail"] = {
            identity: dict(detail)
            for source in sources.values()
            for identity, detail in getattr(source, "doorbell_detail", {}).items()
        }

    def _settle_pass(self):
        """End phase timing and disarm the stuck deadline before any other unwind work."""
        self._phase_started = None
        self.resume_until = None
        self._stuck_idle = self._stuck.close()
        self._stuck = None

    def _record_pass(self, row, settings, counters):
        stuck_idle, self._stuck_idle = self._stuck_idle, None
        units, self._units = self._units, None
        row = {
            "schema": pass_telemetry.SCHEMA,
            "host": self.host,
            "pid": os.getpid(),
            "obligations": len(self.obligations),
            "resume_top": units.top(),
            "resume_units": units.count,
            **row,
        }
        if row["slow"]:
            # Recorded for review only; the alarm never interrupts or restores.
            row["snapshot"] = pass_telemetry.host_snapshot(
                counters, verdict_path=settings.get("dispatch_contention_verdict_path")
            )
        if stuck_idle is not None:
            row["stuck"] = {"reason": "stuck", "idle_s": round(stuck_idle, 3)}
            self._restore_page(
                "recovered", elapsed_ms=row["elapsed_ms"], pass_at=row["at"]
            )
        self._append_pass(row, settings)

    def _settings(self):
        if self.read_settings is not None:
            return self.read_settings()
        return pass_telemetry.call_time_settings()

    @staticmethod
    def _setting(settings, name):
        from ai_chassis.agent_mail_notifier import _dispatch_bound

        return _dispatch_bound(settings, name)

    def _append_pass(self, row, settings):
        with contextlib.suppress(OSError):
            pass_telemetry.append_row(
                self.root / pass_telemetry.PASS_LOG,
                row,
                self._setting(settings, "dispatch_pass_log_max_bytes"),
            )

    def _restore_page(self, reason, **detail):
        """The only restore page: stuck, crash-looping or dead; never a slow pass."""
        record = {
            "schema": pass_telemetry.SCHEMA,
            "reason": reason,
            "host": self.host,
            "pid": os.getpid(),
            "at": self.clock(),
            **detail,
        }
        try:
            if self.page is not None:
                self.page(record)
                return
            pass_telemetry.append_row(
                self.root / pass_telemetry.RESTORE_PAGES, record, 1 << 20
            )
            self.report(
                f"wake restore-page reason={reason} "
                + " ".join(f"{key}={value}" for key, value in sorted(detail.items()))
            )
        except Exception:
            with contextlib.suppress(Exception):
                logging.getLogger(__name__).error("dispatcher:restore-page-failed")

    def _record_process_start(self):
        settings = self._settings()
        now = self.clock()
        row = {
            "schema": pass_telemetry.SCHEMA,
            "kind": "process-start",
            "host": self.host,
            "pid": os.getpid(),
            "at": now,
        }
        with contextlib.suppress(OSError, ValueError, IndexError):
            row["proc_start"] = pass_telemetry.process_start_ticks(os.getpid())
        self._append_pass(row, settings)
        starts = pass_telemetry.recent_starts(
            pass_telemetry.read_rows(self.root / pass_telemetry.PASS_LOG),
            now,
            self._setting(settings, "dispatch_crash_loop_window_s"),
        )
        if len(starts) >= self._setting(settings, "dispatch_crash_loop_starts"):
            self._restore_page("crash-loop", starts=len(starts))

    @contextlib.contextmanager
    def _in_phase(self, name):
        previous = self.phase
        self.phase = name
        try:
            yield
        finally:
            self.phase = previous

    @staticmethod
    def _channel_delivery(channel, session, event):
        return channel.deliver(session, [event])

    @staticmethod
    def read_deadline(name):
        from ai_chassis.agent_mail_notifier import _dispatch_bound

        # Still call-time: the stat-keyed reader re-parses only after a config edit.
        return _dispatch_bound(pass_telemetry.call_time_settings(), name)

    def resume_command_deadline(self):
        """Every lifecycle child shares the remainder of this resume pass."""
        remaining = (
            self.read_deadline("dispatch_resume_budget_s")
            if self.resume_until is None
            else self.resume_until - time.monotonic()
        )
        if remaining <= 0:
            raise ValueError("command-deadline")
        return min(5.0, remaining)

    def _resume_deadline(self):
        """The resume budget's end, timed from AFTER the budget is read (ISSUE-REF).

        The first read in a process imports the notifier and reads its config; on a loaded host
        that outlasted the 1 s budget, so a clock started first deferred every due delivery.
        """
        budget = self.read_deadline("dispatch_resume_budget_s")
        return time.monotonic() + budget

    def _restore_degraded(self, name, error):
        self.status[name] = f"degraded:{error}"

    def _degraded(self, name, error):
        self._restore_degraded(name, error)
        try:
            self.report(f"{name}:{self.status[name]}")
            return True
        except Exception:
            if not self.report_failed:
                self.report_failed = True
                with contextlib.suppress(Exception):
                    logging.getLogger(__name__).error("dispatcher:reporter-failed")
            return False

    def _notify(self):
        self.signal.notify()

    def stop(self):
        self.stopped = True
        self._notify()

    def _load_plugins(self):
        from .deadlines import PersistentDeadlineSet

        context = {
            "state_dir": self.root,
            "intake_dir": self.root / "intake",
            "dispatcher": self,
            "directory": self.directory,
        }
        self.sources = self.registry.load("sources", **context)
        from . import producers

        common_owner = (
            None
            if getattr(self.registry, "outbox_owned_externally", False)
            else producers.owner(self.registry, self.root / "doorbells")
        )
        if common_owner is not None:
            self.sources["producer-outbox"] = common_owner
        admissions = self.registry.load("admissions", **context)
        self.admission = admissions.get("default")
        self.channels = {
            Disposition(name): value
            for name, value in self.registry.load("channels", **context).items()
            if name in set(Disposition)
        }
        loaded = self.registry.load("deadlines", state_dir=self.root / "deadlines")
        self.deadlines = loaded.get("default") or PersistentDeadlineSet(
            self.root / "deadlines"
        )
        # Loaded for future producer legs without changing the dispatcher contract.
        self.routes = self.registry.load("routes", **context)
        self.handlers = self.registry.load("doorbells", **context)
        if producers.enabled(self.registry):
            from .routes.local import DoorbellSource

            self.sources["doorbells"] = DoorbellSource(
                self.root / "doorbells",
                self,
                self.handlers,
                producers.secret(self.registry),
            )
        self.status.update(self.registry.status)

    def start(self):
        if self.started:
            return
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.registry:
            self.phase = "plugins"
            self._load_plugins()
            from .sources.intake import WatchUnavailable, native_watch

            try:
                self.phase = "config-watch"
                self.config_watch = native_watch(self.registry.path.parent)
                self._track_config()
                self.config_body = self.registry.path.read_bytes()
            except Exception as exc:
                if isinstance(exc, WatchUnavailable):
                    self.source_detail["config-watch"] = exc.detail
                self._degraded(
                    "config-watch", getattr(exc, "reason", type(exc).__name__)
                )
        if self.deadlines is None:
            raise ValueError("missing-deadlines")
        with self._in_phase("bind-deadlines"):
            self._bind_deadlines()
        with self._in_phase("restore"):
            self._restore()
        self.started = True
        if self.inbox_permissions is not None:
            with self._in_phase("permissions-open"):
                self.inbox_permissions.open()
        # All terminal/source catch-up precedes overdue effects.
        self._open_sources()
        from . import launches

        with self._in_phase("launches-check"), self.writer_lock():
            launches.check(self, startup=True)
        with self._in_phase("hold-reevaluate"):
            self.reevaluate_inbox_holds(startup=True)

    def _bind_deadlines(self):
        self.deadlines.changed = self._notify
        self.deadlines.cancelling = self._cancelling_deadline
        self.deadlines.eligible = self._recovery_ready
        self.delivery_retries = {
            event.key: event
            for row in self.deadlines.entries.values()
            if (event := Event.from_record(row["event"])).source == "delivery-retry"
        }
        self.delivery_attempts = {
            key: int(event.revision) for key, event in self.delivery_retries.items()
        }
        self.resume_retry = next(
            (
                Event.from_record(row["event"])
                for row in self.deadlines.entries.values()
                if row["event"]["source"] == "resume-batch"
            ),
            None,
        )
        if self.resume_retry is not None and self.resume_retry.revision.isdigit():
            # A restarted process must number past the restored continuation, or
            # acknowledging it would delete a successor that reused its identity.
            self.resume_generation = max(
                self.resume_generation, int(self.resume_retry.revision)
            )
        for source in self.sources.values():
            bind = getattr(source, "bind_deadlines", None)
            if bind is not None:
                bind(self.deadlines, self.clock)

    def _recovery_ready(self, event):
        if event.source == "delivery-retry" and event.key in self.obligations:
            event = Event.from_record(self.obligations[event.key]["event"])
        if self.catching_up or (
            event.target in self.fold_held and event.source in LIFECYCLE_INPUT_KINDS
        ):
            return False
        for name in self.catch_up_pending:
            source = self.disabled_sources.get(name, self.sources.get(name))
            if source is None or source.recovery_blocks(event):
                return False
        return True

    @contextlib.contextmanager
    def _cancelling_deadline(self, key, event):
        with self.writer_lock():
            # A terminal source may replay before the source that created the work.
            # Journal the key even if its obligation has not been replayed yet.
            for identity in {key, event.identity if event is not None else key}:
                write_record(
                    self.root / "cancellations" / f"{digest(identity)}.json",
                    {"key": identity},
                )
                self.cancelled.add(identity)
            record = self.obligations.get(key)
            if record is None and event is not None:
                record = self.obligations.get(event.identity)
            if record is None and event is not None:
                record = {
                    "event": event.as_record(),
                    "state": "pending",
                    "delivered": [],
                }
            if record is not None and record["state"] not in {"delivered", "cancelled"}:
                self._save_obligation({**record, "state": "cancelled"})
            if record is not None:
                for channel in self.channels.values():
                    refresh = getattr(channel, "refresh", None)
                    if refresh is not None:
                        refresh(Event.from_record(record["event"]))
            yield

    def cancel(self, key):
        """Persist a terminal obligation before removing its armed deadline."""
        with self._cancelling_deadline(key, None):
            self.deadlines.cancel(key)
            self._clear_delivery_retry(key)

    def _arm_delivery_retry(self, event, reason):
        if reason == "evidence-read-ready" and self.obligations.get(
            event.identity, {}
        ).get("state") in {"delivered", "cancelled"}:
            return
        if (
            reason not in {"command-deadline", "evidence-read-ready"}
            or event.identity in self.delivery_retries
        ):
            return
        attempt = self.delivery_attempts.get(event.identity, 0) + 1
        retry = Event(
            "delivery-retry",
            event.identity,
            str(attempt),
            event.target,
            deadline=self.clock()
            + (
                0
                if reason == "evidence-read-ready"
                else (5, 15, 60)[min(attempt, 3) - 1]
            ),
        )
        self.deadlines.arm(retry.identity, retry.deadline, retry)
        self.delivery_retries[event.identity] = retry
        self.delivery_attempts[event.identity] = attempt

    def _clear_delivery_retry(self, key):
        retry = self.delivery_retries.pop(key, None)
        if retry is not None:
            self.deadlines.acknowledge(retry.identity)
        self.delivery_attempts.pop(key, None)

    def _resume_obligations(self, directory=None):
        with self._in_phase("resume-obligations"), self.writer_lock():
            for channel in self.channels.values():
                reconcile = getattr(channel, "reconcile", None)
                if reconcile is not None:
                    reconcile()
            for key in self.cancelled:
                self.deadlines.acknowledge(key)
            self._resume_batch(list(self.obligations), directory)
        self._recheck_permission_signals()

    def _restore(self):
        for path in sorted((self.root / "cancellations").glob("*.json")):
            try:
                key = json.loads(path.read_text())["key"]
                if not isinstance(key, str) or digest(key) != path.stem:
                    raise ValueError("cancellation-identity")
                self.cancelled.add(key)
            except (OSError, ValueError, KeyError, TypeError):
                self._degraded(path.name, "invalid-cancellation")
        for path in sorted((self.root / "cursors").glob("*.json")):
            try:
                record = json.loads(path.read_text())
                if digest(record["source"]) != path.stem:
                    raise ValueError("cursor-identity")
                self.cursors[record["source"]] = record["cursor"]
            except (OSError, ValueError, KeyError, TypeError):
                self._degraded(path.name, "invalid-cursor")
        for path in sorted((self.root / "obligations").glob("*.json")):
            try:
                record = json.loads(path.read_text())
                event = Event.from_record(record["event"])
                if event.identity != path.stem:
                    raise ValueError("obligation-identity")
                self.obligations[event.identity] = record
            except (OSError, ValueError, KeyError, TypeError):
                self._degraded(path.name, "invalid-obligation")
        self._held_status()

    def _track_config(self):
        track = getattr(self.config_watch, "track", None)
        if track and self.registry.path.is_file():
            track(self.registry.path)

    def _open_sources(self):
        self.catching_up = True
        self.catch_up_pending = set(self.sources)
        # Subscribe to every source before replaying any of them.
        for name, source in list(self.sources.items()):
            try:
                with self._in_phase("source-open:" + name):
                    source.open()
            except Exception as exc:
                self._disable_source(name, exc)
        self._collect_sources(list(self.sources), replay=True)

    def reconnect(self, name):
        """Transport/power events request one catch-up for the affected source."""
        source = self.sources.get(name) or self.disabled_sources[name]
        self.catching_up = True
        self.catch_up_pending.add(name)
        try:
            source.close()
            with self._in_phase("source-open:" + name):
                source.open()
            self.sources[name] = source
            self.disabled_sources.pop(name, None)
        except Exception as exc:
            self._disable_source(name, exc)
        self._collect_sources([name] if name in self.sources else [], replay=True)

    def _collect_sources(self, names, *, replay=False):
        """Every source hands off facts before the common reducer releases effects."""
        # Startup/catch-up and native source readiness retry durable completion
        # signals. Recovery may acquire run locks; never run it under writer_lock.
        try:
            with self._in_phase("restart-recovery"):
                self.recover_restarts()
        except Exception as exc:
            self._degraded("restart-recovery", type(exc).__name__)
        self.catching_up = True
        try:
            for name in names:
                self.phase = ("replay:" if replay else "source:") + name
                source = self.sources[name]
                try:
                    events = (
                        source.catch_up(self.cursors.get(name))
                        if replay
                        else source.drain()
                    )
                    self.accept(events)
                    self._commit_source(name, source, events)
                    self._source_collected(name, source)
                except WriterBusy:
                    # Native intake has already retained its records. Replay it
                    # after contention; disabling it would lose the next input.
                    raise
                except Exception as exc:
                    self._disable_source(name, exc)
        finally:
            self.catching_up = False
        self._recovery_status()
        self._resume_obligations(frozenset(self.directory()))

    def _sync_source_detail(self, name, source):
        self.status[name] = getattr(source, "status", "ok")
        detail = getattr(source, "detail", None)
        if detail:
            self.source_detail[name] = dict(detail)
        else:
            self.source_detail.pop(name, None)

    def _source_collected(self, name, source):
        self._sync_source_detail(name, source)
        if getattr(source, "recovery_complete", True):
            self.catch_up_pending.discard(name)
        else:
            self.catch_up_pending.add(name)

    def _recovery_status(self):
        if self.catch_up_pending:
            self._degraded("recovery", "source-catch-up-incomplete")
        else:
            self.status["recovery"] = "ok"

    def _commit_source(self, name, source, events):
        with self.writer_lock():
            cursor = source.commit(events)
            if cursor is not None:
                write_record(
                    self.root / "cursors" / f"{digest(name)}.json",
                    {"source": name, "cursor": cursor},
                )
                self.cursors[name] = cursor

    def _disable_source(self, name, exc):
        from .sources.intake import WatchUnavailable

        if isinstance(exc, WatchUnavailable):
            self.source_detail[name] = exc.detail
        self._degraded(name, getattr(exc, "reason", type(exc).__name__))
        source = self.sources.pop(name, None)
        if source is None:
            source = self.disabled_sources.get(name)
        if source:
            self.disabled_sources[name] = source
            self.catch_up_pending.add(name)
            with contextlib.suppress(Exception):
                source.close()

    def _save_obligation(self, record):
        event = Event.from_record(record["event"])
        write_record(self.root / "obligations" / f"{event.identity}.json", record)
        self.obligations[event.identity] = record
        self._held_status()

    def _held_status(self):
        from . import launches

        launches.status(self)
        self.status["git-inbox-held"] = [
            {
                "identity": identity,
                "recipient": row["recipient"],
                "sender": row["sender"],
                "since": row["since"],
            }
            for identity, row in self.obligations.items()
            if row["state"] == "held"
        ]

    def reevaluate_inbox_holds(self, recipients=None, *, startup=False):
        """The only release gate: startup, recipient binding/evidence, or config watch."""
        gate = self.inbox_permissions
        if gate is None or (gate.failed and not startup):
            return
        # Source actions may await a bounded spawn receipt. Collect them before
        # acquiring the ledger lock, just as normal source intake does.
        released = [] if startup else self._reevaluate_inbox_doorbells()
        with self.writer_lock():
            for key, row in list(self.obligations.items()):
                if row["state"] != "held":
                    continue
                if recipients is not None and row["recipient"] not in recipients:
                    continue
                if key in self.cancelled or gate.held(row["recipient"], row["sender"]):
                    continue
                self._save_obligation({**row, "state": "pending"})
                released.append(key)
            if not self.catching_up:
                self._resume_batch(released)
        self._recheck_permission_signals()

    def _reevaluate_inbox_doorbells(self):
        identities = []
        collecting = self.catching_up
        self.catching_up = True
        try:
            for name, source in list(self.sources.items()):
                recheck = getattr(source, "reevaluate_inbox", None)
                if recheck is None:
                    continue
                try:
                    events = recheck()
                    self.accept(events)
                    self._commit_source(name, source, events)
                    identities.extend(event.identity for event in events)
                except Exception as exc:
                    self._disable_source(name, exc)
        finally:
            self.catching_up = collecting
        return identities

    def _recheck_permission_signals(self):
        if self.admitting or not self.permission_rechecks:
            return  # Recursive intake defers source actions until the outer ledger unlocks.
        recipients = set()
        directory = frozenset(self.directory())
        for key, event in list(self.permission_rechecks.items()):
            state = self.obligations[key]["state"]
            if state not in {"delivered", "cancelled"}:
                continue
            del self.permission_rechecks[key]
            if state == "delivered":
                recipients.update(s.slug for s in event.target.resolve(directory))
        if recipients:
            self.reevaluate_inbox_holds(recipients)

    def accept(self, events):
        """Durable receiving handoff; a channel failure leaves replayable pending work."""
        with self.writer_lock():
            identities = []
            for event in events:
                record = self.obligations.get(event.identity)
                if record is None:
                    record = {
                        "event": event.as_record(),
                        "state": "pending",
                        "delivered": [],
                    }
                    # Classify the whole received batch before any aggregate
                    # snapshot can expose another newly received inbox event.
                    if not self._hold_inbox(record, event):
                        self._save_obligation(record)
                    if event.source in {"session-start", "session-evidence"}:
                        self.permission_rechecks[event.identity] = event
                identities.append(event.identity)
            if not self.catching_up:
                self._resume_batch(identities)
        self._recheck_permission_signals()

    def _resume_batch(self, identities, directory=None):
        """Fold durable lifecycle authority before effects, including recursive intake."""
        self.deferred.extend(identities)
        if self.admitting or not self.deferred:
            return
        self.admitting = True
        previous_until = self.resume_until
        until = previous_until
        if until is None:
            until = self._resume_deadline()
        self.resume_until = until
        try:
            if time.monotonic() >= until:
                self._defer_resume(self.deferred)
                self.deferred = []
                return
            while self.deferred:
                batch, self.deferred = list(dict.fromkeys(self.deferred)), []
                targets = {
                    Event.from_record(self.obligations[key]["event"]).target
                    for key in batch
                }
                # Include facts retained across a partial fold/crash, but touch
                # only the targets participating in this admission.
                events = [
                    Event.from_record(row["event"])
                    for row in self.obligations.values()
                    if (
                        row["state"] not in {"delivered", "cancelled"}
                        or row["event"]["source"] in FACTS
                    )
                    and Event.from_record(row["event"]).target in targets
                ]
                if directory is None:
                    with self._resume_unit({"unit": "directory"}):
                        directory = frozenset(self.directory())
                with self._resume_unit(
                    {"unit": "fold", "targets": len(targets), "events": len(events)}
                ):
                    self._fold_batch(targets, events, directory)
                for index, key in enumerate(batch):
                    if index and time.monotonic() >= until:
                        self._defer_resume([*batch[index:], *self.deferred])
                        self.deferred = []
                        return
                    if self.deferred:
                        # A recursive admission invalidates the completed fold.
                        # Refold its authority with all remaining work before
                        # another handler can observe the old watermark.
                        self.deferred.extend(batch[index:])
                        break
                    # Folding can cancel a record that was in the original batch.
                    record = self.obligations[key]
                    if record["state"] in {"delivered", "cancelled"}:
                        self.deadlines.acknowledge(key)
                        self._clear_delivery_retry(key)
                    else:
                        with self._resume_unit(
                            {
                                "unit": "resume",
                                "identity": key,
                                "source": record["event"]["source"],
                                "state": record["state"],
                            }
                        ):
                            directory = self._resume_in_directory(record, directory)
                from . import launches

                with self._resume_unit(
                    {"unit": "launches-check-batch", "batch": len(batch)}
                ):
                    launches.check_batch(self, batch)
                # Recursive admission may have registered a new session.
                directory = None
        finally:
            self.resume_until = previous_until
            self.admitting = False

    @contextlib.contextmanager
    def _resume_unit(self, unit):
        """Time one resume unit, so a slow pass names the obligation that held it."""
        started = time.monotonic()
        try:
            yield
        finally:
            if self._units is not None:
                self._units.add((time.monotonic() - started) * 1000, unit)
            if self._stuck is not None:
                self._stuck.progress()

    def _defer_resume(self, identities):
        self.resume_pending = list(dict.fromkeys([*self.resume_pending, *identities]))
        if self.resume_retry is not None:
            return
        self.resume_generation += 1
        event = Event(
            "resume-batch",
            "dispatcher",
            str(self.resume_generation),
            MachineTarget("local"),
            deadline=self.clock(),
        )
        self.deadlines.arm(event.identity, event.deadline, event)
        self.resume_retry = event

    def _fold_batch(self, targets, events, directory):
        for target in targets:
            for channel in self.channels.values():
                fold = getattr(channel, "fold", None)
                if fold is not None:
                    receipt = fold(
                        [event for event in events if event.target == target], directory
                    )
                    if not receipt.accepted:
                        self.fold_held.add(target)
                        self._degraded("lifecycle-fold", receipt.reason)
                        break
            else:
                self.fold_held.discard(target)

    def _resume_in_directory(self, record, directory):
        self._resume(record, directory)
        if self.obligations[Event.from_record(record["event"]).identity][
            "state"
        ] == "delivered" and record["event"]["source"] in {
            "session-start",
            "session-evidence",
            "session-end",
            "process-exit",
        }:
            # Registration/retirement can change eligibility during delivery.
            # Ordinary effects share the batch snapshot.
            return frozenset(self.directory())
        return directory

    def _resume(self, record, directory=None):
        event = Event.from_record(record["event"])
        if event.identity in self.cancelled:
            self._save_obligation({**record, "state": "cancelled"})
            self.deadlines.acknowledge(event.identity)
            return
        if record["state"] in {
            "held",
            "launch-reserved",
            "launch-waiting",
            "launch-failed",
        }:
            return
        recovering = not self._recovery_ready(event)
        if event.deadline is not None and (event.deadline > self.clock() or recovering):
            self.deadlines.arm(event.identity, event.deadline, event)
            self._save_obligation({**record, "state": "scheduled"})
            return
        if recovering:
            self._save_obligation(
                {**record, "state": "pending", "reason": "source-catch-up-incomplete"}
            )
            return
        if not self._hold_inbox(record, event) and not self._replay_terminal(
            record, event
        ):
            self._deliver(record, event, directory)
        self.deadlines.acknowledge(event.identity)

    def _hold_inbox(self, record, event):
        if event.source != "git-inbox" or event.sender is None:
            return False
        gate = self.inbox_permissions
        recipient = record.get("launch", {}).get("recipient", event.target.value)
        if gate is not None and not gate.held(recipient, event.sender):
            return False
        held = {
            **record,
            "state": "held",
            "recipient": recipient,
            "sender": event.sender,
            "since": record.get("since", self.clock()),
            "reason": "sender-permission",
        }
        self._save_obligation(held)
        self.report(f"git-inbox-held:{recipient}:{event.sender}:{held['since']}")
        return True

    def _replay_terminal(self, record, event):
        for channel in self.channels.values():
            replay = getattr(channel, "replay_terminal", None)
            receipt = replay(record, event) if replay is not None else None
            if receipt is not None:
                self._save_obligation(
                    {
                        **record,
                        "state": "delivered" if receipt.accepted else "pending",
                        "reason": receipt.reason,
                    }
                )
                return True
        return False

    def _retain_terminal(self, record, event, session):
        if event.source in {"process-exit", "session-end"}:
            # Retain the exact SID before a terminal channel excludes it from
            # the live directory. Only its publication can replay.
            record = {
                **record,
                "terminal_session": {
                    "slug": session.slug,
                    "session_id": session.session_id,
                    "machine": session.machine,
                },
            }
            self._save_obligation(record)
        return record

    def _targets(self, record, event, directory):
        if "launch" in record:
            from .launches import targets as launch_targets

            return launch_targets(self, record, directory)
        return event.target.resolve(directory) & directory

    def _deliver(self, record, event, directory=None):
        directory = frozenset(self.directory()) if directory is None else directory
        targets = self._targets(record, event, directory)
        if not targets:
            self._save_obligation(
                {**record, "state": "undelivered", "reason": "no-target"}
            )
            return
        delivered = set(record["delivered"])
        for session in sorted(targets, key=lambda s: s.slug):
            if session.slug in delivered:
                continue
            admit = getattr(self.admission, "admit", self.admission)
            if admit is None:
                self._save_obligation(
                    {**record, "state": "pending", "reason": "admission-unavailable"}
                )
                return
            disposition = admit(event, session)
            if self.deferred:
                # Admission can discover new facts after the batch fold. Retain
                # this delivery until the same reducer has consumed that handoff.
                self.accept([event])
                self._save_obligation(
                    {
                        **record,
                        "state": "pending",
                        "reason": "lifecycle-refold-required",
                    }
                )
                return
            if (
                isinstance(event.target, MachineTarget) or event.scope == "machine"
            ) and disposition == Disposition.WAKE:
                disposition = Disposition.PASSIVE
            if disposition != Disposition.DROP:
                channel = self.channels.get(disposition)
                if channel is None:
                    self._save_obligation(
                        {**record, "state": "pending", "reason": "channel-unavailable"}
                    )
                    return
                record = self._retain_terminal(record, event, session)
                with self._in_phase("deliver:" + event.source):
                    receipt = self.delivery(channel, session, event)
                # The snapshot channel durably records first prompt admission.
                record = self.obligations[event.identity]
                if not receipt.accepted:
                    self._degraded(
                        f"delivery:{session.slug}:{event.identity}", receipt.reason
                    )
                    self._save_obligation(
                        {
                            **record,
                            "state": "launch-failed"
                            if "launch" in record
                            and receipt.reason.startswith("launch-prompt:")
                            else "pending",
                            "reason": receipt.reason,
                        }
                    )
                    self._arm_delivery_retry(event, receipt.reason)
                    return
            self.status.pop(f"delivery:{session.slug}:{event.identity}", None)
            delivered.add(session.slug)
            record = {**record, "delivered": sorted(delivered)}
            self._save_obligation(record)
        self._save_obligation({**record, "state": "delivered"})
        self._clear_delivery_retry(event.identity)

    def _reload(self):
        self.config_watch.drain()
        self._track_config()
        body = self.registry.path.read_bytes()
        if body == self.config_body:
            return
        self.config_body = body
        for source in self.sources.values():
            source.close()
        self.disabled_sources.clear()
        self._load_plugins()
        self._bind_deadlines()
        if self.inbox_permissions is not None:
            self.inbox_permissions.open()
        self._open_sources()

    def _permission_handles(self):
        return (
            self.inbox_permissions.readable_handles() if self.inbox_permissions else ()
        )

    def _collect_ready(self, ready, sources_by_handle, config_handles):
        """Detect watch failure before lifecycle release; fold sources before effects."""
        permission_changed = (
            self.inbox_permissions.drain(ready) if self.inbox_permissions else False
        )
        if set(ready) & set(config_handles):
            self._reload()
            # The handles above belong to the old config. Rebuild on the next wait.
            sources_by_handle = {}
        names = dict.fromkeys(
            sources_by_handle[h] for h in ready if h in sources_by_handle
        )
        if names:
            self._collect_sources(names)
        if permission_changed:
            self.reevaluate_inbox_holds()

    def step(self):
        due = min(
            (
                at
                for at in [
                    self.deadlines.next_due(),
                    self.lock_retry_at,
                    *(t.next_due() for t in self.tasks),
                ]
                if at is not None
            ),
            default=None,
        )
        timeout = None if due is None else max(0.0, due - self.clock())
        sources_by_handle = {
            handle: name
            for name, source in self.sources.items()
            for handle in source.readable_handles()
        }
        permission_handles = self._permission_handles()
        task_handles = [h for task in self.tasks for h in task.readable_handles()]
        if self.read_work:
            task_handles.extend(self.read_work.readable_handles())
        config_handles = (
            self.config_watch.readable_handles() if self.config_watch else ()
        )
        try:
            ready = self.wait(
                [
                    self.reader,
                    *sources_by_handle,
                    *config_handles,
                    *permission_handles,
                    *task_handles,
                ],
                timeout,
            )
        except Exception as exc:
            self._degraded("wait", type(exc).__name__)
            self.failed = True
            self.stop()
            return
        with self._watch_tick():
            self._step_ready(ready, sources_by_handle, config_handles)

    def _step_ready(self, ready, sources_by_handle, config_handles):
        self.phase = "collect"
        if self.reader in ready:
            self.signal.consume()
        if self.stopped:
            return
        self._collect_ready(ready, sources_by_handle, config_handles)
        if self.read_work:
            self.read_work.step(ready)
        if self.lock_retry_at is not None and self.clock() >= self.lock_retry_at:
            self.lock_retry_at = None
            self._collect_sources(list(self.sources), replay=True)
        for task in self.tasks:
            self.phase = "task:" + getattr(task, "name", type(task).__name__)
            task.step(ready, self.clock())
        self.phase = "deadlines"
        try:
            self._expire_due()
        except WriterBusy:
            raise
        except Exception as exc:
            self._degraded("deadline", type(exc).__name__)
            self.failed = True
            self.stop()

    def _expire_due(self):
        with self.writer_lock():
            due_events = list(self.deadlines.pop_due(self.clock()))
        until = self.resume_until
        if until is None and due_events:
            until = self._resume_deadline()
        for index, (key, event) in enumerate(due_events):
            if index and time.monotonic() >= until:
                # The tick budget is spent. Unexpired deadlines stay persisted
                # and due, so the next wait returns at once and fires them.
                return
            # Source expiry can replay an inbox launch; it owns its ledger
            # locks and must never await supervisor spawn under this lock.
            self._expire(event)
            with self.writer_lock():
                self.deadlines.acknowledge(key)

    def _expire(self, event):
        if event.source == "resume-batch":
            self.resume_retry = None
            pending, self.resume_pending = self.resume_pending, []
            with self.writer_lock():
                self._resume_batch(pending)
            return
        if event.source == "delivery-retry":
            current = self.delivery_retries.get(event.key)
            if current is None or current.identity != event.identity:
                return
            self.delivery_retries.pop(event.key, None)
            with self.writer_lock():
                if event.key in self.obligations:
                    self._resume_batch([event.key])
            return
        for name, source in list(self.sources.items()):
            if getattr(source, "expire", lambda _event: False)(event):
                self._source_collected(name, source)
                self._recovery_status()
                self._resume_obligations()
                return
        self.accept([event])

    def run(self):
        try:
            if self.watchdog_seconds is not None:
                self._record_process_start()
            with self._watch_tick():
                self.start()
            while not self.stopped:
                self.step()
            return int(self.failed)
        finally:
            self.close()

    def close(self):
        if self.read_work:
            self.read_work.close()
        for task in self.tasks:
            task.close()
        for source in self.sources.values():
            source.close()
        self.sources.clear()
        self.disabled_sources.clear()
        if self.config_watch:
            self.config_watch.close()
            self.config_watch = None
        if self.inbox_permissions is not None:
            self.inbox_permissions.close()
        self.signal.close()
