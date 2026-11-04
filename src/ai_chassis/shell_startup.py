"""Shell-startup snippets: install.sh's retired Step 10b (ISSUE-REF).

A TUI that enables mouse, focus or bracketed-paste reporting and dies without disabling it leaves
the TERMINAL typing its reports into the next program's input. ``shell/terminal-mode-reset.{sh,ps1}``
clear that leak at shell startup, behind their own guards (interactive, a tty, no live Claude Code
session); ``shell/vscode-ipc-refresh.sh`` (ISSUE-REF) unsets a ``VSCODE_IPC_HOOK_CLI`` whose socket
is gone. This step links each script under ``~/.claude`` and sources it from the interactive
startup files through one marker-delimited block per file. It runs on every install, since an
ephemeral ``$HOME`` loses its startup files on a restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .links import LinkError
from .rendered_links import _git_run

TERMINAL_RESET_TAG = (
    "ai-harness terminal mode reset (ISSUE-REF)"
)
#: The hardlink and copy fallbacks publish_link wrote, under the state directory.
PUBLISHED_COPIES = Path("ai-harness") / "published-copies.json"
#: The one git read behind the committed-revision ownership test; past it, no match.
HISTORY_TIMEOUT_SECONDS = 30
NULL_BLOB = "0" * 40
VSCODE_IPC_TAG = "ai-harness stale vscode ipc socket (ISSUE-REF)"
# The startup files source the published copy under $HOME, never the checkout, so a moved
# checkout cannot leave a dangling source line behind.
TERMINAL_RESET_SH_BODY = """if [ -r "$HOME/.claude/terminal-mode-reset.sh" ]; then
  . "$HOME/.claude/terminal-mode-reset.sh"
fi"""
TERMINAL_RESET_PS_BODY = """$aihTerminalModeReset = Join-Path $HOME ".claude/terminal-mode-reset.ps1"
if (Test-Path -LiteralPath $aihTerminalModeReset) { . $aihTerminalModeReset }
Remove-Variable -Name aihTerminalModeReset -ErrorAction SilentlyContinue"""
VSCODE_IPC_BODY = """if [ -r "$HOME/.claude/vscode-ipc-refresh.sh" ]; then
  . "$HOME/.claude/vscode-ipc-refresh.sh"
fi"""
#: Windows PowerShell 5.1 and PowerShell 7+ read separate profiles; each is one host process.
POWERSHELL_PROFILES = (
    Path("Documents/WindowsPowerShell/Microsoft.PowerShell_profile.ps1"),
    Path("Documents/PowerShell/Microsoft.PowerShell_profile.ps1"),
)


def _is_windows_shell(environ: Mapping[str, str]) -> bool:
    return environ.get("MSYSTEM", "").upper().startswith(("MINGW", "MSYS", "CYGWIN"))


def _marker_problem(lines: list[str], start: str, end: str) -> str | None:
    """What is wrong with the markers' pairing, or None when every block is closed."""
    inside = False
    for line in lines:
        if line == start:
            if inside:
                return "an unterminated managed block"
            inside = True
        elif line == end:
            if not inside:
                return "a managed-block end marker with no start"
            inside = False
    return "an unterminated managed block" if inside else None


def write_managed_block(path: Path, tag: str, body: str) -> bool:
    """Write ``# >>> tag >>>`` / body / ``# <<< tag <<<`` into ``path``, replacing a block in place.

    The markers must pair up (each start closed by an end before the next start, no end outside
    a block); a file where they do not was hand-edited, so it is left untouched and False is
    returned rather than guessing where a block ends. The first block takes the body and any
    later block for the same tag is dropped, so duplicates a past install left converge to one.
    The file is rewritten in place (its mode, and its identity if it is a symlink, survive) and
    only when its bytes change.
    """
    start, end = f"# >>> {tag} >>>", f"# <<< {tag} <<<"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = current.split("\n")
        if current.endswith("\n"):
            lines.pop()
        problem = _marker_problem(lines, start, end)
        if problem:
            print(f"  [warn] {path} has {problem} — left untouched", file=sys.stderr)
            return False
        if start not in lines:
            prefix = (
                current if not current or current.endswith("\n") else current + "\n"
            )
            rendered = f"{prefix}{start}\n{body}\n{end}\n"
        else:
            kept: list[str] = []
            inside = written = False
            for line in lines:
                if line == start:
                    if not written:
                        kept.extend((start, body, end))
                        written = True
                    inside = True
                elif inside:
                    inside = line != end
                else:
                    kept.append(line)
            rendered = "\n".join(kept) + "\n"
        if rendered != current:
            with path.open("w", encoding="utf-8") as stream:
                stream.write(rendered)
    except OSError as exc:
        raise LinkError(f"could not write the managed block in {path}: {exc}") from exc
    return True


