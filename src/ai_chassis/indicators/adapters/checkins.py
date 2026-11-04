"""The ``goal-waker`` adapter: each live session's next goal check-in.

Reads the waker's per-session state (``goal-waker/sessions/<sessionId>.json``) and its resolved
config. Whether a session has an active goal is the waker's own verdict from its last tick: a
goal gate refusal (``no-goal``, ``goal-ended`` ...) recorded as ``last_skip_reason`` means none.
The projector never re-reads a transcript to decide it.

``fires_at`` is the earliest moment the waker's cadence allows the next nudge: the waker
measures idleness from the transcript's last write, and nudges once that idle time reaches
``min(idle_after_seconds * backoff_factor ** nudges_in_epoch, max_backoff_seconds)``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from ai_chassis.indicators.model import (
    AdapterContext,
    AdapterResult,
    Entry,
    LiveSession,
    from_epoch,
)
from ai_chassis.indicators.reasons import blocked_on

# goal_waker._GOAL_GATES' refusal reasons, plus `undetermined`, which the waker records for
# an unreadable goal as well as an unreadable idle state, and `budget`: the epoch's nudges are
# spent, so no check-in is due until the goal's next epoch.
_NO_ACTIVE_GOAL = frozenset(
    {
        "no-goal",
        "goal-ended",
        "not-qualifying",
        "evaluator-loop",
        "undetermined",
        "budget",
    }
)


def _transcript_mtime(home: Path, session: LiveSession) -> float | None:
    from ai_chassis.context_supervisor import _slug  # noqa: PLC0415 - heavy module

    if not session.cwd:
        return None
    path = (
        home
        / ".claude"
        / "projects"
        / _slug(session.cwd)
        / f"{session.session_id}.jsonl"
    )
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def project(context: AdapterContext) -> AdapterResult:
    from ai_chassis import goal_waker  # noqa: PLC0415 - the waker imports this package

    config = goal_waker.load_config(goal_waker.live_config_path())
    result = AdapterResult()
    if not config["enabled"]:
        result.status = {
            "ok": True,
            "enabled": False,
            "reason": config["_error"] or "disabled",
        }
        return result
    root = context.state_home / "goal-waker" / "sessions"
    for session in context.sessions.values():
        try:
            state = json.loads(
                (root / f"{session.session_id}.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(state, dict):
            entry = _entry(session, state, config, context)
            if entry is not None:
                result.add(session.session_id, entry)
    result.status = {"ok": True, "enabled": True}
    return result


def _entry(
    session: LiveSession,
    state: dict[str, Any],
    config: dict[str, Any],
    context: AdapterContext,
) -> Entry | None:
    reason = state.get("last_skip_reason")
    reason = reason if isinstance(reason, str) else None
    last_nudge = _number(state.get("last_nudge_at"))
    if reason in _NO_ACTIVE_GOAL or (reason is None and last_nudge is None):
        return None
    count = int(_number(state.get("nudges_in_epoch")) or 0)
    if count >= config["max_nudges_per_epoch"]:
        return None
    reference = _transcript_mtime(context.home, session) or last_nudge
    if reference is None:
        return None
    threshold = min(
        config["idle_after_seconds"] * config["backoff_factor"] ** count,
        config["max_backoff_seconds"],
    )
    blocked = blocked_on(reason)
    detail = {"nudges_in_epoch": str(count), "threshold_seconds": str(int(threshold))}
    if last_nudge is not None:
        detail["last_nudge_at"] = str(int(last_nudge))
    since = from_epoch(reference)
    return Entry(
        id="check-in:goal-waker",
        kind="check-in",
        mechanism="goal-waker",
        label="goal check-in",
        command="prompt",
        state="waiting" if blocked is not None else "armed",
        blocked_on=blocked,
        reason=reason,
        since=since,
        next_check_at=None,
        fires_at=since + timedelta(seconds=threshold),
        detail=detail,
    )
