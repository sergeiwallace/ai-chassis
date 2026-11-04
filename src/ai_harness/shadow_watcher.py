"""Fail-closed private task-row primitives for shadow Codex watchers.

Claude Code exposes no documented external TaskUpdate interface. The direct
task-store writer records the installed version for diagnostics and permits
writes only when the captured task-row shape remains valid.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TASK_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TASK_ID_RE = re.compile(r"^[1-9][0-9]*$")
TASK_STATUSES = frozenset({"pending", "in_progress", "completed"})
TASK_ROW_KEYS = frozenset(
    {
        "id",
        "subject",
        "description",
        "owner",
        "status",
        "blocks",
        "blockedBy",
    }
)


def map_agent_status(record: Mapping[str, object]) -> str:
    """Map the ledger outcome without mistaking an unclassified signal for cancel."""
    state = record.get("state")
    if state == "queued":
        return "queued"
    if state == "running":
        return "running"
    if state == "succeeded":
        return "completed"
    if state == "failed":
        return "failed"
    if state == "signaled":
        return "cancelled" if record.get("cancellation_reason") else "died"
    return "unknown"


class ShadowWatcherError(RuntimeError):
    """Base error for a watcher that must stop rather than write unsafely."""


class TaskPreconditionError(ShadowWatcherError):
    """The target row is not safe or eligible for a periodic projection."""


class ProjectionDisabled(ShadowWatcherError):
    """The capability probe has not authorized direct writes."""


@dataclass(frozen=True)
class TaskTarget:
    """The sole namespace-local task row a watcher may project to."""

    namespace: str
    cc_task_id: str


@dataclass(frozen=True)
class ProbeResult:
    """Recorded task-shape result and installed-version diagnostic signal."""

    client_version: str
    documented_external_task_update_available: bool
    task_shape_matches: bool

    @property
    def direct_projection_enabled(self) -> bool:
        return (
            not self.documented_external_task_update_available
            and self.task_shape_matches
        )


@dataclass(frozen=True)
class WatcherConfig:
    refresh_interval_seconds: float = 30.0
    status_read_grace_seconds: float = 5.0
    max_consecutive_failures: int = 3

    def __post_init__(self) -> None:
        if self.refresh_interval_seconds <= 0 or self.status_read_grace_seconds < 0:
            raise ValueError("watcher intervals must be positive")
        if self.max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be positive")


@dataclass(frozen=True)
class WatcherCycle:
    stopped: bool
    interval_seconds: float
    reason: str | None = None


class ShadowWatcher:
    """One observer cycle; it deliberately has no detached-job cancel capability.

    Callers own process scheduling.  This small state machine makes final
    projection/release ordering and hot reload testable without a live CC task.
    """

    def __init__(  # noqa: PLR0913 - callbacks are the watcher isolation boundary.
        self,
        *,
        read_status: Callable[[], Mapping[str, object]],
        renew_lease: Callable[[], bool],
        release_lease: Callable[[], bool],
        project: Callable[[Mapping[str, object], bool], None],
        load_config: Callable[[], WatcherConfig],
        read_status_with_grace: Callable[
            [Callable[[], Mapping[str, object]], float], Mapping[str, object]
        ]
        | None = None,
    ) -> None:
        self._read_status = read_status
        self._read_status_with_grace = read_status_with_grace or (
            lambda read, _grace: read()
        )
        self._renew_lease = renew_lease
        self._release_lease = release_lease
        self._project = project
        self._load_config = load_config
        self._config = WatcherConfig()
        self._failures = 0
        self._stopped = False

    @property
    def config(self) -> WatcherConfig:
        return self._config

    def stop(self) -> None:
        """Release this watcher's lease for a requested graceful shutdown."""
        if not self._stopped:
            self._release_lease()
            self._stopped = True

    def cycle(self) -> WatcherCycle:
        """Observe once, retaining the previous interval on invalid config."""
        if self._stopped:
            return WatcherCycle(True, self._config.refresh_interval_seconds, "stopped")
        try:
            self._config = self._load_config()
        except (OSError, ValueError, TypeError):
            # Invalid/unreadable configuration is not a job failure.
            pass
        try:
            if not self._renew_lease():
                raise ShadowWatcherError("fenced lease renewal refused")
            status = self._read_status_with_grace(
                self._read_status, self._config.status_read_grace_seconds
            )
            if not isinstance(status, Mapping) or not isinstance(
                status.get("state"), str
            ):
                raise ShadowWatcherError("malformed supervisor status")
            terminal = status["state"] in {"succeeded", "failed", "signaled", "unknown"}
            self._project(status, terminal)
            self._failures = 0
            if terminal:
                # The final completed projection happens before release and no later cycle polls.
                if not self._release_lease():
                    self._stopped = True
                    return WatcherCycle(
                        True,
                        self._config.refresh_interval_seconds,
                        ShadowWatcherError.__name__,
                    )
                self._stopped = True
                return WatcherCycle(
                    True, self._config.refresh_interval_seconds, "terminal"
                )
            return WatcherCycle(False, self._config.refresh_interval_seconds)
        except (OSError, ShadowWatcherError, ValueError, TypeError) as exc:
            self._failures += 1
            if self._failures >= self._config.max_consecutive_failures:
                self._release_lease()
                self._stopped = True
                return WatcherCycle(
                    True, self._config.refresh_interval_seconds, type(exc).__name__
                )
            return WatcherCycle(
                False, self._config.refresh_interval_seconds, type(exc).__name__
            )


