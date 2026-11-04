"""Is the composer free? Read-only, attribute-aware, two stable samples, fail closed.

The analyser is copied from ``context_supervisor`` (bff30e522) with the
``/compact`` literal generalised to whatever text the attempt staged. One
behaviour is ADDED: an untitled frame whose cursor row wraps onto further rows
before the closing rule is read as one multi-row composer, and staged text is
then matched ignoring whitespace (the wrap points), because a typed ``/goal``
of up to ``max_typed_chars`` wraps in a normal-width pane and the legacy parser,
which only ever saw ``/compact``, would refuse every such staging.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from ai_chassis.prompt_injection import _tmux
from ai_chassis.prompt_injection.targets import SessionTarget

# Copied from context_supervisor (329-365, 413-415).
_SGR_SEQUENCE = re.compile(r"\x1b\[[0-9;:]*m")
_COMMAND_HIGHLIGHT_SGR = "\x1b[38;5;153m"
_SUGGESTION_SGR_SEQUENCES: frozenset[str] = frozenset(
    {"\x1b[2m", _COMMAND_HIGHLIGHT_SGR}
)
SAFE_STAGED_ATTRIBUTE_CLASSES = frozenset({"normal", "suggestion"})
_READINESS_FALLBACK_REGION_LINES = 20
_MAX_WRAPPED_COMPOSER_ROWS = 40
_CLAUDE_CODE_NOTICE_PATTERNS = (
    (
        "update-installed-restart-required",
        re.compile(
            r"update installed\s*(?:[·.\-–—]\s*)?restart to update", re.IGNORECASE
        ),
    ),
    (
        "auto-update-failed",
        re.compile(
            r"auto-update failed\s*(?:[·.\-–—]\s*)?run claude doctor", re.IGNORECASE
        ),
    ),
)
_COMPOSER_TOP_BORDER = re.compile(r"^\s*─+(?:\s+\S(?:.*\S)?\s+─+)?\s*$")
_DEFAULT_COMPOSER_CURSOR_PATTERN = r"❯"
STAGE_VERIFY_TIMEOUT_SECONDS = 0.5
STAGE_VERIFY_POLL_SECONDS = 0.05
# A view that follows a keystroke the library sent itself (a cursor move, the
# stage undo). Measured on 2.1.296 (ISSUE-REF): 646 DEL keys took over 1 s to render.
KEYED_VIEW_TIMEOUT_SECONDS = 3.0
# TmuxAdapter.ready's two-sample rule (5636-5671).
_STABLE_EMPTY_SAMPLES = 2


@dataclass(frozen=True)
class ComposerSample:
    # Copied from context_supervisor.ComposerSample (1815-1826).
    classification: str
    region: str
    composer_recognized: bool
    content: str | None = None
    error_reason: str | None = None
    claude_code_notice: str | None = None
    attribute_class: str = "unknown"
    raw_content_debug: str | None = None
    #: Stage-verify only: the draft's styling is one the verify accepts.
    staged_style_safe: bool = False
    #: Stage-verify only: the raw (``-e``) capture this sample was read from.
    capture: str | None = field(default=None, repr=False, compare=False)
    #: Rows from the composer's top rule to the pane bottom: the most it can show
    #: without scrolling what sits above it (ISSUE-REF D-15).
    visible_rows: int | None = None


def _cursor_pattern() -> re.Pattern[str]:
    return re.compile(
        os.environ.get("AIH_COMPOSER_CURSOR_PATTERN", _DEFAULT_COMPOSER_CURSOR_PATTERN)
    )


def _is_box_drawing_horizontal_rule(line: str) -> bool:
    rule = line.strip()
    return bool(rule) and all(
        unicodedata.name(character, "").startswith("BOX DRAWINGS")
        and "HORIZONTAL" in unicodedata.name(character, "")
        for character in rule
    )


def is_composer_top_border(line: str) -> bool:
    # Copied from context_supervisor._is_composer_top_border (417-421).
    return bool(
        _COMPOSER_TOP_BORDER.fullmatch(line)
    ) or _is_box_drawing_horizontal_rule(line)


def strip_sgr(value: str) -> str:
    return _SGR_SEQUENCE.sub("", value)


def _cell_styles(raw_content: str, content_start: int) -> set[str]:
    """The styles of the visible non-space cells: ``normal``, a suggestion SGR
    sequence itself, or ``unknown``."""
    styled: list[tuple[str, str]] = []
    style = "normal"
    offset = 0
    for match in _SGR_SEQUENCE.finditer(raw_content):
        for character in raw_content[offset : match.start()]:
            styled.append((character, style))
        sequence = match.group(0)
        parameters = sequence[2:-1]
        if not parameters or all(
            part in {"", "0", "39", "49"} for part in parameters.split(";")
        ):
            style = "normal"
        elif sequence in _SUGGESTION_SGR_SEQUENCES:
            style = sequence
        else:
            style = "unknown"
        offset = match.end()
    for character in raw_content[offset:]:
        styled.append((character, style))
    return {
        cell_style
        for character, cell_style in styled[content_start:]
        if not character.isspace()
    }


def staged_style_safe(
    attribute_class: str, raw_content: str, content_start: int
) -> bool:
    """Whether a staged draft's styling is one the stage verify accepts: a safe
    class, or normal text with slash commands in Claude Code's command colour,
    which it draws wherever a command stands in a draft (ISSUE-REF, recorded raw
    captures). Any other mix, a dim ghost tail among them, is not."""
    return attribute_class in SAFE_STAGED_ATTRIBUTE_CLASSES or _cell_styles(
        raw_content, content_start
    ) == {"normal", _COMMAND_HIGHLIGHT_SGR}


def composer_attribute_class(raw_content: str, content_start: int) -> str:
    # Copied from context_supervisor._composer_attribute_class (5044-5074).
    classes = {
        "suggestion" if style in _SUGGESTION_SGR_SEQUENCES else style
        for style in _cell_styles(raw_content, content_start)
    }
    if not classes:
        return "normal"
    if len(classes) > 1:
        return "mixed"
    return classes.pop()


def matches_staged_text(composer_text: str | None, staged_text: str | None) -> bool:
    """Whether the composer holds exactly the payload staged (ISSUE-REF).

    Copied from context_supervisor._matches_staged_text (5077-5099): only
    trailing whitespace is forgiven on a one-row composer, and a blank
    expectation never matches. A multi-row (wrapped) composer is compared with
    all whitespace removed, since wrap points are the terminal's, not the text's.
    ADDED for ``multiline`` text: the composer must show at least one row per
    staged line, since the whitespace-free comparison alone cannot tell a soft
    newline that never landed from a wrap.
    """
    if not isinstance(staged_text, str) or not staged_text.strip():
        return False
    if not isinstance(composer_text, str):
        return False
    if "\n" in composer_text.strip():
        rows = composer_text.strip().split("\n")
        if len(rows) < len(staged_text.strip().split("\n")):
            return False
        return "".join(composer_text.split()) == "".join(staged_text.split())
    return composer_text.rstrip() == staged_text.rstrip()


def _squeezed(value: str) -> str:
    return "".join(value.split())


def draft_rows(content: str | None) -> list[str]:
    """The composer's visible draft rows, cursor glyph and wrap indent removed."""
    text = composer_text(content)
    if not text:
        return []
    return [row.strip() for row in text.strip().split("\n")]


