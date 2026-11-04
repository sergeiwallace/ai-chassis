#!/usr/bin/env python3
# ruff: noqa: E402
"""What persona and model is that agent actually running? (ISSUE-REF)

`ListAgents` prints the subagent TYPE and nothing else, so the only durable answer to
"what model is that agent?" was the persona file's frontmatter -- which is the DEFAULT,
not the launch. Measured: the coordinator for ISSUE-REF/ISSUE-REF reports as
``architect``, ``architect.md`` says ``model: opus``, and that launch's own runtime
record says ``fable`` across 439 assistant turns. A session asserted ``opus`` twice in
one turn off that frontmatter before measuring, and was wrong by a whole model family.

The fleet already requires every launch to REPORT its resolved parameters in chat. That
report dies at compaction while the question outlives it, and the documented fallback --
parsing the subagent's own ``.jsonl`` -- costs ~20k tokens and is discouraged for
exactly that reason. This module is the cheap durable answer instead.

TWO HALVES, JOINED ON ``tool_use_id``
-------------------------------------
Claude Code already writes half the record itself, and it is the authoritative half:
``<transcript>/subagents/agent-<id>.meta.json``. Measured over all 72 records on this
host, every one carries ``agentType`` and ``model``; 51 carry ``toolUseId``; 32 carry
``parentAgentId``, and **32 of 32 resolve to a sibling record in the same directory**,
which is what makes a child COUNT computable without reading a transcript.

What CC does not record is the launch's ROLE -- whether this agent was given delegation
authority or told it was a leaf. That is only knowable from the brief, which only
``PreToolUse(Agent)`` sees. So ``record_launch`` writes a small sidecar there, keyed by
``tool_use_id`` (a documented ``PreToolUse`` field), and ``launch_records`` joins the
two into records keyed by the AGENT ID.

The EFFECTIVE model is taken from CC's record, never from the profile: that is the whole
point. The profile default is reported ALONGSIDE it, with ``model_overridden``, so a
reader can see that the two differ instead of having to suspect it.

WHAT THIS DELIBERATELY DOES NOT CLAIM
-------------------------------------
* ``effort`` is NOT in the Agent tool payload. Measured across 53 real ``Agent`` tool
  calls in this machine's transcripts: the input keys are ``description``,
  ``subagent_type``, ``model``, ``prompt``, ``isolation`` (14/53) and ``name`` (25/53) --
  no ``effort`` at all. ``meta.json`` is not uniform: measured 2026-10-11 on example-client-host-mac, 26
  of 186 records carry an ``effort`` key (``high`` 14, ``medium`` 10, ``low`` 2) and the
  other 160 carry none. This module still records the persona profile's own ``effort:``
  frontmatter, and ``effort_source`` says so; it does not read ``meta.json``'s field.
  Reporting the frontmatter as the launch's resolved effort would be the exact mistake
  this module exists to stop, one field over.
* A launch with no ``tool_use_id`` in its hook payload, or an in-process teammate (which
  has no tool call and so no ``toolUseId``), joins to no sidecar. Its persona and model
  are still reported from CC's record; its ROLE reads ``unrecorded`` rather than being
  guessed.
"""

from __future__ import annotations

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
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1

LEDGER_DIR_ENV = "CLAUDE_AGENT_LAUNCH_LEDGER_DIR"
LEDGER_DIR_NAME = "agent-launch-ledger"

#: ``architect`` is the only profile carrying this today (measured: 1 of 7), and it is
#: also one of the two whitelisted coordinators -- which is precisely the combination
#: that produces an epic reporting success having changed nothing. Detection is
#: POSITIVE-ONLY: a profile without the phrase is not claimed to be an implementer, it
#: is simply not proven to be barred. Same discipline as ``landing_obligation`` and as
#: ``check-agent-profiles.py``'s own ``FRESHNESS_MARKER``, and for the same reason -- a
#: false positive here would refuse correct work.
SOURCE_WRITE_PROHIBITION = re.compile(r"never source code", re.I)

ROLE_COORDINATOR = "coordinator"
ROLE_LEAF = "leaf"
ROLE_UNRECORDED = "unrecorded"

AGENTS_SUBPATH = Path(".claude") / "agents"
META_PREFIX = "agent-"
META_SUFFIX = ".meta.json"


def _user_config_dir() -> Path:
    """Where Claude Code keeps its config tree, honouring ``CLAUDE_CONFIG_DIR``.

    Same resolution ``agent_isolation_guard`` uses: a hard-coded ``~/.claude`` reads the
    wrong tree on any machine that relocates it.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".claude"


def ledger_dir(directory: Path | None = None) -> Path:
    """Where launch sidecars live."""
    if directory is not None:
        return directory
    override = os.environ.get(LEDGER_DIR_ENV)
    if override:
        return Path(override)
    return _user_config_dir() / LEDGER_DIR_NAME


def _sidecar_name(tool_use_id: str) -> str:
    """A filename from a tool-use id, with no path component it did not earn.

    ``tool_use_id`` arrives from a hook payload, so it is treated as untrusted input:
    anything outside the safe set is replaced rather than sanitised away, which keeps
    two distinct ids from colliding on one filename.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", tool_use_id) + ".json"


