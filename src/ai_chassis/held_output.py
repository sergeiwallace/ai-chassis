"""Held items: the files under `docs/held/` that the maintainer must act on (ISSUE-REF, T-04).

Contract: `docs/held/README.md`; decisions D-4 (layout), D-5 (lifecycle) and D-7 (shipping) in
`docs/plans/issue-ref-chief-of-staff-fleet-coordination-plan.md`. Entry point:
`scripts/held_cli.py`, which owns every git and forge call; this module only renders, parses,
validates and lists, so `chief_of_staff_cli.py fleet` can read the same items without a git
dependency.

The frontmatter is a fixed flat schema this module writes itself, so it is parsed by a strict
reader rather than a YAML library: `null`, an integer, a double-quoted (JSON-escaped) string, or a
bare token, with an optional trailing `# comment` after a bare value. Anything else is malformed
and is reported as such, never guessed at.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ai_chassis.session_address import (
    AddressError,
    Address,
    address_from_board_name,
    board_name,
    parse_address,
)

SCHEMA_VERSION = 1
HELD_DIR = Path("docs") / "held"
README_NAME = "README.md"
#: File-name endings of the files directly in `docs/held/` that are never items, skipped like
#: `README_NAME`: the per-session aggregate held review doc `<host>--<slug>-held-review.md`
#: (ISSUE-REF D-2), then the decisions-only digest `<host>--<slug>-open-decisions.md` it replaced
#: (ISSUE-REF), kept reserved so a store still carrying one parses it as neither item nor
#: malformed. Only that top level is reserved: an item lives one directory down, in its session's
#: directory, and keeps its name whatever its title ends in (`is_reserved`, ISSUE-REF).
#: The per-session to-review message `<host>--<slug>-to-review.md` (ISSUE-REF D-4) is reserved
#: the same way: a view over items, never one.
HELD_REVIEW_SUFFIX = "-held-review.md"
LEGACY_OPEN_DECISIONS_SUFFIX = "-open-decisions.md"
TO_REVIEW_SUFFIX = "-to-review.md"
#: Every reserved ending, for `str.endswith`; name a suffix by its constant, never by position.
RESERVED_SUFFIXES = (HELD_REVIEW_SUFFIX, LEGACY_OPEN_DECISIONS_SUFFIX, TO_REVIEW_SUFFIX)
ANSWER_HEADING = "## Answer"
#: The held kind registry (ISSUE-REF D-1): per kind, its TEMPLATE, STUB, producers, validator
#: and whether it supersedes. Read at call time by `load_kind_registry`.
REGISTRY_PATH = Path(__file__).resolve().parents[2] / "config" / "held_kinds.toml"
#: Names another registry file in place of `REGISTRY_PATH` (a test's edited copy).
REGISTRY_ENV = "AIH_HELD_KINDS_REGISTRY"

KINDS = (
    "decision",
    "blocker",
    "exit-goal",
    "progress-review",
    "summary",
    "fleet-brief",
    "other",
)
STATUS_OPEN = "open"
STATUS_ANSWERED = "answered"
STATUSES = (STATUS_OPEN, STATUS_ANSWERED)

#: Exactly these keys, in this order (README § File format).
KEYS = (
    "held_schema",
    "kind",
    "status",
    "session",
    "machine",
    "task",
    "title",
    "ask",
    "source_skill",
    "created_at",
    "answered_at",
    "answer_recorded_in",
)
#: Optional keys, rendered after `KEYS` in this order and only when set (ISSUE-REF D-3, D-7):
#: an item escalated to a decider instead of the maintainer names its `decider` and `escalation_id`,
#: the asker's `recommended` option, and once answered the decider's `chosen` option, its
#: `decider_model`, and whether the decider's framework run sent it on to the maintainer.
OPTIONAL_KEYS = (
    "decider",
    "escalation_id",
    "recommended",
    "chosen",
    "decider_model",
    "escalated_to_owner",
)
#: The optional keys only an answer sets.
ANSWER_KEYS = ("chosen", "decider_model", "escalated_to_owner")
_QUOTED_KEYS = frozenset(
    {"title", "ask", "source_skill", "answer_recorded_in", "recommended", "chosen"}
)
_BOOL_KEYS = frozenset({"escalated_to_owner"})
#: A decider: a `<slug>@<host>` session, or `<kind>:<id>` for a provider or a delegated agent
#: (`provider:jev`, `agent:codex:sol`).
DECIDER_RE = re.compile(r"^[a-z][a-z0-9-]*:[a-z0-9][a-z0-9._:-]*$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

_SEGMENTS = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_HALF_RE = re.compile(rf"^{_SEGMENTS}$")
TOPIC_RE = re.compile(rf"^{_SEGMENTS}$")
TASK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9][A-Za-z0-9._-]*$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_KIND_ALTERNATION = "|".join(
    sorted((re.escape(k) for k in KINDS), key=len, reverse=True)
)
FILENAME_RE = re.compile(
    rf"^(?P<date>\d{{4}}-\d{{2}}-\d{{2}})-(?P<nn>\d{{2,}})-(?P<kind>{_KIND_ALTERNATION})"
    rf"-(?P<topic>{_SEGMENTS})\.md$"
)
_FM_LINE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*):[ \t]*(?P<value>.*)$")
_INT = re.compile(r"^-?\d+$")
_BARE_COMMENT = re.compile(r"\s+#.*$")
TOPIC_MAX_WORDS = 6
TOPIC_MAX_CHARS = 60
#: `docs`, `held`, `<session>`, `<file>`.
_ITEM_DEPTH = 4


class HeldError(Exception):
    """A request refused or a file rejected, carrying the field it is about."""

    def __init__(self, key: str, detail: str, exit_code: int = 2) -> None:
        super().__init__(f"{key}: {detail}")
        self.key = key
        self.detail = detail
        self.exit_code = exit_code


class KindRegistryError(HeldError):
    """`config/held_kinds.toml` is unreadable, malformed, or out of step with `KINDS`."""

    def __init__(self, detail: str) -> None:
        super().__init__("kind_registry", detail)


class HeldRootMissing(Exception):
    """`docs/held/` does not exist at the root asked about; never an empty listing."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"held_root_missing: {path}")
        self.path = path


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------------------------
# Field grammar
# ---------------------------------------------------------------------------------------------


