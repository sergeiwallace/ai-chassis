"""Bounded semantic closure packets; absence is a refusal, never a guessed close."""

from __future__ import annotations

from typing import Any

REQUIRED_PACKET = {
    "packet_id",
    "finding_keys",
    "invariant_ids",
    "counterexample_ids",
    "producers",
    "transforms",
    "consumers",
    "authority_lifecycle_surfaces",
    "artifact_family_members",
    "states_polarities",
    "support_frontier",
    "invalidated_evidence_ids",
    "unchanged_cells",
    "out_of_scope_cells",
    "challenge_evidence_id",
}


class ClosureError(ValueError):
    pass


def validate_packet(packet: dict[str, Any]) -> None:
    missing = REQUIRED_PACKET - set(packet)
    if missing:
        raise ClosureError("closure packet missing " + ", ".join(sorted(missing)))
    for key in REQUIRED_PACKET - {"packet_id", "challenge_evidence_id"}:
        if not isinstance(packet[key], list):
            raise ClosureError(f"closure packet {key} must be a list")
    if any(
        cell in {"unknown", "plausible-outside-support"}
        for cell in packet["unchanged_cells"] + packet["out_of_scope_cells"]
    ):
        raise ClosureError("unknown/plausible-outside-support cells cannot be closed")


def challenge(
    packet: dict[str, Any], *, original_safe: bool, adjacent_fails: bool
) -> bool:
    validate_packet(packet)
    if not original_safe or not adjacent_fails:
        raise ClosureError(
            "closure challenge must preserve counterexample and reject adjacent wrong behavior"
        )
    return True
