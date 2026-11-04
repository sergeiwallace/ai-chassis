#!/usr/bin/env python3
# ruff: noqa: E402
"""Serialized pull-then-push publication for a Beads Dolt store -- D-8(b) prerequisite 4.

D-8(b) (docs/designs/beads-concurrent-sync-resilience.md) chose per-machine stores with
"serialized-not-single-owner publication (a lease/CAS guard any machine can acquire in turn)".
Prerequisite 4 is the protocol itself. Its shape was fixed by two measured outages rather than
by preference:

* A conflicting merge left in a shared store blocks every ``bd`` write AND, because the
  pre-push ``bd-invariants`` gate reads the store, every ``git push`` in the repository for every
  session on the host (ISSUE-REF, hours long, alerted nothing). So the protocol checks
  ``dolt_merge_status`` itself -- before it touches anything, and again after the pull -- and if
  the pull left a conflicted merge it ABORTS that merge (``call dolt_merge('--abort')`` picks no
  side; the incoming side is the remote and stays re-pullable), then refuses to push. Detection
  cannot be delegated to the pulling client: a plain CLI ``dolt merge`` writes the mid-merge
  state straight into the working set with no server session at all.
* ``bd dolt push`` against an unrelated lineage fails closed with ``no common ancestor``
  (ISSUE-REF). The protocol attests the lineage first when a remote-tracking ref exists, and
  refuses with the graft runbook's name instead of letting the push try and print bd's three
  remedies, two of which destroy data.

SERIALIZATION IS HOST-LOCAL AND PER DATABASE. All worktrees of one repo on this host share one
database on the shared ``dolt sql-server``, so the lock lives next to the server's own state
(``$HOME/.beads/shared-server/publish-<DB>.lock``), not in any checkout. Cross-machine
serialization is out of scope by ruling (the maintainer, 2026-09-27: "we can't coordinate all machines.
just get it done on this machine"); between machines bd's push is a manifest CAS, so a lost race
is a refused push, never an overwrite.

ORDER IS THE PROTOCOL: lock -> settle -> preflight -> pull -> post-pull check -> push ->
verify. A push is unreachable when any earlier step refuses; there is no flag that skips a
step.

A REFUSAL NAMES WHAT DIFFERS, NOT ONLY THAT SOMETHING DOES (ISSUE-REF). ``dolt_status``
answers at table granularity, and the dirty arm used to repeat it verbatim: "the working set
is dirty". But `issues` carries bd's own derived columns beside every authored field, so
"the issues table is modified" is read as "these are somebody's unpublished records" -- a
true premise and a false conclusion, and the conclusion a careful session must act on by
refusing to touch a shared store. Measured: a publish refused three times across a compact
boundary on ONE row differing in exactly one of 56 columns, ``is_blocked`` 0 -> 1, with
``updated_at`` unchanged. So the arm now resolves the dirt to its COLUMNS (``diagnose_dirty``
/ ``render_dirty``) and quotes them, which is the difference between a forensic dig and a
glance. Note that ``content_hash`` is NOT the discriminator and must not be used as one: it
is byte-identical across 36 of the 37 historical diffs in which the TITLE changed, and moves
in only 21 of 5780 modified-row diffs, so an unchanged ``content_hash`` is no evidence that
authored content is unchanged.

Resolving it also makes it CLEARABLE, which matters because this runs unattended: the
``settle`` step commits -- never discards -- a working set it proves twice to hold nothing
but ``DERIVED_RECOMPUTED_COLUMNS``, and verifies the commit against its own parent before
the push is reachable. ``--no-settle-derived`` turns that off and refuses instead.

Two transports carry the same protocol. ``BdServerTransport`` is the fleet's live path (``bd -C
<repo> dolt pull|push`` against the shared server, SQL through ``dolt --host``).
``DoltCliTransport`` runs the identical protocol against an embedded directory with a
``file://`` remote, which is what lets tests/test_dolt_publication_drill.py prove T-2.1 with
real Dolt and no mocks.

A PUBLISH ATTRIBUTES ITS OWN EFFECT OR SAYS IT CANNOT (ISSUE-REF). On a store several
sessions publish to, "did the push land" and "did MY push move the remote" are different
questions, and reporting only ``remote_head_after`` answers the first while being read as
answering the second: exit 0 beside ``main: X -> X`` is satisfied both by "the remote was
already current" and by "nothing was written". So the remote-tracking head is read THREE
times -- once pre-pull (``remote_head_at_start``, the reader's baseline), once post-pull
immediately pre-push (``remote_head_before``), once post-push (``remote_head_after``) -- and
the outcome carries ``advanced_by_this_publish`` (``true`` / ``false`` / ``unknown``, with
``advance_reason`` naming why). ``false`` is a legitimate, frequent state (a no-op publish),
never a failure, and the pair is NOT a freshness verdict: hash equality in a live store has a
lifetime of minutes. An unreadable pre-push read renders ``UNRESOLVED(<reason>)`` -- never an
empty string, never ``None`` printed where a head belongs.

Exit codes: 0 published; 2 not verified (a store state could not be read, INCLUDING the
remote ref after the push -- a landing that cannot be read is not a publish, D-14); 3 refused
by preflight; 4 pull failed; 5 pull left a conflict, which was aborted; 6 push failed;
7 not converged (the push returned 0 but the remote does not hold the pushed head, D-14).
The attribution fields change no exit code: the exits are D-14's, the proof lives in the
fields.
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
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import NamedTuple, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bd_common import bd_env, exclusive_file_lock  # noqa: E402
from beads_publication_protocol import PUBLISH_TIMEOUT_SECONDS, load_config
from bd_store_backup import declared_database, resolve_location  # noqa: E402,F401
from bd_dolt_conflict_classify import ReadOnlyViolation, assert_read_only  # noqa: E402
import bd_dolt_premerge_align as align  # noqa: E402
from dolt_server import (  # noqa: E402
    DEFAULT_HOST,
    STORE_QUERY_TIMEOUT_SECONDS,
    DoltQueryTimeout,
    Endpoint,
    shared_server_endpoint,
)

EXIT_OK = 0
EXIT_NOT_VERIFIED = 2
EXIT_REFUSED = 3
EXIT_PULL_FAILED = 4
EXIT_CONFLICT_ABORTED = 5
EXIT_PUSH_FAILED = 6
# A NEW code rather than a reuse of 6 (D-14): "the push itself failed" and "the push said
# yes and the remote says no" must stay distinguishable in every status file and ledger.
EXIT_NOT_CONVERGED = 7

# ISSUE-REF: how long to wait before re-reading a state a concurrent writer changes. Short
# enough to add nothing noticeable to a publish, long enough to fall outside one bridge-export
# window. Injectable so tests do not sleep.
RESAMPLE_DELAY = 2.0

# ISSUE-REF: how many pull-then-push attempts one publish may make. A non-fast-forward
# rejection means the REAL remote moved between this run's pull and its push -- on this fleet a
# peer publishes every minute or two, so the window is lost often and the next tick resolves it
# anyway. Measured 2026-10-08 on the AIH store: a timer run failed, a hand sweep failed with
# `! [rejected] main -> main (non-fast-forward)` while the store was clean and a strict
# descendant of the tracking ref, and a third attempt ~40 s later landed. So the transient was
# real, the retry is what the operator was doing by hand, and reporting it as exit 6 trains the
# reader to ignore the unit (the ISSUE-REF/ISSUE-REF health-signal shape).
#
# BOUNDED, and the bound is the point: a rejection that survives three pulls is not a race, so
# the last rejection is reported verbatim at exit 6 rather than retried forever under the
# per-database lock every other publisher is waiting on.
PUSH_RETRY_ATTEMPTS = 3

# Long enough for a peer's in-flight push to finish landing (so the next pull actually sees
# it), short enough that three attempts add at most ~4 s to a publish. Injectable so tests do
# not sleep.
PUSH_RETRY_BACKOFF = 2.0

# What a non-fast-forward rejection says, as substrings of the push's own stderr. Matched
# rather than inferred from the exit code, because rc=1 covers every push failure and only
# THIS one is resolvable by pulling again -- a rejected hook, an auth failure or bd's
# pre-push fsck timeout (`publisher.fsck_timeout_seconds`) must stay terminal on the first
# attempt.
NON_FAST_FORWARD_MARKERS = ("non-fast-forward", "fetch first")

# bd's pre-push `fsck` budget is `publisher.fsck_timeout_seconds` in this file, read on every
# bd call (ISSUE-REF, ISSUE-REF). A publish that runs out of it returns code 6 with bd's own
# text "pre-push integrity check timed out: fsck did not complete within the configured
# timeout; the push was aborted without checking integrity (the store is not necessarily
# corrupt)". Read that precisely -- the check DID NOT COMPLETE, it did not fail. The store
# grows as the queue bridge exports into it and swap pressure slows the check, so this is a
# budget, not a corruption signal. `dolt gc` is the way to shrink it, and it must NOT be run
# by the publisher: on a shared server with live connections and a remote mid-update it is
# unsafe, so it stays a scheduled, human-gated follow-up.
PUBLICATION_CONFIG = (
    Path(__file__).resolve().parents[1] / "config/beads_publication.toml"
)

GRAFT_RUNBOOK = (
    "docs/procedures/beads-dolt-server-migration.md "
    "section 'Grafting a store whose lineage is unrelated'"
)
CLASSIFIER = "scripts/bd_dolt_conflict_classify.py"

ADVANCED_YES = "true"
ADVANCED_NO = "false"
ADVANCED_UNKNOWN = "unknown"
MOVED_BETWEEN_READS = "remote moved between reads"

# Columns bd RECOMPUTES from other rows rather than ones a session authors. A dirty working
# set confined to these is bd's own bookkeeping left uncommitted, not anybody's unpublished
# work -- and because bd recomputes it anyway, committing it is not a one-way door.
#
# `is_blocked` is here on a measurement, not on a reading of bd's source (bd ships as a Go
# binary; no source checkout exists on this host). Two independent instruments agree:
#   * `blocked_issues` is a VIEW in every store, and in example-client-host-workspace it agreed with
#     `issues.is_blocked` exactly -- 61 rows each way, zero rows set-but-not-in-view and zero
#     in-view-but-clear (2026-10-05). A flag a view reproduces row for row is a cache of that
#     view, i.e. derived.
#   * Across the KG store's whole history (5782 modified-row diffs) `is_blocked` moved in 289,
#     and 282 of those left `updated_at` UNCHANGED -- the recomputation is decoupled from
#     session writes, which is what the measured incident looked like close up: ONE row
#     differing in exactly one of 56 columns, `is_blocked` 0 -> 1, `updated_at` identical.
#
# DO NOT turn that into an `updated_at` test, and do not read it as "a session write always
# moves `updated_at`". That stronger claim is FALSE and was measured false in the same pass:
# `title` moved in 37 diffs and moved `updated_at` in all 37, but 24 `status` diffs and 6
# `notes` diffs left `updated_at` unchanged. The guard is a column ALLOWLIST precisely because
# it does not need that invariant -- in all 30 of those cases `status`/`notes` are themselves
# in the differing set, so they refuse on their own name either way.
#
# THE SET IS DELIBERATELY MINIMAL AND WIDENING IT NEEDS A MEASUREMENT, NOT A GUESS. The
# refusal below NAMES every column that moved, so an unlisted derived column refuses loudly
# with its own name in the text rather than being published around silently -- which is the
# failure mode a generous allowlist produces and the narrow one cannot. `row_lock` is a
# visible candidate (it moved on 11 of 11 rows across HEAD~50..HEAD) and is deliberately NOT
# here: nothing has measured it as recomputed rather than written.
DERIVED_RECOMPUTED_COLUMNS = frozenset({"is_blocked"})

# How many affected primary keys the refusal quotes. The point is identification, not a dump:
# the keys let a reader go straight to the rows, and a dirty set of hundreds is a different
# problem from the one this text is diagnosing.
MAX_REPORTED_ROW_IDS = 5

# Table and column names reach SQL by interpolation (Dolt's `dolt_diff()` takes the table as a
# string literal and a column cannot be a bind parameter), so every identifier is matched
# against this before it is interpolated and skipped otherwise. The names come from
# `information_schema`, i.e. from the server rather than from a caller, which makes this a
# belt-and-braces check -- and a cheap one next to building a query out of unvalidated text.
_SQL_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

# The working set, as `dolt_diff()` names the two sides of "what is uncommitted right now".
HEAD_REF = "HEAD"
WORKING_REF = "WORKING"


class TransportError(RuntimeError):
    """A store-state read failed; the state is UNKNOWN, not clean."""


class TransportTimeout(TransportError, DoltQueryTimeout):
    """A store-state read got no answer within its bound; UNKNOWN, like any failed read."""


class Transport(Protocol):
    name: str
    lock_path: Path
    remote_branch: str

    def sql(self, query: str) -> list[dict]: ...

    def pull(self) -> subprocess.CompletedProcess: ...

    def push(self) -> subprocess.CompletedProcess: ...


@dataclass(frozen=True)
class Outcome:
    code: int
    step: str
    detail: str
    head_before: str | None = None
    head_after: str | None = None
    # The head as it stood IMMEDIATELY BEFORE the push: the one the push actually carried,
    # and therefore the one `converged` compares the remote against. `head_after` can differ
    # from it without anything being wrong, when a concurrent writer commits locally after
    # the push lands.
    head_pushed: str | None = None
    # THREE readings of the REMOTE head, not only the post-push one: with a post-push reading
    # alone, "the remote did not move" cannot be told from "the remote was already there".
    #   remote_head_at_start -- pre-pull, recorded on EVERY outcome (a refusal too) so the
    #     reader always has a baseline; None when unreadable, which never fails a publish
    #     (that would refuse every first publish to a fresh remote).
    #   remote_head_before   -- post-pull, immediately pre-push: the ref the push was made
    #     against. Rendered: an unread value is the string ``UNRESOLVED(<reason>)``, never
    #     None, because it is printed where a head belongs (ISSUE-REF).
    #   remote_head_after    -- post-push: the landing `converged` is derived from.
    remote_head_at_start: str | None = None
    remote_head_before: str | None = None
    remote_head_after: str | None = None
    # True only when the fast-forward re-read EQUALLED the pushed head; False when it
    # disagreed; None when the question was never reached or could not be read. The sweep
    # and every ledger read this rather than re-deriving it from the detail string.
    converged: bool | None = None
    attestation: dict | None = None
    # Did THIS push move the remote ref? `converged` says the remote holds the pushed head;
    # it does not say who put it there. `unknown` is a first-class answer (ISSUE-REF).
    advanced_by_this_publish: str = ADVANCED_UNKNOWN
    advance_reason: str | None = None
    # True when this run COMMITTED a working set it proved held only bd's derived columns.
    # A field rather than a detail-string substring, for the same reason as `converged`: the
    # sweep and every ledger must be able to count these without parsing prose.
    settled_derived_dirt: bool = False
    # ISSUE-REF: how many pull-then-push attempts this publish made. 1 on every outcome that
    # never reached the push and on every clean first-try publish, so `attempts > 1` is
    # exactly "this run lost the push race at least once" -- the rate the fleet needs to see.
    # A field rather than a detail substring, same reason as the two above.
    attempts: int = 1

    @property
    def published(self) -> bool:
        return self.code == EXIT_OK


def _rows(proc: subprocess.CompletedProcess, query: str) -> list[dict]:
    if proc.returncode != 0:
        raise TransportError(
            f"query failed (rc={proc.returncode}): {proc.stderr.strip()} :: {query}"
        )
    if not proc.stdout.strip():
        return []
    try:
        return json.loads(proc.stdout).get("rows", [])
    except ValueError as exc:
        raise TransportError(f"unparseable query result for {query}: {exc}") from exc


class DoltCliTransport:
    """The protocol against an embedded database directory and a named Dolt remote."""

    def __init__(
        self, db_dir: Path, *, remote: str = "origin", branch: str = "main", runner=None
    ):
        self.db_dir = Path(db_dir)
        self.remote = remote
        self.branch = branch
        self.name = f"dolt-cli:{self.db_dir}"
        self.lock_path = self.db_dir / ".publish.lock"
        self.remote_branch = f"remotes/{remote}/{branch}"
        self._run = subprocess.run if runner is None else runner

    def _dolt(self, *args: str) -> subprocess.CompletedProcess:
        return self._run(
            ["dolt", *args],
            cwd=str(self.db_dir),
            capture_output=True,
            text=True,
            check=False,
        )

    def sql(self, query: str) -> list[dict]:
        return _rows(self._dolt("sql", "-r", "json", "-q", query), query)

    def pull(self) -> subprocess.CompletedProcess:
        return self._dolt("pull", self.remote, self.branch)

    def push(self) -> subprocess.CompletedProcess:
        return self._dolt("push", self.remote, self.branch)


class BdServerTransport:
    """The fleet's live path: bd's git+ssh transport, SQL through the shared server."""

    def __init__(
        self,
        repo: Path,
        database: str,
        *,
        endpoint: Endpoint,
        home: Path | None = None,
        runner=None,
    ):
        self.repo = Path(repo)
        self.database = database
        self.endpoint = endpoint
        self.name = f"bd:{database}@{endpoint.host}:{endpoint.port}"
        self.home = home
        self.lock_path = align.publish_lock_path(database, home)
        self.remote_branch = "remotes/origin/main"
        self._run = subprocess.run if runner is None else runner

    def sql(self, query: str) -> list[dict]:
        argv = [
            "dolt",
            "--host",
            self.endpoint.host,
            "--port",
            str(self.endpoint.port),
            "--user",
            "root",
            "--password",
            "",
            "--no-tls",
            "--use-db",
            self.database,
            "sql",
            "-r",
            "json",
            "-q",
            query,
        ]
        # Reads only: an interrupted write costs a record (see START_TIMEOUT_SECONDS).
        try:
            assert_read_only(query)
            timeout = STORE_QUERY_TIMEOUT_SECONDS
        except ReadOnlyViolation:
            timeout = None
        try:
            proc = self._run(
                argv, capture_output=True, text=True, check=False, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise TransportTimeout(
                query, self.endpoint.host, self.endpoint.port, timeout
            ) from exc
        return _rows(proc, query)

    def _bd(self, *args: str) -> subprocess.CompletedProcess:
        env = bd_env()
        # Read on every call so a running publisher observes config edits. Only bd's
        # environment gets this override; SQL and the parent environment stay untouched.
        try:
            publisher = load_config(PUBLICATION_CONFIG).get("publisher", {})
            seconds = publisher.get("cli_transfer_timeout_seconds", 840)
        except (OSError, ValueError, AttributeError) as exc:
            raise TransportError(
                f"cannot read publisher config {PUBLICATION_CONFIG}"
            ) from exc
        if type(seconds) is not int or not 0 < seconds < PUBLISH_TIMEOUT_SECONDS:
            raise TransportError(
                f"publisher.cli_transfer_timeout_seconds={seconds} must be a positive "
                f"integer below the {PUBLISH_TIMEOUT_SECONDS}s outer publish budget"
            )
        env["BEADS_CLI_TRANSFER_TIMEOUT"] = f"{seconds}s"
        # ISSUE-REF, ISSUE-REF: bd runs the pre-push fsck inside the transfer deadline, so
        # the fsck budget can never usefully exceed it; absent a setting it gets all of it.
        fsck = publisher.get("fsck_timeout_seconds", seconds)
        if type(fsck) is not int or not 0 < fsck <= seconds:
            raise TransportError(
                f"publisher.fsck_timeout_seconds={fsck} must be a positive integer no "
                f"larger than the {seconds}s cli_transfer_timeout_seconds it runs inside"
            )
        # `setdefault` is the whole of "never override an explicit value".
        env.setdefault("BEADS_FSCK_TIMEOUT", str(fsck))
        return self._run(
            ["bd", "-C", str(self.repo), "dolt", *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def pull(self) -> subprocess.CompletedProcess:
        return self._bd("pull")

    def push(self) -> subprocess.CompletedProcess:
        return self._bd("push", "--no-adopt")


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes"}


def _count(rows: list[dict]) -> int:
    if not rows:
        return 0
    value = next(iter(rows[0].values()))
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def is_merging(transport: Transport) -> bool:
    rows = transport.sql("select is_merging from dolt_merge_status")
    return bool(rows) and _truthy(rows[0].get("is_merging"))


def head(transport: Transport) -> str | None:
    rows = transport.sql("select hash from dolt_branches where name='main'")
    return str(rows[0]["hash"]) if rows else None


def remote_head(transport: Transport) -> str | None:
    rows = transport.sql(
        f"select hash from dolt_remote_branches where name='{transport.remote_branch}'"
    )
    return str(rows[0]["hash"]) if rows else None


def _confirmed(probe, *, resample_delay: float) -> bool:
    """True only when a MUTABLE state reads positive TWICE (ISSUE-REF).

    The three states below blink. Measured on this host: a publish refused with exit 3
    "dirty working set" and seconds later ``select * from dolt_status`` returned ZERO rows,
    because the read had landed inside one of the eight queue-bridge export windows per 30
    seconds. A one-shot read of a blinking value is a coin toss, and two of the arms print
    remedies that destroy work.

    The re-sample must not become a swallow: a TransportError from EITHER read propagates,
    because preflight's invariant is that every check is a READ and an unreadable state is
    NOT VERIFIED -- never "transient, carry on".
    """
    if not probe():
        return False
    if resample_delay:
        time.sleep(resample_delay)
    return bool(probe())


@dataclass(frozen=True)
class DirtyTable:
    """One row of ``dolt_status``, resolved down to WHICH COLUMNS differ.

    ``dolt_status`` answers "something in this table differs". That is the whole of what the
    refusal used to say, and it is not enough to act on: `issues` carries bd's derived
    columns alongside every authored field, so "the issues table is modified" is read as
    "these are somebody's records" -- a true premise and a false conclusion (ISSUE-REF cost a
    multi-step forensic dig and a correct refusal to touch the store). ``columns`` is the
    discriminator, and it is reported whether or not anything acts on it.
    """

    name: str
    status: str
    staged: bool = False
    added: int = 0
    removed: int = 0
    modified: int = 0
    # Columns that differ on the MODIFIED rows only. An added row has no HEAD side, so every
    # column of it "differs" -- folding those in would report the whole schema as changed.
    columns: tuple[str, ...] = ()
    row_ids: tuple[str, ...] = ()
    # Why the column-level read could not be made. NOT the same as "no columns differ": an
    # unreadable table is UNKNOWN, and every judgement below treats it as not proved safe.
    unreadable: str | None = None


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _quote(text: str) -> str:
    """A SQL string literal. ``call dolt_commit(...)`` takes no bind parameters.

    Every message this quotes is built from validated identifiers and fixed prose, so there
    is nothing hostile to escape -- which is exactly why the escaping belongs here rather
    than in the caller: the next caller is the one that will not be so lucky.
    """
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def _identifiers(rows: list[dict], key: str) -> list[str]:
    """Server-supplied names, filtered to ones safe to interpolate (see ``_SQL_IDENTIFIER``)."""
    out = []
    for row in rows:
        name = str(row.get(key, ""))
        if _SQL_IDENTIFIER.match(name):
            out.append(name)
    return out


def _column_names(
    transport: Transport, table: str, *, pk_only: bool = False
) -> list[str]:
    """``table``'s column names in ordinal order, or just its primary key.

    Every projection is ALIASED. Measured through the live transport 2026-10-05: a
    ``select column_name from information_schema.columns`` comes back under a key that is not
    ``column_name``, so reading the unaliased name raises ``KeyError`` and a diagnosis that
    reached for it would report "no columns" on a table whose columns were simply unread.
    """
    where = " and column_key='PRI'" if pk_only else ""
    rows = transport.sql(
        "select column_name as c from information_schema.columns "
        f"where table_name='{table}' and table_schema=database(){where} "
        "order by ordinal_position"
    )
    return _identifiers(rows, "c")


def _diff_type_counts(transport: Transport, table: str, frm: str, to: str) -> dict:
    rows = transport.sql(
        "select "
        "sum(case when diff_type='added' then 1 else 0 end) as added, "
        "sum(case when diff_type='removed' then 1 else 0 end) as removed, "
        "sum(case when diff_type='modified' then 1 else 0 end) as modified "
        f"from dolt_diff('{frm}','{to}','{table}')"
    )
    return rows[0] if rows else {}


def _moved_columns(
    transport: Transport, table: str, columns: list[str], frm: str, to: str
) -> tuple[str, ...]:
    """Which of ``columns`` actually differ on the modified rows.

    ``<=>`` (null-safe equality), never ``!=``: a plain ``!=`` evaluates to NULL when either
    side is NULL, so the ``not (...)`` wrapper reads a NULL-to-NULL pair as a DIFFERENCE and
    every nullable column in the table reports itself as changed. Of the 56 columns in
    `issues`, 40 are nullable, so the wrong operator here does not shade the answer -- it
    buries the one real column among forty false ones and the diagnosis becomes noise.
    """
    if not columns:
        return ()
    parts = ", ".join(
        f"sum(case when not (`from_{c}` <=> `to_{c}`) then 1 else 0 end) as `{c}`"
        for c in columns
    )
    rows = transport.sql(
        f"select {parts} from dolt_diff('{frm}','{to}','{table}') "
        "where diff_type='modified'"
    )
    if not rows:
        return ()
    return tuple(c for c in columns if _int(rows[0].get(c)) > 0)


def _affected_row_ids(
    transport: Transport, table: str, frm: str, to: str
) -> tuple[str, ...]:
    """The primary keys of the differing rows -- best effort, never a gate.

    Skipped for a composite or absent primary key rather than guessed at: the keys are an
    aid to a reader, and no decision in this module turns on them.
    """
    pk = _column_names(transport, table, pk_only=True)
    if len(pk) != 1:
        return ()
    rows = transport.sql(
        f"select `to_{pk[0]}` as rid from dolt_diff('{frm}','{to}','{table}') "
        f"limit {MAX_REPORTED_ROW_IDS}"
    )
    return tuple(str(r.get("rid")) for r in rows if r.get("rid") is not None)


class _Listed(NamedTuple):
    """A table named as changed, before anything has been resolved about HOW it changed."""

    name: str
    status: str
    staged: bool


def _diagnose_table(
    transport: Transport, listed: _Listed, frm: str, to: str
) -> DirtyTable:
    """One table's dirt, resolved to columns. An unreadable table says so and is not 'clean'."""
    name, status, staged = listed
    if not _SQL_IDENTIFIER.match(name):
        return DirtyTable(
            name=name,
            status=status,
            staged=staged,
            unreadable="the table name is not a plain SQL identifier, so it was not diffed",
        )
    try:
        counts = _diff_type_counts(transport, name, frm, to)
        columns = _moved_columns(
            transport, name, _column_names(transport, name), frm, to
        )
        row_ids = _affected_row_ids(transport, name, frm, to)
    except TransportError as exc:
        return DirtyTable(name=name, status=status, staged=staged, unreadable=str(exc))
    return DirtyTable(
        name=name,
        status=status,
        staged=staged,
        added=_int(counts.get("added")),
        removed=_int(counts.get("removed")),
        modified=_int(counts.get("modified")),
        columns=columns,
        row_ids=row_ids,
    )


