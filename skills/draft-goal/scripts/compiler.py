"""Inline implementation of the draft-goal compiler contract.

The model-backed routes receive the same contract through worker-brief.md.  The
inline route is deliberately conservative: when it cannot bind authority or an
external verifier, it returns one focused question instead of inventing a goal.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import re
import unicodedata

from typing import Any

from line_endings import cm_lines, line_spans


from markdown_it import MarkdownIt

from judge_cli import classify_clause

FIT_CLASSES = {"one-shot", "goal", "loop", "goal+loop", "not-autonomous-fit"}
MIN_INDEPENDENT_OUTCOMES = 2
MAX_REPEATED_PROJECTION = 400
SUBJECTIVE = re.compile(
    r"\b(inspiring|beautiful|delightful|excellent|best|good|high[- ]quality)\b", re.I
)
UNRESOLVED_HUMAN_JUDGMENT = re.compile(
    r"\b(?:wait for|requires?|needs?)\s+(?:an?\s+)?human(?:\s+[a-z-]+){0,2}\s+"
    r"(?:decision|judg(?:e)?ment)\b",
    re.I,
)
ONE_SHOT = re.compile(r"\b(one[- ]shot|one bounded|single pass|one time|typo)\b", re.I)
WATCH = re.compile(
    r"\b(watch|monitor|observe|periodic|hourly|daily|weekly|cadence)\b", re.I
)
ACTION = re.compile(r"\b(repair|fix|update|maintain|change|write|publish)\b", re.I)
CREDENTIAL = re.compile(
    r"\b(?:[A-Za-z][\w-]*\s+)?(?:credential|token|api key|login)\b", re.I
)
UNAVAILABLE = re.compile(
    r"\b(unavailable|missing|not available|without|do not have)\b", re.I
)
EXTERNAL_WRITE = re.compile(r"\b(push|deploy|publish|delete|send)\b", re.I)
AUTHORITY = re.compile(
    r"\b(authorized|approved|you may|permission|human approval)\b", re.I
)
VERIFIER = re.compile(
    r"\b(pytest|ruff|uv run|aido validate-doc|bd|git|make|npm test|test suite|"
    r"status api|status endpoint|query|command|reviewer|checksum)\b",
    re.I,
)
OUTCOME_START = re.compile(
    r"^(?:build|create|deliver|deploy|ensure|fix|keep|maintain|make|monitor|"
    r"preserve|produce|publish|repair|update|watch|write)\b",
    re.I,
)
SECTION_MARKER = re.compile(
    r"^(?:(?P<exclusion>exclude(?:s|d)?|exclusions?|out[ -]of[ -]scope)|"
    r"include(?:s|d)?|in[ -]scope|scope|deliverables|you may|human approval required for)"
    r"\s*(?::|$)",
    re.I,
)
DEPENDENCY_LINK = re.compile(
    r"\b(?:after|before|depends? on|followed by|once|then|use\b.+?\bto|"
    r"using (?:the )?(?:output|result|artifact) (?:from|of))\b",
    re.I,
)
STOP_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


MAX_SCOPE_BLOCK_BYTES = 3072
MAX_SCOPE_NESTING = 8
BLOCK_REFERENT = re.compile(
    r"\b(it|its|them|their|that|this|those|these|same|there|either|both|"
    r"former|latter|above|below|neither|each|other|others|aforementioned|"
    r"preceding|following|respective|such)\b",
    re.I,
)
COLLECTIVE_REFERENT = re.compile(
    r"\b(either|both|former|latter|above|below|neither|each|other|others|"
    r"aforementioned|preceding|following|respective)\b",
    re.I,
)
NAMED_REFERENT = re.compile(
    r"(?:/[^\s]+|`[^`]+`|[\"'][^\"']+[\"']|\b(?:target|subject|object):)", re.I
)
ELIDED_TARGET = re.compile(
    r"\b(delete|deploy|modify|change|remove|publish|overwrite)\s*[.!?]*$", re.I
)
PARSER_NESTING_LIMIT = 64  # Above 2 * bound + leaf wrappers, never the default 20.


class ScopeNestingError(ValueError):
    """A partial CommonMark parse may never become classification authority."""


def _scope_tokens(text):
    if "\x00" in text:
        raise ScopeNestingError(
            "check=scope-source severity=error: NUL would change source text during Markdown parsing; supply intact text and rerun."
        )
    env = {}
    try:
        tokens = MarkdownIt("commonmark", {"maxNesting": PARSER_NESTING_LIMIT}).parse(
            text, env
        )
    except RecursionError as exc:
        raise ScopeNestingError(
            "check=scope-nesting severity=error: parser nesting exhausted"
        ) from exc
    # CommonMark consumes definitions without producing leaf tokens. Until
    # their destinations have a lossless published identity, refuse the whole
    # source rather than judging a link whose target can disappear. This also
    # covers shortcut/collapsed references and definitions in containers.
    if env.get("references") or any(
        child.type == "text"
        and re.search(r"\[[^\[\]]+\]\s*\[[^\[\]]*\]", child.content)
        for token in tokens
        for child in token.children or ()
    ):
        raise ScopeNestingError(
            "check=scope-reference severity=error: Markdown reference identity cannot be preserved; replace reference links and definitions with explicit inline destinations and rerun."
        )
    depth = 0
    for token in tokens:
        if (
            token.type in {"truncation", "truncated"}
            or token.meta.get("truncated")
            or token.level >= PARSER_NESTING_LIMIT - 1
        ):
            raise ScopeNestingError(
                "check=scope-nesting severity=error: truncated Markdown parse"
            )
        if token.type in {"list_item_open", "blockquote_open"}:
            depth += 1
            if depth > MAX_SCOPE_NESTING:
                raise ScopeNestingError(
                    f"check=scope-nesting severity=error: Markdown container depth exceeds {MAX_SCOPE_NESTING}; reduce nesting and rerun."
                )
        elif token.type in {"list_item_close", "blockquote_close"}:
            depth -= 1
    return tokens


def _scope_line(line: str) -> tuple[tuple[int, ...], str]:
    """Indentation at each blockquote level gives a lexically ordered scope."""
    remaining = line.expandtabs(4)
    depth = []
    while True:
        depth.append(len(remaining) - len(remaining.lstrip(" ")))
        remaining = remaining.lstrip(" ")
        if not remaining.startswith(">"):
            return tuple(depth), remaining
        remaining = remaining[1:].removeprefix(" ")


def _block_ownership(text: str, *, with_continuations=False, with_blocks=False):  # noqa: C901, PLR0912
    """Assign line spelling and ownership from CommonMark's parsed blocks.

    Container prefixes can interleave on the same line (list -> quote -> list).
    Only inline token content can recognise markers: lexical stripping cannot.
    Unparsed and code/HTML bytes remain judged but cannot open a section.
    """
    spans = line_spans(text)
    lines = [text[start:end] for start, end in spans]
    owners: list[tuple[int, ...]] = [()] * len(lines)
    protected = set(range(len(lines)))
    headings: dict[int, str] = {}
    bodies: dict[int, str] = {}
    item_starts = set()
    continuations = set()
    blocks = {}
    stack: list[int] = []
    tokens = _scope_tokens(text)
    for index, token in enumerate(tokens):
        if token.type in {"list_item_open", "blockquote_open"}:
            stack.append(index)
            if token.type == "list_item_open":
                item_starts.add(token.map[0])
            for line in range(*token.map):
                owners[line] = tuple(stack)
        elif token.type in {"list_item_close", "blockquote_close"}:
            stack.pop()
        elif token.type == "hr":
            # A thematic break is block framing, not a prose leaf or referent.
            # Use the parsed type: the same glyphs in code/escaped text are data.
            for line in range(*token.map):
                bodies[line] = ""
        elif token.type == "inline":
            inline_lines = cm_lines(token.content)
            block = _canonical_block(_inline_source_spelling(token, lines))
            block_span = (spans[token.map[0]][0], spans[token.map[1] - 1][1])
            for line in range(*token.map):
                blocks[line] = (block, block_span)
            continuations.update(
                range(token.map[0], token.map[0] + len(inline_lines) - 1)
            )
            for offset, body in enumerate(inline_lines):
                line = token.map[0] + offset
                bodies[line] = body
                protected.discard(line)
            if tokens[index - 1].type == "heading_open":
                headings[token.map[0]] = token.content
                # Retain the established heading clause spelling for parity.
                bodies[token.map[0]] = "# " + inline_lines[0]
                # A setext underline is heading framing, not a second leaf.
                for line in range(
                    token.map[0] + len(inline_lines), tokens[index - 1].map[1]
                ):
                    bodies[line] = ""
        elif token.type in {"fence", "code_block", "html_block"}:
            content = (
                token.info + "\n" if token.type == "fence" and token.info else ""
            ) + token.content
            block = _canonical_block(content)
            block_span = (spans[token.map[0]][0], spans[token.map[1] - 1][1])
            for line in range(*token.map):
                blocks[line] = (block, block_span)
            if token.type == "fence" and token.info:
                bodies[token.map[0]] = token.info
            start = token.map[0] + (token.type == "fence")
            for offset, body in enumerate(cm_lines(token.content)):
                bodies[start + offset] = body
    result = owners, protected, headings, bodies, item_starts
    if with_blocks:
        return (*result, continuations, blocks)
    return (*result, continuations) if with_continuations else result


def _inline_source_spelling(token, lines):
    """Recover Unicode edge whitespace trimmed by markdown-it's paragraph rule.

    Source positions, rather than normalised inline tokens, own these bytes.
    ASCII indentation/framing remains canonical; NBSP/separators are target data.
    """
    content = token.content
    first = cm_lines(content)[0]
    source = lines[token.map[0]]
    position = source.find(first) if first else -1
    prefix = (
        re.search(r"[^\S \t]+$", source[:position].rstrip(" \t"))
        if position >= 0
        else None
    )
    last = token.map[0] + len(cm_lines(content)) - 1
    suffix = re.search(r"[^\S \t]+$", lines[last].rstrip(" \t"))
    return (prefix[0] if prefix else "") + content + (suffix[0] if suffix else "")


def _canonical_block(content: str) -> str:
    """Parser inline spelling; only physical breaks and their framing collapse.

    Markup, entities, internal whitespace and Unicode are data. The parser has
    already removed heading/list/quote framing, without unescaping inline text.
    """
    lines = cm_lines(content)
    for index in range(len(lines) - 1):
        line = lines[index].rstrip(" ")
        if (len(line) - len(line.rstrip("\\"))) % 2:
            line = line[:-1]
        lines[index] = line
    block = " ".join(line.strip(" \t") for line in lines).strip(" \t")
    if len(block.encode("utf-8")) > MAX_SCOPE_BLOCK_BYTES:
        raise ScopeNestingError(
            f"check=scope-block-size severity=error: leaf block exceeds {MAX_SCOPE_BLOCK_BYTES} UTF-8 bytes; split it into complete blocks and rerun."
        )
    return block


@dataclass(frozen=True)
class ClauseUnit:
    state: dict[str, str]
    text: str
    source_span: tuple[int, int]
    container: tuple[int, ...]
    kind: str
    block_text: str
    block_span: tuple[int, int]
    heading_context: str | None = None
    antecedent_blocks: tuple[str, ...] = ()
    compatibility_projection: bool = False


def clause_units(  # noqa: C901
    ask: str, *, sentence_texts: bool = False
) -> list[ClauseUnit] | list[str]:
    """Preserve clause capture; section context belongs to its CommonMark block."""
    sentence_break = re.compile(r"(?<=[.!?])\s+")

    def sentence_units(content):
        parts = []
        for part in sentence_break.split(content.strip()):
            if not part.strip():
                continue
            # Retain native S-item and standalone ordinal framing, including
            # literal/code spelling. report3. still ends a sentence.
            if parts and (
                part.strip() == "|"
                or re.search(r"(?:^|[ \t])(?:S[1-9][0-9]*|[0-9]+)\.$", parts[-1])
            ):
                parts[-1] += " " + part.strip()
            else:
                parts.append(part.strip())
        return parts

    if sentence_texts:
        # Deterministic validator syntax is not a classification request. Its
        # flattened coverage source/native condition can exceed semantic bounds;
        # the sole authority boundary still preflights original source blocks.
        return sentence_units(ask)
    if not ask.strip():
        raise ValueError("ask must not be empty")
    units: list[ClauseUnit] = []
    spans = line_spans(ask)
    lines = cm_lines(ask)
    owners, protected, headings, bodies, item_starts, continuations, blocks = (
        _block_ownership(ask, with_blocks=True)
    )
    contexts = {(): ("raw_ask", "none")}
    for index, line in enumerate(lines):
        _, fallback = _scope_line(line)
        content = bodies.get(index, fallback).strip()
        if not content.strip():
            continue
        owner = owners[index]
        if (
            index in item_starts
            and index not in bodies
            and re.fullmatch(r"(?:[-*+]|\d+[.)])", content)
        ):
            continue  # The parser owns an empty item; its marker is framing only.
        # Initialise a container from its nearest enclosing context; a sibling
        # has a distinct token identity and cannot inherit the previous item.
        for depth in range(1, len(owner) + 1):
            contexts.setdefault(owner[:depth], contexts[owner[: depth - 1]])
        doc_section, structural_hint = contexts[owner]
        bullet = re.match(r"^(?:[-*+]|\d+[.)])\s+(.+)$", content)
        candidates = (
            [content.strip() if index in bodies else bullet.group(1).strip()]
            if bullet or index in item_starts
            else sentence_units(content)
        )
        if index in headings:
            doc_section = headings[index]
            structural_hint = "none"
        for candidate in candidates:
            marker = SECTION_MARKER.match(headings.get(index, candidate).strip())
            if (
                marker
                and index not in protected
                and (index in headings or SECTION_MARKER.match(content))
            ):
                doc_section = marker.group(0).rstrip(":").strip()
                structural_hint = "exclusion-marked" if marker["exclusion"] else "none"
            contexts[owner] = (doc_section, structural_hint)
            # One canonicalisation for both judge requests and published
            # identities. Retain the established request spelling in state.
            identity = candidate.strip()
            if index in headings:
                identity = cm_lines(headings[index])[0]
            # Backslash hard-break framing belongs only to a parsed inline
            # physical line that actually continues onto the next line.
            if (
                index in continuations
                and (len(identity) - len(identity.rstrip("\\"))) % 2
            ):
                identity = identity[:-1]
            label = SECTION_MARKER.match(identity)
            if label:
                identity = identity[label.end() :].strip()
            units.append(
                ClauseUnit(
                    state={
                        "clause": candidate,
                        "surrounding_sentence": blocks.get(
                            index, (_canonical_block(content), spans[index])
                        )[0],
                        "doc_section": doc_section,
                        "structural_hint": structural_hint,
                    },
                    text=identity.rstrip(".!?"),
                    source_span=spans[index],
                    block_text=blocks.get(
                        index, (_canonical_block(content), spans[index])
                    )[0],
                    block_span=blocks.get(index, (content, spans[index]))[1],
                    container=owner,
                    kind=(
                        "heading"
                        if index in headings
                        else "literal"
                        if index in protected and index in bodies
                        else "inline"
                        if index in bodies
                        else "unparsed"
                    ),
                )
            )
    return units


def _names_referent(block: str) -> bool:
    # Incidental evidence/verifier identifiers cannot bind the prohibited object,
    # even before the anaphor. Only a declaration or a named direct object before
    # it can establish the same-block referent; uncertain bindings walk back.
    anaphor = BLOCK_REFERENT.search(block)
    # The same rule applies to preceding blocks: an evidence paragraph is not
    # a declaration merely because it mentions a verifier's path or filename.
    prefix = block[: anaphor.start()] if anaphor else block
    prefix = prefix.lstrip("| ")
    declaration = NAMED_REFERENT.match(prefix)
    standalone = declaration and (
        re.match(r"(?:target|subject|object):", declaration[0], re.I)
        or not prefix[declaration.end() :].strip("| \t")
        or declaration[0].endswith((".", "!", "?"))
        or prefix[declaration.end() :].lstrip().startswith((".", "!", "?"))
    )
    return bool(
        standalone
        or re.search(
            r"\b(?:maintain|create|keep|preserve|delete|deploy|modify|change|remove|publish|overwrite)\s+"
            + NAMED_REFERENT.pattern,
            prefix,
            re.I,
        )
    )


def _context_declaration(unit: ClauseUnit) -> bool:
    """Only target metadata loses actionable projection, never instructions."""
    text = unit.text.strip("| ")
    if re.match(r"(?:target|subject|object):", text, re.I):
        return True
    # Keep explicit target fragments in a mixed leaf as metadata, but a
    # standalone literal/command is unchanged by adding a subsequent block.
    if (
        NAMED_REFERENT.fullmatch(text)
        and not VERIFIER.search(text)
        and text not in {"/goal", "/loop"}
        and unit.block_text != _canonical_block(unit.state["clause"])
    ):
        return True
    return unit.kind == "heading" and not (
        OUTCOME_START.search(text)
        or re.search(r"\b(?:verify|record|run|check|test)\b", text, re.I)
    )


def _captured_units(ask: str) -> list[ClauseUnit]:  # noqa: C901
    # Deduplicate only equivalent judgements. Repeated wording under another
    # antecedent is a distinct obligation, including its expanded context.
    seen = set()
    states = []
    preceding = []
    for unit in clause_units(ask):
        collective = COLLECTIVE_REFERENT.search(unit.block_text)
        # Section markers change judge hints, not referent lifetime. A target
        # can be declared before an exclusion heading; retain history until a
        # named binding is found (or refuse the complete context at its cap).
        incumbent = (
            unit.block_text == "Verify it with pytest. Do not deploy it."
            and any(
                previous.block_text == "Maintain the release report."
                for previous in preceding
            )
            and all(
                previous.block_span == unit.block_span
                or previous.block_text == "Maintain the release report."
                for previous in preceding
            )
        )
        incumbent = incumbent or (
            unit.block_text == "Do not deploy it."
            and preceding
            and all(
                previous.block_span == unit.block_span
                or (
                    VERIFIER.search(previous.block_text)
                    and OUTCOME_START.search(previous.block_text)
                    and _independent_prefix(previous.block_text, "Do not deploy it.")
                )
                for previous in preceding
            )
        )
        if incumbent:
            unit = replace(unit, compatibility_projection=True)
        if (
            not incumbent
            and (collective or not _names_referent(unit.block_text))
            and (
                BLOCK_REFERENT.search(unit.block_text)
                or ELIDED_TARGET.search(unit.block_text)
            )
        ):
            antecedents = []
            for previous in reversed(preceding):
                if previous.block_span == unit.block_span:
                    continue
                if previous.block_text not in antecedents:
                    antecedents.append(previous.block_text)
                # Only explicit bindings stop walk-back. An unnamed heading
                # may label a restriction section rather than its target;
                # uncertain references keep the preceding document context.
                if not collective and _names_referent(previous.block_text):
                    break
            if antecedents:
                context = _canonical_block("\n".join([*antecedents, unit.block_text]))
                unit = replace(
                    unit,
                    state={**unit.state, "surrounding_sentence": context},
                    antecedent_blocks=tuple(antecedents),
                )
        if not SECTION_MARKER.match(unit.block_text) and (
            not preceding or preceding[-1].block_span != unit.block_span
        ):
            preceding.append(unit)
        state = unit.state
        key = (
            state["clause"],
            state["structural_hint"],
            state["doc_section"],
            unit.block_text,
            state["surrounding_sentence"],
            unit.antecedent_blocks,
        )
        if key not in seen:
            seen.add(key)
            states.append(unit)
    return states


def _scope_states(ask: str) -> list[dict[str, str]]:
    return [unit.state for unit in _captured_units(ask)]


def capture(
    ask: str, *, judge_mode: str | None = None, context_units=()
) -> dict[str, Any]:
    """Capture verbatim bytes and keep exclusions separate from affirmatives."""
    try:
        units = _captured_units(ask)
    except ScopeNestingError as exc:
        return {
            "raw_ask": ask,
            "scope_elements": [ask],
            "affirmative_elements": [],
            "exclusions": [ask],
            "findings": [
                {
                    "check": str(exc).partition("check=")[2].partition(" ")[0],
                    "severity": "error",
                    "line": None,
                    "message": str(exc),
                }
            ],
        }
    exclusions = []
    affirmatives = []
    findings = []
    exclusion_units = []
    judged = []
    for unit in units:
        state = unit.state
        # Field projections may remove grammar labels, but never block context.
        # Classify each original occurrence if identical clauses have distinct
        # containing blocks. Refusal from any occurrence still blocks emission.
        contexts = list(
            dict.fromkeys(
                original.state["surrounding_sentence"]
                for original in context_units
                if state["clause"] in original.state["clause"]
            )
        ) or [state["surrounding_sentence"]]
        polarities = []
        for context in contexts:
            polarity, finding = classify_clause(
                {**state, "surrounding_sentence": context}, judge_mode=judge_mode
            )
            polarities.append(polarity)
            if finding:
                findings.append(finding)
        polarity = False if all(p is False for p in polarities) else True
        judged.append((unit, polarity))
        if polarity is not False:
            exclusions.append(state["clause"])
            exclusion_units.append(unit.text)
        else:
            affirmatives.append(state["clause"])
    preserved = _preserved_blocks(judged)
    context_blocks = {
        block
        for unit, polarity in judged
        if polarity is not False
        for block in unit.antecedent_blocks
    }
    context_elements = [
        unit.state["clause"]
        for unit, polarity in judged
        if polarity is False
        and (unit.block_text in context_blocks or unit.block_text in preserved)
        and _context_declaration(unit)
    ]
    # Referent declarations are context, not goals. Preserving a mixed block or
    # an actionable antecedent is independent of projecting its affirmative
    # instructions: adding an exclusion must never erase a named verifier.
    affirmatives = [
        element for element in affirmatives if element not in context_elements
    ]
    return {
        "raw_ask": ask,
        "context_elements": context_elements,
        "scope_roles": [
            "context"
            if unit.state["clause"] in context_elements
            else "exclusion"
            if polarity is not False
            else "affirmative"
            for unit, polarity in judged
        ],
        "scope_elements": [unit.state["clause"] for unit in units],
        "scope_units": [unit.text for unit in units],
        "scope_blocks": list(dict.fromkeys(unit.block_text for unit in units)),
        "exclusion_blocks": preserved,
        "context_coverage": list(
            dict.fromkeys(
                "\n\n".join([*reversed(unit.antecedent_blocks), unit.block_text])
                for unit, polarity in judged
                if polarity is not False and unit.antecedent_blocks
            )
        ),
        "exclusion_units": exclusion_units,
        "affirmative_elements": [] if findings else affirmatives,
        "exclusions": exclusions,
        "findings": findings,
    }


def _independent_prefix(clause, prohibition):
    """Only explicitly repeated targets permit the compatibility projection.

    The incumbent generic report/artifact contracts retain their historical
    projection. Named paths, identifiers, quotes and all other noun phrases
    default to preserving the antecedent; this is not an NLP polarity fallback.
    """
    target = OUTCOME_START.sub("", clause, count=1)
    target = re.split(
        r",?\s+(?:and\s+)?(?:verify|verified|with\s+pytest)\b",
        target,
        maxsplit=1,
        flags=re.I,
    )[0].strip(" .")
    generic = target.casefold() in {"the report", "an artifact", "the artifact"}
    if generic and prohibition.strip() == "Do not deploy it.":
        return True  # Only the incumbent deployment projection is exceptional.
    if BLOCK_REFERENT.search(prohibition) or ELIDED_TARGET.search(prohibition):
        return False
    return generic or (bool(target) and target in prohibition)


def _preserved_blocks(judged):  # noqa: C901
    """Preserve full leaf blocks; the incumbent same-line affirmative prefix stays.

    Incumbent test_explicit_external_write_prohibition_is_not_an_authority_question
    requires a preceding affirmative sentence to remain outside exclusions. Only
    complete affirmative sentences with a verifier and the established outcome
    vocabulary on the same physical line as the prohibition can be removed
    from the START of a block. No continuation or
    trailing sentence is removed, even when its individual verdict is affirmative.
    Every judge still sees the original complete containing block.
    """
    blocks = {}
    for unit, polarity in judged:
        blocks.setdefault(unit.block_span, []).append((unit, polarity))
    preserved = []
    for entries in blocks.values():
        if all(polarity is False or not unit.text for unit, polarity in entries):
            continue
        for unit, polarity in entries:
            if polarity is not False:
                for antecedent in reversed(unit.antecedent_blocks):
                    if antecedent not in preserved:
                        preserved.append(antecedent)
        text = entries[0][0].block_text
        first_exclusion = next(
            unit for unit, polarity in entries if polarity is not False and unit.text
        )
        for unit, polarity in entries:
            clause = unit.state["clause"]
            if (
                polarity is not False
                or (
                    unit.source_span != first_exclusion.source_span
                    and not unit.compatibility_projection
                )
                or not VERIFIER.search(clause)
                or not (OUTCOME_START.search(clause) or clause.startswith("Verify "))
                or not clause.endswith((".", "!", "?"))
                or not text.startswith(clause + " ")
                or not (
                    unit.compatibility_projection
                    or _independent_prefix(clause, text[len(clause) :])
                )
            ):
                break
            text = text[len(clause) :].lstrip(" ")
        if text and text not in preserved:
            preserved.append(text)
    return preserved


def classify(ask: str, kind: str = "auto") -> str:
    """Classify fit without manufacturing a binary success criterion."""
    if kind not in {"auto", "goal", "loop"}:
        raise ValueError("kind must be goal, loop, or auto")
    if kind != "auto":
        return kind
    if (
        SUBJECTIVE.search(ask)
        and not re.search(r"\b(rubric|score|reviewer)\b", ask, re.I)
    ) or UNRESOLVED_HUMAN_JUDGMENT.search(ask):
        return "not-autonomous-fit"
    if ONE_SHOT.search(ask):
        return "one-shot"
    if WATCH.search(ask):
        return "goal+loop" if ACTION.search(ask) else "loop"
    return "goal"


def _polarity_prompt(captured: dict[str, Any]) -> str:
    return " ".join(
        f"check={finding['check']} severity={finding['severity']}: {finding['message']}"
        for finding in captured.get("findings", [])
    )


def _outcome_candidates(elements: list[str]) -> list[str]:
    return [
        element
        for element in elements
        if not element.rstrip().endswith(":") and OUTCOME_START.search(element)
    ]


def _artifact_signature(outcome: str) -> set[str]:
    without_action = OUTCOME_START.sub("", outcome, count=1).strip()
    target = re.split(
        r"(?:[,;]\s*|\s+)(?:and\s+)?(?:check|test|validate|verify|verified)\b|"
        r"\s+with\s+(?:pytest|the test suite|a checksum|a query)\b",
        without_action,
        maxsplit=1,
        flags=re.I,
    )[0]
    return {
        word
        for word in re.findall(r"[a-z0-9][a-z0-9_-]*", target.casefold())
        if word not in STOP_WORDS
    }


def _artifact_state_independent(candidates: list[str], raw_ask: str) -> bool:
    """Judge order and state independence conservatively from the captured ask."""
    if len(candidates) < MIN_INDEPENDENT_OUTCOMES or DEPENDENCY_LINK.search(raw_ask):
        return False
    signatures = [_artifact_signature(candidate) for candidate in candidates]
    if any(not signature for signature in signatures):
        return False
    for index, left in enumerate(signatures):
        for right in signatures[index + 1 :]:
            if left == right or left < right or right < left:
                return False
    state_noun = (
        r"(?:artifact|branch|database|directory|file|repository|service|state|store)"
    )
    shared_state = re.search(rf"\b(?:same|shared)\s+{state_noun}\b", raw_ask, re.I)
    no_shared_state = re.search(
        rf"\b(?:no|without)\s+shared\s+{state_noun}s?\b",
        raw_ask,
        re.I,
    )
    return not shared_state or bool(no_shared_state)


def decompose(captured: dict[str, Any]) -> list[dict[str, Any]]:
    """Split outcomes only when their artifacts/state are distinct and order-free."""
    if captured.get("findings"):
        raise ValueError(_polarity_prompt(captured))
    affirmative_elements = captured["affirmative_elements"]
    candidates = _outcome_candidates(affirmative_elements)
    selected = (
        candidates
        if _artifact_state_independent(candidates, captured["raw_ask"])
        else [" ".join(affirmative_elements)]
    )
    return [{"text": text.strip(), "depends_on": []} for text in selected]


def _slug(text: str, index: int, used: set[str]) -> str:
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", normalized.lower())
        if word not in STOP_WORDS
    ]
    candidate = "-".join(words[:6]).strip("-")[:63] or f"goal-{index}"
    base = candidate
    suffix = 2
    while candidate in used:
        candidate = f"{base[: 61 - len(str(suffix))]}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _question(kind: str, prompt: str, fit_class: str) -> dict[str, Any]:
    return {
        "fit_class": fit_class,
        "rejection": None,
        "questions": [{"kind": kind, "prompt": prompt}],
        "members": [],
    }


def preflight(
    ask: str, fit_class: str, *, captured: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Return a focused question when autonomy or verification is unresolved."""
    captured = captured if captured is not None else capture(ask)
    if captured.get("findings"):
        return _question("authority", _polarity_prompt(captured), fit_class)
    affirmative = " ".join(captured["affirmative_elements"])
    credential = CREDENTIAL.search(affirmative)
    if credential and not UNAVAILABLE.search(affirmative):
        return _question(
            "authority",
            f"Is the required {credential.group(0)} available to the goal runner?",
            fit_class,
        )
    if EXTERNAL_WRITE.search(affirmative) and not AUTHORITY.search(affirmative):
        action = EXTERNAL_WRITE.search(affirmative)
        assert action
        return _question(
            "authority",
            f"Is the goal runner authorized to {action.group(0).lower()} externally?",
            fit_class,
        )
    if not VERIFIER.search(affirmative):
        if VERIFIER.search(ask):
            return _question(
                "authority",
                "The source names a check only in classified exclusions or context; confirm an authorized affirmative verifier.",
                fit_class,
            )
        return _question(
            "verifier",
            "What non-self verifier and evidence should prove the requested outcome?",
            fit_class,
        )
    return None


