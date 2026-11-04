"""The template method every use case runs, and the use-case subclasses.

Fixed order (design § The Injector template): preflights -> resolve -> ARMED ->
wait_ready -> stage (fenced) -> verify staged text -> offset + Enter -> landing
-> post-submit validation -> execution oracle -> terminal stage. A subclass names
its use case, stage prefix, preflights, oracle and retry policy; it cannot reorder
the steps, override a validation step (:data:`CONTRACT_STEPS`, D-19) or remove the
parity observers, and Enter is pressed only through ``Injector._enter``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_chassis.prompt_injection import _tmux, boundary
from ai_chassis.prompt_injection.config import (
    LIBRARY_VERSION,
    boundary_hold,
    landing_attachment,
    landing_timeout_seconds,
    load_config,
    now,
    resume_root,
    safe_component,
    self_heal_policy,
    stage_walk,
    validation,
)
from ai_chassis.prompt_injection.observe import (
    COMPACT_STAGE_ALIASES,
    AttemptStore,
    CompositeObserver,
    Observer,
    new_run_id,
    parity_observers,
    run_record_path,
    stage_name,
    write_notice,
)
from ai_chassis.prompt_injection.outcomes import (
    PRE_SUBMIT_UNVERIFIED,
    PRE_SUBMIT_VERIFIED,
    SUBMITTED_LANDINGS,
    TRANSPORT_REASONS,
    InjectionOutcome,
)
from ai_chassis.prompt_injection.payload import (
    Payload,
    PromptPayload,
    SlashCommandPayload,
)
from ai_chassis.prompt_injection.preflight import (
    COMPOSER_GUTTER_COLUMNS,
    DisposableTarget,
    GoalStateIs,
    InjectionEnabled,
    PayloadTypable,
    Preflight,
    SlashCommandInstalled,
    TransportAvailable,
    VerifiedSlackReply,
    repair_message,
    unmeasured,
    unverified_detail,
    walk_blockers,
    wrapped_rows,
)
from ai_chassis.prompt_injection.readiness import (
    KEYED_VIEW_TIMEOUT_SECONDS,
    STAGE_VERIFY_POLL_SECONDS,
    ComposerReadiness,
    ComposerSample,
    WalkBand,
    band_shift,
    climb_shift,
    cursor_on_first_row,
    later_window,
    draft_rows,
    is_scrolled_window,
    shows_row_zero,
    shows_tail,
    stitched_matches,
    stitched_verdict,
    undo_tail_holds,
    walk_band,
)
from ai_chassis.prompt_injection.self_heal import SelfHealArmed, failure_of
from ai_chassis.prompt_injection.targets import (
    SessionTarget,
    TargetUnresolved,
    key_for_spec,
    resolve_target,
)
from ai_chassis.prompt_injection.transport import TmuxTransport, Transport
from ai_chassis.prompt_injection.verify import (
    POST_SUBMIT_MISMATCH,
    ExecutionOracle,
    Expectation,
    GoalActiveOracle,
    GoalClearedOracle,
    Landing,
    LocalCommandOracle,
    OracleVerdict,
    PostSubmitValidator,
    PostSubmitVerdict,
    PreCompactOracle,
    TranscriptVerifier,
    goal_state,
    transcript_size,
)
from ai_chassis.session_key import refusal as session_key_refusal

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on Windows
    fcntl = None  # type: ignore[assignment]

#: How often a run waiting for another run's pane lease tries it again.
_PANE_LEASE_POLL_SECONDS = 0.05


class _PaneLease:
    """One injection at a time per pane, across processes (D-14, ISSUE-REF).

    Without it two runs that each proved the composer empty both type into it, and one
    Enter submits their texts run together. Taken before the first readiness sample and
    held through the landing check; a run that cannot take it inside its ready budget
    refuses ``injection-in-flight`` and types nothing. Closing the descriptor drops the
    ``flock``, so a run that dies releases it with its process."""

    def __init__(self, pane_id: str) -> None:
        self.path = resume_root() / "locks" / f"pane-{safe_component(pane_id)}.lock"
        self.fd: int | None = None
        #: Whether the first try found the lease held, and how long acquiring took.
        self.waited = False
        self.wait_ms = 0

    def acquire(self, wait: float) -> bool:
        if fcntl is None:
            return True
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        started = time.monotonic()
        deadline = started + max(0.0, wait)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.waited = True
                self.wait_ms = int((time.monotonic() - started) * 1000)
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(_PANE_LEASE_POLL_SECONDS)
                continue
            self.wait_ms = int((time.monotonic() - started) * 1000)
            self.fd = fd
            return True

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


# _write_transcript_debug_capture's bound (4021-4055).
_TRANSCRIPT_CAPTURE_BYTES = 16 * 1024
# The most DELs the stage undo sends before it re-checks the composer (ISSUE-REF).
_UNDO_CHUNK = 200
# How much longer the undo waits past the keyed-view bound while the composer still
# shows its own text from before the last chunk: a busy session drew a chunk's frame
# later than 3 s (start-goal run 2026-10-24T233354Z), and the repair found the text
# intact afterwards, so the DELs had landed.
UNDO_LAG_TIMEOUT_SECONDS = 10.0
# What the shared Up guard returns instead of pressing (ISSUE-REF).
UP_REFUSED_ON_FIRST_ROW = "up-refused-cursor-on-first-row"
# Ups in a row, after the band scrolled, that moved neither band nor cursor before the
# climb refuses walk-top-unproved: the one that stopped the live climb, and one re-probe
# (ISSUE-REF).
INERT_UPS_REFUSED = 2
# One staging, at most one further Enter (ISSUE-REF parity).
_MAX_SUBMISSIONS = 2
# _restage_backoff_ceiling_seconds bounds (4173-4181).
_CEILING_MIN_SECONDS = 60
_CEILING_MAX_SECONDS = 3600


class RetryPolicy(ABC):
    @abstractmethod
    def fields(
        self, record: dict[str, Any], config: dict[str, Any]
    ) -> dict[str, int] | None:
        """Backoff fields for a retryable attempt, or None for a one-shot one."""


class NoRetry(RetryPolicy):
    def fields(self, _record, _config):
        return None


class ExponentialBackoff(RetryPolicy):
    """ContextSupervisor._retry_fields (6021-6033): base * 2**(stagings-1), capped
    by ``restage_backoff_ceiling_seconds`` (env ``AIH_RESTAGE_BACKOFF_CEILING_SECONDS``
    bounded 60-3600, as _restage_backoff_ceiling_seconds 4173-4181)."""

    def fields(self, record, config):
        base = max(1, int(record.get("lifecycle_interval_seconds", 30)))
        ceiling = base
        try:
            value = float(
                os.environ.get(
                    "AIH_RESTAGE_BACKOFF_CEILING_SECONDS",
                    str(config["restage_backoff_ceiling_seconds"]),
                )
            )
            if _CEILING_MIN_SECONDS <= value <= _CEILING_MAX_SECONDS:
                ceiling = int(value)
        except ValueError:
            pass
        stagings = max(1, int(record.get("stagings", 0)))
        backoff = min(ceiling, base * (2 ** (stagings - 1)))
        return {"backoff_seconds": backoff, "retry_not_before": now() + backoff}


@dataclass
class _WalkView:
    """One settled capture during the walk."""

    sample: ComposerSample
    band: WalkBand | None
    cursor: tuple[int, int] | None
    #: Seconds from the settle's start to this capture, and False when the bound
    #: returned it unsettled: a key whose frame the pane never drew in time.
    waited: float = 0.0
    settled: bool = True


def _band_cursor(view: _WalkView) -> tuple[int, int] | None:
    """The terminal cursor relative to the band's first line, while it stands on a
    band row with the closing rule drawn below, so the frame is not clipped and the
    cursor shows each Up; None otherwise. Output above the composer can move the band
    on screen, so only the relative position says a key landed."""
    band, cursor = view.band, view.cursor
    if band is None or cursor is None or not band.closed:
        return None
    row = cursor[1] - band.first_line
    return (cursor[0], row) if 0 <= row < len(band.rows) else None


@dataclass
class _Walk:
    """The walk's state (design § The walk): every band placed at its draft row index,
    counted from the first band's top, and every key press with what it showed."""

    text: str
    settle: float
    #: How much longer than the keyed-view bound a key whose frame must change waits for
    #: that frame (``stage_walk.late_frame_seconds``, ISSUE-REF).
    late: float = 0.0
    #: How long the band must hold still before an Up whose landing no frame shows
    #: (``stage_walk.staged_quiet_seconds``): fewer are pressed blind into a stall.
    quiet: float = 0.0
    #: The longest a frame some key of this walk must change took to arrive: the host's
    #: measured lag, so an Up no frame shows waits at least that long (ISSUE-REF).
    slowest: float = 0.0
    started: float = field(default_factory=time.monotonic)
    top: int = 0
    rows: dict[int, str] = field(default_factory=dict)
    conflict: int | None = None
    filler: int = 0
    ups: int = 0
    downs: int = 0
    #: What ended the climb (design § The walk): ``row-zero-content``,
    #: ``first-row-cursor``, or the refusal's own (``inert-up``, ``move-limit``,
    #: ``up-send-failed``, ``band-jump``). None while the climb has not ended.
    climb_end: str | None = None
    #: Key indexes of the Ups after the band first scrolled that moved neither the
    #: band nor the terminal cursor (ISSUE-REF).
    inert_ups: list[int] = field(default_factory=list)
    keys: list[dict[str, Any]] = field(default_factory=list)
    frames: list[str] = field(default_factory=list)
    #: Called with each frame's label, its text and (for a key press) its key entry,
    #: so the run record keeps one capture file per press.
    listener: Callable[[str, str, dict[str, Any] | None], None] | None = None

    def restart(self, band: WalkBand) -> None:
        """Take ``band`` as the staged draft: every row placed so far came from a frame
        that showed the draft only partly drawn."""
        self.top, self.rows, self.conflict = 0, {}, None
        self.place(band, 0)

    def place(self, band: WalkBand, shift: int) -> None:
        self.top += shift
        for offset, row in enumerate(band.rows):
            index = self.top + offset
            if self.rows.setdefault(index, row) != row and self.conflict is None:
                self.conflict = index

    def frame(
        self, label: str, view: _WalkView, entry: dict[str, Any] | None = None
    ) -> None:
        x, y = view.cursor if view.cursor is not None else (None, None)
        capture = view.sample.capture or ""
        if not capture.endswith("\n"):
            capture += "\n"
        text = f"=== {label}\ncursor={x},{y}\n{capture}"
        self.frames.append(text)
        if self.listener is not None:
            self.listener(label, text, entry)

    def record(self, key: str, view: _WalkView, shift: int | None) -> None:
        rows = view.band.rows if view.band is not None else ()
        x, y = view.cursor if view.cursor is not None else (None, None)
        entry = {
            "key": key,
            "index": len(self.keys) + 1,
            "first": rows[0] if rows else None,
            "last": rows[-1] if rows else None,
            "shift": shift,
            "cursor_x": x,
            "cursor_y": y,
            "settled": view.settled,
            "waited_seconds": round(view.waited, 3),
        }
        self.keys.append(entry)
        self.frame(f"key {key} index={len(self.keys)} shift={shift}", view, entry)

    def stitched(self) -> list[str] | None:
        """Row 0 onwards, or None when a row was never seen or seen two ways."""
        if self.conflict is not None or not self.rows:
            return None
        last = max(self.rows)
        if any(index not in self.rows for index in range(self.top, last + 1)):
            return None
        return [self.rows[index] for index in range(self.top, last + 1)]


@dataclass
class _Run:
    """One attempt's mutable state while ``run`` walks the template."""

    record: dict[str, Any]
    target: SessionTarget | None = None
    samples: list[ComposerSample] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    trail: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Path] = field(default_factory=dict)
    #: The record came from a legacy call site that already armed and bound it.
    adopted: bool = False
    #: Stop once the composer is proved ready; type nothing.
    dry_run: bool = False
    #: The common capability gate passed before readiness and live input.
    walk_capability_verified: bool = False
    #: The capability check's own failure when it did not pass, for every refusal that
    #: follows from it (ISSUE-REF).
    capability_failure: str | None = None
    capability_seconds: float = 0.0


def _climb_jump(
    walk: _Walk,
    before: _WalkView,
    after: _WalkView,
    shift: int | None,
    reach: int = 1,
) -> tuple[str, str] | None:
    """The refusal for an Up that moved more than one row, setting the climb's end: the
    band by a shift no single row explains, or (band unmoved) the cursor by several
    rows, which means several Ups drew in one frame, so the frames recorded before it
    were not their own Ups' and the shared row-0 guard read a stale cursor. ``reach`` is
    how many rows the band may have moved in this frame: more than one only before it
    first scrolled, where Ups move a cursor the clipped pane does not show (ISSUE-REF)."""
    if after.band is None or shift is None or not -reach <= shift <= 0:
        walk.climb_end = "band-jump"
        return "walk-stitch-mismatch", f"up-shift: {walk.ups} shift={shift}"
    jump = _cursor_jump(before, after) if shift == 0 else None
    if jump is not None:
        walk.climb_end = "cursor-jump"
        return "walk-stitch-mismatch", f"up-cursor: {walk.ups} rows={jump}"
    return None


def _held_bands(
    walk: _Walk, bands: list[WalkBand], landed: WalkBand, rows: int
) -> None:
    """After one frame landed ``rows`` rows of scroll from held Ups, the band each of the
    last ``rows - 1`` of those Ups really left, rebuilt from the placed rows, in place of
    the old frame recorded for it: the walk down retraces one row per Down. The Ups before
    them moved the unseen cursor and left the band where it was. A row never placed
    leaves the old frame recorded, so the walk down refuses rather than guesses."""
    for back in range(1, rows):
        top = walk.top + back
        held = [walk.rows.get(top + offset) for offset in range(len(landed.rows))]
        rebuilt = tuple(row for row in held if row is not None)
        if len(rebuilt) == len(held):
            bands[-back] = WalkBand(rebuilt, landed.first_line, landed.closed)


def _cursor_jump(before: _WalkView, after: _WalkView) -> int | None:
    """How many band rows the cursor climbed in one Up when that is more than one,
    else None (also when either view does not show the cursor on a band row)."""
    first, second = _band_cursor(before), _band_cursor(after)
    if first is None or second is None:
        return None
    rows = first[1] - second[1]
    return rows if rows > 1 else None


def _undrawn(sample: ComposerSample) -> bool:
    """A recognised composer showing no draft after a stage: nothing to walk, and on a
    loaded host the keystrokes' frame may simply not be drawn yet (ISSUE-REF)."""
    return (
        sample.composer_recognized
        and sample.classification != "stage-verified"
        and not (sample.content or "").strip()
    )


def _blank(band: WalkBand | None) -> bool:
    """A draft band drawn with no text in any row: no staged draft is on screen yet."""
    return band is not None and not any(row.strip() for row in band.rows)


