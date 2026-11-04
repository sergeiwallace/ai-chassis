"""Bounded save-state recovery, carried by the goal waker's existing asyncRewake hook.

The retry budget belongs to the context window, not the attempt: publishing another
save-resume must never reset it. All claims share the resume ledger's lock.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ai_chassis import context_supervisor as supervisor
from ai_chassis.delegation import default_delegation_target
from ai_chassis.generations import write_json_fsync

KIND = "compact-rearm"
DEFAULTS = {
    "compact_rearm_after_seconds": (180, 30, 7200),
    "compact_rearm_retry_seconds": (300, 30, 7200),
    "compact_rearm_max_retries": (3, 1, 10),
    "compact_rearm_background_grace_seconds": (1800, 60, 7200),
}
# Exact load-bearing wording of the standing recovery request (2026-10-18).
ASK = (
    "for my personal mac and framework machine profiles, it should use /delegate /codex "
    "to investigate root cause and review cc session jsonl transcript and resume logs "
    "and whatever other telemetry / log data is relevant and then /bug-fix it as "
    "appropriate. for example-client-host-mac and example-project-ec2 cc sessions and any that use api, they "
    "should just /delegate to latest opus model to do same and /bug-fix. and then the "
    "cc session should re-invoke /save-state to re-arm and hopefully it'll work again. "
    "but we need it to be more resilient to retry / re-invoke /save-state to re-arm "
    "it and try to get it to fire. and the agent to investigate and bug fix whatever "
    "caused it to not fire as well."
)


def _inject(use_case: str, command: str) -> str:
    return (
        f"{sys.executable} -m ai_chassis.prompt_injection inject "
        f"--use-case {use_case} --target self --command {command}"
    )


def outlet() -> str:
    """The goal-unblock outlet (skills/save-state/SKILL.md § Freeze point), named in every
    recovery reminder so a stuck session sees it at the moment it needs it."""
    return (
        "Goal-unblock outlet, only if needed: the freeze never blocks an unblock action, "
        "and a frozen session is not an outside wait. If compaction or prompt injection is "
        "not firing, break the freeze to fix it or /delegate /bug-fix it on this machine's "
        "route (Codex on a subscription profile, Opus 5.5 or newer on an API profile). If an "
        "active /goal "
        "keeps re-firing, submit /goal clear through the shared prompt-injection library "
        f"({_inject('goal-clear', '/goal --args clear')}), then re-invoke /save-state. If "
        "compaction still does not start, submit /compact the same way "
        f"({_inject('compact', '/compact')}). Exit 2 QUEUED is normal for --target self: it "
        "lands when this turn ends. At FORCE the gate admits these only inside a /save-state "
        "grace window, so re-invoke /save-state first if one is denied. After compaction, in the new window, re-arm a goal you "
        "cleared with /start-goal. "
    )


def exhausted() -> str:
    return (
        "Save-state compact did not start, and automatic compaction recovery for this window "
        "is exhausted and held for review: no compaction is arranged. This changes the freeze "
        "point, so do not reply 'Freeze point unchanged'. Submit /compact yourself through "
        f"the shared prompt-injection library ({_inject('compact', '/compact')}); ask the "
        "user to type /compact only if that injection is refused. Read ctx-pct and "
        "ctx-pct --advice and quote both, since naming /compact is a context claim. "
        "Keep task results held until /auto-start, except work the outlet needs. "
        + outlet()
    )


def settings() -> dict[str, int]:
    """Independently validate live settings, as idle_save_state_settings does."""
    try:
        live = json.loads(supervisor._config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        live = {}
    if not isinstance(live, dict):
        live = {}
    return {
        key: value if supervisor._bounded_config_int(value, low, high) else default
        for key, (default, low, high) in DEFAULTS.items()
        for value in [live.get(key, default)]
    }


def _stamp(value: Any) -> float:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else 0
    )


def failure(  # noqa: PLR0913, PLR0917 - each caller already holds all six; idle is the inactivity clock
    store, key, session_id, window, now, idle
) -> tuple[str, float] | None:
    """A current-window save that has not reached CC-START; no inference from ctx %."""
    attempts = [
        r
        for r in store.read_attempts(key)
        if r.get("attempt_type") == "compact"
        and r.get("session_id") == session_id
        and _stamp(r.get("armed_at")) >= window
    ]
    latest = max(attempts, key=lambda r: _stamp(r.get("armed_at")), default={})
    if latest.get("started_at") or latest.get("state") in {
        "STARTED",
        "COMPLETED",
        "CONTINUATION_CONFIRMED",
    }:
        return None
    # Outbox disappears on successful SessionStart delivery. A historical REFUSED
    # record alone must never restart recovery after compaction or an ACK.
    artifact = store.root / "outbox" / f"{key}.md"
    saved = _saved_at(store.root, key, session_id, artifact)
    if saved >= window and not artifact.is_file():
        return None
    if artifact.is_file() and saved < window:
        return None  # An old or other-session outbox cannot authorize this recovery.
    if latest and artifact.is_file() and _stamp(latest.get("armed_at")) >= saved:
        progress = max(
            _stamp(latest.get(k))
            for k in (
                "armed_at",
                "bound_at",
                "sent_at",
                "landed_at",
                "last_staged_at",
                "terminal_at",
            )
        )
        return str(latest.get("reason") or latest.get("state")), progress
    if artifact.is_file() and saved >= window:
        return "never-armed", saved
    return _unpublished(store, key, window, now, idle)


def _saved_at(root: Path, key: str, session_id: str, artifact: Path) -> float:
    saved = 0.0
    try:
        artifact_digest = supervisor.digest(artifact.read_bytes())
    except OSError:
        artifact_digest = None
    try:
        with (root / "history" / f"{key}.jsonl").open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (
                    isinstance(row, dict)
                    and row.get("session_id") == session_id
                    and (
                        artifact_digest is None
                        or row.get("command_digest") == artifact_digest
                    )
                ):
                    saved = max(saved, _stamp(row.get("ts")))
    except OSError:
        pass
    # The writer can die between the durable outbox rename and the history append.
    try:
        with artifact.open(encoding="utf-8") as handle:
            header = handle.read(2048).split("---", 2)[1]
        fields = dict(
            line.split(": ", 1) for line in header.splitlines() if ": " in line
        )
        if fields.get("key") == key and fields.get("session_id") == session_id:
            saved = max(saved, float(fields["created_at"]))
    except (OSError, ValueError, KeyError, IndexError):
        pass
    return saved


def _armed_at(store, key) -> float | None:
    try:
        return float(store._active_save_state_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _owner_cancelled(store, key, idle) -> bool:
    """The owner interrupted the turn after this save-state armed, then prompted again.

    Nothing else ends an unpublished save-state: the marker clears only at compact start,
    so a save the owner stopped read as a stalled one (ISSUE-REF). A re-typed /save-state
    re-stamps the marker past the interrupt, and recovery applies to it again.
    """
    active = _armed_at(store, key)
    cancelled = idle.owner_cancelled_at
    return active is not None and cancelled is not None and cancelled >= active


def _clear_if_cancelled(store, key, idle) -> None:
    # Cleared, so the cancellation outlives the interrupt leaving the transcript tail.
    if not _owner_cancelled(store, key, idle):
        return
    with store.locked():
        if _owner_cancelled(store, key, idle):
            store._clear_save_state_active_locked(key)
            supervisor.log_stage(
                "CC-SAVE-STATE-CANCELLED", key, "source=owner-interrupt"
            )


def _unpublished(store, key, window, now, idle) -> tuple[str, float] | None:
    # A freeze can stall before publication. The marker proves a save-state began in
    # this window, but only the FORCE gate re-stamps it, so below FORCE it dates the
    # arm, not the last step. Inactivity runs from the newest transcript tool call
    # instead, and a background job the save launched counts as progress until it
    # reports or outlives its grace (ISSUE-REF).
    active = _armed_at(store, key)
    if active is None or not window <= active <= now:
        return None
    if _owner_cancelled(store, key, idle):
        return None
    progress = max(active, idle.last_tool_use_at or 0)
    waiting = idle.pending_background_launched_at
    if (
        waiting is not None
        and waiting >= active
        and now - waiting < settings()["compact_rearm_background_grace_seconds"]
    ):
        progress = now
    return "never-published", min(progress, now)


def safe_to_remind(transcript: Path, pane: str | None, now: float) -> bool:
    """Require a quiet ended turn, no human gate and a freshly proven empty composer."""
    from ai_chassis import goal_waker  # noqa: PLC0415 - shared transcript classifier

    config = goal_waker.load_config(None)
    # Four-subtype reading preserves the quiet-turn requirement before typing a reminder.
    idle = goal_waker.idle_state(transcript, now, dict(config, idle_after_seconds=0))
    if idle.kind != goal_waker.IDLE or not pane:
        return False
    sample = supervisor.TmuxAdapter().compact_sample(pane, phase="precheck")
    return supervisor._is_proven_staging_target(sample)


def _window(store, session_id: str, idle) -> float | None:
    # The tail's oldest row moves when save-state writes more than the read bound.
    # Only a positively observed SessionStart/compact boundary may reset the budget.
    prior = store.session_state(session_id).get("compact_rearm_window")
    return idle.last_session_start_at or _stamp(prior) or idle.window_started_at


def _brief(key: str, transcript: Path, root: Path, reason: str) -> str:
    from ai_chassis import goal_waker  # noqa: PLC0415 - existing module loader

    fidelity = goal_waker._script("ask_fidelity_launch_gate").af
    framing = (
        "### 1. Objective\nInvestigate this save-state non-fire and /bug-fix its root cause.\n"
        f"### 2. Context / Inputs\nSession: {key}; failure: {reason}. "
        f"Stream the relevant window of {transcript}; read {root / 'resume.log'}, "
        f"{root / 'attempts'}, {root / 'pollers'} and the persisted resume artifact. "
        "These are snapshots; verify load-bearing claims against the working tree.\n"
        "### 3. Output contract\nReturn evidence, root cause, regression RED/GREEN, "
        "changed paths, commit ids, tests and any remaining gaps.\n"
        "### 4. Tools & scope\nUse /bug-fix and /implement; role implement, medium effort. "
        "Read skills/delegate/SKILL.md and docs/procedures/agent-lifecycle-and-worktree-naming.md. "
        "Inspect the harness only; never type into a live session or launch another agent. "
        "Use fixtures for all injection and launcher tests.\n"
        "### 5. Boundaries\nLeaf worker, no delegation. Preserve unrelated changes; "
        "no push, merge, install or deployment. Commit only through aih_commit.\n"
        "### 6. Failure / escalation\nIf unreproduced or blocked, report the evidence gap; "
        "never invent a cause. Leave integration and live re-arming to the parent."
    )
    brief = fidelity.render_brief_carrier(
        tier=1, exact_ask=ASK.encode(), stage_framing=framing
    )
    fidelity.validate_launch_prompt(brief)
    return brief


def _investigation_worktree(
    signature: str, session_title: str
) -> tuple[Path, str] | None:
    """Provision through the canonical creator before a write-capable job is accepted."""
    from ai_chassis import goal_waker  # noqa: PLC0415
    from ai_chassis.job_supervisor import JobError, require_linked_worktree  # noqa: PLC0415

    result = subprocess.run(
        [
            sys.executable,
            str(goal_waker.REPO_ROOT / "scripts/agent_worktree_preflight.py"),
            "--name",
            f"compact-{signature}",
            "--session",
            session_title,
            "--no-node",
            str(goal_waker.REPO_ROOT),
        ],
        cwd=goal_waker.REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode:
        return None
    # The first repository entry is the writable checkout; later entries are
    # diagnostic Git metadata paths. Never infer isolation from a path's spelling.
    prefix = f"- {goal_waker.REPO_ROOT.name}: "
    candidate = next(
        (
            line.removeprefix(prefix)
            for line in result.stdout.splitlines()
            if line.startswith(prefix)
        ),
        "",
    )
    if not candidate:
        return None
    try:
        return require_linked_worktree(candidate), result.stdout
    except JobError:
        return None


def _submit(
    directory: Path, signature: str, brief: str, session_title: str = "compact-rearm"
) -> str:
    """Use the shipped durable job supervisor; a missing service is an explicit fallback."""
    from ai_chassis import goal_waker  # noqa: PLC0415 - canonical harness root
    from ai_chassis.job_supervisor import Ledger  # noqa: PLC0415 - shared service location

    if not (Ledger.from_environment().root / "control.sock").exists():
        return "unavailable"  # No request could have been accepted.

    prepared = _investigation_worktree(signature, session_title)
    if prepared is None:
        return "unavailable"  # Provisioning failed before any submission.
    worktree, scope = prepared
    path = directory / f"{signature}.brief.md"
    path.write_text(f"{brief}\n\n{scope}", encoding="utf-8")
    os.chmod(path, 0o600)
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, local supervisor only
        [
            sys.executable,
            "-m",
            "ai_chassis.job_supervisor",
            "submit",
            "Investigate save-state compact non-fire",
            "--idempotency-key",
            signature,
            "--",
            str(goal_waker.REPO_ROOT / ".claude/bin/cx"),
            "implement",
            "--effort",
            "medium",
            "-C",
            str(worktree),
            "--prompt-file",
            str(path),
        ],
        cwd=worktree,
        env={**os.environ, "CLAUDE_CODE_TASK_LIST_ID": session_title},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode:
        return "uncertain"  # A response can be lost after durable acceptance.
    return f"job={result.stdout.strip()}"


def _linked_paths(text: str, key: str, host: str, repo_root: Path) -> str:
    """``text`` with each bare absolute path a link, as `held_cli.py add` requires.

    The targets here (the transcript, the resume log) lie outside every repository, so their
    relative path from a store's item directory, ``<projects>/<repo>/docs/held/<board>/``, is
    the same whichever repository the route picks (docs/held/README.md § Links).
    """
    from ai_chassis import held_links, held_output  # noqa: PLC0415

    base = repo_root / held_output.HELD_DIR / held_output.session_dir(f"{key}@{host}")
    tokens = {
        f.token: None
        for f in held_links.bare_references(text)
        if f.rule == held_links.BARE_PATH and f.token.startswith("/")
    }
    for token in tokens:
        text = text.replace(token, held_links.path_link(token, base, f"📄 {token}"))
    return text


def _escalate(directory: Path, key: str, text: str) -> str:
    """Start the held blocker's add, which writes its own outcome into the recovery ledger.

    `held_cli.py add` refreshes the landing pad and commits through the repository's hooks,
    which took minutes on a loaded host, and this runs under the resume ledger lock that
    PreCompact and publication also take. A 10 s bound killed the add between its write and
    its commit and left the pad dirty (ISSUE-REF). So the add runs in a detached child,
    `hold_blocker` below, bounded only by held_cli's own per-step timeouts, which undo a
    refused commit.
    """
    from ai_chassis import goal_waker  # noqa: PLC0415

    host = goal_waker._machine_identity()
    if not host:
        return "held item refused: machine identity unavailable"
    body = directory / "exhausted.md"
    body.write_text(
        _linked_paths(text, key, host, goal_waker.REPO_ROOT), encoding="utf-8"
    )
    held_cli = goal_waker.REPO_ROOT / "scripts/held_cli.py"
    with (directory / "escalation.log").open("ab") as log:
        child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, this module's own CLI
            [
                sys.executable,
                "-m",
                "ai_chassis.compact_rearm",
                "hold-blocker",
                str(directory),
                str(held_cli),
                key,
                host,
            ],
            cwd=goal_waker.REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return (
        f"held blocker submission started (pid {child.pid}); "
        f"its outcome replaces this line in {directory / 'state.json'}"
    )


def hold_blocker(directory: Path, held_cli: Path, key: str, host: str) -> str:
    """Run the held add for an exhausted recovery and record its outcome in the ledger."""
    result = subprocess.run(  # noqa: S603 - fixed CLI, no shell or remote publication
        [
            sys.executable,
            str(held_cli),
            "add",
            "--kind",
            "blocker",
            "--session",
            f"{key}@{host}",
            "--task",
            "none",
            "--title",
            "Automatic compaction retry limit",
            "--ask",
            "Review compact recovery failure",
            "--source-skill",
            "save-state",
            "--body-file",
            str(directory / "exhausted.md"),
        ],
        cwd=held_cli.parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        printed = result.stdout.strip().splitlines()
        outcome = "held blocker recorded" + (f": {printed[-1]}" if printed else "")
    else:
        reason = (result.stderr.strip() or result.stdout.strip()).splitlines()
        outcome = (
            f"held blocker refused (exit {result.returncode}): "
            f"{reason[-1] if reason else 'no output'}; review the recovery ledger"
        )
    path = directory / "state.json"
    # directory is <resume root>/compact-rearm/<epoch>, the store `recover` locked.
    with supervisor.ResumeStore(directory.parents[1]).locked():
        state = json.loads(path.read_text(encoding="utf-8"))
        state["escalation_result"] = outcome
        _write(path, state)
    return outcome


def recover(tick, session, *, pane_for_session=None) -> bool:  # noqa: PLR0911 - explicit safety gates before any mutation
    """Handle one stalled freeze before ordinary goal nudges or evaluator-loop clears."""
    from ai_chassis import goal_waker  # noqa: PLC0415

    if not supervisor._enabled() or not session.name:
        return False
    key = goal_waker.session_key(session)
    if key is None or not supervisor._enrolled(key):
        return False
    # Four-subtype reading preserves recovery's existing activity and window measurements.
    idle = goal_waker.idle_state(
        session.transcript, tick.now, dict(tick.config, idle_after_seconds=0)
    )
    window = _window(tick.store, session.session_id, idle)
    if window is None:
        return False
    store = supervisor.ResumeStore(tick.store.root)
    if not tick.dry_run:
        _clear_if_cancelled(store, key, idle)
    found = failure(store, key, session.session_id, window, tick.now, idle)
    if found is None:
        return False
    policy = settings()
    reason, progress = found
    if tick.now - progress < policy["compact_rearm_after_seconds"]:
        tick.emit(
            "GW-SKIP",
            session=session.label,
            reason="save-state-active"
            if reason == "never-published"
            else "compact-rearm-wait",
        )
        return True
    pane = (
        _pane_for_session(session)
        if pane_for_session is None
        else pane_for_session(session)
    )
    if tick.dry_run or not safe_to_remind(session.transcript, pane, tick.now):
        return True
    with store.locked():
        # Recheck after acquiring the same lock that PreCompact and publication use.
        if failure(store, key, session.session_id, window, tick.now, idle) != found:
            return True
        if not safe_to_remind(
            session.transcript, pane, tick.now
        ) or not tick.automatic_wakes(session.label):
            return True
        snapshot = tick.store.session_state(session.session_id)
        snapshot["compact_rearm_window"] = window
        _write(tick.store._state_path(session.session_id), snapshot)
        _deliver_locked(tick, session, key, window, reason)
    return True


def recover_at_prompt(key: str, session_id: str, transcript: Path) -> str | None:
    """The recovery reminder for a synthetic prompt that re-enters a stalled freeze.

    A cron, task-notification or peer prompt starts a turn whatever this returns, so
    showing the reminder there wakes nothing. That is why neither a pane, a composer proof
    nor the idle-wake ``automatic_wakes`` admission applies, while the window's shared
    budget, spacing and cap do. Without it a session the timer cannot reach (no pane, or
    automatic wakes off) answered every re-entry with the unchanged-freeze line for two
    hours and never compacted (ISSUE-REF).
    """
    from ai_chassis import goal_waker  # noqa: PLC0415

    if not supervisor._enabled() or not supervisor._enrolled(key):
        return None
    tick = goal_waker._Tick(
        goal_waker.load_config(None), dry_run=False, now=supervisor._now()
    )
    session = goal_waker.WakerSession(key, os.getpid(), session_id, transcript)
    # Four-subtype reading, as recover's: only its window and activity stamps are used.
    idle = goal_waker.idle_state(
        transcript, tick.now, dict(tick.config, idle_after_seconds=0)
    )
    window = _window(tick.store, session_id, idle)
    if window is None:
        return None
    store = supervisor.ResumeStore(tick.store.root)
    _clear_if_cancelled(store, key, idle)
    found = failure(store, key, session_id, window, tick.now, idle)
    if found is None or tick.now - found[1] < settings()["compact_rearm_after_seconds"]:
        return None
    with store.locked():
        if failure(store, key, session_id, window, tick.now, idle) != found:
            return None
        snapshot = tick.store.session_state(session_id)
        snapshot["compact_rearm_window"] = window
        _write(tick.store._state_path(session_id), snapshot)
        return _deliver_locked(tick, session, key, window, found[0], at_prompt=True)


def _pane_for_session(session) -> str | None:
    """Use the waker's validated inventory, never assume the timer's own pane."""
    from ai_chassis import launch_resume  # noqa: PLC0415 - shared PID-fenced pane resolver

    return next(
        (
            pane
            for _, pane, _ in launch_resume._panes()
            if launch_resume._session_pid(pane) == session.pid
        ),
        None,
    )


def _previous_investigation(path: Path, previous, family: str) -> str | None:
    if not previous:
        return None
    if not previous.get("directive"):
        return (
            "Investigation submission is uncertain; inspect the job supervisor before any manual retry. "
            if previous.get("status") in {"launch-claimed", "uncertain"}
            else ""
        )
    claimed = (
        path.parents[2] / "goal-nudge-claimed" / previous.get("marker", "unissued")
    )
    if previous.get("delivered") or claimed.is_file():
        return ""
    # An unclaimed directive launched nothing. Re-resolve its family rather than
    # carrying an old API authorization onto a subscription profile.
    return previous["directive"] if previous.get("family") == family else None


def _investigation(  # noqa: PLR0913 - submit is off inside the prompt hook's budget
    path: Path, state, key, session, reason, *, submit=True
) -> str:
    signature = supervisor.digest(f"{path.parent.name}/{reason}")[:24]
    target = default_delegation_target()
    family = target.removeprefix("/")
    prior = _previous_investigation(path, state["signatures"].get(signature), family)
    if prior is not None:
        return prior
    root = path.parents[2]
    brief = _brief(key, session.transcript, root, reason)
    # Persist before launch: an interrupted launch has an uncertain outcome and
    # must not be repeated. The durable job also deduplicates this signature.
    record = state["signatures"][signature] = {
        "status": "launch-claimed",
        "family": family,
    }
    launched = "directed"
    if family == "codex" and submit:
        _write(path, state)
        try:
            launched = _submit(path.parent, signature, brief, key)
        except OSError:
            launched = "unavailable"
        except subprocess.SubprocessError:
            # A timed-out submit may already have been accepted. Never launch a
            # second worker through the session when the outcome is uncertain.
            launched = "uncertain"
    record["status"] = launched
    if launched == "uncertain":
        return "Codex investigation submission is uncertain; inspect the job supervisor before any manual retry. "
    if launched.startswith("job="):
        return f"Codex /bug-fix investigation launched: {launched}. "
    brief_path = path.parent / f"{signature}.brief.md"
    brief_path.write_text(brief, encoding="utf-8")
    os.chmod(brief_path, 0o600)
    route = (
        "latest Opus from the live profile"
        if family == "opus"
        else "direct Codex launch unavailable"
    )
    record["directive"] = (
        f"Launch /delegate {target} /bug-fix now using {brief_path} ({route}). "
    )
    return record["directive"]


def _ledger(path: Path, policy: dict[str, int]) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("signatures"), dict)
            or not all(isinstance(r, dict) for r in state["signatures"].values())
            or not supervisor._bounded_config_int(state.get("retries"), 0, 10)
            or not isinstance(state.get("last_reminder_at", 0), (int, float))
        ):
            raise ValueError("invalid recovery ledger")
    except FileNotFoundError:
        return {"retries": 0, "signatures": {}}
    except (OSError, ValueError):
        # A broken ledger must not manufacture a new launch allowance.
        return {"retries": policy["compact_rearm_max_retries"], "signatures": {}}
    return state


def _deliver_locked(  # noqa: PLR0913 - at_prompt selects the delivery channel
    tick, session, key, window, reason, *, at_prompt=False
) -> str | None:
    """Spend one reminder from the window's budget: a marker, or text a prompt shows.

    The timer publishes a marker for the asyncRewake claim. ``at_prompt`` returns the text
    instead, for a turn that is starting anyway, and reports an exhausted budget on every
    such turn so the session never takes a missing compaction for an arranged one.
    """
    policy = settings()
    epoch = supervisor.digest(f"{key}/{session.session_id}/{window}")[:24]
    directory = tick.store.root / "compact-rearm" / epoch
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "state.json"
    state = _ledger(path, policy)
    if state.get("escalated"):
        return exhausted() if at_prompt else None
    if not at_prompt and tick.store.pending_marker(session.session_id):
        return None
    if (
        tick.now - state.get("last_reminder_at", 0)
        < policy["compact_rearm_retry_seconds"]
    ):
        return None
    evidence = f"Transcript: {session.transcript}; resume log: {tick.store.root / 'resume.log'}."
    if state["retries"] >= policy["compact_rearm_max_retries"]:
        text = f"Automatic compaction recovery exhausted ({state['retries']} retries). Stop re-arming; hold this blocker for review. {evidence}"
        state["escalated"] = True
        _write(path, state)
        try:
            state["escalation_result"] = _escalate(directory, key, text)
        except (OSError, subprocess.SubprocessError) as exc:
            state["escalation_result"] = (
                f"Held blocker submission failed ({type(exc).__name__}: {exc}); "
                "review the recovery ledger."
            )
        text += " " + state["escalation_result"] + " " + outlet()
    else:
        text = (
            f"Save-state compact did not start ({reason}). This changes the freeze point: "
            f"re-invoke /save-state to re-arm (retry {state['retries'] + 1}/{policy['compact_rearm_max_retries']}). "
            "Read ctx-pct and ctx-pct --advice and quote both; finish the checkpoint, lifecycle and sentinel. "
            "Keep task results held until /auto-start, except work the outlet needs. "
        )
        text += (
            _investigation(path, state, key, session, reason, submit=not at_prompt)
            + outlet()
            + evidence
        )
        state["retries"] += 1
    state["last_reminder_at"] = tick.now
    _write(path, state)
    _record_delivery(tick, session, text, path, state, at_prompt=at_prompt)
    tick.emit(
        "GW-COMPACT-REARM",
        session=session.label,
        reason=reason,
        retries=state["retries"],
        escalated=bool(state.get("escalated")),
        channel="prompt" if at_prompt else "marker",
    )
    return text if at_prompt else None


def _record_delivery(tick, session, text, path, state, *, at_prompt) -> None:  # noqa: PLR0913 - the ledger and its channel
    """Publish the marker, or note that a prompt showed the text, against each directive."""
    from ai_chassis import goal_waker  # noqa: PLC0415

    marker = (
        None
        if at_prompt
        else goal_waker.issue(
            session, text, tick.store.root, epoch_offset=None, now=tick.now, kind=KIND
        )
    )
    for record in state["signatures"].values():
        if record.get("directive") and record["directive"] in text:
            if marker is None:
                record["delivered"] = True
            else:
                record["marker"] = marker.name
    _write(path, state)


def _write(path: Path, state: dict[str, Any]) -> None:
    write_json_fsync(path, state)
    os.chmod(path, 0o600)


def expire_marker(tick, marker: Path, session, age: float) -> None:
    """Re-offer an unclaimed reminder within the same budget; never use typed fallback."""
    from ai_chassis import goal_waker  # noqa: PLC0415

    current = False
    if session:
        key = goal_waker.session_key(session)
        # Four-subtype reading preserves the activity clock used to expire reminders.
        idle = goal_waker.idle_state(
            session.transcript, tick.now, dict(tick.config, idle_after_seconds=0)
        )
        window = _window(tick.store, session.session_id, idle)
        if key and window is not None:
            current = (
                failure(
                    supervisor.ResumeStore(tick.store.root),
                    key,
                    session.session_id,
                    window,
                    tick.now,
                    idle,
                )
                is not None
            )
    if current and age < settings()["compact_rearm_retry_seconds"]:
        return
    if not tick.dry_run:
        try:
            os.replace(marker, tick.store.expired / marker.name)
        except FileNotFoundError:  # The session may have claimed it concurrently.
            return
    tick.emit(
        "GW-EXPIRED", session=session.session_id if session else "gone", reason=KIND
    )


def claim_check() -> bool:
    """The FileChanged hook must see positive proof before it consumes a reminder."""
    from ai_chassis import goal_waker  # noqa: PLC0415

    try:
        payload = json.load(sys.stdin)
        key = supervisor._derive_session_key()
        transcript = Path(payload["transcript_path"])
        session_id = payload["session_id"]
        now = supervisor._now()
        if not key or not supervisor._enabled() or not supervisor._enrolled(key):
            return False
        # Four-subtype reading preserves the activity proof required to claim a reminder.
        idle = goal_waker.idle_state(
            transcript, now, dict(goal_waker.load_config(None), idle_after_seconds=0)
        )
        store = goal_waker.WakerStore(
            supervisor._root_from_environment(), goal_waker.state_dir()
        )
        window = _window(store, session_id, idle)
        if window is None:
            return False
        found = failure(supervisor.ResumeStore(), key, session_id, window, now, idle)
        return bool(
            found
            and now - found[1] >= settings()["compact_rearm_after_seconds"]
            and safe_to_remind(transcript, os.environ.get("TMUX_PANE"), now)
        )
    except (OSError, ValueError, TypeError, KeyError):
        return False


def main(argv: list[str] | None = None) -> int:
    """`python -m ai_chassis.compact_rearm hold-blocker <directory> <held_cli> <key> <host>`,
    the detached child `_escalate` starts."""
    parser = argparse.ArgumentParser(prog="python -m ai_chassis.compact_rearm")
    commands = parser.add_subparsers(dest="command", required=True)
    hold = commands.add_parser("hold-blocker")
    hold.add_argument("directory", type=Path)
    hold.add_argument("held_cli", type=Path)
    hold.add_argument("key")
    hold.add_argument("host")
    args = parser.parse_args(argv)
    print(hold_blocker(args.directory, args.held_cli, args.key, args.host))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
