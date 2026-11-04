"""The reboot-signal producer for launch-resume (ISSUE-REF, generalised by ISSUE-REF).

Designs: ``docs/designs/issue-ref-example-project-ec2-reboot-auto-resume.md`` (the stamps and the
sweep) and ``docs/designs/issue-ref-typing-aware-launch-resume.md`` (what a launch does).

The signal is a per-session STAMP carrying the host's boot id, refreshed every five
minutes by ``ai-harness-session-stamp.timer`` on Linux and the
``com.ai-harness.session-stamp`` LaunchAgent on macOS (``stamp``). After a Linux boot,
``ai-harness-reboot-resume.service`` runs ``boot-sweep`` once: every stamp whose boot id
no longer matches becomes a rendered first prompt in the file the ``ai c -R`` launcher
already reads (``cc-resume-prompt-<tmux_session>``) plus a ``pending/`` record. What the
relaunched session does next is ``python -m ai_chassis.launch_resume session-start``'s
decision, which consumes the pending record; this module types nothing.

Layout under ``${AIH_RESUME_ROOT:-~/.claude/resume}/boot-stamps/``::

    <key>.json              raw stamp, rewritten by every stamp tick
    pending/<key>.json      swept at boot, waiting for the relaunched session
    consumed/<key>-<ts>.json  the session started and the record was acted on
    expired/<key>-<ts>.json   older than launch_resume.stamp_ttl_seconds, never acted on
    last-sweep.json         the newest sweep's summary, read by ``status``

Testability overrides, every one read at call time:

``AIH_BOOT_ID_FILE``
    the boot id file (default ``/proc/sys/kernel/random/boot_id``).
``AIH_BOOT_TIME_EPOCH``
    the boot time in epoch seconds (default: ``btime`` from ``<proc>/stat``).
``AIH_PROC_ROOT``
    the proc tree for ``<pid>/stat``, ``cmdline``, ``comm``, ``environ``, ``cwd`` and
    ``uptime`` (default ``/proc``).
``AIH_TMUX_COMMAND``
    the tmux executable (default ``tmux``).
``AIH_RESUME_ROOT``
    the resume store root (default ``~/.claude/resume``).
``XDG_STATE_HOME``
    the launcher prompt file is
    ``${XDG_STATE_HOME:-~/.local/state}/ai-cli-utils/cc-resume-prompt-<tmux_session>``.
``AIH_REBOOT_RESUME_NOW``
    an ISO-8601 override of "now" for the stop-reason and TTL arithmetic.

macOS has no proc tree: there the claude process under a pane is found from one
``ps -o pid=,ppid=,command= -ax`` (:class:`DarwinProcesses`), and with no readable environ
the key is the tmux session name stripped of ``c-``/``c-r-``.

PRIVACY: ``stamp`` reads exactly ONE variable from ``/proc/<pid>/environ``
(``CLAUDE_CODE_TASK_LIST_ID``), by splitting on NUL and matching that prefix. No other
environ content is ever decoded, printed or logged.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ai_chassis import context_supervisor as supervisor
from ai_chassis import detached_ledger
from ai_chassis import host_boot
from ai_chassis.exit_cause import NOT_EXITS
from ai_chassis.session_key import refusal as session_key_refusal

STAMP_DIRECTORY = "boot-stamps"
LAST_SWEEP = "last-sweep.json"
TASK_LIST_VARIABLE = b"CLAUDE_CODE_TASK_LIST_ID="
LAUNCHER_SESSION = re.compile(r"^c-(?:r-)?(?P<name>.+)$")
TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / ".claude"
    / "hooks"
    / "launch-resume-prompt.md"
)
INVENTORY_MAX_ROWS = 20
ARGV_MAX_CHARACTERS = 200
ARGV_MIN_CHARACTERS = 20
ARGV_SHRINK_STEP = 10
SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:password|passwd|token|secret|api[_-]?key|authorization|bearer)[=:])\S+"
)
SECRET_FLAGS = frozenset({"-p", "--password"})
REDACTED = "<redacted>"
# The launcher passes the prompt as ONE argv element; the design bounds it under 6 KB.
PROMPT_MAX_BYTES = 6144
# Paths are shortened from the front to this many bytes before the body itself is cut.
PATH_MIN_TAIL_BYTES = 80
ELLIPSIS = "..."
STAT_STARTTIME_INDEX = 19
STAT_PPID_INDEX = 1
PS_COMMAND = ("ps", "-o", "pid=,ppid=,command=", "-ax")
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400
FRIDAY = 4
SATURDAY = 5
THIRD_WEEK = range(15, 22)
WEEKEND_STOP_FROM_FRIDAY = (23, 45, 0)
WEEKEND_STOP_UNTIL_SATURDAY = (0, 15, 0)
PATCH_WINDOW = ((3, 45, 0), (8, 15, 0))


# --- environment ---------------------------------------------------------------------


def _now() -> datetime:
    override = os.environ.get("AIH_REBOOT_RESUME_NOW")
    if override:
        parsed = _parse_time(override)
        if parsed is not None:
            return parsed
    return datetime.now(UTC)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _proc_root() -> Path:
    return Path(os.environ.get("AIH_PROC_ROOT", "/proc"))


def current_boot_id() -> str | None:
    """This boot's identity from :mod:`ai_chassis.host_boot`, or None when unreadable."""
    try:
        return host_boot.identity().boot_id
    except host_boot.BootIdentityUnavailable:
        return None


