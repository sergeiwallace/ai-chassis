#!/usr/bin/env python3
"""Attest a Beads store's Dolt lineage against its PUBLISHED remote -- offline and read-only.

D-8(b) prerequisite 3 (docs/designs/beads-concurrent-sync-resilience.md, D-11) discharges per
store by one verdict: ``shared``, ``unrelated`` or ``unknown``. The runbook's one-line check,
``select dolt_merge_base('main','remotes/origin/main')``, answers it ONLY when a remote-tracking
ref exists, and on this fleet it usually does not: bd's ``git+ssh://`` transport (its
"gitblobstore") never writes ``remotes/origin/main`` on a failed push, so three of six stores on
``example-project-ec2`` read ``branch not found`` for ten days and were recorded ``unknown`` -- while all
three were in fact ``unrelated`` (measured 2026-09-27). An absent tracking ref is the weakest
evidence there is, and it was hiding the exact condition prerequisite 3 exists for.

WHAT THIS DOES INSTEAD, with no write to any live store:

1. Fetches the remote's ``refs/dolt/data`` into a SCRATCH bare git repository. That ref is bd's
   whole remote: a commit whose tree is a raw Dolt chunk store (``manifest`` plus table files),
   with any table file over GitHub's blob limit split into ``<name>.darc/0001, 0002, ...``.
2. Reconstructs that tree as a bare noms directory and re-joins the split table files. A Dolt
   ``file://`` remote IS such a directory, so the reconstruction is directly fetchable -- as long
   as nothing ever opens it with ``dolt sql``, which writes a chunk journal into it and makes
   ``dolt fetch`` refuse (``cannot create NBS store for directory containing chunk journal``).
   The copy that is READ is therefore always a second copy.
3. Copies the live database directory (the same resolution ``bd_store_backup.py`` uses, never
   ``bd info``'s nominal path) into scratch, adds the reconstruction as a remote, fetches, and asks
   ``dolt_merge_base``. The verdict is read off the error text, not inferred: a hash is ``shared``,
   ``no common ancestor`` is ``unrelated``, anything else is ``unknown`` and is never upgraded.

The record it emits quotes both the git OID of ``refs/dolt/data`` and the Dolt hash of the remote's
``main``, because a verdict without the ref it was computed against is a claim about an unknown
moment (D-11 exit criterion 1). Exit codes: 0 shared, 3 unrelated, 2 unknown or not measurable,
and 4 with ``--require-converged`` when the drift comparison below is not ``converged``.

FIELD-LEVEL DRIFT (D-13, T-5.1). The id-set verdict above was blind twice over, and both
blindnesses were measured on 2026-10-04:

* On purpose: ``local_only_ids``/``remote_only_ids`` are set differences of ``id``, so a status
  flip, a retitle, a new comment or a new edge on a SHARED id is invisible. It could prove two
  new issues had shipped while unable to prove a close on a pre-existing record had.
* By accident: the old ``remote_issue_count`` was ``select count(*) from issues`` on the
  reconstructed remote, which Dolt resolves against the WORKING SET that bd's push carried
  inside the chunk store. On ``project-template`` that printed ``local 38 / remote 27`` while
  ``issues as of 'main'`` was 38 on BOTH sides. Two sessions then quoted matching working-set
  reads as corroborating sync evidence. A field name that can be read two ways will be, so the
  counts are now ``local_head_issue_count`` / ``remote_head_issue_count`` (each read ``as of``
  a commit) and ``local_working_set_issue_count`` / ``local_uncommitted_tables``.

So after the fetch, ``main`` and ``remotes/lineage-attest/main`` are two commits in one scratch
store and every comparison reads one of them BY NAME:

* ``issues`` come from ``dolt_diff('<remote ref>','main','issues')``, keyed by the primary key.
  Only columns the registry calls AUTHORED are compared; the columns bd rewrites on every write
  are classified EXCLUDED and a row differing only in those produces no finding at all.
* ``comments`` are candidate-matched by ``(issue_id, author, normalized text)`` and compared
  as an occurrence multiset -- never keyed by ``id`` or ``created_at``, both of which ``bd
  comment add`` mints fresh. Exact authored text remains payload, including Markdown whitespace.
* ``dependencies`` match stable identity ``(issue_id, target, type)``, then every non-derived
  payload column is compared.
* ``labels`` match ``(issue_id, label)``.

The registry is a CLOSED allowlist read against each side's own schema at runtime: a column
present in neither class is a hard refusal (``verdict`` and ``sync_state`` ``unknown``, exit 2),
never a silent exclusion -- an instrument that answers a narrower question than its field names
imply is the defect this whole tool exists to remove. A table absent from BOTH committed trees
is skipped (it holds no record on either side); a table present on ONE side still diffs, so a
remote-only table's rows are found rather than missed.

Each finding carries a DIRECTION, and the rule is reproduced here as well as in ``--help``
(``RECENCY_RULE``) because D-13's own mitigation for the "newer wins" temptation is that the
rule travels with the instrument reporting it: ``local_newer`` and ``remote_newer`` from
``updated_at``; ``unstamped`` when the stamps are equal but the content differs (bd's ``dep
add`` and label writes do not bump it) and ``unorderable`` when a stamp is missing -- both are
findings, never an ordering. A one-sided row is ``local_added``/``remote_added`` when absent
from the merge base and ``local_deleted``/``remote_deleted`` when present there. These
summarize to ``sync_state``: ``converged``, ``local_ahead``, ``remote_ahead``, or ``diverged``
(which absorbs every ``unstamped`` and ``unorderable``, so no gate can read one as clean).

**This tool never writes, and recency never resolves a contested row.** It says which side a
publish would carry forward uncontested; where both sides changed the same row, nothing
overrides -- the merge conflicts, the protocol aborts, and
``scripts/bd_dolt_conflict_classify.py`` is the next step.
"""

from __future__ import annotations

