"""One evidence transition per bound compact attempt; no readiness backstop."""

import contextlib
import json
import os
from pathlib import Path

from ai_chassis import context_supervisor as supervisor

from ..events import Event, digest
from ..authority import Authority, FIELDS, CANCELLATIONS
from .. import sessions
from ..read_work import ReadPending
from .transcript import read_records, stop_acceptance, observe_landing
from . import fifo
from . import continuation


def completion_proved(record, path):  # noqa: PLR0911 - fail-closed evidence predicates
    session_id = record.get("session_id") or record.get("transcript_session_id")
    since = record.get("sent_at") or record.get("started_at")
    if not session_id or not isinstance(since, int) or isinstance(since, bool):
        return False
    path = Path(path)
    if path.stem != session_id or "subagents" in path.parts:
        return False
    try:
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > supervisor._transcript_tail_bytes():
                handle.seek(size - supervisor._transcript_tail_bytes())
                handle.readline()
            raw = handle.read()
    except OSError:
        return False
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            return False
        if not isinstance(row, dict):
            return False
        stamp = supervisor._transcript_timestamp(row.get("timestamp"))
        if (
            row.get("subtype") == "compact_boundary"
            and not row.get("isSidechain")
            and row.get("sessionId", session_id) == session_id
            and stamp is not None
            and stamp >= since
        ):
            return True
    return False


