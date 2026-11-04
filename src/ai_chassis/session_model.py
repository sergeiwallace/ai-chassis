"""Set the driving-session default model, the Bedrock family pins and the driver's effort.

Option B of docs/research/claude-code-session-model-selection-and-persistence.md section 9
(ISSUE-REF). The policy is ``config/session-model.toml``, read on every run:

*   ``model`` gets a family ALIAS (``opus[1m]``), never a version. Claude Code reads it at session
    start only, so a running session keeps its model, and no re-login path writes it.
*   On a host whose route-table entry lists ``claude-bedrock``, each family in ``pin_families``
    is resolved by the model catalog, proved invocable by a one-token probe under the identity
    Claude Code signs with, and written as ``env.ANTHROPIC_DEFAULT_<FAMILY>_MODEL``. A family
    that fails either step keeps its live value; a guessed id is never written.
*   The effort goes in ``modelSettings.<canonical opus id>.effortLevel``, derived from the
    resolved pin, because a top-level ``effortLevel`` is ignored by Opus 5.5 and later.
*   ``reset_on_install`` governs a three-way rule over desired, live and last-written values.
*   ``retire_env_keys`` are deleted from the live ``env``: ``ANTHROPIC_MODEL`` outranks ``model``.

Stdlib only: install.sh runs this under a bare interpreter with no project environment.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

BEDROCK_ROUTE = "claude-bedrock"
FAMILIES = ("fable", "opus", "sonnet", "haiku")
CONTEXTS = ("1m", "")
EFFORTS = ("low", "medium", "high", "xhigh")
ONE_M_SUFFIX = "[1m]"
PROBE_TTL = dt.timedelta(hours=24)
RESOLVER_TIMEOUT_S = 120
PROBE_TIMEOUT_S = 60

_KEY_TYPES: dict[str, type] = {
    "enabled": bool,
    "family": str,
    "context": str,
    "effort": str,
    "reset_on_install": bool,
    "pin_families": list,
    "one_m_families": list,
    "retire_env_keys": list,
}
_DEFAULTS = {"enabled": True}
_SCALAR_CHOICES = {"family": FAMILIES, "context": CONTEXTS, "effort": EFFORTS}
_ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_RESOLVED_LINE = re.compile(r"^family=(?P<family>\S+) resolved=(?P<id>\S+) requested=")
# The provider prefix, the `[1m]` suffix, and a `-vN[:M]` revision and/or `-YYYYMMDD` date.
_CANONICAL = re.compile(
    r"^(?:[a-z]{2,4}\.)?(?:anthropic\.)?(?P<base>claude-[a-z]+-[0-9]+(?:-[0-9]{1,2})*?)"
    r"(?:-[0-9]{8})?(?:-v[0-9]+(?::[0-9]+)?)?(?:\[1m\])?$"
)


class SessionModelError(ValueError):
    """The session-model config or the settings file cannot be used; nothing was written."""


@dataclass(frozen=True)
class Outcome:
    """What one family's resolve-and-probe produced: an id, or the reason there is none."""

    model_id: str | None
    reason: str = ""


Resolver = Callable[[str], Outcome]
Prober = Callable[[str, str, str | None], Outcome]


# --- config --------------------------------------------------------------------------------


