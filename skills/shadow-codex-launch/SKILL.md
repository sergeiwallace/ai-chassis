---
name: shadow-codex-launch
description: Launch one cx Codex job from a live Claude Code turn and durably bind its dedicated CC task row and native background task to a shadow-watcher claim.
---

# shadow-codex-launch

Use this procedure only when a live Claude Code turn dispatches one `cx`
implementation, audit, or research job with `Bash(run_in_background: true)` and
that launch must be tracked by the shadow-watcher registry. The live turn, not a
script, must call `TaskCreate` and `TaskUpdate`.

## Preconditions

- Have a dedicated child-task subject/description for this one `cx` launch. Do
  not reuse a parent, epic, or another launch's child task.
- Confirm `CLAUDE_CODE_SESSION_ID` is the non-empty current process session ID
  for D-9 ownership, and `CLAUDE_CODE_TASK_LIST_ID` is the non-empty native task
  namespace. Resolve them independently; stop loudly if either is unavailable.
  The helper resolves these variables when flags are omitted; explicit
  `--session-id` and `--task-namespace` take precedence. Do not infer a team name
  or reuse a stale session ID or namespace.
- Use the actual `~/.claude/bin/cx` launch path. Do not use
  `job_supervisor.py submit`: it does not publish the `cx` process record.

## Required live-turn sequence

1. Call `TaskCreate` for the dedicated child row. Its description must contain
   the exact marker `shadow-codex-launch:dedicated-child`; the binding command
   reads the native row back and rejects any unmarked shared parent/epic row.
   Retain its returned `id` as `cc_task_id`.
2. Call `TaskUpdate` for that same ID with `status: in_progress` and the exact
   launch owner. Retain the confirmation.
3. Make the one real `cx` Bash call with `run_in_background: true` (no `&`,
   `nohup`, or `disown`). Set `AIH_TASK_ID` only for this command to the fresh
   child ID, so `cx_session_supervisor.py` publishes it in the running process
   record:

   <!-- bg-launch: begin label=SHADOW_CODEX_JOB owner=shadow-codex-launch -->
   ```bash
   SHADOW_CODEX_JOB=shadow-codex-launch
   AIH_TASK_ID="$cc_task_id" ~/.claude/bin/cx <role> --session "$launch_name" \
     -C "$worktree" "$prompt"
   ```
   <!-- bg-launch: end -->

   Capture the Bash result's `backgroundTaskId` exactly. A missing result or
   missing ID is a failed launch; do not guess an ID or use a private task file.

   Before step 4, use this fail-closed cleanup sequence:

   - If `TaskCreate fails`, stop; it returned no usable row ID and no command or
     watcher relation has been launched.
   - If `TaskUpdate fails`, or if the `Bash call or its result capture fails`,
     run the command below with the retained `cc_task_id`, then best-effort
     update that dedicated row to a completed failed-launch status. The helper
     locates only the exact process correlation and removes any partial claim
     and unleased shadow binding; it refuses a healthy lease.

     ```bash
     python3 scripts/shadow_codex_launch.py cleanup-partial \
       --cc-task-id "$cc_task_id" \
       --session-id "$CLAUDE_CODE_SESSION_ID" \
       --task-namespace "$CLAUDE_CODE_TASK_LIST_ID"
     ```

   Do not continue to claim after any of these failures. The cleanup command is
   safe when the `cx` process record was never published: it performs no write.
4. Claim the watcher slot in the foreground with the captured live values:

   ```bash
   python3 scripts/shadow_codex_launch.py claim \
     --cc-task-id "$cc_task_id" \
     --cx-background-task-id "$backgroundTaskId" \
     --session-id "$CLAUDE_CODE_SESSION_ID" \
     --task-namespace "$CLAUDE_CODE_TASK_LIST_ID"
   ```

   It waits briefly for exactly one **running** `cx` record whose `taskId`
   equals `cc_task_id`; zero or multiple records fail closed. It then registers
   the immutable D-9 shadow binding when absent, claims the watcher slot, and
   prints the exact `job_id` and `claim_id`. Retain both values. A claim failure
   after the claim exists calls `cleanup_live_turn_claim()` before returning.
5. Make a second, claimed watcher-runner Bash call with
   `run_in_background: true` (again no `&`, `nohup`, or `disown`):

   ```bash
   python -m ai_harness.shadow_watcher_runner \
     --job-id "$job_id" --claim-id "$claim_id"
   ```

   Capture this Bash result's `backgroundTaskId` as
   `watcher_backgroundTaskId`. This is the ID that must be bound; the earlier
   `cx` Bash ID is evidence of the job launch only and must never be bound as a
   watcher task.
6. Bind the dedicated row and the watcher Bash result in the foreground:

   ```bash
   python3 scripts/shadow_codex_launch.py bind \
     --cc-task-id "$cc_task_id" \
     --watcher-background-task-id "$watcher_backgroundTaskId" \
     --session-id "$CLAUDE_CODE_SESSION_ID" \
     --task-namespace "$CLAUDE_CODE_TASK_LIST_ID" \
     --job-id "$job_id" --claim-id "$claim_id"
   ```

   Only this final operation calls `bind_live_turn_claim()`. The sequence is
   complete only after it reports `bound`.
7. On any failure after the claim was returned, call the cleanup command before
   updating the dedicated CC task row to show the failed launch:

   ```bash
   python3 scripts/shadow_codex_launch.py cleanup \
     --session-id "$CLAUDE_CODE_SESSION_ID" \
     --task-namespace "$CLAUDE_CODE_TASK_LIST_ID" \
     --job-id "$job_id" --claim-id "$claim_id"
   ```

   The `bind` command performs this cleanup itself when binding fails. Never
   leave an unbound claim or run the watcher from a failed claim.

The correlation deliberately uses `AIH_TASK_ID`, not the delegation receipt.
`cx_session_supervisor.py` inherits that exact per-launch value and persists it
as validated `taskId` beside host/boot/PID/start-token identity. Delegation
receipts are issued only after `cx` exits and their v2 schema contains neither
that identity nor this task ID, so they cannot safely identify a live process.

## Watcher boundary

This procedure performs the required second watcher-launch Bash call. The
SessionStart hook cannot create a CC-tracked Bash task, so the live turn binds
the watcher runner's own native ID before that watcher can activate its lease.
