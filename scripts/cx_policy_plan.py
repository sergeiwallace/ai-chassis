"""Immutable policy resolution shared by attached Codex control-plane clients.

The existing one-shot ``cx`` path remains the compatibility authority during the
experiment.  This module freezes the profile-derived inputs it already uses so
the attached renderer cannot silently invent a looser policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Container
from typing import Any, Literal

Topology = Literal["per-lease", "shared"]
Persistence = Literal["ephemeral", "recovery"]
REGISTERED_ADAPTER_KINDS = frozenset({"codex-app-server", "codex-ollama-exec"})


class PolicyError(ValueError):
    """The requested policy cannot be represented safely."""


@dataclass(frozen=True)
class AgentSettings:
    topology: Topology = "per-lease"
    persistence: Persistence = "ephemeral"


@dataclass(frozen=True)
class AppServerRender:
    process: dict[str, Any]
    thread: dict[str, Any]
    turn: dict[str, Any]


@dataclass(frozen=True)
class PolicyPlan:
    role: str
    profile_path: Path
    cwd: str
    additional_dirs: tuple[str, ...]
    model: str
    effort: str
    approval_policy: str
    sandbox_mode: str | None
    developer_instructions: str
    digest: str

    def render_exec_argv(self) -> list[str]:
        """Render only the policy-owned portion of the ordinary ``codex exec`` argv."""
        argv = ["-p", self.role, "-C", self.cwd]
        for directory in self.additional_dirs:
            argv.extend(["--add-dir", directory])
        argv.extend(["-c", f'model_reasoning_effort="{self.effort}"'])
        return argv

    def render_app_server(self) -> AppServerRender:
        """Render typed app-server fields without combining sandbox mechanisms."""
        thread: dict[str, Any] = {
            "cwd": self.cwd,
            "additionalDirs": list(self.additional_dirs),
            "model": self.model,
            "developerInstructions": self.developer_instructions,
            "ephemeral": True,
        }
        if self.sandbox_mode:
            thread["sandbox"] = self.sandbox_mode
        return AppServerRender(
            process={"approvalPolicy": self.approval_policy},
            thread=thread,
            turn={"model": self.model, "effort": self.effort},
        )


@dataclass(frozen=True)
class AttachedPolicyPlan:
    """Backend-neutral immutable policy used by attached-agent adapters."""

    policy: PolicyPlan
    backend_id: str
    adapter_kind: str
    engine: str
    profile_id: str
    sandbox_mechanism: str
    network_posture: str
    digest: str

    @property
    def model(self) -> str:
        return self.policy.model

    @property
    def effort(self) -> str:
        return self.policy.effort

    @property
    def sandbox_mode(self) -> str | None:
        return self.policy.sandbox_mode


@dataclass(frozen=True)
class PolicyPlanOverrides:
    cwd: Path
    additional_dirs: tuple[Path, ...] = ()
    model: str | None = None
    effort: str | None = None


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            loaded = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PolicyError(f"cannot read policy source {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise PolicyError(f"policy source {path} is not a TOML object")
    return loaded


def _canonical_directory(path: Path) -> str:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PolicyError(f"cannot canonicalize policy root {path}: {exc}") from exc
    if not resolved.is_dir():
        raise PolicyError(f"policy root is not a directory: {resolved}")
    return str(resolved)


def resolve_agent_settings(config_path: Path) -> AgentSettings:
    """Read backend-neutral settings, falling back to legacy `[codex.agent]`."""
    if not config_path.exists():
        return AgentSettings()
    config = _load_toml(config_path)
    agents = config.get("agents", {})
    attached = agents.get("attached", {}) if isinstance(agents, dict) else {}
    legacy = config.get("codex", {}).get("agent", {})
    agent = attached or legacy
    if not isinstance(agent, dict):
        raise PolicyError("[agents.attached] or [codex.agent] must be a TOML table")
    topology = agent.get("supervisor_topology", "per-lease")
    persistence = agent.get("thread_persistence", "ephemeral")
    if topology not in ("per-lease", "shared"):
        raise PolicyError(
            "[codex.agent].supervisor_topology must be per-lease or shared"
        )
    if persistence not in ("ephemeral", "recovery"):
        raise PolicyError(
            "[codex.agent].thread_persistence must be ephemeral or recovery"
        )
    return AgentSettings(topology=topology, persistence=persistence)


def resolve_policy_plan(
    role: str,
    profile_path: Path,
    overrides: PolicyPlanOverrides | None = None,
    **override_fields: object,
) -> PolicyPlan:
    """Create a fully immutable profile plan before either backend is started."""
    overrides = overrides or PolicyPlanOverrides(**override_fields)
    profile = _load_toml(profile_path)
    required = (
        "model",
        "model_reasoning_effort",
        "approval_policy",
        "developer_instructions",
    )
    missing = [
        key
        for key in required
        if not isinstance(profile.get(key), str) or not profile[key]
    ]
    if missing:
        raise PolicyError(
            f"role profile {profile_path} lacks required keys: {', '.join(missing)}"
        )
    canonical_cwd = _canonical_directory(overrides.cwd)
    canonical_dirs = tuple(
        _canonical_directory(directory) for directory in overrides.additional_dirs
    )
    sandbox = profile.get("sandbox_mode")
    if sandbox is not None and not isinstance(sandbox, str):
        raise PolicyError(f"role profile {profile_path} has invalid sandbox_mode")
    payload = {
        "role": role,
        "profile": str(profile_path.resolve()),
        "cwd": canonical_cwd,
        "additionalDirs": canonical_dirs,
        "model": overrides.model or profile["model"],
        "effort": overrides.effort or profile["model_reasoning_effort"],
        "approvalPolicy": profile["approval_policy"],
        "sandbox": sandbox,
        "developerInstructions": profile["developer_instructions"],
    }
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    return PolicyPlan(
        role=role,
        profile_path=profile_path.resolve(),
        cwd=canonical_cwd,
        additional_dirs=canonical_dirs,
        model=payload["model"],
        effort=payload["effort"],
        approval_policy=payload["approvalPolicy"],
        sandbox_mode=sandbox,
        developer_instructions=payload["developerInstructions"],
        digest=digest,
    )


def _profile_network_posture(profile: dict[str, Any]) -> str:
    """Read the role profile's network policy without inventing a permissive default."""
    permissions_name = profile.get("default_permissions")
    if isinstance(permissions_name, str) and permissions_name:
        permissions = profile.get("permissions", {})
        named = (
            permissions.get(permissions_name, {})
            if isinstance(permissions, dict)
            else {}
        )
        network = named.get("network", {}) if isinstance(named, dict) else {}
        enabled = network.get("enabled") if isinstance(network, dict) else None
    else:
        sandbox = profile.get("sandbox_workspace_write", {})
        enabled = sandbox.get("network_access") if isinstance(sandbox, dict) else None
    if enabled is True:
        return "enabled"
    if enabled is False:
        return "disabled"
    return "unconfigured"


