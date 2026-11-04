---
title: Claude Code background sub-agent transcript pane rendering bug
category: research
tags: [research, claude-code, tui, subagent, background-agent, rendering-bug]
status: complete
source: "codex-research-2026-08-06"
template_version: "research-1.2.0"
---
<!-- Canonical Jinja source: STUB.md.jinja. This direct-copy stub is retained for consumers that have not migrated to rendering it. -->

# Claude Code background sub-agent transcript pane rendering bug

**Status:** complete

**Created:** 2026-08-06

<!-- doc:region name="context" kind="immutable" -->

## Table of Contents

- [Context](#context)
- [1. Finding: two related UI defects, only one exact transcript-content match](#1-finding-two-related-ui-defects-only-one-exact-transcript-content-match)
- [2. Focused panes can show parent-session model, effort, and cwd](#2-focused-panes-can-show-parent-session-model-effort-and-cwd)
  - [2.1 Bottom status line: exact prior reports](#21-bottom-status-line-exact-prior-reports)
  - [2.2 Other pane chrome: corroborating model and effort defects](#22-other-pane-chrome-corroborating-model-and-effort-defects)
- [3. Transcript body: no exact public match located](#3-transcript-body-no-exact-public-match-located)
  - [3.1 This is not a documented placeholder limitation](#31-this-is-not-a-documented-placeholder-limitation)
  - [3.2 Closest adjacent content failures](#32-closest-adjacent-content-failures)
- [4. Reproducibility and conditions](#4-reproducibility-and-conditions)
- [5. Release status and practical diagnostics](#5-release-status-and-practical-diagnostics)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
- [Gaps, blindspots & emergent findings](#gaps-blindspots--emergent-findings)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Ambiguous Items from Auto-Remediation (Post-Run Review)](#ambiguous-items-from-auto-remediation-post-run-review)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

## Context

Firsthand observation, this fleet, 2026-08-06 (session ISSUE-REF, `example-user` repo): a Claude Code
background sub-agent was launched via the `Agent` tool (named teammate `aih706-fable-coordinator`,
model Fable, running a long autonomous task). When the user selected that agent in the CC TUI's
agent switcher to inspect its transcript, the pane initially showed a stale/wrong render — visible
content was the tail of the agent's own system prompt text, followed by a status bar reading
"Sonnet 5 with high effort" and a cwd of the *parent* session's own worktree
(`<workspace>/.worktrees/ISSUE-REF`), not the sub-agent's actual model (Fable) or its own working
directory (an `ai-harness` worktree). On a later selection of the same agent, the transcript pane
showed the correct label (`CANONICAL ALIGNMENT — READ THESE FIRST, BEFORE ANY...`, matching the
real prompt) with a plausible elapsed-time/token counter and a queued-message count, i.e. genuine
live data — but the same bottom status bar mismatch (wrong model/cwd) was reported as still
present. The user separately confirmed that a *different* Fable-model background agent, running
in a different concurrent CC session on the same machine, rendered its own transcript pane
correctly with no such symptoms — suggesting this is not a universal/always-reproducible failure
mode but something session-, timing-, or state-dependent. The user also independently recalled
having seen "agent transcripts not always rendering correctly" before, and this session separately
experienced an unrelated CC statusline going fully black earlier the same day (resolved by
highlighting the terminal pane) — both offered as anecdotal priors, not confirmed related.

This document exists to determine whether this is a known, previously reported Claude Code defect
(the TUI's handling of background/sub-agent transcript panes, multi-agent switcher UI, or its
status-bar binding), find any existing GitHub issues, forum threads, or changelog entries
describing the same or a closely related symptom, and assess whether there is a known workaround,
root cause, or fix version.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## 1. Finding: two related UI defects, only one exact transcript-content match

There is a known and publicly reported Claude Code defect in which focusing an Agent-tool
subagent or agent-team teammate changes the transcript body but leaves the bottom `statusLine`
bound to the parent/main session. Two open August 2026 reports describe the parent model,
effort, context, cost, and cwd/worktree continuing to appear while a child transcript is
focused. One report reproduced it on Claude Code 2.1.220/Windows 11; the other states that the
status-line payload has no focused-subagent field, so a custom script cannot correct it.
[VERIFIABLE][^2][^3]

The broader wrong-identity family is independently reported in the subagent view header,
thinking indicator, and task/model badge. In those reports, the saved subagent JSONL shows the
requested model actually ran while the TUI displays the parent model. This makes the observed
`Sonnet 5`/parent-cwd footer on a Fable/worktree subagent a close match to known defects, not
evidence that the Fable override or isolated cwd necessarily failed at runtime.
[VERIFIABLE][^4][^5]

The transcript-body symptom is less settled. After multi-angle searches of the Claude Code issue
tracker, releases, official documentation, Reddit, and the general web, no public report was
located that exactly says an in-process Agent-tool subagent pane displays the parent
conversation's prompt or live turns instead of the selected subagent's own transcript.
[NO SOURCE] The incident recorded in `Context` is also narrower than that proposed symptom:
the visible text was later identified as the selected agent's own system prompt, not the
parent's prompt. Its initial appearance may still have been stale or incomplete, but the
available firsthand record does not establish cross-session transcript substitution.
[INFERENCE]

## 2. Focused panes can show parent-session model, effort, and cwd

### 2.1 Bottom status line: exact prior reports

Issue #83512 is the closest exact report. It says that switching to a Task subagent or
agent-team teammate changes the transcript while the bottom bar continues to show the main
loop's model, context-window usage, effort, cost, and related values. Its reproduction uses
different parent/child models and reports the behavior on 2.1.220. The reporter's inspection of
the shipped bundle—not an Anthropic maintainer statement—found one global `statusLineText`
value whose producer consumes the main REPL message list, `mainLoopModel`, and `cwd`; pane
focus changes transcript selection without re-parameterizing that producer.
[VERIFIABLE][^2]

Issue #83289 independently reports the same focused-pane behavior and explicitly names
`cwd/branch/worktree` alongside model, effort, and context. It also records the key product
constraint: the normal `statusLine` input does not identify the focused subagent, whereas the
separate `subagentStatusLine` input already carries each task's resolved model, effort, and cwd.
The official status-line documentation confirms that `subagentStatusLine` is a per-row agent
panel renderer and that each task object includes `model`, `effort`, and `cwd`; it is not the
bottom global bar. [VERIFIABLE][^3][^11]

Accordingly, a persistent parent model/cwd footer after the body has corrected is best explained
by a main-session-scoped status-line data contract, not by stale terminal cells. Resizing or
refocusing may trigger a repaint, but it cannot synthesize focused-agent fields that the hook
payload does not contain. [INFERENCE]

### 2.2 Other pane chrome: corroborating model and effort defects

Issue #77655 reports that a focused subagent's view header and thinking indicator render the
main session's model, effort, and agent identity. Issue #76343 gives a model-ground-truth check:
an Agent-tool background subagent launched from Fable with an Opus override showed Fable in the
TUI while all 29 assistant messages in its JSONL were stamped Opus. Both were open when fetched.
[VERIFIABLE][^4][^5]

The effort-label subcase has a partial released fix. Issue #83252 reported that the subagent
spinner used the session effort because a per-agent effort field was populated only for the main
agent; Claude Code v2.1.222, released 2026-08-04, states that it fixed the spinner effort label
inside a subagent transcript view. That release note does not mention the global status line,
focused-pane cwd, header identity, or transcript-body selection. [VERIFIABLE][^6][^7]

## 3. Transcript body: no exact public match located

### 3.1 This is not a documented placeholder limitation

Claude Code's current documentation says named subagents run in their own context window with
their own system prompt, model, and permissions; the panel opens a selected subagent's
transcript, accepts user messages directed to that agent, and stores each transcript separately
at `~/.claude/projects/{project}/{sessionId}/subagents/agent-{agentId}.jsonl`. Main-conversation
compaction does not alter those files. The documented behavior is therefore a real per-agent
live transcript, not a placeholder that intentionally reuses the parent view.
[VERIFIABLE][^1]

One important disconfirming case is `fork`/`/subtask`. A fork intentionally inherits the full
parent conversation, system prompt, tools, and model, while a named subagent starts fresh with
its own definition and separate prompt cache. Parent history in a fork transcript can therefore
be correct behavior; the classification must be verified from the Agent invocation or transcript
metadata before calling the body content wrong. [VERIFIABLE][^1]

### 3.2 Closest adjacent content failures

The closest body-render report is issue #83045: returning from an agent detail view to FleetView
can leave fragments of the previous screen until a terminal resize forces a repaint. It was
intermittent on 2.1.220 over Linux/SSH and had related Windows reports. This establishes an
active stale-cell/full-repaint failure class, but it does not establish that the in-process
subagent transcript selector reads the wrong transcript object. [VERIFIABLE][^8]

Issue #74403 describes a more serious identity mix-up in the separate `claude agents`/workflow
surface under tmux: a one-time hydration relaunch could attach a fresh TUI to a stale session,
replace the visible conversation, and append new records to the wrong transcript. Its reporter
linked the window to a shared daemon plus recycled tmux tty/pane identities. That is relevant
only if the incident involved agent view/workflow hydration; it is not an exact reproduction of
opening an Agent-tool child inside an already-running parent TUI. [VERIFIABLE][^9]

Issue #74114 reports a different content failure after compaction: an internal compactor prompt
appeared as a user message in a subagent JSONL, observed but not reliably reproduced. If suspect
text exists in the child JSONL, this transcript-contamination class is more plausible than a
paint-only bug; if the child JSONL is correct while the screen is wrong, it is not.
[VERIFIABLE][^10]

## 4. Reproducibility and conditions

- **Footer model/cwd:** the public reports present it as deterministic whenever the focused
  agent differs from the parent and the bottom `statusLine` renders main-session fields.
  Reports cover Agent-tool subagents and teammates, and the exact footer case appears on Windows
  while related wrong-model chrome appears on macOS. This argues against tmux or one terminal
  emulator being necessary. [VERIFIABLE][^2][^3][^5]

- **Other chrome:** wrong model and effort were repeatedly reported across 2.1.206–2.1.220.
  The spinner effort component is fixed in 2.1.222; the cited global status-line and header/model
  issues remained open when fetched on 2026-08-06. [VERIFIABLE][^2][^4][^5][^7]

- **Transcript body:** the firsthand behavior was intermittent in one session and absent in a
  concurrent comparison session. Public evidence supports intermittent stale repaint in
  FleetView and a tmux/daemon identity race in another surface, but not the exact Agent-tool pane
  symptom. [VERIFIABLE][^8][^9] No source ties the exact symptom to elapsed runtime, prompt or
  token volume, queued inbound-message count, number of concurrent sessions, or compaction.
  [NO SOURCE]

- **Focus/resize:** official status-line troubleshooting says a custom line can go blank when
  its command exits nonzero or emits nothing and can remain stale while a slow script runs.
  Separately, issue #83045 says resize restores a garbled agent view. These explain some
  blank/stale UI anecdotes, but neither proves the parent-bound footer and transient transcript
  body share a root cause. [VERIFIABLE][^8][^11]

- **Multiplexers:** in-process agent teams work in any terminal; split-pane teams require tmux
  or iTerm2 and are explicitly unsupported in VS Code's terminal, Windows Terminal, and Ghostty.
  Thus terminal/multiplexer details matter to split-pane and repaint investigations, but not to
  the documented in-process selector itself. [VERIFIABLE][^12]

## 5. Release status and practical diagnostics

Claude Code v2.1.222 is the first located release note that directly names one component of this
family: the wrong session effort in a subagent transcript spinner. It also contains model-family
and worktree isolation fixes, but it does not claim a fix for #83512's global bottom bar,
#83289's focused cwd/worktree payload, the subagent header/model badge, or wrong transcript-body
selection. Treat 2.1.222 as a partial fix, not evidence that the full incident is resolved.
[VERIFIABLE][^7]

The fastest discriminator is the saved child JSONL:

1. Record `claude --version`, OS, terminal, tmux/iTerm2 version and mode, alternate-screen
   settings, parent session ID/cwd/model, child agent ID/name/resolved model, child cwd/worktree,
   queue count, and whether compaction or resume occurred.
2. At the wrong render, capture a timestamped screenshot or screen recording and copy a short,
   sanitized text fingerprint from the visible body.
3. Compare that fingerprint and timestamp with the parent JSONL and the child's
   `subagents/agent-{id}.jsonl`. Compare child assistant `message.model` values with the TUI
   badge and compare tool-call cwd evidence with the footer.
4. Repeat with the custom bottom `statusLine` enabled and disabled; with parent/child models and
   cwds equal and different; before/after resize and re-selection; before/after compaction; in a
   clean terminal outside tmux; and, for teams, in-process versus split-pane.

If the child JSONL is correct while the body is wrong, file a transcript-selector/repaint defect.
If the body corrects but the footer stays on parent values, it matches the known pane-unaware
`statusLine` defect. If the child JSONL itself contains foreign text, file a transcript-routing
or compaction-contamination defect and preserve the unmodified logs. [INFERENCE]

## Comparison

| # | Candidate explanation | Fit to the incident | Conditions/evidence | Assessment |
|---:|---|---|---|---|
| 1 | Global bottom `statusLine` is main-session scoped | Explains persistent parent model and cwd after the body corrected | Exact reports name model, effort, cwd/worktree and an absent focused-agent payload | Strongest for footer [VERIFIABLE][^2][^3] |
| 2 | Subagent chrome reads parent identity | Explains wrong model/effort in header, spinner, or task badge | Multiple model-ground-truth reports; spinner effort fixed in 2.1.222 | Strong for non-footer chrome [VERIFIABLE][^4][^5][^7] |
| 3 | Incomplete repaint or transcript-selection race | Could explain transient old/system-prompt text that corrects after navigation | Adjacent FleetView stale-cell report; no exact child-pane report | Plausible but unverified [INFERENCE] |
| 4 | tmux/daemon session-identity collision | Could show wholly unrelated session content | Exact in `claude agents` workflow hydration, shared daemon, tmux 3.7 | Conditional, different surface [VERIFIABLE][^9] |
| 5 | Actual transcript contamination during compaction/resume | Could explain foreign text only if it is present in child JSONL | Intermittent compactor-prompt injection report | Conditional [VERIFIABLE][^10] |
| 6 | Expected fork inheritance | Explains parent history if the worker was a fork rather than a named subagent | Official fork versus named-subagent semantics | Disconfirming check [VERIFIABLE][^1] |

## Recommendation

Do not file the footer mismatch as a wholly new generic bug. Add the sanitized reproduction to
#83512 and cross-reference #83289, emphasizing that the body eventually became correct while
the bottom line continued to show the parent model and worktree. Include the exact Claude Code
version after upgrading to at least 2.1.222 so maintainers can separate the now-fixed spinner
effort component from the still pane-unaware global status line. [INFERENCE]

File a separate focused-transcript bug only if the wrong body recurs and the evidence package can
answer which file was correct. The highest-value report is a minimal matrix plus parent/child
JSONL fingerprints proving one of two assertions: (a) the selector painted parent or stale body
content despite a correct child file, or (b) Claude Code wrote foreign content into the child
file. Name the exact surface—Agent-tool panel, in-process team, split-pane team, or
`claude agents`—because current evidence shows these surfaces have different architectures and
failure modes. [INFERENCE]

For current operation, use the child's JSONL `message.model` as runtime ground truth and
`subagentStatusLine` for per-row resolved model/effort/cwd. Resizing is a reasonable diagnostic
for stale pixels, not a workaround for the global bottom bar. Upgrading is recommended for the
2.1.222 spinner fix and adjacent worktree/model fixes, but no located source says it repairs the
global focused-pane footer. [VERIFIABLE][^5][^7][^11]

## Gaps, blindspots & emergent findings

- **Blindspot: exact transcript substitution.** Searches using `subagent transcript wrong
  model`, `agent switcher stale pane`, `background agent status bar bug`, `parent transcript`,
  `focused transcript`, `teams transcript render`, and Reddit-specific variants found no exact
  public Agent-tool body-content report. The absence may reflect the feature's recency, niche
  usage, private support reports, or different terminology; it is not proof that the defect does
  not exist. [NO SOURCE]

- **Emergent finding: the initially visible text was child-owned.** The frozen incident record
  identifies it as the selected agent's own system prompt. That weakens the proposed
  cross-session-content claim and shifts the unresolved question to why the pane initially
  showed an old/incomplete part of the correct child's state. [INFERENCE]

- **Blindspot: internal TUI architecture.** No official source located in this research confirms
  that Claude Code uses Ink/React component trees or documents how transcript stores and render
  loops are multiplexed. Issue reporters' minified-bundle analyses are useful primary
  observations, but they are not a stable public architecture contract. [NO SOURCE]

- **Emergent finding: there are at least two status-line systems.** The main `statusLine` is
  session-level, while `subagentStatusLine` receives per-task resolved state. Conflating their
  rows can make a correct per-agent row and an incorrect global footer look like one inconsistent
  component. [VERIFIABLE][^3][^11]

- **Contrarian check: terminal behavior is neither necessary nor irrelevant.** Cross-platform
  reports make a terminal-only cause unlikely for the persistent wrong identity, while
  resize-repaired stale frames and tmux hydration collisions show that terminal state can still
  explain a transient wrong body in some surfaces. [VERIFIABLE][^2][^5][^8][^9]

- **Blindspot: trigger variables.** No controlled public matrix was located for elapsed time,
  context/token size, queued messages, number of agents, simultaneous Claude Code sessions,
  compaction boundary, or terminal focus. The firsthand comparison suggests state specificity,
  but it cannot identify which variable matters. [NO SOURCE]

- **Scope correction: subagents, teams, and background sessions are not interchangeable.**
  Official docs distinguish Agent-tool subagents in one session, agent-team teammates as
  independent sessions, and `claude agents` background sessions managed by a supervisor. A fix
  or bug report in one surface should be treated as adjacent evidence until reproduced in the
  other. [VERIFIABLE][^1][^12][^13]

## Open Questions

1. In a recurrence, does the wrong visible text exist in the child JSONL, the parent JSONL, both,
   or neither at the same timestamp?
2. Was the worker a named Agent-tool subagent, a fork/`/subtask`, an in-process teammate, or a
   full background session? Which exact key opened its view?
3. Does the footer remain wrong on v2.1.222 or later with the custom `statusLine` disabled?
4. Does `subagentStatusLine` report the correct resolved model/cwd at the same moment the global
   footer is wrong?
5. Does a terminal resize repair only body pixels, or does it also change the selected transcript
   identity? Does Escape/reselect behave differently?
6. Did the transition occur near auto-compaction, a `SendMessage` resume, a queued inbound
   message, worktree creation/removal, or a one-time agent-view/workflow hydration?
7. Can a minimal two-model/two-cwd reproduction be made deterministic outside tmux and then
   repeated inside tmux or iTerm2?
8. Will Anthropic close #83512/#83289 or add focused-agent state to the `statusLine` payload in a
   post-2.1.222 release?

## Sources

[^1]: Anthropic. (2026). [Create custom subagents](https://code.claude.com/docs/en/sub-agents).
    Claude Code Documentation. Verified accessible (live fetch) 2026-08-06. (Own context,
    transcript storage, panel controls, model/cwd isolation, and fork semantics.)
[^2]: Rojas, A. (2026, August 3). [`statusLine` always reports the main session, even when a subagent or teammate transcript is focused (Issue #83512)](https://github.com/anthropics/claude-code/issues/83512).
    Anthropic/Claude Code. Verified accessible (live fetch) 2026-08-06. (Open user report;
    includes 2.1.220 reproduction and reporter-authored shipped-bundle analysis.)
[^3]: bwish71. (2026, August 2). [`statusLine`: expose focused-subagent context and running-hook status (Issue #83289)](https://github.com/anthropics/claude-code/issues/83289).
    Anthropic/Claude Code. Verified accessible (live fetch) 2026-08-06. (Open user report;
    explicitly covers model, effort, cwd/branch/worktree, and payload limitation.)
[^4]: riptscripts. (2026, July 15). [Subagent view renders the main session's model/effort/agent identity instead of the subagent's (Issue #77655)](https://github.com/anthropics/claude-code/issues/77655).
    Anthropic/Claude Code. Verified accessible (live fetch) 2026-08-06. (Open user report on
    header, thinking indicator, and agent list.)
[^5]: m3kka. (2026, July 10). [TUI model badge for background subagents shows the session model, not the resolved override (Issue #76343)](https://github.com/anthropics/claude-code/issues/76343).
    Anthropic/Claude Code. Verified accessible (live fetch) 2026-08-06. (Open user report with
    JSONL model-count ground truth.)
[^6]: owenarden. (2026, August 2). [Subagent spinner shows the session's effort level, not the subagent's (Issue #83252)](https://github.com/anthropics/claude-code/issues/83252).
    Anthropic/Claude Code. Verified accessible (live fetch) 2026-08-06. (Open user report with
    2.1.220 reproduction and reporter-authored bundle analysis.)
[^7]: Anthropic. (2026, August 4). [Claude Code v2.1.222 release notes](https://github.com/anthropics/claude-code/releases/tag/v2.1.222).
    GitHub. Verified accessible (live fetch) 2026-08-06. (Official release note; spinner effort,
    worktree isolation, and model-family fixes.)
[^8]: shahadil1. (2026, August 1). [Agents list renders garbled after returning from an agent's detail view; window resize required to repaint (Issue #83045)](https://github.com/anthropics/claude-code/issues/83045).
    Anthropic/Claude Code. Verified accessible (live fetch) 2026-08-06. (Open adjacent repaint
    report on 2.1.220/Linux over SSH.)
[^9]: TheMizeGuy. (2026, July 5). [Workflows/agents view hydration relaunch can attach the TUI to a stale session under tmux (Issue #74403)](https://github.com/anthropics/claude-code/issues/74403).
    Anthropic/Claude Code. Verified accessible (live fetch) 2026-08-06. (Open adjacent
    session-identity report; different agent-view/workflow surface.)
[^10]: lebaige. (2026, July 4). [Compaction artifacts: session model silently resets to default; compactor prompt attributed as a user message in subagent transcript (Issue #74114)](https://github.com/anthropics/claude-code/issues/74114).
    Anthropic/Claude Code. Verified accessible (live fetch) 2026-08-06. (Open adjacent
    transcript-content report; intermittent/observational.)
[^11]: Anthropic. (2026). [Customize your status line](https://code.claude.com/docs/en/statusline).
    Claude Code Documentation. Verified accessible (live fetch) 2026-08-06. (Global status-line
    schema, per-task subagent status lines, and blank/stale troubleshooting.)
[^12]: Anthropic. (2026). [Orchestrate teams of Claude Code sessions](https://code.claude.com/docs/en/agent-teams).
    Claude Code Documentation. Verified accessible (live fetch) 2026-08-06. (In-process versus
    split-pane behavior, model semantics, and terminal support.)
[^13]: Anthropic. (2026). [Manage multiple agents with agent view](https://code.claude.com/docs/en/agent-view).
    Claude Code Documentation. Verified accessible (live fetch) 2026-08-06. (Background-session
    supervisor, transcript warm-up, resume, and recovery behavior.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** codex-research-2026-08-06
**Model:** GPT-5 Codex
**Date:** 2026-08-06

````text
You are researching for a technical documentation record, not answering a casual question. Write
findings directly into the target file at the location the wrapper designates; do not print a
separate chat-facing summary.

## Research question

Is there a known, previously reported bug or limitation in Anthropic's Claude Code CLI where
selecting/switching to a background sub-agent's transcript pane in the TUI (the multi-agent
switcher, e.g. the interface shown when a session has launched named background agents via the
`Agent` tool / Claude Code's agent-teams feature) renders incorrect, stale, or parent-session
content instead of that sub-agent's own live transcript — specifically:

1. The sub-agent's transcript pane showing text that belongs to the *parent* orchestrating
   session (e.g. the tail of a long prompt/instruction block also present in the parent's own
   context) rather than the sub-agent's own conversation.
2. The bottom status bar / info line in that pane reporting the *parent* session's model
   (e.g. "Sonnet 5") and current working directory instead of the sub-agent's actual resolved
   model (e.g. a different model tier such as Opus or a named custom persona/model alias) and its
   own working directory (which may differ if the sub-agent was launched with worktree isolation
   or a different `-C`/cwd).
3. Whether this is consistently reproducible, intermittent, or tied to specific conditions
   (elapsed run time, token volume, number of queued inbound messages to the sub-agent, terminal
   emulator, tmux/multiplexer usage, Claude Code version, or multi-pane/multi-session setups).

## Background — anchor context (a floor, not the full scope of what to investigate)

This is being researched because of a firsthand incident: a background sub-agent launched via the
`Agent` tool (a distinct model tier from the parent session) showed, when selected in the agent
switcher, a status bar reading the *parent* session's model and cwd rather than its own — while
separately, on the same machine, a comparable background sub-agent in a *different* concurrent CC
session rendered correctly. This suggests the bug (if it is one) may be state-, timing-, or
session-specific rather than universal. Also relevant as prior anecdotal context (not to be
over-weighted, and not assumed to be the same root cause): general reports of Claude Code TUI
elements (e.g. the custom statusline feature) intermittently failing to render or going blank
until the terminal pane receives focus/interaction.

## Scope note — questions, examples, and named references are a starting point, not a checklist

The questions, topics, and named examples below are illustrative anchors and a FLOOR for this
research — not an exhaustive list to answer only or evaluate only. Reason independently: survey the
landscape broadly, follow the evidence where it leads, expand scope where warranted, and surface
relevant work, factors, and failure modes not named here. Actively resist answering only the listed
questions or evaluating only the named approaches — an output that merely fills in the listed items
has NOT met the research goal.

## Independent exploration (gaps, blindspots, emergent threads) — required

Treat the question list as a FLOOR, not a ceiling. As you research, actively surface what this
framing may be missing and pursue each promising thread to a logical conclusion:
- Adjacent or upstream factors the questions don't capture (e.g. how Claude Code's TUI renders
  multiple concurrent Ink/React component trees, how it multiplexes terminal output across
  sessions, whether background agents share a render loop or process with the parent).
- Contrarian / disconfirming evidence — report it even when it challenges the premise (e.g.
  evidence this is user/terminal-emulator misconfiguration rather than a Claude Code defect).
- Emerging 2025-2026 practices, tools, or research not anchored by the named examples.
- Known failure modes and second-order effects (stale terminal buffers, tmux pane redraw bugs,
  ANSI escape-sequence handling issues in specific terminal emulators, race conditions between
  a background agent's own render state and the foreground session's).
Whenever a load-bearing thread surfaces mid-research, follow it to its conclusion and report it in a
dedicated "Gaps, blindspots & emergent findings" subsection. Explicitly NAME any blindspot you
suspect but cannot resolve (and why) rather than omitting it. Anchor bias — over-fitting to the
listed questions and example approaches — is a known failure mode; counter it deliberately and say
where you did.

## What to look for specifically

- GitHub issues/discussions in `anthropics/claude-code` (and any related repos) describing this
  symptom or closely adjacent ones (multi-agent/sub-agent transcript viewing, agent switcher UI,
  status bar showing wrong session info, stale terminal panes).
- Claude Code release notes / changelog entries mentioning fixes to the agent switcher, background
  agent transcript rendering, or status bar binding.
- Community reports (forums, Reddit, X/Twitter threads, blog posts) describing the same or a
  similar symptom with Claude Code's background/teams agent feature.
- Whether this is a known limitation the product explicitly does not support yet (e.g. background
  sub-agents may not have a fully supported live transcript view at all, and what's rendered is
  effectively a placeholder or a reused UI component that hasn't been updated for the selected
  agent).
- Any documented workaround (re-selecting the agent, resizing the terminal, `ctrl+t` panel
  toggles, upgrading to a specific version, disabling a specific experimental flag).

## Retrieval requirement (mandatory)

For each load-bearing claim, you must make at least one live web-search or web-fetch call and
must not return [VERIFIABLE] without a URL you actually fetched in this session. Do not stop
after one round. If no source is found for a sub-question, run additional searches across
alternate framings (e.g. "Claude Code sub-agent transcript wrong model", "Claude Code agent
switcher stale pane", "Claude Code background agent status bar bug", "anthropics/claude-code
issue teams transcript render") before tagging it [NO SOURCE] — that tag is reserved for genuine
evidence absence, not retrieval avoidance. If, after a genuine multi-angle search effort, no
public report of this exact symptom exists, say so plainly and explicitly — that absence is itself
a useful finding (it may mean the feature is too new/niche for public bug reports yet, or that
this is a fleet-local/environment-specific issue rather than a general Claude Code defect) and
should not be papered over with vague hedging.

<grounding_instructions>
You are a principal engineer who has built and personally debugged production-grade terminal UI
tooling — multiplexed session managers, tmux-integrated dashboards, Ink/React-based CLI renderers,
and multi-pane agent-orchestration interfaces. You have directly triaged rendering bugs in complex
ANSI/terminal-based interfaces (stale buffers, race conditions between concurrent render trees,
status-bar state binding to the wrong underlying session) and you have strong, evidence-backed
opinions about what such symptoms typically indicate. When you cannot find a source, you say so
explicitly rather than speculating as if you had one.

Temporal scope: Weight sources by recency — 2026 (primary) → 2025 → 2024. Pre-2024 sources are
background context only unless foundational to the topic (this feature — background/teams
sub-agents in Claude Code — did not exist before 2025, so pre-2025 sources are unlikely to be
relevant at all). If post-2024 literature is genuinely sparse for a subtopic, state
"[subtopic]: no significant post-2024 developments found" rather than backfilling with older
sources. Backfilling is a failure mode, not a hedge.

Before generating your final output, execute a Chain-of-Verification (CoVe) to ensure factual
fidelity over compliance.

Inside your thought process:
1. Isolate the core facts required.
2. Draft a tentative response.
3. Hostile Cross-Examination: flag any claim where you are citing a source because the prompt
   implied you should, rather than because you verified it.
4. Strip away any claim that cannot be empirically verified.

When generating your final output, classify every major claim. Write your rationale before
appending the tag — writing the tag first causes post-hoc rationalization. Rationale → evidence
check → tag.

- [VERIFIABLE]: backed by documentation, peer-reviewed research, or official tech blogs
  (2024-2026). Carry an inline footnote ref to the source: [VERIFIABLE][^N].
- [HEURISTIC]: widely accepted best practice without a specific citation.
- [INFERENCE]: a logical conclusion drawn from context. Provide your reasoning in-text. Do not
  fabricate a source. Tier tag only — NO footnote ref.
- [NO SOURCE]: explicitly state when you cannot find verifiable data. Tier tag only — NO
  footnote ref.

Citation format (mandatory for every externally-sourced claim):
- Inline: append the GFM footnote ref directly after the tier tag — [VERIFIABLE][^N]. A claim
  citing multiple sources carries ascending separate refs — [VERIFIABLE][^3][^7] (never grouped
  [^3,^7]; never out of order).
- Footnote definitions live once, under ## Sources, in APA form with a clickable URL or DOI and
  an access-verification stamp. Worked example:
    [^1]: LangChain. (2026). [Threads](https://docs.langchain.com/langsmith/threads).
    LangSmith Documentation. Verified accessible (HTTP 200) 2026-06-03. (Scope note.)
- URL-or-DOI ALWAYS: every source entry carries a clickable URL or a doi.org link —
  paywalled/gated is fine (link it anyway; stamp the access status). Only the truly-irreducible
  case (no online catalog presence anywhere) gets an explicit [no online source located] marker
  with a one-line justification.
- Integrity: footnote refs are contiguous from [^1], every [^N] ref has a matching definition,
  and every definition is referenced — no gaps, no orphans.
- [INFERENCE] / [NO SOURCE] claims carry the tier tag with NO footnote ref.

Hard constraint (overrides all formatting preferences): never invent a citation to satisfy a
formatting instruction. Accuracy > completeness.

Format diagrams using Mermaid.js or ASCII. Format math using LaTeX. NEVER generate binary images.
</grounding_instructions>

## Required document structure

Write directly into the target file, preserving its existing frontmatter and region markers
(`<!-- doc:region ... -->` / `<!-- /doc:region ... -->` comments — do not delete or rename them).
Populate, in order: `## Context` is already frozen/immutable — do not edit it, only read it for
framing. Replace the `(synthesis will populate this region)` body placeholder with topic sections
(numbered `## 1.`, `## 2.`, etc.) covering your findings, a `## Comparison` section only if there
turn out to be multiple candidate root causes/explanations worth comparing side by side (numbered
`#` column required if so), a `## Recommendation` section (what, if anything, should be filed as a
bug report or watched for in a future Claude Code release), a `## Gaps, blindspots & emergent
findings` subsection per the independent-exploration requirement above, an `## Open Questions`
section, and a `## Sources` section with the GFM footnote definitions in APA form as specified
above. Set frontmatter `status: complete` when done. Update the Table of Contents region to list
every real `##`/`###` heading you add, with GitHub-style anchors, so it stays accurate.

Also append, after your findings, this exact appendix block (fill in the model you actually ran
as and today's date):

## Appendix: Research Prompt

**Registry ID:** codex-research-2026-08-06
**Model:** [model actually used]
**Date:** 2026-08-06

```text
[paste this entire prompt back verbatim]
```
````

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

(none yet — auto-populated at commit from the validated provenance ledger)

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

<!-- /doc:region name="run_history" -->