# `docs/procedures/new-machine-bootstrap.md` invokes this with a bare `python3`, which on an
# Ubuntu 22.04 host is 3.10 and cannot be upgraded. Re-exec under the project floor before
# anything else imports, the same preamble every other live `python3` target in this repo
# carries; enforced by tests/test_py_interpreter_bootstrap.py.
import os as _bootstrap_os
import sys as _bootstrap_sys

_bootstrap_sys.path.insert(
    0, _bootstrap_os.path.dirname(_bootstrap_os.path.realpath(__file__))
)
from py_interpreter_bootstrap import ensure_supported_python  # noqa: E402

ensure_supported_python()

import argparse  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from collections import Counter, defaultdict  # noqa: E402
from dataclasses import asdict, dataclass, field  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from attest_scratch import make_scratch, term_cleanup  # noqa: E402
from bd_store_backup import StoreLocation, resolve_location  # noqa: E402

# The direction vocabulary and the stamp comparison come from the canonical comparator so the
# fleet does not answer "which side is newer" two different ways -- the ISSUE-REF failure.
from check_beads_divergence import (  # noqa: E402
    MIRROR_BEHIND,
    UNORDERABLE,
    UNSTAMPED,
    _normalized_text,
    record_direction,
)

DOLT_DATA_REF = "refs/dolt/data"
# The scratch remote name. Distinct from anything a graft or a store registers (`origin`,
# `pub`), because the copy inherits the live store's remotes and a collision fails silently.
ATTEST_REMOTE = "lineage-attest"
DOLT_HASH_LEN = 32
# `dolt sql -r csv` prints a header row before the single scalar row.
CSV_HEADER_AND_ROW = 2
SHARED = "shared"
UNRELATED = "unrelated"
UNKNOWN = "unknown"
EXIT_SHARED = 0
EXIT_UNKNOWN = 2
EXIT_UNRELATED = 3
EXIT_NOT_CONVERGED = 4

# bd registers remotes as git+<transport>://<host>/<owner>/<repo>.git; git wants the transport.
_BD_PREFIX = "git+"

# --- D-13: the recency rule, shipped in the tool ------------------------------------------
# It is in the docstring, in --help and in the module, because D-13's own mitigation for the
# "newer wins" temptation is that the rule travels with the instrument that reports it.
RECENCY_RULE = """\
Recency classification for a shared id whose authored columns differ (D-13):

  updated_at relation   direction      what it means for a publish
  --------------------  -------------  ------------------------------------------------------
  local newer           local_newer    the protocol's pull keeps it UNLESS the remote also
                                       changed the row, in which case Dolt raises a conflict
                                       and the protocol aborts -- nothing is overridden
  remote newer          remote_newer   a pull is owed before any claim of convergence; the
                                       local copy must not be force-pushed over it
  equal, content differs unstamped     bd wrote without bumping the stamp (dep add, labels);
                                       resolve by a checkable referent, NEVER by timestamp
  a stamp missing       unorderable    reported, never ordered

A row on one side only is local_added/remote_added when absent from the merge base and
local_deleted/remote_deleted when present there.

sync_state: converged (no findings) | local_ahead (only local_*) | remote_ahead (only
remote_*) | diverged (anything else, including every unstamped and unorderable finding).

This tool NEVER writes. Recency tells you which side the protocol will carry forward
uncontested; where both sides changed the same row nothing overrides -- the merge conflicts,
the protocol aborts, and scripts/bd_dolt_conflict_classify.py is the next step. The
attestation is ONE input to a mirror-retirement decision and never the proof: it compares
this store with its remote, and says nothing about whether a tracked JSONL mirror's records
are in the store at all (that is the separate per-record proof, asked in the other
direction).
"""

ISSUES = "issues"
COMMENTS = "comments"
DEPENDENCIES = "dependencies"
LABELS = "labels"
COMPARED_TABLES = (ISSUES, COMMENTS, DEPENDENCIES, LABELS)

AUTHORED = "authored"
IMMUTABLE = "immutable-but-compared"
EXCLUDED = "derived/node-local-and-excluded"

LOCAL_NEWER = "local_newer"
REMOTE_NEWER = "remote_newer"
LOCAL_ADDED = "local_added"
REMOTE_ADDED = "remote_added"
LOCAL_DELETED = "local_deleted"
REMOTE_DELETED = "remote_deleted"

CONVERGED = "converged"
LOCAL_AHEAD = "local_ahead"
REMOTE_AHEAD = "remote_ahead"
DIVERGED = "diverged"

LOCAL_DIRECTIONS = frozenset({LOCAL_NEWER, LOCAL_ADDED, LOCAL_DELETED})
REMOTE_DIRECTIONS = frozenset({REMOTE_NEWER, REMOTE_ADDED, REMOTE_DELETED})

DRIFT_SAMPLE_CAP = 20

