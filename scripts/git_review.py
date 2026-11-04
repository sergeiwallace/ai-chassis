"""Combined Phase 1–4 /git-review proof, action, and reclamation wrapper."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

# ruff: noqa: E402
# ISSUE-REF: docs/procedures/worktree-workflow.md runs this as `python3 scripts/git_review.py`, so
# it bootstraps the interpreter floor before loading its siblings. Pattern and reasoning:
# check_code_quality_ratchet.py (ISSUE-REF follow-up).
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from py_interpreter_bootstrap import ensure_supported_python
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):
        """Stand in for the bootstrap when this file was copied without it.

        A partial copy cannot re-exec, because the module that knows how is the one
        missing. Below the floor, warn and let the real failure happen where it can be read.
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


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(filename)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


core = _load("git_review_core", "git_review_core.py")
ship_merge = _load("git_review_ship_merge", "git_review_ship_merge.py")
sweep = _load("git_review_sweep", "git_review_sweep.py")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Proof-gated combined git-review ship, merge, and reclamation. "
            "A bare run DELETES what it proves redundant (worktrees, remote "
            "branches, stashes, orphan directories): reclamation is on by "
            "default (ISSUE-REF). Use --dry-run to withhold every action; "
            "the --delete-* flags narrow reclamation to one class, they do "
            "not enable it."
        )
    )
    parser.add_argument("--projects-dir", type=Path)
    parser.add_argument("--registry", type=Path, default=core.DEFAULT_REGISTRY)
    parser.add_argument("--only", action="append", metavar="REPO")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--surface-only", action="store_true")
    parser.add_argument("--branch-limit", type=int)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--delete-worktrees", action="store_true")
    parser.add_argument("--delete-remote-branches", action="store_true")
    parser.add_argument("--drop-stashes", action="store_true")
    parser.add_argument("--delete-orphan-dirs", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> dict[str, object]:
    args = build_parser().parse_args(argv)
    registry = core.load_registry(args.registry)
    canonical_registry = args.registry.resolve() == core.DEFAULT_REGISTRY.resolve()
    projects_dir = args.projects_dir or core.default_projects_dir()
    repos, skips = core.discover_repos(projects_dir, registry, only=args.only)
    report: dict[str, object] = {
        "version": 2,
        "workflow_state": "UNCERTAIN",
        "ship_candidates": [],
        "merge_candidates": [],
        "ship_merge_actions": [],
        "reclamations": [],
        "reclamation_errors": [],
        "dry_run": args.dry_run or args.surface_only,
        "skips": [skip.__dict__ for skip in skips],
    }
    narrowed = (
        args.delete_worktrees
        or args.delete_remote_branches
        or args.drop_stashes
        or args.delete_orphan_dirs
    )
    reclaim = not (args.dry_run or args.surface_only) and not narrowed

    def wanted(flag: bool) -> bool | None:
        if args.dry_run or args.surface_only:
            return False
        return True if flag else None

    def record_reclamation(
        repo: core.Repo, config: dict, *, phase: str, readback: object | None = None
    ) -> None:
        try:
            result = sweep.sweep_repo(
                sweep.core.Repo(repo.name, repo.path, repo.registered),
                config,
                reclaim=reclaim if phase == "initial" else not args.dry_run,
                delete_worktrees=wanted(args.delete_worktrees),
                delete_remote_branches=wanted(args.delete_remote_branches),
                drop_stashes=wanted(args.drop_stashes),
                delete_orphan_dirs=wanted(args.delete_orphan_dirs),
                branch_limit=args.branch_limit,
                fetch=args.fetch,
            )
        except (
            OSError,
            RuntimeError,
        ) as exc:  # A failed inventory is evidence, never a deletion cue.
            report["reclamation_errors"].append(  # type: ignore[index]
                {"repo": repo.name, "phase": phase, "reason": str(exc)}
            )
            result = {"workflow_state": "UNCERTAIN", "reason": str(exc)}
        report["reclamations"].append(  # type: ignore[index]
            {
                "repo": repo.name,
                "phase": phase,
                "readback_proof": ship_merge.jsonable(readback),
                "result": ship_merge.jsonable(result),
            }
        )

    for repo in repos:
        config = registry.get(repo.name, {})
        # Reclamation is the retained default workflow.  It runs even where no
        # ship/merge profile exists; surface/dry-run merely makes it report-only.
        record_reclamation(repo, config, phase="initial")
        try:
            profiles, policy = ship_merge.policy_for_repo(
                config, canonical=canonical_registry
            )
        except ship_merge.PolicyError as exc:
            report["ship_merge_actions"].append(
                {"repo": repo.name, "workflow_state": "REFUSED", "reason": str(exc)}
            )  # type: ignore[index]
            continue
        if not args.surface_only:
            ship_candidates = ship_merge.inventory_ship_candidates(
                repo.path, config, profiles
            )
            merge_candidates = (
                ship_merge.inventory_merge_candidates(repo.path, config, policy)
                if policy is not None
                else []
            )
            report["ship_candidates"].extend(
                ship_merge.jsonable(candidate) for candidate in ship_candidates
            )  # type: ignore[index]
            report["merge_candidates"].extend(
                ship_merge.jsonable(candidate) for candidate in merge_candidates
            )  # type: ignore[index]
            if not profiles:
                report["ship_merge_actions"].append(
                    {
                        "repo": repo.name,
                        "workflow_state": "NOT_APPLICABLE",
                        "reason": "SHIP_UNAUTHORIZED: no canonical literal opt-in profile",
                    }
                )  # type: ignore[index]
            else:
                for candidate in ship_candidates:
                    for profile in profiles:
                        proof = ship_merge.prove_ship(
                            candidate, profile, config, dry_run=args.dry_run
                        )
                        action = (
                            ship_merge.execute_ship(proof, dry_run=False)
                            if not args.dry_run
                            else None
                        )
                        report["ship_merge_actions"].append(
                            {
                                "repo": repo.name,
                                "workflow_state": (
                                    action.workflow_state
                                    if action is not None
                                    else proof.workflow_state
                                ),
                                "proof": ship_merge.jsonable(proof),
                                "action": ship_merge.jsonable(action),
                            }
                        )  # type: ignore[index]
                        readback = (
                            ship_merge.verify_shipment(action)
                            if action is not None
                            else None
                        )
                        if readback is not None and readback.state == "PASS":
                            record_reclamation(
                                repo,
                                config,
                                phase="post_ship_readback",
                                readback=readback,
                            )
            if policy is not None:
                for candidate in merge_candidates:
                    if policy is not None and policy.route == "queue":
                        proof = ship_merge.prove_queue_enqueue(
                            candidate, policy, config
                        )
                        anchored = proof
                    else:
                        proof = ship_merge.prove_merge(candidate, policy, config)
                        anchored = proof
                    action = (
                        ship_merge.execute_merge(anchored, dry_run=False)
                        if not args.dry_run
                        and anchored.workflow_state == "PREFLIGHT_READY"
                        else None
                    )
                    report["ship_merge_actions"].append(
                        {
                            "repo": repo.name,
                            "workflow_state": (
                                action.workflow_state
                                if action is not None
                                else anchored.workflow_state
                            ),
                            "proof": ship_merge.jsonable(anchored),
                            "action": ship_merge.jsonable(action),
                        }
                    )  # type: ignore[index]
                    # Reclamation must observe a post-action world, and only an
                    # authoritative terminal readback authorizes that rescan.
                    readback = (
                        ship_merge.verify_merge(action) if action is not None else None
                    )
                    if readback is not None and readback.state == "PASS":
                        record_reclamation(
                            repo,
                            config,
                            phase="post_merge_readback",
                            readback=readback,
                        )
            else:
                report["ship_merge_actions"].append(
                    {
                        "repo": repo.name,
                        "workflow_state": "NOT_APPLICABLE",
                        "reason": "MERGE_UNAUTHORIZED: no canonical literal opt-in policy",
                    }
                )  # type: ignore[index]
    actions = report["ship_merge_actions"]
    assert isinstance(actions, list)
    states = [
        action.get("workflow_state") for action in actions if isinstance(action, dict)
    ]
    if states:
        if any(state == "READBACK_FAILED" for state in states):
            report["workflow_state"] = "READBACK_FAILED"
        elif any(state == "ACTION_FAILED" for state in states):
            report["workflow_state"] = "ACTION_FAILED"
        elif any(state == "UNCERTAIN" for state in states):
            report["workflow_state"] = "UNCERTAIN"
        elif any(state == "REFUSED" for state in states):
            report["workflow_state"] = "REFUSED"
        elif report["reclamation_errors"]:
            report["workflow_state"] = "UNCERTAIN"
        elif any(state == "CANCELLED" for state in states):
            report["workflow_state"] = "CANCELLED"
        elif any(state == "TIMED_OUT" for state in states):
            report["workflow_state"] = "TIMED_OUT"
        elif any(state == "STALE" for state in states):
            report["workflow_state"] = "STALE"
        elif any(state == "MERGED" for state in states):
            report["workflow_state"] = "MERGED"
        elif any(state == "COMPLETE" for state in states):
            report["workflow_state"] = "COMPLETE"
        elif any(state == "PREFLIGHT_READY" for state in states):
            report["workflow_state"] = "PREFLIGHT_READY"
        elif all(state in {"NO_CHANGE", "NOT_APPLICABLE"} for state in states):
            report["workflow_state"] = "COMPLETE"
    elif not report["reclamation_errors"]:
        # A surface-only run still completed its ordinary per-repo reclamation
        # inventory; it is not an uncertain ship/merge action merely because
        # those expensive proof phases were intentionally omitted.
        report["workflow_state"] = "COMPLETE"
    return report


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run(argv)
    if args.json:
        print(json.dumps(report, sort_keys=True, default=str))
    else:
        print(f"git-review: {report['workflow_state']}")
        for action in report["ship_merge_actions"]:
            if isinstance(action, dict):
                print(
                    f"  {action.get('repo', 'unknown')}: "
                    f"{action.get('workflow_state', 'UNKNOWN')}"
                )
        for reclamation in report["reclamations"]:
            if isinstance(reclamation, dict):
                print(f"  {reclamation['repo']}: reclamation {reclamation['phase']}")
    return int(
        report["workflow_state"]
        in {
            "REFUSED",
            "UNCERTAIN",
            "STALE",
            "ACTION_FAILED",
            "READBACK_FAILED",
            "CANCELLED",
            "TIMED_OUT",
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
