"""The goal waker's ``to-review-reminder`` marker kind (ISSUE-REF D-2, T-3.4).

Design: docs/designs/issue-ref-to-review-mode-and-quota-gate.md § Resurfacing the message,
"Periodic reminder". A session that stays in to-review mode and idle is reminded on the waker's
tick at most once per ``interval_minutes``. The reminder is a model turn, so it is issued only
when all five conditions hold, and re-validated at every later tick and again at claim time
(``scripts/to_review_hook.py reminder-check``, run by ``goal-waker-rewake.sh``):

1. the session's to-review hook cache reads mode ``to-review``;
2. no goal is armed;
3. it has been idle for at least ``interval_minutes`` since its last turn end or reminder;
4. ``idle_wake`` admits automatic wakes (the fleet's one wake brake);
5. ``quota_gate.decide("to_review.idle_reminder")`` allows.

The conditions themselves live in ``scripts/to_review_state.py`` (``reminder_conditions``), which
``scripts/to_review_hook.py`` also uses, so the issue, reoffer and claim checks are one rule. Only
that module is loaded here, never the hook: the waker is reachable from ``worktree_audit.py``, so
the hook's classifier, held and Beads dependencies would join the reaper's authorising closure. This module adds the marker lifecycle: at most one
pending marker per session, none while a ``compact-rearm`` or ``save-state`` marker is pending,
removal when a condition fails or the marker outlives its interval, and the marker's own body for
the typed fallback. It never applies the goal ``epoch_offset`` gate.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

KIND = "to-review-reminder"
_STATE = Path(__file__).resolve().parents[2] / "scripts" / "to_review_state.py"


def _state() -> Any:
    name = "ai_harness_to_review_state"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _STATE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def conditions(slug: str | None, now: float) -> tuple[bool, str, dict[str, Any]]:
    """The five issue conditions for the session named ``slug``."""
    if not slug:
        return False, "no session name", {}
    try:
        return _state().reminder_conditions(slug, now)
    except Exception as error:  # noqa: BLE001 - an unread condition is a failed one
        return False, f"conditions unreadable: {type(error).__name__}: {error}", {}


def _pending(run: Any, session_id: str) -> list[tuple[Path, dict[str, str]]]:
    from ai_chassis.goal_waker import read_marker  # noqa: PLC0415 - that module imports this

    found = []
    if not run.store.wake.is_dir():
        return found
    for marker in sorted(run.store.wake.glob(f"goal-nudge-{session_id}-*.marker")):
        try:
            fields, _ = read_marker(marker)
        except OSError:
            continue
        found.append((marker, fields))
    return found


def _remove(run: Any, marker: Path, session: str, reason: str) -> None:
    if not run.dry_run:
        from ai_chassis.goal_waker import marker_completed

        try:
            os.replace(marker, run.store.expired / marker.name)
        except OSError:
            marker.unlink(missing_ok=True)
        marker_completed(marker)
    run.emit("GW-TO-REVIEW-REMOVED", session=session, nudge=marker.name, reason=reason)


def maybe_issue(run: Any, session: Any) -> None:
    """The issue rule, once per session per tick; records why when it writes nothing."""
    from ai_chassis.goal_waker import issue  # noqa: PLC0415 - that module imports this

    label = session.label
    state_module = _state()
    if not session.name or not state_module.cache_path(session.name).is_file():
        return  # never classified by the to-review Stop hook: not a candidate
    pending = _pending(run, session.session_id)
    if any(f.get("kind") in ("compact-rearm", "save-state") for _, f in pending):
        for marker, fields in pending:
            if fields.get("kind") == KIND:
                _remove(
                    run,
                    marker,
                    label,
                    "a compact-rearm or save-state marker is pending",
                )
        return
    if any(f.get("kind") == KIND for _, f in pending):
        return
    ok, why, details = conditions(session.name, run.now)
    if not ok:
        run.emit("GW-TO-REVIEW-SKIP", session=label, reason=why)
        return
    state = state_module.read_json(state_module.reminder_path(session.name))
    if run.now - float(state.get("last_issued_at") or 0) < details["interval"] * 60:
        run.emit(
            "GW-TO-REVIEW-SKIP", session=label, reason="issued within the interval"
        )
        return
    nonce = "dry-run"
    if not run.dry_run:
        marker = issue(
            session,
            details["body"],
            run.store.root,
            epoch_offset=None,
            kind=KIND,
            now=run.now,
        )
        nonce = marker.name
        state["last_issued_at"] = run.now
        state_module.write_json_atomic(state_module.reminder_path(session.name), state)
    run.emit("GW-TO-REVIEW-ISSUE", session=label, nudge=nonce)


def handle_marker(
    run: Any, marker: Path, session: Any, fields: dict[str, str], age: float
) -> None:
    """A pending reminder at a later tick: remove it when stale or a condition fails, else
    reoffer it once a delivery was refused, else type its own body after the fallback delay."""
    label = session.label if session else fields.get("session_id", "unknown")
    if session is None:
        _remove(run, marker, label, "no live session")
        return
    ok, why, details = conditions(session.name, run.now)
    if not ok:
        _remove(run, marker, label, why)
        return
    if age >= details["interval"] * 60:
        _remove(run, marker, label, "older than the reminder interval")
        return
    if run.reoffer(marker, session, fields):
        return
    if age >= run.config["fallback_after_seconds"]:
        _, body = _read_body(marker)
        run.typed(
            marker,
            session.session_id,
            _nonce(marker),
            session.name,
            compact=lambda: body,
        )


def _read_body(marker: Path) -> tuple[dict[str, str], str]:
    from ai_chassis.goal_waker import read_marker  # noqa: PLC0415 - that module imports this

    return read_marker(marker)


def _nonce(marker: Path) -> str:
    from ai_chassis.goal_waker import _MARKER_NAME  # noqa: PLC0415 - that module imports this

    match = _MARKER_NAME.match(marker.name)
    return match["nonce"] if match else ""


def reoffer_eligible(run: Any, session: Any) -> bool:
    """The reoffer recheck: the same five conditions, never the goal epoch gate."""
    ok, _why, _details = conditions(session.name, run.now)
    return ok
