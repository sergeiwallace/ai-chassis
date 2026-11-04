"""The public ai-harness command-line interface."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import traceback
from importlib.metadata import PackageNotFoundError, distribution, distributions
from pathlib import Path
from urllib.parse import unquote, urlparse

from platformdirs import user_state_path

from .apply import (
    ApplyError,
    InstallTarget,
    _validate_artifacts,
    apply,
    doctor,
    quarantine,
    rollback,
)
from .generations import manifest_path, pointer, read_json
from .checkpoint_activation import doctor_activation
from .capabilities import (
    Capability,
    CapabilityValidationError,
    records,
    resolve_capabilities,
    validate_required,
)
from .config import ConfigValidationError, LoadedConfig, load_config
from . import claude_daemon_service
from .discovery import config_root, discover_config_root, resolve_projects_root
from .links import LinkError
from .machine import (
    AmbiguousMachineError,
    EmptyMarkerError,
    MachineDetectionError,
    machine_specs,
    resolve_machine_identity,
)
from .machine_registry import Registry, canonicalize, machine_tables
from .machine_settings import merge_machine_fragment, read_machine_fragment
from .platform import PlatformDetectionError
from .profile import (
    NATIVE_OPS_FILE_ENV,
    HandBackRefused,
    ProfileMigrationError,
    _is_windows_shell as profile_is_windows_shell,
    adopt_handed_back_ops,
    append_export,
    pending_windows_ops,
    queue_handed_back_op,
    read_handed_back,
    migrate_profile_host_value,
    migrate_profile_var,
    flush_windows_user_env,
    persist_exported_var,
    persist_helper_bin_path,
    persist_managed_exported_var,
    profile_targets,
)
from .platform import resolve_install_path
from .render import build_render_plan, explain_config_value
from .schema import SchemaCompatibilityError
from . import (
    aido_config,
    boot_check,
    canonical_readme_sync,
    claude_readiness,
    codex_integration,
    compact_wake_readiness,
    core_requirements,
    deferred_failures,
    dependency_bootstrap,
    durable_tmux,
    hook_links,
    hooks_manifest,
    idle_wake,
    install_sequence,
    link_publication,
    machine_id,
    manual_steps,
    node_toolchain,
    pi_integration,
    projects_link,
    python_shim,
    real_git,
    rendered_links,
    retired_wrappers,
    restart_mcp_install,
    session_model,
    session_names,
    settings_lock,
    shared_agents,
    shell_startup,
    skills_install,
    supervisor_config,
    terminal_keybindings,
    wake_sidecar,
    windows_path_order,
)
from .messaging import agent_mail_extra
from .plugins import ensure_slack_plugin, ensure_template_plugins
from .settings_merge import (
    SettingsMergeError,
    load_settings_template,
    merge_settings_template,
)
from .docker_defaults import (
    DockerDefaultsError,
    apply_docker_defaults,
    plan_docker_defaults,
)
from .docker_roots import (
    DockerRootsError,
    apply_drop_ins,
    doctor_drop_ins,
    plan_docker_roots,
)
from .storage import (
    Runner,
    StorageApplyError,
    StorageDeclarationError,
    apply_storage,
    doctor_storage,
    resolve,
)

# ISSUE-REF Phase 5: the root holding docker/daemon.json and the runner its sudo -n write goes
# through; read at call time so the tests point both at a fixture.
ETC_ROOT = Path("/etc")
DOCKER_DEFAULTS_RUN: Runner = subprocess.run
# Phase 6: the runner `plan` reads `docker info` through (read-only); `apply` moves no root,
# and writes only the boot-order drop-ins through it once the roots are on the placement.
DOCKER_ROOTS_RUN: Runner = subprocess.run

_KINDS = {"symlink", "junction", "hardlink", "copy"}
_PROFILE_VAR_FILES = (".bashrc", ".zshenv", ".zshrc", ".bash_profile")
_PROFILE_HOST_FILES = (*_PROFILE_VAR_FILES, ".profile")
_GLOBAL_GIT_GUARDRAILS = (
    ("core.attributesFile", "master-roadmap.md merge=union", "attributes"),
    ("core.excludesFile", ".worktrees/", "ignore"),
    ("core.excludesFile", ".claude/worktrees/", "ignore"),
)


def _is_windows_shell(environ: dict[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    return values.get("MSYSTEM", "").upper().startswith(("MINGW", "MSYS", "CYGWIN"))


def _install_jq_wrapper(home: Path | None = None) -> Path | None:
    """Install the WinGet jq wrapper when jq is unavailable on Windows shell."""
    if not _is_windows_shell():
        return None
    target_home = Path.home() if home is None else home
    wrapper = target_home / ".local/bin/jq"
    # install.sh Step 9 created ~/.local/bin on every Windows run, before probing jq.
    try:
        wrapper.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ApplyError(f"could not create {wrapper.parent}: {exc}") from exc
    found = shutil.which("jq")
    if found:
        print(f"  [ok]  jq already in PATH: {found}")
        return None
    packages = target_home / "AppData/Local/Microsoft/WinGet/Packages"
    try:
        executable = next(packages.rglob("jq.exe"), None)
    except OSError as exc:
        raise ApplyError(f"could not search WinGet packages for jq: {exc}") from exc
    if executable is None:
        # jq is a hard dependency of statusline-command.sh, which parses its whole CC payload in
        # one `jq` call whose stderr is discarded. With jq absent that call emits nothing,
        # `mapfile` reads zero lines, and every payload-derived field silently becomes empty —
        # model name, context %, token counts, cost — while the statusline still exits 0 and
        # still renders. Measured on an affected Windows host:
        # `22:38 │  │ ai-harness:main` and `🪟 --%`; installing jq alone restored both.
        #
        # The advisory already existed and was still missed, because a `[warn]` on stdout reads
        # as install noise. Hence stderr + `[ERROR]` + the consequence named. Still no
        # auto-install: D-7/T-3.1 has the installer probe and declare, not install for the
        # operator.
        print(
            "  [ERROR] jq is NOT available — it is a hard dependency of the statusline.\n"
            "          Until it is installed the statusline renders blank model/context segments.\n"
            "          Install with: winget install jqlang.jq   — then re-run install.sh.",
            file=sys.stderr,
        )
        return None
    try:
        wrapper.write_text(
            f'#!/usr/bin/env bash\nexec "{executable}" "$@"\n', encoding="utf-8"
        )
        wrapper.chmod(0o755)
    except OSError as exc:
        raise ApplyError(f"could not install jq wrapper {wrapper}: {exc}") from exc
    print(f"  [ok]  jq wrapper → {wrapper} (→ {executable})")
    return wrapper


def _write_capability_records(
    state_root: Path, capabilities: tuple[Capability, ...]
) -> None:
    try:
        state_root.mkdir(parents=True, exist_ok=True)
        (state_root / "capabilities.json").write_text(
            json.dumps(records(capabilities), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ApplyError(f"could not record capability state: {exc}") from exc


def _capability_doctor_messages(
    capabilities: tuple[Capability, ...],
) -> tuple[str, ...]:
    """Report every resolved capability and its non-empty reason."""
    return tuple(
        f"capability {capability.id}: {capability.state}"
        f" ({capability.version})"
        f" — {capability.reason}; remediation: {capability.remediation}"
        for capability in capabilities
    )


def _gated_hooks(
    config: LoadedConfig, capabilities: tuple[Capability, ...]
) -> list[tuple[str, Capability]]:
    """Manifest hooks whose capability predicate resolved to anything but enabled."""
    manifest_path = config.root / "hooks-manifest.json"
    if not manifest_path.is_file():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    states = {capability.id: capability for capability in capabilities}
    hooks = manifest.get("hooks") if isinstance(manifest, dict) else None
    if not isinstance(hooks, list):
        return []
    gated = []
    for hook in hooks:
        if not isinstance(hook, dict) or not isinstance(hook.get("capability"), str):
            continue
        capability = states.get(hook["capability"])
        if capability is not None and capability.state != "enabled":
            gated.append((str(hook.get("name", "<unnamed>")), capability))
    return gated


def _hook_doctor_messages(
    config: LoadedConfig, capabilities: tuple[Capability, ...]
) -> tuple[str, ...]:
    """Explain manifest hooks omitted by a disabled capability predicate."""
    return tuple(
        f"hook {name}: not wired because capability {capability.id} is disabled"
        f" — {capability.reason}"
        for name, capability in _gated_hooks(config, capabilities)
    )


def _capability_unavailable_lines(
    config: LoadedConfig, capabilities: tuple[Capability, ...]
) -> list[str]:
    """What apply reports for an optional provider that is absent (machine-install contract).

    A declared capability whose provider executable is missing is reported and recorded, never
    fatal; only ``mode = "true"`` makes it required, and
    ``validate_required`` has already refused that case. Every step gated on it says so too.
    """
    lines = [
        f"capability unavailable: {capability.id} — {capability.reason};"
        f" remediation: {capability.remediation}"
        for capability in capabilities
        if capability.state != "enabled" and capability.requested != "false"
    ]
    lines.extend(
        f"hook {name}: capability unavailable: {capability.id} — {capability.reason};"
        " not wired, the run continues"
        for name, capability in _gated_hooks(config, capabilities)
    )
    return lines


class UnknownMachineError(ValueError):
    """``AI_HOST`` names no registered machine (machine-install contract: exit 2)."""

    exit_code = 2


def _require_registered_machine(config: LoadedConfig, machine: str | None) -> None:
    """Refuse an identity the registry does not declare, before anything mutates.

    The registered set is the ``[machine.<name>]`` tables (ISSUE-REF D-1 (d)) plus their declared
    aliases. A config that declares no machine tables has nothing to check against, so it passes.
    """
    tables = machine_tables(config.data)
    if not machine or not tables or machine in tables:
        return
    aliases = {
        alias
        for table in tables.values()
        for alias in (table.get("aliases") or ())
        if isinstance(alias, str)
    }
    if machine in aliases:
        return
    raise UnknownMachineError(
        f"unknown machine '{machine}'; registered machines: {', '.join(sorted(tables))}"
        f" (from {config.path})"
    )


def _storage_report(storage_step) -> list[str]:
    count = sum(len(outcome.actions) for outcome in storage_step.outcomes)
    return [*storage_step.lines, f"storage apply: {count} mutations"]


def _targets(values: list[str]) -> list[InstallTarget]:
    """Parse OUTPUT:PATH[:KIND] without splitting a Windows drive letter."""
    result = []
    for value in values:
        if ":" not in value:
            raise ApplyError(f"invalid target {value!r}; expected OUTPUT:PATH[:KIND]")
        output, tail = value.split(":", 1)
        target, separator, possible_kind = tail.rpartition(":")
        kind = possible_kind if separator and possible_kind in _KINDS else "symlink"
        path = (
            target
            if kind != "symlink"
            else (tail if not (separator and possible_kind in _KINDS) else target)
        )
        if not output or not path:
            raise ApplyError(f"invalid target {value!r}; expected OUTPUT:PATH[:KIND]")
        result.append(InstallTarget(output, Path(path).expanduser(), kind))
    return result


def _config_targets(config: LoadedConfig) -> list[InstallTarget]:
    targets = config.install.get("target", [])
    assert isinstance(targets, list)
    return [
        InstallTarget(
            str(item["output"]),
            _config_path(config, str(item["path"])),
            str(item["kind"]),
            str(item["consumer"]) if "consumer" in item else None,
        )
        for item in targets
        if isinstance(item, dict)
    ]


def _config_path(config: LoadedConfig, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else config.root / path


def _state_root(config: LoadedConfig | None, override: Path | None) -> Path:
    if override is not None:
        return override.expanduser()
    if config and "state_root" in config.install:
        return _config_path(config, str(config.install["state_root"]))
    return Path(user_state_path("ai-harness"))


def _ensure_context_install_dir(path: Path) -> None:
    """Create the context-install (projects-root) directory with mkdir -p semantics."""
    if path.exists():
        if path.is_dir():
            return
        raise ApplyError(f"context-install directory is not a directory: {path}")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ApplyError(
            f"could not create context-install directory {path}: {exc}"
        ) from exc


_PRUNED_CONTEXT_FILES = ("CLAUDE.md", "GEMINI.md", "AGENTS.md")


def _resolves_into(link: Path, tree: Path) -> bool:
    """install.sh's `[[ -L x ]]` then `[[ "$(readlink -f x)" == "$tree"/* ]]`.

    GNU `readlink -f` fails, leaving an empty target that matches nothing, when any component
    but the last is missing; a lexically resolved path under a missing directory is not a match.
    """
    if not link.is_symlink():
        return False
    try:
        target, base = link.resolve(), tree.resolve()
    except (OSError, RuntimeError):
        return False
    if not target.parent.is_dir():
        return False
    return target != base and target.is_relative_to(base)


def _same_directory(left: Path, right: Path) -> bool:
    """Bash `-ef`: both exist and share device and inode (case-insensitive filesystems too)."""
    try:
        a, b = left.stat(), right.stat()
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _has_direct_child_checkout(root: Path) -> bool:
    """install.sh's `for d in "$stale"/*/; do [[ -e "${d}.git" ]]`: one level, never a walk."""
    try:
        children = tuple(root.iterdir())
    except OSError:
        return False
    return any(
        not child.name.startswith(".") and child.is_dir() and (child / ".git").exists()
        for child in children
    )


def _prune_rule_links(root: Path, ours: Callable[[Path], bool]) -> int:
    """Unlink the `.claude/rules/*.md` symlinks under `root` that `ours` accepts; returns the count.

    Directories go through `rmdir` only, so a foreign file, an unmanaged link or a sibling
    `.claude` entry keeps its directory. Never the root itself: that is the caller's call.
    """
    rules = root / ".claude" / "rules"
    if rules.is_symlink() or not rules.is_dir():
        return 0
    pruned = 0
    for link in sorted(rules.glob("[!.]*.md")):
        if not ours(link):
            continue
        try:
            link.unlink()
        except OSError:
            continue
        pruned += 1
    for directory in (rules, root / ".claude"):
        try:
            directory.rmdir()
        except OSError:
            pass
    return pruned


def _prune_retired_live_rules(
    live_root: Path, source_tree: Path, *, generations: Path | None = None
) -> int:
    """ISSUE-REF: drop the retired `<projects root>/.claude/rules` copy at the LIVE root.

    The rules moved to `~/.claude/rules`, and the previous-root prune above returns at once for
    a root that did not move, so on that host the old copy stayed forever: a second set Claude
    reads when it walks up from a repo, which diverges the day a rule is added or removed.
    Only the rules are retired. The live root's context links are current, so they stay, and
    the checkout guard does not apply: a live root is full of repos by design.
    """

    def ours(link: Path) -> bool:
        return _resolves_into(link, source_tree / "rules") or (
            generations is not None and _resolves_into(link, generations)
        )

    pruned = _prune_rule_links(live_root, ours)
    if pruned:
        print(
            f"  [fixed] removed {pruned} retired rule link(s) from the live projects "
            f"root's .claude/rules: {live_root}"
        )
    return pruned


def _prune_stale_session_config(
    stale: Path, live_root: Path, source_tree: Path, *, generations: Path | None = None
) -> int:
    """install.sh's retired Step 3c for one candidate root; returns the links removed.

    Deliberately narrow, because this deletes under $HOME: only the three context-file basenames
    and `.claude/rules/*.md`, only symlinks, only ones resolving into this config tree (the rules
    into its `rules/`) or into this state root's ``generations`` (where the rendered links have
    pointed since ISSUE-REF D-11), never a root that is the live one or holds a git checkout as a
    direct child, and directories only through `rmdir`. Every refusal is a silent skip.
    """
    if not stale.is_dir() or _same_directory(stale, live_root):
        return 0
    if _has_direct_child_checkout(stale):
        return 0

    def ours(link: Path, tree: Path) -> bool:
        return _resolves_into(link, tree) or (
            generations is not None and _resolves_into(link, generations)
        )

    pruned = 0
    for name in _PRUNED_CONTEXT_FILES:
        link = stale / name
        if not ours(link, source_tree):
            continue
        try:
            link.unlink()
        except OSError:
            continue
        pruned += 1
    pruned += _prune_rule_links(stale, lambda link: ours(link, source_tree / "rules"))
    try:
        stale.rmdir()
    except OSError:
        pass
    if pruned:
        print(
            f"  [prune] removed {pruned} stale session-config link(s) from a previous "
            f"projects root: {stale}"
        )
    return pruned


def _hygiene(config: LoadedConfig) -> tuple[int, Path | None, Path | None, int]:
    hygiene = config.hygiene
    if not hygiene:
        return 0o700, None, None, 1
    return (
        int(str(hygiene["generation_mode"]), 8),
        _config_path(config, str(hygiene["credential_patterns"])),
        _config_path(config, str(hygiene["credential_allowlist"])),
        int(hygiene["retain_previous"]),
    )


