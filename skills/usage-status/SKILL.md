---
name: usage-status
description: Show the current Claude Code context window and Claude, Fable, and Codex quota usage with reset times. Use when a session has no visible statusline, including remote-controlled Claude iOS sessions.
argument-hint: ""
model: inherit
---

# usage-status

Show the existing Claude Code status signals on demand when the terminal statusline is not
available.

**Usage:**

```text
/usage-status
```

Run the bundled script with no arguments:

```bash
uv run --script "${CLAUDE_SKILL_DIR}/scripts/usage_status.py"
```

The report reads the existing `ctx-pct`, `quota-pct`, `fable-pct`, and `codex-weekly-pct` JSON
interfaces; it does not recalculate usage. Context and quota readings use the statusline's
green/yellow/red thresholds. Claude and Fable reset times are hour-precision; Codex weekly reset
times keep minutes.

Context-window and Claude-quota numbers reflect the most recent live statusline render (normally
very recent in an active session) and cannot be independently force-refreshed; Codex weekly is
always pulled live when this skill runs.

If the current Claude Code session has no context reading, report it as unavailable and keep the
quota lines. If Fable has no recorded weekly reading, report it as unavailable. If Codex has no
weekly reading yet, report that no Codex usage is recorded.
