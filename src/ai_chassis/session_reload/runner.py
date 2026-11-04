"""The reload pass, one run per goal-waker tick (design ISSUE-REF § Framework shape).

Refresh each due plugin on disk, then for every live session: ask each enabled target's
kind whether the session is stale, coalesce the stale targets by command (one
``/reload-plugins`` reloads every plugin), gate, type through the library, classify the
result, re-check with the kind, and record it in the session's ledger. Every event is a
``SR-*`` line in ``<state>/session-reload/session-reload.log``, and the tick ends by
rewriting ``status.json`` for an indicator to read.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ai_chassis import context_supervisor, goal_waker
from ai_chassis.session_reload import config, ledger
from ai_chassis.session_reload.actions import (
    DeliveryResult,
    InjectSlashCommand,
    PluginRefresh,
)
from ai_chassis.session_reload.config import Target
from ai_chassis.session_reload.kinds import (
    CURRENT,
    KINDS,
    Kind,
    LiveSession,
    Verdict,
)
from ai_chassis.session_reload.ledger import (
    GAVE_UP,
    HELD_CACHE,
    PENDING,
    VERIFIED,
    SessionLedger,
)

STATUS_SCHEMA = "session-reload-status/v1"
#: The goal waker's timer period, which every tick rides (design fact 7).
TICK_INTERVAL_SECONDS = 120
#: A local command starts no turn (design fact 3), so its result record ends the turn.
_TURN_ENDS = frozenset({"local_command"})
_RESTARTED_PANE = re.compile(r"\bUR-RESTART(?:-FAIL)?\b.*?\bpane=(%\d+)")
_FORCE = " --force"
STATES = frozenset(
    {
        "current",
        "armed",
        "waiting-busy",
        "waiting-draft",
        "waiting-question",
        "waiting-boundary",
        "fired",
        "verified",
        "held-cache",
        "gave-up",
        "not-loaded",
    }
)
_WAIT_STATES = {
    "composer-nonempty": "waiting-draft",
    "composer-suggestion": "waiting-draft",
    "status-waiting": "waiting-question",
    "boundary-held": "waiting-boundary",
    "tick-cap": "armed",
    "backoff": "armed",
}


def _iso(moment: float | None) -> str | None:
    if moment is None:
        return None
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _quote(value: Any) -> str:
    text = str(value)
    return f'"{text}"' if not text or any(c.isspace() for c in text) else text


def wait_state(reason: str) -> str:
    if reason.startswith("transcript-human_gate"):
        return "waiting-question"
    return _WAIT_STATES.get(reason, "waiting-busy")


def restarted_panes(update_restart: dict[str, Any] | None) -> set[str]:
    """The panes the update restart acted on in this same tick (gate 4)."""
    events = (update_restart or {}).get("events") or []
    return {m.group(1) for line in events if (m := _RESTARTED_PANE.search(str(line)))}


def live_sessions(emit) -> list[LiveSession] | None:
    """Live sessions with their validated registry records, by the update restart's own
    inventory; a session with no record waits on its own."""
    from ai_chassis import update_restart  # noqa: PLC0415 - imports goal_waker

    inventory = update_restart._session_inventory()
    if inventory is None:
        return None
    registered, unregistered = inventory
    for session in unregistered:
        emit("SR-WAIT", session=session.label, reason="registry-record-missing")
    return [
        LiveSession(s.session_id, s.pid, s.name, s.transcript, record)
        for s, record in registered
    ]


def compacted_since(session: LiveSession, moment: float) -> bool:
    """A ``compact_boundary`` record newer than ``moment`` in the transcript's tail."""
    snapshot = context_supervisor._transcript_records(session.transcript)
    for record in reversed(snapshot[0] if snapshot else []):
        if (
            record.get("type") == "system"
            and record.get("subtype") == "compact_boundary"
        ):
            stamp = goal_waker._timestamp(record)
            return stamp is not None and stamp > moment
    return False


@dataclass
class _Visit:
    """One session's share of a tick: its ledger, the tick's kinds and its status rows."""

    session: LiveSession
    book: SessionLedger
    kinds: dict[str, Kind]
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)


