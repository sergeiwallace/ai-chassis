"""The zsh, gitleaks, Doppler and GitHub CLI bootstraps: install.sh's retired Steps 1d, 1e, 1f and 1f4
(ISSUE-REF batches C-A1 and C-A2).

Each is a dependency bootstrap of the public core: it probes for its tool, installs it only where
that can plausibly succeed, says what it did, and never fails the install. Every subprocess runs
through ``run_bounded``: a wall-clock bound, a closed stdin (nothing here may wait on a prompt), and
its output kept out of the install's own unless the caller relays it redacted.

``ensure_zsh`` installs through apt-get or dnf under ``sudo -n`` (or as root), after asking ``sudo``
whether it will run THAT package manager (ISSUE-REF). ``ensure_doppler`` installs without any
privilege: Homebrew on macOS, a release tarball into ``~/.local/bin`` on Linux. ``ensure_gitleaks``
probes the capability the founding-ask gate needs (``gitleaks stdin``) before the version floor,
tries the platform's package manager, then a checksum-verified release into ``~/.local/bin``.
``ensure_gh`` keeps this user's ``gh`` at the latest release: a checksum-verified tarball into
``~/.local/bin`` on Linux, Homebrew on macOS, a manual step anywhere else. Neither of the last two
hands ``GH_TOKEN`` or ``GITHUB_TOKEN`` to any child.
"""

from __future__ import annotations

import glob
import hashlib
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TextIO
from pathlib import Path

from . import manual_steps
from .manual_steps import redact_secrets

#: ``timeout``'s own contract, which the shell's ``_run_bounded`` exposed: 124 on expiry, 137 if the
#: KILL was needed, 127 for a command that cannot start.
EXPIRED = 124
KILLED = 137
NOT_RUNNABLE = 127

ZSH_INSTALL_SECONDS = 300.0
ZSH_INSTALL_GRACE_SECONDS = 10.0
PROBE_SECONDS = 30.0
BREW_INSTALL_SECONDS = 900.0
RELEASE_LOOKUP_SECONDS = 40.0
DOWNLOAD_SECONDS = 190.0
EXTRACT_SECONDS = 60.0
DEFAULT_GRACE_SECONDS = 5.0
DRAIN_SECONDS = 2.0

DOPPLER_FORMULA = "dopplerhq/cli/doppler"
_BREW_COMMAND = f"brew install {DOPPLER_FORMULA}"
DOPPLER_MANUAL = "https://docs.doppler.com/docs/install-cli"
DOPPLER_RELEASES = "https://api.github.com/repos/DopplerHQ/cli/releases/latest"
DOPPLER_DOWNLOAD = "https://github.com/DopplerHQ/cli/releases/download"
_CURL = (
    "curl",
    "--fail",
    "--silent",
    "--show-error",
    "--location",
    "--connect-timeout",
    "10",
)
_ARCHES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_TAG_NAME = re.compile(r'.*"tag_name"\s*:\s*"([^"]*)".*')
#: What a Doppler release tag looks like. The tag becomes part of a download URL, so anything else
#: is "could not discover the latest release" rather than a path to fetch.
_RELEASE_TAG = re.compile(r"v?[0-9]+(\.[0-9]+)+")

GITLEAKS_MIN_VERSION = "8.30.1"
GITLEAKS_MANUAL = "https://github.com/gitleaks/gitleaks#installing"
GITLEAKS_RELEASES = "https://api.github.com/repos/gitleaks/gitleaks/releases/latest"
GITLEAKS_DOWNLOAD = "https://github.com/gitleaks/gitleaks/releases/download"
_GITLEAKS_ARCHES = {
    "x86_64": "x64",
    "amd64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
}
PACKAGE_INSTALL_SECONDS = 300.0
PACKAGE_INSTALL_GRACE_SECONDS = 10.0
GITLEAKS_DOWNLOAD_SECONDS = 130.0
CHECKSUMS_SECONDS = 70.0
GH_RELEASES = "https://api.github.com/repos/cli/cli/releases/latest"
GH_DOWNLOAD = "https://github.com/cli/cli/releases/download"
GH_MANUAL = "https://github.com/cli/cli/blob/trunk/docs/install_linux.md"
GH_MANUAL_STEP = "github-cli-latest"
#: The credentials ``gh`` and the GitHub API read from the environment; no child of the gitleaks or
#: gh step needs either, so neither is ever passed on.
GITHUB_TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN")
_SEMVER_CORE = re.compile(r"[^0-9]*([0-9][0-9.]*)")
_WINDOWS_SHELLS = ("MINGW", "MSYS", "CYGWIN")


@dataclass(frozen=True)
class Bounded:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def expired(self) -> bool:
        return self.returncode in {EXPIRED, KILLED}


_WINDOWS = os.name == "nt"
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
TREE_KILL_SECONDS = 30.0


