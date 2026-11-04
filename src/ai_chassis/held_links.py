"""Clickable references in held bodies (ISSUE-REF).

Every file or doc a held body names is a Markdown link with display text, and every URL is
``[display text](URL)``; never a bare path or URL (``docs/held/README.md`` § Links). This module
finds the bare ones ``held_cli.py add`` refuses and builds the links ``held_cli.py link``,
``add --plan`` and ``compile`` print.

What counts as a reference (plan ``docs/plans/aih-held-docs-clickable-backlinks-plan.md`` D-4):
outside fenced code, HTML comments, Markdown links and images, reference definitions and inline
code spans that contain whitespace (a command), a whitespace-split token with its surrounding
punctuation stripped is a bare URL when it carries ``http://`` or ``https://``, and a bare path
when it is an absolute or ``~/`` path of two or more segments, or a relative path whose last
segment has a letter-led extension. A token carrying a placeholder or shell character, or an
ellipsis, is skipped: the non-path shapes ``scripts/evidence_citation_gate.py`` measured.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

#: The two sentences every held surface carries byte for byte (plan D-5).
CANONICAL_FIRST = (
    "Canonical first: read the ai-harness version of the held README, of each kind's STUB.md "
    "and TEMPLATE.md and of every other held doc (/workspace/projects and "
    "/workspace/projects before any repository-specific copy, and use a "
    "repository-specific version only for what it adds or changes."
)
LINK_RULE = (
    "Links: write every file or doc a held body references as a Markdown link with display "
    "text, [📄 <repo>/<path>](<path relative to the item's directory>), and every URL (a Jira "
    "issue, a Confluence page, a pull request, anything else) as [<display text>](<URL>), never "
    "a bare path or URL; held_cli.py link prints the file form and held_cli.py add refuses a "
    "bare one (docs/held/README.md § Links)."
)

BARE_PATH = "HL-01"
BARE_URL = "HL-02"
FIX_PATH = (
    "write it as [📄 <repo>/<path>](<path relative to the item's directory>); "
    "`held_cli.py link --session <slug>@<host> <absolute path>` prints that link"
)
FIX_URL = "write it as [<display text>](<the URL>), e.g. [KEY-123](https://<site>/browse/KEY-123)"

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_LINK = re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)")
_REF_DEFINITION = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s+\S")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_URL = re.compile(r"https?://", re.IGNORECASE)
_LEAD = "([{\"'*_"
_TRAIL = ")]}\"'.,;:!?*_"
_PLACEHOLDER = set("<>{}*$|=")
_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,9}$")
_LINE_SUFFIX = re.compile(r":\d+(?::\d+)?$")
_ABSOLUTE = re.compile(r"^(?:~/[^/]+|/[^/]+/[^/]+)")
_LEADING_LINK = re.compile(r"^\[([^\]\n]+)\]\(([^)\s]+)\)")


@dataclass(frozen=True)
class Finding:
    """One bare reference: the rule it breaks, its 1-based line and the token as written."""

    rule: str
    line: int
    token: str

    @property
    def fix(self) -> str:
        return FIX_URL if self.rule == BARE_URL else FIX_PATH

    def render(self, label: str) -> str:
        what = "bare URL" if self.rule == BARE_URL else "bare path"
        return f"{label}:{self.line}: {self.rule} {what} {self.token}. Fix: {self.fix}"


def _visible_lines(text: str) -> list[tuple[int, str]]:
    """``(line number, text)`` with fenced blocks dropped and HTML comments blanked."""
    found: list[tuple[int, str]] = []
    fence = ""
    in_comment = False
    for number, line in enumerate(text.splitlines(), start=1):
        if not in_comment:
            match = _FENCE.match(line)
            if fence:
                if (
                    match
                    and match.group(1)[0] == fence[0]
                    and len(match.group(1)) >= len(fence)
                ):
                    fence = ""
                continue
            if match:
                fence = match.group(1)
                continue
        kept: list[str] = []
        rest = line
        while rest:
            if in_comment:
                end = rest.find("-->")
                if end < 0:
                    rest = ""
                    break
                rest = rest[end + 3 :]
                in_comment = False
                continue
            start = rest.find("<!--")
            if start < 0:
                kept.append(rest)
                break
            kept.append(rest[:start])
            rest = rest[start + 4 :]
            in_comment = True
        found.append((number, " ".join(kept)))
    return found


def _strip_spans(line: str) -> str:
    if _REF_DEFINITION.match(line):
        return ""
    line = _LINK.sub(" ", line)
    return _CODE_SPAN.sub(
        lambda m: " " if re.search(r"\s", m.group(1)) else f" {m.group(1)} ", line
    )


def _is_path(token: str) -> bool:
    if "..." in token or "…" in token or any(c in _PLACEHOLDER for c in token):
        return False
    token = _LINE_SUFFIX.sub("", token.split("#", 1)[0])
    if not token or token.endswith("//"):
        return False
    if token[0] in "/~":
        return bool(_ABSOLUTE.match(token))
    if "/" not in token or token.endswith("/"):
        return False
    return bool(_EXTENSION.search(token.rsplit("/", 1)[-1]))


def bare_references(text: str) -> list[Finding]:
    """Every bare path and bare URL in ``text``, in reading order (plan D-4)."""
    findings: list[Finding] = []
    for number, line in _visible_lines(text):
        for raw in _strip_spans(line).split():
            token = raw.lstrip(_LEAD).rstrip(_TRAIL)
            if not token:
                continue
            if _URL.search(token):
                findings.append(Finding(BARE_URL, number, token.strip("<>")))
            elif _is_path(token):
                findings.append(Finding(BARE_PATH, number, token))
    return findings


def split_reference(value: str) -> tuple[str, str]:
    """``(path, rest)`` of a path-line value: a leading link's display text, else its first token.

    Reads both the link form ``[<abs path>](<relative>) rest`` and the bare form an item held
    before the link rule carries, so every held item still parses.
    """
    value = value.strip()
    match = _LEADING_LINK.match(value)
    if match:
        shown = match.group(1).strip()
        return shown.split()[0].strip("`") if shown.split() else "", value[
            match.end() :
        ]
    head, _, rest = value.partition(" ")
    return head.strip("`"), (f" {rest}" if rest else "")


def leading_target(value: str) -> str | None:
    """The target of the link a path-line value starts with, else None."""
    match = _LEADING_LINK.match(value.strip())
    return match.group(2) if match else None


def relative_target(target: str | Path, base: Path) -> str:
    """``target`` (anchor kept) as a POSIX path relative to the directory ``base``."""
    path, hash_, anchor = str(target).partition("#")
    rel = Path(os.path.relpath(os.path.expanduser(path), base)).as_posix()
    return f"{rel}{hash_}{anchor}"


_LINK_TARGET = re.compile(r"(!?\[[^\]\n]*\]\()([^)\s]+)((?:\s+\"[^\"\n]*\")?\))")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _relative_target(target: str) -> bool:
    """A link target written relative to the file it sits in: no anchor-only, URL or absolute."""
    return not (
        target.startswith(("#", "/", "~", "<")) or _SCHEME.match(target) is not None
    )


def rebase_links(text: str, source: Path, dest: Path) -> tuple[str, list[str]]:
    """``text`` with each relative link target re-pointed from ``source`` to ``dest``.

    Both are directories: the one the text was written in and the one it is copied into. A
    rebased link opens the same file it did; an anchor-only target, a URL, an absolute path and
    every link inside fenced code or an inline code span are left as written. Returns the new
    text and, in order, the targets (as written) that name no file from ``source``.
    """
    missing: list[str] = []

    def rebase(match: re.Match[str]) -> str:
        target = match.group(2)
        if not _relative_target(target):
            return match.group(0)
        path, hash_, anchor = target.partition("#")
        if not path:
            return match.group(0)
        # Lexical, as a Markdown viewer resolves it: the item's own directory may not exist
        # in this tree yet (an unshipped item), and the kernel will not walk `..` out of it.
        resolved = os.path.normpath(source / path)
        if not os.path.exists(resolved) and target not in missing:
            missing.append(target)
        moved = os.path.relpath(resolved, dest)
        return (
            f"{match.group(1)}{Path(moved).as_posix()}{hash_}{anchor}{match.group(3)}"
        )

    out: list[str] = []
    fence = ""
    for line in text.splitlines(keepends=True):
        match = _FENCE.match(line)
        if fence:
            if (
                match
                and match.group(1)[0] == fence[0]
                and len(match.group(1)) >= len(fence)
            ):
                fence = ""
            out.append(line)
            continue
        if match:
            fence = match.group(1)
            out.append(line)
            continue
        parts = re.split(r"(`[^`\n]+`)", line)
        out.append(
            "".join(
                p if p.startswith("`") else _LINK_TARGET.sub(rebase, p) for p in parts
            )
        )
    return "".join(out), missing


def path_link(target: str | Path, base: Path, display: str | None = None) -> str:
    """``[display](relative)``; the display text defaults to ``target`` itself."""
    return f"[{display or target}]({relative_target(target, base)})"
