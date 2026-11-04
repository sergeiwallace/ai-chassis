"""The git-inbox message rung: envelope, `slug@machine` addressing, matcher and sender.

ISSUE-REF Phase A, design `docs/designs/issue-ref-3-git-inbox-watcher-and-addressing.md`
(§ Sender, § Recipient matching, § Data Model; D-2, D-3).

ONE PARSER FOR BOTH HEADERS. A message file is untrusted input from another machine. The new
header (`FROM: ISSUE-REF@example-project-ec2`, `TO: a@m, *@m`, `SENT:`, `RE:`, `ID:`) and the legacy four-line
header (`FROM: <slug> (<machine>)`, `TO: <slug> (<machine>)`) are read by :func:`parse_envelope`
into the same :class:`Envelope`, so the watcher (Phase B) never branches on the format. Only the
known header keys are accepted; everything after the first blank line is opaque body. A file with
no `TO:` line falls back to its recipient directory (`<dir>@*`), and a file with neither is
refused as `unaddressed`.

THE HEADER IS THE ROUTING AUTHORITY, the directory is for humans (D-3). The sender writes into
`coordination/session-inbox/<slug>/` for a single named recipient and `_broadcast/` otherwise.

APPEND-ONLY, FAST-FORWARD ONLY (D-2). :class:`InboxSender` commits with plumbing onto
`refs/heads/fleet-inbox/<machine>` whose parent is the remote's current tip, and pushes without
any force flag. A non-fast-forward rejection (a second sender on the same machine raced) is
re-fetched and rebuilt once, then refused naming the ref. Every refusal that does not need git --
an unparseable address, an oversize body, a secret shape -- is raised by :func:`build_message`
before the sender runs a single git command.

The git seam is `fleet_liveness.run_git`/`GitRunner`, reused rather than re-implemented.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_chassis.fleet_liveness import (
    DELEGATION_TRAILER,
    GitResult,
    GitRunner,
    _refusal_reason,
    run_git,
)
from ai_chassis.session_address import (
    _NAME,
    NAME_RE,
    WILDCARD,
    Address,
    AddressError,
    format_addresses,
    parse_address,
    parse_addresses,
)

#: The TOML table Phase B adds to `projects-wide-session-config/ai-harness.toml`.
CONFIG_SECTION = "git_inbox_notify"

DEFAULT_MAX_BYTES = 4096
DEFAULT_RETENTION_DAYS = 30
DEFAULT_INBOX_REF_PREFIX = "refs/heads/fleet-inbox/"
DEFAULT_POLL_INTERVAL_S = 60
DEFAULT_PENDING_TTL_S = 604800
DEFAULT_PROMPT_MAX_CHARS = 400

#: The design's § Data Model scalars. List-valued keys are owned by the phase that consumes them,
#: because `merge_typed_section` types scalars only: `allow_senders` is read by :func:`load_config`
#: itself (Phase B's hold), `delivery` by Phase C.
DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "poll_interval_s": DEFAULT_POLL_INTERVAL_S,
    "max_bytes": DEFAULT_MAX_BYTES,
    "retention_days": DEFAULT_RETENTION_DAYS,
    "pending_ttl_s": DEFAULT_PENDING_TTL_S,
    "prompt_enabled": False,
    "prompt_max_chars": DEFAULT_PROMPT_MAX_CHARS,
    "inbox_ref_prefix": DEFAULT_INBOX_REF_PREFIX,
    #: ISSUE-REF D-1: write lifecycle receipts to this machine's inbox ref.
    "receipts": True,
}
_POSITIVE_INT_KEYS = (
    "poll_interval_s",
    "max_bytes",
    "retention_days",
    "pending_ttl_s",
    "prompt_max_chars",
)

INBOX_DIR_PARTS = ("coordination", "session-inbox")
INBOX_DIR = "/".join(INBOX_DIR_PARTS)
BROADCAST_DIR = "_broadcast"
#: Lifecycle receipts (ISSUE-REF D-1): `_receipts/<message-dir>/<slug>@<machine>.<event>.json`.
RECEIPTS_DIR_NAME = "_receipts"
#: Not messages: the heartbeat records, the lifecycle receipts and the protocol README.
NON_MESSAGE_DIRS = frozenset({"_heartbeat", RECEIPTS_DIR_NAME})
NON_MESSAGE_NAMES = frozenset({"README.md"})
#: Where the sender parks the fetched tip of its own ref; the same namespace Phase B reads into.
INBOX_READ_NAMESPACE = "refs/fleet-inbox/"

#: `ISSUE-REF (example-project-ec2)`, `ISSUE-REF (example-project-ec2, example-client-host EC2 dev host) -- written by ...`.
_LEGACY_ADDRESS_RE = re.compile(rf"^({_NAME})\s*\(([^)]*)\)")
_HEADER_LINE_RE = re.compile(r"^([A-Z][A-Z-]*):[ \t]?(.*)$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
HEADER_KEYS = ("FROM", "TO", "SENT", "RE", "ID", "ACTIONS", "ACTIONS-NOTE")
#: ISSUE-REF D-3: `ACTIONS: <display-id>@<repo>, ...`, the Beads issues a recipient takes.
_ACTION_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
_ACTION_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
#: `ACTIONS-NOTE:` value `send --allow-assigned` records.
ACTIONS_NOTE_ASSIGNED_ALLOWED = "assigned-allowed"

#: How an envelope's `to` was established. The directory form is the loosest target (D-3 Con).
ADDRESSING_HEADER = "header"
ADDRESSING_LEGACY_HEADER = "legacy-header"
ADDRESSING_LEGACY_DIRECTORY = "legacy-directory"

CLASS_DIRECT = "direct"
CLASS_BROADCAST = "broadcast"

#: `SENT:` and `ID:` stamp (the design's example), and the filename stamp, which carries seconds
#: so two sends in one minute from one session cannot share a path.
SENT_FORMAT = "%Y-%m-%dT%H%MZ"
FILENAME_STAMP_FORMAT = "%Y-%m-%dT%H%M%SZ"
_FILENAME_STAMP_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2})(\d{2})(\d{2})?Z")

_TREE_MODE = "040000"
_BLOB_MODE = "100644"


class EnvelopeError(ValueError):
    """A message file the parser refuses. `reason` is `unaddressed`, `oversize` or `malformed:<why>`."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


