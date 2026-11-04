"""Declared optional-provider capability resolution.

The registry deliberately probes only command-line providers.  Integrations in
this harness are subprocess boundaries, so loading provider code is neither
needed nor permitted to decide whether a capability is available.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path


class CapabilityRegistryError(ValueError):
    """The packaged capability registry is invalid."""


class CapabilityValidationError(ValueError):
    """A requested capability cannot be provided."""


@dataclass(frozen=True)
class CapabilityDefinition:
    """One core-owned external-provider contract."""

    id: str
    provider: str
    remediation: str
    minimum_version: str | None = None
    version_args: tuple[str, ...] = ("--version",)


@dataclass(frozen=True)
class Capability:
    """The immutable result consumed by apply, doctor, and future plugins."""

    id: str
    requested: str
    state: str
    provider: str
    version: str | None
    reason: str
    remediation: str

    def as_dict(self) -> dict[str, object]:
        """Return the durable state-record representation."""
        return asdict(self)


_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


def load_registry(path: Path) -> dict[str, CapabilityDefinition]:
    """Load and validate a registry TOML document."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CapabilityRegistryError(
            f"could not load capability registry {path}: {exc}"
        ) from exc
    return _registry_from_data(data)


def _registry_from_data(data: object) -> dict[str, CapabilityDefinition]:
    """Validate parsed capability-registry data."""
    if not isinstance(data, dict):
        raise CapabilityRegistryError("capability registry must be a TOML table")
    entries = data.get("capabilities")
    if not isinstance(entries, dict) or not entries:
        raise CapabilityRegistryError(
            "capability registry must contain [capabilities] entries"
        )
    definitions: dict[str, CapabilityDefinition] = {}
    for capability_id, raw in entries.items():
        if not isinstance(capability_id, str) or not capability_id:
            raise CapabilityRegistryError("capability registry contains an empty ID")
        if not isinstance(raw, dict):
            raise CapabilityRegistryError(
                f"capability {capability_id!r} must be a table"
            )
        allowed = {"provider", "remediation", "minimum_version", "version_args"}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise CapabilityRegistryError(
                f"capability {capability_id!r} has unknown field(s): {', '.join(unknown)}"
            )
        provider, remediation = raw.get("provider"), raw.get("remediation")
        if not isinstance(provider, str) or not provider:
            raise CapabilityRegistryError(
                f"capability {capability_id!r} needs a provider"
            )
        if not isinstance(remediation, str) or not remediation:
            raise CapabilityRegistryError(
                f"capability {capability_id!r} needs remediation"
            )
        minimum = raw.get("minimum_version")
        if minimum is not None and (not isinstance(minimum, str) or not minimum):
            raise CapabilityRegistryError(
                f"capability {capability_id!r} minimum_version must be a non-empty string"
            )
        args = raw.get("version_args", ["--version"])
        if (
            not isinstance(args, list)
            or not args
            or not all(isinstance(arg, str) and arg for arg in args)
        ):
            raise CapabilityRegistryError(
                f"capability {capability_id!r} version_args must be a non-empty string list"
            )
        definitions[capability_id] = CapabilityDefinition(
            capability_id, provider, remediation, minimum, tuple(args)
        )
    return definitions


