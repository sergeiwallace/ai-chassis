#!/usr/bin/env python3
# ruff: noqa: E402
"""Per-machine contention budget, sampler, unit count and launch admission (ISSUE-REF).

A session cannot see the whole machine: six sessions each honouring a fan-out cap of three still
put eighteen agents on one box, and on example-client-host-mac eight agents plus a reap loop drove the load to
15.6 on 10 cores until per-prompt hooks timed out (2026-10-10, ISSUE-REF). This module is the
machine-wide view, in four parts (design: docs/designs/issue-ref-machine-resource-contention-
heuristic.md):

* ``load_budget`` reads ``config/machine-contention.toml`` at CALL TIME (D-1). A host either has
  measured figures with their date, or no budget at all (D-5); nothing is computed for a host
  nobody measured.
* ``sample`` (scheduled every ``sample_interval_seconds`` by a LaunchAgent or systemd user timer,
  D-4) appends one record to ``samples.jsonl`` and atomically replaces ``verdict.json`` with a
  two-window verdict: ADMIT, LOOK or HOLD.
* ``count_units`` counts live units machine-wide from records the harness already keeps (D-2),
  because sub-agents are tasks inside one ``claude`` process and a process count reads 1 however
  many run.
* ``admit`` is the ``PreToolUse(Agent)`` decision (D-3): refuse ONLY on a measured persistent
  stall (HOLD), quote the reading from the operating target up, and fail open with a one-line
  diagnosis on anything it cannot read.

Nothing here kills, renices or reconfigures a process, and the kernel pressure level is read
through libc ``sysctlbyname``, never by running the ``sysctl`` command.
"""

from __future__ import annotations

try:
    from py_interpreter_bootstrap import ensure_supported_python
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):
        """Stand in for the bootstrap when this file was copied without it.

        A partial copy cannot re-exec, because the module that knows how is the one
        missing. Staying silent when the running interpreter ALREADY meets the floor is
        correct rather than lax: there was nothing to enforce. Below the floor there is
        no remedy left to apply, so say so and let the real failure happen where it can
        be read, instead of dying here on an import that is not the problem.
        """
        import sys as _sys

        if _sys.version_info < minimum:
            _sys.stderr.write(
                "warning: {} is running under Python {} without its interpreter "
                "bootstrap, so the {}.{}+ floor could not be enforced\n".format(
                    __file__,
                    ".".join(str(p) for p in _sys.version_info[:3]),
                    minimum[0],
                    minimum[1],
                )
            )


ensure_supported_python()

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import coordinator_launch_ledger as ledger
import process_liveness
from background_wait_guard import TERMINAL as BACKGROUND_TERMINAL

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "machine-contention.toml"
MACHINE_REGISTRY = ROOT / "projects-wide-session-config" / "ai-harness.toml"

SCHEMA = 1
#: Every key a ``measured = true`` host must carry. ``notes`` is optional prose.
BUDGET_KEYS = (
    "measured_on",
    "cores",
    "reserved_cores",
    "unit_cost_cores",
    "operating_target_units",
    "hard_stop_units",
)
#: Every ``[defaults.thresholds]`` key and the type its value must have. A missing key fails the
#: config closed, naming the key: a check that silently skipped would read as ADMIT.
NUMBER, TEXT, TEXTS = "a number", "a string", "a list of strings"
THRESHOLD_KEYS = {
    "idle_pct_floor": NUMBER,
    "load_per_core_look": NUMBER,
    "memory_free_pct_floor": NUMBER,
    "psi_cpu_some_pct": NUMBER,
    "psi_cpu_full_pct": NUMBER,
    "psi_io_full_pct": NUMBER,
    "psi_memory_full_pct": NUMBER,
    "pressure_level_hold": TEXT,
    "canary_spawn_seconds_hold": NUMBER,
    "swapouts_growth_samples": NUMBER,
    "swapouts_per_minute_hold": NUMBER,
    "pageouts_per_minute_hold": NUMBER,
    "psi_memory_some_thrash_pct": NUMBER,
    "swap_thrash_pressure_levels": TEXTS,
}


class ConfigError(ValueError):
    """The budget config cannot be trusted; the message names the offending host or key."""


@dataclass(frozen=True)
class Budget:
    """One host's budget. ``measured`` False means no unit limits at all (D-5)."""

    host: str
    measured: bool
    measured_on: str | None = None
    cores: int | None = None
    reserved_cores: float | None = None
    unit_cost_cores: float | None = None
    operating_target_units: int | None = None
    hard_stop_units: int | None = None
    notes: str | None = None


def registered_machines(registry: Path = MACHINE_REGISTRY) -> set[str]:
    """The ``[machine.<name>]`` table keys of the harness's machine registry.

    ISSUE-REF Phase 1: there is no ``site.machines`` array; the table keys ARE the machine set
    (``ai_chassis.machine_registry.machine_names`` is the shared rule, spelled inline because
    this script runs from its own directory without the package on ``sys.path``).
    """
    try:
        document = tomllib.loads(registry.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(
            f"machine registry {registry} is unreadable: {error}"
        ) from error
    machines = document.get("machine")
    if not isinstance(machines, dict) or not machines:
        raise ConfigError(
            f"machine registry {registry} declares no [machine.<name>] table"
        )
    return {str(machine) for machine in machines}


def _validate_numbers(host: str, table: dict) -> None:
    for key in ("cores", "operating_target_units", "hard_stop_units"):
        value = table[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"[hosts.{host}] {key} must be a positive integer")
    for key in ("reserved_cores", "unit_cost_cores"):
        value = table[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"[hosts.{host}] {key} must be a non-negative number")


#: The kernel's own range for ``vm.swappiness`` (0 to 200 since Linux 5.8).
SWAPPINESS_RANGE = range(0, 201)


def _validate_host(host: str, table: object) -> None:
    if not isinstance(table, dict):
        raise ConfigError(f"[hosts.{host}] is not a table")
    swappiness = table.get("vm_swappiness")
    if swappiness is not None and (
        not isinstance(swappiness, int)
        or isinstance(swappiness, bool)
        or swappiness not in SWAPPINESS_RANGE
    ):
        raise ConfigError(f"[hosts.{host}] vm_swappiness must be an integer 0 to 200")
    measured = table.get("measured", False)
    if not isinstance(measured, bool):
        raise ConfigError(f"[hosts.{host}] measured must be true or false")
    if not measured:
        return
    for key in BUDGET_KEYS:
        if key not in table:
            raise ConfigError(f"[hosts.{host}] is measured but lacks {key}")
    _validate_numbers(host, table)
    if table["hard_stop_units"] < table["operating_target_units"]:
        raise ConfigError(
            f"[hosts.{host}] hard_stop_units ({table['hard_stop_units']}) is below "
            f"operating_target_units ({table['operating_target_units']})"
        )


def load_config(config_path: Path | None = None) -> dict:
    """Parse and validate the budget config; raise ``ConfigError`` naming what is wrong."""
    path = DEFAULT_CONFIG if config_path is None else Path(config_path)
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"budget config {path} is unreadable: {error}") from error
    hosts = document.get("hosts", {})
    if not isinstance(hosts, dict):
        raise ConfigError(f"budget config {path}: [hosts] is not a table")
    if hosts:
        known = registered_machines()
        for host in sorted(hosts):
            if host not in known:
                raise ConfigError(
                    f"budget config {path} names host {host!r}, which is not in "
                    f"site.machines of {MACHINE_REGISTRY}"
                )
    for host, table in hosts.items():
        _validate_host(host, table)
    _validate_prefixes("[defaults]", settings(document))
    for host, table in hosts.items():
        _validate_prefixes(f"[hosts.{host}]", table)
    for host in ["", *sorted(hosts)]:
        _validate_thresholds(path, host, thresholds(document, host))
    return document


#: Session-name prefixes a HOLD admits (D-5 revision, ISSUE-REF); absent reads as none.
ADMIT_PREFIXES_KEY = "hold_admit_session_prefixes"


def _validate_prefixes(where: str, table: dict) -> None:
    if ADMIT_PREFIXES_KEY not in table:
        return
    value = table[ADMIT_PREFIXES_KEY]
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ConfigError(
            f"{where} {ADMIT_PREFIXES_KEY} must be a list of non-empty strings"
        )


def hold_admit_prefixes(config: dict, host: str) -> tuple[str, ...]:
    """The host's own list when it sets one, else ``[defaults]``'s, else none."""
    table = config.get("hosts", {}).get(host) or {}
    if ADMIT_PREFIXES_KEY in table:
        return tuple(table[ADMIT_PREFIXES_KEY])
    return tuple(settings(config).get(ADMIT_PREFIXES_KEY, ()))


def _validate_thresholds(path: Path, host: str, merged: dict) -> None:
    where = f"[hosts.{host}.thresholds] over " if host else ""
    for key, kind in THRESHOLD_KEYS.items():
        if key not in merged:
            raise ConfigError(
                f"budget config {path}: {where}[defaults.thresholds] lacks {key}"
            )
        value = merged[key]
        if kind == NUMBER:
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif kind == TEXT:
            valid = isinstance(value, str)
        else:
            valid = isinstance(value, list) and all(isinstance(v, str) for v in value)
        if not valid:
            raise ConfigError(
                f"budget config {path}: {where}threshold {key} must be {kind}"
            )


