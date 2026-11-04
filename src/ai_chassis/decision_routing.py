"""`config/decision-routing.toml`, loaded and validated at call time (ISSUE-REF D-2, D-6).

The file names who may decide (`[decider]`), what an asker delegates when no decider session is
live (`[fallback.<billing_mode>]`), the System-1 classifier providers (`[providers.<id>]`) and the
per-use-case provider chains (`[use_cases.<name>]`). This module reads and checks it; it calls no
provider. Any deviation from the schema raises `DecisionRoutingError` naming the key, so a typo can
never silently drop a provider or widen the decider list.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "decision-routing.toml"
#: Names another file in place of `CONFIG_PATH` (a test's edited copy).
CONFIG_ENV = "AIH_DECISION_ROUTING_CONFIG"
SCHEMA_VERSION = 1

VENDORS = ("claude", "codex")
#: D-5: the family a machine's spikes, investigations and `/research-doc` delegate to, from its
#: `billing_mode` when its `[machine.<name>.delegation] investigate` does not override it.
#: Subscription profiles spend the maintainer's Codex quota; API-billed ones pay per token either way,
#: so they use the stronger model directly.
INVESTIGATE_BY_BILLING = {"subscription": "codex:sol", "api": "claude:opus"}
PROVIDER_MODES = ("advise", "act")
BUDGET_SOURCES = ("ledger", "balance")
ABSTAIN_ACTIONS = ("session-decider", "asker")

_FAMILY_TOKEN = re.compile(
    rf"^(?P<vendor>{'|'.join(VENDORS)}):(?P<family>[a-z][a-z0-9]*)$"
)
_CREDENTIAL_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9.-]*$")
#: `claude-opus-5-5`, `us.anthropic.claude-opus-5-5-v1:0`, `claude-opus-5-5[1m]`.
#: A two-digit cap on the minor keeps a dated id's `-20260101` suffix out of the version.
_CLAUDE_ID = re.compile(
    r"^(?:[a-z]{2}\.)?(?:anthropic\.)?claude-(?P<family>[a-z]+)-"
    r"(?P<major>\d+)(?:-(?P<minor>\d{1,2})(?!\d))?"
)
#: `gpt-6.1-sol`, `gpt-6-astra`, `gpt-5.6-sol`.
_CODEX_ID = re.compile(r"^gpt-(?P<version>\d+(?:\.\d+)*)-(?P<family>[a-z]+)$")
_VERSION = re.compile(r"^\d+(?:\.\d+)*$")


class DecisionRoutingError(ValueError):
    """The routing config cannot be used as written; the message names the key."""


@dataclass(frozen=True)
class Family:
    vendor: str
    family: str

    def __str__(self) -> str:
        return f"{self.vendor}:{self.family}"


@dataclass(frozen=True)
class Fallback:
    family: Family
    command: str


@dataclass(frozen=True)
class Provider:
    id: str
    enabled: bool
    mode: str
    model: str
    credential: str
    budget_usd: float
    budget_source: str
    endpoint: str | None


@dataclass(frozen=True)
class UseCase:
    name: str
    chain: tuple[str, ...]
    confidence_bar: float
    on_abstain: str


@dataclass(frozen=True)
class DecisionRouting:
    path: Path
    families: tuple[Family, ...]
    minimum_versions: dict[Family, tuple[int, ...]]
    chief_slug_pattern: str
    live_statuses: tuple[str, ...]
    fallback: dict[str, Fallback]
    providers: dict[str, Provider]
    use_cases: dict[str, UseCase]

    def chief_slug_re(self, alias: str) -> re.Pattern[str]:
        return re.compile(self.chief_slug_pattern.replace("{alias}", re.escape(alias)))

    def decider_refusal(self, model_id: str) -> str | None:
        """Why ``model_id`` may not decide by itself, or None when it may (ISSUE-REF)."""
        parsed = _parse_model(model_id)
        if parsed is None or parsed[0] not in self.families:
            return (
                f"{model_id} is not in the decider tier "
                f"({', '.join(str(f) for f in self.families)}; {self.path})"
            )
        family, version = parsed
        minimum = self.minimum_versions[family]
        if _padded(version) < _padded(minimum):
            return (
                f"{model_id} is {family} {_dotted(version)}, below the decider floor "
                f"{_dotted(minimum)} ({self.path} [decider].minimum_versions)"
            )
        return None

    def is_decider_model(self, model_id: str) -> bool:
        return self.decider_refusal(model_id) is None

    def chain(self, use_case: str) -> UseCase:
        return self.use_cases.get(use_case, self.use_cases["default"])


def parse_family(token: object, key: str) -> Family:
    match = _FAMILY_TOKEN.match(token) if isinstance(token, str) else None
    if match is None:
        raise DecisionRoutingError(
            f"{key}: {token!r} is not <vendor>:<family> with vendor one of {', '.join(VENDORS)}"
        )
    return Family(match["vendor"], match["family"])


def investigate_family(machine: str, table: Mapping[str, Any]) -> Family:
    """D-5: ``machine``'s investigation family: its explicit override, else its billing default."""
    delegation = table.get("delegation")
    if delegation is not None:
        if not isinstance(delegation, dict) or set(delegation) - {"investigate"}:
            raise DecisionRoutingError(
                f"[machine.{machine}.delegation] may hold only `investigate`"
            )
        if "investigate" in delegation:
            return parse_family(
                delegation["investigate"], f"machine.{machine}.delegation.investigate"
            )
    billing = table.get("billing_mode")
    if billing not in INVESTIGATE_BY_BILLING:
        raise DecisionRoutingError(
            f"[machine.{machine}] billing_mode {billing!r} names no investigation family"
        )
    return parse_family(
        INVESTIGATE_BY_BILLING[billing], f"machine.{machine}.billing_mode"
    )


