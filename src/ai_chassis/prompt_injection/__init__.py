"""Prompt injection and auto-submission into live Claude Code sessions.

Design: ``docs/designs/issue-ref-prompt-injection-library.md``. Standard library
only, and nothing here imports ``context_supervisor``, ``reboot_resume`` or
``scripts/`` (D-10; ``tests/prompt_injection/test_import_boundary.py``).

Public names (stable from 0.1.0; additions allowed, renames need a design
amendment):

- ``SessionTarget``, ``resolve_target``, ``TargetUnresolved``: ``self``,
  ``session:<name>``, ``pane:%N``, ``key:<key>``.
- ``Payload``, ``SlashCommandPayload``, ``PromptPayload``.
- ``Injector``, ``GoalInjector``, ``IdleNudgeInjector``, ``InjectionOutcome``.
- ``command_available``, ``goal_state``, ``verify``, ``load_config``.
- ``in_boundary``, ``drain``: the save-state-to-compact boundary hold (ISSUE-REF);
  ``hold_legacy`` and ``BoundaryState`` live in ``ai_chassis.prompt_injection.boundary``.

Use-case injectors outside the consumer contract (``CompactInjector``, and the goal
waker's ``GoalClearInjector`` / ``GoalLoopNudgeInjector``, ISSUE-REF) live in
``ai_chassis.prompt_injection.injector``.
"""

from ai_chassis.prompt_injection.boundary import drain, in_boundary
from ai_chassis.prompt_injection.config import LIBRARY_VERSION, load_config
from ai_chassis.prompt_injection.injector import (
    GoalInjector,
    IdleNudgeInjector,
    Injector,
    verify,
)
from ai_chassis.prompt_injection.outcomes import InjectionOutcome
from ai_chassis.prompt_injection.payload import (
    Payload,
    PromptPayload,
    SlashCommandPayload,
)
from ai_chassis.prompt_injection.preflight import command_available
from ai_chassis.prompt_injection.targets import (
    SessionTarget,
    TargetUnresolved,
    resolve_target,
)
from ai_chassis.prompt_injection.verify import goal_state

__version__ = LIBRARY_VERSION

__all__ = [
    "GoalInjector",
    "IdleNudgeInjector",
    "InjectionOutcome",
    "Injector",
    "Payload",
    "PromptPayload",
    "SessionTarget",
    "SlashCommandPayload",
    "TargetUnresolved",
    "command_available",
    "drain",
    "goal_state",
    "in_boundary",
    "load_config",
    "resolve_target",
    "verify",
]
