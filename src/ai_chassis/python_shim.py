"""The fleet Python floor interpreter and its ``~/.local/bin/python3`` shim (ISSUE-REF T6-1b).

install.sh's ``install-python-shim`` region, as the core ``python-shim`` step: a dependency
bootstrap in the zsh/gitleaks shape (C-8). It finds a Python at or above ``FLEET_PYTHON_FLOOR``,
and when there is none and uv is on PATH it provisions uv-managed CPython and writes the shim as
``~/.local/bin/python3`` and ``python``. Every arm warns and continues; the answer, which the
shell kept in ``_PYTHON_BIN``, may be empty.

The answer is a word the installer's shell runs, so it is the candidate as the shell named it:
``$HOME/.local/bin/python3`` (handed back home-relative, so the caller's shell expands its own
``$HOME``), or a bare ``python3``, ``python`` or ``py`` found on PATH.

A candidate must execute the floor probe, not just exist: on example-client-host Windows ``python3`` can resolve
only to the Microsoft Store app-execution stub, which hangs or fails when run non-interactively.
Under Git Bash the shim is a bash script, which Windows cannot start directly, so every candidate
there runs through ``bash`` (``command``), as the shell ran it.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

from . import dependency_bootstrap

#: The interpreter used for the harness's Python entry points. Do not lower this without lowering
#: every workspace member's requires-python and CI selection. Both ``3.14`` literals in the shim
#: bodies below must track it.
FLEET_PYTHON_FLOOR = "3.14"
#: The POSIX body's revision marker: the refresh regenerates any shim whose marker is missing or
#: older. Bump it whenever that body changes; without it, rev 1 could not be told from rev 2.
SHIM_REV = "3"
PROBE = (
    'import sys; floor = tuple(map(int, sys.argv[1].split("."))); '
    "raise SystemExit(0 if sys.version_info[:len(floor)] >= floor else 1)"
)
#: The probe has no bound in the shell; a hung Store stub must not hang the install.
PROBE_SECONDS = dependency_bootstrap.PROBE_SECONDS
PYTHON_INSTALL_SECONDS = 900.0
#: Searched after ``~/.local/bin/python3``, which is probed FIRST by absolute path: on example-client-host
#: Windows the Store stub can shadow it on PATH, so a bare-name lookup would never find an
#: already-installed shim and would reprovision it every run.
BARE_CANDIDATES = ("python3", "python", "py")
HOME_SHIM = "$HOME/.local/bin/python3"
EXPORT_NAME = "_PYTHON_BIN"

# On Windows the shim keeps `uv run`: the measurement that chose it there was about Microsoft
# Store stub shadowing and EDR warm-start, and the process-shape argument does not falsify it.
WINDOWS_BODY = """\
#!/usr/bin/env bash
# Real CPython shim — the OS `python3` on example-client-host Windows is the Microsoft Store stub.
exec uv run --no-project --python 3.14 python "$@"
"""
# On Linux and macOS the shim execs the resolved interpreter: `uv run` spawns it and waits, so a
# caller that backgrounds `python3` would hold uv's pid while the interpreter ran one level down
# (ai-cli ISSUE-REF). The target is uv's `python3.14` symlink, which survives a patch upgrade.
POSIX_BODY = """\
#!/usr/bin/env bash
# aih-shim-rev: 3
# Real CPython shim — the OS `python3` here is below the fleet Python floor.
# Exec the interpreter rather than spawning it under `uv run`, which waits on a child, so
# the pid a caller backgrounds is uv's and not the interpreter's.
#
# The rungs below REPRODUCE what `uv run --no-project` actually resolved, measured
# 2026-09-24: an active VIRTUAL_ENV first, then the nearest `.venv` at or above the working
# directory (uv discovers one even under --no-project), then the uv-managed interpreter.
# Keeping only the last rung silently removed every third-party import from a bare `python3`
# call, which is not a shape change but a dependency change.
#
# The walk uses parameter expansion, never `dirname`: a helper per level would reintroduce
# exactly the spawns this shim exists to remove.
if [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
  exec "$VIRTUAL_ENV/bin/python" "$@"
fi
_aih_dir="$PWD"
while :; do
  if [[ -x "$_aih_dir/.venv/bin/python" ]]; then
    exec "$_aih_dir/.venv/bin/python" "$@"
  fi
  [[ -z "$_aih_dir" ]] && break
  _aih_dir="${_aih_dir%/*}"
done
# python3.14 is a uv-maintained symlink, so this survives a patch upgrade. It sits beside this
# shim, so it is found from the shim's own path: a caller exporting another HOME must still
# get the interpreter, not the `uv run` rung below.
_aih_bin="${BASH_SOURCE[0]%/*}"
if [[ -x "$_aih_bin/python3.14" ]]; then
  exec "$_aih_bin/python3.14" "$@"
fi
exec uv run --no-project --python 3.14 python "$@"
"""
_CURRENT = re.compile(rf"^# aih-shim-rev: {SHIM_REV}$", re.MULTILINE)
_OURS = re.compile(r"^exec uv run --no-project|^# aih-shim-rev: ", re.MULTILINE)

#: The full apply's answer once the step has run in this process; None until then.
_answer: str | None = None


def git_bash(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this runs under Git Bash, MSYS or Cygwin, where the shim is a bash script."""
    msystem = (os.environ if environ is None else environ).get("MSYSTEM", "")
    return msystem.startswith(dependency_bootstrap._WINDOWS_SHELLS)


def command(interpreter: str) -> list[str]:
    """The argv prefix that runs ``interpreter`` as the installer's shell would."""
    if git_bash():
        return ["bash", "-c", '"$0" "$@"', interpreter]
    return [interpreter]


def answer() -> str | None:
    """What the step resolved in this process, or None when it has not run here."""
    return _answer


def reset() -> None:
    global _answer
    _answer = None


def meets_floor(interpreter: str) -> bool:
    """``interpreter`` runs and reports a version at or above the floor (the shell's
    ``_python_meets_fleet_floor``): a candidate that reports a version but fails this is skipped."""
    result = dependency_bootstrap.run_bounded(
        [*command(interpreter), "-c", PROBE, FLEET_PYTHON_FLOOR], seconds=PROBE_SECONDS
    )
    return result.returncode == 0


def _present(candidate: str) -> bool:
    if os.path.isabs(candidate):
        return os.access(candidate, os.X_OK)
    return shutil.which(candidate) is not None


def _shim_paths(home: Path) -> tuple[Path, Path]:
    bin_dir = home / ".local" / "bin"
    return bin_dir / "python3", bin_dir / "python"


def _make_executable(path: Path) -> None:
    """``chmod +x``: execute for each class the umask allows."""
    mask = os.umask(0)
    os.umask(mask)
    path.chmod(path.stat().st_mode | (0o111 & ~mask))


def write_shim(path: Path) -> None:
    """The shell's ``_write_python_shim``: this platform's body, then ``chmod +x``."""
    path.write_text(WINDOWS_BODY if git_bash() else POSIX_BODY, encoding="utf-8")
    _make_executable(path)


def _copy_to_python(shim: Path, python: Path) -> None:
    shutil.copyfile(shim, python)
    _make_executable(python)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def refresh(home: Path) -> None:
    """Repoint a shim this installer wrote with an older body (``_refresh_python_shim``).

    The selection probes ``~/.local/bin/python3`` first, so an old shim passes the floor and the
    provisioning never runs; only this reaches an existing machine. It refuses anything it did not
    write, verifies the new body against the floor probe, and restores the old one if it fails.
    """
    shim, python = _shim_paths(home)
    if not os.access(shim, os.X_OK) or git_bash():
        return
    body = _read(shim)
    if _CURRENT.search(body) or not _OURS.search(body):
        return
    resolved = shim.parent / f"python{FLEET_PYTHON_FLOOR}"
    if not os.access(resolved, os.X_OK):
        return
    previous = shim.with_name(shim.name + ".aih-prev")
    shutil.copy(shim, previous)
    write_shim(shim)
    if meets_floor(shim.as_posix()):
        _copy_to_python(shim, python)
        previous.unlink()
        print(
            f"  [ok]  python3 shim → resolved CPython {FLEET_PYTHON_FLOOR}, no uv run layer "
            "(~/.local/bin/python3)"
        )
        return
    previous.replace(shim)
    _make_executable(shim)
    print(
        f"  [warn] python3 shim left on uv run: {resolved} failed the {FLEET_PYTHON_FLOOR} probe",
        file=sys.stderr,
    )


def _select(home: Path) -> tuple[str, str]:
    """The first candidate meeting the floor: (the shell's word for it, this process's path)."""
    shim, _python = _shim_paths(home)
    if _present(str(shim)) and meets_floor(shim.as_posix()):
        return HOME_SHIM, shim.as_posix()
    for name in BARE_CANDIDATES:
        if _present(name) and meets_floor(name):
            return name, name
    return "", ""


def _provision(home: Path) -> tuple[str, str]:
    """No candidate: provision the floor with uv and write the shim (no admin, no Store)."""
    print(
        f"  [py]  no Python >= {FLEET_PYTHON_FLOOR} found — provisioning uv-managed CPython "
        f"{FLEET_PYTHON_FLOOR}..."
    )
    dependency_bootstrap.run_bounded(
        ["uv", "python", "install", FLEET_PYTHON_FLOOR], seconds=PYTHON_INSTALL_SECONDS
    )
    shim, python = _shim_paths(home)
    shim.parent.mkdir(parents=True, exist_ok=True)
    write_shim(shim)
    _copy_to_python(shim, python)
    # The shim's ABSOLUTE path, never the bare name: on a example-client-host Cloud PC WindowsApps precedes
    # ~/.local/bin on PATH, so a bare `python3` keeps resolving to the Store stub.
    if not meets_floor(shim.as_posix()):
        return "", ""
    print(
        f"  [ok]  python3 shim → uv-managed CPython {FLEET_PYTHON_FLOOR} (~/.local/bin/python3)"
    )
    return HOME_SHIM, shim.as_posix()


def ensure(home: Path) -> str:
    """Find or provision the floor interpreter, refresh the shim, and return the shell's word."""
    global _answer
    word, path = _select(home)
    if not word and shutil.which("uv"):
        word, path = _provision(home)
    # Runs whether or not provisioning did: provisioning skips a machine whose shim already works.
    refresh(home)
    _answer = path
    return word
