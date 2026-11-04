"""Goal waker: wake an idle Claude Code session whose work-all-in-progress /goal is active.

Design: ``docs/designs/issue-ref-goal-waker.md`` (ISSUE-REF). A per-user timer runs
``python -m ai_chassis.goal_waker tick``; each tick reads every live session's transcript,
recognises a qualifying goal that is still active (T-3.1) and a session idle at its prompt
(T-3.2), applies the per-epoch budget, back-off and context-supervisor coexistence gates
(T-3.3), and publishes one ``goal-nudge-<session_id>-<nonce>.marker`` into the resume wake directory
every session already watches (T-3.4). The bash-only ``FileChanged``/``asyncRewake`` hook
``.claude/hooks/goal-waker-rewake.sh`` claims it for its own session and wakes it. A marker
the hook did not claim is typed through the shared injection library once that library is
importable (T-3.6); until then the gap is logged, never silent. Both legs read one switch, the
live ``~/.claude/state/goal-waker-config.json``: an absent file reads as disabled (D-11), and
``systemd/install-goal-waker-timer.sh`` seeds it from ``defaults/goal-waker-config.json``.

The same tick carries the context supervisor's idle save-state wake (ISSUE-REF;
``docs/designs/context-session-state-lifecycle.md`` Phase 9, D-40): a session idle at or above
``idle_save_state_pct`` (the supervisor's live config, read at call time, compared against
``ctx-pct --json``'s ``advice_pct``) gets a ``kind=save-state`` marker telling it to hold its
pending outputs and run ``/save-state``, whether or not a /goal is active. It travels the same
marker, hook and typed fallback as the goal nudge.

A native goal whose declared terminal the evaluator keeps rejecting is an ``EVALUATOR_LOOP``
(ISSUE-REF), whatever its template: it is never nudged, since a wake only restarts the loop
(ISSUE-REF). It gets ``/goal clear`` through the library's ``goal-clear`` use case, idle or
mid-burst inside one long turn unless that turn is working since the newest verdict (ISSUE-REF), then,
only once that landed, one ``goal-loop-nudge`` line telling the session to run
``/delegate <billing-default> /bug-fix`` on the goal text. A follow-up the library queued behind a
busy turn (state ``QUEUED``) is accepted and logged ``GW-CLEAR`` with ``state=QUEUED``: it is
delivered at the session's next input, so it is neither a failure nor noticed a second time
(ISSUE-REF). A clear that did not land is logged
``GW-CLEAR-FAIL``, left as a notice for the session, and tried again after
``max_backoff_seconds``, or after ``loop_clear_retry_seconds`` (growing by
``backoff_factor``) when the library refused on the pane before typing anything.
A typed clear that ended ``landing-none`` and that a later tick reads as cleared, on the
same goal, landed after its check: it is logged ``GW-CLEAR state=LANDED-LATE`` and its
follow-up is sent then (ISSUE-REF).
A threshold-qualified ``no-pane`` resolution failure instead records a session-local
HALTED marker: Stop suppresses continuation until a human prompt, leaving the native goal armed.

A goal whose turn Claude Code's Stop-hook block cap ended after a rejected verdict is an
``EVALUATOR_LOOP`` too (ISSUE-REF). The clear, its follow-up and its pointer obey
``loop_clear_enabled`` alone, never the idle-wake ``automatic_wakes`` rollout switch: they end
a loop rather than wake a session.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import time
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_chassis import context_supervisor as supervisor
from ai_chassis import goal_loop_halt, idle_wake, quota_optional, to_review_reminder
from ai_chassis.delegation import default_delegation_target

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"
DEFAULT_CONFIG_PATH = DEFAULTS_DIR / "goal-waker-config.json"
CONFIG_PATH_ENV = "AIH_GOAL_WAKER_CONFIG_PATH"
# Legacy scheduler identity, retained only for explicit host cutover/handback.
SYSTEMD_SCHEDULER_UNIT = "ai-harness-goal-waker.timer"

NUDGE_VARIANTS = ("generic", "example-project")
_NUDGE_FILES = {
    "generic": "goal-waker-nudge.md",
    "example-project": "goal-waker-nudge.example-project.md",
}
# One line each, for the library's typed mode (D-13): it takes one line of max_typed_chars.
_COMPACT_NUDGE_FILES = {
    "generic": "goal-waker-nudge.compact.md",
    "example-project": "goal-waker-nudge.compact.example-project.md",
}
_QUIET_RANGE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


class NudgeTooLong(ValueError):
    """A rendered nudge exceeds ``max_nudge_chars``."""


class ConfigInvalid(ValueError):
    """The live config file is present but cannot be trusted."""


def live_config_path() -> Path:
    override = os.environ.get(CONFIG_PATH_ENV)
    if override:
        return Path(override)
    return Path.home() / ".claude" / "state" / "goal-waker-config.json"


def _defaults() -> dict[str, Any]:
    return json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


def _same_shape(default: Any, value: Any) -> bool:
    """Whether a live value has the type its default has (bool is never a number here)."""
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and value >= 0
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    if isinstance(default, dict):
        return isinstance(value, dict) and all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in value.items()
        )
    return isinstance(value, type(default))


def _validate(config: dict[str, Any]) -> None:
    for pattern in config["condition_patterns"]:
        try:
            re.compile(pattern)
        except re.error as error:
            raise ConfigInvalid(f"condition_patterns: {pattern!r}: {error}") from error
    for window in config["quiet_hours"]:
        if not _QUIET_RANGE.match(window):
            raise ConfigInvalid(f"quiet_hours: {window!r} is not HH:MM-HH:MM")
    for key in (
        "loop_clear_after_rejections",
        "loop_clear_after_working_rejections",
        "loop_clear_max_stretches",
        "transcript_scan_bytes",
    ):
        value = config[key]
        if not isinstance(value, int) or value < 1:
            raise ConfigInvalid(f"{key}: {value!r} is not a whole number >= 1")
    for host, variant in config["profile_variants"].items():
        if variant not in NUDGE_VARIANTS:
            raise ConfigInvalid(
                f"profile_variants: {host} -> unknown variant {variant!r}"
            )


def _merge(defaults: dict[str, Any], live: Any) -> dict[str, Any]:
    if not isinstance(live, dict):
        raise ConfigInvalid("the live config is not a JSON object")
    merged = dict(defaults)
    for key, value in live.items():
        if key not in defaults:
            continue
        if not _same_shape(defaults[key], value):
            raise ConfigInvalid(f"{key}: {value!r} does not match the default's type")
        merged[key] = value
    _validate(merged)
    return merged


def load_config(path: Path | None) -> dict[str, Any]:
    """The live settings over the shipped defaults, read at call time.

    ``path=None`` means the shipped defaults alone. A missing live file reads as DISABLED with
    ``_error`` ``no-live-config`` (D-11): the hook can read only the live file, so a tick that fell
    open to the defaults would publish markers nothing claims; the installer seeds the file. A
    live file that is present but unreadable, unparseable or wrongly typed fails CLOSED the same
    way, with the reason in ``_error``.
    """
    defaults = _defaults()
    config = dict(defaults, _error=None, _source="defaults")
    if path is None:
        return config
    if not path.exists():
        return dict(config, enabled=False, _error="no-live-config")
    try:
        live = json.loads(path.read_text(encoding="utf-8"))
        merged = _merge(defaults, live)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ConfigInvalid) as error:
        return dict(config, enabled=False, _error=f"{path}: {error}")
    return dict(merged, _error=None, _source=str(path))


def nudge_variant_for_host(host: str | None, profile_variants: dict[str, str]) -> str:
    """The nudge variant for a machine identity; anything unmapped gets the generic text."""
    variant = profile_variants.get(host or "", "generic")
    return variant if variant in NUDGE_VARIANTS else "generic"


_RIDER_LINE_FILE = "goal-waker-rider-line.md"


def render_nudge(
    variant: str,
    *,
    idle_minutes: int,
    max_chars: int,
    compact: bool = False,
    rider: str = "",
) -> str:
    """The nudge text for ``variant``, refusing a render over the size budget.

    ``rider`` is the projects-relative path of the goal's rider (ISSUE-REF): the nudge then
    names it, so a woken session re-reads it. The budget bounds the variant's own text; the
    rider line, whose path is capped at 80 characters, comes after it.
    """
    files = _COMPACT_NUDGE_FILES if compact else _NUDGE_FILES
    template = (DEFAULTS_DIR / files[variant]).read_text(encoding="utf-8")
    text = template.rstrip("\n").replace("{idle_minutes}", str(idle_minutes))
    if len(text) > max_chars:
        raise NudgeTooLong(
            f"nudge variant {variant!r} renders {len(text)} characters, "
            f"over max_nudge_chars={max_chars}"
        )
    if rider:
        line = (DEFAULTS_DIR / _RIDER_LINE_FILE).read_text(encoding="utf-8").strip()
        text += (" " if compact else "\n\n") + line.replace("{rider_path}", rider)
    return text


SAVE_STATE_KIND = "save-state"
_SAVE_STATE_NUDGE_FILE = "context-save-state-nudge.md"
_SAVE_STATE_COMPACT_FILE = "context-save-state-nudge.compact.md"


def render_save_state_nudge(  # noqa: PLR0913 - readings, render limits and pause preservation
    *,
    context_pct: int,
    threshold_pct: int,
    idle_minutes: int,
    max_chars: int,
    compact: bool = False,
    paused_goal: bool = False,
) -> str:
    """The idle save-state directive (D-40), refusing a render over the size budget."""
    name = _SAVE_STATE_COMPACT_FILE if compact else _SAVE_STATE_NUDGE_FILE
    text = (
        (DEFAULTS_DIR / name)
        .read_text(encoding="utf-8")
        .rstrip("\n")
        .replace("{idle_minutes}", str(idle_minutes))
        .replace("{context_pct}", str(context_pct))
        .replace("{threshold_pct}", str(threshold_pct))
    )
    if paused_goal:
        text += (
            " Record the goal as paused in the resume message."
            " Do not re-arm, clear or advance the goal. On resume, keep the goal paused."
        )
    if len(text) > max_chars:
        raise NudgeTooLong(
            f"save-state nudge renders {len(text)} characters, "
            f"over max_nudge_chars={max_chars}"
        )
    return text


_LOOP_CLEARED_NUDGE_FILE = "goal-loop-cleared-nudge.md"
_LOOP_CLEARED_OPENER = "Goal cleared by the goal waker: "
# A notice is relayed as hook context, never typed, so only a sanity bound applies.
_NOTICE_MAX_CHARS = 4000
GOAL_EXIT_PROCEDURE = "docs/procedures/goal-exit-robustness.md"
_ELLIPSIS = "..."


def _loop_delegation_target() -> str:
    """Share the billing resolver with the self-heal prompts."""
    return default_delegation_target(_machine_identity())


def render_loop_cleared_nudge(
    *,
    goal_label: str,
    rejections: int,
    last_reason: str,
    max_chars: int,
    cleared: bool = True,
) -> str:
    """The one-line follow-up typed after an evaluator-loop ``/goal clear`` (ISSUE-REF).

    ``last_reason`` is clipped so the line fits ``max_chars`` (the library's typed limit);
    the rest of the text is never cut, so a budget too small for it refuses. ``cleared``
    False drops the opening claim that the goal was cleared, for a clear that failed.
    """
    template = (DEFAULTS_DIR / _LOOP_CLEARED_NUDGE_FILE).read_text(encoding="utf-8")
    if not cleared:
        template = template.removeprefix(_LOOP_CLEARED_OPENER)
    text = (
        template.rstrip("\n")
        .replace("{template_or_condition_head}", goal_label)
        .replace("{rejections}", str(rejections))
        .replace("{procedure}", GOAL_EXIT_PROCEDURE)
        .replace("{delegation_target}", _loop_delegation_target())
    )
    room = max_chars - (len(text) - len("{last_reason}"))
    reason = " ".join(last_reason.split())
    if len(reason) > room:
        # ASCII only: this line is typed through tmux.
        cut = room - len(_ELLIPSIS)
        reason = reason[:cut].rstrip() + _ELLIPSIS if cut > 0 else ""
    text = text.replace("{last_reason}", reason)
    if len(text) > max_chars:
        raise NudgeTooLong(
            f"loop-cleared nudge renders {len(text)} characters, over {max_chars}"
        )
    return text


_LOOP_CLEARED_POINTER_FILE = "goal-loop-cleared-pointer.md"
# Refusals of the follow-up, before any keystroke, because the composer cannot show it
# whole: the full text is then the session's notice, which a shorter line delivers.
_FOLLOW_UP_TOO_TALL = frozenset(
    {"payload-exceeds-visible-rows", "walk-unverified-version"}
)


def render_loop_cleared_pointer(*, rejections: int) -> str:
    """The short line typed when the follow-up is too tall for the session's composer: the
    ``UserPromptSubmit`` hook relays the notice holding the full follow-up with it."""
    template = (DEFAULTS_DIR / _LOOP_CLEARED_POINTER_FILE).read_text(encoding="utf-8")
    return template.rstrip("\n").replace("{rejections}", str(rejections))


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- repo scripts


def _script(name: str) -> types.ModuleType:
    """Load one repo script by path, the same way ``agent_mail_notifier`` does.

    Scripts are not package modules; a ``sys.path`` edit would change what every other
    import in the process resolves to.
    """
    module_name = f"aih_goal_waker_{name}"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


START_GOAL_SCRIPTS = REPO_ROOT / "skills" / "start-goal" / "scripts"


def _start_goal_ledger() -> types.ModuleType:
    """Load /start-goal's ``ledger`` module by path (D-3 source (i)).

    It imports its sibling ``start_goal_settings`` by bare name, so that sibling is loaded by
    path first and registered under that name -- the one name the ledger's own import asks
    for -- rather than putting the skill directory on ``sys.path`` for the whole process.
    """
    module_name = "aih_goal_waker_start_goal_ledger"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    for name, path in (
        ("start_goal_settings", START_GOAL_SCRIPTS / "start_goal_settings.py"),
        (module_name, START_GOAL_SCRIPTS / "ledger.py"),
    ):
        if name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"could not load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[name]
            raise
    return sys.modules[module_name]


def _machine_identity() -> str | None:
    return _script("worktree_policy").machine_identity()


# ------------------------------------------------------------ T-3.1 goal state

ACTIVE_QUALIFYING = "ACTIVE_QUALIFYING"
ACTIVE_OTHER = "ACTIVE_OTHER"
# A declared terminal the native evaluator keeps rejecting (ISSUE-REF): cleared, never nudged.
EVALUATOR_LOOP = "EVALUATOR_LOOP"
ENDED = "ENDED"
NO_GOAL = "NONE"
UNDETERMINED = "UNDETERMINED"

_GOAL_MARKER = b'"goal_status"'
_COMMAND_NAME = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
# What may precede a terminal phrase on its own line: indentation, quoting and emphasis.
_LINE_DECORATION = " \t>*#_`-"


@dataclass(frozen=True)
class GoalState:
    kind: str
    epoch: int | None = None
    reason: str = ""
    # Which D-3 source qualified the goal: `ledger:<template_name>`, or `condition-pattern`
    # with the reason the ledger could not decide, recorded per session and never an error.
    source: str = ""
    # EVALUATOR_LOOP only: the rejected verdicts since the declaration, the newest one's
    # reason (first LOOP_REASON_CHARS), and the goal named by its template or condition.
    rejections: int = 0
    last_reason: str = ""
    goal_label: str = ""
    # A tool call newer than the newest verdict: the turn may be answering it with work.
    working_since_verdict: bool = False
    # A declaration still stands even below the loop-clear threshold.
    rejected_terminal: bool = False
    # ISSUE-REF: the projects-relative path of the rider the goal's condition arms, if any.
    rider: str = ""
    # The call-time rejection threshold whose counting rule classified this loop.
    loop_threshold_key: str = ""


def _goal_attachment(raw: bytes) -> dict[str, Any] | None:
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict) or record.get("type") != "attachment":
        return None
    attachment = record.get("attachment")
    if isinstance(attachment, dict) and attachment.get("type") == "goal_status":
        return attachment
    return None


def _last_sentinel(
    transcript: Path, floor: int = 0
) -> tuple[int, dict[str, Any]] | None:
    """The newest ``goal_status`` sentinel record and its byte offset.

    ``wq_client.find_last_goal_status`` answers for the newest goal record of any kind; an
    evaluated verdict carries no ``condition``, so search backwards in bounded chunks,
    skipping verdicts without making the whole transcript resident. Above a nonzero
    ``floor``, finding none is not an answer: it raises ``ValueError``.
    """
    lines = _script("wq_client")._transcript_lines_backwards(transcript, floor)
    for start, raw in lines:
        if _GOAL_MARKER in raw:
            attachment = _goal_attachment(raw)
            if attachment is not None and attachment.get("sentinel"):
                return start, attachment
    if floor:
        raise ValueError(f"no goal sentinel above byte {floor} (transcript_scan_bytes)")
    return None


def _message_texts(record: dict[str, Any]) -> list[str]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]


def _user_command(text: str) -> str:
    """A user record as the command it ran: ``/goal clear`` from its tags, else the text."""
    name = _COMMAND_NAME.search(text)
    if name is None:
        return text.strip()
    args = _COMMAND_ARGS.search(text)
    tail = args[1].strip() if args else ""
    return f"/{name[1]} {tail}".strip()


def _declaration(record: dict[str, Any], config: dict[str, Any]) -> str | None:
    """The terminal phrase or exit command one record declares, if any (D-4).

    A phrase counts only at the start of a line of assistant text, so a phrase quoted in
    prose ("the template says to say GOAL COMPLETE") is not read as a declaration.
    """
    kind = record.get("type")
    if kind == "assistant":
        for text in _message_texts(record):
            for line in text.splitlines():
                stripped = line.lstrip(_LINE_DECORATION)
                for phrase in config["terminal_phrases"]:
                    if stripped.startswith(phrase):
                        return phrase
    if kind == "user":
        for text in _message_texts(record):
            command = _user_command(text)
            if command in config["terminal_user_commands"]:
                return command
    return None


def _phrase_bytes(config: dict[str, Any]) -> list[bytes]:
    """Each terminal phrase as it can appear in a JSON line: raw UTF-8 or escaped."""
    forms = set()
    for phrase in config["terminal_phrases"]:
        forms.add(phrase.encode("utf-8"))
        forms.add(json.dumps(phrase)[1:-1].encode("ascii"))
        forms.add(json.dumps(phrase, ensure_ascii=False)[1:-1].encode("utf-8"))
    return sorted(forms)


def _declared_end(transcript: Path, offset: int, config: dict[str, Any]) -> str | None:
    """A terminal declaration since the sentinel, released by a new human prompt.

    Rejected verdicts cannot make the stay-busy path wake a declared terminal again.
    The evaluator-loop classification still takes precedence at its configured threshold.
    Only a user record, or an assistant record carrying a terminal phrase, can decide, so
    every other line is skipped unparsed (ISSUE-REF).
    """
    phrases = _phrase_bytes(config)
    for _, raw in _script("wq_client")._transcript_lines_backwards(transcript, offset):
        if b'"user"' not in raw and not (
            b'"assistant"' in raw and any(p in raw for p in phrases)
        ):
            continue
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        declared = _declaration(record, config)
        if declared is not None:
            return declared
        if _human_prompt(record):
            return None
    return None


LOOP_REASON_CHARS = 200
# How far back from the end the loop walk reads. A loop's run of verdicts is short; a
# walk that reaches this far without a boundary decides on what it has read.
_LOOP_SCAN_BYTES = 32 * 1024 * 1024
_GOAL_HEAD_CHARS = 60


def _rejected(status: dict[str, Any]) -> bool:
    """An evaluated verdict that kept the goal open: not a sentinel, unmet, with a reason."""
    return (
        not status.get("sentinel")
        and status.get("met") is False
        and not status.get("failed")
        and isinstance(status.get("reason"), str)
    )


def _human_prompt(record: dict[str, Any]) -> bool:
    """A user turn a person typed, as opposed to a hook, tool result or injection.

    Stop-hook feedback arrives as an ``isMeta`` user record and the waker's own nudge as a
    ``<task-notification>``; neither is a human, so neither resets an evaluator loop.
    """
    if record.get("type") != "user":
        return False
    if any(
        record.get(flag)
        for flag in ("isMeta", "isCompactSummary", "isVisibleInTranscriptOnly")
    ):
        return False
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return False
    text = "\n".join(_message_texts(record))
    if not text.strip() or text.lstrip().startswith("<local-command"):
        return False
    return supervisor._prompt_origin(text) == "human"


def _typed_compact(record: dict[str, Any]) -> bool:
    """A typed ``/compact`` command record: it manages context, it does not address the goal."""
    if record.get("type") != "user":
        return False
    text = "\n".join(_message_texts(record)).strip()
    command = _COMMAND_NAME.match(text)
    return (command is not None and command[1] == "compact") or bool(
        re.fullmatch(r"/compact(?:[ \t]+[^\r\n]*)?", text)
    )


def _compaction_marker(record: dict[str, Any]) -> bool:
    """Context management records that can identify a restored goal sentinel."""
    return (
        record.get("type") == "system" and record.get("subtype") == "compact_boundary"
    ) or (
        record.get("type") == "user"
        and (bool(record.get("isCompactSummary")) or _typed_compact(record))
    )


# Claude Code's Stop-hook block cap, whatever CLAUDE_CODE_STOP_HOOK_BLOCK_CAP sets it to.
# A loop it qualifies is recorded under this key, at its own rejection count.
BLOCK_CAP_THRESHOLD_KEY = "stop_hook_block_cap"
_BLOCK_CAP = re.compile(r"^A hook blocked the turn from ending \d+ consecutive times")


def _block_cap(record: dict[str, Any]) -> bool:
    """Claude Code overrode a Stop hook that kept blocking the turn from ending."""
    content = record.get("content")
    return (
        record.get("type") == "system"
        and record.get("subtype") == "informational"
        and isinstance(content, str)
        and _BLOCK_CAP.match(content) is not None
    )


def _loop_record(raw: bytes, phrases: list[bytes]) -> dict[str, Any] | None:
    """The records the loop walk reads, parsed; everything else is skipped unparsed."""
    if not any(
        marker in raw
        for marker in (
            b'"goal_status"',
            b'"user"',
            b'"compact_boundary"',
            b'"informational"',
        )
    ):
        if b'"assistant"' not in raw or not (
            b'"tool_use"' in raw or any(p in raw for p in phrases)
        ):
            return None
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _used_a_tool(record: dict[str, Any]) -> bool:
    """An assistant record that called a tool: the session did work, not only re-declare."""
    if record.get("type") != "assistant":
        return False
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_use" for block in content
    )


def _loop_records_backwards(
    transcript: Path, phrases: list[bytes]
) -> Iterator[dict[str, Any]]:
    """The loop walk's records, newest first, within ``_LOOP_SCAN_BYTES`` of the end."""
    floor = max(0, transcript.stat().st_size - _LOOP_SCAN_BYTES)
    for _, raw in _script("wq_client")._transcript_lines_backwards(transcript, floor):
        record = _loop_record(raw, phrases)
        if record is not None:
            yield record


def _loop_step(
    record: dict[str, Any], config: dict[str, Any]
) -> tuple[str | None, str]:
    """What one record is to the loop walk, with a rejected verdict's reason.

    ``rejected``, ``declared``, ``tool``, ``capped`` (Claude Code's Stop-hook block cap),
    ``sentinel`` (needs restore lookahead), ``boundary`` (a human prompt or another goal
    status) or ``None`` (neutral).
    A typed ``/compact`` is not a boundary: like a compaction summary it leaves the
    loop window and any declaration inside it standing.
    """
    attachment = record.get("attachment")
    if (
        record.get("type") == "attachment"
        and isinstance(attachment, dict)
        and attachment.get("type") == "goal_status"
    ):
        if _rejected(attachment):
            return "rejected", attachment["reason"]
        return ("sentinel" if attachment.get("sentinel") else "boundary"), ""
    if _compaction_marker(record) or _block_cap(record):
        return ("capped" if _block_cap(record) else None), ""
    if _declaration(record, config) is not None:
        return "declared", ""
    if _human_prompt(record):
        return "boundary", ""
    return ("tool" if _used_a_tool(record) else None), ""


def _loop_steps(
    transcript: Path, config: dict[str, Any]
) -> Iterator[tuple[str | None, str]]:
    """Share one bounded walk, resolving sentinels before changing any counters.

    A sentinel restores context only if an older compaction marker comes before any
    human prompt or other goal status. Buffer intervening actions until that is known;
    a real goal start or an exhausted scan still ends the walk at the sentinel.
    """
    phrases = [phrase.encode("utf-8") for phrase in config["terminal_phrases"]]
    records = _loop_records_backwards(transcript, phrases)
    for record in records:
        step = _loop_step(record, config)
        if step[0] != "sentinel":
            yield step
            continue
        pending = []
        for older in records:
            if _compaction_marker(older):
                break
            older_step = _loop_step(older, config)
            if _human_prompt(older) or older_step[0] in (
                "boundary",
                "sentinel",
                "rejected",
            ):
                yield "boundary", ""
                return
            if older_step[0] is not None:
                pending.append(older_step)
        else:
            yield "boundary", ""
            return
        yield from pending


@dataclass
class _LoopWalk:
    """The loop walk's counters, fed one record at a time, newest first."""

    threshold: int
    max_stretches: int
    # The stretch rule (ISSUE-REF, ISSUE-REF): counted while ``counting``.
    rejections: int = 0
    declared_seen: bool = False
    stretches: int = 0
    seen_since_stretch: bool = False
    counting: bool = True
    # Every verdict walked, and the run latch (ISSUE-REF): the current run's rejected
    # declarations and the verdicts since the newest declaration in it.
    seen: int = 0
    pairs: int = 0
    pending: int = 0
    latched: bool = False
    newest_reason: str | None = None
    working: bool = False

    def tool(self) -> None:
        self.pairs = self.pending = 0
        self.working = self.working or not self.seen
        if self.counting and self.seen_since_stretch and self.rejections:
            self.stretches += 1
            self.seen_since_stretch = False
            self.counting = self.stretches <= self.max_stretches

    def rejected(self, reason: str) -> None:
        self.seen += 1
        self.pending += 1
        self.newest_reason = self.newest_reason or reason
        if self.counting:
            self.seen_since_stretch = True
            if self.stretches < self.max_stretches:
                self.rejections += 1

    def declared(self) -> None:
        self.pairs += 1 if self.pending else 0
        self.pending = 0
        self.latched = self.latched or self.pairs >= self.threshold
        if self.counting:
            self.seen_since_stretch = True
            self.declared_seen = True

    def result(self) -> int | None:
        if self.latched:
            return self.seen
        if not self.declared_seen or self.rejections < self.threshold:
            return None
        return self.rejections


def _tight_evaluator_loop(
    transcript: Path, config: dict[str, Any]
) -> tuple[int, str, bool] | None:
    """``(rejections, newest reason, working)`` when a declared terminal was rejected in a
    loop; ``working`` is a tool call newer than the newest verdict.

    Walks back from the end counting rejected verdicts while the session did nothing
    between them but re-declare (plan D-2). The count ends at a human prompt, a tool call
    older than the newest verdict, the goal's sentinel, any other goal status, or the
    transcript start; a sentinel restored by compaction is neutral. A tool call newer
    than the newest verdict is reported as ``working`` and left to ``decide_loop``.
    It is a loop once the count reaches
    ``loop_clear_after_rejections`` with a declaration that still stands.

    A declaration stands inside the counted window, or behind up to
    ``loop_clear_max_stretches`` (default 1) stretches of work that followed its own
    rejected verdicts (ISSUE-REF, ISSUE-REF): a session that obeys "stop re-declaring"
    answers a rejection with work, then text-only turns with no terminal phrase. A tool
    call ends the window, not the search. A new stretch starts at a tool call older than
    a verdict or declaration seen since the last stretch; rejections count in the windows
    newer than the last allowed stretch, and the search ends at one stretch more (the
    session kept answering the evaluator), a human prompt or a goal boundary.

    A run that already re-declared and was rejected ``loop_clear_after_rejections``
    times with no tool call inside it stays a loop for the goal (ISSUE-REF): a later
    tool call does not un-count it, so the verdict no longer depends on where the
    2-minute tick lands. Only a human prompt, a goal boundary or ``_LOOP_SCAN_BYTES``
    ends that search; runs are never summed across tool calls by this tight rule.
    ``rejections`` is then every verdict from that run to the end. If it does not
    qualify, the separate working-run rule counts across tool calls.
    """
    walk = _LoopWalk(
        threshold=int(config["loop_clear_after_rejections"]),
        max_stretches=int(config["loop_clear_max_stretches"]),
    )
    for step, reason in _loop_steps(transcript, config):
        if step == "boundary" or (step == "tool" and walk.latched):
            break
        if step == "tool":
            walk.tool()
        elif step == "rejected":
            walk.rejected(reason)
        elif step == "declared":
            walk.declared()
    rejections = walk.result()
    if rejections is None:
        return None
    reason = " ".join((walk.newest_reason or "").split())[:LOOP_REASON_CHARS]
    return rejections, reason, walk.working


def _evaluator_loop(
    transcript: Path, config: dict[str, Any]
) -> tuple[int, str, bool] | None:
    return (
        _tight_evaluator_loop(transcript, config)
        or _working_evaluator_loop(transcript, config)
        or _capped_evaluator_loop(transcript, config)
    )


def _working_evaluator_loop(
    transcript: Path, config: dict[str, Any]
) -> tuple[int, str, bool] | None:
    """Count rejected verdicts across work since the newest human or goal boundary.

    A compaction summary is not a human prompt. The caller requires the newest
    goal status to be rejected, and a declaration must still stand in this window.
    The tight rule and its stretch bound take precedence, unchanged.
    """
    rejections = 0
    declared = False
    newest_reason = ""
    working = False
    for step, reason in _loop_steps(transcript, config):
        if step == "boundary":
            break
        if step == "rejected":
            if not rejections:
                newest_reason = " ".join(reason.split())[:LOOP_REASON_CHARS]
            rejections += 1
        elif step == "declared":
            declared = True
        elif step == "tool" and not rejections:
            working = True
    if declared and rejections >= config["loop_clear_after_working_rejections"]:
        return rejections, newest_reason, working
    return None


def _capped_evaluator_loop(
    transcript: Path, config: dict[str, Any]
) -> tuple[int, str, bool] | None:
    """Claude Code's Stop-hook block cap after a rejected verdict, since the newest human
    or goal boundary: a loop at any count, with or without a declaration (ISSUE-REF).

    The hook blocked the turn from ending until Claude Code overrode it, so the turn has
    ended and nothing is working. It is checked after the tight and working rules.
    """
    rejections = 0
    capped = False
    newest_reason = ""
    for step, reason in _loop_steps(transcript, config):
        if step == "boundary":
            break
        if step == "rejected":
            if not rejections:
                newest_reason = " ".join(reason.split())[:LOOP_REASON_CHARS]
            rejections += 1
        elif step == "capped":
            capped = True
    if capped and rejections:
        return rejections, newest_reason, False
    return None


def goal_state(
    transcript: Path, config: dict[str, Any], session_name: str | None = None
) -> GoalState:
    """Classify a transcript's native goal for the waker (design § Goal-state detection).

    Every backward walk stops ``transcript_scan_bytes`` from the end (ISSUE-REF); a goal
    record or sentinel it cannot reach is UNDETERMINED, never "no goal".
    """
    wq = _script("wq_client")
    try:
        size = transcript.stat().st_size
        floor = max(0, size - int(config["transcript_scan_bytes"]))
        found = wq.find_last_goal_status(transcript, floor)
        if found is None:
            return GoalState(NO_GOAL)
        offset, status = found
        if wq.goal_cleared(status):
            return GoalState(ENDED, offset, "goal-status-cleared")
        # Before the declared-end shortcut: a rejected declaration is exactly the case
        # where more verdicts follow it, and the newest one may follow a re-declaration.
        loop = _tight_evaluator_loop(transcript, config) if _rejected(status) else None
        threshold_key = "loop_clear_after_rejections"
        if loop is None and _rejected(status):
            loop = _working_evaluator_loop(transcript, config)
            threshold_key = "loop_clear_after_working_rejections"
        if loop is None and _rejected(status):
            loop = _capped_evaluator_loop(transcript, config)
            threshold_key = BLOCK_CAP_THRESHOLD_KEY
        sentinel = (
            (offset, status)
            if status.get("sentinel")
            else _last_sentinel(transcript, floor)
        )
        declared = _declared_end(
            transcript, sentinel[0] if sentinel else offset, config
        )
    except (wq.GoalProbeFailed, OSError, ValueError) as error:
        return GoalState(UNDETERMINED, reason=str(error))
    if loop is not None:
        rejections, last_reason, working = loop
        label = _goal_label(session_name, transcript, sentinel, config)
        return GoalState(
            EVALUATOR_LOOP,
            sentinel[0] if sentinel else offset,
            f"declared terminal rejected {rejections} times",
            source=label,
            rejections=rejections,
            last_reason=last_reason,
            goal_label=label,
            working_since_verdict=working,
            loop_threshold_key=threshold_key,
        )
    if declared is not None:
        why = f"declared {declared}"
        if _rejected(status):
            why += "; newest verdict rejected, below the loop threshold"
        return GoalState(ENDED, offset, why, rejected_terminal=_rejected(status))
    return _qualify(offset, sentinel, config, transcript, session_name)


def _goal_label(
    session_name: str | None,
    transcript: Path,
    sentinel: tuple[int, dict[str, Any]] | None,
    config: dict[str, Any],
) -> str:
    """The goal as its /start-goal template when the ledger names one, else its head."""
    row, _ = _ledger_row(session_name, transcript, sentinel, config)
    if row is not None and row.template_name:
        return f"template {row.template_name}"
    condition = sentinel[1].get("condition") if sentinel else None
    if not isinstance(condition, str) or not condition.strip():
        return "goal condition unreadable"
    head = " ".join(condition.split())
    if len(head) > _GOAL_HEAD_CHARS:
        head = head[: _GOAL_HEAD_CHARS - len(_ELLIPSIS)].rstrip() + _ELLIPSIS
    return f"goal: {head}"


def _record_epoch(transcript: Path, offset: int) -> float | None:
    """The ``timestamp`` of the transcript record starting at ``offset``, as epoch seconds."""
    with transcript.open("rb") as handle:
        handle.seek(offset)
        line = handle.readline()
    try:
        stamp = json.loads(line).get("timestamp")
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def _older_than(row: Any, sentinel_at: float, config: dict[str, Any]) -> bool:
    reference = row.verified_at or row.started_at
    return sentinel_at > reference + float(config.get("clock_skew_seconds", 0))


def _ledger_row_superseded(
    rows: list[Any],
    transcript: Path,
    sentinel: tuple[int, dict[str, Any]] | None,
    config: dict[str, Any],
) -> str | None:
    """Why none of ``rows`` describes the transcript's newest goal, or None when one may.

    Asked of every candidate before any is classified, it skips the classification (a read
    of the transcript per row) when whichever row would be chosen is rejected anyway
    (ISSUE-REF); asked again of the chosen row, it is the original check.
    """
    if sentinel is None:
        return None
    sentinel_at = _record_epoch(transcript, sentinel[0])
    if sentinel_at is None:
        return "sentinel time unreadable"
    if all(_older_than(row, sentinel_at, config) for row in rows):
        return "row older than the sentinel"
    return None


def _ledger_row(
    session_name: str | None,
    transcript: Path,
    sentinel: tuple[int, dict[str, Any]] | None,
    config: dict[str, Any],
) -> tuple[Any, str]:
    """The /start-goal ledger row that still describes this goal, else None and why not."""
    if not session_name:
        return None, "ledger: no session name"
    try:
        ledger = _start_goal_ledger()
        # Only rows naming the live transcript, read as it (ISSUE-REF).
        rows = ledger.goal_rows_for(session_name, transcript=transcript)
        moot = (
            _ledger_row_superseded(rows, transcript, sentinel, config) if rows else None
        )
        row = (
            None
            if moot or not rows
            else ledger.active_goal_for(
                session_name,
                transcript=transcript,
                max_scan_bytes=int(config["transcript_scan_bytes"]),
            )
        )
    except Exception as error:  # noqa: BLE001 -- any ledger failure is the designed fallback
        return None, f"ledger unavailable: {type(error).__name__}"
    if moot:
        return None, f"ledger: {moot}"
    if row is None:
        return None, "ledger: no active row"
    stale = _ledger_row_superseded([row], transcript, sentinel, config)
    if stale:
        return None, f"ledger: {stale}"
    return row, ""


def _ledger_decision(
    session_name: str | None,
    transcript: Path,
    sentinel: tuple[int, dict[str, Any]] | None,
    config: dict[str, Any],
) -> tuple[bool | None, str]:
    """D-3 source (i): does a /start-goal ledger row qualify this goal, and how?

    Returns ``(True, "ledger:<template>")`` when it does, else ``(None, <why>)`` and the
    condition patterns decide. A row decides only when its goal is still the newest one: the
    row's ``started_at`` necessarily precedes the sentinel its own landing writes, so the row is
    compared by ``verified_at`` (else ``started_at``) plus ``clock_skew_seconds``, and a sentinel
    newer than that is a hand-typed replacement, which wins.
    """
    row, why = _ledger_row(session_name, transcript, sentinel, config)
    if row is None:
        return None, f"condition-pattern ({why})"
    name = row.template_name or ""
    if name in config.get("qualifying_template_names", []):
        return True, f"ledger:{name}"
    # A drafted or non-qualifying template does not disqualify: its condition may still match.
    return (
        None,
        f"condition-pattern (ledger: {name or 'ad-hoc'} not a qualifying template)",
    )


def _rider_path(condition: Any) -> str:
    """The rider path a goal condition's rider clause names (scripts/goal_rider_notice.py)."""
    if not isinstance(condition, str):
        return ""
    reference = _script("goal_rider_notice").parse_reference(condition)
    return reference[0] if reference else ""


def _qualify(
    offset: int,
    sentinel: tuple[int, dict[str, Any]] | None,
    config: dict[str, Any],
    transcript: Path | None = None,
    session_name: str | None = None,
) -> GoalState:
    """An active goal, qualified ledger first (D-3), then by its sentinel's condition.

    The sentinel is the epoch either way.
    """
    epoch = sentinel[0] if sentinel else offset
    source = "condition-pattern"
    condition = sentinel[1].get("condition") if sentinel else None
    rider = _rider_path(condition)
    if transcript is not None:
        qualifies, source = _ledger_decision(session_name, transcript, sentinel, config)
        if qualifies:
            return GoalState(ACTIVE_QUALIFYING, epoch, source=source, rider=rider)
    if not isinstance(condition, str):
        return GoalState(UNDETERMINED, offset, "no-sentinel-condition", source=source)
    if any(re.search(pattern, condition) for pattern in config["condition_patterns"]):
        return GoalState(ACTIVE_QUALIFYING, epoch, source=source, rider=rider)
    return GoalState(
        ACTIVE_OTHER, epoch, " ".join(condition.split())[:80], source=source
    )


# ------------------------------------------------------------ T-3.2 idle state

IDLE = "IDLE"
BUSY = "BUSY"
HUMAN_GATE = "HUMAN_GATE"

_TURN_END_SUBTYPES = frozenset(
    {"turn_duration", "away_summary", "stop_hook_summary", "compact_boundary"}
)


@dataclass(frozen=True)
class IdleState:
    kind: str
    idle_seconds: float | None = None
    reason: str = ""
    last_tool_use_at: float | None = None
    last_session_start_at: float | None = None
    # This context window's start: the newest SessionStart or compact boundary in the tail,
    # else the tail's oldest record, so a save before an unseen boundary is never counted.
    window_started_at: float | None = None
    # Launch time of the newest background Bash task in the tail with no completion
    # notification yet: the session is waiting on it, not stalled (ISSUE-REF).
    pending_background_launched_at: float | None = None
    # The newest owner interrupt that a later human prompt followed: the owner stopped
    # the turn and redirected it, which cancels a save-state begun before it (ISSUE-REF).
    owner_cancelled_at: float | None = None


def _timestamp(record: dict[str, Any]) -> float | None:
    value = record.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _blocks(record: dict[str, Any], block_type: str) -> list[dict[str, Any]]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == block_type]


