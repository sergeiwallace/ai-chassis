---
title: "WSJF-lite/CD3 Scoring Rubric — fleet-wide calibration anchors"
category: procedures
tags: [prioritization, wsjf, beads, curation-metadata, calibration]
status: active
source: ai-harness
---

# WSJF-lite/CD3 Scoring Rubric

The canonical calibration anchors for the ISSUE-REF curation-metadata fields. **Every agent scoring
issues in any fleet repo uses this file verbatim.** Its whole purpose is cross-repo comparability:
if one agent's `value=8` means something different from another's, the composite is noise and the
ranking it produces is worse than no ranking at all.

**Scope (ISSUE-REF).** This rubric governs the five composite *inputs* — `value`, `time_criticality`,
`risk_opportunity`, `effort`, `confidence` — not Beads `priority`. Priority is moving toward becoming
a **derived output** of the composite rather than another hand-scored input (the priority-as-output
direction; the task-prioritization scoring-models research doc §§8–13, and the work-organization-system design doc (ISSUE-REF), AD-2,
approved 2026-07-27). Score the five inputs honestly and do not backfill `value`
from an issue's existing priority band — see below.

Scored fields feed the single composite in `scripts/bd_curation_metadata.py::compute_score`:

```
composite = confidence × (value + time_criticality + risk_opportunity) × (1 + ln(1 + unblock)) ÷ effort
```

All four Fibonacci fields use the modified-Fibonacci enum `{1, 2, 3, 5, 8, 13, 20}`. `confidence`
is an integer percentage 0–100.

## The rule that matters most: score against the FLEET, not the repo

**`value` is fleet-relative, never repo-relative.** A chore in `trip-planner` and a chore in
`ai-harness` that deliver equally little get the same `value`. Do **not** normalise within a repo so
that each repo has its own "top" issue — that is precisely the failure this rubric exists to prevent.
A small leaf project can legitimately have no issue above `value=3`. **Supported by research** —
fixed, shared anchors are a precondition for any cross-repo comparison at all: "fixed global
thresholds only mean the same thing if the operator uses shared component anchors across all ~20
repos; per-repo anchoring would make global `/next` scores incomparable" (task-prioritization-
scoring-models.md, Gaps & blindspots).

