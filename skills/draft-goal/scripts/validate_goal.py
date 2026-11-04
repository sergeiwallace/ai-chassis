#!/usr/bin/env python3
# ruff: noqa: E402
"""Deterministic validation for rendered draft-goal documents.

This module deliberately uses only the standard library: it is the caller-side
gate and must be available before any delegated drafting route is selected.
"""

from __future__ import annotations

import os as _bootstrap_os
import sys as _bootstrap_sys

_bootstrap_directory = _bootstrap_os.path.dirname(_bootstrap_os.path.realpath(__file__))
while not _bootstrap_os.path.isfile(
    _bootstrap_os.path.join(_bootstrap_directory, "pyproject.toml")
):
    _bootstrap_parent = _bootstrap_os.path.dirname(_bootstrap_directory)
    if _bootstrap_parent == _bootstrap_directory:
        break
    _bootstrap_directory = _bootstrap_parent
_bootstrap_sys.path.insert(0, _bootstrap_os.path.join(_bootstrap_directory, "scripts"))
try:
    from py_interpreter_bootstrap import ensure_supported_python  # noqa: E402
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):  # noqa: E402
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
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

from pathlib import Path
from typing import Any, NamedTuple

# These consumers are also loaded directly by file path outside the CLI.
# Resolve their sibling helper without relying on import order in the caller.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from line_endings import cm_lines

