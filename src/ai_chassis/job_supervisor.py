"""Durable local job ledger and detached-process supervisor.

The ledger is the authority: the control socket is only a way to ask the
supervisor to mutate it.  Records deliberately contain no executable command,
prompt, environment, or other secret-bearing invocation data.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Protocol, Self

import psutil

TERMINAL_STATES = frozenset({"succeeded", "failed", "signaled", "unknown"})
VALID_STATES = frozenset({"queued", "running", *TERMINAL_STATES})
LOG_LIMIT_BYTES = 1_000_000
MAX_CONTROL_FRAME_BYTES = 64 * 1024
CLAIM_STALE_SECONDS = 15 * 60
ACKNOWLEDGED_RETENTION_SECONDS = 7 * 24 * 60 * 60
JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")


class JobError(RuntimeError):
    """A requested job operation cannot safely be performed."""


def require_linked_worktree(worktree: str | Path) -> Path:
    """Fail closed: arbitrary argv has no enforceable read-only classification."""
    remedy = "use a linked worktree for the job"
    try:
        resolved = Path(worktree).resolve()
        result = subprocess.run(
            [
                "git",
                "-C",
                str(resolved),
                "rev-parse",
                "--path-format=absolute",
                "--git-dir",
                "--git-common-dir",
                "--is-inside-work-tree",
            ],
            # A submitter's Git environment must not redirect the topology probe.
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        fields = result.stdout.splitlines()
        if result.returncode or len(fields) != 3 or fields[2] != "true":
            raise JobError(f"cannot verify job worktree Git topology; {remedy}")
        git_dir, common_dir = (Path(value).resolve() for value in fields[:2])
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise JobError(f"cannot verify job worktree Git topology; {remedy}") from error
    if git_dir == common_dir:
        raise JobError(f"job worktree is the main working tree; {remedy}")
    return resolved


class ControlConnection(Protocol):
    """The byte-stream surface shared by Unix sockets and Windows pipe handles."""

    def recv(self, maximum_bytes: int) -> bytes: ...

    def sendall(self, data: bytes) -> None: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, *args: object) -> None: ...


class ControlServer(Protocol):
    """The listener surface used by the daemon's control loop."""

    def accept(self) -> tuple[ControlConnection, object]: ...

    def close(self) -> None: ...


class PlatformAdapter(Protocol):
    """The observed OS boundaries of the durable supervisor core."""

    def restrict_permissions(self, path: Path, mode: int) -> None: ...

    def lock_file(
        self, path: Path, *, nonblocking: bool = False
    ) -> contextlib.AbstractContextManager[None]: ...

    def open_control_client(self, path: Path) -> ControlConnection: ...

    def open_control_server(self, path: Path) -> ControlServer: ...

    def remove_control_endpoint(self, path: Path) -> None: ...

    def start_process(
        self, command: Sequence[str], *, cwd: str
    ) -> subprocess.Popen[bytes]: ...

    def terminate_process_tree(self, pid: int, sig: signal.Signals) -> None: ...

    def notify(self, message: str, click_target: str | None) -> None: ...


