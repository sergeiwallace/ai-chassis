"""Library configuration, read at every call, and the filesystem roots it governs.

Two files matter. The library's own ``prompt-injection-config.json`` (D-7) is
validated fail-closed: a file that exists but does not validate is treated as
``enabled: false`` and logged, never silently replaced by the defaults. For the
legacy use cases the host system's own master switch in
``context-supervisor-config.json`` is ALSO read, with the legacy semantics
reproduced exactly (see :func:`legacy_injection_enabled`).
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from importlib import resources
from pathlib import Path
from typing import Any

#: Written into every attempt record (D-10).
LIBRARY_VERSION = "0.1.0"

LEGACY_USE_CASES = frozenset({"compact", "continuation", "reauth-wake"})

_DEFAULTS_NAME = "prompt-injection-config.json"
_LANDING_TIMEOUT_MAX_SECONDS = 30
_BUILTINS_NAME = "prompt-injection-builtins.json"


def claude_root() -> Path:
    """Claude Code's user root: ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path.home() / ".claude"


def resume_root() -> Path:
    """``${AIH_RESUME_ROOT:-~/.claude/resume}``, the legacy observability root."""
    return Path(os.environ.get("AIH_RESUME_ROOT", Path.home() / ".claude" / "resume"))


def config_path() -> Path:
    return Path(
        os.environ.get("AIH_PROMPT_INJECTION_CONFIG_PATH")
        or Path.home() / ".claude" / "state" / _DEFAULTS_NAME
    )


def legacy_config_path() -> Path:
    # Copied from context_supervisor._config_path (460-464 at bff30e522).
    return Path(
        os.environ.get("AIH_CONTEXT_SUPERVISOR_CONFIG_PATH")
        or Path.home() / ".claude" / "state" / "context-supervisor-config.json"
    )


def safe_component(value: str) -> str:
    # Copied from context_supervisor._safe_marker_component (2590-2591).
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def now() -> int:
    return int(time.time())


def _read_packaged(name: str) -> dict[str, Any]:
    text = (resources.files("ai_chassis") / "defaults" / name).read_text("utf-8")
    return json.loads(text)


def defaults() -> dict[str, Any]:
    return _read_packaged(_DEFAULTS_NAME)


def builtin_registry() -> dict[str, dict[str, str]]:
    return dict(_read_packaged(_BUILTINS_NAME)["commands"])


def _number(value: object, minimum: float, maximum: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and minimum <= value <= maximum
    )


def _integer(value: object, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and (minimum <= value <= maximum)
    )


_VALIDATORS = {
    "enabled": lambda v: isinstance(v, bool),
    "landing_timeout_seconds": lambda v: _number(v, 0, 30),
    "ready_wait_seconds": lambda v: _number(v, 0, 600),
    "ready_poll_seconds": lambda v: _number(v, 0.01, 10),
    "max_typed_chars": lambda v: _integer(v, 1, 4000),
    "completion_timeout_seconds": lambda v: _number(v, 0, 3600),
    "restage_backoff_ceiling_seconds": lambda v: _integer(v, 60, 3600),
    "debug_diagnostics_enabled": lambda v: isinstance(v, bool),
}


def _boundary_hold_valid(block: object) -> bool:
    if not isinstance(block, dict):
        return False
    if "enabled" in block and not isinstance(block["enabled"], bool):
        return False
    return "ttl_seconds" not in block or _integer(block["ttl_seconds"], 60, 86400)


# multiline_mode (D-13 Outcome): max_chars is bounded by Claude Code's 4,000-character
# /goal condition cap; a live file may set any subset, merged over the defaults.
# verified_cc_versions lists every exact version the live probe passed on (ISSUE-REF).
_MULTILINE_VALIDATORS = {
    "enabled": lambda v: isinstance(v, bool),
    "verified_cc_versions": lambda v: (
        isinstance(v, list)
        and bool(v)
        and all(isinstance(item, str) and bool(item.strip()) for item in v)
    ),
    "max_chars": lambda v: _integer(v, 1, 4000),
    "chunk_chars": lambda v: _integer(v, 1, 4000),
    "chunk_pause_seconds": lambda v: _number(v, 0, 5),
}


