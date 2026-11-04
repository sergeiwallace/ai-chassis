"""The git-inbox MESSAGE rung's watcher, driven from the one notifier's tick (ISSUE-REF D-1).

Not a daemon. ``agent_mail_notifier.Notifier`` holds one :class:`GitInboxWatcher` and calls
:meth:`GitInboxWatcher.poll` from ``_finish_tick``; the watcher fetches, enumerates, dedupes,
matches and returns a :class:`InboxBatch`, and the NOTIFIER renders it and hands it to its own
``_write_markers``. This module writes no marker and never touches ``notice.id``,
so marker publication uses the notifier's shared flock in machine and session processes.
The stable ledger sidecar lock spans poll, delivery and commit; append and compaction
also take that lock. Scoped deliveries retain per-target deduplication without marking
the entire envelope complete for recipients owned by another watcher.

The one file it does write is the delivered ledger, ``<state>/git-inbox/delivered.jsonl``, and only
from :meth:`GitInboxWatcher.commit`, which the notifier calls AFTER the marker write: a crash between
the two re-delivers a message rather than losing it, the less harmful direction.

The envelope parser and the recipient matcher are Phase A's (``ai_chassis.git_inbox``); this module
reaches them through :func:`_envelope_api` alone, so a rename there is a change here in one place.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_chassis import notifier_pass
from ai_chassis.fleet_liveness import GitResult, GitRunner, _refusal_reason, run_git

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    from ai_chassis import windows_file_lock as fcntl  # type: ignore[no-redef]

#: Private read namespaces (D-6): a fetch here never moves `refs/remotes/origin/*`, so the user's
#: tracking refs in a shared checkout are left alone.
INBOX_READ_NAMESPACE = "refs/fleet-inbox/"
MAIN_READ_REF = "refs/fleet-inbox-read/main"
DEFAULT_INBOX_REF_PREFIX = "refs/heads/fleet-inbox/"
SOURCE_MAIN = "main"

INBOX_DIR = "coordination/session-inbox"
README_NAME = "README.md"
HEARTBEAT_SEGMENT = "_heartbeat"
RECEIPTS_SEGMENT = "_receipts"
MESSAGE_SUFFIX = ".md"

DEFAULT_POLL_INTERVAL_S = 60
#: D-6's Con mitigation: a mis-set overlay cannot drive the fetch faster than this.
POLL_INTERVAL_FLOOR_S = 15
DEFAULT_MAX_BYTES = 4096
DEFAULT_RETENTION_DAYS = 30
DEFAULT_PENDING_TTL_S = 604800

#: T-B.2's compaction: past this many lines, keep the newest `LEDGER_COMPACT_KEEP` plus every line
#: newer than `retention_days`.
LEDGER_COMPACT_ABOVE = 10_000
LEDGER_COMPACT_KEEP = 5_000

#: The ledger/target key for a message carried on the machine notice (undetermined liveness).
#: The notifier's own `MACHINE_KEY` spelling, so the two never disagree about what it means.
MACHINE_TARGET = "__machine__"
CLASS_BROADCAST = "broadcast"

#: D-9: the marker write records `written`; `delivered` is written only when a session whose slug
#: pointer names the target has CLAIMED a notice id that carried the line, and `expired-unclaimed`
#: when no claim arrived within `pending_ttl_s`. A claim or expiry record names ONE slug.
OUTCOME_WRITTEN = "written"
OUTCOME_DELIVERED = "delivered"
OUTCOME_EXPIRED_UNCLAIMED = "expired-unclaimed"
SETTLED_OUTCOMES = frozenset({OUTCOME_DELIVERED, OUTCOME_EXPIRED_UNCLAIMED})
OUTCOME_HELD = "held"
OUTCOME_EXPIRED = "expired"
OUTCOME_NOT_FOR_MACHINE = "not-for-this-machine"
OUTCOME_REFUSED_OVERSIZE = "refused(oversize)"
STATUS_DISABLED = "disabled"
LAYER_HOOK = "hook"

SECONDS_PER_DAY = 86_400
_SENT_FORMATS = ("%Y-%m-%dT%H%MZ", "%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ")


class LedgerUnreadableError(RuntimeError):
    """The delivered ledger exists and cannot be read; nothing may be delivered this tick."""


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(moment: dt.datetime) -> str:
    return moment.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_moment(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if not isinstance(value, str) or not value:
        return None
    for fmt in _SENT_FORMATS:
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=dt.UTC)
        except ValueError:
            continue
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def poll_interval_s(cfg: dict[str, Any]) -> int:
    """`[git_inbox_notify].poll_interval_s`, bounded below at :data:`POLL_INTERVAL_FLOOR_S`."""
    value = cfg.get("poll_interval_s", DEFAULT_POLL_INTERVAL_S)
    if isinstance(value, bool) or not isinstance(value, int):
        value = DEFAULT_POLL_INTERVAL_S
    return max(POLL_INTERVAL_FLOOR_S, value)


def _int_setting(cfg: dict[str, Any], key: str, default: int) -> int:
    value = cfg.get(key, default)
    return default if isinstance(value, bool) or not isinstance(value, int) else value


def pending_ttl_s(cfg: dict[str, Any]) -> int:
    """`[git_inbox_notify].pending_ttl_s`: a pending target's wait AND an unclaimed line's (D-9)."""
    return _int_setting(cfg, "pending_ttl_s", DEFAULT_PENDING_TTL_S)


# ---------------------------------------------------------------------------------------------
# Phase A boundary: the ONE place this module names `ai_chassis.git_inbox`.
# ---------------------------------------------------------------------------------------------


def _envelope_api() -> Any:
    from ai_chassis import git_inbox

    return git_inbox


#: A header-less legacy file names no sender; the reply verb then has no address to offer.
UNKNOWN_SENDER = "unknown"


def address_text(address: Any) -> str:
    return UNKNOWN_SENDER if address is None else f"{address.slug}@{address.machine}"


def parse_message(text: str, *, path: str, source: str, max_bytes: int) -> Any:
    """Phase A's `parse_envelope`; raises its `EnvelopeError` for a file it refuses."""
    return _envelope_api().parse_envelope(
        text, path=path, source=source, max_bytes=max_bytes
    )


def envelope_error() -> type[Exception]:
    return _envelope_api().EnvelopeError