class PosixPlatform:
    """POSIX implementation kept behind seams for later native platform work."""

    def restrict_permissions(self, path: Path, mode: int) -> None:
        os.chmod(path, mode)

    @contextlib.contextmanager
    def lock_file(self, path: Path, *, nonblocking: bool = False) -> Iterator[None]:
        # Delayed import keeps the shared state-machine module importable on Windows.
        import fcntl

        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.restrict_permissions(path, 0o600)
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            try:
                fcntl.flock(fd, flags)
            except BlockingIOError as error:
                raise JobError("job-supervisor daemon is already running") from error
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def open_control_client(self, path: Path) -> socket.socket:
        if path.is_symlink():
            raise JobError("unsafe control socket")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(path))
        except Exception:
            client.close()
            raise
        return client

    def open_control_server(self, path: Path) -> socket.socket:
        self.remove_control_endpoint(path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(path))
            self.restrict_permissions(path, 0o600)
            server.listen()
            server.settimeout(0.2)
        except Exception:
            server.close()
            raise
        return server

    def remove_control_endpoint(self, path: Path) -> None:
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                raise JobError("refusing symlinked control socket")
            path.unlink()

    def start_process(
        self, command: Sequence[str], *, cwd: str
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            list(command),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def terminate_process_tree(self, pid: int, sig: signal.Signals) -> None:
        os.killpg(pid, sig)

    def notify(self, message: str, click_target: str | None) -> None:
        _display_notification(message, click_target)


POSIX_PLATFORM = PosixPlatform()


class _WindowsModules(Protocol):
    """The pywin32 APIs used by ``WindowsPlatform`` (loaded only on demand)."""

    win32api: Any
    win32con: Any
    win32file: Any
    win32pipe: Any
    win32security: Any
    pywintypes: Any


def _load_windows_modules() -> _WindowsModules:
    """Load the Windows-only dependency without making POSIX imports fail."""
    try:
        import pywintypes
        import win32api
        import win32con
        import win32file
        import win32pipe
        import win32security
    except ImportError as error:
        raise JobError(
            "Windows job-supervisor requires pywin32 for its secured named pipe"
        ) from error

    class Modules:
        pass

    modules = Modules()
    modules.win32api = win32api
    modules.win32con = win32con
    modules.win32file = win32file
    modules.win32pipe = win32pipe
    modules.win32security = win32security
    modules.pywintypes = pywintypes
    return modules


class _NamedPipeConnection:
    """Adapt a pywin32 named-pipe handle to the shared byte-stream protocol."""

    def __init__(self, handle: Any, modules: _WindowsModules) -> None:
        self.handle = handle
        self.modules = modules

    def recv(self, maximum_bytes: int) -> bytes:
        result = self.modules.win32file.ReadFile(self.handle, maximum_bytes)
        return result[1] if isinstance(result, tuple) else result

    def sendall(self, data: bytes) -> None:
        self.modules.win32file.WriteFile(self.handle, data)

    def close(self) -> None:
        self.modules.win32file.CloseHandle(self.handle)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class _NamedPipeServer:
    """Create one pipe instance for each accepted control request."""

    def __init__(self, platform: WindowsPlatform, name: str) -> None:
        self.platform = platform
        self.name = name
        self.handle: Any | None = self.platform._create_named_pipe(name)

    def accept(self) -> tuple[ControlConnection, object]:
        if self.handle is None:
            self.handle = self.platform._create_named_pipe(self.name)
        handle = self.handle
        try:
            self.platform.modules.win32pipe.ConnectNamedPipe(handle, None)
        except self.platform.modules.pywintypes.error as error:
            if error.winerror != self.platform.modules.win32con.ERROR_PIPE_CONNECTED:
                raise
        self.handle = None
        return _NamedPipeConnection(handle, self.platform.modules), self.name

    def close(self) -> None:
        if self.handle is not None:
            self.platform.modules.win32file.CloseHandle(self.handle)
            self.handle = None


class WindowsPlatform:
    """Windows OS boundaries using a current-user-DACL named pipe via pywin32.

    ``modules`` is injectable solely so that the security-descriptor construction
    can be regression-tested on non-Windows hosts without pretending to exercise
    Windows APIs there.
    """

    def __init__(self, modules: _WindowsModules | None = None) -> None:
        self.modules = modules or _load_windows_modules()

    @staticmethod
    def _pipe_name(path: Path) -> str:
        digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:32]
        return rf"\\.\pipe\ai-harness-job-supervisor-{digest}"

    def _current_user_sid(self) -> Any:
        token = self.modules.win32security.OpenProcessToken(
            self.modules.win32api.GetCurrentProcess(),
            self.modules.win32con.TOKEN_QUERY,
        )
        try:
            sid, _attributes = self.modules.win32security.GetTokenInformation(
                token, self.modules.win32security.TokenUser
            )
            return sid
        finally:
            self.modules.win32api.CloseHandle(token)

    def _security_attributes(self) -> Any:
        sid = self._current_user_sid()
        descriptor = self.modules.win32security.SECURITY_DESCRIPTOR()
        dacl = self.modules.win32security.ACL()
        dacl.AddAccessAllowedAce(
            self.modules.win32security.ACL_REVISION,
            self.modules.win32con.GENERIC_ALL,
            sid,
        )
        descriptor.SetSecurityDescriptorDacl(True, dacl, False)
        attributes = self.modules.win32security.SECURITY_ATTRIBUTES()
        attributes.SECURITY_DESCRIPTOR = descriptor
        return attributes

    def restrict_permissions(self, path: Path, mode: int) -> None:
        del mode  # Windows mode bits are not an access-control boundary.
        descriptor = self._security_attributes().SECURITY_DESCRIPTOR
        self.modules.win32security.SetFileSecurity(
            str(path),
            self.modules.win32security.DACL_SECURITY_INFORMATION,
            descriptor,
        )

    @contextlib.contextmanager
    def lock_file(self, path: Path, *, nonblocking: bool = False) -> Iterator[None]:
        file = self.modules.win32file
        modules = self.modules
        handle = file.CreateFile(
            str(path),
            modules.win32con.GENERIC_READ | modules.win32con.GENERIC_WRITE,
            modules.win32con.FILE_SHARE_READ | modules.win32con.FILE_SHARE_WRITE,
            None,
            modules.win32con.OPEN_ALWAYS,
            modules.win32con.FILE_ATTRIBUTE_NORMAL,
            None,
        )
        self.restrict_permissions(path, 0o600)
        overlapped = modules.pywintypes.OVERLAPPED()
        flags = modules.win32con.LOCKFILE_EXCLUSIVE_LOCK
        if nonblocking:
            flags |= modules.win32con.LOCKFILE_FAIL_IMMEDIATELY
        locked = False
        try:
            try:
                file.LockFileEx(handle, flags, 0, 0xFFFFFFFF, 0xFFFFFFFF, overlapped)
                locked = True
            except modules.pywintypes.error as error:
                if (
                    nonblocking
                    and error.winerror == modules.win32con.ERROR_LOCK_VIOLATION
                ):
                    raise JobError(
                        "job-supervisor daemon is already running"
                    ) from error
                raise
            yield
        finally:
            if locked:
                file.UnlockFileEx(handle, 0, 0xFFFFFFFF, 0xFFFFFFFF, overlapped)
            file.CloseHandle(handle)

    def _create_named_pipe(self, name: str) -> Any:
        pipe = self.modules.win32pipe
        file = self.modules.win32file
        return pipe.CreateNamedPipe(
            name,
            pipe.PIPE_ACCESS_DUPLEX | file.FILE_FLAG_FIRST_PIPE_INSTANCE,
            pipe.PIPE_TYPE_BYTE
            | pipe.PIPE_READMODE_BYTE
            | pipe.PIPE_WAIT
            | pipe.PIPE_REJECT_REMOTE_CLIENTS,
            1,
            MAX_CONTROL_FRAME_BYTES,
            MAX_CONTROL_FRAME_BYTES,
            200,
            self._security_attributes(),
        )

    def open_control_client(self, path: Path) -> ControlConnection:
        name = self._pipe_name(path)
        handle = self.modules.win32file.CreateFile(
            name,
            self.modules.win32con.GENERIC_READ | self.modules.win32con.GENERIC_WRITE,
            0,
            None,
            self.modules.win32con.OPEN_EXISTING,
            0,
            None,
        )
        return _NamedPipeConnection(handle, self.modules)

    def open_control_server(self, path: Path) -> ControlServer:
        return _NamedPipeServer(self, self._pipe_name(path))

    def remove_control_endpoint(self, path: Path) -> None:
        del path  # Closing the final named-pipe handle removes its namespace entry.

    def start_process(
        self, command: Sequence[str], *, cwd: str
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            list(command),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

    def terminate_process_tree(self, pid: int, sig: signal.Signals) -> None:
        if sig != signal.SIGTERM:
            raise JobError(f"Windows cannot map requested cancellation signal {sig}")
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=True)

    def notify(self, message: str, click_target: str | None) -> None:
        del click_target
        print(f"job-supervisor: {message}", file=sys.stderr)


