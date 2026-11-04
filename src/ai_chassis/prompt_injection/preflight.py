"""Checks that refuse BEFORE a single keystroke, each with a named reason that
becomes the attempt's terminal ``REFUSED`` state."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ai_chassis.prompt_injection import _tmux
from ai_chassis.prompt_injection.capability import check_capability
from ai_chassis.prompt_injection.config import (
    LEGACY_USE_CASES,
    builtin_registry,
    claude_root,
    legacy_injection_enabled,
    stage_walk,
)
from ai_chassis.prompt_injection.payload import Payload
from ai_chassis.prompt_injection.targets import SessionTarget


@dataclass(frozen=True)
class PreflightVerdict:
    passed: bool
    reason: str | None = None
    detail: str = ""

    def __bool__(self) -> bool:
        return self.passed


PASS = PreflightVerdict(True)


def refuse(reason: str, detail: str = "") -> PreflightVerdict:
    return PreflightVerdict(False, reason, detail)


class Preflight(ABC):
    #: False for a check that needs no resolved target, so it runs before any
    #: tmux call (a disabled use case never touches the pane).
    requires_target = True

    @abstractmethod
    def check(
        self, target: SessionTarget | None, payload: Payload, config: dict[str, Any]
    ) -> PreflightVerdict: ...


class InjectionEnabled(Preflight):
    """The library's ``enabled``, the use case's ``enabled``, and for a legacy use
    case the host system's own ``injection_enabled``, so the library never fires
    where the legacy system would have declined."""

    requires_target = False

    def __init__(self, use_case: str) -> None:
        self.use_case = use_case

    def check(self, _target, _payload, config):
        if not config.get("enabled", False):
            reason = config.get("invalid_reason")
            return refuse(
                "disabled", f"library config invalid: {reason}" if reason else "library"
            )
        entry = config.get("use_cases", {}).get(self.use_case)
        if not isinstance(entry, dict) or not entry.get("enabled", False):
            return refuse("disabled", f"use_cases.{self.use_case}")
        if self.use_case in LEGACY_USE_CASES and not legacy_injection_enabled():
            return refuse("disabled", "context-supervisor injection_enabled")
        return PASS


class VerifiedSlackReply(Preflight):
    """The verbatim exception binds a signed Slack record to this target and text."""

    def __init__(self, record, public_key, inbox):
        self.record, self.public_key, self.inbox = record, public_key, inbox

    def check(self, target, payload, _config):
        from ai_chassis.reply_router.grammar import payload_for
        from ai_chassis.reply_router.signing import verify_reply

        if self.record is None or self.public_key is None:
            return refuse("unverified-reply")
        try:
            verify_reply(self.record, Path(self.public_key))
            if self.record["channel"] != "slack" or self.record["kind"] == "note":
                raise ValueError("unverified-reply")
            name = self.record["target"].split("@", 1)[0]
            if target is None or name not in (target.key, target.session_name):
                return refuse("reply-target-mismatch")
            expected = payload_for(self.record)
            if asdict(payload) == asdict(expected):
                return PASS
            if self.inbox is not None and payload.kind == "prompt":
                path = Path(self.inbox) / f"{self.record['delivery_id']}.md"
                if (
                    payload.text == f"Read the reply in {path}"
                    and path.read_text(encoding="utf-8") == self.record["text"]
                ):
                    return PASS
        except (OSError, ValueError):
            return refuse("unverified-reply")
        return refuse("reply-payload-mismatch")


class TransportAvailable(Preflight):
    """tmux SERVER at or above the floor, a resolving fingerprint, and
    ``$TMUX_PANE`` for a self target: ``no-tmux`` / ``tmux-version`` / ``no-pane``,
    the reasons ``launch_transport_reason`` emits today."""

    def __init__(self, tmux_command: _tmux.Command = _tmux.DEFAULT_COMMAND) -> None:
        self.tmux_command = tmux_command

    def check(self, target, _payload, _config):
        version = _tmux.server_version(self.tmux_command)
        if version is None:
            return refuse("no-tmux")
        if not _tmux.version_allowed(version):
            return refuse("tmux-version", f"server={version}")
        if target is None or (target.is_self and not os.environ.get("TMUX_PANE")):
            return refuse("no-pane")
        if _tmux.fingerprint(self.tmux_command, target.pane_id) is None:
            return refuse("no-pane", target.pane_id)
        return PASS


def _version_tuple(version: str | None) -> tuple[int, ...] | None:
    match = re.match(r"\s*(\d+(?:\.\d+)*)", version or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _plugin_roots() -> list[tuple[str, Path]]:
    path = claude_root() / "plugins" / "installed_plugins.json"
    try:
        plugins = json.loads(path.read_text(encoding="utf-8")).get("plugins", {})
    except (OSError, ValueError, AttributeError):
        return []
    roots: list[tuple[str, Path]] = []
    if not isinstance(plugins, dict):
        return roots
    for name, installs in plugins.items():
        for install in installs if isinstance(installs, list) else []:
            location = install.get("installPath") if isinstance(install, dict) else None
            if isinstance(location, str):
                roots.append((str(name).split("@", 1)[0], Path(location)))
    return roots


def command_paths(target: SessionTarget | None, command: str) -> list[Path]:
    """Where the target session resolves a user, project or plugin ``command``.

    Generalises ``queue_waker.nudge_command_paths`` (scripts/queue_waker.py
    194-221, ISSUE-REF) from ``wq`` to any name, plus installed plugins.
    """
    name = command.removeprefix("/")
    plugin, _, bare = name.rpartition(":")
    roots = [claude_root()]
    if target is not None and target.cwd is not None:
        roots.append(target.cwd / ".claude")
    paths = [
        path
        for root in roots
        for path in (
            root / "skills" / name / "SKILL.md",
            root / "commands" / f"{name}.md",
        )
    ]
    for plugin_name, root in _plugin_roots():
        if plugin and plugin != plugin_name:
            continue
        paths += [root / "commands" / f"{bare}.md", root / "skills" / bare / "SKILL.md"]
    return paths


def command_available(target: SessionTarget | None, command: str) -> PreflightVerdict:
    """Whether ``command`` resolves in the target session before anything is typed.

    A built-in resolves by the registry's Claude Code version floor against the
    target's ``cc_version`` (an unreadable version refuses); anything else
    resolves only by a file in the locations :func:`command_paths` names.
    """
    builtin = builtin_registry().get(command)
    if builtin is not None:
        floor = builtin["since"]
        have = _version_tuple(target.cc_version if target else None)
        if have is None:
            return refuse(
                "unknown-command",
                f"{command} needs Claude Code >= {floor}; version unreadable",
            )
        if have < (_version_tuple(floor) or (0,)):
            return refuse(
                "unknown-command",
                f"{command} needs Claude Code >= {floor}; target runs {target.cc_version}",
            )
        return PASS
    if any(path.is_file() for path in command_paths(target, command)):
        return PASS
    return refuse("unknown-command", f"{command} is not installed for this session")


class SlashCommandInstalled(Preflight):
    def __init__(self, *, refusal_reason: str | None = None):
        self.refusal_reason = refusal_reason

    def check(self, target, payload, _config):
        if payload.kind != "slash-command" or payload.command is None:
            return PASS
        verdict = command_available(target, payload.command)
        if not verdict and self.refusal_reason is not None:
            return refuse(self.refusal_reason, verdict.detail)
        return verdict


def _shift_enter_is_newline() -> bool:
    """Whether Claude Code's keybindings map ``shift+enter`` to ``chat:newline`` in the
    Chat context, the binding the T-1.7 spike found and arm A depended on."""
    try:
        config = json.loads(
            (claude_root() / "keybindings.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    blocks = config.get("bindings") if isinstance(config, dict) else None
    return any(
        isinstance(block, dict)
        and block.get("context") == "Chat"
        and isinstance(block.get("bindings"), dict)
        and block["bindings"].get("shift+enter") == "chat:newline"
        for block in (blocks if isinstance(blocks, list) else [])
    )


def multiline_preconditions_missing(
    tmux_command: _tmux.Command, pane_id: str | None, *, check_key_mode: bool = True
) -> list[str]:
    """What stops ``S-Enter`` being a soft newline; each one makes it submit a
    partial command.

    Measured on tmux 3.7c with throwaway servers: with the SERVER option
    ``extended-keys`` off tmux sends ``S-Enter`` as a bare carriage return; with
    it on, it still does unless the pane's application asked for extended keys
    while the option was on, which ``#{pane_key_mode}`` reports (``Ext 2`` sent
    ``CSI 27;2;13 ~``; ``VT10x`` sent a carriage return, including for a pane
    that asked before the option was turned on). Without the keybinding Claude
    Code reads the sequence as Enter."""
    missing = []
    result = _tmux.run(tmux_command, ["show-options", "-s", "-v", "extended-keys"])
    value = (
        result.stdout.strip() if result is not None and result.returncode == 0 else ""
    )
    if value != "on":
        missing.append(f"tmux extended-keys={value or 'unreadable'}, needs on")
    if check_key_mode:
        key_mode = (
            _tmux.display(tmux_command, pane_id, "#{pane_key_mode}")
            if pane_id
            else None
        )
        if not (key_mode or "").startswith("Ext"):
            missing.append(
                f"pane {pane_id} key mode={key_mode or 'unreadable'}, needs Ext 1 or Ext 2"
            )
    if not _shift_enter_is_newline():
        missing.append(
            f"{claude_root() / 'keybindings.json'} binds no shift+enter -> chat:newline in Chat"
        )
    return missing


def walk_blockers(
    tmux_command: _tmux.Command,
    target: SessionTarget | None,
    config: dict[str, Any],
) -> list[str]:
    """Why the pre-submit walk (D-16) cannot run on ``target``; empty when it can. The
    walk types its filler rows with ``S-Enter``, so it needs every soft-newline
    precondition ``multiline`` needs."""
    if not stage_walk(config)["enabled"]:
        return ["stage_walk.enabled=false"]
    return multiline_preconditions_missing(
        tmux_command, target.pane_id if target is not None else None
    )


#: The common capability-check recovery procedure for multiline and the walk.
REVERIFY_PROCEDURE = (
    "docs/designs/issue-ref-prompt-injection-library.md "
    "§ Re-verifying a Claude Code version"
)

#: A check that measured nothing about the version: taken while machine-contention read
#: anything but ADMIT, or another process still measuring (ISSUE-REF).
UNMEASURED_PREFIXES = ("probe-inconclusive:", "probe-deferred:")


def unmeasured(failure: str) -> bool:
    return failure.startswith(UNMEASURED_PREFIXES)


def capability_failure(
    target: SessionTarget | None, settings: dict[str, Any]
) -> str | None:
    """The capability check's own failure for ``target``'s version, or None when
    committed evidence or a passing measurement admits it. Errors are failures."""
    if target is not None and target.cc_version in settings["verified_cc_versions"]:
        return None
    try:
        return check_capability(target.cc_version if target is not None else None)
    except (OSError, subprocess.SubprocessError) as error:
        return f"probe-error: {type(error).__name__}"


def version_unverified(
    target: SessionTarget | None, settings: dict[str, Any]
) -> str | None:
    """Use committed evidence or measure the capability in a disposable session.

    Both multiline and walk admission call this gate before staging. Errors are
    refusals with the failing check, never an implicit permission to type.
    """
    return unverified_detail(target, settings, capability_failure(target, settings))


def unverified_detail(
    target: SessionTarget | None, settings: dict[str, Any], failure: str | None
) -> str | None:
    if failure is None:
        return None
    running = target.cc_version if target is not None else None
    return (
        f"target runs {running or 'an unreadable version'}; "
        f"capability check {failure}; verified "
        f"{', '.join(settings['verified_cc_versions'])}; re-verify: {REVERIFY_PROCEDURE}"
    )


def repair_message(
    version: str | None, failure: str, outcome: str, *, label: str
) -> str:
    """The one line a failed capability check prints for the session that ran it: the
    check and its observation, what this run did (``outcome``), and the billing route's
    ``/delegate ... /bug-fix`` command (ISSUE-REF). A check that measured nothing about
    the version asks for a re-run instead (ISSUE-REF). ``label`` names what ran it."""
    named = f"Claude Code {version}" if version else "an unreadable Claude Code version"
    if unmeasured(failure):
        return (
            f"The {label} capability check on {named} measured nothing about the "
            f"version ({failure}); {outcome}. Re-run {label}: the next check measures "
            "again, and a measurement taken while machine-contention reads ADMIT is the "
            "version's result."
        )
    import ai_chassis.delegation  # noqa: PLC0415 - the billing route, read at call time

    check, _, observation = failure.partition(": ")
    seen = f"observation: {observation or 'none recorded'}"
    return (
        f"{named[0].upper()}{named[1:]} failed the {label} capability check {check} "
        f"({seen}); {outcome}. Run this now: "
        f"{ai_chassis.delegation.bug_fix_delegation()} The {label} capability check "
        f"{check} failed on {named} ({seen}). Find whether that version changed the "
        "composer input path or the probe itself is wrong, fix the cause, and re-run "
        f"{label} (procedure: {REVERIFY_PROCEDURE})."
    )


def walk_version_mismatch(
    target: SessionTarget | None, config: dict[str, Any]
) -> str | None:
    """None when ``target`` runs a Claude Code version the walk's scroll, wrap and
    history behaviour was measured on."""
    return version_unverified(target, config["multiline_mode"])


#: Cells of every composer row that are not the draft's: the cursor glyph and its
#: gap on the first row, the two-space continuation indent on the rest (recorded in
#: ISSUE-REF's 2.1.283 frame and drawn the same by the stub composer).
COMPOSER_GUTTER_COLUMNS = 2


def wrapped_rows(text: str, pane_width: int) -> int:
    """The composer rows ``text`` takes at ``pane_width``: per line, its length over
    the row's text width rounded up, and at least one row for an empty line."""
    columns = max(1, pane_width - COMPOSER_GUTTER_COLUMNS)
    return sum(max(1, math.ceil(len(line) / columns)) for line in text.split("\n"))


