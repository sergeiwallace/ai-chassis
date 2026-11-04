"""Typing-aware launch-resume for Claude Code sessions on every profile (ISSUE-REF).

Design: ``docs/designs/issue-ref-typing-aware-launch-resume.md`` (Launch decision, Composer
gate, Delivery legs, Observability, Data Model).

``session-start`` runs from the SessionStart hook: it classifies why the previous run of the
resume key ended (``ai_chassis.exit_cause``), applies the profile's ``launch_resume`` mode and
scenario toggles, skips when the launcher already delivered the prompt natively, and otherwise
writes the launch record, prints the rendered context as one ``<system-reminder>`` block and
spawns one detached ``gate-run`` watcher. The watcher feeds the composer gate
(:mod:`ai_chassis.launch_resume_gate`) composer samples, the submit marker the
UserPromptSubmit hook writes and the end record the SessionEnd hook writes, and fires the one
typed line through :class:`~ai_chassis.prompt_injection.injector.LaunchResumeInjector`.

Layout under ``${AIH_RESUME_ROOT:-~/.claude/resume}/launch/``::

    <key>.json          the CURRENT launch instance's record: session_id, state, cause,
                        timers, every transition
    <key>.record.lock   held for the instant of each write of <key>.json
    <key>.<sid>.lock    held by the one watcher driving instance <sid>, its token inside
    <key>.prompt        the rendered context the record was armed with
    <key>.firing        session_id, a one-use nonce and the exact line, one per line,
                        written just before the type and removed on every exit
    <key>.submit.json   the last UserPromptSubmit: at, origin, prompt_sha256, session_id,
                        nonce
    <key>.end.json      written by the SessionEnd hook (Phase 1); read here only
    <key>.gate.log      the watcher's stderr
    history/<key>.<sid>.json  a replaced instance's record, in its terminal state
    last-boot.json      the host-boot record (Phase 1's ``ai_chassis.host_boot``)

A launch instance is one SessionStart: the CC ``session_id`` from the hook payload plus
the ``claude`` process that ran it (ISSUE-REF). The process matters because a
``claude --resume`` relaunch keeps its session id. A new instance supersedes a waiting
record of the previous one (``ABANDONED reason=superseded``), so a silence, an end record,
a lock or a consumed delivery of one instance never reaches the next.

Every hook entry point fails open: an error is logged as a ``CC-LAUNCH-RESUME-SKIP`` line and
the command exits 0.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_chassis import context_supervisor as supervisor
from ai_chassis import launch_resume_gate as gm
from ai_chassis import reboot_resume as rr

try:  # POSIX: Linux, macOS
    import fcntl as _fcntl
except ImportError:  # Windows: no tmux composer to watch, so no second watcher either
    _fcntl = None

RESUME_TEMPLATE = rr.TEMPLATE_PATH
START_TEMPLATE = RESUME_TEMPLATE.with_name("launch-start-prompt.md")
GATE_LABEL = "LAUNCH_RESUME_GATE=launch-resume-session-start.sh"
RESUME_LINE = (
    "Resume after {cause}: first /load-task-panel and then follow the launch-resume "
    "context above, including its check for background shells and agents the previous "
    "exit moved to a detached background session, then /auto-start"
)
START_LINE = "Start: run the session startup checklist from CLAUDE.md, then /next"
#: The beads-ui mod's board. Its ``/show-beads-ui`` is a mod command the
#: model cannot invoke, and text after a command name is that command's arguments, so the
#: gate types it as its own submission AFTER the line: once open, the pane takes the
#: keyboard from an empty prompt, and a line typed after it would land in the pane.
BEADS_UI_PLUGIN = "beads-ui@beads-ui"
BEADS_UI_LINE = "/show-beads-ui"
#: A pending record consumed as one of these already started this boot's first turn:
#: the launcher's argv first prompt, or a foreign first prompt (a cross-session handoff
#: the sweep would not clobber). Arming the gate as well would be a second resume.
NATIVE_DELIVERIES = frozenset({"launcher", "context-only"})
#: A relauncher's name for why it relaunched, mapped to the cause it implies.
LAUNCH_REASON_CAUSES = {
    **{cause: cause for cause in supervisor.LAUNCH_RESUME_SCENARIOS},
    "boot-sweep": "host_reboot",
}
#: The gate has just seen an empty composer; the injector's own two-sample check needs
#: only this long, and a human typing in that window is refused rather than waited out.
FIRE_READY_WAIT_SECONDS = 2.0
#: The causes that mean the previous run was killed rather than closed: the status inventory.
KILLED_CAUSES = frozenset({"host_reboot", "terminal_quit_or_crash"})
_NOT_RECORDS = (".end.json", ".submit.json")
#: How often a watcher re-tries the instance lock a superseded watcher still holds.
LOCK_RETRY_SECONDS = 0.25
#: The states in which a launch record still has a watcher driving it.
LIVE_STATES = gm.WAITING | {gm.FIRING}
_LAST_BOOT = "last-boot.json"

Classifier = Callable[..., Any]


# --- paths, time and logging -----------------------------------------------------------


def launch_root() -> Path:
    return rr.resume_root() / "launch"


def _path(key: str, suffix: str) -> Path:
    return launch_root() / f"{key}{suffix}"


def record_path(key: str) -> Path:
    return _path(key, ".json")


def _iso(epoch: float) -> str:
    moment = datetime.fromtimestamp(epoch, UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _epoch(value: object) -> float | None:
    parsed = rr._parse_time(value)
    return parsed.timestamp() if parsed is not None else None


def _word(value: object) -> str:
    return "".join("_" if c.isspace() else c for c in str(value)) or "-"


def _evidence(evidence: dict[str, Any] | None) -> str:
    if not evidence:
        return "none recorded"
    return " ".join(
        f"{name}={_word(value)}" for name, value in sorted(evidence.items())
    )


def _log(stage: str, key: str, detail: str = "", **fields: object) -> None:
    head = " ".join(f"{name}={_word(value)}" for name, value in fields.items())
    supervisor.log_stage(stage, key, " ".join(part for part in (head, detail) if part))


def _skip(key: str, reason: str, **fields: object) -> int:
    _log("CC-LAUNCH-RESUME-SKIP", key, f"reason={reason}", **fields)
    return 0


def _write(path: Path, record: dict[str, Any]) -> None:
    rr._write_json(path, record)


def _records() -> Iterator[Path]:
    root = launch_root()
    if not root.is_dir():
        return
    for path in sorted(root.glob("*.json")):
        if path.name != _LAST_BOOT and not path.name.endswith(_NOT_RECORDS):
            yield path


# --- launch instances --------------------------------------------------------------------


def instance_name(session_id: str) -> str:
    """``session_id`` as a path component; ``-`` when the hook passed none."""
    return rr.sanitize_key(session_id) or "-"


def instance_lock_path(key: str, session_id: str) -> Path:
    return _path(key, f".{instance_name(session_id)}.lock")


def history_path(key: str, session_id: str) -> Path:
    return launch_root() / "history" / f"{key}.{instance_name(session_id)}.json"


def _session_of(record: dict[str, Any]) -> str:
    return str(record.get("session_id") or "")


def _same_instance(record: dict[str, Any], session_id: str, pid: int | None) -> bool:
    """Whether ``record`` was armed by this SessionStart's instance: the same session id
    and, where both are known, the same ``claude`` process."""
    if _session_of(record) != session_id:
        return False
    held = record.get("pid")
    return not (isinstance(held, int) and isinstance(pid, int) and held != pid)


def _owns(record: dict[str, Any] | None, session_id: str, armed_at: object) -> bool:
    """Whether ``record`` is still the one a watcher of (session_id, armed_at) drives."""
    return (
        record is not None
        and _session_of(record) == session_id
        and record.get("armed_at") == armed_at
    )


@contextmanager
def _record_lock(key: str) -> Iterator[None]:
    """Serialises every write of ``<key>.json``: session-start's supersede and arm, and a
    watcher's ownership check plus its write, so a superseded watcher cannot overwrite
    the record that replaced its own."""
    if _fcntl is None:
        yield
        return
    path = _path(key, ".record.lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)


def _retire(key: str, record: dict[str, Any], now: float, by: str) -> None:
    """Move another instance's record to ``history/``. A record still waiting or firing
    ends ``ABANDONED reason=superseded`` there. The caller holds the record lock."""
    state = record.get("state")
    if state not in gm.TERMINAL:
        record.setdefault("transitions", []).append(
            {
                "at": _iso(now),
                "from": state,
                "to": gm.ABANDONED,
                "observation": "superseded",
            }
        )
        record.update(state=gm.ABANDONED, reason="superseded", superseded_by=by)
        _log(
            "CC-LAUNCH-RESUME-ABANDONED",
            key,
            "reason=superseded",
            previous_state=state,
            cause=record.get("cause"),
            mode=record.get("mode"),
        )
    previous = _session_of(record)
    target = history_path(key, previous)
    if target.exists():
        armed = _epoch(record.get("armed_at")) or now
        target = target.with_name(f"{target.stem}.{int(armed * 1000)}.json")
    _write(target, record)
    record_path(key).unlink(missing_ok=True)
    if previous != by:
        # Its watcher sees the record gone at its next sample and exits; the lock file is
        # that instance's alone, so no later instance ever waits on it.
        instance_lock_path(key, previous).unlink(missing_ok=True)


def _store(key: str, record: dict[str, Any], now: float) -> None:
    """Write ``record`` as the key's current one, retiring whatever instance it replaces.
    The caller holds the record lock."""
    existing = rr._read_json(record_path(key))
    if existing is not None and not _owns(
        existing, _session_of(record), record.get("armed_at")
    ):
        _retire(key, existing, now, _session_of(record))
    _write(record_path(key), record)


# --- the launch decision -----------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    decision: str
    reason: str | None
    cause: str
    classified_cause: str
    launch_reason: str | None
    toggle: str

    @property
    def armed(self) -> bool:
        return self.decision == "arm"


def decide(
    classified_cause: str, settings: dict[str, Any], launch_reason: str | None = None
) -> Decision:
    """The mode and the scenario toggle for one cause. A relauncher's ``launch_reason``
    wins over the classifier, which is inferring, and both are recorded."""
    cause = LAUNCH_REASON_CAUSES.get(launch_reason or "", classified_cause)
    mode = settings["mode"]

    def result(armed: bool, reason: str | None, toggle: str) -> Decision:
        return Decision(
            "arm" if armed else "skip",
            None if armed else reason,
            cause,
            classified_cause,
            launch_reason,
            toggle,
        )

    if mode == "never":
        return result(False, "mode-never", "mode")
    if mode == "reboot_only":
        return result(cause == "host_reboot", "mode-reboot-only", "mode")
    if cause == "still_running":
        return result(False, "still-running", "mode")
    if cause not in settings["scenarios"]:
        return result(False, f"cause-{cause}", "mode")
    toggle = f"scenarios.{cause}"
    return result(settings["scenarios"][cause], f"scenario-off:{cause}", toggle)


def _other_boot(record: dict[str, Any] | None, boot_id: object) -> bool:
    held = (record or {}).get("boot_id")
    return bool(boot_id) and isinstance(held, str) and bool(held) and held != boot_id


def ran_before_boot(key: str, boot: Any) -> str | None:
    """The record showing ``key`` ran before the current boot, or None (ISSUE-REF).

    A raw stamp from another boot, a launch record (current or replaced) armed before the
    boot, or a consumed sweep claim whose stamp came from another boot. The boot sweep's
    live pending claim is the caller's: it is consumed before a later launch could read it.
    """
    boot_id, boot_time = (
        getattr(boot, "boot_id", None),
        getattr(boot, "boot_time", None),
    )
    stamps = rr.stamp_root()
    if _other_boot(rr._read_json(stamps / f"{key}.json"), boot_id):
        return "stamp"
    if isinstance(boot_time, int | float) and not isinstance(boot_time, bool):
        history = launch_root() / "history"
        replaced = sorted(history.glob(f"{key}.*.json")) if history.is_dir() else []
        for path in (record_path(key), *replaced):
            record = rr._read_json(path) or {}
            armed = _epoch(record.get("armed_at")) if record.get("key") == key else None
            if armed is not None and armed < boot_time:
                return "launch-record"
    consumed = stamps / "consumed"
    for path in sorted(consumed.glob(f"{key}-*.json")) if consumed.is_dir() else []:
        record = rr._read_json(path)
        if record is not None and record.get("key") == key:
            if _other_boot(record, boot_id):
                return "consumed"
    return None


def backed_reason(
    key: str, launch_reason: str | None, boot: Any, pending_live: bool
) -> str | None:
    """``launch_reason`` when it may override the classifier, else None (ISSUE-REF).

    ``AIH_LAUNCH_REASON`` is inherited by every process a relaunched session starts, so a
    ``host_reboot`` reason reaches sessions that never ran before the boot. It is honoured
    only with a live sweep claim or a record of the key from before the boot."""
    if launch_reason is None or pending_live:
        return launch_reason
    if LAUNCH_REASON_CAUSES.get(launch_reason) != "host_reboot":
        return launch_reason
    return launch_reason if ran_before_boot(key, boot) is not None else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _panes() -> list[tuple[str, str, int]]:
    return rr._list_panes() or []


def _session_pid(pane: str) -> int | None:
    """The ``claude`` process under ``pane``, for the watcher's session-gone check."""
    match = next((p for p in _panes() if p[1] == pane), None)
    if match is None:
        return None
    source = rr.process_source()
    return rr._find_claude(match[2], source.children(), source.is_claude)