#: The template's validation steps (D-19, ISSUE-REF): no subclass may override one, so
#: every use case stages and verifies its draft before Enter, presses Enter only through
#: ``_enter`` and runs the post-submit validator and the self-heal hook after it.
CONTRACT_STEPS = frozenset(
    {
        "run",
        "_run",
        "_deliver",
        "_stage_text",
        "_pre_submit",
        "_enter",
        "_landed",
        "_post_submit",
        "_self_heal",
    }
)


class Injector(ABC):
    use_case: str = ""
    stage_prefix: str = ""
    attempt_type: str = ""
    stage_aliases: dict[str, str] | None = None
    #: The reason a slash command that landed as plain text is recorded under.
    text_reason = "delivered-as-text"
    #: The directory under the resume root this use case's attempt records go to.
    ledger = "attempts"
    #: Maximum Enter sends; launch assignments opt out of the guarded second Enter.
    max_submissions = _MAX_SUBMISSIONS
    #: True for the injections that perform or follow the compaction itself
    #: (ISSUE-REF): they are never held at the save-state-to-compact boundary.
    self_rescue: bool = False
    #: Set by :func:`boundary.drain` on the instance re-delivering a held record,
    #: so that run does not drain again.
    draining: bool = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        overridden = sorted(CONTRACT_STEPS & set(vars(cls)))
        if overridden:
            raise TypeError(
                f"{cls.__name__} overrides the validation contract: {', '.join(overridden)}"
            )

    def __init__(  # noqa: PLR0913 - keyword-only wiring; each names one input of the run
        self,
        observers: Iterable[Observer] | None = None,
        *,
        tmux_command: _tmux.Command = _tmux.DEFAULT_COMMAND,
        config: dict[str, Any] | None = None,
        transcript_path: Path | None = None,
        adopt_record: dict[str, Any] | None = None,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        parent_attempt_id: str | None = None,
        resume_argv: list[str] | None = None,
        submission_gate: Callable[[], bool] | None = None,
    ) -> None:
        """``observers`` are added to the parity set, never substituted for it.
        ``transcript_path`` is the hook payload's, when the caller has one; it
        outranks every other transcript source.

        ``adopt_record`` is a legacy cutover call site's own armed and bound
        attempt record: ``run`` advances THAT record (same file, same attempt id,
        its stagings carried forward) instead of writing a second attempt the
        legacy ledger would count as another pending one, and logs no ARM/BIND.

        ``run_id`` names the run this attempt belongs to (a retry, or the self-heal
        loop's own injections, which also name their ``parent_run_id``); a first attempt
        mints one. ``resume_argv`` is how the consumer re-runs this run once a repair
        is verified (ISSUE-REF, D-18)."""
        self.adopt_record = adopt_record
        self.run_id = run_id
        self.parent_run_id = parent_run_id
        self.parent_attempt_id = parent_attempt_id
        self.resume_argv = resume_argv
        self.tmux_command = tmux_command
        self.observer = CompositeObserver(
            [*parity_observers(self.ledger), *(observers or [])]
        )
        self.transport: Transport = TmuxTransport(tmux_command)
        self.readiness = ComposerReadiness(tmux_command)
        self.verifier = TranscriptVerifier()
        self.config = config
        self.transcript_path = transcript_path
        # Consumer policy is live at every Enter, after leases/readiness/staging.
        self.submission_gate = submission_gate

    # --- subclass hooks ---------------------------------------------------------
    @abstractmethod
    def preflights(self) -> list[Preflight]: ...

    def oracle(self) -> ExecutionOracle | None:
        return None

    def retry_policy(self) -> RetryPolicy:
        return NoRetry()

    def on_landing(self, record: dict[str, Any], landing: Landing) -> None:
        """A use-case side effect once the transcript proves the landing."""
        record["landing_record_type"] = landing.record_type

    def still_relevant(self, _record: dict[str, Any]) -> tuple[bool, str]:
        """Whether a held record is still worth delivering once the boundary has
        closed, checked after its TTL and its preflights (which already carry
        each use case's own relevance, such as goal state)."""
        return True, "ttl"

    def hold_options(self) -> dict[str, Any]:
        """The constructor keywords a held record needs to rebuild this injector."""
        return {}

    def default_payload(self) -> Payload | None:
        """The payload ``run`` types when the caller passes none."""
        return None

    def default_landing_timeout(self) -> float | None:
        """The landing budget when the caller passes none; None reads the config."""
        return None

    # --- template ---------------------------------------------------------------
    def stage(self, run: _Run, stage: str, detail: str) -> None:
        self.observer.stage(
            stage_name(self.stage_prefix, stage, self.stage_aliases),
            str(run.record["key"]),
            detail,
        )

    def save(self, run: _Run, **changes: Any) -> None:
        run.record.update(changes)
        path = self.observer.attempt(run.record)
        if path is not None:
            run.evidence["attempt"] = path

    def _run_event(self, run: _Run, event: str, fields: dict[str, Any]) -> None:
        self.observer.run_event(run.record, event, fields)

    def _run_capture(self, run: _Run, label: str, text: str) -> str | None:
        path = self.observer.run_capture(run.record, label, text)
        return str(path) if path is not None else None

    def run(
        self,
        target_ref: str | SessionTarget,
        payload: Payload | None = None,
        *,
        dry_run: bool = False,
        landing_timeout: float | None = None,
        ready_wait: float | None = None,
    ) -> InjectionOutcome:
        payload = payload if payload is not None else self.default_payload()
        if payload is None:
            raise TypeError(f"{type(self).__name__}.run needs a payload")
        if landing_timeout is None:
            landing_timeout = self.default_landing_timeout()
        self._active: _Run | None = None
        try:
            outcome = self._run(
                target_ref, payload, dry_run, landing_timeout, ready_wait
            )
        except Exception as error:
            # _write_hook_exception_diagnostic parity: gated, bounded, then re-raised.
            self.observer.exception(f"{self.use_case}-run", error)
            if self._active is not None:
                self._run_event(
                    self._active,
                    "exception",
                    {"type": type(error).__name__, "message": str(error)[:512]},
                )
            raise
        return self._self_heal(outcome)

    def _self_heal(self, outcome: InjectionOutcome) -> InjectionOutcome:
        """The self-heal hook on every run, outside the pane lease (the loop's own
        injections take it): a pass re-arms what this run's retry was for (T6); a
        validation failure is the ``fail`` event, whose decision rides on the outcome. A
        fault here is recorded and never turns into a failure of the injection."""
        if outcome.state == "PENDING_UNDO":
            return outcome
        run = self._active
        if run is None:
            return outcome
        from ai_chassis.prompt_injection.self_heal import SelfHeal

        record = run.record
        try:
            heal = SelfHeal(config=self.config)
            if record.get(
                "post_submit_verdict"
            ) == "post-submit-verified" and record.get("state") in {
                "SENT",
                "STARTED",
                "COMPLETED",
            }:
                heal.on_pass(record)
                return outcome
            decision = heal.on_fail(record)
            if decision is None:
                self._validation_notice(run)
        except Exception as error:  # noqa: BLE001 - the loop never breaks the injection
            self.observer.exception(f"{self.use_case}-self-heal", error)
            self._run_event(
                run,
                "exception",
                {"type": type(error).__name__, "message": str(error)[:512]},
            )
            return outcome
        if decision is None:
            return outcome
        return replace(outcome, self_heal=decision.to_json())

    def _validation_notice(self, run: _Run) -> None:
        """D-19 for a use case that does not self-heal: a validation failure is
        recorded (its verdicts are on the attempt and in the run record already) and
        relayed into the session as a one-shot notice; nothing is injected. A notice
        this run already wrote (a transport refusal, a failed undo) is kept."""
        if "notice" in run.evidence:
            return
        config = self.config if self.config is not None else load_config()
        record = run.record
        signature = failure_of(record, config)
        if signature is None or self_heal_policy(config, signature.use_case)[0]:
            return
        verdict = record.get("post_submit_verdict") or record.get("pre_submit_verdict")
        text = (
            f"Validation failed: {signature.human} (verdict={verdict}, "
            f"reason={record.get('reason')}). Self-heal is off for this use case, so "
            "no repair prompt was injected. The run record is "
            f"{run_record_path(str(record.get('run_id')))}."
        )
        path = write_notice(record, "validation-failed", text, heading="VALIDATION")
        if path is not None:
            run.evidence["notice"] = path

    def _run(
        self,
        target_ref: str | SessionTarget,
        payload: Payload,
        dry_run: bool,
        landing_timeout: float | None,
        ready_wait: float | None,
    ) -> InjectionOutcome:
        config = self.config if self.config is not None else load_config()
        budget = (
            ready_wait
            if ready_wait is not None
            else float(config["ready_wait_seconds"])
        )
        ready_deadline = time.monotonic() + max(0.0, budget)
        given = target_ref if isinstance(target_ref, SessionTarget) else None
        spec = str(target_ref) if given is None else None
        key = given.key if given is not None else key_for_spec(spec or "")
        if self.adopt_record is None:
            run = _Run(self._new_record(secrets.token_hex(8), key, payload))
        else:
            run = _Run(self._adopted_record(self.adopt_record, payload), adopted=True)
        effect = landing_attachment(config, self.use_case)
        if effect is not None:
            run.record["landing_attachment"] = effect
        self._active = run
        run.evidence["log"] = resume_root() / "resume.log"
        run.evidence["run"] = run_record_path(str(run.record["run_id"]))
        if given is not None:
            self._bind(run, given)
        refused = self._preflight(run, spec, payload, config, ready_deadline)
        ready_deadline += run.capability_seconds
        if refused is None:
            refused = self._boundary_step(run, payload, config)
        if refused is not None:
            return refused
        timeout = landing_timeout
        if timeout is None:
            timeout = landing_timeout_seconds(config)
        if timeout is None:
            return self._refuse(
                run, "landing-timeout-invalid", "AIH_LANDING_TIMEOUT_SECONDS"
            )
        self._arm(run)
        run.dry_run = dry_run
        landing = self._deliver(
            run, payload, max(0.0, ready_deadline - time.monotonic()), timeout, config
        )
        if isinstance(landing, InjectionOutcome):
            return landing
        # Outside the pane lease: an execution oracle may wait up to
        # completion_timeout_seconds, and nothing it does types.
        return self._landed(run, landing, config)

    def _deliver(
        self,
        run: _Run,
        payload: Payload,
        budget: float,
        timeout: float,
        config: dict[str, Any],
    ) -> Landing | InjectionOutcome:
        """Readiness, staging (with its undo), Enter and the landing check, under the
        per-pane lease (D-14): the undo's DELs remove only this attempt's text because
        no other run can type between the empty-composer proof and them."""
        target = run.target
        assert target is not None
        ready_deadline = time.monotonic() + max(0.0, budget)
        lease = _PaneLease(target.pane_id)
        held = lease.acquire(budget)
        # Persisted by the next save (the refusal's, or readiness's idle proof), so the
        # lease adds no fsync of its own to the path before the first keystroke.
        run.record["pane_lease"] = {
            "pane": target.pane_id,
            "held": held,
            "wait_ms": lease.wait_ms,
        }
        if not held:
            return self._refuse(run, "injection-in-flight", f"pane={target.pane_id}")
        if lease.waited:
            self.stage(
                run,
                "LEASE-WAIT",
                f"waited_ms={lease.wait_ms} attempt={run.record['attempt_id']}",
            )
        try:
            if not run.dry_run:
                from ai_chassis.prompt_injection.repair import retry_pending_undos

                retry_pending_undos(pane_id=target.pane_id, lease_held=True)
            refused = self._await_ready(
                run,
                payload,
                max(0.0, ready_deadline - time.monotonic()),
                float(config["ready_poll_seconds"]),
            )
            if refused is None:
                self._intent(run, payload)
            if refused is None and run.dry_run:
                refused = self._finish(run, "READY", reason="dry-run", terminal=True)
            if refused is None:
                refused = self._fits_composer(run, payload, config)
            if refused is None:
                refused = self._stage_text(run, payload, config)
            if refused is not None:
                return refused
            return self._submit(run, payload, timeout)
        finally:
            lease.release()

    def _resolve(self, run: _Run, spec: str | None) -> InjectionOutcome | None:
        try:
            self._bind(
                run,
                resolve_target(
                    spec,
                    transcript_path=self.transcript_path,
                    tmux_command=self.tmux_command,
                ),
            )
        except TargetUnresolved as error:
            return self._refuse(run, error.reason, error.detail)
        return None

    def _preflight(
        self,
        run: _Run,
        spec: str | None,
        payload: Payload,
        config: dict[str, Any],
        ready_deadline: float,
    ) -> InjectionOutcome | None:
        """Every preflight in order, resolving the target only once one needs it,
        so a disabled use case never touches tmux. A use case that lists no
        :class:`PayloadTypable` still gets one last, so every use case runs the
        capability check before its first keystroke and can admit the walk
        (ISSUE-REF: ``compact`` listed none, and its walk was never admitted)."""
        checks = list(self.preflights())
        if not any(isinstance(check, PayloadTypable) for check in checks):
            checks.append(PayloadTypable(self.tmux_command))
        for preflight in checks:
            if preflight.requires_target and run.target is None:
                refused = self._resolve(run, spec)
                if refused is not None:
                    return refused
            verdict = (
                preflight.wait_ready(
                    run.target,
                    payload,
                    config,
                    ready_deadline,
                    float(config["ready_poll_seconds"]),
                )
                if isinstance(preflight, PayloadTypable)
                else preflight.check(run.target, payload, config)
            )
            if isinstance(preflight, PayloadTypable) and preflight.capability_failure:
                run.capability_failure = preflight.capability_failure
                self._capability_repair(
                    run, preflight.capability_failure, delivered=bool(verdict)
                )
            if not verdict:
                return self._refuse(run, str(verdict.reason), verdict.detail)
            if isinstance(preflight, PayloadTypable):
                run.walk_capability_verified |= preflight.capability_verified
                run.capability_seconds += preflight.capability_seconds
        if run.target is None:
            return self._resolve(run, spec)
        return None

    def _capability_repair(self, run: _Run, failure: str, *, delivered: bool) -> None:
        """Record the repair a failed capability check prints for every use case, the
        line /start-goal prints (ISSUE-REF): on the attempt record and its outcome, and
        in the stage log. Self-heal, when on, still runs on its own validation."""
        target = run.target
        repair = repair_message(
            target.cc_version if target is not None else None,
            failure,
            "the delivery goes on with the walk off"
            if delivered
            else "nothing was typed",
            label=self.use_case,
        )
        run.record["capability_repair"] = repair
        self.stage(
            run,
            "CAPABILITY",
            f"result={'inconclusive' if unmeasured(failure) else 'failed'} "
            f"attempt={run.record['attempt_id']} repair={repair}",
        )

    def _boundary_step(
        self, run: _Run, payload: Payload, config: dict[str, Any]
    ) -> InjectionOutcome | None:
        """Hold an injection that is not self-rescue inside the save-state-to-compact
        boundary (D-5); outside it, deliver whatever is held for the key first (D-6).

        A self-rescue injection neither holds nor drains: delivering held nudges
        ahead of a ``/compact`` or the continuation would delay the very send the
        hold exists to protect, so the compact lifecycle's own drain covers it."""
        enabled, ttl = boundary_hold(config)
        if not enabled or self.self_rescue:
            return None
        key = str(run.record["key"])
        state = boundary.in_boundary(key)
        if state is None:
            if not self.draining:
                self._drain_held(run, key)
            return None
        return self._hold(run, payload, state, ttl)

    def _hold(
        self, run: _Run, payload: Payload, state: boundary.BoundaryState, ttl: int
    ) -> InjectionOutcome:
        target = run.target
        assert target is not None
        attempt_id = str(run.record["attempt_id"])
        self.save(
            run,
            **boundary.held_fields(payload, state, ttl),
            **boundary.target_fields(target),
            hold_options=self.hold_options(),
            tmux_command=list(self.tmux_command),
        )
        self.stage(run, "HOLD", boundary.hold_detail(attempt_id, state))
        held = self._finish(run, "HELD", reason="boundary-held")
        if self.draining or boundary.in_boundary(target.key) is not None:
            return held
        # The boundary closed while the hold was being written: deliver it now
        # rather than strand it until the next drain.
        delivered = {o.attempt_id: o for o in self._drain_held(run, target.key)}
        record = AttemptStore(ledger=self.ledger).find(attempt_id) or {}
        return delivered.get(str(record.get("released_attempt_id")), held)

    def _drain_held(self, run: _Run, key: str) -> list[InjectionOutcome]:
        try:
            return boundary.drain(key)
        except Exception as error:  # noqa: BLE001 - a drain fault never blocks this run
            self.stage(run, "DRAIN-ERROR", f"error={type(error).__name__}")
            self.observer.exception(f"{self.use_case}-drain", error)
            return []

    def _arm(self, run: _Run) -> None:
        target = run.target
        assert target is not None
        goal = goal_state(target)
        run.trail["goal_active"] = {"active": "true", "inactive": "false"}.get(
            goal, "unknown"
        )
        if run.adopted:
            return
        self.save(run, state="ARMED")
        self.stage(run, "ARM", f"attempt={run.record['attempt_id']}")
        self.save(run, state="BOUND")
        self.stage(run, "BIND", f"pane={target.pane_id}")

    def _await_ready(
        self, run: _Run, payload: Payload, budget: float, poll: float
    ) -> InjectionOutcome | None:
        target = run.target
        assert target is not None
        started = time.monotonic()
        ready = self.readiness.wait_ready(target, budget, poll)
        run.samples += ready.samples
        run.phases += ready.phases
        if not ready.ready:
            reason = str(ready.reason)
            self.stage(
                run, "NOT-READY", f"reason={reason} attempt={run.record['attempt_id']}"
            )
            # The frame the refusal was decided on, kept whatever the debug gate says:
            # three goal-clear refusals left nothing to read (ISSUE-REF).
            if ready.samples:
                self._frame_evidence(run, reason, ready.samples[-1])
            if reason == "composer-nonempty":
                self.stage(
                    run,
                    "ESCALATE",
                    "reason=composer-nonempty action=submit-or-clear-draft",
                )
                self.transport.warn(
                    target,
                    f"AIH: {payload.command or 'prompt'} pending — submit or clear the composer draft",
                )
            detail = (
                "composer-not-visible"
                if ready.samples and not ready.samples[-1].composer_recognized
                else ""
            )
            return self._refuse(run, reason, detail, stage=None)
        self.save(run, idle_proof="composer-empty")
        self.stage(
            run,
            "IDLE-CONFIRMED",
            f"elapsed_ms={int((time.monotonic() - started) * 1000)}",
        )
        return None

    def _intent(self, run: _Run, payload: Payload) -> None:
        """The run record's first event: everything that was meant to be typed, where,
        and the geometry of the composer that was proved empty for it."""
        target = run.target
        assert target is not None
        ready = next((s for s in reversed(run.samples) if s.composer_recognized), None)
        height = _tmux.display(self.tmux_command, target.pane_id, "#{pane_height}")
        self._run_event(
            run,
            "intent",
            {
                "text": payload.text,
                "digest": payload.digest,
                "args_digest": payload.args_digest,
                "payload_kind": payload.kind,
                "payload_mode": payload.mode,
                "lines": payload.line_count,
                "chars": len(payload.text),
                "use_case": self.use_case,
                "key": target.key,
                "pane": target.pane_id,
                "transcript": str(target.transcript_path)
                if target.transcript_path
                else None,
                "cc_version": target.cc_version,
                "pane_width": self.readiness.pane_width(target),
                "pane_height": int(height)
                if (height or "").strip().isdigit()
                else None,
                "visible_rows": ready.visible_rows if ready is not None else None,
                "idle_proof": run.record.get("idle_proof"),
                "parent_run_id": run.record.get("parent_run_id"),
            },
        )

    def _fits_composer(
        self, run: _Run, payload: Payload, config: dict[str, Any]
    ) -> InjectionOutcome | None:
        """Every ``PayloadTypable`` this use case runs, asked again with the geometry
        of the readiness sample that proved the composer empty (D-15), so a payload
        the composer cannot show is refused before its first keystroke. The numbers
        ride on the record and are persisted by the next save, adding no write of
        their own before typing."""
        checks = [p for p in self.preflights() if isinstance(p, PayloadTypable)]
        if not checks:
            return None
        target = run.target
        assert target is not None
        ready = next((s for s in reversed(run.samples) if s.composer_recognized), None)
        visible = ready.visible_rows if ready is not None else None
        width = self.readiness.pane_width(target)
        run.record.update(
            pane_width=width,
            visible_rows=visible,
            wrapped_rows=wrapped_rows(payload.text, width) if width else None,
        )
        if (
            checks
            and width is not None
            and visible is not None
            and wrapped_rows(payload.text, width) > visible - PayloadTypable.slack_rows
            and not run.walk_capability_verified
            and not walk_blockers(self.tmux_command, target, config)
        ):
            return self._refuse(
                run,
                "walk-unverified-version",
                unverified_detail(
                    target, config["multiline_mode"], run.capability_failure
                )
                or "capability check not confirmed before readiness",
            )
        for check in checks:
            verdict = check.fits_composer(
                payload, width, visible, target=target, config=config
            )
            if not verdict:
                return self._refuse(run, str(verdict.reason), verdict.detail)
        return None

    def _stage_text(
        self, run: _Run, payload: Payload, config: dict[str, Any]
    ) -> InjectionOutcome | None:
        """Stage once (an attempt never stages twice, ISSUE-REF), then require the
        composer to hold exactly that text before any Enter (ISSUE-REF).

        A ``multiline`` stage that fails after some keystrokes landed leaves a
        partial draft; its reason says so (``<send_result>-partial-draft``) and no
        Enter is ever pressed on it. Every refusal after text reached the composer
        goes through ``_refuse_staged``, which takes the text back out."""
        target = run.target
        assert target is not None
        prior = int(run.record.get("stagings") or 0)
        send_result = self.transport.stage(
            target,
            payload,
            chunk_pause_seconds=float(config["multiline_mode"]["chunk_pause_seconds"]),
        )
        progress = self.transport.progress
        partial = (
            send_result != "succeeded"
            and progress is not None
            and progress.keystrokes > 0
        )
        self.save(
            run,
            stagings=prior + 1,
            last_staged_text=payload.text,
            last_staged_at=now(),
        )
        run.trail.update(send_attempted=True, text_may_have_been_staged=True)
        stage_event = {
            "result": send_result,
            "mode": payload.mode,
            "chunks": payload.chunk_count,
            "lines": progress.lines if progress is not None else payload.line_count,
            "lines_typed": progress.lines_typed if progress is not None else None,
            "keystrokes": progress.keystrokes if progress is not None else None,
        }
        if send_result != "succeeded":
            self._run_event(run, "stage", stage_event)
            if send_result == "fence-mismatch" and not partial:
                self.save(run, stagings=prior)
                run.trail["text_may_have_been_staged"] = False
            run.trail["send_result"] = send_result
            if partial:
                assert progress is not None
                reason = f"{send_result}-partial-draft"
                if send_result == "text-send-error":
                    return self._refuse_staged(run, payload, reason)
                # The fence refused: the pane is no longer provably this attempt's,
                # so nothing is pressed to undo what was typed into it.
                return self._refuse(
                    run,
                    reason,
                    f"typed {progress.lines_typed} of {progress.lines} lines; "
                    "the partial draft is left in the composer and no Enter was pressed",
                )
            return self._refuse(run, send_result)
        staged = self.readiness.verify_staged(target, payload.text)
        run.samples.append(staged)
        run.phases.append("stage-verify")
        frame = staged.capture or getattr(self.readiness, "last_capture", "") or ""
        self._run_event(
            run,
            "stage",
            {
                **stage_event,
                "verify": staged.classification,
                "composer_recognized": staged.composer_recognized,
                "frame": self._run_capture(run, "stage-verify", frame),
            },
        )
        if _undrawn(staged):
            # The stage succeeded and the composer still reads empty: a loaded host has
            # not drawn the keystrokes yet, so wait for the draft before judging it
            # (ISSUE-REF), up to the late-frame bound (ISSUE-REF).
            staged = self.readiness.poll(
                target,
                lambda sample: not _undrawn(sample),
                KEYED_VIEW_TIMEOUT_SECONDS
                + float(stage_walk(config)["late_frame_seconds"]),
                phase="stage-verify",
                staged_text=payload.text,
            )
            run.samples.append(staged)
        if staged.composer_recognized and is_scrolled_window(
            staged.content, payload.text, head=False
        ):
            staged = self._verify_scrolled(run, target, payload.text, staged)
        if (
            staged.classification != "stage-verified"
            and staged.composer_recognized
            and not _undrawn(staged)
            and not walk_blockers(self.tmux_command, target, config)
        ):
            refused = self._walk(run, payload, config)
            if refused is None:
                self._pre_submit(run, "pre-submit-verified")
            return refused
        if staged.classification != "stage-verified":
            result = (
                "stage-verify-mismatch"
                if staged.composer_recognized
                else "stage-verify-no-composer"
            )
            run.trail["send_result"] = result
            self._frame_evidence(run, result, staged)
            return self._refuse_staged(
                run, payload, result, press=staged.composer_recognized
            )
        run.trail["send_result"] = "succeeded"
        self._pre_submit(run, "pre-submit-verified")
        return None

    def _pre_submit(self, run: _Run, verdict: str, reason: str | None = None) -> None:
        """The pre-submit verdict, once per attempt: verified (plain, scrolled head and
        tail, or walked) or failed with the refusal reason and the walk's divergence."""
        if "pre_submit" in run.trail:
            return
        run.trail["pre_submit"] = verdict
        # Persisted by the next save (the Enter's READY, or the refusal's).
        run.record["pre_submit_verdict"] = verdict
        self._run_event(
            run,
            "pre-submit",
            {
                "verdict": verdict,
                "reason": reason,
                "stage_verify": run.trail.get("stage_verify", "plain"),
                "divergence": run.record.get("walk_divergence"),
            },
        )

    # --- the walk (ISSUE-REF, design § The walk, D-16) --------------------------
    def press_up(self, target: SessionTarget) -> str:
        """The one way the library sends ``Up`` (ISSUE-REF). On the draft's first
        row Claude Code 2.1.296 answers ``Up`` by replacing the WHOLE draft with the
        previous history entry, which a later Enter would submit. So this reads the
        pane first and presses nothing (``UP_REFUSED_ON_FIRST_ROW``) while the cursor
        is, or cannot be proved not to be, on the composer's first visible row."""
        view = self.readiness.sample(target, phase="stage-verify")
        if cursor_on_first_row(walk_band(view.capture), self.readiness.cursor(target)):
            return UP_REFUSED_ON_FIRST_ROW
        return self.transport.press(target, "Up")

    def _walk_view(  # noqa: PLR0913 - one keyword per thing a stale frame is told by
        self,
        target: SessionTarget,
        walk: _Walk,
        *,
        expect: WalkBand | None = None,
        changed_from: WalkBand | None = None,
        quiet: float = 0.0,
        unmoved: _WalkView | None = None,
        patient: bool = False,
    ) -> _WalkView:
        """A settled capture: after the settle, captures until two in a row show the
        same band (or, given ``expect``, until one shows it), bounded. ``patient``, for a
        key whose frame must change, adds the walk's ``late`` seconds to that bound: a
        host that deschedules Claude Code draws such a frame later than 3 s, and a key
        pressed before it lands in the same frame (ISSUE-REF). Given
        ``changed_from``, the band from before a key that always redraws, a capture
        still showing that band is the old frame, not a settled one: on a loaded host
        the redraw lands late, and two stale captures read as "moved 0" (ISSUE-REF).
        Given ``quiet``, the band must also have held unchanged that many seconds, so
        a frame a loaded host holds back for less than that cannot read as settled
        (ISSUE-REF). Given ``unmoved``, the view from before a key that moves the
        cursor or the band, a capture showing the same band with the cursor on the same
        band row is the old frame too: a busy session draws an Up late before the band
        scrolls, where only the cursor moves (start-goal run 2026-10-24T233354Z). At the
        bound the last capture is returned, marked unsettled."""
        began = time.monotonic()
        time.sleep(walk.settle)
        deadline = (
            time.monotonic()
            + KEYED_VIEW_TIMEOUT_SECONDS
            + quiet
            + (walk.late if patient else 0.0)
        )
        previous: WalkBand | None = None
        since = time.monotonic()
        while True:
            sample = self.readiness.sample(
                target, phase="stage-verify", staged_text=walk.text
            )
            band = walk_band(sample.capture)
            cursor = self.readiness.cursor(target)
            view = _WalkView(sample, band, cursor)
            moment = time.monotonic()
            if band != previous:
                since = moment
            settled = (
                band == (expect if expect is not None else previous)
                and (changed_from is None or band != changed_from)
                and moment - since >= quiet
                and not (
                    unmoved is not None
                    and band == unmoved.band
                    and _band_cursor(view) == _band_cursor(unmoved)
                )
            )
            if (band is not None and settled) or moment >= deadline:
                view.waited = moment - began
                view.settled = band is not None and settled
                return view
            previous = band
            time.sleep(STAGE_VERIFY_POLL_SECONDS)

    def _walk_key(  # noqa: PLR0913 - the key and how its stale frame is told
        self,
        run: _Run,
        walk: _Walk,
        key: str,
        *,
        expect: WalkBand | None = None,
        changed_from: WalkBand | None = None,
        unmoved: _WalkView | None = None,
        patient: bool = False,
        quiet: float = 0.0,
    ) -> _WalkView | str:
        """Press ``key`` (``Up`` only through :meth:`press_up`); the settled view, or
        the press result when nothing was pressed or the press failed."""
        target = run.target
        assert target is not None
        result = (
            self.press_up(target) if key == "Up" else self.transport.press(target, key)
        )
        if result != "succeeded":
            return result
        if key == "Up":
            walk.ups += 1
        elif key == "Down":
            walk.downs += 1
        view = self._walk_view(
            target,
            walk,
            expect=expect,
            changed_from=changed_from,
            unmoved=unmoved,
            patient=patient,
            quiet=quiet,
        )
        if patient and view.settled:
            walk.slowest = max(walk.slowest, view.waited)
        run.samples.append(view.sample)
        run.phases.append("stage-walk")
        return view

    def _walk(
        self, run: _Run, payload: Payload, config: dict[str, Any]
    ) -> InjectionOutcome | None:
        """Prove a draft the composer clips, row by row, before Enter: trailing
        ``S-Enter`` filler until the tail is on screen, ``Up`` until row 0 is, ``Down``
        back to the end, then the stitched rows against the text. Every press is
        captured; a failure presses no Enter and undoes from the draft's end, filler
        included. Claude Code trims the filler from what it submits."""
        target = run.target
        assert target is not None
        if not run.walk_capability_verified:
            run.trail["send_result"] = "stage-verify-mismatch"
            return self._refuse_staged(
                run,
                payload,
                "walk-unverified-version",
                detail=unverified_detail(
                    target, config["multiline_mode"], run.capability_failure
                )
                or "capability check not confirmed before staging",
            )
        settings = stage_walk(config)
        walk = _Walk(
            payload.text,
            float(settings["capture_settle_seconds"]),
            late=float(settings["late_frame_seconds"]),
            quiet=float(settings["staged_quiet_seconds"]),
        )
        walk.listener = lambda label, text, entry: self._walk_capture(
            run, label, text, entry
        )
        # The staged view follows no key of the walk's own, so nothing it could differ
        # from proves it is the whole draft; a host still drawing the stage holds each
        # line's frame back, so the band must hold still longer than that (ISSUE-REF).
        quiet = float(settings["staged_quiet_seconds"])
        current = self._walk_view(target, walk, quiet=quiet)
        if _blank(current.band):
            # The stage always draws text: an empty band is a frame not drawn yet.
            current = self._walk_view(
                target, walk, quiet=quiet, changed_from=current.band, patient=True
            )
        run.samples.append(current.sample)
        run.phases.append("stage-walk")
        walk.frame("staged", current)
        if current.band is None or _blank(current.band):
            return self._walk_refused(
                run, payload, walk, "walk-stitch-mismatch", "no-draft-band"
            )
        walk.place(current.band, 0)
        end = self._walk_filler(run, walk, current, int(settings["max_filler_rows"]))
        if isinstance(end, tuple):
            return self._walk_refused(run, payload, walk, *end)
        failure = self._walk_travel(run, walk, end, int(settings["max_row_moves"]))
        if failure is not None:
            return self._walk_refused(run, payload, walk, *failure)
        return self._walk_verdict(run, payload, walk)

    def _walk_filler(
        self, run: _Run, walk: _Walk, current: _WalkView, limit: int
    ) -> _WalkView | tuple[str, str]:
        """Step 1: filler rows until the band shows the tail with a blank row below."""
        while current.band is None or not shows_tail(current.band, walk.text):
            if walk.filler >= limit:
                return "walk-tail-unreachable", f"filler={walk.filler}"
            # A soft newline always redraws the draft band, so wait out a late frame,
            # and press no further key until it is drawn: a second S-Enter pressed on
            # the old frame lands in the same frame as this one (ISSUE-REF).
            view = self._walk_key(
                run, walk, "S-Enter", changed_from=current.band, patient=True
            )
            if isinstance(view, str):
                return "walk-stitch-mismatch", f"filler-send: {view}"
            walk.filler += 1
            if not view.settled and view.band == current.band:
                walk.record("S-Enter", view, 0)
                return "walk-tail-unreachable", (
                    f"filler-unanswered: S-Enter {walk.filler} drew no new frame "
                    f"in {view.waited:.1f}s"
                )
            shift = (
                band_shift(current.band, view.band)
                if current.band is not None and view.band is not None
                else None
            )
            walk.record("S-Enter", view, shift)
            if (
                shift not in (0, 1)
                and walk.filler == 1
                and current.band is not None
                and view.band is not None
                and later_window(current.band, view.band, walk.text)
            ):
                # The first filler is pressed after every staged key, so its frame holds
                # the whole draft; a staged view taken while a stalled host was still
                # drawing the stage did not (ISSUE-REF).
                walk.restart(view.band)
                current = view
                continue
            if view.band is None or shift not in (0, 1):
                return (
                    "walk-stitch-mismatch",
                    f"filler-shift: {walk.filler} shift={shift}",
                )
            walk.place(view.band, shift)
            current = view
        return current

    def _walk_travel(
        self, run: _Run, walk: _Walk, end: _WalkView, limit: int
    ) -> tuple[str, str] | None:
        """Steps 2 and 3: the climb to row 0, then the walk back down."""
        assert end.band is not None
        bands = [end.band]
        failure = self._walk_climb(run, walk, end, bands, limit)
        return failure if failure is not None else self._walk_down(run, walk, bands)

    def _walk_climb(
        self,
        run: _Run,
        walk: _Walk,
        current: _WalkView,
        bands: list[WalkBand],
        limit: int,
    ) -> tuple[str, str] | None:
        """``Up`` until row 0 shows, proved by content or by the shared guard finding
        the cursor on the window's first row; ``bands`` collects the band after each
        press. Once the band has scrolled, an Up that moves neither the band nor the
        terminal cursor proves nothing: Claude Code 2.1.296 answers some Ups at a soft
        wrap that way mid-draft (ISSUE-REF), so the climb presses once more, and a
        second such Up in a row refuses ``walk-top-unproved``, never stitching."""
        scrolled = False
        inert = 0
        assert current.band is not None
        while not shows_row_zero(current.band, walk.text):
            if walk.ups >= limit:
                walk.climb_end = "move-limit"
                return "walk-top-unreachable", f"moves={walk.ups}"
            # Once the band has scrolled, an Up moves it a row, so on a loaded host wait
            # out a late frame before reading it as inert (ISSUE-REF), up to the late
            # bound: an Up pressed on the old frame lands in the same frame (ISSUE-REF).
            # A truly inert Up costs that bound and is still recorded as inert below.
            # Before the band scrolls an unmoved band is the ordinary climb, so there the
            # climb waits for the cursor instead, wherever the frame shows it on a band
            # row; where it does not, nothing on screen tells a late frame from an Up.
            unmoved = None if scrolled or _band_cursor(current) is None else current
            view = self._walk_key(
                run,
                walk,
                "Up",
                changed_from=current.band if scrolled else None,
                unmoved=unmoved,
                patient=scrolled or unmoved is not None,
                quiet=(
                    min(walk.late, max(walk.quiet, walk.slowest))
                    if not scrolled and unmoved is None
                    else 0.0
                ),
            )
            if view == UP_REFUSED_ON_FIRST_ROW:
                walk.climb_end = "first-row-cursor"
                return None
            if isinstance(view, str):
                walk.climb_end = "up-send-failed"
                return "walk-stitch-mismatch", f"up-send: {view}"
            shift = band_shift(current.band, view.band) if view.band else None
            # Before the first scroll every Up so far may have been held, unseen, and
            # land in this one frame (ISSUE-REF).
            reach = 1 if scrolled else walk.ups
            if shift is None and view.band is not None and reach > 1:
                shift = climb_shift(current.band, view.band, reach)
            walk.record("Up", view, shift)
            failure = _climb_jump(walk, current, view, shift, reach)
            if failure is not None:
                return failure
            assert view.band is not None and shift is not None
            walk.place(view.band, shift)
            _held_bands(walk, bands, view.band, -shift)
            bands.append(view.band)
            if scrolled and shift == 0 and view.cursor == current.cursor:
                walk.inert_ups.append(len(walk.keys))
                inert += 1
                if inert >= INERT_UPS_REFUSED:
                    walk.climb_end = "inert-up"
                    return "walk-top-unproved", (
                        f"Up {walk.ups}: band and cursor {view.cursor} unchanged "
                        f"{inert} times in a row after the band scrolled"
                    )
            else:
                inert = 0
            scrolled = scrolled or shift < 0
            current = view
            assert current.band is not None
        walk.climb_end = "row-zero-content"
        return None

    def _walk_down(
        self, run: _Run, walk: _Walk, bands: list[WalkBand]
    ) -> tuple[str, str] | None:
        """``Down`` once per ``Up``, each band the climb's in reverse ("what it saw for
        each cursor move down one row"), ending on the end band at text column 0."""
        current: _WalkView | None = None
        for step in range(1, walk.ups + 1):
            expect = bands[walk.ups - step]
            # The band this Down must show is known, so wait out a late frame for it.
            view = self._walk_key(run, walk, "Down", expect=expect, patient=True)
            if isinstance(view, str):
                return "walk-stitch-mismatch", f"down-send: {view}"
            walk.record("Down", view, None)
            if view.band != expect:
                return "walk-stitch-mismatch", f"down-not-retraced: Down {step}"
            current = view
        if current is not None and (
            current.cursor is None or current.cursor[0] != COMPOSER_GUTTER_COLUMNS
        ):
            return "walk-stitch-mismatch", f"end-cursor: {current.cursor}"
        return None

    def _walk_verdict(
        self, run: _Run, payload: Payload, walk: _Walk
    ) -> InjectionOutcome | None:
        """Step 4: the stitched rows, squeezed, against the intended text."""
        stitched = walk.stitched()
        if stitched is None:
            return self._walk_refused(
                run, payload, walk, "walk-stitch-mismatch", "row-seen-two-ways"
            )
        verdict = stitched_verdict(stitched, payload.text)
        fields = {**self._walk_fields(walk), "walk_rows": verdict.rows}
        if not verdict.matched:
            divergence = {
                "offset": verdict.offset,
                "row": verdict.row,
                "row_text": verdict.row_text,
                "expected": verdict.expected,
            }
            return self._walk_refused(
                run,
                payload,
                walk,
                "walk-stitch-mismatch",
                f"verdict-offset: offset={verdict.offset} row={verdict.row}",
                walk_divergence=divergence,
                walk_rows=verdict.rows,
            )
        self.save(run, stage_verify="walked", **fields)
        self._walk_frames(run, walk)
        run.trail.update(stage_verify="walked", send_result="succeeded")
        self._walk_stitch(run, walk, "walked", verdict.rows)
        self.stage(
            run,
            "WALK",
            f"result=walked stage_verify=walked rows={verdict.rows} "
            f"filler={walk.filler} moves={walk.ups} climb_end={walk.climb_end} "
            f"inert_ups={len(walk.inert_ups)} seconds={fields['walk_seconds']} "
            f"attempt={run.record['attempt_id']}",
        )
        return None

    @staticmethod
    def _walk_fields(walk: _Walk) -> dict[str, Any]:
        return {
            "walk_filler": walk.filler,
            "walk_moves": walk.ups,
            "walk_seconds": round(time.monotonic() - walk.started, 3),
            "walk_keys": walk.keys,
            "walk_stitch": walk.stitched(),
            "walk_climb_end": walk.climb_end,
            "walk_inert_ups": walk.inert_ups,
        }

    def _walk_refused(  # noqa: PLR0913 - the refusal's evidence rides along
        self,
        run: _Run,
        payload: Payload,
        walk: _Walk,
        reason: str,
        detail: str,
        **fields: Any,
    ) -> InjectionOutcome:
        """Refuse a walk: back to the draft's end (one Down for every Up not yet
        retraced), then the guarded undo with one more DEL per filler row."""
        target = run.target
        assert target is not None
        record = {**self._walk_fields(walk), "walk_rows": None, **fields}
        self.save(run, **record)
        self.stage(
            run,
            "WALK",
            f"result={reason} filler={walk.filler} moves={walk.ups} "
            f"climb_end={walk.climb_end} inert_ups={len(walk.inert_ups)} "
            f"seconds={record['walk_seconds']} attempt={run.record['attempt_id']} "
            f"detail={detail}",
        )
        run.trail["send_result"] = "stage-verify-mismatch"
        self._walk_frames(run, walk)
        self._walk_stitch(run, walk, reason, record.get("walk_rows"), detail)
        back = walk.ups - walk.downs
        returned = back == 0 or (
            self.transport.press(target, "Down", count=back) == "succeeded"
        )
        return self._refuse_staged(
            run, payload, reason, press=returned, filler=walk.filler, detail=detail
        )

    def _walk_capture(
        self, run: _Run, label: str, text: str, entry: dict[str, Any] | None
    ) -> None:
        """One walk frame into the run record: its capture file, and for a key press a
        ``walk-key`` event ("what it saw for each cursor move")."""
        name = (
            "walk-staged"
            if entry is None
            else f"walk-{entry['index']:03d}-{entry['key']}"
        )
        capture = self._run_capture(run, name, text)
        if entry is None:
            self._run_event(run, "walk-frame", {"label": label, "capture": capture})
        else:
            self._run_event(run, "walk-key", {**entry, "capture": capture})

    def _walk_stitch(  # noqa: PLR0913 - the stitch's whole record
        self,
        run: _Run,
        walk: _Walk,
        result: str,
        rows: int | None,
        detail: str = "",
    ) -> None:
        """How the bands were stitched together, and the result read from it."""
        self._run_event(
            run,
            "walk-stitch",
            {
                "result": result,
                "detail": detail or None,
                "stitch": walk.stitched(),
                "positional_rows": {str(k): v for k, v in sorted(walk.rows.items())},
                "top": walk.top,
                "conflict_row": walk.conflict,
                "rows": rows,
                "filler": walk.filler,
                "moves": walk.ups,
                "climb_end": walk.climb_end,
                "inert_ups": walk.inert_ups,
                "walk_seconds": round(time.monotonic() - walk.started, 3),
                "divergence": run.record.get("walk_divergence"),
            },
        )

    def _walk_frames(self, run: _Run, walk: _Walk) -> None:
        """Every capture the walk took, in the probe's frames-file shape: one
        ``=== <label>`` block per capture, each capture's lines as tmux printed them
        (a blank last pane row included), then ``=== end``, so the file never ends
        in the blank rows an end-of-file fixer would strip from committed evidence."""
        frames = "".join(walk.frames) + "=== end\n"
        path = self.observer.evidence(run.record, "walk", frames)
        if path is not None:
            run.evidence["walk"] = path
            self.save(run, walk_frames_path=str(path))

    def _frame_evidence(self, run: _Run, reason: str, sample: ComposerSample) -> None:
        """Keep the frame a stage-verify refusal was decided on, before the undo
        re-captures, and name it in the record's ``evidence_path``."""
        capture = sample.capture
        if capture is None:
            capture = getattr(self.readiness, "last_capture", "") or ""
        path = self.observer.evidence(run.record, reason, capture)
        if path is not None:
            run.evidence["frame"] = path
            self.save(run, evidence_path=str(path))

    def _refuse_staged(  # noqa: PLR0913 - keyword-only refusal options
        self,
        run: _Run,
        payload: Payload,
        reason: str,
        *,
        press: bool = True,
        filler: int = 0,
        detail: str = "",
    ) -> InjectionOutcome:
        """Refuse an attempt whose text is in the composer, and take it back out
        first (ISSUE-REF): left there, a human's typing appends to it and every
        later injection refuses ``composer-nonempty``.

        The undo is Ctrl+E then one DEL per character this attempt typed, each
        fenced like the stage. Never Ctrl+C: Claude Code reads it as a user
        interrupt and stops the session's background agents (ISSUE-REF, ISSUE-REF
        18:09Z). See ``_undo`` for how the DELs stay on this attempt's text. When
        the clear cannot be proved because the composer is hidden, recovery stays
        pending for a later tick. Other failures produce a notice for the human.
        ``press=False`` presses nothing and goes straight to that notice.
        ``filler`` soft newlines the walk typed below the text are deleted too."""
        target = run.target
        assert target is not None
        if not int(run.record.get("submissions") or 0):
            self._pre_submit(run, "pre-submit-failed", reason)
        typed = self._typed(payload) + "\n" * filler
        self._save_undo_remaining(run, typed)
        undo_failure = self._undo(run, target, typed) if press else None
        cleared = press and undo_failure is None
        if cleared:
            empty = self.readiness.poll(
                target,
                lambda sample: (
                    sample.composer_recognized
                    and sample.classification == "empty-composer"
                ),
                KEYED_VIEW_TIMEOUT_SECONDS,
                phase="precheck",
            )
            run.samples.append(empty)
            run.phases.append("stage-undo")
            cleared = (
                empty.composer_recognized and empty.classification == "empty-composer"
            )
        run.trail["stage_undo"] = "cleared" if cleared else "failed"
        deleted = int(run.trail.get("undo_deleted") or 0)
        lag_waits = int(run.trail.get("undo_lag_waits") or 0)
        frame = self._undo_frame(run)
        self.save(
            run,
            stage_undo_deleted_chars=deleted,
            stage_undo_lag_waits=lag_waits,
            stage_undo_failure_frame=frame,
        )
        self._run_event(
            run,
            "undo",
            {
                "result": run.trail["stage_undo"],
                "after": reason,
                "failure": undo_failure,
                "pressed": press,
                "typed_chars": len(typed),
                "deleted_chars": deleted,
                "remaining_chars": len(
                    run.record.get("stage_undo_remaining_text") or ""
                ),
                "lag_waits": lag_waits,
                "frame": frame,
                "filler": filler,
            },
        )
        because = f" reason={undo_failure}" if undo_failure else ""
        self.stage(
            run,
            "STAGE-UNDO",
            f"result={run.trail['stage_undo']}{because} deleted={deleted}/{len(typed)} "
            f"lag_waits={lag_waits} attempt={run.record['attempt_id']}",
        )
        if cleared:
            run.trail["text_may_have_been_staged"] = False
            self.save(run, staged_text_may_remain=False, stage_undo_remaining_text="")
            return self._refuse(run, reason, detail)
        if undo_failure:
            run.trail["stage_undo_reason"] = undo_failure
            self.save(run, stage_undo_reason=undo_failure)
        repair = (
            "python -m ai_chassis.prompt_injection.repair "
            f"--attempt {run.record['attempt_id']}"
        )
        self.save(run, staged_text_may_remain=True, stage_undo_repair_command=repair)
        detail = (
            f"after {reason}: {detail or reason}"
            + (f"; {undo_failure}" if undo_failure else "")
            + f"; injected text may remain; repair: {repair}"
        )
        if undo_failure == "composer-hidden":
            self.save(run, stage_undo_retries=0, refusal_detail=detail)
            self.stage(run, "PENDING-UNDO", f"attempt={run.record['attempt_id']}")
            return self._finish(run, "PENDING_UNDO", reason="stage-undo-failed")
        self.transport.warn(target, f"AIH: injected draft may remain; repair: {repair}")
        path = self.observer.notice(run.record, "stage-undo-failed")
        if path is not None:
            run.evidence["notice"] = path
        return self._refuse(run, "stage-undo-failed", detail)

    def _undo_frame(self, run: _Run) -> str | None:
        """Keep the capture a failed undo stopped on, as attempt evidence and in the
        run record; the kept path, or None when the undo did not stop on a frame."""
        text = run.trail.get("undo_frame")
        if text is None:
            return None
        self._run_capture(run, "undo-failed", text)
        path = self.observer.evidence(run.record, "undo-failed", text)
        if path is None:
            return None
        run.evidence["undo_frame"] = path
        return str(path)

    def _save_undo_remaining(self, run: _Run, text: str) -> None:
        self.save(
            run,
            stage_undo_remaining_text=text,
            stage_undo_remaining_digest=hashlib.sha256(text.encode()).hexdigest(),
        )

    def _typed(self, payload: Payload) -> str:
        """The text this attempt's keystrokes provably put in the composer: all of
        it, or for a ``multiline`` stage that stopped, the lines and soft newlines
        whose send-keys calls landed (the order ``_stage_lines`` sends them in)."""
        progress = self.transport.progress
        if payload.mode == "type" or progress is None:
            return payload.text
        keys: list[str] = []
        for index, line in enumerate(payload.text.split("\n")):
            keys += (["\n"] if index else []) + ([line] if line else [])
        return "".join(keys[: progress.keystrokes])

    def _undo(self, run: _Run, target: SessionTarget, typed: str) -> str | None:
        """Delete ``typed`` from the composer's end, at most ``_UNDO_CHUNK`` DELs at a
        time; None once the composer shows no draft, else why it stopped.

        Before the first DEL and after every chunk the composer is re-captured and
        must still end in text this attempt typed and has not yet deleted
        (:func:`undo_tail_holds`). If it does not, a human typed in the window: the
        undo stops, leaves the composer as it is and reports
        ``composer-changed-during-undo``, so it never deletes their text. A capture
        still ending in this attempt's text from before the last chunk is a frame the
        pane has not drawn yet, so the undo waits for it up to
        ``UNDO_LAG_TIMEOUT_SECONDS`` more and reports ``undo-not-drawn`` if it never
        comes. ``run.trail`` keeps the DELs sent, the lag waits and the stopping
        frame for the undo's record."""
        self._save_undo_remaining(run, typed)
        run.trail.update(undo_deleted=0, undo_lag_waits=0, undo_frame=None)
        if self.transport.press(target, "C-e") != "succeeded":
            return "key-send-failed"
        remaining = typed
        before: str | None = None
        while True:
            view = self._undo_view(run, target, remaining, before)
            if view.composer_recognized and not draft_rows(view.content):
                self._save_undo_remaining(run, "")
                return None
            failure = self._undo_failure(view, remaining, before)
            if failure is not None:
                run.trail["undo_frame"] = (
                    view.capture or getattr(self.readiness, "last_capture", "") or ""
                )
                return failure
            count = min(_UNDO_CHUNK, len(remaining))
            if self.transport.press(target, "BSpace", count=count) != "succeeded":
                return "key-send-failed"
            run.trail["undo_deleted"] += count
            before, remaining = remaining, remaining[:-count]
            self._save_undo_remaining(run, remaining)

    def _undo_view(
        self, run: _Run, target: SessionTarget, remaining: str, before: str | None
    ) -> ComposerSample:
        """The composer once it shows no draft or ends in ``remaining``, waiting past
        the keyed bound while it still ends in ``before``, the text the last chunk's
        DELs have not yet been drawn removing."""

        def done(sample: ComposerSample) -> bool:
            return sample.composer_recognized and (
                not draft_rows(sample.content)
                or (bool(remaining) and undo_tail_holds(sample.content, remaining))
            )

        def late(sample: ComposerSample) -> bool:
            return (
                before is not None
                and sample.composer_recognized
                and undo_tail_holds(sample.content, before)
            )

        view = self.readiness.poll(
            target, done, KEYED_VIEW_TIMEOUT_SECONDS, phase="precheck"
        )
        if not done(view) and late(view):
            run.trail["undo_lag_waits"] += 1
            view = self.readiness.poll(
                target,
                lambda sample: done(sample) or not late(sample),
                UNDO_LAG_TIMEOUT_SECONDS,
                phase="precheck",
            )
        run.samples.append(view)
        run.phases.append("stage-undo")
        return view

    @staticmethod
    def _undo_failure(
        view: ComposerSample, remaining: str, before: str | None
    ) -> str | None:
        """Why the undo cannot send its next chunk on ``view``, or None."""
        if not view.composer_recognized:
            return "composer-hidden"
        if remaining and undo_tail_holds(view.content, remaining):
            return None
        if before is not None and undo_tail_holds(view.content, before):
            return "undo-not-drawn"
        return "composer-changed-during-undo"

    def _verify_scrolled(
        self, run: _Run, target: SessionTarget, text: str, tail: ComposerSample
    ) -> ComposerSample:
        """The composer shows only the tail of a line it scrolled (ISSUE-REF). Move
        the cursor to the start (Ctrl+A) to read the head, back to the end (Ctrl+E)
        to read the tail again, and verify only when the two views share a row and
        together hold exactly the staged text. Both keys only move the cursor, and
        each is fenced like the stage; Enter then goes from the end, as always."""
        failed = tail
        if self.transport.press(target, "C-a") != "succeeded":
            return failed
        head = self.readiness.poll(
            target,
            lambda sample: is_scrolled_window(sample.content, text, head=True),
            KEYED_VIEW_TIMEOUT_SECONDS,
            phase="stage-verify",
            staged_text=text,
        )
        run.samples.append(head)
        run.phases.append("stage-verify-head")
        if self.transport.press(target, "C-e") != "succeeded":
            return failed
        back = self.readiness.poll(
            target,
            lambda sample: sample.content == tail.content,
            KEYED_VIEW_TIMEOUT_SECONDS,
            phase="stage-verify",
            staged_text=text,
        )
        run.samples.append(back)
        run.phases.append("stage-verify")
        if (
            back.content == tail.content
            and head.staged_style_safe
            and back.staged_style_safe
            and is_scrolled_window(head.content, text, head=True)
            and stitched_matches(head.content, back.content, text)
        ):
            run.trail["stage_verify"] = "scrolled-head-and-tail"
            return replace(back, classification="stage-verified")
        return failed

    def _submit(
        self, run: _Run, payload: Payload, timeout: float
    ) -> Landing | InjectionOutcome:
        """Enter, then the landing; a refusal (with its undo) when Enter itself could
        not be sent. Run under the pane lease, so the landing is judged before another
        run types."""
        target = run.target
        assert target is not None
        expect = Expectation.of(payload, run.record.get("landing_attachment"))
        landing = Landing("none", transcript_size(target), None, None)
        for submission in range(1, self.max_submissions + 1):
            target = run.target
            assert target is not None
            offset = transcript_size(target)
            run.trail.setdefault("offset_before_enter", offset)
            pressed = self._enter(run)
            if pressed in {PRE_SUBMIT_UNVERIFIED, "disabled"}:
                return self._refuse_staged(
                    run,
                    payload,
                    pressed,
                    filler=int(run.record.get("walk_filler") or 0),
                )
            entered = pressed == "succeeded"
            self.save(run, state="READY", submissions=submission, landing_offset=offset)
            self._submit_event(run, payload, submission, entered, offset)
            if not entered:
                run.trail["send_result"] = "enter-send-error"
                return self._refuse_staged(run, payload, "enter-send-error")
            landing = self._poll_landing(run, offset, timeout, expect)
            if landing.kind != "none":
                break
            # One further Enter, never a second staging (ISSUE-REF): only when the
            # composer still holds exactly the text this attempt staged.
            if submission == self.max_submissions:
                break
            still = self.readiness.sample(
                target, phase="stage-verify", staged_text=payload.text
            )
            run.samples.append(still)
            run.phases.append("stage-verify")
            if still.classification != "stage-verified":
                break
        return landing

    def _poll_landing(
        self, run: _Run, offset: int, timeout: float, expect: Expectation
    ) -> Landing:
        """A fresh session may publish its transcript only after Enter. Resolve the
        missing path within the landing budget, retaining the original pane identity
        and the pre-Enter offset; never adopt a replacement pane's transcript."""
        target = run.target
        assert target is not None
        deadline = time.monotonic() + max(0.0, timeout)
        while target.transcript_path is None:
            try:
                found = resolve_target(
                    f"pane:{target.pane_id}", tmux_command=self.tmux_command
                )
            except TargetUnresolved:
                found = None
            if found is not None and found.fingerprint == target.fingerprint:
                target = replace(
                    target,
                    transcript_path=found.transcript_path,
                    transcript_source=found.transcript_source,
                )
                if target.transcript_path is not None:
                    self._bind(run, target)
                    self.save(run)
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return Landing("none", offset, None, None)
            time.sleep(min(0.1, remaining))
        return self.verifier.landing(
            target, offset, max(0.0, deadline - time.monotonic()), expect
        )

    def _enter(self, run: _Run) -> str:
        """The one way the library presses Enter (D-19): only on a draft this attempt
        staged and proved (plain, scrolled head and tail, or walked); otherwise nothing
        is pressed and the result is ``pre-submit-unverified``."""
        target = run.target
        assert target is not None
        if run.trail.get("pre_submit") != PRE_SUBMIT_VERIFIED:
            return PRE_SUBMIT_UNVERIFIED
        if self.submission_gate is not None and not self.submission_gate():
            return "disabled"
        return "succeeded" if self.transport.submit(target) else "enter-send-error"

    def _submit_event(  # noqa: PLR0913 - one Enter's whole record
        self, run: _Run, payload: Payload, submission: int, entered: bool, offset: int
    ) -> None:
        self._run_event(
            run,
            "submit",
            {
                "key": "Enter",
                "submission": submission,
                "entered": entered,
                "offset_before_enter": offset,
                "text": payload.text,
                "mode": payload.mode,
                "filler": run.record.get("walk_filler") or 0,
            },
        )

    def _landed(
        self, run: _Run, landing: Landing, config: dict[str, Any]
    ) -> InjectionOutcome:
        record = run.record
        attempt_id = record["attempt_id"]
        run.trail["landing"] = landing.kind
        self._run_event(
            run,
            "landing",
            {
                "kind": landing.kind,
                "offset": landing.offset,
                "record_type": landing.record_type,
                "shape": landing.shape,
                "landed_at": landing.landed_at,
            },
        )
        fields = {
            "landing": landing.kind,
            "landing_offset": landing.offset,
            "landing_record_type": landing.record_type,
            **_shape_fields(landing),
        }
        post = self._post_submit(run, landing, config)
        if landing.kind in SUBMITTED_LANDINGS:
            self.save(
                run,
                state="SENT",
                sent_at=now(),
                landed_at=landing.landed_at,
                backoff_seconds=0,
                retry_not_before=0,
                reason=None,
                **fields,
            )
            self.stage(
                run,
                "SENT",
                f"attempt={attempt_id} landing={landing.kind}{_shape_suffix(landing)}",
            )
            self.observer.clear_notice(record)
            self.on_landing(record, landing)
            return self._execution(run, config)
        if landing.kind == "queued":
            self.save(run, state="QUEUED", sent_at=now(), reason=None, **fields)
            self.stage(run, "QUEUED", f"reason=queued attempt={attempt_id}")
            self.observer.clear_notice(record)
            return self._finish(run, "QUEUED", write=False)
        retry = self.retry_policy().fields(record, config)
        if landing.kind == "text":
            reason = self.text_reason
            self.save(run, reason=reason, **fields, **(retry or {}))
            self.stage(run, "CORRUPTED", f"reason={reason} attempt={attempt_id}")
            path = self.observer.notice(record, reason)
            if path is not None:
                run.evidence["notice"] = path
            self._debug(run, reason)
            self._transcript_debug(run, reason)
            return self._finish(
                run,
                "BOUND" if retry else "CORRUPTED",
                reason=reason,
                terminal=retry is None,
            )
        mismatched = post is not None and post.verdict == POST_SUBMIT_MISMATCH
        reason = "post-submit-mismatch" if mismatched else "landing-none"
        target = run.target
        if (
            landing.kind == "none"
            and target is not None
            and (target.transcript_path is None or not target.transcript_path.is_file())
        ):
            reason = "transcript-not-found"
        self.save(run, **fields, **(retry or {}))
        if retry:
            self.stage(run, "BOUND", f"reason={reason} attempt={attempt_id}")
            self._debug(run, reason)
            self._transcript_debug(run, reason)
            return self._finish(run, "BOUND", reason=reason)
        self.stage(run, "AMBIGUOUS", f"reason={reason} attempt={attempt_id}")
        self._debug(run, reason)
        self._transcript_debug(run, reason)
        return self._finish(run, "AMBIGUOUS", reason=reason, terminal=True)

    def _post_submit(
        self, run: _Run, landing: Landing, config: dict[str, Any]
    ) -> PostSubmitVerdict | None:
        """The D-19 post-submit check: what the transcript recorded against what was
        intended, recorded on the attempt and as a ``POST-SUBMIT`` stage line. None while
        ``validation.post_submit`` is off, which keeps today's landing-only record."""
        if not validation(config)["post_submit"] or run.target is None:
            return None
        verdict = PostSubmitValidator().verdict(
            run.target,
            landing,
            Expectation.from_record(run.record),
            int(run.trail.get("offset_before_enter", landing.offset)),
        )
        self.save(
            run, post_submit_verdict=verdict.verdict, post_submit=verdict.to_json()
        )
        self._run_event(run, "post-submit", verdict.to_json())
        # Every use case logs it, compact included (`CC-COMPACT-POST-SUBMIT`, the
        # parity matrix's verdict row, ISSUE-REF).
        offset = (verdict.divergence or {}).get("offset")
        at = f" divergence_offset={offset}" if offset is not None else ""
        self.stage(
            run,
            "POST-SUBMIT",
            f"verdict={verdict.verdict} attempt={run.record['attempt_id']} "
            f"landing={landing.kind}{at}",
        )
        return verdict

    def _execution(self, run: _Run, config: dict[str, Any]) -> InjectionOutcome:
        oracle = self.oracle()
        if oracle is None or run.target is None:
            return self._finish(run, "SENT", write=False)
        verdict = oracle.observe(
            run.target, run.record, float(config["completion_timeout_seconds"])
        )
        if verdict.state is None:
            return self._finish(run, "SENT", write=False)
        current = AttemptStore().find(str(run.record["attempt_id"])) or {}
        if current.get("state") not in {None, "SENT"}:
            # The host system (the PreCompact hook) advanced it; adopt, never overwrite.
            run.record.update(
                {k: v for k, v in current.items() if not k.startswith("_")}
            )
            return self._finish(run, str(current["state"]), write=False)
        self.save(run, oracle_detail=verdict.detail)
        self.stage(
            run, verdict.state, f"attempt={run.record['attempt_id']} {verdict.detail}"
        )
        return self._finish(run, verdict.state, terminal=verdict.state == "COMPLETED")

    def _refuse(
        self, run: _Run, reason: str, detail: str = "", *, stage: str | None = "REFUSED"
    ) -> InjectionOutcome:
        if stage is not None:
            if reason == "disabled":
                stage = "NOOP"
            elif reason in TRANSPORT_REASONS:
                stage = "TRANSPORT-UNAVAILABLE"
            suffix = f" detail={detail}" if detail else ""
            self.stage(
                run,
                stage,
                f"reason={reason} attempt={run.record['attempt_id']}{suffix}",
            )
        if reason in TRANSPORT_REASONS:
            path = self.observer.notice(run.record, reason)
            if path is not None:
                run.evidence["notice"] = path
        self.save(run, refusal_detail=detail or None)
        self._debug(run, reason)
        return self._finish(run, "REFUSED", reason=reason, terminal=True)

    def _debug(self, run: _Run, reason: str) -> None:
        if run.target is None:
            return
        capture = getattr(self.readiness, "last_capture", "") or self.transport.capture(
            run.target
        )
        path = self.observer.debug_capture(run.record, reason, capture)
        if path is not None:
            run.evidence["debug"] = path

    def _transcript_debug(self, run: _Run, reason: str) -> None:
        """The records the verifier saw after Enter (_write_transcript_debug_capture
        4021-4055 parity), bounded to the last 16 KiB, same gate as the pane capture."""
        target = run.target
        if target is None or target.transcript_path is None:
            return
        offset = int(run.trail.get("offset_before_enter", 0))
        try:
            with target.transcript_path.open("rb") as handle:
                handle.seek(offset)
                seen = handle.read()[-_TRANSCRIPT_CAPTURE_BYTES:]
        except OSError:
            return
        self.observer.debug_capture(
            run.record, f"{reason}-transcript", seen.decode("utf-8", errors="replace")
        )

    def _finish(
        self,
        run: _Run,
        state: str,
        *,
        reason: str | None = None,
        terminal: bool = False,
        write: bool = True,
    ) -> InjectionOutcome:
        if write:
            changes: dict[str, Any] = {"state": state, "reason": reason}
            if terminal:
                changes["terminal_at"] = now()
            self.save(run, **changes)
        if run.target is not None and run.samples:
            self.observer.readiness(run.record, run.samples, run.phases, run.trail)
        record = run.record
        self._run_event(
            run,
            "finish",
            {
                "state": record.get("state"),
                "reason": record.get("reason"),
                "use_case": self.use_case,
                "landing": record.get("landing"),
                "post_submit_verdict": record.get("post_submit_verdict"),
                "attempt_path": str(run.evidence.get("attempt") or ""),
            },
        )
        return InjectionOutcome(
            attempt_id=str(record["attempt_id"]),
            use_case=self.use_case,
            state=str(record["state"]),
            landing=record.get("landing"),
            reason=record.get("reason"),
            staged=int(record.get("stagings", 0)) > 0,
            submitted=int(record.get("submissions", 0)) > 0,
            evidence=dict(run.evidence),
            repair=record.get("capability_repair"),
        )

    def _new_record(
        self, attempt_id: str, key: str, payload: Payload
    ) -> dict[str, Any]:
        return {
            "key": key,
            "attempt_id": attempt_id,
            "attempt_type": self.attempt_type,
            "origin": "library",
            "use_case": self.use_case,
            "library_version": LIBRARY_VERSION,
            "state": "ARMED",
            "armed_at": now(),
            "pane_id": None,
            "pane_fingerprint": None,
            "transcript_path": None,
            "transcript_source": None,
            "cc_version": None,
            "payload_kind": payload.kind,
            "payload_digest": payload.digest,
            "command": payload.command,
            "args_digest": payload.args_digest,
            "sentinel": payload.sentinel,
            "mode": payload.mode,
            "lines": payload.line_count,
            "chars": len(payload.text),
            "chunks": payload.chunk_count,
            "stagings": 0,
            "submissions": 0,
            "last_staged_text": None,
            "landing": None,
            "landing_offset": None,
            "landed_at": None,
            "sent_at": None,
            "reason": None,
            "backoff_seconds": 0,
            "retry_not_before": 0,
            "terminal_at": None,
            "idle_proof": None,
            "superseded_staging_attempt_ids": [],
            "evidence_path": None,
            **self._run_fields(),
        }

    def _run_fields(self) -> dict[str, Any]:
        """The run identity every attempt carries (ISSUE-REF § the run record)."""
        run_id = self.run_id or new_run_id(self.use_case)
        return {
            "run_id": run_id,
            "parent_run_id": self.parent_run_id,
            "parent_attempt_id": self.parent_attempt_id,
            "run_record_path": str(run_record_path(run_id)),
            "stage_prefix": self.stage_prefix,
            "tmux_command": list(self.tmux_command),
            "resume_argv": self.resume_argv,
        }

    def _adopted_record(
        self, legacy: dict[str, Any], payload: Payload
    ) -> dict[str, Any]:
        """The legacy record with the library's identity fields added; every legacy
        field (origin, armed_at, stagings, retry state) wins over a library default."""
        record = self._new_record(
            str(legacy["attempt_id"]), str(legacy["key"]), payload
        )
        if "origin" not in legacy:
            del record["origin"]
        record.update(
            {
                k: v
                for k, v in legacy.items()
                if not k.startswith("_") and not (k == "run_id" and not v)
            }
        )
        record.update(
            use_case=self.use_case,
            library_version=LIBRARY_VERSION,
            payload_kind=payload.kind,
            payload_digest=payload.digest,
            command=payload.command,
            args_digest=payload.args_digest,
            sentinel=payload.sentinel,
            mode=payload.mode,
        )
        return record

    def _bind(self, run: _Run, target: SessionTarget) -> None:
        run.target = target
        run.record.update(
            key=target.key,
            pane_id=target.pane_id,
            pane_fingerprint=target.fingerprint,
            transcript_path=str(target.transcript_path)
            if target.transcript_path
            else None,
            transcript_source=target.transcript_source,
            cc_version=target.cc_version,
        )


