"""Restart a settled Claude Code session onto an installed update with background handoff.

It rides the goal waker's timer (``goal_waker.tick`` calls :func:`tick`), so it runs on
every host that timer is installed on. For each live session it restarts only when every
gate below holds, in this order, and logs the first that fails:

* the native launcher ``~/.local/bin/claude`` names a version newer than the one the
  session's registry record says it is running (an npm-global install, a Windows
  ``claude.exe`` or an unreadable link is ``launcher-unknown``: nothing is restarted);
* Claude Code's registry reports an idle main turn, a background shell, or a busy
  session with a background sub-agent, with no ``waitingFor``, and the
  transcript shows a finished turn quiet for ``idle_after_seconds`` (the goal waker's
  :func:`~ai_chassis.goal_waker.idle_state`);
* its background state can be read; shells and sub-agents do not block;
* no goal-waker nudge is pending or held for it, and the context supervisor has no
  save-state or post-compact wake in flight for it;
* its pane is hosted by the ai c wrapper (the pane's start command is
  ``<XDG_STATE_HOME>/ai-cli-utils/sessions/<tmux session>.sh``), so ``/exit`` relaunches
  ``claude --resume`` instead of ending the session;
* it was not already tried for this target version within ``retry_after_seconds``.

The restart extends the injection library's synchronous ``session-restart`` use case.
Before sending ``/exit`` it writes a pending detached record with the session's name and cwd.
The dialog's own move row supplies the digit, after its stop list is saved in ``labels``.
Unknown kinds cancel the dialog and wait; capped retries report their verbatim labels.
Stopped items remain unconfirmed until readable post-exit evidence arrives. The launcher's
``finish-background`` call adds the new background id and stop evidence before attach.
The live config ``~/.claude/state/update-restart-config.json`` is
optional and overrides ``defaults/update-restart-config.json`` key by key. This machine's
``[profiles.<p>]`` table in ``config/update_restart_profiles.toml`` wins over both (ISSUE-REF).
The public restart command's stable per-tmux-session lock is held from revalidation
through replacement verification, with a shared durable attempt recorded before injection.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import secrets
import subprocess
import sys
import time
import tomllib
import types
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_chassis import context_supervisor as supervisor
from ai_chassis import detached_ledger, goal_waker, launch_resume, machine
from ai_chassis.delegation import bug_fix_delegation
from ai_chassis.generations import write_json_fsync
from ai_chassis.prompt_injection import capability, observe
from ai_chassis.prompt_injection import injector as restart_injection
from ai_chassis.prompt_injection.injector import SessionRestartInjector

DEFAULT_CONFIG_PATH = goal_waker.DEFAULTS_DIR / "update-restart-config.json"
CONFIG_PATH_ENV = "AIH_UPDATE_RESTART_CONFIG_PATH"
PROFILES_PATH = goal_waker.REPO_ROOT / "config" / "update_restart_profiles.toml"
PROFILES_PATH_ENV = "AIH_UPDATE_RESTART_PROFILES_PATH"
_VERSION = re.compile(r"^\d+(?:\.\d+)+$")
#: A session writes these notices after its turn has ended ("A hook blocked the turn from
#: ending 9 consecutive times", "Goal paused"), so for a restart the registry's idle status
#: plus the settle window decide, not the notice (ISSUE-REF R-4).
_NOTICE_TURN_ENDS = frozenset({"informational"})
#: Per-installed-version record that a refusal notice went out, and its re-probe state.
_NOTICE_PREFIX = "refusal-notice-"
#: Attempt-record keys a later attempt record keeps: when the session went stale, and the
#: target a window notice already went out for.
_KEPT_ACROSS_ATTEMPTS = ("stale", "window_noticed", "unknown_kind")
_SCRIPTS = goal_waker.REPO_ROOT / "scripts"
_MIN_EXIT_DIALOG_LINES = 6


def _log_quote(value: Any) -> str:
    encoded = goal_waker._quote(value)
    return (
        json.dumps(str(value), ensure_ascii=False)
        if encoded.startswith('"')
        else encoded
    )


def live_config_path() -> Path:
    override = os.environ.get(CONFIG_PATH_ENV)
    if override:
        return Path(override)
    return Path.home() / ".claude" / "state" / "update-restart-config.json"


def profiles_path() -> Path:
    override = os.environ.get(PROFILES_PATH_ENV)
    return Path(override) if override else PROFILES_PATH


def _machine() -> str | None:
    try:
        return machine.resolve_machine_identity(())[0]
    except (machine.MachineDetectionError, OSError):
        return None


def _profile_tables(document: dict[str, Any]) -> dict[str, dict[str, Any]] | str:
    """Every override table in the file by label, or the shape error that voids the file.

    The labels are ``default`` and ``profiles.<p>``. Validated whole, before this machine's
    table is chosen, so a typo anywhere (``[profile.x]``, ``profiles = []``) fails closed on
    every machine rather than leaving the one it misnames restarting.
    """
    if unknown := sorted(set(document) - {"default", "profiles"}):
        return f"profiles-unknown-table: {unknown[0]}"
    profiles = document.get("profiles", {})
    if not isinstance(profiles, dict):
        return "profiles-not-a-table: profiles"
    tables = {"default": document.get("default", {})} | {
        f"profiles.{name}": table for name, table in profiles.items()
    }
    for label, table in tables.items():
        if not isinstance(table, dict):
            return f"profiles-not-a-table: {label}"
    return tables


def _profile_overrides(defaults: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """This machine's ``[profiles.<p>]`` table, else ``[default]``, read at call time
    (ISSUE-REF).

    ``[default]`` also covers a machine whose identity cannot be resolved, or resolves to a
    name with no table (an alias, a new profile), so an off switch never fails open. A
    missing file overrides nothing. A file that cannot be read or parsed, a misshapen table,
    or a key outside the shipped config or of the wrong type, is an error: the caller turns
    the pass off.
    """
    try:
        document = tomllib.loads(profiles_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except (OSError, tomllib.TOMLDecodeError) as error:
        return {}, f"profiles-unreadable: {error}"
    tables = _profile_tables(document)
    if isinstance(tables, str):
        return {}, tables
    for label, table in tables.items():
        for key, value in table.items():
            if key not in defaults:
                return {}, f"profiles-unknown-key: {label}.{key}"
            if type(value) is not type(defaults[key]):
                return {}, f"profiles-bad-type: {label}.{key}"
    name = _machine()
    label = f"profiles.{name}" if name and f"profiles.{name}" in tables else "default"
    table = tables[label]
    source = name if label != "default" else f"default ({name or 'machine unresolved'})"
    overrides = (
        {**table, "_profile": source, "_profile_keys": sorted(table)} if table else {}
    )
    return overrides, None


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Defaults, overridden key by key by the live file, then by this machine's profile
    table, which wins over both; a bad live file or profile file disables the pass."""
    config = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    profile, profile_error = _profile_overrides(config)
    if profile_error:
        return {**config, "enabled": False, "_error": profile_error}
    path = live_config_path() if path is None else path
    try:
        live = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {**config, **profile, "_error": None}
    except (OSError, json.JSONDecodeError) as error:
        return {**config, "enabled": False, "_error": f"config-unreadable: {error}"}
    if not isinstance(live, dict):
        return {**config, "enabled": False, "_error": "config-not-an-object"}
    for key, value in live.items():
        if key in config and type(value) is not type(config[key]):
            return {**config, "enabled": False, "_error": f"config-bad-type: {key}"}
    return {**config, **live, **profile, "_error": None}


