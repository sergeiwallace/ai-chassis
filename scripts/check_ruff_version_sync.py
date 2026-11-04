#!/usr/bin/env python3
# ruff: noqa: E402
"""Fail if pyproject.toml's ruff pin and .pre-commit-config.yaml's ruff rev
disagree (SW ruff-unification sweep, 2026-07-27).

The pre-commit hook (ruff-pre-commit) and a developer's `uv run ruff` are two
separate binaries provisioned from two separate pins. Nothing keeps them in
sync by default, so a repo can be green in the pre-commit gate and red at the
terminal (or vice versa) with no warning. This script is wired in as a local
pre-commit hook so the disagreement is caught at commit time, not discovered
months later by a confused agent.

A floor (`ruff>=X`) in pyproject.toml is also treated as a failure even if the
resolved lockfile version happens to match today: a floor lets `uv lock
--upgrade` (or a fresh clone with no lockfile) silently drift past the
pre-commit pin. Only an exact `ruff==X.Y.Z` pin is accepted.
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

import re
import sys
from pathlib import Path

PRECOMMIT_REV_RE = re.compile(
    r"repo:\s*https://github\.com/astral-sh/ruff-pre-commit\s*\n\s*rev:\s*v([0-9][0-9A-Za-z.]*)"
)
PYPROJECT_PIN_RE = re.compile(r'"ruff==([0-9][0-9A-Za-z.]*)"')
PYPROJECT_FLOOR_RE = re.compile(r'"ruff(>=|>|~=|<=|<|!=)[0-9]')


def check(root: Path) -> list[str]:
    """Return a list of error messages; empty means everything is in sync."""
    precommit_path = root / ".pre-commit-config.yaml"
    pyproject_path = root / "pyproject.toml"

    if not precommit_path.exists() or not pyproject_path.exists():
        return []

    precommit_text = precommit_path.read_text()
    pyproject_text = pyproject_path.read_text()

    if "astral-sh/ruff-pre-commit" not in precommit_text:
        return []  # non-python tech stack, or ruff hook not present here

    errors: list[str] = []

    precommit_rev = PRECOMMIT_REV_RE.search(precommit_text)
    if precommit_rev is None:
        errors.append(
            "ruff-version-sync: could not parse the ruff-pre-commit rev out of "
            ".pre-commit-config.yaml"
        )
        return errors

    pyproject_pin = PYPROJECT_PIN_RE.search(pyproject_text)
    if pyproject_pin is None:
        if PYPROJECT_FLOOR_RE.search(pyproject_text):
            errors.append(
                "ruff-version-sync: pyproject.toml pins ruff with a floor "
                "(e.g. ruff>=X) instead of an exact ruff==X.Y.Z pin -- a floor "
                "lets `uv lock --upgrade` drift away from the pre-commit rev "
                f"(currently v{precommit_rev.group(1)})"
            )
        else:
            errors.append(
                "ruff-version-sync: pyproject.toml has no ruff dependency pin "
                f"to compare against the pre-commit rev (v{precommit_rev.group(1)})"
            )
        return errors

    if precommit_rev.group(1) != pyproject_pin.group(1):
        errors.append(
            f"ruff-version-sync: pre-commit pins ruff {precommit_rev.group(1)} "
            f"but pyproject.toml pins ruff {pyproject_pin.group(1)} -- these "
            "must be identical (the pre-commit hook and `uv run ruff` are "
            "different binaries; a mismatch means green-in-gate can be "
            "red-in-terminal, or vice versa)."
        )

    return errors


def main() -> int:
    errors = check(Path.cwd())
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
