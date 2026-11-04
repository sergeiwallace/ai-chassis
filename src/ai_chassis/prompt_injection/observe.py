"""Observability sinks reproducing the auto-compact injector's observables file for
file (D-6): stage lines, attempt records, one-shot notices, debug captures and
the readiness trail, under ``${AIH_RESUME_ROOT:-~/.claude/resume}``.

Every sink is best-effort by contract, like ``log_stage``: a failure to record
an attempt must never become a second failure of the attempt.
"""

from __future__ import annotations

import glob
import json
import os
import secrets
import time
import traceback
from abc import ABC
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_chassis.prompt_injection.config import load_config, resume_root, safe_component
from ai_chassis.prompt_injection.outcomes import TERMINAL_STATES, TRANSPORT_REASONS
from ai_chassis.session_key import refusal

# Library stage -> legacy compact-path name, byte for byte (T-2.2). The legacy
# names are the ones _bound_compact_transport, _submit_verified_compact,
# arm_compact and the lifecycle emit (context_supervisor.py at bff30e522).
COMPACT_STAGE_ALIASES = {
    "ARM": "CC-ARM",
    "NOOP": "CC-ARM-NOOP",
    "BIND": "CC-BIND",
    "IDLE-CONFIRMED": "CC-IDLE-CONFIRMED",
    "NOT-READY": "CC-WAIT",
    "QUEUED": "CC-WAIT",
    "BOUND": "CC-WAIT",
    "ESCALATE": "CC-ESCALATE",
    "SENT": "CC-SEND",
    "CORRUPTED": "CC-CORRUPTED",
    "REFUSED": "CC-REFUSED",
    "TRANSPORT-UNAVAILABLE": "CC-TRANSPORT-UNAVAILABLE",
    "AMBIGUOUS": "CC-AMBIGUOUS",
    "STARTED": "CC-START",
    "LIB-FALLBACK": "CC-COMPACT-LIB-FALLBACK",
    # The D-19 post-submit verdict (ISSUE-REF): new, so it carries the use case.
    "POST-SUBMIT": "CC-COMPACT-POST-SUBMIT",
}

_DEBUG_DIAGNOSTIC_MAX_BYTES = 16 * 1024
_READINESS_TRAIL_MAX_BYTES = 1024 * 1024
_READINESS_TRAIL_RETENTION_SECONDS = 7 * 24 * 60 * 60
_READINESS_HOUSEKEEPING_INTERVAL_SECONDS = 24 * 60 * 60
_readiness_housekeeping_last_run: float | None = None


def stage_name(prefix: str, stage: str, aliases: dict[str, str] | None = None) -> str:
    """``CC-<PREFIX>-<STAGE>``, or the alias when the use case keeps legacy names."""
    if aliases and stage in aliases:
        return aliases[stage]
    return f"CC-{prefix}-{stage}"


def write_stage_line(stage: str, key: str, detail: str = "") -> None:
    """Copied from context_supervisor.log_stage (3982-4000): the aggregate log and
    the per-session log, same line format. Like the legacy writer it never trims
    ``resume.log`` (bash ``resume_log`` does; a filed inconsistency, reproduced).
    A key the shared session-key rule refuses is logged as its per-session log name,
    so a newline in it cannot forge a line ``prompt_injection fallbacks`` counts."""
    logged = key if refusal(key) is None else safe_component(key)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {stage:<10} key={logged} {detail}\n"
    root = resume_root()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (root / "resume.log").open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass
    try:
        directory = root / "logs"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (directory / f"{safe_component(key)}.log").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(line)
    except OSError:
        pass


def _fsync_directory(directory: Path) -> None:
    # Copied from context_supervisor._fsync_directory (2393-2422).
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _no_separator(value: str) -> str:
    """A key or attempt id as one path component. Only the separators change, so every
    key ``resume_derive_key`` produces (already ``[A-Za-z0-9._-]``) names the same file
    the supervisor's ``ResumeStore`` does, and a ``key:`` spec carrying ``../`` (the
    agent-mail ``inject_prompt`` tool forwards a client's target) cannot leave the ledger."""
    return value.replace("/", "_").replace("\\", "_")


