"""The save-state-to-compact boundary hold (ISSUE-REF D-5..D-8).

Between a ``/save-state`` and the compaction it ends in, nothing but the
compact path itself may type into the session: an inbox nudge, a goal nudge or
a wake line landing there would be compacted away or, worse, sit in the
composer the ``/compact`` needs. So every injector that is not ``self_rescue``
records ``HELD`` instead of typing, and :func:`drain` re-delivers each held
record once the boundary has closed, if it is still within its TTL, still passes
its own injector's preflights and is still relevant; anything else is dropped
with a named reason.

The boundary is read from disk only (the supervisor's attempt ledger and its
save-state grace marker), never by importing the supervisor (D-10). Every
uncertain reading fails toward delivery: a marker that cannot be read, or that
is dated in the future, is absent, because a hold with no boundary behind it is
the silent-loss failure.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ai_chassis.prompt_injection import _tmux
from ai_chassis.prompt_injection.config import boundary_hold, load_config, resume_root
from ai_chassis.prompt_injection.observe import (
    AttemptStore,
    stage_name,
    write_stage_line,
)
from ai_chassis.prompt_injection.outcomes import InjectionOutcome
from ai_chassis.prompt_injection.payload import (
    Payload,
    PromptPayload,
    SlashCommandPayload,
    sha256,
)
from ai_chassis.prompt_injection.targets import (
    SessionTarget,
    TargetUnresolved,
    resolve_target,
)

if TYPE_CHECKING:
    from ai_chassis.prompt_injection.injector import Injector

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore[assignment]
try:
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None  # type: ignore[assignment]

#: A save-state compact attempt in one of these states is a live boundary
#: (context_supervisor.COMPACT_STATES less its terminal states).
BOUNDARY_ATTEMPT_STATES = frozenset(
    {"ARMED", "BOUND", "READY", "SENT", "STARTED", "QUEUED", "STAGED_UNVERIFIED"}
)
HOLD_REASON = "boundary"
SAVE_STATE_TIMEOUT_ENV = "AIH_SAVE_STATE_ACTIVE_TIMEOUT_SECONDS"
# context_supervisor._DEFAULT_SAVE_STATE_ACTIVE_TIMEOUT_SECONDS.
_SAVE_STATE_TIMEOUT_DEFAULT = 300
# A filesystem stamp a second ahead of the clock is granularity, not the future.
_MTIME_SLACK_SECONDS = 1.0
_LOCK_NAME = "boundary.lock"


@dataclass(frozen=True)
class BoundaryState:
    """Why a key is inside the boundary: the save-state attempt holding it open
    (``source="attempt"``), or the grace marker alone (``attempt_id=None``,
    ``state="ACTIVE"``, ``source="marker"``)."""

    attempt_id: str | None
    state: str
    source: Literal["attempt", "marker"]


def marker_path(key: str, root: Path | None = None) -> Path:
    """ResumeStore._active_save_state_path: ``sensing/<sha256(key)>.save-state-active``."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return (root or resume_root()) / "sensing" / f"{digest}.save-state-active"


def _marker_live(key: str, root: Path, current: float) -> bool:
    try:
        timeout = int(
            os.environ.get(SAVE_STATE_TIMEOUT_ENV, _SAVE_STATE_TIMEOUT_DEFAULT)
        )
    except ValueError:
        return False
    path = marker_path(key, root)
    try:
        stamp = int(path.read_text(encoding="utf-8").strip())
        mtime = path.stat().st_mtime
    except (OSError, ValueError):
        return False
    if timeout < 0 or stamp > current or mtime - current > _MTIME_SLACK_SECONDS:
        return False
    return current - stamp <= timeout


def _attempt_boundary(
    key: str, root: Path, current: float, max_age: int
) -> BoundaryState | None:
    """The newest non-terminal save-state compact attempt for ``key``, armed no
    longer ago than the hold's own TTL: an attempt stuck short of terminal for
    longer than any held record could survive must not hold injections forever."""
    for record in AttemptStore(root).read(key):
        if (
            record.get("attempt_type") != "compact"
            or record.get("origin") != "save-state"
            or record.get("state") not in BOUNDARY_ATTEMPT_STATES
        ):
            continue
        armed = record.get("armed_at")
        if not isinstance(armed, int) or isinstance(armed, bool):
            continue
        if 0 <= current - armed <= max_age:
            return BoundaryState(
                str(record.get("attempt_id")), record["state"], "attempt"
            )
    return None