RULESET = "draft-goal-v1.0.0"
MAX_CHARACTERS = 4000
# Reusable goal templates; a file directly here re-stamps without the judge (ISSUE-REF).
TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "docs/prompts/goal/templates"
WARNING_CHARACTERS = 3600
MIN_MEANINGFUL_TOKEN_LENGTH = 3
MIN_COVERAGE_CELLS = 2
MIN_COVERAGE_OVERLAP = 2
MAX_RAW_ASK_DELIMITERS = 2
COVERAGE_RATIO = 0.6
REQUIRED_SECTIONS = ("GOAL", "SCOPE & AUTHORITY", "SUCCESS", "TERMINATION")
# STUB.md/TEMPLATE.md document these as optional sections between required ones
# (CONTEXT after GOAL, GROUND TRUTH after SCOPE & AUTHORITY). They must still be
# recognized as section boundaries, or their body silently folds into whichever
# required section precedes them and corrupts that section's own checks (e.g. a
# folded-in CONTEXT paragraph makes GOAL look multi-line and trips single-outcome).
OPTIONAL_SECTIONS = ("CONTEXT", "GROUND TRUTH", "LOOP POLICY", "FINAL REPORT")
SECTION_HEADERS = REQUIRED_SECTIONS + OPTIONAL_SECTIONS
TERMINAL_BRANCHES = ("SUCCESS", "BLOCKED", "BUDGET_EXHAUSTED", "UNSAFE_OR_OUT_OF_SCOPE")
EFFORT_VERBS = re.compile(r"\b(try|attempt|work on|improve|investigate)\b", re.I)
CADENCE = re.compile(r"/loop|every\s+\d+\s*(?:s|min)|ScheduleWakeup", re.I)
FENCE = re.compile(r"(?ms)^```text[ \t]*\n(.*?)\n```[ \t]*$")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
# A reusable template may carry `{{name}}` placeholders ONLY for names its frontmatter declares
# under `params:`; /start-goal substitutes them before injection (ISSUE-REF D-3). A rendered
# condition declares nothing, so any placeholder left in it is an error.
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
# The reserved, system-declared param /start-goal fills with the rider clause (ISSUE-REF D-5).
# No template declares it; the gating count is measured with it at the configured reserve.
RIDER_PARAM = "rider"
MAX_CHARS = re.compile(r"\bmax_chars:\s*(\d+)")
DEFAULT = re.compile(r'\bdefault:\s*"([^"]*)"')
REPO_ROOT = Path(__file__).resolve().parents[3]
RIDER_CONFIG = REPO_ROOT / "config" / "start-goal.toml"
RIDER_ENV_PREFIX = "AIH_START_GOAL_RIDER__"
FLEET_PREFIXES = REPO_ROOT / "config" / "bd_fleet_prefixes.yaml"
RIDER_DEFAULTS: dict[str, Any] = {
    "dir": "docs/prompts/goal/riders",
    "max_path_chars": 80,
    "reserve_chars": 175,
    "max_body_chars": 6000,
    "public_repos": ["ai-cli-utils"],
}
RIDER_REQUIRED_KEYS = (
    "category",
    "rider_version",
    "created_at",
    "created_by",
    "amends",
    "issue",
    "ask",
    "supersedes",
)
RIDER_VERSIONS = {"1"}
RIDER_NAME = re.compile(r"^(?P<date>\d{6})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
BD_TIMEOUT_SECONDS = 30
LEGACY_COMPATIBLE = {
    "issue-ref-goal.md",
    "issue-ref-worktree-and-remote-branch-cleanup-goal.md",
    "example-project-incubator-goal.md",
}
ACTION_FORMS = {
    "build": {"build", "building", "built"},
    "change": {"change", "changed", "changes", "changing", "modification", "modify"},
    "create": {"create", "created", "creates", "creating", "creation"},
    "delete": {
        "delete",
        "deleted",
        "deletes",
        "deleting",
        "deletion",
        "remove",
        "removing",
    },
    "deploy": {"deploy", "deployed", "deploying", "deployment"},
    "edit": {"edit", "edited", "editing"},
    "fix": {"fix", "fixed", "fixes", "fixing", "repair", "repaired", "repairing"},
    "maintain": {"maintain", "maintained", "maintaining", "maintenance"},
    "merge": {"merge", "merged", "merging"},
    "publish": {"publish", "published", "publishing"},
    "send": {"send", "sending", "sent"},
    "surface": {"surface", "surfaced", "surfacing", "report", "reporting"},
    "update": {"update", "updated", "updates", "updating"},
    "verify": {"verify", "verified", "verifies", "verifying", "verification"},
    "write": {"write", "writes", "writing", "written"},
}
TOKEN_STOP = {
    "and",
    "for",
    "from",
    "into",
    "never",
    "none",
    "scope",
    "that",
    "the",
    "this",
    "with",
    "without",
}


def _finding(
    check: str, severity: str, line: int | None, message: str
) -> dict[str, Any]:
    return {"check": check, "severity": severity, "line": line, "message": message}


def _frontmatter(text: str) -> tuple[dict[str, str], tuple[int, int]]:
    match = FRONTMATTER.match(text)
    if not match:
        return {}, (0, 0)
    fields: dict[str, str] = {}
    for line in cm_lines(match.group(1)):
        if ":" in line and not line.startswith((" ", "\t")):
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip().strip('"')
    return fields, match.span(1)


def _declared_params(text: str) -> set[str]:
    """Names declared under the frontmatter `params:` block (indented child keys)."""
    match = FRONTMATTER.match(text)
    if not match:
        return set()
    declared: set[str] = set()
    in_block = False
    for line in cm_lines(match.group(1)):
        if line.startswith("params:"):
            in_block = True
            continue
        if in_block and line.startswith((" ", "\t")) and ":" in line:
            declared.add(line.strip().split(":", 1)[0])
        elif in_block:
            break
    return declared


def _param_budget_fills(text: str) -> dict[str, str]:
    """The text each declared param is measured at: `max_chars` characters when it sets that
    optional key, else its `default:` string, which /start-goal renders when none is supplied."""
    match = FRONTMATTER.match(text)
    if not match:
        return {}
    fills: dict[str, str] = {}
    in_block = False
    for line in cm_lines(match.group(1)):
        if line.startswith("params:"):
            in_block = True
            continue
        if in_block and line.startswith((" ", "\t")) and ":" in line:
            name = line.strip().split(":", 1)[0]
            limit = MAX_CHARS.search(line)
            default = DEFAULT.search(line)
            if limit:
                fills[name] = "x" * int(limit.group(1))
            elif default:
                fills[name] = default.group(1)
        elif in_block:
            break
    return fills


def _rider_table(path: Path) -> dict[str, Any]:
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8")).get("rider", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {k: v for k, v in table.items() if k in RIDER_DEFAULTS}


def _rider_env() -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, default in RIDER_DEFAULTS.items():
        raw = os.environ.get(f"{RIDER_ENV_PREFIX}{key.upper()}")
        if raw is None:
            continue
        try:
            found[key] = (
                json.loads(raw)
                if isinstance(default, list)
                else type(default)(raw.strip())
            )
        except ValueError:
            continue
    return found


def rider_settings() -> dict[str, Any]:
    """`[rider]` resolved at call time through start_goal_settings' layers: env
    `AIH_START_GOAL_RIDER__*` over the user's start-goal.toml over the repo's over defaults."""
    user = Path.home() / ".config" / "ai-harness" / "start-goal.toml"
    return {
        **RIDER_DEFAULTS,
        **_rider_table(RIDER_CONFIG),
        **_rider_table(user),
        **_rider_env(),
    }


def _budget_condition(condition: str, text: str, reserve: int) -> tuple[str, int]:
    """The condition as the gating count measures it, and how many `{{rider}}` it carries.

    A param that sets `max_chars` is measured at that length, any other param at its default,
    and the one `{{rider}}` slot at the rider reserve, so the count is the rendered size a
    default arming produces, never the shorter `{{name}}` placeholder.
    """
    fills = _param_budget_fills(text)
    riders = 0

    def fill(match: re.Match[str]) -> str:
        nonlocal riders
        name = match.group(1)
        if name == RIDER_PARAM:
            riders += 1
            return "x" * reserve
        return fills.get(name, match.group(0))

    return PLACEHOLDER.sub(fill, condition), riders


def raw_ask_span(text: str) -> tuple[int, int] | None:
    """Locate raw bytes using render's character count, never payload headings.

    Older handwritten documents use the canonical Coverage/Launch delimiters.
    Ambiguous repeated delimiters refuse recovery rather than truncate the ask.
    """
    heading = re.search(r"(?m)^## Raw ask\n\n", text)
    if not heading:
        return None
    start = heading.end()
    fields, _ = _frontmatter(text)
    if "raw_ask_chars" in fields:
        try:
            count = int(fields["raw_ask_chars"])
        except ValueError as exc:
            raise ValueError("check=raw-ask-framing: invalid raw_ask_chars") from exc
        end = start + count
        if count < 1 or end > len(text) or not re.match(r"\n\n(?:## |\Z)", text[end:]):
            raise ValueError("check=raw-ask-framing: invalid raw ask boundary")
        return start, end
    delimiters = list(
        re.finditer(
            r"(?m)^## (?P<kind>Coverage|Launch)[^\n]*\n(?=\s*(?:\||`|Paste|copy))",
            text[start:],
        )
    )
    if len(delimiters) > MAX_RAW_ASK_DELIMITERS or len(
        {m["kind"] for m in delimiters}
    ) != len(delimiters):
        raise ValueError(
            "check=raw-ask-framing: ambiguous raw ask; rerender with framing"
        )
    end = start + delimiters[0].start() if delimiters else len(text)
    return start, end


def raw_ask_text(text: str) -> str:
    span = raw_ask_span(text)
    return text[slice(*span)] if span else ""


def read_document(path: Path) -> str:
    """Preserve framed asks' newline bytes; retain legacy universal newlines."""
    text = path.read_bytes().decode("utf-8")
    fields, _ = _frontmatter(text)
    if "raw_ask_chars" in fields:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _without_raw_ask(text: str) -> str:
    span = raw_ask_span(text)
    if not span:
        return text
    start, end = span
    # Preserve offsets and line numbers used in deterministic diagnostics.
    return text[:start] + re.sub(r"[^\n]", " ", text[start:end]) + text[end:]


def _condition_matches(text: str) -> list[re.Match[str]]:
    text = _without_raw_ask(text)
    launch = re.search(r"(?ms)^## Launch[^\n]*\n(.*?)(?=^## |\Z)", text)
    if not launch:
        return []
    return list(FENCE.finditer(launch.group(1)))


def _condition(text: str) -> tuple[str, int]:
    text = _without_raw_ask(text)
    matches = _condition_matches(text)
    if not matches:
        return "", 1
    match = matches[0]
    launch = re.search(r"(?ms)^## Launch[^\n]*\n(.*?)(?=^## |\Z)", text)
    assert launch
    absolute_start = launch.start(1) + match.start(1)
    return match.group(1), text[:absolute_start].count("\n") + 1


def _section_header(line: str) -> str | None:
    """Match a section header line, allowing a qualifier after the bare header

    (e.g. ``CONTEXT (snapshot; re-verify anything load-bearing)``, the convention
    every real-world goal doc uses in place of STUB.md's bare placeholder word).
    The qualifier opens with ``(`` or `` — ``, and only an optional section takes one: a
    required header is its bare word, so a prose line that merely starts with one, such as
    ``GOAL COMPLETE is said ...`` or ``GOAL (as above) ...`` in TERMINATION, is no header.
    """
    if line == "SUCCESS — ALL MUST BE TRUE":
        return "SUCCESS"
    for header in SECTION_HEADERS:
        if line == header:
            return header
    for header in OPTIONAL_SECTIONS:
        if line.startswith((header + " (", header + " — ")):
            return header
    return None


def _sections(condition: str) -> dict[str, tuple[str, int]]:
    result: dict[str, tuple[str, int]] = {}
    lines = cm_lines(condition)
    starts = []
    for index, line in enumerate(lines):
        header = _section_header(line)
        if header:
            starts.append((index, header))
    for position, (index, name) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        result[name] = ("\n".join(lines[index + 1 : end]).strip(), index + 1)
    return result


def _count(condition: str) -> dict[str, int]:
    return {
        "scalars": len(condition),
        "utf8_bytes": len(condition.encode("utf-8")),
        "utf16_units": len(condition.encode("utf-16-le")) // 2,
    }


def _gating_count(counts: dict[str, int], semantics: str) -> int:
    if semantics == "max":
        return max(counts.values())
    return counts[semantics]


def _gating_semantic(counts: dict[str, int], semantics: str) -> str:
    if semantics != "max":
        return semantics
    maximum = max(counts.values())
    winners = [name for name, value in counts.items() if value == maximum]
    return winners[0] if len(winners) == 1 else f"max({','.join(winners)})"


def _validator_settings(
    count_semantics: str, warn_at: int, fail_at: int
) -> tuple[str, int, int]:
    if count_semantics not in {"max", "scalars", "utf8_bytes", "utf16_units"}:
        raise ValueError("invalid count semantics")
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (warn_at, fail_at)
    ):
        raise ValueError("validator thresholds must be integers")
    if warn_at <= 0 or fail_at <= 0 or warn_at > fail_at:
        raise ValueError("validator thresholds require 0 < warn_at <= fail_at")
    if fail_at > MAX_CHARACTERS:
        raise ValueError(
            f"validator fail_at cannot exceed native limit {MAX_CHARACTERS}"
        )
    return count_semantics, warn_at, fail_at


