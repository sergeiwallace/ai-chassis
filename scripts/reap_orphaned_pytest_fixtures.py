#!/usr/bin/env python3
"""Reap abandoned fake app-server fixtures only after their pytest run is dead.

ISSUE-REF. This is intentionally separate from ``reap_stuck_processes.py``.
That janitor identifies processes that were never scheduled (stopped/zombie with
near-zero lifetime CPU); these fixtures are healthy, running Bash loops. Their
distinct, deliberately narrow signature is:

* argv names a binary below ``pytest-of-USER/pytest-N/.../fakebin/`` and contains
  both ``app-server`` and ``--stdio``;
* it has been adopted by init or a user-session subreaper (see ``_reaper_pids``)
  and remains its process-group leader; and
* no *other* live process refers to that exact pytest numbered root in its own
  argv or cwd.

The final condition proves the owning pytest controller/worker is gone rather
than merely assuming that a long-lived fixture is unwanted. The default is a
read-only report. ``--kill`` is explicit and rechecks every load-bearing
condition immediately before signalling the fixture's dedicated process group.

Usage:
    uv run python scripts/reap_orphaned_pytest_fixtures.py
    uv run python scripts/reap_orphaned_pytest_fixtures.py --kill
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import sys
import time
from collections import Counter
from collections.abc import Iterable
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover - dependency is supplied by the project
    sys.exit(
        "psutil is required: uv run python scripts/reap_orphaned_pytest_fixtures.py"
    )


# The numbered root is intentionally captured without ``popen-gwN``. A live
# xdist controller normally references pytest-N while its child fixture lives
# below pytest-N/popen-gwN, so either one must protect the fixture.
PYTEST_ROOT_RE = re.compile(r"(?P<root>/pytest-of-[^/\s]+/pytest-\d+)(?=/|\s|$)")

# These names are never expected for this fixture signature. ``codex`` is
# deliberately absent: a fixture-created fake binary may itself have that name.
PROTECT_NAMES = frozenset(
    {
        "init",
        "launchd",
        "systemd",
        "kernel_task",
        "windowserver",
        "loginwindow",
        "claude",
        "code",
    }
)


def ancestors_of(pid: int) -> set[int]:
    """Return ancestor PIDs so the janitor can never kill its own lineage."""
    ancestors: set[int] = set()
    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return ancestors
    for _ in range(64):
        try:
            parent = proc.parent()
        except (psutil.Error, OSError):
            break
        if parent is None or parent.pid in ancestors:
            break
        ancestors.add(parent.pid)
        proc = parent
    return ancestors


def _cmdline(info: dict[str, Any]) -> str | None:
    cmdline = info.get("cmdline")
    if not cmdline:
        return None
    return " ".join(cmdline)


def pytest_root_for(info: dict[str, Any]) -> str | None:
    """Return the pytest root only for the exact fake app-server signature."""
    command = _cmdline(info)
    if command is None:
        return None
    root_match = PYTEST_ROOT_RE.search(command)
    if root_match is None:
        return None
    args = info.get("cmdline") or []
    if "app-server" not in args or "--stdio" not in args:
        return None
    tail = command[root_match.end() :]
    if "/fakebin/" not in tail:
        return None
    return root_match.group("root")


def _references_root(info: dict[str, Any], root: str) -> bool:
    """Whether a process's *own* argv or cwd proves its pytest root is alive."""
    command = _cmdline(info)
    return root in (command or "") or root in (info.get("cwd") or "")


def _is_process_group_leader(pid: int) -> bool:
    """A fixture created by ``start_new_session=True`` leads its own group."""
    if os.name != "posix":
        return False
    try:
        return os.getpgid(pid) == pid
    except OSError:
        return False


