#!/usr/bin/env python3
# ruff: noqa: E402
"""Reject divergence between a live Beads export and its committed JSONL copy.

This is deliberately a file-to-file comparison. ``bd export`` can write a snapshot
outside ``.beads/``; pass that snapshot as ``live_export`` and a committed copy as
``committed_export``. **No fleet repo tracks ``.beads/issues.jsonl`` since D-8(b)
(ISSUE-REF)**, so the committed copy is a historical blob
(``git show <sha>:.beads/issues.jsonl > /tmp/committed.jsonl``) or a leftover file,
never the working-tree path, which does not exist in a retired repo.

The check reports four independent assertions:

* records only in the committed export;
* records only in the live export;
* display-id disagreements for shared record IDs; and
* other whole-record disagreements for shared IDs.

The final assertion is necessary for stale but internally-consistent exports:
changing an issue's status in the store leaves both its record ID and display
ID unchanged, but must still fail until the export is refreshed.

A fifth assertion covers dependency-edge loss (ISSUE-REF).  ``bd import`` drops an
edge whose target does not resolve in the importing store -- the cross-repo case
-- warning only on stderr while still exiting 0.  A later ``bd export`` is
faithful to that now-lossy store, so the edge vanishes from the committed JSONL
while every count-based check still passes: the record is present, only a field
inside it shrank.  Edge loss is reported separately from a generic whole-record
disagreement because the two demand OPPOSITE remedies -- staleness is fixed by
re-exporting, and re-exporting is what completes the data loss here.

Because those remedies are opposite, the edge comparison is made on a NORMALIZED
target identity (ISSUE-REF).  ISSUE-REF's own sanctioned repair rewrites a bare
cross-repo target into ``external:<project>:<suffix>``, which denotes the same
issue; comparing the raw strings read that correct migration as one edge deleted
and an unrelated one added, and then told the operator the pre-migration copy was
the healthy one and not to re-export -- inverting the advice and recommending a
'bd dep add' that would duplicate an existing edge.  A form change is therefore
reported as its own informational line, never as loss.

That fix made the COMPARISON correct and left the EXPLANATION unconditional, which
is its own defect (ISSUE-REF): a single fixed block asserted the cross-repo cause
above whenever ANY edge was missing.  Measured on ai-harness main, all six edges it
reported were intra-repo with every endpoint resolving on both sides, so the cause
was false for every one and its ``external:`` remedy would have rewritten six
same-repo edges as references to another project.  The reason is now DERIVED per
edge -- unresolvable target, retargeted, or absent -- because the gate already holds
both edge lists and every record ID, so it can classify rather than guess.  Only the
first class is a cross-repo skip and only it carries the ``external:`` command.

For the same reason the whole-record comparison excludes bd's DERIVED COUNTERS
(ISSUE-REF).  ``dependent_count`` counts edges stored on OTHER records, so a
``bd dep add`` elsewhere moves it without stamping this record's ``updated_at``.
That combination is a LIVELOCK, not a slow path: the merge in
``bd_jsonl_merge.py`` breaks ties on ``updated_at`` and keeps the mirror, so it
reports "nothing to project" while this check rejects the same record, and no
write to that record can change either verdict.  Excluding them hides nothing --
they are unreliable as data (420 of 1331 ai-harness records export a
``dependency_count`` of 0 while carrying edges, which is why the edge comparison
above ignores them), every edge is compared explicitly, and an edge that moves a
counter is authored on the record at the other end of it, where it IS compared.
The difference is still printed, as the informational line it is.

THE ``--mirror-retirement-gate`` MODE GUARDS AN IRREVERSIBLE UNTRACK, AND SO FAILS CLOSED ON
EVERY AUTHORED DIFFERENCE (T-00, extended by T-5.4/D-16).  ``mirror_retirement_findings`` is
the third predicate of the untrack chain the mirror-retirement notice renders --

    bd_dolt_publish.py --repo R --attest && bd_mirror_redundancy_proof.py --repo R \\
      && bd_mirror_payload_proof.py --repo R && git rm --cached .beads/issues.jsonl

-- and it is the only predicate in that chain that compares mirror FIELDS with the store, and
the only one covering dependency EDGES at all.  The round-3 audit of the design established that
three of its leniencies were defects in that position, because each one passed ``.clean`` on a
difference the deletion would have destroyed: a mirror-newer prose edit (the
``INFORMATIONAL_TEXT_FIELDS`` were ignored unconditionally), an equal-stamp or unorderable
authored difference, and a duplicate comment -- two occurrences in the mirror against one in the
store, which a set-based content identity reports as agreement.

Recency is advisory now: it classifies a disagreement but never clears an unequal authored
value. Every direction blocks, because an unrelated newer live edit cannot prove that a distinct
mirror value was deliberately reconciled. The three exemptions that remain are each argued above
and each has a negative control in ``tests/test_mirror_retirement_gate.py``: the derived counters (excluding
them is what keeps the gate from livelocking), the three fields that have their own axis, and
``updated_at`` itself.  The symmetric ``compare`` mode is untouched by all of this -- it answers
a different question and nothing chains a deletion to it.
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

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from bd_display_id_invariants import entry_display_id  # (path shim must run first)
except (
    ModuleNotFoundError
) as exc:  # a copy of this script that did not carry its sibling
    # ISSUE-REF. Exit 2, not Python's 1. This script's contract already reserves 2 for "no
    # verdict was produced" (an unreadable or malformed export), and a missing sibling is the
    # same claim -- whereas 1 means "the committed copy holds something", which T-04 reads as a
    # reason to reconcile. The two were the same integer, and the only consumer is
    #
    #     check_beads_divergence.py --mirror-retirement-gate <live> <mirror> \
    #       && git rm --cached .beads/issues.jsonl
    #
    # where `&&` reads the exit code ALONE. The stdout line that distinguishes a crash from a
    # verdict is therefore consulted exactly where nobody is deciding, and absent exactly where
    # someone is -- so better prose could not have fixed this.
    #
    # The sibling guard at the top of this module stubs `ensure_supported_python` and continues,
    # because a skipped version check leaves the real failure readable later. That is not
    # available here: `entry_display_id` is load-bearing for the comparison, so a stub would
    # change the verdict rather than defer a check. Same philosophy -- let the real failure be
    # readable -- different remedy, because this one has no safe silent form.
    #
    # NOT a cwd problem: the `sys.path` insert above resolves siblings from any working
    # directory (measured exit 0 running the real path from `/tmp`). Copying the file away from
    # its siblings is the trigger.
    sys.stderr.write(
        f"beads divergence check: ERROR: this script was copied without its sibling "
        f"module ({exc.name}), so no comparison was performed. Run it from the repo's "
        f"scripts/ directory, or copy bd_display_id_invariants.py alongside it. "
        f"This is NOT a divergence verdict.\n"
    )
    raise SystemExit(2) from exc

# One explanation per CAUSE, selected by evidence. Before ISSUE-REF a single fixed block was
# printed whenever any edge was missing, and it asserted the cross-repo cause below. Measured on
# ai-harness main, all six edges it reported were intra-repo with every endpoint resolving on both
# sides, so that cause was false for every one of them -- and its remedy would have CORRUPTED them,
# rewriting same-repo edges as cross-repo references to a project named AIH. The block named an
# issue id, a mechanism and an exact command, so it read as a finding rather than as boilerplate,
# and a later session acted on it. The gate already holds both edge lists and every record id, so
# it can classify instead of guessing.
UNRESOLVABLE_TARGET_REMEDY = """
  The target of each edge above resolves in NEITHER export, so nothing local can
  hold it, and the committed copy is the healthy one.
  DO NOT resolve this by re-exporting: a plain 'bd export' is faithful to the
  lossy store and would delete them.

  Cause (ISSUE-REF): 'bd import' silently skips a dependency whose target does not
  resolve locally, which is every bare cross-repo target. It reports
  'Skipped dependency: X -> Y: target not found' on stderr and still exits 0.

  To restore an edge, re-add it in the form that survives a round-trip:
    bd dep add <issue> --depends-on external:<project>:<target>
  A bare cross-repo target ('--depends-on ISSUE-REF') is accepted and exports,
  but is dropped by the next import. The 'external:' form is stored in a
  separate column and survives. Verify with a re-export before committing.
