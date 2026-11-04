#!/usr/bin/env bash
# resume-lib.sh (ISSUE-REF): shared helpers for the resume-message delivery
# lifecycle, sourced by .claude/bin/save-resume (writer + ACK) and
# .claude/hooks/resume-inject.sh (deliverer). Centralizing the key derivation,
# directory layout, fingerprint, and sweep removes the "keep in sync" fragility
# of duplicating them across the two scripts.
#
# Lifecycle (Maildir-shaped, ISSUE-REF plan D4=(c)):
#   outbox/    <key>.md         saved by /save-state, undelivered            [new]
#   injected/  <key>-<ts>.md    delivered on /compact, awaiting consumer ACK [in-flight]
#   firstturn/ <key>.stamp      the receiving session reached its first turn [clock start]
#   confirmed/ <key>-<ts>.md    assistant ACKed it (processed)               [done]
#   failed/    <key>-<ts>.md    swept: ACK late, first turn never reached,
#                               superseded, or the delivery was stale        [dead-letter]
#
# "ACK late" is measured from the first-turn stamp, not from injection (ISSUE-REF
# AC-2); the two intervals and their two thresholds are stated in full above
# resume_sweep below.
#
# The <key> is the coarse routing id. Normally this is the per-pane-unique
# CLAUDE_CODE_TASK_LIST_ID, cross-checked against the current session's persisted
# registry name when available. The fingerprint front-matter additionally records
# the raw task-list id, registry name, session id, and cwd so a delivery is
# auditable/verifiable (ISSUE-REF folds in the CC#10 per-session keying: both the
# human-readable pane id AND the unique session id are captured, not just one).
#
# Functions must be safe under the caller's `set -euo pipefail` (use explicit `if`,
# never a bare `(( ))` as a statement).

# Root of the resume store.
#
# AIH_RESUME_ROOT is honoured here because the two OTHER readers of this same root
# already honour it -- context_supervisor.py's _resume_root() and
# .claude/hooks/context-supervisor-user-prompt.sh's `${AIH_RESUME_ROOT:-$HOME/.claude/resume}`
# -- and this function was the one that did not. That divergence is load-bearing for
# the first-turn stamp below: the stamp is written through this library by BOTH the
# shell call site and the Python wake path, and then read back by resume_sweep. A
# writer resolving one root while the reader resolves another is a guard looking in the
# wrong place, which reports CLEAR rather than refusing.
resume_root() { printf '%s' "${AIH_RESUME_ROOT:-$HOME/.claude/resume}"; }