def is_scrolled_window(content: str | None, staged_text: str, *, head: bool) -> bool:
    """Whether the visible draft is a proper head (``head``) or tail of a ONE-line
    staged text, the view Claude Code shows when the draft outgrows its row cap.

    Measured on 2.1.296 at 45x30 (ISSUE-REF): at most 10 draft rows show, scrolled
    to the cursor, with the cursor glyph on the first VISIBLE row, so a long line's
    head is off screen and no whole-text comparison can match."""
    if "\n" in staged_text.strip():
        return False
    visible = _squeezed("".join(draft_rows(content)))
    whole = _squeezed(staged_text)
    if not visible or len(visible) >= len(whole):
        return False
    return whole.startswith(visible) if head else whole.endswith(visible)


#: How many of the composer's last non-space characters the undo checks are its own.
UNDO_TAIL_WINDOW = 24


def undo_tail_holds(content: str | None, remaining: str) -> bool:
    """Whether the composer still ends in text this attempt typed and has not yet
    deleted, so the next DELs remove only that (ISSUE-REF ask 2).

    Typing lands at the cursor, which the undo keeps at the end, so a human's
    keystrokes show up as a tail that is not in ``remaining``, or as a draft
    longer than it. Whitespace is ignored on both sides (wrap points are the
    terminal's), so the check is blind to typed spaces alone."""
    visible = _squeezed("".join(draft_rows(content)))
    expected = _squeezed(remaining)
    if not visible or len(visible) > len(expected):
        return False
    return visible[-UNDO_TAIL_WINDOW:] in expected


