"""Host-wide Docker logging defaults under a profile opt-in (ISSUE-REF Phase 5, D-10 (a), T-5.1).

``[machine.<m>.storage]`` may carry ``docker_default_log_driver`` (``local`` or ``json-file``) and,
for ``json-file``, ``docker_default_log_opts`` with ``max-size`` and ``max-file``. ``apply`` merges
exactly the two keys this design owns, ``log-driver`` and ``log-opts``, into
``<etc_root>/docker/daemon.json`` through ``sudo -n``. Every other key keeps its exact bytes: the
merge edits the original text at the owned members' spans and never re-serializes the document.

The write goes to a same-directory temporary file renamed over the target, so the daemon never
reads a partial file. The daemon is never restarted here: the restart is the operator's act, and
only containers created after it pick up the new default. No opt-in, or ``sudo -n`` refused, is a
reported ``not applied`` state rather than a failure, because the opt-in is the operator's choice.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from json.decoder import scanstring
from pathlib import Path
from typing import Any

from .storage import NotDeclared, Runner, StorageDeclaration, _sudo

OWNED_KEYS: tuple[str, ...] = ("log-driver", "log-opts")
#: Phase 6 (D-10 (c)): the root move owns only this key, through the same byte-preserving merge.
DATA_ROOT_KEY = "data-root"
RESTART_LINE = (
    "docker defaults: daemon restart required for {path} to take effect "
    "(operator: sudo systemctl restart docker); containers keep their log config until recreated"
)
_WHITESPACE = " \t\n\r"


class DockerDefaultsError(RuntimeError):
    """The opted-in write could not be completed; the message names the file and the remedy."""


@dataclass(frozen=True)
class _Member:
    key: str
    start: int
    value_start: int
    value_end: int


@dataclass(frozen=True)
class DockerDefaultsResult:
    lines: tuple[str, ...]
    mutated: bool


def daemon_json_path(etc_root: Path) -> Path:
    return etc_root / "docker" / "daemon.json"


def desired_values(declaration: StorageDeclaration) -> dict[str, Any]:
    """The owned keys' target values; an empty opts table means ``log-opts`` is absent."""
    desired: dict[str, Any] = {"log-driver": declaration.docker_default_log_driver}
    if declaration.docker_default_log_opts:
        desired["log-opts"] = dict(declaration.docker_default_log_opts)
    return desired


def _skip(text: str, index: int) -> int:
    while index < len(text) and text[index] in _WHITESPACE:
        index += 1
    return index


def _members(
    text: str, path: Path, owned: tuple[str, ...] = OWNED_KEYS
) -> tuple[list[_Member], int]:
    """The top-level object's members with their exact spans, and the closing brace's index."""
    decoder = json.JSONDecoder()
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DockerDefaultsError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise DockerDefaultsError(f"{path} must hold a JSON object")
    index = _skip(text, 0) + 1
    members: list[_Member] = []
    while True:
        index = _skip(text, index)
        if text[index] == "}":
            break
        if text[index] == ",":
            index = _skip(text, index + 1)
        start = index
        key, index = scanstring(text, index + 1)
        index = _skip(text, index) + 1
        value_start = _skip(text, index)
        _value, value_end = decoder.raw_decode(text, value_start)
        members.append(_Member(key, start, value_start, value_end))
        index = value_end
    keys = [member.key for member in members]
    duplicated = [key for key in owned if keys.count(key) > 1]
    if duplicated:
        raise DockerDefaultsError(
            f"{path} declares {', '.join(duplicated)} more than once; fix it by hand"
        )
    return members, index


def _removal(members: list[_Member], position: int) -> tuple[int, int, str]:
    """The edit deleting one member together with exactly one adjoining comma."""
    member = members[position]
    if position > 0:
        return members[position - 1].value_end, member.value_end, ""
    if position + 1 < len(members):
        return member.start, members[position + 1].start, ""
    return member.start, member.value_end, ""