def _tree_kill(pid: int) -> bool:
    """``taskkill /PID <pid> /T /F``: the process and every descendant; True when it exited 0."""
    try:
        return (
            subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=TREE_KILL_SECONDS,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _signal_group(process: subprocess.Popen[str], sig: signal.Signals) -> None:
    """Stop ``process`` with everything it started: its group on POSIX, its tree on Windows
    (where the tree kill is already forceful, so TERM and KILL are the same act)."""
    if _WINDOWS:
        if not _tree_kill(process.pid):
            process.kill()
        return
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _drain(process: subprocess.Popen[str], timeout: float) -> tuple[bool, str, str]:
    """Whether the pipes closed within ``timeout``, and what was read from them so far."""
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return False, _text(exc.stdout), _text(exc.stderr)
    return True, out or "", err or ""


def _text(captured: object) -> str:
    if isinstance(captured, bytes):
        return captured.decode("utf-8", errors="replace")
    return captured if isinstance(captured, str) else ""


def _abandon(process: subprocess.Popen[str]) -> None:
    """Close the pipes a descendant that escaped the group still holds, and reap the parent."""
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    try:
        process.wait(timeout=DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def run_bounded(  # noqa: PLR0913 - one keyword per Popen knob a caller sets
    argv: Sequence[str],
    *,
    seconds: float,
    grace: float | None = None,
    env: Mapping[str, str] | None = None,
    capture: bool = False,
    cwd: Path | None = None,
) -> Bounded:
    """Run ``argv`` for at most ``seconds``, in ``cwd`` when given; never prompts, never raises.

    The child gets its own process group, so an expiry takes down whatever it spawned: TERM, then
    KILL after ``grace`` whether or not TERM closed the pipes (a descendant that ignores TERM and
    moved its stdio is still running). Every wait after a stop is bounded, so a descendant that
    escaped the group and still holds a pipe cannot hang the step: the pipes are abandoned and the
    result is 137. Output is discarded unless ``capture`` is set. ``GIT_TERMINAL_PROMPT=0`` is set
    for the child, since nothing here can answer a credential prompt.
    """
    wait = DEFAULT_GRACE_SECONDS if grace is None else grace
    sink = subprocess.PIPE if capture else subprocess.DEVNULL
    child_env = {**(os.environ if env is None else env), "GIT_TERMINAL_PROMPT": "0"}
    group: dict[str, object] = (
        {"creationflags": _CREATE_NEW_PROCESS_GROUP}
        if _WINDOWS
        else {"start_new_session": True}
    )
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            cwd=cwd,
            **group,
        )
    except OSError:
        return Bounded(NOT_RUNNABLE)
    with process:
        try:
            out, err = process.communicate(timeout=seconds)
            return Bounded(process.returncode, out or "", err or "")
        except subprocess.TimeoutExpired as exc:
            partial, partial_err = _text(exc.stdout), _text(exc.stderr)
        _signal_group(process, signal.SIGTERM)
        closed, out, err = _drain(process, wait)
        if not _WINDOWS:
            _signal_group(process, signal.SIGKILL)
        if closed:
            return Bounded(EXPIRED, out or partial, err or partial_err)
        closed, last, last_err = _drain(process, DRAIN_SECONDS)
        if not closed:
            _abandon(process)
        return Bounded(KILLED, last or out or partial, last_err or err or partial_err)


def expiry_note(result: Bounded, seconds: float) -> str:
    """`` (timed out after N s)`` when the bound fired, so the line names the cause."""
    return f" (timed out after {seconds:g} s)" if result.expired else ""


def relay_redacted(text: str, stream: TextIO) -> None:
    """Print a tool's output with every credential shape replaced."""
    if text.strip():
        print(redact_secrets(text.rstrip("\n")), file=stream)


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def sudo_can_run(
    program: str, *args: str, env: Mapping[str, str] | None = None
) -> bool:
    """Whether ``sudo -n`` will run THIS program (ISSUE-REF), asked with a real read-only call.

    Root needs no sudo. Otherwise sudo and the program must both exist (sudo's own "command not
    found" is indistinguishable from a refusal), and the call runs with stdin closed: an endpoint
    agent's "Confirm Execution" prompt reads EOF and refuses at once instead of blocking forever.
    """
    if _is_root():
        return True
    if shutil.which("sudo") is None or shutil.which(program) is None:
        return False
    return (
        run_bounded(
            ["sudo", "-n", program, *args], seconds=PROBE_SECONDS, env=env
        ).returncode
        == 0
    )


def ensure_zsh() -> None:
    """Install zsh where a package manager and non-interactive privilege allow; never fail."""
    present = shutil.which("zsh")
    if present:
        print(redact_secrets(f"  [ok]  zsh present: {present}"))
        return
    manager = next((m for m in ("apt-get", "dnf") if shutil.which(m)), "")
    if not manager:
        print(
            "  [skip] zsh not installed: no recognised package manager (apt-get, dnf)"
        )
        print(
            "         NOTE: 'ai c' launches its tmux session with zsh and will fail with a bare"
        )
        print(
            "         '[exited]' until zsh is present. Install it with this machine's package manager."
        )
        return
    sudo_cmd: list[str] = []
    if not _is_root():
        if not sudo_can_run(manager, "--version"):
            print(
                f"  [skip] zsh not installed: non-interactive sudo unavailable for {manager}"
            )
            print(
                "         Install it manually — 'ai c' cannot start a session without zsh:"
            )
            print(f"             sudo {manager} install -y zsh")
            return
        sudo_cmd = ["sudo", "-n"]
    result = run_bounded(
        [
            *sudo_cmd,
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            manager,
            "install",
            "-y",
            "zsh",
        ],
        seconds=ZSH_INSTALL_SECONDS,
        grace=ZSH_INSTALL_GRACE_SECONDS,
    )
    if result.returncode == 0 and shutil.which("zsh"):
        print(
            f"  [fixed] zsh installed via {manager} (required by 'ai c'; also un-skips zsh tests)"
        )
    else:
        note = expiry_note(result, ZSH_INSTALL_SECONDS)
        print(
            f"  [skip] zsh install attempt failed via {manager}{note}; install it "
            "manually — 'ai c' cannot start without it"
        )


def _manual(*lines: str) -> None:
    for line in lines:
        print(line)


def _latest_tag(url: str, env: Mapping[str, str] | None = None) -> str:
    """The first ``tag_name`` the release API at ``url`` answers with, or ``""``."""
    result = run_bounded(
        [*_CURL, "--max-time", "30", url],
        seconds=RELEASE_LOOKUP_SECONDS,
        capture=True,
        env=env,
    )
    for line in result.stdout.splitlines():
        found = _TAG_NAME.fullmatch(line)
        if found:
            return found.group(1)
    return ""


def _discover_release(
    url: str = DOPPLER_RELEASES, env: Mapping[str, str] | None = None
) -> tuple[str, str]:
    """The latest release tag at ``url`` and its version, or ``("", "")``."""
    release = _latest_tag(url, env)
    if not _RELEASE_TAG.fullmatch(release):
        return "", ""
    return release, release.removeprefix("v")


def _install_release(
    release: str, version: str, arch: str, home: Path, tmpdir: Path
) -> bool | None:
    """Download, unpack and place the CLI: ``True`` when ``doppler`` then resolves, ``None`` when a
    file operation failed and the line naming it was already printed."""
    archive = tmpdir / "doppler.tar.gz"
    target_dir = home / ".local" / "bin"
    download = run_bounded(
        [
            *_CURL,
            "--max-time",
            "180",
            "-o",
            str(archive),
            f"{DOPPLER_DOWNLOAD}/{release}/doppler_{version}_linux_{arch}.tar.gz",
        ],
        seconds=DOWNLOAD_SECONDS,
        capture=True,
    )
    relay_redacted(download.stderr, sys.stderr)
    if download.returncode != 0:
        return False
    extract = run_bounded(
        ["tar", "-xzf", str(archive), "-C", str(tmpdir)],
        seconds=EXTRACT_SECONDS,
        capture=True,
    )
    relay_redacted(extract.stderr, sys.stderr)
    unpacked = tmpdir / "doppler"
    if (
        extract.returncode == 0
        and os.path.lexists(unpacked)
        and not _confined_regular_file(unpacked, tmpdir)
    ):
        print(
            f"  [skip] doppler {version} archive member is not a regular file inside the "
            "download; leaving the existing install alone"
        )
        return None
    if extract.returncode != 0 or not unpacked.is_file():
        return False
    placed = target_dir / "doppler"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(unpacked), str(placed))
        placed.chmod(placed.stat().st_mode | 0o111)
    except OSError as exc:
        # The shell aborted the install here under `set -e`; the row says skip and continue, so
        # the step names the real cause rather than blaming the network.
        print(
            redact_secrets(
                f"  [skip] doppler install: {exc.strerror or exc} ({exc.filename or placed})"
            )
        )
        return None
    paths = os.environ.get("PATH", "").split(os.pathsep)
    if str(target_dir) not in paths:
        os.environ["PATH"] = os.pathsep.join([str(target_dir), *paths])
    if shutil.which("doppler") is None:
        return False
    print(f"  [fixed] doppler installed to {target_dir} (no sudo required)")
    return True


