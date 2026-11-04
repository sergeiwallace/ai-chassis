#!/usr/bin/env python3
"""Probe task-panel ordering, allocation, and delegation preconditions.

The Claude Code task tools are available to the calling agent rather than to a
Python process.  This probe therefore evaluates observations captured by the
driver (render order, created id, and spawned-agent capability report) and
fails closed when they contradict the contracts required by task reordering.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path


def _plan_digest(value: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise argparse.ArgumentTypeError(
            "plan digest must be a lowercase SHA-256 hex value"
        )
    return value


def _ids(value: str) -> list[int]:
    try:
        ids = [int(item) for item in value.split(",") if item]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "ids must be comma-separated integers"
        ) from exc
    if not ids:
        raise argparse.ArgumentTypeError("at least one id is required")
    if len(ids) != len(set(ids)):
        raise argparse.ArgumentTypeError("ids must be unique")
    return ids


def _print_contract(name: str, confirmed: bool) -> int:
    state = "confirmed" if confirmed else "violated"
    print(f"{name}-contract: {state}")
    return 0 if confirmed else 1


def probe_order(args: argparse.Namespace) -> int:
    observed = args.observed_order
    mtime = args.mtime_order
    numeric = sorted(observed)
    lexicographic = sorted(observed, key=str)
    uniform_digit_count = len({len(str(task_id)) for task_id in observed}) == 1
    if set(mtime) != set(observed) or len(mtime) != len(observed):
        print("ordering-contract: violated (mtime order is not a row permutation)")
        return 1

    print(f"observed-render-order: {','.join(map(str, observed))}")
    print(f"pinned-client-version: {args.client_version}")
    confirmed = (
        observed == numeric
        and (uniform_digit_count or observed != lexicographic)
        and observed != mtime
    )
    return _print_contract("ordering", confirmed)


def probe_allocation(args: argparse.Namespace) -> int:
    maximum = max(args.existing_ids)
    threshold = max(maximum, args.highwatermark)
    print(f"maximum-existing-id: {maximum}")
    print(f"highwatermark: {args.highwatermark}")
    print(f"created-id: {args.new_id}")
    return _print_contract("allocation", args.new_id > threshold)


def probe_delegation(args: argparse.Namespace) -> int:
    """Superseded by `delegation-receipt`; self-attested evidence is never confirmed.

    This subcommand used to compare caller-supplied ``--agent-task-list-id``/
    ``--agent-tools`` strings against the environment and report a caller-authored
    combination as ``confirmed`` -- exactly the self-attestation weakness
    `delegation-receipt` and `curate.py --evidence-agent subagent` were made
    unconditionally fail-closed against. Kept only so existing invocations get a
    named refusal instead of a missing-subcommand error.
    """
    if args.evidence_agent != "subagent":
        print("delegation-contract: not-applicable")
        return 0
    print(
        "delegation-contract: unavailable (no non-caller-editable Task-tool "
        "provenance channel exists; use delegation-receipt, which refuses "
        "unconditionally for the same reason)"
    )
    return 1


def probe_receipt(args: argparse.Namespace) -> int:
    """Persist the two measured ordering facts bound to one namespace/client build."""
    if probe_order(args) or probe_allocation(args):
        return 1
    receipt = {
        "kind": "task-panel-ordering",
        "namespace": args.namespace,
        "plan_digest": args.plan_digest,
        "client_version": args.client_version,
        "confirmed": True,
        "ordering_confirmed": True,
        "allocation_confirmed": True,
        "observed_order": args.observed_order,
        "mtime_order": args.mtime_order,
        "existing_ids": args.existing_ids,
        "highwatermark": args.highwatermark,
        "new_id": args.new_id,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(f"probe-receipt: {args.out}")
    return 0


def delegation_receipt(_args: argparse.Namespace) -> int:
    """Refuse self-attested delegated evidence until trusted provenance exists."""
    print(
        "delegation-contract: unavailable (no non-caller-editable Task-tool provenance channel exists)"
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="probe", required=True)

    ordering = commands.add_parser("ordering")
    ordering.add_argument("--observed-order", required=True, type=_ids)
    ordering.add_argument("--mtime-order", required=True, type=_ids)
    ordering.add_argument("--client-version", required=True)
    ordering.set_defaults(run=probe_order)

    allocation = commands.add_parser("allocation")
    allocation.add_argument("--existing-ids", required=True, type=_ids)
    allocation.add_argument("--highwatermark", required=True, type=int)
    allocation.add_argument("--new-id", required=True, type=int)
    allocation.set_defaults(run=probe_allocation)

    receipt = commands.add_parser("receipt")
    receipt.add_argument("--namespace", required=True)
    receipt.add_argument("--plan-digest", required=True, type=_plan_digest)
    receipt.add_argument("--observed-order", required=True, type=_ids)
    receipt.add_argument("--mtime-order", required=True, type=_ids)
    receipt.add_argument("--client-version", required=True)
    receipt.add_argument("--existing-ids", required=True, type=_ids)
    receipt.add_argument("--highwatermark", required=True, type=int)
    receipt.add_argument("--new-id", required=True, type=int)
    receipt.add_argument("--out", required=True, type=Path)
    receipt.set_defaults(run=probe_receipt)

    delegation = commands.add_parser("delegation")
    delegation.add_argument(
        "--evidence-agent", choices=("session", "subagent"), required=True
    )
    delegation.add_argument("--agent-task-list-id")
    delegation.add_argument("--agent-tools", default="")
    delegation.set_defaults(run=probe_delegation)
    delegation_receipt_parser = commands.add_parser("delegation-receipt")
    delegation_receipt_parser.add_argument("--namespace", required=True)
    delegation_receipt_parser.add_argument(
        "--plan-digest", required=True, type=_plan_digest
    )
    delegation_receipt_parser.add_argument("--nonce", required=True)
    delegation_receipt_parser.add_argument("--out", required=True, type=Path)
    delegation_receipt_parser.set_defaults(run=delegation_receipt)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
