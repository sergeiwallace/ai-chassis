"""Docker's ``data-root`` and containerd's ``root`` under the ``docker_root`` placement
(ISSUE-REF Phase 6, D-10 (c), T-6.1).

Engine 29 keeps images and snapshots in containerd's own root when the containerd snapshotter is
in use, so ``data-root`` alone relocates less than it claims (research doc § 4.2). Both roots
therefore move together, as siblings under the placement: ``<placement>/engine`` for Docker and
``<placement>/containerd`` for containerd. Siblings rather than nesting, because Docker's data-root
holds a ``containerd/`` directory of its own, and because the placement directory itself stays
owned by the running user, so the storage step's write probe keeps passing after the move.

``plan`` prints both settings and fails, naming the root it would leave behind, when the engine
uses the snapshotter and only one of the two config files can be edited. Nothing here restarts,
stops or copies anything: the window is the operator's
(``docs/procedures/docker-root-migration-runbook.md``). The config writes the window performs go
through :func:`write_roots` and :func:`restore_roots`, the same ``sudo -n`` temporary-file-plus-
rename write Phase 5 uses, editing only the one key each file owns here. The window also writes
systemd drop-ins making ``docker.service`` and ``containerd.service`` require the placement's mount
(``RequiresMountsFor``), and its rollback removes them; ``apply`` writes any that are missing once
``docker info`` reports the roots on the placement, and ``doctor`` reports their state.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .docker_defaults import (
    DATA_ROOT_KEY,
    DockerDefaultsError,
    daemon_json_path,
    merge,
    read_text,
    sudo_write_file,
)
from .storage import (
    DOCKER_ROOT_PLACEMENT,
    NotDeclared,
    Runner,
    StorageDeclaration,
    _sudo,
)

DEFAULT_DOCKER_ROOT = Path("/var/lib/docker")
DEFAULT_CONTAINERD_ROOT = Path("/var/lib/containerd")
SNAPSHOTTER_DRIVER_TYPE = "io.containerd.snapshotter.v1"
RUNBOOK = "docs/procedures/docker-root-migration-runbook.md"
CONTAINERD_ROOT_KEY = "root"
DATA_ROOT_NAME = "data-root"
CONTAINERD_ROOT_NAME = "containerd root"
MOUNT_DROP_IN = "aih-docker-root-mount.conf"
ORDERED_UNITS = ("docker.service", "containerd.service")
APPLY_REMEDY = "ai-harness apply --only storage"
NOT_RESTARTED = "docker roots: nothing was restarted; the runbook's next step starts containerd, then docker"

_TABLE_HEADER = re.compile(r"^\s*\[")
_ROOT_LINE = re.compile(r"""^\s*(?:root|"root"|'root')\s*=""")


class DockerRootsError(RuntimeError):
    """The roots cannot be planned or written as one move; the message names the root."""


@dataclass(frozen=True)
class EngineInfo:
    root_dir: Path
    version: str
    driver: str
    snapshotter: bool


@dataclass(frozen=True)
class RootMove:
    name: str
    config: Path
    current: Path
    target: Path
    problem: str = ""


DOCKER_SERVICE = "docker.service"


def require_active_daemon(run: Runner) -> None:
    """Refuse before any ``docker`` call unless ``docker.service`` is active: with only
    ``docker.socket`` listening, the first client call would start dockerd."""
    command = ["systemctl", "is-active", DOCKER_SERVICE]
    try:
        completed = run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise DockerRootsError(
            f"docker roots: {' '.join(command)} failed: {exc}; not contacting docker"
        ) from exc
    state = (completed.stdout or "").strip() or f"rc={completed.returncode}"
    if completed.returncode != 0 or state != "active":
        raise DockerRootsError(
            f"docker roots: {DOCKER_SERVICE} is {state}; not contacting docker, because a "
            "socket-activated daemon would start; no plan is printed"
        )


def engine_info(run: Runner) -> EngineInfo:
    """``docker info`` through the injected runner (read-only, no privilege)."""
    command = ["docker", "info", "--format", "{{json .}}"]
    try:
        completed = run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise DockerRootsError(
            f"docker roots: {' '.join(command)} failed: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip() or f"rc={completed.returncode}"
        raise DockerRootsError(
            f"docker roots: {' '.join(command)} failed ({detail}); cannot tell whether the "
            "containerd snapshotter is in use, so no plan is printed"
        )
    try:
        data = json.loads(completed.stdout)
        status = data.get("DriverStatus") or []
        snapshotter = any(
            isinstance(pair, list) and SNAPSHOTTER_DRIVER_TYPE in pair
            for pair in status
        )
        return EngineInfo(
            Path(str(data["DockerRootDir"])),
            str(data.get("ServerVersion", "")),
            str(data.get("Driver", "")),
            snapshotter,
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DockerRootsError(
            f"docker roots: docker info output not understood: {exc}"
        ) from exc


def targets(placement: Path) -> tuple[Path, Path]:
    """(Docker data-root, containerd root), both strictly under the placement, as siblings."""
    return placement / "engine", placement / "containerd"


def containerd_config_path(etc_root: Path) -> Path:
    return etc_root / "containerd" / "config.toml"


def _parse_toml(text: str, path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise DockerRootsError(f"{path} is not valid TOML: {exc}") from exc


def configured_containerd_root(text: str | None, path: Path) -> str | None:
    """The ``root`` the file sets, or None when the file or the key is absent."""
    if text is None:
        return None
    value = _parse_toml(text, path).get(CONTAINERD_ROOT_KEY)
    if value is not None and not isinstance(value, str):
        raise DockerRootsError(f"{path}: root must be a string, got {value!r}")
    return value


def set_containerd_root(text: str | None, root: str | None, path: Path) -> str | None:
    """``text`` with only its top-level ``root`` set to ``root`` (removed when None).

    Every other byte is kept: the edit replaces, inserts or deletes the one ``root =`` line in the
    top-level region (before the first table header), then re-parses both documents and refuses
    unless they differ in ``root`` alone. Returns None when there is no file and nothing to set.
    """
    if text is None:
        return None if root is None else f"{CONTAINERD_ROOT_KEY} = {json.dumps(root)}\n"
    before = _parse_toml(text, path)
    lines = text.splitlines(keepends=True)
    region_end = next(
        (i for i, line in enumerate(lines) if _TABLE_HEADER.match(line)), len(lines)
    )
    found = [i for i in range(region_end) if _ROOT_LINE.match(lines[i])]
    if len(found) > 1:
        raise DockerRootsError(f"{path} sets root more than once; fix it by hand")
    rendered = (
        f"{CONTAINERD_ROOT_KEY} = {json.dumps(root)}\n" if root is not None else None
    )
    if found:
        index = found[0]
        lines[index : index + 1] = [rendered] if rendered else []
    elif rendered:
        lines.insert(0, rendered)
    edited = "".join(lines)
    after = _parse_toml(edited, path) if edited.strip() else {}
    if after.get(CONTAINERD_ROOT_KEY) != root or {
        k: v for k, v in after.items() if k != CONTAINERD_ROOT_KEY
    } != {k: v for k, v in before.items() if k != CONTAINERD_ROOT_KEY}:
        raise DockerRootsError(
            f"{path}: root cannot be edited without touching other keys; fix it by hand"
        )
    return edited


def _volume_of(declaration: StorageDeclaration, path: Path) -> str:
    """The declared volume holding ``path`` by longest mount prefix, as ``name (mount)``."""
    holders = [
        v for v in declaration.volumes if v.mount == path or v.mount in path.parents
    ]
    if not holders:
        return "an undeclared volume"
    volume = max(holders, key=lambda v: len(v.mount.parts))
    return f"the {volume.role} volume {volume.name} ({volume.mount})"


def plan_moves(
    placement: Path, etc_root: Path, info: EngineInfo
) -> tuple[RootMove, RootMove]:
    """Both root moves; a move whose config file cannot be edited carries its ``problem``."""
    data_target, containerd_target = targets(placement)
    daemon = daemon_json_path(etc_root)
    problem = ""
    try:
        merge(
            read_text(daemon),
            {DATA_ROOT_KEY: str(data_target)},
            daemon,
            (DATA_ROOT_KEY,),
        )
    except DockerDefaultsError as exc:
        problem = str(exc)
    data = RootMove(DATA_ROOT_NAME, daemon, info.root_dir, data_target, problem)
    config = containerd_config_path(etc_root)
    problem = ""
    current = DEFAULT_CONTAINERD_ROOT
    try:
        text = read_text(config)
        current = Path(
            configured_containerd_root(text, config) or DEFAULT_CONTAINERD_ROOT
        )
        set_containerd_root(text, str(containerd_target), config)
    except (DockerRootsError, DockerDefaultsError) as exc:
        problem = str(exc)
    return data, RootMove(
        CONTAINERD_ROOT_NAME, config, current, containerd_target, problem
    )


def _placement(
    declaration: StorageDeclaration | NotDeclared | None,
) -> Path | None:
    if not isinstance(declaration, StorageDeclaration):
        return None
    placement = declaration.placements.get(DOCKER_ROOT_PLACEMENT)
    return placement.path if placement else None


def _refusal(
    declaration: StorageDeclaration,
    info: EngineInfo,
    stuck: list[RootMove],
    data: RootMove,
) -> str:
    """The plan's refusal, naming the root that would stay where it is and why."""
    if len(stuck) > 1:
        why, move = "neither root can be set", data
    elif info.snapshotter:
        why = (
            f"the engine uses the containerd snapshotter ({SNAPSHOTTER_DRIVER_TYPE}) and the "
            "plan would move only one root"
        )
        move = stuck[0]
    else:
        why, move = "Docker's data-root is the move itself", data
    trap = (
        "; both roots move together or neither (Engine 29 trap)"
        if info.snapshotter
        else ""
    )
    return (
        f"docker roots: refusing: {why}: {move.name} {move.current} would stay on "
        f"{_volume_of(declaration, move.current)}: {move.problem}{trap}"
    )


