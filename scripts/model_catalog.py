#!/usr/bin/env python3
# ruff: noqa: E402
"""model-catalog: the per-host record of which model ids each route actually serves.

Design: docs/designs/model-catalog-refresher.md (ISSUE-REF, Phase 2; D-1..D-10).

WHAT THIS IS FOR. Every agent-launch surface needs to turn "the newest Sol" or "the newest
Opus" into a concrete id. Until now each surface carried its own hand-maintained pin, so a
newly deployed version was picked up by nobody until a human edited a file. This module makes
that a fact the machine MEASURES: a refresher asks each route the host can use what it serves
and records the answer with a timestamp; a resolver reads that record and answers "newest id of
family F on route R", or REFUSES and says exactly why.

THE REFUSAL IS THE POINT (AC-2). There is deliberately no fallback to a remembered id. A
resolver that degrades silently to a stale default reintroduces the staleness invisibly and is
strictly WORSE than the hardcoded pin it replaces, because the pin at least admits what it is.
A missing, stale, errored or family-less catalog therefore gets one bounded live refresh of the
requested route (D-6 -- a fresher read, not a static fallback) and then refuses with a non-zero
exit and a line naming the family, the route, the catalog path, its `fetched_at` and the
catalog's own answer.

  exit 0  resolved / refreshed
  exit 2  usage or config refusal (a malformed route table, an unknown route id, a lock timeout)
  exit 3  the family has no deployment on the route (or none the provider reports as available)
  exit 4  a candidate of the family has an unparsable version -- "latest" is undefined
  exit 5  the route entry is staler than stale_after_hours and refresh-on-miss failed
  exit 6  the catalog is missing and refresh-on-miss failed
  exit 7  the route's recorded status is not `ok`
  exit 75 `refresh` only: every failed route failed because this host's own SOCKS proxy refused
          the connection (EX_TEMPFAIL; the tunnel is down, recorded as `proxy-unreachable`)

STDLIB ONLY, PYTHON >= 3.11 (tomllib). The ~/.claude/bin/model-catalog wrapper selects such an
interpreter, because the systemd user manager's python3 is 3.10 on example-project-ec2 and cannot import
tomllib. Nothing here imports ai_chassis: this module must run under a bare system interpreter
that has no repo venv on its path, which is also why the machine-identity resolution below is a
small local mirror of src/ai_chassis/machine.py's contract rather than an import of it.
"""

from __future__ import annotations

# The wrapper picks a >=3.11 interpreter, but this module is also launched AMBIENTLY -- directly
# by a systemd user unit, a hook, or a human who found the file -- and the `import tomllib` below
# does not exist under 3.10. So the floor is enforced HERE as well as in the wrapper: a guard
# that lives only in one of two launch paths is not a guard for the other.
try:
    from py_interpreter_bootstrap import ensure_supported_python
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):
        """Stand in for the bootstrap when this file was copied without it.

        A partial copy cannot re-exec, because the module that knows how is the one
        missing. Staying silent when the running interpreter ALREADY meets the floor is
        correct rather than lax: there was nothing to enforce. Below the floor there is
        no remedy left to apply, so say so and let the real failure happen where it can
        be read, instead of dying here on an import that is not the problem.
        """
        import sys as _sys

        if _sys.version_info < minimum:
            _sys.stderr.write(
                "warning: {} is running under Python {} without its interpreter "
                "bootstrap, so the {}.{}+ floor could not be enforced\n".format(
                    __file__,
                    ".".join(str(p) for p in _sys.version_info[:3]),
                    minimum[0],
                    minimum[1],
                )
            )


ensure_supported_python()

import argparse
import datetime as dt
import fcntl
import json
import os
import platform
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.ai_chassis import aws_profile as aws_profile_resolver  # noqa: E402

SCHEMA_VERSION = 1

#: The value a host row sets instead of a profile NAME, meaning "read it from this machine's
#: own `~/.aws/config` when the AWS call is made" (ISSUE-REF). The same account is labelled
#: differently on each machine and three hardcoded names have gone stale in turn on this
#: fleet, so the route table names a SELECTOR and never a name. A literal is still accepted
#: for a host that genuinely needs a specific one.
DERIVE_AWS_PROFILE = "derive"

#: Every refusal line starts with this, so a consumer can relay it verbatim and a human can grep
#: for it. The em-dash is part of the contract the design doc fixes.
REFUSAL_PREFIX = "model-catalog: REFUSED —"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FAMILY_ABSENT = 3
EXIT_UNPARSABLE_VERSION = 4
EXIT_STALE = 5
EXIT_CATALOG_MISSING = 6
EXIT_ROUTE_NOT_OK = 7
#: `sysexits.h` EX_TEMPFAIL. A refresh whose ONLY failures are the host's SOCKS proxy refusing the
#: connection (ISSUE-REF): the tunnel is down, which is an outage of its own (cx uses the same
#: forward) and is reported as that by scripts/launchagent_health_status.py, not as a broken agent.
EXIT_PROXY_UNREACHABLE = 75

#: The route status recorded for that case. Every other route failure stays `error`.
STATUS_PROXY_UNREACHABLE = "proxy-unreachable"

ROUTE_AZURE_GATEWAY = "azure-gateway"
ROUTE_CODEX_OAUTH = "codex-oauth"
ROUTE_CLAUDE_BEDROCK = "claude-bedrock"
ROUTE_CLAUDE_SUBSCRIPTION = "claude-subscription"

#: The four routes this design knows. A host's `routes` list may name only these; an unknown id
#: is a load-time refusal naming the key rather than a silently skipped route, because a typo
#: that merely drops a route looks exactly like a host that legitimately has none.
KNOWN_ROUTES = (
    ROUTE_AZURE_GATEWAY,
    ROUTE_CODEX_OAUTH,
    ROUTE_CLAUDE_BEDROCK,
    ROUTE_CLAUDE_SUBSCRIPTION,
)

#: Any top-level key outside these is a refusal.
ALLOWED_TOP_LEVEL_KEYS = frozenset({"defaults", "routes", "hosts", "scheduler"})

#: D-2 as amended by review F-4: the mechanism that refreshes the catalog on each platform, keyed
#: by `uname -s`. `[scheduler]` in the route table names it per platform; install.sh asks
#: `host-config --platform "$(uname -s)"` which one applies and verifies THAT one by state.
SCHEDULER_SYSTEMD = "systemd-user-timer"
SCHEDULER_LAUNCHD = "launchd-agent"
SCHEDULER_SESSION_START = "session-start-hook"
KNOWN_SCHEDULERS = (SCHEDULER_SYSTEMD, SCHEDULER_LAUNCHD, SCHEDULER_SESSION_START)
SCHEDULER_DEFAULT_KEY = "default"

STALE_AFTER_HOURS_MIN = 1
#: One week. D-3's chosen-Con mitigation: the knob exists for a host whose scheduler cannot run,
#: but it must not be settable to a value that hides a dead refresher indefinitely.
STALE_AFTER_HOURS_MAX = 168

DEFAULT_STALE_AFTER_HOURS = 24
DEFAULT_REFRESH_TIMEOUT_SECONDS = 25

#: Shared verbatim with `cx`'s CX_MACHINE_MARKER_FILE and with src/ai_chassis/machine.py's
#: MACHINE_MARKER_ENV_VAR. The marker lives outside $HOME on purpose: it names the BOX, not the
#: user, so every shell shape -- interactive, login, non-interactive, a Claude Code Bash tool
#: call -- resolves the same identity with nothing exported.
MACHINE_MARKER_ENV_VAR = "CX_MACHINE_MARKER_FILE"
MACHINE_MARKER_PATH = "/etc/ai-harness/machine"
MACHINE_ENV_VAR = "AI_HOST"

GRAMMAR_OPENAI = "openai"
GRAMMAR_ANTHROPIC_BEDROCK = "anthropic-bedrock"
GRAMMAR_ANTHROPIC_API = "anthropic-api"

# Each grammar is matched in TWO steps, not one: a loose match that extracts the FAMILY, then a
# strict parse of the version segment. One combined strict regex would fail entirely on
# `gpt-preview-sol`, so that id would carry family: null and the resolver could never reach exit
# 4 -- "refuse, do not guess" would be unreachable code. `cx`'s shipped resolver splits it the
# same way for the same reason.
_OPENAI_LOOSE = re.compile(
    r"^[A-Za-z][A-Za-z0-9]*-(?P<rest>[^-]+)-(?P<family>[a-z][a-z0-9]*)$"
)
_OPENAI_VERSION = re.compile(r"^[0-9]+(\.[0-9]+)*$")

_BEDROCK_LOOSE = re.compile(
    r"^(?:[a-z]{2}\.)?anthropic\.claude-(?P<family>[a-z]+)-(?P<rest>.+)$"
)
_API_LOOSE = re.compile(r"^claude-(?P<family>[a-z]+)-(?P<rest>.+)$")
# The revision suffix is OPTIONAL and may lack `:M`, because the live Bedrock list carries all
# three shapes at once (`claude-opus-5-5`, `claude-opus-4-6-v1`,
# `claude-haiku-4-5-20251001-v1:0`; measured 2026-10-08 on example-project-ec2).
_BEDROCK_VERSION = re.compile(
    r"^(?P<ver>[0-9]+(?:-[0-9]+)*?)"
    r"(?:-(?P<date>[0-9]{8}))?"
    r"(?:-v(?P<rev>[0-9]+)(?::(?P<minor>[0-9]+))?)?$"
)
_API_VERSION = re.compile(r"^(?P<ver>[0-9]+(?:-[0-9]+)*?)(?:-(?P<date>[0-9]{8}))?$")

