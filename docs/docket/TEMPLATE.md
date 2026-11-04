---
title: "Docket Template"
category: docket
tags: [docket, review-queue, autonomous-batch]
status: template
template_version: "docket-1.0.0"
---

# [Repository] Docket

**Repository:** [repository name]

**Last refreshed:** YYYY-MM-DD

**Ordering:** Within each queue, order ready work by priority (P0 → P3), then by
unblock impact. The score is a decision aid, not ground truth; a
`priority_override` always wins.

## Row-field convention

Every docket row uses these interim fields so the docket can be projected into the
future Beads model without a rewrite:

| Field | Convention |
|-------|------------|
| `bucket` | `review` or `autonomous` |
| `priority` / `value` | P0–P3 band, with its numeric value recorded for scoring |
| `task_id` / `title` | Stable task identifier and concise task title |
| `decision_needed` / `why_ungated` | Review action needed, or why the work is safe without a human gate |
| `doc_path` / `unblock_links` | `doc_path` is a genuine absolute filesystem path (for example `/absolute/path/to/doc.md`, optionally `:line-N`), never a relative or path-less reference; plus typed links or affected work it unblocks |
| `time_criticality` | Relative urgency input |
| `risk_opportunity` | Risk-reduction or opportunity-enablement input |
| `confidence` | Estimate confidence as a percentage |
| `effort` / `size` | Modified-Fibonacci estimate: 1, 2, 3, 5, 8, 13, or 20 |
| `unblock_count` | Derived count of work unblocked by this task |
| `agent_ready` | Boolean gate for Autonomous Batch eligibility |
| `priority_override` | Human ordinal override; always takes precedence over the formula |
| `score` | Derived, never frozen: `confidence × (value + time_criticality + risk_opportunity) × (1 + log(1 + unblock_count)) ÷ effort` |

<!-- doc:region name="review_queue" kind="replaceable" -->

## Review Queue

Items requiring the maintainer's decision, review, or merge. Keep priority→impact order.

| Priority | Task | Title | Decision needed | Absolute doc path | Unblocks | Score inputs |
|----------|------|-------|-----------------|-------------------|----------|--------------|
| P? | TASK-? | [title] | [decision] | /absolute/path/to/doc.md | [typed links / work] | value · time_criticality · risk_opportunity · confidence · effort · unblock_count · agent_ready · priority_override |

<!-- /doc:region name="review_queue" -->

<!-- doc:region name="autonomous_batch" kind="replaceable" -->

## Autonomous Batch

Ready work that is safe to execute without a human gate. Keep priority→impact
order; `agent_ready` must be true.

| Priority | Task | Title | Why ungated | Size | Absolute doc path | Score inputs |
|----------|------|-------|-------------|------|-------------------|--------------|
| P? | TASK-? | [title] | [why safe] | ? | /absolute/path/to/doc.md | value · time_criticality · risk_opportunity · confidence · effort · unblock_count · agent_ready=true · priority_override |

<!-- /doc:region name="autonomous_batch" -->

<!-- doc:region name="provenance_log" kind="append_only" -->

## Provenance Log

| Date | Change | Source / notes |
|------|--------|----------------|
| YYYY-MM-DD | Created | [how this docket was refreshed] |

<!-- /doc:region name="provenance_log" -->