def _literal(value: str) -> str:
    """``_no_separator(value)`` as a glob pattern that matches only itself, so a ``*``
    or ``[`` key never lists another session's records (ISSUE-REF)."""
    return glob.escape(_no_separator(value))


class AttemptStore:
    """``attempts/<key>-<attempt_id>.json``; ResumeStore.write_attempt/read_attempts
    (1950-1974) with the same atomic write and newest-first ordering."""

    def __init__(self, root: Path | None = None, *, ledger: str = "attempts") -> None:
        """``ledger`` names the directory under the root; a use case whose records
        must stay out of the supervisor's ``attempts/`` budget names its own."""
        self.root = root or resume_root()
        self.ledger = ledger

    @property
    def directory(self) -> Path:
        return self.root / self.ledger

    def path(self, key: str, attempt_id: str) -> Path:
        return self.directory / f"{_no_separator(key)}-{_no_separator(attempt_id)}.json"

    def write(self, record: dict[str, Any]) -> Path:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.path(str(record["key"]), str(record["attempt_id"]))
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        return path

    def _load(self, paths: Iterable[Path]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(value, dict):
                value["_path"] = str(path)
                records.append(value)
        return sorted(records, key=lambda r: str(r.get("armed_at", "")), reverse=True)

    def read(self, key: str) -> list[dict[str, Any]]:
        return [
            r
            for r in self._load(self.directory.glob(f"{_literal(key)}-*.json"))
            if r.get("key") == key
        ]

    def all(self) -> list[dict[str, Any]]:
        return self._load(self.directory.glob("*.json"))

    def find(self, attempt_id: str) -> dict[str, Any] | None:
        matches = [
            r
            for r in self._load(self.directory.glob(f"*-{_literal(attempt_id)}.json"))
            if r.get("attempt_id") == attempt_id
        ]
        return matches[0] if matches else None


class Observer(ABC):  # noqa: B024 - every hook is optional, a sink overrides what it records
    """One observability sink. Each hook is a no-op here so a custom sink (a
    goal-doc JSONL log, say) overrides only what it records."""

    def stage(self, _name: str, _key: str, _detail: str) -> None:
        """A lifecycle transition, already named (``CC-GOAL-SENT``, ``CC-SEND``)."""
        return None

    def attempt(self, _record: dict[str, Any]) -> Path | None:
        """The attempt record changed; the record is its whole current state."""
        return None

    def notice(self, _record: dict[str, Any], _reason: str) -> Path | None:
        return None

    def clear_notice(self, _record: dict[str, Any]) -> None:
        """The attempt landed; withdraw its undelivered retry notice."""
        return None

    def debug_capture(
        self, _record: dict[str, Any], _reason: str, _capture: str
    ) -> Path | None:
        return None

    def evidence(
        self, _record: dict[str, Any], _reason: str, _capture: str
    ) -> Path | None:
        """The composer frame a refusal was decided on, written whatever the debug
        gate says, so the refusal is diagnosable from its record alone."""
        return None

    def readiness(
        self,
        _record: dict[str, Any],
        _samples: Sequence[Any],
        _phases: Sequence[str],
        _outcome: dict[str, Any],
    ) -> None:
        """One readiness evaluation: its composer samples and the decision taken."""
        return None

    def exception(self, _event: str, _error: BaseException) -> Path | None:
        return None

    def run_event(
        self, _record: dict[str, Any], _event: str, _fields: dict[str, Any]
    ) -> None:
        """One event of the run record (design § Observability: the run record)."""
        return None

    def run_capture(
        self, _record: dict[str, Any], _label: str, _text: str
    ) -> Path | None:
        """One raw pane capture kept in the run record."""
        return None


class StageLogObserver(Observer):
    def stage(self, name, key, detail):
        write_stage_line(name, key, detail)


class AttemptStoreObserver(Observer):
    def __init__(self, store: AttemptStore | None = None) -> None:
        self.store = store

    def attempt(self, record):
        return (self.store or AttemptStore()).write(
            {k: v for k, v in record.items() if not k.startswith("_")}
        )


# Copied from context_supervisor._TRANSPORT_NOTICE_TEXT (986-1017).
NOTICE_TEXT = {
    "no-tmux": (
        "no tmux server answered, so this session has no pane to send into\n"
        "(the supervisor asks the running server, not the tmux binary)"
    ),
    "no-pane": (
        "a tmux server is running but this session has no pane of its own, so there\n"
        "is nothing to send into (it was launched outside tmux, and a server\n"
        "appearing later cannot give a running process a pane)"
    ),
    "tmux-version": (
        "this host's tmux is older than the minimum this transport needs, so the "
        "composer fence refuses to drive it -- upgrade tmux"
    ),
    "stage-undo-failed": (
        "it typed text into the composer, then refused before Enter and could not\n"
        "prove it cleared that text: the composer may still hold it. Clear it by hand\n"
        "(Backspace, or Ctrl+U per row); never Ctrl+C, which interrupts the session\n"
        "and stops its background agents"
    ),
    "compact-delivered-as-text": (
        "Claude Code delivered the staged `/compact` as plain user text instead of "
        "executing the built-in command"
    ),
}
_RETRY_MARKER = "AUTO-COMPACT RETRYING"


def notice_body(record: dict[str, Any], reason: str) -> str:
    """The one-shot notice text. The compact use case keeps the legacy wording of
    ``write_transport_notice`` (1153-1221) less its two host-derived paragraphs
    (native auto-compact and relaunch advice); other use cases name themselves."""
    detail = NOTICE_TEXT.get(reason, reason)
    if reason == "stage-undo-failed" and record.get("stage_undo_repair_command"):
        detail = (
            "the injected text may remain in the composer. From a separate shell run:\n"
            f"{record['stage_undo_repair_command']}\n"
            "The repair refuses foreign text or a hidden composer; never Ctrl+C"
        )
        if record.get("stage_undo_escalated_at"):
            detail += (
                f"\nDeferred undo exhausted after {record.get('stage_undo_retries')} "
                f"retries: {record.get('stage_undo_reason')}. Expose the unchanged "
                "draft tail, then run that command.\n"
                f"Remaining injected text: {record.get('stage_undo_remaining_text')!r}"
            )
    use_case = str(record.get("use_case", ""))
    what = str(record.get("command") or "the injected prompt")
    if reason == "compact-delivered-as-text":
        body = (
            f"{_RETRY_MARKER} (reason={reason}).\n\n{detail}. The supervisor "
            "will retry after its backoff and fresh idle/composer checks.\n"
        )
    elif use_case == "compact":
        body = (
            "THE SUPERVISOR WILL NOT SEND `/compact` IN THIS SESSION "
            f"(reason={reason}).\n\n"
            "The context supervisor arms `/compact` by typing it into this session's own\n"
            f"tmux pane, and {detail}. Nothing is retrying; no `/compact` will arrive from\n"
            "the supervisor.\n\n"
            "Do this: run `/compact` yourself when ready (invoke `/save-state` first if it\n"
            "has not run).\n"
        )
    else:
        body = (
            f"PROMPT INJECTION ({use_case}) WILL NOT SEND {what} IN THIS SESSION "
            f"(reason={reason}).\n\n"
            "The prompt-injection library types into this session's own tmux pane,\n"
            f"and {detail}. Nothing is retrying.\n"
        )
    attempt_id = record.get("attempt_id")
    if attempt_id:
        body += f"\nattempt_id={attempt_id}\n"
    return body


def _publish_notice(path: Path, body: str) -> None:
    """Stage ``body`` through a temp file of its own, then rename it onto ``path``.

    A shared temp name let two concurrent writers for one key splice their bodies
    into one published notice (ISSUE-REF). A failed write removes its temp file.
    """
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


class NoticeObserver(Observer):
    """``notices/<key>.md``, read and rotated by the ``UserPromptSubmit`` hook."""

    def notice(self, record, reason):
        if reason not in TRANSPORT_REASONS and reason not in {
            "compact-delivered-as-text",
            "stage-undo-failed",
        }:
            return None
        directory = resume_root() / "notices"
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = directory / f"{safe_component(str(record['key']))}.md"
            _publish_notice(path, notice_body(record, reason))
        except OSError:
            return None
        return path

    def clear_notice(self, record):
        """Withdraw the key's undelivered retry notice once a landing is proven.

        context_supervisor.clear_retry_notice (1438-1450) is attempt-scoped because
        the legacy retry re-runs the SAME attempt. Each library run is a new
        attempt, so the retry that lands never carries the id that wrote the
        notice; scoping by attempt here would deliver the stale "retrying" notice
        after the compact already happened (the audit's F-2). Only a retry notice
        is withdrawn; a transport notice stays until the hook delivers it.
        """
        path = resume_root() / "notices" / f"{safe_component(str(record['key']))}.md"
        try:
            if _RETRY_MARKER in path.read_text(encoding="utf-8"):
                path.unlink()
        except OSError:
            pass


class DebugCaptureObserver(Observer):
    """``debug/<key>-<ts>-<reason>.txt`` and hook-exception diagnostics, written only
    while ``debug_diagnostics_enabled`` is true (TmuxAdapter.write_debug_capture
    5776-5812, _write_hook_exception_diagnostic 4083-4113)."""

    def debug_capture(self, record, reason, capture):
        if not load_config().get("debug_diagnostics_enabled", False):
            return None
        path = (
            resume_root()
            / "debug"
            / (
                f"{safe_component(str(record['key']))}-{int(time.time())}-"
                f"{safe_component(reason)}.txt"
            )
        )
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(
                f"reason={reason}\nattempt_id={record.get('attempt_id')}\n"
                f"pane_id={record.get('pane_id')}\n\n{capture}",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
        except OSError:
            return None
        return path

    def exception(self, event, error):
        try:
            if not load_config().get("debug_diagnostics_enabled", False):
                return None
            header = (
                f"event={event}\nexception_type={type(error).__name__}\n"
                f"exception_message={str(error)[:512]}\n\ntraceback:\n"
            ).encode("utf-8", errors="replace")
            body = "".join(traceback.format_exception(error)).encode(
                "utf-8", errors="replace"
            )
            path = (
                resume_root()
                / "debug"
                / f"hook-exception-{time.time_ns()}-{safe_component(event)}.txt"
            )
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(
                header + body[-(_DEBUG_DIAGNOSTIC_MAX_BYTES - len(header)) :]
            )
            os.chmod(path, 0o600)
            return path
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            return None


class EvidenceObserver(Observer):
    """``evidence/<key>-<attempt_id>-<reason>.txt``: the raw (``-e``) composer frame
    a stage-verify refusal compared against. Not gated by ``debug_diagnostics_enabled``:
    the live refusal at ISSUE-REF 05:38Z left ``evidence_path`` null and nothing to read."""

    def evidence(self, record, reason, capture):
        path = (
            resume_root()
            / "evidence"
            / (
                f"{safe_component(str(record['key']))}-"
                f"{safe_component(str(record.get('attempt_id')))}-"
                f"{safe_component(reason)}.txt"
            )
        )
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(
                f"reason={reason}\nattempt_id={record.get('attempt_id')}\n"
                f"pane_id={record.get('pane_id')}\n\n{capture}",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
        except OSError:
            return None
        return path


def _rfc3339_utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _jsonl_line(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _housekeep_readiness_trails(root: Path) -> None:
    # Copied from context_supervisor._housekeep_readiness_trails (5333-5364).
    global _readiness_housekeeping_last_run
    current = time.time()
    previous = _readiness_housekeeping_last_run
    if previous is not None and 0 <= current - previous < (
        _READINESS_HOUSEKEEPING_INTERVAL_SECONDS
    ):
        return
    _readiness_housekeeping_last_run = current
    active = {
        r.get("attempt_id")
        for r in AttemptStore(root).all()
        if r.get("state") not in TERMINAL_STATES
    }
    for path in (root / "readiness").glob("*.jsonl"):
        try:
            age = current - path.stat().st_mtime
            ids = {
                json.loads(line).get("attempt_id")
                for line in path.read_text(encoding="utf-8").splitlines()
                if line
            }
            if age > _READINESS_TRAIL_RETENTION_SECONDS and not (active & ids):
                path.unlink()
        except (OSError, json.JSONDecodeError, AttributeError):
            continue


class ReadinessTrailObserver(Observer):
    """``readiness/<key>-<attempt_id>.jsonl``: one ``sample`` record per composer
    capture and one ``outcome`` record per evaluation, 1 MiB cap with a
    ``truncated`` marker, 7-day retention (_write_readiness_evaluation 5382-5519)."""

    def readiness(self, record, samples, phases, outcome):
        from ai_chassis.prompt_injection.readiness import composer_text

        root = resume_root()
        try:
            directory = root / "readiness"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            _housekeep_readiness_trails(root)
            path = directory / (
                f"{safe_component(str(record['key']))}-"
                f"{safe_component(str(record['attempt_id']))}.jsonl"
            )
            existing = path.read_bytes() if path.exists() else b""
            if b'"record_type":"truncated"' in existing:
                return
            sequence = 1 + sum(
                1 for line in existing.splitlines() if b'"record_type":"sample"' in line
            )
            timestamp = _rfc3339_utc_now()
            last_staged = record.get("last_staged_text")
            rows: list[dict[str, Any]] = []
            for sample, phase in zip(samples, phases, strict=True):
                text = composer_text(sample.content)
                starts = bool(
                    last_staged
                    and isinstance(text, str)
                    and text.startswith(last_staged)
                )
                row = {
                    "record_type": "sample",
                    "timestamp": timestamp,
                    "attempt_id": record["attempt_id"],
                    "sequence": sequence,
                    "pane_id": record.get("pane_id"),
                    "phase": phase,
                    "classification": sample.classification,
                    "attribute_class": sample.attribute_class,
                    "region": sample.region,
                    "goal_active": outcome.get("goal_active", "unknown"),
                    "claude_code_notice": sample.claude_code_notice,
                    "content_length": len(text) if isinstance(text, str) else None,
                    "content_starts_with_last_staged_text": starts,
                    "content_repeats_last_staged_text": bool(
                        starts and text.count(last_staged) > 1
                    ),
                }
                if sample.raw_content_debug is not None:
                    row["raw_content_debug"] = sample.raw_content_debug
                rows.append(row)
                sequence += 1
            send_result = outcome.get("send_result", "not-attempted")
            rows.append(
                {
                    "record_type": "outcome",
                    "timestamp": timestamp,
                    "attempt_id": record["attempt_id"],
                    "sample_sequence": sequence - 1,
                    "send_attempted": bool(outcome.get("send_attempted", False)),
                    "send_result": send_result,
                    "failure_reason": (
                        None
                        if send_result in {"not-attempted", "succeeded"}
                        else send_result
                    ),
                    "text_may_have_been_staged": bool(
                        outcome.get("text_may_have_been_staged", False)
                    ),
                    "idle_proof": outcome.get("idle_proof"),
                    "landing": outcome.get("landing", "none"),
                    "stagings": int(record.get("stagings", 0)),
                    "submissions": int(record.get("submissions", 0)),
                    "backoff_seconds": int(record.get("backoff_seconds", 0)),
                    "goal_active": outcome.get("goal_active", "unknown"),
                    "claude_code_notice": samples[-1].claude_code_notice
                    if samples
                    else None,
                }
            )
            payload = b"".join(_jsonl_line(row) for row in rows)
            marker = _jsonl_line(
                {
                    "record_type": "truncated",
                    "timestamp": timestamp,
                    "attempt_id": record["attempt_id"],
                    "first_omitted_sample_sequence": rows[0].get("sequence", sequence),
                    "reason": "size-cap",
                }
            )
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                if (
                    len(existing) + len(payload) + len(marker)
                    > _READINESS_TRAIL_MAX_BYTES
                ):
                    if len(existing) + len(marker) <= _READINESS_TRAIL_MAX_BYTES:
                        os.write(fd, marker)
                    return
                os.write(fd, payload)
            finally:
                os.close(fd)
        except (OSError, ValueError, TypeError):
            return


# --- the run record (ISSUE-REF) ------------------------------------------------------

RUN_RECORD_MAX_BYTES = 1024 * 1024
_RUN_RECORD_RETENTION_SECONDS = 7 * 24 * 60 * 60
_RUN_HOUSEKEEPING_INTERVAL_SECONDS = 24 * 60 * 60
_run_housekeeping_last_run: float | None = None
_EVENTS = "events.jsonl"


def new_run_id(use_case: str) -> str:
    """``<use_case>-<UTC stamp>-<6 hex>``, minted by a run's first attempt."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{safe_component(use_case)}-{stamp}-{secrets.token_hex(3)}"


def run_record_path(run_id: str) -> Path:
    return resume_root() / "runs" / safe_component(run_id)


def _run_bytes(directory: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _open_runs() -> set[str]:
    """Run ids a self-heal state still points at; their records are never expired."""
    keep: set[str] = set()
    for path in (resume_root() / "self-heal").glob("*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(state, dict):
            keep |= {
                str(state[k])
                for k in ("pending_run_id", "escalation_run_id")
                if state.get(k)
            }
            keep |= {str(h.get("run_id")) for h in state.get("history") or []}
    return keep


def _remove_run(directory: Path) -> None:
    """Delete one expired run directory: its captures, its events, then the dirs."""
    for sub in sorted(directory.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if sub.is_file() or sub.is_symlink():
            sub.unlink(missing_ok=True)
        elif sub.is_dir():
            sub.rmdir()
    directory.rmdir()


def _housekeep_runs(*, force: bool = False) -> None:
    """The readiness trail's retention (7 days) applied to ``runs/``."""
    global _run_housekeeping_last_run
    current = time.time()
    previous = _run_housekeeping_last_run
    if (
        not force
        and previous is not None
        and 0 <= current - previous < _RUN_HOUSEKEEPING_INTERVAL_SECONDS
    ):
        return
    _run_housekeeping_last_run = current
    keep = _open_runs()
    for directory in (resume_root() / "runs").glob("*"):
        try:
            if (
                directory.is_dir()
                and directory.name not in keep
                and current - directory.stat().st_mtime > _RUN_RECORD_RETENTION_SECONDS
            ):
                _remove_run(directory)
        except OSError:
            continue


def _within_cap(directory: Path, adding: int) -> bool:
    """Whether ``adding`` bytes fit; once they do not, one ``truncated`` event (if it
    fits) ends the record and every later write is dropped. A ``.truncated`` marker file
    answers "already ended" without re-reading the record on every append."""
    ended = directory / ".truncated"
    if ended.exists():
        return False
    used = _run_bytes(directory)
    if used + adding <= RUN_RECORD_MAX_BYTES:
        return True
    marker = _jsonl_line(
        {"event": "truncated", "at": _rfc3339_utc_now(), "reason": "size-cap"}
    )
    if used + len(marker) <= RUN_RECORD_MAX_BYTES:
        with (directory / _EVENTS).open("ab") as handle:
            handle.write(marker)
    ended.touch()
    return False


def append_run_event(
    run_id: str | None,
    event: str,
    fields: dict[str, Any],
    *,
    attempt_id: str | None = None,
    housekeep: bool = False,
) -> Path | None:
    """Append one event to ``runs/<run_id>/events.jsonl``; best-effort like every sink."""
    if not run_id:
        return None
    try:
        _housekeep_runs(force=housekeep)
        directory = run_record_path(run_id)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        line = _jsonl_line(
            {
                "event": event,
                "at": _rfc3339_utc_now(),
                "run_id": run_id,
                "attempt_id": attempt_id,
                **fields,
            }
        )
        if not _within_cap(directory, len(line)):
            return None
        path = directory / _EVENTS
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return path
    except (OSError, TypeError, ValueError):
        return None


def write_run_capture(run_id: str | None, label: str, text: str) -> Path | None:
    """``runs/<run_id>/captures/NNN-<label>.txt``, counted against the record's cap."""
    if not run_id:
        return None
    try:
        directory = run_record_path(run_id)
        captures = directory / "captures"
        captures.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = text.encode("utf-8", errors="replace")
        if not _within_cap(directory, len(data)):
            return None
        number = len(list(captures.glob("*.txt"))) + 1
        while True:
            path = captures / f"{number:03d}-{safe_component(label)[:60]}.txt"
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                number += 1
                continue
            break
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return path
    except OSError:
        return None


def write_notice(
    record: dict[str, Any], reason: str, text: str, *, heading: str = "SELF-HEAL"
) -> Path | None:
    """A notice the ``UserPromptSubmit`` hook relays into the target session, for the
    self-heal loop's own reasons and a validation failure where self-heal is off
    (``heading`` ``VALIDATION``), and a ``notice`` event in the run record."""
    directory = resume_root() / "notices"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / f"{safe_component(str(record['key']))}.md"
        body = (
            f"PROMPT INJECTION {heading} ({record.get('use_case')}, reason={reason}).\n\n"
            f"{text}\n\nrun_id={record.get('run_id')}\n"
            f"attempt_id={record.get('attempt_id')}\n"
        )
        _publish_notice(path, body)
    except OSError:
        return None
    append_run_event(
        record.get("run_id"),
        "notice",
        {"reason": reason, "path": str(path)},
        attempt_id=record.get("attempt_id"),
    )
    return path


class RunRecordObserver(Observer):
    """``runs/<run_id>/``: every event of one run under one id, its captures, and a
    ``notice`` event whenever the notice sink writes one."""

    def run_event(self, record, event, fields):
        append_run_event(
            record.get("run_id"), event, fields, attempt_id=record.get("attempt_id")
        )

    def run_capture(self, record, label, text):
        return write_run_capture(record.get("run_id"), label, text)

    def notice(self, record, reason):
        append_run_event(
            record.get("run_id"),
            "notice",
            {"reason": reason},
            attempt_id=record.get("attempt_id"),
        )


def parity_observers(ledger: str = "attempts") -> list[Observer]:
    """The sinks every injector carries; a subclass or caller can add, not remove."""
    return [
        StageLogObserver(),
        AttemptStoreObserver(AttemptStore(ledger=ledger)),
        NoticeObserver(),
        DebugCaptureObserver(),
        EvidenceObserver(),
        ReadinessTrailObserver(),
        RunRecordObserver(),
    ]


class CompositeObserver(Observer):
    """Fan one event out to every sink; one sink's failure never reaches the others."""

    def __init__(self, observers: Iterable[Observer]) -> None:
        self.observers = list(observers)

    def _each(self, method: str, *args: Any) -> Any:
        result = None
        for observer in self.observers:
            try:
                value = getattr(observer, method)(*args)
            except Exception:  # noqa: BLE001 - a sink failure is never an attempt failure
                continue
            if result is None and value is not None:
                result = value
        return result

    def stage(self, name, key, detail):
        self._each("stage", name, key, detail)

    def attempt(self, record):
        return self._each("attempt", record)

    def notice(self, record, reason):
        return self._each("notice", record, reason)

    def clear_notice(self, record):
        self._each("clear_notice", record)

    def debug_capture(self, record, reason, capture):
        return self._each("debug_capture", record, reason, capture)

    def evidence(self, record, reason, capture):
        return self._each("evidence", record, reason, capture)

    def readiness(self, record, samples, phases, outcome):
        self._each("readiness", record, samples, phases, outcome)

    def exception(self, event, error):
        return self._each("exception", event, error)

    def run_event(self, record, event, fields):
        self._each("run_event", record, event, fields)

    def run_capture(self, record, label, text):
        return self._each("run_capture", record, label, text)
