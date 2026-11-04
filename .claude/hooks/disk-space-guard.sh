#!/usr/bin/env bash
# SessionStart hook (ISSUE-REF): warn when a distinct filesystem holding $HOME,
# /tmp, or / is close to full, on BYTES or on INODES.
#
# The illustrative figures in this paragraph were genericized for public release.
# Why both dimensions: a development environment filled a 10G $HOME
# volume to 100% of bytes AND 100% of inodes with ~5G of regenerable tool caches
# (uv, playwright, pre-commit, npm, pip, old CC versions). The uv cache alone held
# ~10,000 inodes across tiny files, so that volume can reach ENOSPC with gigabytes
# of bytes still free. A guard reading only `df -h` would have stayed green through
# part of the real failure. Nothing warned; the first symptom was Claude Code
# failing to write its own transcript.
#
# NEVER blocks and NEVER fails a session start: exit 0 on every path, including a
# missing `df`, a `df` that errors, an unparseable table, and a nonexistent path.
# A guard that hangs would trade a slow disk fill for a hard stop, so the manifest
# entry also caps it with a hook timeout.
#
# Threshold: AIH_DISK_WARN_PCT (integer percent, default 85). A non-integer or
# out-of-range value falls back to the default rather than erroring.
#
# Read-only by design: this hook writes nothing at all (ISSUE-REF false-event class).

set -uo pipefail

# Drain the SessionStart payload; no field of it is needed, so it lands in `_`.
# `read` is a builtin (`cat` would fork), and `read -d ''` returns non-zero at EOF,
# which is expected for stdin with no NUL terminator.
IFS= read -r -d '' -t 2 _ || true

command -v df >/dev/null 2>&1 || exit 0
command -v awk >/dev/null 2>&1 || exit 0

THRESHOLD="${AIH_DISK_WARN_PCT:-85}"
case "$THRESHOLD" in
  '' | *[!0-9]*) THRESHOLD=85 ;;
esac
[[ "$THRESHOLD" -ge 1 && "$THRESHOLD" -le 100 ]] || THRESHOLD=85

# Select the percentage column BY HEADER NAME, never by position: GNU `df -Pi`
# emits "Filesystem Inodes IUsed IFree IUse% Mounted on" (percent in field 5)
# while BSD/macOS `df -Pi` emits "... Capacity iused ifree %iused Mounted on"
# (percent in field 8). Positional parsing reads the byte percentage as the inode
# percentage on macOS — silently green on the dimension this guard exists for.
_pct_column() {
  awk -v want="$1" '
    NR == 1 {
      for (i = 1; i <= NF; i++) if (tolower($i) ~ want) col = i
      next
    }
    NR == 2 && col {
      v = $col
      sub(/%$/, "", v)
      if (v ~ /^[0-9]+$/) print v
    }
  '
}

# Two `df` calls rather than one: GNU-only `df --output=pcent,ipcent` would read
# both dimensions in a single spawn but is unavailable on macOS, and this hook is
# wired fleet-wide. Each call costs ~1ms and is scoped to a single path.
# Keep the first readable target for a device and silently skip later aliases.
# A newline-delimited list avoids Bash 4 associative arrays, so this remains
# compatible with the Bash 3.2 shipped by macOS.
SEEN_DEVICES=$'\n'
WARNINGS=""
device_identity() {
  local identity
  identity="$(stat -c %d "$1" 2>/dev/null)" && [[ "$identity" =~ ^[0-9]+$ ]] && {
    printf '%s\n' "$identity"
    return
  }
  identity="$(stat -f %d "$1" 2>/dev/null)" && [[ "$identity" =~ ^[0-9]+$ ]] && {
    printf '%s\n' "$identity"
    return
  }
  return 1
}
for TARGET in "${HOME:-/}" /tmp /; do
  [[ -e "$TARGET" ]] || continue
  BYTES_TABLE="$(df -P -k "$TARGET" 2>/dev/null)"
  DEVICE="$(device_identity "$TARGET" || true)"
  if [[ -n "$DEVICE" ]]; then
    case "$SEEN_DEVICES" in
      *$'\n'"$DEVICE"$'\n'*) continue ;;
    esac
    SEEN_DEVICES+="${DEVICE}"$'\n'
  fi

  BYTES_PCT="$(printf '%s\n' "$BYTES_TABLE" | _pct_column '^(use%|capacity)$')"
  INODE_PCT="$(df -P -i "$TARGET" 2>/dev/null | _pct_column '^(iuse%|%iused|iused%|icapacity)$')"

  # Either dimension crossing warns; either being unreadable (empty, "-", a
  # filesystem that does not report inodes) is skipped for this target only.
  CROSSED=""
  if [[ -n "$BYTES_PCT" ]] && [[ "$BYTES_PCT" -ge "$THRESHOLD" ]]; then
    CROSSED="bytes ${BYTES_PCT}%"
  fi
  if [[ -n "$INODE_PCT" ]] && [[ "$INODE_PCT" -ge "$THRESHOLD" ]]; then
    CROSSED="${CROSSED:+${CROSSED}, }inodes ${INODE_PCT}%"
  fi
  [[ -n "$CROSSED" ]] || continue
  WARNINGS="${WARNINGS:+${WARNINGS}; }the filesystem holding ${TARGET} is at ${CROSSED}"
done
[[ -n "$WARNINGS" ]] || exit 0

MSG="DISK SPACE WARNING: ${WARNINGS} (threshold ${THRESHOLD}%)."
MSG="${MSG} Both bytes and inodes are checked independently; either one hitting 100% causes ENOSPC,"
MSG="${MSG} which breaks Claude Code's own transcript and lock-file writes. Tell the user now."
MSG="${MSG} Regenerable tool caches are the usual cause (uv, playwright, pre-commit, npm, pip, old"
MSG="${MSG} Claude Code versions). Tune or silence with AIH_DISK_WARN_PCT."

# JSON-escape without a jq/python dependency (same approach as docs-priming.sh).
ESCAPED=${MSG//\\/\\\\}
ESCAPED=${ESCAPED//\"/\\\"}
printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$ESCAPED"
exit 0
