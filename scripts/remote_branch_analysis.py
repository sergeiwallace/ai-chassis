#!/usr/bin/env python3
# ruff: noqa: E402
"""Analyse remote branches and recommend a disposition. Never mutates anything.

Config-driven: the config names the repo, its target ref, and the branch scope (every
branch, or one author's). Output is a git-tracked run log plus a JSON checkpoint.

Three operational properties, each of which exists because its absence was measured on
this fleet:

*Report-only.* It never merges and never deletes. A deletion recommendation is a
sentence in a log file for a human to act on. `worktree_audit.py` couples analysis to
`--delete` at repo scope, so reaping one worktree adjudicates every other worktree in
the repo (ISSUE-REF); this script has no mutating mode to couple to.

*Checkpointed and resumable per branch.* A whole-repo pass does not fit in one run:
`worktree_audit.py` exits 124 at 600s here (ISSUE-REF) and the CLI's Bash tool caps at
600s whatever timeout you ask for, so a long pass WILL be killed. State is flushed after
every branch, so a restart resumes rather than starting over.

*Periodic heartbeat.* A background job that prints nothing for ~6 minutes is reaped as
idle. Some branches need a fetch that takes minutes, so silence is not evidence of being
stuck -- but the reaper cannot tell. It therefore emits progress on a fixed interval
regardless of how slow the current branch is.

Usage:
    remote_branch_analysis.py --config <path> [--resume] [--limit N]
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
import json
import os
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HEARTBEAT_SECONDS = 45.0
GIT_TIMEOUT = 120

# refname, objectname, authoremail, committerdate -- the four fields the for-each-ref
# format below requests. A row with any other count is malformed and is skipped.
REF_FIELDS = 4

# `git merge-tree --write-tree` landed in git 2.38. Its absence is a hard failure rather
# than a fallback: it is one of the two independent landed proofs this script relies on,
# and every test it could silently fall back to is one measured to under-report
# (ISSUE-REF). Losing a proof would turn a KEEP-because-uncertain into a KEEP-because-blind
# with no way to tell them apart in the log.
MERGE_TREE_MIN_GIT = (2, 38)

# Rows are classified from measurements whose meaning changed in ISSUE-REF. A checkpoint
# written before that carries the OLD verdicts, and `--resume` would copy them into the
# new run log unre-measured -- reporting the under-report as though it were the fix.
CHECKPOINT_SCHEMA = 3

# `git cherry` recomputes patch-ids for every upstream commit, so its cost scales with how
# far BEHIND the branch is, not how much it changed, and it costs <= 3s for every other call
# in this script.
#
# Re-measured on example-project-sagemaker 2026-09-11 (ISSUE-REF), uncontended, both rc=0:
#   origin/wt/statusline-cxwk-bugfix     3645 behind -> 339s
#   origin/wt-git-sync-orchestrate-skill 4575 behind -> 297s
# The earlier note here recorded 140s and the budget was set to 60s, so the budget was short
# by 5-6x against a cost that is TYPICAL on this repo rather than pathological -- ai-harness
# branches are routinely 3600-4575 commits behind. The proof therefore could not complete,
# and because a timeout records the proof as not obtained (which errs to KEEP), the failure
# was invisible: the pass still exits 0 and every row still reads plausibly. A real
# `--patch-id-proof` run had the proof ATTEMPTED and unobtained on 13 of its first 16 rows.
#
# A budget below the measured cost is not a bound, it is an off switch. Keep the budget
# above the measurement, and let the aggregate accounting in write_run_log() surface any
# remaining blindness instead of hiding it one line deep in each entry.
PATCH_ID_MEASURED_WORST_SECONDS = 339
PATCH_ID_TIMEOUT = 420


def log(msg: str) -> None:
    """Unbuffered, timestamped. Buffered output defeats the point of a heartbeat."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


#: What ``install.sh`` exports for "where this machine's repos live". Resolving the
#: checkout at run time is the whole point of ISSUE-REF: the configs here named the
#: retired SageMaker EFS path, so every run on example-project-ec2 refused with
#: ``repo is not a git checkout: /mnt/custom-file-systems/efs/...``.
PROJECTS_DIR_ENV_VAR = "PROJECTS_DIR"


