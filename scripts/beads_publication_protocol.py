#!/usr/bin/env python3
"""Fail-closed wire-format support for Beads publication proposal v1."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# Shared with the migration caller: transfer settings must leave room for cleanup.
PUBLISH_TIMEOUT_SECONDS = 900
FILES = frozenset(
    {"manifest.json", "base.issues.jsonl", "candidate.issues.jsonl", "delta.json"}
)
HEX = re.compile(r"^[0-9a-f]{64}$")
MARKERS = ("<<<<<<<", "=======", ">>>>>>>", "|||||||")


class ProtocolError(ValueError):
    """A proposal violated the versioned, credential-free wire contract."""


@dataclass(frozen=True)
class Proposal:
    manifest: dict[str, Any]
    base: tuple[dict[str, Any], ...]
    candidate: tuple[dict[str, Any], ...]
    digest: str


def load_config(path: Path = ROOT / "config/beads_publication.toml") -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProtocolError(f"non-finite JSON number {value!r}")


def canonical_json(value: Any) -> bytes:
    """Serialize the v1 canonical JSON byte domain."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_jsonl(
    raw: bytes, label: str, schema: dict[str, Any]
) -> tuple[dict[str, Any], ...]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"{label}: invalid UTF-8") from exc
    records: list[dict[str, Any]] = []
    issue_ids: set[str] = set()
    for number, line in enumerate(
        text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1
    ):
        if not line:
            continue
        # A marker embedded in a legitimate issue description is data, not a Git
        # conflict.  Only an unresolved marker line is a malformed JSONL record.
        if line.startswith(MARKERS):
            raise ProtocolError(f"{label} line {number}: conflict marker")
        try:
            record = json.loads(
                line, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant
            )
        except (json.JSONDecodeError, ProtocolError) as exc:
            raise ProtocolError(f"{label} line {number}: invalid JSON") from exc
        if not isinstance(record, dict):
            raise ProtocolError(f"{label} line {number}: record is not an object")
        _validate_record(record, schema, label, number)
        if record["id"] in issue_ids:
            raise ProtocolError(f"{label}: duplicate issue id {record['id']!r}")
        issue_ids.add(record["id"])
        records.append(record)
    return tuple(records)


def _validate_record(
    record: dict[str, Any], schema: dict[str, Any], label: str, number: int
) -> None:
    _validate_issue_scalars(record, schema, label, number)
    _validate_auxiliaries(record, schema, label, number)


def _validate_issue_scalars(
    record: dict[str, Any], schema: dict[str, Any], label: str, number: int
) -> None:
    unknown = set(record).difference(schema["issue_fields"])
    if unknown:
        raise ProtocolError(
            f"{label} line {number}: unregistered issue field {min(unknown)!r}"
        )
    if (
        record.get("_type") != "issue"
        or not isinstance(record.get("id"), str)
        or not record["id"]
        or not isinstance(record.get("title"), str)
        or not isinstance(record.get("priority"), int)
        or not isinstance(record.get("created_at"), str)
        or not isinstance(record.get("updated_at"), str)
    ):
        raise ProtocolError(f"{label} line {number}: required _type/id")
    if (
        record.get("status") is not None
        and record["status"] not in schema["status_values"]
    ):
        raise ProtocolError(f"{label} line {number}: status enum")
    if (
        record.get("issue_type") is not None
        and record["issue_type"] not in schema["issue_type_values"]
    ):
        raise ProtocolError(f"{label} line {number}: issue_type enum")


