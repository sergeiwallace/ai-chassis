"""Detectors: is what a live session loaded older than what is installed now?

A kind answers that for one target and one session. Kinds register by name in
:data:`KINDS`, so a new reload system is one class here (or in a test module) plus a
config block, with no change to the pass (design ISSUE-REF D-3).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from ai_chassis.prompt_injection.config import claude_root
from ai_chassis.session_reload.config import Target

STALE = "stale"
CURRENT = "current"
NOT_LOADED = "not-loaded"
NOT_INSTALLED = "not-installed"
UNKNOWN = "unknown"

Emit = Callable[..., None]


@dataclass(frozen=True)
class LiveSession:
    """One live interactive session joined to its validated registry record."""

    session_id: str
    pid: int
    name: str | None
    transcript: Path
    record: Mapping[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.name or self.session_id

    @property
    def proc_start(self) -> str:
        return str(self.record.get("procStart", "")).strip()

    @property
    def cwd(self) -> str:
        value = self.record.get("cwd")
        return value if isinstance(value, str) else ""

    @property
    def pane(self) -> str | None:
        """The pane id from the registry's ``tmux`` field, ``<session>:@<window>.%<pane>``."""
        value = self.record.get("tmux")
        if not isinstance(value, str) or ":" not in value or "." not in value:
            return None
        pane = value.rpartition(".")[2]
        return pane if pane.startswith("%") else None

    @property
    def started_at(self) -> float | None:
        value = self.record.get("startedAt")
        return value / 1000 if isinstance(value, (int, float)) else None


@dataclass(frozen=True)
class Build:
    identity: str
    label: str
    path: Path | None = None
    updated: float | None = None


@dataclass(frozen=True)
class Verdict:
    state: str
    loaded: Build | None = None
    installed: Build | None = None
    source: str = ""

    @property
    def stale(self) -> bool:
        return self.state == STALE


class Kind:
    """One detector instance per tick: it sees the tick's whole session population."""

    name: ClassVar[str] = ""
    #: Target fields this kind cannot work without; the resolver drops a target lacking one.
    required: ClassVar[tuple[str, ...]] = ()
    #: Whether the ``refresh`` host action applies to this kind's targets.
    refreshable: ClassVar[bool] = False

    def __init__(self, sessions: Sequence[LiveSession], emit: Emit) -> None:
        self.sessions = sessions
        self.emit = emit

    def installed(self, target: Target, session: LiveSession | None) -> Build | None:
        raise NotImplementedError

    def verdict(
        self, target: Target, session: LiveSession, entry: Mapping[str, Any]
    ) -> Verdict:
        raise NotImplementedError

    def reread(self) -> None:
        """Forget what this tick read, so the next verdict reads the disk again."""


KINDS: dict[str, type[Kind]] = {}


def register_kind(cls: type[Kind]) -> type[Kind]:
    if not cls.name:
        raise ValueError(f"{cls.__name__} has no name")
    KINDS[cls.name] = cls
    return cls


