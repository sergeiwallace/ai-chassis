"""Provision a durable, fingerprint-capable tmux: install.sh's retired Step 1g (ISSUE-REF, ISSUE-REF).

The auto-compact transport types into a tmux pane and identifies it by expanding a format the tmux
SERVER evaluates; a server too old to know a specifier emits it literally, so the transport needs a
build at or above ``MINIMUM_TMUX_VERSION``, not merely "some tmux". On a machine whose home is rebuilt
on restart the build lives on persistent storage beside the projects root and is COPIED (never
linked: 72 launcher call sites would each pay the mount's latency) into ``~/.local/bin`` by an atomic
rename (the replaced binary may be running). The newest master wins, compared numerically.

With no master at all the step asks whether the tmux already present clears the floor: if so it
stays silent, if not it runs the repository's builder once and, when that succeeds, discovers again.
It warns and continues on every arm: tmux is one capability among many.
"""

from __future__ import annotations

import filecmp
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from . import dependency_bootstrap
from .dependency_bootstrap import expiry_note, relay_redacted
from .manual_steps import redact_secrets

#: Relative to the persistent root (the projects root's parent): a GLOB, never a pinned version.
DURABLE_GLOB_RELPATH = "tools/tmux-*/bin/tmux"
BUILDER_RELPATH = "scripts/provision_durable_tmux.sh"
BUILDER_SECONDS = 1800.0
BUILDER_GRACE_SECONDS = 10.0

_ORDER = re.compile(r"([0-9]*)[^.]*\.([0-9]*)(.*)", re.DOTALL)
#: tmux's own version forms: `3.4`, `3.3a`, `next-3.5`, `3.4-rc2`. Anything else a stand-in or a
#: corrupt binary prints is "no version", and is never echoed.
_VERSION_SHAPE = re.compile(r"(next-)?[0-9]+\.[0-9]+[a-z]?(-rc[0-9]+)?")


def transport_floor() -> str:
    """The floor the context supervisor's transport enforces, read from it rather than mirrored."""
    from .context_supervisor import MINIMUM_TMUX_VERSION

    major, minor = MINIMUM_TMUX_VERSION
    return f"{major}.{minor}"


def version_gt(a: str, b: str) -> bool:
    """Whether ``a`` is a strictly newer tmux version than ``b``.

    (major, minor) are compared as integers, because "3.10" sorts below "3.7" as a string while
    being the newer build, and only then the patch letter lexically. A version with no minor is
    not a tmux version and compares as neither newer nor older.
    """
    a, b = a.removeprefix("next-"), b.removeprefix("next-")
    if "." not in a or "." not in b:
        return False
    left, right = _ORDER.fullmatch(a), _ORDER.fullmatch(b)
    if left is None or right is None or not all(left.groups()[:2] + right.groups()[:2]):
        return False
    a_key = (int(left[1]), int(left[2]), left[3])
    b_key = (int(right[1]), int(right[2]), right[3])
    return a_key > b_key


def meets_floor(version: str) -> bool:
    """At or above the floor. An EMPTY version is below it: a machine with no tmux is not fine."""
    if not version:
        return False
    floor = transport_floor()
    return version == floor or version_gt(version, floor)


def _binary_version(binary: Path | str) -> str:
    """The version a tmux binary reports for ``-V``, or ``""``."""
    result = dependency_bootstrap.run_bounded(
        [str(binary), "-V"], seconds=dependency_bootstrap.PROBE_SECONDS, capture=True
    )
    fields = result.stdout.split()
    version = fields[1] if result.returncode == 0 and len(fields) > 1 else ""
    return redact_secrets(version) if _VERSION_SHAPE.fullmatch(version) else ""


def effective_version(home: Path) -> str:
    """The version of the tmux this machine would actually USE, or ``""``.

    The provisioning target is asked before PATH: once a master has been provisioned the target is
    what a session runs, and answering from the system's tmux would report a build nothing uses.
    """
    target = home / ".local" / "bin" / "tmux"
    if target.is_file() and os.access(target, os.X_OK):
        return _binary_version(target)
    found = shutil.which("tmux")
    return _binary_version(found) if found else ""


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _warn(*lines: str) -> None:
    for line in lines:
        print(line, file=sys.stderr)