def match_message(
    envelope: Any,
    *,
    machine: str,
    live_slugs: set[str] | None,
    registered_slugs: set[str],
) -> tuple[dict[str, str], set[str], bool]:
    """``({slug: class}, pending_slugs, broadcast_undetermined)`` from `match_recipients`.

    `broadcast_undetermined` is Phase A's `MatchResult.broadcast is None`: a `*@M`/`*@*` entry
    applies but the live-session probe could not say who is live.
    """
    if getattr(envelope, "launch_event", None) is not None:
        # Registered hook pointers can outlive their process. A carried launch must wait
        # for actual liveness; ordinary messages retain the registered-marker behavior.
        registered_slugs = registered_slugs & (live_slugs or set())
    result = _envelope_api().match_recipients(
        envelope.to,
        machine=machine,
        live=lambda: live_slugs,
        registered=registered_slugs,
    )
    return result.classes(), set(result.pending), result.broadcast is None


# ---------------------------------------------------------------------------------------------
# Fetch and enumerate (T-B.1)
# ---------------------------------------------------------------------------------------------


def fetch_refspecs(inbox_ref_prefix: str = DEFAULT_INBOX_REF_PREFIX) -> list[str]:
    return [
        f"+{inbox_ref_prefix}*:{INBOX_READ_NAMESPACE}*",
        f"+refs/heads/main:{MAIN_READ_REF}",
    ]


def source_of(ref: str) -> str:
    """`fleet-inbox/<machine>` for a message ref, `main` for the legacy read ref."""
    if ref == MAIN_READ_REF:
        return SOURCE_MAIN
    return "fleet-inbox/" + ref[len(INBOX_READ_NAMESPACE) :]


@dataclass(frozen=True)
class InboxFile:
    """One message file on one fetched tip. `blob` is what the ledger skips by before parsing."""

    ref: str
    path: str
    blob: str

    @property
    def source(self) -> str:
        return source_of(self.ref)


def is_message_path(path: str) -> bool:
    """`coordination/session-inbox/**/*.md`, minus any `README.md` and anything under `_heartbeat/`
    or `_receipts/` (ISSUE-REF: a receipt is never parsed as a message)."""
    parts = path.split("/")
    prefix = INBOX_DIR.split("/")
    if parts[: len(prefix)] != prefix or len(parts) <= len(prefix):
        return False
    if not parts[-1].endswith(MESSAGE_SUFFIX) or parts[-1] == README_NAME:
        return False
    return not {HEARTBEAT_SEGMENT, RECEIPTS_SEGMENT} & set(parts[2:-1])


def list_inbox_files(
    git: Callable[[list[str]], GitResult],
    *,
    trees: Any = None,
) -> tuple[list[str], list[InboxFile], str]:
    """Every fetched tip, every message file on them, and the first reason one was skipped."""
    from ai_chassis.git_inbox_trees import Trees

    trees = trees if trees is not None else Trees(git)
    tips = trees.tips()
    refs = sorted(tips)
    files: list[InboxFile] = []
    for ref in refs:
        for path, blob in trees.files(tips[ref]):
            if is_message_path(path):
                files.append(InboxFile(ref=ref, path=path, blob=blob))
    return refs, files, trees.reason


# ---------------------------------------------------------------------------------------------
# The delivered ledger (T-B.2)
# ---------------------------------------------------------------------------------------------


@dataclass
class LedgerView:
    """What the ledger says has been handled, folded from its records."""

    #: Ids with no target left to evaluate: skipped BEFORE matching.
    done_ids: set[str] = field(default_factory=set)
    #: Blobs of files whose id is done: skipped before the file is even read.
    done_blobs: set[str] = field(default_factory=set)
    #: id -> every slug already delivered to or held for, so a re-evaluation only adds.
    reached: dict[str, set[str]] = field(default_factory=dict)
    #: id -> when this machine first recorded it, the pending TTL's fallback base.
    first_at: dict[str, str] = field(default_factory=dict)
    #: Ids whose line reached at least one marker file (`written`, not yet seen by anyone).
    written_ids: set[str] = field(default_factory=set)
    #: (id, slug) pairs a session CLAIMED (D-9): the only thing `delivered_total` counts.
    claimed: set[tuple[str, str]] = field(default_factory=set)
    #: (id, slug) pairs whose line expired at `pending_ttl_s` with no claim.
    expired_unclaimed: set[tuple[str, str]] = field(default_factory=set)
    #: Expired pending recipients; distinct from expiry of an already written line.
    expired_targets: set[tuple[str, str]] = field(default_factory=set)
    #: (id, slug) pairs written to `held/`: D-8's visible count of what the hold withheld.
    held: set[tuple[str, str]] = field(default_factory=set)
    launch_attempted: set[str] = field(default_factory=set)
    #: id -> (source, path, blob) of the message file its first written record names.
    files: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    lines: int = 0


_ledger_holds = threading.local()