def _shape(
    record: dict[str, Any], turn_end: frozenset[str] = _TURN_END_SUBTYPES
) -> str | None:
    """``turn-end``, ``busy:<why>``, or None for a record that says nothing about the turn."""
    kind = record.get("type")
    if kind == "user":
        return "busy:user-record"
    if kind == "assistant":
        return "busy:tool-use" if _blocks(record, "tool_use") else "turn-end"
    if kind == "system":
        subtype = record.get("subtype")
        return "turn-end" if subtype in turn_end else f"busy:system-{subtype}"
    return None


def notice_fields(transcript: Path) -> dict[str, str]:
    """Bounded diagnostics from the newest turn record, never from user messages."""
    snapshot = supervisor._transcript_records(transcript)
    if snapshot is None:
        return {}
    record = next((r for r in reversed(snapshot[0]) if _shape(r) is not None), {})
    if record.get("type") != "system" or record.get("subtype") != "informational":
        return {}
    content = record.get("content")
    lines = content.splitlines() if isinstance(content, str) else []
    first = lines[0][:120] if lines else ""
    first = "".join(c if c.isprintable() else " " for c in first)
    return {"subtype": "informational", "notice": first}


def _pending_question(records: list[dict[str, Any]]) -> bool:
    """An ``AskUserQuestion`` after the last turn end with no tool_result is a human gate."""
    last_end = max(
        (
            index
            for index, record in enumerate(records)
            if record.get("type") == "system"
            and record.get("subtype") == "turn_duration"
        ),
        default=-1,
    )
    asked: set[Any] = set()
    answered: set[Any] = set()
    for record in records[last_end + 1 :]:
        asked |= {
            block.get("id")
            for block in _blocks(record, "tool_use")
            if block.get("name") == "AskUserQuestion"
        }
        answered |= {
            block.get("tool_use_id") for block in _blocks(record, "tool_result")
        }
    return bool(asked - answered)


