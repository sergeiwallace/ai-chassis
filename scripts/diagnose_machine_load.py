#!/usr/bin/env python3
"""Measure what is actually consuming this machine, and attribute it to a parent.

Written while diagnosing the glacial enterprise-cld-windows profile. The point is ATTRIBUTION, not a
process count: a count alone cannot distinguish "one runaway spawner" from
"a normally busy desktop", and the previous round of work on this machine
already showed that reaping processes improved hook wall-clock while leaving
bare-exec cost unchanged. So a fix aimed at the count is only justified if the
count is what dominates -- this script is what decides that.

Reports, in order:

1. Totals: process count, CPU, memory (used/available/committed), swap.
2. The per-process top-N by RSS and by CPU time, so a single hog is visible.
3. **Parent attribution**: every process grouped by its root ancestor, with
   subtree counts and summed RSS. This is the measurement that names a
   spawner. A flat count says "908 processes"; this says which tree they are in.
4. Repeated-name clusters: identical images with many instances, with their
   oldest/newest start times, which distinguishes a stable pool (a browser's
   renderers) from an accumulating leak (same name, ages spread over hours).
5. Orphans -- processes whose parent is gone. On Windows the parent pid is
   NOT cleared when the parent dies, and pids are recycled, so a "parent" that
   started AFTER its child is a recycled number, not the real parent. Both
   cases are reported as orphans rather than silently mis-attributed.

Deliberately read-only. It kills nothing and changes nothing: deciding what may
be reaped is a separate, human-reviewed step.

Usage:
    uv run --with psutil python scripts/diagnose_machine_load.py [--top N] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

try:
    import psutil
except ImportError:  # pragma: no cover - dependency is supplied by `uv run --with`
    sys.exit(
        "psutil is required: uv run --with psutil python scripts/diagnose_machine_load.py"
    )


@dataclass
class Proc:
    """A snapshot of one process. Fields default to safe values when denied."""

    pid: int
    ppid: int
    name: str
    rss: int
    cpu_time: float
    create_time: float
    username: str
    cmdline: str
    accessible: bool = True


@dataclass
class Subtree:
    """An aggregate over one root ancestor's descendants."""

    root_pid: int
    root_name: str
    count: int = 0
    rss: int = 0
    cpu_time: float = 0.0
    names: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def _mb(value: float) -> float:
    return round(value / (1024 * 1024), 1)


def snapshot() -> list[Proc]:
    """Collect every visible process.

    A denied process is recorded with zeroed metrics and ``accessible=False``
    rather than dropped: on an enterprise Windows box a good number of system
    processes are unreadable, and silently omitting them would understate the
    total and hide where the load sits. An undercount that looks like a clean
    read is exactly the failure this reports instead.
    """
    procs: list[Proc] = []
    # `name` is read per process: psutil's `name()` calls `cmdline()` for a 15+ character
    # name, and psutil 7.2.2's Darwin `proc_cmdline` raises a bare SystemError for a
    # process mid-exit (ISSUE-REF), which inside the snapshot would abort the iterator.
    fields = [
        "pid",
        "ppid",
        "memory_info",
        "cpu_times",
        "create_time",
        "username",
    ]
    for handle in psutil.process_iter(fields, ad_value=None):
        info = handle.info
        pid = info.get("pid")
        if pid is None:
            continue
        mem = info.get("memory_info")
        cpu = info.get("cpu_times")
        accessible = mem is not None
        try:
            name = handle.name()
        except (psutil.Error, OSError, SystemError):
            name = None
        try:
            cmdline = " ".join(handle.cmdline() or [])
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, SystemError):
            cmdline = ""
            accessible = False
        procs.append(
            Proc(
                pid=pid,
                ppid=info.get("ppid") or 0,
                name=name or "<unknown>",
                rss=getattr(mem, "rss", 0) or 0,
                cpu_time=(cpu.user + cpu.system) if cpu else 0.0,
                create_time=info.get("create_time") or 0.0,
                username=info.get("username") or "",
                cmdline=cmdline[:200],
                accessible=accessible,
            )
        )
    return procs


def find_root(
    pid: int, by_pid: dict[int, Proc], *, max_depth: int = 64
) -> tuple[int, bool]:
    """Walk to the topmost ancestor. Returns ``(root_pid, is_orphan)``.

    ``is_orphan`` is True when the chain breaks before reaching a real root --
    either the parent pid is absent, or the claimed parent started *after* this
    process, which on Windows means the pid was recycled and the number no
    longer identifies the real parent. Treating a recycled pid as a genuine
    parent would attribute a whole subtree to an unrelated process, which is
    the mis-attribution this check exists to prevent (the same pid-reuse trap
    that made ``os.kill(pid, 0)`` unusable here).

    ``max_depth`` bounds a cycle: a corrupted ppid chain would otherwise loop
    forever.
    """
    current = pid
    orphan = False
    for _ in range(max_depth):
        proc = by_pid.get(current)
        if proc is None:
            orphan = True
            break
        parent = by_pid.get(proc.ppid)
        if proc.ppid in (0, current) or parent is None:
            if proc.ppid not in (0, current):
                orphan = True
            break
        if parent.create_time > proc.create_time:
            orphan = True  # recycled pid, not the real parent
            break
        current = proc.ppid
    return current, orphan


