"""install.sh's ~/.claude settings write lock, shared by the shell and the full apply (ISSUE-REF).

Every global-settings writer is read-modify-write, so two installs must not interleave them. The
lock is a directory created with ``mkdir`` (macOS ships no ``flock``) holding a ``pid`` file. The
shell's ``_acquire_settings_lock`` and :func:`acquire` take the same directory with the same wait,
so a running install.sh and a full ``ai-harness apply`` exclude each other.

The wait: poll every 0.1 s for ``AIH_SETTINGS_LOCK_ATTEMPTS`` attempts (default 300), reclaiming
only a lock whose pid is definitely dead; a just-created lock with no pid yet is not stale. At the
timeout, :func:`timeout` names the holder and breaks the lock only when it is stale (ISSUE-REF):

  0  the lock is gone, or was stale and has been broken: try to take it again
  1  still held: a FATAL line naming the holder was printed to stderr

A holder that is itself an install (its argv names install.sh or install_harness.py) holds the
lock for most of a run, far longer than the shell's own wait, so it is waited on for up to
``AIH_SETTINGS_LOCK_INSTALL_WAIT`` seconds (default 600), with one "waiting on" line every
``AIH_SETTINGS_LOCK_REPORT_EVERY`` seconds (default 30). Any other holder is reported at once. A
lock is stale when its pid file names a process that is not running, or when it has no readable pid
for longer than ``AIH_SETTINGS_LOCK_PIDLESS_GRACE`` seconds (default 10).

``scripts/settings_lock_report.py`` loads this file by path before install.sh resolves the
fleet-floor interpreter, so it stays stdlib-only and runs on any python3 >= 3.9 (stock macOS).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: The lock directory's name inside the Claude dir (install.sh's ``$CLAUDE_DIR``).
LOCK_NAME = ".ai-harness-settings.lock"
FATAL = "FATAL: timed out waiting for concurrent ~/.claude/settings.json update"
ATTEMPTS_ENV = "AIH_SETTINGS_LOCK_ATTEMPTS"
DEFAULT_ATTEMPTS = 300
ATTEMPT_SECONDS = 0.1
#: The holder argv is cut to this many characters, so one report stays one line.
ARGV_SHOWN = 240
#: A holder whose argv names one of these is an install (ISSUE-REF).
INSTALLERS = ("install.sh", "install_harness.py")
POLL_SECONDS = 0.5
#: GetExitCodeProcess's code for a process that has not exited.
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class SettingsLockTimeout(Exception):
    """The lock was still held when the wait ran out; the holder was reported on stderr."""


def lock_dir(claude_dir: Path) -> Path:
    return claude_dir / LOCK_NAME


def _env_seconds(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _attempts() -> int:
    try:
        return int(os.environ.get(ATTEMPTS_ENV, DEFAULT_ATTEMPTS))
    except ValueError:
        return DEFAULT_ATTEMPTS


def _windows_pid_exists(pid: int) -> bool:
    """Whether a Windows process ``pid`` is running, read without signalling it."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED  # type: ignore[attr-defined]
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_exists(pid: int) -> bool:
    """The shell's ``kill -0``. On Windows ``os.kill(pid, 0)`` would TerminateProcess the pid, so
    there the process is only queried."""
    if os.name == "nt":
        return _windows_pid_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _alive(pid: int) -> bool:
    """Running, and not a zombie: an exited holder answers kill(pid, 0) until reaped."""
    if not _pid_exists(pid):
        return False
    if not shutil.which("ps"):
        return True
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return not state.startswith("Z")


