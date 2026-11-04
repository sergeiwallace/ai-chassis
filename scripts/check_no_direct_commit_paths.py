#!/usr/bin/env python3
# ruff: noqa: E402
"""Reject direct Git commit producers outside the canonical ``provenance-commit`` wrapper.

The installed Claude skills tree is a whole-tree link to ``skills/`` (install.sh
Step 8), so this gate scans that source tree rather than a machine-local install
path.  It also scans executable shell and Python files in ``scripts/``.  Markdown
is examined only inside fenced command examples; prose and inline literals are
not commands.

``scripts/provenance_commit.py`` is the approved canonical constructor.  A low-level
test or command-inspection tool that contains a syntactically direct command but
never creates a commit may exempt its next command with:

    # provenance-commit-gate: exempt -- <why this code never creates a commit>

The reason is required.  Keep exemptions narrow: this marker is for parsers and
fixtures, not for a producer that should route through ``provenance-commit``.
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

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

COMMAND_RE = re.compile(r"\bgit\s+commit\b")
EXEMPT_RE = re.compile(r"provenance-commit-gate:\s*exempt\s*--\s*(?P<reason>\S.*)")
APPROVED_PATHS = {Path("scripts/provenance_commit.py")}
SCRIPT_SUFFIXES = {".py", ".sh", ".bash"}
ROOT_SCRIPT_PATHS = (Path("setup.sh"),)


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    text: str


def _is_exempt(lines: list[str], line: int) -> bool:
    """Whether the line has the documented, reasoned exemption immediately above."""
    return line > 1 and bool(EXEMPT_RE.search(lines[line - 2]))


def _shell_commands(line: str) -> list[tuple[int, str]]:
    """Return command fragments outside shell quotes and comments.

    This deliberately recognizes only commands whose command word is ``git``.
    That avoids treating an echo/help string as an invocation while covering the
    normal ``git commit`` forms used by shell producers and skill code blocks.
    """
    code: list[str] = []
    quote: str | None = None
    escaped = False
    for char in line:
        if escaped:
            code.append(" ")
            escaped = False
        elif char == "\\" and quote != "'":
            code.append(" ")
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
            code.append(" ")
        elif char == "#":
            break
        else:
            code.append(char)
    text = "".join(code)
    matches: list[tuple[int, str]] = []
    for fragment in re.split(r"(?:&&|\|\||[;|])", text):
        command = fragment.strip()
        command = re.sub(r"^(?:if|then|do|!|command)\s+", "", command)
        command = re.sub(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)+", "", command)
        if COMMAND_RE.match(command):
            matches.append((line.find(fragment) + 1, command))
    return matches


def _literal_command(node: ast.AST) -> list[str] | None:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    values = []
    for element in node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        values.append(element.value)
    return values


def _is_command_runner(node: ast.Call) -> bool:
    """Whether *node* invokes one of the standard Python command runners."""
    function = node.func
    return isinstance(function, ast.Attribute) and function.attr in {
        "run",
        "call",
        "check_call",
        "check_output",
        "Popen",
        "system",
    }


def _python_violations(path: Path, lines: list[str]) -> list[Violation]:
    try:
        tree = ast.parse("\n".join(lines), filename=str(path))
    except SyntaxError:
        return []
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or not node.args
            or not _is_command_runner(node)
        ):
            continue
        command = _literal_command(node.args[0])
        shell_command = (
            node.args[0].value
            if isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            else ""
        )
        if (
            command is None or command[:2] != ["git", "commit"]
        ) and not COMMAND_RE.search(shell_command):
            continue
        line = node.lineno
        if not _is_exempt(lines, line):
            violations.append(Violation(path, line, lines[line - 1].strip()))
    return violations


def scan_text(path: Path, text: str) -> list[Violation]:
    """Find direct commit invocations in one supported source file."""
    if path in APPROVED_PATHS:
        return []
    lines = text.splitlines()
    if path.suffix == ".py":
        return _python_violations(path, lines)

    violations: list[Violation] = []
    in_fence = False
    for line_number, line in enumerate(lines, start=1):
        if path.suffix == ".md" and line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if path.suffix == ".md" and not in_fence:
            continue
        if _is_exempt(lines, line_number):
            continue
        for _, command in _shell_commands(line):
            violations.append(Violation(path, line_number, command))
    return violations


def scan_repo(repo_root: Path) -> list[Violation]:
    """Scan installed-source skills and repository scripts deterministically."""
    violations: list[Violation] = []
    for path in sorted((repo_root / "skills").rglob("*.md")):
        violations.extend(
            scan_text(path.relative_to(repo_root), path.read_text(encoding="utf-8"))
        )
    for path in sorted((repo_root / "scripts").rglob("*")):
        if path.is_file() and path.suffix in SCRIPT_SUFFIXES:
            violations.extend(
                scan_text(path.relative_to(repo_root), path.read_text(encoding="utf-8"))
            )
    for relative_path in ROOT_SCRIPT_PATHS:
        path = repo_root / relative_path
        if path.is_file():
            violations.extend(
                scan_text(relative_path, path.read_text(encoding="utf-8"))
            )
    return violations


def main(argv: list[str] | None = None) -> int:
    del argv
    repo_root = Path(__file__).resolve().parent.parent
    violations = scan_repo(repo_root)
    if not violations:
        return 0
    print(
        "REJECTED: direct git commit path outside provenance-commit.", file=sys.stderr
    )
    for violation in violations:
        print(f"  {violation.path}:{violation.line}: {violation.text}", file=sys.stderr)
    print(
        "Route commit producers through provenance-commit or add a reasoned low-level exemption.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
