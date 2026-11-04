"""The fleet machine registry: ``[machine.<name>]`` tables in ``ai-harness.toml``.

ISSUE-REF Phase 1 (``docs/designs/ISSUE-REF-machine-profile-contract.md``, D-1 (d), D-2 (a),
D-3 (b), D-4 (b)). One registry, one generator, one digest. This module is the loader and
validator every producer-side reader shares; ``scripts/fleet_machines.py`` is the CLI around
it, and ``scripts/_fleet_machines.py`` is the generated committed fallback.

The module is SCHEMA-GENERIC on purpose: the attribute vocabulary is data in the registry's
own ``[machine_schema]`` table, not code here, so this public-core module names no fleet
machine, tool or service. Three rules the design makes binding, enforced here:

* ``machine_names()`` is the ONLY source of the compiled machine set. The old ``site.machines``
  array is gone and a config that still carries one is refused by ``config.py``, so the set of
  machines and the set of machine tables cannot disagree.
* Attribute values are declared, never detected, and come from the closed vocabulary the
  schema declares. A value outside it fails naming the machine, the attribute and the
  permitted values; a missing attribute fails naming the machine and the attribute.
* Resolution canonicalizes before lookup. An alias resolves to its canonical machine; a name
  that is neither is ``unknown`` with a diagnostic naming the value and the recognized names.
  Nothing here reads a hostname or a platform.

Schema 3 (one-machine-table design, Phase 1; ISSUE-REF D-12 (b)) adds the INSTALL keys every
table carries, because the installer acts on them rather than reporting them: ``platform``,
the identity checks ``hostname_exact``, ``hostname_prefix`` and ``path_exists`` (lists of
strings, possibly empty), and ``settings_fragment`` (a path relative to the config root, or
``""`` for no fragment). Their names and types are fixed here, not data in ``[machine_schema]``:
a wrong value there is a wrong identity, so no schema may relax them. ``ai_chassis.machine``
evaluates the checks; this module only validates them.

Schema shape (``[machine_schema]`` in the registry document)::

    [machine_schema]
    version = 2
    [machine_schema.attributes]
    os_family = ["macos", "linux", "windows"]   # a list is the closed vocabulary
    task_store = "string"                       # the literal "string" means any string
    [machine_schema.tooling]
    tmux = [true, false, "unverified"]          # tri-state values may mix types
"""

from __future__ import annotations

import hashlib
import json
import math
import pprint
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Mapping

SCHEMA_VERSION = 3
FREE_STRING = "string"
TOOLING_GROUP = "tooling"
#: `[machine.<name>.delegation]`: an explicit `investigate` family overriding the billing_mode
#: default (ISSUE-REF D-5), read by `ai_chassis.decision_routing.investigate_family`.
DELEGATION_GROUP = "delegation"

#: `[machine.<name>.quota_gate]`: the machine profile's usage-quota gate thresholds
#: (ISSUE-REF D-9), read at call time by `scripts/quota_gate.py`. Passive policy: validated
#: here, never an activation set, and kept out of the exported attributes and digest.
QUOTA_GATE_GROUP = "quota_gate"
#: The keys a quota_gate table (or one of its `features."<f>"` tables) may set, and the kind of
#: value each takes. `scripts/quota_gate.py` owns the schema; a parity test pins this copy to it.
QUOTA_GATE_KEYS: dict[str, str] = {
    "pace_margin_pct": "number",
    "high_usage_pct": "number",
    "high_usage_behind_pct": "number",
    "families": "families",
    "windows": "windows",
    "on_unknown": "on_unknown",
    "max_age_seconds": "positive",
    "enabled": "bool",
    "interval_minutes": "interval",
}
#: The fleet `[to_review]` table (ISSUE-REF D-16): its keys and their types. Passive policy read
#: at call time by `scripts/to_review_hook.py`, validated here so a typo fails the registry check.
TO_REVIEW_TABLE = "to_review"
TO_REVIEW_KEYS: dict[str, type] = {
    "mirror_turn_under_denial": bool,
    "mirror_file_write": bool,
    "mirror_file_validated_version": str,
}
#: Per-machine POLICY groups: passive configuration a module reads at call time. Neither facts
#: about the machine nor capabilities that activate somewhere.
# Quota traffic is also passive, call-time admission policy, like quota_gate.
POLICY_SUBTABLES: frozenset[str] = frozenset({QUOTA_GATE_GROUP, "quota_traffic"})

