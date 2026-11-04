#!/usr/bin/env bash
# Focused offline checks for substrate-health.sh. Run with:
#   bash .claude/hooks/tests/test-substrate-health.sh

# shellcheck disable=SC2015,SC2016
# The harness intentionally writes literal mock-script source and follows the
# repository's compact `condition && ok || bad` counter pattern.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HOOK="$REPO/.claude/hooks/substrate-health.sh"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
MOCK_BIN="$SANDBOX/mock-bin"
EMPTY_BIN="$SANDBOX/empty-bin"
STATE="$SANDBOX/.claude/state"
mkdir -p "$MOCK_BIN" "$EMPTY_BIN" "$STATE"

PASS=0
FAIL=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

printf '%s\n' '#!/usr/bin/env bash' \
  '[[ "$1 $2" == "doctor --json" ]] || exit 2' \
  'case "${CORE_MODE:-green}" in' \
  '  green) printf "%s\n" "{\"healthy\":true,\"checks\":[]}" ;;' \
  '  age) printf "%s\n" "{\"healthy\":false,\"checks\":[{\"name\":\"index_drift\",\"status\":\"red\",\"detail\":\"last successful reconcile 9.0h ago\"}]}"; exit 1 ;;' \
  '  drift) printf "%s\n" "{\"healthy\":false,\"checks\":[{\"name\":\"index_drift\",\"status\":\"red\",\"observed\":\"{\\\"memory\\\":{\\\"drift_total\\\":2}}\"}]}"; exit 1 ;;' \
  '  error) exit 1 ;;' \
  '  malformed) printf "%s\n" "not json" ;;' \
  '  timeout) exec sleep 10 ;;' \
  'esac' > "$MOCK_BIN/core"
chmod +x "$MOCK_BIN/core"

printf '%s\n' '#!/usr/bin/env bash' \
  '[[ "$*" == *"/healthz"* ]] || exit 2' \
  'case "${HTTP_MODE:-error}" in' \
  '  green) printf "%s\n" "{\"healthy\":true,\"checks\":[]}" ;;' \
  '  age) printf "%s\n" "{\"healthy\":false,\"checks\":[{\"name\":\"index_drift\",\"status\":\"red\",\"detail\":\"last successful reconcile never completed\"}]}"; exit 22 ;;' \
  '  error) exit 7 ;;' \
  '  malformed) printf "%s\n" "<html>bad gateway</html>" ;;' \
  'esac' > "$MOCK_BIN/curl"
chmod +x "$MOCK_BIN/curl"

SID="health-$RANDOM"
FLAG="$STATE/substrate-health-$SID.json"

run_hook() {
  # Every classification case below is about a machine that HAS a substrate, so
  # the DSN is set explicitly -- the ISSUE-REF guard exits silently without one,
  # and these cases would otherwise all assert against a substrate-less machine.
  OUT="$(printf '{"session_id":"%s","cwd":"/tmp"}' "$SID" | HOME="$SANDBOX" PATH="$MOCK_BIN:$PATH" AI_CORE_DATABASE_URL="postgresql://localhost/ai_core" CORE_MODE="${1:-green}" HTTP_MODE="${2:-error}" /bin/bash "$HOOK" 2>/dev/null)"
  RC=$?
}

check_flag() {
  local classification="$1" field="$2"
  [[ -f "$FLAG" ]] \
    && grep -q "\"classification\":\"$classification\"" "$FLAG" \
    && grep -q "\"failing_field\":\"$field\"" "$FLAG" \
    && grep -Eq '"checked_at":"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"' "$FLAG"
}

echo "== substrate health =="
printf '%s\n' '{"classification":"unreachable","failing_field":"reachability","checked_at":"2000-01-01T00:00:00Z"}' > "$FLAG"
run_hook green
[[ "$RC" -eq 0 && -z "$OUT" && ! -e "$FLAG" ]] \
  && ok "green: silent and clears a stale session flag" \
  || bad "green (RC=$RC OUT=$OUT flag=$([[ -e "$FLAG" ]] && echo yes || echo no))"

run_hook age
{ [[ "$RC" -eq 0 ]] && [[ "$OUT" == *'last_successful_reconcile_age'* ]] \
  && check_flag degraded last_successful_reconcile_age; } \
  && ok "doctor degraded: age field is surfaced and flagged" \
  || bad "doctor age degraded (RC=$RC OUT=$OUT)"

rm -f "$FLAG"
run_hook drift
{ [[ "$RC" -eq 0 ]] && [[ "$OUT" == *'drift_total'* ]] && check_flag degraded drift_total; } \
  && ok "doctor degraded: drift field is surfaced and flagged" \
  || bad "doctor drift degraded (RC=$RC OUT=$OUT)"

rm -f "$FLAG"
run_hook error green
[[ "$RC" -eq 0 && -z "$OUT" && ! -e "$FLAG" ]] \
  && ok "doctor error: falls back to green GET /healthz" \
  || bad "HTTP green fallback (RC=$RC OUT=$OUT flag=$([[ -e "$FLAG" ]] && echo yes || echo no))"

rm -f "$FLAG"
run_hook error age
{ [[ "$RC" -eq 0 ]] && [[ "$OUT" == *'last_successful_reconcile_age'* ]] \
  && check_flag degraded last_successful_reconcile_age; } \
  && ok "doctor error: degraded GET /healthz is surfaced and flagged" \
  || bad "HTTP degraded fallback (RC=$RC OUT=$OUT)"