""".rstrip()

ABSENT_EDGE_NOTE = """
  Every target above RESOLVES in both exports, so ISSUE-REF's cross-repo skip
  cannot explain these and the 'external:' remedy must NOT be used on them: it
  would rewrite a same-repo edge as a reference to another project.

  What this is instead: the committed mirror records an edge the live store has
  never held -- typically another machine's work that this store never imported.
  Re-exporting would delete it, so treat the mirror as the healthy copy and
  reconcile with 'bd_jsonl_merge.py', or re-add the edge with its BARE target:
    bd dep add <issue> --depends-on <target>
""".rstrip()

RETARGETED_EDGE_NOTE = """
  These edges are not missing, they DISAGREE: for each one the live store holds
  an edge of the same type pointing somewhere else. Both targets are printed
  above so the disagreement is visible rather than inferred.

  Nothing here is a cross-repo skip and the 'external:' remedy does not apply.
  Decide which target is correct -- one side re-pointed the edge and the other
  did not -- then make the two agree. 'bd dep add' would ADD a second edge
  rather than replace the existing one, so remove the wrong edge explicitly.
""".rstrip()

MIGRATED_DEPENDENCY_NOTE = """
  These edges are present on BOTH sides and differ only in how the target is
  spelled: the live store holds the 'external:<project>:<suffix>' form of a bare
  '<project>-<suffix>' target. That is ISSUE-REF's own sanctioned repair, so NO
  edge was lost and the LIVE copy is the healthy one -- re-exporting is safe and
  is the fix. Do NOT 'bd dep add' these: the edge already exists, and adding it
  again duplicates it.
""".rstrip()


DERIVED_COUNT_FIELDS = ("comment_count", "dependency_count", "dependent_count")

#: Printed after every ``--mirror-retirement-gate`` refusal, because this entry point takes TWO
#: FILES and some of what it refuses is not refusable on that evidence. Two exports cannot
#: distinguish "the store reconciled this value and moved on" from "the mirror holds the only
#: copy of it": a newer live ``updated_at`` can come from an edit to a different field on the
#: same row, so recency classifies a disagreement without resolving it. Blocking is the right
#: answer in front of an irreversible delete, and a block whose own remedy is unnamed is the
#: ``ISSUE-REF`` deadlock -- a gate demanding equality the remedy cannot produce. The ``--repo``
#: proof CAN resolve it, by asking the store's Dolt history whether the mirror's value ever
#: existed there, so the refusal points at it rather than leaving the operator to guess.
TWO_FILE_LIMIT_REMEDY = """
  Some of the above may be STALE rather than lost, and this entry point cannot tell: it has two
  export files and no store to ask. A mirror value that the store once held and has since
  changed is safe to delete; a value the store has never held is not, and the two look
  identical in two files.

  To decide it, run the payload proof against the repository itself, which asks the store's own
  Dolt history per value:
    python3 <ai-harness>/scripts/bd_mirror_payload_proof.py --repo <repo>
  It exits 0 only when every mirror value is present in the store or superseded in its history.