def default_registry() -> dict[str, CapabilityDefinition]:
    """Load the capability registry shipped with the core package."""
    resource = files("ai_chassis").joinpath("defaults/capabilities.toml")
    try:
        data = tomllib.loads(resource.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CapabilityRegistryError(
            f"could not load packaged capability registry: {exc}"
        ) from exc
    return _registry_from_data(data)


def known_capability_ids() -> tuple[str, ...]:
    """Return declared IDs in deterministic diagnostic order."""
    return tuple(sorted(default_registry()))


def validate_declarations(
    declarations: Mapping[str, Mapping[str, object]],
    registry: Mapping[str, CapabilityDefinition] | None = None,
) -> None:
    """Reject unknown config IDs with the complete core-owned vocabulary."""
    definitions = default_registry() if registry is None else registry
    unknown = sorted(set(declarations) - set(definitions))
    if unknown:
        raise CapabilityValidationError(
            "unknown capability ID(s): "
            + ", ".join(unknown)
            + "; known IDs: "
            + ", ".join(sorted(definitions))
        )


def _version_at_least(version: str, minimum: str) -> bool:
    found, required = _VERSION_RE.search(version), _VERSION_RE.search(minimum)
    if found is None or required is None:
        return False
    actual_parts = tuple(int(part) for part in found.group().split("."))
    required_parts = tuple(int(part) for part in required.group().split("."))
    width = max(len(actual_parts), len(required_parts))
    return actual_parts + (0,) * (width - len(actual_parts)) >= required_parts + (
        0,
    ) * (width - len(required_parts))


def _disabled(
    definition: CapabilityDefinition, requested: str, reason: str
) -> Capability:
    return Capability(
        definition.id,
        requested,
        "disabled",
        definition.provider,
        None,
        reason,
        definition.remediation,
    )


def resolve_capabilities(
    declarations: Mapping[str, Mapping[str, object]],
    *,
    machine: str | None = None,
    registry: Mapping[str, CapabilityDefinition] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[Capability, ...]:
    """Resolve every known capability without probing disabled declarations."""
    definitions = default_registry() if registry is None else registry
    validate_declarations(declarations, definitions)
    resolved: list[Capability] = []
    for capability_id in sorted(definitions):
        definition = definitions[capability_id]
        declaration = declarations.get(capability_id)
        if declaration is None:
            resolved.append(_disabled(definition, "false", "not declared in config"))
            continue
        requested = str(declaration["mode"])
        machines = declaration.get("machines")
        if isinstance(machines, list) and machine not in machines:
            # Out of scope for this machine is equivalent to not declared here at
            # all -- never a validate_required failure, even when mode is "true"
            # on the machines the declaration does apply to.
            resolved.append(
                _disabled(definition, "false", "not enabled for this machine")
            )
            continue
        if requested == "false":
            resolved.append(
                _disabled(definition, requested, "disabled by configuration")
            )
            continue
        provider_path = which(definition.provider)
        if provider_path is None:
            resolved.append(_disabled(definition, requested, "provider is not on PATH"))
            continue
        try:
            completed = run(
                [provider_path, *definition.version_args],
                check=False,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            resolved.append(
                _disabled(definition, requested, f"provider probe failed: {exc}")
            )
            continue
        output = completed.stdout.strip() or completed.stderr.strip()
        if completed.returncode != 0:
            resolved.append(
                _disabled(
                    definition,
                    requested,
                    f"provider probe exited {completed.returncode}",
                )
            )
            continue
        if not output:
            resolved.append(
                _disabled(definition, requested, "provider returned no version")
            )
            continue
        if definition.minimum_version and not _version_at_least(
            output, definition.minimum_version
        ):
            resolved.append(
                _disabled(
                    definition,
                    requested,
                    f"provider version {output!r} is below required {definition.minimum_version}",
                )
            )
            continue
        resolved.append(
            Capability(
                definition.id,
                requested,
                "enabled",
                definition.provider,
                output,
                "provider is available",
                definition.remediation,
            )
        )
    return tuple(resolved)


def validate_required(capabilities: Sequence[Capability]) -> None:
    """Fail required-but-disabled capabilities with their exact remediation."""
    missing = [
        capability
        for capability in capabilities
        if capability.requested == "true" and capability.state != "enabled"
    ]
    if missing:
        capability = missing[0]
        raise CapabilityValidationError(
            f"capability {capability.id!r} was requested but is disabled: "
            f"{capability.reason}. Remediation: {capability.remediation}"
        )


def records(capabilities: Sequence[Capability]) -> dict[str, dict[str, object]]:
    """Map capability IDs to durable records suitable for JSON state."""
    return {capability.id: capability.as_dict() for capability in capabilities}
