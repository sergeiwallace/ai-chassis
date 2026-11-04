"""Bound continuation reservation and notification-driven submission."""

import secrets
import os

from ai_chassis import context_supervisor as supervisor
from ..events import Event


def request(key, pane, store, tmux):
    """Reserve once without reading a composer or waiting for the banner turn."""
    if not supervisor._enabled() or not supervisor._enrolled(key):
        return None
    with store.locked():
        records = store.read_attempts(key)
        if any(
            row.get("attempt_type") == "continuation"
            and row.get("state") not in supervisor.TERMINAL_STATES
            for row in records
        ):
            return None
        completed = next(
            (
                row
                for row in records
                if row.get("attempt_type") == "compact"
                and row.get("state") == "COMPLETED"
                and row.get("event_completion_proved")
            ),
            None,
        )
        if completed is None or not store.permitted(key, "continuation"):
            return None
        if not supervisor._post_compact_typed_wake_enabled() and not completed.get(
            "typed_wake_override"
        ):
            return None
        command = completed.get("resume_command")
        if not isinstance(command, str) or supervisor.digest(
            command.encode()
        ) != completed.get("resume_command_digest"):
            return None
        record = {
            "key": key,
            "attempt_id": secrets.token_hex(8),
            "attempt_type": "continuation",
            "state": "BOUND",
            "parent_compact": completed["attempt_id"],
            "session_id": completed.get("session_id")
            or completed.get("transcript_session_id"),
            "transcript_session_id": completed.get("transcript_session_id"),
            "transcript_path": completed.get("transcript_path"),
            "sent_command": command,
            "command_digest": completed["resume_command_digest"],
            "pane_id": pane,
            "pane_fingerprint": tmux.fingerprint(pane),
            "armed_at": supervisor._now(),
            "event_managed": True,
        }
        store.write_attempt(record)
    supervisor._event_handoff(
        key, "compact-lifecycle", store=store, attempt_id=record["attempt_id"]
    )
    return record


def advance(handler, event, session, record):  # noqa: C901, PLR0912 - bound banner and transport transitions
    """Observe banner consumption, then stage and verify on separate changes."""
    store = handler.store
    from .compact import completion_proved

    parent = next(
        (
            row
            for row in store.read_attempts(session.slug)
            if row.get("attempt_id") == record.get("parent_compact")
        ),
        None,
    )
    if (
        parent is None
        or not parent.get("event_completion_proved")
        or not completion_proved(parent, parent.get("transcript_path", ""))
        or (parent.get("session_id") or parent.get("transcript_session_id"))
        != session.session_id
    ):
        store.update(
            record, "CONTINUATION_AMBIGUOUS", reason="missing-validated-history"
        )
        return
    if record.get("state") == "CONTINUATION_SENT":
        # UserPromptSubmit's existing exact-command confirmation owns the ack.
        handler._expiry(event, record)
        return
    engine = supervisor.ContextSupervisor(store)
    expected = parent.get("resume_artifact_digest")
    if isinstance(expected, str):
        if engine._banner_packet(session.slug, expected, "confirmed") is not None:
            store.update(
                record,
                "CONTINUATION_CONFIRMED",
                reason="banner-turn-consumed",
                confirmed_at=int(handler.dispatcher.clock()),
            )
            return
        if engine._banner_packet(
            session.slug, expected, "injected"
        ) is not None and not record.get("event_banner_expired"):
            try:
                budget = float(
                    os.environ.get(
                        "AIH_INJECT_BANNER_WAIT_SECONDS",
                        str(supervisor._DEFAULT_BANNER_TURN_WAIT_SECONDS),
                    )
                )
                if not 0 <= budget <= supervisor._MAX_BANNER_TURN_WAIT_SECONDS:
                    raise ValueError
            except ValueError:
                handler._hold(record, "banner-budget-invalid")
                return
            deadline = Event(
                "continuation-banner-expiry",
                record["attempt_id"],
                "1",
                event.target,
                deadline=record["armed_at"] + budget,
            )
            if event.identity == deadline.identity:
                store.update(
                    record,
                    record["state"],
                    event_banner_expired=True,
                    event_ready_since=int(handler.dispatcher.clock()),
                    event_hold="fresh-pane-evidence-required",
                )
            else:
                handler._hold(record, "banner-turn-pending")
                handler.dispatcher.accept([deadline])
            return
    if record.get("event_banner_expired") and event.source != "pane-evidence":
        handler._hold(record, "fresh-pane-evidence-required")
        handler._expiry(event, record)
        return
    if session.session_id not in handler.notifications or handler.pane is None:
        handler._hold(record, "pane-notifications-unavailable")
    elif not supervisor._tmux_version_allowed(handler.pane.tmux.version()):
        store.update(record, "CONTINUATION_AMBIGUOUS", reason="tmux-version")
    else:
        handler.pane.advance(event, session, record, store)
    handler._expiry(event, record)
