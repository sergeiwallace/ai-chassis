#!/usr/bin/env python3
# ruff: noqa: E402
"""Resolve instruction references or require a current typed exemption.

Exemption files use closed-schema ``[exemptions."<reference>"]`` tables with
``owner``, ISO ``review_by``, and ``reason`` fields.
"""

from __future__ import annotations

try:
    from py_interpreter_bootstrap import ensure_supported_python
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):
        """Stand in for the bootstrap when this file was copied without it.

        A partial copy cannot re-exec, because the module that knows how is the one
        missing. Staying silent when the running interpreter ALREADY meets the floor is
        correct rather than lax: there was nothing to enforce. Below the floor there is
        no remedy left to apply, so say so and let the real failure happen where it can
        be read, instead of dying here on an import that is not the problem.
        """
        import sys as _sys

        if _sys.version_info < minimum:
            _sys.stderr.write(
                "warning: {} is running under Python {} without its interpreter "
                "bootstrap, so the {}.{}+ floor could not be enforced\n".format(
                    __file__,
                    ".".join(str(p) for p in _sys.version_info[:3]),
                    minimum[0],
                    minimum[1],
                )
            )


ensure_supported_python()

import argparse
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

EXTERNAL_RE = re.compile(r"https?://[^\s<>`\"]+")
PATH_RE = re.compile(r"(?<![\w/])((?:/|\.\.?/|~/)?[\w~.${},%-]+/(?:[\w./${},%-]+))")
COMMAND_RE = re.compile(
    r"\b(?:run|invoke|execute|command|required development tool)\s*(?:is|:)?\s*"
    r"`([^`\n]+)`",
    re.IGNORECASE,
)
SHELL_FENCE_RE = re.compile(
    r"```(?:bash|sh|shell|zsh)[^\n]*\n(.*?)```", re.IGNORECASE | re.DOTALL
)
EXEMPTION_FIELDS = {"owner", "review_by", "reason"}
EXEMPTION_KINDS = {"dynamic", "external"}
SHELL_NON_COMMANDS = {
    "do",
    "done",
    "else",
    "fi",
    "for",
    "if",
    "then",
    "while",
}


def load_exemptions(path: Path | None) -> dict[str, dict[str, str]]:
    """Load closed-schema exemption records keyed by reference."""
    if path is None:
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    rows = data.get("exemptions", {})
    if set(data) != {"exemptions"} or not isinstance(rows, dict):
        raise TypeError("instruction exemptions must be a table")

    result: dict[str, dict[str, str]] = {}
    for reference, record in rows.items():
        label = f"exemption {reference}"
        if not isinstance(record, dict) or set(record) != EXEMPTION_FIELDS:
            raise ValueError(f"{label}: owner, review_by, and reason are required")
        if not all(
            isinstance(record[key], str) and record[key].strip()
            for key in EXEMPTION_FIELDS
        ):
            raise ValueError(f"{label}: blank or non-string typed field")
        try:
            review_by = date.fromisoformat(record["review_by"])
        except ValueError as error:
            raise ValueError(
                f"exemption {reference}: review_by must be an ISO date"
            ) from error
        if review_by < datetime.now(UTC).date():
            raise ValueError(f"exemption {reference}: review_by is expired")
        result[reference] = record
    return result


def _clean_reference(reference: str) -> str:
    return reference.rstrip(".,):;!?]'")


def _path_references(text: str) -> list[tuple[str, str]]:
    references: list[tuple[str, str]] = []
    external_spans: list[tuple[int, int]] = []
    for match in EXTERNAL_RE.finditer(text):
        reference = _clean_reference(match.group())
        references.append((reference, "external"))
        external_spans.append(match.span())
    for match in PATH_RE.finditer(text):
        if any(start <= match.start() < end for start, end in external_spans):
            continue
        reference = _clean_reference(match.group(1))
        kind = "dynamic" if any(marker in reference for marker in "{}$%~") else "path"
        references.append((reference, kind))
    return references