def load_budget(
    host: str, *, config_path: Path | None = None, config: dict | None = None
) -> Budget:
    """The budget for ``host`` exactly as written, or an unmeasured one."""
    document = load_config(config_path) if config is None else config
    table = document.get("hosts", {}).get(host)
    if not table or not table.get("measured", False):
        return Budget(host=host, measured=False)
    return Budget(
        host=host,
        measured=True,
        measured_on=str(table["measured_on"]),
        cores=table["cores"],
        reserved_cores=table["reserved_cores"],
        unit_cost_cores=table["unit_cost_cores"],
        operating_target_units=table["operating_target_units"],
        hard_stop_units=table["hard_stop_units"],
        notes=table.get("notes"),
    )


def host_marker_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "ai-harness" / "machine"


def resolve_host() -> str:
    """``AI_HOST``, else the host marker file; empty reads as an unmeasured host.

    A scheduler-launched sampler inherits no login shell, so ``AI_HOST`` may be unset there.
    The marker is parsed by ``model_catalog.resolve_host`` itself (first non-blank,
    non-comment line), imported only on this path because it costs ~120 ms and the hook's
    session environment always carries ``AI_HOST``.
    """
    from_env = (os.environ.get("AI_HOST") or "").strip()
    if from_env:
        return from_env
    import model_catalog

    return model_catalog.resolve_host(
        {model_catalog.MACHINE_MARKER_ENV_VAR: str(host_marker_path())}
    )


def settings(config: dict) -> dict:
    """``[defaults]`` without its sub-tables."""
    return {
        key: value
        for key, value in config.get("defaults", {}).items()
        if not isinstance(value, dict)
    }


# --- unit accounting (D-2) ------------------------------------------------------------------

META_SUFFIX = ".meta.json"
TERMINAL_SUFFIX = ".terminal.json"
AGENT_PREFIX = "agent-"


def _agent_ids(names: list[str], suffix: str) -> set[str]:
    return {
        name[len(AGENT_PREFIX) : -len(suffix)]
        for name in names
        if name.startswith(AGENT_PREFIX) and name.endswith(suffix)
    }


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def session_liveness(config_dir: Path) -> tuple[set[str], set[str]]:
    """``(live session ids, registered session ids)`` from ``<config>/sessions/<pid>.json``.

    Live means the record's pid exists now. ``pid_alive`` answers ALIVE when it cannot decide,
    which here over-counts a unit rather than hiding one; the count never refuses on its own.
    """
    live: set[str] = set()
    registered: set[str] = set()
    try:
        paths = sorted((config_dir / "sessions").glob("*.json"))
    except OSError:
        return live, registered
    for path in paths:
        record = _read_json(path)
        if record is None:
            continue
        session_id, pid = record.get("sessionId"), record.get("pid")
        if not isinstance(session_id, str) or not isinstance(pid, int):
            continue
        registered.add(session_id)
        if pid > 0 and process_liveness.pid_alive(pid):
            live.add(session_id)
    return live, registered


def _session_dirs(projects: Path) -> list[Path]:
    """Every ``projects/<project>/<session>`` that holds a ``subagents`` directory.

    Raises ``OSError`` when ``projects`` or any ``subagents`` directory cannot be listed: an
    unreadable marker directory must read as an unknown count, never as zero agents.
    """
    found: list[Path] = []
    with os.scandir(projects) as entries:
        project_dirs = [Path(entry.path) for entry in entries if entry.is_dir()]
    for project in project_dirs:
        with os.scandir(project) as entries:
            for entry in entries:
                subagents = Path(entry.path) / "subagents"
                if entry.is_dir() and subagents.is_dir():
                    found.append(Path(entry.path))
    return found


@dataclass
class Tally:
    """What the count saw but did not count: dead-session markers, and idle agents/shells."""

    idle_seconds: float
    now: float
    stale: list[dict]
    idle: list[dict]
    background_idle_seconds: float = 3600


def _last_written(subagents: Path, agent_id: str) -> float | None:
    """The agent transcript's mtime, else its record's: a stat, never a read of the ``.jsonl``."""
    for suffix in (".jsonl", META_SUFFIX):
        try:
            return (subagents / f"{AGENT_PREFIX}{agent_id}{suffix}").stat().st_mtime
        except OSError:
            continue
    return None


def _running_agents(
    subagents: Path, tally: Tally
) -> tuple[set[str], set[str], set[str]]:
    """``(started-not-stopped, quiet, units)`` for one session's agents.

    Quiet: the transcript has not been written for ``agent_idle_seconds``, which is how a
    finished agent whose SubagentStop never landed reads (measured 2026-10-11 on example-client-host-mac: 4 of
    8 unstopped agents had been quiet 2-24 h). An agent whose own child is still active is a
    coordinator awaiting it; the design's unit definition counts the child, not the parent.
    """
    names = os.listdir(subagents)
    running = _agent_ids(names, META_SUFFIX) - _agent_ids(names, TERMINAL_SUFFIX)
    quiet = set()
    for agent_id in running:
        written = _last_written(subagents, agent_id)
        if written is not None and tally.now - written > tally.idle_seconds:
            quiet.add(agent_id)
    active = running - quiet
    awaiting = set()
    for agent_id in active:
        meta = _read_json(subagents / f"{AGENT_PREFIX}{agent_id}{META_SUFFIX}") or {}
        parent = meta.get("parentAgentId")
        if parent in active:
            awaiting.add(parent)
    return running, quiet, active - awaiting


def _count_agents(config_dir: Path, live: set[str], tally: Tally) -> int:
    count = 0
    for session_dir in _session_dirs(config_dir / "projects"):
        session_id = session_dir.name
        running, quiet, units = _running_agents(session_dir / "subagents", tally)
        if session_id in live:
            count += len(units)
            tally.idle.extend(
                {"kind": "agent", "id": agent_id, "session_id": session_id}
                for agent_id in sorted(quiet)
            )
            continue
        tally.stale.extend(
            {"kind": "agent", "id": agent_id, "session_id": session_id}
            for agent_id in sorted(running)
        )
    return count


def _task_output_dirs(session_id: str) -> list[Path]:
    """Claude Code's ``claude-<uid>/<project>/<session>/tasks`` directories for one session.

    On macOS Claude Code writes under ``/private/tmp`` rather than ``$TMPDIR``; elsewhere the
    root is the platform temp directory. The project slug is globbed: a marker does not record
    it, and the session id is unique on its own.
    """
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return []
    name = f"claude-{getuid()}"
    roots = [Path("/private/tmp") / name, Path(tempfile.gettempdir()) / name]
    found: list[Path] = []
    for root in dict.fromkeys(roots):
        try:
            found.extend(path for path in root.glob(f"*/{session_id}/tasks"))
        except OSError:
            continue
    return found


def _background_last_active(
    marker: Path, record: dict, task_id: str, outputs: list[Path]
) -> float | None:
    """The later of the marker's ``updated_at`` (else its mtime) and the output file's mtime."""
    stamps: list[float] = []
    updated = record.get("updated_at")
    if isinstance(updated, (int, float)):
        stamps.append(float(updated))
    else:
        try:
            stamps.append(marker.stat().st_mtime)
        except OSError:
            pass
    for tasks in outputs:
        try:
            stamps.append((tasks / f"{task_id}.output").stat().st_mtime)
        except OSError:
            continue
    return max(stamps) if stamps else None


def _count_background(config_dir: Path, live: set[str], tally: Tally) -> int:
    """Non-terminal ``task-*.json`` markers of live sessions that are not idle.

    Idle: neither the marker nor the task's output file has been written for
    ``background_idle_seconds``, which is how a finished shell whose marker still reads
    ``running`` looks (measured 2026-10-11 on example-client-host-mac: 5 of 7 counted shells had finished).
    """
    root = config_dir / "state" / "background-wait"
    count = 0
    try:
        session_dirs = sorted(path for path in root.iterdir() if path.is_dir())
    except OSError:
        return 0
    for session_dir in session_dirs:
        count += _live_background(session_dir, session_dir.name in live, tally)
    return count


def _live_background(session_dir: Path, live: bool, tally: Tally) -> int:
    """One session's non-terminal, not-idle background shells; ``live`` is its liveness."""
    session_id = session_dir.name
    count = 0
    outputs: list[Path] | None = None
    for marker in sorted(session_dir.glob("task-*.json")):
        record = _read_json(marker)
        if record is None:
            continue
        status = str(record.get("status") or "").lower()
        if status in BACKGROUND_TERMINAL:
            continue
        task_id = record.get("task_id") or marker.stem.split("-", 1)[-1]
        entry = {"kind": "background", "id": task_id, "session_id": session_id}
        if not live:
            tally.stale.append(entry)
            continue
        if outputs is None:
            outputs = _task_output_dirs(session_id)
        active = _background_last_active(marker, record, task_id, outputs)
        if active is not None and tally.now - active > tally.background_idle_seconds:
            tally.idle.append(entry)
            continue
        count += 1
    return count


