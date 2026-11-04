"""git-inbox lifecycle receipts: paths, the pending set, the writer and the reader (ISSUE-REF).

Design `docs/designs/issue-ref-git-inbox-lifecycle-and-harness-update-notice.md`, D-1 and
§ Data Model. A receipt is one small JSON file in the shared recipient-state shape
(:mod:`ai_chassis.recipient_state`) at

    coordination/session-inbox/_receipts/<message-dir>/<slug>@<machine>.<event>.json

on the ACTING machine's own `refs/heads/fleet-inbox/<machine>`, committed with
:class:`git_inbox.InboxSender`'s plumbing (fast-forward, one re-fetch on a race). No machine ever
writes another machine's ref.

THE PATH IS DETERMINISTIC, SO A RECEIPT IS WRITTEN AT MOST ONCE. `InboxSender.commit_files` keeps
a blob already on the tip, so a second write of the same (message, recipient, event) is a no-op
tree and pushes nothing.

A FAILED PUSH LOSES NOTHING. :class:`ReceiptWriter` folds the on-disk pending set into every write
and rewrites it with whatever did not land, under one flock shared by the notifier and the CLI
verbs that also write receipts.

RETENTION. A receipt ages out with the message it names: the sender's tip rebuild reads the
message id's leading stamp from the directory name (`git_inbox.retention_stamp`). A message id
with no stamp keeps its receipts.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_chassis import git_inbox, notifier_pass
from ai_chassis import recipient_state as rs
from ai_chassis.fleet_liveness import GitResult, _refusal_reason
from ai_chassis.git_inbox_trees import Trees

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    from ai_chassis import windows_file_lock as fcntl  # type: ignore[no-redef]

RUNG = rs.RUNG_GIT_INBOX
RECEIPTS_DIR = f"{git_inbox.INBOX_DIR}/{git_inbox.RECEIPTS_DIR_NAME}"
PENDING_NAME = "receipts-pending.jsonl"
READ_NAMESPACE = git_inbox.INBOX_READ_NAMESPACE

_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
LEGACY_DIR_PREFIX = "legacy-"


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def receipt_dir_name(message_id: str) -> str:
    """The message id itself, or `legacy-<16 hex>` for an id that is not one path segment."""
    if _SAFE_SEGMENT_RE.match(message_id):
        return message_id
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:16]
    return f"{LEGACY_DIR_PREFIX}{digest}"


def receipt_path(message_id: str, recipient: str, event: str) -> str:
    return f"{RECEIPTS_DIR}/{receipt_dir_name(message_id)}/{recipient}.{event}.json"


def build(  # noqa: PLR0913  # one keyword per record field
    *,
    message_id: str,
    recipient: str,
    event: str,
    at: str,
    message_ref: str = "",
    evidence: Mapping[str, Any] | None = None,
) -> rs.RecipientRecord:
    return rs.RecipientRecord(
        rung=RUNG,
        message_id=message_id,
        recipient=recipient,
        event=event,
        at=at,
        extra={"message_ref": message_ref, "evidence": dict(evidence or {})},
    )


def path_of(record: rs.RecipientRecord) -> str:
    return receipt_path(record.message_id, record.recipient, record.event)


def render(record: rs.RecipientRecord) -> str:
    return json.dumps(record.as_dict(), indent=2, sort_keys=True) + "\n"


def parse(text: str) -> rs.RecipientRecord:
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise rs.RecordError("not JSON") from exc
    return rs.RecipientRecord.from_dict(raw)


# ---------------------------------------------------------------------------------------------
# The pending set and the writer
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingReceipts:
    """`<state>/git-inbox/receipts-pending.jsonl`: receipts built but not yet on the remote."""

    path: Path

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (self.path.parent / f"{self.path.name}.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def load(self) -> list[rs.RecipientRecord]:
        """Every well-formed pending receipt. A torn or malformed line is skipped, never fatal."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        records: list[rs.RecipientRecord] = []
        for line in lines:
            with contextlib.suppress(ValueError):
                records.append(rs.RecipientRecord.from_dict(json.loads(line)))
        return records

    def save(self, records: Iterable[rs.RecipientRecord]) -> None:
        """Atomic rewrite; a crash leaves the previous file."""
        payload = "".join(
            json.dumps(r.as_dict(), sort_keys=True) + "\n" for r in records
        )
        handle, staged = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            os.fchmod(handle, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                notifier_pass.fsync(stream.fileno())
            os.replace(staged, self.path)
        except BaseException:
            Path(staged).unlink(missing_ok=True)
            raise


@dataclass(frozen=True)
class WriteOutcome:
    """What one write left on the remote. `committed` are the paths new on the tip."""

    committed: tuple[str, ...] = ()
    pending: int = 0
    reason: str = ""
    commit: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason


@dataclass
class ReceiptWriter:
    """Commit receipts to this machine's inbox ref, carrying the pending set across failures."""

    sender: git_inbox.InboxSender
    pending: PendingReceipts

    def commit(
        self, records: Iterable[rs.RecipientRecord], *, now: dt.datetime | None = None
    ) -> WriteOutcome:
        with self.pending.locked():
            merged: dict[str, rs.RecipientRecord] = {}
            for record in [*self.pending.load(), *records]:
                merged.setdefault(path_of(record), record)
            if not merged:
                return WriteOutcome()
            outcome = self.sender.commit_files(
                {path: render(record) for path, record in merged.items()},
                subject=f"git-inbox receipts: {len(merged)}",
                now=now,
            )
            if not outcome.sent:
                self.pending.save(merged.values())
                return WriteOutcome(pending=len(merged), reason=outcome.reason)
            self.pending.save(())
            written = tuple(p for p in outcome.path.split(",") if p)
            return WriteOutcome(committed=written, commit=outcome.commit)


def writer_for(  # noqa: PLR0913  # one keyword per sender seam
    *,
    repo: Path,
    machine: str,
    remote: str,
    state_dir: Path,
    cfg: Mapping[str, Any],
    runner: Any,
    log: Callable[[str], None] = lambda _line: None,
) -> ReceiptWriter:
    """The writer for this machine: `InboxSender` on its own ref, the pending file in `state_dir`."""
    sender = git_inbox.InboxSender(
        repo=repo,
        machine=machine,
        remote=remote,
        ref_prefix=str(
            cfg.get("inbox_ref_prefix") or git_inbox.DEFAULT_INBOX_REF_PREFIX
        ),
        retention_days=int(
            cfg.get("retention_days") or git_inbox.DEFAULT_RETENTION_DAYS
        ),
        runner=runner,
        log=log,
    )
    return ReceiptWriter(
        sender=sender, pending=PendingReceipts(state_dir / PENDING_NAME)
    )


# ---------------------------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------------------------


@dataclass
class ReceiptReader:
    """Every receipt on every fetched `refs/fleet-inbox/*` tip. Blobs are cached by oid."""

    git: Callable[[list[str]], GitResult]
    _cache: dict[str, rs.RecipientRecord | None] = field(default_factory=dict)
    reason: str = ""
    trees: Trees | None = None

    def _trees(self) -> Trees:
        if self.trees is None:
            self.trees = Trees(self.git)
        return self.trees

    def refs(self) -> list[str]:
        listed = self.git(["for-each-ref", "--format=%(refname)", READ_NAMESPACE])
        if listed.rc != 0:
            self.reason = f"for-each-ref-failed ({_refusal_reason(listed)})"
            return []
        return sorted(line.strip() for line in listed.out.splitlines() if line.strip())

    def _record(self, oid: str) -> rs.RecipientRecord | None:
        if oid not in self._cache:
            shown = self.git(["cat-file", "blob", oid])
            record = None
            if shown.rc == 0:
                with contextlib.suppress(rs.RecordError):
                    record = parse(shown.out)
            self._cache[oid] = record
        return self._cache[oid]

    def found(self, message_id: str | None = None) -> list[rs.Found]:
        """Receipts for `message_id`, or every receipt when None. Unparseable files are skipped."""
        self.reason = ""
        scope = (
            f"{RECEIPTS_DIR}/{receipt_dir_name(message_id)}/"
            if message_id is not None
            else f"{RECEIPTS_DIR}/"
        )
        result: list[rs.Found] = []
        trees = self._trees()
        for ref, oid in sorted(trees.tips().items()):
            if not ref.startswith(READ_NAMESPACE):
                continue
            for path, blob in trees.files(oid):
                if not path.startswith(scope):
                    continue
                record = self._record(blob)
                if record is None or path != path_of(record):
                    continue
                if message_id is not None and record.message_id != message_id:
                    continue
                result.append(
                    rs.Found(
                        writer_machine=ref.removeprefix(READ_NAMESPACE),
                        record=record,
                        location=f"{ref}:{path}",
                    )
                )
        self.reason = trees.reason
        return result
