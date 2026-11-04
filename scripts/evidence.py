"""Immutable, deterministic records for `/release-prep` Phase 2.

This module deliberately contains no repository-command runner.  Phase 0 owns
the sandbox that executes commands; callers hand this module only the completed,
sanitized-to-be result so it can make the evidence durable before any agent is
allowed to interpret it.
"""

from __future__ import annotations

try:
    import fcntl
except ModuleNotFoundError:  # Windows: no fcntl. RunLock.__enter__ refuses explicitly.
    fcntl = None  # type: ignore[assignment]
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

SCHEMA_VERSION = 1
ARTIFACT_CHECKS = frozenset(
    {"build", "metadata", "clean-install", "import", "cli-smoke"}
)
_SENSITIVE_NAME = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|authorization|credential)"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b[A-Z0-9_-]*(?:password|passwd|secret|token|api[_-]?key|authorization|credential)"
    r"[A-Z0-9_-]*\s*[=:]\s*)[^\r\n]*"
)
_HEADER_SECRET = re.compile(
    r"(?i)^(\s*(?:authorization|proxy-authorization|x-api-key|x-auth-token)\s*:\s*).*$"
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_KNOWN_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|"
    r"glpat-[A-Za-z0-9_-]{12,}|xox[baprs]-[A-Za-z0-9-]{12,})\b"
)


class ImmutableRecordError(RuntimeError):
    """An immutable evidence, event, or manifest record was already present."""


class RunLockedError(RuntimeError):
    """Another process owns this run's exclusive resume/write lock."""


class SanitizationError(ValueError):
    """A value cannot be made safe enough to persist as evidence."""


@dataclass(frozen=True)
class IdempotencyInputs:
    run_id: str
    stage: str
    identity_digest: str
    evidence_ids: Sequence[str]
    action_kind: str
    operation: str | None
    service_resource: str | None
    payload_ref: str | None
    authorization_record_id: str | None
    remediation_digest: str | None


@dataclass(frozen=True)
class CommandEvidence:
    command: Sequence[str]
    tool_version: str
    source_identity: Mapping[str, str]
    claim_ids: Sequence[str]
    exit_status: int
    output: str
    evidence_id: str | None = None


