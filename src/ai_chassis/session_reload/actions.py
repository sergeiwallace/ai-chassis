"""The two actions: ``inject-slash-command`` types a reload into one session through the
prompt-injection library, and ``refresh`` brings a plugin up to date on disk with Claude
Code's own updater (design ISSUE-REF D-3, D-9)."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_chassis.prompt_injection import _tmux
from ai_chassis.prompt_injection.injector import SessionReloadInjector
from ai_chassis.prompt_injection.payload import SlashCommandPayload
from ai_chassis.session_reload.config import Target
from ai_chassis.session_reload.kinds import LiveSession

#: The refresh's wall-clock limit (design § Getting the update onto disk).
REFRESH_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class DeliveryResult:
    """``result`` is ``reloaded``, ``held-cache``, ``queued-by-cc``, ``remote-unavailable``
    or ``other`` once submitted, ``refused`` (with the library's ``reason``) otherwise."""

    submitted: bool
    result: str
    reason: str | None
    attempt_id: str
    stdout_line: str = ""
    staged: bool = False


@dataclass(frozen=True)
class RefreshResult:
    result: str  # updated | up-to-date | needs-acceptance | failed
    from_version: str | None = None
    to_version: str | None = None
    detail: str = ""


def _attempt_record(evidence: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(Path(evidence["attempt"]).read_text(encoding="utf-8"))
    except (KeyError, OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class InjectSlashCommand:
    name = "inject-slash-command"

    def __init__(self, tmux_command: _tmux.Command = _tmux.DEFAULT_COMMAND) -> None:
        self.tmux_command = tmux_command

    def deliver(self, session: LiveSession, command: str) -> DeliveryResult:
        outcome = SessionReloadInjector(tmux_command=self.tmux_command).run(
            f"pane:{session.pane}", SlashCommandPayload(command)
        )
        if not outcome.submitted:
            return DeliveryResult(
                False,
                "refused",
                outcome.reason or outcome.state.lower(),
                outcome.attempt_id,
                staged=outcome.staged,
            )
        detail = str(_attempt_record(outcome.evidence).get("oracle_detail") or "")
        result, _, line = detail.partition(": ")
        if outcome.state != "COMPLETED" or not result:
            result, line = "other", ""
        return DeliveryResult(
            True, result, None, outcome.attempt_id, line, staged=outcome.staged
        )


def _json_line(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def classify_update(code: int, stdout: str) -> RefreshResult:
    """``claude plugin update --json``'s one result line (Claude Code 2.1.310: ``outcome``
    ``ok`` with ``oldVersion``/``newVersion``, or ``failed`` with ``failureCode``; a
    marketplace-declared command it wants a person to accept rides ``shownCommand``)."""
    parsed = _json_line(stdout)
    if parsed is None:
        return RefreshResult("failed", detail=f"no-json exit={code}")
    old, new = parsed.get("oldVersion"), parsed.get("newVersion")
    old = old if isinstance(old, str) else None
    new = new if isinstance(new, str) else None
    message = str(parsed.get("message") or parsed.get("failureCode") or "")[:200]
    if parsed.get("shownCommand"):
        return RefreshResult("needs-acceptance", old, new, message)
    if code != 0 or parsed.get("outcome") != "ok":
        reason = parsed.get("failureCode") or f"exit={code}"
        return RefreshResult("failed", old, new, f"{reason} {message}".strip())
    if new and new != old:
        return RefreshResult("updated", old, new, message)
    return RefreshResult("up-to-date", old, new, message)


class PluginRefresh:
    """``claude plugin update <plugin> --json`` with stdin closed and a 120 s limit. It
    never passes ``--yes`` or ``--accept-command``: a command needing acceptance is
    reported, never accepted on a person's behalf."""

    name = "refresh"

    def run(self, target: Target) -> RefreshResult:
        claude = shutil.which("claude")
        if claude is None:
            return RefreshResult("failed", detail="claude-not-found")
        argv = [claude, "plugin", "update", str(target.options["plugin"]), "--json"]
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=REFRESH_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return RefreshResult("failed", detail=f"timeout={REFRESH_TIMEOUT_SECONDS}s")
        except OSError as error:
            return RefreshResult("failed", detail=f"exec-error {error}")
        return classify_update(done.returncode, done.stdout)