def _read_toml(path: Path, label: str) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SessionModelError(f"cannot read {label} {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SessionModelError(f"{label} {path} is not valid TOML: {exc}") from exc


def _check_types(table: dict, where: str) -> None:
    for key, value in table.items():
        expected = _KEY_TYPES.get(key)
        if expected is None:
            known = ", ".join(sorted(_KEY_TYPES))
            raise SessionModelError(
                f"{where}.{key} is not a known key (known: {known})"
            )
        if not isinstance(value, expected):
            raise SessionModelError(
                f"{where}.{key} must be a {expected.__name__}, got {type(value).__name__}"
            )
        if expected is list and not all(isinstance(item, str) for item in value):
            raise SessionModelError(f"{where}.{key} must be a list of strings")


def _check_values(table: dict, where: str) -> None:
    for key, allowed in _SCALAR_CHOICES.items():
        if key in table and table[key] not in allowed:
            raise SessionModelError(
                f"{where}.{key}={table[key]!r} is not one of {allowed}"
            )
    for key in ("pin_families", "one_m_families"):
        unknown = sorted(set(table.get(key, [])) - set(FAMILIES))
        if unknown:
            raise SessionModelError(f"{where}.{key} names unknown families: {unknown}")
    bad = [key for key in table.get("retire_env_keys", []) if not _ENV_KEY.match(key)]
    if bad:
        raise SessionModelError(f"{where}.retire_env_keys has invalid names: {bad}")


def _validate_table(table: object, where: str) -> dict:
    if not isinstance(table, dict):
        raise SessionModelError(f"{where} must be a table")
    _check_types(table, where)
    _check_values(table, where)
    return table


def load_config(path: Path, *, known_hosts: set[str]) -> dict:
    """Parse and validate the policy file, refusing by naming the offending key."""
    document = _read_toml(path, "session-model config")
    unknown = sorted(set(document) - {"defaults", "hosts"})
    if unknown:
        raise SessionModelError(f"{path}: unknown top-level key(s): {unknown}")
    defaults = _validate_table(document.get("defaults", {}), "defaults")
    missing = sorted(set(_KEY_TYPES) - set(_DEFAULTS) - set(defaults))
    if missing:
        raise SessionModelError(f"{path}: [defaults] is missing {missing}")
    hosts = document.get("hosts", {})
    if not isinstance(hosts, dict):
        raise SessionModelError(f"{path}: hosts must be a table of [hosts.<AI_HOST>]")
    for name, table in hosts.items():
        if name not in known_hosts:
            raise SessionModelError(
                f"{path}: hosts.{name} names no host in the model-catalog route table or "
                "the [machine.<name>] registry"
            )
        _validate_table(table, f"hosts.{name}")
    return {"defaults": defaults, "hosts": hosts}


def host_policy(config: dict, machine: str) -> dict:
    """The defaults with this host's overrides applied."""
    return {**_DEFAULTS, **config["defaults"], **config["hosts"].get(machine, {})}


def read_route_table(path: Path) -> dict:
    """The model-catalog route table, read-only; its own loader owns validation."""
    return _read_toml(path, "model-catalog route table")


def host_routes(route_table: dict, machine: str) -> list[str]:
    """The routes the table gives ``machine``, falling back to ``[defaults].routes``."""
    entry = (route_table.get("hosts") or {}).get(machine)
    source = entry if isinstance(entry, dict) else route_table.get("defaults") or {}
    return list(source.get("routes") or [])


def known_hosts(route_table: dict, registry_path: Path | None) -> set[str]:
    """Host names a ``[hosts.*]`` table may use: the route table's plus the machine registry's.

    The registry's machine set is the ``[machine.<name>]`` table keys (ISSUE-REF Phase 1;
    ``ai_chassis.machine_registry.machine_names`` is the shared rule). Spelled inline here
    because this module runs under a bare interpreter with no project environment.
    """
    names = set((route_table.get("hosts") or {}).keys())
    if registry_path is not None and registry_path.is_file():
        tables = _read_toml(registry_path, "machine registry").get("machine") or {}
        if isinstance(tables, dict):
            names.update(str(name) for name in tables)
    return names


# --- ids -----------------------------------------------------------------------------------


def canonical_model_id(model_id: str) -> str | None:
    """The key Claude Code files ``modelSettings`` under: ``us.anthropic.claude-opus-5-5[1m]`` ->
    ``claude-opus-5-5``. None when the id has no recognisable Claude shape."""
    match = _CANONICAL.match(model_id)
    return match.group("base") if match else None


def pin_key(family: str) -> str:
    return f"ANTHROPIC_DEFAULT_{family.upper()}_MODEL"


def _strip_one_m(model_id: str) -> str:
    return model_id.removesuffix(ONE_M_SUFFIX)


# --- resolver and probe --------------------------------------------------------------------


def command_resolver(
    command: list[str], *, route_table: Path, machine: str
) -> Resolver:
    """Resolve through the model-catalog CLI. After one failure the rest skip the refresh, so an
    unreachable network costs one bounded refresh, not one per family."""
    refresh = {"allowed": True}

    def resolve(family: str) -> Outcome:
        argv = [
            *command,
            "--route-table",
            str(route_table),
            "resolve",
            "--route",
            BEDROCK_ROUTE,
            "--family",
            family,
            "--host",
            machine,
        ]
        if not refresh["allowed"]:
            argv.append("--no-refresh")
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=RESOLVER_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            refresh["allowed"] = False
            return Outcome(None, f"resolver did not run: {exc}")
        if proc.returncode != 0:
            refresh["allowed"] = False
            detail = (proc.stderr.strip().splitlines() or ["no stderr"])[-1]
            return Outcome(None, f"resolver exited {proc.returncode}: {detail}")
        for line in proc.stdout.splitlines():
            match = _RESOLVED_LINE.match(line.strip())
            if match and match.group("family") == family:
                return Outcome(match.group("id"))
        return Outcome(None, "resolver printed no family=... resolved=... line")

    return resolve


def command_prober(command: list[str] | None) -> Prober:
    """One-token invoke of ``model_id``. ``command`` replaces the default ``aws`` call and gets
    ``--model-id <id> --region <r> [--profile <p>]``; exit 0 means invocable."""

    def probe(model_id: str, region: str, profile: str | None) -> Outcome:
        if command is None:
            argv = [
                "aws",
                "bedrock-runtime",
                "converse",
                "--model-id",
                model_id,
                "--region",
                region,
                "--messages",
                '[{"role":"user","content":[{"text":"ok"}]}]',
                "--inference-config",
                '{"maxTokens":1}',
                "--output",
                "json",
            ]
            if profile:
                argv += ["--profile", profile]
        else:
            argv = [*command, "--model-id", model_id, "--region", region]
            if profile:
                argv += ["--profile", profile]
        env = dict(os.environ)
        if not profile:
            # The ambient chain Claude Code signs with when settings name no profile. An
            # inherited shell AWS_PROFILE (example-project-ec2's ~/.bashrc exports an SSO one for credo)
            # is not that identity, and a probe under it can pass where the model path fails.
            env.pop("AWS_PROFILE", None)
            env.pop("AWS_DEFAULT_PROFILE", None)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_S,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Outcome(None, f"probe did not run: {exc}")
        if proc.returncode != 0:
            detail = (proc.stderr.strip().splitlines() or ["no stderr"])[-1]
            return Outcome(
                None, f"probe of {model_id} exited {proc.returncode}: {detail}"
            )
        return Outcome(model_id)

    return probe


# --- state ---------------------------------------------------------------------------------


def default_state_dir(environ: dict[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    base = values.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ai-harness" / "session-model"


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f"{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _cached_probe(cache: dict, cache_key: str, now: dt.datetime) -> bool:
    entry = cache.get(cache_key)
    if not isinstance(entry, dict) or entry.get("ok") is not True:
        return False
    try:
        at = dt.datetime.fromisoformat(str(entry.get("at")))
    except ValueError:
        return False
    return now - at < PROBE_TTL


# --- the writer ----------------------------------------------------------------------------


def _owned_write(live: object, desired: str, last: object, reset: bool) -> str | None:
    """Why to write ``desired`` over ``live``, or None to leave it (the three-way rule)."""
    if live == desired:
        return None
    if reset:
        return "reset_on_install"
    if live is None:
        return "absent"
    if live == last:
        return "install owns it"
    return None


@dataclass(frozen=True)
class Request:
    """Which settings file, for which host, under which policy files."""

    settings_path: Path
    machine: str
    config_path: Path
    route_table_path: Path
    registry_path: Path | None
    state_dir: Path
    dry_run: bool = False


@dataclass(frozen=True)
class Seams:
    """The network-facing and output edges, injectable so tests never reach the network."""

    resolver: Resolver
    prober: Prober
    out: TextIO
    now: dt.datetime | None = None


@dataclass
class _Run:
    """One run's live settings, the last-written record, and the record this run leaves."""

    request: Request
    seams: Seams
    policy: dict
    settings: dict
    env: dict
    last: dict
    written: dict = field(default_factory=dict)

    def say(self, line: str) -> None:
        print(f"  {line}", file=self.seams.out)

    def three_way(self, label: str, live: object, desired: str, last: object) -> bool:
        """Apply the reset rule, report it, and say whether ``desired`` should be written."""
        why = _owned_write(live, desired, last, self.policy["reset_on_install"])
        if why:
            self.say(f"[ok]  {label}: {live!r} -> {desired!r} ({why})")
        elif live == desired:
            self.say(f"[ok]  {label}: {desired!r} already set")
        else:
            self.say(
                f"[ok]  {label} left as the user's choice: {live!r} (reset_on_install = false)"
            )
        return why is not None


def _read_settings(path: Path) -> tuple[str | None, dict, dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = None
    except OSError as exc:
        raise SessionModelError(f"cannot read {path}: {exc}") from exc
    try:
        settings = {} if text is None else json.loads(text)
    except json.JSONDecodeError as exc:
        raise SessionModelError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(settings, dict):
        raise SessionModelError(f"{path} must hold a JSON object")
    env = settings.get("env", {})
    if not isinstance(env, dict):
        raise SessionModelError(f"{path}: env must be a JSON object")
    return text, settings, env


def _set_model(run: _Run) -> None:
    """The session default: an alias, never a version."""
    suffix = ONE_M_SUFFIX if run.policy["context"] == "1m" else ""
    desired = run.policy["family"] + suffix
    if run.three_way(
        "model", run.settings.get("model"), desired, run.last.get("model")
    ):
        run.settings["model"] = desired
        run.written["model"] = desired


def _pin_for(run: _Run, family: str, params: dict, cache: dict) -> Outcome:
    """Resolve, validate the shape, then probe (or reuse a passing probe) for one family."""
    outcome = run.seams.resolver(family)
    model_id = outcome.model_id
    if model_id is None:
        return outcome
    prefix = str(params.get("inference_profile_prefix") or "")
    if not (
        model_id.startswith(prefix)
        and f"anthropic.claude-{family}-" in model_id
        and canonical_model_id(model_id) is not None
    ):
        return Outcome(None, f"resolver returned an unusable id {model_id!r}")
    bare = _strip_one_m(model_id)
    profile = run.env.get("AWS_PROFILE") or None
    cache_key = f"{bare}|{f'profile:{profile}' if profile else 'ambient'}"
    moment = run.seams.now or dt.datetime.now(dt.UTC)
    if _cached_probe(cache, cache_key, moment):
        return Outcome(bare)
    region = str(run.env.get("AWS_REGION") or params.get("region") or "us-east-1")
    outcome = run.seams.prober(bare, region, profile)
    if outcome.model_id is not None:
        cache[cache_key] = {"ok": True, "at": moment.isoformat()}
    return outcome


def _set_pins(run: _Run, route_table: dict) -> dict[str, str]:
    """Bedrock family pins, resolved and probed; a failure keeps the live value."""
    machine = run.request.machine
    if BEDROCK_ROUTE not in host_routes(route_table, machine):
        if str(run.env.get("CLAUDE_CODE_USE_BEDROCK", "")).lower() in ("1", "true"):
            run.say(
                f"[warn] pins: settings select Bedrock but {run.request.route_table_path.name} "
                f"gives {machine} no {BEDROCK_ROUTE} route, so no family is pinned"
            )
        else:
            run.say(f"[ok]  pins: none ({machine} has no {BEDROCK_ROUTE} route)")
        return {}
    params = (route_table.get("routes") or {}).get(BEDROCK_ROUTE) or {}
    cache_path = run.request.state_dir / "probe-cache.json"
    cache = _read_json(cache_path)
    cached_before = json.dumps(cache, sort_keys=True)
    last_env = run.last.get("env") if isinstance(run.last.get("env"), dict) else {}
    resolved: dict[str, str] = {}
    for family in run.policy["pin_families"]:
        outcome = _pin_for(run, family, params, cache)
        if outcome.model_id is None:
            run.say(f"[warn] {family}: {outcome.reason}; pin left unchanged")
            continue
        suffix = ONE_M_SUFFIX if family in run.policy["one_m_families"] else ""
        desired = _strip_one_m(outcome.model_id) + suffix
        resolved[family] = desired
        key = pin_key(family)
        if run.three_way(f"env.{key}", run.env.get(key), desired, last_env.get(key)):
            run.env[key] = desired
            run.written.setdefault("env", {})[key] = desired
    if json.dumps(cache, sort_keys=True) != cached_before and not run.request.dry_run:
        _atomic_write(cache_path, json.dumps(cache, indent=2, sort_keys=True) + "\n")
    return resolved


def _drop_stale_effort(run: _Run, model_settings: dict, canonical: str) -> None:
    """Remove the entry this writer made for an opus id the pin has moved off."""
    last = run.last.get("effort")
    if not isinstance(last, dict) or last.get("canonical") in (None, canonical):
        return
    stale = model_settings.get(last["canonical"])
    if isinstance(stale, dict) and stale.get("effortLevel") == last.get("value"):
        del stale["effortLevel"]
        if not stale:
            del model_settings[last["canonical"]]
        run.say(
            f"[ok]  modelSettings.{last['canonical']}.effortLevel removed (the opus pin moved)"
        )


def _set_effort(run: _Run, resolved: dict[str, str]) -> None:
    """Effort, keyed by the canonical id of the opus pin in effect."""
    if "opus" not in resolved:
        run.say(
            "[ok]  effort: not written (no resolved opus pin to key modelSettings by)"
        )
        return
    model_settings = run.settings.get("modelSettings", {})
    canonical = canonical_model_id(str(run.env.get(pin_key("opus"))))
    if not isinstance(model_settings, dict) or canonical is None:
        run.say(
            "[warn] effort: modelSettings or the opus pin in effect is unusable; not written"
        )
        return
    _drop_stale_effort(run, model_settings, canonical)
    entry = model_settings.get(canonical)
    entry = entry if isinstance(entry, dict) else {}
    last = run.last.get("effort")
    last_value = (
        last.get("value")
        if isinstance(last, dict) and last.get("canonical") == canonical
        else None
    )
    desired = run.policy["effort"]
    label = f"modelSettings.{canonical}.effortLevel"
    if run.three_way(label, entry.get("effortLevel"), desired, last_value):
        entry["effortLevel"] = desired
        model_settings[canonical] = entry
        run.written["effort"] = {"canonical": canonical, "value": desired}
    if model_settings:
        run.settings["modelSettings"] = model_settings
    else:
        run.settings.pop("modelSettings", None)


def _retire(run: _Run) -> None:
    """Keys that outrank `model`. A running process keeps its copy until it restarts."""
    for key in run.policy["retire_env_keys"]:
        if key in run.env:
            removed = run.env.pop(key)
            run.say(f"[ok]  env.{key} removed (was {removed!r}; it outranks `model`)")


def apply(request: Request, seams: Seams) -> int:
    """Apply the session-model policy to ``request.settings_path``. Raises SessionModelError
    before any write when the config or the settings file is unusable."""
    route_table = read_route_table(request.route_table_path)
    config = load_config(
        request.config_path, known_hosts=known_hosts(route_table, request.registry_path)
    )
    policy = host_policy(config, request.machine)
    if not policy["enabled"]:
        print(
            f"  [skip] session model: disabled for {request.machine} in {request.config_path}",
            file=seams.out,
        )
        return 0
    text, settings, env = _read_settings(request.settings_path)
    state_path = request.state_dir / "last-written.json"
    state = _read_json(state_path)
    record_key = str(request.settings_path.resolve())
    last = state.get(record_key)
    run = _Run(
        request, seams, policy, settings, env, last if isinstance(last, dict) else {}
    )

    _set_model(run)
    resolved = _set_pins(run, route_table)
    _set_effort(run, resolved)
    _retire(run)
    if env or "env" in settings:
        settings["env"] = env

    rendered = json.dumps(settings, indent=2) + "\n"
    if rendered == text:
        run.say(f"[ok]  {request.settings_path} (session model unchanged)")
        return 0
    if request.dry_run:
        run.say(f"[ok]  {request.settings_path} (dry run: would write)")
        return 0
    _atomic_write(request.settings_path, rendered)
    record = {**run.last, **run.written}
    record["env"] = {**(run.last.get("env") or {}), **run.written.get("env", {})}
    state[record_key] = record
    _atomic_write(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
    run.say(f"[ok]  {request.settings_path} (session model written)")
    return 0