class CompactInjector(Injector):
    """``/compact``, standing in for _bound_compact_transport + _submit_verified_compact.
    Logs the legacy ``CC-*`` stage names through :data:`COMPACT_STAGE_ALIASES`."""

    use_case = "compact"
    stage_prefix = "COMPACT"
    attempt_type = "compact"
    stage_aliases = COMPACT_STAGE_ALIASES
    text_reason = "compact-delivered-as-text"
    self_rescue = True

    def preflights(self):
        return [
            InjectionEnabled(self.use_case),
            TransportAvailable(self.tmux_command),
            SlashCommandInstalled(),
        ]

    def oracle(self):
        return PreCompactOracle()

    def retry_policy(self):
        return ExponentialBackoff()

    def default_payload(self):
        return SlashCommandPayload("/compact")


class GoalInjector(Injector):
    """``/goal <condition>`` for ``/start-goal``. One shot; a self target returns
    QUEUED and the caller runs :func:`verify` after its turn ends."""

    use_case = "start-goal"
    stage_prefix = "GOAL"
    attempt_type = "goal"

    def __init__(
        self,
        observers=None,
        *,
        goal_doc: Path | str | None = None,
        require_goal_inactive: bool = True,
        **kwargs,
    ):
        super().__init__(observers, **kwargs)
        self.goal_doc = Path(goal_doc) if goal_doc is not None else None
        self.require_goal_inactive = require_goal_inactive

    def hold_options(self):
        return {
            "goal_doc": str(self.goal_doc) if self.goal_doc is not None else None,
            "require_goal_inactive": self.require_goal_inactive,
        }

    def preflights(self):
        checks: list[Preflight] = [
            InjectionEnabled(self.use_case),
            TransportAvailable(self.tmux_command),
            SelfHealArmed(self.use_case),
            SlashCommandInstalled(),
            PayloadTypable(self.tmux_command),
        ]
        if self.require_goal_inactive:
            checks.append(GoalStateIs(inactive=True))
        return checks

    def oracle(self):
        return GoalActiveOracle()

    def on_landing(self, record, landing):
        super().on_landing(record, landing)
        if self.goal_doc is not None:
            record["goal_doc"] = str(self.goal_doc)