#: An environment variable whose NAME ends in one of these and whose VALUE is at least this long
#: must not appear in the serialized catalog. The length floor is not cosmetic: without it an
#: empty or two-character value (`""`, `"ok"`) matches ordinary JSON and the scrub fires on every
#: write, which is a guard that has to be disabled to get any work done.
SECRET_NAME_SUFFIXES = ("KEY", "TOKEN", "SECRET")
SECRET_MIN_VALUE_LENGTH = 8
SECRET_SHAPES = ("Bearer ", "sk-", "api-key:")

#: `refresh --capture-fixture` writes here, in the checkout this module runs from (T-3.2).
FIXTURE_CAPTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "model_catalog"
_CAPTURE_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class CatalogError(Exception):
    """A refusal that carries the exit code the CLI must use."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class ScrubViolation(CatalogError):
    """A credential-shaped value reached the serialized catalog; the write must not happen."""

    def __init__(self, message: str) -> None:
        super().__init__(message, EXIT_USAGE)


class CrossGrammarComparison(Exception):
    """Two ids from different vendor grammars were handed to one comparator."""


# --- machine identity ---------------------------------------------------------------------


def machine_marker_path(environ: dict[str, str] | None = None) -> Path:
    """Where to read this box's identity from."""
    values = os.environ if environ is None else environ
    override = (values.get(MACHINE_MARKER_ENV_VAR) or "").strip()
    return Path(override) if override else Path(MACHINE_MARKER_PATH)


def resolve_host(environ: dict[str, str] | None = None) -> str:
    """This machine's AI_HOST identity, from the environment or the marker file.

    Mirrors `cx`'s `_cx_ai_host` exactly, including the marker's parse rule: the first
    non-blank, non-comment line, trimmed. A marker file is one short token; anything else is
    malformed and resolves to the empty string rather than to a GUESSED host, because guessing
    the host picks the wrong route table.
    """
    values = os.environ if environ is None else environ
    from_env = (values.get(MACHINE_ENV_VAR) or "").strip()
    if from_env:
        return from_env
    try:
        text = machine_marker_path(values).read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


# --- the route table (D-4) ----------------------------------------------------------------


def default_route_table_path() -> Path:
    """The tracked route table that ships in this repo."""
    return (
        Path(__file__).resolve().parent.parent / "config" / "model-catalog-routes.toml"
    )


def load_route_table(path: Path | None = None) -> dict:
    """Parse and VALIDATE the route table, refusing by naming the offending key.

    Validation is not decoration here. Every refusal below is a case whose alternative is a route
    that silently does not get enumerated, which the resolver then reports as a missing family --
    a confusing symptom two layers away from its cause.
    """
    table_path = default_route_table_path() if path is None else Path(path)
    document = _read_toml(table_path)

    unknown = sorted(set(document) - ALLOWED_TOP_LEVEL_KEYS)
    if unknown:
        raise CatalogError(
            f"{REFUSAL_PREFIX} the route table at {table_path} has unknown top-level key(s) "
            f"{', '.join(unknown)}; allowed: {', '.join(sorted(ALLOWED_TOP_LEVEL_KEYS))}.",
            EXIT_USAGE,
        )

    defaults = document.get("defaults", {})
    routes = document.get("routes", {})
    hosts = document.get("hosts", {})

    unknown_params = sorted(set(routes) - set(KNOWN_ROUTES))
    if unknown_params:
        raise CatalogError(
            f"{REFUSAL_PREFIX} the route table at {table_path} declares parameters for unknown "
            f"route id(s) {', '.join(unknown_params)}; known: {', '.join(KNOWN_ROUTES)}.",
            EXIT_USAGE,
        )

    _validate_route_ids(defaults.get("routes", []), "[defaults].routes", table_path)
    for host_name, host_entry in hosts.items():
        _validate_route_ids(
            host_entry.get("routes", []), f"[hosts.{host_name}].routes", table_path
        )

    stale_after_hours = defaults.get("stale_after_hours", DEFAULT_STALE_AFTER_HOURS)
    _validate_stale_after_hours(stale_after_hours, table_path)

    scheduler = document.get("scheduler", {})
    _validate_scheduler(scheduler, table_path)

    return {
        "path": table_path,
        "defaults": defaults,
        "routes": routes,
        "hosts": hosts,
        "scheduler": scheduler,
        "stale_after_hours": stale_after_hours,
        "refresh_timeout_seconds": defaults.get(
            "refresh_timeout_seconds", DEFAULT_REFRESH_TIMEOUT_SECONDS
        ),
    }


def _read_toml(table_path: Path) -> dict:
    try:
        raw = table_path.read_bytes()
    except OSError as exc:
        raise CatalogError(
            f"{REFUSAL_PREFIX} the route table at {table_path} could not be read ({exc}).",
            EXIT_USAGE,
        ) from exc
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise CatalogError(
            f"{REFUSAL_PREFIX} the route table at {table_path} is not valid TOML ({exc}).",
            EXIT_USAGE,
        ) from exc


def _validate_route_ids(values: object, where: str, table_path: Path) -> None:
    if not isinstance(values, list):
        raise CatalogError(
            f"{REFUSAL_PREFIX} the route table at {table_path} sets {where} to {values!r}, "
            f"which is not a list of route ids.",
            EXIT_USAGE,
        )
    unknown = [value for value in values if value not in KNOWN_ROUTES]
    if unknown:
        raise CatalogError(
            f"{REFUSAL_PREFIX} the route table at {table_path} names unknown route id(s) "
            f"{', '.join(repr(value) for value in unknown)} in {where}; "
            f"known: {', '.join(KNOWN_ROUTES)}.",
            EXIT_USAGE,
        )


def _validate_stale_after_hours(value: object, table_path: Path) -> None:
    valid = (
        isinstance(value, int)
        and not isinstance(value, bool)
        and STALE_AFTER_HOURS_MIN <= value <= STALE_AFTER_HOURS_MAX
    )
    if not valid:
        raise CatalogError(
            f"{REFUSAL_PREFIX} the route table at {table_path} sets "
            f"[defaults].stale_after_hours = {value!r}, outside the permitted "
            f"{STALE_AFTER_HOURS_MIN}..{STALE_AFTER_HOURS_MAX}. A window above "
            f"{STALE_AFTER_HOURS_MAX} h would let a dead refresher go unnoticed indefinitely.",
            EXIT_USAGE,
        )


def _validate_scheduler(scheduler: object, table_path: Path) -> None:
    if not isinstance(scheduler, dict):
        raise CatalogError(
            f"{REFUSAL_PREFIX} the route table at {table_path} sets [scheduler] to "
            f"{scheduler!r}, which is not a table of platform = mechanism.",
            EXIT_USAGE,
        )
    unknown = sorted(
        f"[scheduler].{key} = {value!r}"
        for key, value in scheduler.items()
        if value not in KNOWN_SCHEDULERS
    )
    if unknown:
        raise CatalogError(
            f"{REFUSAL_PREFIX} the route table at {table_path} names unknown scheduler "
            f"mechanism(s) {', '.join(unknown)}; known: {', '.join(KNOWN_SCHEDULERS)}.",
            EXIT_USAGE,
        )


def scheduler_for(table: dict, platform_name: str) -> str:
    """The refresh mechanism for `platform_name` (a `uname -s` value).

    A platform the table does not name gets `[scheduler].default`, and with no default the
    SessionStart hook: it is the one mechanism that needs no user scheduler, so it is the only
    answer that cannot claim a timer the host does not have.
    """
    scheduler = table.get("scheduler") or {}
    return scheduler.get(
        platform_name, scheduler.get(SCHEDULER_DEFAULT_KEY, SCHEDULER_SESSION_START)
    )


def host_plan(table: dict, host: str) -> dict:
    """Which routes `host` enumerates, and with what parameters.

    A host the table does not name gets `[defaults].routes` and is FLAGGED `from_defaults: True`
    rather than quietly given an entry, so `show` and the refresh summary can say which input
    decided -- the same reason `cx` reports whether the identity came from the marker file.
    """
    entry = table["hosts"].get(host)
    from_defaults = entry is None
    if entry is None:
        entry = {"routes": list(table["defaults"].get("routes", []))}
    return {
        "host": host,
        "routes": list(entry.get("routes", [])),
        "aws_profile": entry.get("aws_profile"),
        # Carried beside the profile, not folded into it: the suffix is an account DISPLAY
        # name, which embeds no account id and so may live in a tracked file, while the
        # profile name it selects does embed one.
        "aws_account_suffix": entry.get("aws_account_suffix"),
        "from_defaults": from_defaults,
        "scheduler": table.get("scheduler") or {},
        "stale_after_hours": table["stale_after_hours"],
        "refresh_timeout_seconds": table["refresh_timeout_seconds"],
        "route_params": table["routes"],
    }


