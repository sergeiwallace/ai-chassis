#!/usr/bin/env python3
"""Pure, shadow-mode implementation of the worktree reap tier decision table.

THERE IS NO TIER L (ISSUE-REF). The tier vocabulary is M and H, and that is the whole
vocabulary -- not "L exists but is unarmed". A tier authorises an UNATTENDED deletion, and
the only unattended deletion this fleet performs is of content it has mechanically proved
integrated (Tier M). Supersession is a JUDGEMENT about two pieces of work, which the tier
ladder cannot measure; it used to be delegated to a ``cx review`` the reaper then acted on
automatically, and the owner's ruling (2026-10-06) is that an LLM judgement does not become
safer for being wrapped in automation. So the judging LLM is now the SESSION, and it records
its judgement where a human can read it: ``worktree_audit.py --delete --confirm-superseded
<path> --superseded-reason "<what superseded these commits>"``. That route is unchanged,
independently gated (it demands a pre-existing tip anchor), and documented as the authorised
path in ``REFUSAL_REMEDIES["KEEP_UNINTEGRATED_COMMITS"]`` and in the harness context files.

Consequence to keep in mind when reading ``assign_tier``: unintegrated content now reaches
Tier H / KEEP with no bypass at all. Nothing is waived by a signal the tool cannot verify.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Tier(StrEnum):
    MEDIUM = "M"
    HIGH = "H"


class Action(StrEnum):
    KEEP = "keep"
    BACKUP_AND_REASSESS = "backup-and-reassess"
    SNAPSHOT_AND_REASSESS = "snapshot-and-reassess"
    REAP = "reap"


class TierArm(StrEnum):
    """WHICH rung of :func:`assign_tier` produced an assignment, as a machine-readable token.

    ISSUE-REF. ``reason`` already said which arm fired, in prose, and prose is the one form a
    dispatch cannot read. The whole ladder reaches a caller as a single ``SHADOW_TIER``
    verdict, so ``REFUSAL_REMEDIES`` -- keyed on that verdict -- had exactly one remedy for
    all six arms, and it described the owner/liveness arms only. A run that had MEASURED the
    owner clear and refused on dirt still printed "message the session the reason names" and
    "a transcript inside the activity window is cleared by WAITING", then closed by naming
    ``--delete --confirm-merged <path>`` -- the invocation that had just refused.

    This is a LABEL, not an input: no decision in ``assign_tier`` reads it, and the ladder's
    outcomes are byte-for-byte what they were. The same split by token rather than by prose is
    what ISSUE-REF did one rung up, in ``_canonical_refusal``.
    """

    OWNER_LIVENESS = "owner-liveness"
    BACKUP = "backup-not-equal-head"
    DIRT = "uncaptured-dirt"
    IGNORED_UNCLASSIFIED = "ignored-unclassified"
    INTEGRATED = "integrated"
    NOT_INTEGRATED = "content-not-proved-integrated"


@dataclass(frozen=True)
class TierAssignment:
    """One mechanical result; callers decide whether a shadow-mode action may run."""

    tier: Tier | None
    action: Action
    reason: str
    arm: TierArm


def assign_tier(
    *,
    owner_clear: bool,
    backed_up: bool,
    integrated: bool,
    uncaptured_dirt: str,
) -> TierAssignment:
    """Apply AC-DT-1 without making a Git, network, or deletion call."""
    if uncaptured_dirt not in {"none", "capturable", "unclassified_ignored"}:
        raise ValueError(f"unknown uncaptured dirt state: {uncaptured_dirt}")
    if not owner_clear:
        assignment = TierAssignment(
            Tier.HIGH,
            Action.KEEP,
            "owner or liveness is not clear",
            TierArm.OWNER_LIVENESS,
        )
    elif not backed_up:
        assignment = TierAssignment(
            None,
            Action.BACKUP_AND_REASSESS,
            "backup does not equal HEAD",
            TierArm.BACKUP,
        )
    # IC-1: a snapshot preserves bytes for review; it never renames actual dirt to
    # ``none`` and never authorizes an automatic reap.  Only a later, recorded human
    # resolution can change the worktree itself into a clean candidate.
    elif uncaptured_dirt == "capturable":
        assignment = TierAssignment(
            Tier.HIGH,
            Action.KEEP,
            "tracked or untracked dirt requires human resolution",
            TierArm.DIRT,
        )
    elif uncaptured_dirt == "unclassified_ignored":
        assignment = TierAssignment(
            Tier.HIGH,
            Action.KEEP,
            "ignored paths are unclassified",
            TierArm.IGNORED_UNCLASSIFIED,
        )
    elif integrated:
        assignment = TierAssignment(
            Tier.MEDIUM, Action.REAP, "integrated and backed up", TierArm.INTEGRATED
        )
    else:
        # ISSUE-REF: the former Tier-L branch stood here, and a branch-config supersession
        # signal reached it. No signal substitutes for the integration proof now -- an
        # operator or session that has judged the work superseded says so through
        # ``--confirm-superseded``, where the claim is recorded and anchor-gated.
        assignment = TierAssignment(
            Tier.HIGH,
            Action.KEEP,
            "content is not proved integrated",
            TierArm.NOT_INTEGRATED,
        )
    return assignment