#: Registry attribute GROUPS that TOML renders as `[machine.<name>.<group>]` subtables, plus the
#: policy groups. None is a per-machine capability table, so the activation coverage guard
#: excludes exactly these from its `[machine.<name>.<capability>]` discovery.
REGISTRY_SUBTABLES: frozenset[str] = frozenset(
    {TOOLING_GROUP, DELEGATION_GROUP, *POLICY_SUBTABLES}
)

REGENERATE_COMMAND = "python3 scripts/fleet_machines.py generate"

#: The platforms an identity check is gated by: an MSYS-family shell, macOS, anything else.
PLATFORMS: tuple[str, ...] = ("windows-shell", "darwin", "linux")
#: The identity-check keys, in the order ``ai_chassis.machine`` evaluates them.
IDENTITY_CHECK_KEYS: tuple[str, ...] = (
    "hostname_exact",
    "hostname_prefix",
    "path_exists",
)
#: Every key the installer acts on (schema 3), required on every ``[machine.<name>]`` table.
INSTALL_KEYS: tuple[str, ...] = ("platform", *IDENTITY_CHECK_KEYS, "settings_fragment")


class MachineRegistryError(ValueError):
    """The registry cannot be loaded as declared. The message names what is wrong and where."""


@dataclass(frozen=True)
class Schema:
    """The declared vocabulary: attribute -> permitted values (``None`` = any string)."""

    version: int
    attributes: dict[str, tuple[object, ...] | None]
    tooling: dict[str, tuple[object, ...]]

    @property
    def required(self) -> tuple[str, ...]:
        return (*self.attributes, TOOLING_GROUP)


@dataclass(frozen=True)
class Registry:
    schema: Schema
    machines: dict[str, dict[str, object]]
    aliases: dict[str, str] = field(default_factory=dict)

    @property
    def schema_version(self) -> int:
        return self.schema.version

    def names(self) -> list[str]:
        return sorted(self.machines)


@dataclass(frozen=True)
class Resolution:
    """One lookup. ``canonical`` is ``None`` exactly when the name is unknown."""

    requested: str
    canonical: str | None
    attributes: Mapping[str, object]
    diagnostic: str

    @property
    def unknown(self) -> bool:
        return self.canonical is None


# --- names -----------------------------------------------------------------------------------


def machine_tables(document: Mapping[str, object]) -> dict[str, dict[str, object]]:
    """Every ``[machine.<name>]`` table, by name. A document with none has no machines."""
    tables = document.get("machine", {})
    if not isinstance(tables, dict):
        raise MachineRegistryError(
            "[machine] must be a table of [machine.<name>] tables"
        )
    out: dict[str, dict[str, object]] = {}
    for name, table in tables.items():
        if not isinstance(table, dict):
            raise MachineRegistryError(f"[machine.{name}] must be a table")
        out[str(name)] = table
    return out


def machine_names(document: Mapping[str, object]) -> list[str]:
    """The compiled machine set: the ``[machine.<name>]`` table keys, sorted.

    Names only, no attribute validation: compile and install derive the SET here, while the
    strict registry check (``load_registry``) runs in the generator, the pre-commit gate and
    the registry tests. A table with no registry attributes is still a machine for the
    purposes of compilation (a fixture, or a machine mid-declaration); the gate says so.
    """
    return sorted(machine_tables(document))


# --- schema ----------------------------------------------------------------------------------------


def _vocabulary(
    label: str, value: object, *, allow_free: bool
) -> tuple[object, ...] | None:
    if allow_free and value == FREE_STRING:
        return None
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, (str, bool)) for item in value)
    ):
        raise MachineRegistryError(
            f"[machine_schema] {label} must be a non-empty list of permitted values"
            + (f' or the literal "{FREE_STRING}"' if allow_free else "")
        )
    return tuple(value)


