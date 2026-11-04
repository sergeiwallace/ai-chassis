"""Durable generation manifests and pointer management."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

# Windows has no stdlib directory descriptor; file contents are still fsynced.
_DIRECTORY_DESCRIPTORS_EXIST = os.name != "nt"


def _fsync_directory(directory: Path) -> None:
    if not _DIRECTORY_DESCRIPTORS_EXIST:
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def generation_id() -> str:
    return uuid.uuid4().hex


def write_json_fsync(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def pointer(state_root: Path, name: str) -> str | None:
    path = state_root / name
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def swap_pointer(state_root: Path, name: str, value: str) -> None:
    """Atomically publish a pointer only after all preceding work is durable."""
    path = state_root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(value + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def clear_pointer(state_root: Path, name: str) -> None:
    """Durably remove an optional pointer during transaction recovery."""
    path = state_root / name
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def manifest_path(state_root: Path, generation: str) -> Path:
    return state_root / "generations" / generation / "manifest.json"
