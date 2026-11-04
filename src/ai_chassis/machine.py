"""Machine identity: which ``[machine.<name>]`` registry table describes this host.

The registry (``ai_chassis.machine_registry``, schema 3) declares, per machine, a ``platform``
and three lists of identity checks: ``hostname_exact`` (the lowercased hostname, or its first
label, equals an entry), ``hostname_prefix`` (it starts with an entry) and ``path_exists`` (an
entry is readable). A machine matches when ANY of its checks matches on its own platform;
exactly one must, and zero or several raise naming them, never a default. ``AI_HOST`` and the
marker file outrank the checks (:func:`resolve_machine_identity`), as in install.sh Step 1b.
"""

from __future__ import annotations

import os
import socket
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


class MachineDetectionError(RuntimeError):
    """Configured machine predicates did not select exactly one machine."""


#: The fleet's identity file, written by ``install.sh`` Step 1b outside ``$HOME``
#: because ``$HOME`` is what gets migrated and rebuilt.  Spelled identically in
#: ``scripts/worktree_policy.py``, ``scripts/wq_client.py`` and ``.claude/bin/cx``.
MACHINE_MARKER_PATH = Path("/etc/ai-harness/machine")

#: Where ``install.sh`` records the identity when it cannot write ``/etc`` --
#: ``$XDG_STATE_HOME/ai-harness/machine``, the same unprivileged root the private ``chief-identity``
#: install step (install.sh's Step 14c until ISSUE-REF batch 13) falls back to.
MACHINE_MARKER_RELATIVE_PATH = Path("ai-harness") / "machine"

#: Names the ONE marker file to consult, replacing the default search order rather than
#: any single entry.  Shared verbatim with ``cx``'s ``CX_MACHINE_MARKER_FILE`` and with
#: ``scripts/worktree_policy.py`` so the fleet has one pin for "what is this machine?".
MACHINE_MARKER_ENV_VAR = "CX_MACHINE_MARKER_FILE"

#: The documented interface for an explicitly chosen identity.
MACHINE_ENV_VAR = "AI_HOST"


def machine_marker_paths(environ: Mapping[str, str] | None = None) -> list[Path]:
    """The marker files to consult, in order, for this process's environment."""
    values = os.environ if environ is None else environ
    override = values.get(MACHINE_MARKER_ENV_VAR, "").strip()
    if override:
        return [Path(override)]
    state_home = values.get("XDG_STATE_HOME", "").strip()
    state_root = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return [MACHINE_MARKER_PATH, state_root / MACHINE_MARKER_RELATIVE_PATH]


def marker_identity(
    environ: Mapping[str, str] | None = None,
) -> tuple[str | None, list[Path]]:
    """``(identity, paths consulted)`` from the machine marker.

    ``None`` is a real answer: nothing here invents an identity, because a wrong machine
    id silently selects another machine's profile.  An empty or whitespace-only marker is
    the state ``install.sh`` calls FATAL, so it is treated as no answer rather than as the
    empty identity -- a falsy name would match no spec while still looking resolved.
    """
    consulted = machine_marker_paths(environ)
    for marker in consulted:
        try:
            value = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value, consulted
    return None, consulted


@dataclass(frozen=True)
class MachineSpec:
    """One registry machine's identity checks (schema 3), tried only on its own platform."""

    name: str
    platform: str
    hostname_exact: tuple[str, ...] = ()
    hostname_prefix: tuple[str, ...] = ()
    path_exists: tuple[str, ...] = ()


class AmbiguousMachineError(MachineDetectionError):
    """More than one machine's checks matched; ``candidates`` names them."""

    def __init__(self, message: str, candidates: Sequence[str]) -> None:
        super().__init__(message)
        self.candidates = tuple(candidates)


class EmptyMarkerError(MachineDetectionError):
    """A readable marker file holds no name (install.sh Step 1b's FATAL)."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} exists but is empty")
        self.path = path


def machine_specs(
    machines: Mapping[str, Mapping[str, object]],
) -> tuple[MachineSpec, ...]:
    """The specs for a loaded registry's ``machines`` (``machine_registry.Registry.machines``)."""
    return tuple(
        MachineSpec(
            name=name,
            platform=str(attributes["platform"]),
            hostname_exact=tuple(attributes["hostname_exact"]),  # type: ignore[arg-type]
            hostname_prefix=tuple(attributes["hostname_prefix"]),  # type: ignore[arg-type]
            path_exists=tuple(attributes["path_exists"]),  # type: ignore[arg-type]
        )
        for name, attributes in sorted(machines.items())
    )