def stitched_matches(
    head_content: str | None, tail_content: str | None, staged_text: str
) -> bool:
    """Whether a head view and a tail view of the same draft share at least one
    whole row and, joined on it, hold exactly the staged text. Without a shared
    row the middle was never seen, so the draft is not proved and this is False."""
    head = draft_rows(head_content)
    tail = draft_rows(tail_content)
    whole = _squeezed(staged_text)
    for overlap in range(min(len(head), len(tail)), 0, -1):
        if head[-overlap:] == tail[:overlap] and (
            _squeezed("".join(head[:-overlap] + tail)) == whole
        ):
            return True
    return False


@dataclass(frozen=True)
class WalkBand:
    """The composer's visible draft rows, positionally: ``rows[0]`` is the first window
    row (the one after the cursor glyph and its gap), the rest lose the two-cell
    continuation indent, and blank rows are kept, since a filler row is one.
    ``first_line`` is the pane line of ``rows[0]``; ``closed`` says a closing rule was
    drawn below the last row, so nothing of the draft is cut off at the pane bottom."""

    rows: tuple[str, ...]
    # Output above the composer can move it without changing the draft window.
    first_line: int = field(compare=False)
    closed: bool


def walk_band(capture: str | None) -> WalkBand | None:
    """The draft band of the lowest composer frame in ``capture``, or None.

    Unlike :func:`analyse_composer_capture` this never stops at a blank row: with the
    task panel showing, Claude Code 2.1.296 truncates the frame at the pane bottom,
    so the band runs to the closing rule or to the last pane line (ISSUE-REF)."""
    if not isinstance(capture, str):
        return None
    lines = [strip_sgr(line) for line in capture.splitlines()]
    pattern = _cursor_pattern()
    tops = [
        index
        for index in range(len(lines) - 1)
        if is_composer_top_border(lines[index]) and pattern.match(lines[index + 1])
    ]
    if not tops:
        return None
    first = tops[-1] + 1
    stop = next(
        (i for i in range(first + 1, len(lines)) if is_composer_top_border(lines[i])),
        None,
    )
    raw = lines[first : stop if stop is not None else len(lines)]
    rows = [(composer_text(raw[0]) or "").rstrip()]
    rows += [(row[2:] if row.startswith("  ") else row).rstrip() for row in raw[1:]]
    return WalkBand(tuple(rows), first, stop is not None)


def cursor_on_first_row(band: WalkBand | None, cursor: tuple[int, int] | None) -> bool:
    """Whether the cursor may be on the draft's first row, where ``Up`` recalls the
    previous history entry OVER the draft (measured on 2.1.296, ISSUE-REF). True
    when the cursor stands on the band's first line, and also when the band or the
    cursor cannot be read, so the guard fails closed. With the band scrolled the
    cursor sits mid-window (2.1.296 keeps it on window row ``W/2``), so the first
    line holds it only once the window shows row 0."""
    if band is None or cursor is None:
        return True
    return cursor[1] == band.first_line


def shows_row_zero(band: WalkBand, text: str) -> bool:
    """Whether the band's first row is the draft's row 0, read from content: that row,
    and the band as a whole, are each a non-empty prefix of ``text`` (squeezed).

    A repeated prefix can also be a window mid-draft or at the tail. Only a unique
    prefix proves row 0; otherwise keep climbing to the shared first-row cursor
    guard, which stops Up before it can recall history over the draft.
    """
    whole = _squeezed(text)
    first = _squeezed(band.rows[0]) if band.rows else ""
    visible = _squeezed("".join(band.rows))
    return (
        bool(first)
        and whole.startswith(first)
        and whole.startswith(visible)
        and whole.find(visible, 1) == -1
    )


