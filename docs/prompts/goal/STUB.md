---
title: "Goal — [FILL title]"
category: goal
status: drafted
goal_kind: goal
autonomy: autonomous
cadence_seconds: null
budget: { activations: 40, no_progress_cycles: 3, wall_clock: null }
char_count: {}
validated_at:
validated_digest:
validator_ruleset:
manifest: null
---

<!-- Condition budget: <= 3900 maximum-count characters. The fenced block opens with the literal
line `/goal` so the whole block pastes as one command (Example User, 2026-10-10, ISSUE-REF). A goal that
carries the fleet's standing in-progress default also runs `/exit-goal` once as the last instruction
of its SUCCESS and BLOCKED branches; see docs/prompts/goal/README.md § Reusable templates. The
`## Launch` section comes first, directly under the title, so the paste-ready prompt needs no
scrolling (Example User, 2026-10-13, ISSUE-REF); render.py fills each section by its heading. -->

## Launch

Paste the fenced block as one command; its first line is `/goal`.

```text
/goal
GOAL
[FILL one observable end state]

CONTEXT
[FILL minimum snapshot]

SCOPE & AUTHORITY
In scope: [FILL]. Out of scope: [FILL]. You may: [FILL]. Human approval required for: none.{{rider}}

GROUND TRUTH
Re-read [FILL] on every activation. Persist progress in [FILL].

SUCCESS — ALL MUST BE TRUE
S1. [FILL predicate]. verify: [FILL verifier]; evidence: [FILL artifact]; authority: [FILL authority].

TERMINATION
SUCCESS: every S-item proven; say GOAL COMPLETE and stop.
BLOCKED: missing permission, credential, decision, dependency, or environment; report the exact blocker and owner action; say GOAL BLOCKED and stop.
BUDGET_EXHAUSTED: report unmet S-items; say GOAL BUDGET EXHAUSTED and stop.
UNSAFE_OR_OUT_OF_SCOPE: do not act; report the boundary; say GOAL OUT OF SCOPE and stop.

STOP CONDITION
Any TERMINATION branch stops this goal, not only SUCCESS. BLOCKED is met when GOAL BLOCKED was said and each unproven S-item waits outside this session (owner, credential, outside party, unreached date, peer, chief's go, PENDING, owner's-call or Authority-escalated decision, built too; a task's why, not its waits-on label, decides), with attempts, or rests only on past conduct or order (like missing figures) no later turn can change, or on a redo no S-item asks for. Only an unreached date's scheduled wakeup is waiting, not pending work; no outcome due; report stays last. An unproven S-item actionable here means BLOCKED is not met.
Evaluator rejections and their /bug-fix bar nothing. No prior or accepted verdict owed. Budget/unsafe/scope exits: trigger+phrase only. Evaluator: JSON object only.
```

## Raw ask

[FILL verbatim ask]

## Coverage

| Raw element | Placed in |
|---|---|
| [FILL] | condition |
