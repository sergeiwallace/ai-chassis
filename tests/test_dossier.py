"""Phase-2 canonical dossier coverage."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

dossier = importlib.import_module("dossier")


def test_dossier_is_canonical_and_contains_authority_ledger(tmp_path: Path) -> None:
    path = tmp_path / "docs" / "audits" / "release-readiness-1.2.3.md"
    created = dossier.create_dossier(
        path=path,
        run_id="run-1",
        source_identity={"commit": "abc", "digest": "source-1"},
        authority=dossier.RunLedgerAuthority(
            run_id="run-1",
            contract="release-prep/audit-lane",
            decision_classes=["commit-without-push"],
            push=False,
        ),
    )

    assert created == path
    content = path.read_text(encoding="utf-8")
    assert "## Run Ledger" in content
    assert "commit-without-push" in content
    complete = {
        "source": "PASS",
        "docs": "PASS",
        "quality": "PASS",
        "artifact": "NOT_APPLICABLE",
        "reconcile": "PASS",
    }
    assert dossier.dossier_verdict(complete) == "READY_FOR_HUMAN_GO_NO_GO"
    assert (
        dossier.dossier_verdict({**complete, "quality": "INCONCLUSIVE"})
        == "INCONCLUSIVE"
    )
    assert dossier.dossier_verdict({**complete, "docs": "WARN"}) == "WARN"
    with pytest.raises(dossier.CanonicalDossierError):
        dossier.create_dossier(
            path=path,
            run_id="run-2",
            source_identity={"commit": "other"},
            authority=None,
        )


@pytest.mark.parametrize("statuses", [[], ["UNKNOWN"], ["WARN"], ["PASS", "PASS"]])
def test_dossier_verdict_fails_closed_for_empty_unknown_or_incomplete_claims(
    statuses: list[str],
) -> None:
    """F-1: missing/corrupt claim evidence cannot become release-ready."""
    assert dossier.dossier_verdict(statuses) != "READY_FOR_HUMAN_GO_NO_GO"
