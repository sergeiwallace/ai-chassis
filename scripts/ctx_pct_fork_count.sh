#!/usr/bin/env bash
# ctx_pct_fork_count.sh -- count the processes one command creates, re-runnably, with no
# strace/perf/bpf (none of which the SageMaker container permits).
#
# METHOD. /proc/sys/kernel/ns_last_pid is the last PID the kernel handed out. Read it just
# before and just after the command: the two `cat`s are pids b and a themselves, so the
# processes created strictly between them number a-b-1. Concurrent activity elsewhere on
# the box can only INFLATE a sample, never deflate it, so the MINIMUM over N runs converges
# on the command's own count (the command's own process included when it is an external
# program; a bash builtin such as `true` counts 0, which is the control). On a busy host
# use a large N -- the same command measured 7 and 36 in two min-of-7 batches while five
# sessions were spawning ~100 processes/s, and min-of-40 settled it.
#
# This is a Linux-only DEVELOPMENT instrument, not a test: its answer is a lower bound that
# needs a quiet box to be exact, which is why the numbers it produced are recorded in
# docs/designs/ctx-pct-context-sensing.md rather than asserted in CI.
#
# Usage:
#   scripts/ctx_pct_fork_count.sh <runs> <command> [args...]
#   scripts/ctx_pct_fork_count.sh 40 env CTX_PCT_CACHE_TTL=0 .claude/bin/ctx-pct        # miss
#   scripts/ctx_pct_fork_count.sh 40 .claude/bin/ctx-pct --advice                       # hit
#   scripts/ctx_pct_fork_count.sh 40 env CTX_PCT_CACHE_TTL=0 CTX_PCT_MAXAGE=0 .claude/bin/ctx-pct  # transcript path
set -u
if [ "$#" -lt 2 ] || ! [[ "$1" =~ ^[0-9]+$ ]]; then
  echo "usage: $0 <runs> <command> [args...]" >&2
  exit 2
fi
if [ ! -r /proc/sys/kernel/ns_last_pid ]; then
  echo "ns_last_pid unavailable (Linux only)" >&2
  exit 3
fi
runs="$1"; shift
min=""
for ((i = 0; i < runs; i++)); do
  b=$(cat /proc/sys/kernel/ns_last_pid)
  "$@" >/dev/null 2>&1
  a=$(cat /proc/sys/kernel/ns_last_pid)
  d=$((a - b - 1))
  if [ -z "$min" ] || [ "$d" -lt "$min" ]; then min=$d; fi
done
echo "processes=$min method=ns_last_pid-min-of-$runs command=$*"