def _handle_absent_master(projects_root: Path, home: Path, harness_root: Path) -> bool:
    """Decide what a machine with no master needs; ``True`` when the builder just built one."""
    current = effective_version(home)
    if meets_floor(current):
        print(f"  [ok]  tmux {current} meets the transport floor {transport_floor()}")
        return False
    builder = harness_root / BUILDER_RELPATH
    if _executable(builder):
        result = dependency_bootstrap.run_bounded(
            [str(builder), "--if-needed"],
            seconds=BUILDER_SECONDS,
            grace=BUILDER_GRACE_SECONDS,
            env={**os.environ, "PROJECTS_DIR": str(projects_root)},
            capture=True,
        )
        relay_redacted(result.stdout, sys.stdout)
        relay_redacted(result.stderr, sys.stderr)
        if result.returncode == 0:
            return True
        note = expiry_note(result, BUILDER_SECONDS)
        if note:
            _warn(f"  [warn] the tmux builder{note}.")
    _warn(
        "  [warn] auto-compact cannot work on this machine yet.",
        f"         This machine's tmux is {current or 'absent'}, below the transport floor {transport_floor()},",
        f"         and there is no durable build under {projects_root.parent}/tools to provision from.",
        "         The transport types /compact into a tmux pane and identifies the pane",
        "         through a format string the tmux SERVER expands; an older server emits the",
        "         specifier verbatim, so the gate refuses rather than typing into the wrong pane.",
        "         Fix it by running:",
        f"           {builder}",
        "         That builds the newest tmux onto persistent storage; re-run install.sh",
        "         afterwards and Step 1g will provision it. If it reports missing build",
        "         dependencies, it prints the one apt command to run first.",
        "         The install is otherwise complete -- this is the only affected capability.",
    )
    return False


def _newest_master(candidates: list[Path]) -> tuple[Path | None, str]:
    """The executable candidate reporting the newest version; each is asked, not trusted by name."""
    master: Path | None = None
    version = ""
    for candidate in candidates:
        if not _executable(candidate):
            continue
        reported = _binary_version(candidate)
        if reported and (master is None or version_gt(reported, version)):
            master, version = candidate, reported
    return master, version


def _copy_atomically(
    master: Path, target_dir: Path, target: Path, version: str
) -> bool:
    """Replace ``target`` by rename so a running binary keeps its inode; report why on failure."""
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        descriptor, name = tempfile.mkstemp(prefix=".tmux.", dir=target_dir)
    except OSError:
        print(f"  [skip] durable tmux: {target_dir} is not writable")
        return False
    os.close(descriptor)
    try:
        shutil.copyfile(master, name)
        os.chmod(name, 0o755)
        os.replace(name, target)
    except OSError:
        Path(name).unlink(missing_ok=True)
        print(f"  [skip] durable tmux: could not install {version} into {target_dir}")
        return False
    print(
        f"  [fixed] tmux {version} provisioned into {target_dir} from persistent storage"
    )
    return True


def _identical(master: Path, target: Path) -> bool:
    try:
        return target.exists() and filecmp.cmp(master, target, shallow=False)
    except OSError:
        return False


def provision(
    projects_root: Path, home: Path, harness_root: Path, *, retried: bool = False
) -> None:
    """Provision the newest durable master into ``home``'s bin directory; never fail the install."""
    persistent = projects_root.parent
    candidates = sorted(persistent.glob(DURABLE_GLOB_RELPATH))
    if not candidates:
        # One retry only: the builder writes the master the glob just missed, and `--if-needed`
        # also succeeds when one is already present, so an unguarded retry would recurse forever.
        if not retried and _handle_absent_master(projects_root, home, harness_root):
            provision(projects_root, home, harness_root, retried=True)
        return
    master, version = _newest_master(candidates)
    if master is None:
        print(
            f"  [skip] durable tmux: no master under {persistent}/{DURABLE_GLOB_RELPATH} "
            "reported a version"
        )
        return
    target_dir = home / ".local" / "bin"
    target = target_dir / "tmux"
    if _identical(master, target):
        print(f"  [ok]  tmux {version} already provisioned ({target})")
    elif not _copy_atomically(master, target_dir, target, version):
        return
    # A client-only upgrade is the dangerous-looking state: the server keeps its version until
    # every session exits, and the fingerprint is expanded by the SERVER.
    asked = dependency_bootstrap.run_bounded(
        [str(target), "display-message", "-p", "#{version}"],
        seconds=dependency_bootstrap.PROBE_SECONDS,
        capture=True,
    )
    server = asked.stdout.rstrip("\n") if asked.returncode == 0 else ""
    server = redact_secrets(server) if _VERSION_SHAPE.fullmatch(server) else ""
    if server and server != version:
        _warn(
            f"  [warn] a live tmux server reports {server} but the client is now {version} —",
            "         relaunch every session so client and server match. Auto-compact stays",
            "         refused until then: it gates on the SERVER version, not the binary.",
        )
