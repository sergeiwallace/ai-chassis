#!/usr/bin/env bash
# Refresh (or roll back) the fleet-testkit Git revision transactionally.
set -euo pipefail

usage() {
  echo "usage: $0 <40-hex-commit>" >&2
  exit 2
}

[[ $# -eq 1 && $1 =~ ^[0-9a-f]{40}$ ]] || usage
commit=$1
script_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
fleet_root=${FLEET_ROOT:-$(cd -- "$script_root/../../../.." && pwd -P)}
# Add every repository in your fleet that depends on fleet-testkit before running this script.
# This exported template intentionally has no default fleet and is non-functional until configured.
repos=()
backup=$(mktemp -d)
changed=()
status=0

restore() {
  local path
  for path in ${changed[@]+"${changed[@]}"}; do
    if [[ -e "$backup/${path#/}" ]]; then
      cp -- "$backup/${path#/}" "$path"
    else
      rm -f -- "$path"
    fi
  done
}
trap 'status=$?; if (( status != 0 )); then restore; fi; rm -rf -- "$backup"; exit "$status"' EXIT

for repo in ${repos[@]+"${repos[@]}"}; do
  directory=$fleet_root/$repo
  manifest=$directory/pyproject.toml
  [[ -f $manifest ]] || { echo "missing manifest: $manifest" >&2; exit 1; }
  for path in "$manifest" "$directory/uv.lock"; do
    [[ -e $path ]] || continue
    mkdir -p -- "$backup/$(dirname -- "${path#/}")"
    cp -- "$path" "$backup/${path#/}"
    changed+=("$path")
  done
  python3 - "$manifest" "$commit" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
revision = sys.argv[2]
text = path.read_text(encoding="utf-8")
pattern = r'(fleet-testkit\s*=\s*\{[^}]*\brev\s*=\s*")[0-9a-f]{40}("[^}]*\})'
updated, count = re.subn(pattern, rf"\g<1>{revision}\2", text)
if count != 1:
    raise SystemExit(f"expected exactly one fleet-testkit pin in {path}, got {count}")
path.write_text(updated, encoding="utf-8")
PY
  (cd -- "$directory" && uv lock --upgrade-package fleet-testkit && uv run pytest)
done

echo "fleet-testkit pins refreshed to $commit in ${#repos[@]} repositories"