def plan_docker_roots(
    declaration: StorageDeclaration | NotDeclared | None,
    etc_root: Path,
    run: Runner,
) -> list[str]:
    """What the window would set; mutates nothing. Raises on the Engine 29 trap (T-6.1)."""
    placement = _placement(declaration)
    if placement is None:
        return ["docker roots: not planned (profile declares no docker_root placement)"]
    assert isinstance(declaration, StorageDeclaration)
    require_active_daemon(run)
    info = engine_info(run)
    moves = plan_moves(placement, etc_root, info)
    stuck = [move for move in moves if move.problem]
    data = moves[0]
    if stuck and (info.snapshotter or data in stuck):
        raise DockerRootsError(_refusal(declaration, info, stuck, data))
    lines: list[str] = []
    for move in moves:
        if move.problem:
            lines.append(
                f"docker roots: {move.name} stays at {move.current} ({move.problem}); not "
                f"required: storage driver {info.driver} does not use the containerd snapshotter"
            )
        elif move.current == move.target:
            lines.append(
                f"docker roots: {move.config} already sets {move.name}={move.target}"
            )
        else:
            lines.append(
                f"docker roots: would set {move.name}={move.target} in {move.config} "
                f"(now {move.current})"
            )
    lines += _drop_in_preview(
        placement, etc_root, _on_placement(info.root_dir, placement)
    )
    lines.append(
        f"docker roots: engine {info.version} driver={info.driver} "
        f"containerd-snapshotter={'yes' if info.snapshotter else 'no'}; the move is "
        f"operator-run ({RUNBOOK}); apply performs no root move"
    )
    return lines


