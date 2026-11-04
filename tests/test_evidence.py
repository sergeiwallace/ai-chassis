"""Phase-2 deterministic evidence and source-binding coverage."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# Assembled from this scheme variable, never written literally: the canary must LOOK like a
# real header for the redactor to catch it, while a literal one in tracked source is exactly
# what tests/test_no_inline_credentials.py forbids repository-wide. That guard keeps its
# allowlist deliberately narrow on purpose, so the fix belongs here, not in the allowlist.
_BEARER_SCHEME = "Bearer"


def _secret_canaries() -> dict[str, str]:
    """Build scanner canaries at runtime so this test is safe to export."""
    github_token = "".join(
        (
            "gh",
            "p_",
            "0123456789abcdefgh",
            "ijklmnopqrstuvwxyz",
            "ABCD",
        )
    )
    token_url = "".join(
        (
            "https://",
            "user",
            ":password@example.invalid/download?access_",
            "token=canary",
        )
    )
    bearer_token = " ".join((_BEARER_SCHEME, "bearer-canary-token"))
    return {
        "github_token": github_token,
        "token_url": token_url,
        "bearer_token": bearer_token,
        "raw_argv": "raw-argv-canary",
    }


SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

evidence = importlib.import_module("evidence")


def test_required_command_persists_sanitized_evidence_before_claim_linkage(
    tmp_path: Path,
) -> None:
    store = evidence.EvidenceStore(tmp_path, "run-1")
    record = store.record_command(
        command=["scanner", "--json"],
        tool_version="scanner 1.0",
        source_identity={"commit": "abc", "digest": "source-1"},
        claim_ids=["source.secrets"],
        exit_status=0,
        output="token=super-secret\nresult=clean",
    )

    persisted = (tmp_path / "evidence" / f"{record['id']}.json").read_text()
    assert "super-secret" not in persisted
    assert record["claim_ids"] == ["source.secrets"]
    assert record["verdict"] == "PASS"
    with pytest.raises(evidence.ImmutableRecordError):
        store.record_command(
            command=["scanner", "--json"],
            tool_version="scanner 1.0",
            source_identity={"commit": "abc", "digest": "source-1"},
            claim_ids=["source.secrets"],
            exit_status=0,
            output="different",
            evidence_id=record["id"],
        )


def test_unavailable_required_evidence_never_synthesizes_pass(tmp_path: Path) -> None:
    store = evidence.EvidenceStore(tmp_path, "run-1")

    inconclusive = store.record_unavailable(
        requirement="required scanner",
        claim_ids=["source.secrets"],
        policy="inconclusive",
    )
    blocked = store.record_unavailable(
        requirement="required build", claim_ids=["artifact.build"], policy="block"
    )

    assert inconclusive["verdict"] == "INCONCLUSIVE"
    assert blocked["verdict"] == "BLOCK"


def test_artifact_binding_and_identity_change_invalidate_dependent_claims(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "dist" / "candidate.whl"
    artifact.parent.mkdir()
    artifact.write_bytes(b"candidate-v1")
    identity = evidence.SourceIdentity(
        commit="abc",
        source_digest="source-1",
        ref="refs/heads/main",
        policy_digest="policy-1",
        tool_capabilities={"build": "1"},
        submodules={},
    )
    binding = evidence.bind_artifacts(identity, [artifact])
    assert binding["artifacts"][0]["sha256"]

    changed = evidence.SourceIdentity(
        commit="abc",
        source_digest="dirty-source-2",
        ref="refs/heads/main",
        policy_digest="policy-1",
        tool_capabilities={"build": "1"},
        submodules={},
    )
    assert evidence.invalidated_claims(
        identity, changed, {"source": ["a"], "artifact": ["b"]}
    ) == {"source": ["a"], "artifact": ["b"]}


def test_artifact_checks_bind_clean_install_import_and_cli_smoke_to_exact_bytes(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "candidate.whl"
    artifact.write_bytes(b"candidate-v1")
    identity = evidence.SourceIdentity(
        commit="abc",
        source_digest="source-1",
        ref="refs/heads/main",
        policy_digest="policy-1",
        tool_capabilities={"build": "1"},
        submodules={},
    )
    store = evidence.EvidenceStore(tmp_path / "run", "run-1")
    records = [
        store.record_artifact_check(
            identity=identity, artifact=artifact, check=check, status="PASS"
        )
        for check in ("clean-install", "import", "cli-smoke")
    ]

    assert all(
        evidence.artifact_check_is_current(record, identity) for record in records
    )
    artifact.write_bytes(b"candidate-v2")
    assert not any(
        evidence.artifact_check_is_current(record, identity) for record in records
    )


def test_event_records_are_immutable_for_resume_replay(tmp_path: Path) -> None:
    store = evidence.EvidenceStore(tmp_path, "run-1")
    event = store.record_event(event_type="audit-dispatch", payload={"lane": "source"})

    assert event["id"].startswith("event-")
    with pytest.raises(evidence.ImmutableRecordError):
        store.record_event(
            event_type="audit-dispatch",
            payload={"lane": "source"},
            event_id=event["id"],
        )


@pytest.mark.parametrize(
    "field,first,second",
    [
        ("operation", "push", "publish"),
        ("service_resource", "github:repo", "pypi:project"),
        ("payload_ref", "main", "v1.0.0"),
        ("authorization_record_id", "auth-1", "auth-2"),
        ("remediation_digest", "patch-1", "patch-2"),
    ],
)
def test_idempotency_key_changes_for_each_action_defining_parameter(
    field: str, first: str, second: str
) -> None:
    base = {
        "run_id": "run-1",
        "stage": "source",
        "identity_digest": "identity",
        "evidence_ids": ["ev-1"],
        "action_kind": "external-write",
        "operation": "push",
        "service_resource": "github:repo",
        "payload_ref": "main",
        "authorization_record_id": "auth-1",
        "remediation_digest": "patch-1",
    }
    other = dict(base)
    base[field] = first
    other[field] = second
    assert evidence.idempotency_key(**base) != evidence.idempotency_key(**other)


def test_per_run_lock_rejects_concurrent_resume(tmp_path: Path) -> None:
    lock = evidence.RunLock(tmp_path / "run.lock")
    with lock:
        pytest.raises(
            evidence.RunLockedError,
            evidence.RunLock(tmp_path / "run.lock").__enter__,
        )


def test_secret_canaries_never_reach_serialized_output_or_command_records(
    tmp_path: Path,
) -> None:
    """DV-3: structured output, headers, URLs, argv, and exceptions are redacted."""
    values = _secret_canaries()
    canaries = set(values.values())
    store = evidence.EvidenceStore(tmp_path, "run-1")
    record = store.record_command(
        command=["scanner", "--token", values["raw_argv"]],
        tool_version="scanner 1.0",
        source_identity={"commit": "abc", "digest": "source-1"},
        claim_ids=["source.secrets"],
        exit_status=1,
        output=(
            '{"access_token":"'
            + values["github_token"]
            + '"}\nAuthorization: '
            + values["bearer_token"]
            + "\n"
            + values["token_url"]
        ),
    )

    persisted = (tmp_path / "evidence" / f"{record['id']}.json").read_text()
    assert all(canary not in persisted for canary in canaries)
    assert "command" not in record
    assert record["command_identity"].startswith("sha256:")


@pytest.mark.parametrize("scheme", ["HTTPS", "Https", "hTTp"])
def test_given_a_mixed_case_url_scheme_when_sanitized_then_its_password_is_redacted(
    tmp_path: Path, scheme: str
) -> None:
    """ISSUE-REF review: the URL pattern matched only a lowercase scheme."""
    password = "FAKE" + "-pw-not-a-secret-0000"
    line = f"mirror {scheme}://fixture-user:{password}@example.invalid/r.git\n"

    sanitized = evidence.sanitize_text(line)
    assert password not in sanitized
    assert "example.invalid/r.git" in sanitized
    store = evidence.EvidenceStore(tmp_path, "run-1")
    record = store.record_command(
        command=["scanner"],
        tool_version="scanner 1.0",
        source_identity={"commit": "abc", "digest": "source-1"},
        claim_ids=["source.secrets"],
        exit_status=1,
        output=line,
    )
    persisted = (tmp_path / "evidence" / f"{record['id']}.json").read_text()
    assert password not in persisted