def _sentences(text: str) -> list[str]:
    from compiler import clause_units

    return clause_units(text, sentence_texts=True)


def _stem(token: str) -> str:
    lowered = token.casefold()
    for action, forms in ACTION_FORMS.items():
        if lowered in forms:
            return action
    for suffix in ("ing", "ed", "es", "s"):
        if len(lowered) > len(suffix) + 3 and lowered.endswith(suffix):
            return lowered[: -len(suffix)]
    return lowered


def _tokens(text: str) -> set[str]:
    return {
        stemmed
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", text)
        if (stemmed := _stem(token)) not in TOKEN_STOP
        and len(stemmed) >= MIN_MEANINGFUL_TOKEN_LENGTH
    }


def _target_stem(token: str) -> str:
    lowered = token.casefold()
    for suffix in ("ing", "ed", "es", "s"):
        if len(lowered) > len(suffix) + 3 and lowered.endswith(suffix):
            return lowered[: -len(suffix)]
    return lowered


def _target_tokens(text: str) -> set[str]:
    return {
        stemmed
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", text)
        if (stemmed := _target_stem(token)) not in TOKEN_STOP
        and len(stemmed) >= MIN_MEANINGFUL_TOKEN_LENGTH
    }


def _action_targets(
    text: str, *, negative_only: bool = False
) -> list[tuple[str | None, set[str]]]:
    """Return every normalized action with the target tail in its clause."""
    pairs: list[tuple[str | None, set[str]]] = []
    for clause in re.split(r"[.;:\n]+", text):
        if negative_only and not re.search(
            r"\b(?:not|don't|never|no|cannot|without|only|except)\b", clause, re.I
        ):
            continue
        words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", clause)
        clause_pairs = [
            (normalized, _target_tokens(" ".join(words[index + 1 :])))
            for index, word in enumerate(words)
            if (normalized := _stem(word)) in ACTION_FORMS
        ]
        pairs.extend(clause_pairs or [(None, _target_tokens(clause))])
    return pairs


def _exclusion_action_targets(text: str) -> list[tuple[str | None, set[str]]]:
    """Do not treat affirmative twins in a preserved block as forbidden actions.

    This is the deterministic contradiction heuristic, not classification
    authority. The shared publication guard still judges every operational
    clause and refuses any classified prohibition there.
    """
    negative = r"\b(?:not|don't|never|no|cannot|without|only|except)\b"
    return _action_targets(text, negative_only=bool(re.search(negative, text, re.I)))


def _scope_exclusions(scope: str) -> list[str]:
    match = re.search(
        r"(?is)Out of scope:\s*(.*?)(?=\s+(?:You may|Human approval required for):|$)",
        scope,
    )
    if not match:
        return []
    # Consume the schema's Markdown item structure, with the same units as the
    # compiler/publication guard. Authored semicolons and commas are data.
    from compiler import clause_units

    return [unit.text for unit in clause_units(match.group(1)) if unit.text]