def _queued(records: list[dict[str, Any]]) -> bool:
    """An item enqueued since the last turn record and not yet removed is a queued turn.

    Measured on ISSUE-REF (2026-10-10): background notifications are enqueued and then
    ``remove``d while the session sits at its prompt, and no turn follows. A drained queue
    leaves the session idle, so only an enqueue without its matching removal counts.
    """
    last_turn = max(
        (i for i, r in enumerate(records) if r.get("type") in {"user", "assistant"}),
        default=-1,
    )
    pending: list[Any] = []
    for record in records[last_turn + 1 :]:
        if record.get("type") != "queue-operation":
            continue
        content = record.get("content")
        if record.get("operation") == "enqueue":
            pending.append(content)
        elif content in pending:
            pending.remove(content)
    return bool(pending)


_TASK_ID = re.compile(r"<task-id>([^<\s]+)</task-id>")


def _pending_background_launched_at(records: list[dict[str, Any]]) -> float | None:
    """A Bash result carries ``toolUseResult.backgroundTaskId`` (explicit or moved on
    timeout); its completion arrives as a ``queue-operation`` enqueue naming the task-id."""
    launched: dict[str, float] = {}
    for record in records:
        result = record.get("toolUseResult")
        task = result.get("backgroundTaskId") if isinstance(result, dict) else None
        if isinstance(task, str) and task and (stamp := _timestamp(record)) is not None:
            launched.setdefault(task, stamp)
        content = record.get("content")
        if (
            record.get("type") == "queue-operation"
            and record.get("operation") == "enqueue"
            and isinstance(content, str)
        ):
            for done in _TASK_ID.findall(content):
                launched.pop(done, None)
    return max(launched.values(), default=None)


# What Claude Code writes when the owner presses Esc or rejects a tool call.
_INTERRUPT_TEXTS = frozenset(
    {"[Request interrupted by user]", "[Request interrupted by user for tool use]"}
)


def _owner_cancelled_at(records: list[dict[str, Any]]) -> float | None:
    cancelled = interrupted = None
    for record in records:
        texts = _message_texts(record) if record.get("type") == "user" else []
        if texts and all(text.strip() in _INTERRUPT_TEXTS for text in texts):
            interrupted = _timestamp(record)
        elif interrupted is not None and _human_prompt(record):
            cancelled, interrupted = interrupted, None
    return cancelled


def _session_started(record: dict[str, Any]) -> bool:
    if record.get("type") == "system" and record.get("subtype") == "compact_boundary":
        return True
    attachment = record.get("attachment")
    return (
        isinstance(attachment, dict)
        and attachment.get("type") == "hook_success"
        and str(attachment.get("hookName", "")).startswith("SessionStart")
    )


def _newest(records: list[dict[str, Any]], predicate) -> float | None:
    for record in reversed(records):
        if predicate(record):
            stamp = _timestamp(record)
            if stamp is not None:
                return stamp
    return None


def idle_state(
    transcript: Path,
    now: float,
    config: dict[str, Any],
    *,
    extra_turn_end_subtypes: frozenset[str] = frozenset(),
) -> IdleState:
    """Whether the session is idle at its prompt, measured from its transcript (D-5).

    ``extra_turn_end_subtypes`` names system-record subtypes a caller reads as a turn end
    on top of the four goal-nudge subtypes. Save-state and restart callers add notices."""
    snapshot = supervisor._transcript_records(transcript)
    if snapshot is None:
        return IdleState(UNDETERMINED, reason="transcript-unreadable")
    records, stat = snapshot
    stamps = [stamp for record in records if (stamp := _timestamp(record)) is not None]
    if not stamps:
        return IdleState(UNDETERMINED, reason="no-timestamped-record")
    newest_at = stamps[-1]
    skew = config["clock_skew_seconds"]
    # Only a record claiming to be newer than the last write is inconsistent. An mtime past the
    # newest record is a write in flight (a trailing partial line is not a record yet), measured
    # live at 141 s to 2865 s ahead, so the session has been quiet only since that write.
    if newest_at - stat.st_mtime > skew:
        return IdleState(UNDETERMINED, reason="clock-skew")
    in_flight = stat.st_mtime - newest_at > skew
    last_write = max(newest_at, stat.st_mtime)
    return _classify_turn(
        records,
        max(0.0, now - last_write),
        config,
        in_flight,
        _TURN_END_SUBTYPES | extra_turn_end_subtypes,
    )


def _classify_turn(
    records: list[dict[str, Any]],
    age: float,
    config: dict[str, Any],
    in_flight: bool = False,
    turn_end: frozenset[str] = _TURN_END_SUBTYPES,
) -> IdleState:
    started = _newest(records, _session_started)
    oldest = next((s for r in records if (s := _timestamp(r)) is not None), None)
    facts = {
        "last_tool_use_at": _newest(records, lambda r: bool(_blocks(r, "tool_use"))),
        "last_session_start_at": started,
        "window_started_at": started if started is not None else oldest,
        "pending_background_launched_at": _pending_background_launched_at(records),
        "owner_cancelled_at": _owner_cancelled_at(records),
    }
    if _pending_question(records):
        return IdleState(HUMAN_GATE, age, "ask-user-question", **facts)
    if _queued(records):
        return IdleState(BUSY, age, "queued", **facts)
    shape = next(
        (s for r in reversed(records) if (s := _shape(r, turn_end)) is not None), None
    )
    if shape is None:
        return IdleState(UNDETERMINED, age, "no-turn-record", **facts)
    if shape != "turn-end":
        return IdleState(BUSY, age, shape.removeprefix("busy:"), **facts)
    if age < config["idle_after_seconds"]:
        reason = "recent-write" if in_flight else "recent-activity"
        return IdleState(BUSY, age, reason, **facts)
    return IdleState(IDLE, age, "turn-ended", **facts)


# ----------------------------------------------------------------- live sessions

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class WakerSession:
    name: str | None
    pid: int
    session_id: str
    transcript: Path

    @property
    def label(self) -> str:
        return self.name or self.session_id


def _registered_session_ids(inventory: types.ModuleType) -> dict[int, str]:
    """pid -> sessionId from VALIDATED registry records, where Claude Code wrote one (D-6)."""
    records = inventory._validated_records(inventory._proc_root(None), None)
    return {
        pid: record["sessionId"]
        for pid, record in records.items()
        if isinstance(record.get("sessionId"), str)
    }


