"""Chief-of-staff pair reconcile, session discovery and VP enrolment (issue-ref Phase 4).

Design: `docs/designs/issue-ref-firstmate-chief-of-staff.md` § Fleet pair coordination, § Data
Model > Fleet-pair additions, D-16 to D-19. Entry point: `scripts/chief_of_staff_cli.py`.

This module owns the logic and the two files the chief writes, `$FM_HOME/registry.json`
(`vp_sessions` only; every other key is preserved as found) and the append-only
`$FM_HOME/dispatch.jsonl`. It gathers nothing itself: the CLI hands it the inventories it read.

The chief NEVER injects into a VP. An enrolment is one plain message the chief sends itself; this
module renders it and records that it was sent, and refuses to render it twice.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fnmatch
import json
import os
import re
import secrets
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None  # type: ignore[assignment]

from ai_chassis.message_transports import select_pair
from ai_chassis.session_address import (
    AddressError,
    address_from_board_name,
    parse_address,
    session_address,
)

REGISTRY_SCHEMA_VERSION = 2

STATE_DISCOVERED = "discovered"
STATE_UNACKED = "enrolled-unacked"
STATE_ENROLLED = "enrolled"
STATE_GONE = "gone"

ROLE_VP = "vp"
ROLE_AGENT = "agent"
ROLE_CHIEF = "chief"

SOURCE_LIVE_SESSIONS = "claude_sessions.live_sessions"
SOURCE_AI_REGISTRY = "ai-session-registry"
SOURCE_FLEET_LIVENESS = "fleet-liveness"

#: The D-18 triggers. A message to a session names one of these and a `bd` issue or decision.
TRIGGERS = (
    "idle-without-work",
    "blocked",
    "ack-window-elapsed",
    "fleet-gate",
    "dependency-moved",
    "the maintainer-ask",
)

ENROLMENT_REFUSAL_REASONS = ("not-live", "role-agent", "unreachable")

#: `ai` names a Claude session's tmux target `c-<name>`, or `c-r-<name>` when resumed.
_AI_CLAUDE_PREFIXES = ("c-r-", "c-")
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


class ChiefError(Exception):
    """A request the chief must refuse, carrying the status word the CLI reports."""

    def __init__(self, status: str, detail: str, exit_code: int = 4) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.exit_code = exit_code


def chief_homes(root: Path | None = None) -> list[Path]:
    """Discover chief homes using the explicit override or the deterministic state root."""
    explicit = os.environ.get("FM_HOME", "").strip()
    if explicit:
        return [Path(explicit)]
    if root is None:
        state = Path(
            os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
        )
        root = state / "firstmate" / "chief-of-staff"
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def iso(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def mint_id(prefix: str, now: dt.datetime) -> str:
    """`<prefix>-<UTC basic timestamp>-<4 hex>`: sortable, and unique within one second."""
    return f"{prefix}-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(2)}"


# ---------------------------------------------------------------------------------------------
# The registered-chief guard (design D-24 (b), ISSUE-REF T-09)
# ---------------------------------------------------------------------------------------------

CHIEF_ROLE = "chief-of-staff"


def require_chief(
    role: str | None, fm_home: str | None, machine_key: str | None
) -> dict[str, Any]:
    """Return the registration naming this session as this machine's chief, or refuse.

    `/chief-of-staff` is model-invocable, so this is the only thing between prose in another
    session and the chief's loop. It fails closed on every input it cannot prove: a role other
    than `chief-of-staff`, no `FM_HOME`, an absent, unreadable or malformed registration, a
    registration without an agent name, and a machine key that is unknown or does not match.
    It also proves the calling Claude process belongs to the registered tmux pane.
    Unreadable process, registry or tmux facts fail closed; it writes nothing.
    """
    if role != CHIEF_ROLE:
        raise ChiefError(
            "not-chief", f"refused: not the registered chief (role={role or 'unset'})"
        )
    registration = chief_registration(fm_home, machine_key)
    from ai_chassis.chief_process import ChiefBindingError, require_chief_process

    try:
        require_chief_process(registration)
    except ChiefBindingError as exc:
        raise ChiefError(exc.status, exc.detail) from exc
    return registration


def chief_registration(fm_home: str | None, machine_key: str | None) -> dict[str, Any]:
    """Read and validate a target chief registration; this grants no caller authority.

    VP escalation routing reads another session's registration. Only require_chief
    authorizes the caller, after checking its role and live process binding.
    """
    home = (fm_home or "").strip()
    path = (
        Path(home) / "state" / "chief-session.json"
        if home
        else Path("$FM_HOME") / "state" / "chief-session.json"
    )
    if not home or not path.exists():
        suffix = "" if home else " (FM_HOME is unset)"
        raise ChiefError(
            "no-registration", f"refused: no chief registration at {path}{suffix}"
        )
    try:
        registration = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ChiefError(
            "registration-unreadable", f"refused: cannot read {path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ChiefError(
            "registration-malformed", f"refused: cannot parse {path}: {exc}"
        ) from exc
    if not isinstance(registration, dict):
        raise ChiefError(
            "registration-malformed",
            f"refused: {path} holds a JSON {type(registration).__name__}, not an object",
        )
    name = registration.get("native_agent_name")
    if not isinstance(name, str) or not name.strip():
        raise ChiefError(
            "registration-malformed",
            f"refused: {path} names no chief (native_agent_name missing or empty)",
        )
    if not machine_key:
        raise ChiefError(
            "machine-unknown",
            "refused: this machine's key is unresolved (no [machine] id in "
            "ai-harness/machine.toml and AI_MACHINE_ID unset)",
        )
    if registration.get("machine_key") != machine_key:
        raise ChiefError(
            "machine-mismatch",
            f"refused: {path} does not name this machine's chief "
            "(its machine_key differs from this machine's key)",
        )
    return registration


# ---------------------------------------------------------------------------------------------
# The pair
# ---------------------------------------------------------------------------------------------


def pair_view(config: Mapping[str, Any], machine: str) -> dict[str, Any]:
    """This machine's pair from `config/message-transports.json` `pairs`, or `pair: none`."""
    pair = select_pair(dict(config), machine)
    if pair is None:
        return {"machine": machine, "pair": "none", "members": [machine], "peer": None}
    peers = [member for member in pair["members"] if member != machine]
    return {
        "machine": machine,
        "pair": pair["name"],
        "members": list(pair["members"]),
        "peer": peers[0] if peers else None,
        "relay_host": pair["relay_host"],
    }


# ---------------------------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------------------------


def ai_registry_path(env: Mapping[str, str]) -> Path:
    """Where ai-cli-utils `session_registry.py` keeps `iterm2-sessions.json` on this host."""
    base = (env.get("XDG_STATE_HOME") or "").strip()
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "ai-cli-utils" / "iterm2-sessions.json"


