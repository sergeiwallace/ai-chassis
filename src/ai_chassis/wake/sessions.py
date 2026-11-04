"""Host selection and session-local watch registration acknowledgements."""

import json
import math
import os
import time
import uuid
from pathlib import Path

from .events import write_record, sync_directory
from .registry import Registry
from .targets import safe_identity


def enabled():
    return Registry().enabled()


def mail_root():
    return (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "ai-harness/agent-mail"
    )


def directory(root, slug):
    target = Path(root) / "sessions" / safe_identity(slug)
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("symlink-session")
    return target


def migration_window(root):
    try:
        row = json.loads((Path(root) / "dispatch/migration-window.json").read_text())
        if (
            row["schema"] == 1
            and isinstance(row["window_id"], str)
            and row["expires_at"] > time.time()
            and isinstance(row["sessions"], list)
        ):
            return row
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def migration_id(root, slug, session_id):
    row = migration_window(root)
    if row and f"{slug}:{session_id}" in row["sessions"]:
        return row["window_id"]
    return None


def registration_enabled(root, slug, session_id):
    """Registration alone may overlap legacy delivery during a bounded window."""
    return enabled() or migration_id(root, slug, session_id) is not None


def close_migration(root):
    path = Path(root) / "dispatch/migration-window.json"
    if path.is_symlink():
        raise ValueError("symlink-state")
    if path.exists():
        # Inspect this exact owned record before removing it; never remove hooks' acks.
        row = json.loads(path.read_text())
        write_record(
            path.with_name("migration-window-closed.json"),
            {"schema": 1, "window_id": row["window_id"]},
        )
        path.unlink()
        sync_directory(path.parent)


def acknowledge(root, slug, session_id, producer):
    safe_identity(session_id)
    if producer not in {"mail", "context"}:
        raise ValueError("invalid-registration-producer")
    record = {"schema": 1, "slug": slug, "session_id": session_id}
    window_id = migration_id(root, slug, session_id)
    if window_id:
        record["window_id"] = window_id
    write_record(directory(root, slug) / f"watch-{producer}.json", record)
    (Path(root) / "dispatch/intake").mkdir(parents=True, exist_ok=True, mode=0o700)


def registered(root, slug, session_id, producer=None):
    try:
        safe_identity(session_id)
        target = directory(root, slug)
        window_id = migration_id(root, slug, session_id)
        if (Path(root) / "dispatch/migration-window.json").exists() and not window_id:
            return False
        for name in (producer,) if producer else ("mail", "context"):
            record = json.loads((target / f"watch-{name}.json").read_text())
            if not isinstance(record, dict):
                return False
            saved_window = record.pop("window_id", None)
            if window_id and saved_window != window_id:
                return False
            if record != {"schema": 1, "slug": slug, "session_id": session_id}:
                return False
        return True
    except (OSError, ValueError, TypeError):
        return False


def activation_ready(root, live):
    """A verified live inventory and both acknowledgements are a cutover gate.

    The caller obtains the inventory once at host activation. Unavailable
    inventory is not an empty host, and a stale session's ack is not reusable.
    """
    return live is not None and all(
        registered(root, item.slug, item.session_id) for item in live
    )


def _bind_migration_session(root, slug, sid, item):
    path = directory(root, slug) / "binding.json"
    binding = json.loads(path.read_text()) if path.exists() else None
    if binding is not None and binding.get("session_id") != sid:
        raise ValueError("session-binding-mismatch")
    if hasattr(item, "transcript") and (
        binding is None or (not binding.get("pane") and binding.get("pid") == item.pid)
    ):
        from ai_chassis.compact_rearm import _pane_for_session

        # Cutover runs outside the target pane. Resolve its validated Claude
        # PID, never the cutover process's TMUX_PANE or a renamed session key.
        pane = _pane_for_session(item)
        if binding is None or pane is not None:
            bind(root, slug, sid, item.transcript, item.pid, pane=pane)


