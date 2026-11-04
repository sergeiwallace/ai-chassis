---
name: quality-check
description: Review uncommitted changes against project quality standards (read-only)
---

# quality-check

**Ask fidelity: Tier 2 when scope is reviewed.** Route external review through `/delegate` with the
request/root and diff; otherwise mark scope-alignment checks unverified.

Review code changes using the LangGraph orchestrator (`hlg review`).

**Usage:** `/quality-check` or `/quality-check "focus on security"` or `/quality-check --jira PROJECT-NNN`

## Instructions

1. **Run hlg review** — Execute the parallel review workflow:

```bash
hlg review
```

With optional focus or Jira posting:

```bash
hlg review "focus on error handling"
hlg review --jira PROJECT-NNN
```

This launches parallel reviews:
- **Quality review** (Claude) — scope, code quality, AI slop detection, test coverage
- **Architecture review** (non-author family) — patterns, extensibility, cross-system concerns
- Both reviews are merged into a unified report

1. **Present results** — Show the merged review report to the user.

If `hlg` is not available, fall back to a manual review:
1. Run `git diff` and `git diff --cached` to see all changes
1. Check against: scope, code quality, AI slop, and test coverage
1. Report findings as a checklist with [PASS] or [FLAG] for each criterion
1. Do NOT make any changes — this is a read-only review