class SendRefused(ValueError):
    """A send refused before any git write. `reason` is a class name, never the matched text."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


# ---------------------------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------------------------


def load_config(
    path: Path | None = None, *, env: dict[str, str] | None = None
) -> dict[str, Any]:
    """`[git_inbox_notify]` with the XDG overlay, merged over :data:`DEFAULT_CONFIG`.

    The same loader and drop-to-default discipline as `fleet_liveness.load_config`: an absent
    table, an unreadable file, a wrongly-typed key or a non-positive count each fall back to the
    default rather than raising. Imported lazily so the notifier can import this module.
    """
    from ai_chassis.agent_mail_notifier import merge_typed_section, read_config_tables

    defaults = {**DEFAULT_CONFIG, "allow_senders": []}
    try:
        tables = read_config_tables(path, env=env)
    except RuntimeError:
        return defaults
    section = tables.get(CONFIG_SECTION)
    if not isinstance(section, dict):
        return defaults
    merged = {**defaults, **merge_typed_section(section, DEFAULT_CONFIG)}
    for key in _POSITIVE_INT_KEYS:
        if merged[key] <= 0:
            merged[key] = DEFAULT_CONFIG[key]
    # Fail closed, as `[buzz_notify]`'s loader does: a non-list admits no sender.
    senders = section.get("allow_senders")
    if isinstance(senders, list):
        merged["allow_senders"] = [item for item in senders if isinstance(item, str)]
    return merged


def known_machines(
    path: Path | None = None, *, env: dict[str, str] | None = None
) -> frozenset[str]:
    """Every machine with a `[machine.<name>]` table in the tracked config or its overlay.

    Empty on an unreadable config, so the sender refuses every concrete machine rather than
    accepting a typo it cannot check.
    """
    from ai_chassis.agent_mail_notifier import read_config_tables

    try:
        tables = read_config_tables(path, env=env)
    except RuntimeError:
        return frozenset()
    machines = tables.get("machine")
    if not isinstance(machines, dict):
        return frozenset()
    return frozenset(name for name in machines if isinstance(name, str))


# ---------------------------------------------------------------------------------------------
# T-A.1 Address grammar
# ---------------------------------------------------------------------------------------------


# The grammar lives in `session_address`, shared by every rung (ISSUE-REF).


# ---------------------------------------------------------------------------------------------
# T-A.2 Envelope
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """One `ACTIONS:` entry: a Beads display id and the repo whose store owns it."""

    id: str
    repo: str

    def __str__(self) -> str:
        return f"{self.id}@{self.repo}"


def parse_action(entry: str) -> Action:
    """`<display-id>@<repo>`; anything else raises `EnvelopeError("malformed:actions")`."""
    display_id, at_sign, repo = entry.strip().rpartition("@")
    if not (
        at_sign and _ACTION_ID_RE.match(display_id) and _ACTION_REPO_RE.match(repo)
    ):
        raise EnvelopeError(
            "malformed:actions", f"{entry.strip()!r} is not <display-id>@<repo>"
        )
    return Action(id=display_id, repo=repo)


def parse_actions(value: str) -> tuple[Action, ...]:
    return tuple(parse_action(entry) for entry in value.split(",") if entry.strip())


@dataclass(frozen=True)
class Envelope:
    """One parsed message. `id` is the header's `ID:`, else `<source>:<path>` (stable per file)."""

    from_: Address | None
    to: tuple[Address, ...]
    sent: str
    re: str
    id: str
    body: str
    path: str = ""
    source: str = ""
    launch_event: object | None = None
    addressing: str = ADDRESSING_HEADER
    actions: tuple[Action, ...] = ()
    actions_note: str = ""


