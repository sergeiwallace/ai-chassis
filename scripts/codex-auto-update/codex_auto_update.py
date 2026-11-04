#!/usr/bin/env python3
# ruff: noqa: E402
"""Run ``codex update`` only while no Codex dispatch is active."""

from __future__ import annotations

import os as _bootstrap_os
import sys as _bootstrap_sys

_bootstrap_directory = _bootstrap_os.path.dirname(_bootstrap_os.path.realpath(__file__))
while not _bootstrap_os.path.isfile(
    _bootstrap_os.path.join(_bootstrap_directory, "pyproject.toml")
):
    _bootstrap_parent = _bootstrap_os.path.dirname(_bootstrap_directory)
    if _bootstrap_parent == _bootstrap_directory:
        break
    _bootstrap_directory = _bootstrap_parent
_bootstrap_sys.path.insert(0, _bootstrap_os.path.join(_bootstrap_directory, "scripts"))
try:
    from py_interpreter_bootstrap import ensure_supported_python  # noqa: E402
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):  # noqa: E402
        """Stand in for the bootstrap when this file was copied without it.

        A partial copy cannot re-exec, because the module that knows how is the one
        missing. Staying silent when the running interpreter ALREADY meets the floor is
        correct rather than lax: there was nothing to enforce. Below the floor there is
        no remedy left to apply, so say so and let the real failure happen where it can
        be read, instead of dying here on an import that is not the problem.
        """
        import sys as _sys

        if _sys.version_info < minimum:
            _sys.stderr.write(
                "warning: {} is running under Python {} without its interpreter "
                "bootstrap, so the {}.{}+ floor could not be enforced\n".format(
                    __file__,
                    ".".join(str(p) for p in _sys.version_info[:3]),
                    minimum[0],
                    minimum[1],
                )
            )


ensure_supported_python()

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def _state_dir() -> Path:
    override = os.environ.get("AIH_CODEX_AUTO_UPDATE_STATE_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / (
            "ai-harness/codex-auto-update"
        )
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / (
        "ai-harness/codex-auto-update"
    )


def _command_parts(command: str) -> list[str]:
    try:
        import shlex

        return shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return command.split()


def _is_codex_dispatch(command: str) -> str | None:
    for part in _command_parts(command):
        name = Path(part).name.lower()
        if name in {"codex", "codex.exe"}:
            return "codex"
        if name in {"cx", "cx.exe"}:
            return "cx"
    return None


def _active_processes() -> list[dict[str, int | str]]:
    if os.name == "nt":
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | "
                "ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        records = json.loads(result.stdout)
        if isinstance(records, dict):
            records = [records]
        return [
            {"pid": int(record["ProcessId"]), "command": detected}
            for record in records
            if isinstance(record, dict)
            and isinstance(record.get("CommandLine"), str)
            and (detected := _is_codex_dispatch(record["CommandLine"])) is not None
        ]

    # pgrep's "print the full argument list too" flag is NOT portable, and getting
    # it wrong fails silently rather than loudly. procps (Linux) spells it `-a`
    # (`--list-full`); BSD/macOS pgrep spells it `-l` and gives `-a` an unrelated
    # meaning -- "include process ancestors in the match list" (man pgrep, example-client-host-mac
    # 2026-09-25). Under `-a` on macOS pgrep prints BARE PIDS, so every
    # `line.partition(" ")` below finds no separator and this function returns an
    # empty list on every run. The updater then reports no Codex dispatch active
    # and runs `codex update` in the middle of a live one, which is the single
    # thing the check exists to prevent.
    #
    # Two literal call sites rather than one with a computed flag: the AST safety
    # inventory in tests/test_git_review_ship_merge.py reviews subprocess calls by
    # content digest, so a literal argv keeps each branch individually reviewable
    # and leaves the already-reviewed Linux call byte-identical.
    if sys.platform == "darwin":
        result = subprocess.run(
            ["pgrep", "-l", "-f", "."], capture_output=True, text=True, check=False
        )
    else:
        result = subprocess.run(
            ["pgrep", "-a", "-f", "."], capture_output=True, text=True, check=False
        )
    if result.returncode not in {0, 1}:
        return []
    active = []
    for line in result.stdout.splitlines():
        pid, separator, command = line.partition(" ")
        detected = _is_codex_dispatch(command) if separator else None
        if detected is not None and pid.isdigit() and int(pid) != os.getpid():
            active.append({"pid": int(pid), "command": detected})
    return active


