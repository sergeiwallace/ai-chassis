#!/usr/bin/env bash
# PreToolUse(Bash): only worktree_audit.py may remove an agent worktree.
# A registry dependency failure is a deletion-safety failure for an ACTUAL
# deletion-pattern command -- but this bash pre-filter matches the whole JSON
# payload, and every worktree-based session's own `cwd`/`transcript_path`
# already contains the substring "worktree", so the fast-exit below is
# unreachable in practice: nearly every call falls through to Python, which
# re-parses tool_input.command specifically and is the real arbiter (same
# scoping rationale as the sibling guards below). Resolving the script path
# must therefore follow this hook's own symlink back to the repo, the same as
# every sibling guard -- get that wrong and EVERY Bash call in EVERY worktree
# session fails closed, not just deletions (measured live in production).
# `read` builtin, not $(cat): one less spawn per invocation (rationale: enforce-workflow.sh)
_stdin_rc=0
_stdin_t0=$SECONDS
IFS= read -r -d '' -t 2 _wrg_payload || _stdin_rc=$?
# /bin/bash 3.2 (macOS) reports a read -t timeout as 1, the same as EOF; only the
# elapsed time tells a timed-out partial payload from a complete one (ISSUE-REF).
# bash 4+ reports a timeout above 128 itself, and whole-second $SECONDS calls a 1.1s read 2s,
# so the check runs only below bash 4 (ISSUE-REF).
[ "${BASH_VERSINFO[0]}" -lt 4 ] && [ "$_stdin_rc" -eq 1 ] && [ $((SECONDS - _stdin_t0)) -ge 2 ] && _stdin_rc=142
[ "$_stdin_rc" -gt 128 ] && {
  printf 'worktree-removal-guard: UNRESOLVED(stdin-timeout): stdin did not reach EOF within 2s, so the partial payload was not evaluated\n' >&2
  exit 3
}
[ -n "$_wrg_payload" ] || {
  printf 'worktree-removal-guard: UNRESOLVED(no-payload): no hook JSON on stdin within 2s, so nothing was evaluated\n' >&2
  exit 3
}
case "$_wrg_payload" in
  *worktree*|*'rm -rf'*|*'branch -D'*) ;;
  *'"tool_name"'*) exit 0 ;;
  *) ;;
esac

_wrg_src="${BASH_SOURCE[0]}"
while [ -h "$_wrg_src" ]; do
  _wrg_dir="$(cd -P "$(dirname "$_wrg_src")" && pwd)"
  _wrg_src="$(readlink "$_wrg_src")"
  [[ "$_wrg_src" != /* ]] && _wrg_src="$_wrg_dir/$_wrg_src"
done
_wrg_dir="$(cd -P "$(dirname "$_wrg_src")" && pwd)"
_wrg_script="$_wrg_dir/../../scripts/worktree_removal_guard.py"

_wrg_deny() {
  printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Canonical worktree registry check failed; refusing deletion because the guard runtime is unavailable."}}'
  exit 2
}

if [[ ! -f "$_wrg_script" ]]; then
  _wrg_deny
fi

# Prefer the repo venv, exactly as the sibling SessionStart hook does. The bare `python3`
# this used to exec is the SYSTEM interpreter, and on example-project-ec2 that is 3.10.12 while the
# guard's import chain reaches `from datetime import UTC` (3.11+). Every invocation there
# died on ImportError.
_wrg_python="$_wrg_dir/../../.venv/bin/python"
[[ -x "$_wrg_python" ]] || _wrg_python="$_wrg_dir/../../.venv/Scripts/python.exe"
[[ -x "$_wrg_python" ]] || _wrg_python="$(command -v python3 || true)"
if [[ -z "$_wrg_python" ]]; then
  _wrg_deny
fi

# NOT `exec`: the guard's exit status must be TRANSLATED, not inherited. A PreToolUse hook
# blocks on exit 2 and on nothing else -- any other non-zero exit is surfaced as a hook
# error and the command runs anyway. So an `exec` made every crash inside the guard a
# silent fail-open on the one surface standing between an arbitrary `rm -rf` and a live
# canonical worktree (ISSUE-REF). 0 stays 0 so an allow is still an allow; 2 passes through
# with the guard's own reason; anything else becomes a deny.
"$_wrg_python" "$_wrg_script" <<<"$_wrg_payload"
_wrg_rc=$?
case "$_wrg_rc" in
  0|2) exit "$_wrg_rc" ;;
  *) _wrg_deny ;;
esac
