"""Detached restart supervision; the engine only signals the existing tmux pane.

Each private run directory retains the input, process identities, log and atomic exit
receipt. A separate supervisor owns wait(), the deadline and audit completion, so a
notifier's lifetime never limits compaction or loses the engine's return code.
"""

from __future__ import annotations

import dataclasses
import contextlib
import json
import logging
import math
import os
import re
import select
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import psutil

from ai_chassis import remote_launch as launch
from ai_chassis.context_supervisor import _ai_cli_config_paths
from ai_chassis.job_supervisor import PosixPlatform, _process_start_time, _same_process

RUNS_DIR = "restart-runs"
MARGIN_SECONDS = 300
START_TIMEOUT_SECONDS = 10
MAX_BIRTH_TIME = 1e12
SUCCESS_VERDICTS = frozenset({"restarted", "restarted-local-verified"})
# Recovery has no independent config section. Share a transition/replay budget per
# existing source event so a completion backlog cannot monopolise collection.
MAX_RECOVERY_RUNS = 8
MAX_RECOVERY_ATTEMPTS = 32
MAX_COMPLETION_FAILURES = 3


def bound_seconds() -> float:
    """Read the engine's file at call time, without load_config's migration/writes."""
    section = {}
    for path in _ai_cli_config_paths():
        try:
            with path.open("rb") as stream:
                section = tomllib.load(stream).get("restart_session", {})
            break
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            break
    if not isinstance(section, dict):
        section = {}

    def seconds(key, default):
        value = section.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return value if math.isfinite(value) and value >= 0 else default

    margin = max(
        MARGIN_SECONDS,
        seconds("term_grace_seconds", 20)
        + seconds("relaunch_timeout_seconds", 180)
        + 60,
    )
    return (
        seconds("compaction_wait_seconds", 1800)
        + seconds("settle_seconds", 60)
        + margin
    )


def lock(path: Path, *, nonblocking: bool = False):
    launch._private_dir(path.parent)
    return PosixPlatform().lock_file(path, nonblocking=nonblocking)


def read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write(path: Path, value: dict) -> None:
    """Atomic owner-only receipt, flushed before it can be used for recovery."""
    from ai_chassis.agent_mail_notifier import write_if_changed

    write_if_changed(path, json.dumps(value))
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _append(state_dir: Path, row: dict) -> None:
    launch.append_audit(state_dir / launch.AUDIT_FILE, row)
    with (state_dir / launch.AUDIT_FILE).open("rb") as stream:
        os.fsync(stream.fileno())


def engine_alive(row: dict) -> bool:
    pid = row.get("engine_pid")
    try:
        return (
            _same_process(pid, row.get("engine_start_time"))
            and os.getpgid(pid) == row.get("engine_pgid") == pid
        )
    except (ProcessLookupError, PermissionError):
        return False


def terminate(row: dict) -> None:
    # Identity includes creation time as well as the session-leading pid/pgid.
    # Never signal a recycled pid, the supervisor, or the caller's own group.
    if engine_alive(row):
        try:
            os.killpg(row["engine_pgid"], signal.SIGKILL)
        except ProcessLookupError:
            pass


def _locally_verified_without_observer(job: Path) -> bool:
    """Only a proven local replacement with a known absent observer qualifies."""
    try:
        output = (job / "engine.log").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False
    fields = re.findall(
        r"(?<![\w-])(reason|local|peer|replacement_pid)=([^;\s]+)", output
    )
    values = dict(fields)
    if (
        len(fields) != len(values)
        or values.get("reason") != "verification-incomplete"
        or values.get("local") != "VERIFIED"
        or values.get("peer") != "UNVERIFIED"
        or not re.fullmatch(r"[1-9][0-9]*", values.get("replacement_pid", ""))
    ):
        return False
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    directory = base / "ai-cli-utils" / "restart-session.d"
    try:
        # Unlike glob(), iterdir() surfaces an unreadable or missing directory.
        for path in directory.iterdir():
            if path.suffix == ".toml":
                with path.open("rb") as stream:
                    if "observer" in tomllib.load(stream):
                        return False
    except (OSError, ValueError):
        return False
    return True