def _owner_claude(hook_parent: int) -> int | None:
    """The ``claude`` that fired the hook: the nearest ``claude`` at or above the hook's parent
    pid, or None when none is one (a host whose process tree cannot be read).

    ISSUE-REF: a headless ``claude -p``, a forked sub-agent or any nested ``claude`` inherits
    the pane's ``TMUX_PANE`` from its parent, so the pane's top-level ``claude`` (``_session_pid``)
    is NOT the one starting; arming for it would sample, and could type into, the parent's
    composer. The decision arms only when the two pids agree."""
    from ai_chassis import exit_cause

    try:
        found = exit_cause._claude_ancestor(hook_parent)
        source = rr.process_source()
        return found if source.is_claude(found) else None
    except Exception:  # noqa: BLE001 - an unreadable tree is "unknown", never a crash
        return None


def _stamp(key: str) -> dict[str, Any]:
    for path in (
        rr.stamp_root() / f"{key}.json",
        rr.stamp_root() / "pending" / f"{key}.json",
    ):
        record = rr._read_json(path)
        if record is not None:
            return record
    return {}


def _classify(
    key: str,
    settings: dict[str, Any],
    classify: Classifier | None,
    boot: Any,
    now: datetime,
) -> Any:
    """Phase 1's classifier, imported lazily so a missing module is a logged skip."""
    if classify is None:
        from ai_chassis import exit_cause

        classify = exit_cause.classify
    if boot is None:
        from ai_chassis import host_boot

        boot = host_boot.identity()
    return classify(
        key,
        resume_root=rr.resume_root(),
        boot=boot,
        live_pane_ids=frozenset(p[1] for p in _panes()),
        pid_alive=_pid_alive,
        now=now,
        ttl_seconds=int(settings["stamp_ttl_seconds"]),
    )