def _validate_auxiliaries(
    record: dict[str, Any], schema: dict[str, Any], label: str, number: int
) -> None:
    validators = (
        ("labels", None, lambda item: item),
        (
            "comments",
            schema["comment_fields"],
            lambda item: tuple(
                item.get(k) for k in ("issue_id", "author", "text", "created_at")
            ),
        ),
        (
            "dependencies",
            schema["dependency_fields"],
            lambda item: tuple(
                item.get(k) for k in ("issue_id", "depends_on_id", "type")
            ),
        ),
    )
    for field, allowed, identity in validators:
        values = record.get(field, [])
        if not isinstance(values, list):
            raise ProtocolError(f"{label} line {number}: {field} is not an array")
        seen: set[Any] = set()
        for value in values:
            if field == "labels":
                valid = isinstance(value, str)
            else:
                valid = isinstance(value, dict) and not set(value).difference(
                    allowed or []
                )
            if not valid:
                raise ProtocolError(f"{label} line {number}: invalid {field} member")
            if (
                field == "dependencies"
                and value.get("type") not in schema["dependency_type_values"]
            ):
                raise ProtocolError(f"{label} line {number}: dependency type enum")
            key = identity(value)
            if key in seen:
                raise ProtocolError(
                    f"{label} line {number}: duplicate {field} identity"
                )
            seen.add(key)


def semantic_digest(raw: bytes, label: str, schema: dict[str, Any]) -> str:
    """Hash semantic records independent of JSON keys, line endings and auxiliary order."""
    normalized: list[dict[str, Any]] = []
    for record in _parse_jsonl(raw, label, schema):
        item = dict(record)
        for local_field in ("heartbeat_at", "lease_expires_at", "lease_granted_node"):
            item.pop(local_field, None)
        item["labels"] = sorted(
            item.get("labels", []), key=lambda value: value.encode("utf-8")
        )
        item["comments"] = sorted(
            item.get("comments", []),
            key=lambda value: canonical_json(
                {
                    key: value.get(key)
                    for key in ("issue_id", "author", "text", "created_at")
                }
            ),
        )
        item["dependencies"] = sorted(
            item.get("dependencies", []),
            key=lambda value: canonical_json(
                {key: value.get(key) for key in ("issue_id", "depends_on_id", "type")}
            ),
        )
        normalized.append(item)
    normalized.sort(key=lambda item: item["id"].encode("utf-8"))
    return sha256(b"\n".join(canonical_json(item) for item in normalized) + b"\n")


def proposal_digest(manifest: dict[str, Any]) -> str:
    copy = dict(manifest)
    if "proposal_commit" in copy or "proposal_digest" in copy:
        raise ProtocolError(
            "manifest must not contain proposal_commit or proposal_digest"
        )
    fields = (
        "base_payload_sha256",
        "candidate_payload_sha256",
        "base_semantic_sha256",
        "candidate_semantic_sha256",
    )
    values = [copy.get(field) for field in fields]
    if not all(isinstance(value, str) and HEX.fullmatch(value) for value in values):
        raise ProtocolError("manifest digest field")
    return sha256(
        canonical_json(copy)
        + b"\n"
        + b"\n".join(value.encode("ascii") for value in values)
    )


def _validate_manifest(
    manifest: dict[str, Any], raw: dict[str, bytes], config: dict[str, Any]
) -> None:
    required = {
        "schema_version",
        "request_id",
        "repository",
        "database",
        "node_id",
        "base_canonical_oid",
        "semantic_schema_version",
        "base_payload_sha256",
        "candidate_payload_sha256",
        "base_semantic_sha256",
        "candidate_semantic_sha256",
        "bd_version",
        "dolt_version",
        "created_at",
        "deletion_requests",
        "counts",
    }
    if set(manifest) != required:
        raise ProtocolError("manifest field set")
    _validate_manifest_identity(manifest, config)
    _validate_manifest_payloads(manifest, raw, config)


