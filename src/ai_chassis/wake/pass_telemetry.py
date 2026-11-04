"""Durable per-pass dispatcher telemetry, slow-pass host snapshots and the stuck detector.

Every completed dispatcher pass appends one JSON line to ``<dispatch>/passes.jsonl``: its
total and per-phase durations and the slowest resume units. A pass over the alarm bound
(``tick_watchdog_s``) also carries a host resource snapshot. That alarm only records; it
never interrupts or restores anything.

The only restore page is the stuck detector's: a pass with no progress for
``dispatch_stuck_limit_s``, a dispatcher that started ``dispatch_crash_loop_starts`` times
within ``dispatch_crash_loop_window_s``, or (from the ``health`` command) a dispatcher whose
recorded process is gone. Pages append to ``<dispatch>/restore-pages.jsonl``.

``python -m ai_chassis.wake.pass_telemetry review --log <passes.jsonl> ...`` reports
pass-duration percentiles per host and lists slow passes with their snapshots.
"""

from __future__ import annotations

import argparse
import contextlib
import heapq
import json
import math
import os
from pathlib import Path
import sys
import threading
import time

PROC_ROOT = Path("/proc")
SYS_BLOCK = Path("/sys/block")
PASS_LOG = "passes.jsonl"
RESTORE_PAGES = "restore-pages.jsonl"
RESUME_TOP = 8
SCHEMA = 1
PSI_KINDS = ("cpu", "memory", "io")
MEMINFO_KEYS = ("MemTotal", "MemFree", "MemAvailable", "SwapTotal", "SwapFree")
ZRAM_FIELDS = (
    "orig_data_size",
    "compr_data_size",
    "mem_used_total",
    "mem_limit",
    "mem_used_max",
    "same_pages",
    "pages_compacted",
    "huge_pages",
    "huge_pages_since",
)
CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
PERCENTILES = (50, 90, 95, 99)


_SETTINGS = {}


def _stamp(path):
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def call_time_settings():
    """``[agent_mail_notify]`` as of now, re-parsed only when a config file changed.

    The parse costs about 9 ms (measured on example-project-ec2); the two stats cost microseconds,
    so every pass still sees an edit made since the previous one.
    """
    from ai_chassis import agent_mail_notifier as mail
    from ai_chassis.config_tables import overlay_config_path

    key = (
        _stamp(mail.CONFIG_PATH),
        _stamp(overlay_config_path()),
        os.environ.get("AI_HOST"),
    )
    if _SETTINGS.get("key") != key:
        _SETTINGS.update(key=key, value=mail.load_config())
    return dict(_SETTINGS["value"])


def _unreadable(error):
    return {"unreadable": f"{type(error).__name__}: {error}"}


def _cpu_counters():
    line = (PROC_ROOT / "stat").read_text().splitlines()[0].split()
    if line[0] != "cpu":
        raise ValueError("no aggregate cpu line")
    return dict(zip(CPU_FIELDS, (int(value) for value in line[1:]), strict=False))


def _vmstat():
    values = {}
    for line in (PROC_ROOT / "vmstat").read_text().splitlines():
        name, _, value = line.partition(" ")
        if name in {"pswpin", "pswpout"}:
            values[name] = int(value)
    if len(values) != 2:  # noqa: PLR2004 - pswpin and pswpout
        raise ValueError("pswpin/pswpout absent")
    return values


def start_counters():
    """Cumulative counters read at pass start, so a slow pass can report its own window."""
    counters = {}
    for name, read in (("cpu", _cpu_counters), ("vmstat", _vmstat)):
        try:
            counters[name] = read()
        except (OSError, ValueError, IndexError) as error:
            counters[name] = _unreadable(error)
    return counters


def _psi(kind):
    rows = {}
    for line in (PROC_ROOT / "pressure" / kind).read_text().splitlines():
        label, *pairs = line.split()
        rows[label] = {
            key: float(value) for key, value in (pair.split("=") for pair in pairs)
        }
    if "some" not in rows:
        raise ValueError("no some line")
    return rows


