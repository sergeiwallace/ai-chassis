"""Installer-only boot activation, outside the notifier import closure."""

import sys
import tomllib
from pathlib import Path


def wake_sidecar_boot_enabled(table: object) -> bool:
    """The boot activation rule shared by install.sh and machine coverage checks."""
    return isinstance(table, dict) and table.get("boot") is True


def login_item_bundle_id(source: Path, host: str, platform: str) -> str:
    """Read macOS login attribution before messaging chooses its notifier mode.

    Board, Buzz and git-inbox installs receive the same bundle ID. A login sidecar
    also survives messaging declaring that none of its rungs wants the notifier.
    Full sidecar validation and boot convergence run after messaging.
    """
    if platform != "Darwin":
        return ""
    try:
        with source.open("rb") as stream:
            tables = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        print(
            "  [warn] wake_sidecar.boot: config could not be read; treating as false",
            file=sys.stderr,
        )
        return ""
    profile = tables.get("machine", {}).get(host, {})
    table = profile.get("wake_sidecar", {})
    boot = table.get("boot", False) if isinstance(table, dict) else None
    if type(boot) is not bool:
        print(
            "  [warn] wake_sidecar.boot must be boolean; treating as false",
            file=sys.stderr,
        )
    elif wake_sidecar_boot_enabled(table):
        bundle_id = (
            tables.get("notify", {})
            .get("families", {})
            .get("fleet", {})
            .get("bundle_id", "")
        )
        if isinstance(bundle_id, str) and bundle_id:
            return bundle_id
        print(
            "  [warn] wake_sidecar.boot: notify.families.fleet.bundle_id is missing",
            file=sys.stderr,
        )
    return ""