""".rstrip()


class ExportFormatError(Exception):
    """An export cannot be compared safely."""


@dataclass
class Divergence:
    """The independent ways two exports can disagree."""

    committed_only: list[str] = field(default_factory=list)
    live_only: list[str] = field(default_factory=list)
    display_id_mismatches: list[tuple[str, Any, Any]] = field(default_factory=list)
    record_mismatches: list[str] = field(default_factory=list)
    dropped_dependencies: list[tuple[str, str, str]] = field(default_factory=list)
    # The derived cause for each entry in dropped_dependencies, keyed by that entry's own tuple so
    # the rejection set keeps its shape and arity for bd_prepush_divergence, which unpacks it.
    # Values are (classification, live target of the same type or None).
    dependency_causes: dict[tuple[str, str, str], tuple[str, str | None]] = field(
        default_factory=dict
    )
    # Diagnostic only: a form change is never itself a reason to reject, but it
    # explains the whole-record disagreement it necessarily causes.
    migrated_dependencies: list[tuple[str, str, str, str]] = field(default_factory=list)
    # Diagnostic only, and deliberately not a rejection: see the module docstring
    # on why a derived counter cannot be lost information and why rejecting on one
    # livelocks. (issue_id, field, live_value, committed_value)
    derived_count_differences: list[tuple[str, str, Any, Any]] = field(
        default_factory=list
    )

    @property
    def clean(self) -> bool:
        return not (
            self.committed_only
            or self.live_only
            or self.display_id_mismatches
            or self.record_mismatches
            or self.dropped_dependencies
        )


def load_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    """Load one JSON object per line from ``path``, rejecting malformed exports."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExportFormatError(f"cannot read {path}: {exc}") from exc
    return parse_jsonl(text, str(path))


def parse_jsonl(text: str, source: str) -> dict[str, dict[str, Any]]:
    """Parse export JSONL already in memory, ``source`` naming it in every error.

    Split out of :func:`load_jsonl` for ``bd_mirror_payload_proof``, which holds the mirror as
    TEXT: ISSUE-REF's ``mirror_blob`` can return the committed blob at HEAD, which is not a file on
    disk, and spooling it through a temporary file to re-read it would make the proof a writer
    for no reason. Same parser either way -- a second one is a second set of answers to "is this
    export usable".
    """
    rows: dict[str, dict[str, Any]] = {}
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExportFormatError(
                f"{source}:L{line_no}: invalid JSON ({exc.msg})"
            ) from exc
        if not isinstance(row, dict):
            raise ExportFormatError(f"{source}:L{line_no}: expected a JSON object")
        issue_id = row.get("id")
        if not isinstance(issue_id, str) or not issue_id:
            raise ExportFormatError(
                f"{source}:L{line_no}: missing non-empty string 'id'"
            )
        if issue_id in rows:
            raise ExportFormatError(
                f"{source}:L{line_no}: duplicate record id {issue_id!r}"
            )
        rows[issue_id] = row
    return rows


def normalize_dependency_target(target: str) -> str:
    """The identity of a dependency target, collapsing the two spellings bd accepts.

    ``external:<project>:<suffix>`` and the bare ``<project>-<suffix>`` name the
    same issue, so they must compare equal (ISSUE-REF).  Anything that is not a
    well-formed ``external:`` triple is returned unchanged -- the point is to
    collapse two known-equivalent spellings, not to guess at malformed ones.
    """
    scheme, _, rest = target.partition(":")
    if scheme != "external":
        return target
    project, separator, suffix = rest.partition(":")
    if not (separator and project and suffix):
        return target
    return f"{project}-{suffix}"


# The three reasons a committed edge can have no identity match in the live store. They need
# different repairs, so they need different words: the external: form fixes only the first and
# actively corrupts the other two.
UNRESOLVABLE_TARGET = "unresolvable_target"
RETARGETED_EDGE = "retargeted"
ABSENT_EDGE = "absent"


def classify_dropped_edge(
    target: str,
    dep_type: str,
    live_edges: dict[tuple[str, str], str],
    known_ids: set[str],
) -> tuple[str, str | None]:
    """Why this committed edge has no match in the live store, derived from the exports.

    ``known_ids`` is every id either export holds. A target absent from it resolves nowhere local,
    which is the one case ISSUE-REF's cross-repo diagnosis and its ``external:`` remedy apply to.

    A target that DOES resolve cannot have been skipped for being unresolvable, so the edge is
    either absent from live entirely or pointed elsewhere. The second is distinguishable: look for
    a live edge of the same type on the same record. Returns the classification and, for a
    retargeted edge, the live target so the caller can print both sides of the disagreement.
    """
    if normalize_dependency_target(target) not in known_ids:
        return UNRESOLVABLE_TARGET, None
    same_type = [
        live_target
        for (_, live_type), live_target in live_edges.items()
        if live_type == dep_type
    ]
    if same_type:
        # Deterministic pick so the report is stable across runs; a many-valued type can have
        # several, and the note is worded to describe rather than to conclude.
        return RETARGETED_EDGE, sorted(same_type)[0]
    return ABSENT_EDGE, None


