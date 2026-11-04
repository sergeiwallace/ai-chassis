#!/usr/bin/env python3
# ruff: noqa: E402
"""Anchor every local branch to a remote ref, so committed work survives losing this box.

Remote branch refs provide an independent copy if local storage is lost.

WHY A PER-REPO ALLOWLIST RATHER THAN DISCOVERED REMOTES. An unattended pusher that
discovers remotes and pushes to whatever it finds would push a shared team repo, where
pushing is hard-gated on explicit human approval. So authority is declared per repo in
``config/fleet-projects.toml`` as ``anchor_push_branches = true``, and a repo without it is
REFUSED BY NAME -- never skipped quietly, because a silent skip is indistinguishable from
"nothing to do" and that is the state this job exists to make visible.

That key is deliberately SEPARATE from ``reclaim_remote_branches``. The two grant different
authorities -- pushing a branch versus deleting someone's remote branch -- and reusing the
deletion key would mean a repo could not be granted push without also being granted
deletion. Authority must not escalate as a side effect of configuration reuse.

DEFAULTS ARE A SURVEY, NOT A PUSH. Without ``--apply`` nothing is pushed. A first run on
this fleet would otherwise fire hundreds of pushes across other live sessions' branches;
``--max-pushes`` bounds even the applied form. Pushing another session's branch is safe by
construction here (never forced, never deleting), but it is still a remote mutation, so it
is opt-in.

WHERE A PUSH LANDS (ISSUE-REF). When the remote has no branch of that name, or its branch
is an ancestor of the local tip, the push fast-forwards ``refs/heads/<branch>`` as it always
did. When the remote's same-named branch has DIVERGED (a non-forced push to it would be
refused on every pass), or when that ``refs/heads`` push is refused for any other reason
(the provenance backstop refuses publishing commits without terminal trailers), the tip --
that one commit, never a ``create_backup_ref`` snapshot with its ignored files (ISSUE-REF) --
goes once to ``refs/backup/<UTC date>/worktree-branch/<branch>-<tip12>`` instead, a
preservation ref the provenance gate exempts and no sibling can refuse. The remote's own
branch is never touched. A refusal is reported by its cause line (``refusal_line``, read from the
push's stdout as well as its stderr, where pre-commit puts a hook's verdict), not by
git's trailing ``failed to push some refs``. Because a ``refs/backup/`` push writes no
remote-tracking ref, a survey reads the remote's ``refs/backup/*`` with ONE ls-remote per
granted repo and counts a tip any of them already holds as anchored, so the job does not
re-push it every hour.

A RECLAIMED BRANCH IS NEVER RECREATED (ISSUE-REF). When ``git_review_sweep`` reclaims a
remote branch it anchors the tip only locally, at ``refs/backup/<date>/remote-branch/
<branch>``, then deletes ``refs/heads/<branch>``. An owner that keeps the local branch leaves
it looking unanchored here, and pushing ``refs/heads`` would undo the reclaim every hour. A
local branch whose tip carries such a record goes straight to the ``refs/backup/`` push
above, so the tip is still off this machine and the remote branch stays deleted. A branch
that has moved past the reclaimed tip is new work and lands on ``refs/heads`` as before.
"""

from __future__ import annotations

try:
    from py_interpreter_bootstrap import ensure_supported_python
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):
        """Stand in for the bootstrap when this file was copied without it.

        A partial copy cannot re-exec, because the module that knows how is the one
        missing. Staying silent when the running interpreter ALREADY meets the floor is
        correct rather than lax: there was nothing to enforce. Below the floor there is
        no remedy left to apply, so say so and let the real failure happen where it can
        be read, instead of dying here on an import that is not the problem.
        """
        import sys as _sys

        if _sys.version_info < minimum:
            _sys.stderr.write(
                "warning: {} is running under Python {} without its interpreter "
                "bootstrap, so the {}.{}+ floor could not be enforced\n".format(
                    __file__,
                    ".".join(str(p) for p in _sys.version_info[:3]),
                    minimum[0],
                    minimum[1],
                )
            )


ensure_supported_python()

import argparse
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent

#: Registry key granting this job authority to push a repo's branches.
AUTHORITY_KEY = "anchor_push_branches"

#: Per-repo override for the remote to anchor to; ``origin`` when unset.
REMOTE_KEY = "anchor_push_remote"

DEFAULT_REMOTE = "origin"

#: Preservation namespace on the remote. A push into it is not a publication, so the
#: provenance gate exempts it (#2888) and a diverged same-named branch cannot refuse it.
BACKUP_NAMESPACE = "refs/backup"

#: Cap on pushes per run. Bounds a first run against the whole existing backlog.
DEFAULT_MAX_PUSHES = 10

# Exit codes. Distinct on purpose: a job whose exit code is always non-zero trains the
# operator to ignore it, which is the same silent-no-protection failure as no job at all.
EXIT_OK = 0
EXIT_FAILURE = 1  # something broke: a push failed, git or the registry is unusable
EXIT_REFUSED = (
    2  # a usage refusal: the caller asked for a repo or branch it may not have
)
EXIT_ADVISORY = 3  # nothing broke, but work is unanchored and this job cannot anchor it