def _refuse_tracked_file(link: Path) -> None:
    """install.sh's ISSUE-REF guard: never replace a file a git checkout tracks with a link."""
    if link.is_symlink() or not link.exists():
        return
    toplevel = _git_run(link.parent, "rev-parse", "--show-toplevel")
    if toplevel is None or toplevel.returncode != 0:
        return
    tracked = _git_run(link.parent, "ls-files", "--error-unmatch", "--", link.name)
    if tracked is not None and tracked.returncode == 0:
        repo = toplevel.stdout.strip()
        raise LinkError(
            f"refusing to symlink '{link}' — it resolves to a tracked file in repo '{repo}'; "
            "creating the symlink there would clobber real source (ISSUE-REF self-clobber guard)"
        )


def is_own_copy(source: Path, link: Path) -> bool:
    """A regular file at ``link`` that is ``source``'s publication: a hardlink to it or its bytes.

    These are the only regular files ``publish_link`` itself leaves (Git Bash's hardlink and copy
    fallbacks). A symlink, a directory or an unreadable file is not one.
    """
    if link.is_symlink() or not link.is_file():
        return False
    try:
        return (
            os.path.samefile(link, source) or link.read_bytes() == source.read_bytes()
        )
    except OSError:
        return False


def _record_path(environ: Mapping[str, str]) -> Path | None:
    state = environ.get("XDG_STATE_HOME")
    if state:
        return Path(state) / PUBLISHED_COPIES
    home = environ.get("HOME")
    return Path(home) / ".local" / "state" / PUBLISHED_COPIES if home else None


def _read_record(path: Path | None) -> dict[str, dict[str, str]]:
    """The recorded fallbacks by link path; a missing, unreadable or corrupt record is empty."""
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    copies = data.get("copies") if isinstance(data, dict) else None
    if not isinstance(copies, dict):
        return {}
    return {
        key: row
        for key, row in copies.items()
        if isinstance(row, dict) and isinstance(row.get("sha256"), str)
    }


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _is_recorded_unchanged(link: Path, environ: Mapping[str, str]) -> bool:
    """``link`` holds exactly the bytes the record says ``publish_link`` last wrote there."""
    row = _read_record(_record_path(environ)).get(os.path.abspath(link))
    return row is not None and _sha256(link) == row["sha256"]


def _blob_id(path: Path) -> str | None:
    """The git blob id of ``path``'s bytes, computed here rather than by a git call."""
    try:
        content = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha1(
        b"blob %d\0" % len(content) + content, usedforsecurity=False
    ).hexdigest()


def _is_committed_revision(source: Path, link: Path) -> bool:
    """``link`` holds the bytes of a revision ``source``'s path has had in its checkout's history.

    One read-only ``git log --raw`` over the source path; a source outside any work tree, a git
    that fails or a git that outlives ``HISTORY_TIMEOUT_SECONDS`` is no match.
    """
    blob = _blob_id(link)
    if blob is None:
        return False
    result = _git_run(
        source.parent,
        "log",
        "--format=",
        "--raw",
        "--no-abbrev",
        "--no-renames",
        "--",
        source.name,
        timeout=HISTORY_TIMEOUT_SECONDS,
    )
    if result is None or result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        if not line.startswith(":"):
            continue
        fields = line.split("\t", 1)[0].split()
        if blob in fields[2:4] and blob != NULL_BLOB:
            return True
    return False