def dependency_edges(row: dict[str, Any]) -> dict[tuple[str, str], str]:
    """Map each edge's normalized (target, type) identity to its raw target string.

    Comparison uses the identity so an ``external:``/bare form change is not read
    as loss; the raw target is carried alongside so diagnostics quote the string
    that is actually in the file the operator will open.

    ``dependencies`` is absent on some records, ``null`` on a record whose edges
    were all dropped, and a list otherwise.  ``dependency_count`` is NOT used:
    it is unreliable in practice (records carrying three edges export with a
    count of 0), so trusting it would make the check silently under-report.
    """
    edges: dict[tuple[str, str], str] = {}
    raw = row.get("dependencies")
    if not isinstance(raw, list):
        return edges
    for edge in raw:
        if not isinstance(edge, dict):
            continue
        target = edge.get("depends_on_id")
        if isinstance(target, str) and target:
            identity = (normalize_dependency_target(target), str(edge.get("type", "")))
            # A record can carry both spellings of one edge; either raw value
            # points the operator at the same edge, so the first is kept.
            edges.setdefault(identity, target)
    return edges


# The four ways a disagreeing pair of records can be ordered. Named here, in the
# module both gates already import, because they must not answer this question two
# different ways: ISSUE-REF's deadlock was a gate demanding equality while the merge
# tool it prescribed as the remedy declined to produce it, and one policy with one
# owner is what that issue asks for.
MIRROR_BEHIND = "mirror_behind"
STORE_BEHIND = "store_behind"
UNSTAMPED = "unstamped"
UNORDERABLE = "unorderable"


def _stamp(row: dict[str, Any]) -> str | None:
    """A record's ``updated_at``, or ``None`` when it cannot order anything."""
    value = row.get("updated_at")
    return value if isinstance(value, str) and value else None


def record_direction(live_row: dict[str, Any], mirror_row: dict[str, Any]) -> str:
    """Which side of a disagreeing record the evidence favours.

    A plain string comparison of two identically-formatted ISO timestamps, so it
    needs no parsing. The last two answers are kept apart because
    ``bd_jsonl_merge`` treats them differently and conflating them produced a
    livelock (ISSUE-REF):

    * ``UNSTAMPED`` -- equal timestamps with differing content is an unstamped
      write, not ambiguity. ``bd dep add`` edges and label writes both change a
      record without moving ``updated_at``. The merge tool resolves these by
      taking the store side, so they clear with the ordinary projection.
    * ``UNORDERABLE`` -- a missing timestamp on either side. The merge tool
      declines it and says so; absent an ordering, picking a side is exactly the
      failure direction reporting exists to stop.
    """
    live_at = _stamp(live_row)
    mirror_at = _stamp(mirror_row)
    if live_at is None or mirror_at is None:
        return UNORDERABLE
    if live_at == mirror_at:
        return UNSTAMPED
    return MIRROR_BEHIND if live_at > mirror_at else STORE_BEHIND


def authored_fields(row: dict[str, Any]) -> dict[str, Any]:
    """``row`` without bd's derived counters -- the part a session actually wrote.

    Whole-record equality is asked of this, not of the raw row, so a counter
    nobody authored cannot reject a push (see the module docstring).
    """
    return {key: value for key, value in row.items() if key not in DERIVED_COUNT_FIELDS}


def compare(
    live: dict[str, dict[str, Any]], committed: dict[str, dict[str, Any]]
) -> Divergence:
    """Compare exports without collapsing a display-ID conflict into an ID-set result."""
    shared = live.keys() & committed.keys()
    # Read through ``entry_display_id``, not off ``external_ref`` (ISSUE-REF
    # D-1(b)/D-5). Both sides here are ``bd export`` JSONL, which carries no
    # ``metadata`` key, so the display id arrives as the ``id:`` label echo; and
    # post-cutover ``external_ref`` holds a JIRA KEY, so comparing it would have
    # reported a Jira link added on one machine as a display-id conflict and
    # blocked the push. Raw-field divergence is still covered -- whole-record
    # equality below compares every key, including ``external_ref``.
    display_id_mismatches = sorted(
        (
            issue_id,
            entry_display_id(live[issue_id]),
            entry_display_id(committed[issue_id]),
        )
        for issue_id in shared
        if entry_display_id(live[issue_id]) != entry_display_id(committed[issue_id])
    )
    # Directional on purpose: only edges the committed copy has and the live
    # store lacks are LOSS.  An edge added live is ordinary staleness, already
    # covered by record_mismatches.
    dropped_dependencies: list[tuple[str, str, str]] = []
    migrated_dependencies: list[tuple[str, str, str, str]] = []
    # Resolvability is judged against BOTH exports, not just live. A target the mirror knows and
    # the live store does not is still a locally-known issue, so calling it an unresolvable
    # cross-repo target would be the same false claim in a narrower form.
    known_ids = set(live.keys()) | set(committed.keys())
    dependency_causes: dict[tuple[str, str, str], tuple[str, str | None]] = {}
    for issue_id in shared:
        live_edges = dependency_edges(live[issue_id])
        for identity, committed_target in dependency_edges(committed[issue_id]).items():
            _, dep_type = identity
            live_target = live_edges.get(identity)
            if live_target is None:
                entry = (issue_id, committed_target, dep_type)
                dropped_dependencies.append(entry)
                dependency_causes[entry] = classify_dropped_edge(
                    committed_target, dep_type, live_edges, known_ids
                )
            elif live_target != committed_target:
                migrated_dependencies.append(
                    (issue_id, committed_target, live_target, dep_type)
                )
    record_mismatches: list[str] = []
    derived_count_differences: list[tuple[str, str, Any, Any]] = []
    for issue_id in shared:
        live_row, committed_row = live[issue_id], committed[issue_id]
        if live_row == committed_row:
            continue
        if authored_fields(live_row) != authored_fields(committed_row):
            record_mismatches.append(issue_id)
            continue
        derived_count_differences.extend(
            (issue_id, name, live_row.get(name), committed_row.get(name))
            for name in DERIVED_COUNT_FIELDS
            if live_row.get(name) != committed_row.get(name)
        )
    return Divergence(
        committed_only=sorted(committed.keys() - live.keys()),
        live_only=sorted(live.keys() - committed.keys()),
        display_id_mismatches=display_id_mismatches,
        record_mismatches=sorted(record_mismatches),
        dropped_dependencies=sorted(dropped_dependencies),
        dependency_causes=dependency_causes,
        migrated_dependencies=sorted(migrated_dependencies),
        derived_count_differences=sorted(derived_count_differences),
    )


