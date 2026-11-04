---
description: When a state change needs two writes that cannot be collapsed into one, name the chosen survival mechanism in the code at the gap — a lock does not protect the operation, and a write-ahead journal beats both a lock and a rollback. Path-scoped — loads only when touching source or shell files.
paths:
  - "**/*.py"
  - "**/*.sh"
  - "**/*.ts"
  - "**/*.tsx"
  - "**/*.js"
---

# Two-phase mutations: name the mechanism at the gap

**If a state change needs two writes because the second depends on the first's result, the gap
between them is a failure mode you are choosing to handle or choosing to leave open.** Say which,
in the code, at the gap. A reader at the call site must be able to see which failures are covered
without inferring it from the absence of anything, because the available mechanisms are not
interchangeable:

- **A lock protects rivals, not the operation.** A dying process *releases* its lock, so a
  `flock` spanning both writes stops a concurrent reader seeing a half-written state and does
  nothing about interruption.
- **Signal deferral covers the measured killer** (`timeout(1)` sends `SIGTERM`) and nothing
  unkillable.
- **A write-ahead journal is the only layer that survives `SIGKILL`.** Record the intent before
  the second write so a later sweep can finish or repair it.
- **Rollback usually makes it worse**, trading a recoverable record for a destroyed one. If you
  take it anyway, disclose the choice rather than making it quietly.

**Recovery must be joint, and the failure message must name the repair tool.** A repair path
nobody can find is indistinguishable from no repair path; the session that hits the failure is
normally blocked and has no reason to guess a sweeper exists.

**Assert nothing about how often interruption strands something.** The governing factor is not
established (an interrupted `git worktree add` was measured *sometimes* rolling back completely),
and "rare enough to ignore" is how the gap gets left open in the first place.

**Prove it by interrupting it, and assert the state is RECOVERABLE rather than clean.** Use fault
injection that fails closed on value and announces itself on stderr, not a lowered timeout, which
proves the path fires on a fast store rather than on the store that fails.

Canonical statement, the mechanism-vs-failure table and the worked `bd-new` example:
the canonical development-workflow procedure's § Survive an interrupted two-phase mutation. Its
sibling, a guard whose refusal does not stop the mutation that follows, is § Gate the mutation
on the guard in the same procedure.
