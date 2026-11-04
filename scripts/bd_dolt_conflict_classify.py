#!/usr/bin/env python3
# ruff: noqa: E402
"""Say whether a diverged Dolt data plane can be merged WITHOUT losing content.

``bd dolt push`` rejected non-fast-forward means the two peers must merge, and on the
AIH store that merge stops on 44 conflicts (43 ``issues``, 1 ``dependencies``).  The
count is the wrong question.  A Dolt conflict means both sides wrote the same cell to
different values -- and bd writes several cells on EVERY update that carry no content
at all, so a conflict count cannot distinguish "two sessions disagree about an issue"
from "two sessions each touched the same row".  Measured on the live AIH store
2026-09-25: of 43 ``issues`` conflicts, **27 differed only in ``row_lock``** -- a random
64-bit nonce bd rewrites on every write (base 0, ours -2214605988570740094, theirs
3853522532863038711 on ``ISSUE-REF``) -- with every other column byte-equal on both
sides.  Resolving those 27 either way loses nothing, and no rule is needed to do it.

So this script classifies each conflicting row instead of counting it, and it does so
**read-only**: the only statement it issues is ``dolt_preview_merge_conflicts``, which
reports what a merge WOULD conflict on without entering one.  That matters more here
than in most places.  Entering a merge on this store and leaving it conflicted blocks
``git push`` for every concurrent session on the machine, because the pre-push
``bd-invariants`` gate reads the store -- an outage measured on 2026-09-24.

THE FOUR VERDICTS, and why only the last one needs a human.

``bookkeeping-only``  No column outside :data:`BOOKKEEPING_COLUMNS` differs.  Those four
    are not authored: ``row_lock`` is a per-write nonce, ``content_hash`` is derived from
    the content, ``updated_at`` is stamped by the write, and ``is_blocked`` is derived
    from the dependency edges.  Either side may be taken.  (A derived value taken from
    the losing side stays stale until the next write of that row, which is why this is
    reported as loss-free rather than as correct.)
``remote-superset`` / ``local-superset``  Every differing content cell is CONTAINED by
    one side: that side's value is the other's plus more (an empty ``close_reason``
    against a populated one, or a ``notes`` that is a strict substring of the other), and
    any ``status`` difference advances in the same direction.  Containment is measured,
    so taking the containing side is not a preference and not a tie-break.
``two-sided``  At least one content cell holds text neither side contains, or the cells
    disagree about which side is ahead.  **No rule inside this script can resolve these.**
    They are the whole output: the operator learns the exact, usually tiny, set of records
    that needs a stated and justified rule, instead of being handed 44 opaque conflicts.

A bare timestamp is reported ``two-sided`` deliberately, because no containment orders
one.  The live ``dependencies`` conflict is exactly that shape: both sides retyped the
same edge ``relates-to`` -> ``parent-child``, so the content agrees, and only
``created_at`` differs (ours ``2026-09-25 00:10:32``, theirs ``2026-09-17 04:53:18``).
Calling that loss-free would need the rule "keep the earlier ``created_at``", which is a
judgement this script is not entitled to make on the operator's behalf.

WHY IT DOES NOT PRESCRIBE A SCOPED IMPORT.  The runbook's remedy for a diverged JSONL
MIRROR is a scoped ``bd import`` of the absent ids, because that is additive and cannot
lose either side.  Between two Dolt PEERS it is not the same operation: a row inserted
locally that the remote also holds becomes "added on both sides", and bd's fresh
``row_lock`` guarantees the two copies differ, so the import manufactures one new
conflict per imported id.  Measured in situ rather than inferred -- 21 of the 43 live
conflicts already have that exact shape (no base row, ``our_diff_type`` and
``their_diff_type`` both ``added``).  The additive route for two Dolt peers IS the merge.
"""

from __future__ import annotations

