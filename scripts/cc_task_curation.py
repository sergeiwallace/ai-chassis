"""Pure snapshot, classification, and ranking support for task-panel curation.

This module deliberately has no task-tool, Beads, or command-line integration.
Callers supply their own TaskList/TaskGet observations and evidence, and receive a
plan.  In particular, Beads results can only be reported as unmirrored here; they
can never add a row to a plan.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

SourceResult = Literal["confirms", "contradicts", "not-applicable", "unavailable"]
WorkType = Literal["code", "doc", "non-artifact"]
RowStatus = Literal["pending", "in_progress", "completed"]
IssueState = Literal["open", "closed", "closed-duplicate", "none", "unknown"]
RepoReading = Literal["demonstrating", "absent", "no-artifact-by-nature", "unreadable"]
WorktreeReading = Literal["active", "quiet", "none-exist", "unreadable"]

NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SOURCE_RESULTS = frozenset(("confirms", "contradicts", "not-applicable", "unavailable"))
WORK_TYPES = frozenset(("code", "doc", "non-artifact"))
ROW_STATUSES = frozenset(("pending", "in_progress", "completed"))
ISSUE_STATES = frozenset(("open", "closed", "closed-duplicate", "none", "unknown"))
REPO_READINGS = frozenset(
    ("demonstrating", "absent", "no-artifact-by-nature", "unreadable")
)
WORKTREE_READINGS = frozenset(("active", "quiet", "none-exist", "unreadable"))
DECISION_REASONS = frozenset(("already-done", "stale", "outdated", "not-worth"))


class CurationError(ValueError):
    """A supplied observation violates a curation safety precondition."""


class SnapshotError(CurationError):
    """The semantic and file snapshots cannot safely be used together."""


class ContainmentError(CurationError):
    """A snapshot path is unsafe or falls outside its expected root."""


class ReorderError(CurationError):
    """A reorder cannot safely continue or needs operator recovery."""


@dataclass(frozen=True)
class TaskRecord:
    row_key: str
    cc_id: str
    namespace: str
    subject: str
    description: str
    status: str
    active_form: str | None
    owner: str | None
    metadata: dict[str, Any]
    blocks: list[str]
    blocked_by: list[str]
    fingerprint: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class Snapshot:
    schema_version: int
    run_id: str
    taken_at: str
    namespace: str
    client_version: str
    rows: list[TaskRecord]
    file_copy_dir: str | None
    persisted: bool
    manifest_digest: str
    file_capture: dict[str, bytes] = field(repr=False)


@dataclass(frozen=True)
class Evidence:
    """Raw readings for all required sources, plus their recorded reasons."""

    issue_state: str
    repo_reading: str
    worktree_reading: str
    source_reasons: dict[str, str] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RowVerdict:
    row_key: str
    display_id: str | None
    bead_hash: str | None
    provenance: str
    verdict: str
    work_type: str
    row_status: str
    sources: dict[str, str]
    issue_state: str
    repo_reading: str
    worktree_reading: str
    superseded_by: str | None
    any_source_unavailable: bool
    decision_reason: str | None
    dispositions: list[str]
    disposition_consequences: dict[str, dict[str, str]]
    source_reasons: dict[str, str]
    evidence: list[str]
    proposed_status: str | None
    enrichment_payload: dict[str, Any] | None = None
    report: str | None = None


@dataclass(frozen=True)
class ChangeLogEntry:
    row_key: str
    old_status: str
    new_status: str
    evidence: list[str]


@dataclass(frozen=True)
class CurationPlan:
    schema_version: int
    snapshot: Snapshot
    verdicts: list[RowVerdict]
    rank_order: list[str]
    priority_writes: dict[str, int]
    requires_destructive: bool
    change_log: list[ChangeLogEntry] = field(default_factory=list)
    unmirrored_issues: list[dict[str, Any]] = field(default_factory=list)
    requires_codex_spot_check: bool = False


def validate_namespace(namespace: str) -> str:
    """Validate a namespace before a path is formed from it."""
    if not isinstance(namespace, str) or not NAMESPACE_RE.fullmatch(namespace):
        raise ContainmentError(
            "namespace must match ^[a-z0-9][a-z0-9._-]{0,63}$ (no path separators)"
        )
    if any(part in {".", ".."} for part in namespace.split("/")):
        raise ContainmentError("namespace may not contain . or .. components")
    return namespace


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _record_fingerprint(row_key: str, task: Mapping[str, Any]) -> str:
    payload = {
        "row_key": row_key,
        # The destructive guard must cover every TaskGet field, including edges
        # and forward-compatible fields a caller does not otherwise interpret.
        "fields": {key: value for key, value in task.items() if key != "id"},
    }
    return _digest(payload)


def record_fingerprint(row_key: str, task: Mapping[str, Any]) -> str:
    """Public identity fingerprint used by the reorder recovery protocol."""
    return _record_fingerprint(row_key, task)


def _task_record(namespace: str, task: Mapping[str, Any]) -> TaskRecord:
    if "id" not in task:
        raise SnapshotError("TaskGet row is missing id")
    cc_id = str(task["id"])
    row_key = f"{namespace}#{cc_id}"
    fields = dict(task)
    return TaskRecord(
        row_key=row_key,
        cc_id=cc_id,
        namespace=namespace,
        subject=str(task.get("subject", "")),
        description=str(task.get("description", "")),
        status=str(task.get("status", "pending")),
        active_form=task.get("activeForm"),
        owner=task.get("owner"),
        metadata=dict(task.get("metadata") or {}),
        blocks=list(task.get("blocks") or []),
        blocked_by=list(task.get("blockedBy") or []),
        fingerprint=_record_fingerprint(row_key, task),
        fields=fields,
    )


def _assert_contained(path: Path, root: Path) -> None:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise ContainmentError(f"path escapes its root: {path}")


def _reject_symlink_components(path: Path, root: Path) -> None:
    """Reject a symlink in path's existing chain without traversing it to copy data."""
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ContainmentError(f"path escapes its root: {path}") from exc
    current = root
    if current.exists() and stat.S_ISLNK(os.lstat(current).st_mode):
        raise ContainmentError(f"symlink in snapshot path: {current}")
    for component in relative.parts:
        current /= component
        if current.exists() and stat.S_ISLNK(os.lstat(current).st_mode):
            raise ContainmentError(f"symlink in snapshot path: {current}")


