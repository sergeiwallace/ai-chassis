"""Caller-side STUB rendering, validation, and recoverable publication."""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
import uuid

from functools import lru_cache
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from line_endings import LINE_ENDING, cm_lines, line_spans


from jsonschema import Draft202012Validator

from compiler import (
    _captured_units,
    _scope_states,
    _scope_tokens,
    capture,
    clause_units,
)
from validate_goal import (
    MAX_CHARACTERS,
    MIN_COVERAGE_CELLS,
    RIDER_PARAM,
    TERMINAL_BRANCHES,
    WARNING_CHARACTERS,
    _condition as document_condition,
    _frontmatter,
    _sections,
    _without_raw_ask,
    raw_ask_text,
    read_document,
    validate_text,
    write_validation,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
STUB_PATH = REPO_ROOT / "docs/prompts/goal/STUB.md"
SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "references/worker-output.schema.json"
)
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Native /goal's evaluator judges only the condition text and answers only ok or impossible,
# so the condition itself must say a met non-success branch ends the goal. It closes the
# condition because the evaluator reads it last, and on Bedrock nothing but this prompt asks
# for JSON: Claude Code drops the schema output format there (ISSUE-REF). An S-item about past
# conduct or order can never come to wait outside the session, so without its clause a session
# that once broke one could never reach BLOCKED and looped until /goal clear (ISSUE-REF). The
# evaluator read a PENDING or owner's-call decision as one the session could still resolve, and
# its built, green branch as actionable, so the list names that decision outright (ISSUE-REF).
# The past-conduct clause is a second accepted state INSIDE the BLOCKED sentence: as a separate
# sentence after the actionable test, the evaluator read it as qualifying that test and still
# required every unproven S-item to wait outside (ISSUE-REF). The evaluator cited its own earlier
# rejections of GOAL BLOCKED as an unmet S-item, a state no later turn can change, so the
# condition says a rejection is never one (ISSUE-REF). It then conceded every outside wait and
# rejected on the count of its own rejections, on no /exit-goal run it had accepted, and on the
# /bug-fix TERMINATION orders after three rejections never having edited the armed text; none of
# those is an S-item, so the sentence names the branch and the /bug-fix too (ISSUE-REF).
# Live, the evaluator read a `waits on: owner` label as an outside wait over a why naming the
# session's own unfinished work, so the outside-wait list says the why decides (ISSUE-REF D-15).
# Two evaluators read a pending chief's go as actionable ("actionable here once the chief's go
# arrives") although the GOAL line counts it, so the list names the go (ISSUE-REF). The armed
# work-all templates sat 6 characters under the 4000 limit, so the entry is paid for by
# dropping "or" before PENDING, "Budget and unsafe" for "Budget/unsafe" and "trigger and
# phrase" for "trigger+phrase" ("named with" is pinned by the why-decides test).
# A third read a gate queued behind a host lock another session held as actionable ("a gate is
# queued, which is actionable here"), so the list names the peer before the chief's go (tests
# pin "outside party" between its neighbours) (ISSUE-REF); paid for by "exits: trigger+phrase
# only." for "exits need only trigger+phrase.".
# A goal drew 33 evaluator rejections in one run, each asking for a step the
# transcript already showed to be done again ("which it could do here", "rest on current-state
# checks this session could still run"); the actionable test turned any repeatable step into a
# bar. The BLOCKED sentence now also accepts an S-item resting only on a redo no S-item asks for,
# inside the sentence beside the past-conduct clause, as ISSUE-REF requires (ISSUE-REF).
STOP_CONDITION = (
    "Any TERMINATION branch stops this goal, not only SUCCESS. BLOCKED is met when GOAL "
    "BLOCKED was said and each unproven S-item waits outside this session (owner, credential, "
    "outside party, unreached date, peer, chief's go, PENDING, owner's-call or "
    "Authority-escalated decision, built too; a task's why, not its waits-on label, "
    "decides), with attempts, or rests only on past conduct or order (like missing "
    "figures) no later turn can change, or on a redo no S-item asks for. "
    "Only an unreached date's scheduled wakeup is waiting, not pending work; "
    "no outcome due; report stays last. An unproven S-item "
    "actionable here means BLOCKED is not met.\nEvaluator rejections and their /bug-fix "
    "bar nothing. No prior or accepted verdict owed. Budget/unsafe/scope exits: trigger+phrase only. "
    "Evaluator: JSON object only."
)


