---
title: Claude Code TUI Footer Shows a Stale Merged-PR Indicator -- Root Cause and Fix Options
category: research
tags: [research, claude-code, tui, github, worktrees]
status: draft
source: "gpt-5.6-sol-2026-08-20"
date: 2026-08-20
template_version: "research-1.2.0"
delegation_provenance:
  delegated_to: codex/research
  tier: research
  model: gpt-5.6-sol
  effort: high
  persona: research
  worktree: /home/example-user/projects/ai-harness/.worktrees/ISSUE-REF
  session: ISSUE-REF
---
<!-- Canonical Jinja source: STUB.md.jinja. This direct-copy stub is retained for consumers that have not migrated to rendering it. -->

# Claude Code TUI Footer Shows a Stale Merged-PR Indicator

**Status:** draft

**Created:** 2026-08-13

**Updated:** 2026-08-20

<!-- doc:region name="context" kind="immutable" -->

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. Current Contract and Release History](#1-current-contract-and-release-history)
  - [Current documented behavior](#current-documented-behavior)
  - [History matters](#history-matters)
- [2. Prior Reports and Match Quality](#2-prior-reports-and-match-quality)
  - [Strong matches](#strong-matches)
  - [Adjacent but distinct reports](#adjacent-but-distinct-reports)
  - [Fresh status and issue survey](#fresh-status-and-issue-survey)
- [3. Mechanism Assessment](#3-mechanism-assessment)
  - [Evidence-constrained causal model](#evidence-constrained-causal-model)
  - [Hypothesis evaluation](#hypothesis-evaluation)
- [4. Workarounds and Disablement](#4-workarounds-and-disablement)
- [5. Worktree and Local-Tooling Scope](#5-worktree-and-local-tooling-scope)
- [6. Impact and Second-Order Effects](#6-impact-and-second-order-effects)
- [7. Agent View Multi-PR Associations: Distinct Architecture](#7-agent-view-multi-pr-associations-distinct-architecture)
  - [Association mechanism](#association-mechanism)
  - [Why sessions show many PRs or none](#why-sessions-show-many-prs-or-none)
  - [Clearability and configurability](#clearability-and-configurability)
- [Recommendation](#recommendation)
- [Gaps, blindspots & emergent findings](#gaps-blindspots--emergent-findings)
  - [Missing incident capture](#missing-incident-capture)
  - [Distinct cache classes](#distinct-cache-classes)
  - [Adjacent CLI comparison](#adjacent-cli-comparison)
  - [Surface-name ambiguity](#surface-name-ambiguity)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Ambiguous Items from Auto-Remediation (Post-Run Review)](#ambiguous-items-from-auto-remediation-post-run-review)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

## Context

[Original framing — set once, frozen thereafter.]

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

Primary focus is Claude Code behavior and documentation available on 2026-08-20, weighted 2026
first, then 2025 and 2024. The official changelog had advanced to 2.1.238.
[VERIFIABLE][^3] The research worktree's read-only `claude --version` measurement returned 2.1.236;
the user's latest captured reproduction remains the 2026-08-15 observation on 2.1.233. These are
direct local observations, not facts an external source can establish. [NO SOURCE]

**Refine update (2026-08-20):** this pass re-verified the prior Agent-view investigation against
current official documentation and issues, surveyed reports added since the original 2026-08-13
version, corrected the now-provider-dependent footer polling contract, and preserved the original
0.88-confidence footer ownership finding. The working-tree document already contained a
2026-08-15 Agent-view refine when this pass began; that differed from the issue brief's snapshot,
which described the Agent-view material as still missing. [NO SOURCE]

The footer feature arrived in the 2.1.20--2.1.27 release window and its intended merged-PR behavior
changed at least once. That history is load-bearing: a merged badge was once explicitly intentional,
whereas the current documentation requires removal. [VERIFIABLE][^1][^3]

## Executive Summary

1. **The currently reproducing footer is contrary to the product contract.** Claude Code documents
   the badge as representing an open PR for the current branch, says it disappears after merge or
   close, polls on a provider-dependent schedule (about 90 seconds for active Anthropic-API sessions
   with feature flags; 60 seconds for other documented configurations), and refreshes immediately
   after a successful `git push` or PR-changing `gh pr` command. The status-line JSON documentation
   repeats the open-current-branch and removal contract. [VERIFIABLE][^1][^2]
2. **This remains a live Claude Code bug family, but the exact cross-worktree variant was not
   found.** CLI issues #24834 and #25512 report stale closed/merged PR badges, and newly added issue
   #85715 reports the footer staying on PR A for about an hour after the session checked out PR B and
   exercised the documented refresh triggers. No searched issue exactly matched a never-PR session
   branch showing a PR merged from another worktree. [VERIFIABLE][^4][^5][^12] Exact trigger match:
   no source located. [NO SOURCE]
3. **The defensible root-cause class is failed invalidation inside Claude Code's own PR-state
   producer, not GitHub state and not the custom status-line script.** A 2026 CLI report independently
   found a per-process PR poller that can permanently disable itself after one slow fetch or repeated
   failures. That mechanism proves session-local poller failure exists, but its reported symptom was a
   missing badge, so it does not prove the exact stale-retention path here. [VERIFIABLE][^6] Given the
   current branch had no PR, `gh` returned the correct remote state, and the native badge remained,
   either a session-linked PR association or its rendered state failed to clear. [INFERENCE]
4. **The Agent view's multi-PR count is architecturally distinct and, by itself, documented
   behavior.** Agent view accumulates PRs linked by specific session actions, deliberately counts
   multiple links, and retains merged and closed PRs with purple and grey status. The view is backed
   by supervisor-managed per-session state, not the footer's current-branch/open-PR contract.
   [VERIFIABLE][^11] The evidence therefore does not support a shared poller root cause. [INFERENCE]
5. **Recommendation: file both now, but separately.** File the still-live footer defect through
   `/feedback` as a bug, not a fleet `/bug-fix`. File the Agent-view symptom as a product-control
   feature request (open-only/latest display plus per-PR unlink), unless a captured link falls outside
   the documented action rules; that would warrant a separate Agent-view bug. No supported control
   selectively clears or hides the native footer badge or one Agent-view PR association.
   [VERIFIABLE][^9][^10][^11][^13]

## 1. Current Contract and Release History

### Current documented behavior

Anthropic's current interactive-mode documentation is unambiguous about the state contract: the
badge is for an open pull request on the current branch and disappears after merge/close. Its refresh
schedule is now provider-dependent: about 90 seconds while active on the Anthropic API with feature-
flag fetching on, and 60 seconds on the listed third-party providers or with feature-flag fetching
off. A successful `git push` or a `gh pr` command that changes PR state triggers an immediate refresh;
after an hour without user input polling pauses until the next prompt. It requires an installed,
authenticated `gh` CLI. [VERIFIABLE][^1]

The custom status-line interface exposes the same producer state as `pr.number`, `pr.url`, and
optionally `pr.review_state`. Anthropic says those fields mirror the built-in bottom-bar badge and are
absent after the PR merges/closes. It also exposes `cwd`, `workspace.current_dir`, and
`workspace.git_worktree`, enabling a direct check of which worktree Claude Code thinks is active.
[VERIFIABLE][^2]

The settings reference documents `prUrlTemplate`, which changes the badge destination, but the
searched official settings and footer documentation expose no setting that hides only the native PR
badge. `FORCE_HYPERLINK=0` changes it from a hyperlink to plain text; it does not hide it.
[VERIFIABLE][^1][^9] A supported CLI-only visibility toggle was not located. [NO SOURCE]

### History matters

The changelog establishes a non-linear contract history: 2.1.20 added a current-branch PR-state
footer; 2.1.23 changed merged PRs to a purple footer state; 2.1.27 added automatic session-to-PR
linking after `gh pr create`; and 2.1.145 fixed immediate badge updates after PR-state-changing
commands. [VERIFIABLE][^3]

This explains why older reports and cached documentation can disagree with today's docs. A purple
merged badge was once product behavior, not necessarily a cache error. The current official pages no
longer list a merged color and instead require the badge to disappear. [VERIFIABLE][^1][^3]

No changelog entry through 2.1.238 explicitly claims to fix stale merged-PR or wrong-branch footer
badges. Absence of a release-note entry is not proof that behavior did not change. [NO SOURCE]
Therefore, retesting on 2.1.238 or later is useful, but is not evidence that the issue self-resolves.
[INFERENCE]

## 2. Prior Reports and Match Quality

### Strong matches

- Issue #24834, filed against CLI 2.1.38, reports the same native footer text for a closed PR and says
  it persisted across process restarts. The reporter observed that `gh pr view` returned the most
  recent closed PR for that branch, while `gh pr list` with its open-only default returned none.
  [VERIFIABLE][^4]
- Issue #25512 is a CLI duplicate: a branch formerly used for a PR continued to display the old
  merged/closed PR number. [VERIFIABLE][^5]

These establish that stale merged/closed native CLI badges are previously reported. They do **not**
fully explain this incident because `wt-ISSUE-REF` reportedly never had a PR. Reusing the historical
`gh pr view` explanation would require evidence that Claude Code queried the deleted PR branch or
bound the explicit `gh pr merge` target to the session; that evidence was not captured. [INFERENCE]

### Adjacent but distinct reports

- Issue #80209, reported against CLI 2.1.217, reverse-engineered an in-process PR poller that disables
  itself after a fetch takes more than four seconds or after repeated failures. The report says a new
  process restores polling and that the normal 60-second/immediate triggers do not revive a disabled
  poller. Its demonstrated symptom is a badge that disappears and never returns, not an existing badge
  that remains, so it supports the *failure mode* but is not a root-cause match. [VERIFIABLE][^6]
- Desktop issues #48694/#63767 report a stale merged/closed PR on `main` caused by a `state=all` query
  selecting the most recent PR. They concern the Desktop PR bar and its REST-query path, not the CLI
  footer, so importing that root cause into this incident would be unsound. [VERIFIABLE][^7]

The exact never-PR-current-branch plus other-worktree-merge/delete sequence was not found in the
searched open and closed issue corpus. [NO SOURCE]

### Fresh status and issue survey

As of 2026-08-20, #24834 is closed with no linked development shown on the issue, #25512 is closed
as a duplicate, and #80209 and #85715 remain open. [VERIFIABLE][^4][^5][^6][^12] Those tracker states
do not establish that the stale-retention path was fixed: the current changelog is 2.1.238 and no
entry through that release explicitly names a stale merged-PR or wrong-branch footer fix. [NO SOURCE]
The user's 2.1.233 reproduction is direct disconfirming evidence against treating closure of #24834
as proof of resolution. [INFERENCE]

One additional strong footer report was missed by Run 1: open issue #85715, filed against 2.1.227,
reports the built-in badge remaining on PR A roughly an hour after `gh pr checkout` moved the same
session to open PR B; repeated `gh pr` commands, `git push`, and waits beyond 60 seconds did not move
it, while `git`/`gh` and a custom status line all identified PR B correctly. [VERIFIABLE][^12] This
is closer than #24834 to the present wrong-session/wrong-worktree association because the stale value
survived a branch and PR transition, although it still does not reproduce the deleted-other-worktree
sequence. [INFERENCE]

The fresh Agent-view search found documentation/feature reports, not an exact bug report for “some
sessions show many historical PRs while others show none.” Issue #71165 explicitly treats `N PRs` as
the current cumulative display and requests a configurable latest-PR alternative; it remains closed
as a duplicate. [VERIFIABLE][^13] Issue #75887 documented the v2.1.205 expansion from PRs a session
opened to existing PRs it edited, merged, commented on, or pushed; it is now closed and current
official docs contain the expanded mechanism. [VERIFIABLE][^11][^14] Exact terminal Agent-view
count-staleness bug through 2026-08-20: no source located. [NO SOURCE]

An issue opened on 2026-08-13 reports missing PR-state badges in the Claude Desktop app's session
sidebar while `claude agents` still showed them. [VERIFIABLE][^15] It is useful disconfirming evidence
against treating every session-list PR symptom as one subsystem, but it does not describe the
terminal Agent-view count or the single-session footer. [INFERENCE]

A newer 2026-08-15 report, #86838, says a Claude Desktop update reset merged sessions' sidebar badges
to an open state while GitHub still reported the PRs merged. [VERIFIABLE][^16] It is another Desktop-
sidebar state-rendering report, not a terminal `claude agents` many-versus-none count report, so it
does not supply a shared root cause for either terminal surface. [INFERENCE]

## 3. Mechanism Assessment

### Evidence-constrained causal model

```text
GitHub state       gh command output       Claude Code PR producer       TUI footer
MERGED / no PR  -> correct current state -> stale association/poller  -> still "PR #1"
       ruled out by incident checks          unresolved fault boundary     symptom
```

The remote PR and current-branch association were independently checked with `gh`; therefore GitHub
itself was not still reporting an open PR for `wt-ISSUE-REF`. [INFERENCE] The native footer and documented
`pr.*` mirror are generated by Claude Code, so the unresolved fault boundary is between Claude Code's
workspace/PR lookup, its session-local PR cache/poller, and the final renderer. [INFERENCE]

### Hypothesis evaluation

| Hypothesis | Assessment | Evidence and reasoning |
|---|---|---|
| (a) Branch-to-PR association cached only at session start/switch | **Possible but incomplete** | The feature is documented as current-branch state and should refresh periodically, so a start-only cache contradicts the contract. The branch never had a PR, which also argues against a simple cache keyed to that branch. Session-to-PR linking exists historically, making a separate session association plausible. [VERIFIABLE][^1][^3] |
| (b) Normal polling interval had not elapsed | **Low plausibility for the long-lived reports; original incident timing incompletely captured** | The current documented active interval is about 90 seconds on the Anthropic API with feature flags and 60 seconds on the listed alternatives. Immediate refresh applies to successful `git push` and PR-changing `gh pr` commands, not every read-only `gh pr` command. The original incident included the merge command but lacks exact elapsed time; #85715 persisted for roughly an hour despite `git push`. [VERIFIABLE][^1][^12] [INFERENCE] |
| (c) Claude Code remained associated with the deleted PR worktree/branch | **Moderate plausibility** | The merge/delete happened from the still-running session, and the stale PR belonged to another worktree. No captured `workspace.*` or `pr.*` payload shows what Claude Code believed, so the stale-worktree part is unverified. [INFERENCE] |
| (d1) PR poller disabled or failed and retained prior state | **Moderate-to-high root-cause class; exact path unproved** | A per-process poller kill switch is independently reported. The issue demonstrates stopped refresh, but not retained badge state. [VERIFIABLE][^6] [INFERENCE] |
| (d2) Historical unfiltered `gh pr view` behavior | **Known mechanism, weak fit here** | It explains stale PRs on a branch that formerly had one, as in #24834, but not a branch that never did unless Claude Code used a stale branch/session target. [VERIFIABLE][^4] [INFERENCE] |
| (d3) Pure one-frame renderer glitch | **Low plausibility** | The badge survived several completed turns. A stale producer value or repeated stale render is more likely than one missed clear. [INFERENCE] |

The most precise conclusion the evidence permits is: **Claude Code failed to invalidate a
session-local PR association after a cross-worktree merge; whether the producer was stuck, keyed to
the wrong worktree, or the TUI retained an already-cleared producer value is unresolved.** [INFERENCE]

## 4. Workarounds and Disablement

1. **First refresh attempt:** keep the terminal focused and active for at least 90 seconds on the
   Anthropic API (or 60 seconds on the other documented configurations). A successful `git push` or
   PR-changing `gh pr` command is the documented immediate trigger; `gh pr status` remains a useful
   independent control but is no longer documented as a refresh trigger. This is not a fix when the
   poller itself is disabled, and #85715 persisted despite `git push` and waits of roughly an hour.
   [VERIFIABLE][^1][^6][^8][^12]
2. **Process restart is a diagnostic, not a reliable clear:** exit and relaunch Claude Code in the
   unchanged worktree. A new process restores a *missing* badge in #80209, but #24834 reports the
   stale closed badge persisting across restarts. [VERIFIABLE][^4][^6] A restart may distinguish
   process-only state from branch lookup, but the evidence does not justify promising that it will
   clear the present stale badge. [INFERENCE]
3. **Version retest:** the research shell is on 2.1.236, the user's latest captured reproduction is
   on 2.1.233, and the current changelog reaches 2.1.238. [VERIFIABLE][^3] No matching release note was
   found, so updating is a worthwhile retest condition, not an evidence-backed clear action.
   [INFERENCE]
4. **Disablement:** no documented setting selectively disables the CLI PR badge. Removing `gh` from
   the process `PATH` or de-authenticating it would suppress all PR detection but would also break
   useful GitHub integration; it is an unsupported, disproportionate workaround. [VERIFIABLE][^1]
   `FORCE_HYPERLINK=0` only removes link behavior. [VERIFIABLE][^1]

**Answer for “can I clear it right now?”: not with a supported PR-specific command.** `/clear`
removes only configurable regex footer links, not the native PR badge; `/compact`, terminal resize,
and renderer switching were not found as native-PR refresh controls. The documented settings surface
offers `prUrlTemplate` for changing the destination and `FORCE_HYPERLINK=0` for removing hyperlink
rendering, neither of which hides or invalidates the badge. [VERIFIABLE][^1][^9] A full process
restart is the only low-cost diagnostic left, with the limitation above. [INFERENCE]

## 5. Worktree and Local-Tooling Scope

The stale-badge class is **not worktree-specific**: #24834 and #25512 reproduce it in ordinary CLI
branch workflows. [VERIFIABLE][^4][^5] Worktrees can increase exposure because long-running sessions
can act on PRs and branches outside their own checkout, and the other checkout can then disappear,
but no source proves that worktree deletion is required for this variant. [INFERENCE]

Read-only inspection of `ai-cli-utils` found that the `ai c` launcher changes into the dedicated
session worktree and invokes `claude` under `direnv`; it does not parse PRs or render Claude Code's
native footer. It can preserve a long-lived process and worktree, which shapes reproduction, but it
cannot directly supply the stale `PR #1` text. [INFERENCE]

Consequently, there is no justified fleet `/bug-fix` in the custom status-line script or launcher.
At most, local tooling could implement a heavy-handed process restart after merges, but it does not
intercept arbitrary `gh` commands inside Claude Code and such behavior would mask an upstream defect.
[INFERENCE]

## 6. Impact and Second-Order Effects

The directly evidenced impact is misleading UI: users can click or reason from an irrelevant PR and
may mistake merged work for the current branch's review state. [INFERENCE]

The same producer state is exposed to custom status-line commands as `pr.*`, so scripts that consume
those fields can repeat the false association. [VERIFIABLE][^2] No evidence was found that the stale
footer value changes Git commands, checkout selection, merge targets, or model tool behavior. Those
higher-impact effects should not be asserted from a cosmetic symptom alone. [NO SOURCE]

## 7. Agent View Multi-PR Associations: Distinct Architecture

### Association mechanism

The terminal surface in question is **Agent view**, opened with `claude agents`, not the `/agents`
subagent library/running panel and not the single-session prompt footer. Agent view is a machine-wide
list of background sessions managed by a per-user supervisor; each background session is its own
Claude Code process, and the state shown in the view is maintained per session under
`~/.claude/jobs/<id>/state.json`. [VERIFIABLE][^11]

Its PR association is event-driven and cumulative:

- A PR is linked when the session opens it. [VERIFIABLE][^11]
- For an existing PR, Claude Code links the PR named in captured `gh` output when the session edits,
  comments on, closes, or marks it ready. `gh pr merge` is a documented exception that commonly
  fails to create a link because its result is written only to an interactive terminal.
  [VERIFIABLE][^11]
- `gh pr checkout` and pushes trigger a branch lookup with `gh pr view`; after a push, Claude Code can
  retry discovery after as many as five later `git`, `gh`, `glab`, or `curl` commands in that
  directory. [VERIFIABLE][^11]
- Multiple associations are intentional: the row changes from one PR number to `N PRs`, the peek
  panel lists them all, and merged and closed associations remain representable as purple and grey.
  [VERIFIABLE][^11]

The docs establish a supervisor/session-state path for Agent view and a current-branch poller contract
for the footer, but do not publish the exact internal field or refresh algorithm used for the Agent-
view PR collection. [NO SOURCE] The two paths can both invoke `gh`, yet shared dependency is not shared
state: Agent view answers “which PRs did this session become linked to?” while the footer answers
“what open PR belongs to the current branch now?” [INFERENCE]

Claude Code's implementation is closed-source, so public evidence cannot establish whether the two
features reuse any lower-level helper, how Agent view refreshes the state of an already-linked PR, or
whether its PR collection has an undocumented expiry or garbage-collection policy. [NO SOURCE] The
architectural conclusion here is intentionally limited to the documented boundary: the surfaces have
different contracts and documented persistence locations. Claiming a shared internal poller, a
separate internal cache object, or a never-cleared collection would go beyond the available evidence.
[NO SOURCE]

### Why sessions show many PRs or none

The observed asymmetry follows from those trigger rules. A long-lived session that opens or acts on
several PRs should accumulate several links even after they merge or close, while another session can
show none when its commands never expose a PR through a documented detection path. The explicit
`gh pr merge` output exception makes “the session merged a PR but shows none” possible by design.
[VERIFIABLE][^11]

There is also evidence of a historical missing-link failure mode: release 2.1.218 fixed PR events
occasionally being lost when a session exited immediately after creating or linking a PR.
[VERIFIABLE][^3] That narrow race can explain some absent links, but it does not explain why a living
session retains a high cumulative count. [INFERENCE]

Therefore, **the count's independence from current open/closed state is not evidence of the footer's
stale-current-branch bug**; it is the documented Agent-view data model. [INFERENCE] A genuine Agent-
view bug would need a narrower invariant, such as a PR that appears despite no linking trigger, a
documented trigger that never links after its retry window, or a PR whose displayed color disagrees
with its current state. No exact public report matching the observed “many versus none” count pattern
was located. [NO SOURCE]

The docs explicitly render merged and closed associations and provide a multiple-PR count, so older
links can remain visible after they cease to be open. [VERIFIABLE][^11] They do not specify how long a
link lives or promise that it is never cleared. “Older PRs accumulate forever” is therefore not an
established mechanism; only cumulative display for the lifetime observed is supported. [NO SOURCE]

### Clearability and configurability

- Press `Space` on the row to see every linked PR and its status; this explains a count but does not
  remove an association. [VERIFIABLE][^11]
- There is no documented per-PR unlink, open-only filter, latest-only mode, association reset, or
  force-refresh command. Issue #71165 requested a configurable latest-PR alternative precisely
  because `N PRs` hides the most relevant link; it is closed as a duplicate, but current docs and
  settings still expose no such control. [VERIFIABLE][^13] Supported per-PR clear control: no source
  located. [NO SOURCE]
- `Ctrl+X` twice in Agent view, or `claude rm <id>`, removes the **entire session row**. That is not a
  PR-only clear and can also remove a Claude-created worktree subject to the documented safety checks,
  so it is appropriate only when the session itself is disposable. [VERIFIABLE][^11]
- `disableAgentView: true` or `CLAUDE_CODE_DISABLE_AGENT_VIEW=1` turns off Agent view and background
  agents as a whole. It does not preserve the view while hiding PR counts. [VERIFIABLE][^9][^11]

For the user's immediate case, there is no supported way to clear individual stale-looking Agent-view
associations while retaining the session. If the session is finished and its work is safely retained,
removing that entire session clears its row; otherwise the non-destructive action is to inspect the
count with `Space` and leave it. [INFERENCE]

## Recommendation

Use **both an upstream footer bug report and a separate Agent-view feature request**; do not fold the
two symptoms into one root-cause claim.

1. **Footer: `/report-feedback` upstream, not local `/bug-fix`.** Confidence: **high (0.88)** that the
   still-live `PR #1` symptom is in Claude Code's native PR-state/footer subsystem; confidence:
   **moderate (0.62)** that a stopped or mis-keyed per-session poller/association is the immediate
   mechanism. This preserves the original finding: the current contract still promises bounded
   refresh, #85715 independently reproduces a wrong-PR badge past the documented triggers, and no
   changelog entry through 2.1.238 names a fix. [VERIFIABLE][^1][^3][^12] The evidence identifies
   product ownership, not the closed-source invalidation branch. [INFERENCE]
2. **Agent view: file a separate feature request now, not the same bug report.** Confidence:
   **high (0.93)** that the observed multi-PR count is cumulative, event-linked session history at
   the documented boundary and not evidence of the footer poller failure. Use `/feedback`'s prefilled
   GitHub-issue flow to request a per-PR unlink, open-only filter, latest-PR display, and non-destructive
   association reset; reference #71165 rather than presenting cumulative `N PRs` alone as a defect.
   [VERIFIABLE][^10][^11][^13]
3. **Escalate Agent view as a separate bug only with a violated rule.** Examples are a PR linked with
   no documented trigger, a documented `gh`/push trigger that remains missing after its retry window,
   or an incorrect status color. The existing observation does not yet isolate one of those failures.
   [INFERENCE]

Before restarting the affected footer process, capture the minimal status-line input fields `version`, `cwd`,
`workspace.current_dir`, `workspace.git_worktree`, and `pr` for one render. Then restart, capture them
again, and include both in the report. Use `/feedback` because Anthropic documents that it sends the
transcript and description and offers a prefilled GitHub issue. [VERIFIABLE][^2][^10]

The footer report should reference #85715 first (same wrong-PR retention across a branch transition),
#24834 (closed/merged badge family, now closed without linked development), and #80209 (known per-
process PR-poller failure, still open). Emphasize the disambiguating reproduction: the session branch
never had a PR; the session merged a PR belonging to a different worktree; that worktree/branch was
deleted; `gh pr status` showed no association; and the badge survived the successful PR-changing
command that current docs name as an immediate refresh trigger. [INFERENCE]

The lack of native footer force-refresh/hide controls can be included as a recovery requirement in
the bug report, but the Agent-view controls should remain a separate enhancement because that surface
intentionally retains historical links. [INFERENCE]

## Gaps, blindspots & emergent findings

### Missing incident capture

The original 2026-08-13 process version was not captured; the 2026-08-15 recurrence is reported on
2.1.233, this run measured 2.1.236 in the research shell, and the fetched changelog reaches 2.1.238.
The recurrence's badge
underline color, elapsed wall time after merge, and status-line input's `pr`/`workspace` values remain
uncaptured. [NO SOURCE] These are not minor details:

- `pr.number == 1` after merge would locate the bug upstream of both footer renderers.
- `pr` absent while the native badge remains would isolate a TUI state/render invalidation bug.
- A wrong `workspace.current_dir` or `workspace.git_worktree` would support stale-worktree identity.
- Correct workspace plus stale `pr` would support a mis-keyed/stopped PR producer.

This diagnostic split follows directly from Anthropic's statement that `pr.*` mirrors the native
badge and that workspace identity is separately available. [VERIFIABLE][^2]

### Distinct cache classes

No evidence links this PR-state bug to task-panel namespace rotation or task persistence. Claude Code
documents tasks and PR status as separate interactive features, and the reported PR poller has its own
process state. Treating the fleet's prior task-cache incident as the same architecture would be anchor
bias. [VERIFIABLE][^1][^6] Architectural linkage: no source located. [NO SOURCE]

The historical product-contract reversal is the more important blindspot: releases explicitly showed
merged PRs in purple, while current docs require disappearance. A report should include the running
version so Anthropic can distinguish a stale implementation/feature flag from a refresh failure.
[VERIFIABLE][^1][^3]

### Adjacent CLI comparison

GitHub CLI's `gh pr status` is an appropriate control because its documented output separates the
current branch and the user's open PRs; the incident's empty result therefore conflicts with Claude
Code's badge association. [VERIFIABLE][^8] No load-bearing evidence was found that `gh pr status`
itself persistently caches PR state, and no comparison with lazygit/git-flow changes the ownership
conclusion: those tools do not render this Claude Code-native footer. [NO SOURCE]

### Surface-name ambiguity

Claude Code now has at least four easily conflated PR/session surfaces: the single-session prompt
footer, terminal Agent view (`claude agents`), the `/agents` subagent panel, and Claude Desktop's
session sidebar. Official docs explicitly distinguish `claude agents` from `/agents`; Desktop issues
#86289 and #86838 report sidebar failures without establishing a terminal Agent-view count defect,
and #86289 explicitly says terminal Agent view still works. [VERIFIABLE][^11][^15][^16] Future
reports should name the invocation and attach a screenshot; “Agents panel” alone is not specific
enough for triage. [HEURISTIC]

## Open Questions

1. Does the symptom reproduce on 2.1.238 or later, and is its badge underline purple?
2. Did the status-line JSON contain stale `pr.number: 1`, and what did its workspace fields report?
3. Did a full process restart clear the badge while resuming the same transcript and worktree?
4. Can the sequence reproduce on the current release with a plain repository: start on branch A with
   no PR, run `gh pr merge <PR-on-branch-B>`, delete B, and wait/run `gh pr status`?
5. Does the issue require the `gh pr merge` command to be executed by Claude Code's Bash tool (so its
   command observer sees it), or does the same occur when the merge happens externally?
6. For each PR in one high-count Agent-view row, which documented linking trigger appears in that
   session's transcript, and which expected trigger is absent from a zero-count row?
7. Does an Agent-view PR color update after the remote PR changes state, even though the historical
   association intentionally remains?

## Sources

[^1]: Anthropic. (2026). [Interactive mode: PR review status](https://code.claude.com/docs/en/interactive-mode#pr-review-status). Claude Code Docs. Verified accessible (HTTP 200) 2026-08-20. (Current open-PR-only contract, provider-dependent polling schedule, immediate refresh triggers, idle pause, and hyperlink opt-out.)
[^2]: Anthropic. (2026). [Customize your status line: available data](https://code.claude.com/docs/en/statusline#available-data). Claude Code Docs. Verified accessible (HTTP 200) 2026-08-20. (`pr.*` mirror semantics and workspace identity fields.)
[^3]: Anthropic. (2026). [Claude Code changelog](https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md). GitHub. Verified accessible (HTTP 200) 2026-08-20. (Release history for PR footer and Agent view through latest listed version 2.1.238, including the 2.1.218 lost-PR-event fix.)
[^4]: srinitude. (2026). [Footer shows closed/merged PR indicator — should only display open PRs](https://github.com/anthropics/claude-code/issues/24834). anthropics/claude-code issue #24834. Verified accessible (HTTP 200) 2026-08-20. (Closed CLI stale-badge report; no linked development shown.)
[^5]: ArtemYurov. (2026). [Status bar shows merged/closed PR instead of hiding it](https://github.com/anthropics/claude-code/issues/25512). anthropics/claude-code issue #25512. Verified accessible (HTTP 200) 2026-08-20. (CLI stale-badge report closed as duplicate.)
[^6]: 178inaba. (2026). [Footer PR badge silently and permanently disabled after a slow status fetch](https://github.com/anthropics/claude-code/issues/80209). anthropics/claude-code issue #80209. Verified accessible (HTTP 200) 2026-08-20. (Open reverse-engineered 2.1.217 per-process poller failure and restart recovery.)
[^7]: ChrispyKreme86. (2026). [Desktop app PR status bar shows closed/merged PR on main](https://github.com/anthropics/claude-code/issues/63767). anthropics/claude-code issue #63767. Verified accessible (HTTP 200) 2026-08-15. (Adjacent Desktop `state=all` stale-PR path, closed as not planned; not the CLI path.)
[^8]: GitHub. (2026). [`gh pr status`](https://cli.github.com/manual/gh_pr_status). GitHub CLI Manual. Verified accessible (HTTP 200) 2026-08-15. (Current-branch and relevant-open-PR status behavior.)
[^9]: Anthropic. (2026). [Claude Code settings](https://code.claude.com/docs/en/settings). Claude Code Docs. Verified accessible (HTTP 200) 2026-08-20. (`prUrlTemplate`, native footer-link behavior, and whole-Agent-view disablement.)
[^10]: Anthropic. (2026). [Error reference: report an error](https://code.claude.com/docs/en/errors#report-an-error). Claude Code Docs. Verified accessible (HTTP 200) 2026-08-20. (`/feedback` transcript submission and prefilled-GitHub-issue workflow.)
[^11]: Anthropic. (2026). [Manage multiple agents with agent view](https://code.claude.com/docs/en/agent-view). Claude Code Docs. Verified accessible (HTTP 200) 2026-08-20. (Per-session PR-link triggers, multi-PR count, merged/closed display states, supervisor architecture, state location, removal, and disablement.)
[^12]: sjboberg. (2026). [Footer PR badge stuck on previous PR after switching branches with `gh pr checkout`](https://github.com/anthropics/claude-code/issues/85715). anthropics/claude-code issue #85715. Verified accessible (HTTP 200) 2026-08-20. (Open wrong-PR retention report against 2.1.227 despite documented refresh triggers.)
[^13]: cheekychops. (2026). [Agent View: make PR label and row summary configurable](https://github.com/anthropics/claude-code/issues/71165). anthropics/claude-code issue #71165. Verified accessible (HTTP 200) 2026-08-20. (Closed duplicate feature request for latest-PR versus cumulative-count display.)
[^14]: coygeek. (2026). [Agent view documentation omits linking to existing PRs a session acts on](https://github.com/anthropics/claude-code/issues/75887). anthropics/claude-code issue #75887. Verified accessible (HTTP 200) 2026-08-20. (Closed documentation report for the v2.1.205 event-link expansion now described by current docs.)
[^15]: manyuksheerasagar-gif. (2026). [PR state badges missing from session sidebar in Claude app](https://github.com/anthropics/claude-code/issues/86289). anthropics/claude-code issue #86289. Verified accessible (HTTP 200) 2026-08-20. (Open Desktop-sidebar regression that explicitly says terminal Agent view still works.)
[^16]: GL1ZZO. (2026). [Desktop: session list PR badges all reset to “open” after app update](https://github.com/anthropics/claude-code/issues/86838). anthropics/claude-code issue #86838. Verified accessible (HTTP 200) 2026-08-20. (Open Desktop-sidebar stale-state report; not terminal Agent view or the prompt footer.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** N/A — ad hoc `cx research`
**Model:** `gpt-5.6-sol`
**Date:** 2026-08-13

```text
ROLE: You are a leaf worker. Do NOT spawn sub-agents. You are researching a narrow, self-contained
question via Codex `cx research --write-target`, following the fleet's `/research-doc` method
(this run is delegated FROM the Claude Code orchestrating session, which read /research-doc's
SKILL.md and confirmed a cross-repo doc-discovery pass found no existing overlapping doc before
launching you).

TASK: bd issue ISSUE-REF (display ISSUE-REF), ai-harness, P3, type=bug. Write a
research doc; do not propose or make any code change yourself.

## Objective

Determine the root cause of, and the correct fix/workaround for, this observed behavior: Claude
Code's own native TUI footer (the bottom status line showing e.g. `⏵⏵ bypass permissions on ·
PR #1 · 1 shell`) continued to display a merged pull request's number after that PR was merged and
after the current git branch had no PR associated with it at all.

## Context / Inputs

**Standing snapshot line:** These repo facts are a snapshot taken at the time stamped below.
Re-verify anything load-bearing against current Claude Code documentation/release notes before
concluding a specific version fixed or introduced this; if it has moved, say so and work from what
you measured, not from this brief.

**Observed incident (measured 2026-08-13, session ISSUE-REF, ai-harness repo):**
- Session merged a PR ("PR #1", branch `wt/aih89bm8-install-harness-skill` -> `main`) via
  `gh pr merge --squash --delete-branch`, then deleted the local worktree/branch.
- The session's current working branch is `wt-ISSUE-REF` (a different, always-existing branch used by
  this `ai c`/`ai-cli-utils`-launched session), which has never had any PR associated with it.
- `gh pr view 1 --json state,mergedAt,headRefName,baseRefName` confirmed: `state: MERGED`,
  `mergedAt: 2026-08-13T01:25:29Z`.
- `gh pr status` from the `wt-ISSUE-REF` branch confirmed: "There is no pull request associated with
  [wt-ISSUE-REF]" and no open PRs created by this user.
- Despite both of the above, the Claude Code CLI's own TUI footer chrome continued to render
  `PR #1` in its status line for an extended period after the merge (observed continuously across
  several subsequent turns in the same running session -- not a one-off render glitch caught mid-
  transition).
- This machine's custom `~/.claude/statusline-command.sh` (a user-configured `statusLine` command
  hook, distinct from CC's built-in TUI chrome) was checked and contains NO PR-related logic
  whatsoever (grepped for "pr\b", "pull request", "gh pr" -- zero matches) -- ruling out the custom
  statusline script as the source; this is CC's own built-in footer, not a user hook.
- Session details: Claude Code CLI, macOS (Darwin), model Sonnet, running via a session launched by
  the `ai-cli-utils` `ai c <n>` wrapper (worktree-per-session pattern, `.claude/settings.json`
  `statusLine.command` configured to the custom script above -- that script is a SEPARATE
  status line from the footer chrome under discussion).

## What to research

1. Is this a documented or previously-reported Claude Code CLI behavior/bug? Search the
   `anthropics/claude-code` GitHub repository's issues (open and closed) and any official docs for
   mentions of a stale/cached PR indicator in the TUI footer, git-branch-to-PR association caching,
   or footer refresh timing. Also check Claude Code's official documentation
   (`code.claude.com/docs` / the docs `llms.txt` index) for any description of how/when the footer's
   PR indicator is computed or refreshed.
2. What likely mechanism would produce this specific symptom? Consider and evaluate (do not just
   list -- reason about plausibility given the observed facts): (a) the footer caches the
   branch-to-PR association at session start or at branch-switch time and does not re-poll `gh`/the
   GitHub API after a merge event within the same session; (b) the footer polls on an interval that
   hadn't yet elapsed; (c) the footer is actually associated with a DIFFERENT branch/worktree than
   the one currently active (e.g. a stale reference to the now-deleted `wt/aih89bm8-...` branch that
   didn't clear on worktree/branch deletion); (d) some other mechanism.
3. Known workarounds: does restarting the CC session, running a specific command, or any documented
   action force the footer to re-poll? Is there a way to disable this footer indicator entirely if
   no fix exists?
4. Is this specific to worktree-based multi-session setups (branch/worktree deleted out from under
   a still-running session), or would it also reproduce in a plain single-worktree repo? This
   matters for scoping whether the fix belongs to this fleet's own `ai-cli-utils` tooling or is
   purely a CC product issue with no local workaround.
5. If you find NO reported issue and no documented mechanism (plausible, since the exact internal
   implementation of CC's footer chrome is closed-source and this may be a genuinely undocumented
   corner case), say so explicitly rather than speculating past the evidence -- state clearly that
   this appears to be an unreported/undocumented CC behavior and that a `/report-feedback` to Anthropic
   (rather than a local `/bug-fix`, since there is no local code producing this footer) is the
   only available next step.

## Scope note -- questions, examples, and named references are a starting point, not a checklist
The questions above are illustrative anchors and a FLOOR for this research -- not an exhaustive
list to answer only. Reason independently: survey the landscape broadly (other CLI tools' similar
git/PR-status footer features and how they handle staleness, e.g. `gh`'s own `gh pr status`
caching behavior, `lazygit`, `git-flow` tooling), follow the evidence where it leads, and surface
relevant findings not anchored here.

## Independent exploration (gaps, blindspots, emergent findings) -- required
As you research, actively surface what this framing may be missing:
- Adjacent factors the questions don't capture (e.g. is this related to CC's known task-panel
  namespace rotation behavior on process restart -- a DIFFERENT stale-cache class this fleet has
  already documented for a different CC subsystem -- or is it architecturally unrelated).
- Contrarian/disconfirming evidence -- report it even if it complicates the "CC bug" framing (e.g.
  if you find evidence this is actually caused by something in `ai-cli-utils`' own session-launch
  wrapper rather than CC itself).
- Known failure modes and second-order effects of a stale PR indicator (is this purely cosmetic, or
  could stale-branch-association data feed into any other CC behavior in a way that matters).
Whenever a load-bearing thread surfaces mid-research, follow it to its conclusion and report it in
a dedicated "Gaps, blindspots & emergent findings" subsection.

<grounding_instructions>
You are a senior developer-tools engineer with deep hands-on experience debugging CLI TUI
applications, git/GitHub integration tooling, and terminal status-line rendering. You have strong
opinions backed by evidence. When you cannot find a source, you say so explicitly rather than
speculating past the evidence.

Temporal scope: Weight sources by recency -- 2026 (primary) -> 2025 -> 2024. Pre-2024 sources are
background context only unless foundational. If post-2024 literature is genuinely sparse for a
subtopic, state "[subtopic]: no significant post-2024 developments found" rather than backfilling
with older sources. Backfilling is a failure mode, not a hedge.

Before generating your final output, execute a Chain-of-Verification (CoVe) to ensure factual
fidelity over compliance. Inside your thought process: (1) isolate the core facts required, (2)
draft a tentative response, (3) Hostile Cross-Examination -- flag any claim where you are citing a
source because the prompt implied you should, rather than because you verified it, (4) strip away
any claim that cannot be empirically verified.

When generating your final output, classify every major claim. Write your rationale before
appending the tag -- writing the tag first causes post-hoc rationalization. Rationale -> evidence
check -> tag.

- [VERIFIABLE]: backed by documentation, peer-reviewed research, or official tech blogs
  (2024-2026). Carry an inline footnote ref to the source: [VERIFIABLE][^N].
- [HEURISTIC]: widely accepted best practice without a specific citation.
- [INFERENCE]: a logical conclusion drawn from context. Provide your reasoning in-text. Do not
  fabricate a source. Tier tag only -- NO footnote ref.
- [NO SOURCE]: explicitly state when you cannot find verifiable data. Tier tag only -- NO footnote
  ref.

Citation format (mandatory for every externally-sourced claim):
- Inline: append the GFM footnote ref directly after the tier tag -- [VERIFIABLE][^N]. A claim
  citing multiple sources carries ascending separate refs -- [VERIFIABLE][^3][^7] (never grouped,
  never out of order).
- Footnote definitions live once, under ## Sources, in APA form with a clickable URL or DOI and an
  access-verification stamp, e.g.:
    [^1]: Example Org. (2026). [Page title](https://example.com/page). Example Docs. Verified
    accessible (HTTP 200) 2026-08-13. (Scope note.)
- URL-or-DOI ALWAYS: every source entry carries a clickable URL -- paywalled/gated is fine (link it
  anyway; stamp the access status). Only the truly-irreducible case gets an explicit
  [no online source located] marker with a one-line justification.
- Integrity: footnote refs are contiguous from [^1], every [^N] ref has a matching definition, and
  every definition is referenced -- no gaps, no orphans.
- [INFERENCE] / [NO SOURCE] claims carry the tier tag with NO footnote ref.

Hard constraint (overrides all formatting preferences): never invent a citation to satisfy a
formatting instruction. Accuracy > completeness.

For each load-bearing claim, run at least one web_search or web_fetch call; do not return
[VERIFIABLE] without a URL you actually fetched in-session. Do not stop after one round -- if no
source is found for a sub-question, run additional searches across alternate framings before
tagging it [NO SOURCE].

Format diagrams using Mermaid.js or ASCII. Format math using LaTeX. NEVER generate binary images.
</grounding_instructions>

## Output contract

Write the completed research doc to docs/research/cc-tui-stale-pr-footer-indicator.md (already
scaffolded from the canonical STUB at that exact path in this worktree -- fill it, do not create a
new file), following the canonical `docs/research/TEMPLATE.md` structure in full: frontmatter
(update `status: complete`... actually leave `status:` as `draft` since this is a narrow research
doc, not a full deep-research doc -- use your judgment per the template's own conventions) -> Table
of Contents -> Context (immutable region -- leave as scaffolded) -> Temporal Scope -> Executive
Summary (3-5 numbered findings) -> your research sections -> Sources -> Appendix: Research Prompt
(Registry ID: N/A -- ad hoc cx research, no aido registry; Model: your resolved model id; Date:
2026-08-13; full prompt text in a fenced text block) -> Appendix: Provenance Ledger -> Run History.

In your final response text (under 250 words): state your top-line conclusion (known CC issue /
plausible undocumented behavior / cause identified elsewhere) and your recommendation on
`/bug-fix` (a local code fix exists) vs `/report-feedback` (report upstream to Anthropic, no local fix
possible) vs "self-resolves, no action needed" -- with your confidence level and the one-line
reason.

## Boundaries

Write ONLY docs/research/cc-tui-stale-pr-footer-indicator.md. No `bd` access from this worktree.
No git commit, no git push. The orchestrating session reviews and ships your output.
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

| Claim | Source | Supporting excerpt or observation | Verdict |
|---|---|---|---|
| Current badge must disappear after merge/close; polling is provider-dependent | [Interactive mode](https://code.claude.com/docs/en/interactive-mode#pr-review-status) | “The badge disappears once the pull request merges or closes”; the page now specifies about 90 seconds for active Anthropic-API sessions with feature flags and 60 seconds for listed alternatives | Entails |
| `pr.*` can distinguish producer state from native-footer render state | [Status line data](https://code.claude.com/docs/en/statusline#available-data) | “Mirrors the PR badge in the bottom status bar.” | Entails |
| Merged badges were historically intentional | [Changelog](https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md) | “merged pull requests ... purple status indicator” | Entails historical claim only |
| Stale merged/closed CLI badge was previously reported | [Issue #24834](https://github.com/anthropics/claude-code/issues/24834) | “persists across session restarts” | Entails bug-family claim |
| Duplicate plain-CLI report exists | [Issue #25512](https://github.com/anthropics/claude-code/issues/25512) | CLI status bar retained a prior merged PR | Entails |
| Per-process PR poller can stop after a slow fetch | [Issue #80209](https://github.com/anthropics/claude-code/issues/80209) | Reporter located a permanent disable branch in 2.1.217 | Supports mechanism class; does not prove this incident |
| Desktop has a separate stale-PR path | [Issue #63767](https://github.com/anthropics/claude-code/issues/63767) | Desktop queried `state=all` | Entails distinction |
| `gh pr status` is a valid current-branch control | [`gh pr status` manual](https://cli.github.com/manual/gh_pr_status) | “Show status of relevant pull requests.” | Entails |
| No local renderer was found in the launcher | `ai-cli-utils/src/ai_cli/session_script.py` (read-only local inspection) | Launcher changes cwd and invokes `claude`; no PR/footer producer | Supports local-ownership inference |
| Installed research-shell version | Local `claude --version` on 2026-08-13 | `2.1.228 (Claude Code)` | Direct local measurement; incident process version unresolved |
| Current release baseline | [Changelog](https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md) | “## 2.1.238” | Entails latest listed release at retrieval time |
| Current research-shell version | Local `claude --version` on 2026-08-20 | `2.1.236 (Claude Code)` | Direct local measurement; the latest incident process remains the user-reported 2.1.233 recurrence |
| Previously tracked footer reports have mixed tracker states | [Issue #24834](https://github.com/anthropics/claude-code/issues/24834), [#25512](https://github.com/anthropics/claude-code/issues/25512), and [#80209](https://github.com/anthropics/claude-code/issues/80209) | `Closed`, `Closed as duplicate`, and `Open`, respectively | Entails tracker status only; does not establish resolution |
| Wrong-PR footer retention still has a current public reproduction | [Issue #85715](https://github.com/anthropics/claude-code/issues/85715) | “continues to display PR A indefinitely” after checkout, refresh commands, and more than 60 seconds | Entails close bug-family match; not the exact cross-worktree sequence |
| Agent view associations are event-linked and cumulative | [Agent view](https://code.claude.com/docs/en/agent-view) | “When a session has multiple PRs, the cell shows a count such as `3 PRs`”; captured commands and branch lookups create links | Entails documented multi-PR behavior |
| Agent view is a distinct supervisor-backed session surface | [Agent view](https://code.claude.com/docs/en/agent-view) | “Each session runs as its own Claude Code process” and state is stored at `~/.claude/jobs/<id>/state.json` | Entails architectural distinction at the documented boundary |
| Whole-session removal and whole-view disablement exist, but no per-PR unlink is documented | [Agent view](https://code.claude.com/docs/en/agent-view) and [Settings](https://code.claude.com/docs/en/settings) | `Ctrl+X`/`claude rm` remove a session; `disableAgentView` disables the feature | Entails supported broad controls; absence of per-PR control remains a search finding |
| A latest-PR mode is a community feature request | [Issue #71165](https://github.com/anthropics/claude-code/issues/71165) | Requests showing the “latest/relevant PR instead of a generic count” | Entails control request, not product commitment |
| Existing-PR association expanded in 2.1.205 | [Issue #75887](https://github.com/anthropics/claude-code/issues/75887) and [Agent view](https://code.claude.com/docs/en/agent-view) | Sessions can link PRs they edit, comment on, close, mark ready, check out, or push to | Entails current documented trigger set |
| Desktop sidebar and terminal Agent view can diverge | [Issue #86289](https://github.com/anthropics/claude-code/issues/86289) | “PR badges still appear correctly in `claude agents` terminal UI” while missing in Desktop | Supports treating that report as an adjacent surface, not the terminal-count symptom |
| A newer Desktop sidebar report does not establish a terminal Agent-view count defect | [Issue #86838](https://github.com/anthropics/claude-code/issues/86838) | Desktop session badges reset merged sessions to open after an app update while `gh` still showed them merged | Entails a separate Desktop symptom only |

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

- **2026-08-13 — Run 1:** Ad hoc `cx research --write-target`; model `gpt-5.6-sol`, effort `high`.
  Searched current official Claude Code documentation, raw changelog, open/closed
  `anthropics/claude-code` issues, and GitHub CLI documentation; inspected the designated scaffold,
  canonical templates, installed Claude Code version, and `ai-cli-utils` launch path read-only. Wrote
  only this designated research target. No registry ID; no code changes; no commit or push.
- **2026-08-15 — Run 2 (refine):** Delegation session `ISSUE-REF`; model `gpt-5.6-sol`, effort `high`,
  persona `research`. Re-fetched the official footer, status-line, settings, error-reporting, and
  Agent-view documentation; re-checked the live changelog and previously cited GitHub issues; and
  surveyed new footer, Agent-view, and adjacent Desktop reports. Added the Agent-view architecture,
  multi-PR semantics, clearability analysis, fresh tracker status, separate bug/feature recommendation,
  source entries, and provenance rows. Completed conventional, contrarian, historical, adjacent-field,
  and skeptical claim review; wrote only this designated research target. No push.
- **2026-08-20 — Run 3 (refine):** Model `gpt-5.6-sol`, effort `high`, persona `research`.
  Re-fetched the current official footer, status-line, settings, error-reporting, Agent-view docs,
  changelog through 2.1.238, and tracked GitHub issues; surveyed reports added since 2026-08-13.
  Corrected the provider-dependent footer polling contract, made the closed-source Agent-view failure
  path explicit, added the current file-now recommendation and newer Desktop report, restored the
  original 0.88 footer ownership confidence, and wrote only this designated target. The issue DB was
  unavailable because its embedded lock could not be opened in the sandbox, so the supplied issue
  brief was used as authorized. No push.

<!-- /doc:region name="run_history" -->