def shows_tail(band: WalkBand, text: str) -> bool:
    """Whether the band ends in a blank row right below a row that ends ``text``: the
    last intended row is on screen with the filler beneath it."""
    rows = list(band.rows)
    if not rows or rows[-1].strip():
        return False
    while rows and not rows[-1].strip():
        rows.pop()
    return bool(rows) and _squeezed(text).endswith(_squeezed(rows[-1]))


def band_shift(previous: WalkBand, current: WalkBand) -> int | None:
    """How far the band moved between two captures one key press apart: 0 (it stayed;
    rows may have been added or removed below), +1 (one row down), -1 (one row up), or
    None when no single-row move explains the two (ISSUE-REF § The walk)."""
    before, after = previous.rows, current.rows
    for shift in (0, 1, -1):
        pairs = [
            (before[index + shift], row)
            for index, row in enumerate(after)
            if 0 <= index + shift < len(before)
        ]
        if pairs and all(left == right for left, right in pairs):
            return shift
    return None


def later_window(previous: WalkBand, current: WalkBand, text: str) -> bool:
    """Whether ``current`` shows a part of ``text`` that begins after the part
    ``previous`` shows (squeezed), so ``previous`` was a draft still being drawn
    (ISSUE-REF). The stitched verdict still checks every row the walk places."""
    whole = _squeezed(text)
    before = _squeezed("".join(previous.rows))
    after = _squeezed("".join(current.rows))
    start = whole.find(before) if before else -1
    return start >= 0 and bool(after) and whole.find(after) > start


def climb_shift(previous: WalkBand, current: WalkBand, reach: int) -> int | None:
    """How many rows up the band moved in one frame that landed several Ups at once
    (ISSUE-REF): the least ``-rows`` for ``rows`` in 1..``reach`` that the two bands'
    overlap explains, or None. The stitched verdict still checks every row placed."""
    before, after = previous.rows, current.rows
    for rows in range(1, reach + 1):
        pairs = [
            (before[index - rows], row)
            for index, row in enumerate(after)
            if 0 <= index - rows < len(before)
        ]
        if pairs and all(left == right for left, right in pairs):
            return -rows
    return None


@dataclass(frozen=True)
class WalkVerdict:
    """The stitched draft against the intended text. ``rows`` is the number of rows
    holding text (trailing blank rows are the filler, not counted). On a mismatch,
    ``offset`` is the first divergent character of the intended text (its index in
    ``text``), ``row`` the stitched row holding it, ``row_text`` that row and
    ``expected`` the intended text the same row should have shown, squeezed."""

    matched: bool
    rows: int
    offset: int | None = None
    row: int | None = None
    row_text: str | None = None
    expected: str | None = None


def stitched_verdict(stitched: list[str], text: str) -> WalkVerdict:
    """Whether the positional rows ``stitched`` (row 0 first) hold exactly ``text``
    followed by at least one blank filler row. Compared with all whitespace removed,
    as the screen cannot show a wrap point from a soft newline; there must also be a
    row for each of the text's lines."""
    content = list(stitched)
    while content and not content[-1].strip():
        content.pop()
    filler = len(stitched) - len(content)
    seen = _squeezed("".join(content))
    whole = _squeezed(text)
    lines = len(text.strip().split("\n"))
    if seen == whole and filler >= 1 and len(content) >= lines:
        return WalkVerdict(True, len(content))
    offset = next(
        (i for i, (a, b) in enumerate(zip(seen, whole, strict=False)) if a != b),
        min(len(seen), len(whole)),
    )
    row, start = 0, 0
    for index, value in enumerate(content):
        width = len(_squeezed(value))
        if start + width > offset or index == len(content) - 1:
            row = index
            break
        start += width
    positions = [i for i, character in enumerate(text) if not character.isspace()]
    return WalkVerdict(
        False,
        len(content),
        offset=positions[offset] if offset < len(positions) else len(text),
        row=row,
        row_text=content[row] if content else "",
        expected=whole[start : start + max(1, len(_squeezed(content[row])))]
        if content
        else whole[:1],
    )