def _transcript_for(
    directory: Path, pid: int, registered: dict[int, str], sharing: int
) -> tuple[Path | None, str]:
    session_id = registered.get(pid)
    if session_id and (directory / f"{session_id}.jsonl").is_file():
        return directory / f"{session_id}.jsonl", ""
    if sharing > 1:
        # Two live sessions in one cwd: "newest transcript" would hand both the same file.
        return None, "shared-cwd-unregistered"
    try:
        candidates = sorted(
            directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return None, "transcript-dir-unreadable"
    if not candidates:
        return None, "no-transcript"
    return candidates[0], ""


def discover_sessions(emit) -> list[WakerSession] | None:
    """Live sessions joined to their transcripts, or None when the inventory is blind."""
    inventory = _script("claude_sessions")
    live = inventory.live_sessions()
    if live is None:
        return None
    registered = _registered_session_ids(inventory)
    projects = Path.home() / ".claude" / "projects"
    by_slug: dict[str, list[Any]] = {}
    for session in live:
        if session.cwd:
            by_slug.setdefault(supervisor._slug(session.cwd), []).append(session)
    found: list[WakerSession] = []
    for slug, members in by_slug.items():
        for member in members:
            path, why = _transcript_for(
                projects / slug, member.pid, registered, len(members)
            )
            label = member.name or str(member.pid)
            if path is None or not _SESSION_ID.match(path.stem):
                emit("GW-SKIP", session=label, reason=why or "bad-session-id")
                continue
            found.append(WakerSession(member.name, member.pid, path.stem, path))
    return found


# ------------------------------------------------------- state and coexistence


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ai-harness" / "goal-waker"


_FRESH_STATE: dict[str, Any] = {
    "epoch_offset": None,
    "nudges_in_epoch": 0,
    "last_nudge_at": None,
    "last_nudge_id": None,
    "last_progress_at": None,
    "last_skip_reason": None,
    "budget_logged_epoch": None,
    # The idle save-state wake's own count, per context window (D-40).
    "save_state_window": None,
    "save_state_wakes": 0,
    "save_state_budget_logged": None,
    # The evaluator-loop clear (ISSUE-REF): the goal it cleared, and its last failure.
    "last_clear_at": None,
    "last_clear_epoch": None,
    "last_clear_failed_at": None,
    "last_clear_failure": None,
    # A typed clear its landing check missed: the goal it was for, its run and its notice.
    "last_clear_failed_goal": None,
}
_LIVE_ATTEMPT_STATES = frozenset({"ARMED", "BOUND", "READY"})
_KEY_DERIVATION_TIMEOUT_SECONDS = 10


class WakerStore:
    """The resume ledger, its marker-only watch directory, and per-session state."""

    def __init__(self, root: Path, state_root: Path, key: str | None = None) -> None:
        self.root = root
        self.state_root = state_root
        self.key = key

    @property
    def wake(self) -> Path:
        return supervisor.wake_directory(self.root, self.key)

    @property
    def bookkeeping(self) -> Path:
        from ai_chassis.wake import sessions

        if self.key is not None and sessions.event_layout(
            sessions.mail_root(), self.key
        ):
            return sessions.directory(self.root, self.key)
        return self.root

    @property
    def claimed(self) -> Path:
        return self.bookkeeping / "goal-nudge-claimed"

    @property
    def expired(self) -> Path:
        return self.bookkeeping / "goal-nudge-expired"

    @property
    def deferred(self) -> Path:
        return self.bookkeeping / "goal-nudge-deferred"

    def prepare(self) -> None:
        for directory in (self.wake, self.claimed, self.expired, self.deferred):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _state_path(self, session_id: str) -> Path:
        from ai_chassis.wake import sessions

        if self.key is not None and sessions.event_layout(
            sessions.mail_root(), self.key
        ):
            return (
                sessions.directory(self.state_root, self.key)
                / f"{sessions.safe_identity(session_id)}.json"
            )
        return self.state_root / "sessions" / f"{session_id}.json"

    def session_state(self, session_id: str) -> dict[str, Any]:
        path = self._state_path(session_id)
        # Migration reads the existing budget until the first session-local
        # update. An unreadable new record must not reset a charged budget.
        if not path.exists() and self.key is not None:
            path = self.state_root / "sessions" / f"{session_id}.json"
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            from ai_chassis.wake import sessions

            if (
                self.key is not None
                and sessions.event_layout(sessions.mail_root(), self.key)
                and path.exists()
            ):
                raise ValueError("invalid-goal-state") from None
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        return {**_FRESH_STATE, **stored}

    def save_state(self, session_id: str, state: dict[str, Any]) -> None:
        from ai_chassis.wake import sessions

        path = self._state_path(session_id)
        if self.key is not None and sessions.event_layout(
            sessions.mail_root(), self.key
        ):
            from ai_chassis.wake.events import write_record

            write_record(path, state)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _held_path(self, name: str) -> Path:
        from ai_chassis.wake import sessions

        if self.key is not None and sessions.event_layout(
            sessions.mail_root(), self.key
        ):
            return sessions.directory(self.state_root, self.key) / "held.json"
        return self.state_root / "held" / f"{name}.json"

    def held(self, name: str) -> dict[str, Any] | None:
        """The hold on typing into ``name``, or None (ISSUE-REF)."""
        path = self._held_path(name)
        if not path.exists() and self.key is not None:
            path = self.state_root / "held" / f"{name}.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            # An unreadable hold is still a hold: releasing it needs the library's proof.
            return {"reason": "unreadable"}
        if isinstance(record, dict) and record.get("released") is True:
            return None
        return record if isinstance(record, dict) else {"reason": "unreadable"}

    def hold(
        self,
        name: str,
        reason: str,
        now: float,
        *,
        fields: dict[str, Any] | None = None,
    ) -> None:
        path = self._held_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        record = {
            "reason": reason,
            "since": _iso(now),
            "since_epoch": now,
            **(fields or {}),
        }
        temporary.write_text(
            json.dumps(record, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)

    def release(self, name: str) -> None:
        from ai_chassis.wake import sessions

        if self.key is not None and sessions.event_layout(
            sessions.mail_root(), self.key
        ):
            from ai_chassis.wake.events import write_record

            write_record(self._held_path(name), {"released": True})
            return
        self._held_path(name).unlink(missing_ok=True)

    def mark_probed(self, name: str, now: float) -> None:
        """Stamp a hold whose composer could not be proved empty, so the next proof waits.

        It rewrites the record through :meth:`hold`, keeping the original reason and age."""
        hold = self.held(name)
        if hold is None:
            return
        kept = {key: value for key, value in hold.items() if key != "reason"}
        self.hold(
            name,
            str(hold.get("reason", "unknown")),
            now,
            fields={**kept, "probed_epoch": now},
        )

    def pending_marker(self, session_id: str, *, exclude: Path | None = None) -> bool:
        return any(
            path != exclude
            for path in self.wake.glob(f"goal-nudge-{session_id}-*.marker")
        )

    def coexistence(self, key: str, now: float, ttl: float) -> str | None:
        """The context supervisor owns this session's next turn, or None (design § coexistence)."""
        resume = supervisor.ResumeStore(self.root)
        # A terminal stamp ends an attempt whatever its state: a library dry run (the
        # composer proof) finishes READY with one, and owns no turn.
        if any(
            r.get("state") in _LIVE_ATTEMPT_STATES and r.get("terminal_at") is None
            for r in resume.read_attempts(key)
        ):
            return "save-state-armed"
        component = supervisor._safe_marker_component(key)
        for marker in self.wake.glob(f"wake-ready-{component}-*.marker"):
            try:
                if now - marker.stat().st_mtime < ttl:
                    return "post-compact-wake-pending"
            except OSError:
                continue
        return None

    def save_state_frozen(self, key: str) -> bool:
        """A /save-state is running for this key: the supervisor's grace marker is live."""
        return supervisor.ResumeStore(self.root).save_state_active(key)

    def compact_requested(self, session: WakerSession, now: float) -> bool:
        """An update hold requests the existing save-state path for this target only."""
        path = (
            self.state_root.parent
            / "update-restart"
            / "sessions"
            / f"{session.session_id}.json"
        )
        if not path.is_file():
            return False
        from ai_chassis import update_restart  # noqa: PLC0415 - imports this module

        return update_restart.compact_requested(session, now=now)

    def compact_in_flight(self, key: str) -> bool:
        return any(
            r.get("attempt_type") == "compact"
            and r.get("state") in supervisor.COMPACT_STATES - supervisor.TERMINAL_STATES
            and r.get("terminal_at") is None
            for r in supervisor.ResumeStore(self.root).read_attempts(key)
        )

    def saved_since(self, key: str, since: float | None) -> bool:
        """Whether a save-state for ``key`` was published, or armed a compact, since ``since``.

        Read from the supervisor's own records: the ``history/<key>.jsonl`` line that
        ``save-resume`` appends once the resume artifact is durable (D-24, D-27), and the
        ``armed_at`` of any attempt. An unknown window start counts nothing as saved.
        """
        if since is None:
            return False
        stamps: list[Any] = []
        history = self.root / "history" / f"{key}.jsonl"
        try:
            lines = history.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                stamps.append(record.get("ts"))
        resume = supervisor.ResumeStore(self.root)
        stamps.extend(record.get("armed_at") for record in resume.read_attempts(key))
        return any(
            isinstance(stamp, (int, float))
            and not isinstance(stamp, bool)
            and stamp >= since
            for stamp in stamps
        )


CTX_PCT = REPO_ROOT / ".claude" / "bin" / "ctx-pct"
_CTX_PCT_TIMEOUT_SECONDS = 10


def context_reading(session: WakerSession) -> int | None:
    """The session's ``advice_pct`` from ``ctx-pct --json``, the band's own source, or None.

    ``advice_pct`` is the number the band is computed from, so the threshold shares the
    band's basis when an auto-compact window is pinned. ``unavailable`` reads as None.
    """
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID=session.session_id)
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, the session id travels in env
            ["bash", str(CTX_PCT), "--json"],  # noqa: S607
            capture_output=True,
            check=False,
            env=env,
            text=True,
            timeout=_CTX_PCT_TIMEOUT_SECONDS,
        )
        snapshot = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    value = snapshot.get("advice_pct") if isinstance(snapshot, dict) else None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def session_key(session: WakerSession) -> str | None:
    """The session's resume key, from ``resume_derive_key()`` itself, never a second copy.

    The library reads the identity from its environment, so the session's own id and name
    are supplied the way that session's hooks would see them.
    """
    library = supervisor._resume_library()
    if not library.is_file():
        return None
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "CLAUDE_CODE_SESSION_ID": session.session_id,
    }
    if session.name:
        env["CLAUDE_CODE_TASK_LIST_ID"] = session.name
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, the identity travels in env
            ["bash", "-c", supervisor._DERIVE_KEY_SNIPPET, "goal-waker", str(library)],
            capture_output=True,
            check=False,
            env=env,
            text=True,
            timeout=_KEY_DERIVATION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    key = result.stdout.strip()
    if result.returncode != 0 or not key or key == "unknown":
        return None
    return key


# ------------------------------------------------------ T-3.3 decide


@dataclass(frozen=True)
class Decision:
    action: str
    event: str
    reason: str
    state: dict[str, Any]
    nudge_index: int = 0
    threshold: float | None = None


_GOAL_GATES = {
    ACTIVE_QUALIFYING: None,
    ACTIVE_OTHER: "not-qualifying",
    # D-4 as amended (ISSUE-REF): a rejected declaration is a loop, never a goal to nudge.
    EVALUATOR_LOOP: "evaluator-loop",
    ENDED: "goal-ended",
    NO_GOAL: "no-goal",
    UNDETERMINED: "undetermined",
}
_IDLE_GATES = {
    IDLE: None,
    BUSY: "not-idle",
    HUMAN_GATE: "human-gate",
    UNDETERMINED: "undetermined",
}
_LOOP_IDLE_GATES = dict(_IDLE_GATES, **{BUSY: None})
_MINUTES_PER_DAY = 24 * 60


def _skip(reason: str, state: dict[str, Any], **extra: Any) -> Decision:
    return Decision(
        "SKIP", "GW-SKIP", reason, dict(state, last_skip_reason=reason), **extra
    )


def _minutes(clock: str) -> int:
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


def in_quiet_hours(windows: list[str], now: float) -> bool:
    local = datetime.fromtimestamp(now)
    current = local.hour * 60 + local.minute
    for window in windows:
        start, end = (_minutes(part) for part in window.split("-"))
        inside = (
            start <= current < end
            if start <= end
            else current >= start or current < end % _MINUTES_PER_DAY
        )
        if inside:
            return True
    return False


def _advance(
    state: dict[str, Any], epoch: int | None, idle: IdleState
) -> dict[str, Any]:
    """A new sentinel starts a new epoch; a tool call since the last nudge resets the count."""
    state = dict(state)
    if state.get("epoch_offset") != epoch:
        state.update(epoch_offset=epoch, nudges_in_epoch=0, budget_logged_epoch=None)
    last_nudge = state.get("last_nudge_at")
    progress = idle.last_tool_use_at
    if last_nudge is not None and progress is not None and progress > last_nudge:
        state.update(nudges_in_epoch=0, last_progress_at=progress)
    return state


def _held(  # noqa: PLR0913 - shared gates also reconcile an already charged marker
    session: WakerSession,
    idle: IdleState,
    store: WakerStore,
    config: dict[str, Any],
    now: float,
    *,
    pending: Path | None = None,
) -> str | None:
    started = idle.last_session_start_at
    if started is not None and now - started < config["post_compact_grace_seconds"]:
        return "post-compact-grace"
    if store.pending_marker(session.session_id, exclude=pending):
        return "marker-pending"
    key = session_key(session)
    if key is None:
        return "key-undetermined"
    return store.coexistence(key, now, config["marker_ttl_seconds"])


def decide(  # noqa: PLR0913 - the five positionals are T-3.3's named contract
    session: WakerSession,
    goal: GoalState,
    idle: IdleState,
    store: WakerStore,
    config: dict[str, Any],
    *,
    now: float | None = None,
) -> Decision:
    """Whether to nudge one session now (design § Budget, back-off and coexistence)."""
    now = time.time() if now is None else now
    state = store.session_state(session.session_id)
    if quota_traffic_paused(session):
        return _skip("quota-traffic-paused", state)
    gate = _GOAL_GATES[goal.kind] or _IDLE_GATES[idle.kind]
    if gate is not None:
        return _skip(gate, state)
    state = _advance(state, goal.epoch, idle)
    if in_quiet_hours(config["quiet_hours"], now):
        return Decision(
            "SKIP", "GW-QUIET", "quiet", dict(state, last_skip_reason="quiet")
        )
    held = _held(session, idle, store, config, now)
    if held is not None:
        return _skip(held, state)
    return _budgeted(state, goal, idle, config)


def _budgeted(
    state: dict[str, Any], goal: GoalState, idle: IdleState, config: dict[str, Any]
) -> Decision:
    """The per-epoch cap, then the back-off threshold for the next nudge (D-9)."""
    count = state["nudges_in_epoch"]
    if count >= config["max_nudges_per_epoch"]:
        if state.get("budget_logged_epoch") == goal.epoch:
            return _skip("budget", state)
        spent = dict(state, budget_logged_epoch=goal.epoch, last_skip_reason="budget")
        return Decision("SKIP", "GW-BUDGET", "budget", spent)
    threshold = min(
        config["idle_after_seconds"] * config["backoff_factor"] ** count,
        config["max_backoff_seconds"],
    )
    if (idle.idle_seconds or 0) < threshold:
        return _skip("backoff", state, threshold=threshold)
    return Decision("ISSUE", "GW-ISSUE", "idle", state, count, threshold)


def _save_state_held(  # noqa: PLR0913 - shared gates also reconcile an already charged marker
    session: WakerSession,
    idle: IdleState,
    store: WakerStore,
    config: dict[str, Any],
    now: float,
    *,
    pending: Path | None = None,
) -> str | None:
    """The waker's coexistence gates, then the save-state freeze and this window's save."""
    held = _held(session, idle, store, config, now, pending=pending)
    if held is not None:
        return held
    key = session_key(session)
    if key is None:
        return "key-undetermined"
    if store.save_state_frozen(key):
        return "save-state-active"
    if store.compact_requested(session, now):
        return "save-state-armed" if store.compact_in_flight(key) else None
    if store.saved_since(key, idle.window_started_at):
        return "already-saved"
    return None


def decide_save_state(  # noqa: PLR0911, PLR0913 - explicit ordered safety gates
    session: WakerSession,
    idle: IdleState,
    store: WakerStore,
    config: dict[str, Any],
    after_seconds: int,
    *,
    now: float,
) -> Decision:
    """Whether to wake an idle session at or above the threshold to save state (D-40).

    The caller has already measured the session idle and its context at or above the
    threshold. Background work in flight does not hold the wake (D-40 (c)).
    """
    state = dict(store.session_state(session.session_id))
    if quota_traffic_paused(session):
        return _skip("quota-traffic-paused", state)
    window = idle.window_started_at
    if state.get("save_state_window") != window:
        state.update(
            save_state_window=window, save_state_wakes=0, save_state_budget_logged=None
        )
    if in_quiet_hours(config["quiet_hours"], now):
        return Decision(
            "SKIP", "GW-QUIET", "quiet", dict(state, last_skip_reason="quiet")
        )
    held = _save_state_held(session, idle, store, config, now)
    if held is not None:
        return _skip(held, state)
    count = state["save_state_wakes"]
    if count >= config["max_nudges_per_epoch"]:
        if state.get("save_state_budget_logged") == window:
            return _skip("budget", state)
        spent = dict(state, save_state_budget_logged=window, last_skip_reason="budget")
        return Decision("SKIP", "GW-BUDGET", "budget", spent)
    threshold = min(
        after_seconds * config["backoff_factor"] ** count,
        config["max_backoff_seconds"],
    )
    if (idle.idle_seconds or 0) < threshold:
        return _skip("backoff", state, threshold=threshold)
    return Decision("ISSUE", "GW-ISSUE", "idle-save-state", state, count, threshold)


