"""One recipient-state shape for every messaging rung (ISSUE-REF D-7).

Design `docs/designs/issue-ref-git-inbox-lifecycle-and-harness-update-notice.md`, § States and
receipts (AC-SM-1) and § Data Model. The shape was agreed with example-session's Agent Mail coordinator
(ISSUE-REF) on 2026-10-21. Three things live here and nowhere else:

THE VOCABULARY. :data:`EVENTS` is every receipt event any rung may write. The last four belong to
the `agent-mail` rung only (:data:`AGENT_MAIL_ONLY_EVENTS`).

A TRANSITION TABLE PER RUNG. :func:`register_rung` installs one rung's table: for each event it
uses, the state the event records and the states it may follow. The fold applies a receipt only
through its own rung's table, so neither rung's rows can move the other's states. A receipt whose
rung has no table, or whose event is not in its rung's table, folds as `foreign-receipt`. This
module registers the `git-inbox` table (AC-SM-1); the `agent-mail` table is example-session's.

AC-SM-1'S INPUT MACHINE. :func:`step` is the design's transition table for one git-inbox
(message, recipient) pair, used by the CLI verbs that must refuse an invalid move before writing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

SCHEMA = "recipient-state/1"

RUNG_GIT_INBOX = "git-inbox"
RUNG_AGENT_MAIL = "agent-mail"

# Receipt events: the one vocabulary.
EVENT_READ = "read"
EVENT_EXPIRED = "expired"
EVENT_TAKEN = "taken"
EVENT_CONFIRMED = "confirmed"
EVENT_CONFIRM_REFUSED = "confirm-refused"
EVENT_ESCALATED = "escalated"
EVENT_BLOCKED = "blocked"
EVENT_ACKED = "acked"
EVENT_RETIRED = "retired"
EVENTS = (
    EVENT_READ,
    EVENT_EXPIRED,
    EVENT_TAKEN,
    EVENT_CONFIRMED,
    EVENT_CONFIRM_REFUSED,
    EVENT_ESCALATED,
    EVENT_BLOCKED,
    EVENT_ACKED,
    EVENT_RETIRED,
)
AGENT_MAIL_ONLY_EVENTS = frozenset(
    {EVENT_ESCALATED, EVENT_BLOCKED, EVENT_ACKED, EVENT_RETIRED}
)

# AC-SM-1 states (git-inbox).
NONE = "none"
DELIVERED_PENDING = "delivered-pending"
READ = "read"
EXPIRED = "expired"
TAKEN = "taken"
CONFIRMED = "confirmed"
TERMINAL_STATES = frozenset({EXPIRED, CONFIRMED})
#: What a reader of the remote calls `none`: nothing on any fetched ref, never "unread".
NO_RECEIPT = "no-receipt"

#: Whose machine's ref carries a receipt: the recipient's, the message sender's, or unchecked.
WRITER_RECIPIENT = "recipient"
WRITER_SENDER = "sender"

FOREIGN_RECEIPT = "foreign-receipt"

_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@dataclass(frozen=True)
class Transition:
    """One event's row, optionally selected by a declared evidence field.

    A missing/invalid variant is foreign. Existing three-argument rows keep their meaning.
    """

    state: str
    after: frozenset[str]
    writer: str | None = None
    evidence_field: str | None = None
    variants: Mapping[Any, Transition] = field(default_factory=dict)


_TABLES: dict[str, dict[str, Transition]] = {}


def register_rung(rung: str, table: Mapping[str, Transition]) -> None:
    """Install `rung`'s transition table, in fold order. Re-registering replaces it.

    Refuses an event outside :data:`EVENTS`, and an agent-mail-only event in another rung's table.
    """
    if not rung:
        raise ValueError("a rung needs a name")
    _validate_table(rung, table)
    _TABLES[rung] = dict(table)


def _validate_table(rung: str, table: Mapping[str, Transition]) -> None:
    for event in table:
        if event not in EVENTS:
            raise ValueError(f"{event!r} is not in the recipient-state vocabulary")
        if event in AGENT_MAIL_ONLY_EVENTS and rung != RUNG_AGENT_MAIL:
            raise ValueError(f"{event!r} belongs to the {RUNG_AGENT_MAIL} rung only")


def rung_table(rung: str) -> Mapping[str, Transition] | None:
    return _TABLES.get(rung)


#: AC-SM-1's receipts, in fold order.
GIT_INBOX_TABLE: dict[str, Transition] = {
    EVENT_READ: Transition(
        READ, frozenset({NONE, DELIVERED_PENDING}), WRITER_RECIPIENT
    ),
    EVENT_EXPIRED: Transition(
        EXPIRED, frozenset({NONE, DELIVERED_PENDING}), WRITER_RECIPIENT
    ),
    # `take` writes `taken` only after it verified the read, so the fold accepts one whose `read`
    # receipt has not reached the remote yet. The strict T8 check lives in `step`, before writing.
    EVENT_TAKEN: Transition(
        TAKEN, frozenset({NONE, DELIVERED_PENDING, READ}), WRITER_RECIPIENT
    ),
    EVENT_CONFIRM_REFUSED: Transition(TAKEN, frozenset({TAKEN}), WRITER_SENDER),
    EVENT_CONFIRMED: Transition(CONFIRMED, frozenset({TAKEN}), WRITER_SENDER),
}
register_rung(RUNG_GIT_INBOX, GIT_INBOX_TABLE)

#: The shared fields every rung reports (D-7); anything else is rung-specific `extra`.
SHARED_FIELDS = ("schema", "rung", "message_id", "recipient", "event", "at")


class RecordError(ValueError):
    """A receipt that is not a recipient-state record."""


@dataclass(frozen=True)
class RecipientRecord:
    """One receipt. `extra` carries the rung-specific fields (`message_ref`, `evidence`)."""

    rung: str
    message_id: str
    recipient: str
    event: str
    at: str
    extra: Mapping[str, Any] = field(default_factory=dict)
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.event not in EVENTS:
            raise RecordError(f"unknown event {self.event!r}")
        for name in ("rung", "message_id", "recipient"):
            if not getattr(self, name):
                raise RecordError(f"{name} is empty")
        slug, at_sign, machine = self.recipient.partition("@")
        if not (slug and at_sign and machine):
            raise RecordError(f"recipient {self.recipient!r} is not slug@machine")
        if not _AT_RE.match(self.at):
            raise RecordError(f"at {self.at!r} is not a UTC YYYY-MM-DDTHH:MM:SSZ stamp")

    @property
    def recipient_machine(self) -> str:
        return self.recipient.partition("@")[2]

    def as_dict(self) -> dict[str, Any]:
        return {
            **dict(self.extra),
            "schema": self.schema,
            "rung": self.rung,
            "message_id": self.message_id,
            "recipient": self.recipient,
            "event": self.event,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> RecipientRecord:
        if not isinstance(raw, dict):
            raise RecordError("not an object")
        if raw.get("schema") != SCHEMA:
            raise RecordError(f"schema {raw.get('schema')!r} is not {SCHEMA}")
        values = {name: raw.get(name) for name in SHARED_FIELDS if name != "schema"}
        if not all(isinstance(value, str) for value in values.values()):
            raise RecordError("a shared field is missing or not a string")
        extra = {k: v for k, v in raw.items() if k not in SHARED_FIELDS}
        return cls(**values, extra=extra)


# ---------------------------------------------------------------------------------------------
# AC-SM-1: the git-inbox input machine
# ---------------------------------------------------------------------------------------------

# AC-SM-1 event alphabet (inputs to :func:`step`).
MATCH = "match"
CLAIM = "claim"
TTL = "ttl"
TAKE = "take"
CONFIRM_OK = "confirm-ok"
CONFIRM_DISAGREE = "confirm-disagree"
INPUTS = (MATCH, CLAIM, TTL, TAKE, CONFIRM_OK, CONFIRM_DISAGREE)

REFUSED_NOT_READ = "not-read"
REFUSED_NOT_TAKEN = "not-taken"
REFUSED_NO_ACTIONS = "no-actions"
REFUSED_CHECKS = "checks-failed"
REFUSED_DISAGREE = "confirm-disagree"


@dataclass(frozen=True)
class Step:
    """One transition's outcome: the target state, the receipt to write, and any refusal."""

    source: str
    event: str
    target: str
    receipt: str | None = None
    refused: str = ""

    @property
    def ok(self) -> bool:
        return not self.refused


