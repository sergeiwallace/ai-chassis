"""Shell-profile migrations performed during ``ai-harness apply``."""

from __future__ import annotations

import os
import platform
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


class ProfileMigrationError(RuntimeError):
    """A required shell-profile mutation could not be completed."""


_SHELL_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


#: A value the shell must expand when it reads the line: ``$HOME`` and plain path segments only,
#: so writing it unquoted cannot split or inject.
_HOME_RELATIVE_VALUE = re.compile(r"\$HOME(?:/[A-Za-z0-9._-]+)*\Z")


def _export_assignment(name: str, value: str, *, home_relative: bool = False) -> str:
    """Render a shell-safe export assignment for trusted variable names.

    ``home_relative`` keeps a ``$HOME/...`` value unquoted, so each shell that reads the line
    expands its own ``$HOME`` (install.sh's ``export XDG_STATE_HOME=$HOME/.local/state``); a
    quoted value would export the literal text ``$HOME``.
    """
    if not _SHELL_VARIABLE_NAME.fullmatch(name):
        raise ProfileMigrationError(f"invalid shell variable name: {name!r}")
    if home_relative:
        if not _HOME_RELATIVE_VALUE.fullmatch(value):
            raise ProfileMigrationError(
                f"{name}: {value!r} is not a $HOME-relative path"
            )
        return f"export {name}={value}"
    return f"export {name}={shlex.quote(value)}"


def _login_shell(values: Mapping[str, str]) -> str:
    """Resolve ``$SHELL`` first, with the POSIX account record as a fallback."""
    if shell := values.get("SHELL", ""):
        return shell
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_shell
    except (AttributeError, ImportError, KeyError, OSError):
        return ""


#: The MSYS registry mirror install.sh's ``_queue_user_env`` read before queueing a ``setx``,
#: and the runner the one batched ``cmd.exe`` launch goes through; module attributes so the tests
#: point both at fixtures.
WINDOWS_USER_ENV_ROOT = Path("/proc/registry/HKEY_CURRENT_USER/Environment")
WINDOWS_USER_ENV_RUN = subprocess.run
#: The bound on that one launch: a `cmd /c` batch of `setx` lines finishes in seconds.
WINDOWS_USER_ENV_TIMEOUT_S = 60
#: User-environment writes queued this run, flushed once by :func:`flush_windows_user_env`.
_PENDING_WINDOWS_USER_ENV: dict[str, str] = {}
#: Directory junctions queued this run, link to target, flushed in the same one batch.
_PENDING_WINDOWS_JUNCTIONS: dict[Path, Path] = {}
#: The runner for the one ``cygpath -w`` a standalone flush makes under a POSIX (MSYS) interpreter,
#: as install.sh's ``create_junction_win`` converted each path; a native Windows interpreter's
#: paths are already Windows paths, so there it launches nothing.
WINDOWS_PATH_RUN = subprocess.run
WINDOWS_PATH_TIMEOUT_S = 30
#: The batch variable a standalone flush's ops set when one of them fails.
_NATIVE_FAILED = "AIH_NATIVE_FAILED"
#: Set by a standalone CLI for a child step (ISSUE-REF): the child's flush writes its ops here,
#: in the exports-file line format, and the CLI adopts them into its own one batch.
NATIVE_OPS_FILE_ENV = "AI_HARNESS_NATIVE_OPS_FILE"


def _is_windows_shell(values: Mapping[str, str]) -> bool:
    return values.get("MSYSTEM", "").upper().startswith(("MINGW", "MSYS", "CYGWIN"))


def profile_targets(home: Path, values: Mapping[str, str]) -> tuple[Path, ...]:
    """install.sh's selection: ``.bashrc`` on an MSYS shell, ``.zshenv`` for a zsh login shell,
    and both ``.bashrc`` and ``.zshenv`` otherwise.

    The pair on a bash-login host is for ``ai c``, which runs every session under zsh whatever
    the login shell is, so an export only in ``.bashrc`` never reaches a session (ISSUE-REF).
    Writing ``.zshenv`` ALONE there was the earlier bug: the bash login shell never read it.
    """
    if _is_windows_shell(values):
        return (home / ".bashrc",)
    if _login_shell(values).endswith("/zsh"):
        return (home / ".zshenv",)
    return (home / ".bashrc", home / ".zshenv")


