#!/usr/bin/env python3
"""Stop a Claude Code turn from ending into an idle session while background Bash work is live.

The hook bodies are intentionally thin.  This module owns the state machine so
the PreToolUse, PostToolUse, Stop, and SessionStart entry points cannot drift.
It always fails open when it cannot establish the live task registry.

Scope note (ISSUE-REF): the ISSUE-REF reap needs *two* conditions -- the turn ends
**and** the session then sits idle for roughly six minutes.  Gating on ``Stop``
is only appropriate for non-interactive sessions: ``Stop`` establishes the
first condition, but an interactive session can receive another prompt before
the second.  Claude Code persists that distinction in its session registry, so
this guard lets an identified interactive session end its current turn and
keeps the conservative block for headless or unknown metadata. When an
individual registry record is absent, it uses the invoking client's verified
interactive CLI mode as a narrow fallback. This module tracks background
**Bash** launches only; an ``Agent`` sub-agent is
covered by the procedure, not by this hook, and its supported hold is a
synchronous launch.

Completion (ISSUE-REF): a marker normally leaves ``running`` only in ``_stop``, so a task
that finished mid-turn kept being named pending on every PreToolUse.  ``_pre`` now also
reads the tail of the task's Claude Code output file
(``<tmp>/claude-<uid>/<cwd-slug>/<session>/tasks/<id>.output``) and, when it ends in the
client's ``[exited with code N]`` line, moves the marker to ``completed``/``failed``.
A task stopped with TaskStop ends in a ``[killed]`` line instead and moves to ``killed``
(ISSUE-REF: example-session's stopped watcher was named pending for 67 minutes of one turn).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

EXCEPTION_PREFIX = "AIH_BACKGROUND_EXCEPTION:"
# The ONLY remedy this guard recommends, and it is deliberately non-blocking
# (ISSUE-REF). The old text pointed at `TaskOutput(..., block=true)`, which the
# shipped client removed -- its own release notes read "Removed the deprecated
# TaskOutput tool; Claude reads a background task's output file with Read
# instead" (string in the installed 2.1.283 executable). Blocking was the wrong
# advice even while it worked: it freezes an attended session purely to keep a
# background shell alive. Read-and-poll keeps the turn alive on real work, and is
# also the vendor's own prescribed replacement.
NON_BLOCKING_REMEDY = (
    "Do not hold the turn open with a blocking wait. A background launch returns an "
    "output file path, so poll that file with a cheap Read between other work; for a "
    "command with no handle, append `> /tmp/<name>.log 2>&1` and poll that file the "
    "same way. See docs/procedures/background-agent-lifecycle.md."
)
TERMINAL = {"completed", "failed", "killed", "stopped", "cancelled", "canceled"}
LIVE = {"running", "pending", "in_progress", "background", "live"}
TERMINAL_TTL_SECONDS = 24 * 60 * 60
UNCONFIRMED_LAUNCH = {"provisional", "parse_failed"}
# A Bash PostToolUse hook normally promotes this within the same tool-call
# round-trip. Beyond this bound, an unconfirmed launch is an abandoned attempt
# rather than a task that can still be waited on.
PROVISIONAL_TTL_SECONDS = 5 * 60
MAX_STOP_BLOCKS = 8
MAX_AUDIT_BYTES = 256 * 1024
# One retry is the whole remedy for the prune race in `_atomic_replace`: the rival rmdirs
# only a directory it saw empty, and the retry re-creates it before writing again.
_ATOMIC_WRITE_ATTEMPTS = 2


def _warning(message: str) -> None:
    print(f"[background-wait-guard] {message}", file=sys.stderr)


def _safe_component(value: object, fallback: str = "unknown") -> str:
    if not isinstance(value, str) or not value:
        return fallback
    clean = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    if clean in {"", ".", ".."} or len(clean) > 96:
        return f"sha256-{hashlib.sha256(value.encode()).hexdigest()[:20]}"
    return clean


def _config_dir() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def _state_dir(session_id: object) -> Path:
    return _config_dir() / "state" / "background-wait" / _safe_component(session_id)


LEFTOVER_NOTICE_PREFIX = "leftover-process notice (agent-release-on-stop): "


def leftover_notice_dir(session_id: object) -> Path:
    """Where the SubagentStop hook queues leftover-process notices for one launcher (ISSUE-REF)."""
    return (
        _config_dir()
        / "state"
        / "leftover-process-notice"
        / _safe_component(session_id)
    )


def write_leftover_notice(session_id: str, agent_id: str, notice: str) -> None:
    """Queue ``notice`` for the launcher's next Bash PreToolUse; raises OSError on failure."""
    directory = leftover_notice_dir(session_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "session_id": session_id,
        "agent_id": agent_id,
        "notice": notice,
        "written_at": time.time(),
    }
    _write_and_rename(
        directory / f"{_safe_component(agent_id)}.json",
        json.dumps(payload, sort_keys=True) + "\n",
    )