def _epoch(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _contains(root: str, path: str) -> bool:
    root = root.rstrip("/")
    return bool(root) and (path == root or path.startswith(root + "/"))


@dataclass(frozen=True)
class _Marker:
    build_dir: Path
    mtime: float


@register_kind
class PluginKind(Kind):
    """A Claude Code plugin, by the ``.in_use/<pid>`` load markers Claude Code writes in
    each cached build directory for every process that loaded it (design fact 1, D-1).
    The newest marker whose ``pid`` and ``procStart`` match the session's registry
    record names the build that process runs now."""

    name = "plugin"
    required = ("plugin",)
    refreshable = True

    def __init__(self, sessions: Sequence[LiveSession], emit: Emit) -> None:
        super().__init__(sessions, emit)
        self._registry: dict[str, Any] | None = None
        self._markers: dict[Path, dict[int, list[tuple[_Marker, dict[str, Any]]]]] = {}
        self._degraded: set[str] = set()

    def reread(self) -> None:
        self._registry = None
        self._markers = {}

    def _entries(self, plugin: str) -> list[dict[str, Any]]:
        if self._registry is None:
            path = claude_root() / "plugins" / "installed_plugins.json"
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                value = {}
            self._registry = value if isinstance(value, dict) else {}
        plugins = self._registry.get("plugins")
        entries = plugins.get(plugin) if isinstance(plugins, dict) else None
        return [e for e in entries or [] if isinstance(e, dict)]

    def installed(self, target: Target, session: LiveSession | None) -> Build | None:
        """The entry that applies to the session's cwd: the most specific ``project``
        entry whose ``projectPath`` contains it, else the ``user`` entry."""
        entries = self._entries(str(target.options["plugin"]))
        cwd = session.cwd if session is not None else ""
        projects = [
            e
            for e in entries
            if e.get("scope") == "project"
            and isinstance(e.get("projectPath"), str)
            and _contains(e["projectPath"], cwd)
        ]
        users = [e for e in entries if e.get("scope") == "user"]
        chosen = max(projects, key=lambda e: len(e["projectPath"]), default=None)
        chosen = chosen or (users[0] if users else None)
        if chosen is None or not isinstance(chosen.get("installPath"), str):
            return None
        sha = chosen.get("gitCommitSha")
        sha = sha if isinstance(sha, str) else ""
        updated = _epoch(chosen.get("lastUpdated"))
        version = str(chosen.get("version") or Path(chosen["installPath"]).name)
        return Build(
            identity=f"{chosen['installPath']}#{sha or chosen.get('lastUpdated')}",
            label=f"{version} {sha[:7]}".strip(),
            path=Path(chosen["installPath"]),
            updated=updated,
        )

    def _cache(self, cache_root: Path) -> dict[int, list[tuple[_Marker, dict]]]:
        if cache_root not in self._markers:
            found: dict[int, list[tuple[_Marker, dict]]] = {}
            for marker in cache_root.glob("*/.in_use/*"):
                try:
                    body = json.loads(marker.read_text(encoding="utf-8"))
                    mtime = marker.stat().st_mtime
                    pid = int(marker.name)
                except (OSError, ValueError):
                    continue
                if isinstance(body, dict):
                    found.setdefault(pid, []).append(
                        (_Marker(marker.parent.parent, mtime), body)
                    )
            self._markers[cache_root] = found
        return self._markers[cache_root]

    def loaded(self, session: LiveSession, cache_root: Path) -> _Marker | None:
        """The newest marker this exact process wrote; a reused pid's is ignored."""
        matching = [
            marker
            for marker, body in self._cache(cache_root).get(session.pid, [])
            if str(body.get("pid")) == str(session.pid)
            and str(body.get("procStart", "")).strip() == session.proc_start
        ]
        return max(matching, key=lambda m: m.mtime, default=None)

    def verdict(
        self, target: Target, session: LiveSession, entry: Mapping[str, Any]
    ) -> Verdict:
        installed = self.installed(target, session)
        if installed is None or installed.path is None:
            return Verdict(NOT_INSTALLED, source="installed-plugins")
        cache_root = installed.path.parent
        marker = self.loaded(session, cache_root)
        if marker is not None:
            loaded = Build(
                str(marker.build_dir), marker.build_dir.name, marker.build_dir
            )
            behind = marker.build_dir != installed.path or (
                installed.updated is not None and marker.mtime < installed.updated
            )
            return Verdict(
                STALE if behind else CURRENT, loaded, installed, "in-use-marker"
            )
        if any(self.loaded(other, cache_root) for other in self.sessions):
            return Verdict(NOT_LOADED, None, installed, "in-use-marker")
        return self._fallback(target, session, entry, installed)

    def _fallback(
        self,
        target: Target,
        session: LiveSession,
        entry: Mapping[str, Any],
        installed: Build,
    ) -> Verdict:
        """No live session has a marker, so the format has drifted: stale when the
        process started before the install and no reload of this build is on record."""
        if target.name not in self._degraded:
            self._degraded.add(target.name)
            self.emit("SR-DEGRADED", target=target.name, reason="no-in-use-marker")
        reloaded = entry.get("build") == installed.identity and (
            entry.get("state") == "verified" or entry.get("last_result") == "reloaded"
        )
        started = session.started_at
        behind = (
            started is not None
            and installed.updated is not None
            and started < installed.updated
            and not reloaded
        )
        return Verdict(
            STALE if behind else CURRENT, None, installed, "start-time-fallback"
        )