# stage_walk (ISSUE-REF, design § The walk): the pre-submit walk of a draft the
# composer clips; its bounds are key presses, its settle the pause before each capture.
# late_frame_seconds (ISSUE-REF): how much longer than the keyed-view bound the walk waits
# for a frame a key must change, before it presses anything else.
_STAGE_WALK_VALIDATORS = {
    "enabled": lambda v: isinstance(v, bool),
    "max_filler_rows": lambda v: _integer(v, 1, 50),
    "max_row_moves": lambda v: _integer(v, 1, 2000),
    "capture_settle_seconds": lambda v: _number(v, 0, 5),
    "staged_quiet_seconds": lambda v: _number(v, 0, 5),
    "late_frame_seconds": lambda v: _number(v, 0, 120),
    "undo_max_retries": lambda v: _integer(v, 1, 100),
}


# capability (ISSUE-REF): the per-version capability check. inconclusive_escalate_after:
# how many inconclusive results in a row for one version identity before the check reports
# a plain failure with its /bug-fix repair (still never cached).
_CAPABILITY_VALIDATORS = {
    "inconclusive_escalate_after": lambda v: _integer(v, 1, 100),
}


# validation (ISSUE-REF, D-19): the pre-submit verify and the post-submit check.
_VALIDATION_VALIDATORS = {
    "pre_submit": lambda v: isinstance(v, bool),
    "post_submit": lambda v: isinstance(v, bool),
}

# use_cases.<name>.self_heal (ISSUE-REF, D-18): the repair loop's policy; the cap is
# The maintainer's 3 by default for start-goal, bounded 1..10 here.
_SELF_HEAL_VALIDATORS = {
    "enabled": lambda v: isinstance(v, bool),
    "max_rounds": lambda v: _integer(v, 1, 10),
}


def _nested_error(name: str, block: object, validators: dict[str, Any]) -> str | None:
    if not isinstance(block, dict):
        return f"invalid-{name}"
    for key, valid in validators.items():
        if key in block and not valid(block[key]):
            return f"invalid-{name}.{key}"
    return None


def _block_error(config: dict[str, Any]) -> str | None:
    """The first invalid nested block (``boundary_hold``, ``multiline_mode``,
    ``stage_walk``), or None."""
    if "boundary_hold" in config and not _boundary_hold_valid(config["boundary_hold"]):
        return "invalid-boundary_hold"
    return (
        _nested_error(
            "multiline_mode", config.get("multiline_mode", {}), _MULTILINE_VALIDATORS
        )
        or _nested_error(
            "stage_walk", config.get("stage_walk", {}), _STAGE_WALK_VALIDATORS
        )
        or _nested_error(
            "validation", config.get("validation", {}), _VALIDATION_VALIDATORS
        )
        or _nested_error(
            "capability", config.get("capability", {}), _CAPABILITY_VALIDATORS
        )
    )


def _use_case_error(name: str, entry: object) -> str | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("enabled"), bool):
        return f"invalid-use_cases.{name}"
    if "self_heal" in entry and _nested_error(
        "self_heal", entry["self_heal"], _SELF_HEAL_VALIDATORS
    ):
        return f"invalid-use_cases.{name}"
    if "landing_attachment" in entry and not _landing_attachment_valid(
        entry["landing_attachment"]
    ):
        return f"invalid-use_cases.{name}"
    return None


def _landing_attachment_valid(block: object) -> bool:
    return (
        isinstance(block, dict)
        and isinstance(block.get("type"), str)
        and all(isinstance(v, (str, bool, int)) for v in block.values())
    )


def _validation_error(config: object) -> str | None:
    if not isinstance(config, dict):
        return "not-an-object"
    for name, valid in _VALIDATORS.items():
        if name in config and not valid(config[name]):
            return f"invalid-{name}"
    block_error = _block_error(config)
    if block_error is not None:
        return block_error
    use_cases = config.get("use_cases", {})
    if not isinstance(use_cases, dict):
        return "invalid-use_cases"
    for name, entry in use_cases.items():
        error = _use_case_error(name, entry)
        if error is not None:
            return error
    return None


# Blocks a live file overrides key by key rather than replacing whole.
_MERGED_BLOCKS = (
    "use_cases",
    "boundary_hold",
    "multiline_mode",
    "stage_walk",
    "validation",
    "capability",
)