def session_units(
    session_id: str,
    *,
    idle_seconds: float,
    background_idle_seconds: float,
    now: float | None = None,
) -> dict:
    """One live session's own working agents and background shells (ISSUE-REF).

    The same records and the same quiet rules as :func:`count_units`, scoped to the session,
    so a restart gate and the machine budget can never disagree about what is running.
    ``agents`` is None, with ``error`` naming the directory, when an agent marker directory
    cannot be read: a caller must treat that as unknown, never as zero.
    """
    config_dir = ledger._user_config_dir()
    tally = Tally(
        idle_seconds=idle_seconds,
        now=time.time() if now is None else now,
        stale=[],
        idle=[],
        background_idle_seconds=background_idle_seconds,
    )
    agents: set[str] = set()
    try:
        for subagents in (config_dir / "projects").glob(f"*/{session_id}/subagents"):
            agents |= _running_agents(subagents, tally)[2]
    except OSError as error:
        return {
            "agents": None,
            "background": None,
            "error": f"cannot read agent marker directory under "
            f"{config_dir / 'projects'}: {error}",
        }
    session_dir = config_dir / "state" / "background-wait" / session_id
    background = _live_background(session_dir, True, tally)
    return {"agents": len(agents), "background": background, "error": None}


def _count_cx(stale: list[dict]) -> int:
    """Live attached cx workers: a lease capsule whose app-server pid exists."""
    leases = Path.home() / ".codex" / "cx-agents" / "v1" / "leases"
    count = 0
    try:
        paths = sorted(leases.glob("*.json"))
    except OSError:
        return 0
    for path in paths:
        record = _read_json(path)
        server = (record or {}).get("app_server")
        pid = server.get("pid") if isinstance(server, dict) else None
        if not isinstance(pid, int) or pid <= 0:
            continue
        if process_liveness.pid_alive(pid):
            count += 1
        else:
            stale.append({"kind": "cx", "id": path.stem, "pid": pid})
    return count


def _recorded_at(sidecar: dict) -> float | None:
    value = sidecar.get("recorded_at")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _started_tool_use_ids(config_dir: Path, session_id: str) -> set[str]:
    ids: set[str] = set()
    for subagents in (config_dir / "projects").glob(f"*/{session_id}/subagents"):
        for meta in ledger.read_meta_records(subagents).values():
            if isinstance(meta.get("toolUseId"), str):
                ids.add(meta["toolUseId"])
    return ids


def _count_recent_launches(
    config_dir: Path, live: set[str], *, grace: float, now: float, exclude: str | None
) -> int:
    """Launches younger than the grace period whose agent record does not exist yet.

    The load average lags a launch, so a launch counts from its sidecar (research § 2, GNU
    make's ``-l`` correction). The launch being admitted is excluded: it is the one asking.
    """
    count = 0
    started: dict[str, set[str]] = {}
    for tool_use_id, sidecar in ledger.read_sidecars().items():
        if tool_use_id == exclude:
            continue
        recorded = _recorded_at(sidecar)
        session_id = sidecar.get("session_id")
        if recorded is None or not 0 <= now - recorded <= grace:
            continue
        if session_id not in live:
            continue
        if session_id not in started:
            started[session_id] = _started_tool_use_ids(config_dir, session_id)
        if tool_use_id not in started[session_id]:
            count += 1
    return count


def count_units(
    *,
    exclude_tool_use_id: str | None = None,
    grace_seconds: float = 90,
    idle_seconds: float = 600,
    background_idle_seconds: float = 3600,
    now: float | None = None,
) -> dict:
    """Live units machine-wide, from the records the harness already keeps.

    ``units`` is None, with ``error`` naming the directory, when the agent marker directory
    cannot be read; a caller must treat that as unknown, never as zero.
    """
    config_dir = ledger._user_config_dir()
    moment = time.time() if now is None else now
    live, _registered = session_liveness(config_dir)
    tally = Tally(
        idle_seconds=idle_seconds,
        now=moment,
        stale=[],
        idle=[],
        background_idle_seconds=background_idle_seconds,
    )
    try:
        agents = _count_agents(config_dir, live, tally)
    except OSError as error:
        return {
            "units": None,
            "units_detail": None,
            "stale": [],
            "idle": [],
            "error": f"cannot read agent marker directory {config_dir / 'projects'}: "
            f"{error}",
        }
    stale = tally.stale
    detail = {
        "agents": agents,
        "background": _count_background(config_dir, live, tally),
        "cx": _count_cx(stale),
        "recent_launches": _count_recent_launches(
            config_dir,
            live,
            grace=grace_seconds,
            now=moment,
            exclude=exclude_tool_use_id,
        ),
    }
    return {
        "units": sum(detail.values()),
        "units_detail": detail,
        "stale": stale,
        "idle": tally.idle,
        "error": None,
    }


def _count_for(config: dict, **kwargs) -> dict:
    values = settings(config)
    return count_units(
        grace_seconds=values.get("launch_grace_seconds", 90),
        idle_seconds=values.get("agent_idle_seconds", 600),
        background_idle_seconds=values.get("background_idle_seconds", 3600),
        **kwargs,
    )


# --- signals --------------------------------------------------------------------------------

#: ``kern.memorystatus_vm_pressure_level``'s values. Research § 8 records 1/2/4 as believed
#: and unverified; measured on example-client-host-mac 2026-10-11 the level read 2 while ``memory_pressure``
#: reported 37% free with swap 3.4 GB of 4 GB used, which is consistent with 2 = warn. Any
#: other value is recorded verbatim as ``unknown:<n>`` rather than guessed.
PRESSURE_LEVELS = {1: "normal", 2: "warn", 4: "critical"}
TOOL_TIMEOUT_SECONDS = 15
MEMORY_FREE = re.compile(r"System-wide memory free percentage:\s*(\d+(?:\.\d+)?)%")
VM_STAT_FIELD = re.compile(r'^"?(Swapouts|Pageouts)"?:\s+(\d+)\.?\s*$', re.MULTILINE)
#: The PSI lines read per resource. ``full`` on cpu and io and memory is the stall that holds;
#: cpu ``some`` is LOOK only and memory ``some`` corroborates swap thrash (2026-10-24 ruling).
PSI_KINDS = (("cpu", ("some", "full")), ("memory", ("some", "full")), ("io", ("full",)))
#: The PSI readings that HOLD on their own, each against ``psi_<resource>_<kind>_pct``.
PSI_STALLS = (("cpu", "full"), ("memory", "full"), ("io", "full"))