def projects_root() -> tuple[Path, str]:
    """``(this machine's projects root, the source that answered)``.

    ``~/projects`` is the fallback, not the answer: it is a near-empty TRAP on
    example-project-sagemaker, whose real root is ``~/user-default-efs/projects``. So the source is
    returned and logged rather than inferred, which is what makes the fallback observable
    instead of a silently wrong root (AC-3).
    """
    configured = os.environ.get(PROJECTS_DIR_ENV_VAR, "").strip()
    if configured:
        return Path(os.path.expanduser(configured)), PROJECTS_DIR_ENV_VAR
    return (
        Path.home() / "projects",
        f"${PROJECTS_DIR_ENV_VAR} unset, ~/projects default",
    )


def state_root() -> Path:
    """The user-owned XDG state root -- where a relative checkpoint lands.

    Same root ``scripts/worktree_policy.state_root`` uses, so this is not a new location.
    """
    configured = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(configured) if configured else Path.home() / ".local" / "state"
    return base / "ai-harness"


def git(repo: str, *args: str, timeout: int = GIT_TIMEOUT) -> tuple[int, str]:
    try:
        p = subprocess.run(  # noqa: S603 - fixed argv, resolved executable
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout.strip()
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return 1, f"ERROR {exc}"


def git_version(repo: str) -> tuple[int, ...]:
    rc, out = git(repo, "--version")
    if rc != 0:
        raise SystemExit(f"cannot determine git version: {out}")
    digits = [p for p in out.split() if p and p[0].isdigit()]
    if not digits:
        raise SystemExit(f"cannot parse git version from {out!r}")
    parts: list[int] = []
    for chunk in digits[0].split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts)


def require_merge_tree(repo: str) -> None:
    """Refuse to run at all rather than silently answer the wrong question.

    Every cheaper test available here is known-broken for the case that matters, so a
    graceful degradation would produce a confident under-report instead of an error --
    which is the defect being fixed, not a mitigation of it.
    """
    have = git_version(repo)
    if have < MERGE_TREE_MIN_GIT:
        want = ".".join(str(n) for n in MERGE_TREE_MIN_GIT)
        raise SystemExit(
            f"git {'.'.join(str(n) for n in have)} is too old: `merge-tree --write-tree` "
            f"needs >= {want}, and there is no safe fallback -- ancestry and tree diffs "
            "both under-report a squash-merged branch (ISSUE-REF). Upgrade git."
        )


def merge_adds_nothing(cfg: Config, sha: str) -> tuple[bool, str]:
    """Would merging this branch into the target change the target's content at all?

    This is the landed question, asked the only way that survives the target moving on.
    A three-way merge is evaluated against the merge base, so unrelated commits the
    target has since accumulated stay put; if the branch contributes nothing, the merged
    tree is byte-identical to the target's tree.

    Measured on this repo 2026-09-10: `docs/pass26-worktree-upstreams` was squash-merged
    as PR #741, and `git cherry` still reported all 4 of its commits unlanded -- a squash
    collapses N commits into one patch, so no individual patch-id matches upstream. The
    merged tree was identical, which is the truth. Ancestry misses the same case, and a
    per-file identity check misses it too because the target had moved on.

    A conflict is reported as NOT landed. That is the safe direction: it means the branch
    holds content git cannot reconcile with the target, so the branch is kept.
    """
    rc, target_tree = git(cfg.repo, "rev-parse", f"{cfg.target}^{{tree}}")
    if rc != 0:
        return False, f"cannot resolve target tree: {target_tree}"
    rc, merged = git(cfg.repo, "merge-tree", "--write-tree", cfg.target, sha)
    if rc != 0:
        return (
            False,
            "merging into the target conflicts, so it holds unreconciled content",
        )
    merged_tree = merged.splitlines()[0].strip() if merged else ""
    if merged_tree == target_tree:
        return True, "merging it into the target produces the target's tree unchanged"
    rc, names = git(cfg.repo, "diff", "--name-only", target_tree, merged_tree)
    n = len([f for f in names.splitlines() if f.strip()]) if rc == 0 else "?"
    return False, f"merging it into the target would change {n} file(s)"


