#!/usr/bin/env bash
# Focused offline checks for cx-agent-lifecycle.sh's own child deadline. Run with:
#   bash .claude/hooks/tests/test-cx-agent-lifecycle-deadline.sh
#
# ISSUE-REF established the rule this enforces: a synchronous interactive hook that
# is fail-open on EXIT is still not fail-open on TIME. An unbounded child consumes
# the whole Claude Code per-hook budget and the platform then prints a user-visible
# "hook timed out after Ns -- output discarded" warning. The wrapper must therefore
# bound its own child beneath that budget and exit 0 itself.
#
# The interpreter and module tree this hook launches live on the repo checkout,
# which on example-project-sagemaker is an EFS mount: measured 177-206 ms warm but 5628 ms
# for the module import alone on a cold or contended read, against a 5 s budget.
#
# `supervise` is deliberately exempt: it is the long-running per-lease monitor, not
# a hook, and bounding it would kill the primary TTL enforcer.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HOOK="$REPO/.claude/hooks/cx-agent-lifecycle.sh"
[[ -f "$HOOK" ]] || { echo "hook under test not found: $HOOK" >&2; exit 1; }
SANDBOX="$(mktemp -d)" || { echo "unable to create test sandbox" >&2; exit 1; }
[[ -n "$SANDBOX" && "$SANDBOX" == /* ]] || { echo "unsafe sandbox path" >&2; exit 1; }
trap 'rm -rf -- "$SANDBOX"' EXIT
# This suite's own outer bound, which must not depend on coreutils. macOS ships neither `timeout`
# nor `gtimeout`, so the hard requirement that used to stand here made this file exit 1 on a Mac
# -- reporting "timeout command is required for this test" instead of checking the deadline it
# exists to check, on the platform where the hook's own bound was ALSO missing. A test that
# cannot run where the defect lives is not coverage. Returns 124 on expiry, like coreutils; the
# resolution order means a coreutils host still exercises the real binary.
outer_bounded() {
  local _s="$1"; shift
  if command -v timeout >/dev/null 2>&1; then timeout -k 2s "${_s}s" "$@"; return $?; fi
  if command -v gtimeout >/dev/null 2>&1; then gtimeout -k 2s "${_s}s" "$@"; return $?; fi
  local _sleep=sleep
  command -v sleep >/dev/null 2>&1 || _sleep=/bin/sleep
  # `<&0` is not redundant: bash redirects an ASYNCHRONOUS command's stdin from /dev/null in a
  # non-interactive shell, so the bare `"$@" &` silently discarded a piped or here-string payload
  # -- measured: a JSON payload reaching the bound arrived EMPTY at the child. Naming fd 0
  # explicitly overrides that default, and is a no-op when the caller supplies no stdin.
  set -m; "$@" <&0 & local _p=$!; set +m
  local _n=0
  while (( _n < _s )); do kill -0 "$_p" 2>/dev/null || break; "$_sleep" 1; _n=$(( _n + 1 )); done
  if ! kill -0 "$_p" 2>/dev/null; then local _rc=0; wait "$_p" || _rc=$?; return "$_rc"; fi
  { kill -KILL "-$_p" 2>/dev/null || kill -KILL "$_p" 2>/dev/null || true
    wait "$_p" || true; } 2>/dev/null
  return 124
}

PASS=0
FAIL=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# Build a fixture checkout whose ".venv/bin/python" is a slow stub, then install a
# real copy of the hook under test into it. The wrapper derives its root from its
# own location, so it resolves the stub interpreter rather than the real one. A
# copy (not a symlink) is used so the shipped file's own bytes are what runs.
#
# The stub answers `-V` like a real Python and sleeps for everything else. That is
# not decoration: the hook resolves its interpreter through _python-resolver.sh,
# which PROVES a candidate by what it prints, so a stub that printed nothing would be
# rejected (correctly -- that is the 0-byte forged-success case, ISSUE-REF). The
# checks below would then all measure the fallback path instead of the bound, and the
# two `>= Ns` negative constraints would fail outright. The resolver is copied in
# alongside the hook for the same reason.
build_fixture() {
  local sleep_seconds="$1" fixture
  fixture="$SANDBOX/fixture-$sleep_seconds-$RANDOM"
  mkdir -p "$fixture/.venv/bin" "$fixture/scripts" "$fixture/.claude/hooks"
  printf '%s\n' '#!/bin/bash' \
    'if [[ "${1:-}" == "-V" ]]; then printf "Python 3.99.0\n"; exit 0; fi' \
    "/bin/sleep $sleep_seconds" 'exit 0' > "$fixture/.venv/bin/python"
  chmod +x "$fixture/.venv/bin/python"
  : > "$fixture/scripts/cx_agent_lifecycle.py"
  cp "$HOOK" "$fixture/.claude/hooks/cx-agent-lifecycle.sh"
  cp "$REPO/.claude/hooks/_python-resolver.sh" "$fixture/.claude/hooks/_python-resolver.sh"
  printf '%s' "$fixture"
}

PAYLOAD='{"session_id":"11111111-2222-3333-4444-000000000abc","hook_event_name":"UserPromptSubmit","cwd":"/projects/repo"}'

# Runs the fixture hook and sets RC/ELAPSED. Uses an outer bound well above the
# assertion threshold so an unbounded child fails the assertion instead of hanging
# this suite indefinitely.
run_fixture() {
  local fixture="$1" outer="$2"
  shift 2
  local start end
  start="$(date +%s)"
  OUT="$(printf '%s' "$PAYLOAD" | outer_bounded "$outer" \
    /bin/bash "$fixture/.claude/hooks/cx-agent-lifecycle.sh" "$@" 2>/dev/null)"
  RC=$?
  end="$(date +%s)"
  ELAPSED=$((end - start))
}

echo "== cx-agent-lifecycle child deadline =="

# The reported defect. The sweep's manifest budget is 5s, so a slow child must be
# abandoned below that and the wrapper must still exit 0 on its own.
FIX_SLOW="$(build_fixture 12)"
run_fixture "$FIX_SLOW" 20 sweep --source UserPromptSubmit
{ [[ "$RC" -eq 0 ]] && [[ -z "$OUT" ]] && [[ "$ELAPSED" -lt 5 ]]; } \
  && ok "slow sweep child is abandoned below the 5s hook budget (${ELAPSED}s < 5s)" \
  || bad "slow sweep child exceeded the hook budget (RC=$RC elapsed=${ELAPSED}s OUT=$OUT)"

# Negative constraint: the bound must not be applied to the supervisor, which is
# expected to outlive any hook budget. Over-fixing here would kill lease teardown.
FIX_SUPERVISE="$(build_fixture 7)"
run_fixture "$FIX_SUPERVISE" 20 supervise cxa1-0000000000000000
{ [[ "$RC" -eq 0 ]] && [[ "$ELAPSED" -ge 6 ]]; } \
  && ok "supervise is left unbounded (${ELAPSED}s >= 6s)" \
  || bad "supervise was bounded like a hook (RC=$RC elapsed=${ELAPSED}s)"

# Negative constraint: owner-stop carries a 10s budget because it must durably
# write its tombstone, so it must not be truncated at the sweep deadline.
FIX_OWNER="$(build_fixture 6)"
run_fixture "$FIX_OWNER" 20 owner-stop --source SessionEnd
{ [[ "$RC" -eq 0 ]] && [[ "$ELAPSED" -ge 5 ]]; } \
  && ok "owner-stop is not truncated at the sweep deadline (${ELAPSED}s >= 5s)" \
  || bad "owner-stop was cut short (RC=$RC elapsed=${ELAPSED}s)"

# The ordinary path must be unaffected: a fast child still runs to completion.
FIX_FAST="$(build_fixture 0)"
run_fixture "$FIX_FAST" 20 sweep --source SessionStart
{ [[ "$RC" -eq 0 ]] && [[ "$ELAPSED" -lt 3 ]]; } \
  && ok "fast sweep child completes normally (${ELAPSED}s)" \
  || bad "fast sweep child regressed (RC=$RC elapsed=${ELAPSED}s)"

# A PATH holding the ordinary externals but NO `timeout`/`gtimeout` -- which is a stock macOS,
# and is what these last two checks are about. An EMPTY PATH was used here before and it made the
# check vacuous: without `dirname` the wrapper resolved its own root to `/`, took the `python3`
# fallback, and failed in 0.2s at interpreter lookup, so it never reached the bounding code the
# check is named after. Symlinks, not copies, so the real binaries run.
NO_TIMEOUT_BIN="$SANDBOX/no-timeout-bin"
mkdir -p "$NO_TIMEOUT_BIN"
for _tool in dirname readlink sleep basename; do
  _resolved="$(command -v "$_tool" 2>/dev/null)" || continue
  ln -sf "$_resolved" "$NO_TIMEOUT_BIN/$_tool"
done
command -v timeout >/dev/null 2>&1 && [[ -e "$NO_TIMEOUT_BIN/timeout" ]] \
  && { echo "sandbox PATH must not contain timeout" >&2; exit 1; }

# Fail-open must not depend on `timeout` existing: the wrapper must still exit 0.
FIX_NOTIMEOUT="$(build_fixture 0)"
OUT="$(printf '%s' "$PAYLOAD" | PATH="$NO_TIMEOUT_BIN" \
  /bin/bash "$FIX_NOTIMEOUT/.claude/hooks/cx-agent-lifecycle.sh" sweep --source SessionStart 2>/dev/null)"
RC=$?
[[ "$RC" -eq 0 ]] \
  && ok "absent timeout binary: still fail-open" \
  || bad "absent timeout binary changed the exit status (RC=$RC)"

# The bound itself must survive an absent `timeout`, not merely the exit status. This is the
# assertion that distinguishes the coreutils-free fallback from what preceded it: the same slow
# child as the first check, with `timeout` unreachable. Before the fallback existed this arm was
# UNBOUNDED, which is the state every macOS run was silently in -- the old comment here even
# described losing the bound as acceptable.
FIX_SLOW_NOTIMEOUT="$(build_fixture 12)"
NT_START="$(date +%s)"
OUT="$(printf '%s' "$PAYLOAD" | PATH="$NO_TIMEOUT_BIN" \
  /bin/bash "$FIX_SLOW_NOTIMEOUT/.claude/hooks/cx-agent-lifecycle.sh" sweep --source UserPromptSubmit 2>/dev/null)"
RC=$?
NT_ELAPSED=$(( $(date +%s) - NT_START ))
{ [[ "$RC" -eq 0 ]] && [[ "$NT_ELAPSED" -lt 5 ]]; } \
  && ok "absent timeout binary: the 5s budget is still enforced (${NT_ELAPSED}s < 5s)" \
  || bad "absent timeout binary lost the bound (RC=$RC elapsed=${NT_ELAPSED}s)"

[[ "$FAIL" -eq 0 ]] || exit 1
echo "All $PASS cx-agent-lifecycle deadline checks passed."
