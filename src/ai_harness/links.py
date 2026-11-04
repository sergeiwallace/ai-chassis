"""Filesystem link reconciliation primitives used by the apply pipeline."""

from __future__ import annotations

import base64
import errno
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class LinkError(RuntimeError):
    """A target could not be reconciled safely."""


@dataclass(frozen=True)
class TargetState:
    kind: str
    data: bytes | str | None = None

    def as_dict(self) -> dict[str, object]:
        data: object = self.data
        if isinstance(data, bytes):
            data = {"base64": base64.b64encode(data).decode("ascii")}
        return {"kind": self.kind, "data": data}

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> TargetState:
        data = value.get("data")
        if isinstance(data, dict) and isinstance(data.get("base64"), str):
            data = base64.b64decode(data["base64"])
        return cls(str(value["kind"]), data)  # type: ignore[arg-type]


def observe(path: Path) -> TargetState:
    """Capture a target without dereferencing a symlink."""
    if path.is_symlink():
        return TargetState("symlink", os.readlink(path))
    if not path.exists():
        return TargetState("absent")
    if path.is_dir():
        return TargetState("directory")
    return TargetState("file", path.read_bytes())


def capture(
    path: Path,
    aside_root: Path,
    *,
    before_mutation: Callable[[TargetState], None] | None = None,
    after_capture: Callable[[TargetState], None] | None = None,
) -> TargetState:
    """Capture prior state, moving directories aside after journalling the move."""
    state = observe(path)
    if state.kind != "directory":
        if before_mutation:
            before_mutation(state)
        return state
    aside = aside_root
    prior = TargetState("directory-aside", str(aside))
    if before_mutation:
        before_mutation(prior)
    aside.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(path, aside)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise LinkError(
                f"cannot move directory target aside: {path}: {exc}"
            ) from exc
        try:
            shutil.copytree(path, aside, symlinks=True)
        except OSError as copy_error:
            shutil.rmtree(aside, ignore_errors=True)
            raise LinkError(
                f"AD-2 refuses directory target before mutation: {path}: {copy_error}"
            ) from copy_error
        try:
            shutil.rmtree(path)
        except OSError as remove_error:
            shutil.rmtree(aside, ignore_errors=True)
            raise LinkError(
                f"AD-2 refuses directory target before mutation: {path}: {remove_error}"
            ) from remove_error
    if after_capture:
        after_capture(prior)
    return prior


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def restore(path: Path, state: TargetState) -> None:
    """Restore a state captured by :func:`observe`."""
    if state.kind == "directory-aside":
        aside = Path(str(state.data))
        if not aside.exists():
            # A completed restore consumes the aside.  In particular, recovery
            # may be resuming after the rename but before its journal marker.
            if not path.is_symlink() and path.is_dir():
                return
            raise LinkError(
                f"directory aside is missing and target was not restored: {path}"
            )
        _remove(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(aside, path)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            try:
                shutil.copytree(aside, path, symlinks=True)
                shutil.rmtree(aside)
            except OSError as copy_error:
                _remove(path)
                raise LinkError(
                    f"cannot restore directory aside {aside}: {copy_error}"
                ) from copy_error
        return
    _remove(path)
    if state.kind == "absent":
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if state.kind == "symlink":
        path.symlink_to(str(state.data))
    elif state.kind == "file":
        path.write_bytes(bytes(state.data))
    elif state.kind == "directory":
        path.mkdir()
    else:
        raise LinkError(f"unknown prior target kind {state.kind!r}: {path}")


def reconcile(source: Path, target: Path, requested_kind: str = "symlink") -> str:
    """Install source at target, returning the achieved portable kind.

    This never makes a junction: a requested ``junction`` is made as a symlink and
    recorded as one, on every platform (ISSUE-REF).  A Windows directory junction is
    queued through ``profile.queue_windows_junction`` instead.  File links degrade from
    symlink to hardlink to copy, so the manifest always describes reality.
    """
    source = source.resolve(strict=True)
    if target.resolve(strict=False) == source:
        raise LinkError(
            f"refusing self-clobber: target {target} equals source {source}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    _remove(target)
    if requested_kind == "copy":
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
        return "copy"
    try:
        target.symlink_to(source, target_is_directory=source.is_dir())
        return "symlink"
    except OSError:
        if source.is_dir():
            shutil.copytree(source, target)
            return "copy"
        try:
            os.link(source, target)
            return "hardlink"
        except OSError:
            shutil.copy2(source, target)
            return "copy"
