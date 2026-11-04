"""Proof from the session's JSONL transcript that what was submitted landed (D-3),
and optional proof that the operation it named then began.

The transcript is Claude Code's internal format, so every landing records the
raw ``type``/``subtype`` it matched, and ``tests/prompt_injection/fixtures``
pins the record shapes of the version the library was built against.
"""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

from ai_chassis.prompt_injection.config import builtin_registry
from ai_chassis.prompt_injection.payload import sha256
from ai_chassis.prompt_injection.targets import SessionTarget

_POLL_SECONDS = 0.1
_GOAL_TAIL_BYTES = 8 * 1024 * 1024
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_QUEUE_BOUNDARY_SUBTYPES = frozenset({"stop_hook_summary", "turn_duration"})
# Mirrors context_supervisor._DEFERRED_RECORD_COMMANDS (ISSUE-REF): Claude Code
# writes these commands' `<command-name>` record only after they finish, so the
# plain record matching the payload is the submission echo, `command-echo`.
DEFERRED_RECORD_COMMANDS = frozenset({"/compact"})
#: ``Landing.shape`` of a busy-session ``/goal`` (ISSUE-REF): the native
#: ``Goal set: <args>`` enqueue echo, corroborated by its ``goal_status``.
NATIVE_ENQUEUE_ECHO = "native-enqueue-echo"
#: ``Landing.shape`` of a submission proven by its effect attachment alone
#: (``Expectation.landing_attachment``, ISSUE-REF).
EFFECT_ATTACHMENT = "effect-attachment"


@dataclass(frozen=True)
class Landing:
    kind: str
    offset: int
    landed_at: int | None
    record_type: str | None
    shape: str | None = None


@dataclass(frozen=True)
class Expectation:
    """What a landing is matched against; rebuildable from an attempt record, which
    is how ``verify(attempt_id)`` re-polls a QUEUED attempt later."""

    text: str
    kind: str
    command: str | None
    sentinel: str | None
    args_digest: str | None
    #: The use case's ``landing_attachment``: an attachment with all these fields
    #: lands the submission, for a command Claude Code may record only by its effect.
    landing_attachment: dict[str, Any] | None = None

    @classmethod
    def of(
        cls, payload: Any, landing_attachment: dict[str, Any] | None = None
    ) -> Expectation:
        return cls(
            payload.text,
            payload.kind,
            payload.command,
            payload.sentinel,
            payload.args_digest,
            landing_attachment,
        )

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Expectation:
        return cls(
            str(record.get("last_staged_text") or ""),
            str(record.get("payload_kind") or "slash-command"),
            record.get("command"),
            record.get("sentinel"),
            record.get("args_digest"),
            record.get("landing_attachment"),
        )


def message_text(record: dict[str, Any]) -> str | None:
    # Copied from context_supervisor._message_text (2913-2924).
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return None


def transcript_timestamp(value: object) -> int | None:
    # Copied from context_supervisor._transcript_timestamp (3440-3452).
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        return None
    return int(parsed.timestamp()) if parsed.tzinfo is not None else None


def _record_type(record: dict[str, Any]) -> str:
    subtype = record.get("subtype")
    return f"{record.get('type')}/{subtype}" if subtype else str(record.get("type"))


def classify_record(
    record: dict[str, Any], expect: Expectation, *, queued: bool
) -> tuple[str | None, bool]:
    """One post-Enter record's landing kind, and the carried queue state.

    Generalises context_supervisor._landing_record (2927-2955) from the
    ``/compact`` literal to the payload: a command record must also carry the
    payload's ``<command-args>`` digest when it has args, so a ``/goal`` a human
    typed is never taken for ours.
    """
    record_type = record.get("type")
    if (
        record_type == "queue-operation"
        and record.get("operation") == "enqueue"
        and (record.get("content") == expect.text or native_echo(record, expect))
    ):
        return "queued", True
    if record_type == "user":
        content = message_text(record)
        kind = (
            _classify_user_text(content, expect) if isinstance(content, str) else None
        )
        if kind is not None:
            return kind, queued
    if _local_command(record, expect) or effect_attachment(record, expect):
        return "command-record", queued
    if queued and record.get("subtype") in _QUEUE_BOUNDARY_SUBTYPES:
        return "queue-dropped", queued
    return None, queued