WINDOWS_PLATFORM = WindowsPlatform() if os.name == "nt" else None
DEFAULT_PLATFORM: PlatformAdapter = (
    WINDOWS_PLATFORM if WINDOWS_PLATFORM is not None else POSIX_PLATFORM
)


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def _safe_description(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 .,:;_\-]", "_", value.strip())[:160]
    cleaned = cleaned.replace("..", "__")
    if not cleaned:
        raise JobError("description must contain printable text")
    return cleaned


def _command_hash(command: Sequence[str]) -> str:
    return hashlib.sha256("\0".join(command).encode()).hexdigest()


def _terminal_digest(record: dict[str, Any]) -> str:
    """Return the idempotence key for a terminal notification."""
    payload = {
        "job_id": record["job_id"],
        "state": record["state"],
        "exit_code": record["exit_code"],
        "signal": record["signal"],
        "result_sha256": record["files"]["result"]["sha256"],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _terminal_notification_message(record: dict[str, Any]) -> str:
    """Render sanctioned terminal record fields for a human-facing alert."""
    exit_code = record["exit_code"]
    rendered_exit_code = "not available" if exit_code is None else str(exit_code)
    return (
        f"{record['command_description']} — "
        f"State: {record['state']}; Exit code: {rendered_exit_code}"
    )


#: The fleet's one desktop poster, this checkout's copy, for a daemon whose PATH lacks the installed
#: ``aih-notify`` (a LaunchAgent's is /usr/bin:/bin).
NOTIFIER_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "notify" / "aih_notify.sh"
)
NOTIFICATION_TITLE = "Local job supervisor completion"
#: aih-notify's whole ladder: two bundle rungs of 15 s each plus its probes.
NOTIFIER_TIMEOUT_S = 45


def _resolve_notifier() -> str | None:
    installed = shutil.which("aih-notify")
    if installed is not None:
        return installed
    return str(NOTIFIER_SCRIPT) if NOTIFIER_SCRIPT.is_file() else None


def _display_notification(message: str, click_target: str | None = None) -> None:
    """Post the completion banner through ``aih-notify``; no iTerm2 target is inferred or claimed.

    The daemon cannot prove which iTerm2 terminal is live and attached, so it deliberately does
    not attempt a bell or badge signal.

    THROUGH aih-notify AND NOTHING ELSE (ISSUE-REF, design D-12). This used to try
    ``terminal-notifier``, which this managed Mac refuses ("Notifications are not allowed for this
    application"), and then bare ``osascript``, whose banner macOS files under Script Editor with
    no icon and a "Show" button that launches Script Editor (ISSUE-REF). aih-notify posts from a
    bundle the fleet owns, under the ``pipeline`` feature.

    ``click_target`` is the job's result FILE, and no click may open a file (ISSUE-REF), so the
    banner carries no click action. Raises when no notifier resolves or it reports not delivered,
    which is what makes the caller record ``notification_last_error`` and retry on reconciliation.
    """
    del click_target
    notifier = _resolve_notifier()
    if notifier is None:
        raise FileNotFoundError("aih-notify is not installed; no banner was posted")
    subprocess.run(
        [notifier, "--feature", "pipeline", NOTIFICATION_TITLE, message],
        check=True,
        timeout=NOTIFIER_TIMEOUT_S,
        capture_output=True,
    )


