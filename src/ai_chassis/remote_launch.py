"""Hardened, config-gated cross-machine session launch (issue-ref D-20, T-4.2; ISSUE-REF).

An allowlisted session asks its own or its peer machine's notifier daemon to run `ai <engine>
<name>` in an allowlisted repo. The request is a closed-schema JSON envelope, signed by the
requester's Buzz session key and carried only on a rung the target's overlay accepts; this module
decides whether to honour it and, in exactly one branch, runs it.

Shape, so the dangerous call is unreachable unless every guard passed:

* `load_remote_launch_config` -- the per-machine `[remote_launch]` overlay table. Absent means
  OFF. An unknown key or a malformed value turns the WHOLE table OFF, naming the key (T6).
* `parse_request` -- the closed envelope. There is no free-text field, so "run this shell line"
  or an arbitrary goal cannot be expressed at all (T3).
* `decide` -- pure: the ordered checks, ending in `dry-run` or `accepted`. An argv is built only
  on those two outcomes, never for a refusal.
* `execute` -- the ONE impure call. It refuses anything but an `accepted` decision itself, so a
  caller that forgot to check still cannot start a process.
* `handle_event` -- the daemon-facing entry: load, parse, decide, audit, execute-if-accepted.

Every request appends to an append-only audit log; detached restarts have started and
completion records. Shipping
this OFF changes nothing on any machine; turning it on is the maintainer's (ISSUE-REF).

Git-inbox carries only a signed launch event encrypted to this daemon and resolved through the
Buzz directory. The untrusted git-inbox sender field never authorizes a launch.

Five verbs share the envelope and every check (D-21, ISSUE-REF; ISSUE-REF OQ-3): `launch`
runs `ai <engine> <name>`; `inject-goal` runs the target's own `start_goal.py start --target
session:<slug>`; `restart` signals an existing pane through `ai --restart-session <slug>`;
`pause-goal` and `rearm-goal` call the target host's goal-waker adapters directly. The requester
side records what it sent (`record_request`) and every reply it receives (`record_result`),
matching a reply only to its own request.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_chassis.session_address import _NAME, session_address

SECTION = "remote_launch"
ALLOW_SECTION = "allow"

DECISION_ACCEPTED = "accepted"
DECISION_REFUSED = "refused"
DECISION_DRY_RUN = "dry-run"

REASON_DISABLED = "disabled"
REASON_CROSS_PAIR = "cross-pair-not-admitted"
REASON_RUNG = "rung-not-accepted"
REASON_SENDER = "sender-not-allowed"
REASON_SCHEMA = "schema-invalid"
REASON_EXPIRED = "expired"
REASON_REPLAYED = "replayed"
REASON_VERB = "verb-not-allowed"
REASON_ENGINE = "engine-not-allowed"
REASON_REPO = "repo-not-allowed"
REASON_NAME = "name-not-slug"
REASON_GOAL_DOC = "goal-doc-not-tracked"
REASON_TARGET = "target-not-on-machine"
REASON_TEMPLATE = "template-not-allowed"
REASON_PARAM = "param-invalid"
REASON_LIBRARY = "library-absent"
REASON_SESSION = "session-not-local"
REASON_SESSION_INVENTORY = "session-inventory-unavailable"
REASON_CONCURRENCY = "concurrency-capped"
REASON_RATE = "rate-capped"
REASON_DRY_RUN = "dry_run = true"
REASON_ACCEPTED = "all checks passed"

SCHEMA_VERSION = 1

#: Values the overlay may list. Anything else is a malformed value and turns the table OFF.
#: Git-inbox accepts only events signed, encrypted to this daemon and named by the Buzz
#: directory. Its unauthenticated FROM header is never the launch sender being judged.
KNOWN_RUNGS = frozenset({"buzz", "agent-mail", "git-inbox"})
VERB_LAUNCH = "launch"
#: Start a goal in a session on the target through its local `/start-goal` (D-21, ISSUE-REF).
VERB_INJECT_GOAL = "inject-goal"
VERB_RESTART = "restart"
VERB_PAUSE_GOAL = "pause-goal"
VERB_REARM_GOAL = "rearm-goal"
#: What an overlay MAY list. The shipped default allows only `launch`: listing `inject-goal`
#: or either quota goal-control verb is the operator's act, so shipping the verbs changes
#: nothing on any machine.
KNOWN_VERBS = frozenset(
    {
        VERB_LAUNCH,
        VERB_INJECT_GOAL,
        VERB_RESTART,
        VERB_PAUSE_GOAL,
        VERB_REARM_GOAL,
    }
)
#: `ai c`, `ai cx`, `ai p`, `ai g`. Gemini is legal and lowest priority.
KNOWN_ENGINES = frozenset({"c", "cx", "p", "g"})

DEFAULT_ACCEPT_RUNGS = ("buzz",)
DEFAULT_REQUEST_TTL_S = 120
DEFAULT_MAX_CONCURRENT = 1
DEFAULT_PER_SENDER_PER_HOUR = 6
DEFAULT_RESTART_MESSAGE_MAX_CHARS = 700
# Includes the full 64-character request id and the longest completion verdict.
RESTART_NOTICE_MAX_CHARS = 96
RESTART_NOTICE_SEPARATOR = " | "

SECONDS_PER_HOUR = 3600
#: An `issued_at` this far ahead of the receiver's clock is not a skew, it is a forged stamp
#: buying a longer life. The same +/- 60 s Buzz documents for an AUTH event.
CLOCK_SKEW_S = 60
#: A launch that has not returned by now is reported as timed out; the daemon's tick is bounded.
LAUNCH_TIMEOUT_S = 60
#: `start_goal.py start` waits up to its own `landing_timeout_seconds` (90 s shipped) for the
#: transcript landing, plus `uv run --script` start-up.
INJECT_GOAL_TIMEOUT_S = 150

#: A session name becomes an argv element, so it must never look like an option (`-R`).
NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,47}\Z")
#: A repo is a basename under the projects root: no separator, no `..`, no leading dot.
REPO_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
#: A concrete `<session>@<host>` (ISSUE-REF): a requester, a sender, a target.
IDENTITY_RE = re.compile(rf"\A{_NAME}@{_NAME}\Z")
REQUEST_ID_RE = re.compile(r"\A[A-Za-z0-9-]{8,64}\Z")
GOAL_DOC_PREFIX = "docs/prompts/goal/"
#: `daemon@<machine>` is the target daemon's own identity; a session must never claim it.
RESERVED_NAMES = frozenset({"daemon"})

#: The start-goal stores (`config/start-goal.toml` `[stores]`): an `inject-goal` goal_doc must be
#: a tracked file under one of them, and a template name must resolve to one under the first.
GOAL_TEMPLATES_PREFIX = "docs/prompts/goal/templates/"
GOAL_DRAFTS_PREFIX = "docs/prompts/goal/drafts/"
GOAL_REGISTRY = "docs/prompts/goal/templates/registry.toml"
#: `inject-goal` runs in the ai-harness MAIN tree on the target, whatever `repos` allows.
HARNESS_REPO = "ai-harness"
START_GOAL_SCRIPT = "skills/start-goal/scripts/start_goal.py"
#: The shared injection library start-goal delivers through (ISSUE-REF). Absent = refused.
INJECTION_LIBRARY = "src/ai_chassis/prompt_injection/__init__.py"
TEMPLATE_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")
DISPATCH_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z")
PARAM_KEY_RE = re.compile(r"\A[a-z][a-z0-9_-]{0,31}\Z")
PARAM_VALUE_MAX = 500
PARAMS_MAX = 16
GOAL_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")

AUDIT_FILE = "audit.jsonl"
#: Requester side: one line per request this machine SENT, and one per reply it received.
REQUESTS_FILE = "requests.jsonl"
RESULTS_FILE = "results.jsonl"
IN_FLIGHT_DIR = "in-flight"
PAUSED_GOALS_DIR = "paused-goals"
DIR_MODE = 0o700
FILE_MODE = 0o600

_TOP_KEYS = frozenset(
    {
        "enabled",
        "dry_run",
        "accept_rungs",
        "request_ttl_s",
        "git_inbox_request_ttl_s",
        "restart_message_max_chars",
        "max_concurrent",
        "per_sender_per_hour",
        ALLOW_SECTION,
        "cross_pair",
    }
)
_ALLOW_KEYS = frozenset({"senders", "verbs", "engines", "repos"})

#: Every verb's envelope carries these; `parse_request` checks them.
_COMMON_FIELDS = (
    "schema_version",
    "request_id",
    "requester",
    "target_machine",
    "verb",
    "issued_at",
    "expires_at",
)
#: Per verb: (required, optional). The closed schema is the union; a field of the OTHER verb is
#: refused by `decide` once the verb itself is allowed, so a disallowed verb is always reported
#: `verb-not-allowed` whatever else its envelope carries.
_VERB_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    VERB_LAUNCH: (("engine", "repo", "name"), ("goal_doc",)),
    VERB_RESTART: (("target",), ()),
    VERB_INJECT_GOAL: (
        ("target", "dispatch_id"),
        ("template_name", "goal_doc", "params"),
    ),
    VERB_PAUSE_GOAL: (("target",), ()),
    VERB_REARM_GOAL: (("target", "goal_digest"), ()),
}
_REQUIRED_FIELDS = _COMMON_FIELDS + _VERB_FIELDS[VERB_LAUNCH][0]
_ALL_FIELDS = frozenset(_COMMON_FIELDS).union(
    *(set(required) | set(optional) for required, optional in _VERB_FIELDS.values())
)
_STRING_FIELDS = _ALL_FIELDS - {"schema_version", "params"}


# ---------------------------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteLaunchConfig:
    """The validated `[remote_launch]` table. `disabled_reason` says why it is OFF, when it is."""

    enabled: bool = False
    dry_run: bool = True
    accept_rungs: tuple[str, ...] = DEFAULT_ACCEPT_RUNGS
    request_ttl_s: int = DEFAULT_REQUEST_TTL_S
    git_inbox_request_ttl_s: int = 600
    restart_message_max_chars: int = DEFAULT_RESTART_MESSAGE_MAX_CHARS
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    per_sender_per_hour: int = DEFAULT_PER_SENDER_PER_HOUR
    senders: tuple[str, ...] = ()
    verbs: tuple[str, ...] = ()
    engines: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()
    disabled_reason: str = ""
    source: str = "default"
    cross_pair: tuple[CrossPairPolicy, ...] = ()

    def as_record(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "accept_rungs": list(self.accept_rungs),
            "request_ttl_s": self.request_ttl_s,
            "git_inbox_request_ttl_s": self.git_inbox_request_ttl_s,
            "restart_message_max_chars": self.restart_message_max_chars,
            "max_concurrent": self.max_concurrent,
            "per_sender_per_hour": self.per_sender_per_hour,
            "allow": {
                "senders": list(self.senders),
                "verbs": list(self.verbs),
                "engines": list(self.engines),
                "repos": list(self.repos),
            },
            "cross_pair": {
                policy.name: policy.as_record() for policy in self.cross_pair
            },
            "disabled_reason": self.disabled_reason or None,
            "source": self.source,
        }


@dataclass(frozen=True)
class CrossPairPolicy:
    name: str
    from_pair: str
    to_machines: tuple[str, ...]
    config: RemoteLaunchConfig

    def as_record(self) -> dict[str, Any]:
        record = self.config.as_record()
        return {
            key: record[key] for key in ("enabled", "dry_run", "accept_rungs", "allow")
        } | {"from_pair": self.from_pair, "to_machines": list(self.to_machines)}


def _pairs() -> dict[str, Any]:
    from ai_chassis import message_transports

    try:
        return message_transports.load_tracked_config().get("pairs", {})
    except (OSError, ValueError, message_transports.TransportConfigError):
        return {}


def machine_pair(machine: str) -> str:
    """Call-time transport membership; an unknown machine has no trusted pair."""
    return next(
        (name for name, pair in _pairs().items() if machine in pair["members"]), ""
    )


def is_cross_pair(sender: str, machine: str) -> bool:
    source = sender.partition("@")[2]
    if source == machine and source:
        return False
    pair = machine_pair(machine)
    return not pair or machine_pair(source) != pair


def cross_pair_policy(
    config: RemoteLaunchConfig, request: LaunchRequest, *, sender: str, rung: str
) -> CrossPairPolicy | None:
    """Match one entire tuple; validation already binds each sender to its source pair."""
    if not config.enabled or rung != "git-inbox":
        return None
    for policy in config.cross_pair:
        allowed = policy.config
        if (
            allowed.enabled
            and request.target_machine in policy.to_machines
            and rung in allowed.accept_rungs
            and sender in allowed.senders
            and request.verb in allowed.verbs
            and request.engine in allowed.engines
            and request.repo in allowed.repos
        ):
            return policy
    return None


def _cross_pair_policies(section: Mapping[str, Any]) -> tuple[CrossPairPolicy, ...]:
    """Closed policy schema. One malformed table disables the entire launch configuration."""
    raw = section.get("cross_pair", {})
    if not isinstance(raw, dict):
        raise ValueError("malformed:remote_launch.cross_pair")
    policies = []
    pairs = _pairs() if raw else {}
    keys = {"enabled", "dry_run", "accept_rungs", "from_pair", "to_machines", "allow"}
    for name, table in raw.items():
        prefix = f"remote_launch.cross_pair.{name}"
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or not isinstance(
            table, dict
        ):
            raise ValueError(f"malformed:{prefix}")
        if unknown := sorted(set(table) - keys):
            raise ValueError(f"unknown-key:{prefix}.{unknown[0]}")
        source = table.get("from_pair")
        targets = table.get("to_machines")
        if not isinstance(source, str) or source not in pairs:
            raise ValueError(f"malformed:{prefix}.from_pair")
        if not _string_list(targets) or any(
            not any(target in pair["members"] for pair in pairs.values())
            or target in pairs[source]["members"]
            for target in targets
        ):
            raise ValueError(f"malformed:{prefix}.to_machines")
        values = {
            key: value
            for key, value in table.items()
            if key not in {"from_pair", "to_machines"}
        }
        # Buzz can never become a cross-pair transport, even through an edited policy.
        if not _string_list(
            values.get("accept_rungs", []), allowed=frozenset({"git-inbox"})
        ):
            raise ValueError(f"malformed:{prefix}.accept_rungs")
        config = load_remote_launch_config({SECTION: values})
        if config.disabled_reason and config.disabled_reason != "enabled is not true":
            raise ValueError(config.disabled_reason.replace(SECTION, prefix, 1))
        if any(
            sender.partition("@")[2] not in pairs[source]["members"]
            for sender in config.senders
        ):
            raise ValueError(f"malformed:{prefix}.allow.senders")
        policies.append(CrossPairPolicy(name, source, tuple(targets), config))
    return tuple(policies)


def _off(reason: str, source: str) -> RemoteLaunchConfig:
    return RemoteLaunchConfig(enabled=False, disabled_reason=reason, source=source)


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _restart_message_bound_valid(value: object) -> bool:
    from ai_chassis.prompt_injection.config import load_config

    return _is_positive_int(value) and value < (
        load_config()["max_typed_chars"]
        - RESTART_NOTICE_MAX_CHARS
        - len(RESTART_NOTICE_SEPARATOR)
    )


def restart_message_refusal(message: str | None, config: RemoteLaunchConfig) -> str:
    """The same call-time capacity guard before signing and sidecar execution."""
    if message is None:
        return ""
    invalid = f"malformed:{SECTION}.restart_message_max_chars"
    if config.disabled_reason == invalid or not _restart_message_bound_valid(
        config.restart_message_max_chars
    ):
        return invalid
    if len(message) > config.restart_message_max_chars:
        return (
            f"restart-message-too-long (length={len(message)}, "
            f"max={config.restart_message_max_chars})"
        )
    return ""


def _string_list(
    value: object, *, allowed: frozenset[str] | None = None, pattern=None
) -> bool:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return False
    if allowed is not None and not set(value) <= allowed:
        return False
    return pattern is None or all(pattern.match(item) for item in value)


_ALLOW_RULES: dict[str, dict[str, Any]] = {
    "senders": {"pattern": IDENTITY_RE},
    "verbs": {"allowed": KNOWN_VERBS},
    "engines": {"allowed": KNOWN_ENGINES},
    "repos": {"pattern": REPO_RE},
}


def load_remote_launch_config(  # noqa: C901, PLR0911  # one return per named fail-closed key
    tables: Mapping[str, Any],
) -> RemoteLaunchConfig:
    """`[remote_launch]` + `[remote_launch.allow]` from the shared loader's merged tables.

    `tables` is what `agent_mail_notifier.read_config_tables` returns (tracked TOML with the
    operator overlay deep-merged), or `read_tables()`'s `{"_config_error": ...}` when the overlay
    could not be parsed. Fails CLOSED: anything this function does not positively recognise turns
    the whole table off and names the key, because a typo that silently widened an allowlist is
    the misconfiguration T6 exists for.
    """
    source = str(tables.get("_config_source") or "default")
    error = tables.get("_config_error")
    if error:
        return _off(f"config-unreadable ({error})", source)
    section = tables.get(SECTION)
    if section is None:
        return _off("absent", source)
    if not isinstance(section, dict):
        return _off(f"malformed:{SECTION}", source)
    for key in sorted(section):
        if key not in _TOP_KEYS:
            return _off(f"unknown-key:{SECTION}.{key}", source)

    checks = (
        ("enabled", lambda v: isinstance(v, bool)),
        ("dry_run", lambda v: isinstance(v, bool)),
        ("accept_rungs", lambda v: _string_list(v, allowed=KNOWN_RUNGS)),
        ("request_ttl_s", _is_positive_int),
        ("git_inbox_request_ttl_s", _is_positive_int),
        ("restart_message_max_chars", _restart_message_bound_valid),
        ("max_concurrent", _is_positive_int),
        ("per_sender_per_hour", _is_positive_int),
    )
    for key, ok in checks:
        if key in section and not ok(section[key]):
            return _off(f"malformed:{SECTION}.{key}", source)

    allow = section.get(ALLOW_SECTION, {})
    if not isinstance(allow, dict):
        return _off(f"malformed:{SECTION}.{ALLOW_SECTION}", source)
    for key in sorted(allow):
        if key not in _ALLOW_KEYS:
            return _off(f"unknown-key:{SECTION}.{ALLOW_SECTION}.{key}", source)
        if not _string_list(allow[key], **_ALLOW_RULES[key]):
            return _off(f"malformed:{SECTION}.{ALLOW_SECTION}.{key}", source)

    try:
        policies = _cross_pair_policies(section)
    except ValueError as exc:
        return _off(str(exc), source)
    enabled = section.get("enabled") is True
    return RemoteLaunchConfig(
        enabled=enabled,
        dry_run=section.get("dry_run", True),
        accept_rungs=tuple(section.get("accept_rungs", DEFAULT_ACCEPT_RUNGS)),
        request_ttl_s=section.get("request_ttl_s", DEFAULT_REQUEST_TTL_S),
        git_inbox_request_ttl_s=section.get("git_inbox_request_ttl_s", 600),
        restart_message_max_chars=section.get(
            "restart_message_max_chars", DEFAULT_RESTART_MESSAGE_MAX_CHARS
        ),
        max_concurrent=section.get("max_concurrent", DEFAULT_MAX_CONCURRENT),
        per_sender_per_hour=section.get(
            "per_sender_per_hour", DEFAULT_PER_SENDER_PER_HOUR
        ),
        senders=tuple(allow.get("senders", ())),
        verbs=tuple(allow.get("verbs", ())),
        engines=tuple(allow.get("engines", ())),
        repos=tuple(allow.get("repos", ())),
        disabled_reason="" if enabled else "enabled is not true",
        source=source,
        cross_pair=policies,
    )


def read_tables(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The shared loader's tables, read at CALL time; an unreadable overlay is a reported state."""
    from ai_chassis.agent_mail_notifier import read_config_tables

    try:
        return read_config_tables(env=dict(env) if env is not None else None)
    except RuntimeError as exc:
        return {"_config_error": str(exc)}


