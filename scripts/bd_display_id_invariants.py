"""Source-agnostic display-id invariant cores (ISSUE-REF half 1).

Import-only: there is no shebang because this module has no entrypoint. Its
callers are the gates (``bd_precommit_validate``, ``bd_invariants``).

**Why this module exists.** The dual-ID invariants were implemented only over JSONL
*text*, so the only gate that could enforce them was ``bd_precommit_validate``,
reading the staged ``.beads/issues.jsonl``. That coupling is what makes ISSUE-REF's
second half dangerous in the wrong order: turning off ``export.git-add`` stops
staging the mirror on every write, and a gate whose input is no longer produced
does not fail — it silently passes. The invariants have to be expressed over
something the LIVE STORE can also supply before that switch is safe to flip.

**The shape.** Every core takes ``(locator, entry)`` pairs. A locator is whatever
identifies the record in its own source — ``"L12"`` for a mirror line,
``"store"`` for a Dolt row — and appears as the message prefix. Entries are plain
dicts with bd's export/list field names, which both sources already use: a
``bd list -n 0 --all --json`` row and an ``issues.jsonl`` line carry the same
``id``/``labels``/``_type`` keys. So the store gate adds **no store read at
all** — it re-uses rows its caller already fetched.

**THE TWO SOURCES ARE NO LONGER INTERCHANGEABLE, AND PRETENDING THEY ARE WOULD
FAIL EVERY COMMIT (ISSUE-REF D-5).** Post-cutover a record's display id lives in
the bd metadata field ``display_id``, and ``bd export`` emits NO ``metadata`` key
on any row — measured 2026-10-02 against bd 1.3.x, and export has no option to
include it. So a store row can be asked about ``display_id`` and a mirror line
cannot, ever. ``metadata_capable`` below is the discriminator every
metadata-dependent core consults: on a metadata-less source they return ``[]``
with that reason, because "no row here carries display_id metadata" is a fact
about the EXPORT FORMAT and reporting it as a violation would reject every commit
in the fleet. What survives on the mirror is the ``id:<display_id>`` label echo
(D-4): label UNIQUENESS and the retired-range check both run there, on the label.

**Stdlib only, deliberately.** ``bd_precommit_validate`` is a Tier-B hook that runs
under bare system ``python3`` where PyYAML and friends are unavailable, and it
imports these cores. A third-party import here would break the commit path
on a fresh machine.

Three invariant families, kept separate because each wants a DIFFERENT repair
command. Collapsing them into one "something is off" verdict is what forces the
improvised hand recovery this gate's remediation output exists to prevent:

  * ``label_problems`` — an ``id:`` label must equal ``id:<own
    metadata.display_id>`` (agreement, metadata-dependent) and may be held by at
    most one issue (uniqueness, label-only and so always checked). Repair is
    ``--remove-label``, never ``--set-labels`` (which replaces the whole set and
    drops real routing labels) and never ``--add-label`` (which created the defect).
  * ``missing_authoritative_ref_problems`` — ``metadata.display_id`` specifically
    must be present. Repair is ``bd update --set-metadata display_id=``, and the
    message carries ``bd_common.display_id_migration_hint`` so an un-migrated
    store is told what to run instead of just what is wrong. Metadata-dependent.
  * ``retired_range_problems`` — a legacy ``PREFIX-N`` display id at or above the
    declared ceiling. Reads ``metadata.display_id``, falling back to the ``id:``
    label echo on a metadata-less source, so the mirror gate keeps a working
    range check. Repair re-mints in the slug scheme; a smaller number is not a fix.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bd_common import row_display_id  # (path shim above must run first)

LABEL_PREFIX = "id:"

# LEGACY ids have exactly one dash. New slug ids always have at least two, even
# when their copied base-36 hash suffix happens to be all digits — which is why
# the scheme split is dash count, not digit-ness.
DISPLAY_ID = re.compile(r"^(?P<prefix>[A-Za-z][\w-]*?)-(?P<n>\d+)$")

Located = Sequence[tuple[str, dict]]


def metadata_capable(located: Located) -> bool:
    """Can this source be asked about ``metadata.display_id`` at all?

    True when ANY row carries a ``metadata`` mapping. That is the honest
    discriminator between the two sources rather than a source-name argument the
    caller could get wrong: a live ``bd list --json`` row always carries
    ``metadata`` (``{}`` at minimum) while an ``issues.jsonl`` line never carries
    the key, because ``bd export`` does not emit it (ISSUE-REF D-5).

    An EMPTY ``located`` is not capable, which is the safe direction: with no rows
    there is no violation to find either way, so returning ``[]`` from the
    metadata cores costs nothing and asserting capability we have not observed
    would be a guess.
    """
    return any(
        isinstance(entry, dict) and isinstance(entry.get("metadata"), dict)
        for _, entry in located
    )


def label_echo(entry: dict) -> str | None:
    """The display id from a SINGLE ``id:`` label, else ``None``.

    The fallback carrier for a metadata-less source. Deliberately refuses to
    answer when a row holds two ``id:`` labels: a label set is multi-valued, so
    there is then no fact of the matter about which is the display id (ISSUE-REF),
    and ``label_problems``' uniqueness half is the check that reports it.
    """
    labels = _id_labels(entry)
    if len(labels) != 1:
        return None
    value = labels[0][len(LABEL_PREFIX) :].strip()
    return value or None


def legacy_display_id_match(display_id: str) -> re.Match[str] | None:
    """Match a legacy ``PREFIX-N`` id, never a numeric-suffixed slug id."""
    return DISPLAY_ID.fullmatch(display_id) if display_id.count("-") == 1 else None


def _issue_rows(located: Located) -> Iterable[tuple[str, dict]]:
    """Only the top-level issue records, in source order.

    A row carrying an explicit non-``issue`` ``_type`` is skipped; ``None`` counts
    as an issue because bd's default export omits the key entirely.
    """
    for locator, entry in located:
        if isinstance(entry, dict) and entry.get("_type") in (None, "issue"):
            yield locator, entry


def _id_labels(entry: dict) -> list[str]:
    return [
        label
        for label in entry.get("labels") or []
        if isinstance(label, str) and label.startswith(LABEL_PREFIX)
    ]


def _matches_primary(display_id: str, primary: str) -> bool:
    """True when ``display_id``'s trailing segment is the suffix of ``primary``.

    A slug display id is ``PREFIX-<body>-<suffix>`` where the suffix comes from the id bd assigned
    to the record's own hash primary. That relationship is what makes the record self-describing:
    whichever half of a mismatch carries the matching suffix is the half that belongs to THIS
    record, and the other one came from somewhere else.

    Returns False for the legacy ``PREFIX-N`` scheme, where the number has no relationship to the
    hash id at all. That is correct rather than a limitation -- for a legacy id the record genuinely
    cannot settle the direction, and saying so is the honest answer.
    """
    segment = display_id.rsplit("-", 1)[-1]
    return bool(segment) and segment != display_id and primary.endswith(segment)


def _disagreement(locator: str, issue_id: str, label: str, ref: str) -> str:
    """Describe a label/display_id mismatch, naming the side the RECORD says is wrong.

    The previous wording called the label stale unconditionally and the aggregated remedy told the
    reader to strip it. Measured 2026-09-10, that was backwards: this gate's own earlier remedy had
    set ``external_ref`` from a newly allocated display id, so the label held the correct value and
    ``external_ref`` held the corruption. Following the printed advice would have destroyed the only
    correct copy.

    So the direction is derived, and where it cannot be derived no command is printed at all. A
    remedy offered with the authority of the tool that detected the problem must not be a guess.
    """
    label_id = label[len(LABEL_PREFIX) :] if label.startswith(LABEL_PREFIX) else label
    label_ok = _matches_primary(label_id, issue_id)
    ref_ok = _matches_primary(ref, issue_id)

    head = (
        f"{locator}: issue {issue_id} has display-id label {label!r} "
        f"disagreeing with its display_id metadata {ref!r}"
    )
    if label_ok and not ref_ok:
        return (
            f"{head} -- the label's suffix matches this record's own id and the metadata's does "
            f"not, so the DISPLAY_ID METADATA is the wrong side; repair with "
            f"`bd update {issue_id} --set-metadata display_id={label_id}`"
        )
    if ref_ok and not label_ok:
        return (
            f"{head} -- the metadata's suffix matches this record's own id and the label's does "
            f"not, so the LABEL is the wrong side; repair with "
            f"`bd update {issue_id} --remove-label={label}`, never --set-labels"
        )
    return (
        f"{head} -- which side is correct cannot be determined from the record: "
        f"{'both suffixes match' if label_ok else 'neither suffix matches'} its own id "
        f"{issue_id!r}. Resolve by hand against the committed mirror's history; no repair command "
        f"is offered, because either direction would be a guess and the wrong one destroys the "
        f"correct value."
    )


def label_problems(located: Located) -> list[str]:
    """Agreement + uniqueness for ``id:`` labels; ``[]`` means clean.

    Scheme-agnostic: a label is compared to its own issue's ``metadata.display_id``
    as a STRING, so legacy and slug ids are both guarded without either being
    parsed.

    THE TWO HALVES HAVE DIFFERENT SOURCE REQUIREMENTS, which is why they are not
    one check (ISSUE-REF D-5). UNIQUENESS reads labels only, so it runs on any
    source and is what keeps the staged-mirror gate load-bearing. AGREEMENT needs
    ``metadata.display_id``, so on a metadata-less source (an ``issues.jsonl``
    line, which carries no ``metadata`` key at all) it is SKIPPED rather than
    reported: every row there would otherwise look like a disagreement, which is a
    fact about the export format and not a defect in any record.

    Uniqueness is checked explicitly even though agreement plus display-id
    uniqueness would imply it, because the two failures want different repair
    commands.

    A child created with ``--parent`` INHERITS its parent's labels, ``id:``
    included, and that inheritance was the largest single source of the duplicates
    recorded in ``docs/bugs/ISSUE-REF-stale-display-id-labels.md``. It is therefore a
    violation to clean, not an exemption — a deliberate difference from
    ``bd_invariants`` invariant 1, where an inherited label is ignored because that
    check asks who authoritatively OWNS a display id, a question only
    ``metadata.display_id`` answers.

    Agreement is not claimed for a row with no ``display_id`` metadata: there is
    nothing authoritative to disagree with, and the absent half is its own
    invariant, so reporting it here would emit two errors and two conflicting
    remedies for one defect.
    """
    problems: list[str] = []
    holders: dict[str, tuple[str, str]] = {}
    check_agreement = metadata_capable(located)
    for locator, entry in _issue_rows(located):
        issue_id = entry.get("id") or f"<no id, {locator}>"
        ref = row_display_id(entry) if check_agreement else None
        labels = entry.get("labels")
        if labels is not None and not isinstance(labels, list):
            problems.append(f"{locator}: issue {issue_id} has a non-list 'labels'")
            continue
        for label in _id_labels(entry):
            if ref and label != f"{LABEL_PREFIX}{ref}":
                problems.append(_disagreement(locator, str(issue_id), label, str(ref)))
            if label in holders:
                other_id, other_locator = holders[label]
                problems.append(
                    f"{locator}: issue {issue_id} shares display-id label "
                    f"{label!r} with issue {other_id} at {other_locator}"
                )
            else:
                holders[label] = (str(issue_id), locator)
    return problems


def missing_authoritative_ref_problems(located: Located) -> list[str]:
    """Rows lacking ``metadata.display_id``, the AUTHORITATIVE half; ``[]`` is clean.

    This closes a real asymmetry rather than duplicating an existing check.
    ``bd_invariants`` invariant 2 asks whether an issue has *any* display id and
    accepts an ``id:`` label as sufficient, so a row carrying a label and no
    authoritative half passed the push gate while failing the commit gate. That
    matters because anything resolving a display id by label instead of the
    metadata gets a real but UNRELATED issue and nothing errors — an observed
    ``bd dep add`` attached an edge to the wrong issue twice in one session.

    RETURNS ``[]`` ON A METADATA-LESS SOURCE, and that is the whole correctness
    story for the staged-mirror gate (ISSUE-REF D-5). ``bd export`` emits no
    ``metadata`` key, so asking an ``issues.jsonl`` line for ``display_id`` always
    answers "absent" — every row in every mirror would be a violation, and the
    gate would reject every commit in the fleet while pointing at nothing real.
    An un-migrated STORE is still caught, because a store row carries
    ``metadata`` (``{}`` at minimum) and so is capable.

    Returns ONLY per-row problems, one string each, so ``len()`` is the record
    count. The compatibility remedy is NOT folded in here: every gate that reports
    a non-empty result appends ``bd_common.display_id_migration_hint`` once to its
    own output (``bd_invariants``, ``bd_precommit_validate``), because a multi-line
    instruction repeated per row is how a real remedy gets scrolled past, and
    counting it as a row would overstate the damage.

    An empty string is absence, not presence. Set-but-empty is a distinct third
    state from present and absent, and it is reliably the buggy one;
    ``row_display_id`` collapses it to ``None`` for exactly that reason.
    """
    if not metadata_capable(located):
        return []
    problems: list[str] = []
    for locator, entry in _issue_rows(located):
        if row_display_id(entry) is None:
            issue_id = entry.get("id") or f"<no id, {locator}>"
            problems.append(
                f"{locator}: issue {issue_id} has no display_id metadata — the "
                "authoritative half of its display id is missing"
            )
    return problems


def entry_display_id(entry: dict) -> str | None:
    """One row's display id: ``metadata.display_id``, else the single ``id:`` label.

    THE PER-ROW reader for a caller that may be handed EITHER source and cannot
    look at the whole set first — notably ``bd_display_id_history.owner_map``,
    which is fed ``.beads/issues.jsonl`` text from both a historical git revision
    and a fresh ``bd export``, neither of which carries ``metadata`` (ISSUE-REF
    D-5). Without the label rung it would read zero display ids out of a
    post-cutover export and report every published id as having DISAPPEARED.

    The label rung is sanctioned here and the ``external_ref`` rung is not, which
    is not inconsistency: D-4 keeps ``id:<display_id>`` as the indexed ECHO of the
    metadata, maintained by ``bd-new`` and gated for agreement, whereas D-1(b)
    frees ``external_ref`` to hold an unrelated Jira key.

    CAVEAT, and it is the reason this is not the default accessor: a child created
    with ``--parent`` inherits its parent's labels verbatim, so a label can name an
    id the row does not own. ``label_echo`` refuses a row carrying two or more
    ``id:`` labels — the common inherited shape, once the child has its own — but a
    child killed before ``bd_new._cleanup_inherited_display_id_labels`` ran carries
    exactly one, the parent's, and is indistinguishable here. Prefer
    ``bd_common.row_display_id`` wherever the metadata is actually readable.
    """
    return row_display_id(entry) or label_echo(entry)


def _range_checkable_display_id(entry: dict, *, from_metadata: bool) -> str | None:
    """The display id the retired-range check should judge, or ``None``.

    ``metadata.display_id`` on a capable source; the single ``id:`` label echo
    otherwise (ISSUE-REF D-4/D-5). The fallback is what keeps the staged-mirror
    gate's range check load-bearing: the retired legacy ``PREFIX-N`` ids are real
    and numerous, and re-importing an old export is exactly how one reaches a
    commit, so dropping the check on the only source that sees a commit would
    retire the guard rather than the range.
    """
    if from_metadata:
        return row_display_id(entry)
    return label_echo(entry)


def retired_range_problems(located: Located, *, ceiling: int) -> list[str]:
    """Legacy display ids at or above the retired ceiling; ``[]`` means clean.

    ``ceiling`` is always passed in, never defaulted here: the caller resolves it
    from the manifest so an unreadable policy fails loudly in one place instead of
    silently becoming a built-in number this module would then enforce as if it
    had been declared.

    Checks the authoritative display id only — ``metadata.display_id``, or the
    single ``id:`` label on a metadata-less source. A stale label above the
    ceiling cannot agree with an in-range display id, so ``label_problems``
    already has it, and re-reporting would give two conflicting remediation
    commands for one defect.
    """
    from_metadata = metadata_capable(located)
    problems: list[str] = []
    for locator, entry in _issue_rows(located):
        ref = _range_checkable_display_id(entry, from_metadata=from_metadata)
        if not isinstance(ref, str):
            continue
        match = legacy_display_id_match(ref)
        if match and int(match.group("n")) >= ceiling:
            issue_id = entry.get("id") or f"<no id, {locator}>"
            problems.append(
                f"{locator}: issue {issue_id} carries display id {ref!r}, which is "
                f"in the RETIRED display-id range (N >= {ceiling})"
            )
    return problems


def has_legacy_display_ids(located: Located) -> bool:
    """Whether any row carries a legacy sequential ``PREFIX-N`` display id.

    The retired-range check's precondition, and it reads the display id by the
    same rule that check does — metadata where available, the ``id:`` label echo
    otherwise — so the two cannot disagree about whether there is anything to
    judge. Numbers are the only thing a ceiling can judge, so a store carrying
    none of them must not be asked for that policy at all: a required config key
    whose subject matter is absent can only ever fail closed, which rejects clean
    work and teaches nothing.
    """
    from_metadata = metadata_capable(located)
    return any(
        legacy_display_id_match(ref)
        for _, entry in _issue_rows(located)
        for ref in (_range_checkable_display_id(entry, from_metadata=from_metadata),)
        if isinstance(ref, str)
    )
