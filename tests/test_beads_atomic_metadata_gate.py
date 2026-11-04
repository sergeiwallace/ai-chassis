"""Regression coverage for the D-12 native-capability rollout gate."""

from __future__ import annotations

from types import SimpleNamespace

from scripts import beads_atomic_metadata_gate as gate


def test_current_bd_1_1_capability_is_blocked_without_arbitrary_metadata() -> None:
    result = gate.assess_capability(
        "bd version 1.1.0 (Homebrew)",
        "Supported update keys: status, priority, title, assignee\nNOTE: narrow subset\n",
    )

    assert result.version == "1.1.0"
    assert not result.supported
    assert any("arbitrary atomic metadata" in problem for problem in result.problems)


def test_only_complete_official_atomic_capability_unblocks_rollout() -> None:
    result = gate.assess_capability(
        "bd version 1.2.0",
        """Supported atomic mutations: create, update, claim, unclaim, close, reopen, dependency
Supported atomic metadata: arbitrary
""",
    )

    assert result.version == "1.2.0"
    assert result.supported


def test_incomplete_mutator_matrix_remains_blocked() -> None:
    result = gate.assess_capability(
        "bd version 1.2.0",
        """Supported atomic mutations: create, update, claim, close
Supported atomic metadata: arbitrary
""",
    )

    assert not result.supported
    assert "dependency" in " ".join(result.problems)


def test_main_reports_a_blocking_runtime_probe(monkeypatch, capsys) -> None:
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="bd version 1.1.0\n", stderr=""),
            SimpleNamespace(
                returncode=0, stdout="Supported update keys: status\n", stderr=""
            ),
        ]
    )
    monkeypatch.setattr(
        gate.subprocess, "run", lambda *_args, **_kwargs: next(responses)
    )

    assert gate.main(["bd"]) == 1
    assert "BLOCKED" in capsys.readouterr().err
