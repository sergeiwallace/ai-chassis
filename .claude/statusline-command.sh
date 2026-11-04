#!/usr/bin/env bash
set -u

# Portable status line: clock | model | project:branch | context percentage.
# Optional fields degrade to `--` without invoking fleet-only helpers.
# `timeout` is coreutils, and macOS ships neither it nor `gtimeout`. So this read exited 127
# before `cat` ran, `2>/dev/null` ate the shell's "command not found" and `|| true` ate the 127:
# the payload came back EMPTY and every field this script advertises degraded at once. Measured
# 2026-09-19 with a full payload on stdin, this printed `model | ... | ctx --%` -- the literal
# word "model" and no context number -- on the one file whose stated purpose is portability.
#
# The bound exists only for Windows Git Bash, where Claude Code leaves the stdin pipe open and a
# plain `cat` therefore never returns; on macOS and Linux the pipe closes on its own. So bound the
# read when a bounding binary is present and read plainly when none is, rather than making the
# read itself depend on coreutils being installed.
_read_payload() {
  if command -v timeout >/dev/null 2>&1; then timeout 2 cat 2>/dev/null; return 0; fi
  if command -v gtimeout >/dev/null 2>&1; then gtimeout 2 cat 2>/dev/null; return 0; fi
  cat 2>/dev/null
}
payload="$(_read_payload || true)"

if command -v jq >/dev/null 2>&1 && [[ -n "$payload" ]]; then
  model="$(jq -r '.model.display_name // .model // "model"' <<<"$payload" 2>/dev/null || true)"
  current_dir="$(jq -r '.workspace.current_dir // .cwd // empty' <<<"$payload" 2>/dev/null || true)"
  used="$(jq -r '.context_window.used_percentage // empty' <<<"$payload" 2>/dev/null || true)"
else
  model="model"
  current_dir="${PWD:-.}"
  used=""
fi

project="$(basename "${current_dir:-.}")"
branch="$(git -C "${current_dir:-.}" branch --show-current 2>/dev/null || true)"
branch="${branch:-detached}"

if [[ "$used" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  used_int="${used%%.*}"
  if (( used_int >= 80 )); then
    context_color='\033[31m'
  elif (( used_int >= 50 )); then
    context_color='\033[33m'
  else
    context_color='\033[32m'
  fi
  context="${context_color}${used_int}%\033[0m"
else
  context="--%"
fi

printf '%s | %s | %s:%s | ctx %b\n' "$(date '+%H:%M')" "$model" "$project" "$branch" "$context"