#: Where --check-schedule reads the ai-core scheduler's ledger path and freshness window.
DEFAULT_SCHEDULE_CONFIG = REPO_ROOT / "config" / "anchor_branches_schedule.toml"

#: Test-only override naming a different schedule config file.
SCHEDULE_CONFIG_ENV = "ANCHOR_SCHEDULE_CONFIG"


class AnchorRefusal(RuntimeError):
    """Raised when an argv would force-push, delete a ref, or otherwise exceed authority."""


def _load(module_name: str, filename: str):
    """Load a sibling script by absolute path, matching this repo's standalone convention.

    By path, not by import: these run as scripts, so a plain import would depend on the
    caller's ``sys.path``.
    """
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


core = _load("_anchor_git_review_core", "git_review_core.py")


# --------------------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------------------

#: argv tokens that would make a push destructive. Matched as whole tokens anywhere in
#: argv, the same bluntness ``worktree_audit._git_local_only`` uses and for the same
#: reason: parsing git's option grammar properly means knowing which globals take a value,
#: and under-refusal here rewrites or deletes a remote branch.
_FORBIDDEN_PUSH_TOKENS = frozenset(
    {
        "--force",
        "-f",
        "--force-with-lease",
        "--force-if-includes",
        "--mirror",
        "--delete",
        "-d",
        "--prune",
        "--all",
        "--tags",
        "--follow-tags",
    }
)


def _assert_push_is_safe(args: list[str]) -> None:
    """Refuse any push argv that could rewrite or remove a remote ref.

    Three separate ways a push destroys instead of anchoring, so three checks:
    a force flag, a leading ``+`` on the refspec (force by other spelling), and an empty
    refspec source (``:refs/heads/x`` is a delete). ``--prune``/``--mirror``/``--all``
    are refused as well: each can remove refs the caller never named.
    """
    offenders = sorted(token for token in args if token in _FORBIDDEN_PUSH_TOKENS)
    if offenders:
        raise AnchorRefusal(
            f"refusing `git {' '.join(args)}` (names {', '.join(offenders)}): this job "
            "anchors work by fast-forwarding a remote ref and must be incapable of "
            "rewriting or deleting one"
        )
    for token in args:
        if ":" not in token or token.startswith("-"):
            continue
        source, _, dest = token.partition(":")
        if token.startswith("+"):
            raise AnchorRefusal(
                f"refusing refspec `{token}`: a leading `+` is a force push by another "
                "spelling"
            )
        if not source:
            raise AnchorRefusal(
                f"refusing refspec `{token}`: an empty source deletes `{dest}` on the "
                "remote"
            )