def _coverage_rows(text: str) -> tuple[str | None, list[tuple[str, str]]]:
    text = _without_raw_ask(text)
    coverage = re.search(r"^## Coverage\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not coverage:
        return None, []
    rows: list[tuple[str, str]] = []
    for line in cm_lines(coverage.group(1)):
        if not line.strip().startswith("|") or "---" in line or "Raw element" in line:
            continue
        from render import _decode_table_cell

        cells = [
            _decode_table_cell(cell.strip())
            for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]
        ]
        if len(cells) >= MIN_COVERAGE_CELLS:
            rows.append((cells[0], cells[1]))
    return coverage.group(1), rows


def _raw_requirements(raw: str) -> list[str]:
    requirements: list[str] = []
    imperative = re.compile(
        r"^(?:build|create|deliver|ensure|keep|maintain|make|monitor|preserve|produce|repair|watch)\b",
        re.I,
    )
    for line in cm_lines(raw):
        bullet = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.+)$", line)
        if bullet:
            requirements.append(bullet.group(1).strip())
    for sentence in _sentences(re.sub(r"\s+", " ", raw)):
        if imperative.search(sentence) or re.search(
            r"\b(must|shall|should|need to|do not|never|only)\b", sentence, re.I
        ):
            requirements.append(sentence)
    unique: list[str] = []
    for requirement in requirements:
        if requirement not in unique:
            unique.append(requirement)
    return unique


def _coverage_match(requirement: str, element: str) -> bool:
    required = _tokens(requirement)
    covered = _tokens(element)
    if not required:
        return True
    overlap = len(required & covered)
    return overlap >= MIN_COVERAGE_OVERLAP or overlap / len(required) >= COVERAGE_RATIO


def _structured_dependency_values(
    sections: dict[str, tuple[str, int]],
) -> list[str]:
    """Return dependency-bearing values from the condition's labelled fields."""
    values: list[str] = []
    scope = sections.get("SCOPE & AUTHORITY", ("", 0))[0]
    in_scope = re.search(r"(?is)In scope:\s*(.*?)(?=\s+Out of scope:|$)", scope)
    if in_scope:
        values.append(in_scope.group(1))
    success = sections.get("SUCCESS", ("", 0))[0]
    for predicate in re.finditer(r"(?m)^S\d+\.\s+(.+)$", success):
        body = predicate.group(1)
        values.append(body.split("evidence:", 1)[0])
    for name in ("CONTEXT", "GROUND TRUTH"):
        value = sections.get(name, ("", 0))[0]
        if value:
            values.append(value)
    return values


def _named_dependencies(values: list[str]) -> set[str]:
    """Extract named dependencies without treating ordinary prose as labels."""
    introduced = re.compile(
        r"\b(?:using|requires?|needs?|depends? on)\s+(?:the\s+)?"
        r"(?P<name>[A-Za-z][\w-]*(?:\s+[A-Za-z][\w-]*){0,2}\s+"
        r"(?:credential|token|API key|service|system|dependency|environment))\b",
        re.I,
    )
    proper_named = re.compile(
        r"\b(?P<name>[A-Z][A-Za-z0-9_-]*(?:\s+[A-Z][A-Za-z0-9_-]*)*\s+"
        r"(?:credential|token|service|system|dependency|environment))\b"
    )
    dependencies: set[str] = set()
    for value in values:
        for pattern in (introduced, proper_named):
            for match in pattern.finditer(value):
                name = match.group("name").strip()
                if name.split(maxsplit=1)[0].casefold() in {
                    "a",
                    "an",
                    "named",
                    "the",
                    "this",
                }:
                    continue
                dependencies.add(name)
    return dependencies


