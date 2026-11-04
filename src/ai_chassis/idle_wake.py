"""Typed idle admission and durable obligations, owned by the notifier's marker lock.

Adapters accept authenticated transport facts, never notice text. This module neither
selects owners nor claims hook delivery: I-1 supplies ownership, I-2 supplies delivery.
Omitted observations and shown receipts never close source work.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ai_chassis import notifier_pass
from ai_chassis.quota_optional import admission as quota_admission
from ai_chassis.session_address import AddressError, parse_address

MIN_DELAY_S = 60
MAX_DELAY_S = 86400
MAX_REMINDERS = 3
# Retain recent terminal detail; exact replay tombstones outlive detailed history.
TERMINAL_HISTORY_LIMIT = 128
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
KNOWN_SOURCES = frozenset(
    {
        "agent-mail",
        "buzz",
        "git-inbox",
        "escalation",
        "harness-sync",
        "broadcast",
        "poke",
        "outage",
        "incident",
        "save-state",
        "goal",
        "compact-recovery",
        "digest",
        "task-reminder",
        "total-tokens-reminder",
    }
)
PASSIVE_SOURCES = frozenset(
    {"outage", "digest", "task-reminder", "total-tokens-reminder"}
)
LIFECYCLE_SOURCES = frozenset({"save-state", "goal", "compact-recovery"})


@dataclass(frozen=True)
class Policy:
    automatic_wakes: bool = False
    passive_owner_after_s: int = 900
    incident_after_s: int = 300
    sync_reminder_after_s: int = 900
    sync_reminder_limit: int = 1
    reason: str = "ok"


def policy_path() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))
        / "ai-harness"
        / "idle-wake.json"
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate-key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise ValueError("non-json-constant")


def load_policy(path: Path | None = None) -> Policy:
    """Read the one live gate each call; legacy gate-only objects remain valid.

    Additional top-level keys are compatible with the existing builtins hook parser.
    No installer/seeder or environment override grants permission here.
    """
    try:
        raw = json.loads(
            (path or policy_path()).read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except OSError:
        return Policy(reason="policy-unavailable")
    except (ValueError, UnicodeError, RecursionError):
        return Policy(reason="policy-malformed")
    if not isinstance(raw, dict) or type(raw.get("automatic_wakes")) is not bool:
        return Policy(reason="policy-malformed")
    if type(raw.get("schema", 1)) is not int or raw.get("schema", 1) != 1:
        return Policy(reason="policy-schema")
    values: dict[str, Any] = {"automatic_wakes": raw["automatic_wakes"]}
    for name in (
        "passive_owner_after_s",
        "incident_after_s",
        "sync_reminder_after_s",
        "sync_reminder_limit",
    ):
        value = raw.get(name, getattr(Policy(), name))
        if type(value) is not int:
            return Policy(reason="policy-malformed")
        low, high = (
            (0, MAX_REMINDERS)
            if name == "sync_reminder_limit"
            else (MIN_DELAY_S, MAX_DELAY_S)
        )
        values[name] = max(low, min(high, value))
    return Policy(**values)


def seed_policy() -> bool:
    """Create the installed policy OFF, preserving every existing path and its bytes.

    No service restart is needed: publishers and consumers read the file at call time.
    Exclusive creation also preserves a concurrent installer's or operator's policy.
    """
    path = policy_path()
    if os.path.lexists(path):
        return False
    defaults = Path(__file__).parent / "defaults" / "idle-wake.json"
    policy = load_policy(defaults)
    if policy.reason != "ok" or policy.automatic_wakes:
        raise ValueError("idle-wake defaults must be valid and automatic wakes off")
    values = asdict(policy)
    values.pop("reason")
    body = (json.dumps({"schema": 1, **values}, separators=(",", ":")) + "\n").encode()
    previous = os.umask(0o077)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "wb") as target:
            target.write(body)
    finally:
        os.umask(previous)
    return True


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Event:
    source: str
    key: str
    revision: str
    recipients: tuple[str, ...]
    scope: str
    requires_action: bool
    terminal_condition: str
    owner: str | None = None
    owner_term: int = 0
    stage: str = "initial"
    attempt: int = 0
    qualified: bool = False
    ineligible: bool = False
    terminal: bool = False
    quarantine: str = ""
    transport_sender: str | None = None
    human_typed: bool = False

    @property
    def identity(self) -> str:
        # Recipient claims are separate: adding a group member must not reopen peers.
        return _digest([self.source, self.key, self.scope])

    @property
    def token(self) -> str:
        prefix = "sync" if self.source == "harness-sync" else self.source
        return (
            prefix
            + ":"
            + _digest(
                [
                    self.identity,
                    self.revision,
                    self.owner,
                    self.owner_term,
                    self.stage,
                    self.attempt,
                ]
            )
        )


def event_from_record(record: Any) -> Event:  # noqa: C901, PLR0912 - fail-closed schema boundary
    """Validate the adapter boundary without retaining content in quarantine records."""
    try:
        if not isinstance(record, dict):
            raise ValueError("metadata")
        if record.get("authenticated") is not True:
            raise ValueError("identity")
        if type(record.get("schema", 1)) is not int or record.get("schema", 1) != 1:
            raise ValueError("metadata")
        for key in ("source", "key", "revision", "terminal_condition"):
            if not isinstance(record.get(key), str) or not record[key]:
                raise ValueError("identity")
        recipients = record.get("recipients")
        if (
            not isinstance(recipients, list)
            or not recipients
            or any(not isinstance(s, str) or not SAFE.fullmatch(s) for s in recipients)
        ):
            raise ValueError("identity")
        if record.get("scope") not in {"addressed", "machine", "broadcast"}:
            raise ValueError("scope")
        if not SAFE.fullmatch(record["source"]):
            raise ValueError("identity")
        if record["source"] in LIFECYCLE_SOURCES | {"escalation"} and (
            record["scope"] != "addressed" or len(recipients) != 1
        ):
            raise ValueError("scope")
        if record["source"] == "poke" and (
            not record["revision"].isdigit() or int(record["revision"]) < 1
        ):
            raise ValueError("action")
        for key in (
            "requires_action",
            "held",
            "expired",
            "self_originated",
            "terminal",
            "qualified",
            "human_typed",
        ):
            if type(record.get(key, False)) is not bool:
                raise ValueError("action")
        if "requires_action" not in record:
            raise ValueError("action")
        for key in ("owner_term", "attempt"):
            if type(record.get(key, 0)) is not int or record.get(key, 0) < 0:
                raise ValueError("action")
        owner = record.get("owner")
        if owner is not None and (
            not isinstance(owner, str) or owner not in recipients
        ):
            raise ValueError("scope")
        if not isinstance(record.get("stage", "initial"), str):
            raise ValueError("action")
        return Event(
            source=record["source"],
            key=record["key"],
            revision=record["revision"],
            recipients=tuple(sorted(set(recipients))),
            scope=record["scope"],
            requires_action=record["requires_action"],
            terminal_condition=record["terminal_condition"],
            owner=owner,
            owner_term=record.get("owner_term", 0),
            stage=record.get("stage", "initial"),
            attempt=record.get("attempt", 0),
            qualified=record.get("qualified", False),
            ineligible=any(
                record.get(k, False)
                for k in ("held", "expired", "self_originated", "terminal")
            ),
            terminal=record.get("terminal", False),
            transport_sender=record.get("transport_sender"),
            human_typed=record.get("human_typed", False),
        )
    except (ValueError, TypeError) as exc:
        return Event(
            "quarantine",
            _digest(record),
            "invalid",
            (),
            "addressed",
            False,
            "none",
            quarantine="malformed-" + str(exc),
        )


@dataclass(frozen=True)
class Admission:
    wake: frozenset[str] = frozenset()
    passive: frozenset[str] = frozenset()
    reason: str = "admitted"

    def for_session(self, session: str) -> str:
        return (
            "wake"
            if session in self.wake
            else "passive"
            if session in self.passive
            else "never"
        )


def classify(event: Event) -> Admission:  # noqa: PLR0911 - one return per admission row category
    if event.quarantine:
        return Admission(reason=event.quarantine)
    if event.ineligible:
        return Admission(reason="source-ineligible")
    targets = frozenset(event.recipients)
    if event.source in LIFECYCLE_SOURCES:
        return (
            Admission(wake=targets)
            if event.qualified
            else Admission(reason="lifecycle-refused")
        )
    if event.source == "poke":
        return Admission(wake=targets)
    if event.source in PASSIVE_SOURCES or not event.requires_action:
        return Admission(passive=targets)
    reason = "admitted" if event.source in KNOWN_SOURCES else "unknown-source-action"
    if event.scope == "addressed":
        return Admission(wake=targets, reason=reason)
    if event.owner:
        return Admission(
            wake=frozenset({event.owner}),
            passive=targets - {event.owner},
            reason=reason,
        )
    return Admission(passive=targets, reason="no-eligible-owner")


def message_event(
    record: dict[str, Any], recipients: Iterable[str], *, machine: bool = False
) -> Event:
    """Adapt Message.as_record AFTER authenticated transport recipient expansion.

    Direct/group addressing is already proved by the producer's direct class.
    Unknown rungs retain explicit actions; untyped broadcasts cannot invent actions.
    """
    source = record.get("rung")
    message_class = record.get("class")
    action = (
        message_class == "direct"
        or record.get("ack_required") is True
        or record.get("requires_action", False)
    )
    raw = dict(
        source=source,
        key=str(record.get("message_id", "")),
        revision=str(record.get("action_revision", "pending")),
        recipients=list(recipients),
        scope="machine"
        if machine
        else "addressed"
        if message_class == "direct"
        else "broadcast",
        authenticated=True,
        requires_action=action,
        # Git headers are not authenticated identities. Buzz and board adapters
        # resolve senders from their transport directory before this boundary.
        transport_sender=record.get("from")
        if source in {"buzz", "agent-mail"}
        else None,
        terminal_condition=f"{source}:ack"
        if source == "agent-mail" and record.get("ack_required") is True
        else "agent-mail:read"
        if source == "agent-mail"
        else f"{source}:read-or-ack"
        if source != "git-inbox"
        else "git-inbox:taken-or-confirmed",
    )
    for key in ("held", "expired", "self_originated", "terminal"):
        if key in record:
            raw[key] = record[key]
    if (
        message_class not in {"direct", "broadcast"}
        or type(record.get("ack_required")) is not bool
        or type(record.get("message_id")) is not int
        or ("requires_action" in record and type(record["requires_action"]) is not bool)
        or (
            "action_revision" in record
            and (
                not isinstance(record["action_revision"], str)
                or not record["action_revision"]
            )
        )
    ):
        raw["authenticated"] = False
    return event_from_record(raw)


def escalation_event(record: dict[str, Any]) -> Event:
    target = (
        record.get("chief")
        if record.get("status") == "blocked"
        else record.get("routed_to")
    )

    try:
        recipient = parse_address(record.get("recipient", ""))
        stage = record.get("stage") or "blocked"
        session = record.get("session") or recipient.slug
        expected = (
            (record.get("chief") or session)
            if stage == "chief"
            else record.get("chief")
            if stage == "blocked"
            else session
        )
        valid = target == expected and stage in {
            "chief",
            "recipient",
            "reminder",
            "blocked",
        }
        valid = valid and (
            type(record.get("attempt")) is int
            or stage == "blocked"
            and record.get("attempt") is None
        )
    except (AddressError, AttributeError, TypeError):
        valid = False
    return event_from_record(
        dict(
            source="escalation",
            key=str(record.get("message_id", ""))
            + ":"
            + str(record.get("recipient", "")),
            revision=str(record.get("status", "")),
            recipients=[target],
            scope="addressed",
            authenticated=valid,
            requires_action=True,
            terminal_condition="agent-mail:read-or-ack",
            stage=record.get("stage") or "blocked",
            attempt=record.get("attempt") or 0,
        )
    )


def sync_event(token: str, owner: str, recipients: Iterable[str]) -> Event:
    """I-1's persisted census transition is the only installation episode authority."""
    try:
        prefix, stamp, ordinal, term, attempt = token.split(":")
        valid = prefix == "sync" and int(ordinal) > 0 and bool(stamp)
        owner_term, issued_attempt = int(term), int(attempt)
    except (ValueError, AttributeError):
        valid, owner_term, issued_attempt = False, 0, 0
    return event_from_record(
        dict(
            source="harness-sync",
            key="installation",
            revision=token,
            recipients=sorted(set(recipients) | {owner}),
            scope="machine",
            authenticated=valid,
            requires_action=True,
            owner=owner,
            owner_term=owner_term,
            attempt=issued_attempt,
            terminal_condition="harness-sync-owner.json:installed-stamp",
        )
    )