def _is_local_command(record: dict[str, Any]) -> bool:
    return record.get("type") == "system" and record.get("subtype") == "local_command"


def _local_command(record: dict[str, Any], expect: Expectation) -> bool:
    """A ``system``/``local_command`` record of ``expect``'s command and args: how Claude
    Code 2.1.296 records a command it runs locally, ``/goal clear`` on an idle session
    (ISSUE-REF transcript 6e58fe01, 2026-10-13T00:50:54Z), with no user command record."""
    if expect.kind != "slash-command" or not expect.command:
        return False
    if not _is_local_command(record):
        return False
    content = record.get("content")
    return (
        isinstance(content, str)
        and f"<command-name>{expect.command}</command-name>" in content
        and _args_match(content, expect)
    )


def effect_attachment(record: dict[str, Any], expect: Expectation) -> bool:
    """An ``attachment`` record carrying every field of ``expect.landing_attachment``:
    Claude Code 2.1.296 may record an idle ``/goal clear`` only by its clearing
    ``goal_status`` (``met`` and ``sentinel`` true), with no command record at all
    (ISSUE-REF transcript 6e58fe01, 2026-10-14T11:46:22Z)."""
    wanted = expect.landing_attachment
    attachment = record.get("attachment")
    return (
        bool(wanted)
        and record.get("type") == "attachment"
        and isinstance(attachment, dict)
        and all(attachment.get(key) == value for key, value in wanted.items())
    )


def _classify_user_text(content: str, expect: Expectation) -> str | None:
    if expect.kind == "slash-command" and expect.command:
        if f"<command-name>{expect.command}</command-name>" in content:
            return "command-record" if _args_match(content, expect) else None
        if content.strip() != expect.text.strip():
            return None
        return "command-echo" if expect.command in DEFERRED_RECORD_COMMANDS else "text"
    if expect.sentinel is not None:
        return "prompt-record" if expect.sentinel in content else None
    return "prompt-record" if content.strip() == expect.text.strip() else None


@cache
def _echo_prefix(command: str) -> str | None:
    """The registry's ``enqueue_echo_prefix`` for a command it marks
    ``native_midturn``: one Claude Code handles at once even mid-turn."""
    entry = builtin_registry().get(command)
    if not isinstance(entry, dict) or entry.get("native_midturn") is not True:
        return None
    prefix = entry.get("enqueue_echo_prefix")
    return prefix if isinstance(prefix, str) and prefix else None


def native_echo(record: dict[str, Any], expect: Expectation) -> bool:
    """A busy session's enqueue of our native command: ``<prefix><args>`` with the
    payload's args digest, so a human's own ``/goal`` is never taken for ours."""
    if expect.kind != "slash-command" or not expect.command or not expect.args_digest:
        return False
    prefix = _echo_prefix(expect.command)
    content = record.get("content")
    return (
        prefix is not None
        and record.get("type") == "queue-operation"
        and record.get("operation") == "enqueue"
        and isinstance(content, str)
        and content.startswith(prefix)
        and sha256(content[len(prefix) :].strip()) == expect.args_digest
    )


def _native_effect(record: dict[str, Any], expect: Expectation) -> bool:
    """The ``goal_status`` that proves the echoed command took effect. A
    ``condition`` it carries must be the payload's args."""
    attachment = record.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "goal_status":
        return False
    condition = attachment.get("condition")
    if condition is None:
        return True
    return (
        isinstance(condition, str) and sha256(condition.strip()) == expect.args_digest
    )


def _args_match(content: str, expect: Expectation) -> bool:
    if expect.args_digest is None:
        return True
    match = _COMMAND_ARGS.search(content)
    return bool(match) and sha256(match.group(1).strip()) == expect.args_digest


