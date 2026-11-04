#!/usr/bin/env bash
# SessionStart hook: surface a size-gate remediation created by the main-tree
# Git hook. TaskCreate is only available in a live CC session, so this hook
# deliberately asks that attended session to create the one-way CC mirror.
set -uo pipefail

# `read` builtin, not $(cat): one less spawn per invocation (rationale: enforce-workflow.sh)
IFS= read -r -d '' -t 2 _ || true  # drain the SessionStart hook payload; this hook does not need its content
STATE_FILE="${HOME}/.claude/state/install-harness-remediation-pending"
[[ -r "$STATE_FILE" ]] || exit 0

display_id=""
report_file=""
while IFS='=' read -r key value; do
    case "$key" in
        display_id) display_id="$value" ;;
        report_file) report_file="$value" ;;
    esac
done < "$STATE_FILE"

if [[ -n "$display_id" ]]; then
    subject="${display_id}: Remediate ai-harness context-size gate failure"
    issue="${display_id}"
else
    subject="File and remediate ai-harness context-size gate failure"
    issue="no Beads issue was created; inspect the handoff before filing one"
fi

cat <<EOF
<system-reminder>
ai-harness automatic install needs human-attended remediation. Beads: ${issue}. Gate report: ${report_file:-unavailable}.

Before remediation, create exactly one CC TaskCreate mirror with subject "${subject}", identify Beads as the durable system of record, then TaskUpdate it to in_progress. Mark the Beads issue in_progress too when one exists. Follow /install-harness's documented trim/offload/removal ladder; do not launch an unattended agent. After those updates succeed, remove ${STATE_FILE} so later sessions are not re-notified.
</system-reminder>
EOF
