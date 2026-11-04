---
title: "Claude Code Statusline Best Practices"
category: research
tags: [research, statusline, claude-code, developer-experience, terminal-ui]
status: complete
source: "gpt-5.6-sol-high-2026-08-15"
template_version: "research-1.2.0"
---

# Claude Code Statusline Best Practices

**Status:** complete

**Created:** 2026-07-17

<!-- Optional fields:
**Task:** ISSUE-REF
-->

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. The statusline mechanism — contract, payload, cadence, performance](#1-the-statusline-mechanism--contract-payload-cadence-performance)
  - [When to use](#when-to-use)
- [2. What to display — high-signal vs vanity segments](#2-what-to-display--high-signal-vs-vanity-segments)
  - [When to use](#when-to-use-2)
- [3. Community tooling and examples](#3-community-tooling-and-examples)
  - [When to use](#when-to-use-3)
- [4. Cross-domain best practices — shell prompts and status bars](#4-cross-domain-best-practices--shell-prompts-and-status-bars)
  - [When to use](#when-to-use-4)
- [5. Agentic-coding-specific value — context, compaction, quota](#5-agentic-coding-specific-value--context-compaction-quota)
  - [When to use](#when-to-use-5)
- [6. Failure modes and anti-patterns](#6-failure-modes-and-anti-patterns)
  - [Configuration edits: `statusLine.command` hot-reloads](#configuration-edits-statuslinecommand-hot-reloads)
- [Gaps, blindspots & emergent findings](#gaps-blindspots--emergent-findings)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

<!-- doc:region name="context" kind="immutable" -->

## Context

Survey what experienced developers, OSS projects, and the Claude Code (CC) community
actually put in their CC statuslines, and distill evidence-backed best practices — so
ai-harness can decide what (if anything) to add, drop, or reorder in its own statusline
(`.claude/statusline-command.sh`). The current ai-harness statusline carries: model name,
a Fable/model indicator, usage-quota percentages (session 5h, week all-models, week Fable),
a weekly-reset datetime (`wk↻ Day M/D HH:MM`), and rotating tips; a separate context-%
signal (`ctx-pct`) exists in the ecosystem. This is a FLOOR to react to, not the whole scope.

**Primary period:** 2025–2026
**Source weighting:** 2026 primary → 2025 secondary → pre-2024 background only (shell-prompt
foundations broadened where warranted, since starship/powerlevel10k predate CC).

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

Primary focus: 2026 and late-2025 sources. Claude Code and its statusline feature are a
2025–2026 phenomenon, so nearly all CC-specific evidence is in-window. Cross-domain
shell-prompt best practices (starship, powerlevel10k) draw on foundational material from
2022–2025; that window is deliberately broadened because the design principles predate CC
and remain load-bearing. No significant pre-2024 CC-statusline literature exists (the feature
did not); backfilling with older CC material was not attempted.

## Executive Summary

1. **The statusline is an event-driven shell script fed a rich JSON payload on stdin, debounced at
   300 ms, cancelled in-flight on the next update, and run locally with zero token cost.** [VERIFIABLE][^1]
   Because it re-runs constantly during active sessions, **latency is the primary design constraint** —
   slow commands (`git status` in a large repo, any network call) visibly degrade the UI. [VERIFIABLE][^1]
2. **The winning 2026 consensus is minimal, not dense: five well-chosen high-signal segments beat ten.**
   Context %, model, git branch, and cost/quota burn are the segments practitioners keep; cumulative
   token counts, session duration, and lines-changed are widely treated as vanity. [VERIFIABLE][^4]
3. **Community tooling is mature and battle-tested** — `ccstatusline` (11.8k GitHub stars, ~40 widgets,
   powerline/themes) and `ccusage` (burn-rate + billing-block projection) — but both enforce the same
   performance discipline: git-command caching with a TTL, and offline/cached data over network calls. [VERIFIABLE][^2][^3]
4. **Cross-domain prompt engineering transfers directly**: conditional/contextual segments (show only
   when relevant), strict per-command timeouts (starship defaults `scan_timeout=30 ms`,
   `command_timeout=500 ms`), and async "show last-known value, recompute in background" rendering
   (powerlevel10k). [VERIFIABLE][^5][^6]
5. **The single highest-signal agentic-specific feature is context-% measured against the ~77% auto-compact
   threshold, not the raw window** — plus quota/weekly-reset burn awareness. CC "has no way of knowing how
   much context it is currently consuming"; the statusline is the only automatic surface for that signal,
   which makes "distance to auto-compact" more actionable than raw `used_percentage`. [VERIFIABLE][^7]

## 1. The statusline mechanism — contract, payload, cadence, performance

**Configuration contract.** The statusline is configured in `settings.json` under a `statusLine`
key with `type: "command"` and a `command` pointing to a script or inline shell command; optional
`padding` (default `0`) and `refreshInterval` (minimum `1` second, for time-based data or idle
subagent work) fields tune it. [VERIFIABLE][^1] CC also exposes `hideVimModeIndicator` and a
separate `subagentStatusLine` for per-subagent rows. [VERIFIABLE][^1]

**The JSON payload (stdin).** CC pipes a rich JSON object to the script on stdin. The load-bearing
fields for a coding harness:

- `model.id`, `model.display_name` — current model. [VERIFIABLE][^1]
- `workspace.current_dir`, `workspace.project_dir`, `workspace.git_worktree`, `workspace.repo.{host,owner,name}` —
  location and repo identity parsed from the `origin` remote. [VERIFIABLE][^1]
- `cost.total_cost_usd` (client-side estimate, resets on `/clear`), `cost.total_duration_ms`,
  `cost.total_api_duration_ms`, `cost.total_lines_added`, `cost.total_lines_removed`. [VERIFIABLE][^1]
- `context_window.used_percentage`, `remaining_percentage`, `context_window_size` (200000 default /
  1000000 extended), `total_input_tokens`, `total_output_tokens`, and `current_usage.{input,output,cache_creation,cache_read}_tokens`.
  As of v2.1.132 these reflect *current* context, not cumulative session totals; `used_percentage` is
  computed **input-only** (input + cache-creation + cache-read, excluding output). [VERIFIABLE][^1]
- `exceeds_200k_tokens` — a fixed-threshold boolean regardless of actual window size. [VERIFIABLE][^1]
- `rate_limits.five_hour.{used_percentage,resets_at}`, `rate_limits.seven_day.{used_percentage,resets_at}` —
  **Claude.ai Pro/Max subscribers only**, present after the first API response; `resets_at` is Unix epoch seconds. [VERIFIABLE][^1]
- `effort.level`, `thinking.enabled`, `session_id`, `session_name`, `prompt_id`, `version`,
  `output_style.name`, `vim.mode`, `agent.name`, `pr.{number,url,review_state}`, and full
  `worktree.*` metadata. [VERIFIABLE][^1]

Several fields are conditionally **absent** (not null): `rate_limits` (non-subscribers), `workspace.repo`
(no origin remote), `worktree` (non-`--worktree` sessions), `effort` (models without the param). Scripts
must handle absence (`// empty` in jq) distinctly from null (`// 0`). [VERIFIABLE][^1]

**Update cadence and cancellation.** The script runs "after each new assistant message, after `/compact`
finishes, when the permission mode changes, or when vim mode toggles," with updates **debounced at 300 ms**;
"if a new update triggers while your script is still running, the in-flight execution is cancelled."
[VERIFIABLE][^1] Event triggers "go quiet when the main session is idle" (e.g. a coordinator waiting on
subagents) — the reason `refreshInterval` exists for time-based segments. [VERIFIABLE][^1]

**Performance and rendering.** The status line "runs locally and does not consume API tokens." [VERIFIABLE][^1]
Output supports multiple lines (one row per `echo`), ANSI colors, and OSC 8 clickable links (iTerm2/Kitty/WezTerm;
Terminal.app does not support them). [VERIFIABLE][^1] Width detection inside the script must read the `COLUMNS`/`LINES`
env vars (CC captures stdout, so `tput cols` cannot see the terminal), available v2.1.153+. [VERIFIABLE][^1] The docs
explicitly warn: "your script runs frequently during active sessions, so commands like `git status` can cause lag,"
and recommend caching slow operations keyed on the stable `session_id` (never `$$`/pid, which change per invocation). [VERIFIABLE][^1]

### When to use

Read the payload contract before adding any segment: prefer a pre-computed field CC already supplies
(`used_percentage`, `rate_limits.*`, `cost.*`) over shelling out. Reach for `refreshInterval` only when a
segment is genuinely time-based (a clock, a reset countdown, idle-subagent git drift) — otherwise it adds
render load for no signal.

## 2. What to display — high-signal vs vanity segments

The 2026 practitioner consensus is a **restraint-first** philosophy. The most-cited framing: "When everything
is highlighted, nothing is." [VERIFIABLE][^4] Information density has "diminishing returns," and the
recommended starting point is deliberately spare — "a single line with git branch, model name, and context
percentage will immediately improve your Claude Code experience." [VERIFIABLE][^4]

**High-signal segments (keep):**

- **Context %** — treated as the single most critical segment: "I need to know when I'm approaching limits
  before I hit them." [VERIFIABLE][^4] (See §5 for the auto-compact nuance that makes raw % only half the story.)
- **Model name / reasoning effort** — cheap, and it matters when models/effort change mid-session (CC supplies
  both `model.display_name` and `effort.level`). [VERIFIABLE][^1]
- **Git branch** — the primary context-switch indicator; putting it first tells you which codebase you are
  affecting without scanning. [HEURISTIC]
- **Cost / token burn** — the metric "that costs money" earns prominent (inverse-color) treatment; session cost
  is "the financial reality check." [VERIFIABLE][^4] For subscription users this maps to quota %, not dollars.
- **Quota + reset time** — 5h and weekly `rate_limits.used_percentage` + `resets_at`, for subscription burn awareness. [VERIFIABLE][^1]

**Vanity / low-signal segments (drop or demote):** cumulative session duration, lines-added/removed, raw
cumulative token totals, and static text the user already knows (username, host). These consume the scarce
character budget without informing a decision. [INFERENCE] The reasoning: a statusline segment earns its place
only if it changes what you do next; a number you never act on is decoration.

### When to use <!-- 2 -->

Apply a "does this change my next action?" test per segment. Context %, quota %, and cost/burn pass because
they gate compaction and spend decisions. Duration and lines-changed fail for most workflows. Git branch passes
for multi-repo/worktree work (ai-harness's exact situation).

## 3. Community tooling and examples

**`ccstatusline`** (sirmalloc, **11.8k stars, 516 forks, MIT**) is the reference implementation: ~40 widgets
across Model & Context (Model, Context Length, Context %, "Context % (usable)" accounting for auto-compact,
Context Bar), Token Metrics (input/output speed), Git (branch, status, staged/unstaged/untracked, ahead/behind,
SHA, PR/MR, worktree), Session & Usage (Session Cost, Weekly Usage, Block Timer, "Weekly Reset Timer"), Cache
Metrics (hit rate, read, write), and System (CWD, Compaction Counter, Thinking Effort, Terminal Width). [VERIFIABLE][^2]
It ships powerline separators, gradients, 16/256/truecolor, optional Nerd-Font icons, PTY-probed width detection with
flex separators, and — critically — **git-command caching under `~/.cache/ccstatusline/git-cache` with a configurable TTL**,
block-timer metrics cached with 5-hour invalidation, and `no-optional-locks` git flags to prevent `index.lock` races. [VERIFIABLE][^2]

**`ccusage`** provides a purpose-built statusline command showing active model + effort, session cost, daily total,
current 5-hour billing block with time remaining, **burn rate** ("$/hr" with color-coded thresholds — green
< 2,000 tok/min, yellow 2,000–5,000, red > 5,000), and context usage. [VERIFIABLE][^3] It "uses offline mode by
default for instant response times without network dependencies," reading session info from stdin. [VERIFIABLE][^3]

The official docs also name `ccstatusline` and `starship-claude` as recommended community projects, and ship
copy-paste examples for context bars, git-with-colors, cost/duration, clickable OSC 8 repo links, rate-limit
display, and session-id-keyed git caching. [VERIFIABLE][^1]

### When to use <!-- 3 -->

Study `ccstatusline`'s caching architecture even if not adopting it: the `~/.cache` TTL + `no-optional-locks`
pattern is the canonical answer to the "git in a big repo is slow" failure mode. Use `ccusage`'s offline-first
burn-rate approach as the model for any spend/quota segment — never a live network call in the render path.

## 4. Cross-domain best practices — shell prompts and status bars

Decades of shell-prompt engineering (starship, powerlevel10k, oh-my-posh, tmux) converge on principles that
transfer directly to CC statuslines:

**Conditional / contextual segments.** starship shows modules "only when relevant" — git modules only inside a
repo, the battery module only under 10% charge, language modules only when their files are detected. [VERIFIABLE][^5]
This is the antidote to clutter: a segment that is absent when uninformative costs zero attention.

**Strict per-command timeouts.** starship's global options default to `scan_timeout` = 30 ms ("Timeout for starship
to scan files") and `command_timeout` = 500 ms ("Timeout for commands executed by starship"). [VERIFIABLE][^5]
The lesson for CC: bound every shelled-out command; never let one slow call stall the render.

**Async / last-known-value rendering.** powerlevel10k's approach when git status is slow: "Powerlevel10k displays
the last known Git status in grey and continues to compute up-to-date Git status in the background." [VERIFIABLE][^6]
Its Instant Prompt "can remove Zsh startup lag even if it's not caused by a theme," letting users "start typing
while plugins are still loading." [VERIFIABLE][^6] CC's own model is different (it *cancels* in-flight renders on
the next update rather than backgrounding them), so the transferable idea is the **cache-and-show-stale** pattern
(ccstatusline / the official caching example) rather than true async. [INFERENCE]

**Width budgeting and truncation.** The official docs and community both stress short output — "the status bar has
limited width, so long output may get truncated or wrap awkwardly." [VERIFIABLE][^1] Use symbols/colored indicators
over words ("three-bar indicators instead of text labels") to pack signal into few characters. [HEURISTIC]

### When to use <!-- 4 -->

Adopt conditional rendering aggressively: a worktree segment only in worktrees, a PR segment only when `pr` is
present, quota only when `rate_limits` exists. Bound any `git`/external call with a timeout and a cache. Treat
width as a hard budget on narrow terminals.

## 5. Agentic-coding-specific value — context, compaction, quota

This is where a coding-agent statusline diverges from a shell prompt, and where the highest signal lives.

**Distance to auto-compact beats raw context %.** CC automatically compacts when context "exceeds approximately
77%," reserving a ~22.5% buffer of a 200k window. [VERIFIABLE][^7] Crucially, "Claude Code itself has no way of
knowing how much context it is currently consuming," and the statusline is "the only place where context usage is
automatically available." [VERIFIABLE][^7] The raw `used_percentage` (e.g. 75%) and the "context left until
auto-compact" (e.g. ~2%) are **different numbers**; surfacing the *headroom-to-threshold* is more actionable than
the raw fill, because auto-compaction mid-implementation risks quality degradation. [VERIFIABLE][^7] Community
context-warning systems escalate by band (NOTICE 60–69% → WARNING 70–74% → CRITICAL 75%+). [VERIFIABLE][^7]

**Quota / weekly-reset burn.** For subscription users the money-equivalent signal is `rate_limits` (5h + 7d
`used_percentage`) and the `resets_at` epoch. [VERIFIABLE][^1] A weekly-reset datetime and quota bars — exactly
what ai-harness already surfaces, and what `ccstatusline`'s "Weekly Reset Timer" / `ccusage`'s billing block also
target — let a user pace work against a hard weekly cap. [VERIFIABLE][^2][^3]

**Session distinction across parallel work.** `session_id` / `session_name`, worktree name, and repo identity let
a multi-session/multi-worktree operator (ai-harness's `sw-N` model) tell windows apart at a glance. [VERIFIABLE][^1]

### When to use <!-- 5 -->

For any agentic harness, prioritize context-headroom-to-compaction and quota-burn over generic shell segments.
These are the two signals that gate the two most consequential agentic decisions: *when to compact* and *when to
stop / pace spend*.

## 6. Failure modes and anti-patterns

- **Slow commands in the render path.** `git status`/`git diff` in large repos, or any network call, cause
  per-keystroke lag; the in-flight script is cancelled on the next update, so a slow script can mean the line
  never updates. Cache keyed on `session_id`. [VERIFIABLE][^1]
- **The render fork-bomb (observed in ai-harness).** On slow platforms (Windows Git-Bash) a render can outlast the
  interval to the next invocation; without a single-flight guard, orphaned slow renders accumulate and peg the CPU
  (ai-harness's `.claude/statusline-command.sh` carries an explicit per-session lock guard for exactly this — a
  documented 2026-07-02 incident of 300+ stuck procs). [VERIFIABLE][^8]
- **Over-crowding.** "When everything is highlighted, nothing is"; density has diminishing returns. [VERIFIABLE][^4]
- **Escape-sequence glitches.** Complex ANSI/OSC 8 sequences can garble output when they overlap other UI updates;
  multi-line + escape codes are more prone to rendering issues than single-line plain text. Use `printf '%b'` over
  `echo -e` for reliable escapes. [VERIFIABLE][^1]
- **Terminal/emulator incompatibility.** OSC 8 links are silently non-clickable in Terminal.app; SSH/tmux may strip
  OSC sequences; Windows Terminal often needs `FORCE_HYPERLINK=1`. [VERIFIABLE][^1]
- **Stale / misleading context math.** A naive context bar that ignores the auto-compact buffer misreports urgency;
  hard-coding the buffer breaks when `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is customized. [VERIFIABLE][^7]
- **Blank line on error.** Scripts that exit non-zero or print nothing blank the status line; workspace-trust must
  be accepted or the line is skipped entirely (`statusline skipped · restart to fix`). [VERIFIABLE][^1]
- **Right-side notification collisions.** System notifications (MCP errors, auto-update, context-low warnings) and
  the verbose-mode token counter share the same row and can truncate the line on narrow terminals. [VERIFIABLE][^1]

### Configuration edits: `statusLine.command` hot-reloads

**A full exit and relaunch is not required for an ordinary edit to `statusLine.command`.** The current official
setup walkthrough says, immediately after instructing the user to add the `statusLine` object (including its
`command`) to `settings.json`, that settings reload automatically and the result appears after the next interaction
with Claude Code. Therefore, correcting a broken command path on disk during a running session should take effect
on the next interaction-triggered statusline update. [VERIFIABLE][^1]

This is configuration reload, not output refresh: `refreshInterval` only supplies additional timer-based executions
of the currently configured command, while ordinary updates occur on documented UI/session events. Editing the
script file itself is likewise observed on the next update trigger because each invocation executes the script
again. [VERIFIABLE][^1] The changelog independently confirms that Claude Code has a settings hot-reload mechanism;
v2.1.140 fixed that mechanism failing to detect edits when `~/.claude/settings.json` was symlinked. [VERIFIABLE][^9]
The public evidence establishes automatic settings reload but does not document whether the statusline renderer
re-reads `settings.json` on every render tick; the changelog evidence is more consistent with a file-watcher-driven
settings update followed by a normal render trigger. [INFERENCE]

The workspace-trust message `statusline skipped · restart to fix` is a separate exception: it concerns trust not
being accepted when the session started, not a stale or cached `statusLine.command` value. [VERIFIABLE][^1]
Targeted searches of `anthropics/claude-code` issues and discussions found no report that directly contradicts or
independently reproduces the documented command-string reload behavior. [NO SOURCE]

## Gaps, blindspots & emergent findings

- **Emergent: `used_percentage` is input-only and window-relative, not compaction-relative.** The official field
  excludes output tokens and measures against the full window, while the *actionable* threshold is ~77%. A harness
  that displays raw `used_percentage` is technically correct but strategically misleading about compaction urgency —
  the strongest single finding of this research, and directly relevant to the ecosystem's `ctx-pct` signal. [VERIFIABLE][^1][^7]
- **Contrarian, and it won:** the premise "survey what people put in their statuslines" implicitly rewards density,
  but the evidence favors *subtraction*. The best-regarded 2026 setups start at three segments and add reluctantly. [VERIFIABLE][^4]
- **Blindspot named (unresolved): no controlled study.** All "high-signal vs vanity" claims are practitioner opinion
  and tool-design choices, not measured (e.g. no eye-tracking or task-completion study of statusline density). The
  consensus is strong and convergent but remains [HEURISTIC] at root; I could not find empirical DX research on
  statusline segment selection. [NO SOURCE]
- **Blindspot named (unresolved): CC-internal render-cost budget.** CC documents debounce (300 ms) and in-flight
  cancellation but publishes no target render-time budget (starship's "< ~200 ms feels instant" is the nearest
  cross-domain proxy, itself a heuristic, not a CC number). The true cost of a heavy statusline on CC's UI thread
  is undocumented. [NO SOURCE]
- **Emergent: subagent statuslines are a distinct, newer surface.** `subagentStatusLine` (per-task rows with
  `tokenCount`, `contextWindowSize`, `model`, v2.1.205+) is a 2026 addition few community tools cover yet — an
  opportunity area for a coordinator-heavy harness. [VERIFIABLE][^1]
- **Contrarian on multi-line:** the docs support multi-line output and the highest-signal community setups use 2–4
  lines, but the docs also warn multi-line + escape codes are more glitch-prone. The tradeoff (more signal vs more
  fragility) is unresolved and terminal-dependent. [VERIFIABLE][^1]

## Comparison

| # | Criterion | Minimal (3-segment) | Dense (ccstatusline-style) | ai-harness current |
|---|-----------|---------------------|----------------------------|--------------------|
| 1 | Segments | branch, model, ctx% | ~40 available, 8–12 typical | model, Fable ind., 3 quota %, wk-reset, tips |
| 2 | Primary signal | context awareness | everything, opt-in | quota/subscription burn |
| 3 | Glanceability | high | medium (needs discipline) | medium |
| 4 | Perf risk | low | medium (mitigated by caching) | low (single-flight guard present) |
| 5 | Compaction-relative ctx | manual | "Context % (usable)" widget | separate `ctx-pct` signal |
| 6 | Setup cost | trivial | TUI wizard | bespoke script |
| 7 | Best fit | solo, single repo | power users, tuned | multi-session subscription ops |

## Recommendation

For ai-harness specifically, the evidence supports **keeping the quota-first orientation** (it matches the
subscription-burn use case and is already well-built with a single-flight guard), while making three evidence-backed
moves:

1. **Elevate context-headroom-to-auto-compact to a first-class, always-visible segment** — not raw `used_percentage`,
   but distance to the ~77% threshold, with NOTICE/WARNING/CRITICAL banding. This is the highest-signal agentic
   segment and the current statusline delegates it to a separate `ctx-pct` command rather than surfacing it inline. [VERIFIABLE][^4][^7]
2. **Add a conditional git-branch/worktree segment** (present only in a repo/worktree), placed early. ai-harness runs
   a multi-worktree `sw-N` model where "which codebase am I affecting" is a real, frequent question; CC supplies
   `workspace.git_worktree` and `worktree.*` for free, and the branch-first convention is the community default. [VERIFIABLE][^1] [HEURISTIC]
3. **Audit the tip rotation against the density principle.** Rotating tips are the segment most exposed to the
   "when everything is highlighted, nothing is" critique — verify they are demoted (dim, right-most, or truncated
   first) so they never crowd out the quota/context signals, especially on narrow terminals where the right-side
   notification area already competes for space. [VERIFIABLE][^1][^4]

Cross-cutting: keep every external call cached and timeout-bounded (already partly done via the single-flight guard;
adopt ccstatusline's `session_id`-keyed TTL cache pattern for any git call), and lean on **conditional rendering** so
absent-when-uninformative segments cost zero attention. Do **not** add duration, lines-changed, or cumulative-token
segments — they are the canonical vanity metrics. [VERIFIABLE][^1][^2]

## Open Questions

1. Does CC publish (or can we measure) an actual render-time budget for the statusline on the UI thread, beyond the
   300 ms debounce, so "keep it fast" can be quantified rather than asserted?
2. Should ai-harness fold its separate `ctx-pct` signal directly into the statusline (single surface), or keep it
   as an on-demand command — what is the token/attention tradeoff of always-on vs pull?
3. Is the ~77% auto-compact threshold stable across CC versions and the `CLAUDE_CODE_AUTO_COMPACT_WINDOW` override,
   and should the statusline read that env var rather than hard-code the buffer?
4. Is there measurable value in a `subagentStatusLine` for the coordinator-heavy `sw-N` workflow, given few community
   tools have adopted it yet?

## Sources

[^1]: Anthropic. (2026). [Customize your status line](https://code.claude.com/docs/en/statusline). Claude Code Documentation. Verified accessible (HTTP 200) 2026-08-15. (Primary source: automatic settings reload, full JSON payload contract, cadence/debounce, performance guidance, examples, troubleshooting.)
[^2]: sirmalloc. (2026). [ccstatusline: Beautiful highly customizable statusline for Claude Code CLI](https://github.com/sirmalloc/ccstatusline). GitHub. Verified accessible (HTTP 200) 2026-07-17. (Widget catalog, powerline/theme support, git-cache TTL, 11.8k stars.)
[^3]: ccusage. (2026). [Statusline Integration Guide](https://ccusage.com/guide/statusline). ccusage Documentation. Verified accessible (HTTP 200) 2026-07-17. (Burn-rate thresholds, billing block, offline-mode performance.)
[^4]: Eftimie, O. (2026). [Claude Code status lines that actually matter](https://ovidiueftimie.substack.com/p/claude-code-status-lines-that-actually). Substack. Verified accessible (HTTP 200) 2026-07-17. (High-signal vs vanity opinion; "when everything is highlighted, nothing is"; minimal-first advice.)
[^5]: Starship. (2026). [Configuration — Prompt options](https://starship.rs/config/). Starship Cross-Shell Prompt. Verified accessible (HTTP 200) 2026-07-17. (scan_timeout=30ms and command_timeout=500ms defaults; conditional module display.)
[^6]: romkatv. (2026). [Powerlevel10k — A Zsh theme](https://github.com/romkatv/powerlevel10k). GitHub. Verified accessible (HTTP 200) 2026-07-17. (Instant Prompt; last-known-Git-status-in-grey background recompute pattern.)
[^7]: trust_delta. (2026). [Preventing Automatic Compacting in Claude Code: Building a Context Warning System with Hooks and Statusline](https://zenn.dev/trust_delta/articles/claude-code-context-warning-001?locale=en). Zenn. Verified accessible (HTTP 200) 2026-07-17. (~77% auto-compact threshold / 22.5% buffer; statusline as the only automatic context surface; NOTICE/WARNING/CRITICAL banding.)
[^8]: ai-harness. (2026). [.claude/statusline-command.sh](https://github.com/) — local repository file, `<workspace>/repository/.claude/statusline-command.sh`. Verified accessible (local file read) 2026-07-17. (Single-flight guard rationale; documented 2026-07-02 fork-bomb incident of 300+ stuck statusline processes. [no online source located] — internal repository file; cited as first-party evidence of the failure mode.)
[^9]: Anthropic. (2026). [Claude Code changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md). GitHub. Verified accessible (HTTP 200) 2026-08-15. (Release 2.1.140: settings hot-reload fix for symlinked `~/.claude/settings.json`.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** R-adhoc (research-doc skill, no aido registry)
**Model:** opus researcher (Claude ad-hoc web, research-doc skill)
**Date:** 2026-07-17

```text
You are producing a template-conformant research document on Claude Code (CC) statusline
best practices for the ai-harness repo.

RESEARCH GOAL
Survey what experienced developers, OSS projects, and the Claude Code community actually put
in their CC statuslines, and distill evidence-backed best practices — so ai-harness can decide
what (if anything) to add, drop, or reorder in its own statusline (.claude/statusline-command.sh).

Current ai-harness statusline segments (a FLOOR to react to, not the whole scope): model name,
a Fable/model indicator, usage-quota percentages (current session 5h, current week all-models,
current week Fable), a weekly-reset datetime (wk↻ Day M/D HH:MM), and rotating tips. Also
relevant: a separate context-% (ctx-pct) self-awareness signal exists in the ecosystem.

RESEARCH QUESTIONS (a FLOOR, not a checklist — expand freely):
1. The CC statusline mechanism itself: the statusLine settings command contract, the JSON
   payload fields CC passes to the script on stdin (model, workspace/cwd, git, cost/session data,
   output style, version, etc.), refresh cadence, and the latency/performance implications.
2. What people actually display: model, context-window %, git branch/status, cwd/project,
   session cost + burn rate, token usage, subscription/plan quota + rate-limit reset times,
   time/clock, output style, lines-changed, custom project state. Which are high-signal vs vanity?
3. Community tooling + examples: ccstatusline, cc-statusline (npm), awesome-claude-code lists,
   popular dotfiles, blog posts, GitHub gists, and any Anthropic official guidance/docs.
4. Cross-domain best practices from shell prompts / status bars (starship, powerlevel10k, tmux
   status, oh-my-posh): segment selection, signal density vs noise, color/ANSI/Nerd-Font usage,
   width budgeting + truncation, conditional/contextual segments, async rendering.
5. Agentic-coding-specific value: context-% exhaustion warnings, cost/quota burn, weekly-reset
   awareness, when-to-compact signals.
6. Failure modes + anti-patterns: commands that block/slow rendering, flicker, terminal/emulator
   compatibility, over-crowding, stale data, clipboard/focus interactions.

## Scope note — questions, examples, and named references are a starting point, not a checklist
The questions, topics, and named examples above are illustrative anchors and a FLOOR for this
research — not an exhaustive list to answer only or evaluate only. Reason independently: survey
the landscape broadly, follow the evidence where it leads, expand scope where warranted, and
surface relevant work, factors, and failure modes not named here.

## Independent exploration (gaps, blindspots, emergent threads) — required
Treat the question list as a FLOOR, not a ceiling. Surface adjacent/upstream factors, contrarian/
disconfirming evidence (e.g. "a minimal statusline beats a dense one"), emerging 2025–2026
practices, and known failure modes / second-order effects. Report load-bearing threads in a
dedicated "Gaps, blindspots & emergent findings" subsection. Name any blindspot you suspect but
cannot resolve. Anchor bias is a known failure mode; counter it deliberately.

RETRIEVAL ENFORCEMENT (mandatory):
- For each load-bearing claim, run at least one WebSearch or WebFetch; do NOT tag a claim
  [VERIFIABLE] without a URL you actually fetched in-session and recorded in the provenance ledger.
- Do not stop after one round. If no source is found, run additional searches across alternate
  framings before tagging [NO SOURCE].
- Live-check every citation (WebFetch it); apply the failure taxonomy for any unreachable source.

<grounding_instructions>
You are a principal developer-experience / terminal-tooling engineer who has built and tuned CLI
status lines, shell prompts (starship, powerlevel10k, oh-my-posh), and tmux status bars in
production, and who has configured agentic-coding-tool UIs (Claude Code, etc.). You have strong
opinions backed by evidence. When you cannot find a source, you say so explicitly.

Temporal scope: Weight sources by recency — 2026 (primary) → 2025 → 2024. Pre-2024 sources are
background context only unless foundational to the topic. If post-2024 literature is genuinely
sparse for a subtopic, state "[subtopic]: no significant post-2024 developments found" rather
than backfilling with older sources. Backfilling is a failure mode, not a hedge. (Claude Code and
its statusline feature are 2025–2026; shell-prompt best practices may draw on older foundational
material — broaden the window there where warranted.)

Before generating your final output, execute a Chain-of-Verification (CoVe) to ensure factual
fidelity over compliance. Inside your thought process: 1. Isolate the core facts required. 2. Draft
a tentative response. 3. Hostile Cross-Examination: flag any claim where you are citing a source
because the prompt implied you should, rather than because you verified it. 4. Strip away any claim
that cannot be empirically verified.

When generating your final output, classify every major claim. Write your rationale before
appending the tag — writing the tag first causes post-hoc rationalization. Rationale → evidence
check → tag.
- [VERIFIABLE]: backed by documentation, peer-reviewed research, or official tech blogs (2024–2026).
  Carry an inline footnote ref to the source: [VERIFIABLE][^N].
- [HEURISTIC]: widely accepted best practice without a specific citation.
- [INFERENCE]: a logical conclusion drawn from context. Provide reasoning in-text. No source, no ref.
- [NO SOURCE]: explicitly state when you cannot find verifiable data. Tier tag only — no ref.

Citation format: inline GFM footnote ref directly after the tier tag — [VERIFIABLE][^N]; multiple
sources carry ascending separate refs — [VERIFIABLE][^3][^7]. Footnote definitions live once, under
## Sources, in APA form with a clickable URL or DOI and an access-verification stamp. URL-or-DOI
ALWAYS. Contiguous from [^1], no gaps, no orphans. [INFERENCE]/[NO SOURCE] carry no footnote ref.

Hard constraint (overrides all formatting preferences): never invent a citation to satisfy a
formatting instruction. Accuracy > completeness.

Format diagrams using Mermaid.js or ASCII. Format math using LaTeX. NEVER generate binary images.
</grounding_instructions>
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

| Claim | Source URL | Verbatim quote (from fetched page) | Verdict | Live? |
|---|---|---|---|---|
| Updates debounced at 300 ms; in-flight render cancelled on next update | https://code.claude.com/docs/en/statusline | "Updates are debounced at 300ms, meaning rapid changes batch together and your script runs once things settle. If a new update triggers while your script is still running, the in-flight execution is cancelled." | SUPPORTED | yes |
| Runs locally, no API tokens | https://code.claude.com/docs/en/statusline | "The status line runs locally and does not consume API tokens." | SUPPORTED | yes |
| used_percentage is input-only | https://code.claude.com/docs/en/statusline | "The `used_percentage` field is calculated from input tokens only: `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`. It does not include `output_tokens`." | SUPPORTED | yes |
| rate_limits present only for Pro/Max subscribers after first API response | https://code.claude.com/docs/en/statusline | "`rate_limits`: appears only for Claude.ai subscribers (Pro/Max) after the first API response in the session." | SUPPORTED | yes |
| Cache slow ops keyed on session_id, not pid | https://code.claude.com/docs/en/statusline | "Process-based identifiers like `$$`, `os.getpid()`, or `process.pid` change on every invocation and defeat the cache. Use the `session_id` from the JSON input instead" | SUPPORTED | yes |
| Restraint beats density | https://ovidiueftimie.substack.com/p/claude-code-status-lines-that-actually | "When everything is highlighted, nothing is" | SUPPORTED | yes |
| Minimal starting point | https://ovidiueftimie.substack.com/p/claude-code-status-lines-that-actually | "A single line with git branch, model name, and context percentage will immediately improve your Claude Code experience." | SUPPORTED | yes |
| ccstatusline scale + git cache | https://github.com/sirmalloc/ccstatusline | "Git command caching under ~/.cache/ccstatusline/git-cache with configurable TTL … 11.8k stars, 516 forks, MIT licensed" | SUPPORTED | yes |
| ccusage burn-rate offline mode | https://ccusage.com/guide/statusline | "Uses offline mode by default for instant response times without network dependencies" | SUPPORTED | yes |
| starship timeout defaults | https://starship.rs/config/ | "scan_timeout … Default: 30 … command_timeout … Default: 500" | SUPPORTED | yes |
| p10k background git recompute | https://github.com/romkatv/powerlevel10k | "Powerlevel10k displays the last known Git status in grey and continues to compute up-to-date Git status in the background." | SUPPORTED | yes |
| ~77% auto-compact threshold; statusline only surface | https://zenn.dev/trust_delta/articles/claude-code-context-warning-001?locale=en | "automatic compaction triggers when you exceed approximately 77%" … "Claude Code itself has no way of knowing 'how much context it is currently consuming'" | SUPPORTED | yes |
| `statusLine.command` settings changes reload automatically and become visible after the next interaction | https://code.claude.com/docs/en/statusline | "Your status line appears at the bottom of the interface. Settings reload automatically, but changes won’t appear until your next interaction with Claude Code." | SUPPORTED | yes |
| Claude Code has a settings hot-reload path that detects edits to user settings | https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md | "Fixed settings hot-reload not detecting edits to symlinked `~/.claude/settings.json`" | SUPPORTED | yes |

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

### 2026-08-15 (Run 2, narrow refine, gpt-5.6-sol high)

- Scope: resolved whether editing `statusLine.command` in `settings.json` takes effect in a running
  Claude Code session or requires exit and relaunch; the rest of the survey was not re-run.
- Method: live-fetch of the official statusline documentation and upstream changelog, plus targeted
  GitHub issue/discussion searches across restart, reload, hot-reload, and mid-session phrasings.
- Result: live reload is documented; the corrected command is picked up after the next interaction.
  `refreshInterval` is command re-invocation cadence, and the workspace-trust restart instruction is
  a separate failure class. No direct upstream issue report contradicted or independently reproduced
  the documented behavior.
- Verification: five-perspective critic pass and CoVe narrowed the claim to the documented next-interaction
  boundary; it does not claim that the renderer re-reads the file on every tick. Sources [^1] and [^9]
  were re-opened and provenance quotes confirmed verbatim.

### 2026-07-17 (Run 1, ad-hoc, opus researcher)

- Method: research-doc skill (Claude subscription WebSearch/WebFetch; no metered aido/Gemini).
- Fan-out: official CC docs (primary), community tooling (ccstatusline, ccusage), opinion
  (minimal-vs-dense), cross-domain (starship, powerlevel10k), agentic-specific (auto-compact
  context warning). 9 sources, all live-checked.
- 5-perspective critic pass run pre-finalization. Hostile cross-examination dropped the
  powerlevel10k gitstatusd "30.9ms Chromium" benchmark (present in third-party summaries, NOT in
  the official README on fetch) and the "< 200ms render" figure (starship-community heuristic, not
  a fetched CC number) — both re-tagged rather than over-cited.
- Open: no empirical DX study of statusline density found ([NO SOURCE]); CC render-time budget
  undocumented ([NO SOURCE]).

<!-- /doc:region name="run_history" -->
</content>
</invoke>
