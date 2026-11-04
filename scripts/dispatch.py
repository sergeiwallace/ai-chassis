"""Fail-closed Phase-2 placement, audit, remediation, and human-write gates."""

from __future__ import annotations

import hmac
import importlib.util
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evidence import ImmutableRecordError, canonical_json, immutable_json, sha256

FORBIDDEN_EXTERNAL_WRITES = (
    "push",
    "pull-request-create",
    "pull-request-update",
    "issue-create",
    "comment-create",
    "status-create",
    "check-create",
    "upload",
    "publish",
    "api-mutation",
    "tag",
    "credential-change",
    "credential-rotation",
    "credential-revocation",
    "history-rewrite",
    "force-push",
    "ownership-change",
    "permission-change",
    "merge",
    "waiver-acceptance",
    "policy-lowering",
    "public-release-wording-approval",
)
_AUTH_FIELDS = {
    "id",
    "operation",
    "target",
    "payload_digest",
    "approver",
    "issued_at",
    "expires_at",
    "scope",
    "run_id",
    "source_identity",
    "nonce",
    "signature",
}
_UNSIGNED_AUTH_FIELDS = _AUTH_FIELDS - {"signature"}
_MANIFEST_FIELDS = {
    "schema_version",
    "run_id",
    "lane",
    "source_identity",
    "policy_digest",
    "evidence",
    "artifacts",
    "manifest_digest",
}


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    status: str
    reason: str


@dataclass(frozen=True)
class AuthorizationRequest:
    authorization_id: str
    operation: str
    target: str
    payload_digest: str
    run_id: str
    source_identity: str
    now: datetime


@dataclass(frozen=True)
class ExternalWriteRequest:
    operation: str
    target: str
    payload_ref: str
    run_id: str
    source_identity: str
    authorization_id: str | None = None
    payload_digest: str | None = None
    now: datetime | None = None


@dataclass(frozen=True)
class LaneManifestRequest:
    repository: Path
    run_id: str
    lane: str
    source_identity: Mapping[str, str]
    policy_digest: str
    evidence_ids: Sequence[str]
    artifacts: Sequence[Mapping[str, str]]


@dataclass(frozen=True)
class ManifestValidationRequest:
    repository: Path
    run_id: str
    lane: str
    manifest: Path
    source_identity: str
    policy_digest: str | None


@dataclass(frozen=True)
class StructuredAuditRequest:
    repository: Path
    run_id: str
    source_identity: str
    manifest: Path
    ledger_path: Path
    audit_document: str
    commit_payload_ref: str
    route: Mapping[str, str]
    intent: Mapping[str, Any]
    authorization_id: str | None = None
    policy_digest: str | None = None


