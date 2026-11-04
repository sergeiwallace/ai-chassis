"""``~/.claude/state/context-supervisor-config.json``, seeded and migrated (ISSUE-REF; T6-1b).

install.sh's context-supervisor block, as the core ``context-supervisor-config`` step. D-35 makes
injection and fleet enrolment the runtime defaults, and they are materialized here so operators and
tools can read them without knowing the Python fallback. ``scripts/provision_supervisor_config.py``
three-way merges each untouched key against the
digest and values recorded when the file was seeded, and leaves every operator-changed key alone.
A machine profile's ``launch_resume`` overrides (the checkout's ``config/launch_resume_profiles.toml``,
ISSUE-REF) are overlaid first.

The merge needs a real interpreter: the fleet-floor one the python-shim step resolved, handed to
this step as ``AIH_PYTHON_BIN``. Resolving it again here is the G105 interpreter-class bug. When
it is empty, the shipped defaults are copied create-only, so the file still exists. A merge or a
creation that fails stops the install, as the shell's ``_exit_reporting_deferred`` did.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import python_shim

DEFAULTS = (
    Path(__file__).resolve().parent / "defaults" / "context-supervisor-config.json"
)
#: The merge ships beside the core, as `scripts/plugin_skill_overrides.py` does for the plugins
#: step: under install.sh's editable tool this is `$SCRIPT_DIR/scripts`, the file the shell ran.
PROVISIONER = (
    Path(__file__).resolve().parents[2] / "scripts" / "provision_supervisor_config.py"
)
PROFILES = Path("config") / "launch_resume_profiles.toml"
#: The merge only reads and writes one small file; the shell gave it no bound.
MERGE_SECONDS = 120.0


def config_path(home: Path) -> Path:
    return home / ".claude" / "state" / "context-supervisor-config.json"


def _private_mkdir(path: Path) -> None:
    """``(umask 077; mkdir -p ...)``: every directory it makes is owner-only."""
    previous = os.umask(0o077)
    try:
        path.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(previous)


def _merge(python: str, checkout: Path, config: Path, machine: str) -> int:
    argv = [
        *python_shim.command(python),
        str(PROVISIONER),
        "--config",
        str(config),
        "--defaults",
        str(DEFAULTS),
    ]
    profiles = checkout / PROFILES
    if profiles.is_file() and machine:
        argv += ["--profiles-file", str(profiles), "--machine-profile", machine]
    try:
        code = subprocess.run(argv, check=False, timeout=MERGE_SECONDS).returncode
    except (OSError, subprocess.TimeoutExpired):
        code = 1
    if code:
        print(f"  [FAIL] could not provision {config}", file=sys.stderr)
        return 1
    return 0


def _seed(config: Path) -> int:
    """Copy the shipped defaults create-only (the shell's ``noclobber``), owner-only."""
    floor = python_shim.FLEET_PYTHON_FLOOR
    if os.path.lexists(config):
        print(
            f"  [ok]  {config} (preserved; no python3 >= {floor} to migrate defaults)"
        )
        return 0
    previous = os.umask(0o077)
    try:
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(descriptor, "wb") as target:
            target.write(DEFAULTS.read_bytes())
    except OSError:
        # Another concurrent installer won the race: the path now belongs to its creator.
        if os.path.lexists(config):
            print(f"  [ok]  {config} (preserved)")
            return 0
        print(f"  [FAIL] could not create {config}", file=sys.stderr)
        return 1
    finally:
        os.umask(previous)
    print(
        f"  [ok]  {config} (created without a provenance marker; no python3 >= {floor})"
    )
    return 0


def provision(home: Path, checkout: Path, python: str, machine: str) -> int:
    """Seed or migrate the config; 1 when it could not be provisioned."""
    config = config_path(home)
    _private_mkdir(config.parent)
    if python:
        return _merge(python, checkout, config, machine)
    return _seed(config)