# --- id grammars and the ordering rule (D-5) ----------------------------------------------


def _parse_openai(model_id: str) -> tuple[str | None, list[int] | None]:
    match = _OPENAI_LOOSE.match(model_id)
    if match is None:
        return None, None
    family = match.group("family")
    rest = match.group("rest")
    if _OPENAI_VERSION.match(rest) is None:
        return family, None
    return family, [int(part) for part in rest.split(".")]


def _parse_anthropic_bedrock(model_id: str) -> tuple[str | None, list[int] | None]:
    match = _BEDROCK_LOOSE.match(model_id)
    if match is None:
        return None, None
    family = match.group("family")
    version_match = _BEDROCK_VERSION.match(match.group("rest"))
    if version_match is None:
        return family, None
    parts = [int(piece) for piece in version_match.group("ver").split("-")]
    # Date, revision and minor are appended as explicit zeros rather than omitted, so that
    # `claude-opus-4-6-v1` and `claude-opus-4-5-20251101-v1:0` compare on the same axes. They are
    # the LAST axes on purpose: a generation bump outranks any date, which is what makes
    # `4-6-v1` (4,6,0,1,0) correctly beat `4-5-20251101-v1:0` (4,5,20251101,1,0).
    parts.append(int(version_match.group("date") or 0))
    parts.append(int(version_match.group("rev") or 0))
    parts.append(int(version_match.group("minor") or 0))
    return family, parts


def _parse_anthropic_api(model_id: str) -> tuple[str | None, list[int] | None]:
    match = _API_LOOSE.match(model_id)
    if match is None:
        return None, None
    family = match.group("family")
    version_match = _API_VERSION.match(match.group("rest"))
    if version_match is None:
        return family, None
    parts = [int(piece) for piece in version_match.group("ver").split("-")]
    parts.append(int(version_match.group("date") or 0))
    return family, parts


_GRAMMARS = {
    GRAMMAR_OPENAI: _parse_openai,
    GRAMMAR_ANTHROPIC_BEDROCK: _parse_anthropic_bedrock,
    GRAMMAR_ANTHROPIC_API: _parse_anthropic_api,
}


def parse_model_id(model_id: str, grammar: str) -> tuple[str | None, list[int] | None]:
    """Split an id into (family, comparable version) under one vendor grammar.

    Returns `(family, None)` for an id whose family is recognizable but whose version is not:
    that is the state the resolver refuses on (exit 4). Returns `(None, None)` when the id does
    not belong to this grammar at all, which is not an error -- a route's catalog legitimately
    carries ids of other shapes.
    """
    try:
        parser = _GRAMMARS[grammar]
    except KeyError:
        raise ValueError(f"unknown grammar {grammar!r}") from None
    return parser(model_id)


def version_sort_key(entry: dict) -> tuple[int, ...]:
    """The comparable key for one catalog entry. Never a string sort (AC-3)."""
    version = entry.get("version")
    if version is None:
        raise ValueError(f"{entry.get('id')!r} has no parsed version to compare")
    return tuple(version)


def latest(entries: list[dict]) -> dict:
    """The newest of `entries`, which must all carry ONE grammar.

    Refusing a cross-grammar comparison is not pedantry: `gpt-5.6-sol` and
    `anthropic.claude-opus-5-5` parse to tuples that compare without error and the answer is
    meaningless. A route carries one grammar by construction, so a mixed list is a defect in the
    caller and has to surface as one.
    """
    if not entries:
        raise ValueError("latest() needs at least one entry")
    grammars = {entry.get("grammar") for entry in entries}
    if len(grammars) > 1:
        raise CrossGrammarComparison(
            "refusing to compare model ids from different vendor grammars: "
            + ", ".join(sorted(str(grammar) for grammar in grammars))
        )
    return max(entries, key=version_sort_key)


# --- the catalog file (D-1) ---------------------------------------------------------------


def catalog_path(environ: dict[str, str] | None = None) -> Path:
    """`${XDG_STATE_HOME:-$HOME/.local/state}/ai-harness/model-catalog/catalog.json`."""
    values = os.environ if environ is None else environ
    configured = (values.get("XDG_STATE_HOME") or "").strip()
    root = (
        Path(configured)
        if configured
        else Path(values.get("HOME", "~")) / ".local" / "state"
    )
    return root / "ai-harness" / "model-catalog" / "catalog.json"


def lock_path(catalog: Path) -> Path:
    return catalog.with_name(catalog.name + ".lock")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def age_hours(fetched_at: str | None, now: dt.datetime | None = None) -> float | None:
    moment = parse_iso(fetched_at) if fetched_at else None
    if moment is None:
        return None
    reference = utc_now() if now is None else now
    return (reference - moment).total_seconds() / 3600.0


def read_catalog(catalog: Path) -> dict | None:
    """The catalog as written, or None when it is absent or unreadable as JSON."""
    try:
        text = catalog.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return None
    return document if isinstance(document, dict) else None


class Context:
    """The ambient inputs every operation needs, injectable as one object.

    They travel together because they are one question -- "which environment, which file, as of
    when, reporting where" -- and because a test needs to replace all four at once. Passing them
    individually through four call layers was the alternative and it put six-parameter
    signatures on the two functions whose readability matters most.
    """

    def __init__(
        self,
        environ: dict[str, str] | None = None,
        catalog: Path | None = None,
        now: dt.datetime | None = None,
        stderr: object = None,
    ) -> None:
        self.environ = dict(os.environ) if environ is None else environ
        self.catalog = catalog_path(self.environ) if catalog is None else catalog
        self.now = utc_now() if now is None else now
        self.stderr = sys.stderr if stderr is None else stderr


def scrub(serialized: str, environ: dict[str, str] | None = None) -> None:
    """Raise if the payload about to be written carries a credential-shaped value.

    CALLED IMMEDIATELY BEFORE THE WRITE AND IN THE SAME CONTROL-FLOW UNIT (see `_atomic_write`):
    raising here makes the `os.replace` UNREACHABLE, which is the only form in which this is a
    guard. A scrub that merely reported a hit and let the next statement rename the temp file
    into place would leave the secret in exactly the state it exists to prevent.

    Names the variable and the shape, NEVER the value -- a guard that prints the secret it caught
    has moved that secret from a mode-0600 file to a terminal and a log.
    """
    values = os.environ if environ is None else environ
    for name, value in values.items():
        if not name.endswith(SECRET_NAME_SUFFIXES):
            continue
        if not value or len(value) < SECRET_MIN_VALUE_LENGTH:
            continue
        if value in serialized:
            raise ScrubViolation(
                f"{REFUSAL_PREFIX} the catalog about to be written contains the value of "
                f"${name} (an environment variable whose name ends in a credential suffix and "
                f"whose value is {len(value)} characters). The write was ABORTED and any prior "
                f"catalog is untouched. The value is deliberately not printed."
            )
    for shape in SECRET_SHAPES:
        if shape in serialized:
            raise ScrubViolation(
                f"{REFUSAL_PREFIX} the catalog about to be written contains a credential-shaped "
                f"string ({shape!r}). The write was ABORTED and any prior catalog is untouched. "
                f"The value is deliberately not printed."
            )