def load_schema(document: Mapping[str, object]) -> Schema:
    schema = document.get("machine_schema")
    if not isinstance(schema, dict) or "version" not in schema:
        raise MachineRegistryError(
            "[machine_schema] version is missing; declare "
            f"[machine_schema]\\nversion = {SCHEMA_VERSION}"
        )
    version = schema["version"]
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise MachineRegistryError(
            f"[machine_schema] version = {version!r} is not the supported schema "
            f"version {SCHEMA_VERSION}"
        )
    attributes = schema.get("attributes")
    tooling = schema.get(TOOLING_GROUP)
    if not isinstance(attributes, dict) or not attributes:
        raise MachineRegistryError(
            "[machine_schema.attributes] must declare at least one attribute"
        )
    if not isinstance(tooling, dict) or not tooling:
        raise MachineRegistryError(
            f"[machine_schema.{TOOLING_GROUP}] must declare at least one attribute"
        )
    reserved = [
        name for name in (TOOLING_GROUP, "aliases", *INSTALL_KEYS) if name in attributes
    ]
    if reserved:
        raise MachineRegistryError(
            "[machine_schema.attributes] may not redeclare "
            + ", ".join(repr(name) for name in reserved)
            + "; their types are fixed by the loader"
        )
    return Schema(
        version=SCHEMA_VERSION,
        attributes={
            str(name): _vocabulary(f"attributes.{name}", value, allow_free=True)
            for name, value in attributes.items()
        },
        tooling={
            str(name): _vocabulary(f"{TOOLING_GROUP}.{name}", value, allow_free=False)
            for name, value in tooling.items()
        },
    )


# --- loading and validation --------------------------------------------------------------------


def _permitted(values: tuple[object, ...]) -> str:
    return ", ".join(json.dumps(value) for value in values)


def _validate_value(
    machine: str, attribute: str, value: object, permitted: tuple[object, ...] | None
) -> None:
    if permitted is None:
        if not isinstance(value, str):
            raise MachineRegistryError(
                f"[machine.{machine}] {attribute} must be a string, got {value!r}"
            )
        return
    # bool is an int subclass; compare by exact type so True never matches 1.
    if not any(type(value) is type(option) and value == option for option in permitted):
        raise MachineRegistryError(
            f"[machine.{machine}] {attribute} = {json.dumps(value)} is outside the declared "
            f"vocabulary; permitted values: {_permitted(permitted)}"
        )


def _install_attributes(machine: str, table: Mapping[str, object]) -> dict[str, object]:
    """The schema-3 install keys, validated by their fixed types."""
    platform = table["platform"]
    _validate_value(machine, "platform", platform, PLATFORMS)
    attributes: dict[str, object] = {"platform": platform}
    for key in IDENTITY_CHECK_KEYS:
        value = table[key]
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise MachineRegistryError(
                f"[machine.{machine}] {key} must be an array of non-empty strings, "
                f"got {value!r}"
            )
        attributes[key] = list(value)
    for path in attributes["path_exists"]:  # type: ignore[union-attr]
        if not (
            PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()
        ):
            raise MachineRegistryError(
                f"[machine.{machine}] path_exists entry {path!r} must be an absolute path"
            )
    fragment = table["settings_fragment"]
    if not isinstance(fragment, str):
        raise MachineRegistryError(
            f'[machine.{machine}] settings_fragment must be a string ("" for none), '
            f"got {fragment!r}"
        )
    attributes["settings_fragment"] = fragment
    return attributes


def _quota_gate_value_ok(kind: str, value: object) -> bool:
    number = (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )
    checks = {
        "number": lambda: number,
        "positive": lambda: number and value > 0,  # type: ignore[operator]
        "bool": lambda: isinstance(value, bool),
        "interval": lambda: (
            isinstance(value, int) and not isinstance(value, bool) and value >= 1
        ),
        "on_unknown": lambda: value in ("deny", "allow"),
        "families": lambda: _string_subset(value, ("claude", "codex")),
        "windows": lambda: _string_subset(value, ("five_hour", "seven_day")),
    }
    return checks[kind]()


def _string_subset(value: object, allowed: tuple[str, ...]) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(item in allowed for item in value)
    )


def _validate_quota_gate_keys(where: str, table: Mapping[str, object]) -> None:
    for key, value in table.items():
        if key not in QUOTA_GATE_KEYS:
            raise MachineRegistryError(f"{where} declares unknown key {key!r}")
        if not _quota_gate_value_ok(QUOTA_GATE_KEYS[key], value):
            raise MachineRegistryError(
                f"{where} {key} = {value!r} is not a valid {QUOTA_GATE_KEYS[key]} value"
            )