def take_snapshot(
    namespace: str,
    task_list: Iterable[Mapping[str, Any]],
    task_get: Callable[[str], Mapping[str, Any]],
    *,
    tasks_root: Path | None = None,
    client_version: str,
    run_id: str | None = None,
) -> Snapshot:
    """Capture driver-supplied TaskList/TaskGet observations exactly once.

    The session which owns the task tools supplies both observations.  The capture is
    serialized into the recovery artifact, rather than re-reading Claude Code's
    private task-store files from this process.
    """
    # Kept as an ignored compatibility parameter for direct pure-engine callers.
    # Snapshot bytes always come from the TaskGet observation above, never this path.
    del tasks_root
    namespace = validate_namespace(namespace)
    list_ids = [str(item["id"]) for item in task_list]
    if len(list_ids) != len(set(list_ids)):
        raise SnapshotError("TaskList contains duplicate row ids")
    rows = [_task_record(namespace, task_get(cc_id)) for cc_id in list_ids]
    if {row.cc_id for row in rows} != set(list_ids):
        raise SnapshotError("TaskGet disagrees with TaskList row set")
    file_capture = {f"{row.cc_id}.json": _canonical(row.fields) + b"\n" for row in rows}

    digest_rows = [
        {"row_key": row.row_key, "fingerprint": row.fingerprint, "fields": row.fields}
        for row in rows
    ]
    return Snapshot(
        schema_version=1,
        run_id=run_id or str(uuid.uuid4()),
        taken_at=datetime.now(UTC).isoformat(),
        namespace=namespace,
        client_version=client_version,
        rows=rows,
        file_copy_dir=None,
        persisted=False,
        manifest_digest=_digest(digest_rows),
        file_capture=file_capture,
    )


def _derive(raw: str, table: Mapping[str, SourceResult], label: str) -> SourceResult:
    try:
        return table[raw]
    except KeyError as exc:
        raise CurationError(f"invalid {label} raw reading: {raw!r}") from exc


def derive_sources(evidence: Evidence) -> dict[str, str]:
    """Derive the table's four-valued source results from raw observations."""
    return {
        "issue": _derive(
            evidence.issue_state,
            {
                "closed": "confirms",
                "closed-duplicate": "confirms",
                "open": "contradicts",
                "none": "not-applicable",
                "unknown": "unavailable",
            },
            "issue",
        ),
        "repo": _derive(
            evidence.repo_reading,
            {
                "demonstrating": "confirms",
                "absent": "contradicts",
                "no-artifact-by-nature": "not-applicable",
                "unreadable": "unavailable",
            },
            "repo",
        ),
        "worktree": _derive(
            evidence.worktree_reading,
            {
                "quiet": "confirms",
                "active": "contradicts",
                "none-exist": "not-applicable",
                "unreadable": "unavailable",
            },
            "worktree",
        ),
    }


def _validate_verdict_inputs(
    record: TaskRecord, work_type: str, evidence: Evidence, decision_reason: str | None
) -> None:
    if work_type not in WORK_TYPES:
        raise CurationError(f"invalid work_type: {work_type!r}")
    if record.status not in ROW_STATUSES:
        raise CurationError(f"invalid row_status: {record.status!r}")
    if evidence.issue_state not in ISSUE_STATES:
        raise CurationError(f"invalid issue_state: {evidence.issue_state!r}")
    if evidence.repo_reading not in REPO_READINGS:
        raise CurationError(f"invalid repo_reading: {evidence.repo_reading!r}")
    if evidence.worktree_reading not in WORKTREE_READINGS:
        raise CurationError(f"invalid worktree_reading: {evidence.worktree_reading!r}")
    if decision_reason is not None and decision_reason not in DECISION_REASONS:
        raise CurationError(f"invalid decision_reason: {decision_reason!r}")