@dataclass(frozen=True)
class LaunchFacts:
    """What only ``PreToolUse(Agent)`` knows about a launch.

    A record rather than nine keyword arguments so the schema has one definition and the
    writer's signature does not grow with it.
    """

    tool_use_id: str
    persona: str
    requested_model: str | None
    role: str
    role_signal: str | None
    isolation: str | None
    session_id: str
    prompt_id: str


def record_launch(facts: LaunchFacts, *, directory: Path | None = None) -> Path | None:
    """Write one launch sidecar; return its path, or None if it could not be written.

    BEST EFFORT BY DESIGN, and the asymmetry is deliberate: this is a RECORD, not a
    guard. A guard whose failure is tolerated is not a guard, but a record whose failure
    BLOCKED every delegation on the machine would be a fleet-wide outage in exchange for
    an observability nicety. The caller allows the launch either way.
    """
    if not facts.tool_use_id:
        return None
    payload = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        **asdict(facts),
    }
    target = ledger_dir(directory) / _sidecar_name(facts.tool_use_id)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    except OSError:
        return None
    return target


def read_sidecars(directory: Path | None = None) -> dict[str, dict]:
    """Every readable sidecar, keyed by ``tool_use_id``."""
    found: dict[str, dict] = {}
    base = ledger_dir(directory)
    try:
        paths = sorted(base.glob("*.json"))
    except OSError:
        return found
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and isinstance(record.get("tool_use_id"), str):
            found[record["tool_use_id"]] = record
    return found


def _profile_path(persona: str, agents_dir: Path | None) -> Path | None:
    directories = [agents_dir] if agents_dir else [_user_config_dir() / "agents"]
    for directory in directories:
        if directory is None:
            continue
        candidate = directory / f"{persona}.md"
        if candidate.is_file():
            return candidate
    return None


def profile_facts(persona: str, agents_dir: Path | None = None) -> dict[str, object]:
    """The persona file's own DEFAULT model/effort, and whether it bars source writes.

    Flat top-level scalars only, stdlib-only, no PyYAML -- the same grammar and the same
    reason as ``agent_isolation_guard.parse_frontmatter``, which is deliberately NOT
    imported here: that module loads THIS one, and a module-scope import back would be a
    cycle in a file that runs under a bare hook interpreter.
    """
    path = _profile_path(persona, agents_dir)
    if path is None:
        return {"model": None, "effort": None, "forbids_source_writes": False}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"model": None, "effort": None, "forbids_source_writes": False}
    fields: dict[str, str] = {}
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line[:1].isspace() or ":" not in line:
                continue
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip().strip("\"'")
    return {
        "model": fields.get("model"),
        "effort": fields.get("effort"),
        "forbids_source_writes": bool(SOURCE_WRITE_PROHIBITION.search(text)),
    }


def read_meta_records(subagents_dir: Path) -> dict[str, dict]:
    """Claude Code's own per-agent records, keyed by agent id."""
    records: dict[str, dict] = {}
    try:
        paths = sorted(subagents_dir.glob(f"{META_PREFIX}*{META_SUFFIX}"))
    except OSError:
        return records
    for path in paths:
        agent_id = path.name[len(META_PREFIX) : -len(META_SUFFIX)]
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(meta, dict):
            records[agent_id] = meta
    return records