@dataclass(frozen=True)
class Ledger:
    path: Path

    @contextlib.contextmanager
    def locked(self, *, blocking: bool = True) -> Iterator[None]:
        """Serialize read, delivery and commit on a stable inode, including compaction.

        Nested ledger operations on this thread reuse the outer transaction's hold.
        """
        key = str(self.path.resolve())
        held = getattr(_ledger_holds, "paths", set())
        if key in held:
            yield
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.parent / f"{self.path.name}.lock"
        with lock_path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            _ledger_holds.paths = held | {key}
            try:
                yield
            finally:
                _ledger_holds.paths = held
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _lines(self) -> list[str]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError) as exc:
            raise LedgerUnreadableError(
                f"ledger-unreadable ({type(exc).__name__})"
            ) from exc
        return raw.splitlines()

    def read(self) -> LedgerView:
        with self.locked():
            return self._read()

    def _read(self) -> LedgerView:
        """The folded view. Raises :class:`LedgerUnreadableError` rather than return empty.

        An empty view from an unreadable file would re-deliver EVERYTHING on every tip, which is
        exactly the failure T-B.2 forbids. One tolerance only: an unparseable LAST line is a torn
        append from a crash mid-write and is ignored; :meth:`append` starts a fresh line after it.
        """
        lines = self._lines()
        view = LedgerView(lines=len(lines))
        for record in _parsed(lines):
            _fold(view, record)
        return view

    def records(self) -> list[dict[str, Any]]:
        """Every record in order, under the lock; raises :class:`LedgerUnreadableError` as `read`."""
        with self.locked():
            return list(_parsed(self._lines()))

    def append(self, records: list[dict[str, Any]]) -> None:
        with self.locked():
            return self._append(records)

    def _append(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._drop_torn_tail()
        payload = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            notifier_pass.fsync(stream.fileno())
        os.chmod(self.path, 0o600)

    def _drop_torn_tail(self) -> None:
        """Truncate a final line with no newline: the fragment of a crashed append.

        :meth:`read` already ignores it, so nothing is lost; left in place, the next append would
        turn it into a corrupt MIDDLE line and every later read would refuse.
        """
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return
        if not data or data.endswith(b"\n"):
            return
        with self.path.open("r+b") as stream:
            stream.truncate(data.rfind(b"\n") + 1)

    def compact(self, *, retention_days: int, now: dt.datetime) -> bool:
        with self.locked():
            return self._compact(retention_days=retention_days, now=now)

    def _compact(self, *, retention_days: int, now: dt.datetime) -> bool:
        """Past :data:`LEDGER_COMPACT_ABOVE` lines, keep the newest 5,000 plus everything newer
        than `retention_days`. True when it rewrote the file. Atomic: a crash leaves the old one."""
        lines = self._lines()
        if len(lines) <= LEDGER_COMPACT_ABOVE:
            return False
        horizon = now - dt.timedelta(days=retention_days)
        cut = len(lines) - LEDGER_COMPACT_KEEP
        kept = [line for line in lines[:cut] if _newer_than(line, horizon)]
        kept.extend(lines[cut:])
        handle, staged = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            os.fchmod(handle, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write("".join(line + "\n" for line in kept))
                stream.flush()
                notifier_pass.fsync(stream.fileno())
            os.replace(staged, self.path)
        except BaseException:
            Path(staged).unlink(missing_ok=True)
            raise
        return True


def _parsed(lines: list[str]) -> Iterator[dict[str, Any]]:
    """Each line's record. A torn LAST line is skipped; any other bad line raises."""
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except ValueError as exc:
            if index == len(lines) - 1:
                continue
            raise LedgerUnreadableError(
                f"ledger-unreadable (line {index + 1} is not JSON)"
            ) from exc
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise LedgerUnreadableError(
                f"ledger-unreadable (line {index + 1} has no id)"
            )
        yield record


def _newer_than(line: str, horizon: dt.datetime) -> bool:
    try:
        record = json.loads(line)
    except ValueError:
        return False
    moment = _parse_moment(record.get("at")) if isinstance(record, dict) else None
    return moment is not None and moment >= horizon


def _fold(view: LedgerView, record: dict[str, Any]) -> None:
    message_id = record["id"]
    if record.get("outcome") == "launch-attempted":
        view.launch_attempted.add(message_id)
        view.first_at.setdefault(message_id, str(record.get("at") or ""))
        return
    slug = record.get("slug")
    if isinstance(slug, str) and record.get("outcome") in SETTLED_OUTCOMES:
        # A claim or expiry settles a line ALREADY written: it must not touch the dedupe state, or
        # a claim for one target would mark an id with a still-pending target done.
        settled = (
            view.claimed
            if record["outcome"] == OUTCOME_DELIVERED
            else view.expired_unclaimed
        )
        settled.add((message_id, slug))
        return
    reached = view.reached.setdefault(message_id, set())
    reached.update(str(slug) for slug in record.get("targets") or [])
    reached.update(str(slug) for slug in record.get("held") or [])
    view.first_at.setdefault(message_id, str(record.get("at") or ""))
    if record.get("targets"):
        view.written_ids.add(message_id)
    view.held.update((message_id, str(slug)) for slug in record.get("held") or [])
    view.expired_targets.update(
        (message_id, str(slug)) for slug in record.get("expired") or []
    )
    blob = record.get("blob")
    if isinstance(blob, str) and blob:
        view.files.setdefault(
            message_id, (str(record.get("source")), str(record.get("path")), blob)
        )
    if record.get("pending") or record.get("partial"):
        view.done_ids.discard(message_id)
        return
    view.done_ids.add(message_id)
    if isinstance(blob, str) and blob:
        view.done_blobs.add(blob)


def settled_record(
    *, message_id: str, slug: str, outcome: str, at: str
) -> dict[str, Any]:
    """One D-9 record: `delivered` on a claim, `expired-unclaimed` at the TTL, for ONE slug."""
    if outcome not in SETTLED_OUTCOMES:
        raise ValueError(f"not a settled outcome: {outcome!r}")
    return {"id": message_id, "slug": slug, "outcome": outcome, "at": at}


def ledger_record(  # noqa: PLR0913  # one keyword per field of the design's record
    *,
    message_id: str,
    file: InboxFile,
    outcome: str,
    at: str,
    classes: dict[str, str] | None = None,
    held: list[str] | None = None,
    pending: list[str] | None = None,
) -> dict[str, Any]:
    """One `delivered.jsonl` record, in the design's § Data Model shape plus `outcome`.

    `layers` carries the hook layer's outcome only; Phase C's layer loop adds the others. That
    outcome is `written` (D-9): the marker file holds the line, and only a claim says a session saw it.
    """
    targets = sorted(classes or {})
    message_class = ""
    if classes:
        message_class = "direct" if "direct" in classes.values() else "broadcast"
    return {
        "id": message_id,
        "source": file.source,
        "path": file.path,
        "blob": file.blob,
        "class": message_class,
        "classes": dict(sorted((classes or {}).items())),
        "targets": targets,
        "held": sorted(held or []),
        "pending": sorted(pending or []),
        "outcome": outcome,
        "layers": {LAYER_HOOK: OUTCOME_WRITTEN} if targets else {},
        "at": at,
    }


# ---------------------------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------------------------


@dataclass
class InboxBatch:
    """One due poll's output: what the notifier renders, holds and then commits."""

    #: slug -> the `agent_mail_notifier.Message`s that session must see.
    per_slug: dict[str, list[Any]] = field(default_factory=dict)
    #: Messages addressed to the machine rather than to one session.
    machine: list[Any] = field(default_factory=list)
    #: Records for `held/`, in the notifier's `_write_held` shape.
    held: list[dict[str, Any]] = field(default_factory=list)
    #: `Message.message_id` -> `<ref>:<path>`, so a notice can say where the body is.
    locations: dict[int, str] = field(default_factory=dict)
    #: `Message.message_id` -> the envelope id its claim or expiry record names (D-9).
    ids: dict[int, str] = field(default_factory=dict)
    #: Ledger records to append once the markers are on disk.
    records: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Pass:
    """One due poll's working state, so the per-file steps share it without a long signature."""

    view: LedgerView
    live: set[str] | None
    registered: set[str]
    now: dt.datetime
    max_bytes: int
    pending_ttl_s: int
    allow: frozenset[str]
    classes: dict[str, str] | None
    batch: InboxBatch = field(default_factory=InboxBatch)
    seen_ids: set[str] = field(default_factory=set)
    pending_report: list[dict[str, Any]] = field(default_factory=list)

    @property
    def at(self) -> str:
        return _iso(self.now)

    def record(
        self, message_id: str, file: InboxFile, outcome: str, **fields: Any
    ) -> dict[str, Any]:
        record = ledger_record(
            message_id=message_id, file=file, outcome=outcome, at=self.at, **fields
        )
        self.batch.records.append(record)
        return record


def to_message(envelope: Any, *, message_class: str, now: dt.datetime) -> Any:
    """One `agent_mail_notifier.Message` for a git-inbox envelope.

    The int id is derived the way Buzz derives its own (`buzz_channel.event_message_id`), from the
    envelope id, so the same message renders the same id on every tick. Imported lazily because
    the notifier imports this module.
    """
    from ai_chassis.agent_mail_notifier import (
        RUNG_GIT_INBOX,
        Message,
        _sanitise,
        _subject,
    )
    from ai_chassis.buzz_channel import event_message_id

    sent = _parse_moment(getattr(envelope, "sent", None))
    age_s = max(0, int((now - sent).total_seconds())) if sent else 0
    return Message(
        message_id=event_message_id(str(envelope.id)),
        sender=_sanitise(address_text(envelope.from_)),
        subject=_subject(str(getattr(envelope, "re", "") or "")),
        age_s=age_s,
        ack_required=False,
        message_class=message_class,
        rung=RUNG_GIT_INBOX,
        reply=_sanitise(str(getattr(envelope, "reply", "") or "")),
        requires_action=True if getattr(envelope, "actions", ()) else None,
    )


#: The ledger id of a sender's `taken` notice: `taken:<message id>:<taker>` (ISSUE-REF D-6).
TAKEN_NOTICE_PREFIX = "taken:"


@dataclass(frozen=True)
class TakenNotice:
    """A `taken` receipt, shaped as an envelope so it routes like any message (ISSUE-REF D-6).

    Addressed to the message's sending session on this machine, from the taker, so the D-8 hold
    reads the taker's address against `allow_senders` and D-9 keeps the line until it is claimed.
    """

    id: str
    from_: Any
    to: tuple[Any, ...]
    sent: str
    re: str
    reply: str
    body: str = ""
    launch_event: object | None = None


def carried_message(body: Any) -> str | None:
    """The first prompt a launch envelope carries; a non-JSON or non-object body carries none."""
    try:
        carried = json.loads(body)
    except (TypeError, ValueError):
        return None
    message = carried.get("message") if isinstance(carried, dict) else None
    return message if isinstance(message, str) else None


@dataclass
class GitInboxWatcher:
    """The git-inbox message rung. Fetches at its own cadence; everything else is pure reading."""

    repo: Path
    machine: str
    ledger_path: Path
    #: `[git_inbox_notify]`, re-read on every poll so an overlay `enabled = false` takes effect on
    #: the next tick without a restart (D-5).
    config_loader: Callable[[], dict[str, Any]]
    remote: str = "origin"
    runner: GitRunner = run_git
    log: Callable[[str], None] = field(default=lambda _message: None)
    #: slug -> `bypass|normal|unknown` (`buzz_channel.permission_classes`), or None for no probe.
    permission_probe: Callable[[], dict[str, str]] | None = None
    launch_handler: Callable[..., Any] | None = None
    defer_launch_delivery: bool = False
    last_poll_at: float | None = None
    last_fetch_at: str | None = None
    last_fetch_ok: bool | None = None
    reason: str = ""
    refs_seen: list[str] = field(default_factory=list)
    messages_seen: int | None = None
    #: Claims (D-9), not marker writes; `written_total` counts those.
    delivered_total: int | None = None
    written_total: int | None = None
    expired_unclaimed_total: int | None = None
    held_total: int | None = None
    pending_targets: list[dict[str, Any]] = field(default_factory=list)
    enabled: bool = True
    #: ISSUE-REF Phase A1: `[git_inbox_notify].receipts`, re-read on every write.
    receipts_enabled: bool = True
    receipts_written_total: int = 0
    receipts_pending: int | None = None
    #: D-6: `{message_id, recipient, since}` for each take of a message sent here, unconfirmed.
    unconfirmed_takes: list[dict[str, Any]] = field(default_factory=list)
    _receipts_reader: Any = field(default=None, repr=False)
    _sent_messages: dict[str, Any] = field(default_factory=dict, repr=False)
    _actionless_blobs: dict[str, bool] = field(default_factory=dict, repr=False)
    delivery_scope: set[str] | None = field(default=None, kw_only=True)
    excluded_slugs: set[str] = field(default_factory=set, kw_only=True)
    #: In-memory first sighting of a pending id whose `SENT:` is unreadable.
    _first_seen: dict[str, dt.datetime] = field(default_factory=dict)

    @property
    def ledger(self) -> Ledger:
        return Ledger(self.ledger_path)

    def _git(self, args: list[str]) -> GitResult:
        return self.runner(args, self.repo, None)

    def due(self, *, interval_s: int, now: float | None = None) -> bool:
        """Whether `interval_s` has elapsed since the last ATTEMPT, as `GitInboxPublisher.due`."""
        moment = now if now is not None else _utcnow().timestamp()
        if self.last_poll_at is None:
            return True
        return moment - self.last_poll_at >= interval_s

    def _permission_classes(self) -> dict[str, str] | None:
        if self.permission_probe is None:
            return None
        try:
            return dict(self.permission_probe())
        except Exception:  # noqa: BLE001 - a probe that raised classifies every slug `unknown`
            self.log("git-inbox permission probe raised; every target is held")
            return {}

    def fetch(self, inbox_ref_prefix: str) -> None:
        """One fetch of both namespaces. A failure is a REASON; the last tips stay readable."""
        fetched = self._git(
            [
                "fetch",
                "--quiet",
                "--prune",
                self.remote,
                *fetch_refspecs(inbox_ref_prefix),
            ]
        )
        self.last_fetch_at = _iso(_utcnow())
        self.last_fetch_ok = fetched.rc == 0
        self._receipt_reader()._trees().invalidate()
        self.reason = (
            ""
            if fetched.rc == 0
            else f"remote: unreachable ({_refusal_reason(fetched)})"
        )
        if fetched.rc != 0:
            self.log(f"git-inbox fetch failed; {self.reason}")

    def fetch_on_event(self) -> None:
        """One verified fetch, without the legacy cadence or cached-tip fallback."""
        cfg = self.config_loader()
        self.fetch(str(cfg.get("inbox_ref_prefix") or DEFAULT_INBOX_REF_PREFIX))
        if self.last_fetch_ok is not True:
            raise OSError("inbox-fetch-unverified")

    def poll(
        self,
        *,
        live: set[str] | None,
        registered: set[str],
        now: dt.datetime | None = None,
    ) -> InboxBatch:
        """One due pass: fetch, enumerate, dedupe, match. An empty batch when not due."""
        moment = now or _utcnow()
        cfg = self.config_loader()
        self.enabled = cfg.get("enabled", True) is not False
        if not self.enabled:
            return InboxBatch()
        if not self.due(interval_s=poll_interval_s(cfg), now=moment.timestamp()):
            return InboxBatch()
        self.last_poll_at = moment.timestamp()
        self.fetch(str(cfg.get("inbox_ref_prefix") or DEFAULT_INBOX_REF_PREFIX))
        refs, files, list_reason = list_inbox_files(
            self._git, trees=self._receipt_reader()._trees()
        )
        self.refs_seen = refs
        if not refs:
            self.messages_seen = None
            if not self.last_fetch_ok:
                self.reason = f"{self.reason}; no tip has ever been fetched"
            return InboxBatch()
        self.messages_seen = len(files)
        if list_reason and not self.reason:
            self.reason = list_reason
        try:
            view = self.ledger.read()
        except LedgerUnreadableError as exc:
            self.reason = str(exc)
            self.log(f"git-inbox delivers nothing this tick: {exc}")
            return InboxBatch()
        self._totals(view)
        return self._evaluate(
            files, view, cfg=cfg, live=live, registered=registered, now=moment
        )

    def _evaluate(  # noqa: PLR0913  # one keyword per fact the pass needs
        self,
        files: list[InboxFile],
        view: LedgerView,
        *,
        cfg: dict[str, Any],
        live: set[str] | None,
        registered: set[str],
        now: dt.datetime,
    ) -> InboxBatch:
        run = _Pass(
            view=view,
            live=live,
            registered=registered,
            now=now,
            max_bytes=_int_setting(cfg, "max_bytes", DEFAULT_MAX_BYTES),
            pending_ttl_s=pending_ttl_s(cfg),
            allow=frozenset(str(s) for s in cfg.get("allow_senders") or []),
            classes=self._permission_classes(),
        )
        for file in files:
            if file.blob in view.done_blobs:
                continue
            envelope = self._read_envelope(file, run)
            if envelope is None:
                continue
            message_id = str(envelope.id)
            if message_id in view.done_ids or message_id in run.seen_ids:
                continue
            run.seen_ids.add(message_id)
            if len(str(envelope.body).encode("utf-8")) > run.max_bytes:
                run.record(message_id, file, OUTCOME_REFUSED_OVERSIZE)
                continue
            self._route(file, envelope, run)
        self._route_takes(run)
        self.pending_targets = run.pending_report
        return run.batch

    def _receipt_reader(self) -> Any:
        from ai_chassis import git_inbox_receipts as receipts

        if self._receipts_reader is None:
            self._receipts_reader = receipts.ReceiptReader(self._git)
        return self._receipts_reader

    def _sent_message(self, message_ref: str) -> Any | None:
        """The envelope a receipt's `message_ref` names, cached; None when unreadable."""
        if message_ref not in self._sent_messages:
            ref, _, path = message_ref.partition(":")
            shown = self._git(["show", message_ref]) if ref and path else None
            envelope = None
            if shown is not None and shown.rc == 0:
                with contextlib.suppress(envelope_error()):
                    envelope = _envelope_api().parse_envelope(
                        shown.out + "\n",
                        path=path,
                        source=source_of(ref),
                        max_bytes=None,
                    )
            self._sent_messages[message_ref] = envelope
        return self._sent_messages[message_ref]

    def _claimed_actionless(
        self, view: LedgerView, key: str, recipients: list[str]
    ) -> bool:
        """A message with no ACTIONS cannot be taken (`git-inbox take` refuses it), so its
        recipients' claims are the only completion it has (ISSUE-REF)."""
        from ai_chassis.buzz_channel import event_message_id

        claimed = {
            message_id for message_id, slug in view.claimed if slug in recipients
        }
        return any(
            str(event_message_id(message_id)) == key
            and message_id in view.files
            and all((message_id, slug) in view.claimed for slug in recipients)
            and self._actionless(view.files[message_id])
            for message_id in claimed
        )

    def _actionless(self, file: tuple[str, str, str]) -> bool:
        """Whether the message file carries no ACTIONS; unreadable reads as carrying some."""
        source, path, blob = file
        if blob not in self._actionless_blobs:
            shown = self._git(["cat-file", "blob", blob])
            envelope = None
            if shown.rc == 0:
                with contextlib.suppress(envelope_error()):
                    envelope = parse_message(
                        shown.out, path=path, source=source, max_bytes=None
                    )
            if envelope is None:
                return False
            self._actionless_blobs[blob] = not getattr(envelope, "actions", ())
        return self._actionless_blobs[blob]

    def _route_takes(self, run: _Pass) -> None:
        """D-6: a `taken` receipt for a message a session HERE sent, not yet confirmed, becomes
        one direct notice to that session, routed like any message (hold, pending, D-9)."""
        from ai_chassis import recipient_state as rs

        found = self._receipt_reader().found()
        confirmed = {
            (f.record.message_id, f.record.recipient)
            for f in found
            if f.record.event == rs.EVENT_CONFIRMED
        }
        unconfirmed: list[dict[str, Any]] = []
        for item in found:
            record = item.record
            key = (record.message_id, record.recipient)
            if record.event != rs.EVENT_TAKEN or key in confirmed:
                continue
            if rs.is_foreign(item, sender_machine=None):
                continue
            sent = self._sent_message(str(record.extra.get("message_ref") or ""))
            if sent is None or sent.from_ is None or sent.from_.machine != self.machine:
                continue
            unconfirmed.append(
                {
                    "message_id": record.message_id,
                    "recipient": record.recipient,
                    "since": record.at,
                }
            )
            notice_id = f"{TAKEN_NOTICE_PREFIX}{record.message_id}:{record.recipient}"
            if notice_id in run.view.done_ids or notice_id in run.seen_ids:
                continue
            run.seen_ids.add(notice_id)
            api = _envelope_api()
            try:
                taker = api.parse_address(record.recipient)
            except ValueError:
                self.log(
                    f"git-inbox taken receipt names no address: {record.recipient!r}"
                )
                continue
            notice = TakenNotice(
                id=notice_id,
                from_=taker,
                to=(api.Address(slug=sent.from_.slug, machine=self.machine),),
                sent=record.at,
                re=f"taken: {sent.re}",
                reply=f"git-inbox confirm {record.message_id}",
            )
            ref, _, path = item.location.partition(":")
            self._route(InboxFile(ref=ref, path=path, blob=""), notice, run)
        self.unconfirmed_takes = sorted(
            unconfirmed, key=lambda t: (t["since"], t["message_id"], t["recipient"])
        )

    def _read_envelope(self, file: InboxFile, run: _Pass) -> Any | None:
        """The parsed envelope, or None with a `refused(...)` record for a file it refuses."""
        shown = self._git(["cat-file", "blob", file.blob])
        if shown.rc != 0:
            self.reason = self.reason or f"cat-file-failed ({file.path})"
            return None
        try:
            return parse_message(
                shown.out, path=file.path, source=file.source, max_bytes=run.max_bytes
            )
        except envelope_error() as exc:
            fallback_id = f"{file.source}:{file.path}"
            if fallback_id not in run.view.done_ids:
                outcome = _refusal_outcome(str(getattr(exc, "reason", "") or exc))
                self.log(f"git-inbox {outcome}: {fallback_id}")
                run.record(fallback_id, file, outcome)
            return None

    def _targets(
        self, envelope: Any, run: _Pass
    ) -> tuple[dict[str, str], set[str], list[str]]:
        """``({slug: class} not yet reached, still-pending slugs, slugs whose wait expired)``."""
        message_id = str(envelope.id)
        already = run.view.reached.get(message_id, set())
        if envelope.re.startswith("session:") and any(
            address.slug == "*" and address.machine == self.machine
            for address in envelope.to
        ):
            # machine:<host> normalises to *@host. Commands belong to the host even
            # when liveness resolves to zero or many sessions, never to their notices.
            targets = {} if MACHINE_TARGET in already else {MACHINE_TARGET: "direct"}
            return targets, set(), []
        targets, pending, undetermined = match_message(
            envelope,
            machine=self.machine,
            live_slugs=run.live,
            registered_slugs=run.registered,
        )
        if undetermined:
            # Who is live is unknown, so the broadcast is carried on the MACHINE notice rather
            # than dropped, as Buzz carries one (ISSUE-REF D-8: undetermined is never "none").
            targets[MACHINE_TARGET] = CLASS_BROADCAST
        if self.delivery_scope is not None:
            targets = {s: c for s, c in targets.items() if s in self.delivery_scope}
            pending &= self.delivery_scope
        targets = {s: c for s, c in targets.items() if s not in self.excluded_slugs}
        pending -= self.excluded_slugs
        targets = {s: c for s, c in targets.items() if s not in already}
        if MACHINE_TARGET in already:
            # Everyone on the machine was already told; a later per-session broadcast repeats it.
            targets = {s: c for s, c in targets.items() if c != CLASS_BROADCAST}
        pending -= already | set(targets)
        if (
            pending
            and self._pending_age_s(envelope, run.view, run.now) > run.pending_ttl_s
        ):
            return targets, set(), sorted(pending)
        return targets, pending, []

    def _attempt_launch(
        self,
        envelope: Any,
        message_id: str,
        pending: set[str],
        run: _Pass,
        *,
        live_target: bool = False,
    ) -> Any:
        """Write the launch attempt ahead of the action, then hand the event to the launcher."""
        if (
            (not pending and not live_target)
            or run.live is None
            or getattr(envelope, "launch_event", None) is None
        ):
            return
        # Write ahead of the dangerous action. Even a crash before normal batch commit
        # cannot cause the same message to start another process on the next tick.
        record = {"id": message_id, "outcome": "launch-attempted", "at": run.at}
        attempted = message_id in run.view.launch_attempted
        if not attempted:
            self.ledger.append([record])
            run.view.launch_attempted.add(message_id)
        exact = [
            a.slug for a in envelope.to if a.machine == self.machine and a.slug != "*"
        ]
        target = exact[0] if len(exact) == 1 and len(envelope.to) == 1 else ""
        # A live recipient may restart, but must never be launched a second time.
        restrictions: dict[str, Any] = (
            {"required_verb": "restart"} if live_target else {}
        )
        restrictions["message"] = carried_message(envelope.body)
        if self.defer_launch_delivery:
            restrictions["wait_for_bind"] = True
        if attempted:
            restrictions["reconcile_only"] = True
        if self.launch_handler is not None:
            result = self.launch_handler(
                envelope.launch_event, target, run.now, **restrictions
            )
            return result[0] if isinstance(result, tuple) else result
        else:
            from ai_chassis import remote_launch

            return remote_launch.handle_git_inbox_event(
                envelope.launch_event,
                target=target,
                channel=None,
                machine=self.machine,
                now=int(run.now.timestamp()),
                config_tables=remote_launch.read_tables(),
                state_dir=remote_launch.default_state_dir(),
                projects_root=remote_launch.default_projects_root(),
                **restrictions,
            )[0]

    def _is_host_session_command(self, envelope: Any) -> bool:
        """A ``session:`` command addressed to every session on this host."""
        return envelope.re.startswith("session:") and any(
            address.slug == "*" and address.machine == self.machine
            for address in envelope.to
        )

    def _scoped(self) -> bool:
        """True when this pass serves only part of the machine's recipients."""
        return self.delivery_scope is not None or bool(self.excluded_slugs)

    def _route(self, file: InboxFile, envelope: Any, run: _Pass) -> None:
        """Match, then deliver or hold per target. Every target not yet reached is evaluated."""
        message_id = str(envelope.id)
        targets, pending, expired = self._targets(envelope, run)
        if not any((targets, pending, expired)):
            if not self._scoped() and message_id not in run.view.reached:
                run.record(message_id, file, OUTCOME_NOT_FOR_MACHINE)
            return
        if MACHINE_TARGET in targets and self._is_host_session_command(envelope):
            self._apply_session_command(file, envelope, run)
            return
        launched = self._attempt_launch(
            envelope,
            message_id,
            pending,
            run,
            live_target=bool(targets) and not pending,
        )
        if self._settle_launch(launched, pending, message_id, file, run):
            return
        delivered, held = self._batch_messages(file, envelope, targets, run)
        run.pending_report.extend(
            {"id": message_id, "slug": slug} for slug in sorted(pending)
        )
        if not (delivered or held or expired):
            return
        outcome = (
            OUTCOME_WRITTEN if delivered else OUTCOME_HELD if held else OUTCOME_EXPIRED
        )
        record = run.record(
            message_id,
            file,
            outcome,
            classes=delivered,
            held=held,
            pending=sorted(pending),
        )
        if self._scoped():
            # A scoped pass cannot declare a whole envelope done for other recipients.
            record["partial"] = True
        if expired:
            record["expired"] = expired

    def _batch_messages(self, file, envelope, targets, run):
        from ai_chassis.buzz_channel import is_held

        message_id = str(envelope.id)
        sender = address_text(envelope.from_)
        delivered: dict[str, str] = {}
        held: list[str] = []
        for slug, message_class in sorted(targets.items()):
            message = to_message(envelope, message_class=message_class, now=run.now)
            if is_held(slug, run.classes, sender, run.allow):
                held.append(slug)
                run.batch.held.append(
                    _held_record(
                        slug,
                        sender=sender,
                        file=file,
                        message=message,
                        classes=run.classes,
                        at=run.at,
                    )
                )
                continue
            delivered[slug] = message_class
            if slug == MACHINE_TARGET:
                run.batch.machine.append(message)
            else:
                run.batch.per_slug.setdefault(slug, []).append(message)
            run.batch.locations[message.message_id] = f"{file.ref}:{file.path}"
            run.batch.ids[message.message_id] = message_id
        return delivered, held

    def _settle_launch(self, launched, pending, message_id, file, run):  # noqa: PLR0913 - one delivery's ledger inputs
        if launched is not None and getattr(launched, "reason", "") == "quota-held":
            # Keep the source and reservation pending. No ordinary notice may
            # wake the recipient around the authenticated launch admission gate.
            return True
        if launched is not None and getattr(launched, "message_held", False):
            # Ownership has moved to the durable restart request before engine start.
            # Neither marker nor nudge may expose its message while that engine runs.
            record = run.record(message_id, file, "restart-message-held")
            record["request_id"] = launched.request_id
            self.ledger.append([record])
            run.view.done_ids.add(message_id)
            return True
        if (
            launched is None
            or not launched.launched_identity
            or launched.verdict != "SENT"
        ):
            return False
        # Settle the requested address durably before another live poll can deliver again.
        classes = {slug: "direct" for slug in pending}
        record = run.record(message_id, file, OUTCOME_WRITTEN, classes=classes)
        record["layers"] = {"prompt": "delivered"}
        self.ledger.append([record])
        run.view.reached.setdefault(message_id, set()).update(pending)
        return True

    def _apply_session_command(
        self, file: InboxFile, envelope: Any, run: _Pass
    ) -> None:
        """Execute once for the host, after the same sender hold as ordinary delivery."""
        with self.ledger.locked():
            if str(envelope.id) in self.ledger.read().done_ids:
                return
            self._record_session_command(file, envelope, run)

    def _record_session_command(
        self, file: InboxFile, envelope: Any, run: _Pass
    ) -> None:
        from ai_chassis.buzz_channel import is_held
        from ai_chassis.git_inbox_session import apply_command

        sender = address_text(envelope.from_)
        held = is_held(MACHINE_TARGET, run.classes, sender, run.allow)
        outcome = "refused"
        result = None
        if held:
            run.batch.held.append(
                _held_record(
                    MACHINE_TARGET,
                    sender=sender,
                    file=file,
                    message=to_message(envelope, message_class="direct", now=run.now),
                    classes=run.classes,
                    at=run.at,
                )
            )
        elif envelope.from_ is not None:
            # Settle the id durably BEFORE any side effect. A crash after reservation
            # leaves an attempted outcome for inspection, never an automatic replay.
            self.ledger.append(
                [
                    ledger_record(
                        message_id=str(envelope.id),
                        file=file,
                        outcome="session-attempted",
                        at=run.at,
                    )
                ]
            )
            result = apply_command(envelope.body)
            outcome = result.outcome
        record = run.record(
            str(envelope.id),
            file,
            f"session-{outcome}",
            classes={} if held else {MACHINE_TARGET: "direct"},
            held=[MACHINE_TARGET] if held else [],
        )
        record["session_command"] = outcome
        if result is not None:
            record.update(exit_status=result.exit_status, output=result.output)
        # A command settles at execution, without a session notice/claim.
        record["layers"] = {}
        if result is not None:
            self.ledger.append([record])
            _fold(run.view, record)
            run.batch.records.remove(record)
        self.log(f"git-inbox session-{outcome}: {envelope.id}")

    def _pending_age_s(
        self, envelope: Any, view: LedgerView, now: dt.datetime
    ) -> float:
        message_id = str(envelope.id)
        base = _parse_moment(getattr(envelope, "sent", None)) or _parse_moment(
            view.first_at.get(message_id)
        )
        if base is None:
            base = self._first_seen.setdefault(message_id, now)
        return (now - base).total_seconds()

    def commit(self, batch: InboxBatch, *, now: dt.datetime | None = None) -> None:
        """Append the batch's ledger records, then compact if due. Called AFTER the markers."""
        if not batch.records:
            return
        self.ledger.append(batch.records)
        cfg = self.config_loader()
        self.ledger.compact(
            retention_days=_int_setting(cfg, "retention_days", DEFAULT_RETENTION_DAYS),
            now=now or _utcnow(),
        )
        self._refresh_totals()

    def record_settled(self, records: list[dict[str, Any]]) -> None:
        """Append D-9 claim and expiry records (:func:`settled_record`), then refresh the totals."""
        if not records:
            return
        self.ledger.append(records)
        self._refresh_totals()

    def action_state(self, key: str, recipients: list[str]) -> str:  # noqa: C901 - fold source receipts before delivery state
        """Delivery/TTL are not completion. Consult the existing ledger and receipts."""
        from ai_chassis import recipient_state as rs
        from ai_chassis.buzz_channel import event_message_id

        view = self.ledger.read()
        reader = self._receipt_reader()
        found = reader.found()
        if reader.reason:
            return "unavailable"
        targets = {f"{slug}@{self.machine}" for slug in recipients}
        completed = set()
        grouped: dict[str, list[Any]] = {}
        for item in found:
            record = item.record
            grouped.setdefault(record.message_id, []).append(item)
        for message_id, receipts in grouped.items():
            original = str(event_message_id(message_id)) == key
            notice = any(
                str(
                    event_message_id(
                        f"{TAKEN_NOTICE_PREFIX}{message_id}:{item.record.recipient}"
                    )
                )
                == key
                for item in receipts
            )
            if not original and not notice:
                continue
            # A confirmation is written on the original sender's ref. For a
            # taken-notice that sender is this host; recipient actions close at take.
            folded = rs.fold(receipts, sender_machine=self.machine if notice else None)
            for recipient, state in folded.rung(rs.RUNG_GIT_INBOX).items():
                if (
                    notice
                    and state.state == rs.CONFIRMED
                    and str(
                        event_message_id(
                            f"{TAKEN_NOTICE_PREFIX}{message_id}:{recipient}"
                        )
                    )
                    == key
                ):
                    return "completed"
                if original and recipient in targets:
                    if rs.EVENT_TAKEN in state.events:
                        completed.add(recipient)
                    elif state.state == rs.EXPIRED:
                        return "expired"
        if (completed == targets and targets) or self._claimed_actionless(
            view, key, recipients
        ):
            return "completed"
        for pairs, state in (
            (view.held, "held"),
            (view.expired_unclaimed | view.expired_targets, "expired"),
        ):
            if any(
                str(event_message_id(message_id)) == key and slug in recipients
                for message_id, slug in pairs
            ):
                return state
        return "pending"

    def action_taken(self, message_id: str) -> bool:
        """Whether a fetched ref carries a `taken` or `confirmed` receipt for ``message_id``.

        ISSUE-REF D-11: a read line with actions leaves the notice once ANY recipient has taken
        it, because one take covers every recipient (§ States and receipts). A foreign receipt
        (one on a ref its event's writer does not own) does not count; an unreadable fetch reads
        as not taken, so the line stays rather than vanishing.
        """
        from ai_chassis import recipient_state as rs

        reader = self._receipt_reader()
        found = reader.found(message_id)
        if reader.reason:
            return False
        return any(
            item.record.event in {rs.EVENT_TAKEN, rs.EVENT_CONFIRMED}
            and not rs.is_foreign(item, sender_machine=None)
            for item in found
        )

    def write_receipts(
        self,
        settled: list[dict[str, Any]],
        *,
        locations: dict[tuple[str, str], str] | None = None,
        now: dt.datetime | None = None,
    ) -> None:
        """Turn D-9 settles into `read`/`expired` receipts on this machine's ref (ISSUE-REF D-2).

        Called by the notifier AFTER the marker write and outside the marker lock: it pushes. Every
        call also retries the pending set, so an empty `settled` still lands earlier failures. A
        machine-notice settle names no recipient and writes none.
        """
        from ai_chassis import git_inbox_receipts as receipts
        from ai_chassis import recipient_state as rs

        cfg = self.config_loader()
        self.receipts_enabled = cfg.get("receipts", True) is not False
        if not self.receipts_enabled:
            return
        events = {
            OUTCOME_DELIVERED: rs.EVENT_READ,
            OUTCOME_EXPIRED_UNCLAIMED: rs.EVENT_EXPIRED,
        }
        moment = now or _utcnow()
        records = []
        for settle in settled:
            event = events.get(settle.get("outcome"))
            slug, message_id = settle.get("slug"), settle.get("id")
            if event is None or not slug or not message_id or slug == MACHINE_TARGET:
                continue
            if str(message_id).startswith(TAKEN_NOTICE_PREFIX):
                continue  # a sender's taken notice is not a message; its claim is no read
            try:
                records.append(
                    receipts.build(
                        message_id=str(message_id),
                        recipient=f"{slug}@{self.machine}",
                        event=event,
                        at=str(settle.get("at") or _iso(moment)),
                        message_ref=(locations or {}).get((message_id, slug), ""),
                        # D-9/D-12: a read that was not a hook claim says which it was.
                        evidence={"via": settle["via"]} if settle.get("via") else None,
                    )
                )
            except rs.RecordError as exc:
                self.log(f"git-inbox receipt skipped for {message_id}: {exc}")
        writer = receipts.writer_for(
            repo=self.repo,
            machine=self.machine,
            remote=self.remote,
            state_dir=self.ledger_path.parent,
            cfg=cfg,
            runner=self.runner,
            log=self.log,
        )
        outcome = writer.commit(records, now=moment)
        self.receipts_written_total += len(outcome.committed)
        self.receipts_pending = outcome.pending
        if not outcome.ok:
            self.log(
                f"git-inbox receipts pending ({outcome.pending}): {outcome.reason}"
            )

    def _refresh_totals(self) -> None:
        try:
            self._totals(self.ledger.read())
        except LedgerUnreadableError as exc:
            self.reason = str(exc)

    def _totals(self, view: LedgerView) -> None:
        self.delivered_total = len(view.claimed)
        self.written_total = len(view.written_ids)
        self.expired_unclaimed_total = len(view.expired_unclaimed)
        self.held_total = len(view.held)

    def status(self) -> dict[str, Any] | str:
        """The `git_inbox` block of the notifier's status record."""
        if not self.enabled:
            return STATUS_DISABLED
        block: dict[str, Any] = {
            "last_fetch_at": self.last_fetch_at,
            "last_fetch_ok": self.last_fetch_ok,
            "reason": self.reason,
            "refs_seen": list(self.refs_seen),
            "written_total": self.written_total,
            "delivered_total": self.delivered_total,
            "expired_unclaimed_total": self.expired_unclaimed_total,
            "held": self.held_total,
            "pending_targets": list(self.pending_targets),
            "receipts": "enabled" if self.receipts_enabled else STATUS_DISABLED,
            "receipts_written_total": self.receipts_written_total,
            "receipts_pending": self.receipts_pending,
            "unconfirmed_takes": list(self.unconfirmed_takes),
        }
        if self.messages_seen is not None:
            block["messages_seen"] = self.messages_seen
        return block


def _refusal_outcome(reason: str) -> str:
    """`refused(oversize)` or `refused(malformed:<why>)` from Phase A's `EnvelopeError.reason`."""
    if reason.startswith("oversize"):
        return OUTCOME_REFUSED_OVERSIZE
    if reason.startswith("malformed:"):
        return f"refused({reason.split(' ', 1)[0]})"
    return f"refused(malformed:{reason.split(':', 1)[0] or 'unparseable'})"


def _held_record(  # noqa: PLR0913  # one keyword per field of the held record
    slug: str,
    *,
    sender: str,
    file: InboxFile,
    message: Any,
    classes: dict[str, str] | None,
    at: str,
) -> dict[str, Any]:
    from ai_chassis.agent_mail_notifier import RUNG_GIT_INBOX
    from ai_chassis.buzz_channel import PERMISSION_UNKNOWN

    permission_class = (classes or {}).get(slug, PERMISSION_UNKNOWN)
    return {
        "event_id": f"git-inbox-{message.message_id}-{slug}",
        "rung": RUNG_GIT_INBOX,
        "session": slug,
        "sender": sender,
        "source": file.source,
        "path": file.path,
        "permission_class": permission_class,
        "reason": f"{permission_class}-permissions-sender-not-allow-listed",
        "held_at": at,
        "subject": message.subject,
    }