def classify_row(
    record: TaskRecord,
    *,
    work_type: str,
    evidence: Evidence,
    display_id: str | None = None,
    bead_hash: str | None = None,
    superseded_by: str | None = None,
    decision_reason: str | None = None,
    repo_scope: str | None = None,
    selected_repo: str | None = None,
) -> RowVerdict:
    """Evaluate the published eleven-row verdict table in first-match order."""
    _validate_verdict_inputs(record, work_type, evidence, decision_reason)
    sources = derive_sources(evidence)
    for source, result in sources.items():
        if result not in SOURCE_RESULTS:
            raise CurationError(f"invalid {source} result: {result!r}")
        if result in {
            "not-applicable",
            "unavailable",
        } and not evidence.source_reasons.get(source):
            raise CurationError(f"{source}={result} requires a recorded reason")
    if evidence.issue_state == "closed-duplicate" and not superseded_by:
        raise CurationError("closed-duplicate issue requires superseded_by")
    if evidence.issue_state == "none":
        display_id = None
        bead_hash = None
    provenance = (
        "unlinked-orphan"
        if evidence.issue_state == "none"
        else "mirror-owned"
        if record.metadata.get("mirror_owner")
        else "bd-linked"
    )
    unavailable = "unavailable" in sources.values()
    verdict = "ambiguous"
    proposed_status: str | None = None
    dispositions: list[str] = []
    disposition_consequences: dict[str, dict[str, str]] = {}
    # Rows 1 through 11: first match wins.  Keep this ordering visually aligned
    # with the design; changing it changes safety semantics.
    if sources["worktree"] == "contradicts":
        verdict = "live"
    elif (
        sources["issue"] == "confirms"
        and evidence.issue_state == "closed-duplicate"
        and superseded_by is not None
    ):
        verdict = "superseded"
    elif unavailable:
        verdict = "ambiguous"
    elif (
        work_type == "code"
        and sources["issue"] == "confirms"
        and sources["repo"] == "confirms"
        and sources["worktree"] in {"confirms", "not-applicable"}
    ):
        verdict, proposed_status = "done", "completed"
    elif (
        work_type == "code"
        and sources["issue"] == "confirms"
        and sources["repo"] == "contradicts"
        and sources["worktree"] in {"confirms", "not-applicable"}
    ) or (
        work_type == "code"
        and sources["issue"] == "contradicts"
        and sources["repo"] == "confirms"
        and sources["worktree"] in {"confirms", "not-applicable"}
    ):
        verdict = "ambiguous"
    elif (
        work_type == "doc"
        and sources["issue"] == "confirms"
        and sources["repo"] == "confirms"
        and sources["worktree"] in {"confirms", "not-applicable"}
    ) or (
        work_type == "non-artifact"
        and sources["issue"] == "confirms"
        and sources["repo"] == "not-applicable"
        and sources["worktree"] in {"confirms", "not-applicable"}
    ):
        verdict, proposed_status = "done", "completed"
    elif (
        record.status == "in_progress"
        and sources["repo"] == "contradicts"
        and sources["worktree"] in {"confirms", "not-applicable"}
    ):
        verdict, proposed_status = "stale-active", "pending"
    elif record.status == "pending" and decision_reason is not None:
        verdict = "decision-needed"
        dispositions = ["close-both", "drop-row-only"]
        disposition_consequences = {
            "close-both": {
                "beads": f"close backing issue: {decision_reason}",
                "cc": "delete panel row",
            },
            "drop-row-only": {
                "beads": "leave backing issue open and unchanged",
                "cc": "delete panel row",
            },
        }

    enrichment = None
    if verdict == "done":
        enrichment = {
            "row_key": record.row_key,
            "subject": record.subject,
            "description": record.description,
            "status": record.status,
            "activeForm": record.active_form,
            "owner": record.owner,
            "metadata": record.metadata,
        }
    report = None
    if provenance == "unlinked-orphan":
        report = "promote-to-Beads; retain panel row"
    elif repo_scope and selected_repo and repo_scope != selected_repo:
        report = f"repo-scoped work belongs to {repo_scope}; not moved"
    return RowVerdict(
        row_key=record.row_key,
        display_id=display_id,
        bead_hash=bead_hash,
        provenance=provenance,
        verdict=verdict,
        work_type=work_type,
        row_status=record.status,
        sources=sources,
        issue_state=evidence.issue_state,
        repo_reading=evidence.repo_reading,
        worktree_reading=evidence.worktree_reading,
        superseded_by=superseded_by,
        any_source_unavailable=unavailable,
        decision_reason=decision_reason,
        dispositions=dispositions,
        disposition_consequences=disposition_consequences,
        source_reasons=dict(evidence.source_reasons),
        evidence=list(evidence.evidence),
        proposed_status=proposed_status,
        enrichment_payload=enrichment,
        report=report,
    )


def validate_rank_order(snapshot: Snapshot, rank_order: Iterable[str]) -> list[str]:
    order = list(rank_order)
    expected = [row.row_key for row in snapshot.rows]
    if len(order) != len(expected) or set(order) != set(expected):
        raise CurationError(
            "rank_order must be an exact permutation of snapshot row_keys"
        )
    if len(order) != len(set(order)):
        raise CurationError("rank_order must not duplicate a row_key")
    return order