def _validate_manifest_identity(
    manifest: dict[str, Any], config: dict[str, Any]
) -> None:
    if (
        manifest["schema_version"] != config["protocol"]["schema_version"]
        or manifest["semantic_schema_version"]
        != config["protocol"]["semantic_schema_version"]
    ):
        raise ProtocolError("manifest schema version")
    repository = config["repositories"].get(manifest["repository"])
    if not repository or manifest["database"] != repository["database"]:
        raise ProtocolError("repository mapping")
    if (
        manifest["bd_version"] != config["exporter"]["bd_version"]
        or manifest["dolt_version"] != config["exporter"]["dolt_version"]
    ):
        raise ProtocolError("exporter version")
    if not all(
        isinstance(manifest[field], str) and manifest[field]
        for field in ("request_id", "node_id", "base_canonical_oid", "created_at")
    ):
        raise ProtocolError("manifest identifier")


def _validate_manifest_payloads(
    manifest: dict[str, Any], raw: dict[str, bytes], config: dict[str, Any]
) -> None:
    for filename, field in (
        ("base.issues.jsonl", "base_payload_sha256"),
        ("candidate.issues.jsonl", "candidate_payload_sha256"),
    ):
        if len(raw[filename]) > config["protocol"]["max_payload_bytes"]:
            raise ProtocolError("payload limit")
        if sha256(raw[filename]) != manifest[field]:
            raise ProtocolError(field)
    if not isinstance(manifest["counts"], dict) or set(manifest["counts"]) != {
        "issues",
        "comments",
        "dependencies",
        "labels",
    }:
        raise ProtocolError("counts")
    if not isinstance(manifest["deletion_requests"], list):
        raise ProtocolError("deletion_requests")


def load_bundle(bundle: Path, config: dict[str, Any] | None = None) -> Proposal:
    """Validate one exact bundle directory without reading a credential."""
    config = config or load_config()
    paths = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file()
    }
    if paths != FILES:
        raise ProtocolError("bundle path set")
    raw = {name: (bundle / name).read_bytes() for name in FILES}
    try:
        manifest = json.loads(
            raw["manifest.json"],
            object_pairs_hook=_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ProtocolError) as exc:
        raise ProtocolError("manifest: invalid JSON") from exc
    if not isinstance(manifest, dict):
        raise ProtocolError("manifest is not an object")
    _validate_manifest(manifest, raw, config)
    base = _parse_jsonl(raw["base.issues.jsonl"], "base", config["schema"])
    candidate = _parse_jsonl(
        raw["candidate.issues.jsonl"], "candidate", config["schema"]
    )
    deleted = {item["id"] for item in base}.difference(item["id"] for item in candidate)
    requests = manifest["deletion_requests"]
    request_ids = [item.get("issue_id") for item in requests if isinstance(item, dict)]
    if (
        any(not isinstance(item, dict) for item in requests)
        or set(request_ids) != deleted
        or len(request_ids) != len(set(request_ids))
        or any(
            set(item) != {"issue_id", "reason", "requester", "backup_digest"}
            or not isinstance(item["reason"], str)
            or not item["reason"]
            or item["requester"] != manifest["node_id"]
            or not isinstance(item["backup_digest"], str)
            or not HEX.fullmatch(item["backup_digest"])
            for item in requests
            if isinstance(item, dict)
        )
    ):
        raise ProtocolError("deletion_requests")
    observed_counts = {
        "issues": len(candidate),
        "comments": sum(len(item.get("comments", [])) for item in candidate),
        "dependencies": sum(len(item.get("dependencies", [])) for item in candidate),
        "labels": sum(len(item.get("labels", [])) for item in candidate),
    }
    if manifest["counts"] != observed_counts:
        raise ProtocolError("counts")
    for filename, label, field in (
        ("base.issues.jsonl", "base", "base_semantic_sha256"),
        ("candidate.issues.jsonl", "candidate", "candidate_semantic_sha256"),
    ):
        if semantic_digest(raw[filename], label, config["schema"]) != manifest[field]:
            raise ProtocolError(f"{label}: semantic digest")
    return Proposal(
        manifest=manifest,
        base=base,
        candidate=candidate,
        digest=proposal_digest(manifest),
    )