def _reaper_pids() -> set[int]:
    """PIDs that adopt orphans on this machine: init, plus any user-session subreaper.

    ``ppid == 1`` alone is a Linux-without-a-subreaper assumption, and this script's
    entire safety model rests on it -- so on a machine where it is false, the script
    does not misfire, it stops working entirely: every candidate is vetoed as "still
    has a parent" and nothing is ever reaped.

    That machine is any systemd **user session**. ``systemd --user`` calls
    ``prctl(PR_SET_CHILD_SUBREAPER)``, so it adopts the orphans of its whole
    descendant tree instead of PID 1. Measured on Fedora 44 Workstation
    (linux-workstation bootstrap, 2026-08-15): a fixture whose owner was killed
    reparented to ``/usr/lib/systemd/systemd --user``, and the janitor reported
    ``vetoed: still has a parent=1`` about a genuinely orphaned process. macOS and
    bare Linux containers are unaffected, which is why this survived until the fleet
    gained a Linux desktop.

    Deliberately NOT relaxed to "any parent that no longer owns the fixture": this
    function gates a KILL. Widening it would let a live-owned fixture be reaped, which
    is far worse than failing to reap a dead one. Only init-equivalents are accepted --
    processes whose job is precisely to hold orphans.
    """
    pids = {1}
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            info = proc.info
            if (info.get("name") or "").lower() != "systemd":
                continue
            if "--user" in (info.get("cmdline") or []):
                pids.add(info["pid"])
    except (psutil.Error, SystemError):  # pragma: no cover - iteration is best-effort
        # A missed reaper only means fewer processes read as orphaned: fewer kills.
        pass
    return pids


def _is_orphaned(info: dict, reapers: set[int]) -> bool:
    """True when this process has been adopted by init or a user-session subreaper."""
    return info.get("ppid") in reapers


def _live_owner_pids(
    procs: Iterable[psutil.Process], *, root: str, excluded_pid: int
) -> set[int]:
    """All other live processes whose argv/cwd still names ``root``."""
    owners: set[int] = set()
    for handle in procs:
        info = handle.info
        pid = info.get("pid")
        if pid is not None and pid != excluded_pid and _references_root(info, root):
            owners.add(pid)
    return owners


