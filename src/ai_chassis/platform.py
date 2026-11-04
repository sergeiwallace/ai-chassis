"""Explicit platform detection and filesystem safety checks.

The installer has five distinct runtime environments.  Detection is deliberately
strict: an unfamiliar platform must be named and supported before it can mutate
an install target.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path


class PlatformDetectionError(RuntimeError):
    """The current runtime does not map to a supported environment."""


class PathSafetyError(RuntimeError):
    """An install path is unsafe or cannot be represented faithfully."""


CI_ENVIRONMENTS = frozenset({"darwin", "linux", "wsl", "gitbash"})
EXPERIMENTAL_ENVIRONMENTS = frozenset({"powershell"})
_MAX_PATH_LENGTHS = {
    "darwin": 1024,
    "linux": 4096,
    "wsl": 4096,
    "gitbash": 260,
    "powershell": 260,
}


def _proc_version() -> str:
    try:
        return Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def detect_environment(
    *,
    platform_name: str | None = None,
    os_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    proc_version: str | None = None,
) -> str:
    """Return the explicit runtime environment, or fail rather than guess POSIX."""
    platform_value = sys.platform if platform_name is None else platform_name
    os_value = os.name if os_name is None else os_name
    values = os.environ if environ is None else environ
    mssystem = values.get("MSYSTEM", "").upper()
    if mssystem.startswith(("MINGW", "MSYS", "CYGWIN")) or platform_value.startswith(
        "cygwin"
    ):
        return "gitbash"
    if platform_value == "darwin":
        return "darwin"
    if os_value == "nt" or platform_value == "win32":
        return "powershell"
    if platform_value.startswith("linux"):
        version = _proc_version() if proc_version is None else proc_version
        return "wsl" if "microsoft" in version.lower() else "linux"
    raise PlatformDetectionError(
        f"could not determine supported environment (sys.platform={platform_value!r}, "
        f"os.name={os_value!r}); refusing to assume POSIX"
    )


def is_ci_supported(environment: str) -> bool:
    """Whether the environment has a required supported CI lane."""
    return environment in CI_ENVIRONMENTS


def support_label(environment: str) -> str:
    """Return the user-facing support label for doctor output."""
    return "supported" if is_ci_supported(environment) else "experimental"


def resolve_install_path(path: Path) -> Path:
    """Resolve a path without requiring an as-yet absent install target to exist."""
    expanded = path.expanduser()
    return expanded.parent.resolve(strict=False) / expanded.name


def validate_install_target(
    path: Path,
    environment: str,
    *,
    maximum_length: int | None = None,
    filesystem_encoding: str | None = None,
) -> Path:
    """Resolve and validate an install destination before any target mutation."""
    encoding = (
        sys.getfilesystemencoding()
        if filesystem_encoding is None
        else filesystem_encoding
    )
    try:
        str(path.expanduser()).encode(encoding, errors="strict")
    except UnicodeEncodeError as exc:
        raise PathSafetyError(
            f"install target cannot be represented in filesystem encoding {encoding}: "
            f"{str(path)!r}"
        ) from exc
    try:
        resolved = resolve_install_path(path)
    except UnicodeError as exc:
        raise PathSafetyError(
            f"install target cannot be represented in filesystem encoding {encoding}: "
            f"{str(path)!r}"
        ) from exc
    rendered = str(resolved)
    try:
        rendered.encode(encoding, errors="strict")
    except UnicodeEncodeError as exc:
        raise PathSafetyError(
            f"install target cannot be represented in filesystem encoding {encoding}: "
            f"{rendered!r}"
        ) from exc
    limit = _MAX_PATH_LENGTHS[environment] if maximum_length is None else maximum_length
    if len(rendered) > limit:
        raise PathSafetyError(
            f"install target path exceeds maximum length {limit}: {rendered!r} "
            f"(resolved length {len(rendered)})"
        )
    if environment == "wsl" and resolved.is_relative_to(Path("/mnt")):
        raise PathSafetyError(
            f"refusing WSL install target beneath /mnt/: {resolved}; Windows-mount "
            "link semantics can corrupt managed links"
        )
    return resolved