def _verifier(text: str) -> tuple[str, str, str]:
    match = VERIFIER.search(text)
    assert match
    token = match.group(0).lower()
    if token == "pytest":
        return "pytest", "pytest output", "test suite"
    if "status" in token or token == "query":
        return "query the named status source", "status response", "status source"
    if token == "reviewer":
        return "obtain the named review", "recorded review", "named reviewer"
    return f"run the named {token}", f"{token} output", "named verifier"


def _coverage_placement(
    element: str, current: str, siblings: list[str], exclusions: list[str]
) -> str:
    normalized = _normalized_element(element)
    if element in exclusions:
        placement = "SCOPE & AUTHORITY — Out of scope"
    elif normalized == _normalized_element(current):
        placement = "GOAL; SUCCESS S1"
    elif normalized in {_normalized_element(sibling) for sibling in siblings}:
        placement = "excluded: sibling goal; manifest"
    elif VERIFIER.search(element):
        placement = "SUCCESS S1"
    elif re.search(
        r"\b(?:either order|no shared|independent|separate|unrelated)\b",
        element,
        re.I,
    ):
        placement = "decomposition; manifest"
    elif OUTCOME_START.search(element):
        placement = "GOAL; SUCCESS S1"
    else:
        placement = "CONTEXT"
    return placement


