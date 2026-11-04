#!/usr/bin/env python3
# ruff: noqa: E402
"""Append-only state and folds for the prompt audit-loop engine.

The JSONL file is authoritative; Markdown is only a projection. Version 1 has
no hash chain, lease epoch, lanes, generations, or per-member digests. Git is
the durable per-round history.
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
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.audit_lock import exclusive, fsync_directory

_ASK_FIDELITY_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "_shared"
    / "ask-fidelity"
    / "ask_fidelity.py"
)
_ASK_SPEC = importlib.util.spec_from_file_location(
    "audit_receipt_ask_fidelity", _ASK_FIDELITY_PATH
)
if _ASK_SPEC is None or _ASK_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("ask-fidelity validator is unavailable")
ask_fidelity = importlib.util.module_from_spec(_ASK_SPEC)
sys.modules[_ASK_SPEC.name] = ask_fidelity
_ASK_SPEC.loader.exec_module(ask_fidelity)

SCHEMA_VERSION = 1
STAGES = {"doc-audit", "impl-audit"}
SEVERITIES = {"critical", "major", "minor"}
DISPOSITIONS = {"open-blocking", "deferred", "fixed", "accepted-risk"}
ROUND_KINDS = {"full", "verify"}
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$")
TERMINALS = {
    "audit-converged",
    "audit-stabilized",
    "halted-recurrence",
    "halted-cap",
    "superseded",
}
COMMON_FIELDS = {
    "schema_version",
    "kind",
    "round",
    "ts",
    "target",
    "stage",
    "spec_epoch",
    "writer",
}
EVENT_PAYLOADS: dict[str, tuple[set[str], set[str]]] = {
    "loop-start": ({"budget", "tier_source", "carrier"}, {"takeover_pid"}),
    "audit": (
        {
            "scope",
            "findings",
            "new_by_severity",
            "major_nonblocking_justifications",
            "source_event_id",
        },
        set(),
    ),
    "fix": ({"applied", "action"}, {"source_commit"}),
    "halt": ({"halt", "finding_keys", "reason"}, set()),
    "terminal": ({"terminal", "reason"}, set()),
    "doc-review-resume": (
        {"finding_keys", "reviewer", "rationale", "reviewed_digest"},
        set(),
    ),
    "resume-after-fix": (
        {"finding_keys", "reviewer", "rationale", "reviewed_digest"},
        set(),
    ),
    "intent-alignment": ({"payload"}, set()),
    "override": ({"payload", "actor"}, set()),
    "spec-freeze": ({"register"}, {"human_ratification"}),
}

REGION_START = '<!-- doc:region name="loop_receipt" kind="generated" -->'
REGION_END = '<!-- /doc:region name="loop_receipt" -->'


class ReceiptError(ValueError):
    """The state file is malformed, legacy, or internally inconsistent."""


@dataclass(frozen=True)
class Fold:
    audit_iterations: int
    fix_passes: int
    backlog: tuple[str, ...]
    counts: dict[str, int]
    stable: bool
    converged: bool
    promotion_ready: bool
    terminal: str | None = None


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def event_identity(event: dict[str, Any]) -> str:
    return "evt:sha256:" + hashlib.sha256(canonical(event)).hexdigest()


def receipt_path(audit_path: Path) -> Path:
    return audit_path.with_suffix(".receipt.jsonl")


def lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


@contextmanager
def locked(path: Path) -> Iterable[None]:
    lock = lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle, exclusive(handle):
        yield


def _closed(value: Any, required: set[str], optional: set[str], name: str) -> None:
    if not isinstance(value, dict):
        raise ReceiptError(f"{name} must be an object")
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing or extra:
        raise ReceiptError(
            f"{name} has closed-schema violation "
            f"(missing={sorted(missing)}, extra={sorted(extra)})"
        )


def _nonempty(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ReceiptError(f"{name} must be a non-empty string")


def _digest(value: Any, name: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ReceiptError(f"{name} must be sha256:<64 lowercase hex>")


def _optional_identifier(value: Any, name: str) -> None:
    if value is not None and (
        not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value)
    ):
        raise ReceiptError(f"{name} must be a valid identifier")


def _validate_ratification(summary: Any, register_digest: str) -> None:
    _closed(
        summary,
        {"held_item", "held_item_blob", "chosen", "answered_at", "register_digest"},
        set(),
        "spec-freeze human_ratification",
    )
    path = summary["held_item"]
    _nonempty(path, "human_ratification held_item")
    relative = PurePosixPath(path)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or "\\" in path
        or relative.as_posix() != path
        or path == "."
    ):
        raise ReceiptError("human_ratification held_item must be repo-relative")
    blob = summary["held_item_blob"]
    if not isinstance(blob, str) or not re.fullmatch(
        r"(?:[0-9a-f]{40}|[0-9a-f]{64})", blob
    ):
        raise ReceiptError("human_ratification held_item_blob must be a Git blob id")
    if summary["chosen"] != "(a)":
        raise ReceiptError("human_ratification chosen must be (a)")
    timestamp = summary["answered_at"]
    try:
        if not isinstance(timestamp, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", timestamp
        ):
            raise ValueError
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ReceiptError(
            "human_ratification answered_at must be a real UTC timestamp"
        ) from exc
    _digest(summary["register_digest"], "human_ratification register_digest")
    if summary["register_digest"] != register_digest:
        raise ReceiptError(
            "human_ratification register_digest must match spec-freeze register"
        )


def _validate_finding(finding: Any) -> None:
    _closed(
        finding,
        {
            "finding_key",
            "id",
            "submitted_severity",
            "severity",
            "fingerprint",
            "cluster",
            "reachable_behavior",
            "disposition",
        },
        set(),
        "finding",
    )
    for field in ("finding_key", "id", "fingerprint", "reachable_behavior"):
        _nonempty(finding[field], f"finding {field}")
    if finding["submitted_severity"] not in SEVERITIES:
        raise ReceiptError("finding submitted_severity has unknown value")
    if finding["severity"] not in SEVERITIES:
        raise ReceiptError("finding severity has unknown value")
    if finding["disposition"] not in DISPOSITIONS:
        raise ReceiptError("finding disposition has unknown value")
    _closed(
        finding["cluster"],
        {"spec_version", "invariant_id", "subsystem", "violation_kind"},
        set(),
        "finding cluster",
    )
    for field in ("spec_version", "invariant_id"):
        _optional_identifier(finding["cluster"][field], f"finding cluster {field}")
    for field in ("subsystem", "violation_kind"):
        _nonempty(finding["cluster"][field], f"finding cluster {field}")


def _validate_payload(kind: str, payload: Any) -> None:  # noqa: C901, PLR0912
    """Validate one event-specific payload against the canonical schema."""
    if kind not in EVENT_PAYLOADS:
        raise ReceiptError(f"unknown event kind: {kind}")
    required, optional = EVENT_PAYLOADS[kind]
    _closed(payload, required, optional, f"{kind} payload")
    if kind == "loop-start":
        budget = payload["budget"]
        _closed(
            budget,
            {
                "tier",
                "audit_rounds",
                "fix_passes",
                "fingerprint_recurrence",
                "stagnation_rounds",
            },
            set(),
            "budget",
        )
        if payload["tier_source"] not in {"embedded", "repo"}:
            raise ReceiptError("tier_source must be embedded or repo")
        for field in (
            "audit_rounds",
            "fix_passes",
            "fingerprint_recurrence",
            "stagnation_rounds",
        ):
            value = budget[field]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ReceiptError(f"budget {field} must be a positive integer")
        carrier = payload["carrier"]
        _closed(carrier, {"digest", "tier"}, set(), "carrier")
        _digest(carrier["digest"], "carrier digest")
        if carrier["tier"] not in {2, 3}:
            raise ReceiptError("carrier tier must be 2 or 3")
    elif kind == "audit":
        scope = payload["scope"]
        _closed(
            scope,
            {"kind", "backlog", "diff_since", "diff_summary"},
            set(),
            "audit scope",
        )
        if scope["kind"] not in ROUND_KINDS:
            raise ReceiptError("audit scope kind must be full or verify")
        if not isinstance(scope["backlog"], list) or not all(
            isinstance(item, str) and item for item in scope["backlog"]
        ):
            raise ReceiptError("audit scope backlog must be a string list")
        if scope["diff_since"] is not None:
            _digest(scope["diff_since"], "audit scope diff_since")
        if not isinstance(scope["diff_summary"], dict):
            raise ReceiptError("audit scope diff_summary must be an object")
        if not isinstance(payload["findings"], list):
            raise ReceiptError("audit findings must be a list")
        for finding in payload["findings"]:
            _validate_finding(finding)
        counts = payload["new_by_severity"]
        _closed(counts, SEVERITIES, set(), "new_by_severity")
        if not all(
            isinstance(counts[severity], int)
            and not isinstance(counts[severity], bool)
            and counts[severity] >= 0
            for severity in SEVERITIES
        ):
            raise ReceiptError("new_by_severity values must be non-negative integers")
        justifications = payload["major_nonblocking_justifications"]
        if not isinstance(justifications, dict) or not all(
            isinstance(key, str) and key and isinstance(value, str) and value.strip()
            for key, value in justifications.items()
        ):
            raise ReceiptError("major_nonblocking_justifications must be a string map")
        _nonempty(payload["source_event_id"], "audit source_event_id")
    elif kind == "fix":
        if not isinstance(payload["applied"], list) or not all(
            isinstance(item, str) and item for item in payload["applied"]
        ):
            raise ReceiptError("fix applied must be a string list")
        action = payload["action"]
        _closed(action, {"class", "invariant_consumer"}, set(), "fix action")
        if action["class"] not in {"semantic", "validation", "administrative"}:
            raise ReceiptError("fix action class has unknown value")
        if action["class"] == "administrative" and not action["invariant_consumer"]:
            raise ReceiptError("administrative fix requires an invariant_consumer")
    elif kind == "halt":
        if payload["halt"] not in {"fingerprint", "cluster", "streak", "cap"}:
            raise ReceiptError("halt has unknown value")
    elif kind == "terminal":
        if payload["terminal"] not in TERMINALS:
            raise ReceiptError("terminal has unknown value")
    elif kind in {"doc-review-resume", "resume-after-fix"}:
        finding_keys = payload["finding_keys"]
        if (
            not isinstance(finding_keys, list)
            or not finding_keys
            or not all(isinstance(key, str) and key for key in finding_keys)
            or len(finding_keys) != len(set(finding_keys))
        ):
            raise ReceiptError(
                f"{kind} finding_keys must be a non-empty unique string list"
            )
        _nonempty(payload["reviewer"], f"{kind} reviewer")
        _nonempty(payload["rationale"], f"{kind} rationale")
        _digest(payload["reviewed_digest"], f"{kind} reviewed_digest")
    elif kind == "intent-alignment":
        try:
            ask_fidelity.validate_alignment_receipt(payload["payload"])
        except (TypeError, ValueError) as exc:
            raise ReceiptError(f"invalid intent_alignment: {exc}") from exc
    elif kind == "override":
        override = payload["payload"]
        _closed(
            override,
            {
                "finding_keys",
                "human_actor",
                "residual_risk_owner",
                "justification",
                "expires_at",
                "target_digest",
                "critical_compensating_control",
            },
            set(),
            "override",
        )
        if not isinstance(override["finding_keys"], list):
            raise ReceiptError("override finding_keys must be a list")
        _closed(payload["actor"], {"family", "name"}, set(), "override actor")
    elif kind == "spec-freeze":
        register = payload["register"]
        _closed(register, {"path", "digest"}, set(), "spec-freeze register")
        _nonempty(register["path"], "spec-freeze register path")
        _digest(register["digest"], "spec-freeze register digest")
        if "human_ratification" in payload:
            _validate_ratification(payload["human_ratification"], register["digest"])


def validate_event(event: Any) -> None:
    if not isinstance(event, dict):
        raise ReceiptError("event must be an object")
    serialized = json.dumps(event, sort_keys=True)
    legacy = [
        field
        for field in (
            "event_type",
            "generation",
            "member_digest",
            "member_digests",
            "lane",
            "capability_plan",
            "alignment",
        )
        if f'"{field}"' in serialized
    ]
    if legacy:
        raise ReceiptError(
            "legacy receipt; start a new loop (legacy fields: "
            + ", ".join(legacy)
            + ")"
        )
    kind = event.get("kind")
    if kind not in EVENT_PAYLOADS:
        raise ReceiptError(f"unknown event kind: {kind}")
    required, optional = EVENT_PAYLOADS[kind]
    _closed(event, COMMON_FIELDS | required, optional, "event")
    if event["schema_version"] != SCHEMA_VERSION:
        raise ReceiptError("legacy receipt; start a new loop")
    if event["stage"] not in STAGES:
        raise ReceiptError("event stage has unknown value")
    if (
        not isinstance(event["round"], int)
        or isinstance(event["round"], bool)
        or event["round"] < 0
    ):
        raise ReceiptError("event round must be a non-negative integer")
    if (
        not isinstance(event["spec_epoch"], int)
        or isinstance(event["spec_epoch"], bool)
        or event["spec_epoch"] < 0
    ):
        raise ReceiptError("event spec_epoch must be a non-negative integer")
    _closed(event["target"], {"path", "digest"}, set(), "target")
    _nonempty(event["target"]["path"], "target path")
    _digest(event["target"]["digest"], "target digest")
    _nonempty(event["ts"], "event ts")
    if event["writer"] != "audit_loop.py":
        raise ReceiptError("event writer is not the audit-loop driver")
    payload = {key: event[key] for key in required | optional if key in event}
    _validate_payload(kind, payload)
    if (
        kind in {"doc-review-resume", "resume-after-fix"}
        and event["reviewed_digest"] != event["target"]["digest"]
    ):
        raise ReceiptError(f"{kind} reviewed_digest must match target digest")


def _validate_sequence(events: list[dict[str, Any]]) -> None:
    if not events:
        return
    if events[0]["kind"] != "loop-start" or events[0]["round"] != 0:
        raise ReceiptError("state must begin with loop-start at round 0")
    stage = events[0]["stage"]
    target_path = events[0]["target"]["path"]
    latest_audit_round = 0
    terminal_seen: str | None = None
    spec_epoch = 0
    freeze_epochs: set[int] = set()
    for index, event in enumerate(events, start=1):
        if event["stage"] != stage or event["target"]["path"] != target_path:
            raise ReceiptError(f"event line {index} changes the loop identity")
        if terminal_seen and event["kind"] not in {
            "spec-freeze",
            "doc-review-resume",
            "resume-after-fix",
            "intent-alignment",
            "audit",
            "terminal",
        }:
            raise ReceiptError(f"event line {index} appears after terminal")
        if event["kind"] == "spec-freeze":
            if terminal_seen is None:
                raise ReceiptError(f"event line {index} freezes a non-halted loop")
            if event["spec_epoch"] != spec_epoch + 1:
                raise ReceiptError(f"event line {index} has non-monotonic spec_epoch")
            if event["spec_epoch"] in freeze_epochs:
                raise ReceiptError(f"event line {index} reuses a spec_epoch")
            freeze_epochs.add(event["spec_epoch"])
            spec_epoch = event["spec_epoch"]
            terminal_seen = None
        elif event["kind"] == "doc-review-resume":
            if stage != "doc-audit":
                raise ReceiptError(f"event line {index} resumes a non-doc-audit loop")
            if terminal_seen != "halted-recurrence":
                raise ReceiptError(
                    f"event line {index} resumes without halted-recurrence"
                )
            preceding = events[index - 3] if index > 2 else None
            if preceding is None or preceding["kind"] != "halt":
                raise ReceiptError(
                    f"event line {index} has no recurrence halt to resume"
                )
            requested = set(event["finding_keys"])
            if not requested <= set(preceding["finding_keys"]):
                raise ReceiptError(
                    f"event line {index} resumes a finding outside the recurrence halt"
                )
            prior_events = events[: index - 1]
            if not requested <= set(fold(prior_events).backlog):
                raise ReceiptError(
                    f"event line {index} resumes a finding outside the current backlog"
                )
            latest_digests: dict[str, str] = {}
            for prior in prior_events:
                if prior["kind"] == "audit":
                    for finding in prior["findings"]:
                        latest_digests[finding["finding_key"]] = prior["target"][
                            "digest"
                        ]
            if any(
                latest_digests.get(key) != event["target"]["digest"]
                for key in requested
            ):
                raise ReceiptError(
                    f"event line {index} resumes a finding with a stale target digest"
                )
            terminal_seen = None
        elif event["kind"] == "resume-after-fix":
            if stage != "doc-audit":
                raise ReceiptError(f"event line {index} resumes a non-doc-audit loop")
            if terminal_seen != "halted-recurrence":
                raise ReceiptError(
                    f"event line {index} resumes without halted-recurrence"
                )
            preceding = events[index - 3] if index > 2 else None
            if preceding is None or preceding["kind"] != "halt":
                raise ReceiptError(
                    f"event line {index} has no recurrence halt to resume"
                )
            requested = set(event["finding_keys"])
            if not requested <= set(preceding["finding_keys"]):
                raise ReceiptError(
                    f"event line {index} resumes a finding outside the recurrence halt"
                )
            prior_events = events[: index - 1]
            if not requested <= set(fold(prior_events).backlog):
                raise ReceiptError(
                    f"event line {index} resumes a finding outside the current backlog"
                )
            latest_digests: dict[str, str] = {}
            for prior in prior_events:
                if prior["kind"] == "audit":
                    for finding in prior["findings"]:
                        latest_digests[finding["finding_key"]] = prior["target"][
                            "digest"
                        ]
            if not any(
                latest_digests.get(key) != event["target"]["digest"]
                for key in requested
            ):
                raise ReceiptError(
                    f"event line {index} resumes without a changed target digest"
                )
            if event["spec_epoch"] != spec_epoch + 1:
                raise ReceiptError(f"event line {index} has non-monotonic spec_epoch")
            spec_epoch = event["spec_epoch"]
            terminal_seen = None
        elif terminal_seen == "audit-converged":
            if event["kind"] == "intent-alignment":
                terminal_seen = "boundary-review"
            elif event["kind"] == "audit":
                # A changed artifact is turned into an ordinary finding and resumes
                # the content loop rather than retaining stale handoff authority.
                terminal_seen = None
            else:
                raise ReceiptError(f"event line {index} cannot follow audit-converged")
        elif terminal_seen == "boundary-review":
            if event["kind"] != "terminal" or event["terminal"] != "audit-stabilized":
                raise ReceiptError(
                    f"event line {index} must finalize the boundary review"
                )
        elif event["spec_epoch"] != spec_epoch:
            raise ReceiptError(f"event line {index} has stale spec_epoch")
        if event["kind"] == "audit":
            if event["round"] != latest_audit_round + 1:
                raise ReceiptError(f"event line {index} has out-of-sequence round")
            latest_audit_round = event["round"]
        elif event["round"] != latest_audit_round:
            raise ReceiptError(f"event line {index} has out-of-sequence round")
        if event["kind"] == "terminal":
            terminal_seen = event["terminal"]


def load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ReceiptError(f"could not read receipt: {path}")
    events: list[dict[str, Any]] = []
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw.strip():
            raise ReceiptError(f"receipt line {line_number} is blank")
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReceiptError(
                f"receipt line {line_number} is invalid JSON: {exc.msg}"
            ) from exc
        try:
            validate_event(event)
        except ReceiptError as exc:
            raise ReceiptError(f"receipt line {line_number}: {exc}") from exc
        events.append(event)
    _validate_sequence(events)
    return events


def append(path: Path, event: dict[str, Any]) -> bool:
    """Durably append exactly one complete JSON line."""
    validate_event(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        events = load(path) if path.exists() else []
        _validate_sequence([*events, event])
        with path.open("ab") as stream:
            stream.write(canonical(event) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        fsync_directory(path.parent)
    return True


def append_many(path: Path, events_to_append: list[dict[str, Any]]) -> bool:
    """Atomically replace the receipt with one complete boundary transition."""
    if not events_to_append:
        return True
    for event in events_to_append:
        validate_event(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        events = load(path) if path.exists() else []
        _validate_sequence([*events, *events_to_append])
        payload = path.read_bytes() if path.exists() else b""
        payload += b"".join(canonical(event) + b"\n" for event in events_to_append)
        descriptor, replacement = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(replacement, path)
        except BaseException:
            Path(replacement).unlink(missing_ok=True)
            raise
        fsync_directory(path.parent)
    return True


def _valid_override_keys(
    events: list[dict[str, Any]], severities: dict[str, str]
) -> set[str]:
    accepted: set[str] = set()
    if not events:
        return accepted
    target_digest = events[-1]["target"]["digest"]
    now = datetime.now(UTC)
    for event in events:
        if event["kind"] != "override":
            continue
        payload = event["payload"]
        try:
            expiry = datetime.fromisoformat(
                payload["expires_at"].replace("Z", "+00:00")
            )
        except (AttributeError, ValueError):
            continue
        if (
            event["actor"]["family"] != "human"
            or not payload["human_actor"].strip()
            or not payload["residual_risk_owner"].strip()
            or not payload["justification"].strip()
            or expiry <= now
            or payload["target_digest"] != target_digest
        ):
            continue
        for key in payload["finding_keys"]:
            if (
                severities.get(key) == "critical"
                and not payload["critical_compensating_control"]
            ):
                continue
            accepted.add(key)
    return accepted


def valid_override_keys(events: list[dict[str, Any]]) -> set[str]:
    return _valid_override_keys(events, finding_severities(events))


def finding_severities(events: list[dict[str, Any]]) -> dict[str, str]:
    severities: dict[str, str] = {}
    for event in events:
        if event["kind"] == "audit":
            for finding in event["findings"]:
                severities[finding["finding_key"]] = finding["severity"]
    return severities


def fold(events: list[dict[str, Any]], *_: Any) -> Fold:
    if not events:
        return Fold(
            0,
            0,
            (),
            {severity: 0 for severity in SEVERITIES},
            False,
            False,
            False,
        )
    _validate_sequence(events)
    state: dict[str, str] = {}
    severities: dict[str, str] = {}
    latest_audit: dict[str, Any] | None = None
    audits = fixes = 0
    terminal: str | None = None
    for event in events:
        if event["kind"] == "audit":
            audits += 1
            latest_audit = event
            terminal = None
            for finding in event["findings"]:
                key = finding["finding_key"]
                state[key] = finding["disposition"]
                severities[key] = finding["severity"]
        elif event["kind"] == "fix":
            fixes += 1
        elif event["kind"] == "spec-freeze":
            terminal = None
        elif event["kind"] == "doc-review-resume":
            terminal = None
        elif event["kind"] == "resume-after-fix":
            terminal = None
        elif event["kind"] == "terminal":
            terminal = event["terminal"]
    for key in _valid_override_keys(events, severities):
        state[key] = "accepted-risk"
    backlog = tuple(
        sorted(
            key for key, disposition in state.items() if disposition == "open-blocking"
        )
    )
    stable = bool(
        latest_audit
        and latest_audit["scope"]["kind"] == "verify"
        and latest_audit["new_by_severity"]["critical"] == 0
        and latest_audit["new_by_severity"]["major"] <= 2
        and all(
            latest_audit["major_nonblocking_justifications"].get(finding["finding_key"])
            for finding in latest_audit["findings"]
            if finding["severity"] == "major" and finding["disposition"] != "fixed"
        )
    )
    return Fold(
        audits,
        fixes,
        backlog,
        (
            dict(latest_audit["new_by_severity"])
            if latest_audit
            else {severity: 0 for severity in SEVERITIES}
        ),
        stable,
        stable and not backlog,
        stable
        and not backlog
        and terminal == "audit-stabilized"
        and any(
            event["kind"] == "intent-alignment"
            and event["payload"]["verdict"] == "ALIGNED"
            for event in events
        ),
        terminal,
    )


def validate_receipt_authority(events: list[dict[str, Any]]) -> None:
    for event in events:
        validate_event(event)
    _validate_sequence(events)


def render_audit_payload_example() -> str:
    payload = {
        "scope": {
            "kind": "full",
            "backlog": [],
            "diff_since": None,
            "diff_summary": {"changed": True},
        },
        "findings": [
            {
                "finding_key": "round1:F-1",
                "id": "F-1",
                "submitted_severity": "major",
                "severity": "major",
                "fingerprint": "fp:example",
                "cluster": {
                    "spec_version": "audit-loop@schema-v1",
                    "invariant_id": None,
                    "subsystem": "audit-loop",
                    "violation_kind": "incorrect-behavior",
                },
                "reachable_behavior": "A reachable caller observes incorrect behavior.",
                "disposition": "open-blocking",
            }
        ],
        "new_by_severity": {"critical": 0, "major": 1, "minor": 0},
        "major_nonblocking_justifications": {
            "round1:F-1": "The rule is unambiguous; this only records coverage."
        },
        "source_event_id": "round-1-audit",
    }
    _validate_payload("audit", payload)
    return json.dumps(payload, indent=2, sort_keys=True)


def render_region(events: list[dict[str, Any]]) -> str:
    state = fold(events)
    latest = next(
        (event for event in reversed(events) if event["kind"] == "audit"), None
    )
    if latest is None:
        return (
            f"{REGION_START}\n## Status Summary\n\n"
            "**Loop state:** not started — NOT promotion ready\n"
            f"{REGION_END}\n"
        )
    counts = latest["new_by_severity"]
    loop_state = state.terminal or ("audit-converged" if state.converged else "active")
    promotion = "promotion ready" if state.promotion_ready else "NOT promotion ready"
    return (
        f"{REGION_START}\n## Status Summary\n\n"
        f"**Loop state:** {loop_state} — {promotion}\n\n"
        "| Loop | Round | Target digest | New CRITICAL | New MAJOR | New MINOR | Open blocking | Scope | Stable |\n"
        "|---|---:|---|---:|---:|---:|---|---|---|\n"
        f"| {latest['stage']} | {latest['round']} | {latest['target']['digest']} | "
        f"{counts['critical']} | {counts['major']} | {counts['minor']} | "
        f"{', '.join(state.backlog) or '—'} | {latest['scope']['kind']} | "
        f"{'yes' if state.stable else 'no'} |\n{REGION_END}\n"
    )


def render(audit_doc: Path, *, check: bool = False) -> bool:
    events = load(receipt_path(audit_doc))
    text = audit_doc.read_text(encoding="utf-8")
    if text.count(REGION_START) != 1 or text.count(REGION_END) != 1:
        raise ReceiptError(
            "audit doc must contain exactly one generated loop_receipt region"
        )
    before, rest = text.split(REGION_START, 1)
    _, after = rest.split(REGION_END, 1)
    expected = before + render_region(events) + after.lstrip("\n")
    if expected == text:
        return True
    if check:
        return False
    audit_doc.write_text(expected, encoding="utf-8")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    render_parser = sub.add_parser("render")
    render_parser.add_argument("audit_doc", type=Path)
    render_parser.add_argument("--check", action="store_true")
    state_parser = sub.add_parser("state")
    state_parser.add_argument("receipt", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            current = render(args.audit_doc, check=args.check)
            return 0 if current else (1 if args.check else 0)
        print(json.dumps(fold(load(args.receipt)).__dict__, sort_keys=True))
        return 0
    except ReceiptError as exc:
        print(f"audit-receipt: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