def rank_rows(
    snapshot: Snapshot,
    verdicts: Iterable[RowVerdict],
    *,
    priorities: Mapping[str, int] | None = None,
    composite_scores: Mapping[str, float] | None = None,
    rank_refs: Iterable[str] = (),
) -> list[str]:
    """Rank every snapshot row, retaining position for missing scores and priorities."""
    verdict_by_key = {verdict.row_key: verdict for verdict in verdicts}
    if set(verdict_by_key) != {row.row_key for row in snapshot.rows}:
        raise CurationError("verdicts must cover every snapshot row exactly once")
    priorities = priorities or {}
    composite_scores = composite_scores or {}
    indexed = list(enumerate(snapshot.rows))

    def ranking(item: tuple[int, TaskRecord]) -> tuple[int, float, int]:
        index, row = item
        verdict = verdict_by_key[row.row_key]
        priority = priorities.get(verdict.bead_hash or "", 4)
        if not isinstance(priority, int) or not 0 <= priority <= 3:
            priority = 4
        # The composite is a value score (higher ranks first, as in
        # bd_curation_metadata's canonical order); an unscored row sorts last.
        score = composite_scores.get(verdict.bead_hash or "")
        if not isinstance(score, (int, float)):
            return priority, float("inf"), index
        return priority, -float(score), index

    base = [row.row_key for _, row in sorted(indexed, key=ranking)]
    pins: list[str] = []
    for ref in rank_refs:
        matches = [
            row.row_key
            for row in snapshot.rows
            if row.row_key == ref or verdict_by_key[row.row_key].display_id == ref
        ]
        if len(matches) != 1:
            raise CurationError(
                f"rank reference {ref!r} resolves to {len(matches)} rows"
            )
        if matches[0] not in pins:
            pins.append(matches[0])
    return validate_rank_order(
        snapshot, [*pins, *(key for key in base if key not in pins)]
    )


def build_plan(
    snapshot: Snapshot,
    verdicts: Iterable[RowVerdict],
    *,
    priorities: Mapping[str, int] | None = None,
    composite_scores: Mapping[str, float] | None = None,
    rank_refs: Iterable[str] = (),
    beads_results: Iterable[Mapping[str, Any]] = (),
    evidence_agent: str = "driver",
    requested_operation: str | None = None,
) -> CurationPlan:
    """Build a complete, inert plan.  It never creates or populates task rows."""
    if requested_operation in {"merge", "consolidate", "recreate"}:
        raise CurationError(f"status rung rejects {requested_operation}")
    verdict_list = list(verdicts)
    rank_order = rank_rows(
        snapshot,
        verdict_list,
        priorities=priorities,
        composite_scores=composite_scores,
        rank_refs=rank_refs,
    )
    display_ids = {verdict.display_id for verdict in verdict_list if verdict.display_id}
    unmirrored = [
        dict(issue)
        for issue in beads_results
        if str(issue.get("display_id", issue.get("id", ""))) not in display_ids
    ]
    changes = [
        ChangeLogEntry(
            row_key=verdict.row_key,
            old_status=verdict.row_status,
            new_status=verdict.proposed_status,
            evidence=verdict.evidence,
        )
        for verdict in verdict_list
        if verdict.proposed_status is not None
        and verdict.verdict != "decision-needed"
        and verdict.proposed_status != verdict.row_status
    ]
    return CurationPlan(
        schema_version=1,
        snapshot=snapshot,
        verdicts=verdict_list,
        rank_order=rank_order,
        priority_writes={},
        requires_destructive=False,
        change_log=changes,
        unmirrored_issues=unmirrored,
        requires_codex_spot_check=evidence_agent == "codex" and bool(changes),
    )


def recovery_create_keys(
    snapshot: Snapshot, classifications: Mapping[str, str]
) -> list[str]:
    """Return the sole sanctioned create set: snapshotted, provably-missing rows."""
    snapshot_keys = {row.row_key for row in snapshot.rows}
    unknown = set(classifications) - snapshot_keys
    if unknown:
        raise CurationError("recovery cannot create a row absent from its snapshot")
    return [
        row.row_key
        for row in snapshot.rows
        if classifications.get(row.row_key) == "provably-missing"
    ]


@dataclass(frozen=True)
class ReconciliationReport:
    """Live-state recovery result; ``mapping`` is safe to rewire only when complete."""

    classifications: dict[str, str]
    mapping: dict[str, str]
    foreign_ids: list[str]
    errors: list[str]
    created_ids: dict[str, str]

    @property
    def complete(self) -> bool:
        return not self.errors and len(self.mapping) == len(self.classifications)


@dataclass(frozen=True)
class ReconciliationPlan:
    """Inert recovery work for the task-tool-owning driving session."""

    report: ReconciliationReport
    operations: list[dict[str, Any]]
    edge_updates: list[dict[str, Any]]