# ---------------------------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchRequest:
    """One parsed envelope. A field absent from the envelope is None (`params`: absent = None).

    `present` names the verb-specific fields the envelope actually carried, so `decide` can
    refuse a field of the other verb without guessing from a None.
    """

    schema_version: int
    request_id: str
    requester: str
    target_machine: str
    verb: str
    issued_at: str
    expires_at: str
    engine: str | None = None
    repo: str | None = None
    name: str | None = None
    goal_doc: str | None = None
    target: str | None = None
    dispatch_id: str | None = None
    template_name: str | None = None
    goal_digest: str | None = None
    params: dict[str, str] | None = None
    present: frozenset[str] = frozenset()

    def as_content(self) -> str:
        document: dict[str, Any] = {key: getattr(self, key) for key in _COMMON_FIELDS}
        for key in sorted(self.present):
            document[key] = getattr(self, key)
        return json.dumps(document, sort_keys=True)


@dataclass(frozen=True)
class RequestError:
    """Why an envelope did not parse. `field` is what `schema-invalid:<field>` names."""

    field: str
    request_id: str | None = None

    @property
    def reason(self) -> str:
        return f"{REASON_SCHEMA}:{self.field}"


def parse_iso(value: str) -> int | None:
    """Epoch seconds for a `YYYY-MM-DDTHH:MM:SSZ` stamp, or None."""
    try:
        moment = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    return int(moment.replace(tzinfo=dt.timezone.utc).timestamp())


def iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def parse_request(  # noqa: C901, PLR0911, PLR0912  # one return per named schema refusal
    content: str,
) -> LaunchRequest | RequestError:
    """The closed envelope, or the first field that broke it. Never raises."""
    try:
        document = json.loads(content)
    except (TypeError, ValueError):
        return RequestError("body")
    if not isinstance(document, dict):
        return RequestError("body")
    raw_id = document.get("request_id")
    request_id = (
        raw_id if isinstance(raw_id, str) and REQUEST_ID_RE.match(raw_id) else None
    )
    for key in sorted(document):
        if key not in _ALL_FIELDS:
            return RequestError(str(key)[:64], request_id)
    for key in _COMMON_FIELDS:
        if key not in document:
            return RequestError(key, request_id)
    version = document["schema_version"]
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        return RequestError("schema_version", request_id)
    for key in sorted(_STRING_FIELDS & set(document)):
        if not isinstance(document[key], str):
            return RequestError(key, request_id)
    params = document.get("params")
    if "params" in document and not (
        isinstance(params, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in params.items())
    ):
        return RequestError("params", request_id)
    if request_id is None:
        return RequestError("request_id")
    if not IDENTITY_RE.match(document["requester"]):
        return RequestError("requester", request_id)
    for key in ("issued_at", "expires_at"):
        if parse_iso(document[key]) is None:
            return RequestError(key, request_id)
    present = frozenset(document) - set(_COMMON_FIELDS)
    return LaunchRequest(
        schema_version=SCHEMA_VERSION,
        request_id=request_id,
        requester=document["requester"],
        target_machine=document["target_machine"],
        verb=document["verb"],
        issued_at=document["issued_at"],
        expires_at=document["expires_at"],
        engine=document.get("engine"),
        repo=document.get("repo"),
        name=document.get("name"),
        goal_doc=document.get("goal_doc"),
        target=document.get("target"),
        dispatch_id=document.get("dispatch_id"),
        template_name=document.get("template_name"),
        goal_digest=document.get("goal_digest"),
        params=dict(params) if isinstance(params, dict) else None,
        present=present,
    )


def _peek_request_id(content: str) -> str | None:
    try:
        document = json.loads(content)
    except (TypeError, ValueError):
        return None
    raw = document.get("request_id") if isinstance(document, dict) else None
    return raw if isinstance(raw, str) and REQUEST_ID_RE.match(raw) else None


def compose_request(  # noqa: PLR0913  # one keyword per envelope field
    *,
    request_id: str,
    requester: str,
    target_machine: str,
    engine: str,
    repo: str,
    name: str,
    now: int,
    ttl_s: int = DEFAULT_REQUEST_TTL_S,
    goal_doc: str | None = None,
    verb: str = "launch",
) -> LaunchRequest:
    """The envelope the requester sends. The receiver re-validates every field regardless."""
    present = {"engine", "repo", "name"} | (
        {"goal_doc"} if goal_doc is not None else set()
    )
    return LaunchRequest(
        schema_version=SCHEMA_VERSION,
        request_id=request_id,
        requester=requester,
        target_machine=target_machine,
        verb=verb,
        engine=engine,
        repo=repo,
        name=name,
        issued_at=iso(now),
        expires_at=iso(now + ttl_s),
        goal_doc=goal_doc,
        present=frozenset(present),
    )


def compose_restart(  # noqa: PLR0913 - one keyword per envelope field
    *,
    request_id: str,
    requester: str,
    target_machine: str,
    target: str,
    now: int,
    ttl_s: int = DEFAULT_REQUEST_TTL_S,
) -> LaunchRequest:
    """Restart one existing session; no command, engine or path crosses the wire."""
    return LaunchRequest(
        schema_version=SCHEMA_VERSION,
        request_id=request_id,
        requester=requester,
        target_machine=target_machine,
        verb=VERB_RESTART,
        target=target,
        issued_at=iso(now),
        expires_at=iso(now + ttl_s),
        present=frozenset({"target"}),
    )


def compose_inject_goal(  # noqa: PLR0913  # one keyword per envelope field
    *,
    request_id: str,
    requester: str,
    target_machine: str,
    target: str,
    dispatch_id: str,
    template_name: str | None,
    goal_doc: str | None,
    params: Mapping[str, str],
    now: int,
    ttl_s: int = DEFAULT_REQUEST_TTL_S,
) -> LaunchRequest:
    """The `inject-goal` envelope: a goal for `target`, named by template or tracked path."""
    present = {"target", "dispatch_id"}
    if template_name is not None:
        present.add("template_name")
    if goal_doc is not None:
        present.add("goal_doc")
    if params:
        present.add("params")
    return LaunchRequest(
        schema_version=SCHEMA_VERSION,
        request_id=request_id,
        requester=requester,
        target_machine=target_machine,
        verb=VERB_INJECT_GOAL,
        issued_at=iso(now),
        expires_at=iso(now + ttl_s),
        goal_doc=goal_doc,
        target=target,
        dispatch_id=dispatch_id,
        template_name=template_name,
        params=dict(params) if params else None,
        present=frozenset(present),
    )


def compose_goal_control(  # noqa: PLR0913 - one keyword per envelope field
    *,
    request_id: str,
    requester: str,
    target_machine: str,
    target: str,
    verb: str,
    goal_digest: str | None,
    now: int,
    ttl_s: int = DEFAULT_REQUEST_TTL_S,
) -> LaunchRequest:
    """A closed quota goal-control envelope for one session on the target host."""
    present = {"target"}
    if goal_digest is not None:
        present.add("goal_digest")
    return LaunchRequest(
        schema_version=SCHEMA_VERSION,
        request_id=request_id,
        requester=requester,
        target_machine=target_machine,
        verb=verb,
        target=target,
        goal_digest=goal_digest,
        issued_at=iso(now),
        expires_at=iso(now + ttl_s),
        present=frozenset(present),
    )