class TrustedAuthorizationStore:
    """Verify out-of-band signed records and consume each nonce exactly once."""

    def __init__(self, root: Path, verification_key: bytes) -> None:
        if not verification_key:
            raise ValueError("a trusted authorization verification key is required")
        self.root = root.resolve()
        self.verification_key = verification_key

    def _record_path(self, authorization_id: str) -> Path:
        if not authorization_id or Path(authorization_id).name != authorization_id:
            raise ValueError("authorization id is invalid")
        return self.root / "records" / f"{authorization_id}.json"

    def _signature(self, record: Mapping[str, Any]) -> str:
        unsigned = {key: record[key] for key in sorted(_UNSIGNED_AUTH_FIELDS)}
        return hmac.new(
            self.verification_key, canonical_json(unsigned).encode("utf-8"), "sha256"
        ).hexdigest()

    def _load_record(self, authorization_id: str) -> object | None:
        try:
            path = self._record_path(authorization_id)
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _schema_error(self, record: object) -> GateResult | None:
        if not isinstance(record, dict) or set(record) != _AUTH_FIELDS:
            return GateResult(
                False, "NEEDS_HUMAN", "trusted authorization record is malformed"
            )
        if not all(
            isinstance(record[key], str) and record[key] for key in _AUTH_FIELDS
        ):
            return GateResult(
                False, "NEEDS_HUMAN", "trusted authorization record has invalid fields"
            )
        return None

    def read_verified(
        self,
        *,
        authorization_id: str,
        now: datetime,
    ) -> tuple[dict[str, str] | None, GateResult]:
        record = self._load_record(authorization_id)
        if record is None:
            return None, GateResult(
                False, "NEEDS_HUMAN", "trusted authorization record is unavailable"
            )
        schema_error = self._schema_error(record)
        if schema_error:
            return None, schema_error
        assert isinstance(record, dict)
        if not hmac.compare_digest(record["signature"], self._signature(record)):
            return None, GateResult(
                False, "NEEDS_HUMAN", "trusted authorization signature is invalid"
            )
        issued_at = _timestamp(record["issued_at"])
        expires_at = _timestamp(record["expires_at"])
        if (
            issued_at is None
            or expires_at is None
            or issued_at > now
            or expires_at <= now
        ):
            return None, GateResult(
                False, "NEEDS_HUMAN", "trusted authorization is expired"
            )
        if (self.root / "revoked" / record["id"]).exists():
            return None, GateResult(
                False, "NEEDS_HUMAN", "trusted authorization is revoked"
            )
        return record, GateResult(True, "AUTHORIZED", "trusted authorization verified")

    def consume(
        self, *, authorization_id: str, record: Mapping[str, str], now: datetime
    ) -> GateResult:
        consumed = self.root / "consumed" / record["nonce"]
        try:
            immutable_json(
                consumed,
                {
                    "authorization_id": authorization_id,
                    "nonce": record["nonce"],
                    "consumed_at": now.isoformat(),
                },
            )
        except (ImmutableRecordError, OSError):
            return GateResult(
                False, "NEEDS_HUMAN", "trusted authorization was already consumed"
            )
        return GateResult(True, "AUTHORIZED", "trusted human authorization verified")

    def verify_and_consume(self, request: AuthorizationRequest) -> GateResult:
        record, result = self.read_verified(
            authorization_id=request.authorization_id, now=request.now
        )
        if record is None:
            return result
        expected = {
            "id": request.authorization_id,
            "operation": request.operation,
            "target": request.target,
            "payload_digest": request.payload_digest,
            "run_id": request.run_id,
            "source_identity": request.source_identity,
        }
        if any(record[key] != value for key, value in expected.items()):
            return GateResult(
                False, "NEEDS_HUMAN", "trusted authorization does not match operation"
            )
        return self.consume(
            authorization_id=request.authorization_id, record=record, now=request.now
        )


def configured_authorization_store() -> TrustedAuthorizationStore | None:
    """Load the controller-owned MAC key from a private, out-of-band key file."""

    root_value = os.environ.get("AIH_RELEASE_PREP_AUTHORIZATION_STORE")
    key_value = os.environ.get("AIH_RELEASE_PREP_AUTHORIZATION_KEY_FILE")
    if not root_value or not key_value:
        return None
    root = Path(root_value)
    key_path = Path(key_value)
    try:
        mode = stat.S_IMODE(key_path.stat().st_mode)
        if not root.is_absolute() or not key_path.is_absolute() or mode & 0o077:
            return None
        key = key_path.read_bytes()
    except OSError:
        return None
    return TrustedAuthorizationStore(root, key)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def authorize_external_write(
    request: ExternalWriteRequest | None = None,
    *,
    authorization: Mapping[str, Any] | None = None,
    authorization_store: TrustedAuthorizationStore | None = None,
    **request_fields: object,
) -> GateResult:
    """Accept only a trusted-store record, never caller-supplied authorization data."""

    request = request or ExternalWriteRequest(**request_fields)

    if authorization is not None:
        return GateResult(
            False, "NEEDS_HUMAN", "caller-supplied authorization is untrusted"
        )
    if authorization_store is not None:
        return GateResult(
            False, "NEEDS_HUMAN", "caller-injected authorization store is untrusted"
        )
    configured_store = configured_authorization_store()
    if request.authorization_id is None or configured_store is None:
        return GateResult(
            False, "NEEDS_HUMAN", "trusted authorization record is required"
        )
    current = request.now or datetime.now(UTC)
    return configured_store.verify_and_consume(
        AuthorizationRequest(
            authorization_id=request.authorization_id,
            operation=request.operation,
            target=request.target,
            payload_digest=request.payload_digest or sha256(request.payload_ref),
            run_id=request.run_id,
            source_identity=request.source_identity,
            now=current,
        )
    )