def _git(
    args: list[str],
    cwd: Path,
    *,
    timeout: int | None = None,
    stdin: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run git with a scrubbed environment; never raises on a non-zero exit.

    ``GIT_*`` is stripped for the reason ``git_review_core._git`` documents: git exports
    ``GIT_DIR`` into hook subprocesses and ``cwd=`` does not override it, so an inherited
    value would silently operate on the wrong repo.

    A command that times out is killed with every process under it (ISSUE-REF).
    ``subprocess.run(timeout=)`` kills only git, and a push's pre-push hook is git's child
    with the test gate under it, so killing git alone left that gate running orphaned.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(extra_env or {})
    try:
        process = subprocess.Popen(
            ["git", *args],
            cwd=str(cwd),
            stdin=subprocess.PIPE if stdin is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except OSError as exc:
        return 127, "", str(exc)
    try:
        stdout, stderr = process.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            process.communicate(timeout=_STOPPED_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        return 124, "", f"`git {' '.join(args)}` timed out after {timeout}s"
    except BaseException:
        _kill_process_tree(process)
        raise
    return process.returncode, stdout.strip(), stderr.strip()


#: How long a killed command's pipes are drained once its process tree is gone.
_STOPPED_DRAIN_SECONDS = 5.0

#: Where a timed-out command's SIGTERM grace is read, at call time, on every kill.
DEFAULT_PUSH_CONFIG = REPO_ROOT / "config" / "anchor_branches_push.toml"

#: Test-only override naming a different push config file.
PUSH_CONFIG_ENV = "ANCHOR_PUSH_CONFIG"

#: The grace used when the push config is missing or unusable.
DEFAULT_TERM_GRACE_SECONDS = 5.0

#: How often the grace re-lists the tree, so a child forked during it is still tracked.
_GRACE_POLL_SECONDS = 0.05


def _term_grace_seconds() -> float:
    """The SIGTERM grace from the push config; the default when it cannot be read."""
    path = Path(os.environ.get(PUSH_CONFIG_ENV) or DEFAULT_PUSH_CONFIG)
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        grace = float(config["push_timeout"]["term_grace_seconds"])
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError):
        return DEFAULT_TERM_GRACE_SECONDS
    return max(grace, 0.0)


def _kill_process_tree(process: subprocess.Popen) -> None:
    """Stop ``process`` and every descendant: SIGTERM, a grace, then freeze and SIGKILL.

    SIGTERM first because the pre-push test gate traps it and its EXIT trap removes the
    temporary tree it built under ``.git/test-gate-trees/``; a SIGKILL skips the trap and
    leaks the tree. The tree is frozen before the TERM, so the signal reaches every process
    that exists, and each is resumed to handle it. During the grace the tree is re-listed,
    so a child forked by a handler stays tracked after its parent exits. Whatever is still
    running when the grace ends is frozen, re-listed until no new descendant appears, and
    SIGKILLed, so nothing spawns past the kill. It returns once each killed process has
    exited, bounded by ``_SETTLE_SECONDS``, so the tree is dead when the caller looks.

    Descendants are found by parentage, not process group: the job shares its caller's
    group, so the scheduler's own kill of that group still reaches a push in flight.
    """
    try:
        import psutil
    except ModuleNotFoundError:
        process.kill()
        return
    try:
        root = psutil.Process(process.pid)
        root.suspend()
    except psutil.Error:
        process.kill()
        return
    tree = _freeze_descendants(psutil, [root])
    for victim in tree:
        try:
            victim.terminate()
        except psutil.Error:
            continue
    for victim in tree:
        try:
            victim.resume()
        except psutil.Error:
            continue

    known = {victim.pid for victim in tree}
    deadline = time.monotonic() + _term_grace_seconds()
    while True:
        alive = [victim for victim in tree if _running(psutil, victim)]
        if not alive or time.monotonic() >= deadline:
            break
        for parent in alive:
            try:
                children = parent.children(recursive=True)
            except psutil.Error:
                continue
            for child in children:
                if child.pid not in known:
                    known.add(child.pid)
                    tree.append(child)
        time.sleep(_GRACE_POLL_SECONDS)

    stragglers = [victim for victim in tree if _running(psutil, victim)]
    for victim in stragglers:
        try:
            victim.suspend()
        except psutil.Error:
            continue
    killed = _freeze_descendants(psutil, stragglers)
    for victim in killed:
        try:
            victim.kill()
        except psutil.Error:
            continue
    # SIGKILL is sent, not done: a loaded host can take a while to schedule each victim's
    # exit, and a caller that checks the tree on return must find it dead (ISSUE-REF).
    _await_settled(psutil, killed, _exited(psutil))


def _running(psutil, victim) -> bool:
    """True for a live process; a zombie has already exited and its pid is only unreaped."""
    try:
        return victim.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _freeze_descendants(psutil, roots: list) -> list:
    """``roots`` (already frozen) plus every descendant, each frozen as it is found.

    Re-lists until a pass finds no new process, so a child forked after one listing is
    frozen by the next rather than running on orphaned. Each pass lists only once every
    frozen process has actually stopped: ``suspend()`` only sends SIGSTOP, and a process
    inside ``fork()`` finishes the fork before it stops, so a listing taken earlier misses
    that child and it outlives the SIGKILL (ISSUE-REF).
    """
    frozen = {victim.pid: victim for victim in roots}
    while True:
        _await_settled(psutil, list(frozen.values()), _stopped(psutil))
        fresh = []
        for parent in list(frozen.values()):
            try:
                fresh.extend(
                    child
                    for child in parent.children(recursive=True)
                    if child.pid not in frozen
                )
            except psutil.Error:
                continue
        if not fresh:
            return list(frozen.values())
        for child in fresh:
            if child.pid in frozen:
                continue
            frozen[child.pid] = child
            try:
                child.suspend()
            except psutil.Error:
                continue


#: How long a signalled process is given to reach the state its signal leads to: stopped
#: after SIGSTOP, exited after SIGKILL. Both are immediate on an idle host; this bounds a
#: stall on a loaded one and is not part of the SIGTERM grace.
_SETTLE_SECONDS = 2.0


def _exited(psutil) -> set[str]:
    return {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}


def _stopped(psutil) -> set[str]:
    return {psutil.STATUS_STOPPED, psutil.STATUS_TRACING_STOP} | _exited(psutil)


def _await_settled(psutil, victims: list, settled: set[str]) -> None:
    """Wait, up to ``_SETTLE_SECONDS``, until each victim's status is in ``settled`` or gone."""
    deadline = time.monotonic() + _SETTLE_SECONDS
    pending = list(victims)
    while pending:
        still = []
        for victim in pending:
            try:
                if victim.status() not in settled:
                    still.append(victim)
            except psutil.Error:
                continue
        if not still or time.monotonic() >= deadline:
            return
        pending = still
        time.sleep(_GRACE_POLL_SECONDS / 10)


# Marks every anchor push as a backup push, as worktree_backup marks its own: the pre-push
# chain then yields it to a session's in-flight push of the same ref, and hooks/test-gate.sh
# never waits on or holds the host test lock for it (ISSUE-REF, after ISSUE-REF). A yielded
# branch push falls through to the refs/backup/ anchor below, which runs no test body.
_BACKUP_PUSH_ENV = {"AIH_BACKUP_PUSH": "1"}


def _git_push(
    remote: str, source: str, dest: str, cwd: Path, *, timeout: int
) -> tuple[int, str, str]:
    """Push ``source`` to the fully spelled ``dest`` ref on ``remote``, never forced.

    The refspec is fully spelled on both sides so the destination cannot be inferred from
    ``push.default`` or a branch's configured upstream -- an anchor must land on the ref
    this job chose, not wherever the repo's push configuration happens to point.
    """
    args = ["push", remote, f"{source}:{dest}"]
    _assert_push_is_safe(args)
    return _git(args, cwd, timeout=timeout, extra_env=_BACKUP_PUSH_ENV)


def backup_ref_for(branch: str, tip: str) -> str:
    """The preservation ref a diverged branch's tip is anchored to.

    ``refs/backup/<UTC date>/worktree-branch/`` is the namespace ``worktree_audit`` already
    writes, so every reader of it finds these too; the ``-<tip12>`` suffix keeps two
    different tips of one branch on one day from colliding, since a second push to the same
    name would be a non-fast-forward and refused.
    """
    day = datetime.now(UTC).date().isoformat()
    return f"{BACKUP_NAMESPACE}/{day}/worktree-branch/{branch}-{tip[:12]}"


def remote_refs(
    remote: str, cwd: Path, *, timeout: int
) -> tuple[dict[str, str], dict[str, str]] | str:
    """``remote``'s branch heads and ``refs/backup/*`` refs, or the error text.

    ONE ls-remote per repo, scoped by pattern: an unscoped call on GitHub also lists every
    ``refs/pull/*`` ref, thousands on a repo of this age.
    """
    rc, out, err = _git(
        ["ls-remote", remote, "refs/heads/*", f"{BACKUP_NAMESPACE}/*"],
        cwd,
        timeout=timeout,
    )
    if rc != 0:
        return err or f"git ls-remote exit {rc}"
    heads: dict[str, str] = {}
    backups: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            heads[ref.removeprefix("refs/heads/")] = sha
        elif ref.startswith(f"{BACKUP_NAMESPACE}/"):
            backups[ref] = sha
    return heads, backups


def _present_locally(shas: set[str], cwd: Path) -> set[str]:
    """The subset of ``shas`` whose commit objects exist in this repo (one cat-file call)."""
    if not shas:
        return set()
    ordered = sorted(shas)
    rc, out, _ = _git(
        ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
        cwd,
        stdin="\n".join(ordered) + "\n",
    )
    if rc != 0:
        return set()
    return {
        name
        for name, _, kind in (line.partition(" ") for line in out.splitlines())
        if kind == "commit"
    }


def _is_ancestor(ancestor: str, descendant: str, cwd: Path) -> bool:
    rc, _, _ = _git(["merge-base", "--is-ancestor", ancestor, descendant], cwd)
    return rc == 0


# --------------------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------------------

ANCHORED = "anchored"
NEEDS_ANCHOR = "needs-anchor"
GATED = "gated"
PUSHED = "pushed"
PUSH_FAILED = "push-failed"


@dataclass
class BranchState:
    """One local branch and whether a remote ref already holds its tip."""

    name: str
    tip: str
    status: str
    reason: str


@dataclass
class RepoResult:
    """One repo's outcome: its authority, its branches, and what was done."""

    name: str
    path: Path
    granted: bool
    remote: str | None
    branches: list[BranchState] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def unanchored(self) -> list[BranchState]:
        return [b for b in self.branches if b.status == NEEDS_ANCHOR]

    @property
    def pushed(self) -> list[BranchState]:
        return [b for b in self.branches if b.status == PUSHED]

    @property
    def failed(self) -> list[BranchState]:
        return [b for b in self.branches if b.status == PUSH_FAILED]


def resolve_push_remote(repo: Path, config: dict) -> str | None:
    """The remote to anchor to: registry override, then ``remote.pushDefault``, then origin.

    ``None`` means the repo has no remote at all, so nothing about it can be anchored --
    reported as such rather than treated as "already safe".
    """
    configured = config.get(REMOTE_KEY)
    rc, out, _ = _git(["remote"], repo)
    if rc != 0:
        return None
    remotes = {line.strip() for line in out.splitlines() if line.strip()}
    if isinstance(configured, str) and configured:
        return configured if configured in remotes else None
    rc, out, _ = _git(["config", "--get", "remote.pushDefault"], repo)
    if rc == 0 and out and out in remotes:
        return out
    return DEFAULT_REMOTE if DEFAULT_REMOTE in remotes else None


def _describe_push_config(push_ref: str, push_track: str) -> str:
    """The push-config diagnostic for a branch, as a phrase for a reason string."""
    if not push_ref:
        return "no push destination is configured"
    if "gone" in push_track:
        return f"push ref {push_ref} does not exist"
    if push_track:
        return f"{push_ref} {push_track}"
    return f"push ref {push_ref}"


def classify_branches(repo: Path, config: dict) -> tuple[list[BranchState], list[str]]:
    """Every local branch, labelled ANCHORED, NEEDS_ANCHOR or GATED.

    THE GATE IS THE OBJECT GRAPH, NOT THE PUSH CONFIG:

        git rev-list --count <branch> --not --remotes

    Note the argument order. ``--not`` negates every rev after it, so the branch must come
    FIRST -- ``rev-list --count --not --remotes <branch>`` returns 0 for every branch alive
    and reads as "everything is anchored", which is the exact false clean this job must
    never report. That form silently produced two empty surveys before it was caught.

    ``%(push:track)``'s ``[gone]`` state is what D-4(d) named as the detector, and this job is
    indeed the first thing in the fleet to read it -- but it is the DIAGNOSTIC here, not the
    gate, because measurement killed it as a gate twice over.

    1. IT IS STRUCTURALLY INERT UNDER THIS FLEET'S OWN CONVENTION. A worktree branch must
       track its MERGE TARGET (docs/procedures/worktree-workflow.md), and with ``push.default``
       at its default ``simple`` a branch whose upstream has a different name resolves NO push
       ref. Measured 2026-08-29: ``%(push)`` is empty for 90 of ai-harness's 97 local branches,
       so ``[gone]`` fires for 2 -- missing 42 of the 44 that genuinely hold unanchored commits.
    2. COUNTING EMPTY ``%(push)`` AS PUSH-NEEDED OVERSHOOTS THE OTHER WAY, to 92 branches on
       ai-harness and 51 on managed-platform, because it flags every branch already merged into
       ``origin/main``. That would be a ~135-branch first push to anchor commits already off
       the machine -- the bulk push this job was explicitly built not to perform.

    Both readings ask "does a remote ref with this branch's name exist". ``rev-list --not
    --remotes`` asks the question durability actually turns on: is any commit reachable from no
    remote ref at all. It gives 44 and 8.

    Consequence worth stating: a branch called ANCHORED here may still have no remote ref of
    its own, so losing this machine would lose the branch NAME while keeping every commit.
    That is the accepted boundary -- D-4(d) exists for the measured loss of commits.

    AC-5 is honored by failing toward protection: an unreadable count is NEEDS_ANCHOR.
    """
    errors: list[str] = []
    gated = set(core.gated_branches(config)) | set(core.ALWAYS_PROTECTED)
    rc, out, err = _git(
        [
            "for-each-ref",
            "--format=%(refname:short)%09%(objectname)%09%(push)%09%(push:track)",
            "refs/heads",
        ],
        repo,
    )
    if rc != 0:
        return [], [f"could not enumerate local branches: {err or f'git exit {rc}'}"]

    states: list[BranchState] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # Pad rather than reject a short row. `%(push)` and `%(push:track)` are both empty
        # for a branch with no push configuration, so its row ends in trailing tabs -- and
        # the trailing tabs of the LAST row are eaten by the stdout strip in `_git`. A
        # length check dropped exactly one real branch per repo, silently, as "unparseable".
        parts = (line.split("\t") + ["", "", ""])[:4]
        name, tip, push_ref, push_track = parts
        if not name or not tip:
            errors.append(f"unparseable for-each-ref row: {line!r}")
            continue
        diagnostic = _describe_push_config(push_ref, push_track)
        if name in gated:
            states.append(
                BranchState(
                    name,
                    tip,
                    GATED,
                    "behind a hard human gate or always-protected; never pushed by this job",
                )
            )
            continue
        rc, count, err = _git(["rev-list", "--count", name, "--not", "--remotes"], repo)
        if rc != 0:
            errors.append(f"{name}: could not count unanchored commits: {err}")
            states.append(
                BranchState(
                    name,
                    tip,
                    NEEDS_ANCHOR,
                    f"unanchored commit count unreadable; {diagnostic}",
                )
            )
            continue
        if count.strip() not in {"", "0"}:
            states.append(
                BranchState(
                    name,
                    tip,
                    NEEDS_ANCHOR,
                    f"{count.strip()} commit(s) reachable from no remote ref; {diagnostic}",
                )
            )
            continue
        states.append(
            BranchState(
                name, tip, ANCHORED, f"every commit is on a remote ref; {diagnostic}"
            )
        )
    return states, errors


# --------------------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------------------


def anchor_repo(
    repo: Path,
    name: str,
    config: dict,
    *,
    apply: bool,
    budget: list[int],
    timeout: int,
) -> RepoResult:
    """Classify one repo's branches and, with ``apply``, anchor the unanchored ones.

    ``budget`` is a one-element list so the remaining push allowance is shared across
    repos: the cap bounds the RUN, not each repo, or a fleet-wide first run would still
    fire a multiple of the cap.
    """
    granted = config.get(AUTHORITY_KEY) is True
    remote = resolve_push_remote(repo, config) if granted else None
    result = RepoResult(name=name, path=repo, granted=granted, remote=remote)
    result.branches, result.errors = classify_branches(repo, config)

    if not granted:
        return result
    if remote is None:
        if apply:
            result.errors.append(
                f"{name} is granted {AUTHORITY_KEY} but has no usable push remote, so its "
                "branches cannot be anchored anywhere"
            )
        return result
    if not result.unanchored:
        return result

    listed = remote_refs(remote, repo, timeout=timeout)
    if isinstance(listed, str):
        result.errors.append(
            f"could not list {remote}'s refs, so no branch was checked against its "
            f"refs/backup/ anchors or pushed: {listed.splitlines()[-1][:400]}"
        )
        return result
    heads, backups = listed
    local = _present_locally(set(heads.values()) | set(backups.values()), repo)
    _credit_remote_backups(repo, result.branches, backups, local)

    if not apply:
        return result

    reclaimed = reclaim_records(repo)
    for branch in result.branches:
        if branch.status != NEEDS_ANCHOR:
            continue
        if budget[0] <= 0:
            break
        budget[0] -= 1
        remote_tip = heads.get(branch.name)
        backup = backup_ref_for(branch.name, branch.tip)
        record = reclaimed.get((branch.name, branch.tip))
        if record is not None:
            why = (
                f"its {remote}/{branch.name} was reclaimed at this tip ({record}), so "
                "refs/heads is not recreated"
            )
        elif remote_tip is None or (
            remote_tip in local and _is_ancestor(remote_tip, branch.tip, repo)
        ):
            head = f"refs/heads/{branch.name}"
            rc, out, err = _git_push(remote, head, head, repo, timeout=timeout)
            if rc == 0:
                branch.status = PUSHED
                branch.reason = f"anchored to {remote}/{branch.name}"
                continue
            why = f"its {head} push was refused: {refusal_line(out, err, rc)}"
        else:
            why = f"its {branch.name} has diverged at {remote_tip[:12]}"
        rc, out, err = _git_push(remote, branch.tip, backup, repo, timeout=timeout)
        if rc == 0:
            branch.status = PUSHED
            branch.reason = f"anchored to {remote} {backup} ({why})"
        else:
            branch.status = PUSH_FAILED
            branch.reason = (
                f"{backup} push was refused: {refusal_line(out, err, rc)} ({why})"
            )[:400]
    return result


#: A pre-commit status line (``<hook name>.....Passed``): framing, whatever its name says.
_PRE_COMMIT_STATUS = re.compile(
    r"\.{3,}\s*(\(no files to check\))?(Passed|Failed|Skipped)$"
)


def refusal_line(stdout: str, stderr: str, rc: int) -> str:
    """The line of a failed push's output that says WHY it was refused.

    Not the last line: git always ends a refused push with ``error: failed to push some refs
    to <url>`` or a ``hint:``, which names no cause, and recording only that line once hid a
    provenance-hook refusal across seven branches. STDOUT is read first, because pre-commit
    prints each hook's verdict there and leaves stderr holding only git's framing, which
    recorded no cause at all (ISSUE-REF). The first line that says ``reject`` or ``error``
    is the hook's or the remote's own verdict; failing that, the first line that is not
    framing -- git's, or a pre-commit status/``- hook id:`` line.
    """
    lines = [
        line.strip() for line in f"{stdout}\n{stderr}".splitlines() if line.strip()
    ]
    cause = [
        line
        for line in lines
        if not line.startswith(
            ("hint:", "To ", "- hook id:", "- exit code:", "- duration:")
        )
        and "failed to push some refs" not in line
        and line != "remote:"
        and not _PRE_COMMIT_STATUS.search(line)
    ]
    for line in cause:
        lowered = line.lower()
        if "reject" in lowered or "error" in lowered:
            return line[:300]
    if cause:
        return cause[0][:300]
    return lines[-1][:300] if lines else f"git exit {rc}"


#: The ``kind`` segment ``git_review_sweep.preserve_ref`` writes when a reclaim deletes a
#: remote branch: ``refs/backup/<stamp>/remote-branch/<branch>[-<tip12>]``.
RECLAIM_KIND = "remote-branch"


def reclaim_records(repo: Path) -> dict[tuple[str, str], str]:
    """``(branch, tip)`` to the local anchor a reclaim left when it deleted that remote branch.

    The reclaim's anchor is LOCAL ONLY by design (``preserve_ref``'s AC-7), so after it
    deletes ``refs/heads/<branch>`` nothing on the remote holds the tip, and an owner still
    keeping the local branch makes it look unanchored here. Recreating ``refs/heads`` would
    undo the reclaim on the next pass (ISSUE-REF), so a tip carrying one of these records is
    anchored under ``refs/backup/`` instead. Keyed on the exact tip: a branch that moved
    past the reclaimed commit is new work, and lands as any other branch does.
    """
    rc, out, _ = _git(
        ["for-each-ref", "--format=%(refname)%09%(objectname)", BACKUP_NAMESPACE], repo
    )
    if rc != 0:
        return {}
    records: dict[tuple[str, str], str] = {}
    for line in out.splitlines():
        ref, _, sha = line.partition("\t")
        _stamp, _, rest = ref.removeprefix(f"{BACKUP_NAMESPACE}/").partition("/")
        kind, _, branch = rest.partition("/")
        if kind != RECLAIM_KIND or not branch or not sha:
            continue
        # ``preserve_ref`` suffixes the short sha only on a same-day name collision.
        for name in {branch, branch.removesuffix(f"-{sha[:12]}")}:
            records.setdefault((name, sha), ref)
    return records


def _credit_remote_backups(
    repo: Path,
    branches: list[BranchState],
    backups: dict[str, str],
    local: set[str],
) -> None:
    """Mark ANCHORED every NEEDS_ANCHOR branch a remote ``refs/backup/*`` ref already holds.

    ``rev-list --not --remotes`` cannot see these refs -- a push to ``refs/backup/`` writes no
    remote-tracking ref -- so without this the job would re-push every diverged branch's tip
    on every pass. Held means the tip IS a backup ref's object, or every commit the tip
    reaches is on a remote-tracking ref or a locally present backup object. A backup object
    absent from this repo cannot be checked for ancestry, so it credits only an exact match.
    """
    by_sha: dict[str, str] = {}
    for ref, sha in sorted(backups.items()):
        by_sha.setdefault(sha, ref)
    present = sorted(sha for sha in by_sha if sha in local)
    for branch in branches:
        if branch.status != NEEDS_ANCHOR:
            continue
        if branch.tip in by_sha:
            branch.status = ANCHORED
            branch.reason = f"tip is held by remote {by_sha[branch.tip]}"
            continue
        if not present:
            continue
        rc, count, _ = _git(
            ["rev-list", "--count", branch.tip, "--not", "--remotes", *present], repo
        )
        if rc == 0 and count.strip() == "0":
            branch.status = ANCHORED
            branch.reason = (
                "every commit is on a remote ref or a remote refs/backup/ ref"
            )


# --------------------------------------------------------------------------------------
# schedule detectability (AC-7)
# --------------------------------------------------------------------------------------


def schedule_backends() -> list[dict]:
    """Which periodic-scheduler backends could actually run this job, measured not assumed.

    A scheduler's CLIENT existing proves nothing about whether its DAEMON runs. Measured on
    ``managed-linux`` 2026-08-29: ``crontab`` is installed and answers cleanly, no cron
    daemon is running, and PID 1 is supervisord -- so a crontab entry would be accepted and
    never fire. That is precisely the silent-no-protection state this function exists to
    surface, and reporting "cron is available" from the binary alone would manufacture it.
    """
    backends: list[dict] = []

    crontab = shutil.which("crontab")
    daemon = False
    rc, out, _ = _run(["ps", "-eo", "comm"])
    if rc == 0:
        daemon = any(line.strip() in {"cron", "crond"} for line in out.splitlines())
    backends.append(
        {
            "name": "cron",
            "client": bool(crontab),
            "usable": bool(crontab) and daemon,
            "detail": (
                "client present, no cron daemon is running, so an entry would never fire"
                if crontab and not daemon
                else "crontab is not installed"
                if not crontab
                else "cron daemon is running"
            ),
        }
    )

    systemctl = shutil.which("systemctl")
    bus = False
    if systemctl:
        rc, _, _ = _run([systemctl, "--user", "show", "--property=Version"])
        bus = rc == 0
    backends.append(
        {
            "name": "systemd-user",
            "client": bool(systemctl),
            "usable": bus,
            "detail": (
                "systemctl present but no user bus is reachable"
                if systemctl and not bus
                else "systemctl is not installed"
                if not systemctl
                else "user bus is reachable"
            ),
        }
    )

    return backends


def _run(args: list[str]) -> tuple[int, str, str]:
    """Run a non-git command, tolerating its absence."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, check=False, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _schedule_config() -> dict:
    path = Path(os.environ.get(SCHEDULE_CONFIG_ENV) or DEFAULT_SCHEDULE_CONFIG)
    return tomllib.loads(path.read_text(encoding="utf-8"))["ai_core_scheduler"]


def ai_core_scheduler_backend(now: datetime | None = None) -> dict:
    """Whether ai-core's scheduler is firing this job, judged from its run ledger.

    Evidence of firing, not of configuration: ai-core's arming file can exist while the
    worker is down, so only a ``run_log`` row that started inside the freshness window
    counts. The ledger is opened read-only and any failure is a reported reason.
    """
    backend = {
        "name": "ai-core-scheduler",
        "client": False,
        "usable": False,
        "last_run": None,
        "last_status": None,
    }
    try:
        config = _schedule_config()
        job = str(config["job_name"])
        ledger = Path(str(config["ledger"])).expanduser()
        window = float(config["freshness_hours"])
    except (OSError, KeyError, ValueError, tomllib.TOMLDecodeError) as exc:
        backend["detail"] = f"schedule config unusable: {exc!r}"
        return backend

    if not ledger.is_file():
        backend["detail"] = f"ledger not found at {ledger}"
        return backend
    backend["client"] = True
    try:
        conn = sqlite3.connect(f"{ledger.as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT started_at, status FROM run_log WHERE job_name = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (job,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        backend["detail"] = f"ledger unreadable at {ledger}: {exc}"
        return backend
    if row is None:
        backend["detail"] = f"no {job} run recorded in {ledger}"
        return backend

    started_at, status = row
    try:
        started = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        backend["detail"] = (
            f"ledger unreadable at {ledger}: bad started_at {started_at!r}"
        )
        return backend
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    age_hours = ((now or datetime.now(UTC)) - started).total_seconds() / 3600
    backend["last_run"] = started.isoformat()
    backend["last_status"] = status
    backend["usable"] = age_hours <= window
    backend["detail"] = (
        f"{job} last started {started.isoformat()} ({age_hours:.1f} h ago, status "
        f"{status}); {'within' if backend['usable'] else 'OUTSIDE'} the {window:g} h "
        "freshness window"
        + ("" if backend["usable"] else ", so the job has stopped reporting")
    )
    return backend


def schedule_report() -> tuple[list[dict], bool]:
    """The backends plus whether this job is actually installed on any of them."""
    backends = schedule_backends()
    rc, out, _ = _run(["crontab", "-l"])
    in_crontab = rc == 0 and Path(__file__).name in out
    ai_core = ai_core_scheduler_backend()
    backends.append(ai_core)
    return backends, in_crontab or ai_core["usable"]


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _render(results: list[RepoResult], *, apply: bool, truncated: bool) -> None:
    granted = [r for r in results if r.granted]
    refused = [r for r in results if not r.granted]

    print(f"anchor-worktree-branches: {'APPLY' if apply else 'SURVEY (no pushes)'}")
    print()
    for result in granted:
        head = f"  {result.name}  remote={result.remote or 'NONE'}"
        print(head)
        for branch in result.branches:
            if branch.status == ANCHORED:
                continue
            print(f"      [{branch.status}] {branch.name} -- {branch.reason}")
        anchored = sum(1 for b in result.branches if b.status == ANCHORED)
        print(
            f"      {anchored} already anchored, {len(result.unanchored)} unanchored, "
            f"{len(result.pushed)} pushed, {len(result.failed)} failed"
        )
        for error in result.errors:
            print(f"      ERROR {error}")
    if refused:
        print()
        print(f"  REFUSED -- these repos do not grant `{AUTHORITY_KEY}`:")
        for result in refused:
            at_risk = len(result.unanchored)
            note = (
                f"{at_risk} branch(es) hold commits on no remote ref -- pushing them is "
                "human-gated here"
                if at_risk
                else "nothing unanchored"
            )
            print(f"      {result.name}: {note}")
    if truncated:
        print()
        print("  TRUNCATED: the per-run push cap was reached; re-run to continue.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Anchor local branches to a remote ref so committed work survives losing this "
            "machine. Surveys by default; --apply pushes."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually push. Without it nothing is pushed.",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="REPO",
        help="restrict to this repo (repeatable). Naming a repo without authority refuses.",
    )
    parser.add_argument(
        "--max-pushes",
        type=int,
        default=DEFAULT_MAX_PUSHES,
        help=f"cap pushes for the whole run (default {DEFAULT_MAX_PUSHES})",
    )
    parser.add_argument(
        "--timeout", type=int, default=900, help="per-push timeout, seconds"
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable output"
    )
    parser.add_argument(
        "--check-schedule",
        action="store_true",
        help="report scheduler backends and whether this job is installed; non-zero if not",
    )
    parser.add_argument(
        "--projects-dir",
        type=Path,
        default=None,
        help="fleet projects root (defaults to the main checkout's parent)",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=core.DEFAULT_REGISTRY,
        help="path to fleet-projects.toml",
    )
    args = parser.parse_args(argv)

    if args.check_schedule:
        backends, installed = schedule_report()
        if args.json:
            print(json.dumps({"backends": backends, "installed": installed}, indent=2))
        else:
            print("scheduler backends:")
            for backend in backends:
                mark = "usable" if backend["usable"] else "UNUSABLE"
                print(f"  {backend['name']}: {mark} -- {backend['detail']}")
            print()
            print(
                f"this job installed on a schedule: {'yes' if installed else 'NO'}"
                if installed
                else "this job installed on a schedule: NO -- nothing anchors work "
                "periodically, so protection is manual only"
            )
        return EXIT_OK if installed else EXIT_ADVISORY

    if args.max_pushes < 0:
        parser.error("--max-pushes cannot be negative")

    try:
        registry = core.load_registry(args.registry)
    except core.RegistryUnreadable as exc:
        print(f"anchor-worktree-branches: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    projects_dir = args.projects_dir or core.default_projects_dir()
    repos, skips = core.discover_repos(projects_dir, registry, only=args.only)

    if args.only:
        for name in args.only:
            config = registry.get(name, {})
            if config.get(AUTHORITY_KEY) is not True:
                print(
                    f"anchor-worktree-branches: REFUSED {name} -- it does not grant "
                    f"`{AUTHORITY_KEY}` in {args.registry}. Pushing it is human-gated; "
                    "this job will not push a repo it was not granted, even when asked "
                    "for by name.",
                    file=sys.stderr,
                )
                return EXIT_REFUSED

    budget = [args.max_pushes]
    results = [
        anchor_repo(
            repo.path,
            repo.name,
            registry.get(repo.name, {}),
            apply=args.apply,
            budget=budget,
            timeout=args.timeout,
        )
        for repo in repos
    ]

    truncated = args.apply and budget[0] <= 0 and any(r.unanchored for r in results)

    if args.json:
        print(
            json.dumps(
                {
                    "apply": args.apply,
                    "truncated": truncated,
                    "skips": [
                        {"subject": s.subject, "reason": s.reason} for s in skips
                    ],
                    "repos": [
                        {
                            "name": r.name,
                            "path": str(r.path),
                            "granted": r.granted,
                            "remote": r.remote,
                            "errors": r.errors,
                            "branches": [
                                {
                                    "name": b.name,
                                    "tip": b.tip,
                                    "status": b.status,
                                    "reason": b.reason,
                                }
                                for b in r.branches
                            ],
                        }
                        for r in results
                    ],
                },
                indent=2,
            )
        )
    else:
        _render(results, apply=args.apply, truncated=truncated)

    if any(r.failed for r in results) or any(r.errors for r in results):
        return EXIT_FAILURE
    if truncated:
        return EXIT_ADVISORY
    if any(not r.granted and r.unanchored for r in results):
        return EXIT_ADVISORY
    if any(r.granted and r.unanchored for r in results):
        return EXIT_ADVISORY
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
