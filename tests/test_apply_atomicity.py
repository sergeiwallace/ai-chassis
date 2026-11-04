import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import ai_harness.apply as apply_module
from ai_harness.apply import ApplyError, InstallTarget, apply, doctor
from ai_harness.links import LinkError, TargetState, restore


def _hygiene(tmp_path: Path) -> tuple[Path, Path]:
    """T-2.10 AC3: every successful apply supplies non-empty scan inputs."""
    patterns, allowlist = tmp_path / "patterns", tmp_path / "allowlist"
    patterns.write_text("never-matches")
    allowlist.write_text("context\tnever-matches\n")
    return patterns, allowlist


def test_apply_journals_then_rolls_back_fake_targets(tmp_path: Path) -> None:
    """All targets are tmp_path children, never a real home/install path."""
    state = tmp_path / "state"
    first, second = tmp_path / "install" / "one", tmp_path / "install" / "two"
    first.parent.mkdir(parents=True)
    first.write_text("old-one")
    second.write_text("old-two")
    targets = [InstallTarget("one", first), InstallTarget("two", second)]

    def fail(stage: str, target: InstallTarget) -> None:
        if stage == "reconcile" and target.path == second:
            raise OSError("seeded second-target failure")

    with pytest.raises(ApplyError, match="seeded second-target failure"):
        patterns, allowlist = _hygiene(tmp_path)
        apply(
            {"one": b"new-one", "two": b"new-two"},
            targets,
            state,
            patterns=patterns,
            allowlist=allowlist,
            failure_hook=fail,
        )
    assert first.read_text() == "old-one"
    assert second.read_text() == "old-two"
    manifest = next((state / "generations").glob("*/manifest.json")).read_text()
    assert '"state": "aborted-restored"' in manifest
    assert '"restore_outcome": "restored"' in manifest


def test_directory_target_is_restored_byte_for_byte_after_later_failure(
    tmp_path: Path,
) -> None:
    """AD-2 / T-2.2 AC: an aside directory survives compensating rollback intact."""
    state = tmp_path / "state"
    directory, later = tmp_path / "install" / "skills", tmp_path / "install" / "later"
    directory.mkdir(parents=True)
    (directory / "nested").mkdir()
    (directory / "nested" / "user.md").write_bytes(b"precious\x00bytes")
    later.write_text("old")
    patterns, allowlist = _hygiene(tmp_path)

    def fail(stage: str, target: InstallTarget) -> None:
        if stage == "reconcile" and target.path == later:
            raise OSError("seeded later failure")

    before = (directory / "nested" / "user.md").read_bytes()
    with pytest.raises(ApplyError, match=str(later)):
        apply(
            {"skills": b"generated", "later": b"generated"},
            [InstallTarget("skills", directory), InstallTarget("later", later)],
            state,
            patterns=patterns,
            allowlist=allowlist,
            failure_hook=fail,
        )
    assert (directory / "nested" / "user.md").read_bytes() == before
    manifest = next((state / "generations").glob("*/manifest.json")).read_text()
    assert '"restore_outcome": "restored"' in manifest


def test_directory_source_is_refused_by_ad2(tmp_path: Path) -> None:
    """AD-2: unsupported whole-tree sources fail loudly instead of being coerced."""
    source = tmp_path / "source"
    source.mkdir()
    patterns, allowlist = _hygiene(tmp_path)
    with pytest.raises(ApplyError, match="AD-2"):
        apply(
            {"skills": source},  # type: ignore[arg-type]
            [InstallTarget("skills", tmp_path / "install" / "skills")],
            tmp_path / "state",
            patterns=patterns,
            allowlist=allowlist,
        )


def test_hash_of_directory_symlink_is_not_a_file_error(tmp_path: Path) -> None:
    """F-15: symlink-to-directory drift observation does not read it as a file."""
    from ai_harness.apply import _hash

    directory, link = tmp_path / "directory", tmp_path / "link"
    directory.mkdir()
    link.symlink_to(directory, target_is_directory=True)
    assert _hash(link) is None


