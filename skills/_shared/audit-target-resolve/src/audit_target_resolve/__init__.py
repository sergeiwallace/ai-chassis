"""Normalize one explicit existing regular-file target for audit skills."""

from __future__ import annotations

from pathlib import Path


class TargetResolutionError(ValueError):
    """An explicit audit target that cannot safely enter the audit workflow."""


def resolve_explicit_target(
    positional_path: str | None, file_path: str | None, *, base_dir: Path
) -> dict[str, str]:
    """Return one canonical existing regular-file target and nothing else."""
    if positional_path is None and file_path is None:
        raise TargetResolutionError(
            "provide exactly one explicit target path or --file PATH"
        )
    if positional_path is not None and file_path is not None:
        raise TargetResolutionError("provide either PATH or --file PATH, not both")

    supplied = positional_path if positional_path is not None else file_path
    assert supplied is not None
    candidate = Path(supplied).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise TargetResolutionError(f"target path does not exist: {candidate}") from exc
    if not resolved.is_file():
        raise TargetResolutionError(f"target path is not a regular file: {resolved}")
    return {"target_path": str(resolved)}