class _Pass:
    def __init__(
        self,
        *,
        dry_run: bool,
        now: float,
        update_restart: dict[str, Any] | None,
    ) -> None:
        self.dry_run = dry_run
        self.now = now
        self.restarted = restarted_panes(update_restart)
        self.root = ledger.state_dir()
        self.events: list[str] = []
        self.typed = 0
        self.settings: dict[str, Any] = dict(config.DEFAULTS)
        self.profile: str | None = None
        self.action = InjectSlashCommand()
        self.refresher = PluginRefresh()
        self.status_sessions: dict[str, Any] = {}
        self.gates: dict[str, str | None] = {}

    @property
    def next_tick(self) -> float:
        return self.now + TICK_INTERVAL_SECONDS

    def emit(self, event: str, **fields: Any) -> None:
        line = " ".join(
            [_iso(time.time()) or "", event]
            + [f"{key}={_quote(value)}" for key, value in fields.items()]
        )
        self.events.append(line)
        if self.dry_run:
            return
        path = self.root / "session-reload.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def result(self) -> dict[str, Any]:
        return {"events": self.events, "injected": self.typed}

    # -- refresh -------------------------------------------------------------------

    def refresh(self, targets: Sequence[Target]) -> None:
        path = self.root / "refresh.json"
        state = ledger.read_json(path)
        interval = self.settings["refresh_interval_seconds"]
        changed = False
        for target in targets:
            if not KINDS[target.kind].refreshable or not target.options.get("refresh"):
                continue
            last = state.get(target.name)
            at = last.get("at") if isinstance(last, dict) else None
            if isinstance(at, (int, float)) and self.now - at < interval:
                continue
            if self.dry_run:
                self.emit(
                    "SR-REFRESH", target=target.name, result="due", detail="dry-run"
                )
                continue
            try:
                done = self.refresher.run(target)
                result = (done.result, done.from_version, done.to_version, done.detail)
            except Exception as error:  # noqa: BLE001 - a refresh fault never stops the reload half
                result = ("failed", None, None, f"{type(error).__name__}: {error}")
            state[target.name] = dict(
                zip(("result", "from", "to", "detail"), result, strict=True),
                at=self.now,
            )
            changed = True
            self.emit(
                "SR-REFRESH",
                target=target.name,
                plugin=target.options.get("plugin"),
                result=result[0],
                **{"from": result[1] or "-", "to": result[2] or "-"},
                detail=result[3] or "-",
            )
        if changed:
            ledger.write_json(path, state)

    # -- gates ---------------------------------------------------------------------

    def gate(self, session: LiveSession) -> str | None:
        """Gates 1 to 4 of the design's gate table; the library holds gate 7."""
        if session.session_id not in self.gates:
            self.gates[session.session_id] = self._gate(session)
        return self.gates[session.session_id]

    def _gate(self, session: LiveSession) -> str | None:  # noqa: PLR0911 - a return per gate
        record = session.record
        if record.get("kind") != "interactive":
            return "not-interactive"
        if session.pane is None:
            return "no-pane"
        if record.get("waitingFor"):
            return "status-waiting"
        status = record.get("status")
        if status != "idle":
            return f"status-{status or 'unknown'}"
        settle = self.settings["settle_seconds"]
        updated = record.get("statusUpdatedAt")
        if not isinstance(updated, (int, float)) or self.now - updated / 1000 < settle:
            return "status-not-settled"
        idle = goal_waker.idle_state(
            session.transcript,
            self.now,
            {"clock_skew_seconds": 90, "idle_after_seconds": settle},
            extra_turn_end_subtypes=_TURN_ENDS,
        )
        if idle.kind != goal_waker.IDLE:
            return f"transcript-{idle.kind.lower()}-{idle.reason}"
        if session.pane in self.restarted:
            return "update-restart-this-tick"
        return None

    def backoff_until(self, entry: dict[str, Any]) -> float | None:
        at = entry.get("attempted_at")
        if not entry.get("attempts") or not isinstance(at, (int, float)):
            return None
        until = at + self.settings["retry_after_seconds"]
        return until if self.now < until else None

    # -- one session ---------------------------------------------------------------

    def row(self, target: Target, verdict: Verdict, entry: dict[str, Any]) -> dict:
        installed = verdict.installed
        state = "not-loaded"
        if verdict.stale:
            state = "armed"
        elif verdict.state == CURRENT:
            verified = (
                entry.get("state") == VERIFIED
                and installed is not None
                and entry.get("build") == installed.identity
            )
            state = "verified" if verified else "current"
        return {
            "state": state,
            "loaded": verdict.loaded.label if verdict.loaded else None,
            "wanted": installed.label if installed else None,
            "reason": verdict.state if state == "not-loaded" else None,
            "source": verdict.source,
            "since": _iso(entry.get("since")),
            "next_check_at": _iso(self.next_tick),
            "command": target.command,
            "last_result": entry.get("last_result"),
            "attempts": entry.get("attempts", 0),
        }

    def session(
        self, session: LiveSession, targets: Sequence[Target], kinds: dict[str, Kind]
    ) -> None:
        book = SessionLedger(self.root, session.session_id)
        book.bind(session.pid, session.proc_start, self.now)
        visit = _Visit(session, book, kinds)
        groups: dict[str, list[Target]] = {}
        for target in targets:
            command = self.consider(visit, target)
            if command is not None:
                groups.setdefault(command, []).append(target)
        for command in [c for c in groups if c.endswith(_FORCE)]:
            groups[command] += groups.pop(command.removesuffix(_FORCE), [])
        for command, members in groups.items():
            self.deliver(visit, command, members)
        if not self.dry_run:
            book.save()
        self.status_sessions[session.session_id] = {
            "name": session.name,
            "pid": session.pid,
            "targets": visit.rows,
        }

    def consider(self, visit: _Visit, target: Target) -> str | None:
        """The command this target wants typed now, or None (its row says why)."""
        session, book = visit.session, visit.book
        entry = book.entry(target.name)
        verdict = visit.kinds[target.kind].verdict(target, session, entry)
        visit.rows[target.name] = row = self.row(target, verdict, entry)
        installed = verdict.installed
        if not verdict.stale or installed is None:
            if (
                verdict.state == CURRENT
                and installed is not None
                and entry.get("build") == installed.identity
                and entry.get("state") in {PENDING, HELD_CACHE}
                and entry.get("attempt_id")
            ):
                # Claude Code applied a queued or held reload since the last tick.
                entry.update(state=VERIFIED, verified_at=self.now)
                self.emit("SR-VERIFIED", session=session.label, target=target.name)
                row["state"] = "verified"
            return None
        if book.observe(target.name, installed.identity, installed.label, self.now):
            self.emit(
                "SR-STALE",
                session=session.label,
                target=target.name,
                loaded=row["loaded"] or "-",
                wanted=installed.label,
                source=verdict.source,
            )
        entry = book.entry(target.name)
        row["since"] = _iso(entry.get("since"))
        state = entry.get("state")
        if state in {GAVE_UP, VERIFIED}:
            row["state"] = state
            return None
        if state == HELD_CACHE:
            row["state"] = "held-cache"
            if not compacted_since(session, float(entry.get("held_at") or 0)):
                row["reason"] = "awaiting-compaction"
                return None
            return target.command + _FORCE
        until = self.backoff_until(entry)
        if until is not None:
            row.update(state="armed", reason="backoff", next_check_at=_iso(until))
            return None
        return target.command

    def wait(self, visit: _Visit, members: Iterable[Target], reason: str) -> None:
        members = list(members)
        self.emit(
            "SR-WAIT",
            session=visit.session.label,
            targets=",".join(t.name for t in members),
            reason=reason,
        )
        for target in members:
            entry = visit.book.entry(target.name)
            if entry.get("wait_reason") != reason:
                entry.update(wait_reason=reason, wait_since=self.now)
            visit.rows[target.name].update(
                state=wait_state(reason),
                reason=reason,
                since=_iso(entry["wait_since"]),
            )

    def deliver(self, visit: _Visit, command: str, members: list[Target]) -> None:
        session = visit.session
        names = ",".join(t.name for t in members)
        reason = self.gate(session)
        if reason is None and self.typed >= self.settings["max_injections_per_tick"]:
            reason = "tick-cap"
        if reason is not None:
            self.wait(visit, members, reason)
            return
        if self.dry_run:
            self.emit(
                "SR-INJECT",
                session=session.label,
                targets=names,
                command=command,
                detail="dry-run",
            )
            for target in members:
                visit.rows[target.name].update(state="armed", reason="dry-run")
            return
        self.emit(
            "SR-INJECT",
            session=session.label,
            pane=session.pane,
            targets=names,
            command=command,
        )
        delivered = self.action.deliver(session, command)
        if delivered.staged or delivered.submitted:
            self.typed += 1
        if not delivered.submitted:
            self.wait(visit, members, delivered.reason or "refused")
            return
        self.emit(
            "SR-RESULT",
            session=session.label,
            targets=names,
            command=command,
            result=delivered.result,
            attempt=delivered.attempt_id,
            stdout=delivered.stdout_line or "-",
        )
        for kind in {
            id(visit.kinds[t.kind]): visit.kinds[t.kind] for t in members
        }.values():
            kind.reread()
        for target in members:
            self.settle(visit, target, delivered)

    def settle(self, visit: _Visit, target: Target, delivered: DeliveryResult) -> None:
        """One target's share of one submitted reload, re-checked by its kind."""
        entry = visit.book.entry(target.name)
        row = visit.rows[target.name]
        forced = entry.get("state") == HELD_CACHE
        entry.update(
            last_result=delivered.result,
            last_stdout=delivered.stdout_line,
            attempt_id=delivered.attempt_id,
            attempted_at=self.now,
        )
        entry.pop("wait_reason", None)
        entry.pop("wait_since", None)
        row.update(last_result=delivered.result, reason=None)
        if delivered.result == "remote-unavailable":
            self.give_up(visit, target, delivered.result)
            return
        if delivered.result == "held-cache" and not forced:
            entry.update(state=HELD_CACHE, held_at=self.now)
            row.update(state="held-cache", reason="awaiting-compaction")
            return
        verdict = visit.kinds[target.kind].verdict(target, visit.session, entry)
        if not verdict.stale:
            entry.update(state=VERIFIED, verified_at=self.now)
            row.update(
                state="verified",
                loaded=verdict.loaded.label if verdict.loaded else row["loaded"],
            )
            self.emit("SR-VERIFIED", session=visit.session.label, target=target.name)
            return
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry["state"] = PENDING
        row["attempts"] = entry["attempts"]
        if entry["attempts"] >= self.settings["max_attempts"]:
            self.give_up(visit, target, f"still-stale:{delivered.result}")
            return
        row.update(
            state="fired" if delivered.result == "queued-by-cc" else "armed",
            reason=f"still-stale:{delivered.result}",
            next_check_at=_iso(self.now + self.settings["retry_after_seconds"]),
        )

    def give_up(self, visit: _Visit, target: Target, reason: str) -> None:
        entry = visit.book.entry(target.name)
        entry["state"] = GAVE_UP
        visit.rows[target.name].update(state="gave-up", reason=reason)
        self.emit(
            "SR-GIVE-UP",
            session=visit.session.label,
            target=target.name,
            build=entry.get("label"),
            attempts=entry.get("attempts", 0),
            reason=reason,
        )

    # -- end of tick -----------------------------------------------------------------

    def write_status(self, targets: Sequence[Target], kinds: dict[str, Kind]) -> None:
        refreshed = ledger.read_json(self.root / "refresh.json")
        summary = {}
        for target in targets:
            installed = kinds[target.kind].installed(target, None)
            last = refreshed.get(target.name)
            last = last if isinstance(last, dict) else {}
            summary[target.name] = {
                "installed": installed.label if installed else None,
                "last_refresh_at": _iso(last.get("at")),
                "last_refresh_result": last.get("result"),
            }
        document = {
            "schema": STATUS_SCHEMA,
            "written_at": _iso(self.now),
            "host_profile": self.profile,
            "next_tick_at": _iso(self.next_tick),
            "targets": summary,
            "sessions": self.status_sessions,
        }
        try:
            ledger.write_json(self.root / "status.json", document)
        except OSError as error:
            self.emit("SR-STATUS-FAILED", error=f"{type(error).__name__}: {error}")


