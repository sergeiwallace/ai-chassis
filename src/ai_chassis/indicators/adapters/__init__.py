"""Adapters registered by name (design § Adding a source).

``sessions`` runs first and feeds every other adapter's context; the rest run in this order,
each isolated, so one that fails is recorded and the others still emit.
"""

from __future__ import annotations

from ai_chassis.indicators.adapters import attempts, checkins, session_reload
from ai_chassis.indicators.adapters.sessions import discover as discover_sessions
from ai_chassis.indicators.model import Adapter

SESSIONS = "sessions"
ADAPTERS: dict[str, Adapter] = {
    "session-reload": session_reload.project,
    "attempts": attempts.project,
    "goal-waker": checkins.project,
}

__all__ = ["ADAPTERS", "SESSIONS", "discover_sessions"]
