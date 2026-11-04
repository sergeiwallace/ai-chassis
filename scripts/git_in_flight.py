"""Detect a multi-step git operation in flight, for guards that precede destructive acts.

``git status --short`` / ``--porcelain`` answers a NARROWER question than the one a
destructive path actually asks. It reports **path status only**; the "you are currently
rebasing" banner lives solely in long-form ``git status``. Measured on git 2.55.0 in
scratch repos (ISSUE-REF):

======================================================  ==================  ===============
state                                                   ``status --short``  marker present
======================================================  ==================  ===============
rebase stopped by ``--exec false``                      *empty*             rebase-merge
rebase ``-i`` stopped at a ``break``                    *empty*             rebase-merge
bisect in progress                                      *empty*             BISECT_LOG
rebase stopped on conflict (unresolved)                  ``UU f.txt``        rebase-merge
rebase stopped on conflict, resolution staged            ``M  f.txt``        rebase-merge
rebase ``--apply`` conflict, resolution staged           ``M  f.txt``        rebase-apply
cherry-pick conflict, resolution staged                  ``M  f.txt``        CHERRY_PICK_HEAD
revert conflict, resolution staged                       ``M  f.txt``        REVERT_HEAD
merge conflict, resolution staged                        ``M  f.txt``        MERGE_HEAD
genuinely idle clean tree (positive control)             *empty*             *none*
======================================================  ==================  ===============

So the short form is a false all-clear in two distinct ways: it is literally empty for an
``--exec``/``break``/bisect stop, and for the conflict stops it reports only ``M  f.txt``,
which a caller reads as "merely dirty" and answers with the very ``reset --hard`` that
destroys the sequence. Neither reading can be repaired by parsing it harder, because the
operation is not in its output at all.

**``REBASE_HEAD`` is not a substitute, and its gap aligns with the hazard.** It is absent
for exactly the two rebase stop modes where ``status --short`` is *also* empty
(``--exec false``, ``break``) and present only for a conflict stop. A guard keyed on it is
therefore blind precisely in the invisible case -- guard and cheap probe failing in the
same direction, which is how a fail-open survives review. ``rebase-merge`` /
``rebase-apply`` are present for every rebase stop mode measured.

**The markers are PER-WORKTREE, so this resolves ``--git-dir`` and never
``--git-common-dir``.** Measured: a rebase started inside a linked worktree puts
``rebase-merge`` in ``<common>/worktrees/<name>/``, and the common dir has no such entry,
so a common-dir probe reports "clear" during a live rebase. Resolution goes through
``git rev-parse`` rather than parsing ``.git`` by hand because a linked worktree's ``.git``
is a FILE, and under ``worktree.useRelativePaths=true`` its ``gitdir:`` pointer is
RELATIVE (measured) -- hand-parsing resolves that against the caller's CWD, i.e. the wrong
directory, while reporting success.

Widening the marker set costs no false refusals: after a SUCCESSFUL rebase, single and
multi-commit cherry-pick, non-ff merge, revert, ``rebase --abort`` and ``bisect reset``,
zero markers linger (measured, all seven checked each time).

This probe answers "is an operation in flight in THIS tree". It is deliberately silent
about *other processes* working in the tree -- an operation can sit between its own steps
with no live git process at all, and conversely a process can be busy in a tree with no
marker set. A destructive path needs both this and a liveness check.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

#: State entries git writes while a multi-step operation is mid-flight, in the order
#: reported. Presence means the tree belongs to that operation even when no git process is
#: live and even when ``git status --short`` is empty. ``sequencer`` covers the
#: multi-commit cherry-pick/revert todo list.
IN_PROGRESS_MARKERS = (
    "rebase-merge",
    "rebase-apply",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "sequencer",
)


@dataclass(frozen=True)
class InFlightVerdict:
    """The outcome of the probe. ``clear`` is true ONLY on a positive observation.

    Three states, kept distinct on purpose: an operation was seen, nothing was seen, or
    the question could not be answered. Collapsing the third into the second is the
    fail-open this module exists to prevent, so ``clear`` is false whenever
    ``determined`` is false.
    """

    operation: str | None
    determined: bool
    reason: str

    @property
    def clear(self) -> bool:
        """True only when the probe positively established that no operation is in flight."""
        return self.determined and self.operation is None


def _git_dir_from_pointer(repo: Path) -> Path | None:
    """Read ``repo/.git`` directly: a directory is the git dir, a file points at it.

    The fallback for when ``git`` cannot answer -- an absent binary, or a ``.git`` that is
    a valid marker container but not a complete repository. A relative ``gitdir:`` pointer
    is resolved against ``repo``, NOT against the caller's working directory, which is the
    bug this replaces: the previous hand-parse returned a CWD-relative path and then
    reported "no operation" because that path did not exist.
    """
    git = repo / ".git"
    if git.is_dir():
        return git
    if git.is_file():
        try:
            pointer = git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        prefix = "gitdir:"
        if not pointer.startswith(prefix):
            return None
        target = Path(pointer[len(prefix) :].strip())
        if not target.is_absolute():
            target = (repo / target).resolve()
        return target
    return None


def resolve_git_dir(repo: Path) -> Path | None:
    """This tree's OWN git dir, or ``None`` when neither route can answer.

    ``--path-format=absolute --git-dir`` is the authority: it is correct for a main
    checkout, for a linked worktree whose ``.git`` is a file, and for the relative
    ``gitdir:`` pointer that ``worktree.useRelativePaths`` writes. ``.git`` is never
    assumed to be a directory, because in a linked worktree it is a file.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip())
    return _git_dir_from_pointer(repo)


def in_flight_operation(repo: Path) -> InFlightVerdict:
    """Name the multi-step git operation in flight in ``repo``, failing closed."""
    git_dir = resolve_git_dir(repo)
    if git_dir is None:
        return InFlightVerdict(
            None,
            False,
            f"neither git nor {repo}/.git could resolve a git dir, "
            "so an in-flight operation cannot be ruled out",
        )
    for marker in IN_PROGRESS_MARKERS:
        if (git_dir / marker).exists():
            return InFlightVerdict(
                marker, True, f"a {marker} operation is in flight in {repo}"
            )
    return InFlightVerdict(
        None, True, f"no multi-step git operation in flight in {repo}"
    )