def _atomic_write(
    catalog: Path, document: dict, environ: dict[str, str] | None = None
) -> None:
    """Scrub, then publish atomically. The scrub GATES the publish; it does not merely precede it.

    A reader never observes a partial file: the bytes land in a temp file in the same directory
    and `os.replace` is atomic within a filesystem. If the rename fails the temp file is removed
    and the previous catalog is still the one on disk.
    """
    serialized = json.dumps(document, indent=2, sort_keys=False) + "\n"
    try:
        scrub(serialized, environ)  # raises => the os.replace below is unreachable
    except ScrubViolation as exc:
        # `scrub` sees only bytes, so it cannot name the ROUTE the AC requires ("abort the write,
        # leave any prior catalog untouched, and name the route -- never the value"). The routes
        # are added here, where the document is in scope. Re-raising still leaves the write
        # unreachable: there is no path from this handler to the `os.replace` below.
        routes = ", ".join(sorted(document.get("routes") or {})) or "(none)"
        raise ScrubViolation(
            f"{exc.message} Routes in the aborted write: {routes}."
        ) from exc
    catalog.parent.mkdir(parents=True, exist_ok=True)
    # The same shape as `scripts/work_queue_config.py::write_atomic`: a pid-named sibling in the
    # destination directory, chmod-ed BEFORE the rename so the file is never briefly 0644 under
    # a name anything reads, then one `os.replace`. Reusing that idiom rather than inventing one
    # keeps this module's write reviewable against a precedent the repo already signed off.
    temporary = catalog.with_name(f".{catalog.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(serialized, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, catalog)
    finally:
        # Reached on the success path too, where the rename has already consumed the temp file;
        # `missing_ok` makes that a no-op. On ANY failure -- a full disk, a failing rename, a
        # KeyboardInterrupt between the write and the replace -- this is what guarantees no
        # partial file is left observable beside the catalog.
        temporary.unlink(missing_ok=True)


class _CatalogLock:
    """An exclusive flock around the whole read-merge-write, with a deadline.

    The deadline exists because the alternative to a bounded wait is a refresh-on-miss on the
    launch path hanging forever behind a wedged timer tick. A timeout REFUSES and names the lock
    path; it never writes, so the file a competing writer is part-way through is left alone.
    """

    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._handle = None

    def __enter__(self) -> _CatalogLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise CatalogError(
                        f"{REFUSAL_PREFIX} could not acquire the catalog lock at {self.path} "
                        f"within {self.timeout_seconds:g}s, so another refresh is in progress. "
                        f"Nothing was written and the existing catalog is untouched.",
                        EXIT_USAGE,
                    ) from None
                time.sleep(0.05)

    def __exit__(self, *exc_info: object) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


# --- enumeration seams -------------------------------------------------------------------
# The two places this module touches the outside world. Isolated as functions so the test suite
# can feed captured fixtures through the REAL parsers: a test that monkeypatched the parsing
# would prove only that a mock returns what it was handed.


#: The per-host SOCKS upstream `cx` bridges codex through (ISSUE-REF). Shared with `cx`, including
#: its CX_SOCKS_BRIDGE_CONFIG override, so a host has one statement of its tunnel.
SOCKS_BRIDGE_CONFIG_ENV_VAR = "CX_SOCKS_BRIDGE_CONFIG"
_PROXY_ENV_VARS = ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy")


def socks_bridge_config_path(environ: dict[str, str] | None = None) -> Path:
    """config/cx-socks-bridge.toml, or the file CX_SOCKS_BRIDGE_CONFIG names."""
    values = os.environ if environ is None else environ
    return Path(
        values.get(SOCKS_BRIDGE_CONFIG_ENV_VAR)
        or _REPO_ROOT / "config" / "cx-socks-bridge.toml"
    )


def host_socks_upstream(environ: dict[str, str] | None = None) -> str | None:
    """This host's `[upstream]` entry in config/cx-socks-bridge.toml, or None.

    A value carrying credentials is ignored rather than used: it would be echoed in a
    curl error, and `cx` refuses the same entry by name.
    """
    values = os.environ if environ is None else environ
    path = socks_bridge_config_path(values)
    host = resolve_host(values)
    if not host:
        return None
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    value = (table.get("upstream") or {}).get(host)
    if not isinstance(value, str) or not value or "@" in value:
        return None
    return value


def _socks_proxy() -> str | None:
    """The SOCKS proxy URL to fetch through, if any; urllib cannot speak SOCKS.

    The environment wins. With no proxy in it at all, the host's own SOCKS upstream is used, so
    the scheduled refresh on example-client-host-mac reaches the gateway through the tunnel instead of recording
    the route as a 403 that every family resolution then refuses on.
    """
    for name in _PROXY_ENV_VARS:
        value = os.environ.get(name, "")
        if value.lower().startswith("socks"):
            return value
    if any(os.environ.get(name) for name in _PROXY_ENV_VARS):
        return None
    return host_socks_upstream()


class ProxyUnreachable(urllib.error.URLError):
    """curl could not connect to the SOCKS proxy itself: nothing listens on its host:port."""

    def __init__(self, reason: str, proxy: str) -> None:
        super().__init__(reason)
        self.proxy = proxy


#: curl's CURLE_COULDNT_CONNECT. Through a proxy it is also what curl returns when the PROXY's
#: socket refuses, so the stderr must name the proxy's own host and port to count as that.
_CURL_COULDNT_CONNECT = 7


def _refused_by_proxy(proxy: str, returncode: int, stderr: str) -> bool:
    if returncode != _CURL_COULDNT_CONNECT:
        return False
    try:
        parts = urllib.parse.urlsplit(proxy)
        host, port = parts.hostname, parts.port
    except ValueError:
        return False
    if not host or port is None:
        return False
    if f"Failed to connect to {host} port {port}" in stderr:
        return True
    # curl 8.x names the TARGET first and the proxy host after "via" (the proxy port is not
    # printed): "Failed to connect to <target> port 443 via 127.0.0.1 after 0 ms". Through a
    # SOCKS proxy a refusal by the upstream is curl 97, so exit 7 naming the proxy host is the
    # proxy's own socket refusing.
    return (
        re.search(rf"Failed to connect to \S+ port \d+ via {re.escape(host)}\b", stderr)
        is not None
    )


def http_get_json(url: str, timeout_seconds: float) -> dict:
    """GET a JSON document. No credential: /info/models is unauthenticated by design.

    Through a SOCKS proxy the GET goes via curl: urllib has no SOCKS support and would send plain
    HTTP to the SOCKS port. example-client-host-mac reaches the example-client-host gateway only through an SSH `-D` tunnel with
    `ALL_PROXY=socks5h://...`, which is what codex itself honours (ISSUE-REF).
    """
    proxy = _socks_proxy()
    if proxy:
        argv = [
            "curl",
            "-sS",
            "--fail",
            "--max-time",
            f"{timeout_seconds:g}",
            "--proxy",
            proxy,
        ]
        argv += ["-H", "Accept: application/json", url]
        try:
            proc = run_command(argv, timeout_seconds + 5)
        except subprocess.TimeoutExpired as exc:
            raise urllib.error.URLError(f"curl via {proxy} timed out") from exc
        if proc.returncode != 0:
            reason = f"curl via {proxy} exited {proc.returncode}: {proc.stderr.strip()}"
            if _refused_by_proxy(proxy, proc.returncode, proc.stderr):
                raise ProxyUnreachable(reason, proxy)
            raise urllib.error.URLError(reason)
        return json.loads(proc.stdout)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        payload = response.read()
    return json.loads(payload.decode("utf-8"))


def run_command(argv: list[str], timeout_seconds: float) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        argv, capture_output=True, text=True, timeout=timeout_seconds, check=False
    )


def _which(binary: str) -> str | None:
    for directory in (os.environ.get("PATH") or "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# --- route enumerators --------------------------------------------------------------------


def _route_entry(
    status: str,
    source: str,
    models: list[dict],
    error: str | None,
    fetched_at: str,
    **extra,
) -> dict:
    entry = {
        "status": status,
        "source": source,
        "fetched_at": fetched_at,
        "error": error,
        "models": models,
    }
    entry.update(extra)
    return entry


def azure_gateway_url(lane: str) -> str:
    """The lane's unauthenticated catalog endpoint.

    The gateway base `cx` sends turns to (`_cx_azure_base_url`) with `/openai/v1` stripped and
    `/info/models` appended. Since T-4.1 this module is the only catalog reader: `cx` no longer
    fetches the catalog itself, it calls `resolve`.
    """
    return f"https://example-client-host-aigateway-mvswb-{lane}.azu.example-client-host.com/info/models"


def _gateway_model_entry(
    vendor: str, raw_entry: dict, is_candidate_vendor: bool
) -> dict:
    name = raw_entry["deployment_name"]
    if is_candidate_vendor:
        family, version = parse_model_id(name, GRAMMAR_OPENAI)
        grammar: str | None = GRAMMAR_OPENAI
    else:
        # Recorded, but never a family candidate: `vendor` is set and `family` is null, so the id
        # stays visible in `show` and unreachable from `--family`.
        family, version, grammar = None, None, None
    return {
        "id": name,
        "vendor": vendor,
        "family": family,
        "version": version,
        "grammar": grammar,
        # An allowlisted `raw`, not the provider's whole record (Risk 7): there is no field here
        # for a token to ride in, which is the first layer of the credential defence and the
        # reason the scrub is only the second.
        "raw": {
            "deployment_name": name,
            "model_version": raw_entry.get("model_version"),
        },
    }


def enumerate_azure_gateway(
    params: dict, timeout_seconds: float, now: dt.datetime
) -> dict:
    """The example-client-host gateway's own deployment list.

    `models` is an object keyed by VENDOR whose values are lists of entries carrying
    `deployment_name` -- the shape `cx` and scripts/codex_azure_preflight.sh already parse. Only
    entries under the route's `vendor_keys` are family candidates: the nonprod lane lists
    Anthropic `claude-*` deployments beside the OpenAI ones (measured 2026-10-08: 5 vendor keys,
    77 deployments, 51 under OpenAI), so without that filter `--family opus` would resolve
    against a route whose credential serves Codex.
    """
    lane = params.get("lane", "nonprod")
    vendor_keys = set(params.get("vendor_keys", ["OpenAI"]))
    url = azure_gateway_url(lane)
    source = f"GET {url}"
    try:
        payload = http_get_json(url, timeout_seconds)
    except ProxyUnreachable as exc:
        return _route_entry(
            STATUS_PROXY_UNREACHABLE,
            source,
            [],
            f"{type(exc).__name__}: {exc.reason}",
            iso(now),
            lane=lane,
            proxy=exc.proxy,
        )
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        return _route_entry(
            "error", source, [], f"{type(exc).__name__}: {exc}", iso(now), lane=lane
        )

    vendors = payload.get("models")
    if not isinstance(vendors, dict):
        return _route_entry(
            "error",
            source,
            [],
            "the response did not parse as the expected catalog shape "
            "(no `models` object keyed by vendor)",
            iso(now),
            lane=lane,
        )

    models = [
        _gateway_model_entry(vendor, raw_entry, vendor in vendor_keys)
        for vendor, entries in vendors.items()
        if isinstance(entries, list)
        for raw_entry in entries
        if isinstance(raw_entry, dict) and raw_entry.get("deployment_name")
    ]
    return _route_entry("ok", source, models, None, iso(now), lane=lane)


def _codex_ids_from_json(text: str) -> list[str]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"`codex debug models` stdout began as JSON but did not parse ({exc})"
        ) from exc
    if isinstance(document, dict):
        document = document.get("models", document.get("data", []))
    if not isinstance(document, list):
        raise ValueError(
            "`codex debug models` JSON output was not a list of models "
            f"(got {type(document).__name__})"
        )
    ids = []
    for item in document:
        if isinstance(item, str):
            ids.append(item.strip())
        elif isinstance(item, dict):
            value = item.get("id") or item.get("name") or item.get("slug")
            if not value:
                raise ValueError(
                    "a `codex debug models` JSON entry carried no id/name/slug field"
                )
            ids.append(str(value).strip())
        else:
            raise ValueError(
                f"a `codex debug models` JSON entry was a {type(item).__name__}, "
                "not a string or an object"
            )
    return ids


