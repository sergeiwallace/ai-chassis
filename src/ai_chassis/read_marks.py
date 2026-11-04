"""Read marks: a session's proved read of one carried notice line (ISSUE-REF D-9, D-10).

A read verb that printed a message's full body to the calling session writes ONE file,
``<state>/read-marks/<slug>.<rung>.<message key>``, created with ``O_CREAT|O_EXCL``. The notifier's
``_carry`` settles a carried line on a matching mark exactly as it does on a hook claim, so the
daemon stays the only writer of the delivered ledger, the unclaimed store and the receipts. The
exclusive create is the whole concurrency story: two sessions or two runs of the same read never
race, and a second read of the same message is a no-op that reports ``False``.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

MARKS_DIR = "read-marks"
#: D-12: the evidence a backlog line's `read` receipt carries; it was cleared, not proved read.
VIA_BACKLOG = "backlog-clear"
_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_KEY = re.compile(r"\d+\Z")


def marks_dir(root: Path) -> Path:
    return root / MARKS_DIR


def _name(slug: str, rung: str, key: int) -> str:
    if not _SAFE.fullmatch(slug) or not _SAFE.fullmatch(rung) or "." in rung:
        raise ValueError(f"unsafe read-mark name part: {slug!r} {rung!r}")
    return f"{slug}.{rung}.{int(key)}"


def mark(  # noqa: PLR0913  # one keyword per field of the mark
    root: Path,
    *,
    slug: str,
    rung: str,
    key: int,
    via: str,
    ledger_id: str | None = None,
    now: dt.datetime | None = None,
) -> bool:
    """Record that ``slug`` read message ``key`` on ``rung``. True when this call created it."""
    directory = marks_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _name(slug, rung, key)
    moment = (now or dt.datetime.now(dt.UTC)).replace(microsecond=0)
    body = json.dumps(
        {
            "at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ledger_id": ledger_id,
            "via": via,
        },
        sort_keys=True,
    )
    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(body + "\n")
    return True


def marks(root: Path) -> dict[tuple[str, str, int], dict[str, Any]]:
    """``{(slug, rung, key): record}`` for every well-named mark; an unreadable body reads ``{}``."""
    found: dict[tuple[str, str, int], dict[str, Any]] = {}
    try:
        entries = list(marks_dir(root).iterdir())
    except OSError:
        return found
    for path in entries:
        # From the right: the key is digits and the rung holds no dot, so a slug may.
        head, _, key = path.name.rpartition(".")
        slug, _, rung = head.rpartition(".")
        if not slug or not rung or not _KEY.fullmatch(key):
            continue
        record: Any = {}
        with contextlib.suppress(OSError, ValueError):
            record = json.loads(path.read_text(encoding="utf-8"))
        found[(slug, rung, int(key))] = record if isinstance(record, dict) else {}
    return found


def prune(root: Path, keep: Callable[[str, str, int], bool]) -> None:
    """Drop every mark ``keep`` rejects: a mark whose line has settled has done its one job.

    The caller passes a predicate over the lines still carried, not a snapshot of the marks it
    read, so a mark written after that read and naming a carried line survives to the next pass.
    """
    for slug, rung, key in marks(root):
        if not keep(slug, rung, key):
            with contextlib.suppress(OSError):
                (marks_dir(root) / _name(slug, rung, key)).unlink()