def in_boundary(
    key: str, *, root: Path | None = None, now: float | None = None
) -> BoundaryState | None:
    """Whether ``key`` is between a ``/save-state`` and its confirmed compaction.

    Inside when a save-state-origin ``compact`` attempt is in a non-terminal
    state (:data:`BOUNDARY_ATTEMPT_STATES`), or when the save-state grace marker
    is live (stamped within ``AIH_SAVE_STATE_ACTIVE_TIMEOUT_SECONDS``, default
    300). ``None`` otherwise, including for an unreadable or future-dated marker.
    """
    base = root or resume_root()
    current = time.time() if now is None else float(now)
    _, ttl = boundary_hold(load_config())
    found = _attempt_boundary(key, base, current, ttl)
    if found is not None:
        return found
    if _marker_live(key, base, current):
        return BoundaryState(None, "ACTIVE", "marker")
    return None


# --- the held record ---------------------------------------------------------------


def held_fields(
    payload: Payload, boundary: BoundaryState | None, ttl_seconds: int
) -> dict[str, Any]:
    """The fields a ``HELD`` record adds to an attempt record: enough to rebuild
    the payload and to explain the hold."""
    args = None
    if payload.kind == "slash-command" and payload.command:
        args = payload.text[len(payload.command) :].strip() or None
    return {
        "state": "HELD",
        "held_at": int(time.time()),
        "hold_reason": HOLD_REASON,
        "boundary_attempt_id": boundary.attempt_id if boundary else None,
        "boundary_source": boundary.source if boundary else None,
        "ttl_seconds": int(ttl_seconds),
        "payload_text": payload.text,
        "payload_kind": payload.kind,
        "payload_command": payload.command,
        "payload_sentinel": payload.sentinel,
        "payload_args": args,
        "payload_mode": payload.mode,
    }


def target_fields(target: SessionTarget) -> dict[str, Any]:
    return {
        "pane_id": target.pane_id,
        "pane_fingerprint": target.fingerprint,
        "transcript_path": str(target.transcript_path)
        if target.transcript_path
        else None,
        "transcript_source": target.transcript_source,
        "cwd": str(target.cwd) if target.cwd else None,
        "session_name": target.session_name,
        "cc_version": target.cc_version,
    }


def _where(boundary: BoundaryState | None) -> str:
    if boundary is None:
        return "boundary=none source=none"
    return f"boundary={boundary.attempt_id or 'marker'} source={boundary.source}"


def hold_detail(attempt_id: str, boundary: BoundaryState | None) -> str:
    return f"reason={HOLD_REASON} attempt={attempt_id} {_where(boundary)}"


def held_outcome(record: dict[str, Any], path: Path | None) -> InjectionOutcome:
    return InjectionOutcome(
        attempt_id=str(record["attempt_id"]),
        use_case=str(record.get("use_case")),
        state="HELD",
        landing=None,
        reason="boundary-held",
        staged=False,
        submitted=False,
        evidence={"attempt": path} if path is not None else {},
    )


def _registry() -> Mapping[str, type[Injector]]:
    from ai_chassis.prompt_injection.injector import INJECTORS

    return INJECTORS


def _payload_for(text: str) -> Payload:
    return SlashCommandPayload(text) if text.startswith("/") else PromptPayload(text)


def hold_legacy(  # noqa: PLR0913 - the consumer contract the plan's D-8 names
    key: str,
    use_case: str,
    text: str,
    *,
    ttl_seconds: int | None = None,
    pane_id: str | None = None,
    fingerprint: str | None = None,
    root: Path | None = None,
) -> InjectionOutcome:
    """Record a ``HELD`` line for a caller that types without an :class:`Injector`
    (the supervisor's legacy ``fence_and_send`` legs), in the ledger the
    registered injector for ``use_case`` uses, so :func:`drain` re-delivers it
    through that injector. Logs ``CC-<PREFIX>-HOLD``. ``ValueError`` for a
    use case with no registered injector."""
    cls = _registry().get(use_case)
    if cls is None:
        raise ValueError(f"no injector is registered for {use_case!r}")
    payload = _payload_for(text)
    attempt_id = secrets.token_hex(8)
    boundary = in_boundary(key, root=root)
    if ttl_seconds is None:
        _, ttl_seconds = boundary_hold(load_config())
    record = cls()._new_record(attempt_id, key, payload)
    record.update(held_fields(payload, boundary, ttl_seconds))
    record.update(pane_id=pane_id, pane_fingerprint=fingerprint, reason="boundary-held")
    record.update(hold_options={}, tmux_command=list(_tmux.DEFAULT_COMMAND))
    path = AttemptStore(root, ledger=cls.ledger).write(record)
    _log(cls, key, "HOLD", hold_detail(attempt_id, boundary))
    return held_outcome(record, path)