def composer_text(content: str | None) -> str | None:
    # Copied from context_supervisor._composer_text (5102-5116).
    if not isinstance(content, str):
        return None
    pattern = _cursor_pattern()
    cursor_end = next(
        (end for end in range(1, len(content) + 1) if pattern.fullmatch(content[:end])),
        None,
    )
    if cursor_end is None:
        return content
    value = content[cursor_end:]
    return value[1:] if value.startswith((" ", "\N{NO-BREAK SPACE}")) else value


def _bounded_capture_region(lines: list[str], border_indexes: list[int]) -> str:
    start = (
        min(border_indexes)
        if border_indexes
        else max(0, len(lines) - _READINESS_FALLBACK_REGION_LINES)
    )
    return "\n".join(lines[start:])


def _next_border(lines: list[str], start: int) -> int | None:
    for following in range(start, min(len(lines), start + _MAX_WRAPPED_COMPOSER_ROWS)):
        if is_composer_top_border(lines[following]):
            return following
    return None


def _draft_end(lines: list[str], start: int) -> int:
    """Where a frame with no closing rule ends: the first blank row or the pane
    bottom, at most ``_MAX_WRAPPED_COMPOSER_ROWS`` rows on (ISSUE-REF). A blank
    first row ends it at once, so its blank content still refuses (ISSUE-REF)."""
    stop = start
    limit = min(len(lines), start + _MAX_WRAPPED_COMPOSER_ROWS)
    while stop < limit and lines[stop].strip():
        stop += 1
    return stop


def _unbounded_frame_end(lines: list[str], start: int) -> int:
    """``_draft_end``, except that a read holding only the cursor goes on to the pane
    bottom (ISSUE-REF): a human draft whose first line is empty shows the cursor row,
    a blank row, then text, and stopping at the blank row certified it empty. Any
    non-blank row below therefore belongs to the frame and reads as a draft."""
    stop = _draft_end(lines, start)
    read = "\n".join(lines[start:stop])
    if not read.strip() or (composer_text(read) or "").strip():
        return stop
    below = [row for row in range(stop, len(lines)) if lines[row].strip()]
    return below[-1] + 1 if below else stop


_Candidate = tuple[int, str, str, bool]  # (top border, content, raw content, bounded)


def _candidate_at(
    index: int, lines: list[str], raw_lines: list[str], pattern: re.Pattern[str]
) -> _Candidate | None:
    """The composer frame whose top rule is ``lines[index]``, if it is one."""
    line = lines[index]
    if bool(_COMPOSER_TOP_BORDER.fullmatch(line) and " " in line.strip()):  # titled
        bottom = next(
            (
                f
                for f in range(index + 1, len(lines))
                if is_composer_top_border(lines[f])
            ),
            None,
        )
        stop = bottom if bottom is not None else _unbounded_frame_end(lines, index + 1)
        return (
            index,
            "\n".join(lines[index + 1 : stop]),
            "\n".join(raw_lines[index + 1 : stop]),
            bottom is not None,
        )
    if index + 1 >= len(lines):
        return None
    cursor_line = lines[index + 1].strip()
    blank_before_rule = (
        not cursor_line
        and index + 2 < len(lines)
        and is_composer_top_border(lines[index + 2])
    )
    if pattern.fullmatch(cursor_line) or blank_before_rule:
        return (index, "", "", True)
    if not pattern.match(cursor_line):
        return None
    bottom = _next_border(lines, index + 2)
    if bottom is None:
        return None
    return (
        index,
        "\n".join(lines[index + 1 : bottom]),
        "\n".join(raw_lines[index + 1 : bottom]),
        True,
    )


