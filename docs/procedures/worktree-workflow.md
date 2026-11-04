# Git Worktree Workflow

Use a separate Git worktree for each concurrent write-capable task. It is an independent checkout
on its own branch and prevents one session's edits, index, and branch changes contaminating another.

## Start a task

1. Read contribution guidance and identify the integration branch.
2. Update it using the repository-approved synchronization procedure.
3. Create a task branch and worktree:

```bash
git worktree add -b fix/parser ../example-repo-fix-parser origin/main
cd ../example-repo-fix-parser
```

Keep all writes, tests, and commits for the task in that worktree. One worker owns one bounded
deliverable; coordinate before two workers change the same file.

## Integrate and clean up

Review the task branch and run required checks before integration. A merge alone is not cleanup
proof. Before removal, verify:

```bash
git worktree list --porcelain
git -C ../example-repo-fix-parser status --short
git log --oneline origin/main..fix/parser
```

Confirm no active process or session is using it, it has no unshipped work, its commits are
reachable from the target or preserved, and no worker needs it. Only then use `git worktree remove`.
If proof does not close, retain it and report the evidence. Create and commit in each repository
that owns files; never commit another repository's changes from an unrelated checkout.
