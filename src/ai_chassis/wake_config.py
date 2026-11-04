"""Resolve per-session wake sources and actions through the fleet cascade.

``resolve`` is the pure boundary: callers inject already-loaded tables plus repository and role
lookups. ``resolve_runtime`` reads the shared notifier configuration at call time and supplies the
fleet's existing lookup readers. The future chief loop can call ``resolved_sources(slug, host)``;
it returns only the effective source tuple while retaining the same fail-closed resolution.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from ai_chassis import chief_of_staff, machine_id, remote_launch

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_SOURCES = ("prompt",)
DEFAULT_ACTIONS: tuple[str, ...] = ()
SOURCE_VALUES = frozenset(
    {"message", "prompt", "post_boot", "dated_oneshot", "goal_waker"}
)
ACTION_VALUES = frozenset({"wake", "launch", "restart"})
ROLE_VALUES = frozenset({"chief-of-staff", "repo-session", "agent"})

RepoLookup = Callable[[str], tuple[str | None, str]]
RoleLookup = Callable[[str, str], tuple[str, str]]


@dataclass(frozen=True)
class WakeResolution:
    """One immutable effective configuration and the source of every resolved fact."""

    session: str
    host: str
    repo: str | None
    role: str
    sources: tuple[str, ...]
    actions: tuple[str, ...]
    sources_layer: str
    actions_layer: str
    repo_source: str
    role_source: str
    error: str | None = None


class _InvalidWakeTable(ValueError):
    pass


def _fail_closed(  # noqa: PLR0913 - one argument per immutable resolution fact.
    *,
    slug: str,
    host: str,
    repo: str | None,
    role: str,
    repo_source: str,
    role_source: str,
    error: str,
) -> WakeResolution:
    return WakeResolution(
        session=slug,
        host=host,
        repo=repo,
        role=role,
        sources=DEFAULT_SOURCES,
        actions=DEFAULT_ACTIONS,
        sources_layer="default",
        actions_layer="default",
        repo_source=repo_source,
        role_source=role_source,
        error=error,
    )


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _InvalidWakeTable(f"unknown value {path}")
    return value


def _string_list(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _InvalidWakeTable(f"unknown value {path}")
    return tuple(value)


def _validate_values(
    table: Mapping[str, Any], path: str, *, repo_set: bool = False
) -> None:
    allowed = {"sources", "actions"}
    if repo_set:
        allowed.add("repos")
    for key in table:
        if key not in allowed:
            raise _InvalidWakeTable(f"unknown key {path}.{key}")
    if "sources" in table:
        values = _string_list(table["sources"], f"{path}.sources")
        if any(value not in SOURCE_VALUES for value in values):
            raise _InvalidWakeTable(f"unknown value {path}.sources")
    if "actions" in table:
        values = _string_list(table["actions"], f"{path}.actions")
        if any(value not in ACTION_VALUES for value in values):
            raise _InvalidWakeTable(f"unknown value {path}.actions")
    if repo_set and "repos" in table:
        _string_list(table["repos"], f"{path}.repos")


def _validate_root(table: Mapping[str, Any], path: str) -> None:
    allowed = {
        "sources",
        "actions",
        "repo_set_order",
        "repo_sets",
        "repos",
        "roles",
        "sessions",
    }
    for key in table:
        if key not in allowed:
            raise _InvalidWakeTable(f"unknown key {path}.{key}")
    _validate_values(
        {key: table[key] for key in ("sources", "actions") if key in table}, path
    )
    if "repo_set_order" in table:
        _string_list(table["repo_set_order"], f"{path}.repo_set_order")
    for key in ("repo_sets", "repos", "roles", "sessions"):
        if key in table:
            _mapping(table[key], f"{path}.{key}")


def wake_declares_a_capability(table: object) -> bool:
    """Whether a valid machine wake table declares sources, actions or overrides."""
    if not isinstance(table, dict):
        return False
    try:
        _validate_root(table, "wake")
    except _InvalidWakeTable:
        return False
    return any(
        table.get(key)
        for key in ("sources", "actions", "repo_sets", "repos", "roles", "sessions")
    )


def _child(
    table: Mapping[str, Any], container: str, name: str, path: str
) -> tuple[Mapping[str, Any] | None, str]:
    values = table.get(container)
    child_path = f"{path}.{container}.{name}"
    if not isinstance(values, Mapping) or name not in values:
        return None, child_path
    return _mapping(values[name], child_path), child_path


def _repo_set_repos(
    fleet_table: Mapping[str, Any] | None,
    machine_table: Mapping[str, Any] | None,
    fleet_path: str,
    machine_path: str,
) -> tuple[str, ...]:
    repos: tuple[str, ...] = ()
    if fleet_table is not None and "repos" in fleet_table:
        repos = _string_list(fleet_table["repos"], f"{fleet_path}.repos")
    if machine_table is not None and "repos" in machine_table:
        repos = _string_list(machine_table["repos"], f"{machine_path}.repos")
    return repos


def resolve(  # noqa: C901, PLR0915 - the six specified layers are applied in order.
    tables: Mapping[str, Any],
    host: str,
    slug: str,
    *,
    repo_lookup: RepoLookup,
    role_lookup: RoleLookup,
) -> WakeResolution:
    """Resolve ``slug`` on ``host`` from ``tables``, with all external inputs injected."""
    try:
        repo, repo_source = repo_lookup(slug)
    except Exception as exc:  # noqa: BLE001 - lookup failure is reported provenance.
        repo, repo_source = None, f"repo lookup unavailable: {exc}"
    try:
        role, role_source = role_lookup(slug, host)
    except Exception as exc:  # noqa: BLE001 - role has a specified fallback.
        role, role_source = "repo-session", f"fallback: role lookup unavailable: {exc}"
    if role not in ROLE_VALUES:
        role, role_source = (
            "repo-session",
            f"fallback: role lookup returned unknown role {role!r}",
        )

    sources, actions = DEFAULT_SOURCES, DEFAULT_ACTIONS
    sources_layer = actions_layer = "default"

    def apply(
        table: Mapping[str, Any] | None, path: str, *, repo_set: bool = False
    ) -> None:
        nonlocal sources, actions, sources_layer, actions_layer
        if table is None:
            return
        _validate_values(table, path, repo_set=repo_set)
        if "sources" in table:
            sources = tuple(table["sources"])
            sources_layer = path
        if "actions" in table:
            actions = tuple(table["actions"])
            actions_layer = path

    try:
        raw_wake = tables.get("wake", {})
        wake = _mapping(raw_wake, "wake")
        machines = tables.get("machine", {})
        machines = machines if isinstance(machines, Mapping) else {}
        machine = machines.get(host, {})
        machine = machine if isinstance(machine, Mapping) else {}
        raw_machine_wake = machine.get("wake", {})
        machine_wake = _mapping(raw_machine_wake, f"machine.{host}.wake")

        _validate_root(wake, "wake")
        _validate_root(machine_wake, f"machine.{host}.wake")
        apply(
            {key: wake[key] for key in ("sources", "actions") if key in wake},
            "wake",
        )
        apply(
            {
                key: machine_wake[key]
                for key in ("sources", "actions")
                if key in machine_wake
            },
            f"machine.{host}.wake",
        )

        order_value = machine_wake.get("repo_set_order", wake.get("repo_set_order", []))
        order = _string_list(order_value, "wake.repo_set_order")
        for set_name in order:
            fleet_set, fleet_path = _child(wake, "repo_sets", set_name, "wake")
            machine_set, machine_path = _child(
                machine_wake, "repo_sets", set_name, f"machine.{host}.wake"
            )
            if fleet_set is None and machine_set is None:
                raise _InvalidWakeTable(
                    f"unknown value wake.repo_set_order ({set_name})"
                )
            set_repos = _repo_set_repos(
                fleet_set, machine_set, fleet_path, machine_path
            )
            if repo is not None and repo in set_repos:
                apply(fleet_set, fleet_path, repo_set=True)
                apply(machine_set, machine_path, repo_set=True)

        if repo is not None:
            fleet_repo, fleet_path = _child(wake, "repos", repo, "wake")
            machine_repo, machine_path = _child(
                machine_wake, "repos", repo, f"machine.{host}.wake"
            )
            apply(fleet_repo, fleet_path)
            apply(machine_repo, machine_path)

        fleet_role, fleet_path = _child(wake, "roles", role, "wake")
        machine_role, machine_path = _child(
            machine_wake, "roles", role, f"machine.{host}.wake"
        )
        apply(fleet_role, fleet_path)
        apply(machine_role, machine_path)

        fleet_session, fleet_path = _child(wake, "sessions", slug, "wake")
        machine_session, machine_path = _child(
            machine_wake, "sessions", slug, f"machine.{host}.wake"
        )
        apply(fleet_session, fleet_path)
        apply(machine_session, machine_path)
    except _InvalidWakeTable as exc:
        return _fail_closed(
            slug=slug,
            host=host,
            repo=repo,
            role=role,
            repo_source=repo_source,
            role_source=role_source,
            error=str(exc),
        )

    return WakeResolution(
        session=slug,
        host=host,
        repo=repo,
        role=role,
        sources=sources,
        actions=actions,
        sources_layer=sources_layer,
        actions_layer=actions_layer,
        repo_source=repo_source,
        role_source=role_source,
    )


def permit(action: str, resolution: WakeResolution) -> tuple[bool, str]:
    """Allow a resolved action, or refuse while naming the layer that set ``actions``."""
    if action in resolution.actions:
        return True, f"permitted by {resolution.actions_layer}"
    return False, f"refused: {action} omitted by {resolution.actions_layer}"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def default_repo_lookup(slug: str) -> tuple[str | None, str]:
    """Reuse the fleet's canonical session-prefix resolver."""
    path = REPO_ROOT / "skills" / "_shared" / "ask-fidelity" / "ask_fidelity.py"
    try:
        module = _load_module("wake_config_ask_fidelity", path)
        return module.session_repository(slug), f"{path}:session_repository"
    except Exception as exc:  # noqa: BLE001 - unresolved repo is explicit provenance.
        return None, f"repo lookup unavailable: {exc}"