def canonical_json(value: Any) -> str:
    """Return the single stable encoding used for identity and idempotency."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(value: bytes | str) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _is_sensitive_name(value: object) -> bool:
    return isinstance(value, str) and bool(_SENSITIVE_NAME.search(value))


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _is_sensitive_name(key) else _sanitize_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise SanitizationError("unsupported structured evidence value")


def _sanitize_url(match: re.Match[str]) -> str:
    raw_url = match.group(0)
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise SanitizationError("malformed URL cannot be safely persisted") from exc
    if not parsed.hostname:
        return raw_url
    username = parsed.username
    netloc = parsed.hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    if username is not None:
        netloc = f"{username}:[REDACTED]@{netloc}"
    query = urlencode(
        [
            (key, "[REDACTED]" if _is_sensitive_name(key) else value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ],
        doseq=True,
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


# A URL scheme is case-insensitive (RFC 3986 3.1), so `HTTPS://user:pass@host` is one too.
_URL = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)


def _sanitize_text(value: str) -> str:
    if "\x00" in value:
        raise SanitizationError("NUL-containing output cannot be safely persisted")
    sanitized = _URL.sub(_sanitize_url, value)
    lines: list[str] = []
    for line in sanitized.splitlines(keepends=True):
        newline = "\n" if line.endswith("\n") else ""
        body = line[:-1] if newline else line
        header = _HEADER_SECRET.match(body)
        if header:
            lines.append(f"{header.group(1)}[REDACTED]{newline}")
            continue
        body = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}[REDACTED]", body)
        body = _BEARER_TOKEN.sub("Bearer [REDACTED]", body)
        body = _KNOWN_TOKEN.sub("[REDACTED]", body)
        lines.append(body + newline)
    return "".join(lines)


def sanitize_text(value: str) -> str:
    """Redact credentials from free text: URL passwords, auth headers, assignments, tokens.

    Unlike :func:`sanitize_output`, a line that looks like JSON is treated as text, so
    tool output such as ``[test-gate] ...`` does not fail closed for not parsing.
    Raises :class:`SanitizationError` on NUL or a malformed URL.
    """
    return _sanitize_text(value)


def sanitize_output(output: str) -> str:
    """Sanitize text/JSON evidence; malformed structured payloads fail closed."""

    if not isinstance(output, str):
        raise SanitizationError("output must be text")
    sanitized_lines: list[str] = []
    for line in output.splitlines(keepends=True):
        newline = "\n" if line.endswith("\n") else ""
        body = line[:-1] if newline else line
        stripped = body.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise SanitizationError("malformed structured output") from exc
            sanitized_lines.append(canonical_json(_sanitize_json(parsed)) + newline)
        else:
            sanitized_lines.append(_sanitize_text(body) + newline)
    return "".join(sanitized_lines)


def sanitize_command(command: Sequence[str]) -> dict[str, Any]:
    """Persist only a stable command identity and a redacted argument rendering."""

    if not command or not all(isinstance(part, str) and part for part in command):
        raise SanitizationError("command must be a non-empty string sequence")
    rendered: list[str] = []
    redact_next = False
    for index, part in enumerate(command):
        if index == 0:
            rendered.append("<executable>")
            continue
        if redact_next:
            rendered.append("[REDACTED]")
            redact_next = False
            continue
        if part.startswith("--"):
            name, separator, _value = part.partition("=")
            if _is_sensitive_name(name):
                rendered.append(f"{name}=[REDACTED]" if separator else name)
                redact_next = not bool(separator)
            elif separator:
                rendered.append(f"{name}=<arg:{sha256(part):.12}>")
            else:
                rendered.append(name)
            continue
        rendered.append(f"<arg:{sha256(part):.12}>")
    return {
        "command_identity": f"sha256:{sha256(command[0])}",
        "command_rendering": rendered,
    }


@dataclass(frozen=True)
class SourceIdentity:
    """All values that can invalidate source-bound claims."""

    commit: str
    source_digest: str
    ref: str
    policy_digest: str
    tool_capabilities: Mapping[str, str]
    submodules: Mapping[str, str]
    artifact_digests: Mapping[str, str] = field(default_factory=dict)
    authorization_context: Mapping[str, str] = field(default_factory=dict)
    side_effects: Mapping[str, str] = field(default_factory=dict)

    def digest(self) -> str:
        return sha256(canonical_json(asdict(self)))


def artifact_digest(path: Path) -> str:
    return sha256(path.read_bytes())


def bind_artifacts(
    identity: SourceIdentity, artifacts: Sequence[Path]
) -> dict[str, Any]:
    """Bind candidate metadata and every smoke/check claim to exact artifact bytes."""

    records = []
    for artifact in sorted(artifacts, key=lambda item: str(item)):
        if not artifact.is_file():
            raise ValueError(f"artifact is not a regular file: {artifact}")
        records.append(
            {
                "path": str(artifact),
                "filename": artifact.name,
                "sha256": artifact_digest(artifact),
            }
        )
    return {"source_identity_digest": identity.digest(), "artifacts": records}


def invalidated_claims(
    previous: SourceIdentity,
    current: SourceIdentity,
    dependent_claims: Mapping[str, list[str]],
) -> dict[str, list[str]]:
    """Fail closed: any identity dimension change invalidates dependent claims."""

    return (
        {}
        if previous == current
        else {key: list(value) for key, value in dependent_claims.items()}
    )


def idempotency_key(inputs: IdempotencyInputs | None = None, **fields: object) -> str:
    """Include every action-defining input; inapplicable fields stay explicit null."""

    inputs = inputs or IdempotencyInputs(**fields)

    return sha256(
        canonical_json(
            {
                "run_id": inputs.run_id,
                "stage": inputs.stage,
                "identity_digest": inputs.identity_digest,
                "evidence_ids": sorted(inputs.evidence_ids),
                "action_kind": inputs.action_kind,
                "operation": inputs.operation,
                "service_resource": inputs.service_resource,
                "payload_ref": inputs.payload_ref,
                "authorization_record_id": inputs.authorization_record_id,
                "remediation_digest": inputs.remediation_digest,
            }
        )
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically publish mutable state on its final filesystem."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    """Create an immutable record exactly once, with durable file publication."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(canonical_json(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ImmutableRecordError(f"immutable record already exists: {path}") from exc
    _fsync_directory(path.parent)