def _disabled_line(config: dict[str, Any]) -> str:
    if config["_error"]:
        return f"update restart disabled: {config['_error']}"
    if "enabled" in config.get("_profile_keys", ()):
        return f"update restart disabled by machine profile {config['_profile']}"
    return "update restart disabled by the live config"


def _version(text: object) -> tuple[int, ...] | None:
    if not isinstance(text, str) or not _VERSION.match(text):
        return None
    return tuple(int(part) for part in text.split("."))


def installed_version(home: Path | None = None) -> str | None:
    """The version a new session starts on: the native launcher's link target's name.

    Anthropic documents ``~/.local/bin/claude`` as the native installer's managed link into
    ``~/.local/share/claude/versions/``. Anything else is not that install, so None.
    """
    home = Path.home() if home is None else home
    launcher = home / ".local" / "bin" / "claude"
    try:
        target = Path(os.readlink(launcher))
    except OSError:
        return None
    if not target.is_absolute():
        target = launcher.parent / target
    versions = home / ".local" / "share" / "claude" / "versions"
    if target.parent.resolve() != versions.resolve() or not target.is_file():
        return None
    return target.name if _version(target.name) else None


def _contention() -> types.ModuleType:
    """``scripts/machine_contention.py``, whose sibling imports need ``scripts`` on the path
    while it loads; the path entry is removed again once it has."""
    cached = sys.modules.get("machine_contention")
    if cached is not None:
        return cached
    sys.path.insert(0, str(_SCRIPTS))
    try:
        import machine_contention  # noqa: PLC0415 - loaded only when a session is stale
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(str(_SCRIPTS))
    return machine_contention


def state_dir() -> Path:
    return goal_waker.state_dir().parent / "update-restart"


def _attempt_record(result: Any) -> dict[str, Any]:
    """The attempt record the injection run left (empty when it left none)."""
    evidence = getattr(result, "evidence", None)
    path = evidence.get("attempt") if isinstance(evidence, dict) else None
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _refusal_detail(result: Any) -> str | None:
    """The refusal detail the attempt record carries (``refusal_detail``), on one line."""
    detail = _attempt_record(result).get("refusal_detail")
    if not isinstance(detail, str) or not detail.strip():
        return None
    return " ".join(detail.split())[:400]


