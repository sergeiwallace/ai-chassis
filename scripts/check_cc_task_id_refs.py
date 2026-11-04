#!/usr/bin/env python3
"""Reject ambiguous bare Claude Code task ids in persisted files."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

BASELINE_PATH = Path("config/cc-task-id-baseline.txt")
SCOPED_SUFFIXES = {".md", ".py", ".sh", ".bash", ".yaml", ".yml", ".toml", ".jinja"}
# What may sit between the word `task` and the `#` without breaking the construction.
# `\s` alone was the whole defect (G68): in markdown, bold and a code span are the two
# normal ways to write an id, so the gate fired on the LEAST likely spelling and passed the
# two most likely ones. That is worse than a gap in coverage -- it teaches the author which
# spelling to use, and the author never learns a check was skipped.
#
# Deliberately NOT the table pipe. `|` would let the construction cross a column boundary,
# so `| task | #1590 |` -- an id column beside a PR column -- would read as a bare task id.
# Measured over every tracked scoped file: adding `|` caught exactly ZERO lines that the
# emphasis and code-span characters did not already catch, so it is pure false-positive risk
# for no detection. The bare `| #NN |` form G68 also names is out of reach by construction:
# it carries no `task` word at all, and matching a lone `#NN` in a table cell cannot be
# distinguished from the PR references this gate must keep passing.
_MARKUP = r"[\s*_`]*"
# `\b` cannot close the number here: `_` is a word character, so an underscore-emphasised
# `task _#NN_` has no boundary between the final digit and the closing underscore, and the
# whole match was lost. A non-digit lookahead keeps the property `\b` was actually there for
# -- a short id must not match inside a longer number -- without caring what markup follows.
# (`#NN` stands in for digits throughout this file. Writing real ones would make these
# comments citations, and this gate would rightly flag them.)
_END = r"(?!\d)"
CONSTRUCTION = re.compile(
    rf"\b(?:CC\s+)?tasks?\b{_MARKUP}#(?P<number>\d{{1,5}}){_END}", re.IGNORECASE
)
CONTINUATION = re.compile(rf"{_MARKUP}(?:[-,]){_MARKUP}#(?P<number>\d{{1,5}}){_END}")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
IMMUTABLE_START = re.compile(
    r"<!--\s*doc:region\b[^>]*\bkind=[\"']immutable[\"'][^>]*-->"
)
IMMUTABLE_END = re.compile(r"<!--\s*/doc:region\s*-->")
ALLOW_MARKER = re.compile(r"^\s*<!--\s*cc-task-id-lint:\s*allow(?:\s+(.*?))?\s*-->\s*$")


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    match: str
    fingerprint: str
    occurrence: int
    message: str = "bare CC task id"

    @property
    def key(self) -> tuple[str, str, str, int]:
        return (self.path, self.match, self.fingerprint, self.occurrence)


def _normalize_line(line: str) -> str:
    return line.rstrip()


def _fingerprint(scope: str, line: str) -> str:
    content = f"{scope}\n{_normalize_line(line)}".encode()
    return hashlib.sha256(content).hexdigest()


def _python_top_level_scopes(text: str) -> dict[int, str]:
    """Map each Python line to its enclosing top-level class or function."""
    if not text.endswith("\n"):
        text += "\n"
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    scopes: dict[int, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            label = f"{type(node).__name__}:{node.name}"
            scopes.update({line: label for line in range(node.lineno, end + 1)})
    return scopes


def scan_text(text: str, path: str) -> list[Violation]:
    """Return non-exempt bare CC task-id references in *text*."""
    violations: list[Violation] = []
    heading_stack = [""] * 6
    python_scopes = _python_top_level_scopes(text) if path.endswith(".py") else {}
    immutable_depth = 0
    previous_allow = False
    occurrences: defaultdict[tuple[str, str, str], int] = defaultdict(int)

    for line_no, line in enumerate(text.splitlines(), start=1):
        scope = python_scopes.get(
            line_no, " > ".join(item for item in heading_stack if item)
        )
        marker = ALLOW_MARKER.match(line)
        if marker and not (marker.group(1) or "").strip():
            fingerprint = _fingerprint(scope, line)
            key = (path, "cc-task-id-lint: allow", fingerprint)
            occurrences[key] += 1
            violations.append(
                Violation(
                    path,
                    line_no,
                    "cc-task-id-lint: allow",
                    fingerprint,
                    occurrences[key],
                    "cc-task-id-lint allow marker requires a reason",
                )
            )

        if IMMUTABLE_END.search(line):
            immutable_depth = max(0, immutable_depth - 1)
        is_immutable = immutable_depth > 0
        if IMMUTABLE_START.search(line):
            immutable_depth += 1
            is_immutable = True

        heading_match = HEADING.match(line)
        if heading_match:
            level = len(line) - len(line.lstrip("#"))
            heading_stack[level - 1] = heading_match.group(1).strip()
            for index in range(level, len(heading_stack)):
                heading_stack[index] = ""

        if not is_immutable and not previous_allow:
            for construction in CONSTRUCTION.finditer(line):
                matches = [
                    (
                        construction.start("number") - 1,
                        f"#{construction.group('number')}",
                    )
                ]
                cursor = construction.end()
                while continuation := CONTINUATION.match(line, cursor):
                    matches.append(
                        (
                            continuation.start("number") - 1,
                            f"#{continuation.group('number')}",
                        )
                    )
                    cursor = continuation.end()
                for start, matched in matches:
                    # The construction grammar already excludes owner/repo, PR,
                    # issue, URL, namespace-qualified, and link-fragment forms.
                    # Do not maintain unreachable semantic exemptions for them.
                    del start
                    fingerprint = _fingerprint(scope, line)
                    key = (path, matched, fingerprint)
                    occurrences[key] += 1
                    violations.append(
                        Violation(path, line_no, matched, fingerprint, occurrences[key])
                    )

        previous_allow = bool(marker and (marker.group(1) or "").strip())

    return violations


def baseline_entries(violations: list[Violation]) -> set[tuple[str, str, str, int]]:
    return {violation.key for violation in violations}


def load_baseline(path: Path) -> set[tuple[str, str, str, int]]:
    if not path.exists():
        return set()
    entries: set[tuple[str, str, str, int]] = set()
    for line_no, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line:
            continue
        try:
            entry = json.loads(line)
            entries.add(
                (
                    entry["path"],
                    entry["match"],
                    entry["fingerprint"],
                    entry["occurrence"],
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(f"invalid baseline entry at {path}:{line_no}") from exc
    return entries


def _baseline_from_bytes(raw: bytes, source: str) -> set[tuple[str, str, str, int]]:
    entries: set[tuple[str, str, str, int]] = set()
    for line_no, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line:
            continue
        try:
            entry = json.loads(line)
            entries.add(
                (
                    entry["path"],
                    entry["match"],
                    entry["fingerprint"],
                    entry["occurrence"],
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(f"invalid baseline entry at {source}:{line_no}") from exc
    return entries


def merge_base_baseline(
    root: Path,
) -> set[tuple[str, str, str, int]] | None:
    """Read the immutable ratchet ceiling from origin/main's merge base."""
    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", "origin/main"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if merge_base.returncode:
        raise RuntimeError("cannot resolve origin/main merge base for baseline ratchet")
    result = subprocess.run(
        ["git", "show", f"{merge_base.stdout.strip()}:{BASELINE_PATH.as_posix()}"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        # The first baseline introduced on a branch has no historical ceiling.
        # Once it reaches origin/main, every later run is strictly shrink-only.
        return None
    return _baseline_from_bytes(
        result.stdout, f"{merge_base.stdout.strip()}:{BASELINE_PATH}"
    )


def write_baseline(path: Path, violations: list[Violation]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = sorted(violations, key=lambda item: item.key)
    path.write_text(
        "".join(
            json.dumps(
                {
                    "path": item.path,
                    "match": item.match,
                    "fingerprint": item.fingerprint,
                    "occurrence": item.occurrence,
                    "line": item.line,
                },
                sort_keys=True,
            )
            + "\n"
            for item in entries
        ),
        encoding="utf-8",
    )


def repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("cc-task-id-refs must be run inside a Git repository")
    return Path(result.stdout.strip())


def tracked_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    )
    return [root / path for path in result.stdout.splitlines() if path]


def relative_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def source_label(*, staged: bool) -> str:
    """What `scan_paths` actually read, in the words a reader needs (ISSUE-REF).

    Printed alongside every finding because the two sources disagree exactly when it matters: a
    fix that is on disk but unstaged. Measured 2026-09-27 -- nine edits were saved, and re-running
    this tool on the same paths reprinted all ten findings with their ORIGINAL line numbers, which
    an inserted line had already invalidated. Only `git add` moved them. The failure direction is
    the expensive one: it cannot hide a real violation, so nothing unsafe merges, but it makes a
    CORRECT fix look ineffective and invites abandoning the right remedy for a wrong one.
    """
    return (
        "the git INDEX (staged content)"
        if staged
        else "the WORKING TREE (files on disk)"
    )


def scan_paths(root: Path, paths: list[Path], *, staged: bool) -> list[Violation]:
    violations: list[Violation] = []
    for path in paths:
        if path == root / BASELINE_PATH or path.suffix not in SCOPED_SUFFIXES:
            continue
        try:
            if staged:
                relative = relative_path(root, path)
                result = subprocess.run(
                    ["git", "show", f":{relative}"],
                    cwd=root,
                    capture_output=True,
                    check=False,
                )
                if result.returncode:
                    continue
                text = result.stdout.decode("utf-8")
            else:
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        violations.extend(scan_text(text, relative_path(root, path)))
    return violations


def _paths_from_args(root: Path, names: list[str]) -> list[Path]:
    if not names:
        return tracked_paths(root)
    paths: list[Path] = []
    for name in names:
        path = (Path.cwd() / name).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError(f"path is outside repository: {name}") from exc
        paths.append(path)
    return paths


def _report(violations: list[Violation], *, staged: bool) -> None:
    if violations:
        print(
            f"cc-task-id-refs: read {source_label(staged=staged)}",
            file=sys.stderr,
        )
    for violation in violations:
        print(
            f"{violation.path}:{violation.line}: {violation.message}: {violation.match}; "
            "replace it with the backing Beads display id or a namespace-qualified id "
            "such as ISSUE-REF#299",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="files supplied by pre-commit")
    parser.add_argument(
        "--scan", action="store_true", help="scan tracked working-tree files"
    )
    parser.add_argument(
        "--init-baseline",
        action="store_true",
        help="write a baseline if one does not exist",
    )
    # ISSUE-REF: the source used to be INFERRED -- any explicit path meant "read the index",
    # because the only caller in mind was pre-commit. A human or an agent re-measuring a fix
    # passes the same explicit paths and means the opposite, and the two invocations are
    # indistinguishable on the command line. So the index is now opt-in and named, and the
    # pre-commit hook passes it (see .pre-commit-config.yaml); every other caller reads disk,
    # which is what "re-run it on these files" has always meant everywhere else.
    parser.add_argument(
        "--staged",
        action="store_true",
        help="read the git index rather than the working tree (what pre-commit wants)",
    )
    args = parser.parse_args(argv)
    if args.scan and args.files:
        parser.error("--scan does not accept file arguments")
    if args.init_baseline and (args.files or args.scan):
        parser.error("--init-baseline does not accept --scan or file arguments")
    if args.staged and not args.files:
        parser.error("--staged needs explicit file arguments")

    staged = args.staged
    try:
        root = repo_root()
        paths = _paths_from_args(
            root, [] if (args.scan or args.init_baseline) else args.files
        )
        violations = scan_paths(root, paths, staged=staged)
    except (RuntimeError, ValueError) as exc:
        print(f"cc-task-id-refs: {exc}", file=sys.stderr)
        return 1

    baseline_path = root / BASELINE_PATH
    if args.init_baseline:
        if baseline_path.exists():
            print(
                f"cc-task-id-refs: refusing to overwrite {BASELINE_PATH}",
                file=sys.stderr,
            )
            return 1
        write_baseline(baseline_path, violations)
        print(f"wrote {BASELINE_PATH} with {len(violations)} entries")
        return 0

    try:
        baseline = load_baseline(baseline_path)
        merge_base = merge_base_baseline(root) if baseline_path.exists() else set()
    except ValueError as exc:
        print(f"cc-task-id-refs: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"cc-task-id-refs: {exc}", file=sys.stderr)
        return 1
    if merge_base is not None and not baseline <= merge_base:
        print(
            "cc-task-id-refs: baseline may only shrink from origin/main merge base",
            file=sys.stderr,
        )
        return 1
    new_violations = [item for item in violations if item.key not in baseline]
    if new_violations:
        _report(new_violations, staged=staged)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