def validate_text(  # noqa: C901, PLR0912, PLR0913, PLR0915
    text: str,
    *,
    path: str = "<memory>",
    count_semantics: str = "max",
    warn_at: int = WARNING_CHARACTERS,
    fail_at: int = MAX_CHARACTERS,
    legacy_compatibility: bool = False,
    polarity_findings: list[dict[str, Any]] | None = None,
    require_rider: bool = False,
) -> dict[str, Any]:
    """Return the complete validation report without mutating *text*.

    `require_rider` fails a condition with no `{{rider}}` slot; more than one always fails.
    """
    count_semantics, warn_at, fail_at = _validator_settings(
        count_semantics, warn_at, fail_at
    )
    fields, _ = _frontmatter(text)
    condition, offset = _condition(text)
    findings: list[dict[str, Any]] = list(polarity_findings or [])
    measured, riders = _budget_condition(
        condition, text, int(rider_settings()["reserve_chars"])
    )
    if riders > 1 or (require_rider and riders == 0):
        findings.append(
            _finding(
                "rider-placeholder",
                "error",
                offset,
                f"condition carries {{{{{RIDER_PARAM}}}}} {riders} times; a template carries"
                " it exactly once, at the end of its SCOPE & AUTHORITY line",
            )
        )
    if RIDER_PARAM in _declared_params(text):
        findings.append(
            _finding(
                "reserved-param",
                "error",
                None,
                f"params must not declare {RIDER_PARAM!r}: it is reserved for /start-goal's"
                " rider clause",
            )
        )
    matches = _condition_matches(text)
    if not matches:
        findings.append(
            _finding(
                "condition",
                "error",
                1,
                "missing fenced text condition in canonical ## Launch section",
            )
        )
    elif len(matches) > 1:
        findings.append(
            _finding(
                "condition",
                "error",
                offset,
                "multiple fenced text conditions in canonical ## Launch section",
            )
        )
    counts = _count(measured)
    gating = _gating_count(counts, count_semantics)
    gating_semantic = _gating_semantic(counts, count_semantics)
    counts["gating"] = gating
    if gating > fail_at:
        findings.append(
            _finding(
                "length",
                "error",
                offset,
                f"condition is {gating}, limit is {fail_at}",
            )
        )
    elif gating >= warn_at:
        findings.append(
            _finding(
                "length",
                "warning",
                offset,
                f"condition is {gating}, warning threshold is {warn_at}",
            )
        )
    if re.search(r"<[^>]+>|\[FILL[^\]]*\]", condition):
        findings.append(
            _finding(
                "placeholders", "error", offset, "condition contains a placeholder"
            )
        )
    undeclared = sorted(
        {found.group(1) for found in PLACEHOLDER.finditer(condition)}
        - _declared_params(text)
        - {RIDER_PARAM}
    )
    if undeclared:
        findings.append(
            _finding(
                "unrendered-parameter",
                "error",
                offset,
                "condition carries undeclared placeholder(s) "
                + ", ".join("{{" + name + "}}" for name in undeclared)
                + ": declare them under frontmatter params, or render them before validating",
            )
        )

    validation_mode = "legacy-compatibility" if legacy_compatibility else "canonical"
    if legacy_compatibility:
        if Path(path).name not in LEGACY_COMPATIBLE:
            findings.append(
                _finding(
                    "legacy-compatibility",
                    "error",
                    None,
                    "legacy compatibility is limited to the three migrated canonical examples",
                )
            )
        for marker in ("OBJECTIVE", "DONE", "GOAL COMPLETE"):
            if marker not in condition:
                findings.append(
                    _finding(
                        "legacy-compatibility",
                        "error",
                        offset,
                        f"legacy condition lacks {marker}",
                    )
                )
        coverage_text, _ = _coverage_rows(text)
        if coverage_text is None:
            findings.append(
                _finding(
                    "confirmed-raw-ask-coverage", "error", None, "missing ## Coverage"
                )
            )
        autonomy = fields.get("autonomy")
        if autonomy not in {"autonomous", "human-gated"}:
            findings.append(
                _finding(
                    "autonomy",
                    "error",
                    None,
                    "autonomy: expected autonomous|human-gated",
                )
            )
        return {
            "path": path,
            "ok": not any(finding["severity"] == "error" for finding in findings),
            "counts": counts,
            "gating_semantic": gating_semantic,
            "findings": findings,
            "autonomy": autonomy,
            "validation_mode": validation_mode,
        }

    sections = _sections(condition)
    for name in REQUIRED_SECTIONS:
        if name not in sections or not sections[name][0]:
            findings.append(
                _finding(
                    "required-sections", "error", offset, f"{name} is missing or empty"
                )
            )
    goal = sections.get("GOAL", ("", 0))[0]
    if goal and (
        len([line for line in cm_lines(goal) if line.strip()]) > 1
        or len(_sentences(goal)) > 1
    ):
        findings.append(
            _finding(
                "single-outcome",
                "error",
                offset,
                "GOAL must contain one outcome sentence",
            )
        )

    success, success_line = sections.get("SUCCESS", ("", 0))
    predicates = list(re.finditer(r"^S\d+\.\s+(.+)$", success, re.M))
    if not predicates:
        findings.append(
            _finding(
                "predicates",
                "error",
                offset + success_line,
                "at least one S<n>. predicate is required",
            )
        )
    for predicate in predicates:
        line = offset + success_line + success[: predicate.start()].count("\n")
        body = predicate.group(1)
        for label in ("verify:", "evidence:", "authority:"):
            if label not in body:
                findings.append(
                    _finding("predicates", "error", line, f"S-item lacks {label}")
                )
        before_metadata = body.split("verify:", 1)[0]
        if EFFORT_VERBS.search(before_metadata) and not re.search(
            r"\d|>=|≤|<|>", before_metadata
        ):
            findings.append(
                _finding(
                    "effort-only-success", "error", line, "predicate is effort-only"
                )
            )
    termination, termination_line = sections.get("TERMINATION", ("", 0))
    for branch in TERMINAL_BRANCHES:
        if not re.search(rf"(?m)^{branch}:", termination):
            findings.append(
                _finding(
                    "terminal-branches",
                    "error",
                    offset + termination_line,
                    f"missing {branch}: branch",
                )
            )
    blocked = re.search(r"(?m)^BLOCKED:\s*(.*)$", termination)
    if blocked and re.search(r"GOAL COMPLETE|success", blocked.group(1), re.I):
        findings.append(
            _finding(
                "terminal-branches",
                "error",
                offset + termination_line,
                "BLOCKED must not claim success",
            )
        )
    if CADENCE.search(success + "\n" + termination):
        findings.append(
            _finding(
                "cadence-leakage",
                "error",
                offset,
                "cadence token appears in SUCCESS or TERMINATION",
            )
        )

    scope = sections.get("SCOPE & AUTHORITY", ("", 0))[0]
    exclusions = _scope_exclusions(scope)
    from compiler import ScopeNestingError, _captured_units, clause_units

    # An exact source antecedent is context, not a forbidden action. This
    # deterministic hint never bypasses the publication authority guard.
    try:
        antecedent_blocks = {
            block
            for unit in _captured_units(raw_ask_text(text))
            for block in unit.antecedent_blocks
        }
        antecedent_units = {block.rstrip(".!?") for block in antecedent_blocks} | {
            context.text
            for block in antecedent_blocks
            for context in clause_units(block)
        }
    except (ScopeNestingError, ValueError):
        antecedent_units = set()
    for exclusion in exclusions:
        if exclusion in antecedent_units:
            continue
        for predicate in predicates:
            body = predicate.group(1).split("verify:", 1)[0]
            confirmed = False
            overlapping = False
            for excluded_action, excluded_target in _exclusion_action_targets(
                exclusion
            ):
                for predicate_action, predicate_target in _action_targets(body):
                    if not excluded_target & predicate_target:
                        continue
                    overlapping = True
                    if excluded_action and excluded_action == predicate_action:
                        confirmed = True
            if not overlapping:
                continue
            line = offset + success_line + success[: predicate.start()].count("\n")
            if confirmed:
                findings.append(
                    _finding(
                        "confirmed-contradiction",
                        "error",
                        line,
                        "normalized action and target violate Out of scope",
                    )
                )
            elif not re.search(
                r"\b(?:do not|don't|never|must not|shall not|may not)\b",
                exclusion,
                re.I,
            ):
                findings.append(
                    _finding(
                        "suspected-contradiction",
                        "warning",
                        line,
                        "scope and predicate overlap",
                    )
                )
    coverage_text, coverage_rows = _coverage_rows(text)
    if coverage_text and re.search(r"\bunplaced\b", coverage_text, re.I):
        findings.append(
            _finding(
                "confirmed-raw-ask-coverage",
                "error",
                None,
                "Coverage records an unplaced requirement",
            )
        )
    elif coverage_text is None:
        findings.append(
            _finding("confirmed-raw-ask-coverage", "error", None, "missing ## Coverage")
        )
    raw = raw_ask_text(text)
    if raw.strip() and coverage_text is not None:
        for requirement in _raw_requirements(raw):
            matching = [
                placement
                for element, placement in coverage_rows
                if _coverage_match(requirement, element)
            ]
            label = " ".join(sorted(_tokens(requirement)))
            if not matching:
                findings.append(
                    _finding(
                        "confirmed-raw-ask-coverage",
                        "error",
                        None,
                        f"raw requirement absent from Coverage: {label}",
                    )
                )
            elif not any(
                _tokens(requirement) & _tokens(condition_section)
                for condition_section in (condition, " ".join(matching))
            ):
                findings.append(
                    _finding(
                        "suspected-raw-ask-coverage",
                        "warning",
                        None,
                        f"Coverage placement is not substantiated for: {label}",
                    )
                )

    approval = re.search(r"Human approval required for:\s*(.*?)(?=\.|$)", scope, re.I)
    authority_text = (
        (approval.group(1) if approval else "")
        + "\n"
        + (blocked.group(1) if blocked else "")
    )
    dependency_values = _structured_dependency_values(sections)
    for dependency in sorted(_named_dependencies(dependency_values)):
        if dependency.casefold() not in authority_text.casefold():
            findings.append(
                _finding(
                    "dependency-without-authority",
                    "warning",
                    None,
                    f"{dependency} is not bound to approval or BLOCKED",
                )
            )

    autonomy = fields.get("autonomy")
    if autonomy not in {"autonomous", "human-gated"}:
        findings.append(
            _finding(
                "autonomy", "error", None, "autonomy: expected autonomous|human-gated"
            )
        )
    elif autonomy == "human-gated":
        approval = re.search(
            r"Human approval required for:\s*(?!none\b)(.+)", scope, re.I
        )
        bounded = blocked and re.search(
            r"\b(within|after|by)\b.*\b(hour|day|activation|cycle)",
            blocked.group(1),
            re.I,
        )
        if not approval or not bounded:
            findings.append(
                _finding(
                    "autonomy",
                    "error",
                    None,
                    "human-gated requires bounded approval metadata",
                )
            )

    return {
        "path": path,
        "ok": not any(f["severity"] == "error" for f in findings),
        "counts": counts,
        "gating_semantic": gating_semantic,
        "findings": findings,
        "autonomy": autonomy,
        "validation_mode": validation_mode,
    }