def host_platform(
    *, platform_name: str | None = None, environ: Mapping[str, str] | None = None
) -> str:
    """``windows-shell`` under an MSYS-family shell, ``darwin`` on macOS, else ``linux``.

    The three arms install.sh Step 1b branched on, in its order: ``MSYSTEM`` first, so Git Bash
    on Windows is never mistaken for a POSIX host, then ``uname`` Darwin, then everything else.
    """
    values = os.environ if environ is None else environ
    if values.get("MSYSTEM", "").upper().startswith(("MINGW", "MSYS", "CYGWIN")):
        return "windows-shell"
    if (sys.platform if platform_name is None else platform_name) == "darwin":
        return "darwin"
    return "linux"


def _readable(path: Path) -> bool:
    """install.sh's ``[[ -r path ]]``."""
    return os.access(path, os.R_OK)


def _matches(
    spec: MachineSpec,
    *,
    platform: str,
    hostname: str,
    readable: Callable[[Path], bool],
) -> bool:
    """Any one of the spec's checks matching, on the spec's own platform.

    ``hostname`` is already lowercased. An exact entry matches the whole name or its first
    label, so ``name.local`` and a DNS suffix match as Step 1b's Darwin arm stripped them.
    """
    if spec.platform != platform:
        return False
    short = hostname.split(".", 1)[0]
    if any(entry.lower() in (hostname, short) for entry in spec.hostname_exact):
        return True
    if any(hostname.startswith(entry.lower()) for entry in spec.hostname_prefix):
        return True
    return any(readable(Path(entry)) for entry in spec.path_exists)


def detect_machine(
    configured: Sequence[MachineSpec],
    *,
    platform_name: str | None = None,
    hostname: str | None = None,
    environ: Mapping[str, str] | None = None,
    readable: Callable[[Path], bool] | None = None,
) -> str:
    """The one machine whose checks match this host; zero or several raise, naming them."""
    platform = host_platform(platform_name=platform_name, environ=environ)
    name = (socket.gethostname() if hostname is None else hostname).lower()
    matched = [
        spec.name
        for spec in configured
        if _matches(
            spec, platform=platform, hostname=name, readable=readable or _readable
        )
    ]
    if len(matched) == 1:
        return matched[0]
    where = f"platform {platform}, hostname {name or '(none)'}"
    if not matched:
        raise MachineDetectionError(
            f"no machine's identity check matched this host ({where})"
        )
    raise AmbiguousMachineError(
        f"more than one machine's identity check matched this host ({where}): "
        + ", ".join(matched),
        matched,
    )


def resolve_machine_identity(
    configured: Sequence[MachineSpec],
    *,
    platform_name: str | None = None,
    hostname: str | None = None,
    environ: Mapping[str, str] | None = None,
    readable: Callable[[Path], bool] | None = None,
) -> tuple[str, str]:
    """``(identity, the source that answered)``: ``AI_HOST``, then the marker, then the checks.

    ``install.sh`` has always ordered the sources this way. A hostname names a LEASE: measured
    on example-cloud-host 2026-10-04, the marker read ``example-cloud-host`` while the leased hostname was
    ``ip-192-0-2-17``, so a hostname rung ahead of the marker persisted a wrong ``AI_HOST``.

    The first READABLE marker decides: a name answers, and an empty one raises
    :class:`EmptyMarkerError` rather than falling through, as install.sh Step 1b refused it.
    """
    values = os.environ if environ is None else environ
    from_env = values.get(MACHINE_ENV_VAR, "").strip()
    if from_env:
        return from_env, f"{MACHINE_ENV_VAR}={from_env}"
    consulted = machine_marker_paths(values)
    for marker in consulted:
        try:
            value = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not value:
            raise EmptyMarkerError(marker)
        return value, f"marker {marker}"
    tried = ", ".join(str(path) for path in consulted)
    try:
        detected = detect_machine(
            configured,
            platform_name=platform_name,
            hostname=hostname,
            environ=values,
            readable=readable,
        )
    except AmbiguousMachineError as exc:
        raise AmbiguousMachineError(
            f"{exc} (no {MACHINE_ENV_VAR}, and no marker answered: {tried})",
            exc.candidates,
        ) from exc
    except MachineDetectionError as exc:
        raise MachineDetectionError(
            f"{exc} (no {MACHINE_ENV_VAR}, and no marker answered: {tried})"
        ) from exc
    return detected, "identity checks"
