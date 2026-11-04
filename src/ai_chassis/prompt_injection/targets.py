"""Which live session to inject into, measured at call time and never cached.

Spec grammar: ``self`` | ``session:<name>`` | ``pane:%N`` | ``key:<key>``. A
target that does not resolve is never a target: resolution raises
``TargetUnresolved(reason)`` and nothing is typed.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_chassis.prompt_injection import _tmux
from ai_chassis.prompt_injection.config import claude_root, resume_root, safe_component
from ai_chassis.prompt_injection.observe import AttemptStore


class TargetUnresolved(Exception):  # noqa: N818 - public name fixed by the design
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class SessionTarget:
    key: str
    pane_id: str
    fingerprint: str
    transcript_path: Path | None
    cwd: Path | None
    transcript_source: str | None = None
    session_name: str | None = None
    cc_version: str | None = None
    is_self: bool = False


def slug(path: str) -> str:
    # Copied from context_supervisor._slug (2646-2648).
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def newest_cwd_transcript(cwd: Path) -> Path | None:
    # Copied from context_supervisor._resolve_own_transcript_path (2651-2659), with
    # the cwd as a parameter instead of the calling process's.
    project_dir = claude_root() / "projects" / slug(str(cwd))
    if not project_dir.is_dir():
        return None
    candidates = sorted(
        project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


@functools.lru_cache(maxsize=8)
def _claude_version_at(binary: str) -> str | None:
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"\s*(\d+\.\d+\.\d+)", result.stdout)
    return match.group(1) if result.returncode == 0 and match else None


def claude_version() -> str | None:
    """``claude --version`` on this host, read once per binary per process."""
    binary = shutil.which("claude")
    return _claude_version_at(binary) if binary else None


def session_records() -> list[dict[str, Any]]:
    """``~/.claude/sessions/*.json``, newest ``updatedAt`` first."""
    records: list[dict[str, Any]] = []
    for path in (claude_root() / "sessions").glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return sorted(records, key=lambda r: int(r.get("updatedAt") or 0), reverse=True)


def _record_pane(record: dict[str, Any]) -> str | None:
    # The record's `tmux` field is `<session>:@<window>.%<pane>` (measured 2026-10-10).
    value = record.get("tmux")
    if not isinstance(value, str) or ".%" not in value:
        return None
    return value.rsplit(".", 1)[1]


def _record_transcript(record: dict[str, Any]) -> Path | None:
    session_id, cwd = record.get("sessionId"), record.get("cwd")
    if not isinstance(session_id, str) or not isinstance(cwd, str):
        return None
    return claude_root() / "projects" / slug(cwd) / f"{session_id}.jsonl"


def _newest_attempt(key: str) -> dict[str, Any] | None:
    """The newest ``attempts/`` record for ``key``, read through ``AttemptStore`` so the
    file-name mapping and glob escaping are the writer's own (ISSUE-REF)."""
    records = AttemptStore(resume_root()).read(key)
    return records[0] if records else None


def _boot_stamp(key: str) -> dict[str, Any] | None:
    """The reboot-resume stamp for ``key`` (``boot-stamps/<key>.json``, written every
    few minutes by reboot_resume's ``stamp``), the design's second ``key:`` source.
    Its transcript is that module's newest-in-cwd pick, so it is recorded as
    ``cwd-newest``, never as a session record."""
    if not key or "/" in key or "\\" in key or ".." in key:
        return None
    path = resume_root() / "boot-stamps" / f"{safe_component(key)}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


class TargetResolver(ABC):
    def __init__(self, tmux_command: _tmux.Command = _tmux.DEFAULT_COMMAND) -> None:
        self.tmux_command = tmux_command

    @abstractmethod
    def resolve(self, spec: str, **hints: Any) -> SessionTarget: ...

    def _bind_pane(
        self,
        pane_id: str,
        *,
        key: str | None,
        transcript_path: Path | None,
        transcript_source: str | None,
        record: dict[str, Any] | None,
    ) -> SessionTarget:
        if _tmux.server_version(self.tmux_command) is None:
            raise TargetUnresolved("no-tmux")
        fingerprint = _tmux.fingerprint(self.tmux_command, pane_id)
        if fingerprint is None:
            raise TargetUnresolved("no-pane", pane_id)
        cwd_text = _tmux.display(self.tmux_command, pane_id, "#{pane_current_path}")
        cwd = Path(cwd_text) if cwd_text else None
        if record is not None and isinstance(record.get("cwd"), str):
            cwd = Path(record["cwd"])
        session_name = _tmux.display(self.tmux_command, pane_id, "#{session_name}")
        if transcript_path is None and cwd is not None:
            transcript_path = newest_cwd_transcript(cwd)
            transcript_source = "cwd-newest" if transcript_path else None
        version = record.get("version") if record else None
        return SessionTarget(
            key=key or (record or {}).get("name") or session_name or pane_id,
            pane_id=pane_id,
            fingerprint=fingerprint,
            transcript_path=transcript_path,
            cwd=cwd,
            transcript_source=transcript_source,
            session_name=session_name,
            cc_version=version if isinstance(version, str) else claude_version(),
        )

    def _from_record(
        self, record: dict[str, Any], key: str | None, transcript_path: Path | None
    ) -> SessionTarget:
        pane = _record_pane(record)
        if pane is None:
            raise TargetUnresolved("no-pane", "session record names no tmux pane")
        hinted = transcript_path is not None
        return self._bind_pane(
            pane,
            key=key,
            transcript_path=transcript_path or _record_transcript(record),
            transcript_source="hook-payload" if hinted else "session-record",
            record=record,
        )


class TmuxPaneResolver(TargetResolver):
    """``self`` (``$TMUX_PANE``) and ``pane:%N``."""

    def resolve(self, spec: str, **hints: Any) -> SessionTarget:
        transcript_path = hints.get("transcript_path")
        is_self = spec == "self"
        if is_self:
            pane = os.environ.get("TMUX_PANE")
            if not pane:
                raise TargetUnresolved("no-pane", "TMUX_PANE is unset")
            key = hints.get("key") or os.environ.get("CLAUDE_CODE_TASK_LIST_ID")
        else:
            pane = spec.removeprefix("pane:")
            key = hints.get("key")
        record = next((r for r in session_records() if _record_pane(r) == pane), None)
        source = None
        if transcript_path is not None:
            source = "hook-payload"
        elif record is not None:
            transcript_path = _record_transcript(record)
            source = "session-record" if transcript_path else None
        target = self._bind_pane(
            pane,
            key=key,
            transcript_path=transcript_path,
            transcript_source=source,
            record=record,
        )
        return dataclasses.replace(target, is_self=True) if is_self else target


class SessionKeyResolver(TargetResolver):
    """``session:<name>`` from the session record; ``key:<key>``, the legacy route."""

    def resolve(self, spec: str, **hints: Any) -> SessionTarget:
        transcript_path = hints.get("transcript_path")
        if spec.startswith("session:"):
            name = spec.removeprefix("session:")
            record = next((r for r in session_records() if r.get("name") == name), None)
            if record is None:
                raise TargetUnresolved("no-session-record", name)
            return self._from_record(record, hints.get("key") or name, transcript_path)
        key = spec.removeprefix("key:")
        recorded = _newest_attempt(key)
        source = "attempt-record"
        if recorded is None or not isinstance(recorded.get("pane_id"), str):
            recorded, source = _boot_stamp(key), "cwd-newest"
        if recorded is not None and isinstance(recorded.get("pane_id"), str):
            path_text = recorded.get("transcript_path")
            path = transcript_path or (Path(path_text) if path_text else None)
            if transcript_path is not None:
                source = "hook-payload"
            return self._bind_pane(
                str(recorded["pane_id"]),
                key=key,
                transcript_path=path,
                transcript_source=source if path else None,
                record=None,
            )
        record = next((r for r in session_records() if r.get("name") == key), None)
        if record is not None:
            return self._from_record(record, key, transcript_path)
        return self._bind_pane(
            self._pane_by_session_name(key),
            key=key,
            transcript_path=transcript_path,
            transcript_source="hook-payload" if transcript_path else None,
            record=None,
        )

    def _pane_by_session_name(self, key: str) -> str:
        result = _tmux.run(
            self.tmux_command, ["list-panes", "-a", "-F", "#{session_name} #{pane_id}"]
        )
        if result is None or result.returncode != 0:
            raise TargetUnresolved("no-tmux")
        matches = [
            pane
            for name, _, pane in (
                line.partition(" ") for line in result.stdout.splitlines()
            )
            if name == key or name.endswith(f"-{key}")
        ]
        if not matches:
            raise TargetUnresolved("no-pane", f"no tmux session named for {key}")
        if len(set(matches)) > 1:
            raise TargetUnresolved("ambiguous-key", ", ".join(sorted(set(matches))))
        return matches[0]


def resolve_target(
    spec: str | None = None,
    *,
    pane_id: str | None = None,
    transcript_path: Path | None = None,
    key: str | None = None,
    tmux_command: _tmux.Command = _tmux.DEFAULT_COMMAND,
) -> SessionTarget:
    """Resolve ``spec`` (or ``pane_id=``) to a live, fingerprinted target."""
    if pane_id is not None:
        spec = f"pane:{pane_id}"
    if not spec:
        raise ValueError("a target spec or pane_id is required")
    hints = {"transcript_path": transcript_path, "key": key}
    if spec == "self" or spec.startswith("pane:"):
        return TmuxPaneResolver(tmux_command).resolve(spec, **hints)
    if spec.startswith(("session:", "key:")):
        return SessionKeyResolver(tmux_command).resolve(spec, **hints)
    raise ValueError(f"unknown target spec: {spec!r}")


def key_for_spec(spec: str) -> str:
    """The attempt key to record when a spec never resolved."""
    if spec == "self":
        return os.environ.get("CLAUDE_CODE_TASK_LIST_ID") or "self"
    for prefix in ("session:", "key:", "pane:"):
        if spec.startswith(prefix):
            return spec.removeprefix(prefix)
    return spec