def validate_receipt(receipt, bind_identity, actor):
    """Trust only the documented exec receipt from this supervised actor."""
    if not isinstance(receipt, dict) or receipt.get("reason") != "started":
        raise ValueError("restart-receipt:invalid-object")
    for key in ("session_id", "restart_session_id"):
        if not isinstance(receipt.get(key), str) or not receipt[key]:
            raise ValueError("restart-receipt:invalid-session-id")
    if receipt["session_id"] != receipt["restart_session_id"]:
        raise ValueError("restart-receipt:conversation-mismatch")
    if "launch_id" not in receipt or receipt["launch_id"] not in {None, bind_identity}:
        raise ValueError("restart-receipt:launch-mismatch")
    if any(
        type(receipt.get(key)) is not int or receipt[key] <= 1
        for key in ("replacement_pid", "engine_pid")
    ):
        raise ValueError("restart-receipt:invalid-pid")
    for key in ("replacement_start_time", "engine_start_time"):
        value = receipt.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 < value < MAX_BIRTH_TIME
        ):
            raise ValueError("restart-receipt:invalid-birth")
    if any(
        receipt[key] != actor.get(key) for key in ("engine_pid", "engine_start_time")
    ):
        raise ValueError("restart-receipt:actor-mismatch")
    return receipt


def _event_outcome(output, outcome, spec, row):
    if outcome["verdict"] == "timed-out":
        return outcome
    refused = {
        "exit": outcome.get("exit"),
        "verdict": "refused",
        "reason": "restart-receipt:nonzero-exit",
    }
    if outcome.get("exit") != 0:
        return refused
    try:
        receipt = validate_receipt(json.loads(output), spec["bind_identity"], row)
    except (ValueError, TypeError):
        return {**refused, "reason": "restart-receipt:invalid"}
    return {
        "exit": 0,
        "verdict": "restarted",
        "reason": "restart-receipt:started",
        "receipt": receipt,
    }


def complete(job: Path, outcome: dict) -> dict:
    """Serialize supervisor/reconciler, writing exactly one durable completion row."""
    with lock(job / "completion.lock"):
        spec = read(job / "request.json")
        row = _complete_locked(job, outcome, spec)
        _completion_signal(job, spec, row)
        return row