def read_ai_registry(path: Path) -> tuple[list[dict[str, Any]], str]:
    """Open local Claude records from the `ai` registry, and a status word for the read.

    An absent file is an empty registry, as its writer defines it; a file that is not a
    registry is reported and contributes nothing.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], "absent"
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"unreadable: {exc}"
    if not isinstance(doc, dict) or not isinstance(doc.get("sessions"), list):
        return [], "unreadable: not a session registry"
    records = [
        record
        for record in doc["sessions"]
        if isinstance(record, dict)
        and record.get("kind") == "local"
        and not record.get("ended_at")
        and ai_slug(str(record.get("name") or ""))
    ]
    return records, "ok"


def ai_slug(tmux_name: str) -> str | None:
    """The session slug of an `ai`-launched Claude tmux name, or None for any other name."""
    for prefix in _AI_CLAUDE_PREFIXES:
        if tmux_name.startswith(prefix) and len(tmux_name) > len(prefix):
            return tmux_name[len(prefix) :]
    return None


def _session_home(cwd: str | None, name: str) -> str | None:
    """The repo of a canonical session home `<repo>/.worktrees/<name>`, else None."""
    if not cwd:
        return None
    path = Path(cwd)
    if path.name == name and path.parent.name == ".worktrees":
        return path.parent.parent.name
    return None


def local_inventory(
    machine: str,
    sessions: list[Mapping[str, Any]] | None,
    ai_records: Iterable[Mapping[str, Any]],
    *,
    chief_slug: str | None,
) -> list[dict[str, Any]]:
    """Union `live_sessions()` and the `ai` registry into one entry per slug.

    Risk 16: `role: vp` only for a session proved to be an `ai`-launched session home (a
    validated `live_sessions` record whose cwd is `<repo>/.worktrees/<name>`, or an open `ai`
    registry record). Anything else is `agent` and is never messaged.
    """
    entries: dict[str, dict[str, Any]] = {}
    for session in sessions or []:
        name = session.get("name")
        if not name:
            continue
        repo = _session_home(session.get("cwd"), str(name))
        canonical = bool(session.get("registered")) and repo is not None
        entries[str(name)] = {
            "slug": str(name),
            "liveness": "live",
            "sources": [SOURCE_LIVE_SESSIONS],
            "role": ROLE_VP if canonical else ROLE_AGENT,
            "repo": repo,
            "tmux_target": None,
            "rung": "native",
        }
    for record in ai_records:
        tmux = str(record["name"])
        slug = ai_slug(tmux)
        if slug is None:
            continue
        cwd = record.get("cwd")
        entry = entries.setdefault(
            slug,
            {
                "slug": slug,
                "liveness": "registered",
                "sources": [],
                "role": ROLE_VP,
                "repo": Path(cwd).name if cwd else None,
                "tmux_target": None,
                "rung": "native",
            },
        )
        entry["sources"].append(SOURCE_AI_REGISTRY)
        entry["role"] = ROLE_VP
        entry["tmux_target"] = tmux
    out = []
    for slug, entry in sorted(entries.items()):
        if chief_slug and slug == chief_slug:
            entry["role"] = ROLE_CHIEF
        out.append(
            {"vp_id": session_address(machine, slug), "machine": machine, **entry}
        )
    return out


def _slug_of(identity: str) -> str:
    try:
        return parse_address(identity).slug
    except AddressError:
        return ""


def remote_inventory(peer: str, report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The peer's sessions from a `fleet-liveness status --machine <peer> --json` document."""
    out = []
    for record in report.get("identities") or []:
        if not isinstance(record, Mapping) or record.get("machine") != peer:
            continue
        identity = str(record.get("identity") or "")
        slug = record.get("slug") or _slug_of(identity)
        if not slug or slug == "daemon":
            continue
        rung = str(record.get("rung") or "")
        out.append(
            {
                "vp_id": session_address(peer, slug),
                "machine": peer,
                "slug": slug,
                "liveness": str(record.get("status") or "undetermined"),
                "sources": [f"{SOURCE_FLEET_LIVENESS}:{rung}"],
                "role": ROLE_VP,
                "repo": None,
                "tmux_target": None,
                "rung": rung,
                "age_s": record.get("age_s"),
            }
        )
    return sorted(out, key=lambda entry: entry["vp_id"])


def peer_published(peer: str, report: Mapping[str, Any]) -> bool:
    """Whether the status document carries ANY identity for the peer, its daemon included."""
    return any(
        isinstance(record, Mapping) and record.get("machine") == peer
        for record in report.get("identities") or []
    )


def classify(liveness: str, *, has_work: bool, awaiting_ack: bool) -> str:
    """The one classifier, for a local or a remote VP (design § Blocker surfacing).

    Inputs: a liveness word (`fleet-liveness` for a remote session; for a local one the chief
    maps `ListAgents` busy -> `live`, idle -> `idle`, absent -> `gone`), whether Beads holds
    in-progress work for it, and whether an enrolment or dispatch to it awaits its ack.
    A heartbeat cannot tell busy from idle, so `live` reads as `busy`: the direction that
    never nags a working session.
    """
    if awaiting_ack:
        return "awaiting-ack"
    if liveness == "live":
        return "busy"
    if liveness == "idle":
        return "idle-with-work" if has_work else "idle-without-work"
    return "unreachable"


# ---------------------------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------------------------


def registry_path(fm_home: Path) -> Path:
    return fm_home / "registry.json"


def load_registry(fm_home: Path) -> tuple[dict[str, Any], str | None]:  # noqa: PLR0911  # one return per named failure
    """The registry document and None, or a fresh document and the exact failure (AC11)."""
    path = registry_path(fm_home)
    fresh: dict[str, Any] = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "vp_sessions": [],
    }
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return fresh, f"{path} is missing"
    except OSError as exc:
        return fresh, f"{path} is unreadable: {exc}"
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        return fresh, f"{path} is not valid JSON ({exc.msg} at line {exc.lineno})"
    if not isinstance(doc, dict):
        return fresh, f"{path} is not a JSON object"
    if doc.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        return fresh, (
            f"{path} has schema_version {doc.get('schema_version')!r}, "
            f"expected {REGISTRY_SCHEMA_VERSION}"
        )
    rows = doc.setdefault("vp_sessions", [])
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return fresh, f"{path} has a vp_sessions that is not a list of objects"
    for row in rows:
        _rename_legacy_row(row)
    return doc, None


def _rename_legacy_row(row: dict[str, Any]) -> None:
    """Rewrite a row stored under the pre-ISSUE-REF `<host>--<slug>` id to `<slug>@<host>`.

    Done in place on load, so the session keeps its enrolment instead of reading as `gone`
    beside a freshly `discovered` twin; the next save persists it. `session` held the old
    `<host>/<slug>` send address and takes the same value as the id.
    """
    vp_id = row.get("vp_id")
    if not isinstance(vp_id, str) or "@" in vp_id:
        return
    address = address_from_board_name(vp_id)
    if address is None:
        return
    row["vp_id"] = str(address)
    row["session"] = str(address)


