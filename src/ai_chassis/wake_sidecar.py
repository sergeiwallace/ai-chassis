"""Fail-closed configuration for the fleet wake sidecar.

An absent ``[machine.<host>.wake_sidecar]`` table preserves the pre-table behaviour:
machine-wide wake is permitted, launch and restart are not, and installers leave the
existing notifier state unchanged.  Callers load the table at the point of use so a local
configuration edit takes effect without restarting the notifier.

Session and both modes have the same fallback behavior: a live per-session watcher
owns its slug, and the machine sidecar serves sessions without a live watcher.
The two mode names record operator intent; all other machine-side duties continue.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KNOWN_ACTIONS = frozenset({"wake", "launch", "restart"})
KNOWN_MODES = frozenset({"machine", "session", "both"})
_KEYS = frozenset({"enabled", "mode", "boot", "actions"})
SYSTEMD_UNIT = "ai-harness-agent-mail-notifier.service"
LAUNCHD_UNIT = "com.ai-harness.agent-mail-notifier"


@dataclass(frozen=True)
class WakeSidecarConfig:
    enabled: bool
    mode: str
    boot: bool
    actions: tuple[str, ...]
    source: str
    disabled_reason: str | None = None


def _disabled(reason: str, source: str) -> WakeSidecarConfig:
    return WakeSidecarConfig(
        enabled=False,
        mode="machine",
        boot=False,
        actions=(),
        source=source,
        disabled_reason=reason,
    )


def load_wake_sidecar_config(  # noqa: C901, PLR0911, PLR0912 - one refusal per named key
    tables: Mapping[str, Any], host: str
) -> WakeSidecarConfig:
    """Load one machine table, refusing the entire table on its first invalid key."""
    source = str(tables.get("_config_source") or "default")
    if error := tables.get("_config_error"):
        return _disabled(f"config-unreadable ({error})", source)

    machines = tables.get("machine")
    if machines is None:
        return WakeSidecarConfig(True, "machine", False, ("wake",), source)
    if not isinstance(machines, dict):
        return _disabled("malformed:machine", source)
    profile = machines.get(host)
    if profile is None:
        return WakeSidecarConfig(True, "machine", False, ("wake",), source)
    if not isinstance(profile, dict):
        return _disabled(f"malformed:machine.{host}", source)
    section = profile.get("wake_sidecar")
    if section is None:
        return WakeSidecarConfig(True, "machine", False, ("wake",), source)
    prefix = f"machine.{host}.wake_sidecar"
    if not isinstance(section, dict):
        return _disabled(f"malformed:{prefix}", source)

    for key in sorted(section):
        if key not in _KEYS:
            return _disabled(f"unknown-key:{prefix}.{key}", source)
    for key in ("enabled", "boot"):
        if key in section and not isinstance(section[key], bool):
            return _disabled(f"malformed:{prefix}.{key}", source)

    mode = section.get("mode", "machine")
    if mode not in KNOWN_MODES:
        return _disabled(f"malformed:{prefix}.mode", source)
    actions = section.get("actions", ["wake"])
    if (
        not isinstance(actions, list)
        or not all(isinstance(action, str) for action in actions)
        or not set(actions) <= KNOWN_ACTIONS
        or len(set(actions)) != len(actions)
    ):
        return _disabled(f"malformed:{prefix}.actions", source)

    enabled = section.get("enabled", False)
    if not enabled:
        return _disabled("wake-sidecar-disabled", source)
    return WakeSidecarConfig(
        enabled=True,
        mode=mode,
        boot=section.get("boot", False),
        actions=tuple(actions),
        source=source,
    )


def action_permitted(config: WakeSidecarConfig, action: str) -> tuple[bool, str]:
    """Return whether ``action`` is enabled and a stable refusal reason when it is not."""
    if action not in KNOWN_ACTIONS:
        return False, f"unknown-action:{action}"
    if not config.enabled:
        return False, config.disabled_reason or "wake-sidecar-disabled"
    if action not in config.actions:
        return False, f"wake-sidecar-action-disabled:{action}"
    return True, ""


def status_text(config: WakeSidecarConfig) -> str:
    """Render the notifier's stable one-line status field."""
    if not config.enabled:
        return f"disabled ({config.disabled_reason or 'wake-sidecar-disabled'})"
    actions = ",".join(config.actions)
    return (
        f"enabled mode={config.mode} boot={str(config.boot).lower()} actions={actions}"
    )


def declared(tables: Mapping[str, Any], host: str) -> bool:
    """Whether this profile explicitly opts into wake-sidecar install management."""
    machines = tables.get("machine")
    profile = machines.get(host) if isinstance(machines, dict) else None
    return isinstance(profile, dict) and "wake_sidecar" in profile


def platform_name(*, runner=subprocess.run) -> str:
    """Return install.sh's PATH-resolved ``uname -s`` value."""
    try:
        result = runner(["uname", "-s"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def supervision_artifact(
    platform: str, environ: Mapping[str, str] = os.environ
) -> Path | None:
    """Return the notifier artifact whose presence makes boot policy actionable."""
    home = Path(environ.get("HOME") or Path.home())
    if platform == "Linux":
        config_home = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
        return config_home / "systemd" / "user" / SYSTEMD_UNIT
    if platform == "Darwin":
        launch_agents = Path(
            environ.get("LAUNCH_AGENTS_DIR") or home / "Library" / "LaunchAgents"
        )
        return launch_agents / f"{LAUNCHD_UNIT}.plist"
    return None


def apply_boot_policy(  # noqa: PLR0911 - one stable outcome per supervisor state
    config: WakeSidecarConfig,
    *,
    explicitly_declared: bool,
    runner=subprocess.run,
    environ: Mapping[str, str] = os.environ,
    platform: str | None = None,
) -> int:
    """Disable future boot activation without stopping a notifier that is running now."""
    if not explicitly_declared or not config.enabled or config.boot:
        return 0
    platform = platform or platform_name(runner=runner)
    artifact = supervision_artifact(platform, environ)
    if artifact is None:
        return 0
    if not artifact.is_file():
        kind = "LaunchAgent" if platform == "Darwin" else "unit"
        print(
            f"  [skip] wake sidecar: boot=false but the notifier {kind} is not installed"
        )
        return 0

    if platform == "Linux":
        active = (
            runner(
                ["systemctl", "--user", "is-active", "--quiet", SYSTEMD_UNIT],
                check=False,
            ).returncode
            == 0
        )
        disabled = runner(["systemctl", "--user", "disable", SYSTEMD_UNIT], check=False)
        if disabled.returncode:
            print(
                "  [warn] wake sidecar: could not disable the notifier unit for boot=false",
                file=sys.stderr,
            )
            return 0
        suffix = "; left running" if active else " for launcher start"
        print(f"  [ok]  wake sidecar: notifier disabled at boot{suffix} (boot=false)")
        return 0

    target = f"gui/{os.getuid()}/{LAUNCHD_UNIT}"
    state = runner(
        ["launchctl", "print", target],
        capture_output=True,
        text=True,
        check=False,
    )
    active = state.returncode == 0 and "state = running" in state.stdout
    disabled = runner(["launchctl", "disable", target], check=False)
    if disabled.returncode:
        print(
            "  [warn] wake sidecar: could not disable the LaunchAgent for boot=false",
            file=sys.stderr,
        )
        return 0
    suffix = "; left running" if active else " for launcher start"
    print(f"  [ok]  wake sidecar: LaunchAgent disabled at boot{suffix} (boot=false)")
    return 0
