"""Versioned boundary records. Invalid input is refused, never repaired."""

from __future__ import annotations

import json
import re
from datetime import datetime

from ai_chassis.session_address import AddressError, parse_address

MAX_RECORD_BYTES = 512 * 1024
REPLY = "aih-reply/v1"
RESULT = "aih-reply-result/v1"
_FIELDS = {
    REPLY: {
        "schema",
        "delivery_id",
        "channel",
        "source",
        "target",
        "kind",
        "item_path",
        "text",
        "command",
        "form",
        "received_at",
        "not_after",
        "signature",
    },
    RESULT: {
        "schema",
        "delivery_id",
        "host",
        "session",
        "layers",
        "injection",
        "skill",
        "finished_at",
        "signature",
    },
}


class RecordError(ValueError):
    """A record failed its schema or byte limit."""


def timestamp(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise RecordError(f"invalid {field}")
    return parsed


def _require(passed: bool, field: str) -> None:
    if not passed:
        raise RecordError(f"invalid {field}")


def _reply(record: dict) -> None:
    _require(record["channel"] in ("slack", "email"), "channel")
    source = record["source"]
    fields = (
        ("team", "user", "channel", "ts", "thread_ts")
        if record["channel"] == "slack"
        else ("token",)
    )
    _require(
        isinstance(source, dict)
        and all(
            isinstance(source.get(name), str) and bool(source[name]) for name in fields
        ),
        "source",
    )
    try:
        address = parse_address(record["target"])
    except (AddressError, TypeError, AttributeError):
        raise RecordError("invalid target") from None
    _require("*" not in str(address) and str(address) == record["target"], "target")
    _require(record["kind"] in ("conversation", "held-answer", "note"), "kind")
    item = record["item_path"]
    _require(
        isinstance(item, str) and bool(item)
        if record["kind"] == "held-answer"
        else item is None,
        "item_path",
    )
    _require(isinstance(record["text"], str) and bool(record["text"].strip()), "text")
    command = record["command"]
    if command is not None:
        _require(
            isinstance(command, dict) and set(command) == {"skill", "args"}, "command"
        )
        _require(
            isinstance(command["skill"], str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_-]*", command["skill"])
            is not None
            and isinstance(command["args"], str),
            "command",
        )
    _require(record["form"] == "verbatim", "form")
    timestamp(record["received_at"], "received_at")
    timestamp(record["not_after"], "not_after")


def _result(record: dict) -> None:
    for field in ("host", "session"):
        _require(isinstance(record[field], str) and bool(record[field]), field)
    for field, fields in (
        ("layers", ("layer", "outcome", "detail")),
        ("injection", ("attempt_id", "state", "landing", "reason")),
    ):
        _require(isinstance(record[field], list), field)
        for row in record[field]:
            _require(
                isinstance(row, dict) and all(name in row for name in fields), field
            )
            for name in fields:
                _require(
                    isinstance(row[name], str)
                    or (
                        field == "injection"
                        and name in ("landing", "reason")
                        and row[name] is None
                    ),
                    f"{field}.{name}",
                )
    skill = record["skill"]
    _require(
        skill is None
        or (
            isinstance(skill, dict)
            and all(name in skill for name in ("name", "state", "landing"))
        ),
        "skill",
    )
    timestamp(record["finished_at"], "finished_at")


def read_record(raw: str | bytes, *, max_bytes: int = MAX_RECORD_BYTES) -> dict:
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(encoded) > max_bytes:
        raise RecordError(f"max_bytes limit {max_bytes}")
    try:
        record = json.loads(encoded)
    except (ValueError, UnicodeError):
        raise RecordError("invalid JSON") from None
    validate_record(record)
    return record


def validate_record(record: dict) -> None:
    """Check the schema without serializing the record."""
    _require(isinstance(record, dict), "record")
    schema = record.get("schema")
    _require(isinstance(schema, str) and schema in _FIELDS, "schema")
    missing = sorted(_FIELDS[schema] - record.keys())
    if missing:
        raise RecordError(f"missing {missing[0]}")
    _require(
        isinstance(record["delivery_id"], str)
        and re.fullmatch(r"[0-9a-f]+", record["delivery_id"]) is not None,
        "delivery_id",
    )
    _require(isinstance(record["signature"], str), "signature")
    (_reply if schema == REPLY else _result)(record)


def write_record(record: dict) -> bytes:
    validate_record(record)
    encoded = json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_RECORD_BYTES:
        raise RecordError(f"max_bytes limit {MAX_RECORD_BYTES}")
    return encoded
