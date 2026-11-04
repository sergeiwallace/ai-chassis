"""The FIFO's text and Enter are separate durable one-shot actions."""

from pathlib import Path

from ai_chassis import context_supervisor as supervisor, submit_fifo

from ..events import Event


def _hold(record, store, reason):
    if record.get("event_hold") != reason:
        store.update(record, record["state"], event_hold=reason)


def advance(event, record, store, dispatcher):  # noqa: PLR0911 - refusal and durable stage/Enter boundaries
    state = record.get("state")
    if state not in {"BOUND", "STAGED_UNVERIFIED"}:
        return
    path = Path(record.get("submit_fifo", ""))
    reason = submit_fifo.refusal(path) or supervisor._fifo_input_idle(record)
    if reason == "input-after-last-prompt":
        _hold(record, store, reason)
        return
    if reason:
        store.update(record, "REFUSED", reason=reason)
        return
    if state == "BOUND":
        due = dispatcher.clock() + float(
            supervisor._config()["fifo_enter_delay_seconds"]
        )
        enter = Event(
            "fifo-enter",
            str(record["attempt_id"]),
            str(record["armed_at"]),
            event.target,
            deadline=due,
        )
        current = store.update(
            record,
            "STAGED_UNVERIFIED",
            event_fifo_enter=enter.as_record(),
            event_fifo_staged=True,
        )
        store.consume(current)
        reason = submit_fifo.write_part(path, b"/compact")
        if reason:
            store.update(current, "REFUSED", reason=reason)
        else:
            store.update(current, "STAGED_UNVERIFIED", event_fifo_written=True)
            dispatcher.accept([enter])
        return
    if not record.get("event_fifo_written"):
        _hold(record, store, "fifo-write-unproved")
        return
    enter = Event.from_record(record["event_fifo_enter"])
    if event.source != "fifo-enter":
        dispatcher.accept([enter])
        return
    if event.identity != enter.identity:
        return
    current = store.update(
        record,
        "SENT",
        sent_at=supervisor._now(),
        submissions=int(record.get("submissions", 0)) + 1,
        landing="fifo-written",
    )
    reason = submit_fifo.write_part(path, b"\r")
    if reason:
        store.update(current, "REFUSED", reason=reason)
