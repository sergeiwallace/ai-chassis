"""Re-exec repo scripts under an interpreter meeting their runtime requirements.

This module intentionally remains importable by Python 3.8 so it can repair an
old OS ``python3`` before the calling script imports newer standard-library APIs.
"""

import os
import pathlib
import shutil
import subprocess
import sys


_SENTINEL = "AI_HARNESS_PY_BOOTSTRAP_REEXEC"


def _checkout_root(script_file):
    """Return the checkout containing *script_file*, if one can be found."""
    directory = os.path.dirname(os.path.realpath(script_file))
    while True:
        if os.path.isfile(os.path.join(directory, "pyproject.toml")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def _main_checkout_root(root):
    """Return a linked worktree's distinct main checkout, if resolvable."""
    git_file = os.path.join(root, ".git")
    if not os.path.isfile(git_file):
        return None
    try:
        lines = pathlib.Path(git_file).read_text(encoding="utf-8").splitlines()
        line = lines[0] if lines else ""
        gitdir = line[len("gitdir:") :].strip()
        if not line.startswith("gitdir:") or not gitdir:
            return None
        gitdir = os.path.realpath(os.path.join(root, gitdir))
        common = pathlib.Path(gitdir, "commondir").read_text(encoding="utf-8").strip()
        if not common:
            return None
        common = os.path.realpath(os.path.join(gitdir, common))
    except (OSError, ValueError):
        return None
    main_root = os.path.dirname(common)
    if os.path.basename(common) != ".git" or main_root == root:
        return None
    return main_root


def _candidates(script_file):
    root = _checkout_root(script_file)
    if root:
        yield os.path.join(root, ".venv", "bin", "python")
        yield os.path.join(root, ".venv", "Scripts", "python.exe")
        main_root = _main_checkout_root(root)
        if main_root:
            yield os.path.join(main_root, ".venv", "bin", "python")
            yield os.path.join(main_root, ".venv", "Scripts", "python.exe")
    for name in ("python3.14", "python3.13", "python3.12", "python3.11"):
        yield shutil.which(name) or name


def _supports_minimum(candidate, minimum, required_modules=()):
    """Prove *candidate* meets the version and optional dependency requirements."""
    probe = (
        "import sys, importlib; "
        "[importlib.import_module(name) for name in "
        + repr(tuple(required_modules))
        + "]; "
        "raise SystemExit(0 if sys.version_info >= " + repr(tuple(minimum)) + " else 1)"
    )
    try:
        return (
            subprocess.run(
                [candidate, "-c", probe],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # stdin too, not just the output streams: an unredirected child inherits
                # fd 0, and fd 0 here is often a hook payload the caller has not read
                # yet. This probe has no business seeing it, let alone consuming it.
                stdin=subprocess.DEVNULL,
                check=False,
            ).returncode
            == 0
        )
    except OSError:
        return False


def _is_entry_point(caller_file):
    """True when *caller_file* is the script the user actually invoked.

    ``os.execv`` restarts the ENTIRE process, re-running the entry point's module body
    from the top. That is safe only while nothing has happened yet, which is true when
    the entry point calls this at its own import time and false when the call is reached
    through a lazily imported sibling: by then the entry point may have consumed stdin,
    written files or acquired a lock, and none of it is undone.

    Measured (2026-09-19, macOS ``/usr/bin/python3`` 3.9.6): ``worktree_create_naming.py``
    read its hook payload, then imported ``claude_sessions``, whose module-level call
    re-exec'd -- one pid, module body entered twice, the second pass reading an
    already-drained fd 0. The payload loss was silent because the restart is invisible.
    """
    if caller_file is None:
        # No frame to inspect (a stripped interpreter, or an exec'd string). Treat it as
        # the entry point: that preserves the pre-existing behaviour rather than
        # withholding a repair on a guess.
        return True
    try:
        return os.path.realpath(caller_file) == os.path.realpath(sys.argv[0])
    except OSError:
        return True


def _caller_file():
    """The ``__file__`` of whoever called ``ensure_supported_python``, if knowable."""
    try:
        return sys._getframe(2).f_globals.get("__file__")
    except (AttributeError, ValueError):  # no frame support, or too shallow a stack
        return None


def ensure_supported_python(minimum=(3, 11), *, required_modules=()):
    """Prove the version and optional imports before entry-point side effects.

    A modern bare Python can meet the version floor while lacking project
    dependencies. Callers needing them must opt in here, before reading stdin or
    creating files, so the existing candidate ladder can safely repair that case.
    """
    if sys.version_info >= minimum:
        try:
            for module in required_modules:
                __import__(module)  # daemon-fingerprint: dependency-probe
        except ImportError:
            pass
        else:
            return

    caller_file = _caller_file()
    if not _is_entry_point(caller_file):
        # A re-exec here would restart the entry point with its side effects already
        # done. Say so and return: the caller's own 3.11-only imports will fail next,
        # which is a readable error in the right place, unlike a silent restart.
        print(
            "warning: {0} asked to re-exec under Python {1}.{2}+ but the running script "
            "is {3}, which has already started; refusing to restart it mid-run. Call "
            "ensure_supported_python() at the top of {3} instead.".format(
                os.path.basename(caller_file),
                minimum[0],
                minimum[1],
                sys.argv[0] or "<unknown>",
            ),
            file=sys.stderr,
        )
        if required_modules:
            raise SystemExit(3)
        return

    candidates = list(_candidates(sys.argv[0]))
    if not os.environ.get(_SENTINEL):
        for candidate in candidates:
            if _supports_minimum(candidate, minimum, required_modules):
                os.environ[_SENTINEL] = "1"
                os.execv(candidate, [candidate] + sys.argv)

    version = ".".join(str(part) for part in sys.version_info[:3])
    tried = ", ".join(candidates) if candidates else "none"
    dependencies = (
        "; required importable modules: " + ", ".join(required_modules)
        if required_modules
        else ""
    )
    print(
        "ai-harness requires Python {0}.{1}+; running {2}. Tried: {3}{4} "
        "(interpreter: {5})".format(
            minimum[0], minimum[1], version, tried, dependencies, sys.executable
        ),
        file=sys.stderr,
    )
    raise SystemExit(3)