def decide_loop(  # noqa: PLR0911, PLR0913 - explicit ordered safety gates
    session: WakerSession,
    goal: GoalState,
    idle: IdleState,
    store: WakerStore,
    config: dict[str, Any],
    *,
    now: float,
) -> Decision:
    """Whether to clear an evaluator-looping goal now (ISSUE-REF; D-4 as amended).

    A busy session is cleared too (ISSUE-REF): a loop inside one long turn never goes idle,
    and ISSUE-REF's hand-typed ``/goal clear`` landed mid-burst through this same use case. A
    busy turn with a tool call newer than the newest verdict may be answering it with
    work, so it waits for the next verdict. A clear that failed waits
    ``max_backoff_seconds`` before the next try, never retried blindly, and a goal this
    waker already cleared is not cleared again.
    """
    state = store.session_state(session.session_id)
    if quota_traffic_paused(session):
        return _skip("quota-traffic-paused", state)
    gate = (
        "working-since-verdict"
        if idle.kind == BUSY and goal.working_since_verdict
        else _LOOP_IDLE_GATES[idle.kind]
    )
    if gate is not None:
        return _skip(gate, state)
    if not config["loop_clear_enabled"]:
        return _skip("loop-clear-disabled", state)
    if session.name is None:
        return _skip("no-live-named-session", state)
    if state.get("last_clear_epoch") == goal.epoch:
        return _skip("already-cleared", state)
    failed = state.get("last_clear_failed_at")
    backoff = _clear_backoff(state, config)
    if isinstance(failed, (int, float)) and now - failed < backoff:
        return _skip("clear-backoff", state, threshold=float(backoff))
    return Decision("CLEAR", "GW-CLEAR", "evaluator-loop", state)


def quota_traffic_paused(session: WakerSession) -> bool:
    """Fail open unless an existing optional controller store records a pause."""
    if session.name is None:
        return False
    traffic = None
    try:
        # By name, so the optional controller is not part of this module's import closure.
        service = quota_optional.component("service")
        store = quota_optional.component("store")
        if service is None or store is None:
            return False

        path = service.store_file()
        if not path.exists():
            return False
        traffic = store.Store(path)
        saved = traffic.session(os.environ.get("AI_HOST", ""), session.name)
        return saved.ok and saved.value is not None and saved.value.state == "paused"
    except Exception:  # noqa: BLE001 - an unavailable optional controller cannot fail the waker
        return False
    finally:
        if traffic is not None:
            traffic.close()


def active_goal_text(transcript: Path) -> str | None:
    """Return the newest armed goal as a command suitable for re-arming."""
    try:
        wq = _script("wq_client")
        found = wq.find_last_goal_status(transcript, 0)
        if found is None or wq.goal_cleared(found[1]):
            return None
        sentinel = found if found[1].get("sentinel") else _last_sentinel(transcript)
        condition = sentinel[1].get("condition") if sentinel else None
        if not isinstance(condition, str) or not condition.strip():
            return None
        condition = condition.strip()
        return condition if condition.startswith("/goal") else f"/goal {condition}"
    except Exception:  # noqa: BLE001 - unreadable goal state is not resumable text
        return None


def guarded_goal_clear(session_name: str, transcript: Path) -> InjectionResult:
    """Phase 3's public guarded-clear adapter over the waker's existing primitive."""
    return TypedFallback().clear_goal(
        session_name,
        "Quota pause goal clear did not land; keep the goal paused and notify the host chief.",
        transcript,
    )


def rearm_goal(session_name: str, goal_text: str) -> str:
    """Re-arm exactly the goal text durably captured before a quota pause."""
    return TypedFallback().deliver(session_name, goal_text)


# Refusals the library decided on the pane before typing anything: what it saw was not a
# ready composer, a view a later tick need not see. example-session (2026-10-14 06:30Z, 07:30Z)
# and job-fw-1 (2026-10-15 00:28Z) refused not-ready and then waited the full hour.
_UNTYPED_CLEAR_REFUSALS = frozenset(
    {"not-ready", "pane-in-mode", "injection-in-flight"}
)


def _clear_backoff(state: dict[str, Any], config: dict[str, Any]) -> float:
    """How long a failed clear waits: ``loop_clear_retry_seconds`` times ``backoff_factor``
    per earlier such refusal for an untyped pane refusal, else ``max_backoff_seconds``."""
    ceiling = float(config["max_backoff_seconds"])
    if state.get("last_clear_staged") or (
        state.get("last_clear_failure") not in _UNTYPED_CLEAR_REFUSALS
    ):
        return ceiling
    tries = max(1, int(state.get("clear_quick_failures") or 1))
    retry = config["loop_clear_retry_seconds"] * config["backoff_factor"] ** (tries - 1)
    return min(float(retry), ceiling)


# ------------------------------------------------- T-3.4 markers and lifecycle

_MARKER_NAME = re.compile(r"^goal-nudge-(?P<sid>.+)-(?P<nonce>[0-9a-f]{16})\.marker$")
_SWEEP_AFTER_SECONDS = 24 * 60 * 60


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def issue(  # noqa: PLR0913 - keyword-only header fields beyond the session and text
    session: WakerSession,
    text: str,
    root: Path,
    *,
    epoch_offset: int | None,
    idle_minutes: int = 0,
    now: float | None = None,
    save_state: tuple[int, int] | None = None,
    kind: str | None = None,
    rider: str = "",
    paused_goal: bool = False,
    nonce: str | None = None,
) -> Path:
    """Publish one nudge marker atomically, mode 0600, into the wake directory.

    ``save_state`` is ``(context_pct, threshold_pct)`` for an idle save-state wake (D-40):
    the header then names ``kind=save-state`` and both readings, so the typed fallback
    renders the matching one-line text. The hook reads only the body either way.
    """
    from ai_chassis.wake import sessions
    from ai_chassis.wake.events import sync_directory

    key = session.name or session.session_id
    if sessions.event_layout(sessions.mail_root(), key) and not sessions.registered(
        sessions.mail_root(), key, session.session_id
    ):
        raise ValueError("watch-registration-pending")
    nonce = nonce or secrets.token_hex(8)
    if not re.fullmatch(r"[0-9a-f]{16}", nonce):
        raise ValueError("invalid-marker-nonce")
    directory = supervisor.wake_directory(root, session.name or session.session_id)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = directory / f"goal-nudge-{session.session_id}-{nonce}.marker"
    if marker.exists() or any(
        (directory.parent / group / marker.name).exists()
        for group in ("goal-nudge-claimed", "goal-nudge-expired")
    ):
        return marker
    kind = f"kind={kind or SAVE_STATE_KIND}\n" if kind or save_state else ""
    readings = (
        f"context_pct={save_state[0]}\nthreshold_pct={save_state[1]}\n"
        if save_state
        else ""
    )
    rider_field = f"rider={rider}\n" if rider else ""
    paused_field = "paused_goal=true\n" if paused_goal else ""
    contents = (
        f"session_id={session.session_id}\n"
        f"nudge_id={nonce}\n"
        f"{kind}"
        f"issued_at={_iso(time.time() if now is None else now)}\n"
        f"epoch_offset={epoch_offset}\n"
        f"idle_minutes={idle_minutes}\n"
        f"{readings}"
        f"{paused_field}"
        f"{rider_field}"
        f"text_sha256={text_digest(text)}\n"
        f"\n{text}\n"
    )
    # The temporary name must not match the hook's `goal-nudge-.*\.marker` matcher, or its
    # creation would spawn a hook in every live session for nothing.
    temporary = directory / f".gw-tmp-{nonce}-{secrets.token_hex(4)}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(contents)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, marker)
    sync_directory(directory)
    return marker


def read_marker(path: Path) -> tuple[dict[str, str], str]:
    header, _, body = path.read_text(encoding="utf-8").partition("\n\n")
    fields = {}
    for line in header.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value
    return fields, body.rstrip("\n")


def _int_field(fields: dict[str, str], key: str) -> int:
    try:
        return max(0, int(fields[key]))
    except (KeyError, ValueError):
        return 0


def _utc_stamp(epoch: float) -> str:
    """``epoch`` as ``YYYY-MM-DDTHH:MM:SSZ``, so a notice can say when its claim was true."""
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _issued_at(fields: dict[str, str], marker: Path) -> float:
    try:
        return datetime.fromisoformat(
            fields["issued_at"].replace("Z", "+00:00")
        ).timestamp()
    except (KeyError, ValueError):
        return marker.stat().st_mtime


# ------------------------------------------------- T-3.6 typed fallback adapter


class FallbackUnavailable(RuntimeError):
    """The shared injection library (ISSUE-REF) is not importable."""


class ClearRaised(RuntimeError):
    """The library's ``goal-clear`` run raised; ``notice`` is the failure notice left for
    the session in its place, if the run got far enough to key one."""

    def __init__(self, error: Exception, notice: Path | None) -> None:
        super().__init__(f"{type(error).__name__}: {error}")
        self.error = error
        self.notice = notice


