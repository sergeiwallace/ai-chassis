---
title: "R-6 Acceptance Controlled Live-Run Protocol"
category: procedure
tags: [r6, job-supervisor, acceptance, claude-code, live-run]
status: active
source: ai-harness
template_version: "procedure-1.0.0"
---

<!-- doc:region name="overview" kind="replaceable" -->

# R-6 Acceptance Controlled Live-Run Protocol

Use this protocol once on macOS before treating R-6 as the default path. It records the real
Claude Code turn/session observations that a headless pytest process cannot honestly drive. Keep
the resulting terminal transcript, job IDs, timestamps, and `status` JSON with the release
evidence; do not put secrets in the command, description, or captured output.

The automated complement is the job-supervisor test suite and
the job-supervisor pickup-hook test suite. Those tests prove detached ownership, bounded ledger
state, process identity, pickup behavior, and security boundaries. They do not prove that a live
Claude client emits `end_turn`, waits through its own idle-reap interval, compacts, or accepts a
human prompt.

## Preconditions

1. Install the current main-tree harness, then confirm the persistent supervisor is listening:

   ```bash
   ~/.claude/bin/job-supervisor status 00000000000000000000000000000000
   ```

   It should reach the daemon and report `job not found`, not a connection error. Record the
   harness commit and macOS/Claude Code versions.
2. Set `W` to the measured idle-reap window for this Claude Code build. If it is not known, record
   that fact and use a job duration of at least 30 minutes; do not call the survival claim passed
   until `W` has been observed.
3. Use a disposable worktree and a secret-free long job. In the launching Claude turn, submit one
   whose duration exceeds `W + 10 minutes`, retaining the printed `JOB_A`:

   ```bash
   JOB_A="$(~/.claude/bin/job-supervisor submit r6-live-survival -- \
     /bin/sh -c 'sleep 1800; printf r6-live-survival')"
   ~/.claude/bin/job-supervisor status "$JOB_A"
   ```

   Record that `JOB_A` is `running`, its `child_pid`, and its `child_start_time` before continuing.

## Live-run steps

### Step 1: Steer while the job is live

While `JOB_A` remains `running`, send a new ordinary user prompt in the same Claude session, such
as: “Report the state of `JOB_A`, then continue with the next small read-only task.” Record the
accepted prompt, the response/action, and a `status` result showing `JOB_A` remains `running`.
This is the only evidence for the Claude turn-taking part of **Steerability**.

### Step 2: End the launching turn and cross the idle-reap window

In the turn that launched `JOB_A`, finish normally so Claude emits `end_turn`; make no activity in
that session for more than `W`. Start a new turn afterwards and run:

```bash
~/.claude/bin/job-supervisor status "$JOB_A"
```

Record the launch time, end-turn time, idle interval, and status. The result must still be
`running` (or a correct terminal state if the deliberately chosen duration has elapsed). This is
the live evidence for both **end_turn survival** and **idle-reap-window survival**.

### Step 3: Prove session-exit and Claude-restart pickup

While `JOB_A` is still live, close the owning Claude session completely. Do not stop the
LaunchAgent or daemon. After the job has finished, start a fresh Claude Code session and let its
`SessionStart` hook run; if it does not surface the terminal record immediately, send one user
prompt so `UserPromptSubmit` runs. Capture the hook context plus:

```bash
~/.claude/bin/job-supervisor status "$JOB_A"
```

The ledger must contain a terminal state, result hash, and bounded file metadata. This one record
proves **owning-session exit**, **Claude restart**, and recovery pickup across the boundary.

### Step 4: Prove compact pickup separately

Submit a second secret-free long job `JOB_B`, confirm it is `running`, then invoke `/compact` in
the same Claude session while it remains live. After compaction, issue a new user prompt and run
`status "$JOB_B"`; retain the compact boundary and output. Let it finish and confirm that a later
`SessionStart` or `UserPromptSubmit` invocation surfaces its terminal record. This is the live
evidence for **compact recovery**; it must not be inferred from an ordinary new prompt.

### Step 5: Record the terminal outcome and clean up

For every live job, save the final `status` JSON before acknowledgement. Verify the state,
`files.result.sha256`, and each `files.*.bytes` value. Only then acknowledge it:

```bash
~/.claude/bin/job-supervisor ack "$JOB_A"
~/.claude/bin/job-supervisor ack "$JOB_B"
```

If a job remains live because this protocol was interrupted, cancel its exact job ID and retain the
resulting terminal record. Do not use a bare PID.

## Evidence matrix and release gate