class SelfHealInjector(Injector):
    """The self-heal loop's ``/delegate <billing-default> /bug-fix`` prompt
    (ISSUE-REF, D-17), typed into the TARGET session whose run failed validation, under
    that run's id. Validated before and after Enter like any payload; its use case has
    no ``self_heal`` policy, so
    it is never self-healed itself."""

    use_case = "self-heal"
    stage_prefix = "SELF-HEAL-PROMPT"
    attempt_type = "self-heal"

    def preflights(self):
        return [
            InjectionEnabled(self.use_case),
            TransportAvailable(self.tmux_command),
            SlashCommandInstalled(),
            PayloadTypable(self.tmux_command),
        ]


class RepairEscalationInjector(Injector):
    """The prompt injected once when a signature's repair rounds are spent: the target
    session files the in-progress item for the maintainer. Validated, never self-healed."""

    use_case = "repair-escalation"
    stage_prefix = "REPAIR-ESCALATION"
    attempt_type = "repair-escalation"

    def preflights(self):
        return [
            InjectionEnabled(self.use_case),
            TransportAvailable(self.tmux_command),
            PayloadTypable(self.tmux_command),
        ]


DEFAULT_NUDGE = "Goal check-in {sentinel}: your /goal is still active; continue."


class IdleNudgeInjector(Injector):
    """A one-line plain prompt carrying an attempt sentinel; the ``prompt-record``
    landing is the proof. The caller's cadence owns retry."""

    use_case = "idle-nudge"
    stage_prefix = "IDLE-NUDGE"
    attempt_type = "idle-nudge"

    def __init__(self, observers=None, *, require_goal_active: bool = False, **kwargs):
        super().__init__(observers, **kwargs)
        self.require_goal_active = require_goal_active

    def hold_options(self):
        return {"require_goal_active": self.require_goal_active}

    @staticmethod
    def payload(template: str = DEFAULT_NUDGE) -> PromptPayload:
        sentinel = f"aih-{secrets.token_hex(4)}"
        if "{sentinel}" in template:
            return PromptPayload(
                template.replace("{sentinel}", sentinel), sentinel=sentinel
            )
        return PromptPayload(template)

    def preflights(self):
        checks: list[Preflight] = [
            InjectionEnabled(self.use_case),
            TransportAvailable(self.tmux_command),
            PayloadTypable(self.tmux_command),
        ]
        if self.require_goal_active:
            checks.append(GoalStateIs(inactive=False))
        return checks