class RenderError(ValueError):
    """Structured worker data or filesystem state is unsafe to publish."""


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            json.dump(data, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _contained(root: Path, candidate: Path) -> Path:
    root_real = root.resolve(strict=False)
    candidate_real = candidate.resolve(strict=False)
    if root_real != candidate_real and root_real not in candidate_real.parents:
        raise RenderError("output path escapes goal-docs root")
    return candidate_real


def _relative_path(path: Path) -> Path:
    if path.is_absolute() or ".." in path.parts:
        raise RenderError("output paths must be root-relative and contain no ..")
    return path


def _safe_target(root: Path, directory: Path, name: str) -> Path:
    if Path(name).name != name or ".." in Path(name).parts:
        raise RenderError("output paths must be root-relative and contain no ..")
    target = directory / name
    _contained(root, target)
    if target.is_symlink():
        raise RenderError("final target must not be a symlink")
    if target.exists() and not target.is_file():
        raise RenderError("existing final target must be a regular file")
    return target


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _schema_path(error) -> str:
    return ".".join(str(part) for part in error.absolute_path) or "<root>"


def _normalized_phrase(value: str) -> str:
    # Word-only folding confuses targets such as /goal with the fixed GOAL
    # terminal-state word. Retain authored punctuation and identifiers here;
    # the shared parsed-block guard remains the authority for full identity.
    return re.sub(r"[ \t\r\n]+", " ", value.casefold()).strip().rstrip(".!?")


def _assert_exclusion_separation(member: dict[str, Any]) -> None:
    """Reject a structured exclusion copied into an affirmative/operational field."""
    scope = member["scope_authority"]
    non_exclusion_values = [
        member["outcome"],
        member["context"],
        scope["in_scope"],
        scope["allowed_actions"],
        scope["human_approval_required_for"],
        member["ground_truth"],
        member["loop_policy"],
        member["final_report"],
        *member["termination"].values(),
        *(
            value
            for success in member["success"]
            for value in success.values()
            if isinstance(value, str)
        ),
    ]
    normalized_values = [_normalized_phrase(value) for value in non_exclusion_values]
    # Walk-back may preserve an affirmative instruction as context. Its exact
    # source identity can also project into scope; the authority guard separately
    # refuses ANY positive prohibition verdict in an operational field.
    antecedents = {
        _normalized_phrase(block)
        for unit in _captured_units(member["raw_ask"])
        for block in unit.antecedent_blocks
    }
    for exclusion in scope["exclusions"]:
        normalized = _normalized_phrase(exclusion)
        if normalized in antecedents:
            continue
        if normalized and any(normalized in value for value in normalized_values):
            raise RenderError(
                "worker output schema: exclusion appears outside scope_authority.exclusions"
            )


def validate_worker_output(  # noqa: C901, PLR0912
    worker: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate the complete D-9 object before any filesystem mutation."""
    if not isinstance(worker, dict):
        raise RenderError("worker output schema: root must be an object")
    errors = sorted(
        Draft202012Validator(_schema()).iter_errors(worker),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        rendered = "; ".join(
            f"{_schema_path(error)}: {error.message}" for error in errors[:5]
        )
        raise RenderError(f"worker output schema: {rendered}")
    members = worker["members"]
    fit_class = worker["fit_class"]
    if worker["questions"]:
        if members or worker["rejection"] is not None:
            raise RenderError(
                "worker output schema: questions cannot accompany members or rejection"
            )
        return members
    if fit_class in {"one-shot", "not-autonomous-fit"}:
        if worker["rejection"] is None or members:
            raise RenderError(
                "worker output schema: rejected fit requires rejection and no members"
            )
        return members
    if worker["rejection"] is not None or not members:
        raise RenderError(
            "worker output schema: goal fit requires members and no rejection"
        )
    slugs = [member["slug"] for member in members]
    # The schema's `$` also matches before a trailing newline; the slug names files.
    if not all(SLUG.fullmatch(slug) for slug in slugs):
        raise RenderError("worker output schema: member slug must be a bare kebab name")
    if len(set(slugs)) != len(slugs):
        raise RenderError("member slugs must be unique")
    known = set(slugs)
    for member in members:
        _assert_exclusion_separation(member)
        if "\n" in member["outcome"]:
            raise RenderError(
                "check=worker-schema severity=error: member outcome must be one line"
            )
        dependencies = set(member["depends_on"])
        if member["slug"] in dependencies or not dependencies <= known:
            raise RenderError("depends_on must reference other member slugs")
        if member["goal_kind"] in {"loop", "goal+loop"}:
            if member["cadence_seconds"] is None:
                raise RenderError("loop members require cadence_seconds")
        elif member["cadence_seconds"] is not None:
            raise RenderError("goal members must not carry cadence_seconds")
        ids = [item["id"] for item in member["success"]]
        expected = [f"S{index}" for index in range(1, len(ids) + 1)]
        if ids != expected:
            raise RenderError("success ids must be unique and contiguous from S1")
        placements: dict[str, set[str]] = {}
        for row in member["coverage"]:
            normalized = re.sub(r"\W+", " ", row["element"].casefold()).strip()
            placements.setdefault(normalized, set()).add(row["placement"].casefold())
        for element, values in placements.items():
            excluded = any(
                "excluded" in value or "out of scope" in value for value in values
            )
            placed = any(
                "unplaced" not in value
                and "excluded" not in value
                and "out of scope" not in value
                for value in values
            )
            if excluded and placed:
                raise RenderError(
                    f"worker output schema: coverage contradicts scope for {element}"
                )
    return members


def _condition(member: dict[str, Any]) -> str:
    success = "\n".join(
        f"{item['id']}. {item['predicate']} verify: {item['verify']}; "
        f"evidence: {item['evidence']}; authority: {item['authority']}."
        for item in member["success"]
    )
    termination = member["termination"]
    scope = member["scope_authority"]
    in_scope = scope["in_scope"].rstrip(".!?")
    # Schema exclusions are array items. Serialize their boundaries as Markdown
    # list items, so semicolons in prose, entities, code and links remain data.
    exclusions = "\n".join(
        "- " + "\n  ".join(cm_lines(exclusion)) for exclusion in scope["exclusions"]
    )
    allowed_actions = scope["allowed_actions"].rstrip(".!?")
    approval = scope["human_approval_required_for"].rstrip(".!?")
    scope_authority = (
        f"In scope: {in_scope}.\nOut of scope:\n{exclusions}\n\n"
        f"You may: {allowed_actions}. Human approval required for: {approval}."
        # The reserved slot /start-goal fills with an armed rider, empty without one.
        f"{{{{{RIDER_PARAM}}}}}"
    )
    return f"""GOAL
{member["outcome"]}

CONTEXT
{member["context"]}

SCOPE & AUTHORITY
{scope_authority}

GROUND TRUTH
{member["ground_truth"]}

SUCCESS — ALL MUST BE TRUE
{success}

LOOP POLICY
{member["loop_policy"]}

TERMINATION
SUCCESS: {termination["success"]}
BLOCKED: {termination["blocked"]}
BUDGET_EXHAUSTED: {termination["budget_exhausted"]}
UNSAFE_OR_OUT_OF_SCOPE: {termination["unsafe_or_out_of_scope"]}

FINAL REPORT
{member["final_report"]}

STOP CONDITION
{STOP_CONDITION}"""


def _replace_frontmatter_scalar(text: str, key: str, value: str) -> str:
    replaced, count = re.subn(rf"(?m)^{re.escape(key)}:.*$", f"{key}: {value}", text)
    if count != 1:
        raise RenderError(f"canonical STUB is missing frontmatter field {key}")
    return replaced


def _launch(member: dict[str, Any]) -> str:
    if member["goal_kind"] == "goal":
        return "`/goal <paste the fenced condition>`"
    return f"`/loop {member['cadence_seconds']}s /goal <paste the fenced condition>`"


def _table_cell(value: str) -> str:
    # Escape authored HTML before encoding physical lines as table framing.
    # Decode that framing before classification, so multiline projections keep
    # the same units as the source. Literal <br> and entities stay distinct.
    value = LINE_ENDING.sub("<br>", html.escape(value, quote=False))
    return value.replace("|", "\\|")


def _decode_table_cell(value: str) -> str:
    value = re.sub(r"\\\|", "|", value)
    return html.unescape(re.sub(r"<br>", "\n", value))


_COVERAGE_HEADER = "| Raw element | Placed in |\n|---|---|\n"


def _fill_section(
    rendered: str, heading: str, body: str, *, keep: str = "", verbatim: bool = False
) -> tuple[str, int]:
    """Replace the body of the STUB's ``## <heading>`` section, wherever it sits.

    A section runs from its heading to the next ``## `` heading or the end of the
    document, so the fill does not depend on section order (the goal kind puts
    ``## Launch`` first, directly under the title). ``keep`` is a prefix the STUB's
    body must open with and the fill preserves, such as the coverage table header.
    """
    pattern = re.compile(
        rf"(?ms)^## {re.escape(heading)}\n\n{re.escape(keep)}.*?(?=^## |\Z)"
    )
    return pattern.subn(
        lambda _: f"## {heading}\n\n{keep}{body if verbatim else body.rstrip()}\n\n",
        rendered,
    )


def _member_document(member: dict[str, Any], manifest: str | None = None) -> str:
    """Fill the canonical STUB; no second document template exists here."""
    rendered = STUB_PATH.read_text(encoding="utf-8")
    rendered = rendered.replace("[FILL title]", member["slug"])
    for key, value in (
        ("goal_kind", member["goal_kind"]),
        ("autonomy", member["autonomy"]),
        (
            "cadence_seconds",
            "null"
            if member["cadence_seconds"] is None
            else str(member["cadence_seconds"]),
        ),
        ("budget", json.dumps(member["budget"], separators=(",", ":"))),
        ("manifest", manifest or "null"),
    ):
        rendered = _replace_frontmatter_scalar(rendered, key, value)
    coverage = "\n".join(
        f"| {_table_cell(row['element'])} | {_table_cell(row['placement'])} |"
        for row in member["coverage"]
    )
    # The verbatim ask is filled last: it is the one body a human wrote, so a heading
    # inside it must not be read as a STUB section by a later fill.
    rendered, coverage_count = _fill_section(
        rendered, "Coverage", coverage, keep=_COVERAGE_HEADER
    )
    rendered, launch_count = _fill_section(
        rendered,
        "Launch",
        f"{_launch(member)}\n\n```text\n{_condition(member)}\n```",
    )
    rendered, raw_count = _fill_section(
        rendered, "Raw ask", member["raw_ask"], verbatim=True
    )
    if (raw_count, coverage_count, launch_count) != (1, 1, 1):
        raise RenderError("canonical STUB body contract is incomplete")
    # Store framing outside the verbatim body. Character counts preserve Unicode
    # and trailing whitespace without reserving any marker string inside an ask.
    rendered = rendered.replace(
        "\n---\n", f"\nraw_ask_chars: {len(member['raw_ask'])}\n---\n", 1
    )
    # Caller-owned input survives stamping/replay independently of the editable
    # Raw ask section. Base64 preserves CRLF, whitespace and Unicode bytes.
    binding = base64.b64encode(member["raw_ask"].encode("utf-8")).decode("ascii")
    rendered = rendered.replace("\n---\n", f"\ncaller_raw_ask_b64: {binding}\n---\n", 1)
    rendered = rendered.replace(
        "\n---\n", f"\ndepends_on: {json.dumps(member['depends_on'])}\n---\n", 1
    )
    if "[FILL" in rendered:
        raise RenderError("canonical STUB render left a placeholder")
    return rendered


def _manifest_document(members: list[dict[str, Any]]) -> str:
    rows = "\n".join(
        f"  - path: {member['slug']}-goal.md\n"
        f"    outcome: {json.dumps(member['outcome'])}\n"
        f"    depends_on: [{', '.join(dependency + '-goal.md' for dependency in member['depends_on'])}]\n"
        "    status: validated"
        for member in members
    )
    rendered = (
        f"---\nask_ref: {members[0]['slug']}-goal.md#raw-ask\nmembers:\n{rows}\n---\n"
    )
    if rendered.count("  - path:") != len(members):
        raise RenderError("manifest validation failed")
    return rendered


def _journal_path(root: Path, batch_id: str) -> Path:
    return root / f".draft-goal-publish-{batch_id}.journal"


def _staging_directory(data: dict[str, Any]) -> Path | None:
    if data.get("staging_dir"):
        candidate = Path(data["staging_dir"])
        return candidate if candidate.name.startswith(".draft-goal-stage-") else None
    members = data.get("members", [])
    candidate = Path(members[0]["staging"]).parent if members else None
    if candidate and candidate.name.startswith(".draft-goal-stage-"):
        return candidate
    return None


def _validate_journal_paths(root: Path, data: dict[str, Any]) -> None:
    staging_dir = _staging_directory(data)
    if staging_dir is None:
        raise RenderError("publication journal has no safe staging directory")
    _contained(root, staging_dir)
    for item in data.get("members", []):
        for key in ("final", "staging", "backup"):
            if item.get(key):
                _contained(root, Path(item[key]))


def _remove_staging_directory(root: Path, staging_dir: Path) -> None:
    if staging_dir.is_symlink():
        staging_dir.unlink()
    else:
        _contained(root, staging_dir)
        if staging_dir.resolve(strict=False) == root.resolve(strict=False):
            raise RenderError("journal staging directory cannot be the goal-docs root")
        shutil.rmtree(staging_dir, ignore_errors=True)


def recover(root: Path) -> None:  # noqa: C901, PLR0912
    """Finish a committed batch or roll back a preparing batch before a new run."""
    if not root.exists():
        return
    for journal in root.glob(".draft-goal-publish-*.journal"):
        data = json.loads(journal.read_text(encoding="utf-8"))
        _validate_journal_paths(root, data)
        if data["state"] == "preparing":
            for item in data["members"]:
                final, staging = Path(item["final"]), Path(item["staging"])
                backup = Path(item["backup"]) if item.get("backup") else None
                if backup and backup.exists():
                    os.replace(backup, final)
                    _fsync_directory(final.parent)
                elif final.exists() and item.get("published_new"):
                    final.unlink()
                    _fsync_directory(final.parent)
                if staging.exists():
                    staging.unlink()
            journal.unlink()
        elif data["state"] == "committed":
            _publish_records(data["members"])
            for item in data["members"]:
                if item.get("backup"):
                    Path(item["backup"]).unlink(missing_ok=True)
            journal.unlink()
        else:
            raise RenderError("unknown publication journal state")
        _fsync_directory(root)
        staging_dir = _staging_directory(data)
        if staging_dir:
            _remove_staging_directory(root, staging_dir)
    for orphan in root.glob(".draft-goal-stage-*"):
        _remove_staging_directory(root, orphan)


def reader_state(root: Path) -> str:
    """Return visible, unavailable, or hidden for the current publication state."""
    journals = list(root.glob(".draft-goal-publish-*.journal")) if root.exists() else []
    if not journals:
        return "visible"
    for journal in journals:
        data = json.loads(journal.read_text(encoding="utf-8"))
        if data["state"] == "preparing":
            return "hidden"
        if any(
            not Path(item["final"]).exists()
            or _digest(Path(item["final"])) != item["digest"]
            for item in data["members"]
        ):
            return "unavailable"
    return "visible"


def _journal_data(
    batch_id: str, state: str, staging_dir: Path, records: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "state": state,
        "staging_dir": str(staging_dir),
        "members": records,
    }


def _authored_fields(value, schema, path="", role="operational"):
    """Walk schema-defined authored strings, including future nested fields.

    Only provenance/source fields and the explicit exclusions list may carry
    prohibitions. An unannotated new string field defaults to operational.
    """
    # The same registry applies to a rendered slot and its worker projection.
    # Member schema validation precedes this traversal; an identifier with
    # authored prose is a typed integrity refusal, never unjudged instructions.
    if (
        schema.get("x-generated-document-field")
        in _schema()["x-generated-document-fields"]
    ):
        return
    role = schema.get("x-prohibition-role", role)
    if isinstance(value, str):
        yield path, value, role
    elif isinstance(value, dict):
        for key, item in value.items():
            child = schema.get("properties", {}).get(key, {})
            yield from _authored_fields(item, child, f"{path}.{key}".lstrip("."), role)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _authored_fields(
                item, schema.get("items", {}), f"{path}.{index}", role
            )


def _condition_regions(condition, schema):
    """Visit every region, retaining repeated headers and unknown text.

    Sections come from the schema's render bindings. No section dictionary may
    overwrite earlier authored bytes. STOP CONDITION is fixed caller scaffolding;
    only its exact canonical bytes get that treatment.
    """
    headers = {
        node["x-condition-section"]
        for node in schema["properties"].values()
        if "x-condition-section" in node
    } | {"STOP CONDITION"}
    section = "condition"
    lines = []
    for line in [*cm_lines(condition), ""]:
        header = next(
            (name for name in headers if line == name or line.startswith(name + " ")),
            None,
        )
        if header:
            if lines:
                yield section, "\n".join(lines)
            section, lines = header, []
            qualifier = line[len(header) :].strip()
            if qualifier and qualifier != "— ALL MUST BE TRUE":
                yield header + ".heading", qualifier
        else:
            lines.append(line)
    if lines:
        yield section, "\n".join(lines)


def _condition_fields(condition, schema):
    for section, value in _condition_regions(condition, schema):
        yield from _region_fields(section, value)


@lru_cache(maxsize=1)
def _fixed_safety_lines():
    """Template schema's fixed termination/stop slots, read from templates and STUB.

    Positions count nonblank lines in the region. No spelling is duplicated in
    this guard; edits or a move to another region lose the positional exception.
    """
    registry = set()
    # Inline drafts use the canonical STUB's full unsafe branch; compact reusable
    # templates can spell that branch differently. Both are schema-owned bytes.
    for path in (
        STUB_PATH,
        *(REPO_ROOT / "docs/prompts/goal/templates").glob("*-goal.md"),
    ):
        condition, _ = document_condition(read_document(path))
        schema = _schema()["properties"]["members"]["items"]
        for section, value in _condition_regions(condition, schema):
            # The STUB's other regions are authoring parameters, not fixed safety
            # scaffolding. Keep their bytes subject to ordinary authored checks.
            if path == STUB_PATH and section not in {"TERMINATION", "STOP CONDITION"}:
                continue
            for position, line in enumerate(
                line for line in cm_lines(value) if line.strip()
            ):
                registry.add((section, position, line))
    return registry


@lru_cache(maxsize=1)
def _fixed_template_registry():
    schema = _schema()["properties"]["members"]["items"]
    registry = {}
    for path in (REPO_ROOT / "docs/prompts/goal/templates").glob("*-goal.md"):
        text = read_document(path)
        fields, _ = _frontmatter(text)
        registry[fields["title"]] = (
            raw_ask_text(text),
            list(_document_fields(text, schema)),
            text,
        )
    return registry


def _template_contract(text):
    # Stamps may change header metadata. Every fixed body byte, including table
    # framing/entities and whitespace, must stay verbatim; decoded field equality
    # cannot confer the whole-source template exemption.
    _, (_, end) = _frontmatter(text)
    return text[end:]


def _publication_fields(text, schema):
    """Only verbatim template fields in the same schema slot are scaffolding."""
    fields, _ = _frontmatter(text)
    reference = _fixed_template_registry().get(fields.get("title"))
    actual = list(_document_fields(text, schema))
    for index, (field, source, role) in enumerate(actual):
        if reference and index < len(reference[1]):
            fixed_field, fixed_source, _ = reference[1][index]
            if field == fixed_field and source == fixed_source:
                role = "generated"
        yield field, source, role


def _scope_labels(value):
    # The schema's region labels are top-level paragraph line prefixes.
    # Inline code/link/emphasis text and list-item contents cannot delimit
    # the enclosing region, even if they spell the same label.
    spans = line_spans(value)
    labels = []
    for token in _scope_tokens(value):
        if token.type != "inline" or token.level != 1:
            continue
        for offset, line in enumerate(cm_lines(token.content)):
            match = re.match(
                r"(?i)^(?:In scope|Out of scope|You may|Human approval required for):",
                line,
            )
            if match:
                # Incumbent one-line scope grammar is a plain-text labelled
                # field sequence. Inline markup never opens another field.
                matches = [match]
                if all(
                    child.type in {"text", "softbreak"}
                    for child in token.children or ()
                ):
                    matches.extend(
                        re.finditer(
                            r"(?i)(?<=[.;!?])\s+(?:Out of scope|You may|Human approval required for):",
                            line[match.end() :],
                        )
                    )
                start = spans[token.map[0] + offset][0]
                prefix = value[start : spans[token.map[0] + offset][1]].find(line)
                labels.append(
                    (
                        start + max(prefix, 0),
                        start + max(prefix, 0) + match.end(),
                        match[0],
                    )
                )
                for following in matches[1:]:
                    leading = len(following[0]) - len(following[0].lstrip())
                    labels.append(
                        (
                            start
                            + max(prefix, 0)
                            + match.end()
                            + following.start()
                            + leading,
                            start + max(prefix, 0) + match.end() + following.end(),
                            following[0].strip(),
                        )
                    )
    return labels


def _region_fields(section, value):
    if section == "SCOPE & AUTHORITY":
        labels = _scope_labels(value)
        if labels:
            yield section + ".prefix", value[: labels[0][0]], "operational"
        for index, label in enumerate(labels):
            end = labels[index + 1][0] if index + 1 < len(labels) else len(value)
            role = (
                "exclusion" if label[2].casefold() == "out of scope:" else "operational"
            )
            yield section + "." + label[2], value[label[1] : end], role
        if not labels:
            yield section, value, "operational"
    elif section == "TERMINATION":
        position = 0
        for line in cm_lines(value):
            # Match the validator's branch grammar at the start of the line.
            # Unknown labels, prefixed labels and quoted/code colons are authored
            # text: trimming them can erase a prohibition before it is judged.
            body = (
                line.partition(":")[2]
                if line.startswith(tuple(branch + ":" for branch in TERMINAL_BRANCHES))
                else line
            )
            role = (
                "generated"
                if (section, position, line) in _fixed_safety_lines()
                else "operational"
            )
            if line.strip():
                position += 1
            yield section, body if body else line, role
    elif section == "STOP CONDITION":
        # Two lines since ISSUE-REF: each must be its own template line, in place.
        lines = [line for line in cm_lines(value) if line.strip()]
        fixed = _fixed_safety_lines()
        role = (
            "generated"
            if lines
            and all((section, index, line) in fixed for index, line in enumerate(lines))
            else "operational"
        )
        yield section, value, role
    else:
        role = (
            "generated"
            if (section, 0, value.strip()) in _fixed_safety_lines()
            else "operational"
        )
        yield section, value, role


def _without_generated_fields(text):
    """Schema-listed machine fields have integrity checks, never prose verdicts.

    Preserve character positions and physical line endings while masking only
    registered frontmatter slots. Unknown metadata remains authored text.
    """
    fields, span = _frontmatter(text)
    registered = _schema()["x-generated-document-fields"]
    for name in fields.keys() & registered.keys():
        if "\x00" in fields[name]:
            raise RenderError(
                f"check=generated-field severity=error: NUL in machine field {name}"
            )
    if "depends_on" in fields:
        try:
            identifiers = json.loads(fields["depends_on"])
        except ValueError as exc:
            raise RenderError(
                "check=generated-field severity=error: unreadable dependency ids"
            ) from exc
        schema = _schema()["properties"]["members"]["items"]["properties"]["depends_on"]
        if next(Draft202012Validator(schema).iter_errors(identifiers), None):
            raise RenderError(
                "check=generated-field severity=error: invalid dependency ids"
            )
    start, end = span
    header = text[start:end]
    masked = []
    generated = False
    boundaries = [
        0,
        *(match.end() for match in LINE_ENDING.finditer(header)),
        len(header),
    ]
    for left, right in zip(boundaries, boundaries[1:], strict=False):
        line = header[left:right]
        if line and line[0] not in " \t\r\n":
            generated = line.partition(":")[0] in registered
        if generated and "\x00" in line:
            raise RenderError(
                "check=generated-field severity=error: NUL in machine field continuation"
            )
        masked.append(re.sub(r"[^\r\n]", " ", line) if generated else line)
    return text[:start] + "".join(masked) + text[end:]


def _document_fields(text, schema):
    """Classify actual document bytes even on handwritten/template/replay routes."""
    condition, _ = document_condition(text)
    yield from _condition_fields(condition, schema)
    remainder = _without_generated_fields(_without_raw_ask(text))
    if condition:
        remainder = remainder.replace(condition, "", 1)
    for _, field in _remainder_fields(remainder):
        yield field


def _remainder_fields(remainder):
    # Coverage elements quote the ask; placements are instructions, not exclusions.
    # Remove only consumed table rows, so extra text/sections remain operational.
    section = "document"
    fence = None
    comment = False
    for span in line_spans(remainder):
        line = remainder[slice(*span)]
        delimiter = re.match(r"^(`{3,}|~{3,})", line)
        if delimiter:
            if fence is None:
                fence = delimiter[1]
            elif line.strip() == fence:
                fence = None
        if not fence and not comment and line.startswith("## "):
            section = line.removeprefix("## ").strip()
        if section == "Coverage" and not fence and not comment and line.startswith("|"):
            parts = re.split(r"(?<!\\)\|", line)
            cells = parts[1:-1]
            if len(cells) == MIN_COVERAGE_CELLS:
                yield (
                    span,
                    (
                        "coverage.element",
                        _decode_table_cell(cells[0].strip()),
                        "source",
                    ),
                )
                yield (
                    span,
                    (
                        "coverage.placement",
                        _decode_table_cell(cells[1].strip()),
                        "operational",
                    ),
                )
                # Only the table cells have consumed roles. Authored bytes after
                # the closing pipe must still be judged as operational text.
                yield (
                    (span[1] - len(parts[-1]), span[1]),
                    ("coverage.suffix", parts[-1], "operational"),
                )
                comment = (comment or "<!--" in line) and "-->" not in line
                continue
        yield span, ("document", line, "operational")
        comment = (comment or "<!--" in line) and "-->" not in line


def _preflight_document(text, *, fixed_template=False):
    """Parse original semantic blocks before any field/line projections.

    The launch fence is transport: parse its condition as Markdown, rather than
    as one code block. Raw ask framing is independently bound and parsed intact.
    The outer document keeps its paragraph boundaries, including Notes fields.
    This same preflight runs on stamping, publication and committed recovery.
    """
    condition, _ = document_condition(text)
    remainder = _without_generated_fields(_without_raw_ask(text))
    if condition:
        position = remainder.find(condition)
        remainder = remainder[:position] + remainder[position + len(condition) :]
    transported = []
    masked = list(remainder)
    for (start, end), (field, source, _) in _remainder_fields(remainder):
        if field in {"coverage.element", "coverage.placement"}:
            transported.append(source)
            masked[start:end] = " " * (end - start)
        elif field == "coverage.suffix":
            masked[start:end] = source
    remainder = "".join(masked)
    return [
        unit
        for source in (
            (condition,)
            if fixed_template
            else (raw_ask_text(text), condition, remainder, *transported)
        )
        if source.strip()
        for unit in clause_units(source)
    ]


def _guard_publication(  # noqa: C901
    text: str, *, raw_ask: str | None = None, member: dict[str, Any] | None = None
) -> None:
    """Recheck actual document scope at publication, regardless of its author.

    Recovery uses the same boundary: a previous act result is not authority to
    publish after the machine switch or activation evidence has changed.
    """
    _guard_caller_input(text, raw_ask=raw_ask, member=member)
    schema = _schema()["properties"]["members"]["items"]
    fixed = _fixed_template_registry().get(_frontmatter(text)[0].get("title"))
    fixed_template = bool(fixed and raw_ask_text(text) == fixed[0])
    if fixed_template and _template_contract(text) != _template_contract(fixed[2]):
        raise RenderError(
            "check=prohibition-field severity=error: fixed template regions differ; restore the canonical contract or author a new goal."
        )
    document_units = _preflight_document(text, fixed_template=fixed_template)
    raw = raw_ask_text(text)
    condition, _ = document_condition(text)
    sections = _sections(condition)
    scope = sections.get("SCOPE & AUTHORITY", ("", 0))[0]
    sources = [("raw_ask", raw, "source")]
    if raw_ask is not None:
        sources.insert(0, ("original_ask", raw_ask, "source"))
    if member is not None:
        if next(Draft202012Validator(schema).iter_errors(member), None):
            raise RenderError(
                "check=worker-schema severity=error: invalid journal member"
            )
        sources.extend(_authored_fields(member, schema))
    if fixed_template:
        sources = [
            (
                field,
                source,
                "generated"
                if field in {"raw_ask", "original_ask"} and source == fixed[0]
                else role,
            )
            for field, source, role in sources
        ]
    sources.extend(_publication_fields(text, schema))
    sources = list(dict.fromkeys(sources))
    # Preflight every authored field before the first judge call: a late invalid
    # container cannot waste earlier calls or consume partially parsed authority.
    for field, source, role in sources:
        if source.strip() and not (
            role == "generated" and field in {"raw_ask", "original_ask"}
        ):
            _scope_states(source)
    classified = []
    for field, source, role in sources:
        if not source.strip() or (
            role == "generated" and field in {"raw_ask", "original_ask"}
        ):
            continue
        captured = capture(
            "Out of scope:\n\n" + source if role == "exclusion" else source,
            context_units=document_units
            if field not in {"raw_ask", "original_ask"}
            else (),
        )
        if captured["findings"]:
            raise RenderError(
                " ".join(
                    f"check={finding['check']} severity={finding['severity']}: {finding['message']}"
                    for finding in captured["findings"]
                )
            )
        # The structured worker unsafe branch shares the template's branch slot.
        branch = field.removeprefix("termination.").upper()
        if (
            field.startswith("termination.")
            and branch in TERMINAL_BRANCHES
            and any(
                section == "TERMINATION"
                and position == TERMINAL_BRANCHES.index(branch)
                and line == branch + ": " + source
                for section, position, line in _fixed_safety_lines()
            )
        ):
            role = "generated"
        classified.append((field, role, captured))
    _assert_prohibition_placement(classified, scope)


def _assert_caller_fields(value, schema, original, path="", role="operational"):
    """Schema traversal makes every new source restatement default to verbatim.

    Coverage elements are classified projections, not a restatement of the whole
    ask. Their explicit annotation is the only exemption from whole-input equality.
    """
    role = schema.get("x-prohibition-role", role)
    if isinstance(value, str) and role == "source":
        binding = schema.get("x-caller-input", "verbatim")
        if binding == "projection":
            return
        if binding != "verbatim" or value.encode("utf-8") != original.encode("utf-8"):
            raise RenderError(
                f"check=raw-ask-mismatch severity=error: {path} differs from caller input; restore the original bytes and rerun."
            )
    elif isinstance(value, dict):
        for key, child in value.items():
            _assert_caller_fields(
                child,
                schema.get("properties", {}).get(key, {}),
                original,
                f"{path}.{key}".lstrip("."),
                role,
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_caller_fields(
                child, schema.get("items", {}), original, f"{path}.{index}", role
            )


def _guard_caller_input(text, *, raw_ask=None, member=None):
    """Refuse rewritten source bytes before any classification or mutation."""
    raw = raw_ask_text(text)
    fields, _ = _frontmatter(text)
    bound = None
    if "caller_raw_ask_b64" in fields:
        try:
            bound = base64.b64decode(
                fields["caller_raw_ask_b64"], validate=True
            ).decode("utf-8")
        except (binascii.Error, UnicodeError) as exc:
            raise RenderError(
                "check=raw-ask-binding severity=error: unreadable caller input binding"
            ) from exc
    # A legacy, caller-authored template has only its stored Raw ask; preserve
    # that API. New rendered documents must retain their independent binding.
    if bound is None and "raw_ask_chars" in fields:
        raise RenderError(
            "check=raw-ask-binding severity=error: missing caller input binding; rerender from the original ask."
        )
    original = raw_ask if raw_ask is not None else bound if bound is not None else raw
    if raw.encode("utf-8") != original.encode("utf-8") or (
        bound is not None and bound.encode("utf-8") != original.encode("utf-8")
    ):
        raise RenderError(
            "check=raw-ask-mismatch severity=error: rendered raw_ask differs from caller input; restore the original bytes and rerun."
        )
    if member is not None:
        _assert_caller_fields(
            member, _schema()["properties"]["members"]["items"], original
        )


def _exclusion_items(value):
    # Boundaries and framing are defined by the same clause function that builds
    # requests. In particular, semicolons are never document item delimiters.
    return [
        unit.text
        for unit in clause_units(value)
        if unit.text and unit.kind != "unparsed"
    ]


def _exclusion_blocks(value):
    units = clause_units(value)
    return list(
        {
            unit.block_span: unit.block_text
            for unit in units
            if unit.text and unit.kind != "unparsed"
        }.values()
    )


def _assert_prohibition_placement(classified, scope):  # noqa: C901
    # Authority and every failure guard above precede use of any polarity. A
    # worker may not discard an original prohibition or put a newly authored
    # prohibition in an affirmative field. Reworded exclusions need confirmation
    # when their preservation cannot be verified from the document's own bytes.
    # Compare exact shared units, never concatenated text or semicolon segments.
    # The schema's exclusion array is rendered as actual Markdown list items.
    excluded = Counter(
        item
        for _, source, role in _region_fields("SCOPE & AUTHORITY", scope)
        if role == "exclusion"
        for item in _exclusion_items(source)
    )
    source_blocks = {
        block
        for field, role, captured in classified
        if field in {"raw_ask", "original_ask"} and role != "generated"
        for block in captured.get("exclusion_blocks", [])
    }
    source_clauses = {
        clause
        for field, role, captured in classified
        if field in {"raw_ask", "original_ask"} and role != "generated"
        for clause in captured["exclusions"]
    }
    excluded_blocks = Counter(
        block
        for _, source, role in _region_fields("SCOPE & AUTHORITY", scope)
        if role == "exclusion"
        for block in _exclusion_blocks(source)
    )
    if any(excluded_blocks[block] != 1 for block in source_blocks):
        raise RenderError(
            "check=prohibition-scope severity=error: complete prohibition block is not preserved in Out of scope"
        )
    for field, role, captured in classified:
        if role == "generated":
            continue
        if "exclusion_blocks" in captured and role != "operational":
            # Coverage is a clause projection of the original ask. Its source
            # blocks are checked above, so fragments cannot replace those blocks.
            projected = field.startswith("coverage") and all(
                clause in source_clauses for clause in captured["exclusions"]
            )
            if not projected and any(
                excluded_blocks[block] != 1 for block in captured["exclusion_blocks"]
            ):
                raise RenderError(
                    "check=prohibition-scope severity=error: complete prohibition block is not preserved in Out of scope"
                )
            continue
        identities = captured.get("exclusion_units")
        for index, clause in enumerate(captured["exclusions"]):
            if role == "generated":
                continue
            if role == "operational":
                check = (
                    "prohibition-scope"
                    if field.startswith("SCOPE & AUTHORITY")
                    or field.startswith("scope_authority")
                    else "prohibition-field"
                )
                raise RenderError(
                    f"check={check} severity=error: classified prohibition in non-exclusion field {field}; move it to Out of scope and rerun."
                )
            if identities is None:  # Legacy in-memory caller; same unit function.
                items = _exclusion_blocks(clause)
                excluded = excluded_blocks
            else:
                items = [identities[index]] if identities[index] else []
            if (not items and any(u.text for u in clause_units(clause))) or any(
                excluded[item] != 1 for item in items
            ):
                raise RenderError(
                    "check=prohibition-scope severity=error: classified prohibition is not "
                    "preserved in Out of scope; confirm the exclusion and rerun."
                )


def _publish_records(
    records: list[dict[str, Any]],
    *,
    journal: Path | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    """Guard the complete batch before the first document or manifest rename."""
    pending = []
    documents = []
    for record in records:
        final, staged = Path(record["final"]), Path(record["staging"])
        current = final.exists() and _digest(final) == record["digest"]
        source = final if current else staged
        if not source.exists():
            raise RenderError(f"committed journal cannot recover {final.name}")
        # A journal filename is not proof that its bytes are a manifest rather
        # than a goal. Guard every record, including replayed/renamed documents.
        text = read_document(source)
        _guard_caller_input(
            text, raw_ask=record.get("raw_ask"), member=record.get("member")
        )
        documents.append((text, record))
        if not current:
            pending.append((staged, final))
    # Preflight the entire batch's caller bytes before wasting a judge call on
    # an earlier member, then apply authority guards before the first rename.
    for text, record in documents:
        _guard_publication(
            text,
            raw_ask=record.get("raw_ask"),
            member=record.get("member"),
        )
    if journal is not None:
        assert data is not None
        _atomic_json(journal, data)
        for record in records:
            if not record["backup"]:
                continue
            final, backup = Path(record["final"]), Path(record["backup"])
            if final.exists() and not backup.exists():
                os.replace(final, backup)
                _fsync_directory(final.parent)
        data["state"] = "committed"
        _atomic_json(journal, data)
    for staged, final in pending:
        os.replace(staged, final)
        _fsync_directory(final.parent)


def render_and_publish(  # noqa: C901, PLR0912, PLR0913, PLR0915
    worker: dict[str, Any],
    root: Path,
    *,
    output_dir: Path = Path("."),
    force: bool = False,
    validator_settings: Mapping[str, Any] | None = None,
    raw_ask: str | None = None,
) -> list[Path]:
    """Render all members from STUB, validate, then publish recoverably."""
    members = validate_worker_output(worker)
    if not members:
        raise RenderError("worker output contains no publishable members")
    for member in members:
        _assert_caller_fields(
            member,
            _schema()["properties"]["members"]["items"],
            raw_ask if raw_ask is not None else member["raw_ask"],
        )
    output_dir = _relative_path(output_dir)
    if root.is_symlink():
        raise RenderError("goal-docs root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    destination = _contained(root, root / output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    destination = destination.resolve()
    recover(destination)
    batch_id = uuid.uuid4().hex
    staging_dir = destination / f".draft-goal-stage-{batch_id}"
    staging_dir.mkdir()
    records: list[dict[str, Any]] = []
    validator_settings = validator_settings or {}
    try:
        manifest_name = (
            f"{members[0]['slug']}-manifest.md" if len(members) > 1 else None
        )
        for member in members:
            final = _safe_target(root, destination, f"{member['slug']}-goal.md")
            if final.exists() and not force:
                raise RenderError(f"target exists: {final.name}")
            staged = staging_dir / final.name
            text = _member_document(member, manifest_name)
            _guard_caller_input(
                text,
                raw_ask=raw_ask if raw_ask is not None else member["raw_ask"],
                member=member,
            )
            staged.write_text(text, encoding="utf-8", newline="")
            report = validate_text(
                read_document(staged),
                path=str(staged),
                count_semantics=validator_settings.get("count_semantics", "max"),
                warn_at=validator_settings.get("warn_at", WARNING_CHARACTERS),
                fail_at=validator_settings.get("fail_at", MAX_CHARACTERS),
            )
            if not report["ok"]:
                raise RenderError(json.dumps(report, sort_keys=True))
            write_validation(staged, report)
            records.append(
                {
                    "final": str(final),
                    "staging": str(staged),
                    "digest": _digest(staged),
                    "backup": None,
                    "published_new": not final.exists(),
                    "raw_ask": raw_ask if raw_ask is not None else member["raw_ask"],
                    "member": member,
                }
            )
        if manifest_name:
            final = _safe_target(root, destination, manifest_name)
            if final.exists() and not force:
                raise RenderError(f"target exists: {final.name}")
            staged = staging_dir / manifest_name
            staged.write_text(_manifest_document(members), encoding="utf-8")
            records.append(
                {
                    "final": str(final),
                    "staging": str(staged),
                    "digest": _digest(staged),
                    "backup": None,
                    "published_new": not final.exists(),
                }
            )
        if force:
            for record in records:
                final = Path(record["final"])
                if final.exists():
                    record["backup"] = str(staging_dir / f"{final.name}.backup")
        journal = _journal_path(destination, batch_id)
        _publish_records(
            records,
            journal=journal,
            data=_journal_data(batch_id, "preparing", staging_dir, records),
        )
        for record in records:
            if record["backup"]:
                Path(record["backup"]).unlink(missing_ok=True)
        journal.unlink()
        _fsync_directory(destination)
        shutil.rmtree(staging_dir)
        return [Path(record["final"]) for record in records]
    except Exception:
        if not _journal_path(destination, batch_id).exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise
