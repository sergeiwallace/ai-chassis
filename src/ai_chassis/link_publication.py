"""Publish the checkout's helper files into the home directory (ISSUE-REF batch 6).

install.sh's retired Steps 5, 5b, 5bb, 5bb2 and 5bb2a. Each links a file under the checkout's
MAIN working tree, through ``shell_startup.publish_link``, into one of three places:

* ``~/.claude``: the statusline command and its shared usage-status config;
* ``~/.claude/bin``: every helper the checkout ships in ``.claude/bin``;
* ``~/.local/bin``: the hook entrypoints, then every other bare-named wrapper, then the mdb
  wrapper over the example-client-host kit's own link.

Why ``~/.local/bin`` at all: a ``language: system`` pre-commit hook resolves its bare entry name
through the HOOK SUBPROCESS's PATH, not the interactive shell's. The helper-path step appends
``~/.claude/bin`` to ``.bashrc``/``.zshenv``, which a non-interactive bash never reads (a login
shell reads ``.profile``, and Ubuntu's ``.profile`` sources ``.bashrc`` only past its own
``case $- in *i*)`` guard, which returns first for ``bash -c``, ``ssh host cmd`` and any SSM
payload; ISSUE-REF). Measured, not assumed: a real ``git commit`` through pre-commit with a bare
``test-gate`` entry printed ``Executable test-gate not found`` with ``~/.claude/bin`` published.
``~/.local/bin`` IS on that subprocess's PATH (the uv-tool convention; pre-commit itself lives
there), so the executable is placed where the PATH already points rather than extending the
PATH. A consumer repository has no local copy of a hook entrypoint, so this publication is its
only distribution mechanism.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from .links import LinkError
from .shell_startup import publish_link

#: Step 5's two files, linked from ``<checkout>/.claude`` into ``~/.claude`` under the same name.
STATUSLINE_FILES = ("statusline-command.sh", "usage-status-config.json")
#: The sweep never publishes these by name. The mdb wrapper finds the vendored example-client-host tool by
#: READING ``~/.local/bin/mdb``, so publishing itself there would make it resolve to itself;
#: ``wrap_vendored_mdb`` publishes it instead, after moving the kit's link aside.
SWEEP_EXCLUDED = frozenset({"mdb"})


def _helper_dir(checkout: Path) -> Path:
    return checkout / ".claude" / "bin"


def _make_dir(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LinkError(f"could not create {directory}: {exc}") from exc


def _entries(directory: Path) -> list[Path]:
    """The directory's entries in name order, without dot-names, as the shell's ``*`` glob."""
    try:
        return sorted(
            entry for entry in directory.iterdir() if not entry.name.startswith(".")
        )
    except OSError as exc:
        raise LinkError(f"could not list {directory}: {exc}") from exc


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def _make_executable(path: Path, umask: int) -> None:
    """``chmod +x``, best effort: a failure is ignored, as the shell's ``2>/dev/null || true``."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        wanted = mode | (0o111 & ~umask)
        if wanted != mode:
            os.chmod(path, wanted)
    except OSError:
        pass


def publish_statusline(checkout: Path, home: Path, environ: Mapping[str, str]) -> None:
    """Step 5: link the statusline command and its usage-status config into ``~/.claude``.

    A source the checkout does not ship is a ``[skip]`` naming it, as Steps 5b and 5ba treat an
    absent source, where the shell linked a dangling path and printed ``[ok]``. A present source
    whose link cannot be made still fails the step (``publish_link`` raises ``LinkError``).
    """
    for name in STATUSLINE_FILES:
        source = checkout / ".claude" / name
        if not source.is_file():
            print(f"  [skip] ~/.claude/{name}: {source} is not shipped")
            continue
        if not publish_link(source, home / ".claude" / source.name, environ):
            continue
        print(f"  [ok]  ~/.claude/{source.name} → ai-harness/.claude/{source.name}")


def publish_helper_bins(
    checkout: Path,
    home: Path,
    environ: Mapping[str, str],
    *,
    excluded: frozenset[str] = frozenset(),
) -> None:
    """Step 5b: link every regular file in ``<checkout>/.claude/bin`` into ``~/.claude/bin``."""
    source_dir = _helper_dir(checkout)
    if not source_dir.is_dir():
        print("  [skip] .claude/bin/ is not shipped; helper installation skipped")
        return
    target_dir = home / ".claude" / "bin"
    _make_dir(target_dir)
    entries = [entry for entry in _entries(source_dir) if entry.name not in excluded]
    umask = _umask()
    for entry in entries:
        _make_executable(entry, umask)
    for entry in entries:
        if entry.is_file():
            publish_link(entry, target_dir / entry.name, environ)
    print("  [ok]  ~/.claude/bin/ ← ai-harness/.claude/bin/* (ctx-pct, quota-pct)")


def publish_tier_b_entrypoints(
    checkout: Path, home: Path, names: Sequence[str], environ: Mapping[str, str]
) -> None:
    """Step 5bb: link each declared hook entrypoint into ``~/.local/bin``.

    A declared name the checkout does not ship is a silent skip: the check that fails the
    install when a required entrypoint does not resolve stays in install.sh's Step 5d. Unlike
    the sweep below, an existing link is repointed, as the shell's ``ln -sf`` did; a regular file
    that is not ``publish_link``'s own copy is left as found and warned.
    """
    local_bin = home / ".local" / "bin"
    if not names:
        print(
            "  [skip] ~/.local/bin/ shared entrypoints: install.tier_b_entrypoints is empty"
        )
        return
    _make_dir(local_bin)
    for name in names:
        source = _helper_dir(checkout) / name
        if source.is_file():
            publish_link(source, local_bin / name, environ)
    print(f"  [ok]  ~/.local/bin/ ← shared entrypoints ({', '.join(names)})")