# The pinned bd schema, measured 2026-10-04 off a SCRATCH COPY of the live `TPL` store
# (`describe` on issues/comments/dependencies/labels: 56/5/10/2 columns). This is a CLOSED
# allowlist: `unclassified_columns` refuses anything absent from it, so a bd release that adds
# a column fails the attestation rather than quietly dropping it from the comparison.
#
# EXCLUDED covers two kinds, and both are listed by name rather than omitted, because D-13
# requires every field to be classified:
#   * rewritten by bd on every write -- row_lock, content_hash, is_blocked, compaction_level,
#     compacted_at, compacted_at_commit, original_size. Comparing these reports churn on a
#     row whose authored payload is identical.
#   * generated identity -- a comment's and an edge's `id`, and a comment's `created_at`,
#     which `bd comment add` mints fresh on a replay of the same content.
FIELD_REGISTRY: dict[str, dict[str, str]] = {
    ISSUES: {
        column: AUTHORED
        for column in (
            "title",
            "description",
            "design",
            "acceptance_criteria",
            "notes",
            "status",
            "priority",
            "issue_type",
            "assignee",
            "estimated_minutes",
            "owner",
            "updated_at",
            "closed_at",
            "closed_by_session",
            "external_ref",
            "spec_id",
            "sender",
            "ephemeral",
            "wisp_type",
            "pinned",
            "is_template",
            "mol_type",
            "work_type",
            "source_system",
            "metadata",
            "source_repo",
            "close_reason",
            "event_kind",
            "actor",
            "target",
            "payload",
            "await_type",
            "await_id",
            "timeout_ns",
            "waiters",
            "hook_bead",
            "role_bead",
            "agent_state",
            "last_activity",
            "role_type",
            "rig",
            "due_at",
            "defer_until",
            "no_history",
            "started_at",
            "storage_class",
        )
    }
    | {column: IMMUTABLE for column in ("id", "created_at", "created_by")}
    | {
        column: EXCLUDED
        for column in (
            "content_hash",
            "compaction_level",
            "compacted_at",
            "compacted_at_commit",
            "original_size",
            "is_blocked",
            "row_lock",
        )
    },
    COMMENTS: {
        "issue_id": AUTHORED,
        "author": AUTHORED,
        "text": AUTHORED,
        "id": EXCLUDED,
        "created_at": EXCLUDED,
    },
    DEPENDENCIES: {
        "issue_id": AUTHORED,
        "type": AUTHORED,
        "created_at": IMMUTABLE,
        "created_by": IMMUTABLE,
        "metadata": AUTHORED,
        "thread_id": AUTHORED,
        "depends_on_issue_id": AUTHORED,
        "depends_on_wisp_id": AUTHORED,
        "depends_on_external": AUTHORED,
        "id": EXCLUDED,
    },
    LABELS: {"issue_id": AUTHORED, "label": AUTHORED},
}

# The columns each content identity is BUILT FROM, so the payload comparison can exclude
# them. Comparing an identity column again as payload would undo the normalization the
# identity depends on: a comment whose text differs only in whitespace matches as the same
# comment and would then be reported as a differing `text` -- which is the reflow-is-not-a-
# different-comment case `_normalized_text` exists for. Caught by the replay control.
IDENTITY_COLUMNS: dict[str, frozenset[str]] = {
    COMMENTS: frozenset({"issue_id", "author", "text"}),
    DEPENDENCIES: frozenset(
        {
            "issue_id",
            "type",
            "depends_on_issue_id",
            "depends_on_wisp_id",
            "depends_on_external",
        }
    ),
    LABELS: frozenset({"issue_id", "label"}),
}

# dolt_diff adds these around the row payload; they describe the DIFF, not the record.
_DIFF_META = frozenset(
    {"diff_type", "from_commit", "from_commit_date", "to_commit", "to_commit_date"}
)


@dataclass(frozen=True)
class Finding:
    """One record-level disagreement between the two committed trees.

    ``key`` is the record's own identity as the comparison matched it -- an issue id, or a
    rendered content key for the tables whose generated ids are not an identity. ``columns``
    holds the differing AUTHORED columns and is empty for a one-sided record.
    """

    table: str
    key: str
    direction: str
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Attestation:
    database: str
    repo: str
    remote_url: str
    remote_git_oid: str
    remote_git_committed_at: str
    remote_main: str | None
    local_main: str | None
    # Each count names a commit. The old `local_issue_count`/`remote_issue_count` pair is gone
    # (D-13 exit criterion): one of them was a working-set read and its name did not say so.
    local_head_issue_count: int | None
    remote_head_issue_count: int | None
    local_working_set_issue_count: int | None
    local_uncommitted_tables: int | None
    verdict: str
    merge_base: str | None
    local_only_ids: int | None
    remote_only_ids: int | None
    remote_head_in_local_ancestry: bool | None
    sync_state: str
    drift: dict = field(default_factory=dict)
    unclassified_columns: tuple[str, ...] = ()
    attested_at_utc: str = ""
    detail: str = ""


def git_url(bd_remote_url: str) -> str:
    """Translate bd's ``git+<transport>://`` remote form into the URL git itself accepts.

    bd prefixes every git-backed remote with ``git+`` (``git+ssh://``, ``git+https://``,
    ``git+file://``); git wants the bare transport.
    """
    if bd_remote_url.startswith(_BD_PREFIX):
        return bd_remote_url[len(_BD_PREFIX) :]
    return bd_remote_url


def classify_merge_base(
    returncode: int, stdout: str, stderr: str
) -> tuple[str, str | None]:
    """Map a ``dolt_merge_base`` query result onto the three verdicts.

    Only a hash is ``shared``. ``no common ancestor`` is the one error that means
    ``unrelated``. Every other failure -- ``branch not found``, a connection error, an empty
    result -- is ``unknown``, and unknown is never promoted: the absence of a tracking ref is
    exactly the reading this tool exists to replace, not to trust.
    """
    combined = f"{stdout}\n{stderr}"
    if "no common ancestor" in combined:
        return UNRELATED, None
    if returncode != 0:
        return UNKNOWN, None
    for line in reversed(stdout.strip().splitlines()):
        token = line.strip().strip('"')
        if (
            token
            and token != "base"
            and " " not in token
            and len(token) == DOLT_HASH_LEN
        ):
            return SHARED, token
    return UNKNOWN, None


def rejoin_chunked_tables(noms: Path) -> list[str]:
    """Concatenate ``<name>.darc/0001,0002,...`` part directories back into one table file."""
    rejoined: list[str] = []
    for part_dir in sorted(
        p for p in noms.iterdir() if p.is_dir() and p.suffix == ".darc"
    ):
        parts = sorted(p for p in part_dir.iterdir() if p.is_file())
        staging = part_dir.with_name(part_dir.name + ".rejoining")
        with staging.open("wb") as out:
            for part in parts:
                out.write(part.read_bytes())
        shutil.rmtree(part_dir)
        staging.rename(part_dir)
        rejoined.append(part_dir.name)
    return rejoined


