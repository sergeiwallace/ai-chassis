#!/usr/bin/env bash
# UserPromptSubmit Tier-2 recall without Claude Code's MCP-hook transport.
#
# MCP tool hooks require an already-connected server, so a cold connection fails
# before its timeout applies. Shelling out costs a CLI cold start, but keeps this
# hook independent of that connection state. Every failure path is fail-open:
# exit 2 would erase the submitted prompt.

set -uo pipefail

INPUT=""; IFS= read -r -d '' -t 2 INPUT || true

PROMPT=""
SESSION_ID="unknown"
CWD="unknown"
if [[ "$INPUT" =~ \"prompt\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then
  PROMPT="${BASH_REMATCH[1]}"
fi
if [[ "$INPUT" =~ \"session_id\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then
  SESSION_ID="${BASH_REMATCH[1]}"
fi
if [[ "$INPUT" =~ \"cwd\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then
  CWD="${BASH_REMATCH[1]}"
fi

command -v core >/dev/null 2>&1 || exit 0

# Bound the child without GNU coreutils. `timeout` is coreutils and macOS ships neither it nor
# `gtimeout` -- measured on example-client-host-mac, 2026-09-19, both `command -v` probes empty. The bare
# `timeout -k 1s 5s core recall ... 2>/dev/null || true` this replaced therefore failed on a Mac
# before `core` ever ran, and failed INVISIBLY: the redirect ate the shell's "command not found"
# and `|| true` ate the 127, so the hook exited 0 having recalled nothing. Tier-2 recall would
# have looked installed and silently done nothing on this machine for as long as it took anyone
# to notice the absence of context. Inlined rather than sourced from a shared lib because a hook
# has to be self-contained: the deadline test copies this file alone into a fixture.
#
# 124 on expiry, otherwise the command's own status. `set -m` gives the child its own process
# group so the signal reaches the CLI's own children, and the signalling region is
# stderr-suppressed because the shell would otherwise print a job notice coreutils never emits.
# BOUNDED RUNNER (ISSUE-REF). _AIH_HOOK_BUDGET is this hook's manifest timeout in seconds, 0 where
# the hook budgets itself or declares none. Each bound is cut to what the budget has left after the
# hook's own pre-work, less _AIH_HOOK_MARGIN for the TERM->KILL grace and one spawn, so the outer
# kill never lands first; under 1 s left the child is skipped (124). Without timeout/gtimeout (the
# Macs) ONE watchdog sleeps the whole bound: a `sleep` per poll cost 0.5-2 s a spawn under
# contention, so the loop spent the budget on its own ticks. `<&0` keeps a piped payload, since
# bash points an async job's stdin at /dev/null. 124 after TERM, 137 when KILL was needed.
_AIH_HOOK_BUDGET=10
_AIH_HOOK_MARGIN=2
_bounded() {
  local _s="$1"; shift
  if (( ${_AIH_HOOK_BUDGET:-0} > 0 )); then
    local _left=$(( _AIH_HOOK_BUDGET - SECONDS - ${_AIH_HOOK_MARGIN:-4} ))
    (( _s > _left )) && _s=$_left
    (( _s >= 1 )) || return 124
  fi
  if command -v timeout >/dev/null 2>&1; then timeout -k 1s "${_s}s" "$@"; return $?; fi
  if command -v gtimeout >/dev/null 2>&1; then gtimeout -k 1s "${_s}s" "$@"; return $?; fi
  local _sleep=sleep
  command -v sleep >/dev/null 2>&1 || _sleep=/bin/sleep
  set -m; "$@" <&0 & local _p=$!
  ( "$_sleep" "$_s"; kill -TERM "-$_p" 2>/dev/null || kill -TERM "$_p" 2>/dev/null
    "$_sleep" 1; kill -KILL "-$_p" 2>/dev/null || kill -KILL "$_p" 2>/dev/null ) >/dev/null 2>&1 &
  local _wd=$! _rc=0 _t0=$SECONDS; set +m
  {
    wait "$_p" || _rc=$?
    kill -KILL "-$_wd" 2>/dev/null || kill -KILL "$_wd" 2>/dev/null
    wait "$_wd"
  } 2>/dev/null
  # Timed out = died of the watchdog's TERM or KILL once the bound had run ($SECONDS is whole
  # seconds, so one second early is still the bound); any other status is the child's own.
  if (( (_rc == 143 || _rc == 137) && SECONDS - _t0 >= _s - 1 )); then
    if [[ "$_rc" -eq 137 ]]; then return 137; fi
    return 124
  fi
  return "$_rc"
}

# Keep the fail-open path below Claude Code's 10s per-hook ceiling even when
# the substrate backend is degraded and the CLI connection does not return.
_bounded 5 core recall "$PROMPT" --tier auto-recall --session "$SESSION_ID" \
  --repo "$CWD" --project "$CWD" 2>/dev/null || true
exit 0
