"""Intersect operator settings with the packaged catalog and fixed authority."""

from dataclasses import dataclass
from importlib.resources import files
import tomllib

CEILING = {
    "harness_validate": {"fields": []},
    "harness_explain": {"fields": ["key"], "keys": ["site.agents", "site.machines"]},
    "harness_plan": {
        "fields": ["targets"],
        "targets": ["AGENTS.md", "CLAUDE.md", "GEMINI.md"],
    },
    "harness_doctor": {
        "fields": ["checks"],
        "checks": ["config", "generation", "links", "storage"],
    },
}
LIMITS = {"call_timeout_seconds": 10, "max_result_bytes": 65536, "max_items": 100}
MIN_RESULT_BYTES = 256


@dataclass(frozen=True)
class Policy:
    tools: tuple[str, ...]
    call_timeout_seconds: float
    max_result_bytes: int
    max_items: int
    safe_fields: dict[str, dict[str, list[str]]]

    def describe(self) -> dict:
        return {
            "schema_version": 1,
            "tools": list(self.tools),
            "call_timeout_seconds": self.call_timeout_seconds,
            "max_result_bytes": self.max_result_bytes,
            "max_items": self.max_items,
            "redaction_version": 1,
        }


def load_catalog() -> dict:
    """Read package data; never import a configured entry point."""
    return tomllib.loads(
        files("ai_harness").joinpath("defaults/core-mcp.toml").read_text("utf-8")
    )


def _reject() -> None:
    raise ValueError("Invalid core MCP policy")


def _validate_catalog(catalog: dict) -> None:
    if set(catalog) != {
        "schema_version",
        "api_major",
        "redaction_version",
        "limits",
        "tools",
    }:
        _reject()
    if any(
        type(catalog[key]) is not int or catalog[key] != 1
        for key in ("schema_version", "api_major", "redaction_version")
    ):
        _reject()
    if catalog["limits"] != LIMITS or set(catalog["tools"]) != set(CEILING):
        _reject()
    for name, ceiling in CEILING.items():
        rules = catalog["tools"][name]
        if set(rules) != set(ceiling):
            _reject()
        for field, values in rules.items():
            if not isinstance(values, list) or any(
                value not in ceiling[field] for value in values
            ):
                _reject()
            if field == "fields" and values != ceiling[field]:
                _reject()


def _settings(settings: dict) -> None:
    if set(settings) - {"enabled", "clients", "required_clients", "tools", *LIMITS}:
        _reject()
    if type(settings.get("enabled", True)) is not bool:
        _reject()
    for key, permitted in (("clients", {"claude", "codex"}), ("tools", set(CEILING))):
        values = settings.get(key, sorted(permitted))
        if not isinstance(values, list) or any(
            not isinstance(v, str) or v not in permitted for v in values
        ):
            _reject()
        if len(set(values)) != len(values):
            _reject()
    required = settings.get("required_clients", [])
    if (
        not isinstance(required, list)
        or any(
            not isinstance(v, str)
            or v not in settings.get("clients", ["claude", "codex"])
            for v in required
        )
        or len(set(required)) != len(required)
    ):
        _reject()


def load_policy(settings: dict | None = None, *, catalog: dict | None = None) -> Policy:
    """Reject invalid metadata/settings before server registration begins."""
    try:
        catalog = load_catalog() if catalog is None else catalog
        settings = {} if settings is None else settings
        _validate_catalog(catalog)
        _settings(settings)
        bounds = {key: settings.get(key, ceiling) for key, ceiling in LIMITS.items()}
        for key, value in bounds.items():
            if type(value) not in (int, float) or not 0 < value <= LIMITS[key]:
                _reject()
            if key != "call_timeout_seconds" and type(value) is not int:
                _reject()
        if bounds["max_result_bytes"] < MIN_RESULT_BYTES:
            _reject()
        tools = (
            tuple(settings.get("tools", list(CEILING)))
            if settings.get("enabled", True)
            else ()
        )
        return Policy(tools=tools, safe_fields=catalog["tools"], **bounds)
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Invalid core MCP policy") from None