#: AC-SM-1's rows with a true guard: (source, event) -> (target, receipt, refusal).
_ROWS: dict[tuple[str, str], tuple[str, str | None, str]] = {
    (NONE, MATCH): (DELIVERED_PENDING, None, ""),  # T1
    (DELIVERED_PENDING, CLAIM): (READ, EVENT_READ, ""),  # T2
    (DELIVERED_PENDING, TTL): (EXPIRED, EVENT_EXPIRED, ""),  # T3
    (TAKEN, CONFIRM_OK): (CONFIRMED, EVENT_CONFIRMED, ""),  # T6
    (TAKEN, CONFIRM_DISAGREE): (TAKEN, EVENT_CONFIRM_REFUSED, REFUSED_DISAGREE),  # T7
    (NONE, TAKE): (NONE, None, REFUSED_NOT_READ),  # T8
    (DELIVERED_PENDING, TAKE): (DELIVERED_PENDING, None, REFUSED_NOT_READ),  # T8
    **{  # T9
        (source, event): (source, None, REFUSED_NOT_TAKEN)
        for source in (NONE, DELIVERED_PENDING, READ)
        for event in (CONFIRM_OK, CONFIRM_DISAGREE)
    },
}


def step(
    state: str, event: str, *, has_actions: bool = False, checks_pass: bool = False
) -> Step:
    """AC-SM-1 for one (message, recipient). A refused step names why and stays put."""
    if event not in INPUTS:
        raise ValueError(f"not an AC-SM-1 event: {event!r}")
    if state in TERMINAL_STATES:
        return Step(state, event, state, refused=f"terminal:{state}")  # T10
    if (state, event) == (READ, TAKE):
        if has_actions and checks_pass:
            return Step(state, event, TAKEN, receipt=EVENT_TAKEN)  # T4
        refused = REFUSED_CHECKS if has_actions else REFUSED_NO_ACTIONS
        return Step(state, event, state, refused=refused)  # T5
    target, receipt, refused = _ROWS.get(
        (state, event), (state, None, f"invalid:{state}+{event}")
    )
    return Step(state, event, target, receipt=receipt, refused=refused)


