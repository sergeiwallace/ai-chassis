"""One projector pass: every adapter, then the ``aih-indicators/v1`` host contract.

The contract is ``$XDG_STATE_HOME/ai-harness/indicators/host.json``, mode 0600, written
atomically, and only when its content other than ``written_at`` and ``next_tick_at`` changed
or on every tenth unchanged pass. ``last-pass.json`` beside it keeps the digest, the unchanged
count and each adapter's last result for ``status``. A pass never raises for a source: a
failing adapter is a ``sources`` row, and a write failure is the report's ``error``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

from ai_chassis.compact_mod_contract import atomic_write_json
from ai_chassis.indicators import adapters
from ai_chassis.indicators.config import ConfigError, ResolvedConfig, defaults, load
from ai_chassis.indicators.model import (
    TERMINAL_STATES,
    AdapterContext,
    Entry,
    LiveSession,
    from_epoch,
    iso,
)

SCHEMA = "aih-indicators/v1"
#: The goal waker's timer period: every two minutes on every OS.
TICK_INTERVAL_SECONDS = 120
REWRITE_EVERY = 10
_VOLATILE = ("written_at", "next_tick_at")


def state_home() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ai-harness"


def contract_path() -> Path:
    return state_home() / "indicators" / "host.json"


def last_pass_path() -> Path:
    return state_home() / "indicators" / "last-pass.json"


def host_profile() -> str | None:
    from ai_chassis import goal_waker  # noqa: PLC0415 - the waker imports this package

    try:
        return goal_waker._machine_identity()
    except Exception:  # noqa: BLE001 - an unknown profile resolves the fleet layers only
        return None


def run_pass(
    *,
    now: float | None = None,
    write: bool = True,
    force: bool = False,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Run every adapter and publish the contract; ``force`` writes even when unchanged."""
    started = time.monotonic()
    moment = time.time() if now is None else now
    profile = host_profile()
    sources: dict[str, Any] = {}
    targets: dict[str, Any] = {}
    sessions: dict[str, Any] = {}
    try:
        config = load(profile, path=config_path)
    except ConfigError as error:
        display = defaults(profile).display()
        sources["config"] = {"ok": False, "error": f"config:{error}"}
    else:
        if not config.enabled:
            return {"enabled": False, "written": False}
        display = config.display()
        live, entries = _collect(config, moment, sources, targets, started)
        sessions = _sessions(live, entries, moment, config)

    contract = {
        "schema": SCHEMA,
        "written_at": iso(from_epoch(moment)),
        "next_tick_at": iso(
            from_epoch(moment) + timedelta(seconds=TICK_INTERVAL_SECONDS)
        ),
        "host_profile": profile,
        "display": display,
        "sources": dict(sorted(sources.items())),
        "targets": dict(sorted(targets.items())),
        "sessions": sessions,
    }
    report: dict[str, Any] = {
        "written": False,
        "path": str(contract_path()),
        "sessions": len(sessions),
        "entries": sum(len(s["entries"]) for s in sessions.values()),
        "sources": contract["sources"],
    }
    if write:
        try:
            report["written"] = _publish(contract, moment, force=force)
        except OSError as error:
            report["error"] = f"write: {type(error).__name__}: {error}"
    return report


def _collect(
    config: ResolvedConfig,
    moment: float,
    sources: dict[str, Any],
    targets: dict[str, Any],
    started: float,
) -> tuple[dict[str, LiveSession], dict[str, list[Entry]]]:
    from ai_chassis.context_supervisor import _root_from_environment  # noqa: PLC0415

    try:
        live, sources[adapters.SESSIONS] = adapters.discover_sessions()
    except Exception as error:  # noqa: BLE001 - isolation is the point
        live = {}
        sources[adapters.SESSIONS] = {
            "ok": False,
            "error": f"{type(error).__name__}: {error}",
        }
    context = AdapterContext(
        now=from_epoch(moment),
        config=config,
        sessions=live,
        state_home=state_home(),
        resume_root=_root_from_environment(),
        home=Path.home(),
    )
    entries: dict[str, list[Entry]] = {}
    for name, adapter in adapters.ADAPTERS.items():
        if time.monotonic() - started >= config.pass_budget_seconds:
            sources[name] = {"ok": False, "error": "budget-exceeded"}
            continue
        try:
            result = adapter(context)
        except Exception as error:  # noqa: BLE001 - isolation is the point
            sources[name] = {"ok": False, "error": f"{type(error).__name__}: {error}"}
            continue
        sources[name] = result.status
        targets.update(result.targets)
        for session_id, rows in result.entries.items():
            entries.setdefault(session_id, []).extend(rows)
    return live, entries


def _sessions(
    live: Mapping[str, LiveSession],
    entries: Mapping[str, list[Entry]],
    moment: float,
    config: ResolvedConfig,
) -> dict[str, Any]:
    """One key per live session with an entry; a shown result drops after its window."""
    out: dict[str, Any] = {}
    for session_id in sorted(entries):
        if session_id not in live:
            continue
        kept = [
            entry
            for entry in entries[session_id]
            if entry.state not in TERMINAL_STATES
            or moment - entry.since.timestamp() <= config.terminal_show_seconds
        ]
        if kept:
            out[session_id] = {
                "name": live[session_id].name,
                "entries": [
                    entry.to_json() for entry in sorted(kept, key=lambda e: e.id)
                ],
            }
    return out


def _digest(contract: Mapping[str, Any]) -> str:
    stable = {key: value for key, value in contract.items() if key not in _VOLATILE}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True).encode("utf-8")
    ).hexdigest()


def read_last_pass() -> dict[str, Any] | None:
    try:
        record = json.loads(last_pass_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _publish(contract: dict[str, Any], moment: float, *, force: bool) -> bool:
    path = contract_path()
    digest = _digest(contract)
    prior = read_last_pass() or {}
    same = prior.get("digest") == digest and path.exists()
    unchanged = int(prior.get("unchanged", 0)) + 1 if same else 0
    rewrite = force or unchanged == 0 or unchanged >= REWRITE_EVERY
    if rewrite:
        atomic_write_json(path, contract)
        unchanged = 0
    atomic_write_json(
        last_pass_path(),
        {
            "pass_at": iso(from_epoch(moment)),
            "digest": digest,
            "unchanged": unchanged,
            "rewrote": rewrite,
            "sources": contract["sources"],
        },
    )
    return rewrite
