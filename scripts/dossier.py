"""Canonical release-readiness dossier and Run Ledger writer."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class CanonicalDossierError(RuntimeError):
    """A completed run may have exactly one canonical dossier."""


class ClaimStatus(StrEnum):
    """The closed evidence status vocabulary used by readiness reduction."""

    PASS = "PASS"
    WARN = "WARN"
    BLOCK = "BLOCK"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


REQUIRED_CLAIMS = frozenset({"source", "docs", "quality", "artifact", "reconcile"})


@dataclass(frozen=True)
class RunLedgerAuthority:
    run_id: str
    contract: str
    decision_classes: Sequence[str]
    push: bool


def dossier_verdict(
    claim_statuses: Mapping[str, str] | Sequence[str],
    *,
    required_claims: Sequence[str] = tuple(sorted(REQUIRED_CLAIMS)),
) -> str:
    """Reduce only a complete, closed claim record; otherwise fail closed."""

    required = set(required_claims)
    if (
        not required
        or not isinstance(claim_statuses, Mapping)
        or set(claim_statuses) != required
        or not all(isinstance(claim, str) and claim for claim in claim_statuses)
        or not all(status in ClaimStatus for status in claim_statuses.values())
    ):
        return "INCONCLUSIVE"
    statuses = set(claim_statuses.values())
    if "BLOCK" in statuses:
        return "BLOCKED"
    if "NEEDS_HUMAN" in statuses:
        return "NEEDS_HUMAN"
    if "INCONCLUSIVE" in statuses:
        return "INCONCLUSIVE"
    if "WARN" in statuses:
        return "WARN"
    return "READY_FOR_HUMAN_GO_NO_GO"


def create_dossier(
    *,
    path: Path,
    run_id: str,
    source_identity: Mapping[str, str],
    authority: RunLedgerAuthority | None,
) -> Path:
    """Create the sole dossier before an audit lane can validate its authority."""

    path.parent.mkdir(parents=True, exist_ok=True)
    authority_row = "| _none_ | _none_ | [] | false |"
    if authority is not None:
        classes = ", ".join(f'"{item}"' for item in authority.decision_classes)
        authority_row = (
            f"| {authority.run_id} | {authority.contract} | [{classes}] | "
            f"{str(authority.push).lower()} |"
        )
    identity_lines = "\n".join(
        f"- `{key}`: `{value}`" for key, value in sorted(source_identity.items())
    )
    content = (
        "# Release readiness dossier\n\n"
        f"- Run ID: `{run_id}`\n"
        "- Verdict: `INCONCLUSIVE`\n\n"
        "## Source identity\n\n"
        f"{identity_lines}\n\n"
        "## Run Ledger\n"
        "- `ledger-version`: 1\n\n"
        "| Run ID | Contract | Decision classes | Push |\n"
        "| --- | --- | --- | --- |\n"
        f"{authority_row}\n\n"
        "## Evidence\n\n"
        "Evidence IDs, candidate digests, findings, residual risk, and human go/no-go "
        "checklist are appended by the controller. This dossier is not publication authority.\n"
    )
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise CanonicalDossierError(
            f"canonical dossier already exists: {path}"
        ) from exc
    return path
