"""Reload what a live Claude Code session loaded when it goes out of date.

Each registered target names a kind (a detector) and the command that reloads it. Once
per goal-waker tick, :func:`tick` brings each plugin target up to date on disk, finds
the idle sessions whose loaded build is older than the installed one, and types the
reload through the shared prompt-injection library. Design:
``docs/designs/issue-ref-session-reload-framework.md``.
"""

from __future__ import annotations

from ai_chassis.session_reload.runner import tick

__all__ = ["tick"]
