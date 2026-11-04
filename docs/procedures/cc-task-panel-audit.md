---
title: "Task Panel Audit"
category: procedure
tags: [tasks, audit, handoff, tracking]
status: active
template_version: "procedure-1.0.0"
---

<!-- doc:region name="overview" kind="replaceable" -->

# Task Panel Audit

Audit an open task panel to find stale or duplicate entries, verify durable issue linkage, identify
work that belongs elsewhere, and promote untracked findings into durable work items.

## When to use

Use this when open tasks no longer clearly show which are live, duplicated, blocked, or owned by
another repository or session.

## Delegation prompt

```text
Audit the supplied open task list against the repository's durable issue tracker. Follow its
evidence, classification, and disposition rules. Return a table mapping each task to a disposition,
with a one-line reason and supporting evidence. Do not close or delete tasks yourself; the driving
session applies confirmed changes.
```

## Driving-session checklist

1. Spot-check recommended dispositions against the actual issue tracker.
2. Apply only the confirmed task updates or creations.
3. Leave ambiguous rows open and state what evidence is missing.
4. Remove tasks already confirmed complete through the task-panel cleanup process.
5. Refresh the repository's prioritized docket from the durable tracker, using its canonical row
   schema rather than inventing a local format.

The central rule is independent verification: a worker's report is evidence for a decision, not the
decision itself.

<!-- /doc:region name="overview" -->

<!-- doc:region name="steps" kind="replaceable" -->
<!-- /doc:region name="steps" -->

<!-- doc:region name="rationale" kind="replaceable" -->
<!-- /doc:region name="rationale" -->

<!-- doc:region name="revision_log" kind="append_only" -->
<!-- /doc:region name="revision_log" -->
