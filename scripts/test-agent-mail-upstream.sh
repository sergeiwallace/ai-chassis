#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lock_file="$repo_root/services/agent_mail/upstream.lock"
# `while read` rather than `readarray`, and `uv run` rather than a bare `python3`:
# readarray is bash 4.0 while both fleet Macs run /bin/bash 3.2.57, where it exits 127
# and the `set -e` above aborts the script on this line; and a bare `python3` is 3.9.6
# on example-client-host-mac, which has no `tomllib` at all.  `uv` is already a hard dependency further
# down (`uv sync`, `uv run --frozen pytest`), so resolving the interpreter through it
# adds nothing new and fails closed if the lock is stale.
lock_values=()
while IFS= read -r lock_value; do
  lock_values+=("$lock_value")
done < <(
  uv run --project "$repo_root" --frozen python - "$lock_file" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    lock = tomllib.load(handle)
for key in ("repository", "commit", "license_sha256", "pyproject_sha256", "uv_lock_sha256"):
    print(lock[key])
PY
)
repository="${lock_values[0]}"
commit="${lock_values[1]}"

temporary_checkout=""
checkout="${AGENT_MAIL_UPSTREAM_CHECKOUT:-}"
if [[ -z "$checkout" ]]; then
  temporary_checkout="$(mktemp -d "${TMPDIR:-/tmp}/agent-mail-upstream.XXXXXX")"
  checkout="$temporary_checkout"
  git -C "$checkout" init -q
  git -C "$checkout" remote add origin "$repository"
  git -C "$checkout" fetch --depth 1 origin "$commit"
  git -C "$checkout" checkout -q FETCH_HEAD
fi
cleanup() {
  if [[ -n "$temporary_checkout" ]]; then
    rm -rf -- "$temporary_checkout"
  fi
}
trap cleanup EXIT

[[ "$(git -C "$checkout" rev-parse HEAD)" == "$commit" ]]
[[ "$(sha256sum "$checkout/LICENSE" | cut -d' ' -f1)" == "${lock_values[2]}" ]]
[[ "$(sha256sum "$checkout/pyproject.toml" | cut -d' ' -f1)" == "${lock_values[3]}" ]]
[[ "$(sha256sum "$checkout/uv.lock" | cut -d' ' -f1)" == "${lock_values[4]}" ]]

uv sync --project "$checkout" --frozen
(
  cd "$checkout"
  test_files=(
    tests/test_http_auth.py
    tests/test_http_negative_jwt.py
    tests/test_security_path_traversal.py
    tests/test_security_input_sanitization.py
    tests/test_xss_corpus.py
    tests/test_attachment_policy.py
    tests/test_concurrent_writes.py
    tests/test_e2e_disaster_recovery.py
  )
  for test_file in ${test_files[@]+"${test_files[@]}"}; do
    pytest_args=(--confcutdir . -q --no-cov "$test_file")
    if [[ "$test_file" == "tests/test_e2e_disaster_recovery.py" ]]; then
      pytest_args+=(
        --deselect=tests/test_e2e_disaster_recovery.py::TestDisasterRecoveryE2E::test_complete_disaster_recovery_scenario
        --deselect=tests/test_e2e_disaster_recovery.py::TestMultipleArchives::test_restore_specific_archive
      )
    fi
    PYTHONPATH="$repo_root/tests/agent_mail/upstream" uv run --frozen pytest \
      -p pytest_cleanup ${pytest_args[@]+"${pytest_args[@]}"}
  done
  PYTHONPATH="$checkout/src" uv run --frozen python \
    "$repo_root/tests/agent_mail/upstream/credential_restore_gate.py"
)