def _persist_windows_user_env(name: str, value: str) -> None:
    """Queue a Windows user-environment write unless the registry mirror already holds it.

    install.sh batched every such write into ONE ``cmd.exe`` launch (``_queue_user_env`` then
    ``_flush_win_cmd``), because each native launch is an EDR-scanned cold start; this keeps that
    shape, so nothing is launched here (see :func:`flush_windows_user_env`).
    """
    try:
        current = (
            (WINDOWS_USER_ENV_ROOT / name).read_bytes().replace(b"\0", b"").decode()
        )
    except (OSError, UnicodeDecodeError):
        current = None
    if current != value:
        _PENDING_WINDOWS_USER_ENV[name] = value


def _points_at(link: Path, target: Path) -> bool:
    """Whether ``link`` is a junction or symlink to ``target``, read without following it."""
    try:
        current = os.readlink(link)
    except (OSError, ValueError):
        return False
    current = current.removeprefix("\\\\?\\")
    resolved = os.path.join(os.path.dirname(link), current)
    return os.path.normcase(os.path.realpath(resolved)) == os.path.normcase(
        os.path.realpath(target)
    )


def queue_windows_junction(target: Path, link: Path) -> bool:
    """Queue a directory junction at ``link`` to ``target``; False when it is already there.

    install.sh's ``create_junction_win``: the junction is made by the same one batched
    ``cmd.exe`` launch as the queued ``setx`` writes (see :func:`flush_windows_user_env`), so
    nothing is launched here. A link that already resolves to ``target`` queues nothing.
    """
    if _points_at(link, target):
        return False
    _PENDING_WINDOWS_JUNCTIONS[link] = target
    return True


def _queue_for_installer(
    exports_file: Path, pending: Mapping[str, str], junctions: Mapping[Path, Path]
) -> None:
    try:
        with exports_file.open("a", encoding="utf-8") as stream:
            for name, value in pending.items():
                stream.write(f"_queue_user_env {name} {shlex.quote(value)}\n")
            for link, target in junctions.items():
                stream.write(
                    f"create_junction_win {shlex.quote(str(target))} "
                    f"{shlex.quote(str(link))}\n"
                )
    except OSError as exc:
        raise ProfileMigrationError(
            f"could not write exports file {exports_file}: {exc}"
        ) from exc


