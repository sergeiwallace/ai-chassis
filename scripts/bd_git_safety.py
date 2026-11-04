#!/usr/bin/env python3
"""Shared git-subprocess boundary for the fleet's cross-repo Beads tooling (ISSUE-REF).

`GIT_DIR`/`GIT_INDEX_FILE`/`GIT_WORK_TREE` are **exported by git itself** while a
hook runs, and a child `git` process honours them over `cwd` — so any tool that
shells out to git with `cwd=<repo>` while reachable from a hook silently operates
on the *hook's* repo instead of the one it was asked to operate on. This class of
bug has now bitten the fleet three times: ISSUE-REF (ai-cli-utils pre-commit,
root-caused separately), and independently, before this module existed, in both
`bd_capture_sync.py`'s `_git` and `bd_roadmap_retire.py`'s `_git` — the latter is
the instructive case, because that script's AC-4 checker is *designed* to be wired
as a pre-commit/pre-push gate, so the leak made it silently inert in exactly its
intended deployment (a gate guarding 20 roadmaps that never fired). Every cross-repo
git shell-out in this fleet's scripts routes through `run_git` (or, for a subprocess
that spawns something other than `git` directly but might itself spawn `git` as a
grandchild, `strip_git_env`) rather than reimplementing the guard.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def strip_git_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of `env` (default `os.environ`) with every `GIT_*` key removed.

    Use this to build the `env=` for any subprocess call that could itself spawn
    `git` as a grandchild (e.g. a `bd` command, or another Python script run as a
    subprocess) — a leaked `GIT_*` var propagates to grandchildren exactly the same
    way it does to a direct `git` child.
    """
    source = env if env is not None else os.environ
    return {k: v for k, v in source.items() if not k.startswith("GIT_")}


ARCHIVED_ROADMAP_REL = "docs/roadmap/archive/master-roadmap.DO-NOT-EDIT.md"


def roadmap_source_doc(repo_name: str) -> str:
    """The D-9 ``~/projects``-relative ``source_doc`` value a roadmap-origin bd issue
    should carry: ``<repo_name>/docs/roadmap/archive/master-roadmap.DO-NOT-EDIT.md``.

    Single source of truth for this format — ``bd_migrate.py`` (issue creation and
    resync) and ``bd_roadmap_retire.py`` (the retirement re-stamp) both compute it
    from here, so the two can never drift into two disagreeing constants for the
    same value again (ISSUE-REF: ``bd_migrate.py`` previously carried its own stale,
    repo-blind ``docs/roadmap/master-roadmap.md`` constant that any resync would
    write back over the correct, repo-aware value ``bd_roadmap_retire.py`` stamps
    at retirement).
    """
    return f"{repo_name}/{ARCHIVED_ROADMAP_REL}"


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run git against `cwd`, immune to an ambient git context.

    `GIT_DIR`/`GIT_INDEX_FILE`/`GIT_WORK_TREE` are **exported by git itself** while
    a hook runs, and a child `git` honours them over `cwd` — so invoked from a hook
    this would silently inspect the hook's repo instead of the one asked for. The
    whole point of this tool is reading the right repo's state, so strip them.
    (`git -C <path>` is **not** an escape hatch: `-C` changes directory, but a set
    `GIT_DIR` still wins over it.)

    **This guard is not universally correct — classify the call site first:**

    - *Names a target repo* (`cwd=` or `-C`) and is hook-reachable or cross-repo →
      **strip** `GIT_*`. Use this function. A leak makes it operate on the wrong
      repo while reporting success (ISSUE-REF; `bd_roadmap_retire.py`'s AC-4
      checker, which was silently inert as the pre-commit gate it was built to be).
    - *Wants whatever repo it is ambiently inside* (typically no `cwd`, no `-C`) →
      **inherit** the environment. Do NOT use this function.
      `.githooks/validate-claude-settings.py` is the reference example: it runs
      `git rev-parse --show-toplevel` to find the repo whose hook is firing, so
      honouring the inherited `GIT_DIR` is the required behaviour and "guarding" it
      would break it.

    A sweep that guards every git call site indiscriminately introduces bugs of the
    second kind while fixing the first.
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=strip_git_env(),
    )