def _search_paths() -> tuple[str, str]:
    """``(lookup PATH, run PATH)`` for codex under a bare service PATH (ISSUE-REF).

    The systemd user manager starts this unit with a fixed PATH that holds neither the
    user's ``~/.local/bin`` (npm, node) nor the npm global prefix (codex), so a bare
    ``shutil.which`` failed every night on example-project-ec2. npm is asked for its own prefix, so a
    relocated prefix (``/data/npm-global`` there) is found without hardcoding it.

    The lookup keeps the inherited PATH first, so an explicit PATH still decides which codex
    runs. The run PATH puts the user's bins first, as their login shell does, so codex's
    ``#!/usr/bin/env node`` launcher gets the user's node rather than an older system one
    that ``/usr/bin`` may also hold.
    """
    inherited = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    user = [str(Path.home() / ".local" / "bin")]
    npm = shutil.which("npm", path=os.pathsep.join(user + inherited))
    if npm is not None:
        try:
            prefix = subprocess.run(
                [npm, "config", "get", "prefix"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=os.environ | {"PATH": os.pathsep.join(user + inherited)},
            )
        except (OSError, subprocess.TimeoutExpired):
            prefix = None
        if prefix is not None and prefix.returncode == 0 and prefix.stdout.strip():
            user.insert(0, str(Path(prefix.stdout.strip()) / "bin"))
    lookup = os.pathsep.join(dict.fromkeys(inherited + user))
    run = os.pathsep.join(dict.fromkeys(user + inherited))
    return lookup, run


QUARANTINE_ATTRIBUTE = "com.apple.quarantine"
#: Gatekeeper's "the user clicked Open" bit; the same constant Homebrew's cask/quarantine.rb uses.
USER_APPROVED_FLAG = 0x0040
#: A Developer ID Application signature from OpenAI's team (`codesign -dv`: 2DC432GLL2), the
#: signer of every Mach-O in the codex cask. Apple platform binaries and unsigned files fail it.
OPENAI_DEVELOPER_ID = (
    "=anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists "
    "and certificate leaf[field.1.2.840.113635.100.6.1.13] exists "
    'and certificate leaf[subject.OU] = "2DC432GLL2"'
)
_MACH_O_MAGICS = {
    bytes.fromhex(magic)
    for magic in (
        "feedface",
        "feedfacf",
        "cefaedfe",
        "cffaedfe",
        "cafebabe",
        "bebafeca",
    )
}


def _cask_payload(executable: str) -> Path | None:
    """The versioned ``Caskroom/codex/<version>`` directory ``executable`` resolves into."""
    for directory in Path(executable).resolve().parents:
        if (
            directory.parent.name == "codex"
            and directory.parent.parent.name == "Caskroom"
        ):
            return directory
    return None


def _is_mach_o(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) in _MACH_O_MAGICS
    except OSError:
        return False


def approve_cask_payload(executable: str) -> dict[str, object] | None:
    """Mark the codex cask's OpenAI-signed code Gatekeeper-approved (ISSUE-REF).

    Homebrew Cask quarantines every file it unpacks and, since Homebrew 4.7, offers no
    ``--no-quarantine``. On upgrade it carries the user's earlier approval forward for ``app``
    artifacts only; codex ships ``binary`` artifacts, so every upgrade leaves a fresh, unapproved
    payload and the first exec of each helper raises "codex-code-mode-host is an app downloaded
    from the Internet". This sets the approved bit Homebrew's own ``inherit_user_approval!`` sets,
    which keeps the quarantine provenance, and only on a Mach-O whose signature verifies as
    OpenAI's Developer ID. Anything else keeps its record untouched.
    """
    if sys.platform != "darwin":
        return None
    payload = _cask_payload(executable)
    if payload is None:
        return None
    # One recursive read: /usr/bin/xattr is a Python script, about 0.1 s per process.
    listing = subprocess.run(
        ["/usr/bin/xattr", "-r", "-p", QUARANTINE_ATTRIBUTE, str(payload)],
        capture_output=True,
        text=True,
        check=False,
    )
    counts = {"approved": 0, "already_approved": 0, "not_openai_code": 0, "failed": 0}
    for line in listing.stdout.splitlines():
        name, separator, record = line.rpartition(": ")
        path = Path(name)
        if not separator or path.is_symlink() or not path.is_file():
            continue
        flags, _, rest = record.partition(";")
        try:
            value = int(flags, 16)
        except ValueError:
            continue
        if value & USER_APPROVED_FLAG:
            counts["already_approved"] += 1
            continue
        verified = _is_mach_o(path) and (
            subprocess.run(
                [
                    "/usr/bin/codesign",
                    "--verify",
                    "--strict",
                    "-R",
                    OPENAI_DEVELOPER_ID,
                    str(path),
                ],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        if not verified:
            counts["not_openai_code"] += 1
            continue
        approved = f"{value | USER_APPROVED_FLAG:0{len(flags)}x};{rest}"
        written = subprocess.run(
            ["/usr/bin/xattr", "-w", QUARANTINE_ATTRIBUTE, approved, str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        counts["approved" if written.returncode == 0 else "failed"] += 1
    return {"payload": str(payload), **counts}


def _write_event(event: dict[str, object]) -> None:
    directory = _state_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    target = directory / "codex-auto-update.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)


def approve_only() -> int:
    """``--approve-cask-payload``: what install-scheduler.sh runs on every install.sh pass."""
    executable = shutil.which("codex", path=_search_paths()[0])
    result = approve_cask_payload(executable) if executable else None
    if result is None:
        print(
            "  [ok]  codex is not a Homebrew cask install; no Gatekeeper approval needed"
        )
        return 0
    print(
        f"  [ok]  codex cask Gatekeeper approval: {result['approved']} approved, "
        f"{result['already_approved']} already approved, "
        f"{result['not_openai_code']} left quarantined (not OpenAI-signed code) "
        f"in {result['payload']}"
    )
    if result["failed"]:
        print(
            f"  [warn] could not approve {result['failed']} file(s) in {result['payload']}; "
            "Gatekeeper may prompt on their first launch",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    if sys.argv[1:] == ["--approve-cask-payload"]:
        return approve_only()
    event: dict[str, object] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "command": ["codex", "update"],
    }
    active = (
        []
        if os.environ.get("AIH_CODEX_AUTO_UPDATE_SKIP_ACTIVE_CHECK") == "1"
        else _active_processes()
    )
    if active:
        event.update({"outcome": "skipped-active-process", "processes": active})
        _write_event(event)
        print("Codex auto-update skipped: a codex/cx process is active.")
        return 0

    search, run_path = _search_paths()
    executable = shutil.which("codex", path=search)
    if executable is None:
        event["outcome"] = "codex-not-found"
        _write_event(event)
        print(
            f"Codex auto-update failed: codex is not on PATH (searched {search}).",
            file=sys.stderr,
        )
        return 1

    result = subprocess.run(
        [executable, "update"], check=False, env=os.environ | {"PATH": run_path}
    )
    event.update(
        {
            "outcome": "updated" if result.returncode == 0 else "update-failed",
            "exit_code": result.returncode,
        }
    )
    # Re-resolved: a cask upgrade repoints the codex link at the new version's payload.
    upgraded = shutil.which("codex", path=search)
    gatekeeper = approve_cask_payload(upgraded) if upgraded else None
    if gatekeeper is not None:
        event["gatekeeper"] = gatekeeper
    _write_event(event)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