def _doppler_with_homebrew() -> None:
    if shutil.which("brew") is None:
        _manual(
            "  [skip] doppler not installed: Homebrew is unavailable",
            f"         Install Homebrew, then run: {_BREW_COMMAND}",
        )
        return
    result = run_bounded(
        ["brew", "install", DOPPLER_FORMULA], seconds=BREW_INSTALL_SECONDS
    )
    if result.returncode == 0 and shutil.which("doppler"):
        print("  [fixed] doppler installed via Homebrew")
        return
    note = expiry_note(result, BREW_INSTALL_SECONDS)
    print(f"  [skip] doppler Homebrew install failed{note}; retry: {_BREW_COMMAND}")


def _doppler_from_release(home: Path) -> None:
    machine = platform.machine()
    arch = _ARCHES.get(machine)
    if arch is None:
        _manual(
            f"  [skip] doppler not installed: unsupported Linux architecture {machine}",
            f"         Install Doppler CLI manually: {DOPPLER_MANUAL}",
        )
        return
    if shutil.which("curl") is None or shutil.which("tar") is None:
        _manual(
            "  [skip] doppler not installed: curl and tar are required for the no-sudo download",
            "         Install them, then re-run install.sh.",
        )
        return
    release, version = _discover_release()
    if not release:
        _manual(
            "  [skip] doppler not installed: could not discover the latest release",
            "         Check network access, then re-run install.sh; manual instructions: "
            + DOPPLER_MANUAL,
        )
        return
    try:
        tmpdir = Path(
            tempfile.mkdtemp(prefix="doppler.", dir=os.environ.get("TMPDIR") or "/tmp")
        )
    except OSError:
        print(
            "  [skip] doppler not installed: could not create a temporary download directory"
        )
        return
    try:
        installed = _install_release(release, version, arch, home, tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if installed:
        return
    if installed is not None:
        print(
            "  [skip] doppler download/install failed; retry after checking network access"
        )
    print(f"         Manual instructions: {DOPPLER_MANUAL}")


def ensure_doppler(home: Path) -> None:
    """Install the Doppler CLI without privilege where the platform allows; never fail."""
    present = shutil.which("doppler")
    if present:
        print(redact_secrets(f"  [ok]  doppler present: {present}"))
        return
    system = platform.system()
    if system == "Darwin":
        _doppler_with_homebrew()
    elif system == "Linux":
        _doppler_from_release(home)
    else:
        _manual(
            f"  [skip] doppler bootstrap is unsupported on {system}",
            f"         Install Doppler CLI manually: {DOPPLER_MANUAL}",
        )


def _tokenless_env() -> dict[str, str]:
    """This process's environment without the GitHub credentials (``GITHUB_TOKEN_VARIABLES``)."""
    return {
        name: value
        for name, value in os.environ.items()
        if name not in GITHUB_TOKEN_VARIABLES
    }


def _run(
    argv: Sequence[str],
    seconds: float,
    *,
    grace: float | None = None,
    capture: bool = False,
) -> Bounded:
    """``run_bounded`` for the gitleaks and gh steps: never hands a child a GitHub token."""
    return run_bounded(
        argv, seconds=seconds, grace=grace, env=_tokenless_env(), capture=capture
    )


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _windows_shell(system: str) -> bool:
    return system == "Windows" or system.upper().startswith(_WINDOWS_SHELLS)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"[0-9]+", version))