def _legacy_address(value: str) -> Address | None:
    """`slug (machine[, prose])` -> `slug@machine`; a parenthetical that is not a machine -> `slug@*`."""
    match = _LEGACY_ADDRESS_RE.match(value.strip())
    if match is None:
        return None
    machine = match.group(2).split(",")[0].strip()
    return Address(
        slug=match.group(1), machine=machine if NAME_RE.match(machine) else WILDCARD
    )


def recipient_directory(path: str) -> str | None:
    """The slug of `coordination/session-inbox/<slug>/<file>`, or None (`_broadcast`, top level, other)."""
    parts = Path(path).parts
    if (
        len(parts) != len(INBOX_DIR_PARTS) + 2
        or tuple(parts[: len(INBOX_DIR_PARTS)]) != INBOX_DIR_PARTS
    ):
        return None
    directory = parts[len(INBOX_DIR_PARTS)]
    return directory if NAME_RE.match(directory) else None


def is_message_path(path: str) -> bool:
    """A `.md` under the inbox directory that is neither a README, heartbeat state nor a receipt."""
    parts = Path(path).parts
    if tuple(parts[: len(INBOX_DIR_PARTS)]) != INBOX_DIR_PARTS or len(parts) <= len(
        INBOX_DIR_PARTS
    ):
        return False
    if parts[-1] in NON_MESSAGE_NAMES or not parts[-1].endswith(".md"):
        return False
    return not any(
        part in NON_MESSAGE_DIRS for part in parts[len(INBOX_DIR_PARTS) : -1]
    )


def _split_header(text: str) -> tuple[dict[str, str], str]:
    lines = text.split("\n")
    first = _HEADER_LINE_RE.match(lines[0].rstrip("\r")) if lines else None
    if first is None or first.group(1) not in HEADER_KEYS:
        return {}, text
    header: dict[str, str] = {}
    for number, raw in enumerate(lines, start=1):
        line = raw.rstrip("\r")
        if not line.strip():
            return header, "\n".join(lines[number:])
        match = _HEADER_LINE_RE.match(line)
        if match is None or match.group(1) not in HEADER_KEYS:
            raise EnvelopeError(
                "malformed:header-line", f"line {number} is not a known header key"
            )
        if match.group(1) in header:
            raise EnvelopeError(
                "malformed:duplicate-header",
                f"{match.group(1)} repeated at line {number}",
            )
        header[match.group(1)] = match.group(2).strip()
    return header, ""


def _parse_from(value: str) -> Address:
    legacy = _legacy_address(value)
    if legacy is not None:
        return legacy
    try:
        address = parse_address(value)
    except AddressError as exc:
        raise EnvelopeError("malformed:from", str(exc)) from exc
    if WILDCARD in (address.slug, address.machine):
        raise EnvelopeError(
            "malformed:from", f"a sender must be one session on one machine: {value!r}"
        )
    return address


