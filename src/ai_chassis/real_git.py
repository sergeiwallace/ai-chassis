"""Put the real git ahead of the macOS ``/usr/bin/git`` xcrun shim (ISSUE-REF).

On macOS ``/usr/bin/git`` is an xcrun shim. Spawned from the harness venv's interpreter (uv
cpython 3.14.7) it was measured stalling in ``_dyld_start`` for 0.2-13 s per call interactively
and 26-29 s to minutes under launchd, while the binary ``xcrun --find git`` names started in
0.01 s (example-client-host-mac 2026-10-13). ``~/.local/bin`` is first on PATH in every ai-harness LaunchAgent
and agent shell, so a ``git`` there that execs the real binary removes the stall for all of them.

It is a wrapper and never a symlink: git resolves its exec-path (``git-remote-https`` and the
other helpers) relative to the path it was invoked by, so a symlinked git fails every HTTPS fetch
and push with ``'remote-https' is not a git command`` (measured live 2026-10-13 08:32-08:44Z).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

SHIM = "/usr/bin/git"
# Read at call time so a test can point either at a stub.
PLATFORM = sys.platform
XCRUN = "/usr/bin/xcrun"
XCRUN_TIMEOUT_SECONDS = 20
MARKER = "# ISSUE-REF: exec the real git past the /usr/bin xcrun shim"


def wrapper_text(real: str) -> str:
    return (
        "#!/bin/sh\n"
        f"{MARKER} (stalls in launchd jobs).\n"
        "# A wrapper, not a symlink: a symlinked git resolves its helpers (git-remote-https)\n"
        "# relative to the link path and breaks every fetch/push.\n"
        f'exec {shlex.quote(real)} "$@"\n'
    )


def _xcrun_git() -> tuple[str | None, str]:
    """The binary ``xcrun --find git`` names, or None and why it is unusable.

    Each refusal names its own arm: a timed-out xcrun (the shim family stalls in
    ``_dyld_start``, ISSUE-REF) and a refusing one need different remedies (ISSUE-REF).
    """
    try:
        found = subprocess.run(
            [XCRUN, "--find", "git"],
            capture_output=True,
            text=True,
            check=False,
            timeout=XCRUN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return (
            None,
            f"{XCRUN} --find git did not answer within {XCRUN_TIMEOUT_SECONDS} s",
        )
    except OSError as exc:
        return None, f"{XCRUN} could not be run: {exc}"
    if found.returncode:
        reason = f"{XCRUN} --find git exited {found.returncode}"
        detail = found.stderr.strip().splitlines()
        return None, f"{reason}: {detail[0]}" if detail else reason
    real = found.stdout.strip()
    if not real or real == SHIM or not os.access(real, os.X_OK):
        return (
            None,
            f"{XCRUN} --find git named {real!r}, not an executable git past the shim",
        )
    return real, ""


def _is_own_wrapper(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(512).decode("utf-8", errors="replace")
    except OSError:
        return False
    return head.startswith("#!/bin/sh\n") and f"\n{MARKER}" in head


def _kept(path: Path, real: str, wanted: str) -> str | None:
    """The report line when ``path`` must not be written, else None.

    Only this step's own wrapper, or its retired symlink form (a link resolving to the binary
    xcrun names), is ever replaced; anything else at the path is somebody's and is left alone.
    """
    if path.is_symlink():
        if os.path.realpath(path) == os.path.realpath(real):
            return None
        return f"  [skip] git: {path} is a link to {os.readlink(path)}, not this step's; left alone"
    if not path.exists():
        return None
    if not _is_own_wrapper(path):
        return f"  [skip] git: {path} is not this step's wrapper; left alone"
    if path.read_text(encoding="utf-8", errors="replace") == wanted:
        return f"  [ok]  git: {path} execs {real}, unchanged"
    return None


def _write(path: Path, real: str, wanted: str) -> str:
    staged = path.with_name(f".git.tmp.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(wanted, encoding="utf-8")
        staged.chmod(0o755)
        os.replace(staged, path)
    except OSError as exc:
        staged.unlink(missing_ok=True)
        return f"  [warn] git: could not write {path}: {exc}"
    return f"  [fixed] git: {path} now execs {real} (past the xcrun shim)"


def ensure_real_git_wrapper(home: Path) -> str | None:
    """Write ``~/.local/bin/git`` as an exec wrapper for the real git; return the report line.

    Never install-failing: every arm reports and returns, and off macOS nothing is reported.
    """
    if PLATFORM != "darwin":
        return None
    if not os.path.exists(SHIM):
        return f"  [skip] git: {SHIM} is absent, no xcrun shim to resolve past"
    real, reason = _xcrun_git()
    if real is None:
        return f"  [skip] git: {reason}; ~/.local/bin/git left as found"
    path = home / ".local" / "bin" / "git"
    wanted = wrapper_text(real)
    return _kept(path, real, wanted) or _write(path, real, wanted)
