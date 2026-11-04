"""Deferred install failures: the ledger, its ``[deferred]`` notice and the end report (ISSUE-REF).

A step whose failure does not invalidate the others records it here instead of exiting: every
independent step still runs, the failure is printed where it happens and again at the end of the
install, and the exit status says WHO MUST ACT on what remains.

    0  nothing deferred
    1  at least one gap the fleet can clear itself, or one whose cause is undetermined
    2  EVERY remaining gap is human-gated: a credential, an account, an approval
    3  the report itself failed (an unreadable ledger, an I/O or interpreter error): no
       report was printed, so install.sh keeps the ledger and fails the run

``unknown`` counts with 1, never with 2: exit 2 asserts that only the owner can clear the gap, and
asserting that about an unclassified cause sends a false blocker. A ``human`` gap must name its
blocker, or the reader is left to infer the remedy, which is the failure this module exists to
remove; an unknown class is refused rather than defaulted, so a typo cannot demote a real blocker.

Ledger: the explicit path in ``AI_HARNESS_DEFERRED_FAILURES_FILE``, one JSON row per deferral.
install.sh creates one run-unique file with ``mktemp`` before its first producer, exports it to
every apply subprocess and shell caller (``ai-harness deferred-failure add``), and the
``deferred-failures-report`` step prints it at the end and then removes it: a ledger is only
ever removed after its report printed, so one still present after the step was never reported.
A line the report cannot parse is itself reported, as an ``unknown`` gap naming the line. A process not under install.sh creates
its own on first use and owns its report and cleanup (``finish_owned_run``). Private steps run in
a subprocess of the core and register in-process through ``register``, inheriting the path.

The owner the human-gated lines name is configuration (``install.owner_name`` and
``install.owner_pronoun``), so the public core carries no person's name. Every text field passes
through ``manual_steps.sanitize`` before it is persisted or printed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TextIO

from .manual_steps import sanitize

LEDGER_ENV = "AI_HARNESS_DEFERRED_FAILURES_FILE"
#: The basename prefix of every run ledger; only a file carrying it is ever removed.
LEDGER_PREFIX = "ai-harness-deferred-failures."
CLASSES = ("self", "human", "unknown")
#: The rendering a public install gets when its config names no owner.
DEFAULT_OWNER_NAME = "the owner"
DEFAULT_OWNER_PRONOUN = "they"

EXIT_CLEAN = 0
EXIT_ACTIONABLE = 1
EXIT_HUMAN_ONLY = 2
EXIT_REPORT_FAILED = 3

_owned: Path | None = None
_owned_owner: Owner | None = None
_reported: Path | None = None


@dataclass(frozen=True)
class Owner:
    """Who can clear a human-gated gap, as the notice and the report name them."""

    name: str = DEFAULT_OWNER_NAME
    pronoun: str = DEFAULT_OWNER_PRONOUN


DEFAULT_OWNER = Owner()


def owner_of(install: Mapping[str, object]) -> Owner:
    """The owner a validated ``[install]`` table declares, or the public default."""
    name = install.get("owner_name", DEFAULT_OWNER_NAME)
    pronoun = install.get("owner_pronoun", DEFAULT_OWNER_PRONOUN)
    return Owner(
        name if isinstance(name, str) and name else DEFAULT_OWNER_NAME,
        pronoun if isinstance(pronoun, str) and pronoun else DEFAULT_OWNER_PRONOUN,
    )


@dataclass(frozen=True)
class DeferredFailure:
    failure_class: str
    summary: str
    blocker: str = ""
    actions: list[str] = field(default_factory=list)
    source_step: str = ""

    def row(self) -> dict[str, object]:
        return {
            "class": self.failure_class,
            "summary": self.summary,
            "blocker": self.blocker,
            "actions": list(self.actions),
            "source_step": self.source_step,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> DeferredFailure:
        actions = row.get("actions") or []
        return cls(
            failure_class=str(row.get("class", "")),
            summary=str(row.get("summary", "")),
            blocker=str(row.get("blocker", "")),
            actions=[str(action) for action in actions]
            if isinstance(actions, list)
            else [],
            source_step=str(row.get("source_step", "")),
        )


class DeferralRefused(ValueError):
    """A deferral install.sh would have refused with ``exit 1``; never swallow it."""


def _sanitized(failure: DeferredFailure) -> DeferredFailure:
    return replace(
        failure,
        failure_class=sanitize(failure.failure_class),
        summary=sanitize(failure.summary),
        blocker=sanitize(failure.blocker),
        actions=[sanitize(action) for action in failure.actions],
        source_step=sanitize(failure.source_step),
    )


def refusal(failure: DeferredFailure, owner: Owner = DEFAULT_OWNER) -> str:
    """The FATAL lines for a deferral that may not exist, or "" when it may."""
    if failure.failure_class not in CLASSES:
        return (
            f"FATAL: _defer_failure got an unknown class '{failure.failure_class}' for: "
            f"{failure.summary}\n"
            "       Valid classes: self | human | unknown\n"
        )
    if failure.failure_class == "human" and not failure.blocker:
        return (
            "FATAL: _defer_failure class 'human' requires --blocker naming what only\n"
            f"       {owner.name} can do, and got none for: {failure.summary}\n"
        )
    return ""


def notice(failure: DeferredFailure, owner: Owner = DEFAULT_OWNER) -> str:
    """The ``[deferred]`` block printed where the failure happens."""
    failure = _sanitized(failure)
    lines = [f"  [deferred] {failure.summary}"]
    if failure.failure_class == "human":
        lines.append(
            f"             Class: HUMAN-GATED -- only {owner.name} can clear this."
        )
        lines.append(
            f"             BLOCKER FOR {owner.name.upper()}: {failure.blocker}"
        )
    elif failure.failure_class == "self":
        lines.append(
            "             Class: fleet-fixable -- fixable in this repo or its config."
        )
    else:
        lines.append(
            "             Class: UNDETERMINED -- needs triage to say who must act."
        )
    lines.append(
        "             The install continues. This is reported again at the end,"
    )
    lines.append(
        '             and install.sh exits non-zero -- it does not print "Done."'
    )
    return "\n".join(lines) + "\n"


def ledger_path(
    environ: MutableMapping[str, str] = os.environ, owner: Owner | None = None
) -> Path:
    """This run's ledger; created and exported here when no caller passed one."""
    global _owned, _owned_owner
    value = environ.get(LEDGER_ENV, "").strip()
    if value:
        return Path(value)
    handle, name = tempfile.mkstemp(prefix=LEDGER_PREFIX, suffix=".jsonl")
    os.close(handle)
    environ[LEDGER_ENV] = name
    _owned, _owned_owner = Path(name), owner
    return _owned


