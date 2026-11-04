#!/usr/bin/env bash
# SessionStart hook: surface a bounded, provenance-only index of the current
# repository's docs and memories. This is deliberately dependency-free: a
# missing or unavailable substrate must never delay session startup.

set -uo pipefail

MAX_ITEMS=5
MAX_FIELD_CHARS=120
MAX_BLOCK_CHARS=4800
FLAG_TTL_SECONDS=3600

# Read the complete SessionStart payload without jq/python. `read -d ''` returns
# non-zero at EOF, which is expected for stdin without a NUL terminator.
INPUT=""; IFS= read -r -d '' -t 2 INPUT || true

CWD=""
SESSION_ID=""
if [[ "$INPUT" =~ \"cwd\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then
  CWD="${BASH_REMATCH[1]}"
fi
if [[ "$INPUT" =~ \"session_id\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then
  SESSION_ID="${BASH_REMATCH[1]}"
fi
[[ -n "$CWD" ]] || exit 0

# Resolve the supplied project directory. Fleet project names in projects.toml are
# simple checkout basenames; a worktree belongs to the checkout before
# `/.worktrees/`, not to its task-specific worktree directory.
PROJECT_DIR="$(cd "$CWD" 2>/dev/null && pwd -P)" || exit 0
case "$PROJECT_DIR" in
  */.worktrees/*) PROJECT_DIR="${PROJECT_DIR%%/.worktrees/*}" ;;
esac
PROJECT_NAME="${PROJECT_DIR##*/}"
[[ -n "$PROJECT_NAME" ]] || exit 0

# A machine without ai-core (or without its venv active) must start sessions
# normally. This mirrors bd-prime.sh's tool-presence guard.
command -v core >/dev/null 2>&1 || exit 0

shorten() {
  local value="$1"
  if (( ${#value} > MAX_FIELD_CHARS )); then
    printf '%s…' "${value:0:MAX_FIELD_CHARS-1}"
  else
    printf '%s' "$value"
  fi
}

# Extract only provenance from the CLI's indented JSON. This intentionally does
# not parse or propagate content. Globals are per-domain output/counters so the
# caller can combine both domains into one transparent, bounded block.
collect_domain() {
  local domain="$1" results="$2" line source="" title="" score=""
  local path="" domain_block="" label="" count=0
  DOMAIN_BLOCK=""
  DOMAIN_COUNT=0
  DOMAIN_SOURCES=""
  DOMAIN_SCORES=""

  while IFS= read -r line; do
    if [[ "$line" =~ \"source\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then
      source="${BASH_REMATCH[1]}"
    elif [[ "$line" =~ \"title\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then
      title="${BASH_REMATCH[1]}"
    elif [[ "$line" =~ \"score\"[[:space:]]*:[[:space:]]*([^,[:space:]]+) ]]; then
      score="${BASH_REMATCH[1]}"
    elif [[ "$line" =~ \"source_path\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then
      (( count < MAX_ITEMS )) || break
      [[ -n "$source" ]] || continue
      path="${BASH_REMATCH[1]}"
      [[ -n "$title" ]] || title="(untitled)"
      domain_block+="${domain_block:+$'\n'}- [$(shorten "$source")] $(shorten "$path") — $(shorten "$title")"
      DOMAIN_SOURCES+="${DOMAIN_SOURCES:+, }${domain}:$(shorten "$source")/$(shorten "$path")"
      DOMAIN_SCORES+="${DOMAIN_SCORES:+, }${domain}:$(shorten "${score:-unknown}")"
      count=$((count + 1))
      source=""
      title=""
      score=""
    fi
  done <<< "$results"

  if (( count > 0 )); then
    case "$domain" in
      docs) label="Docs" ;;
      memory) label="Memory" ;;
      *) label="$domain" ;;
    esac
    DOMAIN_BLOCK="$label index (source repo / path / title):"$'\n'"$domain_block"
  fi
  DOMAIN_COUNT="$count"
}

# The T-04 session flag is consumed only when its safe session key, exact JSON
# shape, and T-01 one-hour staleness bound all validate. A bad or stale flag is
# ignored so it cannot make a later session look degraded.
read_degraded_flag() {
  local flag classification failing_field checked_at now flag_epoch age
  DEGRADED_NOTE=""
  [[ "$SESSION_ID" =~ ^[A-Za-z0-9._-]+$ ]] || return 0
  flag="${HOME}/.claude/state/substrate-health-${SESSION_ID}.json"
  [[ -r "$flag" ]] || return 0
  flag="$(<"$flag")" || return 0
  if [[ "$flag" =~ \"classification\"[[:space:]]*:[[:space:]]*\"(degraded|unreachable)\" ]]; then
    classification="${BASH_REMATCH[1]}"
  else
    return 0
  fi
  if [[ "$flag" =~ \"failing_field\"[[:space:]]*:[[:space:]]*\"(reachability|last_successful_reconcile_age|drift_total|red:[A-Za-z0-9_,]+)\" ]]; then
    failing_field="${BASH_REMATCH[1]}"
  else
    return 0
  fi
  if [[ "$flag" =~ \"checked_at\"[[:space:]]*:[[:space:]]*\"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)\" ]]; then
    checked_at="${BASH_REMATCH[1]}"
  else
    return 0
  fi
  now="$(date -u +%s 2>/dev/null)" || return 0
  flag_epoch="$(date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$checked_at" +%s 2>/dev/null || date -u -d "$checked_at" +%s 2>/dev/null)" || return 0
  [[ "$now" =~ ^[0-9]+$ && "$flag_epoch" =~ ^[0-9]+$ ]] || return 0
  age=$((now - flag_epoch))
  (( age >= 0 && age <= FLAG_TTL_SECONDS )) || return 0
  DEGRADED_NOTE="Substrate ${classification}: ${failing_field} is unhealthy; listed provenance may be stale."
}

# The coordinated ai-core CLI change adds the caller-context flags below. Each
# invocation reaches recall()'s T-08a writer even for zero-hit/error outcomes.
# Do not return early after one domain: both calls must retain their telemetry.
DOCS_RESULTS="$(core recall "*" --domain docs --project "$PROJECT_NAME" --limit "$MAX_ITEMS" --tier prime --session "$SESSION_ID" --repo "$PROJECT_NAME" --json 2>/dev/null)"
MEMORY_RESULTS="$(core recall "*" --domain memory --project "$PROJECT_NAME" --limit "$MAX_ITEMS" --tier prime --session "$SESSION_ID" --repo "$PROJECT_NAME" --json 2>/dev/null)"

collect_domain docs "$DOCS_RESULTS"
DOCS_BLOCK="$DOMAIN_BLOCK"
DOCS_COUNT="$DOMAIN_COUNT"
DOCS_SOURCES="$DOMAIN_SOURCES"
DOCS_SCORES="$DOMAIN_SCORES"
collect_domain memory "$MEMORY_RESULTS"
MEMORY_BLOCK="$DOMAIN_BLOCK"
MEMORY_COUNT="$DOMAIN_COUNT"
MEMORY_SOURCES="$DOMAIN_SOURCES"
MEMORY_SCORES="$DOMAIN_SCORES"

TOTAL_HITS=$((DOCS_COUNT + MEMORY_COUNT))
read_degraded_flag
# A zero-hit recall must not suppress the degraded-substrate note: a red
# substrate is exactly the condition most likely to make recall itself return
# nothing, so gating this exit on hits alone would silently drop the one
# signal T-04a/T-08d's mid-session and fleet-wide backstops don't cover yet
# (the very first SessionStart after degradation begins).
[[ "$TOTAL_HITS" -gt 0 || -n "$DEGRADED_NOTE" ]] || exit 0

SOURCES="${DOCS_SOURCES}${DOCS_SOURCES:+${MEMORY_SOURCES:+, }}${MEMORY_SOURCES}"
SCORES="${DOCS_SCORES}${DOCS_SCORES:+${MEMORY_SCORES:+, }}${MEMORY_SCORES}"
DETAILS="${DEGRADED_NOTE:+$DEGRADED_NOTE$'\n'}${DOCS_BLOCK}${DOCS_BLOCK:+${MEMORY_BLOCK:+$'\n'}}${MEMORY_BLOCK}"
HEADER_SOURCES="$(shorten "$SOURCES")"
HEADER_SCORES="$(shorten "$SCORES")"

# The canonical T-01 fields open the block. D-7's retraction mechanism is not
# shipped yet, so no exclusion (and no suppressed count) is attempted here.
HEADER="Substrate recall: query=\"*\"; tier=prime; hits=${TOTAL_HITS}; sources=${HEADER_SOURCES}; scores=${HEADER_SCORES}; used=all listed provenance; cost=pending"
CONTEXT="$HEADER"$'\n'"$DETAILS"
BYTES=${#CONTEXT}
TOKENS=$(((BYTES + 3) / 4))
HEADER="Substrate recall: query=\"*\"; tier=prime; hits=${TOTAL_HITS}; sources=${HEADER_SOURCES}; scores=${HEADER_SCORES}; used=all listed provenance; cost=${BYTES} bytes/~${TOKENS} tokens"
CONTEXT="$HEADER"$'\n'"$DETAILS"

# Ten capped provenance lines plus the header remain far below the 10,000-char
# Claude Code ceiling and target approximately 1,200 tokens. Retain the header
# if an unexpected field shape still exceeds the defensive cap.
if (( ${#CONTEXT} > MAX_BLOCK_CHARS )); then
  CONTEXT="${CONTEXT:0:MAX_BLOCK_CHARS}"
fi

# JSON-escape the text block without a jq/python dependency.
ESCAPED=${CONTEXT//\\/\\\\}
ESCAPED=${ESCAPED//\"/\\\"}
ESCAPED=${ESCAPED//$'\n'/\\n}
ESCAPED=${ESCAPED//$'\r'/\\r}
ESCAPED=${ESCAPED//$'\t'/\\t}
printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$ESCAPED"
