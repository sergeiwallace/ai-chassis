"""A peer's device-code launch record over Buzz, ADDED to the D-17 SSM pull (`ISSUE-REF`).

D-18 (d) of ``docs/designs/issue-ref-sso-device-code-auto-approval.md``, approved 2026-10-10 as
belt-and-suspenders: when example-project-ec2 writes a launch record, it also sends that record to example-client-host-mac
as a Buzz DM, so the Mac's approver holds it within one notifier tick instead of waiting for the
sentinel's next pull. The pull is unchanged and stays the path that works when Buzz is down.

THE ADMISSION PATH IS THE PULL'S, NOT A NEW ONE. A Buzz-delivered record goes through
:meth:`attribution_record.PeerLaunchLedger.ingest`, the same call the sentinel makes for every
pulled mapping, so it is parsed by ``from_peer_mapping`` (a ``user_code`` or a ``sender`` block is
refused, a future-dated launch is refused) and can only ever attribute a code through the
ledger's exact salted-HMAC :class:`attribution_record.CodeBinding` match against the code read
off the page -- D-7's predicate, unmoved. Nothing here can approve anything.

THE SENDER PROOF. The pull's anchor is that this host dialled a peer named in its own config.
A DM is pushed, so the equivalent anchor is the sealed NIP-59 sender: the seal is Schnorr-signed
by the sender's key (``unwrap_gift`` verifies it), and the key must be one the operator listed in
``[buzz_notify].allow_senders`` after checking it out of band. An unlisted sender's record is
refused before it is parsed. That list is the existing vetted-peer list rather than a second
one, so there is one place an operator revokes a peer.

NEVER FAILS A LOGIN, NEVER LOGS A CODE. The sender side runs inside the login's browser handoff,
so every failure becomes an outcome word and the login continues; the record carries no code to
log in the first place.

THE RETURN LEG (`ISSUE-REF`, ISSUE-REF D-3 (a)). When the approving host's approver exits for a
code a peer launched, its sentinel DMs an ``approval-outcome`` record to the daemon of every other
member of its transport pair (:func:`push_approval_outcome`). The launching host's notifier admits
it through :func:`admit`, behind the same sender gate, into
:class:`attribution_record.ApprovalOutcomeStore`, which writes only for a binding the receiver's
own launch ledger holds. A mis-addressed outcome is therefore refused by the host it reached.

THE DELIVERY VERDICT (`ISSUE-REF`, ISSUE-REF T-09). The launching host's VS Code rung exits 0
whether or not the URL ever reached the approving host's browser (measured 2026-10-13: two of four
rc=0 opens never appeared there). The approving host holds the facts, so it says which: when its
look matches an open device page to a peer launch record's binding it publishes ``url-delivered``
with the first-seen time (:func:`publish_url_delivery`), and when :func:`delivery_window_s` passes
with no such match its notifier publishes ``url-not-seen`` (:func:`due_not_seen`). Both are an
``approval-outcome`` record on the T-04 leg, admitted by the same guard into a sibling store, and
each launch gets exactly one (:func:`claim_url_delivery`). A send that does not land is resent, the
same record, until the launcher's wait would end (:func:`send_until_delivered`, ISSUE-REF). The
launcher's handoff waits for it, bounded by :func:`delivery_wait_s` (:func:`await_url_delivery`);
no verdict leaves rc=0 standing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_chassis import attribution_record
from ai_chassis.session_address import WILDCARD, AddressError, parse_address

SECTION = "sso_reauth"
#: Seconds the sender may spend on one push. The handoff waits for it before opening the URL, so
#: a relay that hangs must cost seconds, not the login.
DEFAULT_SEND_TIMEOUT_S = 5

ADMITTED = "admitted"
ALREADY_HELD = "already-held"
SENDER_NOT_ALLOWED = "sender-not-allowed"
#: The refusal words for an approval outcome. Words, never a value: the log is what reads them.
PLAINTEXT_USER_CODE = "plaintext-user-code"
NO_LOCAL_LAUNCH = "no-local-launch"
NO_PEER_IN_PAIR = "no-peer-in-pair"

#: The approving host's ceiling for "the device page appeared", per launch. Measured 2026-10-13: a
#: delivered page appeared 2-3 s after its launch; the rest is the watcher's poll, a look that may
#: wait on the look lock, and the record's own arrival. Capped at a quarter of the device-code
#: lifetime, so on any configured lifetime the launcher still has most of it for the next rung.
DELIVERY_WINDOW_S = 60
#: What the launcher adds on top for the verdict to cross back: the approver's notifier tick
#: (10 s shipped, 30 s at most), the send bound, and the launcher's own tick.
DELIVERY_WAIT_SLACK_S = 30
#: Where the APPROVING host records that a launch already has its verdict. One file per launch.
URL_DELIVERY_SENT_NAME = "url-delivery-sent"
#: How many times one return-leg record is sent before it is reported NOT delivered
#: (ISSUE-REF). The sentinel's send builds a fresh channel, whose first send paid the daemon's
#: whole start-up (a new socket through the SSM port-forward, AUTH, REQ, the `kind 0` announce, the
#: NIP-29 self-add, each waiting up to 3 s for an `OK`) inside the one send bound. Measured
#: 2026-10-21: 3 of 61 sends timed out that way and the next send on a fresh channel landed in all 3.
#: A one-shot send now dials and AUTHs one socket and nothing else (ISSUE-REF); the resend stays
#: for a socket or port-forward that is slow to answer at all.
URL_DELIVERY_ATTEMPTS = 3
#: Seconds before the second and third attempt.
URL_DELIVERY_BACKOFF_S = (1.0, 2.0)
#: Outcomes no resend can change: this host is not configured to send at all.
_UNRETRYABLE = (
    "not sent: disabled",
    "not sent: buzz-not-enabled",
    f"not sent: {NO_PEER_IN_PAIR}",
)


@dataclass(frozen=True)
class Settings:
    """The ``attribution_buzz_*`` keys plus the ledger keys the sentinel and notifier read."""

    enabled: bool
    recipient: str
    send_timeout_s: int
    peer_ledger_dir: Path
    ttl_cap_s: int
    local_ledger_dir: Path
    outcome_dir: Path
    profile_aliases: dict[str, str] = field(default_factory=dict)
    delivery_dir: Path = Path(attribution_record.PEER_DELIVERY_NAME)
    delivery_sent_dir: Path = Path(URL_DELIVERY_SENT_NAME)


def state_dir(env: dict[str, str] | None = None) -> Path:
    """``sso_reauth.state_dir()``, with the environment injectable. Pinned equal by the suite."""

    environment = os.environ if env is None else env
    root = environment.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(root) / "ai-harness" / "sso-reauth"


def settings_from(
    section: dict[str, Any], *, env: dict[str, str] | None = None
) -> Settings:
    """Settings from one ``[sso_reauth]`` table. Disabled whenever a value it needs is unusable."""

    recipient = str(section.get("attribution_buzz_recipient") or "").strip()
    raw_timeout = section.get("attribution_buzz_send_timeout_s")
    timeout = (
        raw_timeout
        if isinstance(raw_timeout, int)
        and not isinstance(raw_timeout, bool)
        and raw_timeout > 0
        else DEFAULT_SEND_TIMEOUT_S
    )
    raw_ttl = section.get("attribution_ttl_cap_s") or section.get("approve_timeout_s")
    ttl = raw_ttl if isinstance(raw_ttl, int) and not isinstance(raw_ttl, bool) else 0
    aliases = section.get("attribution_peer_profiles")
    return Settings(
        enabled=section.get("attribution_buzz_push") is True
        and bool(_recipient_machine(recipient))
        and ttl > 0,
        recipient=recipient,
        send_timeout_s=timeout,
        peer_ledger_dir=_state_path(
            section, "attribution_peer_ledger", attribution_record.PEER_LEDGER_NAME, env
        ),
        ttl_cap_s=ttl,
        local_ledger_dir=_state_path(
            section,
            "attribution_local_ledger",
            attribution_record.LOCAL_LEDGER_NAME,
            env,
        ),
        outcome_dir=state_dir(env) / attribution_record.PEER_OUTCOME_NAME,
        delivery_dir=state_dir(env) / attribution_record.PEER_DELIVERY_NAME,
        delivery_sent_dir=state_dir(env) / URL_DELIVERY_SENT_NAME,
        profile_aliases={
            str(key): str(value)
            for key, value in (aliases if isinstance(aliases, dict) else {}).items()
            if str(key).strip() and str(value).strip()
        },
    )


def _state_path(
    section: dict[str, Any], key: str, default: str, env: dict[str, str] | None
) -> Path:
    """A ledger directory key, resolved the way the sentinel and sso-reauth resolve it."""
    path = Path(str(section.get(key) or "").strip() or default).expanduser()
    return path if path.is_absolute() else state_dir(env) / path


def _recipient_machine(recipient: str) -> str:
    """The host of a concrete `<session>@<host>` recipient (ISSUE-REF), or ""."""
    try:
        address = parse_address(recipient)
    except AddressError:
        return ""
    return "" if WILDCARD in (address.slug, address.machine) else address.machine


def payload_kind(text: object) -> str:
    """The ``kind`` of a DM whose text is an ``aih-attribution/v1`` record, else ""."""

    if not isinstance(text, str) or not text.lstrip().startswith("{"):
        return ""
    try:
        document = json.loads(text)
    except ValueError:
        return ""
    if (
        not isinstance(document, dict)
        or document.get("schema") != attribution_record.SCHEMA
    ):
        return ""
    return str(document.get("kind") or "") or "unknown"


def is_record_payload(text: object) -> bool:
    """Whether a DM's text is an ``aih-attribution/v1`` record rather than a message."""

    return bool(payload_kind(text))


