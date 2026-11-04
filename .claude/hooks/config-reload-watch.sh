#!/usr/bin/env bash
# FileChanged hook (config-reload): a watched CLAUDE.md changed on disk during the
# session. Record a per-session sentinel; config-reload-check.sh surfaces a
# one-time, non-blocking notice at the next prompt.
#
# FileChanged is side-effect-only (it cannot block or inject model context), so the
# surfacing is deferred to the UserPromptSubmit hook. Near-zero cost: no external
# interpreters.

INPUT=""; IFS= read -r -d '' -t 2 INPUT || true

SESSION_ID=""
if [[ "$INPUT" =~ \"session_id\"[[:space:]]*:[[:space:]]*\"([^\"]+)\" ]]; then
  SESSION_ID="${BASH_REMATCH[1]}"
fi
[[ -z "$SESSION_ID" ]] && SESSION_ID="unknown"
# Defensive: never let a stray / \ or .. in session_id escape the state dir (F-06).
case "$SESSION_ID" in *[/\\]* | *..*) SESSION_ID="unknown" ;; esac

# Best-effort: capture which file changed for a more useful notice.
FILE_PATH=""
if [[ "$INPUT" =~ \"file_path\"[[:space:]]*:[[:space:]]*\"([^\"]+)\" ]]; then
  FILE_PATH="${BASH_REMATCH[1]}"
fi

STATE_DIR="$HOME/.claude/state"
mkdir -p "$STATE_DIR"
printf '%s\n' "${FILE_PATH:-CLAUDE.md}" >> "$STATE_DIR/config-changed-${SESSION_ID}"
exit 0