def native_delivery(  # noqa: PLR0913 - past the window, the launch instance's identity
    key: str,
    now: datetime,
    window_seconds: float,
    *,
    session_id: str = "",
    pid: int | None = None,
    cause: str | None = None,
) -> str | None:
    """How the first turn of THIS launch instance already started, or None.

    The pending record is read before ``consumed/``: the reboot-resume store writes the
    consumed copy before it unlinks the pending one, so this order has no gap in which a
    delivery is invisible to both reads. A consumed record counts only for the instance
    that consumed it (F10): one carrying a ``session_id`` belongs to that session and
    process; one without (written before the session existed) counts only for a
    ``host_reboot`` launch whose stamp predates it.
    """
    stamps = rr.stamp_root()
    pending = rr._read_json(stamps / "pending" / f"{key}.json")
    if pending is not None:
        prompt = pending.get("prompt_file")
        if isinstance(prompt, str) and prompt not in {"", "existing"}:
            if not Path(prompt).exists():
                return "launcher"
    consumed = stamps / "consumed"
    for path in sorted(consumed.glob("*.json")) if consumed.is_dir() else []:
        record = rr._read_json(path)
        if record is None or record.get("key") != key:
            continue
        at = rr._parse_time(record.get("consumed_at"))
        recent = at is not None and 0 <= (now - at).total_seconds() <= window_seconds
        if not recent or record.get("delivered") not in NATIVE_DELIVERIES:
            continue
        if _consumed_by(record, key, at, session_id=session_id, pid=pid, cause=cause):
            return str(record["delivered"])
    return None


def _consumed_by(  # noqa: PLR0913 - the record, its time, the instance's three facts
    record: dict[str, Any],
    key: str,
    at: datetime | None,
    *,
    session_id: str,
    pid: int | None,
    cause: str | None,
) -> bool:
    if "session_id" in record:
        owner = record["session_id"]
        launched = record.get("launch_pid")
        mismatch = (
            isinstance(launched, int) and isinstance(pid, int) and launched != pid
        )
        return owner == session_id and not mismatch
    if cause != "host_reboot" or at is None:
        return False
    stamp = rr._read_json(rr.stamp_root() / f"{key}.json") or {}
    written = rr._parse_time(stamp.get("written_at"))
    return written is None or written <= at


def _live(record: dict[str, Any], now: float) -> bool:
    if record.get("state") not in LIVE_STATES:
        return False
    expires = _epoch(record.get("expires_at"))
    return expires is not None and now < expires


def typed_line(cause: str) -> str:
    return START_LINE if cause == "fresh_launch" else RESUME_LINE.format(cause=cause)


def _claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _within(cwd: str, project: object) -> bool:
    if not cwd or not isinstance(project, str) or not project:
        return False
    try:
        return Path(cwd).resolve().is_relative_to(Path(project).resolve())
    except (OSError, ValueError):
        return False


def beads_ui_installed(cwd: str) -> bool:
    """True when Claude Code would load the beads-ui plugin in ``cwd``: a user-scope install,
    or a project or local one for ``cwd``'s project, not switched off in user settings."""
    root = _claude_config_dir()
    enabled = _json_object(root / "settings.json").get("enabledPlugins")
    if isinstance(enabled, dict) and enabled.get(BEADS_UI_PLUGIN) is False:
        return False
    plugins = _json_object(root / "plugins" / "installed_plugins.json").get("plugins")
    entries = plugins.get(BEADS_UI_PLUGIN) if isinstance(plugins, dict) else None
    return any(
        isinstance(entry, dict)
        and (
            entry.get("scope") == "user"
            or (
                entry.get("scope") in {"project", "local"}
                and _within(cwd, entry.get("projectPath"))
            )
        )
        for entry in (entries if isinstance(entries, list) else ())
    )


def follow_up_line(settings: dict[str, Any], cwd: str) -> str | None:
    """``/show-beads-ui`` after the startup line, when the profile enables ``beads_ui_enabled`` and
    the plugin is installed for ``cwd``; never asked for where it would not resolve."""
    if settings.get("beads_ui_enabled") and beads_ui_installed(cwd):
        return BEADS_UI_LINE
    return None


# --- render --------------------------------------------------------------------------------


def render(record: dict[str, Any], stamp: dict[str, Any], now: datetime) -> str:
    """The resume template, or the start template for a fresh launch, filled from the
    launch record and the key's last stamp."""
    cause = str(record.get("cause") or "unknown")
    evidence = record.get("evidence") or {}
    ended = evidence.get("ended_at") or stamp.get("ended_at") or stamp.get("written_at")
    ended_at = rr._parse_time(ended)
    cwd = str(stamp.get("cwd") or record.get("cwd") or "")
    transcript = stamp.get("transcript_path") or rr._newest_transcript(cwd) or "none"
    fixed = {
        "__KEY__": str(record.get("key", "")),
        "__TMUX_SESSION__": str(record.get("tmux_session") or "unknown"),
        "__CWD__": cwd or "unknown",
        "__TRANSCRIPT_PATH__": str(transcript),
        "__CAUSE__": cause,
        "__EVIDENCE__": _evidence(evidence),
        "__ENDED_AT__": str(ended) if ended else "unknown",
        "__DOWNTIME__": rr.format_downtime(int((now - ended_at).total_seconds()))
        if ended_at is not None
        else "unknown",
    }
    template = START_TEMPLATE if cause == "fresh_launch" else RESUME_TEMPLATE
    return rr.render_template(template, fixed, list(stamp.get("children") or []))


# --- session start ---------------------------------------------------------------------------


