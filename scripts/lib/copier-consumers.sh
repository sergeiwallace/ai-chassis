#!/usr/bin/env bash
# Shared copier-consumer discovery.  Safe to source: definitions only.

# copier_consumer_repos <projects-dir> <ai-harness-repo-name>
copier_consumer_repos() {
  local projects_dir="${1:-}" ai_harness_repo_name="${2:-}"
  local d repo

  if [[ -z "$projects_dir" || -z "$ai_harness_repo_name" ]]; then
    echo "copier_consumer_repos: projects directory and ai-harness repo name are required" >&2
    return 2
  fi

  for d in "$projects_dir"/*/; do
    [[ -d "$d" ]] || continue
    repo="$(basename "$d")"
    [[ "$repo" == "$ai_harness_repo_name" ]] && continue
    # aido keeps its own bundled templates.
    [[ "$repo" == "aido" ]] && continue
    [[ -f "$d/.copier-answers.yml" ]] || continue
    grep -q 'project-template' "$d/.copier-answers.yml" && echo "$repo"
  done
}
