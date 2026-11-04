#!/usr/bin/env python3
"""Capture and fail-closed restore a pilot's pre-existing pi state.

Hard links are restored as independent files; sockets, FIFOs, and other special
file types fail the snapshot closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

MANIFEST_NAME = "pi-pilot-manifest.json"
SNAPSHOT_NAME = "pi-pilot-preflight-snapshot"


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _metadata(path: Path, relative: str) -> dict[str, object]:
    status = os.lstat(path)
    mode = stat.S_IMODE(status.st_mode)
    if stat.S_ISREG(status.st_mode):
        kind = "regular"
    elif stat.S_ISDIR(status.st_mode):
        kind = "directory"
    elif stat.S_ISLNK(status.st_mode):
        kind = "symlink"
    else:
        raise ValueError(f"unsupported path type: {relative}")
    item: dict[str, object] = {
        "path": relative,
        "type": kind,
        "mode": mode,
        "uid": status.st_uid,
        "gid": status.st_gid,
        "mtime_ns": status.st_mtime_ns,
    }
    if kind == "regular":
        item["sha256"] = _digest(path)
        item["size"] = status.st_size
    if kind == "symlink":
        item["target"] = os.readlink(path)
    return item


def _canonical_digest(manifest: dict[str, object]) -> str:
    copy = dict(manifest)
    copy.pop("manifest_digest", None)
    raw = json.dumps(copy, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mkdir_private(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    manifest["manifest_digest"] = _canonical_digest(manifest)
    _mkdir_private(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".pi-pilot-manifest-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(manifest, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _valid_relative(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _is_a_git_marker(candidate: Path) -> bool:
    """Whether ``candidate`` is a real ``.git``, not merely something with that name.

    A bare DIRECTORY named ``.git`` is not a repository — git itself reports "not a git
    repository" for one. Treating the name alone as proof made this refusal fire on any
    path under a directory where something had left an empty ``.git`` behind; measured on
    On an example host, a codex sandbox left ``/tmp/.git`` empty and 23 tests in
    this script's own suite refused to snapshot into ``/tmp``. A primary checkout always
    has ``HEAD``; a linked worktree or submodule uses a ``.git`` FILE pointing at its
    gitdir, which is why the file case needs no further proof.
    """
    if candidate.is_symlink():
        return True
    if candidate.is_file():
        return True
    if candidate.is_dir():
        return (candidate / "HEAD").is_file()
    return False


def _is_in_git_work_tree(path: Path) -> bool:
    current = path.resolve()
    while True:
        if _is_a_git_marker(current / ".git"):
            return True
        if current.parent == current:
            return False
        current = current.parent


def _validate_record_destination(
    root: Path, manifest_path: Path, snapshot_dir: Path
) -> str | None:
    if _is_within(manifest_path, root) or _is_within(snapshot_dir, root):
        return "record destination is inside target root"
    if _is_in_git_work_tree(manifest_path.parent) or _is_in_git_work_tree(
        snapshot_dir.parent
    ):
        return "record destination is inside a git work tree"
    return None


def _walk_inventory(root: Path) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    if root.is_symlink():
        return [_metadata(root, ".")]
    for current, directories, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(root).as_posix()
        inventory.append(_metadata(current_path, relative_dir or "."))
        for name in list(directories):
            candidate = current_path / name
            if candidate.is_symlink():
                directories.remove(name)
                inventory.append(
                    _metadata(candidate, candidate.relative_to(root).as_posix())
                )
        for name in names:
            candidate = current_path / name
            inventory.append(
                _metadata(candidate, candidate.relative_to(root).as_posix())
            )
    return inventory


def snapshot(root: Path, manifest_path: Path, snapshot_dir: Path) -> int:
    destination_error = _validate_record_destination(root, manifest_path, snapshot_dir)
    if destination_error:
        print(f"refusing snapshot: {destination_error}", file=sys.stderr)
        return 1
    inventory: list[dict[str, object]] = []
    root_existed = root.exists() or root.is_symlink()
    temporary: Path | None = None
    try:
        _mkdir_private(snapshot_dir.parent)
        temporary = Path(
            tempfile.mkdtemp(prefix=".pi-pilot-snapshot-", dir=snapshot_dir.parent)
        )
        os.chmod(temporary, 0o700)
        files = temporary / "files"
        _mkdir_private(files)
        if root_existed:
            inventory = _walk_inventory(root)
            for item in inventory:
                if item["type"] != "regular":
                    continue
                relative = str(item["path"])
                destination = files / relative
                if not _is_within(destination, temporary):
                    raise ValueError(f"snapshot path escapes root: {relative}")
                _mkdir_private(destination.parent)
                shutil.copyfile(root / relative, destination, follow_symlinks=False)
                os.chmod(destination, int(item["mode"]))
                if _digest(destination) != item["sha256"]:
                    raise ValueError(f"snapshot digest mismatch: {relative}")
        manifest = {
            "version": 1,
            "root": str(root.resolve()),
            "root_existed": root_existed,
            "snapshot_dir": str(snapshot_dir.resolve()),
            "preflight_inventory": sorted(
                inventory, key=lambda item: str(item["path"])
            ),
            "creation_manifest": [],
            "created_recorded": False,
        }
        if snapshot_dir.exists() or snapshot_dir.is_symlink():
            raise ValueError(f"snapshot directory already exists: {snapshot_dir}")
        os.replace(temporary, snapshot_dir)
        temporary = None
        _fsync_directory(snapshot_dir.parent)
        _write_manifest(manifest_path, manifest)
    except (OSError, ValueError) as exc:
        print(f"snapshot failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
    print(f"snapshot recorded {len(inventory)} paths at {manifest_path}")
    return 0


def _load_and_verify(
    manifest_path: Path, snapshot_dir: Path
) -> tuple[dict[str, object] | None, str | None, list[str]]:
    if not manifest_path.is_file():
        return None, f"manifest invalid or missing: missing {manifest_path}", []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, f"manifest invalid or missing: unreadable {manifest_path}", []
    if not isinstance(manifest, dict):
        return None, f"manifest invalid or missing: malformed {manifest_path}", []
    if manifest.get("manifest_digest") != _canonical_digest(manifest):
        return None, f"manifest invalid or missing: digest mismatch {manifest_path}", []
    inventory = manifest.get("preflight_inventory")
    created = manifest.get("creation_manifest")
    if not isinstance(inventory, list) or not isinstance(created, list):
        return None, f"manifest invalid or missing: malformed {manifest_path}", []
    paths = [item.get("path") for item in inventory if isinstance(item, dict)]
    if (
        len(paths) != len(inventory)
        or len(set(paths)) != len(paths)
        or not all(_valid_relative(path) for path in paths)
        or not all(_valid_relative(path) for path in created)
        or not isinstance(manifest.get("root_existed"), bool)
        or not isinstance(manifest.get("created_recorded"), bool)
    ):
        return None, f"manifest invalid or missing: malformed {manifest_path}", []
    if len(set(created)) != len(created) or set(created) & set(paths):
        return None, f"manifest invalid or missing: malformed {manifest_path}", []
    if any(
        inventory_path != created_path and inventory_path.startswith(f"{created_path}/")
        for created_path in created
        for inventory_path in paths
    ):
        return None, f"manifest invalid or missing: malformed {manifest_path}", []
    if not snapshot_dir.is_dir() or not (snapshot_dir / "files").is_dir():
        return None, f"snapshot missing: {snapshot_dir}", []
    problems: list[str] = []
    for item in inventory:
        if not isinstance(item, dict) or item.get("type") not in {
            "regular",
            "directory",
            "symlink",
        }:
            return None, f"manifest invalid or missing: malformed {manifest_path}", []
        if item["type"] == "regular":
            stored = snapshot_dir / "files" / str(item["path"])
            if not _is_within(stored, snapshot_dir):
                return (
                    None,
                    f"manifest invalid or missing: malformed {manifest_path}",
                    [],
                )
            try:
                if not stored.is_file() or _digest(stored) != item.get("sha256"):
                    problems.append(str(item["path"]))
            except OSError:
                problems.append(str(item["path"]))
    return manifest, None, problems


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _live_paths(root: Path) -> list[str]:
    if not (root.exists() or root.is_symlink()):
        return []
    return [str(item["path"]) for item in _walk_inventory(root)]


def _confirm(paths: list[str], yes: bool) -> bool:
    print("prospective deletion list:")
    for path in paths:
        print(path)
    if yes:
        return True
    if not sys.stdin.isatty():
        print(
            "record-created aborted: confirmation required (use --yes)", file=sys.stderr
        )
        return False
    return input("Type yes to record these paths: ") == "yes"


def record_created(
    root: Path, manifest_path: Path, snapshot_dir: Path, yes: bool
) -> int:
    """Diff the live root against preflight state and record pilot-created paths."""
    manifest, reason, problems = _load_and_verify(manifest_path, snapshot_dir)
    if reason:
        print(f"record-created aborted; {reason}", file=sys.stderr)
        return 1
    if problems or manifest is None:
        print(
            "record-created aborted; paths cannot be restored: " + ", ".join(problems),
            file=sys.stderr,
        )
        return 1
    inventory = {str(item["path"]) for item in manifest["preflight_inventory"]}
    created = sorted(path for path in _live_paths(root) if path not in inventory)
    if not _confirm(created, yes):
        return 1
    manifest["creation_manifest"] = created
    manifest["created_recorded"] = True
    _write_manifest(manifest_path, manifest)
    print(f"recorded {len(created)} pilot-created paths")
    return 0


def _apply_metadata(path: Path, item: dict[str, object], reports: list[str]) -> None:
    if path.is_symlink():
        if hasattr(os, "lchmod"):
            os.lchmod(path, int(item["mode"]))
        elif os.chmod in os.supports_follow_symlinks:
            os.chmod(path, int(item["mode"]), follow_symlinks=False)
        else:
            reports.append(
                "symlink mode parity omitted: platform cannot apply symlink modes"
            )
    else:
        os.chmod(path, int(item["mode"]))
    # uid/gid restoration is deliberately attempted despite being untestable without privilege.
    try:
        os.chown(path, int(item["uid"]), int(item["gid"]), follow_symlinks=False)
    except PermissionError:
        reports.append(f"ownership not restored (permission denied): {item['path']}")
    os.utime(
        path, ns=(int(item["mtime_ns"]), int(item["mtime_ns"])), follow_symlinks=False
    )


def _restore_item(
    root: Path, snapshot_dir: Path, item: dict[str, object], reports: list[str]
) -> None:
    relative = str(item["path"])
    destination = root if relative == "." else root / relative
    if not _is_within(destination, root):
        raise ValueError(f"restore path escapes root: {relative}")
    kind = item["type"]
    if kind == "directory":
        if (destination.exists() or destination.is_symlink()) and (
            not destination.is_dir() or destination.is_symlink()
        ):
            _remove(destination)
        destination.mkdir(parents=True, exist_ok=True)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        if destination.exists() or destination.is_symlink():
            _remove(destination)
        os.symlink(str(item["target"]), destination)
        _apply_metadata(destination, item, reports)
        return
    source = snapshot_dir / "files" / relative
    if not _is_within(source, snapshot_dir):
        raise ValueError(f"snapshot path escapes root: {relative}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".pi-pilot-", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
        _apply_metadata(temporary, item, reports)
        if (
            destination.exists()
            and destination.is_dir()
            and not destination.is_symlink()
        ):
            _remove(destination)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _parity(root: Path, inventory: list[dict[str, object]]) -> list[str]:
    problems: list[str] = []
    for item in inventory:
        path = root if item["path"] == "." else root / str(item["path"])
        try:
            actual = _metadata(path, str(item["path"]))
        except (OSError, ValueError):
            problems.append(str(item["path"]))
            continue
        keys = ("type", "mode", "uid", "gid", "mtime_ns", "target", "sha256")
        if item["type"] == "symlink" and not (
            hasattr(os, "lchmod") or os.chmod in os.supports_follow_symlinks
        ):
            keys = tuple(key for key in keys if key != "mode")
        if any(key in item and actual.get(key) != item[key] for key in keys):
            problems.append(str(item["path"]))
    return problems


def restore(root: Path, manifest_path: Path, snapshot_dir: Path) -> int:
    manifest, reason, problems = _load_and_verify(manifest_path, snapshot_dir)
    if reason:
        print(f"restore aborted; {reason}", file=sys.stderr)
        return 1
    if problems or manifest is None:
        print(
            "restore aborted; paths cannot be restored: " + ", ".join(problems),
            file=sys.stderr,
        )
        return 1
    inventory = manifest["preflight_inventory"]
    created = manifest["creation_manifest"]
    if not manifest["created_recorded"]:
        print(
            "restore aborted; creation manifest has not been recorded",
            file=sys.stderr,
        )
        return 1
    reports: list[str] = []
    try:
        for relative in sorted(
            created, key=lambda value: len(Path(value).parts), reverse=True
        ):
            candidate = root if relative == "." else root / relative
            if candidate.exists() or candidate.is_symlink():
                _remove(candidate)
        ordered = sorted(
            inventory,
            key=lambda item: (
                str(item["path"]) != ".",
                len(Path(str(item["path"])).parts),
            ),
        )
        for item in ordered:
            _restore_item(root, snapshot_dir, item, reports)
        for item in sorted(
            inventory,
            key=lambda value: len(Path(str(value["path"])).parts),
            reverse=True,
        ):
            path = root if item["path"] == "." else root / str(item["path"])
            _apply_metadata(path, item, reports)
    except (OSError, ValueError) as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1
    parity = _parity(root, inventory)
    if parity:
        print("restore parity failed: " + ", ".join(parity), file=sys.stderr)
        return 1
    for report in dict.fromkeys(reports):
        print(report, file=sys.stderr)
    print(f"restore verified {len(inventory)} paths")
    return 0


def destroy(manifest_path: Path, snapshot_dir: Path) -> int:
    if not manifest_path.is_file():
        print(
            f"destroy aborted; manifest invalid or missing: missing {manifest_path}",
            file=sys.stderr,
        )
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(
            f"destroy aborted; manifest invalid or missing: unreadable {manifest_path}",
            file=sys.stderr,
        )
        return 1
    if not isinstance(manifest, dict):
        print(
            f"destroy aborted; manifest invalid or missing: malformed {manifest_path}",
            file=sys.stderr,
        )
        return 1
    if manifest.get("manifest_digest") != _canonical_digest(manifest):
        print(
            f"destroy aborted; manifest invalid or missing: digest mismatch {manifest_path}",
            file=sys.stderr,
        )
        return 1
    if manifest.get("snapshot_dir") != str(snapshot_dir.resolve()):
        print(
            "destroy aborted; snapshot root is not the recorded snapshot root",
            file=sys.stderr,
        )
        return 1
    if not (snapshot_dir / "files").is_dir():
        print(f"destroy aborted; snapshot missing: {snapshot_dir}", file=sys.stderr)
        return 1
    try:
        shutil.rmtree(snapshot_dir)
        manifest_path.unlink()
    except OSError as exc:
        print(f"destroy failed: {exc}", file=sys.stderr)
        return 1
    print(f"destroyed snapshot tree {snapshot_dir} and manifest {manifest_path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("snapshot", "record-created", "restore", "destroy"):
        command = commands.add_parser(name)
        if name != "destroy":
            command.add_argument("--root", type=Path, required=True)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--snapshot-dir", type=Path, required=True)
        if name == "record-created":
            command.add_argument("--yes", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "snapshot":
        return snapshot(args.root, args.manifest, args.snapshot_dir)
    if args.command == "record-created":
        return record_created(args.root, args.manifest, args.snapshot_dir, args.yes)
    if args.command == "restore":
        return restore(args.root, args.manifest, args.snapshot_dir)
    return destroy(args.manifest, args.snapshot_dir)


if __name__ == "__main__":
    sys.exit(main())