class DeliveryVerifier(ABC):
    @abstractmethod
    def landing(
        self,
        target: SessionTarget,
        offset: int,
        timeout: float,
        expect: Expectation,
        *,
        queued: bool = False,
    ) -> Landing: ...


class TranscriptVerifier(DeliveryVerifier):
    """Copied from context_supervisor._poll_landing (2958-2998): reads only records
    appended after ``offset``, stops at the first classifiable one or the deadline,
    never spins, and treats a torn or non-object record as ``none``.

    A native command's enqueue echo (:func:`native_echo`) is held, not returned:
    it lands as ``command-record`` (shape ``native-enqueue-echo``, offset just past
    the echo, so the execution oracle still reads the effect) once its
    ``goal_status`` follows, and otherwise stays ``queued`` at the echo's own offset
    so :func:`~ai_chassis.prompt_injection.injector.verify` re-reads it. A turn
    boundary does not drop it: the command never waited for one.

    A transcript that cannot be read yet is "not yet", polled to the deadline: a fresh
    session creates its file only after the first Enter (ISSUE-REF,
    measured live on both Phase 8 probe goals)."""

    def landing(self, target, offset, timeout, expect, *, queued=False):
        path = target.transcript_path
        if path is None:
            return Landing("none", offset, None, None)
        deadline = time.monotonic() + max(0.0, timeout)
        echo: _Echo | None = None
        queue = _BareQueue()
        while True:
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    data = handle.read()
            except OSError:
                data = b""
            cursor = offset
            for raw_line in data.splitlines(keepends=True):
                if not raw_line.endswith((b"\n", b"\r")):
                    break
                record = _parse(raw_line)
                if record is None:
                    return Landing("none", offset, None, None)
                start, cursor = cursor, cursor + len(raw_line)
                kind, queued = classify_record(record, expect, queued=queued)
                kind, queued = queue.step(record, kind, queued)
                landing, echo, held = _hold_echo(
                    record, kind, expect, echo, (start, cursor)
                )
                if landing is None and not held and kind is not None:
                    landed_at = transcript_timestamp(record.get("timestamp")) or int(
                        time.time()
                    )
                    shape = (
                        EFFECT_ATTACHMENT if effect_attachment(record, expect) else None
                    )
                    landing = Landing(
                        kind, cursor, landed_at, _record_type(record), shape
                    )
                if landing is not None:
                    return landing
            offset = cursor
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return (
                    Landing("queued", echo.start, None, None)
                    if echo is not None
                    else Landing("queued" if queued else "none", offset, None, None)
                )
            time.sleep(min(_POLL_SECONDS, remaining))


@dataclass(frozen=True)
class _Echo:
    start: int
    end: int
    at: int | None


def _queue_operation(record: dict[str, Any], operation: str) -> bool:
    return (
        record.get("type") == "queue-operation" and record.get("operation") == operation
    )


@dataclass
class _BareQueue:
    """Claude Code 2.1.296 records a plain prompt submitted mid-turn as an enqueue
    with no ``content``, and writes the prompt's user record only after dequeuing
    it, past the in-flight turn's closing boundary (ISSUE-REF, run
    inbox-nudge-20261021T105132Z-43f690: dequeued 32 s after its Enter).

    The first enqueue after our Enter, when it has no content, is read as ours and
    ``queued``: it carries no text, so only the later user record proves the landing.
    Once a ``dequeue`` follows a queued attempt, a turn boundary no longer drops it."""

    enqueued: bool = False
    dequeued: bool = False

    def step(
        self, record: dict[str, Any], kind: str | None, queued: bool
    ) -> tuple[str | None, bool]:
        if _queue_operation(record, "enqueue"):
            if (
                kind is None
                and not queued
                and not self.enqueued
                and "content" not in record
            ):
                kind, queued = "queued", True
            self.enqueued = True
        if queued and _queue_operation(record, "dequeue"):
            self.dequeued = True
        if kind == "queue-dropped" and self.dequeued:
            return None, queued
        return kind, queued


