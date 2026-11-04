#!/usr/bin/env python3
"""Process questions answered on a host that may have no ``/proc``.

WHY THIS EXISTS
---------------
Three probes in this repo answered "is this pid alive" and "who is this pid's parent"
by reading ``/proc`` and nothing else. macOS has no procfs, so on example-client-host-mac (measured
2026-09-20) each one returned the SAME answer for a live process and for a pid that
never existed:

* ``dolt_server.process_alive`` -- ``Path(f"/proc/{pid}").is_dir()`` -> every recorded
  server pid read as gone, so ``probe`` reported ``recorded pid N is gone`` for its own
  interpreter and ``ensure`` went on to start a second server over a healthy one;
* ``bd_new.intent_is_live`` -> every in-flight filing read as an orphan, which is what
  authorizes the repair write that "finishes" a record still being written;
* ``git_index_lock_reaper._parent_pid`` -> ``None`` on the first step, so ``_own_pids``
  walked no ancestry at all and the reaper could report its own launcher as the lock
  holder -- the exact defect the ancestry walk was added to close (ISSUE-REF, 2026-09-07).

All three fail WRONG rather than closed: a ``/proc``-less host cannot be distinguished
from a dead process, and the wrong answer is the one that authorizes the destructive or
duplicative action. That is the difference from ``git_index_lock_reaper.ProcTable``,
which is equally Linux-only and correct anyway, because listing an absent ``/proc``
raises and the caller refuses to act.

The ladder here is the one already settled twice in this repo for process identity --
``/proc`` first, then the platform's stock ``ps`` (``heavy_test_lease.
_process_start_identity``, ``aido_authority._process_creation_time``). This module is
where the next caller finds it instead of writing a fourth copy.

START AND BOOT IDENTITY (G91)
-----------------------------
:func:`start_token` and :func:`boot_token` exist because a pid ALONE cannot carry
ownership across pid reuse or a reboot, and the two copies of that pairing in this repo
were both ``/proc``-only. One of them -- ``fleet_testkit/temproot.py::_owner_record`` --
therefore wrote a pytest temporary-root ownership record whose pid, start identity and
boot identity were all ``None`` on every macOS run, and the reaper that reads it treated
that anonymous record as carrying no process reference at all. Measured: the only thing
then standing between a live macOS session's temp root and deletion was that the reaper's
OTHER proof, a Linux-only ``/proc`` inventory, was *unavailable* -- safe by accident, and
unsafe the moment that inventory is ported.

``fleet_testkit.identity`` is the reference implementation of the same ladder, and the
two must agree token-for-token: a record is written through one and read through the
other. ``tests/test_process_liveness.py`` pins that agreement on the running host.

NEVER READS ARGV
----------------
No function here reads ``/proc/<pid>/cmdline``, ``ps -o args`` or any other argv source,
and none may be added: on this fleet a Dolt or ``bd`` command line can carry a run
handle, so printing one is a secret disclosure (ISSUE-REF).
Existence and parentage are answerable without it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# The procfs mount point as one named constant rather than a literal per read, so the
# real field parsing can be exercised against a synthetic procfs from a host that has
# none. Stubbing the walk instead leaves the parser untested on every Mac -- measured in
# `cx_session_supervisor`, which carries the same constant for the same reason.
PROCFS_ROOT = Path("/proc")

# `ps` is bounded because it is on the path of a guard that must not hang: the index-lock
# reaper walks up to 64 ancestors, one call each, while holding an operator waiting on a
# lock. Generous relative to a measured local `ps` (single-digit ms) and still finite.
_PS_TIMEOUT_SECONDS = 5


def _windows_pid_alive(pid: int) -> bool:
    """Ask Windows for the actual process list.

    MEASURED on Windows: a DEAD pid makes ``os.kill(pid, 0)`` raise ``PermissionError``
    ([WinError 5] Access is denied), exactly as a live process owned by someone else
    does, so the POSIX idiom cannot tell the two apart and every dead lock looked live.
    ``tasklist`` is a stock system binary, so this needs no third-party dependency, and
    psutil is used ahead of it when present because it is the right primitive.
    """
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    tasklist = shutil.which("tasklist")
    if tasklist is None:
        return True
    try:
        out = subprocess.run(
            [tasklist, "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    # tasklist prints "INFO: No tasks are running..." when nothing matches; a match
    # echoes the image name followed by the pid.
    return str(pid) in out.stdout


def pid_alive(pid: int) -> bool:
    """True if ``pid`` names a live process.

    On POSIX ``os.kill(pid, 0)`` is preferred over ``psutil.pid_exists`` deliberately,
    even though psutil is a declared runtime dependency here: it is a syscall on one pid
    rather than a process-table read, needs no dependency at all in a script that may be
    copied out of this repo, and answers exactly this question. ``PermissionError`` means
    the process exists and is not ours, which is alive. Windows needs a different
    instrument entirely -- see :func:`_windows_pid_alive`.

    Anything that cannot be decided counts as ALIVE. Every caller uses this to gate a
    delete, a repair write or a restart, so an unknown answer must not authorize the
    action. An out-of-range pid raises ``OverflowError`` rather than being mapped here,
    because a caller that can be handed an unbounded pid (a lock file records digits, not
    an int) already decides that case for itself -- ``worktree_audit._pid_is_alive``
    catches it and answers alive.
    """
    if sys.platform == "win32":
        return _windows_pid_alive(pid)

    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        # Includes ProcessLookupError (a subclass): no such process.
        return False
    return True


def ppid_from_proc_stat(text: str) -> int | None:
    """PPID out of one ``/proc/<pid>/stat`` line, parsed from the LAST ``)``.

    ``comm`` is field 2, wrapped in parentheses, and may itself contain spaces and
    parentheses (``odd (name) with spaces``, systemd's ``(sd-pam)``), so splitting the
    line from the start misreads every later field. Slicing after the final ``)`` is the
    only correct way in.

    Split out from the file read so the parsing is testable on a host with no procfs:
    while it was welded to ``Path(f"/proc/{pid}/stat")`` the only test of it compared
    ``_parent_pid(os.getpid())`` with ``os.getppid()``, whose ``comm`` is ``python3.13``
    -- so the paren handling the test was named for was never exercised anywhere.
    """
    try:
        return int(text[text.rindex(")") + 2 :].split()[1])
    except (ValueError, IndexError):
        return None


def start_token_from_proc_stat(text: str) -> str | None:
    """Field 22 (``starttime``) out of one ``/proc/<pid>/stat`` line.

    Sliced after the LAST ``)`` for the same reason as :func:`ppid_from_proc_stat`:
    ``comm`` is parenthesised and may itself contain spaces and parentheses.

    Returned as the raw STRING, not an int. Nothing interprets a start token -- the
    only operation on one is equality against a token read on the same host and boot --
    and keeping it opaque is what lets one untagged token type serve a source whose
    value is a jiffy count and one whose value is a date.
    """
    try:
        return text[text.rindex(")") + 2 :].split()[19]
    except (ValueError, IndexError):
        return None


def start_token(pid: int, *, procfs_root: Path = PROCFS_ROOT) -> str | None:
    """A start identity for ``pid`` that a recycled pid cannot forge, or None.

    UNTAGGED BY DESIGN: the caller is forbidden from parsing this, so it carries no
    platform marker. ``/proc/<pid>/stat`` field 22 where procfs exists, otherwise
    ``ps -o lstart=``. Same ladder as :func:`parent_pid`, same rung order.

    None means "cannot say", NEVER "no longer ours". A caller must map it to unknown
    and take no action: a missing token is exactly what an unreadable process and a
    dead one have in common, so pair this with :func:`pid_alive`, which is the only
    positive absence proof here.

    KNOWN ASYMMETRY, stated where the token is produced: ``ps -o lstart=`` is
    second-resolution while Linux' field 22 is jiffy-resolution, so a macOS token is a
    weaker pid-reuse discriminator -- a recycled pid would have to land inside the same
    second. Still strictly better than the anonymous record it replaces (G91).
    """
    try:
        token = start_token_from_proc_stat(
            (procfs_root / str(pid) / "stat").read_text(encoding="utf-8")
        )
    except OSError:
        token = None
    if token is not None:
        return token

    ps = shutil.which("ps")
    if ps is None:
        return None
    try:
        result = subprocess.run(
            [ps, "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        # A dead pid, or one too large for the platform, exits non-zero with no rows.
        return None
    return result.stdout.strip() or None


def pid_namespace(
    *, procfs_root: Path = PROCFS_ROOT, platform: str = sys.platform
) -> str:
    """The pid namespace this process's pids are numbered in.

    The stdlib twin of ``fleet_testkit.identity.pid_namespace()``, which WRITES the value
    into ownership records, and it must answer identically. A pid is evidence only inside
    the namespace that issued it: a reader in another namespace looks it up in a different
    process table, so its absence there proves nothing about the recorder (ISSUE-REF).
    ``"none"`` on a kernel without pid namespaces; ``"unknown"`` on Linux when the link is
    hidden or denied, which a reader must never treat as a match.
    """
    if not platform.startswith("linux"):
        return "none"
    try:
        return os.readlink(procfs_root / "self" / "ns" / "pid")
    except OSError:
        return "unknown"


def boot_token(*, procfs_root: Path = PROCFS_ROOT) -> str:
    """A per-boot identity, or the literal ``"unknown"``.

    A recorded boot token that differs from the current one resolves DEAD by
    construction -- nothing recorded before a reboot can still be running -- which
    makes reboot the one ownership question decidable without touching the process
    table at all.

    ``"unknown"`` rather than None deliberately, matching
    ``fleet_testkit.identity.boot_id()``: this value is WRITTEN into ownership records,
    so one sentinel shared by the writer's ladder and the reader's ladder is what keeps
    "could not read it" from being confused with a real boot id on either side.
    """
    try:
        value = (
            (procfs_root / "sys/kernel/random/boot_id")
            .read_text(encoding="utf-8")
            .strip()
        )
    except OSError:
        value = ""
    if value:
        return value

    sysctl = shutil.which("sysctl")
    if sysctl is None:
        return "unknown"
    try:
        result = subprocess.run(
            [sysctl, "-n", "kern.bootsessionuuid"],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        # Linux has `sysctl` and no such key, which is the same answer as no `sysctl`.
        return "unknown"
    return result.stdout.strip() or "unknown"


def parent_pid(pid: int, *, procfs_root: Path = PROCFS_ROOT) -> int | None:
    """``pid``'s parent, or None when this host cannot say.

    ``/proc`` first: one read, no subprocess, and the only source available inside a
    container with no ``ps``. Then the platform's stock ``ps``, which is how macOS
    answers -- same ladder as ``heavy_test_lease._process_start_identity``.

    Windows reaches neither rung and gets None, which is what it got before. That is in
    contract for both callers. ``git_index_lock_reaper._own_pids`` serves a tool whose
    holder scan reads ``/proc`` directly and already refuses outright on a host without
    it, so there is no Windows path in which a missing ancestor widens a hazard.
    ``agent_release_on_stop._own_lineage`` only REPORTS leftover process groups and never
    signals one, so a missing ancestor can at worst name the launcher's own group.
    """
    try:
        return ppid_from_proc_stat(
            (procfs_root / str(pid) / "stat").read_text(encoding="utf-8")
        )
    except OSError:
        pass

    ps = shutil.which("ps")
    if ps is None:
        return None
    try:
        result = subprocess.run(
            [ps, "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        # A dead pid exits non-zero with no rows, as does a pid too large for the
        # platform ("process id too large"). Neither is an ancestor.
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None
