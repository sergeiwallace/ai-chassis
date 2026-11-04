#!/usr/bin/env bash
# SessionStart hook: auto-invoke /load-task-panel skill on session start so the
# TUI task panel populates immediately without the user having to type
# /load-task-panel manually.
#
# The TUI task panel is per-process and resets on every fresh CC spawn
# (including resume, clear, and post-compact), so this hook fires on ALL
# SessionStart sources. The /load-task-panel skill itself handles the
# delete-and-recreate flow when needed after compaction.

set -euo pipefail

# Drain stdin (CC sends a JSON payload; this hook does not need any field from it)
# `read` builtin, not $(cat): one less spawn per invocation (rationale: enforce-workflow.sh)
IFS= read -r -d '' -t 2 _ || true

printf '%s\n' \
  '<system-reminder>' \
  'SessionStart auto-task-panel hook fired. Invoke the /load-task-panel skill now as your first action so open CC tasks appear in the TUI task panel. Do this silently — do not narrate it to the user.' \
  '</system-reminder>'