def write_roots(placement: Path, etc_root: Path, run: Runner) -> list[str]:
    """The window's config writes: ``data-root`` into ``daemon.json`` and ``root`` into
    containerd's ``config.toml``, both or neither (each edit is computed before either write),
    then the boot-order drop-ins ordering both daemons after the placement's mount."""
    data_target, containerd_target = targets(placement)
    daemon = daemon_json_path(etc_root)
    config = containerd_config_path(etc_root)
    daemon_text = read_text(daemon)
    config_text = read_text(config)
    try:
        daemon_new = merge(
            daemon_text, {DATA_ROOT_KEY: str(data_target)}, daemon, (DATA_ROOT_KEY,)
        )
    except DockerDefaultsError as exc:
        raise DockerRootsError(f"docker roots: nothing written: {exc}") from exc
    config_new = set_containerd_root(config_text, str(containerd_target), config)
    lines = _write_pair(
        run, (daemon, daemon_text, daemon_new), (config, config_text, config_new)
    )
    try:
        lines += write_drop_ins(placement, etc_root, run)
    except DockerRootsError as exc:
        raise DockerRootsError(
            f"{exc}; the root settings are ALREADY WRITTEN: do not start the daemons, run the "
            "runbook's rollback (restore-config)"
        ) from exc
    return [*lines, NOT_RESTARTED]


