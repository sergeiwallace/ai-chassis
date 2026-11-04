"""Private SessionStart binding records for ``sync-git`` mutation authority.

The session identifier is only a lookup key.  A usable record additionally
binds the registered worktree/common-dir and the controller process start time;
that prevents a copied state file or a reused PID from authorizing a mutation.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover - production fail-closed path
    psutil = None

SCRIPT_ROOT = Path(__file__).resolve().parents[3]


def _load_sync_engine():
    """Load the shared git-review-sync engine from its own file location.

    This module is always executed outside a package -- by the SessionStart hook
    directly, and by the sibling ``sync_git.py``, which itself loads it by file
    location -- so the repo root is never on ``sys.path``.  ``sync_git.py``
    resolves this same engine exactly this way (``_load_shared``), so doing it
    here keeps one idiom for one dependency instead of adding a ``sys.path``
    shim, whose only effect was to make the engine's ``import`` statement
    module-level code out of order.  A private module name is used so this never
    replaces an engine instance a caller has already loaded.
    """
    spec = importlib.util.spec_from_file_location(
        "sync_git_review_engine", SCRIPT_ROOT / "scripts" / "git_review_sync.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_git_review_engine"] = module
    spec.loader.exec_module(module)
    return module


sync = _load_sync_engine()

API_VERSION = 1
MAX_TTL_SECONDS = 12 * 60 * 60
STATE_DIRECTORY = Path.home() / ".claude" / "state" / "sync-git-owner-bindings-v1"


class BindingError(ValueError):
    """The binding producer or verifier cannot prove owner-session identity."""


@dataclass(frozen=True)
class OwnerSessionBindingV1:
    api_version: int
    session_id: str
    worktree: str
    common_dir: str
    controller_pid: int
    controller_start_time: float
    issued_at: float
    expires_at: float
    allowed_command: str


def _safe_session_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(char in value for char in "/\\")
        or ".." in value
    ):
        raise BindingError("session id is not a safe registry key")
    return value


def _process_start_time(pid: int) -> float:
    if psutil is None:
        raise BindingError("process identity probe is unavailable")
    try:
        process = psutil.Process(pid)
        if process.uids().real != os.getuid():
            raise BindingError("controller is not owned by this user")
        return process.create_time()
    except (psutil.Error, OSError) as exc:
        raise BindingError("controller identity is unreadable") from exc


def _controller_ancestor() -> tuple[int, float]:
    """Find one same-user Claude controller; absence is a producer refusal."""
    if psutil is None:
        raise BindingError("process ancestry probe is unavailable")
    try:
        chain = [psutil.Process(os.getppid()), *psutil.Process(os.getppid()).parents()]
    except psutil.Error as exc:
        raise BindingError("controller ancestry is unreadable") from exc
    for process in chain:
        try:
            if process.uids().real != os.getuid():
                continue
            name = process.name().lower()
            if "claude" in name:
                return process.pid, process.create_time()
        except psutil.Error:
            continue
    raise BindingError("no same-user Claude controller ancestor is provable")


def _record_path(session_id: str, directory: Path = STATE_DIRECTORY) -> Path:
    return directory / f"{_safe_session_id(session_id)}.json"


def write_session_binding(
    payload: dict[str, Any],
    *,
    directory: Path = STATE_DIRECTORY,
    now: float | None = None,
    controller: tuple[int, float] | None = None,
) -> OwnerSessionBindingV1:
    """Atomically publish one private binding; never replace a live record."""
    session_id = _safe_session_id(payload.get("session_id"))
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise BindingError("SessionStart payload has no worktree cwd")
    target = sync.resolve_sync_targets(Path(cwd), [cwd])[0]
    controller_pid, controller_start = controller or _controller_ancestor()
    if controller_pid <= 0 or controller_start <= 0:
        raise BindingError("controller identity is invalid")
    issued = time.monotonic() if now is None else now
    record = OwnerSessionBindingV1(
        api_version=API_VERSION,
        session_id=session_id,
        worktree=str(target.worktree),
        common_dir=str(target.common_dir),
        controller_pid=controller_pid,
        controller_start_time=controller_start,
        issued_at=issued,
        expires_at=issued + MAX_TTL_SECONDS,
        allowed_command="sync-git",
    )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    destination = _record_path(session_id, directory)
    if destination.exists():
        current = read_session_binding(session_id, directory=directory)
        if current.expires_at > issued:
            raise BindingError("a live binding already exists for this session")
        raise BindingError("expired binding is retained; a new namespace is required")
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".binding-", dir=directory)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record.__dict__, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, destination)
        temporary.unlink()
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError as exc:
        raise BindingError("binding replacement is forbidden") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return record


def read_session_binding(
    session_id: str, *, directory: Path = STATE_DIRECTORY
) -> OwnerSessionBindingV1:
    """Load a regular, private, schema-complete record without link traversal."""
    path = _record_path(session_id, directory)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BindingError("owner-session binding is absent") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise BindingError("owner-session binding is not a regular file")
    if metadata.st_mode & 0o077:
        raise BindingError("owner-session binding permissions are not private")
    if metadata.st_uid != os.getuid():
        raise BindingError("owner-session binding owner differs")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        binding = OwnerSessionBindingV1(**value)
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise BindingError("owner-session binding is malformed") from exc
    if binding.api_version != API_VERSION or binding.allowed_command != "sync-git":
        raise BindingError("owner-session binding version or command differs")
    if binding.session_id != session_id:
        raise BindingError("owner-session binding session differs")
    return binding


def verify_session_binding(
    session_id: str,
    target: sync.SyncTarget,
    *,
    directory: Path = STATE_DIRECTORY,
    now: float | None = None,
    current_pid: int | None = None,
) -> OwnerSessionBindingV1:
    """Prove current process ancestry and exact checked-out target identity."""
    binding = read_session_binding(session_id, directory=directory)
    current_time = time.monotonic() if now is None else now
    if binding.expires_at <= current_time:
        raise BindingError("owner-session binding expired")
    if (
        Path(binding.worktree).resolve() != target.worktree
        or Path(binding.common_dir).resolve() != target.common_dir
    ):
        raise BindingError("owner-session binding target differs")
    if _process_start_time(binding.controller_pid) != binding.controller_start_time:
        raise BindingError("controller PID has exited or been reused")
    if psutil is None:
        raise BindingError("process ancestry probe is unavailable")
    child_pid = os.getpid() if current_pid is None else current_pid
    try:
        lineage = [psutil.Process(child_pid), *psutil.Process(child_pid).parents()]
        if binding.controller_pid not in {process.pid for process in lineage}:
            raise BindingError("current command is not a controller descendant")
    except psutil.Error as exc:
        raise BindingError("current command ancestry is unreadable") from exc
    return binding


def main() -> int:
    """SessionStart hook entry point: failures write nothing and return non-zero."""
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise BindingError("SessionStart payload is not an object")
        write_session_binding(payload)
    except (BindingError, OSError, json.JSONDecodeError) as exc:
        print(f"sync-git binding not written: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - invoked by SessionStart.
    raise SystemExit(main())