def _load_per_core():
    loads, cores = os.getloadavg(), os.cpu_count() or 1
    return {
        name: round(value / cores, 3)
        for name, value in zip(("1m", "5m", "15m"), loads, strict=True)
    }


def _memory():
    values = {}
    for line in (PROC_ROOT / "meminfo").read_text().splitlines():
        name, _, rest = line.partition(":")
        if name in MEMINFO_KEYS:
            values[f"{name}_kb"] = int(rest.split()[0])
    if not values:
        raise ValueError("no meminfo fields")
    return values


def _swap(start):
    end = _vmstat()
    swap = dict(end)
    if "unreadable" in start:
        swap["delta_unreadable"] = start["unreadable"]
    else:
        for name, value in end.items():
            swap[f"{name}_delta"] = value - start[name]
    return swap


def _zram():
    devices = {}
    for device in sorted(SYS_BLOCK.glob("zram*")):
        fields = (device / "mm_stat").read_text().split()
        devices[device.name] = dict(
            zip(ZRAM_FIELDS, (int(value) for value in fields), strict=False)
        )
    if not devices:
        raise FileNotFoundError(f"no zram device under {SYS_BLOCK}")
    return devices


def _iowait(start):
    end = _cpu_counters()
    total = sum(end.values())
    result = {"since_boot_fraction": round(end["iowait"] / total, 4) if total else None}
    if "unreadable" in start:
        result["pass_unreadable"] = start["unreadable"]
    else:
        spent = total - sum(start.values())
        waited = end["iowait"] - start["iowait"]
        result["pass_fraction"] = round(waited / spent, 4) if spent > 0 else None
    return result


def _contention(path, now):
    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"verdict.json missing at {path}")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or "verdict" not in value:
        raise ValueError(f"verdict.json at {path} is malformed")
    observed = value.get("observed_at")
    return {
        "verdict": value["verdict"],
        "reasons": value.get("reasons", []),
        "age_s": round(now - observed, 1)
        if isinstance(observed, (int, float))
        else None,
    }


def host_snapshot(start, *, verdict_path, now=None):
    """Each field is its reading or ``{"unreadable": reason}``; one bad source spoils none."""
    now = time.time() if now is None else now
    readers = {
        "load_per_core": _load_per_core,
        "swap": lambda: _swap(start.get("vmstat", {"unreadable": "not sampled"})),
        "zram": _zram,
        "memory": _memory,
        "iowait": lambda: _iowait(start.get("cpu", {"unreadable": "not sampled"})),
        "contention": lambda: _contention(verdict_path, now),
    }
    snapshot = {}
    for name, read in readers.items():
        try:
            snapshot[name] = read()
        except Exception as error:  # noqa: BLE001 - every source failure is a recorded field
            snapshot[name] = _unreadable(error)
    snapshot["psi"] = {}
    for kind in PSI_KINDS:
        try:
            snapshot["psi"][kind] = _psi(kind)
        except Exception as error:  # noqa: BLE001 - recorded, never raised
            snapshot["psi"][kind] = _unreadable(error)
    return snapshot


class ResumeUnits:
    """The slowest resume units of one pass, bounded so a large batch stays cheap."""

    def __init__(self, limit=RESUME_TOP):
        self.limit, self.heap, self.count = limit, [], 0

    def add(self, ms, unit):
        self.count += 1
        item = (ms, self.count, unit)
        if len(self.heap) < self.limit:
            heapq.heappush(self.heap, item)
        elif ms > self.heap[0][0]:
            heapq.heapreplace(self.heap, item)

    def top(self):
        return [
            {**unit, "ms": round(ms, 3)}
            for ms, _order, unit in sorted(self.heap, reverse=True)
        ]