#: The states a ``goal-clear`` or ``goal-loop-nudge`` run reports as done: the payload
#: landed, and for a clear maybe also the goal_status proving the goal ended.
GOAL_CLEARED_STATES = frozenset({"SENT", "STARTED", "COMPLETED"})


class _FailureNotice:
    """``notify_failure`` for the goal waker's two loop injections (ISSUE-REF)."""

    stage_prefix: str

    def notify_failure(self, outcome: InjectionOutcome, text: str) -> Path | None:
        """Leave ``text`` for the target session's next prompt when this run did not land.

        A notice this run already wrote (a transport refusal, a validation failure, a
        failed undo) is kept: it carries the run's own remedy. Nothing to write for a run
        that never resolved its target, since the notice is keyed by the target."""
        if outcome.state in GOAL_CLEARED_STATES:
            return None
        if "notice" in outcome.evidence:
            return Path(outcome.evidence["notice"])
        return self._notice(outcome.reason or outcome.state.lower(), text)

    def notify_exception(self, error: BaseException, text: str) -> Path | None:
        """``notify_failure`` for a run that raised instead of returning an outcome."""
        run = getattr(self, "_active", None)
        if run is not None and "notice" in run.evidence:
            return Path(run.evidence["notice"])
        return self._notice(f"exception:{type(error).__name__}", text)

    def _notice(self, reason: str, text: str) -> Path | None:
        run = getattr(self, "_active", None)
        if run is None or run.target is None:
            return None
        return write_notice(run.record, reason, text, heading=self.stage_prefix)