def restore_roots(  # noqa: PLR0913 -- both keys, whether each file existed, and the seams
    data_root: str | None,
    containerd_root: str | None,
    etc_root: Path,
    run: Runner,
    *,
    daemon_existed: bool = True,
    containerd_existed: bool = True,
) -> list[str]:
    """The rollback's config writes: both keys back to the values recorded before the window
    (None = the key was absent), every other byte as it is now. A file that did not exist before
    the window and holds nothing but the restored state afterwards is removed, not left empty;
    one that gained other content since (a concurrent edit) is kept with only its root restored.
    The boot-order drop-ins the window wrote are removed too, then systemd reloads."""
    daemon = daemon_json_path(etc_root)
    config = containerd_config_path(etc_root)
    daemon_text = read_text(daemon)
    config_text = read_text(config)
    desired = {DATA_ROOT_KEY: data_root} if data_root is not None else {}
    try:
        daemon_new = (
            merge(daemon_text, desired, daemon, (DATA_ROOT_KEY,))
            if daemon_text is not None or desired
            else None
        )
    except DockerDefaultsError as exc:
        raise DockerRootsError(f"docker roots: nothing restored: {exc}") from exc
    config_new = set_containerd_root(config_text, containerd_root, config)
    remove = []
    if not daemon_existed and daemon_new is not None and json.loads(daemon_new) == {}:
        remove.append(daemon)
        daemon_new = None
    if not containerd_existed and config_new is not None and not config_new.strip():
        remove.append(config)
        config_new = None
    lines = _write_pair(
        run,
        (daemon, daemon_text, daemon_new),
        (config, config_text, config_new),
        remove=remove,
    )
    return [*lines, *remove_drop_ins(etc_root, run), NOT_RESTARTED]