def _age(seconds: float) -> str:
    """A span as days, hours and minutes, for the notice and the status listing."""
    minutes = max(0, int(seconds // 60))
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return " ".join(
        part
        for part in (
            f"{days}d" if days else "",
            f"{hours}h" if hours else "",
            f"{minutes}m",
        )
        if part
    )


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pane(record: dict[str, Any]) -> tuple[str, str] | None:
    """``(tmux session, pane id)`` from the registry's ``session:@window.%pane`` field."""
    value = record.get("tmux")
    if not isinstance(value, str) or ":" not in value or "." not in value:
        return None
    session, _, rest = value.partition(":")
    pane = rest.rpartition(".")[2]
    if not session or not pane.startswith("%"):
        return None
    return session, pane


def _agent_rows() -> list[dict[str, Any]]:
    """Read the live inventory without including subprocess output in errors."""
    _contention()  # Loads the watchdog's shared process_liveness sibling.
    rows, error = goal_waker._script("dead_session_watchdog").call_claude_agents()
    if error:
        raise ValueError("agents inventory unavailable")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("agents inventory malformed")
    return rows


def _background_result(capture: str) -> tuple[str, list[str]]:
    """Read the newest backgrounded block; a pane reused across restarts keeps older ones."""
    lines = [line.strip() for line in capture.splitlines()]
    marks = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(r"backgrounded · [0-9a-f]{8}", line)
    ]
    if not marks:
        raise ValueError("backgrounded id missing")
    stopped = [
        line
        for line in lines[marks[-1] + 1 :]
        if re.fullmatch(r"\d+ .+ couldn['’]t be moved and was stopped\.?", line)
    ]
    return lines[marks[-1]].rpartition(" ")[2], stopped


def finish_background(key: str, capture_path: Path) -> int:
    """Complete the pending handoff before the launcher attaches; ambiguity writes nothing."""
    try:
        rec = detached_ledger.load(key)
        if rec is None or not rec.get("name") or not rec.get("cwd"):
            raise ValueError("handoff identity unavailable")
        shown_id, stopped = _background_result(capture_path.read_text(encoding="utf-8"))
        # The pane printed the new copy's id; an older finished copy may hold the bare name,
        # so the copy is the one row with that id that is this session's, name or name (N).
        matches = [
            row
            for row in _agent_rows()
            if row.get("id") == shown_id
            and detached_ledger.is_moved_copy(row, rec["name"], rec["cwd"])
        ]
        if len(matches) != 1:
            raise ValueError("background identity missing, ambiguous or mismatched")
        detached_ledger.update(
            key,
            {
                "background_session": matches[0]["id"],
                "stopped_items": stopped,
                "stopped_items_confirmed": True,
                "unproven": False,
            },
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        print(
            f"UR-RESTART-WARN session={key} reason=background-unproven", file=sys.stderr
        )
        return 1
    # A launcher reads stdout as the answer, so it carries the verified id alone.
    for item in stopped:
        print(
            f"UR-STOPPED session={key} item={goal_waker._quote(item)}", file=sys.stderr
        )
    print(shown_id)
    return 0


_DIALOG_HEADINGS = ("Background work is running", "Background this session?")
_DIALOG_FOOTER = "Enter to confirm · Esc to cancel"
# With no footer to end it, this cap is what bounds the text logged.
_DIALOG_LOG_LINES = 14
_DIALOG_LOG_WIDTH = 160


def _dialog_text(capture: str) -> str:
    """The dialog's own lines for the log, its heading to its footer and nothing below.

    A dialog the restart does not recognise is cancelled, and the first sighting of a
    changed one is the only evidence of what it said. The pane below the footer is not
    the dialog's, so it is never logged.
    """
    lines = [
        line.strip()
        for line in capture.splitlines()
        if line.strip() and not re.fullmatch(r"[─-▟\s]+", line)
    ]
    starts = [i for i, line in enumerate(lines) if line in _DIALOG_HEADINGS]
    if not starts:
        return ""
    lines = lines[starts[-1] :]
    if _DIALOG_FOOTER in lines:
        lines = lines[: lines.index(_DIALOG_FOOTER) + 1]
    return " | ".join(line[:_DIALOG_LOG_WIDTH] for line in lines[:_DIALOG_LOG_LINES])


# The kinds the restart can move: each keeps running in the background session.
_MOVABLE_KINDS = frozenset({"shell", "subagent", detached_ledger.SCHEDULED_KIND})


def _wrapped_scheduled(previous: str, line: str) -> str | None:
    """The one row a narrow pane drew over two lines: "scheduled ... · text" then "task tail".

    Only that exact wrap is joined. Any other line with no kind separator stays its own
    row, so a kind this restart does not know is refused rather than absorbed into a row
    it can move.
    """
    kind, separator, rest = previous.partition("·")
    words = line.split()
    if (
        not separator
        or "·" in line
        or " ".join(kind.split()) != "scheduled"
        or not words
        or words[0] != "task"
    ):
        return None
    tail = " ".join(words[1:])
    return f"{detached_ledger.SCHEDULED_KIND} ·{rest.rstrip()} {tail}".rstrip()


def _exit_rows(capture: str) -> tuple[str, list[str]]:
    """Validate the live dialog as a whole before returning any actionable digit."""
    lines = [
        line.strip()
        for line in capture.splitlines()
        if line.strip() and not re.fullmatch(r"[─-▟\s]+", line)
    ]
    footer = "Enter to confirm · Esc to cancel"
    try:
        start = len(lines) - 1 - lines[::-1].index("Background work is running")
        # The dialog ends at its own footer, not at the end of the capture: a live pane
        # can draw the composer or a status line below it.
        lines = lines[start : start + lines[start:].index(footer) + 1]
    except ValueError:
        raise ValueError("exit-dialog-unidentified") from None
    if (
        len(lines) < _MIN_EXIT_DIALOG_LINES
        or lines[1] != "The following will stop when you exit:"
    ):
        raise ValueError("exit-dialog-unidentified")
    labels, rows = [], {}
    focused = 0
    for line in lines[2:-1]:
        row = re.fullmatch(r"(❯ )?([1-9])\. (.+)", line)
        if row:
            focus, digit, label = row.groups()
            if digit in rows or label in rows.values():
                raise ValueError("exit-dialog-ambiguous")
            rows[digit] = label
            focused += bool(focus)
        elif not rows:
            # Every row above the digit rows is listed as printed, so a kind this
            # restart does not know is logged and refused as an unknown kind instead
            # of making the whole dialog unidentified.
            joined = _wrapped_scheduled(labels[-1], line) if labels else None
            if joined is None:
                labels.append(line)
            else:
                labels[-1] = joined
        else:
            raise ValueError("exit-dialog-unidentified")
    if focused != 1 or set(rows.values()) != {
        "Exit and stop tasks",
        "Move to background and exit",
        "Stay",
    }:
        raise ValueError("move-row-missing-or-unidentified")
    return next(
        digit for digit, label in rows.items() if label == "Move to background and exit"
    ), labels


class _BackgroundRestartInjector(SessionRestartInjector):
    """Reuse the restart's readiness, pane lease and oracle, changing only its Stay handler."""

    def __init__(self, restart_pass: _Pass, key: str, **kwargs):
        super().__init__(exit_dialog="stay", **kwargs)
        self.restart_pass = restart_pass
        self.key = key

    def _capture(self, target):
        capture = self.transport.capture(target)
        if not capture.strip():
            raise OSError("pane-unreadable")
        return capture

    def _submit(self, run, payload, timeout):
        self._capture(run.target)  # No /exit when the pane cannot be read.
        return super()._submit(run, payload, timeout)

    def _dialog_open(self, target):
        try:
            capture = self._capture(target)
        except OSError:
            self.transport.press(target, "Escape")
            self._drop_prompt()
            raise
        return (
            "Background work is running" in capture
            or "Background this session?" in capture
        ) and not self._composer_drawn(target)

    def _cancel(self, run, reason):
        pressed = self.transport.press(run.target, "Escape")
        self._drop_prompt()
        return self._refuse(run, reason, f"escape={pressed}")

    def _log_dialog(self, capture, reason):
        text = _dialog_text(capture)
        if text:
            self.restart_pass.emit(
                "UR-RESTART-WARN", session=self.key, reason=reason, dialog=text
            )

    def _stay(self, run, target):
        capture = ""
        try:
            capture = self._capture(target)
            digit, labels = _exit_rows(capture)
        except (OSError, ValueError) as error:
            self._log_dialog(capture, str(error))
            return self._cancel(run, str(error))
        for item in labels:
            self.restart_pass.emit("UR-STOP-LIST", session=self.key, item=item)
        unknown = next(
            (
                item
                for item in labels
                if detached_ledger.stop_kind(item) not in _MOVABLE_KINDS
            ),
            None,
        )
        if unknown is not None:
            return self._cancel(run, f"unknown-kind:{unknown}")
        try:
            detached_ledger.update(
                self.key,
                {
                    "labels": labels,
                    "stopped_items": [],
                    "stopped_items_confirmed": False,
                },
            )
        except (OSError, ValueError):
            return self._cancel(run, "ledger-unwritable")
        if self.transport.press(target, digit) != "succeeded":
            return self._cancel(run, "move-key-failed")
        return self._await_exit(run, target)

    def _await_exit(self, run, target):
        deadline = time.monotonic() + self.exit_wait
        while time.monotonic() < deadline:
            if not any(
                restart_injection._alive(pid) for pid in run.record["restart_old_pids"]
            ):
                self._stopped_items(target)
                return restart_injection.Landing(
                    "process-exit", run.record["landing_offset"], int(time.time()), None
                )
            time.sleep(restart_injection._EXIT_POLL_SECONDS)
        try:
            self._log_dialog(self._capture(target), "prompt-after-digit")
        except OSError:
            pass  # The pane is gone or unreadable; the landing below still says none.
        self._drop_prompt()
        return restart_injection.Landing(
            "none", run.record["landing_offset"], None, None
        )

    def _stopped_items(self, target):
        try:
            _, stopped = _background_result(self._capture(target))
            for item in stopped:
                self.restart_pass.emit("UR-STOPPED", session=self.key, item=item)
            detached_ledger.update(
                self.key, {"stopped_items": stopped, "stopped_items_confirmed": True}
            )
        except (OSError, ValueError):
            self.restart_pass.emit(
                "UR-RESTART-WARN", session=self.key, reason="stopped-items-unread"
            )


def wrapper_script(tmux_session: str) -> Path:
    return _cli_state_home() / "sessions" / f"{tmux_session}.sh"


def _cli_state_home() -> Path:
    """Match ai-cli-utils' platform-specific state root and absolute env rules."""
    if sys.platform == "win32":
        raw = os.environ.get("LOCALAPPDATA", "")
        fallback = Path.home() / "AppData" / "Local"
    else:
        raw = os.environ.get("XDG_STATE_HOME", "")
        fallback = Path.home() / ".local" / "state"
    base = Path(raw) if raw and Path(raw).is_absolute() else fallback
    return base / "ai-cli-utils"


def _restart_paths(session: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session):
        raise ValueError("invalid tmux session name")
    root = _cli_state_home() / "restart-session"
    return root / f"{session}.lock", root / f"{session}.json"


@contextlib.contextmanager
def _restart_lock(path: Path):
    """Same nonblocking OS lock as portalocker; never replace or unlink it."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
    except OSError:
        yield False
        return
    with handle:
        handle.seek(0)  # Windows locks the byte at the current offset.
        owned = supervisor._try_lock_exclusive(handle.fileno())
        try:
            yield owned
        finally:
            if owned:
                supervisor._unlock(handle.fileno())


def hosted_by_wrapper(tmux_session: str, pane: str) -> bool:
    """The pane was started by the ai c wrapper's generated session script, which
    relaunches ``claude --resume`` after a normal exit (ISSUE-REF)."""
    from ai_chassis.prompt_injection import _tmux  # noqa: PLC0415 - only for a candidate

    shown = _tmux.display(
        _tmux.DEFAULT_COMMAND, pane, "#{session_name}\t#{pane_start_command}"
    )
    if not shown:
        return False
    name, _, command = shown.strip().partition("\t")
    script = wrapper_script(tmux_session)
    return name == tmux_session and str(script) in command and script.is_file()


@dataclass(frozen=True)
class Candidate:
    session: goal_waker.WakerSession
    running: str
    installed: str
    tmux_session: str
    pane: str
    cwd: str = ""


class _Pass:
    def __init__(self, config: dict[str, Any], *, dry_run: bool, now: float) -> None:
        self.config = config
        self.dry_run = dry_run
        self.now = now
        self.events: list[str] = []
        self.store = goal_waker.WakerStore(
            supervisor._root_from_environment(), goal_waker.state_dir()
        )
        self.fallback = goal_waker.TypedFallback()
        self.warned_gate = False
        self.background_state: dict[str, dict[str, Any]] = {}

    def emit(self, event: str, **fields: Any) -> None:
        line = " ".join(
            [_iso(time.time()), event]
            + [f"{key}={_log_quote(value)}" for key, value in fields.items()]
        )
        self.events.append(line)
        if not self.dry_run:
            log = state_dir() / "update-restart.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def _attempt_path(self, session_id: str) -> Path:
        return state_dir() / "sessions" / f"{session_id}.json"

    def _attempt(self, session_id: str) -> dict[str, Any]:
        try:
            value = json.loads(self._attempt_path(session_id).read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _record_attempt(self, session_id: str, record: dict[str, Any]) -> None:
        path = self._attempt_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        temporary.write_text(json.dumps(record, sort_keys=True) + "\n", "utf-8")
        os.replace(temporary, path)

    def _note_attempt(self, candidate: Candidate, outcome: str) -> None:
        session_id = candidate.session.session_id
        self._record_attempt(
            session_id,
            {
                **{
                    key: value
                    for key, value in self._attempt(session_id).items()
                    if key in _KEPT_ACROSS_ATTEMPTS
                },
                "target": candidate.installed,
                "running": candidate.running,
                "pane": candidate.pane,
                "attempted_at": self.now,
                "outcome": outcome,
            },
        )

    # -- the gates, in the module docstring's order

    @staticmethod
    def stale(record: dict[str, Any], installed: str) -> str | None:
        running = record.get("version")
        if record.get("kind") != "interactive":
            return None
        current, target = _version(running), _version(installed)
        if current is None or target is None or target <= current:
            return None
        return str(running)

    def quiescent(
        self, session: goal_waker.WakerSession, record: dict[str, Any]
    ) -> str | None:
        if reason := self.background_gate(session, record):
            return reason
        updated = record.get("statusUpdatedAt")
        settle = self.config["idle_after_seconds"]
        if not isinstance(updated, (int, float)) or self.now - updated / 1000 < settle:
            return "status-not-settled"
        # Restart reading includes notices after a finished main turn with background work.
        idle = goal_waker.idle_state(
            session.transcript,
            self.now,
            {"clock_skew_seconds": 90, "idle_after_seconds": settle},
            extra_turn_end_subtypes=_NOTICE_TURN_ENDS,
        )
        if idle.kind != goal_waker.IDLE:
            if record.get("status") == "busy":
                return "foreground-command"
            return f"transcript-{idle.kind.lower()}-{idle.reason}"
        return None

    def background_gate(self, session, record) -> str | None:
        status = record.get("status")
        if unknown := self.no_work(session):
            return unknown
        if status not in {"idle", "shell", "busy"}:
            return "background-state-unknown"
        agents = self.background_state.get(session.session_id, {}).get("agents", 0)
        if status == "busy" and not agents:
            return "foreground-command"
        if record.get("waitingFor"):
            return f"status-{status or 'unknown'}"
        return None

    def no_work(self, session: goal_waker.WakerSession) -> str | None:
        try:
            units = _contention().session_units(
                session.session_id,
                # A long-running silent sub-agent still explains registry status busy.
                idle_seconds=float("inf"),
                background_idle_seconds=self.config["background_idle_seconds"],
                now=self.now,
            )
        except (OSError, ValueError):
            return "background-state-unknown"
        if (
            not isinstance(units, dict)
            or units.get("error") is not None
            or any(
                type(units.get(key)) is not int or units[key] < 0
                for key in ("agents", "background")
            )
        ):
            return "background-state-unknown"
        self.background_state[session.session_id] = units
        return None

    def hold_stands(self, name: str, pane: str | None) -> bool:
        """Whether a composer hold on ``name`` still blocks the restart (ISSUE-REF R-3).

        A hold records that text the waker typed may still sit in the composer. Nothing
        released one unless the waker wanted to type again, so holds written days ago kept
        working sessions from ever restarting. This runs only for a session that passed
        every other gate, so the library's own proof (a dry run that types nothing) samples
        a quiet pane, and the hold is released only when that proof says the composer is
        empty. A refused proof stamps the hold and waits ``hold_probe_retry_seconds``."""
        hold = self.store.held(name)
        if hold is None:
            return False
        if self.dry_run:
            return True
        probed = hold.get("probed_epoch")
        if (
            isinstance(probed, (int, float))
            and self.now - probed < self.config["hold_probe_retry_seconds"]
        ):
            return True
        try:
            empty = self.fallback.composer_empty(
                name,
                self.config["hold_probe_text"],
                target_spec=f"pane:{pane}" if pane else None,
            )
        except Exception:  # noqa: BLE001 - no proof, so the hold stands
            empty = False
        if not empty:
            self.store.mark_probed(name, self.now)
            return True
        self.store.release(name)
        self.emit("UR-HOLD-RELEASED", session=name, after=hold.get("reason", "unknown"))
        return False

    def unclaimed(
        self, session: goal_waker.WakerSession, pane: str | None = None
    ) -> str | None:
        if self.store.pending_marker(session.session_id):
            return "goal-nudge-pending"
        if session.name and self.hold_stands(session.name, pane):
            return "composer-held"
        key = goal_waker.session_key(session)
        if key is None:
            return "key-undetermined"
        return self.store.coexistence(
            key, self.now, self.config["coexistence_ttl_seconds"]
        )

    def awaiting_compact(
        self, session: goal_waker.WakerSession, installed: str
    ) -> str | None:
        """Defer the restart while the session's context says it should compact first.

        ISSUE-REF D-8. With ``compact_before_restart_when`` ``at_or_above`` (the shipped
        default) a session at or above ``compact_before_restart_pct`` waits and requests
        the existing idle save-state wake. ``below`` inverts the direction; ``off`` removes
        the gate. A window boundary after the hold began fulfils it in either direction.
        The wait is bounded: past ``compact_before_restart_max_wait_seconds`` from the first
        deferral for this target the session restarts without compacting, because the
        ``--resume`` relaunch keeps its transcript. An unavailable reading never holds it."""
        when = self.config["compact_before_restart_when"]
        if when == "off":
            return None
        if when not in ("at_or_above", "below"):
            if not self.warned_gate:
                self.warned_gate = True
                self.emit(
                    "UR-SKIP",
                    session="*",
                    reason=f"config-bad-value: compact_before_restart_when={when}",
                )
            return None
        percent = goal_waker.context_reading(session)
        if percent is None:
            return None
        limit = self.config["compact_before_restart_pct"]
        held = percent >= limit if when == "at_or_above" else percent < limit
        if not held:
            return None
        record = self._attempt(session.session_id)
        wait = record.get("compact_wait")
        if not isinstance(wait, dict) or wait.get("target") != installed:
            wait = {"target": installed, "since": self.now}
        since = wait.get("since")
        # Four-subtype reading suffices here: only the window start is used for compact expiry.
        idle = goal_waker.idle_state(
            session.transcript, self.now, goal_waker.load_config(None)
        )
        started = getattr(idle, "last_session_start_at", None)
        if isinstance(since, (int, float)) and (
            self.now - since >= self.config["compact_before_restart_max_wait_seconds"]
            or (started is not None and started > since)
        ):
            return None
        if "requested_at" not in wait and not self.dry_run:
            self._record_attempt(
                session.session_id,
                {**record, "compact_wait": {**wait, "requested_at": self.now}},
            )
        return "context-awaiting-compact"

    def clear_compact_request(self, session_id: str) -> None:
        """Retire the wake request when another gate holds or the compact hold ends.

        Keep ``since`` so a failed wake cannot reset the bounded wait.
        """
        record = self._attempt(session_id)
        wait = record.get("compact_wait")
        if not self.dry_run and isinstance(wait, dict) and "requested_at" in wait:
            self._record_attempt(
                session_id,
                {
                    **record,
                    "compact_wait": {
                        k: v for k, v in wait.items() if k != "requested_at"
                    },
                },
            )

    def recently_tried(self, candidate: Candidate) -> bool:
        last = self._attempt(candidate.session.session_id)
        at = last.get("attempted_at")
        return (
            last.get("target") == candidate.installed
            and isinstance(at, (int, float))
            and self.now - at < self.config["retry_after_seconds"]
        )

    def blocking_gate(
        self,
        session: goal_waker.WakerSession,
        record: dict[str, Any],
        installed: str,
        place: tuple[str, str] | None,
    ) -> str | None:
        """The first gate that keeps a stale session from restarting, in the docstring's order."""
        return (
            self.unknown_kind_gate(session, installed)
            or self.quiescent(session, record)
            or self.unclaimed(session, place[1] if place else None)
            or (None if place else "no-tmux-pane")
            or (None if place and hosted_by_wrapper(*place) else "not-wrapper-hosted")
            or self.awaiting_compact(session, installed)
        )

    def unknown_kind_gate(self, session, installed) -> str | None:
        unknown = self._attempt(session.session_id).get("unknown_kind", {})
        if (
            isinstance(unknown, dict)
            and unknown.get("target") == installed
            and unknown.get("attempts", 0) >= self.config["unknown_kind_max_attempts"]
        ):
            return f"unknown-kind-capped:{unknown['label']}"
        return None

    def stale_since(self, session_id: str, installed: str) -> float | None:
        """When this session was first seen stale against ``installed``, if recorded."""
        mark = self._attempt(session_id).get("stale")
        if (
            isinstance(mark, dict)
            and mark.get("target") == installed
            and isinstance(mark.get("since"), (int, float))
        ):
            return float(mark["since"])
        return None

    def note_stale(
        self,
        session: goal_waker.WakerSession,
        running: str,
        installed: str,
        reason: str,
    ) -> None:
        """Keep how long a blocked session has been stale, and tell it once (ISSUE-REF D-9).

        A session that never goes quiet is never restarted by a pass that waits for quiet
        (ISSUE-REF). After ``window_notice_after_seconds`` stale, the session gets one notice
        per target version asking it to reach a restart point. It is advice only: the pass
        restarts the session whenever its gates pass, and types nothing. ``0`` disables it."""
        if self.dry_run:
            return
        session_id = session.session_id
        since = self.stale_since(session_id, installed)
        if since is None:
            since = self.now
            self._record_attempt(
                session_id,
                {
                    **self._attempt(session_id),
                    "stale": {"target": installed, "since": since},
                },
            )
        after = self.config["window_notice_after_seconds"]
        if (
            not after
            or self.now - since < after
            or self._attempt(session_id).get("window_noticed") == installed
        ):
            return
        key = goal_waker.session_key(session)
        if key is None:
            return
        written = observe.write_notice(
            {"key": key, "use_case": "update-restart"},
            "restart-window",
            (
                f"Claude Code {installed} is installed and this session still runs "
                f"{running}. The update restart has waited {_age(self.now - since)} "
                f"for it; the gate holding it is {reason}.\n"
                "Finish the batch you are on and go idle. Background shells and sub-agents "
                "can remain running. The restart then relaunches this session on the new version "
                "and resumes it. Nothing is forced and nothing is typed into this session."
            ),
            heading="UPDATE RESTART",
        )
        if written is not None:
            self._record_attempt(
                session_id,
                {**self._attempt(session_id), "window_noticed": installed},
            )

    def assess(
        self, session: goal_waker.WakerSession, record: dict[str, Any], installed: str
    ) -> Candidate | None:
        running = self.stale(record, installed)
        if running is None:
            self.clear_compact_request(session.session_id)
            return None
        fields = {
            "session": session.label,
            "running": running,
            "installed": installed,
            **goal_waker.notice_fields(session.transcript),
        }
        place = _pane(record)
        reason = self.blocking_gate(session, record, installed, place)
        if reason != "context-awaiting-compact":
            self.clear_compact_request(session.session_id)
        if reason is not None:
            self.note_stale(session, running, installed, reason)
            self.emit("UR-WAIT", **fields, reason=reason)
            return None
        candidate = Candidate(
            session, running, installed, *place, cwd=record.get("cwd", "")
        )
        if self.recently_tried(candidate):
            self.emit("UR-WAIT", **fields, reason="tried-recently")
            return None
        return candidate

    # -- the restart

    def consider(
        self, session: goal_waker.WakerSession, record: dict[str, Any], installed: str
    ) -> bool:
        place = _pane(record)
        if place is None or self.stale(record, installed) is None:
            self.assess(session, record, installed)
            return False
        try:
            lock, result = _restart_paths(place[0])
            with _restart_lock(lock) as owned:
                if not owned:
                    self.emit("UR-SKIP", session=session.label, reason="restart-lock")
                    return False
                return self._consider_owned(session, record, installed, result)
        except (OSError, ValueError, TypeError, OverflowError):
            self.emit(
                "UR-SKIP", session=session.label, reason="restart-state-unavailable"
            )
            return False

    def _consider_owned(
        self,
        session: goal_waker.WakerSession,
        record: dict[str, Any],
        installed: str,
        result_path: Path,
    ) -> bool:
        if reason := self.unknown_kind_gate(session, installed):
            self.emit(
                "UR-WAIT",
                session=session.label,
                reason=reason,
            )
            return False
        try:
            previous = json.loads(result_path.read_text("utf-8"))
        except FileNotFoundError:
            previous = {}
        if not isinstance(previous, dict):
            raise ValueError("invalid restart result")
        at = previous.get("last_restart", 0)
        if type(at) not in (int, float) or not math.isfinite(at):
            raise ValueError("invalid restart timestamp")
        if time.time() - at < self.config["retry_after_seconds"]:
            self.emit("UR-SKIP", session=session.label, reason="restart-cooldown")
            return False
        inventory = goal_waker._script("claude_sessions")
        current = inventory._validated_records(inventory._proc_root(None), None).get(
            session.pid
        )
        if current is None or any(
            current.get(key) != record.get(key)
            for key in ("sessionId", "procStart", "tmux")
        ):
            self.emit(
                "UR-WAIT" if current is None else "UR-SKIP",
                session=session.label,
                reason="background-state-unknown"
                if current is None
                else "restart-identity",
            )
            return False
        candidate = self.assess(session, current, installed)
        return candidate is not None and self.restart(candidate, result_path)

    def tell_session(self, candidate: Candidate, result: Any, reason: str) -> None:
        """A refusal that names an unverified Claude Code version is a bug (ISSUE-REF R-1).

        The session reads this at its next prompt and is told to run the fix, with the
        evidence and the ``/delegate ... /bug-fix`` command for this machine's billing
        route, the pattern /start-goal follows (ISSUE-REF). One notice per installed
        version, whichever session meets the refusal first, so a failing probe starts one
        fix, not one per tick. The timer launches nothing itself."""
        record = _attempt_record(result)
        if "unverified-version" not in reason or "key" not in record:
            return
        self._tell_version(
            record,
            candidate.installed,
            candidate.running,
            reason,
            f"The update restart onto Claude Code {candidate.installed} (this session "
            f"runs {candidate.running}) was refused: {reason}.\n"
            "A new Claude Code version must never block a restart, so this is a bug.\n",
        )

    def _tell_version(  # noqa: PLR0913 - the notice's record, versions and wording
        self,
        record,
        installed: str,
        running: str,
        reason: str,
        summary: str,
        *,
        probe_version=None,
    ) -> None:
        marker = f"{_NOTICE_PREFIX}{installed}"
        if self._attempt(marker):
            return
        written = observe.write_notice(
            record,
            "unverified-version",
            (
                summary
                + f"Run this command now, before anything else, and let it finish: "
                f"{bug_fix_delegation()}\n"
                "Give it this evidence: the detail above, the log "
                "~/.local/state/ai-harness/update-restart/update-restart.log and the design "
                "docs/designs/issue-ref-cc-update-detect-auto-restart.md (R-1)."
            ),
            heading="UPDATE RESTART",
        )
        if written is not None:
            self._record_attempt(
                marker,
                {
                    "running": running,
                    "reason": reason,
                    "noticed_at": self.now,
                    "reprobes": 0,
                    "healed": False,
                    **({"probe_version": probe_version} if probe_version else {}),
                },
            )

    def ctx_pct_notices(self, live) -> None:
        if self.dry_run or not live:
            return
        for version, detail in capability.ctx_pct_notices():
            marker = f"{_NOTICE_PREFIX}{version}"
            if self._attempt(marker):
                continue
            session, record = next(
                ((s, r) for s, r in live if r.get("version") == version), live[0]
            )
            key = goal_waker.session_key(session)
            if key is None:
                continue
            # Not a skip: no restart waits on this. Two inconclusive measurements admit
            # the version; a cached ctx-pct failure refuses only deliveries that need the
            # walk. Either way the probe stage itself is the bug the notice hands over.
            admitted = detail.startswith("ctx-pct: inconclusive")
            self.emit(
                "UR-PROBE-NOTICE",
                session=session.label,
                version=version,
                admitted="yes" if admitted else "no",
                reason=detail,
            )
            self._tell_version(
                {"key": key, "use_case": "session-restart"},
                version,
                record.get("version", version),
                detail,
                (
                    f"Claude Code {version} was admitted, but its capability probe "
                    f"could not measure the ctx-pct stage: {detail}.\n"
                    if admitted
                    else f"The capability probe for Claude Code {version} failed its "
                    f"ctx-pct stage: {detail}. Update restarts do not need this check; "
                    "deliveries that need the multiline walk do.\n"
                )
                + "The probe should verify every new version on its own, so this is a "
                "bug.\n",
                probe_version=version,
            )

    def reprobe(self) -> None:
        """Re-run the capability probe for a version a refusal named, bounded.

        A version whose probe starts passing heals without anyone acting. Each version is
        probed at most ``reprobe_max`` times, no more often than every
        ``reprobe_after_seconds``, and never after it passes."""
        if self.dry_run:
            return
        for path in sorted((state_dir() / "sessions").glob(f"{_NOTICE_PREFIX}*.json")):
            record = self._attempt(path.stem)
            version = record.get("probe_version", record.get("running"))
            if not isinstance(version, str) or record.get("healed"):
                continue
            tried = record.get("reprobes", 0)
            last = record.get("reprobed_at", record.get("noticed_at"))
            if (
                not isinstance(tried, int)
                or tried >= self.config["reprobe_max"]
                or not isinstance(last, (int, float))
                or self.now - last < self.config["reprobe_after_seconds"]
            ):
                continue
            try:
                failure = capability.check_capability(version, refresh=True)
            except Exception as error:  # noqa: BLE001 - a probe error is a failed probe
                failure = f"probe-error: {type(error).__name__}"
            self._record_attempt(
                path.stem,
                {
                    **record,
                    "reprobes": tried + 1,
                    "reprobed_at": self.now,
                    "healed": failure is None,
                },
            )
            self.emit(
                "UR-REPROBE",
                version=version,
                outcome="passed" if failure is None else failure,
            )

    def resume_prompt(self, candidate: Candidate) -> str:
        """The first prompt the relaunched process is handed (ISSUE-REF R-2).

        The same line launch-resume types after any relaunch, so the session loads its task
        panel and then continues its resume chain, and so the two cannot drift. This prompt's
        submit silences the launch-resume watcher the relaunch also arms, which keeps the
        chain to once. A non-empty ``resume_prompt`` in the config replaces it."""
        template = self.config["resume_prompt"]
        if template:
            return template.format(
                running=candidate.running, installed=candidate.installed
            )
        return launch_resume.RESUME_LINE.format(
            cause=f"an update restart onto {candidate.installed} (was {candidate.running})"
        )

    def restart(self, candidate: Candidate, result_path: Path) -> bool:
        fields = {
            "session": candidate.session.label,
            "pane": candidate.pane,
            "running": candidate.running,
            "installed": candidate.installed,
            **goal_waker.notice_fields(candidate.session.transcript),
        }
        if self.dry_run:
            self.emit("UR-RESTART", **fields, detail="dry-run")
            return True
        key = goal_waker.session_key(candidate.session)
        try:
            if (
                not key
                or not candidate.session.name
                or not isinstance(candidate.cwd, str)
                or not Path(candidate.cwd).is_absolute()
            ):
                raise ValueError("handoff identity unavailable")
            detached_ledger.record(
                key,
                candidate.session.name,
                [],
                cc_version=candidate.running,
                name=candidate.session.name,
                cwd=candidate.cwd,
            )
        except (OSError, ValueError):
            self.emit("UR-WAIT", **fields, reason="ledger-unwritable")
            return False
        prompt = state_dir() / "prompts" / f"{candidate.session.session_id}.txt"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text(self.resume_prompt(candidate), encoding="utf-8")
        shared = {
            "session_id": candidate.session.session_id,
            "last_restart": time.time(),
            "failed": True,
            "reason": "interrupted",
        }
        write_json_fsync(result_path, shared)
        try:
            # Act in the lock owner's process. No helper can outlive ownership.
            # The library returns only after replacement verification or failure.
            result = _BackgroundRestartInjector(
                self,
                key,
                resume_prompt=prompt.read_text(encoding="utf-8"),
                agent_names=self.config["agent_names"],
            ).run(f"pane:{candidate.pane}")
        except OSError as error:
            reason = f"launch-failed: {error}"
        else:
            if result.reason and result.reason.startswith("unknown-kind:"):
                self._unknown_kind(
                    candidate, result.reason.removeprefix("unknown-kind:")
                )
                shared.update(reason=result.reason)
                write_json_fsync(result_path, shared)
                return False
            if result.reason == "ledger-unwritable":
                shared.update(reason=result.reason)
                write_json_fsync(result_path, shared)
                self.emit("UR-WAIT", **fields, reason=result.reason)
                return False
            reason = (
                "restarted"
                if result.state == "COMPLETED"
                else f"launch-refused: {result.reason}"
            )
            if reason != "restarted":
                # The failing stage and detail, not only the bare reason (ISSUE-REF R-1).
                detail = _refusal_detail(result)
                reason = f"{reason} ({detail})" if detail else reason
                self.tell_session(candidate, result, reason)
        failed = reason != "restarted"
        shared.update(failed=failed, reason=reason)
        write_json_fsync(result_path, shared)
        if failed:
            self._note_attempt(candidate, reason)
            self.emit("UR-RESTART-FAIL", **fields, reason=reason)
            self.report_exit(candidate, reason)
            return False
        self._note_attempt(candidate, "launched")
        self.emit("UR-RESTART", **fields, detail=result.reason)
        return True

    def _unknown_kind(self, candidate: Candidate, label: str) -> None:
        previous = self._attempt(candidate.session.session_id).get("unknown_kind", {})
        attempts = (
            previous.get("attempts", 0)
            if previous.get("target") == candidate.installed
            else 0
        )
        self._note_attempt(candidate, "unknown-kind")
        self._record_attempt(
            candidate.session.session_id,
            {
                **self._attempt(candidate.session.session_id),
                "unknown_kind": {
                    "target": candidate.installed,
                    "label": label,
                    "attempts": attempts + 1,
                },
            },
        )
        reason = f"unknown-kind:{label}"
        self.emit(
            "UR-WAIT",
            session=candidate.session.label,
            reason="monitor-running",
            label=label,
        )
        self.emit("UR-WAIT", session=candidate.session.label, reason=reason)
        if attempts + 1 >= self.config["unknown_kind_max_attempts"]:
            self.emit("UR-RESTART-FAIL", session=candidate.session.label, reason=reason)
            self.report_exit(candidate, f"{reason}; retry cap reached")

    @staticmethod
    def report_exit(candidate: Candidate, reason: str) -> None:
        key = goal_waker.session_key(candidate.session)
        if key:
            observe.write_notice(
                {"key": key, "use_case": "update-restart"},
                "background-exit",
                f"The update restart could not finish: {reason}.",
                heading="UPDATE RESTART",
            )


def _session_inventory() -> (
    tuple[
        list[tuple[goal_waker.WakerSession, dict[str, Any]]],
        list[goal_waker.WakerSession],
    ]
    | None
):
    """``(registered, unregistered)`` live sessions, or None when the inventory is unreadable.

    A live session without a validated registry record for its own conversation waits on
    its own; it never makes the whole pass undetermined (ISSUE-REF: one such session on
    example-project-ec2 held every restart on the host from #5650 on).
    """
    try:
        sessions = goal_waker.discover_sessions(lambda *_a, **_k: None)
        if sessions is None:
            return None
        inventory = goal_waker._script("claude_sessions")
        records = inventory._validated_records(inventory._proc_root(None), None)
    except (OSError, ValueError):
        return None
    registered = []
    unregistered = []
    for session in sessions:
        record = records.get(session.pid)
        if record is not None and record.get("sessionId") == session.session_id:
            registered.append((session, record))
        else:
            unregistered.append(session)
    return registered, unregistered


def _live_sessions() -> list[tuple[goal_waker.WakerSession, dict[str, Any]]] | None:
    """Each live session with its validated registry record, or None when undetermined."""
    inventory = _session_inventory()
    return None if inventory is None else inventory[0]


def tick(*, dry_run: bool = False, now: float | None = None) -> dict[str, Any]:
    """One pass over every live session; returns the events it logged."""
    # The waker unit has a 90s start limit. Reserve 30s for non-probe work and
    # teardown; all re-probes and preflight checks share this one deadline.
    with capability.probe_budget(60):
        return _tick(dry_run=dry_run, now=now)


def _tick(*, dry_run: bool, now: float | None) -> dict[str, Any]:
    config = load_config()
    run = _Pass(config, dry_run=dry_run, now=time.time() if now is None else now)
    if not config["enabled"]:
        return {"events": run.events, "restarted": 0}
    installed = installed_version()
    if installed is None:
        return {"events": run.events, "restarted": 0}
    run.reprobe()
    inventory = _session_inventory()
    if inventory is None:
        run.emit("UR-WAIT", session="*", reason="background-state-unknown")
        return {"events": run.events, "restarted": 0}
    live, unregistered = inventory
    for session in unregistered:
        run.emit(
            "UR-WAIT",
            session=session.label,
            reason="background-state-unknown",
            cause="registry-record-missing",
        )
    run.ctx_pct_notices(live)
    restarted = 0
    for session, record in live:
        if run.consider(session, record, installed):
            restarted += 1
    return {"events": run.events, "restarted": restarted}


def compact_requested(session: goal_waker.WakerSession, *, now: float) -> bool:
    """A persisted request still held for the currently installed update target.

    Recheck the live config, context, window boundary and deadline before the waker
    relaxes its historical-save gate. This read never creates or refreshes a request.
    """
    config = load_config()
    if not config["enabled"]:
        return False
    run = _Pass(config, dry_run=True, now=now)
    wait = run._attempt(session.session_id).get("compact_wait")
    if not isinstance(wait, dict) or "requested_at" not in wait:
        return False
    installed = installed_version()
    return (
        installed is not None
        and wait.get("target") == installed
        and run.awaiting_compact(session, installed) == "context-awaiting-compact"
    )


def status(*, now: float | None = None) -> list[str]:
    """One line per stale session: its first failing gate and how long it has been stale
    (ISSUE-REF R-6). Read-only: it probes nothing and records nothing."""
    config = load_config()
    if not config["enabled"]:
        return [_disabled_line(config)]
    run = _Pass(config, dry_run=True, now=time.time() if now is None else now)
    installed = installed_version()
    if installed is None:
        return ["installed Claude Code version undetermined"]
    live = _live_sessions()
    if live is None:
        return ["session inventory undetermined"]
    lines = []
    for session, record in live:
        running = run.stale(record, installed)
        if running is None:
            continue
        gate = run.blocking_gate(session, record, installed, _pane(record))
        since = run.stale_since(session.session_id, installed)
        marks = run._attempt(session.session_id)
        lines.append(
            f"{session.label} runs {running}, installed {installed}: "
            f"{gate or 'no gate holds it'}; "
            + (
                f"stale {_age(run.now - since)}"
                if since is not None
                else "stale age not yet recorded"
            )
            + (
                "; window notice sent"
                if marks.get("window_noticed") == installed
                else ""
            )
            + (
                "; compact requested"
                if gate == "context-awaiting-compact"
                and isinstance(wait := marks.get("compact_wait"), dict)
                and wait.get("target") == installed
                and "requested_at" in wait
                else ""
            )
        )
    return lines or [f"no stale session (installed {installed})"]


def identity_recovery_actions(session: str) -> list[str]:
    """Print a launcher runbook; do not inspect jobs, write panels or signal PIDs.

    An attach client is not an interactive restart target. Its daemon must first
    be repaired by the launcher coordinating all owners. A restart of just the
    viewer cannot replace a contaminated background worker.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", session):
        raise ValueError("session must be a registered session slug")
    return [
        "Report only. No process or task directory is modified.",
        "1. From the installed main checkout: .venv/bin/python scripts/session_identity.py doctor",
        "2. Coordinate every daemon owner's handoff. Preserve all task directories, including the daemon creator's panel.",
        "3. For a pending Move to background handoff, capture the owning pane, then run: "
        ".venv/bin/python -m ai_chassis.update_restart finish-background "
        f"--session {session} --pane-capture <verified-capture-path>",
        "4. Launcher prerequisite: replace the contaminated shared daemon and its idle spares "
        "with a clean environment after owners are safe. A viewer restart alone retains the worker environment. "
        "The daemon lifecycle and attach-client recovery require ai-cli-utils coordination; "
        "do not stop the shared daemon while owners still rely on it.",
        f"5. For an interactive launcher target: ai -X c-{session}. "
        "For a background attach target this command refuses: use the launcher's "
        "verified background-job handoff and supervisor resume after daemon repair. "
        "Do not create a second --bg --resume copy.",
        "6. In the recovered conversation: /load-task-panel. Verify the registered name "
        "and host before using native task tools; never rewrite another panel.",
        "7. Re-run .venv/bin/python scripts/session_identity.py doctor. Unreadable evidence "
        "is unknown and does not establish recovery.",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ai_chassis.update_restart",
        description="Restart idle sessions onto an installed Claude Code update.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    tick_parser = commands.add_parser("tick", help="run one pass")
    tick_parser.add_argument(
        "--dry-run", action="store_true", help="log decisions; restart nothing"
    )
    commands.add_parser(
        "status", help="list each stale session and the gate holding it"
    )
    finish_parser = commands.add_parser(
        "finish-background", help="complete a background handoff"
    )
    finish_parser.add_argument("--session", required=True)
    finish_parser.add_argument("--pane-capture", required=True, type=Path)
    recovery_parser = commands.add_parser(
        "recover-identity",
        help="print ordered identity recovery actions; execute nothing",
    )
    recovery_parser.add_argument("--session", required=True)
    args = parser.parse_args(argv)
    if args.command == "recover-identity":
        try:
            actions = identity_recovery_actions(args.session)
        except ValueError as exc:
            parser.error(str(exc))
        print("\n".join(actions))
        return 0
    if args.command == "finish-background":
        return finish_background(args.session, args.pane_capture)
    if args.command == "status":
        print("\n".join(status()))
        return 0
    report = tick(dry_run=args.dry_run)
    print("\n".join(report["events"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