def normalized_digest(text: str) -> str:
    """Digest a document after blanking mutable validator outputs only."""
    normalized = re.sub(
        r"(?m)^(validated_at|validated_digest|char_count):.*$", r"\1: ", text
    )
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_currently_validated(text: str) -> bool:
    fields, _ = _frontmatter(text)
    return bool(
        fields.get("validated_at")
        and fields.get("validator_ruleset")
        and fields.get("validated_digest") == normalized_digest(text)
    )


class RiderLocationError(ValueError):
    """A rider path that does not name a file in a repository under the projects root."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class UnknownBeadsId(ValueError):
    """An `exclude` value `bd show` (or `bdx`, as a display id) cannot resolve."""


def projects_root() -> Path:
    """`PROJECTS_DIR`, else `~/projects`, resolved: the root every rider path is relative to."""
    configured = os.environ.get("PROJECTS_DIR", "").strip()
    return (Path(configured) if configured else Path.home() / "projects").resolve()


def _git_text(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=BD_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


class RiderLocation(NamedTuple):
    """Where a rider is: its projects-relative name, its repository's main tree, the path
    inside that repository, and the working file the caller named when it exists."""

    relative: str
    repo: str
    repo_dir: Path
    in_repo: str
    working_file: Path | None


def locate_rider(reference: str, root: Path | None = None) -> RiderLocation:
    """Resolve `reference` (projects-relative, or absolute and under the projects root).

    An absolute path inside a worktree names the same rider as its main tree's path, since
    a rider is bound from the repository's default branch, never from a checkout.
    """
    base = root or projects_root()
    given = Path(reference).expanduser()
    candidate = (given if given.is_absolute() else base / given).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise RiderLocationError(
            "rider-outside-projects", f"{reference!r} is not under {base}"
        ) from exc
    existing = candidate.parent
    while not existing.is_dir() and existing != base:
        existing = existing.parent
    top = _git_text(["rev-parse", "--show-toplevel"], existing)
    common = _git_text(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], existing
    )
    if not top or not common:
        raise RiderLocationError(
            "rider-not-found", f"{reference!r} is not inside a git repository"
        )
    toplevel = Path(top).resolve()
    common_dir = Path(common).resolve()
    main = common_dir.parent if common_dir.name == ".git" else toplevel
    try:
        in_repo = candidate.relative_to(toplevel).as_posix()
        repo = main.relative_to(base).as_posix()
    except ValueError as exc:
        raise RiderLocationError(
            "rider-outside-projects",
            f"{reference!r}: its repository is not under {base}",
        ) from exc
    if not in_repo or in_repo == "." or repo == ".":
        raise RiderLocationError(
            "rider-not-found", f"{reference!r} does not name a file in a repository"
        )
    return RiderLocation(
        relative=f"{repo}/{in_repo}",
        repo=repo,
        repo_dir=main,
        in_repo=in_repo,
        working_file=candidate if candidate.is_file() else None,
    )


def _bd_error(result: subprocess.CompletedProcess[str]) -> str:
    lines = [
        line
        for line in cm_lines(result.stderr)
        if line.strip() and not line.startswith(("Warning:", "Hint:"))
    ]
    if lines:
        return " ".join(lines)
    try:
        return (
            str(json.loads(result.stdout).get("error", ""))
            or f"exit {result.returncode}"
        )
    except (ValueError, AttributeError):
        return f"exit {result.returncode}"


def _run_show(
    argv: list[str], value: str, cwd: Path
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=BD_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UnknownBeadsId(f"{value!r}: {argv[0]} failed: {exc}") from exc


def _bd_primary(value: str, bd: str, cwd: Path) -> str:
    """`value`'s hash primary per `bd show --json` in `cwd`; raises UnknownBeadsId otherwise.

    `bd show` takes a hash id only, so a value it cannot find is retried as a display id (the
    long form a CC panel row carries) through the shipped resolver, `bdx <id> --exec bd show`.
    """
    if value.strip().isdigit():
        raise UnknownBeadsId(
            f"{value!r} is a bare number (a CC task panel id?); pass the row's Beads id"
        )
    result = _run_show([bd, "show", value, "--json"], value, cwd)
    if result.returncode != 0:
        detail = f"{value!r}: {_bd_error(result)}"
        bdx = shutil.which("bdx")
        if bdx is None:
            raise UnknownBeadsId(f"{detail}; bdx is not on PATH")
        result = _run_show(
            [bdx, value, "--exec", bd, "show", "{}", "--json"], value, cwd
        )
        if result.returncode != 0:
            raise UnknownBeadsId(f"{detail}; as a display id: {_bd_error(result)}")
    try:
        data = json.loads(result.stdout)
        issue = data[0] if isinstance(data, list) else data
        return str(issue["id"])
    except (ValueError, LookupError, TypeError) as exc:
        raise UnknownBeadsId(
            f"{value!r}: bd show --json gave no issue id ({exc})"
        ) from exc


def resolve_beads_ids(values: list[str], cwd: Path) -> list[str]:
    """Each value's hash primary, resolved in `cwd`, de-duplicated in the order given."""
    bd = shutil.which("bd")
    if bd is None:
        raise UnknownBeadsId(f"{values[0]!r}: bd is not on PATH")
    return list(dict.fromkeys(_bd_primary(value, bd, cwd) for value in values))