def validate_quota_gate(machine: str, table: object) -> None:
    """Refuse a `[machine.<name>.quota_gate]` table with an unknown key or a wrong type."""
    where = f"[machine.{machine}.{QUOTA_GATE_GROUP}]"
    if not isinstance(table, dict):
        raise MachineRegistryError(f"{where} must be a table")
    features = table.get("features", {})
    if not isinstance(features, dict):
        raise MachineRegistryError(
            f"{where} features must be a table of feature tables"
        )
    _validate_quota_gate_keys(
        where, {key: value for key, value in table.items() if key != "features"}
    )
    for feature, feature_table in features.items():
        label = f'[machine.{machine}.{QUOTA_GATE_GROUP}.features."{feature}"]'
        if not isinstance(feature_table, dict):
            raise MachineRegistryError(f"{label} must be a table")
        _validate_quota_gate_keys(label, feature_table)


def _registry_attributes(
    schema: Schema, machine: str, table: Mapping[str, object]
) -> dict[str, object]:
    attributes: dict[str, object] = {}
    if QUOTA_GATE_GROUP in table:
        validate_quota_gate(machine, table[QUOTA_GATE_GROUP])
    missing = [key for key in (*schema.required, *INSTALL_KEYS) if key not in table]
    if missing:
        raise MachineRegistryError(
            f"[machine.{machine}] omits required attribute(s): {', '.join(missing)}"
        )
    for attribute, permitted in schema.attributes.items():
        value = table[attribute]
        _validate_value(machine, attribute, value, permitted)
        attributes[attribute] = value
    tooling = table[TOOLING_GROUP]
    if not isinstance(tooling, dict):
        raise MachineRegistryError(
            f"[machine.{machine}] {TOOLING_GROUP} must be a table"
        )
    tooling_missing = [key for key in schema.tooling if key not in tooling]
    if tooling_missing:
        raise MachineRegistryError(
            f"[machine.{machine}] {TOOLING_GROUP} omits required attribute(s): "
            + ", ".join(f"{TOOLING_GROUP}.{key}" for key in tooling_missing)
        )
    unknown_tooling = sorted(set(tooling) - set(schema.tooling))
    if unknown_tooling:
        raise MachineRegistryError(
            f"[machine.{machine}] {TOOLING_GROUP} declares undeclared attribute(s): "
            + ", ".join(f"{TOOLING_GROUP}.{key}" for key in unknown_tooling)
        )
    for key, permitted in schema.tooling.items():
        _validate_value(machine, f"{TOOLING_GROUP}.{key}", tooling[key], permitted)
    attributes[TOOLING_GROUP] = {key: tooling[key] for key in schema.tooling}
    attributes.update(_install_attributes(machine, table))
    aliases = table.get("aliases", [])
    if not isinstance(aliases, list) or not all(
        isinstance(alias, str) and alias for alias in aliases
    ):
        raise MachineRegistryError(
            f"[machine.{machine}] aliases must be an array of non-empty strings"
        )
    attributes["aliases"] = list(aliases)
    return attributes


def validate_to_review(document: Mapping[str, object]) -> None:
    """Refuse a `[to_review]` table with an unknown key or a wrong type."""
    table = document.get(TO_REVIEW_TABLE)
    if table is None:
        return
    if not isinstance(table, dict):
        raise MachineRegistryError(f"[{TO_REVIEW_TABLE}] must be a table")
    for key, value in table.items():
        wanted = TO_REVIEW_KEYS.get(key)
        if wanted is None:
            raise MachineRegistryError(
                f"[{TO_REVIEW_TABLE}] declares unknown key {key!r}"
            )
        if type(value) is not wanted:
            raise MachineRegistryError(
                f"[{TO_REVIEW_TABLE}] {key} = {value!r} must be {wanted.__name__}"
            )


