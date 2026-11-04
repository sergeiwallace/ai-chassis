"""Shared `Annotated` option aliases -- the primary shared-flag mechanism.

Skills place these directly on a single command's signature so every skill CLI
imports the same type, validation, and help text instead of re-declaring
`typer.Option(...)` per script. Enum-typed aliases (``ModeOption``) validate at
parse time, so an invalid value exits non-zero without per-script code.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated

import typer

SHARED_SHORT_ALIASES = {
    "mode": "-m",
    "model": "-M",
    "dry_run": "-n",
}


def validate_shared_short_aliases(
    aliases: dict[str, str] = SHARED_SHORT_ALIASES,
) -> None:
    """Reject a collision in the shared short-option namespace."""
    values = list(aliases.values())
    if len(values) != len(set(values)):
        raise ValueError("shared short option aliases must be unique")


class Mode(str, Enum):
    """The shared ``--mode`` toggle: interactive authoring vs. autonomous run."""

    interactive = "interactive"
    automated = "automated"


ModeOption = Annotated[
    Mode | None,
    typer.Option(
        SHARED_SHORT_ALIASES["mode"], "--mode", help="interactive | automated"
    ),
]

ModelOption = Annotated[
    str | None,
    typer.Option(
        SHARED_SHORT_ALIASES["model"], "--model", help="Model override for the run"
    ),
]

ModelIdOption = Annotated[
    str | None,
    typer.Option(
        SHARED_SHORT_ALIASES["model"],
        "--model-id",
        help="Concrete deployment identifier for the run",
    ),
]

DryRunOption = Annotated[
    bool,
    typer.Option(
        SHARED_SHORT_ALIASES["dry_run"],
        "--dry-run",
        help="Plan the run without side effects",
    ),
]


validate_shared_short_aliases()