def _is_subagent_call(payload: dict[str, Any]) -> bool:
    """A sub-agent's tool payload carries ``agent_id``/``agent_type``; the session's own does not.

    Measured shape: ``subagent_main_tree_guard.py`` (SUB-AGENT DETECTION). A sub-agent's
    calls share the launcher's ``session_id``, so without this a sibling agent, not the
    launching model, would receive the notice.
    """
    return any(
        isinstance(payload.get(key), str) and payload.get(key)
        for key in ("agent_id", "agent_type")
    )


def _claim_leftover_notices(payload: dict[str, Any]) -> list[str]:
    """Take every queued notice for this session, exactly once, by atomic rename.

    The rename is the claim: of two hooks racing for one file only one rename succeeds.
    A claimed file is always unlinked, so a malformed one is dropped with one warning
    instead of being re-delivered on every call.
    """
    session = payload.get("session_id")
    if not isinstance(session, str) or not session or _is_subagent_call(payload):
        return []
    directory = leftover_notice_dir(session)
    try:
        with os.scandir(directory) as entries:
            names = sorted(
                entry.name
                for entry in entries
                if entry.name.endswith(".json") and not entry.name.startswith(".")
            )
    except FileNotFoundError:
        return []
    except OSError as exc:
        _warning(f"leftover notices unreadable ({exc})")
        return []
    notices = [
        notice
        for name in names
        if (notice := _claim_one_notice(directory, name, session)) is not None
    ]
    try:
        directory.rmdir()
    except OSError:
        pass
    return notices


def _claim_one_notice(directory: Path, name: str, session: str) -> str | None:
    claimed = directory / f".claimed-{os.getpid()}-{name}"
    try:
        os.rename(directory / name, claimed)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _warning(f"could not claim leftover notice {name} ({exc})")
        return None
    try:
        value = json.loads(claimed.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = None
    finally:
        try:
            claimed.unlink()
        except OSError:
            pass
    notice = value.get("notice") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("session_id") != session
        or not isinstance(notice, str)
        or not notice
    ):
        _warning(f"discarded malformed leftover notice {name}")
        return None
    return LEFTOVER_NOTICE_PREFIX + notice


def _calling_claude_is_interactive() -> bool:
    """Return whether this hook's Claude ancestor proves an interactive CLI mode.

    Claude Code's Stop payload does not contain the invocation mode. This is a
    positive-only fallback for a missing individual registry record: the client
    must have a controlling terminal, identify as ``claude``, and lack every
    documented headless/background switch. Any failure remains conservative.
    """
    pid = os.getppid()
    seen: set[int] = set()
    for _ in range(12):
        if pid <= 1 or pid in seen:
            return False
        seen.add(pid)
        try:
            result = subprocess.run(
                ["ps", "-ww", "-o", "pid=,ppid=,tty=,command=", "-p", str(pid)],
                capture_output=True,
                check=False,
                text=True,
            )
        except OSError:
            return False
        if result.returncode != 0:
            return False
        fields = result.stdout.strip().split(maxsplit=3)
        if len(fields) != 4:
            return False
        try:
            parent_pid = int(fields[1])
        except ValueError:
            return False
        tty, command = fields[2], fields[3]
        try:
            args = shlex.split(command)
        except ValueError:
            return False
        if args and Path(args[0]).name == "claude":
            if tty in {"", "?", "??"}:
                return False
            return not any(
                argument in {"-p", "--print", "--bg", "--background"}
                or argument.startswith("--print=")
                for argument in args[1:]
            )
        pid = parent_pid
    return False