def _ps(pid: int) -> tuple[str, str] | None:
    """(etime, argv) of a running pid, or None when ps cannot read it."""
    if not shutil.which("ps"):
        return None
    result = subprocess.run(
        ["ps", "-o", "etime=", "-o", "args=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    line = result.stdout.strip()
    if result.returncode != 0 or not line:
        return None
    etime, _, args = line.partition(" ")
    args = " ".join(args.split())
    return etime, args if len(args) <= ARGV_SHOWN else args[: ARGV_SHOWN - 3] + "..."


class Lock:
    """One reading of the lock directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            stat = path.stat()
        except FileNotFoundError:
            self.present = False
            return
        self.present = True
        self.inode = stat.st_ino
        self.age = max(0.0, time.time() - stat.st_mtime)
        try:
            raw: str | None = (path / "pid").read_text(encoding="utf-8").strip()
        except OSError:
            raw = None
        self.raw = raw
        self.pid = int(raw) if raw and raw.isdigit() else None

    def stale(self, grace: float) -> bool:
        if self.pid is not None:
            return not _alive(self.pid)
        return self.age > grace

    def describe(self) -> str:
        age = f"lock age {self.age:.0f}s"
        if self.raw is None:
            return f"pid file missing, {age}"
        if self.pid is None:
            return f"pid file malformed ({self.raw[:40]!r}), {age}"
        if not _alive(self.pid):
            return f"held by pid {self.pid} (not running), {age}"
        shown = _ps(self.pid)
        if shown is None:
            return f"held by pid {self.pid} (alive, argv unreadable), {age}"
        etime, argv = shown
        return f"held by pid {self.pid} (alive, etime {etime}): {argv}, {age}"


def _break(lock: Lock) -> bool:
    """Remove a lock judged stale, only if the directory is still the one judged.

    The directory is first renamed to a name only this process uses, so a fresh lock
    created after the judgement is never deleted; one that replaced it is put back."""
    aside = lock.path.with_name(f"{lock.path.name}.stale.{os.getpid()}")
    try:
        os.rename(lock.path, aside)
    except OSError:
        return not lock.path.exists()
    if aside.stat().st_ino != lock.inode:
        try:
            os.rename(aside, lock.path)
        except OSError:
            pass
        return False
    for child in aside.iterdir():
        child.unlink()
    aside.rmdir()
    return True


def _installer(lock: Lock) -> tuple[str, str] | None:
    """(etime, argv) when the lock's live holder is an install, else None."""
    if lock.pid is None or not _alive(lock.pid):
        return None
    shown = _ps(lock.pid)
    if shown is None or not any(name in shown[1] for name in INSTALLERS):
        return None
    return shown


def _settle(path: Path, grace: float) -> Lock | None:
    """None once the lock is gone or broken as stale, else its current reading."""
    lock = Lock(path)
    if lock.present and lock.stale(grace):
        reason = lock.describe()
        if _break(lock):
            print(f"settings lock: broke a stale lock ({reason})", file=sys.stderr)
            return None
        lock = Lock(path)
    return lock if lock.present else None


def timeout(path: Path) -> int:
    """The shell's timeout report: 0 when the lock is gone or was broken, 1 when still held."""
    grace = _env_seconds("AIH_SETTINGS_LOCK_PIDLESS_GRACE", 10)
    budget = _env_seconds("AIH_SETTINGS_LOCK_INSTALL_WAIT", 600)
    every = _env_seconds("AIH_SETTINGS_LOCK_REPORT_EVERY", 30)
    start = time.monotonic()
    reported = None
    while True:
        lock = _settle(path, grace)
        if lock is None:
            return 0
        installer = _installer(lock)
        now = time.monotonic()
        if installer is None and lock.stale(grace) and now - start < budget:
            # The holder exited after _settle read it alive: settle again rather than fail.
            time.sleep(POLL_SECONDS)
            continue
        if installer is None or now - start >= budget:
            break
        if reported is None or now - reported >= every:
            etime, argv = installer
            print(
                f"settings lock: waiting on pid {lock.pid} ({argv}, etime {etime})",
                file=sys.stderr,
            )
            reported = now
        time.sleep(POLL_SECONDS)
    print(f"{FATAL}: {lock.describe()}", file=sys.stderr)
    return 1


def _recorded_pid(path: Path) -> int | None:
    """The shell's ``read -r owner_pid < pid``: the first line, when it is all digits."""
    try:
        lines = (path / "pid").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    first = lines[0].strip() if lines else ""
    return int(first) if first.isdigit() else None


def acquire(path: Path) -> None:
    """Take the lock the way install.sh's ``_acquire_settings_lock`` does, or raise
    :class:`SettingsLockTimeout` once :func:`timeout` reported a holder that is still there."""
    attempts = 0
    while True:
        try:
            os.mkdir(path)
            break
        except FileExistsError:
            pass
        owner = _recorded_pid(path)
        if owner is not None and not _pid_exists(owner):
            (path / "pid").unlink(missing_ok=True)
            try:
                os.rmdir(path)
                continue
            except OSError:
                pass
        attempts += 1
        if attempts >= _attempts():
            try:
                reported = timeout(path)
            except Exception:  # noqa: BLE001 - the shell reads any non-1 status as unknown
                reported = None
            if reported == 0:
                attempts = 0
                continue
            if reported != 1:
                print(f"{FATAL} (holder unknown)", file=sys.stderr)
            raise SettingsLockTimeout(f"{FATAL}: {path}")
        time.sleep(ATTEMPT_SECONDS)
    (path / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")


def release(path: Path) -> None:
    """install.sh's ``_release_settings_lock``: remove the pid file, then the directory."""
    (path / "pid").unlink(missing_ok=True)
    try:
        os.rmdir(path)
    except OSError:
        pass


@contextmanager
def held(path: Path) -> Iterator[None]:
    """The lock, released on every exit path."""
    acquire(path)
    try:
        yield
    finally:
        release(path)
