"""Shared worktree naming module -- grammar, normalisation, composition, derivation.

This module is the SINGLE source of truth for worktree naming rules, used by all
enforcement layers (L1-L5) to ensure Claude and Codex worktrees are identically shaped.

Design: docs/designs/issue-ref-worktree-naming-enforcement.md
"""

from __future__ import annotations

import re

# Grammar: TWO OR MORE lowercase alphanumeric segments, hyphen-joined.
# Each segment: 1-16 chars, [a-z0-9].
SLUG_PATTERN = re.compile(r"^[a-z0-9]{1,16}(-[a-z0-9]{1,16})+$")

# Task ID pattern: letter prefix + digits (e.g., "aih824", "kg101")
TASK_ID_PATTERN = re.compile(r"^[a-z]+[0-9]+$")

# Directory roots differ on purpose:
# - .worktrees for directories (where these already live)
# - wt/ for branches (because refs cannot start with .)
WORKTREES_DIR = ".worktrees"
BRANCH_ROOT = "wt"

# Appended to directory components that name an agent or session (not task-ids)
AGENT_SUBTREE_SUFFIX = "-agents"


def normalise(name: str) -> str:
    """Normalise a launch name to lowercase, hyphen-separated form.

    Applied BEFORE validation. Deterministic.

    Rules:
    - lowercase
    - underscore and whitespace become hyphen
    - runs of hyphens collapse to one
    - leading/trailing `-._` stripped

    Examples:
        "Dev_UAT" -> "dev-uat"
        "wt__name--sys" -> "wt-name-sys"
        "-leaf-" -> "leaf"
    """
    s = name.lower()
    # Replace underscores and whitespace with hyphens
    s = re.sub(r"[_\s]+", "-", s)
    # Collapse runs of hyphens
    s = re.sub(r"-+", "-", s)
    # Strip leading/trailing hyphens, dots, underscores
    s = s.strip("-._")
    return s


def validate(name: str) -> tuple[bool, str]:
    """Validate a normalised name against the slug grammar.

    Args:
        name: A normalised name (already passed through normalise())

    Returns:
        (is_valid, reason) tuple. reason is empty string if valid.

    The name must have:
    - TWO OR MORE segments
    - Each segment: 1-16 chars, lowercase alphanumeric [a-z0-9]
    - Segments separated by single hyphens

    Invalid examples:
        "wtnamesys" -> (False, "fewer than 2 segments")
        "dev-UAT" -> (False, "illegal characters (uppercase)")
        "a" -> (False, "fewer than 2 segments")
    """
    if not name:
        return False, "empty name"

    if not SLUG_PATTERN.match(name):
        # Diagnose the specific problem
        segments = name.split("-")
        if len(segments) < 2:
            return False, "fewer than 2 segments after normalisation"

        # Check for illegal characters
        if not all(re.match(r"^[a-z0-9]{1,16}$", seg) for seg in segments):
            # Find the first problematic segment
            for seg in segments:
                if not seg:
                    return False, "empty segment (consecutive hyphens)"
                if not re.match(r"^[a-z0-9]+$", seg):
                    return (
                        False,
                        "illegal characters (only lowercase a-z and 0-9 allowed)",
                    )
                if len(seg) > 16:
                    return False, f"segment too long (max 16 chars, got {len(seg)})"
            return False, "invalid format"

    return True, ""


def is_task_id(segment: str) -> bool:
    """Whether a segment is a task ID (e.g., 'aih824', 'kg101').

    Task IDs: letter prefix + digits, e.g., aih824.
    They pass through as plain directories (no -agents suffix).
    """
    return bool(TASK_ID_PATTERN.match(segment))


def compose(
    *,
    session: str,
    parent_chain: list[str] | None = None,
    leaf: str,
) -> tuple[str, str]:
    """Compose (directory path, branch name) from components.

    Args:
        session: Session title (e.g., "ISSUE-REF")
        parent_chain: Parent agent/task segments, if any (e.g., ["wt-name-sys"] or ["aih824", "wt-hook"])
        leaf: Leaf name (e.g., "dev-uat")

    Returns:
        (directory, branch) tuple

    The directory gets -agents suffix on every agent/session component.
    Task IDs pass through unchanged.

    Examples:
        session="ISSUE-REF", leaf="wt-name-sys"
        -> (".worktrees/ISSUE-REF-agents/wt-name-sys", "wt/ISSUE-REF/wt-name-sys")

        session="ISSUE-REF", parent_chain=["wt-name-sys"], leaf="dev-uat"
        -> (".worktrees/ISSUE-REF-agents/wt-name-sys-agents/dev-uat", "wt/ISSUE-REF/wt-name-sys/dev-uat")

        session="ISSUE-REF", parent_chain=["aih824", "wt-hook"], leaf="probe-run"
        -> (".worktrees/ISSUE-REF-agents/aih824/wt-hook-agents/probe-run", "wt/ISSUE-REF/aih824/wt-hook/probe-run")
    """
    chain = parent_chain or []

    # Build directory path: session + chain + leaf, with -agents suffix on non-task-id components
    dir_parts = []
    # Session always gets -agents
    dir_parts.append(f"{session}{AGENT_SUBTREE_SUFFIX}")
    # Parent chain: -agents on agent names, pass through task-ids
    for component in chain:
        if is_task_id(component):
            dir_parts.append(component)
        else:
            dir_parts.append(f"{component}{AGENT_SUBTREE_SUFFIX}")
    # Leaf is always a plain directory (never a container)
    dir_parts.append(leaf)

    directory = "/".join([WORKTREES_DIR, *dir_parts])

    # Build branch name: pure ownership tree, no suffixes
    branch_parts = [session, *chain, leaf]
    branch = "/".join([BRANCH_ROOT, *branch_parts])

    return directory, branch


def derive_directory_from_branch(branch: str) -> str:
    """Derive directory path from a branch name.

    Inverse of the branch->directory mapping in compose().

    Args:
        branch: Branch name (e.g., "wt/ISSUE-REF/wt-name-sys/dev-uat")

    Returns:
        Directory path (e.g., ".worktrees/ISSUE-REF-agents/wt-name-sys-agents/dev-uat")

    The branch must start with "wt/" (BRANCH_ROOT). Every component that is NOT
    a task-id gets -agents appended in the directory form.
    """
    if not branch.startswith(f"{BRANCH_ROOT}/"):
        raise ValueError(f"Branch must start with '{BRANCH_ROOT}/', got: {branch}")

    # Remove branch root prefix
    relative = branch[len(BRANCH_ROOT) + 1 :]  # +1 for the slash
    components = relative.split("/")

    # Apply -agents suffix to non-task-id components (all but the leaf)
    dir_parts = []
    for i, component in enumerate(components):
        is_leaf = i == len(components) - 1
        if is_leaf:
            # Leaf never gets -agents (it's not a container)
            dir_parts.append(component)
        elif is_task_id(component):
            # Task IDs pass through
            dir_parts.append(component)
        else:
            # Agent/session components get -agents
            dir_parts.append(f"{component}{AGENT_SUBTREE_SUFFIX}")

    return "/".join([WORKTREES_DIR, *dir_parts])
