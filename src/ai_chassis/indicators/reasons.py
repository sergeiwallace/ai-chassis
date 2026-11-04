"""The one raw-reason to ``blocked_on`` table (design § Sources and adapters)."""

from __future__ import annotations

from fnmatch import fnmatchcase

from ai_chassis.indicators.model import BlockedOn

# Library and reload-framework reasons, as the design lists them, then the goal waker's own
# skip reasons for the same conditions (`not-idle`, `human-gate`), which its state file records.
_TABLE: tuple[tuple[tuple[str, ...], BlockedOn], ...] = (
    (
        (
            "composer-nonempty",
            "composer-suggestion",
            "stage-verify-mismatch",
            "waiting-draft",
        ),
        "draft",
    ),
    (
        (
            "status-busy",
            "status-not-settled",
            "transcript-*-busy",
            "injection-in-flight",
            "waiting-busy",
            "not-idle",
        ),
        "busy",
    ),
    (
        ("status-waiting", "transcript-*-question", "waiting-question", "human-gate"),
        "question",
    ),
    (("boundary-held", "waiting-boundary"), "boundary"),
    (("backoff",), "backoff"),
    (("not-ready", "pane-in-mode"), "not-ready"),
)

#: The classes a retrying injector waits out; ``other`` is not a wait.
WAIT_CLASSES: frozenset[str] = frozenset(
    {"draft", "busy", "question", "boundary", "backoff", "not-ready"}
)


def blocked_on(reason: str | None) -> BlockedOn | None:
    """The ``blocked_on`` class for a raw reason; ``other`` when the table does not name it."""
    if not reason:
        return None
    for patterns, verdict in _TABLE:
        if any(fnmatchcase(reason, pattern) for pattern in patterns):
            return verdict
    return "other"