# --- the drain ---------------------------------------------------------------------


def _log(cls: type[Injector] | None, key: str, stage: str, detail: str) -> None:
    prefix = cls.stage_prefix if cls is not None else "LIB"
    aliases = cls.stage_aliases if cls is not None else None
    write_stage_line(stage_name(prefix, stage, aliases), key, detail)


def _ledgers(registry: Mapping[str, type[Injector]]) -> list[str]:
    return sorted({"attempts", *(cls.ledger for cls in registry.values())})


def held_records(
    key: str | None = None,
    *,
    root: Path | None = None,
    injectors: Mapping[str, type[Injector]] | None = None,
) -> list[dict[str, Any]]:
    """Every ``HELD`` record (for ``key``, or for every key), oldest hold first,
    across every ledger a registered injector writes. Each carries ``_ledger``."""
    found: list[dict[str, Any]] = []
    for ledger in _ledgers(injectors if injectors is not None else _registry()):
        store = AttemptStore(root, ledger=ledger)
        for record in store.read(key) if key is not None else store.all():
            if record.get("state") == "HELD":
                record["_ledger"] = ledger
                found.append(record)
    return sorted(
        found, key=lambda r: (int(r.get("held_at") or 0), str(r.get("armed_at", "")))
    )


@contextlib.contextmanager
def _locked(root: Path) -> Iterator[None]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / _LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_EX)
        elif _msvcrt is not None:
            _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)
        yield
    finally:
        os.close(fd)


def _transition(
    record: dict[str, Any], root: Path, state: str, **changes: Any
) -> dict[str, Any] | None:
    """Move a HELD record to ``state`` under the boundary lock, or ``None`` when a
    concurrent drain already took it. Runs BEFORE anything is typed, so two
    drains can never deliver the same record twice."""
    store = AttemptStore(root, ledger=record["_ledger"])
    with _locked(root):
        current = next(
            (
                r
                for r in store.read(str(record["key"]))
                if r.get("attempt_id") == record["attempt_id"]
            ),
            None,
        )
        if current is None or current.get("state") != "HELD":
            return None
        current.pop("_path", None)
        current.update(state=state, terminal_at=int(time.time()), **changes)
        path = store.write(current)
    current["_ledger"] = record["_ledger"]
    current["_path"] = str(path)
    return current


def _drop(
    record: dict[str, Any], cls: type[Injector] | None, root: Path, reason: str
) -> InjectionOutcome | None:
    dropped = _transition(record, root, "DROPPED", reason=reason)
    if dropped is None:
        return None
    _log(
        cls,
        str(record["key"]),
        "DROP",
        f"reason={reason} attempt={record['attempt_id']}",
    )
    return InjectionOutcome(
        attempt_id=str(record["attempt_id"]),
        use_case=str(record.get("use_case")),
        state="DROPPED",
        landing=None,
        reason=reason,
        staged=False,
        submitted=False,
        evidence={"attempt": Path(dropped["_path"])},
    )


def _held_payload(record: dict[str, Any]) -> Payload:
    mode = record.get("payload_mode") or "type"
    if record.get("payload_kind") == "slash-command":
        if record.get("use_case") == "slack-reply":
            text = str(record["payload_text"])
            args = str(record.get("payload_args") or "").strip()
            return Payload(
                text,
                "slash-command",
                record["payload_command"],
                None,
                sha256(text),
                sha256(args) if args else None,
                mode,
            )
        return SlashCommandPayload(
            str(record["payload_command"]), record.get("payload_args") or "", mode=mode
        )
    return PromptPayload(
        str(record["payload_text"]), record.get("payload_sentinel"), mode=mode
    )


def _held_target(record: dict[str, Any], tmux_command: _tmux.Command) -> SessionTarget:
    """The recorded target when the record carries all of it (verify()'s rebuild);
    otherwise the live pane resolved by id, refused if its fingerprint moved.
    Raises :class:`TargetUnresolved`."""
    key = str(record["key"])
    pane, fingerprint = record.get("pane_id"), record.get("pane_fingerprint")
    path = record.get("transcript_path")
    if pane and fingerprint and path:
        cwd = record.get("cwd")
        return SessionTarget(
            key=key,
            pane_id=str(pane),
            fingerprint=str(fingerprint),
            transcript_path=Path(path),
            cwd=Path(cwd) if cwd else None,
            transcript_source=record.get("transcript_source"),
            session_name=record.get("session_name"),
            cc_version=record.get("cc_version"),
        )
    live = resolve_target(
        f"pane:{pane}" if pane else f"key:{key}",
        key=key,
        transcript_path=Path(path) if path else None,
        tmux_command=tmux_command,
    )
    if fingerprint and live.fingerprint != fingerprint:
        raise TargetUnresolved("no-pane", "pane fingerprint changed since the hold")
    return live


