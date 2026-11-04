#!/usr/bin/env python3
"""Reject a ``chore(beads)`` commit that stages files outside the task store.

This is a commit-msg hook because the invariant relates the staged index to the
commit subject; the subject does not exist at the pre-commit stage.  It reads
the index rather than the working tree so the verdict describes exactly what
Git will commit.

WHY THE SUBJECT MATCH IS THE WHOLE ``chore(beads)`` SCOPE AND NOT ``sync``
(ISSUE-REF, measured 2026-09-20).  This gate used to fire only on a subject
beginning ``chore(beads): sync``.  ``80d5af41 chore(beads): claim ISSUE-REF,
file ISSUE-REF (Dolt conflict resolved)`` was pushed directly to ``main`` from a
stale worktree and reverted three files belonging to two PRs that had merged
minutes earlier -- turning ``main`` red -- and this gate passed it, because the
subject said ``claim`` rather than ``sync``.  Reworded to ``sync`` with the
identical index, the gate named all three offenders.  So the mechanism was
sound and only its trigger was too narrow: the thing that makes a commit claim
pure bookkeeping is the ``chore(beads)`` scope, not one verb inside it.

Measured blast radius of the widening over ``main``'s whole history: 1911
``chore(beads)`` commits, of which 1892 stage nothing outside the task store.
Of the 19 that would now be refused, one is the G88 incident, three mix code or
docs into a Beads title outright, and fifteen ship a regenerated or
auto-mutated tracked file (``.share-surface.toml``, ``superwhisper/settings/``).
Those fifteen are shippable on sight per the fleet rules -- as their own commit.
Refusing to let them ride inside a Beads title is the atomic-commit rule, and
the remedy this gate already prints (retitle, or unstage) is the right one.

WHY THE ALLOWLIST IS A DIRECTORY AND NOT FOUR FILENAMES.  ``.beads/`` *is* the
task store, so every path in it is by construction task-store bookkeeping.
Enumerating filenames had already failed once by the time G88 was measured:
``.beads/display-id-owners.tsv`` is written **and ``git add``-ed** by this
repo's own ``advance the display-id owner ledger`` pre-commit hook, which runs
one stage earlier than this gate, so a three-filename allowlist plus the
widened subject above would have refused the repo's own bookkeeping commits --
30 of them in ``main``'s history.  A fifth store file would have reproduced the
same defect, which is the argument against enumerating at all.

WHAT THIS DOES ABOUT THE DIRECT-TO-``main`` EXEMPTION.  The fleet grants a
pure-bookkeeping commit a direct push to its target branch.  G88's commit was
never eligible for it and nothing stopped the push.  Nothing here inspects a
push destination, deliberately: refusing the commit at ``commit-msg`` means an
ineligible commit is never created, so it can never be pushed under an
exemption it does not qualify for.  Eligibility is enforced by making the
ineligible commit unmakeable, which is one guard earlier than the push.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

STORE_PREFIX = ".beads/"
# Kept for the message and for tests: the three the fleet rules name explicitly.
# They are a subset of STORE_PREFIX, never the definition of it -- see the module
# docstring on why enumerating filenames is the defect and not the contract.
DOCUMENTED_PATHS = frozenset(
    {
        ".beads/issues.jsonl",
        ".beads/interactions.jsonl",
        ".beads/config.yaml",
    }
)
BEADS_BOOKKEEPING_SUBJECT = re.compile(r"^chore\(beads\)(?:\([^)]*\))?!?:")


def is_task_store_path(path: str) -> bool:
    """Return whether ``path`` lives in the Beads task store.

    A plain ``startswith`` would also accept ``.beadsfoo/x``; the separator is
    part of the prefix, so the check is on the directory and not on the string.
    """
    return path.startswith(STORE_PREFIX) and path != STORE_PREFIX


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run Git without inheriting a parent hook's repository bindings."""
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=environment,
    )


def staged_paths(repo: Path) -> list[str]:
    """Return the index paths Git will commit, preserving unusual filenames."""
    result = _git(repo, "diff", "--cached", "--name-only", "-z")
    if result.returncode:
        detail = result.stderr.strip() or "no stderr"
        raise RuntimeError(f"cannot read the staged file set: {detail}")
    return [path for path in result.stdout.split("\0") if path]


def offending_paths(message: str, paths: list[str]) -> list[str]:
    """Return non-bookkeeping paths only for a Beads bookkeeping subject."""
    subject = message.splitlines()[0] if message.splitlines() else ""
    if not BEADS_BOOKKEEPING_SUBJECT.match(subject):
        return []
    return sorted(path for path in paths if not is_task_store_path(path))


def check_commit(repo: Path, message: str) -> int:
    """Validate the staged scope claimed by a Beads bookkeeping subject."""
    try:
        offenders = offending_paths(message, staged_paths(repo))
    except RuntimeError as exc:
        print(
            f"beads bookkeeping scope gate: ERROR (failing closed) — {exc}",
            file=sys.stderr,
        )
        return 1
    if not offenders:
        return 0
    print(
        "beads bookkeeping scope gate: REJECTED — a `chore(beads): ...` subject "
        f"claims pure task-store bookkeeping, so it may stage only {STORE_PREFIX}* "
        "paths (canonically " + ", ".join(sorted(DOCUMENTED_PATHS)) + ").",
        file=sys.stderr,
    )
    print("  Offending staged file(s):", file=sys.stderr)
    for path in offenders:
        print(f"    {path}", file=sys.stderr)
    print(
        "  Either retitle this commit because it is not pure Beads bookkeeping, "
        "or unstage the unrelated file(s) and ship them as their own commit.",
        file=sys.stderr,
    )
    print(
        "  If you did not mean to stage them at all, check for a STALE WORKTREE "
        "before unstaging: ISSUE-REF was a `git add -A` that committed pre-merge "
        "copies of three files and reverted them on main.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "message_file", help="the commit message file supplied by commit-msg"
    )
    args = parser.parse_args(argv)
    try:
        root = _git(Path.cwd(), "rev-parse", "--show-toplevel")
        if root.returncode:
            raise RuntimeError(root.stderr.strip() or "not inside a Git worktree")
        message = Path(args.message_file).read_text(encoding="utf-8", errors="replace")
    except (OSError, RuntimeError) as exc:
        print(
            f"beads bookkeeping scope gate: ERROR (failing closed) — {exc}",
            file=sys.stderr,
        )
        return 1
    return check_commit(Path(root.stdout.strip()), message)


if __name__ == "__main__":
    raise SystemExit(main())