# ---------------------------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    decision: str
    reason: str
    request_id: str | None = None
    request: LaunchRequest | None = None
    argv: list[str] | None = None
    cwd: str | None = None
    exit: int | None = None
    launched_identity: str | None = None
    #: The verb's verdict and reason: start-goal's report or the restart lifecycle.
    verdict: str | None = None
    verdict_reason: str | None = None
    goal_id: str | None = None
    tmux_exists: bool | None = None
    delivery_attempt_id: str | None = None
    engine_pid: int | None = None
    engine_pgid: int | None = None
    engine_start_time: float | None = None
    started_at: str | None = None
    log_path: str | None = None
    message_held: bool = False
    resolved_target: str | None = None
    cross_pair: bool = False
    cross_pair_policy: str | None = None
    receipt: dict[str, Any] | None = None
    #: Exact result of a target-host goal-waker effect. `effect_ok` is explicit so a requester
    #: never promotes a send acknowledgement or a non-empty outcome string to success.
    effect_ok: bool | None = None
    outcome: str | None = None
    goal_digest: str | None = None

    def reply_payload(self) -> dict[str, Any]:
        """What the `KIND_LAUNCH_RESULT` reply carries. Nothing from the request beyond its id.

        An `inject-goal` reply also echoes the `target` and `dispatch_id` it was asked for and
        carries start-goal's verdict, so the chief can ack its dispatch from the reply alone.
        """
        payload: dict[str, Any] = {
            "request_id": self.request_id,
            "decision": self.decision,
            "reason": self.reason,
            "launched_identity": self.launched_identity,
        }
        if self.tmux_exists is not None:
            payload.update(exit=self.exit, tmux_exists=self.tmux_exists)
        request = self.request
        if request is not None and request.verb == VERB_INJECT_GOAL:
            payload.update(
                verb=VERB_INJECT_GOAL,
                target=request.target,
                dispatch_id=request.dispatch_id,
                verdict=self.verdict,
                verdict_reason=self.verdict_reason,
                goal_id=self.goal_id,
            )
        if request is not None and request.verb == VERB_RESTART:
            payload.update(
                verb=VERB_RESTART,
                target=request.target,
                exit=self.exit,
                verdict=self.verdict,
                verdict_reason=self.verdict_reason,
            )
        if request is not None and request.verb in {
            VERB_PAUSE_GOAL,
            VERB_REARM_GOAL,
        }:
            payload.update(
                verb=request.verb,
                target=request.target,
                effect_ok=self.effect_ok,
                outcome=self.outcome,
                goal_digest=self.goal_digest,
            )
        return payload


def build_argv(
    request: LaunchRequest, *, bind_identity: str | None = None
) -> list[str]:
    """The one argv a verb runs. Never a shell string, never `-R`/`--remote`.

    `launch`: exactly `["ai", <engine>, <name>]`. `inject-goal`: start-goal's `start` with the
    target session, the chief's dispatch id, the goal source and each param as its own element,
    and `--json` so its verdict can be read back verbatim.
    """
    if request.verb == VERB_RESTART:
        if bind_identity is not None:
            return ["ai", "-X", str(request.target).partition("@")[0], "-E", "-j"]
        return ["ai", "--restart-session", str(request.target).partition("@")[0]]
    if request.verb == VERB_INJECT_GOAL:
        slug = str(request.target).partition("@")[0]
        argv = [
            "uv",
            "run",
            "--script",
            START_GOAL_SCRIPT,
            "start",
            "--target",
            f"session:{slug}",
            "--dispatch-id",
            str(request.dispatch_id),
        ]
        if request.template_name is not None:
            argv += ["--template", request.template_name]
        else:
            argv += ["--goal-doc", str(request.goal_doc)]
        for key in sorted(request.params or {}):
            argv += ["--param", f"{key}={(request.params or {})[key]}"]
        return [*argv, "--json"]
    return ["ai", str(request.engine), str(request.name)]


def _refuse(reason: str, request: LaunchRequest | RequestError | None) -> Decision:
    request_id = request.request_id if request is not None else None
    parsed = request if isinstance(request, LaunchRequest) else None
    return Decision(DECISION_REFUSED, reason, request_id=request_id, request=parsed)


def _goal_doc_tracked(goal_doc: str, tracked: Collection[str] | None) -> bool:
    if not goal_doc.startswith(GOAL_DOC_PREFIX) or ".." in goal_doc.split("/"):
        return False
    return tracked is not None and goal_doc in tracked


def decide(  # noqa: C901, PLR0911, PLR0912, PLR0913  # one return per ordered check; every input injected
    request: LaunchRequest | RequestError | None,
    *,
    sender: str,
    rung: str,
    config: RemoteLaunchConfig,
    now: int,
    seen_ids: Collection[str],
    in_flight: int,
    sender_hour_count: int,
    projects_root: Path,
    repo_tracked_paths: Collection[str] | None = None,
    machine: str = "",
    template_names: Collection[str] | None = None,
    restart_targets: Collection[str] = (),
    local_goal_sessions: Mapping[str, Path] | None = None,
    cross_pair: bool = False,
    received_at: int | None = None,
) -> Decision:
    """The ordered D-20 checks. Pure: every fact it judges is passed in.

    Pair-local requests retain the switch, carrier (T4), sender (T1), then payload order.
    Authenticated cross-pair requests check lifetime, replay and host capacity before matching
    the policy's rung, sender, verb, engine and repo. An argv is built only after every guard.
    """
    if not config.enabled:
        return _refuse(REASON_DISABLED, request)
    if not cross_pair and rung not in config.accept_rungs:
        return _refuse(REASON_RUNG, request)
    if not sender or (not cross_pair and sender not in config.senders):
        return _refuse(REASON_SENDER, request)
    if request is None:
        return _refuse(f"{REASON_SCHEMA}:body", None)
    if isinstance(request, RequestError):
        return _refuse(request.reason, request)
    # The envelope's own claims must agree with what the carrier proved: a request whose
    # `requester` names someone other than the signer would put a third party in the audit log.
    if request.requester != sender:
        return _refuse(f"{REASON_SCHEMA}:requester", request)
    if machine and request.target_machine != machine:
        return _refuse(f"{REASON_SCHEMA}:target_machine", request)
    issued = parse_iso(request.issued_at)
    expires = parse_iso(request.expires_at)
    lifetime_at = now if received_at is None else received_at
    if (
        issued is None
        or expires is None
        or expires <= issued
        or expires - issued
        > (
            config.git_inbox_request_ttl_s
            if rung == "git-inbox"
            else config.request_ttl_s
        )
        or issued > lifetime_at + CLOCK_SKEW_S
        or lifetime_at >= expires
    ):
        return _refuse(REASON_EXPIRED, request)
    if request.request_id in seen_ids:
        return _refuse(REASON_REPLAYED, request)
    if cross_pair:
        if in_flight >= config.max_concurrent:
            return _refuse(REASON_CONCURRENCY, request)
        if sender_hour_count >= config.per_sender_per_hour:
            return _refuse(REASON_RATE, request)
        policy = cross_pair_policy(config, request, sender=sender, rung=rung)
        if policy is None:
            return _refuse(REASON_CROSS_PAIR, request)
        config = dataclasses.replace(
            config,
            dry_run=policy.config.dry_run,
            verbs=policy.config.verbs,
            engines=policy.config.engines,
            repos=policy.config.repos,
        )
    if request.verb not in config.verbs or request.verb not in KNOWN_VERBS:
        return _refuse(REASON_VERB, request)
    required, optional = _VERB_FIELDS[request.verb]
    foreign = sorted(request.present - set(required) - set(optional))
    missing = [key for key in required if key not in request.present]
    if foreign or missing:
        return _refuse(f"{REASON_SCHEMA}:{(foreign or missing)[0]}", request)
    if request.verb == VERB_RESTART:
        refusal = _target_refusal(request, machine)
        cwd = projects_root
    elif request.verb == VERB_PAUSE_GOAL:
        refusal = _pause_goal_refusal(
            request,
            machine=machine,
            local_goal_sessions=local_goal_sessions,
        )
        cwd = projects_root / HARNESS_REPO
    elif request.verb == VERB_REARM_GOAL:
        refusal = _rearm_goal_refusal(
            request,
            machine=machine,
            local_goal_sessions=local_goal_sessions,
        )
        cwd = projects_root / HARNESS_REPO
    elif request.verb == VERB_INJECT_GOAL:
        refusal = _inject_goal_refusal(
            request,
            machine=machine,
            tree=projects_root / HARNESS_REPO,
            tracked=repo_tracked_paths,
            template_names=template_names,
        )
        cwd = projects_root / HARNESS_REPO
    else:
        refusal = _launch_refusal(
            request,
            config=config,
            projects_root=projects_root,
            tracked=repo_tracked_paths,
        )
        cwd = projects_root / str(request.repo)
    if refusal:
        return _refuse(refusal, request)
    if request.verb == VERB_RESTART and request.target in restart_targets:
        return _refuse("restart-in-progress", request)
    if in_flight >= config.max_concurrent:
        return _refuse(REASON_CONCURRENCY, request)
    if sender_hour_count >= config.per_sender_per_hour:
        return _refuse(REASON_RATE, request)
    outcome = DECISION_DRY_RUN if config.dry_run else DECISION_ACCEPTED
    return Decision(
        outcome,
        REASON_DRY_RUN if config.dry_run else REASON_ACCEPTED,
        request_id=request.request_id,
        request=request,
        argv=(
            None
            if request.verb in {VERB_PAUSE_GOAL, VERB_REARM_GOAL}
            else build_argv(request)
        ),
        cwd=str(cwd),
    )


