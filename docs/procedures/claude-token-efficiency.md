# Token Efficiency Guide

Use the least capable model and reasoning level that can complete the task reliably. Escalate only
when uncertainty, impact, or failed focused attempts justify it.

## Model and effort selection

- Use a fast, lower-cost model for classification, search, extraction, and mechanical work.
- Use a general implementation model for ordinary coding and debugging.
- Use deeper reasoning for architecture, difficult diagnosis, or high-consequence decisions.
- Set low effort for deterministic edits; reserve high effort for work needing exploration.

## Context management

Search before reading whole trees. Read files needed for the next decision, summarize completed
decisions, and start a fresh session for unrelated work. Compact or clear after a cohesive task
instead of carrying stale exploration into the next one.

Keep prompts directive and testable: name target, outcome, constraints, and verification. Capture
noisy output in a file and inspect the relevant portion rather than injecting an entire log into
context. Disable unneeded tools so their definitions do not consume prompt space.

## Quick reference

```text
small model + low effort  -> discovery and mechanical work
general model             -> normal implementation
deeper reasoning          -> uncertain or consequential decisions
compact or clear          -> after a completed cohesive task
```

Do not use personal usage histories, account limits, or subscription economics as portable planning
inputs. Measure actual task complexity and verification needs instead.