def investigate_line(family: Family) -> str:
    """The one overlay line D-5 renders from a machine's investigation family."""
    if family.vendor == "codex":
        return (
            "Investigation (spikes, `/research-doc`): Codex "
            f"`{family.family}`, `luna` fan-out allowed (ISSUE-REF)."
        )
    # Sonnet is allowed only for a large gather fan-out an Opus agent integrates (ISSUE-REF
    # D-7). The line must stay no longer than the 89-character one it replaced, so the
    # tightest overlay's size gate does not move.
    return (
        "Investigation (spikes, `/research-doc`): Claude "
        f"`{family.family}`; Sonnet: big fan-outs (ISSUE-REF)."
    )


def _padded(version: tuple[int, ...]) -> tuple[int, ...]:
    return version + (0,) * (3 - len(version))


def _dotted(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _parse_model(model_id: str) -> tuple[Family, tuple[int, ...]] | None:
    """The family and version of a concrete model id (`gpt-6.1-sol` -> codex:sol, (6, 1))."""
    model_id = model_id.strip().split("[", 1)[0]
    claude = _CLAUDE_ID.match(model_id)
    if claude:
        minor = claude["minor"]
        version = (int(claude["major"]),) + ((int(minor),) if minor else ())
        return Family("claude", claude["family"]), version
    codex = _CODEX_ID.match(model_id)
    if codex:
        version = tuple(int(part) for part in codex["version"].split("."))
        return Family("codex", codex["family"]), version
    return None


def model_family(model_id: str) -> Family | None:
    """The family of a concrete model id (`claude-opus-5-5` -> claude:opus), or None."""
    parsed = _parse_model(model_id)
    return parsed[0] if parsed else None


def _table(
    data: Mapping[str, Any], key: str, allowed: set[str], required: set[str]
) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise DecisionRoutingError(f"[{key}] is missing or not a table")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise DecisionRoutingError(f"[{key}] has unknown key(s): {', '.join(unknown)}")
    missing = sorted(required - set(value))
    if missing:
        raise DecisionRoutingError(f"[{key}] omits: {', '.join(missing)}")
    return value


def _typed(value: object, kind: type | tuple[type, ...], key: str) -> Any:
    if not isinstance(value, kind) or (kind is not bool and isinstance(value, bool)):
        raise DecisionRoutingError(f"{key}: {value!r} has the wrong type")
    return value


def _choice(value: object, choices: tuple[str, ...], key: str) -> str:
    if value not in choices:
        raise DecisionRoutingError(
            f"{key}: {value!r} is not one of {', '.join(choices)}"
        )
    return str(value)


def _strings(value: object, key: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(v, str) and v for v in value)
    ):
        raise DecisionRoutingError(f"{key}: must be a non-empty array of strings")
    return tuple(value)


def _minimum_versions(
    value: object, families: tuple[Family, ...]
) -> dict[Family, tuple[int, ...]]:
    key = "decider.minimum_versions"
    if not isinstance(value, dict):
        raise DecisionRoutingError(f"{key}: must be a table")
    minimums = {}
    for token, version in value.items():
        family = parse_family(token, key)
        if not isinstance(version, str) or not _VERSION.match(version):
            raise DecisionRoutingError(
                f"{key}.{token}: {version!r} is not a dotted version"
            )
        minimums[family] = tuple(int(part) for part in version.split("."))
    if set(minimums) != set(families):
        raise DecisionRoutingError(
            f"{key}: must name exactly the decider families "
            f"({', '.join(str(f) for f in families)})"
        )
    return minimums


def _decider(
    data: Mapping[str, Any],
) -> tuple[tuple[Family, ...], dict[Family, tuple[int, ...]], str, tuple[str, ...]]:
    keys = {"families", "minimum_versions", "chief_slug_pattern", "live_statuses"}
    table = _table(data, "decider", keys, keys)
    families = tuple(
        parse_family(token, "decider.families")
        for token in _strings(table["families"], "decider.families")
    )
    minimums = _minimum_versions(table["minimum_versions"], families)
    pattern = _typed(table["chief_slug_pattern"], str, "decider.chief_slug_pattern")
    if "{alias}" not in pattern:
        raise DecisionRoutingError("decider.chief_slug_pattern: must contain {alias}")
    try:
        re.compile(pattern.replace("{alias}", "x"))
    except re.error as exc:
        raise DecisionRoutingError(f"decider.chief_slug_pattern: {exc}") from exc
    return (
        families,
        minimums,
        pattern,
        _strings(table["live_statuses"], "decider.live_statuses"),
    )