def spawn_gate(key: str, pane: str, record: Path, session_id: str = "") -> int:
    """One detached ``gate-run``, in its own session so the hook's bound cannot kill it, its
    command text led by the label line (background-agent-lifecycle § Label every
    programmatic launch)."""
    argv = [sys.executable, "-m", "ai_chassis.launch_resume", "gate-run"]
    argv += ["--key", key, "--pane", pane, "--record", str(record)]
    argv += ["--session-id", session_id]
    command = f"{GATE_LABEL}\nexec {shlex.join(argv)}\n"
    log = _path(key, ".gate.log")
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no caller-supplied shell text
            ["/bin/sh", "-c", command],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
            close_fds=True,
        )
    return process.pid


@dataclass(frozen=True)
class _Launch:
    """One SessionStart: what the hook passed plus the injection seams."""

    key: str
    pane: str
    source: str
    launch_reason: str | None
    now: datetime
    classify: Classifier | None
    boot: Any
    spawn: Callable[[str, str, Path, str], int]
    session_id: str = ""
    pid: int | None = None


def session_start(  # noqa: PLR0913 - past launch_reason, keyword-only test seams
    raw_key: str,
    pane: str,
    source: str = "startup",
    launch_reason: str | None = None,
    *,
    session_id: str = "",
    classify: Classifier | None = None,
    boot: Any = None,
    now: datetime | None = None,
    spawn: Callable[[str, str, Path, str], int] = spawn_gate,
    hook_parent: int | None = None,
    owner: Callable[[int], int | None] = _owner_claude,
) -> int:
    """The launch decision, steps 1-4 of the design. Prints nothing, or exactly one
    ``<system-reminder>`` block. Always exits 0.

    ``classify`` and ``boot`` default to Phase 1's ``exit_cause.classify`` and
    ``host_boot.identity()``, imported only when needed. ``hook_parent`` is the hook's ``$PPID``,
    the ``claude`` that fired it; when the pane's top-level ``claude`` is a different process, this
    launch is a nested or headless ``claude`` that inherited the pane and the decision skips
    (ISSUE-REF). With no ``hook_parent`` the check is not run."""
    key = rr.sanitize_key(raw_key)
    if key is None or source in {"compact", "clear"}:
        return 0
    launch = _Launch(
        key,
        pane,
        source,
        launch_reason,
        now or rr._now(),
        classify,
        boot,
        spawn,
    )
    try:
        pane_pid = _session_pid(pane) if pane else None
        own = owner(hook_parent) if hook_parent is not None else None
        if pane and pane_pid is not None and own is not None and own != pane_pid:
            return _skip(key, "not-pane-owner", pane_pid=pane_pid, own_pid=own)
        launch = replace(launch, session_id=session_id, pid=pane_pid)
        return _session_start(launch)
    except Exception as error:  # noqa: BLE001 - fail open at the hook boundary
        return _skip(key, "error", type=type(error).__name__)


def _session_start(launch: _Launch) -> int:
    key = launch.key
    try:
        settings = supervisor.launch_resume_config()
    except supervisor.LaunchResumeConfigError as error:
        return _skip(key, "config-error", key_error=error.reason)
    mode = settings["mode"]
    if mode == "never":
        return _skip(key, "mode-never", mode=mode)
    now = launch.now.timestamp()
    with _record_lock(key):
        existing = rr._read_json(record_path(key))
        if existing is not None:
            if _same_instance(existing, launch.session_id, launch.pid):
                if _live(existing, now):
                    state = existing.get("state")
                    return _skip(key, "already-armed", state=state, mode=mode)
            elif existing.get("state") not in gm.TERMINAL:
                _retire(key, existing, now, launch.session_id)
    return _classify_and_deliver(launch, settings)


def _classify_and_deliver(launch: _Launch, settings: dict[str, Any]) -> int:
    key, mode = launch.key, settings["mode"]
    try:
        launch = replace(launch, boot=_boot(launch.boot))
    except RuntimeError as error:  # host_boot.BootIdentityUnavailable
        return _skip(key, "boot-unavailable", mode=mode, type=type(error).__name__)
    pending = _live_pending(launch, settings)
    if pending is not None and launch.launch_reason is None:
        # The boot sweep is the relauncher here: its claim says why this launch happens,
        # and the sweep already removed the raw stamp the classifier would have read.
        launch = replace(launch, launch_reason="boot-sweep")
    classified = _classified(launch, settings, pending_live=pending is not None)
    if classified is None:
        return 0
    decision, evidence = classified
    if pending is not None and pending.get("stop_reason"):
        evidence.setdefault("stop_reason", pending["stop_reason"])
    if decision.armed:
        return _arm_unless_delivered(launch, decision, evidence, settings, pending)
    return _skip(key, str(decision.reason), cause=decision.cause, mode=mode)


def _boot(boot: Any) -> Any:
    if boot is None:
        from ai_chassis import host_boot

        boot = host_boot.identity()
    return boot


def _pending_path(key: str) -> Path:
    return rr.stamp_root() / "pending" / f"{key}.json"


def _live_pending(launch: _Launch, settings: dict[str, Any]) -> dict[str, Any] | None:
    """The boot sweep's claim for this key, swept on this boot inside the TTL.

    An abandoned claim (another boot, or past the TTL) moves to ``expired/`` and is
    never acted on.
    """
    path = _pending_path(launch.key)
    record = rr._read_json(path)
    if record is None:
        return None
    ttl = int(settings["stamp_ttl_seconds"])
    if rr.pending_is_live(
        record, getattr(launch.boot, "boot_id", None), ttl, launch.now
    ):
        return record
    rr.expire(path, record, launch.key)
    _log("CC-LAUNCH-RESUME-EXPIRED", launch.key, "source=pending reason=abandoned")
    return None


def _arm_unless_delivered(
    launch: _Launch,
    decision: Decision,
    evidence: dict[str, Any],
    settings: dict[str, Any],
    pending: dict[str, Any] | None,
) -> int:
    """Step 3: a first turn the launcher already started gets nothing armed beside it.

    A live pending record is consumed exactly once, after its delivery: ``launcher`` when
    the launcher read and deleted the prompt file, ``context-only`` when a foreign first
    prompt was already waiting (the context is printed, nothing is typed), and ``gate``
    when this launch armed the composer gate instead, in which case the unread prompt
    file is deleted so a later launch of the pane cannot deliver it a second time.
    """
    max_wait = float(settings["max_wait_seconds"])
    delivered = native_delivery(
        launch.key,
        launch.now,
        max_wait,
        session_id=launch.session_id,
        pid=launch.pid,
        cause=decision.cause,
    )
    if delivered is None and pending is not None:
        if pending.get("prompt_file") == "existing":
            delivered = "context-only"
    # The consumed record names the instance that consumed it (F10).
    owned = {**(pending or {}), "session_id": launch.session_id}
    owned["launch_pid"] = launch.pid
    if delivered is None:
        watched = _arm(launch, decision, evidence, settings)
        if pending is not None:
            prompt = pending.get("prompt_file")
            if isinstance(prompt, str) and prompt:
                Path(prompt).unlink(missing_ok=True)
            delivered = "gate" if watched else "context-only"
            rr.consume(
                _pending_path(launch.key), owned, launch.key, delivered, launch.now
            )
        return 0
    if delivered == "context-only" and pending is not None:
        record = {"key": launch.key, "cause": decision.cause, "evidence": evidence}
        record["tmux_session"] = pending.get("tmux_session")
        rr._emit(render(record, pending, launch.now))
    if pending is not None:
        rr.consume(_pending_path(launch.key), owned, launch.key, delivered, launch.now)
    _log(
        "CC-LAUNCH-RESUME-NATIVE-CONSUMED",
        launch.key,
        f"delivered={delivered}",
        cause=decision.cause,
        mode=settings["mode"],
    )
    return 0