def analyse_composer_capture(
    capture: str, *, phase: str, staged_text: str | None = None
) -> ComposerSample:
    """Classify the viewport. Copied from context_supervisor._analyse_composer_capture
    (5135-5313); see its comments for the incident behind each branch."""
    pattern = _cursor_pattern()
    claude_code_notice = next(
        (
            notice
            for notice, notice_pattern in _CLAUDE_CODE_NOTICE_PATTERNS
            if notice_pattern.search(capture)
        ),
        None,
    )
    raw_lines = capture.splitlines()
    lines = [strip_sgr(line) for line in raw_lines]
    candidates = [
        candidate
        for index, line in enumerate(lines)
        if is_composer_top_border(line)
        and (candidate := _candidate_at(index, lines, raw_lines, pattern)) is not None
    ]
    region = _bounded_capture_region(lines, [c[0] for c in candidates])
    if not candidates:
        return ComposerSample(
            "not-obviously-occupied"
            if phase == "precheck"
            else "stage-verify-mismatch",
            region,
            False,
            claude_code_notice=claude_code_notice,
        )
    top, content, raw_content, bounded = max(candidates, key=lambda c: c[0])
    visible_rows = len(lines) - top - 1
    if not bounded and not content.strip():
        # ISSUE-REF: an unbounded frame's blank content is not evidence of an
        # empty composer.
        return ComposerSample(
            "not-obviously-occupied"
            if phase == "precheck"
            else "stage-verify-mismatch",
            region,
            False,
            claude_code_notice=claude_code_notice,
        )
    exact_content = composer_text(content) or ""
    content_start = len(content) - len(exact_content)
    attribute_class = composer_attribute_class(raw_content, content_start)
    if phase == "stage-verify":
        style_safe = staged_style_safe(attribute_class, raw_content, content_start)
        return ComposerSample(
            (
                "stage-verified"
                if matches_staged_text(exact_content, staged_text) and style_safe
                else "stage-verify-mismatch"
            ),
            region,
            True,
            content,
            claude_code_notice=claude_code_notice,
            attribute_class=attribute_class,
            staged_style_safe=style_safe,
            visible_rows=visible_rows,
        )
    empty = not content.strip() or bool(pattern.fullmatch(content.strip()))
    return ComposerSample(
        (
            "empty-composer"
            if empty
            else "composer-suggestion"
            if attribute_class == "suggestion"
            else "composer-nonempty"
        ),
        region,
        True,
        content,
        claude_code_notice=claude_code_notice,
        attribute_class=attribute_class,
        raw_content_debug=(
            raw_content
            if not empty and attribute_class in {"unknown", "mixed"}
            else None
        ),
        visible_rows=visible_rows,
    )


@dataclass(frozen=True)
class ReadinessVerdict:
    ready: bool
    reason: str | None
    samples: list[ComposerSample] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)


_TIMEOUT_REASONS = {
    "composer-suggestion": "composer-suggestion",
    "composer-nonempty": "composer-nonempty",
}


class ReadinessProbe(ABC):
    @abstractmethod
    def sample(
        self,
        target: SessionTarget,
        *,
        phase: str = "precheck",
        staged_text: str | None = None,
    ) -> ComposerSample: ...

    @abstractmethod
    def wait_ready(
        self, target: SessionTarget, budget: float, poll: float
    ) -> ReadinessVerdict: ...

    def pane_width(self, _target: SessionTarget) -> int | None:
        """The pane's width in columns; None when it cannot be read."""
        return None

    def cursor(self, _target: SessionTarget) -> tuple[int, int] | None:
        """The pane cursor's ``(x, y)``; None when it cannot be read."""
        return None