def _resolve_repo(value: str, root: Path, root_source: str) -> str:
    """Resolve a configured checkout, which is normally a NAME under the projects root.

    An absolute path is still honoured -- an ad hoc config pointing at a checkout outside
    the projects root is legitimate -- but it is no longer what the shipped configs use,
    because an absolute path is exactly the fact that stops being true on the next machine.
    A relative path is resolved against the projects root rather than the CWD: resolving
    against the CWD would make the same config mean different things per directory, the
    same trap a relative uv ``path`` falls into inside a worktree.
    """
    expanded = os.path.expanduser(value)
    if os.path.isabs(expanded):
        return expanded
    log(f"projects root: {root} (from {root_source})")
    return str(root / expanded)


def _resolve_checkpoint(value: str) -> str:
    """Resolve the checkpoint path, creating its directory when we chose the directory.

    A relative checkpoint lands under the XDG state root, so a config does not have to
    name one machine's home (the shipped ones named ``/home/example-user/example-logs``,
    which exists on no current machine). An absolute value is left exactly as written.
    """
    if not value:
        return ""
    expanded = os.path.expanduser(value)
    if os.path.isabs(expanded):
        return expanded
    resolved = state_root() / expanded
    resolved.parent.mkdir(parents=True, exist_ok=True)
    log(f"checkpoint: {resolved} (relative to the XDG state root)")
    return str(resolved)


@dataclass
class Config:
    repo: str
    target: str
    scope: str = "all"  # "all" | "author"
    authors: list[str] = field(default_factory=list)
    exclude_refs: list[str] = field(default_factory=list)
    run_log: str = ""
    checkpoint: str = ""

    @classmethod
    def load(cls, path: str) -> Config:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        section = raw.get("analysis", raw)
        missing = [k for k in ("repo", "target") if not section.get(k)]
        if missing:
            raise SystemExit(f"config {path}: missing required key(s) {missing}")
        root, root_source = projects_root()
        cfg = cls(
            repo=_resolve_repo(section["repo"], root, root_source),
            target=section["target"],
            scope=section.get("scope", "all"),
            authors=list(section.get("authors", [])),
            exclude_refs=list(section.get("exclude_refs", [])),
            run_log=section.get("run_log", ""),
            checkpoint=_resolve_checkpoint(section.get("checkpoint", "")),
        )
        if cfg.scope == "author" and not cfg.authors:
            raise SystemExit(
                f"config {path}: scope='author' requires a non-empty authors list"
            )
        if cfg.run_log and not os.path.isabs(cfg.run_log):
            cfg.run_log = os.path.join(cfg.repo, cfg.run_log)
        if not os.path.isdir(os.path.join(cfg.repo, ".git")) and not os.path.exists(
            os.path.join(cfg.repo, ".git")
        ):
            raise SystemExit(
                f"config {path}: repo is not a git checkout: {cfg.repo} "
                f"(projects root {root} from {root_source})"
            )
        return cfg


def all_refs(cfg: Config) -> list[tuple[str, str, str, str]]:
    """Every candidate remote branch, before the author filter."""
    rc, out = git(
        cfg.repo,
        "for-each-ref",
        "--format=%(refname)\t%(objectname)\t%(authoremail)\t%(committerdate:iso8601)",
        "refs/remotes/origin/",
    )
    if rc != 0:
        raise SystemExit(f"cannot list remote refs: {out}")
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != REF_FIELDS:
            continue
        ref, sha, email, when = parts
        short = ref.replace("refs/remotes/", "")
        if short in ("origin", "origin/HEAD") or short == cfg.target:
            continue
        if any(short == f"origin/{x}" for x in cfg.exclude_refs):
            continue
        rows.append((short, sha, email.strip("<>"), when))
    return rows


SELF_TOKEN = "@self"


def resolve_authors(cfg: Config) -> list[str]:
    """Expand the ``@self`` token to the repo's configured identity.

    A tracked config must not carry a literal address: it is personal data on a surface
    intended to be publishable, and the repo's own gitleaks rule rejects it. ``@self``
    also makes the config genuinely portable -- the same file selects "my branches" on any
    checkout, which is what a config-driven tool should mean by an author scope.
    """
    out: list[str] = []
    for a in cfg.authors:
        if a != SELF_TOKEN:
            out.append(a)
            continue
        rc, email = git(cfg.repo, "config", "user.email")
        if rc != 0 or not email:
            raise SystemExit(
                f"config names {SELF_TOKEN} but this repo has no user.email configured, "
                "so 'my branches' cannot be resolved; set it or name the author literally"
            )
        out.append(email)
    return out


