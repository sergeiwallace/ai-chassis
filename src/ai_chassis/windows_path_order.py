"""install.sh's retired Step 1c (IN-15): put ``~/.local/bin`` ahead of the Windows ``python3`` stub.

On Windows the Microsoft Store App Execution Alias (``WindowsApps/python3.exe``) is a decoy that
prints an install prompt and exits non-zero. When it precedes the real ``~/.local/bin/python3`` on
PATH, ``python3`` resolves but does not run, so every hook that invokes it fails. The wrong order is
detected by EXECUTION, the User PATH is backed up, reordered with ``~/.local/bin`` first and
deduplicated, and the new value is queued as one ``setx PATH`` in the core's one batched
``cmd.exe`` (:mod:`ai_chassis.profile`). Under install.sh the queued write is handed back as a
``_queue_user_env`` line, and so is the session's own PATH, as the shell's ``export PATH`` did.

The guard compares UNIQUE entry counts and runs before anything is queued, so a PATH that would
lose an entry is refused and nothing of this step's is queued (ISSUE-REF: the shell compared the
raw old count, duplicates included, against the deduplicated new one, so a duplicate entry printed
a false error, and its refusal cleared a variable that does not exist, so the write went ahead).
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

from .profile import (
    ProfileMigrationError,
    _is_windows_shell,
    _persist_windows_user_env,
    _windows_paths,
)

SKIP_ENV = "SKIP_PYTHON_PATH_FIX"
EXPORTS_FILE_ENV = "AI_HARNESS_EXPORTS_FILE"
USER_PATH_QUERY = '[Environment]::GetEnvironmentVariable("Path", "User")'
#: install.sh's ``export PATH="$HOME/.local/bin:$PATH"``, handed back for its own shell.
SESSION_PATH_EXPORT = 'export PATH="$HOME/.local/bin:$PATH"'
#: The bound on each probe and on the registry read; the shell bounded none of them.
PROBE_TIMEOUT_S = 60

NO_FIX = "  [ok]  python3 execution works — PATH order correct or python3 not in ~/.local/bin"
FIXING = "  [fix] WindowsApps python3 stub shadows ~/.local/bin — reordering User PATH"


def _runs(argv: list[str], environ: Mapping[str, str]) -> bool:
    try:
        return (
            subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=dict(environ),
                check=False,
                timeout=PROBE_TIMEOUT_S,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def needs_fix(home: Path, environ: Mapping[str, str]) -> bool:
    """``python3`` on PATH fails to execute while ``~/.local/bin/python3`` runs."""
    probe = ["-c", "import sys; sys.exit(0)"]
    if _runs(["python3", *probe], environ):
        return False
    local = home / ".local" / "bin" / "python3"
    return os.access(local, os.X_OK) and _runs([str(local), *probe], environ)


def read_user_path(environ: Mapping[str, str]) -> str:
    """The User PATH from the registry, through PowerShell, as the shell's ``$(... | tr -d '\\r')``.

    A read that fails stops the step, as the shell's ``set -e`` stopped the install there.
    """
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", USER_PATH_QUERY],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=dict(environ),
            check=False,
            timeout=PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProfileMigrationError(
            f"windows-path-order: could not read the User PATH with powershell.exe: {exc}"
        ) from exc
    if done.returncode != 0:
        raise ProfileMigrationError(
            f"windows-path-order: powershell.exe exited {done.returncode} reading the User PATH"
        )
    return done.stdout.replace("\r", "").rstrip("\n")


def _key(entry: str) -> str:
    return entry.replace("/", "\\").lower()


def local_bin_entry(home: Path) -> str:
    """``cygpath -w "$HOME/.local/bin"``, falling back to the path itself, as the shell did."""
    local = home / ".local" / "bin"
    try:
        return _windows_paths([local])[0]
    except ProfileMigrationError:
        return str(local)


def _own_keys(local_bin: str, home: Path) -> set[str]:
    """``~/.local/bin`` in the shell's ``$HOME\\.local\\bin`` spelling and the one it prepends."""
    return {_key(local_bin), _key(f"{home}\\.local\\bin")}


def reorder(entries: list[str], local_bin: str, home: Path) -> list[str]:
    """``local_bin`` first, then every other entry once, in order, first spelling kept.

    An existing ``~/.local/bin`` entry (:func:`_own_keys`) is dropped for the one prepended.
    """
    own = _own_keys(local_bin, home)
    seen: set[str] = set()
    kept: list[str] = []
    for entry in entries:
        key = _key(entry)
        if not entry or key in own or key in seen:
            continue
        seen.add(key)
        kept.append(entry)
    return [local_bin, *kept]


def _unique(entries: list[str]) -> int:
    return len({_key(entry) for entry in entries if entry})


def _hand_back_session_path(environ: Mapping[str, str]) -> None:
    target = environ.get(EXPORTS_FILE_ENV)
    if not target:
        return
    try:
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write(f"{SESSION_PATH_EXPORT}\n")
    except OSError as exc:
        raise ProfileMigrationError(
            f"could not write exports file {target}: {exc}"
        ) from exc


def fix_path_order(home: Path, environ: Mapping[str, str]) -> int:
    """The step. Every arm exits 0 except a failed registry read or backup write, which stopped
    the shell's install under ``set -e``; a refused reorder is the shell's error-and-continue."""
    if not _is_windows_shell(environ) or environ.get(SKIP_ENV, "0") == "1":
        return 0
    if not needs_fix(home, environ):
        print(NO_FIX)
        return 0
    print(FIXING)
    raw = read_user_path(environ)
    backup = (
        home
        / ".local"
        / "state"
        / f"ai-harness-user-path-backup-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    )
    try:
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(f"{raw}\n", encoding="utf-8")
    except OSError as exc:
        raise ProfileMigrationError(
            f"windows-path-order: could not write the User PATH backup {backup}: {exc}"
        ) from exc
    print(f"        Backup: {backup}")
    old = raw.split(";")
    local_bin = local_bin_entry(home)
    new = reorder(old, local_bin, home)
    found_local = any(_key(entry) in _own_keys(local_bin, home) for entry in old)
    old_count = _unique(old)
    new_count = _unique(new)
    expected = old_count if found_local else old_count + 1
    if new_count < expected:
        print(
            f"        [ERROR] PATH entry count mismatch (old={old_count} new={new_count} "
            f"expected={expected}) — not persisting"
        )
        print(f"                Backup preserved at {backup}")
        return 0
    _persist_windows_user_env("PATH", ";".join(new))
    _hand_back_session_path(environ)
    print("        [ok]   PATH reordered: ~/.local/bin now precedes WindowsApps")
    print(f"        [ok]   Entry count: {new_count} (expected {expected})")
    return 0