def parse_envelope(
    text: str, *, path: str, source: str, max_bytes: int | None = DEFAULT_MAX_BYTES
) -> Envelope:
    """Parse one message file, new or legacy header, into an :class:`Envelope`.

    `path` is the repo-relative file path and `source` names where it was read (a ref such as
    `fleet-inbox/example-project-ec2` or `main`); together they are a legacy file's id. `max_bytes=None`
    disables the body cap, for a reader listing files rather than delivering them.
    """
    header, body = _split_header(text)
    size = len(body.encode("utf-8"))
    if max_bytes is not None and size > max_bytes:
        raise EnvelopeError(
            "oversize", f"body is {size} bytes, max_bytes is {max_bytes}"
        )

    addressing = ADDRESSING_HEADER
    to_value = header.get("TO", "")
    if to_value:
        legacy = _legacy_address(to_value)
        if legacy is not None:
            to: tuple[Address, ...] = (legacy,)
            addressing = ADDRESSING_LEGACY_HEADER
        else:
            try:
                to = parse_addresses(to_value)
            except AddressError as exc:
                raise EnvelopeError("malformed:to", str(exc)) from exc
    else:
        directory = recipient_directory(path)
        if directory is None:
            raise EnvelopeError(
                "unaddressed", f"{path} has no TO: line and no recipient directory"
            )
        to = (Address(slug=directory, machine=WILDCARD),)
        addressing = ADDRESSING_LEGACY_DIRECTORY

    from_value = header.get("FROM", "")
    sender = _parse_from(from_value) if from_value else None
    if (
        sender is not None
        and addressing == ADDRESSING_HEADER
        and _LEGACY_ADDRESS_RE.match(from_value)
    ):
        addressing = ADDRESSING_LEGACY_HEADER

    message_id = header.get("ID", "")
    if message_id and not _ID_RE.match(message_id):
        raise EnvelopeError(
            "malformed:id",
            f"ID {message_id!r} carries a character outside [A-Za-z0-9._:-]",
        )
    launch_event = None
    try:
        carrier = json.loads(body)
    except ValueError:
        carrier = None
    if isinstance(carrier, dict) and "launch_event" in carrier:
        launch_event = (
            carrier["launch_event"] or {}
        )  # present unsigned requests reach audit
    return Envelope(
        launch_event=launch_event,
        from_=sender,
        to=to,
        sent=header.get("SENT", ""),
        re=header.get("RE", ""),
        id=message_id or f"{source}:{path}",
        body=body,
        path=path,
        source=source,
        addressing=addressing,
        actions=parse_actions(header.get("ACTIONS", "")),
        actions_note=header.get("ACTIONS-NOTE", ""),
    )


def format_envelope(envelope: Envelope) -> str:
    """The canonical new-format file text. The only form the sender ever writes."""
    if envelope.from_ is None:
        raise EnvelopeError("malformed:from", "a sent message must name its sender")
    lines = [
        f"FROM: {envelope.from_}",
        f"TO: {format_addresses(envelope.to)}",
        f"SENT: {envelope.sent}",
        f"RE: {envelope.re}",
        f"ID: {envelope.id}",
    ]
    if envelope.actions:
        lines.append(f"ACTIONS: {', '.join(str(a) for a in envelope.actions)}")
    if envelope.actions_note:
        lines.append(f"ACTIONS-NOTE: {envelope.actions_note}")
    lines += [
        "",
        envelope.body if envelope.body.endswith("\n") else envelope.body + "\n",
    ]
    return "\n".join(lines)


def message_id_for(  # noqa: PLR0913  # one keyword per hashed envelope field
    *,
    sent: str,
    sender: Address,
    to: Iterable[Address],
    re: str,
    body: str,
    salt: str = "",
) -> str:
    """`<SENT>-<slug>-<machine>-<4 hex>`, e.g. `2026-10-10T1215Z-ISSUE-REF-example-project-ec2-7f3a`.

    The suffix hashes the content and `salt` (the sender passes its seconds-precision stamp), so
    two messages from one session in one minute get distinct ids.
    """
    digest = hashlib.sha256(
        "\0".join([sent, str(sender), format_addresses(to), re, body, salt]).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{sent}-{sender.slug}-{sender.machine}-{digest[:4]}"


# ---------------------------------------------------------------------------------------------
# Secret-shape refusal (the sender's stand-in for the push-time gate the ref does not run)
# ---------------------------------------------------------------------------------------------

SECRET_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("pem-private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    (
        "github-token",
        re.compile(r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ),
    ("sk-api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")),
)
DENY_PATTERN_CLASS = "deny-pattern-file"