def _session_address(session: str) -> Address:
    """The one concrete `<slug>@<host>` address ``session`` names (plan D-12), or refuse."""
    try:
        address = parse_address(session or "")
    except AddressError:
        address = None
    if (
        address is None
        or session != str(address)
        or not _HALF_RE.match(address.slug)
        or not _HALF_RE.match(address.machine)
    ):
        raise HeldError(
            "session",
            f"{session!r} is not a <slug>@<host> session address "
            "(lowercase a-z0-9 segments joined by '-', slug and host joined by '@')",
        )
    return address


def check_session(session: str) -> tuple[str, str]:
    """`(machine, slug)` of a `<slug>@<host>` session address, or refuse naming the field."""
    address = _session_address(session)
    return address.machine, address.slug


def session_dir(session: str) -> str:
    """The on-disk directory name of a session: the board form `<host>--<slug>` (D-12)."""
    return board_name(_session_address(session))


def session_from_dir(name: str) -> str:
    """The `<slug>@<host>` address a `<host>--<slug>` directory name stands for, or refuse."""
    address = address_from_board_name(name)
    if address is None or board_name(_session_address(str(address))) != name:
        raise HeldError(
            "session", f"{name!r} is not a <host>--<slug> session directory"
        )
    return str(address)


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise HeldError("kind", f"{kind!r} is not one of {', '.join(KINDS)}")
    return kind


_REGISTRY_REQUIRED = {
    "name": str,
    "template": str,
    "stub": str,
    "source_skills": list,
    "supersedes": bool,
}
_REGISTRY_OPTIONAL = {"validator": str}


