#!/usr/bin/env python3
"""Verify that every table-of-contents link in a Markdown doc resolves to a real anchor.

A table of contents whose anchors do not resolve is worse than no table of contents: it
looks like working navigation and silently is not. This checker never guesses. It parses
the document's real headings, derives the GitHub-style slug for each, collects every
explicit ``<a id="..."></a>`` anchor, then confirms that every ``](#target)`` link inside
the Table of Contents section points at one of them.

Usage::

    check_doc_toc.py [--require-toc] [--min-lines N] PATH [PATH ...]

Exit status is 0 when every checked file passes and 1 when any file fails, so it works
as a gate. ``--require-toc`` additionally fails a file that has no ToC at all (with
``--min-lines`` to exempt short docs).
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

TOC_HEADING = "## Table of Contents"

_FENCE = re.compile(r"^(?P<fence>```+|~~~+)")
_HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.*?)\s*$")
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MD_LINK = re.compile(r"\[(?P<text>[^\]]*)\]\((?P<href>[^)]*)\)")
_EXPLICIT_ANCHOR = re.compile(r"""<a\s+id=["'](?P<id>[^"']+)["']""", re.IGNORECASE)
_INLINE_MARKS = re.compile(r"(\*\*|__|\*|`)")
# A decision's stable identifier, as it appears at the start of a detail heading or a ToC
# label: `D-7: ...`, `D-13 — ...`. The separator is deliberately loose because the fleet's
# docs use a colon, an em dash and a plain space interchangeably.
_DECISION_ID = re.compile(r"^D-(?P<id>[0-9]+)\b")
_DECISION_SUMMARY_ROW = re.compile(r"^\|\s*D-(?P<id>[0-9]+)\s*\|", re.MULTILINE)
DECISION_HEADING_LEVEL = 4
# github-slugger keeps Unicode letters/digits/marks plus hyphen, underscore and space.
_SLUG_KEEP_EXTRA = {"-", "_", " "}


def strip_inline_markdown(text: str) -> str:
    """Reduce heading source to the text content a Markdown renderer would emit."""
    text = _HTML_COMMENT.sub("", text)
    text = _MD_LINK.sub(lambda m: m.group("text"), text)
    text = _HTML_TAG.sub("", text)
    text = _INLINE_MARKS.sub("", text)
    return text.strip()


def slugify(heading_text: str) -> str:
    """Derive the GitHub anchor slug for a heading's rendered text.

    Mirrors github-slugger: lowercase, drop every character that is not a Unicode
    letter/digit/mark, hyphen, underscore or space, then map each remaining space to a
    hyphen. Runs of spaces are deliberately NOT collapsed, which is why an em-dash
    heading such as ``Round 1 - Main Audit`` yields a double hyphen.
    """
    text = strip_inline_markdown(heading_text).lower()
    kept = []
    for ch in text:
        if ch in _SLUG_KEEP_EXTRA:
            kept.append(ch)
            continue
        if unicodedata.category(ch)[0] in {"L", "N", "M"}:
            kept.append(ch)
    return "".join(kept).replace(" ", "-")


@dataclass
class Heading:
    level: int
    text: str
    slug: str
    line: int


def parse_headings(text: str) -> list[Heading]:
    """Every ATX heading outside fenced code blocks, with duplicate-slug suffixing."""
    headings: list[Heading] = []
    seen: dict[str, int] = {}
    fence: str | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        fence_match = _FENCE.match(stripped)
        if fence_match:
            marker = fence_match.group("fence")[0] * 3
            if fence is None:
                fence = marker
            elif stripped.startswith(fence):
                fence = None
            continue
        if fence is not None:
            continue
        match = _HEADING.match(raw)
        if match is None:
            continue
        base = slugify(match.group("text"))
        count = seen.get(base, 0)
        seen[base] = count + 1
        slug = base if count == 0 else f"{base}-{count}"
        headings.append(
            Heading(
                level=len(match.group("hashes")),
                text=strip_inline_markdown(match.group("text")),
                slug=slug,
                line=lineno,
            )
        )
    return headings


def explicit_anchors(text: str) -> set[str]:
    """Every ``<a id="...">`` target in the doc, including inside HTML comments."""
    return {m.group("id") for m in _EXPLICIT_ANCHOR.finditer(text)}


def toc_bounds(text: str) -> tuple[int, int] | None:
    """(start, end) line indices of the ToC section body, or None when absent.

    The section runs from the ``## Table of Contents`` heading to the next heading of
    the same or higher level.
    """
    lines = text.splitlines()
    start = None
    for idx, raw in enumerate(lines):
        if raw.strip() == TOC_HEADING:
            start = idx
            break
    if start is None:
        return None
    for idx in range(start + 1, len(lines)):
        match = _HEADING.match(lines[idx])
        if match is not None and len(match.group("hashes")) <= 2:
            return start, idx
    return start, len(lines)


def toc_links(text: str) -> list[tuple[str, str, int]]:
    """(link text, fragment target, line number) for each in-document ToC link.

    A link inside an HTML comment is author guidance, not navigation, so it is excluded.
    Comments in these blocks span many lines, so they are blanked across the whole section
    rather than per line: stripping line-by-line would leak every example link in a
    multi-line guidance comment into the results and check it as if it were real.
    """
    bounds = toc_bounds(text)
    if bounds is None:
        return []
    start, end = bounds
    lines = text.splitlines()
    section = "\n".join(lines[start:end])
    # Preserve line structure so reported line numbers stay accurate.
    masked = _HTML_COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), section)
    found: list[tuple[str, str, int]] = []
    for offset, raw in enumerate(masked.splitlines()):
        for match in _MD_LINK.finditer(raw):
            href = match.group("href").strip()
            if href.startswith("#"):
                found.append((match.group("text"), href[1:], start + offset + 1))
    return found


@dataclass
class Result:
    path: Path
    has_toc: bool
    link_count: int
    broken: list[tuple[str, str, int]]
    exempt: bool = False
    uncovered: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.broken and not self.uncovered


def decision_coverage_gaps(text: str) -> list[str]:
    """Decisions a reader cannot reach, in both directions the ToC check cannot see.

    The link check above asks only "does every ToC link resolve". That is one direction,
    and a PREFIX ToC satisfies it perfectly: measured 2026-10-07, the SSO design listed
    D-1..D-7, had detail sections through D-17 and an orphaned D-8 row, and the link
    check returned PASS on all 30 of its links. Nine decisions were unreachable from the
    document's own navigation and nothing said so (ISSUE-REF).

    So this asks the converse, which is where the failure actually lives:

    1. Every ``#### D-N`` detail heading has a ToC link pointing at it. A heading with no
       ToC row is a decision only a grep can find.
    2. Every ``| D-N |`` Decision Summary row has a ``#### D-N`` heading. A summary row
       with no detail section is a decision whose reasoning is missing, and the summary
       makes it look recorded.

    Deliberately keyed on the ``D-<n>`` identifier rather than on heading text, because
    the heading text carries the outcome and changes when a decision is amended or
    superseded, while the identifier is stable for the life of the document.
    """

    headings = {
        match.group(1): heading
        for heading in parse_headings(text)
        if heading.level == DECISION_HEADING_LEVEL
        and (match := _DECISION_ID.match(heading.text))
    }
    linked = {
        match.group(1)
        for label, _target, _line in toc_links(text)
        if (match := _DECISION_ID.match(label))
    }
    rows = {m.group("id") for m in _DECISION_SUMMARY_ROW.finditer(text)}

    gaps = []
    for decision_id in sorted(headings.keys() - linked, key=_decision_sort_key):
        gaps.append(
            f"D-{decision_id} has a detail section "
            f"(line {headings[decision_id].line}) but no Table of Contents row"
        )
    for decision_id in sorted(rows - headings.keys(), key=_decision_sort_key):
        gaps.append(
            f"D-{decision_id} has a Decision Summary row but no `#### D-{decision_id}` "
            "detail heading; add one, or a heading that points at where its detail lives"
        )
    return gaps


def _decision_sort_key(decision_id: str) -> tuple[int, str]:
    return (int(decision_id) if decision_id.isdigit() else 1 << 30, decision_id)


def check_file(
    path: Path,
    *,
    require_toc: bool = False,
    min_lines: int = 0,
    require_decision_coverage: bool = False,
) -> Result:
    text = path.read_text(encoding="utf-8")
    targets = {h.slug for h in parse_headings(text)} | explicit_anchors(text)
    links = toc_links(text)
    has_toc = toc_bounds(text) is not None
    broken = [
        (label, target, line) for label, target, line in links if target not in targets
    ]
    exempt = text.count("\n") < min_lines
    if require_toc and not has_toc and not exempt:
        broken.append(("<no table of contents>", "", 0))
    uncovered = decision_coverage_gaps(text) if require_decision_coverage else []
    return Result(
        path=path,
        has_toc=has_toc,
        link_count=len(links),
        broken=broken,
        exempt=exempt,
        uncovered=uncovered,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument(
        "--require-toc",
        action="store_true",
        help="fail a file that has no Table of Contents section",
    )
    parser.add_argument(
        "--min-lines",
        type=int,
        default=0,
        help="with --require-toc, exempt files shorter than this many lines",
    )
    parser.add_argument(
        "--require-decision-coverage",
        action="store_true",
        help=(
            "fail a file whose D-N decisions are unreachable: a detail section with no "
            "ToC row, or a Decision Summary row with no detail heading"
        ),
    )
    args = parser.parse_args(argv)

    failures = 0
    for path in args.paths:
        result = check_file(
            path,
            require_toc=args.require_toc,
            min_lines=args.min_lines,
            require_decision_coverage=args.require_decision_coverage,
        )
        if result.ok:
            state = (
                "ok" if result.has_toc else ("exempt" if result.exempt else "no-toc")
            )
            print(f"PASS {path} ({result.link_count} links, {state})")
            continue
        failures += 1
        print(f"FAIL {path} ({result.link_count} links)")
        for label, target, line in result.broken:
            if target:
                print(
                    f"     line {line}: [{label}](#{target}) -> no such heading or anchor"
                )
            else:
                print(f"     {label}")
        for gap in result.uncovered:
            print(f"     {gap}")
    print(f"--- {len(args.paths) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