def _run_tool(argv: list[str]) -> tuple[str | None, float]:
    """``(stdout, wall seconds)``; stdout None when the tool could not be run cleanly."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, float(TOOL_TIMEOUT_SECONDS)
    except OSError:
        return None, time.monotonic() - started
    elapsed = time.monotonic() - started
    return (completed.stdout if completed.returncode == 0 else None), elapsed


def _darwin_pressure_level() -> str | None:
    """The kernel's own composite level, through libc; never the ``sysctl`` command.

    ``CDLL(None)`` is ``dlopen(NULL)``: libSystem is in every Darwin process already.
    ``ctypes.util.find_library("c")`` is not used because a set ``DYLD_FALLBACK_LIBRARY_PATH``
    (the pre-push test gate exports ``/opt/homebrew/lib``) replaces its default search list
    and it returns None.
    """
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    value = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(value))
    status = libc.sysctlbyname(
        b"kern.memorystatus_vm_pressure_level",
        ctypes.byref(value),
        ctypes.byref(size),
        None,
        ctypes.c_size_t(0),
    )
    if status != 0:
        return None
    return PRESSURE_LEVELS.get(value.value, f"unknown:{value.value}")


def _read_darwin(reading: dict, missing: list[str]) -> None:
    out, _ = _run_tool(["vm_stat"])
    counters = dict(VM_STAT_FIELD.findall(out or ""))
    if "Swapouts" in counters and "Pageouts" in counters:
        reading["swapouts_total"] = int(counters["Swapouts"])
        reading["pageouts_total"] = int(counters["Pageouts"])
    else:
        missing.append("vm_stat")
    out, _ = _run_tool(["memory_pressure"])
    match = MEMORY_FREE.search(out or "")
    if match:
        reading["memory_free_pct"] = float(match.group(1))
    else:
        missing.append("memory_pressure")
    reading["pressure_level"] = _darwin_pressure_level()
    if reading["pressure_level"] is None:
        missing.append("kern.memorystatus_vm_pressure_level")


#: The cgroup v2 mount. Pressure can be read per slice beneath it (ISSUE-REF).
CGROUP_ROOT = Path("/sys/fs/cgroup")


def _psi_line(text: str, kind: str) -> tuple[float, float] | None:
    for line in text.splitlines():
        if line.startswith(kind):
            fields = dict(
                part.split("=", 1) for part in line.split()[1:] if "=" in part
            )
            return float(fields["avg60"]), float(fields["avg300"])
    return None


def _linux_psi(
    cgroups: tuple[str, ...] = (),
    *,
    pressure_dir: Path = Path("/proc/pressure"),
    cgroup_root: Path = CGROUP_ROOT,
) -> dict | None:
    """Host PSI, read from ``cgroups`` for each resource any of them reports.

    The root's pressure counts every stalled task, including tasks a slice's own ``CPUQuota``
    or ``MemoryHigh`` throttles. On framework-fedora the one-core, 6G-soft-capped ``ci.slice``
    alone held the root at cpu some 87% and memory full 2% while the interactive slices read 0,
    so the host sat at HOLD and refused every launch (ISSUE-REF). Naming the slices that matter
    reads their worst pressure instead; a resource none of them reports keeps the root reading.
    Host-wide memory exhaustion still surfaces through the swap-thrash check. An unreadable
    resource file drops that resource only: discarding the rest would hide a stall read beside
    it. No readable resource at all is no reading (``None``).
    """
    psi: dict[str, dict[str, float]] = {}
    for resource, kinds in PSI_KINDS:
        try:
            text = (pressure_dir / resource).read_text(encoding="utf-8")
        except OSError:
            continue
        local_texts = []
        for name in cgroups:
            try:
                local_texts.append(
                    (cgroup_root / name / f"{resource}.pressure").read_text(
                        encoding="utf-8"
                    )
                )
            except OSError:
                continue
        values: dict[str, float] = {}
        for kind in kinds:
            reading = _psi_line(text, kind)
            scoped = [r for r in (_psi_line(t, kind) for t in local_texts) if r]
            if scoped:
                reading = (max(v[0] for v in scoped), max(v[1] for v in scoped))
            if reading is not None:
                values[f"{kind}_avg60"], values[f"{kind}_avg300"] = reading
        if values:
            psi[resource] = values
    return psi or None


def _read_linux(
    reading: dict, missing: list[str], psi_cgroups: tuple[str, ...] = ()
) -> None:
    try:
        vmstat = dict(
            line.split()[:2]
            for line in Path("/proc/vmstat").read_text().splitlines()
            if line.strip()
        )
        reading["swapouts_total"] = int(vmstat["pswpout"])
    except (OSError, KeyError, ValueError):
        missing.append("/proc/vmstat")
    try:
        meminfo = {
            line.split(":")[0]: int(line.split()[1])
            for line in Path("/proc/meminfo").read_text().splitlines()
            if ":" in line
        }
        reading["memory_free_pct"] = round(
            100.0 * meminfo["MemAvailable"] / meminfo["MemTotal"], 1
        )
    except (OSError, KeyError, ValueError, ZeroDivisionError, IndexError):
        missing.append("/proc/meminfo")
    reading["psi"] = _linux_psi(psi_cgroups)
    if reading["psi"] is None:
        missing.append("/proc/pressure")


def _read_psutil(reading: dict, missing: list[str]) -> None:
    try:
        import psutil
    except ImportError:
        missing.append("psutil")
        return
    reading["idle_pct"] = round(psutil.cpu_times_percent(interval=1).idle, 1)
    reading["swap_used_mb"] = round(psutil.swap_memory().used / (1024 * 1024), 1)


class HostSignals:
    """This host's contention signals, read without privilege.

    Every signal that cannot be read is recorded as None and named in ``missing``, so the
    verdict is computed from what remains and says what it could not see.
    """

    def __init__(self, psi_cgroups: tuple[str, ...] = ()) -> None:
        self.psi_cgroups = tuple(psi_cgroups)

    def collect(self) -> dict:
        missing: list[str] = []
        reading: dict = {
            key: None
            for key in (
                "load1",
                "load5",
                "idle_pct",
                "swap_used_mb",
                "swapouts_total",
                "pageouts_total",
                "memory_free_pct",
                "pressure_level",
                "canary_seconds",
                "psi",
            )
        }
        reading["cores"] = os.cpu_count()
        try:
            load1, load5, _ = os.getloadavg()
            reading["load1"], reading["load5"] = round(load1, 2), round(load5, 2)
        except OSError:
            missing.append("loadavg")
        _read_psutil(reading, missing)
        if sys.platform == "darwin":
            _read_darwin(reading, missing)
        elif sys.platform.startswith("linux"):
            _read_linux(reading, missing, self.psi_cgroups)
        else:
            missing.append(f"platform {sys.platform}")
        out, elapsed = _run_tool([sys.executable, "-c", "pass"])
        reading["canary_seconds"] = round(elapsed, 3) if out is not None else None
        if out is None:
            missing.append("canary")
        reading["missing"] = missing
        return reading


# --- sampler and verdict (D-4) --------------------------------------------------------------

ADMIT, LOOK, HOLD = "ADMIT", "LOOK", "HOLD"
SAMPLES_FILE = "samples.jsonl"
VERDICT_FILE = "verdict.json"
#: A week at one sample a minute: Open Question 2 asks for a week of samples as the first
#: real data for re-tuning the thresholds.
RETAIN_SAMPLES = 7 * 24 * 60
WINDOW_FIELDS = (
    "load1",
    "idle_pct",
    "memory_free_pct",
    "swapouts_per_min",
    "pageouts_per_min",
    "canary_seconds",
)


def state_dir(config: dict) -> Path:
    return Path(str(settings(config)["state_dir"])).expanduser()


def thresholds(config: dict, host: str) -> dict:
    """Fleet defaults, overridden by an optional ``[hosts.<host>.thresholds]`` table."""
    merged = dict(config.get("defaults", {}).get("thresholds", {}))
    merged.update(config.get("hosts", {}).get(host, {}).get("thresholds", {}))
    return merged


def _rate(reading: dict, previous: dict | None, key: str, now: float, interval: float):
    """Counter delta per minute against the previous sample, or None across a gap/reset."""
    total = reading.get(f"{key}_total")
    before = (previous or {}).get(f"{key}_total")
    elapsed = now - (previous or {}).get("ts", now)
    if total is None or before is None or not 0 < elapsed <= 3 * interval:
        return None
    if total < before:
        return None
    return round((total - before) * 60.0 / elapsed, 1)


def build_record(
    reading: dict, previous: dict | None, *, host: str, now: float, interval: float
) -> dict:
    record = {"schema": SCHEMA, "ts": now, "host": host}
    record.update({key: value for key, value in reading.items() if key != "missing"})
    record["swapouts_per_min"] = _rate(reading, previous, "swapouts", now, interval)
    record["pageouts_per_min"] = _rate(reading, previous, "pageouts", now, interval)
    record["missing"] = list(reading.get("missing", []))
    return record


def read_samples(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    samples = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("ts"), (int, float)):
            samples.append(value)
    return samples


def _atomic_write(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _store_sample(path: Path, history: list[dict], record: dict) -> None:
    line = json.dumps(record, sort_keys=True) + "\n"
    if len(history) + 1 > RETAIN_SAMPLES:
        kept = history[-(RETAIN_SAMPLES - 1) :] + [record]
        _atomic_write(path, "".join(json.dumps(s, sort_keys=True) + "\n" for s in kept))
        return
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line)


def _mean(window: list[dict], key: str) -> float | None:
    values = [s[key] for s in window if isinstance(s.get(key), (int, float))]
    return round(sum(values) / len(values), 2) if values else None


def _window_summary(window: list[dict], seconds: float) -> dict:
    summary = {"seconds": seconds, "samples": len(window)}
    summary.update({key: _mean(window, key) for key in WINDOW_FIELDS})
    return summary


@dataclass(frozen=True)
class Windows:
    """The short and long windows' samples, the thresholds, and how full the long one is."""

    short: list[dict]
    long: list[dict]
    limits: dict
    count: int = 1
    long_n: int = 1


def _below(name: str, key: str, floor_key: str, unit: str):
    def check(w: Windows) -> tuple[str | None, str | None]:
        short, long_ = _mean(w.short, key), _mean(w.long, key)
        floor = w.limits[floor_key]
        if short is None or short >= floor:
            return None, None
        text = f"{name} {short}{unit} over 60 s and {long_}{unit} over 300 s, below {floor_key} {floor}"
        if long_ is not None and long_ < floor:
            return text, None
        return None, text + " (short window only)"

    return check


def _above(name: str, key: str, limit_key: str, unit: str):
    def check(w: Windows) -> tuple[str | None, str | None]:
        short, long_ = _mean(w.short, key), _mean(w.long, key)
        limit = w.limits[limit_key]
        if short is None or short <= limit:
            return None, None
        text = f"{name} {short}{unit} over 60 s and {long_}{unit} over 300 s, over {limit_key} {limit}"
        if long_ is not None and long_ > limit:
            return text, None
        return None, text + " (short window only)"

    return check


def _swapouts(w: Windows) -> tuple[str | None, str | None]:
    """Over the rate floor on each of the last ``swapouts_growth_samples`` rates.

    ``_thrash`` makes that a HOLD only beside a measured memory stall. A rise below ``swapouts_per_minute_hold``, or over it on fewer samples, is LOOK: a trickle
    is not a stall (1,400 pages/min measured on example-client-host-mac 2026-10-11 held at every count).
    """
    needed = int(w.limits["swapouts_growth_samples"])
    floor = w.limits["swapouts_per_minute_hold"]
    rates = [s.get("swapouts_per_min") for s in w.long[-needed:]]
    latest = rates[-1] if rates else None
    if not isinstance(latest, (int, float)) or latest <= 0:
        return None, None
    text = (
        f"swap-out counter rising ({rates} pages/min); {_mean(w.short, 'swapouts_per_min')} "
        f"over 60 s and {_mean(w.long, 'swapouts_per_min')} over 300 s"
    )
    stalled = len(rates) == needed and all(
        isinstance(rate, (int, float)) and rate > floor for rate in rates
    )
    if stalled:
        return (
            f"{text}, over swapouts_per_minute_hold {floor} on {needed} consecutive "
            "samples (swapouts_growth_samples)",
            None,
        )
    return (
        None,
        f"{text}; not over swapouts_per_minute_hold {floor} on {needed} samples",
    )


