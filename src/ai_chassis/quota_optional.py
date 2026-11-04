"""Reach the optional quota admission component by name, never by import.

``ai_chassis.quota_traffic`` gates automatic wakes and launches on the account's quota. It
is optional: a source extract may omit it. Importing it statically from the modules that
use it would drag the whole package into their import closure, so they call it through the
``admission`` object here. It resolves the real component on each call, so a test or a
caller that patches the real module still takes effect, and when the component is absent
it admits everything and says so once on stderr rather than failing silently.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from types import ModuleType
from typing import Any

PACKAGE = "ai_chassis.quota_traffic"
ADMISSION_MODULE = f"{PACKAGE}.admission"


def _admit(*_args: Any, **_kwargs: Any) -> bool:
    return True


def _nothing(*_args: Any, **_kwargs: Any) -> None:
    return None


def _no_items(*_args: Any, **_kwargs: Any) -> list[Any]:
    return []


# What each operation returns when there is no component to ask: nothing is held, nothing
# is replayed, and every item is admitted.
_ADMIT_ALL: dict[str, Callable[..., Any]] = {
    "check": _admit,
    "complete": _nothing,
    "replay_items": _no_items,
    "held_items": _no_items,
}
_announced = False


def _import_module(name: str) -> ModuleType:
    return importlib.import_module(name)


def _import_optional(module_name: str) -> ModuleType | None:
    """Import ``module_name``, or return None when the quota package is not here."""
    try:
        return _import_module(module_name)
    except ModuleNotFoundError as error:
        # Whole module names only: a different missing module is a real failure and
        # must stay loud.
        if error.name not in {module_name, PACKAGE}:
            raise
        return None


def component(name: str) -> ModuleType | None:
    """Return ``ai_chassis.quota_traffic.<name>``, or None when the package is absent."""
    return _import_optional(f"{PACKAGE}.{name}")


def _load() -> ModuleType | None:
    """Return the real admission module, or None when this checkout omits it."""
    global _announced  # noqa: PLW0603 - one plain line per process
    module = _import_optional(ADMISSION_MODULE)
    if module is None and not _announced:
        _announced = True
        print(
            "quota admission: component not included in this extract; "
            "admitting everything",
            file=sys.stderr,
        )
    return module


class _Admission:
    """Forwards each attribute to the real admission module, or to admit-all."""

    def __getattr__(self, name: str) -> Any:
        module = _load()
        if module is not None:
            return getattr(module, name)
        try:
            return _ADMIT_ALL[name]
        except KeyError:
            raise AttributeError(name) from None


admission = _Admission()
