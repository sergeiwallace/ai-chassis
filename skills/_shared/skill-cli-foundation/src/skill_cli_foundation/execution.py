"""Narrow argv-only execution seam for typed skill drivers."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path


class CommandSpecError(ValueError):
    """A driver attempted to use an unsafe command representation."""


def run_argv(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Execute a prevalidated argv without a shell or implicit pass-through."""
    if (
        isinstance(argv, str)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
    ):
        raise CommandSpecError(
            "command must be a non-empty argv sequence of non-empty strings"
        )
    env = None if environment is None else {**os.environ, **environment}
    completed = subprocess.run(list(argv), cwd=cwd, env=env, check=False)
    return completed.returncode
