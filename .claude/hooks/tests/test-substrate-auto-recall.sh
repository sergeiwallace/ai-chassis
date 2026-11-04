#!/usr/bin/env bash
# Focused offline checks for substrate-auto-recall.sh. Run with:
#   bash .claude/hooks/tests/test-substrate-auto-recall.sh

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HOOK="$REPO/.claude/hooks/substrate-auto-recall.sh"
SANDBOX="$(mktemp -d)" || { echo "unable to create test sandbox" >&2; exit 1; }
[[ -n "$SANDBOX" && "$SANDBOX" == /* ]] || { echo "unsafe sandbox path" >&2; exit 1; }
trap 'rm -rf -- "$SANDBOX"' EXIT
MOCK_BIN="$SANDBOX/mock-bin"
EMPTY_BIN="$SANDBOX/empty-bin"
CALLS="$SANDBOX/core-calls"
mkdir -p "$MOCK_BIN" "$EMPTY_BIN"
# No hard `timeout` requirement. The hook no longer needs coreutils -- macOS ships neither
# `timeout` nor `gtimeout` -- and requiring one here meant this file exited 1 on a Mac, reporting
# a missing tool instead of checking the hook, on the platform where the hook's own bound was
# missing too. The passthrough below is still installed wherever a real `timeout` exists, so a
# coreutils host exercises the real binary and a Mac exercises the builtin fallback: each
# platform tests the path it actually takes.
TIMEOUT_BIN="$(command -v timeout 2>/dev/null || true)"

PASS=0
FAIL=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

printf '%s\n' '#!/bin/bash' \
  'printf "%s\\n" "$@" > "$CORE_CALLS"' \
  '[[ "${CORE_MODE:-ok}" != hang ]] || { /bin/sleep 12; exit 0; }' \
  '[[ "${CORE_MODE:-ok}" == ok ]] || exit 1' > "$MOCK_BIN/core"
chmod +x "$MOCK_BIN/core"
if [[ -n "$TIMEOUT_BIN" ]]; then
  printf '%s\n' '#!/bin/bash' "exec \"$TIMEOUT_BIN\" \"\$@\"" > "$MOCK_BIN/timeout"
  chmod +x "$MOCK_BIN/timeout"
fi

run_hook() {
  OUT="$(printf '%s' "$1" | PATH="$2" CORE_CALLS="$CALLS" CORE_MODE="${3:-ok}" /bin/bash "$HOOK" 2>/dev/null)"
  RC=$?
}

echo "== substrate auto-recall command hook =="
run_hook '{"prompt":"recover known context","session_id":"session-1","cwd":"/projects/repo"}' "$MOCK_BIN"
{ [[ "$RC" -eq 0 ]] && [[ -z "$OUT" ]] \
  && [[ "$(sed -n '1p' "$CALLS")" == recall ]] \
  && [[ "$(sed -n '2p' "$CALLS")" == 'recover known context' ]] \
  && grep -qx -- '--tier' "$CALLS" \
  && grep -qx -- 'auto-recall' "$CALLS" \
  && grep -qx -- '--session' "$CALLS" \
  && grep -qx -- 'session-1' "$CALLS" \
  && grep -qx -- '--repo' "$CALLS" \
  && grep -qx -- '/projects/repo' "$CALLS" \
  && grep -qx -- '--project' "$CALLS" \
  && ! grep -qx -- '--log-only' "$CALLS" \
  && ! grep -qx -- '--status' "$CALLS"; } \
  && ok "recall runs outside the MCP transport" \
  || bad "recall invocation (RC=$RC OUT=$OUT)"

run_hook '{"prompt":"","session_id":"empty","cwd":"/projects/repo"}' "$MOCK_BIN"
{ [[ "$RC" -eq 0 ]] && [[ -z "$OUT" ]] && [[ "$(sed -n '2p' "$CALLS")" == "" ]]; } \
  && ok "empty prompt remains a valid recall query" \
  || bad "empty prompt (RC=$RC OUT=$OUT)"

run_hook '{not-json' "$MOCK_BIN"
[[ "$RC" -eq 0 && -z "$OUT" ]] \
  && ok "malformed input: fail-open with unknown context" \
  || bad "malformed input (RC=$RC OUT=$OUT)"

run_hook '{"prompt":"core fails","session_id":"failure","cwd":"/projects/repo"}' "$MOCK_BIN" fail
[[ "$RC" -eq 0 && -z "$OUT" ]] \
  && ok "core failure: never exits non-zero" \
  || bad "core failure (RC=$RC OUT=$OUT)"

run_hook '{"prompt":"core absent","session_id":"absent","cwd":"/projects/repo"}' "$EMPTY_BIN"
[[ "$RC" -eq 0 && -z "$OUT" ]] \
  && ok "core absent: never exits non-zero" \
  || bad "core absent (RC=$RC OUT=$OUT)"

MARKER="$SANDBOX/command-substitution-ran"
run_hook "{\"prompt\":\"\\\$(touch $MARKER)\",\"session_id\":\"quoted\",\"cwd\":\"/projects/repo\"}" "$MOCK_BIN"
{ [[ "$RC" -eq 0 ]] && [[ ! -e "$MARKER" ]]; } \
  && ok "prompt text is passed as an argument, never evaluated" \
  || bad "prompt quoting (RC=$RC marker=$([[ -e "$MARKER" ]] && echo yes || echo no))"

# A degraded backend can leave the CLI process hung. The hook must stop waiting
# well before its own 10s Claude Code timeout and still remain fail-open.
SECONDS=0
run_hook '{"prompt":"slow backend","session_id":"timeout","cwd":"/projects/repo"}' "$MOCK_BIN" hang
ELAPSED="$SECONDS"
{ [[ "$RC" -eq 0 ]] && [[ -z "$OUT" ]] && [[ "$ELAPSED" -lt 8 ]]; } \
  && ok "hung core recall is bounded (${ELAPSED}s < 8s)" \
  || bad "hung core recall exceeded hook-safe budget (RC=$RC elapsed=${ELAPSED}s OUT=$OUT)"

[[ "$FAIL" -eq 0 ]] || exit 1
echo "All $PASS substrate-auto-recall checks passed."