def _parse(raw_line: bytes) -> dict[str, Any] | None:
    """One complete transcript line as an object; ``None`` when torn or not one."""
    try:
        record = json.loads(raw_line.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _hold_echo(
    record: dict[str, Any],
    kind: str | None,
    expect: Expectation,
    echo: _Echo | None,
    span: tuple[int, int],
) -> tuple[Landing | None, _Echo | None, bool]:
    """The native-echo step of :class:`TranscriptVerifier`: the landing it proves,
    the echo held so far, and whether this record was consumed by it."""
    if echo is None:
        if kind == "queued" and native_echo(record, expect):
            at = transcript_timestamp(record.get("timestamp"))
            return None, _Echo(span[0], span[1], at), True
        return None, None, False
    if _native_effect(record, expect):
        landed_at = echo.at or int(time.time())
        landing = Landing(
            "command-record",
            echo.end,
            landed_at,
            "queue-operation",
            NATIVE_ENQUEUE_ECHO,
        )
        return landing, echo, True
    return None, echo, kind in {"queued", "queue-dropped"}


#: The four post-submit verdicts (design § Post-submit validation).
POST_SUBMIT_VERIFIED = "post-submit-verified"
POST_SUBMIT_PENDING = "post-submit-pending"
POST_SUBMIT_MISMATCH = "post-submit-mismatch"
POST_SUBMIT_MISSING = "post-submit-missing"
#: How many squeezed leading characters of a plain user record must equal the intent's
#: for it to be read as a garbled copy of the submission.
_PROMPT_PREFIX_CHARS = 40
_LOCAL_STDOUT = re.compile(
    r"<local-command-stdout>(.*?)</local-command-stdout>", re.DOTALL
)


@dataclass(frozen=True)
class PostSubmitVerdict:
    """What the transcript holds against what was intended, after Enter."""

    verdict: str
    landing: str
    recorded_text: str | None = None
    record_offset: int | None = None
    record_type: str | None = None
    divergence: dict[str, Any] | None = None
    records_by_type: dict[str, int] | None = None

    @property
    def failed(self) -> bool:
        return self.verdict in {POST_SUBMIT_MISMATCH, POST_SUBMIT_MISSING}

    def to_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "landing": self.landing,
            "recorded_text": self.recorded_text,
            "record_offset": self.record_offset,
            "record_type": self.record_type,
            "divergence": self.divergence,
            "records_by_type": self.records_by_type or {},
        }


def _squeeze(text: str) -> str:
    return "".join(text.split())


def _row_at(text: str, offset: int) -> tuple[int, str]:
    """The line of ``text`` holding character ``offset`` (the last line past the end)."""
    lines = text.split("\n")
    start = 0
    for index, line in enumerate(lines):
        if offset <= start + len(line):
            return index, line
        start += len(line) + 1
    return len(lines) - 1, lines[-1]


def divergence(recorded: str, intended: str) -> dict[str, Any]:
    """The first character offset where ``recorded`` leaves ``intended``, and the row of
    each that holds it."""
    offset = next(
        (i for i, (a, b) in enumerate(zip(recorded, intended, strict=False)) if a != b),
        min(len(recorded), len(intended)),
    )
    row, intended_row = _row_at(intended, offset)
    _, recorded_row = _row_at(recorded, offset)
    return {
        "offset": offset,
        "row": row,
        "intended_row": intended_row,
        "recorded_row": recorded_row,
        "intended_chars": len(intended),
        "recorded_chars": len(recorded),
    }


def _intended(expect: Expectation) -> str:
    """The text the transcript should record: a command's args, else the prompt, both
    right-stripped as Claude Code trims what it submits."""
    if expect.kind == "slash-command" and expect.command:
        return expect.text.strip()[len(expect.command) :].strip()
    return expect.text.rstrip()