def create_lane_manifest(
    request: LaneManifestRequest | None = None, **request_fields: object
) -> Path:
    """Publish the immutable regular-file audit input before audit dispatch."""

    request = request or LaneManifestRequest(**request_fields)

    if not request.lane or not request.run_id or not request.policy_digest:
        raise ValueError("run, lane, and policy identity are required")
    path = (
        request.repository
        / ".release-prep"
        / "runs"
        / request.run_id
        / "manifests"
        / f"{request.lane}.json"
    )
    evidence: list[dict[str, str]] = []
    for evidence_id in sorted(request.evidence_ids):
        evidence_path = (
            request.repository
            / ".release-prep"
            / "runs"
            / request.run_id
            / "evidence"
            / f"{evidence_id}.json"
        )
        if not evidence_path.is_file():
            raise ValueError(f"evidence record is missing: {evidence_id}")
        evidence.append(
            {"id": evidence_id, "sha256": sha256(evidence_path.read_bytes())}
        )
    normalized_artifacts = [dict(artifact) for artifact in request.artifacts]
    payload = {
        "schema_version": 1,
        "run_id": request.run_id,
        "lane": request.lane,
        "source_identity": dict(request.source_identity),
        "policy_digest": request.policy_digest,
        "evidence": evidence,
        "artifacts": normalized_artifacts,
    }
    immutable_json(
        path, {**payload, "manifest_digest": sha256(canonical_json(payload))}
    )
    return path


def _validate_evidence_bindings(
    repository: Path, run_id: str, evidence: object
) -> None:
    if not isinstance(evidence, list):
        raise ValueError("immutable manifest identity does not match this dispatch")
    seen_evidence: set[str] = set()
    for entry in evidence:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"id", "sha256"}
            or not all(isinstance(entry[key], str) and entry[key] for key in entry)
            or entry["id"] in seen_evidence
        ):
            raise ValueError("immutable manifest evidence bindings are malformed")
        seen_evidence.add(entry["id"])
        evidence_path = (
            repository
            / ".release-prep"
            / "runs"
            / run_id
            / "evidence"
            / f"{entry['id']}.json"
        )
        if not evidence_path.is_file() or not hmac.compare_digest(
            entry["sha256"], sha256(evidence_path.read_bytes())
        ):
            raise ValueError("immutable manifest evidence digest does not match")


def _validate_artifact_bindings(repository: Path, artifacts: object) -> None:
    if not isinstance(artifacts, list):
        raise ValueError("immutable manifest identity does not match this dispatch")
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {
            "path",
            "filename",
            "sha256",
        }:
            raise ValueError("immutable manifest artifact bindings are malformed")
        path_value = artifact["path"]
        if not isinstance(path_value, str) or not isinstance(artifact["sha256"], str):
            raise TypeError("immutable manifest artifact binding is malformed")
        artifact_path = (repository / path_value).resolve()
        try:
            artifact_path.relative_to(repository.resolve())
        except ValueError as exc:
            raise ValueError("immutable manifest artifact escapes repository") from exc
        if (
            not artifact_path.is_file()
            or artifact["filename"] != artifact_path.name
            or not hmac.compare_digest(
                artifact["sha256"], sha256(artifact_path.read_bytes())
            )
        ):
            raise ValueError("immutable manifest artifact digest does not match")