class FallbackRefused(RuntimeError):
    """The library refused or could not deliver the nudge."""

    def __init__(
        self, reason: str, detail: str | None = None, *, staged: bool = False
    ) -> None:
        super().__init__(reason if detail is None else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        # True when the library typed text before refusing, so the composer may hold it.
        self.staged = staged


@dataclass(frozen=True)
class InjectionResult:
    """One library run of the evaluator-loop clear or its follow-up, as the tick logs it."""

    state: str
    reason: str | None
    run_id: str | None
    ok: bool
    staged: bool = False
    notice: Path | None = None
    # Not a state the library saw land, but the transcript's goal ended after the run.
    confirmed: bool = False


def _run_id(outcome: Any) -> str | None:
    """The run id from the outcome's run-record path (``runs/<run_id>``)."""
    run = (getattr(outcome, "evidence", None) or {}).get("run")
    return Path(run).name if run else None


def _goal_ended(transcript: Path) -> bool:
    wq = _script("wq_client")
    try:
        found = wq.find_last_goal_status(transcript)
    except (wq.GoalProbeFailed, OSError, ValueError):
        return False
    return found is not None and wq.goal_cleared(found[1])


# A clear typed and submitted whose record the landing check did not see: mid-turn, Claude
# Code writes the record when the in-flight message ends (example-session, 2026-10-29T06:06Z).
_UNSEEN_CLEAR_FAILURES = frozenset({"landing-none"})


def _cleared_goal_armed_at(transcript: Path, floor: int) -> int | None:
    """When the newest ``goal_status`` is ``/goal clear``'s own record (``met`` and
    ``sentinel``), the byte offset of the sentinel that armed the goal it cleared."""
    wq = _script("wq_client")
    newest = True
    for start, raw in wq._transcript_lines_backwards(transcript, floor):
        if _GOAL_MARKER not in raw:
            continue
        attachment = _goal_attachment(raw)
        if attachment is None:
            continue
        if newest:
            if not (attachment.get("met") and attachment.get("sentinel")):
                return None
            newest = False
        elif attachment.get("sentinel") and not wq.goal_cleared(attachment):
            return start
    return None


class TypedFallback:
    """The typed leg: the only keystroke route, through ``ai_chassis.prompt_injection``."""

    library_name = "ai_chassis.prompt_injection"

    def _library(self) -> types.ModuleType:
        try:
            return importlib.import_module(self.library_name)
        except ImportError as error:
            raise FallbackUnavailable(f"{self.library_name}: {error}") from error

    def check(self) -> None:
        self._library()

    def typed_limit(self) -> int:
        return int(self._library().load_config()["max_typed_chars"])

    def _target(self, library: types.ModuleType, session_name: str) -> Any:
        try:
            return library.resolve_target(f"session:{session_name}")
        except library.TargetUnresolved as error:
            raise FallbackRefused("target-unresolved", str(error)) from error

    def _injected(
        self,
        injector: Any,
        outcome: Any,
        cleared: frozenset[str],
        notice_text: str,
        *,
        confirmed: bool = False,
    ) -> InjectionResult:
        ok = confirmed or outcome.state in cleared
        # goal-clear is self-rescue and never held; a held loop nudge is delivered by the
        # library's drain, so the session is not told it failed.
        notice = (
            None
            if ok or outcome.state == "HELD"
            else injector.notify_failure(outcome, notice_text)
        )
        return InjectionResult(
            outcome.state,
            outcome.reason,
            _run_id(outcome),
            ok,
            bool(getattr(outcome, "staged", False)) and not confirmed,
            notice,
            confirmed,
        )

    def clear_goal(
        self, session_name: str, notice_text: str, transcript: Path
    ) -> InjectionResult:
        """Type and submit ``/goal clear`` through the library's ``goal-clear`` use case
        (ISSUE-REF), refusing a goal that already ended; a failure leaves ``notice_text``
        for the session's next prompt.

        A run the library could not see land counts as landed when ``transcript``'s newest
        ``goal_status`` now reads cleared: mid-burst, Claude Code writes that status before
        any record the landing check matches (ISSUE-REF, 2026-10-13T12:38:29Z, ISSUE-REF)."""
        library = self._library()
        target = self._target(library, session_name)
        injectors = library.injector
        injector = injectors.GoalClearInjector(observers=[], require_goal_active=True)
        try:
            outcome = injector.run(target, library.SlashCommandPayload("/goal clear"))
        except Exception as error:
            raise ClearRaised(
                error, injector.notify_exception(error, notice_text)
            ) from error
        cleared = injectors.GOAL_CLEARED_STATES
        return self._injected(
            injector,
            outcome,
            cleared,
            notice_text,
            confirmed=outcome.state not in cleared and _goal_ended(transcript),
        )

    def loop_nudge(
        self, session_name: str, text: str, notice_text: str | None = None
    ) -> InjectionResult:
        """Type one line after a clear; a failure leaves ``notice_text`` (default the
        line itself) as the session's notice."""
        library = self._library()
        target = self._target(library, session_name)
        injectors = library.injector
        injector = injectors.GoalLoopNudgeInjector(observers=[])
        outcome = injector.run(target, library.PromptPayload(text))
        # QUEUED is accepted, not failed: the library delivers the line at the session's
        # next input, so a failure notice here would be a second, duplicate copy of it
        # (the first live clear, 2026-10-18, ISSUE-REF).
        return self._injected(
            injector,
            outcome,
            injectors.GOAL_CLEARED_STATES | {"QUEUED"},
            notice_text or text,
        )

    def deliver(self, session_name: str, text: str) -> str:
        """Type ``text`` (the compact one-line nudge) under the library's live typed limit."""
        library = self._library()
        limit = int(library.load_config()["max_typed_chars"])
        if len(text) > limit:
            raise FallbackRefused("compact-too-long", f"{len(text)} > {limit}")
        try:
            target = library.resolve_target(f"session:{session_name}")
        except library.TargetUnresolved as error:
            raise FallbackRefused("target-unresolved", str(error)) from error
        outcome = library.IdleNudgeInjector(observers=[]).run(
            target, library.PromptPayload(text)
        )
        if outcome.state == "REFUSED":
            raise FallbackRefused(
                outcome.reason or "refused",
                staged=bool(getattr(outcome, "staged", False)),
            )
        return f"state={outcome.state} landing={outcome.landing} chars={len(text)}"

    def composer_empty(
        self, session_name: str, text: str, *, target_spec: str | None = None
    ) -> bool:
        """The library's own proof that the composer is empty, typing nothing.

        A ``dry_run`` runs the library's preflight and readiness wait, which requires an
        empty composer, and stops before the first keystroke. Anything but its READY
        ``dry-run`` answer, an exception included, is not that proof (ISSUE-REF).
        """
        library = self._library()
        try:
            target = library.resolve_target(target_spec or f"session:{session_name}")
            outcome = library.IdleNudgeInjector(observers=[]).run(
                target, library.PromptPayload(text), dry_run=True
            )
        except Exception:  # noqa: BLE001 - no proof, so the hold stands
            return False
        return outcome.state == "READY" and outcome.reason == "dry-run"


# ------------------------------------------------------------------- the tick


def _quote(value: Any) -> str:
    text = str(value)
    return json.dumps(text) if not text or re.search(r'[\s"=]', text) else text


class _Tick:
    def __init__(self, config: dict[str, Any], *, dry_run: bool, now: float) -> None:
        self.config = config
        self.dry_run = dry_run
        self.now = now
        self.operation_key = None
        self.events: list[str] = []
        self.decisions: list[dict[str, Any]] = []
        self.store = WakerStore(supervisor._root_from_environment(), state_dir())
        self.fallback = TypedFallback()

    def emit(self, event: str, **fields: Any) -> None:
        parts = [
            _iso(time.time()),
            event,
            *(f"{k}={_quote(v)}" for k, v in fields.items()),
        ]
        line = " ".join(parts)
        self.events.append(line)
        if not self.dry_run:
            log = self.store.state_root / "goal-waker.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def result(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "events": self.events,
            "decisions": self.decisions,
        }

    def automatic_wakes(self, session: str) -> bool:
        """Call-time admission only; classification and native goal state remain intact."""
        policy = idle_wake.load_policy()
        if policy.automatic_wakes:
            return True
        self.emit(
            "GW-SUPPRESSED",
            session=session,
            reason="automatic-wakes-off",
            policy=policy.reason,
            save_state_protection="disabled",
        )
        return False

    # -- marker lifecycle

    def markers(self, sessions: dict[str, WakerSession], variant: str) -> None:  # noqa: C901, PLR0912 - recovery bypasses the typed fallback
        if not self.store.wake.is_dir():
            return
        for marker in sorted(self.store.wake.glob("goal-nudge-*.marker")):
            match = _MARKER_NAME.match(marker.name)
            if match is None:
                continue
            try:
                fields, _ = read_marker(marker)
                age = self.now - _issued_at(fields, marker)
            except OSError:
                continue
            sid, nonce = match["sid"], match["nonce"]
            # A quota hold must not age a durable marker out of existence.
            if not marker_admitted(marker):
                if not self.dry_run:
                    (self.store.deferred / marker.name).touch(mode=0o600, exist_ok=True)
                self.emit("GW-SUPPRESSED", session=sid, reason="quota-held")
                continue
            from ai_chassis.quota_optional import admission

            quota_held = any(
                row["item"] == marker.name
                for row in admission.held_items(sid, source="goal-marker")
            )
            # Recovery is notification-only: never type it over a draft or replace
            # its diagnostic brief with the ordinary compact stay-busy rendering.
            if fields.get("kind") == "compact-rearm":
                from ai_chassis import compact_rearm  # noqa: PLC0415

                compact_rearm.expire_marker(self, marker, sessions.get(sid), age)
                if marker.exists():
                    self.reoffer(marker, sessions.get(sid), fields)
                continue
            # ISSUE-REF T-3.4: a first-class kind with its own lifecycle and body.
            if fields.get("kind") == to_review_reminder.KIND:
                to_review_reminder.handle_marker(
                    self, marker, sessions.get(sid), fields, age
                )
                continue
            session = sessions.get(sid)
            goal = goal_state(session.transcript, self.config) if session else None
            paused_save = (
                fields.get("kind") == SAVE_STATE_KIND
                and fields.get("paused_goal") == "true"
                and session is not None
                and notice_fields(session.transcript)
                .get("notice", "")
                .startswith("Goal paused")
            )
            if (
                goal
                and (goal.rejected_terminal or goal.kind == EVALUATOR_LOOP)
                and not paused_save
            ):
                if not self.dry_run:
                    os.replace(marker, self.store.expired / marker.name)
                    marker_completed(marker)
                self.emit(
                    "GW-EXPIRED", session=sid, nudge=nonce, reason="rejected-terminal"
                )
                continue
            if age >= self.config["marker_ttl_seconds"] and not quota_held:
                if not self.dry_run:
                    os.replace(marker, self.store.expired / marker.name)
                    marker_completed(marker)
                self.emit("GW-EXPIRED", session=sid, nudge=nonce, age=int(age))
            elif self.reoffer(marker, session, fields):
                continue
            elif age >= self.config["fallback_after_seconds"]:
                # Idle at issue, plus the time the marker has waited unclaimed.
                idle = _int_field(fields, "idle_minutes") + int(age) // 60
                self.typed(
                    marker,
                    sid,
                    nonce,
                    session.name if session else None,
                    compact=self.compact_renderer(fields, variant, idle),
                )

    def reoffer(
        self, marker: Path, session: WakerSession | None, fields: dict[str, str]
    ) -> bool:
        """Re-emit a switch-refused file event once, keeping its charged obligation.

        The hook records refusals outside the watch, including off/on edits between
        ticks. A disabled tick records them too. Never recreate a marker: an open
        descriptor stays with its inode if a concurrent hook wins the rename claim.
        """
        deferred = self.store.deferred / marker.name
        offering = deferred.with_suffix(".reoffering")
        if not self.automatic_wakes(fields.get("session_id", "unknown")):
            if not self.dry_run:
                deferred.touch(mode=0o600, exist_ok=True)
            return True
        if not deferred.is_file() and not offering.is_file():
            return False
        if session is None or not self.reoffer_eligible(marker, session, fields):
            return True
        if not self.automatic_wakes(session.label):
            return True
        if not self.dry_run:
            try:
                with marker.open("r+b") as handle:
                    first = handle.read(1)
                    handle.seek(0)
                    # Move before emission so a fresh hook refusal cannot be lost.
                    # A crash here leaves a retry record for the next reconciliation.
                    if deferred.is_file():
                        os.replace(deferred, offering)
                    handle.write(first)
                    handle.flush()
                    offering.unlink(missing_ok=True)
            except FileNotFoundError:  # A concurrent hook already claimed this inode.
                return True
        self.emit("GW-REOFFER", session=session.label, nudge=fields.get("nudge_id", ""))
        return True

    def reoffer_eligible(  # noqa: C901, PLR0911 - preserve each lifecycle refusal
        self, marker: Path, session: WakerSession, fields: dict[str, str]
    ) -> bool:
        """Recheck source/idle/coexistence predicates, without charging another wake."""
        if in_quiet_hours(self.config["quiet_hours"], self.now):
            return False
        key = session_key(session)
        # Recovery can repair a stalled save-state freeze; ordinary wakes stay held.
        if fields.get("kind") == "compact-rearm":
            from ai_chassis import compact_rearm  # noqa: PLC0415

            return key is not None and compact_rearm.safe_to_remind(
                session.transcript, compact_rearm._pane_for_session(session), self.now
            )
        if key is None or self.store.save_state_frozen(key):
            return False
        if fields.get("kind") == to_review_reminder.KIND:
            return to_review_reminder.reoffer_eligible(self, session)
        goal = goal_state(session.transcript, self.config, session.name)
        if fields.get("kind") == SAVE_STATE_KIND:
            settings = supervisor.idle_save_state_settings()
            # Save-state reading includes informational notices, as at publication.
            idle = idle_state(
                session.transcript,
                self.now,
                dict(
                    self.config,
                    idle_after_seconds=settings["idle_save_state_after_seconds"],
                ),
                extra_turn_end_subtypes=frozenset({"informational"}),
            )
            if idle.kind != IDLE:
                return False
            context = context_reading(session)
            if context is None or (
                context < settings["idle_save_state_pct"]
                and not self.store.compact_requested(session, self.now)
            ):
                return False
            if (
                self.store.session_state(session.session_id)["save_state_window"]
                != idle.window_started_at
            ):
                return False
            return (
                _save_state_held(
                    session, idle, self.store, self.config, self.now, pending=marker
                )
                is None
            )
        # Goal reading keeps the four-subtype refusal of informational goal pauses.
        idle = idle_state(session.transcript, self.now, self.config)
        if _GOAL_GATES[goal.kind] or _IDLE_GATES[idle.kind]:
            return False
        if fields.get("epoch_offset") != str(goal.epoch):
            return False
        return (
            _held(session, idle, self.store, self.config, self.now, pending=marker)
            is None
        )

    def compact_renderer(
        self, fields: dict[str, str], variant: str, idle_minutes: int
    ) -> Callable[[], str]:
        """The one-line text matching the marker's kind: goal nudge or save-state (D-40)."""
        limit = self.config["max_nudge_chars"]
        if fields.get("kind") == SAVE_STATE_KIND:
            return lambda: render_save_state_nudge(
                context_pct=_int_field(fields, "context_pct"),
                threshold_pct=_int_field(fields, "threshold_pct"),
                idle_minutes=idle_minutes,
                max_chars=limit,
                compact=True,
                paused_goal=fields.get("paused_goal") == "true",
            )
        return lambda: render_nudge(
            variant,
            idle_minutes=idle_minutes,
            max_chars=limit,
            compact=True,
            rider=fields.get("rider", ""),
        )

    def typed(  # noqa: PLR0911, PLR0913 - explicit claim gates; marker identity and rendering
        self,
        marker: Path,
        sid: str,
        nonce: str,
        name: str | None,
        *,
        compact: Callable[[], str],
    ) -> None:
        """D-10: type only an unclaimed marker, with the library present, claiming it first."""
        if not self.config["typed_fallback_enabled"]:
            self.emit(
                "GW-UNCLAIMED", session=sid, nudge=nonce, reason="typed-fallback-off"
            )
            return
        if not self.automatic_wakes(name or sid):
            return
        try:
            self.fallback.check()
        except FallbackUnavailable as error:
            self.emit("GW-FALLBACK-UNAVAILABLE", session=sid, nudge=nonce, reason=error)
            return
        if name is None:
            self.emit(
                "GW-UNCLAIMED", session=sid, nudge=nonce, reason="no-live-named-session"
            )
            return
        if self.dry_run:
            self.emit("GW-FALLBACK", session=name, nudge=nonce, detail="dry-run")
            return
        if (
            not self.unheld(name, nonce, compact=compact)
            or not marker_admitted(marker)
            or not self.automatic_wakes(name)
        ):
            return
        try:
            os.rename(marker, self.store.claimed / marker.name)
        except FileNotFoundError:
            return
        self.deliver(name, nonce, compact=compact)
        marker_completed(marker)

    def deliver(self, name: str, nonce: str, *, compact: Callable[[], str]) -> None:
        """Type the one-line compact nudge (D-13); the claimed marker keeps the full text."""
        try:
            text = compact()
            detail = self.fallback.deliver(name, text)
        except FallbackRefused as error:
            extra = {} if error.detail is None else {"detail": error.detail}
            self.emit(
                "GW-FALLBACK-REFUSED",
                session=name,
                nudge=nonce,
                reason=error.reason,
                **extra,
            )
            if error.staged:
                self.store.hold(name, error.reason, self.now)
            return
        except Exception as error:  # noqa: BLE001 - any library failure is a refusal here
            self.emit("GW-FALLBACK-REFUSED", session=name, nudge=nonce, reason=error)
            # Whether it typed before failing is unknown, so it is held like a staged refusal.
            self.store.hold(name, "library-error", self.now)
            return
        self.emit("GW-FALLBACK", session=name, nudge=nonce, detail=detail)

    def unheld(self, name: str, nonce: str, *, compact: Callable[[], str]) -> bool:
        """D-10 held-after-staging (ISSUE-REF): a session whose last typed attempt may
        have left text in its composer is not typed into again until the library proves
        the composer empty. The marker stays unclaimed, so it expires by TTL as before."""
        hold = self.store.held(name)
        if hold is None:
            return True
        # An empty composer is not enough on its own (ISSUE-REF): the library's undo
        # leaves it empty, so the same payload into the same frame would refuse again.
        # Within the window nothing is probed or typed; a record with no stamp (older, or
        # unreadable) is past it, so the dry-run proof below still decides.
        since = hold.get("since_epoch")
        if isinstance(since, (int, float)) and (
            self.now - since < self.config["held_retry_after_seconds"]
        ):
            self.emit(
                "GW-FALLBACK-HELD",
                session=name,
                nudge=nonce,
                reason="refused-recently",
                after=hold.get("reason", "unknown"),
                retry_in=int(
                    self.config["held_retry_after_seconds"] - (self.now - since)
                ),
            )
            return False
        try:
            empty = self.fallback.composer_empty(name, compact())
        except Exception:  # noqa: BLE001 - no proof of an empty composer, so it stays held
            empty = False
        if empty:
            self.store.release(name)
            self.emit("GW-FALLBACK-RELEASED", session=name, nudge=nonce)
            return True
        self.emit(
            "GW-FALLBACK-HELD",
            session=name,
            nudge=nonce,
            reason="composer-may-hold-text",
            after=hold.get("reason", "unknown"),
        )
        return False

    def sweep(self) -> None:
        if self.dry_run:
            return
        for path in self.store.deferred.glob("goal-nudge-*"):
            if not (self.store.wake / path.with_suffix(".marker").name).exists():
                path.unlink(missing_ok=True)
        for directory in (self.store.claimed, self.store.expired):
            for path in directory.glob("goal-nudge-*.marker"):
                try:
                    if self.now - path.stat().st_mtime > _SWEEP_AFTER_SECONDS:
                        path.unlink()
                except OSError:
                    continue

    # -- one session

    def session(self, session: WakerSession, variant: str) -> bool:
        if goal_loop_halt.is_halted(session.session_id):
            self.emit("GW-SKIP", session=session.label, reason="goal-loop-halted")
            return False
        return self.active_session(session, variant)

    def active_session(self, session: WakerSession, variant: str) -> bool:  # noqa: C901 - goal lifecycle and held-demand cancellation
        from ai_chassis import compact_rearm  # noqa: PLC0415 - recovery shares this timer

        if compact_rearm.recover(self, session):
            return True
        goal = goal_state(session.transcript, self.config, session.name)
        if not self.dry_run and goal.kind in {
            NO_GOAL,
            ENDED,
            ACTIVE_OTHER,
            EVALUATOR_LOOP,
        }:
            from ai_chassis.quota_optional import admission

            admission.complete("goal-wake", session.session_id, "active-goal")
        paused_goal = (
            notice_fields(session.transcript)
            .get("notice", "")
            .startswith("Goal paused")
        )
        routed = self.loop_route(session, goal, paused=paused_goal)
        if routed is not None:
            return routed
        # D-40: a session idle at or above the idle save-state threshold is told to hold its
        # outputs and save state, whatever its goal; the stay-busy nudge would only fill the
        # window further. ctx-pct runs only for a session already measured idle.
        settings = supervisor.idle_save_state_settings()
        after = settings["idle_save_state_after_seconds"]
        threshold_pct = settings["idle_save_state_pct"]
        # Save-state reading includes informational: a finished notice can need a save.
        early = idle_state(
            session.transcript,
            self.now,
            dict(self.config, idle_after_seconds=after),
            extra_turn_end_subtypes=frozenset({"informational"}),
        )
        context_pct = (
            context_reading(session)
            if early.kind == IDLE and (not goal.rejected_terminal or paused_goal)
            else None
        )
        saving = context_pct is not None and (
            context_pct >= threshold_pct
            or self.store.compact_requested(session, self.now)
        )
        if saving:
            idle = early
            decision = decide_save_state(
                session, idle, self.store, self.config, after, now=self.now
            )
        else:
            # Goal-nudge reading keeps four subtypes so notices cannot resume a goal.
            idle = (
                idle_state(session.transcript, self.now, self.config)
                if goal.kind == ACTIVE_QUALIFYING
                else IdleState(UNDETERMINED, reason="not-measured")
            )
            decision = decide(
                session, goal, idle, self.store, self.config, now=self.now
            )
        if decision.action == "ISSUE" and not self.automatic_wakes(session.label):
            decision = _skip("automatic-wakes-off", decision.state)
        state = decision.state
        if decision.action == "ISSUE" and saving:
            state = self.issue_save_state(
                session, idle, decision, (context_pct, threshold_pct)
            )
        elif decision.action == "ISSUE":
            state = self.issue(session, goal, idle, decision, variant)
        elif saving:
            self.emit(
                decision.event,
                session=session.label,
                reason=decision.reason,
                context_pct=context_pct,
                threshold_pct=threshold_pct,
                **notice_fields(session.transcript),
            )
        else:
            self.report(session, goal, decision)
        self.decisions.append(
            {
                "session": session.label,
                "session_id": session.session_id,
                "action": decision.action,
                "event": decision.event,
                "reason": decision.reason,
                "goal": goal.kind,
                "goal_source": goal.source,
                "idle": idle.kind,
                "idle_seconds": idle.idle_seconds,
                "threshold": decision.threshold,
                "context_pct": context_pct,
                "idle_save_state_pct": threshold_pct,
            }
        )
        if not self.dry_run and state != self.store.session_state(session.session_id):
            self.store.save_state(session.session_id, state)
        if saving:
            return True
        return goal.kind == ACTIVE_QUALIFYING and idle.kind == IDLE

    # -- the evaluator-loop clear (ISSUE-REF; D-4 as amended)

    def loop_route(
        self, session: WakerSession, goal: GoalState, *, paused: bool
    ) -> bool | None:
        """The loop clear's own handling of this tick, or None for the wakes."""
        if goal.kind == EVALUATOR_LOOP and not paused:
            # Before the save-state wake too: any wake restarts the loop (ISSUE-REF).
            return self.evaluator_loop(session, goal)
        if self.late_clear(session, goal):
            return False
        return None

    def evaluator_loop(self, session: WakerSession, goal: GoalState) -> bool:
        """Classify and log the loop every tick; clear it only when the gates allow."""
        # Goal-nudge reading keeps four subtypes to preserve evaluator-loop gates.
        idle = idle_state(session.transcript, self.now, self.config)
        decision = decide_loop(
            session, goal, idle, self.store, self.config, now=self.now
        )
        # The clear obeys loop_clear_enabled alone, never the idle-wake automatic_wakes
        # switch: it ends a loop rather than waking a session (ISSUE-REF).
        self.emit(
            "GW-LOOP",
            session=session.label,
            rejections=goal.rejections,
            goal=goal.goal_label,
            action=decision.action,
            reason=decision.reason,
            last_reason=goal.last_reason,
            **notice_fields(session.transcript),
        )
        state = decision.state
        if decision.action == "CLEAR":
            state = self.clear(session, goal, state)
        self.decisions.append(
            {
                "session": session.label,
                "session_id": session.session_id,
                "action": decision.action,
                "event": decision.event,
                "reason": decision.reason,
                "goal": goal.kind,
                "goal_source": goal.source,
                "idle": idle.kind,
                "idle_seconds": idle.idle_seconds,
                "threshold": decision.threshold,
                "rejections": goal.rejections,
                "last_reason": goal.last_reason,
            }
        )
        if not self.dry_run and state != self.store.session_state(session.session_id):
            self.store.save_state(session.session_id, state)
        return idle.kind == IDLE

    def _follow_up(
        self, goal: GoalState, max_chars: int, *, cleared: bool = True
    ) -> str:
        return render_loop_cleared_nudge(
            goal_label=goal.goal_label,
            rejections=goal.rejections,
            last_reason=goal.last_reason,
            max_chars=max_chars,
            cleared=cleared,
        )

    def clear(
        self, session: WakerSession, goal: GoalState, state: dict[str, Any]
    ) -> dict[str, Any]:
        """``/goal clear`` through the library, then one follow-up only if it landed.

        A typed clear whose landing went unseen keeps its goal, so a later tick that reads
        the goal cleared can still send the follow-up (:meth:`late_clear`)."""
        cleared = self._clear(session, goal, state)
        unseen = (
            cleared.get("last_clear_failed_at") == self.now
            and cleared.get("last_clear_failure") in _UNSEEN_CLEAR_FAILURES
            and cleared.get("last_clear_staged")
        )
        failed_goal = (
            {
                "epoch": goal.epoch,
                "rejections": goal.rejections,
                "last_reason": goal.last_reason,
                "goal_label": goal.goal_label,
                "run": cleared.get("last_clear_run"),
                "notice": cleared.get("last_clear_notice"),
            }
            if unseen
            else None
        )
        if self.dry_run:
            return cleared
        return dict(cleared, last_clear_failed_goal=failed_goal)

    def late_clear(self, session: WakerSession, goal: GoalState) -> bool:
        """A clear this waker typed whose landing check saw nothing, on the goal it was
        for, now reads cleared: count it landed, withdraw its notice, send the follow-up."""
        if goal.kind != ENDED:
            return False
        state = self.store.session_state(session.session_id)
        failed = state.get("last_clear_failed_goal")
        if (
            not isinstance(failed, dict)
            or session.name is None
            or goal.reason != "goal-status-cleared"
            or goal.epoch is None
            or not isinstance(failed.get("epoch"), int)
            or goal.epoch <= failed["epoch"]
        ):
            return False
        try:
            size = session.transcript.stat().st_size
            floor = max(0, size - int(self.config["transcript_scan_bytes"]))
            armed = _cleared_goal_armed_at(session.transcript, floor)
        except (OSError, ValueError):
            return False
        if armed != failed["epoch"]:
            return False
        name = str(session.name)
        fields = {"session": name, "rejections": failed.get("rejections", 0)}
        self.emit(
            "GW-CLEAR",
            **fields,
            step="clear",
            state="LANDED-LATE",
            run=failed.get("run"),
            confirmed="goal-status",
        )
        if self.dry_run:
            return True
        self.store.release(name)
        self._withdraw_notice(failed.get("notice"), failed.get("run"))
        self.store.save_state(
            session.session_id,
            dict(
                state,
                last_clear_at=self.now,
                last_clear_epoch=failed["epoch"],
                last_clear_failed_at=None,
                last_clear_failure=None,
                last_clear_staged=False,
                last_clear_failed_goal=None,
                clear_quick_failures=0,
                last_skip_reason=None,
            ),
        )
        loop = GoalState(
            EVALUATOR_LOOP,
            failed["epoch"],
            rejections=int(failed.get("rejections") or 0),
            last_reason=str(failed.get("last_reason") or ""),
            goal_label=str(failed.get("goal_label") or ""),
        )
        self.follow_up(name, loop, fields)
        return True

    @staticmethod
    def _withdraw_notice(notice: object, run: object) -> None:
        """Remove the clear's "did not land" notice if it is still that run's."""
        if not notice or not run:
            return
        path = Path(str(notice))
        try:
            if f"run_id={run}" in path.read_text(encoding="utf-8"):
                path.unlink()
        except OSError:
            return

    def _clear(
        self, session: WakerSession, goal: GoalState, state: dict[str, Any]
    ) -> dict[str, Any]:
        name = str(session.name)
        fields = {"session": name, "rejections": goal.rejections}
        if self.dry_run:
            self.emit("GW-CLEAR", **fields, step="clear", state="dry-run")
            return state
        # The notice is not typed, so it carries the follow-up's full instruction. A model
        # cannot run the built-in /goal clear, so the notice asks for the user (ISSUE-REF).
        notice = (
            f"The goal waker's /goal clear for this session did not land, and the native "
            f"/goal evaluator is still rejecting your declared terminal "
            f"({goal.rejections} times). /goal clear is a built-in command a model cannot "
            f"run. This is the goal state as of {_utc_stamp(self.now)}; it goes stale. "
            f"Before you ask the user to type /goal clear, run start_goal.py status and ask "
            f"only if it still shows the goal armed; a goal that reads cleared or absent "
            f"needs no ask, and an ask already made is not repeated. The waker also tries "
            f"again. "
            f"The investigation is owed now either way: "
            + self._follow_up(goal, _NOTICE_MAX_CHARS, cleared=False)
        )
        try:
            result = self.fallback.clear_goal(name, notice, session.transcript)
        except (FallbackUnavailable, FallbackRefused) as error:
            return self.clear_refused(session, state, fields, error)
        except Exception as error:  # noqa: BLE001 - any library failure is a refusal here
            # Whether it typed before failing is unknown, so it is held like a staged one.
            self.store.hold(name, "library-error", self.now)
            cause = error.error if isinstance(error, ClearRaised) else error
            return self.clear_failed(
                state,
                fields,
                f"exception:{type(cause).__name__}",
                detail=cause,
                notice=getattr(error, "notice", None),
            )
        if not result.ok:
            if result.staged:
                self.store.hold(name, result.reason or result.state, self.now)
            return self.clear_failed(
                state,
                fields,
                result.reason or result.state.lower(),
                result=result,
            )
        confirmed = {"confirmed": "goal-status"} if result.confirmed else {}
        self.emit(
            "GW-CLEAR",
            **fields,
            step="clear",
            state=result.state,
            run=result.run_id,
            **confirmed,
        )
        cleared = dict(
            state,
            last_clear_at=self.now,
            last_clear_epoch=goal.epoch,
            last_clear_failed_at=None,
            last_clear_failure=None,
            last_clear_staged=False,
            clear_quick_failures=0,
            last_skip_reason=None,
        )
        self.follow_up(name, goal, fields)
        return cleared

    def clear_refused(
        self,
        session: WakerSession,
        state: dict[str, Any],
        fields: dict[str, Any],
        error: FallbackUnavailable | FallbackRefused,
    ) -> dict[str, Any]:
        if (
            isinstance(error, FallbackRefused)
            and error.reason == "target-unresolved"
            and getattr(error.__cause__, "reason", None) == "no-pane"
        ):
            return self.halt_no_pane(session, state)
        reason = getattr(error, "reason", "library-unavailable")
        return self.clear_failed(state, fields, reason, detail=error)

    def halt_no_pane(
        self, session: WakerSession, state: dict[str, Any]
    ) -> dict[str, Any]:
        """Stop continuation for this id; this does not clear the native goal.

        Resolution may have taken time: re-read the live thresholds and transcript
        before writing, retaining every existing loop-clear eligibility gate.
        """
        config = load_config(live_config_path())
        goal = goal_state(session.transcript, config, session.name)
        # The idle reading is taken fresh after resolution, so the halt reuses the same
        # gates the clear path applies and never acts on a stale reading.
        idle = idle_state(session.transcript, self.now, config)
        if (
            not config["enabled"]
            or goal.kind != EVALUATOR_LOOP
            or decide_loop(session, goal, idle, self.store, config, now=self.now).action
            != "CLEAR"
        ):
            return state
        # A block-cap loop qualifies at its own count; no config key holds a threshold.
        threshold = (
            goal.rejections
            if goal.loop_threshold_key == BLOCK_CAP_THRESHOLD_KEY
            else config[goal.loop_threshold_key]
        )
        goal_loop_halt.record_halt(
            session.session_id,
            epoch=goal.epoch,
            threshold_key=goal.loop_threshold_key,
            threshold=threshold,
            rejections=goal.rejections,
            halted_at=self.now,
        )
        self.emit(
            "GW-HALTED",
            session=session.label,
            session_id=session.session_id,
            threshold_key=goal.loop_threshold_key,
            threshold=threshold,
            rejections=goal.rejections,
            reason="no-pane",
            native_goal="armed",
        )
        return state

    def clear_failed(  # noqa: PLR0913 - the failure's whole record
        self,
        state: dict[str, Any],
        fields: dict[str, Any],
        reason: str,
        *,
        result: InjectionResult | None = None,
        detail: object = None,
        notice: Path | None = None,
    ) -> dict[str, Any]:
        """Report a clear that did not land; the back-off, not a retry, decides the next."""
        extra: dict[str, Any] = {}
        if result is not None:
            extra.update(state=result.state, run=result.run_id, notice=result.notice)
        if notice is not None:
            extra["notice"] = notice
        if detail is not None:
            extra["detail"] = detail
        self.emit("GW-CLEAR-FAIL", **fields, step="clear", reason=reason, **extra)
        staged = bool(result is not None and result.staged)
        quick = reason in _UNTYPED_CLEAR_REFUSALS and not staged
        return dict(
            state,
            last_clear_run=extra.get("run"),
            last_clear_notice=str(extra["notice"]) if extra.get("notice") else None,
            last_clear_failed_at=self.now,
            last_clear_failure=reason,
            last_clear_staged=staged,
            clear_quick_failures=(
                int(state.get("clear_quick_failures") or 0) + 1 if quick else 0
            ),
            last_skip_reason="clear-failed",
        )

    def follow_up(self, name: str, goal: GoalState, fields: dict[str, Any]) -> None:
        """The billing-aware ``/delegate`` instruction after a landed clear."""
        try:
            text = self._follow_up(goal, self.fallback.typed_limit())
            result = self.fallback.loop_nudge(name, text)
        except NudgeTooLong as error:
            self.emit(
                "GW-CLEAR-FAIL",
                **fields,
                step="follow-up",
                reason="nudge-too-long",
                detail=error,
            )
            return
        except FallbackRefused as error:
            self.emit("GW-CLEAR-FAIL", **fields, step="follow-up", reason=error.reason)
            return
        except Exception as error:  # noqa: BLE001 - the clear stands; the miss is logged
            self.emit(
                "GW-CLEAR-FAIL",
                **fields,
                step="follow-up",
                reason="library-error",
                detail=error,
            )
            return
        if not result.ok:
            if result.staged:
                self.store.hold(name, result.reason or result.state, self.now)
            self.emit(
                "GW-CLEAR-FAIL",
                **fields,
                step="follow-up",
                reason=result.reason or result.state.lower(),
                state=result.state,
                run=result.run_id,
                notice=result.notice,
            )
            if (
                result.reason in _FOLLOW_UP_TOO_TALL
                and not result.staged
                and result.notice is not None
            ):
                self.pointer(name, goal, fields, text)
            return
        self.emit(
            "GW-CLEAR",
            **fields,
            step="follow-up",
            state=result.state,
            run=result.run_id,
            chars=len(text),
        )

    def pointer(
        self, name: str, goal: GoalState, fields: dict[str, Any], full: str
    ) -> None:
        """A follow-up too tall for the composer is already the session's notice; type a
        line that fits, which the ``UserPromptSubmit`` hook delivers it with (example-session,
        2026-10-15). A failure here leaves ``full`` as the notice, never the pointer."""
        text = render_loop_cleared_pointer(rejections=goal.rejections)
        try:
            result = self.fallback.loop_nudge(name, text, notice_text=full)
        except FallbackRefused as error:
            self.emit("GW-CLEAR-FAIL", **fields, step="pointer", reason=error.reason)
            return
        except Exception as error:  # noqa: BLE001 - the clear stands; the miss is logged
            self.emit(
                "GW-CLEAR-FAIL",
                **fields,
                step="pointer",
                reason="library-error",
                detail=error,
            )
            return
        if not result.ok:
            if result.staged:
                self.store.hold(name, result.reason or result.state, self.now)
            self.emit(
                "GW-CLEAR-FAIL",
                **fields,
                step="pointer",
                reason=result.reason or result.state.lower(),
                state=result.state,
                run=result.run_id,
                notice=result.notice,
            )
            return
        self.emit(
            "GW-CLEAR",
            **fields,
            step="pointer",
            state=result.state,
            run=result.run_id,
            chars=len(text),
        )

    def issue_save_state(
        self,
        session: WakerSession,
        idle: IdleState,
        decision: Decision,
        readings: tuple[int, int],
    ) -> dict[str, Any]:
        """Publish the idle save-state directive (D-40) through the goal nudge's marker."""
        if not self.automatic_wakes(session.label):
            return decision.state
        idle_seconds = int(idle.idle_seconds or 0)
        context_pct, threshold_pct = readings
        notice = notice_fields(session.transcript)
        paused_goal = notice.get("notice", "").startswith("Goal paused")
        try:
            text = render_save_state_nudge(
                context_pct=context_pct,
                threshold_pct=threshold_pct,
                idle_minutes=idle_seconds // 60,
                max_chars=self.config["max_nudge_chars"],
                compact=context_pct < threshold_pct,
                paused_goal=paused_goal,
            )
        except NudgeTooLong as error:
            self.emit(
                "GW-SKIP",
                session=session.label,
                reason="nudge-too-long",
                detail=error,
                **notice,
            )
            return dict(decision.state, last_skip_reason="nudge-too-long")
        nonce = "dry-run"
        count = decision.nudge_index + 1
        charged = dict(
            decision.state,
            save_state_wakes=count,
            last_nudge_at=self.now,
            last_skip_reason=None,
        )
        if not self.dry_run:
            marker = self.publish_event_marker(
                session,
                text,
                decision.state,
                charged,
                epoch_offset=None,
                idle_minutes=idle_seconds // 60,
                now=self.now,
                save_state=readings,
                paused_goal=paused_goal,
                **(
                    {"nonce": text_digest(self.operation_key)[:16]}
                    if self.operation_key
                    else {}
                ),
            )
            nonce = _MARKER_NAME.match(marker.name)["nonce"]
        self.emit(
            "GW-ISSUE",
            session=session.label,
            nudge=nonce,
            idle=idle_seconds,
            n=count,
            reason="idle-save-state",
            context_pct=context_pct,
            threshold_pct=threshold_pct,
            **notice,
        )
        return dict(
            charged,
            last_nudge_id=nonce,
        )

    def publish_event_marker(self, session, text, before, charged, **fields):
        """Write a recoverable budget intent before exposing an event's marker."""
        if self.operation_key:
            nonce = text_digest(self.operation_key)[:16]
            charged = dict(charged, last_nudge_id=nonce)
            self.store.save_state(
                session.session_id,
                dict(
                    before,
                    event_publication={"key": self.operation_key, "state": charged},
                ),
            )
        return issue(session, text, self.store.root, **fields)

    def report(
        self, session: WakerSession, goal: GoalState, decision: Decision
    ) -> None:
        if decision.event == "GW-SKIP":
            extra = (
                {"condition": goal.reason}
                if goal.kind == ACTIVE_OTHER
                else {"detail": goal.reason}
                if goal.kind == ENDED
                else {}
            )
            self.emit(
                "GW-SKIP",
                session=session.label,
                reason=decision.reason,
                **extra,
                **notice_fields(session.transcript),
            )
        else:
            self.emit(
                decision.event,
                session=session.label,
                epoch=goal.epoch,
                **notice_fields(session.transcript),
            )

    def issue(
        self,
        session: WakerSession,
        goal: GoalState,
        idle: IdleState,
        decision: Decision,
        variant: str,
    ) -> dict[str, Any]:
        if not self.automatic_wakes(session.label):
            return decision.state
        from ai_chassis.quota_optional import admission

        # The native goal owns one outstanding nudge demand. Replacing/ending the
        # goal cancels that demand; an old epoch must not block its replacement.
        item = "active-goal"
        if not admission.check("goal-wake", session.session_id, item):
            self.emit("GW-SUPPRESSED", session=session.label, reason="quota-held")
            return decision.state
        idle_seconds = int(idle.idle_seconds or 0)
        try:
            text = render_nudge(
                variant,
                idle_minutes=idle_seconds // 60,
                max_chars=self.config["max_nudge_chars"],
                rider=goal.rider,
            )
        except NudgeTooLong as error:
            self.emit(
                "GW-SKIP", session=session.label, reason="nudge-too-long", detail=error
            )
            return dict(decision.state, last_skip_reason="nudge-too-long")
        nonce = "dry-run"
        count = decision.nudge_index + 1
        charged = dict(
            decision.state,
            nudges_in_epoch=count,
            last_nudge_at=self.now,
            last_skip_reason=None,
        )
        if not self.dry_run:
            marker = self.publish_event_marker(
                session,
                text,
                decision.state,
                charged,
                epoch_offset=goal.epoch,
                idle_minutes=idle_seconds // 60,
                now=self.now,
                rider=goal.rider,
                **(
                    {"nonce": text_digest(self.operation_key)[:16]}
                    if self.operation_key
                    else {}
                ),
            )
            nonce = _MARKER_NAME.match(marker.name)["nonce"]
        self.emit(
            "GW-ISSUE", session=session.label, nudge=nonce, idle=idle_seconds, n=count
        )
        if not self.dry_run:
            admission.complete("goal-wake", session.session_id, item)
        return dict(
            charged,
            last_nudge_id=nonce,
        )


