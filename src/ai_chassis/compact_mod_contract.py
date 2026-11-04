"""The file contract between the context supervisor and the compact-guard mod (ISSUE-REF).

A bare Claude Code session with no tmux pane and no submit FIFO can still be compacted in
process by a mod calling ``$.session.compact``. The mod and the supervisor meet only through
the files here, under the mods design's runtime root (D-10): the mod's ``presence`` record
says it is loaded in a session, and the supervisor's ``request`` asks it to compact for one
save-state attempt. Every state transition stays the supervisor's (design D-3).

Standard library only, because the bridge wrapper runs it before any venv is known.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MOD_NAME = "compact-guard"
ROOT_ENV = "AIH_COMPACT_MOD_ROOT"


def runtime_root() -> Path:
    """``~/.claude/state/ai-harness/mods/compact``, or ``$AIH_COMPACT_MOD_ROOT``."""
    override = os.environ.get(ROOT_ENV)
    if override:
        return Path(override)
    return Path.home() / ".claude" / "state" / "ai-harness" / "mods" / "compact"


def session_key(session_id: str) -> str:
    """Lowercase hex sha256 of the session id's UTF-8 bytes, the mods design's file key."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def presence_path(session_id: str, root: Path | None = None) -> Path:
    return (root or runtime_root()) / "presence" / f"{session_key(session_id)}.json"


def request_path(session_id: str, root: Path | None = None) -> Path:
    return (root or runtime_root()) / "requests" / f"{session_key(session_id)}.json"


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _parse_stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def atomic_write_json(path: Path, record: dict[str, Any]) -> None:
    """Write ``record`` to ``path`` by rename, directories 0700 and the file 0600."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, staging = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.write("\n")
        os.chmod(staging, 0o600)
        os.replace(staging, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(staging)
        raise


def _read_object(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "absent"
    except (OSError, UnicodeDecodeError):
        return None, "unreadable"
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None, "malformed"
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
        return None, "malformed"
    return record, "read"


def write_presence(
    session_id: str, *, mod_version: str, cc_version: str, root: Path | None = None
) -> Path:
    """The mod's record that it is loaded in ``session_id`` (the bridge ``presence`` verb)."""
    path = presence_path(session_id, root)
    atomic_write_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "mod": MOD_NAME,
            "mod_version": mod_version,
            "cc_version": cc_version,
            "written_at": _stamp(_now()),
        },
    )
    return path


def presence_reason(
    session_id: str,
    *,
    ttl_seconds: float,
    root: Path | None = None,
    now: datetime | None = None,
) -> str | None:
    """None when a fresh presence record for ``session_id`` exists, else why not.

    Fresh means ``written_at`` within ``ttl_seconds`` of now and not in the future. The
    reason is one of ``absent``, ``unreadable``, ``malformed``, ``other-session`` or
    ``stale``, each a ``mod-absent`` detail for the transport notice.
    """
    record, detail = _read_object(presence_path(session_id, root))
    if record is None:
        return detail
    if record.get("session_id") != session_id or record.get("mod") != MOD_NAME:
        return "other-session"
    written = _parse_stamp(record.get("written_at"))
    if written is None:
        return "malformed"
    age = ((now or _now()) - written).total_seconds()
    if age < 0 or age > ttl_seconds:
        return "stale"
    return None


def write_request(  # noqa: PLR0913 -- the request's five fields plus its root
    session_id: str,
    *,
    key: str,
    attempt_id: str,
    bound_at: datetime,
    expires_at: datetime,
    root: Path | None = None,
) -> Path:
    """The supervisor's ask that the mod compact ``session_id`` for one attempt."""
    path = request_path(session_id, root)
    atomic_write_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "key": key,
            "attempt_id": attempt_id,
            "bound_at": _stamp(bound_at),
            "expires_at": _stamp(expires_at),
        },
    )
    return path


def read_request(
    session_id: str, root: Path | None = None
) -> tuple[dict[str, Any] | None, str]:
    """``(request, "read")`` for ``session_id``, else ``(None, reason)``."""
    record, detail = _read_object(request_path(session_id, root))
    if record is None:
        return None, detail
    if record.get("session_id") != session_id:
        return None, "other-session"
    return record, "read"


def remove_request(
    session_id: str, *, attempt_id: str | None = None, root: Path | None = None
) -> bool:
    """Remove ``session_id``'s request, only if it names ``attempt_id`` when one is given."""
    path = request_path(session_id, root)
    if attempt_id is not None:
        record, _detail = read_request(session_id, root)
        if record is None or record.get("attempt_id") != attempt_id:
            return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