class PayloadTypable(Preflight):
    """``type``: one line, at most ``max_typed_chars``. A newline would be an Enter
    mid-command, and a long draft collapses into a ``[Pasted text #N]`` placeholder
    that submits a slash command as text (#85654).

    ``multiline`` (D-13 Outcome): enabled, at most ``multiline_mode.max_chars`` with
    every line under ``chunk_chars``, admitted by shipped evidence or the private
    capability probe, with both soft-newline preconditions present. The version
    and the preconditions are the target's, so this check needs a resolved target."""

    #: Rows the composer must show beyond the payload's own (D-15).
    slack_rows = 1

    def __init__(self, tmux_command: _tmux.Command = _tmux.DEFAULT_COMMAND) -> None:
        self.tmux_command = tmux_command
        self.capability_verified = False
        #: The capability check's own failure in this check, for the repair line every
        #: use case reports (ISSUE-REF); None when it passed or did not run.
        self.capability_failure: str | None = None
        self.capability_seconds = 0.0

    def _version_check(self, target, settings):
        started = time.monotonic()
        try:
            self.capability_failure = capability_failure(target, settings)
            return unverified_detail(target, settings, self.capability_failure)
        finally:
            self.capability_seconds += time.monotonic() - started

    def fits_composer(
        self,
        payload: Payload,
        pane_width: int | None,
        visible_rows: int | None,
        *,
        target: SessionTarget | None = None,
        config: dict[str, Any] | None = None,
    ) -> PreflightVerdict:
        """ISSUE-REF D-15 (b): refuse, before any keystroke, a payload that needs more
        rows than the composer shows minus ``slack_rows``. Measured live: a 645-
        character nudge in a 46-column pane whose composer showed 4 rows was typed,
        could not be verified, and had to be deleted again. The geometry comes from
        the readiness sample that proved the composer empty; when either number is
        unreadable this refuses, so nothing is typed into a pane we cannot measure.

        D-16: when the walk can run on ``target`` (``stage_walk.enabled`` and every
        soft-newline precondition), an over-tall payload is admitted and walked before
        Enter; without shipped evidence or a passing capability check it refuses
        ``walk-unverified-version`` instead, still before any keystroke."""
        if pane_width is None or visible_rows is None:
            return refuse(
                "payload-exceeds-visible-rows",
                f"geometry unreadable: pane_width={pane_width} visible={visible_rows}",
            )
        needed = wrapped_rows(payload.text, pane_width)
        if needed <= visible_rows - self.slack_rows:
            return PASS
        if config is not None and not walk_blockers(self.tmux_command, target, config):
            mismatch = walk_version_mismatch(target, config)
            if mismatch is None:
                return PASS
            return refuse(
                "walk-unverified-version",
                f"needed={needed} visible={visible_rows}; {mismatch}",
            )
        return refuse(
            "payload-exceeds-visible-rows",
            f"needed={needed} visible={visible_rows}",
        )

    def check(self, target, payload, config):
        self.capability_verified = False
        self.capability_failure = None
        if payload.mode == "multiline":
            return self._multiline(target, payload, config["multiline_mode"])
        limit = int(config["max_typed_chars"])
        if "\n" in payload.text or "\r" in payload.text:
            return refuse("payload-too-long", "type mode carries one line")
        if len(payload.text) > limit:
            return refuse("payload-too-long", f"{len(payload.text)} > {limit}")
        # Even a short draft can need the walk if its plain verify fails, so every
        # payload is checked before its first keystroke, the way /start-goal does
        # (ISSUE-REF). A pass admits the walk. A failure leaves it off and is reported
        # with its repair; it refuses no line the plain verify can prove, so a new Claude
        # Code version never blocks a one-line delivery such as a restart's /exit
        # (ISSUE-REF R-1). A line the composer cannot show still refuses before typing.
        if (
            target is not None
            and target.cc_version in config["multiline_mode"]["verified_cc_versions"]
        ):
            self.capability_verified = True
        elif not walk_blockers(self.tmux_command, target, config):
            mismatch = self._version_check(target, config["multiline_mode"])
            self.capability_verified = mismatch is None
        return PASS

    def wait_ready(self, target, payload, config, deadline, poll):
        """Allow a fresh application's extended-key negotiation to finish within
        the injector's shared readiness deadline. Static guards still refuse at
        once; no keystroke may precede the successful check."""
        self.capability_seconds = 0.0
        while True:
            prior_capability_seconds = self.capability_seconds
            verdict = self.check(target, payload, config)
            deadline += self.capability_seconds - prior_capability_seconds
            if verdict.reason != "multiline-preconditions-missing":
                return verdict
            if multiline_preconditions_missing(
                self.tmux_command,
                target.pane_id if target is not None else None,
                check_key_mode=False,
            ):
                return verdict
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return verdict
            time.sleep(min(poll, remaining))

    @staticmethod
    def _multiline_too_long(text: str, settings: dict[str, Any]) -> str | None:
        if "\r" in text:
            return "a carriage return is an Enter"
        limit = int(settings["max_chars"])
        if len(text) > limit:
            return f"{len(text)} > {limit}"
        chunk = int(settings["chunk_chars"])
        longest = max(len(line) for line in text.split("\n"))
        return f"a line of {longest} >= {chunk}" if longest >= chunk else None

    def _multiline(self, target, payload, settings):
        if not settings["enabled"]:
            return refuse("multiline-mode-disabled")
        too_long = self._multiline_too_long(payload.text, settings)
        if too_long is not None:
            return refuse("payload-too-long", too_long)
        unverified = self._version_check(target, settings)
        if unverified is not None:
            return refuse("multiline-unverified-version", unverified)
        self.capability_verified = True
        missing = multiline_preconditions_missing(
            self.tmux_command, target.pane_id if target is not None else None
        )
        if missing:
            return refuse("multiline-preconditions-missing", "; ".join(missing))
        return PASS