def _registry_row(index: int, row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise KindRegistryError(f"row {index} is not a table")
    for key, kind in _REGISTRY_REQUIRED.items():
        if not isinstance(row.get(key), kind):
            raise KindRegistryError(
                f"row {index} ({row.get('name')!r}): {key!r} is missing or not a {kind.__name__}"
            )
    for key, kind in _REGISTRY_OPTIONAL.items():
        if key in row and not isinstance(row[key], kind):
            raise KindRegistryError(
                f"row {index} ({row['name']!r}): {key!r} is not a {kind.__name__}"
            )
    extra = sorted(set(row) - set(_REGISTRY_REQUIRED) - set(_REGISTRY_OPTIONAL))
    if extra:
        raise KindRegistryError(
            f"row {index} ({row['name']!r}): unknown key(s) {', '.join(extra)}"
        )
    return dict(row)


def load_kind_registry(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """``{kind: row}`` from `config/held_kinds.toml`, read now; refuses any drift from `KINDS`.

    Every row carries `name`, `template`, `stub`, `source_skills` and `supersedes`, and may
    carry `validator`. A kind in `KINDS` with no row, a row naming a kind outside `KINDS`, or a
    kind with two rows raises `KindRegistryError` naming each, so the table and the core cannot
    drift apart (ISSUE-REF D-1).
    """
    path = path or Path(os.environ.get(REGISTRY_ENV) or REGISTRY_PATH)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise KindRegistryError(f"{path}: {exc}") from exc
    rows = data.get("kind")
    if not isinstance(rows, list):
        raise KindRegistryError(f"{path}: no [[kind]] rows")
    registry: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(rows):
        row = _registry_row(index, raw)
        if row["name"] in registry:
            raise KindRegistryError(f"{path}: kind {row['name']!r} has two rows")
        registry[row["name"]] = row
    missing = [k for k in KINDS if k not in registry]
    unknown = [k for k in registry if k not in KINDS]
    if missing or unknown:
        parts = []
        if missing:
            parts.append(f"KINDS with no row: {', '.join(missing)}")
        if unknown:
            parts.append(f"rows outside KINDS: {', '.join(unknown)}")
        raise KindRegistryError(
            f"{path} and held_output.KINDS differ ({'; '.join(parts)})"
        )
    return registry


def briefing_kinds(registry: Mapping[str, Mapping[str, Any]]) -> frozenset[str]:
    """The kinds whose newest open item supersedes the older ones (`supersedes = true`)."""
    return frozenset(name for name, row in registry.items() if row["supersedes"])


def kind_validator(kind: str, path: Path | None = None) -> str | None:
    """The validator `add` runs on a body of ``kind`` (its registry row's `validator`), or None."""
    return load_kind_registry(path)[check_kind(kind)].get("validator")


def check_task(task: str) -> str:
    if task != "none" and not TASK_RE.match(task or ""):
        raise HeldError("task", f"{task!r} is not a Beads id or 'none'")
    return task


def _check_line(key: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HeldError(key, "must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise HeldError(key, "must be one line")
    return value


#: Title prefix of the one AI-resolved decision item the maintainer asked to see: an agent's ruling that
#: made an open held decision moot (README § Answering, partial answers and supersession, case 2).
AI_SUPERSEDED_PREFIX = "AI-superseded:"
#: The item's own decision line: a `### Decision: ...` heading or a `> **Decision:** ...` line.
_DECISION_LINE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*Decision\b|>[ \t]*\*\*Decision:\*\*).*$", re.M
)
_RESOLVED_MARK = re.compile(r"✅\s*Resolved by\b")
_RESOLVED_TITLE = re.compile(
    r"✅\s*Resolved by\b|\bresolved\b[^)]*\bfor review\b", re.I
)


def ai_resolved_decision(title: str, body: str) -> bool:
    """True when a decision item records a choice an agent already made.

    Such a decision is never held for the maintainer (README § Decisions are git-tracked before they are
    shown): its record is its governing doc or the `resolve` log. Only the item's FIRST decision
    line is read, so an escalated decision that quotes an earlier resolved one is not caught. An
    `AI-superseded:` review item is the exception the maintainer asked for and is never caught.
    """
    if title.startswith(AI_SUPERSEDED_PREFIX):
        return False
    if _RESOLVED_TITLE.search(title):
        return True
    first = _DECISION_LINE.search(body)
    return bool(first and _RESOLVED_MARK.search(first.group(0)))


def check_decision_escalated(kind: str, title: str, body: str) -> None:
    """Refuse to hold a decision an agent resolved; the error names where it goes instead."""
    if kind == "decision" and ai_resolved_decision(title, body):
        raise HeldError(
            "body",
            "this decision is already resolved by an agent, so it is not held for the maintainer: "
            "record it in its governing doc, or with `resolve` when no doc governs "
            "(docs/held/README.md § Decisions are git-tracked before they are shown)",
        )


def topic_slug(title: str) -> str:
    """A filename topic from a title: lowercase words, hyphen-joined, bounded."""
    words = re.findall(r"[a-z0-9]+", title.lower())[:TOPIC_MAX_WORDS]
    slug = "-".join(words)[:TOPIC_MAX_CHARS].strip("-")
    return slug or "item"


def check_topic(topic: str) -> str:
    if not TOPIC_RE.match(topic or ""):
        raise HeldError("topic", f"{topic!r} is not a lowercase hyphenated slug")
    return topic


# ---------------------------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------------------------


def item_relpath(session: str, date: str, number: int, kind: str, topic: str) -> Path:
    return HELD_DIR / session_dir(session) / f"{date}-{number:02d}-{kind}-{topic}.md"


def next_number(names: list[str], date: str) -> int:
    """One past the highest `NN` any of ``names`` (file names, live or historic) uses on ``date``."""
    used = [
        int(match["nn"])
        for match in (FILENAME_RE.match(Path(name).name) for name in names)
        if match and match["date"] == date
    ]
    return max(used, default=0) + 1


def relpath_in_checkout(path: Path) -> Path:
    """`docs/held/<host>--<slug>/<file>` from any path that names an item, absolute or relative."""
    parts = path.parts
    for index in range(len(parts) - 2):
        if parts[index] == "docs" and parts[index + 1] == "held":
            rel = Path(*parts[index:])
            if len(rel.parts) != _ITEM_DEPTH or rel.suffix != ".md":
                break
            session_from_dir(rel.parts[2])
            return rel
    raise HeldError("path", f"{path} does not name docs/held/<host>--<slug>/<file>.md")


# ---------------------------------------------------------------------------------------------
# Render and parse
# ---------------------------------------------------------------------------------------------


def _render_value(key: str, value: Any) -> str:
    if value is None:
        return "null"
    if key in _BOOL_KEYS:
        return "true" if value else "false"
    if key in _QUOTED_KEYS:
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def render(frontmatter: Mapping[str, Any], rest: str) -> str:
    """The file text: frontmatter in `KEYS` order, then each set `OPTIONAL_KEYS` key, a blank
    line, then ``rest`` verbatim."""
    keys = [*KEYS, *(k for k in OPTIONAL_KEYS if frontmatter.get(k) is not None)]
    lines = [f"{key}: {_render_value(key, frontmatter[key])}" for key in keys]
    return "---\n" + "\n".join(lines) + "\n---\n\n" + rest


def escalated(frontmatter: Mapping[str, Any]) -> bool:
    """True for an item addressed to a decider and not sent on to the maintainer (ISSUE-REF D-3).

    Such an item is the asker's question to its decider, so it is never listed among the maintainer's
    open decisions; it is listed under its own "Escalated to a decider" heading.
    """
    return bool(frontmatter.get("decider")) and not frontmatter.get(
        "escalated_to_owner"
    )


def _escalation_fields(
    decider: str | None, escalation_id: str | None, recommended: str | None
) -> dict[str, Any]:
    if decider is None:
        if escalation_id is not None or recommended is not None:
            raise HeldError(
                "decider", "escalation_id and recommended need a decider to address"
            )
        return {}
    return {
        "decider": check_decider(decider),
        "escalation_id": None
        if escalation_id is None
        else check_token("escalation_id", escalation_id),
        "recommended": None
        if recommended is None
        else _check_line("recommended", recommended),
    }


def check_decider(decider: str) -> str:
    """A `<slug>@<host>` session address or a `<kind>:<id>` provider or agent id, or refuse."""
    if DECIDER_RE.match(decider or ""):
        return decider
    try:
        _session_address(decider)
    except HeldError:
        raise HeldError(
            "decider",
            f"{decider!r} is neither a <slug>@<host> session nor a <kind>:<id> "
            "provider or agent id",
        ) from None
    return decider


def check_token(key: str, value: str) -> str:
    if not TOKEN_RE.match(value or ""):
        raise HeldError(key, f"{value!r} is not a bare token ([A-Za-z0-9._:-])")
    return value


def new_item(  # noqa: PLR0913  # one field per frontmatter key the caller supplies
    *,
    kind: str,
    session: str,
    task: str,
    title: str,
    ask: str,
    source_skill: str,
    body: str,
    now: dt.datetime,
    decider: str | None = None,
    escalation_id: str | None = None,
    recommended: str | None = None,
) -> tuple[dict[str, Any], str]:
    """The frontmatter and full text of a new `open` item; the body is kept byte for byte.

    ``decider`` addresses the item to a decider instead of the maintainer (ISSUE-REF D-3), with the
    optional ``escalation_id`` and the asker's ``recommended`` option.
    """
    machine, _ = check_session(session)
    frontmatter: dict[str, Any] = {
        "held_schema": SCHEMA_VERSION,
        "kind": check_kind(kind),
        "status": STATUS_OPEN,
        "session": session,
        "machine": machine,
        "task": check_task(task),
        "title": _check_line("title", title),
        "ask": _check_line("ask", ask),
        "source_skill": _check_line("source_skill", source_skill),
        "created_at": iso(now),
        "answered_at": None,
        "answer_recorded_in": None,
        **_escalation_fields(decider, escalation_id, recommended),
    }
    if kind != "decision" and decider is not None:
        raise HeldError("decider", "only a decision item is escalated to a decider")
    separator = "" if body.endswith("\n") or not body else "\n"
    text = render(frontmatter, f"{body}{separator}\n{ANSWER_HEADING}\n")
    validate_text(text)
    return frontmatter, text


def _parse_value(key: str, raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            value, end = json.JSONDecoder().raw_decode(raw)
        except json.JSONDecodeError as exc:
            raise HeldError(
                key, f"unterminated or invalid quoted string: {exc}"
            ) from exc
        trailer = raw[end:]
        if trailer.strip() and not trailer.lstrip().startswith("#"):
            raise HeldError(key, f"unexpected text after the quoted value: {trailer!r}")
        return value
    if raw.startswith("'"):
        raise HeldError(
            key, "single-quoted values are not supported; use double quotes"
        )
    raw = _BARE_COMMENT.sub("", raw).strip()
    if raw in {"", "null", "~"}:
        return None
    if key in _BOOL_KEYS:
        if raw not in {"true", "false"}:
            raise HeldError(key, f"{raw!r} is not true or false")
        return raw == "true"
    if _INT.match(raw):
        return int(raw)
    return raw


def split(text: str) -> tuple[dict[str, Any], str]:
    """`(frontmatter, rest)`; raises `HeldError` naming the offending key or `frontmatter`."""
    if not text.startswith("---\n"):
        raise HeldError("frontmatter", "the file does not open with '---'")
    end = text.find("\n---\n", 3)
    if end == -1:
        raise HeldError("frontmatter", "no closing '---' line")
    frontmatter: dict[str, Any] = {}
    for line in text[4:end].split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _FM_LINE.match(line)
        if not match:
            raise HeldError("frontmatter", f"not a 'key: value' line: {line!r}")
        key = match["key"]
        if key in frontmatter:
            raise HeldError(key, "appears twice")
        frontmatter[key] = _parse_value(key, match["value"])
    rest = text[end + len("\n---\n") :]
    return frontmatter, rest.removeprefix("\n")


def _check_frontmatter(fm: Mapping[str, Any]) -> None:  # noqa: C901, PLR0912  # one branch per key, in KEYS order
    for key in KEYS:
        if key not in fm:
            raise HeldError(key, "missing")
    if fm["held_schema"] != SCHEMA_VERSION or isinstance(fm["held_schema"], bool):
        raise HeldError(
            "held_schema", f"must be {SCHEMA_VERSION}, got {fm['held_schema']!r}"
        )
    if fm["kind"] not in KINDS:
        raise HeldError("kind", f"{fm['kind']!r} is not one of {', '.join(KINDS)}")
    if fm["status"] not in STATUSES:
        raise HeldError(
            "status", f"{fm['status']!r} is not one of {', '.join(STATUSES)}"
        )
    machine, _ = check_session(str(fm["session"]))
    if fm["machine"] != machine:
        raise HeldError(
            "machine", f"{fm['machine']!r} is not the session's machine {machine!r}"
        )
    check_task(str(fm["task"]))
    for key in ("title", "ask", "source_skill"):
        _check_line(key, fm[key])
    if not isinstance(fm["created_at"], str) or not TIMESTAMP_RE.match(
        fm["created_at"]
    ):
        raise HeldError(
            "created_at", f"{fm['created_at']!r} is not YYYY-MM-DDTHH:MM:SSZ"
        )
    answered_at = fm["answered_at"]
    if fm["status"] == STATUS_OPEN and answered_at is not None:
        raise HeldError("answered_at", "must be null while status is open")
    if fm["status"] == STATUS_ANSWERED and (
        not isinstance(answered_at, str) or not TIMESTAMP_RE.match(answered_at)
    ):
        raise HeldError("answered_at", f"{answered_at!r} is not YYYY-MM-DDTHH:MM:SSZ")
    if fm["answer_recorded_in"] is not None:
        _check_line("answer_recorded_in", fm["answer_recorded_in"])
    extra = [key for key in fm if key not in KEYS and key not in OPTIONAL_KEYS]
    if extra:
        raise HeldError(extra[0], "is not a held_schema 1 key")
    _check_escalation(fm)


def _check_escalation(fm: Mapping[str, Any]) -> None:
    """The optional escalation keys (ISSUE-REF): each well formed, and none without a decider."""
    present = [key for key in OPTIONAL_KEYS if fm.get(key) is not None]
    if not present:
        return
    if fm.get("decider") is None:
        raise HeldError(present[0], "is set on an item with no decider")
    if fm["kind"] != "decision":
        raise HeldError("decider", "only a decision item is escalated to a decider")
    check_decider(str(fm["decider"]))
    for key in present:
        if key in ("escalation_id", "decider_model"):
            check_token(key, str(fm[key]))
        elif key in _QUOTED_KEYS:
            _check_line(key, fm[key])
        elif key in _BOOL_KEYS and not isinstance(fm[key], bool):
            raise HeldError(key, "must be true or false")
    answer_keys = [key for key in ANSWER_KEYS if key in present]
    if (
        fm["status"] == STATUS_OPEN
        and answer_keys
        and not fm.get("escalated_to_owner")
    ):
        raise HeldError(
            answer_keys[0], "requires an answered item or escalation to the maintainer"
        )


def _check_path(rel: Path, fm: Mapping[str, Any]) -> None:
    match = FILENAME_RE.match(rel.name)
    if not match:
        raise HeldError(
            "path", f"{rel.name!r} is not <YYYY-MM-DD>-<NN>-<kind>-<topic>.md"
        )
    if rel.parent.name != session_dir(fm["session"]):
        raise HeldError(
            "session",
            f"{fm['session']!r} does not match the directory {rel.parent.name!r}",
        )
    if match["kind"] != fm["kind"]:
        raise HeldError(
            "kind", f"{fm['kind']!r} does not match the file name's {match['kind']!r}"
        )


def validate_text(text: str, rel: Path | None = None) -> dict[str, Any]:
    """The frontmatter of a conforming item; raises `HeldError` naming the first violated key."""
    frontmatter, rest = split(text)
    _check_frontmatter(frontmatter)
    if ANSWER_HEADING not in rest.splitlines():
        raise HeldError(ANSWER_HEADING, "the heading is missing")
    if rel is not None:
        _check_path(rel, frontmatter)
    return frontmatter


def answered(  # noqa: PLR0913  # the answer plus one argument per decider field it records
    text: str,
    answer: str,
    now: dt.datetime,
    recorded_in: str | None = None,
    *,
    chosen: str | None = None,
    decider_model: str | None = None,
    escalated_to_owner: bool | None = None,
) -> str:
    """``text`` moved to `answered`, with ``answer`` appended under `## Answer` verbatim.

    ``chosen``, ``decider_model`` and ``escalated_to_owner`` record a decider's ruling on an
    escalated item (ISSUE-REF D-7) and are refused on an item with no decider.
    Passing the question to the maintainer keeps it open with no answered_at until he answers it.
    """
    frontmatter = validate_text(text)
    if frontmatter["status"] != STATUS_OPEN:
        raise HeldError(
            "status", f"is {frontmatter['status']!r}; only an open item is answered", 4
        )
    if not answer.strip():
        raise HeldError("answer", "the answer text is empty")
    # A decider can pass the question to the maintainer without closing his outstanding ask.
    frontmatter = {
        **frontmatter,
        "status": STATUS_OPEN if escalated_to_owner else STATUS_ANSWERED,
        "answered_at": None if escalated_to_owner else iso(now),
    }
    ruling = {
        "chosen": chosen,
        "decider_model": decider_model,
        "escalated_to_owner": escalated_to_owner,
    }
    for key, value in ruling.items():
        if value is not None:
            frontmatter[key] = value
    if recorded_in is not None:
        frontmatter["answer_recorded_in"] = _check_line(
            "answer_recorded_in", recorded_in
        )
    _, rest = split(text)
    rest = rest if rest.endswith("\n") else rest + "\n"
    tail = answer if answer.endswith("\n") else answer + "\n"
    result = render(frontmatter, f"{rest}\n{tail}")
    validate_text(result)
    return result


# ---------------------------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------------------------


def in_held_top(path: Path) -> bool:
    """True when ``path`` sits directly in a `docs/held` directory, where the aggregates live."""
    return path.parent.parts[-2:] == HELD_DIR.parts


def is_reserved(path: Path) -> bool:
    """True for a `docs/held/` file that is neither an item nor malformed.

    Any `README_NAME` is reserved; a `RESERVED_SUFFIXES` name only at the top of `docs/held`, so
    an item in a session directory titled "... open decisions" stays an item.
    """
    if path.name == README_NAME:
        return True
    return in_held_top(path) and path.name.endswith(RESERVED_SUFFIXES)


def list_items(
    root: Path,
    *,
    status: str = "all",
    session: str | None = None,
    task: str | None = None,
) -> dict[str, Any]:
    """Every item under ``root``/docs/held, filtered; unparseable files under `malformed`.

    `README_NAME` and any file ending in one of `RESERVED_SUFFIXES` are neither.

    Raises `HeldRootMissing` when the directory is absent, so a caller can never read a
    missing root as "nothing held".
    """
    if status not in (*STATUSES, "all"):
        raise HeldError("status", f"{status!r} is not open, answered or all")
    wanted = None if session is None else session_dir(session)
    held = (root / HELD_DIR).resolve()
    if not held.is_dir():
        raise HeldRootMissing(held)
    items: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    for path in sorted(held.rglob("*.md")):
        if is_reserved(path):
            continue
        rel = path.relative_to(held.parent.parent)
        if wanted is not None and rel.parts[2:3] != (wanted,):
            continue
        try:
            frontmatter = validate_text(path.read_text(encoding="utf-8"), rel)
        except (HeldError, OSError, UnicodeDecodeError) as exc:
            malformed.append({"path": str(path), "error": str(exc)})
            continue
        if status != "all" and frontmatter["status"] != status:
            continue
        if task is not None and frontmatter["task"] != task:
            continue
        items.append(
            {
                "path": str(path),
                "relpath": rel.as_posix(),
                "status": frontmatter["status"],
                "frontmatter": frontmatter,
            }
        )
    return {"root": str(held), "items": items, "malformed": malformed}


def list_stores(
    roots: Mapping[str, Path],
    *,
    status: str = "all",
    session: str | None = None,
    task: str | None = None,
) -> dict[str, Any]:
    """`list_items` across several stores (plan D-11), every item and malformed file tagged.

    ``roots`` maps a store name (its repo) to the checkout holding its `docs/held/`. Each
    store gets its own ``{status, path, error}``: one missing or unreadable store is reported
    as such and never hides the others' items.
    """
    stores: dict[str, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    for name, root in roots.items():
        try:
            listing = list_items(root, status=status, session=session, task=task)
        except HeldRootMissing as exc:
            stores[name] = {
                "status": "held_root_missing",
                "path": str(exc.path),
                "error": str(exc),
            }
            continue
        except OSError as exc:
            stores[name] = {
                "status": "failed",
                "path": str(root / HELD_DIR),
                "error": str(exc),
            }
            continue
        stores[name] = {"status": "ok", "path": listing["root"], "error": None}
        items += [{**item, "store": name} for item in listing["items"]]
        malformed += [{**bad, "store": name} for bad in listing["malformed"]]
    return {"stores": stores, "items": items, "malformed": malformed}
