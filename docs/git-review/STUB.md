---
title: "[FILL: Git review run title]"
category: subagent-brief
tags: [subagents, delegation, git-review, worktrees, branches]
status: stub
template_version: "subagent-brief-1.0.0"
related_docs:
  - docs/git-review/TEMPLATE.md
---

# Git-review agent brief

Use this fill-in scaffold when delegating a repository sweep. It carries the destructive-action
vetoes that a worker needs; use the paired git-review report template for the deliverable shape.

## Public portability inventory

Must survive: canonical-worktree protection, process-CWD liveness, anchor-before-delete,
no-force and no-raw-Git safeguards, merge-target-aware upstream review, evidence requirements,
and report/template validation. Intentionally dropped: organization names, host paths, user names,
machine identifiers, task references, measured fleet examples, and private tool locations.

## Fill before launch

```text
Scope: [FILL: repositories or explicit repository list]
Mode: [FILL: reclaiming | dry-run | surface-only]
Narrowed classes: [FILL: deletion classes, or none]
Report path: docs/git-review/[FILL: date]-git-review-[FILL: slug].md
Repeat pass: [FILL: new report | existing report path]
Human authorization: [FILL: quoted authorization for a gated action, or none]
```

Do not infer authorization. Discarding changes and editing ignore rules remain human gates. A local
branch whose tip is proved redundant (an ancestor of the target, or anchored and journalled by the
retirement tool) is retired without asking. Without a quoted decision for a gated item, surface it
with its evidence.

## Mandatory safety controls

### Canonical session worktrees are never deleted

Identify every canonical session worktree afresh before the sweep. A canonical worktree remains
protected regardless of cleanliness, merge status, or apparent inactivity. Match both its directory
and branch convention, including detached worktrees. A generic convention alone is insufficient:
the brief must list the currently protected worktrees and the time the inventory was collected.

### Process liveness is a delete veto

Treat a live process whose current working directory is inside a worktree as an absolute delete
veto. Probe both the candidate and enclosing worktree, and repeat the probe immediately before an
action. An unavailable, incomplete, or unreadable probe is LIVE, not idle. Repository status is not
liveness evidence because an active worker can have a clean tree between commits.

### Preserve first; delete only through the guarded tool

Do not use raw worktree or branch deletion commands. Use the repository's guarded cleanup tool,
which must create and read back a local backup reference before and after deletion. An anchor is a
recovery precondition, not proof that unique work is expendable: unique or ambiguous work stays.
There is no force, confirmation-bypass, or proof-skipping option.

### Upstream tracking answers a specific question

The useful question is whether a branch is merged into its integration target, not merely whether
it has an upstream. Report `self`, `wrong-target`, `absent`, `ok`, or `unchecked` exactly as the
tool defines them. A self-referential upstream can hide unreviewed commits; an integration branch
tracking its own remote can be correct. Name each comparison ref beside its count. If a target does
not resolve, report `unchecked` and do not repair or reclaim.

Repairing local upstream configuration is permitted only where repository policy grants it and the
worktree is not live. It must not push, move a branch tip, or guess a target.

## Evidence discipline

- Treat a patch-equivalence positive as a candidate, not deletion proof; squash merges and merge
  commits can defeat that heuristic. Cross-check with ancestry and content evidence.
- Each finding includes the comparison ref, counts, changed paths or unique patch evidence, and
  liveness result. A human must not have to repeat the investigation to make a decision.
- State what a failed probe would have reported. If failure is indistinguishable from success, fix
  the probe or report the result as unknown.
- State every skipped repository, bound, unreachable remote, and unqueryable service. Partial work
  must never be described as a complete sweep.
- Record each action beside the proof and backup reference that authorized it.

## Deliverable

Write one report at the stated path using the paired git-review report template. On a repeat pass,
preserve prior report content and append a new update entry. Before completion, validate the report
with the repository's document validator. If this brief and the template disagree about required
fields, repair the template in the same change and record the repair; do not silently omit a
requirement.