| §13.5 scenario | Evidence source | Pass condition |
|---|---|---|
| `end_turn` | Live Step 2 | `JOB_A` remains live/terminates correctly after the launching turn ends. |
| idle-reap | Live Step 2 | Recorded quiet interval exceeds measured `W`; job remains live/terminates correctly. |
| steerability | Live Step 1; `test_daemon_submission_is_nonblocking_and_isolated_from_submitter` | New prompt is acted on while status is running; submit is non-blocking. |
| Claude restart | Live Step 3 | Fresh-session hook surfaces the terminal record. |
| compact | Live Step 4 | Post-compact prompt/hook surfaces the job correctly. |
| supervisor restart | `test_supervisor_crash_then_restart_reconciles_completed_orphan` | Crash then replacement daemon commits bounded `unknown` with result hash. |
| duplicate watcher | `test_duplicate_file_changed_events_are_read_only_and_idempotent` | Repeated `FileChanged` output is identical and ledger bytes are unchanged. |
| duplicate submit | `test_idempotency_key_returns_original_job_and_rejects_payload_change`; `test_idempotency_key_is_atomic_and_survives_supervisor_restart` | **PASS (Round 3):** `Ledger.create_idempotent()` and the CLI's `--idempotency-key` flag give identical submits an idempotency contract; a payload change against the same key is rejected. |
| retention | `test_prune_acknowledged_records_dry_run_expiry_race_and_failure` | **PASS (Round 3):** `Supervisor.prune_acknowledged()` bounds terminal-record retention (dry-run, expiry, and race/failure paths all covered). |
| log bound | `test_runaway_stdout_and_stderr_are_bounded` | Both logs are exactly `LOG_LIMIT_BYTES`; metadata and result hash match disk. |

Additional §13.5 evidence: `test_real_subprocess_records_success_failure_and_signal` covers exit
0, nonzero exit, and SIGTERM; `test_reconciliation_retries_failed_ntfy_delivery_after_restart`
covers ntfy outage; `test_claim_without_ack_remains_visible_to_a_fresh_pickup_consumer` covers a
consumer crash after claim; `test_cancel_refuses_a_reused_pid_with_a_different_start_time` covers
PID reuse; `test_ntfy_success_is_post_terminal_and_idempotent` covers duplicate terminal
notification; and the secret, symlink, mode, and traversal tests cover the remaining security
boundaries.

R-6 is not eligible to become default until every row has passing evidence. Duplicate submit
idempotency and ledger retention (above) are resolved as of Round 3; the remaining gap is the
`Claude restart` and `compact` rows (Live Steps 3-4), which genuinely need a real session
close/restart and a real `/compact` and have not yet been run (tracked as ISSUE-REF).

<!-- /doc:region name="overview" -->

<!-- doc:region name="steps" kind="replaceable" -->

## Synthetic protocol walkthrough

**Preconditions.** In this illustrative run, choose an idle window `W` of five minutes and launch
a disposable supervisor with a temporary ledger. Submit `JOB_A`, a synthetic long-running job whose
duration exceeds `W` by a comfortable margin. The supervisor is independent of the interactive
session process.

### Step 1 -- steer while live: PASS

The next ordinary user message in the owning session landed while `JOB_A` was still `running`
(status checked at that point confirmed `running`, `child_pid` unchanged). Evidence source:
this session's own transcript.

### Step 2 -- end_turn + idle-reap-window survival: PASS

Two illustrative idle gaps both exceed `W`:

| Gap | Duration | Notes |
| --- | --- | --- |
| First gap | 12 minutes | Follows `JOB_A`'s launch and exceeds `W`. |
| Second gap | 16 minutes | Spans `JOB_A`'s natural completion; the supervisor records the terminal state without session activity. |

The synthetic ledger records an unbroken `queued -> running -> succeeded` sequence, and the
supervisor identifier is unchanged at launch and final status. A later session verifies
`state: succeeded`, `exit_code: 0`, and a non-empty result digest. This meets the pass condition:
the job remains running, or reaches a correct terminal state after its chosen duration elapses.

**Step 2 verdict: PASS.** Steps 3 (session-exit + restart pickup) and 4 (compact pickup,
`JOB_B`) remain open -- both need a genuine session close/restart and a second submitted job,
not inferable from this run.

<!-- /doc:region name="steps" -->

<!-- doc:region name="rationale" kind="replaceable" -->

This protocol implements the controlled-live-run portion of the §13.5 acceptance bar in
the Claude Code idle-session background-task-reap research doc. It deliberately separates
observable Claude-client behavior from structural process and ledger tests.

<!-- /doc:region name="rationale" -->

<!-- doc:region name="revision_log" kind="append_only" -->

- 2026-08-17: Added F-08 controlled live-run protocol and its automated-evidence mapping.

<!-- /doc:region name="revision_log" -->
