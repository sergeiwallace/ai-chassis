"""The detached-job ledger: what a session moved to the background when it exited.

Choosing "Move to background and exit" hands the live conversation to a supervisor-run
background session. The restart that follows needs to know that this happened and what the
dialog listed, which ``claude agents --json`` does not say. The restart path writes one JSON
file per session key under ``<resume root>/detached/``; the launch-resume prompt builder reads
it back.

The file is a hint, never the authority (the chief's ruling on ISSUE-REF). Whether the
background session still exists is answered by ``claude agents --json`` read at resume time,
filtered by kind so the fleet's own interactive sessions never match. A record whose session
that list does not show is reported ``stopped`` and is never reattached.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "detached-ledger/v1"
LEDGER_DIRNAME = "detached"
INTERACTIVE_KIND = "interactive"
# Row words ``claude agents --json`` uses, in ``state`` or ``status``, for a session that is
# over. ``done`` is not one: a done row is a live copy whose turn ended and which waits for
# input (its process is alive, its status idle). A listed row with any other word, or none,
# counts as running: absence from the list is what proves a stop.
FINISHED_STATUSES = frozenset({"failed", "stopped", "exited", "killed"})


@dataclass(frozen=True)
class Verdict:
    """The state of one ledger record against the live agents list."""

    state: str  # "running", "finished" or "stopped"
    reason: str


def default_root() -> Path:
    """``~/.claude/resume``, where the resume messages already live."""
    return Path.home() / ".claude" / "resume"


def ledger_path(key: str, root: Path | None = None) -> Path:
    """The file for session ``key``; a key that is not a plain name is refused."""
    if not key or key != Path(key).name or key.startswith("."):
        msg = f"ledger key must be a plain session name, got {key!r}"
        raise ValueError(msg)
    return (root or default_root()) / LEDGER_DIRNAME / f"{key}.json"


def record(  # noqa: PLR0913 - one field per fact the ruling says to stamp
    key: str,
    background_session: str,
    labels: Iterable[str],
    *,
    cc_version: str,
    name: str | None = None,
    cwd: str | None = None,
    writer_pid: int | None = None,
    writer_started: str | None = None,
    root: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Write the record for ``key`` atomically (temp file, then rename) and return its path.

    Stamps the writer's pid, start time and Claude Code version, so a reader can tell a record
    written by a live restart from one a crashed writer left.
    """
    path = ledger_path(key, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "schema": SCHEMA,
        "key": key,
        "background_session": background_session,
        "labels": [str(label) for label in labels],
        "written_at": stamp,
        "writer_pid": os.getpid() if writer_pid is None else writer_pid,
        "writer_started": writer_started or stamp,
        "cc_version": cc_version,
    }
    if name is not None and cwd is not None:
        body.update(
            name=name,
            cwd=cwd,
            stopped_items=[],
            stopped_items_confirmed=False,
            unproven=True,
        )
    _write(path, body)
    return path