#: Fields whose disagreement is reported without blocking ONLY WHERE THE COMMITTED SIDE IS
#: PROVABLY OLDER. The AI_CLI text differences are a measured redaction (plan Table B), not lost
#: work, and the live store is expected to be ahead on prose -- but "expected" is a prior, and
#: T-5.4/D-16 put this gate in front of an irreversible untrack, where the prior has to be proved
#: per record. So these names no longer select a LENIENCY; they select which informational LINE a
#: ``MIRROR_BEHIND`` difference is printed on. Every other direction blocks, here as everywhere
#: else (see :data:`PAYLOAD_EXCLUDED_FIELDS`).
INFORMATIONAL_TEXT_FIELDS = (
    "title",
    "description",
    "notes",
    "design",
    "acceptance_criteria",
)

#: Keys the authored-payload comparison does NOT look at, each excluded for its own reason.
#:
#: The comparison is deliberately a CLOSED EXCLUSION LIST over the MIRROR row's keys, never an
#: allowlist of fields we know about: a column a future bd schema adds is then compared by
#: construction, and the gate fails closed on it (T-5.1's unclassified-field rule). An allowlist
#: reads identically on every test and goes blind the day the schema grows. The mirror's keys
#: rather than the union, because the axis is directional -- see :func:`_authored_regressions`.
#:
#: * :data:`DERIVED_COUNT_FIELDS` -- bd rewrites them without stamping ``updated_at``, so every
#:   such pair is ``UNSTAMPED`` and comparing them would relivelock the gate (ISSUE-REF): no
#:   write to the record could clear it. The module docstring carries the full argument.
#: * ``status``, ``comments``, ``dependencies`` -- each already has its own blocking axis, which
#:   reports the loss in its own vocabulary with the operator's remedy attached. Comparing them
#:   here would report one loss twice, the second time in vaguer words.
#: * ``updated_at`` -- it IS the input to :func:`record_direction`. Every direction except
#:   ``UNSTAMPED`` differs in it by construction, so including it would make
#:   ``authored_regressions`` fire on every mirror-newer record whether or not any payload is at
#:   risk. A mirror-newer stamp over byte-identical content is a stamp, not a sole copy of
#:   anything; the content that WOULD be at risk lives in the fields above and is compared there.
PAYLOAD_EXCLUDED_FIELDS = frozenset(DERIVED_COUNT_FIELDS) | {
    "status",
    "comments",
    "dependencies",
    "updated_at",
}