def load_registry(document: Mapping[str, object]) -> Registry:
    """Validate and return the registry declared by ``document`` (a parsed ai-harness.toml)."""
    schema = load_schema(document)
    validate_to_review(document)
    tables = machine_tables(document)
    if not tables:
        raise MachineRegistryError("the registry declares no [machine.<name>] table")
    machines: dict[str, dict[str, object]] = {}
    for name in sorted(tables):
        machines[name] = _registry_attributes(schema, name, tables[name])
    aliases: dict[str, str] = {}
    for name, attributes in machines.items():
        for alias in attributes["aliases"]:  # type: ignore[union-attr]
            if alias in machines:
                raise MachineRegistryError(
                    f"[machine.{name}] declares alias {alias!r}, which is itself a machine"
                )
            if alias in aliases and aliases[alias] != name:
                first, second = sorted((aliases[alias], name))
                raise MachineRegistryError(
                    f"alias {alias!r} is declared by two machines: "
                    f"[machine.{first}] and [machine.{second}]"
                )
            aliases[alias] = name
    return Registry(schema=schema, machines=machines, aliases=aliases)


# --- resolution ------------------------------------------------------------------------------------


def canonicalize(registry: Registry, name: str) -> str | None:
    """The canonical machine for ``name`` (itself, or the alias target), else ``None``."""
    if name in registry.machines:
        return name
    return registry.aliases.get(name)


def resolve(registry: Registry, name: str | None) -> Resolution:
    """Resolve ``name`` through the registry, failing closed with a diagnostic."""
    recognized = ", ".join(sorted([*registry.machines, *registry.aliases]))
    if not name:
        return Resolution(
            requested=name or "",
            canonical=None,
            attributes={},
            diagnostic=(
                "no machine name was supplied and AI_HOST is unset; recognized names: "
                f"{recognized}"
            ),
        )
    canonical = canonicalize(registry, name)
    if canonical is None:
        return Resolution(
            requested=name,
            canonical=None,
            attributes={},
            diagnostic=(
                f"{name!r} is neither a machine nor a declared alias; recognized names: "
                f"{recognized}"
            ),
        )
    return Resolution(
        requested=name,
        canonical=canonical,
        attributes=registry.machines[canonical],
        diagnostic="",
    )


def attribute_value(attributes: Mapping[str, object], dotted: str) -> object:
    """``tooling.codex`` style lookup; raises ``KeyError`` naming the attribute when absent."""
    value: object = attributes
    for part in dotted.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise KeyError(dotted)
        value = value[part]
    return value


# --- digest, artifact, fallback --------------------------------------------------------------------


def _canonical_registry_payload(registry: Registry) -> dict[str, object]:
    return {
        "schema_version": registry.schema_version,
        "aliases": dict(sorted(registry.aliases.items())),
        "machines": {name: registry.machines[name] for name in registry.names()},
    }


def registry_digest(registry: Registry) -> str:
    """``sha256:<hex>`` of the canonical serialization of the machine tables and aliases only.

    ``generated_at`` and ``generated_by`` are excluded by construction: a regeneration that
    changes nothing produces an identical digest, so it never creates a spurious diff.
    """
    serialized = json.dumps(
        _canonical_registry_payload(registry),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def artifact_payload(
    registry: Registry, *, generated_at: str, generated_by: str
) -> dict[str, object]:
    """The exported artifact's content (``~/.config/ai-fleet/machines.json``)."""
    return {
        **_canonical_registry_payload(registry),
        "registry_digest": registry_digest(registry),
        "generated_at": generated_at,
        "generated_by": generated_by,
    }


def fallback_module_source(registry: Registry, *, repo_label: str) -> str:
    """A generated Python data module: literals plus the digest, no logic.

    Python literals (``pprint``), not JSON: ``true`` is not a Python name, and the module is
    imported by consumers under any interpreter.
    """
    payload = _canonical_registry_payload(registry)
    lines = [
        "# GENERATED by ai-harness scripts/fleet_machines.py -- DO NOT EDIT.",
        "# Regenerate:  python3 <ai-harness>/scripts/fleet_machines.py emit-fallback "
        f"--repo {repo_label}",
        "# Data only: the resolution logic lives in the consumer and is reviewed there.",
        f"SCHEMA_VERSION = {registry.schema_version}",
        f"REGISTRY_DIGEST = {registry_digest(registry)!r}",
        f"ALIASES = {pprint.pformat(payload['aliases'], width=96, sort_dicts=True)}",
        f"MACHINES = {pprint.pformat(payload['machines'], width=96, sort_dicts=True)}",
        "",
    ]
    return "\n".join(lines)
