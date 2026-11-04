from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import audit_receipt as receipt


DIGEST = "sha256:" + "1" * 64


def event(kind: str, round_number: int, **payload: object) -> dict:
    return {
        "schema_version": 1,
        "kind": kind,
        "round": round_number,
        "ts": "2026-09-13T00:00:00Z",
        "target": {"path": "target.md", "digest": DIGEST},
        "stage": "doc-audit",
        "spec_epoch": 0,
        "writer": "audit_loop.py",
        **payload,
    }


def start() -> dict:
    return event(
        "loop-start",
        0,
        budget={
            "tier": "standard",
            "audit_rounds": 4,
            "fix_passes": 3,
            "fingerprint_recurrence": 2,
            "stagnation_rounds": 3,
        },
        tier_source="embedded",
        carrier={"digest": DIGEST, "tier": 3},
    )


def finding(
    *,
    key: str = "round1:F-1",
    severity: str = "major",
    disposition: str = "open-blocking",
    fingerprint: str = "fp:one",
) -> dict:
    return {
        "finding_key": key,
        "id": key.split(":")[-1],
        "submitted_severity": severity,
        "severity": severity,
        "fingerprint": fingerprint,
        "cluster": {
            "spec_version": None,
            "invariant_id": None,
            "subsystem": "driver",
            "violation_kind": "incorrect-behavior",
        },
        "reachable_behavior": "The reachable path is incorrect.",
        "disposition": disposition,
    }


def audit(  # noqa: PLR0913 - event fixtures expose the independent fold inputs
    round_number: int,
    findings: list[dict],
    *,
    kind: str = "full",
    counts: dict[str, int] | None = None,
    justifications: dict[str, str] | None = None,
) -> dict:
    return event(
        "audit",
        round_number,
        scope={
            "kind": kind,
            "backlog": [],
            "diff_since": None,
            "diff_summary": {"changed": False},
        },
        findings=findings,
        new_by_severity=counts or {"critical": 0, "major": 0, "minor": 0},
        major_nonblocking_justifications=justifications or {},
        source_event_id=f"round-{round_number}-audit",
    )


def alignment_payload(*, verdict: str = "ALIGNED", artifact: str = "1" * 64) -> dict:
    evidence = [{"kind": "citation", "value": "ask:A001", "source": "record"}]
    return {
        "schema_version": 1,
        "run_id": "run",
        "stage": "implement",
        "transition": "doc-audit->implement",
        "founding_ask_ref_sha256": "2" * 64,
        "subject": {
            "artifact": {"state": "present", "ref": "doc", "sha256": artifact},
            "next_brief": {"state": "present", "sha256": "3" * 64},
        },
        "reviewer": {
            "family": "codex",
            "model": "model",
            "effort": "high",
            "persona": "audit",
        },
        "requirements": [
            {
                "requirement_id": "R1",
                "source_entry": "A001",
                "disposition": "SATISFIED",
                "applicability": "DIRECT",
                "rationale": "covered",
                "evidence": evidence,
                "authority_ref": None,
            }
        ],
        "scope_changes": [],
        "verdict": verdict,
        "confidence": "high",
        "blocking": verdict != "ALIGNED",
        "evidence": evidence,
    }


def test_closed_finding_validation_rejects_extra_field() -> None:
    item = finding()
    item["member_digest"] = DIGEST
    with pytest.raises(receipt.ReceiptError, match="legacy.*member_digest"):
        receipt.validate_event(audit(1, [item]))


def test_cluster_identifiers_allow_commit_qualified_spec_versions() -> None:
    item = finding()
    item["cluster"]["spec_version"] = "job-feedback-taxonomy-curation-design@27a0571"
    item["cluster"]["invariant_id"] = "receipt/schema-v1"
    receipt.validate_event(audit(1, [item]))

    item["cluster"]["spec_version"] = "not a valid identifier"
    with pytest.raises(
        receipt.ReceiptError, match="spec_version must be a valid identifier"
    ):
        receipt.validate_event(audit(1, [item]))


def test_append_adds_one_complete_json_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.receipt.jsonl"
    receipt.append(path, start())
    before = path.read_bytes()
    receipt.append(path, audit(1, []))
    after = path.read_bytes()
    assert after.startswith(before)
    assert after.count(b"\n") == before.count(b"\n") + 1
    assert json.loads(after.splitlines()[-1])["kind"] == "audit"