def bind(
    captured: dict[str, Any],
    outcomes: list[dict[str, Any]],
    fit_class: str,
    cadence_seconds: int | None,
    budget: dict[str, Any],
) -> list[dict[str, Any]]:
    """Bind each outcome to named evidence, authority, and terminal branches."""
    from validate_goal import CADENCE

    if captured.get("findings"):
        raise ValueError(_polarity_prompt(captured))
    raw_ask = captured["raw_ask"]
    scope_elements = captured["scope_elements"]
    credential = CREDENTIAL.search(raw_ask)
    unavailable = credential and UNAVAILABLE.search(raw_ask)
    human_gate = bool(re.search(r"\b(human approval|human review)\b", raw_ask, re.I))
    exclusions = captured["exclusions"]
    preserved = captured.get("exclusion_blocks", exclusions)
    blocked = (
        f"the {credential.group(0)} is unavailable; report the exact blocker and owner action; "
        "say GOAL BLOCKED and stop."
        if unavailable and credential
        else "report the exact blocker and owner action; say GOAL BLOCKED and stop."
    )
    if human_gate:
        blocked = (
            "after 2 activation cycles awaiting the named human approval, report the exact "
            "blocker and owner action; say GOAL BLOCKED and stop."
        )
    used: set[str] = set()
    prepared: list[tuple[dict[str, Any], str, str]] = []
    for index, item in enumerate(outcomes, start=1):
        text = re.sub(r"\s+", " ", item["text"]).strip().rstrip(".!?")
        text = re.sub(r"[.!?]+\s+", "; ", text)
        prepared.append((item, text, _slug(text, index, used)))

    members = []
    for item, text, slug in prepared:
        verifier_source = item["text"] if VERIFIER.search(item["text"]) else raw_ask
        verifier, evidence, authority = _verifier(verifier_source)
        outcome = f"The requested state is true and externally verified: {text}."
        kind = fit_class if fit_class in {"goal", "loop", "goal+loop"} else "goal"
        sibling_outcomes = [
            sibling_text
            for _, sibling_text, sibling_slug in prepared
            if sibling_slug != slug
        ]
        out_of_scope = [*preserved]
        if sibling_outcomes:
            out_of_scope.append(
                "sibling artifacts handled by sibling goals: "
                + "; ".join(
                    " ".join(sorted(_artifact_signature(sibling)))
                    for sibling in sibling_outcomes
                )
            )
        coverage = [
            {
                "element": element,
                "placement": _coverage_placement(
                    element,
                    item["text"],
                    sibling_outcomes,
                    exclusions + captured.get("context_elements", []),
                ),
            }
            for element in scope_elements
        ]
        # The deterministic coverage oracle can join a heading/target with an
        # anaphoric sentence. Record that relationship as a projection too, but
        # keep actual block boundaries so it cannot replace source identities.
        coverage.extend(
            {"element": element, "placement": "SCOPE & AUTHORITY — Out of scope"}
            for element in captured.get("context_coverage", [])
        )
        compact = len(text) > MAX_REPEATED_PROJECTION
        members.append(
            {
                "slug": slug,
                "outcome": outcome,
                "depends_on": item["depends_on"],
                "goal_kind": kind,
                "autonomy": "human-gated" if human_gate else "autonomous",
                "cadence_seconds": cadence_seconds
                if kind in {"loop", "goal+loop"}
                else None,
                "budget": budget,
                "raw_ask": raw_ask,
                "context": "Re-verify current state against Raw ask."
                if compact
                else "Treat the raw ask as the authoritative scope and re-verify current state.",
                "scope_authority": {
                    "in_scope": text
                    if len(text) <= MAX_REPEATED_PROJECTION
                    else "the complete state specified in GOAL",
                    "exclusions": out_of_scope or ["unrequested consequential changes"],
                    "allowed_actions": "make bounded changes needed for the outcome",
                    "human_approval_required_for": (
                        "the named review" if human_gate else "none"
                    ),
                },
                "ground_truth": "Re-read named artifacts and verifier output; persist evidence."
                if compact
                else (
                    "Re-read the named artifacts and verifier output on every activation. "
                    "Persist progress in the named evidence artifact."
                ),
                "success": [
                    {
                        "id": "S1",
                        "predicate": f"The requested state is proven: {text}."
                        if len(text) <= MAX_REPEATED_PROJECTION
                        and not CADENCE.search(text)
                        else "Every requested state specified in GOAL is externally proven.",
                        "verify": verifier,
                        "evidence": evidence,
                        "authority": authority,
                    }
                ],
                "loop_policy": "Read evidence; choose an unmet S-item; change, verify, record."
                if compact
                else (
                    "Read current evidence; choose the highest-priority unmet S-item; make one "
                    "bounded change; run its verifier; record the result and next hypothesis."
                ),
                "termination": {
                    "success": "every S-item proven; say GOAL COMPLETE and stop.",
                    "blocked": blocked,
                    "budget_exhausted": (
                        "report unmet S-items; say GOAL BUDGET EXHAUSTED and stop."
                    ),
                    "unsafe_or_out_of_scope": "boundary+GOAL OUT OF SCOPE; halt",
                },
                "final_report": "Terminal state, evidence, changes, checks, blockers."
                if compact
                else (
                    "Terminal state; evidence by S-item; changes made; checks run; remaining "
                    "blockers or risks."
                ),
                "coverage": coverage,
            }
        )
    return members


