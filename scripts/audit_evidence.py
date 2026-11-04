"""Capability-routed oracle receipt helpers."""

from __future__ import annotations

from typing import Any


class EvidenceError(ValueError):
    pass


def route(required: set[str], executors: dict[str, set[str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for capability in required:
        candidates = [name for name, caps in executors.items() if capability in caps]
        if not candidates:
            raise EvidenceError(f"unsupported capability {capability}")
        result[capability] = min(candidates)
    return result


def validate(
    receipt: dict[str, Any], *, candidate_digest: str, policy_version: str
) -> None:
    required = {
        "claim_id",
        "property",
        "production_boundary",
        "capability",
        "command",
        "environment",
        "executor",
        "positive_baseline",
        "negative_control",
        "candidate_digest",
        "member_digests",
        "outcome",
    }
    if required - set(receipt):
        raise EvidenceError(
            "evidence receipt missing " + ", ".join(sorted(required - set(receipt)))
        )
    if (
        receipt["candidate_digest"] != candidate_digest
        or receipt.get("policy_version", policy_version) != policy_version
    ):
        raise EvidenceError("stale or wrong-policy evidence")
    if not receipt["positive_baseline"] or not receipt["negative_control"]:
        raise EvidenceError("non-discriminating evidence")


def transport_attempt(semantic_event_id: str, attempt: int) -> dict[str, Any]:
    return {"semantic_event_id": semantic_event_id, "attempt": attempt}
