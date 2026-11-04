#!/usr/bin/env python3
"""Build the Tier-4 packet; absence of a ratchet receipt is a hard error."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def push_diff(root: Path, ratchet: dict[str, object]) -> str:
    """Read the exact range the ratchet receipt attests to."""
    from_ref = ratchet.get("from_ref")
    to_ref = ratchet.get("to_ref")
    if not isinstance(from_ref, str) or not isinstance(to_ref, str):
        raise TypeError("ratchet receipt has no push range")
    result = subprocess.run(
        ["git", "-C", str(root), "diff", "--binary", f"{from_ref}..{to_ref}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(f"cannot read ratchet push range: {result.stderr.strip()}")
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--acceptance-criteria", required=True, type=Path)
    parser.add_argument("--ratchet", required=True, type=Path)
    parser.add_argument("--patch-budget", required=True, type=Path)
    parser.add_argument("--test-result", required=True, type=Path)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        ratchet = json.loads(args.ratchet.read_text())
        if not isinstance(ratchet.get("findings"), list):
            raise TypeError("ratchet receipt has no findings list")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(f"cannot build review packet: {error}")
    try:
        diff = push_diff(args.repository, ratchet)
    except ValueError as error:
        parser.error(f"cannot build review packet: {error}")
    print(
        json.dumps(
            {
                "schema_version": 1,
                "task_id": args.task_id,
                "acceptance_criteria": args.acceptance_criteria.read_text(),
                "diff": diff,
                "analyzer_findings": ratchet["findings"],
                "ratchet": ratchet,
                "patch_budget": json.loads(args.patch_budget.read_text()),
                "test_result": args.test_result.read_text(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
