"""Safety-critical external-write human-gate regression coverage."""

from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

dispatch = importlib.import_module("dispatch")


def _request(**kwargs: object) -> dispatch.ExternalWriteRequest:
    return dispatch.ExternalWriteRequest(**kwargs)


@pytest.mark.parametrize("operation", dispatch.FORBIDDEN_EXTERNAL_WRITES)
def test_every_external_write_without_matching_authorization_is_rejected(
    operation: str,
) -> None:
    result = dispatch.authorize_external_write(
        _request(
            operation=operation,
            target="service:target",
            payload_ref="exact-payload",
            run_id="run-1",
            source_identity="source-1",
        ),
        authorization=None,
    )
    assert result.status == "NEEDS_HUMAN"
    assert result.allowed is False


def test_nonmatching_or_expired_authorization_fails_closed() -> None:
    base = {
        "id": "auth-1",
        "operation": "push",
        "target": "service:target",
        "payload_ref": "wrong-ref",
        "approver": "approver-1",
        "issued_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2026-01-01T01:00:00+00:00",
        "scope": "release",
        "run_id": "run-1",
        "source_identity": "source-1",
    }
    result = dispatch.authorize_external_write(
        _request(
            operation="push",
            target="service:target",
            payload_ref="exact-ref",
            run_id="run-1",
            source_identity="source-1",
        ),
        authorization=base,
    )
    assert result.status == "NEEDS_HUMAN"


def test_caller_supplied_human_authorization_never_grants_an_external_write() -> None:
    """DV-1: untrusted input must not be mistaken for a human decision."""
    now = datetime.now(UTC)
    fabricated = {
        "id": "self-asserted",
        "operation": "push",
        "target": "github:owner/repo",
        "payload_ref": "refs/heads/main",
        "approver": "approver-1",
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "scope": "release",
        "run_id": "run-1",
        "source_identity": "source-1",
    }

    result = dispatch.authorize_external_write(
        _request(
            operation="push",
            target="github:owner/repo",
            payload_ref="refs/heads/main",
            run_id="run-1",
            source_identity="source-1",
            now=now,
        ),
        authorization=fabricated,
    )

    assert result.allowed is False
    assert result.status == "NEEDS_HUMAN"


def test_verified_out_of_band_record_is_payload_bound_revocable_and_single_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DV-1: only a private-key-backed record can authorize the exact one write."""
    root = tmp_path / "trusted-authority"
    root.mkdir()
    key_path = root / "verification.key"
    key_path.write_bytes(b"human-controlled-key")
    key_path.chmod(0o600)
    monkeypatch.setenv("AIH_RELEASE_PREP_AUTHORIZATION_STORE", str(root))
    monkeypatch.setenv("AIH_RELEASE_PREP_AUTHORIZATION_KEY_FILE", str(key_path))
    store = dispatch.TrustedAuthorizationStore(root, key_path.read_bytes())
    now = datetime.now(UTC)
    record = {
        "id": "auth-1",
        "operation": "push",
        "target": "service:target",
        "payload_digest": dispatch.sha256("exact-payload"),
        "approver": "approver-1",
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "scope": "release",
        "run_id": "run-1",
        "source_identity": "source-1",
        "nonce": "nonce-1",
    }
    record["signature"] = store._signature(record)
    path = root / "records" / "auth-1.json"
    path.parent.mkdir()
    path.write_text(json.dumps(record), encoding="utf-8")

    first = dispatch.authorize_external_write(
        _request(
            operation="push",
            target="service:target",
            payload_ref="exact-payload",
            run_id="run-1",
            source_identity="source-1",
            authorization_id="auth-1",
            now=now,
        )
    )
    second = dispatch.authorize_external_write(
        _request(
            operation="push",
            target="service:target",
            payload_ref="exact-payload",
            run_id="run-1",
            source_identity="source-1",
            authorization_id="auth-1",
            now=now,
        )
    )
    revoked = {**record, "id": "auth-2", "nonce": "nonce-2"}
    revoked["signature"] = store._signature(revoked)
    (root / "records" / "auth-2.json").write_text(json.dumps(revoked), encoding="utf-8")
    (root / "revoked").mkdir()
    (root / "revoked" / "auth-2").write_text("revoked", encoding="utf-8")
    revoked_result = dispatch.authorize_external_write(
        _request(
            operation="push",
            target="service:target",
            payload_ref="exact-payload",
            run_id="run-1",
            source_identity="source-1",
            authorization_id="auth-2",
            now=now,
        )
    )

    assert first.allowed is True
    assert second.allowed is False
    assert revoked_result.allowed is False