def prepare_migration(root, live, resume_root, *, window_s=120):
    """Prepare a supervisor's verified inventory for CwdChanged registration.

    This does not acknowledge watches, select a scheduler or publish markers.
    The cutover owner must still wait for both target hooks' acknowledgements.
    Existing legacy state and delivery stay available until that gate passes.
    """
    if live is None:
        raise ValueError("inventory-unavailable")
    for item in live:
        slug = safe_identity(getattr(item, "slug", None) or item.name)
        sid = safe_identity(item.session_id)
        directory(root, slug).mkdir(parents=True, exist_ok=True, mode=0o700)
        (directory(resume_root, slug) / "wake").mkdir(
            parents=True, exist_ok=True, mode=0o700
        )
        pointer = Path(root) / "sessions" / f"{sid}.slug"
        if pointer.is_symlink():
            raise ValueError("symlink-session")
        if pointer.exists():
            if pointer.read_text().strip() != slug:
                raise ValueError("session-binding-mismatch")
        else:
            with pointer.open("x") as handle:
                handle.write(slug + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        _bind_migration_session(root, slug, sid, item)
    (Path(root) / "dispatch/intake").mkdir(parents=True, exist_ok=True, mode=0o700)
    row = {
        "schema": 1,
        "window_id": uuid.uuid4().hex,
        "expires_at": math.ceil(time.time() + window_s),
        "sessions": [
            f"{getattr(item, 'slug', None) or item.name}:{item.session_id}"
            for item in live
        ],
    }
    # The closed record names only the single most recent window.
    closed = Path(root) / "dispatch/migration-window-closed.json"
    if closed.is_symlink():
        raise ValueError("symlink-state")
    closed.unlink(missing_ok=True)
    write_record(Path(root) / "dispatch/migration-window.json", row)
    return row


def activation_inventory():
    """One activation inventory, never an idle liveness probe.

    Named apart from the notifier's liveness probe (`claude_sessions.live_sessions`) so the
    notifier's read-site inventory test, which matches on that name, does not mistake it
    for an unchained idle read.
    """
    from ai_chassis.goal_waker import discover_sessions
    from .targets import SessionRef

    live = discover_sessions(lambda *_args, **_kwargs: None)
    if live is None:
        return None
    try:
        return frozenset(
            SessionRef(safe_identity(item.name), safe_identity(item.session_id))
            for item in live
        )
    except (ValueError, TypeError):
        return None


def migration_inventory():
    """Retain process-generation metadata for the one-shot cutover wait."""
    from ai_chassis.goal_waker import discover_sessions

    return discover_sessions(lambda *_args, **_kwargs: None)


def main():
    # Service installers use the same gate as the daemon before disabling the
    # legacy scheduler. No marker or selector is changed by this command.
    if not activation_ready(mail_root(), activation_inventory()):
        print("watch-registration-pending")
        return 1
    return 0


def watch_paths(root, slug):
    target = directory(root, slug)
    return [str(target / "wake.notice.txt"), str(target / "poke.txt")]


def legacy_watch_paths(root, slug):
    target = Path(root) / "sessions"
    return [
        str(Path(root) / "wake.poke.txt"),
        str(target / f"{slug}.snapshots" / "wake.notice.txt"),
        str(target / f"{slug}.poke.txt"),
    ]


def registered_watch_paths(root, slug):
    """Retain the watches emitted at registration, independent of host cutover.

    Pre-change transports have no paths. Infer from the event poke's existence and
    keep the answer while the record is unchanged. An absent or unusable transport
    retains the current mode's historical fallback.
    """
    target = Path(root) / "sessions" / slug
    path = target.parent / f"{slug}.transport.json"
    fallback = watch_paths if enabled() else legacy_watch_paths
    key = (str(root), slug)
    signature = _signature(path)
    cached = _REGISTERED.get(key)
    if signature is not None and cached is not None and cached[0] == signature:
        # A steady pass reads no registration record: the stat above is the whole cost.
        return cached[1]
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        # One session's absent or unreadable record must not fail a delivery pass.
        return fallback(root, slug)
    if not isinstance(record, dict) or not isinstance(
        record.get("watch_paths", []), list
    ):
        return fallback(root, slug)
    registered = record.get("watch_paths")
    if registered is None:
        # Never write the registration record from the delivery path: the notifier reads
        # it as a registration signal and would republish every inferred session.
        registered = (
            watch_paths(root, slug)
            if (target / "poke.txt").exists()
            else legacy_watch_paths(root, slug)
        )
    if signature is not None:
        _REGISTERED[key] = (signature, registered)
    return registered


_REGISTERED = {}


def _signature(path):
    try:
        status = os.stat(path)
    except OSError:
        return None
    return (status.st_ino, status.st_mtime_ns, status.st_size)


def delivery_watch_paths(root, slug, kind):
    """Select only this session's registered markers, never the shared watch."""
    target = Path(root) / "sessions" / slug
    event = [str(target / "wake.notice.txt"), str(target / "poke.txt")]
    legacy = legacy_watch_paths(root, slug)
    candidates = {"notice": {event[0], legacy[1]}, "poke": {event[1], legacy[2]}}
    paths = [
        Path(path)
        for path in registered_watch_paths(root, slug)
        if path in candidates[kind]
    ]
    if not paths:
        # A record that names no marker of this kind keeps the current mode's path.
        fallback = event if enabled() else legacy
        paths = [Path(path) for path in fallback if path in candidates[kind]]
    if any(str(path) in event for path in paths):
        directory(root, slug)
    return paths


def event_layout(root, slug):
    """The session's recorded (or pre-record inferred) layout, not host scheduling."""
    paths = registered_watch_paths(root, slug)
    target = Path(root) / "sessions" / slug
    if any(str(target / name) in paths for name in ("wake.notice.txt", "poke.txt")):
        return True
    if any(path in paths for path in legacy_watch_paths(root, slug)):
        return False
    return enabled()


def registration_watch_paths(root, slug, session_id):
    paths = registered_watch_paths(root, slug)
    if migration_id(root, slug, session_id):
        paths = list(
            dict.fromkeys(
                paths + legacy_watch_paths(root, slug) + watch_paths(root, slug)
            )
        )
    return paths


def context_watch_paths(root, slug, session_id=None):
    """Map the registered mail layout onto its matching lifecycle watch."""
    root = Path(root)
    primary = (
        directory(root, slug) / "wake"
        if slug is not None and event_layout(mail_root(), slug)
        else root / "wake"
    )
    paths = [primary]
    if slug is not None and migration_id(mail_root(), slug, session_id):
        paths = list(
            dict.fromkeys(paths + [root / "wake", directory(root, slug) / "wake"])
        )
    return paths


def ensure_watch_files(root, slug):
    """Create each watched file empty if absent, so it exists when the paths register.

    Claude Code fires FileChanged only for a watched file that existed at registration,
    so a marker first written at the first publish never woke an idle session. An empty
    file never equals a wake digest, and an existing file is left exactly as it is.
    """
    target = directory(root, slug)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    _create_empty(target / "wake.notice.txt")
    _create_empty(target / "poke.txt")
    sync_directory(target)


def ensure_legacy_watch_files(root, slug):
    """Make legacy registration watchable without replacing published bytes."""
    paths = [Path(path) for path in legacy_watch_paths(root, slug)]
    for path in paths:
        if path.parent.is_symlink() or path.parent.parent.is_symlink():
            raise ValueError("symlink-session")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _create_empty(path)
    for parent in {path.parent for path in paths}:
        sync_directory(parent)


def _create_empty(path):
    try:
        os.close(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        )
    except FileExistsError:
        pass


def bind(root, slug, session_id, transcript, pid, *, pane=None):  # noqa: PLR0913 - complete target binding, with an optional owned pane
    """Persist only trusted session metadata, never prompt or message contents."""
    safe_identity(session_id)
    path = Path(transcript)
    if path.stem != session_id or path.suffix != ".jsonl" or "subagents" in path.parts:
        raise ValueError("invalid-session-transcript")
    stamp = None
    if isinstance(pid, int) and pid > 0:
        import psutil

        try:
            stamp = psutil.Process(pid).create_time()
        except psutil.Error:
            pass
    from .launches import binding_provenance

    write_record(
        directory(root, slug) / "binding.json",
        {
            "schema": 1,
            "slug": slug,
            "session_id": session_id,
            "transcript": str(path),
            "pid": pid,
            "start_stamp": stamp,
            "pane": pane,
            "bound_at": time.time(),
            **binding_provenance(pid, pane, session_id=session_id),
        },
    )


def bindings(root):
    """One startup/cutover reconciliation, never called on the idle path."""
    found = []
    for path in (Path(root) / "sessions").glob("*/binding.json"):
        try:
            row = json.loads(path.read_text())
            safe_identity(row["slug"])
            safe_identity(row["session_id"])
            if directory(root, row["slug"]) != path.parent:
                continue
            found.append(row)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return found


def eligible(root, binding):
    """Only a terminal event for this bound process generation proves it gone.

    Retain the binding for replay and replacement fencing. Quiet or unknown
    presence is never evidence of exit, and no process sampling is needed.
    """
    path = directory(Path(root) / "dispatch", binding["slug"]) / "presence.json"
    try:
        presence = json.loads(path.read_text())
    except (OSError, ValueError):
        return True
    return not (
        presence.get("session") == binding["session_id"]
        and presence.get("state") in {"exited", "ended"}
        and (
            presence.get("start_stamp") is None
            or binding.get("start_stamp") is None
            or presence["start_stamp"] == binding["start_stamp"]
        )
    )


def refs(root):
    from .targets import SessionRef
    from ai_chassis import chief_of_staff
    from .producers import _script

    rows = [row for row in bindings(root) if eligible(root, row)]
    names = {row["slug"] for row in rows}
    chief = None
    for home in chief_of_staff.chief_homes():
        registry, _ = chief_of_staff.load_registry(home)
        found = chief_of_staff.chief_name(registry, home)
        if found in names:
            chief = found
            break
    policy = _script("harness_sync_census").load_config().installer()
    path = Path(root) / "harness-sync-owner.json"
    saved = json.loads(path.read_text()) if path.exists() else {}
    owner = (saved.get("episode") or {}).get("owner")
    if owner not in names or (policy == "chief" and owner != chief):
        owner = chief or (min(names) if names and policy == "any" else None)
    machine = os.environ.get("AI_HOST", "local")

    return frozenset(
        SessionRef(
            row["slug"],
            row["session_id"],
            roles=frozenset({"installer_owner"})
            if row["slug"] == owner
            else frozenset(),
            assignees=frozenset({row["slug"], f"{row['slug']}@{machine}"}),
            machine=machine,
        )
        for row in rows
    )


def emit(root, slug, session_id, kind, *, epoch=None, metadata=""):  # noqa: PLR0913 - optional durable replay identity
    safe_identity(slug)
    safe_identity(session_id)
    safe_identity(kind)
    epoch = int(time.time()) if epoch is None else epoch
    suffix = f" {metadata}" if metadata else ""
    intake = Path(root) / "dispatch/intake"
    intake.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = intake / f"{uuid.uuid4().hex}.event"
    with name.open("x") as handle:
        handle.write(f"1 {kind} {session_id} {slug} {epoch}{suffix}\n")
        handle.flush()
        os.fsync(handle.fileno())
    sync_directory(intake)


if __name__ == "__main__":
    raise SystemExit(main())