def _session_is_attended(session_id: object) -> bool:
    """Return whether a session has persisted interactive attendance evidence.

    The Stop hook payload has no mode field, but the client writes one JSON
    registry entry per active session under ``<config>/sessions``.  An exact
    ``kind: interactive`` match is attended.  Current Claude Code versions
    also write an interactive session's ``parkedJobId`` as the eight-character
    ID of its bridged ``kind: bg`` session.  Require both sides of that exact
    relation. If the registry has no record for this exact session, a verified
    interactive CLI ancestor is also attendance evidence; corrupt, unknown, or
    non-interactive matching metadata remains conservative.
    """
    if not isinstance(session_id, str) or not session_id:
        return False
    session_kind: str | None = None
    session_record_found = False
    session_has_bridge_session_id = False
    interactive_parked_job_ids: set[str] = set()
    sessions_dir = _config_dir() / "sessions"
    try:
        if not sessions_dir.is_dir():
            return False
        for path in sessions_dir.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            kind = value.get("kind")
            if value.get("sessionId") == session_id:
                session_record_found = True
                session_kind = kind if isinstance(kind, str) else None
                bridge_session_id = value.get("bridgeSessionId")
                session_has_bridge_session_id = isinstance(
                    bridge_session_id, str
                ) and bool(bridge_session_id)
            parked_job_id = value.get("parkedJobId")
            if kind == "interactive" and isinstance(parked_job_id, str):
                interactive_parked_job_ids.add(parked_job_id)
    except OSError:
        return False
    if not session_record_found:
        return _calling_claude_is_interactive()
    return (
        # A Stop payload from an interactive terminal is directly attended.
        session_kind == "interactive"
        # An interactive terminal can attend a bg job it parked by short ID.
        or (session_kind == "bg" and session_id[:8] in interactive_parked_job_ids)
        # Remote Control can instead attend the bg job through its own bridge.
        or (session_kind == "bg" and session_has_bridge_session_id)
    )


def _ensure_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    except OSError as exc:
        _warning(f"state unavailable ({exc}); enforcement is advisory only")
        return False
    return True