def diagnose_dirty(
    transport: Transport, *, frm: str = HEAD_REF, to: str = WORKING_REF
) -> tuple[DirtyTable, ...]:
    """Every dirty table between ``frm`` and ``to``, resolved to its differing columns.

    Read-only. Defaults to HEAD-vs-working-set, which is the state the dirty arm refuses on;
    the same function diffs a COMMIT against its parent, which is how the settle step below
    verifies what it just committed.
    """
    if to == WORKING_REF:
        rows = transport.sql(
            "select table_name as t, staged as s, status as st from dolt_status"
        )
        listed = [
            _Listed(str(r.get("t")), str(r.get("st")), _truthy(r.get("s")))
            for r in rows
        ]
    else:
        rows = transport.sql(
            f"select to_table_name as t, diff_type as st from dolt_diff_summary('{frm}','{to}')"
        )
        listed = [_Listed(str(r.get("t")), str(r.get("st")), True) for r in rows]
    return tuple(
        _diagnose_table(transport, item, frm, to)
        for item in listed
        if item.name and item.name != "None"
    )


def render_dirty(tables: tuple[DirtyTable, ...]) -> str:
    """The dirt as a reader can act on it: table, counts, COLUMNS, and example keys."""
    if not tables:
        return "no table was listed as dirty"
    parts = []
    for t in tables:
        if t.unreadable is not None:
            parts.append(f"{t.name} ({t.status}): NOT READ -- {t.unreadable}")
            continue
        shape = f"{t.modified} modified"
        if t.added or t.removed:
            shape += f", {t.added} added, {t.removed} removed"
        cols = ", ".join(t.columns) if t.columns else "none on the modified rows"
        piece = f"{t.name} ({t.status}): {shape}; differing column(s): {cols}"
        if t.row_ids:
            piece += f"; key(s): {', '.join(t.row_ids)}"
        parts.append(piece)
    return " | ".join(parts)