def _launch_refusal(
    request: LaunchRequest,
    *,
    config: RemoteLaunchConfig,
    projects_root: Path,
    tracked: Collection[str] | None,
) -> str:
    """The first failed `launch` payload check, or ""."""
    engine, repo, name = str(request.engine), str(request.repo), str(request.name)
    if engine not in config.engines or engine not in KNOWN_ENGINES:
        return REASON_ENGINE
    if (
        repo not in config.repos
        or not REPO_RE.match(repo)
        or not (projects_root / repo).is_dir()
    ):
        return REASON_REPO
    if not NAME_RE.match(name) or name in RESERVED_NAMES:
        return REASON_NAME
    if request.goal_doc is not None and not _goal_doc_tracked(
        request.goal_doc, tracked
    ):
        return REASON_GOAL_DOC
    return ""


def _target_refusal(request: LaunchRequest, machine: str) -> str:
    """Require a concrete session on this host before constructing an argv."""
    slug, _, target_machine = str(request.target).partition("@")
    if (
        not IDENTITY_RE.match(str(request.target))
        or (machine and target_machine != machine)
        or target_machine != request.target_machine
        or not NAME_RE.match(slug)
        or slug in RESERVED_NAMES
    ):
        return REASON_TARGET
    return ""


def _pause_goal_refusal(
    request: LaunchRequest,
    *,
    machine: str,
    local_goal_sessions: Mapping[str, Path] | None,
) -> str:
    """The first failed local-session check before guarded clear."""
    if refusal := _target_refusal(request, machine):
        return refusal
    slug = str(request.target).partition("@")[0]
    if local_goal_sessions is None:
        return REASON_SESSION_INVENTORY
    if slug not in local_goal_sessions:
        return REASON_SESSION
    return ""


def _rearm_goal_refusal(
    request: LaunchRequest,
    *,
    machine: str,
    local_goal_sessions: Mapping[str, Path] | None,
) -> str:
    """The first failed local-session or digest-shape check before re-arm."""
    if not isinstance(request.goal_digest, str) or not GOAL_DIGEST_RE.match(
        request.goal_digest
    ):
        return f"{REASON_SCHEMA}:goal_digest"
    return _pause_goal_refusal(
        request, machine=machine, local_goal_sessions=local_goal_sessions
    )


def _inject_goal_refusal(  # noqa: C901, PLR0911  # one return per named payload check
    request: LaunchRequest,
    *,
    machine: str,
    tree: Path,
    tracked: Collection[str] | None,
    template_names: Collection[str] | None,
) -> str:
    """The first failed `inject-goal` payload check, or "". Every check precedes the argv."""
    if not DISPATCH_ID_RE.match(str(request.dispatch_id)):
        return f"{REASON_SCHEMA}:dispatch_id"
    if refusal := _target_refusal(request, machine):
        return refusal
    if request.template_name is None and request.goal_doc is None:
        return f"{REASON_SCHEMA}:template_name"
    if request.template_name is not None and request.goal_doc is not None:
        return f"{REASON_SCHEMA}:goal_doc"
    params = request.params or {}
    if len(params) > PARAMS_MAX or not all(
        PARAM_KEY_RE.match(key)
        and len(value) <= PARAM_VALUE_MAX
        and value.isprintable()
        for key, value in params.items()
    ):
        return REASON_PARAM
    if (
        not (tree / START_GOAL_SCRIPT).is_file()
        or not (tree / INJECTION_LIBRARY).is_file()
    ):
        return REASON_LIBRARY
    if request.template_name is not None:
        if (
            not TEMPLATE_RE.match(request.template_name)
            or template_names is None
            or request.template_name not in template_names
        ):
            return REASON_TEMPLATE
        return ""
    goal_doc = str(request.goal_doc)
    if not goal_doc.startswith((GOAL_TEMPLATES_PREFIX, GOAL_DRAFTS_PREFIX)) or not (
        goal_doc.endswith(".md") and _goal_doc_tracked(goal_doc, tracked)
    ):
        return REASON_GOAL_DOC
    return ""


