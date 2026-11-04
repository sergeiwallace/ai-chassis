#!/usr/bin/env python3
# ruff: noqa: E402
"""Detect project PATH entries that shadow fleet-managed editable CLI tools.

Run this from a project's effective environment, normally with:

    direnv exec . python3 scripts/check_editable_install_shadowing.py

The installer verifies that the global tools are editable. This companion check
verifies that an activated project environment has not put another executable
before the canonical ``~/.local/bin`` wrapper.
"""

from __future__ import annotations

try:
    from py_interpreter_bootstrap import ensure_supported_python
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):
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

import argparse
import os
import subprocess
from pathlib import Path

DEFAULT_TOOLS = ("aido", "ai-core-mcp", "core")


def which_all(tool: str) -> list[Path]:
    """Return ``which -a`` results in PATH precedence order."""
    result = subprocess.run(
        ["which", "-a", tool],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]


def _same_path(left: Path, right: Path) -> bool:
    """Compare lexical absolute paths; canonical wrappers are often symlinks."""
    return os.path.abspath(left.expanduser()) == os.path.abspath(right.expanduser())


def check_tool(tool: str, canonical_bin_dir: Path) -> tuple[bool, str]:
    """Return whether the canonical executable wins PATH precedence for ``tool``."""
    canonical = canonical_bin_dir.expanduser() / tool
    candidates = which_all(tool)
    canonical_index = next(
        (
            index
            for index, candidate in enumerate(candidates)
            if _same_path(candidate, canonical)
        ),
        None,
    )
    if canonical_index is None:
        found = ", ".join(str(candidate) for candidate in candidates) or "not found"
        return (
            False,
            f"FAIL: {tool}: canonical {canonical} is not on PATH (found: {found})",
        )

    shadowing = candidates[:canonical_index]
    if shadowing:
        return (
            False,
            f"FAIL: {tool}: {shadowing[0]} precedes canonical {canonical} in PATH",
        )
    return True, f"PASS: {tool}: canonical {canonical} has PATH precedence"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tool",
        action="append",
        dest="tools",
        metavar="BINARY",
        help="binary to check (repeatable; defaults to all enforced global CLIs)",
    )
    parser.add_argument(
        "--canonical-bin-dir",
        type=Path,
        default=Path("~/.local/bin"),
        help="directory containing canonical global wrappers (default: ~/.local/bin)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    failures = 0
    for tool in args.tools or DEFAULT_TOOLS:
        passed, message = check_tool(tool, args.canonical_bin_dir)
        print(message)
        failures += not passed
    if failures:
        print(f"FAIL: {failures} editable global CLI precedence check(s) failed")
        return 1
    print("PASS: no editable global CLI is shadowed in PATH")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