def validation(config: dict[str, Any]) -> dict[str, bool]:
    """``validation`` over the shipped defaults, so a caller-built config without the
    block still validates as shipped."""
    block = config.get("validation")
    return {**defaults()["validation"], **(block if isinstance(block, dict) else {})}


def self_heal_policy(config: dict[str, Any], use_case: str) -> tuple[bool, int]:
    """``use_cases.<use_case>.self_heal`` as ``(enabled, max_rounds)``, read at call time.
    Absent means disabled (D-19: validate, record, notify, never inject)."""
    entry = config.get("use_cases", {}).get(use_case)
    block = entry.get("self_heal") if isinstance(entry, dict) else None
    if not isinstance(block, dict):
        return False, 0
    shipped = defaults()["use_cases"].get(use_case, {}).get("self_heal", {})
    merged = {**shipped, **block}
    return merged.get("enabled") is True, int(merged.get("max_rounds", 0))


def landing_attachment(config: dict[str, Any], use_case: str) -> dict[str, Any] | None:
    """``use_cases.<use_case>.landing_attachment``: the attachment whose fields, all
    equal, prove a submission that writes no command record (ISSUE-REF). A caller-built
    config without the use case falls back to the shipped entry."""
    entry = config.get("use_cases", {}).get(use_case)
    if not isinstance(entry, dict):
        entry = defaults()["use_cases"].get(use_case, {})
    block = entry.get("landing_attachment")
    return dict(block) if _landing_attachment_valid(block) else None


def boundary_hold(config: dict[str, Any]) -> tuple[bool, int]:
    """``boundary_hold`` as ``(enabled, ttl_seconds)``. A caller-built config
    without the block falls back to the shipped defaults, so a consumer passing
    its own dict (the supervisor's cutover legs do) still holds."""
    shipped = defaults()["boundary_hold"]
    block = config.get("boundary_hold")
    if not isinstance(block, dict):
        block = shipped
    ttl = block.get("ttl_seconds", shipped["ttl_seconds"])
    return block.get("enabled") is True, int(ttl)


def stage_walk(config: dict[str, Any]) -> dict[str, Any]:
    """``stage_walk`` over the shipped defaults, so a caller-built config without the
    block still walks as shipped."""
    block = config.get("stage_walk")
    return {**defaults()["stage_walk"], **(block if isinstance(block, dict) else {})}


def capability_settings(config: dict[str, Any]) -> dict[str, Any]:
    """``capability`` over the shipped defaults."""
    block = config.get("capability")
    return {**defaults()["capability"], **(block if isinstance(block, dict) else {})}


def load_config() -> dict[str, Any]:
    """The effective library config, read now.

    A missing live file means the shipped defaults. A live file that does not
    parse or validate yields the defaults with ``enabled`` forced ``False`` and
    ``invalid_reason`` set, and is logged once per call as
    ``CC-LIB-CONFIG-INVALID``; it never yields the defaults unchanged.
    """
    effective = defaults()
    path = config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        effective["source"] = "defaults"
        return effective
    except OSError as error:
        return _invalid(effective, f"unreadable:{type(error).__name__}")
    try:
        live = json.loads(text)
    except json.JSONDecodeError:
        return _invalid(effective, "malformed-json")
    error = _validation_error(live)
    if error is not None:
        return _invalid(effective, error)
    merged = {
        name: {**copy.deepcopy(effective[name]), **live.get(name, {})}
        for name in _MERGED_BLOCKS
    }
    # One level deeper for use cases, so a live entry naming only `enabled` keeps the
    # shipped `self_heal` policy (ISSUE-REF).
    merged["use_cases"] = {
        name: {**effective["use_cases"].get(name, {}), **entry}
        for name, entry in merged["use_cases"].items()
    }
    effective.update({k: v for k, v in live.items() if k not in _MERGED_BLOCKS})
    effective.update(merged)
    effective["source"] = str(path)
    return effective


def _invalid(effective: dict[str, Any], reason: str) -> dict[str, Any]:
    effective["enabled"] = False
    effective["invalid_reason"] = reason
    effective["source"] = str(config_path())
    _log_invalid(reason)
    return effective


def _log_invalid(reason: str) -> None:
    from ai_chassis.prompt_injection.observe import write_stage_line

    write_stage_line("CC-LIB-CONFIG-INVALID", "library", f"reason={reason}")