rm -f "$FLAG"
run_hook malformed malformed
{ [[ "$RC" -eq 0 ]] && [[ "$OUT" == *'reachability'* ]] && check_flag unreachable reachability; } \
  && ok "malformed doctor and HTTP output: unreachable flag, exit 0" \
  || bad "malformed output (RC=$RC OUT=$OUT)"

rm -f "$FLAG"
START=$(date +%s)
run_hook timeout error
ELAPSED=$(( $(date +%s) - START ))
{ [[ "$RC" -eq 0 ]] && [[ "$ELAPSED" -le 5 ]] && [[ "$OUT" == *'reachability'* ]] \
  && check_flag unreachable reachability; } \
  && ok "timed out doctor: bounded, flagged unreachable, exit 0 (${ELAPSED}s)" \
  || bad "timeout (RC=$RC elapsed=${ELAPSED}s OUT=$OUT)"

rm -f "$FLAG"
OUT="$(printf '{"session_id":"%s"}' "$SID" | HOME="$SANDBOX" PATH="$EMPTY_BIN" /bin/bash "$HOOK" 2>/dev/null)"
RC=$?
[[ "$RC" -eq 0 && -z "$OUT" && ! -e "$FLAG" ]] \
  && ok "core absent: silent, no degraded flag" \
  || bad "core absent (RC=$RC OUT=$OUT flag=$([[ -e "$FLAG" ]] && echo yes || echo no))"

rm -f "$FLAG"
OUT="$(printf '{"session_id":"../../escape"}' | HOME="$SANDBOX" PATH="$MOCK_BIN:$PATH" /bin/bash "$HOOK" 2>/dev/null)"
RC=$?
[[ "$RC" -eq 0 && -z "$OUT" && ! -e "$SANDBOX/escape" ]] \
  && ok "unsafe session_id: silent and cannot escape the state directory" \
  || bad "unsafe session_id (RC=$RC OUT=$OUT)"

# --- ISSUE-REF: substrate-CONFIGURED vs substrate-less -----------------------
#
# `command -v core` tests whether the CLI exists, which is not whether this
# machine has a substrate. On example-project-sagemaker `core` is a uv tool install on
# PATH while no substrate was configured, so the guard passed, the doctor
# reported red (no DSN), the 127.0.0.1:8400 fallback found nothing, and EVERY
# session opened with "Substrate health degraded: reachability is unhealthy" --
# a false alarm indistinguishable from a real outage.
#
# The distinguishing signal is configuration, not CLI presence: an unset
# AI_CORE_DATABASE_URL with no AI_CORE_HEALTHZ_URL override means "no substrate
# on this machine" (silent). Once either is set, an unreachable substrate is a
# real finding and must still be reported.

run_hook_env() {
  # Same as run_hook but with a caller-controlled substrate configuration.
  # `env` requires its options (-u) BEFORE any KEY=VALUE assignment, so the
  # caller's args go first and the fixed sandbox assignments follow.
  local core_mode="$1" http_mode="$2"
  shift 2
  OUT="$(printf '{"session_id":"%s","cwd":"/tmp"}' "$SID" \
    | /usr/bin/env "$@" \
      HOME="$SANDBOX" PATH="$MOCK_BIN:$PATH" \
      CORE_MODE="$core_mode" HTTP_MODE="$http_mode" \
      /bin/bash "$HOOK" 2>/dev/null)"
  RC=$?
}

rm -f "$FLAG"
run_hook_env error error -u AI_CORE_DATABASE_URL -u AI_CORE_HEALTHZ_URL
[[ "$RC" -eq 0 && -z "$OUT" && ! -e "$FLAG" ]] \
  && ok "substrate not configured: silent, no degraded flag (core on PATH)" \
  || bad "unconfigured substrate misreported (RC=$RC OUT=$OUT flag=$([[ -e "$FLAG" ]] && echo yes || echo no))"

rm -f "$FLAG"
run_hook_env error error -u AI_CORE_HEALTHZ_URL AI_CORE_DATABASE_URL=postgresql://localhost/ai_core
{ [[ "$RC" -eq 0 ]] && [[ "$OUT" == *'reachability'* ]] && check_flag unreachable reachability; } \
  && ok "substrate configured but unreachable: still reported" \
  || bad "configured-unreachable was silenced (RC=$RC OUT=$OUT)"

rm -f "$FLAG"
run_hook_env error error -u AI_CORE_DATABASE_URL AI_CORE_HEALTHZ_URL=http://127.0.0.1:8400/healthz
{ [[ "$RC" -eq 0 ]] && [[ "$OUT" == *'reachability'* ]] && check_flag unreachable reachability; } \
  && ok "healthz URL alone counts as configured: still reported" \
  || bad "explicit healthz URL was silenced (RC=$RC OUT=$OUT)"

rm -f "$FLAG"
run_hook_env green error AI_CORE_DATABASE_URL=postgresql://localhost/ai_core
[[ "$RC" -eq 0 && -z "$OUT" && ! -e "$FLAG" ]] \
  && ok "configured and green: silent" \
  || bad "configured-green misreported (RC=$RC OUT=$OUT)"

[[ "$FAIL" -eq 0 ]] || exit 1
echo "All $PASS substrate-health checks passed."