class CapabilityProbe:
    """Check the captured task-row shape and record the installed CC version."""

    def __init__(self, *, client_version: str) -> None:
        self.client_version = client_version

    def check(self, row: Path) -> ProbeResult:
        return ProbeResult(
            client_version=self.client_version,
            documented_external_task_update_available=False,
            task_shape_matches=_matches_pinned_task_shape(row),
        )


def _matches_pinned_task_shape(path: Path) -> bool:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(decoded, dict)
        and set(decoded) in (TASK_ROW_KEYS, TASK_ROW_KEYS | {"activeForm"})
        and isinstance(decoded["id"], str)
        and TASK_ID_RE.fullmatch(decoded["id"]) is not None
        and all(
            isinstance(decoded[name], str)
            for name in ("subject", "description", "owner")
        )
        and ("activeForm" not in decoded or isinstance(decoded["activeForm"], str))
        and decoded["status"] in TASK_STATUSES
        and all(
            isinstance(decoded[name], list)
            and all(isinstance(value, str) for value in decoded[name])
            for name in ("blocks", "blockedBy")
        )
    )


class TaskRowResolver:
    """Resolve exactly one regular, user-owned task row beneath a trusted root."""

    def __init__(self, tasks_root: Path) -> None:
        self.tasks_root = Path(tasks_root)

    def resolve(self, namespace: str, cc_task_id: str) -> Path:
        if TASK_NAMESPACE_RE.fullmatch(namespace) is None:
            raise TaskPreconditionError("invalid CC task namespace")
        if TASK_ID_RE.fullmatch(cc_task_id) is None:
            raise TaskPreconditionError(f"invalid cc_task_id={cc_task_id!r}")
        root = self._validate_directory(self.tasks_root, "task root")
        namespace_dir = self._validate_directory(root / namespace, "task namespace")
        candidate = namespace_dir / f"{cc_task_id}.json"
        try:
            candidate.relative_to(root)
        except (
            ValueError
        ) as exc:  # Defensive: grammar above already excludes traversal.
            raise TaskPreconditionError("task row escapes task root") from exc
        self._validate_regular(candidate, "task row")
        return candidate

    @staticmethod
    def _validate_directory(path: Path, label: str) -> Path:
        try:
            details = path.lstat()
        except OSError as exc:
            raise TaskPreconditionError(f"{label} cannot be inspected: {path}") from exc
        if stat.S_ISLNK(details.st_mode):
            raise TaskPreconditionError(f"{label} is a symlink: {path}")
        if not stat.S_ISDIR(details.st_mode):
            raise TaskPreconditionError(f"{label} is not a directory: {path}")
        _validate_owner_and_permissions(path, details)
        try:
            return path.resolve(strict=True)
        except OSError as exc:
            raise TaskPreconditionError(
                f"{label} cannot be canonicalized: {path}"
            ) from exc

    @staticmethod
    def _validate_regular(path: Path, label: str) -> None:
        try:
            details = path.lstat()
        except FileNotFoundError as exc:
            raise TaskPreconditionError(
                f"cc_task_id={path.stem} observed state=missing"
            ) from exc
        except OSError as exc:
            raise TaskPreconditionError(f"{label} cannot be inspected: {path}") from exc
        if stat.S_ISLNK(details.st_mode):
            raise TaskPreconditionError(f"{label} is a symlink: {path}")
        if not stat.S_ISREG(details.st_mode):
            raise TaskPreconditionError(f"{label} is not a regular file: {path}")
        _validate_owner_and_permissions(path, details)


