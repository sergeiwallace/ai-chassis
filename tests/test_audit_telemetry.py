from pathlib import Path

from scripts import audit_receipt
from scripts.audit_telemetry import summarize
from tests.test_audit_receipt import audit, event, start


def write(path: Path, events: list[dict]) -> None:
    for item in events:
        audit_receipt.append(path, item)


def test_invalid_receipt_is_reported_not_omitted(tmp_path: Path) -> None:
    path = tmp_path / "bad.receipt.jsonl"
    path.write_text("bad\n", encoding="utf-8")
    assert str(path) in summarize([path])["invalid"]


def test_metrics_are_grouped_by_stage_and_risk_tier(tmp_path: Path) -> None:
    path = tmp_path / "receipt.jsonl"
    full = audit(
        1,
        [],
        counts={"critical": 0, "major": 1, "minor": 2},
    )
    terminal = event("terminal", 1, terminal="halted-cap", reason="budget exhausted")
    write(path, [start(), full, terminal])
    report = summarize([path])
    assert report["counts"]["audit"] == 1
    assert report["counts"]["round_full"] == 1
    assert report["counts"]["new_major"] == 1
    assert report["counts"]["new_minor"] == len(("first", "second"))
    assert report["counts"]["terminal_halted-cap"] == 1
    assert report["by_stage"]["doc-audit"]["audit"] == 1
    assert report["by_stage_risk_tier"]["doc-audit"]["standard"]["audit"] == 1


def test_halt_reason_is_counted(tmp_path: Path) -> None:
    path = tmp_path / "receipt.jsonl"
    halt = event(
        "halt",
        0,
        halt="fingerprint",
        finding_keys=["round1:F-1"],
        reason="recurred",
    )
    terminal = event("terminal", 0, terminal="halted-recurrence", reason="recurred")
    write(path, [start(), halt, terminal])
    counts = summarize([path])["counts"]
    assert counts["halt_fingerprint"] == 1
    assert counts["terminal_halted-recurrence"] == 1
