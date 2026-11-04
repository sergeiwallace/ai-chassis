"""The composer gate: a pure state machine for one launch instance (ISSUE-REF, D-8).

Design: ``docs/designs/issue-ref-typing-aware-launch-resume.md`` § Composer gate. The watcher
in :mod:`ai_chassis.launch_resume` samples the composer, reads the submit and end markers, and
feeds :meth:`Gate.step` one observation at a time with the time it was made; the gate answers
with its new state and the actions to execute. No I/O, no sleeping, no tmux here, so every row
of the table is testable with plain values.

The gate holds while text is in the composer, re-arms ``rearm_after_clear_seconds`` after the
box is seen empty again, fires once ``first_fire_delay_seconds`` after arming (a floor, not a
window), and stands down for this launch the moment a prompt is submitted by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ARMED = "ARMED"
PAUSED = "PAUSED"
REARMING = "REARMING"
FIRING = "FIRING"
FIRED = "FIRED"
HELD = "HELD"
SILENCED = "SILENCED"
EXPIRED = "EXPIRED"
ABANDONED = "ABANDONED"
SKIPPED = "SKIPPED"

WAITING = frozenset({ARMED, PAUSED, REARMING})
TERMINAL = frozenset({FIRED, HELD, SILENCED, EXPIRED, ABANDONED, SKIPPED})
STATES = WAITING | TERMINAL | {FIRING}

STAGE_PREFIX = "CC-LAUNCH-RESUME-"
#: Refusals that say the composer was busy at the instant of the send: a human typed.
BUSY_REFUSALS = frozenset({"composer-nonempty", "composer-suggestion"})
#: Refusals that say the fire was switched off rather than failed.
OFF_REFUSALS = frozenset({"disabled", "typed-off"})


@dataclass(frozen=True)
class GateConfig:
    first_fire_delay_seconds: float
    rearm_after_clear_seconds: float
    max_wait_seconds: float
    typed_fallback_enabled: bool

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> GateConfig:
        """From a validated ``launch_resume`` config object."""
        return cls(
            float(settings["first_fire_delay_seconds"]),
            float(settings["rearm_after_clear_seconds"]),
            float(settings["max_wait_seconds"]),
            bool(settings["typed_fallback_enabled"]),
        )


@dataclass(frozen=True)
class Observation:
    kind: str
    reason: str | None = None

    @property
    def label(self) -> str:
        base = self.kind.replace("_", "-")
        return f"{base}:{self.reason}" if self.reason else base


COMPOSER_EMPTY = Observation("composer_empty")
COMPOSER_TEXT = Observation("composer_text")
COMPOSER_UNOBSERVABLE = Observation("composer_unobservable")
MANUAL_SUBMIT = Observation("manual_submit")
OWN_SUBMIT = Observation("own_submit")
SESSION_ENDED = Observation("session_ended")
FIRE_SENT = Observation("fire_sent")
FIRE_HELD = Observation("fire_held")
SIMPLE_OBSERVATIONS = (
    COMPOSER_EMPTY,
    COMPOSER_TEXT,
    COMPOSER_UNOBSERVABLE,
    MANUAL_SUBMIT,
    OWN_SUBMIT,
    SESSION_ENDED,
    FIRE_SENT,
    FIRE_HELD,
)
_KINDS = frozenset({o.kind for o in SIMPLE_OBSERVATIONS} | {"fire_refused"})
_OUTCOMES = frozenset({"fire_sent", "fire_held", "fire_refused"})


def fire_refused(reason: str) -> Observation:
    return Observation("fire_refused", reason)


@dataclass(frozen=True)
class Action:
    """``log`` a stage line, ``deliver`` the typed fire, or ``exit`` the watcher."""

    kind: str
    stage: str = ""
    detail: str = ""


@dataclass(frozen=True)
class Transition:
    previous: str
    state: str
    observation: Observation
    actions: tuple[Action, ...] = ()

    @property
    def changed(self) -> bool:
        return self.previous != self.state


def _log(stage: str, detail: str = "") -> Action:
    return Action("log", STAGE_PREFIX + stage, detail)


_EXIT = Action("exit")
_DELIVER = Action("deliver")


class Gate:
    def __init__(
        self,
        config: GateConfig,
        armed_at: float,
        *,
        state: str = ARMED,
        rearm_at: float | None = None,
    ) -> None:
        """``state`` and ``rearm_at`` let a watcher resume a record another one left."""
        if state not in STATES:
            raise ValueError(f"unknown gate state {state!r}")
        self.config = config
        self.armed_at = armed_at
        self.deadline = armed_at + config.first_fire_delay_seconds
        self.expires_at = armed_at + config.max_wait_seconds
        self.state = state
        self.rearm_at = rearm_at

    def step(self, observation: Observation, now: float) -> Transition:
        if observation.kind not in _KINDS:
            raise ValueError(f"unknown observation {observation.kind!r}")
        previous = self.state
        if previous in TERMINAL:
            return Transition(previous, previous, observation)
        if previous == FIRING:
            actions = self._outcome(observation, now)
        else:
            actions = self._waiting(observation, now)
        return Transition(previous, self.state, observation, actions)

    # --- waiting states: ARMED, PAUSED, REARMING ---------------------------------------
    def _waiting(self, observation: Observation, now: float) -> tuple[Action, ...]:
        kind = observation.kind
        if kind in _OUTCOMES or kind == "own_submit":
            return ()
        if kind == "session_ended":
            return self._end(ABANDONED, _log("ABANDONED"))
        if kind == "manual_submit":
            return self._end(SILENCED, _log("SILENCED", "reason=manual-submit"))
        if now >= self.expires_at:
            return self._end(EXPIRED, _log("EXPIRED", f"state={self.state}"))
        sampled = {"composer_text": self._text, "composer_empty": self._empty}.get(kind)
        return sampled(now) if sampled is not None else ()

    def _text(self, _now: float) -> tuple[Action, ...]:
        if self.state == PAUSED:
            return ()
        self.state, self.rearm_at = PAUSED, None
        return (_log("PAUSED", "reason=composer-text"),)

    def _empty(self, now: float) -> tuple[Action, ...]:
        if self.state == PAUSED:
            return self._rearm(now, _log("REARMED", self._rearm_detail()))
        due = self.deadline if self.state == ARMED else self.rearm_at
        if due is None or now < due:
            return ()
        if not self.config.typed_fallback_enabled:
            return self._end(SKIPPED, _log("SKIP", "reason=typed-off source=config"))
        self.state = FIRING
        return (_DELIVER,)

    # --- FIRING: the injector's outcome decides --------------------------------------------
    def _outcome(self, observation: Observation, now: float) -> tuple[Action, ...]:
        kind, reason = observation.kind, observation.reason
        if kind == "fire_sent":
            return self._end(FIRED, _log("FIRED", "leg=typed"))
        if kind == "fire_held":
            return self._end(HELD, _log("HELD"))
        if kind != "fire_refused":
            return ()
        if reason in BUSY_REFUSALS:
            self.state, self.rearm_at = PAUSED, None
            return (_log("PAUSED", f"reason={reason} at=send"),)
        if reason in OFF_REFUSALS:
            return self._end(SKIPPED, _log("SKIP", "reason=typed-off source=library"))
        return self._rearm(
            now, _log("RETRY", f"reason={reason} {self._rearm_detail()}")
        )

    # --- helpers ------------------------------------------------------------------------
    def _rearm_detail(self) -> str:
        return f"rearm_after_s={self.config.rearm_after_clear_seconds:g}"

    def _rearm(self, now: float, log: Action) -> tuple[Action, ...]:
        self.state = REARMING
        self.rearm_at = now + self.config.rearm_after_clear_seconds
        return (log,)

    def _end(self, state: str, log: Action) -> tuple[Action, ...]:
        self.state = state
        return (log, _EXIT)
