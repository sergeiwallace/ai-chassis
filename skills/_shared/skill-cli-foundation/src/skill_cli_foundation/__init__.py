"""skill-cli-foundation -- shared Typer CLI primitives for ai-harness skills.

Exports the shared primitives of the package design: ``make_app`` (app factory,
opt-in callback), the ``Annotated`` option aliases (``ModeOption``,
``ModelOption``, ``ModelIdOption``, ``DryRunOption``), ``BaseSkillSettings`` (config precedence),
and the ``cli_runner`` pytest fixture (via ``skill_cli_foundation.testing``).
"""

from __future__ import annotations

from .app import SharedContext, make_app
from .execution import CommandSpecError, run_argv
from .options import DryRunOption, Mode, ModelIdOption, ModelOption, ModeOption
from .settings import BaseSkillSettings, CallerSettings

__all__ = [
    "BaseSkillSettings",
    "CallerSettings",
    "CommandSpecError",
    "DryRunOption",
    "Mode",
    "ModeOption",
    "ModelIdOption",
    "ModelOption",
    "SharedContext",
    "make_app",
    "run_argv",
]