def secret_shape(text: str, deny_literals: Iterable[str] = ()) -> str | None:
    """The first matching pattern CLASS, never the matched text; None when nothing matches."""
    for name, pattern in SECRET_SHAPES:
        if pattern.search(text):
            return name
    folded = text.casefold()
    if any(literal.casefold() in folded for literal in deny_literals if literal):
        return DENY_PATTERN_CLASS
    return None


def read_deny_literals(path: Path) -> tuple[str, ...]:
    """One literal per line; blank lines and `#` comments skipped. Unreadable is a refusal."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SendRefused(
            "deny-pattern-file-unreadable", f"{path}: {exc.strerror or exc}"
        ) from exc
    return tuple(
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


# ---------------------------------------------------------------------------------------------
# T-A.3 Recipient matching
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchResult:
    """What one message means on one machine.

    `broadcast` is None when a `*@M`/`*@*` entry applies but the live-session source failed:
    UNDETERMINED, never an empty set that would read as "delivered to nobody".
    """

    for_this_machine: bool
    direct: tuple[str, ...] = ()
    broadcast: tuple[str, ...] | None = ()
    pending: tuple[str, ...] = ()
    reason: str = ""

    def classes(self) -> dict[str, str]:
        """`{slug: class}`; a direct beats a broadcast for the same session."""
        result = {slug: CLASS_BROADCAST for slug in self.broadcast or ()}
        result.update({slug: CLASS_DIRECT for slug in self.direct})
        return result


REASON_NOT_FOR_THIS_MACHINE = "not-for-this-machine"
REASON_LIVE_UNAVAILABLE = "live-sessions-unavailable"


def match_recipients(
    to: Iterable[Address],
    *,
    machine: str,
    live: Callable[[], Iterable[str | None] | None],
    registered: Iterable[str] = (),
) -> MatchResult:
    """Resolve a `TO:` list against machine `machine` (the § Recipient matching table).

    `live` returns the live session slugs (None entries ignored); returning None or raising means
    the probe cannot be trusted, and then every named target that could be on this machine is
    `pending` and the broadcast set is undetermined. `registered` is the registered-but-idle
    slugs (the notifier's per-session marker files): a named target, never a broadcast target.
    """
    relevant = [address for address in to if address.machine in (machine, WILDCARD)]
    if not relevant:
        return MatchResult(for_this_machine=False, reason=REASON_NOT_FOR_THIS_MACHINE)
    named = [address for address in relevant if not address.is_broadcast]
    wants_broadcast = any(address.is_broadcast for address in relevant)

    reason = ""
    try:
        sessions = live()
    except Exception as exc:  # noqa: BLE001 - any probe failure is "cannot tell", reported
        sessions = None
        reason = f"{REASON_LIVE_UNAVAILABLE} ({type(exc).__name__})"
    if sessions is None:
        pending = tuple(dict.fromkeys(address.slug for address in named))
        return MatchResult(
            for_this_machine=True,
            pending=pending,
            broadcast=None if wants_broadcast else (),
            reason=reason or REASON_LIVE_UNAVAILABLE,
        )

    live_slugs = [slug for slug in sessions if slug]
    known = set(live_slugs) | set(registered)
    direct: list[str] = []
    pending_list: list[str] = []
    for address in named:
        if address.slug in known:
            if address.slug not in direct:
                direct.append(address.slug)
        elif address.machine == machine and address.slug not in pending_list:
            pending_list.append(address.slug)
    broadcast: tuple[str, ...] = ()
    if wants_broadcast:
        broadcast = tuple(
            slug for slug in dict.fromkeys(live_slugs) if slug not in direct
        )
    return MatchResult(
        for_this_machine=True,
        direct=tuple(direct),
        broadcast=broadcast,
        pending=tuple(pending_list),
    )


# ---------------------------------------------------------------------------------------------
# T-A.4 Sender
# ---------------------------------------------------------------------------------------------


def inbox_ref(machine: str, *, prefix: str = DEFAULT_INBOX_REF_PREFIX) -> str:
    return f"{prefix}{machine}"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True)
class Message:
    """A built, validated message: the file's path and text, ready for the sender."""

    envelope: Envelope
    path: str
    text: str


def message_path(sender: Address, to: tuple[Address, ...], moment: dt.datetime) -> str:
    """`coordination/session-inbox/<dir>/<UTC-ts>-from-<sender-slug>.md` (design § Sender)."""
    directory = to[0].slug if len(to) == 1 and not to[0].is_broadcast else BROADCAST_DIR
    stamp = moment.astimezone(dt.UTC).strftime(FILENAME_STAMP_FORMAT)
    return f"{INBOX_DIR}/{directory}/{stamp}-from-{sender.slug}.md"


def build_message(  # noqa: PLR0913  # one keyword per envelope field
    *,
    sender: Address,
    to: tuple[Address, ...],
    re: str,
    body: str,
    launch_event: object | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    deny_literals: Iterable[str] = (),
    message_id: str | None = None,
    now: dt.datetime | None = None,
    actions: tuple[Action, ...] = (),
    actions_note: str = "",
) -> Message:
    """Validate and format one message. Every refusal here precedes any git command.

    The formatted text is re-read through :func:`parse_envelope`, so what is pushed is exactly
    what every reader will parse.
    """
    if not to:
        raise SendRefused("no-recipients", "--to is empty")
    if WILDCARD in (sender.slug, sender.machine):
        raise SendRefused(
            "sender-unresolved", f"sender {sender} is not one session on one machine"
        )
    if "\n" in re or "\r" in re:
        raise SendRefused("malformed-subject", "RE must be one line")
    if launch_event is not None:
        body = json.dumps(
            {"message": body, "launch_event": launch_event}, separators=(",", ":")
        )
    size = len(body.encode("utf-8"))
    if size > max_bytes:
        raise SendRefused("oversize", f"body is {size} bytes, max_bytes is {max_bytes}")
    shape = secret_shape(f"{re}\n{body}", deny_literals)
    if shape is not None:
        raise SendRefused(
            "secret-shape", f"the message matches the {shape} pattern class"
        )
    if message_id is not None and not _ID_RE.match(message_id):
        raise SendRefused(
            "malformed-id",
            f"--id {message_id!r} carries a character outside [A-Za-z0-9._:-]",
        )

    moment = now or _utcnow()
    sent = moment.astimezone(dt.UTC).strftime(SENT_FORMAT)
    stamp = moment.astimezone(dt.UTC).strftime(FILENAME_STAMP_FORMAT)
    envelope = Envelope(
        from_=sender,
        to=to,
        sent=sent,
        re=re,
        id=message_id
        or message_id_for(
            sent=sent, sender=sender, to=to, re=re, body=body, salt=stamp
        ),
        body=body if body.endswith("\n") else body + "\n",
        actions=actions,
        actions_note=actions_note,
    )
    path = message_path(sender, to, moment)
    text = format_envelope(envelope)
    reparsed = parse_envelope(text, path=path, source="send", max_bytes=max_bytes)
    if (
        reparsed.to != to
        or reparsed.from_ != sender
        or reparsed.id != envelope.id
        or reparsed.actions != actions
        or reparsed.actions_note != actions_note
    ):
        raise SendRefused(
            "self-check-failed", "the formatted envelope does not parse back to itself"
        )
    return Message(envelope=reparsed, path=path, text=text)


def file_stamp(path: str) -> dt.datetime | None:
    """The UTC moment a sender-written filename carries, or None when it carries none."""
    match = _FILENAME_STAMP_RE.match(Path(path).name)
    if match is None:
        return None
    year, month, day, hour, minute, second = match.groups()
    return dt.datetime(
        int(year),
        int(month),
        int(day),
        int(hour),
        int(minute),
        int(second or 0),
        tzinfo=dt.UTC,
    )


def retention_stamp(path: str) -> dt.datetime | None:
    """The moment retention ages `path` by: a receipt by its message's id, a message by its name.

    A receipt ages out with the message it names: its directory is the message id, whose leading
    `SENT:` stamp :func:`file_stamp` reads. A receipt whose message id carries no stamp (a custom
    `--id`, a legacy id) returns None and is never dropped by the sender.
    """
    parts = Path(path).parts
    head = len(INBOX_DIR_PARTS)
    if tuple(parts[: head + 1]) == (*INBOX_DIR_PARTS, RECEIPTS_DIR_NAME):
        return file_stamp(parts[head + 1]) if len(parts) > head + 2 else None  # noqa: PLR2004
    return file_stamp(path)


@dataclass(frozen=True)
class SendOutcome:
    """The remote's state after a send. `sent` is True only after the push itself returned 0."""

    sent: bool
    id: str
    path: str
    ref: str
    commit: str = ""
    parent: str = ""
    recipients: str = ""
    reason: str = ""
    dropped: tuple[str, ...] = ()
    verify: str = ""

    def as_record(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "id": self.id,
            "path": self.path,
            "ref": self.ref,
            "commit": self.commit,
            "parent": self.parent,
            "recipients": self.recipients,
            "reason": self.reason,
            "dropped": list(self.dropped),
            "verify": self.verify,
        }


class _GitFailure(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _is_non_fast_forward(result: GitResult) -> bool:
    return "[rejected]" in result.err and (
        "non-fast-forward" in result.err or "fetch first" in result.err
    )


@dataclass
class InboxSender:
    """Append one message to `refs/heads/fleet-inbox/<machine>` and push it as a fast-forward.

    Plumbing only (`hash-object`, `mktree`, `commit-tree`, `push`), so the checkout the CLI runs
    in keeps its index, working tree and branches untouched. No force flag exists on any path.
    """

    repo: Path
    machine: str
    remote: str = "origin"
    ref_prefix: str = DEFAULT_INBOX_REF_PREFIX
    retention_days: int = DEFAULT_RETENTION_DAYS
    runner: GitRunner = run_git
    log: Callable[[str], None] = field(default=lambda _message: None)

    @property
    def ref(self) -> str:
        return inbox_ref(self.machine, prefix=self.ref_prefix)

    @property
    def read_ref(self) -> str:
        return f"{INBOX_READ_NAMESPACE}{self.machine}"

    def verify_command(self, path: str) -> str:
        return f"git show {self.remote}/{self.ref.removeprefix('refs/heads/')}:{path}"

    def _git(self, args: list[str], stdin: str | None = None) -> GitResult:
        return self.runner(args, self.repo, stdin)

    def _need(self, args: list[str], what: str, stdin: str | None = None) -> str:
        result = self._git(args, stdin)
        if result.rc != 0:
            raise _GitFailure(f"{what}-failed ({_refusal_reason(result)})")
        return result.out

    def _fetch_tip(self) -> str:
        """The remote's current tip of this ref ("" when it does not exist yet), fetched locally."""
        listed = self._git(["ls-remote", self.remote, self.ref])
        if listed.rc != 0:
            raise _GitFailure(f"remote-unreachable ({_refusal_reason(listed)})")
        if not listed.out:
            return ""
        tip = listed.out.split()[0]
        self._need(
            ["fetch", "--quiet", self.remote, f"+{self.ref}:{self.read_ref}"], "fetch"
        )
        return tip

    def _tip_blobs(self, tip: str) -> dict[str, str]:
        if not tip:
            return {}
        listing = self._need(["ls-tree", "-r", "-z", "--full-tree", tip], "ls-tree")
        blobs: dict[str, str] = {}
        for entry in listing.split("\0"):
            if not entry:
                continue
            meta, _, path = entry.partition("\t")
            mode, kind, oid = meta.split()
            if kind != "blob" or mode != _BLOB_MODE:
                raise _GitFailure(f"unexpected-tree-entry ({mode} {kind} {path})")
            blobs[path] = oid
        return blobs

    def _write_tree(self, blobs: Mapping[str, str]) -> str:
        """Build the nested tree bottom-up; `mktree` writes one level per call."""
        files: list[tuple[str, str]] = []
        subdirs: dict[str, dict[str, str]] = {}
        for path, oid in blobs.items():
            head, sep, rest = path.partition("/")
            if sep:
                subdirs.setdefault(head, {})[rest] = oid
            else:
                files.append((head, oid))
        lines = [f"{_BLOB_MODE} blob {oid}\t{name}" for name, oid in files]
        lines += [
            f"{_TREE_MODE} tree {self._write_tree(children)}\t{name}"
            for name, children in subdirs.items()
        ]
        return self._need(
            ["mktree", "-z"], "mktree", "".join(f"{line}\0" for line in sorted(lines))
        )

    def _attempt(
        self,
        files: Mapping[str, str],
        *,
        subject: str,
        now: dt.datetime,
        rename: Callable[[str], str] | None,
    ) -> tuple[str, str, tuple[str, ...], list[str], GitResult | None]:
        """One build and push. `rename` moves a colliding path aside; without it the tip's blob wins.

        The push is None when the rebuilt tree equals the tip's: there is nothing to commit.
        """
        parent = self._fetch_tip()
        blobs = self._tip_blobs(parent)
        before = dict(blobs)
        cutoff = now - dt.timedelta(days=self.retention_days)
        dropped = tuple(
            sorted(
                path
                for path in blobs
                if (stamp := retention_stamp(path)) is not None and stamp < cutoff
            )
        )
        for path in dropped:
            del blobs[path]
        written: list[str] = []
        for path, text in files.items():
            target = path
            if target in blobs:
                if rename is None:
                    continue
                target = rename(target)
            blobs[target] = self._need(
                ["hash-object", "-w", "--stdin"], "hash-object", text
            )
            written.append(target)
        if parent and blobs == before:
            return parent, parent, (), written, None
        tree = self._write_tree(blobs)
        args = ["commit-tree", tree]
        if parent:
            args += ["-p", parent]
        args += ["-m", subject, "-m", DELEGATION_TRAILER]
        commit = self._need(args, "commit-tree")
        from ai_chassis.wake.notifications import notifying

        with notifying("prepare_git", self, files, commit, parent):
            pushed = self._git(["push", "--quiet", self.remote, f"{commit}:{self.ref}"])
        return commit, parent, dropped, written, pushed

    def _push_loop(
        self,
        files: Mapping[str, str],
        *,
        subject: str,
        now: dt.datetime,
        rename: Callable[[str], str] | None,
        outcome: Callable[..., SendOutcome],
    ) -> SendOutcome:
        """Commit and push; one re-fetch-and-rebuild on a non-fast-forward, then refuse."""

        def unsent(reason: str, **extra: Any) -> SendOutcome:
            self.log(f"git-inbox send unsent: {reason}")
            return outcome(sent=False, reason=reason, **extra)

        commit = parent = ""
        for attempt in (1, 2):
            try:
                commit, parent, dropped, written, pushed = self._attempt(
                    files, subject=subject, now=now, rename=rename
                )
            except _GitFailure as exc:
                return unsent(exc.reason)
            if pushed is None or pushed.rc == 0:
                self._git(["update-ref", self.read_ref, commit])
                return outcome(
                    sent=True,
                    commit=commit,
                    parent=parent,
                    dropped=dropped,
                    written=tuple(written),
                )
            if not _is_non_fast_forward(pushed):
                return unsent(
                    f"push-failed ({_refusal_reason(pushed)})",
                    commit=commit,
                    parent=parent,
                )
            self.log(
                f"git-inbox send: {self.ref} moved under us (attempt {attempt}); re-fetching"
            )
        return unsent(
            f"push-rejected-non-fast-forward twice on {self.ref}",
            commit=commit,
            parent=parent,
        )

    def send(self, message: Message, *, now: dt.datetime | None = None) -> SendOutcome:
        """Commit and push one message; a path already on the tip gets the id's suffix."""
        recipients = format_addresses(message.envelope.to)
        suffix = message.envelope.id.rsplit("-", 1)[-1]

        def outcome(*, written: tuple[str, ...] = (), **fields: Any) -> SendOutcome:
            path = written[0] if written else message.path
            return SendOutcome(
                id=message.envelope.id,
                path=path,
                ref=self.ref,
                recipients=recipients,
                verify=self.verify_command(path) if fields.get("sent") else "",
                **fields,
            )

        return self._push_loop(
            {message.path: message.text},
            subject=f"git-inbox: {message.envelope.id}",
            now=now or _utcnow(),
            rename=lambda path: path.removesuffix(".md") + f"-{suffix}.md",
            outcome=outcome,
        )

    def commit_files(
        self, files: Mapping[str, str], *, subject: str, now: dt.datetime | None = None
    ) -> SendOutcome:
        """Add `files` to the tip; a path already there keeps its blob (first write wins).

        The receipt path (ISSUE-REF T-A1.2): a receipt's path is deterministic, so writing the
        same receipt twice leaves the tip tree unchanged and pushes nothing.
        """

        def outcome(*, written: tuple[str, ...] = (), **fields: Any) -> SendOutcome:
            return SendOutcome(
                id=subject, path=",".join(written), ref=self.ref, **fields
            )

        return self._push_loop(
            files, subject=subject, now=now or _utcnow(), rename=None, outcome=outcome
        )
