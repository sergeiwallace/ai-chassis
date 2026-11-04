#!/usr/bin/env bash
# Install the root-owned systemd-tmpfiles backstop for stale test scratch.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$SCRIPT_DIR/ai-harness-tmpfiles.conf"
DESTINATION=/etc/tmpfiles.d/ai-harness-tmpfiles.conf

if [[ "${EUID}" -ne 0 ]]; then
    echo "run with sudo: sudo bash $0" >&2
    exit 1
fi

if ! command -v systemd-tmpfiles >/dev/null; then
    echo "systemd-tmpfiles is required" >&2
    exit 1
fi

if [[ ! -f "$SOURCE" ]]; then
    echo "missing tmpfiles rule: $SOURCE" >&2
    exit 1
fi

if ! cmp -s "$SOURCE" "$DESTINATION" 2>/dev/null; then
    install -D -m 0644 "$SOURCE" "$DESTINATION"
fi

systemd-tmpfiles --create --dry-run
systemd-tmpfiles --clean