def plan_reconciliation(
    snapshot: Snapshot,
    *,
    list_live: Callable[[], Iterable[Mapping[str, Any]]],
    get_live: Callable[[str], Mapping[str, Any]],
) -> ReconciliationPlan:
    """Observe live state and return, but never execute, recovery operations."""
    originals = {row.cc_id: row for row in snapshot.rows}
    expected = {row.row_key: row for row in snapshot.rows}
    live = _live_rows(snapshot.namespace, list_live, get_live)
    recreated: dict[str, list[TaskRecord]] = {}
    survivors: dict[str, TaskRecord] = {}
    foreign: list[str] = []
    for live_row in live:
        metadata = live_row.metadata
        if metadata.get("curate_run_id") == snapshot.run_id:
            fingerprint = metadata.get("curate_row_fingerprint")
            if isinstance(fingerprint, str):
                recreated.setdefault(fingerprint, []).append(live_row)
            else:
                foreign.append(live_row.cc_id)
        elif live_row.cc_id in originals:
            survivors[originals[live_row.cc_id].row_key] = live_row
        else:
            foreign.append(live_row.cc_id)

    classifications: dict[str, str] = {}
    mapping: dict[str, str] = {}
    errors: list[str] = []
    operations: list[dict[str, Any]] = []
    for key, original in expected.items():
        copies = recreated.get(original.fingerprint, [])
        survivor = survivors.get(key)
        if copies and survivor:
            classifications[key] = "duplicated"
            if len(copies) != 1:
                errors.append(f"{key} resolves to {len(copies) + 1} live rows")
            elif survivor.fields != original.fields:
                errors.append(
                    f"{key} survivor content changed; refusing duplicate delete"
                )
            else:
                mapping[key] = copies[0].cc_id
                operations.append(
                    {"op": "delete", "row_key": key, "cc_id": survivor.cc_id}
                )
        elif copies:
            classifications[key] = "present"
            if len(copies) == 1:
                mapping[key] = copies[0].cc_id
            else:
                errors.append(f"{key} resolves to {len(copies)} recreated live rows")
        elif survivor:
            classifications[key] = "original-intact"
            if survivor.fields != original.fields:
                errors.append(
                    f"{key} survivor content changed; refusing stale recovery"
                )
            else:
                mapping[key] = survivor.cc_id
        else:
            classifications[key] = "provably-missing"
            operations.append(
                {
                    "op": "create",
                    "row_key": key,
                    "fingerprint": original.fingerprint,
                    "payload": _recreate_payload(original, snapshot.run_id),
                }
            )

    report = ReconciliationReport(
        classifications=classifications,
        mapping={} if errors else mapping,
        foreign_ids=sorted(foreign),
        errors=errors,
        created_ids={},
    )
    edge_updates: list[dict[str, Any]] = []
    if not errors and not operations and len(mapping) == len(expected):
        id_to_key = {row.cc_id: row.row_key for row in snapshot.rows}
        for row in snapshot.rows:
            edge_updates.append(
                {
                    "op": "update",
                    "row_key": row.row_key,
                    "cc_id": mapping[row.row_key],
                    "blocks": [
                        mapping[id_to_key[item]] if item in id_to_key else item
                        for item in row.blocks
                    ],
                    "blocked_by": [
                        mapping[id_to_key[item]] if item in id_to_key else item
                        for item in row.blocked_by
                    ],
                }
            )
    return ReconciliationPlan(report, operations, edge_updates)


