"""Fail-closed Claude Code compaction and continuation supervisor.

The terminal transport is deliberately only trusted to report whether tmux accepted
the fenced command.  Hook acknowledgements, persisted here in the resume store,
are the sole success oracle.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
import traceback
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ai_chassis.session_key import refusal as _session_key_refusal
from ai_chassis.tmux_compat import (
    TMUX_FINGERPRINT_FORMAT as TMUX_FINGERPRINT_FORMAT,
    fingerprint_format,
    send_keys_command,
)

try:  # POSIX: Linux, macOS
    import fcntl as _fcntl

    _msvcrt = None
except ImportError:  # Windows
    _fcntl = None
    import msvcrt as _msvcrt


def _lock_exclusive(fd: int) -> None:
    """Take an exclusive advisory lock on ``fd``, on either platform's primitive.

    Probing the module rather than branching on ``sys.platform``, matching
    ``scripts/bd_common.py``: CPython ships ``fcntl`` on POSIX and ``msvcrt`` on
    Windows and neither imports on the other, so the unguarded ``import fcntl``
    this replaces was an ImportError at module load -- which made this module and
    every test importing it uncollectable on Windows, so pytest could not
    enumerate the suite at all.

    ``msvcrt`` locks a byte range from the descriptor's current offset rather than
    the whole file. ``locked()`` has just opened the lock file, so that offset is
    0, and locking one byte there is the conventional whole-file equivalent.
    """
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
    else:
        _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)


def _try_lock_exclusive(fd: int) -> bool:
    """Take an exclusive lock without blocking; False when another holder has it.

    Measured on macOS 27 with CPython: ``flock`` conflicts between two
    descriptors *in the same process*, unlike POSIX ``fcntl`` record locks, which
    are per-process and would silently grant the second request.  That is why
    :func:`poller_health` can probe a holder it shares a process with, and why
    this must stay on the same primitive as :func:`_lock_exclusive`.
    """
    if _fcntl is not None:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            return False
        return True
    try:
        _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
    else:
        _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)


# Whether this platform can hand out a file descriptor for a directory, which is
# what fsyncing a directory requires. See ``_fsync_directory`` for why this is an
# ``os.name`` gate rather than a capability probe.
_DIRECTORY_DESCRIPTORS_EXIST = os.name != "nt"


COMPACT_STATES = {
    "ARMED",
    "BOUND",
    "READY",
    "SENT",
    "STARTED",
    "COMPLETED",
    "AMBIGUOUS",
    "QUEUED",
    "STAGED_UNVERIFIED",
    "CORRUPTED",
    "SESSION_ENDED",
    "REFUSED",
}
CONTINUATION_STATES = {
    "CONTINUATION_SENT",
    "CONTINUATION_CONFIRMED",
    "CONTINUATION_AMBIGUOUS",
}
TERMINAL_STATES = {
    "COMPLETED",
    "AMBIGUOUS",
    "CORRUPTED",
    "SESSION_ENDED",
    "REFUSED",
    "CONTINUATION_CONFIRMED",
    "CONTINUATION_AMBIGUOUS",
}
FENCE_MISMATCH = "__aih_context_fence_mismatch__"
# Why a compact that completed cleanly still carries no deliverable wake (ISSUE-REF).
# Both strings below are halves of one disclosure -- the record's and the operator's --
# kept adjacent so neither can drift into silence alone.
#
# THE REASON string belongs to the NOTIFICATION legs. `wake_state: READY` is a
# claimable status and `claim_wake` is reachable only from those legs, so with the
# notification gate shut there is no claimant and READY described a wake that was in
# fact abandoned by policy. It is now default-ON (ISSUE-REF), so this reason is
# reached only where an operator deliberately turned the notification wake off.
POST_COMPACT_WAKE_DISABLED_REASON = "post-compact-notification-wake-not-enabled"
# THE NOTICE string belongs to the TYPED leg, which stays default-OFF. Its previous
# text ("nothing will submit the resume turn for you ... submit the injected resume
# message yourself") was wrong in both halves and cost an operator real time: the
# SessionStart banner delivers the resume artifact regardless of either gate, and the
# post-compact turn was MEASURED self-starting from the compaction summary and acting
# on that banner with nobody typing anything (2026-10-07). So a disabled typed wake
# is a statement about one redundant transport, not a manual step anyone owes.
POST_COMPACT_WAKE_DISABLED_NOTICE = (
    "NOTE: the typed post-compact wake is off by design (ISSUE-REF, reaffirmed "
    "2026-10-07) -- it is a redundant transport, not a missing one. The SessionStart "
    "banner delivers your resume message after the next /compact and the post-compact "
    "turn acts on it unprompted, so there is NO manual step here. To exercise the "
    "typed leg for one attempt only, launch the lifecycle with "
    "AIH_SKILL_POST_COMPACT_TYPED_WAKE=1 (or --enable-typed-wake); the fleet default "
    "stays off."
)
# The oldest tmux whose SERVER expands TMUX_FINGERPRINT_FORMAT into a shape this
# transport can use.  A floor, not a set of blessed builds (example-user, 2026-09-06:
# "do't pin on any particular tmux version for our code ... at least a version
# floor.  we can set that version floor for code to 3.7 and if newer versions
# break anythign then we can fix them and set hte new floor to that new version").
#
# 3.7 is the floor because the `W/i:` window-iteration specifier is what divides
# the two shapes.  Measured 2026-09-06 (ISSUE-REF) on one format string, one
# pane, isolated `-L` sockets:
#
#     3.7c -> '$0||0|@0[%0=3228827=0;]'          (W/i: INTERPRETED)
#     3.4  -> '$0|<gen>|1|W/i:@0[%0=3150204=0;]' (W/i: emitted VERBATIM)
#
# WHY A FLOOR AND NOT AN ALLOWLIST.  An exact-version set was the defect, not the
# guard: it recurred four times across two subsystems, it locked every host whose
# build was not a frozen member out of the send path entirely, and its documented
# escape hatch (AIH_INJECT_TESTED_TMUX_VERSIONS) could only ever NARROW the set.
# A floor admits a newer build sight unseen, and that is the deliberate trade --
# a future release that changes the format is a bug to fix and a floor to raise
# when it is observed, not a reason to pre-refuse every build nobody has enrolled
# by hand.  Raise MINIMUM_TMUX_VERSION; do not reintroduce a membership test.
MINIMUM_TMUX_VERSION = (3, 7)
# How long a supervisor status-line warning stays up; tmux's own default is 750ms.
PANE_WARNING_DISPLAY_MS = 15000
_TMUX_VERSION_RE = re.compile(r"^(?:next-)?(\d+)\.(\d+)")
_DEFAULT_BAND_THRESHOLDS = {
    "gentle": 50,
    "recommend": 55,
    "urge": 60,
    "urgent": 65,
    # Raised 70 -> 80 on example-user's instruction, 2026-09-24 ("can you bump it to 80 ... i
    # don't want or need it to be that strict"). Only this band moved: the four below are
    # advisory notices that block nothing, and widening the 65 -> 80 gap gives URGENT more
    # runway before the hard deny, which is the point.
    #
    # WHAT THIS NUMBER IS NOT: a position relative to Claude Code's own auto-compact. A
    # comment here used to assert "Claude Code's own auto-compact is reported at ~83%, so a
    # session now has ~3 points between this deny and native compaction rather than ~13".
    # DELETED 2026-09-25 (ISSUE-REF): that figure was never measured, and two other
    # unmeasured figures for the same threshold were in circulation at the same time (~92-95%
    # asserted on the issue, ~96.7% implied by Claude Code's docs for a 1M model). The defect
    # was not the wrong number -- it was positioning a HARD DENY relative to a number nobody
    # measured, and then reasoning about "3 points of runway" from it.
    #
    # WHAT IS DOCUMENTED (ISSUE-REF; code.claude.com/docs/en/model-config.md § Context
    # window and auto-compaction, settings-reference.md `autoCompactWindow` and
    # `modelSettings`, env-vars.md; all fetched 2026-10-15). The window resolves as
    # CLAUDE_CODE_AUTO_COMPACT_WINDOW > the `--autocompact` launch flag > the window
    # `/autocompact` saves per model under `modelSettings` (v2.1.288+) > the top-level
    # `autoCompactWindow`, and is capped at the model's context window. With none set,
    # models with a native 1M window (Fable, Sonnet 5+, Haiku 5.5, Opus 4.7+) "compact before
    # the window fills, at about 967K tokens by default", and other models compact at their
    # context limit. CLAUDE_AUTOCOMPACT_PCT_OVERRIDE only lowers the trigger, as a percentage
    # of that window.
    #
    # WHAT IS INFERRED, not documented: that the derived window is the trigger. The documented
    # default is itself "about" 967K, the client keeps a summary buffer whose size is not
    # published, and its own schema documents `enforced: false` when the worker defers to the
    # API's prompt-too-long. So a boundary derived from the documented configuration is an
    # UPPER BOUND on where native compaction fires, never the exact point.
    #
    # WHERE THE RELATIONSHIP IS RESOLVED: `.claude/bin/ctx-pct` derives the boundary in that
    # precedence, down to the documented default, and bands on distance to it, reporting
    # `band_basis: auto_compact_boundary` plus `auto_compact_window_source`. On that basis
    # this number means "% of the way to the boundary", not "% of the window". Only a session
    # whose model id is unknown keeps the `window` basis and an UNKNOWN position, and the
    # deny is in any case never the session's only route to compaction: see
    # :func:`_transportless_release_checkpoint`.
    "force": 80,
}
_DEFAULT_HISTORY_RECENCY_SECONDS = 300
# The grace window bounds INACTIVITY, not total elapsed time: force_gate_output re-stamps
# the marker on every permitted call, so a save-state that is still working keeps it alive
# while an abandoned one still expires.
_DEFAULT_SAVE_STATE_ACTIVE_TIMEOUT_SECONDS = 300
# How recently a resume artifact must have been published for it to count as THIS episode's
# checkpoint when the FORCE deny is released on a transport-less session
# (:func:`_transportless_release_checkpoint`). It is deliberately a freshness bound and not a
# one-shot latch: a latch would release the gate once and never re-arm, and the session would
# then ride an hours-old resume message into a native compaction, losing the continuity the
# artifact exists to carry. 30 minutes is a judgement, not a measurement -- it is long enough
# that a session is not re-checkpointing every few tool calls, and short enough that what the
# release lets through is bounded work rather than a whole unrecorded session. Both errors it
# can make are small and self-correcting: too short costs one extra /save-state, too long
# ships a resume message up to this many minutes behind the session.
_TRANSPORTLESS_CHECKPOINT_TTL_SECONDS = 1800

# Claude Code's own machine-readable code for a turn it could not run because the
# cloud credential was dead. Detection keys on this rather than on the human text
# beside it ("API Error: Could not load AWS credentials ..."), because that text is
# routinely QUOTED by agent briefs and tool results that are not aborted turns.
# Measured shape and counts: `_is_credential_abort`.
CREDENTIAL_ABORT_ERROR = "cloud_credential_error"

# Permitted at FORCE even with no grace marker armed, so save-state can always record state
# and reach its own continuation. Reads and mirrors only; nothing here can publish.
#
# WIDENED 2026-09-24 on example-user's instruction ("make it less restrictive wrt to tool calling
# etc ... we should still be able to do stuff like create and update beads tasks etc").
# THE INVARIANT THAT DID NOT MOVE, and the reason the widening is safe: nothing added may
# hide an arbitrary command inside a permitted one, and nothing added may PUBLISH -- no
# push, no PR, no comment, nothing sent to an external service. Cost was never why this is
# narrow. Anything borderline is left out and recommended instead, not added on a guess.
_FORCE_GATE_ALWAYS_ALLOWED_TOOLS = frozenset(
    {
        # The CC panel is a display mirror of Beads. Creating and updating a task is the
        # capability example-user named explicitly, and it was already permitted.
        "TaskList",
        "TaskGet",
        "TaskCreate",
        "TaskUpdate",
        # Read-only, cannot mutate anything and cannot send anywhere. A session that cannot
        # read cannot compose an accurate resume message, which is the artifact this gate
        # exists to protect: a delegated Opus leaf was frozen out of `Read` entirely on
        # 2026-09-24 and returned unable to measure anything at all.
        "Read",
        "Grep",
        "Glob",
    }
)
_FORCE_GATE_ALLOWED_EXECUTABLES = frozenset(
    {
        "save-resume",  # publishes the resume artifact; the continuation path itself
        "bd",  # the task store of record
        # The fleet wrapper that is how issues are ACTUALLY filed here, because `bd create`
        # is guard-blocked fleet-wide. Without it a session at FORCE cannot file an issue at
        # all and has to leave a [BD-PENDING] placeholder in its own hand-off -- measured in
        # session ISSUE-REF this week. Same store and same blast radius as `bd`, which was
        # already permitted, so this closes an inconsistency rather than widening reach.
        "bd-new",
        # Reads a cached statusline snapshot and prints a percentage. The fleet requires a
        # save-state to QUOTE its ctx-pct reading, so denying the gate's own instrument made
        # the instruction it prints unfollowable.
        "ctx-pct",
    }
)
_FORCE_GATE_ALLOWED_COMMAND_PREFIXES = frozenset(
    {
        ("git", "commit"),  # cannot publish; a commit is local
        # Read-only inspection. A resume message is required to state branch, uncommitted
        # changes and recent commits (the fleet's compact instructions name all three), so
        # these are the minimum needed to describe git state truthfully rather than from
        # memory. Neither can mutate a tree or reach a remote.
        ("git", "status"),
        ("git", "log"),
    }
)
# Shell parsing lives in the shared library, `ai_chassis.prompt_injection.self_rescue`
# (ISSUE-REF D-1), together with the self-rescue rules the gate consults FIRST: the
# compact-lifecycle and poller-health launches, ctx-pct, save-resume and Skill(save-state)
# are never denied at any band, because denying them denies the compaction itself; and
# `held_cli.py add|list|show|validate|stores` (`self-rescue:held-cli`, ISSUE-REF D-6), so
# an output is held at emit time at FORCE too. Its `ship`, `answer` and `retire` stay out.
# The same rule admits `format_decision_check.py`, the check `add --kind decision` needs.
#
# The test is QUOTE-AWARE. It was a raw substring scan, so a `;` inside a QUOTED argument
# refused too (session ISSUE-REF, 2026-09-24: `save-resume --resume-command '<text with ; >'`
# refused three times). It now LEXES: separators are inert inside either quote, `$` and a
# backtick only inside single quotes (bash expands "$(cat x)"), and a newline or CR is
# refused rawly because shlex reads it as whitespace. A chain joined by `&&`, `;` or `||`
# is permitted iff every segment is (D-3: the fleet requires `ctx-pct && ctx-pct --advice`
# and the gate denied it on 2026-10-10); pipes, redirections, subshells, a lone `&` and
# any live expansion still refuse, so `bd update x -d "$(cat /tmp/body.md)"` stays denied.
_DEFAULT_COMPACT_CORRELATION_WINDOW_SECONDS = 30
_DEFAULT_IDLE_PROMPT_RECENCY_SECONDS = 120
_DEFAULT_CONTINUATION_READY_TIMEOUT_SECONDS = 3.0
_DEFAULT_CONTINUATION_READY_POLL_SECONDS = 0.25
# A real ceiling, distinct from the shipped default. These were the same number,
# which made the default refuse every larger configured value (ISSUE-REF).
_MAX_CONTINUATION_READY_TIMEOUT_SECONDS = 600.0
_DEFAULT_REBOOT_RESUME_STAMP_TTL_SECONDS = 7 * 24 * 60 * 60
# How long the typed leg waits for the SessionStart banner turn to ack its packet
# before concluding no turn is coming. A judgement, not a measurement: long enough
# that an ordinary post-compact turn (which also runs /auto-start) finishes inside
# it, short enough that a genuinely dead session is not held all day. Both errors
# are small -- too short risks one duplicate prompt, too long delays a keystroke
# nothing is waiting on.
_DEFAULT_BANNER_TURN_WAIT_SECONDS = 180.0
_DEFAULT_BANNER_TURN_POLL_SECONDS = 0.5
_MAX_BANNER_TURN_WAIT_SECONDS = 3600.0
# How many times a QUEUED continuation's confirmation wait is extended before the
# attempt is allowed to reach a terminal state. Each extension doubles the window,
# so the default spends 30s + 60s + 120s + 240s before giving up rather than the
# single 30s that abandoned a prompt the composer had merely not reached yet.
_DEFAULT_CONTINUATION_QUEUED_RETRIES = 3
_DEFAULT_COMPACT_STAGE_VERIFY_TIMEOUT_SECONDS = 0.5
_DEFAULT_COMPACT_STAGE_VERIFY_POLL_SECONDS = 0.05
_SGR_SEQUENCE = re.compile(r"\x1b\[[0-9;:]*m")
# Measured from production captures across Claude Code sessions (C-5): staged
# suggestions render with either dim or foreground color 153.
_SUGGESTION_SGR_SEQUENCES: frozenset[str] = frozenset({"\x1b[2m", "\x1b[38;5;153m"})
_SAFE_STAGED_ATTRIBUTE_CLASSES = frozenset({"normal", "suggestion"})
_DEFAULT_TRANSCRIPT_SIGNAL_TIMEOUT_SECONDS = 5.0
_DEFAULT_TRANSCRIPT_SIGNAL_POLL_SECONDS = 0.1
_DEFAULT_TURN_QUIET_SECONDS = 15
_DEFAULT_NO_COMPOSER_ESCALATE_SECONDS = 120
_DEFAULT_NO_COMPOSER_STALL_SECONDS = 900
# ISSUE-REF: Claude Code clips its composer off a window shorter than its bottom stack,
# so a hidden composer on a short window gets a bounded, temporary grow of the window.
_DEFAULT_NO_COMPOSER_GROW_AFTER_SECONDS = 60
_DEFAULT_NO_COMPOSER_GROW_ROWS = 50
_DEFAULT_NO_COMPOSER_GROW_MAX_ATTEMPTS = 3
_COMPOSER_GROW_SETTLE_SECONDS = 1.0
# ISSUE-REF: a bare ai-cli-utils pty session is sent `/compact` through its submit
# FIFO. The pause between the text and its Enter, and how far after the session's last
# submitted prompt its terminal may still show input before a draft is presumed.
_DEFAULT_FIFO_ENTER_DELAY_SECONDS = 0.5
_DEFAULT_FIFO_INPUT_TOLERANCE_SECONDS = 1.0
_DEFAULT_LANDING_TIMEOUT_SECONDS = 5.0
_DEFAULT_RESTAGE_BACKOFF_CEILING_SECONDS = 300
_DEFAULT_LONG_WAIT_ESCALATE_SECONDS = 7200
# ISSUE-REF: how long a compact-guard presence record stays fresh. The mod's 5 s tick
# refreshes it, so an idle session stays fresh.
_DEFAULT_PRESENCE_TTL_SECONDS = 900
_MAX_HOOK_EXCEPTION_DIAGNOSTIC_BYTES = 16 * 1024
_READINESS_TRAIL_MAX_BYTES = 1024 * 1024
_READINESS_TRAIL_RETENTION_SECONDS = 7 * 24 * 60 * 60
_READINESS_HOUSEKEEPING_INTERVAL_SECONDS = 24 * 60 * 60
_READINESS_FALLBACK_REGION_LINES = 20
_MAX_WRAPPED_COMPOSER_ROWS = 40
_GOAL_TRANSCRIPT_INITIAL_SCAN_BYTES = 256 * 1024
_CLAUDE_CODE_NOTICE_PATTERNS = (
    (
        "update-installed-restart-required",
        re.compile(
            r"update installed\s*(?:[·.\-–—]\s*)?restart to update", re.IGNORECASE
        ),
    ),
    (
        "auto-update-failed",
        re.compile(
            r"auto-update failed\s*(?:[·.\-–—]\s*)?run claude doctor", re.IGNORECASE
        ),
    ),
)
# No Claude Code version allowlist: the causal transcript signal runs on EVERY build
# (example-user, 2026-09-06). An exact-version set gated a capability it only proxied for --
# the very next check parses the AIH-COMPACT-READY sentinel and fails closed on its own,
# so the pin added no safety and silently disabled auto-compact on every upgrade. Twice
# measured: 2.1.259 (ISSUE-REF, 106 fallbacks / 0 binds) and 2.1.263 four days later
# (276 fallbacks / 0 binds across all seven live sessions). A future build that changes
# the transcript shape is a bug to fix when observed, not a reason to pre-refuse.
_COMPOSER_TOP_BORDER = re.compile(r"^\s*─+(?:\s+\S(?:.*\S)?\s+─+)?\s*$")
_DEFAULT_COMPOSER_CURSOR_PATTERN = r"❯"
_PERMISSION_QUESTION = re.compile(r"^\s*(?:│\s*)?Do you want to .+\?\s*$")
_PERMISSION_SELECTED_OPTION = re.compile(r"^\s*(?:│\s*)?❯\s*\d+\.\s+\S")
_BUDGET_DISARM_REASONS = frozenset({"wake-budget", "compact-budget"})
# ISSUE-REF: `unpaired-history` and `postcompact-order` both fire AFTER a
# `/compact` was already sent, so the attempt is already charged against the
# budget. Clearing their latch without also starting a fresh budget epoch would
# leave `permitted()` to re-disarm the key immediately on the very next call --
# a fix that reads as applied and is inert. They belong here for the same reason
# `completion-ack-timeout` and `prompt-mismatch` already do.
_SAVE_STATE_BUDGET_RESET_DISARM_REASONS = _BUDGET_DISARM_REASONS | {
    # A submitted compact that never started (ISSUE-REF), the twin of a started
    # one that never completed: neither may outlive an explicit /save-state.
    "start-ack-timeout",
    "completion-ack-timeout",
    "prompt-mismatch",
    "unpaired-history",
    "postcompact-order",
}
_SAVE_STATE_RESETTABLE_DISARM_REASONS = _SAVE_STATE_BUDGET_RESET_DISARM_REASONS | {
    "causal-correlation-mismatch",
}
# How long a disarm latch stays authoritative. Measured 2026-09-15: six session
# keys were latched on 2026-09-08 and every one still refused to send `/compact`
# seven days later, because nothing ever read the stamp `disarm()` writes.
#
# Six hours is chosen so a genuine runaway stays suppressed well beyond the point
# a human would notice it, while a session starting the next working day arrives
# armed. The per-type budget counters, not this latch, are the real bound on a
# send loop; the latch only has to outlive the episode that set it.
DISARM_MAX_AGE_SECONDS = 6 * 60 * 60
_readiness_housekeeping_last_run: float | None = None


def _now() -> int:
    return int(time.time())


def _is_box_drawing_horizontal_rule(line: str) -> bool:
    """Recognize an untitled composer rule without tying it to one glyph."""
    rule = line.strip()
    return bool(rule) and all(
        unicodedata.name(character, "").startswith("BOX DRAWINGS")
        and "HORIZONTAL" in unicodedata.name(character, "")
        for character in rule
    )


def _is_composer_top_border(line: str) -> bool:
    """Accept the current titled rule or a future all-horizontal box rule."""
    return bool(
        _COMPOSER_TOP_BORDER.fullmatch(line)
    ) or _is_box_drawing_horizontal_rule(line)


def _matches_composer_cursor(line: str) -> bool:
    """Match the configurable idle cursor, defaulting to Claude Code's current glyph."""
    try:
        return bool(
            re.fullmatch(
                os.environ.get(
                    "AIH_COMPOSER_CURSOR_PATTERN", _DEFAULT_COMPOSER_CURSOR_PATTERN
                ),
                line.strip(),
            )
        )
    except re.error:
        return False


def _is_empty_composer_content(lines: list[str], index: int) -> bool:
    """Recognize the cursor line without confusing trailing pane whitespace for it."""
    if _matches_composer_cursor(lines[index]):
        return True
    return (
        not lines[index].strip()
        and index + 1 < len(lines)
        and _is_composer_top_border(lines[index + 1])
    )


def digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:16]


# Shared marker-only watch directory; the bash cwd re-arm uses this documented name.
WAKE_DIRECTORY_NAME = "wake"


def _root_from_environment() -> Path:
    return Path(os.environ.get("AIH_RESUME_ROOT", Path.home() / ".claude" / "resume"))


def wake_directory(root: Path, key: str | None = None) -> Path:
    from ai_chassis.wake import sessions

    if key is not None and sessions.event_layout(sessions.mail_root(), key):
        return sessions.directory(root, key) / WAKE_DIRECTORY_NAME
    return root / WAKE_DIRECTORY_NAME


def _config_path() -> Path:
    return Path(
        os.environ.get("AIH_CONTEXT_SUPERVISOR_CONFIG_PATH")
        or Path.home() / ".claude" / "state" / "context-supervisor-config.json"
    )


#: The legacy injectors that can be switched onto the prompt-injection library
#: (design ISSUE-REF D-8). Every switch ships false; with a switch false or absent
#: the library is never imported.
LIBRARY_CUTOVER_SWITCHES = ("compact", "continuation", "reauth_wake")
_DEFAULT_LIBRARY_FALLBACK_AFTER_SECONDS = 8
_library_cutover_invalid_logged: set[str] = set()
#: Keys a live file provisioned before ISSUE-REF may still carry. Each is ignored and
#: logged once per process; it is NOT a malformed key, so it resets nothing.
#: ``reboot_resume_typed_wake_enabled`` became ``launch_resume.typed_fallback_enabled``,
#: ``reboot_resume_stamp_ttl_seconds`` became ``launch_resume.stamp_ttl_seconds``, and
#: ``library_cutover.reboot_wake`` went with the reboot wake it switched.
RETIRED_KEYS = frozenset(
    {"reboot_resume_typed_wake_enabled", "reboot_resume_stamp_ttl_seconds"}
)
RETIRED_LIBRARY_CUTOVER_SWITCHES = frozenset({"reboot_wake"})
_retired_logged: set[str] = set()


def _log_retired(name: str) -> None:
    if name not in _retired_logged:
        _retired_logged.add(name)
        log_stage("CC-SUPERVISOR-CONFIG-RETIRED-KEY", "supervisor", f"key={name}")


def _library_cutover_defaults() -> dict[str, Any]:
    return {
        "library_cutover": dict.fromkeys(LIBRARY_CUTOVER_SWITCHES, False),
        "library_fallback_after_seconds": _DEFAULT_LIBRARY_FALLBACK_AFTER_SECONDS,
    }


def _library_cutover_settings(config: dict[str, Any]) -> dict[str, Any]:
    """The cutover switches from a parsed live file, failing closed per value.

    An absent object or key is ``False``. A malformed object, an unknown key or a
    non-boolean value turns EVERY switch off and logs ``CC-LIB-CUTOVER-INVALID``
    once per process per reason; a bad fallback budget keeps the default.
    """
    settings = _library_cutover_defaults()
    switches = config.get("library_cutover", {})
    reason = None
    if not isinstance(switches, dict):
        reason = "library_cutover-not-an-object"
    else:
        for name in sorted(set(switches) & RETIRED_LIBRARY_CUTOVER_SWITCHES):
            _log_retired(f"library_cutover.{name}")
        switches = {
            name: value
            for name, value in switches.items()
            if name not in RETIRED_LIBRARY_CUTOVER_SWITCHES
        }
        unknown = sorted(set(switches) - set(LIBRARY_CUTOVER_SWITCHES))
        bad = sorted(k for k, v in switches.items() if not isinstance(v, bool))
        if unknown:
            reason = f"library_cutover-unknown-key:{unknown[0]}"
        elif bad:
            reason = f"library_cutover-not-boolean:{bad[0]}"
        else:
            settings["library_cutover"].update(switches)
    budget = config.get(
        "library_fallback_after_seconds", _DEFAULT_LIBRARY_FALLBACK_AFTER_SECONDS
    )
    if (
        isinstance(budget, (int, float))
        and not isinstance(budget, bool)
        and 0 < budget <= 60
    ):
        settings["library_fallback_after_seconds"] = budget
    elif reason is None:
        reason = "library_fallback_after_seconds-invalid"
    if reason is not None and reason not in _library_cutover_invalid_logged:
        _library_cutover_invalid_logged.add(reason)
        log_stage("CC-LIB-CUTOVER-INVALID", "supervisor", f"reason={reason}")
    return settings


def _library_cutover(use_case: str) -> bool:
    """Whether ``use_case``'s legacy call site runs the library first. Read at call time."""
    return _config()["library_cutover"][use_case] is True


#: The ``launch_resume`` object (ISSUE-REF D-5): one unit with its own error path.
LAUNCH_RESUME_MODES = ("never", "reboot_only", "always")
LAUNCH_RESUME_SCENARIOS = (
    "host_reboot",
    "terminal_quit_or_crash",
    "manual_exit",
    "fresh_launch",
)
#: ``launch_resume.relaunch`` shipped with Phase 2 and moved to ai-cli-utils' iterm2.toml
#: (design, Configuration): a live object still carrying it is ignored and logged once,
#: never read as a malformed key that would switch launch-resume off.
RETIRED_LAUNCH_RESUME_KEYS = frozenset({"relaunch"})
#: key -> (minimum, maximum, integer only)
_LAUNCH_RESUME_NUMBERS: dict[str, tuple[float, float, bool]] = {
    "first_fire_delay_seconds": (0, 3600, False),
    "rearm_after_clear_seconds": (0, 3600, False),
    "max_wait_seconds": (1, 86400, False),
    "sample_interval_seconds": (0.05, 60, False),
    "stamp_ttl_seconds": (3600, 31536000, True),
}


class LaunchResumeConfigError(ValueError):
    """The live ``launch_resume`` object is malformed; ``reason`` names the first bad key."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _launch_resume_defaults() -> dict[str, Any]:
    return {
        "mode": "always",
        "scenarios": dict.fromkeys(LAUNCH_RESUME_SCENARIOS, True),
        "first_fire_delay_seconds": 15,
        "rearm_after_clear_seconds": 30,
        "max_wait_seconds": 900,
        "sample_interval_seconds": 1.0,
        "typed_fallback_enabled": True,
        "stamp_ttl_seconds": _DEFAULT_REBOOT_RESUME_STAMP_TTL_SECONDS,
        "beads_ui_enabled": False,
    }


def _launch_resume_value_error(name: str, value: Any, default: Any) -> str | None:
    if name == "mode":
        return None if value in LAUNCH_RESUME_MODES else "invalid-value"
    if isinstance(default, bool):
        return None if isinstance(value, bool) else "not-boolean"
    low, high, integer = _LAUNCH_RESUME_NUMBERS[name]
    kinds = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds):
        return "not-a-number"
    return None if low <= value <= high else "out-of-range"


def _launch_resume_error(
    live: dict[str, Any], defaults: dict[str, Any], path: str
) -> str | None:
    """The first malformed key under ``path``; absent keys keep their defaults."""
    for name in sorted(live):
        where = f"{path}.{name}"
        if name not in defaults:
            return f"{where}-unknown-key"
        value, default = live[name], defaults[name]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                return f"{where}-not-an-object"
            nested = _launch_resume_error(value, default, where)
            if nested is not None:
                return nested
            continue
        problem = _launch_resume_value_error(name, value, default)
        if problem is not None:
            return f"{where}-{problem}"
    return None


def _merged(defaults: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    merged = dict(defaults)
    for name, value in live.items():
        default = defaults[name]
        merged[name] = _merged(default, value) if isinstance(default, dict) else value
    return merged


def _launch_resume_settings(config: dict[str, Any]) -> dict[str, Any]:
    """``launch_resume`` and ``launch_resume_error`` for ``_config()``.

    All or nothing WITHIN the object: one malformed key leaves the defaults in place and
    names the reason, which :func:`launch_resume_config` raises, so a typo such as
    ``"mode": "nevr"`` is reported and skipped rather than read as ``always``. It never
    resets a key outside the object, and is computed even when another key is malformed,
    so a profile's ``mode: never`` survives an unrelated bad value.
    """
    defaults = _launch_resume_defaults()
    if "launch_resume" not in config:
        return {"launch_resume": defaults, "launch_resume_error": None}
    live = config["launch_resume"]
    if not isinstance(live, dict):
        reason = "launch_resume-not-an-object"
    else:
        for name in sorted(set(live) & RETIRED_LAUNCH_RESUME_KEYS):
            _log_retired(f"launch_resume.{name}")
        live = {k: v for k, v in live.items() if k not in RETIRED_LAUNCH_RESUME_KEYS}
        reason = _launch_resume_error(live, defaults, "launch_resume")
    if reason is not None:
        return {"launch_resume": defaults, "launch_resume_error": reason}
    return {"launch_resume": _merged(defaults, live), "launch_resume_error": None}


def launch_resume_config() -> dict[str, Any]:
    """The live ``launch_resume`` object, read at call time.

    Raises :class:`LaunchResumeConfigError` when the object is malformed; every caller
    treats that as "do not arm" and logs ``CC-LAUNCH-RESUME-SKIP reason=config-error``.
    """
    config = _config()
    if config["launch_resume_error"] is not None:
        raise LaunchResumeConfigError(config["launch_resume_error"])
    return _merged(_launch_resume_defaults(), config["launch_resume"])


def _bounded_config_int(value: object, low: int, high: int) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
    )


def _bounded_config_number(value: object, low: float, high: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and low <= value <= high
    )


#: How long `ctx-pct` waits for its caller's transcript record, in seconds. The same number is
#: the built-in fallback in `.claude/bin/ctx-pct`, so a host whose operator file lacks the key
#: gets it too.
_DEFAULT_CTX_PCT_CALL_WAIT_SECONDS = 5


def _config() -> dict[str, Any]:
    """Read the live supervisor settings, failing open to safe defaults."""
    defaults: dict[str, Any] = {
        "ctx_pct_call_wait_seconds": _DEFAULT_CTX_PCT_CALL_WAIT_SECONDS,
        "injection_enabled": True,
        "post_compact_typed_wake_enabled": False,
        "post_compact_notification_wake_enabled": True,
        "enroll_all_sessions": True,
        "enrolled_keys": [],
        "band_thresholds": _DEFAULT_BAND_THRESHOLDS,
        "history_recency_seconds": _DEFAULT_HISTORY_RECENCY_SECONDS,
        "debug_diagnostics_enabled": False,
        "turn_quiet_seconds": _DEFAULT_TURN_QUIET_SECONDS,
        "no_composer_escalate_seconds": _DEFAULT_NO_COMPOSER_ESCALATE_SECONDS,
        "no_composer_stall_seconds": _DEFAULT_NO_COMPOSER_STALL_SECONDS,
        "no_composer_grow_after_seconds": _DEFAULT_NO_COMPOSER_GROW_AFTER_SECONDS,
        "no_composer_grow_rows": _DEFAULT_NO_COMPOSER_GROW_ROWS,
        "no_composer_grow_max_attempts": _DEFAULT_NO_COMPOSER_GROW_MAX_ATTEMPTS,
        "fifo_enter_delay_seconds": _DEFAULT_FIFO_ENTER_DELAY_SECONDS,
        "fifo_input_tolerance_seconds": _DEFAULT_FIFO_INPUT_TOLERANCE_SECONDS,
        "landing_timeout_seconds": _DEFAULT_LANDING_TIMEOUT_SECONDS,
        "restage_backoff_ceiling_seconds": (_DEFAULT_RESTAGE_BACKOFF_CEILING_SECONDS),
        "long_wait_escalate_seconds": _DEFAULT_LONG_WAIT_ESCALATE_SECONDS,
        "presence_ttl_seconds": _DEFAULT_PRESENCE_TTL_SECONDS,
        "reboot_resume_enabled": True,
        **_library_cutover_defaults(),
        **_launch_resume_settings({}),
    }
    # A MISSING file means the shipped defaults. A file that exists but cannot be read as
    # a JSON object still gets the safe defaults everywhere else, but must not read as
    # `launch_resume.mode = always`: a host that turned launch-resume off and then broke
    # its file would otherwise start typing into panes. Flagged as the launch_resume
    # error, so every launch-resume caller skips with reason=config-error.
    try:
        config = json.loads(_config_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return defaults
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {**defaults, "launch_resume_error": "config-file-unreadable"}
    if not isinstance(config, dict):
        return {**defaults, "launch_resume_error": "config-file-not-an-object"}
    for name in sorted(set(config) & RETIRED_KEYS):
        _log_retired(name)
    # Validated on their own, never as part of the check below: a bad cutover
    # value must fail closed to the legacy path for that switch alone, not reset
    # every other key to the hard-coded defaults (which would turn a host's
    # `injection_enabled: false` back on).
    cutover = _library_cutover_settings(config)
    launch_resume = _launch_resume_settings(config)
    call_wait = config.get(
        "ctx_pct_call_wait_seconds", _DEFAULT_CTX_PCT_CALL_WAIT_SECONDS
    )
    if not _bounded_config_number(call_wait, 0, 10):
        call_wait = _DEFAULT_CTX_PCT_CALL_WAIT_SECONDS

    # The declared default, so a valid file that omits the key agrees with a
    # missing file instead of silently disabling injection (ISSUE-REF).
    enabled = config.get("injection_enabled", defaults["injection_enabled"])
    # The two wake gates read their own shipped default rather than a shared `False`,
    # because the live file on every host provisioned before the split carries NEITHER
    # key -- so this per-key fallback, not the `defaults` dict above, is what actually
    # governs there (measured on example-project-ec2, 2026-10-07). The notification default must
    # therefore be True in BOTH places or an unmigrated host silently keeps the old
    # all-legs-off behaviour.
    post_compact_typed_wake_enabled = config.get(
        "post_compact_typed_wake_enabled", False
    )
    post_compact_notification_wake_enabled = config.get(
        "post_compact_notification_wake_enabled", True
    )
    enroll_all_sessions = config.get("enroll_all_sessions", True)
    keys = config.get("enrolled_keys", [])
    thresholds = config.get("band_thresholds", _DEFAULT_BAND_THRESHOLDS)
    history_recency_seconds = config.get(
        "history_recency_seconds", _DEFAULT_HISTORY_RECENCY_SECONDS
    )
    debug_diagnostics_enabled = config.get("debug_diagnostics_enabled", False)
    turn_quiet_seconds = config.get("turn_quiet_seconds", _DEFAULT_TURN_QUIET_SECONDS)
    no_composer_escalate_seconds = config.get(
        "no_composer_escalate_seconds", _DEFAULT_NO_COMPOSER_ESCALATE_SECONDS
    )
    no_composer_stall_seconds = config.get(
        "no_composer_stall_seconds", _DEFAULT_NO_COMPOSER_STALL_SECONDS
    )
    no_composer_grow_after_seconds = config.get(
        "no_composer_grow_after_seconds", _DEFAULT_NO_COMPOSER_GROW_AFTER_SECONDS
    )
    no_composer_grow_rows = config.get(
        "no_composer_grow_rows", _DEFAULT_NO_COMPOSER_GROW_ROWS
    )
    no_composer_grow_max_attempts = config.get(
        "no_composer_grow_max_attempts", _DEFAULT_NO_COMPOSER_GROW_MAX_ATTEMPTS
    )
    fifo_enter_delay_seconds = config.get(
        "fifo_enter_delay_seconds", _DEFAULT_FIFO_ENTER_DELAY_SECONDS
    )
    fifo_input_tolerance_seconds = config.get(
        "fifo_input_tolerance_seconds", _DEFAULT_FIFO_INPUT_TOLERANCE_SECONDS
    )
    landing_timeout_seconds = config.get(
        "landing_timeout_seconds", _DEFAULT_LANDING_TIMEOUT_SECONDS
    )
    restage_backoff_ceiling_seconds = config.get(
        "restage_backoff_ceiling_seconds", _DEFAULT_RESTAGE_BACKOFF_CEILING_SECONDS
    )
    long_wait_escalate_seconds = config.get(
        "long_wait_escalate_seconds", _DEFAULT_LONG_WAIT_ESCALATE_SECONDS
    )
    presence_ttl_seconds = config.get(
        "presence_ttl_seconds", _DEFAULT_PRESENCE_TTL_SECONDS
    )
    # Per-key fallbacks equal to the shipped defaults, for the same reason as the
    # wake gates above: a live file provisioned before these keys carries none of them.
    reboot_resume_enabled = config.get("reboot_resume_enabled", True)
    threshold_values = (
        [thresholds.get(name) for name in _DEFAULT_BAND_THRESHOLDS]
        if isinstance(thresholds, dict)
        else []
    )
    if (
        not isinstance(enabled, bool)
        or not isinstance(post_compact_typed_wake_enabled, bool)
        or not isinstance(post_compact_notification_wake_enabled, bool)
        or not isinstance(enroll_all_sessions, bool)
        or not isinstance(keys, list)
        or not all(isinstance(key, str) for key in keys)
        or not isinstance(thresholds, dict)
        or set(thresholds) != set(_DEFAULT_BAND_THRESHOLDS)
        or not all(
            isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100
            for value in threshold_values
        )
        or threshold_values != sorted(threshold_values)
        or not isinstance(history_recency_seconds, int)
        or isinstance(history_recency_seconds, bool)
        or not 0 <= history_recency_seconds <= 3600
        or not isinstance(debug_diagnostics_enabled, bool)
        or not isinstance(turn_quiet_seconds, int)
        or isinstance(turn_quiet_seconds, bool)
        or not 5 <= turn_quiet_seconds <= 3600
        or not isinstance(no_composer_escalate_seconds, int)
        or isinstance(no_composer_escalate_seconds, bool)
        or not 0 <= no_composer_escalate_seconds <= 3600
        or not isinstance(no_composer_stall_seconds, int)
        or isinstance(no_composer_stall_seconds, bool)
        or not 60 <= no_composer_stall_seconds <= 86400
        or not _bounded_config_int(no_composer_grow_after_seconds, 0, 3600)
        or not _bounded_config_int(no_composer_grow_rows, 20, 200)
        or not _bounded_config_int(no_composer_grow_max_attempts, 1, 20)
        or not _bounded_config_number(fifo_enter_delay_seconds, 0, 5)
        or not _bounded_config_number(fifo_input_tolerance_seconds, 0, 10)
        or not isinstance(landing_timeout_seconds, (int, float))
        or isinstance(landing_timeout_seconds, bool)
        or not 0 <= landing_timeout_seconds <= 30
        or not isinstance(restage_backoff_ceiling_seconds, int)
        or isinstance(restage_backoff_ceiling_seconds, bool)
        or not 60 <= restage_backoff_ceiling_seconds <= 3600
        or not isinstance(long_wait_escalate_seconds, int)
        or isinstance(long_wait_escalate_seconds, bool)
        or not 1800 <= long_wait_escalate_seconds <= 86400
        or not _bounded_config_int(presence_ttl_seconds, 60, 3600)
        or not isinstance(reboot_resume_enabled, bool)
    ):
        return {**defaults, **launch_resume, "ctx_pct_call_wait_seconds": call_wait}
    return {
        **cutover,
        **launch_resume,
        "ctx_pct_call_wait_seconds": call_wait,
        "injection_enabled": enabled,
        "post_compact_typed_wake_enabled": post_compact_typed_wake_enabled,
        "post_compact_notification_wake_enabled": (
            post_compact_notification_wake_enabled
        ),
        "enroll_all_sessions": enroll_all_sessions,
        "enrolled_keys": keys,
        "band_thresholds": thresholds,
        "history_recency_seconds": history_recency_seconds,
        "debug_diagnostics_enabled": debug_diagnostics_enabled,
        "turn_quiet_seconds": turn_quiet_seconds,
        "no_composer_escalate_seconds": no_composer_escalate_seconds,
        "no_composer_stall_seconds": no_composer_stall_seconds,
        "no_composer_grow_after_seconds": no_composer_grow_after_seconds,
        "no_composer_grow_rows": no_composer_grow_rows,
        "no_composer_grow_max_attempts": no_composer_grow_max_attempts,
        "fifo_enter_delay_seconds": fifo_enter_delay_seconds,
        "fifo_input_tolerance_seconds": fifo_input_tolerance_seconds,
        "landing_timeout_seconds": landing_timeout_seconds,
        "restage_backoff_ceiling_seconds": restage_backoff_ceiling_seconds,
        "long_wait_escalate_seconds": long_wait_escalate_seconds,
        "presence_ttl_seconds": presence_ttl_seconds,
        "reboot_resume_enabled": reboot_resume_enabled,
    }


def _enabled() -> bool:
    if "AIH_INJECT_ENABLED" in os.environ:
        return os.environ["AIH_INJECT_ENABLED"].lower() in {"1", "true", "yes"}
    return _config()["injection_enabled"]


def _force_band_percent() -> int:
    """The context percentage at which the FORCE gate starts denying tool calls.

    Read from the live config instead of restated, because the denial text hardcoded `70`
    while the threshold itself was configurable -- so the number a session was told could
    differ from the number that denied it, and moving the band left the string stale. Falls
    back to the shipped default if the config's value is missing or not an int; `_config`
    already rejects a malformed `band_thresholds` wholesale, so this is belt-and-braces on a
    hook that must never raise.
    """
    thresholds = _config()["band_thresholds"]
    value = (
        thresholds.get("force") if isinstance(thresholds, dict) else None
    ) or _DEFAULT_BAND_THRESHOLDS["force"]
    if not isinstance(value, int) or isinstance(value, bool):
        return _DEFAULT_BAND_THRESHOLDS["force"]
    return value


#: The idle save-state wake (design Phase 9, D-40): its context threshold and its delay.
IDLE_SAVE_STATE_DEFAULTS = {
    "idle_save_state_pct": (50, 1, 100),
    "idle_save_state_after_seconds": (120, 30, 3600),
}


def idle_save_state_settings() -> dict[str, int]:
    """The idle save-state wake's settings from the live config, read at call time.

    The goal waker's tick reads these to wake a session idle at or above the threshold
    (ISSUE-REF). Each key is validated on its own and falls back to its shipped default,
    so a bad value here can never reset the rest of the supervisor's settings, and a bad
    value elsewhere in the file never resets these.
    """
    try:
        config = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    settings = {}
    for name, (default, low, high) in IDLE_SAVE_STATE_DEFAULTS.items():
        value = config.get(name, default)
        valid = isinstance(value, int) and not isinstance(value, bool)
        settings[name] = value if valid and low <= value <= high else default
    return settings


def _post_compact_typed_wake_enabled() -> bool:
    """Whether :meth:`ContextSupervisor.arm_continuation` may TYPE into a pane.

    Default FALSE, and that is the settled end state rather than an unfinished
    one (example-user, 2026-10-07: "keep typed wake off").  This supersedes the
    2026-10-05 reversal of ISSUE-REF, which had made `true` the target for a
    single flag that gated every leg at once.

    WHY OFF IS CORRECT AND NOT A GAP.  The typed keystroke is a REDUNDANT
    transport, not the one that carries the resume.  The SessionStart banner
    delivers the resume artifact regardless of this gate, and the post-compact
    turn was measured self-starting from the compaction summary and acting on
    that banner unprompted -- so a typed "Resume: /auto-start" in that window is
    a duplicate submission, which is exactly the double-send ISSUE-REF was
    filed about.  What ISSUE-REF changed is that the leg is now CORRECT when
    something does enable it (it serialises against the banner turn and retries
    a queued landing), so "off" is a preference rather than a known defect.

    The automation the owner asked for rides
    :func:`_post_compact_notification_wake_enabled` instead, which is default
    TRUE: a `/save-state` -> `/compact` cycle resumes end to end with no typed
    prompt at all.

    TWO SOURCES OF TRUTH, and moving one alone is a silent no-op: `_config()`'s
    fallback dict AND `src/ai_chassis/defaults/context-supervisor-config.json`,
    since an install-time-only write never retrofits a key into an existing
    file.  To exercise this leg without touching either, use the per-attempt
    override -- :func:`_typed_wake_override_requested`.
    """
    return _config()["post_compact_typed_wake_enabled"]


def _reboot_resume_enabled() -> bool:
    """Whether the ISSUE-REF stamps and boot sweep run: the reboot-signal producer.

    Read at call time, like every other gate here, so a live session can turn the
    whole mechanism off without a restart. Whether a launch then resumes, and whether
    it types, is the ``launch_resume`` object's (ISSUE-REF D-5).
    """
    return _config()["reboot_resume_enabled"]


def _launch_resume_stamp_ttl_seconds() -> int:
    """``launch_resume.stamp_ttl_seconds``: past it a stamp is expired, never resumed.

    The one TTL key, read by the boot sweep and the exit-cause classifier alike. A
    malformed ``launch_resume`` object leaves its defaults here, so the sweep still
    expires stale stamps while the launch decision skips with ``config-error``.
    """
    return int(_config()["launch_resume"]["stamp_ttl_seconds"])


def _post_compact_notification_wake_enabled() -> bool:
    """Whether the notification legs may claim the D-38 wake marker.

    Default TRUE (ISSUE-REF, example-user 2026-10-07: the poller-based auto-wake
    "enabled (with typed wake off) ... so when you /save-state and compact, it
    will be end-to-end automated").

    These legs -- :func:`run_post_compact_poller`'s claim and
    :func:`claim_wake_from_stdin` -- send NO keystrokes.  Their wake is the
    native task-completion notification re-invoking the session, so they cannot
    double-submit into a composer and none of the serialisation hazard that
    keeps the typed leg off applies to them.  They were nevertheless dark on
    every host, because one flag gated all three legs: the live log carried 16
    `CC-POLLER-NOOP` and 17 `CC-REWAKE-NOOP` lines on exactly this reason.
    """
    return _config()["post_compact_notification_wake_enabled"]


def _typed_wake_override_settings() -> type[Any]:
    """Build the one-attempt typed-wake override settings class on first use.

    IMPORTED AT CALL TIME, NOT MODULE SCOPE, and that is load-bearing rather
    than stylistic: this module is a Claude Code hook entry point that runs on
    `PreToolUse`, `UserPromptSubmit` and `SessionStart`, and `pydantic` plus
    `pydantic_settings` measured 93ms to import against the module's own 172ms
    (example-project-ec2, 2026-10-07) -- a >50% latency regression on every hook call to
    serve a path only a test takes.  The override still resolves through the
    shared skill-CLI precedence chain; only the cost moved to the caller that
    actually asks for it.
    """
    from pydantic_settings import SettingsConfigDict
    from skill_cli_foundation.settings import BaseSkillSettings

    class TypedWakeOverrideSettings(BaseSkillSettings):
        """One-attempt opt-in to the typed wake, on the shared skill precedence.

        Deliberately a :class:`BaseSkillSettings` subclass rather than a bespoke
        `os.environ` read, so the override inherits the same `init > env > TOML
        > default` chain every other skill CLI resolves through and a caller can
        pass it as a kwarg, an `AIH_SKILL_`-prefixed env var, or a flag that
        becomes one.

        Scope is ONE attempt because the resolved answer is written onto that
        attempt's own record (see :func:`_stamp_typed_wake_override`), never
        into a config file -- so the next `/save-state` reserves a fresh record
        with no override and the fleet default governs again.
        """

        model_config = SettingsConfigDict(env_prefix="AIH_SKILL_", extra="ignore")

        post_compact_typed_wake: bool = False

    return TypedWakeOverrideSettings


def _typed_wake_override_requested(*, override: bool | None = None) -> bool:
    """Resolve the per-attempt typed-wake override, failing closed.

    Fails closed on ANY resolution error, including a missing dependency: an
    override that cannot be read is an override nobody asked for, and the
    keystroke leg must never arm itself on a broken read.
    """
    try:
        settings = _typed_wake_override_settings()
        resolved = (
            settings()
            if override is None
            else settings(post_compact_typed_wake=override)
        )
        return bool(resolved.post_compact_typed_wake)
    except Exception:  # noqa: BLE001 - fail closed; this gate arms a keystroke
        return False


def _enrolled(key: str) -> bool:
    if "AIH_INJECT_KEYS" in os.environ:
        keys = os.environ["AIH_INJECT_KEYS"].split(",")
    else:
        config = _config()
        if config["enroll_all_sessions"]:
            return True
        keys = config["enrolled_keys"]
    return key in {item.strip() for item in keys if item.strip()}


def _parse_tmux_version(version: str | None) -> tuple[int, int] | None:
    """``(major, minor)`` for a tmux version string, or None when unparseable.

    Three shapes are real, all seen on this fleet: ``3.6``, ``3.7c`` -- a letter
    marks a patch release -- and ``next-3.8``, a development build of the named
    release.  Both the letter and the ``next-`` prefix are deliberately dropped.
    The letter only ever orders releases WITHIN a minor, so it cannot change a
    floor comparison; and ``next-3.8`` IS a 3.8 build, so refusing it would lock
    a host out for being early rather than for being incapable.

    The comparison has to be numeric, not lexical: ``"3.10" < "3.7"`` as strings
    while 3.10 is the newer build, so a string floor would refuse it.

    Anything that is not a version returns None and every caller fails closed.
    """
    if not version:
        return None
    match = _TMUX_VERSION_RE.match(version.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def _tmux_version_allowed(version: str | None) -> bool:
    """Whether this tmux build's SERVER may drive the composer.

    A floor, not a membership test: anything at or above ``MINIMUM_TMUX_VERSION``
    passes, so upgrading tmux can never silently remove a host's transport.  See
    that constant for why the floor sits where it does, and why an exact-version
    allowlist -- plus the environment override that existed only to work around
    it -- was deleted rather than curated.

    Fails closed on a version that does not parse and on no version at all: with
    no reachable server there is nothing to expand the fingerprint format, and
    substituting a guess there is what made the old gate unsound.
    """
    parsed = _parse_tmux_version(version)
    return parsed is not None and parsed >= MINIMUM_TMUX_VERSION


def transport_unavailable_reason(version: str | None) -> str | None:
    """Why this session can never be sent ``/compact``, or None when it can.

    The send path is a tmux ``send-keys`` into the session's own pane, so a
    session whose tmux is below ``MINIMUM_TMUX_VERSION`` has **no** transport --
    not a slow one, not an intermittent one.  Keystrokes cannot be injected into
    another process's terminal without owning it (``TIOCSTI`` is privileged), so
    there is nothing to retry and waiting is pure silence.

    Only the two permanent conditions are reported.  A missing pane fingerprint
    is deliberately NOT one of them: a pane can be renumbered or a probe can
    lose a race, and that is worth retrying.

    SCOPE, and why it is not the whole question (ISSUE-REF).  Every branch here
    asks the tmux SERVER something, so this answers "can this HOST run the
    transport", host-wide.  Whether THIS session has a pane of its own is a
    separate fact about the process, and :func:`session_pane_reason` is the one
    that answers it.  Composing both is what the three decision points do; asking
    only this one is what let a paneless session on a host with a live server pass
    every gate.
    """
    if not version:
        return "no-tmux"
    if not _tmux_version_allowed(version):
        return "tmux-version"
    return None


def session_pane_reason() -> str | None:
    """``"no-pane"`` when this process has no tmux pane, else None.

    PERMANENT FOR THE LIFE OF THE PROCESS, which is why it belongs beside the two
    host-level reasons rather than in the retry path.  The send route is a
    ``tmux send-keys`` into the session's own pane, and a running process's
    controlling terminal cannot be relocated into one -- so a session launched
    outside tmux can never be sent ``/compact`` by any amount of harness code, and
    a tmux server appearing later on the host does not give it a pane.  Nothing
    retries its way out of this; the session has to be relaunched under tmux.

    ``TMUX_PANE`` is the right witness and the only one available: the supervisor
    already reads it to pick the send target, and it is exported by the server into
    the pane's own process tree, so the hooks and pollers that run as children of a
    real session inherit it.  A session with no pane has no value to inherit.

    Kept separate from :func:`transport_unavailable_reason` deliberately.  That
    function is about the HOST; this one is about the PROCESS.  The two were
    conflated, and the conflation is exactly how ISSUE-REF hid: a paneless session
    on a host running any tmux server answered "transport available" to every
    question the harness knew how to ask.
    """
    return None if os.environ.get("TMUX_PANE") else "no-pane"


def fifo_submit_target() -> Path | None:
    """The ai-cli-utils submit FIFO this session is sent ``/compact`` through, if any.

    ISSUE-REF option (a). Selected only when the process has NO tmux pane: with a
    pane, the pane stays the transport and nothing below changes. A bare ``ai c``
    session hosted on ai-cli-utils' launcher-owned pty has no pane but does have a
    FIFO whose bytes reach the agent as typed input, so it is not transportless.
    Whether the path is safe to write is judged at send time
    (:func:`ai_chassis.submit_fifo.refusal`), so a bad FIFO is refused with its own
    reason instead of falling back to ``no-pane``.
    """
    if os.environ.get("TMUX_PANE"):
        return None
    from ai_chassis import submit_fifo  # noqa: PLC0415 - hook import latency

    return submit_fifo.discover()


def mod_presence_reason(session_id: str | None) -> str | None:
    """None when compact-guard is loaded in ``session_id`` and no pane exists, else why not.

    ISSUE-REF D-2: the mod route is asked only for a process with no tmux pane, so a
    session with a pane keeps the tmux transport whatever the mod records. The reason is
    a ``mod-absent`` detail: ``pane``, ``no-session``, or the contract's own reading of
    the presence record (``absent``, ``stale``, ``malformed``, ``other-session``,
    ``unreadable``).
    """
    if os.environ.get("TMUX_PANE"):
        return "pane"
    if not session_id:
        return "no-session"
    from ai_chassis import compact_mod_contract  # noqa: PLC0415 - hook import latency

    return compact_mod_contract.presence_reason(
        session_id, ttl_seconds=float(_config()["presence_ttl_seconds"])
    )


def compact_transport_reason(
    version: str | None, *, session_id: str | None = None
) -> str | None:
    """Why this session can never be sent ``/compact``, counting the FIFO and the mod.

    The host and pane questions of :func:`transport_unavailable_reason` and
    :func:`session_pane_reason`, asked only when no submit FIFO is selected and
    compact-guard has no fresh presence record for ``session_id`` (ISSUE-REF).
    """
    if fifo_submit_target() is not None:
        return None
    if mod_presence_reason(session_id) is None:
        return None
    return transport_unavailable_reason(version) or session_pane_reason()


# Overridable so the remedy check is testable without a real ai-cli-utils install,
# mirroring AIH_CLAUDE_SETTINGS_PATH.
_AI_CLI_CONFIG_PATH_ENV = "AIH_AI_CLI_CONFIG_PATH"

# Why `ai c <name>` would not hand the relaunched session a pane. Each is a real,
# reachable configuration, not a hypothetical.
_RELAUNCH_INERT_REASONS = {
    "ai-not-on-path": "`ai` is not on this session's PATH",
    "use-tmux-false": "`[session] use_tmux = false` is set in ai-cli-utils' config",
}


def _ai_cli_config_paths() -> tuple[Path, ...]:
    """Where ai-cli-utils' config may live, current directory name first."""
    override = os.environ.get(_AI_CLI_CONFIG_PATH_ENV)
    if override:
        return (Path(override),)
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return (base / "ai-cli-utils" / "config.toml", base / "ai-cli" / "config.toml")


def relaunch_inert_reason() -> str | None:
    """Why ``ai c <name>`` would NOT give a relaunched session a pane, else None.

    The ``no-pane`` notice names "relaunch under ``ai c <name>``" as its remedy, and
    that remedy is inert in three measured configurations: ``ai`` absent from PATH,
    ``[session] use_tmux = false``, and the ``-b/--bare`` flag.  Only the first two
    are observable here -- a flag is chosen at the next launch, so nothing can read
    it in advance, and the notice says so rather than implying it checked.

    This exists because of ISSUE-REF: **a refusal must name an action that can
    actually clear it.**  Naming a remedy that cannot work is the same defect as
    naming none, and it is worse at SessionStart than at the FORCE band, because at
    turn zero the reader will actually act on it.

    WHICH WAY IT FAILS, chosen deliberately.  An unreadable or unparseable config
    returns None, so the remedy is still named.  Recommending a remedy that turns
    out to be inert costs one wasted relaunch at turn zero; suppressing one that
    would have worked leaves the reader with no action at all, which is the failure
    this function exists to prevent.
    """
    if shutil.which("ai") is None:
        return "ai-not-on-path"
    for path in _ai_cli_config_paths():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            config = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, ValueError):
            # Cannot tell; see the failure-direction note above.
            return None
        session = config.get("session")
        if isinstance(session, dict) and session.get("use_tmux") is False:
            return "use-tmux-false"
        return None
    return None


def relaunch_advice() -> str:
    """The relaunch remedy, conditioned on whether it can actually work.

    ONE definition shared by both callers -- the SessionStart verdict and the
    FORCE-band notice -- because the previous arrangement stated it inside the
    shared ``no-pane`` sentence, where it could not be conditioned, and the
    SessionStart verdict then contradicted it two lines later.
    """
    inert = relaunch_inert_reason()
    if inert is None:
        return (
            "Relaunch with `ai c <name>`, which launches into a tmux pane. Nothing\n"
            "else gives a running process one.\n"
        )
    return (
        f"`ai c <name>` would NOT help here: {_RELAUNCH_INERT_REASONS[inert]}, so a\n"
        "relaunch through it lands bare as well. Fix that first, or use the routes\n"
        "below -- nothing else gives a running process a pane.\n"
    )


def launch_transport_reason(
    *, tmux: Any | None = None, session_id: str | None = None
) -> str | None:
    """Why this session can never be SENT ``/compact``, decided at launch, else None.

    Composes the two facts the harness had been asking separately -- and asking
    only one of which is how ISSUE-REF hid for weeks:

    * :func:`transport_unavailable_reason` is about the HOST (is there a usable
      tmux server at all), and yields ``no-tmux`` / ``tmux-version``;
    * :func:`session_pane_reason` is about the PROCESS (does *this* session own a
      pane), and yields ``no-pane``.

    Host first, because ``no-tmux`` is the more actionable of the two and a host
    with no server gives every session on it no pane -- reporting ``no-pane`` there
    would send the reader to relaunch into a tmux that is not running.

    NEVER RAISES.  ``tmux`` is injectable for tests; the default adapter shells out,
    and a missing binary surfaces as ``OSError`` from :meth:`TmuxAdapter.version`,
    which is mapped to ``no-tmux`` rather than allowed to escape. A SessionStart
    hook that crashes costs the session its startup, so a verdict it cannot compute
    must degrade to silence.

    A fresh compact-guard presence record for ``session_id`` is a transport too
    (ISSUE-REF T-2.6): Phase 1 M-6 measured the mod's ``session.start`` before the
    settings ``SessionStart`` hooks, so the record is already there to read.
    """
    if fifo_submit_target() is not None:
        return None
    if mod_presence_reason(session_id) is None:
        return None
    adapter = tmux if tmux is not None else TmuxAdapter()
    try:
        version = adapter.version()
    except OSError:
        return "no-tmux"
    reason = transport_unavailable_reason(version)
    if reason is not None:
        return reason
    return session_pane_reason()


def session_start_transport_verdict(payload: dict[str, Any], key: str) -> str | None:
    """The turn-zero verdict text, or None when there is nothing worth saying.

    WHY THIS EXISTS AT SessionStart AT ALL (ISSUE-REF D-1).  The same fact was
    already reported, correctly, at the FORCE band -- i.e. at >=80% context, through
    the ``UserPromptSubmit`` notice.  But the only remedy it can offer is a
    RELAUNCH, and a relaunch is cheap exactly once: before the session has
    accumulated anything.  So the harness was reporting a launch-time fault at
    end-of-life, when acting on it costs the whole window. The fix is not better
    wording; it is a different moment.

    SILENT WHEN IRRELEVANT, which is most of the time.  None when this session owns
    a pane, when the supervisor is disabled, and when this key is not enrolled -- a
    startup banner that fires for sessions it does not apply to is noise, and noise
    at turn zero is read once and then never again.

    It deliberately does NOT claim the session cannot compact.  It cannot be *sent*
    ``/compact``; Claude Code's native auto-compact needs no pane, and ``/compact``
    can still be typed. Saying otherwise would be false and would also discourage
    the one route that still works.
    """
    if not _enabled() or not _enrolled(key):
        return None
    # The event matters only for the session id the mod's presence record is keyed on.
    session_id = payload.get("session_id")
    reason = launch_transport_reason(
        session_id=session_id if isinstance(session_id, str) else None
    )
    if reason is None:
        return None
    detail = _TRANSPORT_NOTICE_TEXT.get(reason, reason)
    lines = [
        f"AUTO-COMPACT CANNOT BE SENT TO THIS SESSION (reason={reason}).",
        "",
        "Decided at launch, not now, and not repairable afterwards:",
        f"{detail}.",
        "",
        "ACTING ON THIS IS FREE RIGHT NOW AND NOT LATER. Any remedy is a relaunch,",
        "and this session has accumulated nothing yet. The same verdict at the FORCE",
        f"band ({_force_band_percent()}%) costs the whole window.",
    ]
    # Only `no-pane` has a relaunch remedy. `no-tmux` means no server answered, so
    # relaunching into tmux is not the next step, and `tmux-version` is cleared by
    # upgrading tmux -- naming `ai c` for either would be an action that cannot
    # clear the refusal, which is the defect this whole paragraph exists to avoid.
    # Caught by `test_no_server_reports_the_host_fact_not_the_pane_fact` after the
    # first draft appended the advice unconditionally.
    if reason == "no-pane":
        lines += ["", relaunch_advice().rstrip("\n")]
    lines += [
        "",
        "THIS SESSION CAN STILL COMPACT. What is missing is only the keystroke:",
        "`/compact` is a REPL-only built-in, so type it yourself when ready, and",
        "Claude Code's native auto-compact needs no pane at all.",
    ]
    return "\n".join(lines) + "\n"


_TRANSPORT_NOTICE_TEXT = {
    # Says only what was established. The supervisor asks the tmux SERVER for its
    # version, so a host where the binary is installed and no server is running lands
    # here -- the measured state on example-client-host-mac (`tmux -V` -> 3.7c, `tmux ls` -> no server).
    # The old wording, "this session is not running under tmux", read as a claim about
    # the binary and sent readers to install something they already had.
    "no-tmux": (
        "no tmux server answered, so this session has no pane to send into\n"
        "(the supervisor asks the running server, not the tmux binary)"
    ),
    # ISSUE-REF.  Distinct from `no-tmux` because the remedy is different and the
    # reader is otherwise sent to fix a server that is already running: here a tmux
    # server DID answer, and this session still has no pane of its own because it was
    # not launched into one.  Says what to do about it, since nothing retries.
    # ISSUE-REF: the remedy clause that used to end this sentence ("-- relaunch under
    # `ai c <name>`") moved to :func:`relaunch_advice`, which CONDITIONS it. Stating
    # it here made it unconditional, and three reachable configurations make it inert
    # -- so the notice could recommend an action that cannot clear it (ISSUE-REF).
    # The fact stays here; the action is composed by both callers from one place.
    "no-pane": (
        "a tmux server is running but this session has no pane of its own, so there\n"
        "is nothing to send into (it was launched outside tmux, and a server\n"
        "appearing later cannot give a running process a pane)"
    ),
    "tmux-version": (
        "this host's tmux is older than the minimum this transport needs, so the "
        "composer fence refuses to drive it -- upgrade tmux"
    ),
    "compact-delivered-as-text": (
        "Claude Code delivered the staged `/compact` as plain user text instead of "
        "executing the built-in command"
    ),
    # ISSUE-REF: the bare-pty FIFO transport's refusals. Each names what was found at
    # the FIFO path, because each has a different owner to fix it.
    "fifo-missing": "no FIFO exists at the submit path the session was launched with",
    "fifo-unreadable": "the submit FIFO's path could not be inspected",
    "fifo-not-a-fifo": "the submit path is not a FIFO (a symlink is refused too)",
    "fifo-wrong-owner": "the submit FIFO belongs to another user",
    "fifo-wrong-mode": "the submit FIFO is not mode 0600",
    "fifo-no-reader": (
        "nothing is reading the submit FIFO, so the launcher that owned this "
        "session's pty is gone"
    ),
    "fifo-write-error": "writing to the submit FIFO failed",
    # ISSUE-REF: the in-process mod route's terminal refusal.
    "mod-compaction-disabled": (
        "the compact-guard mod's `$.session.compact` call was refused because "
        "compaction is switched off in this session"
    ),
    "fifo-idle-unprovable": (
        "it could not prove the composer holds no typed input (it needs the "
        "launcher's record of the last typed input, a 0600 <session>.input-at "
        "beside the FIFO, and a submitted prompt in the transcript)"
    ),
}

# Claude Code's OWN auto-compact, and the only compaction route that needs no transport
# whatsoever -- no pane, no keystroke, no terminal the harness has to own.  These names
# are the documented public surface that decides where it fires; the harness reads them
# to REPORT the configuration (see :func:`native_autocompact_source`), and
# `.claude/bin/ctx-pct` derives the boundary from them.
#
# Precedence is Claude Code's, not ours (model-config.md § Set the auto-compact window,
# fetched 2026-10-15): the environment variable, then the `--autocompact` launch flag,
# then the window `/autocompact` saves per model under `modelSettings` (v2.1.288+), then
# the top-level `autoCompactWindow`.  The client asserts the first rung itself: with the
# variable set its configurator says "CLAUDE_CODE_AUTO_COMPACT_WINDOW is set and takes
# precedence. Unset it to change this setting here."  (2.1.282 binary, ISSUE-REF.)
_NATIVE_AUTOCOMPACT_WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
_NATIVE_AUTOCOMPACT_PCT_ENV = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"
_NATIVE_AUTOCOMPACT_SETTINGS_KEY = "autoCompactWindow"
_NATIVE_AUTOCOMPACT_MODEL_SETTINGS_KEY = "modelSettings"
# "Models running with a native 1M window compact before the window fills, at about 967K
# tokens by default" (model-config.md § Default auto-compact thresholds, 2026-10-15).
NATIVE_1M_DEFAULT_AUTOCOMPACT_WINDOW = 967_000
_AUTOCOMPACT_DOCS_URL = "https://code.claude.com/docs/en/model-config.md"


def _claude_settings_path() -> Path:
    """Where Claude Code's user settings live, overridable for tests."""
    return Path(
        os.environ.get("AIH_CLAUDE_SETTINGS_PATH")
        or Path.home() / ".claude" / "settings.json"
    )


def native_autocompact_source(model_id: str | None = None) -> str | None:
    """Where Claude Code's auto-compact window is configured, or None when it is not.

    Returns ``"env"`` for :data:`_NATIVE_AUTOCOMPACT_WINDOW_ENV`, ``"model_settings"`` for
    a window ``/autocompact`` saved for ``model_id`` under ``modelSettings``, ``"settings"``
    for the top-level ``autoCompactWindow`` user setting (or the variable in the settings
    file's ``env`` block), and ``None`` when none applies -- in which case the client uses
    its documented default (:data:`NATIVE_1M_DEFAULT_AUTOCOMPACT_WINDOW` on a native-1M
    model, else the context limit).  A ``modelSettings`` entry saved for another model is
    not this session's window, so without ``model_id`` that rung is skipped, and a saved
    ``"auto"`` selects the default for its model even over the top-level key.

    Fails open to ``None`` on an unreadable or malformed settings file: an unknown
    configuration and an absent one lead to the same advice, and a notice path must not
    raise.
    """
    if os.environ.get(_NATIVE_AUTOCOMPACT_WINDOW_ENV):
        return "env"
    try:
        settings = json.loads(_claude_settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(settings, dict):
        return None
    environment = settings.get("env")
    if isinstance(environment, dict) and environment.get(
        _NATIVE_AUTOCOMPACT_WINDOW_ENV
    ):
        return "settings"
    saved = _saved_model_window(settings, model_id)
    if saved == "auto":
        return None
    if saved is not None:
        return "model_settings"
    if settings.get(_NATIVE_AUTOCOMPACT_SETTINGS_KEY):
        return "settings"
    return None


def _saved_model_window(settings: dict[str, Any], model_id: str | None) -> object:
    """The ``autoCompactWindow`` saved for ``model_id`` under ``modelSettings``, or None.

    Entries are keyed by the canonical name (``claude-opus-5-5``), and Claude Code matches
    the model's ``[1m]``, date-suffixed and provider-specific ids to the same entry
    (settings-reference.md ``modelSettings``, fetched 2026-10-15).
    """
    from .session_model import canonical_model_id

    canonical = canonical_model_id(model_id) if model_id else None
    entries = settings.get(_NATIVE_AUTOCOMPACT_MODEL_SETTINGS_KEY)
    if canonical is None or not isinstance(entries, dict):
        return None
    for key, entry in entries.items():
        if (
            isinstance(key, str)
            and canonical_model_id(key) == canonical
            and isinstance(entry, dict)
        ):
            value = entry.get(_NATIVE_AUTOCOMPACT_SETTINGS_KEY)
            if value == "auto":
                return value
            if _settings_window_tokens(value) is not None:
                return value
    return None


def _settings_window_tokens(value: object) -> int | None:
    """A settings window in tokens, or None outside the documented 100000-1000000 range.

    Same reading as ``_settings_window`` in ``.claude/bin/ctx-pct``: an integer, or a
    string of digits with an optional k/m suffix.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        tokens = value
    elif isinstance(value, str) and (
        match := re.fullmatch(r"([0-9]{1,8})([kKmM]?)", value)
    ):
        scale = {"": 1, "k": 1000, "m": 1_000_000}[match.group(2).lower()]
        tokens = int(match.group(1)) * scale
    else:
        return None
    return tokens if 100_000 <= tokens <= 1_000_000 else None


def native_autocompact_notice() -> str:
    """The paragraph a transport-less session needs: the route that needs no keystroke.

    A bare session's old notice named only the remedy it could not use (launch under
    tmux) and never mentioned that Claude Code compacts on its own.  That made the
    failure silent in the way that matters: the reader was told what was broken and
    not told that a working route existed, nor that this harness's own FORCE band can
    stand between the session and that route.

    TOTAL BY CONTRACT.  Its caller, :func:`write_transport_notice`, builds the notice
    body *before* the try block that writes it, and is documented never to raise -- "a
    failure to explain a decline must not become a second failure".  This function
    reads a settings file and the supervisor config to compose its text, so it degrades
    to the one sentence that is true regardless rather than letting a config read
    deprive a stranded session of its entire explanation.
    """
    try:
        return _native_autocompact_notice_text()
    except Exception:  # noqa: BLE001 - see the totality contract above
        return (
            "THE ROUTE THAT NEEDS NO KEYSTROKE. Claude Code's own auto-compact is on by\n"
            "default and needs no pane and no transport, so this session can still compact\n"
            "itself. Its configuration could not be read here, so whether it fires below\n"
            "this harness's FORCE band is unknown -- check `ctx-pct --advice`.\n"
        )


def _native_autocompact_notice_text() -> str:
    """Compose the paragraph; see :func:`native_autocompact_notice` for the contract."""
    source = native_autocompact_source()
    if source is not None:
        origin = {
            "env": f"the {_NATIVE_AUTOCOMPACT_WINDOW_ENV} environment variable",
            "model_settings": "the window `/autocompact` saved for this model",
        }.get(source, f"the `{_NATIVE_AUTOCOMPACT_SETTINGS_KEY}` user setting")
        return (
            "THE ROUTE THAT NEEDS NO KEYSTROKE. Claude Code's own auto-compact is on by\n"
            f"default and its window is pinned here, from {origin}. It needs no pane and\n"
            "no transport, so this session will still compact itself.\n"
            "`ctx-pct --json` DERIVES the boundary from that pinned window and bands on the\n"
            f"distance to it, so the FORCE band ({_force_band_percent()}%) means "
            f"{_force_band_percent()}% of the\n"
            "way to compaction rather than that share of the window. The derived boundary is\n"
            "an UPPER BOUND: Claude Code subtracts a private per-model summary buffer, so it\n"
            "fires at or before that point.\n"
            "The deny cannot strand you either -- with no transport it releases once\n"
            "/save-state has published a resume artifact.\n"
        )
    return (
        "THE ROUTE THAT NEEDS NO KEYSTROKE, and where it fires.\n"
        "Claude Code's own auto-compact is on by default and needs no pane and no\n"
        f"transport at all. {_NATIVE_AUTOCOMPACT_WINDOW_ENV} is unset and\n"
        f"`{_NATIVE_AUTOCOMPACT_SETTINGS_KEY}` is absent from ~/.claude/settings.json, so "
        "unless `/autocompact`\n"
        "saved a window for this model it fires at Claude Code's DOCUMENTED DEFAULT:\n"
        f"about {NATIVE_1M_DEFAULT_AUTOCOMPACT_WINDOW:,} tokens on a model with a native "
        "1M window (Fable, Sonnet 5+,\n"
        "Haiku 5.5, Opus 4.7+), and the context limit on other models\n"
        f"({_AUTOCOMPACT_DOCS_URL}). `ctx-pct --json` derives that boundary for\n"
        "this session's model, names its source, and bands on the distance to it, so the\n"
        f"FORCE band ({_force_band_percent()}%) means that share of the way to compaction. "
        "The derived\n"
        "boundary is an UPPER BOUND: the client keeps a summary buffer it does not\n"
        "publish, so it fires at or before that point. Nothing needs pinning for this.\n"
        "With no send transport the deny RELEASES once /save-state has published a resume\n"
        "artifact, so this session can always reach the free compaction. Checkpoint\n"
        "first -- the deny is not a wall.\n"
    )


def _remove_mod_request(record: dict[str, Any]) -> None:
    """Remove a terminal mod attempt's request (T-2.2); best-effort, like the log."""
    session_id = record.get("mod_session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    from ai_chassis import compact_mod_contract  # noqa: PLC0415 - hook import latency

    with contextlib.suppress(OSError):
        compact_mod_contract.remove_request(
            session_id, attempt_id=str(record.get("attempt_id"))
        )


def write_transport_notice(
    key: str,
    reason: str,
    *,
    attempt_id: str | None = None,
    root: Path | None = None,
    mod_absent: str | None = None,
) -> Path | None:
    """Leave a one-shot, human-readable notice that auto-compact cannot fire.

    A bare session has no pane, so the supervisor cannot tell it anything the
    way it normally would.  What it *can* do is leave a file the
    ``UserPromptSubmit`` hook reads with no Python at all and emits as
    additional context -- the same tmux-independent channel the post-compact
    resume already rides, and the one thing measured to reach a bare session.

    Best-effort by contract, like :func:`log_stage`: this is called from decline
    paths, and a failure to explain a decline must not become a second failure.
    """
    directory = (root or _root_from_environment()) / "notices"
    detail = _TRANSPORT_NOTICE_TEXT.get(reason, reason)
    if reason == "compact-delivered-as-text":
        body = (
            f"AUTO-COMPACT RETRYING (reason={reason}).\n\n{detail}. The supervisor "
            "will retry after its backoff and fresh idle/composer checks.\n"
        )
    else:
        channel = (
            "writing it into this session's ai-cli submit FIFO"
            if reason.startswith("fifo-")
            else "typing it into this session's own\ntmux pane"
        )
        body = (
            "THE SUPERVISOR WILL NOT SEND `/compact` IN THIS SESSION "
            f"(reason={reason}).\n\n"
            f"The context supervisor arms `/compact` by {channel}, and {detail}. Nothing"
            " is retrying; no `/compact` will arrive from\n"
            "the supervisor. There is no assistant-callable `/compact` either -- it is a\n"
            "REPL-only built-in, so no hook, tool, or model action can start one.\n\n"
            "Saved state is unaffected -- the resume message was written and will still be\n"
            "delivered after a compact. What is missing is only the keystroke.\n\n"
            f"{native_autocompact_notice()}\n"
            # ISSUE-REF: the relaunch remedy is composed here, conditioned, rather
            # than stated inside the shared `no-pane` sentence where it could not be.
            # Only `no-pane` has a relaunch remedy at all -- `no-tmux` means no server
            # answered, so relaunching into tmux is not the next step, and
            # `tmux-version` is fixed by upgrading tmux.
            f"{relaunch_advice() if reason == 'no-pane' else ''}"
            "Do this: run `/compact` yourself when ready (invoke `/save-state` first if it\n"
            "has not run).\n\n"
            # ISSUE-REF.  This notice is injected as additional context, and the turn
            # that reads it has to relay "you will need to type `/compact` yourself".
            # That sentence matches `context-claim-guard`'s Stop predicate, so the
            # relay was refused after an otherwise-complete save-state.  The guard is
            # deliberately left alone -- its own header records that a false negative
            # defeats the unmeasured-context-claim rule it enforces while a false
            # positive costs one extra read -- so the instruction carries its own
            # remedy instead.
            "MEASURE BEFORE YOU RELAY THIS: naming `/compact` in a reply is a context\n"
            "claim, so read `~/.claude/bin/ctx-pct` and `~/.claude/bin/ctx-pct --advice`\n"
            "in the same turn and quote both outputs verbatim. Without that reading\n"
            "`context-claim-guard` refuses the Stop and the explanation never lands\n"
            "(ISSUE-REF).\n"
        )
    if mod_absent:
        # ISSUE-REF T-2.1: the in-process route was asked too, and why it was not taken.
        body += (
            f"\nThe in-process route is unavailable as well: mod-absent ({mod_absent}). "
            "It needs the\ncompact-guard mod loaded in this session "
            "(config/mods-by-machine.toml), which\nrecords its presence at session start "
            "and on every turn.\n"
        )
    if attempt_id:
        body += f"\nattempt_id={attempt_id}\n"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / f"{_safe_marker_component(key)}.md"
        # A unique temp name per writer: a shared one let two concurrent writers
        # splice their bodies into one published notice (ISSUE-REF).
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            temporary.write_text(body, encoding="utf-8")
            temporary.replace(path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
    except OSError:
        return None
    return path


# What a fault's reason actually means to the operator, keyed by the reason string
# the lifecycle exits with.  Only reasons whose remedy is not obvious from the name
# need an entry; anything absent is printed as itself rather than guessed at.
#
# `state=REFUSED` earns the longest entry because its own name is actively
# misleading: it reads as the OBSERVER having been refused something, and three
# sessions in a row diagnosed it as a failure to bind.  It is neither.  The attempt
# the observer was launched to watch went terminal underneath it, and the measured
# cause (ISSUE-REF, 2026-10-06) was `handle_user_prompt` refusing the attempt
# because a human prompt arrived before the compact was sent.
_POLLER_FAULT_DETAIL = {
    "state=REFUSED": (
        "the save-state attempt this was watching went TERMINAL before the compact\n"
        "could be sent, so the observer had nothing left to observe. The observer did\n"
        "not fail to start and did not crash.\n\n"
        "THE USUAL CAUSE IS YOUR OWN NEXT MESSAGE. An unsent `/save-state` attempt is\n"
        "refused the moment a user prompt arrives (`reason=user-prompt-before-fast-\n"
        "release`), because the supervisor arms `/compact` by typing into this\n"
        "session's composer and must never type into a composer a human is using.\n"
        "Typing anything between `/save-state` and the compact therefore cancels the\n"
        "automation on purpose -- the saved state itself is unaffected."
    ),
    "state=AMBIGUOUS": (
        "a compact started that PreCompact could not attribute to this attempt\n"
        "(`causal-correlation-mismatch`). The measured cause is that\n"
        "more than one save-state attempt was pending when it started (resume.log\n"
        "`why=pending=2`), so it could not tell which saved state the compact\n"
        "belonged to. If you typed `/compact` yourself, that compact still ran;\n"
        "only this attempt's bookkeeping ended"
    ),
    "attempt-not-unique": (
        "the attempt record the observer was launched for was not found exactly once,\n"
        "which is the shape a mis-derived session key produces (ISSUE-REF). Nothing\n"
        "was observed"
    ),
    "transcript-gone": (
        "this session's transcript stopped resolving, so the observer had no evidence\n"
        "channel left to read"
    ),
    "returned-without-verdict": (
        "the observer returned without recording why, which is itself a defect in the\n"
        "lifecycle rather than a statement about this session"
    ),
    "poller-died-without-verdict": (
        "the observer process disappeared while it still intended to poll -- it was\n"
        "killed rather than having decided anything, so its last published phase is\n"
        "the newest thing known about it"
    ),
}


def _poller_fault_recovery(key: str, phase: str, attempt_id: str | None) -> str:
    """The recovery that actually applies, chosen by what the phase establishes.

    Two different failures need two different remedies, and offering the wrong one
    wastes the operator's window:

    * ``dead`` and ``crashed`` say the OBSERVER stopped.  They say nothing against
      the attempt, which is commonly still armed -- the measured trigger is a
      prompt typed right after `/save-state` interrupting the background shell
      that held the observer.  Relaunching re-attaches an observer to that same
      attempt, so the ``attempt_id`` and its one-time completion nonce survive and
      no second `/save-state` is needed.
    * ``abandoned`` says the ATTEMPT was already gone or unactionable when the
      observer read it.  Relaunching would re-read the same terminal record and
      abandon again, so the only recovery is a fresh arm.

    Decided from the phase alone, which is why this needs no ledger read: the
    phase is already a statement about which of the two stopped, and it is
    available on every path that writes a notice.
    """
    if phase in {"dead", "crashed"} and attempt_id:
        return (
            "RELAUNCH THE OBSERVER -- the attempt is probably still armed, so this\n"
            "does NOT need another `/save-state` and does NOT invalidate the\n"
            "attempt id or its completion nonce:\n\n"
            # ONE line, no label line and no continuation: this notice is read at
            # FORCE, where the gate refuses a newline rawly, so the labelled
            # multi-line block it used to print was advice the reader could not
            # follow (ISSUE-REF). The wrapper is on the self-rescue allowlist.
            f"  compact-lifecycle --key {key} --attempt-id {attempt_id} "
            "--interval-seconds 30 --warning-interval-seconds 300 "
            "--long-wait-escalate-seconds 7200 --post-compact-escalate-seconds 600\n\n"
            "Run it in the background. If it exits at once, read `poller-health`:\n"
            "an `abandoned` verdict means the attempt went terminal too and a fresh\n"
            "`/save-state` is needed after all.\n"
        )
    return (
        "Do this: run `/save-state` again and then `/compact` WITHOUT typing\n"
        "anything in between, or just run `/compact` yourself if the saved state is\n"
        "current. Re-arming is safe and does not poison the earlier attempt's key --\n"
        "a fresh arm supersedes its predecessors and becomes the live one.\n"
    )


def write_poller_fault_notice(
    key: str,
    phase: str,
    reason: str,
    *,
    attempt_id: str | None = None,
    root: Path | None = None,
) -> Path | None:
    """Tell the operator a compact observer failed, without being asked.

    ``poller_health`` can already answer this question precisely, and ISSUE-REF
    added it for exactly that reason.  But asking is OPT-IN, and nothing makes a
    caller ask: the lifecycle exits 0 on every path it takes, including the ones
    that observed nothing, and until ISSUE-REF its stdout was empty too, so a
    launched-and-dead observer was byte-identical to a healthy one from outside
    the process.  Measured three times in one session on 2026-10-06 (ISSUE-REF),
    each time reported to the launcher as a successful background task.  A
    SIGKILLed observer still prints nothing, which is why this notice exists.

    So the fault announces itself here, through the notice channel ISSUE-REF
    already proved reaches a session with no pane: ``UserPromptSubmit`` reads
    ``notices/<key>.md`` with no Python at all and emits it as additional
    context.  Deliberately the SAME mechanism rather than a second one -- a
    parallel channel would be a second thing to drift, and the hook's unknown-
    reason branch already delivers anything it cannot re-check.

    The first line carries ``reason=poller-fault`` and not the raw reason,
    because the hook parses that line with ``[a-z-]*`` to decide whether the
    notice has gone stale.  A raw ``state=REFUSED`` would be captured as
    ``state``, which happens to fall through to the deliver-anyway branch today
    but only by accident; a stable lowercase token says what it means.  The real
    reason is stated in the body, where nothing parses it.

    Best-effort by contract, like :func:`log_stage` and
    :func:`write_transport_notice`: this is called from failure paths, and
    failing to explain a failure must not become a second, louder one.
    """
    directory = (root or _root_from_environment()) / "notices"
    detail = _POLLER_FAULT_DETAIL.get(reason, reason)
    body = (
        f"THE COMPACT OBSERVER FOR THIS SESSION FAILED (reason=poller-fault).\n\n"
        f"phase={phase} reason={reason}\n\n"
        f"What happened: {detail}.\n\n"
        "WHY YOU WERE NOT TOLD SOONER. The observer is an observer, so it exits 0 on\n"
        "every path by design and other callers depend on that -- its exit code can\n"
        "never carry this. Nothing is retrying and no `/compact` will arrive from the\n"
        "supervisor for this attempt.\n\n"
        "Saved state is unaffected: the resume message was written and will still be\n"
        "delivered after a compact. What is missing is the keystroke.\n\n"
        f"{_poller_fault_recovery(key, phase, attempt_id)}\n"
        "To confirm independently: `ai_chassis.context_supervisor poller-health`\n"
        "with this key -- `--attempt-id` is optional and defaults to the newest\n"
        "attempt -- reports the same verdict from the record, and always prints one.\n\n"
        "MEASURE BEFORE YOU RELAY THIS: naming `/compact` in a reply is a context\n"
        "claim, so read `~/.claude/bin/ctx-pct` and `~/.claude/bin/ctx-pct --advice`\n"
        "in the same turn and quote both outputs verbatim. Without that reading\n"
        "`context-claim-guard` refuses the Stop and the explanation never lands\n"
        "(ISSUE-REF).\n"
    )
    if attempt_id:
        body += f"\nattempt_id={attempt_id}\n"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / f"{_safe_marker_component(key)}.md"
        # A unique temp name per writer: a shared one let two concurrent writers
        # splice their bodies into one published notice (ISSUE-REF).
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            temporary.write_text(body, encoding="utf-8")
            temporary.replace(path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
    except OSError:
        return None
    return path


def write_continuation_fault_notice(
    key: str,
    reason: str,
    *,
    attempt_id: str | None = None,
    retries: int = 0,
    root: Path | None = None,
) -> Path | None:
    """Tell the operator a queued resume prompt was finally written off.

    Deliberately the SAME ``notices/<key>.md`` channel
    :func:`write_poller_fault_notice` established rather than a second one: that
    channel is already proved to reach a session with no pane, because
    ``UserPromptSubmit`` reads the file with no Python at all and emits it as
    additional context.  A parallel channel would be a second thing to drift.

    This exists because the old failure was SILENT.  A queued continuation went
    to TERMINAL ``CONTINUATION_AMBIGUOUS`` with ``reason=confirmation-timeout``
    and nothing anywhere said so, so a session could lose its resume turn and
    show no trace of it outside a stage log nobody reads.  Reaching terminal is
    now the end of a bounded retry rather than the first timeout, which makes the
    notice a statement about an exhausted budget instead of a race.

    Best-effort by contract, like every other notice writer here: failing to
    explain a failure must not become a second, louder one.
    """
    directory = (root or _root_from_environment()) / "notices"
    body = (
        "YOUR RESUME TURN WAS QUEUED AND NEVER CONFIRMED (reason=continuation-fault).\n\n"
        f"reason={reason} retries={retries}\n\n"
        "What happened: the typed post-compact wake submitted your resume command and\n"
        "Claude Code accepted it into its PROMPT QUEUE rather than running it. A queued\n"
        "prompt never fires `UserPromptSubmit`, which is the only signal that confirms\n"
        "this attempt, so the confirmation wait was extended with backoff and has now\n"
        "been exhausted.\n\n"
        "WHAT IS AND IS NOT LOST. Your saved state is unaffected -- the resume artifact\n"
        "was written and the SessionStart banner still delivers it. If the queued prompt\n"
        "eventually ran, you have already resumed and this notice is redundant. If it was\n"
        "dropped, re-run your resume command yourself; it is in the banner.\n\n"
        "NOTE THAT THE TYPED WAKE IS OFF BY DEFAULT. Seeing this at all means either an\n"
        "operator enabled it or this attempt carried the one-attempt test override\n"
        "(AIH_SKILL_POST_COMPACT_TYPED_WAKE=1). The notification-based wake, which is the\n"
        "default path, does not type and cannot produce this fault.\n"
    )
    if attempt_id:
        body += f"\nattempt_id={attempt_id}\n"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / f"{_safe_marker_component(key)}.md"
        # A unique temp name per writer: a shared one let two concurrent writers
        # splice their bodies into one published notice (ISSUE-REF).
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            temporary.write_text(body, encoding="utf-8")
            temporary.replace(path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
    except OSError:
        return None
    return path


def write_no_composer_stall_notice(
    key: str,
    *,
    attempt_id: str,
    waited_seconds: int,
    frame: str,
    frame_width: str,
    pane_geometry: str,
    root: Path | None = None,
) -> Path | None:
    """Tell the session once that its compact waits on a composer it cannot see.

    The no-composer wait used to escalate every few minutes for as long as the
    lifecycle lived, with a status-line message telling the operator to clear a
    draft that did not exist (ISSUE-REF: 73 minutes on ISSUE-REF, 7 hours on
    aih-ISSUE-REF).  This is the bounded end of that wait.  Same notice channel
    as :func:`write_poller_fault_notice`, and best-effort like it.
    """
    directory = (root or _root_from_environment()) / "notices"
    measurement = (
        f"waited_s={waited_seconds} frame={frame} frame_width={frame_width} "
        f"pane={pane_geometry}\n\n"
    )
    context_claim = (
        "MEASURE BEFORE YOU RELAY THIS: naming `/compact` in a reply is a context\n"
        "claim, so read `~/.claude/bin/ctx-pct` and `~/.claude/bin/ctx-pct --advice`\n"
        "in the same turn and quote both outputs verbatim (ISSUE-REF).\n"
        f"\nattempt_id={attempt_id}\n"
    )
    if frame == "permission-dialog":
        body = (
            "AUTO-COMPACT IS WAITING ON A PERMISSION PROMPT "
            "(reason=no-composer-stalled).\n\n"
            + measurement
            + "What happened: /save-state armed a compact, but for the whole wait a\n"
            "Claude Code permission prompt (`Do you want to ...?`) held the input area,\n"
            "so there was no prompt line to type into. A background agent's tool call\n"
            "can raise one in this pane while the main thread keeps working.\n\n"
            "WHAT FIXES IT. Answer the prompt in the pane (pick an option or press Esc).\n"
            "The attempt is still pending, so the supervisor sends /compact once the\n"
            "composer is back. Do not type /compact into the pane while the prompt\n"
            "shows: the prompt takes the keystrokes, and Enter accepts the selected\n"
            "option.\n\n" + context_claim
        )
    else:
        body = _offscreen_composer_notice(measurement, context_claim)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / f"{_safe_marker_component(key)}.md"
        # A unique temp name per writer: a shared one let two concurrent writers
        # splice their bodies into one published notice (ISSUE-REF).
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            temporary.write_text(body, encoding="utf-8")
            temporary.replace(path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
    except OSError:
        return None
    return path


def _offscreen_composer_notice(measurement: str, context_claim: str) -> str:
    return (
        "AUTO-COMPACT IS WAITING ON A COMPOSER THE PANE DOES NOT SHOW "
        "(reason=no-composer-stalled).\n\n"
        + measurement
        + "What happened: /save-state armed a compact, but for the whole wait the\n"
        "pane's bottom rows held no prompt line Claude Code could be typed into.\n"
        "`frame=cut-at-pane-bottom` means the composer's top rule is the pane's last\n"
        "row: Claude Code drew the prompt line and status below the visible pane,\n"
        "which happens when its idea of the terminal size disagrees with the pane's\n"
        "(compare frame_width with the pane width). The supervisor never types into a\n"
        "composer it cannot read, so it sends nothing.\n\n"
        "WHAT FIXES IT. Resize or re-attach the terminal window showing this session;\n"
        "the resize makes Claude Code repaint at the real size. The attempt is still\n"
        "pending, so the supervisor sends /compact once it sees an empty composer.\n"
        "Or type /compact yourself.\n\n" + context_claim
    )


def clear_retry_notice(key: str, *, attempt_id: str, root: Path | None = None) -> None:
    """Remove this attempt's undelivered retry notice after it no longer retries."""
    path = (
        (root or _root_from_environment())
        / "notices"
        / (f"{_safe_marker_component(key)}.md")
    )
    try:
        body = path.read_text(encoding="utf-8")
        if "AUTO-COMPACT RETRYING" in body and f"attempt_id={attempt_id}" in body:
            path.unlink()
    except OSError:
        pass


# Phases that mean the observation failed, as opposed to declining or finishing.
# Each one must reach the operator on its own, because the lifecycle exits 0 on
# every path by design (it is an observer, so a non-zero exit would be a lie about
# the session) and `poller-health` is opt-in.  See `write_poller_fault_notice`.
POLLER_FAULT_PHASES = frozenset({"abandoned", "crashed", "dead"})
# Phases a poller publishes while it still intends to poll.  A record left in one
# of these by a process that no longer holds its liveness lock is a death, not a
# decline -- that distinction is the whole point of the record.
POLLER_ACTIVE_PHASES = frozenset({"starting", "waiting-for-binding", "observing"})
# Exit reasons that mean "this host cannot run the transport", as opposed to "the
# observation finished".  Kept in sync with :func:`transport_unavailable_reason`.
# The FIFO refusals (ISSUE-REF) are declines too: each already wrote its own
# transport notice, which a fault notice would overwrite in the same file.
FIFO_REFUSAL_REASONS = frozenset(
    {
        "fifo-missing",
        "fifo-unreadable",
        "fifo-not-a-fifo",
        "fifo-wrong-owner",
        "fifo-wrong-mode",
        "fifo-no-reader",
        "fifo-write-error",
        "fifo-idle-unprovable",
    }
)
POLLER_DECLINE_REASONS = (
    frozenset({"no-tmux", "no-pane", "tmux-version", "mod-compaction-disabled"})
    | FIFO_REFUSAL_REASONS
)
# Exit reasons that mean the poller returned with nothing to observe, which is the
# accidental early return ISSUE-REF was filed about -- not a decline and not a
# completed observation.  `attempt-not-unique` is the shape a mis-derived session
# key produces (ISSUE-REF), and it used to exit 0 exactly like a success.
POLLER_ABANDON_REASONS = frozenset(
    {
        "attempt-not-unique",
        "transcript-gone",
        "returned-without-verdict",
        # Hooks said the compact completed; the session's transcript never showed
        # it (ISSUE-REF). Reporting `finished` here is the false confirmation.
        "post-compact-unproved",
        # A mod attempt still BOUND past its request's expiry: the mod ignores an
        # expired request, so nothing would ever claim it (ISSUE-REF).
        "mod-request-expired",
    }
)
# Verdicts that are not a fault.  Everything else must make a health check fail.
POLLER_HEALTHY_VERDICTS = frozenset(
    {"running", "declined", "finished", "event-handoff"}
)


def _poller_slug(key: str, attempt_id: str) -> str:
    return f"{_safe_marker_component(key)}__{_safe_marker_component(attempt_id)}"


def poller_status_path(key: str, attempt_id: str, *, root: Path | None = None) -> Path:
    """Where this poller publishes its phase."""
    directory = (root or _root_from_environment()) / "pollers"
    return directory / f"{_poller_slug(key, attempt_id)}.json"


def poller_liveness_path(
    key: str, attempt_id: str, *, root: Path | None = None
) -> Path:
    """The lock a running poller holds for its whole life, released only by death."""
    return poller_status_path(key, attempt_id, root=root).with_suffix(".lock")


class PollerReporter:
    """Publish a poller's phase so its silence can be read.

    The compact-lifecycle poller exits 0 on every path it takes (and printed
    nothing at all until ISSUE-REF), including two that are not the same event
    at all: a deliberate decline where
    the host has no tmux transport, and an accidental immediate return where the
    attempt record it was launched for is not there.  Neither was distinguishable
    from a poller that had settled into its loop (ISSUE-REF).

    So the record is written **before** the window it reports on, not after it,
    and a terminal phase is written by the process that reached it.  An absent
    terminal phase paired with a released liveness lock is therefore a positive
    statement -- this poller died without deciding anything -- rather than the
    absence of evidence it used to be.
    """

    def __init__(self, key: str, attempt_id: str, *, root: Path | None = None) -> None:
        self.key = key
        self.attempt_id = attempt_id
        self.root = root or _root_from_environment()
        self.path = poller_status_path(key, attempt_id, root=self.root)
        self._phase: str | None = None
        self._reason: str | None = None
        self._started_at = _now()
        self.terminal = False
        # Set when another live poller already owns this key and attempt, in which
        # case every write here would overwrite the incumbent's own phase with a
        # duplicate's.  The duplicate stays silent rather than lying about whose
        # state the record describes.
        self.muted = False

    def phase(self, name: str, *, reason: str | None = None) -> None:
        """Record an intermediate phase, skipping a write that changes nothing."""
        if self._phase == name and self._reason == reason:
            return
        self._write(name, reason)

    def decline(self, reason: str) -> None:
        """Record that this host will never run the transport, and why."""
        self.terminal = True
        self._write("declined", reason)

    def finish(self, reason: str) -> None:
        """Record that the observation ended, and on what evidence.

        A reason in :data:`POLLER_ABANDON_REASONS` is recorded as ``abandoned``
        instead: those exits observed nothing, and calling them a finish is how
        an accidental immediate return came to read as a success.
        """
        self.terminal = True
        phase = "abandoned" if reason in POLLER_ABANDON_REASONS else "finished"
        self._write(phase, reason)

    def abandon(self, reason: str) -> None:
        """Record an exit that observed nothing, whatever its reason's spelling.

        :data:`POLLER_ABANDON_REASONS` can only classify reasons that are fixed
        strings.  An exit naming the attempt state it gave up on is formatted
        (`state=REFUSED`), so it could never be a member, and it reported the
        healthy `finished` instead -- the same defect ISSUE-REF fixed for
        `attempt-not-unique` only.  Callers that know they observed nothing say
        so here rather than relying on their reason being enumerable.
        """
        self.terminal = True
        self._write("abandoned", reason)

    def crash(self, reason: str) -> None:
        """Record an exception the poller saw itself raise."""
        self.terminal = True
        self._write("crashed", reason)

    def _write(self, phase: str, reason: str | None) -> None:
        if self.muted:
            return
        self._phase = phase
        self._reason = reason
        payload = {
            "attempt_id": self.attempt_id,
            "key": self.key,
            "phase": phase,
            "pid": os.getpid(),
            "reason": reason,
            "started_at": self._started_at,
            "updated_at": _now(),
        }
        body = json.dumps(payload, sort_keys=True) + "\n"
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(body, encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            # Best-effort by contract, like ``log_stage``: failing to explain an
            # exit must not become a second, louder failure.
            pass
        # ANNOUNCED HERE, where the phase is DECIDED, and not at the call sites.
        # Three different routes reach a fault phase -- `abandon`, `crash`, and a
        # `finish` whose reason is in POLLER_ABANDON_REASONS -- so a notice written
        # per route is a notice the next route forgets.  Centralising it means no
        # caller can reach a fault phase without the operator being told.
        if phase in POLLER_FAULT_PHASES:
            write_poller_fault_notice(
                self.key,
                phase,
                reason or "unspecified",
                attempt_id=self.attempt_id,
                root=self.root,
            )


@contextlib.contextmanager
def poller_supervision(
    key: str, attempt_id: str, *, root: Path | None = None
) -> Iterator[PollerReporter]:
    """Hold a liveness lock and publish a phase for the duration of a poller.

    The lock is the liveness signal precisely because the kernel releases it --
    a ``SIGKILL``\\ ed poller cannot write "I died", but it cannot keep the lock
    either.  Deliberately not a pid check alone: a recycled pid answers ``kill
    -0`` for an unrelated process, and reading ``/proc/<pid>/cmdline`` or ``ps -o
    args`` to disambiguate is forbidden here because a command line can carry a
    run handle.

    The lock is taken **non-blocking**, and a refusal never changes what the
    poller does: a blocking acquire would park a duplicate poller before it did
    any work, which trades a silent exit for a silent hang.  A refused duplicate
    runs exactly as before and only stops publishing, so the incumbent's record
    keeps describing the process that actually holds the window.
    """
    reporter = PollerReporter(key, attempt_id, root=root)
    lock_path = poller_liveness_path(key, attempt_id, root=reporter.root)
    handle: int | None = None
    try:
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        if not _try_lock_exclusive(handle):
            reporter.muted = True
            os.close(handle)
            handle = None
    except OSError:
        if handle is not None:
            os.close(handle)
            handle = None
    reporter.phase("starting")
    try:
        yield reporter
    except BaseException as error:
        reporter.crash(type(error).__name__)
        raise
    else:
        if not reporter.terminal:
            # A return with no terminal phase is itself a defect worth naming,
            # rather than one indistinguishable from a death.
            reporter.finish("returned-without-verdict")
    finally:
        if handle is not None:
            with contextlib.suppress(OSError):
                _unlock(handle)
            with contextlib.suppress(OSError):
                os.close(handle)


def poller_liveness_held(
    key: str, attempt_id: str, *, root: Path | None = None
) -> bool:
    """Whether some process still holds this poller's liveness lock."""
    path = poller_liveness_path(key, attempt_id, root=root)
    try:
        handle = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        if not _try_lock_exclusive(handle):
            return True
        with contextlib.suppress(OSError):
            _unlock(handle)
        return False
    finally:
        with contextlib.suppress(OSError):
            os.close(handle)


def poller_health(
    key: str, attempt_id: str, *, root: Path | None = None
) -> dict[str, Any]:
    """Classify a poller as running, declined, finished, crashed, dead or absent.

    Every verdict outside :data:`POLLER_HEALTHY_VERDICTS` is a fault, so this can
    fail -- which a check that only ever reported "exited 0" could not.
    """
    for attempt in ResumeStore(root).read_attempts(key):
        if attempt.get("attempt_id") == attempt_id and attempt.get("event_handoff"):
            state = attempt.get("state")
            reason = attempt.get("event_hold") or "awaiting-event-evidence"
            verdict = "event-handoff"
            if state in TERMINAL_STATES:
                verdict = "event-failed"
                reason = attempt.get("reason") or f"state={state}"
                if state == "COMPLETED" and attempt.get("event_completion_proved"):
                    verdict = "finished"
                    reason = "event-completion-proved"
                if attempt.get("wake_state") == "FAILED":
                    verdict = "event-failed"
                    reason = attempt.get("wake_failure") or "wake-failed"
            elif attempt.get("event_handoff") != "intake-written":
                verdict = "event-held"
                reason = attempt["event_handoff"]
            elif attempt.get("event_hold"):
                verdict = "event-held"
            return {
                "attempt_id": attempt_id,
                "key": key,
                "phase": "event-dispatch",
                "state": state,
                "reason": reason,
                "event_hold": attempt.get("event_hold"),
                "verdict": verdict,
            }
    path = poller_status_path(key, attempt_id, root=root)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = None
    if not isinstance(record, dict):
        return {
            "attempt_id": attempt_id,
            "key": key,
            "phase": None,
            "reason": "no-status-record",
            "verdict": "never-launched",
        }
    phase = record.get("phase")
    if phase in POLLER_ACTIVE_PHASES:
        held = poller_liveness_held(key, attempt_id, root=root)
        verdict = "running" if held else "dead"
    elif phase in {"declined", "finished", "crashed", "abandoned"}:
        verdict = phase
    else:
        verdict = "unknown-phase"
    return {
        "attempt_id": record.get("attempt_id", attempt_id),
        "key": record.get("key", key),
        "phase": phase,
        "pid": record.get("pid"),
        "reason": record.get("reason"),
        "started_at": record.get("started_at"),
        "updated_at": record.get("updated_at"),
        "verdict": verdict,
    }


def _latest_attempt_id(key: str, *, root: Path | None = None) -> str | None:
    """The newest attempt id this store holds for ``key``, by arming time."""
    records = [
        record
        for record in ResumeStore(root).read_attempts(key)
        if isinstance(record.get("attempt_id"), str)
    ]
    if not records:
        return None
    newest = max(
        records,
        key=lambda record: (
            record["armed_at"]
            if isinstance(record.get("armed_at"), int)
            and not isinstance(record.get("armed_at"), bool)
            else 0
        ),
    )
    return str(newest["attempt_id"])


def poller_notified_path(
    key: str, attempt_id: str, *, root: Path | None = None
) -> Path:
    """The once-only marker proving this poller's fault has been announced."""
    return poller_status_path(key, attempt_id, root=root).with_suffix(".notified")


def surface_dead_pollers(key: str, *, root: Path | None = None) -> list[str]:
    """Announce every observer for ``key`` that died without reaching a verdict.

    The one fault phase no process can publish about itself is ``dead``: a
    ``SIGKILL``\\ ed observer cannot write "I died", which is exactly why
    :func:`poller_supervision` makes the liveness lock the signal instead.  So an
    active phase with a released lock is a death, and nobody is left to say so.

    This is the sweep that says it, driven by ``UserPromptSubmit`` -- the same
    event that delivers the notice -- so a death surfaces on the next prompt
    without the operator having to think of the question.  Writing is marked
    once-only per attempt, because a notice re-written on every prompt is noise
    that trains the reader to skip the channel.

    Returns the attempt ids newly announced, so a caller can log how many rather
    than inferring it from the notice file.
    """
    directory = (root or _root_from_environment()) / "pollers"
    announced: list[str] = []
    try:
        candidates = sorted(directory.glob(f"{_safe_marker_component(key)}__*.json"))
    except OSError:
        return announced
    for path in candidates:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("phase") not in POLLER_ACTIVE_PHASES:
            continue
        attempt_id = record.get("attempt_id")
        record_key = record.get("key", key)
        if not isinstance(attempt_id, str) or not isinstance(record_key, str):
            continue
        marker = poller_notified_path(record_key, attempt_id, root=root)
        if marker.exists():
            continue
        # Measured LAST, after the cheap filters, because the lock probe is the only
        # part that can race: a poller still in its loop holds the lock and must not
        # be reported dead, and asking as late as possible keeps that window at its
        # narrowest.
        if poller_liveness_held(record_key, attempt_id, root=root):
            continue
        log_stage(
            "CC-POLLER-DEAD",
            record_key,
            f"attempt={attempt_id} phase={record.get('phase')}",
        )
        write_poller_fault_notice(
            record_key,
            "dead",
            "poller-died-without-verdict",
            attempt_id=attempt_id,
            root=root,
        )
        with contextlib.suppress(OSError):
            marker.write_text(f"{_now()}\n", encoding="utf-8")
        announced.append(attempt_id)
    return announced


@dataclass(frozen=True)
class HistoryRecord:
    resume_command: str
    command_digest: str
    outbox_path: str


@dataclass(frozen=True)
class ComposerSample:
    """One current-viewport composer classification and its bounded evidence."""

    classification: str
    region: str
    composer_recognized: bool
    content: str | None = None
    error_reason: str | None = None
    claude_code_notice: str | None = None
    attribute_class: str = "unknown"
    raw_content_debug: str | None = None


@dataclass(frozen=True)
class WindowGrow:
    """What a temporary window grow changed, so it can be put back exactly."""

    pane_id: str
    original_rows: int
    # The window-LOCAL `window-size`, or None when the window inherited it.
    window_size: str | None


class ResumeStore:
    """Small file ledger; all state transitions happen while its lock is held."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _root_from_environment()
        self._wake_not_ready_logged_at: dict[tuple[str, str, str], int] = {}

    @property
    def attempts(self) -> Path:
        return self.root / "attempts"

    def initialise(self) -> None:
        for directory in (
            self.root,
            wake_directory(self.root),
            self.attempts,
            self.root / "history",
            self.root / "outbox",
            self.root / "consumed",
            self.root / "disarmed",
            self.root / "sensing",
            self.root / "debug",
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _session_file(self, key: str, group: str, name: str, legacy: Path) -> Path:
        from ai_chassis.wake import sessions

        if not sessions.event_layout(sessions.mail_root(), key):
            return legacy
        path = sessions.directory(self.root, key) / group / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        migrated = path.with_name(f".{path.name}.migrated")
        if not path.exists() and not migrated.exists() and legacy.is_file():
            # Carry forward a consumed capability or safety latch. Never remove
            # the old copy: an unselected host can still serve legacy delivery.
            data = legacy.read_bytes()
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                migrated.touch(mode=0o600, exist_ok=True)
        return path

    def _active_save_state_path(self, key: str) -> Path:
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._session_file(
            key,
            "sensing",
            "save-state-active",
            self.root / "sensing" / f"{key_digest}.save-state-active",
        )

    def _idle_prompt_path(self, key: str) -> Path:
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._session_file(
            key,
            "sensing",
            "idle-prompt",
            self.root / "sensing" / f"{key_digest}.idle-prompt",
        )

    def _budget_reset_path(self, key: str) -> Path:
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._session_file(
            key,
            "sensing",
            "budget-reset",
            self.root / "sensing" / f"{key_digest}.budget-reset",
        )

    def mark_idle_prompt_confirmed(self, key: str) -> None:
        """Record Claude Code's own idle_prompt lifecycle confirmation."""
        with self.locked():
            path = self._idle_prompt_path(key)
            path.write_text(f"{_now()}\n", encoding="utf-8")
            os.chmod(path, 0o600)

    def idle_prompt_confirmed_recently(self, key: str, window: int) -> bool:
        """Return whether idle_prompt confirmation remains valid for this key."""
        with self.locked():
            return self._idle_prompt_confirmed_recently_locked(key, window)

    def _idle_prompt_confirmed_recently_locked(self, key: str, window: int) -> bool:
        path = self._idle_prompt_path(key)
        try:
            confirmed_at = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            return False
        age = _now() - confirmed_at
        if age < 0 or age > window:
            path.unlink(missing_ok=True)
            return False
        return True

    def _clear_idle_prompt_confirmed_locked(self, key: str) -> None:
        self._idle_prompt_path(key).unlink(missing_ok=True)

    def mark_save_state_active(self, key: str) -> None:
        """Allow the save-state skill and its follow-on compact start to finish."""
        with self.locked():
            path = self._active_save_state_path(key)
            path.write_text(f"{_now()}\n", encoding="utf-8")
            os.chmod(path, 0o600)

    def save_state_active(self, key: str) -> bool:
        """Return whether a non-expired save-state grace marker exists."""
        try:
            timeout = int(
                os.environ.get(
                    "AIH_SAVE_STATE_ACTIVE_TIMEOUT_SECONDS",
                    str(_DEFAULT_SAVE_STATE_ACTIVE_TIMEOUT_SECONDS),
                )
            )
        except ValueError:
            return False
        if timeout < 0:
            return False
        with self.locked():
            return self._save_state_active_locked(key, timeout)

    def _save_state_active_locked(self, key: str, timeout: int) -> bool:
        path = self._active_save_state_path(key)
        try:
            marked_at = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            return False
        age = _now() - marked_at
        if age < 0 or age > timeout:
            path.unlink(missing_ok=True)
            return False
        return True

    def clear_save_state_active(self, key: str) -> None:
        with self.locked():
            self._clear_save_state_active_locked(key)

    def _clear_save_state_active_locked(self, key: str) -> None:
        self._active_save_state_path(key).unlink(missing_ok=True)

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.initialise()
        lock = self.root / "supervisor.lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock_exclusive(fd)
            yield
        finally:
            _unlock(fd)
            os.close(fd)

    def _attempt_path(self, key: str, attempt_id: str) -> Path:
        from ai_chassis.wake import sessions

        if sessions.event_layout(sessions.mail_root(), key):
            return (
                sessions.directory(self.root, key)
                / "attempts"
                / f"{_safe_marker_component(attempt_id)}.json"
            )
        return self.attempts / f"{key}-{attempt_id}.json"

    def write_attempt(self, record: dict[str, Any]) -> Path:
        path = self._attempt_path(str(record["key"]), str(record["attempt_id"]))
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        return path

    def read_attempts(self, key: str) -> list[dict[str, Any]]:
        from ai_chassis.wake import sessions

        records: list[dict[str, Any]] = []
        paths = list(self.attempts.glob(f"{glob.escape(key)}-*.json"))
        if sessions.event_layout(sessions.mail_root(), key):
            paths.extend(
                (sessions.directory(self.root, key) / "attempts").glob("*.json")
            )
        by_id = {}
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("key") == key:
                    value["_path"] = str(path)
                    by_id[value.get("attempt_id")] = value
            except (OSError, json.JSONDecodeError):
                continue
        records = list(by_id.values())
        return sorted(
            records, key=lambda item: str(item.get("armed_at", "")), reverse=True
        )

    def keys(self) -> list[str]:
        """Every session key this store holds an attempt for.

        The filename is `{key}-{attempt_id}.json` and a key may itself contain `-`,
        so the key is read from the record body; parsing it back out of the name
        would split `wt-foo-1-a1b2c3d4` in the wrong place.
        """
        found: set[str] = set()
        for path in self.attempts.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and isinstance(value.get("key"), str):
                found.add(str(value["key"]))
        return sorted(found)

    def claim_reauth_wake(self, key: str, window: str) -> bool:
        """Win the one re-auth wake this key gets for this dead window.

        `O_CREAT | O_EXCL` so two sentinel ticks racing over the same window cannot
        both send. Deliberately NOT routed through `permitted()`: that budget is
        shared with the compact and continuation legs and `disarm()`s the whole key
        when it runs out, so charging a wake against it would silently cost the
        session its next compact.
        """
        path = (
            self.root
            / "consumed"
            / f"reauth-wake-{_safe_marker_component(key)}-{window}"
        )
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        except OSError:
            return False
        os.close(handle)
        return True

    def update(
        self, record: dict[str, Any], state: str, **extra: Any
    ) -> dict[str, Any]:
        record = {key: value for key, value in record.items() if key != "_path"}
        record["state"] = state
        record.update(extra)
        self.write_attempt(record)
        if state in TERMINAL_STATES and record.get("transport") == "mod":
            _remove_mod_request(record)
        return record

    def pending(self, key: str, attempt_type: str) -> list[dict[str, Any]]:
        states = COMPACT_STATES if attempt_type == "compact" else CONTINUATION_STATES
        return [
            record
            for record in self.read_attempts(key)
            if record.get("attempt_type") == attempt_type
            and record.get("state") in states - TERMINAL_STATES
        ]

    def disarmed(self, key: str) -> bool:
        """Is this key still latched? A marker past DISARM_MAX_AGE_SECONDS is not.

        Existence alone WAS the bug (ISSUE-REF). `disarm()` has always stamped the
        marker with `_now()`, and nothing ever read that stamp back, so a single
        transient failure latched a key for good: measured at six keys, seven
        days, and the only symptom was one `CC-REFUSED` line per session.

        Staleness is decided here and nowhere else. `permitted()` used to ask the
        same question twice -- once through this method and once through
        `disarm_reason()`, joined by `or` -- so a fix applied to one reader would
        have been silently inert. That second reader is now log-only.

        Fails closed: a marker present but undateable still suppresses, because
        "I cannot tell how old this latch is" is not "this latch has expired".
        """
        if not self._disarm_path(key).exists():
            return False
        stamp = self._disarm_stamp(key)
        if stamp is None or _now() - stamp <= DISARM_MAX_AGE_SECONDS:
            return True
        self._expire_disarm(key, stamp)
        return False

    def _disarm_stamp(self, key: str) -> int | None:
        """The epoch `disarm()` wrote, or None when absent or unparseable."""
        try:
            raw = self._disarm_path(key).read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            return int(raw.split(maxsplit=1)[0])
        except (IndexError, ValueError):
            return None

    def _expire_disarm(self, key: str, stamp: int) -> None:
        """Drop a latch that has outlived its episode, and say so in the log."""
        reason = self.disarm_reason(key) or "unknown"
        try:
            self._disarm_path(key).unlink(missing_ok=True)
        except OSError:
            return
        log_stage(
            "CC-REARM",
            key,
            f"reason={reason} source=expiry age_seconds={_now() - stamp}",
        )

    def _disarm_path(self, key: str) -> Path:
        return self._session_file(
            key, "sensing", "disarmed", self.root / "disarmed" / key
        )

    def disarm_reason(self, key: str) -> str | None:
        try:
            _, reason = (
                self._disarm_path(key).read_text(encoding="utf-8").split(maxsplit=1)
            )
        except (OSError, ValueError):
            return None
        return reason.strip() or None

    def disarm(self, key: str, reason: str) -> None:
        # Atomic, because `disarmed()` fails closed on an undateable marker: an
        # empty file left by a torn write would latch the key for good (ISSUE-REF).
        path = self._disarm_path(key)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(f"{_now()} {reason}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def _budget_reset_attempt_ids(self, key: str) -> set[str]:
        try:
            value = json.loads(self._budget_reset_path(key).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            return set()
        return set(value)

    def _reset_save_state_budget_locked(self, key: str) -> None:
        reset_attempt_ids = self._budget_reset_attempt_ids(key)
        reset_attempt_ids.update(
            str(record.get("attempt_id"))
            for record in self.read_attempts(key)
            if record.get("sent_at") or record.get("state") == "REFUSED"
        )
        path = self._budget_reset_path(key)
        path.write_text(json.dumps(sorted(reset_attempt_ids)) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)

    def reset_save_state_disarm(self, key: str) -> bool:
        """Start a fresh save-state budget epoch and clear resettable disarms."""
        with self.locked():
            reason = self.disarm_reason(key)
            disarmed = self.disarmed(key)
            if disarmed and reason not in _SAVE_STATE_RESETTABLE_DISARM_REASONS:
                return False
            if not disarmed or reason in _SAVE_STATE_BUDGET_RESET_DISARM_REASONS:
                self._reset_save_state_budget_locked(key)
            if not disarmed:
                return False
            self._disarm_path(key).unlink()
        log_stage("CC-REARM", key, f"reason={reason} source=save-state")
        return True

    def consume(self, record: dict[str, Any]) -> None:
        self._consumed_path(record).write_text("consumed\n", encoding="utf-8")

    def _consumed_path(self, record: dict[str, Any]) -> Path:
        attempt = str(record["attempt_id"])
        return self._session_file(
            str(record["key"]), "consumed", attempt, self.root / "consumed" / attempt
        )

    def consumed(self, record: dict[str, Any]) -> bool:
        return self._consumed_path(record).exists()

    def unconsume(self, record: dict[str, Any]) -> None:
        """Clear a mod claim's marker when its attempt returns to BOUND (ISSUE-REF)."""
        with contextlib.suppress(FileNotFoundError):
            self._consumed_path(record).unlink()

    def _wake_consumed_path(self, attempt_id: str, key: str | None = None) -> Path:
        """Keep post-compact wake consumption separate from compact's capability nonce."""
        legacy = self.root / "consumed" / f"wake-{attempt_id}"
        return (
            self._session_file(key, "consumed", f"wake-{attempt_id}", legacy)
            if key
            else legacy
        )

    def _wake_marker_path(self, key: str, attempt_id: str) -> Path:
        return _wake_ready_marker_path(self.root, key, attempt_id)

    def _log_wake_not_ready(
        self,
        key: str,
        attempt_id: str,
        reason: str,
        record: dict[str, Any] | None,
        *,
        interval_seconds: int | None,
    ) -> None:
        now = _now()
        log_key = (key, attempt_id, reason)
        if interval_seconds is not None:
            previous = self._wake_not_ready_logged_at.get(log_key)
            if previous is not None and now - previous < interval_seconds:
                return
            self._wake_not_ready_logged_at[log_key] = now
        started_at = record.get("session_start_at") if record is not None else None
        elapsed = (
            str(max(0, now - started_at))
            if isinstance(started_at, int) and not isinstance(started_at, bool)
            else "unknown"
        )
        log_stage(
            "CC-WAKE-NOOP",
            key,
            f"attempt={attempt_id} reason={reason} elapsed_s={elapsed}",
        )

    def claim_wake(
        self,
        key: str,
        attempt_id: str,
        claimant: str,
        *,
        not_ready_log_interval_seconds: int | None = None,
        refuse_manual_completion: bool = False,
        marker_path: Path | None = None,
    ) -> bool:
        """Claim exactly one PostCompact-validated wake marker under the store lock.

        ``refuse_manual_completion`` declines the claim when a human's own
        ``/compact`` already carried this attempt to ``COMPLETED`` (ISSUE-REF).
        It is checked here, inside the lock, and not by the caller: the whole
        window this exists to cover is a record that changes between a caller's
        read and this claim, so a caller-side test of the same condition is a
        TOCTOU that measurably lets the claim through anyway.
        """
        with self.locked():
            record = next(
                (
                    item
                    for item in self.read_attempts(key)
                    if item.get("attempt_id") == attempt_id
                ),
                None,
            )
            marker = self._wake_marker_path(key, attempt_id)
            if marker_path is not None:
                from ai_chassis.wake import sessions

                allowed = sessions.context_watch_paths(
                    self.root,
                    key,
                    (record or {}).get("session_id")
                    or (record or {}).get("transcript_session_id"),
                )
                if not any(
                    marker_path.resolve() == (path / marker.name).resolve()
                    for path in allowed
                ):
                    return False
                marker = marker_path
            if record is None:
                self._log_wake_not_ready(
                    key,
                    attempt_id,
                    "not-ready:no-record",
                    record,
                    interval_seconds=not_ready_log_interval_seconds,
                )
                return False
            if record.get("wake_state") == "CLAIMED":
                log_stage(
                    "CC-WAKE-NOOP", key, f"attempt={attempt_id} reason=already-claimed"
                )
                return False
            from ai_chassis.wake import sessions

            if sessions.enabled():
                from ai_chassis.wake.handlers.compact import completion_proved

                if not completion_proved(record, record.get("transcript_path") or ""):
                    self._log_wake_not_ready(
                        key,
                        attempt_id,
                        "not-ready:compact-boundary-unproved",
                        record,
                        interval_seconds=not_ready_log_interval_seconds,
                    )
                    return False
            if refuse_manual_completion and _manual_compact_completed(record):
                log_stage(
                    "CC-WAKE-NOOP",
                    key,
                    f"attempt={attempt_id} reason=manual-compact-completed "
                    f"claimant={claimant}",
                )
                return False
            if not marker.is_file():
                self._log_wake_not_ready(
                    key,
                    attempt_id,
                    "not-ready:marker-missing",
                    record,
                    interval_seconds=not_ready_log_interval_seconds,
                )
                return False
            try:
                values = dict(
                    line.split("=", 1)
                    for line in marker.read_text(encoding="utf-8").splitlines()
                    if "=" in line
                )
            except OSError:
                log_stage(
                    "CC-WAKE-NOOP", key, f"attempt={attempt_id} reason=unreadable"
                )
                return False
            expected_digest = record.get("resume_artifact_digest")
            if (
                values.get("key") != key
                or values.get("attempt_id") != attempt_id
                or values.get("digest") != expected_digest
                or record.get("wake_state") != "READY"
                or self._wake_consumed_path(attempt_id, key).exists()
            ):
                log_stage(
                    "CC-WAKE-NOOP", key, f"attempt={attempt_id} reason=already-claimed"
                )
                return False
            consumed = self._wake_consumed_path(attempt_id, key)
            consumed.write_text(f"claimant={claimant}\n", encoding="utf-8")
            os.chmod(consumed, 0o600)
            self.update(
                record,
                str(record["state"]),
                wake_state="CLAIMED",
                wake_claimant=claimant,
                wake_claimed_at=_now(),
            )
            marker.unlink(missing_ok=True)
        log_stage("CC-WAKE-CLAIM", key, f"attempt={attempt_id} claimant={claimant}")
        # ISSUE-REF AC-2: a claimed wake starts this delivery's ack-lateness interval.
        # Outside the lock deliberately -- it forks a shell, and nothing in the stamp
        # needs the attempt record.
        _mark_first_turn_via_resume_library(key)
        return True

    def wake_state(self, key: str, attempt_id: str) -> str | None:
        with self.locked():
            record = next(
                (
                    item
                    for item in self.read_attempts(key)
                    if item.get("attempt_id") == attempt_id
                ),
                None,
            )
            value = record.get("wake_state") if record is not None else None
            return value if isinstance(value, str) else None

    def fail_wake(self, key: str, attempt_id: str, reason: str) -> None:
        with self.locked():
            record = next(
                (
                    item
                    for item in self.read_attempts(key)
                    if item.get("attempt_id") == attempt_id
                ),
                None,
            )
            if record is None or record.get("wake_state") == "CLAIMED":
                return
            self.update(
                record,
                str(record["state"]),
                wake_state="FAILED",
                wake_failure=reason,
                wake_failed_at=_now(),
            )

    def permitted(self, key: str, attempt_type: str) -> bool:
        """Bound the event nonce, debounce, wake budget, and per-type budget."""
        # `disarmed()` is the single authority on whether the latch still holds;
        # the reason is read only to label the log line. These were previously
        # joined by `or`, which meant an age-blind `disarm_reason()` could refuse
        # on its own and no expiry in `disarmed()` could ever take effect.
        if self.disarmed(key):
            log_stage(
                "CC-REFUSED",
                key,
                f"reason=disarmed disarm_reason={self.disarm_reason(key) or 'unknown'}",
            )
            return False
        records = self.read_attempts(key)
        reset_attempt_ids = self._budget_reset_attempt_ids(key)
        sent = [record for record in records if record.get("sent_at")]
        charged = [
            record
            for record in records
            if (record.get("sent_at") or record.get("state") == "REFUSED")
            and str(record.get("attempt_id")) not in reset_attempt_ids
        ]
        try:
            debounce = int(os.environ.get("AIH_INJECT_DEBOUNCE_SECONDS", "60"))
            wake_budget = int(os.environ.get("AIH_INJECT_WAKE_BUDGET", "2"))
            compact_budget = int(os.environ.get("AIH_INJECT_COMPACT_BUDGET", "1"))
            cont_budget = int(os.environ.get("AIH_INJECT_CONT_BUDGET", "1"))
        except ValueError:
            return False
        if len(charged) >= wake_budget:
            self.disarm(key, "wake-budget")
            log_stage("CC-DISARM", key, "reason=wake-budget")
            return False
        if any(
            record.get("attempt_type") == attempt_type
            and _now() - int(record["sent_at"]) < debounce
            for record in sent
        ):
            return False
        used = sum(record.get("attempt_type") == attempt_type for record in charged)
        if used >= (compact_budget if attempt_type == "compact" else cont_budget):
            self.disarm(key, f"{attempt_type}-budget")
            log_stage("CC-DISARM", key, f"reason={attempt_type}-budget")
            return False
        return True

    def latest_history(self, key: str) -> HistoryRecord | None:
        path = self.root / "history" / f"{key}.jsonl"
        outbox = self.root / "outbox" / f"{key}.md"
        if not path.is_file():
            return None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            try:
                value = json.loads(line)
                command = value["resume_command"]
                recorded_path = value["outbox_path"]
                recorded_digest = value["command_digest"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if (
                isinstance(command, str)
                and isinstance(recorded_digest, str)
                and recorded_path == str(outbox)
            ):
                candidates = [outbox]
                candidates.extend(
                    (self.root / "injected").glob(f"{glob.escape(key)}-*.md")
                )
                for candidate in candidates:
                    try:
                        if digest(candidate.read_bytes()) == recorded_digest:
                            return HistoryRecord(
                                command, recorded_digest, recorded_path
                            )
                    except OSError:
                        continue
        return None


def _fsync_directory(directory: Path) -> None:
    """Flush ``directory``'s own metadata so a rename into it survives a crash.

    Skipped where the platform has no directory descriptors. Windows has none:
    ``os.open`` on a directory raises ``PermissionError``, and the stdlib exposes
    no directory-flush primitive to substitute, so there is genuinely nothing to
    call. Raising instead aborted the publication *after* the artifact had
    already been renamed into place, which is how ``/save-state`` lost its resume
    artifact outright on example-client-host-windows (ISSUE-REF) -- a durability call taking down
    the durability it exists to provide.

    **A rename here is therefore only as durable as the filesystem's own metadata
    journaling on Windows**, which is weaker than what this call buys on POSIX.
    That gap is the platform's, not a shortcut: closing it would need a
    ``CreateFile``/``FlushFileBuffers`` pair with ``FILE_FLAG_BACKUP_SEMANTICS``
    through ``ctypes``, which is not reachable from the stdlib.

    Gated on ``os.name`` rather than on a probe of ``os.O_DIRECTORY``, unlike the
    fcntl/msvcrt probe in ``_lock_exclusive`` above: there is no Windows module to
    probe for this, and a missing ``O_DIRECTORY`` on some Unix would silently
    discard the fsync on a platform that does guarantee it. Failing loudly is
    unavailable, so the gate must not be able to over-match.
    """
    if not _DIRECTORY_DESCRIPTORS_EXIST:
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append_history_after_outbox(
    *, root: Path, key: str, session_id: str, resume_command: str, outbox_path: Path
) -> str:
    """Append a durable history record only after the outbox rename is committed."""
    data = outbox_path.read_bytes()
    record = {
        "event_id": secrets.token_hex(16),
        "ts": _now(),
        "session_id": session_id,
        "resume_command": resume_command,
        "command_digest": digest(data),
        "outbox_path": str(outbox_path),
    }
    line = json.dumps(record, sort_keys=True, separators=(",", ":"))
    history = root / "history" / f"{key}.jsonl"
    history.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with history.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    _trim_history(history)
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def publish_resume_from_stdin(key: str, session_id: str) -> int:
    """Commit an outbox artifact, then its corresponding history record.

    This command is used by the shell writer so the crash-consistency boundary is
    implemented in one place with real fsync calls rather than shell redirections.
    """
    command = os.environ.get("AIH_RESUME_COMMAND", "Resume: /auto-start")
    if (
        not command.startswith("Resume: /auto-start")
        or "\n" in command
        or "\r" in command
    ):
        return 2
    root = _root_from_environment()
    outbox = root / "outbox" / f"{key}.md"
    outbox.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = outbox.with_name(f".{outbox.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(sys.stdin.buffer.read())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, outbox)
        _fsync_directory(outbox.parent)
        history_marker = append_history_after_outbox(
            root=root,
            key=key,
            session_id=session_id,
            resume_command=command,
            outbox_path=outbox,
        )
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    attempt = reserve_save_state_compact(
        key, session_id, outbox, history_marker=history_marker
    )
    published: dict[str, str] = {
        "attempt_id": attempt["attempt_id"],
        "artifact_digest": digest(outbox.read_bytes()),
        "completion_nonce": attempt["completion_nonce"],
        "outbox_path": str(outbox),
    }
    # The publication succeeding says nothing about whether anything will act on it.
    # This is the leg that cost an operator real time (ISSUE-REF): a clean success,
    # a READY attempt, and no output anywhere naming the disabled wake -- so the
    # session was left holding an injected resume message nobody was going to submit.
    #
    # The notice rides INSIDE this object rather than on a line of its own, because
    # both of the wrapper's channels are already spoken for and `tests/hooks/
    # test_hooks.sh` pins both: `save_resume_result_matches` runs `json.load` over
    # the whole of stdout, and two of its cases assert the success path's stderr is
    # EMPTY. An extra key is the one addition neither contract can see -- the
    # matcher reads four fields by name and tolerates the rest.
    if not _post_compact_typed_wake_enabled():
        published["post_compact_wake_notice"] = POST_COMPACT_WAKE_DISABLED_NOTICE
    print(json.dumps(published, sort_keys=True))
    return 0


def print_resume_command(key: str) -> int:
    """Print the digest-matched persisted command for a current outbox artifact."""
    record = ResumeStore().latest_history(key)
    if record is None:
        return 1
    print(record.resume_command)
    return 0


def reserve_save_state_compact(
    key: str,
    session_id: str,
    outbox_path: Path,
    *,
    history_marker: str | None = None,
) -> dict[str, str]:
    """Reserve the one-time completion capability before the skill responds."""
    store = ResumeStore()
    # Terminalise an overdue SENT/STARTED attempt BEFORE the disarm reset, so the
    # reset clears the latch it writes (ISSUE-REF). A submitted compact that never
    # began -- an echo a human cancelled -- is SENT with `sent_at` set, so the
    # supersede loop below cannot see it; left pending it makes PreCompact refuse
    # both it and this attempt, and its timeout disarm would otherwise outlive the
    # reset and refuse the resume continuation of the compaction it never was.
    with store.locked():
        reconcile_timeouts(store, key, "compact")
    # Publication follows a durable explicit /save-state history append.  Start
    # its rate-limit epoch here, before the compact's SessionStart continuation
    # can run; waiting for the later Stop pickup leaves lifetime charges able to
    # disarm that continuation first.
    store.reset_save_state_disarm(key)
    completion_nonce = secrets.token_hex(16)
    attempt_id = secrets.token_hex(16)
    with store.locked():
        # A newer explicit save-state supersedes every earlier one whose compact is
        # not proven accepted.  Keeping it terminal ensures the later Stop event and
        # every send evaluation see exactly one candidate.  QUEUED is included even
        # though its `/compact` was submitted: Claude Code can drain a queued slash
        # command without running it (19 of 24 `popAll`-drained `/compact`s on this
        # host never compacted), and a QUEUED survivor made `send_armed_compact` see
        # two candidates and return silently on every tick (ISSUE-REF).  If the
        # queued command does still run, PreCompact attributes it to the one live
        # attempt, which carries the newer artifact.
        superseded_staging_attempt_ids: list[str] = []
        for previous in store.pending(key, "compact"):
            if previous.get("origin") == "save-state" and (
                not previous.get("sent_at") or previous.get("state") == "QUEUED"
            ):
                staged_attempt_id = previous.get("attempt_id")
                if (
                    isinstance(staged_attempt_id, str)
                    and not previous.get("sent_at")
                    and isinstance(previous.get("last_staged_text"), str)
                    and isinstance(previous.get("stagings"), int)
                    and not isinstance(previous.get("stagings"), bool)
                    and previous["stagings"] > 0
                ):
                    superseded_staging_attempt_ids.append(staged_attempt_id)
                state_before = previous.get("state")
                store.update(
                    previous,
                    "REFUSED",
                    reason="superseded-save-state",
                    terminal_at=_now(),
                )
                log_stage(
                    "CC-SUPERSEDE",
                    key,
                    f"attempt={previous.get('attempt_id')} by={attempt_id} "
                    f"state_before={state_before}",
                )
        try:
            artifact_digest = digest(outbox_path.read_bytes())
        except OSError as error:
            raise RuntimeError("resume artifact unavailable") from error
        record: dict[str, Any] = {
            "attempt_id": attempt_id,
            "attempt_type": "compact",
            "origin": "save-state",
            "key": key,
            "session_id": session_id,
            "history_marker": history_marker,
            "resume_artifact_digest": artifact_digest,
            "completion_nonce_digest": digest(completion_nonce),
            "pane_id": None,
            "pane_fingerprint": None,
            "armed_at": _now(),
            "sent_at": None,
            "landed_at": None,
            "stagings": 0,
            "submissions": 0,
            "backoff_seconds": 0,
            "last_staged_text": None,
            "superseded_staging_attempt_ids": superseded_staging_attempt_ids,
            "state": "ARMED",
            "evidence_path": None,
        }
        store.write_attempt(record)
    log_stage("CC-ARM", key, f"attempt={attempt_id} origin=save-state")
    return {
        "attempt_id": attempt_id,
        "completion_nonce": completion_nonce,
        "outbox_path": str(outbox_path),
    }


def _safe_marker_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def _wake_ready_marker_path(root: Path, key: str, attempt_id: str) -> Path:
    return wake_directory(root, key) / (
        f"wake-ready-{_safe_marker_component(key)}-"
        f"{_safe_marker_component(attempt_id)}.marker"
    )


def _manual_compact_completed(record: dict[str, Any]) -> bool:
    """Return whether a human's own ``/compact`` already completed this attempt."""
    return (
        record.get("state") == "COMPLETED" and record.get("compact_source") == "manual"
    )


def _write_wake_ready_marker(
    store: ResumeStore, record: dict[str, Any], artifact_digest: str
) -> Path:
    key = str(record["key"])
    from ai_chassis.wake import sessions

    if sessions.event_layout(sessions.mail_root(), key) and not sessions.registered(
        sessions.mail_root(),
        key,
        record.get("session_id") or record.get("transcript_session_id"),
    ):
        raise ValueError("watch-registration-pending")
    attempt_id = str(record["attempt_id"])
    marker = _wake_ready_marker_path(store.root, key, attempt_id)
    marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = marker.with_name(f".{marker.name}.{secrets.token_hex(4)}.tmp")
    contents = f"key={key}\nattempt_id={attempt_id}\ndigest={artifact_digest}\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(contents)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, marker)
    _fsync_directory(marker.parent)
    return marker


def _matching_injected_artifact(
    store: ResumeStore, record: dict[str, Any]
) -> tuple[Path | None, str | None]:
    """Find the sole injected packet whose bytes match the attempt's digest."""
    key = record.get("key")
    expected = record.get("resume_artifact_digest")
    if not isinstance(key, str) or not isinstance(expected, str):
        return None, "missing-artifact-digest"
    artifacts = []
    for artifact in (store.root / "injected").glob(f"{glob.escape(key)}-*.md"):
        try:
            if digest(artifact.read_bytes()) == expected:
                artifacts.append(artifact)
        except OSError:
            continue
    if len(artifacts) != 1:
        return None, "matching-injected-artifact-not-found"
    return artifacts[0], None


def _slug(path: str) -> str:
    """CC's own project-directory slug transform: every non-alnum byte -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


_SESSION_ID_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _resolve_own_transcript_path(session_id: str | None = None) -> Path | None:
    """Best-effort: the newest transcript JSONL under this cwd's project slug dir.

    Two sessions sharing a cwd share that directory, so "newest" can be a
    sibling's transcript (ISSUE-REF). A caller that knows its own
    ``session_id`` gets ``<session_id>.jsonl`` there or nothing, never a sibling.
    """
    project_dir = Path.home() / ".claude" / "projects" / _slug(str(Path.cwd()))
    if not project_dir.is_dir():
        return None
    if session_id is not None:
        if not _SESSION_ID_FILENAME.fullmatch(session_id):
            return None
        own = project_dir / f"{session_id}.jsonl"
        return own if own.is_file() else None
    candidates = sorted(
        project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


def _own_transcript(
    pinned: object, session_id: object = None
) -> tuple[Path | None, str]:
    """The transcript a legacy leg reads, and its ``transcript_source``.

    ``hook-payload`` when the caller holds the path a hook payload carried (the
    attempt record's ``transcript_path`` is written from one), ``session-record``
    when only the session id is known, and ``cwd-newest`` as the last resort --
    the library's ``SessionTarget`` vocabulary (ISSUE-REF).
    """
    if isinstance(pinned, (str, Path)) and str(pinned) and Path(pinned).is_absolute():
        return Path(pinned), "hook-payload"
    if isinstance(session_id, str) and session_id:
        return _resolve_own_transcript_path(session_id), "session-record"
    return _resolve_own_transcript_path(), "cwd-newest"


def _scan_transcript_for_wake_signals(path: Path, offset: int) -> tuple[int, bool, int]:
    """Return (new_offset, saw_sessionstart_compact, new_compact_command_count)."""
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
            new_offset = handle.tell()
    except OSError:
        return offset, False, 0
    saw_sessionstart = False
    compact_commands = 0
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") == "user":
            content = record.get("message", {}).get("content")
            if isinstance(content, str) and content.strip() == "/compact":
                compact_commands += 1
            continue
        attachment = record.get("attachment")
        if (
            isinstance(attachment, dict)
            and attachment.get("type") == "hook_success"
            and attachment.get("hookName") == "SessionStart:compact"
        ):
            saw_sessionstart = True
        if (
            isinstance(attachment, dict)
            and attachment.get("type") == "compact_boundary"
        ):
            saw_sessionstart = True
    return new_offset, saw_sessionstart, compact_commands


def _scan_transcript_for_goal_state(
    path: Path, offset: int, goal_active: str
) -> tuple[int, str]:
    """Return the latest observed native-goal state from transcript attachments.

    ``goal_status`` is an observed Claude Code transcript attachment, not a
    supported schema. Its boolean ``met`` is the only current-state signal this
    supervisor can cheaply observe: an unmet goal remains active; a met goal no
    longer does. Missing or malformed markers deliberately remain ``unknown``.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
            new_offset = handle.tell()
    except OSError:
        return offset, "unknown"
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        attachment = record.get("attachment")
        if not isinstance(attachment, dict) or attachment.get("type") != "goal_status":
            continue
        met = attachment.get("met")
        goal_active = "false" if met is True else "true" if met is False else "unknown"
    return new_offset, goal_active


_DEFAULT_TRANSCRIPT_TAIL_BYTES = 8 * 1024 * 1024
_DEFAULT_COMPACT_BOUNDARY_WAIT_SECONDS = 60.0
_COMPACT_BOUNDARY_CLOCK_SLACK_SECONDS = 5


def _transcript_tail_bytes() -> int:
    """How much of the transcript's end a readiness check is allowed to read."""
    value = _bounded_environment_number(
        "AIH_TRANSCRIPT_TAIL_BYTES",
        float(_DEFAULT_TRANSCRIPT_TAIL_BYTES),
        1024,
        1024 * 1024 * 1024,
    )
    return int(value) if value is not None else _DEFAULT_TRANSCRIPT_TAIL_BYTES


def _transcript_records(
    path: Path,
    *,
    failure: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], os.stat_result] | None:
    """Read a bounded transcript tail; malformed input inside it supplies no proof.

    This reads only the last ``_transcript_tail_bytes()`` because a quiet-turn
    boundary is a property of the transcript's END, and reading the whole file made
    two unrelated failures inevitable (ISSUE-REF):

    * It refused the ENTIRE file on the first unparseable line. ISSUE-REF's transcript
      held 206,174 records over 588 MiB with exactly ONE malformed line, number
      2324 -- an interleaved write surrounded by valid records. An append-only file
      never repairs that, so every readiness poll returned "no proof" permanently
      and the supervisor never typed ``/compact`` at all.
    * It parsed 588 MiB every 30 seconds, and that cost grows for the whole life of
      a session.

    Malformed input INSIDE the window still refuses, so the original soundness rule
    is intact exactly where it can still mean something. A malformed line only ever
    leaves the window by being pushed out of it, which is bounded, not permanent.
    """
    window = _transcript_tail_bytes()
    try:
        stat = path.stat()
        with path.open("rb") as handle:
            if stat.st_size > window:
                handle.seek(stat.st_size - window)
                handle.readline()  # the window cut a line in half; it is not ours
            data = handle.read()
    except OSError:
        if failure is not None:
            failure["reason"] = "unreadable-or-missing"
        return None
    if not data.endswith(b"\n"):
        # Claude Code appends while this reads, so the last line is routinely half
        # written. An incomplete line is not a record yet -- the previous complete
        # one is the transcript's current end.
        data = data[: data.rfind(b"\n") + 1]
    records: list[dict[str, Any]] = []
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            if failure is not None:
                failure["reason"] = "malformed-line-in-tail"
            return None
        if not isinstance(record, dict):
            if failure is not None:
                failure["reason"] = "malformed-line-in-tail"
            return None
        records.append(record)
    return records, stat


# Rows Claude Code appends between turns that record session metadata, not turn
# activity. Measured on aih-ISSUE-REF's transcript (2026-10-13): of 2,097 rows between
# a `turn_duration` and the next turn, 1,278 were `queue-operation`, 36
# `attachment`, and the other 783 these types.
_BETWEEN_TURN_METADATA_TYPES = frozenset(
    {
        "agent-name",
        "atis-latch",
        "cost-state",
        "custom-title",
        "file-history-snapshot",
        "last-prompt",
        "mode",
        "permission-mode",
        "pr-link",
    }
)


# Claude Code 2.1.300+ stores each API request's messages as content-addressed rows
# of this type (``sessionId``, ``hash``, ``message``; no timestamp), referenced by
# the ``api-request`` row that follows. They are written when a request is built,
# so a blob's copy of an assistant tool_use lands AFTER that tool's result. Pairing
# them re-opened finished tools: every one of the 14 "pending" tools in
# ai-cli-ISSUE-REF's transcript on 2026-10-17 was a blob copy (ISSUE-REF, ISSUE-REF).
_API_REQUEST_BLOB_TYPE = "api-request-blob"


def _final_turn_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    """The last record that says whether a turn is running.

    Skips between-turn metadata and queue churn that settled without a turn: a
    background-task notification Claude Code enqueued and then removed. Treating
    that churn as activity kept aih-ISSUE-REF attempt b6c4a538 "not-idle" from
    14:11Z to 14:39Z with no readiness sample taken, so its no-composer stall was
    reported at 1742 s instead of 900 s (ISSUE-REF). An enqueue with no matching
    remove, or a dequeue, is a prompt still to run and stays the final record.
    """
    removed = 0
    for record in reversed(records):
        record_type = record.get("type")
        if record_type in _BETWEEN_TURN_METADATA_TYPES:
            continue
        if record_type == "queue-operation":
            operation = record.get("operation")
            if operation == "remove":
                removed += 1
                continue
            if operation == "enqueue" and removed:
                removed -= 1
                continue
        return record
    return records[-1]


def _transcript_quiet_boundary(
    path: Path,
    quiet_seconds: int,
    *,
    diagnostic: dict[str, Any] | None = None,
) -> bool:
    """Return whether the application transcript proves an idle turn boundary."""
    failure: dict[str, str] = {}
    snapshot = _transcript_records(path, failure=failure)
    if snapshot is None:
        if diagnostic is not None:
            # Name the actual failure. "unreadable-or-missing" on a file that read
            # perfectly sent this investigation after a permissions or path fault
            # for the first several minutes (ISSUE-REF).
            diagnostic.update(
                transcript_result=failure.get("reason", "unreadable-or-missing"),
                wait_class="potentially-permanent",
            )
        return False
    records, stat = snapshot
    if not records:
        if diagnostic is not None:
            diagnostic.update(
                transcript_result="empty",
                wait_class="potentially-permanent",
            )
        return False
    pending_tools: set[str] = set()
    pending_tool_started_at: dict[str, int | None] = {}
    for record in records:
        if record.get("type") == _API_REQUEST_BLOB_TYPE:
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_id = block.get("id") or block.get("tool_use_id")
            if block.get("type") == "tool_use" and isinstance(block_id, str):
                pending_tools.add(block_id)
                pending_tool_started_at[block_id] = _transcript_timestamp(
                    block.get("timestamp", record.get("timestamp"))
                )
            elif block.get("type") == "tool_result" and isinstance(block_id, str):
                pending_tools.discard(block_id)
                pending_tool_started_at.pop(block_id, None)
    if pending_tools:
        if diagnostic is not None:
            pending_timestamps = [
                timestamp
                for tool_id in pending_tools
                if (timestamp := pending_tool_started_at.get(tool_id)) is not None
            ]
            oldest_age: int | str = "unknown"
            if len(pending_timestamps) == len(pending_tools):
                oldest_age = _now() - min(pending_timestamps)
            diagnostic.update(
                transcript_result="pending-tools",
                wait_class="potentially-permanent",
                pending_tool_count=len(pending_tools),
                oldest_pending_tool_age_s=oldest_age,
            )
        return False
    final = _final_turn_record(records)
    subtype = final.get("subtype")
    if subtype == "stop_hook_summary":
        if final.get("preventedContinuation") is not False:
            if diagnostic is not None:
                diagnostic.update(
                    transcript_result="stop-continuation-prevented",
                    wait_class="potentially-permanent",
                    final_subtype=subtype,
                    prevented_continuation=final.get("preventedContinuation"),
                )
            return False
    elif subtype != "turn_duration":
        if diagnostic is not None:
            diagnostic.update(
                transcript_result="final-subtype-mismatch",
                wait_class="potentially-permanent",
                final_subtype=subtype,
            )
        return False
    boundary_at = _transcript_timestamp(final.get("timestamp"))
    if boundary_at is None:
        boundary_at = int(stat.st_mtime)
    age = _now() - boundary_at
    idle = 0 <= quiet_seconds <= age
    if diagnostic is not None:
        diagnostic.update(
            transcript_result=(
                "quiet"
                if idle
                else "boundary-age-invalid"
                if age < 0
                else "quiet-period-invalid"
                if quiet_seconds < 0
                else "quiet-period-pending"
            ),
            wait_class="satisfied"
            if idle
            else "bounded"
            if quiet_seconds >= 0 and age >= 0
            else "potentially-permanent",
            boundary_age_s=age,
            quiet_seconds=quiet_seconds,
        )
    return idle


def _message_text(record: dict[str, Any]) -> str | None:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return None


# Commands whose `<command-name>` record Claude Code writes only AFTER they finish,
# so the one record a landing poll can see is the plain user record CC appends at
# submission -- the echo, whose content is exactly the command (ISSUE-REF).
# `/compact` is the one: measured on example-project-ec2 2026-10-11, 625 of 629 plain
# `/compact` records across Claude Code 2.1.220..2.1.283 were followed by the
# compaction and then its command record behind the `compact_boundary`; the other
# four were a submission a human cancelled, and none was answered as text. `/goal`
# writes its command record at once, so a plain `/goal ...` record is still a real
# text landing, and a plain `Resume: ...` prompt is the continuation leg's success.
# Mirrored in ai_chassis.prompt_injection.verify; the parity test pins the two.
_DEFERRED_RECORD_COMMANDS = frozenset({"/compact"})

# Landings that prove Claude Code accepted the submission. Neither proves the
# command RAN: for a compact that proof is PreCompact moving SENT to STARTED,
# and a SENT attempt that never starts ends `start-ack-timeout`.
_SUBMITTED_LANDINGS = frozenset({"command-record", "command-echo"})


def _landing_record(
    record: dict[str, Any], *, queued: bool, command: str = "/compact"
) -> tuple[str | None, bool]:
    """Classify one post-Enter transcript record and retain queue state.

    `command` defaults to `/compact` (parameterised for the resume leg,
    ISSUE-REF). A plain user record matching the command is `command-echo` for a
    command in :data:`_DEFERRED_RECORD_COMMANDS` -- the submission echo, which
    every observed Claude Code version writes before compacting -- and `text`
    for anything else. Reading the echo as `text` was ISSUE-REF: every
    supervisor-submitted `/compact` was logged CC-CORRUPTED and then credited to
    a human by PreCompact, because the command record a landing poll waits for
    is written only after the compaction it would prove.
    """
    record_type = record.get("type")
    if record_type == "queue-operation" and record.get("operation") == "enqueue":
        content = record.get("content")
        if content == command:
            return "queued", True
    if record_type == "user":
        content = _message_text(record)
        if isinstance(content, str):
            if f"<command-name>{command}</command-name>" in content:
                return "command-record", queued
            if content.strip() == command:
                if command in _DEFERRED_RECORD_COMMANDS:
                    return "command-echo", queued
                return "text", queued
    if queued and record.get("subtype") in {"stop_hook_summary", "turn_duration"}:
        return "queue-dropped", queued
    return None, queued


def _poll_landing(
    path: Path | None,
    offset: int,
    timeout: float,
    *,
    queued: bool = False,
    command: str = "/compact",
) -> tuple[str, int, int | None]:
    """Poll only records appended after Enter for a command landing disposition."""
    if path is None:
        return "none", offset, None
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read()
        except OSError:
            return "none", offset, None
        cursor = offset
        for raw_line in data.splitlines(keepends=True):
            if not raw_line.endswith((b"\n", b"\r")):
                break
            try:
                record = json.loads(raw_line.decode("utf-8", errors="strict"))
            except (UnicodeError, json.JSONDecodeError):
                return "none", offset, None
            cursor += len(raw_line)
            if not isinstance(record, dict):
                return "none", offset, None
            disposition, queued = _landing_record(
                record, queued=queued, command=command
            )
            if disposition is not None:
                landed_at = _transcript_timestamp(record.get("timestamp")) or _now()
                return disposition, cursor, landed_at
        offset = cursor
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ("queued" if queued else "none"), offset, None
        time.sleep(min(_DEFAULT_TRANSCRIPT_SIGNAL_POLL_SECONDS, remaining))


@dataclass
class _GoalStateTracker:
    """Incrementally retain only the observed native-goal state for one process."""

    path: Path | None = None
    offset: int = 0
    goal_active: str = "unknown"
    source: str | None = None

    def current(self, pinned: object = None, session_id: object = None) -> str:
        path, self.source = _own_transcript(pinned, session_id)
        if path is None or not path.is_file():
            self.path = None
            self.offset = 0
            self.goal_active = "unknown"
            return self.goal_active
        try:
            size = path.stat().st_size
        except OSError:
            return "unknown"
        if path != self.path or size < self.offset:
            self.path = path
            self.offset = max(0, size - _GOAL_TRANSCRIPT_INITIAL_SCAN_BYTES)
            self.goal_active = "unknown"
        self.offset, self.goal_active = _scan_transcript_for_goal_state(
            path, self.offset, self.goal_active
        )
        return self.goal_active


def run_post_compact_poller(
    key: str,
    attempt_id: str,
    escalation_seconds: int,
    transcript_path: Path | None = None,
    *,
    claim_wake: bool = True,
    store: ResumeStore | None = None,
) -> bool:
    """Observe compaction until application evidence or session termination."""
    store = store or ResumeStore()
    if _event_handoff(key, "compact-lifecycle", store=store, attempt_id=attempt_id):
        return True
    if claim_wake and not _post_compact_notification_wake_enabled():
        log_stage(
            "CC-POLLER-NOOP", key, "reason=post-compact-notification-wake-not-enabled"
        )
        return False
    if transcript_path is None:
        bound = next(
            (r for r in store.read_attempts(key) if r.get("attempt_id") == attempt_id),
            {},
        )
        path, source = _own_transcript(
            bound.get("transcript_path"), bound.get("transcript_session_id")
        )
    else:
        path, source = transcript_path, "hook-payload"
    log_stage(
        "CC-POLLER-TRANSCRIPT",
        key,
        f"attempt={attempt_id} transcript_source={source} found={path is not None}",
    )
    offset = path.stat().st_size if path is not None and path.is_file() else 0
    compact_commands_seen = 0
    next_escalation = time.monotonic() + escalation_seconds
    while True:
        # A human's own /compact needs no wake from this observer, so do not consume
        # the claim for one (ISSUE-REF).  The claim would buy nothing -- this process's
        # wake is its own completion notification, which fires whether or not it wins
        # -- and it costs the marker, which `claim_wake` unlinks, leaving `asyncRewake`
        # no file to react to.  That silently removes one of D-38's racing legs on the
        # one path where the compact was never ours.  It does not retire the poller
        # fallback (D-39 reserves that): the manual path just ends in the same shape as
        # the already-ratified "asyncRewake won the race" path.
        if claim_wake and store.claim_wake(
            key,
            attempt_id,
            "poller",
            not_ready_log_interval_seconds=escalation_seconds,
            refuse_manual_completion=True,
        ):
            log_stage(
                "CC-POLLER-DONE", key, f"attempt={attempt_id} reason=wake-claimed"
            )
            return True
        if store.wake_state(key, attempt_id) == "CLAIMED":
            log_stage(
                "CC-POLLER-DONE", key, f"attempt={attempt_id} reason=already-claimed"
            )
            return True
        records = [
            record
            for record in store.read_attempts(key)
            if record.get("attempt_id") == attempt_id
        ]
        if len(records) != 1:
            log_stage(
                "CC-POLLER-DONE", key, f"attempt={attempt_id} reason=attempt-gone"
            )
            return False
        state = records[0].get("state")
        if state == "COMPLETED":
            log_stage(
                "CC-POLLER-DONE", key, f"attempt={attempt_id} reason=compact-confirmed"
            )
            return True
        if state in TERMINAL_STATES:
            reason = "session-ended" if state == "SESSION_ENDED" else f"state={state}"
            log_stage("CC-POLLER-DONE", key, f"attempt={attempt_id} reason={reason}")
            return False
        if path is None or not path.is_file():
            log_stage(
                "CC-POLLER-DONE", key, f"attempt={attempt_id} reason=transcript-gone"
            )
            return False
        offset, saw_sessionstart, new_compacts = _scan_transcript_for_wake_signals(
            path, offset
        )
        if new_compacts:
            compact_commands_seen += new_compacts
            next_escalation = time.monotonic() + escalation_seconds
            log_stage(
                "CC-POLLER-RESET",
                key,
                f"attempt={attempt_id} reason="
                f"{'compact-start-detected' if compact_commands_seen == 1 else 'compact-relaunch-detected'} "
                f"occurrence={compact_commands_seen}",
            )
        if saw_sessionstart:
            log_stage(
                "CC-POLLER-DONE",
                key,
                f"attempt={attempt_id} reason=transcript-sessionstart-compact",
            )
            return True
        if time.monotonic() >= next_escalation:
            log_stage(
                "CC-POLLER-LONG-WAIT",
                key,
                f"attempt={attempt_id} elapsed_s={escalation_seconds}",
            )
            next_escalation = time.monotonic() + escalation_seconds
        time.sleep(2.0)


def _lifecycle_exit(
    key: str,
    attempt_id: str,
    reason: str,
    result: bool,
    reporter: PollerReporter | None = None,
    *,
    observed_nothing: bool = False,
) -> bool:
    """Record why the independently-launched lifecycle process returned.

    The log line alone was not enough: every exit here is ``exit 0`` with no
    output, so a launcher could not tell a transport decline from an accidental
    immediate return from a poller that was still polling.  The reporter turns
    each into a positive, queryable verdict.

    ``observed_nothing`` is for an exit taken on the first read of the attempt,
    before anything was observed.  It is neither a decline nor a finished
    observation, and reporting it as the latter is what let a zero-observation
    return read as healthy.
    """
    log_stage("CC-LIFECYCLE-EXIT", key, f"attempt={attempt_id} reason={reason}")
    if reporter is not None:
        if reason in POLLER_DECLINE_REASONS:
            reporter.decline(reason)
        elif observed_nothing:
            reporter.abandon(reason)
        else:
            reporter.finish(reason)
    return result


def _run_compact_lifecycle(
    key: str,
    attempt_id: str,
    send_interval_seconds: int,
    warning_interval_seconds: int,
    long_wait_escalate_seconds: int,
    post_compact_escalate_seconds: int,
    *,
    supervisor: ContextSupervisor | None = None,
    transcript_path: Path | None = None,
    claim_post_compact_wake: bool = True,
    reporter: PollerReporter | None = None,
) -> bool:
    """Run one save-state attempt through safe send and completion confirmation.

    Native ``idle_prompt`` remains the immediate fast path.  This lifecycle is
    its independent fallback: while a bound arm has no visible draft, it uses
    the shared stage-then-verify compact transport. A bare manual compact moves
    the record to ``STARTED`` through ``handle_precompact`` and is observed here
    without injecting a second command.
    """
    active_supervisor = supervisor or ContextSupervisor()
    store = active_supervisor.store
    path = transcript_path or _resolve_own_transcript_path()
    next_long_wait = time.monotonic() + long_wait_escalate_seconds
    long_wait_logged = False
    next_warning = time.monotonic() + warning_interval_seconds
    # Probed at most once, and only on the wait that can actually hang: an
    # already-sent attempt has no use for the answer, and `tmux -V` is a
    # sub-process.  Neither condition the probe reports can change inside one
    # session, so one answer holds for the whole lifecycle.
    transport_reason: str | None = None
    transport_probed = False
    completed: dict[str, Any] | None = None
    while True:
        with store.locked():
            candidates = [
                record
                for record in store.read_attempts(key)
                if record.get("attempt_id") == attempt_id
            ]
            if len(candidates) != 1:
                return _lifecycle_exit(
                    key, attempt_id, "attempt-not-unique", True, reporter
                )
            record = candidates[0]
            state = record.get("state")
            if state == "COMPLETED":
                completed = record
                break
            if state in {"SENT", "STARTED"}:
                log_stage(
                    "CC-LIFECYCLE-PIVOT",
                    key,
                    f"attempt={attempt_id} state={state}",
                )
                break
            if state == "ARMED":
                # Step 5b launches before the terminal save-state response can
                # be bound by Stop.  Keep following this reserved attempt, but
                # never inspect or write the pane until binding succeeds.
                should_wait_for_binding = True
            elif _expire_mod_attempt(store, record, key):
                return _lifecycle_exit(
                    key, attempt_id, "mod-request-expired", False, reporter
                )
            elif state in {"BOUND", "STAGED_UNVERIFIED", "QUEUED"}:
                should_wait_for_binding = False
            elif state == "SESSION_ENDED":
                return _lifecycle_exit(
                    key, attempt_id, "session-ended", False, reporter
                )
            elif (
                state == "REFUSED"
                and record.get("transport") == "fifo"
                and record.get("reason") in FIFO_REFUSAL_REASONS
            ):
                return _lifecycle_exit(
                    key, attempt_id, str(record["reason"]), False, reporter
                )
            elif (
                state == "REFUSED"
                and record.get("transport") == "mod"
                and record.get("mod_failure") == "compaction-disabled"
            ):
                # The mod claimed and reported between two polls (ISSUE-REF T-2.4);
                # its notice is already written.
                return _lifecycle_exit(
                    key, attempt_id, "mod-compaction-disabled", False, reporter
                )
            else:
                # Every state reaching here is one the lifecycle cannot act on,
                # so this return observed nothing -- measured on ISSUE-REF 2026-10-01,
                # launched one second after its attempt went terminal REFUSED.
                # The fault notice this writes is read only on the NEXT prompt, so
                # also say it on the pane now (ISSUE-REF) -- except for a
                # superseded attempt, whose successor is the one that will send.
                pane_id = record.get("pane_id")
                warn = getattr(active_supervisor.tmux, "warn_compact_stalled", None)
                if (
                    isinstance(pane_id, str)
                    and callable(warn)
                    and record.get("reason") != "superseded-save-state"
                ):
                    warn(pane_id, f"state={state}")
                return _lifecycle_exit(
                    key,
                    attempt_id,
                    f"state={state}",
                    True,
                    reporter,
                    observed_nothing=True,
                )
        if should_wait_for_binding and not transport_probed:
            # Both questions, because a live server host-wide says nothing about
            # whether THIS session has a pane to be sent into (ISSUE-REF).
            transport_reason = compact_transport_reason(
                active_supervisor.tmux.version(), session_id=record.get("session_id")
            )
            transport_probed = True
        if should_wait_for_binding and transport_reason is not None:
            log_stage(
                "CC-TRANSPORT-UNAVAILABLE",
                key,
                f"attempt={attempt_id} reason={transport_reason}",
            )
            write_transport_notice(
                key, transport_reason, attempt_id=attempt_id, root=store.root
            )
            return _lifecycle_exit(key, attempt_id, transport_reason, False, reporter)
        if path is None or not path.is_file():
            return _lifecycle_exit(key, attempt_id, "transcript-gone", False, reporter)
        if time.monotonic() >= next_long_wait:
            if not long_wait_logged:
                log_stage(
                    "CC-LIFECYCLE-LONG-WAIT",
                    key,
                    f"attempt={attempt_id} state={state}",
                )
                long_wait_logged = True
            pane_id = record.get("pane_id")
            warn = getattr(active_supervisor.tmux, "warn_long_wait", None)
            if isinstance(pane_id, str) and callable(warn):
                warn(pane_id, str(state))
            next_long_wait = time.monotonic() + warning_interval_seconds
        if should_wait_for_binding:
            if reporter is not None:
                reporter.phase("waiting-for-binding")
            time.sleep(send_interval_seconds)
            continue
        repeat_warning = time.monotonic() >= next_warning
        active_supervisor.send_armed_compact(
            key,
            repeat_composer_warning=repeat_warning,
            transcript_path=path,
            lifecycle_interval_seconds=send_interval_seconds,
        )
        if active_supervisor._composer_warning_emitted:
            next_warning = time.monotonic() + warning_interval_seconds
        time.sleep(send_interval_seconds)

    # Only an attempt this supervisor wrote to the FIFO; a human's own /compact in a
    # bare session is observed by the unchanged path below.
    if record.get("landing") == "fifo-written":
        return _confirm_fifo_compact(key, attempt_id, record, path, store, reporter)
    if record.get("transport") == "mod":
        return _confirm_mod_compact(key, attempt_id, record, path, store, reporter)
    if completed is not None:
        return _proved_compact_exit(
            key, attempt_id, "compact-confirmed", completed, path, reporter
        )
    if reporter is not None:
        reporter.phase("observing")
    result = run_post_compact_poller(
        key,
        attempt_id,
        post_compact_escalate_seconds,
        transcript_path=path,
        claim_wake=claim_post_compact_wake,
        store=store,
    )
    if not result:
        return _lifecycle_exit(key, attempt_id, "post-compact-ended", result, reporter)
    _drain_boundary_hold(key)
    observed = next(
        (r for r in store.read_attempts(key) if r.get("attempt_id") == attempt_id),
        {},
    )
    return _proved_compact_exit(
        key, attempt_id, "post-compact-confirmed", observed, path, reporter
    )


def _compact_boundary_wait_seconds() -> float:
    """How long to wait for the session's ``compact_boundary`` row after PostCompact.

    Measured on aih-ISSUE-REF attempt 096b5c51 (2026-10-13): PostCompact completed the
    attempt at 19:48:07Z and Claude Code appended the boundary row at 19:48:09Z.
    """
    value = _bounded_environment_number(
        "AIH_COMPACT_BOUNDARY_WAIT_SECONDS",
        float(_DEFAULT_COMPACT_BOUNDARY_WAIT_SECONDS),
        0,
        600,
    )
    return value if value is not None else _DEFAULT_COMPACT_BOUNDARY_WAIT_SECONDS


def _session_compact_boundary_since(path: Path | None, since: int) -> bool:
    """Whether the session's own transcript holds a compact boundary at or after ``since``.

    Only the session's transcript counts: a sub-agent's compaction writes its
    boundary into its own ``subagents/`` file, so this is the one record that says
    THIS window compacted. Lines are parsed only when they name the subtype, so a
    malformed line elsewhere in the tail does not hide a boundary.
    """
    if path is None:
        return False
    window = _transcript_tail_bytes()
    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            if size > window:
                handle.seek(size - window)
            data = handle.read()
    except OSError:
        return False
    for raw in data.split(b"\n"):
        if b'"compact_boundary"' not in raw:
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(record, dict)
            and record.get("subtype") == "compact_boundary"
            and not record.get("isSidechain")
        ):
            stamp = _transcript_timestamp(record.get("timestamp"))
            # `since` is a whole second taken in a hook; the row carries Claude
            # Code's own millisecond clock. A previous compact is minutes older.
            if (
                stamp is not None
                and stamp >= since - _COMPACT_BOUNDARY_CLOCK_SLACK_SECONDS
            ):
                return True
    return False


def _transcript_instant(value: object) -> float | None:
    """A transcript timestamp in fractional seconds, or None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


def _is_submitted_prompt(record: dict[str, Any]) -> bool:
    """Whether a transcript row is a prompt the composer submitted.

    Tool results are ``user`` rows too, and so are meta and compact-summary rows;
    none of them empties the composer, so none of them may stand in for a submit.
    """
    if record.get("type") != "user" or any(
        record.get(flag) for flag in ("isMeta", "isSidechain", "isCompactSummary")
    ):
        return False
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list) or not content:
        return False
    kinds = {block.get("type") for block in content if isinstance(block, dict)}
    return "tool_result" not in kinds and bool(kinds & {"text", "image"})


def _last_submitted_prompt_at(path: Path) -> float | None:
    snapshot = _transcript_records(path)
    if snapshot is None:
        return None
    for record in reversed(snapshot[0]):
        if _is_submitted_prompt(record):
            return _transcript_instant(record.get("timestamp"))
    return None


def _fifo_input_idle(record: dict[str, Any]) -> str | None:
    """Why a bare session's composer may hold human input, or None when it cannot.

    THE IDLE CONDITION FOR A SCREENLESS SEND (ISSUE-REF AC-4). The FIFO path has no
    pane to read, so emptiness is proved from two records instead of a capture:

    * Claude Code empties the composer when it submits a prompt, and the session's
      own transcript records that prompt with a timestamp;
    * the launcher that owns the session's pty records the time of every operator
      keystroke it relays in ``<session>.input-at`` beside the FIFO
      (:func:`submit_fifo.last_operator_input`, ISSUE-REF).

    So a session whose last operator input is no later than the last submitted
    prompt (plus ``fifo_input_tolerance_seconds``, which covers the submitting
    Enter being recorded just after the transcript's timestamp) has had nothing
    typed since the composer was emptied. A later input means a draft may be
    there: ``input-after-last-prompt``, which waits, because the next submit clears
    it. Every other answer cannot be proved either way and refuses, with the
    record's own ``input-at-*`` detail.

    Residual, stated rather than hidden: input that arrives within the tolerance
    of the submit is not seen. Bytes that are not text, such as a focus or mouse
    report, also move the time, which can only make this wait, never send.
    """
    from ai_chassis import submit_fifo  # noqa: PLC0415 - hook import latency

    fifo = record.get("submit_fifo")
    if not isinstance(fifo, str) or not fifo:
        return "input-at-unnamed"
    last_input, unprovable = submit_fifo.last_operator_input(Path(fifo))
    if last_input is None:
        return unprovable or "input-at-unreadable"
    path_value = record.get("transcript_path")
    if not isinstance(path_value, str):
        return "no-transcript"
    prompt_at = _last_submitted_prompt_at(Path(path_value))
    if prompt_at is None:
        return "no-submitted-prompt"
    tolerance = float(_config()["fifo_input_tolerance_seconds"])
    if last_input > prompt_at + tolerance:
        return "input-after-last-prompt"
    return None


def _confirm_fifo_compact(
    key: str,
    attempt_id: str,
    record: dict[str, Any],
    path: Path | None,
    store: ResumeStore,
    reporter: PollerReporter | None,
) -> bool:
    """Confirm a FIFO-sent ``/compact`` from the session's own transcript alone.

    The #3439 proof (:func:`_session_compact_boundary_since`), waited for up to the
    no-composer stall bound from the write. Nothing is resent: no boundary in time
    exits ``post-compact-unproved``, which the poller supervision reports as
    abandoned and writes the session's fault notice for, as on the pane path.
    """
    since = record.get("sent_at")
    if not isinstance(since, int) or isinstance(since, bool):
        since = None
    if reporter is not None:
        reporter.phase("observing")
    bound = _no_composer_stall_seconds()
    deadline = time.monotonic() + (
        bound if bound is not None else _DEFAULT_NO_COMPOSER_STALL_SECONDS
    )
    while since is not None:
        if _session_compact_boundary_since(path, since):
            log_stage("CC-FIFO-CONFIRMED", key, f"attempt={attempt_id} since={since}")
            _drain_boundary_hold(key)
            return _lifecycle_exit(
                key, attempt_id, "fifo-compact-confirmed", True, reporter
            )
        current = next(
            (r for r in store.read_attempts(key) if r.get("attempt_id") == attempt_id),
            {},
        )
        if current.get("state") == "SESSION_ENDED":
            return _lifecycle_exit(key, attempt_id, "session-ended", False, reporter)
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    log_stage(
        "CC-COMPACT-UNPROVED",
        key,
        f"attempt={attempt_id} claimed=fifo-write since={since} transport=fifo "
        f"submissions={record.get('submissions', 0)} "
        "reason=no-compact-boundary-in-session-transcript",
    )
    return _lifecycle_exit(key, attempt_id, "post-compact-unproved", False, reporter)


def _confirm_mod_compact(
    key: str,
    attempt_id: str,
    record: dict[str, Any],
    path: Path | None,
    store: ResumeStore,
    reporter: PollerReporter | None,
) -> bool:
    """Confirm a mod-claimed compaction from the session's own transcript (T-2.5).

    The FIFO route's proof: a non-sidechain ``compact_boundary`` at or after
    ``sent_at``, waited for up to the no-composer stall bound from the claim. A
    ``turn-running`` report returns the attempt to ``BOUND``; the wait then follows
    the next claim's ``sent_at``. No boundary in time is ``post-compact-unproved``, and
    the request is removed so the mod is never asked again for this attempt.
    """
    from ai_chassis import compact_mod_contract  # noqa: PLC0415 - hook import latency

    if reporter is not None:
        reporter.phase("observing")
    bound = _no_composer_stall_seconds()
    stall = bound if bound is not None else _DEFAULT_NO_COMPOSER_STALL_SECONDS
    current = record
    while True:
        state = current.get("state")
        since = current.get("sent_at")
        if state == "REFUSED" and current.get("mod_failure") == "compaction-disabled":
            return _lifecycle_exit(
                key, attempt_id, "mod-compaction-disabled", False, reporter
            )
        if state == "SESSION_ENDED":
            return _lifecycle_exit(key, attempt_id, "session-ended", False, reporter)
        if state == "BOUND":
            with store.locked():
                expired = any(
                    _expire_mod_attempt(store, r, key)
                    for r in store.read_attempts(key)
                    if r.get("attempt_id") == attempt_id
                )
            if expired:
                return _lifecycle_exit(
                    key, attempt_id, "mod-request-expired", False, reporter
                )
        else:
            if not isinstance(since, int) or isinstance(since, bool):
                break
            if _session_compact_boundary_since(path, since):
                log_stage(
                    "CC-MOD-CONFIRMED", key, f"attempt={attempt_id} since={since}"
                )
                _drain_boundary_hold(key)
                return _lifecycle_exit(
                    key, attempt_id, "mod-compact-confirmed", True, reporter
                )
            if _now() - since >= stall:
                break
        time.sleep(1.0)
        current = next(
            (r for r in store.read_attempts(key) if r.get("attempt_id") == attempt_id),
            {},
        )
    log_stage(
        "CC-COMPACT-UNPROVED",
        key,
        f"attempt={attempt_id} claimed=mod-claim since={since} transport=mod "
        f"retries={current.get('mod_retries', 0)} "
        "reason=no-compact-boundary-in-session-transcript",
    )
    session_id = current.get("mod_session_id")
    if isinstance(session_id, str):
        compact_mod_contract.remove_request(session_id, attempt_id=attempt_id)
    return _lifecycle_exit(key, attempt_id, "post-compact-unproved", False, reporter)


def _proved_compact_exit(
    key: str,
    attempt_id: str,
    reason: str,
    record: dict[str, Any],
    path: Path | None,
    reporter: PollerReporter | None,
) -> bool:
    """Report a confirmed compact only when the session's transcript shows it.

    The attempt reaching COMPLETED is hook evidence, and hooks can be wrong about
    whose compact they saw: b6c4a538 was COMPLETED by a sub-agent's compaction and
    reported ``post-compact-confirmed`` while the window carried on uncompacted
    (ISSUE-REF). The boundary row is written by the compaction itself.
    """
    since = next(
        (
            value
            for field in ("started_at", "sent_at", "armed_at")
            if isinstance(value := record.get(field), int)
            and not isinstance(value, bool)
        ),
        None,
    )
    deadline = time.monotonic() + _compact_boundary_wait_seconds()
    while since is not None:
        if _session_compact_boundary_since(path, since):
            return _lifecycle_exit(key, attempt_id, reason, True, reporter)
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    log_stage(
        "CC-COMPACT-UNPROVED",
        key,
        f"attempt={attempt_id} claimed={reason} since={since} "
        f"compact_source={record.get('compact_source') or 'none'} "
        f"submissions={record.get('submissions', 0)} "
        "reason=no-compact-boundary-in-session-transcript",
    )
    return _lifecycle_exit(key, attempt_id, "post-compact-unproved", False, reporter)


def _drain_boundary_hold(key: str) -> None:
    """Deliver or drop what the library held across this key's boundary (ISSUE-REF D-6).

    Called once the compaction is CONFIRMED, which is the moment the boundary
    closes: the attempt is ``COMPLETED`` and ``PreCompact`` cleared the grace
    marker, so :func:`ai_chassis.prompt_injection.boundary.in_boundary` reads
    ``None`` and the drain can decide each held record (TTL, the injector's own
    preflights, its relevance hook). FAIL-OPEN: the lifecycle's verdict is the
    thing the operator relies on, and a drain fault must never change it, so every
    error is one ``CC-DRAIN-ERROR`` line and nothing else. The next non-self-rescue
    injection for the key drains opportunistically as well, so a fault here delays
    delivery rather than losing it.
    """
    try:
        from ai_chassis.prompt_injection import boundary

        held = boundary.held_records(key)
        log_stage("CC-DRAIN-START", key, f"held={len(held)}")
        if held:
            boundary.drain(key)
    except Exception as error:  # noqa: BLE001 - an observer; see the docstring
        log_stage("CC-DRAIN-ERROR", key, f"error={type(error).__name__}")


def _stamp_typed_wake_override(
    key: str,
    attempt_id: str,
    override: bool | None,
    *,
    root: Path | None = None,
) -> bool:
    """Record a one-attempt typed-wake opt-in on that attempt's own record.

    WHY THE RECORD AND NOT THE ENVIRONMENT.  The legs that read this run in
    different processes at different times -- `arm_continuation` fires from a
    SessionStart hook long after the lifecycle launched -- so an env var set for
    the lifecycle is simply absent by then.  Writing the resolved answer onto the
    attempt record makes it durable for exactly as long as the attempt exists,
    which is the scope the override is supposed to have.

    Writes NOTHING when no override is requested, so an ordinary attempt record
    carries no such field at all and the fleet default governs.  That absence is
    what the next attempt inherits.
    """
    if not _typed_wake_override_requested(override=override):
        return False
    store = ResumeStore(root) if root is not None else ResumeStore()
    try:
        with store.locked():
            for record in store.read_attempts(key):
                if (
                    record.get("attempt_id") == attempt_id
                    and record.get("attempt_type") == "compact"
                ):
                    store.update(record, str(record["state"]), typed_wake_override=True)
                    log_stage(
                        "CC-TYPED-WAKE-OVERRIDE",
                        key,
                        f"attempt={attempt_id} scope=this-attempt",
                    )
                    return True
    except OSError:
        return False
    return False


def run_compact_lifecycle(
    key: str,
    attempt_id: str,
    send_interval_seconds: int,
    warning_interval_seconds: int,
    long_wait_escalate_seconds: int,
    post_compact_escalate_seconds: int,
    *,
    supervisor: ContextSupervisor | None = None,
    transcript_path: Path | None = None,
    claim_post_compact_wake: bool = True,
    typed_wake_override: bool | None = None,
) -> bool:
    """Run a lifecycle with a durable diagnostic for every normal or error exit.

    The diagnostic is now two things, because the log line alone left every exit
    looking like every other one from outside the process: an appended
    ``CC-LIFECYCLE-*`` line, and a phase record published *before* the poll
    window opens so :func:`poller_health` can answer even when this process is
    killed before it can say anything (ISSUE-REF).
    """
    root = supervisor.store.root if supervisor is not None else None
    # Resolved and recorded ONCE, here, before any leg runs. Every leg then reads
    # the same answer off the same attempt record instead of re-resolving an env
    # var that a later process might not inherit -- which is what makes the scope
    # exactly one attempt rather than "whatever is in the environment right now".
    _stamp_typed_wake_override(key, attempt_id, typed_wake_override, root=root)
    if _event_handoff(
        key,
        "compact-lifecycle",
        store=supervisor.store if supervisor is not None else None,
        attempt_id=attempt_id,
    ):
        return True
    with poller_supervision(key, attempt_id, root=root) as reporter:
        try:
            return _run_compact_lifecycle(
                key,
                attempt_id,
                send_interval_seconds,
                warning_interval_seconds,
                long_wait_escalate_seconds,
                post_compact_escalate_seconds,
                supervisor=supervisor,
                transcript_path=transcript_path,
                claim_post_compact_wake=claim_post_compact_wake,
                reporter=reporter,
            )
        except Exception as error:
            log_stage(
                "CC-LIFECYCLE-ERROR",
                key,
                f"attempt={attempt_id} error={type(error).__name__}",
            )
            raise


_SAVE_STATE_COMPLETION = re.compile(
    r"(?:\A|\n)AIH-COMPACT-READY v=1 attempt_id="
    r"(?P<attempt_id>[0-9a-f]{32}) completion_nonce="
    r"(?P<completion_nonce>[0-9a-f]{32})\n\nTHEN COMPACT:\n```\n/compact\n```\Z"
)


def parse_save_state_completion(
    last_assistant_message: object,
) -> dict[str, str] | None:
    """Parse only an unquoted, terminal save-state completion suffix."""
    if not isinstance(last_assistant_message, str):
        return None
    match = _SAVE_STATE_COMPLETION.search(last_assistant_message)
    return match.groupdict() if match is not None else None


def _transcript_signal_wait() -> tuple[float, float] | None:
    try:
        timeout = float(
            os.environ.get(
                "AIH_TRANSCRIPT_SIGNAL_TIMEOUT_SECONDS",
                str(_DEFAULT_TRANSCRIPT_SIGNAL_TIMEOUT_SECONDS),
            )
        )
        poll = float(
            os.environ.get(
                "AIH_TRANSCRIPT_SIGNAL_POLL_SECONDS",
                str(_DEFAULT_TRANSCRIPT_SIGNAL_POLL_SECONDS),
            )
        )
    except ValueError:
        return None
    if not 0 <= timeout <= 15 or not 0.05 <= poll <= 0.5:
        return None
    return timeout, poll


def _transcript_timestamp(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        return None
    return int(parsed.timestamp()) if parsed.tzinfo is not None else None


def _rfc3339_utc(stamp: int) -> str:
    return datetime.fromtimestamp(stamp, UTC).isoformat().replace("+00:00", "Z")


def _is_credential_abort(record: dict[str, Any]) -> bool:
    """Whether one transcript record is a turn Claude Code lost to a dead credential.

    The conjunction is measured, not guessed (ISSUE-REF T-03, 2026-10-05). Across
    this host's 66 transcripts, `isApiErrorMessage: true` selects 28 records and
    every one of them also carries `error: "cloud_credential_error"`. Neither field
    alone is a safe filter:

    * The human string `Could not load AWS credentials` appears in 11 files -- it is
      quoted by agent briefs, tool_use arguments and tool_result bodies, which are
      not aborted turns at all.
    * `cloud_credential_error` alone matches 34 records; the 6 extra are
      `user`/`tool_use`/`tool_result` records from sessions grepping for it.

    `isSidechain` is excluded because 8 of the 28 are sub-agent turns. A sub-agent
    has no pane of its own, so its abort is the parent session's to recover; waking
    on it would target a key that cannot receive anything.
    """
    return (
        record.get("type") == "assistant"
        and record.get("isApiErrorMessage") is True
        and record.get("error") == CREDENTIAL_ABORT_ERROR
        and not record.get("isSidechain")
    )


def credential_abort_in_window(path: Path, dead_from: int, dead_to: int) -> bool:
    """Whether this transcript was left stopped by a credential abort in the window.

    An abort followed by an ordinary assistant turn *still inside* the window is not
    a loss: Claude Code's own `awsAuthRefresh` retry recovered it, and T-02 measured
    exactly that shape (`post_error_turns_that_are_errors=0`). Prompting such a
    session would interrupt work that never stopped, so the later turn clears the
    verdict.
    """
    snapshot = _transcript_records(path)
    if snapshot is None:
        return False
    records, _ = snapshot
    aborted = False
    for record in records:
        stamp = _transcript_timestamp(record.get("timestamp"))
        if stamp is None or not dead_from <= stamp <= dead_to:
            continue
        if _is_credential_abort(record):
            aborted = True
        elif record.get("type") == "assistant" and not record.get("isSidechain"):
            aborted = False
    return aborted


def reauth_wake_prompt(dead_from: int, dead_to: int) -> str:
    """The single-line resume prompt sent after a verified re-login.

    One line by construction. The transport is `tmux send-keys` into a live
    composer, where an embedded newline submits whatever is staged at that moment --
    so a multi-line prompt would submit its first line as the whole instruction and
    leave the rest as a second, truncated turn.

    It names the window and the restored credential and nothing else. No token,
    device code, URL, profile or username reaches a pane, a log or a transcript.
    """
    return (
        "Your AWS credential expired between "
        f"{_rfc3339_utc(dead_from)} and {_rfc3339_utc(dead_to)} "
        "and has now been restored, so the turn you were running in that window was "
        "aborted and lost. Resume the in-flight work from where it stopped, and "
        "relaunch or resume any agents that died with it."
    )


def _record_parent(record: dict[str, Any]) -> str | None:
    parent = record.get("parentUuid", record.get("parent_uuid"))
    return parent if isinstance(parent, str) else None


def _is_descendant(
    record: dict[str, Any], assistant_uuid: str, parents: dict[str, str | None]
) -> bool:
    current = _record_parent(record)
    seen: set[str] = set()
    while current is not None and current not in seen:
        if current == assistant_uuid:
            return True
        seen.add(current)
        current = parents.get(current)
    return False


def bind_save_state_transcript_attempt(
    payload: dict[str, Any], key: str, *, store: ResumeStore | None = None
) -> dict[str, Any] | None:
    """Bind a reserved attempt to the supported Stop payload, fail closed."""
    store = store or ResumeStore()
    completion = parse_save_state_completion(payload.get("last_assistant_message"))
    path_value = payload.get("transcript_path")
    session_id = payload.get("session_id")
    with store.locked():
        candidates = [
            record
            for record in store.pending(key, "compact")
            if record.get("origin") == "save-state"
        ]
        active_candidates = [
            record for record in candidates if record.get("state") in {"ARMED", "BOUND"}
        ]
        bound_candidates = [
            record for record in candidates if record.get("state") == "BOUND"
        ]
        observed = False

        def observe(outcome: str) -> None:
            nonlocal observed
            if observed or len(bound_candidates) != 1:
                return
            observed = True
            log_stage(
                "CC-STOP-OBSERVED",
                key,
                f"attempt={bound_candidates[0]['attempt_id']} match={outcome}",
            )

        def capture(checks: dict[str, str]) -> None:
            if len(bound_candidates) == 1:
                outcomes = {
                    "sentinel_parsed": "not-evaluated",
                    "attempt_id_match": "not-evaluated",
                    "completion_nonce_digest_match": "not-evaluated",
                    "session_id_match": "not-evaluated",
                    "resume_artifact_digest_match": "not-evaluated",
                    "consumed": "not-evaluated",
                }
                outcomes.update(checks)
                _write_transcript_debug_capture(
                    key,
                    "bound-attempt",
                    payload.get("last_assistant_message"),
                    state=",".join(
                        str(record["state"]) for record in active_candidates
                    ),
                    checks=outcomes,
                )

        if completion is None:
            observe("sentinel-mismatch")
            capture({"sentinel_parsed": "no"})
            _transcript_fallback(key, "sentinel")
            return None
        if not isinstance(path_value, str) or not Path(path_value).is_absolute():
            observe("transcript-path")
            capture({"sentinel_parsed": "yes", "transcript_path_valid": "no"})
            _transcript_fallback(key, "transcript-path")
            return None
        if not isinstance(session_id, str) or not session_id:
            observe("session-id")
            capture(
                {
                    "sentinel_parsed": "yes",
                    "transcript_path_valid": "yes",
                    "session_id_valid": "no",
                }
            )
            _transcript_fallback(key, "session-id")
            return None
        if len(candidates) != 1:
            observe("attempt-count")
            capture({"sentinel_parsed": "yes", "attempt_count_match": "no"})
            # Carry the count, because on the zero arm this line is the ONLY evidence
            # that survives: `observe` and `capture` above are both gated on exactly
            # one BOUND record, and `bound_candidates` is a subset of `candidates`, so
            # zero candidates necessarily means zero bound ones and silences both.
            # `reason=attempt-count` alone cannot then tell an attempt that had already
            # ended (0 -- what a terminal refusal leaves behind) from a genuine
            # two-record ambiguity (2+), and those want different remedies. Measured on
            # a live session 2026-10-02: the decline logged with no CC-STOP-OBSERVED
            # anywhere near it and an empty debug directory (ISSUE-REF).
            states = (
                ",".join(sorted({str(record.get("state")) for record in candidates}))
                or "none"
            )
            _transcript_fallback(
                key, f"attempt-count candidates={len(candidates)} states={states}"
            )
            return None
        record = candidates[0]
        history = store.latest_history(key)
        checks = {
            "sentinel_parsed": "yes",
            "attempt_id_match": (
                "yes" if record.get("attempt_id") == completion["attempt_id"] else "no"
            ),
            "completion_nonce_digest_match": (
                "yes"
                if record.get("completion_nonce_digest")
                == digest(completion["completion_nonce"])
                else "no"
            ),
            "session_id_match": (
                "yes" if record.get("session_id") == session_id else "no"
            ),
            "resume_artifact_digest_match": (
                "yes"
                if history is not None
                and record.get("resume_artifact_digest") == history.command_digest
                else "no"
            ),
            "consumed": "yes" if store.consumed(record) else "no",
        }
        if (
            record.get("state") not in {"ARMED", "BOUND"}
            or any(
                value == "no"
                for value in (
                    checks["attempt_id_match"],
                    checks["completion_nonce_digest_match"],
                    checks["session_id_match"],
                    checks["resume_artifact_digest_match"],
                )
            )
            or checks["consumed"] == "yes"
        ):
            observe("attempt-mismatch")
            capture(checks)
            _transcript_fallback(key, "attempt-mismatch")
            return None
        fifo = fifo_submit_target() if record.get("state") == "ARMED" else None
        mod_reason = (
            mod_presence_reason(session_id)
            if fifo is None and record.get("state") == "ARMED"
            else None
        )
        if fifo is not None:
            record = store.update(
                record, "BOUND", transport="fifo", submit_fifo=str(fifo)
            )
            log_stage(
                "CC-BIND",
                key,
                f"transport=fifo fifo={_log_token(fifo.name)} origin=save-state",
            )
        elif record.get("state") == "ARMED" and mod_reason is None:
            record = _bind_mod_transport(store, record, key, session_id)
        elif record.get("state") == "ARMED":
            pane_id = os.environ.get("TMUX_PANE") or key
            tmux = TmuxAdapter()
            tmux_version = tmux.version()
            fingerprint = tmux.fingerprint(pane_id)
            if not _tmux_version_allowed(tmux_version) or not fingerprint:
                capture({**checks, "pane_binding": "no"})
                _transcript_fallback(key, "pane-binding")
                # A permanently absent transport is not the same event as a lost
                # pane, and the session has no other way to be told which one it
                # got: this decline is exactly where auto-compact stops, so leave
                # the notice here too rather than only in the lifecycle poller,
                # which a session may not have launched at all.
                # `session_pane_reason` is what makes this branch reachable at all
                # for a bare session: with no pane, the fingerprint now fails, and
                # without the pane question the reason would be None and the notice
                # would never be written (ISSUE-REF).
                reason = (
                    transport_unavailable_reason(tmux_version) or session_pane_reason()
                )
                if reason is not None:
                    write_transport_notice(
                        key,
                        reason,
                        attempt_id=completion["attempt_id"],
                        root=store.root,
                        mod_absent=None if mod_reason == "pane" else mod_reason,
                    )
                return None
            record = store.update(
                record, "BOUND", pane_id=pane_id, pane_fingerprint=fingerprint
            )
            log_stage("CC-BIND", key, f"pane={pane_id} origin=save-state")
        capture({**checks, "pane_binding": "yes"})
        observe("matched")
        suffix = (
            "AIH-COMPACT-READY v=1 attempt_id="
            f"{completion['attempt_id']} "
            f"completion_nonce={completion['completion_nonce']}"
            "\n\nTHEN COMPACT:\n```\n/compact\n```"
        )
        return store.update(
            record,
            "BOUND",
            transcript_path=path_value,
            transcript_suffix=suffix,
            transcript_session_id=session_id,
        )


def _bind_mod_transport(
    store: ResumeStore, record: dict[str, Any], key: str, session_id: str
) -> dict[str, Any]:
    """Bind ``record`` to the compact-guard mod route and write its request (T-2.1, T-2.2).

    The request is the only thing the mod acts on, and it is written only while injection
    is enabled, read at binding time; ``claim`` re-reads the switch at call time.
    """
    from ai_chassis import compact_mod_contract  # noqa: PLC0415 - hook import latency

    bound = datetime.now(UTC)
    expires = bound + timedelta(seconds=int(_config()["long_wait_escalate_seconds"]))
    record = store.update(
        record,
        "BOUND",
        transport="mod",
        mod_session_id=session_id,
        mod_expires_at=int(expires.timestamp()),
    )
    log_stage(
        "CC-BIND",
        key,
        f"transport=mod session={compact_mod_contract.session_key(session_id)[:12]} "
        "origin=save-state",
    )
    if not _enabled():
        return record
    compact_mod_contract.write_request(
        session_id,
        key=key,
        attempt_id=str(record["attempt_id"]),
        bound_at=bound,
        expires_at=expires,
    )
    log_stage(
        "CC-MOD-REQUEST",
        key,
        f"attempt={record['attempt_id']} expires_at={expires.isoformat()}",
    )
    return record


def _expire_mod_attempt(store: ResumeStore, record: dict[str, Any], key: str) -> bool:
    """End a mod attempt still BOUND past its request's expiry; True when it did.

    The mod ignores an expired request (design Failure paths), so without this the
    attempt would wait in BOUND for good and its lifecycle would never exit. Making it
    REFUSED removes the request. The caller holds the store lock.
    """
    expires = record.get("mod_expires_at")
    if (
        record.get("state") != "BOUND"
        or record.get("transport") != "mod"
        or not isinstance(expires, int)
        or isinstance(expires, bool)
        or _now() < expires
    ):
        return False
    store.update(record, "REFUSED", reason="mod-request-expired")
    log_stage(
        "CC-MOD-EXPIRED",
        key,
        f"attempt={record['attempt_id']} expires_at={expires} "
        f"retries={record.get('mod_retries', 0)}",
    )
    return True


MOD_FAILURE_CLASSES = frozenset({"turn-running", "compaction-disabled", "other"})


def _mod_attempt(
    store: ResumeStore, session_id: str, attempt_id: str
) -> tuple[dict[str, Any] | None, str]:
    """The mod attempt ``attempt_id`` of ``session_id``, or ``(None, reason)``.

    The request names the attempt's key, so the store is read for that key alone. Called
    under the store lock.
    """
    from ai_chassis import compact_mod_contract  # noqa: PLC0415 - hook import latency

    request, detail = compact_mod_contract.read_request(session_id)
    if request is None:
        return None, f"no-request ({detail})"
    if request.get("attempt_id") != attempt_id:
        return None, "request-names-another-attempt"
    key = request.get("key")
    if not isinstance(key, str) or _session_key_refusal(key) is not None:
        return None, "request-key-invalid"
    matches = [
        record
        for record in store.read_attempts(key)
        if record.get("attempt_id") == attempt_id
    ]
    if len(matches) != 1:
        return None, f"attempt-count={len(matches)}"
    (record,) = matches
    if record.get("transport") != "mod":
        return None, "not-a-mod-attempt"
    if record.get("mod_session_id") != session_id:
        return None, "other-session"
    return record, "read"


def claim_mod_compact(session_id: str, attempt_id: str) -> tuple[bool, str]:
    """The bridge ``claim`` verb: move a BOUND mod attempt to SENT once (T-2.3).

    Under the store lock, so of two concurrent claims exactly one finds the attempt
    BOUND and unconsumed. The switch is read at call time.
    """
    if not _enabled():
        return False, "disabled"
    store = ResumeStore()
    with store.locked():
        record, reason = _mod_attempt(store, session_id, attempt_id)
        if record is None:
            return False, reason
        if record.get("state") != "BOUND":
            return False, f"state={record.get('state')}"
        if store.consumed(record):
            return False, "consumed"
        key = str(record["key"])
        store.consume(record)
        store.update(record, "SENT", sent_at=_now(), landing="mod-claimed")
    from ai_chassis import compact_mod_contract  # noqa: PLC0415 - hook import latency

    log_stage(
        "CC-MOD-SENT",
        key,
        f"mod={compact_mod_contract.MOD_NAME} mechanism=session.compact "
        f"session={compact_mod_contract.session_key(session_id)[:12]} "
        f"attempt={attempt_id} reason={record.get('origin') or 'unknown'}",
    )
    return True, "claimed"


def report_mod_failure(
    session_id: str, attempt_id: str, failure: str, message: str
) -> tuple[bool, str]:
    """The bridge ``report`` verb: record why ``$.session.compact`` threw (T-2.4).

    ``turn-running`` returns the attempt to BOUND behind the restage backoff, so the next
    ``turn.complete`` retries; ``compaction-disabled`` refuses it for good with its
    notice; ``other`` is logged and left SENT, so the boundary proof decides it.
    """
    if failure not in MOD_FAILURE_CLASSES:
        return False, f"unknown-failure={failure}"
    store = ResumeStore()
    with store.locked():
        record, reason = _mod_attempt(store, session_id, attempt_id)
        if record is None:
            return False, reason
        if record.get("state") != "SENT":
            return False, f"state={record.get('state')}"
        key = str(record["key"])
        if failure == "turn-running":
            retries = int(record.get("mod_retries", 0)) + 1
            store.update(
                record,
                "BOUND",
                mod_failure=failure,
                mod_retries=retries,
                **ContextSupervisor._retry_fields(record, attempts=retries),  # noqa: SLF001
            )
            store.unconsume(record)
        elif failure == "compaction-disabled":
            store.update(record, "REFUSED", mod_failure=failure, reason=failure)
        else:
            store.update(record, "SENT", mod_failure=failure)
    log_stage(
        "CC-MOD-FAILED",
        key,
        f"reason={failure} attempt={attempt_id} message={_log_token(message[:80])}",
    )
    if failure == "compaction-disabled":
        write_transport_notice(
            key, "mod-compaction-disabled", attempt_id=attempt_id, root=store.root
        )
    return True, "recorded"


def mod_bridge_main(argv: list[str]) -> int:
    """``python -m ai_chassis.context_supervisor mod <presence|claim|report> ...``.

    The compact-guard mod's only way to reach the supervisor (design Data Model). Exit 0
    on success, 3 with the reason on stdout when the supervisor refuses, 2 on a bad
    argument.
    """
    from ai_chassis import compact_mod_contract  # noqa: PLC0415 - hook import latency

    parser = argparse.ArgumentParser(prog="compact-mod-bridge")
    verbs = parser.add_subparsers(dest="verb", required=True)
    presence = verbs.add_parser("presence")
    presence.add_argument("--session", required=True)
    presence.add_argument("--mod-version", required=True)
    presence.add_argument("--cc-version", required=True)
    claim = verbs.add_parser("claim")
    claim.add_argument("--session", required=True)
    claim.add_argument("--attempt", required=True)
    report = verbs.add_parser("report")
    report.add_argument("--session", required=True)
    report.add_argument("--attempt", required=True)
    report.add_argument("--failure", required=True, choices=sorted(MOD_FAILURE_CLASSES))
    report.add_argument("--message", default="")
    args = parser.parse_args(argv)
    if not args.session.strip():
        print("compact-mod-bridge: --session is empty", file=sys.stderr)
        return 2
    if args.verb == "presence":
        compact_mod_contract.write_presence(
            args.session, mod_version=args.mod_version, cc_version=args.cc_version
        )
        return 0
    if args.verb == "claim":
        done, reason = claim_mod_compact(args.session, args.attempt)
    else:
        done, reason = report_mod_failure(
            args.session, args.attempt, args.failure, args.message
        )
    print(reason)
    return 0 if done else 3


def wait_for_stop_acceptance(
    record: dict[str, Any], *, timeout: float, poll: float
) -> tuple[bool, str]:
    """Incrementally read the transcript until an accepted Stop summary arrives."""
    path_value = record.get("transcript_path")
    suffix = record.get("transcript_suffix")
    session_id = record.get("transcript_session_id")
    armed_at = record.get("armed_at")
    if not all(isinstance(value, str) for value in (path_value, suffix, session_id)):
        return False, "schema"
    if not isinstance(armed_at, int) or isinstance(armed_at, bool):
        return False, "schema"
    path = Path(path_value)
    deadline = time.monotonic() + timeout
    position = 0
    # Start at the transcript's tail, not byte 0. The record this waits for was
    # written seconds ago, so everything before the tail is irrelevant -- and
    # scanning it made an ancient corrupt line fatal here too: ISSUE-REF's line 2,324
    # returned `malformed-line` before this could reach the Stop summary it wanted
    # (ISSUE-REF). It also read 588 MiB to find something in the last few KiB.
    align_to_tail = True
    fragment = b""
    assistant_uuid: str | None = None
    parents: dict[str, str | None] = {}
    transcript_inode: tuple[int, int] | None = None
    while True:
        try:
            stat = path.stat()
            current_inode = (stat.st_dev, stat.st_ino)
            if transcript_inode is None:
                transcript_inode = current_inode
            elif current_inode != transcript_inode:
                return False, "transcript-rotated"
            if stat.st_size < position:
                return False, "transcript-truncated"
            with path.open("rb") as handle:
                if align_to_tail:
                    window = _transcript_tail_bytes()
                    if stat.st_size > window:
                        handle.seek(stat.st_size - window)
                        handle.readline()  # the window cut a line in half
                    position = handle.tell()
                    align_to_tail = False
                else:
                    handle.seek(position)
                chunk = handle.read()
                position = handle.tell()
        except OSError:
            return False, "transcript-unavailable"
        data = fragment + chunk
        lines = data.split(b"\n")
        fragment = lines.pop()
        for raw in lines:
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False, "malformed-line"
            if not isinstance(value, dict):
                return False, "schema"
            uuid = value.get("uuid")
            if isinstance(uuid, str):
                parents[uuid] = _record_parent(value)
            message = value.get("message")
            role = message.get("role") if isinstance(message, dict) else None
            if assistant_uuid is None:
                content = message.get("content") if isinstance(message, dict) else None
                text_match = isinstance(content, list) and any(
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                    and block["text"].endswith(suffix)
                    for block in content
                )
                if (
                    value.get("type") == "assistant"
                    and value.get("sessionId") == session_id
                    and isinstance(uuid, str)
                    and role == "assistant"
                    and message.get("stop_reason") == "end_turn"
                    and _transcript_timestamp(value.get("timestamp")) is not None
                    and _transcript_timestamp(value.get("timestamp")) >= armed_at
                    and text_match
                ):
                    assistant_uuid = uuid
                continue
            if (
                value.get("type") == "assistant"
                or role in {"assistant", "user"}
                or value.get("type") == "user"
            ):
                return False, "intervening-turn"
            if (
                value.get("type") == "system"
                and value.get("subtype") == "stop_hook_summary"
            ):
                if not _is_descendant(value, assistant_uuid, parents):
                    return False, "summary-parent"
                if value.get("preventedContinuation") is not False:
                    return False, "prevented-stop"
                if value.get("hookErrors") not in ([], None):
                    return False, "hook-errors"
                return True, "accepted"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, "torn-line" if fragment else "timeout"
        time.sleep(min(poll, remaining))


def _transcript_fallback(key: str, reason: str) -> None:
    log_stage("CC-TRANSCRIPT-FALLBACK", key, f"reason={reason}")


def _not_idle_field(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return "null"
    if isinstance(value, str) and (
        key == "transcript_path" or re.fullmatch(r"[A-Za-z0-9_.:+/-]+", value) is None
    ):
        return json.dumps(value)
    return str(value)


def _log_not_idle(key: str, diagnostic: dict[str, Any]) -> None:
    fields = " ".join(
        f"{name}={_not_idle_field(name, diagnostic[name])}"
        for name in (
            "idle_prompt_consulted",
            "idle_prompt_confirmed_recently",
            "idle_prompt_window_s",
            "transcript_consulted",
            "transcript_path",
            "transcript_result",
            "wait_class",
            "composer_consulted",
            "pending_tool_count",
            "oldest_pending_tool_age_s",
            "final_subtype",
            "prevented_continuation",
            "boundary_age_s",
            "quiet_seconds",
        )
        if name in diagnostic
    )
    log_stage("CC-WAIT", key, f"reason=not-idle {fields}")


def release_save_state_compact(
    record: dict[str, Any], *, supervisor: ContextSupervisor | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """Consume one accepted transcript capability and send through the tmux fence."""
    store = supervisor.store if supervisor is not None else ResumeStore()
    supervisor = supervisor or ContextSupervisor(store)
    key = record.get("key")
    attempt_id = record.get("attempt_id")
    if not isinstance(key, str) or not isinstance(attempt_id, str):
        return None, "schema"
    with store.locked():
        candidates = [
            item
            for item in store.pending(key, "compact")
            if item.get("origin") == "save-state"
            and item.get("attempt_id") == attempt_id
        ]
        if len(candidates) != 1 or candidates[0].get("state") != "BOUND":
            return None, "attempt-not-bound"
        current = candidates[0]
        if store.consumed(current):
            return None, "nonce-consumed"
        if current.get("transport") == "fifo":
            # The FIFO send waits on its own idle proof in `send_armed_compact`,
            # which the idle_prompt path and the lifecycle both reach (ISSUE-REF).
            return None, "fifo-transport"
        if current.get("transport") == "mod":
            # The compact-guard mod compacts in process once it claims (ISSUE-REF D-3);
            # the supervisor never sends anything for this attempt.
            return None, "mod-transport"
        pane_id = current.get("pane_id")
        fingerprint = current.get("pane_fingerprint")
        if not isinstance(pane_id, str) or not isinstance(fingerprint, str):
            _write_tmux_debug_capture(
                supervisor.tmux,
                key,
                pane_id if isinstance(pane_id, str) else "unknown",
                attempt_id,
                "missing-pane-binding",
            )
            return None, "missing-pane-binding"
        idle_diagnostic: dict[str, Any] = {}
        idle_proof = supervisor._idle_proof(current, diagnostic=idle_diagnostic)
        if idle_proof is None:
            _log_not_idle(key, idle_diagnostic)
        else:
            current = store.update(
                current, str(current["state"]), idle_proof=idle_proof
            )
        current, reason = supervisor._bound_compact_transport(
            current, idle_proof=idle_proof, consume_before_stage=True
        )
        if current is None or reason is not None:
            return None, reason
        store.consume(current)
        armed_at = current.get("armed_at")
        if isinstance(armed_at, int) and not isinstance(armed_at, bool):
            elapsed_ms = max(0, int(time.time() * 1000) - armed_at * 1000)
            log_stage(
                "CC-TRANSCRIPT-CONFIRMED",
                key,
                f"attempt={attempt_id} elapsed_ms={elapsed_ms}",
            )
        return current, None


def handle_transcript_signal(
    payload: dict[str, Any], key: str, *, supervisor: ContextSupervisor | None = None
) -> None:
    """Async Stop hook entry point; every decline leaves idle_prompt authoritative."""
    if not _enabled() or not _enrolled(key):
        return
    store = supervisor.store if supervisor is not None else ResumeStore()
    record = bind_save_state_transcript_attempt(payload, key, store=store)
    if record is None:
        return
    if _event_handoff(key, "compact-lifecycle", payload=payload):
        return
    wait = _transcript_signal_wait()
    if wait is None:
        _transcript_fallback(key, "configuration")
        return
    accepted, reason = wait_for_stop_acceptance(record, timeout=wait[0], poll=wait[1])
    if not accepted:
        _transcript_fallback(key, reason)
        return
    _, reason = release_save_state_compact(record, supervisor=supervisor)
    if reason is not None:
        _transcript_fallback(key, reason)


def _trim_history(path: Path) -> None:
    try:
        maximum = int(os.environ.get("AIH_RESUME_HISTORY_MAX_LINES", "500"))
    except ValueError:
        maximum = 500
    maximum = max(maximum, 1)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= maximum:
        return
    temporary = path.with_name(f".{path.name}.trim")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(lines[-maximum:]) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def log_stage(stage: str, key: str, detail: str = "") -> None:
    """Best-effort log writer: the shared aggregate log, plus a per-session log."""
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {stage:<10} key={key} {detail}\n"
    try:
        root = _root_from_environment()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (root / "resume.log").open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass
    try:
        root = _root_from_environment()
        log_directory = root / "logs"
        log_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_name = f"{_safe_marker_component(key)}.log"
        with (log_directory / log_name).open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def _transition_failure(
    store: ResumeStore,
    record: dict[str, Any],
    *,
    continuation: bool,
    reason: str,
    detail: str = "",
) -> None:
    state = "CONTINUATION_AMBIGUOUS" if continuation else "AMBIGUOUS"
    stage = "CC-CONT-AMBIGUOUS" if continuation else "CC-AMBIGUOUS"
    store.update(record, state, reason=reason, terminal_at=_now())
    store.disarm(str(record["key"]), reason)
    log_stage(stage, str(record["key"]), f"reason={reason}{detail}")


def _write_tmux_debug_capture(
    tmux: object, key: str, pane_id: str, attempt_id: str, reason: str
) -> None:
    writer = getattr(tmux, "write_debug_capture", None)
    if callable(writer):
        writer(key, pane_id, attempt_id, reason)


def _write_transcript_debug_capture(
    key: str,
    reason: str,
    last_assistant_message: object,
    *,
    state: str | None = None,
    checks: dict[str, str] | None = None,
) -> None:
    """Persist the raw Stop-hook payload field, bounded, opt-in via code-config."""
    try:
        if not _config()["debug_diagnostics_enabled"]:
            return
        root = _root_from_environment()
        path = (
            root
            / "debug"
            / (
                f"{_safe_marker_component(key)}-{time.time_ns()}-"
                f"transcript-{_safe_marker_component(reason)}.txt"
            )
        )
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        contents = (
            f"reason={reason}\n"
            f"last_assistant_message_type={type(last_assistant_message).__name__}\n"
            f"last_assistant_message_repr={last_assistant_message!r}\n"
        )
        if state is not None:
            contents += f"attempt_state={state}\n"
        if checks is not None:
            contents += "".join(f"{name}={result}\n" for name, result in checks.items())
        path.write_text(contents, encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        return


def _write_user_prompt_debug_capture(
    key: str, reason: str, payload: dict[str, Any]
) -> None:
    """Persist the raw UserPromptSubmit payload, bounded and opt-in via code-config."""
    try:
        if not _config()["debug_diagnostics_enabled"]:
            return
        root = _root_from_environment()
        path = (
            root
            / "debug"
            / (
                f"{_safe_marker_component(key)}-{int(time.time())}-"
                f"user-prompt-{_safe_marker_component(reason)}.txt"
            )
        )
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        contents = f"reason={reason}\npayload_repr={payload!r}\n"
        path.write_text(contents, encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        return


def _write_hook_exception_diagnostic(event: str, error: Exception) -> None:
    """Persist bounded opt-in evidence while preserving hook fail-open behavior."""
    try:
        if not _config()["debug_diagnostics_enabled"]:
            return
        header = (
            f"event={event}\n"
            f"exception_type={type(error).__name__}\n"
            f"exception_message={str(error)[:512]}\n\ntraceback:\n"
        )
        encoded_header = header.encode("utf-8", errors="replace")
        encoded_traceback = "".join(traceback.format_exception(error)).encode(
            "utf-8", errors="replace"
        )
        encoded = (
            encoded_header
            + encoded_traceback[
                -(_MAX_HOOK_EXCEPTION_DIAGNOSTIC_BYTES - len(encoded_header)) :
            ]
        )
        root = _root_from_environment()
        path = (
            root
            / "debug"
            / (f"hook-exception-{time.time_ns()}-{_safe_marker_component(event)}.txt")
        )
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(encoded)
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001 - diagnostics must preserve fail-open hooks
        return


def _compact_correlation_window() -> int | None:
    try:
        window = int(
            os.environ.get(
                "AIH_COMPACT_CORRELATION_WINDOW_SECONDS",
                str(_DEFAULT_COMPACT_CORRELATION_WINDOW_SECONDS),
            )
        )
    except ValueError:
        return None
    return window if 0 <= window <= 3600 else None


def _idle_prompt_recency_window() -> int | None:
    try:
        window = int(
            os.environ.get(
                "AIH_IDLE_PROMPT_RECENCY_SECONDS",
                str(_DEFAULT_IDLE_PROMPT_RECENCY_SECONDS),
            )
        )
    except ValueError:
        return None
    return window if 0 <= window <= 3600 else None


def _bounded_environment_number(
    name: str, default: float, minimum: float, maximum: float
) -> float | None:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return None
    return value if minimum <= value <= maximum else None


def _turn_quiet_seconds() -> int | None:
    default = float(_config()["turn_quiet_seconds"])
    value = _bounded_environment_number("AIH_TURN_QUIET_SECONDS", default, 5, 3600)
    return int(value) if value is not None else None


def _no_composer_escalate_seconds() -> int | None:
    default = float(_config()["no_composer_escalate_seconds"])
    value = _bounded_environment_number(
        "AIH_NO_COMPOSER_ESCALATE_SECONDS",
        default,
        0,
        3600,
    )
    return int(value) if value is not None else None


def _no_composer_stall_seconds() -> int | None:
    default = float(_config()["no_composer_stall_seconds"])
    value = _bounded_environment_number(
        "AIH_NO_COMPOSER_STALL_SECONDS", default, 0, 86400
    )
    return int(value) if value is not None else None


def _shows_permission_dialog(region: str) -> bool:
    """A Claude Code permission prompt: a ``Do you want to ...?`` row above a
    numbered option the selection cursor sits on."""
    rows = region.split("\n")
    question = next(
        (index for index, row in enumerate(rows) if _PERMISSION_QUESTION.match(row)),
        None,
    )
    return question is not None and any(
        _PERMISSION_SELECTED_OPTION.match(row) for row in rows[question + 1 :]
    )


def _no_composer_frame(region: str) -> tuple[str, str]:
    """Say what the pane's bottom shows when no composer could be read (ISSUE-REF).

    ``permission-dialog``: a permission prompt holds the input area, often raised by
    a background agent's tool call, and only answering it brings the composer back.
    example-session waited 90 minutes on one on 2026-10-24 while the notice told the
    operator to resize the window or type ``/compact``, keystrokes the dialog would
    have taken as an answer.  ``cut-at-pane-bottom``: the last captured row is a
    composer rule, so the cursor row and closing rule were drawn below the viewport.
    ``capture-pane`` emits every pane row, blank ones included, so this is the
    pane's real last row.  Returns the frame class and the rule's width, which
    against the pane width says whether Claude Code's idea of the terminal size
    disagrees with the pane's.
    """
    if _shows_permission_dialog(region):
        return "permission-dialog", "none"
    rows = region.split("\n")
    if rows and _is_composer_top_border(rows[-1]):
        return "cut-at-pane-bottom", str(len(rows[-1].rstrip()))
    return "no-rule", "none"


def _landing_timeout_seconds() -> float | None:
    default = float(_config()["landing_timeout_seconds"])
    return _bounded_environment_number("AIH_LANDING_TIMEOUT_SECONDS", default, 0, 30)


def _restage_backoff_ceiling_seconds() -> int | None:
    default = float(_config()["restage_backoff_ceiling_seconds"])
    value = _bounded_environment_number(
        "AIH_RESTAGE_BACKOFF_CEILING_SECONDS",
        default,
        60,
        3600,
    )
    return int(value) if value is not None else None


def _continuation_ready_wait() -> tuple[float, float] | None:
    """Return the bounded SessionStart compositor-readiness wait configuration."""
    try:
        timeout = float(
            os.environ.get(
                "AIH_INJECT_CONT_READY_TIMEOUT_SECONDS",
                str(_DEFAULT_CONTINUATION_READY_TIMEOUT_SECONDS),
            )
        )
        poll = float(
            os.environ.get(
                "AIH_INJECT_CONT_READY_POLL_SECONDS",
                str(_DEFAULT_CONTINUATION_READY_POLL_SECONDS),
            )
        )
    except ValueError:
        return None
    # Bounded by its own ceiling, not by the shipped default. Validating against
    # the default made the default a HARD CAP: any larger configured value made
    # this resolver return None, which `_wait_for_continuation_ready` reports as
    # not-ready and `arm_continuation` turns into a refusal -- so configuring a
    # realistic wait silently disabled the leg instead of lengthening it
    # (ISSUE-REF). The serialisation this guard could not do is now the banner
    # handshake's job; this one only waits for a composer.
    if not 0 <= timeout <= _MAX_CONTINUATION_READY_TIMEOUT_SECONDS:
        return None
    return (timeout, poll) if 0 < poll <= 1 else None


def legacy_composer_ready(sample: Callable[[], ComposerSample]) -> tuple[bool, str]:
    """Two consecutive recognised ``empty-composer`` samples inside the
    continuation readiness budget, else the reason the last sample gave.

    The legacy typed legs (reauth wake, queue waker) consult this before their
    ``fence_and_send`` so a human draft is never typed over while their
    ``library_cutover`` switch is off (ISSUE-REF). Same acceptance as the
    library's ``wait_ready``; read-only, never writes to the pane.
    """
    wait = _continuation_ready_wait()
    if wait is None:
        return False, "not-ready"
    timeout, poll = wait
    deadline = time.monotonic() + timeout
    consecutive = 0
    while True:
        current = sample()
        if current.composer_recognized and current.classification == "empty-composer":
            consecutive += 1
            if consecutive >= 2:
                return True, "empty-composer"
        else:
            consecutive = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            nonempty = current.classification in {
                "composer-nonempty",
                "composer-suggestion",
            }
            return False, "composer-nonempty" if nonempty else "not-ready"
        time.sleep(min(poll, remaining))


def legacy_landing(
    transcript: Path | None, offset: int, command: str
) -> tuple[str, int | None]:
    """Read the records a legacy send appended after ``offset`` for its landing,
    within the compact landing budget. ``none`` when nothing landed."""
    timeout = _landing_timeout_seconds()
    if timeout is None:
        return "none", None
    landing, _, landed_at = _poll_landing(transcript, offset, timeout, command=command)
    return landing, landed_at


def transcript_size(transcript: Path | None) -> int:
    """The byte offset a landing poll starts from: the transcript's size now."""
    try:
        return transcript.stat().st_size if transcript is not None else 0
    except OSError:
        return 0


def _banner_turn_wait() -> tuple[float, float] | None:
    """Return the bounded wait for the SessionStart banner turn to consume a packet.

    Deliberately a separate budget from `_continuation_ready_wait`: that one
    waits for a COMPOSER to go quiet and is measured in seconds, while this one
    waits for a whole assistant TURN to run and ack, which takes as long as the
    turn takes. Collapsing them is what capped the serialisation guard at 3.0s.
    """
    try:
        timeout = float(
            os.environ.get(
                "AIH_INJECT_BANNER_WAIT_SECONDS",
                str(_DEFAULT_BANNER_TURN_WAIT_SECONDS),
            )
        )
        poll = float(
            os.environ.get(
                "AIH_INJECT_BANNER_POLL_SECONDS",
                str(_DEFAULT_BANNER_TURN_POLL_SECONDS),
            )
        )
    except ValueError:
        return None
    if not 0 <= timeout <= _MAX_BANNER_TURN_WAIT_SECONDS:
        return None
    return (timeout, poll) if 0 < poll <= 5 else None


def handle_precompact(payload: dict[str, Any], key: str) -> None:
    """Attribute a bare compact to the one save-state attempt in this session."""
    if not _enabled() or not _enrolled(key):
        return
    store = ResumeStore()
    with store.locked():
        pending = store.pending(key, "compact")
        if not pending:
            return
        trigger = payload.get("trigger")

        def mismatch(record: dict[str, Any], why: str) -> None:
            # Name what was seen: attempt 9c59a5bb's line said only the reason, and
            # its cause (state=QUEUED) took a ledger read to find (ISSUE-REF).
            _transition_failure(
                store,
                record,
                continuation=False,
                reason="causal-correlation-mismatch",
                detail=(
                    f" attempt={record.get('attempt_id')} "
                    f"state={record.get('state')} trigger={trigger or 'none'} "
                    f"why={why}"
                ),
            )

        if len(pending) != 1:
            for record in pending:
                mismatch(record, f"pending={len(pending)}")
            return
        record = pending[0]
        from ai_chassis.wake import sessions

        if sessions.enabled() and (
            record.get("session_id") or record.get("transcript_session_id")
        ) != payload.get("session_id"):
            return
        if (
            sessions.enabled()
            and record.get("state") == "SENT"
            and record.get("landing") == "awaiting-transcript"
        ):
            from ai_chassis.wake.handlers.transcript import observe_landing

            record = observe_landing(record, store)
        landed_at = record.get("landed_at")
        if landed_at is None and record.get("landing") in {
            "fifo-written",
            "mod-claimed",
        }:
            # A FIFO write has no landing capture, so its write is its landing: with
            # no landed_at, the session's own PreCompact a second after the send was
            # refused as outside-landing-window and made terminal before the
            # transcript proof could run (ISSUE-REF, live on example-project-ec2). A mod's
            # claim is the same: it compacts in process right after (ISSUE-REF T-2.5).
            landed_at = record.get("sent_at")
        now = _now()
        window = _compact_correlation_window()
        if payload.get("custom_instructions") not in (None, ""):
            mismatch(record, "custom-instructions")
            return
        manually_started_save_state = record.get("origin") == "save-state" and (
            record.get("state") in {"BOUND", "STAGED_UNVERIFIED"}
            or (record.get("state") == "ARMED" and trigger == "manual")
        )
        if manually_started_save_state:
            # A human submitted bare /compact while the lifecycle was waiting
            # for an empty composer or could not bind a transport at all. It is
            # the same saved outbox attempt and must pivot to continuation
            # confirmation, never race to inject a second command. This hook
            # does not launch a second lifecycle; source=manual only attributes
            # the human command to the already-armed save-state attempt.
            clear_retry_notice(
                key,
                attempt_id=str(record["attempt_id"]),
                root=store.root,
            )
            store.update(
                record,
                "STARTED",
                started_at=now,
                compact_source="manual",
                backoff_seconds=0,
                retry_not_before=0,
            )
            store.consume(record)
            store._clear_save_state_active_locked(key)
            # The supervisor's OWN submission no longer reaches this branch: its
            # echo is `command-echo` and the attempt is SENT (ISSUE-REF; until that
            # fix all 323 text-landed attempts arrived here as source=manual). What
            # remains is a human's own /compact while the attempt waits, so the
            # line still names the attempt's staging to keep the two apart.
            staged_at = record.get("last_staged_at")
            staged_age = (
                now - staged_at
                if isinstance(staged_at, int) and not isinstance(staged_at, bool)
                else "unknown"
            )
            log_stage(
                "CC-START",
                key,
                f"attempt={record['attempt_id']} source=manual "
                f"landing={record.get('landing') or 'none'} "
                f"submissions={record.get('submissions', 0)} "
                f"staged_age_s={staged_age}",
            )
            return
        sent_at = record.get("sent_at")
        if (
            record.get("state") == "QUEUED"
            and trigger == "manual"
            and isinstance(sent_at, int)
            and not isinstance(sent_at, bool)
        ):
            # The supervisor's /compact was accepted into Claude Code's prompt
            # queue behind a turn that started first (attempt 9c59a5bb: a peer
            # message dequeued 0.4s before the Enter) and has now drained into
            # this PreCompact.  QUEUED -> SENT is otherwise resolved only by the
            # lifecycle's next 30s poll, which this compact beat, so requiring
            # SENT here discarded the supervisor's own compact as unattributable.
            store.update(
                record,
                "STARTED",
                started_at=now,
                landed_at=now,
                landing="queued-drained",
            )
            store.consume(record)
            store._clear_save_state_active_locked(key)
            log_stage(
                "CC-START",
                key,
                f"attempt={record['attempt_id']} source=queued-send "
                f"queued_s={now - sent_at}",
            )
            return
        if record.get("state") != "SENT":
            mismatch(record, "not-sent")
            return
        if (
            not isinstance(landed_at, int)
            or isinstance(landed_at, bool)
            or window is None
            or not 0 <= now - landed_at <= window
        ):
            mismatch(record, "outside-landing-window")
            return
        store.update(record, "STARTED", started_at=now)
        store.consume(record)
        store._clear_save_state_active_locked(key)
        log_stage("CC-START", key, f"attempt={record['attempt_id']}")


def handle_session_start(payload: dict[str, Any], key: str) -> None:
    if not _enabled() or payload.get("source") != "compact":
        return
    store = ResumeStore()
    with store.locked():
        for record in store.pending(key, "compact"):
            from ai_chassis.wake import sessions

            if sessions.enabled() and (
                record.get("session_id") or record.get("transcript_session_id")
            ) != payload.get("session_id"):
                continue
            if record.get("state") == "STARTED":
                history = store.latest_history(key)
                if history is None:
                    _transition_failure(
                        store, record, continuation=False, reason="unpaired-history"
                    )
                    return
                store.update(
                    record,
                    "STARTED",
                    session_start_at=_now(),
                    resume_command=history.resume_command,
                    resume_command_digest=history.command_digest,
                )
                return


_SESSION_END_REASONS = {
    "clear",
    "resume",
    "logout",
    "prompt_input_exit",
    "other",
}


def handle_session_end(payload: dict[str, Any], key: str) -> None:
    """Terminalize pending work when Claude Code reports its own session end.

    The documented SessionEnd input includes ``reason`` with the values above:
    https://code.claude.com/docs/en/hooks#sessionend-input

    The key does not identify the session that ended. A nested or headless
    ``claude`` started from inside the pane inherits the task-list id, so its
    SessionEnd arrives under this key too (ISSUE-REF: a ``claude -p`` ended
    aih-ISSUE-REF's attempt 544339f1 two minutes after /save-state). The payload's
    ``session_id`` names the session that ended, so an attempt armed for a
    session is ended only by that session's SessionEnd. On ``/clear`` the payload
    still carries the old id, the one the attempt was armed under.

    ``arm_compact`` records no session id, so for its attempts the question is
    process ownership of the bound pane instead (the ISSUE-REF comparison). Only
    a proved non-owner keeps the attempt; an undeterminable owner ends it as
    before, since nothing shows the ending session is someone else's.
    """
    reported_reason = payload.get("reason")
    reason = reported_reason if reported_reason in _SESSION_END_REASONS else "other"
    ending_session = payload.get("session_id")
    store = ResumeStore()
    ended = 0
    foreign = 0
    with store.locked():
        for record in store.read_attempts(key):
            if record.get("state") in TERMINAL_STATES:
                continue
            if record.get("attempt_type") not in {"compact", "continuation"}:
                continue
            armed_for = record.get("session_id") or record.get("transcript_session_id")
            if armed_for:
                if armed_for != ending_session:
                    foreign += 1
                    continue
            elif _prompt_pane_ownership(record)[0] == "non-owner":
                foreign += 1
                continue
            store.update(
                record,
                "SESSION_ENDED",
                session_end_reason=reason,
                terminal_at=_now(),
            )
            ended += 1
    log_stage(
        "CC-SESSION-END",
        key,
        f"reason={reason} attempts={ended} other_session_attempts_kept={foreign} "
        f"session={_log_token(ending_session or 'none')}",
    )


def handle_postcompact(payload: dict[str, Any], key: str) -> None:
    if not _enabled() or not _enrolled(key):
        return
    store = ResumeStore()
    with store.locked():
        pending = store.pending(key, "compact")
        candidates = [record for record in pending if record.get("state") == "STARTED"]
        if len(candidates) != 1 or not candidates[0].get("session_start_at"):
            for record in candidates:
                _transition_failure(
                    store, record, continuation=False, reason="postcompact-order"
                )
            return
        candidate = candidates[0]
        from ai_chassis.wake import sessions

        if sessions.enabled():
            if (
                candidate.get("session_id") or candidate.get("transcript_session_id")
            ) != payload.get("session_id"):
                return
            # The hook acknowledges completion; the bound transcript proves it.
            store.update(candidate, "STARTED", event_postcompact=True)
            return
        artifact, reason = _matching_injected_artifact(store, candidate)
        if artifact is None:
            record = store.update(
                candidate,
                "COMPLETED",
                completed_at=_now(),
                wake_state="FAILED",
                wake_failure=reason,
            )
            log_stage(
                "CC-REWAKE-FAILED",
                key,
                f"attempt={record['attempt_id']} reason={reason}",
            )
            return
        expected_digest = str(candidate["resume_artifact_digest"])
        if candidate.get("wake_state") == "FAILED":
            log_stage(
                "CC-REWAKE-FAILED",
                key,
                f"attempt={candidate['attempt_id']} reason=previous-timeout",
            )
            return
        # READY is a claimable status, and `claim_wake` -- its only reader -- is
        # reachable only from the two NOTIFICATION legs, so this gate is theirs and
        # not the typed leg's (ISSUE-REF split them). With it shut there is no
        # claimant, so READY plus `sent_at: null` described a wake that was waiting
        # when in fact it was abandoned by policy. Record the gate as the terminal
        # wake state instead, and write no marker: the marker is asyncRewake's
        # trigger file and nothing would ever consume or unlink it (ISSUE-REF).
        wake_enabled = _post_compact_notification_wake_enabled()
        wake_fields: dict[str, Any] = (
            {
                "wake_state": "READY",
                "wake_marker": str(
                    _write_wake_ready_marker(store, candidate, expected_digest)
                ),
            }
            if wake_enabled
            else {
                "wake_state": "DISABLED",
                "wake_disabled_reason": POST_COMPACT_WAKE_DISABLED_REASON,
            }
        )
        record = store.update(
            candidate,
            "COMPLETED",
            completed_at=_now(),
            **wake_fields,
        )
        # Distinguish "we delivered this compact" from "it happened anyway" (ISSUE-REF).
        #
        # The attempt record already retains the failure -- ISSUE-REF kept submissions=0,
        # send_result=stage-verify-mismatch and sent_at=None. What it did not have was any
        # visible signal: this line was an unconditional CC-DONE, identical whether the
        # supervisor submitted the compact or a human compacted manually after our send failed.
        # That is why a completely dead send path went unnoticed until someone looked at their
        # prompt box and found "/compact/compact".
        submissions = record.get("submissions")
        delivered = (
            isinstance(submissions, int)
            and not isinstance(submissions, bool)
            and (submissions > 0)
        )
        if delivered:
            log_stage("CC-DONE", key, f"attempt={record['attempt_id']}")
        else:
            # Name the DESIGNED case separately (ISSUE-REF).  With the typed leg off
            # by ruling (ISSUE-REF A002), a human's own /compact completing with
            # submissions=0 is the contract, not a dead send path -- but the one
            # sentence covering both read as a failure, and it was read as one.
            expected_unsent = (
                record.get("compact_source") == "manual"
                and not _post_compact_typed_wake_enabled()
            )
            detail = (
                "reason=typed-wake-disabled detail=a human's own /compact completed "
                "this attempt and the typed submit leg is off by ruling "
                "(ISSUE-REF A002), so no submission was expected"
                if expected_unsent
                else "reason=send-never-landed detail=compact completed but this "
                "supervisor never submitted it"
            )
            log_stage(
                "CC-DONE-UNSENT",
                key,
                f"attempt={record['attempt_id']} submissions={submissions} "
                f"send_result={record.get('send_result')} "
                f"compact_source={record.get('compact_source')} {detail}",
            )
        if wake_enabled:
            log_stage(
                "CC-WAKE-READY",
                key,
                f"attempt={record['attempt_id']} digest={expected_digest}",
            )
        else:
            log_stage(
                "CC-WAKE-DISABLED",
                key,
                f"attempt={record['attempt_id']} digest={expected_digest} "
                "reason=post-compact-wake-not-enabled decision=ISSUE-REF "
                "detail=no leg may claim this wake; submit the resume turn by hand",
            )


_GOAL_CHECKIN_PREFIX = "Goal check-in:"
_STOP_HOOK_FEEDBACK_PREFIX = "Stop hook feedback:"


def _prompt_origin(prompt: object) -> str:
    """Name who produced a UserPromptSubmit prompt, from its text alone.

    The hook payload carries no origin field on Claude Code 2.1.283: its
    construction is the base fields plus ``hook_event_name``, ``prompt`` and
    ``session_title`` (read from the installed binary, ISSUE-REF).  The
    transcript's ``origin.kind`` is written only when a queued prompt is
    DEQUEUED, which can be minutes after this hook fires at enqueue time, so the
    text is the only evidence available here.

    Returns ``human``, ``goal-hook``, ``stop-hook``, ``notification`` or ``peer``.
    Everything unrecognised is ``human``, so the refusal stays fail-closed.
    ``scheduled-wakeup`` needs transcript evidence the text cannot carry, so
    ``handle_user_prompt`` assigns it via ``_scheduled_wakeup_evidence``.
    """
    if not isinstance(prompt, str):
        return "human"
    stripped = prompt.lstrip()
    lines = prompt.splitlines()
    if prompt.startswith("<task-notification>"):
        return "goal-hook" if _GOAL_CHECKIN_PREFIX in prompt else "notification"
    # A sub-agent's SendMessage arrives as `<agent-message from=...>`; Claude Code
    # records it as origin.kind=peer.  Unrecognised, it refused 4 of ISSUE-REF's 5
    # save-state attempts on 2026-10-12 as origin=human (ISSUE-REF).
    if stripped.startswith(("<cross-session-message ", "<agent-message ")):
        return "peer"
    if (
        len(lines) > 1
        and lines[0] == "<system-reminder>"
        and lines[1].startswith(_GOAL_CHECKIN_PREFIX)
    ):
        return "goal-hook"
    # The bare forms are how Claude Code re-invokes a turn from a Stop hook: the
    # native /goal check-in (ISSUE-REF transcript a33dd433 line 116102) and a
    # blocking Stop hook's reason.  Measured to arrive as isMeta user records
    # without a UserPromptSubmit, so this is defence in depth, not the fix.
    if stripped.startswith(_GOAL_CHECKIN_PREFIX):
        return "goal-hook"
    if stripped.startswith(_STOP_HOOK_FEEDBACK_PREFIX):
        return "stop-hook"
    return "human"


def _scheduled_wakeup_evidence(prompt: object, transcript_path: object) -> str | None:
    """Name the transcript record proving ``prompt`` is this session's own wakeup.

    A ``ScheduleWakeup`` prompt is delivered as plain text indistinguishable from a
    typed one, so ``_prompt_origin`` calls it ``human`` (ISSUE-REF: ISSUE-REF attempt
    880518c8 refused at 18:55:39Z by the session's own reminder, scheduled by its
    ``ScheduleWakeup`` tool_use at 18:36:59Z and fired as a ``scheduled_task_fire``
    record). CronCreate's fire record can carry only a 192-character preview;
    its marked user delivery retains the full prompt. A previous recurring
    delivery can supply that evidence before the next delivery is written, even
    after creation falls outside the replay tail. Only an EXACT full-text match
    counts; absent, unreadable or malformed transcripts prove nothing.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    read = _transcript_records(Path(transcript_path))
    if read is None:
        return None
    records, _ = read
    evidence: str | None = None
    for record in records:
        if (
            record.get("type") == "user"
            and record.get("isMeta") is True
            and record.get("promptSource") == "system"
            and record.get("wakeupSource") == "schedule_wakeup"
            and record.get("turnOrigin") == "scheduled"
            and isinstance(record.get("scheduledTaskId"), str)
            and record["scheduledTaskId"]
            and _message_text(record) == prompt
        ):
            return "scheduled-user-record"
        if (
            record.get("type") == "system"
            and record.get("subtype") == "scheduled_task_fire"
            and record.get("prompt") == prompt
        ):
            return "scheduled-task-fire"
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == "ScheduleWakeup"
                and isinstance(block.get("input"), dict)
                and block["input"].get("prompt") == prompt
            ):
                evidence = "schedule-wakeup-tool-use"
    return evidence


def _is_background_task_notification(payload: dict[str, Any]) -> bool:
    """Distinguish Claude Code's synthetic prompt delivery from user-entered input."""
    prompt = payload.get("prompt")
    return isinstance(prompt, str) and _prompt_origin(prompt) != "human"


def _pending_queued_human_prompts(path: Path | None, since: int) -> int | None:
    """How many human prompts enqueued at/after ``since`` are still undelivered.

    This is the one condition under which a DELIVERED post-compact wake cannot be
    seen to start a turn, and it is the measured cause of ISSUE-REF: a prompt the
    owner typed while the compact was still running is enqueued by Claude Code,
    dequeued the instant the compact boundary lands, and consumes the turn the
    wake notification would otherwise have started.  The wake's own
    ``task-notification`` is then folded into that human turn, which is exactly
    what "the wake did not fire" looks like from inside the session.

    Pairing is by CONTENT, not by counting ``dequeue`` records: a ``dequeue``
    carries no content, and Claude Code emits them for a human-queued prompt but
    not for every synthetic notification, so a counter drifts.  An enqueued prompt
    whose text has already appeared as a ``user`` record has been delivered.

    Returns ``None`` when the transcript supplies no answer -- absent, unreadable,
    or malformed inside the read window -- so a caller can say "unknown" rather
    than report the absence of a reading as a zero.
    """
    if path is None:
        return None
    read = _transcript_records(path)
    if read is None:
        return None
    records, _ = read
    enqueued: list[str] = []
    delivered: set[str] = set()
    for record in records:
        if (
            record.get("type") == "queue-operation"
            and record.get("operation") == "enqueue"
        ):
            content = record.get("content")
            stamp = _transcript_timestamp(record.get("timestamp"))
            if (
                isinstance(content, str)
                and content
                and stamp is not None
                and stamp >= since
                and not _is_background_task_notification({"prompt": content})
            ):
                enqueued.append(content)
        elif record.get("type") == "user":
            text = _message_text(record)
            if isinstance(text, str) and text:
                delivered.add(text)
    return sum(1 for content in enqueued if content not in delivered)


def _prompt_shape(prompt: object) -> str:
    """Return a privacy-bounded leading tag or token for refusal diagnostics."""
    if not isinstance(prompt, str) or not prompt.strip():
        return "unknown"
    first = prompt.lstrip().split(maxsplit=1)[0]
    if first.startswith("<"):
        return first.split(">", 1)[0]
    return first


def _log_token(value: object) -> str:
    """One whitespace-free, bounded token, so a log line stays parseable."""
    return re.sub(r"[^A-Za-z0-9._:/<>-]", "_", str(value))[:40] or "unknown"


def _prompt_head(prompt: object) -> str:
    """The prompt's first characters as one parseable token, for refusal diagnosis."""
    if not isinstance(prompt, str) or not prompt.strip():
        return "unknown"
    collapsed = re.sub(r"\s+", "_", prompt.strip())[:48]
    return re.sub(r"[^A-Za-z0-9._:/<>#-]", "_", collapsed)


def _unsent_save_state_attempts(store: ResumeStore, key: str) -> list[dict[str, Any]]:
    with store.locked():
        return [
            record
            for record in store.pending(key, "compact")
            if record.get("origin") == "save-state" and not record.get("sent_at")
        ]


def _log_synthetic_prompt(key: str, origin: str, prompt: object, evidence: str) -> None:
    """Record that a synthetic prompt arrived while an unsent save-state attempt
    was pending and did NOT refuse it, so resume.log alone shows both arms."""
    for record in _unsent_save_state_attempts(ResumeStore(), key):
        log_stage(
            "CC-PROMPT-SYNTHETIC",
            key,
            f"attempt={record['attempt_id']} origin={origin} "
            f"origin_evidence={evidence} prompt_head={_prompt_head(prompt)} "
            f"state={record.get('state')} decision=attempt-kept",
        )


def _prompt_pane_ownership(record: dict[str, Any]) -> tuple[str, str]:
    """Whether THIS hook's claude owns the attempt's bound pane: ``owner``,
    ``non-owner`` or ``unknown``, with the pids it compared as log fields.

    A nested or headless ``claude`` inherits the session key and ``TMUX_PANE``, so
    its prompt fires this same hook (ISSUE-REF: ISSUE-REF attempt 2c9f5108 was refused
    by pid 4129146, a second claude that logged ``not-pane-owner`` six seconds
    earlier).  Ownership is the pane's top-level claude against the claude this
    hook descends from, the comparison launch-resume already makes
    (ISSUE-REF).  It is a process identity, so `/clear`, which changes only
    the session id, keeps the owner the owner.
    """
    pane = record.get("pane_id")
    if not isinstance(pane, str) or not pane:
        return "unknown", "pane=none"
    from ai_chassis import launch_resume  # noqa: PLC0415 - imports this module

    shown = re.sub(r"[^A-Za-z0-9%._:-]", "_", pane)[:40]

    try:
        pane_claude = launch_resume._session_pid(pane)
        prompt_claude = launch_resume._owner_claude(os.getpid())
    except Exception as error:  # noqa: BLE001 - unreadable means unknown, never a crash
        return "unknown", f"pane={shown} error={type(error).__name__}"
    detail = (
        f"pane={shown} pane_claude={pane_claude or 'unknown'} "
        f"prompt_claude={prompt_claude or 'unknown'}"
    )
    if pane_claude is None or prompt_claude is None:
        return "unknown", detail
    return ("owner" if pane_claude == prompt_claude else "non-owner"), detail


def _is_typed_save_state(prompt: object) -> bool:
    """A prompt that is the `/save-state` slash command, with or without arguments."""
    if not isinstance(prompt, str):
        return False
    stripped = prompt.strip()
    return stripped == "/save-state" or stripped.startswith("/save-state ")


def _surface_compact_recovery(payload: dict[str, Any], key: str) -> None:
    """Print the recovery reminder when this re-entry finds save-state's compact unstarted.

    UserPromptSubmit stdout is additional context, the one channel that reaches a session
    with no pane (ISSUE-REF) and the one the idle-wake switch leaves open, because the turn
    is starting anyway. Best-effort: a recovery fault must not break the prompt.
    """
    session_id = payload.get("session_id")
    transcript = payload.get("transcript_path")
    if not isinstance(session_id, str) or not isinstance(transcript, str):
        return
    from ai_chassis import compact_rearm  # noqa: PLC0415 - hook import latency

    try:
        text = compact_rearm.recover_at_prompt(key, session_id, Path(transcript))
    except Exception as error:  # noqa: BLE001 - never fail the prompt over a reminder
        log_stage("CC-REARM-PROMPT-FAILED", key, f"error={type(error).__name__}")
        return
    if text:
        print(text)


def handle_user_prompt(payload: dict[str, Any], key: str) -> None:
    """Observational confirmation: never a denial or rewritten input.

    Its only output is the compact recovery reminder a synthetic prompt carries into a
    stalled save-state freeze (:func:`_surface_compact_recovery`), printed as additional
    context.
    """
    if not _enabled() or not _enrolled(key):
        return
    # A run_in_background completion is delivered through UserPromptSubmit while
    # /save-state is still composing its terminal sentinel.  It did not change
    # the human-owned composer, so it must not consume the pending fast path.
    prompt_text = payload.get("prompt")
    origin = _prompt_origin(prompt_text)
    evidence = "prompt-text" if origin != "human" else "none"
    store = ResumeStore()
    # The text cannot tell a self-scheduled wakeup from a typed prompt, so consult
    # the transcript, but only when a refusal is actually at stake (ISSUE-REF).
    if origin == "human" and _unsent_save_state_attempts(store, key):
        wakeup = _scheduled_wakeup_evidence(prompt_text, payload.get("transcript_path"))
        if wakeup is not None:
            origin, evidence = "scheduled-wakeup", wakeup
    if origin != "human":
        _log_synthetic_prompt(key, origin, prompt_text, evidence)
        _surface_compact_recovery(payload, key)
        return
    # A typed `/save-state` produces no Skill PreToolUse payload (measured in ISSUE-REF's
    # 2026-10-10 transcript), so without this the grace window never armed for the most
    # common invocation and the skill's own steps were denied at FORCE (ISSUE-REF D-4).
    if _is_typed_save_state(payload.get("prompt")):
        store.mark_save_state_active(key)
        log_stage("CC-SAVE-STATE-ARM", key, "source=typed-prompt")
    # BEFORE the refusal below, because that refusal is the single most common
    # thing an observer dies on (ISSUE-REF: 50 of 91 refusals across 9 session
    # keys, measured 2026-10-06) and sweeping first keeps this prompt's own
    # refusal from being mistaken for the death of an earlier attempt's observer.
    # Outside the store lock: it reads the pollers directory, not the ledger.
    surface_dead_pollers(key, root=store.root)
    # Outside the lock: it lists tmux panes and walks the process tree.
    ownership = {
        record["attempt_id"]: _prompt_pane_ownership(record)
        for record in _unsent_save_state_attempts(store, key)
    }
    with store.locked():
        for record in store.pending(key, "compact"):
            if record.get("origin") == "save-state" and record.get("state") in {
                "QUEUED",
                "SENT",
            }:
                # A human prompt racing an already-submitted compact refuses
                # nothing, so it used to leave no line: ISSUE-REF's 01:05:09 prompt and
                # the 01:05:13 send that Claude Code then queued were joined only
                # from the transcript (ISSUE-REF).
                log_stage(
                    "CC-PROMPT-AFTER-SEND",
                    key,
                    f"attempt={record['attempt_id']} state={record.get('state')} "
                    f"origin={origin} prompt_head={_prompt_head(prompt_text)}",
                )
                continue
            if record.get("origin") == "save-state" and not record.get("sent_at"):
                owned, owner_detail = ownership.get(
                    record["attempt_id"], ("unknown", "pane=unread")
                )
                if owned == "non-owner":
                    log_stage(
                        "CC-PROMPT-NON-OWNER",
                        key,
                        f"attempt={record['attempt_id']} "
                        f"reason=prompt-from-non-owner-process {owner_detail} "
                        f"prompt_head={_prompt_head(prompt_text)} "
                        f"state={record.get('state')} decision=attempt-kept",
                    )
                    continue
                _write_user_prompt_debug_capture(
                    key, "user-prompt-before-fast-release", payload
                )
                state_before = record.get("state")
                shape = _prompt_shape(payload.get("prompt"))
                store.consume(record)
                store.update(
                    record,
                    "REFUSED",
                    reason="user-prompt-before-fast-release",
                    prompt_shape=shape,
                    prompt_origin=origin,
                    prompt_origin_evidence=evidence,
                    prompt_head=_prompt_head(prompt_text),
                    terminal_at=_now(),
                )
                # Every field here was missing on 2026-10-10 (ISSUE-REF), so the
                # refusal of attempt 8bed373b by example-user's own queued prompt was
                # misread as a /goal check-in until the ledger and transcript
                # were joined by hand.
                log_stage(
                    "CC-REFUSED-TERMINAL",
                    key,
                    f"attempt={record['attempt_id']} "
                    "reason=user-prompt-before-fast-release "
                    f"origin={origin} origin_evidence={evidence} "
                    f"prompt_shape={_log_token(shape)} "
                    f"prompt_head={_prompt_head(prompt_text)} "
                    f"state_before={state_before} "
                    f"ownership={owned} {owner_detail}",
                )
        # An idle confirmation only describes the input state before this prompt.
        store._clear_idle_prompt_confirmed_locked(key)
        pending = store.pending(key, "continuation")
        if not pending:
            return
        prompt = payload.get("prompt")
        for record in pending:
            if (
                len(pending) == 1
                and isinstance(prompt, str)
                and prompt == record.get("sent_command")
            ):
                store.update(record, "CONTINUATION_CONFIRMED", confirmed_at=_now())
                log_stage("CC-CONT-CONFIRM", key, f"attempt={record['attempt_id']}")
            else:
                _transition_failure(
                    store, record, continuation=True, reason="prompt-mismatch"
                )


def _is_save_state_skill(payload: dict[str, Any]) -> bool:
    """Recognize Claude Code's Skill(save-state) invocation from hook payload data."""
    if payload.get("tool_name") != "Skill":
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    return any(
        tool_input.get(field) == "save-state" for field in ("skill", "name", "command")
    )


def _is_session_memory_write(payload: dict[str, Any]) -> bool:
    """Whether this is a Write/Edit into the session's own memory tree.

    Memory is the one artifact compaction cannot reconstruct, so a save-state must always
    be able to write it. Scoped to the memory tree specifically: a blanket Write allowance
    would let anything be written at FORCE, which is not a scoped allowance at all.
    """
    if payload.get("tool_name") not in ("Write", "Edit"):
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    raw = tool_input.get("file_path")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        candidate = Path(raw).expanduser().resolve()
        root = (Path.home() / ".claude" / "projects").resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return root in candidate.parents and "memory" in candidate.parts


def _is_permitted_force_gate_segment(tokens: list[str], cwd: str | None = None) -> bool:
    """Whether one chained segment is a self-rescue command or on the gate's own lists."""
    from ai_chassis.prompt_injection import self_rescue

    if self_rescue.permits_tokens(tokens, cwd) is not None:
        return True
    if Path(tokens[0]).name in _FORCE_GATE_ALLOWED_EXECUTABLES:
        return True
    return tuple(tokens[:2]) in _FORCE_GATE_ALLOWED_COMMAND_PREFIXES


def _is_permitted_force_gate_command(command: object, cwd: str | None = None) -> bool:
    """Whether a Bash command is one of the few save-state cannot finish without.

    Deliberately strict: any unquoted pipe, redirection, subshell, background `&`,
    substitution or expansion is refused, because each can hide an arbitrary command
    inside a permitted one -- which would make this allowlist decorative rather than a
    boundary. Command substitution is refused even inside double quotes, because bash
    expands it there.

    A chain joined by `&&`, `;` or `||` is permitted iff EVERY segment is (ISSUE-REF D-3),
    so `ctx-pct && ctx-pct --advice` passes and `git status && git push` does not.

    Fails closed: unbalanced quotes refuse, and an argument whose ENTIRE value is
    punctuation (`--notes ';'`) is read as an operator once lexed. Lexing and every
    refusal live in `ai_chassis.prompt_injection.self_rescue.split_command`.
    """
    from ai_chassis.prompt_injection import self_rescue

    segments = self_rescue.split_command(command)
    if segments is None:
        return False
    return all(_is_permitted_force_gate_segment(tokens, cwd) for tokens in segments)


def _force_gate_scoped_allowance(payload: dict[str, Any]) -> str | None:
    """The set permitted even with no grace marker armed, named; None when not permitted.

    Returns the self-rescue rule (`self-rescue:<rule>`) when that is what permits the
    call, checked FIRST so no band can deny a compaction's own steps, else `scoped`.

    This is the defence-in-depth half of the fix: even if the marker was never armed, or
    expired while the session was genuinely idle, save-state can still record state and
    reach its own continuation. None of it is destructive or outward-facing -- the task
    panel is a display mirror, the writes are confined to memory, and `git commit` cannot
    publish anything.
    """
    from ai_chassis.prompt_injection import self_rescue

    rule = self_rescue.permits_tool_call(payload)
    if rule is not None:
        return rule
    if payload.get("tool_name") in _FORCE_GATE_ALWAYS_ALLOWED_TOOLS:
        return "scoped"
    if _is_session_memory_write(payload):
        return "scoped"
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    cwd = payload.get("cwd")
    if _is_permitted_force_gate_command(
        tool_input.get("command"), cwd if isinstance(cwd, str) and cwd else None
    ):
        return "scoped"
    return None


def _transportless_release_checkpoint(
    store: ResumeStore, key: str, *, now: int | None = None
) -> dict[str, Any] | None:
    """This episode's published resume artifact, or None -- the release's first condition.

    A save-state-origin compact attempt exists ONLY after `save-resume` has fsynced the
    outbox artifact into place and appended its history line (see :func:`publish_resume`,
    which calls :func:`reserve_save_state_compact` immediately afterwards). So the record's
    existence is the store's own evidence that a checkpoint was published, and that is what
    the release turns on.

    STATE IS DELIBERATELY NOT FILTERED. With no transport the attempt is driven terminal --
    `REFUSED`, reason `no-tmux` -- by the fallback path, which is precisely the moment the
    deadlock is confirmed. A predicate that required a live attempt would therefore stop
    releasing exactly when it is needed. What matters is the artifact, not the attempt's fate.

    Bounded by :data:`_TRANSPORTLESS_CHECKPOINT_TTL_SECONDS` rather than latched, so a
    checkpoint the session has long since worked past does not keep the gate open. A
    future-dated stamp (clock skew, or a corrupt record) fails CLOSED: it does not count, and
    the deny holds.
    """
    now = _now() if now is None else now
    for record in store.read_attempts(key):
        if (
            record.get("attempt_type") != "compact"
            or record.get("origin") != "save-state"
        ):
            continue
        armed_at = record.get("armed_at")
        if not isinstance(armed_at, int) or isinstance(armed_at, bool):
            continue
        if 0 <= now - armed_at <= _TRANSPORTLESS_CHECKPOINT_TTL_SECONDS:
            return record
    return None


def _transportless_release_notice(reason: str) -> str:
    """What a released session is told, in one paragraph it reads on the denied call."""
    minutes = _TRANSPORTLESS_CHECKPOINT_TTL_SECONDS // 60
    return (
        f"Context is at FORCE ({_force_band_percent()}%+) and this session has NO send "
        f"transport (reason={reason}), so the supervisor can never type `/compact` into a "
        "pane and there is no assistant-callable `/compact` either. Your resume artifact IS "
        "published, so the FORCE deny has nothing left to protect here and is NOT being "
        "enforced -- holding it would only pin this session below the one compaction route "
        "that still works: Claude Code's own auto-compact, which needs no pane at all. "
        f"Re-run /save-state if you do substantial further work; this release lapses "
        f"{minutes} minutes after the checkpoint and the deny re-arms. Permission for this "
        "call is unchanged -- the normal permission flow decides it, exactly as it would "
        "below the band."
    )


def _force_gate_denial_reason() -> str:
    """What a denied call is told it may still run.

    Enumerated FROM the allowlists rather than restated, for the same reason the band number
    is derived: a hand-written list goes stale the moment the allowlist moves, and a denial
    that misdescribes what is permitted is worse than a bare one.
    """
    from ai_chassis.prompt_injection import self_rescue

    allowed_tools = ", ".join(sorted(_FORCE_GATE_ALWAYS_ALLOWED_TOOLS))
    allowed_commands = ", ".join(
        sorted(
            set(_FORCE_GATE_ALLOWED_EXECUTABLES)
            | set(self_rescue.SELF_RESCUE_EXECUTABLES)
            | {" ".join(prefix) for prefix in _FORCE_GATE_ALLOWED_COMMAND_PREFIXES}
        )
    )
    subcommands = "|".join(sorted(self_rescue.LIFECYCLE_SUBCOMMANDS))
    held_verbs = "|".join(sorted(self_rescue.HELD_CLI_VERBS))
    return (
        f"Context is at FORCE ({_force_band_percent()}%+). Run /save-state "
        "immediately; tool use resumes when compaction starts. Still permitted "
        f"meanwhile: {allowed_tools}; a Write or Edit under your memory "
        f"directory; and a Bash command, or an &&/;/|| chain of them, each running "
        f"one of: {allowed_commands}, or `python -m {self_rescue.SUPERVISOR_MODULE} "
        f"{subcommands}`, or `python {self_rescue.HELD_CLI_SCRIPT} {held_verbs}`, or "
        f"`python {self_rescue.FORMAT_DECISION_SCRIPT} --form chat <path>`. "
        "Anything else is denied because it could publish or run "
        "something arbitrary, not because it is expensive."
    )


def force_gate_output(
    payload: dict[str, Any], key: str, *, tmux: TmuxAdapter | None = None
) -> dict[str, Any] | None:
    """Deny FORCE-tier tools except the save-state-to-compact escape sequence.

    THE DENY IS NOT UNCONDITIONAL, and the condition is the escape route (ISSUE-REF). Where
    the harness owns a tmux transport, holding the deny until it sends `/compact` is what the
    band is for. Where it owns none, the deny cannot buy a compaction -- only a checkpoint --
    so once the checkpoint is published it stops protecting anything and starts stranding the
    session below the level at which Claude Code would have compacted it for free.

    RELEASE IS NOT PERMISSION. The release path emits `additionalContext` and NO
    `permissionDecision`, because `allow` from a PreToolUse hook auto-approves the call and
    bypasses the permission system -- which would quietly convert a context gate into a
    blanket authorization, a push included. Handing the call back to the normal flow is the
    whole point. (The `additionalContext`-only shape is what `context-high-notice.sh` already
    emits for RECOMMEND/URGE/URGENT, so it is the established one here.)

    The transport is probed ONLY on the path that would otherwise deny, and only once a
    checkpoint is known to exist, so a permitted call at FORCE costs exactly what it did
    before. The release path logs no stage: its notice is delivered into the transcript
    itself, which is more durable evidence than a log line the session cannot see.

    Two stages ARE logged (ISSUE-REF): `CC-GATE-ALLOW reason=self-rescue:<rule>` when a
    self-rescue rule is what allowed the call, and `CC-GATE-DENY` on every deny, so a
    blocked compaction step is visible in `resume.log` without the transcript.
    """
    if not _enabled() or not _enrolled(key):
        return None
    store = ResumeStore()
    tool_name = _log_token(payload.get("tool_name"))
    if _is_save_state_skill(payload):
        store.mark_save_state_active(key)
        log_stage(
            "CC-GATE-ALLOW",
            key,
            f"reason=self-rescue:save-state-skill tool={tool_name}",
        )
        decision = "allow"
        reason = None
    elif store.save_state_active(key):
        # Re-stamp on every permitted call, so the timeout bounds INACTIVITY rather than
        # total elapsed time. Measured from the arm instant it expired under a save-state
        # that was still working -- routine here, where one `bd` filing can take minutes --
        # and every remaining step was then denied with a reason telling the session to run
        # the skill it was already running. PreCompact still clears the marker explicitly,
        # so this does not extend the window past compact start.
        store.mark_save_state_active(key)
        decision = "allow"
        reason = None
    elif allowance := _force_gate_scoped_allowance(payload):
        # Deliberately does NOT arm the window: one permitted call must not grant the rest.
        if allowance.startswith("self-rescue:"):
            log_stage("CC-GATE-ALLOW", key, f"reason={allowance} tool={tool_name}")
        decision = "allow"
        reason = None
    else:
        checkpoint = _transportless_release_checkpoint(store, key)
        # A paneless session is exactly the one the release exists for, and before
        # ISSUE-REF it was the one case that never got it: a tmux server answers, so
        # the host-level question said "transport available", and the deny held on a
        # session that can never be sent `/compact` -- pinning it below the only
        # compaction route it has left.
        # Deliberately NOT `compact_transport_reason`: a bare pty session keeps this
        # release even though its FIFO can carry `/compact` (ISSUE-REF), because the
        # FIFO send waits on an input-idle proof the deny would otherwise pin.
        transport_reason = (
            (
                transport_unavailable_reason((tmux or TmuxAdapter()).version())
                or session_pane_reason()
            )
            if checkpoint is not None
            else None
        )
        if transport_reason is not None:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": _transportless_release_notice(
                        transport_reason
                    ),
                }
            }
        log_stage("CC-GATE-DENY", key, f"tool={tool_name}")
        decision = "deny"
        reason = _force_gate_denial_reason()
    output: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason is not None:
        output["hookSpecificOutput"]["permissionDecisionReason"] = reason
    return output


def reconcile_timeouts(store: ResumeStore, key: str, attempt_type: str) -> None:
    """Resolve overdue ACK waits once, terminally; this path never sends anything."""
    try:
        start_timeout = int(
            os.environ.get("AIH_INJECT_START_ACK_TIMEOUT_SECONDS", "30")
        )
        done_timeout = int(os.environ.get("AIH_INJECT_DONE_ACK_TIMEOUT_SECONDS", "120"))
        cont_timeout = int(
            os.environ.get("AIH_INJECT_CONT_CONFIRM_TIMEOUT_SECONDS", "30")
        )
    except ValueError:
        return
    now = _now()
    for record in store.read_attempts(key):
        if record.get("attempt_type") != attempt_type:
            continue
        state = record.get("state")
        sent = int(record.get("sent_at") or now)
        if state == "SENT" and now - sent > start_timeout:
            _transition_failure(
                store, record, continuation=False, reason="start-ack-timeout"
            )
        elif state == "STARTED" and now - sent > done_timeout:
            _transition_failure(
                store, record, continuation=False, reason="completion-ack-timeout"
            )
        elif state == "CONTINUATION_SENT":
            _reconcile_continuation_timeout(store, record, now, sent, cont_timeout)


def _continuation_queued_retries() -> int:
    """How many times a queued continuation's confirmation wait may be extended."""
    try:
        value = int(
            os.environ.get(
                "AIH_INJECT_CONT_QUEUED_RETRIES",
                str(_DEFAULT_CONTINUATION_QUEUED_RETRIES),
            )
        )
    except ValueError:
        return _DEFAULT_CONTINUATION_QUEUED_RETRIES
    return value if 0 <= value <= 10 else _DEFAULT_CONTINUATION_QUEUED_RETRIES


def _reconcile_continuation_timeout(
    store: ResumeStore,
    record: dict[str, Any],
    now: int,
    sent: int,
    cont_timeout: int,
) -> None:
    """Retry a QUEUED continuation with backoff before letting it go terminal.

    THE BUG THIS REPLACES.  Confirmation requires `UserPromptSubmit` to fire with
    a prompt equal to `sent_command`, and a prompt sitting in Claude Code's queue
    has not fired it yet.  The old single `cont_timeout` therefore expired on a
    prompt that was still perfectly alive, and `CONTINUATION_AMBIGUOUS` is in
    `TERMINAL_STATES` -- so the attempt was abandoned with no retry and, worse,
    in silence: nothing told the operator their resume turn had been written off.

    Each extension doubles the window, which is why a retry counter and not a
    fixed deadline: the queue drains when the current turn ends, and turn length
    is exactly what nobody can predict here.  Only a landing classified `queued`
    earns this -- a send that never landed at all is still terminalised on the
    first pass, because there is nothing to wait for.
    """
    retries = record.get("confirm_retries")
    retries = (
        retries if isinstance(retries, int) and not isinstance(retries, bool) else 0
    )
    if record.get("landing") != "queued":
        if now - sent > cont_timeout:
            _transition_failure(
                store, record, continuation=True, reason="confirmation-timeout"
            )
        return
    budget = _continuation_queued_retries()
    # The window this attempt has already earned, doubling per extension.
    window = cont_timeout * (2**retries)
    if now - sent <= window:
        return
    if retries >= budget:
        _transition_failure(
            store, record, continuation=True, reason="queued-confirmation-exhausted"
        )
        write_continuation_fault_notice(
            str(record["key"]),
            "queued-confirmation-exhausted",
            attempt_id=str(record.get("attempt_id") or ""),
            retries=retries,
            root=store.root,
        )
        return
    store.update(
        record,
        "CONTINUATION_SENT",
        confirm_retries=retries + 1,
    )
    log_stage(
        "CC-CONT-REQUEUE",
        str(record["key"]),
        f"attempt={record.get('attempt_id')} retry={retries + 1}/{budget} "
        f"landing=queued window={window}",
    )


def _rfc3339_utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _bounded_capture_region(lines: list[str], border_indexes: list[int]) -> str:
    start = (
        min(border_indexes)
        if border_indexes
        else max(0, len(lines) - _READINESS_FALLBACK_REGION_LINES)
    )
    return "\n".join(lines[start:])


def _strip_sgr(value: str) -> str:
    return _SGR_SEQUENCE.sub("", value)


def _composer_attribute_class(raw_content: str, content_start: int) -> str:
    """Classify visible composer cells by the measured Claude Code SGR style."""
    styled: list[tuple[str, str]] = []
    style = "normal"
    offset = 0
    for match in _SGR_SEQUENCE.finditer(raw_content):
        for character in raw_content[offset : match.start()]:
            styled.append((character, style))
        sequence = match.group(0)
        parameters = sequence[2:-1]
        if not parameters or all(
            part in {"", "0", "39", "49"} for part in parameters.split(";")
        ):
            style = "normal"
        elif sequence in _SUGGESTION_SGR_SEQUENCES:
            style = "suggestion"
        else:
            style = "unknown"
        offset = match.end()
    for character in raw_content[offset:]:
        styled.append((character, style))
    classes = {
        cell_style
        for character, cell_style in styled[content_start:]
        if not character.isspace()
    }
    if not classes:
        return "normal"
    if len(classes) > 1:
        return "mixed"
    return classes.pop()


def _matches_staged_text(composer_text: str | None, staged_text: str | None) -> bool:
    """Return whether the composer holds exactly the payload we staged (ISSUE-REF).

    THE ONE PLACE THIS COMPARISON IS ALLOWED TO HAPPEN. There were four independent ``==``
    comparisons between composer text and staged text, and the first fix corrected only one,
    so three sites went on refusing every correctly staged payload. ``tests`` carries an AST
    canary that fails if a bare ``==`` between these two values reappears anywhere in the
    module.

    Trailing whitespace is forgiven because it is the terminal's own padding: Claude Code
    renders ``/compact`` in the composer and ``_composer_text`` returns ``"/compact "``, so an
    exact comparison against ``"/compact"`` can never succeed. Nothing else is forgiven --
    trailing or leading TEXT still fails, because pressing Enter on a composer holding
    anything else submits the wrong command.

    An absent or blank expectation never matches. Otherwise an attempt that has staged nothing
    would "recognise" an empty composer as its own payload and press Enter on it.
    """
    if not isinstance(staged_text, str) or not staged_text.strip():
        return False
    if not isinstance(composer_text, str):
        return False
    return composer_text.rstrip() == staged_text.rstrip()


def _composer_text(content: str | None) -> str | None:
    """Return composer text after the configured cursor and its one-cell gap."""
    if not isinstance(content, str):
        return None
    pattern = re.compile(
        os.environ.get("AIH_COMPOSER_CURSOR_PATTERN", _DEFAULT_COMPOSER_CURSOR_PATTERN)
    )
    cursor_end = next(
        (end for end in range(1, len(content) + 1) if pattern.fullmatch(content[:end])),
        None,
    )
    if cursor_end is None:
        return content
    value = content[cursor_end:]
    return value[1:] if value.startswith((" ", "\N{NO-BREAK SPACE}")) else value


def _is_proven_staging_target(sample: ComposerSample) -> bool:
    """Allow staging only into recognized empty content or a proven suggestion."""
    if not sample.composer_recognized:
        return False
    composer_text = _composer_text(sample.content)
    if not isinstance(composer_text, str):
        return False
    if sample.classification == "empty-composer":
        return not composer_text.strip()
    return (
        sample.classification == "composer-suggestion"
        and sample.attribute_class == "suggestion"
        and bool(composer_text.strip())
    )


def _draft_end(lines: list[str], start: int) -> int:
    """Where a titled frame with no closing rule ends: the first blank row or the pane
    bottom, at most ``_MAX_WRAPPED_COMPOSER_ROWS`` rows on (ISSUE-REF). Reading only the
    first row certified ISSUE-REF's cursor row empty while a human's draft sat below it. A blank
    first row ends it at once, so its blank content still refuses (ISSUE-REF)."""
    stop = start
    limit = min(len(lines), start + _MAX_WRAPPED_COMPOSER_ROWS)
    while stop < limit and lines[stop].strip():
        stop += 1
    return stop


def _unbounded_frame_end(lines: list[str], start: int) -> int:
    """``_draft_end``, except that a read holding only the cursor goes on to the pane
    bottom (ISSUE-REF). A human draft whose first line is empty shows the cursor row, a
    blank row, then text; stopping at the blank row certified it empty and typed over it.
    Any non-blank row below therefore belongs to the frame and reads as a draft."""
    stop = _draft_end(lines, start)
    read = "\n".join(lines[start:stop])
    if not read.strip() or (_composer_text(read) or "").strip():
        return stop
    below = [row for row in range(stop, len(lines)) if lines[row].strip()]
    return below[-1] + 1 if below else stop


def _analyse_composer_capture(capture: str, *, phase: str) -> ComposerSample:
    """Classify the current viewport without interpreting background-task UI."""
    pattern = re.compile(
        os.environ.get("AIH_COMPOSER_CURSOR_PATTERN", _DEFAULT_COMPOSER_CURSOR_PATTERN)
    )
    claude_code_notice = next(
        (
            notice
            for notice, notice_pattern in _CLAUDE_CODE_NOTICE_PATTERNS
            if notice_pattern.search(capture)
        ),
        None,
    )
    raw_lines = capture.splitlines()
    lines = [_strip_sgr(line) for line in raw_lines]
    # (top border index, stripped content, raw content, frame is bounded below)
    candidates: list[tuple[int, str, str, bool]] = []
    border_indexes: list[int] = []
    for index, line in enumerate(lines):
        if not _is_composer_top_border(line):
            continue
        titled = bool(_COMPOSER_TOP_BORDER.fullmatch(line) and " " in line.strip())
        if titled:
            bottom = next(
                (
                    following
                    for following in range(index + 1, len(lines))
                    if _is_composer_top_border(lines[following])
                ),
                None,
            )
            stop = (
                bottom if bottom is not None else _unbounded_frame_end(lines, index + 1)
            )
            content_lines = lines[index + 1 : stop]
            raw_content_lines = raw_lines[index + 1 : stop]
            candidates.append(
                (
                    index,
                    "\n".join(content_lines),
                    "\n".join(raw_content_lines),
                    bottom is not None,
                )
            )
            border_indexes.append(index)
            continue
        if index + 1 >= len(lines):
            continue
        cursor_line = lines[index + 1].strip()
        blank_before_rule = (
            not cursor_line
            and index + 2 < len(lines)
            and _is_composer_top_border(lines[index + 2])
        )
        if pattern.fullmatch(cursor_line) or blank_before_rule:
            candidates.append((index, "", "", True))
            border_indexes.append(index)
            continue
        # The C-6 block loop can leave hook feedback immediately above an untitled
        # composer. Its ghost suggestion is bounded by the next rule, even though
        # it is not itself an empty cursor line. Treat that bounded cursor-bearing
        # row as composer content; an unbounded footer line remains unrecognized.
        if (
            index + 2 < len(lines)
            and bool(pattern.match(cursor_line))
            and _is_composer_top_border(lines[index + 2])
        ):
            candidates.append((index, lines[index + 1], raw_lines[index + 1], True))
            border_indexes.append(index)

    region = _bounded_capture_region(lines, border_indexes)
    if not candidates:
        classification = (
            "not-obviously-occupied" if phase == "precheck" else "stage-verify-mismatch"
        )
        return ComposerSample(
            classification, region, False, claude_code_notice=claude_code_notice
        )

    top, content, raw_content, bounded = max(
        candidates, key=lambda candidate: candidate[0]
    )
    if not bounded and not content.strip():
        # ISSUE-REF: Claude Code can draw its session-titled rule BELOW a permission
        # dialog, with the task panel under it and no cursor row. A region starting at
        # that rule hid the dialog from `_no_composer_frame`, so ai-cli-ISSUE-REF was told
        # to resize or type /compact, which the dialog would have taken as "1. Yes".
        # Keep the fallback window above the rule as this frame's evidence.
        region = "\n".join(lines[max(0, top - _READINESS_FALLBACK_REGION_LINES) :])
        # ISSUE-REF: absence of text in a frame we cannot see the bottom of is NOT evidence
        # that the composer is empty. ISSUE-REF rendered its composer's titled top rule as the
        # last row of the pane 45 times in a row -- the cursor row and the closing rule were
        # below the viewport because the TUI had not repainted for the pane height. This branch
        # used to derive zero content rows from that and report `empty-composer` with
        # `composer_recognized=True`, so `stage-verify` never saw the payload it had just typed
        # and the next precheck authorised typing it again: four `/compact`s in one composer,
        # zero submissions.
        #
        # Text that IS visible stays trustworthy even in a truncated frame -- seeing a draft is
        # positive evidence, and downgrading it would throw away the only warning the user gets.
        return ComposerSample(
            "not-obviously-occupied"
            if phase == "precheck"
            else "stage-verify-mismatch",
            region,
            False,
            claude_code_notice=claude_code_notice,
        )
    exact_content = _composer_text(content) or ""
    content_start = len(content) - len(exact_content)
    attribute_class = _composer_attribute_class(raw_content, content_start)
    if phase == "stage-verify":
        # Compare on trailing-whitespace-normalised text (ISSUE-REF).
        #
        # This was `exact_content == "/compact"`, and it could never pass on a real
        # terminal. `_composer_text` strips the cursor glyph and exactly ONE leading space
        # or NO-BREAK SPACE, and strips nothing from the end, so Claude Code's composer
        # yields "/compact " -- nine characters. The verify therefore refused every
        # correctly staged payload, Enter was never sent, and the retry appended a second
        # staging onto the first, leaving the user's prompt box holding "/compact/compact".
        # Measured on ISSUE-REF 2026-09-17: stagings=2, submissions=0.
        #
        # Only TRAILING WHITESPACE is forgiven, and that asymmetry is load-bearing: it is
        # the terminal's own padding. Trailing or leading TEXT still refuses, because Enter
        # on a composer holding anything else submits the wrong command. Both directions
        # are covered by tests, and the negative controls pass before and after this change.
        #
        # The fixtures for those tests are the REAL captured panes from the failure, kept in
        # tests/fixtures/. The pre-existing hand-written `_STAGED_COMPACT` fixture has no
        # trailing space, so it classified stage-verified throughout and the suite stayed
        # green while the live path never once submitted.
        return ComposerSample(
            (
                "stage-verified"
                if _matches_staged_text(exact_content, "/compact")
                and attribute_class in _SAFE_STAGED_ATTRIBUTE_CLASSES
                else "stage-verify-mismatch"
            ),
            region,
            True,
            content,
            claude_code_notice=claude_code_notice,
            attribute_class=attribute_class,
        )
    if exact_content == "/compact":
        return ComposerSample(
            (
                "composer-suggestion"
                if attribute_class == "suggestion"
                else "composer-nonempty"
            ),
            region,
            True,
            content,
            claude_code_notice=claude_code_notice,
            attribute_class=attribute_class,
            raw_content_debug=(
                raw_content if attribute_class in {"unknown", "mixed"} else None
            ),
        )
    empty = not content.strip() or bool(pattern.fullmatch(content.strip()))
    return ComposerSample(
        (
            "empty-composer"
            if empty
            else "composer-suggestion"
            if attribute_class == "suggestion"
            else "composer-nonempty"
        ),
        region,
        True,
        content,
        claude_code_notice=claude_code_notice,
        attribute_class=attribute_class,
        raw_content_debug=(
            raw_content
            if not empty and attribute_class in {"unknown", "mixed"}
            else None
        ),
    )


def _active_readiness_attempt_ids(root: Path) -> set[str]:
    active: set[str] = set()
    for path in (root / "attempts").glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        attempt_id = record.get("attempt_id")
        if (
            isinstance(attempt_id, str)
            and attempt_id
            and record.get("state") not in TERMINAL_STATES
        ):
            active.add(attempt_id)
    return active


def _housekeep_readiness_trails(root: Path) -> None:
    global _readiness_housekeeping_last_run
    now = time.time()
    previous = _readiness_housekeeping_last_run
    if previous is not None and 0 <= now - previous < (
        _READINESS_HOUSEKEEPING_INTERVAL_SECONDS
    ):
        return
    _readiness_housekeeping_last_run = now
    directory = root / "readiness"
    active = _active_readiness_attempt_ids(root)
    try:
        paths = list(directory.glob("*.jsonl"))
    except OSError:
        return
    for path in paths:
        try:
            age = now - path.stat().st_mtime
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            attempt_ids = {
                record.get("attempt_id")
                for record in records
                if isinstance(record, dict)
            }
            if age > _READINESS_TRAIL_RETENTION_SECONDS and not (active & attempt_ids):
                path.unlink()
        except (OSError, json.JSONDecodeError):
            continue


def _jsonl_line(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise OSError("readiness trail write made no progress")
        offset += written


def _write_readiness_evaluation(
    *,
    root: Path,
    key: str,
    attempt_id: str,
    pane_id: str,
    samples: list[ComposerSample],
    phases: list[str],
    send_attempted: bool,
    send_result: str,
    text_may_have_been_staged: bool,
    goal_active: str = "unknown",
    idle_proof: str | None = None,
    landing: str = "none",
    stagings: int = 0,
    submissions: int = 0,
    backoff_seconds: int = 0,
    last_staged_text: str | None = None,
) -> None:
    """Append one complete evaluation, or one terminal cap marker, best-effort."""
    try:
        directory = root / "readiness"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        _housekeep_readiness_trails(root)
        path = directory / (
            f"{_safe_marker_component(key)}-{_safe_marker_component(attempt_id)}.jsonl"
        )
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            existing = path.read_bytes() if path.stat().st_size else b""
            if b'"record_type":"truncated"' in existing:
                return
            sequence = 1
            for line in existing.splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("record_type") == "sample" and isinstance(
                    record.get("sequence"), int
                ):
                    sequence = max(sequence, int(record["sequence"]) + 1)
            timestamp = _rfc3339_utc_now()
            records: list[dict[str, Any]] = []
            for sample, phase in zip(samples, phases, strict=True):
                try:
                    composer_text = _composer_text(sample.content)
                except re.error:
                    composer_text = sample.content
                starts_with_last_staged_text = bool(
                    last_staged_text
                    and isinstance(composer_text, str)
                    and composer_text.startswith(last_staged_text)
                )
                sample_record = {
                    "record_type": "sample",
                    "timestamp": timestamp,
                    "attempt_id": attempt_id,
                    "sequence": sequence,
                    "pane_id": pane_id,
                    "phase": phase,
                    "classification": sample.classification,
                    "attribute_class": sample.attribute_class,
                    "region": sample.region,
                    "goal_active": goal_active,
                    "claude_code_notice": sample.claude_code_notice,
                    "content_length": (
                        len(composer_text) if isinstance(composer_text, str) else None
                    ),
                    "content_starts_with_last_staged_text": (
                        starts_with_last_staged_text
                    ),
                    "content_repeats_last_staged_text": bool(
                        starts_with_last_staged_text
                        and composer_text.count(last_staged_text) > 1
                    ),
                }
                if sample.raw_content_debug is not None:
                    sample_record["raw_content_debug"] = sample.raw_content_debug
                records.append(sample_record)
                sequence += 1
            final_sequence = sequence - 1
            records.append(
                {
                    "record_type": "outcome",
                    "timestamp": timestamp,
                    "attempt_id": attempt_id,
                    "sample_sequence": final_sequence,
                    "send_attempted": send_attempted,
                    "send_result": send_result,
                    "failure_reason": (
                        None
                        if send_result in {"not-attempted", "succeeded"}
                        else send_result
                    ),
                    "text_may_have_been_staged": text_may_have_been_staged,
                    "idle_proof": idle_proof,
                    "landing": landing,
                    "stagings": stagings,
                    "submissions": submissions,
                    "backoff_seconds": backoff_seconds,
                    "goal_active": goal_active,
                    "claude_code_notice": samples[-1].claude_code_notice,
                }
            )
            payload = b"".join(_jsonl_line(record) for record in records)
            marker = _jsonl_line(
                {
                    "record_type": "truncated",
                    "timestamp": timestamp,
                    "attempt_id": attempt_id,
                    "first_omitted_sample_sequence": records[0]["sequence"],
                    "reason": "size-cap",
                }
            )
            reserved_marker = _jsonl_line(
                {
                    "record_type": "truncated",
                    "timestamp": "9999-12-31T23:59:59.999999Z",
                    "attempt_id": attempt_id,
                    "first_omitted_sample_sequence": sequence,
                    "reason": "size-cap",
                }
            )
            if (
                len(existing) + len(payload) + len(reserved_marker)
                > _READINESS_TRAIL_MAX_BYTES
            ):
                if len(existing) + len(marker) <= _READINESS_TRAIL_MAX_BYTES:
                    _write_all(fd, marker)
                return
            _write_all(fd, payload)
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError):
        return


class TmuxAdapter:
    def __init__(self, command: tuple[str, ...] = ("tmux",), *, deadline=None) -> None:
        self.command = command
        # Seconds, read at call time. The event transport sets it; the legacy
        # supervisor leaves it unset and keeps its unbounded calls.
        self.deadline = deadline
        self.last_capture = ""
        self.last_not_ready_reason: str | None = None

    def _run(self, command, **options):
        if self.deadline is None:
            return subprocess.run(command, **options)
        try:
            return subprocess.run(command, timeout=self.deadline(), **options)
        except subprocess.TimeoutExpired:
            # ValueError reaches LifecycleChannel without being reclassified as
            # a missing composer or permanent refusal by the transport adapter.
            raise ValueError("command-deadline") from None

    def client_version(self) -> str | None:
        """The version of the tmux BINARY on PATH.

        Useful to REPORT, never to gate on: see :meth:`version`.
        """
        result = self._run(
            [*self.command, "-V"], capture_output=True, text=True, check=False
        )
        if result.returncode != 0 or not result.stdout.startswith("tmux "):
            return None
        return result.stdout.strip().removeprefix("tmux ")

    def server_version(self) -> str | None:
        """The version of the running tmux SERVER, via its own ``#{version}``.

        ``None`` when no server is reachable or the answer is not version-shaped.
        """
        result = self._run(
            [*self.command, "display-message", "-p", "#{version}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        # A tmux version is "3.7", "3.7c", or "next-3.8". Anything else is not a
        # version, and must not be handed to the floor check as if it were.
        if not value or not re.match(r"^(?:next-)?\d", value):
            return None
        return value

    def version(self) -> str | None:
        """The version that GOVERNS the transport — the server's, not the client's.

        This is the floor check's input, and the distinction is the whole defect
        (ISSUE-REF). ``TMUX_FINGERPRINT_FORMAT`` is expanded by the **server**, so the
        server's version decides whether the fingerprint comes back in a shape the
        transport can parse. The client binary's version says nothing about that, and the
        two routinely disagree during a migration, because a running server keeps its
        version until every session on it has exited.

        Measured 2026-09-06 with an above-floor tmux on PATH and a live below-floor
        server::

            tmux -V                            -> above the floor, so the gate PASSED
            tmux display-message '#{version}'  -> below it (the server does the expanding)
            fingerprint via the newer client   -> '$0|1|W/i:@0[%0;]'

        That fingerprint still carries the **literal** ``W/i:`` because the older server
        does not know the specifier, so gating on ``-V`` would have declared the transport
        usable and then mis-parsed — the "send ``/compact`` to the wrong pane" failure the
        floor exists to prevent. Gating on the client makes the floor unsound wherever it
        is set.

        Returns ``None`` when no server is reachable: without a server there is no
        transport, and substituting the binary's own version there is exactly what
        reintroduces the bug. Callers already refuse on a falsy version, so this fails
        closed.
        """
        return self.server_version()

    def fingerprint(self, pane_id: str) -> str | None:
        """The target's identity fingerprint, or None when the target does not resolve.

        A NONEXISTENT target is not an error to tmux (ISSUE-REF).  ``display-message
        -p -t`` exits 0 and expands every unknown field to the empty string, so the
        separators in :data:`TMUX_FINGERPRINT_FORMAT` come back on their own.
        Measured 2026-09-26 against a live 3.7c server::

            -t "$TMUX_PANE"      -> rc=0  '$27|2VqQFX...|1|@27[%27=93138=0;]'
            -t %999999           -> rc=0  '|||'
            -t no-such-session   -> rc=0  '|||'

        ``'|||'`` is TRUTHY, and that is the whole defect: a session with no pane of
        its own passes ``TMUX_PANE or key`` as the target, gets ``'|||'`` back, binds
        ARMED->BOUND against a target that does not exist, logs ``pane_binding: yes``,
        and so never reaches the decline path that writes the transport notice.  The
        operator got SILENCE where the design promises a warning, and at FORCE the
        same session is denied without the transportless release, because every caller
        was asking whether a tmux SERVER exists host-wide rather than whether THIS
        session has a pane.

        The first field is ``#{session_id}``, which is ``$N`` for every target that
        resolves and empty for one that does not, so the fingerprint already in hand
        settles it -- no second probe that could race the first, and no new tmux
        question whose own failure mode would have to be reasoned about.
        """
        result = self._run(
            [
                *self.command,
                "display-message",
                "-p",
                "-t",
                pane_id,
                fingerprint_format(self.command, self._run),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        if not value or not value.split("|", 1)[0]:
            return None
        return value

    def ready(self, pane_id: str, _fingerprint: str, _attempt_id: str) -> bool:
        """Read a stable, empty Claude Code composer without writing to the pane."""
        self.last_not_ready_reason = None
        samples: list[bool] = []
        for _ in range(2):
            result = self._run(
                [*self.command, "capture-pane", "-p", "-t", pane_id],
                capture_output=True,
                text=True,
                check=False,
            )
            self.last_capture = result.stdout
            if result.returncode != 0:
                return False
            lines = result.stdout.splitlines()
            # Locate the isolated cursor line first, then require the immediately
            # preceding top rule. This ignores arbitrary footer lines and cannot
            # mistake the lower rule plus trailing pane whitespace for a composer.
            nonempty_titled_composer = False
            for index in range(len(lines) - 1, 0, -1):
                if not _is_composer_top_border(lines[index - 1]):
                    continue
                if _is_empty_composer_content(lines, index):
                    samples.append(True)
                    break
                # An untitled lower border can be followed by arbitrary footer
                # text, so only the titled top border identifies a real draft.
                nonempty_titled_composer |= bool(
                    _COMPOSER_TOP_BORDER.fullmatch(lines[index - 1])
                    and " " in lines[index - 1].strip()
                )
            else:
                if nonempty_titled_composer:
                    self.last_not_ready_reason = "composer-nonempty"
                return False
        return all(samples)

    def compact_sample(self, pane_id: str, *, phase: str) -> ComposerSample:
        """Capture and classify one compact pre-check or staged-command frame."""
        self.last_not_ready_reason = None
        try:
            result = self._run(
                [*self.command, "capture-pane", "-p", "-e", "-t", pane_id],
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            classification = (
                "not-ready" if phase == "precheck" else "stage-verify-mismatch"
            )
            if phase == "precheck":
                self.last_not_ready_reason = "not-ready"
            return ComposerSample(
                classification,
                "",
                False,
                error_reason=f"capture:{type(error).__name__}",
            )
        self.last_capture = result.stdout
        if result.returncode != 0:
            classification = (
                "not-ready" if phase == "precheck" else "stage-verify-mismatch"
            )
            if phase == "precheck":
                self.last_not_ready_reason = "not-ready"
            return ComposerSample(
                classification,
                _bounded_capture_region(result.stdout.splitlines(), []),
                False,
                error_reason="tmux",
            )
        try:
            sample = _analyse_composer_capture(result.stdout, phase=phase)
        except re.error:
            sample = ComposerSample(
                "not-ready" if phase == "precheck" else "stage-verify-mismatch",
                _bounded_capture_region(result.stdout.splitlines(), []),
                False,
                error_reason="cursor-configuration",
            )
        except Exception as error:  # noqa: BLE001 - parser failures are fail-closed
            sample = ComposerSample(
                "not-ready" if phase == "precheck" else "stage-verify-mismatch",
                _bounded_capture_region(result.stdout.splitlines(), []),
                False,
                error_reason=f"parser:{type(error).__name__}",
            )
        if phase == "precheck" and sample.classification in {
            "composer-nonempty",
            "not-ready",
        }:
            self.last_not_ready_reason = sample.classification
        return sample

    def _status_warning(self, pane_id: str, text: str) -> None:
        """Show ``text`` on the pane's status line, never in the composer.

        ``-d`` because tmux's default ``display-time`` is 750ms (measured on
        example-project-ec2), which made every one of these a sub-second flash: ISSUE-REF's
        composer-blocked warning at 01:02:22 on 2026-10-13 was the only sign its
        compact was waiting, and it was gone before anyone could read it
        (ISSUE-REF).  A keypress still dismisses it early.
        """
        self._run(
            [
                *self.command,
                "display-message",
                "-t",
                pane_id,
                "-d",
                str(PANE_WARNING_DISPLAY_MS),
                text,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def warn_composer_blocked(self, pane_id: str) -> None:
        """Show a safe, non-composer warning for a pending compact draft block."""
        self._status_warning(
            pane_id, "AIH: /compact pending — submit or clear the composer draft"
        )

    def warn_composer_offscreen(self, pane_id: str) -> None:
        """Say the composer is not on screen; telling the user to clear a draft
        that does not exist was the old no-composer message (ISSUE-REF)."""
        self._status_warning(
            pane_id,
            "AIH: /compact pending — no prompt line on screen; "
            "resize the window or type /compact",
        )

    def warn_permission_prompt(self, pane_id: str) -> None:
        """Name the permission prompt holding the input area; typed text would answer it."""
        self._status_warning(
            pane_id,
            "AIH: /compact pending — a permission prompt is waiting; "
            "answer it and the compact sends",
        )

    def pane_geometry(self, pane_id: str) -> str:
        """The pane's real size as ``WxH``, read-only, or ``unknown``."""
        try:
            result = self._run(
                [
                    *self.command,
                    "display-message",
                    "-p",
                    "-t",
                    pane_id,
                    "#{pane_width}x#{pane_height}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        value = result.stdout.strip()
        if result.returncode != 0 or not re.fullmatch(r"\d+x\d+", value):
            return "unknown"
        return value

    def warn_restage(self, pane_id: str, staging_count: int) -> None:
        """Make an indefinite retry visible without touching the composer."""
        self._status_warning(pane_id, f"AIH: /compact retry staging #{staging_count}")

    def warn_long_wait(self, pane_id: str, state: str) -> None:
        """Surface a still-live lifecycle without changing its terminal state."""
        self._status_warning(pane_id, f"AIH: /compact is still pending (state={state})")

    def warn_compact_stalled(self, pane_id: str, reason: str) -> None:
        """Say on the pane, at once, that the supervisor will not send `/compact`."""
        self._status_warning(
            pane_id, f"AIH: auto /compact stopped ({reason}) - it will not be sent"
        )

    def _tmux_stdout(self, *args: str) -> str | None:
        try:
            result = self._run(
                [*self.command, *args], capture_output=True, text=True, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout if result.returncode == 0 else None

    def grow_window(self, pane_id: str, rows: int) -> WindowGrow | None:
        """Make a window too short for Claude Code's composer ``rows`` tall (ISSUE-REF).

        Claude Code renders full-screen and clips its bottom stack, composer included,
        when the terminal is shorter than that stack; a redraw cannot bring it back,
        only height can. ``resize-window`` pins the window's ``window-size`` to
        ``manual``, so the prior window-local value is read first for
        :meth:`restore_window`. Returns None, touching nothing, when the pane is
        already ``rows`` tall or tmux refuses.
        """
        height = self._tmux_stdout(
            "display-message", "-p", "-t", pane_id, "#{pane_height}"
        )
        if height is None or not height.strip().isdigit():
            return None
        original_rows = int(height.strip())
        if original_rows >= rows:
            return None
        window_size = self._tmux_stdout(
            "show-options", "-w", "-v", "-t", pane_id, "window-size"
        )
        if window_size is None:
            return None
        if self._tmux_stdout("resize-window", "-t", pane_id, "-y", str(rows)) is None:
            return None
        return WindowGrow(pane_id, original_rows, window_size.strip() or None)

    def restore_window(self, grow: WindowGrow) -> bool:
        """Undo :meth:`grow_window`: the old height, then the old ``window-size``.

        Both steps run even if the first fails. Putting ``window-size`` back is what
        hands sizing to the attached clients again; the explicit height covers a
        window with no client, which would otherwise stay at the grown size.
        """
        resized = (
            self._tmux_stdout(
                "resize-window", "-t", grow.pane_id, "-y", str(grow.original_rows)
            )
            is not None
        )
        option = (
            ("set-option", "-w", "-t", grow.pane_id, "window-size", grow.window_size)
            if grow.window_size is not None
            else ("set-option", "-w", "-u", "-t", grow.pane_id, "window-size")
        )
        return self._tmux_stdout(*option) is not None and resized

    def write_debug_capture(
        self, key: str, pane_id: str, attempt_id: str, reason: str
    ) -> None:
        """Persist bounded diagnostics only when the code-config toggle is enabled."""
        if not _config()["debug_diagnostics_enabled"]:
            return
        capture = self.last_capture
        if not capture:
            result = self._run(
                [*self.command, "capture-pane", "-p", "-t", pane_id],
                capture_output=True,
                text=True,
                check=False,
            )
            capture = result.stdout
        root = _root_from_environment()
        path = (
            root
            / "debug"
            / (
                f"{_safe_marker_component(key)}-{int(time.time())}-"
                f"{_safe_marker_component(reason)}.txt"
            )
        )
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            contents = (
                f"reason={reason}\nattempt_id={attempt_id}\npane_id={pane_id}\n\n"
                f"{capture}"
            )
            path.write_text(
                contents,
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
        except OSError:
            return

    def fence_and_stage(self, pane_id: str, fingerprint: str, command: str) -> str:
        """Stage command text behind the pane fingerprint fence, without Enter."""
        # Empty -c selects no attached client: the explicit pane/fingerprint is
        # the authority, not tmux's most recent (possibly read-only) observer.
        predicate = (
            f"#{{==:{fingerprint_format(self.command, self._run)},{fingerprint}}}"
        )
        send = shlex.join(send_keys_command(self.command, self._run))
        try:
            result = self._run(
                [
                    *self.command,
                    "if-shell",
                    "-F",
                    "-t",
                    pane_id,
                    predicate,
                    f"{send} -t {shlex.quote(pane_id)} {shlex.quote(command)}",
                    f"display-message -p {FENCE_MISMATCH}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return "text-send-error"
        if FENCE_MISMATCH in result.stdout:
            return "fence-mismatch"
        return "succeeded" if result.returncode == 0 else "text-send-error"

    def send_enter(self, pane_id: str) -> bool:
        """Submit already-verified composer text in its own tmux call."""
        # Enter MUST be its own tmux call, never bundled into the same send-keys
        # invocation as the command text. Live-verified against a real Claude
        # Code 2.1.258 TUI (ISSUE-REF, 2026-09-02): a long command's Enter can
        # arrive before the composer finishes processing the text, leaving it
        # staged but unsubmitted; a second, separately issued Enter reliably
        # submits it. A short exact command is unaffected either way.
        try:
            enter_result = self._run(
                [
                    *self.command,
                    *send_keys_command(self.command, self._run),
                    "-t",
                    pane_id,
                    "Enter",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return enter_result.returncode == 0

    def fence_and_send(self, pane_id: str, fingerprint: str, command: str) -> bool:
        """Preserved continuation transport: fenced text, then separate Enter."""
        return self.fence_and_stage(
            pane_id, fingerprint, command
        ) == "succeeded" and self.send_enter(pane_id)


class ContextSupervisor:
    """The external actor.  It is intentionally inert until explicitly enrolled."""

    def __init__(
        self, store: ResumeStore | None = None, tmux: TmuxAdapter | None = None
    ) -> None:
        self.store = store or ResumeStore()
        self.tmux = tmux or TmuxAdapter()
        self._composer_warning_emitted = False
        self._composer_grow_active = False
        self._goal_state_tracker = _GoalStateTracker()

    def _warn_no_composer(self, pane_id: str, frame: str) -> None:
        name = (
            "warn_permission_prompt"
            if frame == "permission-dialog"
            else "warn_composer_offscreen"
        )
        warn = getattr(self.tmux, name, None)
        if callable(warn):
            warn(pane_id)

    def _send_through_grown_window(
        self, record: dict[str, Any], since: int, **transport: Any
    ) -> tuple[dict[str, Any] | None, str | None] | None:
        """Re-run the compact transport once inside a temporarily grown window.

        ISSUE-REF: Claude Code clips its composer off a window shorter than its bottom
        stack, so ``no-composer`` there never clears by waiting: aih-ISSUE-REF sat
        about 10 h and ISSUE-REF 73 min. Growing is counted per attempt against
        ``no_composer_grow_max_attempts``, spaced by ``no_composer_grow_after_seconds``
        from the first hidden sample or the last grow, and every grow is restored
        before this returns. Returns None when no grow happened.
        """
        grow = getattr(self.tmux, "grow_window", None)
        restore = getattr(self.tmux, "restore_window", None)
        if self._composer_grow_active or not callable(grow) or not callable(restore):
            return None
        config = _config()
        limit = int(config["no_composer_grow_max_attempts"])
        count = int(record.get("composer_grows", 0))
        last = int(record.get("composer_grow_at", since))
        if count >= limit or _now() - last < int(
            config["no_composer_grow_after_seconds"]
        ):
            return None
        rows = int(config["no_composer_grow_rows"])
        key = str(record["key"])
        window = grow(str(record["pane_id"]), rows)
        if window is None:
            return None
        count += 1
        record = self.store.update(
            record, "BOUND", composer_grows=count, composer_grow_at=_now()
        )
        log_stage(
            "CC-NUDGE",
            key,
            f"reason=no-composer action=grow-window rows={rows} "
            f"from_rows={window.original_rows} count={count}/{limit}",
        )
        self._composer_grow_active = True
        outcome = "error"
        try:
            time.sleep(_COMPOSER_GROW_SETTLE_SECONDS)
            result = self._bound_compact_transport(record, **transport)
            outcome = result[1] or "sent"
            return result
        finally:
            self._composer_grow_active = False
            restored = restore(window)
            log_stage(
                "CC-NUDGE",
                key,
                f"action=restore-window rows={window.original_rows} "
                f"restored={str(bool(restored)).lower()} outcome={outcome}",
            )

    def _compact_sample(
        self, pane_id: str, fingerprint: str, attempt_id: str
    ) -> ComposerSample:
        sampler = getattr(self.tmux, "compact_sample", None)
        if callable(sampler):
            try:
                sample = sampler(pane_id, phase="precheck")
            except Exception as error:  # noqa: BLE001 - classifier is fail-closed
                return ComposerSample(
                    "not-ready",
                    "",
                    False,
                    error_reason=f"parser:{type(error).__name__}",
                )
            if isinstance(sample, ComposerSample):
                return sample
            return ComposerSample("not-ready", "", False, error_reason="parser")

        # Compatibility for existing injected adapters. Production TmuxAdapter
        # always uses compact_sample(), while continuation alone retains ready().
        try:
            ready = self.tmux.ready(pane_id, fingerprint, attempt_id)
        except Exception as error:  # noqa: BLE001 - classifier is fail-closed
            return ComposerSample(
                "not-ready", "", False, error_reason=f"parser:{type(error).__name__}"
            )
        if ready:
            return ComposerSample(
                "empty-composer", getattr(self.tmux, "last_capture", ""), True, ""
            )
        reason = getattr(self.tmux, "last_not_ready_reason", None)
        return ComposerSample(
            "composer-nonempty" if reason == "composer-nonempty" else "not-ready",
            getattr(self.tmux, "last_capture", ""),
            reason == "composer-nonempty",
        )

    def _write_compact_evaluation(
        self,
        record: dict[str, Any],
        samples: list[ComposerSample],
        phases: list[str],
        *,
        send_attempted: bool,
        send_result: str,
        text_may_have_been_staged: bool,
        idle_proof: str | None = None,
        landing: str = "none",
    ) -> None:
        _write_readiness_evaluation(
            root=self.store.root,
            key=str(record["key"]),
            attempt_id=str(record["attempt_id"]),
            pane_id=str(record["pane_id"]),
            samples=samples,
            phases=phases,
            send_attempted=send_attempted,
            send_result=send_result,
            text_may_have_been_staged=text_may_have_been_staged,
            goal_active=self._goal_state_tracker.current(
                record.get("transcript_path"), record.get("transcript_session_id")
            ),
            idle_proof=idle_proof,
            landing=landing,
            stagings=int(record.get("stagings", 0)),
            submissions=int(record.get("submissions", 0)),
            backoff_seconds=int(record.get("backoff_seconds", 0)),
            last_staged_text=(
                record.get("last_staged_text")
                if isinstance(record.get("last_staged_text"), str)
                else None
            ),
        )

    def _idle_proof(
        self,
        record: dict[str, Any],
        *,
        diagnostic: dict[str, Any] | None = None,
    ) -> str | None:
        key = str(record["key"])
        window = _idle_prompt_recency_window()
        if diagnostic is not None:
            diagnostic.update(
                idle_prompt_consulted=window is not None,
                idle_prompt_confirmed_recently="not-consulted",
                idle_prompt_window_s=window if window is not None else "invalid",
                composer_consulted=False,
            )
        if window is not None:
            idle_prompt_confirmed = self.store._idle_prompt_confirmed_recently_locked(
                key, window
            )
            if diagnostic is not None:
                diagnostic["idle_prompt_confirmed_recently"] = idle_prompt_confirmed
            if idle_prompt_confirmed:
                return "idle_prompt"
        quiet = _turn_quiet_seconds()
        path_value = record.get("transcript_path")
        if quiet is None:
            if diagnostic is not None:
                diagnostic.update(
                    transcript_consulted=False,
                    transcript_path=(
                        path_value if isinstance(path_value, str) else "missing"
                    ),
                    transcript_result="quiet-window-invalid",
                    wait_class="potentially-permanent",
                    quiet_seconds="invalid",
                )
            return None
        if not isinstance(path_value, str):
            if diagnostic is not None:
                diagnostic.update(
                    transcript_consulted=False,
                    transcript_path="missing",
                    transcript_result="path-missing",
                    wait_class="potentially-permanent",
                    quiet_seconds=quiet,
                )
            return None
        if diagnostic is not None:
            diagnostic.update(
                transcript_consulted=True,
                transcript_path=path_value,
                quiet_seconds=quiet,
            )
        return (
            "transcript-quiet"
            if _transcript_quiet_boundary(
                Path(path_value), quiet, diagnostic=diagnostic
            )
            else None
        )

    @staticmethod
    def _landing_start(record: dict[str, Any]) -> tuple[Path | None, int]:
        path_value = record.get("transcript_path")
        if not isinstance(path_value, str):
            return None, 0
        path = Path(path_value)
        try:
            return path, path.stat().st_size
        except OSError:
            return path, 0

    @staticmethod
    def _retry_fields(
        record: dict[str, Any], *, attempts: int | None = None
    ) -> dict[str, int]:
        base = max(1, int(record.get("lifecycle_interval_seconds", 30)))
        ceiling = _restage_backoff_ceiling_seconds() or base
        staging_count = max(
            1, int(record.get("stagings", 0)) if attempts is None else attempts
        )
        backoff = min(ceiling, base * (2 ** (staging_count - 1)))
        return {
            "backoff_seconds": backoff,
            "retry_not_before": _now() + backoff,
        }

    def _mark_corrupted(
        self, record: dict[str, Any], *, landing_offset: int
    ) -> dict[str, Any]:
        retry = self._retry_fields(record)
        current = self.store.update(
            record,
            "BOUND",
            landing="text",
            landing_offset=landing_offset,
            reason="compact-delivered-as-text",
            **retry,
        )
        write_transport_notice(
            str(record["key"]),
            "compact-delivered-as-text",
            attempt_id=str(record["attempt_id"]),
            root=self.store.root,
        )
        # ISSUE-REF: this is the one plain-text-landing branch that historically
        # skipped the debug pane capture every other failure disposition writes
        # (fence-mismatch, stage-verify-mismatch, stage-verify-no-composer,
        # REFUSED) -- leaving zero pane evidence for the recurring
        # compact-delivered-as-text failure across 4 of the last 5 real attempts.
        pane_id = record.get("pane_id")
        if isinstance(pane_id, str) and pane_id:
            _write_tmux_debug_capture(
                self.tmux,
                str(record["key"]),
                pane_id,
                str(record["attempt_id"]),
                "compact-delivered-as-text",
            )
        log_stage(
            "CC-CORRUPTED",
            str(record["key"]),
            "reason=compact-delivered-as-text "
            f"retry_not_before={retry['retry_not_before']}",
        )
        return current

    def _submit_verified_compact(
        self, record: dict[str, Any], pane_id: str
    ) -> tuple[dict[str, Any], str]:
        """Submit verified owned text, with one transcript-gated retry this evaluation."""
        timeout = _landing_timeout_seconds()
        path, offset = self._landing_start(record)
        enter = getattr(self.tmux, "send_enter", None)
        submissions = int(record.get("submissions", 0)) + 1
        try:
            entered = callable(enter) and enter(pane_id)
        except Exception:  # noqa: BLE001 - transport errors are terminal
            entered = False
        current = self.store.update(record, "READY", submissions=submissions)
        if not entered or timeout is None:
            return current, "enter-send-error"
        landing, offset, landed_at = _poll_landing(path, offset, timeout)
        if landing in _SUBMITTED_LANDINGS:
            clear_retry_notice(
                str(record["key"]),
                attempt_id=str(record["attempt_id"]),
                root=self.store.root,
            )
            return (
                self.store.update(
                    current,
                    "SENT",
                    sent_at=_now(),
                    landed_at=landed_at,
                    landing=landing,
                    landing_offset=offset,
                    backoff_seconds=0,
                    retry_not_before=0,
                    reason=None,
                ),
                landing,
            )
        if landing == "queued":
            clear_retry_notice(
                str(record["key"]),
                attempt_id=str(record["attempt_id"]),
                root=self.store.root,
            )
            return (
                self.store.update(
                    current,
                    "QUEUED",
                    sent_at=_now(),
                    landing=landing,
                    landing_offset=offset,
                    reason=None,
                ),
                landing,
            )
        if landing == "text":
            return self._mark_corrupted(current, landing_offset=offset), landing

        verified = self._compact_sample(
            pane_id, str(record["pane_fingerprint"]), str(record["attempt_id"])
        )
        if (
            verified.composer_recognized
            and verified.attribute_class in _SAFE_STAGED_ATTRIBUTE_CLASSES
            and _matches_staged_text(
                _composer_text(verified.content), record.get("last_staged_text")
            )
        ):
            path, offset = self._landing_start(current)
            submissions += 1
            try:
                entered = callable(enter) and enter(pane_id)
            except Exception:  # noqa: BLE001 - transport errors are terminal
                entered = False
            current = self.store.update(current, "READY", submissions=submissions)
            if not entered:
                return current, "enter-send-error"
            landing, offset, landed_at = _poll_landing(path, offset, timeout)
            if landing in _SUBMITTED_LANDINGS:
                clear_retry_notice(
                    str(record["key"]),
                    attempt_id=str(record["attempt_id"]),
                    root=self.store.root,
                )
                return (
                    self.store.update(
                        current,
                        "SENT",
                        sent_at=_now(),
                        landed_at=landed_at,
                        landing=landing,
                        landing_offset=offset,
                        backoff_seconds=0,
                        retry_not_before=0,
                        reason=None,
                    ),
                    landing,
                )
            if landing == "queued":
                clear_retry_notice(
                    str(record["key"]),
                    attempt_id=str(record["attempt_id"]),
                    root=self.store.root,
                )
                return (
                    self.store.update(
                        current,
                        "QUEUED",
                        sent_at=_now(),
                        landing=landing,
                        landing_offset=offset,
                    ),
                    landing,
                )
            if landing == "text":
                return self._mark_corrupted(current, landing_offset=offset), landing
        return (
            self.store.update(
                current,
                "BOUND",
                landing="none",
                landing_offset=offset,
                reason="landing-none",
                **self._retry_fields(current),
            ),
            "none",
        )

    def _resolve_queued(self, record: dict[str, Any]) -> dict[str, Any]:
        path_value = record.get("transcript_path")
        path = Path(path_value) if isinstance(path_value, str) else None
        landing, offset, landed_at = _poll_landing(
            path,
            int(record.get("landing_offset", 0)),
            0,
            queued=True,
        )
        if landing in _SUBMITTED_LANDINGS:
            return self.store.update(
                record,
                "SENT",
                landed_at=landed_at,
                landing=landing,
                landing_offset=offset,
                backoff_seconds=0,
                retry_not_before=0,
            )
        if landing == "text":
            return self._mark_corrupted(record, landing_offset=offset)
        if landing == "queue-dropped":
            log_stage(
                "CC-WAIT",
                str(record["key"]),
                f"reason=queue-dropped attempt={record.get('attempt_id')}",
            )
            return self.store.update(
                record,
                "BOUND",
                landing="none",
                landing_offset=offset,
                reason="queue-dropped",
                **self._retry_fields(record),
            )
        return self.store.update(record, "QUEUED", landing_offset=offset)

    def _has_superseded_owned_staging(
        self, record: dict[str, Any], composer_text: str
    ) -> bool:
        """Return whether this reservation directly superseded the staged command."""
        attempt_ids = record.get("superseded_staging_attempt_ids")
        if not isinstance(attempt_ids, list) or not all(
            isinstance(attempt_id, str) for attempt_id in attempt_ids
        ):
            return False
        superseded_ids = frozenset(attempt_ids)
        return any(
            prior.get("attempt_id") in superseded_ids
            and prior.get("attempt_type") == "compact"
            and prior.get("origin") == "save-state"
            and prior.get("state") == "REFUSED"
            and prior.get("reason") == "superseded-save-state"
            and prior.get("sent_at") is None
            and prior.get("pane_id") == record.get("pane_id")
            and prior.get("pane_fingerprint") == record.get("pane_fingerprint")
            and isinstance(prior.get("stagings"), int)
            and not isinstance(prior.get("stagings"), bool)
            and prior["stagings"] > 0
            and _matches_staged_text(composer_text, prior.get("last_staged_text"))
            for prior in self.store.read_attempts(str(record["key"]))
        )

    def _bound_compact_transport(
        self,
        record: dict[str, Any],
        *,
        idle_proof: str | None,
        repeat_composer_warning: bool = False,
        consume_before_stage: bool = False,
        library: bool = True,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Apply the shared idle, composer, staging, and landing boundary.

        ``library`` is False only for the legacy re-run after a library attempt
        that pressed no Enter (``library_cutover.compact``, ISSUE-REF D-8)."""
        key = str(record["key"])
        attempt_id = str(record["attempt_id"])
        pane_id = str(record["pane_id"])
        fingerprint = str(record["pane_fingerprint"])
        if record.get("state") == "QUEUED":
            current = self._resolve_queued(record)
            if current.get("state") == "SENT":
                log_stage(
                    "CC-SEND",
                    key,
                    f"attempt={attempt_id} landing={current.get('landing')}",
                )
                return current, None
            return current, str(current.get("landing", "queued"))
        precheck = self._compact_sample(pane_id, fingerprint, attempt_id)
        if precheck.attribute_class not in {
            "normal",
            "suggestion",
            "mixed",
            "unknown",
        }:
            precheck = ComposerSample(
                precheck.classification,
                precheck.region,
                precheck.composer_recognized,
                precheck.content,
                precheck.error_reason,
                precheck.claude_code_notice,
                "unknown",
            )
        if precheck.classification not in {
            "empty-composer",
            "composer-suggestion",
            "composer-nonempty",
            "not-obviously-occupied",
            "not-ready",
        }:
            precheck = ComposerSample(
                "not-ready",
                precheck.region,
                precheck.composer_recognized,
                precheck.content,
                precheck.error_reason,
                precheck.claude_code_notice,
                precheck.attribute_class,
            )
        if (
            precheck.classification == "composer-suggestion"
            and precheck.attribute_class != "suggestion"
        ):
            precheck = ComposerSample(
                "composer-nonempty",
                precheck.region,
                precheck.composer_recognized,
                precheck.content,
                precheck.error_reason,
                precheck.claude_code_notice,
                precheck.attribute_class,
            )
        if precheck.classification in {"empty-composer", "composer-suggestion"} and (
            not _is_proven_staging_target(precheck)
        ):
            composer_text = _composer_text(precheck.content)
            precheck = ComposerSample(
                (
                    "composer-nonempty"
                    if isinstance(composer_text, str) and composer_text.strip()
                    else "not-ready"
                ),
                precheck.region,
                precheck.composer_recognized,
                precheck.content,
                precheck.error_reason,
                precheck.claude_code_notice,
                precheck.attribute_class,
            )
        if (
            idle_proof is None
            and precheck.classification == "empty-composer"
            and precheck.composer_recognized
            and _composer_text(precheck.content) == ""
        ):
            idle_proof = "composer-empty"
            record = self.store.update(
                record, str(record["state"]), idle_proof=idle_proof
            )
        samples = [precheck]
        phases = ["precheck"]
        landing = "none"
        own_staged_text = (
            int(record.get("stagings", 0)) > 0
            and precheck.attribute_class in _SAFE_STAGED_ATTRIBUTE_CLASSES
            and _matches_staged_text(
                _composer_text(precheck.content), record.get("last_staged_text")
            )
        ) or (
            precheck.attribute_class in _SAFE_STAGED_ATTRIBUTE_CLASSES
            and self._has_superseded_owned_staging(
                record, _composer_text(precheck.content)
            )
        )
        retry_not_before = int(record.get("retry_not_before", 0))
        if retry_not_before > _now() and (
            own_staged_text
            or precheck.classification in {"empty-composer", "composer-suggestion"}
        ):
            self._write_compact_evaluation(
                record,
                samples,
                phases,
                send_attempted=False,
                send_result="not-attempted",
                text_may_have_been_staged=False,
                idle_proof=idle_proof,
            )
            log_stage(
                "CC-WAIT",
                key,
                f"reason=restage-backoff remaining_s={retry_not_before - _now()}",
            )
            return record, "restage-backoff"
        if own_staged_text:
            current, landing = self._submit_verified_compact(record, pane_id)
            self._write_compact_evaluation(
                current,
                samples,
                phases,
                send_attempted=True,
                send_result=(
                    "enter-send-error" if landing == "enter-send-error" else "succeeded"
                ),
                text_may_have_been_staged=True,
                idle_proof=idle_proof,
                landing=landing if landing != "enter-send-error" else "none",
            )
            if current.get("state") == "SENT":
                log_stage("CC-SEND", key, f"attempt={attempt_id} landing={landing}")
                return current, None
            if current.get("state") == "QUEUED":
                log_stage("CC-WAIT", key, "reason=queued")
                return current, "queued"
            if landing == "text":
                return None, "compact-delivered-as-text"
            if landing == "enter-send-error":
                current = self.store.update(
                    current,
                    "REFUSED",
                    reason=landing,
                    terminal_at=_now(),
                    send_result=landing,
                )
                return None, landing
            log_stage("CC-WAIT", key, "reason=landing-none")
            return None, "landing-none"
        if precheck.classification == "not-obviously-occupied":
            since = int(record.get("no_composer_since", _now()))
            record = self.store.update(
                record,
                "BOUND",
                idle_proof=idle_proof,
                no_composer_since=since,
            )
            self._write_compact_evaluation(
                record,
                samples,
                phases,
                send_attempted=False,
                send_result="not-attempted",
                text_may_have_been_staged=False,
                idle_proof=idle_proof,
            )
            log_stage("CC-WAIT", key, "reason=no-composer")
            # Bounded (ISSUE-REF): once stalled, the session has been told what to do,
            # so stop re-warning, but keep the attempt so a repaint still sends.
            if record.get("no_composer_stalled_at"):
                return record, "no-composer"
            # Before the stall bound, so the notice fires only when growing a short
            # window did not bring the composer back (or could not apply).
            grown = self._send_through_grown_window(
                record,
                since,
                idle_proof=idle_proof,
                repeat_composer_warning=repeat_composer_warning,
                consume_before_stage=consume_before_stage,
                library=library,
            )
            if grown is not None:
                return grown
            frame, frame_width = _no_composer_frame(precheck.region)
            stall = _no_composer_stall_seconds()
            if stall is not None and _now() - since >= stall:
                geometry_reader = getattr(self.tmux, "pane_geometry", None)
                geometry = (
                    geometry_reader(pane_id) if callable(geometry_reader) else "unknown"
                )
                record = self.store.update(
                    record,
                    "BOUND",
                    no_composer_stalled_at=_now(),
                    no_composer_frame=frame,
                    no_composer_frame_width=frame_width,
                    no_composer_pane_geometry=geometry,
                )
                self._warn_no_composer(pane_id, frame)
                action = (
                    "answer-permission-prompt"
                    if frame == "permission-dialog"
                    else "resize-window-or-type-compact"
                )
                log_stage(
                    "CC-NO-COMPOSER-STALLED",
                    key,
                    f"attempt={attempt_id} waited_s={_now() - since} frame={frame} "
                    f"frame_width={frame_width} pane={_log_token(geometry)} "
                    f"decision=attempt-kept action={action}",
                )
                write_no_composer_stall_notice(
                    key,
                    attempt_id=attempt_id,
                    waited_seconds=_now() - since,
                    frame=frame,
                    frame_width=frame_width,
                    pane_geometry=geometry,
                    root=self.store.root,
                )
                return record, "no-composer"
            escalate = _no_composer_escalate_seconds()
            if (
                escalate is not None
                and _now() - since >= escalate
                and (
                    not record.get("no_composer_warning_sent")
                    or repeat_composer_warning
                )
            ):
                record = self.store.update(
                    record, "BOUND", no_composer_warning_sent=True
                )
                self._composer_warning_emitted = True
                self._warn_no_composer(pane_id, frame)
                detail = (
                    " frame=permission-dialog" if frame == "permission-dialog" else ""
                )
                log_stage("CC-ESCALATE", key, f"reason=no-composer{detail}")
            return record, "no-composer"
        if precheck.classification not in {"empty-composer", "composer-suggestion"}:
            self._write_compact_evaluation(
                record,
                samples,
                phases,
                send_attempted=False,
                send_result="not-attempted",
                text_may_have_been_staged=False,
                idle_proof=idle_proof,
            )
            if precheck.classification == "composer-nonempty":
                _write_tmux_debug_capture(
                    self.tmux, key, pane_id, attempt_id, "composer-nonempty"
                )
            if precheck.classification == "composer-nonempty" and (
                not record.get("composer_warning_sent") or repeat_composer_warning
            ):
                record = self.store.update(record, "BOUND", composer_warning_sent=True)
                self._composer_warning_emitted = True
                warn = getattr(self.tmux, "warn_composer_blocked", None)
                if callable(warn):
                    warn(pane_id)
                log_stage(
                    "CC-ESCALATE",
                    key,
                    "reason=composer-nonempty action=submit-or-clear-draft "
                    f"attribute_class={precheck.attribute_class} "
                    "claude_code_notice="
                    f"{precheck.claude_code_notice or 'none'}",
                )
            else:
                detail = "reason=not-ready"
                if precheck.classification == "composer-nonempty":
                    detail += (
                        f" attribute_class={precheck.attribute_class} "
                        "claude_code_notice="
                        f"{precheck.claude_code_notice or 'none'}"
                    )
                log_stage("CC-WAIT", key, detail)
            return record, "not-ready"

        already_staged = int(record.get("stagings", 0))
        # The ONLY positive proof that a previous staging left the composer: the transcript
        # recorded it as a plain user message, so the payload is accounted for and typing again
        # cannot append onto it. An Enter that landed nothing, or a verify that could not read
        # the pane, proves nothing of the kind.
        payload_accounted_for = record.get("landing") == "text"
        if already_staged > 0 and not payload_accounted_for:
            # ISSUE-REF: one UNACCOUNTED-FOR staging per attempt, then stop typing. Typing is
            # the only irreversible thing this transport does, and `fence_and_stage` appends --
            # it does not replace. An attempt that has already typed `/compact` and cannot now
            # see it has keystrokes it cannot account for, and every reading that would
            # authorise typing again ("the composer looks empty") is exactly what an unreadable
            # pane produces. Retrying the SUBMIT is safe and still happens above via
            # `own_staged_text`; retrying the TYPING is what left four `/compact`s in
            # ISSUE-REF's composer, and two in ISSUE-REF's before it.
            #
            # This is not a throttle. `retry_not_before` already throttled it and the composer
            # still compounded, four times, because a delayed unsafe action is still unsafe.
            # A genuinely lost staging is recovered by the NEXT armed attempt, after the user
            # has been told to clear the draft.
            self._write_compact_evaluation(
                record,
                samples,
                phases,
                send_attempted=False,
                send_result="not-attempted",
                text_may_have_been_staged=False,
                idle_proof=idle_proof,
            )
            refusals = int(record.get("restage_refusals", 0)) + 1
            record = self.store.update(
                record,
                "BOUND",
                reason="restage-refused",
                restage_refusals=refusals,
                # Back the refusal off on how many times it has refused, not on `stagings`:
                # `stagings` is now pinned at 1 by this very guard, so deriving the backoff
                # from it would repeat the escalation notice every lifecycle tick forever.
                **self._retry_fields(record, attempts=refusals),
            )
            self._composer_warning_emitted = True
            warn = getattr(self.tmux, "warn_restage", None)
            if callable(warn):
                warn(pane_id, already_staged)
            log_stage(
                "CC-ESCALATE",
                key,
                f"reason=restage-refused staging_count={already_staged} "
                f"attribute_class={precheck.attribute_class}",
            )
            return record, "restage-refused"
        if library and _library_cutover("compact"):
            handled, record = self._library_compact_transport(
                record, consume_before_stage=consume_before_stage
            )
            if handled is not None:
                return handled
            return self._bound_compact_transport(
                record,
                idle_proof=idle_proof,
                repeat_composer_warning=repeat_composer_warning,
                consume_before_stage=consume_before_stage,
                library=False,
            )
        if consume_before_stage:
            self.store.consume(record)
        current = self.store.update(record, "READY")
        text_may_have_been_staged = True
        stage = getattr(self.tmux, "fence_and_stage", None)
        if not callable(stage):
            # An adapter that cannot separate staging from Enter cannot satisfy
            # the compact safety contract. Refuse it without invoking its old
            # combined transport.
            send_result = "text-send-error"
        else:
            try:
                send_result = stage(pane_id, fingerprint, "/compact")
            except Exception:  # noqa: BLE001 - transport errors are terminal
                send_result = "text-send-error"
            if send_result == "succeeded":
                stagings = int(current.get("stagings", 0)) + 1
                current = self.store.update(
                    current,
                    "READY",
                    stagings=stagings,
                    last_staged_at=_now(),
                    last_staged_text="/compact",
                )
                sampler = getattr(self.tmux, "compact_sample", None)
                if not callable(sampler):
                    send_result = "stage-verify-no-composer"
                else:
                    deadline = (
                        time.monotonic() + _DEFAULT_COMPACT_STAGE_VERIFY_TIMEOUT_SECONDS
                    )
                    while True:
                        try:
                            verified = sampler(pane_id, phase="stage-verify")
                        except Exception as error:  # noqa: BLE001 - fail closed
                            verified = ComposerSample(
                                "stage-verify-mismatch",
                                "",
                                False,
                                error_reason=f"parser:{type(error).__name__}",
                            )
                        if not isinstance(verified, ComposerSample):
                            verified = ComposerSample(
                                "stage-verify-mismatch",
                                "",
                                False,
                                error_reason="parser",
                            )
                        samples.append(verified)
                        phases.append("stage-verify")
                        content = verified.content
                        stale_empty_frame = (
                            verified.composer_recognized
                            and isinstance(content, str)
                            and (
                                not content.strip() or _matches_composer_cursor(content)
                            )
                        )
                        if (
                            verified.classification == "stage-verified"
                            or not stale_empty_frame
                            or time.monotonic() >= deadline
                        ):
                            break
                        time.sleep(_DEFAULT_COMPACT_STAGE_VERIFY_POLL_SECONDS)
                    if not verified.composer_recognized:
                        send_result = "stage-verify-no-composer"
                    elif (
                        verified.classification != "stage-verified"
                        or verified.attribute_class
                        not in _SAFE_STAGED_ATTRIBUTE_CLASSES
                    ):
                        send_result = "stage-verify-mismatch"
                    else:
                        current, landing = self._submit_verified_compact(
                            current, pane_id
                        )
                        send_result = (
                            "enter-send-error"
                            if landing == "enter-send-error"
                            else "succeeded"
                        )

        if text_may_have_been_staged:
            text_may_have_been_staged = send_result != "fence-mismatch"
        self._write_compact_evaluation(
            current,
            samples,
            phases,
            send_attempted=True,
            send_result=send_result,
            text_may_have_been_staged=text_may_have_been_staged,
            idle_proof=idle_proof,
            landing=landing,
        )
        diagnostic = {
            "send_result": send_result,
            "text_may_have_been_staged": text_may_have_been_staged,
        }
        if send_result == "succeeded":
            current = self.store.update(current, str(current["state"]), **diagnostic)
            if current.get("state") == "SENT":
                log_stage("CC-SEND", key, f"attempt={attempt_id} landing={landing}")
                return current, None
            if current.get("state") == "QUEUED":
                log_stage("CC-WAIT", key, "reason=queued")
                return current, "queued"
            if landing == "text":
                return None, "compact-delivered-as-text"
            log_stage("CC-WAIT", key, "reason=landing-none")
            return None, "landing-none"
        if send_result in {"fence-mismatch", "stage-verify-mismatch"}:
            retry = (
                self._retry_fields(current)
                if send_result == "stage-verify-mismatch"
                else {}
            )
            current = self.store.update(
                current, "BOUND", reason=send_result, **diagnostic, **retry
            )
            _write_tmux_debug_capture(self.tmux, key, pane_id, attempt_id, send_result)
            detail = f"reason={send_result}"
            if send_result == "stage-verify-mismatch":
                detail += f" attribute_class={samples[-1].attribute_class}"
            log_stage("CC-WAIT", key, detail)
            return None, send_result
        if send_result == "stage-verify-no-composer":
            current = self.store.update(
                current,
                "STAGED_UNVERIFIED",
                reason=send_result,
                **diagnostic,
                **self._retry_fields(current),
            )
            _write_tmux_debug_capture(self.tmux, key, pane_id, attempt_id, send_result)
            log_stage("CC-WAIT", key, f"reason={send_result}")
            return None, send_result
        current = self.store.update(
            current,
            "REFUSED",
            reason=send_result,
            terminal_at=_now(),
            **diagnostic,
        )
        _write_tmux_debug_capture(self.tmux, key, pane_id, attempt_id, send_result)
        log_stage("CC-REFUSED", key, f"reason={send_result}")
        return None, send_result

    def _attempt_by_id(self, key: str, attempt_id: str) -> dict[str, Any] | None:
        for item in self.store.read_attempts(key):
            if item.get("attempt_id") == attempt_id:
                return {name: value for name, value in item.items() if name != "_path"}
        return None

    def _library_compact_transport(
        self, record: dict[str, Any], *, consume_before_stage: bool
    ) -> tuple[tuple[dict[str, Any] | None, str | None] | None, dict[str, Any]]:
        """``library_cutover.compact``: stage and submit through the library's
        ``CompactInjector`` on THIS attempt record (ISSUE-REF D-8).

        Returns the transport result when the library pressed Enter, whatever
        the landing: an Enter cannot be unsent, and a legacy retry after an
        unobserved success is a double ``/compact``. Returns ``None`` with the
        record the legacy re-run starts from ONLY when the library pressed no
        Enter AND its refusal was a transport one; a staging it made is carried
        on that record, so the legacy path submits it rather than typing a
        second ``/compact`` (ISSUE-REF). A pane-judgement refusal returns
        ``(record, reason)`` and no legacy staging runs (D-8 Amendment).
        """
        from ai_chassis.prompt_injection.config import load_config, resume_root
        from ai_chassis.prompt_injection.cutover import run_with_cutover
        from ai_chassis.prompt_injection.injector import CompactInjector
        from ai_chassis.prompt_injection.outcomes import (
            TRANSPORT_REASONS,
            InjectionOutcome,
        )
        from ai_chassis.prompt_injection.targets import SessionTarget, claude_version

        key = str(record["key"])
        attempt_id = str(record["attempt_id"])
        legacy = {name: value for name, value in record.items() if name != "_path"}
        command = getattr(self.tmux, "command", None)
        unusable = None
        if not isinstance(command, tuple) or not all(
            isinstance(part, str) for part in command
        ):
            unusable = "adapter-without-tmux-command"
        elif self.store.root.resolve() != resume_root().resolve():
            # The library writes this record under AIH_RESUME_ROOT; a store rooted
            # elsewhere would leave the legacy ledger reading a record never advanced.
            unusable = "store-root-mismatch"

        def not_run(reason: str) -> InjectionOutcome:
            # Whether an Enter went is read from the record's own counts, never
            # assumed either way.
            seen = self._attempt_by_id(key, attempt_id) or legacy
            return InjectionOutcome(
                attempt_id=attempt_id,
                use_case="compact",
                state="REFUSED",
                landing=None,
                reason=reason,
                staged=int(seen.get("stagings", 0)) > int(legacy.get("stagings", 0)),
                submitted=int(seen.get("submissions", 0))
                > int(legacy.get("submissions", 0)),
                evidence={},
            )

        def run_library() -> InjectionOutcome:
            if unusable is not None:
                return not_run(unusable)
            config = load_config()
            # The PreCompact hook advances this record from its own process, and
            # this caller holds the store lock, so the oracle must not wait; the
            # supervisor correlates STARTED exactly as it does for a legacy send.
            config["completion_timeout_seconds"] = 0
            ceiling = _restage_backoff_ceiling_seconds()
            if ceiling is not None:
                config["restage_backoff_ceiling_seconds"] = ceiling
            path = record.get("transcript_path")
            target = SessionTarget(
                key=key,
                pane_id=str(record["pane_id"]),
                fingerprint=str(record["pane_fingerprint"]),
                transcript_path=Path(path) if isinstance(path, str) else None,
                cwd=None,
                transcript_source="attempt-record",
                cc_version=claude_version(),
            )
            if consume_before_stage:
                self.store.consume(record)
            injector = CompactInjector(
                tmux_command=command, config=config, adopt_record=legacy
            )
            try:
                return injector.run(
                    target,
                    landing_timeout=_landing_timeout_seconds(),
                    ready_wait=float(_config()["library_fallback_after_seconds"]),
                )
            except Exception as error:  # noqa: BLE001 - the record decides
                return not_run(f"library-exception:{type(error).__name__}")

        # The legacy re-run is the caller's; run_with_cutover decides whether it
        # may happen, so the log line says what the caller then does.
        outcome, fall_back = run_with_cutover(
            run_library, lambda: True, stage_prefix="COMPACT", key=key
        )
        current = self._attempt_by_id(key, attempt_id) or legacy
        if not outcome.submitted:
            restored = dict(legacy)
            if outcome.staged:
                restored.update(
                    stagings=int(legacy.get("stagings", 0)) + 1,
                    last_staged_text="/compact",
                    last_staged_at=current.get("last_staged_at", _now()),
                )
            restored = self.store.update(restored, str(legacy["state"]))
            if outcome.reason in TRANSPORT_REASONS:
                # The library's notice body cannot compose the host paragraphs
                # (import boundary), so this call site owns the compact notice
                # and replaces the library's shorter one (ISSUE-REF).
                write_transport_notice(
                    key, outcome.reason, attempt_id=attempt_id, root=self.store.root
                )
            if fall_back:
                return None, restored
            # A pane-judgement refusal: a legacy staging now would type over what
            # the refusal protected (D-8 Amendment).
            return (restored, outcome.reason), restored
        state = str(current.get("state"))
        if state in {"SENT", "STARTED", "COMPLETED"}:
            current = self.store.update(
                current, state, send_result="succeeded", text_may_have_been_staged=True
            )
            return (current, None), current
        if state == "QUEUED":
            current = self.store.update(
                current, state, send_result="succeeded", text_may_have_been_staged=True
            )
            return (current, "queued"), current
        return (None, outcome.reason or "landing-none"), current

    def _library_line(
        self,
        use_case: str,
        record: dict[str, Any],
        text: str,
        transcript: Path | None,
        adopt: bool = False,
    ) -> tuple[Any, bool]:
        """Send one legacy line leg's text through the library (ISSUE-REF Phase 4).

        ``record`` carries the leg's ``key``, ``pane_id`` and ``pane_fingerprint``;
        with ``adopt`` the library advances that record itself. Returns the
        library's ``InjectionOutcome`` and whether the legacy send may run: True only
        after a ``CC-<USE_CASE>-LIB-FALLBACK`` line (no Enter went and the refusal was
        a transport one, D-8 Amendment); a pane-judgement refusal is a
        ``CC-<USE_CASE>-LIB-REFUSED`` line and False. The leg GAINS the
        library's readiness wait, tmux floor and transcript landing (design § Phase 4,
        recorded as behaviour added).
        """
        from ai_chassis.prompt_injection.config import load_config, resume_root
        from ai_chassis.prompt_injection.cutover import run_with_cutover
        from ai_chassis.prompt_injection.injector import INJECTORS
        from ai_chassis.prompt_injection.outcomes import InjectionOutcome
        from ai_chassis.prompt_injection.payload import PromptPayload
        from ai_chassis.prompt_injection.targets import (
            SessionTarget,
            TargetUnresolved,
            claude_version,
            resolve_target,
        )

        key = str(record["key"])
        attempt_id = str(record.get("attempt_id") or "")
        command = getattr(self.tmux, "command", None)
        unusable = None
        if not isinstance(command, tuple) or not all(
            isinstance(part, str) for part in command
        ):
            unusable = "adapter-without-tmux-command"
        elif adopt and self.store.root.resolve() != resume_root().resolve():
            unusable = "store-root-mismatch"

        def not_run(reason: str, *, submitted: bool) -> InjectionOutcome:
            return InjectionOutcome(
                attempt_id=attempt_id,
                use_case=use_case,
                state="REFUSED",
                landing=None,
                reason=reason,
                staged=False,
                submitted=submitted,
                evidence={},
            )

        def run_library() -> InjectionOutcome:
            if unusable is not None:
                return not_run(unusable, submitted=False)
            config = load_config()
            config["completion_timeout_seconds"] = 0
            path = transcript
            if path is None:
                try:
                    path = resolve_target(
                        f"pane:{record['pane_id']}", tmux_command=command
                    ).transcript_path
                except TargetUnresolved:
                    path = None
            target = SessionTarget(
                key=key,
                pane_id=str(record["pane_id"]),
                fingerprint=str(record["pane_fingerprint"]),
                transcript_path=path,
                cwd=None,
                transcript_source="attempt-record" if transcript else "cwd-newest",
                cc_version=claude_version(),
            )
            injector = INJECTORS[use_case](
                tmux_command=command,
                config=config,
                adopt_record=dict(record) if adopt else None,
            )
            try:
                return injector.run(
                    target,
                    PromptPayload(text),
                    landing_timeout=float(_config()["landing_timeout_seconds"]),
                    ready_wait=float(_config()["library_fallback_after_seconds"]),
                )
            except Exception as error:  # noqa: BLE001 - never a second send blind
                # An adopted record's own count says whether Enter went; without
                # one it is unknowable, so it is treated as sent.
                seen = self._attempt_by_id(key, attempt_id) if adopt else None
                submitted = (
                    int(seen.get("submissions", 0)) > 0 if seen is not None else True
                )
                return not_run(
                    f"library-exception:{type(error).__name__}", submitted=submitted
                )

        outcome, fall_back = run_with_cutover(
            run_library,
            lambda: True,
            stage_prefix=use_case.upper(),
            key=key,
        )
        return outcome, fall_back is True

    def arm_compact(self, key: str, pane_id: str) -> dict[str, Any] | None:
        if not _enabled():
            log_stage("CC-ARM-NOOP", key, "reason=not-enabled")
            return None
        if not _enrolled(key):
            log_stage("CC-ARM-NOOP", key, "reason=not-enrolled")
            return None
        version = self.tmux.version()
        if not _tmux_version_allowed(version):
            log_stage(
                "CC-REFUSED",
                key,
                f"reason=tmux-version version={version or 'unavailable'}",
            )
            return None
        with self.store.locked():
            reconcile_timeouts(self.store, key, "compact")
            if self.store.pending(key, "compact") or not self.store.permitted(
                key, "compact"
            ):
                return None
            fingerprint = self.tmux.fingerprint(pane_id)
            if not fingerprint:
                log_stage("CC-REFUSED", key, "reason=no-pane")
                return None
            attempt_id = secrets.token_hex(8)
            record = {
                "attempt_id": attempt_id,
                "attempt_type": "compact",
                "key": key,
                "pane_id": pane_id,
                "pane_fingerprint": fingerprint,
                "armed_at": _now(),
                "sent_at": None,
                "landed_at": None,
                "stagings": 0,
                "submissions": 0,
                "backoff_seconds": 0,
                "last_staged_text": None,
                "state": "ARMED",
                "evidence_path": None,
            }
            self.store.write_attempt(record)
            log_stage("CC-ARM", key, f"attempt={attempt_id}")
            self.store.update(record, "BOUND")
            log_stage("CC-BIND", key, f"pane={pane_id}")
        return self.send_armed_compact(key)

    def bind_reserved_save_state(self, key: str, pane_id: str) -> dict[str, Any] | None:
        """Give the history-reserved save-state arm the ordinary idle fallback."""
        if not _enabled() or not _enrolled(key):
            return None
        version = self.tmux.version()
        if not _tmux_version_allowed(version):
            log_stage(
                "CC-REFUSED",
                key,
                f"reason=tmux-version version={version or 'unavailable'}",
            )
            return None
        with self.store.locked():
            candidates = [
                record
                for record in self.store.pending(key, "compact")
                if record.get("origin") == "save-state"
                and record.get("state") == "ARMED"
            ]
            if len(candidates) != 1:
                return None
            fingerprint = self.tmux.fingerprint(pane_id)
            if not fingerprint:
                log_stage("CC-REFUSED", key, "reason=no-pane")
                return None
            self.store.update(
                candidates[0], "BOUND", pane_id=pane_id, pane_fingerprint=fingerprint
            )
            log_stage("CC-BIND", key, f"pane={pane_id} origin=save-state")
        return self.send_armed_compact(key)

    def _report_attempt_conflict(
        self, key: str, candidates: list[dict[str, Any]]
    ) -> None:
        """Name a send that cannot choose between live attempts, every tick.

        This return was silent: ISSUE-REF's two attempts sat BOUND and QUEUED for 92
        seconds with no line in resume.log until the operator typed `/compact`
        himself (ISSUE-REF).  The log line repeats like every other CC-WAIT; the
        pane warning is shown once per newest attempt.
        """
        states = ",".join(sorted(str(record.get("state")) for record in candidates))
        attempts = ",".join(str(record.get("attempt_id")) for record in candidates)
        log_stage(
            "CC-WAIT",
            key,
            f"reason=attempt-count candidates={len(candidates)} states={states} "
            f"attempts={attempts}",
        )
        newest = max(candidates, key=lambda record: int(record.get("armed_at") or 0))
        pane_id = newest.get("pane_id")
        if newest.get("attempt_conflict_warning_sent") or not isinstance(pane_id, str):
            return
        self.store.update(
            newest, str(newest["state"]), attempt_conflict_warning_sent=True
        )
        warn = getattr(self.tmux, "warn_compact_stalled", None)
        if callable(warn):
            warn(pane_id, f"attempt-count={len(candidates)}")
        log_stage(
            "CC-ESCALATE",
            key,
            f"reason=attempt-count candidates={len(candidates)} "
            "action=run-compact-yourself",
        )

    def send_armed_compact(
        self,
        key: str,
        *,
        idle_prompt_just_confirmed: bool = False,
        repeat_composer_warning: bool = False,
        transcript_path: Path | None = None,
        lifecycle_interval_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        """Release one bound compact arm through the existing safe transport."""
        from ai_chassis.wake import sessions

        if sessions.enabled():
            candidates = self.store.pending(key, "compact")
            for candidate in candidates:
                if not (
                    candidate.get("session_id")
                    or candidate.get("transcript_session_id")
                ):
                    try:
                        binding = json.loads(
                            (
                                sessions.directory(sessions.mail_root(), key)
                                / "binding.json"
                            ).read_text()
                        )
                        if binding.get("pane") != candidate.get("pane_id"):
                            raise ValueError("session-pane-mismatch")
                        sid = sessions.safe_identity(binding["session_id"])
                        path = Path(binding["transcript"])
                        if path.stem != sid or "subagents" in path.parts:
                            raise ValueError("session-transcript-mismatch")
                        candidate = self.store.update(
                            candidate,
                            str(candidate["state"]),
                            session_id=sid,
                            transcript_session_id=sid,
                            transcript_path=str(path),
                        )
                    except (ValueError, OSError, KeyError, TypeError):
                        log_stage(
                            "CC-EVENT-HELD", key, "reason=session-binding-unavailable"
                        )
                        continue
                if transcript_path is not None:
                    candidate = self.store.update(
                        candidate,
                        str(candidate["state"]),
                        transcript_path=str(transcript_path),
                    )
                _event_handoff(
                    key,
                    "compact-lifecycle",
                    store=self.store,
                    attempt_id=candidate["attempt_id"],
                )
            return candidates[0] if len(candidates) == 1 else None
        self._composer_warning_emitted = False
        with self.store.locked():
            reconcile_timeouts(self.store, key, "compact")
            candidates = [
                record
                for record in self.store.pending(key, "compact")
                if record.get("state") in {"BOUND", "STAGED_UNVERIFIED", "QUEUED"}
            ]
            if len(candidates) > 1:
                self._report_attempt_conflict(key, candidates)
                return None
            if not candidates:
                return None
            record = candidates[0]
            if transcript_path is not None:
                record = self.store.update(
                    record, str(record["state"]), transcript_path=str(transcript_path)
                )
            if lifecycle_interval_seconds is not None:
                record = self.store.update(
                    record,
                    str(record["state"]),
                    lifecycle_interval_seconds=lifecycle_interval_seconds,
                )
            if record.get("state") == "QUEUED":
                return self._resolve_queued(record)
            idle_diagnostic: dict[str, Any] = {}
            idle_proof = self._idle_proof(record, diagnostic=idle_diagnostic)
            if idle_proof is None:
                _log_not_idle(key, idle_diagnostic)
                return record
            record = self.store.update(
                record, str(record["state"]), idle_proof=idle_proof
            )
            if idle_prompt_just_confirmed:
                armed_at = record.get("armed_at")
                if isinstance(armed_at, int) and not isinstance(armed_at, bool):
                    elapsed_ms = max(0, int(time.time() * 1000) - armed_at * 1000)
                    log_stage("CC-IDLE-CONFIRMED", key, f"elapsed_ms={elapsed_ms}")
            if record.get("transport") == "fifo":
                return self._send_fifo_compact(record)
            if record.get("transport") == "mod":
                # Only the mod's claim moves a mod attempt to SENT (ISSUE-REF D-3).
                return record
            pane_id = record.get("pane_id")
            fingerprint = record.get("pane_fingerprint")
            if not isinstance(pane_id, str) or not isinstance(fingerprint, str):
                self.store.update(record, "REFUSED", reason="missing-pane-binding")
                _write_tmux_debug_capture(
                    self.tmux,
                    key,
                    pane_id if isinstance(pane_id, str) else "unknown",
                    str(record["attempt_id"]),
                    "missing-pane-binding",
                )
                log_stage("CC-REFUSED", key, "reason=missing-pane-binding")
                return None
            record, _ = self._bound_compact_transport(
                record,
                idle_proof=idle_proof,
                repeat_composer_warning=repeat_composer_warning,
            )
            return record

    def _send_fifo_compact(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """Write ``/compact`` once into a bare session's submit FIFO (ISSUE-REF).

        Reached only with the transcript idle proof already taken. The FIFO has no
        screen, so the empty-composer check the pane path makes is replaced by
        :func:`_fifo_input_idle`; operator input recorded after the last
        submitted prompt waits, anything that cannot be proved refuses. A write is
        never repeated: the attempt goes SENT, and only the session transcript's
        compact boundary confirms it (:func:`_confirm_fifo_compact`).
        """
        from ai_chassis import submit_fifo  # noqa: PLC0415 - hook import latency

        key = str(record["key"])
        attempt_id = str(record["attempt_id"])
        path_value = record.get("submit_fifo")
        if not isinstance(path_value, str) or not path_value:
            return self._refuse_fifo(record, "fifo-missing")
        # The FIFO first: a gone launcher takes its input-at with it, and the gone
        # FIFO is the reason, not the unprovable idle state that follows from it.
        reason = submit_fifo.refusal(Path(path_value))
        if reason is not None:
            return self._refuse_fifo(record, reason)
        not_idle = _fifo_input_idle(record)
        if not_idle == "input-after-last-prompt":
            log_stage(
                "CC-WAIT",
                key,
                f"reason=not-idle transport=fifo attempt={attempt_id} input={not_idle}",
            )
            return record
        if not_idle is not None:
            return self._refuse_fifo(record, "fifo-idle-unprovable", detail=not_idle)
        reason = submit_fifo.submit(
            Path(path_value),
            "/compact",
            enter_delay_seconds=float(_config()["fifo_enter_delay_seconds"]),
        )
        if reason is not None:
            return self._refuse_fifo(record, reason)
        sent = self.store.update(
            record,
            "SENT",
            sent_at=_now(),
            submissions=int(record.get("submissions", 0)) + 1,
            landing="fifo-written",
        )
        log_stage(
            "CC-FIFO-SENT",
            key,
            f"attempt={attempt_id} idle_proof={record.get('idle_proof')} "
            f"fifo={_log_token(Path(path_value).name)}",
        )
        return sent

    def _refuse_fifo(
        self, record: dict[str, Any], reason: str, *, detail: str | None = None
    ) -> None:
        key = str(record["key"])
        attempt_id = str(record["attempt_id"])
        self.store.update(
            record, "REFUSED", reason=reason, refusal_detail=detail, terminal_at=_now()
        )
        log_stage(
            "CC-REFUSED",
            key,
            f"reason={reason} transport=fifo attempt={attempt_id}"
            + (f" detail={detail}" if detail else ""),
        )
        write_transport_notice(key, reason, attempt_id=attempt_id, root=self.store.root)

    def arm_continuation(self, key: str, pane_id: str) -> dict[str, Any] | None:
        """Send the current compact's validated persisted resume command."""
        from ai_chassis.wake import sessions

        if sessions.enabled():
            from ai_chassis.wake.handlers.continuation import request

            return request(key, pane_id, self.store, self.tmux)
        if not _enabled():
            log_stage("CC-CONT-NOOP", key, "reason=not-enabled")
            return None
        if not _enrolled(key):
            log_stage("CC-CONT-NOOP", key, "reason=not-enrolled")
            return None
        # A FAST, DIAGNOSABLE REFUSAL, kept ahead of the wake-budget check so the
        # gate remains the stated reason when the gate is the reason. The
        # authoritative decision is still the in-lock one below, which reads the
        # override off the attempt it is actually about; this only short-circuits
        # the case where NO attempt for this key carries an override, so the answer
        # cannot change. Without it, a refusal caused by the gate was logged as
        # nothing at all -- the silent-status class ISSUE-REF was filed about.
        if not _post_compact_typed_wake_enabled() and not self._any_typed_override(key):
            log_stage("CC-CONT-NOOP", key, "reason=post-compact-typed-wake-not-enabled")
            return None
        if not _tmux_version_allowed(self.tmux.version()):
            log_stage("CC-CONT-AMBIGUOUS", key, "reason=tmux-version")
            return None
        with self.store.locked():
            if self.store.pending(key, "continuation") or not self.store.permitted(
                key, "continuation"
            ):
                return None
            started_records = [
                record
                for record in self.store.read_attempts(key)
                if record.get("attempt_type") == "compact"
                and record.get("state") == "STARTED"
            ]
            if len(started_records) == 1:
                completed = started_records[0]
            elif started_records:
                log_stage("CC-CONT-AMBIGUOUS", key, "reason=multiple-started-compacts")
                return None
            else:
                compact_records = [
                    record
                    for record in self.store.read_attempts(key)
                    if record.get("attempt_type") == "compact"
                    and record.get("state") == "COMPLETED"
                ]
                if not compact_records:
                    log_stage(
                        "CC-CONT-AMBIGUOUS", key, "reason=missing-validated-history"
                    )
                    return None
                completed = compact_records[0]
            if not isinstance(completed.get("resume_command"), str) or not isinstance(
                completed.get("resume_command_digest"), str
            ):
                log_stage("CC-CONT-AMBIGUOUS", key, "reason=missing-validated-history")
                return None
            # THE TYPED GATE IS EVALUATED HERE, not at the top of the method, because
            # the per-attempt override lives on the compact record this block just
            # resolved -- so the gate and the override are one decision read from one
            # place, and no leg can disagree with another about whether this attempt
            # may type. The override NEVER widens anything beyond this attempt: it is
            # a field on `completed`, so the next reservation has no override at all.
            overridden = completed.get("typed_wake_override") is True
            if not _post_compact_typed_wake_enabled() and not overridden:
                log_stage(
                    "CC-CONT-NOOP", key, "reason=post-compact-typed-wake-not-enabled"
                )
                return None
            if overridden:
                log_stage(
                    "CC-CONT-OVERRIDE",
                    key,
                    f"attempt={completed.get('attempt_id')} "
                    "source=AIH_SKILL_POST_COMPACT_TYPED_WAKE scope=this-attempt",
                )
            # SERIALISE AGAINST THE SESSIONSTART BANNER TURN (ISSUE-REF). The banner
            # delivers the resume regardless of this leg, and the post-compact turn
            # acts on it unprompted -- so typing here while that turn is pending is
            # the duplicate submission that disabled this leg in the first place.
            if self._banner_turn_consumed_or_pending(key, completed):
                return None
            command = str(completed["resume_command"])
            command_digest = str(completed["resume_command_digest"])
            fingerprint = self.tmux.fingerprint(pane_id)
            attempt_id = secrets.token_hex(8)
            transcript, transcript_source = _own_transcript(
                completed.get("transcript_path"),
                completed.get("transcript_session_id"),
            )
            record = {
                "attempt_id": attempt_id,
                "attempt_type": "continuation",
                "key": key,
                "pane_id": pane_id,
                "pane_fingerprint": fingerprint,
                "command_digest": command_digest,
                "sent_command": command,
                "armed_at": _now(),
                "sent_at": None,
                "state": "CONTINUATION_SENT",
                "evidence_path": None,
                "transcript_source": transcript_source,
            }
            if fingerprint and _library_cutover("continuation"):
                self.store.write_attempt(record)
                outcome, fall_back = self._library_line(
                    "continuation",
                    record,
                    command,
                    transcript,
                    adopt=True,
                )
                if outcome.submitted:
                    # The legacy vocabulary: a delivered plain line is `text`
                    # (_landing_record), and the sweeper keys on `queued`.
                    landing = {"prompt-record": "text", None: "none"}.get(
                        outcome.landing, outcome.landing
                    )
                    current = self._attempt_by_id(key, attempt_id) or record
                    record = self.store.update(
                        current, "CONTINUATION_SENT", sent_at=_now(), landing=landing
                    )
                    log_stage(
                        "CC-CONT-SEND",
                        key,
                        f"attempt={attempt_id} landing={landing} via=library",
                    )
                    return record
                if not fall_back:
                    # A pane-judgement refusal (or a hold): the library has already
                    # recorded it on the adopted record, and the legacy send is
                    # unreachable.
                    return None
            if not fingerprint or not self._wait_for_continuation_ready(
                pane_id, fingerprint, attempt_id
            ):
                self.store.update(
                    record, "REFUSED", reason="not-ready", terminal_at=_now()
                )
                log_stage("CC-CONT-REFUSED", key, "reason=not-ready")
                return None
            self.store.write_attempt(record)
            # Captured BEFORE Enter so the landing poll reads only records this send
            # appended, exactly as the compact leg does.
            offset = (
                transcript.stat().st_size
                if transcript is not None and transcript.is_file()
                else 0
            )
            if not self.tmux.fence_and_send(pane_id, fingerprint, command):
                _transition_failure(
                    self.store, record, continuation=True, reason="fence-mismatch"
                )
                return None
            # Classify where the keystroke actually landed. A `queued` disposition is
            # the case that used to be detected and then abandoned: the prompt is
            # real and will be processed, it has simply not reached the composer yet,
            # so the sweeper needs to know not to terminalise it on the first pass.
            landing, _, _ = _poll_landing(
                transcript,
                offset,
                float(_config()["landing_timeout_seconds"]),
                command=command,
            )
            record = self.store.update(
                record, "CONTINUATION_SENT", sent_at=_now(), landing=landing
            )
            log_stage(
                "CC-CONT-SEND",
                key,
                f"attempt={attempt_id} landing={landing} "
                f"transcript_source={transcript_source}",
            )
            return record

    def _any_typed_override(self, key: str) -> bool:
        """Whether ANY attempt for this key carries a one-attempt typed override.

        Deliberately broad and best-effort: it answers only "could the typed leg
        possibly be permitted here", which is all a pre-lock short-circuit may
        safely decide. Which attempt actually governs is settled under the lock.
        """
        try:
            return any(
                record.get("typed_wake_override") is True
                for record in self.store.read_attempts(key)
            )
        except OSError:
            return False

    def _banner_packet(self, key: str, expected_digest: str, state: str) -> Path | None:
        """This cycle's resume packet in `injected/` or `confirmed/`, by digest.

        ANCHORED ON THE DIGEST, not on a filename timestamp or an mtime. The
        packet is published BEFORE the compact, so it is always older than the
        attempt that consumes it and no time comparison can decide which cycle
        it belongs to. The attempt record already carries
        `resume_artifact_digest` and `_matching_injected_artifact` already
        matches on it, so this reuses that linkage and only widens it to the
        second directory.
        """
        for artifact in sorted(
            (self.store.root / state).glob(f"{glob.escape(key)}-*.md")
        ):
            try:
                if digest(artifact.read_bytes()) == expected_digest:
                    return artifact
            except OSError:
                continue
        return None

    def _banner_turn_consumed_or_pending(
        self, key: str, completed: dict[str, Any]
    ) -> bool:
        """Whether the SessionStart banner turn makes a typed prompt redundant.

        THE PREDICATE, and why it is this one.  `save-resume --ack <key>` MOVES
        `injected/<name>.md` to `confirmed/<name>.md`, and the post-compact turn
        runs that ack as its first act on the banner.  So the packet's own
        location is positive proof of whether a turn consumed it -- not an
        inference from timing, which is all the old composer-emptiness check
        amounted to.

        THE TRAP THIS IS WRITTEN AGAINST.  Because the ack MOVES the file, an
        empty `injected/` means either "no banner was ever published" or "a
        banner was published and already consumed" -- opposite answers that
        demand opposite actions.  Keying off `injected/` alone therefore reports
        absence over a real consumption and sends a duplicate, which is the
        original defect wearing a new predicate.  Both directories are checked,
        and the packet is identified by DIGEST so a previous cycle's confirmed
        packet cannot suppress this one forever.

        Four outcomes:

        * already in `confirmed/` -> the turn ran, send NOTHING;
        * in `injected/` and confirmed inside the bound -> same, send NOTHING;
        * in `injected/` and the bound expires -> no turn is coming, so let
          exactly one keystroke through.  This is the only case the leg is for;
        * in neither -> no banner to serialise against, proceed without waiting.
          Waiting here would block every caller with nothing to wait for.

        The bound is its own configurable value and NOT
        `_continuation_ready_wait`'s, whose 3.0s ceiling is what made the old
        guard unable to outlast a banner turn at all.
        """
        expected = completed.get("resume_artifact_digest")
        if not isinstance(expected, str):
            return False
        consumed = self._banner_packet(key, expected, "confirmed")
        if consumed is not None:
            detail = f"reason=banner-turn-consumed artifact={consumed.name}"
            log_stage("CC-CONT-NOOP", key, detail)
            return True
        pending = self._banner_packet(key, expected, "injected")
        if pending is None:
            return False
        wait = _banner_turn_wait()
        if wait is None:
            return False
        timeout, poll = wait
        confirmed_directory = self.store.root / "confirmed"
        deadline = time.monotonic() + timeout
        while True:
            if (confirmed_directory / pending.name).exists():
                log_stage(
                    "CC-CONT-NOOP",
                    key,
                    f"reason=banner-turn-consumed artifact={pending.name}",
                )
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log_stage(
                    "CC-CONT-BANNER-UNCONSUMED",
                    key,
                    f"waited={timeout} artifact={pending.name}",
                )
                return False
            time.sleep(min(poll, remaining))

    def _wait_for_continuation_ready(
        self, pane_id: str, fingerprint: str, attempt_id: str
    ) -> bool:
        """Poll the existing read-only composer detector within the SessionStart budget."""
        wait = _continuation_ready_wait()
        if wait is None:
            return False
        timeout, poll = wait
        deadline = time.monotonic() + timeout
        while True:
            if self.tmux.ready(pane_id, fingerprint, attempt_id):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(poll, remaining))

    def _hold_in_boundary(
        self, key: str, use_case: str, text: str, pane_id: str, fingerprint: str
    ) -> bool:
        """Hold a legacy typed leg's line instead of typing it, while ``key`` is
        between a ``/save-state`` and its confirmed compaction (ISSUE-REF D-8).

        The reauth wake is the ``fence_and_send`` leg that does not run through the
        library's ``Injector`` template while its cutover flag is off (the reboot
        wake that was its sibling went with ISSUE-REF), so it consults the same predicate the
        template uses and record the same ``HELD`` line, which the post-compact
        drain re-delivers through the registered injector for ``use_case``.
        Returns True when the line was held and the caller must not type.

        FAILS TOWARD THE SEND: a fault in the hold machinery is logged as
        ``CC-HOLD-ERROR`` and the leg behaves as it did before this change, because
        a wake that types is recoverable and a wake that silently vanishes is not.
        """
        try:
            from ai_chassis.prompt_injection import boundary

            if boundary.in_boundary(key) is None:
                return False
            boundary.hold_legacy(
                key, use_case, text, pane_id=pane_id, fingerprint=fingerprint
            )
            return True
        except Exception as error:  # noqa: BLE001 - see the docstring
            log_stage(
                "CC-HOLD-ERROR",
                key,
                f"use_case={use_case} error={type(error).__name__}",
            )
            return False

    def _reauth_wake_target(self, key: str) -> tuple[str | None, Path | None]:
        """The newest pane binding and transcript path this key has on record."""
        pane: str | None = None
        transcript: Path | None = None
        for record in self.store.read_attempts(key):  # newest first
            if pane is None and isinstance(record.get("pane_id"), str):
                pane = str(record["pane_id"])
            if transcript is None and isinstance(record.get("transcript_path"), str):
                transcript = Path(str(record["transcript_path"]))
            if pane is not None and transcript is not None:
                break
        return pane, transcript

    def send_reauth_wake(
        self,
        key: str,
        *,
        dead_from: int,
        dead_to: int,
        pane_id: str | None = None,
        transcript_path: Path | None = None,
    ) -> dict[str, Any] | None:
        """Tell one session to resume the turn a dead credential aborted (ISSUE-REF).

        Every guard below is a precondition of the send, not a note beside it: the
        `fence_and_send` call is reachable only when enrolment, the tmux floor, a
        live pane fingerprint, transcript evidence of an abort inside the window, an
        unclaimed window marker and (switch off) an empty composer all hold. A session that merely EXISTS is never
        prompted -- that negative case is the whole point of the transcript check.

        The claim is taken BEFORE the send, so a transport failure cannot loop: one
        wake per key per window is the budget whether or not it landed. That is the
        deliberate trade (ISSUE-REF was a notification storm), and the failure is
        logged as `CC-REAUTH-FAILED` rather than retried; a refused readiness check
        (`CC-REAUTH-REFUSED`) and a missing landing (`CC-REAUTH-UNVERIFIED`) spend
        the window the same way.

        Nothing is written to `attempts/`. The wake's durable state is its claim
        marker plus the stage log, which keeps it entirely out of `permitted()`'s
        shared wake budget.
        """
        if not _enabled():
            log_stage("CC-REAUTH-NOOP", key, "reason=not-enabled")
            return None
        if not _enrolled(key):
            log_stage("CC-REAUTH-NOOP", key, "reason=not-enrolled")
            return None
        version = self.tmux.version()
        if not _tmux_version_allowed(version):
            log_stage(
                "CC-REAUTH-NOOP",
                key,
                f"reason=tmux-version version={version or 'unavailable'}",
            )
            return None
        with self.store.locked():
            recorded_pane, recorded_transcript = self._reauth_wake_target(key)
            pane_id = pane_id or recorded_pane
            transcript_path = transcript_path or recorded_transcript
            if pane_id is None:
                log_stage("CC-REAUTH-REFUSED", key, "reason=missing-pane-binding")
                return None
            if transcript_path is None:
                log_stage("CC-REAUTH-REFUSED", key, "reason=no-transcript-path")
                return None
            if not credential_abort_in_window(transcript_path, dead_from, dead_to):
                log_stage("CC-REAUTH-SKIP", key, "reason=no-aborted-turn")
                return None
            fingerprint = self.tmux.fingerprint(pane_id)
            if not fingerprint:
                log_stage("CC-REAUTH-REFUSED", key, "reason=missing-pane-binding")
                return None
            window = digest(f"{dead_from}-{dead_to}")
            if not self.store.claim_reauth_wake(key, window):
                log_stage(
                    "CC-REAUTH-NOOP", key, f"reason=already-woken window={window}"
                )
                return None
            record = {
                "attempt_type": "reauth-wake",
                "key": key,
                "pane_id": pane_id,
                "pane_fingerprint": fingerprint,
                "dead_window": window,
                "dead_from": dead_from,
                "dead_to": dead_to,
                "sent_at": None,
                "state": "REAUTH_WAKE_REFUSED",
            }
            if _library_cutover("reauth_wake"):
                outcome, fall_back = self._library_line(
                    "reauth-wake",
                    record,
                    reauth_wake_prompt(dead_from, dead_to),
                    transcript_path,
                )
                if outcome.state == "HELD":
                    # Held across the boundary; the drain delivers it through
                    # ReauthWakeInjector. The window's claim stays taken, so the
                    # one-wake-per-window budget is unchanged (ISSUE-REF D-8).
                    record["state"] = "REAUTH_WAKE_HELD"
                    record["held_attempt_id"] = outcome.attempt_id
                    log_stage(
                        "CC-REAUTH-HELD",
                        key,
                        f"window={window} pane={pane_id} via=library",
                    )
                    return record
                if outcome.submitted:
                    record.update(
                        state="REAUTH_WAKE_SENT",
                        sent_at=_now(),
                        landing=outcome.landing,
                    )
                    log_stage(
                        "CC-REAUTH-SEND",
                        key,
                        f"window={window} pane={pane_id} via=library "
                        f"landing={outcome.landing}",
                    )
                    return record
                if not fall_back:
                    # A pane-judgement refusal: the legacy send is unreachable, and
                    # the window's claim stays taken (one wake per window).
                    return None
            if self._hold_in_boundary(
                key,
                "reauth-wake",
                reauth_wake_prompt(dead_from, dead_to),
                pane_id,
                fingerprint,
            ):
                record["state"] = "REAUTH_WAKE_HELD"
                log_stage("CC-REAUTH-HELD", key, f"window={window} pane={pane_id}")
                return record
            prompt = reauth_wake_prompt(dead_from, dead_to)
            # The switch-off path and the library's transport fallback both get the
            # readiness check and landing read the library leg has (ISSUE-REF,
            # D-8 Amendment): the send below is unreachable unless the composer read
            # empty twice.
            ready, reason = legacy_composer_ready(
                lambda: self._compact_sample(pane_id, fingerprint, window)
            )
            if not ready:
                log_stage(
                    "CC-REAUTH-REFUSED",
                    key,
                    f"reason={reason} window={window} pane={pane_id}",
                )
                return None
            offset = transcript_size(transcript_path)
            if not self.tmux.fence_and_send(pane_id, fingerprint, prompt):
                log_stage(
                    "CC-REAUTH-FAILED", key, f"reason=fence-mismatch window={window}"
                )
                return None
            record["state"] = "REAUTH_WAKE_SENT"
            record["sent_at"] = _now()
            log_stage("CC-REAUTH-SEND", key, f"window={window} pane={pane_id}")
            landing, landed_at = legacy_landing(transcript_path, offset, prompt)
            record.update(landing=landing, landed_at=landed_at)
            log_stage(
                "CC-REAUTH-LANDED" if landing != "none" else "CC-REAUTH-UNVERIFIED",
                key,
                f"window={window} pane={pane_id} landing={landing}",
            )
            return record


def run_reauth_wake(
    dead_from: int,
    dead_to: int,
    *,
    supervisor: ContextSupervisor | None = None,
) -> list[dict[str, Any]]:
    """Wake every session the credential outage aborted, and report only those.

    The population is the keys the supervisor store knows, which is exactly the set
    of sessions that have ever had a pane bound. A session with no store presence has
    no transport and is unreachable by construction, not skipped by policy.
    """
    supervisor = supervisor or ContextSupervisor()
    woken: list[dict[str, Any]] = []
    for key in supervisor.store.keys():
        record = supervisor.send_reauth_wake(key, dead_from=dead_from, dead_to=dead_to)
        if record is not None:
            woken.append(record)
    return woken


def handle_idle_prompt(
    payload: dict[str, Any],
    key: str,
    *,
    supervisor: ContextSupervisor | None = None,
) -> None:
    """Record the app-owned idle signal, then release an existing Stop arm."""
    if (
        payload.get("notification_type") != "idle_prompt"
        or not _enabled()
        or not _enrolled(key)
    ):
        return
    store = supervisor.store if supervisor is not None else ResumeStore()
    store.mark_idle_prompt_confirmed(key)
    if _event_handoff(key, "idle-prompt", payload=payload):
        return
    (supervisor or ContextSupervisor(store)).send_armed_compact(
        key, idle_prompt_just_confirmed=True
    )


# The canonical derivation lives in ONE place, `resume_derive_key()` in
# .claude/bin/resume-lib.sh, and this is how Python reaches it. The pattern is not
# novel here: scripts/provenance_identity.py::_canonical_registry_name already
# treats that library as the canonical bridge-aware implementation and shells out
# to it for exactly this reason.
#
# ISSUE-REF. ISSUE-REF fixed one READER (context-supervisor-user-prompt.sh) by
# deriving the key in bash and handing it down. The alternative considered and
# rejected there -- add a registry-aware derivation to Python as well -- would have
# made a THIRD implementation of a rule whose entire failure mode is two copies
# disagreeing. So Python does not re-implement the rule; it asks the owner.
#
# Deriving it here rather than trusting the wrapper is what makes the whole class
# fixed instead of the four instances found: `precompact-safety.sh` invokes this
# module BEFORE it derives its own key, and `context-supervisor-idle-prompt.sh`
# never derives at all. Both would otherwise have kept reading a path the writer
# never writes -- and with the bash guards now open in front of them, that reads as
# a working leg doing nothing, which is harder to diagnose than the closed guard.
_KEY_DERIVATION_TIMEOUT_SECONDS = 10

# Distinct exit codes so "the owner refused" and "the owner could not be consulted"
# are not the same answer. They must not be: the raw environment is a legitimate
# last resort for the second and is the very value being rejected in the first.
_DERIVE_KEY_UNAVAILABLE = 3
_DERIVE_KEY_REFUSED = 4
_DERIVE_KEY_SNIPPET = (
    f'source "$1" || exit {_DERIVE_KEY_UNAVAILABLE}; '
    f"resume_derive_key || exit {_DERIVE_KEY_REFUSED}; "
    'printf "%s" "$RESUME_KEY"'
)

#: ``_key_from_resume_library()`` saying "this session has no usable identity".
#: Not ``None``, which means "I could not ask".
REFUSED = "\x00refused"


def _resume_library() -> Path:
    return Path(__file__).resolve().parents[2] / ".claude" / "bin" / "resume-lib.sh"


def _key_from_resume_library() -> str | None:
    """Ask ``resume_derive_key()`` for this session's key.

    Returns the key, ``REFUSED`` when the library rejected the identity outright
    (a path-traversal token: it reports that as non-zero with
    ``RESUME_KEY=unknown``), or ``None`` when it could not be consulted at all --
    absent library, no bash, a failed or timed-out call.

    The environment is inherited deliberately. In production this process IS the
    hook, so its environment is the session's; the derivation reads
    ``CLAUDE_CODE_SESSION_ID``, ``CLAUDE_CODE_TASK_LIST_ID`` and ``HOME``, and
    supplying a curated subset would mean deciding here which inputs the rule is
    allowed to use -- a second copy of the rule, expressed as an allowlist.
    """
    library = _resume_library()
    if not library.is_file():
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell string from input
            ["bash", "-c", _DERIVE_KEY_SNIPPET, "context-supervisor", str(library)],
            capture_output=True,
            check=False,
            text=True,
            timeout=_KEY_DERIVATION_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode == _DERIVE_KEY_REFUSED:
        return REFUSED
    if result.returncode != 0:
        return None
    key = result.stdout.strip()
    # The library also spells an unusable identity as the literal `unknown` on the
    # success path. Treat it as a refusal rather than as a session named "unknown",
    # matching context-supervisor-user-prompt.sh.
    if not key or key == "unknown":
        return REFUSED
    return key


_MARK_FIRST_TURN_SNIPPET = 'source "$1" || exit 3; resume_mark_first_turn "$2"'


def _mark_first_turn_via_resume_library(key: str) -> None:
    """Record this session's first post-compact turn for an in-flight delivery.

    ISSUE-REF AC-2. A CLAIMED wake is the moment a wake transport commits to
    driving this session's next turn: the asyncRewake leg exits 2 with its
    ``rewakeMessage`` immediately after this returns, and the poller's own
    completion notification does the same. It is the best start available for
    the ack-lateness interval on the legs that never pass through
    ``UserPromptSubmit`` -- which is the other call site, and the one that
    covers a human submitting the turn.

    IT IS A PROXY, AND IT IS EARLY RATHER THAN LATE. ISSUE-REF measured the gap
    between a claim and the turn it starts: 7 s when ``ISSUE-REF`` self-started, and
    98 s for ``ISSUE-REF``, whose claim was folded into a human prompt typed during
    the compact (``CC-WAKE-PREEMPTED`` records that case). Nothing in this tree
    records the turn itself starting, so that gap cannot currently be removed.
    Starting the clock early over-states lateness by at most that gap against a
    600 s budget, which is the conservative direction to be wrong in and an
    order of magnitude smaller than the 1046 s error this AC exists to fix. Do
    NOT "improve" this by letting a later ``UserPromptSubmit`` overwrite the
    stamp: re-stamping on a later turn is what makes the deadline unexpirable.

    The rule lives in ONE place, ``resume_mark_first_turn()`` in
    .claude/bin/resume-lib.sh, and this reaches it exactly as
    :func:`_key_from_resume_library` reaches ``resume_derive_key``, for the same
    reason: the stamp is read back by that library's own ``resume_sweep``, so a
    second copy of the filename, the format and the no-delivery no-op would be a
    second thing to drift. Both sides resolve the store through that library's
    ``resume_root()``, which honours ``AIH_RESUME_ROOT`` as this module does.

    This is the NOTIFICATION leg, not the typed one. It reads no wake
    configuration of its own: ``post_compact_typed_wake_enabled`` is false by
    default and this path is unaffected either way.

    Every failure is swallowed. A delivery whose first turn could not be
    recorded falls to the FIRST-TURN BACKSTOP interval, which the sweep logs as
    such rather than reporting it as a late ack.
    """
    library = _resume_library()
    if not library.is_file():
        return
    try:
        subprocess.run(  # noqa: S603 - fixed argv, no shell string from input
            [
                "bash",
                "-c",
                _MARK_FIRST_TURN_SNIPPET,
                "context-supervisor",
                str(library),
                key,
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=_KEY_DERIVATION_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return


def _derive_session_key() -> str | None:
    """This session's resume key, or ``None`` when no identity can be established."""
    key = _key_from_resume_library()
    if key == REFUSED:
        return None
    if key is not None:
        return key
    # LIBRARY-ABSENT FALLBACK ONLY. This is the raw-environment expression that used
    # to be the primary derivation, retained (not added) for an installation where
    # the shell library is unavailable. It disagrees with the writer whenever the
    # task-list id is unset, so it must not be reachable when the library answers.
    raw = (
        os.environ.get("CLAUDE_CODE_TASK_LIST_ID")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or None
    )
    if raw is None or _session_key_refusal(raw) is not None:
        return None
    return raw


# Events that move a compact attempt. A sub-agent's own compaction fires every one of
# them with the PARENT's session id (so the parent's key), and its PreCompact took
# BOUND -> STARTED as a "manual" compact: aih-ISSUE-REF attempt b6c4a538 was confirmed
# for a window that never compacted (ISSUE-REF).
_COMPACT_LIFECYCLE_EVENTS = frozenset({"precompact", "session-start", "postcompact"})


def _subagent_compact_ignored(event: str, payload: dict[str, Any], key: str) -> bool:
    """Drop a compact event that a sub-agent's own context raised.

    Keyed on ``agent_id`` alone, unlike the write guards that also take
    ``agent_type``: Claude Code documents ``agent_id`` as present only inside a
    sub-agent or teammate, while ``agent_type`` is also set on the session's own
    payload when it runs under ``--agent``. Treating that as a sub-agent would drop
    the session's real compacts, the one outcome worse than this bug.
    https://code.claude.com/docs/en/hooks#common-input-fields
    """
    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        return False
    agent_type = payload.get("agent_type") or "none"
    trigger = payload.get("trigger") or payload.get("source") or "none"
    log_stage(
        "CC-SUBAGENT-COMPACT-IGNORED",
        key,
        f"event={event} agent_id={_log_token(agent_id)} "
        f"agent_type={_log_token(agent_type)} trigger={_log_token(trigger)}",
    )
    return True


def _event_handoff(key, kind, *, payload=None, store=None, attempt_id=None):
    """Selected hosts hand ownership to durable intake, including on refusal."""
    from ai_chassis.wake import sessions

    if not sessions.enabled():
        return False
    sid = (payload or {}).get("session_id")
    if not sid and attempt_id:
        records = (store or ResumeStore()).read_attempts(key)
        record = next(
            (row for row in records if row.get("attempt_id") == attempt_id), {}
        )
        sid = record.get("session_id") or record.get("transcript_session_id")
    try:
        sessions.emit(sessions.mail_root(), key, sid, kind)
        outcome = "intake-written"
    except (OSError, ValueError, TypeError):
        outcome = "session-binding-unavailable"
        log_stage("CC-EVENT-HELD", key, "reason=session-binding-unavailable")
    if kind == "compact-lifecycle" and attempt_id:
        attempt_store = store or ResumeStore()
        with attempt_store.locked():
            for record in attempt_store.read_attempts(key):
                if record.get("attempt_id") == attempt_id:
                    attempt_store.update(record, record["state"], event_handoff=outcome)
    return True


def hook_main(event: str) -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        if event == "wake-watch":
            from ai_chassis.wake import sessions

            key = _derive_session_key()
            try:
                directories = sessions.context_watch_paths(
                    _root_from_environment(), key, payload.get("session_id")
                )
                for directory in directories:
                    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                if key is not None and (
                    sessions.event_layout(sessions.mail_root(), key)
                    or sessions.migration_id(
                        sessions.mail_root(), key, payload.get("session_id")
                    )
                ):
                    sessions.acknowledge(
                        sessions.mail_root(), key, payload.get("session_id"), "context"
                    )
            except (ValueError, TypeError, OSError):
                print(
                    json.dumps(
                        {
                            "hookSpecificOutput": {
                                "hookEventName": "SessionStart",
                                "watchPaths": [],
                                "additionalContext": "Wake registration degraded: invalid-session",
                            }
                        }
                    )
                )
                return 0
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "SessionStart",
                            "watchPaths": [str(directory) for directory in directories],
                        }
                    },
                    separators=(",", ":"),
                )
            )
            return 0
        key = _derive_session_key() or "unknown"
        if event in _COMPACT_LIFECYCLE_EVENTS and _subagent_compact_ignored(
            event, payload, key
        ):
            return 0
        if event == "precompact":
            handle_precompact(payload, key)
        elif event == "session-start":
            handle_session_start(payload, key)
        elif event == "session-end":
            handle_session_end(payload, key)
        elif event == "postcompact":
            handle_postcompact(payload, key)
        elif event == "user-prompt":
            handle_user_prompt(payload, key)
        elif event == "idle-prompt":
            handle_idle_prompt(payload, key)
        elif event == "transcript-signal":
            handle_transcript_signal(payload, key)
        elif event == "transport-verdict":
            # Plain text on stdout, deliberately NOT a hookSpecificOutput envelope:
            # this is a separate manifest entry from `wake-watch` precisely because
            # that one prints JSON on the same event, and mixing the two would
            # corrupt the envelope rather than add a line to it.
            verdict = session_start_transport_verdict(payload, key)
            if verdict:
                sys.stdout.write(verdict)
    except Exception as error:  # noqa: BLE001 - fail-open at the hook boundary
        _write_hook_exception_diagnostic(event, error)
        return 0
    return 0


def _wake_preemption_count(key: str, attempt_id: str) -> int | None:
    """Undelivered human prompts enqueued since this attempt started, or None.

    None means unknown -- no attempt record, no transcript on it, or a transcript
    that supplied no reading.  A caller must report unknown as unknown: a zero here
    would claim "nothing is competing for the turn" on the strength of a probe that
    never ran, which is the shape this whole bug was filed under.
    """
    for record in ResumeStore().read_attempts(key):
        if record.get("attempt_id") != attempt_id:
            continue
        started = _transcript_timestamp(
            record.get("started_at")
        ) or _transcript_timestamp(record.get("armed_at"))
        transcript_path = record.get("transcript_path")
        if started is None or not isinstance(transcript_path, str):
            return None
        return _pending_queued_human_prompts(Path(transcript_path), started)
    return None


def _record_wake_delivery(
    key: str, attempt_id: str, artifact_digest: str, marker_path: str
) -> None:
    """Record that this leg handed the wake to Claude Code, and what will consume it.

    ``CC-WAKE-CLAIM`` used to be the last line any leg wrote, which made two
    different outcomes read identically from the log: a wake that started a turn,
    and a wake that was delivered into a session whose turn was already spoken for
    (ISSUE-REF).  Both were measured the same night -- `ISSUE-REF` self-started 7s after
    its claim; `ISSUE-REF` had a prompt the operator typed 98s into its own compact
    waiting in the queue, so the notification was folded into that human turn.
    """
    pending = _wake_preemption_count(key, attempt_id)
    if pending:
        log_stage(
            "CC-WAKE-PREEMPTED",
            key,
            f"attempt={attempt_id} claimant=async-rewake "
            "reason=human-prompt-queued-during-compact "
            f"pending_prompts={pending} detail=the wake was delivered but a prompt "
            "typed during the compact will consume the turn it would have started",
        )
    log_stage(
        "CC-REWAKE-SENT",
        key,
        f"attempt={attempt_id} claimant=async-rewake digest={artifact_digest} "
        f"transport=async-rewake-exit2 marker={Path(marker_path).name} "
        f"preempting_prompts={'unknown' if pending is None else pending}",
    )


def claim_wake_from_stdin() -> int:
    """Claim the marker named by FileChanged and print only a factual envelope.

    Every refusal is recorded.  Four of them used to be a bare ``return 0``, so a
    marker that arrived mis-routed, truncated or already consumed left the same
    trace as a watch that never fired at all.
    """
    key = _derive_session_key()
    log_key = key or "unknown"
    try:
        payload = json.load(sys.stdin)
        marker_path = payload.get("file_path") if isinstance(payload, dict) else None
        if not isinstance(marker_path, str):
            log_stage("CC-REWAKE-NOOP", log_key, "reason=stdin-named-no-marker")
            return 0
        # Resolved on both sides: a symlinked resume root must not make a live wake look foreign.
        from ai_chassis.wake import sessions

        if Path(marker_path).parent.resolve() not in {
            path.resolve()
            for path in sessions.context_watch_paths(
                _root_from_environment(), key, payload.get("session_id")
            )
        }:
            log_stage("CC-REWAKE-NOOP", log_key, "reason=marker-outside-wake")
            return 0
        try:
            marker_text = Path(marker_path).read_text(encoding="utf-8")
        except OSError:
            log_stage("CC-REWAKE-NOOP", log_key, "reason=marker-unreadable")
            return 0
        values = dict(
            line.split("=", 1) for line in marker_text.splitlines() if "=" in line
        )
        marker_key = values.get("key") or ""
        attempt_id = values.get("attempt_id") or ""
        artifact_digest = values.get("digest") or ""
        if not (marker_key and attempt_id and artifact_digest):
            log_stage("CC-REWAKE-NOOP", log_key, "reason=marker-fields-missing")
            return 0
        if marker_key != key:
            log_stage(
                "CC-REWAKE-NOOP",
                log_key,
                f"reason=marker-key-mismatch marker_key={marker_key}",
            )
            return 0
        if not _post_compact_notification_wake_enabled():
            log_stage(
                "CC-REWAKE-NOOP",
                marker_key,
                "reason=post-compact-notification-wake-not-enabled",
            )
            return 0
        if not ResumeStore().claim_wake(
            marker_key, attempt_id, "async-rewake", marker_path=Path(marker_path)
        ):
            log_stage(
                "CC-REWAKE-NOOP",
                marker_key,
                f"attempt={attempt_id} reason=claim-lost",
            )
            return 0
        _record_wake_delivery(marker_key, attempt_id, artifact_digest, marker_path)
        print(
            json.dumps(
                {
                    "attempt_id": attempt_id,
                    "digest": artifact_digest,
                    "key": marker_key,
                },
                sort_keys=True,
            )
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return 0
    return 0


def _usage_refusal(event: str, problems: list[str]) -> int:
    """Refuse a bad invocation of ``event`` on stderr, exit 2 (ISSUE-REF).

    Every arm below used to return 2 with nothing on either stream, so the command
    ``/save-state`` launches to arm the auto-compact answered a missing flag with a
    bare code. ``poller-health`` was fixed the same way under ISSUE-REF.
    """
    print(f"context_supervisor {event}: {'; '.join(problems)}", file=sys.stderr)
    return 2


def _flag_problems(
    args: argparse.Namespace, required: dict[str, tuple[int, int] | None]
) -> list[str]:
    """Name each required flag that is missing, or outside its documented range."""
    problems = []
    for flag, bounds in required.items():
        value = getattr(args, flag.lstrip("-").replace("-", "_"))
        if value is None or value == "":
            problems.append(f"{flag} is required")
        elif bounds is not None and not bounds[0] <= value <= bounds[1]:
            problems.append(f"{flag} {value} is outside {bounds[0]}..{bounds[1]}")
    return problems


def main() -> int:
    if sys.argv[1:2] == ["mod"]:
        return mod_bridge_main(sys.argv[2:])
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "event",
        choices=(
            "precompact",
            "session-start",
            "session-end",
            "postcompact",
            "user-prompt",
            "idle-prompt",
            "transcript-signal",
            "publish-resume",
            "resume-command",
            "compact",
            "continue",
            "force-gate",
            "post-compact-poll",
            "compact-lifecycle",
            "poller-health",
            "claim-wake-from-stdin",
            "wake-watch",
            "transport-verdict",
            "compact-rearm-check",
            "reauth-wake",
        ),
    )
    parser.add_argument("--dead-from")
    parser.add_argument("--dead-to")
    parser.add_argument("--key")
    parser.add_argument("--pane")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--attempt-id")
    parser.add_argument("--interval-seconds", type=int)
    parser.add_argument("--warning-interval-seconds", type=int)
    parser.add_argument("--long-wait-escalate-seconds", type=int)
    parser.add_argument("--post-compact-escalate-seconds", type=int)
    parser.add_argument("--transcript-path")
    parser.add_argument("--observe-post-compact-only", action="store_true")
    # Enables the typed wake for THIS ATTEMPT ONLY, leaving the fleet default off.
    # The flag is the explicit route; `AIH_SKILL_POST_COMPACT_TYPED_WAKE=1` reaches
    # the same resolver through the shared skill-CLI precedence chain.
    parser.add_argument("--enable-typed-wake", action="store_true")
    args = parser.parse_args()
    if args.key:
        refusal = _session_key_refusal(args.key)
        if refusal is not None:
            print(
                f"context_supervisor: refusing --key {args.key!r}: {refusal}",
                file=sys.stderr,
            )
            return 2
    if args.event == "compact-rearm-check":
        from ai_chassis import compact_rearm  # noqa: PLC0415 - recovery's fail-closed guard

        if compact_rearm.claim_check():
            print("compact-rearm-ready")
        return 0
    if args.event == "publish-resume":
        if not args.key:
            return _usage_refusal(args.event, ["--key is required"])
        return publish_resume_from_stdin(args.key, args.session_id)
    if args.event == "resume-command":
        if not args.key:
            return _usage_refusal(args.event, ["--key is required"])
        return print_resume_command(args.key)
    if args.event == "post-compact-poll":
        problems = _flag_problems(
            args,
            {
                "--key": None,
                "--attempt-id": None,
                "--post-compact-escalate-seconds": (60, 3600),
            },
        )
        if problems:
            return _usage_refusal(args.event, problems)
        with poller_supervision(args.key, args.attempt_id) as reporter:
            reporter.phase("observing")
            observed = run_post_compact_poller(
                args.key,
                args.attempt_id,
                args.post_compact_escalate_seconds,
                transcript_path=(
                    Path(args.transcript_path) if args.transcript_path else None
                ),
            )
            reporter.finish("wake-observed" if observed else "observation-ended")
        # The observer exits only on application/session evidence; its boolean
        # result remains available to in-process callers, and its verdict to
        # `poller-health` -- this exit 0 says nothing on its own.
        return 0
    if args.event == "poller-health":
        # `--attempt-id` used to be mandatory and its absence printed NOTHING on
        # either stream, exit 2 -- on the one command the operator notice names for
        # confirming a poller independently, so the check an operator reaches for
        # when a wake looks missing answered with a bare code (ISSUE-REF).  An
        # operator knows the session key; the newest attempt for it is derivable.
        if not args.key:
            print(
                "poller-health needs --key <session-key> "
                "(--attempt-id is optional; the newest attempt is used)",
                file=sys.stderr,
            )
            return 2
        attempt_id = args.attempt_id or _latest_attempt_id(args.key)
        if not attempt_id:
            print(
                json.dumps(
                    {
                        "attempt_id": None,
                        "key": args.key,
                        "phase": None,
                        "reason": "no-attempt-for-key",
                        "verdict": "never-launched",
                    },
                    sort_keys=True,
                )
            )
            return 1
        health = poller_health(args.key, attempt_id)
        print(json.dumps(health, sort_keys=True))
        return 0 if health["verdict"] in POLLER_HEALTHY_VERDICTS else 1
    if args.event == "compact-lifecycle":
        problems = _flag_problems(
            args,
            {
                "--key": None,
                "--attempt-id": None,
                "--interval-seconds": (20, 60),
                "--warning-interval-seconds": (300, 600),
                "--long-wait-escalate-seconds": (1800, 86400),
                "--post-compact-escalate-seconds": (60, 3600),
            },
        )
        if problems:
            return _usage_refusal(args.event, problems)
        run_compact_lifecycle(
            args.key,
            args.attempt_id,
            args.interval_seconds,
            args.warning_interval_seconds,
            args.long_wait_escalate_seconds,
            args.post_compact_escalate_seconds,
            transcript_path=(
                Path(args.transcript_path) if args.transcript_path else None
            ),
            claim_post_compact_wake=not args.observe_post_compact_only,
            typed_wake_override=args.enable_typed_wake or None,
        )
        # The exit code stays 0 (an observer), so stdout is the one channel the
        # launching session reads unasked: Claude Code hands it back with the
        # background task's completion. It was empty on every path (ISSUE-REF).
        health = poller_health(args.key, args.attempt_id)
        print(
            f"compact-lifecycle exit: key={args.key} attempt={args.attempt_id} "
            f"verdict={health['verdict']} reason={health.get('reason')}"
        )
        if health["verdict"] not in POLLER_HEALTHY_VERDICTS:
            print(
                "AUTO-COMPACT DID NOT COMPLETE for this attempt and nothing is "
                "retrying; the session's resume notice explains why."
            )
        return 0
    if args.event == "claim-wake-from-stdin":
        return claim_wake_from_stdin()
    if args.event == "reauth-wake":
        # The sentinel hands over the two ISO stamps it already persists, so the
        # window is read from the same `_transcript_timestamp` parser the records
        # inside it are compared with -- one clock, not two.
        dead_from = _transcript_timestamp(args.dead_from)
        dead_to = _transcript_timestamp(args.dead_to)
        if dead_from is None or dead_to is None or dead_from > dead_to:
            return _usage_refusal(
                args.event,
                [
                    "--dead-from and --dead-to must both be ISO timestamps, "
                    f"from <= to (got {args.dead_from!r}, {args.dead_to!r})"
                ],
            )
        for record in run_reauth_wake(dead_from, dead_to):
            print(json.dumps(record, sort_keys=True))
        return 0
    if args.event in {"compact", "continue"}:
        problems = _flag_problems(args, {"--key": None, "--pane": None})
        if problems:
            return _usage_refusal(args.event, problems)
        supervisor = ContextSupervisor()
        record = (
            supervisor.arm_compact(args.key, args.pane)
            if args.event == "compact"
            else supervisor.arm_continuation(args.key, args.pane)
        )
        if record is not None:
            print(json.dumps(record, sort_keys=True))
        return 0
    if args.event == "force-gate":
        try:
            payload = json.load(sys.stdin)
            if isinstance(payload, dict):
                output = force_gate_output(payload, _derive_session_key() or "unknown")
                if output is not None:
                    print(json.dumps(output, separators=(",", ":")))
        except Exception:  # noqa: BLE001, S110 - fail-open at the hook boundary
            pass
        return 0
    return hook_main(args.event)


if __name__ == "__main__":
    raise SystemExit(main())