def parse_codex_debug_models(stdout: str) -> list[str]:
    """One model id per non-empty line, or a JSON list of strings / {id|name|slug} objects.

    Lenient ON PURPOSE and bounded: the real output shape is unmeasured (Open Question 2), so
    this accepts the documented forms and RAISES on anything else rather than inferring. An
    empty result raises too -- a silent empty list would be recorded as a healthy route serving
    nothing, and the resolver's later refusal would then blame the family.
    """
    text = stdout.strip()
    if not text:
        raise ValueError("`codex debug models` printed nothing on stdout")
    if text[0] in "[{":
        ids = _codex_ids_from_json(text)
    else:
        ids = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    ids = [value for value in ids if value]
    if not ids:
        raise ValueError("`codex debug models` output carried no model ids")
    return ids


def enumerate_codex_oauth(
    params: dict, timeout_seconds: float, now: dt.datetime
) -> dict:
    """The ChatGPT subscription's model list, via `codex debug models`.

    UNMEASURED SHAPE. `codex` is absent from example-project-ec2's agent-shell PATH and this host has no
    ChatGPT login, so the parser is written against the DOCUMENTED output and pinned by
    tests/fixtures/model_catalog/codex-debug-models.documented.txt, whose header says exactly
    that. The first personal host to run /install-harness captures a real one (Phase 3 T-3.2).

    A missing binary or a missing login is `unenumerable`, NOT `error`: the route simply does not
    exist on this host, which is a different fact from a route that exists and failed, and the
    resolver's refusal reads differently for each.
    """
    source = "codex debug models"
    binary = params.get("codex_binary") or _which("codex")
    if not binary:
        return _route_entry(
            "unenumerable",
            source,
            [],
            "codex-cli is not on PATH on this host, so the ChatGPT subscription's model list "
            "cannot be enumerated here",
            iso(now),
        )
    auth_file = Path(
        params.get("codex_auth_file") or (Path.home() / ".codex" / "auth.json")
    )
    if not auth_file.exists():
        return _route_entry(
            "unenumerable",
            source,
            [],
            f"codex-cli is not logged in on this host (no {auth_file})",
            iso(now),
        )
    try:
        proc = run_command([binary, "debug", "models"], timeout_seconds)
    except (OSError, subprocess.SubprocessError) as exc:
        return _route_entry(
            "error", source, [], f"{type(exc).__name__}: {exc}", iso(now)
        )
    if proc.returncode != 0:
        return _route_entry(
            "error",
            source,
            [],
            f"`codex debug models` exited {proc.returncode}: {_stderr_tail(proc)}",
            iso(now),
        )
    captured = _capture_codex_fixture(params, proc.stdout)
    try:
        ids = parse_codex_debug_models(proc.stdout)
    except ValueError as exc:
        return _route_entry(
            "error", source, [], str(exc), iso(now), **_captured_extra(captured)
        )
    models = []
    for model_id in ids:
        family, version = parse_model_id(model_id, GRAMMAR_OPENAI)
        models.append(
            {
                "id": model_id,
                "vendor": "openai",
                "family": family,
                "version": version,
                "grammar": GRAMMAR_OPENAI,
                "raw": {"id": model_id},
            }
        )
    return _route_entry(
        "ok", source, models, None, iso(now), **_captured_extra(captured)
    )


def _capture_codex_fixture(params: dict, stdout: str) -> str | None:
    """Write `codex debug models`' raw stdout to the fixture path T-3.2 names, when asked.

    Risk 8: the OAuth parser is pinned only by the DOCUMENTED output until a personal host
    captures the real one. The capture is opt-in (`refresh --capture-fixture`) and lands in the
    checkout this module runs from, for a human to review and commit; it is never written
    unasked. The same scrub that gates the catalog gates this write, because a tracked fixture
    is a worse place for a credential than a mode-0600 file.

    The target is spelled AT the write, from the module constant and a host that matched
    `_CAPTURE_HOST` (no separator, no leading dot), so the one path this can ever write is
    readable here rather than carried in from a caller.

    Returns the path written, or a `refused:` reason; None when no capture was requested.
    """
    host = params.get("capture_fixture_host")
    if not host or not stdout.strip():
        return None
    if not _CAPTURE_HOST.match(host):
        return f"refused: {host!r} is not a plain AI_HOST to name the fixture file"
    try:
        scrub(stdout)
    except ScrubViolation as exc:
        return f"refused: {exc.message}"
    FIXTURE_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_CAPTURE_DIR / f"codex-debug-models.{host}.txt").write_text(
        stdout, encoding="utf-8"
    )
    return str(capture_fixture_path(host))


def _captured_extra(captured: str | None) -> dict:
    return {} if captured is None else {"captured_fixture": captured}


def _stderr_tail(proc: subprocess.CompletedProcess) -> str:
    tail = (proc.stderr or "").strip().splitlines()
    return (tail[-1] if tail else "")[:200]


def foundation_model_id(profile: dict) -> str | None:
    """The FOUNDATION model id to ask availability about, from the profile's own ARN tail.

    Measured 2026-10-08 on example-project-ec2: `get-foundation-model-availability --model-id` accepts the
    `models[].modelArn` tail (`anthropic.claude-opus-5-5`) and the API echoes `modelId` WITHOUT a
    `:0` suffix. Reading the tail is preferred to stripping the `us.` prefix off the
    inference-profile id, because the ARN is the provider's OWN statement of which foundation
    model the profile routes to, rather than our inference from its name.
    """
    models = profile.get("models")
    if isinstance(models, list):
        for model in models:
            arn = model.get("modelArn") if isinstance(model, dict) else None
            if isinstance(arn, str) and "/" in arn:
                return arn.rsplit("/", 1)[-1]
    profile_id = profile.get("inferenceProfileId")
    if isinstance(profile_id, str):
        return re.sub(r"^[a-z]{2}\.", "", profile_id)
    return None


def availability_of(record: dict) -> tuple[bool, str]:
    """D-10: available only when the provider says yes on ALL FOUR axes.

    A candidate whose availability record is missing or whose call errored is UNAVAILABLE and
    named, never assumed callable -- the whole point of the check is that `listed` is not
    `callable`, so an absent answer must not read as a positive one.
    """
    if not record:
        return False, "no availability record was returned for this candidate"
    axes = {
        "authorizationStatus": ("AUTHORIZED", record.get("authorizationStatus")),
        "entitlementAvailability": ("AVAILABLE", record.get("entitlementAvailability")),
        "regionAvailability": ("AVAILABLE", record.get("regionAvailability")),
        "agreementAvailability.status": (
            "AVAILABLE",
            (record.get("agreementAvailability") or {}).get("status"),
        ),
    }
    failures = [
        f"{name}={actual!r} (wanted {wanted!r})"
        for name, (wanted, actual) in axes.items()
        if actual != wanted
    ]
    if failures:
        return False, "; ".join(failures)
    return True, ""


def _bedrock_model_entry(profile: dict, params: dict, timeout_seconds: float) -> dict:
    profile_id = profile["inferenceProfileId"]
    family, version = parse_model_id(profile_id, GRAMMAR_ANTHROPIC_BEDROCK)
    model_id = foundation_model_id(profile)
    raw = {
        "inferenceProfileId": profile_id,
        "inferenceProfileName": profile.get("inferenceProfileName"),
        "status": profile.get("status"),
        "type": profile.get("type"),
        "createdAt": profile.get("createdAt"),
        "updatedAt": profile.get("updatedAt"),
        "foundationModelId": model_id,
    }
    available, reason = True, ""
    if params.get("availability_check", True):
        record, error = _availability_record(
            model_id,
            params.get("region", "us-east-1"),
            params["aws_profile"],
            timeout_seconds,
        )
        available, reason = (False, error) if error else availability_of(record)
        raw["availability"] = record or {}
        if reason:
            raw["availabilityReason"] = reason
    return {
        "id": profile_id,
        "vendor": "anthropic",
        "family": family,
        "version": version,
        "grammar": GRAMMAR_ANTHROPIC_BEDROCK,
        "available": available,
        "unavailable_reason": reason or None,
        "raw": raw,
    }