def landing_timeout_seconds(config: dict[str, Any]) -> float | None:
    """The landing budget, env-overridable and bounded 0-30 as the legacy does.

    Copied from context_supervisor._landing_timeout_seconds (4169-4171) and
    _bounded_environment_number (4142-4149): an out-of-range or unparsable
    override yields ``None``, which callers treat as "do not press Enter".
    """
    default = float(config["landing_timeout_seconds"])
    try:
        value = float(os.environ.get("AIH_LANDING_TIMEOUT_SECONDS", str(default)))
    except ValueError:
        return None
    return value if 0 <= value <= _LANDING_TIMEOUT_MAX_SECONDS else None


# --- Legacy master switch -------------------------------------------------------
# Copied from context_supervisor._config (467-580 at bff30e522) and _enabled
# (583-586), reduced to the one value the library needs while keeping every
# validation branch, because ANY failed field there returns the hard-coded
# defaults (injection_enabled True). A valid file WITHOUT the key resolves to that
# same declared default, True, as the legacy reader now does (ISSUE-REF).
# The reboot_resume_enabled check arrived with ISSUE-REF's reboot resume on main
# (merged here at e8a6fb825); its two siblings were retired by ISSUE-REF, and a
# live file still carrying them is ignored there, so it is ignored here too.
# test_preflight's legacy-parity test compares this reader with the live _enabled()
# so the next added field fails a test, not a soak.
# The keys of context_supervisor._DEFAULT_BAND_THRESHOLDS (174-182), in order.
_LEGACY_BANDS = ("gentle", "recommend", "urge", "urgent", "force")


def _legacy_valid(config: dict[str, Any]) -> bool:
    def integer(name: str, default: int, low: int, high: int) -> bool:
        return _integer(config.get(name, default), low, high)

    keys = config.get("enrolled_keys", [])
    thresholds = config.get("band_thresholds")
    if thresholds is None:
        thresholds_valid = True
    elif isinstance(thresholds, dict) and set(thresholds) == set(_LEGACY_BANDS):
        values = [thresholds.get(name) for name in _LEGACY_BANDS]
        thresholds_valid = all(
            _integer(v, 0, 100) for v in values
        ) and values == sorted(values)
    else:
        thresholds_valid = False
    return (
        isinstance(config.get("injection_enabled", True), bool)
        and isinstance(config.get("post_compact_typed_wake_enabled", False), bool)
        and isinstance(config.get("post_compact_notification_wake_enabled", True), bool)
        and isinstance(config.get("enroll_all_sessions", True), bool)
        and isinstance(keys, list)
        and all(isinstance(key, str) for key in keys)
        and thresholds_valid
        and integer("history_recency_seconds", 300, 0, 3600)
        and isinstance(config.get("debug_diagnostics_enabled", False), bool)
        and integer("turn_quiet_seconds", 15, 5, 3600)
        and integer("no_composer_escalate_seconds", 120, 0, 3600)
        and integer("no_composer_stall_seconds", 900, 60, 86400)
        and integer("no_composer_grow_after_seconds", 60, 0, 3600)
        and integer("no_composer_grow_rows", 50, 20, 200)
        and integer("no_composer_grow_max_attempts", 3, 1, 20)
        and _number(config.get("fifo_enter_delay_seconds", 0.5), 0, 5)
        and _number(config.get("fifo_input_tolerance_seconds", 1.0), 0, 10)
        and _number(config.get("landing_timeout_seconds", 5.0), 0, 30)
        and integer("restage_backoff_ceiling_seconds", 300, 60, 3600)
        and integer("long_wait_escalate_seconds", 7200, 1800, 86400)
        and integer("presence_ttl_seconds", 900, 60, 3600)
        and isinstance(config.get("reboot_resume_enabled", True), bool)
    )


def legacy_injection_enabled() -> bool:
    """The auto-compact system's own ``injection_enabled``, legacy semantics exactly.

    ``AIH_INJECT_ENABLED`` wins when set; a missing, unreadable, malformed or
    invalid file means enabled, and so does a valid file without the key.
    """
    if "AIH_INJECT_ENABLED" in os.environ:
        return os.environ["AIH_INJECT_ENABLED"].lower() in {"1", "true", "yes"}
    try:
        config = json.loads(legacy_config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    if not isinstance(config, dict) or not _legacy_valid(config):
        return True
    return config.get("injection_enabled", True)
