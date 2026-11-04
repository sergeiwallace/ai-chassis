"""Loading and normalizing external ai-harness configuration."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .capabilities import CapabilityValidationError, validate_declarations
from .machine_registry import Registry, load_registry
from .schema import (
    MINIMUM_SUPPORTED_SCHEMA_VERSION,
    validate_mcp_settings,
    validate_schema_version,
)


@dataclass(frozen=True)
class LoadedConfig:
    root: Path
    path: Path
    data: dict
    schema_version: int
    content: bytes

    @property
    def install(self) -> dict[str, object]:
        """The validated install declaration, with safe optional defaults."""
        value = self.data.get("install", {})
        return {
            "target": [],
            "modify_shell_profile": False,
            "modify_global_git": False,
            "mark_projects_trusted": False,
            "profile_host_renames": {},
            "tier_b_entrypoints": [],
            "pi_persistent_prefix_machines": [],
            "pi_bootstrap_opt_out_machines": [],
            "owner_name": "the owner",
            "owner_pronoun": "they",
            **(value if isinstance(value, dict) else {}),
        }

    @property
    def hygiene(self) -> dict[str, object]:
        value = self.data.get("hygiene", {})
        return value if isinstance(value, dict) else {}

    @property
    def capabilities(self) -> dict[str, dict[str, object]]:
        """Return the validated, opt-in capability declarations."""
        value = self.data.get("capabilities", {})
        assert isinstance(value, dict)
        return {name: entry for name, entry in value.items() if isinstance(entry, dict)}

    @property
    def context_install_dir(self) -> Path | None:
        """Return the validated optional context-install destination."""
        site = self.data.get("site", {})
        if not isinstance(site, dict) or "context_install_dir" not in site:
            return None
        return _path(self.root, site["context_install_dir"], "site.context_install_dir")

    @property
    def previous_projects_root(self) -> Path | None:
        """Return the validated optional prior projects root for stale-link pruning."""
        site = self.data.get("site", {})
        if not isinstance(site, dict) or "previous_projects_root" not in site:
            return None
        return _path(
            self.root, site["previous_projects_root"], "site.previous_projects_root"
        )

    @property
    def registry(self) -> Registry | None:
        """The validated ``[machine.<name>]`` registry, the core's one machine table.

        ``None`` for a config that declares no ``[machine_schema]`` (a fixture or a single-machine
        install); a declared registry that fails validation raises ``MachineRegistryError``.
        """
        if "machine_schema" not in self.data:
            return None
        return load_registry(self.data)


class ConfigValidationError(ValueError):
    """A config shape is not part of schema version 1."""


_MACHINE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


def _machine_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not _MACHINE_VALUE.fullmatch(value):
        raise ConfigValidationError(
            f"{label} must contain only letters, digits, hyphens, or underscores"
        )
    return value


def _path(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigValidationError(f"{label} must be a non-empty path string")
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def _unknown(table: dict[str, object], allowed: set[str], label: str) -> None:
    for key in table:
        if key not in allowed:
            raise ConfigValidationError(f"unknown key {label}.{key}")


def validate_install_apply_config(data: dict[str, object], root: Path) -> None:
    """Validate the install/apply schema extension (design: Data Model, Schema extension for install/apply)."""
    install = data.get("install", {})
    if not isinstance(install, dict):
        raise ConfigValidationError("[install] must be a table")
    _unknown(
        install,
        {
            "state_root",
            "modify_shell_profile",
            "modify_global_git",
            "mark_projects_trusted",
            "profile_host_renames",
            "tier_b_entrypoints",
            "pi_persistent_prefix_machines",
            "pi_bootstrap_opt_out_machines",
            "owner_name",
            "owner_pronoun",
            "target",
            "rendered",
        },
        "install",
    )
    if "state_root" in install:
        _path(root, install["state_root"], "install.state_root")
    rendered = install.get("rendered")
    if rendered is not None:
        if not isinstance(rendered, dict):
            raise ConfigValidationError("[install.rendered] must be a table")
        _unknown(
            rendered, {"context_files", "rules", "keybindings"}, "install.rendered"
        )
        if "context_files" in rendered and not isinstance(
            rendered["context_files"], bool
        ):
            raise ConfigValidationError(
                "install.rendered.context_files must be a boolean"
            )
        for key in ("rules", "keybindings"):
            if key in rendered:
                _path(root, rendered[key], f"install.rendered.{key}")
    capabilities = data.get("capabilities", {})
    if not isinstance(capabilities, dict):
        raise ConfigValidationError("[capabilities] must be a table")
    for name, entry in capabilities.items():
        if not isinstance(entry, dict):
            raise ConfigValidationError(f"capabilities.{name} must be a table")
        _unknown(entry, {"mode", "machines"}, f"capabilities.{name}")
        mode = entry.get("mode")
        if mode not in {"auto", "true", "false"}:
            raise ConfigValidationError(
                f"capabilities.{name}.mode must be auto, true, or false"
            )
        if "machines" in entry:
            machines = entry["machines"]
            if (
                not isinstance(machines, list)
                or not machines
                or any(
                    not isinstance(machine, str) or not machine for machine in machines
                )
            ):
                raise ConfigValidationError(
                    f"capabilities.{name}.machines must be a non-empty string list"
                )
    try:
        validate_declarations(capabilities)
    except CapabilityValidationError as exc:
        raise ConfigValidationError(str(exc)) from exc
    site = data.get("site", {})
    if not isinstance(site, dict):
        raise ConfigValidationError("[site] must be a table")
    if "context_install_dir" in site:
        _path(root, site["context_install_dir"], "site.context_install_dir")
    if "previous_projects_root" in site:
        _path(root, site["previous_projects_root"], "site.previous_projects_root")
    if "machines" in site:
        # ISSUE-REF Phase 1 (D-1 (d)): the compiled machine set is the set of [machine.<name>]
        # tables. A leftover array would silently shadow nothing and drift, so it is refused
        # with the migration named rather than ignored.
        raise ConfigValidationError(
            "site.machines was removed; declare one [machine.<name>] table per machine "
            "instead (the compiled set is derived from the table keys)"
        )
    if "machine" in site:
        # One-machine-table design, Phase 1 (ISSUE-REF D-12 (b)): identity checks, billing mode
        # and the settings fragment live in the [machine.<name>] registry, the one machine table.
        # Pre-v1, so the old array is refused naming its replacement rather than read.
        raise ConfigValidationError(
            "[[site.machine]] was removed; declare each machine's platform, identity checks "
            "(hostname_exact, hostname_prefix, path_exists) and settings_fragment in its "
            "[machine.<name>] registry table instead (ai_chassis.machine_registry, schema 3)"
        )
    for key in ("modify_shell_profile", "modify_global_git", "mark_projects_trusted"):
        if key in install and not isinstance(install[key], bool):
            raise ConfigValidationError(f"install.{key} must be a boolean")
    for key in ("owner_name", "owner_pronoun"):
        if key in install and (
            not isinstance(install[key], str) or not install[key].strip()
        ):
            raise ConfigValidationError(f"install.{key} must be a non-empty string")
    entrypoints = install.get("tier_b_entrypoints", [])
    if not isinstance(entrypoints, list) or any(
        not isinstance(name, str)
        or not name
        or name.startswith(".")
        or "/" in name
        or "\\" in name
        for name in entrypoints
    ):
        raise ConfigValidationError(
            "install.tier_b_entrypoints must be a list of bare file names in .claude/bin"
        )
    for key in ("pi_persistent_prefix_machines", "pi_bootstrap_opt_out_machines"):
        machines = install.get(key, [])
        if not isinstance(machines, list) or any(
            not isinstance(name, str) or not name for name in machines
        ):
            raise ConfigValidationError(
                f"install.{key} must be a list of machine names"
            )
    renames = install.get("profile_host_renames")
    if renames is not None:
        if not isinstance(renames, dict):
            raise ConfigValidationError(
                "[install.profile_host_renames] must be a table"
            )
        for old, new in renames.items():
            _machine_value(old, "install.profile_host_renames key")
            _machine_value(new, "install.profile_host_renames value")
    targets = install.get("target", [])
    if not isinstance(targets, list):
        raise ConfigValidationError("[[install.target]] must be an array of tables")
    for index, target in enumerate(targets):
        if not isinstance(target, dict):
            raise ConfigValidationError("[[install.target]] entries must be tables")
        _unknown(
            target,
            {"output", "path", "kind", "consumer", "source"},
            f"install.target[{index}]",
        )
        if not isinstance(target.get("output"), str) or not target["output"]:
            raise ConfigValidationError(
                "install.target.output must be a non-empty string"
            )
        _path(root, target.get("path"), "install.target.path")
        if target.get("kind") not in {"symlink", "junction", "hardlink", "copy"}:
            raise ConfigValidationError(
                "install.target.kind must be symlink, junction, hardlink, or copy"
            )
        if "consumer" in target and (
            not isinstance(target["consumer"], str) or not target["consumer"]
        ):
            raise ConfigValidationError(
                "install.target.consumer must be a non-empty string"
            )
        if "source" in target and (
            not isinstance(target["source"], str) or not target["source"]
        ):
            raise ConfigValidationError(
                "install.target.source must be a non-empty string"
            )
    hygiene = data.get("hygiene", {})
    if not isinstance(hygiene, dict):
        raise ConfigValidationError("[hygiene] must be a table")
    _unknown(
        hygiene,
        {
            "generation_mode",
            "credential_patterns",
            "credential_allowlist",
            "retain_previous",
        },
        "hygiene",
    )
    if hygiene:
        mode = hygiene.get("generation_mode")
        if not isinstance(mode, str) or not re.fullmatch(r"0[0-7]{3,4}", mode):
            raise ConfigValidationError(
                "hygiene.generation_mode must be an octal string such as '0700'"
            )
        _path(root, hygiene.get("credential_patterns"), "hygiene.credential_patterns")
        _path(root, hygiene.get("credential_allowlist"), "hygiene.credential_allowlist")
        retain = hygiene.get("retain_previous")
        if isinstance(retain, bool) or not isinstance(retain, int) or retain < 1:
            raise ConfigValidationError(
                "hygiene.retain_previous must be an integer >= 1"
            )
    orchestrator = data.get("orchestrator")
    if orchestrator is not None:
        if not isinstance(orchestrator, dict):
            raise ConfigValidationError("[orchestrator] must be a table")
        _unknown(orchestrator, {"entrypoint", "install_sequence"}, "orchestrator")
        _path(root, orchestrator.get("entrypoint"), "orchestrator.entrypoint")
        if "install_sequence" in orchestrator:
            _path(
                root, orchestrator["install_sequence"], "orchestrator.install_sequence"
            )


def config_path(root: Path) -> Path:
    """Return the schema-1 config path, with an N-1 compatibility fallback."""
    current = root / "ai-harness.toml"
    return current if current.exists() else root / "config.toml"


def load_config(root: Path) -> LoadedConfig:
    """Load one config directory and enforce the supported schema range."""
    path = config_path(root)
    content = path.read_bytes()
    data = tomllib.loads(content.decode("utf-8"))
    # Versionless config.toml is the deliberately supported N-1 compatibility shape.
    version = data.get("config_schema_version", MINIMUM_SUPPORTED_SCHEMA_VERSION)
    validate_schema_version(version)
    validate_mcp_settings(data.get("mcp", {}))
    if version == 1:
        validate_install_apply_config(data, root)
    return LoadedConfig(
        root=root, path=path, data=data, schema_version=version, content=content
    )