@dataclass
class Reconciliation:
    tokens: dict[str, set[str]] = field(default_factory=dict)
    passive: dict[str, set[str]] = field(default_factory=dict)
    gains: dict[str, set[str]] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    # Outstanding deliveries retain their bounded snapshot retry identity, but
    # are not fresh wake gains when an item leaves and re-enters a legacy view.
    pending_wakes: dict[str, set[str]] = field(default_factory=dict)


class AdmissionStore:
    """Single writer under the existing notifier lock; atomic, content-free state."""

    def __init__(self, path: Path):
        self.path = path
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {"schema": 1, "records": {}, "quarantine": {}, "reported": []}
        # Corrupt ownership must not silently reset and issue a new initial wake.
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != 1
            or not isinstance(raw.get("records"), dict)
            or not isinstance(raw.get("quarantine"), dict)
            or not isinstance(raw.get("reported"), list)
        ):
            raise ValueError("admission-state-malformed")
        self.records = raw["records"]
        self.quarantine = raw["quarantine"]
        if any(not isinstance(item, str) for item in raw["reported"]) or any(
            not self._valid_saved(record)
            or identity != _digest([record["source"], record["key"], record["scope"]])
            for identity, record in self.records.items()
        ):
            raise ValueError("admission-state-malformed")
        self.reported = set(raw["reported"])
        self.terminal = raw.get("terminal", {})
        self.history_counters = raw.get("history_counters", {})
        if (
            not isinstance(self.terminal, dict)
            or any(
                not isinstance(key, str)
                or not isinstance(value, dict)
                or type(value.get("episode")) is not int
                or not isinstance(value.get("revisions"), list)
                or any(not isinstance(revision, str) for revision in value["revisions"])
                for key, value in self.terminal.items()
            )
            or not isinstance(self.history_counters, dict)
            or any(
                source not in KNOWN_SOURCES | {"unknown"}
                or not isinstance(row, dict)
                or set(row) != set(self._empty_counters())
                or any(type(value) is not int or value < 0 for value in row.values())
                for source, row in self.history_counters.items()
            )
        ):
            raise ValueError("admission-state-malformed")
        for saved in self.records.values():
            saved.setdefault(
                "deliveries",
                {
                    issued: {"kind": "wake", "suppressed": False}
                    for issued in saved["issued"]
                },
            )

    @staticmethod
    def _valid_saved(record: Any) -> bool:
        def delivery_key(value: Any) -> bool:
            if not isinstance(value, str):
                return False
            recipient, separator, token = value.partition(":")
            return bool(SAFE.fullmatch(recipient) and separator and token)

        return (
            isinstance(record, dict)
            and type(record.get("episode")) is int
            and record["episode"] > 0
            and type(record.get("owner_term")) is int
            and record["owner_term"] >= 0
            and type(record.get("closed")) is bool
            and "owner" in record
            and type(record.get("created_at")) in {int, float}
            and math.isfinite(record["created_at"])
            # The aggregate recipients are historical, not one current event.
            # Escalation/lifecycle history can include multiple routed targets.
            and all(
                isinstance(record.get(key), str) and bool(record[key])
                for key in ("source", "key", "revision", "terminal_condition")
            )
            and SAFE.fullmatch(record["source"]) is not None
            and isinstance(record.get("recipients"), list)
            and bool(record["recipients"])
            and all(
                isinstance(recipient, str) and SAFE.fullmatch(recipient) is not None
                for recipient in record["recipients"]
            )
            and isinstance(record.get("scope"), str)
            and record["scope"] in {"addressed", "machine", "broadcast"}
            and (
                record["source"] not in LIFECYCLE_SOURCES | {"escalation"}
                or record["scope"] == "addressed"
            )
            and (
                record["source"] != "poke"
                or record["revision"].isdigit()
                and int(record["revision"]) > 0
            )
            and type(record.get("requires_action")) is bool
            and type(record.get("attempt")) is int
            and record["attempt"] >= 0
            and isinstance(record.get("stage"), str)
            and (
                record.get("owner") is None
                or isinstance(record["owner"], str)
                and SAFE.fullmatch(record["owner"]) is not None
            )
            and all(
                isinstance(record.get(k), list)
                and all(delivery_key(v) for v in record[k])
                for k in ("issued", "shown")
            )
            and all(
                isinstance(record[k], list)
                and all(
                    isinstance(value, str) and SAFE.fullmatch(value) is not None
                    for value in record[k]
                )
                for k in ("wake_recipients", "passive_recipients")
                if k in record
            )
            and (
                "event" not in record
                or isinstance(record["event"], dict)
                and set(record["event"]) == set(Event.__dataclass_fields__)
                and not event_from_record(
                    {**record["event"], "authenticated": True}
                ).quarantine
                and type(record["event"]["ineligible"]) is bool
                and record["event"]["quarantine"] == ""
                and all(
                    record["event"][key] == record[key]
                    for key in ("source", "key", "scope")
                )
            )
            and isinstance(record.get("deliveries", {}), dict)
            and isinstance(record.get("revisions", []), list)
            and all(isinstance(value, str) for value in record.get("revisions", []))
            and all(
                type(record[k]) in {int, float} and math.isfinite(record[k])
                for k in ("first_observed_at", "recovery_at")
                if k in record
            )
            and (
                "recovery_term" not in record
                or (
                    type(record["recovery_term"]) is int
                    and record["recovery_term"] >= 0
                    and "recovery_at" in record
                )
            )
            and all(
                isinstance(record[k], str)
                for k in ("blocked_reason", "completion_proof", "role_constraint")
                if k in record
            )
            and all(
                type(record[k]) is bool
                for k in ("digest_due", "metrics_frozen", "owner_is_chief")
                if k in record
            )
            and all(
                delivery_key(key)
                and isinstance(value, dict)
                and value.get("kind") in ("wake", "passive")
                and type(value.get("suppressed")) is bool
                and value.get("shown_kind", value["kind"]) in ("wake", "passive")
                for key, value in record.get("deliveries", {}).items()
            )
        )

    def _remember_terminal(self, identity: str, episode: int, *revisions: str) -> None:
        terminal = self.terminal.setdefault(identity, {"episode": 0, "revisions": []})
        terminal["episode"] = max(terminal["episode"], episode)
        terminal["revisions"] = sorted(set(terminal["revisions"]) | set(revisions))

    def _prepare(
        self,
        event: Event,
        assign_owner: Callable[[dict[str, Any], str], bool] | None,
        now: float,
    ) -> tuple[Event, dict[str, Any]] | None:
        terminal = self.terminal.get(event.identity, {})
        if event.revision in terminal.get("revisions", []):
            return None
        saved = self.records.setdefault(
            event.identity,
            {
                "episode": terminal.get("episode", 0) + 1,
                "owner": event.owner,
                "owner_term": event.owner_term,
                "role_constraint": "existing-installation-owner"
                if event.source == "harness-sync"
                else "any",
                "issued": [],
                "shown": [],
                "terminal_condition": event.terminal_condition,
                "closed": False,
                "created_at": now,
            },
        )
        if saved["closed"]:
            if event.revision == saved["revision"]:
                return None
            if not event.ineligible:
                # Settle this episode before reusing its identity. Historical
                # receipts must never be reobserved by the next episode.
                row = self._record_counters(saved, lambda *_: "pending")
                self._fold_counters(saved["source"], row)
                self._remember_terminal(
                    event.identity,
                    saved["episode"],
                    *saved.get("revisions", [saved["revision"]]),
                )
                saved.update(issued=[], shown=[], deliveries={}, revisions=[])
                saved["episode"] += 1
                saved["closed"] = False
                saved["created_at"] = now
                saved.pop("metrics_frozen", None)
                saved.pop("completion_proof", None)
                for key in (
                    "first_observed_at",
                    "recovery_term",
                    "recovery_at",
                    "digest_due",
                    "blocked_reason",
                ):
                    saved.pop(key, None)
        if (
            event.scope != "addressed"
            and event.requires_action
            and not event.ineligible
            and event.owner is None
            and assign_owner
        ):
            eligible = assign_owner(saved, "any")
            owner = saved["owner"] if eligible else None
            # The entitled scope may not contain an installation owner. Never expand it.
            event = Event(
                **{
                    **asdict(event),
                    "owner": owner if owner in event.recipients else None,
                    "owner_term": saved["owner_term"],
                }
            )
        return event, saved

    def reconcile(  # noqa: C901, PLR0912, PLR0915 - serialized disposition and issuance transaction
        self,
        events: Iterable[Event],
        policy: Policy,
        *,
        assign_owner: Callable[[dict[str, Any], str], bool] | None = None,
        now: float | None = None,
    ) -> Reconciliation:
        result = Reconciliation()
        now = time.time() if now is None else now

        def report(key: str, reason: str) -> None:
            if key not in self.reported:
                result.diagnostics.append(reason)
                self.reported.add(key)

        if policy.reason != "ok":
            report(policy.reason, policy.reason)
        for event in events:
            if event.quarantine:
                self.quarantine[event.key] = {"reason": event.quarantine}
                report(event.key, event.quarantine)
                continue
            prepared = self._prepare(event, assign_owner, now)
            if prepared is None:
                continue
            event, saved = prepared
            saved.setdefault("first_observed_at", saved["created_at"])
            decision = classify(event)
            if decision.reason == "no-eligible-owner":
                saved["blocked_reason"] = decision.reason
            if decision.reason == "unknown-source-action":
                report(event.identity, decision.reason)
            revisions = saved.setdefault("revisions", [])
            for revision in (saved.get("revision", event.revision), event.revision):
                if revision not in revisions:
                    revisions.append(revision)
            if event.terminal:
                # Completion settles every revision observed in this episode.
                self._remember_terminal(
                    event.identity,
                    saved["episode"],
                    *revisions,
                )
            saved.update(
                source=event.source,
                key=event.key,
                revision=event.revision,
                recipients=sorted(
                    set(saved.get("recipients", [])) | set(event.recipients)
                ),
                scope=event.scope,
                requires_action=event.requires_action,
                terminal_condition=event.terminal_condition,
                attempt=event.attempt,
                stage=event.stage,
                wake_recipients=sorted(decision.wake),
                passive_recipients=sorted(decision.passive),
                event=asdict(event),
            )
            if event.owner:
                saved.update(owner=event.owner, owner_term=event.owner_term)
            # Only explicit source terminal evidence closes an obligation; absence/shown do not.
            if event.ineligible:
                if event.terminal:
                    for recipient in event.recipients:
                        quota_admission.complete("idle-wake", recipient, event.identity)
                    saved["closed"] = True
                    saved["completion_proof"] = (
                        f"{event.source}:{event.key}:{event.revision}"
                    )
                continue
            waking = decision.wake if policy.automatic_wakes else frozenset()
            allowed = set()
            quota_held = set()
            for recipient in sorted(waking):
                if f"{recipient}:{event.token}" in saved["shown"]:
                    quota_admission.complete("idle-wake", recipient, event.identity)
                    continue
                if quota_admission.check(
                    "idle-wake",
                    recipient,
                    event.identity,
                    safety_save=event.source in {"save-state", "compact-recovery"},
                    human_typed=event.human_typed,
                    sender=event.transport_sender,
                ):
                    allowed.add(recipient)
                else:
                    quota_held.add(recipient)
                    saved["blocked_reason"] = "quota-held"
            waking = frozenset(allowed)
            if not quota_held and saved.get("blocked_reason") == "quota-held":
                saved.pop("blocked_reason")
            passive = decision.passive | (decision.wake - waking - quota_held)
            deliveries = saved.setdefault("deliveries", {})
            for recipient in passive:
                result.passive.setdefault(recipient, set()).add(event.token)
                deliveries.setdefault(
                    f"{recipient}:{event.token}",
                    {"kind": "passive", "suppressed": recipient in decision.wake},
                )
            for recipient in waking:
                issued = f"{recipient}:{event.token}"
                if issued in saved["shown"]:
                    continue
                result.pending_wakes.setdefault(recipient, set()).add(event.token)
                if issued not in saved["issued"]:
                    result.tokens.setdefault(recipient, set()).add(event.token)
                    result.gains.setdefault(recipient, set()).add(event.token)
                    saved["issued"].append(issued)
                deliveries.setdefault(issued, {"kind": "wake", "suppressed": False})
            result.events.append(event)
        self.save()
        return result

    def recover_obligations(  # noqa: C901, PLR0912, PLR0913, PLR0915 - serialized source/deadline reconciliation
        self,
        events: list[Event],
        policy: Policy,
        *,
        now: float,
        source_state: Callable[[dict[str, Any]], str],
        assign_owner: Callable[[dict[str, Any], str], bool],
        machine_recipients: set[str],
        snapshot_recipients: set[str] | None = None,
    ) -> list[Event]:
        """Reconcile absent carried actions against source proof, never delivery proof.

        Unread board mail and installation already have their own ladders.
        Board mail absent from unread fetches retains bounded status recovery.
        Buzz/git-inbox work and repair incidents use this backstop;
        confidential addressed mail is never handed to a maintenance owner.
        """
        current = {event.identity: event for event in events if not event.quarantine}
        for identity, saved in self.records.items():
            raw = saved.get("event")
            if raw is None and all(
                key in saved
                for key in ("source", "key", "scope", "requires_action", "recipients")
            ):
                # Phase 1 obligations already contain these typed facts. Preserve
                # their original age/owner/issued tokens during protocol migration.
                raw = asdict(
                    Event(
                        **{
                            key: saved[key]
                            for key in (
                                "source",
                                "key",
                                "revision",
                                "recipients",
                                "scope",
                                "requires_action",
                                "terminal_condition",
                                "owner",
                                "owner_term",
                                "stage",
                                "attempt",
                            )
                        }
                    )
                )
            if not raw or saved["closed"]:
                continue
            event = current.get(identity, Event(**raw))
            if event.scope == "machine":
                event = Event(
                    **{
                        **asdict(event),
                        "recipients": tuple(sorted(machine_recipients)) or ("machine",),
                    }
                )
                if identity in current:
                    current[identity] = event
            if event.source not in {
                "buzz",
                "git-inbox",
                "incident",
                "agent-mail",
                "escalation",
                "harness-sync",
                "outage",
            }:
                continue
            state = source_state(saved)
            # Persist consequences, not unchanged source observations: a quiet pass
            # must leave admission bytes and mtime untouched, even on first recovery.
            if state in {"completed", "retired"}:
                saved.update(closed=True, completion_proof=state)
                current.pop(identity, None)
                continue
            if state in {"held", "expired", "unavailable"}:
                saved["blocked_reason"] = "source-" + state
                current.pop(identity, None)
                continue
            if saved.get("blocked_reason", "").startswith("source-"):
                saved.pop("blocked_reason", None)
            if event.source in {"escalation", "harness-sync", "outage"} or (
                event.source == "agent-mail" and identity in current
            ):
                continue  # Their existing source-specific ladders remain authoritative.
            first = saved.get("first_observed_at", saved["created_at"])
            if now < first:
                saved["first_observed_at"] = now
            if now < saved.get("recovery_at", now):
                saved["recovery_at"] = now
            age = max(0, now - saved.get("first_observed_at", saved["created_at"]))
            if not event.requires_action:
                current[identity] = event
                unshown = any(
                    f"{recipient}:{event.token}" not in saved["shown"]
                    for recipient in event.recipients
                )
                if not unshown:
                    saved.pop("digest_due", None)
                if age >= policy.passive_owner_after_s and unshown:
                    assign_owner(saved, "any")
                    saved["digest_due"] = True
                    if not saved.get("owner"):
                        saved["blocked_reason"] = "no-eligible-owner"
                continue
            if age < policy.incident_after_s and "recovery_term" not in saved:
                continue
            if event.source == "agent-mail":
                # A read can remove ack-required mail before any ladder exists.
                # Retain it as overdue, then blocked, without fetching its body or
                # publishing a second ladder alongside source escalation stages.
                if "recovery_term" not in saved:
                    saved.update(recovery_term=0, recovery_at=now)
                elif now - saved["recovery_at"] >= policy.incident_after_s:
                    saved["blocked_reason"] = "action-unresolved"
                continue
            if event.scope == "addressed":
                # Never give private recipient work to the generic machine owner.
                owner = None
                term = 0
            else:
                eligible = assign_owner(saved, "any")
                owner = saved.get("owner")
                if not eligible or owner not in event.recipients:
                    saved["blocked_reason"] = "no-eligible-owner"
                    continue
                term = saved["owner_term"]
                if saved.get("blocked_reason") == "no-eligible-owner":
                    saved.pop("blocked_reason")
            # One overdue revision per authorized term. Showing it never closes work.
            if saved.get("recovery_term") != term:
                saved.update(recovery_term=term, recovery_at=now)
                if saved.get("blocked_reason") == "action-unresolved":
                    saved.pop("blocked_reason")
            elif now - saved["recovery_at"] >= policy.incident_after_s:
                saved["blocked_reason"] = "action-unresolved"
            if (
                event.scope == "addressed"
                and identity in current
                and set(event.recipients) <= (snapshot_recipients or set())
                and not any(
                    f"{recipient}:{event.token}" in saved["shown"]
                    for recipient in event.recipients
                )
            ):
                # Unshown headers still use the existing bounded delivery retry.
                # Replacing their token would reset that retry's identity/budget.
                continue
            current[identity] = Event(
                **{
                    **asdict(event),
                    "owner": owner,
                    "owner_term": term,
                    "stage": "initial" if event.source == "incident" else "overdue",
                    "attempt": 0 if event.source == "incident" else 1,
                }
            )
        return [*current.values(), *(event for event in events if event.quarantine)]

    @staticmethod
    def _empty_counters() -> dict[str, int]:
        return dict.fromkeys(
            (
                "admitted",
                "passive",
                "passively_delivered",
                "suppressed",
                "shown",
                "uncertain",
                "overdue",
                "blocked",
            ),
            0,
        )

    def _record_counters(
        self, saved: dict[str, Any], observe: Callable[[str, str], str]
    ) -> dict[str, int]:
        row = self._empty_counters()
        row["admitted"] += len(saved["issued"])
        delivery_blocked = False
        for delivery, facts in saved.get("deliveries", {}).items():
            recipient, token = delivery.split(":", 1)
            state = (
                "pending" if saved.get("metrics_frozen") else observe(recipient, token)
            )
            if state in {"shown", "wake-shown", "passive-shown"}:
                if delivery not in saved["shown"]:
                    saved["shown"].append(delivery)
                facts.setdefault(
                    "shown_kind",
                    state.removesuffix("-shown") if state != "shown" else facts["kind"],
                )
            shown = delivery in saved["shown"]
            row["shown"] += int(shown)
            row["uncertain"] += int(state == "uncertain" and not shown)
            delivery_blocked |= state == "blocked" and not shown
            row["passive"] += int(facts["kind"] == "passive")
            row["passively_delivered"] += int(
                shown and facts.get("shown_kind") == "passive"
            )
            row["suppressed"] += int(facts["suppressed"])
        if not saved["closed"]:
            row["overdue"] += int(
                bool(saved.get("digest_due") or "recovery_term" in saved)
            )
            row["blocked"] += int(bool(saved.get("blocked_reason")) or delivery_blocked)
        else:
            saved["metrics_frozen"] = True
            row["uncertain"] = 0
        return row

    def _fold_counters(self, source: str, row: dict[str, int]) -> None:
        source = source if source in KNOWN_SOURCES else "unknown"
        total = self.history_counters.setdefault(source, self._empty_counters())
        for key, value in row.items():
            total[key] += value

    def observe_deliveries(self, observe: Callable[[str, str], str]) -> dict[str, Any]:
        """Idempotent per-source counters; fixed labels, no identities or content."""
        counters = {source: dict(row) for source, row in self.history_counters.items()}
        terminal_rows = {}
        for identity, saved in self.records.items():
            source = saved.get("source", "unknown")
            source = source if source in KNOWN_SOURCES | {"incident"} else "unknown"
            row = self._record_counters(saved, observe)
            if saved["closed"]:
                terminal_rows[identity] = (source, row)
                self._remember_terminal(
                    identity,
                    saved["episode"],
                    *saved.get("revisions", [saved["revision"]]),
                )
            total = counters.setdefault(source, self._empty_counters())
            for key, value in row.items():
                total[key] += value
        # Compact oldest detail deterministically, retaining every open obligation.
        closed = sorted(
            terminal_rows, key=lambda key: (self.records[key]["created_at"], key)
        )
        for identity in closed[:-TERMINAL_HISTORY_LIMIT]:
            self.records.pop(identity)
            source, row = terminal_rows[identity]
            self._fold_counters(source, row)
        self.save()
        return counters

    def save(self) -> None:
        if len(self.quarantine) > TERMINAL_HISTORY_LIMIT:
            self.quarantine = dict(
                sorted(self.quarantine.items())[-TERMINAL_HISTORY_LIMIT:]
            )
        self.reported.intersection_update(
            self.records.keys()
            | self.quarantine.keys()
            | {"policy-unavailable", "policy-malformed", "policy-schema"}
        )
        data = (
            json.dumps(
                {
                    "schema": 1,
                    "records": self.records,
                    "quarantine": self.quarantine,
                    "reported": sorted(self.reported),
                    **({"terminal": self.terminal} if self.terminal else {}),
                    **(
                        {"history_counters": self.history_counters}
                        if self.history_counters
                        else {}
                    ),
                },
                sort_keys=True,
            )
            + "\n"
        )
        if self.path.exists() and self.path.read_text(encoding="utf-8") == data:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".admission-", dir=self.path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            notifier_pass.fsync(stream.fileno())
        os.replace(name, self.path)
