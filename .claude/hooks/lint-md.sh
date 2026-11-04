#!/bin/bash
# `read` builtin, not $(cat): one less spawn per invocation (rationale: enforce-workflow.sh)
IFS= read -r -d '' -t 2 INPUT || true
FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

if [[ "$FILE" == *.md ]]; then
  if command -v npx &>/dev/null; then
    npx markdownlint-cli2 "$FILE" 2>&1
  fi
fi

exit 0