def is_approval_outcome_payload(text: object) -> bool:
    """Whether a DM's text is an ``approval-outcome`` record (`ISSUE-REF`)."""

    return payload_kind(text) == attribution_record.KIND_APPROVAL_OUTCOME


def is_url_delivery_payload(text: object) -> bool:
    """Whether a DM's text is an ``approval-outcome`` carrying a delivery verdict (`ISSUE-REF`)."""

    if not is_approval_outcome_payload(text):
        return False
    return json.loads(str(text)).get("verdict") in attribution_record.DELIVERY_VERDICTS


def wire_text(record: attribution_record.AttributionRecord) -> str:
    """The DM body: the record's own whitelisted serialisation, compact."""

    return json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))


def _refusal_before_send(channel: Any, recipient: str) -> str:
    """Why nothing may be published, or "". Decided before any key is minted or socket dialled."""

    if channel is None or not channel.enabled:
        return "buzz-not-enabled"
    target_machine = _recipient_machine(recipient)
    if target_machine == channel.machine:
        return "recipient-is-this-machine"
    return channel.refuse_cross_pair(target_machine)


async def _send_one(
    channel: Any, recipient: str, text: str, timeout_s: int, *, one_shot: bool = False
) -> str:
    """DM ``text`` to one recipient over ``channel``. One outcome word; the channel stays open.

    ``one_shot`` when the caller built ``channel`` for this send: the send then runs none of the
    daemon's start-up (D-27 in the Buzz design), so a slow announce or self-add ``OK`` cannot
    spend the bound (ISSUE-REF).
    """

    try:
        outcome = await asyncio.wait_for(
            channel.send_direct(
                recipient,
                channel.resolve_recipient_pubkey(recipient),
                text,
                dm_only=True,
                one_shot=one_shot,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        return f"timed-out after {timeout_s}s"
    if outcome.sent:
        return f"sent via {outcome.delivered_via}"
    return f"refused ({outcome.reason})"


def _send_all(
    channel: Any, recipients: list[str], text: str, timeout_s: int
) -> list[str]:
    """DM ``text`` to each recipient over one channel, then close it. One outcome word each.

    DM ONLY: ``send_direct``'s machine-channel mention fallback is refused, because a fallback
    would publish the record to every member of that channel. Never raises.
    """

    async def send() -> list[str]:
        try:
            return [
                await _send_one(channel, recipient, text, timeout_s, one_shot=True)
                for recipient in recipients
            ]
        finally:
            await channel.aclose()

    try:
        return asyncio.run(send())
    except Exception as exc:  # noqa: BLE001 - a push never fails the login it rides on
        return [f"failed ({type(exc).__name__})"] * len(recipients)


def push_record(
    record: attribution_record.AttributionRecord,
    settings: Settings,
    *,
    channel_factory: Any,
) -> str:
    """Send one launch record to ``settings.recipient`` as a DM. Returns an outcome word."""

    if not settings.enabled:
        return "disabled"
    try:
        channel = channel_factory()
    except Exception as exc:  # noqa: BLE001 - a push never fails the login it rides on
        return f"buzz-unavailable ({type(exc).__name__})"
    refusal = _refusal_before_send(channel, settings.recipient)
    if refusal:
        return refusal
    return _send_all(
        channel, [settings.recipient], wire_text(record), settings.send_timeout_s
    )[0]


def push_approval_outcome(
    record: attribution_record.AttributionRecord,
    settings: Settings,
    *,
    channel_factory: Any,
) -> str:
    """DM one approval outcome to the daemon of each other member of this host's pair.

    Returns ONE line's worth of outcome (``to <recipient>: <word>``, or ``not sent: <word>``), so
    the caller logs once per approval. Never raises: the approval it reports has already happened,
    and a Buzz fault must leave it exactly as it was (D-18 (d)'s fail-open rule).
    """

    if not settings.enabled:
        return "not sent: disabled"
    try:
        channel = channel_factory()
    except Exception as exc:  # noqa: BLE001 - a push never fails the approval it reports
        return f"not sent: buzz-unavailable ({type(exc).__name__})"
    refusal = _refusal_before_peer_send(channel)
    if refusal:
        return refusal
    recipients = _peer_daemons(channel)
    outcomes = _send_all(
        channel, recipients, wire_text(record), settings.send_timeout_s
    )
    return _joined(recipients, outcomes)


async def send_to_peer_daemons(
    record: attribution_record.AttributionRecord, channel: Any, *, timeout_s: int
) -> str:
    """:func:`push_approval_outcome`'s send over a channel the CALLER owns and keeps open.

    For the notifier, whose live channel runs on its own loop: closing it here would end that
    daemon's subscription. Same recipients, same one-line outcome, never raises.
    """

    refusal = _refusal_before_peer_send(channel)
    if refusal:
        return refusal
    recipients = _peer_daemons(channel)
    try:
        outcomes = [
            await _send_one(channel, recipient, wire_text(record), timeout_s)
            for recipient in recipients
        ]
    except Exception as exc:  # noqa: BLE001 - a verdict push never fails the tick it rides on
        return f"not sent: failed ({type(exc).__name__})"
    return _joined(recipients, outcomes)


def send_until_delivered(
    record: attribution_record.AttributionRecord,
    send: Callable[[attribution_record.AttributionRecord], str],
    *,
    deadline: dt.datetime | None = None,
    clock: Callable[[], dt.datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """``send(record)`` until a recipient has it, up to :data:`URL_DELIVERY_ATTEMPTS`. One line.

    Resends the SAME record, so the receiver's one-file-per-launch store holds one verdict however
    many attempts it took; a recipient that already has it is never sent it again, because an
    outcome naming any ``sent via`` ends the loop. ``deadline`` is when the receiver stops reading:
    no attempt starts after it. Returns ``<outcome> on attempt N of M`` when delivered, else a line
    beginning ``NOT delivered``.
    """

    now = clock or (lambda: dt.datetime.now(dt.timezone.utc))
    attempt = 0
    while True:
        attempt += 1
        outcome = send(record)
        if "sent via" in outcome:
            return f"{outcome} on attempt {attempt} of {URL_DELIVERY_ATTEMPTS}"
        if attempt >= URL_DELIVERY_ATTEMPTS or outcome.startswith(_UNRETRYABLE):
            break
        pause = URL_DELIVERY_BACKOFF_S[attempt - 1]
        if deadline is not None and now() + dt.timedelta(seconds=pause) >= deadline:
            break
        sleep(pause)
    plural = "" if attempt == 1 else "s"
    return f"NOT delivered after {attempt} attempt{plural} (last: {outcome})"


def _refusal_before_peer_send(channel: Any) -> str:
    if channel is None or not channel.enabled:
        return "not sent: buzz-not-enabled"
    if not _peer_daemons(channel):
        return f"not sent: {NO_PEER_IN_PAIR}"
    return ""


def _peer_daemons(channel: Any) -> list[str]:
    """The daemon identity of every other member of this host's transport pair."""

    return _pair_daemons(channel.pair, channel.machine)


def _pair_daemons(pair: dict[str, Any] | None, machine: str) -> list[str]:
    from ai_chassis.buzz_channel import daemon_identity_name  # noqa: PLC0415

    return [
        daemon_identity_name(member)
        for member in (pair or {}).get("members") or []
        if member != machine
    ]


def _joined(recipients: list[str], outcomes: list[str]) -> str:
    return "; ".join(
        f"to {recipient}: {outcome}"
        for recipient, outcome in zip(recipients, outcomes, strict=True)
    )


# -- the allow-list check (ISSUE-REF) --------------------------------------------------------

#: The prefix of every doctor line this check emits.
ALLOW_LIST_FINDING = "buzz allow_senders:"


def peer_daemon_allow_findings(
    pair: dict[str, Any] | None,
    machine: str,
    directory: dict[str, dict[str, Any]],
    allow_senders: frozenset[str],
) -> list[str]:
    """One line per paired host's daemon whose pubkey :func:`admit` would refuse.

    REPORTS, NEVER ADMITS. The allow-list is the operator's vetted-peer list and also decides
    which senders bypass the D-8 hold, so a key is listed by hand after an out-of-band check.
    This only names the gap the return leg (T-04 outcomes, T-09 verdicts) falls into. A daemon
    with no ``pubkeys.json`` entry is named too: its key cannot be checked, and an unlisted key
    is refused exactly like a wrong one.
    """

    from ai_chassis.buzz_channel import (  # noqa: PLC0415
        PUBKEY_DIRECTORY_FILE,
        legacy_identity_name,
    )

    findings: list[str] = []
    for name in _pair_daemons(pair, machine):
        entry = directory.get(name) or directory.get(legacy_identity_name(name)) or {}
        pubkey = str(entry.get("pubkey") or "").strip()
        if not pubkey:
            findings.append(
                f"{ALLOW_LIST_FINDING} {name} has no pubkey in {PUBKEY_DIRECTORY_FILE}, so "
                "this host cannot check that [buzz_notify].allow_senders admits it; until "
                "its pubkey is listed there, its approval outcomes and url delivery verdicts "
                f"are refused as {SENDER_NOT_ALLOWED}"
            )
        elif pubkey not in allow_senders:
            findings.append(
                f"{ALLOW_LIST_FINDING} {name} (pubkey {pubkey}) is missing from "
                f"[buzz_notify].allow_senders on {machine}, so its approval outcomes and url "
                f"delivery verdicts are refused as {SENDER_NOT_ALLOWED}; list that pubkey "
                "after checking it out of band"
            )
    return findings


def allow_list_findings(
    machine: str,
    *,
    env: dict[str, str] | None = None,
    repo_root: Path | None = None,
    buzz_config_path: Path | None = None,
) -> list[str]:
    """:func:`peer_daemon_allow_findings` for this host's real pair, config and directory.

    Nothing to report when the host is in no pair or Buzz is not enabled for it: no record can
    arrive to be refused. Resolved through the notifier's own loaders, so the doctor reads the
    list the running gate reads.
    """

    from ai_chassis import buzz_channel, message_transports  # noqa: PLC0415

    transports = message_transports.load_tracked_config(repo_root=repo_root, env=env)
    pair = message_transports.select_pair(transports, machine)
    if pair is None:
        return []
    config = buzz_channel.load_buzz_config(
        buzz_config_path, pair=str(pair.get("name") or ""), env=env
    )
    if config.get("enabled") is not True:
        return []
    senders = config.get("allow_senders") or []
    directory = buzz_channel.load_pubkey_directory(
        buzz_channel.pubkey_directory_path(buzz_channel.buzz_key_dir(env))
    )
    return peer_daemon_allow_findings(
        pair,
        machine,
        directory,
        frozenset(item for item in senders if isinstance(item, str)),
    )


# -- the delivery verdict (ISSUE-REF) --------------------------------------------------------


def delivery_window_s(ttl_cap_s: int) -> int:
    """How long after a launch the approving host waits to see its page before saying not-seen."""

    return max(1, min(DELIVERY_WINDOW_S, int(ttl_cap_s) // 4))


def delivery_wait_s(ttl_cap_s: int) -> int:
    """How long after a launch the LAUNCHING host waits for the verdict. Always past the window."""

    window = delivery_window_s(ttl_cap_s)
    return window + max(1, min(DELIVERY_WAIT_SLACK_S, window // 2))


def claim_url_delivery(
    settings: Settings, launch: attribution_record.AttributionRecord
) -> bool:
    """Take the one verdict this launch gets, or learn it is already taken. Exclusive create.

    The sentinel's look and the notifier's sweep are separate processes that can both decide on
    one launch; whichever creates the file first publishes, so the launcher never sees a
    ``url-not-seen`` and a ``url-delivered`` for the same open. Claimed BEFORE the first attempt,
    so the bounded resends of :func:`send_until_delivered` are the claimant's alone and the
    outcome is logged once (D-18 (d)'s fail-open rule: a verdict never fails the look it rides on).
    """

    directory = settings.delivery_sent_dir
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / attribution_record.ApprovalOutcomeStore.ref_for(launch)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def _purge_claims(settings: Settings, now: dt.datetime) -> None:
    """A claim outlives its launch by one more lifetime, then goes. Bounds the directory."""

    horizon = now.timestamp() - 2 * settings.ttl_cap_s
    directory = settings.delivery_sent_dir
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        try:
            if path.stat().st_mtime < horizon:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def publish_url_delivery(  # noqa: PLR0913 - one keyword per fact the verdict carries.
    launch: attribution_record.AttributionRecord,
    *,
    verdict: str,
    observing_host: str,
    settings: Settings,
    send: Callable[[attribution_record.AttributionRecord], str],
    at: dt.datetime | None = None,
    clock: Callable[[], dt.datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """Claim, then send, this launch's one delivery verdict. Returns the outcome line to log.

    ``None`` means there is nothing to say: the return leg is not configured here, or this launch
    already has its verdict. Never raises; ``send`` is :func:`push_approval_outcome` from the
    sentinel and :func:`send_to_peer_daemons` from the notifier, retried by
    :func:`send_until_delivered` until the launcher's own wait (:func:`delivery_wait_s` after the
    launch) would have ended.
    """

    if not settings.enabled:
        return None
    try:
        if not claim_url_delivery(settings, launch):
            return None
        record = attribution_record.AttributionRecord.approval_outcome(
            launch,
            actor="unknown",
            rung="",
            verdict=verdict,
            approving_host=observing_host,
            at=at,
        )
        deadline = _launched(launch) + dt.timedelta(
            seconds=delivery_wait_s(settings.ttl_cap_s)
        )
        return send_until_delivered(
            record, send, deadline=deadline, clock=clock, sleep=sleep
        )
    except Exception as exc:  # noqa: BLE001 - a verdict never fails the look or tick it rides on
        return f"not sent: failed ({type(exc).__name__})"


def due_not_seen(
    settings: Settings, *, now: dt.datetime | None = None
) -> list[attribution_record.AttributionRecord]:
    """Every live peer launch whose window has passed with no verdict yet. The notifier's sweep."""

    moment = now or dt.datetime.now(dt.timezone.utc)
    _purge_claims(settings, moment)
    window = dt.timedelta(seconds=delivery_window_s(settings.ttl_cap_s))
    ledger = attribution_record.PeerLaunchLedger(
        settings.peer_ledger_dir,
        ttl_cap_s=settings.ttl_cap_s,
        profile_aliases=settings.profile_aliases,
    )
    sent = settings.delivery_sent_dir
    return [
        record
        for record in ledger.live_records(
            kind=attribution_record.KIND_DEVICE_CODE_LAUNCH, now=moment
        )
        if _launched(record) + window <= moment
        and not (
            sent / attribution_record.ApprovalOutcomeStore.ref_for(record)
        ).exists()
    ]


def _launched(record: attribution_record.AttributionRecord) -> dt.datetime:
    return dt.datetime.fromisoformat(record.launched_at.replace("Z", "+00:00"))


def await_url_delivery(  # noqa: PLR0913 - one keyword per injected seam.
    user_code: str,
    *,
    profile: str,
    settings: Settings,
    clock: Callable[[], dt.datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = 1.0,
) -> str:
    """The LAUNCHING host's bounded wait for its open's verdict. Returns ONE ``verdict=`` line.

    Only an ADMITTED verdict can come back as ``url-not-seen``: the store is written solely by
    :func:`_admit_outcome`, behind the sender gate and the own-launch guard. Anything else -- no
    return leg configured, no local launch for this code, the bound passing -- is
    ``verdict=none``, and the caller keeps trusting rc=0 exactly as before. The deadline is the
    LAUNCH's time plus :func:`delivery_wait_s`, so time the open itself spent counts against it.
    """

    now = clock or (lambda: dt.datetime.now(dt.timezone.utc))
    if not settings.enabled:
        return "verdict=none reason=return-leg-not-configured"
    try:
        launch = attribution_record.LocalLaunchLedger(
            settings.local_ledger_dir, ttl_cap_s=settings.ttl_cap_s
        ).match_device_code(user_code, profile=profile)
    except (attribution_record.AttributionError, OSError) as exc:
        return f"verdict=none reason=launch-ledger-unreadable ({type(exc).__name__})"
    if launch is None:
        return "verdict=none reason=no-local-launch"
    ref = attribution_record.ApprovalOutcomeStore.ref_for(launch)
    store = attribution_record.ApprovalOutcomeStore(settings.delivery_dir)
    deadline = _launched(launch) + dt.timedelta(
        seconds=delivery_wait_s(settings.ttl_cap_s)
    )
    while True:
        held = store.read(ref)
        if (
            held is not None
            and held.code_binding == launch.code_binding
            and held.verdict in attribution_record.DELIVERY_VERDICTS
        ):
            stamp = (
                "first_seen_at"
                if held.verdict == attribution_record.URL_DELIVERED
                else "at"
            )
            return (
                f"verdict={held.verdict} {stamp}={held.at} "
                f"observing_host={held.approving_host}"
            )
        if now() >= deadline:
            return (
                "verdict=none reason=waited until "
                f"{deadline.strftime('%Y-%m-%dT%H:%M:%SZ')} and no verdict arrived"
            )
        sleep(poll_s)


def admit(
    content: str,
    *,
    sender_pubkey: str,
    allow_senders: frozenset[str],
    settings: Settings,
) -> str:
    """Admit one Buzz-delivered record, or refuse it. Returns a verdict.

    The sender gate runs first, so an unvetted key's payload is never parsed. A launch record's
    admission is :meth:`PeerLaunchLedger.ingest`, the pull's own call: idempotent by binding, so
    the same record arriving later over the pull (or twice over Buzz) is stored once. An approval
    outcome goes to :func:`_admit_outcome`.
    """

    if sender_pubkey not in allow_senders:
        return SENDER_NOT_ALLOWED
    if payload_kind(content) == attribution_record.KIND_APPROVAL_OUTCOME:
        return _admit_outcome(content, settings)
    store = attribution_record.PeerLaunchLedger(
        settings.peer_ledger_dir,
        ttl_cap_s=settings.ttl_cap_s,
        profile_aliases=settings.profile_aliases,
    )
    try:
        written = store.ingest(json.loads(content))
    except ValueError as exc:
        return f"refused ({type(exc).__name__})"
    except OSError as exc:
        return f"unwritable ({type(exc).__name__})"
    return ADMITTED if written is not None else ALREADY_HELD


def _admit_outcome(content: str, settings: Settings) -> str:
    """Record who a peer approved this host's code as, or say why not. Names words, not values."""

    try:
        mapping = json.loads(content)
        if isinstance(mapping, dict) and "user_code" in mapping:
            return f"refused ({PLAINTEXT_USER_CODE})"
        record = attribution_record.AttributionRecord.from_transport_payload(mapping)
        delivery = record.verdict in attribution_record.DELIVERY_VERDICTS
        attribution_record.ApprovalOutcomeStore(
            settings.delivery_dir if delivery else settings.outcome_dir
        ).admit(
            record,
            local_ledger=attribution_record.LocalLaunchLedger(
                settings.local_ledger_dir, ttl_cap_s=settings.ttl_cap_s
            ),
        )
    except attribution_record.AttributionRefused as exc:
        reason = str(exc) if str(exc) == NO_LOCAL_LAUNCH else type(exc).__name__
        return f"refused ({reason})"
    except ValueError as exc:
        return f"refused ({type(exc).__name__})"
    except OSError as exc:
        return f"unwritable ({type(exc).__name__})"
    if delivery:
        return (
            f"{ADMITTED} (verdict={record.verdict}, at={record.at}, "
            f"observing host={record.approving_host})"
        )
    return (
        f"{ADMITTED} (actor={record.actor}, verdict={record.verdict}, "
        f"rung={record.rung or 'unrecorded'}, approving host={record.approving_host})"
    )