def _validate_manifest(
    request: ManifestValidationRequest,
) -> tuple[dict[str, Any], str]:
    expected_path = (
        request.repository
        / ".release-prep"
        / "runs"
        / request.run_id
        / "manifests"
        / f"{request.lane}.json"
    )
    if (
        request.manifest.resolve() != expected_path.resolve()
        or not request.manifest.is_file()
    ):
        raise ValueError(
            "immutable manifest is missing or not canonical for this run/lane"
        )
    try:
        value = json.loads(request.manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("immutable manifest is not parseable") from exc
    if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS:
        raise ValueError("immutable manifest has an invalid closed schema")
    payload = {key: value[key] for key in _MANIFEST_FIELDS - {"manifest_digest"}}
    digest = value["manifest_digest"]
    if not isinstance(digest, str) or not hmac.compare_digest(
        digest, sha256(canonical_json(payload))
    ):
        raise ValueError("immutable manifest digest does not match its contents")
    if (
        value["schema_version"] != 1
        or value["run_id"] != request.run_id
        or value["lane"] != request.lane
        or not isinstance(value["source_identity"], dict)
        or value["source_identity"].get("digest") != request.source_identity
        or not isinstance(value["policy_digest"], str)
        or (
            request.policy_digest is not None
            and value["policy_digest"] != request.policy_digest
        )
    ):
        raise ValueError("immutable manifest identity does not match this dispatch")
    _validate_evidence_bindings(request.repository, request.run_id, value["evidence"])
    _validate_artifact_bindings(request.repository, value["artifacts"])
    return value, digest


def _flow_resolver():
    path = (
        Path(__file__).parent.parent
        / "skills"
        / "auto-flow"
        / "scripts"
        / "flow_resolve.py"
    )
    spec = importlib.util.spec_from_file_location("release_prep_flow_resolve", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("auto-flow resolver is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def dispatch_structured_audit(
    request: StructuredAuditRequest | None = None,
    *,
    authorization: Mapping[str, Any] | None = None,
    authorization_store: TrustedAuthorizationStore | None = None,
    **request_fields: object,
) -> dict[str, Any]:
    """Validate all gates, then return only the canonical delegated audit invocation.

    This function never launches `/audit-auto`; the attending orchestrator may do
    so only after receiving this validated descriptor.
    """

    request = request or StructuredAuditRequest(**request_fields)
    try:
        target = request.manifest.relative_to(request.repository).as_posix()
        ledger = request.ledger_path.relative_to(request.repository).as_posix()
    except ValueError as exc:
        raise ValueError("manifest and ledger must be under repository root") from exc
    try:
        _manifest, manifest_digest = _validate_manifest(
            ManifestValidationRequest(
                repository=request.repository,
                run_id=request.run_id,
                lane=request.manifest.stem,
                manifest=request.manifest,
                source_identity=request.source_identity,
                policy_digest=request.policy_digest,
            )
        )
    except (TypeError, ValueError) as exc:
        return {"status": "NEEDS_HUMAN", "reason": str(exc)}
    gate = authorize_external_write(
        ExternalWriteRequest(
            operation="audit-document-commit",
            target=request.audit_document,
            payload_ref=request.commit_payload_ref,
            run_id=request.run_id,
            source_identity=request.source_identity,
            authorization_id=request.authorization_id,
            payload_digest=sha256(
                canonical_json(
                    {
                        "commit_payload_ref": request.commit_payload_ref,
                        "manifest_digest": manifest_digest,
                    }
                )
            ),
        ),
        authorization=authorization,
        authorization_store=authorization_store,
    )
    if not gate.allowed:
        return {"status": "NEEDS_HUMAN", "reason": gate.reason}
    body = {
        "authority": {
            "run_id": request.run_id,
            "ledger_path": ledger,
            "contract": "release-prep/audit-lane",
            "decision_classes": ["commit-without-push"],
            "push": False,
        },
        "doc": {"kind": "design", "mode": "automated"},
        "lineage_root": ".release-prep",
        "integration_ref": "HEAD",
        "intent": dict(request.intent),
        "stages": {
            "doc-audit": {"family": "codex", "tier": "audit", "effort": "medium"}
        },
        "audit_auto": {
            "target": target,
            "no_push": True,
            "adn_authority": None,
            "manifest_digest": manifest_digest,
        },
    }
    try:
        resolver = _flow_resolver()
    except (ImportError, OSError, RuntimeError) as exc:
        return {
            "status": "NEEDS_HUMAN",
            "reason": f"no-push resolver unavailable: {exc}",
        }
    try:
        envelope = resolver.validate_envelope_body(body, repo_root=request.repository)
    except resolver.ResolutionError as exc:
        return {"status": "NEEDS_HUMAN", "reason": f"no-push envelope invalid: {exc}"}
    if dict(request.route) != {"family": "codex", "tier": "audit", "effort": "medium"}:
        return {
            "status": "NEEDS_HUMAN",
            "reason": "audit route is not canonical delegated Codex",
        }
    return {
        "status": "READY_TO_DISPATCH",
        "route": {"assembled_by": "/delegate", "routed_by": "/codex"},
        "audit_auto": envelope["audit_auto"],
        "authority": envelope["authority"],
        "no_push": True,
    }


def execution_placement(execution_mode: str, stage: str) -> str:
    if execution_mode not in {"delegated", "inline"}:
        raise ValueError("execution mode must be delegated or inline")
    if stage == "remediate":
        return "isolated-auto-flow"
    if execution_mode == "inline" and stage == "discover":
        return "inline-read-only"
    return "delegated"


def remediation_handoff(
    *, approved: bool, preconditions_pass: bool, route: Mapping[str, str]
) -> dict[str, Any]:
    """Return the only permitted remediation route after its two hard gates."""

    if not approved or not preconditions_pass:
        return {
            "status": "NEEDS_HUMAN",
            "reason": "remediation approval or build preconditions missing",
        }
    if dict(route) not in (
        {"family": "codex", "tier": "implement", "effort": "medium"},
        {"family": "codex", "tier": "implement", "effort": "high"},
        {"family": "codex", "tier": "implement", "effort": "xhigh"},
    ):
        return {
            "status": "NEEDS_HUMAN",
            "reason": "remediation route violates auto-flow matrix",
        }
    return {
        "status": "READY_TO_DISPATCH",
        "command": "/auto-flow --preset build",
        "assembled_by": "/delegate",
        "routed_by": "/codex",
        "isolated_worktree": True,
    }