def allowlisted_templates(tree: Path, tracked: Collection[str]) -> frozenset[str]:
    """Template names `start_goal.py --template` resolves to a TRACKED file in `tree`.

    A name is the stem of `templates/<name>-goal.md` (start-goal D-2) or a registry alias whose
    target is such a stem; an alias to a missing template allows nothing.
    """
    stems = {
        path[len(GOAL_TEMPLATES_PREFIX) : -len("-goal.md")]
        for path in tracked
        if path.startswith(GOAL_TEMPLATES_PREFIX)
        and path.endswith("-goal.md")
        and "/" not in path[len(GOAL_TEMPLATES_PREFIX) :]
    }
    names = set(stems)
    if GOAL_REGISTRY in tracked:
        import tomllib

        try:
            registry = tomllib.loads((tree / GOAL_REGISTRY).read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            registry = {}
        aliases = registry.get("aliases")
        if isinstance(aliases, dict):
            names |= {
                alias
                for alias, target in aliases.items()
                if isinstance(alias, str) and target in stems
            }
    return frozenset(name for name in names if TEMPLATE_RE.match(name))


# ---------------------------------------------------------------------------------------------
# The one impure call
# ---------------------------------------------------------------------------------------------


class NotAccepted(RuntimeError):
    """`execute` was handed a decision that is not `accepted`. Nothing was started."""


def _paused_goal_path(state_dir: Path, slug: str) -> Path:
    return state_dir / PAUSED_GOALS_DIR / f"{slug}.goal"


def _write_paused_goal(state_dir: Path, slug: str, goal_text: str) -> str:
    """Atomically store target-local resumable text in an owner-only record."""
    path = _paused_goal_path(state_dir, slug)
    _private_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(goal_text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, FILE_MODE)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return hashlib.sha256(goal_text.encode("utf-8")).hexdigest()


def _read_paused_goal(state_dir: Path, slug: str) -> str | None:
    try:
        return _paused_goal_path(state_dir, slug).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def execute(  # noqa: C901, PLR0911, PLR0912, PLR0913 - verb execution boundary
    decision: Decision,
    *,
    machine: str,
    runner: Callable[..., Any] = subprocess.run,
    timeout_s: int | None = None,
    verify_session: bool = False,
    message: str | None = None,
    state_dir: Path | None = None,
    record: dict[str, Any] | None = None,
    bind_identity: str | None = None,
    local_goal_sessions: Mapping[str, Path] | None = None,
) -> Decision:
    """Execute one accepted closed verb and return its target-host result.

    Process verbs run their argv once with `shell=False`, `cwd` set and a bound. Restart returns
    its detached supervisor's spawn receipt. Quota goal controls call their goal-waker adapters
    directly and carry no argv. The guard performs the refusal: a decision that is not accepted,
    or a process verb with no argv, raises before any effect is reached.
    """
    if (
        decision.decision != DECISION_ACCEPTED
        or decision.request is None
        or (
            not decision.argv
            and decision.request.verb not in {VERB_PAUSE_GOAL, VERB_REARM_GOAL}
        )
        or not decision.cwd
    ):
        raise NotAccepted(decision.decision)
    if decision.request.verb in {VERB_PAUSE_GOAL, VERB_REARM_GOAL}:
        from ai_chassis import goal_waker

        slug = str(decision.request.target).partition("@")[0]
        transcript = (local_goal_sessions or {}).get(slug)
        if transcript is None:
            return dataclasses.replace(
                decision,
                reason=REASON_SESSION_INVENTORY,
                effect_ok=False,
                outcome=REASON_SESSION_INVENTORY,
            )
        try:
            if state_dir is None:
                raise ValueError("goal control requires durable state")
            if decision.request.verb == VERB_PAUSE_GOAL:
                goal_text = goal_waker.active_goal_text(transcript)
                if goal_text is None:
                    return dataclasses.replace(
                        decision,
                        exit=1,
                        effect_ok=False,
                        outcome="no-active-goal",
                    )
                goal_digest = _write_paused_goal(state_dir, slug, goal_text)
                result = goal_waker.guarded_goal_clear(slug, transcript)
                return dataclasses.replace(
                    decision,
                    exit=0 if result.ok else 1,
                    effect_ok=result.ok,
                    outcome=result.state,
                    goal_digest=goal_digest,
                )
            goal_text = _read_paused_goal(state_dir, slug)
            if goal_text is None:
                return dataclasses.replace(
                    decision,
                    exit=1,
                    effect_ok=False,
                    outcome="paused-goal-missing",
                )
            actual_digest = hashlib.sha256(goal_text.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(
                actual_digest, str(decision.request.goal_digest)
            ):
                return dataclasses.replace(
                    decision,
                    exit=1,
                    effect_ok=False,
                    outcome="paused-goal-digest-mismatch",
                )
            outcome = goal_waker.rearm_goal(slug, goal_text)
            _paused_goal_path(state_dir, slug).unlink()
            return dataclasses.replace(
                decision,
                exit=0,
                effect_ok=True,
                outcome=outcome,
                goal_digest=actual_digest,
            )
        except Exception as exc:  # noqa: BLE001 - exact target effect failure is replied
            reason = getattr(exc, "reason", None) or type(exc).__name__
            return dataclasses.replace(
                decision, exit=1, effect_ok=False, outcome=str(reason)
            )
    if decision.request.verb == VERB_RESTART:
        from ai_chassis.remote_restart import start

        if state_dir is None or record is None:
            raise ValueError("restart requires durable audit state")
        if bind_identity is not None:
            decision = dataclasses.replace(
                decision, argv=build_argv(decision.request, bind_identity=bind_identity)
            )
        return start(
            decision,
            state_dir=state_dir,
            record=record,
            runner=subprocess.Popen if runner is subprocess.run else runner,
            message=message,
            **({"bind_identity": bind_identity} if bind_identity is not None else {}),
        )
    inject = decision.request.verb == VERB_INJECT_GOAL
    if timeout_s is None:
        timeout_s = INJECT_GOAL_TIMEOUT_S if inject else LAUNCH_TIMEOUT_S
    if verify_session and decision.request.verb == VERB_LAUNCH:
        from ai_chassis.remote_launch_session import execute_launch

        return execute_launch(
            decision, machine, runner, timeout_s, message, bind_identity=bind_identity
        )
    try:
        completed = runner(
            list(decision.argv),
            cwd=decision.cwd,
            shell=False,
            check=False,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return dataclasses.replace(decision, reason="launch-timeout", exit=None)
    except OSError as exc:
        return dataclasses.replace(
            decision, reason=f"launch-failed ({type(exc).__name__})", exit=None
        )
    code = int(getattr(completed, "returncode", 1))
    reason = decision.reason if code == 0 else f"launch-exit-{code}"
    if inject:
        report = _start_goal_report(str(getattr(completed, "stdout", "") or ""))
        return dataclasses.replace(
            decision,
            reason=reason,
            exit=code,
            verdict=report.get("state"),
            verdict_reason=report.get("reason"),
            goal_id=report.get("goal_id"),
        )
    identity = session_address(machine, decision.request.name) if code == 0 else None
    return dataclasses.replace(
        decision, reason=reason, exit=code, launched_identity=identity
    )


def _start_goal_report(stdout: str) -> dict[str, str | None]:
    """`state`, `reason`, `goal_id` from `start_goal.py --json`'s one object; absent = None."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            document = json.loads(line)
        except ValueError:
            continue
        if isinstance(document, dict):
            return {
                key: (str(document[key]) if document.get(key) is not None else None)
                for key in ("state", "reason", "goal_id")
            }
    return {}


# ---------------------------------------------------------------------------------------------
# Audit and state
# ---------------------------------------------------------------------------------------------


def default_state_dir(env: Mapping[str, str] | None = None) -> Path:
    """`$XDG_STATE_HOME/ai-harness/remote-launch`, the audit log's directory."""
    environment = os.environ if env is None else env
    root = (environment.get("XDG_STATE_HOME") or "").strip()
    base = Path(root) if root else Path.home() / ".local" / "state"
    return base / "ai-harness" / "remote-launch"


def default_projects_root() -> Path:
    return Path.home() / "projects"


def audit_record(  # noqa: PLR0913  # the design's record, one keyword per field
    decision: Decision,
    *,
    received_at: int,
    sender: str,
    rung: str,
) -> dict[str, Any]:
    """The design's JSONL record. Named fields only: never the body, never key material."""
    request = decision.request
    return {
        "received_at": iso(received_at),
        "request_id": decision.request_id,
        "sender": sender or None,
        "rung": rung,
        **(
            {"cross_pair": True, "cross_pair_policy": decision.cross_pair_policy}
            if decision.cross_pair
            else {}
        ),
        "verb": request.verb if request else None,
        "engine": request.engine if request else None,
        "repo": request.repo if request else None,
        "name": request.name if request else None,
        "target": request.target if request else None,
        "template_name": request.template_name if request else None,
        "goal_doc": request.goal_doc if request else None,
        "dispatch_id": request.dispatch_id if request else None,
        "decision": decision.decision,
        "reason": decision.reason,
        "argv": list(decision.argv) if decision.argv else None,
        "cwd": decision.cwd,
        "exit": decision.exit,
        "tmux_exists": decision.tmux_exists,
        "launched_identity": decision.launched_identity,
        "verdict": decision.verdict,
        "verdict_reason": decision.verdict_reason,
        "effect_ok": decision.effect_ok,
        "outcome": decision.outcome,
        **(
            {"delivery_attempt_id": decision.delivery_attempt_id}
            if decision.delivery_attempt_id
            else {}
        ),
        "goal_id": decision.goal_id,
        **(
            {
                key: getattr(decision, key)
                for key in (
                    "engine_pid",
                    "engine_pgid",
                    "engine_start_time",
                    "started_at",
                    "log_path",
                    "resolved_target",
                )
            }
            if decision.request and decision.request.verb == VERB_RESTART
            else {}
        ),
        **({"receipt": decision.receipt} if decision.receipt is not None else {}),
    }


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, DIR_MODE)


def append_audit(path: Path, record: Mapping[str, Any]) -> None:
    """Append one line. `O_APPEND`, created `0600` in a `0700` dir; never rewritten."""
    _private_dir(path.parent)
    line = json.dumps(dict(record), sort_keys=True) + "\n"
    handle = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
    try:
        os.fchmod(handle, FILE_MODE)
        os.write(handle, line.encode("utf-8"))
    finally:
        os.close(handle)


def read_audit(path: Path) -> list[dict[str, Any]]:
    """Every parseable record, oldest first. A torn line is skipped, never fatal."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return _audit_records(text)


def read_audit_strict(path: Path) -> list[dict[str, Any]]:
    """Recovery may ignore absence/torn lines, but must surface unreadable evidence."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return _audit_records(text)


def _audit_records(text: str) -> list[dict[str, Any]]:
    records = []
    for line in text.splitlines():
        try:
            node = json.loads(line)
        except ValueError:
            continue
        if isinstance(node, dict):
            records.append(node)
    return records


# ---------------------------------------------------------------------------------------------
# The requester side: what this machine asked for, and what came back
# ---------------------------------------------------------------------------------------------


def record_request(
    state_dir: Path,
    request: LaunchRequest,
    *,
    sent_at: int,
    cross_pair_policy: str | None = None,
) -> None:
    """One line per request this machine SENT, so a reply can be matched to it later."""
    append_audit(
        state_dir / REQUESTS_FILE,
        {
            "request_id": request.request_id,
            "verb": request.verb,
            "target_machine": request.target_machine,
            "target": request.target,
            "name": request.name,
            "dispatch_id": request.dispatch_id,
            "template_name": request.template_name,
            "goal_doc": request.goal_doc,
            "sent_at": iso(sent_at),
            **(
                {"cross_pair": True, "cross_pair_policy": cross_pair_policy}
                if cross_pair_policy
                else {}
            ),
        },
    )


def read_requests(state_dir: Path) -> list[dict[str, Any]]:
    return read_audit(state_dir / REQUESTS_FILE)


def read_results(state_dir: Path) -> list[dict[str, Any]]:
    return read_audit(state_dir / RESULTS_FILE)


def matching_result(state_dir: Path, request_id: str) -> dict[str, Any] | None:
    """Newest authenticated result for one request, including a reply that won the log race.

    The notifier can receive a fast target reply between the send completing and `record_request`.
    Such a reply was already signature-verified but was initially recorded `matched: false` because
    the requester row did not exist yet. Once that row exists, its target daemon and request id are
    sufficient to finish the same match without trusting the envelope's requester claim.
    """
    sent = next(
        (
            record
            for record in read_requests(state_dir)
            if record.get("request_id") == request_id
        ),
        None,
    )
    if sent is None:
        return None
    expected_sender = session_address(str(sent.get("target_machine")), "daemon")
    for result in reversed(read_results(state_dir)):
        if (
            result.get("request_id") == request_id
            and result.get("sender") == expected_sender
        ):
            return {
                **result,
                "matched": True,
                "verb": sent.get("verb"),
                "target": sent.get("target"),
                "dispatch_id": sent.get("dispatch_id"),
            }
    return None


def requester_active(state_dir: Path) -> bool:
    """Whether this machine has ever sent a request, i.e. whether a reply can be meant for it.

    The notifier subscribes to `KIND_LAUNCH_RESULT` only then, so a machine that never asked
    for anything asks the relay for nothing new.
    """
    return (state_dir / REQUESTS_FILE).is_file()


def record_result(
    state_dir: Path,
    content: str,
    *,
    sender: str,
    now: int,
) -> dict[str, Any]:
    """Record one `KIND_LAUNCH_RESULT` against the request it answers. Nothing else happens.

    `matched` only when `request_id` names a request this machine sent AND the reply was signed
    by that request's target daemon (`daemon@<target_machine>`); anything else is recorded
    `matched: false` with no dispatch id, so no reader can act on it.
    """
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    request_id = payload.get("request_id")
    sent = next(
        (
            record
            for record in read_requests(state_dir)
            if isinstance(request_id, str) and record.get("request_id") == request_id
        ),
        None,
    )
    matched = sent is not None and sender == session_address(
        str(sent.get("target_machine")), "daemon"
    )
    record = {
        "received_at": iso(now),
        "request_id": request_id if isinstance(request_id, str) else None,
        "sender": sender or None,
        "matched": matched,
        "dispatch_id": sent.get("dispatch_id") if matched and sent else None,
        "verb": sent.get("verb") if matched and sent else None,
        "target": sent.get("target") if matched and sent else None,
        **{
            key: payload.get(key) if isinstance(payload.get(key), str) else None
            for key in (
                "decision",
                "reason",
                "launched_identity",
                "verdict",
                "verdict_reason",
                "goal_id",
                "outcome",
                "goal_digest",
            )
        },
        "effect_ok": (
            payload.get("effect_ok")
            if isinstance(payload.get("effect_ok"), bool)
            else None
        ),
    }
    append_audit(state_dir / RESULTS_FILE, record)
    return record


@dataclass
class LaunchState:
    """What `decide` needs from history, read from the audit log and the in-flight markers."""

    seen_ids: set[str] = field(default_factory=set)
    sender_hour_count: int = 0
    in_flight: int = 0
    restart_targets: set[str] = field(default_factory=set)


def load_state(state_dir: Path, *, sender: str, now: int) -> LaunchState:
    """Replay cache and rate count from the audit log itself, so they survive a daemon restart."""
    from ai_chassis.remote_restart import recover

    active = recover(state_dir)
    state = LaunchState(
        seen_ids={str(row["request_id"]) for row in active},
        in_flight=len(active),
        restart_targets={
            str(row.get("resolved_target") or row["target"]) for row in active
        },
    )
    counted = set()
    for record in read_audit(state_dir / AUDIT_FILE):
        request_id = record.get("request_id")
        if isinstance(request_id, str):
            state.seen_ids.add(request_id)
        stamp = parse_iso(str(record.get("received_at") or ""))
        if (
            record.get("sender") == sender
            and record.get("decision") in {DECISION_ACCEPTED, DECISION_DRY_RUN}
            and stamp is not None
            and now - stamp < SECONDS_PER_HOUR
            and request_id not in counted
        ):
            state.sender_hour_count += 1
            counted.add(request_id)
    marker_dir = state_dir / IN_FLIGHT_DIR
    if marker_dir.is_dir():
        for marker in marker_dir.iterdir():
            # A marker older than the launch timeout belongs to a daemon that died mid-launch;
            # counting it forever would cap this host permanently.
            try:
                age = now - int(marker.stat().st_mtime)
            except OSError:
                continue
            if age < 2 * max(LAUNCH_TIMEOUT_S, INJECT_GOAL_TIMEOUT_S):
                state.in_flight += 1
    return state


def git_tracked_paths(repo_dir: Path, prefix: str = GOAL_DOC_PREFIX) -> frozenset[str]:
    """The tracked files under `prefix` in `repo_dir`, or empty when git cannot say."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_dir), "ls-files", "-z", "--", prefix],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    if completed.returncode != 0:
        return frozenset()
    return frozenset(
        item for item in completed.stdout.decode("utf-8", "replace").split("\0") if item
    )


# ---------------------------------------------------------------------------------------------
# The daemon-facing entry
# ---------------------------------------------------------------------------------------------


def _local_goal_sessions() -> Mapping[str, Path] | None:
    """Live named sessions and their target-host transcripts, or None when inventory is blind."""
    from ai_chassis import goal_waker

    try:
        rows = goal_waker.discover_sessions(lambda *_args, **_kwargs: None)
    except Exception:  # noqa: BLE001 - blind inventory is a named fail-closed refusal
        return None
    if rows is None:
        return None
    named: dict[str, Path] = {}
    ambiguous: set[str] = set()
    for row in rows:
        if row.name is None:
            continue
        if row.name in named:
            ambiguous.add(row.name)
        else:
            named[row.name] = row.transcript
    for name in ambiguous:
        named.pop(name, None)
    return named


def _sidecar_decision(  # noqa: PLR0913 - admission state, target inventory and config seams
    decision, *, state, state_dir, runner, config_tables, machine, resolver
):
    from ai_chassis import wake_config, wake_sidecar
    from ai_chassis.remote_launch_session import restart_target

    request = decision.request

    def permit(name):
        return wake_config.sidecar_permit(
            request.verb,
            wake_sidecar.load_wake_sidecar_config(config_tables, machine),
            (resolver or wake_config.resolve_runtime)(name, machine),
        )

    name = (
        str(request.target).partition("@")[0]
        if request.verb == VERB_RESTART
        else str(request.name)
    )
    allowed, reason = permit(name)
    if not allowed:
        return _refuse(reason, request)
    if request.verb != VERB_RESTART:
        return decision
    target, reason = restart_target(
        str(request.target), read_audit(state_dir / AUDIT_FILE), runner
    )
    if target is None:
        return _refuse(reason, request)
    if target in state.restart_targets:
        return _refuse("restart-in-progress", request)
    if target != request.target:
        allowed, reason = permit(target.partition("@")[0])
        if not allowed:
            return _refuse(reason, request)
    return dataclasses.replace(
        decision,
        argv=build_argv(dataclasses.replace(request, target=target)),
        resolved_target=target,
    )


def _cross_pair_audit(
    decision: Decision,
    config: RemoteLaunchConfig,
    *,
    sender: str,
    rung: str,
    cross_pair: bool,
) -> Decision:
    if not cross_pair:
        return decision
    policy = (
        cross_pair_policy(config, decision.request, sender=sender, rung=rung)
        if decision.request
        else None
    )
    return dataclasses.replace(
        decision, cross_pair=True, cross_pair_policy=policy.name if policy else None
    )


def handle_event(  # noqa: C901, PLR0912, PLR0913, PLR0915  # ordered admission and execution
    event_content: str,
    *,
    sender: str,
    rung: str,
    config_tables: Mapping[str, Any],
    now: int,
    state_dir: Path,
    projects_root: Path,
    machine: str,
    runner: Callable[..., Any] = subprocess.run,
    tracked_paths: Callable[[Path], Collection[str]] = git_tracked_paths,
    log: Callable[[str], None] | None = None,
    resolver: Callable[..., Any] | None = None,
    message: str | None = None,
    bind_identity: str | None = None,
    local_goal_sessions: Callable[[], Mapping[str, Path] | None] = _local_goal_sessions,
) -> Decision:
    """Load, parse, decide, audit, execute-if-accepted. Returns the decision the reply carries.

    Launch/inject-goal retain one row after execution. Restart returns a started row;
    its detached supervisor writes completion and retains the target/concurrency slot
    until that row is durable. Restart admission is serialized across notifier processes.
    """
    config = load_remote_launch_config(config_tables)
    from ai_chassis.quota_optional import admission as quota_admission

    cross_pair = bool(sender) and is_cross_pair(sender, machine)
    if not config.enabled and log is not None:
        log(f"remote-launch disabled ({config.disabled_reason}); request refused")
    # T4: the payload is interpreted only once the switch and the carrier have passed. Before
    # that, only a pattern-checked `request_id` is lifted, so the refusal reply can be matched.
    request: LaunchRequest | RequestError | None
    if config.enabled and (cross_pair or rung in config.accept_rungs):
        request = parse_request(event_content)
    else:
        request = RequestError("not-parsed", _peek_request_id(event_content))
    from ai_chassis.remote_restart import lock

    admission = (
        lock(state_dir / "restart-admission.lock")
        if isinstance(request, LaunchRequest) and request.verb == VERB_RESTART
        else contextlib.nullcontext()
    )
    with admission:
        quota_session = (
            request.target or session_address(machine, request.name)
            if isinstance(request, LaunchRequest)
            else ""
        )
        retained = None
        if isinstance(request, LaunchRequest):
            for row in quota_admission.replay_items(source="remote-launch"):
                payload = row["payload"] or {}
                if row["item"] != request.request_id:
                    continue
                if all(
                    payload.get(key) == value
                    for key, value in {
                        "event_content": event_content,
                        "sender": sender,
                        "rung": rung,
                        "machine": machine,
                        "message": message,
                        "bind_identity": bind_identity,
                        "state_dir": str(state_dir.resolve()),
                        "projects_root": str(projects_root.resolve()),
                    }.items()
                ):
                    retained = payload
                    break
                # A conflicting carrier cannot replace, cancel or poison the
                # replay receipt of the original authenticated held request.
                return _refuse("held-request-mismatch", request)
        state = load_state(state_dir, sender=sender, now=now)
        tracked: Collection[str] | None = None
        template_names: Collection[str] | None = None
        goal_sessions: Mapping[str, Path] | None = None
        if (
            isinstance(request, LaunchRequest)
            and request.verb == VERB_INJECT_GOAL
            and request.verb in config.verbs
            and sender in config.senders
        ):
            tree = projects_root / HARNESS_REPO
            tracked = tracked_paths(tree)
            template_names = allowlisted_templates(tree, tracked)
        elif (
            isinstance(request, LaunchRequest)
            and request.goal_doc is not None
            and request.repo in config.repos
            and REPO_RE.match(str(request.repo))
        ):
            tracked = tracked_paths(projects_root / str(request.repo))
        if (
            isinstance(request, LaunchRequest)
            and request.verb in {VERB_PAUSE_GOAL, VERB_REARM_GOAL}
            and request.verb in config.verbs
            and sender in config.senders
        ):
            goal_sessions = local_goal_sessions()
        decision = decide(
            request,
            sender=sender,
            rung=rung,
            config=config,
            now=now,
            seen_ids=state.seen_ids,
            in_flight=state.in_flight,
            sender_hour_count=state.sender_hour_count,
            projects_root=projects_root,
            repo_tracked_paths=tracked,
            machine=machine,
            template_names=template_names,
            restart_targets=state.restart_targets,
            local_goal_sessions=goal_sessions,
            cross_pair=cross_pair,
            received_at=retained["received_at"] if retained else None,
        )
        if retained and decision.reason in {REASON_CONCURRENCY, REASON_RATE}:
            return _refuse("quota-held", request)
        if (
            decision.decision in {DECISION_ACCEPTED, DECISION_DRY_RUN}
            and isinstance(request, LaunchRequest)
            and request.verb == VERB_RESTART
        ):
            refusal = restart_message_refusal(message, config)
            if refusal:
                decision = _refuse(refusal, request)
        if (
            decision.decision == DECISION_ACCEPTED
            and isinstance(request, LaunchRequest)
            and request.verb != VERB_PAUSE_GOAL
            and not (request.verb == VERB_LAUNCH and request.engine != "c")
            and not quota_admission.check(
                "remote-launch",
                quota_session,
                request.request_id,
                sender=sender,
                payload={
                    "event_content": event_content,
                    "sender": sender,
                    "rung": rung,
                    "machine": machine,
                    "message": message,
                    "bind_identity": bind_identity,
                    "received_at": now,
                    "state_dir": str(state_dir.resolve()),
                    "projects_root": str(projects_root.resolve()),
                },
            )
        ):
            # Not an execution attempt or replay receipt. The authenticated carrier
            # remains pending, and a later intake repeats all authorization checks.
            return _refuse("quota-held", request)
        # Admission sees the original carried message. Only execution transfers
        # delivery to the dispatcher; the restart reconciler gets no second copy.
        message = None if bind_identity is not None else message
        if (
            decision.decision in {DECISION_ACCEPTED, DECISION_DRY_RUN}
            and isinstance(request, LaunchRequest)
            and request.verb in {VERB_LAUNCH, VERB_RESTART}
        ):
            decision = _sidecar_decision(
                decision,
                state=state,
                state_dir=state_dir,
                runner=runner,
                config_tables=config_tables,
                machine=machine,
                resolver=resolver,
            )
        decision = _cross_pair_audit(
            decision, config, sender=sender, rung=rung, cross_pair=cross_pair
        )
        if (
            decision.decision == DECISION_ACCEPTED
            and isinstance(request, LaunchRequest)
            and request.verb == VERB_RESTART
        ):
            result = execute(
                decision,
                machine=machine,
                runner=runner,
                state_dir=state_dir,
                record=audit_record(
                    decision, received_at=now, sender=sender, rung=rung
                ),
                message=message,
                **(
                    {"bind_identity": bind_identity}
                    if bind_identity is not None
                    else {}
                ),
            )
            quota_admission.complete("remote-launch", quota_session, request.request_id)
            if bind_identity is not None and result.verdict in {"started", "restarted"}:
                return dataclasses.replace(
                    result,
                    reason="restart-awaiting-bind",
                    launched_identity=result.resolved_target or request.target,
                )
            return dataclasses.replace(result, message_held=message is not None)
        if decision.decision == DECISION_ACCEPTED:
            marker_dir = state_dir / IN_FLIGHT_DIR
            _private_dir(marker_dir)
            marker = marker_dir / f"{decision.request_id}.json"
            marker.write_text(json.dumps({"started_at": iso(now)}), encoding="utf-8")
            try:
                decision = execute(
                    decision,
                    machine=machine,
                    runner=runner,
                    verify_session=rung == "git-inbox",
                    message=message,
                    bind_identity=bind_identity,
                    state_dir=state_dir,
                    local_goal_sessions=goal_sessions,
                )
            finally:
                marker.unlink(missing_ok=True)
        if isinstance(request, LaunchRequest) and decision.reason != "quota-held":
            quota_admission.complete("remote-launch", quota_session, request.request_id)
        append_audit(
            state_dir / AUDIT_FILE,
            audit_record(decision, received_at=now, sender=sender, rung=rung),
        )
        return decision


def resume_quota_held(**kwargs) -> list[Decision]:
    """Dispatcher/notifier seam: replay held authenticated requests in FIFO order.

    Supply current handle_event configuration, time, state_dir, projects_root and
    machine (plus optional effect seams). Transport identities come ONLY from the
    previously verified intake. Current permissions, replay and capacity still apply.
    """
    from ai_chassis.quota_optional import admission

    results = []
    for row in admission.replay_items(source="remote-launch"):
        payload = row["payload"]
        if not payload or any(
            payload.get(key) != value
            for key, value in {
                "machine": kwargs["machine"],
                "state_dir": str(kwargs["state_dir"].resolve()),
                "projects_root": str(kwargs["projects_root"].resolve()),
            }.items()
        ):
            continue
        results.append(
            handle_event(
                payload["event_content"],
                sender=payload["sender"],
                rung=payload["rung"],
                message=payload["message"],
                bind_identity=payload["bind_identity"],
                **kwargs,
            )
        )
    return results


def handle_git_inbox_event(  # noqa: C901, PLR0913, PLR0911 - ordered authentication refusals and read-only recovery
    node: object,
    *,
    target: str,
    channel: Any,
    machine: str,
    now: int,
    config_tables: Mapping[str, Any],
    state_dir: Path,
    projects_root: Path,
    runner: Callable[..., Any] = subprocess.run,
    resolver: Callable[..., Any] | None = None,
    required_verb: str | None = None,
    message: str | None = None,
    reconcile_only: bool = False,
    wait_for_bind: bool = False,
) -> tuple[Decision, str]:
    """Authenticate a carried event before judging its closed launch envelope.

    Return the decision and verified reply key. The FROM header is deliberately not an input.
    Same-host session signers are allowed here; the Buzz receive path retains its own policy.
    """
    from ai_chassis import buzz_channel as buzz

    def refuse(reason: str) -> tuple[Decision, str]:
        decision = Decision(DECISION_REFUSED, reason)
        if reconcile_only:
            return decision, ""
        append_audit(
            state_dir / AUDIT_FILE,
            audit_record(
                decision,
                received_at=now,
                sender="",
                rung="git-inbox",
            ),
        )
        return decision, ""

    problem = buzz.validate_event_node(node)
    if problem:
        return refuse(f"event:{problem}")
    if node["kind"] != buzz.KIND_LAUNCH_REQUEST:
        return refuse("event:kind-not-launch-request")
    daemon_name = buzz.daemon_identity_name(machine)
    if channel is None or not buzz.identity_key_exists(channel.key_dir, daemon_name):
        return refuse("event:daemon-identity-unavailable")
    daemon = channel.daemon_identity
    if daemon is None:
        return refuse("event:daemon-identity-unavailable")
    if [tag for tag in node["tags"] if tag and tag[0] == "p"] != [
        ["p", daemon.pubkey_hex]
    ]:
        return refuse("event:wrong-recipient")
    event = buzz.RelayEvent(
        event_id=node["id"],
        pubkey=node["pubkey"],
        kind=node["kind"],
        content=node["content"],
        created_at=node["created_at"],
        signature=node["sig"],
        tags=node["tags"],
    )
    content = buzz.open_addressed_event(daemon.keys(), event)
    if content is None:
        return refuse("event:undecryptable")
    sender = channel.pubkey_names().get(node["pubkey"], "")
    if (
        not sender
        or node["pubkey"] == daemon.pubkey_hex
        or sender == buzz.daemon_identity_name(machine)
    ):
        return refuse("event:unknown-or-daemon-signer")
    request = parse_request(content)
    if isinstance(request, LaunchRequest) and (
        request.verb not in {VERB_LAUNCH, VERB_RESTART}
        or (required_verb is not None and request.verb != required_verb)
        or (
            request.target != session_address(machine, target)
            if request.verb == VERB_RESTART
            else request.name != target
        )
    ):
        return refuse("event:target-or-verb-mismatch")
    if reconcile_only:
        from ai_chassis.quota_optional import admission

        # A write-ahead carrier reservation is not an effect attempt when quota
        # held its authenticated request. This also recovers a crash before the
        # watcher could record that distinction. Body/header text grants nothing.
        reconcile_only = not any(
            row["payload"]
            and row["payload"].get("event_content") == content
            and row["payload"].get("sender") == sender
            and row["payload"].get("rung") == "git-inbox"
            and row["payload"].get("message") == message
            for row in admission.replay_items(source="remote-launch")
        )
    if reconcile_only:
        if wait_for_bind and isinstance(request, LaunchRequest):
            rows = read_audit(state_dir / AUDIT_FILE)
            row = next(
                (
                    row
                    for row in reversed(rows)
                    if row.get("request_id") == request.request_id
                ),
                {},
            )
            return Decision(
                DECISION_REFUSED,
                "already-attempted",
                request_id=request.request_id,
                log_path=row.get("log_path"),
                launched_identity=(
                    row.get("launched_identity") or row.get("resolved_target")
                )
                if row.get("verdict") not in {"refused", "timed-out"}
                else None,
                engine_pid=row.get("engine_pid"),
                engine_start_time=row.get("engine_start_time"),
                receipt=row.get("receipt"),
                verdict=row.get("verdict"),
            ), ""
        from ai_chassis.remote_restart import held_message

        held = (
            isinstance(request, LaunchRequest)
            and request.verb == VERB_RESTART
            and held_message(state_dir, request.request_id, message)
        )
        return Decision(
            DECISION_REFUSED,
            "already-attempted",
            message_held=held,
            request_id=request.request_id if held else None,
        ), ""
    return handle_event(
        content,
        sender=sender,
        rung="git-inbox",
        config_tables=config_tables,
        now=now,
        state_dir=state_dir,
        projects_root=projects_root,
        machine=machine,
        runner=runner,
        resolver=resolver,
        message=message,
        bind_identity=node["id"] if wait_for_bind else None,
    ), node["pubkey"]