def _fallbacks(data: Mapping[str, Any]) -> dict[str, Fallback]:
    tables = _table(data, "fallback", set(VENDORS), set(VENDORS))
    out = {}
    for mode in VENDORS:
        table = _table(tables, mode, {"family", "command"}, {"family", "command"})
        command = _typed(table["command"], str, f"fallback.{mode}.command")
        if "{held}" not in command:
            raise DecisionRoutingError(
                f"fallback.{mode}.command: must contain {{held}}"
            )
        out[mode] = Fallback(
            parse_family(table["family"], f"fallback.{mode}.family"), command
        )
    return out


_PROVIDER_KEYS = {
    "enabled",
    "mode",
    "model",
    "credential",
    "budget_usd",
    "budget_source",
}


def _provider(provider_id: str, table: Mapping[str, Any]) -> Provider:
    key = f"providers.{provider_id}"
    if not _PROVIDER_ID.match(provider_id):
        raise DecisionRoutingError(f"{key}: not a lowercase provider id")
    table = _table({key: table}, key, _PROVIDER_KEYS | {"endpoint"}, _PROVIDER_KEYS)
    credential = _typed(table["credential"], str, f"{key}.credential")
    if not _CREDENTIAL_NAME.match(credential):
        raise DecisionRoutingError(
            f"{key}.credential: must be a secret's NAME (UPPER_SNAKE), never its value"
        )
    budget = _typed(table["budget_usd"], (int, float), f"{key}.budget_usd")
    if budget < 0:
        raise DecisionRoutingError(f"{key}.budget_usd: must be 0 or more")
    endpoint = table.get("endpoint")
    if endpoint is not None and not (
        isinstance(endpoint, str) and endpoint.startswith("/")
    ):
        raise DecisionRoutingError(f"{key}.endpoint: must be a path starting with /")
    return Provider(
        id=provider_id,
        enabled=_typed(table["enabled"], bool, f"{key}.enabled"),
        mode=_choice(table["mode"], PROVIDER_MODES, f"{key}.mode"),
        model=_typed(table["model"], str, f"{key}.model"),
        credential=credential,
        budget_usd=float(budget),
        budget_source=_choice(
            table["budget_source"], BUDGET_SOURCES, f"{key}.budget_source"
        ),
        endpoint=endpoint,
    )


def _use_case(
    name: str, table: Mapping[str, Any], providers: Mapping[str, Provider]
) -> UseCase:
    key = f"use_cases.{name}"
    allowed = {"chain", "confidence_bar", "on_abstain"}
    table = _table({key: table}, key, allowed, allowed)
    chain = table["chain"]
    if not isinstance(chain, list) or not all(isinstance(p, str) for p in chain):
        raise DecisionRoutingError(f"{key}.chain: must be an array of provider ids")
    undeclared = [p for p in chain if p not in providers]
    if undeclared:
        raise DecisionRoutingError(
            f"{key}.chain: undeclared provider(s) {', '.join(undeclared)}"
        )
    bar = _typed(table["confidence_bar"], (int, float), f"{key}.confidence_bar")
    if not 0 <= bar <= 1:
        raise DecisionRoutingError(f"{key}.confidence_bar: must be between 0 and 1")
    return UseCase(
        name=name,
        chain=tuple(chain),
        confidence_bar=float(bar),
        on_abstain=_choice(table["on_abstain"], ABSTAIN_ACTIONS, f"{key}.on_abstain"),
    )


def load(path: Path | None = None) -> DecisionRouting:
    """The routing config, read now from ``path``, `$AIH_DECISION_ROUTING_CONFIG` or the default."""
    path = path or Path(os.environ.get(CONFIG_ENV) or CONFIG_PATH)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise DecisionRoutingError(f"{path}: {exc}") from exc
    unknown = sorted(
        set(data) - {"schema_version", "decider", "fallback", "providers", "use_cases"}
    )
    if unknown:
        raise DecisionRoutingError(
            f"{path}: unknown top-level key(s): {', '.join(unknown)}"
        )
    if data.get("schema_version") != SCHEMA_VERSION:
        raise DecisionRoutingError(
            f"schema_version: must be {SCHEMA_VERSION}, got {data.get('schema_version')!r}"
        )
    families, minimums, pattern, live = _decider(data)
    providers_table = data.get("providers", {})
    if not isinstance(providers_table, dict):
        raise DecisionRoutingError("[providers] is not a table")
    providers = {pid: _provider(pid, table) for pid, table in providers_table.items()}
    use_cases_table = data.get("use_cases")
    if not isinstance(use_cases_table, dict) or "default" not in use_cases_table:
        raise DecisionRoutingError("[use_cases.default] is missing")
    use_cases = {
        name: _use_case(name, table, providers)
        for name, table in use_cases_table.items()
    }
    return DecisionRouting(
        path=path,
        families=families,
        minimum_versions=minimums,
        chief_slug_pattern=pattern,
        live_statuses=live,
        fallback=_fallbacks(data),
        providers=providers,
        use_cases=use_cases,
    )
