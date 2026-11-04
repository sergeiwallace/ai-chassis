#!/usr/bin/env bash
# Run the public candidate's install workflow in a container with no sibling checkout.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 CANDIDATE_REPOSITORY" >&2
  exit 64
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required for the outside-user install proof" >&2
  exit 69
fi

candidate=$(cd "$1" && pwd -P)
if [[ ! -f "$candidate/pyproject.toml" ]]; then
  echo "ERROR: candidate has no pyproject.toml: $candidate" >&2
  exit 2
fi
scratch=$(mktemp -d "${TMPDIR:-/tmp}/aih-outside-user.XXXXXX")
base_image="aih-outside-user-base-$$"
image="aih-outside-user-$$"
cleanup() {
  docker image rm --force "$image" "$base_image" >/dev/null 2>&1 || true
  rm -rf "$scratch"
}
trap cleanup EXIT

# Two-stage build. Stage 1 (network allowed) stages two things that are
# generic Python packaging machinery, not specific to this candidate's
# content: (a) build tooling (setuptools/wheel), needed by PEP 517 build
# isolation for any source-tree install; (b) the candidate's own declared
# runtime dependencies, read directly from its pyproject.toml so this never
# drifts out of sync with the real dependency list. Stage 2 (network=none)
# then builds and installs the candidate itself with --no-deps
# --no-build-isolation against what stage 1 staged, so the candidate's own
# install step proves it fetches nothing beyond that -- no sibling checkout,
# no private network dependency -- while the resulting install is still
# fully functional (real runtime deps present) for the init/plan/apply
# smoke test below.
cp -R "$candidate" "$scratch/src"
cat >"$scratch/Dockerfile.base" <<DOCKERFILE
FROM python:3.11-slim
COPY src /src
RUN python -m pip install --no-cache-dir "setuptools>=68" "wheel>=0.43" \\
 && python -c "import tomllib; d = tomllib.load(open('/src/pyproject.toml', 'rb')); print(chr(10).join(d['project']['dependencies']))" > /tmp/runtime-deps.txt \\
 && python -m pip install --no-cache-dir -r /tmp/runtime-deps.txt
DOCKERFILE
docker build --tag "$base_image" -f "$scratch/Dockerfile.base" "$scratch"

cat >"$scratch/Dockerfile" <<DOCKERFILE
FROM $base_image
WORKDIR /outside-user
RUN python -m pip install --no-deps --no-build-isolation /src
RUN ai-harness --config /tmp/config init \\
 && printf 'never-matches\n' > /tmp/config/patterns \\
 && printf 'AGENTS.md\tnever-matches\n' > /tmp/config/allowlist \\
 && printf '[hygiene]\ngeneration_mode = "0700"\ncredential_patterns = "patterns"\ncredential_allowlist = "allowlist"\nretain_previous = 1\n' >> /tmp/config/ai-harness.toml \\
 && ai-harness --config /tmp/config --state-root /tmp/state plan --target AGENTS.md:/tmp/output/AGENTS.md \\
 && ai-harness --config /tmp/config --state-root /tmp/state apply --target AGENTS.md:/tmp/output/AGENTS.md
DOCKERFILE

docker build --network=none --tag "$image" "$scratch"