def remote_branches(cfg: Config) -> tuple[list[tuple[str, str, str, str]], int]:
    """(in-scope rows, total candidate count). The total is what makes a zero
    in-scope result distinguishable from an empty remote."""
    rows = all_refs(cfg)
    total = len(rows)
    if cfg.scope == "author":
        wanted = resolve_authors(cfg)
        rows = [r for r in rows if any(a.lower() in r[2].lower() for a in wanted)]
    rows.sort(key=lambda r: r[3])
    return rows, total


def worktree_map(cfg: Config) -> dict[str, str]:
    """branch -> worktree path, read ONCE per run rather than once per branch.

    This was a `git worktree list` per branch. On a repo with 265 refs that is 265
    identical subprocess calls returning the same answer, and the first full pass was
    killed at the 570s ceiling having finished 24 branches. The set of worktrees does not
    change while a report-only pass runs, and if it did, a half-old half-new answer would
    be worse than a consistent snapshot.
    """
    rc, out = git(cfg.repo, "worktree", "list", "--porcelain")
    if rc != 0:
        return {}
    mapping: dict[str, str] = {}
    cur = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = line.split(" ", 1)[1]
        elif line.startswith("branch ") and cur:
            mapping[line.split(" ", 1)[1].replace("refs/heads/", "")] = cur
    return mapping


def analyse(
    cfg: Config,
    row_in: tuple[str, str, str, str],
    worktrees: dict[str, str] | None = None,
    *,
    patch_id_proof: bool = False,
) -> dict:
    """Everything reported here is measured; nothing is inferred from the name.

    ``row_in`` is one row exactly as ``remote_branches`` yields it -- (ref, sha, author
    email, last commit date). Passing the tuple rather than four positionals keeps the
    caller and this function from drifting out of order, which a four-string signature
    invites.
    """
    ref, sha, email, when = row_in
    branch = ref.replace("origin/", "", 1)
    row: dict = {
        "ref": ref,
        "sha": sha,
        "author": email,
        "last_commit": when,
        "target": cfg.target,
    }

    rc_anc, _ = git(cfg.repo, "merge-base", "--is-ancestor", sha, cfg.target)
    merged_by_ancestry = rc_anc == 0
    row["merged_by_ancestry"] = merged_by_ancestry

    rc, ab = git(
        cfg.repo, "rev-list", "--left-right", "--count", f"{sha}...{cfg.target}"
    )
    ahead, behind = (ab.split() + ["?", "?"])[:2] if rc == 0 else ("?", "?")
    row["ahead_of_target"] = ahead
    row["behind_target"] = behind

    # The branch's OWN contribution since the merge base. This is diagnostic ONLY -- it
    # is NOT a landed test, and treating it as one is the defect ISSUE-REF records: it is
    # non-empty for any branch that did anything, so "0 files" only ever fired for a
    # branch that changed nothing at all, and "differs from the target" mostly means the
    # target is newer rather than that the branch has unlanded work.
    rc, diff = git(cfg.repo, "diff", "--name-only", f"{cfg.target}...{sha}")
    changed = [f for f in diff.splitlines() if f.strip()] if rc == 0 else []
    row["files_vs_target"] = len(changed)
    row["beads_only"] = bool(changed) and all(f.startswith(".beads/") for f in changed)

    by_merge, why = merge_adds_nothing(cfg, sha)
    row["landed_by_merge"] = by_merge

    # The patch-id proof is a SECOND CHANCE, so it only runs when the first proof failed,
    # and only when asked for. Measured on origin/wt/statusline-cxwk-bugfix (3491 behind):
    # `git cherry` took 140s while every other call here took <= 3s, because it recomputes
    # patch-ids for every upstream commit -- per branch. At 258 branches that is 10 hours.
    #
    # Skipping it can only ever produce a KEEP, never a delete, so the safety property is
    # unaffected and only recall drops. That asymmetry is the whole reason this is allowed
    # to be optional, and it is why the row records WHICH proofs were actually attempted:
    # "kept because both proofs failed" and "kept because only one proof was available" are
    # different facts, and a log that cannot distinguish them is the KEEP-because-blind
    # problem in another place.
    attempted_patch_id = bool(patch_id_proof) and not by_merge
    if attempted_patch_id:
        rc, cherry = git(cfg.repo, "cherry", cfg.target, sha, timeout=PATCH_ID_TIMEOUT)
        row["unlanded_commits"] = (
            len([ln for ln in cherry.splitlines() if ln.startswith("+")])
            if rc == 0
            else None
        )
    else:
        row["unlanded_commits"] = None
    row["patch_id_proof_attempted"] = attempted_patch_id
    by_patch_id = row["unlanded_commits"] == 0
    row["landed_by_patch_id"] = by_patch_id

    # EITHER proof suffices, because the two are independently sound and each has a
    # measured blind spot the other covers. Patch-id misses a multi-commit squash. The
    # merge test misses a branch whose work landed and whose lines the target then edited
    # again -- measured on ai-core/chore/beads-heartbeat-gitignore, where every commit was
    # patch-equivalent upstream (cherry=0) yet merging conflicted. Requiring both would
    # reintroduce the under-report from the opposite side.
    row["landed"] = by_merge or by_patch_id
    row["landed_evidence"] = (
        why
        if by_merge
        else (
            "every commit on it has a patch-equivalent commit upstream, so it holds no "
            "unique patch even though merging it now conflicts with later edits"
            if by_patch_id
            else why
        )
    )

    wt = (worktree_map(cfg) if worktrees is None else worktrees).get(branch)
    row["local_worktree"] = wt

    # In sync with its own remote ref? Only meaningful when a local branch exists.
    rc, local_sha = git(cfg.repo, "rev-parse", f"refs/heads/{branch}")
    if rc == 0:
        row["local_sha"] = local_sha
        row["in_sync_with_remote"] = local_sha == sha
        rc2, lab = git(
            cfg.repo, "rev-list", "--left-right", "--count", f"{local_sha}...{sha}"
        )
        row["local_vs_remote"] = lab if rc2 == 0 else "?"
    else:
        row["local_sha"] = None
        row["in_sync_with_remote"] = None
        row["local_vs_remote"] = None

    row["classification"], row["recommendation"], row["reasoning"] = classify(row)
    return row