def _make_tmpdir(prefix: str) -> Path | None:
    try:
        return Path(
            tempfile.mkdtemp(prefix=prefix, dir=os.environ.get("TMPDIR") or "/tmp")
        )
    except OSError:
        return None


def _fetch(url: str, target: Path, max_time: str, seconds: float) -> Bounded:
    return _run([*_CURL, "--max-time", max_time, "-o", str(target), url], seconds)


def sha256_of(path: Path) -> str:
    """``path``'s sha256 as a bare lowercase digest, or ``""`` when it cannot be read."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def _published_digest(checksums: Path, asset: str) -> str:
    """The digest a release's checksums file gives ``asset``, matched on the WHOLE name, so a
    prefix-sharing entry (``..._linux_amd64.deb``) cannot be read as the tarball's."""
    try:
        lines = checksums.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        fields = line.split()
        if fields[1:2] in ([asset], [f"*{asset}"]):
            return fields[0]
    return ""


def _verified(  # noqa: PLR0913 - the checksums file name is the one per-release difference
    tool: str,
    version: str,
    release_url: str,
    archive: Path,
    asset: str,
    *,
    checksums_name: str = "",
) -> bool:
    """Fetch the release's checksums file (``<tool>_<version>_checksums.txt`` unless the release
    names it otherwise) and compare ``archive`` with its line for ``asset``; every refusal prints
    its ``[skip]`` and leaves the existing binary alone."""
    checksums = archive.parent / "checksums.txt"
    fetched = _fetch(
        f"{release_url}/{checksums_name or f'{tool}_{version}_checksums.txt'}",
        checksums,
        "60",
        CHECKSUMS_SECONDS,
    )
    if fetched.returncode != 0:
        note = expiry_note(fetched, CHECKSUMS_SECONDS)
        print(
            f"  [skip] {tool} {version} checksums could not be fetched{note}; "
            "refusing to install an unverified binary"
        )
        return False
    expected = _published_digest(checksums, asset)
    actual = sha256_of(archive)
    if not expected or not actual or expected != actual:
        print(
            f"  [skip] {tool} {version} sha256 verification FAILED; the existing {tool} is "
            "left untouched",
            file=sys.stderr,
        )
        print(
            f"         asset={asset} (no digest is printed, so a partial match cannot be "
            "mistaken for a pass)",
            file=sys.stderr,
        )
        return False
    return True


def _confined_regular_file(candidate: Path, root: Path) -> bool:
    """Whether ``candidate`` is a regular file inside ``root`` reached through no symlink: an
    archive member that is a link, or sits under a linked directory, could make the step run or
    install something the verified archive never contained."""
    try:
        relative = candidate.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return False
        if not candidate.resolve().is_relative_to(root.resolve()):
            return False
        return stat.S_ISREG(candidate.lstat().st_mode)
    except (OSError, ValueError):
        return False


def _install_binary(source: Path, home: Path) -> bool:
    """``install -m 0755 <source> ~/.local/bin/<name>``; False when either step failed."""
    target_dir = home / ".local" / "bin"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    placed = _run(
        ["install", "-m", "0755", str(source), str(target_dir / source.name)],
        PROBE_SECONDS,
    )
    return placed.returncode == 0


def _gitleaks_candidates(home: Path) -> tuple[str, ...]:
    """``~/.local/bin/gitleaks`` FIRST, by absolute path: it is where the release download
    installs, and a PATH without that directory cannot see the binary this step placed
    (ISSUE-REF)."""
    return (str(home / ".local" / "bin" / "gitleaks"), "gitleaks")


def _gitleaks_meets_floor(binary: str) -> bool:
    """The CAPABILITY first (``gitleaks stdin --help`` exits 0), the version floor second: a
    build that reports no parseable version ("version is set by build process") passes, since
    the capability is the requirement and refusing it would reinstall forever."""
    if os.path.isabs(binary):
        if not _executable(Path(binary)):
            return False
    elif shutil.which(binary) is None:
        return False
    if _run([binary, "stdin", "--help"], PROBE_SECONDS).returncode != 0:
        return False
    reported = "".join(
        _run([binary, "version"], PROBE_SECONDS, capture=True).stdout.split()
    )
    if not reported or not re.fullmatch(r"[0-9.]+", reported):
        return True
    return _version_key(GITLEAKS_MIN_VERSION) <= _version_key(reported)


