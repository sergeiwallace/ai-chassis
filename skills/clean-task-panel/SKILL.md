---
name: clean-task-panel
description: Remove all completed tasks from the CC TUI task panel.
---

# clean-task-panel

Clears completed tasks from the CC TUI task panel by deleting them from the task database.

**Usage:** `/clean-task-panel`

## Behavior

1. Call `TaskList` to retrieve all tasks.
2. For each task with status `completed`, call `TaskUpdate` with `status: deleted` to remove it.
3. Report how many tasks were cleared.

## Notes

- Only removes `completed` tasks — pending and in_progress tasks are untouched.
- Deletion is permanent in the task database; use only when done tasks are no longer needed for reference.
- Done work stays counted. Beads is the record of finished work, and the beads-ui band's done
  count reads the issue's assignee and `closed_at`, not the panel. Before each deletion, the
  `TaskUpdate` hook fills an empty or human assignee with this session, so clearing the panel never
  drops a session's done count (the Beads assignee owner-sync design).
