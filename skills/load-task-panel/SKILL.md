---
name: load-task-panel
description: Re-display open session tasks in a supported task panel.
---

# load-task-panel

Repopulate the task panel from the session's supported task store after a restart, compaction, or
lost display state. The panel is a convenience view; the tracker and repository evidence remain the
source of truth.

## Behavior

1. Query the current session's task namespace using the supported task-store interface.
2. Select open work only: queued, active, blocked, and review-needed tasks. Do not recreate
   completed work merely because its historical record exists.
3. Render title, state, owner when available, blocking dependency, and a stable identifier.
4. If the store is unavailable, say that the panel could not be restored; do not invent rows from
   memory or stale terminal output.

## Why a plain list is not enough

A plain task list may use a different namespace, omit session-bound tasks, or flatten dependency
state. Use the same identity mapping that created tasks. Verify restored count and sample
identifiers against the source before reporting success.

Confirm an empty source produces an empty panel, an open task appears once, completed tasks do not
reappear, and a missing store yields an explicit degraded result.
