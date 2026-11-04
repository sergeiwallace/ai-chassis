"""One JSON file per session under ``<state>/session-reload/sessions/``, written atomically.

Per target it holds the build last seen stale, its state (``pending``, ``verified``,
``held-cache``, ``gave-up``), the attempt count and the last result (design ISSUE-REF §
Per-session ledger and state machine).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ai_chassis import goal_waker
from ai_chassis.generations import write_json_fsync as write_json

PENDING = "pending"
VERIFIED = "verified"
HELD_CACHE = "held-cache"
GAVE_UP = "gave-up"

#: A gone session's ledger is kept this long, then swept by the pass.
KEEP_SECONDS = 7 * 24 * 60 * 60


def state_dir() -> Path:
    """``$XDG_STATE_HOME/ai-harness/session-reload``, beside the update restart's."""
    return goal_waker.state_dir().parent / "session-reload"


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class SessionLedger:
    def __init__(self, root: Path, session_id: str) -> None:
        self.path = root / "sessions" / f"{session_id}.json"
        self.data = read_json(self.path)
        self.data.setdefault("session_id", session_id)
        self.data.setdefault("targets", {})

    def bind(self, pid: int, proc_start: str, now: float) -> None:
        """A different process under the same session id loaded every build afresh."""
        if (self.data.get("pid"), self.data.get("proc_start")) != (pid, proc_start):
            self.data.update(pid=pid, proc_start=proc_start, targets={})
        self.data.setdefault("first_seen", now)

    def entry(self, target: str) -> dict[str, Any]:
        return self.data["targets"].setdefault(target, {})

    def observe(self, target: str, build: str, label: str, now: float) -> bool:
        """Record that ``build`` is wanted; True when it is a new build, which starts a
        fresh ``pending`` entry with the attempt budget reset."""
        entry = self.entry(target)
        if entry.get("build") == build:
            return False
        entry.clear()
        entry.update(build=build, label=label, state=PENDING, attempts=0, since=now)
        return True

    def save(self) -> None:
        write_json(self.path, self.data)


def sweep(root: Path, live: set[str], now: float | None = None) -> list[str]:
    """Remove the ledgers of sessions not live whose file is older than KEEP_SECONDS."""
    moment = time.time() if now is None else now
    removed: list[str] = []
    for path in (root / "sessions").glob("*.json"):
        if path.stem in live:
            continue
        try:
            old = moment - path.stat().st_mtime > KEEP_SECONDS
        except OSError:
            continue
        if old:
            path.unlink(missing_ok=True)
            removed.append(path.stem)
    return removed