def _classified(
    launch: _Launch, settings: dict[str, Any], *, pending_live: bool
) -> tuple[Decision, dict[str, Any]] | None:
    """Classify and decide, logging CLASSIFIED; None after logging why it could not. A
    launch reason nothing backs is dropped and kept as ``ignored_launch_reason``."""
    mode = settings["mode"]
    try:
        result = _classify(
            launch.key, settings, launch.classify, launch.boot, launch.now
        )
    except ImportError:
        _skip(launch.key, "classifier-unavailable", mode=mode)
        return None
    except RuntimeError as error:  # host_boot.BootIdentityUnavailable
        _skip(launch.key, "boot-unavailable", mode=mode, type=type(error).__name__)
        return None
    reason = backed_reason(launch.key, launch.launch_reason, launch.boot, pending_live)
    decision = decide(str(result.cause), settings, reason)
    evidence = dict(result.evidence or {})
    if reason != launch.launch_reason:
        evidence["ignored_launch_reason"] = launch.launch_reason
    _log(
        "CC-LAUNCH-RESUME-CLASSIFIED",
        launch.key,
        _evidence(evidence),
        cause=decision.cause,
        classified=decision.classified_cause,
        launch_reason=decision.launch_reason or "-",
        mode=mode,
    )
    return decision, evidence


def _arm(
    launch: _Launch,
    decision: Decision,
    evidence: dict[str, Any],
    settings: dict[str, Any],
) -> bool:
    """Step 4. ``ARMED`` is written only once its watcher is spawned (F5): with no pane or
    a failed spawn the instance's record is a terminal ``SKIPPED``, which suppresses no
    later launch. The spawn runs under the record lock, so the watcher's first read
    waits for the record it is meant to drive. True when a watcher is running."""
    key, pane = launch.key, launch.pane
    stamp = _stamp(key)
    armed_at = launch.now.timestamp()
    record: dict[str, Any] = {
        "key": key,
        "session_id": launch.session_id,
        "tmux_session": stamp.get("tmux_session"),
        "pane_id": pane,
        "pid": launch.pid,
        "source": launch.source,
        "cause": decision.cause,
        "classified_cause": decision.classified_cause,
        "launch_reason": decision.launch_reason,
        "evidence": evidence,
        "mode": settings["mode"],
        "decision": decision.decision,
        "toggle": decision.toggle,
        "state": gm.ARMED,
        "armed_at": _iso(armed_at),
        "deadline": _iso(armed_at + float(settings["first_fire_delay_seconds"])),
        "rearm_at": None,
        "expires_at": _iso(armed_at + float(settings["max_wait_seconds"])),
        "prompt_path": str(_path(key, ".prompt")),
        "line": typed_line(decision.cause),
        "follow_up": follow_up_line(settings, str(stamp.get("cwd") or os.getcwd())),
        "leg": None,
        "fired_at": None,
        "transitions": [],
    }
    text = render(record, stamp, launch.now)
    rr._write_text(_path(key, ".prompt"), text)
    failure: tuple[str, str] | None = None
    with _record_lock(key):
        if not pane:
            failure = ("no-pane", "-")
        else:
            try:
                watcher = launch.spawn(key, pane, record_path(key), launch.session_id)
            except Exception as error:  # noqa: BLE001 - a failed spawn is a logged skip
                failure = ("spawn-failed", type(error).__name__)
        if failure is None:
            _store(key, record, armed_at)
        else:
            _store(key, {**record, "state": gm.SKIPPED, "reason": failure[0]}, armed_at)
    rr._emit(text)
    mode = settings["mode"]
    if failure is not None:
        reason, error = failure
        extra = {"type": error} if error != "-" else {}
        _skip(key, reason, **extra, cause=decision.cause, mode=mode)
        return False
    _log(
        "CC-LAUNCH-RESUME-ARMED",
        key,
        f"deadline={record['deadline']} expires_at={record['expires_at']} "
        f"watcher_pid={watcher}",
        cause=decision.cause,
        mode=mode,
    )
    return True


# --- the gate watcher ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Seams:
    """The watcher's I/O, injectable so its loop is testable without tmux or a clock."""

    sampler: Callable[[], Any] | None
    fire: Callable[[str, str, str], Any] | None
    clock: Callable[[], float]
    sleep: Callable[[float], None]


@contextmanager
def _instance_lock(
    path: Path, token: str, still_mine: Callable[[], bool], seams: _Seams, until: float
) -> Iterator[bool]:
    """True while this watcher alone holds its instance's lock, with ``token`` written in.

    On contention (F4) the holder's token is read back: the same instance means another
    watcher already drives this record, so this one exits (False). A different token is a
    watcher of an instance this record replaced, which exits at its next sample, so this
    one waits for it, until the record stops being ours or expires.
    """
    if _fcntl is None:
        yield True
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                break
            except BlockingIOError:
                try:
                    holder = path.read_text(encoding="utf-8")
                except OSError:
                    holder = ""
                if holder == token or not still_mine() or seams.clock() >= until:
                    yield False
                    return
                seams.sleep(LOCK_RETRY_SECONDS)
        os.ftruncate(fd, 0)
        os.pwrite(fd, token.encode("utf-8"), 0)
        try:
            yield True
        finally:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
    finally:
        os.close(fd)


def classify_sample(sample: Any) -> gm.Observation:
    """A ``ComposerSample`` as the gate's observation (design § Composer gate)."""
    if sample.classification == "empty-composer" and sample.composer_recognized:
        return gm.COMPOSER_EMPTY
    if sample.classification in {"composer-nonempty", "composer-suggestion"}:
        return gm.COMPOSER_TEXT
    return gm.COMPOSER_UNOBSERVABLE


def _tmux_command() -> tuple[str, ...]:
    return (rr._tmux(),)


def _default_sampler(key: str, pane: str) -> Callable[[], Any]:
    from ai_chassis.prompt_injection.readiness import ComposerReadiness
    from ai_chassis.prompt_injection.targets import SessionTarget

    readiness = ComposerReadiness(_tmux_command())
    target = SessionTarget(
        key=key, pane_id=pane, fingerprint="", transcript_path=None, cwd=None
    )
    return lambda: readiness.sample(target)