# ---------------------------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Found:
    """One receipt as a rung found it: on whose machine's ref (or store), and where."""

    writer_machine: str
    record: RecipientRecord
    location: str = ""


@dataclass
class RecipientView:
    """One recipient's folded state on one rung. `events` maps each applied event to its `at`."""

    rung: str
    recipient: str
    state: str = NONE
    events: dict[str, str] = field(default_factory=dict)
    locations: dict[str, str] = field(default_factory=dict)
    records: dict[str, RecipientRecord] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return state_label(self.state)


@dataclass
class Folded:
    #: (rung, recipient) -> view.
    views: dict[tuple[str, str], RecipientView] = field(default_factory=dict)
    foreign: list[Found] = field(default_factory=list)

    def rung(self, rung: str) -> dict[str, RecipientView]:
        return {r: view for (g, r), view in self.views.items() if g == rung}


def state_label(state: str) -> str:
    return NO_RECEIPT if state == NONE else state


def _transition(
    record: RecipientRecord, table: Mapping[str, Transition]
) -> Transition | None:
    row = table.get(record.event)
    if row is None or row.evidence_field is None:
        return row
    evidence = record.extra.get("evidence")
    if not isinstance(evidence, Mapping):
        return None
    value = evidence.get(row.evidence_field)
    try:
        return row.variants.get(value)
    except TypeError:  # malformed evidence may contain a list or object
        return None


def is_foreign(
    found: Found,
    *,
    sender_machine: str | None,
    tables: Mapping[str, Mapping[str, Transition]] | None = None,
) -> bool:
    """No table for its rung, an event outside that table, or a ref its writer does not own."""
    table = (tables or {}).get(found.record.rung, rung_table(found.record.rung))
    row = _transition(found.record, table) if table is not None else None
    if row is None:
        return True
    writer = row.writer
    if writer == WRITER_RECIPIENT:
        return found.writer_machine != found.record.recipient_machine
    if writer == WRITER_SENDER:
        return sender_machine is not None and found.writer_machine != sender_machine
    return False


def fold(
    found: Iterable[Found],
    *,
    addressed: Iterable[str] = (),
    addressed_rung: str = RUNG_GIT_INBOX,
    sender_machine: str | None = None,
    tables: Mapping[str, Mapping[str, Transition]] | None = None,
) -> Folded:
    """One view per (rung, recipient) from every receipt found, plus every `addressed` recipient.

    Each receipt applies through its own rung's table, in that table's order and then by `at`, so
    a receipt from a machine whose clock runs behind still lands after the one it follows. A
    receipt whose event may not follow the current state is skipped; a foreign one changes nothing
    and is listed in `foreign`. Optional `tables` overlay the registered tables for this call
    only, leaving global registration intact.
    """
    # A call-local overlay uses the same vocabulary/ownership validation as registration.
    for rung, table in (tables or {}).items():
        if not rung:
            raise ValueError("a rung needs a name")
        _validate_table(rung, table)
    tables = _TABLES if tables is None else {**_TABLES, **tables}
    result = Folded()
    for recipient in addressed:
        key = (addressed_rung, recipient)
        result.views.setdefault(key, RecipientView(addressed_rung, recipient))
    applicable: list[tuple[int, Found]] = []
    for item in found:
        if is_foreign(item, sender_machine=sender_machine, tables=tables):
            result.foreign.append(item)
            continue
        order = list(tables[item.record.rung]).index(item.record.event)
        applicable.append((order, item))
    for _order, item in sorted(applicable, key=lambda p: (p[0], p[1].record.at)):
        record = item.record
        key = (record.rung, record.recipient)
        view = result.views.setdefault(
            key, RecipientView(record.rung, record.recipient)
        )
        row = _transition(record, tables[record.rung])
        assert row is not None  # foreign/invalid selections were excluded above
        if view.state not in row.after:
            continue
        view.state = row.state
        view.events[record.event] = record.at
        view.locations[record.event] = item.location
        view.records[record.event] = record
    return result
