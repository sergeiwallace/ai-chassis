"""Call-time host configuration layers, shared by Python wake entry points.

The tracked (or explicitly supplied private) profile owns plugin configuration.
Only the user overlay is restricted to host selectors, key path and cutover tuning.
"""

from copy import deepcopy
import math
import os
from pathlib import Path
import sys
import tomllib

SELECTORS = ("dispatch", "producers")
OVERLAY_KEYS = {
    "dispatch": {"enabled"},
    "producers": {"enabled", "signing_key"},
    "cutover": {"ack_timeout_s", "settle_window_s"},
}


def overlay_path(environ=None):
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME") or env.get("USERPROFILE") or Path.home())
    if sys.platform == "win32" or env.get("OS") == "Windows_NT":
        home = Path(env.get("USERPROFILE") or home)
        directory = Path(env.get("APPDATA") or home / "AppData/Roaming")
    else:
        directory = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    return directory / "ai-harness/wake-dispatch.toml"


def validate_overlay(document):
    for section, values in document.items():
        if (
            section not in OVERLAY_KEYS
            or not isinstance(values, dict)
            or set(values) - OVERLAY_KEYS[section]
        ):
            raise ValueError("invalid-wake-overlay-key")
        for key, value in values.items():
            if key == "enabled":
                valid = isinstance(value, bool)
            elif key == "signing_key":
                valid = isinstance(value, str) and bool(value)
            else:
                valid = (
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and math.isfinite(value)
                    and value > 0
                )
            if not valid:
                raise ValueError("invalid-wake-overlay-value")
    return document


def read_overlay(path):
    try:
        body = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("unreadable-wake-overlay") from None
    try:
        validate_overlay(tomllib.loads(body.decode("utf-8")))
    except (ValueError, UnicodeError):
        # Never echo TOML diagnostics: they can include private profile values.
        raise ValueError("invalid-wake-overlay") from None
    return body


def _merge(destination, incoming):
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(destination.get(key), dict):
            _merge(destination[key], value)
        else:
            destination[key] = deepcopy(value)


def resolve(default_path, environ=None):
    env = os.environ if environ is None else environ
    try:
        document = tomllib.loads(Path(default_path).read_text())
    except (OSError, ValueError):
        document = {}
    layers = {name: "default" for name in SELECTORS}
    overlay = read_overlay(overlay_path(env))
    inputs = [("overlay", tomllib.loads(overlay.decode()) if overlay else {})]
    override = env.get("AIH_WAKE_DISPATCH_CONFIG")
    if override:
        try:
            inputs.append(("environment", tomllib.loads(Path(override).read_text())))
        except (OSError, ValueError):
            raise ValueError("invalid-wake-environment-config") from None
    for layer, values in inputs:
        _merge(document, values)
        for name in SELECTORS:
            if isinstance(values.get(name), dict) and "enabled" in values[name]:
                if not isinstance(values[name]["enabled"], bool):
                    raise ValueError("invalid-wake-selector")
                layers[name] = layer
    return document, layers