def _validate_recovery_artifact(
    path: Path, *, kind: Literal["directory", "file"], label: str
) -> os.stat_result:
    """Fail closed unless a recovery input is private, owned, and the expected type."""
    try:
        details = os.lstat(path)
    except OSError as exc:
        raise ReorderError(f"recovery {label} cannot be inspected: {exc}") from exc
    expected_mode = 0o700 if kind == "directory" else 0o600
    expected_type = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    if not expected_type(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise ReorderError(f"recovery {label} is unsafe: {path}")
    try:
        current_uid = os.getuid()
    except AttributeError as exc:  # pragma: no cover - recovery is POSIX-only.
        raise ReorderError("recovery requires uid ownership validation") from exc
    if details.st_uid != current_uid:
        raise ReorderError(f"recovery {label} has the wrong owner: {path}")
    if stat.S_IMODE(details.st_mode) != expected_mode:
        raise ReorderError(
            f"recovery {label} has unsafe mode {stat.S_IMODE(details.st_mode):#o}: {path}"
        )
    return details


def _validated_snapshot_dir(snapshot_dir: Path, backup_root: Path) -> Path:
    """Return one private complete backup path after containment and no-follow checks."""
    _reject_symlink_components(snapshot_dir, backup_root)
    _assert_contained(snapshot_dir, backup_root)
    _validate_recovery_artifact(
        snapshot_dir, kind="directory", label="snapshot directory"
    )
    return snapshot_dir


class OperationJournal:
    """Append-only, fsync-per-record journal for destructive task-store operations."""

    def __init__(self, snapshot_dir: Path, run_id: str, *, backup_root: Path):
        snapshot_dir = _validated_snapshot_dir(snapshot_dir, backup_root)
        snapshot = load_persisted_snapshot(snapshot_dir, backup_root=backup_root)
        if snapshot.run_id != run_id:
            raise ReorderError("operation journal run id disagrees with snapshot")
        self.path = snapshot_dir / "journal.jsonl"
        self.run_id = run_id
        self._seq = 0
        if self.path.exists():
            _validate_recovery_artifact(
                self.path, kind="file", label="operation journal"
            )
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    self._seq = max(self._seq, int(json.loads(line)["seq"]))
            except (
                OSError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                raise ReorderError(f"operation journal cannot be read: {exc}") from exc

    def arm(self) -> None:
        """Prove the journal is durable before a caller issues its first delete."""
        self._append({"op": "header", "run_id": self.run_id, "phase": "header"})

    def intent(
        self, op: Literal["delete", "create", "update"], row_key: str, **extra: Any
    ) -> None:
        self._append({"op": op, "row_key": row_key, "phase": "intent", **extra})

    def outcome(
        self,
        op: Literal["delete", "create", "update"],
        row_key: str,
        result: str,
        **extra: Any,
    ) -> None:
        self._append(
            {
                "op": op,
                "row_key": row_key,
                "phase": "outcome",
                "result": result,
                **extra,
            }
        )

    def _append(self, record: dict[str, Any]) -> None:
        self._seq += 1
        payload = {
            "seq": self._seq,
            **record,
            "at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        try:
            try:
                descriptor = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                _validate_recovery_artifact(
                    self.path, kind="file", label="operation journal"
                )
                descriptor = os.open(self.path, flags)
                created = False
            try:
                details = os.fstat(descriptor)
                expected_uid = os.getuid()
                if not stat.S_ISREG(details.st_mode) or details.st_uid != expected_uid:
                    raise ReorderError("operation journal is unsafe after open")
                if created:
                    os.fchmod(descriptor, 0o600)
                    details = os.fstat(descriptor)
                if stat.S_IMODE(details.st_mode) != 0o600:
                    raise ReorderError("operation journal has unsafe mode after open")
            except (AttributeError, ReorderError) as exc:
                os.close(descriptor)
                if isinstance(exc, AttributeError):  # pragma: no cover - POSIX-only.
                    raise ReorderError(
                        "recovery requires uid ownership validation"
                    ) from exc
                raise
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ReorderError(f"operation journal is not writable: {exc}") from exc


def _live_rows(
    namespace: str,
    list_live: Callable[[], Iterable[Mapping[str, Any]]],
    get_live: Callable[[str], Mapping[str, Any]],
) -> list[TaskRecord]:
    listed = [str(item["id"]) for item in list_live()]
    if len(listed) != len(set(listed)):
        raise ReorderError("live TaskList contains duplicate row ids")
    rows = [_task_record(namespace, get_live(cc_id)) for cc_id in listed]
    if {row.cc_id for row in rows} != set(listed):
        raise ReorderError("live TaskGet disagrees with TaskList row set")
    return rows


def recheck_row_set(
    snapshot: Snapshot,
    list_live: Callable[[], Iterable[Mapping[str, Any]]],
    get_live: Callable[[str], Mapping[str, Any]],
) -> None:
    """Reject an id or content race immediately before the destructive sweep."""
    live_ids = [str(item["id"]) for item in list_live()]
    if len(live_ids) != len(set(live_ids)) or set(live_ids) != {
        row.cc_id for row in snapshot.rows
    }:
        raise ReorderError("namespace changed since snapshot; no delete issued")
    for row in snapshot.rows:
        live = _task_record(snapshot.namespace, get_live(row.cc_id))
        if live.fingerprint != row.fingerprint or live.fields != row.fields:
            raise ReorderError(
                "namespace content changed since snapshot; no delete issued"
            )


def _recreate_payload(row: TaskRecord, run_id: str) -> dict[str, Any]:
    payload = dict(row.fields)
    payload.pop("id", None)
    metadata = dict(row.metadata)
    metadata.update(
        {
            "curate_run_id": run_id,
            "curate_row_fingerprint": row.fingerprint,
        }
    )
    payload["metadata"] = metadata
    return payload


def reconcile(
    snapshot: Snapshot,
    *,
    list_live: Callable[[], Iterable[Mapping[str, Any]]],
    get_live: Callable[[str], Mapping[str, Any]],
    create_live: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    delete_live: Callable[[str], None],
    journal: OperationJournal,
) -> ReconciliationReport:
    """Converge live state without ever creating a row not proved absent.

    The live store, especially the self-identifying metadata on recreated rows, is
    authoritative.  Journal records establish durability around calls but are never
    used to infer that a create did or did not land.
    """
    originals = {row.cc_id: row for row in snapshot.rows}
    expected = {row.row_key: row for row in snapshot.rows}
    live = _live_rows(snapshot.namespace, list_live, get_live)
    recreated: dict[str, list[TaskRecord]] = {}
    survivors: dict[str, TaskRecord] = {}
    foreign: list[str] = []
    for live_row in live:
        metadata = live_row.metadata
        if metadata.get("curate_run_id") == snapshot.run_id:
            fingerprint = metadata.get("curate_row_fingerprint")
            if isinstance(fingerprint, str):
                recreated.setdefault(fingerprint, []).append(live_row)
            else:
                foreign.append(live_row.cc_id)
        elif live_row.cc_id in originals:
            survivors[originals[live_row.cc_id].row_key] = live_row
        else:
            foreign.append(live_row.cc_id)

    classifications: dict[str, str] = {}
    mapping: dict[str, str] = {}
    errors: list[str] = []
    created_ids: dict[str, str] = {}
    missing: list[TaskRecord] = []
    for key, original in expected.items():
        copies = recreated.get(original.fingerprint, [])
        survivor = survivors.get(key)
        if copies and survivor:
            classifications[key] = "duplicated"
            if len(copies) != 1:
                errors.append(f"{key} resolves to {len(copies) + 1} live rows")
            elif _record_fingerprint(key, survivor.fields) != original.fingerprint:
                errors.append(
                    f"{key} survivor content changed; refusing duplicate delete"
                )
            else:
                try:
                    journal.intent("delete", key, cc_id=survivor.cc_id)
                    delete_live(survivor.cc_id)
                    journal.outcome("delete", key, "ok", cc_id=survivor.cc_id)
                    mapping[key] = copies[0].cc_id
                except (OSError, ReorderError) as exc:
                    errors.append(f"cannot remove duplicate survivor {key}: {exc}")
        elif copies:
            classifications[key] = "present"
            if len(copies) == 1:
                mapping[key] = copies[0].cc_id
            else:
                errors.append(f"{key} resolves to {len(copies)} recreated live rows")
        elif survivor:
            classifications[key] = "original-intact"
            mapping[key] = survivor.cc_id
        else:
            classifications[key] = "provably-missing"
            missing.append(original)

    for original in missing:
        try:
            journal.intent("create", original.row_key, fingerprint=original.fingerprint)
            created = create_live(_recreate_payload(original, snapshot.run_id))
            new_id = str(created["id"])
            journal.outcome("create", original.row_key, "ok", new_cc_id=new_id)
            created_ids[original.row_key] = new_id
            mapping[original.row_key] = new_id
        except (OSError, KeyError, ReorderError) as exc:
            try:
                journal.outcome("create", original.row_key, "error", error=str(exc))
            except ReorderError:
                pass
            errors.append(f"cannot recreate {original.row_key}: {exc}")

    # A failed create is not guessed past: the next recovery observes whether it landed.
    if errors:
        mapping = {}
    elif created_ids:
        # TaskCreate's return value is useful for the report, but never establishes the
        # rewire map.  Re-read the store so the map is proved from live state too.
        verified = reconcile(
            snapshot,
            list_live=list_live,
            get_live=get_live,
            create_live=create_live,
            delete_live=delete_live,
            journal=journal,
        )
        mapping = verified.mapping
        foreign = sorted({*foreign, *verified.foreign_ids})
        errors.extend(verified.errors)
    return ReconciliationReport(
        classifications=classifications,
        mapping=mapping,
        foreign_ids=sorted(foreign),
        errors=errors,
        created_ids=created_ids,
    )


def rewire_dependencies(
    snapshot: Snapshot,
    mapping: Mapping[str, str],
    *,
    set_edges: Callable[[str, list[str], list[str]], None],
) -> None:
    """Set, rather than append, dependency targets after proving a unique map."""
    expected = {row.row_key for row in snapshot.rows}
    if set(mapping) != expected or len(set(mapping.values())) != len(mapping):
        raise ReorderError(
            "ambiguous row_key-to-live-id map; refusing dependency rewiring"
        )
    id_to_key = {row.cc_id: row.row_key for row in snapshot.rows}
    for row in snapshot.rows:
        blocks = [
            mapping[id_to_key[item]] if item in id_to_key else item
            for item in row.blocks
        ]
        blocked_by = [
            mapping[id_to_key[item]] if item in id_to_key else item
            for item in row.blocked_by
        ]
        set_edges(mapping[row.row_key], blocks, blocked_by)


def _fsync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_captured_file(destination: Path, content: bytes) -> None:
    """Durably write an already-captured task observation without a second read."""
    with destination.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(destination, 0o600)


def _snapshot_manifest(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "schema_version": snapshot.schema_version,
        "run_id": snapshot.run_id,
        "taken_at": snapshot.taken_at,
        "namespace": snapshot.namespace,
        "client_version": snapshot.client_version,
        "manifest_digest": snapshot.manifest_digest,
        "rows": [
            {"row_key": row.row_key, "fields": row.fields} for row in snapshot.rows
        ],
    }


def load_persisted_snapshot(snapshot_dir: Path, *, backup_root: Path) -> Snapshot:
    """Load a COMPLETE snapshot's semantic manifest and immutable file capture."""
    snapshot_dir = _validated_snapshot_dir(snapshot_dir, backup_root)
    complete = snapshot_dir / "COMPLETE"
    manifest_path = snapshot_dir / "manifest.json"
    captured = snapshot_dir / "namespace"
    _validate_recovery_artifact(complete, kind="file", label="COMPLETE marker")
    _validate_recovery_artifact(manifest_path, kind="file", label="manifest")
    _validate_recovery_artifact(captured, kind="directory", label="task capture")
    try:
        marker = json.loads(complete.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        namespace = validate_namespace(manifest["namespace"])
        rows = []
        for item in manifest["rows"]:
            row = _task_record(namespace, item["fields"])
            if row.row_key != item["row_key"]:
                raise SnapshotError(
                    "persisted manifest row key disagrees with its task"
                )
            rows.append(row)
        if marker.get("run_id") != manifest["run_id"]:
            raise SnapshotError("COMPLETE marker run id disagrees with manifest")
        digest_rows = [
            {
                "row_key": row.row_key,
                "fingerprint": row.fingerprint,
                "fields": row.fields,
            }
            for row in rows
        ]
        if marker.get("manifest_digest") != _digest(digest_rows) or manifest.get(
            "manifest_digest"
        ) != _digest(digest_rows):
            raise SnapshotError("persisted manifest digest does not verify")
        file_capture = {}
        for child in captured.iterdir():
            _validate_recovery_artifact(
                child, kind="file", label="captured task observation"
            )
            file_capture[child.name] = child.read_bytes()
    except (KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
        raise ReorderError(
            f"cannot read persisted snapshot {snapshot_dir}: {exc}"
        ) from exc
    return Snapshot(
        schema_version=int(manifest["schema_version"]),
        run_id=str(manifest["run_id"]),
        taken_at=str(manifest["taken_at"]),
        namespace=namespace,
        client_version=str(manifest["client_version"]),
        rows=rows,
        file_copy_dir=str(captured),
        persisted=True,
        manifest_digest=str(manifest["manifest_digest"]),
        file_capture=file_capture,
    )


def rehearse_restore(snapshot_dir: Path, *, backup_root: Path) -> Snapshot:
    """Prove every persisted manifest row can be reconstructed before deletion."""
    snapshot_dir = _validated_snapshot_dir(snapshot_dir, backup_root)
    snapshot = load_persisted_snapshot(snapshot_dir, backup_root=backup_root)
    captured = snapshot_dir / "namespace"
    _validate_recovery_artifact(captured, kind="directory", label="task capture")
    captured_ids = {
        path.stem for path in captured.glob("*.json") if path.stem.isdigit()
    }
    if captured_ids != {row.cc_id for row in snapshot.rows}:
        raise ReorderError("restore rehearsal row set disagrees with manifest")
    for row in snapshot.rows:
        captured_file = captured / f"{row.cc_id}.json"
        _validate_recovery_artifact(
            captured_file, kind="file", label="captured task observation"
        )
        try:
            task = json.loads(captured_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ReorderError(
                f"restore rehearsal cannot read {row.row_key}: {exc}"
            ) from exc
        restored = _task_record(snapshot.namespace, task)
        if restored.fields != row.fields or restored.fingerprint != row.fingerprint:
            raise ReorderError(f"restore rehearsal cannot reconstruct {row.row_key}")
    return snapshot


def persist_snapshot(
    snapshot: Snapshot,
    *,
    tasks_root: Path | None = None,
    backup_root: Path,
    timestamp: str | None = None,
) -> Path:
    """Persist the snapshot's exact captured bytes as a durable recovery artifact."""
    # Recovery artifacts are derived from file_capture, not a second source read.
    del tasks_root
    namespace = validate_namespace(snapshot.namespace)
    namespace_backup_root = backup_root / namespace
    _reject_symlink_components(namespace_backup_root, backup_root)
    _assert_contained(namespace_backup_root, backup_root)
    namespace_backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(namespace_backup_root, 0o700)
    stamp = timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    final = namespace_backup_root / stamp
    partial = namespace_backup_root / f"{stamp}.partial"
    if final.exists() or partial.exists():
        raise CurationError(f"backup destination already exists: {final}")
    partial.mkdir(mode=0o700)
    os.chmod(partial, 0o700)
    copied = partial / "namespace"
    copied.mkdir(mode=0o700)
    os.chmod(copied, 0o700)
    expected_capture = {f"{row.cc_id}.json" for row in snapshot.rows}
    if set(snapshot.file_capture) != expected_capture:
        raise SnapshotError("semantic manifest and captured observations disagree")
    for row in snapshot.rows:
        name = f"{row.cc_id}.json"
        content = snapshot.file_capture[name]
        try:
            captured_row = _task_record(snapshot.namespace, json.loads(content))
        except (TypeError, json.JSONDecodeError) as exc:
            raise SnapshotError(
                f"captured observation is invalid for {row.row_key}"
            ) from exc
        if (
            captured_row.fields != row.fields
            or captured_row.fingerprint != row.fingerprint
        ):
            raise SnapshotError("semantic manifest and captured observations disagree")
        _write_captured_file(copied / name, content)
    manifest = partial / "manifest.json"
    _write_captured_file(
        manifest,
        (json.dumps(_snapshot_manifest(snapshot), sort_keys=True) + "\n").encode(),
    )
    _fsync_path(manifest)
    _fsync_path(copied)
    _fsync_path(partial)
    os.replace(partial, final)
    _fsync_path(namespace_backup_root)
    complete = final / "COMPLETE"
    _write_captured_file(
        complete,
        json.dumps(
            {"manifest_digest": snapshot.manifest_digest, "run_id": snapshot.run_id}
        ).encode(),
    )
    _fsync_path(complete)
    _fsync_path(final)
    _fsync_path(namespace_backup_root)
    recover = final / "RECOVER.md"
    curate_script = (
        Path(__file__).resolve().parent.parent
        / "skills"
        / "curate-task-panel"
        / "scripts"
        / "curate.py"
    )
    command = (
        f"Run: uv run --script {shlex.quote(str(curate_script))} "
        f"--recover {shlex.quote(str(final))} "
        "--input <fresh-live-json> "
        f"--backup-root {shlex.quote(str(backup_root))} "
        "--apply plan\n"
    )
    _write_captured_file(recover, command.encode())
    _fsync_path(recover)
    _fsync_path(final)
    _fsync_path(namespace_backup_root)
    return final


def list_incomplete_backups(namespace: str, backup_root: Path) -> list[Path]:
    """List, but never accept, incomplete snapshot directories."""
    namespace = validate_namespace(namespace)
    root = backup_root / namespace
    _assert_contained(root, backup_root)
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and not (path / "COMPLETE").is_file()
    )


def prune_backups(namespace: str, backup_root: Path, keep: int) -> None:
    """Retain newest complete backups only; failures are surfaced to the caller."""
    if keep < 0:
        raise CurationError("keep must be non-negative")
    namespace = validate_namespace(namespace)
    root = backup_root / namespace
    _assert_contained(root, backup_root)
    if not root.exists():
        return
    complete = sorted(path for path in root.iterdir() if (path / "COMPLETE").is_file())
    for path in complete[:-keep] if keep else complete:
        shutil.rmtree(path)
