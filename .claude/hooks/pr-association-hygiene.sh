#!/usr/bin/env bash
# PostToolUse(Bash): reconcile closed PR links after CC runs a merge/close command.
#
# The hook is intentionally event-driven rather than a daemon: it only runs after
# commands that can close a PR or remove its worktree, and it never blocks a session.
set -euo pipefail

# `read` builtin, not $(cat): one less spawn per invocation (rationale: enforce-workflow.sh)
_stdin_rc=0
_stdin_t0=$SECONDS
IFS= read -r -d '' -t 2 payload || _stdin_rc=$?
# /bin/bash 3.2 (macOS) reports a read -t timeout as 1, the same as EOF; only the
# elapsed time tells a timed-out partial payload from a complete one (ISSUE-REF).
# bash 4+ reports a timeout above 128 itself, and whole-second $SECONDS calls a 1.1s read 2s,
# so the check runs only below bash 4 (ISSUE-REF).
[ "${BASH_VERSINFO[0]}" -lt 4 ] && [ "$_stdin_rc" -eq 1 ] && [ $((SECONDS - _stdin_t0)) -ge 2 ] && _stdin_rc=142
[ "$_stdin_rc" -gt 128 ] && {
  printf 'pr-association-hygiene: UNRESOLVED(stdin-timeout): stdin did not reach EOF within 2s, so the partial payload was not evaluated\n' >&2
  exit 3
}
[ -n "$payload" ] || {
  printf 'pr-association-hygiene: UNRESOLVED(no-payload): no hook JSON on stdin within 2s, so nothing was evaluated\n' >&2
  exit 3
}
case "$payload" in
  *'"tool_name"'*'"Bash"'*) ;;
  *) exit 0 ;;
esac
case "$payload" in
  *'gh pr merge'*|*'gh pr close'*|*'git worktree remove'*|*'git branch -d'*|*'git branch -D'*) ;;
  *) exit 0 ;;
esac

source="${BASH_SOURCE[0]}"
while [ -h "$source" ]; do
  directory="$(cd -P "$(dirname "$source")" && pwd)"
  source="$(readlink "$source")"
  [[ "$source" != /* ]] && source="$directory/$source"
done
directory="$(cd -P "$(dirname "$source")" && pwd)"
script="$directory/../../scripts/claude_pr_hygiene.py"

[[ -f "$script" ]] || exit 0
exec python3 "$script" --all-sessions --quiet