def _unreadable_row(path: Path, number: int, line: str) -> DeferredFailure:
    """A ledger line that is not a row: still a gap, so the report says it rather than dropping it."""
    return DeferredFailure(
        failure_class="unknown",
        summary=f"deferred-failure ledger {path} line {number} is not a readable row",
        actions=[f"The line, verbatim: {line[:500]}"],
        source_step="deferred-failures-report",
    )


def load(path: Path) -> list[DeferredFailure]:
    """Every row of ``path``; [] when it does not exist. An unreadable FILE raises ``OSError``."""
    if not path.exists():
        return []
    failures = []
    for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if isinstance(row, dict):
            failures.append(DeferredFailure.from_row(row))
        else:
            failures.append(_unreadable_row(path, number, line))
    return failures


def check(
    failure: DeferredFailure, *, owner: Owner = DEFAULT_OWNER, err: TextIO | None = None
) -> DeferredFailure:
    """``failure`` sanitized, or the FATAL lines on ``err`` and ``DeferralRefused``; records nothing."""
    err = err or sys.stderr
    failure = _sanitized(failure)
    refused = refusal(failure, owner)
    if refused:
        err.write(refused)
        err.flush()
        raise DeferralRefused(refused)
    return failure


def register(
    failure: DeferredFailure, *, owner: Owner = DEFAULT_OWNER, err: TextIO | None = None
) -> None:
    """Record ``failure`` in this run's ledger and print its ``[deferred]`` block on ``err``.

    Raises ``DeferralRefused`` after printing the FATAL lines for an unknown class or a
    ``human`` deferral with no blocker; nothing is recorded then.
    """
    failure = check(failure, owner=owner, err=err)
    err = err or sys.stderr
    path = ledger_path(owner=owner)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(failure.row(), sort_keys=True) + "\n")
    err.write(notice(failure, owner))
    err.flush()


def _action_lines(actions: Sequence[str]) -> list[str]:
    """The remedy lines under ``ACTION:``; install.sh split each on newlines and dropped blanks."""
    if not actions:
        return []
    lines = ["        ACTION:"]
    for action in actions:
        lines.extend(f"          {line}" for line in action.split("\n") if line)
    return lines