def _default_fire(
    key: str, pane: str, line: str, *, session_id: str = "", armed_at: object = None
) -> Any:
    """One typed fire through the library; returns its ``InjectionOutcome``. The launch
    instance rides on the injector, so a fire held at a boundary is dropped at drain once
    its instance has ended or been replaced (F13)."""
    from ai_chassis.prompt_injection.config import load_config
    from ai_chassis.prompt_injection.injector import LaunchResumeInjector
    from ai_chassis.prompt_injection.payload import PromptPayload
    from ai_chassis.prompt_injection.targets import TargetUnresolved, resolve_target

    try:
        target = resolve_target(pane_id=pane, key=key, tmux_command=_tmux_command())
    except TargetUnresolved as error:
        return error
    config = load_config()
    config["completion_timeout_seconds"] = 0
    injector = LaunchResumeInjector(
        tmux_command=_tmux_command(),
        config=config,
        session_id=session_id,
        armed_at=None if armed_at is None else str(armed_at),
    )
    return injector.run(target, PromptPayload(line), ready_wait=FIRE_READY_WAIT_SECONDS)


def outcome_observation(outcome: Any) -> tuple[gm.Observation, str]:
    """The injector's outcome as the gate's observation, plus its detail for the log.

    Once an Enter went, the fire counts as sent whatever the landing said: a second fire
    after an unproven landing would be a double submission.
    """
    reason = getattr(outcome, "reason", None)
    if not hasattr(outcome, "state"):  # TargetUnresolved
        return gm.fire_refused(str(reason or "no-pane")), f"reason={reason}"
    detail = f"attempt={outcome.attempt_id} state={outcome.state}"
    if outcome.landing:
        detail += f" landing={outcome.landing}"
    if outcome.state == "HELD":
        return gm.FIRE_HELD, detail
    if outcome.submitted or outcome.state in {"SENT", "QUEUED"}:
        return gm.FIRE_SENT, detail
    return gm.fire_refused(str(reason or outcome.state.lower())), detail


@dataclass
class _Watch:
    key: str
    pane: str
    path: Path
    record: dict[str, Any]
    gate: gm.Gate
    armed_at: float
    interval: float
    last_marker: object = None
    nonce: str | None = None
    superseded: bool = False

    @property
    def session_id(self) -> str:
        return _session_of(self.record)

    @property
    def floor(self) -> float:
        """The submit marker's whole-second ``at`` compares against the truncated arm time."""
        return float(int(self.armed_at))

    def mine(self) -> bool:
        current = rr._read_json(self.path)
        if not _owns(current, self.session_id, self.record.get("armed_at")):
            self.superseded = True
        return not self.superseded

    def _end_is_mine(self, end: dict[str, Any]) -> bool:
        """F3: by session and process first, by time only when the end record cannot say.

        A ``claude --resume`` relaunch keeps the session id, so the previous run's end
        record names the same session; its process (or, without one, an end time before
        this arm, compared exactly rather than truncated) is what keeps it from abandoning
        the new run."""
        ended_by, mine = str(end.get("session_id") or ""), self.session_id
        if ended_by and mine and ended_by != mine:
            return False
        end_pid, my_pid = end.get("pid"), self.record.get("pid")
        if ended_by and mine and isinstance(end_pid, int) and isinstance(my_pid, int):
            return end_pid == my_pid
        ended = _epoch(end.get("ended_at"))
        return ended is not None and ended >= self.armed_at

    def ended(self) -> bool:
        end = rr._read_json(_path(self.key, ".end.json"))
        if end is not None and self._end_is_mine(end):
            return True
        pid = self.record.get("pid")
        return isinstance(pid, int) and not _pid_alive(pid)

    def submit(self) -> gm.Observation | None:
        marker = rr._read_json(_path(self.key, ".submit.json"))
        if marker is None:
            return None
        at = _epoch(marker.get("at"))
        if at is None or at < self.floor or marker.get("at") == self.last_marker:
            return None
        submitted_by = str(marker.get("session_id") or "")
        if submitted_by and self.session_id and submitted_by != self.session_id:
            return None
        self.last_marker = marker.get("at")
        return gm.MANUAL_SUBMIT if marker.get("origin") == "manual" else gm.OWN_SUBMIT

    def write_firing(self, line: str) -> None:
        """F6: the marker names this instance and one fire, so the submit hook can tell
        this fire from a later human prompt that happens to carry the same text."""
        self.nonce = secrets.token_hex(8)
        rr._write_text(
            _path(self.key, ".firing"), f"{self.session_id}\n{self.nonce}\n{line}\n"
        )

    def follow_up(self, send: Callable[[str, str, str], Any]) -> None:
        """The record's follow-up line (``/show-beads-ui``), typed once after the line was sent and
        never retried: a refusal is logged and costs only the board."""
        line = self.record.get("follow_up")
        if not isinstance(line, str) or not line:
            return
        self.write_firing(line)
        try:
            outcome, detail = outcome_observation(send(self.key, self.pane, line))
        finally:
            self.clear_firing()
        result = "sent" if outcome == gm.FIRE_SENT else "refused"
        _log(
            "CC-LAUNCH-RESUME-FOLLOW-UP",
            self.key,
            f"line={line} result={result} {detail}",
            cause=self.record.get("cause"),
            mode=self.record.get("mode"),
        )

    def clear_firing(self) -> None:
        """Remove this fire's marker, never another instance's."""
        if self.nonce is None:
            return
        path = _path(self.key, ".firing")
        try:
            head = path.read_text(encoding="utf-8").split("\n")[:2]
        except OSError:
            head = []
        if head == [self.session_id, self.nonce]:
            path.unlink(missing_ok=True)
        self.nonce = None

    def apply(self, transition: gm.Transition, now: float, extra: str = "") -> None:
        if transition.changed:
            self.record["transitions"].append(
                {
                    "at": _iso(now),
                    "from": transition.previous,
                    "to": transition.state,
                    "observation": transition.observation.label,
                }
            )
            self.record["state"] = transition.state
            rearm = self.gate.rearm_at
            self.record["rearm_at"] = _iso(rearm) if rearm is not None else None
            if transition.state in {gm.FIRED, gm.HELD}:
                self.record["leg"] = "typed"
            if transition.state == gm.FIRED:
                self.record["fired_at"] = _iso(now)
            with _record_lock(self.key):
                if not self.mine():
                    return
                _write(self.path, self.record)
        for action in transition.actions:
            if action.kind == "log":
                detail = " ".join(part for part in (action.detail, extra) if part)
                _log(
                    action.stage,
                    self.key,
                    detail,
                    cause=self.record.get("cause"),
                    mode=self.record.get("mode"),
                )