def _pressure(w: Windows) -> tuple[str | None, str | None]:
    hold = w.limits["pressure_level_hold"]
    latest = w.short[-1].get("pressure_level")
    if latest != hold:
        return None, None
    levels = sorted({str(s.get("pressure_level")) for s in w.long})
    return (
        f"kernel pressure level {latest} (pressure_level_hold); 60 s {latest}, "
        f"300 s levels {levels}",
        None,
    )


def _canary(w: Windows) -> tuple[str | None, str | None]:
    hold = w.limits["canary_spawn_seconds_hold"]
    last_two = [s.get("canary_seconds") for s in w.long[-2:]]
    over = [isinstance(v, (int, float)) and v > hold for v in last_two]
    if not over or not over[-1]:
        return None, None
    worst = max(v for v in (s.get("canary_seconds") for s in w.long) if v is not None)
    text = (
        f"canary spawn {last_two} s on the last two samples, over "
        f"canary_spawn_seconds_hold {hold}; {last_two[-1]} s over 60 s, max {worst} s "
        "over 300 s"
    )
    if len(over) == 2 and all(over):  # noqa: PLR2004 - "the last two samples" (T-1.2)
        return text, None
    return None, text + " (short window only)"


def _psi_over(
    w: Windows, fields: tuple[tuple[str, str], ...]
) -> tuple[str | None, str | None]:
    psi = w.short[-1].get("psi")
    if not isinstance(psi, dict):
        return None, None
    holds, looks = [], []
    for resource, kind in fields:
        values = psi.get(resource) or {}
        limit = w.limits[f"psi_{resource}_{kind}_pct"]
        avg60, avg300 = values.get(f"{kind}_avg60"), values.get(f"{kind}_avg300")
        if avg60 is None or avg60 <= limit:
            continue
        text = f"PSI {resource} {kind} {avg60}% over 60 s and {avg300}% over 300 s, over {limit}"
        (holds if avg300 is not None and avg300 > limit else looks).append(text)
    return ("; ".join(holds) or None), ("; ".join(looks) or None)


def _psi(w: Windows) -> tuple[str | None, str | None]:
    """A PSI ``full`` stall on cpu, memory or io: no task in the slice made progress."""
    return _psi_over(w, PSI_STALLS)


def _psi_cpu_some(w: Windows) -> tuple[str | None, str | None]:
    return _psi_over(w, (("cpu", "some"),))


def _memory_stall(w: Windows) -> str | None:
    """The measured memory stall that turns a swap or page-out rate into thrash.

    Linux reads PSI memory ``some``; a host without PSI (Darwin) reads the kernel level.
    """
    latest = w.short[-1]
    psi = latest.get("psi")
    if isinstance(psi, dict):
        some = (psi.get("memory") or {}).get("some_avg60")
        limit = w.limits["psi_memory_some_thrash_pct"]
        if isinstance(some, (int, float)) and some > limit:
            return f"PSI memory some {some}% over psi_memory_some_thrash_pct {limit}"
        return None
    level = latest.get("pressure_level")
    if level in w.limits["swap_thrash_pressure_levels"]:
        return f"kernel pressure level {level} (swap_thrash_pressure_levels)"
    return None


def _thrash(check):
    """HOLD on ``check``'s sustained rate only beside a memory stall; LOOK without one.

    ISSUE-REF: swap-out bursts with 17 of 30 GB available and memory PSI ~0 flapped HOLD.
    """

    def thrash(w: Windows) -> tuple[str | None, str | None]:
        hold, look = check(w)
        if not hold:
            return None, look
        stall = _memory_stall(w)
        if stall:
            return f"{hold}, with {stall}", None
        return None, f"{hold}; no memory stall measured (LOOK only, never HOLD)"

    return thrash


def _advisory(check):
    """A reading reported as LOOK and never HOLD: it is no measured stall by itself."""

    def advise(w: Windows) -> tuple[str | None, str | None]:
        hold, look = check(w)
        text = hold or look
        return None, (f"{text} (LOOK only, never HOLD)" if text else None)

    return advise


_swap_thrash = _thrash(_swapouts)
_pageout_thrash = _thrash(
    _above("pageouts", "pageouts_per_min", "pageouts_per_minute_hold", "/min")
)
#: The checks that can HOLD: each is an observed stall or a measured slowdown (the maintainer,
#: 2026-10-24: "only restrict agent launches ... if you actually observative machine resource
#: contention causing actual verifiable observable slowdown").
STALL_CHECKS = (_psi, _pressure, _canary, _swap_thrash, _pageout_thrash)
#: Readings that are LOOK at most, and LOOK never gates.
ADVISORY_CHECKS = (
    _advisory(_below("idle", "idle_pct", "idle_pct_floor", "%")),
    _advisory(_below("memory free", "memory_free_pct", "memory_free_pct_floor", "%")),
    _advisory(_psi_cpu_some),
)
CHECKS = STALL_CHECKS + ADVISORY_CHECKS


def _sample_breaches(sample: dict, limits: dict) -> bool:
    """Whether one sample reads any stall over its threshold (the hysteresis test).

    Only the stall checks count: an advisory reading never holds, so it cannot keep a HOLD
    from clearing. A thrash rate without a memory stall is LOOK and does not count either,
    nor does a sub-floor swap-out rise (verifier finding W-2).
    """
    window = Windows(short=[sample], long=[sample], limits=limits)
    single = Windows(
        short=[sample], long=[sample], limits={**limits, "swapouts_growth_samples": 1}
    )
    for check in STALL_CHECKS:
        if check in (_swap_thrash, _pageout_thrash):
            if check(single)[0]:
                return True
            continue
        hold, look = check(window)
        if hold or look:
            return True
    return False


def _look_only(latest: dict, budget: Budget, limits: dict) -> list[str]:
    reasons = []
    load, cores = latest.get("load1"), latest.get("cores")
    if isinstance(load, (int, float)) and cores:
        per_core = round(load / cores, 2)
        if per_core > limits["load_per_core_look"]:
            reasons.append(
                f"load {load} on {cores} cores = {per_core} per core, over "
                f"load_per_core_look {limits['load_per_core_look']} (LOOK only, never HOLD)"
            )
    units = latest.get("units")
    if budget.measured and isinstance(units, int):
        if units >= budget.operating_target_units:
            reasons.append(
                f"units {units} >= operating_target {budget.operating_target_units}"
            )
    return reasons


def _windows(samples: list[dict], config: dict, limits: dict) -> Windows:
    """The two windows over the samples taken within the long window of the latest one.

    A sample older than that (the scheduler was stopped, the machine slept) is not history:
    a long window assembled across a gap would be a verdict from stale readings.
    """
    values = settings(config)
    interval = float(values["sample_interval_seconds"])
    short_n = max(1, math.ceil(values["window_short_seconds"] / interval))
    long_seconds = float(values["window_long_seconds"])
    latest = samples[-1]["ts"]
    recent = [s for s in samples if s["ts"] > latest - long_seconds - interval / 2]
    long_n = max(short_n, math.ceil(long_seconds / interval))
    return Windows(
        short=recent[-short_n:],
        long=recent[-long_n:],
        limits=limits,
        count=min(len(recent), long_n),
        long_n=long_n,
    )


def _held(previous: dict | None, now: float) -> dict | None:
    if not isinstance(previous, dict) or previous.get("verdict") != HOLD:
        return None
    expires = previous.get("expires_at")
    return previous if isinstance(expires, (int, float)) and expires > now else None


def compute_verdict(
    samples: list[dict], *, config: dict, budget: Budget, previous: dict | None
) -> dict:
    """ADMIT, LOOK or HOLD over the short and long windows, with hysteresis on HOLD."""
    limits = thresholds(config, budget.host)
    windows = _windows(samples, config, limits)
    latest = samples[-1]
    missing = [f"signal unavailable: {name}" for name in latest.get("missing", [])]
    holds, looks = [], []
    for check in CHECKS:
        hold, look = check(windows)
        holds += [hold] if hold else []
        looks += [look] if look else []
    looks += _look_only(latest, budget, limits)
    held = _held(previous, latest["ts"])
    if windows.count < windows.long_n and held is None:
        outcome = Outcome(ADMIT, [f"insufficient history ({windows.count} samples)"])
    elif holds:
        outcome = Outcome(HOLD, holds + looks)
    elif held is not None:
        outcome = _hysteresis(held, windows, looks)
    else:
        outcome = Outcome(LOOK if looks else ADMIT, looks)
    if outcome.verdict == HOLD:
        outcome.held_since = (held or {}).get("held_since") or latest["ts"]
    outcome.reasons += missing
    return _verdict_document(outcome, windows, latest, budget, config)


@dataclass
class Outcome:
    verdict: str
    reasons: list[str]
    held_since: float | None = None


def _hysteresis(held: dict, windows: Windows, looks: list[str]) -> Outcome:
    """A previous HOLD stands until every signal reads clear for a full long window."""
    clear = sum(not _sample_breaches(s, windows.limits) for s in windows.long)
    if windows.count >= windows.long_n and clear >= windows.long_n:
        return Outcome(LOOK if looks else ADMIT, looks)
    since = held.get("held_since") or held.get("observed_at")
    return Outcome(
        HOLD,
        [
            f"holding since {since}: clears after a full long window reads clear "
            f"({clear}/{windows.long_n} clear samples)",
            *looks,
        ],
    )