def boot_time() -> datetime | None:
    try:
        return datetime.fromtimestamp(host_boot.identity().boot_time, UTC)
    except (host_boot.BootIdentityUnavailable, ValueError, OverflowError, OSError):
        return None


def resume_root() -> Path:
    return Path(os.environ.get("AIH_RESUME_ROOT", Path.home() / ".claude" / "resume"))


def stamp_root() -> Path:
    return resume_root() / STAMP_DIRECTORY


def prompt_name_refusal(tmux_session: str) -> str | None:
    """Why ``tmux_session`` cannot name the launcher's prompt file, else None.

    The ai c wrapper reads ``cc-resume-prompt-$tmux_session`` with the name raw, so a
    valid name stays raw and the rest are refused: tmux accepts ``c-x/../../y``, which
    would put the file outside ``ai-cli-utils/`` (ISSUE-REF)."""
    if not tmux_session:
        return "empty tmux session name"
    reason = session_key_refusal(tmux_session)
    return None if reason is None else f"tmux session name {tmux_session!r} {reason}"


def prompt_path(tmux_session: str) -> Path:
    reason = prompt_name_refusal(tmux_session)
    if reason is not None:
        raise ValueError(reason)
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "ai-cli-utils" / f"cc-resume-prompt-{tmux_session}"


def _tmux() -> str:
    return os.environ.get("AIH_TMUX_COMMAND") or "tmux"


def sanitize_key(raw: str) -> str | None:
    """The resume_derive_key normalisation: refuse traversal, replace the rest."""
    if not raw or "/" in raw or "\\" in raw or ".." in raw:
        return None
    return re.sub(r"[^A-Za-z0-9._-]", "_", raw) or None


# --- small file primitives -----------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _raw_stamps(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    for path in sorted(root.glob("*.json")):
        if path.name != LAST_SWEEP:
            yield path


def _archive(source: Path, directory: str, record: dict[str, Any], key: str) -> Path:
    """Write ``record`` to ``<directory>/<key>-<ts>.json`` and remove ``source``."""
    target = (
        stamp_root() / directory / f"{key}-{int(_now().timestamp())}-{os.getpid()}.json"
    )
    _write_json(target, record)
    source.unlink(missing_ok=True)
    return target


def _log(stage: str, key: str, detail: str) -> None:
    supervisor.log_stage(stage, key, detail)


# --- proc tree -----------------------------------------------------------------------


def _stat_fields(pid: int) -> list[str] | None:
    try:
        text = (_proc_root() / str(pid) / "stat").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    _, separator, rest = text.rpartition(")")
    fields = rest.split()
    if not separator or len(fields) <= STAT_STARTTIME_INDEX:
        return None
    return fields