def merge(
    text: str | None,
    desired: Mapping[str, Any],
    path: Path,
    owned: tuple[str, ...] = OWNED_KEYS,
) -> str:
    """``text`` with exactly the ``owned`` keys set to ``desired`` (absent owned keys removed)."""
    if text is None:
        return json.dumps(dict(desired), indent=2) + "\n"
    members, close = _members(text, path, owned)
    current = json.loads(text)
    edits: list[tuple[int, int, str]] = []
    for position, member in enumerate(members):
        if member.key not in owned:
            continue
        if member.key in desired:
            if current[member.key] != desired[member.key]:
                edits.append(
                    (
                        member.value_start,
                        member.value_end,
                        json.dumps(desired[member.key]),
                    )
                )
        else:
            edits.append(_removal(members, position))
    additions = [key for key in desired if key not in current]
    if additions:
        rendered = "".join(
            f",\n  {json.dumps(key)}: {json.dumps(desired[key])}" for key in additions
        )
        survivors = [m for m in members if m.key not in owned or m.key in desired]
        if survivors:
            anchor = survivors[-1].value_end
            edits.append((anchor, anchor, rendered))
        else:
            edits.append((close, close, rendered.lstrip(",") + "\n"))
    merged = text
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        merged = merged[:start] + replacement + merged[end:]
    return merged


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DockerDefaultsError(f"cannot read {path}: {exc}") from exc


def _describe(desired: Mapping[str, Any]) -> str:
    return ", ".join(f"{key}={json.dumps(value)}" for key, value in desired.items())


def sudo_write_file(path: Path, content: str, run: Runner, *, label: str) -> None:
    """Write ``content`` to a root-owned ``path`` through ``sudo -n``: a same-directory temporary
    file renamed over the target, so a reader never sees a partial file; re-read to prove it."""
    temporary = path.with_name(f".{path.name}.aih-{os.getpid()}.tmp")
    steps: list[tuple[list[str], str | None]] = []
    if not path.parent.is_dir():
        steps.append((["mkdir", "-m", "0755", str(path.parent)], None))
    steps += [
        (["tee", str(temporary)], content),
        (["chmod", "0644", str(temporary)], None),
        (["mv", "-f", str(temporary), str(path)], None),
    ]
    for argv, payload in steps:
        command = ["sudo", "-n", *argv]
        try:
            completed = run(
                command, input=payload, capture_output=True, text=True, check=False
            )
            failure = (
                ((completed.stderr or "").strip() or f"rc={completed.returncode}")
                if completed.returncode != 0
                else ""
            )
        except OSError as exc:
            failure = str(exc)
        if failure:
            if argv[0] != "mkdir":
                _sudo(run, ["rm", "-f", str(temporary)])
            raise DockerDefaultsError(
                f"{label}: writing {path} failed at {' '.join(command)}: {failure}; "
                f"nothing was restarted; remedy: re-run once sudo -n {argv[0]} succeeds"
            )
    if read_text(path) != content:
        raise DockerDefaultsError(
            f"{label}: {path} does not hold the merged content after the rename"
        )


def plan_docker_defaults(
    declaration: StorageDeclaration | NotDeclared | None, etc_root: Path
) -> list[str]:
    """What ``apply`` would write; reads the file unprivileged and mutates nothing."""
    if not isinstance(declaration, StorageDeclaration) or not (
        declaration.docker_default_log_driver
    ):
        return ["docker defaults: not applied (profile does not opt in)"]
    path = daemon_json_path(etc_root)
    desired = desired_values(declaration)
    text = read_text(path)
    if text is not None and merge(text, desired, path) == text:
        return [f"docker defaults: {path} already sets {_describe(desired)}"]
    return [f"write {path} via sudo -n: {_describe(desired)}"]


def apply_docker_defaults(
    declaration: StorageDeclaration | NotDeclared | None,
    etc_root: Path,
    run: Runner,
) -> DockerDefaultsResult:
    """Merge the owned keys into ``daemon.json`` through ``sudo -n``; never restart the daemon."""
    if not isinstance(declaration, StorageDeclaration) or not (
        declaration.docker_default_log_driver
    ):
        return DockerDefaultsResult(
            ("docker defaults: not applied (profile does not opt in)",), False
        )
    path = daemon_json_path(etc_root)
    desired = desired_values(declaration)
    text = read_text(path)
    merged = merge(text, desired, path)
    if merged == text:
        return DockerDefaultsResult(
            (f"docker defaults: {path} already sets {_describe(desired)}",), False
        )
    _completed, refused, _cmd = _sudo(run, ["true"])
    if refused:
        return DockerDefaultsResult(
            (f"docker defaults: not applied (sudo -n refused: {refused})",), False
        )
    sudo_write_file(path, merged, run, label="docker defaults")
    return DockerDefaultsResult(
        (
            f"docker defaults: wrote {path}: {_describe(desired)}",
            RESTART_LINE.format(path=path),
        ),
        True,
    )