class RunLock(AbstractContextManager["RunLock"]):
    """Exclusive non-blocking lock; concurrent resume refuses instead of duplicating work."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # No advisory locking on this platform means exclusivity cannot be PROVEN, and
        # this lock exists so a concurrent resume refuses instead of duplicating work.
        # Refusing is therefore the faithful behaviour: proceeding unlocked would turn a
        # loud "already locked" into two runs silently writing the same evidence.
        if fcntl is None:
            raise RunLockedError(
                f"cannot acquire {self.path}: advisory file locking is unavailable on "
                "this platform (no fcntl module), so exclusive access cannot be proven"
            )
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise RunLockedError(f"run is already locked: {self.path}") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


class EvidenceStore:
    """Append-only evidence/event storage under one already-locked run directory."""

    def __init__(self, run_dir: Path, run_id: str) -> None:
        self.run_dir = run_dir
        self.run_id = run_id

    def record_command(
        self, command: CommandEvidence | Sequence[str] | None = None, **fields: object
    ) -> dict[str, Any]:
        if not isinstance(command, CommandEvidence):
            command = CommandEvidence(command=command or (), **fields)
        if not command.command or not command.tool_version or not command.claim_ids:
            raise ValueError("command, tool version, and claim linkage are required")
        command_record = sanitize_command(command.command)
        sanitized_output = sanitize_output(command.output)
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": command.evidence_id or f"ev-{uuid4().hex}",
            "run_id": self.run_id,
            **command_record,
            "tool_version": sanitize_output(command.tool_version),
            "source_identity": dict(command.source_identity),
            "claim_ids": list(command.claim_ids),
            "exit_status": command.exit_status,
            "sanitized_output": sanitized_output,
            "verdict": "PASS" if command.exit_status == 0 else "BLOCK",
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        immutable_json(self.run_dir / "evidence" / f"{record['id']}.json", record)
        return record

    def record_unavailable(
        self, *, requirement: str, claim_ids: Sequence[str], policy: str
    ) -> dict[str, Any]:
        verdicts = {"inconclusive": "INCONCLUSIVE", "block": "BLOCK"}
        if policy not in verdicts or not requirement or not claim_ids:
            raise ValueError("unavailable evidence requires policy and linked claims")
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": f"ev-{uuid4().hex}",
            "run_id": self.run_id,
            "requirement": requirement,
            "claim_ids": list(claim_ids),
            "available": False,
            "verdict": verdicts[policy],
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        immutable_json(self.run_dir / "evidence" / f"{record['id']}.json", record)
        return record

    def record_artifact_check(
        self,
        *,
        identity: SourceIdentity,
        artifact: Path,
        check: str,
        status: str,
        evidence_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist each candidate verification against the artifact's exact digest."""

        if check not in ARTIFACT_CHECKS or status not in {
            "PASS",
            "BLOCK",
            "INCONCLUSIVE",
        }:
            raise ValueError("unsupported artifact check or status")
        if not artifact.is_file():
            raise ValueError(f"artifact is not a regular file: {artifact}")
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": evidence_id or f"ev-{uuid4().hex}",
            "run_id": self.run_id,
            "check": check,
            "status": status,
            "artifact": {"path": str(artifact), "sha256": artifact_digest(artifact)},
            "source_identity_digest": identity.digest(),
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        immutable_json(self.run_dir / "evidence" / f"{record['id']}.json", record)
        return record

    def record_event(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist immutable transition facts for interrupt/resume idempotency."""

        if not event_type:
            raise ValueError("event type is required")
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": event_id or f"event-{uuid4().hex}",
            "run_id": self.run_id,
            "type": event_type,
            "payload": dict(payload),
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        immutable_json(self.run_dir / "events" / f"{record['id']}.json", record)
        return record


def artifact_check_is_current(
    record: Mapping[str, Any], identity: SourceIdentity
) -> bool:
    """Return false when either reviewed bytes or any source identity field changed."""

    artifact = record.get("artifact")
    if not isinstance(artifact, Mapping) or not isinstance(artifact.get("path"), str):
        return False
    path = Path(artifact["path"])
    return (
        path.is_file()
        and artifact.get("sha256") == artifact_digest(path)
        and record.get("source_identity_digest") == identity.digest()
    )