def _verdict_document(
    outcome: Outcome, windows: Windows, latest: dict, budget: Budget, config: dict
) -> dict:
    values = settings(config)
    observed = latest["ts"]
    verdict, reasons, held_since = outcome.verdict, outcome.reasons, outcome.held_since
    return {
        "schema": SCHEMA,
        "host": budget.host,
        "observed_at": observed,
        "expires_at": observed + float(values["max_sample_age_seconds"]),
        "verdict": verdict,
        "held_since": held_since,
        "reasons": reasons,
        "windows": {
            "short": _window_summary(windows.short, values["window_short_seconds"]),
            "long": _window_summary(windows.long, values["window_long_seconds"]),
        },
        "budget": {
            "operating_target_units": budget.operating_target_units,
            "hard_stop_units": budget.hard_stop_units,
        },
        "units": latest.get("units"),
        "units_detail": latest.get("units_detail"),
        "latest": {
            key: latest.get(key)
            for key in (
                "cores",
                "load1",
                "load5",
                "idle_pct",
                "swap_used_mb",
                "swapouts_per_min",
                "pageouts_per_min",
                "memory_free_pct",
                "pressure_level",
                "canary_seconds",
            )
        },
    }


def sample(
    *,
    provider=None,
    config: dict | None = None,
    host: str | None = None,
    now: float | None = None,
    units: dict | None = None,
) -> dict:
    """Take one sample, append it, and atomically replace the verdict. Returns the verdict."""
    document = load_config() if config is None else config
    name = resolve_host() if host is None else host
    directory = state_dir(document)
    directory.mkdir(parents=True, exist_ok=True)
    samples_path = directory / SAMPLES_FILE
    history = read_samples(samples_path)
    psi_cgroups = tuple(settings(document).get("psi_cgroups", ()))
    reading = (provider or HostSignals(psi_cgroups)).collect()
    moment = time.time() if now is None else now
    counted = _count_for(document) if units is None else units
    record = build_record(
        reading,
        history[-1] if history else None,
        host=name,
        now=moment,
        interval=float(settings(document)["sample_interval_seconds"]),
    )
    record["units"] = counted["units"]
    record["units_detail"] = counted["units_detail"]
    _store_sample(samples_path, history, record)
    verdict = compute_verdict(
        [*history, record],
        config=document,
        budget=load_budget(name, config=document),
        previous=_read_json(directory / VERDICT_FILE),
    )
    _atomic_write(directory / VERDICT_FILE, json.dumps(verdict, indent=2) + "\n")
    return verdict


SCHEDULER_FILE = "scheduler.json"
SCHEDULERS = ("launchd", "systemd", "none")


def record_scheduler(config: dict, kind: str) -> Path:
    """Record which scheduler install.sh set up; ``none`` arms the hook's own sampling."""
    directory = state_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SCHEDULER_FILE
    record = {"schema": SCHEMA, "scheduler": kind, "recorded_at": time.time()}
    _atomic_write(path, json.dumps(record) + "\n")
    return path


def recorded_scheduler(config: dict) -> str | None:
    record = _read_json(state_dir(config) / SCHEDULER_FILE) or {}
    kind = record.get("scheduler")
    return kind if kind in SCHEDULERS else None


def read_verdict(
    config: dict, now: float | None = None
) -> tuple[dict | None, str | None]:
    """``(verdict, None)``, or ``(None, diagnosis)`` when it is missing, stale or malformed.

    The ``heavy_test_lease._pressure_denial_active`` posture: every unusable state is a
    named diagnosis and an admission, never a refusal.
    """
    path = state_dir(config) / VERDICT_FILE
    moment = time.time() if now is None else now
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"verdict.json missing at {path}"
    except (OSError, ValueError) as error:
        return None, f"verdict.json at {path} is unreadable or malformed ({error})"
    observed = value.get("observed_at") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema") != SCHEMA
        or value.get("verdict") not in {ADMIT, LOOK, HOLD}
        or not isinstance(observed, (int, float))
        or not isinstance(value.get("reasons"), list)
    ):
        return None, f"verdict.json at {path} is malformed"
    limit = float(settings(config)["max_sample_age_seconds"])
    if moment - observed > limit:
        return None, (
            f"verdict.json is {moment - observed:.0f} s old, older than "
            f"max_sample_age_seconds {limit:.0f}"
        )
    return value, None


# --- the full-suite slot (ISSUE-REF) --------------------------------------------------------
#
# The fleet rule "nothing else runs at -n while a full suite holds the slot" had no state: a
# full suite was invisible to every other caller, so the push gate ran at -n 2 beside one.
# A running full suite now leaves this pid-carrying marker; a dead or recycled pid is stale.

FULL_SUITE_FILE = "full-suite.json"


def write_full_suite_marker(
    config: dict, argv: list[str], cwd: str
) -> tuple[Path | None, dict | None]:
    """Record this process as the full-suite holder.

    ``(path, None)`` once written, or ``(None, holder)`` when a live holder already has the
    slot: its marker is left exactly as it is (ISSUE-REF), so a pytest started inside or
    beside a full suite can neither replace the holder nor erase it at its own exit.
    """
    directory = state_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / FULL_SUITE_FILE
    holder, _ = full_suite_holder(config)
    if holder is not None:
        return None, holder
    pid = os.getpid()
    marker = {
        "schema": SCHEMA,
        "pid": pid,
        "process_start": process_liveness.start_token(pid),
        "started_at": time.time(),
        "argv": list(argv),
        "cwd": cwd,
    }
    _atomic_write(path, json.dumps(marker, indent=2) + "\n")
    return path, None


def remove_full_suite_marker(path: Path) -> None:
    """Remove the marker only while it still names this process: pid and process start."""
    marker = _read_json(path)
    pid = os.getpid()
    if (
        marker is not None
        and marker.get("pid") == pid
        and marker.get("process_start") == process_liveness.start_token(pid)
    ):
        path.unlink(missing_ok=True)


def full_suite_holder(config: dict) -> tuple[dict | None, str | None]:
    """``(marker, None)`` while a live full suite holds the slot, else ``(None, note)``.

    The note is None when no marker exists, and names the marker when it was ignored.
    """
    path = state_dir(config) / FULL_SUITE_FILE
    if not path.exists():
        return None, None
    marker = _read_json(path)
    pid = marker.get("pid") if marker else None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None, f"malformed full-suite marker at {path} ignored"
    if not process_liveness.pid_alive(pid):
        return None, f"stale full-suite marker ignored (pid {pid} is not running)"
    recorded = marker.get("process_start")
    current = process_liveness.start_token(pid)
    if isinstance(recorded, str) and current is not None and recorded != current:
        return None, (
            f"stale full-suite marker ignored (pid {pid} is a different process "
            "than the suite that wrote it)"
        )
    return marker, None