def _write_pair(
    run: Runner,
    *files: tuple[Path, str | None, str | None],
    remove: list[Path] | None = None,
) -> list[str]:
    remove = [path for path in remove or [] if path.exists()]
    pending = [
        (path, new) for path, old, new in files if new is not None and new != old
    ]
    lines = [
        f"docker roots: {path} unchanged"
        for path, old, new in files
        if (new is None or new == old) and path not in remove
    ]
    if not pending and not remove:
        return lines
    _completed, refused, command = _sudo(run, ["true"])
    if refused:
        raise DockerRootsError(
            f"docker roots: nothing written: {command} refused: {refused}"
        )
    written: list[Path] = []
    for path, content in pending:
        try:
            sudo_write_file(path, content, run, label="docker roots")
        except DockerDefaultsError as exc:
            half = (
                f"; ALREADY WRITTEN: {', '.join(map(str, written))}: do not start the daemons, "
                "run the runbook's rollback (restore-config)"
                if written
                else ""
            )
            raise DockerRootsError(f"{exc}{half}") from exc
        written.append(path)
        lines.append(f"docker roots: wrote {path}")
    for path in remove:
        _completed, refused, command = _sudo(run, ["rm", "-f", str(path)])
        if refused:
            raise DockerRootsError(
                f"docker roots: {command} refused: {refused}; do not start the daemons"
            )
        lines.append(f"docker roots: removed {path} (absent before the window)")
    return lines


# Boot order. /etc/fstab mounts the data volume `nofail`, and systemd then only Wants that mount
# from local-fs.target without ordering it before the target, so nothing orders either daemon
# after it. A late mount would let dockerd and containerd create fresh, empty roots on the root
# filesystem under the unmounted placement, and the mount would then hide those writes.
# RequiresMountsFor adds Requires= and After= on every mount unit the path needs; on a placement
# that is not a separate mount it orders against the existing ancestors only, which is harmless.


class DropInRefused(DockerRootsError):
    """``sudo -n`` refused before any drop-in was written."""


def drop_in_paths(etc_root: Path) -> tuple[Path, ...]:
    system = etc_root / "systemd" / "system"
    return tuple(system / f"{unit}.d" / MOUNT_DROP_IN for unit in ORDERED_UNITS)


def drop_in_text(placement: Path) -> str:
    return (
        "# Written by ai-harness: start only once the Docker root placement is mounted.\n"
        f"# {RUNBOOK}\n"
        "[Unit]\n"
        f"RequiresMountsFor={placement}\n"
    )


def _differing_drop_ins(placement: Path, etc_root: Path) -> list[Path]:
    desired = drop_in_text(placement)
    try:
        return [p for p in drop_in_paths(etc_root) if read_text(p) != desired]
    except DockerDefaultsError as exc:
        raise DockerRootsError(f"docker boot order: {exc}") from exc


def _daemon_reload(run: Runner) -> str:
    _completed, refused, command = _sudo(run, ["systemctl", "daemon-reload"])
    if refused:
        raise DockerRootsError(
            f"docker boot order: drop-ins changed but {command} refused: {refused}; do not "
            "start the daemons until it succeeds"
        )
    return f"docker boot order: ran {command}; no daemon was restarted"


def write_drop_ins(placement: Path, etc_root: Path, run: Runner) -> list[str]:
    """Write each missing or differing drop-in, then one ``daemon-reload``; a no-op when both
    already match. Raises :class:`DropInRefused` when ``sudo -n`` is refused before any write."""
    pending = _differing_drop_ins(placement, etc_root)
    lines = [
        f"docker boot order: {path} present"
        for path in drop_in_paths(etc_root)
        if path not in pending
    ]
    if not pending:
        return lines
    _completed, refused, command = _sudo(run, ["true"])
    if refused:
        raise DropInRefused(f"{command} refused: {refused}")
    content = drop_in_text(placement)
    for path in pending:
        try:
            sudo_write_file(path, content, run, label="docker boot order")
        except DockerDefaultsError as exc:
            raise DockerRootsError(str(exc)) from exc
        lines.append(f"docker boot order: wrote {path} (RequiresMountsFor={placement})")
    lines.append(_daemon_reload(run))
    return lines


