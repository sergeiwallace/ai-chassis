"""The projector's data model (design § Data Model)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ai_chassis.indicators.config import ResolvedConfig

Kind = Literal["injection", "reload", "check-in"]
State = Literal[
    "armed",
    "waiting",
    "held",
    "queued",
    "firing",
    "fired",
    "verified",
    "gave-up",
    "refused",
]
BlockedOn = Literal[
    "draft", "busy", "question", "boundary", "backoff", "not-ready", "other"
]

#: Results shown once for ``terminal_show_seconds`` after ``since``, then dropped.
TERMINAL_STATES: frozenset[str] = frozenset({"verified", "gave-up", "refused"})


def iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_epoch(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


@dataclass(frozen=True)
class Entry:
    id: str  # stable per (session, mechanism, target): "reload:beads-ui"
    kind: Kind
    mechanism: str  # use case or framework name: "session-reload", "idle-nudge"
    label: str  # what the band says
    command: str  # the slash command typed, or "prompt"
    state: State
    blocked_on: BlockedOn | None
    reason: str | None  # the raw reason, for the pane
    since: datetime
    next_check_at: datetime | None
    fires_at: datetime | None
    detail: Mapping[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "mechanism": self.mechanism,
            "label": self.label,
            "command": self.command,
            "state": self.state,
            "blocked_on": self.blocked_on,
            "reason": self.reason,
            "since": iso(self.since),
            "next_check_at": iso(self.next_check_at),
            "fires_at": iso(self.fires_at),
            "detail": dict(sorted(self.detail.items())),
        }


@dataclass(frozen=True)
class LiveSession:
    session_id: str
    name: str
    pid: int
    cwd: str


@dataclass(frozen=True)
class AdapterContext:
    now: datetime
    config: ResolvedConfig
    sessions: Mapping[str, LiveSession]  # sessionId -> live session
    state_home: Path  # $XDG_STATE_HOME/ai-harness
    resume_root: Path  # ~/.claude/resume
    home: Path

    def session_named(self, name: str | None) -> LiveSession | None:
        if not name:
            return None
        return next((s for s in self.sessions.values() if s.name == name), None)


@dataclass
class AdapterResult:
    entries: dict[str, list[Entry]] = field(default_factory=dict)
    targets: dict[str, dict[str, Any]] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=lambda: {"ok": True})

    def add(self, session_id: str, entry: Entry) -> None:
        self.entries.setdefault(session_id, []).append(entry)


Adapter = Callable[[AdapterContext], AdapterResult]