def _command_name(command: str) -> str | None:
    try:
        words = shlex.split(command, comments=True)
    except ValueError:
        return None
    for word in words:
        if "=" in word and not word.startswith(("./", "../", "/")):
            continue
        return word
    return None


def _command_references(text: str) -> list[tuple[str, str]]:
    references: list[tuple[str, str]] = []
    snippets = [match.group(1) for match in COMMAND_RE.finditer(text)]
    for fence in SHELL_FENCE_RE.finditer(text):
        for line in fence.group(1).splitlines():
            line = line.strip().removeprefix("$ ")
            if line and not line.startswith("#"):
                snippets.append(line)
    for snippet in snippets:
        command = _command_name(snippet)
        if command and command not in SHELL_NON_COMMANDS:
            kind = (
                "dynamic" if any(marker in command for marker in "{}$%") else "command"
            )
            references.append((command, kind))
    return references


def _references(text: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    result = []
    for reference, kind in _path_references(text) + _command_references(text):
        if reference not in seen:
            seen.add(reference)
            result.append((reference, kind))
    return result


def _tracked_paths(root: Path) -> set[str] | None:
    env = os.environ.copy()
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_PREFIX",
    ):
        env.pop(name, None)
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=False,
            capture_output=True,
            env=env,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    }


def _is_tracked(reference: str, tracked: set[str]) -> bool:
    normalized = Path(reference).as_posix().removeprefix("./").rstrip("/")
    return normalized in tracked or any(
        path.startswith(f"{normalized}/") for path in tracked
    )


def _exemption_error(
    path: Path,
    reference: str,
    exemptions: dict[str, dict[str, str]],
) -> str | None:
    if reference not in exemptions:
        return f"{path}: dynamic/external reference {reference} needs a typed exemption"
    return None


@dataclass(frozen=True)
class ReferenceContext:
    path: Path
    root: Path
    resolved_root: Path
    exemptions: dict[str, dict[str, str]]


def _reference_errors(
    context: ReferenceContext,
    reference: str,
    kind: str,
    tracked: set[str] | None,
) -> tuple[list[str], set[str] | None]:
    errors: list[str] = []
    if kind in EXEMPTION_KINDS:
        error = _exemption_error(context.path, reference, context.exemptions)
        if error:
            errors.append(error)
    elif kind == "command":
        error = (
            None
            if shutil.which(reference)
            else f"{context.path}: unresolved command/tool {reference}"
        )
        if error:
            errors.append(error)
    elif not (target := (context.root / reference).resolve()).is_relative_to(
        context.resolved_root
    ):
        error = (
            None
            if reference in context.exemptions
            else f"{context.path}: reference {reference} resolves outside repository root"
        )
        if error:
            errors.append(error)
    elif not target.exists():
        errors.append(f"{context.path}: unresolved reference {reference}")
    else:
        tracked = tracked if tracked is not None else _tracked_paths(context.root)
        if tracked is None:
            errors.append(
                f"{context.path}: cannot verify tracked reference {reference} in {context.root}"
            )
        elif not _is_tracked(reference, tracked):
            errors.append(f"{context.path}: reference {reference} is not tracked")
    return errors, tracked


def validate(
    path: Path, root: Path, exemptions: dict[str, dict[str, str]] | None = None
) -> list[str]:
    """Validate references against the consumer repository and current ``PATH``."""
    errors = []
    exemptions = exemptions or {}
    resolved_root = root.resolve()
    context = ReferenceContext(path, root, resolved_root, exemptions)
    tracked: set[str] | None = None
    for reference, kind in _references(path.read_text(encoding="utf-8")):
        item_errors, tracked = _reference_errors(context, reference, kind, tracked)
        errors.extend(item_errors)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--exemptions", type=Path)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="consumer repository root (default: current working directory)",
    )
    args = parser.parse_args()
    try:
        exemptions = load_exemptions(args.exemptions)
    except (OSError, TypeError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"instruction exemptions: {error}")
        return 1
    errors = [
        error for path in args.paths for error in validate(path, args.root, exemptions)
    ]
    print("\n".join(errors))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