def _run(
    argv: list[str], *, cwd: Path | None = None, runner=None
) -> subprocess.CompletedProcess:
    run = subprocess.run if runner is None else runner
    return run(
        argv, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=False
    )


def fetch_remote_data_ref(
    scratch: Path, url: str, *, runner=None
) -> tuple[str, str] | None:
    """Fetch ``refs/dolt/data`` from ``url`` into a scratch bare repo; return (oid, date)."""
    scratch.mkdir(parents=True, exist_ok=True)
    if (
        _run(["git", "init", "-q", "--bare", str(scratch)], runner=runner).returncode
        != 0
    ):
        return None
    fetched = _run(
        ["git", "fetch", "-q", url, f"{DOLT_DATA_REF}:{DOLT_DATA_REF}"],
        cwd=scratch,
        runner=runner,
    )
    if fetched.returncode != 0:
        return None
    oid = _run(
        ["git", "rev-parse", DOLT_DATA_REF], cwd=scratch, runner=runner
    ).stdout.strip()
    date = _run(
        ["git", "log", "-1", "--format=%cI", DOLT_DATA_REF], cwd=scratch, runner=runner
    ).stdout.strip()
    return oid, date


def reconstruct_remote(scratch: Path, pristine: Path, *, runner=None) -> list[str]:
    """Materialize the fetched tree as ``<pristine>/.dolt/noms`` and re-join split tables.

    Goes through a tar FILE rather than a pipe: the archive is binary, and a text-mode
    runner (the managed one tests inject) would corrupt it in transit.
    """
    noms = pristine / ".dolt" / "noms"
    noms.mkdir(parents=True)
    tarball = pristine / "tree.tar"
    archived = _run(
        ["git", "archive", "-o", str(tarball), DOLT_DATA_REF],
        cwd=scratch,
        runner=runner,
    )
    if archived.returncode != 0:
        raise RuntimeError(f"git archive failed: {archived.stderr.strip()}")
    extracted = _run(["tar", "-x", "-f", str(tarball), "-C", str(noms)], runner=runner)
    if extracted.returncode != 0:
        raise RuntimeError(f"tar failed: {extracted.stderr.strip()}")
    tarball.unlink()
    return rejoin_chunked_tables(noms)


def _readable_copy(pristine: Path, dest: Path) -> Path:
    """A SECOND copy that may be opened by ``dolt sql``; the pristine one never is."""
    shutil.copytree(pristine, dest)
    (dest / ".dolt" / "config.json").write_text("{}\n", encoding="utf-8")
    (dest / ".dolt" / "repo_state.json").write_text(
        json.dumps(
            {"head": "refs/heads/main", "remotes": {}, "backups": {}, "branches": {}}
        )
        + "\n",
        encoding="utf-8",
    )
    return dest


def _sql(db_dir: Path, query: str, *, runner=None) -> subprocess.CompletedProcess:
    return _run(["dolt", "sql", "-r", "csv", "-q", query], cwd=db_dir, runner=runner)


def _scalar(db_dir: Path, query: str, *, runner=None) -> str | None:
    done = _sql(db_dir, query, runner=runner)
    if done.returncode != 0:
        return None
    lines = done.stdout.strip().splitlines()
    return lines[-1].strip().strip('"') if len(lines) >= CSV_HEADER_AND_ROW else None


def _int(value: str | None) -> int | None:
    return int(value) if value is not None and value.isdigit() else None


def _json_sql(db_dir: Path, query: str, *, runner=None) -> list[dict] | None:
    """Rows from a query, or ``None`` when it failed. JSON, never CSV.

    CSV cannot distinguish an absent column from an empty string, and ``dolt_diff`` omits the
    whole payload of the side a one-sided row is missing from -- so sidedness read off CSV
    would be a guess.
    """
    done = _run(["dolt", "sql", "-r", "json", "-q", query], cwd=db_dir, runner=runner)
    if done.returncode != 0:
        return None
    if not done.stdout.strip():
        return []
    try:
        return json.loads(done.stdout).get("rows", [])
    except ValueError:
        return None


def unclassified_columns(table: str, columns) -> list[str]:
    """The columns of ``table`` the registry cannot classify, table-qualified and sorted."""
    known = FIELD_REGISTRY.get(table, {})
    return sorted(f"{table}.{column}" for column in columns if column not in known)


def _authored(table: str) -> tuple[str, ...]:
    return tuple(
        column
        for column, kind in FIELD_REGISTRY.get(table, {}).items()
        if kind in (AUTHORED, IMMUTABLE)
    )


def classify_direction(local_stamp: str | None, remote_stamp: str | None) -> str:
    """D-13's recency rule, delegated to the canonical stamp comparison.

    ``record_direction`` answers in the mirror-retirement vocabulary; the mapping is purely a
    rename, so the two instruments cannot drift apart on what "newer" means.
    """
    direction = record_direction(
        {"updated_at": local_stamp}, {"updated_at": remote_stamp}
    )
    if direction in (UNSTAMPED, UNORDERABLE):
        return direction
    return LOCAL_NEWER if direction == MIRROR_BEHIND else REMOTE_NEWER


def summarize_sync_state(findings) -> str:
    """``converged`` | ``local_ahead`` | ``remote_ahead`` | ``diverged`` (D-13)."""
    if not findings:
        return CONVERGED
    directions = {finding.direction for finding in findings}
    if directions <= LOCAL_DIRECTIONS:
        return LOCAL_AHEAD
    if directions <= REMOTE_DIRECTIONS:
        return REMOTE_AHEAD
    # Every unstamped and unorderable finding lands here, so no gate can read one as clean.
    return DIVERGED