def _is_stale_own(source: Path, link: Path, environ: Mapping[str, str]) -> bool:
    """Ours but stale: the record's bytes, else a committed revision of the source (git only
    for a regular file the record does not match, so a normal apply runs no git)."""
    return _is_recorded_unchanged(link, environ) or _is_committed_revision(source, link)


def is_own_publication(source: Path, link: Path, environ: Mapping[str, str]) -> bool:
    """A regular file at ``link`` that ``publish_link`` owns: current (``is_own_copy``), or left
    stale by a source update (its recorded bytes, else a committed revision of the source)."""
    return is_own_copy(source, link) or (
        not link.is_symlink()
        and link.is_file()
        and _is_stale_own(source, link, environ)
    )


def _update_record(source: Path, link: Path, environ: Mapping[str, str]) -> None:
    """Record a hardlink or copy fallback at ``link``; drop the row when ``link`` is a symlink.

    Written atomically and only when a row changes. A record that cannot be written is warned
    and the publication stands: an unrecorded fallback reads as foreign once it goes stale, so
    the failure leaves files alone rather than deleting them.
    """
    path = _record_path(environ)
    if path is None:
        return
    copies = _read_record(path)
    key = os.path.abspath(link)
    updated = dict(copies)
    digest = None if link.is_symlink() else _sha256(link)
    if digest is None:
        updated.pop(key, None)
    else:
        updated[key] = {"source": os.path.abspath(source), "sha256": digest}
    if updated == copies:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"copies": updated}, stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    except OSError as exc:
        print(
            f"  [warn] could not record the published copy {link} in {path}: {exc}",
            file=sys.stderr,
        )


def _repair_exec_bits(source: Path, link: Path) -> None:
    """Give an own copy the source's exec bits when the source is executable and it is not."""
    try:
        source_mode = stat.S_IMODE(source.stat().st_mode)
        mode = stat.S_IMODE(link.stat().st_mode)
        if source_mode & stat.S_IXUSR and not mode & stat.S_IXUSR:
            link.chmod(mode | (source_mode & 0o111))
    except OSError as exc:
        raise LinkError(f"could not make {link} executable: {exc}") from exc


def _make_link(source: Path, link: Path, target: str, windows: bool) -> None:
    """A symlink; under Git Bash a hardlink, then a mode-keeping copy, when that is refused."""
    if not windows:
        link.symlink_to(target)
        return
    try:
        link.symlink_to(target)
    except OSError:
        try:
            os.link(source, link)
        except OSError:
            shutil.copy(source, link)


