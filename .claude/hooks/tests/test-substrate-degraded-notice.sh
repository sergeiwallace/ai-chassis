#!/usr/bin/env bash
# Focused offline checks for substrate-degraded-notice.sh. Run with:
#   bash .claude/hooks/tests/test-substrate-degraded-notice.sh

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HOOK="$REPO/.claude/hooks/substrate-degraded-notice.sh"
SANDBOX="$(mktemp -d)" || { echo "unable to create test sandbox" >&2; exit 1; }
[[ -n "$SANDBOX" && "$SANDBOX" == /* ]] || { echo "refusing to run: unsafe sandbox path" >&2; exit 1; }
trap 'rm -rf -- "$SANDBOX"' EXIT
HOME_DIR="$SANDBOX/home"
STATE_DIR="$HOME_DIR/.claude/state"
MOCK_BIN="$SANDBOX/mock-bin"
mkdir -p "$STATE_DIR" "$MOCK_BIN"

PASS=0
FAIL=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

NOW=2000000000
flag_path() { printf '%s/substrate-health-%s.json' "$STATE_DIR" "$1"; }
marker_path() { printf '%s/substrate-degraded-noticed-%s' "$STATE_DIR" "$1"; }
write_flag() {
  printf '{"classification":"%s","failing_field":"%s","checked_at":"%s"}\n' "$2" "$3" "$4" > "$(flag_path "$1")"
}
write_date_mock() {
  printf '%s\n' '#!/usr/bin/env bash' \
    'for arg in "$@"; do' \
    '  [[ "$arg" == "2033-05-18T02:03:19Z" ]] && { printf "%s\\n" "$((NOTICE_NOW - 3601))"; exit 0; }' \
    '  [[ "$arg" == "2033-05-18T02:33:20Z" ]] && { printf "%s\\n" "$((NOTICE_NOW - 1800))"; exit 0; }' \
    '  [[ "$arg" == "2033-05-18T02:33:21Z" ]] && { printf "%s\\n" "$((NOTICE_NOW - 1799))"; exit 0; }' \
    '  [[ "$arg" == "2033-05-18T02:53:20Z" ]] && { printf "%s\\n" "$((NOTICE_NOW - 600))"; exit 0; }' \
    '  [[ "$arg" == "invalid" ]] && exit 1' \
    'done' \
    'printf "%s\\n" "$NOTICE_NOW"' > "$MOCK_BIN/date"
  chmod +x "$MOCK_BIN/date"
}
run_hook() {
  OUT="$(printf '%s' "$1" | env HOME="$HOME_DIR" NOTICE_NOW="$NOW" PATH="$MOCK_BIN:$PATH" "${@:2}" bash "$HOOK" 2>/dev/null)"
  RC=$?
}

write_date_mock
echo "== substrate degraded notice =="

SID="healthy-session"
write_flag "$SID" degraded drift_total '2033-05-18T03:03:20Z'
run_hook "{\"session_id\":\"$SID\"}"
{ [[ "$RC" -eq 0 ]] && [[ -z "$OUT" ]] && [[ ! -e "$(marker_path "$SID")" ]]; } \
  && ok "fresh flag, interval not yet elapsed since the SessionStart notice: silent" \
  || bad "fresh flag (RC=$RC OUT=$OUT)"

printf '%s\n' "$NOW" > "$(marker_path "$SID")"
run_hook "{\"session_id\":\"$SID\"}"
[[ "$RC" -eq 0 && -z "$OUT" ]] && ok "interval not elapsed: throttled" || bad "throttle (RC=$RC OUT=$OUT)"

printf '%s\n' "$((NOW - 1800))" > "$(marker_path "$SID")"
run_hook "{\"session_id\":\"$SID\"}"
[[ "$RC" -eq 0 && "$OUT" == *'drift_total'* ]] && ok "interval elapsed: re-notices" || bad "re-notice (RC=$RC OUT=$OUT)"

SID="default-interval-session"
write_flag "$SID" degraded drift_total '2033-05-18T02:33:20Z'
run_hook "{\"session_id\":\"$SID\"}"
{ [[ "$RC" -eq 0 ]] && [[ "$OUT" == *'drift_total'* ]] && [[ "$OUT" == *'degraded for 30m'* ]] && [[ "$(cat "$(marker_path "$SID")")" == "$NOW" ]]; } \
  && ok "default interval: 1800-second-old flag first emits" \
  || bad "default interval (RC=$RC OUT=$OUT)"

SID="interval-boundary-session"
write_flag "$SID" degraded drift_total '2033-05-18T02:33:21Z'
run_hook "{\"session_id\":\"$SID\"}"
{ [[ "$RC" -eq 0 ]] && [[ -z "$OUT" ]] && [[ ! -e "$(marker_path "$SID")" ]]; } \
  && ok "interval boundary: 1799-second-old flag is silent" \
  || bad "interval boundary (RC=$RC OUT=$OUT)"

SID="override-interval-session"
write_flag "$SID" degraded drift_total '2033-05-18T02:53:20Z'
run_hook "{\"session_id\":\"$SID\"}" 'SUBSTRATE_DEGRADED_RENOTICE_INTERVAL_SECONDS=600'
{ [[ "$RC" -eq 0 ]] && [[ "$OUT" == *'degraded for 10m'* ]] && [[ "$(cat "$(marker_path "$SID")")" == "$NOW" ]]; } \
  && ok "configured interval: 600-second override first emits" \
  || bad "configured interval (RC=$RC OUT=$OUT)"

for INVALID_INTERVAL in abc '' 0; do
  SID="invalid-interval-${INVALID_INTERVAL:-empty}"
  write_flag "$SID" degraded drift_total '2033-05-18T02:33:20Z'
  run_hook "{\"session_id\":\"$SID\"}" "SUBSTRATE_DEGRADED_RENOTICE_INTERVAL_SECONDS=$INVALID_INTERVAL"
  { [[ "$RC" -eq 0 ]] && [[ "$OUT" == *'degraded for 30m'* ]] && [[ "$(cat "$(marker_path "$SID")")" == "$NOW" ]]; } \
    && ok "invalid interval ${INVALID_INTERVAL:-empty}: falls back to default" \
    || bad "invalid interval ${INVALID_INTERVAL:-empty} old flag (RC=$RC OUT=$OUT)"
  rm -f "$(marker_path "$SID")"
  write_flag "$SID" degraded drift_total '2033-05-18T03:03:20Z'
  run_hook "{\"session_id\":\"$SID\"}" "SUBSTRATE_DEGRADED_RENOTICE_INTERVAL_SECONDS=$INVALID_INTERVAL"
  { [[ "$RC" -eq 0 ]] && [[ -z "$OUT" ]] && [[ ! -e "$(marker_path "$SID")" ]]; } \
    && ok "invalid interval ${INVALID_INTERVAL:-empty}: fresh flag is silent" \
    || bad "invalid interval ${INVALID_INTERVAL:-empty} fresh flag (RC=$RC OUT=$OUT)"
done

SID="malformed-marker-session"
write_flag "$SID" degraded drift_total '2033-05-18T02:33:20Z'
printf '%s\n' not-an-epoch > "$(marker_path "$SID")"
run_hook "{\"session_id\":\"$SID\"}"
{ [[ "$RC" -eq 0 ]] && [[ "$OUT" == *'degraded for 30m'* ]] && [[ "$(cat "$(marker_path "$SID")")" == "$NOW" ]]; } \
  && ok "malformed marker: uses checked_at baseline and re-notices" \
  || bad "malformed marker (RC=$RC OUT=$OUT)"

SID="stale-session"
write_flag "$SID" unreachable reachability '2033-05-18T02:03:19Z'
printf '%s\n' 123 > "$(marker_path "$SID")"
run_hook "{\"session_id\":\"$SID\"}"
{ [[ "$RC" -eq 0 ]] && [[ -z "$OUT" ]] && [[ "$(cat "$(marker_path "$SID")")" == 123 ]]; } \
  && ok "stale flag: silent and marker is not renewed" \
  || bad "stale flag (RC=$RC OUT=$OUT)"

SID="recovered-session"
printf '%s\n' 123 > "$(marker_path "$SID")"
run_hook "{\"session_id\":\"$SID\"}"
[[ "$RC" -eq 0 && -z "$OUT" && ! -e "$(marker_path "$SID")" ]] \
  && ok "absent flag: clears cadence marker" || bad "recovery (RC=$RC OUT=$OUT)"

SID="malformed-session"
printf 'not json\n' > "$(flag_path "$SID")"
run_hook "{\"session_id\":\"$SID\"}"
[[ "$RC" -eq 0 && -z "$OUT" ]] && ok "malformed flag: silent fail-open" || bad "malformed flag (RC=$RC OUT=$OUT)"

SID="invalid-time-session"
write_flag "$SID" degraded drift_total invalid
run_hook "{\"session_id\":\"$SID\"}"
[[ "$RC" -eq 0 && -z "$OUT" ]] && ok "invalid checked_at: silent fail-open" || bad "invalid checked_at (RC=$RC OUT=$OUT)"

SID="state-touch-forbidden"
run_hook '{"session_id":"../escape"}'
[[ "$RC" -eq 0 && -z "$OUT" && ! -e "$(marker_path "$SID")" ]] \
  && ok "unsafe session id: no state access" || bad "unsafe session id (RC=$RC OUT=$OUT)"
run_hook '{"session_id":"safe..not"}'
[[ "$RC" -eq 0 && -z "$OUT" ]] && ok "traversal sequence: no state access" || bad "traversal sequence (RC=$RC OUT=$OUT)"
run_hook '{}'
[[ "$RC" -eq 0 && -z "$OUT" ]] && ok "missing session id: silent fail-open" || bad "missing session id (RC=$RC OUT=$OUT)"

# Fault-injection sweep: each internal command failure must remain fail-open.
SID="fault-session"
write_flag "$SID" degraded drift_total '2033-05-18T03:03:20Z'
for fault in cat date; do
  printf '%s\n' '#!/usr/bin/env bash' 'exit 1' > "$MOCK_BIN/$fault"
  chmod +x "$MOCK_BIN/$fault"
  run_hook "{\"session_id\":\"$SID\"}"
  [[ "$RC" -eq 0 && -z "$OUT" ]] && ok "fault $fault: never exits non-zero" || bad "fault $fault (RC=$RC OUT=$OUT)"
  rm -f "$MOCK_BIN/$fault"
done
SID="fault-recovery"
printf '%s\n' 123 > "$(marker_path "$SID")"
printf '%s\n' '#!/usr/bin/env bash' 'exit 1' > "$MOCK_BIN/rm"
chmod +x "$MOCK_BIN/rm"
run_hook "{\"session_id\":\"$SID\"}"
[[ "$RC" -eq 0 && -z "$OUT" ]] && ok "fault rm: never exits non-zero" || bad "fault rm (RC=$RC OUT=$OUT)"
rm -f "$MOCK_BIN/rm"
write_date_mock
SID="fault-marker"
write_flag "$SID" degraded drift_total '2033-05-18T02:33:20Z'
mkdir "$(marker_path "$SID")"
run_hook "{\"session_id\":\"$SID\"}"
[[ "$RC" -eq 0 && -z "$OUT" ]] && ok "marker write failure: never exits non-zero" || bad "marker write (RC=$RC OUT=$OUT)"
rmdir "$(marker_path "$SID")"

[[ "$FAIL" -eq 0 ]] || exit 1
echo "All $PASS substrate-degraded-notice checks passed."