def _recorded_user_command(
    content: str, command: str, expect: Expectation
) -> tuple[str, bool] | None:
    """The command record itself, or its ``<local-command-stdout>`` echo."""
    if f"<command-name>{command}</command-name>" in content:
        match = _COMMAND_ARGS.search(content)
        args = match.group(1).strip() if match else ""
        return args, _args_match(content, expect)
    prefix = _echo_prefix(command)
    stdout = _LOCAL_STDOUT.search(content)
    if prefix and stdout and stdout.group(1).startswith(prefix):
        echoed = stdout.group(1)[len(prefix) :].strip()
        return echoed, sha256(echoed) == expect.args_digest
    return None


def _recorded_command(
    record: dict[str, Any], expect: Expectation
) -> tuple[str, bool] | None:
    """``(recorded text, matches)`` when ``record`` is a submission of ``expect``'s
    command in any shape Claude Code writes one; None when it is not."""
    command = expect.command
    if expect.kind != "slash-command" or not command:
        return None
    if record.get("type") == "user" or _is_local_command(record):
        content = (
            message_text(record)
            if record.get("type") == "user"
            else record.get("content")
        )
        if not isinstance(content, str):
            return None
        return _recorded_user_command(content, command, expect)
    prefix = _echo_prefix(command)
    content = record.get("content")
    if not (
        prefix
        and record.get("type") == "queue-operation"
        and record.get("operation") == "enqueue"
        and isinstance(content, str)
        and content.startswith(prefix)
    ):
        return None
    echoed = content[len(prefix) :].strip()
    return echoed, sha256(echoed) == expect.args_digest


def _recorded_prompt(
    record: dict[str, Any], expect: Expectation, intended: str
) -> tuple[str, bool] | None:
    if record.get("type") != "user":
        return None
    content = message_text(record)
    if not isinstance(content, str) or content.lstrip().startswith("<"):
        return None
    head = _squeeze(intended)[:_PROMPT_PREFIX_CHARS]
    if not head or not _squeeze(content).startswith(head):
        return None
    matched = (
        expect.sentinel in content
        if expect.sentinel is not None
        else content.rstrip() == intended
    )
    return content.rstrip(), matched


def _complete_records(path: Path, offset: int) -> list[tuple[int, dict[str, Any]]]:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return []
    found: list[tuple[int, dict[str, Any]]] = []
    cursor = offset
    for raw_line in data.splitlines(keepends=True):
        if not raw_line.endswith((b"\n", b"\r")):
            break
        start, cursor = cursor, cursor + len(raw_line)
        record = _parse(raw_line)
        if record is not None:
            found.append((start, record))
    return found


class PostSubmitValidator:
    """The D-19 post-submit check: after the landing poll, every record the transcript
    gained since ``offset`` (the size before Enter) is read once more and the submission
    is judged against the intended text. The comparison is exact after Claude Code's
    trailing-whitespace trim, so a newline that became a space, a dropped row or a cut
    tail is a mismatch while the walk's trailing filler rows are not."""

    def verdict(
        self,
        target: SessionTarget,
        landing: Landing,
        expect: Expectation,
        offset: int,
    ) -> PostSubmitVerdict:
        if landing.kind == "queued":
            return PostSubmitVerdict(
                POST_SUBMIT_PENDING, landing.kind, record_offset=landing.offset
            )
        if landing.kind == "process-exit":
            return PostSubmitVerdict(POST_SUBMIT_VERIFIED, landing.kind)
        intended = _intended(expect)
        path = target.transcript_path
        records = _complete_records(path, offset) if path is not None else []
        by_type: dict[str, int] = {}
        first: tuple[int, dict[str, Any], str, bool] | None = None
        for start, record in records:
            name = _record_type(record)
            by_type[name] = by_type.get(name, 0) + 1
            seen = _recorded_command(record, expect) or _recorded_prompt(
                record, expect, intended
            )
            if seen is None:
                continue
            text, matched = seen
            if matched and landing.kind in SUBMITTED_KINDS:
                return PostSubmitVerdict(
                    POST_SUBMIT_VERIFIED,
                    landing.kind,
                    recorded_text=text,
                    record_offset=start,
                    record_type=name,
                    records_by_type=by_type,
                )
            if first is None and not matched:
                first = (start, record, text, matched)
        if landing.kind in SUBMITTED_KINDS:
            return PostSubmitVerdict(
                POST_SUBMIT_VERIFIED, landing.kind, records_by_type=by_type
            )
        return _unmatched(landing, intended, first, by_type)