def _is_anthropic_profile(profile: object, prefix: str) -> bool:
    if not isinstance(profile, dict):
        return False
    profile_id = profile.get("inferenceProfileId")
    if not isinstance(profile_id, str) or not profile_id.startswith(prefix):
        return False
    return ".anthropic." in profile_id or profile_id.startswith("anthropic.")


def enumerate_claude_bedrock(
    params: dict, timeout_seconds: float, now: dt.datetime
) -> dict:
    """The `us.` Anthropic inference profiles, each carrying the provider's availability answer."""
    region = params.get("region", "us-east-1")
    aws_profile = params.get("aws_profile")
    # RESOLVED AT CALL TIME when the table says `derive` (ISSUE-REF), so a profile rename needs
    # no edit here and no edit to the tracked route table. Fails CLOSED into the same `error`
    # entry the no-profile case uses: an un-profiled AWS call from an EC2 host resolves the
    # INSTANCE role, so a guessed or blank profile would enumerate the wrong identity and
    # report it as the host's catalog.
    if aws_profile == DERIVE_AWS_PROFILE:
        try:
            aws_profile = aws_profile_resolver.resolve_name(
                account_suffix=params.get("aws_account_suffix")
            )
        except aws_profile_resolver.ProfileResolutionError as exc:
            return _route_entry(
                "error",
                "aws bedrock list-inference-profiles",
                [],
                f"the route table defers this host's aws_profile to ~/.aws/config and it "
                f"could not be resolved: {exc}",
                iso(now),
                region=region,
            )
    base = ["aws", "bedrock", "list-inference-profiles", "--region", region]
    if aws_profile:
        base += ["--profile", aws_profile]
    source = " ".join(base)
    if not aws_profile:
        return _route_entry(
            "error",
            source,
            [],
            "the route table names no aws_profile for this host, and an un-profiled AWS call "
            "from an EC2 host resolves the INSTANCE role and would enumerate the wrong identity",
            iso(now),
            region=region,
        )
    try:
        proc = run_command(base, timeout_seconds)
    except (OSError, subprocess.SubprocessError) as exc:
        return _route_entry(
            "error", source, [], f"{type(exc).__name__}: {exc}", iso(now), region=region
        )
    if proc.returncode != 0:
        return _route_entry(
            "error",
            source,
            [],
            f"`aws bedrock list-inference-profiles` exited {proc.returncode}: "
            f"{_stderr_tail(proc)}",
            iso(now),
            region=region,
        )
    try:
        summaries = json.loads(proc.stdout)["inferenceProfileSummaries"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return _route_entry(
            "error",
            source,
            [],
            f"the inference-profile list did not parse as expected ({type(exc).__name__}: {exc})",
            iso(now),
            region=region,
        )

    prefix = params.get("inference_profile_prefix", "us.")
    # Each availability call gets the profile RESOLVED above, never the table's `derive` selector:
    # passing `params` through unchanged sent `--profile derive` to aws for every model.
    resolved = {**params, "aws_profile": aws_profile}
    models = [
        _bedrock_model_entry(profile, resolved, timeout_seconds)
        for profile in summaries
        if _is_anthropic_profile(profile, prefix)
    ]
    return _route_entry("ok", source, models, None, iso(now), region=region)


def _availability_record(
    model_id: str | None, region: str, aws_profile: str, timeout_seconds: float
) -> tuple[dict, str]:
    if not model_id:
        return {}, "could not derive a foundation model id for this inference profile"
    argv = [
        "aws",
        "bedrock",
        "get-foundation-model-availability",
        "--model-id",
        model_id,
        "--region",
        region,
        "--profile",
        aws_profile,
    ]
    try:
        proc = run_command(argv, timeout_seconds)
    except (OSError, subprocess.SubprocessError) as exc:
        return {}, f"the availability call failed ({type(exc).__name__}: {exc})"
    if proc.returncode != 0:
        return (
            {},
            f"the availability call exited {proc.returncode}: {_stderr_tail(proc)}",
        )
    try:
        record = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {}, f"the availability response did not parse as JSON ({exc})"
    return (record if isinstance(record, dict) else {}), ""


def enumerate_claude_subscription(
    _params: dict, _timeout_seconds: float, now: dt.datetime
) -> dict:
    """A first-class recorded finding, not a stub.

    Claude Code resolves its `opus`/`sonnet` aliases internally and exposes no models-list
    command, and Anthropic's `GET /v1/models` needs an API key a subscription does not provide.
    So this route is `unenumerable` with that reason, family tokens refuse on it, and an explicit
    pinned id keeps working unchanged. Open Question 1 tracks whether a source appears.
    """
    return _route_entry(
        "unenumerable",
        "(no enumeration source is known for the Claude subscription route)",
        [],
        "Claude Code resolves its tier aliases internally and exposes no models-list command, "
        "and Anthropic's GET /v1/models needs an API key the subscription does not provide, so "
        "this route cannot be enumerated. Explicit pinned ids are unaffected.",
        iso(now),
    )


ENUMERATORS = {
    ROUTE_AZURE_GATEWAY: enumerate_azure_gateway,
    ROUTE_CODEX_OAUTH: enumerate_codex_oauth,
    ROUTE_CLAUDE_BEDROCK: enumerate_claude_bedrock,
    ROUTE_CLAUDE_SUBSCRIPTION: enumerate_claude_subscription,
}


# --- the refresher ------------------------------------------------------------------------


def _plan_summary_line(plan: dict) -> str:
    return (
        f"host={plan['host'] or '<unset>'} "
        f"from_defaults={'true' if plan['from_defaults'] else 'false'} "
        f"routes={','.join(plan['routes']) or '(none)'}"
    )


def _route_summary_line(route: str, entry: dict) -> str:
    proxy = f" proxy={entry['proxy']}" if entry.get("proxy") else ""
    line = (
        f"route={route} status={entry['status']}{proxy} models={len(entry['models'])} "
        f"fetched_at={entry['fetched_at']}"
    )
    return line + (f" error={entry['error']}" if entry.get("error") else "")


def _enumerate_one(
    plan: dict,
    route: str,
    timeout_seconds: float,
    moment: dt.datetime,
    capture_fixture: bool = False,
) -> dict:
    params = dict(plan["route_params"].get(route) or {})
    if plan.get("aws_profile"):
        params.setdefault("aws_profile", plan["aws_profile"])
    # `derive` is a selector, and the suffix is its argument (ISSUE-REF): without it the
    # resolver can choose only through AWS_PROFILE, which the scheduled refresh never has.
    if plan.get("aws_account_suffix"):
        params.setdefault("aws_account_suffix", plan["aws_account_suffix"])
    if capture_fixture and route == ROUTE_CODEX_OAUTH:
        params["capture_fixture_host"] = plan["host"]
    try:
        return ENUMERATORS[route](params, timeout_seconds, moment)
    except Exception as exc:  # noqa: BLE001 - one bad route must never kill the others
        return _route_entry(
            "error", route, [], f"{type(exc).__name__}: {exc}", iso(moment)
        )


def refresh(
    plan: dict,
    route_ids: list[str] | None = None,
    *,
    if_stale: bool = False,
    ctx: Context | None = None,
    capture_fixture: bool = False,
) -> tuple[dict, list[str]]:
    """Enumerate routes and merge them into the catalog under one exclusive lock.

    Returns `(document, summary_lines)`. A route failure is recorded AS THAT ROUTE'S STATUS and
    never aborts the others: one dead credential must not cost the host every other route's
    freshness. `capture_fixture` writes the `codex-oauth` route's raw stdout to
    `capture_fixture_path(host)` (T-3.2, Risk 8); the path is reported in the summary and never
    stored in the catalog.
    """
    context = Context() if ctx is None else ctx
    timeout_seconds = float(plan["refresh_timeout_seconds"])
    requested = list(route_ids) if route_ids else list(plan["routes"])
    _reject_unlisted_routes(plan, requested)

    summary = [_plan_summary_line(plan)]
    with _CatalogLock(lock_path(context.catalog), timeout_seconds):
        # Re-read INSIDE the lock (review F-1): a route-only refresh that merged into a snapshot
        # taken before the lock would erase whatever the hourly full refresh wrote while it
        # waited. The lock serialises the writers and this re-read is what makes the merge safe.
        routes = dict((read_catalog(context.catalog) or {}).get("routes") or {})
        for route in requested:
            if if_stale and _is_fresh(routes.get(route), plan, context.now):
                existing = routes[route]
                summary.append(
                    f"route={route} status=fresh models={len(existing.get('models') or [])} "
                    f"fetched_at={existing.get('fetched_at')} skipped=if-stale"
                )
                continue
            entry = _enumerate_one(
                plan, route, timeout_seconds, context.now, capture_fixture
            )
            captured = entry.pop("captured_fixture", None)
            routes[route] = entry
            summary.append(_route_summary_line(route, entry))
            if captured:
                summary.append(f"route={route} captured_fixture={captured}")

        document = {
            "schema_version": SCHEMA_VERSION,
            "host": plan["host"],
            "generated_at": iso(context.now),
            "routes": routes,
        }
        _atomic_write(context.catalog, document, context.environ)

    return document, summary


def _reject_unlisted_routes(plan: dict, requested: list[str]) -> None:
    unknown = [route for route in requested if route not in plan["routes"]]
    if unknown:
        raise CatalogError(
            f"{REFUSAL_PREFIX} route(s) {', '.join(unknown)} are not listed for host "
            f"{plan['host'] or '<unset>'} "
            f"({'from [defaults]' if plan['from_defaults'] else 'from its own entry'}); "
            f"this host enumerates: {', '.join(plan['routes']) or '(none)'}.",
            EXIT_USAGE,
        )


def _is_fresh(entry: dict | None, plan: dict, now: dt.datetime) -> bool:
    if not entry or entry.get("status") != "ok":
        return False
    hours = age_hours(entry.get("fetched_at"), now)
    return hours is not None and hours < plan["stale_after_hours"]


# --- the resolver -------------------------------------------------------------------------

#: Per-PROCESS catalog cache (AC-4). The file is the cache; this makes a process resolving
#: several families read it once. Nothing survives the process on purpose: a resolver holding a
#: catalog across runs is how it goes stale while reporting a live read.
_CATALOG_CACHE: dict[str, dict | None] = {}


def load_catalog_cached(target: Path) -> dict | None:
    key = str(target)
    if key not in _CATALOG_CACHE:
        _CATALOG_CACHE[key] = read_catalog(target)
    return _CATALOG_CACHE[key]


def invalidate_catalog_cache(target: Path | None = None) -> None:
    if target is None:
        _CATALOG_CACHE.clear()
    else:
        _CATALOG_CACHE.pop(str(target), None)


class _Refusal:
    """The five things every refusal names, gathered so each raise site states them once."""

    def __init__(
        self, family: str, route: str, catalog: Path, fetched_at: str | None
    ) -> None:
        self.family = family
        self.route = route
        self.catalog = catalog
        self.fetched_at = fetched_at

    def error(self, message: str, code: int) -> CatalogError:
        return CatalogError(
            f"{REFUSAL_PREFIX} {message} "
            f"[family={self.family} route={self.route} catalog={self.catalog} "
            f"fetched_at={self.fetched_at or 'absent'}]",
            code,
        )


def resolve(
    plan: dict,
    route: str,
    family: str,
    *,
    ctx: Context | None = None,
    allow_refresh: bool = True,
) -> str:
    """The newest id of `family` on `route`, or raise a CatalogError carrying the exit code."""
    context = Context() if ctx is None else ctx
    if route not in KNOWN_ROUTES:
        raise CatalogError(
            f"{REFUSAL_PREFIX} '{route}' is not a known route id; "
            f"known: {', '.join(KNOWN_ROUTES)}.",
            EXIT_USAGE,
        )

    document, entry, refresh_error = _entry_for(
        plan, route, family, context, allow_refresh
    )
    refusal = _Refusal(family, route, context.catalog, entry.get("fetched_at"))

    if entry.get("status") != "ok":
        raise refusal.error(
            f"route {route} is recorded as '{entry.get('status')}': {entry.get('error')}",
            EXIT_ROUTE_NOT_OK,
        )
    del document
    return _choose(entry, family, refusal, context)


def _entry_for(
    plan: dict, route: str, family: str, context: Context, allow_refresh: bool
) -> tuple[dict, dict, str | None]:
    """The route's catalog entry, refreshing once on a miss or on staleness (D-6)."""
    document = load_catalog_cached(context.catalog)
    refresh_error: str | None = None

    if document is None or route not in (document.get("routes") or {}):
        if allow_refresh:
            print(
                f"model-catalog: the catalog has no entry for route {route}; refreshing it once "
                f"(bounded at {plan['refresh_timeout_seconds']}s)",
                file=context.stderr,
            )
            refresh_error = _refresh_once(plan, route, context)
            invalidate_catalog_cache(context.catalog)
            document = load_catalog_cached(context.catalog)
        # A FAILED refresh-on-miss is exit 6, not exit 7. The refresher records a failed route as
        # an `error` ENTRY, so that entry now exists and is fresh -- without this check the
        # fall-through would reach the status test below and report "route not ok", losing the
        # fact that the catalog was MISSING and the one repair attempt failed. The AC fixes the
        # code: "If the catalog file is missing and the refresh-on-miss also fails, then the
        # resolver shall exit 6 and its stderr shall name the family, the route, the catalog path
        # and the refresh error."
        if (
            document is None
            or route not in (document.get("routes") or {})
            or refresh_error is not None
        ):
            raise _Refusal(family, route, context.catalog, None).error(
                f"the catalog has no entry for route {route} and the refresh-on-miss did not "
                f"produce one" + (f" ({refresh_error})" if refresh_error else ""),
                EXIT_CATALOG_MISSING,
            )

    entry = (document.get("routes") or {})[route]
    hours = age_hours(entry.get("fetched_at"), context.now)
    if hours is None or hours >= plan["stale_after_hours"]:
        # The age AS MEASURED BEFORE the repair attempt, kept for the refusal message. A failed
        # refresh still writes an `error` entry with a NEW fetched_at, so re-reading the age after
        # it would report ~0 hours and the refusal would claim the catalog was fresh.
        stale_hours = hours
        stale_fetched_at = entry.get("fetched_at")
        if allow_refresh:
            print(
                f"model-catalog: refreshing stale catalog for route {route} "
                f"(age {'unknown' if hours is None else format(hours, '.1f') + 'h'}, "
                f"window {plan['stale_after_hours']}h)",
                file=context.stderr,
            )
            refresh_error = _refresh_once(plan, route, context)
            invalidate_catalog_cache(context.catalog)
            document = load_catalog_cached(context.catalog) or document
            entry = (document.get("routes") or {}).get(route, entry)
            hours = age_hours(entry.get("fetched_at"), context.now)
        # Same reasoning as the missing-catalog branch above: a FAILED refresh is exit 5 naming
        # the age, never exit 7 about the error entry the failure just wrote.
        if (
            refresh_error is not None
            or hours is None
            or hours >= plan["stale_after_hours"]
        ):
            raise _Refusal(family, route, context.catalog, stale_fetched_at).error(
                f"route {route}'s entry is "
                + (
                    "undated"
                    if stale_hours is None
                    else f"{stale_hours:.1f} hours old, past the "
                    f"{plan['stale_after_hours']}-hour staleness window"
                )
                + ", and the refresh-on-miss did not produce a fresher one"
                + (f" ({refresh_error})" if refresh_error else ""),
                EXIT_STALE,
            )
    return document, entry, refresh_error


def _choose(entry: dict, family: str, refusal: _Refusal, context: Context) -> str:
    models = entry.get("models") or []
    candidates = [model for model in models if model.get("family") == family]
    if not candidates:
        listed = (
            ", ".join(sorted(str(model.get("id")) for model in models)) or "(nothing)"
        )
        raise refusal.error(
            f"route {refusal.route} advertises no deployment in the '{family}' family. "
            f"It lists: {listed}",
            EXIT_FAMILY_ABSENT,
        )

    unparsable = sorted(
        str(model.get("id")) for model in candidates if model.get("version") is None
    )
    if unparsable:
        raise refusal.error(
            f"route {refusal.route} lists '{family}' deployment(s) whose version segment does "
            f"not parse as a number ({', '.join(unparsable)}), so 'latest' is undefined for this "
            f"family. This refuses rather than guessing an ordering",
            EXIT_UNPARSABLE_VERSION,
        )

    # D-10: listed is not callable. `available` is absent on routes that record no availability
    # (the gateway has no such API), and absent must read as "not filtered", not as "unavailable".
    available = [model for model in candidates if model.get("available", True)]
    if not available:
        detail = ", ".join(
            f"{model.get('id')} ({model.get('unavailable_reason') or 'no reason recorded'})"
            for model in sorted(candidates, key=lambda model: str(model.get("id")))
        )
        raise refusal.error(
            f"route {refusal.route} lists '{family}' deployment(s) but the provider reports none "
            f"of them as available to this account: {detail}",
            EXIT_FAMILY_ABSENT,
        )

    chosen = latest(available)
    newest_listed = latest(candidates)
    if newest_listed.get("id") != chosen.get("id"):
        # Loud, never silent: the newest id the route LISTS is not the one being returned, and a
        # caller debugging "why did I not get 5.5" needs to see that at the moment it happens.
        print(
            f"skipped={newest_listed.get('id')} "
            f"reason={newest_listed.get('unavailable_reason') or 'unavailable'}",
            file=context.stderr,
        )
    return str(chosen["id"])


def _refresh_once(plan: dict, route: str, context: Context) -> str | None:
    """One bounded refresh of a single route. Returns an error string, or None on success."""
    try:
        document, _summary = refresh(plan, [route], ctx=context)
    except CatalogError as exc:
        return exc.message
    entry = (document.get("routes") or {}).get(route) or {}
    if entry.get("status") != "ok":
        return f"the refresh recorded route {route} as '{entry.get('status')}': {entry.get('error')}"
    return None


# --- CLI ---------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="model-catalog",
        description=(
            "Record and read which model ids each route this host can use actually serves "
            "(ISSUE-REF)."
        ),
    )
    parser.add_argument(
        "--route-table",
        type=Path,
        default=None,
        help="path to the route table (default: the tracked config/model-catalog-routes.toml)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    refresh_parser = subparsers.add_parser(
        "refresh", help="enumerate this host's routes and record what each one serves"
    )
    refresh_parser.add_argument(
        "--route",
        action="append",
        dest="routes",
        default=None,
        help="refresh only this route (repeatable; default: every route this host lists)",
    )
    refresh_parser.add_argument(
        "--if-stale",
        action="store_true",
        help="skip a route whose recorded entry is still inside the staleness window",
    )
    refresh_parser.add_argument(
        "--host", default=None, help="override the detected AI_HOST"
    )
    refresh_parser.add_argument(
        "--capture-fixture",
        action="store_true",
        help=(
            "write the codex-oauth route's raw `codex debug models` stdout to "
            "tests/fixtures/model_catalog/codex-debug-models.<AI_HOST>.txt in this checkout"
        ),
    )

    host_config_parser = subparsers.add_parser(
        "host-config",
        help="print this host's route-table plan, scheduler and catalog path on one line",
    )
    host_config_parser.add_argument(
        "--host", default=None, help="override the detected AI_HOST"
    )
    host_config_parser.add_argument(
        "--platform",
        default=None,
        help="the `uname -s` value whose scheduler to report (default: this platform)",
    )

    resolve_parser = subparsers.add_parser(
        "resolve", help="print the newest id of a family on a route"
    )
    resolve_parser.add_argument("--route", required=True)
    resolve_parser.add_argument("--family", required=True)
    resolve_parser.add_argument(
        "--lane",
        default=None,
        help="accepted for the caller's symmetry; the route table decides the lane",
    )
    resolve_parser.add_argument(
        "--host", default=None, help="override the detected AI_HOST"
    )
    resolve_parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="refuse on a missing or stale catalog instead of refreshing it once (D-6)",
    )

    show_parser = subparsers.add_parser(
        "show",
        help="print each route's status, model count and age, and the catalog path",
    )
    show_parser.add_argument(
        "--host", default=None, help="override the detected AI_HOST"
    )
    show_parser.add_argument(
        "--route",
        default=None,
        help="with --brief: the route whose entry to summarise",
    )
    show_parser.add_argument(
        "--brief",
        action="store_true",
        help="print one line, `catalog=<path> age=<n>m status=<s>`, for --route (cx preflight)",
    )

    return parser