def set_aside_malformed(fm_home: Path, now: dt.datetime) -> Path | None:
    """Rename a malformed registry out of the way so the rewrite loses none of its bytes."""
    path = registry_path(fm_home)
    if not path.exists():
        return None
    kept = path.with_name(f"{path.name}.malformed-{now.strftime('%Y%m%dT%H%M%SZ')}")
    path.rename(kept)
    return kept


@contextlib.contextmanager
def registry_lock(fm_home: Path) -> Iterator[None]:
    """Hold `registry.json.lock` across one load-change-save of the registry.

    Every writer takes it, so a chief CLI update and a session rekey cannot each save over the
    other's change. Readers never take it: `save_registry` replaces the file whole. A home that
    does not exist yet has no registry to race on, so it is not locked.
    """
    if fcntl is None or not fm_home.is_dir():
        yield
        return
    handle = os.open(fm_home / "registry.json.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


def save_registry(fm_home: Path, doc: Mapping[str, Any]) -> None:
    """Replace the registry whole. Call it under `registry_lock` with the load it follows."""
    path = registry_path(fm_home)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(doc, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def find_row(doc: Mapping[str, Any], vp_id: str) -> dict[str, Any]:
    for row in doc.get("vp_sessions", []):
        if row.get("vp_id") == vp_id:
            return row
    raise ChiefError(
        "unknown-session", f"no vp_sessions row for {vp_id}; run enrol first", 2
    )


DEFAULT_DEAD_ROW_TTL_S = 7 * 86400
DEFAULT_PROBE_NAME_PATTERNS = ("tuiprobe-*", "*-probe-*")
_ALIVE = frozenset({"live", "idle"})


def dead_row_policy(tables: Mapping[str, Any]) -> tuple[int, tuple[str, ...]]:
    """`[chief_of_staff].dead_row_ttl_seconds` and `.probe_name_patterns`, else the defaults."""
    section = tables.get("chief_of_staff")
    section = section if isinstance(section, dict) else {}
    ttl = section.get("dead_row_ttl_seconds")
    patterns = section.get("probe_name_patterns")
    if not (isinstance(ttl, int) and not isinstance(ttl, bool) and ttl >= 1):
        ttl = DEFAULT_DEAD_ROW_TTL_S
    if not (
        isinstance(patterns, list) and all(isinstance(item, str) for item in patterns)
    ):
        patterns = list(DEFAULT_PROBE_NAME_PATTERNS)
    return ttl, tuple(patterns)


def is_probe_name(slug: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(slug, pattern) for pattern in patterns)


def _dead_since(row: Mapping[str, Any]) -> dt.datetime | None:
    """When the row stopped being reachable: `gone_at`, else its `unreachable_since` stamp."""
    enrolment = row.get("enrolment") or {}
    stamp = (
        enrolment.get("gone_at")
        if enrolment.get("state") == STATE_GONE
        else row.get("unreachable_since")
    )
    return parse_iso(stamp)


def _reading(entry: Mapping[str, Any], at: str) -> dict[str, Any]:
    return {"liveness": entry["liveness"], "rung": entry.get("rung"), "at": at}


def merge_inventory(  # noqa: PLR0913  # the reconcile inputs, keyword-only after the doc
    doc: dict[str, Any],
    inventory: Iterable[Mapping[str, Any]],
    *,
    readable_machines: set[str],
    pair_name: str | None,
    now: dt.datetime,
    dead_row_ttl_s: int = DEFAULT_DEAD_ROW_TTL_S,
    probe_name_patterns: Iterable[str] = DEFAULT_PROBE_NAME_PATTERNS,
) -> dict[str, list[str]]:
    """Write or update one row per inventoried session; move absent rows to `gone`.

    A probe-named session is never enrolled. A row `gone` or unreachable for longer than
    `dead_row_ttl_s` is retired (removed); `unreachable_since` stamps when an unenrolled
    row's reading first went dead and is cleared when it is live or idle again.

    An `enrolled` row that is present is not modified (AC8). A row is moved to `gone` only
    when every inventory for ITS machine was read: a blind probe is not an absence (AC10).
    A `gone` row that reappears returns to the state it left.
    """
    at = iso(now)
    rows: list[dict[str, Any]] = doc["vp_sessions"]
    by_id = {row.get("vp_id"): row for row in rows}
    seen: set[str] = set()
    result: dict[str, list[str]] = {
        "discovered": [],
        "unchanged_enrolled": [],
        "returned": [],
        "gone": [],
    }
    probe_name_patterns = tuple(probe_name_patterns)
    for entry in inventory:
        if entry["role"] == ROLE_CHIEF:
            continue
        vp_id = entry["vp_id"]
        seen.add(vp_id)
        row = by_id.get(vp_id)
        if row is None:
            if is_probe_name(str(entry["slug"]), probe_name_patterns):
                continue
            row = {
                "vp_id": vp_id,
                "session": vp_id,
                "machine": entry["machine"],
                "pair": pair_name,
                "native_agent_name": entry["slug"],
                "tmux_target": entry.get("tmux_target"),
                "repo": entry.get("repo"),
                "role": entry["role"],
                "discovered_from": entry["sources"][0],
                "discovered_at": at,
                "last_seen": _reading(entry, at),
                "enrolment": {"state": STATE_DISCOVERED},
            }
            rows.append(row)
            by_id[vp_id] = row
            result["discovered"].append(vp_id)
            continue
        enrolment = row.setdefault("enrolment", {"state": STATE_DISCOVERED})
        if enrolment.get("state") == STATE_ENROLLED:
            result["unchanged_enrolled"].append(vp_id)
            continue
        if enrolment.get("state") == STATE_GONE:
            enrolment["state"] = enrolment.pop("previous_state", STATE_DISCOVERED)
            enrolment.pop("gone_at", None)
            result["returned"].append(vp_id)
        _stamp_unreachable(row, entry, at)
        row["last_seen"] = _reading(entry, at)
    for row in rows:
        vp_id = row.get("vp_id")
        enrolment = row.setdefault("enrolment", {"state": STATE_DISCOVERED})
        if (
            vp_id in seen
            or enrolment.get("state") == STATE_GONE
            or row.get("machine") not in readable_machines
        ):
            continue
        enrolment["previous_state"] = enrolment.get("state")
        enrolment["state"] = STATE_GONE
        enrolment["gone_at"] = at
        result["gone"].append(vp_id)
    result["retired"] = retire_dead_rows(rows, now, dead_row_ttl_s)
    return result


def retire_dead_rows(
    rows: list[dict[str, Any]], now: dt.datetime, ttl_s: int
) -> list[str]:
    """Remove, in place, every row dead longer than `ttl_s`; return the retired vp_ids."""
    cutoff = now - dt.timedelta(seconds=ttl_s)
    kept, retired = [], []
    for row in rows:
        since = _dead_since(row)
        if since is not None and since < cutoff:
            retired.append(row.get("vp_id"))
        else:
            kept.append(row)
    rows[:] = kept
    return retired


def _stamp_unreachable(row: dict[str, Any], entry: Mapping[str, Any], at: str) -> None:
    """Start the dead-row clock when a reading first goes dead; a live reading stops it.

    A row whose previous reading was already dead and carries no stamp (written before the
    stamp existed) is dated from that previous reading.
    """
    if entry["liveness"] in _ALIVE:
        row.pop("unreachable_since", None)
    elif "unreachable_since" not in row:
        previous = row.get("last_seen") or {}
        was_dead = previous.get("liveness") not in _ALIVE | {None}
        row["unreachable_since"] = (previous.get("at") if was_dead else None) or at


def _liveness_class(reading: Mapping[str, Any]) -> Any:
    """``stale(523107s)`` and ``stale(500738s)`` are one state: the age grows on every read."""
    liveness = reading.get("liveness")
    return liveness.split("(", 1)[0] if isinstance(liveness, str) else liveness


def enrolment_refused(
    row: Mapping[str, Any], records: Iterable[Mapping[str, Any]]
) -> bool:
    """A refusal applies only while its observed role, state and inventory reading hold.

    Reading timestamps change on every discovery; only liveness and transport rung are
    meaningful changes. Keep the registry state and the append-only ledger untouched.
    """
    for record in records:
        if (
            record.get("kind") != "enrolment-refusal"
            or record.get("vp_id") != row.get("vp_id")
            or record.get("reason") not in ENROLMENT_REFUSAL_REASONS
        ):
            continue
        observed = record.get("observed_state") or {}
        reading = row.get("last_seen") or {}
        previous = observed.get("last_seen") or {}
        if record["reason"] == "not-live" and reading.get("liveness") in {
            "live",
            "idle",
        }:
            continue
        if record["reason"] == "role-agent" and row.get("role") != ROLE_AGENT:
            continue
        if (
            row.get("role") == observed.get("role")
            and row.get("enrolment", {}).get("state")
            == observed.get("enrolment", {}).get("state")
            and _liveness_class(reading) == _liveness_class(previous)
            and reading.get("rung") == previous.get("rung")
        ):
            return True
    return False


def pending_enrolment(
    doc: Mapping[str, Any], records: Iterable[Mapping[str, Any]] = ()
) -> list[str]:
    """Rows that still need their one enrolment message."""
    records = tuple(records)
    return [
        row["vp_id"]
        for row in doc.get("vp_sessions", [])
        if row.get("role") == ROLE_VP
        and row.get("enrolment", {}).get("state") == STATE_DISCOVERED
        and not enrolment_refused(row, records)
    ]


# ---------------------------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------------------------


def chief_name(doc: Mapping[str, Any], fm_home: Path) -> str | None:
    """The chief's agent name: registry `chief_routes`, else `state/chief-session.json`."""
    routes = doc.get("chief_routes")
    if isinstance(routes, Mapping) and routes.get("native_agent_name"):
        return str(routes["native_agent_name"])
    try:
        session = json.loads(
            (fm_home / "state" / "chief-session.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    name = session.get("native_agent_name") if isinstance(session, dict) else None
    return str(name) if name else None


def template_body(template_text: str) -> str:
    """The first fenced `text` block under `## Template`."""
    start = template_text.index("## Template")
    fence = template_text.index("```text\n", start) + len("```text\n")
    end = template_text.index("```", fence)
    return template_text[fence:end]


def render_enrolment(template_text: str, values: Mapping[str, str]) -> str:
    body = template_body(template_text)
    missing = sorted(set(_PLACEHOLDER.findall(body)) - set(values))
    if missing:
        raise ChiefError("template-error", f"no value for {', '.join(missing)}", 2)
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], body)


def ready_to_render(row: Mapping[str, Any]) -> None:
    """Refuse anything but a `discovered` VP row: an enrolment is never re-sent (AC9, AC12)."""
    if row.get("role") != ROLE_VP:
        raise ChiefError(
            "not-a-vp",
            f"{row.get('vp_id')} has role {row.get('role')!r}; never messaged",
        )
    state = row.get("enrolment", {}).get("state")
    if state == STATE_ENROLLED:
        raise ChiefError("already-enrolled", f"{row['vp_id']} is already enrolled")
    if state == STATE_UNACKED:
        raise ChiefError(
            "already-sent",
            f"{row['vp_id']} was sent enrolment {row['enrolment'].get('message_id')}; "
            "silence is surfaced, never re-sent",
        )
    if state != STATE_DISCOVERED:
        raise ChiefError("not-discoverable", f"{row['vp_id']} is {state}")


def mark_sent(
    row: dict[str, Any], message_id: str, sent_via: str, now: dt.datetime
) -> None:
    row["enrolment"] = {
        "state": STATE_UNACKED,
        "message_id": message_id,
        "sent_via": sent_via,
        "sent_at": iso(now),
    }


def mark_acked(row: dict[str, Any], registration_id: str, now: dt.datetime) -> None:
    enrolment = row.get("enrolment", {})
    state = enrolment.get("state")
    if state == STATE_ENROLLED:
        raise ChiefError("already-enrolled", f"{row['vp_id']} is already enrolled")
    if state != STATE_UNACKED:
        raise ChiefError("not-sent", f"{row['vp_id']} has no enrolment awaiting an ack")
    if enrolment.get("message_id") != registration_id:
        raise ChiefError(
            "registration-id-mismatch",
            f"{row['vp_id']} was sent {enrolment.get('message_id')}, ack names {registration_id}",
        )
    enrolment["state"] = STATE_ENROLLED
    enrolment["acked_at"] = iso(now)


# ---------------------------------------------------------------------------------------------
# The dispatch ledger and the cross-machine send record
# ---------------------------------------------------------------------------------------------


def ledger_path(fm_home: Path) -> Path:
    return fm_home / "dispatch.jsonl"


def read_ledger(fm_home: Path) -> list[dict[str, Any]]:
    path = ledger_path(fm_home)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def append_ledger(fm_home: Path, record: Mapping[str, Any]) -> None:
    with ledger_path(fm_home).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def check_condition(condition: str) -> None:
    if condition not in TRIGGERS:
        raise ChiefError(
            "unknown-condition",
            f"condition {condition!r} is not a D-18 trigger ({', '.join(TRIGGERS)})",
            2,
        )


_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_BOARD_ID = r"[0-9]+"

#: The id each rung hands back for a sent message (ISSUE-REF): a SendMessage UUID, an
#: agent-mail board integer, a git-inbox `<SENT>-<slug>-<machine>-<4 hex>` header id. A rung
#: not named here takes the fallback, a UUID or a board integer.
MESSAGE_ID_SHAPES: dict[str, re.Pattern[str]] = {
    "native": re.compile(_UUID),
    "agent-mail": re.compile(_BOARD_ID),
    "git-inbox": re.compile(r"\d{4}-\d{2}-\d{2}T\d{4}Z-[a-z0-9][a-z0-9-]*-[0-9a-f]{4}"),
}
_FALLBACK_MESSAGE_ID = re.compile(f"{_UUID}|{_BOARD_ID}")


def check_message_id(message_id: str, rung: str) -> None:
    """Refuse an id the rung could not have issued, so a placeholder never takes the dedupe slot."""
    shape = MESSAGE_ID_SHAPES.get(rung, _FALLBACK_MESSAGE_ID)
    if not shape.fullmatch(message_id):
        raise ChiefError(
            "bad-message-id",
            f"--message-id {message_id!r} is not an id the {rung} rung issues "
            f"(want {shape.pattern})",
            2,
        )


#: Subject prefix of a quota-regime notice (`ISSUE-REF/<regime>`). The subject carries the
#: regime, so a session told A, then B, then A has an old A row that no longer describes
#: what it was last told (ISSUE-REF).
REGIME_SUBJECT_PREFIX = "ISSUE-REF/"


def canonical_vp_id(vp_id: str, machine: str) -> str:
    """Normalize a session address; a bare name is this machine's session."""
    vp_id = vp_id.strip()
    return vp_id if not vp_id or "@" in vp_id else f"{vp_id}@{machine}"


def already_sent(  # noqa: PLR0913  # send identity and comparison options
    records: Iterable[Mapping[str, Any]],
    vp_id: str,
    condition: str,
    subject: str,
    *,
    newest_regime_only: bool = True,
    machine: str | None = None,
) -> Mapping[str, Any] | None:
    """The standing send for this `(session, condition)` about this subject, if any (D-18).

    A row a later row of the same send names in `supersedes` no longer stands (ISSUE-REF).
    A regime notice stands only while it is the session's newest regime notice: once a
    later row names a different regime, the session is owed this one again (ISSUE-REF).
    `newest_regime_only=False` ignores that rule, for naming the row a send supersedes.
    """
    records = list(records)
    if machine is not None:
        vp_id = canonical_vp_id(vp_id, machine)

    def same_session(record: Mapping[str, Any]) -> bool:
        recorded_id = record.get("vp_id")
        if machine is not None and isinstance(recorded_id, str):
            recorded_id = canonical_vp_id(recorded_id, machine)
        return recorded_id == vp_id

    if (
        newest_regime_only
        and condition == "fleet-gate"
        and subject.startswith(REGIME_SUBJECT_PREFIX)
    ):
        newest = [
            record
            for record in records
            if record.get("kind") == "cross-machine-send"
            and same_session(record)
            and record.get("condition") == condition
            and str(record.get("subject", "")).startswith(REGIME_SUBJECT_PREFIX)
        ]
        if newest and newest[-1].get("subject") != subject:
            return None
    matching = [
        record
        for record in records
        if record.get("kind") == "cross-machine-send"
        and same_session(record)
        and record.get("condition") == condition
        and record.get("subject") == subject
    ]
    superseded = {
        record["supersedes"] for record in matching if record.get("supersedes")
    }
    for record in matching:
        if record.get("message_id") not in superseded:
            return record
    return None


def first_skip_reason(resolution: Mapping[str, Any]) -> str:
    for step in resolution.get("evaluated") or []:
        if not step.get("available"):
            return str(step.get("reason") or "unknown")
    return "no-rung-evaluated"


def check_resolution(
    resolution: Any, target: str, *, machine: str | None = None
) -> dict[str, Any]:
    """A `message_transports_cli.py resolve --json` document for exactly this target."""
    if not isinstance(resolution, dict) or not isinstance(
        resolution.get("evaluated"), list
    ):
        raise ChiefError("bad-resolution", "resolution is not a resolver document", 2)
    resolved_target = resolution.get("target")
    if machine is not None:
        target = canonical_vp_id(target, machine)
        if isinstance(resolved_target, str):
            resolved_target = canonical_vp_id(resolved_target, machine)
    if resolved_target != target:
        raise ChiefError(
            "bad-resolution",
            f"resolution is for {resolved_target!r}, not {target!r}",
            2,
        )
    return resolution


def ladder_for(config: Mapping[str, Any], resolution: Mapping[str, Any]) -> list[str]:
    """The tracked ladder (`config/message-transports.json` `ladders`) the resolution walked."""
    ladders = config.get("ladders") or {}
    return [str(rung) for rung in ladders.get(resolution.get("scope"), [])]


def send_rung(
    resolution: Mapping[str, Any], sent_via: str | None, ladder: Iterable[str] = ()
) -> str:
    """The rung the message goes on, or `ChiefError(unreachable)` when none resolved (AC4).

    `sent_via` lets the chief record that it sent on a rung other than the resolver's pick
    (the git-inbox-by-hand step after the pick proved unusable, D-17). It may name any rung
    of the tracked `ladder`, evaluated or not: the resolver stops at its first available
    rung, so a fall-through to a lower rung is never in `evaluated` (ISSUE-REF). It is
    never accepted when no rung resolved: with nothing resolved, nothing is improvised.
    """
    rung = resolution.get("rung")
    if not rung:
        raise ChiefError("unreachable", first_skip_reason(resolution), 3)
    if sent_via is None:
        return str(rung)
    if sent_via not in set(ladder):
        raise ChiefError(
            "bad-rung",
            f"{sent_via!r} is not a rung of the {resolution.get('scope')} ladder",
            2,
        )
    return sent_via


# ---------------------------------------------------------------------------------------------
# Remote goal outcomes and the goal-waker note (D-21, D-23; ISSUE-REF)
# ---------------------------------------------------------------------------------------------

ROUTE_REMOTE_INJECT_GOAL = "remote-inject-goal"
ROUTE_D2_FALLBACK = "d2-dispatch-fallback"
#: The target's refusals after which the D-2 dispatch asking the VP to run `/start-goal` itself
#: is the one allowed fallback (D-21 AC2). Every other refusal is surfaced, never routed around.
FALLBACK_REASONS = frozenset({"verb-not-allowed", "library-absent"})
#: start-goal's `status` classes (issue-ref § Goal outcome surfacing). From a reply plus a
#: heartbeat only `running`, `stalled` and `lost` are decidable: the others need the target's
#: transcript, which no route reaches from here.
GOAL_CLASSES = (
    "running",
    "complete",
    "blocked",
    "terminal",
    "exhausted",
    "out-of-scope",
    "stalled",
    "lost",
)
_LANDED_VERDICTS = frozenset({"landed", "queued"})
_SURFACED_CLASSES = frozenset({"stalled", "lost"})
DEFAULT_WAKER_NO_PROGRESS_THRESHOLD = 3
_BUDGET_LINE = re.compile(r"\sGW-BUDGET\s.*\bsession=(\S+).*\bepoch=(\S+)")


def remote_goal_dispatches(
    records: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return [
        record
        for record in records
        if record.get("kind") == "dispatch"
        and record.get("route") == ROUTE_REMOTE_INJECT_GOAL
        and record.get("request_id")
    ]


def latest_results(
    results: Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """`request_id` -> the newest MATCHED reply. An unmatched reply is never read as an answer."""
    latest: dict[str, Mapping[str, Any]] = {}
    for record in results:
        if record.get("matched") is True and isinstance(record.get("request_id"), str):
            latest[str(record["request_id"])] = record
    return latest


def classify_remote_goal(
    dispatch: Mapping[str, Any],
    result: Mapping[str, Any] | None,
    liveness: str | None,
) -> dict[str, Any]:
    """One remote goal, from its reply and the target's `fleet-liveness` word only.

    `delivery` is how far the start got; `class` is start-goal's vocabulary once it landed, or
    None when the inputs cannot decide (no reply yet, a delivery failure, a stale heartbeat).
    """
    goal: dict[str, Any] = {
        "dispatch_id": dispatch.get("dispatch_id"),
        "vp_id": dispatch.get("vp_id"),
        "request_id": dispatch.get("request_id"),
        "route": ROUTE_REMOTE_INJECT_GOAL,
        "liveness": liveness,
        "delivery": "awaiting-result",
        "class": None,
        "reason": None,
        "goal_id": None,
    }
    if result is None:
        return goal
    decision = result.get("decision")
    goal["reason"] = result.get("verdict_reason") or result.get("reason")
    goal["goal_id"] = result.get("goal_id")
    if decision == "refused":
        goal["delivery"] = "refused"
        if result.get("reason") in FALLBACK_REASONS:
            goal["route"] = ROUTE_D2_FALLBACK
        return goal
    if decision != "accepted":
        goal["delivery"] = str(decision or "unknown")
        return goal
    verdict = result.get("verdict")
    goal["delivery"] = str(verdict or result.get("reason") or "unknown")
    if verdict in _LANDED_VERDICTS:
        if liveness == "live":
            goal["class"] = "running"
        elif liveness == "idle":
            goal["class"] = "stalled"
        elif liveness in {None, "gone"}:
            goal["class"] = "lost"
    return goal


def goal_items(goal: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What a goal contributes to its VP's digest: an end, a stall, or a failed start."""
    if goal["route"] == ROUTE_D2_FALLBACK:
        condition = ROUTE_D2_FALLBACK
    elif goal["class"] in _SURFACED_CLASSES:
        condition = f"goal-{goal['class']}"
    elif goal["delivery"] not in {"awaiting-result", "dry-run", *_LANDED_VERDICTS}:
        condition = str(goal["delivery"])
    else:
        return []
    return [
        {
            "condition": condition,
            "key": f"{goal['dispatch_id']}:{condition}",
            "dispatch_id": goal["dispatch_id"],
            "detail": goal["reason"],
        }
    ]


def waker_threshold(tables: Mapping[str, Any]) -> int:
    """`[chief_of_staff].waker_no_progress_threshold` from the shared loader, else 3 (D-23)."""
    section = tables.get("chief_of_staff")
    value = (
        section.get("waker_no_progress_threshold")
        if isinstance(section, dict)
        else None
    )
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return DEFAULT_WAKER_NO_PROGRESS_THRESHOLD


def waker_root(env: Mapping[str, str]) -> Path:
    """`$XDG_STATE_HOME/ai-harness/goal-waker`, as `goal_waker.state_dir()` resolves it."""
    base = (env.get("XDG_STATE_HOME") or "").strip()
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "ai-harness" / "goal-waker"


def budget_epochs(log_text: str) -> set[tuple[str, str]]:
    """`(session label, epoch)` for every `GW-BUDGET` line the waker logged."""
    return {
        (match.group(1), match.group(2))
        for line in log_text.splitlines()
        if (match := _BUDGET_LINE.search(f" {line}"))
    }


def waker_items(  # noqa: PLR0913  # every input injected
    sessions_dir: Path,
    log_text: str,
    *,
    machine: str,
    threshold: int,
    name_of: Any,
) -> dict[str, list[dict[str, Any]]]:
    """`vp_id` -> the waker conditions its own record shows NOW, each keyed by its crossing.

    `waker-no-progress`: `nudges_in_epoch >= threshold` with no `last_progress_at` newer than
    `last_nudge_at`; the crossing is (epoch, last progress), so a session that progressed and
    then burned the threshold again is a new crossing. `waker-exhausted`: a `GW-BUDGET` line for
    the record's current epoch. Only this host's waker writes these files, so they cover this
    machine's sessions; a remote VP's waker record has no route here.
    """
    budgets = budget_epochs(log_text)
    found: dict[str, list[dict[str, Any]]] = {}
    try:
        paths = sorted(sessions_dir.glob("*.json"))
    except OSError:
        return found
    for path in paths:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict):
            continue
        session_id = path.stem
        label = name_of(session_id) or session_id
        vp_id = session_address(machine, label)
        epoch = state.get("epoch_offset")
        nudges = state.get("nudges_in_epoch")
        last_nudge = state.get("last_nudge_at")
        progress = state.get("last_progress_at")
        items = []
        if (
            isinstance(nudges, int)
            and nudges >= threshold
            and last_nudge is not None
            and (progress is None or progress < last_nudge)
        ):
            items.append(
                {
                    "condition": "waker-no-progress",
                    "key": f"{session_id}:{epoch}:{progress}",
                    "detail": f"{nudges} wakes this epoch with no progress",
                }
            )
        if (
            epoch is not None
            and {(label, str(epoch)), (session_id, str(epoch))} & budgets
        ):
            items.append(
                {
                    "condition": "waker-exhausted",
                    "key": f"{session_id}:{epoch}",
                    "detail": f"GW-BUDGET for epoch {epoch}",
                }
            )
        if items:
            found.setdefault(vp_id, []).extend(items)
    return found


def surfaced_keys(records: Iterable[Mapping[str, Any]]) -> set[tuple[str, str, str]]:
    return {
        (str(r.get("vp_id")), str(r.get("condition")), str(r.get("key")))
        for r in records
        if r.get("kind") == "digest-note"
    }


def new_digests(
    items_by_vp: Mapping[str, list[dict[str, Any]]],
    surfaced: set[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    """ONE digest per VP holding only items not surfaced before (D-18 one per condition)."""
    digests = []
    for vp_id in sorted(items_by_vp):
        fresh = [
            item
            for item in items_by_vp[vp_id]
            if (vp_id, item["condition"], item["key"]) not in surfaced
        ]
        if fresh:
            digests.append({"vp_id": vp_id, "items": fresh})
    return digests


# ---------------------------------------------------------------------------------------------
# Starting a native goal in a VP (ISSUE-REF D-8)
# ---------------------------------------------------------------------------------------------

#: Goal classes the chief tells the maintainer about; `running` and `not-native` are table rows only.
GOAL_ESCALATION_CLASSES = (
    "complete",
    "blocked",
    "terminal",
    "exhausted",
    "out-of-scope",
    "stalled",
    "lost",
)

LOOP_OWNER_QUEUE = "queue-mode"
LOOP_OWNER_FIRSTMATE = "firstmate"


def goal_target_spec(row: Mapping[str, Any], machine: str) -> str:
    """`session:<name>` for a VP on this machine; anything else is refused before any step."""
    vp_id = row.get("vp_id")
    if row.get("role") != ROLE_VP:
        raise ChiefError(
            "not-a-vp",
            f"{vp_id} has role {row.get('role')!r}; a goal is never started there",
        )
    if row.get("enrolment", {}).get("state") == STATE_GONE:
        raise ChiefError(
            "vp-gone", f"{vp_id} is gone; there is no pane to start a goal in"
        )
    if row.get("machine") != machine:
        raise ChiefError(
            "remote-vp",
            f"{vp_id} is on {row.get('machine')}; starting a goal across machines is the "
            "paired chief's (ISSUE-REF), not this verb's",
        )
    name = row.get("native_agent_name")
    if not name:
        raise ChiefError(
            "no-session-name", f"{vp_id} has no native_agent_name to target", 2
        )
    return f"session:{name}"


def check_goal_guards(row: Mapping[str, Any]) -> None:
    """The chief's two own guards (D-8): never over a secondmate, never over a queue-mode owner.

    The active-native-goal guard is `start_goal.py preflight`'s, which reads the transcript.
    Both refuse before anything is staged. `loop_owner` is the registry row's mirror of
    `config/work_queue.toml` `[loop] owner`; a row that records none is not queue-owned.
    """
    vp_id = row.get("vp_id")
    if (
        row.get("firstmate_task_id")
        or row.get("firstmate_home")
        or row.get("loop_owner") == LOOP_OWNER_FIRSTMATE
    ):
        raise ChiefError(
            "secondmate-supervised",
            f"{vp_id} is a Firstmate-registered secondmate; Firstmate is its sole supervisor "
            "and the chief never starts a goal over it",
        )
    if row.get("loop_owner") == LOOP_OWNER_QUEUE:
        raise ChiefError(
            "queue-mode-owned",
            f"{vp_id} is owned by work-queue queue mode ([loop] owner = queue-mode); "
            "the chief never starts a goal over a leased VP",
        )


def dispatch_closed(records: Iterable[Mapping[str, Any]], dispatch_id: str) -> bool:
    """A goal dispatch is closed by an `acked` record, or by a `refused` one."""
    return any(
        r.get("dispatch_id") == dispatch_id and r.get("kind") in ("acked", "refused")
        for r in records
    )


def goal_escalations(
    entries: Iterable[Mapping[str, Any]],
    vp_by_session: Mapping[str, str],
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Goal rows to tell the maintainer about, once per `(VP, dispatch id)` (design D-4).

    A row started outside a chief dispatch has no dispatch id, so its goal id stands in for it.
    """
    done = {
        (r.get("vp_id"), r.get("dispatch_id"))
        for r in records
        if r.get("kind") == "escalated"
    }
    fresh: list[dict[str, str]] = []
    for entry in entries:
        if entry.get("class") not in GOAL_ESCALATION_CLASSES:
            continue
        session = str(entry.get("target_session") or "")
        vp_id = vp_by_session.get(session, session)
        key_id = str(entry.get("dispatch_id") or entry.get("goal_id"))
        if (vp_id, key_id) in done:
            continue
        done.add((vp_id, key_id))
        fresh.append({"vp_id": vp_id, "dispatch_id": key_id, "class": entry["class"]})
    return fresh


# ---------------------------------------------------------------------------------------------
# The fleet join (ISSUE-REF D-9, T-05)
# ---------------------------------------------------------------------------------------------

#: `goal.state` vocabulary (plan D-9).
GOAL_STATES = (
    "none",
    "running",
    "stalled",
    "exited-complete",
    "exited-blocked",
    "exited-exhausted",
    "exited-out-of-scope",
    "terminal-declared",
    "unknown",
)
#: start-goal's terminal class -> the fleet `goal.state` it reads as.
_DECLARED_STATE = {
    "complete": "exited-complete",
    "blocked": "exited-blocked",
    "exhausted": "exited-exhausted",
    "out-of-scope": "exited-out-of-scope",
    "terminal": "terminal-declared",
}
#: The provisional ack window (procedure § Ack contract and escalation window).
ACK_WINDOW_S = 600
CLASS_GONE = "gone"
ATTENTION_CLASSES = frozenset(
    {"idle-without-work", "idle-with-work", "unreachable", CLASS_GONE}
)
ATTENTION_ENROLMENT = frozenset({STATE_DISCOVERED, STATE_UNACKED})
_GOAL_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_PHRASE_BYTES = b"GOAL "
_TOOL_USE_BYTES = b'"tool_use"'


def input_status(source: str, error: str | None = None) -> dict[str, Any]:
    """One `inputs.<name>` entry: `ok`, or `failed` with the verbatim error."""
    return {
        "status": "ok" if error is None else "failed",
        "error": error,
        "source": source,
    }


def parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def _has_tool_use(record: Mapping[str, Any]) -> bool:
    message = record.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    return isinstance(content, list) and any(
        isinstance(block, Mapping) and block.get("type") == "tool_use"
        for block in content
    )


def _json_record(raw: bytes) -> dict[str, Any] | None:
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _lines_forward(path: Path) -> Iterator[tuple[int, bytes]]:
    """`(byte offset, line)` oldest first, one line resident at a time (ISSUE-REF)."""
    offset = 0
    with path.open("rb") as handle:
        for raw in handle:
            yield offset, raw
            offset += len(raw)


def _lines_backward(path: Path) -> Iterator[tuple[int, bytes]]:
    """`(byte offset, line)` newest first, in bounded chunks (the goal-waker's reader)."""
    from ai_chassis import goal_waker  # noqa: PLC0415 - loads scripts by path

    if not path.stat().st_size:
        return iter(())
    return goal_waker._script("wq_client")._transcript_lines_backwards(path)


def _newest_progress(path: Path) -> dt.datetime | None:
    for _, raw in _lines_backward(path):
        if _TOOL_USE_BYTES not in raw:
            continue
        record = _json_record(raw)
        if record and record.get("type") == "assistant" and _has_tool_use(record):
            moment = parse_iso(record.get("timestamp"))
            if moment is not None:
                return moment
    return None


def _newest_goal(
    lines: Iterable[tuple[int, bytes]], ledger: Any
) -> tuple[int | None, int | None, tuple[str, int] | None]:
    """`(goal set at, cleared at, (declared state, at))` for the newest `/goal` record."""
    marker = ledger.GOAL_COMMAND.encode()
    goal_at: int | None = None
    cleared_at: int | None = None
    declared: tuple[str, int] | None = None
    for start, raw in lines:
        if not (marker in raw or (_PHRASE_BYTES in raw and goal_at is not None)):
            continue
        record = _json_record(raw)
        if record is None:
            continue
        text = ledger._message_text(record) or ""
        if record.get("type") == "user" and ledger.GOAL_COMMAND in text:
            args = _GOAL_ARGS.search(text)
            if args and args[1].strip() == "clear":
                cleared_at = start
            else:
                goal_at, cleared_at, declared = start, None, None
        elif record.get("type") == "assistant" and goal_at is not None:
            found = ledger._phrase_class(text)
            if found:
                declared = (_DECLARED_STATE[found[0]], start)
    return goal_at, cleared_at, declared


def transcript_goal(
    path: Path, ledger: Any, *, now: dt.datetime, stall_after_s: float
) -> dict[str, Any]:
    """Goal state and last progress read from one transcript (D-9); OSError propagates.

    `ledger` is /start-goal's `ledger` module: its `GOAL_COMMAND` marker finds the newest `/goal`
    record and its `_phrase_class` reads `GOAL TERMINAL:` and the terminal phrases in assistant
    text after it, the latest winning. `/goal clear` with no declaration after the set reads
    `none`. Progress is the goal-waker's: the newest assistant record carrying a `tool_use`.
    Neither read holds the transcript: a live one passes 1 GB (ISSUE-REF).
    """
    progress = _newest_progress(path)
    goal_at, cleared_at, declared = _newest_goal(_lines_forward(path), ledger)
    idle_for = None if progress is None else (now - progress).total_seconds()
    if goal_at is None:
        state, matched = "none", None
    elif declared is not None:
        state, matched = declared
    elif cleared_at is not None:
        state, matched = "none", cleared_at
    elif idle_for is not None and idle_for > stall_after_s:
        state, matched = "stalled", goal_at
    else:
        state, matched = "running", goal_at
    return {
        "state": state,
        "evidence": {"transcript": str(path), "offset": matched},
        "last_progress_at": None if progress is None else iso(progress),
        "idle_for_s": None if idle_for is None else int(idle_for),
    }


def pending_acks(
    records: list[Mapping[str, Any]],
    vp_id: str,
    now: dt.datetime,
    window_s: int = ACK_WINDOW_S,
) -> list[dict[str, Any]]:
    """Every enrolment or goal dispatch to `vp_id` the ledger shows no ack (or refusal) for."""
    acked = {r.get("message_id") for r in records if r.get("kind") == "enrolment-ack"}
    found = []
    for record in records:
        if record.get("vp_id") != vp_id:
            continue
        kind = record.get("kind")
        if kind == "enrolment" and record.get("message_id") not in acked:
            ident, sent = record.get("message_id"), record.get("sent_at")
        elif (
            kind in {"staged", "dispatch"}
            and record.get("dispatch_id")
            and not dispatch_closed(records, str(record["dispatch_id"]))
        ):
            ident = record["dispatch_id"]
            sent = record.get("staged_at") or record.get("sent_at")
        else:
            continue
        moment = parse_iso(sent)
        age = None if moment is None else int((now - moment).total_seconds())
        found.append(
            {
                "kind": kind,
                "id": ident,
                "sent_at": sent,
                "age_s": age,
                "past_window": age is not None and age > window_s,
            }
        )
    return found


def fleet_classification(
    liveness: str | None, *, has_work: bool | None, awaiting_ack: bool | None
) -> str | None:
    """`classify`, or None when an input it needs could not be read; `gone` stays `gone`."""
    if awaiting_ack:
        return "awaiting-ack"
    if liveness is None or awaiting_ack is None:
        return None
    if liveness == CLASS_GONE:
        return CLASS_GONE
    if liveness == "idle" and has_work is None:
        return None
    return classify(liveness, has_work=bool(has_work), awaiting_ack=False)


def _class_inputs(row: Mapping[str, Any], machine: str) -> tuple[str, ...]:
    """The inputs a row's classification is derived from (`row()` in the CLI's fleet join)."""
    local = row.get("machine") == machine
    names = (
        "local_sessions" if local else "remote_liveness",
        "registry",
        "dispatch_ledger",
    )
    if str(row.get("classification")).startswith("idle-"):
        names += ("beads", "transcripts") if local else ("beads",)
    return names


def fleet_attention(
    sessions: Iterable[Mapping[str, Any]],
    held: Mapping[str, Any],
    *,
    inputs: Mapping[str, Mapping[str, Any]],
    machine: str,
    refused_enrolment: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Everything to act on, from the fields each row carries; a null field adds nothing.

    A reason is emitted only when every input it is derived from reads `ok` (D-9), so one
    failed input suppresses its reasons fleet-wide rather than leaving a partial list that
    reads as complete. Enrolment and ack reasons point at the registry's and the dispatch
    ledger's `source`.
    """

    def ok(*names: str) -> bool:
        return all(inputs[name]["status"] == "ok" for name in names)

    refused_enrolment = set(refused_enrolment)
    out: list[dict[str, Any]] = []
    for row in sessions:
        vp_id = row["vp_id"]
        goal = row.get("goal") or {}
        where = (goal.get("evidence") or {}).get("transcript") or row.get("worktree")
        if row.get("classification") in ATTENTION_CLASSES and ok(
            *_class_inputs(row, machine)
        ):
            out.append({"vp_id": vp_id, "reason": row["classification"], "path": where})
        state = goal.get("state")
        if (
            isinstance(state, str)
            and (state.startswith("exited-") or state == "stalled")
            and ok("local_sessions", "transcripts")
        ):
            out.append({"vp_id": vp_id, "reason": f"goal-{state}", "path": where})
        enrolment = (row.get("enrolment") or {}).get("state")
        if (
            enrolment in ATTENTION_ENROLMENT
            and ok("registry")
            and not (enrolment == STATE_DISCOVERED and vp_id in refused_enrolment)
        ):
            out.append(
                {
                    "vp_id": vp_id,
                    "reason": f"enrolment-{enrolment}",
                    "path": inputs["registry"]["source"],
                }
            )
        out.extend(
            {
                "vp_id": vp_id,
                "reason": f"ack-window-elapsed: {ack['kind']} {ack['id']}",
                "path": inputs["dispatch_ledger"]["source"],
            }
            for ack in row.get("pending_acks") or []
            if ack["past_window"] and ok("dispatch_ledger")
        )
    # `inputs.held` is one status per store (plan D-11): an item is derived from its own
    # store's read, so one failed store suppresses only its own items.
    held_inputs = inputs["held"]
    for status in ("open", "answered"):
        out.extend(
            {
                "vp_id": item["frontmatter"].get("session"),
                "reason": f"held-{status}",
                "path": item["path"],
            }
            for item in held.get(status) or []
            if (held_inputs.get(item.get("store")) or {}).get("status") == "ok"
        )
    return out