def _validate_owner_and_permissions(path: Path, details: os.stat_result) -> None:
    if details.st_uid != os.getuid():
        raise TaskPreconditionError(f"task path has unexpected owner: {path}")
    if stat.S_IMODE(details.st_mode) & 0o022:
        raise TaskPreconditionError(f"task path has unsafe permissions: {path}")


try:  # POSIX: Linux, macOS
    import fcntl as _fcntl

    _msvcrt = None
except ImportError:  # Windows
    _fcntl = None
    import msvcrt as _msvcrt


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on a sidecar lock file.

    The primitive is chosen by probing the module rather than by branching on
    ``sys.platform``, matching ``scripts/bd_common.py``: CPython ships ``fcntl`` on
    POSIX and ``msvcrt`` on Windows and neither imports on the other, so the
    unguarded ``import fcntl`` this replaces was an ImportError at module load --
    which made this module and every test importing it uncollectable on Windows.

    ``msvcrt`` locks a byte range from the descriptor's current offset rather than
    the whole file; the descriptor is freshly opened so that offset is 0, and
    locking one byte there is the conventional whole-file equivalent. ``os.chmod``
    to 0o600 is retained but is a no-op against Windows DACLs, so the sidecar's
    protection there is the directory it lives in, not this mode.
    """
    lock_path = path.with_name(f".{path.stem}.shadow-watcher.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        if _fcntl is not None:
            _fcntl.flock(descriptor, _fcntl.LOCK_EX)
        else:
            _msvcrt.locking(descriptor, _msvcrt.LK_LOCK, 1)
        yield
    finally:
        with contextlib.suppress(OSError):
            if _fcntl is not None:
                _fcntl.flock(descriptor, _fcntl.LOCK_UN)
            else:
                _msvcrt.locking(descriptor, _msvcrt.LK_UNLCK, 1)
        os.close(descriptor)


def guarded_task_update(
    resolver: TaskRowResolver,
    target: TaskTarget,
    *,
    probe: ProbeResult,
    lease_is_live: Callable[[], bool],
    mutate: Callable[[dict[str, Any]], Mapping[str, Any]],
) -> None:
    """Durably replace one eligible task row while holding a private write lock.

    This intentionally does not claim protection from an independently-writing
    Claude Code process; it only prevents a torn or partial writer-owned update.
    """
    if not probe.direct_projection_enabled:
        raise ProjectionDisabled("task projection is disabled by the capability probe")
    path = resolver.resolve(target.namespace, target.cc_task_id)
    with _exclusive_lock(path):
        # Re-resolve inside the lock: this is the per-write freshness check.
        path = resolver.resolve(target.namespace, target.cc_task_id)
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TaskPreconditionError(f"task row is malformed: {path}") from exc
        if not isinstance(task, dict) or not _matches_pinned_task_shape(path):
            raise ProjectionDisabled(
                "task projection is disabled by a task-shape mismatch"
            )
        if task["status"] == "completed":
            raise TaskPreconditionError(
                f"cc_task_id={target.cc_task_id} observed state=completed"
            )
        if not lease_is_live():
            raise TaskPreconditionError(
                f"cc_task_id={target.cc_task_id} has no live lease"
            )
        updated = dict(mutate(task))
        if updated.get("id") != target.cc_task_id:
            raise TaskPreconditionError("task mutation changed cc_task_id")
        _durable_replace(path, updated)


def _durable_replace(path: Path, task: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(dict(task), indent=2, sort_keys=True) + "\n"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