def _strip_comment(raw: str) -> str:
    quote = ""
    for index, char in enumerate(raw):
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or raw[index - 1] in " \t"):
            return raw[:index]
    return raw


def _unquote(value: str) -> str:
    if len(value) >= len('""') and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _yaml_value(raw: str) -> Any:
    """One flow-style YAML value as the rider frontmatter writes it: a scalar or a list."""
    value = _strip_comment(raw).strip()
    if value in ("", "null", "~"):
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        items, current, quote, depth = [], "", "", 0
        for char in inner:
            if quote:
                quote = "" if char == quote else quote
            elif char in "\"'":
                quote = char
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
            elif char == "," and depth == 0:
                items.append(current)
                current = ""
                continue
            current += char
        items.append(current)
        return [_unquote(item.strip()) for item in items]
    return _unquote(value)


def rider_fields(text: str) -> tuple[dict[str, Any], str] | None:
    """`(frontmatter, body)` of a rider, or None when it has no frontmatter block."""
    match = FRONTMATTER.match(text)
    if not match:
        return None
    fields: dict[str, Any] = {}
    for line in cm_lines(match.group(1)):
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, value = line.split(":", 1)
            fields[key.strip()] = _yaml_value(value)
    return fields, text[match.end() :]


def _private_id_pattern() -> re.Pattern[str]:
    try:
        manifest = FLEET_PREFIXES.read_text(encoding="utf-8")
    except OSError:
        manifest = ""
    prefixes = sorted(
        set(re.findall(r"\{\s*prefix:\s*([A-Z][A-Z0-9-]*)", manifest)),
        key=len,
        reverse=True,
    )
    if not prefixes:
        return re.compile(r"(?!x)x")
    alternatives = "|".join(re.escape(prefix) for prefix in prefixes)
    return re.compile(rf"\b(?:{alternatives})-[A-Za-z0-9][A-Za-z0-9.]*")


def private_id(text: str) -> str | None:
    """The first private Beads id in `text`, by the fleet prefixes in bd_fleet_prefixes.yaml."""
    found = _private_id_pattern().search(text)
    return found.group(0) if found else None


def _amendment(body: str) -> str | None:
    found = re.search(r"(?ms)^## Amendment[ \t]*\n(.*?)(?=^## |\Z)", body)
    return found.group(1).strip() if found else None