# Resolve the authoritative session name into $RESUME_REGISTRY_NAME when the current
# CLAUDE_CODE_SESSION_ID has exactly one name in ~/.claude/sessions/*.json. The
# registry is written by Claude Code, and jq is already a baseline dependency of the
# installed harness scripts. Missing, unreadable, or malformed files preserve the
# existing fallback chain; conflicting matching records are explicitly ambiguous.
resume_registry_name() {
  local sid="${CLAUDE_CODE_SESSION_ID:-}" f candidate name="" found=0
  RESUME_REGISTRY_NAME=""
  RESUME_REGISTRY_STATUS="absent"
  # Consult the same PID/registry resolver as write-capable Python consumers.
  # In a claimed spare even the launch UUID may belong to the daemon creator.
  local lib_dir root python answer status source="${BASH_SOURCE[0]}"
  source="${source//\\//}"
  while [[ -h "$source" ]]; do
    lib_dir="$(cd -P "$(dirname "$source")" && pwd)"
    source="$(readlink "$source")"
    source="${source//\\//}"
    [[ "$source" == /* ]] || source="$lib_dir/$source"
  done
  lib_dir="$(cd -P "$(dirname "$source")" && pwd)"
  root="$(cd "$lib_dir/../.." && pwd)"
  if [[ -f "$root/scripts/session_identity.py" ]]; then
    python="$root/.venv/bin/python"
    [[ -x "$python" ]] || python="$root/.venv/Scripts/python.exe"
    [[ -x "$python" ]] || python="python3"
    if answer="$("$python" "$root/scripts/session_identity.py" registry 2>/dev/null)"; then
      status="${answer%%$'\n'*}"
      RESUME_REGISTRY_STATUS="$status"
      if [[ "$status" == match ]]; then RESUME_REGISTRY_NAME="${answer#*$'\n'}"; fi
      return 0
    fi
    RESUME_REGISTRY_STATUS="unavailable"
    return 0
  fi
  [[ -n "$sid" ]] || return 0
  command -v jq >/dev/null 2>&1 || { RESUME_REGISTRY_STATUS="unavailable"; return 0; }

  for f in "$HOME"/.claude/sessions/*.json; do
    [[ -f "$f" ]] || continue
    candidate="$(jq -er --arg sid "$sid" \
      'select(.sessionId == $sid and (.name | type == "string") and (.name | length > 0)) | .name' \
      "$f" 2>/dev/null)" || continue
    if (( found == 0 )); then
      name="$candidate"
      found=1
    elif [[ "$candidate" != "$name" ]]; then
      RESUME_REGISTRY_STATUS="ambiguous"
      return 0
    fi
  done

  if (( found == 1 )); then
    RESUME_REGISTRY_NAME="$name"
    RESUME_REGISTRY_STATUS="match"
  fi
}

# Derive the per-session mapping key into $RESUME_KEY. A matching session-registry
# name wins over a disagreeing task-list id because the latter can be stale in a
# bridged background job. Returns non-zero (and sets RESUME_KEY=unknown) if the raw
# id contains a path-traversal token — the caller decides whether to fall back or bail.
resume_derive_key() {
  local task_list_id="${CLAUDE_CODE_TASK_LIST_ID:-}" k
  resume_registry_name
  if [[ "$RESUME_REGISTRY_STATUS" == "match" ]]; then
    k="$RESUME_REGISTRY_NAME"
    if [[ -n "$task_list_id" && "$task_list_id" != "$RESUME_REGISTRY_NAME" ]]; then
      printf 'resume_derive_key: task-list id %q disagrees with registry name %q for session %q; using registry name\n' \
        "$task_list_id" "$RESUME_REGISTRY_NAME" "${CLAUDE_CODE_SESSION_ID:-}" >&2
    fi
  elif [[ "$RESUME_REGISTRY_STATUS" == "ambiguous" || "$RESUME_REGISTRY_STATUS" == "unverified" ]]; then
    k="${CLAUDE_CODE_SESSION_ID:-}"
    printf 'resume_derive_key: conflicting registry names for session %q; using session id instead of task-list id\n' \
      "${CLAUDE_CODE_SESSION_ID:-}" >&2
  else
    k="$task_list_id"
  fi
  [[ -z "$k" ]] && k="${CLAUDE_CODE_SESSION_ID:-}"
  [[ -z "$k" ]] && k="dir-$(pwd | cksum | cut -d' ' -f1)"
  case "$k" in *[/\\]* | *..*) RESUME_KEY="unknown"; return 1 ;; esac
  k="${k//[^A-Za-z0-9._-]/_}"
  [[ -z "$k" ]] && { RESUME_KEY="unknown"; return 1; }
  RESUME_KEY="$k"
  return 0
}

# Emit the fingerprint front-matter (bounded by two --- delimiters) to stdout.
# Assumes resume_derive_key already set RESUME_KEY.
resume_fingerprint() {
  printf -- '---\n'
  printf 'key: %s\n' "${RESUME_KEY:-unknown}"
  printf 'task_list_id: %s\n' "${CLAUDE_CODE_TASK_LIST_ID:-}"
  printf 'registry_name: %s\n' "${RESUME_REGISTRY_NAME:-}"
  printf 'registry_status: %s\n' "${RESUME_REGISTRY_STATUS:-unresolved}"
  printf 'session_id: %s\n' "${CLAUDE_CODE_SESSION_ID:-}"
  printf 'cwd: %s\n' "$(pwd)"
  printf 'created_at: %s\n' "$(date +%s)"
  printf -- '---\n'
}

# ── ISSUE-REF AC-2: the first post-compact turn, and the two lateness intervals ──
#
# TWO intervals govern an injected delivery. They are deliberately DIFFERENT
# measurements with DIFFERENT thresholds, and naming their bounding events is itself
# an acceptance criterion of ISSUE-REF.
#
#   ACK-LATENESS  (the lateness clock; option (b), the owner's ruling 2026-10-08)
#     bounded by  the receiving session's FIRST POST-COMPACT TURN  ->  now
#     threshold   AIH_RESUME_ACK_TIMEOUT_SECONDS   (default 600)
#     applies to  a delivery whose first turn was recorded (firstturn/<key>.stamp)
#     This is the only interval the acking session can influence: it starts when the
#     session gets control, so it measures "given control, did it run the ack".
#
#   FIRST-TURN BACKSTOP  (option (a), retained as the backstop)
#     bounded by  INJECTION (the <ts> in the filename)  ->  now
#     threshold   AIH_RESUME_FIRST_TURN_TIMEOUT_SECONDS   (default 14400 = 4 h)
#     applies to  a delivery with NO recorded first turn
#     A session that never starts must still be dead-lettered, and nothing else would
#     catch it -- the ack-lateness clock cannot expire for a session whose clock never
#     started. Keeping this leg is why "the session never woke" and "the session woke
#     and ignored the delivery" stay distinguishable in failed/.
#
# A third rule in the same loop is NOT an interval and has NO threshold: a delivery
# SUPERSEDED by a newer injection for the same key is dead-lettered immediately. It is
# not awaiting a first turn, so neither interval describes it, and leaving it in
# injected/ would misreport it as in flight to the ack, the wake legs and an operator.
#
# WHY the ack-lateness interval had to be invented: injection-to-ack measures
# post-compact context RECONSTRUCTION, which completes before the session's first turn
# and scales with context size. Measured on example-project-ec2 2026-10-07, same key, same
# 58-line body, same binary: 1046 s REFUSED, 180 s CONFIRMED, and ISSUE-REF CONFIRMED in
# under a minute from the same injection batch seventeen seconds apart. Everything the
# runs shared is therefore eliminated; the only variable left was reconstruction cost.
# A threshold over that interval measures the payload rather than the session, and it
# fails the LARGEST payloads first -- the sessions whose resume matters most.
#
# WHY the backstop is 4 h rather than the ack timeout: the largest reconstruction
# actually measured is 1046 s, so 14400 leaves roughly 14x headroom over the observed
# worst case while still dead-lettering a delivery whose session never returned inside
# a working half-day. Giving both intervals ONE number is how the two measurements got
# conflated in the first place, so they get two.

# Path of the first-post-compact-turn stamp for a key. ONE file per key, overwritten by
# the next delivery's first turn, so this directory is bounded by the number of session
# keys and needs no GC -- unlike pollers/, whose per-attempt files leaked (ISSUE-REF).
resume_first_turn_path() { printf '%s/firstturn/%s.stamp' "$(resume_root)" "${1:-unknown}"; }

# Record that the receiving session has reached its first post-compact turn, starting
# the ACK-LATENESS interval for the newest injected delivery of this key.
#
# NO-OP WITH NOTHING IN FLIGHT. With no injected delivery for the key there is no
# interval to start, so nothing is created -- not the file, not the directory. Every
# prompt of every session calls this (UserPromptSubmit), so the common case must cost
# one glob and no more.
#
# WRITTEN ONCE PER DELIVERY. The datum is the FIRST turn. Re-stamping on each later
# turn would push the deadline forward forever, so the ack timeout could never expire
# and failed/ would stop meaning a genuine failure -- silently retiring AC-4. A stamp
# naming a DIFFERENT delivery_ts is replaced, because it belongs to a superseded
# delivery.
#
# INDEPENDENT OF THE TYPED POST-COMPACT WAKE. The wake legs are what may CAUSE a first
# turn; this records only that a turn happened, so it reads no wake configuration and
# is unaffected by post_compact_typed_wake_enabled being false (which it is, by
# default, and this is not the place that would change it).
#
# Swallows every failure and returns 0: observability must never break delivery, and a
# stamp that could not be written degrades the delivery to the BACKSTOP interval, which
# resume_sweep logs as such rather than reporting a late ack.
#   resume_mark_first_turn <key>
resume_mark_first_turn() {
  local key="${1:-}" root f b stem ts existing path
  [[ -n "$key" ]] || return 0
  # `unknown` is how resume_derive_key spells an unusable identity; it names no
  # session, so it must not get a stamp either.
  case "$key" in *[/\\]* | *..* | unknown) return 0 ;; esac
  root="$(resume_root)"

  ts=""
  for f in "$root"/injected/*.md; do
    [[ -e "$f" ]] || continue
    b="${f##*/}"; stem="${b%.md}"
    [[ "${stem%-*}" == "$key" ]] || continue
    [[ "${stem##*-}" =~ ^[0-9]+$ ]] || continue
    # Glob is lexically sorted and the epoch is fixed-width, so last == newest --
    # the same rule save-resume --ack applies to pick its own subject. Only the
    # timestamp is kept: the stamp binds to the DELIVERY, never to a path.
    ts="${stem##*-}"
  done
  [[ -n "$ts" ]] || return 0

  path="$(resume_first_turn_path "$key")"
  if [[ -r "$path" ]]; then
    existing="$(sed -n 's/^delivery_ts=\([0-9]\{1,\}\)$/\1/p' "$path" 2>/dev/null | head -n 1)"
    if [[ "$existing" == "$ts" ]]; then
      return 0
    fi
  fi

  mkdir -p "$root/firstturn" 2>/dev/null || return 0
  printf 'delivery_ts=%s\nfirst_turn_at=%s\n' "$ts" "$(date +%s)" > "$path" 2>/dev/null || true
  return 0
}

# Print the recorded first-turn epoch for <key> IF the stamp belongs to delivery <ts>.
# Prints NOTHING when there is no stamp, when it is unreadable, or when it names a
# different delivery -- all of which mean "this delivery's first turn was not
# recorded", which is exactly what puts it on the backstop interval.
#   resume_first_turn_at <key> <delivery-ts>
resume_first_turn_at() {
  local path stamp_ts at
  path="$(resume_first_turn_path "${1:-}")"
  [[ -r "$path" ]] || return 0
  stamp_ts="$(sed -n 's/^delivery_ts=\([0-9]\{1,\}\)$/\1/p' "$path" 2>/dev/null | head -n 1)"
  [[ "$stamp_ts" == "${2:-}" ]] || return 0
  at="$(sed -n 's/^first_turn_at=\([0-9]\{1,\}\)$/\1/p' "$path" 2>/dev/null | head -n 1)"
  [[ "$at" =~ ^[0-9]+$ ]] || return 0
  printf '%s' "$at"
  return 0
}

# Sweep the lifecycle dirs. Cheap; safe to call on every hook fire / write.
#   injected/<key>-<ts>.md past the interval that governs it -> failed/ (see above)
#   confirmed/ + failed/ older than the archive TTL          -> deleted (GC)
# The BACKSTOP interval's age comes from the <ts> in the filename (inject time), not
# mtime (mv preserves the older save-time mtime, which would wrongly sweep a
# just-injected file). The ACK-LATENESS interval's age comes from the first-turn stamp.
#
# Options:
#   --except-file <path>  Exclude this specific file from the injected sweep
#                         (ISSUE-REF: prevents an ack from consuming its own subject)
resume_sweep() {
  local root ack_timeout first_turn_timeout archive_ttl now f b stem ts k age pid
  local first_turn_at interval limit g g_stem superseded except_file=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --except-file)
        except_file="${2:-}"
        shift 2
        ;;
      *)
        shift
        ;;
    esac
  done

  root="$(resume_root)"
  ack_timeout="${AIH_RESUME_ACK_TIMEOUT_SECONDS:-600}"
  first_turn_timeout="${AIH_RESUME_FIRST_TURN_TIMEOUT_SECONDS:-14400}"
  archive_ttl="${AIH_RESUME_ARCHIVE_TTL_SECONDS:-604800}"
  now="$(date +%s)"
  mkdir -p "$root/outbox" "$root/injected" "$root/confirmed" "$root/failed"

  for f in "$root"/injected/*.md; do
    [[ -e "$f" ]] || continue
    # ISSUE-REF: skip the specific file being acked so the ack cannot consume its own delivery
    if [[ -n "$except_file" && "$f" == "$except_file" ]]; then
      continue
    fi
    b="${f##*/}"; stem="${b%.md}"; ts="${stem##*-}"; k="${stem%-*}"
    [[ "$ts" =~ ^[0-9]+$ ]] || continue

    # SUPERSEDED FIRST, and it is not a lateness judgement at all. A newer injection
    # for the same key has replaced this one -- the ack, the banner and every wake leg
    # all select the newest -- so this delivery is not awaiting a first turn and no
    # interval applies to it. Holding it on the 4 h backstop would leave superseded
    # entries sitting in injected/, which everything else reads as "in flight".
    #
    # Nested glob rather than a newest-per-key map because this library runs on
    # macOS's bash 3.2.57, which has no associative arrays. The inner loop is over the
    # deliveries in flight for ONE key, normally exactly one. The key is already
    # restricted to [A-Za-z0-9._-] by resume_derive_key, so it carries no glob
    # metacharacter.
    superseded=0
    for g in "$root"/injected/"$k"-*.md; do
      [[ -e "$g" ]] || continue
      g_stem="${g##*/}"; g_stem="${g_stem%.md}"
      [[ "${g_stem%-*}" == "$k" ]] || continue
      [[ "${g_stem##*-}" =~ ^[0-9]+$ ]] || continue
      if (( ${g_stem##*-} > ts )); then superseded=1; break; fi
    done

    # Otherwise WHICH interval governs is decided per delivery, by whether its first
    # turn was recorded. See the block above resume_first_turn_path for both.
    first_turn_at="$(resume_first_turn_at "$k" "$ts")"
    if [[ -n "$first_turn_at" ]]; then
      interval="ack-lateness"; age=$(( now - first_turn_at )); limit="$ack_timeout"
    else
      interval="first-turn-backstop"; age=$(( now - ts )); limit="$first_turn_timeout"
    fi
    if (( superseded )); then
      interval="superseded"; age=$(( now - ts )); limit=0
    fi
    if (( superseded )) || (( age > limit )); then
      mv -f "$f" "$root/failed/$b" 2>/dev/null || true
      # Name the interval that condemned it. A delivery dead-lettered on the BACKSTOP
      # is a SHORTFALL -- its first turn was never recorded -- and saying so is what
      # stops an operator or a poller reading it as a late ack (ISSUE-REF AC-4). The
      # ack-lateness case is logged too, so no failed/ entry is anonymous; resume_sweep
      # previously logged nothing at all, which the lifecycle audit flagged.
      resume_log SWEEP "$k" "file=$b interval=$interval age_s=$age limit_s=$limit" || true
    fi
  done

  for f in "$root"/confirmed/*.md "$root"/failed/*.md; do
    [[ -e "$f" ]] || continue
    b="${f##*/}"; stem="${b%.md}"; ts="${stem##*-}"
    [[ "$ts" =~ ^[0-9]+$ ]] || continue
    age=$(( now - ts ))
    if (( age > archive_ttl )); then
      rm -f "$f"
    fi
  done

  # ISSUE-REF: pollers/ gained one status file plus one lock per compact attempt and nothing
  # ever reaped them. It is swept here rather than by a new mechanism because the sibling
  # directories above already establish the convention, and the ISSUE-REF agent that found
  # this correctly declined to invent a second one.
  #
  # SAME WINDOW as the archive sweep above, reusing archive_ttl rather than adding a knob.
  # These are post-mortem records of the same episode the archive keeps, so a separate
  # retention number would be a second thing to get wrong for no gain; the deliberate
  # decision is to inherit, and AIH_RESUME_ARCHIVE_TTL_SECONDS already overrides both.
  #
  # Aged by MTIME, not by a filename timestamp: a poller record is keyed
  # <key>__<attempt-id>.json and carries no timestamp to parse, so the `ts` trick the loops
  # above use does not apply here. `find -mmin` rather than `stat`, because `stat -f %m`
  # (BSD) and `stat -c %Y` (GNU) are different commands and this library runs on both --
  # the same reason resume_stage_reap below uses find.
  #
  # LIVENESS IS NEVER WAIVED, and it wins over age unconditionally. The lifecycle observer
  # exits on terminal attempt state, confirmed compaction, SessionEnd or a missing
  # transcript -- never on elapsed wall time -- so a genuinely long-lived poller is possible
  # and keeping its record is correct rather than a leak. A live PID therefore retains the
  # record whatever its age, which also means PID reuse can only ever cause us to KEEP a
  # stale record, never to delete a live one. Ambiguity defaults to keep, as everywhere else
  # in this tree.
  #
  # The lock is removed only alongside its own status file, so a lock can never outlive the
  # record whose liveness it represents.
  for f in "$root"/pollers/*.json; do
    [[ -e "$f" ]] || continue
    [[ -n "$(find "$f" -mmin +"$(( archive_ttl / 60 ))" 2>/dev/null)" ]] || continue
    pid="$(grep -o '"pid"[[:space:]]*:[[:space:]]*[0-9]\{1,\}' "$f" 2>/dev/null \
      | head -n 1 | grep -o '[0-9]\{1,\}$')"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      continue
    fi
    rm -f "$f" "${f%.json}.lock" 2>/dev/null || true
  done
}

# ── ISSUE-REF: lifecycle observability log + auto-compact safety marker ─────────

# Append one line to a log file, then ring-trim it to the cap. Observability must
# NEVER break delivery, so every failure is swallowed and the function returns 0.
#   _resume_log_append_ring <logfile> <line> <max-lines>
_resume_log_append_ring() {
  local logf="$1" line="$2" max="$3" n tmp
  printf '%s\n' "$line" >> "$logf" 2>/dev/null || return 0
  # `wc -l < file` pads with leading spaces on macOS/BSD — strip to bare digits.
  n="$(wc -l < "$logf" 2>/dev/null | tr -dc '0-9')"
  # Trim only once the file is 10% over the cap, so a full log is rewritten once per
  # max/10 appends rather than on every one.
  # A unique temp file beside the log (one filesystem, so the rename is atomic): a fixed
  # `$logf.tmp` let a second trim truncate the first one's half-written copy, publish it,
  # and then take the first one's remaining writes into the live log (ISSUE-REF).
  if [[ -n "$n" ]] && (( n > max + max / 10 )); then
    tmp="$(mktemp "$logf.tmp.XXXXXX" 2>/dev/null)" || return 0
    if ! { tail -n "$max" "$logf" > "$tmp" 2>/dev/null && mv -f "$tmp" "$logf" 2>/dev/null; }; then
      rm -f "$tmp" 2>/dev/null || true
    fi
  fi
  return 0
}

# Append one structured line to the shared aggregate log AND this session's own
# per-key log, each independently ring-trimmed to the cap. Observability must
# NEVER break delivery, so every failure is swallowed and the function returns 0
# (safe under the caller's `set -euo pipefail`).
#   resume_log <STAGE> <key> <detail...>
resume_log() {
  local stage="${1:-?}" key="${2:-unknown}"
  shift 2 2>/dev/null || true
  local detail="$*" root logdir logf sharedf max ts safe_key line
  root="$(resume_root)"
  safe_key="${key//[^A-Za-z0-9._-]/_}"
  logdir="$root/logs"
  logf="$logdir/${safe_key}.log"
  sharedf="$root/resume.log"
  # 500 held ~5 h of aih-ISSUE-REF on 2026-10-13: the 06:11Z save-state attempt and the
  # 14:08Z arm of the next one had rotated out before anyone looked (ISSUE-REF).
  max="${AIH_RESUME_LOG_MAX_LINES:-5000}"
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)" || ts="?"
  # Trailing space after the key so a later grep can match `key=<k> ` exactly and
  # never confuse `ISSUE-REF` with `ISSUE-REF-extra` (prefix-collision safety, cf. ISSUE-REF R5).
  line="$(printf '%s %-10s key=%s %s' "$ts" "$stage" "$key" "$detail")"
  mkdir -p "$root" 2>/dev/null || return 0
  _resume_log_append_ring "$sharedf" "$line" "$max"
  mkdir -p "$logdir" 2>/dev/null || return 0
  _resume_log_append_ring "$logf" "$line" "$max"
  return 0
}

# Print the 1-minute load average, or the literal `NA` when no source answers.
#
# ISSUE-REF: the ENTER breadcrumb in resume-inject.sh read `/proc/loadavg` directly.
# That file is Linux-only, so on macOS the probe could not fail -- it printed NA
# unconditionally. Measured 2026-09-24 on example-client-host-mac: 162 of 162 ENTER lines across every
# file in ~/.claude/resume/logs/ recorded `load=NA`. The load figure is the datum that
# decides whether a resume was killed by its hook budget under contention (ISSUE-REF),
# and a value that is absent on a whole platform makes that question undecidable
# rather than merely unanswered.
#
# /proc first so Linux behaviour is bit-for-bit unchanged. `sysctl -n vm.loadavg`
# is the Darwin/BSD source and returns the three figures wrapped in braces
# (measured on example-client-host-mac: `{ 7.11 6.74 6.84 }`). `uptime` is deliberately NOT the
# route: its label differs across platforms (`load averages:` on macOS,
# `load average:` with comma separators on Linux), so parsing it is a per-platform
# FORMAT dependency where sysctl is a per-platform SOURCE.
#
# Both sources are then word-split and the FIRST numeric field is taken, rather
# than a fixed field index. That is what makes the brace wrapper a non-issue
# instead of a substitution to get right: `${raw//[{}]/}` looks correct and is
# not -- bash ends the expansion at the `}` INSIDE the bracket expression, so it
# yielded the literal `{` and this probe returned NA on the very platform it was
# written for (caught by the negative control below, which is the whole reason
# that control exists). Emitting `NA` stays the last rung: observability must
# never break delivery.
#
# AIH_RESUME_PROC_ROOT relocates /proc so BOTH branches are reachable from a test on
# either platform -- the same knob scripts/claude_sessions.py already exposes as
# CLAUDE_SESSION_PROC_ROOT, and for the same reason: a probe that can only take the
# branch its host happens to have is a probe that cannot fail on the other one, which
# is the defect this function fixes.
resume_load1() {
  local raw="" field proc_root="${AIH_RESUME_PROC_ROOT:-/proc}"
  if [[ -r "$proc_root/loadavg" ]]; then
    raw="$(cut -d' ' -f1 "$proc_root/loadavg" 2>/dev/null)"
  elif command -v sysctl >/dev/null 2>&1; then
    raw="$(sysctl -n vm.loadavg 2>/dev/null)"
  fi
  # shellcheck disable=SC2086
  set -- $raw
  for field in "$@"; do
    if [[ "$field" =~ ^[0-9]+([.,][0-9]+)?$ ]]; then
      printf '%s' "$field"
      return 0
    fi
  done
  printf 'NA'
  return 0
}

# ── ISSUE-REF: write-ahead stage journal (the only record that survives SIGKILL) ──
#
# The deliverer's EXIT trap records an aborted run (`NOOP reason=aborted stage=...`).
# A trap cannot run when the process is SIGKILLed, and SIGKILL is precisely what a
# hook-timeout enforcement looks like from inside: measured 2026-09-24 in
# ~/.claude/resume/logs/bootstrap-aih-generic-dragon.log, a delivery died between the
# key correction and the outbox move having written ENTER, then nothing -- no INJECT,
# no abort record, outbox intact. The EXIT trap wrote nothing, so the kill was
# uncatchable.
#
# So the record cannot be written AFTER the vulnerable window; it has to exist BEFORE
# it, which is the same argument the ENTER breadcrumb in resume-inject.sh already makes
# at its own call site (see its "LOG BEFORE DOING ANY WORK" comment). ENTER
# proves the hook started and says nothing about WHERE it stopped: `_stage` is a shell
# variable, and a SIGKILLed shell's variables are not evidence. These functions put
# that one variable on disk.
#
# Keyed by PID, not by session key: the key is CORRECTED mid-run (resume_derive_key),
# so a key-named file would be written under one name and reaped under another, and
# two concurrent runs could not be told apart. A PID is stable for the whole run and
# makes liveness decidable with `kill -0` alone -- never by reading an argv source,
# which can carry a run handle.
resume_stage_path() { printf '%s/state/stage-%s.stage' "$(resume_root)" "${1:-0}"; }

# Is a resume body blank -- empty, or nothing but whitespace? Returns 0 when blank.
#
# One function rather than the obvious inline test at each of the two call sites, because
# the obvious inline test is what broke delivery. `[[ -z "${body//[$' \t\n\r']/}" ]]` is
# superlinear in bash 3.2, which is what `#!/usr/bin/env bash` resolves to on every macOS
# host (`/bin/bash` is 3.2.57 there; Apple ships no newer GPL bash). Measured on example-client-host-mac
# against prefixes of one real resume body: 0.103 s at 500 B, 0.486 s at 1 KB, 2.273 s at
# 2 KB, 7.823 s at 4 KB, and 115.491 s at 10,598 B. The deliverer's hook budget is 30 s, so
# any body over roughly 7 KB got the hook SIGKILLed mid-stage and the whole hand-off was
# lost -- silently, because a SIGKILLed shell runs no trap (ISSUE-REF). Nothing else in
# that hook is remotely comparable: 0.018 s to source this library, 0.056 s for
# resume_sweep, 0.063 s for the front-matter strip.
#
# A glob match is flat instead, and forks nothing: 0.285 s at 10,598 B and 0.306 s at
# 31,794 B -- and that floor is the measuring harness's own `date` forks rather than the
# match. A regex test (`[[ $1 =~ [^[:space:]] ]]`) also clears the hang but is NOT flat
# (0.286 s -> 0.539 s across the same two sizes), so it is the worse choice for a payload
# that only grows as a session accumulates state.
#
# Never reintroduce a bracket-class substitution over a whole body here or at a call site;
# a grep assertion in tests/hooks/test_hooks.sh pins that, because a timing test alone is
# load-sensitive and would be the first thing quarantined as flaky.
resume_body_is_blank() {
  case "$1" in
    *[![:space:]]*) return 1 ;;
    *) return 0 ;;
  esac
}

# Record the stage a run is ENTERING, overwriting the previous record for that PID.
# This write's PURPOSE is the event, so it is deliberately unconditional (cf. the
# idempotent-artifact-writes rule, which exempts journals and cursors).
# Cost matters, because this runs on every phase transition INSIDE the budget whose
# expiry it exists to diagnose. So: no `date` fork (the file's own mtime carries the
# time, and resume_stage_reap ages it with `find -mmin`), the mkdir is guarded by a
# builtin test, and the directory is resolved once per process rather than per call.
# That last one is not premature: `$(resume_stage_path ...)` is a subshell fork, and at
# ten transitions it measured +130 ms of the hook's ~750 ms on local APFS -- on the EFS
# mount where this hook's other comments measure 2-4 s per interpreter start, eleven
# avoidable forks are not free. Caching is safe because $HOME does not change inside a
# single hook run; a caller that did change it mid-process would need to unset this.
#   resume_stage_mark <pid> <key> <stage>
resume_stage_mark() {
  if [[ -z "${_RESUME_STAGE_DIR:-}" ]]; then
    _RESUME_STAGE_DIR="$(resume_root)/state"
    [[ -d "$_RESUME_STAGE_DIR" ]] || mkdir -p "$_RESUME_STAGE_DIR" 2>/dev/null || return 0
  fi
  printf 'pid=%s key=%s stage=%s\n' "${1:-0}" "${2:-unknown}" "${3:-?}" \
    > "$_RESUME_STAGE_DIR/stage-${1:-0}.stage" 2>/dev/null || true
  return 0
}

# Drop a run's stage record. Called from the deliverer's EXIT trap, so it covers every
# CATCHABLE exit; a surviving file therefore means the run did not exit catchably.
resume_stage_clear() {
  rm -f "$(resume_stage_path "${1:-0}")" 2>/dev/null || true
  return 0
}

# Convert stranded stage records into log lines. A record is stranded when its PID is
# gone, or when it is older than the reap age even if some process now holds that PID
# (PIDs are reused, so liveness alone would let one stranded record sit forever and
# never be reported).
#
# The reap age must exceed the hook's declared timeout -- measured 30 s in
# ~/.claude/settings.json and projects-wide-session-config/hooks-manifest.json on
# 2026-09-24 -- or a slow but healthy run would be reported dead while still working.
# `find -mmin` rather than `stat`, because `stat -f %m` (BSD) and `stat -c %Y` (GNU)
# are not the same command and this library runs on both.
#
# Observability must never break delivery: every failure is swallowed, return is 0.
resume_stage_reap() {
  local root f b pid rec key stage mins line
  root="$(resume_root)"
  mins="${AIH_RESUME_STAGE_REAP_MINUTES:-5}"
  for f in "$root"/state/stage-*.stage; do
    [[ -e "$f" ]] || continue
    b="${f##*/}"; pid="${b#stage-}"; pid="${pid%.stage}"
    if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
      rm -f "$f" 2>/dev/null || true
      continue
    fi
    if [[ "$pid" == "$$" ]]; then
      continue
    fi
    if kill -0 "$pid" 2>/dev/null; then
      # Possibly a live sibling run, so an in-age record is left alone. An over-age one
      # is still reported, because PIDs are reused and liveness alone would let a
      # stranded record sit forever unreported -- but it is labelled as ambiguous
      # rather than as a confirmed death.
      if [[ -z "$(find "$f" -mmin +"$mins" 2>/dev/null)" ]]; then
        continue
      fi
      rec="pid-live-over-age"
    else
      rec="uncatchable-exit"
    fi
    key="unknown"; stage="?"
    if [[ -r "$f" ]]; then
      while IFS= read -r line; do
        case "$line" in
          *key=*stage=*)
            key="${line#*key=}"; key="${key%% *}"
            stage="${line##*stage=}"; stage="${stage%% *}"
            ;;
        esac
      done < "$f"
    fi
    rm -f "$f" 2>/dev/null || true
    resume_log NOOP "$key" "reason=$rec stage=$stage dead-pid=$pid" || true
  done
  return 0
}

# Path of the auto-compact safety marker for a key.
resume_marker_path() { printf '%s/auto-compact-%s.marker' "$(resume_root)" "${1:-unknown}"; }

# Write the auto-compact marker (D5 = marker-only: RECORD the boundary, never touch
# git). Overwrites any prior marker for the key. Swallows all failures, returns 0.
#   resume_write_marker <key> <detail...>
resume_write_marker() {
  local key="${1:-unknown}"
  shift 2>/dev/null || true
  local root d; root="$(resume_root)"
  mkdir -p "$root" 2>/dev/null || return 0
  {
    printf 'ts=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
    printf 'cwd=%s\n' "$(pwd)"
    # Each detail arg on its OWN line (e.g. `wip=...`), so a consumer can grep `^wip=`.
    for d in "$@"; do printf '%s\n' "$d"; done
  } > "$(resume_marker_path "$key")" 2>/dev/null || true
  return 0
}
