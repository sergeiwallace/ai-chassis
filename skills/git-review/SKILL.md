---
name: git-review
description: Review repository worktrees, branches, and cleanup candidates with evidence.
disable-model-invocation: true
---

# git-review

Inventory first; preserve first; delete only when redundancy is proved. This is not a shortcut
around normal code review or integration policy.

Invoking the review means acting on it, within the rules below: integrate what is ready, reclaim
what is proved redundant, and ship or route uncommitted work. The report is a by-product. If the
repository ships a reusable cleanup goal prompt, print its absolute path and ask once whether to
adopt it as a standing goal, then start the review in the same turn without waiting; a decline or
no answer changes nothing.

```bash
git worktree list --porcelain
git branch -vv
git status --short
git stash list
```

For each candidate, identify owner, active process or session, uncommitted changes, upstream
relation, and whether commits are reachable from the integration target. Classify it as retain,
integrate, preserve, delete, or uncertain. An uncertain item is a finding with evidence, not
permission to delete.

Deletion requires no active owner, no dirty or unshipped work, a recoverable reference, and proof
of redundancy. Process liveness, an unknown upstream, a unique commit, a stash, or incomplete
evidence is a delete veto. Preserve before cleanup and verify the backup reference.

```bash
git log --oneline integration-target..candidate
git log --oneline candidate..integration-target
git merge-base --is-ancestor candidate integration-target
```

Upstream tracking answers where a branch expects to publish or merge, not whether it is removable.
Report each action with evidence and recovery reference. Do not silently truncate the inventory.
