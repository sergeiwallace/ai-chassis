"""The fallback rule a legacy call site applies once its ``library_cutover`` switch
is on (D-8 and its 2026-10-11 Amendment): run the library; fall back to the legacy
path ONLY when the library pressed no Enter AND its refusal was a transport one.
An unobserved success plus a legacy retry is a double submission, and a legacy
send after a pane-judgement refusal types over what that refusal protected."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ai_chassis.prompt_injection.observe import (
    CompositeObserver,
    parity_observers,
    stage_name,
)
from ai_chassis.prompt_injection.outcomes import (
    LIBRARY_EXCEPTION_PREFIX,
    TRANSPORT_REFUSALS,
    InjectionOutcome,
)


def _transport_refusal(outcome: InjectionOutcome) -> bool:
    reason = outcome.reason or ""
    if reason.startswith(LIBRARY_EXCEPTION_PREFIX):
        return not outcome.staged
    return reason in TRANSPORT_REFUSALS


def run_with_cutover(
    run_library: Callable[[], InjectionOutcome],
    run_legacy: Callable[[], Any],
    *,
    stage_prefix: str,
    key: str,
) -> tuple[InjectionOutcome, Any | None]:
    """Return the library outcome and the legacy result (None when it did not run).

    ``submitted`` decides first, whatever the landing: once an Enter has gone, the
    legacy path never runs for that cycle. A ``HELD`` outcome never falls back
    either: the library holds it across the save-state-to-compact boundary and its
    drain delivers it, so a legacy send now would type into exactly the window the
    hold protects (ISSUE-REF). Otherwise the reason's class decides: a
    ``TRANSPORT_REFUSALS`` reason (or a call site's ``library-exception:`` before
    anything was staged) is one countable ``CC-<PREFIX>-LIB-FALLBACK`` line and the
    legacy send; any other reason is one ``CC-<PREFIX>-LIB-REFUSED`` line and no
    legacy send. ``fallbacks`` counts both.
    """
    outcome = run_library()
    if outcome.submitted or outcome.state == "HELD":
        return outcome, None
    fall_back = _transport_refusal(outcome)
    CompositeObserver(parity_observers()).stage(
        stage_name(stage_prefix, "LIB-FALLBACK" if fall_back else "LIB-REFUSED"),
        key,
        f"reason={outcome.reason} attempt={outcome.attempt_id}",
    )
    if not fall_back:
        return outcome, None
    return outcome, run_legacy()