def marker_admitted(marker: Path) -> bool:
    """Recheck a harness marker before either typed or hook rename-claim.

    Save-state and compact recovery are safety saves, even at CRITICAL.
    """
    from ai_chassis.quota_optional import admission

    fields, _ = read_marker(marker)
    match = _MARKER_NAME.match(marker.name)
    if match is None:
        return False
    allowed = admission.check(
        "goal-marker",
        match["sid"],
        marker.name,
        safety_save=fields.get("kind") in {SAVE_STATE_KIND, "compact-rearm"},
    )
    if not allowed:
        # A pre-claim hook may be the first reader to observe the hold. The timer
        # reoffers this same inode when admission returns, without charging again.
        deferred = marker.parent.parent / "goal-nudge-deferred"
        if deferred.is_dir():
            (deferred / marker.name).touch(mode=0o600, exist_ok=True)
    return allowed


def marker_completed(marker: Path) -> None:
    """Retire a quota reference after a marker's consumer owns it."""
    from ai_chassis.quota_optional import admission

    match = _MARKER_NAME.match(marker.name)
    if match:
        admission.complete("goal-marker", match["sid"], marker.name)


def evaluate_session(  # noqa: PLR0913 - bound session and event evaluation callbacks
    session: WakerSession,
    *,
    now: float,
    save_state: bool = False,
    clear=None,
    recover=None,
    operation_key: str | None = None,
    goal_reader=None,
) -> dict[str, Any]:
    """Evaluate one bound session without inventory, sweeps or typed readiness waits."""
    config = load_config(live_config_path())
    run = _Tick(config, dry_run=False, now=now)
    run.operation_key = operation_key
    run.store = WakerStore(
        supervisor._root_from_environment(),
        state_dir(),
        session.name or session.session_id,
    )
    if not config["enabled"] or goal_loop_halt.is_halted(session.session_id):
        return run.result()
    run.store.prepare()
    _reconcile_event_publication(run, session)
    if operation_key and operation_key in run.store.session_state(
        session.session_id
    ).get("applied_events", []):
        return run.result()
    if recover is not None and recover(run):
        return run.result()
    goal = (
        goal_reader(config)
        if goal_reader is not None
        else goal_state(session.transcript, config, session.name)
    )
    settings = supervisor.idle_save_state_settings()
    # The event reading includes informational completions only for save-state;
    # ordinary goal eligibility keeps the four native turn-end subtypes.
    idle = idle_state(
        session.transcript,
        now,
        config,
        extra_turn_end_subtypes=frozenset({"informational"})
        if save_state
        else frozenset(),
    )
    if goal.kind == EVALUATOR_LOOP and not notice_fields(session.transcript).get(
        "notice", ""
    ).startswith("Goal paused"):
        return _evaluate_event_loop(run, session, goal, idle, clear)
    context = context_reading(session) if save_state else None
    if save_state:
        if context is None or (
            context < settings["idle_save_state_pct"]
            and not run.store.compact_requested(session, now)
        ):
            return run.result()
        # Save-state reading includes informational notice completions.
        idle = idle_state(
            session.transcript,
            now,
            dict(config, idle_after_seconds=settings["idle_save_state_after_seconds"]),
            extra_turn_end_subtypes=frozenset({"informational"}),
        )
        decision = _decide_save_state_due(
            run, session, goal, idle, settings["idle_save_state_after_seconds"]
        )
    else:
        decision = decide(session, goal, idle, run.store, config, now=now)
    if decision.action == "ISSUE" and not run.automatic_wakes(session.label):
        decision = _skip("automatic-wakes-off", decision.state)
    state = decision.state
    if decision.action == "ISSUE" and save_state:
        state = run.issue_save_state(
            session, idle, decision, (context, settings["idle_save_state_pct"])
        )
    elif decision.action == "ISSUE":
        variant = nudge_variant_for_host(
            _machine_identity(), config["profile_variants"]
        )
        state = run.issue(session, goal, idle, decision, variant)
    else:
        run.report(session, goal, decision)
    _save_event_evaluation(run, session, state)
    run.decisions.append(
        {
            "session_id": session.session_id,
            "action": decision.action,
            "reason": decision.reason,
            "threshold": decision.threshold,
        }
    )
    return run.result()