def collect(
    *, min_age_minutes: float, only_pid: int | None = None
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Return kill-safe candidates and reportable vetoes from a read-only snapshot.

    ``only_pid`` restricts consideration to one exact PID and is the mechanism this
    script's own regression test uses to scope a live ``--kill`` invocation to the
    single fixture process it created -- without it, a real orphan elsewhere on the
    machine that happens to match the signature would be swept up as a side effect of
    running the test suite, which is exactly the kind of live, out-of-scope action a
    test must never take.
    """
    now = time.time()
    self_pid = os.getpid()
    protected_pids = ancestors_of(self_pid) | {self_pid}
    # Resolved once per snapshot rather than per candidate: it is a full process
    # scan, and the set cannot change meaningfully mid-collection.
    reapers = _reaper_pids()
    try:
        procs = list(
            psutil.process_iter(
                ["pid", "ppid", "name", "cmdline", "cwd", "create_time"]
            )
        )
    except (psutil.Error, OSError, SystemError):
        # An incomplete process table cannot prove that the pytest owner is gone. A
        # SystemError is psutil 7.2.2's Darwin `proc_cmdline` on a process mid-exit
        # (ISSUE-REF); skipping just that process would hide a possible owner, so the
        # whole pass refuses instead.
        return [], Counter({"process list unreadable": 1})
    candidates: list[dict[str, Any]] = []
    vetoes: Counter[str] = Counter()

    for handle in procs:
        info = handle.info
        pid = info.get("pid")
        if only_pid is not None and pid != only_pid:
            continue
        root = pytest_root_for(info)
        if pid is None or root is None:
            continue
        if pid in protected_pids:
            vetoes["self or ancestor"] += 1
            continue
        name = (info.get("name") or "").lower()
        if not name:
            vetoes["name unreadable"] += 1
            continue
        if name in PROTECT_NAMES:
            vetoes["protected name"] += 1
            continue
        if not _is_orphaned(info, reapers):
            vetoes["still has a parent"] += 1
            continue
        if not _is_process_group_leader(pid):
            vetoes["not a process-group leader"] += 1
            continue
        created = info.get("create_time")
        if not created:
            vetoes["age unknown"] += 1
            continue
        age_min = (now - created) / 60
        if age_min < min_age_minutes:
            vetoes["too young"] += 1
            continue
        owner_pids = _live_owner_pids(procs, root=root, excluded_pid=pid)
        if owner_pids:
            vetoes["live pytest root reference"] += 1
            continue
        candidates.append(
            {
                "pid": pid,
                "name": info["name"],
                "root": root,
                "created": created,
                "age_min": round(age_min, 1),
                "cmd": _cmdline(info) or "",
            }
        )
    return candidates, vetoes


def _still_safe_to_kill(candidate: dict[str, Any], *, min_age_minutes: float) -> bool:
    """Revalidate a snapshot candidate against the live process table."""
    pid = candidate["pid"]
    try:
        proc = psutil.Process(pid)
        if abs(proc.create_time() - candidate["created"]) > 0.01:
            return False  # PID was recycled after collection.
        info = proc.as_dict(["pid", "ppid", "name", "cmdline", "cwd", "create_time"])
    except (psutil.Error, OSError, SystemError):
        return False
    if pytest_root_for(info) != candidate["root"]:
        return False
    if not _is_orphaned(info, _reaper_pids()) or not _is_process_group_leader(pid):
        return False
    if (time.time() - info["create_time"]) / 60 < min_age_minutes:
        return False
    if (info.get("name") or "").lower() in PROTECT_NAMES:
        return False
    protected_pids = ancestors_of(os.getpid()) | {os.getpid()}
    if pid in protected_pids:
        return False
    try:
        procs = list(psutil.process_iter(["pid", "cmdline", "cwd"]))
    except (psutil.Error, OSError, SystemError):
        return False
    return not _live_owner_pids(procs, root=candidate["root"], excluded_pid=pid)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap(
    candidates: list[dict[str, Any]], *, min_age_minutes: float
) -> tuple[int, int]:
    """TERM then KILL each still-safe fixture group; return (reaped, skipped)."""
    reaped = skipped = 0
    for candidate in candidates:
        pid = candidate["pid"]
        if not _still_safe_to_kill(candidate, min_age_minutes=min_age_minutes):
            skipped += 1
            continue
        try:
            os.killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while _group_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            if _group_alive(pid):
                os.killpg(pid, signal.SIGKILL)
            reaped += 1
        except (OSError, psutil.Error):
            skipped += 1
    return reaped, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reap orphaned fake app-server pytest fixtures after owner death."
    )
    parser.add_argument(
        "--kill", action="store_true", help="actually reap (default: report only)"
    )
    parser.add_argument("--min-age-minutes", type=float, default=30.0)
    parser.add_argument(
        "--top", type=int, default=15, help="rows to show in the report"
    )
    parser.add_argument(
        "--pid",
        type=int,
        default=None,
        help="restrict to this exact PID only (precise targeting; also used by this "
        "script's own regression test to scope a --kill run to its own fixture)",
    )
    args = parser.parse_args(argv)

    candidates, vetoes = collect(
        min_age_minutes=args.min_age_minutes, only_pid=args.pid
    )
    print(
        f"candidates: {len(candidates)}  "
        f"(min age {args.min_age_minutes}m; dead pytest root required)"
    )
    if vetoes:
        print(
            "vetoed: "
            + ", ".join(f"{reason}={count}" for reason, count in vetoes.most_common())
        )
    if candidates:
        print("\noldest candidates:")
        for candidate in sorted(candidates, key=lambda row: -row["age_min"])[
            : args.top
        ]:
            print(
                f"  age {candidate['age_min']:>8.1f}m pid {candidate['pid']:<7} "
                f"{candidate['name']} root {candidate['root']}"
            )
            print(f"      {candidate['cmd'][:240]}")

    if not args.kill:
        print(
            f"\nDRY RUN -- nothing killed. Re-run with --kill to reap {len(candidates)}."
        )
        return 0

    reaped, skipped = reap(candidates, min_age_minutes=args.min_age_minutes)
    print(f"\nreaped {reaped}, revalidation-vetoed or failed {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