def _windows_paths(paths: Sequence[Path]) -> list[str]:
    """``cygpath -w`` of each path: one launch for all of them, none on a native interpreter."""
    if os.name == "nt":
        return [os.path.abspath(path) for path in paths]
    try:
        converted = WINDOWS_PATH_RUN(
            ["cygpath", "-w", *(str(path) for path in paths)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=WINDOWS_PATH_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProfileMigrationError(
            f"could not convert the junction paths with cygpath -w: {exc}"
        ) from exc
    lines = converted.stdout.splitlines()
    if converted.returncode != 0 or len(lines) != len(paths):
        raise ProfileMigrationError(
            f"cygpath -w exited {converted.returncode} converting the junction paths "
            f"{', '.join(str(path) for path in paths)}"
        )
    return lines


def _run_native_batch(
    pending: Mapping[str, str], junctions: Mapping[Path, Path]
) -> None:
    """install.sh's ``_flush_win_cmd``: one ``cmd //c <batch>`` holding every queued op.

    A ``cmd.exe`` that exits non-zero fails, naming every queued op (IN-44); a launch that
    cannot start or outlives its bound is warned, as before.
    """
    ops = [f'setx {key} "{value}" >nul' for key, value in pending.items()]
    labels = list(pending)
    if junctions:
        converted = _windows_paths(
            [path for link, target in junctions.items() for path in (target, link)]
        )
        for index, (link, target) in enumerate(junctions.items()):
            win_target, win_link = converted[2 * index], converted[2 * index + 1]
            ops.append(
                f'rmdir "{win_link}" 2>nul & mklink /J "{win_link}" "{win_target}" >nul'
            )
            labels.append(f"junction {link} -> {target}")
    batch: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix="ai-harness-win-", suffix=".cmd")
        batch = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            # A `cmd /c` batch exits with its LAST command's status, so each op records its own
            # failure in a flag the last line exits with. cmd binds `||` tighter than `&`, so on a
            # junction line the suffix attaches to `mklink` only: the `rmdir` before it fails
            # whenever no link exists yet, which is expected.
            stream.write(f"@echo off\r\nset {_NATIVE_FAILED}=0\r\n")
            for op in ops:
                stream.write(f"{op} || set {_NATIVE_FAILED}=1\r\n")
            stream.write(f"exit /b %{_NATIVE_FAILED}%\r\n")
        completed = WINDOWS_USER_ENV_RUN(
            ["cmd", "/c", str(batch)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=WINDOWS_USER_ENV_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        if junctions:
            print(
                f"  [warn] could not run the batched cmd.exe for {', '.join(labels)}",
                file=sys.stderr,
            )
        else:
            print(
                f"  [warn] could not persist {', '.join(pending)} to the Windows user "
                "environment (setx)",
                file=sys.stderr,
            )
        return
    finally:
        if batch is not None:
            batch.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise ProfileMigrationError(
            f"the batched cmd.exe exited {completed.returncode}, so these Windows native "
            f"ops did not apply: {', '.join(labels)}"
        )


def flush_windows_user_env(exports_file: Path | None) -> None:
    """Hand this run's queued Windows-native ops on, in one batch.

    The ops are the queued user-environment writes and directory junctions. Under install.sh
    (``exports_file`` set) each becomes a ``_queue_user_env NAME VALUE`` or
    ``create_junction_win TARGET LINK`` line the installer sources into its own queue, which its
    single ``_flush_win_cmd`` call runs at the end of the install. A child step of a standalone
    CLI (``NATIVE_OPS_FILE_ENV`` set) writes the same lines to that file for the CLI to adopt
    with :func:`adopt_handed_back_ops`. Otherwise they run now as ONE ``cmd.exe`` launch of a
    batch file of ``setx`` and ``mklink /J`` lines, and a non-zero exit raises
    :class:`ProfileMigrationError` naming the ops.
    """
    pending = dict(_PENDING_WINDOWS_USER_ENV)
    _PENDING_WINDOWS_USER_ENV.clear()
    junctions = dict(_PENDING_WINDOWS_JUNCTIONS)
    _PENDING_WINDOWS_JUNCTIONS.clear()
    if not pending and not junctions:
        return
    for name in pending:
        if not _SHELL_VARIABLE_NAME.fullmatch(name):
            raise ProfileMigrationError(f"invalid shell variable name: {name!r}")
    handback = os.environ.get(NATIVE_OPS_FILE_ENV)
    if exports_file is not None:
        _queue_for_installer(exports_file, pending, junctions)
    elif handback:
        _queue_for_installer(Path(handback), pending, junctions)
    else:
        _run_native_batch(pending, junctions)


def adopt_handed_back_ops(path: Path) -> None:
    """Queue the ops a child step wrote to ``path``, so this process's one flush runs them.

    Each line is a ``_queue_user_env NAME VALUE`` or ``create_junction_win TARGET LINK`` line, as
    :func:`flush_windows_user_env` writes them. A missing file means the child queued nothing;
    any other line fails closed rather than dropping an op.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ProfileMigrationError(
            f"could not read handed-back Windows ops {path}: {exc}"
        ) from exc
    for line in lines:
        words = shlex.split(line)
        if words and not queue_handed_back_op(words):
            raise ProfileMigrationError(
                f"unrecognised handed-back Windows op in {path}: {line!r}"
            )


def pending_windows_ops() -> int:
    """How many Windows-native ops this run has queued and not yet flushed."""
    return len(_PENDING_WINDOWS_USER_ENV) + len(_PENDING_WINDOWS_JUNCTIONS)


def queue_handed_back_op(words: Sequence[str]) -> bool:
    """Queue one handed-back op line, split into ``words``; False when it is not one.

    The two shapes are :func:`flush_windows_user_env`'s: ``_queue_user_env NAME VALUE`` and
    ``create_junction_win TARGET LINK``. An op naming an invalid variable raises.
    """
    match list(words):
        case ["_queue_user_env", name, value]:
            if not _SHELL_VARIABLE_NAME.fullmatch(name):
                raise ProfileMigrationError(f"invalid shell variable name: {name!r}")
            _PENDING_WINDOWS_USER_ENV[name] = value
        case ["create_junction_win", target, link]:
            _PENDING_WINDOWS_JUNCTIONS[Path(link)] = Path(target)
        case _:
            return False
    return True


def _append_line(path: Path, line: str) -> None:
    """Append one line, first ending an unterminated last line (install.sh ISSUE-REF)."""
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    with path.open("a", encoding="utf-8") as stream:
        if content and not content.endswith("\n"):
            stream.write("\n")
        stream.write(f"{line}\n")


def persist_exported_var(
    name: str,
    value: str,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    targets: tuple[Path, ...] | None = None,
) -> tuple[Path, ...]:
    """Idempotently persist an export in each profile :func:`profile_targets` names.

    install.sh's ``_persist_exported_var``: presence-only per file, so any line mentioning the
    name counts as declared there and a hand-edited value is never duplicated or overwritten. On
    an MSYS shell the Windows user environment is written too. ``targets`` narrows the files to a
    subset the caller already selected from :func:`profile_targets`.
    """
    values = os.environ if environ is None else environ
    paths = (
        profile_targets(Path.home() if home is None else home, values)
        if targets is None
        else targets
    )
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8") if path.exists() else ""
            if name not in content:
                _append_line(path, _export_assignment(name, value))
        except OSError as exc:
            raise ProfileMigrationError(
                f"could not update shell profile {path}: {exc}"
            ) from exc
    if _is_windows_shell(values):
        _persist_windows_user_env(name, value)
    return paths


def persist_managed_exported_var(
    name: str,
    value: str,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    home_relative: bool = False,
) -> tuple[Path, ...]:
    """Converge one machine-managed export, install.sh's ``_persist_managed_exported_var``.

    Unlike :func:`persist_exported_var` the value is the installer's to set: in each profile
    :func:`profile_targets` names, the first ``export <name>=`` line becomes the current value,
    any later one is dropped, and a profile with none gains it at the end. A file is rewritten
    only when that changes its bytes. ``home_relative`` writes a ``$HOME/...`` value unquoted
    (see :func:`_export_assignment`).
    """
    values = os.environ if environ is None else environ
    paths = profile_targets(Path.home() if home is None else home, values)
    assignment = _export_assignment(name, value, home_relative=home_relative)
    declared = re.compile(rf"^[ \t]*export[ \t]+{re.escape(name)}=")
    for path in paths:
        try:
            current = path.read_text(encoding="utf-8") if path.exists() else None
            lines: list[str] = []
            written = False
            for line in (current or "").splitlines():
                if declared.match(line):
                    if not written:
                        lines.append(assignment)
                        written = True
                    continue
                lines.append(line)
            if not written:
                lines.append(assignment)
            rendered = "\n".join(lines) + "\n"
            if rendered != current:
                path.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            raise ProfileMigrationError(
                f"could not update shell profile {path}: {exc}"
            ) from exc
    if _is_windows_shell(values):
        _persist_windows_user_env(name, value)
    return paths


def append_export(
    path: Path, name: str, value: str, *, home_relative: bool = False
) -> None:
    """Append ``export <name>=<value>`` (shell-quoted) for a caller that sources ``path``.

    ``home_relative`` writes ``export <name>="$HOME/..."``, so the caller's own ``$HOME`` is the
    one expanded, as install.sh's ``export XDG_STATE_HOME="$HOME/.local/state"`` did.
    """
    if home_relative:
        _export_assignment(name, value, home_relative=True)
        line = f'export {name}="{value}"'
    else:
        line = _export_assignment(name, value)
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{line}\n")
    except OSError as exc:
        raise ProfileMigrationError(
            f"could not write exports file {path}: {exc}"
        ) from exc


# --- reading an exports file back, as install.sh's `. "$exports"` does (ISSUE-REF T6-0b) -------

_ASSIGNMENT = re.compile(r"(export )?([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z")
_REFERENCE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
#: Unquoted characters that would make a sourced line more than one assignment.
_UNQUOTED_REFUSED = frozenset(" \t;&|<>()`~")
#: The two Windows-native op lines, each a verb and two words.
_NATIVE_OPS = ("_queue_user_env", "create_junction_win")
_NATIVE_OP_WORDS = 3
#: ``$`` followed by one of these is an expansion this grammar does not evaluate.
_EXPANSIONS_REFUSED = frozenset("({?!#@*$-0123456789")


class HandBackRefused(ValueError):
    """A hand-back line the grammar does not accept; carries its 1-based line number only, never
    the line, since an exported value may be a credential."""

    def __init__(self, line: int) -> None:
        super().__init__(f"line {line}")
        self.line = line


@dataclass(frozen=True)
class HandedBack:
    """What a step handed back: its exports in order, the shell-only variables it set without
    exporting them (name to value, for the installer's own later lines), and its Windows-native
    op lines, split into words."""

    exports: dict[str, str]
    shell_only: dict[str, str]
    ops: tuple[tuple[str, ...], ...]


def _reference(raw: str, index: int, environ: Mapping[str, str], out: list[str]) -> int:
    """Expand the ``$`` at ``raw[index]``; the index after it."""
    found = _REFERENCE.match(raw, index)
    if found:
        out.append(environ.get(found.group(1) or found.group(2), ""))
        return found.end()
    following = raw[index + 1 : index + 2]
    if following in _EXPANSIONS_REFUSED and following:
        raise ValueError("an unsupported expansion")
    out.append("$")
    return index + 1


def _double_quoted(
    raw: str, index: int, environ: Mapping[str, str], out: list[str]
) -> int:
    """The body of the double-quoted string opening at ``raw[index]``; the index after it."""
    index += 1
    while index < len(raw):
        char = raw[index]
        if char == '"':
            return index + 1
        if char == "\\" and raw[index + 1 : index + 2] in ("$", "`", '"', "\\"):
            out.append(raw[index + 1])
            index += 2
        elif char == "`":
            raise ValueError("a command substitution")
        elif char == "$":
            index = _reference(raw, index, environ, out)
        else:
            out.append(char)
            index += 1
    raise ValueError("an unterminated double quote")


def _shell_value(raw: str, environ: Mapping[str, str]) -> str:
    """The value bash gives the right-hand side ``raw`` of one assignment: quotes removed and
    ``$NAME``/``${NAME}`` expanded from ``environ``. Anything else that would run or split is
    refused with ``ValueError``."""
    out: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "'":
            end = raw.find("'", index + 1)
            if end < 0:
                raise ValueError("an unterminated single quote")
            out.append(raw[index + 1 : end])
            index = end + 1
        elif char == '"':
            index = _double_quoted(raw, index, environ, out)
        elif char == "\\":
            if index + 1 >= len(raw):
                raise ValueError("a trailing backslash")
            out.append(raw[index + 1])
            index += 2
        elif char == "$":
            index = _reference(raw, index, environ, out)
        elif char in _UNQUOTED_REFUSED:
            raise ValueError("an unquoted shell metacharacter")
        else:
            out.append(char)
            index += 1
    return "".join(out)


def read_handed_back(path: Path, environ: Mapping[str, str]) -> HandedBack:
    """Read an exports file the way install.sh sources it, without running it.

    Each line is an ``export NAME=value`` (the shell-quoted form :func:`append_export` writes, or a
    double-quoted value referring to ``$NAME``, expanded from ``environ`` updated by the lines
    before it), a bare ``NAME=value`` (a variable for the installer's shell alone, which no later
    step's process inherits), a Windows-native op (:func:`queue_handed_back_op`'s two shapes), or
    blank. Any other line raises :class:`HandBackRefused` with its number. A missing file is an
    empty hand-back.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return HandedBack({}, {}, ())
    except OSError as exc:
        raise ProfileMigrationError(
            f"could not read exports file {path}: {exc}"
        ) from exc
    scope = dict(environ)
    exports: dict[str, str] = {}
    shell_only: dict[str, str] = {}
    ops: list[tuple[str, ...]] = []
    for number, line in enumerate(lines, start=1):
        try:
            words = shlex.split(line)
        except ValueError:
            raise HandBackRefused(number) from None
        if not words:
            continue
        if words[0] in _NATIVE_OPS:
            ops.append(_native_op(number, words))
            continue
        exported, name, value = _assignment(number, line, words, scope)
        scope[name] = value
        if exported:
            exports[name] = value
        else:
            shell_only[name] = value
    return HandedBack(exports, shell_only, tuple(ops))


def _native_op(number: int, words: list[str]) -> tuple[str, ...]:
    if len(words) != _NATIVE_OP_WORDS or (
        words[0] == "_queue_user_env" and not _SHELL_VARIABLE_NAME.fullmatch(words[1])
    ):
        raise HandBackRefused(number)
    return tuple(words)


def _assignment(
    number: int, line: str, words: list[str], scope: Mapping[str, str]
) -> tuple[bool, str, str]:
    """(exported, name, value) of an assignment line, or :class:`HandBackRefused`."""
    assignment = _ASSIGNMENT.match(line.strip())
    if assignment is None or len(words) != 1 + bool(assignment.group(1)):
        raise HandBackRefused(number)
    exported, name, raw = assignment.groups()
    try:
        return bool(exported), name, _shell_value(raw, scope)
    except ValueError:
        raise HandBackRefused(number) from None


# install.sh Step 5c's exact line and guard: the profile keeps `$HOME` literal, so it survives a
# moved home, and any line already mentioning `.claude/bin` counts as present (`grep -qF`).
HELPER_BIN_ENTRY = 'export PATH="$HOME/.claude/bin:$PATH"'
_HELPER_BIN_MARKER = ".claude/bin"
# Step 5c1's second profile: both directories a zsh LOGIN shell must still see first after
# path_helper, ~/.local/bin because the python3 shim lives there.
_ZPROFILE_DIRS = ("$HOME/.claude/bin", "$HOME/.local/bin")
_PYTHON_FLOOR_CHECK = (
    "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
)


def _read_profile(path: Path) -> str:
    """The profile's text, or "" when it is absent (the shell's `grep ... 2>/dev/null`)."""
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as exc:
        raise ProfileMigrationError(
            f"could not read shell profile {path}: {exc}"
        ) from exc


def _append(path: Path, text: str) -> None:
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(text)
    except OSError as exc:
        raise ProfileMigrationError(
            f"could not update shell profile {path}: {exc}"
        ) from exc


def _uname() -> str:
    """`uname -s`; a seam, so the Darwin-only arm is exercised off Darwin."""
    return platform.system()


def _run_text(argv: Sequence[str]) -> str:
    """A command's combined output with trailing newlines stripped (`$(... 2>&1)`); "" if absent."""
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=LOGIN_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.rstrip("\n")


# Bounds every Step 5c1 probe: a login shell whose startup files hang must not hang the install.
LOGIN_PROBE_TIMEOUT_S = 30
_REOPEN_HINT = (
    "         ~/.zprofile was updated; open a NEW terminal, or check for a later PATH"
    " rewrite."
)


def _report_login_python(out: TextIO, err: TextIO) -> None:
    """Step 5c1's check: ask a LOGIN zsh which python3 it resolves, never grep the profile.

    The pre-fix state already had the line in ~/.zshenv, so a grep passed while a login shell
    still resolved 3.9.6. A python3 below the floor, or a login shell that does not answer within
    LOGIN_PROBE_TIMEOUT_S, is a warning: the profile has been fixed either way.
    """
    try:
        found = subprocess.run(
            ["zsh", "-l", "-c", "command -v python3"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=LOGIN_PROBE_TIMEOUT_S,
        ).stdout.rstrip("\n")
    except subprocess.TimeoutExpired:
        print(
            "  [warn] a login zsh did not answer `command -v python3` within"
            f" {LOGIN_PROBE_TIMEOUT_S} s; its startup files may hang.",
            file=err,
        )
        print(_REOPEN_HINT, file=err)
        return
    except OSError:
        found = ""
    floor_ok = False
    if found:
        try:
            floor_ok = (
                subprocess.run(
                    [found, "-c", _PYTHON_FLOOR_CHECK],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=LOGIN_PROBE_TIMEOUT_S,
                ).returncode
                == 0
            )
        except (OSError, subprocess.TimeoutExpired):
            floor_ok = False
    version = _run_text([found, "-V"]) if found else ""
    if floor_ok:
        print(f"  [ok]  login-shell python3 → {found} ({version})", file=out)
        return
    print(
        f"  [warn] a login zsh still resolves python3 → {found or '<none>'} ({version}),"
        " below the 3.11 floor.",
        file=err,
    )
    print(_REOPEN_HINT, file=err)


def persist_helper_bin_path(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> Path:
    """install.sh's retired Steps 5c and 5c1: put ~/.claude/bin on PATH in the login profile.

    The profile is ``.bashrc`` on a Windows shell, ``.zshenv`` under a zsh login shell and
    ``.bashrc`` otherwise, and a profile already mentioning ``.claude/bin`` is left untouched.

    On macOS the first-read profile is not sufficient, and the failure is silent. zsh reads
    ``.zshenv`` FIRST; then, for a LOGIN shell, ``/etc/zprofile`` runs ``path_helper -s``, which
    REBUILDS PATH from ``/etc/paths`` and ``/etc/paths.d`` and appends everything already present
    AFTER it, so the entry is correct and outranked by ``/usr/bin`` in exactly the shell flavour
    Terminal.app and iTerm2 open by default. ``~/.zprofile`` is sourced AFTER ``/etc/zprofile``, so
    under Darwin and a zsh login shell both helper directories are re-prepended there (G94), each
    only when absent so an already-correct profile is not rewritten.
    """
    values = os.environ if environ is None else environ
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    target_home = Path.home() if home is None else home
    login_shell = _login_shell(values)
    windows = values.get("MSYSTEM", "").upper().startswith(("MINGW", "MSYS", "CYGWIN"))
    is_zsh = login_shell.endswith("/zsh")
    path = target_home / (".zshenv" if is_zsh and not windows else ".bashrc")
    if _HELPER_BIN_MARKER not in _read_profile(path):
        _append(path, f"{HELPER_BIN_ENTRY}\n")
    print("  [ok]  ~/.claude/bin added to PATH", file=out)
    if _uname() != "Darwin" or not is_zsh:
        return path
    zprofile = target_home / ".zprofile"
    for directory in _ZPROFILE_DIRS:
        if directory not in _read_profile(zprofile):
            # The leading newline keeps the export off a last line that lacks one.
            _append(zprofile, f'\nexport PATH="{directory}:$PATH"\n')
    _report_login_python(out, err)
    return path


def migrate_profile_var(path: Path, old_name: str, new_name: str) -> bool:
    """Replace exported ``old_name`` lines with ``new_name``, once per profile."""
    if not path.is_file():
        return False
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    old_prefix = f"export {old_name}="
    old_lines = [line for line in content.splitlines() if line.startswith(old_prefix)]
    if not old_lines:
        return False

    value = old_lines[0].removeprefix(old_prefix)
    retained = [
        line for line in content.splitlines() if not line.startswith(old_prefix)
    ]
    if new_name not in content:
        retained.append(f"export {new_name}={value}")
    replacement = "\n".join(retained)
    if content.endswith("\n") or replacement:
        replacement += "\n"
    try:
        path.write_text(replacement, encoding="utf-8")
    except OSError as exc:
        raise ProfileMigrationError(
            f"could not update shell profile {path}: {exc}"
        ) from exc
    return True


def migrate_profile_host_value(path: Path, renames: Sequence[tuple[str, str]]) -> bool:
    """Atomically replace exact stale ``AI_HOST`` values declared in ``renames``."""
    if not path.is_file():
        return False
    changed = False
    for old, new in renames:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return changed
        pattern = re.compile(
            rf"^export AI_HOST=(?:{re.escape(old)}|\"{re.escape(old)}\"|'{re.escape(old)}')[ \t]*$",
            re.MULTILINE,
        )
        replacement, count = pattern.subn(
            lambda _match, new=new: _export_assignment("AI_HOST", new), content
        )
        if not count:
            continue
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f"{path.name}.", dir=path.parent
            )
        except OSError:
            continue
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(replacement)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            continue
        try:
            os.replace(temporary, path)
        except OSError as exc:
            raise ProfileMigrationError(
                f"could not replace shell profile {path}: {exc}"
            ) from exc
        changed = True
    return changed
