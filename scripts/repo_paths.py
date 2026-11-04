"""Resolve per-checkout, gitignored repository paths through the MAIN working tree.

Three separate resolvers in this repository broke the same way before this module existed:
they built a path to a **gitignored, per-checkout** directory -- `.venv`, `node_modules`,
`.cache/` -- from the current checkout's root alone. A linked `git worktree` never carries
any of them, because `git worktree add` copies only tracked files, so every one of those
resolvers worked in the main tree and failed in a fresh worktree.

Anchoring on `Path.cwd()` is the same defect with an extra failure mode: it additionally
breaks when the tool is run from a subdirectory. Anchor on the repository root derived from
`__file__`, then hand it to `resolve_repo_path`.
"""

from __future__ import annotations

import subprocess
from functools import cache
from pathlib import Path


@cache
def main_root(root: Path) -> Path:
    """Return the MAIN working tree for ``root``, which may be a linked worktree.

    ``--git-common-dir`` always points at the main tree's ``.git``, and in the main tree
    itself its parent IS the repository root, so the one expression is correct either way --
    the same mechanism ``hooks/test-gate.sh`` uses to resolve pytest (ISSUE-REF). A
    non-repository root resolves to itself rather than raising, because unit tests and
    ad-hoc roots use this ladder too.
    """
    try:
        common_dir = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return root
    return Path(common_dir).parent if common_dir else root


@cache
def checkout_root(root: Path) -> Path:
    """Return the top level of the checkout CONTAINING ``root``.

    ``main_root`` answers "which tree owns the repository", so it collapses every
    subdirectory to the SAME main-tree root. That is the wrong anchor for re-hosting a
    path: to move ``<worktree>/skills/presentation`` onto the main tree you first need
    where it sits inside its OWN checkout, which is what ``--show-toplevel`` gives.

    A non-repository root resolves to itself rather than raising, matching ``main_root``,
    because unit tests and ad-hoc roots use this ladder too.
    """
    try:
        toplevel = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--path-format=absolute",
                "--show-toplevel",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return root
    return Path(toplevel) if toplevel else root


def resolve_repo_path(root: Path, *parts: str) -> Path:
    """Return ``root/*parts``, falling back to the same path under the main working tree.

    The **whole** path is tested, not just its first segment: a worktree that has run a
    node tool owns a `node_modules/` holding only a cache directory, so testing
    `node_modules` alone would accept a tree that carries none of the packages.

    ``root`` may be a SUBDIRECTORY of a checkout, and the fallback preserves its position
    inside that checkout. Collapsing to ``main_root`` alone is what ISSUE-REF's fix
    regressed into once ISSUE-REF moved the presentation callers from the repository root
    to the co-located ``SKILL_DIR``: the fallback for
    ``<worktree>/skills/presentation`` + ``node_modules/.bin/slidev`` became
    ``<main>/node_modules/.bin/slidev``, a path that has never existed in this repository,
    so the resolver silently returned the worktree's own absent path and the renderer
    reported the toolchain unavailable with the pinned Slidev sitting in the main tree.
    Every test covered ``root == checkout root``, where the two anchors coincide, which is
    exactly why the regression was invisible.

    When neither candidate exists the caller's own checkout is returned, so a diagnostic
    names the path that checkout expected rather than another tree's.
    """
    relative = Path(*parts)
    candidates = [root / relative]
    main = main_root(root)
    own = checkout_root(root)
    if main != own:
        try:
            within = Path(root).resolve().relative_to(own)
        except ValueError:
            # ``root`` is not under the toplevel git reports (symlinked or bind-mounted
            # checkout). Re-host at the main tree's root, the pre-subdirectory behaviour,
            # rather than raising in a resolver whose whole job is to degrade gracefully.
            within = Path()
        candidates.append(main / within / relative)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


# A skill's installed npm dependencies go under a HIDDEN directory inside the skill, because
# `install.sh` mounts this repository's whole `skills/` tree as codex's global agent-skills
# root (`~/.agents/skills`) and `codex_skills_extension::loader::host` enumerates that root on
# every invocation, before any model output. A plain `skills/presentation/node_modules` put
# 34,786 entries into that walk, it hit its traversal limit, and codex then produced nothing at
# all on this machine -- one skill's `npm ci` disabling the whole tool.
#
# Hidden is the ONLY mechanism that works, measured against codex-cli 0.159.2 (the table in
# `tests/test_codex_skills_walk.py`): the walker prunes hidden directories, FOLLOWS directory
# symlinks, and reads neither `.ignore` nor `.gitignore`. So a compatibility symlink left at
# the old non-hidden path would restore the defect in full while looking like a fix, and the
# already-present gitignore entry never helped. Raising the traversal limit is refused by the
# fleet rule against lifting a ceiling to clear a failure: the limit is the messenger, and the
# walk itself is a per-invocation cost.
#
# The MANIFEST does not move. `skills/presentation/package.json` stays tracked beside the skill
# so the skill still works in a standalone catalog checkout with no root manifest (ISSUE-REF,
# ratified 2026-09-08); only the install DESTINATION becomes hidden, and the installer stages
# the manifest into it. The Node sibling of these two accessors is
# `skills/presentation/scripts/repo-paths.cjs`, and a test asserts the two agree -- the two
# copies of this skill's path ladder have drifted before (ISSUE-REF).
_TOOLCHAIN_DIR_NAME = ".toolchain"


def toolchain_dir_name() -> str:
    """The hidden, per-skill directory holding installed dependencies."""
    return _TOOLCHAIN_DIR_NAME


def skill_modules_dir(skill_dir: Path, *parts: str) -> Path:
    """``skill_dir``'s installed ``node_modules``, under its hidden toolchain root.

    ``parts`` are joined BEFORE resolution rather than appended to the returned directory, so
    the whole path keeps ``resolve_repo_path``'s main-tree fallback. Appending afterwards tests
    only that the modules directory exists, and a checkout that has run any node tool owns one
    holding nothing but a cache -- the exact case that resolver's own docstring warns about.
    """
    return resolve_repo_path(skill_dir, _TOOLCHAIN_DIR_NAME, "node_modules", *parts)