def test_load_rejects_out_of_sequence_round(tmp_path: Path) -> None:
    path = tmp_path / "audit.receipt.jsonl"
    path.write_bytes(receipt.canonical(start()) + b"\n")
    bad = audit(2, [])
    path.write_bytes(path.read_bytes() + receipt.canonical(bad) + b"\n")
    with pytest.raises(receipt.ReceiptError, match="out-of-sequence round"):
        receipt.load(path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("held_item", "/absolute/item.md"),
        ("held_item", "../item.md"),
        ("held_item", ""),
        ("held_item_blob", "not-a-blob"),
        ("chosen", "(b)"),
        ("answered_at", "2026-02-30T00:00:00Z"),
        ("answered_at", None),
        ("register_digest", "sha256:" + "2" * 64),
        ("extra", "unexpected"),
    ],
)
def test_spec_freeze_ratification_summary_is_validated(field, value) -> None:
    summary = {
        "held_item": "docs/held/session/item.md",
        "held_item_blob": "1" * 40,
        "chosen": "(a)",
        "answered_at": "2026-10-29T00:00:00Z",
        "register_digest": DIGEST,
    }
    freeze = event(
        "spec-freeze",
        1,
        register={"path": "target.md.invariants.yaml", "digest": DIGEST},
        human_ratification=summary,
    )
    receipt.validate_event(freeze)
    summary[field] = value
    with pytest.raises(receipt.ReceiptError, match="human_ratification"):
        receipt.validate_event(freeze)


def test_spec_freeze_epoch_cannot_be_reused() -> None:
    first_terminal = event("terminal", 1, terminal="halted-recurrence", reason="repeat")
    first_freeze = event(
        "spec-freeze",
        1,
        spec_epoch=1,
        register={"path": "target.md.invariants.yaml", "digest": DIGEST},
    )
    second_audit = audit(2, [])
    second_audit["spec_epoch"] = 1
    second_terminal = event(
        "terminal", 2, terminal="halted-recurrence", reason="repeat"
    )
    second_terminal["spec_epoch"] = 1
    reused_freeze = event(
        "spec-freeze",
        2,
        spec_epoch=1,
        register={"path": "target.md.invariants.yaml", "digest": DIGEST},
    )
    with pytest.raises(receipt.ReceiptError, match="non-monotonic spec_epoch"):
        receipt.validate_receipt_authority(
            [
                start(),
                audit(1, []),
                first_terminal,
                first_freeze,
                second_audit,
                second_terminal,
                reused_freeze,
            ]
        )


def test_doc_review_resume_receipt_requires_current_halted_backlog() -> None:
    halted = event(
        "terminal", 1, terminal="halted-recurrence", reason="finding repeated"
    )
    halt = event(
        "halt",
        1,
        halt="fingerprint",
        finding_keys=["round1:F-1"],
        reason="finding repeated",
    )
    resume = event(
        "doc-review-resume",
        1,
        finding_keys=["round1:F-1"],
        reviewer="Example User",
        rationale="Reviewed against this document revision.",
        reviewed_digest=DIGEST,
    )

    receipt.validate_receipt_authority(
        [start(), audit(1, [finding()]), halt, halted, resume]
    )
    assert (
        receipt.fold([start(), audit(1, [finding()]), halt, halted, resume]).terminal
        is None
    )


@pytest.mark.parametrize(
    "legacy",
    [
        "event_type",
        "generation",
        "member_digest",
        "member_digests",
        "lane",
        "capability_plan",
        "alignment",
    ],
)
def test_load_rejects_legacy_fields(tmp_path: Path, legacy: str) -> None:
    path = tmp_path / "audit.receipt.jsonl"
    bad = start()
    bad[legacy] = "old"
    path.write_bytes(receipt.canonical(bad) + b"\n")
    with pytest.raises(receipt.ReceiptError, match=rf"legacy.*{legacy}"):
        receipt.load(path)


def test_fold_uses_latest_finding_disposition_across_target_digests() -> None:
    first = audit(1, [finding()])
    second = audit(
        2,
        [finding(disposition="fixed")],
        kind="verify",
    )
    second["target"]["digest"] = "sha256:" + "9" * 64
    state = receipt.fold([start(), first, second])
    assert state.backlog == ()


def test_ask_drift_is_an_ordinary_blocking_finding() -> None:
    drift = finding()
    drift["cluster"]["violation_kind"] = "ask-drift:narrowed"
    drift["reachable_behavior"] = "An authoritative ask entry was narrowed."
    verify = audit(2, [], kind="verify")
    state = receipt.fold([start(), audit(1, [drift]), verify])
    assert state.backlog == ("round1:F-1",)
    assert not state.converged


