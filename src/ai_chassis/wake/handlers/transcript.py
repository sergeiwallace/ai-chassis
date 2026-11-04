"""Incremental, session-bound evidence reads, called only after notifications."""

import json
from pathlib import Path

from ai_chassis import context_supervisor as supervisor


def read_records(record, cursor):  # noqa: C901, PLR0911 - fail-closed file identity and complete-line checks
    """Return complete appended records and a restart-safe cursor or refusal."""
    path = Path(record.get("transcript_path", ""))
    sid = record.get("transcript_session_id") or record.get("session_id")
    if path.stem != sid or path.suffix != ".jsonl" or "subagents" in path.parts:
        return [], cursor, "transcript-binding"
    try:
        with path.open("rb") as handle:
            import os

            stat = os.fstat(handle.fileno())
            inode = [stat.st_dev, stat.st_ino]
            if cursor.get("inode", inode) != inode:
                return [], cursor, "transcript-rotated"
            position = cursor.get("position", 0)
            if stat.st_size < cursor.get("size", position):
                return [], cursor, "transcript-truncated"
            if not cursor and stat.st_size > supervisor._transcript_tail_bytes():
                handle.seek(stat.st_size - supervisor._transcript_tail_bytes())
                handle.readline()
                position = handle.tell()
            handle.seek(position)
            raw = handle.read(supervisor._transcript_tail_bytes() + 1)
    except OSError:
        return [], cursor, "transcript-unavailable"
    if len(raw) > supervisor._transcript_tail_bytes():
        return [], cursor, "transcript-window-exceeded"
    rows = []
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            return [], cursor, "partial-record"
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            return [], cursor, "malformed-line"
        if not isinstance(row, dict):
            return [], cursor, "schema"
        rows.append(row)
        position += len(line)
    return (
        rows,
        {**cursor, "inode": inode, "position": position, "size": stat.st_size},
        None,
    )


def stop_acceptance(record, cursor):  # noqa: C901, PLR0911, PLR0912 - preserve the existing Stop lineage predicate
    """The legacy Stop lineage predicate, with notification-owned continuation."""
    if cursor.get("refusal"):
        return False, cursor, cursor["refusal"]
    rows, next_cursor, reason = read_records(record, cursor)
    if reason:
        if reason in {"partial-record", "transcript-unavailable"}:
            return False, next_cursor, reason
        return False, {**next_cursor, "accepted": False, "refusal": reason}, reason
    assistant = cursor.get("assistant")
    parents = dict(cursor.get("parents", {}))
    accepted = cursor.get("accepted", False)
    suffix = record.get("transcript_suffix")
    armed_at = record.get("armed_at")
    if not isinstance(suffix, str) or not isinstance(armed_at, int):
        return False, next_cursor, "schema"
    sid = record.get("transcript_session_id")
    for row in rows:
        if row.get("isSidechain") or row.get("sessionId", sid) != sid:
            continue
        uuid = row.get("uuid")
        if isinstance(uuid, str):
            parents[uuid] = supervisor._record_parent(row)
        message = row.get("message")
        role = message.get("role") if isinstance(message, dict) else None
        if assistant is None:
            content = message.get("content") if isinstance(message, dict) else None
            stamp = supervisor._transcript_timestamp(row.get("timestamp"))
            if (
                row.get("type") == "assistant"
                and role == "assistant"
                and row.get("sessionId") == sid
                and isinstance(uuid, str)
                and message.get("stop_reason") == "end_turn"
                and stamp is not None
                and stamp >= armed_at
                and isinstance(content, list)
                and any(
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                    and block["text"].endswith(suffix)
                    for block in content
                )
            ):
                assistant = uuid
            continue
        if row.get("type") in {"assistant", "user"} or role in {"assistant", "user"}:
            return (
                False,
                {**next_cursor, "accepted": False, "refusal": "intervening-turn"},
                "intervening-turn",
            )
        if row.get("type") == "system" and row.get("subtype") == "stop_hook_summary":
            if not supervisor._is_descendant(row, assistant, parents):
                return (
                    False,
                    {**next_cursor, "accepted": False, "refusal": "summary-parent"},
                    "summary-parent",
                )
            if row.get("preventedContinuation") is not False:
                return (
                    False,
                    {**next_cursor, "accepted": False, "refusal": "prevented-stop"},
                    "prevented-stop",
                )
            if row.get("hookErrors") not in ([], None):
                return (
                    False,
                    {**next_cursor, "accepted": False, "refusal": "hook-errors"},
                    "hook-errors",
                )
            accepted = True
    return (
        accepted,
        {
            **next_cursor,
            "assistant": assistant,
            "parents": parents,
            "accepted": accepted,
        },
        None,
    )


def observe_landing(record, store):
    """Classify complete, session-owned records after the persisted Enter fence."""
    rows, cursor, reason = read_records(record, record.get("event_landing_cursor", {}))
    if reason:
        return store.update(record, record["state"], event_hold=reason)
    fields = {"event_landing_cursor": cursor}
    queued = record.get("landing") == "queued"
    for row in rows:
        if row.get("isSidechain") or row.get(
            "sessionId", record.get("session_id")
        ) != record.get("session_id"):
            continue
        stamp = supervisor._transcript_timestamp(row.get("timestamp"))
        if stamp is None or stamp < record.get("sent_at", float("inf")):
            continue
        landing, queued = supervisor._landing_record(
            row, queued=queued, command=record.get("sent_command", "/compact")
        )
        if landing:
            fields.update(landing=landing, landed_at=int(stamp))
    if fields != {"event_landing_cursor": record.get("event_landing_cursor")}:
        return store.update(record, record["state"], **fields)
    return record