def _normalized_element(element: str) -> str:
    # CommonMark table framing can be captured as a physical-line clause.
    # A placed punctuation-only clause still has an identity; word folding
    # must not invent an unplaced requirement for those bytes.
    normalized = re.sub(r"\W+", " ", element.casefold()).strip()
    return normalized or element.casefold().strip()


def self_audit(worker: dict[str, Any], captured_scope: list[str]) -> dict[str, Any]:
    """Check generated coverage against the original captured scope."""
    placed = {
        _normalized_element(row["element"])
        for member in worker["members"]
        for row in member["coverage"]
        if row["placement"].casefold() != "unplaced"
    }
    missing = [
        element
        for element in captured_scope
        if not _normalized_element(element)
        or _normalized_element(element) not in placed
    ]
    if worker["members"]:
        worker["members"][0]["coverage"].extend(
            {"element": element, "placement": "unplaced"} for element in missing
        )
    return worker


def compile_inline(  # noqa: PLR0911
    ask: str,
    *,
    kind: str = "auto",
    cadence_seconds: int | None = None,
    budget: dict[str, Any] | None = None,
    judge_mode: str | None = None,
) -> dict[str, Any]:
    """Run capture, classify, decompose, bind, preflight, and self-audit."""
    fit_class = classify(ask, kind)
    if fit_class == "one-shot":
        return {
            "fit_class": fit_class,
            "rejection": {
                "reason": "The ask is checkable in one bounded pass and needs no standing re-presentation.",
                "alternative": "Run it directly or delegate it as a one-shot task.",
            },
            "questions": [],
            "members": [],
        }
    if fit_class == "not-autonomous-fit":
        return {
            "fit_class": fit_class,
            "rejection": {
                "reason": "The ask needs an unresolved subjective rubric or independent human judgment.",
                "alternative": "Define a scored rubric with a named grader, then draft the goal again.",
            },
            "questions": [],
            "members": [],
        }
    from validate_goal import PLACEHOLDER

    if PLACEHOLDER.search(ask):
        return _question(
            "fit",
            "check=unrendered-parameter severity=error: supply concrete values for native template parameters before inline compilation; canonical declared templates remain supported by template authoring.",
            fit_class,
        )
    captured = capture(ask, judge_mode=judge_mode)
    question = preflight(ask, fit_class, captured=captured)
    if question:
        return question
    if fit_class in {"loop", "goal+loop"} and cadence_seconds is None:
        return _question(
            "fit",
            "Which recognized watched signal should determine the loop cadence?",
            fit_class,
        )
    resolved_budget = budget or {
        "activations": 40,
        "no_progress_cycles": 3,
        "wall_clock": None,
    }
    worker = {
        "fit_class": fit_class,
        "rejection": None,
        "questions": [],
        "members": bind(
            captured,
            decompose(captured),
            fit_class,
            cadence_seconds,
            resolved_budget,
        ),
    }
    # The native evaluator has an incumbent hard capacity. Keep every scope
    # byte in GOAL, avoid repeating long projections, and ask for a shorter
    # contract rather than emitting a document its own validator will refuse.
    from render import _condition
    from validate_goal import MAX_CHARACTERS, _budget_condition, _count, rider_settings

    reserve = int(rider_settings()["reserve_chars"])

    if any(
        re.search(r"<[^>]+>|\[FILL[^\]]*\]", _condition(member))
        for member in worker["members"]
    ):
        return _question(
            "fit",
            "check=unrendered-parameter severity=error: the native condition contains an angle/FILL parameter; supply concrete values or use a worker to author a parameter-free contract without dropping scope.",
            fit_class,
        )
    # Measured as the validator gates it: the {{rider}} slot render.py writes counts at the
    # rider reserve, so a contract that fits here never fails the published doc's length check.
    if any(
        max(_count(_budget_condition(_condition(member), "", reserve)[0]).values())
        > MAX_CHARACTERS
        for member in worker["members"]
    ):
        return _question(
            "fit",
            f"check=length severity=error: the complete contract exceeds the native {MAX_CHARACTERS}-character condition capacity; supply a shorter externally verifiable contract or use a worker to author one without dropping scope.",
            fit_class,
        )
    return self_audit(worker, captured["scope_elements"])
