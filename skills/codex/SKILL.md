---
name: codex
description: Delegate one bounded task through the repository's approved worker wrapper.
---

# codex

Use a worker for a bounded deliverable with clear ownership and observable acceptance criteria. The
coordinator retains scope control, integration, remote operations, and final judgment.

## Choose a role and effort

Use read-only review for evidence-backed findings and read-only research for sourced facts. Use
implementation for scoped changes in an isolated worktree. Choose low effort for mechanical work,
medium for ordinary implementation, and higher effort only for material ambiguity or design impact.

## Delegation brief

Every brief must state:

- the objective and one completed logical deliverable;
- authoritative inputs and repository-relative files in scope;
- explicit non-goals and authority limits;
- acceptance criteria and focused verification commands; and
- changed files, commands and results, and remaining risks.

Inspect existing changes and avoid overlapping write ownership. Tell a worker to preserve unrelated
changes, avoid destructive or remote actions unless authorized, and surface missing decisions rather
than guessing. After it returns, inspect the diff and run verification; a successful summary is
evidence to review, not integration approval.
