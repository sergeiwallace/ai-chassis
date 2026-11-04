"""Atomic, journalled application of rendered ai-harness artifacts."""

from __future__ import annotations

import hashlib
import importlib
import os
import re
import shutil
import stat
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .generations import (
    clear_pointer,
    generation_id,
    manifest_path,
    pointer,
    read_json,
    swap_pointer,
    write_json_fsync,
)
from .links import LinkError, TargetState, capture, observe, reconcile, restore
from .platform import (
    PathSafetyError,
    detect_environment,
    resolve_install_path,
    support_label,
    validate_install_target,
)


class ApplyError(RuntimeError):
    """An apply operation failed after preserving its recovery journal."""


@dataclass(frozen=True)
class InstallTarget:
    output: str
    path: Path
    requested_kind: str = "symlink"
    consumer: str | None = None


@dataclass(frozen=True)
class DoctorResult:
    """Machine-readable health outcome with human-facing diagnostic messages."""

    messages: tuple[str, ...]
    unresolved: int = 0

    @property
    def healthy(self) -> bool:
        return self.unresolved == 0

    def __iter__(self) -> Iterator[str]:
        return iter(self.messages)


FailureHook = Callable[[str, InstallTarget], None]


def _hash(path: Path) -> str | None:
    """Hash installed files without following directory links."""
    if path.is_symlink():
        if not path.exists():
            return None
        resolved = path.resolve(strict=True)
        return (
            None
            if resolved.is_dir()
            else hashlib.sha256(resolved.read_bytes()).hexdigest()
        )
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _manifest_targets(
    targets: list[InstallTarget], root: Path
) -> list[dict[str, object]]:
    return [
        {
            "output": item.output,
            "source": str((root / item.output).resolve(strict=True)),
            "path": str(resolve_install_path(item.path)),
            "requested_kind": item.requested_kind,
            "consumer": item.consumer,
            "prior_state": observe(item.path).as_dict(),
        }
        for item in targets
    ]