def test_doctor_repair_refuses_user_modified_target_and_is_unhealthy(
    tmp_path: Path,
) -> None:
    """F-03/F-16: repair refusal checks applied and prior states, then exits unhealthy."""
    state, first, second = tmp_path / "state", tmp_path / "one", tmp_path / "two"
    first.write_text("before")
    second.write_text("before")
    patterns, allowlist = _hygiene(tmp_path)

    def fail(stage: str, target: InstallTarget) -> None:
        if stage == "reconcile" and target == InstallTarget("two", second):
            raise OSError("later failure")
        if stage == "restore" and target == InstallTarget("one", first):
            raise OSError("restore failure")

    with pytest.raises(ApplyError):
        apply(
            {"one": b"applied", "two": b"applied"},
            [InstallTarget("one", first), InstallTarget("two", second)],
            state,
            patterns=patterns,
            allowlist=allowlist,
            failure_hook=fail,
        )
    # The generation copy the link points at is read-only (ISSUE-REF D-11 review), so a user's
    # edit replaces the link with a file of its own.
    first.unlink()
    first.write_text("user edit")
    result = doctor(state, repair=True)
    assert not result.healthy
    assert any("user-modified" in message for message in result.messages)


def test_given_a_drifted_target_when_applied_then_it_is_quarantined_and_re_rendered(
    tmp_path: Path,
) -> None:
    """ISSUE-REF D-11 (b): drift moves the hand edit aside; it never refuses the install."""
    state, target = tmp_path / "state", tmp_path / "install" / "context"
    patterns, allowlist = _hygiene(tmp_path)
    drifted = apply(
        {"context": b"one"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    target.unlink()
    target.write_text("user edit")
    reported: list[str] = []
    apply(
        {"context": b"two"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
        report=reported.append,
    )
    moved = state / "quarantine" / drifted / Path(*target.absolute().parts[1:])
    assert moved.read_text() == "user edit"
    assert target.read_bytes() == b"two"
    assert len(reported) == 1
    assert str(target) in reported[0] and str(moved) in reported[0]


def test_doctor_names_orphaned_staged_manifest_at_fake_root(tmp_path: Path) -> None:
    state, target = tmp_path / "state", tmp_path / "install" / "context"

    def stop_before_pointer(stage: str, _target: InstallTarget) -> None:
        if stage == "before-pointer":
            raise OSError("simulated kill")

    with pytest.raises(ApplyError):
        apply(
            {"context": b"content"},
            [InstallTarget("context", target)],
            state,
            patterns=_hygiene(tmp_path)[0],
            allowlist=_hygiene(tmp_path)[1],
            failure_hook=stop_before_pointer,
        )
    assert doctor(state).healthy


def test_artifact_patterns_require_an_explicit_fake_allowlist(tmp_path: Path) -> None:
    patterns, allowlist = tmp_path / "patterns", tmp_path / "allowlist"
    patterns.write_text("credential")
    with pytest.raises(ApplyError, match="allowlist"):
        apply(
            {"context": b"credential"},
            [InstallTarget("context", tmp_path / "install" / "context")],
            tmp_path / "state",
            patterns=patterns,
            allowlist=allowlist,
        )
    allowlist.write_text("context\tcredential\n")
    apply(
        {"context": b"credential"},
        [InstallTarget("context", tmp_path / "install" / "context")],
        tmp_path / "state",
        patterns=patterns,
        allowlist=allowlist,
    )


def test_consumer_must_be_a_readable_os_account(tmp_path: Path, monkeypatch) -> None:
    """T-2.10 AC1 / N-06: consumers are OS accounts and unknown names are refused."""
    patterns, allowlist = _hygiene(tmp_path)
    target = tmp_path / "install" / "context"
    target.parent.mkdir()
    account = SimpleNamespace(
        pw_uid=target.parent.stat().st_uid, pw_gid=target.parent.stat().st_gid
    )
    original_import = apply_module.importlib.import_module

    def account_lookup(name: str):
        if name == "pwd":
            return SimpleNamespace(
                getpwnam=lambda consumer: (
                    account
                    if consumer == "known-account"
                    else (_ for _ in ()).throw(KeyError(consumer))
                )
            )
        return original_import(name)

    monkeypatch.setattr(apply_module.importlib, "import_module", account_lookup)
    apply(
        {"context": b"content"},
        [InstallTarget("context", target, consumer="known-account")],
        tmp_path / "state-good",
        patterns=patterns,
        allowlist=allowlist,
    )
    missing = "ai-harness-account-that-does-not-exist"
    with pytest.raises(ApplyError, match=rf"{missing!s}.*OS account name"):
        apply(
            {"context": b"content"},
            [InstallTarget("context", target, consumer=missing)],
            tmp_path / "state-bad",
            patterns=patterns,
            allowlist=allowlist,
        )


def test_consumer_lookup_unavailable_is_a_named_refusal(
    tmp_path: Path, monkeypatch
) -> None:
    """N-05/N-06: unavailable account lookup names the declared OS account."""
    patterns, allowlist = _hygiene(tmp_path)
    consumer = "declared-service-account"
    monkeypatch.setattr(
        apply_module.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("unavailable")),
    )
    with pytest.raises(ApplyError, match=rf"{consumer}.*OS account name.*unavailable"):
        apply(
            {"context": b"content"},
            [InstallTarget("context", tmp_path / "target", consumer=consumer)],
            tmp_path / "state",
            patterns=patterns,
            allowlist=allowlist,
        )


def test_retention_preserves_unrestored_directory_aside(tmp_path: Path) -> None:
    """N-02: a later forced apply cannot delete staged recovery state."""
    state = tmp_path / "state"
    directory, later = tmp_path / "directory", tmp_path / "later"
    directory.mkdir()
    (directory / "precious").write_bytes(b"keep")
    later.write_text("old")
    patterns, allowlist = _hygiene(tmp_path)

    def fail(stage: str, target: InstallTarget) -> None:
        if stage == "reconcile" and target.path == later:
            raise OSError("later failure")
        if stage == "restore" and target.path == directory:
            raise OSError("restore failure")

    with pytest.raises(ApplyError):
        apply(
            {"directory": b"new", "later": b"new"},
            [InstallTarget("directory", directory), InstallTarget("later", later)],
            state,
            patterns=patterns,
            allowlist=allowlist,
            failure_hook=fail,
        )
    failed_manifest = next((state / "generations").glob("*/manifest.json"))
    aside = failed_manifest.parent / "aside" / "0"
    assert (aside / "precious").read_bytes() == b"keep"
    apply(
        {"directory": b"newer", "later": b"newer"},
        [InstallTarget("directory", directory), InstallTarget("later", later)],
        state,
        force=True,
        patterns=patterns,
        allowlist=allowlist,
    )
    assert (aside / "precious").read_bytes() == b"keep"
    assert not doctor(state).healthy


def test_doctor_repairs_directory_captured_before_reconcile(tmp_path: Path) -> None:
    """N2-1: an interrupted directory capture is unhealthy and repairable."""
    state = tmp_path / "state"
    directory = tmp_path / "install" / "skills"
    directory.mkdir(parents=True)
    (directory / "precious").write_bytes(b"keep")

    def interrupt(stage: str, _target: InstallTarget) -> None:
        if stage == "after-capture":
            raise KeyboardInterrupt("simulated kill")

    with pytest.raises(KeyboardInterrupt, match="simulated kill"):
        apply(
            {"skills": b"generated"},
            [InstallTarget("skills", directory)],
            state,
            patterns=_hygiene(tmp_path)[0],
            allowlist=_hygiene(tmp_path)[1],
            failure_hook=interrupt,
        )
    assert not doctor(state).healthy
    assert any("unrestored" in message for message in doctor(state).messages)
    assert doctor(state, repair=True).healthy
    assert (directory / "precious").read_bytes() == b"keep"


def test_doctor_repairs_directory_when_apply_capture_marker_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N3-1: a failed post-capture marker leaves the directory repairable."""
    state = tmp_path / "state"
    directory = tmp_path / "install" / "skills"
    directory.mkdir(parents=True)
    (directory / "precious").write_bytes(b"keep")
    original_write = apply_module.write_json_fsync
    failed = False

    def fail_capture_marker(path: Path, data: dict[str, object]) -> None:
        nonlocal failed
        entries = data.get("targets")
        if (
            not failed
            and path.name == "manifest.json"
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
        apply(
            {"skills": b"generated"},
            [InstallTarget("skills", directory)],
            state,
            patterns=_hygiene(tmp_path)[0],
            allowlist=_hygiene(tmp_path)[1],
        )
    journal = next((state / "generations").glob("*/manifest.json"))
    assert '"state": "aborted-restored"' not in journal.read_text()
    assert not doctor(state).healthy
    assert doctor(state, repair=True).healthy
    assert (directory / "precious").read_bytes() == b"keep"


def test_doctor_repairs_file_apply_after_mutated_outcome(tmp_path: Path) -> None:
    """N5-1: a durable file outcome remains recoverable after an interrupted apply."""
    state, target = tmp_path / "state", tmp_path / "install" / "context"
    target.parent.mkdir()
    target.write_bytes(b"original")

    def interrupt(stage: str, _target: InstallTarget) -> None:
        if stage == "before-pointer":
            raise KeyboardInterrupt("simulated kill")

    with pytest.raises(KeyboardInterrupt, match="simulated kill"):
        apply(
            {"context": b"generated"},
            [InstallTarget("context", target)],
            state,
            patterns=_hygiene(tmp_path)[0],
            allowlist=_hygiene(tmp_path)[1],
            failure_hook=interrupt,
        )
    journal = next((state / "generations").glob("*/manifest.json"))
    assert '"outcome": "mutated"' in journal.read_text()
    assert '"state": "aborted-restored"' not in journal.read_text()
    assert not doctor(state).healthy
    assert doctor(state, repair=True).healthy
    assert target.read_bytes() == b"original"


def test_doctor_repairs_file_apply_between_reconcile_and_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N5-1: durable reconcile intent closes the file pre-outcome kill window."""
    state, target = tmp_path / "state", tmp_path / "install" / "context"
    target.parent.mkdir()
    target.write_bytes(b"original")
    original_reconcile = apply_module.reconcile

    def interrupt_after_reconcile(
        source: Path, destination: Path, requested_kind: str
    ) -> str:
        original_reconcile(source, destination, requested_kind)
        raise KeyboardInterrupt("simulated kill")

    monkeypatch.setattr(apply_module, "reconcile", interrupt_after_reconcile)
    with pytest.raises(KeyboardInterrupt, match="simulated kill"):
        apply(
            {"context": b"generated"},
            [InstallTarget("context", target)],
            state,
            patterns=_hygiene(tmp_path)[0],
            allowlist=_hygiene(tmp_path)[1],
        )
    journal = next((state / "generations").glob("*/manifest.json"))
    assert '"reconcile_outcome": "intent"' in journal.read_text()
    assert '"outcome": "mutated"' not in journal.read_text()
    assert '"state": "aborted-restored"' not in journal.read_text()
    assert not doctor(state).healthy
    assert doctor(state, repair=True).healthy
    assert target.read_bytes() == b"original"


def test_doctor_preserves_restored_directory_when_restore_marker_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N6-1: a consumed directory aside is an already-restored prior state."""
    state = tmp_path / "state"
    directory, later = tmp_path / "install" / "skills", tmp_path / "install" / "later"
    directory.mkdir(parents=True)
    (directory / "precious").write_bytes(b"keep")
    later.write_bytes(b"old")
    original_write = apply_module.write_json_fsync

    def fail_restore_marker(path: Path, data: dict[str, object]) -> None:
        entries = data.get("targets")
        if (
            path.name == "manifest.json"
            and isinstance(entries, list)
            and entries
            and isinstance(entries[0], dict)
            and entries[0].get("restore_outcome") == "restored"
        ):
            raise OSError("restore marker persistence failed")
        original_write(path, data)

    def fail_later(stage: str, target: InstallTarget) -> None:
        if stage == "reconcile" and target.path == later:
            raise OSError("later target failed")

    monkeypatch.setattr(apply_module, "write_json_fsync", fail_restore_marker)
    with pytest.raises(OSError, match="restore marker persistence failed"):
        apply(
            {"skills": b"generated", "later": b"generated"},
            [InstallTarget("skills", directory), InstallTarget("later", later)],
            state,
            patterns=_hygiene(tmp_path)[0],
            allowlist=_hygiene(tmp_path)[1],
            failure_hook=fail_later,
        )
    monkeypatch.setattr(apply_module, "write_json_fsync", original_write)
    assert (directory / "precious").read_bytes() == b"keep"
    assert doctor(state, repair=True).healthy
    assert (directory / "precious").read_bytes() == b"keep"


def test_doctor_does_not_remove_user_directory_after_absent_intent_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N6-1: absence must not compare equal to an arbitrary non-file target."""
    state, target = tmp_path / "state", tmp_path / "install" / "context"

    def interrupt_reconcile(_source: Path, _target: Path, _kind: str) -> str:
        raise KeyboardInterrupt("simulated kill")

    monkeypatch.setattr(apply_module, "reconcile", interrupt_reconcile)
    with pytest.raises(KeyboardInterrupt, match="simulated kill"):
        apply(
            {"context": b"generated"},
            [InstallTarget("context", target)],
            state,
            patterns=_hygiene(tmp_path)[0],
            allowlist=_hygiene(tmp_path)[1],
        )
    target.parent.mkdir()
    target.mkdir()
    (target / "user-created").write_bytes(b"preserve")
    result = doctor(state, repair=True)
    assert not result.healthy
    assert (target / "user-created").read_bytes() == b"preserve"


def test_doctor_compares_symlink_prior_link_text_after_intent_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N6-1: different directory symlinks are not interchangeable None hashes."""
    state, target = tmp_path / "state", tmp_path / "install" / "context"
    prior_directory, user_directory = tmp_path / "prior", tmp_path / "user"
    prior_directory.mkdir()
    user_directory.mkdir()
    target.parent.mkdir()
    target.symlink_to(prior_directory, target_is_directory=True)

    def interrupt_reconcile(_source: Path, _target: Path, _kind: str) -> str:
        raise KeyboardInterrupt("simulated kill")

    monkeypatch.setattr(apply_module, "reconcile", interrupt_reconcile)
    with pytest.raises(KeyboardInterrupt, match="simulated kill"):
        apply(
            {"context": b"generated"},
            [InstallTarget("context", target)],
            state,
            patterns=_hygiene(tmp_path)[0],
            allowlist=_hygiene(tmp_path)[1],
        )
    target.unlink()
    target.symlink_to(user_directory, target_is_directory=True)
    result = doctor(state, repair=True)
    assert not result.healthy
    assert target.readlink() == user_directory


def test_doctor_reconciles_apply_after_current_pointer_before_journal_commit(
    tmp_path: Path,
) -> None:
    """N6-2: a staged generation named by current is committed forward."""
    state, target = tmp_path / "state", tmp_path / "install" / "context"
    patterns, allowlist = _hygiene(tmp_path)
    apply(
        {"context": b"old"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )

    def interrupt(stage: str, _target: InstallTarget) -> None:
        if stage == "after-current-pointer":
            raise KeyboardInterrupt("simulated kill")

    with pytest.raises(KeyboardInterrupt, match="simulated kill"):
        apply(
            {"context": b"new"},
            [InstallTarget("context", target)],
            state,
            patterns=patterns,
            allowlist=allowlist,
            failure_hook=interrupt,
        )
    current = (state / "current").read_text().strip()
    assert not doctor(state).healthy
    assert doctor(state, repair=True).healthy
    assert (state / "current").read_text().strip() == current
    assert target.read_bytes() == b"new"
    assert (
        '"state": "committed"'
        in (state / "generations" / current / "manifest.json").read_text()
    )


def test_doctor_restores_apply_pointer_pair_after_previous_pointer_crash(
    tmp_path: Path,
) -> None:
    """N7-1: repairing an interrupted pair preserves the prior rollback target."""
    state, target = tmp_path / "state", tmp_path / "install" / "context"
    patterns, allowlist = _hygiene(tmp_path)
    oldest = apply(
        {"context": b"oldest"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    current = apply(
        {"context": b"current"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )

    def interrupt(stage: str, _target: InstallTarget) -> None:
        if stage == "after-previous-pointer":
            raise KeyboardInterrupt("simulated kill")

    with pytest.raises(KeyboardInterrupt, match="simulated kill"):
        apply(
            {"context": b"interrupted"},
            [InstallTarget("context", target)],
            state,
            patterns=patterns,
            allowlist=allowlist,
            failure_hook=interrupt,
        )
    assert (state / "current").read_text().strip() == current
    assert (state / "previous").read_text().strip() == current
    assert not doctor(state).healthy
    assert doctor(state, repair=True).healthy
    assert (state / "current").read_text().strip() == current
    assert (state / "previous").read_text().strip() == oldest
    assert target.read_bytes() == b"current"
    assert doctor(state, repair=True).healthy
    assert (state / "previous").read_text().strip() == oldest


def test_missing_directory_aside_is_idempotent_only_for_restored_directory(
    tmp_path: Path,
) -> None:
    """N7-2: a lost aside cannot masquerade as a completed restore."""
    aside = tmp_path / "missing-aside"
    directory = tmp_path / "restored"
    directory.mkdir()
    restore(directory, TargetState("directory-aside", str(aside)))
    assert directory.is_dir()

    file_target = tmp_path / "applied"
    file_target.write_bytes(b"applied")
    with pytest.raises(LinkError, match="aside is missing"):
        restore(file_target, TargetState("directory-aside", str(aside)))
    assert file_target.read_bytes() == b"applied"


def test_doctor_keeps_apply_unresolved_when_directory_aside_is_missing(
    tmp_path: Path,
) -> None:
    """N7-2: a missing aside leaves an applied target and journal untouched."""
    state, directory = tmp_path / "state", tmp_path / "install" / "skills"
    directory.mkdir(parents=True)
    (directory / "precious").write_bytes(b"keep")

    def interrupt(stage: str, _target: InstallTarget) -> None:
        if stage == "before-pointer":
            raise KeyboardInterrupt("simulated kill")

    with pytest.raises(KeyboardInterrupt, match="simulated kill"):
        apply(
            {"skills": b"generated"},
            [InstallTarget("skills", directory)],
            state,
            patterns=_hygiene(tmp_path)[0],
            allowlist=_hygiene(tmp_path)[1],
            failure_hook=interrupt,
        )
    journal = next((state / "generations").glob("*/manifest.json"))
    aside = journal.parent / "aside" / "0"
    shutil.rmtree(aside)
    applied_link = directory.readlink()
    result = doctor(state, repair=True)
    assert not result.healthy
    assert result.unresolved == 1
    assert directory.is_symlink()
    assert directory.readlink() == applied_link
    assert '"restore_outcome": "restored"' not in journal.read_text()


def test_doctor_reconciles_apply_after_committed_journal_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N6-2: failed commit-marker persistence does not compensate current."""
    state, target = tmp_path / "state", tmp_path / "install" / "context"
    patterns, allowlist = _hygiene(tmp_path)
    original_write = apply_module.write_json_fsync

    def fail_committed_manifest(path: Path, data: dict[str, object]) -> None:
        if path.name == "manifest.json" and data.get("state") == "committed":
            raise OSError("committed marker persistence failed")
        original_write(path, data)

    monkeypatch.setattr(apply_module, "write_json_fsync", fail_committed_manifest)
    with pytest.raises(ApplyError, match="failed to record committed generation"):
        apply(
            {"context": b"new"},
            [InstallTarget("context", target)],
            state,
            patterns=patterns,
            allowlist=allowlist,
        )
    monkeypatch.setattr(apply_module, "write_json_fsync", original_write)
    assert doctor(state, repair=True).healthy
    assert target.read_bytes() == b"new"


def test_doctor_repairs_second_apply_target_after_capture_marker_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N4-1: durable entries, rather than `mutated`, govern apply recovery."""
    state = tmp_path / "state"
    first, second = tmp_path / "install" / "one", tmp_path / "install" / "two"
    for target, contents in ((first, b"first bytes"), (second, b"second bytes")):
        target.mkdir(parents=True)
        (target / "precious").write_bytes(contents)
    original_write = apply_module.write_json_fsync
    failed = False

    def fail_second_capture_marker(path: Path, data: dict[str, object]) -> None:
        nonlocal failed
        entries = data.get("targets")
        if (
            not failed
            and path.name == "manifest.json"
            and isinstance(entries, list)
            and len(entries) == 2
            and isinstance(entries[1], dict)
            and entries[1].get("capture_outcome") == "captured"
        ):
            failed = True
            raise OSError("post-capture journal write failed on second target")
        original_write(path, data)

    monkeypatch.setattr(apply_module, "write_json_fsync", fail_second_capture_marker)
    with pytest.raises(ApplyError, match="post-capture journal write failed"):
        apply(
            {"one": b"generated", "two": b"generated"},
            [InstallTarget("one", first), InstallTarget("two", second)],
            state,
            patterns=_hygiene(tmp_path)[0],
            allowlist=_hygiene(tmp_path)[1],
        )
    journal = next((state / "generations").glob("*/manifest.json"))
    assert '"state": "aborted-restored"' not in journal.read_text()
    assert not doctor(state).healthy
    assert doctor(state, repair=True).healthy
    assert (first / "precious").read_bytes() == b"first bytes"
    assert (second / "precious").read_bytes() == b"second bytes"


def test_apply_before_mutation_journal_failure_is_reclaimable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N4-3: a failed pre-mutation capture does not pin a safe generation."""
    state, target = tmp_path / "state", tmp_path / "install" / "skills"
    target.mkdir(parents=True)
    original_write = apply_module.write_json_fsync
    failed = False

    def fail_before_mutation(path: Path, data: dict[str, object]) -> None:
        nonlocal failed
        entries = data.get("targets")
        if (
            not failed
            and path.name == "manifest.json"
            and isinstance(entries, list)
            and entries
            and isinstance(entries[0], dict)
            and entries[0].get("prior_state", {}).get("kind") == "directory-aside"
            and entries[0].get("capture_outcome") is None
        ):
            failed = True
            raise OSError("before-mutation journal write failed")
        original_write(path, data)

    monkeypatch.setattr(apply_module, "write_json_fsync", fail_before_mutation)
    with pytest.raises(ApplyError, match="before-mutation journal write failed"):
        apply(
            {"skills": b"generated"},
            [InstallTarget("skills", target)],
            state,
            patterns=_hygiene(tmp_path)[0],
            allowlist=_hygiene(tmp_path)[1],
        )
    journal = next((state / "generations").glob("*/manifest.json"))
    assert '"state": "aborted-restored"' in journal.read_text()
    apply(
        {"skills": b"generated"},
        [InstallTarget("skills", target)],
        state,
        patterns=_hygiene(tmp_path)[0],
        allowlist=_hygiene(tmp_path)[1],
    )
    assert len(list((state / "generations").iterdir())) == 1


def test_retention_discards_fully_restored_failed_generation(tmp_path: Path) -> None:
    """N2-2: recovered failed transactions do not pin their generation."""
    state = tmp_path / "state"
    target = tmp_path / "install" / "context"
    patterns, allowlist = _hygiene(tmp_path)

    def fail(stage: str, _target: InstallTarget) -> None:
        if stage == "reconcile":
            raise OSError("transient failure")

    for _ in range(2):
        with pytest.raises(ApplyError, match="transient failure"):
            apply(
                {"context": b"generated"},
                [InstallTarget("context", target)],
                state,
                patterns=patterns,
                allowlist=allowlist,
                failure_hook=fail,
            )
    manifests = list((state / "generations").glob("*/manifest.json"))
    assert all('"state": "aborted-restored"' in path.read_text() for path in manifests)
    apply(
        {"context": b"generated"},
        [InstallTarget("context", target)],
        state,
        patterns=patterns,
        allowlist=allowlist,
    )
    assert len(list((state / "generations").iterdir())) == 1