class GoalClearInjector(_FailureNotice, Injector):
    """``/goal clear`` for the goal waker's evaluator-loop clear (ISSUE-REF): a session
    whose declared terminal the native evaluator keeps rejecting. One shot; the waker's
    back-off owns any retry. ``require_goal_active`` refuses ``goal-not-active`` when the
    transcript's goal already ended, so a goal a human cleared is never cleared twice.
    Accepted race: a goal cleared in the seconds between that preflight and the
    submission gets a second ``/goal clear``, which Claude Code answers as no active goal,
    and the waker timer never overlaps itself; re-checking under the pane lease would
    reorder the Injector template's steps, which the library contract forbids.

    Self-rescue, so the save-state boundary never holds it: a goal the evaluator keeps
    re-firing stops the turn from ending, so no ``/compact`` can stage until the goal is
    cleared. Holding the clear until after compaction kept a frozen session stuck for hours
    (the goal-unblock outlet, ``skills/save-state/SKILL.md`` § Freeze point)."""

    use_case = "goal-clear"
    stage_prefix = "GOAL-CLEAR"
    attempt_type = "goal-clear"
    self_rescue = True

    def __init__(self, observers=None, *, require_goal_active: bool = False, **kwargs):
        super().__init__(observers, **kwargs)
        self.require_goal_active = require_goal_active

    def hold_options(self):
        return {"require_goal_active": self.require_goal_active}

    def preflights(self):
        checks: list[Preflight] = [
            InjectionEnabled(self.use_case),
            TransportAvailable(self.tmux_command),
            SlashCommandInstalled(),
            PayloadTypable(self.tmux_command),
        ]
        if self.require_goal_active:
            checks.append(GoalStateIs(inactive=False))
        return checks

    def oracle(self):
        return GoalClearedOracle()

    def retry_policy(self):
        return NoRetry()

    def default_payload(self):
        return SlashCommandPayload("/goal clear")


