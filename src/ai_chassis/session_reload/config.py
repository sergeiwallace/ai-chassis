"""The reload registry: ``[session_reload]`` in ``ai-harness.toml``, read at call time.

Resolution, later wins (design ISSUE-REF § Configuration, D-4): the defaults below, the
fleet ``[session_reload]``, this machine's ``[machine.<profile>.session_reload]``, then the
XDG overlay's own ``[session_reload]`` and ``[machine.<profile>.session_reload]``. Targets
are ``[session_reload.target.<name>]`` tables merged key by key through the same layers.
An invalid setting raises :class:`ConfigInvalid`, which the pass turns into
``SR-DISABLED``; an invalid target is dropped with its reason while the others run.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_chassis import agent_mail_notifier

#: Overrides the tracked toml's path, for tests and for a host reading a copy.
CONFIG_PATH_ENV = "AIH_SESSION_RELOAD_CONFIG_PATH"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "targets": [],
    "settle_seconds": 60,
    "retry_after_seconds": 600,
    "max_attempts": 3,
    "max_injections_per_tick": 3,
    "refresh_interval_seconds": 600,
}
TARGET_DEFAULTS: dict[str, Any] = {"enabled": True, "refresh": True}
_NUMBERS = ("settle_seconds", "retry_after_seconds", "refresh_interval_seconds")
_COUNTS = ("max_attempts", "max_injections_per_tick")
_TARGET_KEYS = frozenset({"kind", "command", "enabled", "refresh"})


class ConfigInvalid(ValueError):
    """The tables cannot be read or carry a setting of the wrong shape."""


@dataclass(frozen=True)
class Target:
    name: str
    kind: str
    command: str
    enabled: bool
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Resolved:
    profile: str | None
    settings: dict[str, Any]
    #: The layer each setting's value came from.
    sources: dict[str, str]
    targets: tuple[Target, ...]
    #: ``(target name, reason)`` for every registered target that was dropped.
    dropped: tuple[tuple[str, str], ...]
    config_source: str

    @property
    def enabled_targets(self) -> tuple[Target, ...]:
        return tuple(target for target in self.targets if target.enabled)


def tracked_path() -> Path:
    override = os.environ.get(CONFIG_PATH_ENV)
    return Path(override) if override else agent_mail_notifier.CONFIG_PATH


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigInvalid(f"{path.name}: {error}") from error


def machine_profile(tables: Mapping[str, Any]) -> str | None:
    """This machine's ``[machine.<name>]`` profile: the fleet label itself, else the
    profile that declares it as an alias, else None."""
    label = agent_mail_notifier.resolve_machine_label()[0]
    machines = tables.get("machine")
    if not isinstance(machines, Mapping):
        return None
    if label in machines:
        return label
    for name, table in machines.items():
        aliases = table.get("aliases") if isinstance(table, Mapping) else None
        if isinstance(aliases, list) and label in aliases:
            return str(name)
    return None


def _section(tables: Mapping[str, Any], profile: str | None, *, machine: bool) -> Any:
    if not machine:
        return tables.get("session_reload")
    if profile is None:
        return None
    machines = tables.get("machine")
    table = machines.get(profile) if isinstance(machines, Mapping) else None
    return table.get("session_reload") if isinstance(table, Mapping) else None


def _check_setting(layer: str, key: str, value: Any) -> None:
    if key not in DEFAULTS:
        raise ConfigInvalid(f"unknown key {layer}.{key}")
    if key == "enabled":
        ok = isinstance(value, bool)
    elif key == "targets":
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
    elif key in _COUNTS:
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= 1
    else:
        ok = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= 0
        )
    if not ok:
        raise ConfigInvalid(f"invalid value {layer}.{key}")


def _target_error(table: Mapping[str, Any]) -> str | None:
    from ai_chassis.session_reload.kinds import KINDS  # noqa: PLC0415 - kinds imports this module

    kind = table.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        return "unknown-kind"
    command = table.get("command")
    if not isinstance(command, str) or not command.startswith("/") or " " in command:
        return "invalid-command"
    for key in ("enabled", "refresh"):
        if not isinstance(table.get(key), bool):
            return f"invalid-{key}"
    for required in KINDS[kind].required:
        if not isinstance(table.get(required), str) or not table[required]:
            return f"missing-{required}"
    return None


def _apply(
    layer: str,
    section: Any,
    settings: dict[str, Any],
    sources: dict[str, str],
    target_tables: dict[str, dict[str, Any]],
) -> None:
    """Merge one layer's section over the settings and target tables so far."""
    if not isinstance(section, Mapping):
        raise ConfigInvalid(f"{layer} is not a table")
    for key, value in section.items():
        if key != "target":
            _check_setting(layer, key, value)
            settings[key] = value
            sources[key] = layer
            continue
        if not isinstance(value, Mapping) or not all(
            isinstance(t, Mapping) for t in value.values()
        ):
            raise ConfigInvalid(f"{layer}.target is not a table of tables")
        for name, table in value.items():
            target_tables.setdefault(str(name), {}).update(table)


def resolve(
    tracked: Mapping[str, Any],
    overlay: Mapping[str, Any],
    profile: str | None,
    *,
    config_source: str = "",
) -> Resolved:
    """Resolve the layers for ``profile``; raises :class:`ConfigInvalid`."""
    settings = dict(DEFAULTS)
    sources = dict.fromkeys(DEFAULTS, "default")
    target_tables: dict[str, dict[str, Any]] = {}
    layers = (
        ("fleet", tracked, False),
        (f"machine:{profile}", tracked, True),
        ("overlay", overlay, False),
        (f"overlay-machine:{profile}", overlay, True),
    )
    for layer, tables, machine in layers:
        section = _section(tables, profile, machine=machine)
        if section is not None:
            _apply(layer, section, settings, sources, target_tables)
    targets: list[Target] = []
    dropped: list[tuple[str, str]] = []
    for name in dict.fromkeys(settings["targets"]):
        table = target_tables.get(name)
        if table is None:
            dropped.append((name, "no-target-table"))
            continue
        merged = {**TARGET_DEFAULTS, **table}
        error = _target_error(merged)
        if error is not None:
            dropped.append((name, error))
            continue
        targets.append(
            Target(
                name=name,
                kind=merged["kind"],
                command=merged["command"],
                enabled=merged["enabled"],
                options={k: v for k, v in merged.items() if k not in _TARGET_KEYS}
                | {"refresh": merged["refresh"]},
            )
        )
    return Resolved(
        profile=profile,
        settings=settings,
        sources=sources,
        targets=tuple(targets),
        dropped=tuple(dropped),
        config_source=config_source,
    )


def session_reload_declares_a_capability(table: object) -> bool:
    """Whether a ``[machine.<profile>.session_reload]`` table turns the pass on for that
    profile: it registers at least one target and does not set ``enabled = false``. The
    activation-coverage guard imports this as its one definition of "declared on"."""
    if not isinstance(table, Mapping) or table.get("enabled") is False:
        return False
    targets = table.get("targets")
    return isinstance(targets, list) and any(isinstance(t, str) and t for t in targets)


def load() -> Resolved:
    """Read the tracked toml and the overlay now and resolve this machine's profile."""
    path = tracked_path()
    overlay_path = agent_mail_notifier.overlay_config_path()
    tracked = _read(path)
    overlay = _read(overlay_path)
    source = str(path) if path.exists() else "default"
    if overlay_path.exists():
        source = f"{source} + overlay:{overlay_path}"
    return resolve(tracked, overlay, machine_profile(tracked), config_source=source)
