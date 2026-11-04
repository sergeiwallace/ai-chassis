"""Contain optional producer effects while preserving the primary operation's result."""

import contextlib
import sys


def attempt(action, *args, **kwargs):
    try:
        return action(*args, **kwargs)
    except Exception as exc:
        # Arbitrary exception text can contain credentials or source content.
        with contextlib.suppress(Exception):
            print("wake producer unavailable:" + type(exc).__name__, file=sys.stderr)
        return None


def _call(name, *args, **kwargs):
    # Keep imports inside the optional boundary, including on dependency-poor hosts.
    from . import producers

    return getattr(producers, name)(*args, **kwargs)


@contextlib.contextmanager
def notifying(name, *args, **kwargs):
    intent = (
        attempt(name, *args, **kwargs)
        if callable(name)
        else attempt(_call, name, *args, **kwargs)
    )
    try:
        yield
    finally:
        if intent is not None:
            attempt(_call, "complete", intent)