def drift_block(findings) -> dict:
    """Per-table, per-direction counts plus capped samples carrying the differing columns."""
    counts: dict[str, dict[str, int]] = {}
    samples: dict[str, dict[str, list[dict]]] = {}
    for finding in findings:
        table_counts = counts.setdefault(finding.table, {})
        table_counts[finding.direction] = table_counts.get(finding.direction, 0) + 1
        bucket = samples.setdefault(finding.table, {}).setdefault(finding.direction, [])
        if len(bucket) < DRIFT_SAMPLE_CAP:
            bucket.append({"key": finding.key, "columns": list(finding.columns)})
    return {"total": len(findings), "counts": counts, "samples": samples}


def _one_sided_direction(*, present_locally: bool, at_base: bool) -> str:
    """The merge base turns a bare asymmetry into an add or a delete.

    Without it, "this row is only on one side" is two different events with opposite
    remedies, and nothing in the row says which.
    """
    if present_locally:
        return REMOTE_DELETED if at_base else LOCAL_ADDED
    return LOCAL_DELETED if at_base else REMOTE_ADDED


def _tables_present(db_dir: Path, revision: str, *, runner=None) -> set[str] | None:
    rows = _json_sql(db_dir, f"show tables as of {revision}", runner=runner)
    if rows is None:
        return None
    return {str(value) for row in rows for value in row.values()}


def _columns_of(db_dir: Path, table: str, revision: str, *, runner=None) -> set[str]:
    rows = _json_sql(db_dir, f"describe {table} as of {revision}", runner=runner)
    return {str(row.get("Field")) for row in rows or [] if row.get("Field")}


def _content_key(table: str, row: dict) -> tuple:
    """The identity the supported writers can reproduce, per table.

    Comments and edges carry a generated ``id`` (and comments a generated ``created_at``), so
    keying on those would report churn where the content is identical -- and could never be
    cleared by ``bd comment add``, the prescribed remedy.
    """
    if table == COMMENTS:
        return (
            _normalized_text(row.get("issue_id")),
            _normalized_text(row.get("author")),
            _normalized_text(row.get("text")),
        )
    if table == DEPENDENCIES:
        target = (
            row.get("depends_on_issue_id")
            or row.get("depends_on_wisp_id")
            or row.get("depends_on_external")
        )
        return (
            _normalized_text(row.get("issue_id")),
            _normalized_text(target),
            _normalized_text(row.get("type")),
        )
    return (_normalized_text(row.get("issue_id")), _normalized_text(row.get("label")))


def _render_key(key: tuple) -> str:
    """A content key as one reportable string; the parts are already normalized text."""
    return " | ".join(key)


def _issue_findings(rows: list[dict], base_ids: set[str]) -> list[Finding]:
    """Reduce ``dolt_diff(<remote ref>, main, issues)`` to per-record findings.

    Direction of the diff matters and reads backwards: ``from`` is the REMOTE ref and ``to``
    is local ``main``, so ``diff_type`` ``added`` means local-only and ``removed`` means
    remote-only. Reading those two words at face value inverts every direction.
    """
    authored = _authored(ISSUES)
    findings: list[Finding] = []
    for row in rows:
        diff_type = row.get("diff_type")
        if diff_type == "modified":
            differing = tuple(
                column
                for column in authored
                if row.get(f"to_{column}") != row.get(f"from_{column}")
            )
            if not differing:
                # Only churn columns moved; bd rewrites those on every write.
                continue
            findings.append(
                Finding(
                    ISSUES,
                    str(row.get("to_id") or row.get("from_id")),
                    classify_direction(
                        row.get("to_updated_at"), row.get("from_updated_at")
                    ),
                    differing,
                )
            )
            continue
        present_locally = diff_type == "added"
        key = str(row.get("to_id") if present_locally else row.get("from_id"))
        findings.append(
            Finding(
                ISSUES,
                key,
                _one_sided_direction(
                    present_locally=present_locally, at_base=key in base_ids
                ),
            )
        )
    return findings


def _multiset_findings(  # noqa: C901, PLR0912 -- occurrence pairing has four directions
    table: str,
    local_rows: list[dict],
    remote_rows: list[dict],
    base_rows: list[dict],
) -> list[Finding]:
    """Compare a table whose identity is content, as an occurrence MULTISET.

    Multiplicity is preserved, generated ids/stamps are ignored, and exact authored comment
    text remains payload so whitespace-only changes are findings.
    """
    identity = IDENTITY_COLUMNS.get(table, frozenset())
    # A normalized content key is only a replay candidate. Every authored byte remains
    # compared, including a comment's raw text: Markdown indentation and paragraph breaks
    # are data, not generated identity.
    payload = [
        column
        for column in _authored(table)
        if column not in identity or (table == COMMENTS and column == "text")
    ]
    local_groups: dict[tuple, list[dict]] = defaultdict(list)
    remote_groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in local_rows:
        local_groups[_content_key(table, row)].append(row)
    for row in remote_rows:
        remote_groups[_content_key(table, row)].append(row)
    local = Counter({key: len(rows) for key, rows in local_groups.items()})
    remote = Counter({key: len(rows) for key, rows in remote_groups.items()})
    base = Counter(_content_key(table, row) for row in base_rows)

    def signature(row: dict) -> str:
        return json.dumps(
            {column: row.get(column) for column in payload},
            sort_keys=True,
            default=str,
        )

    findings: list[Finding] = []
    for key in sorted(set(local) | set(remote)):
        local_unmatched = sorted(local_groups[key], key=signature)
        remote_unmatched = sorted(remote_groups[key], key=signature)
        remote_signatures = Counter(signature(row) for row in remote_unmatched)
        kept_local: list[dict] = []
        for row in local_unmatched:
            row_signature = signature(row)
            if remote_signatures[row_signature]:
                remote_signatures[row_signature] -= 1
            else:
                kept_local.append(row)
        local_unmatched = kept_local
        local_signatures = Counter(signature(row) for row in local_groups[key])
        kept_remote: list[dict] = []
        for row in remote_unmatched:
            row_signature = signature(row)
            if local_signatures[row_signature]:
                local_signatures[row_signature] -= 1
            else:
                kept_remote.append(row)
        remote_unmatched = kept_remote
        # Pair remaining occurrences deterministically. Equal candidate identity is not
        # payload equality: raw comment whitespace and edge metadata are still authored.
        paired = min(len(local_unmatched), len(remote_unmatched))
        for local_row, remote_row in zip(
            local_unmatched[:paired], remote_unmatched[:paired], strict=True
        ):
            differing = tuple(
                column
                for column in payload
                if local_row.get(column) != remote_row.get(column)
            )
            if differing:
                findings.append(Finding(table, _render_key(key), UNSTAMPED, differing))
        surplus = local[key] - remote[key]
        if surplus == 0:
            continue
        if surplus > 0:
            remote_deletions = min(max(base[key] - remote[key], 0), surplus)
            directions = [REMOTE_DELETED] * remote_deletions + [LOCAL_ADDED] * (
                surplus - remote_deletions
            )
        else:
            remote_surplus = -surplus
            local_deletions = min(max(base[key] - local[key], 0), remote_surplus)
            directions = [LOCAL_DELETED] * local_deletions + [REMOTE_ADDED] * (
                remote_surplus - local_deletions
            )
        findings.extend(
            Finding(table, _render_key(key), direction) for direction in directions
        )
    return findings