class GoalLoopNudgeInjector(_FailureNotice, IdleNudgeInjector):
    """The one-line follow-up after a ``goal-clear``: it tells the session to run
    ``/delegate <billing-default> /bug-fix`` on the goal text. Its own use case, so the
    ledger tells it apart from the stay-busy nudge."""

    use_case = "goal-loop-nudge"
    stage_prefix = "GOAL-LOOP-NUDGE"
    attempt_type = "goal-loop-nudge"


class InboxNudgeInjector(IdleNudgeInjector):
    """ISSUE-REF's inbox watcher: one harness-authored line naming the inbox and the
    message count, NEVER a message body (that travels as hook context)."""

    use_case = "inbox-nudge"
    stage_prefix = "INBOX-NUDGE"
    attempt_type = "inbox-nudge"

    @staticmethod
    def inbox_payload(inbox: Path, count: int) -> PromptPayload:
        sentinel = f"aih-{secrets.token_hex(4)}"
        return PromptPayload(
            f"Inbox {sentinel}: {count} new message(s) in {inbox}; read them there.",
            sentinel=sentinel,
        )


class _LegacyLineInjector(Injector):
    """One harness-authored line standing in for a legacy leg's ``fence_and_send``.
    One shot: the legacy leg's own budget, claim or cadence owns any retry."""

    def preflights(self):
        return [
            InjectionEnabled(self.use_case),
            TransportAvailable(self.tmux_command),
            PayloadTypable(self.tmux_command),
        ]


class RemoteLaunchInjector(_LegacyLineInjector):
    """The carried assignment is the first prompt of a sidecar-created session."""

    use_case = "remote-launch"
    stage_prefix = "REMOTE-LAUNCH"
    attempt_type = "remote-launch"
    ledger = "wake-attempts"
    max_submissions = 1


class SaveStateInjector(Injector):
    """``/save-state`` through the shared readiness, staging and landing checks.

    A command landing proves delivery only; the restart caller must wait for the
    subsequent compaction and re-check its idle and context gates.
    """

    use_case = "save-state"
    stage_prefix = "SAVE-STATE"
    attempt_type = "save-state"

    def preflights(self):
        return [
            InjectionEnabled(self.use_case),
            TransportAvailable(self.tmux_command),
            SlashCommandInstalled(),
            PayloadTypable(self.tmux_command),
        ]

    def default_payload(self):
        return SlashCommandPayload("/save-state")


class ContinuationInjector(_LegacyLineInjector):
    """``Resume: /auto-start <args>`` for ``arm_continuation``; the supervisor
    adopts its own continuation record and keeps the legacy state names."""

    use_case = "continuation"
    stage_prefix = "CONT"
    attempt_type = "continuation"
    self_rescue = True


class ReauthWakeInjector(_LegacyLineInjector):
    """``send_reauth_wake``'s line. Its records go to ``wake-attempts/``: the
    legacy wake writes nothing to ``attempts/``, whose every sent or refused
    record ``ResumeStore.permitted`` charges against the shared wake budget."""

    use_case = "reauth-wake"
    stage_prefix = "REAUTH"
    attempt_type = "reauth-wake"
    ledger = "wake-attempts"


def _epoch(value: object) -> float | None:
    """An attempt-record time: the library's epoch seconds, or an ISO-8601 string."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


class LaunchResumeInjector(_LegacyLineInjector):
    """The composer gate's typed fire (ISSUE-REF): one line, reached only through the
    gate's ``FIRING`` action in ``ai_chassis.launch_resume``. Same ledger rule as the
    reauth wake. Never a self-rescue fire, so a save-state-to-compact boundary holds it."""

    use_case = "launch-resume"
    stage_prefix = "LAUNCH-RESUME"
    attempt_type = "launch-resume"
    ledger = "wake-attempts"
    self_rescue = False
    #: A launch instance in one of these states wants no fire delivered for it.
    ENDED_STATES = frozenset({"SILENCED", "EXPIRED", "ABANDONED", "SKIPPED"})

    def __init__(
        self,
        observers=None,
        *,
        session_id: str | None = None,
        armed_at: str | None = None,
        **kwargs,
    ):
        """``session_id`` and ``armed_at`` name the launch instance the fire is for."""
        super().__init__(observers, **kwargs)
        self.session_id = session_id
        self.armed_at = armed_at

    def hold_options(self):
        return {"session_id": self.session_id, "armed_at": self.armed_at}

    def _instance_over(self, launch: dict[str, Any] | None) -> bool:
        """F13: the instance this fire was for has ended or been superseded."""
        if self.session_id is not None:
            if launch is None:
                return True
            if str(launch.get("session_id") or "") != self.session_id:
                return True
            if self.armed_at is not None and launch.get("armed_at") != self.armed_at:
                return True
        return launch is not None and launch.get("state") in self.ENDED_STATES

    def still_relevant(self, record: dict[str, Any]) -> bool:
        """False once the launch instance the fire was for ended or was replaced, or a
        resume was delivered for the key after ``held_at``: the gate's own launch record
        reached ``FIRED``, or the reboot-resume store consumed a record."""
        key = str(record.get("key") or "")
        if not key:
            return True
        root = resume_root()
        launch = _read_record(root / "launch" / f"{safe_component(key)}.json")
        if self._instance_over(launch):
            return False
        held_at = _epoch(record.get("held_at"))
        if held_at is None:
            return True
        if launch is not None and launch.get("state") == "FIRED":
            fired = _epoch(launch.get("fired_at"))
            if fired is not None and fired > held_at:
                return False
        consumed = root / "boot-stamps" / "consumed"
        for path in sorted(consumed.glob("*.json")) if consumed.is_dir() else []:
            found = _read_record(path)
            if found is None or found.get("key") != key:
                continue
            at = _epoch(found.get("consumed_at"))
            if at is not None and at > held_at:
                return False
        return True


class WorkQueueNudgeInjector(Injector):
    """``/wq next`` for the queue waker's Lane B; the command must resolve for
    the target session first (ISSUE-REF)."""

    use_case = "wq-nudge"
    stage_prefix = "WQ-NUDGE"
    attempt_type = "wq-nudge"

    def preflights(self):
        return [
            InjectionEnabled(self.use_case),
            TransportAvailable(self.tmux_command),
            SlashCommandInstalled(),
            PayloadTypable(self.tmux_command),
        ]


class SlackReplyInjector(Injector):
    """Verbatim owner text, admitted only through the signed reply contract."""

    use_case = "slack-reply"
    stage_prefix = "SLACK-REPLY"
    attempt_type = "slack-reply"

    def __init__(self, *, record=None, public_key=None, inbox=None, **kwargs):
        # Snapshot the record so a caller cannot change what was authenticated.
        self.reply_record = json.loads(json.dumps(record))
        self.reply_public_key = str(public_key) if public_key is not None else None
        self.reply_inbox = str(inbox) if inbox is not None else None
        gate = kwargs.pop("submission_gate", None)
        super().__init__(
            submission_gate=lambda: self._reply_active() and (gate is None or gate()),
            **kwargs,
        )

    def _reply_active(self):
        from ai_chassis.reply_router.signing import verify_reply

        if self.reply_record is None or self.reply_public_key is None:
            return False
        try:
            verify_reply(self.reply_record, Path(self.reply_public_key))
        except (OSError, ValueError):
            return False
        return True

    def preflights(self):
        return [
            InjectionEnabled(self.use_case),
            VerifiedSlackReply(
                self.reply_record, self.reply_public_key, self.reply_inbox
            ),
            TransportAvailable(self.tmux_command),
            SlashCommandInstalled(refusal_reason="command-unavailable"),
            PayloadTypable(self.tmux_command),
        ]

    def hold_options(self):
        return {
            "record": self.reply_record,
            "public_key": self.reply_public_key,
            "inbox": self.reply_inbox,
        }


#: The tmux session-name prefix a ``simulated-uat`` target must carry.
SIMULATED_UAT_SESSION_PREFIX = "simulated-uat-"


class SimulatedUatInjector(Injector):
    """An owner-style UAT prompt, typed verbatim into a disposable Claude Code session a
    UAT harness launched for the run (example-client-host-semantic-knowledge-graph's AI UAT tier,
    ES-1516). The landing is the ``prompt-record`` equal to the text, which Claude Code
    records as a typed human prompt; the harness then reads that session's own
    transcript for the tool calls the prompt caused. ``DisposableTarget`` refuses every
    session but a ``simulated-uat-*`` one that is not the caller's own."""

    use_case = "simulated-uat"
    stage_prefix = "SIMULATED-UAT"
    attempt_type = "simulated-uat"

    def preflights(self):
        return [
            InjectionEnabled(self.use_case),
            TransportAvailable(self.tmux_command),
            DisposableTarget(SIMULATED_UAT_SESSION_PREFIX),
            PayloadTypable(self.tmux_command),
        ]


class SessionReloadInjector(Injector):
    """A reload command such as ``/reload-plugins`` into an idle session whose loaded
    build is older than the installed one (ISSUE-REF). Claude Code runs it locally and
    starts no model turn. The landing is its ``command-record``; :class:`LocalCommandOracle`
    then reads the ``local_command`` result record and classifies its stdout. The
    ``ai_chassis.session_reload`` pass decides when to type it and owns every retry, so a
    record held at the save-state boundary is never re-delivered by the library."""

    use_case = "session-reload"
    stage_prefix = "SESSION-RELOAD"
    attempt_type = "session-reload"

    def preflights(self):
        return [
            InjectionEnabled(self.use_case),
            TransportAvailable(self.tmux_command),
            SlashCommandInstalled(),
            # A failed capability check leaves the walk off but never refuses this
            # short reload, which the plain stage verify proves (ISSUE-REF).
            PayloadTypable(self.tmux_command),
        ]

    def oracle(self):
        return LocalCommandOracle()

    def still_relevant(self, _record):
        return False, "pass-owns-retry"


#: Claude Code's confirmation when ``/exit`` meets live background tasks, recorded on
#: example-project-ec2 2026-10-12 (ISSUE-REF): option 1 is preselected, so one Enter confirms it.
EXIT_DIALOG_MARKER = "Exit and stop tasks"
#: The dialog's selected first option as drawn: the cursor, then ``1.`` and the marker.
_EXIT_DIALOG_OPTION = re.compile(r"^\s*\u276f\s*1\.\s*" + re.escape(EXIT_DIALOG_MARKER))
#: How far up from the pane's last drawn line the dialog's option row may sit.
_EXIT_DIALOG_TAIL_LINES = 12
#: Seconds a submitted ``/exit`` may take to end the agent. A busy session queues the
#: command and runs it when its turn ends, so this is a turn's length, not a keystroke's.
DEFAULT_EXIT_WAIT_SECONDS = 900.0
#: What a session-restart does when that confirmation appears: ``confirm`` stops the
#: background work (an operator-requested restart), ``stay`` keeps it and refuses (an
#: unattended update restart, ISSUE-REF, which must never interrupt running work).
EXIT_DIALOG_CHOICES = frozenset({"confirm", "stay"})
_EXIT_POLL_SECONDS = 1.0
#: How long a relaunched agent's pid must stay alive before the restart counts it.
RELAUNCH_STABLE_SECONDS = 3.0