def validate_rider(  # noqa: C901, PLR0912, PLR0913, PLR0915 - one branch per § Validity rule
    text: str,
    path: str,
    template_name: str | None,
    *,
    repo_dir: Path | None = None,
    templates_dir: Path | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one rider (ISSUE-REF § Validity); `path` is its projects-relative name.

    `template_name` is the template being armed, or None to check `amends` only for
    existence. `repo_dir` is where `exclude` ids resolve, the rider's repository by default.
    """
    config = {**rider_settings(), **(settings or {})}
    templates = templates_dir or TEMPLATES_DIR
    findings: list[dict[str, Any]] = []

    def error(check: str, message: str) -> None:
        findings.append(_finding(check, "error", None, message))

    rider_path = Path(path)
    repo = (
        path.split(f"/{config['dir']}/", 1)[0] if f"/{config['dir']}/" in path else ""
    )
    if len(path) > int(config["max_path_chars"]):
        error(
            "path-length",
            f"projects-relative path is {len(path)} characters, over {config['max_path_chars']}",
        )
    named = RIDER_NAME.match(rider_path.name)
    if not named:
        error(
            "file-name",
            f"{rider_path.name!r} is not <YYMMDD>-<slug>.md with a lowercase hyphenated slug",
        )
    else:
        try:
            dt.datetime.strptime(named.group("date"), "%y%m%d")
        except ValueError:
            error("file-name", f"{named.group('date')!r} is not a YYMMDD date")
    if not repo or rider_path.parent.as_posix() != f"{repo}/{config['dir']}":
        error("location", f"a rider lives at <repo>/{config['dir']}/<YYMMDD>-<slug>.md")
    parsed = rider_fields(text)
    if parsed is None:
        error("frontmatter", "the rider has no YAML frontmatter block")
        fields, body = {}, text
    else:
        fields, body = parsed
    missing = [key for key in RIDER_REQUIRED_KEYS if key not in fields]
    if parsed is not None and missing:
        error("frontmatter", "missing required key(s): " + ", ".join(missing))
    if "[FILL" in text:
        error("placeholder", "the rider still carries a [FILL ...] placeholder")
    if "category" in fields and fields["category"] != "goal-rider":
        error("frontmatter", f"category is {fields['category']!r}, not goal-rider")
    if "rider_version" in fields and str(fields["rider_version"]) not in RIDER_VERSIONS:
        error("rider-version", f"rider_version {fields['rider_version']!r} is unknown")
    created = fields.get("created_at")
    if "created_at" in fields and not (
        isinstance(created, str) and UTC_Z.match(created) and _iso_ok(created)
    ):
        error(
            "created-at", f"created_at {created!r} is not a UTC timestamp ending in Z"
        )
    amends = fields.get("amends")
    if "amends" in fields:
        names = amends if isinstance(amends, list) else [amends] if amends else []
        if not names:
            error("amends", "amends names no template")
        unknown = [
            str(name)
            for name in names
            if str(name) != template_name
            and not (templates / f"{name}-goal.md").is_file()
        ]
        if unknown:
            error("amends", "amends names no such template: " + ", ".join(unknown))
        if template_name is not None and template_name not in [str(n) for n in names]:
            error(
                "amends-template",
                f"amends {names} does not name the armed template {template_name!r}",
            )
    amendment = _amendment(body)
    if amendment is None:
        error("amendment", "## Amendment is missing")
    elif not re.search(r"(?m)^\d+\.\s+\S", amendment):
        error("amendment", "## Amendment holds no numbered sentence")
    if len(body) > int(config["max_body_chars"]):
        error(
            "body-length",
            f"the body is {len(body)} characters, over {config['max_body_chars']}",
        )
    excluded = fields.get("exclude")
    if excluded is not None:
        values = excluded if isinstance(excluded, list) else [excluded]
        bare = [str(v) for v in values if str(v).strip().isdigit()]
        if bare:
            error("exclude", f"exclude holds a bare number: {', '.join(bare)}")
        elif values:
            try:
                resolve_beads_ids(
                    [str(v) for v in values], repo_dir or projects_root() / repo
                )
            except (UnknownBeadsId, OSError) as exc:
                error("exclude", f"exclude id does not resolve: {exc}")
    if repo in config["public_repos"]:
        leaked = private_id(text)
        if leaked:
            error(
                "public-private-id",
                f"{repo} is a public repo and the rider carries the private id {leaked}",
            )
    return {
        "path": path,
        "ok": not findings,
        "findings": findings,
        "fields": fields,
    }


def _iso_ok(stamp: str) -> bool:
    try:
        dt.datetime.fromisoformat(stamp)  # 3.11+ reads a trailing Z as UTC
    except ValueError:
        return False
    return True


def _replace_frontmatter(text: str, updates: dict[str, str]) -> str:
    match = FRONTMATTER.match(text)
    if not match:
        raise ValueError("document has no frontmatter")
    lines = cm_lines(match.group(1))
    seen: set[str] = set()
    rendered: list[str] = []
    for line in lines:
        key = line.split(":", 1)[0] if ":" in line else ""
        if key in updates:
            rendered.append(f"{key}: {updates[key]}")
            seen.add(key)
        else:
            rendered.append(line)
    for key, value in updates.items():
        if key not in seen:
            rendered.append(f"{key}: {value}")
    return "---\n" + "\n".join(rendered) + "\n---\n" + text[match.end() :]


def _is_registry_template(path: Path) -> bool:
    """A reviewed template under TEMPLATES_DIR, the set /start-goal digest-checks.

    The maintainer ruled ISSUE-REF (b) on 2026-10-15: the judge cannot reach act mode, so a
    template edit could never be re-stamped and /start-goal refused it. A template
    lands only through a reviewed PR; drafts and every other path keep the judge.
    """
    resolved = path.resolve()
    return resolved.parent == TEMPLATES_DIR.resolve() and resolved.name.endswith(
        "-goal.md"
    )


def write_validation(path: Path, report: dict[str, Any]) -> None:
    original = read_document(path)
    # Import at the mutation boundary to keep deterministic read-only validation
    # import-light and avoid render/validator's module initialization cycle.
    from render import _guard_caller_input, _guard_publication

    _guard_caller_input(original)
    if not report["ok"]:
        return
    if not _is_registry_template(path):
        _guard_publication(original)
    updates = {
        "char_count": json.dumps(report["counts"], separators=(",", ":")),
        "validated_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "validated_digest": "",
        "validator_ruleset": RULESET,
    }
    interim = _replace_frontmatter(original, updates)
    updates["validated_digest"] = normalized_digest(interim)
    final = _replace_frontmatter(original, updates)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as handle:
        handle.write(final)
        handle.flush()
        Path(handle.name).replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a draft-goal document")
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--write", action="store_true", help="write validation metadata when valid"
    )
    parser.add_argument(
        "--count-semantics",
        choices=("max", "scalars", "utf8_bytes", "utf16_units"),
        default="max",
    )
    parser.add_argument(
        "--legacy-compatible",
        action="store_true",
        help="validate one of the three byte-preserved migrated examples",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args(argv)
    text = read_document(args.path)
    if args.write:
        from render import _guard_caller_input

        _guard_caller_input(text)
    report = validate_text(
        text,
        path=str(args.path),
        count_semantics=args.count_semantics,
        legacy_compatibility=args.legacy_compatible,
    )
    if args.write and report["ok"]:
        write_validation(args.path, report)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(
            f"{args.path}: {'ok' if report['ok'] else 'invalid'} ({report['counts']['gating']} gated characters)"
        )
        for finding in report["findings"]:
            print(f"{finding['severity']}: {finding['check']}: {finding['message']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