def derived_only(tables: tuple[DirtyTable, ...]) -> bool:
    """True ONLY when the whole dirt is row modifications confined to derived columns.

    Fails CLOSED on every uncertainty -- nothing listed, a table that could not be read, an
    added or removed row, a modified row whose differing columns could not be resolved, or a
    single column outside ``DERIVED_RECOMPUTED_COLUMNS``. A false positive here publishes
    around somebody's authored edit, and the cost of a false negative is the refusal that
    already happens today, now with the columns named in it.
    """
    if not tables:
        return False
    return all(
        t.unreadable is None
        and t.added == 0
        and t.removed == 0
        and t.modified > 0
        and bool(t.columns)
        and set(t.columns) <= DERIVED_RECOMPUTED_COLUMNS
        for t in tables
    )


def _signature(tables: tuple[DirtyTable, ...]) -> tuple:
    """The identity of a dirt reading, for comparing two samples of a blinking state."""
    return tuple(
        (t.name, t.status, t.added, t.removed, t.modified, t.columns) for t in tables
    )


def _describe_dirt(transport: Transport) -> str:
    """``render_dirty`` for a refusal message, which must survive its own diagnosis failing.

    The refusal is the conservative outcome and it must not be DOWNGRADED by the attempt to
    explain it: letting a TransportError out of here would turn exit 3 (refused, nothing
    pushed) into exit 2 (not verified) purely because the explanation could not be built,
    changing the code the sweep and /save-state branch on. So the failure is reported INSIDE
    the refusal text and the refusal stands.
    """
    try:
        return render_dirty(diagnose_dirty(transport))
    except TransportError as exc:
        return (
            f"the per-column diagnosis could not be read ({exc}); inspect `dolt_status`"
        )