def _process_start_time(pid: int) -> float | None:
    """Return a non-reusable identity component; failure means "cannot prove"."""
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        return process.create_time()
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return None


def _same_process(pid: int | None, start_time: float | None) -> bool:
    if pid is None or start_time is None:
        return False
    current = _process_start_time(pid)
    return current is not None and abs(current - start_time) < 0.01


class Ledger:
    """One atomically-replaced record in an owner-only directory per job."""

    def __init__(
        self, root: Path | None = None, *, platform: PlatformAdapter = DEFAULT_PLATFORM
    ) -> None:
        self.root = (
            (root or Path.home() / ".claude" / "job-ledger").expanduser().resolve()
        )
        self.jobs_root = self.root / "jobs"
        self.idempotency_root = self.root / "idempotency"
        self.platform = platform

    @classmethod
    def from_environment(cls) -> Ledger:
        # This override is intentionally for isolated tests and manually-managed deployments.
        override = os.environ.get("AIH_JOB_LEDGER_ROOT")
        return cls(Path(override) if override else None)

    def initialise(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.jobs_root.mkdir(mode=0o700, exist_ok=True)
        self.idempotency_root.mkdir(mode=0o700, exist_ok=True)
        self.platform.restrict_permissions(self.root, 0o700)
        self.platform.restrict_permissions(self.jobs_root, 0o700)
        self.platform.restrict_permissions(self.idempotency_root, 0o700)

    def _job_dir(self, job_id: str) -> Path:
        if not JOB_ID_RE.fullmatch(job_id):
            raise JobError("invalid job ID")
        path = self.jobs_root / job_id
        if path.parent != self.jobs_root:
            raise JobError("job path escapes ledger root")
        if path.exists() and (path.is_symlink() or not path.is_dir()):
            raise JobError("unsafe job directory")
        return path

    def _record_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "record.json"

    def record_path(self, job_id: str) -> Path:
        """Return the validated on-disk record path for a job."""
        return self._record_path(job_id)

    def _assert_regular_or_missing(self, path: Path) -> None:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise JobError(f"unsafe ledger path: {path.name}")

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        self._assert_regular_or_missing(path)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self.platform.restrict_permissions(path, 0o600)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    @contextlib.contextmanager
    def locked(self, job_id: str) -> Iterator[dict[str, Any]]:
        job_dir = self._job_dir(job_id)
        self._assert_regular_or_missing(job_dir / "record.json")
        lock = job_dir / ".lock"
        with self.platform.lock_file(lock):
            yield self.read(job_id)

    def write_locked(self, record: dict[str, Any]) -> None:
        self._write_json(self._record_path(str(record["job_id"])), record)

    def create(
        self,
        *,
        description: str,
        command: Sequence[str],
        owner_identity: str,
        worktree: str,
        supervisor_unit: str,
    ) -> dict[str, Any]:
        if not command:
            raise JobError("command is required")
        resolved_worktree = require_linked_worktree(worktree)
        self.initialise()
        job_id = uuid.uuid4().hex
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(mode=0o700)
        self.platform.restrict_permissions(job_dir, 0o700)
        accepted_at = _now()
        paths = {
            name: str(job_dir / filename)
            for name, filename in (
                ("stdout", "stdout.log"),
                ("stderr", "stderr.log"),
                ("result", "result.txt"),
            )
        }
        record: dict[str, Any] = {
            "job_id": job_id,
            "owner_identity": owner_identity[:200],
            "worktree": str(resolved_worktree),
            "command_description": _safe_description(description),
            "command_sha256": _command_hash(command),
            "accepted_at": accepted_at,
            "started_at": None,
            "finished_at": None,
            "supervisor_unit": supervisor_unit,
            "child_pid": None,
            "child_start_time": None,
            "state": "queued",
            "state_history": [
                {"state": "queued", "at": accepted_at, "reason": "accepted"}
            ],
            "exit_code": None,
            "signal": None,
            "reason": None,
            "cancellation_reason": None,
            "files": {
                name: {"path": path, "sha256": None, "bytes": 0}
                for name, path in paths.items()
            },
            "notification_attempts": 0,
            "claim_owner": None,
            "claim_at": None,
            "acknowledged_at": None,
            "archive_state": "active",
        }
        self._write_json(self._record_path(job_id), record)
        return record

    def create_idempotent(
        self,
        *,
        description: str,
        command: Sequence[str],
        owner_identity: str,
        worktree: str,
        supervisor_unit: str,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically create or return an owner/worktree-scoped matching job."""
        if not idempotency_key or len(idempotency_key) > 200:
            raise JobError("idempotency key must be 1-200 characters")
        resolved_worktree = str(require_linked_worktree(worktree))
        self.initialise()
        owner = owner_identity[:200]
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "description": _safe_description(description),
                    "command_sha256": _command_hash(command),
                    "owner_identity": owner,
                    "worktree": resolved_worktree,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        index_name = hashlib.sha256(
            f"{owner}\0{resolved_worktree}\0{idempotency_key}".encode()
        ).hexdigest()
        index_path = self.idempotency_root / f"{index_name}.json"
        lock_path = self.idempotency_root / f".{index_name}.lock"
        self._assert_regular_or_missing(index_path)
        self._assert_regular_or_missing(lock_path)
        with self.platform.lock_file(lock_path):
            if index_path.exists():
                index = json.loads(index_path.read_text(encoding="utf-8"))
                if index.get("fingerprint") != fingerprint:
                    raise JobError(
                        "idempotency key was already used with a different payload"
                    )
                return self.read(str(index["job_id"])), False
            record = self.create(
                description=description,
                command=command,
                owner_identity=owner,
                worktree=resolved_worktree,
                supervisor_unit=supervisor_unit,
            )
            self._write_json(
                index_path,
                {"job_id": record["job_id"], "fingerprint": fingerprint},
            )
            return record, True

    @contextlib.contextmanager
    def daemon_lock(self) -> Iterator[None]:
        """Atomically guard daemon start before endpoint cleanup or reconciliation."""
        with self.platform.lock_file(self.root / "daemon.lock", nonblocking=True):
            yield

    def prune_acknowledged(
        self,
        *,
        retention_seconds: int = ACKNOWLEDGED_RETENTION_SECONDS,
        dry_run: bool = False,
        limit: int = 100,
    ) -> dict[str, list[str]]:
        """Prune only terminal records acknowledged before the explicit retention TTL."""
        if retention_seconds < 0:
            raise JobError("retention seconds must be non-negative")
        if limit < 1:
            raise JobError("prune limit must be positive")
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=retention_seconds)
        result: dict[str, list[str]] = {"pruned": [], "skipped": [], "failures": []}
        for processed, initial in enumerate(self.records()):
            if processed >= limit:
                break
            job_id = str(initial["job_id"])
            try:
                with self.locked(job_id) as record:
                    acknowledged_at = record.get("acknowledged_at")
                    if (
                        record["state"] not in TERMINAL_STATES
                        or not acknowledged_at
                        or _parse_time(str(acknowledged_at)) > cutoff
                    ):
                        result["skipped"].append(job_id)
                        continue
                    job_dir = self._job_dir(job_id)
                    entries = {entry.name: entry for entry in job_dir.iterdir()}
                    allowed = {
                        ".lock",
                        "record.json",
                        "stdout.log",
                        "stderr.log",
                        "result.txt",
                    }
                    if set(entries) - allowed:
                        raise JobError("unsafe job directory contents")
                    for entry in entries.values():
                        mode = entry.lstat().st_mode
                        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                            raise JobError("unsafe job directory contents")
                    if dry_run:
                        result["pruned"].append(job_id)
                        continue
                    tombstone = self.jobs_root / f".pruning-{job_id}"
                    if tombstone.exists() or tombstone.is_symlink():
                        raise JobError("prune tombstone already exists")
                    os.replace(job_dir, tombstone)
                    for name in (
                        "record.json",
                        "stdout.log",
                        "stderr.log",
                        "result.txt",
                        ".lock",
                    ):
                        with contextlib.suppress(FileNotFoundError):
                            (tombstone / name).unlink()
                    tombstone.rmdir()
                    result["pruned"].append(job_id)
            except (JobError, OSError, ValueError, json.JSONDecodeError):
                result["failures"].append(job_id)
        return result

    def read(self, job_id: str) -> dict[str, Any]:
        path = self._record_path(job_id)
        self._assert_regular_or_missing(path)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise JobError("job not found") from error

    def records(self) -> Iterator[dict[str, Any]]:
        self.initialise()
        for candidate in self.jobs_root.iterdir():
            if (
                candidate.is_symlink()
                or not candidate.is_dir()
                or not JOB_ID_RE.fullmatch(candidate.name)
            ):
                continue
            try:
                yield self.read(candidate.name)
            except (JobError, json.JSONDecodeError):
                continue

    def transition(
        self, record: dict[str, Any], state: str, *, reason: str | None = None
    ) -> None:
        if state not in VALID_STATES:
            raise JobError("invalid state")
        previous = record["state"]
        if previous in TERMINAL_STATES:
            raise JobError("terminal job cannot transition")
        at = _now()
        record["state"] = state
        record["state_history"].append({"state": state, "at": at, "reason": reason})
        record["reason"] = reason
        if state == "running":
            record["started_at"] = at
        if state in TERMINAL_STATES:
            record["finished_at"] = at


class _LimitedWriter:
    def __init__(
        self,
        path: Path,
        platform: PlatformAdapter,
        limit: int = LOG_LIMIT_BYTES,
    ) -> None:
        self.path = path
        self.platform = platform
        self.limit = limit
        self.written = 0
        self.handle = path.open("wb")
        self.platform.restrict_permissions(path, 0o600)

    def copy(self, source: Any) -> None:
        while chunk := source.read(65536):
            remaining = self.limit - self.written
            if remaining > 0:
                part = chunk[:remaining]
                self.handle.write(part)
                self.written += len(part)
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()


class Supervisor:
    """Owns detached children and is the sole normal writer of accepted jobs."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.unit = f"{socket.gethostname()}:{os.getpid()}:{_now()}"
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._threads: list[threading.Thread] = []

    def submit(
        self,
        description: str,
        command: Sequence[str],
        owner_identity: str,
        worktree: str,
        idempotency_key: str | None = None,
    ) -> str:
        if idempotency_key is None:
            record = self.ledger.create(
                description=description,
                command=command,
                owner_identity=owner_identity,
                worktree=worktree,
                supervisor_unit=self.unit,
            )
            created = True
        else:
            record, created = self.ledger.create_idempotent(
                description=description,
                command=command,
                owner_identity=owner_identity,
                worktree=worktree,
                supervisor_unit=self.unit,
                idempotency_key=idempotency_key,
            )
        if not created:
            return str(record["job_id"])
        thread = threading.Thread(
            target=self._start, args=(record["job_id"], tuple(command)), daemon=True
        )
        thread.start()
        self._threads.append(thread)
        return str(record["job_id"])

    def _start(self, job_id: str, command: Sequence[str]) -> None:
        terminal = False
        with self.ledger.locked(job_id) as record:
            if record["state"] != "queued":
                return
            stdout = _LimitedWriter(
                Path(record["files"]["stdout"]["path"]), self.ledger.platform
            )
            stderr = _LimitedWriter(
                Path(record["files"]["stderr"]["path"]), self.ledger.platform
            )
            try:
                cwd = require_linked_worktree(record["worktree"])
                process = self.ledger.platform.start_process(command, cwd=str(cwd))
            except (JobError, OSError) as error:
                stdout.handle.close()
                stderr.handle.close()
                self.ledger.transition(
                    record,
                    "failed",
                    reason=f"spawn refused: {error}"
                    if isinstance(error, JobError)
                    else f"spawn failed: {error.__class__.__name__}",
                )
                record["exit_code"] = 127
                self._write_result(record)
                self.ledger.write_locked(record)
                terminal = True
            start_time = None if terminal else _process_start_time(process.pid)
            if start_time is None and not terminal:
                process.kill()
                stdout.handle.close()
                stderr.handle.close()
                self.ledger.transition(
                    record, "unknown", reason="could not establish child identity"
                )
                self._write_result(record)
                self.ledger.write_locked(record)
                terminal = True
            if terminal:
                process = None
            else:
                record["child_pid"] = process.pid
                record["child_start_time"] = start_time
                self.ledger.transition(record, "running")
                self.ledger.write_locked(record)
        if terminal:
            self._notify_terminal(job_id)
            return
        assert process is not None
        self._processes[job_id] = process
        out_thread = threading.Thread(
            target=stdout.copy, args=(process.stdout,), daemon=True
        )
        err_thread = threading.Thread(
            target=stderr.copy, args=(process.stderr,), daemon=True
        )
        out_thread.start()
        err_thread.start()
        status = process.wait()
        out_thread.join()
        err_thread.join()
        self._processes.pop(job_id, None)
        with self.ledger.locked(job_id) as record:
            if record["state"] in TERMINAL_STATES:
                return
            if status >= 0:
                target = "succeeded" if status == 0 else "failed"
                self.ledger.transition(record, target)
                record["exit_code"] = status
            else:
                self.ledger.transition(record, "signaled")
                record["signal"] = -status
            self._write_result(record)
            self._refresh_file_metadata(record)
            self.ledger.write_locked(record)
        self._notify_terminal(job_id)

    def _notify_terminal(self, job_id: str) -> None:
        """Best-effort notification after, never instead of, a terminal commit."""
        with self.ledger.locked(job_id) as record:
            if record["state"] not in TERMINAL_STATES:
                return
            digest = _terminal_digest(record)
            if record.get("notification_delivered_digest") == digest:
                return
            record["notification_attempts"] += 1
            # Commit the deduplication marker before crossing the notification
            # boundary.  A process interruption after a successful local
            # notification cannot then redeliver the same terminal result.
            record["notification_delivered_digest"] = digest
            record["notification_delivered_at"] = _now()
            self.ledger.write_locked(record)
        try:
            self.ledger.platform.notify(
                _terminal_notification_message(record),
                str(record["files"]["result"]["path"]),
            )
        except Exception as error:  # noqa: BLE001 - notification is best effort
            try:
                with self.ledger.locked(job_id) as record:
                    if record.get("notification_delivered_digest") == digest:
                        record.pop("notification_delivered_digest", None)
                        record.pop("notification_delivered_at", None)
                        record["notification_last_error"] = error.__class__.__name__
                        self.ledger.write_locked(record)
            except (JobError, OSError):
                pass
            return
        try:
            with self.ledger.locked(job_id) as record:
                if _terminal_digest(record) == digest:
                    record["notification_last_error"] = None
                    self.ledger.write_locked(record)
        except (JobError, OSError):
            pass

    def _write_result(self, record: dict[str, Any]) -> None:
        path = Path(record["files"]["result"]["path"])
        payload = f"state={record['state']}\nexit_code={record['exit_code']}\nsignal={record['signal']}\n"
        with path.open("wb") as handle:
            handle.write(payload.encode()[:LOG_LIMIT_BYTES])
            handle.flush()
            os.fsync(handle.fileno())
        self.ledger.platform.restrict_permissions(path, 0o600)
        self._refresh_file_metadata(record)

    def _refresh_file_metadata(self, record: dict[str, Any]) -> None:
        for item in record["files"].values():
            path = Path(item["path"])
            if path.exists() and not path.is_symlink():
                content = path.read_bytes()
                item["bytes"] = len(content)
                item["sha256"] = hashlib.sha256(content).hexdigest()

    def cancel(self, job_id: str) -> dict[str, Any]:
        terminal = False
        with self.ledger.locked(job_id) as record:
            if record["state"] in TERMINAL_STATES:
                return record
            if record["state"] == "queued":
                record["cancellation_reason"] = "operator requested cancellation"
                self.ledger.transition(
                    record, "signaled", reason="cancelled before launch"
                )
                record["signal"] = signal.SIGTERM
                self._write_result(record)
                self.ledger.write_locked(record)
                terminal = True
            else:
                pid = record["child_pid"]
                started = record["child_start_time"]
                if not _same_process(pid, started):
                    self.ledger.transition(
                        record,
                        "unknown",
                        reason="cancel refused: child identity not live",
                    )
                    self._write_result(record)
                    self.ledger.write_locked(record)
                    terminal = True
                else:
                    # This commit precedes the signal so a watcher can distinguish an
                    # operator-requested cancellation from an unrelated terminal signal.
                    record["cancellation_reason"] = "operator requested cancellation"
                    self.ledger.write_locked(record)
                    try:
                        self.ledger.platform.terminate_process_tree(
                            int(pid), signal.SIGTERM
                        )
                    except ProcessLookupError:
                        self.ledger.transition(
                            record, "unknown", reason="cancel raced child exit"
                        )
                        self._write_result(record)
                        self.ledger.write_locked(record)
                        terminal = True
        if terminal:
            self._notify_terminal(job_id)
        return record

    def claim(self, job_id: str, claimant: str) -> dict[str, Any]:
        with self.ledger.locked(job_id) as record:
            if record["state"] not in TERMINAL_STATES:
                raise JobError("only terminal jobs may be claimed")
            current = record["claim_owner"]
            claim_at = record["claim_at"]
            stale = (
                claim_at is not None
                and (_parse_time(_now()) - _parse_time(claim_at)).total_seconds()
                > CLAIM_STALE_SECONDS
            )
            if current and current != claimant and not stale:
                raise JobError("job already has a fresh claim")
            record["claim_owner"] = claimant[:200]
            record["claim_at"] = _now()
            self.ledger.write_locked(record)
            return record

    def ack(self, job_id: str) -> dict[str, Any]:
        with self.ledger.locked(job_id) as record:
            if record["state"] not in TERMINAL_STATES:
                raise JobError("only terminal jobs may be acknowledged")
            record["acknowledged_at"] = _now()
            self.ledger.write_locked(record)
            return record

    def reconcile(self) -> None:
        for initial in self.ledger.records():
            if initial["state"] in TERMINAL_STATES:
                self._notify_terminal(str(initial["job_id"]))
                continue
            with self.ledger.locked(str(initial["job_id"])) as record:
                if record["state"] in TERMINAL_STATES:
                    continue
                if record["state"] == "queued":
                    # Commands are deliberately not retained. A replacement daemon cannot
                    # safely launch this accepted-but-unstarted record, so make that loss
                    # explicit instead of silently stranding it in the queue.
                    self.ledger.transition(
                        record,
                        "unknown",
                        reason="reconciliation found accepted job before child launch",
                    )
                    self._write_result(record)
                    self.ledger.write_locked(record)
                elif not _same_process(record["child_pid"], record["child_start_time"]):
                    self.ledger.transition(
                        record,
                        "unknown",
                        reason="reconciliation could not prove child identity alive",
                    )
                    self._write_result(record)
                    self.ledger.write_locked(record)
            self._notify_terminal(str(initial["job_id"]))

    def wait_all(self, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))


def _control_frame(payload: dict[str, Any]) -> bytes:
    frame = json.dumps(payload, sort_keys=True).encode() + b"\n"
    if len(frame) > MAX_CONTROL_FRAME_BYTES:
        raise JobError("control frame exceeds 64 KiB")
    return frame


def _receive_control_frame(connection: ControlConnection) -> bytes:
    """Read exactly one bounded newline-terminated frame before JSON decoding."""
    frame = bytearray()
    while len(frame) < MAX_CONTROL_FRAME_BYTES:
        chunk = connection.recv(MAX_CONTROL_FRAME_BYTES - len(frame))
        if not chunk:
            raise JobError("control frame is missing a terminating newline")
        frame.extend(chunk)
        newline = frame.find(b"\n")
        if newline >= 0:
            if newline != len(frame) - 1:
                raise JobError(
                    "control frame contains data after its terminating newline"
                )
            return bytes(frame[:-1])
    raise JobError("control frame exceeds 64 KiB")


def _send_request(ledger: Ledger, request: dict[str, Any]) -> dict[str, Any]:
    with ledger.platform.open_control_client(ledger.root / "control.sock") as client:
        client.sendall(_control_frame(request))
        response = _receive_control_frame(client)
    decoded = json.loads(response)
    if not decoded.get("ok"):
        raise JobError(str(decoded.get("error", "supervisor request failed")))
    return decoded["result"]


def run_daemon(ledger: Ledger, stop: threading.Event | None = None) -> None:
    ledger.initialise()
    socket_path = ledger.root / "control.sock"
    with ledger.daemon_lock():
        supervisor = Supervisor(ledger)
        supervisor.reconcile()
        server = ledger.platform.open_control_server(socket_path)
        try:
            while stop is None or not stop.is_set():
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    response: dict[str, Any]
                    try:
                        request = json.loads(_receive_control_frame(connection))
                        action = request["action"]
                        if action == "submit":
                            result: Any = {
                                "job_id": supervisor.submit(**request["arguments"])
                            }
                        elif action == "status":
                            result = ledger.read(request["arguments"]["job_id"])
                        elif action in {"cancel", "claim", "ack"}:
                            result = getattr(supervisor, action)(**request["arguments"])
                        elif action == "prune":
                            result = ledger.prune_acknowledged(**request["arguments"])
                        elif action == "ping":
                            result = {"pid": os.getpid()}
                        else:
                            raise JobError("unsupported control action")
                        response = {"ok": True, "result": result}
                    except (
                        JobError,
                        KeyError,
                        TypeError,
                        ValueError,
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                    ) as error:
                        response = {"ok": False, "error": str(error)}
                    try:
                        connection.sendall(_control_frame(response))
                    except JobError:
                        connection.sendall(
                            _control_frame(
                                {
                                    "ok": False,
                                    "error": "control response exceeds 64 KiB",
                                }
                            )
                        )
        finally:
            server.close()
            with contextlib.suppress(FileNotFoundError):
                ledger.platform.remove_control_endpoint(socket_path)


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-supervisor")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("daemon")
    commands.add_parser("ping")
    submit = commands.add_parser("submit")
    submit.add_argument("description")
    submit.add_argument("--idempotency-key")
    submit.add_argument("command", nargs=argparse.REMAINDER)
    for name in ("status", "cancel", "ack"):
        command = commands.add_parser(name)
        command.add_argument("job_id")
    claim = commands.add_parser("claim")
    claim.add_argument("job_id")
    claim.add_argument("claimant")
    prune = commands.add_parser("prune")
    prune.add_argument(
        "--retention-seconds", type=int, default=ACKNOWLEDGED_RETENTION_SECONDS
    )
    prune.add_argument("--dry-run", action="store_true")
    prune.add_argument("--limit", type=int, default=100)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # argparse.REMAINDER silently swallows sibling optionals (e.g.
    # --idempotency-key) into the remainder capture once a "submit" command
    # is present on the line -- reproduced directly against both 3.13 and
    # 3.14 -- so the "--"-delimited command is split off before argparse
    # ever sees it, rather than relying on REMAINDER to isolate it.
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    submit_command: list[str] | None = None
    if raw_argv[:1] == ["submit"] and "--" in raw_argv:
        separator = raw_argv.index("--")
        submit_command = raw_argv[separator + 1 :]
        raw_argv = raw_argv[:separator]
    args = _cli_parser().parse_args(raw_argv)
    ledger = Ledger.from_environment()
    if args.action == "daemon":
        try:
            run_daemon(ledger)
        except JobError as error:
            print(f"job-supervisor: {error}", file=sys.stderr)
            return 1
        else:
            return 0
    arguments: dict[str, Any] = vars(args).copy()
    arguments.pop("action")
    if args.action == "submit":
        command = arguments.pop("command")
        if submit_command is not None:
            command = submit_command
        elif command[:1] == ["--"]:
            command = command[1:]
        arguments.update(
            command=command,
            owner_identity=os.environ.get("CLAUDE_CODE_TASK_LIST_ID", "unknown"),
            worktree=os.getcwd(),
        )
    try:
        result = _send_request(ledger, {"action": args.action, "arguments": arguments})
    except (JobError, OSError) as error:
        print(f"job-supervisor: {error}", file=os.sys.stderr)
        return 1
    if args.action == "submit":
        print(result["job_id"])
    else:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