def capture_fixture_path(host: str) -> Path:
    """Where `refresh --capture-fixture` writes the OAuth route's raw output for this host."""
    if not _CAPTURE_HOST.match(host or ""):
        raise CatalogError(
            f"{REFUSAL_PREFIX} --capture-fixture needs a plain AI_HOST to name the fixture "
            f"file, and this host resolved to {host!r}.",
            EXIT_USAGE,
        )
    return FIXTURE_CAPTURE_DIR / f"codex-debug-models.{host}.txt"


def _command_refresh(args: argparse.Namespace, plan: dict, ctx: Context) -> int:
    capture = capture_fixture_path(plan["host"]) if args.capture_fixture else None
    document, summary = refresh(
        plan,
        args.routes,
        if_stale=args.if_stale,
        ctx=ctx,
        capture_fixture=capture is not None,
    )
    for line in summary:
        print(line)
    print(f"catalog={ctx.catalog}")
    if capture is not None and not any("captured_fixture=" in s for s in summary):
        print(
            f"capture_fixture=none: route {ROUTE_CODEX_OAUTH} was not enumerated live with "
            f"output on this host, so {capture} was not written"
        )
    failed_entries = {
        route: entry
        for route, entry in (document.get("routes") or {}).items()
        if entry.get("status") in ("error", STATUS_PROXY_UNREACHABLE)
        and (not args.routes or route in args.routes)
    }
    failed = sorted(failed_entries)
    if failed and all(
        entry["status"] == STATUS_PROXY_UNREACHABLE for entry in failed_entries.values()
    ):
        proxies = sorted(
            {entry.get("proxy") or "?" for entry in failed_entries.values()}
        )
        print(
            f"{REFUSAL_PREFIX} route(s) {', '.join(failed)} could not reach this host's SOCKS "
            f"proxy {', '.join(proxies)}: the tunnel is down. Their entries record "
            f"`{STATUS_PROXY_UNREACHABLE}` and every other route was still written.",
            file=sys.stderr,
        )
        return EXIT_PROXY_UNREACHABLE
    if failed:
        print(
            f"{REFUSAL_PREFIX} route(s) {', '.join(failed)} failed to enumerate; their entries "
            f"record the error and every other route was still written.",
            file=sys.stderr,
        )
        return EXIT_ROUTE_NOT_OK
    return EXIT_OK