def append_row(path, row, max_bytes):
    """Append one JSON line, rotating to ``<name>.1`` once the file passes ``max_bytes``."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(FileNotFoundError):
        if path.stat().st_size > max_bytes:
            os.replace(path, path.with_name(path.name + ".1"))
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")


def read_rows(path):
    """Rows of the rotated generation, then the current file; malformed lines are skipped."""
    path = Path(path)
    rows = []
    for candidate in (path.with_name(path.name + ".1"), path):
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def recent_starts(rows, now, window_s):
    return [
        row
        for row in rows
        if row.get("kind") == "process-start"
        and isinstance(row.get("at"), (int, float))
        and now - row["at"] <= window_s
    ]


def process_start_ticks(pid):
    """Field 22 of ``/proc/<pid>/stat``, so a recycled pid is not read as the same process."""
    text = (PROC_ROOT / str(pid) / "stat").read_text()
    return int(text.rsplit(")", 1)[1].split()[19])


class StuckWatch:
    """One deadline per pass: pages when the pass shows no progress for the stuck limit.

    Progress is any phase change or finished resume unit. The deadline re-arms only for the
    time left since the last progress, so a pass that keeps moving never pages, however long.
    """

    def __init__(self, limit_s, on_stuck):
        self.limit, self.on_stuck = limit_s, on_stuck
        self.progress_at = time.monotonic()
        self.paged = None
        self._lock = threading.Lock()
        self._closed = False
        self._timer = None
        self._arm(limit_s)

    def _arm(self, delay):
        self._timer = threading.Timer(delay, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def progress(self):
        self.progress_at = time.monotonic()

    def _fire(self):
        with self._lock:
            if self._closed or self.paged is not None:
                return
            idle = time.monotonic() - self.progress_at
            if idle < self.limit:
                self._arm(self.limit - idle)
                return
            self.paged = idle
        self.on_stuck(idle)

    def close(self):
        with self._lock:
            self._closed = True
            if self._timer is not None:
                self._timer.cancel()
        return self.paged


def _percentiles(values):
    from ai_chassis.agent_mail_notifier import _nearest_rank

    if not values:
        return {}
    summary = {f"p{percent}": _nearest_rank(values, percent) for percent in PERCENTILES}
    summary["max"] = max(values)
    summary["mean"] = round(sum(values) / len(values), 3)
    return summary


def _bucket(logs, since):
    hosts = {}
    for log in logs:
        rows = read_rows(log)
        pages = read_rows(Path(log).with_name(RESTORE_PAGES))
        for row in [*rows, *pages]:
            at = row.get("at")
            if since is not None and isinstance(at, (int, float)) and at < since:
                continue
            host = hosts.setdefault(
                str(row.get("host") or Path(log).parent.name),
                {"rows": [], "pages": [], "starts": 0},
            )
            if "reason" in row:
                host["pages"].append(row)
            elif row.get("kind") == "process-start":
                host["starts"] += 1
            elif isinstance(row.get("elapsed_ms"), (int, float)):
                host["rows"].append(row)
    return hosts


def review(logs, *, since=None, limit=20):
    """Per-host pass percentiles, per-phase percentiles, slow passes and restore pages."""
    report = {}
    for name, host in sorted(_bucket(logs, since).items()):
        passes = [row for row in host["rows"] if row.get("kind") == "pass"]
        phases = {}
        for row in passes:
            for phase, ms in (row.get("phases_ms") or {}).items():
                if isinstance(ms, (int, float)) and math.isfinite(ms):
                    phases.setdefault(phase, []).append(ms)
        slow = sorted(
            (row for row in host["rows"] if row.get("slow")),
            key=lambda row: row["elapsed_ms"],
            reverse=True,
        )
        report[name] = {
            "passes": len(passes),
            "startup_passes": len(host["rows"]) - len(passes),
            "process_starts": host["starts"],
            "elapsed_ms": _percentiles([row["elapsed_ms"] for row in passes]),
            "phases_ms": {
                phase: _percentiles(values)
                for phase, values in sorted(
                    phases.items(), key=lambda item: -max(item[1])
                )
            },
            "slow_passes": [
                {
                    key: row.get(key)
                    for key in (
                        "at",
                        "kind",
                        "elapsed_ms",
                        "phases_ms",
                        "resume_top",
                        "obligations",
                        "snapshot",
                        "stuck",
                    )
                    if key in row
                }
                for row in slow[:limit]
            ],
            "slow_pass_count": len(slow),
            "restore_pages": host["pages"],
        }
    return {"hosts": report}


def _render(report):
    lines = []
    for name, host in report["hosts"].items():
        elapsed = host["elapsed_ms"]
        lines.append(
            f"{name}: passes={host['passes']} starts={host['process_starts']} "
            + " ".join(f"{key}={value}" for key, value in elapsed.items())
            + f" slow={host['slow_pass_count']}"
        )
        for phase, summary in list(host["phases_ms"].items())[:5]:
            lines.append(
                f"  phase {phase}: p95={summary.get('p95')} max={summary.get('max')}"
            )
        for row in host["slow_passes"]:
            phases = row.get("phases_ms") or {}
            top = max(phases, key=phases.get) if phases else "-"
            contention = (row.get("snapshot") or {}).get("contention", {})
            lines.append(
                f"  slow at={row.get('at')} elapsed_ms={row.get('elapsed_ms')} "
                f"top_phase={top} contention={contention.get('verdict', contention.get('unreadable'))}"
            )
        for page in host["restore_pages"]:
            lines.append(f"  restore-page {json.dumps(page, sort_keys=True)}")
    return "\n".join(lines)


def _alive_in_proc(pid, recorded):
    try:
        fields = (PROC_ROOT / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    except OSError:
        return False
    # Field 3 is the state; field 22 the start time, which tells a recycled pid apart.
    return fields[0] != "Z" and recorded in (None, int(fields[19]))


def _alive(row):
    pid = row.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    if PROC_ROOT.is_dir():
        return _alive_in_proc(pid, row.get("proc_start"))
    # Without procfs only kill(pid, 0) is left. "No such process" proves the dispatcher
    # dead; a pid that answers may be a recycled one, so that answer is "cannot say".
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return None


def health(dispatch_dir, *, now=None):
    """``alive``, ``unknown`` (no recorded start, or no procfs to check it) or ``dead``; a dead process is paged."""
    dispatch_dir = Path(dispatch_dir)
    starts = [
        row
        for row in read_rows(dispatch_dir / PASS_LOG)
        if row.get("kind") == "process-start"
    ]
    if not starts:
        return "unknown", None
    last = starts[-1]
    alive = _alive(last)
    if alive is None:
        return "unknown", last
    if alive:
        return "alive", last
    page = {
        "schema": SCHEMA,
        "reason": "dead",
        "host": last.get("host"),
        "pid": last.get("pid"),
        "started_at": last.get("at"),
        "at": time.time() if now is None else now,
    }
    append_row(dispatch_dir / RESTORE_PAGES, page, max_bytes=1 << 20)
    return "dead", page


def _default_dispatch_dir():
    from ai_chassis.agent_mail_notifier import state_root

    return state_root() / "dispatch"


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m ai_chassis.wake.pass_telemetry", description=__doc__
    )
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("review", help="pass percentiles and slow passes")
    report.add_argument(
        "--log",
        action="append",
        type=Path,
        help="a passes.jsonl (repeat once per host); default: this host's",
    )
    report.add_argument("--since-hours", type=float)
    report.add_argument("--limit", type=int, default=20)
    report.add_argument("--json", action="store_true")
    check = commands.add_parser(
        "health", help="page a restore if the dispatcher is dead"
    )
    check.add_argument("--dispatch-dir", type=Path)
    args = parser.parse_args(argv)
    if args.command == "health":
        verdict, row = health(args.dispatch_dir or _default_dispatch_dir())
        print(
            f"dispatcher {verdict}"
            + (f" {json.dumps(row, sort_keys=True)}" if row else "")
        )
        return 1 if verdict == "dead" else 0
    logs = args.log or [_default_dispatch_dir() / PASS_LOG]
    since = None if args.since_hours is None else time.time() - args.since_hours * 3600
    result = review(logs, since=since, limit=args.limit)
    print(
        json.dumps(result, indent=2, sort_keys=True) if args.json else _render(result)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