def publish_link(source: Path, link: Path, environ: Mapping[str, str]) -> bool:
    """Publish ``source`` at ``link`` as a relative symlink; True when ``link`` is now ours.

    The relative target is computed from the link directory's PHYSICAL path, which is the one the
    kernel resolves it against. Under Git Bash a native symlink is tried, then a hardlink, then a
    copy that keeps the source's mode, as install.sh's ``create_symlink`` did.

    Ownership, on every platform: a symlink at ``link`` is ours and is repointed when its target
    differs; a hardlink to ``source`` or a copy of its bytes (``is_own_copy``) is ours and
    current. Under Git Bash such a hardlink or copy IS the publication and nothing is written to
    it, except that a copy missing the source's exec bits gets them (mode only); elsewhere it
    becomes the symlink.

    Each hardlink or copy fallback written, and each current one found, is recorded (absolute
    link path, source path, sha256 of its bytes) in ``published-copies.json`` under
    ``$XDG_STATE_HOME/ai-harness``, else ``$HOME/.local/state/ai-harness``, both read from
    ``environ``. A source update writes a new file, so a hardlink keeps the old inode and a copy
    the old bytes; a regular file whose sha256 equals its recorded row is that stale fallback
    and is replaced. A symlink made over a recorded path drops its row. A missing, unreadable or
    corrupt record reads as no record.

    A fallback the pre-record code wrote has no row, and is usually already stale on its first
    apply. So a regular file the record does not match is also ours and stale when its git blob
    id (computed here) is one the source's path has had in its checkout's history, read by one
    ``git log --format= --raw --no-abbrev --no-renames -- <name>`` from the source's directory;
    it is replaced and recorded. A source outside any work tree, or a git that fails or times
    out, is no match. Recorded choice: a copy someone deliberately pinned at an old committed
    revision is replaced too; a deliberate pin needs another name or different bytes.

    Any other regular file, including a recorded one somebody edited, is somebody else's: it is
    left as found, a ``[warn]`` naming it and its move-aside remedy goes to stderr, and False is
    returned. A tracked file and a directory are refused with ``LinkError``, as is a link that
    cannot be made.
    """
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            target = os.path.relpath(source, os.path.realpath(link.parent))
        except ValueError:
            # Windows: no relative path joins two drives.
            target = str(source)
        if link.is_symlink() and os.readlink(link) == target:
            return True
        _refuse_tracked_file(link)
        if link.is_dir() and not link.is_symlink():
            raise LinkError(
                f"refusing to replace directory {link} with a link to {source}"
            )
        windows = _is_windows_shell(environ)
        if not link.is_symlink() and link.exists():
            if is_own_copy(source, link):
                if windows:
                    _repair_exec_bits(source, link)
                    _update_record(source, link, environ)
                    return True
            elif not _is_stale_own(source, link, environ):
                print(
                    f"  [warn] {link} exists and is not a publication of {source}; leaving "
                    "it alone. Move it aside and re-run the install to publish it there.",
                    file=sys.stderr,
                )
                return False
        if link.is_symlink() or link.exists():
            link.unlink()
        _make_link(source, link, target, windows)
        _update_record(source, link, environ)
    except OSError as exc:
        raise LinkError(f"could not link {link} to {source}: {exc}") from exc
    return True


def _source_from_startup_files(
    home: Path, tag: str, body: str, label: str, has_zsh: bool
) -> None:
    if write_managed_block(home / ".bashrc", tag, body):
        print(f"  [ok]  {label} → ~/.bashrc")
    if has_zsh:
        if write_managed_block(home / ".zshrc", tag, body):
            print(f"  [ok]  {label} → ~/.zshrc")
    else:
        print(f"  [skip] {label} → ~/.zshrc (no zsh on this machine)")


def install_terminal_mode_reset(
    checkout: Path, home: Path, environ: Mapping[str, str]
) -> None:
    """Publish both snippets under ``~/.claude`` and source them from every startup file."""
    claude_dir = home / ".claude"
    shell_dir = checkout / "shell"
    sources = [
        shell_dir / name
        for name in (
            "terminal-mode-reset.sh",
            "terminal-mode-reset.ps1",
            "vscode-ipc-refresh.sh",
        )
    ]
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise LinkError(
            f"shell-startup snippet missing from the checkout: {', '.join(missing)}"
        )
    # .bashrc and .zshrc are the INTERACTIVE startup files, the scope that wants this; .zshenv
    # and .profile are read by scripts and hook subprocesses too.
    has_zsh = (
        shutil.which("zsh", path=environ.get("PATH")) is not None
        or (home / ".zshrc").is_file()
    )
    reset_sh, reset_ps1, vscode_ipc = sources
    publish_link(reset_sh, claude_dir / reset_sh.name, environ)
    publish_link(reset_ps1, claude_dir / reset_ps1.name, environ)
    _source_from_startup_files(
        home, TERMINAL_RESET_TAG, TERMINAL_RESET_SH_BODY, "terminal mode reset", has_zsh
    )
    if _is_windows_shell(environ):
        for profile in POWERSHELL_PROFILES:
            if write_managed_block(
                home / profile, TERMINAL_RESET_TAG, TERMINAL_RESET_PS_BODY
            ):
                print(f"  [ok]  terminal mode reset → {profile.as_posix()}")
    publish_link(vscode_ipc, claude_dir / vscode_ipc.name, environ)
    _source_from_startup_files(
        home, VSCODE_IPC_TAG, VSCODE_IPC_BODY, "stale vscode ipc guard", has_zsh
    )