def detect_drift(
    local_copy: Path, ref: str, merge_base: str | None, *, runner=None
) -> dict:
    """Compare the local committed HEAD with the fetched remote HEAD, field by field.

    Returns ``{"findings": [...], "unclassified_columns": [...]}``. An unclassified column is
    returned rather than raised so the caller can report every one of them at once.
    """
    local_rev, remote_rev = "hashof('main')", f"'{ref}'"
    local_tables = _tables_present(local_copy, local_rev, runner=runner)
    remote_tables = _tables_present(local_copy, remote_rev, runner=runner)
    if local_tables is None or remote_tables is None:
        return {
            "findings": [],
            "unclassified_columns": [],
            "error": "could not list the tables of both committed trees",
        }

    findings: list[Finding] = []
    unclassified: list[str] = []
    for table in COMPARED_TABLES:
        if table not in local_tables and table not in remote_tables:
            # Absent from both committed trees: no record exists on either side to lose.
            continue
        columns = set()
        if table in local_tables:
            columns |= _columns_of(local_copy, table, local_rev, runner=runner)
        if table in remote_tables:
            columns |= _columns_of(local_copy, table, remote_rev, runner=runner)
        unclassified.extend(unclassified_columns(table, columns))
        compare = _compare_issues if table == ISSUES else _compare_by_content
        table_findings, error = compare(
            local_copy,
            table,
            _Sides(
                local=local_rev,
                remote=remote_rev,
                merge_base=merge_base,
                present_locally=table in local_tables,
                present_remotely=table in remote_tables,
            ),
            runner=runner,
        )
        if error:
            return {
                "findings": [],
                "unclassified_columns": unclassified,
                "error": error,
            }
        findings.extend(table_findings)
    return {
        "findings": findings,
        "unclassified_columns": sorted(set(unclassified)),
    }


@dataclass(frozen=True)
class _Sides:
    """What one table looks like on both committed trees.

    The three revisions are carried together because every read must name a commit, and the
    two presence flags because a table on ONE side only still has rows to find.
    """

    local: str
    remote: str
    merge_base: str | None
    present_locally: bool
    present_remotely: bool


def _compare_issues(
    local_copy: Path, table: str, sides: _Sides, *, runner=None
) -> tuple[list[Finding], str | None]:
    """Issues are keyed by primary key, so Dolt's own per-row diff is the right instrument."""
    rows = _json_sql(
        local_copy,
        f"select * from dolt_diff({sides.remote},'main','{table}')",
        runner=runner,
    )
    if rows is None:
        return [], f"dolt_diff on `{table}` failed"
    # A FAILED base read and an ABSENT base are different facts with opposite consequences:
    # with an empty base every one-sided row classifies as an ADD, so swallowing a failed
    # read here would silently relabel every delete. Only a genuinely absent merge base (an
    # unrelated lineage, already verdict 3) may produce an empty base.
    if sides.merge_base:
        base_rows = _json_sql(
            local_copy,
            f"select id from issues as of '{sides.merge_base}'",
            runner=runner,
        )
        if base_rows is None:
            return [], (
                f"could not read `issues` at the merge base {sides.merge_base}, so an added "
                "record cannot be told from a deleted one"
            )
    else:
        base_rows = []
    return _issue_findings(rows, {str(row.get("id")) for row in base_rows}), None


def _compare_by_content(
    local_copy: Path, table: str, sides: _Sides, *, runner=None
) -> tuple[list[Finding], str | None]:
    """Each side's whole table, read ``as of`` its own commit, compared as a multiset.

    Failing CLOSED on the two COMPARED sides is the point: an empty projection from a failed
    read would make every record on the other side look one-sided and -- worse -- would make
    the failed side's own records vanish from the findings entirely, which is a comparison
    answering "nothing found" because it could not look.
    """
    projections: list[list[dict]] = []
    failed: list[str] = []
    for label, revision, present in (
        ("local HEAD", sides.local, sides.present_locally),
        ("the remote ref", sides.remote, sides.present_remotely),
    ):
        if not present:
            projections.append([])
            continue
        rows = _json_sql(
            local_copy, f"select * from `{table}` as of {revision}", runner=runner
        )
        if rows is None:
            failed.append(f"`{table}` at {label}")
        projections.append(rows or [])
    if failed:
        return [], f"could not read {', '.join(failed)}"
    if sides.merge_base:
        # A table that does not exist at the base reads as absent, which is the right answer
        # for telling an add from a delete.
        base_rows = _json_sql(
            local_copy,
            f"select * from `{table}` as of '{sides.merge_base}'",
            runner=runner,
        )
        projections.append(base_rows or [])
    else:
        projections.append([])
    return _multiset_findings(table, *projections), None