def launch_records(
    subagents_dir: Path,
    *,
    agents_dir: Path | None = None,
    ledger: Path | None = None,
) -> dict[str, dict]:
    """One record per agent id: the effective persona, model, effort, role and children.

    The join is CC's ``toolUseId`` against the sidecar's ``tool_use_id``. Children are
    counted from ``parentAgentId`` within this same directory, which is where every
    nested launch's record lands (measured: 32/32 resolved, 0 unresolved).
    """
    metas = read_meta_records(subagents_dir)
    sidecars = read_sidecars(ledger)
    children: dict[str, int] = dict.fromkeys(metas, 0)
    for meta in metas.values():
        parent = meta.get("parentAgentId")
        if isinstance(parent, str) and parent in children:
            children[parent] += 1

    records: dict[str, dict] = {}
    for agent_id, meta in metas.items():
        persona = meta.get("agentType")
        persona = persona if isinstance(persona, str) else "general-purpose"
        facts = profile_facts(persona, agents_dir)
        tool_use_id = meta.get("toolUseId")
        sidecar = (
            sidecars.get(tool_use_id) if isinstance(tool_use_id, str) else None
        ) or {}
        effective_model = meta.get("model")
        default_model = facts["model"]
        records[agent_id] = {
            "agent_id": agent_id,
            "persona": persona,
            "model": effective_model,
            "model_source": "subagent meta.json (the runtime's own resolution)",
            "persona_default_model": default_model,
            "model_overridden": bool(
                effective_model and default_model and effective_model != default_model
            ),
            "effort": facts["effort"],
            "effort_source": (
                "persona frontmatter -- the Agent tool payload carries no effort field"
            ),
            "role": sidecar.get("role", ROLE_UNRECORDED),
            "role_signal": sidecar.get("role_signal"),
            "requested_model": sidecar.get("requested_model"),
            "forbids_source_writes": facts["forbids_source_writes"],
            "parent_agent_id": meta.get("parentAgentId"),
            "children": children[agent_id],
            "worktree": meta.get("worktreePath"),
            "tool_use_id": tool_use_id,
            "description": meta.get("description"),
        }
    return records


def child_floor_verdict(record: dict) -> str | None:
    """The contradiction a coordinator run must not finish on, or None.

    A persona that MAY NOT write source code, holding delegation authority, having
    launched ZERO children, cannot have implemented anything and did not ask anyone
    else to. Measured instance: the ISSUE-REF epic orchestrator ran zero children --
    launches were dead machine-wide on an unaccepted Xcode licence -- and finished
    without that being an error. The maintainer asked three times whether his SSO and
    notification work was being handled; this is why the answer kept disappointing.

    All three conjuncts are required, and each one excludes a legitimate run: a leaf
    correctly launches nothing; an implementer persona correctly launches nothing
    because it writes the code itself; and a coordinator with children did delegate.
    """
    if record.get("role") != ROLE_COORDINATOR:
        return None
    if not record.get("forbids_source_writes"):
        return None
    if record.get("children", 0) > 0:
        return None
    return (
        f"child-floor: agent {record.get('agent_id')} ({record.get('persona')}, "
        f"model {record.get('model')}) held COORDINATOR authority and launched ZERO "
        "children, but its persona may not write source code -- so this run cannot "
        "have implemented anything and did not ask anyone else to.\n"
        "  Report it as a FAILED run, not a finished one. If the children were "
        "attempted and died, that is the finding: on 2026-10-05 every Agent launch on "
        "this host failed on an unaccepted Xcode licence and the epic above reported "
        "success anyway (ISSUE-REF).\n"
        "  If zero children was genuinely correct, say so explicitly in the report; a "
        "coordinator run that neither implements nor delegates is otherwise "
        "indistinguishable from one whose launches all failed."
    )


def default_subagents_dirs() -> list[Path]:
    """Every session's subagents directory under the Claude config tree."""
    projects = _user_config_dir() / "projects"
    try:
        return sorted(p for p in projects.glob("*/*/subagents") if p.is_dir())
    except OSError:
        return []


def _records_for(args) -> dict[str, dict]:
    directories = (
        [args.subagents_dir] if args.subagents_dir else default_subagents_dirs()
    )
    merged: dict[str, dict] = {}
    for directory in directories:
        merged.update(
            launch_records(
                directory, agents_dir=args.agents_dir, ledger=args.ledger_dir
            )
        )
    return merged


def _format(record: dict) -> str:
    override = " (OVERRIDDEN from " + str(record["persona_default_model"]) + ")"
    return (
        f"{record['agent_id']}  persona={record['persona']}  "
        f"model={record['model']}{override if record['model_overridden'] else ''}  "
        f"effort={record['effort']}  role={record['role']}  "
        f"children={record['children']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subagents-dir", type=Path, help="one session's subagents dir"
    )
    parser.add_argument("--agents-dir", type=Path, help="persona profile directory")
    parser.add_argument("--ledger-dir", type=Path, help="launch sidecar directory")
    parser.add_argument("--agent", help="report one agent id")
    parser.add_argument(
        "--check-child-floor",
        metavar="AGENT_ID",
        help="exit 1 if this coordinator launched zero children",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    records = _records_for(args)

    target = args.check_child_floor or args.agent
    if target is not None:
        record = records.get(target)
        if record is None:
            print(f"no launch record for agent {target}", file=sys.stderr)
            return 2
        if args.check_child_floor:
            verdict = child_floor_verdict(record)
            if verdict is not None:
                print(verdict, file=sys.stderr)
                return 1
            print(f"child-floor: OK for agent {target}")
            return 0
        print(json.dumps(record, indent=2) if args.json else _format(record))
        return 0

    if args.json:
        print(json.dumps(list(records.values()), indent=2))
    else:
        for record in records.values():
            print(_format(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