def _complete_locked(job, outcome, spec, *, strict=False):
    """The shared completion transition; callers own completion.lock."""
    reader = launch.read_audit_strict if strict else launch.read_audit
    state_dir = job.parent.parent
    row = read(job / "started.json") or spec["record"]
    existing = next(
        (
            r
            for r in reader(state_dir / launch.AUDIT_FILE)
            if r.get("request_id") == row["request_id"]
            and r.get("row_kind") == "completion"
        ),
        None,
    )
    if existing:
        return existing
    # A saved exit always wins a racing deadline check.
    saved = read(job / "exit.json")
    if saved:
        outcome = saved
    event_mode = spec.get("bind_identity") is not None
    if (
        not event_mode
        and outcome["verdict"] == "refused"
        and outcome.get("exit") is not None
        and outcome["exit"] != 0
        and _locally_verified_without_observer(job)
    ):
        outcome = {**outcome, "verdict": "restarted-local-verified"}
    if not event_mode and (not saved or outcome != saved):
        write(job / "exit.json", outcome)
    if outcome["verdict"] == "timed-out":
        terminate(row)
    try:
        output = (job / ("engine.stdout" if event_mode else "engine.log")).read_text(
            encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        output = ""
    if event_mode:
        outcome = _event_outcome(output, outcome, spec, row)
        if "receipt" in outcome:
            row = {**row, "receipt": outcome["receipt"]}
            write(job / "started.json", row)
    if event_mode and (not saved or outcome != saved):
        write(job / "exit.json", outcome)
    reason = outcome.get("reason", output)
    row = {
        **row,
        **outcome,
        "row_kind": "completion",
        "completed_at": launch.iso(int(time.time())),
        "reason": reason,
        "verdict_reason": reason if event_mode else output,
    }
    _append(state_dir, row)
    return row


def _completion_diagnostic(job, diagnostic, report, restore):
    degraded = job / "completion-degraded.json"
    if read(degraded) == diagnostic:
        if restore is not None:
            restore(f"restart-recovery:{job.name}", diagnostic["reason"])
        return True
    if report is not None and report(
        f"restart-recovery:{job.name}", diagnostic["reason"]
    ):
        # Delivery precedes persistence: a crash can duplicate, never lose, a report.
        write(degraded, diagnostic)
        return True
    return False


def _completion_signal(job, spec, row, *, report=None, restore=None, failures=None):  # noqa: PLR0913 - optional replay diagnostics
    """Replay durable completion under completion.lock until intake accepts it."""
    if spec.get("bind_identity") is None:
        return False
    receipt = row.get("receipt")
    target = row.get("resolved_target") or row.get("target")
    if row.get("verdict") != "restarted" or not receipt:
        return False
    from ai_chassis.wake import sessions
    from ai_chassis.wake.events import digest

    marker = job / "completion-signalled.json"
    if read(marker).get("signalled"):
        return False
    missing = [
        name
        for name, value in (("wake_root", spec.get("wake_root")), ("target", target))
        if not value
    ]
    try:
        if missing:
            # Only a digest and field names are persisted, never request contents.
            # Serialize with signal emission and suppress repeats across restarts.
            # The supervisor has no reporter: leave diagnostics for recovery.
            diagnostic = {
                "inputs": digest([spec, row]),
                "reason": "missing:" + ",".join(missing),
            }
            delivered = _completion_diagnostic(job, diagnostic, report, restore)
            if failures is not None:
                failures.append(
                    (
                        "ValueError",
                        diagnostic["reason"],
                        not delivered,
                    )
                )
            return False
        # Identity comes only from the durable completion, never replay time.
        # A crash after intake but before the marker must dedupe even downstream
        # goal actions, not just this launch's carried prompt.
        epoch = launch.parse_iso(row["completed_at"])
        if epoch is None:
            raise ValueError("invalid-completion-time")
        sessions.emit(
            Path(spec["wake_root"]),
            target.partition("@")[0],
            receipt["session_id"],
            "session-evidence",
            epoch=epoch,
            metadata="completion="
            + digest([row["request_id"], row["completed_at"], row["verdict"]]),
        )
        write(marker, {"signalled": True})
        if (job / "completion-failures.json").exists():
            write(job / "completion-failures.json", {})
        return True
    except Exception as exc:
        # A notification failure cannot invalidate an already durable completion.
        # The next recovery retries it without re-executing the restart.
        _recovery_note(report, job, exc)
        if failures is not None:
            failures.append((type(exc).__name__, type(exc).__name__, False))
        return False


def _guard_report(report):
    """A broken diagnostic sink cannot abort a collection or the remaining runs."""
    if report is None:
        return None
    failed = False

    def guarded(name, error):
        nonlocal failed
        try:
            return report(name, error) is not False
        except Exception:
            if not failed:
                failed = True
                with contextlib.suppress(Exception):
                    logging.getLogger(__name__).error(
                        "restart-recovery:reporter-failed"
                    )
            return False

    return guarded


def _recovery_note(report, job, exc):
    if report is not None:
        # Persisted request contents and exception text may contain private data.
        report(f"restart-recovery:{job.name}", type(exc).__name__)


def _replay_evidence(job, row, marker):
    """Stat avoids reopening unchanged quarantined requests; the digest binds content."""
    from ai_chassis.wake.events import digest

    def stamp(name):
        try:
            info = (job / name).stat()
            return [
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            ]
        except OSError as exc:
            return type(exc).__name__

    stamps = (
        stamp("request.json")
        if row.get("row_kind") == "completion"
        else [
            stamp(name)
            for name in ("request.json", "started.json", "exit.json", "engine.stdout")
        ]
    )
    return digest([stamps, row, marker])


def _recovery_inputs(spec, row, marker, evidence):
    from ai_chassis.wake.events import digest

    inputs = [spec, row, marker]
    if row.get("row_kind") != "completion":
        inputs.append(evidence)
    return digest(inputs)


def _replay_rows(completed, cursor):
    def key(row):
        return [str(row.get("completed_at", "")), str(row.get("request_id", ""))]

    rows = sorted(completed.values(), key=key)
    if (
        isinstance(cursor, list)
        and len(cursor) == len(key({}))
        and all(isinstance(v, str) for v in cursor)
    ):
        split = next((i for i, row in enumerate(rows) if key(row) > cursor), len(rows))
        rows = rows[split:] + rows[:split]
    return rows


def _read_replay_cursor(path):
    record = read(path)
    last = record.get("last") if isinstance(record, dict) else None
    if (
        isinstance(last, list)
        and len(last) == 2  # noqa: PLR2004 - durable cursor is a pair of strings
        and all(isinstance(value, str) for value in last)
    ):
        return last
    return None


def _read_completion_failures(path):
    record = read(path)
    if (
        not isinstance(record, dict)
        or type(record.get("attempts")) is not int
        or record["attempts"] < 0
    ):
        return {}
    for field in ("inputs", "diagnostic", "evidence", "reason", "failure"):
        if field in record and not isinstance(record[field], str):
            return {}
    if "report_pending" in record and not isinstance(record["report_pending"], bool):
        return {}
    return record


def _try_event_signal(job, spec, row, marker, callbacks, *, evidence, now=None):  # noqa: C901, PLR0913 - one locked transaction with pre-read evidence
    from ai_chassis.wake.events import digest

    report, restore = callbacks
    acquired = False
    transitioned = False
    try:
        with lock(job / "completion.lock", nonblocking=True):
            acquired = True
            record_path = job / "completion-failures.json"
            previous = _read_completion_failures(record_path)
            inputs = _recovery_inputs(spec, row, marker, evidence)
            count = (
                previous.get("attempts", 0) if previous.get("inputs") == inputs else 0
            )
            if (
                previous.get("inputs") == inputs
                and previous.get("evidence") != evidence
            ):
                previous = {**previous, "evidence": evidence}
                write(record_path, previous)
            if _quarantined(job, previous, evidence, report, restore):
                return False
            failures = []
            # Replay rechecks the marker; incomplete runs also recheck the audit
            # inside the shared completion transition to serialize racing callers.
            try:
                if not isinstance(marker, dict):
                    raise ValueError("invalid-signal-marker")
                if spec.get("record", {}).get("request_id") != row["request_id"]:
                    raise ValueError("invalid-restart-request")
                if row.get("row_kind") != "completion":
                    # Recheck under the same lock as supervisor completion. A saved
                    # exit still wins; a concurrent caller's audit row is reused.
                    started = read(job / "started.json")
                    outcome = _recovery_outcome(job, spec, started, now)
                    if outcome is None:
                        return False
                    row = _complete_locked(job, outcome, spec, strict=True)
                    transitioned = True
                signalled = _completion_signal(
                    job, spec, row, report=report, restore=restore, failures=failures
                )
            except Exception as exc:
                _recovery_note(report, job, exc)
                failures.append((type(exc).__name__, type(exc).__name__, False))
                signalled = False
            if failures:
                kind, reason, report_pending = failures[-1]
                write(
                    record_path,
                    {
                        "inputs": inputs,
                        "diagnostic": digest([spec, row]),
                        "evidence": evidence,
                        "attempts": count + 1,
                        "failure": kind,
                        "reason": reason,
                        "report_pending": report_pending,
                    },
                )
            return signalled or transitioned
    except Exception as exc:
        # PosixPlatform wraps flock contention, preserving its cause.
        if not isinstance(exc, BlockingIOError) and not isinstance(
            exc.__cause__, BlockingIOError
        ):
            _recovery_note(report, job, exc)
            if not acquired:
                try:
                    record_path = job / "completion-failures.json"
                    previous = _read_completion_failures(record_path)
                    inputs = _recovery_inputs(spec, row, marker, evidence)
                    count = (
                        previous.get("attempts", 0)
                        if previous.get("inputs") == inputs
                        else 0
                    )
                    # The broken lock cannot protect this atomic replace. This
                    # degraded path accepts last-writer-wins: concurrent failures
                    # can undercount, but work never grows without bound because
                    # the cursor and per-call attempt cap still bound each call.
                    write(
                        record_path,
                        {
                            "inputs": inputs,
                            "diagnostic": digest([spec, row]),
                            "evidence": evidence,
                            "attempts": count + 1,
                            "failure": type(exc).__name__,
                            "reason": type(exc).__name__,
                            "report_pending": False,
                        },
                    )
                except Exception as record_exc:
                    _recovery_note(report, job, record_exc)
    return transitioned


def _quarantined(job, previous, evidence, report, restore):
    if (
        not isinstance(previous, dict)
        or previous.get("evidence") != evidence
        or previous.get("attempts", 0) < MAX_COMPLETION_FAILURES
    ):
        return False
    if previous.get("report_pending"):
        diagnostic = {
            "inputs": previous.get("diagnostic", ""),
            "reason": previous.get("reason", ""),
        }
        if _completion_diagnostic(job, diagnostic, report, restore):
            write(
                job / "completion-failures.json", {**previous, "report_pending": False}
            )
    elif restore is not None:
        restore(f"restart-recovery:{job.name}", previous.get("reason", ""))
    return True


def _event_recovery_ready(job, spec, now):
    try:
        return _recovery_outcome(job, spec, read(job / "started.json"), now) is not None
    except Exception:
        # Record malformed evidence through the locked failure path.
        return True


def _recover_event_signals(state_dir, pending, completed, report, restore, *, now=None):  # noqa: C901, PLR0912, PLR0913 - independent per-run faults and both budgets
    """Bound transitions and replay; resume fairly on the next existing event."""
    cursor_path = state_dir / "completion-replay-cursor.json"
    candidates = dict(completed)
    if now is not None:
        candidates.update(
            (name, {"request_id": name}) for name in pending if name not in completed
        )
    rows = _replay_rows(candidates, _read_replay_cursor(cursor_path))
    successes = attempts = 0
    for row in rows:
        if successes >= MAX_RECOVERY_RUNS or attempts >= MAX_RECOVERY_ATTEMPTS:
            break
        incomplete = row.get("row_kind") != "completion"
        if not incomplete and (
            row.get("verdict") != "restarted" or not row.get("receipt")
        ):
            continue
        candidate = pending.get(row["request_id"])
        if candidate is None:
            continue
        job, marker = candidate
        try:
            # Incomplete-run observations themselves spend the attempt budget,
            # including live, malformed and unreadable runs; advance before reads.
            if incomplete:
                attempts += 1
                write(cursor_path, {"last": ["", str(row["request_id"])]})
            previous = _read_completion_failures(job / "completion-failures.json")
            # Capture before reading inputs: a racing repair must invalidate
            # every failure record written for this snapshot, even without a lock.
            evidence = _replay_evidence(job, row, marker)
            if _quarantined(job, previous, evidence, report, restore):
                continue
            spec = read(job / "request.json")
            if isinstance(spec, dict) and spec.get("bind_identity") is None:
                continue
            # A metadata-only change can still prove a pure skip without a lock.
            # Never carry this snapshot's count into an actual signal attempt.
            if previous.get("inputs") == _recovery_inputs(
                spec, row, marker, evidence
            ) and _quarantined(
                job, previous, previous.get("evidence"), report, restore
            ):
                if previous.get("evidence") != evidence:
                    record_path = job / "completion-failures.json"
                    latest = _read_completion_failures(record_path)
                    if latest.get("inputs") == previous.get("inputs") and latest.get(
                        "evidence"
                    ) == previous.get("evidence"):
                        # Detect changes since the reread. This is not atomic CAS:
                        # a clear after this comparison can still lose to replace.
                        if _read_completion_failures(record_path) == latest:
                            write(record_path, {**latest, "evidence": evidence})
                continue
            if incomplete:
                if not _event_recovery_ready(job, spec, now):
                    continue
            else:
                attempts += 1
                write(
                    cursor_path,
                    {
                        "last": [
                            str(row.get("completed_at", "")),
                            str(row["request_id"]),
                        ]
                    },
                )
            if _try_event_signal(
                job, spec, row, marker, (report, restore), evidence=evidence, now=now
            ):
                successes += 1
        except Exception as exc:
            _recovery_note(report, job, exc)


def _unsignalled_runs(state_dir, report):
    pending = {}
    for job in (state_dir / RUNS_DIR).glob("*"):
        try:
            marker = read(job / "completion-signalled.json")
            if not (isinstance(marker, dict) and marker.get("signalled")):
                pending[job.name] = (job, marker)
        except Exception as exc:
            _recovery_note(report, job, exc)
    return pending


def recover(
    state_dir: Path,
    *,
    now: float | None = None,
    event_only=False,
    report=None,
    restore=None,
) -> list[dict]:
    """Reconcile durable runs, including completed event runs awaiting intake."""
    now = time.time() if now is None else now
    report = _guard_report(report)
    restore = _guard_report(restore)
    pending = _unsignalled_runs(state_dir, report)
    # Durable markers are inspected before the audit, request files or locks.
    if not pending:
        return []
    try:
        rows = launch.read_audit_strict(state_dir / launch.AUDIT_FILE)
    except OSError as exc:
        _recovery_note(report, Path("audit"), exc)
    else:
        completed = {
            r.get("request_id"): r for r in rows if r.get("row_kind") == "completion"
        }
        try:
            _recover_event_signals(
                state_dir,
                pending,
                completed,
                report,
                restore,
                now=now if event_only else None,
            )
        except Exception as exc:
            _recovery_note(report, Path("replay"), exc)
    if event_only:
        return []
    # Strict evidence belongs only to event recovery. Preserve the lenient legacy
    # census and complete() behavior even when that replay snapshot is unreadable.
    completed = {
        r.get("request_id"): r
        for r in launch.read_audit(state_dir / launch.AUDIT_FILE)
        if r.get("row_kind") == "completion"
    }
    return _recover_active_runs(pending, completed, now, report)


def _recover_active_runs(pending, completed, now, report):
    active = []
    for job, _marker in pending.values():
        try:
            spec = read(job / "request.json")
            if not spec:
                continue
            completion = completed.get(spec["record"]["request_id"])
            if completion is not None:
                continue
            started = read(job / "started.json")
            outcome = _recovery_outcome(job, spec, started, now)
            if outcome is not None:
                complete(job, outcome)
            else:
                active.append(started or spec["record"])
        except Exception as exc:
            _recovery_note(report, job, exc)
    return active


def _recovery_outcome(job, spec, started, now):
    """One observation of the legacy saved-exit, deadline and liveness evidence."""
    outcome = read(job / "exit.json")
    if outcome:
        return outcome
    if started and now >= started["deadline"]:
        return {"verdict": "timed-out", "exit": None, "reason": "restart-timeout"}
    if (
        started
        and not engine_alive(started)
        and not _same_process(
            started.get("supervisor_pid"), started.get("supervisor_start_time")
        )
    ):
        return {
            "verdict": "timed-out",
            "exit": None,
            "reason": "restart-exit-status-missing",
        }
    if not started and now >= spec["created_at"] + START_TIMEOUT_SECONDS:
        return {
            "verdict": "refused",
            "exit": None,
            "reason": "launch-failed (supervisor-start)",
        }
    return None


def start(decision, *, state_dir, record, runner, message=None, bind_identity=None):  # noqa: PLR0913 - restart receipt plus optional bind provenance
    """Await only the supervisor spawn receipt, bounded equally in both modes."""
    job = state_dir / RUNS_DIR / decision.request_id
    launch._private_dir(job)
    spec = {
        "record": {**record, "argv": decision.argv}
        if bind_identity is not None
        else record,
        "argv": decision.argv,
        "cwd": decision.cwd,
        "bound_seconds": bound_seconds(),
        "created_at": time.time(),
        "message": message,
    }
    if bind_identity is not None:
        from ai_chassis.wake import sessions

        spec["bind_identity"] = bind_identity
        spec["wake_root"] = str(sessions.mail_root())
    write(job / "request.json", spec)
    os.mkfifo(job / "ready.fifo", launch.FILE_MODE)
    read_fd = os.open(job / "ready.fifo", os.O_RDWR | os.O_NONBLOCK)
    try:
        runner(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                str(job.resolve()),
            ],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if select.select([read_fd], [], [], START_TIMEOUT_SECONDS)[0]:
            data = os.read(read_fd, 65536)
            if data:
                row = json.loads(data)
                return dataclasses.replace(
                    decision,
                    **{
                        key: row.get(key)
                        for key in (
                            "reason",
                            "exit",
                            "verdict",
                            "verdict_reason",
                            "engine_pid",
                            "engine_pgid",
                            "engine_start_time",
                            "receipt",
                            "started_at",
                            "log_path",
                        )
                    },
                )
        # Serialize against spawn before declaring startup failed. An engine that
        # already started is timed out and killed only by its recorded identity.
        row = complete(
            job,
            {
                "verdict": "timed-out",
                "exit": None,
                "reason": "launch-failed (supervisor-start)",
            },
        )
        return dataclasses.replace(
            decision, verdict=row["verdict"], reason=row["reason"]
        )
    except OSError as exc:
        row = complete(
            job,
            {
                "verdict": "refused",
                "exit": None,
                "reason": f"launch-failed ({type(exc).__name__})",
            },
        )
        return dataclasses.replace(
            decision, verdict=row["verdict"], reason=row["reason"]
        )
    finally:
        os.close(read_fd)


def held_message(state_dir: Path, request_id: str, message: str | None) -> bool:
    """Whether an authenticated inbox replay belongs to a durable held delivery."""
    spec = read(state_dir / RUNS_DIR / request_id / "request.json")
    return message is not None and spec.get("message") == message


def deliver_pending(state_dir: Path, *, runner=subprocess.run) -> list[dict]:
    """Release completed runs through launch's pane resolver and shared injector.

    The request remains on disk on failure. Like launch, uncertain submission is
    surfaced rather than blindly repeated. A receipt precedes the terminal effect;
    an interrupted attempt is explicitly unverified, never silently discarded.
    """
    from ai_chassis import remote_launch_session as session

    completions = {
        row["request_id"]: row
        for row in launch.read_audit(state_dir / launch.AUDIT_FILE)
        if row.get("row_kind") == "completion"
    }
    delivered = []
    for request_id, completion in completions.items():
        job = state_dir / RUNS_DIR / request_id
        spec = read(job / "request.json")
        if spec.get("message") is None:
            continue
        with lock(job / "delivery.lock"):
            rows = launch.read_audit(state_dir / launch.AUDIT_FILE)
            if any(
                row.get("request_id") == request_id
                and row.get("row_kind") == "delivery-outcome"
                for row in rows
            ):
                continue
            receipt = read(job / "delivery.json")
            if not receipt:
                # Reserve before injection so an uncertain attempt cannot submit twice.
                write(job / "delivery.json", {"state": "delivering"})
                outcome = _deliver_message(completion, spec["message"], runner, session)
                receipt = {"state": "complete", "outcome": outcome}
                write(job / "delivery.json", receipt)
            outcome = receipt.get("outcome") or {
                "delivery_state": "unverified",
                "delivery_reason": "restart-message-delivery-interrupted",
            }
            row = {
                **completion,
                "row_kind": "delivery-outcome",
                "delivery_target": completion.get("resolved_target")
                or completion["target"],
                "delivery_notice": completion["verdict"] not in SUCCESS_VERDICTS,
                **outcome,
            }
            _append(state_dir, row)
            delivered.append(row)
    return delivered


def _deliver_message(completion, message, runner, session):
    identity = (completion.get("resolved_target") or completion["target"]).partition(
        "@"
    )[0]
    deadline = time.monotonic() + launch.LAUNCH_TIMEOUT_S
    notice_fields = {}
    if completion["verdict"] not in SUCCESS_VERDICTS:
        from ai_chassis.prompt_injection.config import load_config

        header = f"Restart {completion['request_id']}: {completion['verdict']}"
        reason = "".join(
            char if char.isprintable() else " "
            for char in str(completion.get("reason") or "unspecified")
        )
        room = min(
            launch.RESTART_NOTICE_MAX_CHARS,
            load_config()["max_typed_chars"]
            - len(message)
            - len(launch.RESTART_NOTICE_SEPARATOR),
        )
        omitted = room < len(header)
        notice = _bounded_notice(
            header, reason, launch.RESTART_NOTICE_MAX_CHARS if omitted else room
        )
        notice_fields = {"delivery_notice_text": notice, "notice_omitted": omitted}
        if not omitted:
            message = notice + launch.RESTART_NOTICE_SEPARATOR + message
    try:
        pane = session.wait_live(identity, runner, deadline)
        if pane is None:
            return {
                **notice_fields,
                "delivery_state": "REFUSED",
                "delivery_reason": "target-not-live",
            }
        outcome = session.deliver(pane, message, max(0, deadline - time.monotonic()))
        return {
            **notice_fields,
            "delivery_state": outcome.state,
            "delivery_reason": outcome.reason,
            "delivery_attempt_id": outcome.attempt_id,
        }
    except Exception as exc:  # noqa: BLE001 - launch also surfaces uncertain submission
        return {
            **notice_fields,
            "delivery_state": "unverified",
            "delivery_reason": type(exc).__name__,
        }


def _bounded_notice(header: str, reason: str, limit: int) -> str:
    room = limit - len(header) - len(": ")
    if room < len("..."):
        return header
    if len(reason) > room:
        reason = reason[: room - len("...")] + "..."
    return f"{header}: {reason}"


def _ready(job: Path, row: dict) -> None:
    try:
        ready_fd = os.open(job / "ready.fifo", os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(ready_fd, json.dumps(row).encode())
        finally:
            os.close(ready_fd)
    except OSError:
        pass  # The notifier may disappear; completion remains durable.


def supervise(job: Path) -> None:
    spec = read(job / "request.json")
    child = None
    try:
        with lock(job / "completion.lock"):
            if (
                read(job / "exit.json")
                or time.time() >= spec["created_at"] + START_TIMEOUT_SECONDS
            ):
                raise OSError("startup expired")
            fd = os.open(
                job / "engine.log",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                launch.FILE_MODE,
            )
            with contextlib.ExitStack() as streams:
                output = streams.enter_context(os.fdopen(fd, "wb"))
                stdout = output
                stderr = subprocess.STDOUT
                if spec.get("bind_identity") is not None:
                    stdout_fd = os.open(
                        job / "engine.stdout",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        launch.FILE_MODE,
                    )
                    stdout = streams.enter_context(os.fdopen(stdout_fd, "wb"))
                    stderr = output
                child = subprocess.Popen(
                    spec["argv"],
                    cwd=spec["cwd"],
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                    **(
                        {
                            "env": {
                                **os.environ,
                                "AIH_LAUNCH_REASON": f"remote-inbox:{spec['bind_identity']}",
                            }
                        }
                        if spec.get("bind_identity")
                        else {}
                    ),
                )
            stamp = time.time()
            row = {
                **spec["record"],
                "row_kind": "started",
                "verdict": "started",
                "reason": "restart-started",
                "started_at": launch.iso(int(stamp)),
                "engine_pid": child.pid,
                "engine_pgid": child.pid,
                "engine_start_time": psutil.Process(child.pid).create_time(),
                "supervisor_pid": os.getpid(),
                "supervisor_start_time": _process_start_time(os.getpid()),
                "deadline": stamp + spec["bound_seconds"],
                "bound_seconds": spec["bound_seconds"],
                "log_path": str(job / "engine.log"),
            }
            write(job / "started.json", row)
            _append(job.parent.parent, row)
    except OSError as exc:
        if child is not None:
            terminate(row)
            child.wait()
            child = None
        row = complete(
            job,
            {
                "verdict": "refused",
                "exit": None,
                "reason": f"launch-failed ({type(exc).__name__})",
            },
        )
    _ready(job, row)
    if child is None:
        return
    try:
        code = child.wait(timeout=max(0, row["deadline"] - time.time()))
        outcome = {"exit": code, "verdict": "restarted" if code == 0 else "refused"}
    except subprocess.TimeoutExpired:
        outcome = {"exit": None, "verdict": "timed-out", "reason": "restart-timeout"}
    complete(job, outcome)
    child.wait()


if __name__ == "__main__":
    supervise(Path(sys.argv[1]))