def _unmatched(
    landing: Landing,
    intended: str,
    first: tuple[int, dict[str, Any], str, bool] | None,
    by_type: dict[str, int],
) -> PostSubmitVerdict:
    """No record proved the submission: a garbled copy, a plain-text delivery, or none."""
    if first is not None:
        start, record, text, _ = first
        return PostSubmitVerdict(
            POST_SUBMIT_MISMATCH,
            landing.kind,
            recorded_text=text,
            record_offset=start,
            record_type=_record_type(record),
            divergence=divergence(text, intended),
            records_by_type=by_type,
        )
    if landing.kind == "text":
        return PostSubmitVerdict(
            POST_SUBMIT_MISMATCH,
            landing.kind,
            recorded_text=intended,
            divergence={"kind": "delivered-as-text", "offset": None},
            records_by_type=by_type,
        )
    return PostSubmitVerdict(POST_SUBMIT_MISSING, landing.kind, records_by_type=by_type)


#: Landing kinds that already prove the submission (outcomes.SUBMITTED_LANDINGS less
#: ``process-exit``, which has no transcript record to read).
SUBMITTED_KINDS = frozenset({"command-record", "command-echo", "prompt-record"})


def transcript_size(target: SessionTarget) -> int:
    try:
        return target.transcript_path.stat().st_size if target.transcript_path else 0
    except OSError:
        return 0


def _goal_statuses(path: Path, offset: int | None) -> list[bool | None]:
    """``met`` of every ``goal_status`` attachment, oldest first.

    The attachment and its boolean ``met`` are observed, not a supported schema
    (context_supervisor._scan_transcript_for_goal_state 2701-2728)."""
    try:
        with path.open("rb") as handle:
            if offset is None:
                size = handle.seek(0, 2)
                handle.seek(max(0, size - _GOAL_TAIL_BYTES))
            else:
                handle.seek(offset)
            data = handle.read()
    except OSError:
        return []
    found: list[bool | None] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        attachment = record.get("attachment") if isinstance(record, dict) else None
        if isinstance(attachment, dict) and attachment.get("type") == "goal_status":
            met = attachment.get("met")
            found.append(met if isinstance(met, bool) else None)
    return found


def goal_state(target: SessionTarget) -> str:
    """``active`` when the newest ``goal_status`` has ``met`` false, ``inactive``
    when true, ``unknown`` when there is none (or it is malformed)."""
    if target.transcript_path is None:
        return "unknown"
    statuses = _goal_statuses(target.transcript_path, None)
    if not statuses or statuses[-1] is None:
        return "unknown"
    return "inactive" if statuses[-1] else "active"


@dataclass(frozen=True)
class OracleVerdict:
    state: str | None
    detail: str = ""


class ExecutionOracle(ABC):
    """Proof the operation began, beyond the composer accepting it. A verdict moves
    the attempt past SENT; its absence within the budget leaves SENT."""

    @abstractmethod
    def observe(
        self, target: SessionTarget, attempt: dict[str, Any], timeout: float
    ) -> OracleVerdict: ...