def _command_resolve(args: argparse.Namespace, plan: dict, ctx: Context) -> int:
    resolved = resolve(
        plan, args.route, args.family, ctx=ctx, allow_refresh=not args.no_refresh
    )
    print(f"family={args.family} resolved={resolved} requested={args.family}")
    return EXIT_OK


def _command_show_brief(route: str | None, ctx: Context) -> int:
    """One machine-readable line for `cx preflight --route-only` (T-4.1).

    `age=` is in whole minutes and `status=` is the route entry's own recorded status, or
    `absent` (no catalog file) / `missing` (the file has no entry for the route). It reads the
    file only: a preflight that refreshed would report the age of a read it just caused.
    """
    if not route:
        raise CatalogError(
            f"{REFUSAL_PREFIX} `show --brief` needs --route.", EXIT_USAGE
        )
    document = read_catalog(ctx.catalog)
    if document is None:
        print(f"catalog={ctx.catalog} age=unknown status=absent")
        return EXIT_CATALOG_MISSING
    entry = (document.get("routes") or {}).get(route)
    if entry is None:
        print(f"catalog={ctx.catalog} age=unknown status=missing")
        return EXIT_CATALOG_MISSING
    hours = age_hours(entry.get("fetched_at"), ctx.now)
    age = "unknown" if hours is None else f"{int(hours * 60)}m"
    print(f"catalog={ctx.catalog} age={age} status={entry.get('status')}")
    return EXIT_OK


def _command_show(args: argparse.Namespace, plan: dict, ctx: Context) -> int:
    """The D-1 visibility mitigation: an untracked file is still inspectable in one command."""
    if args.brief:
        return _command_show_brief(args.route, ctx)
    print(f"catalog={ctx.catalog}")
    print(
        f"host={plan['host'] or '<unset>'} "
        f"from_defaults={'true' if plan['from_defaults'] else 'false'} "
        f"routes={','.join(plan['routes']) or '(none)'} "
        f"stale_after_hours={plan['stale_after_hours']}"
    )
    document = read_catalog(ctx.catalog)
    if document is None:
        print("catalog=absent (run `model-catalog refresh`)")
        return EXIT_CATALOG_MISSING
    print(f"generated_at={document.get('generated_at')}")
    for route, entry in sorted((document.get("routes") or {}).items()):
        hours = age_hours(entry.get("fetched_at"), ctx.now)
        print(
            f"route={route} status={entry.get('status')} "
            f"models={len(entry.get('models') or [])} "
            f"fetched_at={entry.get('fetched_at')} "
            f"age_hours={'unknown' if hours is None else format(hours, '.1f')}"
            + (f" error={entry.get('error')}" if entry.get("error") else "")
        )
    return EXIT_OK


def _command_host_config(args: argparse.Namespace, plan: dict, ctx: Context) -> int:
    """One parseable line for the installers, read through this module's own loader.

    `aws_profile` is printed as the table holds it: a literal name, the `derive` selector, or
    `-` for none. The systemd installer renders `AWS_PROFILE` only for a literal, because a
    derived name baked into a unit file is exactly the stale copy ISSUE-REF removed.
    """
    platform_name = args.platform or platform.system()
    print(
        f"host={plan['host'] or '<unset>'} "
        f"from_defaults={'true' if plan['from_defaults'] else 'false'} "
        f"routes={','.join(plan['routes']) or '(none)'} "
        f"aws_profile={plan['aws_profile'] or '-'} "
        f"scheduler={scheduler_for(plan, platform_name)} "
        f"catalog={ctx.catalog}"
    )
    return EXIT_OK


COMMANDS = {
    "refresh": _command_refresh,
    "host-config": _command_host_config,
    "resolve": _command_resolve,
    "show": _command_show,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ctx = Context()
    try:
        table = load_route_table(args.route_table)
        host = args.host if args.host else resolve_host(ctx.environ)
        return COMMANDS[args.command](args, host_plan(table, host), ctx)
    except CatalogError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    except CrossGrammarComparison as exc:
        print(f"{REFUSAL_PREFIX} {exc}", file=sys.stderr)
        return EXIT_USAGE


if (
    __name__ == "__main__"
):  # pragma: no cover - exercised through the wrapper and main()
    sys.exit(main())