def _validate_hygiene(config: LoadedConfig) -> None:
    """Validate required scan inputs without creating a generation."""
    _mode, patterns, allowlist, _retain = _hygiene(config)
    if (
        patterns is None
        or allowlist is None
        or not patterns.is_file()
        or not allowlist.is_file()
    ):
        raise ApplyError("credential patterns and non-empty allowlist are required")
    for path, label in (
        (patterns, "credential patterns"),
        (allowlist, "credential allowlist"),
    ):
        if not any(
            line.strip() and not line.startswith("#")
            for line in path.read_text(encoding="utf-8").splitlines()
        ):
            raise ApplyError(f"{label} must be non-empty")


def _migrate_shell_profiles(config: LoadedConfig) -> None:
    """Run the fixed variable and config-driven host-value migrations, reporting each one."""
    home = Path.home()
    for name in _PROFILE_VAR_FILES:
        if migrate_profile_var(home / name, "AI_CLI_HOST", "AI_HOST"):
            print(f"  [migrated] AI_CLI_HOST → AI_HOST in {home / name}")
    configured = config.install["profile_host_renames"]
    assert isinstance(configured, dict)
    for name in _PROFILE_HOST_FILES:
        for old, new in configured.items():
            if migrate_profile_host_value(home / name, ((old, new),)):
                print(
                    f"  [migrated] AI_HOST={old} → {new} in {home / name} (renamed profile)"
                )


def _global_git_file(key: str, fallback_name: str) -> Path:
    """Return the configured global Git file, ignoring an unavailable lookup."""
    try:
        result = subprocess.run(
            ["git", "config", "--global", "--get", key],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).expanduser()
    # `${XDG_CONFIG_HOME:-$HOME/.config}`: an empty value is unset, never a relative path.
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "git" / fallback_name


