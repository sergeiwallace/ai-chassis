---
title: "[Git review report title]"
category: analysis
tags: [analysis, git-review]
status: draft
template_version: "analysis-1.0.0"
created-by: "[session or operator]"
last-updated-by: "[session or operator]"
machine-id: "[machine identifier]"
created-at: "YYYY-MM-DDTHH:MM:SSZ"
last-updated-at: "YYYY-MM-DDTHH:MM:SSZ"
---

# [Git review report title]

## Public portability inventory

Must survive: complete inventory reporting, explicit comparison refs and distances, fail-closed
liveness and remote authority checks, distinct upstream and orphan verdicts, backup-proof logging,
and append-only provenance. Intentionally dropped: organization-specific branch conventions,
machine names, private repository paths, user names, task references, and measured local examples.

## Overview

[What this pass covered, its outcome, actions taken, and material evidence limits.]

## Context

[Why the sweep ran, any relevant prior report, and the authorization boundary.]

## Scope and method

List repositories inspected, how each integration target was resolved, probes used, and every
skipped or bounded part of the work. `origin/HEAD` is a fallback, not proof that it is the merge
target. State what an inadequate liveness or remote probe would have reported if it were wrong.

## Inventory: worktrees, branches, and stashes

Use one status per row: `uncommitted-changes`, `in-progress`, `needs-pull-conflict-resolution`,
`stale-superseded`, `ready-to-merge-push`, or `merged-cleanup-eligible`. Record both distances with
their named refs; an unresolved target is `UNRESOLVED`, never a zero count.

| Item type | Repository | Name | Path or ref | Distances | Status | Evidence and disposition |
| --- | --- | --- | --- | --- | --- | --- |
| [worktree / branch / stash] | [repo] | [name] | [path or ref] | [counts + refs] | [status] | [proof or follow-up] |

## Upstream tracking

Use exactly `ok`, `self`, `absent`, `wrong-target`, or `unchecked`. The question is whether the
upstream represents the branch's integration target. A self-referential worktree upstream can
produce a false all-clear; a long-lived integration branch can legitimately track itself. Report
repairs separately from rows deliberately left unchanged, and never repair when the target is
ambiguous or the worktree is live.

| Repository | Branch | Was | Now | Verdict | Evidence |
| --- | --- | --- | --- | --- | --- |
| [repo] | [branch] | [old upstream] | [new or unchanged] | [verdict] | [counts and refs] |

## Local orphaned branches

Classify each row as `fully-applied`, `genuinely-unique`, `partially-applied`, `content-landed`,
`shipped-by-merge`, or `unknown`. Patch equivalence is one-directional evidence: an applied result
is useful, while unique patches can still be squash-merged. The sweep re-checks unique and partial
rows with the content proof and reports `content-landed` when every change is on the target.
Ambiguous classifications are not actionable.

| Repository | Branch | Tip | Unique | Applied | Classification | Disposition and evidence |
| --- | --- | --- | --- | --- | --- | --- |
| [repo] | [branch] | [short commit] | [count] | [count] | [classification] | [recommendation] |

## Orphan directories

Discover these by walking worktree roots rather than asking Git. Prove both byte identity against a
commit and that every on-disk file is represented there. An unreadable liveness probe is live.

| Repository | Path | Bytes / files | Proof closed? | Liveness | Disposition |
| --- | --- | --- | --- | --- | --- |
| [repo] | [path] | [size / count] | [yes / no] | [live / idle / unknown] | [action or follow-up] |

## Remote-branch authority

Record the policy grant, retention indication, authorship probe, and open-review probe before
evaluating redundancy. An unqueryable authority or review service blocks deletion.

| Repository | Remote | Grant | Authorship probe | Open-review probe | Consequence |
| --- | --- | --- | --- | --- | --- |
| [repo] | [remote] | [granted / report-only] | [result] | [result] | [permitted or blocked] |

## Actions and proofs

[For every destructive action, state the evidence rechecked immediately before it and the backup
reference read back before and after it. State retained work and why it was retained.]

## Findings and flags

[List unresolved, skipped, or unavailable evidence with the information needed for a later decision.]

## Options and recommendation

**Recommended:** [next action and why]

**Chosen:** [owner decision] · **Diverged?** [Yes / No]

<!-- doc:region name="update_log_appendix" kind="append_only" -->

## Update log / appendix

Append only. Add one entry for every pass and preserve all earlier entries.

### Pass [N] — [YYYY-MM-DD]

- **created-by:** [session or operator]
- **last-updated-by:** [session or operator]
- **machine-id:** [machine identifier]
- **created-at:** [YYYY-MM-DDTHH:MM:SSZ]
- **last-updated-at:** [YYYY-MM-DDTHH:MM:SSZ]
- **Summary:** [scope, actions, retained work, and flags]

<!-- /doc:region name="update_log_appendix" -->
