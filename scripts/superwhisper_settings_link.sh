#!/usr/bin/env bash
# Link the git-tracked SuperWhisper settings file into the live app config location, so the
# app's own vocabulary/replacement edits land in the ai-harness-tracked file (ISSUE-REF/ISSUE-REF).
#
# Sourced by the private dictation-settings-link step (install.sh's retired Step 9b) and exercised
# directly by tests/test_superwhisper_settings_link.py. Kept as its own module rather than an
# inline block specifically so the decision logic is callable in isolation.
#
# WHY A LINK RATHER THAN A COPY. A copy drifts in both directions and nothing surfaces the
# divergence: the app's new vocabulary never reaches git, and a git pull never reaches the app.
# The link makes the tracked file and the live file the same file.
#
# WRITE-THROUGH IS MEASURED, NOT ASSUMED — this was ISSUE-REF's explicit blocking gate. On
# enterprise-windows 2026-08-22 SuperWhisper rewrote settings.json (9129 -> 9113 bytes, its own
# formatting) with the inode preserved and link count still 2. It edits in place rather than
# replacing via temp+rename, so a hardlink survives its writes.
set -euo pipefail

# Resolve the live settings path for a machine. Prints the path, or nothing when this machine
# has no known SuperWhisper install. Never guesses a path: an unknown host yields empty, which
# callers treat as "skip", per ISSUE-REF item 4.
superwhisper_target_for_host() {
  local host="$1" home="${2:-$HOME}"
  case "$host" in
    # Windows: CONFIRMED 2026-08-22 by locating the file the running app actually reads.
    enterprise-windows|enterprise-windows|managed-platform-windows)
      printf '%s\n' "$home/AppData/Local/com.superwhisper.app/settings/settings.json" ;;
    # macOS: the path ISSUE-REF recorded as confirmed.
    mac)
      printf '%s\n' "$home/superwhisper/settings/settings.json" ;;
    # Linux hosts and the SageMaker space have no SuperWhisper build — ISSUE-REF surveyed
    # cross-platform alternatives precisely because none exists. Nothing to link.
    *) printf '%s\n' "" ;;
  esac
}

# True when a SuperWhisper process is live. Override with SUPERWHISPER_RUNNING_CMD to inject a
# probe in tests; the default covers both POSIX pgrep and Windows tasklist.
superwhisper_is_running() {
  if [[ -n "${SUPERWHISPER_RUNNING_CMD:-}" ]]; then
    eval "$SUPERWHISPER_RUNNING_CMD"
    return $?
  fi
  if command -v pgrep >/dev/null 2>&1 && pgrep -i superwhisper >/dev/null 2>&1; then
    return 0
  fi
  if command -v tasklist >/dev/null 2>&1 \
     && tasklist //FI "IMAGENAME eq Superwhisper.exe" 2>/dev/null | grep -qi superwhisper; then
    return 0
  fi
  return 1
}

# Link $target -> $source. Echoes one status token on stdout so callers and tests can branch on
# the outcome without parsing prose:
#
#   linked        a real link now exists (symlink or shared inode)
#   already       target and source were already the same file; nothing written
#   skip-no-target        this machine has no known SuperWhisper path
#   skip-not-installed    the app data directory does not exist
#   skip-no-source        the tracked source file is missing
#   skip-running          the app is live; linking now would be clobbered on its quit
#   error-backup          the pre-replacement backup failed; live file left untouched
#   error-copy-fallback   only a copy was possible, so writes will NOT sync back
link_superwhisper_settings() {
  local source="$1" target="$2" backup_dir="$3"

  [[ -n "$target" ]] || { printf 'skip-no-target\n'; return 0; }
  [[ -f "$source" ]] || { printf 'skip-no-source\n'; return 0; }
  [[ -d "$(dirname "$target")" ]] || { printf 'skip-not-installed\n'; return 0; }

  # Idempotent by IDENTITY, not by content (ISSUE-REF). `-ef` is true for a shared inode, which is
  # what a hardlink is; a symlink to the source also resolves -ef true. Re-linking an
  # already-correct target would bump mtime and, worse, briefly unlink the app's live config.
  if [[ "$source" -ef "$target" ]]; then printf 'already\n'; return 0; fi

  # A RUNNING SuperWhisper flushes its stale in-memory settings over this file on quit, and with
  # the link in place that propagates backward and wipes the tracked vocabulary. Measured on
  # enterprise-windows 2026-08-22: linking under a running app lost all 68 replacements when the app was
  # later quit. Refuse rather than seed into a guaranteed clobber.
  if superwhisper_is_running; then printf 'skip-running\n'; return 0; fi

  # Back up the real file before replacing it (ISSUE-REF item 1), and GATE the replacement on the
  # backup succeeding — a refused backup must leave the live file intact rather than proceeding.
  if ! { mkdir -p "$backup_dir" \
         && cp -p "$target" "$backup_dir/settings_$(date +%Y-%m-%d_%H-%M-%S).json"; }; then
    printf 'error-backup\n'
    return 1
  fi

  # Native symlink first (portable, survives a target rewrite), then an NTFS hardlink, then a
  # copy as the last resort. On Windows a native symlink needs Developer Mode or elevation;
  # without it `ln -s` SILENTLY produces a copy, which is why the outcome is verified below
  # rather than inferred from ln's exit status.
  if [[ "${MSYSTEM:-}" =~ ^(MINGW|MSYS|CYGWIN) ]]; then
    MSYS=winsymlinks:nativestrict ln -s "$source" "$target" 2>/dev/null \
      || ln -f "$source" "$target" 2>/dev/null \
      || cp -f "$source" "$target"
  else
    ln -sf "$source" "$target"
  fi

  # Verify we got a LINK, not a copy. For this integration a copy is a silent correctness
  # failure, not a graceful degradation: the app's edits would never reach git.
  if [[ -L "$target" ]] || [[ "$source" -ef "$target" ]]; then
    printf 'linked\n'
    return 0
  fi
  printf 'error-copy-fallback\n'
  return 1
}