class GoalActiveOracle(ExecutionOracle):
    """A ``goal_status`` attachment after the landing: the native goal evaluated."""

    def observe(self, target, attempt, timeout):
        if target.transcript_path is None:
            return OracleVerdict(None, "no transcript")
        offset = int(attempt.get("landing_offset") or 0)
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            statuses = _goal_statuses(target.transcript_path, offset)
            if statuses:
                met = statuses[-1]
                return OracleVerdict("COMPLETED", f"goal_status met={str(met).lower()}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return OracleVerdict(None, "no goal_status after landing")
            time.sleep(min(_POLL_SECONDS, remaining))


class GoalClearedOracle(ExecutionOracle):
    """The goal is no longer live: the transcript's newest ``goal_status`` is met.

    Read from the tail, not from the landing: Claude Code writes the clearing
    ``goal_status`` (``met: true``) BEFORE the ``local_command`` record the landing
    matched (ISSUE-REF transcript 6e58fe01, 2026-10-13T00:50:54Z)."""

    def observe(self, target, _attempt, timeout):
        if target.transcript_path is None:
            return OracleVerdict(None, "no transcript")
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            statuses = _goal_statuses(target.transcript_path, None)
            if statuses and statuses[-1] is True:
                return OracleVerdict("COMPLETED", "goal_status met=true (cleared)")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return OracleVerdict(None, "no clearing goal_status")
            time.sleep(min(_POLL_SECONDS, remaining))


class PreCompactOracle(ExecutionOracle):
    """The supervisor's ``PreCompact`` hook advances the attempt record itself
    (``SENT`` -> ``STARTED``, logged ``CC-START``, context_supervisor 4300-4321), so
    the oracle reads that record rather than a second marker."""

    def observe(self, _target, attempt, timeout):
        from ai_chassis.prompt_injection.observe import AttemptStore

        store = AttemptStore()
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            current = store.find(str(attempt["attempt_id"])) or {}
            if current.get("state") in {"STARTED", "COMPLETED"}:
                return OracleVerdict(str(current["state"]), "PreCompact correlated")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return OracleVerdict(None, "no PreCompact correlation")
            time.sleep(min(_POLL_SECONDS, remaining))


#: How long :class:`LocalCommandOracle` waits for the result record at most. Claude Code
#: wrote it 0.8 s after the command record (fixture ``reload-plugins-2.1.310.jsonl``), so
#: the library's turn-sized ``completion_timeout_seconds`` would only stall the caller.
LOCAL_COMMAND_WAIT_SECONDS = 10.0


def reload_result(stdout: str) -> str:
    """A ``/reload-plugins`` stdout's result, by the strings Claude Code 2.1.310 prints
    (design ISSUE-REF fact 4): ``reloaded``, ``held-cache`` (it asks for ``--force``),
    ``queued-by-cc``, ``remote-unavailable``, or ``other``."""
    text = stdout.strip()
    if text.startswith("Reloaded:"):
        return "reloaded"
    if "--force" in text:
        return "held-cache"
    if "is queued" in text:
        return "queued-by-cc"
    if "remote connection" in text:
        return "remote-unavailable"
    return "other"


def local_command_stdout(record: dict[str, Any]) -> str:
    """The text inside a ``local_command`` record's ``<local-command-stdout>``, else its
    whole content."""
    content = record.get("content")
    if not isinstance(content, str):
        return ""
    match = _LOCAL_STDOUT.search(content)
    return match.group(1) if match else content


class LocalCommandOracle(ExecutionOracle):
    """The ``system``/``local_command`` record Claude Code writes after running a local
    command such as ``/reload-plugins``: the first one after the landing whose
    ``commandRun.command`` is the attempt's command. Its stdout's first line and
    :func:`reload_result` go into the verdict detail as ``<result>: <first line>``."""

    def observe(self, target, attempt, timeout):
        if target.transcript_path is None:
            return OracleVerdict(None, "no transcript")
        name = str(attempt.get("command") or "").lstrip("/")
        offset = int(attempt.get("landing_offset") or 0)
        deadline = time.monotonic() + max(0.0, min(timeout, LOCAL_COMMAND_WAIT_SECONDS))
        while True:
            for _, record in _complete_records(target.transcript_path, offset):
                run = record.get("commandRun")
                if (
                    _is_local_command(record)
                    and isinstance(run, dict)
                    and run.get("command") == name
                ):
                    stdout = local_command_stdout(record)
                    first = next(iter(stdout.strip().splitlines()), "")
                    return OracleVerdict(
                        "COMPLETED", f"{reload_result(stdout)}: {first[:400]}"
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return OracleVerdict(None, f"no local_command record for /{name}")
            time.sleep(min(_POLL_SECONDS, remaining))