def _selected_backend(
    config: dict[str, Any], backend_id: str | None
) -> tuple[str, dict[str, Any]]:
    agents = config.get("agents", {})
    if not isinstance(agents, dict):
        raise PolicyError("[agents] must be a TOML table")
    attached = agents.get("attached", {})
    if not isinstance(attached, dict):
        raise PolicyError("[agents.attached] must be a TOML table")
    selected = backend_id or attached.get("default_backend", "codex-cloud")
    if not isinstance(selected, str) or not selected:
        raise PolicyError(
            "[agents.attached].default_backend must be a non-empty string"
        )
    backends = agents.get("backends", {})
    if not isinstance(backends, dict) or selected not in backends:
        raise PolicyError(f"unknown attached backend: {selected}")
    backend = backends[selected]
    if not isinstance(backend, dict):
        raise PolicyError(f"backend {selected} must be a TOML table")
    unexpected = set(backend) - {"adapter", "engine", "role_profiles"}
    if unexpected:
        raise PolicyError(
            f"backend {selected} has unsupported keys: {', '.join(sorted(unexpected))}"
        )
    return selected, backend


def resolve_attached_policy_plan(  # noqa: PLR0913 - public resolver mirrors start inputs
    role: str,
    profile_path: Path,
    *,
    config_path: Path,
    backend_id: str | None = None,
    profile_id: str | None = None,
    overrides: PolicyPlanOverrides | None = None,
    adapter_kinds: Container[str] = REGISTERED_ADAPTER_KINDS,
) -> AttachedPolicyPlan:
    """Resolve one typed backend/profile plan before an attached process can start.

    The returned ``policy`` deliberately remains the legacy immutable Codex plan.  Its
    digest therefore stays byte-for-byte compatible for callers omitting new fields;
    this outer digest covers the additional backend-bearing policy inputs.
    """
    config = _load_toml(config_path) if config_path.exists() else {}
    selected_backend, backend = _selected_backend(config, backend_id)
    adapter = backend.get("adapter")
    engine = backend.get("engine")
    if not isinstance(adapter, str) or adapter not in adapter_kinds:
        raise PolicyError(f"backend {selected_backend} names an unknown adapter")
    if not isinstance(engine, str) or not engine:
        raise PolicyError(f"backend {selected_backend} has an invalid engine")
    role_profiles = backend.get("role_profiles", {})
    if not isinstance(role_profiles, dict):
        raise PolicyError(
            f"backend {selected_backend}.role_profiles must be a TOML table"
        )
    configured_profile = role_profiles.get(role, role)
    if not isinstance(configured_profile, str) or not configured_profile:
        raise PolicyError(
            f"backend {selected_backend} has no compatible profile for {role}"
        )
    if profile_id is not None and profile_id != configured_profile:
        raise PolicyError(
            f"profile {profile_id} is incompatible with {role}/{selected_backend}"
        )
    selected_profile_path = profile_path.with_name(f"{configured_profile}.config.toml")
    if configured_profile == profile_path.name.removesuffix(".config.toml"):
        selected_profile_path = profile_path
    if not selected_profile_path.is_file():
        raise PolicyError(
            f"backend {selected_backend} profile is unavailable: {configured_profile}"
        )
    policy = resolve_policy_plan(role, selected_profile_path, overrides)
    profile = _load_toml(selected_profile_path)
    sandbox_mechanism = (
        "default_permissions"
        if isinstance(profile.get("default_permissions"), str)
        else "sandbox_mode"
    )
    network_posture = _profile_network_posture(profile)
    payload = {
        "policyDigest": policy.digest,
        "backendId": selected_backend,
        "adapter": adapter,
        "engine": engine,
        "profileId": configured_profile,
        "sandboxMechanism": sandbox_mechanism,
        "networkPosture": network_posture,
    }
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    return AttachedPolicyPlan(
        policy=policy,
        backend_id=selected_backend,
        adapter_kind=adapter,
        engine=engine,
        profile_id=configured_profile,
        sandbox_mechanism=sandbox_mechanism,
        network_posture=network_posture,
        digest=digest,
    )


def repository_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config.toml"


def installed_profile_path(role: str) -> Path:
    # ``HOME`` is intentionally consulted only for the already-installed role profile;
    # topology and persistence never derive from it or from any environment setting.
    return Path(os.path.expanduser("~/.codex")) / f"{role}.config.toml"
