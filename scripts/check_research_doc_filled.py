#!/usr/bin/env python3
"""Reject completed research documents that retain scaffold front matter or context."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SCAFFOLD_NAMES = {"TEMPLATE.md", "STUB.md", "README.md"}
FRONTMATTER_SCAN_LINES = 40
FRONTMATTER_RE = re.compile(r"^(?P<key>[a-z_]+):[ \t]*(?P<value>.*)$", re.MULTILINE)
SCAFFOLD_VALUES = {"", "none", "aido-stub", "[research topic]"}
CONTEXT_NONE_RE = re.compile(r"^## Context\s*\n\s*None\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Verdict:
    path: Path
    reasons: tuple[str, ...]

    @property
    def unfilled(self) -> bool:
        return bool(self.reasons)


def is_research_doc(path: Path) -> bool:
    """A research doc, not an evidence sidecar under ``docs/research/evidence/``.

    An evidence file holds commands and verbatim outputs and carries no scaffold front matter,
    so the filled-state check has nothing to read in it.
    """
    return (
        path.suffix == ".md"
        and path.name not in SCAFFOLD_NAMES
        and any(
            path.parts[index : index + 2] == ("docs", "research")
            and path.parts[index + 2 : index + 3] != ("evidence",)
            for index in range(len(path.parts) - 1)
        )
    )


def inspect(path: Path, text: str) -> Verdict:
    head = "\n".join(text.splitlines()[:FRONTMATTER_SCAN_LINES])
    fields = {
        match.group("key"): match.group("value").strip().strip('"')
        for match in FRONTMATTER_RE.finditer(head)
    }
    reasons: list[str] = []
    if fields.get("status", "").lower() in {"stub", "template"}:
        reasons.append("front matter still says the document is a stub/template")
    if fields.get("stub", "").lower() == "true":
        reasons.append("front matter still says `stub: true`")
    if fields.get("source", "").lower() in SCAFFOLD_VALUES:
        reasons.append("front matter still has a scaffold source value")
    if CONTEXT_NONE_RE.search(text):
        reasons.append("the immutable Context still says `None`")
    return Verdict(path, tuple(reasons))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reject completed research documents that retain scaffold values."
    )
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)
    failures: list[Verdict] = []
    for path in args.paths or sorted(Path("docs/research").glob("*.md")):
        if not is_research_doc(path):
            continue
        try:
            verdict = inspect(path, path.read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"{path}: could not read ({exc})", file=sys.stderr)
            return 1
        if verdict.unfilled:
            failures.append(verdict)
    for verdict in failures:
        print(
            f"{verdict.path}: research document is still an UNFILLED SCAFFOLD",
            file=sys.stderr,
        )
        for reason in verdict.reasons:
            print(f"    - {reason}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