def _decide_save_state_due(run, session, goal, idle, after_seconds):
    state = run.store.session_state(session.session_id)
    if idle.kind != IDLE:
        return _skip(_IDLE_GATES[idle.kind], state)
    paused = (
        notice_fields(session.transcript).get("notice", "").startswith("Goal paused")
    )
    if goal.rejected_terminal and not paused:
        return _skip(_GOAL_GATES[goal.kind], state)
    return decide_save_state(
        session, idle, run.store, run.config, after_seconds, now=run.now
    )


def _evaluate_event_loop(run, session, goal, idle, clear):
    decision = decide_loop(session, goal, idle, run.store, run.config, now=run.now)
    run.emit(
        "GW-LOOP",
        session=session.label,
        action=decision.action,
        reason=decision.reason,
        rejections=goal.rejections,
        goal=goal.goal_label,
        last_reason=goal.last_reason,
    )
    state = decision.state
    if decision.action == "CLEAR":
        # The callback reserves an evidence-driven command, never a synchronous
        # library readiness wait. Missing notification support remains a hold.
        if clear is not None:
            state = clear(run, goal, state)
        else:
            run.emit(
                "GW-CLEAR-HELD",
                session=session.label,
                reason="pane-notifications-unavailable",
            )
    _save_event_evaluation(run, session, state)
    run.decisions.append(
        {
            "session_id": session.session_id,
            "action": decision.action,
            "reason": decision.reason,
            "threshold": decision.threshold,
        }
    )
    return run.result()


def _save_event_evaluation(run, session, state):
    if run.operation_key:
        state = {
            **state,
            "applied_events": [*state.get("applied_events", []), run.operation_key],
        }
    run.store.save_state(session.session_id, state)


def _reconcile_event_publication(run, session):
    state = run.store.session_state(session.session_id)
    intent = state.get("event_publication")
    if not intent:
        return
    charged = intent["state"]
    name = f"goal-nudge-{session.session_id}-{charged['last_nudge_id']}.marker"
    if any(
        (directory / name).exists()
        for directory in (run.store.wake, run.store.claimed, run.store.expired)
    ):
        charged = dict(
            charged, applied_events=[*charged.get("applied_events", []), intent["key"]]
        )
        run.store.save_state(session.session_id, charged)
    else:
        # Publication never happened. Re-evaluate fresh safety/eligibility before
        # creating a marker; a new human turn may have invalidated the intent.
        run.store.save_state(
            session.session_id,
            {key: value for key, value in state.items() if key != "event_publication"},
        )


def tick(*, dry_run: bool = False, now: float | None = None) -> dict[str, Any]:
    """One detection pass over every live session, the update-restart pass, the
    session-reload pass, then the indicators projector, which reads what the passes
    before it wrote."""
    try:
        from ai_chassis.prompt_injection import capability
    except ImportError:
        # Library absence is reported by each optional pass below. With no
        # library there can be no probe, so ordinary detection still runs.
        budget = contextlib.nullcontext()
    else:
        budget = capability.probe_budget(60)

    # Both phases can reach the capability gate. Share a 60s budget inside the
    # unchanged 90s service timeout, including the probe's inter-process lock.
    with budget:
        moment = time.time() if now is None else now
        undos = _pending_undo_pass(dry_run=dry_run)
        report = _goal_tick(dry_run=dry_run, now=moment)
        report["pending_undo"] = undos
        report["update_restart"] = _update_restart_pass(dry_run=dry_run, now=moment)
        report["session_reload"] = _session_reload_pass(
            dry_run=dry_run, now=moment, update_restart=report["update_restart"]
        )
        report["indicators"] = _indicators_pass(dry_run=dry_run, now=moment)
        return report


def _pending_undo_pass(*, dry_run: bool) -> dict[str, Any]:
    """ISSUE-REF's deferred undos ride this timer. Like the update-restart pass, a failure
    in it (including an absent prompt-injection library) never fails the goal waker's
    tick, and is reported in the tick's result instead (ISSUE-REF)."""
    try:
        from ai_chassis.prompt_injection.repair import retry_pending_undos

        return retry_pending_undos(dry_run=dry_run)
    except Exception as error:  # noqa: BLE001 - isolation is the point
        return {
            "pending": 0,
            "cleared": 0,
            "escalated": 0,
            "error": f"{type(error).__name__}: {error}",
        }


def _update_restart_pass(*, dry_run: bool, now: float) -> dict[str, Any]:
    """ISSUE-REF rides this timer. Its own switch and log; a failure in it never fails
    the goal waker's tick, and is reported in the tick's result instead."""
    try:
        from ai_chassis import update_restart  # noqa: PLC0415 - imports this module

        return update_restart.tick(dry_run=dry_run, now=now)
    except Exception as error:  # noqa: BLE001 - isolation is the point
        return {
            "events": [],
            "restarted": 0,
            "error": f"{type(error).__name__}: {error}",
        }


def _session_reload_pass(
    *, dry_run: bool, now: float, update_restart: dict[str, Any]
) -> dict[str, Any]:
    """ISSUE-REF rides this timer after the update restart, which it reads to skip a
    session restarted this tick. Like the other passes, a failure in it never fails the
    goal waker's tick, and is reported in the tick's result instead."""
    try:
        from ai_chassis import session_reload  # noqa: PLC0415 - imports this module

        return session_reload.tick(
            dry_run=dry_run, now=now, update_restart=update_restart
        )
    except Exception as error:  # noqa: BLE001 - isolation is the point
        return {
            "events": [],
            "injected": 0,
            "error": f"{type(error).__name__}: {error}",
        }


def _indicators_pass(*, dry_run: bool, now: float) -> dict[str, Any]:
    """ISSUE-REF's projector rides this timer, last, after every pass it reads. It bounds
    its own adapters by `pass_budget_seconds`; a failure in it never fails the tick, and is
    reported in the tick's result instead. A dry run projects without writing."""
    try:
        from ai_chassis.indicators import projector  # noqa: PLC0415 - imports this module

        return projector.run_pass(now=now, write=not dry_run)
    except Exception as error:  # noqa: BLE001 - isolation is the point
        return {"error": f"{type(error).__name__}: {error}"}


def _goal_tick(*, dry_run: bool, now: float) -> dict[str, Any]:
    config = load_config(live_config_path())
    run = _Tick(config, dry_run=dry_run, now=now)
    if not config["enabled"]:
        run.emit("GW-DISABLED", reason=config["_error"] or "enabled-false")
        return run.result()
    if not dry_run:
        run.store.prepare()
    sessions = discover_sessions(run.emit)
    if sessions is None:
        run.emit("GW-SKIP", session="*", reason="inventory-undetermined")
        return run.result()
    variant = nudge_variant_for_host(_machine_identity(), config["profile_variants"])
    run.markers({s.session_id: s for s in sessions}, variant)
    candidates = sum(run.session(session, variant) for session in sessions)
    for session in sessions:
        try:
            to_review_reminder.maybe_issue(run, session)
        except Exception as error:  # noqa: BLE001 - the reminder never fails the tick
            run.emit(
                "GW-TO-REVIEW-ERROR",
                session=session.label,
                reason=f"{type(error).__name__}: {error}",
            )
    run.sweep()
    issued = sum(1 for d in run.decisions if d["action"] == "ISSUE")
    run.emit("GW-TICK", sessions=len(sessions), candidates=candidates, issued=issued)
    return run.result()


def status() -> dict[str, Any]:
    config = load_config(live_config_path())
    policy = idle_wake.load_policy()
    store = WakerStore(supervisor._root_from_environment(), state_dir())
    pending = sorted(p.name for p in store.wake.glob("goal-nudge-*.marker"))
    sessions = {}
    for path in sorted((store.state_root / "sessions").glob("*.json")):
        sessions[path.stem] = store.session_state(path.stem)
    return {
        "enabled": config["enabled"],
        "config_error": config["_error"],
        "config_source": config["_source"],
        "automatic_wakes": policy.automatic_wakes,
        "wake_policy_reason": policy.reason,
        "automatic_save_state_protection": config["enabled"] and policy.automatic_wakes,
        "resume_root": str(store.root),
        "state_dir": str(store.state_root),
        "pending_markers": pending,
        "sessions": sessions,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ai_chassis.goal_waker",
        description="Wake idle sessions whose work-all-in-progress /goal is active.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    tick_parser = commands.add_parser("tick", help="run one detection pass")
    tick_parser.add_argument(
        "--once", action="store_true", help="one pass (a tick is always one pass)"
    )
    tick_parser.add_argument(
        "--dry-run", action="store_true", help="print decisions; write no marker"
    )
    tick_parser.add_argument("--json", action="store_true")
    status_parser = commands.add_parser("status", help="config, pending markers, state")
    status_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = tick(dry_run=args.dry_run) if args.command == "tick" else status()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    elif args.command == "tick":
        update = report["update_restart"]
        reload = report["session_reload"]
        lines = report["events"] + update["events"] + reload["events"]
        if update.get("error"):
            lines.append(f"UR-ERROR {update['error']}")
        if reload.get("error"):
            lines.append(f"SR-ERROR {reload['error']}")
        if report["indicators"].get("error"):
            lines.append(f"IND-ERROR {report['indicators']['error']}")
        print("\n".join(lines))
    else:
        print("\n".join(f"{key}: {value}" for key, value in report.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