def patch_id_note(row: dict) -> str:
    """Say plainly whether the patch-id proof was obtained, missing, or not attempted.

    "Kept because both proofs failed" and "kept because only one proof was available" are
    different facts about a branch, and a log that prints the same sentence for both turns a
    KEEP-because-uncertain into a KEEP-because-blind with no way to tell them apart.
    """
    if not row.get("patch_id_proof_attempted"):
        return "the patch-id proof was not attempted (--patch-id-proof off)"
    n = row.get("unlanded_commits")
    if n is None:
        return f"the patch-id proof did not complete within {PATCH_ID_TIMEOUT}s"
    return f"{n} commit(s) have no patch-equivalent upstream"


def classify(row: dict) -> tuple[str, str, str]:
    """Map measurements onto a disposition.

    An ORDERED rules table rather than a chain of returns. Order is the whole safety
    property: every "safe to delete" rule must be evaluated before any "keep" rule, and
    the fallback must be a KEEP -- so a measurement combination nobody anticipated is
    retained rather than recommended for deletion. A table makes that order reviewable in
    one place instead of spread across branches.
    """
    rules: list[tuple[bool, str, str, str]] = [
        (
            bool(row["merged_by_ancestry"] and row["local_worktree"]),
            "merged",
            "reap worktree then delete remote ref",
            "an ancestor of the target, so every commit is already published; a local "
            "worktree still holds it, so reap that first",
        ),
        (
            bool(row["merged_by_ancestry"]),
            "merged",
            "SAFE TO DELETE remote ref",
            "an ancestor of the target: nothing on this ref is unique to it",
        ),
        (
            bool(row["landed"] and row["local_worktree"]),
            "merged-by-content",
            "reap worktree then delete remote ref",
            f"not an ancestor ({row['ahead_of_target']} ahead) but {row['landed_evidence']}; "
            "a local worktree still holds it, so reap that first",
        ),
        (
            bool(row["landed"]),
            "merged-by-content",
            "SAFE TO DELETE remote ref",
            f"not an ancestor ({row['ahead_of_target']} ahead) but {row['landed_evidence']}, "
            f"which is what a squash merge looks like ({patch_id_note(row)})",
        ),
        (
            bool(row["beads_only"]),
            "beads-bookkeeping-only",
            "SAFE TO DELETE remote ref",
            f"its only difference from the target is {row['files_vs_target']} .beads/ "
            "mirror file(s), which the store regenerates",
        ),
        (
            bool(row["local_worktree"]),
            "has-local-worktree",
            "KEEP -- resolve via its worktree",
            f"{row['files_vs_target']} file(s) differ from the target and a worktree at "
            f"{row['local_worktree']} holds the branch; disposition belongs to that "
            "worktree",
        ),
        (
            row["in_sync_with_remote"] is False,
            "diverged-from-local",
            "KEEP -- reconcile local and remote first",
            f"local and remote disagree ({row['local_vs_remote']} left/right), so neither "
            "side is a complete record of the work",
        ),
    ]
    for matched, kind, rec, why in rules:
        if matched:
            return kind, rec, why
    return (
        "unique-work-no-worktree",
        "KEEP -- salvage or finish",
        f"{row['landed_evidence']}, and no worktree holds it, so deleting this remote ref "
        f"destroys the sole copy of what it adds ({row['ahead_of_target']} commit(s) ahead; "
        f"{patch_id_note(row)})",
    )