def publish_bare_wrappers(
    checkout: Path,
    home: Path,
    environ: Mapping[str, str],
    *,
    excluded: frozenset[str] = frozenset(),
) -> None:
    """Step 5bb2 (ISSUE-REF): link EVERY bare-named wrapper into ``~/.local/bin``.

    Never fails the install: ``~/.local/bin`` is shared with uv, pip and vendor kits, so a
    collision is reported and stepped around. The selector is the whole directory, because a
    second allowlist is what left ``bd-new``, ``bdx``, ``cx`` and ``ghp`` unresolvable from a
    non-interactive shell for weeks (measured on example-project-ec2 2026-10-02). A dotted name is a
    sourced library or module every caller addresses by absolute path, so it is not published
    as a command; a non-executable file is skipped; ``SWEEP_EXCLUDED`` is skipped by name. A
    destination is replaced only when it is already a symlink resolving to this very wrapper.
    """
    source_dir = _helper_dir(checkout)
    dest_dir = home / ".local" / "bin"
    if not source_dir.is_dir():
        return
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        entries = sorted(source_dir.iterdir())
    except OSError:
        return
    published = refused = 0
    for source in entries:
        name = source.name
        if not source.is_file() or "." in name or name in SWEEP_EXCLUDED | excluded:
            continue
        if not os.access(source, os.X_OK):
            continue
        dest = dest_dir / name
        if dest.is_symlink() or dest.exists():
            if not dest.is_symlink():
                print(
                    f"  [warn] {dest} is a real file, not a managed symlink; leaving it alone",
                    file=sys.stderr,
                )
                refused += 1
                continue
            resolved = os.path.realpath(dest)
            if resolved != os.path.realpath(source):
                print(
                    f"  [warn] {dest} resolves to {resolved}, not this wrapper; leaving it alone",
                    file=sys.stderr,
                )
                refused += 1
                continue
        try:
            publish_link(source, dest, environ)
        except LinkError as exc:
            print(f"  [warn] {exc}; leaving it alone", file=sys.stderr)
            refused += 1
            continue
        published += 1
    left = f" ({refused} left alone)" if refused else ""
    print(
        f"  [ok]  ~/.local/bin/ ← {published} bare-named ~/.claude/bin wrappers{left}"
    )


def wrap_vendored_mdb(checkout: Path, home: Path, environ: Mapping[str, str]) -> None:
    """Step 5bb2a (ISSUE-REF): publish the mdb wrapper over the example-client-host kit's own link.

    The ISSUE-REF wrapper pins an interpreter that can import ``requests``, but the ``claude``
    process ``ai c`` launches carries ``~/.local/bin`` and no ``~/.claude/bin`` (measured on
    example-project-ec2 2026-10-12), so a session's bare ``mdb`` reached the kit's link, whose
    ``exec python3`` found an interpreter without ``requests``. So the kit's link is MOVED (never
    deleted) to ``~/.local/bin/.mdb-vendored`` and the wrapper is linked in its place; the
    wrapper reads the aside link first and never resolves the tool through itself. The kit
    versions itself through its own ``tools/mdb/latest`` link, so the move does not change how
    it runs; a kit that writes its link again is moved aside again on the next apply, and
    ``mv ~/.local/bin/.mdb-vendored ~/.local/bin/mdb`` undoes it.

    Only a SYMLINK is moved: a real file there is somebody's own and is reported and left. A
    host with no kit (no link, nothing aside) gets no mdb, as before. Never fails the install.
    """
    source = _helper_dir(checkout) / "mdb"
    local_bin = home / ".local" / "bin"
    dest = local_bin / "mdb"
    aside = local_bin / ".mdb-vendored"
    if not source.is_file():
        return
    moved = False
    if dest.is_symlink():
        if os.path.realpath(dest) == os.path.realpath(source):
            return
        try:
            os.replace(dest, aside)
        except OSError:
            print(
                f"  [warn] could not move {dest} aside; bare mdb still reaches the kit's "
                "interpreter",
                file=sys.stderr,
            )
            return
        moved = True
        print(f"  [ok]  example-client-host kit's mdb link moved aside to {aside}")
    elif dest.exists():
        print(
            f"  [warn] {dest} is a real file, not the example-client-host kit's link; leaving it alone, so bare "
            "mdb skips the interpreter-fixing wrapper",
            file=sys.stderr,
        )
        return
    elif not aside.is_symlink():
        return
    try:
        publish_link(source, dest, environ)
    except LinkError as exc:
        # A kit link this run moved goes back, so a failed link never leaves bare mdb resolving
        # to nothing at all.
        if moved:
            try:
                os.replace(aside, dest)
            except OSError:
                pass
        print(
            f"  [warn] {exc}; bare mdb skips the interpreter-fixing wrapper",
            file=sys.stderr,
        )
        return
    print("  [ok]  ~/.local/bin/mdb ← the interpreter-fixing wrapper")