class GoalStateIs(Preflight):
    """The caller's goal guard, offered as a preflight: ``GoalStateIs(inactive=True)``
    refuses ``goal-active``; ``GoalStateIs(inactive=False)`` refuses ``goal-not-active``."""

    def __init__(self, *, inactive: bool) -> None:
        self.inactive = inactive

    def check(self, target, _payload, _config):
        from ai_chassis.prompt_injection.verify import goal_state

        state = goal_state(target) if target is not None else "unknown"
        if self.inactive and state == "active":
            return refuse("goal-active")
        if not self.inactive and state != "active":
            return refuse("goal-not-active", f"goal_state={state}")
        return PASS


class DisposableTarget(Preflight):
    """Only a session launched for the purpose may receive the payload: never the
    caller's own (``target-is-self``), and never a session whose tmux name lacks
    ``prefix`` (``target-not-disposable``). A simulated owner prompt typed into a
    working session would be an instruction that session's user never gave."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def check(self, target, _payload, _config):
        if target is None:
            return refuse("no-pane")
        if target.is_self:
            return refuse("target-is-self", target.pane_id)
        if not (target.session_name or "").startswith(self.prefix):
            return refuse(
                "target-not-disposable",
                f"session={target.session_name!r} wants prefix {self.prefix!r}",
            )
        return PASS
