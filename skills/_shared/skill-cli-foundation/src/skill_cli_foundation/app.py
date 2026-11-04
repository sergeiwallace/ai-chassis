"""`make_app()` factory -- consistent Typer config, opt-in shared callback.

Callback is **opt-in**, not default (Spike B): a shared ``@app.callback()``
forces Typer to require a subcommand name on the CLI, which would break the
single-command, no-command-name invocation surface most skills use. Single
command skills compose shared flags via the ``Annotated`` option aliases
instead; only genuinely multi-subcommand skills opt into the callback.
"""

from __future__ import annotations

from dataclasses import dataclass

import typer

from .options import Mode, ModeOption


@dataclass
class SharedContext:
    """Global flags stashed in ``ctx.obj`` for multi-subcommand skills."""

    mode: Mode | None = None


def make_app(name: str, *, multi_command: bool = False) -> typer.Typer:
    """Build a `typer.Typer` with consistent config.

    Single-command (default): no callback is wired, preserving the
    no-command-name invocation surface. ``multi_command=True`` wires a shared
    callback that stashes global flags in ``ctx.obj`` (a ``SharedContext``).
    """
    app = typer.Typer(name=name, add_completion=False, no_args_is_help=True)

    if multi_command:

        @app.callback()
        def _shared(ctx: typer.Context, mode: ModeOption = None) -> None:
            ctx.obj = SharedContext(mode=mode)

    return app