def _unmeasured(detail: str) -> dict:
    """The shape every "could not compute it" return must have.

    ``sync_state`` is UNKNOWN rather than absent or ``converged``: a caller gating on
    convergence must not read a failed measurement as a clean one.
    """
    return {
        "verdict": UNKNOWN,
        "merge_base": None,
        "local_only_ids": None,
        "remote_only_ids": None,
        "remote_head_in_local_ancestry": None,
        "local_head_issue_count": None,
        "remote_head_issue_count": None,
        "local_working_set_issue_count": None,
        "local_uncommitted_tables": None,
        "findings": [],
        "drift": {},
        "unclassified_columns": [],
        "sync_state": UNKNOWN,
        "detail": detail,
    }


def resolve_exit(verdict: str, state: str, *, require_converged: bool) -> int:
    """The exit code, with ``--require-converged`` adding 4 on a non-converged comparison.

    An UNKNOWN verdict keeps exit 2 even under the flag: reporting "not converged" would imply
    a comparison that ran and disagreed, when in fact none could be computed.
    """
    code = exit_code(verdict)
    if verdict == UNKNOWN:
        return code
    if require_converged and state != CONVERGED:
        return EXIT_NOT_CONVERGED
    return code


def attest_offline(local_copy: Path, pristine_remote: Path, *, runner=None) -> dict:
    """Fetch the reconstruction into ``local_copy`` and compute the verdict there."""
    noms = pristine_remote / ".dolt" / "noms"
    # The copy inherits the live store's remotes, and a graft leaves a scratch remote of its
    # own behind; a name collision made `remote add` fail silently once and every count was
    # then measured against the STALE ref. So: a name nobody else uses, replaced if present,
    # and a failed add is a verdict of unknown rather than a fetch of whatever was there.
    _run(["dolt", "remote", "remove", ATTEST_REMOTE], cwd=local_copy, runner=runner)
    added = _run(
        ["dolt", "remote", "add", ATTEST_REMOTE, f"file://{noms}"],
        cwd=local_copy,
        runner=runner,
    )
    if added.returncode != 0:
        return _unmeasured(
            f"could not register the reconstruction as a remote: {added.stderr.strip()}"
        )
    fetched = _run(["dolt", "fetch", ATTEST_REMOTE], cwd=local_copy, runner=runner)
    if fetched.returncode != 0:
        return _unmeasured(
            f"dolt fetch from the reconstruction failed: {fetched.stderr.strip()}"
        )
    ref = f"remotes/{ATTEST_REMOTE}/main"
    base = _sql(
        local_copy,
        f"select dolt_merge_base('main','{ref}') as base",
        runner=runner,
    )
    verdict, merge_base = classify_merge_base(base.returncode, base.stdout, base.stderr)
    local_only = _scalar(
        local_copy,
        f"select count(*) from issues where id not in (select id from issues as of '{ref}')",
        runner=runner,
    )
    remote_only = _scalar(
        local_copy,
        f"select count(*) from issues as of '{ref}' r where r.id not in "
        "(select id from issues as of 'main')",
        runner=runner,
    )
    ancestry = _scalar(
        local_copy,
        "select count(*) from dolt_log where commit_hash = "
        f"(select hash from dolt_remote_branches where name='{ref}')",
        runner=runner,
    )
    # Both counts name a commit. `hashof` does NOT accept a remote-tracking ref (measured
    # 2026-10-04: `invalid ref spec`), so the remote side is read `as of '<ref>'` -- which is
    # still a committed read, because a fetched ref has no working set of its own.
    local_head = _int(
        _scalar(
            local_copy,
            "select count(*) from issues as of hashof('main')",
            runner=runner,
        )
    )
    remote_head = _int(
        _scalar(local_copy, f"select count(*) from issues as of '{ref}'", runner=runner)
    )
    # ...and the working-set reads carry `working_set` in their own field names, because the
    # measured defect was a single integer named `*_issue_count` that was one of these.
    working_set = _int(
        _scalar(local_copy, "select count(*) from issues", runner=runner)
    )
    uncommitted = _int(
        _scalar(local_copy, "select count(*) from dolt_status", runner=runner)
    )
    detail = (
        (base.stderr or base.stdout).strip().splitlines()[-1]
        if (base.stderr or base.stdout).strip()
        else ""
    )

    drift = detect_drift(local_copy, ref, merge_base, runner=runner)
    findings = drift["findings"]
    unclassified = drift["unclassified_columns"]
    if unclassified or drift.get("error"):
        # Fail CLOSED. A column nobody classified, or a comparison that could not run, makes
        # the whole attestation UNKNOWN -- never a narrower comparison reported as a verdict.
        reason = drift.get("error") or (
            "the schema carries columns this tool cannot classify, so the comparison would "
            f"answer a narrower question than its own field names imply: {', '.join(unclassified)}"
        )
        return {
            **_unmeasured(reason),
            "local_head_issue_count": local_head,
            "remote_head_issue_count": remote_head,
            "local_working_set_issue_count": working_set,
            "local_uncommitted_tables": uncommitted,
            "unclassified_columns": unclassified,
        }
    state = summarize_sync_state(findings)
    return {
        "verdict": verdict,
        "merge_base": merge_base,
        "local_only_ids": _int(local_only),
        "remote_only_ids": _int(remote_only),
        "remote_head_in_local_ancestry": None if ancestry is None else ancestry == "1",
        "local_head_issue_count": local_head,
        "remote_head_issue_count": remote_head,
        "local_working_set_issue_count": working_set,
        "local_uncommitted_tables": uncommitted,
        "findings": findings,
        "drift": drift_block(findings),
        "unclassified_columns": unclassified,
        "sync_state": state,
        "detail": detail,
    }