class CompactHandler:
    def __init__(self, state_dir, dispatcher, *, store=None, pane=None):
        self.root = Path(state_dir)
        self.authority = Authority(state_dir)
        self.dispatcher = dispatcher
        self.store = store or supervisor.ResumeStore()
        self.pane = pane
        self.notifications = set()

    def handle(self, event, session):
        if not supervisor._enabled() or not supervisor._enrolled(session.slug):
            return
        state = self.authority.observe([event], session)
        self.fold(state, session)
        if not self.authority.allows(event, state) or event.source in CANCELLATIONS:
            return
        with self._locked(session):
            for record in self.store.read_attempts(session.slug):
                if (
                    record.get("session_id") or record.get("transcript_session_id")
                ) != session.session_id:
                    continue
                self._advance(event, session, record)

    @staticmethod
    def _wait_for_lock(path):
        # Wait on one real holder, off the pass. No probe loop or timed retry.
        # The writer has already created the file; this worker never creates it
        # or updates attempt/obligation state.
        fd = os.open(path, os.O_RDWR)
        try:
            supervisor._lock_exclusive(fd)
            supervisor._unlock(fd)
        finally:
            os.close(fd)

    def _lock_ready(self, session):
        pending = [
            key
            for key, row in self.dispatcher.obligations.items()
            if row["state"] not in {"delivered", "cancelled"}
            and session in Event.from_record(row["event"]).target.resolve({session})
        ]
        if pending:
            # A fold can be held on this lock. A resume continuation refolds
            # authority before delivery; a delivery-retry would be ineligible
            # behind that very fold hold.
            self.dispatcher._defer_resume(pending)

    @contextlib.contextmanager
    def _locked(self, session):
        work = self.dispatcher.__dict__.get("read_work")
        if not work:
            with self.store.locked():
                yield
            return
        self.store.initialise()
        path = self.store.root / "supervisor.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        acquired = False
        try:
            acquired = supervisor._try_lock_exclusive(fd)
            if acquired:
                yield
                return
        finally:
            if acquired:
                supervisor._unlock(fd)
            os.close(fd)
        key = "compact-lock:" + str(path)

        def waiting():
            return work.call(
                key,
                "released",
                lambda: self._wait_for_lock(path),
                (key, session),
                lambda: self._lock_ready(session),
            )

        waiting()  # Raises ReadPending until this specific contention ends.
        work.forget(key, "released")
        # Another writer won the lock after the previous completion. Wait for
        # that holder; never spin on the pass or reuse stale lock readiness.
        waiting()
        raise ReadPending()

    def _hold(self, record, reason):
        if record.get("event_hold") != reason:
            return self.store.update(record, record["state"], event_hold=reason)
        return record

    def fold(self, state, session):
        """Project the common SID hold, including onto newly created attempts."""
        if isinstance(state, Event):
            state = self.authority.observe([state], session)
        if (
            (state.get("ambiguous") and not state.get("lifecycle_ended"))
            or not supervisor._enabled()
            or not supervisor._enrolled(session.slug)
        ):
            return
        with self._locked(session):
            for record in self.store.read_attempts(session.slug):
                if (
                    record.get("session_id") or record.get("transcript_session_id")
                ) != session.session_id:
                    continue
                fields = {k: v for k, v in state.items() if k in FIELDS}
                if fields.get("cancellation") != record.get(
                    "cancellation"
                ) and fields.get("lifecycle_event") != record.get("lifecycle_event"):
                    record = self._cancel_quiet(record)
                    fields["event_pane_sample"] = None
                status = record["state"]
                if status not in supervisor.TERMINAL_STATES:
                    if state.get("lifecycle_ended"):
                        status = "SESSION_ENDED"
                        fields["terminal_at"] = int(self.dispatcher.clock())
                    elif (
                        state.get("event_turn_active")
                        and record.get("attempt_type") == "continuation"
                        and status in {"BOUND", "STAGED_UNVERIFIED"}
                    ):
                        status = "CONTINUATION_AMBIGUOUS"
                        fields["reason"] = "human-prompt"
                if status != record["state"] or any(
                    record.get(k) != v for k, v in fields.items()
                ):
                    self.store.update(record, status, **fields)

    def _advance(self, event, session, record):  # noqa: C901, PLR0911, PLR0912, PLR0915 - one transition per persisted lifecycle state
        if (
            event.source == "compact-quiet"
            and record.get("event_quiet_deadline") != event.as_record()
        ):
            return
        state = record.get("state")
        if event.source == "compact-expiry":
            if record.get(
                "attempt_id"
            ) == event.key and event.revision == self._expiry_revision(record):
                if state == "COMPLETED" and not record.get("event_completion_proved"):
                    self.store.update(
                        record,
                        state,
                        wake_state="FAILED",
                        wake_failure="post-compact-unproved",
                    )
                elif state == "CONTINUATION_SENT":
                    supervisor._reconcile_continuation_timeout(
                        self.store,
                        record,
                        int(self.dispatcher.clock()),
                        record["sent_at"],
                        self._timeout("AIH_INJECT_CONT_CONFIRM_TIMEOUT_SECONDS", 30),
                    )
                    self._expiry(event, record)
                elif state in {"SENT", "STARTED"}:
                    supervisor._transition_failure(
                        self.store,
                        record,
                        continuation=False,
                        reason="start-ack-timeout"
                        if state == "SENT"
                        else "completion-ack-timeout",
                    )
                elif state not in supervisor.TERMINAL_STATES:
                    self.store.update(
                        record,
                        "REFUSED",
                        reason="mod-request-expired"
                        if record.get("transport") == "mod" and state == "BOUND"
                        else "evidence-timeout",
                        terminal_at=int(self.dispatcher.clock()),
                    )
            return
        if state in supervisor.TERMINAL_STATES and state != "COMPLETED":
            return
        path_value = record.get("transcript_path")
        if record.get("event_evidence_refusal"):
            self._hold(record, record["event_evidence_refusal"])
            self._expiry(event, record)
            return
        if path_value:
            _, cursor, reason = read_records(record, record.get("event_cursor", {}))
            if reason:
                record = self._cancel_quiet(record)
                if reason not in {"transcript-unavailable", "partial-record"}:
                    record = self.store.update(
                        record, state, event_evidence_refusal=reason
                    )
                self._hold(record, reason)
                self._expiry(event, record)
                return
            if cursor != record.get("event_cursor"):
                record = self._cancel_quiet(record)
                record = self.store.update(record, state, event_cursor=cursor)
                if event.source == "compact-quiet":
                    return
        if record.get("attempt_type") == "continuation":
            if (
                record.get("state") == "CONTINUATION_SENT"
                and record.get("landing") == "awaiting-transcript"
            ):
                record = observe_landing(record, self.store)
            continuation.advance(self, event, session, record)
            return
        if path_value and state in {"SENT", "STARTED", "COMPLETED"}:
            if state == "SENT" and record.get("landing") == "awaiting-transcript":
                record = observe_landing(record, self.store)
            if completion_proved(record, path_value):
                self._expiry(event, record)
                self._complete(session, record)
                return
            self._hold(record, "compact-boundary-unproved")
            self._expiry(event, record)
            return
        if state in supervisor.TERMINAL_STATES:
            return
        if state == "QUEUED":
            self._hold(record, "queued-submission-pending")
            self._expiry(event, record)
            return
        if state == "ARMED":
            self._hold(record, "binding-pending")
            self._expiry(event, record)
            return
        if record.get("transport") == "mod":
            supervisor._expire_mod_attempt(self.store, record, session.slug)
            self._expiry(event, record)
            return
        if state == "BOUND" and record.get("origin") == "save-state":
            accepted, cursor, reason = stop_acceptance(
                record, record.get("event_stop_cursor", {})
            )
            if cursor != record.get("event_stop_cursor"):
                record = self.store.update(record, state, event_stop_cursor=cursor)
            if not accepted:
                self._hold(record, reason or "stop-acceptance-pending")
                self._expiry(event, record)
                return
        diagnostic = {}
        proof = supervisor.ContextSupervisor(self.store)._idle_proof(
            record, diagnostic=diagnostic
        )
        if record.get("transport") == "fifo":
            if proof is not None:
                fifo.advance(event, record, self.store, self.dispatcher)
                self._finish_quiet(record)
            else:
                self._quiet(event, record, diagnostic)
            self._expiry(event, record)
            return
        if session.session_id not in self.notifications or self.pane is None:
            self._hold(record, "pane-notifications-unavailable")
            self._expiry(event, record)
            return
        if proof is None:
            record = self.pane.remember(event, record, self.store)
            record = self._hold(record, "not-idle")
            self._quiet(event, record, diagnostic)
            self._expiry(event, record)
            return
        # Quiet eligibility uses only retained notification-owned pane evidence.
        # Unsupported adapters hold above and cannot sample or resend on expiry.
        self.pane.advance(event, session, record, self.store)
        self._finish_quiet(record)
        self._expiry(event, record)

    def _finish_quiet(self, record):
        current = next(
            row
            for row in self.store.read_attempts(str(record["key"]))
            if row.get("attempt_id") == record["attempt_id"]
        )
        self._cancel_quiet(current)

    def _cancel_quiet(self, record):
        pending = record.get("event_quiet_deadline")
        if pending:
            self.dispatcher.cancel(Event.from_record(pending).identity)
            return self.store.update(record, record["state"], event_quiet_deadline=None)
        return record

    def _quiet(self, event, record, diagnostic):
        if record.get("state") in supervisor.TERMINAL_STATES:
            return
        if diagnostic.get("transcript_result") != "quiet-period-pending":
            self._cancel_quiet(record)
            return
        if event.source == "compact-quiet":
            # An eligibility deadline is one-shot, never a readiness retry.
            self._cancel_quiet(record)
            return
        pending = record.get("event_quiet_deadline")
        if pending:
            self.dispatcher.accept([Event.from_record(pending)])
            return
        due = (
            self.dispatcher.clock()
            + diagnostic["quiet_seconds"]
            - diagnostic["boundary_age_s"]
        )
        quiet = Event(
            "compact-quiet",
            record["attempt_id"],
            digest(record.get("event_cursor")),
            event.target,
            deadline=due,
        )
        self.store.update(
            record, record["state"], event_quiet_deadline=quiet.as_record()
        )
        self.dispatcher.accept([quiet])

    def _complete(self, session, record):
        if not record.get("session_start_at") or not record.get("event_postcompact"):
            self._hold(record, "postcompact-ack-pending")
            return
        if record.get("wake_state") in {"CLAIMED", "FAILED", "DISABLED"}:
            return
        if (
            record.get("event_completion_proved")
            and record.get("wake_state") == "READY"
            and Path(record.get("wake_marker", "")).is_file()
        ):
            return
        artifact, reason = supervisor._matching_injected_artifact(self.store, record)
        if artifact is None:
            self.store.update(
                record,
                "COMPLETED",
                event_completion_proved=True,
                wake_state="FAILED",
                wake_failure=reason,
            )
            return
        if not sessions.registered(self.root.parent, session.slug, session.session_id):
            self._hold(record, "watch-registration-pending")
            return
        if supervisor._post_compact_notification_wake_enabled():
            marker = self.store._wake_marker_path(session.slug, record["attempt_id"])
            # Store the proved publication intent first. Restart can safely
            # recreate a missing marker; a competing claim is serialized above.
            record = self.store.update(
                record,
                "COMPLETED",
                completed_at=int(self.dispatcher.clock()),
                event_completion_proved=True,
                wake_state="READY",
                wake_marker=str(marker),
            )
            supervisor._write_wake_ready_marker(
                self.store, record, record["resume_artifact_digest"]
            )
            return
        else:
            fields = {
                "wake_state": "DISABLED",
                "wake_disabled_reason": supervisor.POST_COMPACT_WAKE_DISABLED_REASON,
            }
        self.store.update(
            record,
            "COMPLETED",
            completed_at=int(self.dispatcher.clock()),
            event_completion_proved=True,
            **fields,
        )

    def _expiry(self, event, record):
        record = next(
            (
                row
                for row in self.store.read_attempts(str(record["key"]))
                if row.get("attempt_id") == record["attempt_id"]
            ),
            record,
        )
        if (
            record.get("state") in supervisor.TERMINAL_STATES
            and record.get("state") != "COMPLETED"
        ):
            return
        since = (
            record.get("sent_at")
            or record.get("started_at")
            or record.get("event_ready_since")
            or record.get("armed_at")
        )
        if not isinstance(since, int):
            return
        duration = supervisor._compact_boundary_wait_seconds()
        if record.get("state") == "SENT":
            duration = self._timeout("AIH_INJECT_START_ACK_TIMEOUT_SECONDS", 30) + 1
        elif record.get("state") == "STARTED":
            duration = self._timeout("AIH_INJECT_DONE_ACK_TIMEOUT_SECONDS", 120) + 1
        elif record.get("state") == "CONTINUATION_SENT":
            duration = (
                self._timeout("AIH_INJECT_CONT_CONFIRM_TIMEOUT_SECONDS", 30)
                * 2 ** int(record.get("confirm_retries", 0))
                + 1
            )
        due = (
            record.get("mod_expires_at")
            if record.get("transport") == "mod" and record.get("state") == "BOUND"
            else since + duration
        )
        if not isinstance(due, (int, float)):
            self._hold(record, "expiry-unavailable")
            return
        expiry = Event(
            "compact-expiry",
            str(record["attempt_id"]),
            self._expiry_revision(record),
            event.target,
            deadline=due,
            observed_at=event.observed_at,
        )
        self.dispatcher.accept([expiry])

    @staticmethod
    def _expiry_revision(record):
        return f"{record.get('state')}:{record.get('sent_at') or record.get('started_at') or record.get('armed_at')}:{record.get('confirm_retries', 0)}"

    @staticmethod
    def _timeout(name, default):
        try:
            return max(0, int(os.environ.get(name, str(default))))
        except ValueError:
            return default