def _gitleaks_floor_ok(home: Path) -> str:
    """The first candidate that meets the floor, as a path, or ``""``."""
    for candidate in _gitleaks_candidates(home):
        if _gitleaks_meets_floor(candidate):
            return shutil.which(candidate) or candidate
    return ""


def _gitleaks_present(home: Path) -> bool:
    """Presence over the floor's candidates, so an unusable ``~/.local/bin`` binary takes the
    replace-it branch rather than the never-installed one."""
    for candidate in _gitleaks_candidates(home):
        if os.path.isabs(candidate):
            if _executable(Path(candidate)):
                return True
        elif shutil.which(candidate):
            return True
    return False


def _try_gitleaks_package_manager(system: str) -> None:
    """One best-effort package-manager attempt; every return means "this route did not get us
    there", since the caller re-checks the floor and owns the release-download fallback."""
    if system == "Darwin":
        if shutil.which("brew") is None:
            _manual(
                "  [skip] gitleaks not installed: Homebrew is unavailable",
                "         Install Homebrew, then run: brew install gitleaks",
            )
            return
        result = _run(["brew", "install", "gitleaks"], BREW_INSTALL_SECONDS)
        if result.returncode == 0 and shutil.which("gitleaks"):
            print("  [fixed] gitleaks installed via Homebrew")
        else:
            note = expiry_note(result, BREW_INSTALL_SECONDS)
            print(
                f"  [skip] gitleaks Homebrew install failed{note}; retry: brew install gitleaks"
            )
        return
    if system != "Linux":
        where = "Windows Git Bash" if _windows_shell(system) else system
        _manual(
            f"  [skip] gitleaks bootstrap is unsupported on {where}",
            f"         Install it manually: {GITLEAKS_MANUAL}",
        )
        return
    manager = next((m for m in ("dnf", "apt-get") if shutil.which(m)), "")
    if not manager:
        print(
            "  [info] no recognised package manager (dnf, apt-get); the caller's no-sudo "
            "release download will handle it"
        )
        return
    sudo_cmd: list[str] = []
    if not _is_root():
        if not sudo_can_run(manager, "--version", env=_tokenless_env()):
            print(
                f"  [skip] gitleaks not installed: non-interactive sudo unavailable for {manager}"
            )
            print(f"         Install it manually: sudo {manager} install -y gitleaks")
            return
        sudo_cmd = ["sudo", "-n"]
    result = _run(
        [
            *sudo_cmd,
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            manager,
            "install",
            "-y",
            "gitleaks",
        ],
        PACKAGE_INSTALL_SECONDS,
        grace=PACKAGE_INSTALL_GRACE_SECONDS,
    )
    if result.returncode == 0 and shutil.which("gitleaks"):
        print(f"  [fixed] gitleaks installed via {manager}")
    else:
        note = expiry_note(result, PACKAGE_INSTALL_SECONDS)
        print(
            f"  [skip] gitleaks install attempt failed via {manager}{note}; install it "
            f"manually: sudo {manager} install -y gitleaks"
        )


def _place_gitleaks(
    release: str, version: str, asset: str, home: Path, tmpdir: Path
) -> None:
    """Download, VERIFY, extract, probe, then install; every failure is one ``[skip]``."""
    release_url = f"{GITLEAKS_DOWNLOAD}/{release}"
    archive = tmpdir / "gitleaks.tar.gz"
    download = _fetch(
        f"{release_url}/{asset}", archive, "120", GITLEAKS_DOWNLOAD_SECONDS
    )
    failed = (
        f"  [skip] gitleaks {version} download failed"
        + expiry_note(download, GITLEAKS_DOWNLOAD_SECONDS)
        + f"; install it manually: {GITLEAKS_MANUAL}"
    )
    if download.returncode != 0:
        print(failed)
        return
    if not _verified("gitleaks", version, release_url, archive, asset):
        return
    extract = _run(
        ["tar", "-xzf", str(archive), "-C", str(tmpdir), "gitleaks"], EXTRACT_SECONDS
    )
    if extract.returncode != 0:
        print(failed)
        return
    unpacked = tmpdir / "gitleaks"
    if os.path.lexists(unpacked) and not _confined_regular_file(unpacked, tmpdir):
        print(
            f"  [skip] gitleaks {version} archive member is not a regular file inside the "
            "download; leaving the existing binary alone"
        )
        return
    try:
        unpacked.chmod(unpacked.stat().st_mode | 0o111)
    except OSError:
        pass
    # Probe the DOWNLOADED binary before it reaches PATH, so a bad artifact never replaces a
    # working one.
    if _run([str(unpacked), "stdin", "--help"], PROBE_SECONDS).returncode != 0:
        print(
            f"  [skip] downloaded gitleaks {version} lacks the required 'stdin' subcommand; "
            "leaving the existing binary alone"
        )
        return
    if _install_binary(unpacked, home):
        print(
            f"  [fixed] gitleaks {version} installed to ~/.local/bin (no sudo; any system "
            "copy left untouched)"
        )
    else:
        print(f"  [skip] gitleaks {version} could not be installed to ~/.local/bin")