**Do not anchor on the issue's existing `bd` priority.** Those bands were largely agent-estimated,
their provenance is unrecoverable (`bd history` authors everything `root`), and they are being
replaced by a derived output. Read the issue and judge it fresh. **Supported by research** — this is
the governing objective of the priority-as-output refinement (task-prioritization-scoring-models.md,
Executive Summary #2 and §10 "Severing the priority-to-value circularity"): feeding the composite's
own eventual output back in as an input would be a circularity, not a feature. The scorer now uses
a flat, priority-independent default for missing `value` (ISSUE-REF / AD-2).

## `value` — worth of the outcome, fleet-wide

| Score | Anchor |
|---|---|
| 1 | Cosmetic. Nobody notices if it is never done. |
| 2 | Minor quality-of-life inside one repo. |
| 3 | A real improvement to one repo's workflow. |
| 5 | Meaningful new capability, or removes recurring friction the operator hits often. |
| 8 | Unblocks other work, or fixes something that actively wastes time on a regular basis. |
| 13 | Fleet-wide capability, or removes a systemic failure class affecting many repos. |
| 20 | Foundational. Other major work is gated on it. Rare — expect very few. |

## `time_criticality` — how fast the value decays if delayed

| Score | Anchor |
|---|---|
| 1 | No decay. Equally valuable in a year. |
| 2 | Mild decay over many months. |
| 3 | Decays over months. |
| 5 | Decays over weeks, or another task is already waiting on it. |
| 8 | Decays over days, or an external deadline lands within a few weeks. |
| 13 | Actively bleeding now — data loss, a broken main path, a live security exposure. |
| 20 | Emergency. Everything else waits. |

## `risk_opportunity` — risk removed or opportunity opened

| Score | Anchor |
|---|---|
| 1 | Neither. |
| 2 | Slight. |
| 3 | Removes a known annoyance. |
| 5 | Prevents a plausible failure, or opens a genuinely useful option. |
| 8 | Prevents a likely and costly failure — data loss, security, corruption, silent wrong results. |
| 13 | Eliminates an entire class of systemic risk. |
| 20 | Existential to the fleet. |

## `effort` — total **agent-execution** effort, not human dev time

Per the fleet's standing rule, estimate in agent-execution time. Include the full cycle the work
actually needs: tests, review, docs.

| Score | Anchor |
|---|---|
| 1 | Under ~15 minutes of agent time. Single file, no tests needed. **Rare — see the caution below.** |
| 2 | ~30 minutes. A few files, trivially verified. |
| 3 | ~1 hour. Contained, some tests. |
| 5 | ~half a day. Multi-file, needs real tests. |
| 8 | ~1 day. Cross-cutting, needs a design decision. |
| 13 | Multi-day. Needs a design doc and phasing. |
| 20 | Multi-week epic. Should probably be split. |

> **Caution on `effort=1`.** Effort is the denominator, so the composite is unbounded as effort
> falls. A task scored `1` gets a 3× boost over one scored `3` and a 13× boost over one scored `13`,
> which is how a trivial task outranks a critical one. Reserve `1` for work that is genuinely
> near-instant *including* verification. If a task needs a test, it is not a `1`. When genuinely torn
> between `1` and `2`, choose `2`.
>
> **Research note (supported, reframed).** Small-effort dominance is not an arithmetic bug — it is
> the intended behavior of weighted-shortest-processing-time scheduling (Smith's rule): ordering by
> weight/duration minimizes total weighted completion time on a single queue
> (task-prioritization-scoring-models.md §11). The Fibonacci floor at `1` is kept for exactly this
> reason; the research's mitigation comparison rejects raising the effort floor, capping the ratio,
> or log-transforming effort "without first deciding that CD3's objective is wrong for this
> backlog." The caution above does not change the formula's denominator — it counters a *different*,
> also research-documented failure mode: deflating an effort estimate to inflate the score (§7,
> score-gaming). Score effort honestly; do not inflate an estimate to protect a task from its own
> low score.

## `confidence` — how well understood the task is (0–100)

Multiplies the whole numerator, so it is the honest hedge on a shaky estimate.

| Range | Anchor |
|---|---|
| 90–100 | Fully specified. Acceptance criteria are clear and current. |
| 70–85 | Clear intent, a few unknowns in the how. |
| 50–65 | Understood in principle, but scope is fuzzy. |
| 30–45 | Vague, or written long ago and possibly stale. |
| 10–25 | Unclear whether it is even still relevant. Flag it rather than guessing. |

**Judgement, not a calibrated probability.** The discount-multiplier *concept* is research-supported
(RICE's confidence factor is its one genuinely additive idea over ICE, §2); this five-band scale and
its exact percentages are this rubric's own judgement, not derived from RICE's coarser four-tier
scale (100/80/50/<50) or from any outcome data. Do not read `confidence=70` as "this task has a 70%
chance of succeeding" — calling it calibrated without a defined, tracked outcome would be false
precision (task-prioritization-scoring-models.md §14).

## Method

1. **Read the issue** — title, description, acceptance criteria, labels, comments.
2. **Check the code when it cites any** — if it names files, functions, or a design doc, look. An
   issue that is already substantively done should be reported, not scored as live work.
3. **Score all five fields.** Never leave one to its default; a partially-scored issue is marked
   `unscored` by the scorer and ranks on fallbacks rather than on your judgement.
4. **Write via the sanctioned path only:**
   ```
   python3 /workspace/projects --directory /workspace/projects \
     set <issue_id> --value N --time-criticality N --risk-opportunity N --effort N --confidence N
   ```
   It validates the enums and rejects malformed values. Never hand-write `bd update --metadata` for
   these fields.

   To set an explicit sequencing override, add `--priority-override N` (where `N` is 0–3) to the
   same command. It is the sole human sequencing input; priority otherwise remains derived from
   the composite.

## Grounding against research (ISSUE-REF)

Per-anchor status against
the task-prioritization scoring-models research doc (refined 2026-07-26 around priority-as-output). Every anchor below is either grounded in a cited
research finding, explicitly marked judgement, or was changed as a direct result of this
reconciliation — none is silently unsourced.

| Anchor / rule | Status | Basis |
|---|---|---|
| Score `value` fleet-relative, never repo-relative | Supported | Gaps & blindspots: fixed global thresholds only mean the same thing if the operator uses shared component anchors across all ~20 repos; per-repo anchoring would make global `/next` scores incomparable. |
| Do not anchor `value` on the issue's existing `bd` priority | Supported | Executive Summary #2 + §10: priority is a materialized output, never a scoring input; ISSUE-REF/AD-2 removed the circular fallback and uses a flat default for missing `value`. |
| `value` 1–20 point anchor wording | Judgement | Research names no standardized WSJF value-band wording and explicitly leaves it to the owner (Open Question 1: "the owner must supply the semantic anchors"). The three Cost-of-Delay *components* (value / time-criticality / risk-opportunity) are research-established (§1, SAFe primary source); only each point's wording is this rubric's judgement. |
| `time_criticality` 1–20 point anchor wording | Judgement | Same basis as `value` above. |
| `risk_opportunity` 1–20 point anchor wording | Judgement | Same basis as `value` above. |
| `effort` 1–20 point anchor wording (agent-execution-time framing) | Judgement | The Fibonacci scale itself is research-established (§1); the agent-execution-time convention is this fleet's own standing rule, not sourced from the WSJF/RICE literature. |
| Caution on `effort=1` (reserve for genuinely near-instant work) | Supported, reframed | §11 "Small-effort dominance": this is intended CD3/Smith's-rule behavior, not a bug to route around — the caution counters score-gaming (deflating effort to inflate score), a distinct, research-documented anti-pattern (§7), not the dominance itself. |
| `confidence` five-band percentage scale | Judgement | The discount-multiplier *concept* is research-supported (RICE's confidence factor, §2); the exact five-band scale and percentages are this rubric's own judgement, not RICE's four-tier scale and not derived from outcome data. §14 warns explicitly against reading it as a calibrated probability. |
| Method step 3 — never leave a field to its default; mark `unscored` | Supported | §10 "Treatment of incomplete records" recommends an explicit unscored state over any imputation strategy — directly matches. |
| Method step 4 — write via the sanctioned script only | Supported | The sanctioned path validates and writes both the five scored fields and `priority_override` (ISSUE-REF / AD-2). |
| Single-rater calibration honest limit | Supported, expanded | §14: inter-rater controls are *unavailable*, not merely weaker, at n=1; only a specific subset of mechanisms transfer (see below). |
| `unblock` not scored here (native `dependent_count`) | Supported | §6/§11: `f(unblock_count) = 1 + ln(1 + n)` is an explicit, un-tuned inference, consistent with treating unblock as structural/derived rather than a rubric anchor. |

## Honest limits of this rubric

- **Single-rater calibration — research-grounded (task-prioritization-scoring-models.md §14).**
  When one rater supplies every input, inter-rater agreement is *unavailable*, not merely weaker —
  mechanisms that need multiple raters (planning-poker consensus, inter-rater reliability statistics,
  stakeholder-weight aggregation) do not transfer to n=1 at all. What does transfer: stable reference
  exemplars (the tables above), blind re-scoring of a sample without seeing the prior score to
  measure drift, and comparing predicted effort buckets against realized cycle time. None of those
  controls are wired up yet — flagged by this reconciliation, not built by it.
- **The anchors are judgement, not measurement.** They were authored to be self-consistent, not
  derived from outcome data. When enough issues have been completed, the bands should be checked
  against how long work actually took and revised.
- **`unblock` is not scored here.** It comes from Beads' native `dependent_count`, recomputed on
  every read.
- **`priority_override` is an explicit exception.** Set it only through the sanctioned command's
  `--priority-override` flag; it wins over the derived priority for ordering and materialization.