def _write_and_rename(path: Path, payload: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_replace(path: Path, payload: str, failure: str) -> bool:
    """Write and rename, re-creating a parent a rival pruned between the two steps.

    THE GAP THIS CLOSES, and the ones it does not. `_prune_empty_session_dir` rmdirs a
    session directory whose marker scan comes back empty, and that scan skips dot-prefixed
    names, so it cannot see the temp file `_write_and_rename` has in flight. A prune
    landing inside another writer's `_ensure_dir`/`mkstemp` gap made this raise ENOENT,
    which was downgraded to an advisory warning and a False return -- and `_pre` and
    `_post_bash` treat that as benign and exit 0, so a launch record vanished with a clean
    exit status (ISSUE-REF, the 15-of-16 marker signature).

    This is a RIVAL-PROCESS failure, so retrying after re-creating the parent is the
    mechanism that matches it: the rival's rmdir cannot win twice in a row against a
    writer that immediately re-creates what it needs. It deliberately covers nothing else.
    Being killed between mkstemp and os.replace still loses the write and orphans the temp
    file -- a lock would not help either, since a dying process releases it, and only a
    write-ahead journal would survive SIGKILL. That gap stays open.
    """
    last: OSError | None = None
    for _ in range(_ATOMIC_WRITE_ATTEMPTS):
        if not _ensure_dir(path.parent):
            return False
        try:
            _write_and_rename(path, payload)
        except OSError as exc:
            last = exc
            continue
        return True
    _warning(failure.format(exc=last))
    return False


def _atomic_json(path: Path, value: dict[str, Any]) -> bool:
    return _atomic_replace(
        path,
        json.dumps(value, sort_keys=True) + "\n",
        "state write failed ({exc}); enforcement is advisory only",
    )


def _atomic_text(path: Path, value: str) -> bool:
    return _atomic_replace(path, value, "audit compaction failed ({exc})")


def _read_marker(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        _warning(f"ignoring corrupt marker {path.name}: {exc}")
        return None
    return value if isinstance(value, dict) else None


def _markers(session_id: object) -> list[tuple[Path, dict[str, Any]]]:
    directory = _state_dir(session_id)
    try:
        paths = [*directory.glob("provisional-*.json"), *directory.glob("task-*.json")]
    except OSError as exc:
        _warning(f"state unreadable ({exc}); enforcement is advisory only")
        return []
    found: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        marker = _read_marker(path)
        if marker is not None:
            found.append((path, marker))
    return found


def _audit(session_id: object, event: str, **details: object) -> None:
    root = _config_dir() / "state" / "background-wait"
    if not _ensure_dir(root):
        return
    entry = {
        "at": time.time(),
        "event": event,
        "session": _safe_component(session_id),
        **details,
    }
    path = root / "audit.log"
    try:
        with path.open("a", encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
        if path.stat().st_size > MAX_AUDIT_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()
            retained = "\n".join(lines[-1000:]) + "\n"
            _atomic_text(path, retained)
    except OSError as exc:
        _warning(f"audit write failed ({exc})")


def _cleanup_terminal(session_id: object) -> None:
    now = time.time()
    terminal_cutoff = now - TERMINAL_TTL_SECONDS
    provisional_cutoff = now - PROVISIONAL_TTL_SECONDS
    for path, marker in _markers(session_id):
        status = marker.get("status")
        expired_terminal = (
            status in TERMINAL and marker.get("updated_at", 0) < terminal_cutoff
        )
        expired_unconfirmed_launch = (
            status in UNCONFIRMED_LAUNCH
            and marker.get("created_at", 0) < provisional_cutoff
        )
        if expired_terminal or expired_unconfirmed_launch:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                _warning(f"stale marker cleanup failed ({exc})")
    _prune_empty_session_dir(_state_dir(session_id))


def _direct_marker_paths(directory: Path) -> list[Path]:
    """Return regular marker files directly inside one already-selected session."""
    try:
        with os.scandir(directory) as entries:
            return [
                Path(entry.path)
                for entry in entries
                if entry.is_file(follow_symlinks=False)
                and (
                    entry.name.startswith("task-")
                    or entry.name.startswith("provisional-")
                )
                and entry.name.endswith(".json")
            ]
    except FileNotFoundError:
        return []
    except OSError as exc:
        _warning(f"state unreadable ({exc}); enforcement is advisory only")
        return []


def _prune_empty_session_dir(directory: Path) -> None:
    """Remove obsolete per-session bookkeeping without disturbing markers."""
    if directory.is_symlink() or _direct_marker_paths(directory):
        return
    state_path = directory / "stop-state.json"
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _warning(f"stale stop-state cleanup failed ({exc})")
        return
    try:
        directory.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        # A concurrent hook may have written a marker after the empty check.
        pass


def _cleanup_all_terminal() -> None:
    """Sweep expired terminal markers without ever touching other-session live state."""
    root = _config_dir() / "state" / "background-wait"
    try:
        with os.scandir(root) as entries:
            sessions = [
                Path(entry.path)
                for entry in entries
                if entry.is_dir(follow_symlinks=False)
            ]
    except FileNotFoundError:
        return
    except OSError as exc:
        _warning(f"state unreadable ({exc}); enforcement is advisory only")
        return

    terminal_cutoff = time.time() - TERMINAL_TTL_SECONDS
    for directory in sessions:
        for path in _direct_marker_paths(directory):
            # Re-read immediately before unlinking: another hook may have
            # replaced the marker since the directory scan.
            marker = _read_marker(path)
            if (
                marker is None
                or marker.get("status") not in TERMINAL
                or marker.get("updated_at", 0) >= terminal_cutoff
            ):
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                _warning(f"stale marker cleanup failed ({exc})")
        _prune_empty_session_dir(directory)


def _load_payload() -> dict[str, Any] | None:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        _warning(f"malformed hook JSON; allowing: {exc}")
        return None
    if not isinstance(value, dict):
        _warning("hook JSON was not an object; allowing")
        return None
    return value


def _background_id(response: object) -> str | None:
    """Adapter for Claude Code 2.1.220's real Bash response shape.

    The installed 2.1.220 binary's Bash handler returns
    ``{stdout, stderr, code, interrupted, backgroundTaskId}`` for an explicit
    background launch.  The alternate spellings make a future documented
    adapter addition local without accepting arbitrary nested values.
    """
    if not isinstance(response, dict):
        return None
    for key in ("backgroundTaskId", "background_task_id", "task_id"):
        value = response.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _post_tool_calls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the direct and 2.1.220 batched PostToolUse envelopes."""
    calls = payload.get("tool_calls")
    if isinstance(calls, list):
        normalized: list[dict[str, Any]] = []
        for call in calls:
            if isinstance(call, dict):
                normalized.append({"session_id": payload.get("session_id"), **call})
        return normalized
    return [payload]


def _task_id(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("task_id", "id", "backgroundTaskId", "background_task_id"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _status(value: object) -> str | None:
    if isinstance(value, dict):
        for key in ("status", "state"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate.lower()
    return None


def _marker_path(directory: Path, task_id: str | None, tool_use_id: object) -> Path:
    prefix = "task" if task_id else "provisional"
    key = task_id if task_id else tool_use_id
    return directory / f"{prefix}-{_safe_component(key)}.json"


def _write_hook_context(event: str, message: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": message,
                }
            }
        )
    )


def _pending(session_id: object) -> list[tuple[Path, dict[str, Any]]]:
    return [
        (path, marker)
        for path, marker in _markers(session_id)
        if not marker.get("exempt") and marker.get("status") not in TERMINAL
    ]


_EXIT_LINE = re.compile(r"\[exited with code (-?\d+)\]\s*\Z")
_KILLED_LINE = re.compile(r"(?:\A|\n)\[killed\]\s*\Z")
_OUTPUT_TAIL_BYTES = 512


def _task_output_file(session_id: object, task_id: str) -> Path | None:
    """Locate Claude Code's ``<tmp>/claude-<uid>/<cwd-slug>/<session>/tasks/<id>.output``.

    The cwd slug is not in any hook payload, so it is globbed rather than rebuilt: the
    session uuid and task id are the identifying parts and are unique.
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    roots = [os.environ.get("CLAUDE_CODE_TMPDIR") or "/tmp"]
    if "CLAUDE_CODE_TMPDIR" not in os.environ:
        roots.append("/private/tmp")
    session, task = _safe_component(session_id), _safe_component(task_id)
    for root in roots:
        base = Path(root) / f"claude-{os.getuid()}"
        try:
            for candidate in base.glob(f"*/{session}/tasks/{task}.output"):
                return candidate
        except OSError:
            continue
    return None


def _finished_status(output: Path) -> str | None:
    """``completed``/``failed``/``killed`` when the output file ends in the client's final line."""
    try:
        with output.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - _OUTPUT_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    if _KILLED_LINE.search(tail):
        return "killed"
    match = _EXIT_LINE.search(tail)
    if match is None:
        return None
    return "completed" if int(match.group(1)) == 0 else "failed"


def _reconcile_finished(session_id: object) -> None:
    """Move a marker to a terminal status once its task's output file shows it exited.

    Only ``_stop`` used to do this, so for the rest of a turn after a completion
    notification every PreToolUse kept naming the finished task as pending (ISSUE-REF).
    """
    for path, marker in _pending(session_id):
        task_id = marker.get("task_id")
        if not isinstance(task_id, str):
            continue
        output = _task_output_file(session_id, task_id)
        status = _finished_status(output) if output else None
        if status:
            marker.update({"status": status, "updated_at": time.time()})
            _atomic_json(path, marker)
            _audit(session_id, "pre_observed_exit", task_id=task_id, status=status)


def _pre(payload: dict[str, Any]) -> None:
    nudge = _pre_state(payload)
    messages = [*_claim_leftover_notices(payload), *([nudge] if nudge else [])]
    if messages:
        _write_hook_context("PreToolUse", "\n\n".join(messages))


def _pre_state(payload: dict[str, Any]) -> str | None:
    session = payload.get("session_id")
    _cleanup_terminal(session)
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    if tool_name == "Bash" and tool_input.get("run_in_background") is True:
        description = tool_input.get("description")
        exception_reason: str | None = None
        if isinstance(description, str) and description.startswith(EXCEPTION_PREFIX):
            candidate = description[len(EXCEPTION_PREFIX) :].strip()
            if candidate:
                exception_reason = candidate
            else:
                _warning(
                    "exception prefix requires a non-empty reason; enforcing launch"
                )
                _audit(
                    session,
                    "exception_rejected",
                    tool_use_id=payload.get("tool_use_id"),
                )
        if exception_reason:
            _audit(
                session,
                "exception",
                reason=exception_reason,
                tool_use_id=payload.get("tool_use_id"),
            )
            return None
        command = tool_input.get("command")
        launched_at = time.time()
        marker = {
            "tool_use_id": payload.get("tool_use_id"),
            "created_at": launched_at,
            "launch_timestamp": launched_at,
            "updated_at": launched_at,
            "status": "provisional",
            "description": description if isinstance(description, str) else "",
            "command_sha256": hashlib.sha256(
                command.encode() if isinstance(command, str) else b""
            ).hexdigest(),
        }
        _atomic_json(
            _marker_path(_state_dir(session), None, payload.get("tool_use_id")), marker
        )
        return None

    _reconcile_finished(session)
    pending = _pending(session)
    if not pending:
        return None
    ids = ", ".join(str(marker.get("task_id", "unresolved")) for _, marker in pending)
    return (
        "background-wait-guard: background task(s) still pending: "
        f"{ids}. Keep doing same-turn work; in a live interactive session, reflexive "
        f"blocking is a defect, not caution. {NON_BLOCKING_REMEDY}"
    )


def _post_bash(payload: dict[str, Any]) -> None:
    for call in _post_tool_calls(payload):
        tool_input = call.get("tool_input")
        if call.get("tool_name") != "Bash" or not isinstance(tool_input, dict):
            continue
        if tool_input.get("run_in_background") is not True:
            continue
        session = call.get("session_id")
        tool_use_id = call.get("tool_use_id")
        provisional = _marker_path(_state_dir(session), None, tool_use_id)
        marker = _read_marker(provisional)
        if marker is None:
            _warning(
                "background launch had no provisional marker; allowing but audit required"
            )
            _audit(session, "missing_provisional", tool_use_id=tool_use_id)
            continue
        task_id = _background_id(call.get("tool_response"))
        if task_id is None:
            marker["status"] = "parse_failed"
            marker["updated_at"] = time.time()
            _atomic_json(provisional, marker)
            _warning(
                "could not parse Bash backgroundTaskId; provisional marker retained"
            )
            _audit(session, "response_parse_failed", tool_use_id=tool_use_id)
            continue
        marker.update(
            {"task_id": task_id, "status": "running", "updated_at": time.time()}
        )
        target = _marker_path(_state_dir(session), task_id, tool_use_id)
        if _atomic_json(target, marker):
            try:
                provisional.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                _warning(f"could not remove provisional marker ({exc})")


def _clear_stop_state(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _warning(f"stop-state cleanup failed ({exc})")


def _stop(payload: dict[str, Any]) -> None:
    session = payload.get("session_id")
    _cleanup_terminal(session)
    registry = payload.get("background_tasks")
    if not isinstance(registry, list):
        _warning("background_tasks registry absent or malformed; fail-open")
        _audit(session, "registry_unavailable")
        return
    tasks = {task_id: item for item in registry if (task_id := _task_id(item))}
    live: list[str] = []
    for path, marker in _pending(session):
        task_id = marker.get("task_id")
        if not isinstance(task_id, str):
            continue
        observed = tasks.get(task_id)
        state = _status(observed)
        if observed is None or state in TERMINAL:
            marker.update({"status": state or "completed", "updated_at": time.time()})
            _atomic_json(path, marker)
            _audit(session, "ordering_violation", task_id=task_id)
        else:
            # The registry is documented as live-only.  Unknown state in a
            # matching entry is therefore conservatively live.
            live.append(task_id)
    live = sorted(set(live))
    state_path = _state_dir(session) / "stop-state.json"
    if not live:
        _clear_stop_state(state_path)
        return
    if payload.get("stop_hook_active") is True:
        # Claude Code marks a recursive Stop invocation so hooks can return
        # success instead of repeatedly blocking their own prior block.
        _clear_stop_state(state_path)
        _audit(session, "stop_allow_active_hook", task_ids=live)
        return
    if _session_is_attended(session):
        _clear_stop_state(state_path)
        _audit(session, "stop_allow_interactive", task_ids=live)
        return
    state = _read_marker(state_path) or {}
    count = state.get("blocks", 0)
    if not isinstance(count, int) or state.get("live_task_ids") != live:
        count = 0
    if count >= MAX_STOP_BLOCKS:
        _warning("Stop block ceiling reached; allowing after live-state recheck")
        _audit(session, "stop_block_ceiling", task_ids=live)
        return
    _atomic_json(
        state_path,
        {"blocks": count + 1, "live_task_ids": live, "updated_at": time.time()},
    )
    _audit(
        session,
        "stop_block",
        task_ids=live,
        stop_hook_active=payload.get("stop_hook_active"),
    )
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": "background-wait-guard: task "
                f"{live[0]} is still live and this turn is ending. Keep the turn active with "
                f"real same-turn work: the next in-progress task, then your own worktrees and "
                f"the fleet's for merge or proved reap. {NON_BLOCKING_REMEDY} For an Agent child, read its "
                "declared evidence file between steps; an interactive session has no synchronous "
                "Agent launch. If you are mid-conversation and about to keep working, the reap "
                "needs an idle session too.",
            }
        )
    )


def _session_start(payload: dict[str, Any]) -> None:
    session = payload.get("session_id")
    _cleanup_terminal(session)
    _cleanup_all_terminal()
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not isinstance(session, str) or not session or not env_file:
        return
    try:
        _persist_session_id(Path(env_file), session)
    except OSError as exc:
        _warning(f"could not persist heavy-test session ID ({exc})")


_SESSION_EXPORT_PREFIX = "export AI_HARNESS_SESSION_ID="
# Claude Code gives each SessionStart hook its own env file, named for the hook's position
# in the event's hook list, keeps it across events, and sources every file in the session's
# directory. Measured 2026-10-10: this hook's export sat in sessionstart-hook-11..21.sh of
# one session as the manifest grew, and a stale file ending in another session's id would
# win whenever it sorts after the live one (ISSUE-REF).
_SIBLING_ENV_FILE = re.compile(r"sessionstart-hook-\d+\.sh")


def _persist_session_id(env_file: Path, session: str) -> None:
    """Leave exactly one session-id export across the session's env files: this id, here.

    SessionStart fires on startup, resume, clear and compact, so a blind append grew one
    line per event and kept any other session's id (ISSUE-REF). The common case -- this
    file already holds exactly this line -- writes nothing. A file with no export gets a
    plain append, so a concurrent append to it is never lost. A stale or duplicated export
    forces a rewrite through write-temp + rename, with every other line byte-identical.
    """
    desired = f"{_SESSION_EXPORT_PREFIX}{shlex.quote(session)}\n"
    _rewrite_session_exports(env_file, desired)
    if _SIBLING_ENV_FILE.fullmatch(env_file.name):
        for sibling in sorted(env_file.parent.iterdir()):
            if sibling != env_file and _SIBLING_ENV_FILE.fullmatch(sibling.name):
                _rewrite_session_exports(sibling, None)


def _rewrite_session_exports(path: Path, desired: str | None) -> None:
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if desired is None:
            return
        current = ""
    lines = current.splitlines(keepends=True)
    exports = [line for line in lines if line.startswith(_SESSION_EXPORT_PREFIX)]
    if exports == ([desired] if desired else []):
        return
    if not exports:
        separator = "" if not current or current.endswith("\n") else "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(separator + desired)
        return
    kept = [line for line in lines if not line.startswith(_SESSION_EXPORT_PREFIX)]
    if desired and kept and not kept[-1].endswith("\n"):
        kept[-1] += "\n"
    _write_and_rename(path, "".join(kept) + (desired or ""))


def main() -> None:
    payload = _load_payload()
    if payload is None:
        return
    event = os.environ.get("BACKGROUND_WAIT_EVENT", "")
    try:
        if event == "pre":
            _pre(payload)
        elif event == "post-bash":
            _post_bash(payload)
        elif event == "stop":
            _stop(payload)
        elif event == "session-start":
            _session_start(payload)
    except Exception as exc:  # noqa: BLE001 - a hook must never wedge every session.
        _warning(f"internal error; allowing: {exc}")


if __name__ == "__main__":
    main()
