"""The literal vocabularies the library shares with the auto-compact injector.

Every string here is copied from ``context_supervisor.py`` (bff30e522) so a
reader that greps today's attempt records, readiness trails and stage logs
reads the library's the same way. ``test_observe_parity`` asserts each set
against the legacy source text, less the library-only boundary-hold states.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# COMPACT_STATES (95-108).
STATES = frozenset(
    {
        "ARMED",
        "BOUND",
        "READY",
        "SENT",
        "STARTED",
        "COMPLETED",
        "AMBIGUOUS",
        "QUEUED",
        "STAGED_UNVERIFIED",
        "CORRUPTED",
        "SESSION_ENDED",
        "REFUSED",
    }
) | frozenset({"HELD", "RELEASED", "DROPPED", "PENDING_UNDO"})
# The save-state-to-compact boundary hold (ISSUE-REF D-5): library-only, never
# written by the supervisor. HELD is distinct from QUEUED, which stays Claude
# Code's own composer queue.
BOUNDARY_STATES = frozenset({"HELD", "RELEASED", "DROPPED"})
# A staged draft awaiting a visible composer and guarded recovery on a later tick.
RECOVERY_STATES = frozenset({"PENDING_UNDO"})
TERMINAL_STATES = frozenset(
    {"COMPLETED", "AMBIGUOUS", "CORRUPTED", "SESSION_ENDED", "REFUSED"}
) | frozenset({"RELEASED", "DROPPED"})

# _landing_record / _poll_landing, plus the library's prompt-record.
# `command-echo` is a deferred-record command's submission echo (ISSUE-REF).
LANDINGS = frozenset(
    {
        "command-record",
        "command-echo",
        "prompt-record",
        "queued",
        "text",
        "queue-dropped",
        "none",
    }
)
# The landings that prove Claude Code accepted the submission (SENT).
#: ``process-exit``: a session-restart's ``/exit`` ended the agent process (ISSUE-REF);
#: the process, not the transcript, is what proves a command that ends its own writer.
SUBMITTED_LANDINGS = frozenset(
    {"command-record", "command-echo", "prompt-record", "process-exit"}
)

# send_result (6557-6629, 5834-5837).
SEND_RESULTS = frozenset(
    {
        "not-attempted",
        "succeeded",
        "text-send-error",
        "fence-mismatch",
        "stage-verify-mismatch",
        "stage-verify-no-composer",
        "enter-send-error",
    }
)

# idle_proof (5970, 6001-6006, 6348).
IDLE_PROOFS = frozenset({"idle_prompt", "transcript-quiet", "composer-empty"})

# _analyse_composer_capture (5135-5313).
COMPOSER_CLASSIFICATIONS = frozenset(
    {
        "empty-composer",
        "composer-suggestion",
        "composer-nonempty",
        "not-obviously-occupied",
        "not-ready",
        "stage-verified",
        "stage-verify-mismatch",
    }
)
# _composer_attribute_class (5044-5074).
ATTRIBUTE_CLASSES = frozenset({"normal", "suggestion", "mixed", "unknown"})
# _CLAUDE_CODE_NOTICE_PATTERNS (352-365).
CLAUDE_CODE_NOTICES = frozenset(
    {"update-installed-restart-required", "auto-update-failed"}
)

# Refusal and ambiguity reasons the library can return.
TRANSPORT_REASONS = frozenset({"no-tmux", "tmux-version", "no-pane"})
REASONS = TRANSPORT_REASONS | frozenset(
    {
        "disabled",
        "unknown-command",
        "command-unavailable",
        "payload-too-long",
        "payload-exceeds-visible-rows",
        "multiline-mode-disabled",
        "multiline-unverified-version",
        "multiline-preconditions-missing",
        "goal-active",
        "goal-not-active",
        "no-session-record",
        "ambiguous-key",
        "pane-in-mode",
        "not-ready",
        "composer-suggestion",
        "composer-nonempty",
        "fence-mismatch",
        "text-send-error",
        "fence-mismatch-partial-draft",
        "text-send-error-partial-draft",
        "stage-verify-mismatch",
        "stage-verify-no-composer",
        "walk-tail-unreachable",
        "walk-top-unreachable",
        "walk-top-unproved",
        "walk-stitch-mismatch",
        "walk-unverified-version",
        "enter-send-error",
        "stage-undo-failed",
        "landing-none",
        "transcript-not-found",
        "injection-in-flight",
        "delivered-as-text",
        "compact-delivered-as-text",
        "queue-dropped",
        "dry-run",
        "landing-timeout-invalid",
        "boundary-held",
        "stale",
        "unknown-use-case",
        # session-restart (ISSUE-REF): the pane holds no agent process to restart
        "no-agent-process",
        # session-restart with --on-exit-dialog stay (ISSUE-REF): background work was
        # still running at /exit, so the dialog was cancelled and the session kept
        "exit-dialog-stayed",
        # ISSUE-REF: post-submit validation and the self-heal loop
        "post-submit-mismatch",
        "post-submit-missing",
        "self-heal-disarmed",
        "self-heal-escalated",
        # a self-heal decision whose own injection never reached, or never validated in,
        # the target
        "self-heal-injection-refused",
        "escalation-injection-failed",
        # ISSUE-REF: an Enter reached without a stage-verified draft presses nothing
        "pre-submit-unverified",
        # simulated-uat (ES-1516): only a disposable session launched for the run
        "target-is-self",
        "target-not-disposable",
        # Signed Slack reply authentication and binding.
        "unverified-reply",
        "reply-target-mismatch",
        "reply-payload-mismatch",
    }
)

#: The two validation verdicts every injector records (D-19, ISSUE-REF): the
#: pre-submit one on ``pre_submit_verdict``, the post-submit one on ``post_submit_verdict``.
PRE_SUBMIT_VERIFIED = "pre-submit-verified"
PRE_SUBMIT_FAILED = "pre-submit-failed"
PRE_SUBMIT_UNVERIFIED = "pre-submit-unverified"
PRE_SUBMIT_VERDICTS = frozenset({PRE_SUBMIT_VERIFIED, PRE_SUBMIT_FAILED})
POST_SUBMIT_VERDICTS = frozenset(
    {
        "post-submit-verified",
        "post-submit-pending",
        "post-submit-mismatch",
        "post-submit-missing",
    }
)

# Reasons a legacy call site synthesizes when it cannot run the library at all.
CALL_SITE_REASONS = frozenset({"adapter-without-tmux-command", "store-root-mismatch"})
# A call site's ``library-exception:<Name>``; transport only when nothing was staged.
LIBRARY_EXCEPTION_PREFIX = "library-exception:"

# The cutover's two refusal classes (D-8 Amendment, ISSUE-REF). Every reason in
# REASONS | CALL_SITE_REASONS is in exactly one.
#
# TRANSPORT: the library could not reach the pane, or could not act at all, before
# it typed anything or formed a view of the pane, so the legacy send may run.
TRANSPORT_REFUSALS = (
    TRANSPORT_REASONS
    | CALL_SITE_REASONS
    | frozenset(
        {
            # unresolved target
            "no-session-record",
            "ambiguous-key",
            # a send that failed before any text landed (a partial one has its own
            # ``-partial-draft`` reason)
            "text-send-error",
            "fence-mismatch",
            # the library's own configuration or capability, decided before the pane
            "disabled",
            "unknown-command",
            "command-unavailable",
            "payload-too-long",
            "landing-timeout-invalid",
        }
    )
)
# PANE JUDGEMENT: the library looked at the pane or session and declined it, typed
# into it, or already submitted; a legacy send now could type over what it protected.
PANE_JUDGEMENT_REFUSALS = frozenset(
    {
        "composer-nonempty",
        "composer-suggestion",
        "not-ready",
        "pane-in-mode",
        "injection-in-flight",
        "payload-exceeds-visible-rows",
        "multiline-mode-disabled",
        "multiline-unverified-version",
        "multiline-preconditions-missing",
        "goal-active",
        "goal-not-active",
        "fence-mismatch-partial-draft",
        "text-send-error-partial-draft",
        "stage-verify-mismatch",
        "stage-verify-no-composer",
        "walk-tail-unreachable",
        "walk-top-unreachable",
        "walk-top-unproved",
        "walk-stitch-mismatch",
        "walk-unverified-version",
        "stage-undo-failed",
        "enter-send-error",
        "landing-none",
        "transcript-not-found",
        "delivered-as-text",
        "compact-delivered-as-text",
        "queue-dropped",
        "dry-run",
        "boundary-held",
        "stale",
        "unknown-use-case",
        "no-agent-process",
        "exit-dialog-stayed",
        "post-submit-mismatch",
        "post-submit-missing",
        "self-heal-disarmed",
        "self-heal-escalated",
        "self-heal-injection-refused",
        "escalation-injection-failed",
        "pre-submit-unverified",
        "target-is-self",
        "target-not-disposable",
        # A rejected signed reply must never fall through to a legacy send.
        "unverified-reply",
        "reply-target-mismatch",
        "reply-payload-mismatch",
    }
)


@dataclass(frozen=True)
class InjectionOutcome:
    """What one ``Injector.run`` (or ``verify``) produced. ``submitted`` is the
    field the cutover rule reads: True means an Enter was sent."""

    attempt_id: str
    use_case: str
    state: str
    landing: str | None
    reason: str | None
    staged: bool
    submitted: bool
    evidence: dict[str, Path] = field(default_factory=dict)
    #: The self-heal loop's decision when the run failed validation (ISSUE-REF).
    self_heal: dict[str, object] | None = None
    #: The repair line a failed capability check prints, for every use case
    #: (ISSUE-REF); None when the check passed or did not run.
    repair: str | None = None

    def to_json(self) -> dict[str, object]:
        report: dict[str, object] = {
            "attempt_id": self.attempt_id,
            "use_case": self.use_case,
            "state": self.state,
            "landing": self.landing,
            "reason": self.reason,
            "staged": self.staged,
            "submitted": self.submitted,
            "evidence": {name: str(path) for name, path in self.evidence.items()},
        }
        if self.self_heal is not None:
            report["self_heal"] = self.self_heal
        if self.repair is not None:
            report["repair"] = self.repair
        return report


EXIT_CODES = {
    "SENT": 0,
    "STARTED": 0,
    "COMPLETED": 0,
    "QUEUED": 2,
    "REFUSED": 3,
    "AMBIGUOUS": 4,
    "BOUND": 4,
    "CORRUPTED": 4,
    "HELD": 6,
    "DROPPED": 0,
    "PENDING_UNDO": 4,
}
EXIT_USAGE = 5


def exit_code(outcome: InjectionOutcome) -> int:
    if outcome.reason == "dry-run":
        return 0
    return EXIT_CODES.get(outcome.state, 4)
