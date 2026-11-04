import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import ai_harness.apply as apply_module
from ai_harness.apply import ApplyError, InstallTarget, apply, doctor, rollback


def _hygiene(tmp_path: Path) -> tuple[Path, Path]:
    """T-2.10 AC3: rollback fixtures create valid hygiene sources."""
    patterns, allowlist = tmp_path / "patterns", tmp_path / "allowlist"
    patterns.write_text("never-matches")
    allowlist.write_text("context\tnever-matches\n")
    return patterns, allowlist


def test_rollback_reconciles_only_fake_tmp_path_targets(tmp_path: Path) -> None:
    state, target = tmp_path / "state", tmp_path / "install" / "context"
    target.parent.mkdir(parents=True)
    patterns, allowlist = _hygiene(tmp_path)
    apply(
        {"context": b"one"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    apply(
        {"context": b"two"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    rollback(state)
    assert target.read_text() == "one"


def test_rollback_without_previous_is_non_mutating(tmp_path: Path) -> None:
    with pytest.raises(ApplyError, match="no previous"):
        rollback(tmp_path / "state")


def test_rollback_failure_keeps_pointer_and_names_failed_target(tmp_path: Path) -> None:
    """T-2.5 AC3: failed rollback is journalled and leaves current unmoved."""
    state, target = tmp_path / "state", tmp_path / "install" / "context"
    patterns, allowlist = _hygiene(tmp_path)
    apply(
        {"context": b"one"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    current = apply(
        {"context": b"two"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )

    def fail(stage: str, candidate: InstallTarget) -> None:
        if stage == "rollback-reconcile":
            raise OSError("seeded rollback failure")

    with pytest.raises(ApplyError, match=str(target)):
        rollback(state, failure_hook=fail)
    assert (state / "current").read_text().strip() == current
    assert (
        '"state": "aborted-restored"'
        in (state / "generations" / current / "rollback.json").read_text()
    )


def test_rollback_restores_directory_target_byte_for_byte(tmp_path: Path) -> None:
    """N-01 / AD-2: rollback compensation restores a captured directory subtree."""
    state = tmp_path / "state"
    directory, later = tmp_path / "directory", tmp_path / "later"
    patterns, allowlist = _hygiene(tmp_path)
    apply(
        {"directory": b"one", "later": b"one"},
        [InstallTarget("directory", directory), InstallTarget("later", later)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    apply(
        {"directory": b"two", "later": b"two"},
        [InstallTarget("directory", directory), InstallTarget("later", later)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    directory.unlink()
    directory.mkdir()
    (directory / "nested").mkdir()
    (directory / "nested" / "precious").write_bytes(b"directory bytes")

    def fail(stage: str, target: InstallTarget) -> None:
        if stage == "rollback-reconcile" and target.path == later:
            raise OSError("later rollback failure")

    with pytest.raises(ApplyError):
        rollback(state, failure_hook=fail)
    assert (directory / "nested" / "precious").read_bytes() == b"directory bytes"


def test_doctor_repairs_failed_rollback_restore(tmp_path: Path) -> None:
    """N-03: rollback journals contribute to health and doctor repair."""
    state = tmp_path / "state"
    first, later = tmp_path / "first", tmp_path / "later"
    patterns, allowlist = _hygiene(tmp_path)
    targets = [InstallTarget("first", first), InstallTarget("later", later)]
    apply(
        {"first": b"one", "later": b"one"},
        targets,
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    apply(
        {"first": b"two", "later": b"two"},
        targets,
        state,
        patterns=patterns,
        allowlist=allowlist,
    )

    def fail(stage: str, target: InstallTarget) -> None:
        if stage == "rollback-reconcile" and target.path == later:
            raise OSError("rollback failure")
        if stage == "rollback-restore" and target.path == first:
            raise OSError("restore failure")

    with pytest.raises(ApplyError):
        rollback(state, failure_hook=fail)
    assert not doctor(state).healthy
    assert doctor(state, repair=True).healthy
    assert first.read_text() == "two"


def test_doctor_repairs_directory_captured_before_rollback_reconcile(
    tmp_path: Path,
) -> None:
    """N2-1: an interrupted rollback directory capture is unhealthy and repairable."""
    state, target = tmp_path / "state", tmp_path / "install" / "context"
    target.parent.mkdir()
    patterns, allowlist = _hygiene(tmp_path)
    apply(
        {"context": b"one"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    apply(
        {"context": b"two"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    target.unlink()
    target.mkdir()
    (target / "precious").write_bytes(b"keep")

    def interrupt(stage: str, _target: InstallTarget) -> None:
        if stage == "after-capture":
            raise KeyboardInterrupt("simulated kill")

    with pytest.raises(KeyboardInterrupt, match="simulated kill"):
        rollback(state, failure_hook=interrupt)
    assert not doctor(state).healthy
    assert doctor(state, repair=True).healthy
    assert (target / "precious").read_bytes() == b"keep"


def test_doctor_repairs_directory_when_rollback_capture_marker_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N3-1: a failed rollback capture marker leaves the directory repairable."""
    state, target = tmp_path / "state", tmp_path / "install" / "context"
    target.parent.mkdir()
    patterns, allowlist = _hygiene(tmp_path)
    apply(
        {"context": b"one"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    apply(
        {"context": b"two"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    target.unlink()
    target.mkdir()
    (target / "precious").write_bytes(b"keep")
    original_write = apply_module.write_json_fsync
    failed = False

    def fail_capture_marker(path: Path, data: dict[str, object]) -> None:
        nonlocal failed
        entries = data.get("targets")
        if (
            not failed
            and path.name == "rollback.json"
            and isinstance(entries, list)
            and entries
            and isinstance(entries[0], dict)
            and entries[0].get("capture_outcome") == "captured"
        ):
            failed = True
            raise OSError("post-capture journal write failed")
        original_write(path, data)

    monkeypatch.setattr(apply_module, "write_json_fsync", fail_capture_marker)
    with pytest.raises(ApplyError, match="post-capture journal write failed"):
        rollback(state)
    journal = next((state / "generations").glob("*/rollback.json"))
    assert '"state": "aborted-restored"' not in journal.read_text()
    assert not doctor(state).healthy
    assert doctor(state, repair=True).healthy
    assert (target / "precious").read_bytes() == b"keep"


def test_doctor_repairs_second_rollback_target_after_capture_marker_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N4-1/N4-2: rollback recovery follows durable entries and names target two."""
    state = tmp_path / "state"
    first, second = tmp_path / "install" / "one", tmp_path / "install" / "two"
    targets = [InstallTarget("one", first), InstallTarget("two", second)]
    patterns, allowlist = _hygiene(tmp_path)
    apply(
        {"one": b"one", "two": b"one"},
        targets,
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    apply(
        {"one": b"two", "two": b"two"},
        targets,
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    for target, contents in ((first, b"first bytes"), (second, b"second bytes")):
        target.unlink()
        target.mkdir()
        (target / "precious").write_bytes(contents)
    original_write = apply_module.write_json_fsync
    failed = False

    def fail_second_capture_marker(path: Path, data: dict[str, object]) -> None:
        nonlocal failed
        entries = data.get("targets")
        if (
            not failed
            and path.name == "rollback.json"
            and isinstance(entries, list)
            and len(entries) == 2
            and isinstance(entries[1], dict)
            and entries[1].get("capture_outcome") == "captured"
        ):
            failed = True
            raise OSError("post-capture journal write failed on second target")
        original_write(path, data)

    monkeypatch.setattr(apply_module, "write_json_fsync", fail_second_capture_marker)
    with pytest.raises(ApplyError, match=rf"target {second}") as exc_info:
        rollback(state)
    assert str(first) not in str(exc_info.value)
    journal = next((state / "generations").glob("*/rollback.json"))
    assert '"state": "aborted-restored"' not in journal.read_text()
    assert not doctor(state).healthy
    assert doctor(state, repair=True).healthy
    assert (first / "precious").read_bytes() == b"first bytes"
    assert (second / "precious").read_bytes() == b"second bytes"


@pytest.mark.parametrize(
    "interrupted_stage",
    ["after-rollback-current-pointer", "after-rollback-previous-pointer"],
)
def test_doctor_finishes_staged_rollback_pointer_transition(
    tmp_path: Path, interrupted_stage: str
) -> None:
    """N6-2: a rollback records and completes its intended pointer pair."""
    state, target = tmp_path / "state", tmp_path / "install" / "context"
    patterns, allowlist = _hygiene(tmp_path)
    previous = apply(
        {"context": b"one"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    current = apply(
        {"context": b"two"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )

    def interrupt(stage: str, _target: InstallTarget) -> None:
        if stage == interrupted_stage:
            raise KeyboardInterrupt("simulated kill")

    with pytest.raises(KeyboardInterrupt, match="simulated kill"):
        rollback(state, failure_hook=interrupt)
    assert not doctor(state).healthy
    assert doctor(state, repair=True).healthy
    assert (state / "current").read_text().strip() == previous
    assert (state / "previous").read_text().strip() == current
    assert target.read_bytes() == b"one"
    journal = state / "generations" / current / "rollback.json"
    assert '"state": "committed"' in journal.read_text()


def test_doctor_finishes_rollback_after_committed_journal_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N6-2: a committed rollback pointer pair is never compensated backward."""
    state, target = tmp_path / "state", tmp_path / "install" / "context"
    patterns, allowlist = _hygiene(tmp_path)
    previous = apply(
        {"context": b"one"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    current = apply(
        {"context": b"two"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    original_write = apply_module.write_json_fsync

    def fail_committed_rollback(path: Path, data: dict[str, object]) -> None:
        if path.name == "rollback.json" and data.get("state") == "committed":
            raise OSError("committed marker persistence failed")
        original_write(path, data)

    monkeypatch.setattr(apply_module, "write_json_fsync", fail_committed_rollback)
    with pytest.raises(ApplyError, match="failed to record committed rollback"):
        rollback(state)
    monkeypatch.setattr(apply_module, "write_json_fsync", original_write)
    assert doctor(state, repair=True).healthy
    assert (state / "current").read_text().strip() == previous
    assert (state / "previous").read_text().strip() == current
    assert target.read_bytes() == b"one"


def test_doctor_does_not_compensate_legacy_rollback_after_current_pointer(
    tmp_path: Path,
) -> None:
    """N6-2: old journals without a pointer pair fail closed after commit."""
    state, target = tmp_path / "state", tmp_path / "install" / "context"
    patterns, allowlist = _hygiene(tmp_path)
    apply(
        {"context": b"one"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    current = apply(
        {"context": b"two"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )

    def interrupt(stage: str, _target: InstallTarget) -> None:
        if stage == "after-rollback-current-pointer":
            raise KeyboardInterrupt("simulated kill")

    with pytest.raises(KeyboardInterrupt, match="simulated kill"):
        rollback(state, failure_hook=interrupt)
    journal = state / "generations" / current / "rollback.json"
    journal_data = json.loads(journal.read_text())
    journal_data.pop("pointer_transition")
    journal.write_text(json.dumps(journal_data))
    result = doctor(state, repair=True)
    assert not result.healthy
    assert target.read_bytes() == b"one"


def test_doctor_keeps_rollback_unresolved_when_directory_aside_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N7-2: lost rollback recovery assets do not mark an applied target restored."""
    state, target = tmp_path / "state", tmp_path / "install" / "context"
    patterns, allowlist = _hygiene(tmp_path)
    apply(
        {"context": b"one"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    apply(
        {"context": b"two"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    target.unlink()
    target.mkdir()
    (target / "precious").write_bytes(b"keep")
    original_reconcile = apply_module.reconcile

    def interrupt_after_reconcile(
        source: Path, destination: Path, requested_kind: str
    ) -> str:
        original_reconcile(source, destination, requested_kind)
        raise KeyboardInterrupt("simulated kill")

    monkeypatch.setattr(apply_module, "reconcile", interrupt_after_reconcile)
    with pytest.raises(KeyboardInterrupt, match="simulated kill"):
        rollback(state)
    journal = next((state / "generations").glob("*/rollback.json"))
    aside = journal.parent / "rollback-aside" / "0"
    shutil.rmtree(aside)
    applied_link = target.readlink()
    result = doctor(state, repair=True)
    assert not result.healthy
    assert result.unresolved == 1
    assert target.is_symlink()
    assert target.readlink() == applied_link
    assert '"restore_outcome": "restored"' not in journal.read_text()


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            lambda transition: transition.pop("old_current"), id="missing-old-current"
        ),
        pytest.param(
            lambda transition: transition.pop("old_previous"), id="missing-old-previous"
        ),
        *[
            pytest.param(
                lambda transition, field=field: transition.__setitem__(field, ""),
                id=f"empty-{field}",
            )
            for field in (
                "old_current",
                "old_previous",
                "new_current",
                "new_previous",
            )
        ],
        pytest.param(
            lambda transition: transition.update(
                new_current=transition["old_current"],
                new_previous=transition["old_previous"],
            ),
            id="swapped-pair",
        ),
        pytest.param(
            lambda transition: transition.update(
                old_previous="nonexistent-generation",
                new_current="nonexistent-generation",
            ),
            id="nonexistent-new-current",
        ),
        pytest.param(
            lambda transition: transition.update(new_previous="nonexistent-generation"),
            id="nonexistent-new-previous",
        ),
    ],
)
def test_doctor_rejects_invalid_rollback_pointer_transition(
    tmp_path: Path, mutation: object
) -> None:
    """N7-3: malformed rollback pairs fail closed before any recovery mutation."""
    state, target = tmp_path / "state", tmp_path / "install" / "context"
    patterns, allowlist = _hygiene(tmp_path)
    apply(
        {"context": b"one"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    apply(
        {"context": b"two"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )

    def interrupt(stage: str, _target: InstallTarget) -> None:
        if stage == "after-rollback-current-pointer":
            raise KeyboardInterrupt("simulated kill")

    with pytest.raises(KeyboardInterrupt, match="simulated kill"):
        rollback(state, failure_hook=interrupt)
    journal = next((state / "generations").glob("*/rollback.json"))
    journal_data = json.loads(journal.read_text())
    transition = journal_data["pointer_transition"]
    assert isinstance(transition, dict)
    assert callable(mutation)
    mutation(transition)
    journal.write_text(json.dumps(journal_data))
    current_before = (state / "current").read_text()
    previous_before = (state / "previous").read_text()
    target_before = target.readlink()

    result = doctor(state, repair=True)

    assert not result.healthy
    assert result.unresolved == 1
    assert (state / "current").read_text() == current_before
    assert (state / "previous").read_text() == previous_before
    assert target.is_symlink()
    assert target.readlink() == target_before
    assert '"state": "committed"' not in journal.read_text()