def attest_store(
    location: StoreLocation, remote_url: str, work: Path, *, runner=None
) -> Attestation:
    database, repo, live_db_dir = (
        location.dolt_database,
        location.repo,
        location.database_dir,
    )
    stamp = datetime.now(timezone.utc).isoformat()
    scratch = work / "remote-git"
    fetched = fetch_remote_data_ref(scratch, git_url(remote_url), runner=runner)
    if fetched is None:
        return _attestation(
            _unmeasured(f"could not fetch {DOLT_DATA_REF} from {git_url(remote_url)}"),
            stamp,
            database=database,
            repo=str(repo),
            remote_url=remote_url,
            remote_git_oid="",
            remote_git_committed_at="",
            remote_main=None,
            local_main=None,
        )
    oid, date = fetched
    pristine = work / "remote-pristine"
    reconstruct_remote(scratch, pristine, runner=runner)
    readable = _readable_copy(pristine, work / "remote-readable")
    remote_main = _scalar(
        readable, "select hash from dolt_branches where name='main'", runner=runner
    )
    local_copy = work / "local-copy"
    shutil.copytree(live_db_dir, local_copy, symlinks=True)
    local_main = _scalar(
        local_copy, "select hash from dolt_branches where name='main'", runner=runner
    )
    result = attest_offline(local_copy, pristine, runner=runner)
    return _attestation(
        result,
        stamp,
        database=database,
        repo=str(repo),
        remote_url=remote_url,
        remote_git_oid=oid,
        remote_git_committed_at=date,
        remote_main=remote_main,
        local_main=local_main,
    )


def _attestation(result: dict, stamp: str, **identity) -> Attestation:
    """Assemble the record: ``identity`` is the store/remote half, ``result`` the measured half."""
    return Attestation(
        **identity,
        local_head_issue_count=result.get("local_head_issue_count"),
        remote_head_issue_count=result.get("remote_head_issue_count"),
        local_working_set_issue_count=result.get("local_working_set_issue_count"),
        local_uncommitted_tables=result.get("local_uncommitted_tables"),
        verdict=result["verdict"],
        merge_base=result["merge_base"],
        local_only_ids=result.get("local_only_ids"),
        remote_only_ids=result.get("remote_only_ids"),
        remote_head_in_local_ancestry=result.get("remote_head_in_local_ancestry"),
        sync_state=result.get("sync_state", UNKNOWN),
        drift=result.get("drift") or {},
        unclassified_columns=tuple(result.get("unclassified_columns") or ()),
        attested_at_utc=stamp,
        detail=result["detail"],
    )


def remote_url_from_store(live_db_dir: Path, name: str = "origin") -> str | None:
    """The live remote, from the store's own ``repo_state.json`` (never the tracked config)."""
    try:
        state = json.loads(
            (live_db_dir / ".dolt" / "repo_state.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    remote = (state.get("remotes") or {}).get(name) or {}
    url = remote.get("url")
    return str(url) if url else None


def exit_code(verdict: str) -> int:
    return {SHARED: EXIT_SHARED, UNRELATED: EXIT_UNRELATED}.get(verdict, EXIT_UNKNOWN)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bd_lineage_attest.py",
        description="Attest a Beads store's Dolt lineage against its published remote, offline.",
        epilog=RECENCY_RULE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="Repo root of the store."
    )
    parser.add_argument(
        "--remote-url",
        default=None,
        help="Override the remote read from the live store.",
    )
    parser.add_argument(
        "--work",
        type=Path,
        default=None,
        help="Scratch root (default: a fresh temp dir).",
    )
    parser.add_argument(
        "--keep-work", action="store_true", help="Do not delete the scratch root."
    )
    parser.add_argument("--json", action="store_true", help="Emit the record as JSON.")
    parser.add_argument(
        "--require-converged",
        action="store_true",
        help=(
            f"Exit {EXIT_NOT_CONVERGED} when sync_state is not `converged` "
            "(an unmeasurable attestation still exits 2)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        location = resolve_location(args.repo.resolve())
    except FileNotFoundError as exc:
        print(f"bd-lineage-attest: {exc}", file=sys.stderr)
        return EXIT_UNKNOWN
    remote_url = args.remote_url or remote_url_from_store(location.database_dir)
    if not remote_url:
        print(
            "bd-lineage-attest: the live store declares no `origin` remote",
            file=sys.stderr,
        )
        return EXIT_UNKNOWN
    with term_cleanup():
        work = args.work or make_scratch("bd-lineage-attest-", keep=args.keep_work)
        try:
            record = attest_store(location, remote_url, work)
        finally:
            if not args.keep_work and args.work is None:
                shutil.rmtree(work, ignore_errors=True)
    if args.json:
        print(json.dumps(asdict(record), indent=2, sort_keys=True))
    else:
        print(f"  database : {record.database}")
        print(f"  remote   : {record.remote_url}")
        print(
            f"  ref      : {DOLT_DATA_REF} = {record.remote_git_oid} ({record.remote_git_committed_at})"
        )
        print(f"  remote main {record.remote_main}  local main {record.local_main}")
        print(f"  verdict  : {record.verdict.upper()}  ({record.detail})")
        print(
            f"  local-only ids {record.local_only_ids}  remote-only ids {record.remote_only_ids}"
        )
        print(
            f"  HEAD issues: local {record.local_head_issue_count}  "
            f"remote {record.remote_head_issue_count}"
        )
        print(
            f"  local working set: {record.local_working_set_issue_count} issues, "
            f"{record.local_uncommitted_tables} table(s) with uncommitted changes"
        )
        print(f"  sync     : {record.sync_state.upper()}")
        for table, directions in sorted((record.drift.get("counts") or {}).items()):
            for direction, count in sorted(directions.items()):
                print(f"    {table:14} {direction:16} {count}")
        if record.unclassified_columns:
            print(
                "  REFUSED  : unclassified columns "
                f"{', '.join(record.unclassified_columns)}"
            )
    return resolve_exit(
        record.verdict, record.sync_state, require_converged=args.require_converged
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