@dataclass(frozen=True)
class Settlement:
    """What the settle step did. ``refusal`` set means the protocol must STOP before pushing."""

    committed: bool = False
    note: str | None = None
    refusal: str | None = None


def _derived_commit_message(tables: tuple[DirtyTable, ...]) -> str:
    cols = sorted({c for t in tables for c in t.columns})
    names = ", ".join(t.name for t in tables)
    return (
        f"bd derived-column recomputation ({names}: {', '.join(cols)}) committed by "
        "bd_dolt_publish.py so publication is not blocked -- ISSUE-REF"
    )


def settle_derived_dirt(
    transport: Transport,
    *,
    enabled: bool = True,
    resample_delay: float = RESAMPLE_DELAY,
) -> Settlement:
    """Commit a dirty working set PROVED to hold only bd's own derived recomputation.

    This is the one step in the protocol that writes before the push, and it exists because
    the refusal it removes is unattended. ``publish()`` runs from a systemd timer every ~14
    minutes across every store on the host (``ai-harness-beads-publish.timer``, enabled and
    active, measured 2026-10-05), so a derived-only dirt does not block one session until
    somebody looks -- it blocks EVERY store's publication indefinitely, in a log nobody
    reads. Diagnosing it without clearing it would leave that standing.

    THE MUTATION IS GATED ON THE PROOF, AND THE PUSH IS GATED ON THE MUTATION:

    * ``derived_only`` must hold TWICE, with an identical signature across the two samples.
      One read of this state is a coin toss (ISSUE-REF): the queue bridge dirties the store
      in eight windows per 30 seconds, so a single sample can catch a half-written export
      mid-flight and call it settled.
    * The commit is NOT DESTRUCTIVE in either direction. It preserves whatever it sweeps in
      -- the existing refusal already asks a human to do exactly this -- and because bd
      recomputes the flag anyway it is not a one-way door. Nothing here discards, resets or
      checks out anything, ever.
    * The window between the proof and the commit is not closed by the per-database lock,
      which excludes other PUBLISHES and not a peer's own ``bd`` write. So the commit is
      VERIFIED AFTER THE FACT against its own parent, and a commit that turns out to carry
      more than derived columns REFUSES: the work is safe in a local commit and is NOT
      published. That is the ordering that matters -- a commit is recoverable, a push to a
      store other machines pull is not.
    """
    if not enabled:
        return Settlement()
    proved = _prove_derived_only(transport, resample_delay)
    if proved is None:
        return Settlement()
    try:
        transport.sql(
            f"call dolt_commit('-A', '-m', {_quote(_derived_commit_message(proved))})"
        )
    except TransportError as exc:
        # ATTRIBUTED rather than propagated. Letting this out reaches the caller's
        # `store state unreadable` arm, which is the wrong sentence for a statement that was
        # read fine and REFUSED -- and it is the arm an operator would then go and debug.
        # Nothing was committed and nothing is pushed either way.
        return Settlement(
            refusal=(
                "the working set read as a derived-column recomputation twice, but the "
                f"commit that would have settled it FAILED ({exc}); nothing was committed, "
                "nothing was discarded and nothing was pushed. "
                f"WHAT DIFFERS: {render_dirty(proved)}"
            )
        )
    return _verify_settlement(transport)