def gate_run(  # noqa: PLR0913 - past path, keyword-only test seams
    raw_key: str,
    pane: str,
    path: Path,
    *,
    session_id: str = "",
    sampler: Callable[[], Any] | None = None,
    fire: Callable[[str, str, str], Any] | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Drive one launch instance's record to a terminal state; a second watcher of the
    same instance exits at once, and a watcher whose instance was replaced stops."""
    key = rr.sanitize_key(raw_key)
    if key is None:
        return 0
    seams = _Seams(sampler, fire, clock, sleep)
    try:
        with _record_lock(key):
            record = rr._read_json(path)
        if record is None:
            return 0
        if _session_of(record) != session_id:
            return _skip(key, "superseded", session=instance_name(session_id))
        armed = record.get("armed_at")
        token = f"{session_id}|{armed}"
        until = _epoch(record.get("expires_at")) or clock()

        def still_mine() -> bool:
            return _owns(rr._read_json(path), session_id, armed)

        lock = instance_lock_path(key, session_id)
        with _instance_lock(lock, token, still_mine, seams, until) as alone:
            if not alone:
                return 0
            return _gate_run(key, pane, path, session_id, seams)
    except Exception as error:  # noqa: BLE001 - a watcher never raises into nothing
        return _skip(key, "error", type=type(error).__name__)


def _watch(
    key: str, pane: str, path: Path, session_id: str, clock: Callable[[], float]
) -> _Watch | None:
    """The record's gate rebuilt from the record, or None after logging why not to run."""
    record = rr._read_json(path)
    if record is None or record.get("state") in gm.TERMINAL:
        return None
    if _session_of(record) != session_id:
        return None
    if record.get("state") == gm.FIRING:
        # A watcher died mid-send: whether Enter went is unknowable, so never send blind.
        _skip(key, "interrupted-fire", cause=record.get("cause"))
        return None
    try:
        settings = supervisor.launch_resume_config()
    except supervisor.LaunchResumeConfigError as error:
        _skip(key, "config-error", key_error=error.reason)
        return None
    armed_at = _epoch(record.get("armed_at")) or clock()
    gate = gm.Gate(
        gm.GateConfig.from_settings(settings),
        armed_at,
        state=str(record.get("state") or gm.ARMED),
        rearm_at=_epoch(record.get("rearm_at")),
    )
    # The instance's own timers, fixed at arm; the re-arm wait and the typed switch are
    # read now, so a live session can change them for a launch already armed.
    gate.deadline = _epoch(record.get("deadline")) or gate.deadline
    gate.expires_at = _epoch(record.get("expires_at")) or gate.expires_at
    record.setdefault("transitions", [])
    interval = float(settings["sample_interval_seconds"])
    return _Watch(key, pane, path, record, gate, armed_at, interval)


def _gate_run(key: str, pane: str, path: Path, session_id: str, seams: _Seams) -> int:
    watch = _watch(key, pane, path, session_id, seams.clock)
    if watch is None:
        return 0
    gate, record = watch.gate, watch.record
    sample = seams.sampler or _default_sampler(key, pane)
    send = seams.fire or functools.partial(
        _default_fire, session_id=session_id, armed_at=record.get("armed_at")
    )
    try:
        while watch.mine():
            now = seams.clock()
            if watch.ended():
                observation = gm.SESSION_ENDED
            else:
                observation = watch.submit() or classify_sample(sample())
                if observation == gm.COMPOSER_EMPTY:
                    # F7: the marker is read again after the sample, the last read before
                    # an empty box can become a send, so a submit that landed while the
                    # pane was captured silences instead of firing. What remains is inside
                    # the library: between its own two-sample readiness check and the
                    # Enter, a human prompt submitted in that instant empties the box, the
                    # check still passes and the line is typed after it (Claude Code queues
                    # it as a second prompt). The window is the injector's staging time,
                    # well under FIRE_READY_WAIT_SECONDS.
                    observation = watch.submit() or observation
            watch.apply(gate.step(observation, now), now)
            if gate.state == gm.FIRING and not watch.superseded:
                line = str(record.get("line") or typed_line(str(record.get("cause"))))
                watch.write_firing(line)
                outcome, detail = outcome_observation(send(key, pane, line))
                fired = seams.clock()
                watch.apply(gate.step(outcome, fired), fired, detail)
                watch.clear_firing()
                if gate.state == gm.FIRED and not watch.superseded:
                    watch.follow_up(send)
            if gate.state in gm.TERMINAL or watch.superseded:
                return 0
            seams.sleep(watch.interval)
        return 0
    finally:
        watch.clear_firing()


# --- status, explain, render, host-boot ---------------------------------------------------------


def _current_boot_time(last_boot: dict[str, Any] | None) -> tuple[float | None, str]:
    """The live boot's start time, else the stored one, and which of the two it is."""
    try:
        from ai_chassis import host_boot

        return float(host_boot.identity().boot_time), "live"
    except (ImportError, RuntimeError):
        pass
    stored = (last_boot or {}).get("boot_time")
    if isinstance(stored, int | float) and not isinstance(stored, bool):
        return float(stored), "last-boot.json"
    return None, "unknown"


def status_report() -> dict[str, Any]:
    """Every launch record, the boot record, and the sessions this boot's launches found
    killed (cause ``host_reboot`` or ``terminal_quit_or_crash``, armed on this boot)."""
    rows = []
    for path in _records():
        record = rr._read_json(path) or {}
        transitions = record.get("transitions") or []
        last = transitions[-1].get("at") if transitions else record.get("armed_at")
        rows.append(
            {
                "key": record.get("key", path.stem),
                "cause": record.get("cause"),
                "state": record.get("state"),
                "leg": record.get("leg"),
                "last_transition_at": last,
                "armed_at": record.get("armed_at"),
            }
        )
    last_boot = rr._read_json(launch_root() / _LAST_BOOT)
    boot_time, boot_source = _current_boot_time(last_boot)

    def this_boot(row: dict[str, Any]) -> bool:
        armed = _epoch(row["armed_at"])
        return boot_time is None or (armed is not None and armed >= boot_time)

    killed = [r for r in rows if r["cause"] in KILLED_CAUSES and this_boot(r)]
    return {
        "records": rows,
        "last_boot": last_boot,
        "current_boot": {"boot_time": boot_time, "source": boot_source},
        "killed_sessions": killed,
    }


def status(*, as_json: bool = False) -> int:
    report = status_report()
    if as_json:
        print(json.dumps(report, sort_keys=True, default=str))
        return 0
    for row in report["records"]:
        print(
            f"{row['key']} cause={row['cause']} state={row['state']} "
            f"leg={row['leg']} last={row['last_transition_at']}"
        )
    if not report["records"]:
        print("no launch records")
    boot = report["last_boot"]
    if boot is None:
        print("last-boot: none recorded")
    else:
        print(
            f"last-boot: boot_id={boot.get('boot_id')} rebooted={boot.get('rebooted')} "
            f"previous={boot.get('previous_boot_id')} read_at={boot.get('read_at')} "
            f"os={boot.get('os')}"
        )
    killed = report["killed_sessions"]
    print(
        f"killed sessions on this boot (boot from {report['current_boot']['source']}): "
        + (
            ", ".join(
                f"{r['key']} cause={r['cause']} state={r['state']}" for r in killed
            )
            or "none"
        )
    )
    return 0


def _explained_reason(
    key: str,
    launch_reason: str | None,
    boot: Any,
    settings: dict[str, Any],
    moment: datetime,
) -> tuple[str | None, str | None]:
    """The reason as supplied (``boot-sweep`` for a live claim) and as session-start honours it,
    without moving an abandoned claim the way session-start does."""
    pending = rr._read_json(_pending_path(key))
    ttl = int(settings["stamp_ttl_seconds"])
    pending_live = pending is not None and rr.pending_is_live(
        pending, getattr(boot, "boot_id", None), ttl, moment
    )
    if launch_reason is None and pending_live:
        launch_reason = "boot-sweep"
    return launch_reason, backed_reason(key, launch_reason, boot, pending_live)


def explanation(
    raw_key: str,
    launch_reason: str | None = None,
    *,
    classify: Classifier | None = None,
    boot: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """``session-start``'s decision for ``key`` dry-run against the live stamps and config:
    the same checks in the same order, writing no record and no log line."""
    key = rr.sanitize_key(raw_key)
    if key is None:
        raise ValueError("the key is not a valid resume key")
    moment = now or datetime.now(UTC)
    result: dict[str, Any] = {"key": key}
    try:
        settings = supervisor.launch_resume_config()
    except supervisor.LaunchResumeConfigError as error:
        return {
            **result,
            "decision": "skip",
            "reason": "config-error",
            "detail": error.reason,
        }
    result["mode"] = settings["mode"]
    # The next launch is a new instance, so a record still waiting is superseded by it
    # (F2), never a reason for that launch to skip.
    current = rr._read_json(record_path(key))
    if current is not None and _live(current, moment.timestamp()):
        result["supersedes"] = current.get("state")
    try:
        boot = _boot(boot)
        classified = _classify(key, settings, classify, boot, moment)
    except ImportError:
        return {**result, "decision": "skip", "reason": "classifier-unavailable"}
    except RuntimeError as error:
        return {
            **result,
            "decision": "skip",
            "reason": "boot-unavailable",
            "detail": type(error).__name__,
        }
    supplied, launch_reason = _explained_reason(
        key, launch_reason, boot, settings, moment
    )
    decision = decide(str(classified.cause), settings, launch_reason)
    value = (
        settings["mode"]
        if decision.toggle == "mode"
        else settings["scenarios"].get(decision.cause)
    )
    evidence = dict(classified.evidence or {})
    if launch_reason != supplied:
        evidence["ignored_launch_reason"] = supplied
    outcome, leg, reason = decision.decision, "none", decision.reason
    if decision.armed:
        window = float(settings["max_wait_seconds"])
        delivered = native_delivery(key, moment, window, cause=decision.cause)
        if delivered is not None:
            outcome, leg, reason = (
                "native-consumed",
                "launcher",
                f"delivered={delivered}",
            )
        elif settings["typed_fallback_enabled"]:
            leg = "typed"
        else:
            reason = "typed-off"
    return {
        **result,
        "cause": decision.cause,
        "classified_cause": decision.classified_cause,
        "launch_reason": decision.launch_reason,
        "evidence": evidence,
        "toggle": decision.toggle,
        "toggle_value": value,
        "decision": outcome,
        "reason": reason,
        "leg": leg,
        "first_fire_delay_seconds": settings["first_fire_delay_seconds"],
        "rearm_after_clear_seconds": settings["rearm_after_clear_seconds"],
    }


def explain(  # noqa: PLR0913 - past launch_reason, keyword-only seams and --json
    raw_key: str,
    launch_reason: str | None = None,
    *,
    classify: Classifier | None = None,
    boot: Any = None,
    now: datetime | None = None,
    as_json: bool = False,
) -> int:
    """What the next launch of ``key`` would do and the rule that decides it. Writes nothing."""
    try:
        found = explanation(
            raw_key, launch_reason, classify=classify, boot=boot, now=now
        )
    except ValueError as error:
        print(f"explain: {error}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(found, sort_keys=True, default=str))
        return 0
    if "cause" not in found:
        extra = " ".join(f"{k}={found[k]}" for k in ("state", "detail") if k in found)
        print(
            f"key={found['key']} decision=skip reason={found['reason']} {extra}".rstrip()
        )
        return 0
    print(f"key={found['key']}")
    print(
        f"cause={found['cause']} classified={found['classified_cause']} "
        f"launch_reason={found['launch_reason'] or '-'}"
    )
    print(f"evidence: {_evidence(found['evidence'])}")
    print(
        f"mode={found['mode']} toggle={found['toggle']} value={found['toggle_value']}"
    )
    print(
        f"decision={found['decision']} reason={found['reason'] or '-'} leg={found['leg']} "
        f"first_fire_delay_s={found['first_fire_delay_seconds']}"
    )
    if "supersedes" in found:
        print(
            f"supersedes: the waiting record (state={found['supersedes']}) "
            "ends ABANDONED reason=superseded"
        )
    return 0


def render_command(path: Path) -> int:
    record = rr._read_json(path)
    if record is None:
        print(f"render: {path} is not a JSON record", file=sys.stderr)
        return 2
    sys.stdout.write(
        render(record, _stamp(str(record.get("key", ""))), datetime.now(UTC))
    )
    return 0


def host_boot_command() -> int:
    """``python -m ai_chassis.host_boot record``, Phase 1's own entry point, unchanged:
    it owns the ``LAUNCH-RESUME-BOOT`` line and the unavailable-provider exit 0."""
    from ai_chassis import host_boot

    return host_boot.main(["record"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ai_chassis.launch_resume")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("session-start")
    start.add_argument("--key", required=True)
    start.add_argument("--pane", default="")
    start.add_argument("--source", default="startup")
    start.add_argument("--launch-reason", default=None)
    start.add_argument("--session-id", default="")
    # ISSUE-REF: the hook's $PPID, the claude that fired it; the decision arms only when it
    # is the pane's top-level claude.
    start.add_argument("--hook-parent", type=int, default=None)
    run = commands.add_parser("gate-run")
    run.add_argument("--key", required=True)
    run.add_argument("--pane", required=True)
    run.add_argument("--record", required=True, type=Path)
    run.add_argument("--session-id", default="")
    render_parser = commands.add_parser("render")
    render_parser.add_argument("--record", required=True, type=Path)
    commands.add_parser("host-boot")
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
    explain_parser = commands.add_parser("explain")
    explain_parser.add_argument("--key", required=True)
    explain_parser.add_argument("--launch-reason", default=None)
    explain_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "session-start":
        return session_start(
            args.key,
            args.pane,
            args.source,
            args.launch_reason,
            session_id=args.session_id,
            hook_parent=args.hook_parent,
        )
    if args.command == "gate-run":
        return gate_run(args.key, args.pane, args.record, session_id=args.session_id)
    if args.command == "render":
        return render_command(args.record)
    if args.command == "host-boot":
        return host_boot_command()
    if args.command == "status":
        return status(as_json=args.json)
    return explain(args.key, args.launch_reason, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