@dataclass
class MirrorRetirement:
    """What the COMMITTED copy holds that the live store does not (plan T-00).

    Deliberately not a reuse of :class:`Divergence`. That class answers "do these two exports
    agree", which is symmetric; this one answers "is anything lost by deleting the committed
    file", which is directional. Collapsing them would make live-only ids -- the expected steady
    state once the store is authoritative -- block the retirement forever.

    The first six lists BLOCK. The rest are printed and ignored.
    """

    committed_only: list[str] = field(default_factory=list)
    status_regressions: list[tuple[str, str, str, str, str]] = field(
        default_factory=list
    )
    missing_comments: list[tuple[str, str]] = field(default_factory=list)
    missing_edges: list[tuple[str, str, str]] = field(default_factory=list)
    #: (issue_id, field, direction) for an authored field the committed copy holds and the live
    #: store is not PROVED to supersede -- T-5.4/D-16's axis. See :func:`_authored_regressions`.
    authored_regressions: list[tuple[str, str, str]] = field(default_factory=list)
    #: (issue_id, author, excerpt, live_count, committed_count) where the mirror holds MORE
    #: occurrences of one comment's content than the store does.
    comment_multiplicity: list[tuple[str, str, str, int, int]] = field(
        default_factory=list
    )
    live_only: list[str] = field(default_factory=list)
    live_newer: list[str] = field(default_factory=list)
    text_differences: list[tuple[str, str]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True when nothing in the committed copy is absent from the live store."""
        return not (
            self.committed_only
            or self.status_regressions
            or self.missing_comments
            or self.missing_edges
            or self.authored_regressions
            or self.comment_multiplicity
        )


def _normalized_text(value: object) -> str:
    """Whitespace-collapsed text, so a reflow is not read as a different comment."""
    return " ".join(str(value or "").split())


def comment_identities(
    row: dict[str, Any],
) -> tuple[set[str], set[tuple[str, str, str]]]:
    """A record's comments as ``(ids, content_keys)``.

    Two identities, not one, because the gate must accept the plan's own remedy. A mirror-only
    comment is reconciled with ``bd comment add``, which mints a FRESH id -- so an id-only
    comparison could never be cleared by the prescribed fix, and the gate would be permanently
    unpassable however completely an operator reconciled. That is exactly the deadlock
    ``ISSUE-REF`` names and that ``MIRROR_BEHIND``/``UNSTAMPED`` above exist to avoid: a gate
    demanding equality the remedy cannot produce.

    Matching on content as well is safe on the only axis this gate protects. Identical
    normalized ``(author, created_at, text)`` means the information is present on the live
    record, whatever id carries it -- so it cannot hide a loss, only a renumbering.

    ``comment_count`` is not consulted, for the reason :func:`dependency_edges` already gives
    about ``dependency_count``: the counters are measured unreliable, so a count comparison
    would under-report silently. Array length is likewise not an identity -- equal counts with
    different content is a real loss.

    **Superseded as the GATE's identity by :func:`comment_occurrences` (T-5.1/T-5.4), and kept
    because it is this module's published shape for "which ids and contents does this record's
    comments array carry".** Two things it does that a guard in front of an irreversible delete
    must not: it collapses to a SET, so two identical mirror comments against one live comment
    compare equal and one of them is destroyed; and its content key includes ``created_at``,
    which ``bd comment add`` mints fresh -- so the very remedy the paragraphs above prescribe
    could never clear a content-keyed match. Measured: the test asserting the replay converges
    held ``created_at`` fixed across the replay, and so never exercised the replay at all.
    """
    ids: set[str] = set()
    content: set[tuple[str, str, str]] = set()
    raw = row.get("comments")
    if not isinstance(raw, list):
        return ids, content
    for comment in raw:
        if not isinstance(comment, dict):
            continue
        ident = comment.get("id")
        if isinstance(ident, str) and ident:
            ids.add(ident)
        content.add(
            (
                _normalized_text(comment.get("author")),
                _normalized_text(comment.get("created_at")),
                _normalized_text(comment.get("text")),
            )
        )
    return ids, content


def comment_occurrences(row: dict[str, Any]) -> Counter[tuple[str, str]]:
    """A record's comments as a MULTISET of ``(author, exact text)`` occurrences.

    The retirement gate is stricter than the attestation's candidate identity because it is
    about to delete the mirror: exact Markdown whitespace is authored payload. Two deliberate
    choices, each one a measured failure in the other direction:

    * Keyed on CONTENT ONLY -- never ``id``, never ``created_at``. ``bd comment add``, the
      prescribed remedy for a mirror-only comment, mints both fresh, so an identity carrying
      either could not be cleared by a perfect reconciliation: the ``ISSUE-REF`` deadlock, a
      gate demanding equality the remedy cannot produce.
    * Counting OCCURRENCES rather than collapsing to a set. Two identical comments in the
      mirror against one in the store is a real loss of one comment, and a set reports that
      pair as agreeing.
    """
    occurrences: Counter[tuple[str, str]] = Counter()
    raw = row.get("comments")
    if not isinstance(raw, list):
        return occurrences
    for comment in raw:
        if not isinstance(comment, dict):
            continue
        occurrences[
            (
                _normalized_text(comment.get("author")),
                str(comment.get("text") or ""),
            )
        ] += 1
    return occurrences


def authored_payload(row: dict[str, Any]) -> dict[str, Any]:
    """``row``'s authored payload: every key except :data:`PAYLOAD_EXCLUDED_FIELDS`.

    Distinct from :func:`authored_fields`, which the symmetric :func:`compare` uses and which
    excludes only the derived counters. This one additionally drops the three fields that have
    their own blocking axis in the retirement gate, plus the ordering stamp the direction
    classifier consumes -- see :data:`PAYLOAD_EXCLUDED_FIELDS` for why each is out.
    """
    return {
        key: value for key, value in row.items() if key not in PAYLOAD_EXCLUDED_FIELDS
    }


def _authored_regressions(
    issue_id: str, live_row: dict[str, Any], committed_row: dict[str, Any]
) -> list[tuple[str, str, str]]:
    """``(issue_id, field, direction)`` per authored field the live store cannot be proved to hold.

    Recency classifies the disagreement but never clears it. A newer live timestamp can be
    caused by an unrelated field edit and is not evidence that a distinct mirror value was
    deliberately reconciled. Every unequal authored value therefore blocks untracking.

    Fields are taken from the COMMITTED row's keys -- the mirror's -- because this axis is
    directional and only the mirror's own payload can be lost by deleting the mirror. A key the
    mirror carries is compared whether or not the live row has it, so a mirror-only field is a
    difference rather than an absence nobody looked for; a key only the LIVE row carries is the
    store being AHEAD, and reporting it here was a measured defect. Against a real migrated
    store the union form produced 17 findings printed as ``metadata = 'None'`` -- rows whose
    mirror has no ``metadata`` key at all while the store has a one-key ``display_id`` object.
    The verdict blocked the steady state and the stated reason named a mirror value that does
    not exist.
    """
    direction = record_direction(live_row, committed_row)
    live_payload = authored_payload(live_row)
    committed_payload = authored_payload(committed_row)
    return [
        (issue_id, name, direction)
        for name in sorted(committed_payload)
        if live_payload.get(name) != committed_payload[name]
    ]


def mirror_retirement_findings(
    live: dict[str, dict[str, Any]], committed: dict[str, dict[str, Any]]
) -> MirrorRetirement:
    """Answer the one question T-04's untracking is gated on.

    ``ISSUE-REF`` T-04 runs ``check_beads_divergence.py --mirror-retirement-gate <live> <mirror>
    && git rm --cached .beads/issues.jsonl``, so this predicate is what stands between an
    operator and deleting the only copy of a mirror-only record. Every axis is therefore
    evaluated in the committed→live direction only.

    ``scripts/bd_mirror_payload_proof.py`` is the same policy under a ``--repo`` interface, as
    the third predicate of D-16's untrack chain. It delegates here rather than comparing
    anything itself, so there is one answer to "would this deletion lose a field".
    """
    findings = MirrorRetirement(
        committed_only=sorted(committed.keys() - live.keys()),
        live_only=sorted(live.keys() - committed.keys()),
    )
    for issue_id in sorted(live.keys() & committed.keys()):
        live_row, committed_row = live[issue_id], committed[issue_id]
        regression, live_newer = _status_finding(issue_id, live_row, committed_row)
        if regression is not None:
            findings.status_regressions.append(regression)
        if live_newer:
            findings.live_newer.append(issue_id)
        missing, multiplicity = _comment_findings(issue_id, live_row, committed_row)
        findings.missing_comments.extend(missing)
        findings.comment_multiplicity.extend(multiplicity)
        findings.missing_edges.extend(_missing_edges(issue_id, live_row, committed_row))
        findings.authored_regressions.extend(
            _authored_regressions(issue_id, live_row, committed_row)
        )
    return findings


def _status_finding(
    issue_id: str, live_row: dict[str, Any], committed_row: dict[str, Any]
) -> tuple[tuple[str, str, str, str, str] | None, bool]:
    """``(blocking_row_or_None, live_is_newer)`` for one shared record's status.

    Recency remains reportable metadata, never a resolution. A live-newer status can coexist
    with a mirror-only value elsewhere on the row, so every unequal status blocks deletion.
    """
    live_status = str(live_row.get("status", ""))
    committed_status = str(committed_row.get("status", ""))
    if live_status == committed_status:
        return None, False
    return (
        issue_id,
        live_status,
        committed_status,
        str(live_row.get("updated_at", "")),
        str(committed_row.get("updated_at", "")),
    ), record_direction(live_row, committed_row) == MIRROR_BEHIND


def _comment_findings(
    issue_id: str, live_row: dict[str, Any], committed_row: dict[str, Any]
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str, int, int]]]:
    """``(missing, multiplicity)`` for one shared record's comments.

    One occurrence-multiset subtraction answers both questions, and they are reported apart
    because they read differently to whoever has to act: content the live store holds NO copy
    of is a comment to re-add, while a shortfall against content it does hold is a duplicate to
    account for. Both block. A live record with MORE occurrences is the live store being ahead
    and is not a finding at all.
    """
    live = comment_occurrences(live_row)
    committed = comment_occurrences(committed_row)
    missing: list[tuple[str, str]] = []
    multiplicity: list[tuple[str, str, str, int, int]] = []
    for key in sorted(committed):
        author, text = key
        live_count, committed_count = live.get(key, 0), committed[key]
        if committed_count <= live_count:
            continue
        if live_count == 0:
            missing.append((issue_id, text[:120]))
        else:
            multiplicity.append(
                (issue_id, author, text[:120], live_count, committed_count)
            )
    return missing, multiplicity


def _missing_edges(
    issue_id: str, live_row: dict[str, Any], committed_row: dict[str, Any]
) -> list[tuple[str, str, str]]:
    """Committed ``(depends_on_id, type)`` edges absent from the live record."""
    live_edges = dependency_edges(live_row)
    return [
        (issue_id, target, identity[1])
        for identity, target in dependency_edges(committed_row).items()
        if identity not in live_edges
    ]


#: Every blocking axis except ``committed_only`` (which prints as one id list), as
#: ``(heading, MirrorRetirement attribute, per-row template)``. A table rather than six
#: near-identical ``if`` blocks: the report's ORDER is the thing that matters -- blocking first,
#: the numerous informational axes last -- and a table makes that order one readable line each.
BLOCKING_SECTIONS: tuple[tuple[str, str, str], ...] = (
    (
        "status advanced in the committed copy",
        "status_regressions",
        "  {0}: live={1!r} @ {3} committed={2!r} @ {4}",
    ),
    (
        "comments only in the committed copy",
        "missing_comments",
        "  {0}: {1!r}",
    ),
    (
        "comments the committed copy holds MORE occurrences of",
        "comment_multiplicity",
        "  {0}: {1!r} {2!r} live={3} committed={4}",
    ),
    (
        "dependency edges only in the committed copy",
        "missing_edges",
        "  {0} -> {1!r} ({2})",
    ),
    (
        "authored fields the live store is not proved to supersede",
        "authored_regressions",
        "  {0}: {1} ({2})",
    ),
)


def report_mirror_retirement(findings: MirrorRetirement, *, stream) -> None:
    """Print the blocking findings first, then what was deliberately ignored.

    Order is load-bearing: a gate whose reason is buried under expected noise gets waived by
    whoever reads it, and the informational axes are the NUMEROUS ones on a real store.
    """
    _print_ids(
        "records only in the committed copy", findings.committed_only, stream=stream
    )
    for heading, rows, template in BLOCKING_SECTIONS:
        entries = getattr(findings, rows)
        if not entries:
            continue
        print(f"{heading} ({len(entries)}):", file=stream)
        for entry in entries:
            print(template.format(*entry), file=stream)
    print(
        "ignored by design (the live store is expected to be ahead): "
        f"{len(findings.live_only)} live-only record(s), "
        f"{len(findings.live_newer)} live-newer status row(s), "
        f"{len(findings.text_differences)} text-field difference(s)",
        file=stream,
    )


def _print_ids(label: str, ids: list[str], *, stream) -> None:
    if ids:
        print(f"{label} ({len(ids)}): {', '.join(ids)}", file=stream)


def report_derived_counts(divergence: Divergence, *, stream) -> None:
    """Print the counter-only differences, which are never a rejection.

    Called on the clean path too: a difference this check decided to tolerate is
    still a difference the operator should be able to see, and silence there is
    how a tolerated case becomes an unexplained one.
    """
    if not divergence.derived_count_differences:
        return
    print(
        "records differing only in a derived counter, not divergence "
        f"({len(divergence.derived_count_differences)}):",
        file=stream,
    )
    for (
        issue_id,
        name,
        live_value,
        committed_value,
    ) in divergence.derived_count_differences:
        print(
            f"  {issue_id}: {name} live={live_value!r} committed={committed_value!r}",
            file=stream,
        )


def report_dependency_classes(divergence: Divergence, *, stream) -> None:
    """Print one block per derived cause, so each carries only the remedy that fits it.

    Ordering is fixed rather than input-dependent so a CI log diffs cleanly, and the unresolvable
    class comes first because it is the only one whose remedy is a command to run.
    """
    if not divergence.dropped_dependencies:
        return
    grouped: dict[str, list[tuple[str, str, str, str | None]]] = {
        UNRESOLVABLE_TARGET: [],
        RETARGETED_EDGE: [],
        ABSENT_EDGE: [],
    }
    for entry in divergence.dropped_dependencies:
        # An unclassified entry means compare() and report() disagree about the edge set, which is
        # a bug rather than a finding. Treat it as unresolvable: that block is the conservative one
        # (do not re-export, the mirror is healthy) and it never suggests deleting anything.
        classification, live_target = divergence.dependency_causes.get(
            entry, (UNRESOLVABLE_TARGET, None)
        )
        grouped.setdefault(classification, []).append((*entry, live_target))

    blocks = (
        (
            UNRESOLVABLE_TARGET,
            "dependency edges with targets that resolve in neither export",
            UNRESOLVABLE_TARGET_REMEDY,
        ),
        (
            RETARGETED_EDGE,
            "dependency edges the live store points at a different target",
            RETARGETED_EDGE_NOTE,
        ),
        (
            ABSENT_EDGE,
            "dependency edges absent from the live store, targets resolve locally",
            ABSENT_EDGE_NOTE,
        ),
    )
    for classification, heading, note in blocks:
        rows = grouped.get(classification) or []
        if not rows:
            continue
        print(f"{heading} ({len(rows)}):", file=stream)
        for issue_id, target, dep_type, live_target in rows:
            if classification == RETARGETED_EDGE:
                print(
                    f"  {issue_id} ({dep_type}): committed={target!r} live={live_target!r}",
                    file=stream,
                )
            else:
                print(f"  {issue_id} -> {target} ({dep_type})", file=stream)
        print(note, file=stream)


def report(divergence: Divergence, *, stream) -> None:
    """Print stable, per-record diagnostics suitable for a CI failure log."""
    _print_ids("committed-only record IDs", divergence.committed_only, stream=stream)
    _print_ids("live-only record IDs", divergence.live_only, stream=stream)
    if divergence.display_id_mismatches:
        print(
            f"display-id disagreements ({len(divergence.display_id_mismatches)}):",
            file=stream,
        )
        for issue_id, live_ref, committed_ref in divergence.display_id_mismatches:
            print(
                f"  {issue_id}: live={live_ref!r} committed={committed_ref!r}",
                file=stream,
            )
    _print_ids(
        "whole-record disagreements", divergence.record_mismatches, stream=stream
    )
    report_dependency_classes(divergence, stream=stream)
    if divergence.migrated_dependencies:
        print(
            "dependency edges whose target form changed, not lost "
            f"({len(divergence.migrated_dependencies)}):",
            file=stream,
        )
        for (
            issue_id,
            committed_target,
            live_target,
            dep_type,
        ) in divergence.migrated_dependencies:
            print(
                f"  {issue_id} -> committed={committed_target!r} "
                f"live={live_target!r} ({dep_type})",
                file=stream,
            )
        print(MIGRATED_DEPENDENCY_NOTE, file=stream)
    report_derived_counts(divergence, stream=stream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "live_export", type=Path, help="fresh bd export from the live store"
    )
    parser.add_argument(
        "committed_export", type=Path, help="committed .beads/issues.jsonl export"
    )
    parser.add_argument(
        "--mirror-retirement-gate",
        action="store_true",
        help=(
            "answer only 'does the committed copy hold anything the live store lacks' "
            "(ISSUE-REF T-00). Exit 0 authorises `git rm --cached .beads/issues.jsonl`; "
            "live-only ids, live-newer rows and text differences are reported and ignored."
        ),
    )
    args = parser.parse_args(argv)
    try:
        live_rows = load_jsonl(args.live_export)
        committed_rows = load_jsonl(args.committed_export)
    except ExportFormatError as exc:
        # Exit 2 before either mode runs, and the wording never resembles an agreement
        # verdict: "could not tell" phrased as "they agree" is how an operator proceeds
        # anyway, and the next thing in T-04's command line is an irreversible `git rm`.
        print(f"beads divergence check: ERROR: {exc}", file=sys.stderr)
        return 2

    if args.mirror_retirement_gate:
        findings = mirror_retirement_findings(live_rows, committed_rows)
        if findings.clean:
            print(
                "mirror retirement gate: OK (the committed copy holds nothing the live "
                "store lacks)"
            )
            report_mirror_retirement(findings, stream=sys.stdout)
            return 0
        print(
            "mirror retirement gate: BLOCKED (the committed copy holds records the live "
            "store lacks; reconcile before untracking)",
            file=sys.stderr,
        )
        report_mirror_retirement(findings, stream=sys.stderr)
        print(TWO_FILE_LIMIT_REMEDY, file=sys.stderr)
        return 1

    divergence = compare(live_rows, committed_rows)

    if divergence.clean:
        print("beads divergence check: OK (exports agree)")
        report_derived_counts(divergence, stream=sys.stdout)
        return 0
    print("beads divergence check: REJECTED (exports diverge)", file=sys.stderr)
    report(divergence, stream=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