def _prove_derived_only(
    transport: Transport, resample_delay: float
) -> tuple[DirtyTable, ...] | None:
    """Two AGREEING samples that both read derived-only, or None.

    Both the verdict and the signature have to match. One read of this state is a coin toss
    (ISSUE-REF): the queue bridge dirties the store in eight windows per 30 seconds, so a
    single sample can catch a half-written export mid-flight and call it settled.
    """
    try:
        first = diagnose_dirty(transport)
        if not derived_only(first):
            return None
        if resample_delay:
            time.sleep(resample_delay)
        second = diagnose_dirty(transport)
    except TransportError:
        # DECLINES rather than propagating, and that is not the "transient, carry on" this
        # module refuses everywhere else. This step is OPPORTUNISTIC, not a gate: an
        # unreadable diagnosis simply means the dirt is not PROVED derived, which is the
        # answer that leaves the mutation unreachable. Raising instead would hand the
        # verdict to the caller's except arm and turn preflight's refusal (exit 3, nothing
        # pushed) into not-verified (exit 2) for a store whose `dolt_status` count preflight
        # can still read perfectly well -- changing the code the sweep branches on because
        # an EXPLANATION could not be built. The gate below is untouched either way.
        return None
    if not derived_only(second) or _signature(first) != _signature(second):
        return None
    return second


def _verify_settlement(transport: Transport) -> Settlement:
    """What the settle commit ACTUALLY carried, read back off its own parent.

    The window between the proof and the commit is not closed by the per-database lock,
    which excludes other PUBLISHES and not a peer's own ``bd`` write. Losing that race is
    allowed; PUBLISHING the result is not. Every arm here is past the mutation, so every
    arm fails CLOSED -- the work sits in a local commit, which loses nothing.
    """
    try:
        landed = diagnose_dirty(transport, frm="HEAD~1", to=HEAD_REF)
    except TransportError as exc:
        # Past the mutation, so this one FAILS CLOSED. The commit lost nothing, but what it
        # carries is now unknown, and an unknown commit must not be published.
        return Settlement(
            committed=True,
            refusal=(
                "a derived-only working set was COMMITTED (nothing was discarded), but what "
                f"that commit carries could not be read back ({exc}), so it was NOT pushed. "
                "The work is safe in local commit HEAD; inspect it before re-running"
            ),
        )
    if not derived_only(landed):
        return Settlement(
            committed=True,
            refusal=(
                "the working set read as a derived-column recomputation twice, so it was "
                "COMMITTED (nothing was discarded and nothing was pushed) -- but the commit "
                "it produced carries more than derived columns, so a concurrent writer must "
                "have written into the store between the proof and the commit. The work is "
                "safe in local commit HEAD and is NOT published; classify it before "
                f"re-running. WHAT LANDED: {render_dirty(landed)}"
            ),
        )
    return Settlement(
        committed=True,
        note=f"committed a derived-only dirty working set: {render_dirty(landed)}",
    )


def preflight(
    transport: Transport, *, resample_delay: float = RESAMPLE_DELAY
) -> str | None:
    """Return the refusal reason, or None when the store may enter the protocol.

    Every check here is a READ. An unreadable state raises TransportError, which the caller
    reports as NOT VERIFIED -- never as clean. The first three arms read state a concurrent
    writer changes under them, so each is confirmed by a second read (see ``_confirmed``).
    """
    if _confirmed(lambda: is_merging(transport), resample_delay=resample_delay):
        return (
            "the store is mid-merge (dolt_merge_status.is_merging=1, confirmed by a second "
            "read); every bd write and every git push on this host is blocked until it is "
            "resolved. This is a SHARED store: an abort picks no side but it ends whoever's "
            "merge is in flight, which may be another session mid-protocol -- identify the "
            "owner of the merge before aborting anything, see ISSUE-REF"
        )
    if _confirmed(
        lambda: _count(transport.sql("select count(*) as n from dolt_status")) > 0,
        resample_delay=resample_delay,
    ):
        return (
            "the working set is dirty (dolt_status is non-empty, confirmed by a second "
            "read); the usual cause on this host is a concurrent queue-bridge export "
            "committing into the store, so RE-RUN the publish first -- it clears on its "
            "own. If it persists, have the writing session commit its work; do not discard "
            "a working set you did not author. "
            f"WHAT DIFFERS: {_describe_dirt(transport)}"
        )
    if _confirmed(
        lambda: _count(transport.sql("select count(*) as n from dolt_conflicts")) > 0,
        resample_delay=resample_delay,
    ):
        return (
            "the store carries unresolved conflicts (dolt_conflicts is non-empty, confirmed "
            f"by a second read); classify before resolving: {CLASSIFIER}"
        )
    if remote_head(transport) is not None:
        try:
            transport.sql(
                f"select dolt_merge_base('main','{transport.remote_branch}') as base"
            )
        except TransportError as exc:
            if "no common ancestor" not in str(exc):
                raise
            return (
                f"local main and {transport.remote_branch} share no ancestor; publishing "
                f"would need a graft, not a push -- {GRAFT_RUNBOOK}"
            )
    return None


def _normalize_ref(ref: str) -> str:
    """A branch ref reduced to a comparable form: ``refs/remotes/origin/main`` -> ``origin/main``.

    Measured spellings (real Dolt, 2026-10-04): a conflicting ``dolt pull origin main`` leaves
    ``dolt_merge_status.source = 'refs/remotes/origin/main'`` while this module spells the same
    branch ``remotes/origin/main``. Both reduce here, so the comparison is on the whole
    remaining path rather than on a trailing component -- a peer's own ``refs/heads/main``
    reduces to ``main``, which must NOT match our ``origin/main``, and the trailing-component
    form would have called it ours.
    """
    text = str(ref or "").strip()
    for prefix in ("refs/", "remotes/", "heads/"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text


def _merge_is_ours(source: str | None, remote_branch: str) -> bool:
    """Whether a mid-merge was created by pulling ``remote_branch``.

    Fails CLOSED: an absent, empty or unrecognized source is NOT ours, which leaves the
    destructive abort unreachable rather than guessing in its favour.
    """
    if not source:
        return False
    return _normalize_ref(source) == _normalize_ref(remote_branch)


def _merge_state(transport: Transport) -> dict:
    """``dolt_merge_status`` as a row, including WHOSE merge it is.

    Arms 1-3 of ``preflight`` re-sample the same value because it blinks under a concurrent
    writer. This read is ATTRIBUTED instead, not repeated, because the question here is
    different: a pull into a store a peer is mid-merge in fails rather than silently
    proceeding, so the thing a second read must establish is not "is it still true" but
    "whose merge is it". ``source`` is that second reading.
    """
    rows = transport.sql("select is_merging, source, target from dolt_merge_status")
    return rows[0] if rows else {}


def _pull_phase(transport: Transport, before: str | None) -> Outcome | None:
    """Pull, then read the store back. Returns an Outcome only when the protocol must stop."""
    pulled = transport.pull()
    try:
        state = _merge_state(transport)
    except TransportError as exc:
        return Outcome(
            EXIT_NOT_VERIFIED, "pull", f"post-pull state unreadable: {exc}", before
        )
    if _truthy(state.get("is_merging")):
        source, target = state.get("source"), state.get("target")
        # `dolt_merge('--abort')` is the only destructive statement in this file, so it is
        # reachable ONLY for a merge this protocol just created: the pull must have failed
        # (a pull returning 0 cannot have left a merge) AND the merge's source must be the
        # remote branch we pulled. The per-database lock keeps another PUBLISH out, but a
        # peer's raw `bd dolt pull` or CLI merge is not under that lock, and aborting it
        # would destroy their in-flight work to fix nothing (ISSUE-REF).
        ours = pulled.returncode != 0 and _merge_is_ours(
            source, transport.remote_branch
        )
        if not ours:
            return Outcome(
                EXIT_NOT_VERIFIED,
                "pull",
                f"the store is mid-merge (source={source}, target={target}) but this "
                f"protocol did not create that merge (pull rc={pulled.returncode}), so it "
                "did NOT abort it: an abort would end whoever's merge is in flight. "
                f"Identify the owner before touching it, see ISSUE-REF. Nothing was pushed.",
                head_before=before,
                head_after=head(transport),
            )
        transport.sql("call dolt_merge('--abort')")
        cleared = (
            ""
            if not _truthy(_merge_state(transport).get("is_merging"))
            else " -- ABORT DID NOT CLEAR is_merging"
        )
        return Outcome(
            EXIT_CONFLICT_ABORTED,
            "pull",
            "the pull left a conflicted merge of the branch this protocol pulled "
            f"({source}); it was aborted (an abort picks no side and the remote stays "
            f"re-pullable){cleared}. Classify before retrying: {CLASSIFIER}. "
            "Nothing was pushed.",
            head_before=before,
            head_after=head(transport),
        )
    if pulled.returncode != 0:
        return Outcome(
            EXIT_PULL_FAILED,
            "pull",
            f"pull failed (rc={pulled.returncode}): "
            f"{(pulled.stderr or pulled.stdout).strip()}",
            head_before=before,
        )
    return None


def read_remote_head(transport: Transport) -> tuple[str | None, str | None]:
    """``(hash, unresolved reason)`` -- a failed or absent read is NAMED, never a bare None.

    Attribution needs the difference between "the ref is at X" and "X could not be read",
    which a single nullable return collapses. It never raises: the three remote readings are
    evidence for the reader, and the one of them that is a GATE (the post-push landing) is
    judged in ``_push_phase`` from the returned pair, not from an exception.
    """
    try:
        value = remote_head(transport)
    except TransportError as exc:
        return None, f"remote-tracking read failed: {exc}"
    if value is None:
        return None, "transport recorded no remote-tracking ref"
    return value, None


def ref_contains(
    transport: Transport, ref: str, commit: str
) -> tuple[bool | None, str | None]:
    """Is ``commit`` in ``ref``'s history? ``(answer, unresolved reason)`` -- never raises.

    The landing test D-14 needs is ANCESTRY, not equality (``ISSUE-REF``): a push has landed
    the moment the remote ref's history holds the pushed head, whether or not another writer
    has since moved the ref past it.

    ``dolt_log(<ref>)`` over ``dolt_merge_base(<a>,<b>) = <a>``, which is equally correct and
    was measured equally fast: an unresolvable hash makes ``dolt_merge_base`` raise ``Error
    1105 (HY000): branch not found``, where this form answers 0 and the caller can exit 7
    loudly instead. Measured read-only against the live 18,095-commit ``AIH`` store
    2026-10-08: 0.31 s for the full-history MISS, 0.29 s for the hit.

    It returns a pair rather than raising because ``_push_phase`` runs OUTSIDE
    ``_protocol``'s ``try`` -- a raising probe would escape ``publish()`` altogether, and the
    question it failed to answer is the landing, which already has an exit code for exactly
    that (``EXIT_NOT_VERIFIED``).
    """
    try:
        rows = transport.sql(
            f"select count(*) as n from dolt_log({_quote(ref)}) "
            f"where commit_hash = {_quote(commit)}"
        )
    except TransportError as exc:
        return None, f"ancestry read failed: {exc}"
    return _count(rows) > 0, None


def render_head(value: str | None, reason: str | None) -> str:
    """A head for human and JSON output; an unread one says so instead of printing None."""
    if value is not None:
        return value
    return f"UNRESOLVED({reason or 'unread'})"


def attribute_advance(
    *,
    remote_at_start: str | None,
    before_read: tuple[str | None, str | None],
    after_read: tuple[str | None, str | None],
    pushed_head: str | None,
) -> tuple[str, str]:
    """Did THIS push move the remote ref? ``(verdict, reason)``, never a bare guess.

    ``before_read`` and ``after_read`` are ``read_remote_head`` pairs; ``remote_at_start``
    is the pre-pull sample, and it is load-bearing -- without it a ref another writer
    moved during this run reads as a clean advance by this one (pinned by a control in
    tests/test_bd_dolt_publish.py). ``unknown`` is a first-class answer: on a store
    several sessions publish to, an ambiguous signal must not be rounded to the answer
    the caller wants.
    """
    remote_before, before_reason = before_read
    remote_after, after_reason = after_read
    for label, reason in (("pre-push", before_reason), ("post-push", after_reason)):
        if reason is not None:
            return (
                ADVANCED_UNKNOWN,
                f"the {label} remote read did not resolve ({reason})",
            )
    if remote_at_start is not None and remote_at_start != remote_before:
        return (
            ADVANCED_UNKNOWN,
            f"{MOVED_BETWEEN_READS}: {remote_at_start} pre-pull -> {remote_before} "
            "pre-push, so another writer moved the ref during this run and this push is "
            "not provably the sole cause of the advance",
        )
    if remote_before == remote_after:
        if remote_after == pushed_head:
            return (
                ADVANCED_NO,
                "the remote already held this run's head, so the push was a no-op -- a "
                "legitimate state on a shared store, not a failure",
            )
        return (
            ADVANCED_UNKNOWN,
            f"the remote did not move ({remote_after}) and does not hold the head this "
            f"run pushed ({pushed_head})",
        )
    if remote_after == pushed_head:
        return (
            ADVANCED_YES,
            f"this run pushed {pushed_head} and the remote moved "
            f"{remote_before} -> {remote_after}",
        )
    return (
        ADVANCED_UNKNOWN,
        f"the remote moved {remote_before} -> {remote_after}, which is not the head this "
        f"run pushed ({pushed_head})",
    )


def _push_phase(
    transport: Transport, before: str | None, remote_at_start: str | None = None
) -> Outcome:
    # The ref the push is made AGAINST, read immediately pre-push (ISSUE-REF attribution).
    remote_before, remote_before_reason = read_remote_head(transport)
    # The head as it stands IMMEDIATELY BEFORE the push is the one the push carries, and it
    # is what the remote must come to HOLD. Comparing the remote against a head re-read
    # AFTER the push manufactures a FALSE exit 7 whenever a concurrent writer commits in
    # between -- measured on this host, where the queue-bridge watcher exports eight repos
    # into the store every 30 s: a SUCCESSFUL push reported head_after r4uocgrg... against
    # remote_head_after skdrmmim..., which is the inverse of the defect exit 7 exists for.
    head_pushed = head(transport)
    from ai_chassis.wake.notifications import notifying

    with notifying(
        "prepare_beads", transport, head_pushed, remote_before, sys.modules[__name__]
    ):
        pushed = transport.push()
    after = head(transport)
    rendered_before = render_head(remote_before, remote_before_reason)
    if pushed.returncode != 0:
        return Outcome(
            EXIT_PUSH_FAILED,
            "push",
            f"push failed (rc={pushed.returncode}): "
            f"{(pushed.stderr or pushed.stdout).strip()}",
            head_before=before,
            head_after=after,
            head_pushed=head_pushed,
            remote_head_at_start=remote_at_start,
            remote_head_before=rendered_before,
            advanced_by_this_publish=ADVANCED_NO,
            advance_reason="the push failed, so this run advanced nothing",
        )
    # D-14: the verdict word is now DERIVED from this comparison rather than printed beside
    # it. Measured twice live before this change: `PUBLISHED (exit 0)` beside an unmoved
    # hash (ISSUE-REF), and four `PUBLISHED` publishes while the remote stayed six records
    # behind -- caught only by re-attesting afterwards.
    remote_after, remote_after_reason = read_remote_head(transport)
    verdict, reason = attribute_advance(
        remote_at_start=remote_at_start,
        before_read=(remote_before, remote_before_reason),
        after_read=(remote_after, remote_after_reason),
        pushed_head=head_pushed,
    )
    # Every exit below carries the same evidence; only the code and detail differ.
    evidence = dict(
        head_before=before,
        head_after=after,
        head_pushed=head_pushed,
        remote_head_at_start=remote_at_start,
        remote_head_before=rendered_before,
        remote_head_after=remote_after,
        advanced_by_this_publish=verdict,
        advance_reason=reason,
    )
    if remote_after is None:
        return Outcome(
            EXIT_NOT_VERIFIED,
            "push",
            f"the push returned 0 but {transport.remote_branch} could not be read "
            f"({remote_after_reason}), so the landing is UNVERIFIED, not published; a "
            "publish whose landing cannot be read is not a publish",
            converged=None,
            **evidence,
        )
    # ISSUE-REF: the landing test is ANCESTRY, not equality. D-14 fixed the LOCAL-writer
    # half of this by comparing against the head that was PUSHED; the REMOTE-writer half is
    # the mirror and equality cannot express it. Measured 2026-10-08 on example-project-ec2: two
    # consecutive publish ticks read exit 7 (`remotes/origin/main=1cvd2c9d... !=
    # pushed=b9957m0c...`, then `28nr6mp6... != 5l5hcec8...`) when BOTH pushes had landed --
    # `dolt_log('remotes/origin/main')` holds both pushed heads -- because a third fleet
    # writer published within the minute and moved the ref past them before this re-read.
    # Equality is kept as the fast path: it is the common case, needs no extra query, and
    # answers identically through both transports.
    if remote_after == head_pushed:
        landed, landed_reason = True, None
    else:
        landed, landed_reason = ref_contains(transport, remote_after, head_pushed)
    if landed is None:
        return Outcome(
            EXIT_NOT_VERIFIED,
            "push",
            f"the push returned 0 and {transport.remote_branch}={remote_after}, but whether "
            f"it holds the pushed head {head_pushed} could not be read ({landed_reason}), "
            "so the landing is UNVERIFIED, not published",
            converged=None,
            **evidence,
        )
    if not landed:
        return Outcome(
            EXIT_NOT_CONVERGED,
            "push",
            f"the push returned 0 but the remote does NOT hold the head that was pushed: "
            f"{transport.remote_branch}={remote_after} does not contain pushed={head_pushed}",
            converged=False,
            **evidence,
        )
    if remote_after != head_pushed:
        detail = (
            f"published; pushed {head_pushed} is an ancestor of "
            f"{transport.remote_branch}={remote_after} (another writer landed after it)"
        )
    else:
        detail = f"published; {transport.remote_branch} == pushed == {head_pushed}"
    if after != head_pushed:
        # Not a failure, and worth saying out loud so nobody reads the inequality as one.
        detail += (
            f" (local main moved on after the push to {after} -- a concurrent writer, e.g. "
            "the queue-bridge export; the pushed head IS on the remote)"
        )
    return Outcome(EXIT_OK, "push", detail, converged=True, **evidence)


def is_non_fast_forward_rejection(outcome: Outcome) -> bool:
    """Did the push fail because the real remote moved past what this run pulled?

    Read off the rejection TEXT, which ``_push_phase`` carries verbatim, rather than off the
    exit code: ``rc=1`` covers every push failure and only this one is resolvable by pulling
    again. The narrowness is the guard -- an auth failure, a refusing hook or bd's pre-push
    fsck timeout must stay terminal on the first attempt rather than being retried three
    times under the per-database lock.
    """
    if outcome.code != EXIT_PUSH_FAILED or outcome.step != "push":
        return False
    text = outcome.detail.lower()
    return any(marker in text for marker in NON_FAST_FORWARD_MARKERS)


def _pull_push_with_retry(
    transport: Transport,
    before: str | None,
    remote_at_start: str | None,
    *,
    attempts: int,
    backoff: float,
) -> Outcome:
    """Pull then push, re-pulling and re-pushing a non-fast-forward rejection (ISSUE-REF).

    Every attempt after the first RE-PROVES the preflight first. That is AC-3 and it is not a
    formality: the thing that makes a retry safe is that the store is still clean and still a
    descendant of the tracking ref, and a peer (or bd's own bookkeeping) can end either
    between attempts. A dirty or mid-merge store therefore takes the ordinary refusal exits
    rather than being pulled over -- the loop can only ever turn a rejection into a landing or
    into a stricter refusal, never into a laxer one.

    The whole loop runs inside :func:`publish`'s per-database lock, so no second publisher
    interleaves with it; the writer this races is on another machine.
    """
    attempt = 1
    while True:
        outcome = _pull_phase(transport, before) or _push_phase(
            transport, before, remote_at_start
        )
        last = attempt >= attempts
        if last or not is_non_fast_forward_rejection(outcome):
            break
        time.sleep(backoff)
        attempt += 1
        try:
            refusal = preflight(transport)
        except TransportError as exc:
            return replace(
                Outcome(
                    EXIT_NOT_VERIFIED,
                    "preflight",
                    f"store state unreadable before publish attempt {attempt}: {exc}",
                    head_before=before,
                ),
                attempts=attempt,
            )
        if refusal is not None:
            return replace(
                Outcome(EXIT_REFUSED, "preflight", refusal, head_before=before),
                attempts=attempt,
            )
    if attempt > 1:
        verb = "landed" if outcome.code == EXIT_OK else "still failed"
        outcome = replace(
            outcome,
            detail=(
                f"{outcome.detail}; {verb} on attempt {attempt} of {attempts} after a "
                "non-fast-forward rejection (a peer pushed between this run's pull and its "
                "push)"
            ),
        )
    return replace(outcome, attempts=attempt)


def _protocol(
    transport: Transport,
    *,
    settle_derived: bool = True,
    push_attempts: int = PUSH_RETRY_ATTEMPTS,
    retry_backoff: float = PUSH_RETRY_BACKOFF,
) -> Outcome:
    try:
        # The pre-pull baseline is read FIRST and never raises: it is evidence for the
        # reader (and `attribute_advance`'s control against a ref another writer moved),
        # not a gate. The gate is the post-push equality in `_push_phase`, which does treat
        # an unreadable ref as NOT VERIFIED. Before this, `head_before`/`head_after` gave
        # the LOCAL movement while the remote was only ever read AFTER the push (ISSUE-REF),
        # so "the remote did not move" and "the remote was already there" were
        # indistinguishable.
        remote_at_start, _ = read_remote_head(transport)
        # Ordered BEFORE preflight on purpose, and it is the only step here that writes.
        # preflight's own invariant is that every check it makes is a READ, so clearing a
        # proved-derived dirt cannot live inside it; and ordered before rather than after
        # because the state it clears is precisely one preflight would refuse on. If it
        # declines, preflight still refuses -- now with the columns named.
        settlement = settle_derived_dirt(transport, enabled=settle_derived)
        refusal = settlement.refusal or preflight(transport)
        before = head(transport)
    except TransportError as exc:
        return Outcome(EXIT_NOT_VERIFIED, "preflight", f"store state unreadable: {exc}")
    if refusal is not None:
        step = "settle" if settlement.refusal else "preflight"
        outcome = Outcome(EXIT_REFUSED, step, refusal, head_before=before)
    else:
        outcome = _pull_push_with_retry(
            transport,
            before,
            remote_at_start,
            attempts=push_attempts,
            backoff=retry_backoff,
        )
        if settlement.note:
            outcome = replace(outcome, detail=f"{outcome.detail}; {settlement.note}")
    return replace(
        outcome,
        remote_head_at_start=remote_at_start,
        settled_derived_dirt=settlement.committed,
    )


def publish(
    transport: Transport,
    *,
    settle_derived: bool = True,
    push_attempts: int = PUSH_RETRY_ATTEMPTS,
    retry_backoff: float = PUSH_RETRY_BACKOFF,
) -> Outcome:
    """Run the whole protocol under the per-database lock.

    Refuses while /sync-git's alignment journal is pending for the store: its aligned rows
    hold the remote's cells until ``post`` restores them, and a write that timed out there
    can still land and commit, so publishing now would push values nobody restored.
    Checked under the lock /sync-git holds from its fetch through its restore, so a live
    run is waited for, not raced. The journal is looked up beside that same lock, never
    under this process's ``XDG_STATE_HOME``, so a scheduled run with a different
    environment from the /sync-git that wrote it still finds it. Every live transport
    names its database; a transport without one (the embedded and test transports) has
    no journal to honour.
    """
    with exclusive_file_lock(transport.lock_path):
        database = getattr(transport, "database", None)
        root = transport.lock_path.parent / align.SYNC_STATE_DIRNAME
        pending = align.pending_journals(database, root) if database else []
        if pending:
            journal = pending[0]
            if (journal / align.FENCE_NAME).exists():
                home = getattr(transport, "home", None)
                next_step = align.fence_refusal(database, journal, home)
            else:
                next_step = f"Run `{align.recorded_post_command(database, journal)}`"
            return Outcome(
                EXIT_REFUSED,
                "alignment journal",
                f"an alignment of {database} awaits its restore ({journal}); "
                "publishing would push cells it set from the remote, so nothing was "
                f"fetched, merged or pushed. {next_step}, and only once it exits 0 "
                f"delete {journal / align.PENDING_MARKER}",
            )
        return _protocol(
            transport,
            settle_derived=settle_derived,
            push_attempts=push_attempts,
            retry_backoff=retry_backoff,
        )


def publish_holding_lock(transport: Transport) -> Outcome:
    """The same protocol for a caller that ALREADY holds ``transport.lock_path``.

    ``publish()`` cannot be nested inside that caller: a second ``flock`` on the same file
    blocks even within one process, so the call would wait on itself. ``/sync-git``'s
    Beads step (``scripts/bd_store_sync.py``) holds the lock across a fetch, an alignment
    and a restore around this call, so no publisher can interleave between them. Every
    step and exit code is ``publish()``'s own; only the lock acquisition moves out.
    """
    return _protocol(transport)


def run_attestation(location) -> dict:
    """Run the offline lineage attestation against ``location``'s store.

    Imported lazily and kept behind ``--attest`` on purpose: an attestation re-fetches the
    remote's whole ``refs/dolt/data`` into scratch (661 MB for the AIH store), which is the
    divergence-gate latency D-6 removed. Paying it on every publish would re-introduce it.
    """
    import bd_lineage_attest as attest

    remote_url = attest.remote_url_from_store(location.database_dir)
    if not remote_url:
        return {
            "verdict": attest.UNKNOWN,
            "sync_state": attest.UNKNOWN,
            "detail": "the live store declares no `origin` remote",
        }
    work = Path(tempfile.mkdtemp(prefix="bd-dolt-publish-attest-"))
    try:
        return asdict(attest.attest_store(location, remote_url, work))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _with_attestation(outcome: Outcome, location) -> Outcome:
    """Fold the attestation's verdict and sync_state into the outcome (D-14's opt-in (c)).

    Only reached for an already-converged push: a push that failed is non-zero already, and
    re-fetching hundreds of megabytes could not change that.
    """
    import bd_lineage_attest as attest

    record = run_attestation(location) or {}
    verdict, state = record.get("verdict"), record.get("sync_state")
    # Compared against the attestation module's OWN constants, not string literals here, so
    # a rename there cannot silently turn this gate into one that never fires.
    if verdict == attest.SHARED and state == attest.CONVERGED:
        return replace(outcome, attestation=record)
    return replace(
        outcome,
        code=EXIT_NOT_CONVERGED,
        detail=(
            f"{outcome.detail}; but the offline attestation reports verdict={verdict} "
            f"sync_state={state}, so the local store's records and fields are NOT provably "
            "on the published remote"
        ),
        converged=False,
        attestation=record,
    )


def resolve_endpoint(
    host: str, port: int | None, repo: Path, home: Path | None = None
) -> Endpoint:
    """The shared server's endpoint, from the records bd itself writes.

    bd records the port PER REPO in ``<repo>/.beads/dolt-server.port`` (measured on
    example-project-ec2: every store has it, the shared root has only the lock and the log), so the
    shared-root record is tried first and the repo's own file second. Never guessed.
    """
    if port is not None:
        return Endpoint(host=host, port=port, pid=None)
    endpoint = shared_server_endpoint(home)
    if endpoint is not None:
        return endpoint
    try:
        recorded = int((repo / ".beads" / "dolt-server.port").read_text().strip())
    except (OSError, ValueError) as exc:
        raise TransportError(
            "no shared-server port recorded (neither under the shared root nor in "
            f"{repo}/.beads/dolt-server.port); pass --port explicitly"
        ) from exc
    return Endpoint(host=host, port=recorded, pid=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bd_dolt_publish.py",
        description="Serialized pull-then-push publication of a Beads store's Dolt data plane.",
    )
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="Repo root of the store."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="Emit the outcome as JSON.")
    parser.add_argument(
        "--attest",
        action="store_true",
        help=(
            "After a converged push, run the offline lineage attestation and exit "
            f"{EXIT_NOT_CONVERGED} unless its verdict is `shared` and its sync_state is "
            "`converged`. Opt-in: it re-fetches the remote's whole refs/dolt/data."
        ),
    )
    parser.add_argument(
        "--no-settle-derived",
        dest="settle_derived",
        action="store_false",
        help=(
            "Do not commit a dirty working set even when it is proved to hold only bd's "
            "derived columns; refuse instead, naming the columns. Default is to commit it, "
            "because the refusal it removes blocks an UNATTENDED timer across every store."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        location = resolve_location(args.repo.resolve())
        endpoint = resolve_endpoint(args.host, args.port, location.repo)
    except (FileNotFoundError, TransportError) as exc:
        print(f"bd-dolt-publish: {exc}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    transport = BdServerTransport(
        location.repo, location.dolt_database, endpoint=endpoint
    )
    outcome = publish(transport, settle_derived=args.settle_derived)
    if args.attest and outcome.code == EXIT_OK:
        outcome = _with_attestation(outcome, location)
    if args.json:
        print(
            json.dumps(
                {"transport": transport.name, **asdict(outcome)},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        # Derived from the code, never printed beside it: `published` is `code == EXIT_OK`,
        # and EXIT_OK is now only reachable when the remote moved to local main (D-14).
        verdict = "PUBLISHED" if outcome.published else "NOT PUBLISHED"
        print(f"  store   : {transport.name}")
        print(f"  step    : {outcome.step}")
        print(f"  result  : {verdict} (exit {outcome.code})")
        print(f"  detail  : {outcome.detail}")
        print(
            f"  main    : {outcome.head_before} -> {outcome.head_after}"
            f"  (pushed: {outcome.head_pushed})"
        )
        # Three remote readings, each named; an unread one prints as UNRESOLVED(<reason>),
        # never as None where a head belongs (ISSUE-REF).
        print(
            "  remote  : "
            f"remote_head_at_start={render_head(outcome.remote_head_at_start, 'not read')}"
            " -> "
            f"remote_head_before={render_head(outcome.remote_head_before, 'not read')}"
            " -> "
            f"remote_head_after={render_head(outcome.remote_head_after, 'not read')}"
            f" (converged: {outcome.converged})"
        )
        reason = f" -- {outcome.advance_reason}" if outcome.advance_reason else ""
        print(
            "  advanced: "
            f"advanced_by_this_publish={outcome.advanced_by_this_publish}{reason}"
        )
        if outcome.attestation:
            print(
                f"  attested: verdict={outcome.attestation.get('verdict')} "
                f"sync_state={outcome.attestation.get('sync_state')}"
            )
    return outcome.code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