class ComposerReadiness(ReadinessProbe):
    """``TmuxAdapter.ready`` / ``compact_sample`` (5636-5719) as one probe."""

    def __init__(self, tmux_command: _tmux.Command = _tmux.DEFAULT_COMMAND) -> None:
        self.tmux_command = tmux_command
        self.last_capture = ""

    def sample(self, target, *, phase="precheck", staged_text=None):
        """One classified capture. A stage-verify sample keeps its raw capture, the
        frame a refusal's evidence file records."""
        result = _tmux.run(
            self.tmux_command, ["capture-pane", "-p", "-e", "-t", target.pane_id]
        )
        sample = self._classify(result, phase, staged_text)
        if phase == "precheck" or result is None:
            return sample
        return replace(sample, capture=result.stdout)

    def _classify(
        self,
        result: subprocess.CompletedProcess[str] | None,
        phase: str,
        staged_text: str | None,
    ) -> ComposerSample:
        failed = "not-ready" if phase == "precheck" else "stage-verify-mismatch"
        if result is None:
            return ComposerSample(failed, "", False, error_reason="capture")
        self.last_capture = result.stdout
        if result.returncode != 0:
            return ComposerSample(
                failed,
                _bounded_capture_region(result.stdout.splitlines(), []),
                False,
                error_reason="tmux",
            )
        try:
            return analyse_composer_capture(
                result.stdout, phase=phase, staged_text=staged_text
            )
        except Exception as error:  # noqa: BLE001 - parser failures are fail-closed
            return ComposerSample(
                failed,
                _bounded_capture_region(result.stdout.splitlines(), []),
                False,
                error_reason=f"parser:{type(error).__name__}",
            )

    def pane_width(self, target: SessionTarget) -> int | None:
        value = _tmux.display(self.tmux_command, target.pane_id, "#{pane_width}")
        try:
            width = int((value or "").strip())
        except ValueError:
            return None
        return width if width > 0 else None

    def cursor(self, target: SessionTarget) -> tuple[int, int] | None:
        value = _tmux.display(
            self.tmux_command, target.pane_id, "#{cursor_x},#{cursor_y}"
        )
        x, _, y = (value or "").partition(",")
        try:
            return int(x), int(y)
        except ValueError:
            return None

    def pane_in_mode(self, target: SessionTarget) -> bool:
        """True only when tmux SAYS the pane is in a mode; a pane that no longer
        answers is not in one, and its samples refuse as not-ready on their own."""
        value = _tmux.display(self.tmux_command, target.pane_id, "#{pane_in_mode}")
        return value is not None and value.strip() not in {"", "0"}

    def wait_ready(self, target, budget, poll):
        """Two consecutive ``empty-composer`` samples inside ``budget``, else the last
        sample's classification as the reason. Never writes to the pane; a pane in
        copy or view mode refuses at once rather than being cancelled."""
        if self.pane_in_mode(target):
            return ReadinessVerdict(False, "pane-in-mode")
        samples: list[ComposerSample] = []
        deadline = time.monotonic() + max(0.0, budget)
        consecutive = 0
        while True:
            sample = self.sample(target)
            samples.append(sample)
            if sample.composer_recognized and sample.classification == "empty-composer":
                consecutive += 1
                if consecutive >= _STABLE_EMPTY_SAMPLES:
                    return ReadinessVerdict(
                        True, None, samples, ["precheck"] * len(samples)
                    )
            else:
                consecutive = 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = _TIMEOUT_REASONS.get(sample.classification, "not-ready")
                return ReadinessVerdict(
                    False, reason, samples, ["precheck"] * len(samples)
                )
            if self.pane_in_mode(target):
                return ReadinessVerdict(
                    False, "pane-in-mode", samples, ["precheck"] * len(samples)
                )
            time.sleep(min(poll, remaining))

    def verify_staged(self, target: SessionTarget, staged_text: str) -> ComposerSample:
        """Poll the staged frame up to 0.5 s for ``stage-verified`` (6572-6629)."""
        return self.poll(
            target,
            lambda sample: sample.classification == "stage-verified",
            STAGE_VERIFY_TIMEOUT_SECONDS,
            phase="stage-verify",
            staged_text=staged_text,
        )

    def poll(
        self,
        target: SessionTarget,
        accept: Callable[[ComposerSample], bool],
        timeout: float,
        *,
        phase: str,
        staged_text: str | None = None,
    ) -> ComposerSample:
        """Sample until ``accept`` holds or ``timeout`` passes; the last sample."""
        deadline = time.monotonic() + timeout
        while True:
            sample = self.sample(target, phase=phase, staged_text=staged_text)
            if accept(sample):
                return sample
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return sample
            time.sleep(min(STAGE_VERIFY_POLL_SECONDS, remaining))