def _refusal(injector: Injector, target: SessionTarget, payload: Payload) -> str | None:
    config = load_config()
    for check in injector.preflights():
        verdict = check.check(target, payload, config)
        if not verdict:
            return str(verdict.reason)
    return None


def _ready(
    record: dict[str, Any], cls: type[Injector], current: float
) -> tuple[Injector, SessionTarget, Payload] | str:
    """The injector, target and payload to re-deliver, or the drop reason."""
    held_at = int(record.get("held_at") or 0)
    if current - held_at > int(record.get("ttl_seconds") or 0):
        return "stale"
    command = tuple(record.get("tmux_command") or _tmux.DEFAULT_COMMAND)
    injector = cls(tmux_command=command, **(record.get("hold_options") or {}))
    try:
        target = _held_target(record, command)
    except TargetUnresolved as error:
        return f"preflight:{error.reason}"
    payload = _held_payload(record)
    refused = _refusal(injector, target, payload)
    if refused is not None:
        return f"preflight:{refused}"
    # A consumer may answer with the documented ``(bool, detail)`` or with a bare
    # bool (``LaunchResumeInjector`` does); an unpack error here would turn a
    # relevance verdict into a drain crash, so both shapes are read.
    verdict = injector.still_relevant(record)
    if isinstance(verdict, tuple):
        relevant, detail = verdict
    else:
        relevant, detail = bool(verdict), "relevance-hook"
    if not relevant:
        return f"irrelevant:{detail}"
    return injector, target, payload


def _drain_one(
    record: dict[str, Any],
    registry: Mapping[str, type[Injector]],
    root: Path,
    current: float,
) -> InjectionOutcome | None:
    cls = registry.get(str(record.get("use_case")))
    if cls is None:
        return _drop(record, None, root, "unknown-use-case")
    ready = _ready(record, cls, current)
    if isinstance(ready, str):
        return _drop(record, cls, root, ready)
    injector, target, payload = ready
    released = _transition(record, root, "RELEASED")
    if released is None:
        return None
    held_s = int(current) - int(record.get("held_at") or 0)
    _log(cls, target.key, "RELEASE", f"attempt={record['attempt_id']} held_s={held_s}")
    released.pop("_path", None)
    released.pop("_ledger", None)
    store = AttemptStore(root, ledger=record["_ledger"])
    injector.draining = True
    try:
        outcome = injector.run(target, payload)
    except Exception as error:  # noqa: BLE001 - one record's fault never strands the rest
        # Whether an Enter went is unknowable, so the record stays RELEASED: a
        # second delivery could double-submit.
        name = type(error).__name__
        store.write({**released, "release_error": name})
        detail = f"attempt={record['attempt_id']} error={name}"
        _log(cls, target.key, "DRAIN-ERROR", detail)
        return None
    store.write({**released, "released_attempt_id": outcome.attempt_id})
    return outcome


def drain(
    key: str,
    *,
    root: Path | None = None,
    now: float | None = None,
    injectors: Mapping[str, type[Injector]] | None = None,
) -> list[InjectionOutcome]:
    """Re-deliver or drop every ``HELD`` record for ``key``, oldest first.

    While :func:`in_boundary` still holds, nothing is delivered and one
    ``CC-<PREFIX>-DRAIN-DEFERRED`` line is written. Otherwise each record is
    dropped (``DROPPED``, ``CC-<PREFIX>-DROP reason=stale|preflight:<r>|
    irrelevant:<d>``) or released (``RELEASED`` with ``released_attempt_id``,
    ``CC-<PREFIX>-RELEASE``) and run through its registered injector. Returns one
    outcome per record this call decided; a record another drain took is skipped.
    """
    registry = injectors if injectors is not None else _registry()
    base = root or resume_root()
    held = held_records(key, root=base, injectors=registry)
    if not held:
        return []
    boundary = in_boundary(key, root=base, now=now)
    if boundary is not None:
        first = registry.get(str(held[0].get("use_case")))
        _log(first, key, "DRAIN-DEFERRED", f"held={len(held)} {_where(boundary)}")
        return []
    current = time.time() if now is None else float(now)
    outcomes = [_drain_one(record, registry, base, current) for record in held]
    return [outcome for outcome in outcomes if outcome is not None]
