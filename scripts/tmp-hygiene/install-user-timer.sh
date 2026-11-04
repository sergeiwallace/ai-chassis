#!/usr/bin/env bash
# Install the host-local pytest temporary-root hygiene timer for this checkout.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
STATE_DIR="${AI_HARNESS_INODE_TELEMETRY_STATE_DIR:-/var/tmp/ai-harness-tmp-hygiene/inode-creation-telemetry}"

if ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "systemd --user is unavailable; cannot install the tmp-hygiene timer" >&2
    exit 1
fi

mkdir -p "$UNIT_DIR"
if [[ -L "$STATE_DIR" || ( -e "$STATE_DIR" && ! -d "$STATE_DIR" ) ]]; then
    echo "telemetry state path must be a real directory: $STATE_DIR" >&2
    exit 1
fi
# `-O` is bash's own "owned by the effective uid" test: no subprocess, and no
# `stat -c` (GNU-only -- macOS stat rejects the option outright, which left the
# comparison reading an empty string and every run refusing with the wrong
# reason).  Effective uid is also the more correct comparand than `id -u`'s real
# uid, since `install -d` below creates as the effective user.
if [[ -d "$STATE_DIR" && ! -O "$STATE_DIR" ]]; then
    echo "telemetry state directory is not owned by the invoking user: $STATE_DIR" >&2
    exit 1
fi
install -d -m 0700 "$STATE_DIR"
if [[ -L "$STATE_DIR" || ! -O "$STATE_DIR" ]]; then
    echo "failed to provision a private telemetry state directory: $STATE_DIR" >&2
    exit 1
fi
install -m 0644 "$SCRIPT_DIR/ai-harness-tmp-hygiene.service" "$UNIT_DIR/"
install -m 0644 "$SCRIPT_DIR/ai-harness-tmp-hygiene.timer" "$UNIT_DIR/"
install -m 0644 "$SCRIPT_DIR/ai-harness-tmp-guard.service" "$UNIT_DIR/"
install -m 0644 "$SCRIPT_DIR/ai-harness-tmp-guard.timer" "$UNIT_DIR/"
install -m 0644 "$SCRIPT_DIR/ai-harness-inode-telemetry.service" "$UNIT_DIR/"
install -m 0644 "$SCRIPT_DIR/ai-harness-inode-telemetry.timer" "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now ai-harness-tmp-hygiene.timer
systemctl --user enable --now ai-harness-tmp-guard.timer
systemctl --user enable --now ai-harness-inode-telemetry.timer
systemctl --user status ai-harness-tmp-hygiene.timer --no-pager
systemctl --user status ai-harness-tmp-guard.timer --no-pager
systemctl --user status ai-harness-inode-telemetry.timer --no-pager
