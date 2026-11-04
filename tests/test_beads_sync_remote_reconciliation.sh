#!/usr/bin/env bash
# ISSUE-REF: beads_machine_setup.sh must derive sync.remote from the repo's real
# git remote, not from a guess about the platform.
#
# The bug this pins: the script used to comment sync.remote out whenever
# is_windows, on the premise "Windows has no SSH". SSH works fine on the example-client-host
# Windows box (it serves refs/dolt/data over an alias). Acting on the false
# premise led to a hand-written `git+https://github.com/<owner>/<repo>.git`
# replacement, which cannot reach a PRIVATE repo without a credential helper -
# so `bd init` died with "Repository not found" and the store could not be
# reconstituted. Two of five fleet repos sat in that state.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP="$SCRIPT_DIR/../scripts/beads_machine_setup.sh"

fails=0
pass() { echo "  ok   $1"; }
fail() { echo "  FAIL $1"; fails=$((fails + 1)); }

# Pull in only the pure translation helper; the rest of the script executes work.
# shellcheck disable=SC1090
eval "$(sed -n '/^_sync_remote_from_git_url/,/^}/p' "$SETUP")"

expect_url() {
    local input="$1" want="$2" got
    got="$(_sync_remote_from_git_url "$input" 2>/dev/null)" || got="<reject>"
    if [[ "$got" == "$want" ]]; then
        pass "$input -> $got"
    else
        fail "$input -> $got (want $want)"
    fi
}

echo "=== _sync_remote_from_git_url ==="

# The scp-like form is what `git remote -v` prints for an SSH host alias, and is
# the case the original code never handled: its single colon separates host from
# path and must become a slash, or the result parses as a URL but points nowhere.
expect_url "git@gh-ai-harness:Example User/ai-harness.git" \
           "git+ssh://git@gh-ai-harness/Example User/ai-harness.git"
expect_url "git@github.com:owner/repo.git" "git+ssh://git@github.com/owner/repo.git"
expect_url "ssh://git@host/o/r.git"        "git+ssh://git@host/o/r.git"
expect_url "https://github.com/o/r.git"    "git+https://github.com/o/r.git"
expect_url "http://host/o/r.git"           "git+http://host/o/r.git"

# Already in bd's form: pass through untouched rather than double-prefixing.
expect_url "git+ssh://git@host/o/r.git"    "git+ssh://git@host/o/r.git"

# Not a remote URL at all -- must be rejected, so the caller leaves config alone
# instead of writing a nonsense value.
expect_url "/local/path/repo"              "<reject>"
expect_url ""                              "<reject>"

echo "=== the retired platform assumption must not come back ==="

# Guard against reintroduction. Falsifiability: this greps for text that WAS
# present before the fix, so it fails if someone restores the OS-guess branch.
if grep -qE 'is_windows.*sync\.remote|SSH not available on Windows' "$SETUP"; then
    fail "beads_machine_setup.sh still gates sync.remote on is_windows"
else
    pass "sync.remote is not gated on the platform"
fi

# And the probe has to actually be called, or "verified reachable" is a lie.
if grep -q '_sync_remote_reachable' "$SETUP"; then
    pass "reachability probe present"
else
    fail "no reachability probe -- sync.remote would be set unverified"
fi


echo "=== ISSUE-REF: init success must be verified, not assumed ==="

# bd init exits 0 even when it leaves a half-built database (no repo_state.json).
# The setup script must check for that file rather than trust the exit status.
if grep -q 'if \[\[ ! -f "\$_repo_state" \]\]; then' "$SETUP"; then
    pass "post-init repo_state.json assertion present"
else
    fail "no repo_state.json check after bd init -- exit 0 would be trusted"
fi

# The custom types must be registered BEFORE import, or the first issue typed
# `research` aborts the whole import with "invalid issue type".
_types_line=$(grep -n 'types.custom' "$SETUP" | head -1 | cut -d: -f1)
_import_line=$(grep -n 'bd import' "$SETUP" | tail -1 | cut -d: -f1)
if [[ -n "$_types_line" && -n "$_import_line" && "$_types_line" -lt "$_import_line" ]]; then
    pass "types.custom is set before bd import (line $_types_line < $_import_line)"
else
    fail "types.custom must be set before bd import (got $_types_line vs $_import_line)"
fi

echo
if [[ "$fails" -eq 0 ]]; then
    echo "All sync.remote reconciliation checks passed."
else
    echo "$fails check(s) failed."
fi
exit "$fails"