def _pane_agent_pids(
    tmux_command: _tmux.Command, pane_id: str, names: frozenset[str]
) -> list[int]:
    """The pane's process and every descendant whose command name is in ``names``,
    oldest first. ``ps`` reads names and pids only, never arguments or environments."""
    root = (_tmux.display(tmux_command, pane_id, "#{pane_pid}") or "").strip()
    try:
        table = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        return []
    children: dict[str, list[tuple[str, str]]] = {}
    comm: dict[str, str] = {}
    for line in table.splitlines():
        parts = line.split(None, 2)
        if len(parts) == len(("pid", "ppid", "comm")):
            children.setdefault(parts[1], []).append((parts[0], parts[2]))
            comm[parts[0]] = parts[2]
    found: list[int] = []
    stack = [root] if root else []
    while stack:
        pid = stack.pop()
        if Path(comm.get(pid, "")).name in names:
            found.append(int(pid))
        stack.extend(child for child, _ in children.get(pid, []))
    return sorted(found)


def _alive(pid: int) -> bool:
    """Running, as opposed to gone or a zombie: an exited child its parent has not reaped
    yet still answers ``kill(pid, 0)``, so that probe alone would wait out the timeout
    (ISSUE-REF review of #2887). ``ps -o stat=`` reads the state letter only."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except OSError:
        return True
    return bool(state) and not state.startswith("Z")


def exit_dialog_shown(capture: str) -> bool:
    """Claude Code's background-task exit confirmation is the pane's live frame: its
    selected option row sits in the last drawn lines. The phrase quoted anywhere in the
    conversation above is not the dialog (ISSUE-REF review of #2887)."""
    lines = [line for line in capture.splitlines() if line.strip()]
    return any(
        _EXIT_DIALOG_OPTION.match(line) for line in lines[-_EXIT_DIALOG_TAIL_LINES:]
    )


def wrapper_resume_prompt_path(tmux_command: _tmux.Command, pane_id: str) -> Path:
    """The file the ai c wrapper hands its relaunched Claude Code as the first prompt:
    ``$XDG_STATE_HOME/ai-cli-utils/cc-resume-prompt-<tmux session>`` (read by the
    generated ``sessions/c-<name>.sh``, which deletes it once read).

    The wrapper interpolates the name raw, so the name is used raw here too and the two
    name the same file. ``ValueError`` for an empty name (no wrapper reads
    ``cc-resume-prompt-``) or one the shared session-key rule refuses: tmux accepts
    ``x/../y.z``, which would put the file outside ``ai-cli-utils/`` (ISSUE-REF)."""
    session = (_tmux.display(tmux_command, pane_id, "#{session_name}") or "").strip()
    if not session:
        raise ValueError(f"empty tmux session name for pane {pane_id!r}")
    reason = session_key_refusal(session)
    if reason is not None:
        raise ValueError(f"tmux session name {session!r} {reason}")
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "ai-cli-utils" / f"cc-resume-prompt-{session}"


def write_resume_prompt(path: Path, text: str) -> None:
    """Write-temp-then-rename, so the relaunched agent never reads a half-written prompt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


class RelaunchOracle(ExecutionOracle):
    """Post-submit proof that the restart happened: a NEW agent process in the pane,
    started by the hosting wrapper after the old one exited (D-19 post-submit check).

    The same new pid must stay alive for ``stable_seconds``. A process with the agent's
    name is not always the agent: the ai c launcher's relaunch hold runs ``claude agents
    --json`` in the pane, which lives well under a second, and taking it for the relaunch
    logged three parked sessions as restarted (ISSUE-REF)."""

    def __init__(
        self,
        tmux_command: _tmux.Command,
        names: frozenset[str],
        stable_seconds: float = RELAUNCH_STABLE_SECONDS,
    ) -> None:
        self.tmux_command = tmux_command
        self.names = names
        self.stable_seconds = stable_seconds

    def observe(self, target, attempt, timeout):
        old = {int(pid) for pid in attempt.get("restart_old_pids") or []}
        deadline = time.monotonic() + max(0.0, timeout)
        candidate, seen_at = None, 0.0
        while True:
            fresh = [
                pid
                for pid in _pane_agent_pids(
                    self.tmux_command, target.pane_id, self.names
                )
                if pid not in old
            ]
            now = time.monotonic()
            if candidate not in fresh:
                candidate, seen_at = (fresh[-1], now) if fresh else (None, 0.0)
            if candidate is not None and now - seen_at >= self.stable_seconds:
                return OracleVerdict("COMPLETED", f"relaunched pid={candidate}")
            remaining = deadline - now
            if remaining <= 0:
                detail = "no relaunched agent in the pane"
                if candidate is not None:
                    detail += f" (pid={candidate} not yet alive {self.stable_seconds}s)"
                return OracleVerdict(None, detail)
            time.sleep(min(_EXIT_POLL_SECONDS, remaining))


class SessionRestartInjector(Injector):
    """``/exit`` into a session whose host relaunches it, so the exit is a restart
    (ISSUE-REF). Loads what only a process start reads, such as a newly registered MCP
    server. The ai c wrapper relaunches ``claude --resume`` after a normal exit and hands
    the new process its ``cc-resume-prompt-<tmux session>`` file as the first prompt.

    Pre-submit: the shared readiness and stage-verify path, then the resume prompt
    written atomically. Post-submit: the agent's pid exits (Claude Code's background-task
    dialog confirmed by its content, never a blind Enter), then :class:`RelaunchOracle`
    sees a new agent. A refusal after the prompt was written removes it, so a later
    ordinary restart is not handed a stale instruction. Never run this from inside the
    target's own process tree: confirming the dialog stops that tree's tasks."""

    use_case = "session-restart"
    stage_prefix = "RESTART"
    attempt_type = "session-restart"

    def __init__(  # noqa: PLR0913 - the restart's keyword-only options
        self,
        observers=None,
        *,
        resume_prompt: str | None = None,
        resume_prompt_path: Path | str | None = None,
        agent_names: Iterable[str] = ("claude",),
        exit_wait: float = DEFAULT_EXIT_WAIT_SECONDS,
        exit_dialog: str = "confirm",
        **kwargs,
    ):
        super().__init__(observers, **kwargs)
        if exit_dialog not in EXIT_DIALOG_CHOICES:
            raise ValueError(
                f"exit_dialog must be one of {sorted(EXIT_DIALOG_CHOICES)}"
            )
        self.exit_dialog = exit_dialog
        self.resume_prompt = resume_prompt
        self.resume_prompt_path = (
            Path(resume_prompt_path) if resume_prompt_path is not None else None
        )
        self.agent_names = frozenset(agent_names)
        self.exit_wait = exit_wait

    def preflights(self):
        return [
            InjectionEnabled(self.use_case),
            TransportAvailable(self.tmux_command),
            SlashCommandInstalled(),
            PayloadTypable(self.tmux_command),
        ]

    def oracle(self):
        return RelaunchOracle(self.tmux_command, self.agent_names)

    def default_payload(self):
        return SlashCommandPayload("/exit")

    def default_landing_timeout(self):
        # How long /exit may take to end the agent: a turn's length, so the library's
        # keystroke-sized landing default does not apply.
        return self.exit_wait

    def _submit(self, run, payload, timeout):
        target = run.target
        assert target is not None
        old = _pane_agent_pids(self.tmux_command, target.pane_id, self.agent_names)
        refused = "no-agent-process" if not old else self._prompt_path_refusal(run)
        if refused is not None:
            return self._refuse_staged(run, payload, refused)
        self.save(run, restart_old_pids=old)
        prompt = self.resume_prompt_path
        if prompt is not None and self.resume_prompt is not None:
            write_resume_prompt(prompt, self.resume_prompt)
            self.stage(run, "PROMPT", f"path={prompt} chars={len(self.resume_prompt)}")
        offset = transcript_size(target)
        run.trail.setdefault("offset_before_enter", offset)
        pressed = self._enter(run)
        if pressed == PRE_SUBMIT_UNVERIFIED:
            self._drop_prompt()
            return self._refuse_staged(run, payload, pressed)
        entered = pressed == "succeeded"
        self.save(run, state="READY", submissions=1, landing_offset=offset)
        self._submit_event(run, payload, 1, entered, offset)
        if not entered:
            self._drop_prompt()
            return self._refuse_staged(run, payload, "enter-send-error")
        confirmed = False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(_alive(pid) for pid in old):
                self.stage(run, "EXITED", f"pids={','.join(map(str, old))}")
                return Landing("process-exit", offset, int(time.time()), None)
            if not confirmed and self._dialog_open(target):
                self.stage(run, "DIALOG", "background-task exit confirmation")
                if self.exit_dialog == "stay":
                    return self._stay(run, target)
                confirmed = self._enter(run) == "succeeded"
                self.save(run, exit_dialog_confirmed=confirmed)
            time.sleep(_EXIT_POLL_SECONDS)
        self._drop_prompt()
        return Landing("none", offset, None, None)

    def _prompt_path_refusal(self, run: _Run) -> str | None:
        """Resolve the wrapper's prompt file when a prompt is to be handed over, or the
        refusal reason when the tmux session name cannot name it (ISSUE-REF)."""
        if self.resume_prompt is None or self.resume_prompt_path is not None:
            return None
        assert run.target is not None
        try:
            self.resume_prompt_path = wrapper_resume_prompt_path(
                self.tmux_command, run.target.pane_id
            )
        except ValueError as error:
            self.stage(run, "PROMPT-REFUSED", str(error))
            return "resume-prompt-path-refused"
        return None

    def _dialog_open(self, target: SessionTarget) -> bool:
        capture = self.transport.capture(target)
        return exit_dialog_shown(capture) and not self._composer_drawn(target)

    def _stay(self, run: _Run, target: SessionTarget) -> InjectionOutcome:
        """The session still owns background work, so leave it running: Escape is the
        dialog's cancel (its ``onCancel``, measured in the 2.1.299 binary), which keeps
        the session open with every task intact. Never Enter, which stops them."""
        pressed = self.transport.press(target, "Escape")
        self._drop_prompt()
        self.save(run, exit_dialog_stayed=pressed == "succeeded")
        self.stage(run, "DIALOG-STAY", f"escape={pressed}")
        return self._refuse(run, "exit-dialog-stayed", f"escape={pressed}")

    def _composer_drawn(self, target: SessionTarget) -> bool:
        """The composer frame is on screen, so whatever matched above is conversation
        text, not the dialog (which replaces the composer while it is open)."""
        return bool(self.readiness.sample(target).composer_recognized)

    def _drop_prompt(self) -> None:
        if self.resume_prompt_path is not None and self.resume_prompt is not None:
            self.resume_prompt_path.unlink(missing_ok=True)


INJECTORS: dict[str, type[Injector]] = {
    SaveStateInjector.use_case: SaveStateInjector,
    SessionRestartInjector.use_case: SessionRestartInjector,
    GoalInjector.use_case: GoalInjector,
    SelfHealInjector.use_case: SelfHealInjector,
    RepairEscalationInjector.use_case: RepairEscalationInjector,
    IdleNudgeInjector.use_case: IdleNudgeInjector,
    GoalClearInjector.use_case: GoalClearInjector,
    GoalLoopNudgeInjector.use_case: GoalLoopNudgeInjector,
    InboxNudgeInjector.use_case: InboxNudgeInjector,
    CompactInjector.use_case: CompactInjector,
    ContinuationInjector.use_case: ContinuationInjector,
    ReauthWakeInjector.use_case: ReauthWakeInjector,
    LaunchResumeInjector.use_case: LaunchResumeInjector,
    RemoteLaunchInjector.use_case: RemoteLaunchInjector,
    WorkQueueNudgeInjector.use_case: WorkQueueNudgeInjector,
    SlackReplyInjector.use_case: SlackReplyInjector,
    SimulatedUatInjector.use_case: SimulatedUatInjector,
    SessionReloadInjector.use_case: SessionReloadInjector,
}


def _shape_fields(landing: Landing) -> dict[str, str]:
    """``landing_shape`` names a landing proven by other than the command's own
    record (the native enqueue echo, ISSUE-REF); absent otherwise."""
    return {"landing_shape": landing.shape} if landing.shape else {}


def _shape_suffix(landing: Landing) -> str:
    return f" shape={landing.shape}" if landing.shape else ""


def verify(attempt_id: str) -> InjectionOutcome:
    """Second phase for a QUEUED attempt: re-poll from its ``landing_offset``.

    ``command-record``/``prompt-record`` -> SENT; a turn boundary with no command
    record -> BOUND ``queue-dropped``; nothing new -> still QUEUED. Any other
    state is returned as recorded. Raises ``LookupError`` for an unknown id.
    """
    store = AttemptStore()
    record = store.find(attempt_id)
    if record is None:
        raise LookupError(f"no attempt {attempt_id}")
    record.pop("_path", None)
    injector = INJECTORS.get(str(record.get("use_case")), GoalInjector)()
    path = record.get("transcript_path")
    target = SessionTarget(
        key=str(record["key"]),
        pane_id=str(record.get("pane_id") or ""),
        fingerprint=str(record.get("pane_fingerprint") or ""),
        transcript_path=Path(path) if path else None,
        cwd=None,
        transcript_source=record.get("transcript_source"),
    )
    run = _Run(record, target)
    if record.get("state") != "QUEUED":
        return injector._finish(
            run, str(record["state"]), reason=record.get("reason"), write=False
        )
    landing = injector.verifier.landing(
        target,
        int(record.get("landing_offset") or 0),
        0,
        Expectation.from_record(record),
        queued=True,
    )
    if landing.kind in SUBMITTED_LANDINGS:
        injector.save(
            run,
            state="SENT",
            landing=landing.kind,
            landing_offset=landing.offset,
            landed_at=landing.landed_at,
            landing_record_type=landing.record_type,
            **_shape_fields(landing),
        )
        run.trail["offset_before_enter"] = int(record.get("landing_offset") or 0)
        injector._post_submit(run, landing, load_config())
        injector._active = run
        injector.stage(
            run,
            "SENT",
            f"attempt={attempt_id} landing={landing.kind}{_shape_suffix(landing)}"
            " via=verify",
        )
        return injector._self_heal(injector._finish(run, "SENT", write=False))
    if landing.kind in {"queue-dropped", "text"}:
        reason = (
            "queue-dropped" if landing.kind == "queue-dropped" else injector.text_reason
        )
        retry = injector.retry_policy().fields(record, load_config()) or {}
        injector.save(run, landing=landing.kind, landing_offset=landing.offset, **retry)
        injector.stage(run, "BOUND", f"reason={reason} attempt={attempt_id} via=verify")
        return injector._finish(run, "BOUND", reason=reason)
    injector.save(run, landing_offset=landing.offset)
    elapsed = now() - int(record.get("sent_at") or now())
    injector.stage(
        run, "QUEUED", f"reason=queued attempt={attempt_id} elapsed_s={elapsed}"
    )
    return injector._finish(run, "QUEUED", write=False)