try:
    from py_interpreter_bootstrap import ensure_supported_python
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):
        """Stand in for the bootstrap when this file was copied without it."""
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
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bd_common import bd_env
from dolt_server import (
    DEFAULT_HOST,
    STORE_QUERY_TIMEOUT_SECONDS,
    DoltQueryTimeout,
    parse_endpoint,
    shared_server_endpoint,
)

# Columns bd rewrites as a side effect of writing a row, not as content a session
# authored. `row_lock` is a nonce, `content_hash` is derived from the content,
# `updated_at` is stamped by the write, `is_blocked` is derived from the edges.
BOOKKEEPING_COLUMNS = frozenset(
    {"row_lock", "content_hash", "updated_at", "is_blocked"}
)

# A status difference is only containment-ordered if it ADVANCES. Equal ranks (blocked
# vs in_progress) are deliberately not ordered: neither contains the other.
STATUS_RANK = {"open": 0, "in_progress": 1, "blocked": 1, "closed": 2}

BOOKKEEPING_ONLY = "bookkeeping-only"
REMOTE_SUPERSET = "remote-superset"
LOCAL_SUPERSET = "local-superset"
TWO_SIDED = "two-sided"

# Above this length a two-sided cell is described by size; at or below it, both values
# are quoted, because "19c vs 19c" for a pair of timestamps is unactionable.
SHORT_VALUE_CHARS = 40

# Revisions and table names reach SQL by interpolation, so they are constrained to what
# a Dolt branch/tag/table name may contain. The read-only guard below is the second half.
IDENT = re.compile(r"^[A-Za-z0-9_./-]+$")


class ReadOnlyViolation(RuntimeError):
    """A statement this module was asked to run is not a read."""


@dataclass(frozen=True)
class CellVerdict:
    """How one conflicting cell is ordered between the two sides."""

    column: str
    side: str  # REMOTE_SUPERSET, LOCAL_SUPERSET, or TWO_SIDED
    detail: str


@dataclass(frozen=True)
class RowVerdict:
    """The classification of one conflicting row."""

    row_id: str
    verdict: str
    conflict_columns: tuple[str, ...]
    content_columns: tuple[str, ...]
    both_added: bool
    cells: tuple[CellVerdict, ...] = field(default=())

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.row_id,
            "verdict": self.verdict,
            "conflict_columns": list(self.conflict_columns),
            "content_columns": list(self.content_columns),
            "both_added": self.both_added,
            "cells": [
                {"column": c.column, "side": c.side, "detail": c.detail}
                for c in self.cells
            ],
        }


# `select` then any whitespace; the keyword is case-insensitive in SQL. CTEs are not reads
# here: widening this guard past whitespace would widen the write guard too.
_READ_PREFIX = re.compile(r"select\s", re.IGNORECASE)


def assert_read_only(sql: str) -> str:
    """Refuse any statement that is not a single read.

    The guard is here rather than at the call site so that every query path goes
    through it: a mutating statement added later cannot reach the server by being
    written one line above the connection.
    """
    stripped = sql.strip().rstrip(";").strip()
    if not _READ_PREFIX.match(stripped):
        raise ReadOnlyViolation(f"not a read: {sql!r}")
    if ";" in stripped:
        raise ReadOnlyViolation(f"more than one statement: {sql!r}")
    return stripped


def preview_query(from_rev: str, to_rev: str, table: str) -> str:
    """The one statement this module runs, with its interpolations constrained."""
    for value in (from_rev, to_rev, table):
        if not IDENT.match(value):
            raise ValueError(f"not a usable Dolt identifier: {value!r}")
    return assert_read_only(
        "select * from dolt_preview_merge_conflicts"
        f"('{from_rev}', '{to_rev}', '{table}')"
    )


def summary_query(from_rev: str, to_rev: str) -> str:
    for value in (from_rev, to_rev):
        if not IDENT.match(value):
            raise ValueError(f"not a usable Dolt identifier: {value!r}")
    return assert_read_only(
        f"select * from dolt_preview_merge_conflicts_summary('{from_rev}', '{to_rev}')"
    )