def tick(
    *,
    dry_run: bool = False,
    now: float | None = None,
    update_restart: dict[str, Any] | None = None,
    only: str | None = None,
) -> dict[str, Any]:
    """One reload pass. ``update_restart`` is this tick's update-restart report (gate 4);
    ``only`` limits the pass to one session id or name."""
    run = _Pass(
        dry_run=dry_run,
        now=time.time() if now is None else now,
        update_restart=update_restart,
    )
    try:
        resolved = config.load()
    except config.ConfigInvalid as error:
        run.emit("SR-DISABLED", reason=f"config:{error}")
        return run.result()
    run.settings, run.profile = resolved.settings, resolved.profile
    for name, reason in resolved.dropped:
        run.emit("SR-CONFIG", target=name, reason=reason)
    if not resolved.settings["enabled"]:
        run.emit(
            "SR-DISABLED", reason=f"enabled-false layer={resolved.sources['enabled']}"
        )
        return run.result()
    targets = resolved.enabled_targets
    run.refresh(targets)
    sessions = live_sessions(run.emit)
    if sessions is None:
        run.emit("SR-WAIT", session="*", reason="inventory-undetermined")
        return run.result()
    # Every kind sees the whole population (the plugin kind's drift test reads it).
    kinds = {t.kind: KINDS[t.kind](sessions, run.emit) for t in targets}
    chosen = [s for s in sessions if only is None or only in {s.session_id, s.name}]
    for session in chosen:
        run.session(session, targets, kinds)
    if not dry_run:
        if only is None:
            ledger.sweep(run.root, {s.session_id for s in sessions}, run.now)
        run.write_status(targets, kinds)
    run.emit("SR-TICK", sessions=len(chosen), targets=len(targets), injected=run.typed)
    return run.result()
