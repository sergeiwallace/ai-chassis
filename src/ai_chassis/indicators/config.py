"""The indicators config, read at call time (design § Configuration).

Resolution is later-wins over four layers: these code defaults, ``[indicators]``,
``[machine.<profile>.indicators]``, then the XDG overlay's own copies of those two tables.
The tracked file and the overlay are read through ``agent_mail_notifier.read_config_tables``,
the shared loader. That loader deep-merges the overlay table by table, which would let a
tracked machine value beat an overlay ``[indicators]`` value, so the overlay's indicator
tables are applied once more on top and win over both tracked layers.
"""

from __future__ import annotations

import copy
import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "band": True,
    "toasts": {"injection": True, "reload": True, "message": True},
    "attempt_window_seconds": 1800,
    "terminal_show_seconds": 600,
    "stale_after_seconds": 300,
    "retrying_use_cases": [
        "compact",
        "continuation",
        "idle-nudge",
        "goal-loop-nudge",
        "inbox-nudge",
        "reauth-wake",
        "wq-nudge",
        "launch-resume",
    ],
    "pane_hotkey": "a",
    # Wall-clock budget for one pass, checked between adapters, inside the goal waker's
    # 90 s service timeout.
    "pass_budget_seconds": 15,
}
DEFAULT_LAYER = "default"
OVERLAY_LAYER = "overlay"
_PROMPT_INJECTION_DEFAULTS = (
    Path(__file__).resolve().parents[1] / "defaults" / "prompt-injection-config.json"
)


class ConfigError(ValueError):
    pass


def shipped_use_cases() -> frozenset[str]:
    """The prompt-injection library's shipped ``use_cases`` names."""
    data = json.loads(_PROMPT_INJECTION_DEFAULTS.read_text(encoding="utf-8"))
    return frozenset(data["use_cases"])


def indicators_declares_a_capability(table: Mapping[str, Any]) -> bool:
    """Whether a ``[machine.<p>.indicators]`` table leaves the projector on there."""
    return table.get("enabled", DEFAULTS["enabled"]) is True


@dataclass(frozen=True)
class ResolvedConfig:
    profile: str | None
    values: Mapping[str, Any]
    layers: Mapping[str, str]  # key (toasts.<kind> for toasts) -> the layer that set it
    dropped: tuple[str, ...]  # retrying_use_cases names the library does not ship
    source: str

    @property
    def enabled(self) -> bool:
        return self.values["enabled"]

    @property
    def band(self) -> bool:
        return self.values["band"]

    @property
    def attempt_window_seconds(self) -> float:
        return self.values["attempt_window_seconds"]

    @property
    def terminal_show_seconds(self) -> float:
        return self.values["terminal_show_seconds"]

    @property
    def stale_after_seconds(self) -> float:
        return self.values["stale_after_seconds"]

    @property
    def retrying_use_cases(self) -> tuple[str, ...]:
        return tuple(self.values["retrying_use_cases"])

    @property
    def pass_budget_seconds(self) -> float:
        return self.values["pass_budget_seconds"]

    def display(self) -> dict[str, Any]:
        """The block written into the contract, so the mod needs no TOML reader."""
        return {
            "band": self.band,
            "toasts": dict(self.values["toasts"]),
            "pane_hotkey": self.values["pane_hotkey"],
            "stale_after_seconds": self.stale_after_seconds,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "source": self.source,
            "values": copy.deepcopy(dict(self.values)),
            "layers": dict(sorted(self.layers.items())),
            "dropped": list(self.dropped),
        }


def defaults(profile: str | None) -> ResolvedConfig:
    return resolve({}, profile, {}, use_cases=shipped_use_cases(), source="default")


def overlay_path(env: Mapping[str, str]) -> Path:
    """The XDG overlay ``read_config_tables`` merges, by the same rule."""
    xdg_root = (env.get("XDG_CONFIG_HOME") or "").strip()
    base = Path(xdg_root) if xdg_root else Path.home() / ".config"
    return base / "ai-harness" / "ai-harness.local.toml"