def full_suite_line(marker: dict) -> str:
    started = marker.get("started_at")
    since = (
        datetime.fromtimestamp(started, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(started, (int, float))
        else "an unknown time"
    )
    argv = marker.get("argv")
    command = " ".join(str(a) for a in argv) if isinstance(argv, list) else "unknown"
    return f"full-suite slot held by pid {marker['pid']} since {since} ({command})"


def reading_line(verdict: dict, now: float | None = None) -> str:
    latest = verdict.get("latest") or {}
    age = (time.time() if now is None else now) - verdict["observed_at"]
    return (
        f"load {latest.get('load1')}/{latest.get('cores')} cores; idle "
        f"{latest.get('idle_pct')}%; swap-outs {latest.get('swapouts_per_min')}/min; "
        f"memory free {latest.get('memory_free_pct')}%; pressure "
        f"{latest.get('pressure_level')}; canary {latest.get('canary_seconds')} s; "
        f"verdict {verdict['verdict']} (observed {age:.0f} s ago)"
    )


def budget_line(budget: Budget) -> str:
    if not budget.measured:
        return f"no measured budget for {budget.host or '<unset AI_HOST>'}"
    return (
        f"budget: operating target {budget.operating_target_units} units, hard stop "
        f"{budget.hard_stop_units} units (measured {budget.measured_on}; {budget.cores} "
        f"cores, {budget.reserved_cores} reserved)"
    )


def units_line(counted: dict) -> str:
    if counted["units"] is None:
        return f"units: unknown ({counted['error']})"
    detail = counted["units_detail"]
    return (
        f"units: {counted['units']} (agents {detail['agents']}, background "
        f"{detail['background']}, cx {detail['cx']}, recent launches "
        f"{detail['recent_launches']}; {len(counted['stale'])} stale markers ignored)"
    )


def _cmd_status(args, config: dict) -> int:
    budget = load_budget(args.host, config=config)
    counted = _count_for(config)
    verdict, diagnosis = read_verdict(config)
    report = {
        "host": args.host,
        "budget": budget.__dict__,
        "units": counted["units"],
        "units_detail": counted["units_detail"],
        "units_error": counted["error"],
        "stale": counted["stale"],
        "idle": counted["idle"],
        "verdict": verdict,
        "verdict_error": diagnosis,
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    print(f"machine-contention status: host {args.host or '<unset AI_HOST>'}")
    print(budget_line(budget))
    print(units_line(counted))
    if verdict is None:
        print(f"verdict: unavailable ({diagnosis})")
        return 0
    print(f"verdict: {verdict['verdict']}")
    for reason in verdict["reasons"]:
        print(f"  - {reason}")
    print(f"reading: {reading_line(verdict)}")
    return 0


def _cmd_sample(args, config: dict) -> int:
    """Silent unless asked: the scheduler runs this every minute into an unrotated log."""
    verdict = sample(config=config, host=args.host)
    if args.verbose:
        print(
            f"machine-contention: {verdict['verdict']}; "
            + "; ".join(verdict["reasons"])
        )
    return 0


# --- admission (D-3) ------------------------------------------------------------------------

REFUSE = 2
#: The ``admit --hook`` refusal code the wrapper maps to Claude Code's blocking exit 2. Not 2
#: itself: argparse and an uncaught interpreter error already exit 2 or 1, and neither may
#: ever read as a refused launch.
HOOK_REFUSE = 3
FAIL_OPEN = "; admitting without a contention check"
INLINE = "work this task inline and announce the hold to peers"
AGENT_TOOLS = frozenset({"Agent", "Task"})


@dataclass(frozen=True)
class Admission:
    """What the launch gate says: an exit code, and at most one of three messages."""

    code: int = 0
    context: str | None = None
    diagnosis: str | None = None
    refusal: str | None = None


def _figures(counted: dict, budget: Budget, verdict: dict) -> str:
    detail = counted["units_detail"]
    return (
        f"units {counted['units']} (agents {detail['agents']}, background "
        f"{detail['background']}, cx {detail['cx']}, recent launches "
        f"{detail['recent_launches']}) against operating target "
        f"{budget.operating_target_units} and hard stop {budget.hard_stop_units}; "
        + reading_line(verdict)
    )


def caller_session() -> str | None:
    """The launching session's name, or None when nothing names it.

    ``claude_sessions.caller_session_name``: ``CLAUDE_CODE_TASK_LIST_ID`` (which ``ai c``
    pins to the session name, and which a hook and a sub-agent inherit from the session
    process), else the session registry, else this process's ``claude`` ancestry. Read only
    at HOLD, where it decides the launch.
    """
    try:
        import claude_sessions

        return claude_sessions.caller_session_name()
    except (ImportError, OSError, ValueError):
        return None


def _fresh_verdict(
    config: dict, host: str, now: float | None
) -> tuple[dict | None, str | None]:
    verdict, diagnosis = read_verdict(config, now)
    if verdict is None and recorded_scheduler(config) == "none":
        # T-1.5: where install found no user scheduler, the hook keeps the verdict fresh
        # itself. Never otherwise: a sampling hook cannot measure persistence (D-4).
        sample(config=config, host=host)
        verdict, diagnosis = read_verdict(config)
    return verdict, diagnosis


def _hold(config: dict, host: str, verdict: dict, figures: str) -> Admission:
    """Refuse at HOLD unless the launching session is on the host's admit list.

    D-5 revision (the maintainer 2026-10-24 01:09Z, ISSUE-REF): HOLD means an observed stall, so it
    refuses on a host with no unit budget too; a session whose name starts with one of
    ``hold_admit_session_prefixes`` is admitted with the stall as context. An unreadable
    name matches nothing.
    """
    prefixes = hold_admit_prefixes(config, host)
    name = caller_session() if prefixes else None
    matched = next((p for p in prefixes if name and name.startswith(p)), None)
    stall = f"a persistent stall is measured on {host} (verdict HOLD, both windows)"
    if matched is not None:
        return Admission(
            context=(
                f"machine-contention: {stall}: {'; '.join(verdict['reasons'])}. The launch "
                f"is admitted by priority: session {name} matches "
                f"{ADMIT_PREFIXES_KEY} entry {matched!r}. {figures}"
            )
        )
    listed = ", ".join(repr(p) for p in prefixes) or "none"
    if prefixes and name:
        who = f"Launching session {name} is not on"
    elif prefixes:
        who = "The launching session's name is unreadable, so it is not on"
    else:
        who = "No session is on"
    reasons = "\n".join(f"  - {reason}" for reason in verdict["reasons"])
    return Admission(
        code=REFUSE,
        refusal=(
            f"machine-contention: launch refused on {host}: {stall}.\n{reasons}\n"
            f"{figures}\n{who} this host's {ADMIT_PREFIXES_KEY} ({listed}).\n"
            f"Do not launch: {INLINE}. `machine_contention.py status` shows when it "
            "clears."
        ),
    )


def _unmeasured(
    config: dict, host: str, budget: Budget, verdict: dict, now: float | None
) -> Admission:
    """No unit budget (D-5): the verdict alone decides, and HOLD refuses (ISSUE-REF)."""
    figures = f"{budget_line(budget)}; {reading_line(verdict, now)}"
    if verdict["verdict"] == HOLD:
        return _hold(config, host, verdict, figures)
    if verdict["verdict"] == ADMIT:
        return Admission()
    return Admission(
        context=(
            f"machine-contention: {figures}; admitted, no persistent stall is measured: "
            + "; ".join(verdict["reasons"])
        )
    )


def admit(
    *, config: dict, host: str, tool_use_id: str | None = None, now: float | None = None
) -> Admission:
    """The launch decision: refuse only on HOLD; quote the reading from the target up."""
    budget = load_budget(host, config=config)
    verdict, diagnosis = _fresh_verdict(config, host, now)
    if verdict is None:
        return Admission(diagnosis=f"machine-contention: {diagnosis}{FAIL_OPEN}")
    if not budget.measured:
        return _unmeasured(config, host, budget, verdict, now)
    counted = _count_for(config, exclude_tool_use_id=tool_use_id, now=now)
    if counted["units"] is None:
        return Admission(
            diagnosis=f"machine-contention: unit count unavailable "
            f"({counted['error']}){FAIL_OPEN}"
        )
    figures = _figures(counted, budget, verdict)
    if verdict["verdict"] == HOLD:
        return _hold(config, host, verdict, figures)
    return _budget_reading(host, budget, counted["units"], figures)


def _budget_reading(host: str, budget: Budget, units: int, figures: str) -> Admission:
    """Below HOLD on a measured host: the unit count is quoted context, never a refusal."""
    if units >= budget.hard_stop_units:
        return Admission(
            context=(
                f"machine-contention: units {units} is at the hard stop "
                f"{budget.hard_stop_units} on {host}; the launch is admitted because no "
                f"persistent stall is measured. Prefer working inline. {figures}"
            )
        )
    if units >= budget.operating_target_units:
        return Admission(
            context=(
                f"machine-contention: units {units} is at or above the operating target "
                f"{budget.operating_target_units} on {host}; quote this reading before "
                f"launching more. {figures}"
            )
        )
    return Admission()


def hook_main(stream=None) -> int:
    """``PreToolUse(Agent)``: exit ``HOOK_REFUSE`` only on HOLD, 0 on everything else."""
    try:
        payload = json.loads((stream or sys.stdin).read() or "{}")
    except (OSError, ValueError) as error:
        print(
            f"machine-contention: unparseable hook payload ({error}){FAIL_OPEN}",
            file=sys.stderr,
        )
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") not in AGENT_TOOLS:
        return 0
    try:
        config = load_config()
    except ConfigError as error:
        print(f"machine-contention: {error}{FAIL_OPEN}", file=sys.stderr)
        return 0
    tool_use_id = payload.get("tool_use_id")
    decision = admit(
        config=config,
        host=resolve_host(),
        tool_use_id=tool_use_id if isinstance(tool_use_id, str) else None,
    )
    if decision.diagnosis:
        print(decision.diagnosis, file=sys.stderr)
    if decision.context:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": decision.context,
                    }
                }
            )
        )
    if decision.code == REFUSE:
        print(decision.refusal, file=sys.stderr)
        return HOOK_REFUSE
    return 0


#: The launch kinds besides an Agent call that consult the same decision (Phase 3).
GATE_KINDS = ("cx", "heavy-test")


def launch_gate(label: str, stream=None) -> bool:
    """The Agent hook's decision for a non-Agent launch; True only on a HOLD it refuses.

    Every message goes to ``stream`` (stderr): the fail-open diagnosis, the reading from the
    operating target up, and the refusal, prefixed with ``label``, quoting the same figures
    the hook quotes. An unreadable config fails open exactly as the hook does.
    """
    out = sys.stderr if stream is None else stream
    try:
        config = load_config()
    except ConfigError as error:
        print(f"machine-contention: {error}{FAIL_OPEN}", file=out)
        return False
    decision = admit(config=config, host=resolve_host())
    for text in (decision.diagnosis, decision.context):
        if text:
            print(text, file=out)
    if decision.code != REFUSE:
        return False
    print(f"{label}: {decision.refusal}", file=out)
    return True


def pytest_worker_cap() -> int | None:
    """``[defaults] pytest_max_workers``, or None when the config cannot be read."""
    try:
        value = settings(load_config()).get("pytest_max_workers")
    except ConfigError:
        return None
    return value if isinstance(value, int) and value > 0 else None


