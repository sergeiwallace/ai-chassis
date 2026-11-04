"""Content-based configuration identity and deterministic input fingerprints."""

import os
import subprocess
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType


class IdentityValidationError(ValueError):
    """An identity input cannot be represented by the normative framing."""


@dataclass(frozen=True)
class ConsumedEntry:
    path: str
    type: str
    content: bytes | str


@dataclass(frozen=True)
class ConfigProvenance:
    vcs: str | None
    commit: str | None
    dirty: bool | None


@dataclass(frozen=True)
class ConsumedSnapshot:
    """Immutable identity entries and the exact dereferenced bytes rendered."""

    entries: tuple[ConsumedEntry, ...]
    render_bytes: Mapping[str, bytes]


def _normalized_bytes(value: str, label: str) -> bytes:
    normalized = unicodedata.normalize("NFC", value).encode("utf-8")
    if b"\0" in normalized or b"\n" in normalized:
        raise IdentityValidationError(
            f"{label} contains a framing delimiter: {value!r}"
        )
    return normalized


def config_identity(
    schema_version: int,
    entries: Iterable[ConsumedEntry],
    environment_inputs: dict[str, str] | None = None,
) -> str:
    """Compute AC-F-CONFIG-IDENTITY exactly over already-consumed entries."""
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version <= 0
    ):
        raise IdentityValidationError(
            "config schema version must be a positive integer"
        )
    materialized_entries = list(entries)
    if not 1 <= len(materialized_entries) <= 10_000:
        raise IdentityValidationError(
            f"config identity entry count must be 1-10000; got {len(materialized_entries)}"
        )
    env = environment_inputs or {}
    encoded_env = sorted(
        (
            _normalized_bytes(name, "environment input name"),
            _normalized_bytes(value, "environment input value"),
        )
        for name, value in env.items()
    )
    preamble = (
        b"ahci/1\0"
        + str(schema_version).encode("ascii")
        + b"\0"
        + b"".join(name + b"\0" + value + b"\0" for name, value in encoded_env)
        + b"\n"
    )
    if not 10 <= len(preamble) <= 65_536:
        raise IdentityValidationError(
            f"config identity preamble must be 10-65536 bytes; got {len(preamble)}"
        )
    rows: list[tuple[bytes, bytes]] = []
    for entry in materialized_entries:
        path = _normalized_bytes(entry.path.replace("\\", "/"), "entry path")
        if entry.type not in {"file", "sym"}:
            raise IdentityValidationError(
                f"invalid entry type for {entry.path!r}: {entry.type!r}"
            )
        content = (
            unicodedata.normalize("NFC", entry.content).encode("utf-8")
            if isinstance(entry.content, str)
            else entry.content
        )
        digest = sha256(content).hexdigest().encode("ascii")
        rows.append(
            (path, path + b"\0" + entry.type.encode("ascii") + b"\0" + digest + b"\n")
        )
    rows.sort(key=lambda row: row[0])
    if any(left[0] == right[0] for left, right in pairwise(rows)):
        raise IdentityValidationError(
            "two consumed entries share a normalized relative path"
        )
    return (
        "sha256:" + sha256(preamble + b"".join(row for _path, row in rows)).hexdigest()
    )


def consumed_entries(root: Path, paths: Iterable[Path]) -> list[ConsumedEntry]:
    """Read file/symlink entries safely relative to a config root."""
    return list(consumed_snapshot(root, paths).entries)


def consumed_snapshot(
    root: Path,
    paths: Iterable[Path],
    preloaded: Mapping[str, bytes] | None = None,
) -> ConsumedSnapshot:
    """Capture every identity entry and render byte string exactly once."""
    root = root.resolve(strict=True)
    entries: list[ConsumedEntry] = []
    render_bytes: dict[str, bytes] = {}
    supplied = preloaded or {}
    for candidate in paths:
        if candidate.is_absolute():
            raise IdentityValidationError(
                f"consumed entry escapes config root: {candidate}"
            )
        path = root / candidate
        try:
            lexical_path = Path(os.path.abspath(path))
            relative = lexical_path.relative_to(root)
        except ValueError as exc:
            raise IdentityValidationError(
                f"consumed entry escapes config root: {path}"
            ) from exc
        try:
            resolved_path = lexical_path.resolve(strict=True)
        except OSError as exc:
            raise IdentityValidationError(
                f"cannot read consumed entry: {lexical_path}"
            ) from exc
        try:
            resolved_path.relative_to(root)
        except ValueError as exc:
            raise IdentityValidationError(
                f"consumed entry escapes config root: {path}"
            ) from exc
        try:
            relative_name = relative.as_posix()
            if lexical_path.is_symlink():
                target = os.readlink(lexical_path)
                entries.append(ConsumedEntry(relative_name, "sym", target))
                render_bytes[relative_name] = resolved_path.read_bytes()
            else:
                content = supplied.get(relative_name)
                if content is None:
                    content = resolved_path.read_bytes()
                entries.append(ConsumedEntry(relative_name, "file", content))
                render_bytes[relative_name] = content
        except OSError as exc:
            raise IdentityValidationError(
                f"cannot read consumed entry: {lexical_path}"
            ) from exc
    return ConsumedSnapshot(tuple(entries), MappingProxyType(render_bytes))


def config_provenance(root: Path) -> ConfigProvenance:
    """Collect optional Git provenance; it is never identity input."""

    def run(*args: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *args], cwd=root, capture_output=True, text=True, check=False
            )
        except OSError:
            return None
        return completed.stdout.strip() if completed.returncode == 0 else None

    if run("rev-parse", "--is-inside-work-tree") != "true":
        return ConfigProvenance(None, None, None)
    return ConfigProvenance(
        "git", run("rev-parse", "HEAD"), bool(run("status", "--porcelain"))
    )


def inputs_fingerprint(
    core_version: str,
    identity: str,
    environment_inputs: dict[str, str] | None = None,
    effective_inputs: dict[str, str] | None = None,
) -> str:
    """Fingerprint inputs with injective, counted, length-prefixed NFC framing."""
    env = environment_inputs or {}
    effective = effective_inputs or {}

    def frame(value: bytes) -> bytes:
        return len(value).to_bytes(8, "big") + value

    def namespace_rows(namespace: str, values: dict[str, str]) -> bytes:
        if len(values) > 10_000:
            raise IdentityValidationError(
                f"{namespace} input count must be 0-10000; got {len(values)}"
            )
        rows = sorted(
            (
                _normalized_bytes(name, f"{namespace} input name"),
                _normalized_bytes(value, f"{namespace} input value"),
            )
            for name, value in values.items()
        )
        if any(left[0] == right[0] for left, right in pairwise(rows)):
            raise IdentityValidationError(
                f"two {namespace} inputs share a normalized name"
            )
        return len(rows).to_bytes(4, "big") + b"".join(
            frame(name) + frame(value) for name, value in rows
        )

    material = (
        b"ahif/2\0"
        + frame(_normalized_bytes(core_version, "core version"))
        + frame(_normalized_bytes(identity, "config identity"))
        + b"E"
        + namespace_rows("environment", env)
        + b"P"
        + namespace_rows("effective", effective)
    )
    if not 10 <= len(material) <= 65_536:
        raise IdentityValidationError(
            f"inputs fingerprint material must be 10-65536 bytes; got {len(material)}"
        )
    return "sha256:" + sha256(material).hexdigest()