def test_fold_converges_without_promotion_before_boundary_finalization() -> None:
    verify = audit(2, [], kind="verify")
    converged = event(
        "terminal", 2, terminal="audit-converged", reason="content is clean"
    )
    events = [start(), audit(1, []), verify, converged]
    state = receipt.fold(events)
    assert state.converged
    assert not state.promotion_ready


def test_append_many_leaves_prior_receipt_loadable_if_replace_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "receipt.jsonl"
    initial = start()
    receipt.append(path, initial)

    def interrupted_replace(_: object, __: object) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(receipt.os, "replace", interrupted_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        receipt.append_many(path, [audit(1, [])])
    assert receipt.load(path) == [initial]


def test_fixed_major_verify_requires_no_nonblocking_justification() -> None:
    item = finding(disposition="fixed")
    latest = audit(
        2,
        [item],
        kind="verify",
        counts={"critical": 0, "major": 1, "minor": 0},
    )
    state = receipt.fold([start(), audit(1, [finding()]), latest])
    assert state.backlog == ()
    assert state.stable
    assert state.converged


def test_major_verify_requires_nonblocking_justification() -> None:
    item = finding(disposition="deferred")
    latest = audit(
        2,
        [item],
        kind="verify",
        counts={"critical": 0, "major": 1, "minor": 0},
    )
    assert not receipt.fold([start(), audit(1, []), latest]).stable
    latest["major_nonblocking_justifications"] = {
        item["finding_key"]: "The specified behavior is already unambiguous."
    }
    assert receipt.fold([start(), audit(1, []), latest]).stable


def override(
    *,
    expires_at: str,
    family: str = "human",
    target_digest: str = DIGEST,
) -> dict:
    return event(
        "override",
        1,
        payload={
            "finding_keys": ["round1:F-1"],
            "human_actor": "Example User",
            "residual_risk_owner": "Example User",
            "justification": "Accepted for this bounded release.",
            "expires_at": expires_at,
            "target_digest": target_digest,
            "critical_compensating_control": None,
        },
        actor={"family": family, "name": "Example User"},
    )


def test_valid_override_closes_finding_inside_fold() -> None:
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    state = receipt.fold([start(), audit(1, [finding()]), override(expires_at=future)])
    assert state.backlog == ()


@pytest.mark.parametrize(
    ("expires_at", "family"),
    [
        ((datetime.now(UTC) - timedelta(days=1)).isoformat(), "human"),
        ((datetime.now(UTC) + timedelta(days=1)).isoformat(), "codex"),
    ],
    # Pin the ids. The values embed datetime.now(), so pytest derives the node id from
    # a microsecond-precision timestamp -- which differs in every process. Under xdist
    # each worker collected its own, and the whole run aborted with "Different tests
    # were collected between gw0 and gw1", making -n unusable for the entire repo. The
    # values stay relative to now, which is what the test is actually about.
    ids=["expired-human", "unexpired-codex"],
)
def test_invalid_override_does_not_close_finding(expires_at: str, family: str) -> None:
    state = receipt.fold(
        [start(), audit(1, [finding()]), override(expires_at=expires_at, family=family)]
    )
    assert state.backlog == ("round1:F-1",)


def test_action_class_envelope_rejects_pure_bookkeeping() -> None:
    bad = event(
        "fix",
        1,
        applied=["round1:F-1"],
        action={"class": "administrative", "invariant_consumer": ""},
    )
    with pytest.raises(receipt.ReceiptError, match="invariant_consumer"):
        receipt.validate_event(bad)


def test_schema_generated_example_validates_without_modification() -> None:
    payload = json.loads(receipt.render_audit_payload_example())
    receipt._validate_payload("audit", payload)


def test_render_preserves_one_external_ledger_and_status_only(
    tmp_path: Path,
) -> None:
    audit_doc = tmp_path / "audit.md"
    audit_doc.write_text(
        "## Run Ledger\n\nledger-version: 1\n\n"
        "| Field | Value |\n|---|---|\n| Owner | session |\n\n"
        f"{receipt.REGION_START}\nold\n{receipt.REGION_END}\n",
        encoding="utf-8",
    )
    path = receipt.receipt_path(audit_doc)
    receipt.append(path, start())
    receipt.append(path, audit(1, []))
    receipt.render(audit_doc)
    rendered = audit_doc.read_text(encoding="utf-8")
    region = rendered.split(receipt.REGION_START, 1)[1].split(receipt.REGION_END, 1)[0]
    assert rendered.count("Run Ledger") == 1
    assert "Status Summary" in region
    assert "Run Ledger" not in region
