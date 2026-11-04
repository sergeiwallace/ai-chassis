"""The ``aih-attribution/v1`` record: who in the fleet initiated a thing this host is looking at.

ONE FORMAT, TWO CUSTOMERS. `ISSUE-REF` needs to know which fleet SESSION initiated an AWS SSO
device code that loaded here; `ISSUE-REF` needs to know which local PROCESS opened a URL. Four
rounds of diagnosis on the second and a hand-approved code on the first were the same missing
capability -- attribution -- so the two agree on one record rather than each inventing one. This
module owns the schema; ``kind`` selects the customer. :data:`KIND_URL_OPEN` is consumed by ONE
store only, :class:`LocalLaunchLedger` (`ISSUE-REF`): a harness emitter that is not ``sso-reauth``
declares itself to the instrumented opener, which binds the code it was handed.

WHAT MAKES A RECORD TRUSTWORTHY IS NOT IN THE RECORD. D-7 of
``docs/designs/issue-ref-sso-device-code-auto-approval.md`` refuses to approve an authorization
request this system cannot attribute, because approving one you did not initiate is the
consent-phishing shape. Widening that predicate from "this machine launched it" to "a fleet host
launched it" is only safe if the widening carries evidence, so:

* the ``sender`` block is written by the TRANSPORT RECEIVER, never by the sender.
  :func:`AttributionRecord.from_transport_payload` REFUSES a payload that carries its own
  ``sender`` -- a field any caller can set saying "I am a fleet session" is the rejected
  blanket-approval option with extra steps;
* the approver re-checks the stamped identity against the server-owned machine registry
  (:func:`verify_sender`, reusing Agent Mail's ``MachineRegistry.authenticate`` and
  ``TransportIdentity``), so "a known fleet host" stays a checkable fact rather than a convention;
* a record with no ``sender``, an unknown identity, a revoked machine, or an ``initiating_host``
  that disagrees with the registry attributes NOTHING. Every refusal path is fail-closed and the
  caller is expected to fall back to showing the human, exactly as today.

HOW A RECORD IDENTIFIES ITS CODE WITHOUT STORING IT, and the trade-off that forced the shape.
The requirements are in tension: the approver must match the EXACT code on the page (the
anti-phishing control AWS relies on), the ``user_code`` must not be persisted or logged, and a
digest must not be offered as a CONFIDENTIALITY measure -- an AWS device code is ~8 characters
from a small alphabet, so any digest of one is brute-forceable in trivial time. The in-memory-only
shape the issue sketches cannot hold here as measured: the transport receiver and the approver are
SEPARATE PROCESSES minutes apart (the receiver writes the inbox, a later sentinel tick reads it),
so there is no shared memory to hold the code in.

So the record carries :class:`CodeBinding` -- ``HMAC-SHA256(per-record random salt, user_code)``
with the salt stored beside it -- and the approver recomputes it from the code it read off the
page. This is a MATCHING mechanism and is stated here as nothing more:

* it preserves the exact-code match, so a code that differs from the record's by one character
  attributes nothing;
* it keeps the literal code string out of the on-disk record, so an accidental disclosure of the
  inbox (a stray ``cat``, a backup, a future PostgreSQL mirror under `ISSUE-REF`) does not hand
  over a live code;
* it buys NO confidentiality against an attacker who has the file, because the salt is right
  there and the search space is tiny. Confidentiality comes from the transport, the TTL and the
  state directory's permissions -- never from this digest.

The residual risk is recorded in the design doc rather than hidden here: an attacker who can
write this host's SSO state directory can plant a record, and phase 1 detects that only insofar
as the planted ``sender`` must still name an identity the server-owned registry holds for the
named machine.

THREE STORES, THREE TRUST ANCHORS, ONE SCHEMA. :class:`AttributionInbox` holds what a transport
DELIVERED, so the registry governs it; :class:`LocalLaunchLedger` holds what THIS host wrote about
its own login, anchored on its 0700 directory; :class:`PeerLaunchLedger` holds what this host
PULLED from a peer it dialled itself, anchored on having chosen the peer locally before any byte
moved. They are separate directories with separate readers on purpose -- a single reader that
demanded a stamped sender only sometimes is how a fail-closed guard becomes conditional -- and
none of the three widens the POSITIVE MATCH, which is always an exact :class:`CodeBinding` match
against the code read off the page.

TTL. ``expires_at`` is clamped to ``launched_at + ttl_cap_s`` on read, where the cap is one
device-code lifetime (``[sso_reauth].approve_timeout_s``). A sender does not get to extend the
window it is trusted for, and :meth:`AttributionInbox.purge` deletes a record the moment its
clamped expiry passes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

SCHEMA = "aih-attribution/v1"

#: `ISSUE-REF`: a fleet session is about to run `aws sso login --use-device-code`.
KIND_DEVICE_CODE_LAUNCH = "device-code-launch"
#: `ISSUE-REF` (vocabulary from `ISSUE-REF`): a harness component that is NOT sso-reauth ran
#: `aws sso login` through the instrumented opener (`sso_browser_handoff.sh`), DECLARED itself, and
#: the opener bound the code it was handed. Evidence only in :class:`LocalLaunchLedger`.
KIND_URL_OPEN = "url-open"
#: `ISSUE-REF` (ISSUE-REF D-3 (a)): the APPROVING host tells the LAUNCHING host who approved its
#: code. Carries the launch's own binding, never the code, and is admitted only by
#: :class:`ApprovalOutcomeStore`, against a launch the receiving host wrote itself.
KIND_APPROVAL_OUTCOME = "approval-outcome"
KINDS = (KIND_DEVICE_CODE_LAUNCH, KIND_URL_OPEN, KIND_APPROVAL_OUTCOME)

#: `scripts/sso/sso_reauth.py`'s ATTRIBUTION_* words, restated for the reason SOURCES is.
ACTORS = ("automation", "human", "unknown")
#: The sentinel's approver-exit words (`_record_approval_outcome`).
VERDICTS = ("approved", "refused")
#: `ISSUE-REF` (ISSUE-REF T-09): the same return leg carries whether the launch's device page ever
#: appeared on the approving host. Not an approval, so filed apart (:data:`PEER_DELIVERY_NAME`).
URL_DELIVERED = "url-delivered"
URL_NOT_SEEN = "url-not-seen"
DELIVERY_VERDICTS = (URL_DELIVERED, URL_NOT_SEEN)
#: Fields only an approval-outcome carries, in wire order.
OUTCOME_FIELDS = ("actor", "rung", "verdict", "at", "approving_host")

#: The closed set `scripts/sso/sso_reauth.py` already uses for `--source`. Restated rather than
#: imported, the way that module restates the attribution vocabulary: these words are the wire
#: format between processes on two hosts, and a rename must break a test rather than silently
#: turn every past record into an unrecognised value. Pinned equal by the suite.
SOURCES = ("sentinel", "claude-code", "manual")

#: The identity kinds `services/agent_mail/authorization.py` `TransportIdentity` admits, restated
#: for the same reason and pinned equal to it by the suite. :func:`verify_sender` constructs the
#: real `TransportIdentity`, so the reuse is not merely nominal.
IDENTITY_KINDS = ("mtls", "tailscale")

BINDING_ALG = "hmac-sha256"
BINDING_SALT_BYTES = 32

#: Where a local launch lands, relative to the caller's state directory. A SIBLING of the
#: transport inbox, never the same directory -- see :class:`LocalLaunchLedger`.
LOCAL_LEDGER_NAME = "attribution-local-launches"

#: Where records PULLED from a peer fleet host land. A third sibling, for the third trust
#: anchor -- see :class:`PeerLaunchLedger`.
PEER_LEDGER_NAME = "attribution-peer-launches"

#: Where a LAUNCHING host keeps the approval outcomes a peer approver sent back (`ISSUE-REF`). A
#: sibling of sso-approve's own `attributed-approvals`, never that directory: those files are this
#: host's approver's facts, these are a peer's report about a launch this host made.
PEER_OUTCOME_NAME = "attributed-approvals-peer"

#: Where a LAUNCHING host keeps a peer's :data:`DELIVERY_VERDICTS` about its launches (`ISSUE-REF`).
#: A sibling of :data:`PEER_OUTCOME_NAME`, never that directory: every reader of the approvals store
#: takes its ``actor`` as who approved, and a delivery verdict names nobody.
PEER_DELIVERY_NAME = "url-deliveries-peer"

#: How far ahead of this host's clock a peer's ``launched_at`` may sit before the record is
#: refused. Two fleet hosts both run NTP, so the honest skew is seconds; the allowance exists
#: so a one-second disagreement is not a refusal, NOT so a sender can choose its own window.
#: A FUTURE-dated ``launched_at`` is the one direction in which believing the sender's clock
#: EXTENDS the trust window, because the clamp in :meth:`AttributionRecord.effective_expiry`
#: is ``launched_at + ttl_cap`` -- so it is refused rather than clamped.
PEER_CLOCK_SKEW_S = 60

_HOST_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SESSION_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HEX_RE = re.compile(r"\A[0-9a-f]{2,128}\Z")
_RUNG_RE = re.compile(r"\A[A-Za-z0-9._-]{0,64}\Z")
_FILE_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


class AttributionError(ValueError):
    """A record is malformed. Attributes nothing."""


class AttributionRefused(AttributionError):
    """A record was structurally refused as EVIDENCE, not merely found absent.

    Separate from :class:`AttributionError` because the two deserve different log lines: a
    missing record is the ordinary case, while a payload asserting its own ``sender`` or naming
    an identity the registry does not hold is a security event worth saying out loud.
    """


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(stamp: object) -> dt.datetime:
    if not isinstance(stamp, str):
        raise AttributionError("a timestamp must be an ISO-8601 string")
    try:
        moment = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AttributionError(f"unparseable timestamp: {stamp!r}") from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def _require_str(mapping: dict, key: str, pattern: re.Pattern[str]) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise AttributionError(f"{key} is missing or not in the permitted form")
    return value


def local_host_id() -> str:
    """This host's own name, for the ``initiating_host`` of a record it writes about itself.

    DIAGNOSTIC, NOT A CREDENTIAL. Nothing authenticates against this value: a local record's
    locality is established by the directory it sits in, not by a name any writer could choose.
    It exists so a log line can say which host a record describes.

    ``socket.gethostname`` rather than ``AI_HOST`` deliberately: the sentinel runs under launchd,
    which sources no shell profile, so ``AI_HOST`` is absent there (the measured cause of
    `ISSUE-REF`'s sibling defect in ``agent_mail_notifier``). A value the producer would be
    missing exactly when it runs for real is not a usable identifier.
    """

    raw = _FILE_SAFE_RE.sub("_", socket.gethostname().strip() or "localhost")[:64]
    return raw if _HOST_RE.fullmatch(raw) else "localhost"


@dataclass(frozen=True)
class CodeBinding:
    """A keyed digest the approver RECOMPUTES from the code on the page. Not a privacy measure."""

    alg: str
    salt: str
    value: str

    def __post_init__(self) -> None:
        if self.alg != BINDING_ALG:
            raise AttributionError(f"unsupported code binding algorithm: {self.alg!r}")
        for name in ("salt", "value"):
            if not _HEX_RE.fullmatch(getattr(self, name)):
                raise AttributionError(f"code binding {name} is not lowercase hex")

    @classmethod
    def for_code(cls, user_code: str, *, salt: str | None = None) -> CodeBinding:
        if not isinstance(user_code, str) or not user_code.strip():
            raise AttributionError("a code binding needs a non-empty user code")
        salt = salt or secrets.token_hex(BINDING_SALT_BYTES)
        return cls(alg=BINDING_ALG, salt=salt, value=cls._digest(salt, user_code))

    @staticmethod
    def _digest(salt: str, user_code: str) -> str:
        return hmac.new(
            bytes.fromhex(salt), user_code.strip().encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def matches(self, user_code: str) -> bool:
        """Constant-time, so a near miss leaks no timing about how near it was."""
        if not isinstance(user_code, str) or not user_code.strip():
            return False
        return hmac.compare_digest(self.value, self._digest(self.salt, user_code))

    def to_dict(self) -> dict[str, str]:
        return {"alg": self.alg, "salt": self.salt, "value": self.value}

    @classmethod
    def from_mapping(cls, mapping: object) -> CodeBinding:
        if not isinstance(mapping, dict):
            raise AttributionError("code_binding must be a mapping")
        return cls(
            alg=str(mapping.get("alg", "")),
            salt=str(mapping.get("salt", "")),
            value=str(mapping.get("value", "")),
        )


@dataclass(frozen=True)
class Sender:
    """What the RECEIVER observed about the transport. Never what the sender claimed."""

    identity_kind: str
    identity_value: str
    machine_id: str
    stamped_at: str
    stamped_by: str

    def __post_init__(self) -> None:
        if self.identity_kind not in IDENTITY_KINDS:
            raise AttributionError(
                "sender identity must be an mTLS fingerprint or a Tailscale public key"
            )
        if not self.identity_value.strip():
            raise AttributionError("sender identity value is empty")
        for name in ("machine_id", "stamped_by"):
            if not _HOST_RE.fullmatch(getattr(self, name)):
                raise AttributionError(f"sender {name} is not a fleet host id")
        _parse_iso(self.stamped_at)

    def to_dict(self) -> dict[str, str]:
        return {
            "identity_kind": self.identity_kind,
            "identity_value": self.identity_value,
            "machine_id": self.machine_id,
            "stamped_at": self.stamped_at,
            "stamped_by": self.stamped_by,
        }

    @classmethod
    def from_mapping(cls, mapping: object) -> Sender:
        if not isinstance(mapping, dict):
            raise AttributionError("sender must be a mapping")
        return cls(
            identity_kind=str(mapping.get("identity_kind", "")),
            identity_value=str(mapping.get("identity_value", "")),
            machine_id=str(mapping.get("machine_id", "")),
            stamped_at=str(mapping.get("stamped_at", "")),
            stamped_by=str(mapping.get("stamped_by", "")),
        )


@dataclass(frozen=True)
class AttributionRecord:
    """One ``aih-attribution/v1`` record.

    ``user_code`` is MEMORY ONLY: it is excluded from :meth:`to_dict` by whitelist rather than by
    blacklist, kept out of ``repr`` and out of equality, and a sender that has it on hand is
    expected to pass it to :meth:`for_device_code` and then forget it.
    """

    kind: str
    initiating_host: str
    session_id: str
    launched_at: str
    expires_at: str
    profile: str
    source: str
    code_binding: CodeBinding | None = None
    sender: Sender | None = None
    user_code: str | None = field(default=None, repr=False, compare=False)
    #: :data:`KIND_APPROVAL_OUTCOME` only; ``None`` on every other kind (:data:`OUTCOME_FIELDS`).
    actor: str | None = None
    rung: str | None = None
    verdict: str | None = None
    at: str | None = None
    approving_host: str | None = None

    def _check_outcome_fields(self) -> None:
        if self.kind != KIND_APPROVAL_OUTCOME:
            if any(getattr(self, name) is not None for name in OUTCOME_FIELDS):
                raise AttributionError(
                    f"only an {KIND_APPROVAL_OUTCOME} record carries outcome fields"
                )
            return
        if self.code_binding is None:
            raise AttributionError(
                "an approval outcome without the launch's code binding names no launch"
            )
        if self.actor not in ACTORS:
            raise AttributionError("approval outcome actor is not a known actor")
        if self.verdict not in VERDICTS + DELIVERY_VERDICTS:
            raise AttributionError("approval outcome verdict is not a known verdict")
        if not isinstance(self.rung, str) or not _RUNG_RE.fullmatch(self.rung):
            raise AttributionError("approval outcome rung is not in the permitted form")
        if not isinstance(self.approving_host, str) or not _HOST_RE.fullmatch(
            self.approving_host
        ):
            raise AttributionError("approval outcome approving_host is not a host id")
        _parse_iso(self.at)

    def __post_init__(self) -> None:
        self._check_outcome_fields()
        if self.kind not in KINDS:
            raise AttributionError(f"unknown attribution kind: {self.kind!r}")
        if self.source not in SOURCES:
            raise AttributionError(f"unknown attribution source: {self.source!r}")
        for name, pattern in (
            ("initiating_host", _HOST_RE),
            ("session_id", _SESSION_RE),
        ):
            if not pattern.fullmatch(getattr(self, name)):
                raise AttributionError(f"{name} is not in the permitted form")
        if not self.profile.strip():
            raise AttributionError("profile is empty")
        if _parse_iso(self.expires_at) <= _parse_iso(self.launched_at):
            raise AttributionError("expires_at is not after launched_at")
        if self.kind == KIND_DEVICE_CODE_LAUNCH and self.code_binding is None:
            raise AttributionError(
                "a device-code-launch record without a code binding could never be matched "
                "against the code on the page"
            )

    # -- construction -----------------------------------------------------------------------

    @classmethod
    def for_device_code(  # noqa: PLR0913 - every field is a distinct record column.
        cls,
        *,
        user_code: str,
        initiating_host: str,
        session_id: str,
        profile: str,
        source: str,
        lifetime_s: int,
        launched_at: dt.datetime | None = None,
        kind: str = KIND_DEVICE_CODE_LAUNCH,
    ) -> AttributionRecord:
        """The sender side. Binds the code and keeps it in memory, never in the record.

        ``kind`` names WHO bound it: sso-reauth's own launch, or a declared harness emitter's URL
        open. Both carry a binding, so both are matched the same way where they are admitted.
        """

        launched = launched_at or _utcnow()
        if lifetime_s <= 0:
            raise AttributionError("a device-code record needs a positive lifetime")
        return cls(
            kind=kind,
            initiating_host=initiating_host,
            session_id=session_id,
            launched_at=_iso(launched),
            expires_at=_iso(launched + dt.timedelta(seconds=lifetime_s)),
            profile=profile,
            source=source,
            code_binding=CodeBinding.for_code(user_code),
            user_code=user_code,
        )

    @classmethod
    def approval_outcome(  # noqa: PLR0913 - every field is a distinct record column.
        cls,
        launch: AttributionRecord,
        *,
        actor: str,
        rung: str,
        verdict: str,
        approving_host: str,
        at: dt.datetime | None = None,
    ) -> AttributionRecord:
        """The APPROVER side: who approved ``launch``, addressed back to the host that made it.

        Copies the launch's columns and binding so the launcher can match it against its own
        ledger exactly; the code itself is never copied, even when ``launch`` holds it in memory.
        """

        return cls(
            kind=KIND_APPROVAL_OUTCOME,
            initiating_host=launch.initiating_host,
            session_id=launch.session_id,
            launched_at=launch.launched_at,
            expires_at=launch.expires_at,
            profile=launch.profile,
            source=launch.source,
            code_binding=launch.code_binding,
            actor=actor,
            rung=rung,
            verdict=verdict,
            at=_iso(at or _utcnow()),
            approving_host=approving_host,
        )

    # -- serialisation ----------------------------------------------------------------------

    def to_dict(self) -> dict:
        """WHITELIST, not blacklist: a field added to this class is absent until it is listed."""

        payload: dict = {
            "schema": SCHEMA,
            "kind": self.kind,
            "initiating_host": self.initiating_host,
            "session_id": self.session_id,
            "launched_at": self.launched_at,
            "expires_at": self.expires_at,
            "profile": self.profile,
            "source": self.source,
        }
        if self.code_binding is not None:
            payload["code_binding"] = self.code_binding.to_dict()
        if self.sender is not None:
            payload["sender"] = self.sender.to_dict()
        if self.kind == KIND_APPROVAL_OUTCOME:
            for name in OUTCOME_FIELDS:
                payload[name] = getattr(self, name)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def _from_mapping(cls, mapping: object, *, allow_sender: bool) -> AttributionRecord:
        if not isinstance(mapping, dict):
            raise AttributionError("an attribution record must be a JSON object")
        if mapping.get("schema") != SCHEMA:
            raise AttributionError(
                f"unsupported attribution schema: {mapping.get('schema')!r}"
            )
        if "user_code" in mapping:
            raise AttributionRefused(
                "a serialised record carrying a user_code is refused: the code is memory-only"
            )
        if "sender" in mapping and not allow_sender:
            raise AttributionRefused(
                "a transport payload may not carry its own sender block; only the receiver "
                "stamps an observed identity"
            )
        binding = mapping.get("code_binding")
        sender = mapping.get("sender")
        return cls(
            kind=str(mapping.get("kind", "")),
            initiating_host=_require_str(mapping, "initiating_host", _HOST_RE),
            session_id=_require_str(mapping, "session_id", _SESSION_RE),
            launched_at=_iso(_parse_iso(mapping.get("launched_at"))),
            expires_at=_iso(_parse_iso(mapping.get("expires_at"))),
            profile=str(mapping.get("profile", "")),
            source=str(mapping.get("source", "")),
            code_binding=None if binding is None else CodeBinding.from_mapping(binding),
            sender=None if sender is None else Sender.from_mapping(sender),
            **{
                name: None if mapping.get(name) is None else str(mapping[name])
                for name in OUTCOME_FIELDS
            },
        )

    @classmethod
    def from_transport_payload(cls, mapping: object) -> AttributionRecord:
        """UNTRUSTED input: what arrived over the wire. Refuses a self-asserted ``sender``."""

        return cls._from_mapping(mapping, allow_sender=False)

    @classmethod
    def from_local_mapping(cls, mapping: object) -> AttributionRecord:
        """A record THIS host wrote about its own launch. A ``sender`` is refused, not ignored.

        Refused rather than dropped on purpose: a stamped record read out of the local ledger
        would be one that reached an approval path WITHOUT :func:`verify_sender` ever running on
        it. Silently discarding the field would admit the record anyway.
        """

        record = cls._from_mapping(mapping, allow_sender=True)
        if record.sender is not None:
            raise AttributionRefused(
                "a stamped record in the local ledger would bypass the registry check that "
                "governs every record a transport delivered"
            )
        return record

    @classmethod
    def from_inbox_mapping(cls, mapping: object) -> AttributionRecord:
        """What this host's receiver already stamped. A record with no ``sender`` is refused."""

        record = cls._from_mapping(mapping, allow_sender=True)
        if record.sender is None:
            raise AttributionError(
                "an inbox record with no receiver-stamped sender attributes nothing"
            )
        return record

    @classmethod
    def from_peer_mapping(cls, mapping: object) -> AttributionRecord:
        """A record read out of a PEER fleet host's own launch ledger. A ``sender`` is refused.

        Spelled as its own reader rather than reusing :meth:`from_local_mapping` even though the
        two refusals coincide today, because the REASONS differ and only one of them is about
        this host. A peer's ledger holds records that host wrote about its own launches, so it
        contains no stamp for the same reason the local ledger contains none; but a stamp
        appearing in a PULLED payload would additionally be a stamp this host never applied,
        naming an identity no receiver here observed. Both readings refuse it, and a future
        change to one must not silently move the other.
        """

        record = cls._from_mapping(mapping, allow_sender=True)
        if record.sender is not None:
            raise AttributionRefused(
                "a pulled record carrying a sender block was stamped by nobody on this host; "
                "only a transport receiver here stamps an observed identity"
            )
        return record

    # -- the receiver's stamp ---------------------------------------------------------------

    def stamped(
        self,
        *,
        identity_kind: str,
        identity_value: str,
        machine_id: str,
        stamped_by: str,
        stamped_at: dt.datetime | None = None,
    ) -> AttributionRecord:
        """RECEIVER ONLY. Returns a copy carrying what the transport actually established."""

        if self.sender is not None:
            raise AttributionRefused("this record is already stamped")
        return replace(
            self,
            sender=Sender(
                identity_kind=identity_kind,
                identity_value=identity_value,
                machine_id=machine_id,
                stamped_at=_iso(stamped_at or _utcnow()),
                stamped_by=stamped_by,
            ),
        )

    # -- reading ----------------------------------------------------------------------------

    def effective_expiry(self, *, ttl_cap_s: int) -> dt.datetime:
        """CLAMPED: a sender does not get to extend the window it is trusted for."""

        launched = _parse_iso(self.launched_at)
        return min(
            _parse_iso(self.expires_at), launched + dt.timedelta(seconds=ttl_cap_s)
        )

    def is_live(self, *, now: dt.datetime, ttl_cap_s: int) -> bool:
        return now < self.effective_expiry(ttl_cap_s=ttl_cap_s)

    def matches_code(self, user_code: str) -> bool:
        return self.code_binding is not None and self.code_binding.matches(user_code)

    def attributed_by(self) -> str:
        return f"{self.initiating_host}/{self.session_id}"


# -- verification against the server-owned registry -----------------------------------------


def _agent_mail(repo_root: Path):
    """Lazily import Agent Mail. Deferred so a host with attribution UNCONFIGURED never does.

    ``services.agent_mail`` is a top-level repo package rather than an installed one, so the
    checkout root has to be on the path -- the same reason and the same remedy as
    ``src/ai_chassis/agent_mail_notifier.py``.
    """

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from services.agent_mail.authorization import (  # noqa: PLC0415
            AccessDenied,
            TransportIdentity,
        )
        from services.agent_mail.registry import MachineRegistry  # noqa: PLC0415
    except ImportError as exc:
        raise AttributionRefused(
            f"the machine registry is unreachable, so nothing can be attributed: {exc}"
        ) from exc
    return AccessDenied, TransportIdentity, MachineRegistry


def verify_sender(
    record: AttributionRecord,
    *,
    registry_path: Path,
    audit_path: Path,
    repo_root: Path,
) -> str:
    """Re-check the stamped identity against the server-owned registry. Returns the machine id.

    The stamp alone is a claim about a file in this host's state directory; the registry is an
    independent artifact the sender does not write. ``MachineRegistry.authenticate`` is reused
    rather than reimplemented: it refuses an unknown identity and a revoked machine, and audits
    both outcomes by identity HASH, never by value.

    Deliberately NOT called: ``authorize_resource``. Phase 1 performs an ADMISSION check only;
    whether Agent Mail's example-client-host-namespace boundary covers ``example-project-ec2`` is an open question for
    that boundary's owner (`ISSUE-REF`, T-07 phase 2), and a worker does not reinterpret a
    fail-closed security boundary mid-implementation.
    """

    if record.sender is None:
        raise AttributionError("an unstamped record attributes nothing")
    if not registry_path.exists():
        raise AttributionRefused(
            "no machine registry on this host, so no foreign host is known"
        )
    access_denied, transport_identity, machine_registry = _agent_mail(repo_root)
    try:
        identity = transport_identity(
            kind=record.sender.identity_kind, value=record.sender.identity_value
        )
    except ValueError as exc:
        raise AttributionRefused(
            f"the stamped transport identity is invalid: {exc}"
        ) from exc
    # The MANIFEST path is the registry's conventional sibling and is deliberately not forced to
    # exist: `authenticate` never reads it, and `enroll` -- which does -- is not on this path and
    # fails closed if it is ever reached from here.
    registry = machine_registry(
        registry_path, audit_path, registry_path.with_name("machine-manifest.json")
    )
    try:
        machine = registry.authenticate(identity)
    except access_denied as exc:
        raise AttributionRefused(
            f"the registry refused the stamped identity: {exc}"
        ) from exc
    if machine.machine_id != record.initiating_host:
        raise AttributionRefused(
            "the stamped identity belongs to a different machine than the record names"
        )
    return machine.machine_id


# -- the record stores ----------------------------------------------------------------------


class _RecordStore:
    """A directory of records, purged at each record's clamped expiry.

    One file per record, named from the host and launch time plus random bytes: the name carries
    no ``user_code`` either, because a directory listing is as readable as a file.

    Subclasses differ ONLY in which records they admit -- :meth:`write`'s guard and
    :meth:`_read`'s loader. Everything below those two is the same directory discipline, shared
    rather than copied so a purge or TTL fix cannot land in one store and miss the other.
    """

    #: Which record kinds :meth:`match_device_code` may attribute from. Narrowest by default; only
    #: :class:`LocalLaunchLedger` widens it, because only there is a URL open THIS host's fact.
    MATCHABLE_KINDS: tuple[str, ...] = (KIND_DEVICE_CODE_LAUNCH,)

    def __init__(self, directory: Path, *, ttl_cap_s: int) -> None:
        if ttl_cap_s <= 0:
            raise AttributionError("the attribution TTL cap must be positive")
        self.directory = Path(directory)
        self.ttl_cap_s = int(ttl_cap_s)

    def _load(self, mapping: object) -> AttributionRecord:
        raise NotImplementedError

    def path_for(self, record: AttributionRecord) -> Path:
        stem = "-".join(
            (
                record.kind,
                _FILE_SAFE_RE.sub("_", record.initiating_host),
                _FILE_SAFE_RE.sub("_", record.launched_at),
                secrets.token_hex(4),
            )
        )
        return self.directory / f"{stem}.json"

    def _admit_for_write(self, record: AttributionRecord) -> None:
        raise NotImplementedError

    def write(self, record: AttributionRecord) -> Path:
        """Durably place one record this store admits, or refuse it."""

        self._admit_for_write(record)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.path_for(record)
        temp = path.with_name(path.name + ".partial")
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(record.to_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        return path

    def _entries(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return sorted(self.directory.glob("*.json"))

    def purge(self, *, now: dt.datetime | None = None) -> int:
        """Delete every record past its clamped expiry, and every unreadable one."""

        moment = now or _utcnow()
        removed = 0
        for path in self._entries():
            try:
                record = self._read(path)
            except (AttributionError, OSError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
                removed += 1
                continue
            if not record.is_live(now=moment, ttl_cap_s=self.ttl_cap_s):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def _read(self, path: Path) -> AttributionRecord:
        return self._load(json.loads(path.read_text(encoding="utf-8")))

    def live_records(
        self, *, kind: str, now: dt.datetime | None = None
    ) -> list[AttributionRecord]:
        """Every live record of one kind. Purges as it goes, so a read bounds the directory."""

        moment = now or _utcnow()
        self.purge(now=moment)
        records = []
        for path in self._entries():
            try:
                record = self._read(path)
            except (AttributionError, OSError, json.JSONDecodeError):
                continue
            if record.kind == kind and record.is_live(
                now=moment, ttl_cap_s=self.ttl_cap_s
            ):
                records.append(record)
        return records

    def match_device_code(
        self,
        user_code: str,
        *,
        profile: str,
        now: dt.datetime | None = None,
    ) -> AttributionRecord | None:
        """The POSITIVE match the approval is gated on. ``None`` means "show the human"."""

        for kind in self.MATCHABLE_KINDS:
            for record in self.live_records(kind=kind, now=now):
                if record.profile == profile and record.matches_code(user_code):
                    return record
        return None


class AttributionInbox(_RecordStore):
    """The RECEIVING END OF A TRANSPORT: records that crossed a wire and were stamped here.

    Every record here is subject to :func:`verify_sender` against the server-owned machine
    registry, because its ``initiating_host`` is a claim made by another machine.
    """

    def _admit_for_write(self, record: AttributionRecord) -> None:
        """RECEIVER ONLY: an unstamped record is refused rather than written unattributable."""

        if record.sender is None:
            raise AttributionRefused(
                "only a transport receiver writes the inbox, and only a stamped record"
            )

    def _load(self, mapping: object) -> AttributionRecord:
        return AttributionRecord.from_inbox_mapping(mapping)


class LocalLaunchLedger(_RecordStore):
    """Records THIS host wrote about device-code logins IT launched. No transport, no sender.

    WHY THIS IS NOT :class:`AttributionInbox`. The inbox is one end of a wire, so a record in it
    is refused unless a receiver stamped it and :func:`verify_sender` can re-check that stamp
    against the server-owned registry. A record this host wrote about its own login crossed
    nothing: there is no sender identity to authenticate, and demanding one would make the LOCAL
    half depend on the cross-machine registry (`ISSUE-REF`, unbuilt). Keeping both kinds in one
    directory would mean a reader that requires a stamp only sometimes, which is how a
    fail-closed guard quietly becomes conditional. Two directories, two readers, one schema.

    THE TRUST ANCHOR IS THE DIRECTORY, and it is THE SAME ANCHOR THE APPROVAL ALREADY RUNS ON.
    Today ``sso-reauth`` approves the code named by the ``in_flight`` record in this host's 0700
    SSO state directory; anyone who can write there can already name a code. This ledger widens
    no trust -- it persists that same fact with a TTL cap, so a code stays attributable for its
    own lifetime instead of only while a single per-profile ``in_flight`` slot happens to hold
    it. That is what makes two concurrent codes, and a page still open after the login finished,
    decidable on each code's own evidence rather than on which one got the slot.

    A RECORD CARRYING A ``sender`` IS REFUSED, on write and on read. A stamped record is a claim
    about another machine and belongs in the inbox, where the registry governs it; admitting one
    here would let a planted stamp skip :func:`verify_sender` entirely.

    TWO KINDS ARE EVIDENCE HERE, and only here (`ISSUE-REF`, D-16 (c)). ``device-code-launch`` is
    sso-reauth's own login; ``url-open`` is a login a DECLARED harness emitter (an SSM ProxyCommand)
    ran through the instrumented opener, which bound the code it was handed. Same directory, same
    anchor, same exact binding match -- D-7 (a), "codes this host launched", read per launcher.
    The opener writes a ``url-open`` record ONLY for a declared emitter: ``BROWSER`` reaches it from
    every login shell, so recording undeclared opens would approve whatever any local process
    opened, which is D-7's rejected option (b).
    """

    MATCHABLE_KINDS = (KIND_DEVICE_CODE_LAUNCH, KIND_URL_OPEN)

    def _admit_for_write(self, record: AttributionRecord) -> None:
        if record.sender is not None:
            raise AttributionRefused(
                "a stamped record describes a transport and belongs in the inbox, where the "
                "machine registry governs it; the local ledger holds only this host's own "
                "launches"
            )

    def _load(self, mapping: object) -> AttributionRecord:
        return AttributionRecord.from_local_mapping(mapping)


class PeerLaunchLedger(_RecordStore):
    """Records PULLED from a peer fleet host's own launch ledger over an authenticated channel.

    WHY THIS IS A THIRD STORE AND NOT EITHER OF THE OTHER TWO. The three differ in the one thing
    that matters -- WHAT MAKES A RECORD HERE EVIDENCE -- and :class:`LocalLaunchLedger` already
    records why that forbids sharing a directory: a reader that demands a stamped sender only
    sometimes is how a fail-closed guard quietly becomes conditional. So:

    * :class:`AttributionInbox` -- a record arrived unsolicited over a transport, so its
      ``initiating_host`` is another machine's CLAIM and :func:`verify_sender` must re-check the
      receiver's stamp against the server-owned registry.
    * :class:`LocalLaunchLedger` -- this host wrote it about its own login. It crossed nothing;
      the anchor is the 0700 directory, which is the anchor the approval already ran on.
    * This store -- THE ANCHOR IS THAT WE DIALLED THE PEER OURSELVES. The record did not arrive;
      this host opened an IAM-authenticated channel to a host named in ITS OWN local config and
      read that host's ledger. There is no asserted identity to authenticate, because the
      identity was chosen locally before a byte moved, which is why no machine registry gates
      this tier (D-1 of `docs/plans/issue-ref-ssm-pull-device-code-attribution.md`: "IAM
      supplies the sender proof"). The one PUSHED route is D-18 (d)'s Buzz DM
      (`ai_chassis.attribution_buzz`, `ISSUE-REF`), admitted only from a sealed sender the
      operator listed in `[buzz_notify].allow_senders` and only through :meth:`ingest`, the
      pull's own call. Either way an attacker would have to already control a key or a host the
      operator allow-listed.

    WHAT IS NOT WIDENED. The positive match is unchanged and is still the only thing that can
    attribute anything: an exact ``CodeBinding`` match against the code read off the page, inside
    the record's TTL-clamped lifetime, via :meth:`_RecordStore.match_device_code`. Approving
    whatever appears in the window remains the rejected consent-phishing option.

    ``initiating_host`` IS NOT CHECKED AGAINST THE HOST WE DIALLED, deliberately. It is
    documented DIAGNOSTIC-not-credential at :func:`local_host_id`, and measured: example-project-ec2
    writes ``ip-100-120-40-17`` while the operator reaches it by an ssh alias. Requiring the two
    to agree would refuse every real record for a cosmetic reason -- and it would buy nothing,
    since the trust came from the channel, not from a string inside the payload. Which
    allow-listed peer supplied a record is therefore not distinguished here: all of them are
    equally trusted by construction, so conflating them widens nothing.
    """

    def __init__(
        self,
        directory: Path,
        *,
        ttl_cap_s: int,
        profile_aliases: dict[str, str] | None = None,
        clock_skew_s: int = PEER_CLOCK_SKEW_S,
    ) -> None:
        super().__init__(directory, ttl_cap_s=ttl_cap_s)
        #: A peer's ``profile`` is ITS OWN host-local label for an AWS profile, so comparing it to
        #: this host's label is only valid where the two are spelled identically -- and MEASURED
        #: 2026-10-07 they are not: example-client-host-mac calls it
        #: ``poc-discoveryhub-full-access-474504478031`` and example-project-ec2 calls the same account
        #: ``poc-discoveryhub-fullaccess-474504478031``. This map is the operator DECLARING two
        #: names equal, applied on read so the comparison downstream stays an exact one. Empty by
        #: default: an undeclared foreign profile name attributes nothing.
        self.profile_aliases = dict(profile_aliases or {})
        self.clock_skew_s = int(clock_skew_s)

    def _admit_for_write(self, record: AttributionRecord) -> None:
        if record.sender is not None:
            raise AttributionRefused(
                "a stamped record describes a transport this host received and belongs in the "
                "inbox, where the machine registry governs it; this store holds only what a "
                "peer's own launch ledger said"
            )

    def _load(self, mapping: object) -> AttributionRecord:
        """Parse, refuse a future-dated launch, and apply the declared profile alias.

        The alias is applied HERE, at the trust boundary, rather than at the comparison: it is a
        rename the operator declared, so by the time any matcher sees the record there is one
        profile vocabulary and nothing downstream has to know a second one exists.
        """

        record = AttributionRecord.from_peer_mapping(mapping)
        horizon = _utcnow() + dt.timedelta(seconds=self.clock_skew_s)
        if _parse_iso(record.launched_at) > horizon:
            raise AttributionError(
                "a pulled record claims it was launched in the future, which would extend the "
                "window it is trusted for rather than fit inside it"
            )
        renamed = self.profile_aliases.get(record.profile)
        if renamed is None:
            return record
        return replace(record, profile=renamed)

    def holds(
        self, record: AttributionRecord, *, now: dt.datetime | None = None
    ) -> bool:
        """Whether a live record with this exact binding is already stored.

        Compared on the BINDING rather than on a file name so re-pulling the same peer record on
        every tick is idempotent. Not a secret comparison: both sides are digests this host
        already holds on local disk, so constant time would buy nothing here -- the constant-time
        compare that matters is :meth:`CodeBinding.matches`, against the code on the page.
        """

        return any(
            stored.code_binding == record.code_binding
            for stored in self.live_records(kind=record.kind, now=now)
        )

    def ingest(self, mapping: object, *, now: dt.datetime | None = None) -> Path | None:
        """Admit one pulled mapping, or raise. ``None`` means "already stored, nothing to do"."""

        record = self._load(mapping)
        if self.holds(record, now=now):
            return None
        return self.write(record)


class ApprovalOutcomeStore:
    """The LAUNCHING host's record of who a peer approver says approved its code (`ISSUE-REF`).

    One ``<first 16 hex of the launch binding>.json`` per launch: the same key sso-approve files
    its own outcome under and an escalation's ``approval_ref`` names, so a reader resolves either
    store with one name. A later outcome for the same launch replaces the earlier one.

    THE GUARD IS THE LAUNCH THIS HOST WROTE. :meth:`admit` writes only after the record's binding
    equals a live record in this host's own :class:`LocalLaunchLedger`, so a sender can report on
    a launch this host made and on nothing else. The caller has already checked the sender against
    ``[buzz_notify].allow_senders``; nothing here can approve anything.

    The same class, rooted at :data:`PEER_DELIVERY_NAME`, holds a peer's delivery verdicts
    (`ISSUE-REF`): one admission guard for both, two directories so neither reader sees the other.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    @staticmethod
    def ref_for(record: AttributionRecord) -> str:
        if record.code_binding is None:
            raise AttributionError("a record without a code binding has no outcome key")
        return record.code_binding.value[:16]

    def admit(
        self,
        record: AttributionRecord,
        *,
        local_ledger: LocalLaunchLedger,
        now: dt.datetime | None = None,
    ) -> Path:
        """Write one outcome for a launch this host made, or raise. Guard first, then write."""

        if record.kind != KIND_APPROVAL_OUTCOME:
            raise AttributionRefused(f"not an {KIND_APPROVAL_OUTCOME} record")
        launched = any(
            stored.code_binding == record.code_binding
            for kind in LocalLaunchLedger.MATCHABLE_KINDS
            for stored in local_ledger.live_records(kind=kind, now=now)
        )
        if not launched:
            raise AttributionRefused("no-local-launch")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / f"{self.ref_for(record)}.json"
        temp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.partial")
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(record.to_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        return path

    def read(self, ref: str) -> AttributionRecord | None:
        """The outcome filed under ``ref``, or ``None`` for absent or unreadable."""

        if not _HEX_RE.fullmatch(ref or ""):
            return None
        path = self.directory / f"{ref}.json"
        try:
            record = AttributionRecord.from_transport_payload(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (AttributionError, OSError, ValueError):
            return None
        return record if record.kind == KIND_APPROVAL_OUTCOME else None