def _write(path: Path, body: Mapping[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, sort_keys=True)
            handle.write("\n")
        Path(tmp_name).replace(path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def update(key: str, changes: Mapping[str, Any], *, root: Path | None = None) -> None:
    """Atomically extend an existing record, retaining identity and writer stamps.

    The restart saves the dialog's stop list in ``labels`` before choosing its move row.
    ``finish-background`` adds the new id and ``stopped_items`` from the launcher's capture;
    until that evidence is readable, ``stopped_items_confirmed`` remains false.
    """
    rec = load(key, root)
    if rec is None:
        raise ValueError("detached record unavailable")
    _write(ledger_path(key, root), {**rec, **changes})


def load(key: str, root: Path | None = None) -> dict[str, Any] | None:
    """The record for ``key``, or ``None`` when absent, unreadable or not this schema."""
    try:
        data = json.loads(ledger_path(key, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    if (
        not isinstance(data.get("background_session"), str)
        or not data["background_session"]
    ):
        return None
    return data


def is_moved_copy(row: Mapping[str, Any], name: str, cwd: str) -> bool:
    """Whether ``row`` is a background copy of the session called ``name`` in ``cwd``.

    Claude Code numbers a new background copy " (N)" when an older copy already holds the bare
    name, so the moved copy is named ``name`` or ``name (N)``. An interactive row is never one.
    """
    kind = row.get("kind")
    if not kind or kind == INTERACTIVE_KIND or row.get("cwd") != cwd:
        return False
    return (
        re.fullmatch(rf"{re.escape(name)}( \(\d+\))?", str(row.get("name") or ""))
        is not None
    )


def _row_ids(row: Mapping[str, Any]) -> set[str]:
    return {str(row[name]) for name in ("jobId", "id", "name") if row.get(name)}


def classify(rec: Mapping[str, Any], agent_rows: Iterable[Any]) -> Verdict:
    """Judge ``rec`` against the rows ``claude agents --json`` returned.

    Interactive rows are ignored. A matching background row that is not over means the
    session is running; a matching row that is over means it finished; no matching row means
    it stopped, and the caller must not try to reattach it.
    """
    wanted = str(rec.get("background_session", ""))
    for row in agent_rows:
        if not isinstance(row, Mapping) or row.get("kind") == INTERACTIVE_KIND:
            continue
        if wanted not in _row_ids(row):
            continue
        # The over word is carried by ``state`` (a stopped row has no status), so read both.
        status = next(
            (
                word
                for word in (
                    str(row.get(name) or "").lower() for name in ("status", "state")
                )
                if word in FINISHED_STATUSES
            ),
            "",
        )
        if status:
            return Verdict("finished", f"claude agents lists it as {status}")
        return Verdict("running", "claude agents lists it as a background session")
    return Verdict("stopped", "claude agents does not list it; do not reattach")


MAX_NOTE_LABELS = 5
MAX_LABEL_CHARACTERS = 80
# What the exit dialog calls a session cron in its stop list.
SCHEDULED_KIND = "scheduled task"


def stop_kind(label: str) -> str:
    """The kind a stop-list row names, without the padding the dialog adds to align them.

    A row with no separator names no kind that can be classified, so it reads as empty.
    """
    kind, separator, _ = label.partition("·")
    return " ".join(kind.split()) if separator else ""


def describe(rec: Mapping[str, Any] | None) -> str:
    """The launch-resume prompt's one-paragraph account of ``rec``, or the no-record line.

    The prompt builder runs no subprocess, so this states the record as a hint and tells the
    resumed session how to check it. The record's labels are cut to keep the prompt bounded.
    """
    if rec is None:
        return "(no ledger record: the previous run did not choose Move to background and exit)"
    if rec.get("unproven"):
        return (
            f"the previous run prepared a background handoff for {rec.get('name')} in "
            f"{rec.get('cwd')}, but its background id is unproven. Check `claude agents --json` "
            "for non-interactive rows in that cwd named "
            f"{rec.get('name')} or {rec.get('name')} (2), (3) and so on: Claude Code numbers the "
            "moved copy when an older copy holds the bare name. A row whose state is not stopped "
            "is a live copy of this conversation, including state done (a copy waiting for "
            "input): do not work beside it, say so plainly."
        )
    session = str(rec["background_session"])
    labels = [str(label)[:MAX_LABEL_CHARACTERS] for label in rec.get("labels") or []]
    listed = "; ".join(labels[:MAX_NOTE_LABELS]) or "no labels recorded"
    if len(labels) > MAX_NOTE_LABELS:
        listed += f"; and {len(labels) - MAX_NOTE_LABELS} more"
    note = (
        f"the previous run moved to background session {session} at "
        f"{rec.get('written_at', 'an unknown time')}; the dialog listed: {listed}. "
        "This record is a hint. Run `claude agents --json`, ignore rows whose kind is "
        f"interactive, and look for {session}: if it is listed and running, monitor or attach "
        "it; if it is not listed, treat it as stopped and do not reattach it."
    )
    scheduled = sum(
        stop_kind(str(label)) == SCHEDULED_KIND for label in rec.get("labels") or []
    )
    if scheduled:
        note += (
            f" The dialog listed {scheduled} scheduled job{'s' if scheduled != 1 else ''}, "
            "which keep running in the background session. Once attached, run CronList and "
            "confirm each is still listed; if one is missing, say so plainly. The dialog "
            "truncates a job's prompt, so a lost job cannot be recreated from this record."
        )
    return note