def default_role_lookup(
    slug: str,
    host: str,  # noqa: ARG001 - host is part of the RoleLookup interface.
) -> tuple[str, str]:
    """Read the chief registry, then the agent launch ledger, else use repo-session."""
    homes = chief_of_staff.chief_homes()
    machine_key = (
        machine_id._recorded_id(machine_id.config_path(Path.home(), os.environ)) or ""
    ).strip() or os.environ.get("AI_MACHINE_ID", "").strip()
    problems: list[str] = []
    candidates: list[tuple[bool, Path, dict[str, Any]]] = []
    for home in homes:
        registry, problem = chief_of_staff.load_registry(home)
        if problem is not None:
            problems.append(f"{home}: {problem}")
            continue
        try:
            registration = json.loads(
                (home / "state" / "chief-session.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            registration = {}
        local = (
            bool(machine_key)
            and isinstance(registration, dict)
            and registration.get("machine_key") == machine_key
        )
        candidates.append((local, home, registry))
    candidates.sort(key=lambda item: not item[0])
    if candidates:
        _, home, registry = candidates[0]
        if chief_of_staff.chief_name(registry, home) == slug:
            return "chief-of-staff", str(chief_of_staff.registry_path(home))
    else:
        detail = "; ".join(problems) or "no chief homes under state root"
        return "repo-session", f"fallback: chief registry unavailable: {detail}"

    ledger_path = REPO_ROOT / "scripts" / "coordinator_launch_ledger.py"
    try:
        ledger = _load_module("wake_config_agent_launch_ledger", ledger_path)
        for directory in ledger.default_subagents_dirs():
            for agent_id, record in ledger.launch_records(directory).items():
                worktree = record.get("worktree")
                if agent_id == slug or (
                    isinstance(worktree, str) and Path(worktree).name == slug
                ):
                    return "agent", f"{ledger_path}:launch_records"
    except Exception as exc:  # noqa: BLE001 - role fallback is required.
        return "repo-session", f"fallback: agent launch ledger unavailable: {exc}"
    return "repo-session", f"{ledger_path}:launch_records (no matching agent)"


def resolve_runtime(slug: str, host: str | None = None) -> WakeResolution:
    """Read current tables and resolve one session using the fleet's existing readers."""
    from ai_chassis.agent_mail_notifier import resolve_machine_label

    resolved_host = host or resolve_machine_label()[0]
    return resolve(
        remote_launch.read_tables(),
        resolved_host,
        slug,
        repo_lookup=default_repo_lookup,
        role_lookup=default_role_lookup,
    )


def resolved_sources(slug: str, host: str | None = None) -> tuple[str, ...]:
    """Return the chief loop's effective wake sources, read from configuration now."""
    return resolve_runtime(slug, host).sources


def sidecar_permit(
    action: str, machine_config: Any, resolution: WakeResolution
) -> tuple[bool, str]:
    """Compose machine and session gates, including the future restart verb's seam."""
    from ai_chassis.wake_sidecar import action_permitted

    allowed, reason = action_permitted(machine_config, action)
    if not allowed:
        if "machine." in reason:
            return False, reason
        return False, f"machine.{resolution.host}.wake_sidecar.actions: {reason}"
    allowed, reason = permit(action, resolution)
    if not allowed:
        return allowed, reason
    if action == "wake" and "message" not in resolution.sources:
        return False, f"refused: message omitted by {resolution.sources_layer}"
    return True, reason