def run_query(sql: str, *, database: str, host: str, port: int) -> list[dict]:
    """Run one read against the Dolt server and return its rows.

    TLS is off and the password empty because that is how the fleet's shared server is
    configured (loopback only); both are passed explicitly since `dolt sql` otherwise
    negotiates TLS and fails against it.
    """
    argv = [
        "dolt",
        "--host",
        host,
        "--port",
        str(port),
        "--user",
        "root",
        "--password",
        "",
        "--no-tls",
        "--use-db",
        database,
        "sql",
        "-r",
        "json",
        "-q",
        assert_read_only(sql),
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=STORE_QUERY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise DoltQueryTimeout(
            argv[-1], host, port, STORE_QUERY_TIMEOUT_SECONDS
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"dolt query failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    if not proc.stdout.strip():
        return []
    return json.loads(proc.stdout).get("rows", [])


def column_universe(rows: list[dict]) -> tuple[str, ...]:
    """Every table column the preview reports, across ALL rows.

    Deriving this from one row is a measured bug, not a theoretical one: ``dolt sql -r
    json`` OMITS a key whose value is NULL, and a row ADDED ON BOTH SIDES has no base
    row at all, so its ``base_*`` keys are absent entirely. Reading the column set from
    one row's ``base_*`` keys therefore yields the empty set for such a row -- which
    made a record with a real ``status`` and ``close_reason`` disagreement report zero
    conflicting cells and classify as loss-free. That is the one failure direction that
    looks like good news, so the universe is taken across every row and every prefix.
    """
    columns: set[str] = set()
    for row in rows:
        for key in row:
            for prefix in ("base_", "our_", "their_"):
                if key.startswith(prefix):
                    columns.add(key[len(prefix) :])
    # `our_diff_type`/`their_diff_type` are the preview's own annotation rather than
    # table columns, and they have no `base_` counterpart -- left in, every both-added
    # row would report a phantom two-sided `diff_type` conflict.
    return tuple(sorted(columns - {"diff_type"}))


def conflict_columns(row: dict, columns: tuple[str, ...] | None = None) -> list[str]:
    """Columns whose cell both sides changed, to different values.

    A Dolt conflict is per ROW but its cause is per CELL: where one side left the base
    value alone the merge takes the other side silently, so only cells both sides moved
    are the disagreement.
    """
    if columns is None:
        columns = column_universe([row])
    out = []
    for column in columns:
        base = row.get("base_" + column)
        ours = row.get("our_" + column)
        theirs = row.get("their_" + column)
        if ours != theirs and base not in (ours, theirs):
            out.append(column)
    return out


def _classify_status(ours: str, theirs: str) -> CellVerdict:
    rank_ours = STATUS_RANK.get(ours or "", -1)
    rank_theirs = STATUS_RANK.get(theirs or "", -1)
    if rank_ours < 0 or rank_theirs < 0:
        return CellVerdict("status", TWO_SIDED, f"unranked status {ours!r}/{theirs!r}")
    if rank_theirs > rank_ours:
        return CellVerdict("status", REMOTE_SUPERSET, f"{ours} -> {theirs} advances")
    if rank_ours > rank_theirs:
        return CellVerdict("status", LOCAL_SUPERSET, f"{theirs} -> {ours} advances")
    return CellVerdict("status", TWO_SIDED, f"{ours}/{theirs} do not order")


def _json_object(text: str) -> dict | None:
    """``text`` as a JSON object, or None when it is not one."""
    if not text.startswith("{"):
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _contains_object(outer: dict, inner: dict) -> bool:
    """Whether ``outer`` holds every key of ``inner`` with an equal value."""
    return all(key in outer and outer[key] == value for key, value in inner.items())


def _classify_json_objects(
    column: str, ours_s: str, theirs_s: str
) -> CellVerdict | None:
    """Order two JSON objects (bd's ``metadata``) by key containment, not by text.

    ``{}`` is not a substring of ``{"display_id": ...}``, yet the populated object holds
    every fact the empty one does (measured on a live store, 2026-10-12). Returns None
    when either side is not a JSON object or neither contains the other.
    """
    ours_obj, theirs_obj = _json_object(ours_s), _json_object(theirs_s)
    if ours_obj is None or theirs_obj is None:
        return None
    if _contains_object(theirs_obj, ours_obj):
        return CellVerdict(column, REMOTE_SUPERSET, "theirs holds every key of ours")
    if _contains_object(ours_obj, theirs_obj):
        return CellVerdict(column, LOCAL_SUPERSET, "ours holds every key of theirs")
    return None


def _classify_substrings(column: str, ours_s: str, theirs_s: str) -> CellVerdict | None:
    """Order two text cells when one is a substring of the other."""
    if ours_s and ours_s in theirs_s:
        return CellVerdict(column, REMOTE_SUPERSET, "ours is a substring of theirs")
    if theirs_s and theirs_s in ours_s:
        return CellVerdict(column, LOCAL_SUPERSET, "theirs is a substring of ours")
    return None


def classify_cell(column: str, ours: object, theirs: object) -> CellVerdict:
    """Which side, if either, contains the other for this cell."""
    if column == "status":
        return _classify_status(str(ours or ""), str(theirs or ""))
    ours_s = "" if ours is None else str(ours)
    theirs_s = "" if theirs is None else str(theirs)
    if not ours_s and theirs_s:
        return CellVerdict(column, REMOTE_SUPERSET, "ours empty, theirs populated")
    if not theirs_s and ours_s:
        return CellVerdict(column, LOCAL_SUPERSET, "theirs empty, ours populated")
    contained = _classify_json_objects(
        column, ours_s, theirs_s
    ) or _classify_substrings(column, ours_s, theirs_s)
    if contained is not None:
        return contained
    # Short values are printed rather than measured: a bare timestamp or enum reads as
    # "19c vs 19c", which tells the operator nothing they can act on.
    if len(ours_s) <= SHORT_VALUE_CHARS and len(theirs_s) <= SHORT_VALUE_CHARS:
        detail = f"neither contains the other (ours {ours_s!r}, theirs {theirs_s!r})"
    else:
        detail = (
            f"neither contains the other (ours {len(ours_s)}c, theirs {len(theirs_s)}c)"
        )
    return CellVerdict(column, TWO_SIDED, detail)


def classify_row(row: dict, universe: tuple[str, ...] | None = None) -> RowVerdict:
    """Classify one preview row. ``two-sided`` is the only verdict needing a rule."""
    columns = conflict_columns(row, universe)
    content = [c for c in columns if c not in BOOKKEEPING_COLUMNS]
    row_id = str(row.get("our_id") or row.get("their_id") or row.get("base_id") or "?")
    both_added = row.get("base_id") in (None, "")
    if not content:
        return RowVerdict(row_id, BOOKKEEPING_ONLY, tuple(columns), (), both_added, ())
    cells = tuple(
        classify_cell(c, row.get("our_" + c), row.get("their_" + c)) for c in content
    )
    sides = {cell.side for cell in cells}
    verdict = sides.pop() if len(sides) == 1 else TWO_SIDED
    return RowVerdict(
        row_id, verdict, tuple(columns), tuple(content), both_added, cells
    )


def classify_rows(rows: list[dict]) -> list[RowVerdict]:
    """Classify a whole preview result, sharing one column universe across its rows."""
    universe = column_universe(rows)
    return [classify_row(row, universe) for row in rows]


def counts(verdicts: list[RowVerdict]) -> dict[str, int]:
    out = {
        BOOKKEEPING_ONLY: 0,
        REMOTE_SUPERSET: 0,
        LOCAL_SUPERSET: 0,
        TWO_SIDED: 0,
    }
    for verdict in verdicts:
        out[verdict.verdict] += 1
    return out


def report(table: str, verdicts: list[RowVerdict], stream=sys.stdout) -> None:
    tally = counts(verdicts)
    stream.write(f"=== {table}: {len(verdicts)} conflicting row(s)\n")
    for name, count in tally.items():
        stream.write(f"    {count:4d}  {name}\n")
    loss_free = [v for v in verdicts if v.verdict != TWO_SIDED]
    if loss_free:
        stream.write(
            f"    {len(loss_free)} row(s) resolvable with no content loss; ids:\n"
        )
        for verdict in loss_free:
            stream.write(
                f"      {verdict.row_id:18s} {verdict.verdict:16s} "
                f"conflict cells: {', '.join(verdict.conflict_columns)}\n"
            )
    two_sided = [v for v in verdicts if v.verdict == TWO_SIDED]
    if two_sided:
        stream.write(
            f"    {len(two_sided)} row(s) NEED A STATED RULE -- neither side "
            "contains the other:\n"
        )
        for verdict in two_sided:
            stream.write(f"      {verdict.row_id}\n")
            for cell in verdict.cells:
                stream.write(f"        {cell.column}: {cell.side} -- {cell.detail}\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Classify a Dolt merge conflict set read-only. There is no --resolve and "
            "no --merge: this reports what a merge would conflict on, it never enters "
            "one."
        )
    )
    parser.add_argument("--from-rev", default="main", help="the local revision (ours)")
    parser.add_argument(
        "--to-rev",
        default="remotes/origin/main",
        help="the incoming revision (theirs)",
    )
    parser.add_argument(
        "--table",
        action="append",
        dest="tables",
        help="table to classify (repeatable; default: every conflicting table)",
    )
    parser.add_argument("--database", default="AIH", help="Dolt database name")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser.parse_args(argv)


def resolve_port(explicit: int | None) -> int:
    """Where the server listens, from bd's own state or from bd's own report.

    The port file is not written when the server is started by something other than
    ``bd dolt start`` -- on example-project-ec2 it is a systemd unit, so
    ``shared_server_endpoint()`` returns None against a perfectly healthy server.
    ``bd dolt status`` is asked second rather than first because it costs a bd
    invocation, and never instead of raising: a guessed port would report a live
    divergence as "no conflicts", which is the one wrong answer that looks like good news.
    """
    if explicit is not None:
        return explicit
    endpoint = shared_server_endpoint()
    if endpoint is not None:
        return endpoint.port
    proc = subprocess.run(
        ["bd", "dolt", "status"],
        capture_output=True,
        text=True,
        check=False,
        env=bd_env(),
    )
    endpoint = parse_endpoint(proc.stdout + proc.stderr)
    if endpoint is None:
        raise RuntimeError(
            "no shared-server port recorded and `bd dolt status` reported none; "
            "pass --port explicitly"
        )
    return endpoint.port


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    port = resolve_port(args.port)
    query = dict(database=args.database, host=args.host, port=port)

    tables = args.tables
    if not tables:
        rows = run_query(summary_query(args.from_rev, args.to_rev), **query)
        tables = [str(r["table"]) for r in rows if int(r["num_data_conflicts"]) > 0]
        schema_conflicted = [
            str(r["table"]) for r in rows if int(r["num_schema_conflicts"]) > 0
        ]
        if schema_conflicted:
            print(
                "SCHEMA conflicts, which this classifier does not model: "
                f"{', '.join(schema_conflicted)}",
                file=sys.stderr,
            )
    if not tables:
        print(f"=== no data conflicts between {args.from_rev} and {args.to_rev}")
        return 0

    payload: dict[str, object] = {}
    blocked = 0
    for table in tables:
        rows = run_query(preview_query(args.from_rev, args.to_rev, table), **query)
        verdicts = classify_rows(rows)
        blocked += counts(verdicts)[TWO_SIDED]
        if args.json:
            payload[table] = {
                "counts": counts(verdicts),
                "rows": [v.as_dict() for v in verdicts],
            }
        else:
            report(table, verdicts)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    if blocked:
        print(
            f"\n{blocked} row(s) cannot be resolved without a stated rule. A merge "
            "must not be entered until that rule exists: a conflicted merge left in "
            "the store blocks every concurrent session's git push.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
