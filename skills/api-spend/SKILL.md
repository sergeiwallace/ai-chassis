---
name: api-spend
description: Report a clearly labeled estimate from locally available usage data.
---

# api-spend

Report the requested usage estimate at the requested grouping, and state that it is an estimate
rather than an invoice. Use configuration-managed rates and report any unpriced model instead of
silently treating it as zero.

Do not publish personal cost history, account identifiers, budget ownership, corpus measurements,
or fleet-wide billing attribution. Synthetic examples may use `example-account` and `$12.34`.
