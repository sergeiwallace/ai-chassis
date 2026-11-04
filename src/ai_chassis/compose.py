"""Layered values and version-pinned module replacement validation."""

import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType


class ModuleValidationError(ValueError):
    """A module declaration violates the composition contract."""


DEFAULT_MODULES = MappingProxyType(
    {
        "defaults/agents-base.md": (
            "1",
            "fdbf3f3a4f03b3923c43b151f260f97ecc33b39bbf13406edf57a7176b27d10c",
        ),
    }
)


def validate_packaged_default(
    path: Path, public_path: str | None = None, content: bytes | None = None
) -> None:
    """Require a declared module version to accompany every content revision."""
    module_name = public_path or f"defaults/{path.name}"
    expected = DEFAULT_MODULES.get(module_name)
    if expected is None:
        raise ModuleValidationError(
            f"unregistered shipped default module: {module_name}"
        )
    version, digest = expected
    content = path.read_bytes() if content is None else content
    marker = f"<!-- ai-harness-module-version: {version} -->".encode()
    if not content.startswith(marker) or sha256(content).hexdigest() != digest:
        raise ModuleValidationError(
            f"shipped default module {module_name} content changed without a declared module version bump"
        )


@dataclass(frozen=True)
class ValueSource:
    value: object
    layer: str
    source: Path


@dataclass(frozen=True)
class Explanation:
    key: str
    winner: ValueSource
    shadowed: tuple[ValueSource, ...]

    def format(self) -> str:
        lines = [
            f"{self.key} = {self.winner.value!r}",
            f"winner: {self.winner.layer} ({self.winner.source})",
        ]
        lines.extend(
            f"shadowed: {item.value!r} from {item.layer} ({item.source})"
            for item in self.shadowed
        )
        return "\n".join(lines)


def resolve_values(
    key: str,
    layers: list[tuple[str, Mapping[str, object], Path]],
) -> Explanation:
    """Resolve a key in low-to-high precedence order with full provenance."""
    found = [
        ValueSource(values[key], layer, source)
        for layer, values, source in layers
        if key in values
    ]
    if not found:
        raise KeyError(key)
    return Explanation(key, found[-1], tuple(reversed(found[:-1])))


def validate_replacements(
    replacements: list[Mapping[str, object]],
    packaged_versions: Mapping[str, str],
) -> None:
    """Reject unpinned replaces and warn when a pinned public module is stale."""
    for entry in replacements:
        public_path = str(entry.get("public_path", "<unnamed>"))
        if "replaces_version" not in entry:
            raise ModuleValidationError(
                f"modules.replace entry {public_path!r} omits replaces_version"
            )
        packaged = packaged_versions.get(public_path)
        pinned = str(entry["replaces_version"])
        if packaged is not None and _version_key(pinned) < _version_key(packaged):
            warnings.warn(
                f"modules.replace {public_path!r} pins version {pinned}; "
                f"packaged module is version {packaged}",
                stacklevel=2,
            )


def _version_key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError as exc:
        raise ModuleValidationError(
            f"module version must be numeric: {version!r}"
        ) from exc
