"""Enable a clean daemon at user-manager startup; never recycle a live daemon.

Claude 2.1.310 reuses a responsive daemon before its transient spawn path.
Starting it without task-list or host identity prevents omitted dispatch keys
from inheriting the first client's identity. See the adopted-spare bug doc.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from .settings_merge import SettingsMergeError, _write_if_changed
from .storage import Runner

UNIT = "ai-harness-claude-daemon.service"


def render_unit(root: Path, claude: Path) -> str:
    # systemd has its own quoting and specifiers; shell quoting is insufficient.
    binary = str(claude).replace("\\", "\\\\").replace('"', '\\"')
    binary = '"' + binary.replace("%", "%%").replace("$", "$$") + '"'
    return (
        (root / "systemd" / UNIT)
        .read_text(encoding="utf-8")
        .replace("__CLAUDE__", binary)
    )


def install_unit(
    root: Path,
    *,
    run: Runner | None = None,
    unit_dir: Path | None = None,
    system: str | None = None,
    claude: Path | None = None,
) -> int:
    """Enable for the next manager startup, with no start/stop/restart operation.

    A missing binary or user manager leaves the host untouched. Installer
    failures report only the operation and exit code, never manager environment
    or subprocess output. Live activation requires the coordinated runbook.
    """
    if (system or platform.system()) != "Linux":
        print("  [skip] clean Claude daemon: requires a systemd user manager")
        return 0
    binary = str(claude) if claude else shutil.which("claude")
    if not binary or not (root / "systemd" / UNIT).is_file():
        print("  [skip] clean Claude daemon: no Claude executable or unit template")
        return 0
    runner = run or subprocess.run

    def user(*args: str) -> int:
        try:
            return runner(
                ["systemctl", "--user", *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            ).returncode
        except FileNotFoundError:
            return 127

    failed = 0
    try:
        if user("show", "--property=Version", "--value"):
            print("  [warn] clean Claude daemon: user manager unavailable")
            return 0
        directory = (
            unit_dir
            or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            / "systemd/user"
        )
        changed = _write_if_changed(directory / UNIT, render_unit(root, Path(binary)))
        if changed and user("daemon-reload"):
            print("  [FAIL] clean Claude daemon: daemon-reload failed")
            failed = 1
        elif user("enable", UNIT):
            print("  [FAIL] clean Claude daemon: enable failed")
            failed = 1
    except (OSError, subprocess.SubprocessError, SettingsMergeError):
        print("  [FAIL] clean Claude daemon: installation unavailable")
        failed = 1
    if not failed:
        print(
            "  [ok] clean Claude daemon: enabled for next user-manager startup; "
            "live activation requires coordinated recover-identity runbook"
        )
    return failed