def _ensure_global_git_line(path: Path, line: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if line not in existing.splitlines():
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")
    except OSError as exc:
        raise ApplyError(
            f"could not update global Git guardrail {path}: {exc}"
        ) from exc


def _install_global_git_guardrails() -> None:
    """Install the opt-in global attributes and ignore guardrails idempotently."""
    attributes = _global_git_file("core.attributesFile", "attributes")
    excludes = _global_git_file("core.excludesFile", "ignore")
    for key, line, _fallback in _GLOBAL_GIT_GUARDRAILS:
        _ensure_global_git_line(
            attributes if key == "core.attributesFile" else excludes, line
        )
    try:
        subprocess.run(
            ["git", "config", "--global", "core.attributesFile", str(attributes)],
            check=True,
        )
        subprocess.run(
            ["git", "config", "--global", "core.excludesFile", str(excludes)],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ApplyError(f"could not configure global Git guardrails: {exc}") from exc
    print("  [ok]  global git config: roadmap merge=union + worktree dirs ignored")


def _mark_projects_trusted(projects_root: Path | None) -> int:
    """install.sh's retired trust tail: trust each project and worktree in ``~/.claude.json``."""
    trust_store = Path.home() / ".claude.json"
    if not trust_store.is_file():
        return 0
    if projects_root is None or not projects_root.is_dir():
        print(f"  [skip] trust: projects root {projects_root} does not exist")
        return 0
    try:
        data = json.loads(trust_store.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("top-level JSON value must be an object")
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            raise TypeError("projects value must be an object")
        candidates = []
        for project in sorted(projects_root.iterdir()):
            if not project.is_dir() or project.name.startswith("."):
                continue
            candidates.append(project)
            worktrees = project / ".worktrees"
            if worktrees.is_dir():
                candidates.extend(
                    path for path in sorted(worktrees.iterdir()) if path.is_dir()
                )
        added = 0
        for path in candidates:
            entry = projects.setdefault(str(path), {})
            if not isinstance(entry, dict):
                raise TypeError(f"project entry is not an object: {path}")
            if not entry.get("hasTrustDialogAccepted"):
                entry["hasTrustDialogAccepted"] = True
                added += 1
        if added:
            trust_store.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApplyError(
            f"could not update Claude trust store {trust_store}: {exc}"
        ) from exc
    print(f"  [ok]  trust: {added} path(s) added to ~/.claude.json")
    return added


def _install_channel() -> tuple[str, Path | None]:
    """Return registry or source and its checkout path at the packaging boundary."""
    try:
        dist = distribution("ai-chassis")
    except PackageNotFoundError as exc:
        raise ApplyError("installation channel could not be determined") from exc
    direct_url = dist.read_text("direct_url.json")
    if direct_url:
        url = json.loads(direct_url).get("url")
        if isinstance(url, str) and url.startswith("file:"):
            parsed = urlparse(url)
            return "source", Path(unquote(parsed.path))
    return "registry", None


def _update() -> None:
    channel, source = _install_channel()
    if channel == "registry":
        print("uv tool upgrade ai-chassis")
        return
    if source is None:
        raise ApplyError("installation channel could not be determined")
    try:
        subprocess.run(
            ["git", "-C", str(source), "pull", "--ff-only"],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ApplyError(f"source checkout update failed: {source}: {exc}") from exc
    print(f"updated source checkout: {source}")


def _plan_mutations(state_root: Path, targets: list[InstallTarget]) -> list[str]:
    mutations = [
        f"create generation directory under {state_root / 'generations'}",
        "write staged manifest",
    ]
    mutations.extend(
        f"reconcile {target.output} -> {target.path} ({target.requested_kind})"
        for target in targets
    )
    if (state_root / "current").exists():
        mutations.append("swap previous pointer")
    mutations.extend(
        [
            "swap current pointer",
            "write committed manifest",
            "delete retained generation directories",
        ]
    )
    return mutations


def _docker_defaults_lines(config: LoadedConfig, machine: str | None) -> list[str]:
    """D-10 (a): merge the profile's opted-in daemon log default; runs after the storage step.
    Then the boot order: once the roots run on the docker_root placement, both daemons'
    systemd drop-ins require its mount."""
    declaration = resolve(config.data, machine) if machine else None
    try:
        result = apply_docker_defaults(declaration, ETC_ROOT, DOCKER_DEFAULTS_RUN)
        return [*result.lines, *apply_drop_ins(declaration, ETC_ROOT, DOCKER_ROOTS_RUN)]
    except (DockerDefaultsError, DockerRootsError) as exc:
        raise ApplyError(str(exc)) from exc


def _boot_order_doctor(config: LoadedConfig) -> tuple[list[str], bool]:
    """The docker boot-order state; a storage table the reader rejects is doctor_storage's."""
    machine = os.environ.get("AI_HOST")
    if not machine:
        return [], True
    try:
        declaration = resolve(config.data, machine)
    except StorageDeclarationError:
        return [], True
    return doctor_drop_ins(declaration, ETC_ROOT)


def _projects_root_layout_doctor(
    config: LoadedConfig, projects_root: Path
) -> list[str]:
    """Warning lines for every projects-root entry that is not an allowed shape (ISSUE-REF).

    Warning-level by design: the lines print, but they never turn doctor's exit code red, since
    the fix is a person moving or listing a directory, not a repair doctor can make. A checkout
    that ships no contract skips the check; an unreadable root is said, never read as clean.
    """
    from ai_chassis import projects_root_layout

    contract_path = config.root.parent / "config" / "projects-root-layout.toml"
    if not contract_path.is_file():
        return []
    try:
        entries = projects_root_layout.audit(
            projects_root, projects_root_layout.load_contract(contract_path)
        )
    except projects_root_layout.LayoutError as exc:
        return [f"projects-root layout: not checked ({exc})"]
    return projects_root_layout.finding_lines(entries)


def _buzz_allow_list_doctor() -> tuple[list[str], bool]:
    """Each paired host's daemon that this host's Buzz allow-list would refuse (ISSUE-REF)."""
    from ai_chassis import attribution_buzz

    machine = os.environ.get("AI_HOST")
    if not machine:
        return [], True
    try:
        findings = attribution_buzz.allow_list_findings(machine)
    except Exception as exc:  # noqa: BLE001 - an unreadable config is a reported state
        return [
            f"{attribution_buzz.ALLOW_LIST_FINDING} not checked ({type(exc).__name__})"
        ], True
    return findings, not findings


def _shell_rc_credential_doctor() -> tuple[list[str], bool]:
    """Credential-named exports in the shell rc files, names only (ISSUE-REF)."""
    from ai_chassis import shell_rc_credentials

    home = os.environ.get("HOME")
    if not home:
        return [], True
    lines = shell_rc_credentials.findings(Path(home))
    return lines, not lines


def _init(root: Path) -> None:
    path = root / "ai-harness.toml"
    if path.exists():
        raise ApplyError(f"refusing to overwrite existing config: {path}")
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "config_schema_version = 1\n\n# [install]\n# [[install.target]]\n# [hygiene]\n",
        encoding="utf-8",
    )
    print(path)


def _canonical_machine(config: LoadedConfig, machine: str | None) -> str | None:
    """The registry machine ``machine`` names, through its declared ``aliases``.

    install.sh's Step 3 map turned the renamed ``example-project`` into ``example-project-sagemaker`` before any
    settings step saw it; a name no table or alias declares is returned unchanged.
    """
    if not machine:
        return machine
    tables = machine_tables(config.data)
    if machine in tables:
        return machine
    for name, table in tables.items():
        aliases = table.get("aliases") or ()
        if isinstance(aliases, list) and machine in aliases:
            return name
    return machine


# ISSUE-REF: on this one profile the settings merge forces the mouse suppressors ABSENT, so Claude
# Code keeps mouse capture and copies over OSC 52; a stale `1` from the reversed ISSUE-REF pin would
# otherwise survive "user wins" and kill both wheel-scroll and copy-on-select. Machine-scoped
# because a suppressor hand-added elsewhere may be deliberate. The evidence for the reversal is in
# tests/test_settings_machine_scoped_env.py's docstring, which pins the scope both ways.
_MOUSE_CAPTURE_MACHINES = frozenset({"example-project-sagemaker"})
_MOUSE_SUPPRESSOR_ENV_KEYS = (
    "CLAUDE_CODE_DISABLE_MOUSE",
    "CLAUDE_CODE_DISABLE_MOUSE_CLICKS",
)


def _merge_settings(
    machine: str | None,
    fragment_path: Path | None = None,
    overlay_path: Path | None = None,
) -> None:
    """install.sh's retired Step 4: the template merge into ~/.claude/settings.json."""
    merge_settings_template(
        overlay_path=overlay_path,
        fragment_path=fragment_path,
        scrub_env_keys=(
            _MOUSE_SUPPRESSOR_ENV_KEYS if machine in _MOUSE_CAPTURE_MACHINES else ()
        ),
        out=sys.stdout,
    )


def _apply_session_model(root: Path, machine: str | None) -> None:
    """install.sh's retired Step 4d: the session-model writer over the merged settings (ISSUE-REF).

    ``root`` is the session config (``<checkout>/projects-wide-session-config``), so the policy
    is ``<checkout>/config/session-model.toml`` beside the route table, and the resolver is
    ``<checkout>/.claude/bin/model-catalog`` unless ``$AI_HARNESS_ROOT`` names another checkout,
    exactly as ``scripts/session_model_install.py`` derives them. A root without the policy skips.
    A refused config leaves the settings as the merge wrote them and does not abort the install.
    """
    config_path = root.parent / "config" / "session-model.toml"
    checkout = config_path.resolve().parent.parent
    if not machine or not config_path.is_file():
        print(f"  [skip] session model: no machine or no {config_path}")
        return
    route_table = config_path.with_name("model-catalog-routes.toml")
    harness_root = Path(os.environ.get("AI_HARNESS_ROOT") or checkout)
    request = session_model.Request(
        settings_path=Path.home() / ".claude" / "settings.json",
        machine=machine,
        config_path=config_path,
        route_table_path=route_table,
        registry_path=root / "ai-harness.toml",
        state_dir=session_model.default_state_dir(),
    )
    seams = session_model.Seams(
        resolver=session_model.command_resolver(
            [str(harness_root / ".claude" / "bin" / "model-catalog")],
            route_table=route_table,
            machine=machine,
        ),
        prober=session_model.command_prober(None),
        out=sys.stdout,
    )
    try:
        session_model.apply(request, seams)
    except session_model.SessionModelError as exc:
        print(
            f"  [FAIL] session model: {exc}; settings left unchanged", file=sys.stderr
        )
        print(
            "  [WARN] session model step refused; ~/.claude/settings.json left as Step 4b "
            "wrote it",
            file=sys.stderr,
        )


@dataclass
class _StepContext:
    config: LoadedConfig
    machine: str | None
    state_root: Path
    projects_root: Path
    secret_handoff_skip_reported: bool = False


def _projects_link_step(context: _StepContext) -> int:
    """install.sh's header-less ``~/projects`` link (IN-127, ISSUE-REF): ``~/projects`` points at a
    projects root that lives elsewhere, so a committed ``/workspace/projects pointer resolves. A real
    directory there is reported and left alone; every arm exits 0, as the shell block never failed."""
    projects_link.ensure(context.projects_root, Path.home())
    return 0


def _main_branch_check_step(context: _StepContext) -> int:
    """install.sh's header-less main-branch warning (IN-128): the live global config tracks
    whatever branch the main tree has checked out, so a branch other than ``main`` is warned
    about. It only reads, and exits 0 on every arm, as the shell block could not fail."""
    branch = rendered_links.checked_out_branch(_main_tree_checkout(context))
    if branch is not None and branch != "main":
        print(
            f"  [warn] ai-harness main tree is on branch '{branch}', not 'main' — global "
            "config will track it. Checkout main for canonical config."
        )
    return 0


def _context_dir_step(context: _StepContext) -> int:
    """install.sh's retired Step 2: ensure the projects root exists, and say which it is."""
    _ensure_context_install_dir(context.projects_root)
    print(f"  [ok]  projects root: {context.projects_root}")
    return 0


def _zsh_step(_context: _StepContext) -> int:
    """install.sh's retired Step 1d: zsh, which ``ai c`` hardcodes; skips and warns, never fails."""
    dependency_bootstrap.ensure_zsh()
    return 0


def _gitleaks_step(_context: _StepContext) -> int:
    """install.sh's retired Step 1e: a gitleaks that can run the founding-ask gate; never fails."""
    dependency_bootstrap.ensure_gitleaks(Path.home())
    return 0


def _doppler_step(_context: _StepContext) -> int:
    """install.sh's retired Step 1f: the Doppler CLI, installed without privilege; never fails."""
    dependency_bootstrap.ensure_doppler(Path.home())
    return 0


def _gh_step(_context: _StepContext) -> int:
    """install.sh's retired Step 1f4: this user's gh at the latest release; never fails."""
    dependency_bootstrap.ensure_gh(Path.home())
    return 0


def _jq_step(_context: _StepContext) -> int:
    """A jq of at least 1.7.1 on PATH, which firstmate needs (ISSUE-REF); never fails."""
    dependency_bootstrap.ensure_jq(Path.home())
    return 0


def _durable_tmux_step(context: _StepContext) -> int:
    """install.sh's retired Step 1g: the newest durable tmux master, copied into ``~/.local/bin``.

    The builder it runs for a machine with no master is the MAIN tree's, as the shell's
    ``SCRIPT_DIR`` was.
    """
    durable_tmux.provision(
        context.projects_root, Path.home(), _main_tree_checkout(context)
    )
    return 0


def _real_git_step(_context: _StepContext) -> int:
    """Write ~/.local/bin/git past the macOS xcrun shim (ISSUE-REF); never install-failing."""
    line = real_git.ensure_real_git_wrapper(Path.home())
    if line:
        print(line, file=sys.stderr if "[warn]" in line else sys.stdout)
    return 0


def _aido_config_step(context: _StepContext) -> int:
    return aido_config.apply_config(
        context.projects_root,
        context.config.root.parent / "config" / "aido-install.toml",
    )


def _python_shim_step(_context: _StepContext) -> int:
    """install.sh's ``install-python-shim`` region (IN-131): the fleet floor interpreter, the
    ``~/.local/bin`` shim when there is none, and the answer the shell kept in ``_PYTHON_BIN``.

    The answer goes back as a shell-only assignment, as ``_bedrock_api_auth_enabled`` does, so
    install.sh's later readers see it; in a full apply ``AIH_PYTHON_BIN`` reads it (_MODE_VALUES).
    Every arm warns and continues, as the shell block did.
    """
    try:
        word = python_shim.ensure(Path.home())
    except OSError as exc:
        print(f"  [warn] python3 shim not provisioned: {exc}", file=sys.stderr)
        return 1
    target = os.environ.get(EXPORTS_FILE_ENV)
    if target:
        line = (
            f'{python_shim.EXPORT_NAME}="{word}"'
            if word == python_shim.HOME_SHIM
            else f"{python_shim.EXPORT_NAME}={shlex.quote(word)}"
        )
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    return 0


def _context_supervisor_config_step(context: _StepContext) -> int:
    """install.sh's context-supervisor block (IN-132, ISSUE-REF): seed or migrate the config.

    The interpreter is the python-shim step's answer, handed over as ``AIH_PYTHON_BIN`` (empty
    when it found none, so the defaults are seeded create-only); outside install.sh and the full
    apply, an unset one is this process's own. The machine profile is install.sh's ``MACHINE``,
    else the canonical name of this machine. A failure stops the install, as the shell's did.
    """
    machine = os.environ.get("MACHINE")
    if machine is None:
        machine = _canonical_machine(context.config, context.machine) or ""
    return supervisor_config.provision(
        Path.home(),
        _main_tree_checkout(context),
        os.environ.get("AIH_PYTHON_BIN", sys.executable),
        machine,
    )


def _idle_wake_config_step(_context: _StepContext) -> int:
    """Seed the live automatic wake policy OFF once, without restarting a service."""
    try:
        created = idle_wake.seed_policy()
    except (OSError, ValueError):
        print("  [FAIL] could not seed idle-wake policy", file=sys.stderr)
        return 1
    print(
        f"  [ok]  {idle_wake.policy_path()} ({'created OFF' if created else 'preserved'})"
    )
    return 0


def _claude_readiness_step(_context: _StepContext) -> int:
    """install.sh's Claude Code readiness check (IN-134, ISSUE-REF): launchable and configured.

    The interpreter gate reads the python-shim step's answer in ``AIH_PYTHON_BIN`` (an unset one
    is this process's own); whether this machine needs the Bedrock route is the bedrock-auth
    step's hand-back in ``AIH_BEDROCK_API_AUTH_ENABLED``, which only install.sh and the full
    apply set. Every arm only reports, so the step exits 0.
    """
    claude_readiness.check(
        Path.home(),
        os.environ.get("AIH_PYTHON_BIN", sys.executable),
        os.environ.get(install_sequence.BEDROCK_AUTH_ENV),
        os.environ.get("AI_HOST") or "unknown",
    )
    return 0


def _compact_wake_readiness_step(context: _StepContext) -> int:
    """install.sh's auto-compact / auto-wake readiness check (IN-135, ISSUE-REF).

    It reads the MAIN tree's hooks manifest, as the hooks-manifest step writes from it, and probes
    that checkout's own interpreter; the interpreter gate reads the python-shim step's answer in
    ``AIH_PYTHON_BIN`` (an unset one is this process's own). Every arm only reports, a hook that
    cannot run as a ``[FAIL]`` line, so the step exits 0.
    """
    config_root = rendered_links.main_tree_config_root(context.config.root)
    compact_wake_readiness.check(
        config_root / "hooks-manifest.json",
        Path.home() / ".claude" / "settings.json",
        config_root.parent,
        os.environ.get("AIH_PYTHON_BIN", sys.executable),
        os.environ.get("AI_HOST") or "unknown",
    )
    return 0


def _ai_core_config_step(context: _StepContext) -> int:
    return core_requirements.activate(
        _main_tree_checkout(context), context.projects_root / "ai-core"
    )


def _plugins_step(context: _StepContext) -> int:
    """Install the plugins the settings template enables (ISSUE-REF, install.sh Step 4a0).

    Third-party skills arrive as their upstream Claude Code plugin, never a copy under skills/.
    It runs BEFORE the settings merge because `claude plugin marketplace add` rewrites its
    settings entry without `autoUpdate`, and the merge restores it. A failure is deferred by
    install.sh, never fatal.

    It then re-applies config/plugin-skill-overrides.toml to the cached plugin versions (ISSUE-REF),
    whether or not `claude` is present, because a plugin update drops the patched frontmatter.

    The core MCP registration (`core-mcp`, selectable on its own) runs last, in process: it is
    plugin installation too, and riding this install.sh hand-off keeps install.sh from growing
    (config/install-ratchet.toml) and ahead of restart-mcp's certification.

    The checkout's config/settings-overlay.json, when present, adds the plugins its own fleet
    enables to the packaged template's, for this step and the settings merge alike.
    """
    try:
        profile = _machine_profile(context)
    except SettingsMergeError as exc:
        print(
            f"  [FAIL] plugins: not installed, since the machine profile cannot say which it "
            f"disables: {exc}",
            file=sys.stderr,
        )
        installed = 1
    else:
        installed = ensure_template_plugins(
            load_settings_template(_settings_overlay(context)), profile
        )
    slack = _slack_plugin_step(context)
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "plugin_skill_overrides.py"
    )
    try:
        overrides = subprocess.run(
            [sys.executable, str(script)], check=False, timeout=60
        ).returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  [FAIL] plugin skill overrides: {exc!r}", file=sys.stderr)
        overrides = 1
    return installed or overrides or _core_mcp_run("apply", context) or slack


def _slack_plugin_step(context: _StepContext) -> int:
    machine = _canonical_machine(context.config, context.machine)
    policy = machine_tables(context.config.data).get(machine, {})
    return ensure_slack_plugin(policy, context.projects_root)


def _settings_step(context: _StepContext) -> int:
    """install.sh's retired Step 4; the machine fragment is the next step, machine-settings."""
    _merge_settings(
        _canonical_machine(context.config, context.machine),
        overlay_path=_settings_overlay(context),
    )
    return 0


def _machine_profile(context: _StepContext) -> dict[str, object]:
    """This machine's declared settings fragment, parsed; ``{}`` when it declares none.

    The plugins step reads it for the plugins a profile disables. It needs no registry: a
    config without one, or a machine it does not name, declares no fragment.
    """
    registry = context.config.registry
    canonical = (
        canonicalize(registry, context.machine)
        if registry is not None and context.machine
        else None
    )
    if canonical is None:
        return {}
    declared = str(registry.machines[canonical].get("settings_fragment") or "")
    if not declared:
        return {}
    return read_machine_fragment(_config_path(context.config, declared))


def _settings_overlay(context: _StepContext) -> Path:
    """The checkout's settings-template overlay; a missing file adds nothing."""
    return _main_tree_checkout(context) / "config" / "settings-overlay.json"


def _core_mcp_runtime(root: Path):
    """Select installed metadata and interpreter, re-rooting editable source installs."""
    from hashlib import sha256
    from .mcp.client_install import ArtifactError, Runtime

    channel, source = _install_channel()
    if channel == "source" and source is not None:
        source = rendered_links.main_tree_config_root(
            source / "projects-wide-session-config"
        ).parent
        prefix = (source / ".venv").resolve()
        paths = [
            prefix / "Lib/site-packages",
            *prefix.glob("lib/python*/site-packages"),
        ]
        installed = {
            dist.metadata["Name"].replace("_", "-"): dist
            for dist in distributions(path=[str(path) for path in paths])
        }
        package, sdk = installed.get("ai-chassis"), installed.get("mcp")
        revision = rendered_links.source_commit(source)
        # An editable install's metadata keeps the version it was installed at, so a host
        # that has not re-synced since a version bump reports a stale one (0.0.0 on the Mac).
        # The code it runs is this checkout, so the checkout's own pyproject is authoritative.
        try:
            version = tomllib.loads((source / "pyproject.toml").read_text("utf-8"))[
                "project"
            ]["version"]
        except (OSError, KeyError, tomllib.TOMLDecodeError):
            version = None
    else:
        prefix = Path(sys.prefix).absolute()
        package, sdk = distribution("ai-chassis"), distribution("mcp")
        record = package.read_text("RECORD")
        revision = (
            "wheel-sha256:" + sha256(record.encode()).hexdigest() if record else None
        )
        version = None
    interpreter = prefix / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    absent = [
        name
        for name, missing in (
            ("package", package is None),
            ("sdk", sdk is None),
            ("revision", not revision),
            ("interpreter", not interpreter.is_file()),
        )
        if missing
    ]
    if absent:
        # Names only: which prerequisite failed is what tells a transient provisioning
        # window from a wrong runtime, and none of it is a path, value or secret.
        raise ArtifactError(
            f"Installed core MCP runtime unavailable (channel {channel}; missing {', '.join(absent)})"
        )
    direct = json.loads(package.read_text("direct_url.json") or "{}")
    if ".worktrees" in str(direct):
        raise ArtifactError("Installed core MCP runtime is transient")
    return Runtime(
        str(interpreter),
        str(root.resolve()),
        version or package.version,
        revision,
        sdk.version,
    )


def _core_mcp_project_preflight(root: Path) -> None:
    """The registration library delegates project/local policy discovery to its caller."""
    from .mcp.client_install import ArtifactError
    from .restart_mcp import _scopes

    for cwd in dict.fromkeys((root.parent, Path.cwd())):
        for scope in _scopes(Path.home(), cwd):
            if (
                not isinstance(scope, dict)
                or not isinstance(scope.get("mcpServers", {}), dict)
                or "ai-harness-core" in scope.get("mcpServers", {})
            ):
                raise ArtifactError("Core MCP project registration conflict")


def _core_mcp_run(action: str, context: _StepContext) -> int:
    """Dispatch the installer-only library with explicit, canonical client paths."""
    from .mcp import client_install as core

    root = rendered_links.main_tree_config_root(context.config.root).resolve()
    config = load_config(root)
    settings = config.data.get("mcp", {})
    enabled = settings.get("enabled", True)
    clients = settings.get("clients", ["claude", "codex"])
    required = settings.get("required_clients", [])
    # Resolve the host's own layout here, at the installer boundary: on example-project-ec2
    # `~/.claude` is a symlink onto a data volume, and the library refuses any path
    # with a symlink in it (ISSUE-REF). Its guard still covers every path below these.
    state = context.state_root.resolve() / "core-mcp"
    claude = Path(
        os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
    ).resolve()
    codex = (
        Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()
        / "config.toml"
    )
    try:
        if "claude" in clients:
            _core_mcp_project_preflight(root)
        runtime = _core_mcp_runtime(root)
        binaries = {client: shutil.which(client) for client in clients}
        if action == "plan":
            artifacts = core.render_client_artifacts(runtime, settings)
            generation = (
                state
                / "artifacts"
                / (
                    runtime.package_version
                    + "-"
                    + core._digest(core._json(artifacts.manifest))
                )
            )
            plan = core.plan_client_install(runtime, settings, generation, codex)
            print(
                f"core-mcp plan: {sum(c.state != 'unchanged' for c in plan.changes)} intended mutations"
            )
            # Registration ownership is also a plan precondition, with no client launch.
            if "claude" in clients:
                core._preflight_claude(
                    claude, core._registration_json(state / "registration.json"), state
                )
            return plan.exit_code
        artifacts = core.render_client_artifacts(runtime, settings)
        if action == "apply":
            binary = binaries.get("claude")
            measured = Path(binary).resolve().name if binary else None
            result = core.apply_client_registration(
                artifacts,
                state,
                claude,
                codex,
                clients=clients,
                enabled=enabled,
                claude_binary=binary,
                codex_binary=binaries.get("codex"),
                claude_version=measured,
                claude_config_dir=claude,
                required_clients=tuple(required),
            )
        else:
            present = [client for client in clients if binaries[client] or not enabled]
            result = core.doctor_client_registration(
                artifacts, state, claude, codex, clients=present, enabled=enabled
            )
            missing = tuple(
                core.RegistrationCheck(
                    client,
                    "client_missing" if client in required else "skipped",
                    client not in required,
                )
                for client in clients
                if client not in present
            )
            result = core.RegistrationResult((*result.checks, *missing))
        for check in result.checks:
            detail = f" ({check.detail})" if check.detail else ""
            print(f"core-mcp {check.client}: {check.code}{detail}")
        return result.exit_code
    except (
        core.ArtifactError,
        AttributeError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        PackageNotFoundError,
    ) as exc:
        # An ArtifactError message is written to be safe; any other exception names only its class.
        cause = str(exc) if isinstance(exc, core.ArtifactError) else type(exc).__name__
        print(
            f"core-mcp: registration or installed runtime unavailable; refused ({cause})"
        )
        return 1


def _core_mcp_step(context: _StepContext) -> int:
    return _core_mcp_run("apply", context)


def _session_model_step(context: _StepContext) -> int:
    """install.sh's retired Step 4d, for the canonical machine Step 3 resolved."""
    _apply_session_model(
        context.config.root, _canonical_machine(context.config, context.machine)
    )
    return 0


def _profile_migration_step(context: _StepContext) -> int:
    """install.sh's retired Steps 1a and 1a2: migrate the AI_CLI_HOST name and renamed values."""
    _migrate_shell_profiles(context.config)
    return 0


def _helper_path_step(context: _StepContext) -> int:
    """install.sh's retired Steps 5c and 5c1, behind the ``install.modify_shell_profile`` opt-in."""
    if not context.config.install["modify_shell_profile"]:
        print("  [skip] helper PATH: install.modify_shell_profile is false")
        return 0
    persist_helper_bin_path()
    return 0


def _jq_wrapper_step(_context: _StepContext) -> int:
    """install.sh's retired Step 9: the Windows-only jq wrapper in ~/.local/bin."""
    _install_jq_wrapper()
    return 0


def _capabilities_step(context: _StepContext) -> int:
    """install.sh's retired Steps 11 and 11b, public half: the registry's probe-and-declare report.

    Records every capability's state and reports each requested provider that is absent; a
    required one refuses with its remediation. It never installs anything for the operator
    (runtime-composition IN-56/IN-74, D-7): the first-time installs are the private
    optional-cli-install step, which runs just before this one so the record sees its result.
    """
    capabilities = resolve_capabilities(
        context.config.capabilities, machine=context.machine
    )
    validate_required(capabilities)
    _write_capability_records(context.state_root, capabilities)
    for line in _capability_unavailable_lines(context.config, capabilities):
        print(line)
    return 0


def _git_guardrails_step(context: _StepContext) -> int:
    """install.sh's retired Step 13b, behind the ``install.modify_global_git`` opt-in."""
    if not context.config.install["modify_global_git"]:
        print("  [skip] global git config: install.modify_global_git is false")
        return 0
    _install_global_git_guardrails()
    return 0


def _prune_stale_root_step(context: _StepContext) -> int:
    """install.sh's retired Step 3c: the declared previous root, then the shell's two defaults."""
    home = Path.home()
    declared = context.config.previous_projects_root
    _prune_retired_live_rules(
        context.projects_root,
        context.config.root,
        generations=context.state_root / "generations",
    )
    for candidate in (
        *(() if declared is None else (declared,)),
        home / "projects",
        home / "Projects",
    ):
        _prune_stale_session_config(
            candidate,
            context.projects_root,
            context.config.root,
            generations=context.state_root / "generations",
        )
    return 0


def _trust_projects_step(context: _StepContext) -> int:
    """install.sh's retired trust tail, behind the ``install.mark_projects_trusted`` opt-in."""
    if not context.config.install["mark_projects_trusted"]:
        print("  [skip] trust: install.mark_projects_trusted is false")
        return 0
    _mark_projects_trusted(context.projects_root)
    return 0


# --- install.sh Steps 1b, 1b2, 1b (billing) and 4b: the registry is the one machine table --------

#: The file install.sh's `_aih_apply_step` sources after a step: each `export NAME=value` line a
#: step appends there reaches the installer's own environment, the way Step 1b's `export` did.
EXPORTS_FILE_ENV = "AI_HARNESS_EXPORTS_FILE"
#: For each profile an environment-supplied AI_HOST may be persisted to, the files whose line
#: already declares it for the shell that reads that profile: a bash login reads ``.profile`` or
#: ``.bash_profile`` beside ``.bashrc``, and the non-interactive zsh ``ai c`` runs reads only
#: ``.zshenv`` (ISSUE-REF), so an AI_HOST in ``.bashrc`` alone never reached a session.
_IDENTITY_PROFILE_FILES = {
    ".bashrc": (".bashrc", ".bash_profile", ".profile"),
    ".zshenv": (".zshenv",),
}
_API_BILLING_REASON = "API-billed backend detected"
_SUBSCRIPTION_BILLING_REASON = "OAuth/subscription backend detected"


def _export_to_caller(name: str, value: str, *, home_relative: bool = False) -> None:
    """Set ``name`` for the rest of this run and for the installer that launched it.

    A ``home_relative`` value (``$HOME/...``) is expanded here and left for the installer's own
    shell to expand, so each side sees its own ``$HOME``.
    """
    os.environ[name] = (
        str(Path.home()) + value.removeprefix("$HOME") if home_relative else value
    )
    target = os.environ.get(EXPORTS_FILE_ENV)
    if target:
        append_export(Path(target), name, value, home_relative=home_relative)


def _required_registry(config: LoadedConfig, step: str) -> Registry:
    registry = config.registry
    if registry is None:
        raise ApplyError(
            f"{step} reads the [machine.<name>] registry, and {config.path} declares no "
            "[machine_schema]"
        )
    return registry


def _identity_remedy(config: LoadedConfig, names: Sequence[str]) -> list[str]:
    """install.sh Step 1b's remedy block, with the profiles read from the registry."""
    return [
        "",
        "          Fix it either way round:",
        "            1. Durable, survives a rebuilt $HOME (preferred):",
        "                 sudo mkdir -p /etc/ai-harness",
        "                 echo <profile> | sudo tee /etc/ai-harness/machine",
        "            2. This invocation only:",
        "                 AI_HOST=<profile> bash install.sh",
        "",
        f"          Known profiles: {' '.join(names)}",
        "          A genuinely new machine wants its OWN profile: a [machine.<name>] table with",
        f"          its identity checks in {config.path}.",
    ]


def _fatal(lines: Sequence[str]) -> int:
    print("\n".join(lines), file=sys.stderr)
    return 1


def _unknown_identity(config: LoadedConfig, value: str, origin: str) -> int:
    """Step 1b's ``[FATAL]`` block for a name no ``[machine.<name>]`` table or alias declares.

    The second line is the machine-install contract's unknown-machine message, and the exit is
    its unknown-machine code (2): nothing has been persisted or exported.
    """
    if value == "hetzner":
        _fatal(["  [FATAL] Hetzner is archived and no longer a supported profile."])
        return UnknownMachineError.exit_code
    names = sorted(machine_tables(config.data))
    _fatal(
        [
            f"  [FATAL] {origin} names '{value}', which is no registered profile, so no "
            "profile will be installed.",
            f"          unknown machine '{value}'; registered machines: {', '.join(names)}"
            f" (from {config.path})",
            *_identity_remedy(config, names),
        ]
    )
    return UnknownMachineError.exit_code


def _persist_supplied_identity(value: str) -> None:
    """Step 1b2: an AI_HOST that came from the environment, persisted to each profile target
    whose shell no profile declares it for."""
    home = Path.home()

    def declared(name: str) -> bool:
        path = home / name
        try:
            return path.is_file() and "AI_HOST" in path.read_text(encoding="utf-8")
        except OSError:
            return False

    missing = tuple(
        target
        for target in profile_targets(home, os.environ)
        if not any(declared(name) for name in _IDENTITY_PROFILE_FILES[target.name])
    )
    if not missing:
        return
    persist_exported_var("AI_HOST", value, targets=missing)
    print(
        f"  [fixed] AI_HOST={value} persisted — it came from the environment and no shell\n"
        "          profile declared it. Without this the next shell has no machine identity."
    )


def _host_identity_step(context: _StepContext) -> int:
    """install.sh's retired Steps 1b and 1b2: resolve this machine and persist ``AI_HOST``.

    ``AI_HOST`` (a registry name or alias) wins without probing; else the marker file; else the
    registry's identity checks, in Step 1b's platform order. Zero or several matches, an empty
    marker, or a name the registry does not declare refuse the install with Step 1b's
    ``[FATAL]`` wording; nothing is persisted and no default profile is ever chosen.
    """
    config = context.config
    supplied = os.environ.get("AI_HOST", "").strip()
    if supplied:
        # A name or alias is accepted without probing, and the CANONICAL name is what is exported
        # and persisted: install.sh's Step 3 map has an arm for every registry name but not for
        # every alias, so an uncanonicalised alias would pass here and be refused there.
        tables = machine_tables(config.data)
        canonical = _canonical_machine(config, supplied) or supplied
        if tables and canonical not in tables:
            return _unknown_identity(config, supplied, "AI_HOST")
        alias = f" (alias {supplied})" if canonical != supplied else ""
        print(f"  [ok]  AI_HOST={canonical}{alias}")
        _persist_supplied_identity(canonical)
        _export_to_caller("AI_HOST", canonical)
        context.machine = canonical
        return 0
    registry = _required_registry(config, "host-identity")
    try:
        name, source = resolve_machine_identity(machine_specs(registry.machines))
    except EmptyMarkerError as exc:
        return _fatal(
            [
                f"  [FATAL] {exc.path} exists but is empty.",
                "          Write the machine profile name into it, e.g.:",
                f"              echo <profile> | sudo tee {exc.path}",
            ]
        )
    except AmbiguousMachineError as exc:
        return _fatal(
            [
                "  [FATAL] Cannot identify this machine: more than one profile's identity "
                f"check matched ({', '.join(exc.candidates)}), so no profile will be installed.",
                f"          {exc}",
                *_identity_remedy(config, registry.names()),
            ]
        )
    except MachineDetectionError as exc:
        return _fatal(
            [
                "  [FATAL] Cannot identify this machine, so no profile will be installed.",
                f"          {exc}.",
                "          Refusing to fall back to a default profile: applying another machine's",
                "          profile is worse than applying none.",
                *_identity_remedy(config, registry.names()),
            ]
        )
    canonical = canonicalize(registry, name)
    if canonical is None:
        return _unknown_identity(config, name, source.removeprefix("marker "))
    origin = (
        source.removeprefix("marker ")
        if source.startswith("marker ")
        else f"the [machine.{canonical}] identity checks"
    )
    print(f"  [note] identity from {origin}")
    persist_exported_var("AI_HOST", canonical)
    _export_to_caller("AI_HOST", canonical)
    print(f"  [set] AI_HOST={canonical}")
    context.machine = canonical
    return 0


def _machine_attributes(context: _StepContext, step: str) -> tuple[str, Mapping]:
    """The canonical machine and its registry attributes, for a step that acts on them."""
    registry = _required_registry(context.config, step)
    if not context.machine:
        raise ApplyError(
            f"{step} needs this machine's identity: AI_HOST is unset; run "
            "`ai-harness apply --only host-identity` first"
        )
    canonical = canonicalize(registry, context.machine)
    if canonical is None:
        raise UnknownMachineError(
            f"unknown machine '{context.machine}'; registered machines: "
            f"{', '.join(registry.names())} (from {context.config.path})"
        )
    return canonical, registry.machines[canonical]


def _settings_use_bedrock(path: Path) -> bool:
    """Step 1b's backend signal: ``env.CLAUDE_CODE_USE_BEDROCK`` is 1 or true in settings.json.

    An absent or unreadable file is no signal, as the shell's jq read was.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    env = data.get("env") if isinstance(data, dict) else None
    value = env.get("CLAUDE_CODE_USE_BEDROCK") if isinstance(env, dict) else None
    return value is True or (type(value) in (int, str) and str(value) in ("1", "true"))


def _billing_mode_step(context: _StepContext) -> int:
    """install.sh's retired ``Step 1b: Set CC_BILLING_MODE``.

    ``api`` when settings.json selects the Bedrock backend or the machine's registry table
    declares ``billing_mode = "api"``, else ``subscription``; persisted by the converging writer,
    because the value is machine-managed and a stale ``api`` would keep API-only behaviour on.
    """
    _, attributes = _machine_attributes(context, "billing-mode")
    if (
        _settings_use_bedrock(Path.home() / ".claude" / "settings.json")
        or attributes.get("billing_mode") == "api"
    ):
        mode, reason = "api", _API_BILLING_REASON
    else:
        mode, reason = "subscription", _SUBSCRIPTION_BILLING_REASON
    persist_managed_exported_var("CC_BILLING_MODE", mode)
    _export_to_caller("CC_BILLING_MODE", mode)
    print(f"  [set] CC_BILLING_MODE={mode} ({reason})")
    return 0


def _shell_profile_opt_in(context: _StepContext, label: str) -> bool:
    """The ``install.modify_shell_profile`` gate the profile-writing steps share (D-C11, D-C13)."""
    if context.config.install["modify_shell_profile"]:
        return True
    print(f"  [skip] {label}: install.modify_shell_profile is false")
    return False


def _machine_id_step(context: _StepContext) -> int:
    """install.sh's retired Steps 1b3 and 1b3a: provision ``AI_MACHINE_ID`` and mirror it.

    Later installer steps read ``$AI_MACHINE_ID`` in-process, so the effective value is exported
    to the caller on both the generated and the already-provisioned path. A conflict writes and
    exports nothing; every config-file failure is a warning, as in the shell. Behind the
    ``install.modify_shell_profile`` opt-in (D-C13).
    """
    if not _shell_profile_opt_in(context, "AI_MACHINE_ID"):
        return 0
    home = Path.home()
    value = machine_id.provision(home, os.environ)
    if value is None:
        return 0
    _export_to_caller("AI_MACHINE_ID", value)
    machine_id.write_config(value, home, os.environ)
    return 0


def _xdg_state_home_step(context: _StepContext) -> int:
    """install.sh's retired Step 1bb: repair a SET-BUT-EMPTY ``XDG_STATE_HOME`` (ISSUE-REF).

    Claude Code joins its lock directory onto ``XDG_STATE_HOME ?? ~/.local/state``, and JS ``??``
    keeps an empty string, so every session wrote ``claude/locks/`` into its cwd. Only that state
    is repaired: an unset variable already takes Claude Code's own fallback, and a real path is
    the user's. The profile keeps the literal ``$HOME/.local/state`` so a rebuilt home stays right.
    Behind the ``install.modify_shell_profile`` opt-in (D-C13).
    """
    if not _shell_profile_opt_in(context, "XDG_STATE_HOME repair"):
        return 0
    current = os.environ.get("XDG_STATE_HOME")
    if current == "":
        persist_managed_exported_var(
            "XDG_STATE_HOME", "$HOME/.local/state", home_relative=True
        )
        _export_to_caller("XDG_STATE_HOME", "$HOME/.local/state", home_relative=True)
        print(
            "  [set] XDG_STATE_HOME=$HOME/.local/state (was set-but-empty; CC would write "
            "claude/locks/ into each session's cwd)"
        )
    elif current is None:
        print(
            "  [ok]  XDG_STATE_HOME unset — CC's own $HOME/.local/state fallback applies"
        )
    else:
        print("  [ok]  XDG_STATE_HOME set to an explicit path — left alone")
    return 0


def _windows_path_order_step(context: _StepContext) -> int:
    """install.sh's retired Step 1c (IN-15): ``~/.local/bin`` ahead of the Windows ``python3`` stub.

    Windows shells only. It writes the persistent User PATH, so it sits behind the
    ``install.modify_shell_profile`` opt-in, as the other profile and Windows user-environment
    writers do; the write is queued in the run's one batched ``cmd.exe``.
    """
    if not profile_is_windows_shell(os.environ):
        return 0
    if not _shell_profile_opt_in(context, "Windows PATH order"):
        return 0
    return windows_path_order.fix_path_order(Path.home(), os.environ)


def _terminal_keybindings_step(_context: _StepContext) -> int:
    """install.sh's retired Step 10 (IN-39): the Shift+Enter -> newline binding in each terminal.

    Windows shells only: the first Windows Terminal ``settings.json`` and VS Code
    ``keybindings.json`` under the home directory gain the binding once (add-only, repairing a
    drifted escape sequence). A config that cannot be read, parsed or written fails the step, as
    the shell's unguarded script call stopped the install.
    """
    try:
        return terminal_keybindings.install(Path.home(), os.environ)
    except terminal_keybindings.TerminalKeybindingsError as exc:
        raise ApplyError(f"terminal-keybindings: {exc}") from exc


def _main_tree_checkout(context: _StepContext) -> Path:
    """The repository checkout in the MAIN working tree, where every published link points."""
    return rendered_links.main_tree_config_root(context.config.root).parent


def _statusline_links_step(context: _StepContext) -> int:
    """install.sh's retired Step 5: the statusline command and its usage-status config."""
    link_publication.publish_statusline(
        _main_tree_checkout(context), Path.home(), os.environ
    )
    return 0


def _helper_bins_step(context: _StepContext) -> int:
    """install.sh's retired Step 5b: every ``.claude/bin`` helper into ``~/.claude/bin``."""
    link_publication.publish_helper_bins(
        _main_tree_checkout(context),
        Path.home(),
        os.environ,
        excluded=_secret_handoff_excluded(context),
    )
    return 0


def _tier_b_entrypoints_step(context: _StepContext) -> int:
    """install.sh's retired Step 5bb: ``install.tier_b_entrypoints`` into ``~/.local/bin``."""
    link_publication.publish_tier_b_entrypoints(
        _main_tree_checkout(context),
        Path.home(),
        list(context.config.install["tier_b_entrypoints"]),
        os.environ,
    )
    return 0


def _wrapper_publication_step(context: _StepContext) -> int:
    """install.sh's retired Step 5bb2: every bare-named wrapper into ``~/.local/bin``."""
    link_publication.publish_bare_wrappers(
        _main_tree_checkout(context),
        Path.home(),
        os.environ,
        excluded=_secret_handoff_excluded(context),
    )
    return 0


def _secret_handoff_excluded(context: _StepContext) -> frozenset[str]:
    from .secret_handoff.models import HandoffError
    from .secret_handoff.planning import profile_enabled
    from .secret_handoff.launch import DISABLED

    # There is nothing to filter when this helper is absent from the source sweep.
    if not (_main_tree_checkout(context) / ".claude/bin/secret-handoff").is_file():
        return frozenset()
    try:
        enabled = context.config.registry is not None and profile_enabled(
            context.config.registry, context.config.data, context.machine
        )
    except HandoffError as exc:
        raise ApplyError(str(exc)) from None
    if enabled:
        return frozenset()
    if not context.secret_handoff_skip_reported:
        print(DISABLED)
        context.secret_handoff_skip_reported = True
    return frozenset({"secret-handoff"})


def _mdb_wrapper_step(context: _StepContext) -> int:
    """install.sh's retired Step 5bb2a: the mdb wrapper over the example-client-host kit's moved-aside link."""
    link_publication.wrap_vendored_mdb(
        _main_tree_checkout(context), Path.home(), os.environ
    )
    return 0


def _hook_links_step(context: _StepContext) -> int:
    """install.sh's retired Step 7: every hook file into ``~/.claude/hooks``, from the MAIN tree.

    Under Git Bash the junction is queued and the step returns 0, so install.sh sources the
    hand-back line; only the batched flush can fail it.
    """
    hook_links.install(_main_tree_checkout(context), Path.home(), os.environ)
    return 0


def _hooks_manifest_step(context: _StepContext) -> int:
    """install.sh's retired Step 7b (IN-33): wire the MAIN tree's hooks manifest for this machine.

    It writes the managed-hook block of ``~/.claude/settings.json`` and the checkout's
    ``.claude/settings.local.json``, and nothing else. install.sh holds the settings lock around
    it by position, and skips it when the settings merge refused; a missing manifest is the
    shell's ``[WARN]`` and exit 0, and any other failure stops the install at its hand-off.
    """
    config_root = rendered_links.main_tree_config_root(context.config.root)
    claude_dir = Path.home() / ".claude"
    try:
        report = hooks_manifest.apply(
            machine=_canonical_machine(context.config, context.machine) or "",
            manifest_path=config_root / "hooks-manifest.json",
            user_settings=claude_dir / "settings.json",
            local_settings=config_root.parent / ".claude" / "settings.local.json",
            user_hooks_dir=(claude_dir / "hooks").as_posix(),
        )
    except hooks_manifest.HooksManifestError as exc:
        raise ApplyError(str(exc)) from exc
    print(report)
    return 0


def _skills_layout(context: _StepContext) -> skills_install.Layout:
    """The MAIN-tree checkout every skills link points into (D-11), and its registry file."""
    config_root = rendered_links.main_tree_config_root(context.config.root)
    return skills_install.Layout(
        checkout=config_root.parent,
        registry=config_root / skills_install.REGISTRY_NAME,
        profile=context.machine or "",
        projects_dir=context.projects_root,
        home=Path.home(),
    )


def _skills_settings_env_step(context: _StepContext) -> int:
    """install.sh's Step 8 ``claude-settings`` env switches (IN-34), apart from the mounts.

    They write ``~/.claude/settings.json``, so install.sh hands them off inside its settings
    lock, after the hooks manifest; the shell wrote them after the lock was released
    (ISSUE-REF). A write that fails stops the install at its hand-off.
    """
    skills_install.apply_settings_env(_skills_layout(context))
    return 0


def _skills_step(context: _StepContext) -> int:
    """install.sh's retired Steps 8 and 8b (IN-34): both skills mounts in the profile's mode.

    The profile is ``AI_HOST`` as install.sh passed it; an unlisted one, or a registry that
    cannot be read, gets whole-tree. Shell-profile and Windows user-environment switches sit
    behind ``install.modify_shell_profile``. A link that cannot be made stops the install at its
    hand-off; under Git Bash every junction is queued and the step exits 0, so install.sh sources
    the hand-back lines. It requires no particular skill: that policy is the private
    installer's (IN-77), checked by install.sh after the hand-off.
    """
    skills_install.install(
        _skills_layout(context),
        os.environ,
        modify_shell_profile=context.config.install["modify_shell_profile"],
    )
    return 0


def _shared_agents_step(context: _StepContext) -> int:
    """install.sh's retired Step 5ba (shared agents): the config's agents into ~/.claude/agents."""
    shared_agents.install(
        rendered_links.main_tree_config_root(context.config.root),
        Path.home(),
        os.environ,
    )
    return 0


def _node_toolchain_step(_context: _StepContext) -> int:
    """install.sh's retired ``_aih_ensure_npm`` glue: provision Node and npm where that needs no
    password, handing PATH back to install.sh; nothing under ``--update``; never fails."""
    node_toolchain.provision()
    return 0


def _npm_dev_dependencies_step(context: _StepContext) -> int:
    """install.sh's retired Step 11e: the MAIN-tree checkout's own npm devDependencies."""
    node_toolchain.install_repo_dependencies(_main_tree_checkout(context))
    return 0


def _presentation_toolchain_step(context: _StepContext) -> int:
    """install.sh's retired Step 11f: the presentation skill's own npm toolchain and Chromium."""
    node_toolchain.install_presentation_toolchain(_main_tree_checkout(context))
    return 0


def _codex_step(context: _StepContext) -> int:
    """install.sh's retired Step 5c (Codex CLI integration): ``codex/`` into ``~/.codex``."""
    codex_integration.install(
        _main_tree_checkout(context), Path.home(), context.projects_root, os.environ
    )
    return 0


def _pi_wrapper_step(context: _StepContext) -> int:
    """The end of install.sh's retired Step 5cc: the pinned ``~/.claude/bin/pi`` wrapper.

    Runs after the private ``pi-bootstrap`` step, so a pi it installed in the same run is
    published, and extends this process's PATH so the steps after it find that pi.
    """
    pi_integration.publish_wrapper(
        pi_integration.PiMachine(
            _canonical_machine(context.config, context.machine) or "",
            context.projects_root.parent,
            list(context.config.install["pi_persistent_prefix_machines"]),
        ),
        Path.home(),
        os.environ,
    )
    return 0


def _pi_settings_step(context: _StepContext) -> int:
    """install.sh's retired Step 5d (pi CLI integration): render pi's settings, check its auth.

    The template is the canonical machine's, through the registry aliases, so a box still exporting
    a renamed spelling renders its own settings.
    """
    pi_integration.install_settings(
        _main_tree_checkout(context),
        Path.home(),
        _canonical_machine(context.config, context.machine) or "",
        os.environ,
    )
    return 0


def _pi_wq_link_step(context: _StepContext) -> int:
    """The end of install.sh's retired Step 5f: the work-queue extension link, first-time only."""
    pi_integration.install_work_queue(
        _main_tree_checkout(context), Path.home(), os.environ
    )
    return 0


def _retire_wrappers_step(_context: _StepContext) -> int:
    """install.sh's retired Step 5bc: remove dangling links to wrappers this repo renamed away."""
    retired_wrappers.retire_wrappers(Path.home())
    return 0


def _terminal_mode_reset_step(context: _StepContext) -> int:
    """install.sh's retired Step 10b: the shell-startup snippets, linked from the MAIN tree.

    Behind the ``install.modify_shell_profile`` opt-in (D-C13).
    """
    if not _shell_profile_opt_in(context, "terminal mode reset"):
        return 0
    checkout = rendered_links.main_tree_config_root(context.config.root).parent
    shell_startup.install_terminal_mode_reset(checkout, Path.home(), os.environ)
    return 0


def _aws_profiles(checkout: Path) -> Callable[[], tuple[str, frozenset[str]]]:
    """The live AWS SSO profile and every declared one, from the checkout's aws_profile_cli.py.

    install.sh read both from that script at call time (ISSUE-REF); an absent script or a
    failing call answers "nothing resolved", which drops a placeholder rather than writing it.
    """

    def ask() -> tuple[str, frozenset[str]]:
        script = checkout / "scripts" / "aws_profile_cli.py"
        if not script.is_file():
            return "", frozenset()
        answers = []
        for verb in ("name", "list"):
            try:
                result = subprocess.run(
                    [sys.executable, str(script), verb],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired):
                return "", frozenset()
            answers.append(result.stdout if result.returncode == 0 else "")
        name, listed = answers
        return name.strip(), frozenset(
            line.strip() for line in listed.splitlines() if line.strip()
        )

    return ask


def _machine_settings_step(context: _StepContext) -> int:
    """install.sh's retired Step 4b: merge the machine's declared settings fragment.

    The fragment is the registry's ``settings_fragment`` (relative to the config root); ``""``
    declares none and is the normal skip. A declared fragment that is missing fails naming the
    path and the table, where the shell's filename convention could only skip it.
    """
    canonical, attributes = _machine_attributes(context, "machine-settings")
    declared = str(attributes.get("settings_fragment") or "")
    if not declared:
        print(
            f"  [skip] no machine settings fragment for machine={canonical} "
            f"([machine.{canonical}] settings_fragment is empty)"
        )
        return 0
    fragment = _config_path(context.config, declared)
    if not fragment.is_file():
        raise ApplyError(
            f"machine settings fragment {fragment} does not exist; [machine.{canonical}] "
            f"settings_fragment = {declared!r} in {context.config.path} declares it"
        )
    claude_dir = Path.home() / ".claude"
    checkout = Path(
        os.environ.get("AI_HARNESS_ROOT") or context.config.root.resolve().parent
    )
    merge_machine_fragment(
        claude_dir / "settings.json",
        fragment,
        substitutions=(
            ("CLAUDE_HOOKS_DIR", str(claude_dir / "hooks")),
            ("CLAUDE_DIR", str(claude_dir)),
        ),
        aws_profiles=_aws_profiles(checkout),
    )
    print(f"  [ok]  machine settings fragment: {fragment.name}")
    return 0


def _private_step(name: str) -> Callable[[_StepContext], int]:
    """A step only the private orchestrator performs (runtime-composition D-5 (c), T-3.5).

    The public core carries the step's position and nothing of its behaviour: it hands the step
    to ``[orchestrator].entrypoint`` with the terminal inherited, since a private step may prompt,
    and returns that exit code. With no orchestrator configured, or one naming a missing file,
    the step is reported not applicable and nothing is mutated (T-3.5 AC2).
    """

    def run(context: _StepContext, *, adopt: bool = False) -> int:
        entrypoint = _orchestrator_entrypoint(context.config)
        if entrypoint is None:
            print(f"{name}: not applicable (no private orchestrator configured)")
            return 0
        # The step registers its deferrals and manual steps in this run's two ledgers; outside
        # install.sh this process owns them, so the child must inherit their paths to be reported.
        deferred_failures.ledger_path(
            owner=deferred_failures.owner_of(context.config.install)
        )
        manual_steps.ledger_path()
        argv = [
            sys.executable,
            str(entrypoint),
            name,
            "--config-root",
            str(context.config.root),
            "--projects-root",
            str(context.projects_root),
            "--machine",
            context.machine or "",
        ]
        if adopt and not os.environ.get(EXPORTS_FILE_ENV):
            return _run_adopting_exports(context, name, argv)
        # Standalone, the child hands its queued Windows-native ops back instead of launching
        # cmd.exe itself, so this process's one flush (main) carries them (IN-07, ISSUE-REF).
        # Under install.sh the exports file already does this; a nested CLI inherits its
        # caller's hand-back file.
        handback: Path | None = None
        child_env: dict[str, str] | None = None
        if not os.environ.get(EXPORTS_FILE_ENV) and not os.environ.get(
            NATIVE_OPS_FILE_ENV
        ):
            descriptor, handback_name = tempfile.mkstemp(
                prefix="ai-harness-native-ops-", suffix=".sh"
            )
            os.close(descriptor)
            handback = Path(handback_name)
            child_env = {**os.environ, NATIVE_OPS_FILE_ENV: handback_name}
        try:
            code = subprocess.run(argv, env=child_env, check=False).returncode
            if handback is not None:
                adopt_handed_back_ops(handback)
        finally:
            if handback is not None:
                handback.unlink(missing_ok=True)
        return code

    run.private_name = name  # type: ignore[attr-defined]
    return run


#: The shell-only variables private steps handed back in this full apply, which install.sh's own
#: later lines read and no step's process inherits (bedrock-auth's `_bedrock_api_auth_enabled`);
#: the sequencer passes one on only where a hand-off line did. Cleared when the sequence starts.
_HANDED_BACK_SHELL: dict[str, str] = {}


def _run_adopting_exports(context: _StepContext, name: str, argv: list[str]) -> int:
    """A private step in a full apply outside install.sh (ISSUE-REF T6-0b, ruling 2).

    The child writes its own exports file, as under install.sh, and once it exits 0 this process
    adopts it the way `_aih_apply_step`'s `. "$exports"` would: each export into the environment
    every later step inherits, each Windows-native op into this run's one queue. A non-zero exit
    discards the file, as install.sh does. A line the grammar refuses adopts nothing from the file
    and defers the step, naming the line by number only: an exported value may be a credential, so
    no value is ever printed.
    """
    descriptor, exports_name = tempfile.mkstemp(
        prefix="ai-harness-exports-", suffix=".sh"
    )
    os.close(descriptor)
    exports = Path(exports_name)
    try:
        code = subprocess.run(
            argv, env={**os.environ, EXPORTS_FILE_ENV: exports_name}, check=False
        ).returncode
        if code:
            return code
        try:
            handed = read_handed_back(exports, os.environ)
        except HandBackRefused as refused:
            _defer_refused_hand_back(context, name, refused.line)
            return 0
    finally:
        exports.unlink(missing_ok=True)
    os.environ.update(handed.exports)
    _HANDED_BACK_SHELL.update(handed.shell_only)
    for op in handed.ops:
        queue_handed_back_op(op)
    return 0


def _defer_refused_hand_back(context: _StepContext, name: str, line: int) -> None:
    summary = (
        f"{name}: line {line} of its exports hand-back is neither an export nor a "
        "Windows-native op, so nothing it handed back was adopted"
    )
    action = "The step's own output above names what it set; a later step may lack it."
    try:
        deferred_failures.register(
            deferred_failures.DeferredFailure(
                failure_class="self",
                summary=summary,
                actions=[action],
                source_step=name,
            ),
            owner=deferred_failures.owner_of(context.config.install),
        )
    except OSError as exc:
        print(
            f"  [deferred] {summary}\n             [warn] not recorded ({exc})",
            file=sys.stderr,
        )


def _windows_native_flush_step(_context: _StepContext) -> int:
    """install.sh Step 12b, in a full apply: run the Windows-native ops queued so far (junctions,
    ``setx``) in one ``cmd.exe`` batch, before skills-canary needs the skills junction
    (ISSUE-REF). Off Windows, or with nothing queued, it does nothing. Under install.sh
    (``AI_HARNESS_EXPORTS_FILE`` set) its own ``_flush_win_cmd`` owns Step 12b until T6-5, so this
    does nothing either; ``main``'s final flush still carries anything queued later.
    """
    if os.environ.get(EXPORTS_FILE_ENV):
        return 0
    queued = pending_windows_ops()
    if not queued:
        return 0
    if os.environ.get(NATIVE_OPS_FILE_ENV):
        flush_windows_user_env(None)
        return 0
    print(
        f"  [win] applying {queued} native op(s) (junctions/env) in one cmd.exe call..."
    )
    flush_windows_user_env(None)
    print("  [ok]  Windows native ops applied (single cmd.exe)")
    return 0


def _orchestrator_entrypoint(config: LoadedConfig) -> Path | None:
    """``[orchestrator].entrypoint`` when it names a file, else ``None`` (T-3.5 AC2)."""
    table = config.data.get("orchestrator")
    if not isinstance(table, dict) or "entrypoint" not in table:
        return None
    entrypoint = _config_path(config, str(table["entrypoint"]))
    return entrypoint if entrypoint.is_file() else None


def _current_manifest(state_root: Path) -> dict[str, object] | None:
    current = pointer(state_root, "current")
    if not current:
        return None
    try:
        return read_json(manifest_path(state_root, current))
    except (FileNotFoundError, ValueError) as exc:
        raise ApplyError(
            f"current generation manifest is missing or corrupt: {current}"
        ) from exc


def _managed_paths(manifest: dict[str, object] | None) -> set[str]:
    entries = manifest.get("targets", []) if manifest else []
    assert isinstance(entries, list)
    return {
        str(entry["path"])
        for entry in entries
        if isinstance(entry, dict) and "path" in entry
    }


def _generation_inputs(
    context: _StepContext,
    static_outputs: dict[str, bytes],
    static_targets: list[InstallTarget],
) -> tuple[dict[str, bytes], list[InstallTarget], rendered_links.RenderedSet | None]:
    """The one generation's outputs and targets: declared static rows plus this machine's set."""
    rendered = rendered_links.resolve(
        context.config, context.machine, context.projects_root, Path.home()
    )
    # Only what a target installs enters the generation: the render also produces every other
    # machine's context file, and none of those belongs on this machine (D-11 (c)).
    outputs = {
        target.output: static_outputs[target.output]
        for target in static_targets
        if target.output in static_outputs
    }
    targets = list(static_targets)
    if rendered is not None:
        clash = sorted(set(outputs) & set(rendered.outputs))
        if clash:
            raise ApplyError(
                "[[install.target]] and [install.rendered] both render "
                + ", ".join(clash)
            )
        outputs.update(rendered.outputs)
        targets.extend(rendered.targets)
    seen: set[str] = set()
    for target in targets:
        path = str(resolve_install_path(target.path))
        if path in seen:
            raise ApplyError(f"two install targets name the same path: {path}")
        seen.add(path)
    return outputs, targets, rendered


def _remove_stale_links(
    state_root: Path,
    prior: dict[str, object] | None,
    targets: list[InstallTarget],
    rendered: rendered_links.RenderedSet | None,
) -> None:
    """Retire links the new generation no longer renders (a removed rule, a disabled agent)."""
    kept = {str(resolve_install_path(target.path)) for target in targets}
    generations = (state_root / "generations").resolve()
    for path_text in sorted(_managed_paths(prior) - kept):
        path = Path(path_text)
        if path.is_symlink() and path.resolve().is_relative_to(generations):
            path.unlink()
            print(f"  [removed] {path}: no longer rendered for this machine")
    if rendered is None or rendered.gemini_enabled is not False:
        return
    gemini = next(
        target.path.with_name("GEMINI.md")
        for target in rendered.targets
        if target.output == "CLAUDE.md"
    )
    print(f"  [skip] GEMINI.md: gemini is not enabled for {rendered.machine}")
    print(
        "         (ISSUE-REF D-10; re-enable in projects-wide-session-config/ai-harness.toml)"
    )
    # A symlink left from an earlier install would dangle once the artifact stops being compiled,
    # so remove exactly that: a link into a generation or into the compiled config files, never a
    # real file someone wrote and never a link someone else made.
    if not gemini.is_symlink():
        return
    resolved = gemini.resolve()
    if resolved.is_relative_to(generations) or resolved.is_relative_to(
        rendered.source_root.resolve()
    ):
        gemini.unlink()
        print("         removed the stale GEMINI.md symlink from a previous install")
    else:
        print(
            f"  [skip] {gemini} links to {os.readlink(gemini)}, which this installer did not "
            "make; left in place"
        )


def _report_rendered(rendered: rendered_links.RenderedSet, generation: str) -> None:
    commit = f" at {rendered.source_commit[:12]}" if rendered.source_commit else ""
    where = f"generation {generation} (rendered from {rendered.source_root}{commit})"
    names = {
        "CLAUDE.md": f"CLAUDE.{rendered.machine}.md",
        "GEMINI.md": f"GEMINI.{rendered.machine}.md",
    }
    for target in rendered.targets:
        if not target.output.startswith("rules/"):
            source = names.get(target.output, target.output)
            print(f"  [ok]  {target.path} → {source} in {where}")
    if rendered.rules:
        print(
            f"  [ok]  {Path.home() / '.claude' / 'rules'}/ ← {len(rendered.rules)} rule(s) "
            f"from {rendered.rules_dir} in {where}; their relative links point at the "
            "main tree"
        )


def _quarantine_foreign_files(
    context: _StepContext,
    rendered: rendered_links.RenderedSet,
    prior: dict[str, object] | None,
    managed: set[str],
) -> list[tuple[Path, Path]]:
    """A regular file at a rendered target this installer never wrote is kept, not replaced.

    A file whose bytes equal the rendered output (a shell-era copy or hardlink) is not drift: it
    is left for apply to replace with the link. Returns each (live path, quarantine path) moved,
    so the caller can put them back if the transaction fails.
    """
    label = str(prior["generation_id"]) if prior else "before-first-generation"
    moved: list[tuple[Path, Path]] = []
    for target in rendered.targets:
        path = target.path
        if str(resolve_install_path(path)) in managed or path.is_symlink():
            continue
        if not path.is_file():
            continue
        if path.read_bytes() == rendered.outputs[target.output]:
            print(
                f"  [ok]  {path}: identical to the rendered copy; replacing it with the link"
            )
            continue
        destination = quarantine(context.state_root, label, path)
        assert destination is not None
        moved.append((path, destination))
        print(
            f"  [QUARANTINE] {path} was a file this installer did not write; it is now "
            f"at {destination}; linking {path} to the rendered generation",
            file=sys.stderr,
        )
    return moved


def _restore_quarantined(moved: list[tuple[Path, Path]]) -> None:
    """Undo ``_quarantine_foreign_files`` after a failed transaction: the live file returns."""
    for path, destination in reversed(moved):
        if path.exists() or path.is_symlink():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(destination), str(path))
        print(f"  [restored] {path} from {destination}; the install did not complete")


def _install_generation(
    context: _StepContext,
    static_outputs: dict[str, bytes],
    static_targets: list[InstallTarget],
    *,
    force: bool = False,
) -> str:
    """Render, check and swap the one generation every rendered single file links to.

    Shared by the ``rendered-links`` step (install.sh's retired Steps 3, 3b and 6) and the full
    apply, so both install the same set in one transaction (ISSUE-REF D-11 (a)). Every refusal
    (a rule collision, an unknown machine) happens before anything is mutated.
    """
    outputs, targets, rendered = _generation_inputs(
        context, static_outputs, static_targets
    )
    if not targets:
        raise ApplyError(
            "no install targets resolved; declare [[install.target]] or pass --target"
        )
    _validate_hygiene(context.config)
    mode, patterns, allowlist, retain = _hygiene(context.config)
    prior = _current_manifest(context.state_root)
    managed = _managed_paths(prior)
    if rendered is not None:
        for line in rendered.lines:
            print(line)
        rendered_links.check_rule_collisions(
            rendered, context.projects_root, Path.home(), context.state_root, managed
        )
    moved = (
        _quarantine_foreign_files(context, rendered, prior, managed)
        if rendered is not None
        else []
    )
    try:
        generation = apply(
            outputs,
            targets,
            context.state_root,
            force=force,
            restrictive_mode=mode,
            patterns=patterns,
            allowlist=allowlist,
            retain_previous=retain,
            metadata=(
                {
                    "source_commit": rendered.source_commit,
                    "source_root": str(rendered.source_root),
                }
                if rendered is not None
                else None
            ),
            reuse_unchanged=True,
        )
    except (ApplyError, LinkError, OSError):
        _restore_quarantined(moved)
        raise
    _remove_stale_links(context.state_root, prior, targets, rendered)
    if rendered is not None:
        _report_rendered(rendered, generation)
    return generation


def _rendered_links_step(context: _StepContext) -> int:
    """install.sh's retired Steps 3, 3b and 6: this machine's rendered single files."""
    if rendered_links.declaration(context.config) is None:
        print("rendered-links: not applicable (no [install.rendered] declared)")
        return 0
    _install_generation(context, {}, [])
    return 0


def _generation_doctor_lines(
    config: LoadedConfig, state_root: Path
) -> tuple[list[str], int]:
    """D-11 (a): whether the installed generation was rendered from the main tree's HEAD."""
    if rendered_links.declaration(config) is None:
        return [], 0
    manifest = _current_manifest(state_root)
    if manifest is None:
        return ["generation: none installed; run ai-harness apply"], 1
    rendered_from = manifest.get("source_commit")
    source_root = rendered_links.main_tree_config_root(config.root)
    head = rendered_links.source_commit(source_root)
    if not isinstance(rendered_from, str) or head is None:
        return ["generation: source commit unknown (not rendered from a checkout)"], 0
    if rendered_from == head:
        return [f"generation: rendered from main at {head[:12]}"], 0
    if rendered_links.is_ancestor(source_root, rendered_from, head):
        return [f"generation behind main ({rendered_from[:12]} < {head[:12]})"], 1
    return [
        f"generation not rendered from main's history ({rendered_from[:12]} vs {head[:12]})"
    ], 1


def _boot_check_step(context: _StepContext) -> int:
    """Install the once-per-boot verification unit (ISSUE-REF), born extracted.

    The checkout is the resolved config root's parent: a ``--config`` symlinked to
    ``<checkout>/projects-wide-session-config`` still finds ``<checkout>/systemd`` (ISSUE-REF).
    """
    return boot_check.install_unit(
        context.config.root.resolve().parent,
        context.machine or os.environ.get("AI_HOST", ""),
    )


def _claude_daemon_step(context: _StepContext) -> int:
    return claude_daemon_service.install_unit(context.config.root.resolve().parent)


def _canonical_readme_sync_step(context: _StepContext) -> int:
    """Fallback README copies in consumer repos (D-4); a failure is a manual step, never red."""
    return canonical_readme_sync.sync(
        harness=context.config.root.resolve().parent,
        projects_root=context.projects_root,
        host=context.machine or os.environ.get("AI_HOST", ""),
    )


def _manual_steps_report_step(_context: _StepContext) -> int:
    """The install's last step: every manual step this run registered, as one section (D-5)."""
    manual_steps.report(sys.stdout)
    return 0


def _manual_step_add(args: argparse.Namespace) -> int:
    """``ai-harness manual-step add``: the install.sh route into the ledger."""
    step = manual_steps.ManualStep(
        id=args.id,
        title=args.title,
        why=args.why,
        commands=list(args.commands),
        follow=args.follow,
        source_step=args.source_step,
    )
    if manual_steps.register(step):
        print(manual_steps.notice(step, manual_steps.ledger_path()), file=sys.stderr)
    return 0


def _deferred_failures_report_step(context: _StepContext) -> int:
    """The install's terminal report: every deferred failure, and who must act (0, 1 or 2).

    Any failure of the report itself is 3, never a 0/1/2 a caller could read as a report.
    """
    try:
        return deferred_failures.report(
            sys.stderr, deferred_failures.owner_of(context.config.install)
        )
    except Exception as exc:  # noqa: BLE001 - every failure must be the distinct status
        print(f"deferred-failures-report: the report failed: {exc}", file=sys.stderr)
        return deferred_failures.EXIT_REPORT_FAILED


def _deferral_owner(config_root: Path | None) -> deferred_failures.Owner:
    """The configured owner; a config that does not load must not stop a failure being recorded."""
    try:
        config = load_config(config_root or discover_config_root())
    except (
        ConfigValidationError,
        SchemaCompatibilityError,
        FileNotFoundError,
        OSError,
        ValueError,
    ) as exc:
        owner = deferred_failures.DEFAULT_OWNER
        print(
            f"  [warn] deferred-failure: owner not read from config ({exc}); naming {owner.name!r}",
            file=sys.stderr,
        )
        return owner
    return deferred_failures.owner_of(config.install)


def _deferred_failure_add(args: argparse.Namespace) -> int:
    """``ai-harness deferred-failure add``: install.sh's ``_defer_failure`` route into the ledger.

    1 is the refusal install.sh exits on (its FATAL lines are printed); 3 says the row could not
    be written, so the caller still holds the only record of the failure. ``--validate-only`` is
    the check alone, for a caller with no ledger: it records and prints nothing on success.
    """
    failure = deferred_failures.DeferredFailure(
        failure_class=args.failure_class,
        summary=args.summary,
        blocker=args.blocker,
        actions=list(args.actions),
        source_step=args.source_step,
    )
    owner = _deferral_owner(args.config)
    try:
        if args.validate_only:
            deferred_failures.check(failure, owner=owner)
        else:
            deferred_failures.register(failure, owner=owner)
    except deferred_failures.DeferralRefused:
        return 1
    except OSError as exc:
        print(
            f"ai-harness: error: deferred failure not recorded: {exc}", file=sys.stderr
        )
        return 3
    return 0


def _storage_step(context: _StepContext) -> int:
    try:
        storage_step = apply_storage(
            context.config.data,
            context.machine,
            context.state_root,
            extras=(agent_mail_extra(),),
        )
    except StorageApplyError as exc:
        raise ApplyError(str(exc)) from exc
    print("\n".join(_storage_report(storage_step)))
    print("\n".join(_docker_defaults_lines(context.config, context.machine)))
    return 0


def _wake_sidecar_step(context: _StepContext) -> int:
    """Install the declared notifier if needed, then converge its non-stopping boot policy."""
    from .agent_mail_notifier import read_config_tables
    from .wake_sidecar_boot import login_item_bundle_id

    host = os.environ.get("MACHINE") or context.machine or ""
    source = Path(os.environ.get("AIH_MESSAGING_PROFILE") or context.config.path)
    platform = wake_sidecar.platform_name()
    bundle_id = login_item_bundle_id(source, host, platform)
    if os.environ.get("AIH_WAKE_SIDECAR_PREPARE_ONLY") == "1":
        # Messaging needs attribution before its install/remove decision; boot policy
        # must converge after messaging, which may install or enable the notifier.
        _export_to_caller("_wake_sidecar_bundle_id", bundle_id)
        return 0
    try:
        tables = read_config_tables(source)
    except RuntimeError as exc:
        print(
            "  [warn] wake sidecar: the machine table could not be read, so boot state "
            f"is unchanged: {exc}",
            file=sys.stderr,
        )
        return 0
    explicitly_declared = wake_sidecar.declared(tables, host)
    config = wake_sidecar.load_wake_sidecar_config(tables, host)
    artifact = wake_sidecar.supervision_artifact(platform)
    if (
        explicitly_declared
        and config.enabled
        and artifact is not None
        and not artifact.is_file()
    ):
        print(
            "  [run] wake sidecar: installing the notifier for the enabled machine table"
        )
        previous_bundle = os.environ.get("AIH_NOTIFIER_LOGIN_ITEM_BUNDLE_ID")
        os.environ["AIH_NOTIFIER_LOGIN_ITEM_BUNDLE_ID"] = bundle_id
        previous = os.environ.get("AIH_AGENT_MAIL_NOTIFIER_MODE")
        os.environ["AIH_AGENT_MAIL_NOTIFIER_MODE"] = "git-inbox-only"
        try:
            code = _private_step("agent-mail-notifier")(context)
        finally:
            if previous_bundle is None:
                os.environ.pop("AIH_NOTIFIER_LOGIN_ITEM_BUNDLE_ID", None)
            else:
                os.environ["AIH_NOTIFIER_LOGIN_ITEM_BUNDLE_ID"] = previous_bundle
            if previous is None:
                os.environ.pop("AIH_AGENT_MAIL_NOTIFIER_MODE", None)
            else:
                os.environ["AIH_AGENT_MAIL_NOTIFIER_MODE"] = previous
        if code:
            return code
    return wake_sidecar.apply_boot_policy(
        config,
        explicitly_declared=explicitly_declared,
        platform=platform,
    )


def _cross_pair_trust_step(context: _StepContext) -> int:
    """install.sh's retired cross-pair trust line (IN-124): the reviewed public bindings the
    cross-pair launch policies name, merged from the checkout's ``config/buzz-pubkeys`` into
    ``${XDG_CONFIG_HOME:-$HOME/.config}/ai-harness/buzz``. A refusal is the shell's ``[warn]`` on
    stderr and the step still exits 0, as the shell's ``|| echo`` let the install continue.

    A config that declares no ``[remote_launch]`` table names no policy, so nothing is merged and
    nothing is printed; an unreadable one is left to the merge, which refuses and says so."""
    from .agent_mail_notifier import read_config_tables
    from .cross_pair_trust import install_pubkeys

    checkout = _main_tree_checkout(context)
    try:
        declared = "remote_launch" in read_config_tables(
            checkout / "projects-wide-session-config" / "ai-harness.toml"
        )
    except RuntimeError:
        declared = True
    if not declared:
        return 0
    configured = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(configured) if configured else Path.home() / ".config"
    try:
        merged = install_pubkeys(checkout, config_home / "ai-harness" / "buzz")
    except Exception:  # noqa: BLE001 - the shell's child traceback, then its `|| echo` warning
        traceback.print_exc()
        merged = False
    if not merged:
        print(
            "  [warn] cross-pair public bindings: merge refused; see above",
            file=sys.stderr,
        )
    return 0


def _session_names_step(context: _StepContext) -> int:
    """D-9 (issue-ref): validate ``config/session-names.toml`` of the checkout being installed.

    Any problem fails the install at this step, each named on its own line; a valid file prints
    one ok line. Phase 4 adds the name-list sync and the over-limit check after it.
    """
    checkout = context.config.root.parent
    problems = session_names.check_checkout(checkout)
    for problem in problems:
        print(f"  [fail] session-names: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(session_names.summary(checkout))
    return 0


#: The first run downloads the pinned package and its actionlint release into uv's cache.
ACTIONLINT_SECONDS = 300.0


def _actionlint_step(context: _StepContext) -> int:
    """install.sh's header-less actionlint provisioning (IN-137, ISSUE-REF): the checkout's
    pinned workflow linter, run once through bash so uvx warms it into uv's cache and a package or
    download failure is reported now, not first at a workflow push. Its output is relayed; a
    failure exits 1, so the hand-off defers it."""
    script = _main_tree_checkout(context) / "scripts" / "actionlint.sh"
    if not script.is_file():
        print(f"  [skip] actionlint: {script} is not in this checkout")
        return 0
    result = dependency_bootstrap.run_bounded(
        ["bash", str(script), "--version"], seconds=ACTIONLINT_SECONDS, capture=True
    )
    dependency_bootstrap.relay_redacted(result.stdout, sys.stdout)
    dependency_bootstrap.relay_redacted(result.stderr, sys.stderr)
    if result.returncode == 0:
        return 0
    print(
        f"  [fail] actionlint: {script} --version exited {result.returncode}"
        f"{dependency_bootstrap.expiry_note(result, ACTIONLINT_SECONDS)}",
        file=sys.stderr,
    )
    return 1


def _restart_mcp_step(context: _StepContext) -> int:
    messages, healthy = restart_mcp_install.run(
        "apply",
        _restart_mcp_config(context.config, context.state_root),
        _canonical_machine(context.config, context.machine),
        context.config.root.parent,
    )
    if messages:
        print("\n".join(messages))
    return 0 if healthy else 1


def _restart_mcp_config(config: LoadedConfig, state_root: Path) -> dict:
    """The registration and certifier must read the same CLI-selected state root."""
    return {
        **config.data,
        "install": {**config.install, "state_root": str(state_root.absolute())},
    }


# The steps install.sh hands to `apply --only`, in install.sh's own order; a selection always runs
# in this order whatever order it was given in, so the hand-off cannot reorder the installer.
# The full apply runs every one of them at its position, the private steps through
# `_private_step`, under install_sequence's per-step policy (ISSUE-REF T-3.6, R-1).
APPLY_STEPS: dict[str, Callable[[_StepContext], int]] = {
    "install-preflight": _private_step("install-preflight"),
    "projects-link": _projects_link_step,
    "main-branch-check": _main_branch_check_step,
    "skills-catalog": _private_step("skills-catalog"),
    "run-context-cutover": _private_step("run-context-cutover"),
    "profile-migration": _profile_migration_step,
    "host-identity": _host_identity_step,
    "host-alias": _private_step("host-alias"),
    "machine-id": _machine_id_step,
    "machine-registry-export": _private_step("machine-registry-export"),
    "billing-mode": _billing_mode_step,
    "xdg-state-home": _xdg_state_home_step,
    "machine-marker": _private_step("machine-marker"),
    "codex-route": _private_step("codex-route"),
    "windows-path-order": _windows_path_order_step,
    "zsh": _zsh_step,
    "gitleaks": _gitleaks_step,
    "doppler": _doppler_step,
    "claude-code": _private_step("claude-code"),
    "gh": _gh_step,
    "jq": _jq_step,
    "durable-tmux": _durable_tmux_step,
    "context-dir": _context_dir_step,
    "real-git": _real_git_step,
    "aido-config": _aido_config_step,
    "rendered-links": _rendered_links_step,
    "prune-stale-root": _prune_stale_root_step,
    "python-shim": _python_shim_step,
    "plugins": _plugins_step,
    "core-mcp": _core_mcp_step,
    "settings": _settings_step,
    "machine-settings": _machine_settings_step,
    "session-model": _session_model_step,
    "user-mcp-servers": _private_step("user-mcp-servers"),
    "context-supervisor-config": _context_supervisor_config_step,
    "idle-wake-config": _idle_wake_config_step,
    "statusline-links": _statusline_links_step,
    "helper-bins": _helper_bins_step,
    "notifier-app": _private_step("notifier-app"),
    "ai-core-config": _ai_core_config_step,
    "trusted-aido": _private_step("trusted-aido"),
    "mdb-dependencies": _private_step("mdb-dependencies"),
    "tier-b-entrypoints": _tier_b_entrypoints_step,
    "wrapper-publication": _wrapper_publication_step,
    "mdb-wrapper": _mdb_wrapper_step,
    "script-shims": _private_step("script-shims"),
    "retire-wrappers": _retire_wrappers_step,
    "mcp-wrapper-link": _private_step("mcp-wrapper-link"),
    "mcp-tool-source": _private_step("mcp-tool-source"),
    "helper-path": _helper_path_step,
    "bedrock-auth": _private_step("bedrock-auth"),
    "model-catalog": _private_step("model-catalog"),
    "worktree-drift": _private_step("worktree-drift"),
    "goal-waker": _private_step("goal-waker"),
    "machine-contention": _private_step("machine-contention"),
    "agent-mail-notifier": _private_step("agent-mail-notifier"),
    "messaging": _private_step("messaging"),
    "cross-pair-trust": _cross_pair_trust_step,
    "tier-b-entrypoint-check": _private_step("tier-b-entrypoint-check"),
    "wake-sidecar": _wake_sidecar_step,
    "shared-agents": _shared_agents_step,
    "research-write-retire": _private_step("research-write-retire"),
    "node-toolchain": _node_toolchain_step,
    "codex": _codex_step,
    "codex-prefix-check": _private_step("codex-prefix-check"),
    "pi-bootstrap": _private_step("pi-bootstrap"),
    "pi-wrapper": _pi_wrapper_step,
    "pi-settings": _pi_settings_step,
    "pi-extensions": _private_step("pi-extensions"),
    "pi-wq-link": _pi_wq_link_step,
    "account-pr-template": _private_step("account-pr-template"),
    "github-https-auth": _private_step("github-https-auth"),
    "editable-tools": _private_step("editable-tools"),
    "session-names": _session_names_step,
    "actionlint": _actionlint_step,
    "storage": _storage_step,
    "repo-venvs": _private_step("repo-venvs"),
    "ai-cli-remote": _private_step("ai-cli-remote"),
    "ssh-config-hosts": _private_step("ssh-config-hosts"),
    "ai-cli-environment": _private_step("ai-cli-environment"),
    "workload-cli": _private_step("workload-cli"),
    "terminal-session-restore": _private_step("terminal-session-restore"),
    "session-boot-relaunch": _private_step("session-boot-relaunch"),
    "external-host-installers": _private_step("external-host-installers"),
    "held-hook": _private_step("held-hook"),
    "prose-tools": _private_step("prose-tools"),
    "hook-links": _hook_links_step,
    "hooks-steering-check": _private_step("hooks-steering-check"),
    "hooks-manifest": _hooks_manifest_step,
    "skills-settings-env": _skills_settings_env_step,
    "tmp-cleanup-timer": _private_step("tmp-cleanup-timer"),
    "aws-token-warm-scheduler": _private_step("aws-token-warm-scheduler"),
    "launch-resume-schedulers": _private_step("launch-resume-schedulers"),
    "codex-update-scheduler": _private_step("codex-update-scheduler"),
    "macos-process-broker": _private_step("macos-process-broker"),
    "vscode-server-evidence": _private_step("vscode-server-evidence"),
    "vscode-reload-extension": _private_step("vscode-reload-extension"),
    "worktree-lifecycle-scheduler": _private_step("worktree-lifecycle-scheduler"),
    "skill-drift-scheduler": _private_step("skill-drift-scheduler"),
    "vendored-skill-check": _private_step("vendored-skill-check"),
    "fork-sync-timer": _private_step("fork-sync-timer"),
    "skills": _skills_step,
    "repo-hook-self-heal": _private_step("repo-hook-self-heal"),
    "merge-reap-trigger": _private_step("merge-reap-trigger"),
    "sibling-push-guard": _private_step("sibling-push-guard"),
    "jq-wrapper": _jq_wrapper_step,
    "dictation-settings-link": _private_step("dictation-settings-link"),
    "terminal-keybindings": _terminal_keybindings_step,
    "tmux-extended-keys": _private_step("tmux-extended-keys"),
    "terminal-mode-reset": _terminal_mode_reset_step,
    "screenshot-upload": _private_step("screenshot-upload"),
    "optional-cli-install": _private_step("optional-cli-install"),
    "capabilities": _capabilities_step,
    "notifier-helper": _private_step("notifier-helper"),
    # The restart-gate allowlist certifies servers npx launches, so it runs after npm is
    # ensured, where install.sh hands it off (ISSUE-REF).
    "restart-mcp": _restart_mcp_step,
    "task-store-provision": _private_step("task-store-provision"),
    "task-store-mirror-migration": _private_step("task-store-mirror-migration"),
    "task-store-publication": _private_step("task-store-publication"),
    "npm-dev-dependencies": _npm_dev_dependencies_step,
    "presentation-toolchain": _presentation_toolchain_step,
    "template-tool": _private_step("template-tool"),
    "ssm-plugin": _private_step("ssm-plugin"),
    "cache-links": _private_step("cache-links"),
    "template-asset-sync": _private_step("template-asset-sync"),
    "windows-native-flush": _windows_native_flush_step,
    "skills-canary": _private_step("skills-canary"),
    "git-guardrails": _git_guardrails_step,
    "ctime-agent-git-config": _private_step("ctime-agent-git-config"),
    "harness-root": _private_step("harness-root"),
    "boot-check": _boot_check_step,
    "claude-daemon": _claude_daemon_step,
    "credential-scope": _private_step("credential-scope"),
    "personal-url-rewrite": _private_step("personal-url-rewrite"),
    "personal-owner-helper": _private_step("personal-owner-helper"),
    "lint-baseline-pointer": _private_step("lint-baseline-pointer"),
    "checkout-venvs": _private_step("checkout-venvs"),
    "firstmate-fork": _private_step("firstmate-fork"),
    "chief-identity": _private_step("chief-identity"),
    "remote-sync": _private_step("remote-sync"),
    "canonical-copy-sync": _private_step("canonical-copy-sync"),
    "canonical-readme-sync": _canonical_readme_sync_step,
    "trust-projects": _trust_projects_step,
    "claude-readiness": _claude_readiness_step,
    "compact-wake-readiness": _compact_wake_readiness_step,
    "launcher-declaration": _private_step("launcher-declaration"),
    "manual-steps-report": _manual_steps_report_step,
    "deferred-failures-report": _deferred_failures_report_step,
}
# Steps that need this machine's identity resolved and registered first: host-identity resolves
# it (install.sh exports the answer through AI_HARNESS_EXPORTS_FILE), and every later step reads
# AI_HOST. settings and session-model key on the canonical name, as install.sh's Step 3 map did;
# capabilities scope a declaration's `machines` list to it, and optional-cli-install gates
# gemini-cli on it.
_MACHINE_STEPS = frozenset(
    {
        "host-alias",
        "billing-mode",
        "rendered-links",
        "settings",
        "machine-settings",
        "restart-mcp",
        "session-model",
        "hooks-manifest",
        "storage",
        "optional-cli-install",
        "capabilities",
        "pi-bootstrap",
        "pi-wrapper",
        "pi-settings",
        "pi-extensions",
    }
)
# Machine steps that act on the identity itself, so an unset AI_HOST is a refusal rather than a
# run under no machine: the shell ran each only after Step 1b had resolved it. The pi steps choose
# the persistent prefix, the opt-out and the settings template by it.
_IDENTITY_STEPS = frozenset(
    {
        "host-alias",
        "hooks-manifest",
        "pi-bootstrap",
        "pi-wrapper",
        "pi-settings",
        "pi-extensions",
    }
)


#: editable-tools' exit status that stops the install where any other failure defers.
STOP_ON_3_STATUS = 3
#: The failures `_run` reports as ``ai-harness: error:`` and exit 1; the full apply turns each
#: into that step's status of 1, the status install.sh's hand-off line saw from `apply --only`.
_STEP_ERRORS = (
    ApplyError,
    ConfigValidationError,
    MachineDetectionError,
    ProfileMigrationError,
    SettingsMergeError,
    SchemaCompatibilityError,
    CapabilityValidationError,
    PlatformDetectionError,
    FileNotFoundError,
    KeyError,
    LinkError,
    ValueError,
)


@dataclass(frozen=True)
class _FullApply:
    """What the full apply passes to its steps beyond the context: the static generation inputs
    and install.sh's mode flags."""

    outputs: dict[str, bytes]
    targets: list[InstallTarget]
    force: bool = False
    update: bool = False
    hook: bool = False
    local_only: bool = False
    #: install.sh's `_INVOKED_DIR`: the checkout it was invoked from, before the re-root.
    invoked_from: str = ""


def _full_apply_rendered_links(context: _StepContext, run: _FullApply) -> int:
    """The full apply's generation: the static targets too, and the generation id printed."""
    print(_install_generation(context, run.outputs, run.targets, force=run.force))
    return 0


def _full_apply_storage(context: _StepContext, _run: _FullApply) -> int:
    """The full apply's storage report: one line each, and nothing for an empty section."""
    try:
        storage_step = apply_storage(
            context.config.data,
            context.machine,
            context.state_root,
            extras=(agent_mail_extra(),),
        )
    except StorageApplyError as exc:
        raise ApplyError(str(exc)) from exc
    for line in _storage_report(storage_step):
        print(line)
    for line in _docker_defaults_lines(context.config, context.machine):
        print(line)
    return 0


#: The steps whose full-apply body differs from their `--only` body, by function name.
_FULL_APPLY_BODIES = {
    "rendered-links": "_full_apply_rendered_links",
    "storage": "_full_apply_storage",
}


def _full_apply_body(name: str, run: _FullApply) -> Callable[[_StepContext], int]:
    """The callable the full apply runs for ``name``, looked up by name at call time."""
    special = _FULL_APPLY_BODIES.get(name)
    if special is not None:
        body = globals()[special]
        return lambda context: body(context, run)
    step = APPLY_STEPS[name]
    if getattr(step, "private_name", None):
        return lambda context: step(context, adopt=True)
    return globals()[step.__name__]


def _call_step(context: _StepContext, run: _FullApply, name: str) -> int:
    """Run one step and return its status, reporting a failure as `apply --only <step>` does."""
    try:
        return _full_apply_body(name, run)(context)
    except (StorageDeclarationError, UnknownMachineError) as exc:
        print(f"ai-harness: error: {exc}", file=sys.stderr)
        if isinstance(exc, UnknownMachineError) and name == "rendered-links":
            print(
                rendered_links.STEP3_FATAL.format(
                    machine=os.environ.get("AI_HOST", "")
                ),
                file=sys.stderr,
            )
        return exc.exit_code
    except _STEP_ERRORS as exc:
        detail = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        print(f"ai-harness: error: {detail}", file=sys.stderr)
        return 1


def _private_sequence_entry(config: LoadedConfig, name: str) -> dict[str, object]:
    """``name``'s table in the private half of the step policy, ``[orchestrator].install_sequence``,
    read now; ``{}`` when none is declared, the file is unreadable, or it has no such table."""
    table = config.data.get("orchestrator")
    declared = table.get("install_sequence") if isinstance(table, dict) else None
    if not declared:
        return {}
    path = _config_path(config, str(declared))
    try:
        private = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(
            f"  [warn] install sequence table {path} not read: {exc}", file=sys.stderr
        )
        return {}
    entry = private.get(name)
    return entry if isinstance(entry, dict) else {}


def _deferral_prose(config: LoadedConfig, name: str) -> tuple[str, str]:
    """The summary and remedy a failed step's deferral carries: the private table's, else the
    public core's, which names only the step."""
    entry = _private_sequence_entry(config, name)
    summary, action = entry.get("summary"), entry.get("action")
    if not isinstance(summary, str):
        return f"{name}: ai-harness apply failed", ""
    return (
        os.path.expandvars(summary),
        os.path.expandvars(action) if isinstance(action, str) else "",
    )


def _effective_policy(
    config: LoadedConfig, entry: install_sequence.StepPolicy
) -> install_sequence.StepPolicy:
    """``entry`` with the mode variables the private table declares for the step; a variable
    whose source the core does not know is ignored with a warning."""
    declared = _private_sequence_entry(config, entry.name).get("mode_env")
    if not isinstance(declared, dict):
        return entry
    pairs = []
    for variable, source in declared.items():
        if source in _MODE_VALUES:
            pairs.append((variable, source))
        else:
            print(
                f"  [warn] {entry.name}: mode variable {variable} has an unknown source "
                f"{source!r}; not set",
                file=sys.stderr,
            )
    return install_sequence.with_private_mode_env(entry, tuple(pairs))


def _defer(context: _StepContext, name: str) -> bool:
    """install.sh's ``_defer_failure self``: record the step's failure; False if not recorded."""
    summary, action = _deferral_prose(context.config, name)
    try:
        deferred_failures.register(
            deferred_failures.DeferredFailure(
                failure_class="self",
                summary=summary,
                actions=[action] if action else [],
                source_step=name,
            ),
            owner=deferred_failures.owner_of(context.config.install),
        )
    except OSError as exc:
        print(
            f"  [deferred] {summary}\n             [warn] not recorded ({exc}); "
            "the apply exits 1 at its end",
            file=sys.stderr,
        )
        return False
    return True


def _python_bin() -> str:
    """install.sh's ``"$_PYTHON_BIN"`` in a full apply: the python-shim step's answer (empty when
    it found none), or this process's interpreter when the step has not run here."""
    resolved = python_shim.answer()
    return sys.executable if resolved is None else resolved


_MODE_VALUES: dict[str, Callable[[_FullApply, _StepContext, bool], str]] = {
    install_sequence.FROM_UPDATE: lambda run, _context, _refused: str(int(run.update)),
    install_sequence.FROM_HOOK: lambda run, _context, _refused: str(int(run.hook)),
    install_sequence.FROM_LOCAL_ONLY: lambda run, _c, _r: str(int(run.local_only)),
    install_sequence.FROM_INVOKED_DIR: lambda run, _context, _refused: run.invoked_from,
    install_sequence.PINNED_ZERO: lambda _run, _context, _refused: "0",
    install_sequence.FROM_SETTINGS_REFUSAL: lambda _run, _context, refused: str(
        int(refused)
    ),
    install_sequence.FROM_MACHINE: lambda _run, context, _refused: (
        _canonical_machine(context.config, context.machine) or ""
    ),
    install_sequence.FROM_PYTHON_BIN: lambda _run, _context, _refused: _python_bin(),
    install_sequence.FROM_BEDROCK_AUTH: lambda _run, _context, _refused: (
        _HANDED_BACK_SHELL.get(
            install_sequence.BEDROCK_AUTH_HAND_BACK,
            install_sequence.BEDROCK_AUTH_DEFAULT,
        )
    ),
}


@contextmanager
def _mode_environment(
    entry: install_sequence.StepPolicy,
    run: _FullApply,
    context: _StepContext,
    refused: bool,
) -> Iterator[None]:
    """The variables the hand-off line set for this step alone, restored after it."""
    previous: dict[str, str | None] = {}
    for variable, source in entry.mode_env:
        previous[variable] = os.environ.get(variable)
        os.environ[variable] = _MODE_VALUES[source](run, context, refused)
    try:
        yield
    finally:
        for variable, value in previous.items():
            if value is None:
                os.environ.pop(variable, None)
            else:
                os.environ[variable] = value


def _full_apply_gate_open(context: _StepContext, name: str) -> bool:
    """False for a step the full apply skips silently: no registry, or its opt-in is off."""
    if name in install_sequence.REGISTRY_GATED and context.config.registry is None:
        return False
    key = install_sequence.CONFIG_GATES.get(name)
    return key is None or bool(context.config.install[key])


def _finish_reports(context: _StepContext, run: _FullApply) -> int:
    """install.sh's ``_aih_finish_reports``: the manual steps, then the deferred report's status."""
    if _call_step(context, run, "manual-steps-report"):
        _defer(context, "manual-steps-report")
    return _call_step(context, run, "deferred-failures-report")


def _settings_lock_dir() -> Path:
    """install.sh's ``$CLAUDE_DIR/.ai-harness-settings.lock``, with ``$CLAUDE_DIR`` made first."""
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    return settings_lock.lock_dir(claude_dir)


def _run_install_sequence(context: _StepContext, run: _FullApply) -> int:
    """The full apply: every sequence step in order, each failure handled by its policy.

    The settings write lock install.sh takes is held across the same span, from ``plugins``
    through ``skills-settings-env`` (T6-0b, ruling 1), and released on every exit path.
    """
    span = install_sequence.settings_lock_span()
    held: list[Path] = []
    python_shim.reset()
    _HANDED_BACK_SHELL.clear()
    try:
        return _run_steps(context, run, span, held)
    finally:
        for path in held:
            settings_lock.release(path)


def _run_steps(
    context: _StepContext, run: _FullApply, span: frozenset[str], held: list[Path]
) -> int:
    refused = False
    unrecorded = False
    skipped_labels: set[str] = set()
    report_code = deferred_failures.EXIT_CLEAN
    for entry in install_sequence.full_apply_order():
        name = entry.name
        if name in span and not held:
            path = _settings_lock_dir()
            try:
                settings_lock.acquire(path)
            except settings_lock.SettingsLockTimeout:
                return deferred_failures.exit_status(
                    1, _finish_reports(context, run), "self"
                )
            held.append(path)
        elif name not in span and held:
            settings_lock.release(held.pop())
        if not _full_apply_gate_open(context, name):
            continue
        if entry.guard and refused:
            if entry.guard not in skipped_labels:
                print(
                    f"  [skip] {entry.guard}: the settings step refused; "
                    "~/.claude/settings.json left untouched"
                )
                skipped_labels.add(entry.guard)
            continue
        with _mode_environment(
            _effective_policy(context.config, entry), run, context, refused
        ):
            code = _call_step(context, run, name)
        kind = install_sequence.full_apply_kind(entry)
        if kind == install_sequence.RECORD:
            report_code = code
            continue
        if not code:
            continue
        if kind == install_sequence.STOP or (
            kind == install_sequence.STOP_ON_3 and code == STOP_ON_3_STATUS
        ):
            status = code if name in install_sequence.OWN_STATUS else 1
            return deferred_failures.exit_status(
                status, _finish_reports(context, run), entry.stop_class
            )
        unrecorded = not _defer(context, name) or unrecorded
        refused = refused or entry.sets_refusal
    return deferred_failures.exit_status(int(unrecorded), report_code)


def _dry_run_gate(
    context: _StepContext,
    entry: install_sequence.StepPolicy,
    orchestrated: bool,
    locked: frozenset[str] = frozenset(),
) -> str:
    parts = []
    name = entry.name
    if name in locked:
        parts.append("settings lock held")
    if name in install_sequence.NOT_HANDED_OFF:
        parts.append("no install.sh hand-off")
    if name in install_sequence.REGISTRY_GATED:
        parts.append(
            "registry declared"
            if context.config.registry is not None
            else "no registry: skipped"
        )
    key = install_sequence.CONFIG_GATES.get(name)
    if key is not None:
        value = bool(context.config.install[key])
        parts.append(
            f"install.{key}={str(value).lower()}{'' if value else ': skipped'}"
        )
    if name in install_sequence.AHEAD_OF_POSITION:
        parts.append(
            f"ahead of its position, after {install_sequence.AHEAD_OF_POSITION[name]}"
        )
    if getattr(APPLY_STEPS[name], "private_name", None) and not orchestrated:
        parts.append("no orchestrator: not applicable")
    if entry.gate:
        parts.append(f"install.sh: {entry.gate}")
    return "; ".join(parts) or "-"


def _dry_run_env(
    context: _StepContext, entry: install_sequence.StepPolicy, run: _FullApply
) -> str:
    values = []
    for variable, source in entry.mode_env:
        value = (
            "<1 if settings refused, else 0>"
            if source == install_sequence.FROM_SETTINGS_REFUSAL
            else _MODE_VALUES[source](run, context, False)
        )
        values.append(f"{variable}={value}")
    return " ".join(values) or "-"


def _dry_run(context: _StepContext, run: _FullApply, selected: Sequence[str]) -> int:
    """``apply --dry-run``: the sequence and each step's policy, with nothing run or written."""
    entrypoint = _orchestrator_entrypoint(context.config)
    print(f"ai-harness apply --dry-run: config root {context.config.root}")
    print(f"  config file: {context.config.path}")
    print(
        f"  orchestrator: {entrypoint or 'none (every private step is not applicable)'}"
    )
    print(
        f"  machine: {context.machine or 'unset'}; projects root: {context.projects_root}"
    )
    print(
        f"  flags: update={int(run.update)} hook={int(run.hook)} "
        f"local-only={int(run.local_only)}"
    )
    if selected:
        print(
            "  --only: the selected steps run in APPLY_STEPS order and the first failure "
            "stops the run; the policy shown is the full apply's"
        )
    names = (
        list(selected)
        if selected
        else [entry.name for entry in install_sequence.full_apply_order()]
    )
    locked: frozenset[str] = frozenset()
    if not selected:
        locked = install_sequence.settings_lock_span()
        print(
            f"  settings lock: {settings_lock.lock_dir(Path.home() / '.claude')}, held from "
            f"{install_sequence.SETTINGS_LOCK_FROM} through "
            f"{install_sequence.SETTINGS_LOCK_THROUGH}; a dry run takes no lock"
        )
    for index, name in enumerate(names, start=1):
        owner = (
            "private" if getattr(APPLY_STEPS[name], "private_name", None) else "core"
        )
        if name in install_sequence.NOT_SEQUENCED:
            reason = install_sequence.NOT_SEQUENCED[name]
            print(f"  {index:3d}  {name}  {owner}  not a sequence step: {reason}")
            continue
        entry = install_sequence.policy(name)
        kind = install_sequence.full_apply_kind(entry)
        if kind != entry.kind:
            kind = f"{kind} (install.sh: {entry.kind})"
        if name in install_sequence.OWN_STATUS:
            kind += ", own status"
        guard = entry.guard or "-"
        if entry.sets_refusal:
            guard = "sets the settings refusal"
        print(
            f"  {index:3d}  {name}  {owner}  kind={kind}  guard={guard}  "
            f"gate={_dry_run_gate(context, entry, entrypoint is not None, locked)}  "
            f"env={_dry_run_env(context, _effective_policy(context.config, entry), run)}"
        )
    print(f"  total: {len(names)} steps; nothing was run and nothing was written")
    return 0


def _only_step_names(value: str) -> tuple[str, ...]:
    """argparse type for ``--only``: a comma-separated list of registered step names."""
    names = tuple(name.strip() for name in value.split(","))
    unknown = [name for name in names if name not in APPLY_STEPS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown step {', '.join(repr(name) for name in unknown)}; "
            f"known steps: {', '.join(APPLY_STEPS)}"
        )
    return names


def _run(argv: list[str] | None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["secret-handoff-launch"]:
        from .secret_handoff.launch import main as launch

        return launch(arguments[1:])
    parser = argparse.ArgumentParser(prog="ai-chassis")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state-root", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply"):
        command = sub.add_parser(name)
        command.add_argument(
            "--target", action="append", default=[], metavar="OUTPUT:PATH[:KIND]"
        )
        command.add_argument(
            "--projects-root",
            type=Path,
            default=None,
            metavar="PATH",
            help="projects root (default: site.context_install_dir, $PROJECTS_DIR, ~/projects)",
        )
        if name == "apply":
            command.add_argument("--force", action="store_true")
            # install.sh's own flags, mapped to the mode variables its hand-off lines set
            # (ISSUE-REF T-3.6, install_sequence); the full apply only: `--only` callers set them.
            command.add_argument(
                "--update", action="store_true", help="AIH_INSTALL_UPDATE_MODE=1"
            )
            command.add_argument(
                "--hook", action="store_true", help="install.sh's --hook mode"
            )
            command.add_argument(
                "--local-only",
                action="store_true",
                help="install.sh's --local-only mode",
            )
            command.add_argument(
                "--invoked-from",
                default="",
                metavar="PATH",
                help="install.sh's _INVOKED_DIR, the checkout it was invoked from",
            )
            command.add_argument(
                "--dry-run",
                action="store_true",
                help="print the install sequence and each step's policy; run nothing",
            )
            # ISSUE-REF Phase 3 (T-3.2/T-3.4): the shell installer reaches each extracted step
            # through `apply --only <step>` without re-running the steps shell still owns.
            # Measured on example-project-ec2 (2026-10-12): a full `apply` is NOT a no-op on a
            # shell-installed host (`plan` lists nine per-machine context symlinks plus a
            # generation swap the shell installer never performs), so the scoped form is the only
            # safe hand-off until the remaining T-3.4 batches move those steps behind the CLI.
            command.add_argument(
                "--only",
                action="append",
                type=_only_step_names,
                default=None,
                metavar="STEP[,STEP...]",
                help="run only the named steps, in installer order: "
                + ", ".join(APPLY_STEPS),
            )
    sub.add_parser("doctor").add_argument("--repair", action="store_true")
    sub.add_parser(
        "boot-check", help="the post-boot verification"
    ).add_argument(
        "--now", action="store_true", help="run every check now, without the boot wait"
    )
    sub.add_parser("rollback")
    sub.add_parser("update")
    sub.add_parser("init")
    sub.add_parser("validate")
    sub.add_parser("migrate-config")
    explain = sub.add_parser("explain")
    explain.add_argument("key")
    manual_step = sub.add_parser(
        "manual-step", help="register a step the installing session must do by hand"
    )
    manual_step_sub = manual_step.add_subparsers(
        dest="manual_step_command", required=True
    )
    add = manual_step_sub.add_parser("add")
    add.add_argument("--id", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--why", required=True)
    add.add_argument(
        "--command", dest="commands", action="append", default=[], metavar="COMMAND"
    )
    add.add_argument("--follow", required=True, metavar="DOC_OR_SKILL")
    add.add_argument("--source-step", default="install.sh")
    deferred = sub.add_parser(
        "deferred-failure", help="record a failure the install reports at its end"
    )
    deferred_sub = deferred.add_subparsers(
        dest="deferred_failure_command", required=True
    )
    defer = deferred_sub.add_parser("add")
    # Not `choices`: an unknown class is refused with install.sh's own FATAL lines, not usage.
    defer.add_argument("--class", dest="failure_class", required=True)
    defer.add_argument("--summary", required=True)
    defer.add_argument("--blocker", default="")
    defer.add_argument(
        "--action", dest="actions", action="append", default=[], metavar="LINE"
    )
    defer.add_argument("--source-step", default="install.sh")
    defer.add_argument(
        "--validate-only",
        action="store_true",
        help="refuse an invalid deferral as add would, and record nothing",
    )
    args = parser.parse_args(argv)
    if (
        args.command == "apply"
        and args.only
        and (args.update or args.hook or args.local_only or args.invoked_from)
    ):
        parser.error(
            "--update, --hook, --local-only and --invoked-from apply to the full apply only; "
            "under --only the caller sets the mode variables itself"
        )
    try:
        if args.command == "init":
            _init(args.config or config_root())
            return 0
        if args.command == "update":
            _update()
            return 0
        if args.command == "boot-check":
            return boot_check.main(["--now"] if args.now else [])
        if args.command == "manual-step":
            return _manual_step_add(args)
        if args.command == "deferred-failure":
            return _deferred_failure_add(args)
        root = args.config or discover_config_root()
        config = load_config(root)
        state_root = _state_root(config, args.state_root)
        machine = os.environ.get("AI_HOST")
        if machine == "hetzner" and args.command in {"apply", "plan"}:
            return _unknown_identity(config, machine, "AI_HOST")
        selected = (
            [name for name in APPLY_STEPS if any(name in group for group in args.only)]
            if args.command == "apply" and args.only
            else []
        )
        # host-identity resolves and registers the machine itself, so a selection that includes
        # it is checked there; any other machine step reads the AI_HOST it exported. A full apply
        # over a config with no registry keeps the plain name check. context-dir runs before
        # install.sh's identity steps, as Step 2 always did, and needs no machine.
        if args.command == "apply" and (
            (
                selected
                and "host-identity" not in selected
                and _MACHINE_STEPS.intersection(selected)
            )
            or (not selected and config.registry is None)
        ):
            _require_registered_machine(config, machine)
            needing = [name for name in selected if name in _IDENTITY_STEPS]
            if not machine and needing:
                # The shell steps only ever ran with AI_HOST set (Step 1b had resolved it), so an
                # unresolved identity here is a refusal, never a silent `--machine ""`.
                tables = machine_tables(config.data)
                raise ApplyError(
                    f"{', '.join(needing)} need{'s' if len(needing) == 1 else ''} this "
                    "machine's identity: AI_HOST is unset; run "
                    "`ai-harness apply --only host-identity` first; registered machines: "
                    f"{', '.join(sorted(tables)) or 'none'} (from {config.path})"
                )
        projects_root = resolve_projects_root(
            getattr(args, "projects_root", None), config, os.environ, Path.home()
        )
        if args.command == "apply" and args.dry_run:
            if not selected:
                _validate_hygiene(config)
            return _dry_run(
                _StepContext(config, machine, state_root, projects_root),
                _FullApply(
                    {},
                    [],
                    False,
                    args.update,
                    args.hook,
                    args.local_only,
                    args.invoked_from,
                ),
                selected,
            )
        if selected:
            context = _StepContext(config, machine, state_root, projects_root)
            for name in selected:
                code = APPLY_STEPS[name](context)
                if code:
                    return code
            return 0
        if args.command == "validate":
            plan = build_render_plan(root)
            capabilities = resolve_capabilities(
                config.capabilities, machine=os.environ.get("AI_HOST")
            )
            validate_required(capabilities)
            _validate_hygiene(config)
            _mode, patterns, allowlist, _retain = _hygiene(config)
            _validate_artifacts(plan.outputs, patterns, allowlist)
            print("configuration valid")
            return 0
        if args.command == "migrate-config":
            print(f"config schema {config.schema_version} is current")
            return 0
        if args.command == "explain":
            print(explain_config_value(root, args.key))
            return 0
        if args.command == "doctor":
            core_mcp_code = _core_mcp_run(
                "doctor", _StepContext(config, machine, state_root, projects_root)
            )
            result = doctor(state_root, repair=args.repair)
            checkpoint_lines, checkpoint_healthy = doctor_activation(state_root)
            core_config_lines, core_config_healthy = doctor_activation(
                state_root, capability="ai-core-config"
            )
            capabilities = resolve_capabilities(
                config.capabilities, machine=os.environ.get("AI_HOST")
            )
            storage_report = doctor_storage(config.data, os.environ.get("AI_HOST"))
            generation_lines, generation_unresolved = _generation_doctor_lines(
                config, state_root
            )
            order_lines, order_healthy = _boot_order_doctor(config)
            allow_lines, allow_healthy = _buzz_allow_list_doctor()
            layout_lines = _projects_root_layout_doctor(config, projects_root)
            rc_lines, rc_healthy = _shell_rc_credential_doctor()
            mcp_lines, mcp_healthy = restart_mcp_install.run(
                "doctor",
                _restart_mcp_config(config, state_root),
                _canonical_machine(config, machine),
                config.root.parent,
            )
            messages = (
                *result.messages,
                *checkpoint_lines,
                *core_config_lines,
                *generation_lines,
                *_capability_doctor_messages(capabilities),
                *_hook_doctor_messages(config, capabilities),
                *storage_report.lines,
                *order_lines,
                *allow_lines,
                *layout_lines,
                *rc_lines,
                *mcp_lines,
            )
            print("\n".join(messages) or "healthy")
            healthy = (
                result.healthy
                and checkpoint_healthy
                and core_config_healthy
                and storage_report.healthy
                and order_healthy
                and allow_healthy
                and rc_healthy
                and mcp_healthy
                and not core_mcp_code
                and not generation_unresolved
            )
            return 0 if healthy else 1
        if args.command == "rollback":
            print(rollback(state_root))
            return 0
        plan = build_render_plan(root)
        static_targets = _targets(args.target) or _config_targets(config)
        step_context = _StepContext(config, machine, state_root, projects_root)
        if args.command == "plan":
            core_mcp_code = _core_mcp_run("plan", step_context)
            try:
                _outputs, targets, _rendered = _generation_inputs(
                    step_context, plan.outputs, static_targets
                )
            except rendered_links.RenderedLinksError as exc:
                print(f"rendered-links: not planned: {exc}")
                targets = static_targets
            if not targets:
                raise ApplyError(
                    "no install targets resolved; declare [[install.target]] or pass "
                    "--target"
                )
            print("\n".join(_plan_mutations(state_root, targets)))
            mcp_lines, _mcp_healthy = restart_mcp_install.run(
                "plan",
                _restart_mcp_config(config, state_root),
                _canonical_machine(config, machine),
                config.root.parent,
            )
            if mcp_lines:
                print("\n".join(mcp_lines))
            machine = os.environ.get("AI_HOST")
            declaration = resolve(config.data, machine) if machine else None
            print("\n".join(plan_docker_defaults(declaration, ETC_ROOT)))
            try:
                print(
                    "\n".join(
                        plan_docker_roots(declaration, ETC_ROOT, DOCKER_ROOTS_RUN)
                    )
                )
            except DockerRootsError as exc:
                raise ApplyError(str(exc)) from exc
            return core_mcp_code
        _validate_hygiene(config)
        return _run_install_sequence(
            step_context,
            _FullApply(
                plan.outputs,
                static_targets,
                force=args.force,
                update=args.update,
                hook=args.hook,
                local_only=args.local_only,
                invoked_from=args.invoked_from,
            ),
        )
    except (StorageDeclarationError, UnknownMachineError) as exc:
        print(f"ai-harness: error: {exc}", file=__import__("sys").stderr)
        if isinstance(exc, UnknownMachineError) and (
            args.command == "apply"
            and (not args.only or any("rendered-links" in group for group in args.only))
        ):
            # install.sh Step 3's own refusal, for the step that inherited it.
            print(
                rendered_links.STEP3_FATAL.format(
                    machine=os.environ.get("AI_HOST", "")
                ),
                file=sys.stderr,
            )
        return exc.exit_code
    except _STEP_ERRORS as exc:
        detail = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        print(f"ai-harness: error: {detail}", file=__import__("sys").stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    """Run one command, then hand on the Windows user-environment writes it queued, in one batch."""
    code = _run(argv)
    # A run not under install.sh created its own ledgers: report them and remove them. Its
    # deferred failures decide its status as install.sh's report did.
    manual_steps.finish_owned_run(sys.stdout)
    code = deferred_failures.exit_status(
        code, deferred_failures.finish_owned_run(sys.stderr)
    )
    target = os.environ.get(EXPORTS_FILE_ENV)
    try:
        flush_windows_user_env(Path(target) if target else None)
    except ProfileMigrationError as exc:
        print(f"ai-harness: error: {exc}", file=sys.stderr)
        return code or 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