def render_report(
    failures: Sequence[DeferredFailure], owner: Owner = DEFAULT_OWNER
) -> tuple[str, int]:
    """The end-of-install report and its exit status; ``("", 0)`` when nothing was deferred."""
    if not failures:
        return "", EXIT_CLEAN
    failures = [_sanitized(failure) for failure in failures]
    human = sum(1 for failure in failures if failure.failure_class == "human")
    actionable = len(failures) - human
    upper = owner.name.upper()
    lines = [
        "",
        f"  [FATAL] {len(failures)} gap(s) remain. Everything independent of them ran, so this",
        "          machine is PARTIALLY provisioned -- neither unprovisioned nor done.",
        f"          {actionable} need(s) a fix here; {human} need(s) {owner.name}.",
    ]
    for failure in failures:
        lines += ["", f"      - {failure.summary}"]
        if failure.failure_class == "human":
            lines += [
                "        CLASS: HUMAN-GATED -- nothing in this repo can clear it, and",
                "               re-running install.sh will not change the result.",
                f"        BLOCKER FOR {upper}: {failure.blocker}",
            ]
        elif failure.failure_class == "self":
            lines += [
                "        CLASS: fleet-fixable -- fix it in this repo or its config, then",
                "               re-run install.sh.",
            ]
        else:
            lines += [
                "        CLASS: UNDETERMINED -- the cause above was not classified, so who",
                "               must act is unknown. Triage it; do not assume either way.",
            ]
        lines += _action_lines(failure.actions)
    lines.append("")
    if actionable:
        lines += [
            "          install.sh EXITS 1: at least one gap above is actionable here and now.",
            "          Fix those, then re-run install.sh: it is idempotent, so what already",
            "          succeeded is not redone.",
        ]
        if human:
            lines += [
                f"          The {human} HUMAN-GATED gap(s) above will still be there afterwards.",
                f"          SURFACE THOSE TO {upper} AS BLOCKERS -- only {owner.pronoun} can "
                "clear them.",
            ]
        return "\n".join(lines) + "\n", EXIT_ACTIONABLE
    lines += [
        "          install.sh EXITS 2: EVERY gap above is HUMAN-GATED. Nothing in this",
        "          repo can clear them, and re-running install.sh will not change the",
        f"          result. SURFACE THESE TO {upper} AS BLOCKERS -- {owner.pronoun} must "
        "action them.",
    ]
    return "\n".join(lines) + "\n", EXIT_HUMAN_ONLY


def report(err: TextIO, owner: Owner = DEFAULT_OWNER) -> int:
    """Print this run's report on ``err``, then remove its ledger; the report step's behaviour.

    Raises on a ledger it cannot read, before removing anything: the caller turns that into
    ``EXIT_REPORT_FAILED`` and the ledger stays for the next reader.
    """
    global _reported
    path = ledger_path(owner=owner)
    text, code = render_report(load(path), owner)
    err.write(text)
    err.flush()
    _reported = path
    remove_run_ledger(path)
    return code


def exit_status(status: int, report_code: int, in_place_class: str = "unknown") -> int:
    """install.sh's ``_exit_reporting_deferred``: a run's status composed with its report's.

    With nothing failed in place the deferrals alone decide. A real in-place failure keeps its
    own status unless a gap is actionable (1), and becomes 2 only when it is itself human-gated.
    """
    if status == 0:
        return report_code
    if report_code == EXIT_ACTIONABLE:
        return EXIT_ACTIONABLE
    if in_place_class == "human":
        return EXIT_HUMAN_ONLY
    return status


def remove_run_ledger(path: Path) -> bool:
    """Remove ``path`` only when it is a regular file named like a run ledger."""
    if (
        not path.name.startswith(LEDGER_PREFIX)
        or path.is_symlink()
        or not path.is_file()
    ):
        return False
    path.unlink()
    return True


def finish_owned_run(
    err: TextIO, environ: MutableMapping[str, str] = os.environ
) -> int:
    """For a process not under install.sh: report what no report step printed, then clean up.

    Returns the report's status, or 0 when this process owns no ledger or already reported it.
    """
    global _owned, _owned_owner, _reported
    path, owner, _owned, _owned_owner = _owned, _owned_owner, None, None
    if path is None:
        return EXIT_CLEAN
    code = EXIT_CLEAN
    if _reported != path:
        text, code = render_report(load(path), owner or DEFAULT_OWNER)
        err.write(text)
        err.flush()
    _reported = None
    remove_run_ledger(path)
    if environ.get(LEDGER_ENV) == str(path):
        del environ[LEDGER_ENV]
    return code