def _install_gitleaks_release(home: Path, system: str) -> None:
    """The no-sudo user-local install: the release is discovered at run time, so this never
    pins a stale build. Never fatal."""
    machine = platform.machine()
    arch = _GITLEAKS_ARCHES.get(machine)
    if arch is None:
        _manual(
            f"  [skip] gitleaks not installed: unsupported architecture {machine}",
            f"         Install it manually: {GITLEAKS_MANUAL}",
        )
        return
    if shutil.which("curl") is None or shutil.which("tar") is None:
        _manual(
            "  [skip] gitleaks not installed: curl and tar are required for the no-sudo download",
            "         Install them, then re-run install.sh.",
        )
        return
    release, version = _discover_release(GITLEAKS_RELEASES, _tokenless_env())
    if not release:
        _manual(
            "  [skip] gitleaks not installed: could not discover the latest release",
            "         Check network access, then re-run install.sh.",
        )
        return
    asset = f"gitleaks_{version}_{system.lower()}_{arch}.tar.gz"
    tmpdir = _make_tmpdir("gitleaks.")
    if tmpdir is None:
        print("  [skip] gitleaks not installed: could not create a temporary directory")
        return
    try:
        _place_gitleaks(release, version, asset, home, tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def ensure_gitleaks(home: Path) -> None:
    """Provision a gitleaks that can run the founding-ask gate's pinned invocation; never fail.

    ONE exit path: check the floor, try the package manager, check again, and fall back to the
    no-sudo release download, so no package-manager refusal can skip the download.
    """
    found = _gitleaks_floor_ok(home)
    if found:
        print(redact_secrets(f"  [ok]  gitleaks present: {found}"))
        return
    system = platform.system()
    if _gitleaks_present(home):
        _manual(
            "  [warn] the installed gitleaks cannot run the pinned founding-ask detector",
            "         invocation, so every Tier-2/Tier-3 capture would fail closed.",
            f"         Replacing it on this user's PATH with >= {GITLEAKS_MIN_VERSION}.",
        )
    else:
        _try_gitleaks_package_manager(system)
        if _gitleaks_floor_ok(home):
            return
    _install_gitleaks_release(home, system)
    if not _gitleaks_floor_ok(home):
        print(
            "  [warn] gitleaks still does not meet the floor; Tier-2/Tier-3 capture stays blocked"
        )


def gh_semver_core(text: str) -> str:
    """A reported version's bare semver core: ``2.4.0+dfsg1`` is ``2.4.0``, since compared raw a
    Debian revision sorts AFTER ``2.101.0`` and the stalest gh would never upgrade."""
    for line in text.splitlines():
        found = _SEMVER_CORE.match(line)
        if found:
            return found.group(1)
    return ""


def _gh_installed_version(home: Path) -> str:
    """The installed gh's version, ``~/.local/bin/gh`` FIRST (where this step installs; a PATH
    without that directory cannot see it, ISSUE-REF), or ``""`` when absent or unparseable."""
    local = home / ".local" / "bin" / "gh"
    if _executable(local):
        binary = str(local)
    elif shutil.which("gh"):
        binary = "gh"
    else:
        return ""
    lines = _run([binary, "--version"], PROBE_SECONDS, capture=True).stdout.splitlines()
    fields = lines[0].split() if lines else []
    reported = fields[2:3]
    return gh_semver_core(reported[0] if reported else "")


def _gh_latest_version() -> str:
    """The latest published gh release, resolved at run time so this never pins a stale client."""
    return gh_semver_core(_latest_tag(GH_RELEASES, _tokenless_env()).removeprefix("v"))


def _place_gh(version: str, asset: str, home: Path, tmpdir: Path) -> None:
    """Download, VERIFY, then install; every failure is one ``[skip]``."""
    release_url = f"{GH_DOWNLOAD}/v{version}"
    archive = tmpdir / asset
    download = _fetch(f"{release_url}/{asset}", archive, "180", DOWNLOAD_SECONDS)
    if download.returncode != 0:
        note = expiry_note(download, DOWNLOAD_SECONDS)
        print(
            f"  [skip] gh {version} download failed{note}; install it manually: {GH_MANUAL}"
        )
        return
    # NOT optional: this binary lands on PATH, where everything from `ghp pr merge` up runs it.
    if not _verified("gh", version, release_url, archive, asset):
        return
    extract = _run(["tar", "-xzf", str(archive), "-C", str(tmpdir)], EXTRACT_SECONDS)
    if extract.returncode != 0:
        print(f"  [skip] gh {version} archive could not be extracted")
        return
    # By glob rather than the documented gh_<ver>_linux_<arch>/bin/gh path, so a layout change
    # degrades to a reported skip instead of installing nothing silently.
    candidates = [
        *sorted(glob.glob(os.path.join(glob.escape(str(tmpdir)), "*", "bin", "gh"))),
        str(tmpdir / "bin" / "gh"),
        str(tmpdir / "gh"),
    ]
    extracted = next((Path(c) for c in candidates if os.path.lexists(c)), None)
    if extracted is None:
        print(f"  [skip] gh {version} archive contained no gh binary at any known path")
        return
    if not _confined_regular_file(extracted, tmpdir):
        print(
            f"  [skip] gh {version} archive member is not a regular file inside the download; "
            "the existing gh is left untouched"
        )
        return
    if _install_binary(extracted, home):
        print(
            f"  [fixed] gh {version} installed to ~/.local/bin (sha256 verified; no sudo; any "
            "system copy left untouched)"
        )
    else:
        print(f"  [skip] gh {version} could not be installed to ~/.local/bin")


def _install_gh_release(version: str, home: Path) -> None:
    machine = platform.machine()
    arch = _ARCHES.get(machine)
    if arch is None:
        _manual(
            f"  [skip] gh not upgraded: unsupported Linux architecture {machine}",
            f"         Install it manually: {GH_MANUAL}",
        )
        return
    asset = f"gh_{version}_linux_{arch}.tar.gz"
    tmpdir = _make_tmpdir("gh-cli.")
    if tmpdir is None:
        print(
            "  [skip] gh not upgraded: could not create a temporary download directory"
        )
        return
    try:
        _place_gh(version, asset, home, tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _gh_with_homebrew(home: Path) -> None:
    """Homebrew's formula tracks upstream gh, as the gitleaks and Doppler steps' macOS arms do."""
    if shutil.which("brew") is None:
        _manual(
            "  [skip] gh not upgraded: Homebrew is unavailable",
            "         Install Homebrew, then run: brew install gh",
        )
        return
    installed = _gh_installed_version(home)
    latest = _gh_latest_version()
    if installed and latest and installed == latest:
        print(f"  [ok]  gh {installed} is already the latest release (no download)")
    elif not latest:
        print("  [skip] gh version check skipped: could not reach the release API")
    elif not installed:
        result = _run(["brew", "install", "gh"], BREW_INSTALL_SECONDS)
        if result.returncode == 0:
            print("  [fixed] gh installed via Homebrew")
        else:
            note = expiry_note(result, BREW_INSTALL_SECONDS)
            print(f"  [skip] gh Homebrew install failed{note}; retry: brew install gh")
    else:
        result = _run(["brew", "upgrade", "gh"], BREW_INSTALL_SECONDS)
        if result.returncode == 0:
            print(
                f"  [fixed] gh upgraded via Homebrew (was {installed}, latest {latest})"
            )
        else:
            note = expiry_note(result, BREW_INSTALL_SECONDS)
            print(f"  [skip] gh Homebrew upgrade failed{note}; retry: brew upgrade gh")


def _gh_manual_step(system: str) -> None:
    """No release asset for this platform has been measured, and the wrong one would break gh
    outright, so the session upgrades it by hand."""
    step = manual_steps.ManualStep(
        id=GH_MANUAL_STEP,
        title=f"Upgrade the GitHub CLI by hand on {system}",
        why=(
            f"install.sh does not upgrade gh on {system}: no release asset for this platform "
            "has been measured, and the wrong one would break gh outright. An old gh does not "
            "fail loudly: the Ubuntu-archive gh cannot run 'gh issue view' at all."
        ),
        commands=[
            "winget upgrade --id GitHub.cli    # Windows; elsewhere download this platform's "
            "asset from https://github.com/cli/cli/releases/latest"
        ],
        follow="re-run bash install.sh; the step is done when it reports gh [ok]",
        source_step="gh",
    )
    try:
        if manual_steps.register(step):
            print(
                manual_steps.notice(step, manual_steps.ledger_path()), file=sys.stderr
            )
    except (OSError, ValueError, TypeError):
        print(
            f"  [warn] manual step {GH_MANUAL_STEP} not recorded (the manual-steps ledger "
            f"could not be written); upgrade gh by hand on {system}",
            file=sys.stderr,
        )


def ensure_gh(home: Path) -> None:
    """Keep this user's gh at the latest release (ISSUE-REF); never fail.

    Linux is the one platform that downloads a binary, because it is the one measured: when
    the latest release cannot be determined the gh present is left exactly as found, and when
    it matches the installed version nothing is downloaded or written.
    """
    system = platform.system()
    if system == "Darwin":
        _gh_with_homebrew(home)
        return
    if system != "Linux":
        _gh_manual_step(system)
        return
    if shutil.which("curl") is None or shutil.which("tar") is None:
        _manual(
            "  [skip] gh not upgraded: curl and tar are required for the no-sudo download",
            "         Install them, then re-run install.sh.",
        )
        return
    installed = _gh_installed_version(home)
    latest = _gh_latest_version()
    if not latest:
        # Unknown latest is NOT a reason to reinstall, or every offline machine downloads on
        # every run.
        if installed:
            print(
                f"  [skip] gh {installed} left as-is: could not discover the latest release"
            )
        else:
            _manual(
                "  [skip] gh not installed: could not discover the latest release",
                "         Check network access, then re-run install.sh.",
            )
        return
    if installed and installed == latest:
        print(f"  [ok]  gh {installed} is already the latest release (no download)")
        return
    # "Not at the latest" is the condition, so a build NEWER than the latest release is replaced
    # too; that converges, since every later run then matches.
    if not installed:
        print(f"  [gh]  gh is not installed; installing {latest} to ~/.local/bin")
    else:
        print(
            f"  [gh]  gh {installed} is not the latest ({latest}); replacing this user's copy"
        )
    _install_gh_release(latest, home)


#: The floor and the pinned release: firstmate needs jq's 1.7 behaviour (``trim``, keyword-named
#: variables, literal number output) and its CI runs Ubuntu 24.04's apt jq 1.7.1, so this matches
#: the version firstmate is tested with (ISSUE-REF).
JQ_VERSION = "1.7.1"
JQ_DOWNLOAD = f"https://github.com/jqlang/jq/releases/download/jq-{JQ_VERSION}"
JQ_CHECKSUMS = "sha256sum.txt"
JQ_MANUAL = "https://jqlang.org/download/"
JQ_DOWNLOAD_SECONDS = 130.0
_JQ_ARCHES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def _jq_reported(binary: str) -> str:
    """``binary --version`` (``jq-1.7.1``), or ``""`` when it cannot be run or says nothing."""
    words = _run([binary, "--version"], PROBE_SECONDS, capture=True).stdout.split()
    return words[0] if words else ""


def _jq_meets_floor(reported: str) -> bool:
    version = reported.removeprefix("jq-")
    return bool(re.match(r"[0-9]", version)) and _version_key(
        JQ_VERSION
    ) <= _version_key(version)


def _place_jq(asset: str, home: Path, tmpdir: Path) -> None:
    """Download, VERIFY, probe, then install; every failure is one ``[skip]``."""
    binary = tmpdir / "jq"
    download = _fetch(f"{JQ_DOWNLOAD}/{asset}", binary, "120", JQ_DOWNLOAD_SECONDS)
    if download.returncode != 0:
        print(
            f"  [skip] jq {JQ_VERSION} download failed"
            + expiry_note(download, JQ_DOWNLOAD_SECONDS)
            + f"; install it manually: {JQ_MANUAL}"
        )
        return
    if not _verified(
        "jq", JQ_VERSION, JQ_DOWNLOAD, binary, asset, checksums_name=JQ_CHECKSUMS
    ):
        return
    try:
        binary.chmod(binary.stat().st_mode | 0o111)
    except OSError:
        pass
    # Probe the DOWNLOADED binary before it reaches PATH, so a bad artifact never replaces a
    # working one.
    reported = _jq_reported(str(binary))
    if reported != f"jq-{JQ_VERSION}":
        print(
            f"  [skip] downloaded jq reports {reported or 'nothing'}, not jq-{JQ_VERSION}; "
            "leaving the system jq alone"
        )
        return
    if _install_binary(binary, home):
        print(
            f"  [fixed] jq {JQ_VERSION} installed to ~/.local/bin (no sudo; the system jq is "
            "left untouched)"
        )
    else:
        print(f"  [skip] jq {JQ_VERSION} could not be installed to ~/.local/bin")


def _install_jq_release(home: Path) -> None:
    machine = platform.machine()
    arch = _JQ_ARCHES.get(machine)
    if arch is None:
        _manual(
            f"  [skip] jq not installed: unsupported architecture {machine}",
            f"         Install jq {JQ_VERSION} or later manually: {JQ_MANUAL}",
        )
        return
    if shutil.which("curl") is None:
        _manual(
            "  [skip] jq not installed: curl is required for the no-sudo download",
            "         Install it, then re-run install.sh.",
        )
        return
    tmpdir = _make_tmpdir("jq.")
    if tmpdir is None:
        print("  [skip] jq not installed: could not create a temporary directory")
        return
    try:
        _place_jq(f"jq-linux-{arch}", home, tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def ensure_jq(home: Path) -> None:
    """A ``jq`` on PATH of at least ``JQ_VERSION``; never fail.

    The jq PATH resolves is the one every consumer runs, so that is what is measured. Below the
    floor on Linux the pinned release binary goes into ``~/.local/bin`` and the system package is
    never touched; elsewhere the step names the manual remedy. A current ``~/.local/bin/jq`` that
    an older jq earlier on PATH shadows is reported with the PATH fix, not downloaded again.
    """
    effective = shutil.which("jq")
    reported = _jq_reported(effective) if effective else ""
    if effective and _jq_meets_floor(reported):
        print(f"  [ok]  jq present: {effective} ({reported})")
        return
    local = home / ".local" / "bin" / "jq"
    local_reported = _jq_reported(str(local)) if _executable(local) else ""
    if _jq_meets_floor(local_reported):
        _shadowed_warning(local_reported, effective)
        return
    found = (
        f"jq {reported.removeprefix('jq-') or 'of unknown version'} is below {JQ_VERSION}"
        if effective
        else "jq is not installed"
    )
    if platform.system() != "Linux":
        _manual(
            f"  [warn] {found}; firstmate needs jq {JQ_VERSION} or later, and this platform "
            "has no no-sudo release route here.",
            f"         Install it with the system package manager: {JQ_MANUAL}",
        )
        return
    print(
        f"  [warn] {found}"
        + (", which firstmate needs" if effective else "")
        + f"; installing jq {JQ_VERSION} into ~/.local/bin"
    )
    _install_jq_release(home)
    local_reported = _jq_reported(str(local)) if _executable(local) else ""
    if not _jq_meets_floor(local_reported):
        print(
            f"  [warn] jq is still below {JQ_VERSION}; firstmate's jq 1.7 paths will fail"
        )
        return
    effective = shutil.which("jq")
    if not effective or Path(effective).resolve() != local.resolve():
        _shadowed_warning(local_reported, effective)


def _shadowed_warning(local_reported: str, effective: str | None) -> None:
    if effective is None:
        print(
            f"  [warn] ~/.local/bin/jq is {local_reported}, but ~/.local/bin is not on PATH; "
            "add it to PATH"
        )
        return
    print(
        f"  [warn] ~/.local/bin/jq is {local_reported}, but `jq` on PATH is {effective}; put "
        f"~/.local/bin ahead of {Path(effective).parent} on PATH"
    )
