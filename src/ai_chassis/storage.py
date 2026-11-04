"""Declared storage volumes and placements per machine profile (ISSUE-REF, Component 1).

The profile (``ai-harness.toml``) declares, per machine, which volumes exist and which data class
lives on which volume. This module is the one reader of that table family, the containment guard
that ``apply`` runs before touching a placement, and the ``storage`` activation class consumed by
``ai-harness doctor``/``apply`` and by ``scripts/storage_autoprune.py``.

Rules the reader enforces (a violation is a :class:`StorageDeclarationError`, exit code 2):
exactly one ``primary`` volume; any number of uniquely named non-primary volumes with a registered
role; every placement names a declared volume and an absolute path strictly under that volume's
mount; ``owner_uid``/``owner_gid`` are optional and numeric; ``surface`` is a registered adapter
name. A machine with no ``[machine.<m>.storage]`` table resolves to :class:`NotDeclared`.

Containment is re-checked on the resolved filesystem at mutation time (finding F-1): a symlink-free
walk, the nearest existing ancestor's ``st_dev`` against the adapter-verified mount device, and a
post-create ``st_dev`` check. The string rule above is validation, never the guard.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol

REGISTERED_ROLES: tuple[str, ...] = ("primary", "secondary", "network")
REGISTERED_SURFACES: tuple[str, ...] = ("ec2", "sagemaker", "macos", "linux")
DOCKER_DEFAULT_LOG_DRIVERS: tuple[str, ...] = ("local", "json-file")
JSON_FILE_REQUIRED_OPTS: tuple[str, ...] = ("max-size", "max-file")
EXIT_INVALID_DECLARATION = 2
#: Phase 6 (D-10 (c)): the placement Docker's data-root and containerd's root move under. Its
#: whole point is to leave the primary, so the reader refuses it on the primary volume.
DOCKER_ROOT_PLACEMENT = "docker_root"
_MODE_BITS = 0o7777
_DEFAULT_DIR_MODE = 0o755
ACTIVATION_RECORD = "storage-activation.json"
APPLY_JOURNAL = "storage-apply.jsonl"

Role = Literal["primary", "secondary", "network"]
VerificationStatus = Literal["ok", "mismatch", "unverifiable"]


class StorageDeclarationError(ValueError):
    """A storage table violates the reader's rules; ``key`` names the offending TOML key."""

    exit_code = EXIT_INVALID_DECLARATION

    def __init__(self, key: str, problem: str) -> None:
        self.key = key
        self.problem = problem
        super().__init__(f"{key}: {problem}")


@dataclass(frozen=True)
class DeclaredVolume:
    name: str
    mount: Path
    role: Role
    verify: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Placement:
    name: str
    volume: str
    path: Path
    mode: int | None = None
    owner_uid: int | None = None
    owner_gid: int | None = None

    @property
    def declares_owner(self) -> bool:
        return self.owner_uid is not None


@dataclass(frozen=True)
class StorageDeclaration:
    machine: str
    surface: str
    volumes: tuple[DeclaredVolume, ...]
    placements: Mapping[str, Placement]
    docker_default_log_driver: str | None = None
    docker_default_log_opts: Mapping[str, str] = field(default_factory=dict)

    def volume(self, name: str) -> DeclaredVolume:
        for volume in self.volumes:
            if volume.name == name:
                return volume
        raise KeyError(name)

    @property
    def primary(self) -> DeclaredVolume:
        return next(volume for volume in self.volumes if volume.role == "primary")


@dataclass(frozen=True)
class NotDeclared:
    """The machine has no ``[machine.<m>.storage]`` table; distinct from a declared single volume."""

    machine: str


@dataclass(frozen=True)
class Verification:
    status: VerificationStatus
    reason: str = ""
    field: str | None = None
    device: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def describe(self) -> str:
        if self.status == "ok":
            return "ok"
        if self.field:
            return f"{self.status}({self.field}: {self.reason})"
        return f"{self.status}({self.reason})"


@dataclass(frozen=True)
class ObservedVolume:
    mount: Path
    fstype: str | None
    source: str | None
    serial: str | None
    device: int | None


class StorageAdapter(Protocol):
    surface: str

    def verify(self, declared: DeclaredVolume) -> Verification: ...

    def discover(self) -> tuple[ObservedVolume, ...]: ...


class UnimplementedAdapter:
    """A registered surface whose adapter is a name only: honoured, reported, never trusted."""

    def __init__(self, surface: str) -> None:
        self.surface = surface

    def verify(self, _declared: DeclaredVolume) -> Verification:
        return Verification("unverifiable", "adapter not implemented")

    def discover(self) -> tuple[ObservedVolume, ...]:
        return ()


def adapter_registry() -> Mapping[str, StorageAdapter]:
    """The default surface -> adapter mapping; every registered surface has an entry."""
    from .storage_adapters import default_adapters

    return default_adapters()


# ---------------------------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------------------------


def storage_declares_a_capability(table: object) -> bool:
    """The one definition of "this storage table declares something" (activation-set discovery)."""
    if not isinstance(table, dict):
        return False
    volumes = table.get("volume")
    return isinstance(volumes, list) and len(volumes) > 0