def write_run_log(cfg: Config, rows: list[dict], started: str) -> None:
    if not cfg.run_log:
        return
    os.makedirs(os.path.dirname(cfg.run_log) or ".", exist_ok=True)
    safe = [r for r in rows if r["recommendation"].startswith("SAFE TO DELETE")]
    keep = [r for r in rows if r["recommendation"].startswith("KEEP")]
    other = [r for r in rows if r not in safe and r not in keep]
    with open(cfg.run_log, "w", encoding="utf-8") as fh:
        w = fh.write
        w(f"# Remote-branch analysis — {os.path.basename(cfg.repo)}\n\n")
        w(f"- Run started: {started}\n")
        w(f"- Target ref: `{cfg.target}`\n")
        w(f"- Scope: {cfg.scope}")
        if cfg.scope == "author":
            w(f" ({', '.join(cfg.authors)})")
        w(f"\n- Branches analysed: {len(rows)}\n")

        # Aggregate the patch-id outcomes in the HEADER. Each entry already carries its own
        # note, but buried one line deep inside every KEEP it is unreadable at scale: a real
        # run whose proof timed out on 13 of its first 16 branches reported nothing but
        # "Branches analysed", and was taken for a sound pass. A budget that cannot be met
        # degrades this whole tool to its weaker proof, so that has to be the first thing
        # the reader sees, not something only the JSON checkpoint reveals.
        attempted = [r for r in rows if r.get("patch_id_proof_attempted")]
        blind = [r for r in attempted if r.get("unlanded_commits") is None]
        w(
            f"- Patch-id proof: {len(attempted)} attempted, "
            f"{len(attempted) - len(blind)} obtained, "
            f"{len(blind)} did not complete within {PATCH_ID_TIMEOUT}s; "
            f"{len(rows) - len(attempted)} not attempted\n"
        )
        if blind:
            w(
                f"\n> **{len(blind)} of {len(attempted)} patch-id proofs did not complete.** "
                "An unobtained proof errs to KEEP, so those KEEPs are "
                "keep-because-blind rather than keep-because-checked, and this pass "
                "cannot be read as ruling out that they landed by squash merge. Raise "
                "`PATCH_ID_TIMEOUT` above the measured cost and re-run before treating "
                "any of them as holding unique work.\n"
            )
        w(
            "\n**This is analysis only.** Nothing here was merged, deleted or otherwise "
            "mutated; every line is a recommendation for a human to act on.\n"
        )
        w(
            "\n> **How `merged-by-content` is decided (ISSUE-REF).** Three cheaper tests were "
            "measured wrong on this repo and none of them decides anything here. Ancestry "
            "misses a squash merge by construction. A three-dot diff against the target is "
            "non-empty for any branch that did anything. Per-file identity fails too, because "
            "the target keeps moving: 5 of 7 files differed on a branch squash-merged an hour "
            "earlier. Even patch-id (`git cherry`) misses a MULTI-commit squash — the single "
            "upstream patch equals the sum of the branch's patches, so no individual patch-id "
            "matches, and all 4 commits of a branch merged as PR #741 still read as unlanded.\n"
            ">\n"
            "> Two independent proofs are accepted, and **either** is enough. First: would "
            "merging this branch into the target change the target's content at all? A "
            "three-way merge is evaluated from the merge base, so commits the target has "
            "since accumulated stay put, and a branch contributing nothing yields a tree "
            "byte-identical to the target's. Second: does every commit on the branch have a "
            "patch-equivalent commit upstream? Each covers the other's measured blind spot "
            "— patch-id misses a multi-commit squash, and the merge test misses a branch "
            "whose work landed and whose lines the target then edited again (measured on "
            "`ai-core/chore/beads-heartbeat-gitignore`: cherry=0, merge conflicts). When "
            "both fail, the branch is KEPT.\n"
        )
        for title, sel in (
            (f"Safe to delete ({len(safe)})", safe),
            (f"Keep ({len(keep)})", keep),
            (f"Other ({len(other)})", other),
        ):
            if not sel:
                continue
            w(f"\n## {title}\n\n")
            for r in sel:
                w(f"### `{r['ref']}`\n\n")
                w(f"- Classification: **{r['classification']}**\n")
                w(f"- Recommendation: **{r['recommendation']}**\n")
                w(f"- Reasoning: {r['reasoning']}\n")
                w(
                    f"- Measured: {r['ahead_of_target']} ahead / {r['behind_target']} behind; "
                    f"{patch_id_note(r)}; "
                    f"{r['files_vs_target']} file(s) in its own three-dot diff (diagnostic "
                    f"only); last commit {r['last_commit']}\n"
                )
                w(f"- Landed test: {r['landed_evidence']}\n")
                w(f"- Local worktree: {r['local_worktree'] or 'none'}\n")
                if r["in_sync_with_remote"] is not None:
                    w(
                        f"- Local branch in sync with remote: {r['in_sync_with_remote']} "
                        f"({r['local_vs_remote']})\n"
                    )
                w("\n")
    log(f"run log written: {cfg.run_log}")


