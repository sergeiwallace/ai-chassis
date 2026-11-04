#!/usr/bin/env python3
# ruff: noqa: E402
"""Block promotion unless the authoritative audit-loop state permits it."""

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
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import audit_receipt
from scripts.audit_receipt import ReceiptError, fold, load, receipt_path


class GateError(ValueError):
    """The state file cannot authorize implementation."""


def _digest(path: Path) -> str:
    if path.is_file():
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if not path.is_dir():
        return "missing"
    result = hashlib.sha256()
    for member in sorted(
        item
        for item in path.rglob("*")
        if item.is_file()
        and not any(part in {".git", ".venv", "__pycache__"} for part in item.parts)
    ):
        result.update(member.relative_to(path).as_posix().encode())
        result.update(b"\0")
        result.update(member.read_bytes())
        result.update(b"\0")
    return "sha256:" + result.hexdigest()


def _target_path(event: dict, audit_path: Path) -> Path:
    target = Path(event["target"]["path"])
    if target.is_absolute():
        return target
    if audit_path.parent.name == "audits":
        return audit_path.parents[2] / target
    return audit_path.parent / target


def _matching_alignment(
    events: list[dict],
    *,
    root_digest: str,
    transition: str,
    artifact_sha256: str | None,
    next_brief_sha256: str | None,
) -> None:
    alignments = [event for event in events if event["kind"] == "intent-alignment"]
    if not alignments:
        raise GateError("authoritative state has no intent-alignment event")
    latest_error = None
    for event in reversed(alignments):
        try:
            audit_receipt.ask_fidelity.validate_promotion(
                event["payload"],
                root_digest=root_digest,
                transition=transition,
                artifact_sha256=artifact_sha256,
                next_brief_sha256=next_brief_sha256,
            )
        except (KeyError, ValueError) as exc:
            if latest_error is None:
                latest_error = exc
        else:
            return
    raise GateError(
        f"intent alignment is not current: {latest_error}"
    ) from latest_error


def check(
    audit_path: Path,
    ledger_path: Path | None = None,
    *,
    root_digest: str | None = None,
    transition: str | None = None,
    artifact_sha256: str | None = None,
    next_brief: Path | None = None,
) -> str:
    """Return the promotion reason or raise :class:`GateError`."""
    if ledger_path is not None:
        raise GateError(
            "--ledger was removed; the adjacent state file is authoritative"
        )
    try:
        events = load(receipt_path(audit_path))
    except ReceiptError as exc:
        raise GateError(str(exc)) from exc
    if not events:
        raise GateError("receipt has no validated events")
    state = fold(events)
    latest = events[-1]
    target = _target_path(latest, audit_path)
    actual = _digest(target)
    if actual != latest["target"]["digest"]:
        raise GateError(
            "target digest changed after audit "
            f"(recorded={latest['target']['digest']}, actual={actual})"
        )
    if root_digest is not None or transition is not None:
        if root_digest is None or transition is None or next_brief is None:
            raise GateError(
                "intent gate requires root_digest, transition, and next_brief"
            )
        if not next_brief.is_file():
            raise GateError(f"successor brief is missing: {next_brief}")
        _matching_alignment(
            events,
            root_digest=root_digest,
            transition=transition,
            artifact_sha256=artifact_sha256,
            next_brief_sha256=hashlib.sha256(next_brief.read_bytes()).hexdigest(),
        )
    if state.backlog:
        raise GateError("current blocking backlog: " + ", ".join(state.backlog))
    if not state.promotion_ready:
        raise GateError("promotion predicate is not satisfied")
    if state.terminal != "audit-stabilized":
        raise GateError("audit loop has no audit-stabilized terminal")
    return f"state-file promotion readiness for {latest['target']['digest']}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_doc", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--root-digest")
    parser.add_argument("--transition")
    parser.add_argument("--artifact-sha256")
    parser.add_argument("--next-brief", type=Path)
    args = parser.parse_args(argv)
    try:
        reason = check(
            args.audit_doc,
            args.ledger,
            root_digest=args.root_digest,
            transition=args.transition,
            artifact_sha256=args.artifact_sha256,
            next_brief=args.next_brief,
        )
    except GateError as exc:
        print(f"{args.audit_doc}: implement gate BLOCKED: {exc}", file=sys.stderr)
        return 1
    print(f"{args.audit_doc}: implement gate passed ({reason})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
