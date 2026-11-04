---
name: pr-badge-hygiene
description: Remove this Claude Code session's confirmed closed PR links from the Agents panel.
disable-model-invocation: true
---

# pr-badge-hygiene

Sweep this session's stale, closed PR associations from the Claude Code Agents panel.

**Usage:** `/pr-badge-hygiene`

Run the installed hygiene tool with this session's Claude Code session ID:

```bash
skills_root="$(cd -P "${CLAUDE_SKILL_DIR:-$HOME/.claude/skills}" && pwd)"
python3 "$(dirname "$skills_root")/scripts/claude_pr_hygiene.py" \
  --session-id "${CLAUDE_CODE_SESSION_ID:?Claude Code session ID is required}"
```

It removes only exact `fan` PR-link records whose GitHub state is `MERGED` or `CLOSED`. It
leaves all other job state untouched and cannot clear the native footer badge: that producer has
no confirmed file-backed store, so restart the Claude Code process if its badge remains stale.
