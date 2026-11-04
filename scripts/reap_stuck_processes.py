#!/usr/bin/env python3
"""Reap processes that were started and then abandoned before they ever ran.

ISSUE-REF. Replaces ``scripts/windows/reap-orphan-shells.ps1``: same job, but
OS-agnostic (ISSUE-REF), covering the clusters the PowerShell version could not
touch, and selecting on a *measured* signature instead of a name allowlist.

WHAT THIS REAPS, and why the signature is trustworthy
-----------------------------------------------------
This utility selects suspended processes with no accumulated CPU time.
Use the dry run to confirm behavior on the target machine.

SAFETY -- every condition must hold before a kill, and each can veto
--------------------------------------------------------------------
    1. Zero accumulated CPU time (< --max-cpu-seconds). The load-bearing one.
    2. Older than --min-age-minutes (default 30). Protects a process that was
       legitimately created suspended microseconds ago and is about to resume.
    3. Not a parent of any live process. Children mean real work in flight.
    4. Suspended/stopped status, or platform-equivalent (zombie on POSIX).
    5. Not this process, not an ancestor of this process.
    6. Not in --protect-names (a small deny list for anything whose death would
       take a session or the editor with it).

``--dry-run`` is the default: it reports and kills nothing. Killing requires an
explicit ``--kill``, because the failure mode of a too-eager reaper is losing
someone's work, and the failure mode of a too-shy one is 860 MB.

Usage:
    uv run --with psutil python scripts/reap_stuck_processes.py            # report
    uv run --with psutil python scripts/reap_stuck_processes.py --kill     # act
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter

try:
    import psutil
except ImportError:  # pragma: no cover - dependency is supplied by `uv run --with`
    sys.exit(
        "psutil is required: uv run --with psutil python scripts/reap_stuck_processes.py --kill"
    )

# Names never killed regardless of signature. These are the processes whose death
# would cost real work rather than reclaim waste, so they are excluded before any
# other test runs. Kept deliberately short: the CPU-time condition is what makes
# the rest safe, and a long list here would only hide a broken signature.
PROTECT_NAMES = frozenset(
    {
        "claude.exe",
        "claude",
        "code.exe",
        "code",
        "node.exe",
        "node",
        "ssh.exe",
        "sshd.exe",
        "codex.exe",
        "systemd",
        "init",
        "explorer.exe",
        "winlogon.exe",
        "csrss.exe",
        "wininit.exe",
        "services.exe",
        "lsass.exe",
        "system",
        "registry",
        "memcompression",
    }
)

# psutil status values that mean "not currently executing". zombie is included so
# this is useful on POSIX (where an unreaped child is a zombie, not suspended)
# rather than being a Windows-only tool wearing an OS-agnostic name.
IDLE_STATUSES = frozenset({psutil.STATUS_STOPPED, psutil.STATUS_ZOMBIE})


def ancestors_of(pid: int) -> set[int]:
    """Every ancestor pid of ``pid``, so the reaper cannot kill its own parent.

    Walks with a depth bound: a recycled-pid cycle in the ppid chain would
    otherwise loop forever, and on Windows the ppid field is not cleared when a
    parent dies, so cycles are genuinely reachable.
    """
    out: set[int] = set()
    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return out
    for _ in range(64):
        try:
            parent = proc.parent()
        except psutil.Error:
            break
        if parent is None or parent.pid in out:
            break
        out.add(parent.pid)
        proc = parent
    return out


def _process_name(handle: psutil.Process) -> str:
    """The process name, or ``""`` when it cannot be read -- which the caller vetoes."""
    try:
        return handle.name() or ""
    except (psutil.Error, OSError, SystemError):
        return ""


def collect(
    *,
    min_age_minutes: float,
    max_cpu_seconds: float,
    protect_names: frozenset[str],
) -> tuple[list[dict], Counter]:
    """Return (candidates, veto_reasons). Read-only."""
    now = time.time()
    self_pid = os.getpid()
    protected_pids = ancestors_of(self_pid) | {self_pid}

    # `name` is read per process, not in the snapshot: psutil's `name()` calls `cmdline()`
    # for a 15+ character name, and psutil 7.2.2's Darwin `proc_cmdline` raises a bare
    # SystemError for a process mid-exit (ISSUE-REF), which would escape the iterator.
    procs = list(
        psutil.process_iter(
            ["pid", "ppid", "status", "create_time", "cpu_times", "memory_info"]
        )
    )
    live_parents = {p.info.get("ppid") for p in procs if p.info.get("ppid")}

    candidates: list[dict] = []
    vetoes: Counter = Counter()

    for handle in procs:
        info = handle.info
        pid = info.get("pid")
        if pid is None or pid in protected_pids:
            continue
        raw_name = _process_name(handle)
        name = raw_name.lower()

        # An unreadable name means the protect-list check cannot be evaluated, and
        # an unevaluated safety check must veto rather than pass. Measured: pid 92
        # on this box is a nameless kernel-adjacent process alive since boot with
        # zero CPU -- it matches the signature perfectly and must never be killed.
        if not name:
            vetoes["name unreadable"] += 1
            continue
        if name in protect_names:
            vetoes["protected name"] += 1
            continue
        if info.get("status") not in IDLE_STATUSES:
            continue

        cpu = info.get("cpu_times")
        # An unreadable cpu_times means we cannot evaluate the load-bearing
        # condition. Skip rather than assume zero -- never guess in a way that
        # justifies a kill.
        if cpu is None:
            vetoes["cpu time unreadable"] += 1
            continue
        cpu_total = cpu.user + cpu.system
        if cpu_total >= max_cpu_seconds:
            vetoes["has accumulated CPU"] += 1
            continue

        created = info.get("create_time")
        if not created:
            vetoes["age unknown"] += 1
            continue
        age_min = (now - created) / 60
        if age_min < min_age_minutes:
            vetoes["too young"] += 1
            continue

        if pid in live_parents:
            vetoes["is a parent"] += 1
            continue

        try:
            cmdline = " ".join(handle.cmdline() or [])
        except (psutil.Error, OSError, SystemError):
            cmdline = ""

        candidates.append(
            {
                "pid": pid,
                "ppid": info.get("ppid"),
                "name": raw_name,
                "age_min": round(age_min, 1),
                "cpu": cpu_total,
                "rss": getattr(info.get("memory_info"), "rss", 0) or 0,
                "cmd": cmdline[:140],
            }
        )

    return candidates, vetoes


def reap(candidates: list[dict], *, kill: bool) -> tuple[int, int]:
    """Kill each candidate. Returns (killed, failed)."""
    killed = failed = 0
    for cand in candidates:
        if not kill:
            continue
        try:
            proc = psutil.Process(cand["pid"])
            # Re-verify the load-bearing condition against the LIVE process rather
            # than the snapshot: between collection and now the process could have
            # been resumed and started doing real work, and killing it then would
            # destroy exactly the work this condition exists to protect.
            cpu = proc.cpu_times()
            if (cpu.user + cpu.system) >= 0.05:
                failed += 1
                continue
            proc.kill()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error, OSError):
            # Already gone is the expected benign case; an access denial on a
            # protected process is not worth failing the run over.
            failed += 1
    return killed, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reap never-scheduled abandoned processes."
    )
    parser.add_argument(
        "--kill", action="store_true", help="actually kill (default: report only)"
    )
    parser.add_argument("--min-age-minutes", type=float, default=30.0)
    parser.add_argument(
        "--max-cpu-seconds",
        type=float,
        default=0.05,
        help="a candidate must have accumulated LESS than this much CPU (default 0.05)",
    )
    parser.add_argument(
        "--top", type=int, default=15, help="rows to show in the report"
    )
    args = parser.parse_args(argv)

    candidates, vetoes = collect(
        min_age_minutes=args.min_age_minutes,
        max_cpu_seconds=args.max_cpu_seconds,
        protect_names=PROTECT_NAMES,
    )

    total_rss = sum(c["rss"] for c in candidates)
    by_name = Counter(c["name"] for c in candidates)
    print(
        f"candidates: {len(candidates)}  holding {total_rss / 1048576:.0f} MB  "
        f"(min age {args.min_age_minutes}m, cpu < {args.max_cpu_seconds}s)"
    )
    if vetoes:
        print(
            "vetoed: "
            + ", ".join(f"{reason}={count}" for reason, count in vetoes.most_common())
        )
    for name, count in by_name.most_common(args.top):
        print(f"  {count:>5}x {name}")

    if candidates:
        print("\noldest candidates:")
        for cand in sorted(candidates, key=lambda c: -c["age_min"])[: args.top]:
            print(
                f"  age {cand['age_min']:>8.1f}m cpu {cand['cpu']:.2f}s pid {cand['pid']:<7} {cand['name']}"
            )
            if cand["cmd"]:
                print(f"      {cand['cmd']}")

    if not args.kill:
        print(
            f"\nDRY RUN -- nothing killed. Re-run with --kill to reap {len(candidates)}."
        )
        return 0

    killed, failed = reap(candidates, kill=True)
    print(f"\nreaped {killed}, could not kill {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