def build_report(procs: list[Proc], top: int) -> dict:
    by_pid = {p.pid: p for p in procs}
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()

    subtrees: dict[int, Subtree] = {}
    orphans: list[Proc] = []
    for proc in procs:
        root_pid, orphan = find_root(proc.pid, by_pid)
        if orphan:
            orphans.append(proc)
        root = by_pid.get(root_pid)
        entry = subtrees.setdefault(
            root_pid,
            Subtree(
                root_pid=root_pid, root_name=root.name if root else f"<gone:{root_pid}>"
            ),
        )
        entry.count += 1
        entry.rss += proc.rss
        entry.cpu_time += proc.cpu_time
        entry.names[proc.name] += 1

    clusters: dict[str, list[Proc]] = defaultdict(list)
    for proc in procs:
        clusters[proc.name].append(proc)

    now = time.time()
    cluster_rows = []
    for name, members in clusters.items():
        if len(members) < 3:
            continue
        starts = [m.create_time for m in members if m.create_time]
        cluster_rows.append(
            {
                "name": name,
                "count": len(members),
                "rss_mb": _mb(sum(m.rss for m in members)),
                "cpu_seconds": round(sum(m.cpu_time for m in members), 1),
                "oldest_age_min": round((now - min(starts)) / 60, 1)
                if starts
                else None,
                "newest_age_min": round((now - max(starts)) / 60, 1)
                if starts
                else None,
            }
        )
    cluster_rows.sort(key=lambda row: row["count"], reverse=True)

    subtree_rows = [
        {
            "root_pid": s.root_pid,
            "root_name": s.root_name,
            "subtree_count": s.count,
            "rss_mb": _mb(s.rss),
            "cpu_seconds": round(s.cpu_time, 1),
            "top_names": sorted(s.names.items(), key=lambda kv: kv[1], reverse=True)[
                :5
            ],
        }
        for s in subtrees.values()
    ]
    subtree_rows.sort(key=lambda row: row["subtree_count"], reverse=True)

    return {
        "totals": {
            "process_count": len(procs),
            "inaccessible_count": sum(1 for p in procs if not p.accessible),
            "cpu_count": psutil.cpu_count(),
            "cpu_percent": psutil.cpu_percent(interval=1.0),
            "load_avg": list(psutil.getloadavg())
            if hasattr(psutil, "getloadavg")
            else None,
            "mem_total_mb": _mb(vm.total),
            "mem_available_mb": _mb(vm.available),
            "mem_percent": vm.percent,
            "swap_used_mb": _mb(swap.used),
            "swap_percent": swap.percent,
            "sum_rss_mb": _mb(sum(p.rss for p in procs)),
        },
        "top_by_rss": [
            {"pid": p.pid, "name": p.name, "rss_mb": _mb(p.rss), "cmd": p.cmdline}
            for p in sorted(procs, key=lambda p: p.rss, reverse=True)[:top]
        ],
        "top_by_cpu_time": [
            {
                "pid": p.pid,
                "name": p.name,
                "cpu_seconds": round(p.cpu_time, 1),
                "cmd": p.cmdline,
            }
            for p in sorted(procs, key=lambda p: p.cpu_time, reverse=True)[:top]
        ],
        "subtrees": subtree_rows[:top],
        "name_clusters": cluster_rows[:top],
        "orphan_count": len(orphans),
        "orphans": [
            {"pid": p.pid, "ppid": p.ppid, "name": p.name, "rss_mb": _mb(p.rss)}
            for p in sorted(orphans, key=lambda p: p.rss, reverse=True)[:top]
        ],
    }


def render(report: dict) -> str:
    out: list[str] = []
    totals = report["totals"]
    out.append("== TOTALS ==")
    for key, value in totals.items():
        out.append(f"  {key}: {value}")

    out.append("\n== SUBTREES (by descendant count, root ancestor) ==")
    for row in report["subtrees"]:
        names = ", ".join(f"{n}x{c}" for n, c in row["top_names"])
        out.append(
            f"  {row['subtree_count']:>4} procs  {row['rss_mb']:>9} MB  "
            f"{row['cpu_seconds']:>9}s cpu  pid {row['root_pid']} {row['root_name']}  [{names}]"
        )

    out.append("\n== NAME CLUSTERS (>=3 instances) ==")
    for row in report["name_clusters"]:
        out.append(
            f"  {row['count']:>4}x {row['name']:<32} {row['rss_mb']:>9} MB  "
            f"{row['cpu_seconds']:>9}s cpu  age {row['newest_age_min']}-{row['oldest_age_min']} min"
        )

    out.append("\n== TOP BY RSS ==")
    for row in report["top_by_rss"]:
        out.append(f"  {row['rss_mb']:>9} MB  pid {row['pid']:<7} {row['name']}")

    out.append("\n== TOP BY CPU TIME ==")
    for row in report["top_by_cpu_time"]:
        out.append(f"  {row['cpu_seconds']:>9}s  pid {row['pid']:<7} {row['name']}")

    out.append(f"\n== ORPHANS ({report['orphan_count']}) ==")
    for row in report["orphans"]:
        out.append(
            f"  pid {row['pid']:<7} ppid {row['ppid']:<7} {row['rss_mb']:>9} MB  {row['name']}"
        )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Attribute machine load to a parent process tree."
    )
    parser.add_argument(
        "--top", type=int, default=20, help="rows per section (default 20)"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    report = build_report(snapshot(), args.top)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"machine load snapshot -- pid {os.getpid()}")
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