def _validate_artifacts(
    outputs: dict[str, bytes], patterns: Path | None, allowlist: Path | None
) -> None:
    if (
        patterns is None
        or allowlist is None
        or not patterns.is_file()
        or not allowlist.is_file()
    ):
        raise ApplyError("credential patterns and non-empty allowlist are required")
    rules = [
        line.strip()
        for line in patterns.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    allowed = {
        line.strip()
        for line in allowlist.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    if not rules or not allowed:
        raise ApplyError(
            "credential pattern rules and allowlist must both be non-empty"
        )
    for name, content in outputs.items():
        for pattern in rules:
            if (
                re.search(pattern, content.decode("utf-8", errors="replace"))
                and f"{name}\t{pattern}" not in allowed
            ):
                raise ApplyError(
                    f"credential pattern {pattern!r} matched rendered artifact {name}"
                )


def _generation_mode(path: Path, mode: int) -> None:
    """Apply the configured mode recursively, retaining directory traversal.

    Rendered files are read-only (ISSUE-REF D-11 (b)): a live link points straight at them, so a
    writable file would let an edit through the link rewrite the generation that rollback later
    restores. An edit through the link is refused instead.
    """
    read_only = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
    for directory, _dirs, files in os.walk(path):
        os.chmod(directory, mode)
        for name in files:
            os.chmod(Path(directory) / name, mode & read_only)


def _consumer_can_read(root: Path, consumer: str) -> bool:
    """Evaluate the declared principal against mode bits, never caller os.access()."""
    try:
        pwd = importlib.import_module("pwd")
    except ImportError as exc:
        raise ApplyError(
            f"consumer {consumer!r} must be an OS account name; account lookup is unavailable"
        ) from exc
    try:
        account = pwd.getpwnam(consumer)
    except KeyError:
        raise ApplyError(
            f"consumer {consumer!r} must be an OS account name; account does not exist"
        ) from None
    ancestors = [root, *root.parents]
    for path in ancestors:
        info = path.stat()
        if info.st_uid == account.pw_uid:
            permitted = info.st_mode & stat.S_IRUSR and info.st_mode & stat.S_IXUSR
        elif info.st_gid == account.pw_gid:
            permitted = info.st_mode & stat.S_IRGRP and info.st_mode & stat.S_IXGRP
        else:
            permitted = info.st_mode & stat.S_IROTH and info.st_mode & stat.S_IXOTH
        if not permitted:
            return False
    return True


def _verify_consumers(root: Path, targets: list[InstallTarget]) -> None:
    for target in targets:
        if target.consumer and not _consumer_can_read(root, target.consumer):
            achieved = oct(stat.S_IMODE(root.stat().st_mode))
            raise ApplyError(
                f"consumer {target.consumer} cannot read generated tree {root} (mode {achieved})"
            )


def _entry_has_existing_aside(entry: dict[str, object]) -> bool:
    prior_state = entry.get("prior_state")
    return (
        isinstance(prior_state, dict)
        and prior_state.get("kind") == "directory-aside"
        and Path(str(prior_state.get("data"))).exists()
    )


def _unresolved_journal_entries(
    journal: dict[str, object],
) -> list[dict[str, object]]:
    """Return durable target entries that still need aborted-transaction recovery."""
    targets = journal.get("targets", [])
    if not isinstance(targets, list):
        return []
    if journal.get("state") == "committed":
        return []
    return [
        entry
        for entry in targets
        if isinstance(entry, dict)
        and entry.get("restore_outcome") != "restored"
        and (
            entry.get("restore_outcome") == "restore-failed"
            or entry.get("capture_outcome") == "captured"
            or entry.get("reconcile_outcome") == "intent"
            or entry.get("outcome") == "mutated"
            or _entry_has_existing_aside(entry)
        )
    ]


def _journal_has_unresolved_target(journal: dict[str, object]) -> bool:
    """Whether durable journal evidence shows an aborted target still at risk."""
    return bool(_unresolved_journal_entries(journal))


def _journal_needs_recovery(path: Path) -> bool:
    try:
        journal = read_json(path)
    except (FileNotFoundError, ValueError):
        return True
    return _journal_has_unresolved_target(journal)


def _retain(state_root: Path, current: str, retain_previous: int) -> None:
    generations = state_root / "generations"
    protected = {current}
    previous = pointer(state_root, "previous")
    if previous and retain_previous:
        protected.add(previous)
    for child in generations.iterdir() if generations.exists() else ():
        journals = [child / "manifest.json", child / "rollback.json"]
        recovery = any(
            path.exists() and _journal_needs_recovery(path) for path in journals
        )
        if child.is_dir() and child.name not in protected and not recovery:
            shutil.rmtree(child)
        elif child.is_dir() and not recovery:
            shutil.rmtree(child / "aside", ignore_errors=True)


def _stage_aside(
    target: InstallTarget,
    entry: dict[str, object],
    root: Path,
    journal: Path,
    manifest: dict[str, object],
    ordinal: int,
) -> None:
    aside = root / "aside" / str(ordinal)

    def journal_state(state: TargetState) -> None:
        entry["prior_state"] = state.as_dict()
        write_json_fsync(journal, manifest)

    def captured(state: TargetState) -> None:
        entry["capture_outcome"] = "captured"
        write_json_fsync(journal, manifest)

    capture(
        target.path,
        aside,
        before_mutation=journal_state,
        after_capture=captured,
    )


def quarantine_path(state_root: Path, generation: str, target: Path) -> Path:
    """``<state_root>/quarantine/<generation>/<target without its anchor>``, never overwritten."""
    absolute = Path(os.path.abspath(target))
    destination = state_root / "quarantine" / generation / Path(*absolute.parts[1:])
    candidate, counter = destination, 1
    while candidate.exists() or candidate.is_symlink():
        candidate = destination.with_name(f"{destination.name}.{counter}")
        counter += 1
    return candidate


def quarantine(state_root: Path, generation: str, target: Path) -> Path | None:
    """Move a hand-edited target aside (ISSUE-REF D-11 (b)); return where it went.

    A regular file or directory is moved, so the next reconcile finds the path empty. A symlink
    into this state root's generations is a hand-edited generation copy: its bytes are copied out,
    since the generation itself stays retained as the rollback target. Any other symlink, dangling
    or foreign, is recreated as a symlink in the quarantine. An absent target has nothing to keep.
    """
    if not target.is_symlink() and not target.exists():
        return None
    destination = quarantine_path(state_root, generation, target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    generations = (state_root / "generations").resolve()
    if target.is_symlink():
        resolved = target.resolve()
        if (
            target.exists()
            and resolved.is_file()
            and resolved.is_relative_to(generations)
        ):
            shutil.copy2(resolved, destination)
        else:
            destination.symlink_to(os.readlink(target))
        return destination
    shutil.move(str(target), str(destination))
    return destination


def _validate_output_sources(outputs: dict[str, object]) -> None:
    for name, value in outputs.items():
        if isinstance(value, Path) and value.is_dir():
            raise ApplyError(f"AD-2 refuses unsupported directory-source output {name}")
        if not isinstance(value, bytes):
            raise ApplyError(f"rendered output {name} must be bytes")


def _unchanged(
    prior: dict[str, object], outputs: dict[str, bytes], targets: list[InstallTarget]
) -> bool:
    """Whether the committed ``prior`` generation already installs exactly these bytes."""
    entries = prior.get("targets")
    if prior.get("state") != "committed" or not isinstance(entries, list):
        return False
    if len(entries) != len(targets):
        return False
    for entry, target in zip(entries, targets, strict=True):
        if not isinstance(entry, dict):
            return False
        expected = hashlib.sha256(outputs[target.output]).hexdigest()
        if (
            entry.get("output") != target.output
            or entry.get("path") != str(resolve_install_path(target.path))
            or entry.get("requested_kind") != target.requested_kind
            or entry.get("consumer") != target.consumer
            or entry.get("sha256") != expected
            or _hash(target.path) != expected
        ):
            return False
        if entry.get("install_kind") == "symlink" and not (
            target.path.is_symlink()
            and target.path.resolve() == Path(str(entry.get("source"))).resolve()
        ):
            return False
    return True


def apply(
    outputs: dict[str, bytes],
    targets: list[InstallTarget],
    state_root: Path,
    *,
    force: bool = False,
    restrictive_mode: int = 0o700,
    patterns: Path | None = None,
    allowlist: Path | None = None,
    retain_previous: int = 1,
    failure_hook: FailureHook | None = None,
    environment: str | None = None,
    maximum_path_length: int | None = None,
    filesystem_encoding: str | None = None,
    metadata: dict[str, object] | None = None,
    reuse_unchanged: bool = False,
    report: Callable[[str], None] | None = None,
) -> str:
    """Render, journal, reconcile, and finally atomically publish a generation.

    A target that drifted from the current generation (edited by hand) is moved to the quarantine
    and re-rendered, never a refusal (ISSUE-REF D-11 (b)); ``report`` hears each one. With
    ``reuse_unchanged`` and no ``force``, an apply whose outputs and targets equal the current
    generation's, with every target intact, keeps that generation (recording ``metadata`` on it)
    instead of swapping in an identical one, so ``previous`` stays a real rollback target.
    """
    say = report or (lambda line: print(line, file=sys.stderr))
    _validate_output_sources(outputs)
    if not targets:
        raise ApplyError(
            "no install targets resolved; declare [[install.target]] or pass --target"
        )
    if {target.output for target in targets} - outputs.keys():
        raise ApplyError("target references an output that was not rendered")
    active_environment = detect_environment() if environment is None else environment
    for target in targets:
        try:
            validate_install_target(
                target.path,
                active_environment,
                maximum_length=maximum_path_length,
                filesystem_encoding=filesystem_encoding,
            )
        except PathSafetyError as exc:
            raise ApplyError(str(exc)) from exc
    _validate_artifacts(outputs, patterns, allowlist)
    current = pointer(state_root, "current")
    previous = pointer(state_root, "previous")
    if current:
        try:
            prior = read_json(manifest_path(state_root, current))
        except (FileNotFoundError, ValueError) as exc:
            raise ApplyError(
                f"current generation manifest is missing or corrupt: {current}"
            ) from exc
        if reuse_unchanged and not force and _unchanged(prior, outputs, targets):
            if metadata:
                prior.update(metadata)
                write_json_fsync(manifest_path(state_root, current), prior)
            return current
        for entry in prior.get("targets", []):
            path = Path(str(entry["path"]))
            if _hash(path) == entry.get("sha256"):
                continue
            moved = quarantine(state_root, current, path)
            if moved is None:
                say(f"  [drift] {path} is missing; re-rendering it")
            else:
                say(
                    f"  [QUARANTINE] {path} was edited by hand; the drifted copy is at "
                    f"{moved}; re-rendering {path} from the main tree"
                )
    ident = generation_id()
    root = state_root / "generations" / ident
    root.mkdir(parents=True)
    for name, content in outputs.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content)
    _generation_mode(root, restrictive_mode)
    _verify_consumers(root, targets)
    manifest: dict[str, object] = {
        **(metadata or {}),
        "state": "staged",
        "generation_id": ident,
        "targets": _manifest_targets(targets, root),
    }
    journal = manifest_path(state_root, ident)
    write_json_fsync(journal, manifest)
    mutated: list[tuple[InstallTarget, dict[str, object]]] = []
    failed_target: InstallTarget | None = None
    try:
        # strict=True: manifest["targets"] is _manifest_targets(targets, root), so a
        # length mismatch means the manifest no longer describes the targets being
        # staged. Truncating silently would stage a prefix and record the whole set.
        for ordinal, (candidate, entry) in enumerate(
            zip(targets, manifest["targets"], strict=True)
        ):
            assert isinstance(entry, dict)
            failed_target = candidate
            _stage_aside(candidate, entry, root, journal, manifest, ordinal)
            mutated.append((candidate, entry))
            if failure_hook:
                failure_hook("after-capture", candidate)
                failure_hook("reconcile", candidate)
            entry["reconcile_outcome"] = "intent"
            entry["sha256"] = _hash(root / candidate.output)
            write_json_fsync(journal, manifest)
            kind = reconcile(
                root / candidate.output, candidate.path, candidate.requested_kind
            )
            entry["install_kind"], entry["sha256"], entry["outcome"] = (
                kind,
                _hash(candidate.path),
                "mutated",
            )
            write_json_fsync(journal, manifest)
            if kind == "copy" and candidate.requested_kind == "symlink":
                entry["warning"] = "symlink unavailable; installed copy"
                write_json_fsync(journal, manifest)
        if failure_hook:
            failure_hook("before-pointer", InstallTarget("", state_root))
        if current:
            manifest["pointer_transition"] = {
                "old_current": current,
                "old_previous": previous,
                "new_current": ident,
                "new_previous": current,
            }
            write_json_fsync(journal, manifest)
            swap_pointer(state_root, "previous", current)
            if failure_hook:
                failure_hook("after-previous-pointer", InstallTarget("", state_root))
        swap_pointer(state_root, "current", ident)
        if failure_hook:
            failure_hook("after-current-pointer", InstallTarget("", state_root))
        manifest["state"] = "committed"
        write_json_fsync(journal, manifest)
        _retain(state_root, ident, retain_previous)
        return ident
    except (ApplyError, LinkError, OSError) as exc:
        if pointer(state_root, "current") == ident:
            raise ApplyError(
                f"failed to record committed generation {ident}: {exc}"
            ) from exc
        for restored_target, entry in reversed(mutated):
            try:
                if failure_hook:
                    failure_hook("restore", restored_target)
                restore(
                    restored_target.path, TargetState.from_dict(entry["prior_state"])
                )
                entry["restore_outcome"] = "restored"
            except (LinkError, OSError) as restore_exc:
                entry["restore_outcome"], entry["restore_reason"] = (
                    "restore-failed",
                    str(restore_exc),
                )
            write_json_fsync(journal, manifest)
        if not _journal_has_unresolved_target(manifest):
            apply_transition = _apply_pointer_transition(state_root, manifest)
            if (
                apply_transition is not None
                and pointer(state_root, "current") == apply_transition[0]
                and pointer(state_root, "previous") == apply_transition[3]
            ):
                _restore_apply_pointers(
                    state_root, apply_transition[0], apply_transition[1]
                )
            manifest["state"] = "aborted-restored"
            write_json_fsync(journal, manifest)
        failed = failed_target.path if failed_target else "<journal>"
        raise ApplyError(f"failed target {failed}: {exc}") from exc


def _matches_prior_state(observed: TargetState, prior: TargetState) -> bool:
    """Compare an observation to a captured state without hash sentinels."""
    if prior.kind == "directory-aside":
        return observed.kind == "directory" and not Path(str(prior.data)).exists()
    return observed == prior


def _matches_captured_state(observed: TargetState, prior: TargetState) -> bool:
    """Recognize the state after a directory was moved aside, before reconcile."""
    if prior.kind == "directory-aside":
        return observed.kind == "absent" and Path(str(prior.data)).exists()
    return observed == prior


def _targets_match_applied(journal: dict[str, object]) -> bool:
    """Whether every durable target observation is the intended post-state."""
    targets = journal.get("targets")
    return (
        isinstance(targets, list)
        and bool(targets)
        and all(isinstance(entry, dict) and _entry_applied(entry) for entry in targets)
    )


def _entry_applied(entry: dict[str, object]) -> bool:
    path = Path(str(entry.get("path")))
    if entry.get("expected_absent") is True:
        return not path.exists() and not path.is_symlink()
    return isinstance(entry.get("sha256"), str) and _hash(path) == entry["sha256"]


def _verify_generation_files(
    state_root: Path, generation: str, manifest: dict[str, object]
) -> None:
    """Refuse to re-link a generation whose files no longer match their recorded digests."""
    root = manifest_path(state_root, generation).parent
    for entry in manifest.get("targets", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str):
            continue
        source = root / str(entry["output"])
        try:
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
        except OSError:
            actual = None
        if actual != entry["sha256"]:
            raise ApplyError(
                f"generation {generation} file {source} does not match its manifest digest, "
                "so it was modified after it was rendered; rolling back would install the "
                "modified bytes. Re-render instead: ai-harness apply"
            )


def _apply_pointer_transition(
    state_root: Path, journal: dict[str, object]
) -> tuple[str, str | None, str, str] | None:
    transition = journal.get("pointer_transition")
    if not isinstance(transition, dict):
        return None
    old_current = transition.get("old_current")
    old_previous = transition.get("old_previous")
    new_current = transition.get("new_current")
    new_previous = transition.get("new_previous")
    if (
        not isinstance(old_current, str)
        or not old_current
        or not (old_previous is None or isinstance(old_previous, str) and old_previous)
        or not isinstance(new_current, str)
        or not new_current
        or not isinstance(new_previous, str)
        or not new_previous
        or journal.get("generation_id") != new_current
        or new_previous != old_current
    ):
        return None
    try:
        read_json(manifest_path(state_root, old_current))
        if old_previous is not None:
            read_json(manifest_path(state_root, old_previous))
    except (FileNotFoundError, ValueError):
        return None
    return old_current, old_previous, new_current, new_previous


def _restore_apply_pointers(
    state_root: Path, old_current: str, old_previous: str | None
) -> None:
    swap_pointer(state_root, "current", old_current)
    if old_previous is None:
        clear_pointer(state_root, "previous")
    else:
        swap_pointer(state_root, "previous", old_previous)


def _rollback_new_pointers(
    state_root: Path, journal: dict[str, object]
) -> tuple[str, str] | None:
    transition = journal.get("pointer_transition")
    if not isinstance(transition, dict):
        return None
    old_current = transition.get("old_current")
    old_previous = transition.get("old_previous")
    current = transition.get("new_current")
    previous = transition.get("new_previous")
    if not all(
        isinstance(value, str) and value
        for value in (old_current, old_previous, current, previous)
    ):
        return None
    if (
        old_current != journal.get("generation_id")
        or previous != old_current
        or current != old_previous
    ):
        return None
    try:
        for generation in (old_current, old_previous, current, previous):
            read_json(manifest_path(state_root, generation))
    except (FileNotFoundError, ValueError):
        return None
    return current, previous


def _staged_journal_is_current(
    path: Path, state_root: Path, journal: dict[str, object], current: str | None
) -> bool:
    if path.name == "manifest.json":
        return current == journal.get("generation_id")
    new_pointers = _rollback_new_pointers(state_root, journal)
    return new_pointers is not None and current == new_pointers[0]


def _scan_journal(
    path: Path, state_root: Path, *, repair: bool
) -> tuple[list[str], int]:
    """Inspect and optionally repair one apply or rollback recovery journal."""
    try:
        journal = read_json(path)
    except (FileNotFoundError, ValueError) as exc:
        raise ApplyError(f"generation journal is missing or corrupt: {path}") from exc
    messages: list[str] = []
    current = pointer(state_root, "current")
    previous = pointer(state_root, "previous")
    rollback_pointers = _rollback_new_pointers(state_root, journal)
    apply_transition = _apply_pointer_transition(state_root, journal)
    staged_current = journal.get("state") == "staged" and _staged_journal_is_current(
        path, state_root, journal, current
    )
    if (
        journal.get("state") == "staged"
        and path.name == "rollback.json"
        and rollback_pointers is None
    ):
        messages.append(f"interrupted rollback: {journal['generation_id']}")
        messages.append("rollback pointer transition is unavailable; not repaired")
        return messages, 1
    if (
        journal.get("state") == "staged"
        and path.name == "manifest.json"
        and "pointer_transition" in journal
        and apply_transition is None
    ):
        messages.append(f"interrupted apply: {journal['generation_id']}")
        messages.append("apply pointer transition is invalid; not repaired")
        return messages, 1
    if staged_current:
        operation = "rollback" if path.name == "rollback.json" else "apply"
        messages.append(f"interrupted {operation}: {journal['generation_id']}")
        if not _targets_match_applied(journal):
            messages.append(
                "current generation targets do not match the staged journal"
            )
            return messages, 1
        if repair:
            if path.name == "rollback.json":
                new_pointers = rollback_pointers
                assert new_pointers is not None
                swap_pointer(state_root, "current", new_pointers[0])
                swap_pointer(state_root, "previous", new_pointers[1])
            elif apply_transition is not None:
                _old_current, _old_previous, new_current, new_previous = (
                    apply_transition
                )
                swap_pointer(state_root, "current", new_current)
                swap_pointer(state_root, "previous", new_previous)
            journal["state"] = "committed"
            write_json_fsync(path, journal)
            return messages, 0
        return messages, 1
    recovery_entries = _unresolved_journal_entries(journal)
    if journal.get("state") == "staged":
        operation = "rollback" if path.name == "rollback.json" else "apply"
        messages.append(f"interrupted {operation}: {journal['generation_id']}")
    unresolved = 0
    restore_apply_pointers = (
        apply_transition is not None
        and current == apply_transition[0]
        and previous in (apply_transition[1], apply_transition[3])
    )
    for entry in recovery_entries:
        target = Path(str(entry["path"]))
        prior = TargetState.from_dict(entry["prior_state"])
        applied = entry.get("sha256")
        observed = _hash(target)
        observed_state = observe(target)
        matches_prior = _matches_prior_state(observed_state, prior)
        matches_captured = _matches_captured_state(observed_state, prior)
        matches_applied = isinstance(applied, str) and observed == applied
        if repair and (matches_applied or matches_captured or matches_prior):
            try:
                restore(target, prior)
                entry["restore_outcome"] = "restored"
                write_json_fsync(path, journal)
                continue
            except (LinkError, OSError) as exc:
                entry["restore_reason"] = str(exc)
                write_json_fsync(path, journal)
        unresolved += 1
        if repair and not (matches_applied or matches_captured or matches_prior):
            messages.append(f"user-modified {target}; not repaired")
        else:
            messages.append(
                f"unrestored {target}: prior={entry['prior_state']} observed={observed_state.as_dict()}"
            )
    if journal.get("state") == "staged" and not _journal_has_unresolved_target(journal):
        if repair and restore_apply_pointers:
            _restore_apply_pointers(
                state_root, apply_transition[0], apply_transition[1]
            )
        journal["state"] = "aborted-restored"
        write_json_fsync(path, journal)
    return messages, unresolved


def doctor(
    state_root: Path, *, repair: bool = False, environment: str | None = None
) -> DoctorResult:
    """Report interrupted apply/rollback journals and repair untouched targets."""
    current = pointer(state_root, "current")
    active_environment = detect_environment() if environment is None else environment
    messages: list[str] = [
        f"environment: {active_environment} ({support_label(active_environment)})"
    ]
    unresolved = 0
    generations = state_root / "generations"
    candidates = (
        sorted(
            [*generations.glob("*/manifest.json"), *generations.glob("*/rollback.json")]
        )
        if generations.exists()
        else ()
    )
    for candidate in candidates:
        journal_messages, journal_unresolved = _scan_journal(
            candidate, state_root, repair=repair
        )
        messages.extend(journal_messages)
        unresolved += journal_unresolved
    if current:
        messages.insert(0, f"current generation: {current}")
        try:
            committed = read_json(manifest_path(state_root, current))
        except (FileNotFoundError, ValueError) as exc:
            raise ApplyError(
                f"current generation manifest is missing or corrupt: {current}"
            ) from exc
        root = manifest_path(state_root, current).parent
        for entry in committed.get("targets", []):
            consumer = entry.get("consumer")
            if isinstance(consumer, str):
                try:
                    readable = _consumer_can_read(root, consumer)
                except ApplyError as exc:
                    unresolved += 1
                    messages.append(str(exc))
                    continue
                if not readable:
                    unresolved += 1
                    achieved = oct(stat.S_IMODE(root.stat().st_mode))
                    messages.append(
                        f"consumer {consumer} cannot read {root} (mode {achieved})"
                    )
    return DoctorResult(tuple(messages), unresolved)


def rollback(state_root: Path, *, failure_hook: FailureHook | None = None) -> str:
    """Journalled rollback that leaves pointers unmoved if reconciliation fails."""
    current, previous = pointer(state_root, "current"), pointer(state_root, "previous")
    if not current or not previous:
        raise ApplyError("no previous generation exists")
    try:
        manifest = read_json(manifest_path(state_root, previous))
    except (FileNotFoundError, ValueError) as exc:
        raise ApplyError(
            f"previous generation manifest is missing or corrupt: {previous}"
        ) from exc
    _verify_generation_files(state_root, previous, manifest)
    try:
        current_manifest = read_json(manifest_path(state_root, current))
    except (FileNotFoundError, ValueError) as exc:
        raise ApplyError(
            f"current generation manifest is missing or corrupt: {current}"
        ) from exc
    previous_paths = {str(entry["path"]) for entry in manifest.get("targets", [])}
    retired = [
        entry
        for entry in current_manifest.get("targets", [])
        if isinstance(entry, dict) and str(entry.get("path")) not in previous_paths
    ]
    journal_path = state_root / "generations" / current / "rollback.json"
    journal: dict[str, object] = {
        "state": "staged",
        "generation_id": current,
        "pointer_transition": {
            "old_current": current,
            "old_previous": previous,
            "new_current": previous,
            "new_previous": current,
        },
        "targets": [],
    }
    write_json_fsync(journal_path, journal)
    mutated: list[tuple[InstallTarget, dict[str, object]]] = []
    failed: InstallTarget | None = None
    try:
        for entry in manifest.get("targets", []):
            target = InstallTarget(
                str(entry["output"]),
                Path(str(entry["path"])),
                str(entry.get("requested_kind", "symlink")),
                entry.get("consumer")
                if isinstance(entry.get("consumer"), str)
                else None,
            )
            rollback_entry: dict[str, object] = {
                "output": target.output,
                "path": str(target.path),
            }
            journal["targets"].append(rollback_entry)
            ordinal = len(journal["targets"]) - 1
            failed = target

            def journal_state(
                state: TargetState, captured_entry: dict[str, object] = rollback_entry
            ) -> None:
                captured_entry["prior_state"] = state.as_dict()
                write_json_fsync(journal_path, journal)

            def captured(
                _state: TargetState,
                captured_entry: dict[str, object] = rollback_entry,
            ) -> None:
                captured_entry["capture_outcome"] = "captured"
                write_json_fsync(journal_path, journal)

            capture(
                target.path,
                journal_path.parent / "rollback-aside" / str(ordinal),
                before_mutation=journal_state,
                after_capture=captured,
            )
            mutated.append((target, rollback_entry))
            if failure_hook:
                failure_hook("after-capture", target)
                failure_hook("rollback-reconcile", target)
            source = manifest_path(state_root, previous).parent / target.output
            rollback_entry["reconcile_outcome"] = "intent"
            rollback_entry["sha256"] = _hash(source)
            write_json_fsync(journal_path, journal)
            reconcile(source, target.path, target.requested_kind)
            rollback_entry["outcome"] = "mutated"
            rollback_entry["sha256"] = _hash(target.path)
            write_json_fsync(journal_path, journal)
        current_root = manifest_path(state_root, current).parent.resolve()
        for entry in retired:
            # A target only the current generation installed has no previous copy to link
            # to, so it goes: left in place it would keep pointing at a non-current generation.
            target = InstallTarget(str(entry["output"]), Path(str(entry["path"])))
            ours = (
                target.path.is_symlink()
                and target.path.resolve().is_relative_to(current_root)
            ) or (
                not target.path.is_symlink()
                and _hash(target.path) == entry.get("sha256")
            )
            if not ours:
                continue
            rollback_entry = {
                "output": target.output,
                "path": str(target.path),
                "expected_absent": True,
            }
            journal["targets"].append(rollback_entry)
            failed = target
            rollback_entry["prior_state"] = observe(target.path).as_dict()
            rollback_entry["capture_outcome"] = "captured"
            rollback_entry["reconcile_outcome"] = "intent"
            write_json_fsync(journal_path, journal)
            mutated.append((target, rollback_entry))
            if failure_hook:
                failure_hook("rollback-retire", target)
            restore(target.path, TargetState("absent"))
            rollback_entry["outcome"] = "mutated"
            write_json_fsync(journal_path, journal)
        swap_pointer(state_root, "current", previous)
        if failure_hook:
            failure_hook(
                "after-rollback-current-pointer", InstallTarget("", state_root)
            )
        swap_pointer(state_root, "previous", current)
        if failure_hook:
            failure_hook(
                "after-rollback-previous-pointer", InstallTarget("", state_root)
            )
        journal["state"] = "committed"
        write_json_fsync(journal_path, journal)
        shutil.rmtree(journal_path.parent / "rollback-aside", ignore_errors=True)
        return previous
    except (ApplyError, LinkError, OSError) as exc:
        pointers = _rollback_new_pointers(state_root, journal)
        if pointers is not None and pointer(state_root, "current") == pointers[0]:
            raise ApplyError(
                f"failed to record committed rollback {current}: {exc}"
            ) from exc
        for target, entry in reversed(mutated):
            try:
                if failure_hook:
                    failure_hook("rollback-restore", target)
                restore(target.path, TargetState.from_dict(entry["prior_state"]))
                entry["restore_outcome"] = "restored"
            except (LinkError, OSError) as restore_exc:
                entry["restore_outcome"], entry["restore_reason"] = (
                    "restore-failed",
                    str(restore_exc),
                )
            write_json_fsync(journal_path, journal)
        if not _journal_has_unresolved_target(journal):
            journal["state"] = "aborted-restored"
            write_json_fsync(journal_path, journal)
        raise ApplyError(
            f"rollback failed target {failed.path if failed else '<journal>'}: {exc}"
        ) from exc