def load(
    profile: str | None,
    *,
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedConfig:
    """Read and resolve the config now; ConfigError when a layer cannot be read or is invalid."""
    from ai_chassis.agent_mail_notifier import read_config_tables  # noqa: PLC0415 - heavy

    environment = os.environ if env is None else env
    try:
        tables = read_config_tables(path, env=dict(environment))
    except RuntimeError as error:
        raise ConfigError(str(error)) from error
    overlay_file = overlay_path(environment)
    overlay: dict[str, Any] = {}
    if overlay_file.exists():
        try:
            overlay = tomllib.loads(overlay_file.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigError(f"overlay file {overlay_file}: {error}") from error
    return resolve(
        tables,
        profile,
        overlay,
        use_cases=shipped_use_cases(),
        source=str(tables.get("_config_source", DEFAULT_LAYER)),
    )


def _machine_table(tables: Mapping[str, Any], profile: str | None) -> Any:
    machines = tables.get("machine")
    if not profile or not isinstance(machines, dict):
        return None
    machine = machines.get(profile)
    return machine.get("indicators") if isinstance(machine, dict) else None


def resolve(
    tables: Mapping[str, Any],
    profile: str | None,
    overlay: Mapping[str, Any],
    *,
    use_cases: frozenset[str],
    source: str,
) -> ResolvedConfig:
    values = copy.deepcopy(DEFAULTS)
    layers = {key: DEFAULT_LAYER for key in DEFAULTS if key != "toasts"}
    layers.update({f"toasts.{kind}": DEFAULT_LAYER for kind in DEFAULTS["toasts"]})
    machine_layer = f"machine.{profile}.indicators"
    for layer, table in (
        ("indicators", tables.get("indicators")),
        (machine_layer, _machine_table(tables, profile)),
        (OVERLAY_LAYER, overlay.get("indicators")),
        (OVERLAY_LAYER, _machine_table(overlay, profile)),
    ):
        if table is None:
            continue
        if not isinstance(table, dict):
            raise ConfigError(f"{layer} is not a table")
        for key, value in table.items():
            _check(layer, key, value)
            if key == "toasts":
                for kind, switch in value.items():
                    values["toasts"][kind] = switch
                    layers[f"toasts.{kind}"] = layer
            else:
                values[key] = value
                layers[key] = layer
    kept = [name for name in values["retrying_use_cases"] if name in use_cases]
    dropped = tuple(
        name for name in values["retrying_use_cases"] if name not in use_cases
    )
    values["retrying_use_cases"] = kept
    return ResolvedConfig(profile, values, layers, dropped, source)


def _check(layer: str, key: str, value: Any) -> None:
    where = f"{layer}.{key}"
    if key not in DEFAULTS:
        raise ConfigError(f"{where}: unknown key")
    default = DEFAULTS[key]
    if not isinstance(default, dict):
        _check_value(where, default, value)
        return
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: {value!r} is not a table")
    for kind, switch in value.items():
        if kind not in default:
            raise ConfigError(f"{where}.{kind}: unknown key")
        _check_value(f"{where}.{kind}", default[kind], switch)


def _check_value(where: str, default: Any, value: Any) -> None:
    """Refuse a value whose type is not its default's (bool is never a number here)."""
    if isinstance(default, bool):
        ok, wanted = isinstance(value, bool), "true or false"
    elif isinstance(default, int):
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        ok, wanted = number and value >= 0, "a number >= 0"
    elif isinstance(default, str):
        ok, wanted = isinstance(value, str), "a string"
    else:
        ok = isinstance(value, list) and all(isinstance(item, str) for item in value)
        wanted = "a list of strings"
    if not ok:
        raise ConfigError(f"{where}: {value!r} is not {wanted}")