def _argv(pid: int) -> list[str]:
    try:
        raw = (_proc_root() / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _is_claude(pid: int) -> bool:
    try:
        comm = (_proc_root() / str(pid) / "comm").read_text(encoding="utf-8").strip()
    except OSError:
        comm = ""
    argv = _argv(pid)
    return comm == "claude" or (bool(argv) and Path(argv[0]).name == "claude")


def _children_map() -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    try:
        entries = list(_proc_root().iterdir())
    except OSError:
        return children
    for entry in entries:
        if not entry.name.isdigit():
            continue
        fields = _stat_fields(int(entry.name))
        if fields is None:
            continue
        children.setdefault(int(fields[STAT_PPID_INDEX]), []).append(int(entry.name))
    return children


def _descendants(pid: int, children: dict[int, list[int]]) -> list[int]:
    """Breadth-first, so the nearest match wins in :func:`_find_claude`."""
    found: list[int] = []
    frontier = sorted(children.get(pid, []))
    while frontier:
        found.extend(frontier)
        frontier = sorted(c for parent in frontier for c in children.get(parent, []))
    return found


def _find_claude(
    pane_pid: int,
    children: dict[int, list[int]],
    is_claude: Callable[[int], bool] = _is_claude,
) -> int | None:
    for pid in [pane_pid, *_descendants(pane_pid, children)]:
        if is_claude(pid):
            return pid
    return None


def _task_list_id(pid: int) -> str | None:
    """CLAUDE_CODE_TASK_LIST_ID from one process's environ; nothing else is decoded."""
    try:
        raw = (_proc_root() / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    for entry in raw.split(b"\0"):
        if entry.startswith(TASK_LIST_VARIABLE):
            return entry[len(TASK_LIST_VARIABLE) :].decode("utf-8", "replace") or None
    return None


def _cwd(pid: int) -> str:
    try:
        return os.readlink(_proc_root() / str(pid) / "cwd")
    except OSError:
        return ""


def _ps(argv: list[str]) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv
        argv, capture_output=True, text=True, check=False, timeout=10
    )
    if result.returncode != 0:
        raise OSError(f"{argv[0]} exited {result.returncode}")
    return result.stdout


class ProcTree:
    """The Linux process source: the ``/proc`` (or ``AIH_PROC_ROOT``) walk."""

    def children(self) -> dict[int, list[int]]:
        return _children_map()

    def is_claude(self, pid: int) -> bool:
        return _is_claude(pid)

    def parent(self, pid: int) -> int | None:
        fields = _stat_fields(pid)
        return int(fields[STAT_PPID_INDEX]) if fields is not None else None

    def task_list_id(self, pid: int) -> str | None:
        return _task_list_id(pid)

    def cwd(self, pid: int) -> str:
        return _cwd(pid)

    def inventory(
        self, claude_pid: int, children: dict[int, list[int]]
    ) -> list[dict[str, Any]]:
        return _inventory(claude_pid, children)


class DarwinProcesses:
    """The macOS process source (ISSUE-REF F19): Darwin has no ``/proc``, so one
    ``ps -o pid=,ppid=,command= -ax`` gives the tree. ``run`` is injectable, the
    :class:`ai_chassis.host_boot.DarwinProvider` pattern. Another process's environ and
    cwd are not readable without privileges, so the key falls back to the tmux name and
    the cwd is unknown; the inventory carries pid and argv, with no elapsed time."""

    def __init__(self, run: Callable[[list[str]], str] | None = None) -> None:
        self._run = run or _ps
        self._table: dict[int, tuple[int, str]] | None = None

    def _rows(self) -> dict[int, tuple[int, str]]:
        if self._table is None:
            try:
                text = self._run(list(PS_COMMAND))
            except (OSError, subprocess.SubprocessError):
                text = ""
            table: dict[int, tuple[int, str]] = {}
            for line in text.splitlines():
                pid, ppid, command = [*line.split(None, 2), "", "", ""][:3]
                if pid.isdigit() and ppid.isdigit():
                    table[int(pid)] = (int(ppid), command)
            self._table = table
        return self._table

    def children(self) -> dict[int, list[int]]:
        children: dict[int, list[int]] = {}
        for pid, (ppid, _command) in self._rows().items():
            children.setdefault(ppid, []).append(pid)
        return children

    def is_claude(self, pid: int) -> bool:
        argv = self._rows().get(pid, (0, ""))[1].split()
        return bool(argv) and Path(argv[0]).name == "claude"

    def parent(self, pid: int) -> int | None:
        row = self._rows().get(pid)
        return row[0] if row is not None else None

    def task_list_id(self, _pid: int) -> str | None:
        return None

    def cwd(self, _pid: int) -> str:
        return ""

    def inventory(
        self, claude_pid: int, children: dict[int, list[int]]
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for pid in _descendants(claude_pid, children):
            argv = self._rows().get(pid, (0, ""))[1].split()
            if argv:
                args = " ".join(redact_argv(argv))[:ARGV_MAX_CHARACTERS]
                rows.append({"pid": pid, "etime": "?", "args": args})
        return rows


Processes = ProcTree | DarwinProcesses


def process_source() -> Processes:
    """This host's process source: ``ps`` on macOS, the proc tree everywhere else.

    An explicit ``AIH_PROC_ROOT`` names a proc tree to walk, so it selects the walk on every
    platform; otherwise the override documented above is silently ignored on macOS.
    """
    if "AIH_PROC_ROOT" in os.environ:
        return ProcTree()
    return DarwinProcesses() if sys.platform == "darwin" else ProcTree()


def _uptime_seconds() -> float | None:
    try:
        return float((_proc_root() / "uptime").read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def format_elapsed(seconds: int) -> str:
    """``ps -o etime`` shape: ``[[dd-]hh:]mm:ss``."""
    days, rest = divmod(max(0, seconds), SECONDS_PER_DAY)
    hours, rest = divmod(rest, SECONDS_PER_HOUR)
    minutes, secs = divmod(rest, SECONDS_PER_MINUTE)
    clock = f"{minutes:02d}:{secs:02d}"
    if days:
        return f"{days}-{hours:02d}:{clock}"
    return f"{hours:02d}:{clock}" if hours else clock


def parse_elapsed(value: object) -> int:
    """Inverse of :func:`format_elapsed`; 0 for anything not in that shape."""
    if not isinstance(value, str):
        return 0
    days, _, clock = value.rpartition("-")
    try:
        parts = [int(part) for part in clock.split(":")]
        total = int(days) * SECONDS_PER_DAY if days else 0
    except ValueError:
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * SECONDS_PER_MINUTE + part
    return total + seconds


def redact_argv(argv: list[str]) -> list[str]:
    """Credential-shaped values out of an argv before it is persisted or injected."""
    redacted: list[str] = []
    for index, token in enumerate(argv):
        if index and argv[index - 1] in SECRET_FLAGS:
            redacted.append(REDACTED)
        else:
            redacted.append(SECRET_ASSIGNMENT.sub(rf"\1{REDACTED}", token))
    return redacted


def _inventory(claude_pid: int, children: dict[int, list[int]]) -> list[dict[str, Any]]:
    uptime = _uptime_seconds()
    ticks = os.sysconf("SC_CLK_TCK")
    rows: list[dict[str, Any]] = []
    for pid in _descendants(claude_pid, children):
        fields = _stat_fields(pid)
        argv = _argv(pid)
        if fields is None or not argv:
            continue
        elapsed = 0
        if uptime is not None:
            elapsed = int(uptime - int(fields[STAT_STARTTIME_INDEX]) / ticks)
        args = " ".join(redact_argv(argv))[:ARGV_MAX_CHARACTERS]
        rows.append({"pid": pid, "etime": format_elapsed(elapsed), "args": args})
    return rows


def _newest_transcript(cwd: str) -> str:
    if not cwd:
        return ""
    directory = Path.home() / ".claude" / "projects" / supervisor._slug(cwd)
    try:
        candidates = sorted(
            directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return ""
    return str(candidates[0]) if candidates else ""


# --- stamp ---------------------------------------------------------------------------


def _list_panes() -> list[tuple[str, str, int]] | None:
    try:
        result = subprocess.run(
            [
                _tmux(),
                "list-panes",
                "-a",
                "-F",
                "#{session_name} #{pane_id} #{pane_pid}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    panes: list[tuple[str, str, int]] = []
    for line in result.stdout.splitlines():
        session, _, rest = line.rpartition(" ")
        session, _, pane_id = session.rpartition(" ")
        if session and pane_id and rest.isdigit():
            panes.append((session, pane_id, int(rest)))
    return panes


def _pane_tty(pane_id: str) -> str | None:
    """The pane's tty, so the exit-cause classifier can tell a vanished terminal (ISSUE-REF)."""
    try:
        result = subprocess.run(
            [_tmux(), "display-message", "-p", "-t", pane_id, "#{pane_tty}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    tty = result.stdout.strip()
    return tty if result.returncode == 0 and tty else None


def _stamp_for_pane(
    pane: tuple[str, str, int],
    source: Processes,
    children: dict[int, list[int]],
    boot_id: str,
) -> dict[str, Any] | None:
    session, pane_id, pane_pid = pane
    match = LAUNCHER_SESSION.match(session)
    if match is None:
        return None
    claude = _find_claude(pane_pid, children, source.is_claude)
    if claude is None:
        return None
    key = sanitize_key(source.task_list_id(claude) or match.group("name"))
    if key is None:
        return None
    cwd = source.cwd(claude)
    return {
        "key": key,
        "tmux_session": session,
        "pane_id": pane_id,
        "tty": _pane_tty(pane_id),
        "pid": claude,
        "boot_id": boot_id,
        "written_at": _iso(_now()),
        "cwd": cwd,
        "transcript_path": _newest_transcript(cwd),
        "children": source.inventory(claude, children),
        "ended_at": None,
    }


def _record_boot_identity() -> None:
    """Keep the durable host-reboot record (`launch/last-boot.json`, CC-LAUNCH-RESUME-BOOT)
    current from the stamp job, the one job scheduled on every OS; a second call on the same
    boot flips only `rebooted`. Nothing else wrote that record before 2026-10-11."""
    try:
        host_boot.record_boot()
    except host_boot.BootIdentityUnavailable:
        pass


def stamp(*, processes: Processes | None = None) -> int:
    """Write one stamp per live launcher-shaped pane; drop this boot's dead ones.

    A stamp from a PREVIOUS boot is never removed here: it is the reboot signal, and
    between the boot and the Mac's relaunch every pane is gone, so removing stamps
    with no live claude process would erase the signal before anything could read it.
    """
    if not supervisor._reboot_resume_enabled():
        return 0
    boot_id = current_boot_id()
    _record_boot_identity()
    panes = _list_panes()
    if boot_id is None or panes is None:
        return 0
    source = processes or process_source()
    children = source.children()
    live: dict[str, dict[str, Any]] = {}
    for pane in panes:
        record = _stamp_for_pane(pane, source, children, boot_id)
        if record is not None:
            live.setdefault(record["key"], record)
    root = stamp_root()
    for key, record in live.items():
        path = root / f"{key}.json"
        previous = _read_json(path)
        _write_json(path, record)
        if previous is None or previous.get("pid") != record["pid"]:
            _log("REBOOT-STAMP", key, f"pane={record['pane_id']} pid={record['pid']}")
    for path in _raw_stamps(root):
        if path.stem in live:
            continue
        previous = _read_json(path)
        if previous is not None and previous.get("boot_id") == boot_id:
            path.unlink(missing_ok=True)
            _log("REBOOT-STAMP", path.stem, "removed reason=no-live-claude")
    return 0


# --- sweep values and rendering ------------------------------------------------------


def stop_reason(last_stamp_at: datetime) -> str:
    """Label the stop from the last stamp time, in UTC."""
    moment = last_stamp_at.astimezone(UTC)
    clock = (moment.hour, moment.minute, moment.second)
    if (moment.weekday() == FRIDAY and clock >= WEEKEND_STOP_FROM_FRIDAY) or (
        moment.weekday() == SATURDAY and clock <= WEEKEND_STOP_UNTIL_SATURDAY
    ):
        return "weekend-scheduler-stop"
    if (
        moment.weekday() == SATURDAY
        and moment.day in THIRD_WEEK
        and PATCH_WINDOW[0] <= clock <= PATCH_WINDOW[1]
    ):
        return "patch-window"
    return "unscheduled"


def format_downtime(seconds: int) -> str:
    days, rest = divmod(max(0, seconds), SECONDS_PER_DAY)
    hours, rest = divmod(rest, SECONDS_PER_HOUR)
    minutes = rest // SECONDS_PER_MINUTE
    text = f"{hours}h {minutes:02d}m"
    return f"{days}d {text}" if days else text


def sweep_values(record: dict[str, Any], booted: datetime) -> dict[str, Any]:
    written = _parse_time(record.get("written_at")) or booted
    return {
        "boot_time": _iso(booted),
        "stop_reason": stop_reason(written),
        "downtime_seconds": max(0, int((booted - written).total_seconds())),
    }


def _row(child: dict[str, Any], width: int) -> str:
    args = re.sub(r"[\x00-\x1f\x7f]", " ", str(child.get("args", "")))[:width]
    return f"{child.get('pid', '?')} {child.get('etime', '?')} {args}"


def _fill(template: str, values: dict[str, str]) -> str:
    pattern = re.compile("|".join(re.escape(name) for name in values))
    return pattern.sub(lambda match: values[match.group(0)], template)


def _omitted_note(omitted: int) -> str:
    if not omitted:
        return ""
    return f"({omitted} more omitted; the longest-running are listed.)"


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _keep_tail(value: str, drop: int) -> str:
    """``value`` shortened by about ``drop`` bytes from the FRONT, prefixed ``...``."""
    encoded = value.encode("utf-8")
    keep = max(PATH_MIN_TAIL_BYTES, len(encoded) - drop - len(ELLIPSIS))
    if keep >= len(encoded):
        return value
    return ELLIPSIS + encoded[-keep:].decode("utf-8", "ignore")


def _cut_marker(cut: int) -> str:
    return f"[launch-resume: {cut} bytes cut to fit the {PROMPT_MAX_BYTES}-byte prompt bound]"


def _cut_body(text: str) -> str:
    """Last resort: keep the head of the body and say how many bytes were dropped."""
    encoded = text.encode("utf-8")
    cut = 0
    for _ in range(3):
        room = PROMPT_MAX_BYTES - 1 - _size("\n" + _cut_marker(cut) + "\n")
        cut = len(encoded) - room
    head = encoded[:room].decode("utf-8", "ignore").rstrip("\n")
    return f"{head}\n{_cut_marker(cut)}\n"


def _bounded(template: str, values: dict[str, str]) -> str:
    """Fit a row-less render under the bound: path tails first, then the body."""
    values = dict(values)
    for name in ("__TRANSCRIPT_PATH__", "__CWD__"):
        excess = _size(_fill(template, values)) - (PROMPT_MAX_BYTES - 1)
        if excess <= 0:
            break
        values[name] = _keep_tail(values[name], excess // template.count(name) + 1)
    text = _fill(template, values)
    return text if _size(text) < PROMPT_MAX_BYTES else _cut_body(text)


def render_template(
    template_path: Path, fixed: dict[str, str], children: list[Any]
) -> str:
    """``template_path`` filled from ``fixed`` plus the child inventory, under the bound.

    The one truncation ladder, shared by the boot sweep and the launch decision. The 20
    longest-running children are kept. The byte bound is HARD: when 20 rows of 200
    characters do not fit under :data:`PROMPT_MAX_BYTES`, the argv column narrows first
    and rows are dropped (and counted as omitted) only below a 20-character column. With
    no rows left, the transcript path and then the cwd keep only their tails, and as a
    last resort the body is cut with one marker line saying how many bytes went.
    """
    template = template_path.read_text(encoding="utf-8")
    ordered = sorted(
        (child for child in children if isinstance(child, dict)),
        key=lambda child: parse_elapsed(child.get("etime")),
        reverse=True,
    )
    shown = ordered[:INVENTORY_MAX_ROWS]
    width = ARGV_MAX_CHARACTERS
    while True:
        rows = [_row(child, width) for child in shown]
        filled = {
            **fixed,
            "__DETACHED__": detached_ledger.describe(
                detached_ledger.load(fixed["__KEY__"])
            ),
            "__INVENTORY__": "\n".join(rows) if rows else "(none recorded)",
            "__OMITTED_NOTE__": _omitted_note(len(ordered) - len(shown)),
        }
        text = _fill(template, filled)
        if _size(text) < PROMPT_MAX_BYTES:
            return text
        if not shown:
            return _bounded(template, filled)
        if width > ARGV_MIN_CHARACTERS:
            width = max(ARGV_MIN_CHARACTERS, width - ARGV_SHRINK_STEP)
        else:
            shown = shown[:-1]


def render(record: dict[str, Any], values: dict[str, Any]) -> str:
    """One swept stamp as the launch-resume prompt with ``cause=host_reboot`` (ISSUE-REF).

    The stop reason, the boot time and the last stamp time are the evidence line.
    """
    last_stamp = str(record.get("written_at") or "unknown")
    fixed = {
        "__KEY__": str(record.get("key", "")),
        "__TMUX_SESSION__": str(record.get("tmux_session") or "unknown"),
        "__CWD__": str(record.get("cwd") or "unknown"),
        "__TRANSCRIPT_PATH__": str(record.get("transcript_path") or "unknown"),
        "__CAUSE__": "host_reboot",
        "__EVIDENCE__": (
            f"stop_reason={values['stop_reason']} boot_time={values['boot_time']} "
            f"last_stamp_at={last_stamp}"
        ),
        "__ENDED_AT__": last_stamp,
        "__DOWNTIME__": format_downtime(int(values["downtime_seconds"])),
    }
    return render_template(TEMPLATE_PATH, fixed, list(record.get("children") or []))


# --- boot sweep ----------------------------------------------------------------------


def _disposition(
    record: dict[str, Any] | None, boot_id: str, now: datetime, ttl: int
) -> str:
    if record is None or _parse_time(record.get("written_at")) is None:
        return "malformed"
    if record.get("boot_id") == boot_id:
        return "current-boot"
    if record.get("ended_at"):
        return "ended"
    age = now - _parse_time(record["written_at"])  # type: ignore[operator]
    if age > timedelta(seconds=ttl):
        return "expired"
    if prompt_name_refusal(str(record.get("tmux_session", ""))) is not None:
        return "refused-name"
    return "pending"


def _deliver_prompt(record: dict[str, Any], text: str) -> str:
    """Publish the launcher prompt file whole, mode 0600, unless one is already waiting.

    The launcher reads and deletes the file the moment it exists, so the text is written
    and fsynced under a temporary name in the same directory first. It is published with
    ``os.link``, which is as atomic as ``os.replace`` but refuses an existing target, so a
    handoff that appears between the check and the publish is never clobbered.
    """
    target = prompt_path(str(record.get("tmux_session", "")))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return "existing"
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    except FileExistsError:
        return "existing"
    finally:
        temporary.unlink(missing_ok=True)
    return str(target)


def _pending_fields(prompt_file: str) -> dict[str, str]:
    delivered_via = "hook-context" if prompt_file == "existing" else "prompt-file"
    return {"prompt_file": prompt_file, "delivered_via": delivered_via}


def _pend(
    path: Path, record: dict[str, Any], booted: datetime, boot_id: str
) -> dict[str, Any]:
    """Claim, then publish, then drop the raw stamp, so no step can deliver twice.

    The pending record is written first: a SessionStart that lands mid-sweep prefers it
    over the raw stamp, so the raw stamp is never a second source once the claim exists.
    """
    values = sweep_values(record, booted)
    target = prompt_path(str(record.get("tmux_session", "")))
    planned = "existing" if target.exists() else str(target)
    pending = {
        **record,
        **values,
        "swept_at": _iso(_now()),
        "boot_id_at_sweep": boot_id,
        **_pending_fields(planned),
    }
    pending_path = stamp_root() / "pending" / path.name
    _write_json(pending_path, pending)
    if planned != "existing":
        published = _deliver_prompt(record, render(record, values))
        if published != planned:
            pending.update(_pending_fields(published))
            _write_json(pending_path, pending)
    path.unlink(missing_ok=True)
    return pending


def _apply(
    path: Path,
    record: dict[str, Any] | None,
    disposition: str,
    booted: datetime,
    boot_id: str,
) -> str:
    key = path.stem
    if disposition == "ended":
        path.unlink(missing_ok=True)
        return "removed"
    if disposition in {"expired", "malformed"}:
        _archive(path, "expired", record or {"key": key, "malformed": True}, key)
        return "moved to expired/"
    if disposition == "refused-name":
        reason = prompt_name_refusal(str((record or {}).get("tmux_session", "")))
        _archive(path, "expired", {**(record or {}), "refused_reason": reason}, key)
        return f"moved to expired/ reason={reason}"
    pending = _pend(path, record or {}, booted, boot_id)
    return (
        f"prompt_file={'existing' if pending['prompt_file'] == 'existing' else 'written'} "
        f"stop_reason={pending['stop_reason']}"
    )


def _write_last_sweep(
    boot_id: str, booted: datetime, dispositions: dict[str, str]
) -> None:
    path = stamp_root() / LAST_SWEEP
    previous = _read_json(path)
    if previous is not None and previous.get("boot_id") == boot_id:
        if not dispositions:
            return
        dispositions = {**previous.get("dispositions", {}), **dispositions}
    _write_json(
        path,
        {
            "boot_id": boot_id,
            "boot_time": _iso(booted),
            "swept_at": _iso(_now()),
            "dispositions": dispositions,
        },
    )


def _launch_resume_refusal() -> str | None:
    """Why the live ``launch_resume`` object forbids resuming a host reboot, else None."""
    try:
        settings = supervisor.launch_resume_config()
    except supervisor.LaunchResumeConfigError as error:
        return f"launch_resume-config-error:{error.reason}"
    if settings["mode"] == "never":
        return "launch_resume.mode=never"
    if not settings["scenarios"]["host_reboot"]:
        return "launch_resume.scenarios.host_reboot=false"
    return None


def boot_sweep(*, dry_run: bool = False) -> int:
    """Turn every previous-boot stamp into a first prompt, once.

    Idempotent: a second run on the same boot finds no raw stamp with a differing boot
    id, so it writes, moves and logs nothing.
    """
    if not supervisor._reboot_resume_enabled():
        print(json.dumps({"skipped": "reboot_resume_enabled=false"}))
        return 0
    refusal = _launch_resume_refusal()
    if refusal is not None:
        # The launcher's first prompt IS a resume, so a profile whose launch_resume says a
        # host reboot must not resume gets none. The raw stamps stay: each launch still
        # classifies host_reboot and logs its own SKIP.
        _log("REBOOT-SWEEP-SKIP", "all", f"reason={refusal}")
        print(json.dumps({"skipped": refusal}))
        return 0
    boot_id = current_boot_id()
    if boot_id is None:
        print("boot-sweep: the current boot id is unreadable", file=sys.stderr)
        return 0
    booted = boot_time() or _now()
    now = _now()
    ttl = supervisor._launch_resume_stamp_ttl_seconds()
    plan: dict[str, str] = {}
    applied: dict[str, str] = {}
    for path in _raw_stamps(stamp_root()):
        record = _read_json(path)
        disposition = _disposition(record, boot_id, now, ttl)
        if disposition == "current-boot":
            continue
        plan[path.stem] = disposition
        if dry_run:
            continue
        outcome = _apply(path, record, disposition, booted, boot_id)
        applied[path.stem] = disposition
        stage = "REBOOT-SWEEP" if disposition == "pending" else "REBOOT-SWEEP-SKIP"
        _log(stage, path.stem, f"disposition={disposition} {outcome}")
    if dry_run:
        print(
            json.dumps(
                {"boot_id": boot_id, "dry_run": True, "plan": plan}, sort_keys=True
            )
        )
        return 0
    _write_last_sweep(boot_id, booted, applied)
    print(json.dumps({"boot_id": boot_id, "dispositions": applied}, sort_keys=True))
    return 0


# --- status --------------------------------------------------------------------------


def status() -> int:
    root = stamp_root()

    def names(directory: Path) -> list[str]:
        return (
            sorted(p.stem for p in directory.glob("*.json"))
            if directory.is_dir()
            else []
        )

    print(
        json.dumps(
            {
                "boot_id": current_boot_id(),
                "raw": [p.stem for p in _raw_stamps(root)],
                "pending": names(root / "pending"),
                "consumed": names(root / "consumed"),
                "expired": names(root / "expired"),
                "last_sweep": _read_json(root / LAST_SWEEP),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


SWEEP_FIELDS = ("boot_time", "stop_reason", "downtime_seconds")


def _record_values(record: dict[str, Any]) -> dict[str, Any]:
    """A swept record's own sweep values, else ones computed against this boot."""
    if all(name in record for name in SWEEP_FIELDS):
        return {name: record[name] for name in SWEEP_FIELDS}
    return sweep_values(record, boot_time() or _now())


def render_command(record_path: Path) -> int:
    record = _read_json(record_path)
    if record is None:
        print(f"render: {record_path} is not a JSON record", file=sys.stderr)
        return 2
    sys.stdout.write(render(record, _record_values(record)))
    return 0


# --- what the launch decision reads from the store, and session end ------------------


def _emit(text: str) -> None:
    """The one block a SessionStart hook may print."""
    sys.stdout.write(f"<system-reminder>\n{text.rstrip()}\n</system-reminder>\n")
    sys.stdout.flush()


def pending_is_live(
    record: dict[str, Any], boot_id: str | None, ttl: int, now: datetime
) -> bool:
    """Swept on THIS boot and inside the TTL; anything else is an abandoned claim."""
    swept = _parse_time(record.get("swept_at"))
    if boot_id is None or swept is None or record.get("boot_id_at_sweep") != boot_id:
        return False
    return now - swept <= timedelta(seconds=ttl)


def consume(
    source: Path, record: dict[str, Any], key: str, delivered: str, now: datetime
) -> Path:
    """Move a pending record to ``consumed/``: a later SessionStart cannot act on it again."""
    return _archive(
        source,
        "consumed",
        {**record, "consumed_at": _iso(now), "delivered": delivered},
        key,
    )


def expire(source: Path, record: dict[str, Any], key: str) -> Path:
    return _archive(source, "expired", record, key)


def session_end(raw_key: str, reason: str = "other") -> int:
    """Mark a deliberate exit, so the stamp left behind is never a resume signal.

    ``clear`` and ``resume`` continue the session, so they leave the stamp untouched
    (ISSUE-REF T-1.2); every other SessionEnd reason is an exit.
    """
    key = sanitize_key(raw_key)
    if key is None or reason in NOT_EXITS:
        return 0
    path = stamp_root() / f"{key}.json"
    record = _read_json(path)
    if record is None or record.get("ended_at"):
        return 0
    record["ended_at"] = _iso(_now())
    _write_json(path, record)
    _log("REBOOT-SESSION-END", key, f"ended_at={record['ended_at']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ai_chassis.reboot_resume")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("stamp")
    sweep = commands.add_parser("boot-sweep")
    sweep.add_argument("--dry-run", action="store_true")
    commands.add_parser("status")
    render_parser = commands.add_parser("render")
    render_parser.add_argument("--record", required=True, type=Path)
    end = commands.add_parser("session-end")
    end.add_argument("--key", required=True)
    end.add_argument("--reason", default="other")
    args = parser.parse_args(argv)
    if args.command == "stamp":
        return stamp()
    if args.command == "boot-sweep":
        return boot_sweep(dry_run=args.dry_run)
    if args.command == "status":
        return status()
    if args.command == "session-end":
        return session_end(args.key, args.reason)
    return render_command(args.record)


if __name__ == "__main__":
    raise SystemExit(main())
