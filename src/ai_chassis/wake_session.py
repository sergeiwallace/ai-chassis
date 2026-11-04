"""Per-session watcher ownership in the notifier's shared state directory.

Session and both modes have identical fallback behavior; their distinction records
operator intent. A fresh, live watcher owns its slug; the machine serves all others.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from ai_chassis.agent_mail_notifier import write_if_changed

HEARTBEAT_MAX_AGE_S = 30
WATCH_TICK_S = 2
SLUG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def heartbeat_path(root: Path, slug: str) -> Path:
    if not SLUG_PATTERN.fullmatch(slug):
        raise ValueError("invalid --session slug")
    return root / "session-watchers" / f"{slug}.json"


def refresh_heartbeat(root: Path, slug: str) -> None:
    write_if_changed(
        heartbeat_path(root, slug),
        json.dumps({"pid": os.getpid(), "at": time.time()}) + "\n",
    )


def watcher_live(root: Path, slug: str) -> bool:
    """A PID must still exist and its heartbeat must be at most 30 seconds old."""
    try:
        record = json.loads(heartbeat_path(root, slug).read_text())
        pid, at = record["pid"], record["at"]
        if type(pid) is not int or pid <= 0:
            return False
        age = time.time() - float(at)
        if not 0 <= age <= HEARTBEAT_MAX_AGE_S:
            return False
        os.kill(pid, 0)
    except (OSError, ValueError, TypeError, KeyError):
        return False
    return True