def remove_drop_ins(etc_root: Path, run: Runner) -> list[str]:
    """The rollback's half: remove the drop-ins (and the unit directories they leave empty),
    then one ``daemon-reload``; nothing to do when none is present. The empty-directory removal
    is ``find -maxdepth 0 -empty``, which GNU and BSD find both read the same way, rather than
    GNU rmdir's ``--ignore-fail-on-non-empty``, which BSD rmdir rejects."""
    present = [path for path in drop_in_paths(etc_root) if path.exists()]
    if not present:
        return []
    lines: list[str] = []
    for path in present:
        for argv in (
            ["rm", "-f", str(path)],
            [
                "find",
                str(path.parent),
                "-maxdepth",
                "0",
                "-empty",
                "-exec",
                "rmdir",
                "{}",
                "+",
            ],
        ):
            _completed, refused, command = _sudo(run, argv)
            if refused:
                raise DockerRootsError(
                    f"docker boot order: {command} refused: {refused}; do not start the daemons"
                )
        lines.append(f"docker boot order: removed {path}")
    lines.append(_daemon_reload(run))
    return lines


def _on_placement(root: Path, placement: Path) -> bool:
    return root == placement or placement in root.parents


def _drop_in_preview(placement: Path, etc_root: Path, on: bool) -> list[str]:
    pending = _differing_drop_ins(placement, etc_root)
    lines = []
    for path in drop_in_paths(etc_root):
        if path not in pending:
            lines.append(f"docker boot order: {path} present")
        elif on:
            lines.append(
                f"docker boot order: would write {path} via sudo -n "
                f"(RequiresMountsFor={placement}), then systemctl daemon-reload"
            )
        else:
            lines.append(
                f"docker boot order: {path} absent; write-config writes it in the window"
            )
    return lines


def apply_drop_ins(
    declaration: StorageDeclaration | NotDeclared | None,
    etc_root: Path,
    run: Runner,
) -> list[str]:
    """``apply``'s boot-order step: once ``docker info`` reports a root under the placement, make
    sure both drop-ins are present. Never restarts a daemon; a refused ``sudo -n`` and an
    unreachable daemon are reported states, not failures."""
    placement = _placement(declaration)
    if placement is None:
        return []
    try:
        require_active_daemon(run)
        info = engine_info(run)
    except DockerRootsError as exc:
        return [f"docker boot order: not checked ({exc})"]
    if not _on_placement(info.root_dir, placement):
        return [
            f"docker boot order: not needed yet (DockerRootDir {info.root_dir} is not under "
            f"{placement}; the window's write-config writes the drop-ins)"
        ]
    try:
        return write_drop_ins(placement, etc_root, run)
    except DropInRefused as exc:
        return [f"docker boot order: not applied ({exc})"]


def _configured_roots(etc_root: Path) -> list[Path]:
    daemon = daemon_json_path(etc_root)
    config = containerd_config_path(etc_root)
    roots: list[Path] = []
    text = read_text(daemon)
    if text is not None:
        try:
            value = json.loads(text).get(DATA_ROOT_KEY)
        except (ValueError, AttributeError):
            value = None
        if isinstance(value, str):
            roots.append(Path(value))
    try:
        value = configured_containerd_root(read_text(config), config)
    except DockerRootsError:
        value = None
    if value:
        roots.append(Path(value))
    return roots


def doctor_drop_ins(
    declaration: StorageDeclaration | NotDeclared | None, etc_root: Path
) -> tuple[list[str], bool]:
    """The ordering state from the config files alone (no daemon call): when either root is
    configured under the placement, both drop-ins must be present."""
    placement = _placement(declaration)
    if placement is None:
        return [], True
    if not any(_on_placement(root, placement) for root in _configured_roots(etc_root)):
        return [
            f"docker boot order: not required (no root configured under {placement})"
        ], True
    pending = _differing_drop_ins(placement, etc_root)
    lines = [
        f"docker boot order: MISSING {path} (RequiresMountsFor={placement}); a late mount "
        f"would start the daemon on empty roots; remedy: {APPLY_REMEDY}"
        if path in pending
        else f"docker boot order: ok: {path} orders the daemon after {placement}"
        for path in drop_in_paths(etc_root)
    ]
    return lines, not pending