def load_checkpoint(ckpt_path: str, *, resume: bool) -> tuple[dict[str, dict], str]:
    """(rows already analysed, run start time). Empty unless resuming.

    A schema mismatch is a hard refusal, not a silent reset: the rows carry verdicts, and
    a checkpoint written before the ISSUE-REF landed-test fix holds the UNDER-REPORT. Left
    to merge, `--resume` would copy those verdicts straight into the new run log without
    re-measuring a single branch, and the log would claim the fix it never applied.
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    if not (resume and os.path.exists(ckpt_path)):
        return {}, now
    with open(ckpt_path, encoding="utf-8") as fh:
        state = json.load(fh)
    found = state.get("schema", 1)
    if found != CHECKPOINT_SCHEMA:
        raise SystemExit(
            f"checkpoint {ckpt_path} was written by schema {found}, this is "
            f"{CHECKPOINT_SCHEMA}. Its verdicts predate the landed-test fix "
            "(ISSUE-REF) and resuming would copy them forward unre-measured. Delete "
            "the checkpoint and re-run without --resume."
        )
    done = {r["ref"]: r for r in state.get("rows", [])}
    log(f"resumed: {len(done)} branch(es) already analysed")
    return done, state.get("started", now)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument(
        "--resume", action="store_true", help="skip branches already in the checkpoint"
    )
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--patch-id-proof",
        action="store_true",
        help="also try the patch-id proof on branches the merge proof did not clear. "
        "Sound but SLOW: `git cherry` cost 140s on one branch 3491 commits behind, so a "
        "whole-repo pass with this on takes hours. Omitting it can only cause a KEEP.",
    )
    args = ap.parse_args()

    cfg = Config.load(args.config)
    ckpt_path = cfg.checkpoint or "/tmp/remote-branch-analysis.checkpoint.json"
    done, started = load_checkpoint(ckpt_path, resume=args.resume)

    require_merge_tree(cfg.repo)
    log(f"repo={cfg.repo} target={cfg.target} scope={cfg.scope}")

    # A RESUMED pass must not re-fetch, for two independent reasons.
    #
    # Correctness: the rows already in the checkpoint were measured against the refs as
    # they were. Re-fetching mid-pass judges later branches against a different target than
    # earlier ones, so a single run log would mix two snapshots and no reader could tell
    # which rows came from which.
    #
    # Progress: the fetch is the one step that is NOT resumable, and here it does not
    # finish. Measured 2026-09-10 in this repo, `git fetch --prune origin` had not
    # completed after 200s, and the analyser allowed it 540s inside a 560s pass. So every
    # resumed pass spent its entire budget re-fetching and classified ~1 branch. Six passes
    # made essentially no progress -- a resumable design defeated by re-doing its one
    # non-resumable step each time. Same shape as ISSUE-REF: the budget was unsatisfiable,
    # and the measured cost was the baseline rather than the defect.
    if args.resume and done:
        log(
            f"skipping fetch: resuming {len(done)} row(s) measured against existing refs"
        )
    else:
        log("fetching origin (prune) …")
        rc, out = git(cfg.repo, "fetch", "--prune", "origin", timeout=540)
        if rc != 0:
            log(
                f"WARNING: fetch returned {rc}: {out[:200]} — analysing possibly stale refs"
            )

    branches, total = remote_branches(cfg)
    log(f"{len(branches)} in-scope remote branch(es) of {total} on the remote")

    # A scoped run that selects NOTHING from a non-empty remote is a config error, not a
    # clean bill of health. Measured: authors=["Example User"] matched 0 of 87 refs on a
    # remote where the address is "example-user" -- one dot, and the run reported
    # "0 analysed, 0 safe to delete" as though the repo were tidy. Fail loudly instead,
    # and name the emails actually present so the fix is obvious.
    if total and not branches:
        seen = sorted({e for _, _, e, _ in all_refs(cfg)})
        raise SystemExit(
            f"FILTER SELECTED NOTHING: scope={cfg.scope} authors={cfg.authors} matched 0 "
            f"of {total} remote branch(es). This is treated as a config error rather than "
            f"an empty result.\nAuthor emails present on this remote:\n  "
            + "\n  ".join(seen)
        )

    worktrees = worktree_map(cfg)
    log(f"{len(worktrees)} local worktree(s) mapped once for the whole pass")

    def flush_checkpoint() -> None:
        with open(ckpt_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "schema": CHECKPOINT_SCHEMA,
                    "started": started,
                    "rows": list(done.values()),
                },
                fh,
                indent=1,
            )

    last_beat = time.time()
    for i, (ref, sha, email, when) in enumerate(branches, 1):
        if args.limit and len(done) >= args.limit:
            log(f"--limit {args.limit} reached; stopping cleanly (resumable)")
            break
        if ref in done:
            continue
        if time.time() - last_beat > HEARTBEAT_SECONDS:
            log(f"… still working: {i}/{len(branches)}")
            last_beat = time.time()
        log(f"[{i}/{len(branches)}] {ref}")
        branch = ref.replace("origin/", "", 1)
        done[ref] = {
            "ref": ref,
            "sha": sha,
            "author": email,
            "last_commit": when,
            "target": cfg.target,
            "ahead_of_target": "?",
            "behind_target": "?",
            "files_vs_target": "?",
            "patch_id_proof_attempted": False,
            "unlanded_commits": None,
            "landed_evidence": "analysis did not complete inside one pass",
            "local_worktree": worktrees.get(branch),
            "in_sync_with_remote": None,
            "classification": "analysis-incomplete",
            "recommendation": "KEEP -- analysis incomplete",
            "reasoning": "the prior pass ended while analysing this branch, so no "
            "delete proof was obtained; resume skips it instead of stalling forever",
        }
        # Record a safe KEEP before entering work that may outlive the external pass.
        # If the pass is killed, --resume can advance instead of retrying this ref forever.
        flush_checkpoint()
        row = analyse(
            cfg,
            (ref, sha, email, when),
            worktrees,
            patch_id_proof=args.patch_id_proof,
        )
        done[ref] = row
        log(f"    -> {row['classification']}: {row['recommendation']}")
        # Flush after EVERY branch: a kill between branches must cost one branch, not all.
        flush_checkpoint()
        last_beat = time.time()

    rows = list(done.values())
    write_run_log(cfg, rows, started)
    safe = sum(1 for r in rows if r["recommendation"].startswith("SAFE TO DELETE"))
    log(
        f"DONE: {len(rows)} analysed, {safe} safe to delete, {len(rows) - safe} keep/other"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