def _cmd_admit(args, config: dict) -> int:
    decision = admit(config=config, host=args.host)
    if args.json:
        print(json.dumps(decision.__dict__, indent=2))
        return decision.code
    for text in (decision.diagnosis, decision.context, decision.refusal):
        if text:
            print(text)
    if not any((decision.diagnosis, decision.context, decision.refusal)):
        print("machine-contention: admit (below the operating target, no stall)")
    return decision.code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, help="budget config (default: shipped)")
    parser.add_argument("--host", default=None, help="host profile (default: $AI_HOST)")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="print the budget, units and verdict")
    status.add_argument("--json", action="store_true")
    commands.add_parser(
        "sample", help="take one sample and replace verdict.json"
    ).add_argument("--verbose", action="store_true", help="print the verdict")
    admission = commands.add_parser(
        "admit", help="the launch decision; exit 2 only on a measured HOLD"
    )
    commands.add_parser(
        "gate",
        help="the admit decision for a non-Agent launch; exit 3 only on a measured HOLD",
    ).add_argument("kind", choices=GATE_KINDS)
    commands.add_parser(
        "config-value", help="print one [defaults] value (state_dir expanded)"
    ).add_argument("key")
    commands.add_parser(
        "host-value", help="print one [hosts.<host>] value; exit 1 when unset"
    ).add_argument("key", choices=HOST_VALUE_KEYS)
    commands.add_parser(
        "record-scheduler", help="record which scheduler install.sh set up"
    ).add_argument("kind", choices=SCHEDULERS)
    admission.add_argument("--json", action="store_true")
    admission.add_argument(
        "--hook", action="store_true", help="PreToolUse(Agent) mode: payload on stdin"
    )
    baseline = commands.add_parser(
        "baseline", help="per-root-ancestor CPU by group; suggests reserved_cores"
    )
    baseline.add_argument("--interval", type=float, default=10.0)
    baseline.add_argument("--top", type=int, default=15)
    baseline.add_argument("--json", action="store_true")
    return parser


def _cmd_config_value(args, config: dict) -> int:
    """One ``[defaults]`` value for install.sh, which renders the scheduler from it."""
    if args.key == "state_dir":
        print(state_dir(config))
        return 0
    value = settings(config).get(args.key)
    if value is None:
        print(f"machine-contention: no [defaults] {args.key}", file=sys.stderr)
        return 2
    print(value)
    return 0


#: The ``[hosts.<host>]`` keys install reads through ``host-value``.
HOST_VALUE_KEYS = ("vm_swappiness",)


def _cmd_host_value(args, config: dict) -> int:
    """One ``[hosts.<host>]`` value for install; exit 1 when this host does not set it."""
    value = config.get("hosts", {}).get(args.host, {}).get(args.key)
    if value is None:
        print(f"machine-contention: no [hosts.{args.host}] {args.key}", file=sys.stderr)
        return 1
    print(value)
    return 0


def _cmd_record_scheduler(args, config: dict) -> int:
    record_scheduler(config, args.kind)
    return 0


# --- baseline (T-1.6) -----------------------------------------------------------------------

BASELINE_FALLBACK_GROUP = "system"
PSUTIL_HINT = "uv run --with psutil python scripts/machine_contention.py baseline"


def _group_of(chain: list[str], groups: dict[str, list[str]]) -> str:
    """The group of the nearest name in ``chain`` (the process first) that any pattern matches."""
    for name in chain:
        folded = name.casefold()
        for group, patterns in groups.items():
            if any(pattern.casefold() in folded for pattern in patterns):
                return group
    return BASELINE_FALLBACK_GROUP


def _chain(pid: int, by_pid: dict, limit: int = 64) -> list[str]:
    names: list[str] = []
    current = by_pid.get(pid)
    while current is not None and len(names) < limit:
        names.append(current.name)
        parent = by_pid.get(current.ppid)
        if parent is None or parent.pid == current.pid:
            break
        current = parent
    return names


def baseline_report(
    before: list, after: list, *, interval: float, groups: dict[str, list[str]]
) -> dict:
    """Cores per group and per root ancestor between two ``diagnose_machine_load`` snapshots.

    The root is the topmost ancestor BELOW the init process: every macOS process descends from
    launchd (pid 1), so ``find_root`` over the whole table names one root for the machine. With
    pid 1 left out of the table it stops one level down, at the agent, app or login that
    started the subtree.
    """
    import diagnose_machine_load as dml

    prior = {(p.pid, p.create_time): p.cpu_time for p in before}
    by_pid = {p.pid: p for p in after}
    below_init = {pid: p for pid, p in by_pid.items() if pid > 1}
    totals = dict.fromkeys([*groups, BASELINE_FALLBACK_GROUP], 0.0)
    roots: dict[int, float] = {}
    for proc in after:
        used = proc.cpu_time - prior.get((proc.pid, proc.create_time), 0.0)
        cores = max(0.0, used) / interval
        totals[_group_of(_chain(proc.pid, by_pid), groups)] += cores
        root, _orphan = dml.find_root(proc.pid, below_init)
        roots[root] = roots.get(root, 0.0) + cores
    rows = [
        {
            "root_pid": pid,
            "root_name": by_pid[pid].name if pid in by_pid else f"<gone:{pid}>",
            "group": _group_of(_chain(pid, by_pid), groups),
            "cores": round(cores, 2),
        }
        for pid, cores in roots.items()
    ]
    rows.sort(key=lambda row: row["cores"], reverse=True)
    rounded = {group: round(value, 2) for group, value in totals.items()}
    return {
        "interval_seconds": interval,
        "groups": rounded,
        "roots": rows,
        "suggested_reserved_cores": rounded.get("endpoint_agents", 0.0),
    }


def _print_baseline(report: dict, args, budget: Budget) -> None:
    print(
        f"machine-contention baseline: host {args.host or '<unset AI_HOST>'}, "
        f"{args.interval:g} s interval, {os.cpu_count()} cores"
    )
    for group, cores in report["groups"].items():
        print(f"group {group}: {cores} cores")
    print("top root ancestors:")
    for row in report["roots"][: args.top]:
        print(
            f"  {row['cores']:>6} cores  pid {row['root_pid']:<7} {row['root_name']}  "
            f"[{row['group']}]"
        )
    configured = (
        f"configured {budget.reserved_cores} (measured {budget.measured_on})"
        if budget.measured
        else "no measured budget for this host"
    )
    print(
        f"suggested reserved_cores = {report['suggested_reserved_cores']} (the "
        f"endpoint_agents sum); {configured}"
    )


def _cputime_seconds(text: str) -> float | None:
    """``ps``'s cumulative CPU time: ``[dd-]hh:mm:ss`` on Linux, ``mmm:ss.cc`` on macOS."""
    days, _, clock = text.rpartition("-")
    try:
        total = float(days or 0) * 86400
        parts = [float(part) for part in clock.split(":")]
    except ValueError:
        return None
    for multiplier, value in zip((1, 60, 3600), reversed(parts), strict=False):
        total += multiplier * value
    return total


def ps_cpu_seconds() -> dict[int, float]:
    """Cumulative CPU seconds per pid from ``ps``, which reads processes psutil cannot.

    psutil reads another user's process times through ``proc_pidinfo``, which macOS refuses
    for root-owned processes; ``/bin/ps`` reports them. Empty where ``ps`` is absent.
    """
    out, _ = _run_tool(["ps", "-A", "-o", "pid=,time="])
    times: dict[int, float] = {}
    for line in (out or "").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit():  # noqa: PLR2004 - pid, time
            seconds = _cputime_seconds(fields[1])
            if seconds is not None:
                times[int(fields[0])] = seconds
    return times


def fill_cpu_from_ps(procs: list, times: dict[int, float]) -> tuple[list, int]:
    """Give every process psutil could not read its ``ps`` CPU time; return how many."""
    filled = []
    count = 0
    for proc in procs:
        if not proc.accessible and proc.pid in times:
            filled.append(dataclasses.replace(proc, cpu_time=times[proc.pid]))
            count += 1
        else:
            filled.append(proc)
    return filled, count


def _cmd_baseline(args, config: dict) -> int:
    try:
        import diagnose_machine_load as dml
    except (ImportError, SystemExit):
        print(
            "machine-contention: baseline needs psutil, which is not importable here; "
            f"run `{PSUTIL_HINT}`",
            file=sys.stderr,
        )
        return 1
    groups = config.get("defaults", {}).get("baseline_groups", {})
    before, _ = fill_cpu_from_ps(dml.snapshot(), ps_cpu_seconds())
    time.sleep(args.interval)
    after, filled = fill_cpu_from_ps(dml.snapshot(), ps_cpu_seconds())
    report = baseline_report(before, after, interval=args.interval, groups=groups)
    report["cpu_from_ps"] = filled
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    _print_baseline(report, args, load_budget(args.host, config=config))
    return 0


COMMANDS = {
    "status": _cmd_status,
    "sample": _cmd_sample,
    "admit": _cmd_admit,
    "config-value": _cmd_config_value,
    "host-value": _cmd_host_value,
    "record-scheduler": _cmd_record_scheduler,
    "baseline": _cmd_baseline,
}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "admit" and args.hook:
        return hook_main()
    if args.command == "gate":
        return HOOK_REFUSE if launch_gate(args.kind) else 0
    if args.host is None:
        args.host = resolve_host()
    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f"machine-contention: {error}", file=sys.stderr)
        return 2
    return COMMANDS[args.command](args, config)


if __name__ == "__main__":
    raise SystemExit(main())