def _require_str(table: Mapping[str, Any], key: str, label: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise StorageDeclarationError(f"{label}.{key}", "must be a non-empty string")
    return value


def _absolute(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise StorageDeclarationError(label, "must be a non-empty string")
    path = Path(value)
    if not path.is_absolute():
        raise StorageDeclarationError(label, f"must be an absolute path, got {value!r}")
    if any(part == ".." for part in path.parts):
        raise StorageDeclarationError(label, f"must not contain '..', got {value!r}")
    return path


def _optional_int(table: Mapping[str, Any], key: str, label: str) -> int | None:
    if key not in table:
        return None
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StorageDeclarationError(
            f"{label}.{key}", "must be a non-negative integer"
        )
    return value


def _optional_mode(table: Mapping[str, Any], label: str) -> int | None:
    if "mode" not in table:
        return None
    value = table["mode"]
    if isinstance(value, str):
        try:
            parsed = int(value, 8)
        except ValueError:
            parsed = -1
    elif isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    else:
        parsed = -1
    if not 0 <= parsed <= _MODE_BITS:
        raise StorageDeclarationError(
            f"{label}.mode",
            f'must be an octal mode string such as "0700", got {value!r}',
        )
    return parsed


def _strictly_under(path: Path, mount: Path) -> bool:
    return path != mount and mount in path.parents


def _volumes(table: Mapping[str, Any], label: str) -> tuple[DeclaredVolume, ...]:
    raw = table.get("volume")
    if not isinstance(raw, list) or not raw:
        raise StorageDeclarationError(
            f"{label}.volume", "must be a non-empty array of tables"
        )
    volumes: list[DeclaredVolume] = []
    names: set[str] = set()
    for index, entry in enumerate(raw):
        entry_label = f"{label}.volume[{index}]"
        if not isinstance(entry, dict):
            raise StorageDeclarationError(entry_label, "must be a table")
        name = _require_str(entry, "name", entry_label)
        if name in names:
            raise StorageDeclarationError(
                f"{entry_label}.name", f"duplicate volume name {name!r}"
            )
        names.add(name)
        mount = _absolute(entry.get("mount"), f"{entry_label}.mount")
        role = _require_str(entry, "role", entry_label)
        if role not in REGISTERED_ROLES:
            raise StorageDeclarationError(
                f"{entry_label}.role",
                f"{role!r} is not a registered role; registered: {', '.join(REGISTERED_ROLES)}",
            )
        verify_raw = entry.get("verify", {})
        if not isinstance(verify_raw, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in verify_raw.items()
        ):
            raise StorageDeclarationError(
                f"{entry_label}.verify", "must be a table of string hints"
            )
        volumes.append(DeclaredVolume(name, mount, role, dict(verify_raw)))  # type: ignore[arg-type]
    primaries = [volume for volume in volumes if volume.role == "primary"]
    if len(primaries) != 1:
        raise StorageDeclarationError(
            f"{label}.volume",
            f"exactly one primary volume is required, found {len(primaries)}",
        )
    return tuple(volumes)


def _placements(
    table: Mapping[str, Any], volumes: Sequence[DeclaredVolume], label: str
) -> dict[str, Placement]:
    raw = table.get("placement", {})
    if not isinstance(raw, dict):
        raise StorageDeclarationError(f"{label}.placement", "must be a table")
    by_name = {volume.name: volume for volume in volumes}
    placements: dict[str, Placement] = {}
    for name, entry in raw.items():
        entry_label = f"{label}.placement.{name}"
        if not isinstance(entry, dict):
            raise StorageDeclarationError(entry_label, "must be a table")
        volume_name = _require_str(entry, "volume", entry_label)
        if volume_name not in by_name:
            raise StorageDeclarationError(
                f"{entry_label}.volume", f"names undeclared volume {volume_name!r}"
            )
        path = _absolute(entry.get("path"), f"{entry_label}.path")
        mount = by_name[volume_name].mount
        if not _strictly_under(path, mount):
            raise StorageDeclarationError(
                f"{entry_label}.path",
                f"{path} is not strictly under volume {volume_name!r}'s mount {mount}",
            )
        if name == DOCKER_ROOT_PLACEMENT and by_name[volume_name].role == "primary":
            raise StorageDeclarationError(
                f"{entry_label}.volume",
                f"names the primary volume {volume_name!r}; the Docker roots move OFF the primary",
            )
        uid = _optional_int(entry, "owner_uid", entry_label)
        gid = _optional_int(entry, "owner_gid", entry_label)
        if (uid is None) != (gid is None):
            raise StorageDeclarationError(
                f"{entry_label}.owner_uid",
                "owner_uid and owner_gid must be declared together",
            )
        placements[name] = Placement(
            name, volume_name, path, _optional_mode(entry, entry_label), uid, gid
        )
    return placements


def _docker_log_defaults(
    table: Mapping[str, Any], label: str
) -> tuple[str | None, dict[str, str]]:
    """The D-10 (a) opt-in: ``docker_default_log_driver`` and its ``docker_default_log_opts``."""
    driver_key = f"{label}.docker_default_log_driver"
    opts_key = f"{label}.docker_default_log_opts"
    driver = table.get("docker_default_log_driver")
    opts = table.get("docker_default_log_opts", {})
    if not isinstance(opts, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or not v
        for k, v in opts.items()
    ):
        raise StorageDeclarationError(opts_key, "must be a table of non-empty strings")
    if driver is None:
        if opts:
            raise StorageDeclarationError(
                opts_key, "requires docker_default_log_driver"
            )
        return None, {}
    if driver not in DOCKER_DEFAULT_LOG_DRIVERS:
        raise StorageDeclarationError(
            driver_key,
            f"{driver!r} is not a D-10 driver; allowed: {', '.join(DOCKER_DEFAULT_LOG_DRIVERS)}",
        )
    missing = [k for k in JSON_FILE_REQUIRED_OPTS if k not in opts]
    if driver == "json-file" and missing:
        raise StorageDeclarationError(
            opts_key,
            f"json-file rotates nothing without {' and '.join(missing)}; declare both",
        )
    return driver, dict(opts)


def resolve(
    profile: Mapping[str, Any], machine: str
) -> StorageDeclaration | NotDeclared:
    """Parse and validate ``[machine.<machine>.storage]`` from the loaded profile mapping."""
    machines = profile.get("machine")
    entry = machines.get(machine) if isinstance(machines, dict) else None
    table = entry.get("storage") if isinstance(entry, dict) else None
    if table is None:
        return NotDeclared(machine)
    label = f"machine.{machine}.storage"
    if not isinstance(table, dict):
        raise StorageDeclarationError(label, "must be a table")
    surface = _require_str(table, "surface", label)
    if surface not in REGISTERED_SURFACES:
        raise StorageDeclarationError(
            f"{label}.surface",
            f"{surface!r} is not a registered adapter; registered: "
            f"{', '.join(REGISTERED_SURFACES)}",
        )
    volumes = _volumes(table, label)
    driver, opts = _docker_log_defaults(table, label)
    return StorageDeclaration(
        machine, surface, volumes, _placements(table, volumes, label), driver, opts
    )


# ---------------------------------------------------------------------------------------------
# Containment guard (F-1)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Containment:
    ok: bool
    nearest_existing: Path
    reason: str = ""
    component: str | None = None

    def describe(self) -> str:
        return f"{self.reason}: {self.component}" if self.component else self.reason


def check_containment(  # noqa: PLR0911 -- one named refusal per containment failure
    path: Path,
    mount: Path,
    mount_device: int,
    *,
    lstat: Callable[[Path], os.stat_result] = os.lstat,
) -> Containment:
    """Walk ``path`` without following symlinks and prove it stays on the verified mount.

    Refuses (``escapes-volume``) when any existing component is a symlink, when ``path`` is not
    strictly under ``mount``, or when the nearest existing ancestor's ``st_dev`` differs from the
    adapter-verified ``mount_device`` (a nested mount or a bind back onto another volume).
    """
    nearest = Path(path.anchor)
    current = nearest
    for part in path.parts[1:]:
        current = current / part
        try:
            st = lstat(current)
        except FileNotFoundError:
            break
        except PermissionError as exc:
            # A component the running user may not traverse (a 0700 root owned by another
            # uid): refuse by name instead of letting the OSError reach the operator as a
            # traceback (ISSUE-REF Phase 2, measured on example-project-ec2 2026-10-12).
            return Containment(
                False,
                nearest,
                "unreadable",
                f"{current}: {exc.strerror} for uid {os.geteuid()}",
            )
        if stat.S_ISLNK(st.st_mode):
            return Containment(
                False, nearest, "escapes-volume", f"{current} is a symlink"
            )
        if not stat.S_ISDIR(st.st_mode):
            return Containment(False, nearest, "not-a-directory", str(current))
        nearest = current
    if not _strictly_under(path, mount):
        return Containment(
            False, nearest, "escapes-volume", f"{path} is not strictly under {mount}"
        )
    if mount != nearest and mount not in nearest.parents:
        return Containment(False, nearest, "mount-absent", str(mount))
    device = lstat(nearest).st_dev
    if device != mount_device:
        return Containment(
            False,
            nearest,
            "escapes-volume",
            f"{nearest} st_dev {device} != verified mount device {mount_device}",
        )
    return Containment(True, nearest)


# ---------------------------------------------------------------------------------------------
# Placement state and convergence
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlacementState:
    exists: bool
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    is_dir: bool = True


def placement_state(
    placement: Placement, *, lstat: Callable[[Path], os.stat_result] = os.lstat
) -> PlacementState:
    try:
        st = lstat(placement.path)
    except FileNotFoundError:
        return PlacementState(False)
    return PlacementState(
        True, st.st_mode & 0o7777, st.st_uid, st.st_gid, stat.S_ISDIR(st.st_mode)
    )


def placement_problems(placement: Placement, state: PlacementState) -> list[str]:
    """Why a placement is not converged, in the order doctor reports them; empty when it is."""
    if not state.exists:
        return ["missing"]
    problems: list[str] = []
    if not state.is_dir:
        problems.append("not a directory")
    if placement.mode is not None and state.mode != placement.mode:
        problems.append(f"mode {state.mode:04o} != {placement.mode:04o}")
    if placement.declares_owner and (state.uid, state.gid) != (
        placement.owner_uid,
        placement.owner_gid,
    ):
        problems.append(
            f"owner {state.uid}:{state.gid} != {placement.owner_uid}:{placement.owner_gid}"
        )
    return problems


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class PlacementOutcome:
    name: str
    path: Path
    status: Literal["converged", "unchanged", "fail"]
    actions: tuple[str, ...] = ()
    reason: str = ""
    remedy: str = ""
    note: str = ""

    @property
    def mutated(self) -> bool:
        return bool(self.actions)


def _converge_owner(
    placement: Placement, state: PlacementState, run: Runner, euid: int | None
) -> tuple[str | None, str, str]:
    """Return (action, failure_reason, remedy); exactly one of action/failure is set."""
    assert placement.owner_uid is not None and placement.owner_gid is not None
    if (state.uid, state.gid) == (placement.owner_uid, placement.owner_gid):
        return None, "", ""
    command = [
        "sudo",
        "-n",
        "chown",
        f"{placement.owner_uid}:{placement.owner_gid}",
        str(placement.path),
    ]
    remedy = " ".join(command)
    running = os.geteuid() if euid is None else euid
    try:
        if running == 0:
            os.chown(placement.path, placement.owner_uid, placement.owner_gid)
        else:
            completed = run(command, capture_output=True, text=True, check=False)
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                return (
                    None,
                    f"owner {state.uid}:{state.gid} could not be converged: "
                    f"{detail or f'rc={completed.returncode}'}",
                    remedy,
                )
    except OSError as exc:
        return None, f"owner could not be converged: {exc}", remedy
    after = placement_state(placement)
    if (after.uid, after.gid) != (placement.owner_uid, placement.owner_gid):
        return None, f"owner still {after.uid}:{after.gid} after chown", remedy
    return (
        f"chown {state.uid}:{state.gid}->{placement.owner_uid}:{placement.owner_gid}",
        "",
        "",
    )


def converge_placement(
    placement: Placement,
    mount: Path,
    mount_device: int,
    *,
    run: Runner = subprocess.run,
    euid: int | None = None,
) -> PlacementOutcome:
    """Create or converge one placement directory on its verified volume.

    Every refusal names what it refused; a declared owner that cannot be applied fails the step
    with the exact ``chown`` command, never a silent skip; an undeclared owner is reported and left.
    """
    containment = check_containment(placement.path, mount, mount_device)
    if not containment.ok:
        return PlacementOutcome(
            placement.name, placement.path, "fail", reason=containment.describe()
        )
    actions: list[str] = []

    def fail(reason: str, remedy: str = "") -> PlacementOutcome:
        return PlacementOutcome(
            placement.name, placement.path, "fail", tuple(actions), reason, remedy
        )

    action, failure = _create_if_missing(placement, mount_device, run)
    if action:
        actions.append(action)
    if failure:
        return fail(failure)
    state = placement_state(placement)
    if not state.is_dir:
        return fail("exists and is not a directory")
    action, failure = _converge_mode(placement, state)
    if action:
        actions.append(action)
    if failure:
        return fail(failure)
    action, failure, remedy, note = _converge_ownership(placement, state, run, euid)
    if failure:
        return fail(failure, remedy)
    if action:
        actions.append(action)
    status: Literal["converged", "unchanged"] = "converged" if actions else "unchanged"
    return PlacementOutcome(
        placement.name, placement.path, status, tuple(actions), note=note
    )


def _converge_ownership(
    placement: Placement, state: PlacementState, run: Runner, euid: int | None
) -> tuple[str | None, str, str, str]:
    """Return (action, failure, remedy, note) for the owner step (IC-1).

    A declared owner is converged (through ``sudo -n`` when not root) or the step fails naming the
    exact command; an undeclared owner that differs from the running user is reported in ``note``
    and never changed.
    """
    if placement.declares_owner:
        action, failure, remedy = _converge_owner(placement, state, run, euid)
        return action, failure, remedy, ""
    if (state.uid, state.gid) != (os.geteuid(), os.getegid()):
        return (
            None,
            "",
            "",
            f"owner {state.uid}:{state.gid} differs from the running user "
            f"{os.geteuid()}:{os.getegid()}; no owner declared, not changed",
        )
    return None, "", "", ""


def _create_if_missing(
    placement: Placement, mount_device: int, run: Runner
) -> tuple[str | None, str]:
    """Create an absent placement directory; return (action, failure).

    A placement owned by another uid whose parent the running user cannot write (``/srv`` is
    root's; ISSUE-REF) is created through ``sudo -n mkdir -m``, and its owner is converged by the
    chown step after. The post-create ``st_dev`` check is the second half of F-1: a nested mount
    or symlink that appeared between the containment walk and ``mkdir`` is reported as
    ``escapes-volume`` and the directory is left for the operator, never deleted.
    """
    if placement.path.exists():
        return None, ""
    mode = placement.mode if placement.mode is not None else _DEFAULT_DIR_MODE
    try:
        placement.path.mkdir(mode=mode, parents=True)
        os.chmod(placement.path, mode)
    except PermissionError as exc:
        if not _needs_privilege(placement):
            return None, f"mkdir failed: {exc}"
        _, failure, remedy = _sudo(
            run, ["mkdir", "-p", "-m", f"{mode:04o}", str(placement.path)]
        )
        if failure:
            return None, f"mkdir failed: {failure}; remedy: {remedy}"
    except OSError as exc:
        return None, f"mkdir failed: {exc}"
    action = f"created mode={mode:04o}"
    created_device = os.lstat(placement.path).st_dev
    if created_device != mount_device:
        return action, (
            f"escapes-volume: created directory st_dev {created_device} != verified "
            f"mount device {mount_device}; directory left for the operator"
        )
    return action, ""


def _converge_mode(
    placement: Placement, state: PlacementState
) -> tuple[str | None, str]:
    """Converge the directory mode to the declared one; return (action, failure)."""
    if placement.mode is None or state.mode == placement.mode:
        return None, ""
    try:
        os.chmod(placement.path, placement.mode)
    except OSError as exc:
        return None, f"chmod failed: {exc}"
    return f"chmod {state.mode:04o}->{placement.mode:04o}", ""


# ---------------------------------------------------------------------------------------------
# The `storage` activation class
# ---------------------------------------------------------------------------------------------


def verify_volumes(
    declaration: StorageDeclaration,
    adapters: Mapping[str, StorageAdapter] | None = None,
) -> dict[str, Verification]:
    registry = adapter_registry() if adapters is None else adapters
    adapter = registry.get(declaration.surface) or UnimplementedAdapter(
        declaration.surface
    )
    return {volume.name: adapter.verify(volume) for volume in declaration.volumes}


def storage_activation(
    declaration: StorageDeclaration | NotDeclared,
    verifications: Mapping[str, Verification] | None = None,
    placement_states: Mapping[str, PlacementState] | None = None,
    *,
    probe_failures: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The ``storage`` report class: shape of ``messaging_activation()`` (Component 5).

    ``status`` is ``pass`` only when every volume verifies ``ok`` and every placement exists with
    its declared mode and owner; ``not-declared`` when the machine has no table. A ``probe_failures``
    entry (placement -> reason) forces that placement to ``fail`` (T-2.3's hook).
    """
    if isinstance(declaration, NotDeclared):
        return {
            "class": "storage",
            "status": "not-declared",
            "machine": declaration.machine,
            "declared": False,
            "surface": None,
            "volumes": [],
            "placements": [],
            "failing": [],
        }
    verifications = verifications or {}
    placement_states = placement_states or {}
    probe_failures = probe_failures or {}
    failing: list[str] = []
    volumes: list[dict[str, Any]] = []
    for volume in declaration.volumes:
        verification = verifications.get(volume.name) or Verification(
            "unverifiable", "not verified"
        )
        if not verification.ok:
            failing.append(f"volume:{volume.name}")
        volumes.append(
            {
                "name": volume.name,
                "mount": str(volume.mount),
                "role": volume.role,
                "verified": verification.status,
                "reason": verification.describe(),
            }
        )
    placements: list[dict[str, Any]] = []
    for name, placement in declaration.placements.items():
        state = placement_states.get(name) or PlacementState(False)
        problems = placement_problems(placement, state)
        if name in probe_failures:
            problems.append(f"write probe failed: {probe_failures[name]}")
        status = "pass" if not problems else "fail"
        if status == "fail":
            failing.append(f"placement:{name}")
        placements.append(
            {
                "name": name,
                "path": str(placement.path),
                "volume": placement.volume,
                "exists": state.exists,
                "mode": f"{state.mode:04o}" if state.mode is not None else None,
                "owner": f"{state.uid}:{state.gid}" if state.exists else None,
                "status": status,
                "reason": "; ".join(problems) if problems else "converged",
            }
        )
    return {
        "class": "storage",
        "status": "pass" if not failing else "fail",
        "machine": declaration.machine,
        "declared": True,
        "surface": declaration.surface,
        "volumes": volumes,
        "placements": placements,
        "failing": failing,
    }


def report_lines(record: Mapping[str, Any]) -> list[str]:
    """Human-facing doctor lines for one activation record."""
    if not record.get("declared"):
        return ["storage: not declared"]
    lines = [
        f"storage: machine={record['machine']} surface={record['surface']} "
        f"status={record['status']}"
    ]
    for volume in record["volumes"]:
        lines.append(
            f"storage volume {volume['name']} {volume['mount']} {volume['role']}: "
            f"{volume['reason']}"
        )
    for placement in record["placements"]:
        lines.append(
            f"storage placement {placement['name']} {placement['path']}: {placement['reason']}"
        )
    return lines


def write_activation_record(state_root: Path, record: Mapping[str, Any]) -> Path:
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / ACTIVATION_RECORD
    path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def read_activation_record(state_root: Path) -> dict[str, Any] | None:
    path = state_root / ACTIVATION_RECORD
    if not path.exists():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


# ---------------------------------------------------------------------------------------------
# doctor / apply entry points used by ai_chassis.cli
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageDoctor:
    record: dict[str, Any]
    lines: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return self.record["status"] in ("pass", "not-declared")


def doctor_storage(
    profile: Mapping[str, Any],
    machine: str | None,
    *,
    adapters: Mapping[str, StorageAdapter] | None = None,
) -> StorageDoctor:
    """Observe: resolve, verify every volume, read every placement; mutate nothing."""
    if not machine:
        record = storage_activation(NotDeclared("<unidentified>"))
        return StorageDoctor(record, ("storage: not declared (AI_HOST unset)",))
    declaration = resolve(profile, machine)
    if isinstance(declaration, NotDeclared):
        record = storage_activation(declaration)
        return StorageDoctor(record, tuple(report_lines(record)))
    verifications = verify_volumes(declaration, adapters)
    states = {name: placement_state(p) for name, p in declaration.placements.items()}
    record = storage_activation(declaration, verifications, states)
    return StorageDoctor(record, tuple(report_lines(record)))


# --- placement extras and the write probe (ISSUE-REF Phase 2, T-2.1 and T-2.3) ------------------

PROBE_PREFIX = ".aih-write-probe-"
CONTAINER_PROBE_IMAGE = "alpine:latest"
_PROBE_PAYLOAD = b"probe\n"


class PlacementExtra:
    """What the storage step does for one named placement beyond creating its directory.

    ``subdirs`` are converged with the placement's own mode and owner; ``export`` runs after the
    placement and its subdirectories converged and its write probe passed, and returns the report
    lines it produced; ``missing_line`` is reported when the machine declares storage but not this
    placement, so an undeclared data root is named rather than silently left unwritten. The
    agent-mail extra lives in :mod:`ai_chassis.messaging`; this module names no service.
    """

    def __init__(
        self,
        placement: str,
        *,
        subdirs: tuple[str, ...] = (),
        export: Callable[[Path], list[str]] | None = None,
        missing_line: str = "",
    ) -> None:
        self.placement = placement
        self.subdirs = subdirs
        self.export = export
        self.missing_line = missing_line


@dataclass(frozen=True)
class WriterIdentity:
    """Who must be able to write a placement, and how the probe takes that identity."""

    kind: Literal["user", "container"]
    uid: int
    gid: int

    def describe(self) -> str:
        if self.kind == "container":
            return f"uid {self.uid}:{self.gid} (throwaway container)"
        return f"uid {self.uid}:{self.gid} (the running user, direct write)"


def writer_identity(placement: Placement) -> WriterIdentity:
    """A declared owner is a service account the probe must take inside a container; otherwise
    the writer is the running user itself (the collector, the venv store, scratch)."""
    if placement.declares_owner:
        assert placement.owner_uid is not None and placement.owner_gid is not None
        return WriterIdentity("container", placement.owner_uid, placement.owner_gid)
    return WriterIdentity("user", os.geteuid(), os.getegid())


def container_probe_command(path: Path, uid: int, gid: int) -> list[str]:
    """The throwaway-container probe: create, sync, read back and remove one file as ``uid``."""
    script = (
        'set -e; f="/probe/.aih-write-probe-$$"; printf "probe\\n" > "$f"; sync; '
        '[ "$(cat "$f")" = "probe" ]; rm -f "$f"'
    )
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        f"{uid}:{gid}",
        "-v",
        f"{path}:/probe",
        CONTAINER_PROBE_IMAGE,
        "sh",
        "-c",
        script,
    ]


def _direct_probe(path: Path) -> tuple[str, str]:
    """Create, write, fsync, read back and remove one probe file; return (failed op, detail)."""
    probe = path / f"{PROBE_PREFIX}{os.getpid()}"
    operation = "create"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            operation = "write"
            os.write(fd, _PROBE_PAYLOAD)
            operation = "fsync"
            os.fsync(fd)
        finally:
            os.close(fd)
        operation = "read back"
        if probe.read_bytes() != _PROBE_PAYLOAD:
            probe.unlink()
            return operation, "the bytes read back differ from the bytes written"
        operation = "remove"
        probe.unlink()
    except OSError as exc:
        try:
            probe.unlink()
        except OSError:
            pass
        return operation, str(exc)
    return "", ""


def write_probe(placement: Placement, run: Runner) -> tuple[str, str]:
    """Return ("", "") when the placement's writer can write it, else (reason, remedy).

    T-2.3: a placement is reported converged ONLY after this passes, and there is no flag or
    environment variable that skips it.
    """
    identity = writer_identity(placement)
    if identity.kind == "container":
        command = container_probe_command(placement.path, identity.uid, identity.gid)
        remedy = " ".join(command)
        try:
            completed = run(command, capture_output=True, text=True, check=False)
        except OSError as exc:
            return (
                f"write probe as {identity.describe()} failed at docker run: {exc}",
                remedy,
            )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            return (
                f"write probe as {identity.describe()} failed at write-probe-in-container: "
                f"{detail or f'rc={completed.returncode}'}",
                remedy,
            )
        return "", ""
    operation, detail = _direct_probe(placement.path)
    if operation:
        return (
            f"write probe as {identity.describe()} failed at {operation}: {detail}",
            f"ls -ld {placement.path}  # the running user must own and be able to write it",
        )
    return "", ""


def _subdir_placement(placement: Placement, name: str) -> Placement:
    return replace(
        placement, name=f"{placement.name}/{name}", path=placement.path / name
    )


def _single_component(name: str) -> bool:
    """A subdirectory name is exactly one path component: ``../outside`` has basename ``outside``
    and would pass a check on the joined path, so the RAW name is validated before any join."""
    return (
        name not in ("", ".", "..")
        and "/" not in name
        and "\\" not in name
        and Path(name).name == name
    )


def _needs_privilege(placement: Placement) -> bool:
    """A declared owner other than the running, non-root user.

    Such a root is deliberately closed to the running user (0700 10001:10001 after Phase 0), so
    nothing under it can be observed or created as that user: ``lstat``, ``exists()``, ``mkdir``
    and ``chmod`` all fail with EACCES before any ``sudo -n chown`` could run.
    """
    euid = os.geteuid()
    return placement.declares_owner and euid not in (placement.owner_uid, 0)


_SUDO_STAT_FORMAT = "%F %d %u %g %a"  # kind, st_dev, uid, gid, octal mode (GNU stat)


def _sudo(
    run: Runner, argv: Sequence[str]
) -> tuple[subprocess.CompletedProcess[str] | None, str, str]:
    """Run one ``sudo -n`` command; return (completed, failure detail, the exact command)."""
    command = ["sudo", "-n", *argv]
    remedy = " ".join(command)
    try:
        completed = run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        return None, str(exc), remedy
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return completed, detail or f"rc={completed.returncode}", remedy
    return completed, "", remedy


@dataclass(frozen=True)
class _PrivilegedState:
    kind: str
    device: int
    uid: int
    gid: int
    mode: int


def _privileged_state(
    path: Path, run: Runner
) -> tuple[_PrivilegedState | None, str, str]:
    """``stat`` ``path`` through ``sudo -n``: (state, "", cmd); (None, "", cmd) when absent;
    (None, failure, cmd) when it cannot be observed even as root."""
    completed, failure, remedy = _sudo(
        run, ["stat", "-c", _SUDO_STAT_FORMAT, str(path)]
    )
    if failure:
        if completed is not None and "No such file" in (completed.stderr or ""):
            return None, "", remedy
        return None, failure, remedy
    assert completed is not None
    try:
        kind, device, uid, gid, mode = completed.stdout.strip().rsplit(None, 4)
        return (
            _PrivilegedState(kind, int(device), int(uid), int(gid), int(mode, 8)),
            "",
            remedy,
        )
    except ValueError:
        return None, f"stat output not understood: {completed.stdout.strip()!r}", remedy


def _observe_or_create_owned_subdir(
    child: Placement, mode: int, run: Runner, actions: list[str]
) -> tuple[_PrivilegedState | None, str, str]:
    """The subdirectory's state as root, creating it (as root, declared mode) when absent."""
    state, failure, remedy = _privileged_state(child.path, run)
    if failure:
        return None, f"cannot stat as root: {failure}", remedy
    if state is not None:
        return state, "", ""
    _, failure, remedy = _sudo(run, ["mkdir", "-m", f"{mode:04o}", str(child.path)])
    if failure:
        return None, f"mkdir failed: {failure}", remedy
    actions.append(f"created mode={mode:04o}")
    state, failure, remedy = _privileged_state(child.path, run)
    if state is None:
        return (
            None,
            f"created but not observable as root: {failure or 'absent'}",
            remedy,
        )
    return state, "", ""


def _converge_owned_attrs(  # noqa: PLR0913 -- the child, its state, targets, seams
    child: Placement,
    state: _PrivilegedState,
    mode: int,
    owner: str,
    *,
    run: Runner,
    actions: list[str],
) -> tuple[str, str]:
    """Converge mode then owner through ``sudo -n``; return (failure, remedy) or ("", "")."""
    if state.mode != mode:
        _, failure, remedy = _sudo(run, ["chmod", f"{mode:04o}", str(child.path)])
        if failure:
            return f"chmod failed: {failure}", remedy
        actions.append(f"chmod {state.mode:04o}->{mode:04o}")
    if (state.uid, state.gid) != (child.owner_uid, child.owner_gid):
        _, failure, remedy = _sudo(run, ["chown", owner, str(child.path)])
        if failure:
            return (
                f"owner {state.uid}:{state.gid} could not be converged: {failure}",
                remedy,
            )
        actions.append(f"chown {state.uid}:{state.gid}->{owner}")
    return "", ""


def _verify_owned_subdir(child: Placement, mode: int, run: Runner) -> tuple[str, str]:
    """Re-observe the child as root after a mutation; (failure, remedy) when not converged."""
    after, failure, remedy = _privileged_state(child.path, run)
    if after is None:
        return f"not observable as root after convergence: {failure}", remedy
    if (after.uid, after.gid, after.mode) != (child.owner_uid, child.owner_gid, mode):
        return (
            f"still {after.uid}:{after.gid} mode {after.mode:04o} after convergence",
            remedy,
        )
    return "", ""


def _converge_owned_subdir(  # noqa: PLR0911 -- one named refusal per observable failure
    child: Placement, mount_device: int, run: Runner
) -> PlacementOutcome:
    """Converge one subdirectory of a placement whose declared owner is not the running user.

    Every observation and mutation goes through ``sudo -n`` (the privilege that owns the root),
    and every refusal names the exact command, so a root the running user cannot traverse is
    converged or reported, never walked as that user and never a traceback (T-2.3, hardened).
    """
    assert child.owner_uid is not None and child.owner_gid is not None
    actions: list[str] = []
    mode = child.mode if child.mode is not None else _DEFAULT_DIR_MODE
    owner = f"{child.owner_uid}:{child.owner_gid}"

    def fail(reason: str, remedy: str = "") -> PlacementOutcome:
        return PlacementOutcome(
            child.name, child.path, "fail", tuple(actions), reason, remedy
        )

    state, failure, remedy = _observe_or_create_owned_subdir(child, mode, run, actions)
    if state is None:
        return fail(failure, remedy)
    if state.kind != "directory":
        kind = "escapes-volume" if state.kind == "symbolic link" else "not-a-directory"
        return fail(f"{kind}: {child.path} is a {state.kind}")
    if state.device != mount_device:
        return fail(
            f"escapes-volume: {child.path} st_dev {state.device} != verified mount device "
            f"{mount_device}; directory left for the operator"
        )
    failure, remedy = _converge_owned_attrs(
        child, state, mode, owner, run=run, actions=actions
    )
    if failure:
        return fail(failure, remedy)
    if actions:
        failure, remedy = _verify_owned_subdir(child, mode, run)
        if failure:
            return fail(failure, remedy)
    status: Literal["converged", "unchanged"] = "converged" if actions else "unchanged"
    return PlacementOutcome(child.name, child.path, status, tuple(actions))


def _apply_extra(  # noqa: PLR0913 -- the placement, its extra, its verified volume and the seams
    placement: Placement,
    extra: PlacementExtra | None,
    mount: Path,
    mount_device: int,
    *,
    run: Runner,
    outcome: PlacementOutcome,
) -> tuple[PlacementOutcome, list[str]]:
    """Converge the extra's subdirectories, run the write probe, then the export.

    A passing probe adds no report line: the activation record's ``pass`` is the proof, and a
    second apply must stay quiet (Phase 0's contract). A failing probe is the outcome's reason.
    """
    actions = list(outcome.actions)
    lines: list[str] = []
    if extra is not None:
        privileged = _needs_privilege(placement)
        for name in extra.subdirs:
            if not _single_component(name):
                return (
                    PlacementOutcome(
                        placement.name,
                        placement.path,
                        "fail",
                        tuple(actions),
                        f"refusing subdirectory name {name!r}: not a single path component",
                    ),
                    lines,
                )
            sub = _subdir_placement(placement, name)
            child = (
                _converge_owned_subdir(sub, mount_device, run)
                if privileged
                else converge_placement(sub, mount, mount_device, run=run)
            )
            if child.status == "fail":
                return (
                    PlacementOutcome(
                        placement.name,
                        placement.path,
                        "fail",
                        tuple(actions),
                        f"subdirectory {name}: {child.reason}",
                        child.remedy,
                    ),
                    lines,
                )
            actions.extend(f"{name}: {action}" for action in child.actions)
    reason, remedy = write_probe(placement, run)
    if reason:
        return (
            PlacementOutcome(
                placement.name, placement.path, "fail", tuple(actions), reason, remedy
            ),
            lines,
        )
    if extra is not None and extra.export is not None:
        try:
            lines.extend(extra.export(placement.path))
        except OSError as exc:
            return (
                PlacementOutcome(
                    placement.name,
                    placement.path,
                    "fail",
                    tuple(actions),
                    f"export failed: {exc}",
                ),
                lines,
            )
    status: Literal["converged", "unchanged"] = "converged" if actions else "unchanged"
    return (
        PlacementOutcome(
            placement.name, placement.path, status, tuple(actions), note=outcome.note
        ),
        lines,
    )


def _apply_placement(
    declaration: StorageDeclaration,
    placement: Placement,
    verifications: Mapping[str, Verification],
    extras_by_name: Mapping[str, PlacementExtra],
    run: Runner,
) -> tuple[PlacementOutcome, list[str]]:
    """One placement: verified volume, converge, then its extra (subdirectories, probe, export)."""
    volume = declaration.volume(placement.volume)
    verification = verifications[volume.name]
    if not verification.ok or verification.device is None:
        return (
            PlacementOutcome(
                placement.name,
                placement.path,
                "fail",
                reason=f"volume {volume.name!r} not verified: {verification.describe()}",
            ),
            [],
        )
    try:
        outcome = converge_placement(
            placement, volume.mount, verification.device, run=run
        )
        if outcome.status == "fail":
            return outcome, []
        return _apply_extra(
            placement,
            extras_by_name.get(placement.name),
            volume.mount,
            verification.device,
            run=run,
            outcome=outcome,
        )
    except OSError as exc:
        # The backstop for T-2.3's "fail loudly": an OSError nobody named above becomes a
        # failed placement with the path and errno, never a traceback at the install step.
        return (
            PlacementOutcome(
                placement.name,
                placement.path,
                "fail",
                reason=f"{type(exc).__name__} at {exc.filename or placement.path}: "
                f"{exc.strerror or exc}",
                remedy=f"sudo -n ls -ldn {placement.path} {placement.path}/*",
            ),
            [],
        )


class StorageApplyError(RuntimeError):
    """The storage step failed; the message names placement, operation and remedy."""


@dataclass(frozen=True)
class StorageApply:
    record: dict[str, Any]
    outcomes: tuple[PlacementOutcome, ...]
    lines: tuple[str, ...]

    @property
    def mutated(self) -> bool:
        return any(outcome.mutated for outcome in self.outcomes)

    @property
    def failures(self) -> tuple[PlacementOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.status == "fail")


def _append_journal(state_root: Path, entry: Mapping[str, Any]) -> None:
    state_root.mkdir(parents=True, exist_ok=True)
    with (state_root / APPLY_JOURNAL).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def read_apply_journal(state_root: Path) -> list[dict[str, Any]]:
    path = state_root / APPLY_JOURNAL
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _observe_states(
    declaration: StorageDeclaration, outcomes: list[PlacementOutcome]
) -> dict[str, PlacementState]:
    """The post-apply observation of every placement; an unreadable one is appended to
    ``outcomes`` as a named failure (the last place an OSError could still escape; T-2.3's
    "fail loudly" means by name, never a traceback)."""
    states: dict[str, PlacementState] = {}
    for name, placement in declaration.placements.items():
        try:
            states[name] = placement_state(placement)
        except OSError as exc:
            states[name] = PlacementState(False)
            outcomes.append(
                PlacementOutcome(
                    placement.name,
                    placement.path,
                    "fail",
                    reason=f"state unreadable after apply: {type(exc).__name__} at "
                    f"{exc.filename or placement.path}: {exc.strerror or exc}",
                    remedy=f"sudo -n ls -ldn {placement.path}",
                )
            )
    return states


def apply_storage(  # noqa: PLR0913 -- three scope inputs plus four injected seams, all defaulted
    profile: Mapping[str, Any],
    machine: str | None,
    state_root: Path,
    *,
    adapters: Mapping[str, StorageAdapter] | None = None,
    run: Runner = subprocess.run,
    now: Callable[[], float] = time.time,
    extras: Sequence[PlacementExtra] = (),
) -> StorageApply:
    """Converge every placement on its adapter-verified volume and journal what changed.

    Each placement is created or converged, its extra's subdirectories likewise, then WRITE-PROBED
    as its writer identity (T-2.3) and only then reported converged; the extra's export (for
    agent-mail, the compose.env key) runs last. Raises :class:`StorageApplyError` when any
    placement fails, after journalling and writing the activation record, so the install step is
    red and names the placement, the writer identity, the failing operation and the remedy (D-4,
    hardened). Every rerun probes again, so a failure stays red until a probe succeeds.
    """
    if not machine:
        record = storage_activation(NotDeclared("<unidentified>"))
        return StorageApply(record, (), ("storage: not declared (AI_HOST unset)",))
    declaration = resolve(profile, machine)
    if isinstance(declaration, NotDeclared):
        record = storage_activation(declaration)
        write_activation_record(state_root, record)
        return StorageApply(record, (), tuple(report_lines(record)))
    verifications = verify_volumes(declaration, adapters)
    extras_by_name = {extra.placement: extra for extra in extras}
    outcomes: list[PlacementOutcome] = []
    extra_lines: list[str] = []
    for placement in declaration.placements.values():
        outcome, lines = _apply_placement(
            declaration, placement, verifications, extras_by_name, run
        )
        outcomes.append(outcome)
        extra_lines.extend(lines)
    for extra in extras:
        if extra.placement not in declaration.placements and extra.missing_line:
            extra_lines.append(extra.missing_line)
    states = _observe_states(declaration, outcomes)
    failures = {o.name: o.reason for o in outcomes if o.status == "fail"}
    record = storage_activation(
        declaration, verifications, states, probe_failures=failures
    )
    _append_journal(
        state_root,
        {
            "at": now(),
            "machine": machine,
            "mutations": [
                {"placement": o.name, "path": str(o.path), "actions": list(o.actions)}
                for o in outcomes
                if o.mutated
            ],
            "failures": [
                {
                    "placement": o.name,
                    "path": str(o.path),
                    "reason": o.reason,
                    "remedy": o.remedy,
                }
                for o in outcomes
                if o.status == "fail"
            ],
        },
    )
    write_activation_record(state_root, record)
    lines = list(report_lines(record))
    for outcome in outcomes:
        if outcome.mutated:
            lines.append(f"storage apply {outcome.name}: {', '.join(outcome.actions)}")
        if outcome.note:
            lines.append(f"storage apply {outcome.name}: {outcome.note}")
    lines.extend(extra_lines)
    result = StorageApply(record, tuple(outcomes), tuple(lines))
    if result.failures:
        detail = "; ".join(
            f"placement {o.name} ({o.path}): {o.reason}"
            + (f"; remedy: {o.remedy}" if o.remedy else "")
            for o in result.failures
        )
        raise StorageApplyError(f"storage step failed: {detail}")
    return result
