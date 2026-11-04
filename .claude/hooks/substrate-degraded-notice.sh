#!/usr/bin/env bash
# UserPromptSubmit reminder for the T-04 session-scoped substrate health flag.
# Configure re-notices with SUBSTRATE_DEGRADED_RENOTICE_INTERVAL_SECONDS.
# This is deliberately fail-open: a non-zero UserPromptSubmit exit can erase a
# user's prompt, so every path below ends at exit 0.

RENOTICE_INTERVAL_SECONDS="${SUBSTRATE_DEGRADED_RENOTICE_INTERVAL_SECONDS:-1800}" # Invalid overrides must not silence reminders.
[[ "$RENOTICE_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]] || RENOTICE_INTERVAL_SECONDS=1800 # Fall back to the safe default.
STALE_AFTER_SECONDS=3600

INPUT=""; IFS= read -r -d '' -t 2 INPUT || true

SESSION_ID=""
if [[ "$INPUT" =~ \"session_id\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then
  SESSION_ID="${BASH_REMATCH[1]}"
fi

# A session id becomes part of a state-file name. Reject invalid input before
# constructing any path, rather than normalizing it to a shared fallback name.
[[ "$SESSION_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || exit 0
case "$SESSION_ID" in *..*) exit 0 ;; esac

STATE_DIR="${HOME}/.claude/state"
FLAG_PATH="${STATE_DIR}/substrate-health-${SESSION_ID}.json"
MARKER_PATH="${STATE_DIR}/substrate-degraded-noticed-${SESSION_ID}"

# A missing producer flag means recovery; clear this consumer's cadence marker.
if [[ ! -f "$FLAG_PATH" ]]; then
  rm -f "$MARKER_PATH" 2>/dev/null || true
  exit 0
fi

FLAG="$(cat "$FLAG_PATH" 2>/dev/null)" || exit 0
CLASSIFICATION=""
FAILING_FIELD=""
CHECKED_AT=""
if [[ "$FLAG" =~ ^\{\"classification\":\"(degraded|unreachable)\",\"failing_field\":\"([A-Za-z0-9_:,]+)\",\"checked_at\":\"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)\"\}[[:space:]]*$ ]]; then
  CLASSIFICATION="${BASH_REMATCH[1]}"
  FAILING_FIELD="${BASH_REMATCH[2]}"
  CHECKED_AT="${BASH_REMATCH[3]}"
else
  exit 0
fi

# BSD and GNU date use different ISO-8601 parsing flags. Do not treat an
# unparseable timestamp as a degradation signal.
CHECKED_EPOCH="$(date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$CHECKED_AT" '+%s' 2>/dev/null)" \
  || CHECKED_EPOCH="$(date -u -d "$CHECKED_AT" '+%s' 2>/dev/null)" \
  || exit 0
[[ "$CHECKED_EPOCH" =~ ^[0-9]+$ ]] || exit 0
NOW_EPOCH="$(date -u '+%s' 2>/dev/null)" || exit 0
[[ "$NOW_EPOCH" =~ ^[0-9]+$ ]] || exit 0

AGE_SECONDS=$(( NOW_EPOCH - CHECKED_EPOCH ))
(( AGE_SECONDS < 0 )) && AGE_SECONDS=0
(( AGE_SECONDS <= STALE_AFTER_SECONDS )) || exit 0

LAST_NOTICE="$CHECKED_EPOCH"
if [[ -f "$MARKER_PATH" ]]; then
  MARKER_NOTICE="$(cat "$MARKER_PATH" 2>/dev/null)" || MARKER_NOTICE=""
  [[ "$MARKER_NOTICE" =~ ^[0-9]+$ ]] && LAST_NOTICE="$MARKER_NOTICE"
fi
(( NOW_EPOCH - LAST_NOTICE >= RENOTICE_INTERVAL_SECONDS )) || exit 0

# Mark before emitting so a successful notice is throttled even if Claude Code
# stops reading this hook's stdout immediately afterwards.
printf '%s\n' "$NOW_EPOCH" > "$MARKER_PATH" 2>/dev/null || exit 0
if (( AGE_SECONDS < 60 )); then
  AGE_TEXT="${AGE_SECONDS}s"
elif (( AGE_SECONDS < 3600 )); then
  AGE_TEXT="$(( AGE_SECONDS / 60 ))m"
else
  AGE_TEXT="$(( AGE_SECONDS / 3600 ))h"
fi
printf '%s\n' "{\"hookSpecificOutput\":{\"hookEventName\":\"UserPromptSubmit\",\"additionalContext\":\"SUBSTRATE STILL ${CLASSIFICATION}: ${FAILING_FIELD} has been degraded for ${AGE_TEXT}; session continues in degraded mode.\"}}" || true
exit 0
