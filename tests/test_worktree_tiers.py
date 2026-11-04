"""Coverage for every feasible rule in AC-DT-1's reap-tier table."""

from __future__ import annotations

import pytest

from scripts.worktree_verdict import Action, Tier, assign_tier


@pytest.mark.parametrize(
    ("kwargs", "tier", "action"),
    [
        (
            {
                "owner_clear": False,
                "backed_up": False,
                "integrated": False,
                "uncaptured_dirt": "none",
            },
            Tier.HIGH,
            Action.KEEP,
        ),
        (
            {
                "owner_clear": True,
                "backed_up": False,
                "integrated": False,
                "uncaptured_dirt": "none",
            },
            None,
            Action.BACKUP_AND_REASSESS,
        ),
        (
            {
                "owner_clear": True,
                "backed_up": True,
                "integrated": False,
                "uncaptured_dirt": "capturable",
            },
            Tier.HIGH,
            Action.KEEP,
        ),
        (
            {
                "owner_clear": True,
                "backed_up": True,
                "integrated": True,
                "uncaptured_dirt": "unclassified_ignored",
            },
            Tier.HIGH,
            Action.KEEP,
        ),
        (
            {
                "owner_clear": True,
                "backed_up": True,
                "integrated": True,
                "uncaptured_dirt": "none",
            },
            Tier.MEDIUM,
            Action.REAP,
        ),
    ],
)
def test_decision_table_rules(
    kwargs: dict[str, bool | str], tier: Tier | None, action: Action
) -> None:
    assignment = assign_tier(**kwargs)

    assert assignment.tier is tier
    assert assignment.action is action


def test_no_signal_for_unique_work_is_kept_at_high_tier() -> None:
    assignment = assign_tier(
        owner_clear=True, backed_up=True, integrated=False, uncaptured_dirt="none"
    )

    assert assignment.tier is Tier.HIGH
    assert assignment.action is Action.KEEP


def test_integrated_dirty_work_is_still_human_only() -> None:
    assignment = assign_tier(
        owner_clear=True,
        backed_up=True,
        integrated=True,
        uncaptured_dirt="capturable",
    )

    assert assignment.tier is Tier.HIGH
    assert assignment.action is Action.KEEP


def test_unknown_dirt_state_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown uncaptured dirt state"):
        assign_tier(
            owner_clear=True, backed_up=True, integrated=True, uncaptured_dirt="unknown"
        )


def test_given_the_tier_vocabulary_when_read_then_there_is_no_tier_l() -> None:
    """ISSUE-REF: "should we arm L?" must be unaskable, not answered.

    Asserted as a PROPERTY of the vocabulary rather than by probing one call: an
    `assign_tier` input that happens to return Tier H today would pass equally well if
    `Tier.LOW` still existed and merely went unreached.
    """
    assert [tier.value for tier in Tier] == ["M", "H"]
    assert not hasattr(Tier, "LOW")
    assert "L" not in {tier.value for tier in Tier}
    assert not hasattr(Action, "REQUEST_VERDICT")
    assert "request-verdict" not in {action.value for action in Action}


def test_given_unintegrated_backed_up_work_when_assigned_then_no_signal_can_bypass() -> (
    None
):
    """The former Tier-L entry point takes no argument at all now."""
    with pytest.raises(TypeError):
        assign_tier(
            owner_clear=True,
            backed_up=True,
            integrated=False,
            uncaptured_dirt="none",
            mechanical_supersession_signal=True,
        )
