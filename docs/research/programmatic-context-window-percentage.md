---
title: "Programmatic Access to the Agent's Own Context-Window Usage Percentage"
category: research
tags: [research, claude-code, context-management, autonomous-loops, ai-harness]
status: complete
source: "claude-adhoc-2026-07-05"
template_version: "research-1.2.0"
---

# Programmatic Access to the Agent's Own Context-Window Usage Percentage

**Status:** complete

**Created:** 2026-07-05

**Task:** ISSUE-REF

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. Statusline JSON payload](#1-statusline-json-payload)
  - [When to use](#when-to-use)
- [2. Hooks (PreCompact / SessionStart / other lifecycle hooks)](#2-hooks-precompact--sessionstart--other-lifecycle-hooks)
  - [When to use](#when-to-use-2)
- [3. Transcript JSONL self-parsing](#3-transcript-jsonl-self-parsing)
  - [When to use](#when-to-use-3)
- [4. OSS community tools](#4-oss-community-tools)
  - [When to use](#when-to-use-4)
- [5. Anthropic API token counting (`/v1/messages/count_tokens`)](#5-anthropic-api-token-counting-v1messagescount_tokens)
  - [When to use](#when-to-use-5)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

<!-- doc:region name="context" kind="immutable" -->

## Context

**Primary period:** 2026
**Source weighting:** 2026 primary (Claude Code docs current as of the fetch date; GitHub tool READMEs/releases current as of 2026-06). No pre-2025 sources are relevant — the statusline JSON schema and hook system are 2025-2026 Claude Code features.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

Primary focus: the current (2026-07) Claude Code documentation at `code.claude.com/docs`, live GitHub repos with 2026 releases, and the current Anthropic Messages API docs. No significant pre-2025 developments apply — statusline `context_window` fields, the `/context` command, and the hooks system are all 2025-2026 additions to Claude Code.

## Executive Summary

1. Claude Code **does** expose a precomputed context-window-usage percentage (`context_window.used_percentage`) — but only to the **statusline script**, a separate shell process whose stdout renders in the human-facing TUI. It is not injected back into the agent's own conversation context by any documented mechanism [VERIFIABLE][^1].
2. **Hooks** (`SessionStart`, `PreCompact`, and all other documented hook events) carry session/transcript metadata (`session_id`, `transcript_path`, `cwd`, etc.) but **no token-count or context-percentage field** in their published payload schema [VERIFIABLE][^3].
3. The transcript JSONL file (referenced by `transcript_path` in both hook payloads and the statusline payload) does carry a per-message `usage` object, but **community investigation found it materially undercounts** input tokens (up to ~100-174x on ~75% of entries) because Claude Code writes the JSONL entry mid-stream, before the final token count is known [VERIFIABLE][^4]. This makes self-parsing the transcript an unreliable path for a precise percentage, though directionally useful as a coarse trigger.
4. The most robust community tools (`ccusage`, `Claude-Code-Usage-Monitor`) do **not** primarily trust the JSONL per-message usage field for live context-window percentage — the actively-maintained monitor explicitly treats **statusline `rate_limits`/context data as the live source of truth when fresh**, falling back to historical JSONL aggregation only for cost/usage reporting, not live context tracking [VERIFIABLE][^7].
5. The Anthropic `/v1/messages/count_tokens` endpoint is free-to-call but is part of the metered `api.anthropic.com` Messages API surface (requires `ANTHROPIC_API_KEY`) — a separate billing plane from the Claude Max subscription that Claude Code CLI sessions run on [VERIFIABLE][^6]. Per this project's hard constraint (never draw on metered API credits from a CC subscription session), it is out of bounds for the CC-loop use case, though legitimate for an Agent-SDK-over-API deployment.

## 1. Statusline JSON payload

The Claude Code statusline is a user-configured shell command (`~/.claude/settings.json` → `statusLine.command`) that Claude Code re-invokes after each new assistant message, after `/compact` finishes, on permission-mode change, or on vim-mode toggle (debounced 300ms) [VERIFIABLE][^1]. Claude Code pipes a JSON object to the script's **stdin**; the script's **stdout** is what renders in the terminal status bar.

The documented schema includes, among other fields [VERIFIABLE][^1]:

```json
{
  "context_window": {
    "total_input_tokens": 0,
    "total_output_tokens": 0,
    "context_window_size": 200000,
    "used_percentage": 0,
    "remaining_percentage": 0,
    "current_usage": { "input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0 }
  },
  "rate_limits": {
    "five_hour": { "used_percentage": 0, "resets_at": 0 },
    "seven_day": { "used_percentage": 0, "resets_at": 0 }
  }
}
```

`context_window.used_percentage` is a **pre-calculated** percentage — exactly the number the human sees in the TUI status bar and via `/context` [VERIFIABLE][^1]. This is the single richest, most direct signal for "how full is my context window right now."

Two important caveats:

- **It is not agent-readable by design.** The statusline command's stdin/stdout round-trip is a side channel Claude Code (the harness) manages between itself and an external script — the running LLM agent inside the session has no built-in tool call that reads this JSON. The agent could, in principle, read `~/.claude/settings.json` to find the configured `statusLine.command` and then *invoke that exact command itself* via the Bash tool — but the command's script expects the harness-generated JSON on stdin, which the agent would have to fabricate or otherwise obtain [INFERENCE — no documented API for an agent to request "give me the current statusline payload" was found; the schema is a harness→script contract, not a harness→agent contract].
- **The field has a known reliability bug.** A filed and apparently still-open GitHub issue reports that `context_window.total_input_tokens` / `total_output_tokens` behaved as *cumulative session totals* rather than current-window contents in at least one shipped version, producing percentages over 100% and diverging from the `/context` command's correct value [VERIFIABLE][^2]. The docs note a `min-version: 2.1.132` fix annotation for exactly this cumulative-vs-current distinction, implying the bug was real and has since been patched in recent releases, but it is evidence the field's semantics have shifted under active development and should not be assumed stable across CC versions [VERIFIABLE][^1].

### When to use

Statusline-payload reading is the right building block **if and only if** the mechanism reading it is external to the agent's own turn — e.g., a wrapper process, launcher script, or hook-adjacent tooling that can execute the *same statusline command* Claude Code already runs, capture its JSON output (or the JSON Claude Code feeds it), and act on it (write a flag file, send a signal) independently of the agent's reasoning loop.

## 2. Hooks (PreCompact / SessionStart / other lifecycle hooks)

Claude Code hooks fire on lifecycle events and receive a JSON payload on stdin containing session identity and file-path metadata: `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, plus event-specific fields (e.g., `SessionStart` adds `source`: `startup|resume|clear|compact`) [VERIFIABLE][^3]. Critically, **no hook payload documented on `code.claude.com/docs/en/hooks` includes a token count or context-percentage field** [VERIFIABLE][^3]. `PreCompact` — the one event conceptually closest to "the harness is about to act on context pressure" — carries only the common fields (session/transcript/cwd/mode) and a `manual`/`auto` matcher for how compaction was triggered, not a usage figure.

This means a hook **cannot itself report "we are at 87% context"** — it can only tell you compaction is *about to happen* (for `PreCompact`) or *just happened* (for `SessionStart` with `source: compact`), which is reactive, not proactive. A hook can, however, act as the trigger point to *go read* `transcript_path` and estimate usage (see §3), or to shell out to the configured statusline command for a fresher number if one exists.

### When to use

Hooks are useful as the **trigger scaffolding** (fire on `PreCompact`/`SessionStart` to log/react to compaction events) but not as a **usage-reporting source** — pair them with §1 or §3 for the actual number.

## 3. Transcript JSONL self-parsing

Every session's transcript is a JSONL file at a documented path (`~/.claude/projects/<project>/<session-id>.jsonl`, surfaced as `transcript_path` in both hook and statusline payloads) [VERIFIABLE][^3]. Each assistant message entry embeds the API response's `usage` object: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` [VERIFIABLE][^4]. In principle, an agent (or a script it invokes) can `tail`/parse this file itself with the Bash/Read tools, no special permission required, and sum the fields of the most recent assistant turn to approximate current context usage — reusing the same math the statusline's `used_percentage` presumably applies.

In practice, an independent investigation (blog post, cross-referenced against the pattern also implicit in `ccusage`'s and `Claude-Code-Usage-Monitor`'s designs) found this data is **materially unreliable for real-time use**: because Claude Code writes the JSONL entry while the response is still streaming, ~75% of entries record `usage.input_tokens` as a placeholder `0` or `1` that is **never corrected** after the request completes, undercounting the true value by roughly 100-174x on affected entries; `output_tokens` similarly excludes extended-thinking tokens, undercounting by roughly 10-17x. The `cache_read_input_tokens`/`cache_creation_input_tokens` fields, which arrive in the initial (non-streamed) response before the stream begins, were found to be reliable (~1x match) [VERIFIABLE][^4].

### When to use

Transcript self-parsing is workable as a **coarse, delayed heuristic** (e.g., "have we crossed N total turns / N cache-token accumulation since last compact") but should not be trusted as a precise percentage-of-window figure without cross-checking against a fresher source (statusline, or an external monitor that itself trusts the statusline path). Treat any number derived this way as a lower bound, not a point estimate.

## 4. OSS community tools

Several actively maintained open-source tools exist in this space as of 2026-06/07:

- **`ccusage`** (ryoppippi) — a CLI that reads local Claude Code (and other coding-CLI) JSONL usage logs to produce cost/usage reports (`daily`, `monthly`, `session`, `blocks` for 5-hour billing windows); very actively maintained (v20.0.14, 128 releases, latest release dated 2026-06-15, 16.9k GitHub stars) [VERIFIABLE][^5]. It is primarily a **historical cost/usage reporting** tool, not a live context-percentage monitor, and its data source is the same undercounting-prone JSONL logs described in §3.
- **`Claude-Code-Usage-Monitor`** (Maciek-roboblog) — a real-time Python monitor (`claude-monitor` / `cmonitor`) with machine-readable `--once`/`--write-state` output and automation-friendly exit codes, explicitly designed for scripted/automation use. Its README states it treats "**official Claude Code statusline `rate_limits`** ... as the live source of truth when fresh," falling back to 90th-percentile historical estimation only when that live source is unavailable [VERIFIABLE][^7]. This is a meaningful validation of §1: the most automation-oriented tool in this space independently converged on "read what the statusline reads" as the reliable path, rather than parsing JSONL directly for live numbers.
- **`avanrossum/claude-context-monitor`** — a small script that parses JSONL token metadata to warn about impending auto-compaction. Low-signal on maintenance (4 commits, 1 star, no visible recent-activity dates) [VERIFIABLE][^8] — useful only as a reference implementation of the JSONL-parsing approach, not as a dependency to adopt.
- No MCP server or CC plugin was found that exposes context-window percentage as a **tool call the agent itself can invoke mid-session** (as opposed to an external monitor process). This is a plausible blind spot — an MCP server wrapping the statusline JSON or an OTEL metrics scrape into a `get_context_usage()` tool is straightforward to build and was not found to exist yet [INFERENCE].

### When to use

Adopt `Claude-Code-Usage-Monitor`'s design principle (statusline-first, JSONL as fallback) rather than any specific tool as a dependency — none of these projects are built to be called *by the agent, from inside its own turn*; they are external processes.

## 5. Anthropic API token counting (`/v1/messages/count_tokens`)

The Messages API's `count_tokens` endpoint returns an exact input-token count for a given system/messages/tools payload, is free to call (no token billing), but sits on the standard rate-limit tiers (2,000-8,000 RPM) [VERIFIABLE][^6]. It requires the same `x-api-key`/`ANTHROPIC_API_KEY` auth as any Messages API call — i.e., it is **not** reachable from inside a Claude Code CLI session running on a Max subscription without deliberately configuring a metered API key, which this project's operating rules treat as a hard barrier (`ANTHROPIC_API_KEY` intentionally left unset; CLI-subprocess only) [HEURISTIC — this constraint is this project's own policy, not an Anthropic-imposed one].

### When to use

Relevant only for an **Agent-SDK-over-API** deployment (not a CC-CLI-subscription session) where the calling process already holds an API key and wants an exact pre-flight token count for a candidate prompt before sending it — e.g., to decide whether a batch job's next prompt will fit. Not usable, by this project's own metered-API-avoidance rule, as a context-tracking mechanism for autonomous CC-subscription loops.

## Comparison

| # | Criterion | Statusline payload (§1) | Hooks (§2) | Transcript JSONL (§3) | OSS monitor (§4) | `count_tokens` API (§5) |
|---|-----------|--------------------------|------------|-------------------------|--------------------|--------------------------|
| 1 | Reports a live % figure | Yes, pre-calculated | No | No (raw tokens only, self-sum) | Only via statusline passthrough | No (per-request estimate, not window fill) |
| 2 | Reachable by the agent mid-turn without new infra | No (harness↔script contract, not harness↔agent) | No (fires on lifecycle events only) | Yes (Bash/Read on `transcript_path`) | No (external process) | No (needs `ANTHROPIC_API_KEY`, out of bounds) |
| 3 | Known accuracy issues | Yes — cumulative-vs-current bug reported, apparently patched by v2.1.132 | N/A (no usage field to be wrong) | Yes — ~75% of entries undercount ~100-174x mid-stream | Inherits JSONL issue unless statusline-sourced | Estimate may differ slightly from actual billed usage |
| 4 | Cost | Free (local) | Free (local) | Free (local) | Free (local) | Free but metered-API-plane |
| 5 | Maturity / maintenance | Native, documented, versioned | Native, documented | Native (file exists); parsing is DIY | `ccusage` and `Claude-Code-Usage-Monitor` both active in 2026-06/07 | Native Anthropic API, stable |

## Recommendation

Bake a **small external "context-watcher" script**, not an in-agent tool call, into ai-harness: a lightweight process (or a `refreshInterval`-driven statusline companion / cron-style poller) that invokes the **same command Claude Code already runs for the statusline** (or reads its cached output if the harness exposes one), extracts `context_window.used_percentage` from that JSON, and when it crosses a configured threshold, drops a flag file or fires a notification (ntfy) that the *next* turn's `SessionStart`/`UserPromptSubmit` hook — or the agent itself, if instructed to periodically check for that flag file via a Bash call — reads and acts on ("run `/save-state`, then stop for `/compact`"). This is the design **`Claude-Code-Usage-Monitor` already validated** as the most reliable live source [VERIFIABLE][^7], and it sidesteps both the transcript-undercounting problem (§3) and the metered-API barrier (§5).

Ranked, most to least reliable for "the loop knows to self-trigger at a threshold":

1. **Statusline-payload read via an external companion process** (§1) — most accurate, native, zero extra cost; requires only a small script + a place to stash last-seen usage (e.g., a `~/.claude/last_context_pct` file the statusline script itself writes as a side effect each time Claude Code invokes it).
2. **Hook-based trigger, sourced from #1's stashed value** (§2) — `SessionStart`/`UserPromptSubmit` hook reads the flag file #1 wrote and injects an instruction ("context is at 78%, consider `/save-state`") into context at natural checkpoints. Reliable and cheap, but reactive to hook firing cadence, not continuous.
3. **Transcript-JSONL coarse heuristic** (§3) — usable only as a rough tripwire (e.g., "total turns since last compact > N" or watching `cache_read_input_tokens` growth, the one field verified accurate) when #1/#2 aren't wired up yet; do not trust it for a precise %.
4. **External monitor dashboard** (§4) — good for human-facing visibility (a terminal split showing `cmonitor`), not a substitute for #1-2 as an *agent-facing* signal, since it's a separate process the agent doesn't read unless piped in.
5. **`count_tokens` API** (§5) — excluded for CC-subscription loops per this project's metered-API rule; keep in mind only if/when the loop moves to an Agent-SDK-over-API deployment.

Practical next step (~15-20 min agent-execution time to prototype): a `~/.claude/statusline.sh` wrapper that (a) does its normal rendering job and (b) as a side effect, writes `context_window.used_percentage` + a timestamp to a small state file; then a `SessionStart` hook (or a periodic check the agent runs at natural checkpoints, e.g., after every N tool calls) reads that file and, above the configured threshold, tells the agent to invoke `/save-state`.

## Open Questions

1. Does Claude Code offer, or plan to offer, a documented way for the *hooks* system (not just the statusline contract) to receive `context_window` fields directly — closing the gap identified in §2 — and if so, on what timeline? Not found in current docs; worth periodically re-checking `code.claude.com/docs/en/hooks` and the changelog.
2. Is there a supported way to invoke the statusline script's output on demand (rather than only after harness-triggered events) so a "companion writes state file" design in the Recommendation isn't reliant on turn cadence? The `refreshInterval` field (min 1s) suggests near-continuous polling is possible, but its interaction with a wrapper script that also writes a side-effect file wasn't tested here.
3. Would Anthropic consider exposing `context_window.used_percentage` (or an equivalent) as a first-class **tool** the agent can call directly (e.g., an `mcp__builtin__get_context_usage`), removing the need for the external-companion workaround entirely? No evidence found that this is planned; flagged as the cleanest long-term fix if it shipped.
4. How does the reported cumulative-vs-current statusline bug (§1, GH #13783) behave on the Claude Code version currently pinned in this project's environment — worth a direct empirical check (run the sample `jq` one-liner from the docs against a live session) before wiring the Recommendation's design into ai-harness.

## Sources

[^1]: Anthropic. (2026). [Customize your status line](https://code.claude.com/docs/en/statusline). Claude Code Docs. Verified accessible (HTTP 200) 2026-07-05. (Full statusline JSON schema, `context_window.*` fields, update cadence, `refreshInterval`.)
[^2]: GitHub user (reporter). (2026). [[BUG] Statusline context_window JSON contains cumulative tokens instead of current context usage · Issue #13783](https://github.com/anthropics/claude-code/issues/13783). anthropics/claude-code. Verified accessible (HTTP 200) 2026-07-05. (Cumulative-vs-current bug report with example JSON showing 169% usage.)
[^3]: Anthropic. (2026). [Hooks reference](https://code.claude.com/docs/en/hooks). Claude Code Docs. Verified accessible (HTTP 200) 2026-07-05. (Common hook input fields, `SessionStart`/`PreCompact` schemas, `transcript_path`.)
[^4]: Gille. (2026). [Claude Code's Token Counts Are Wrong — 100x Off in the JSONL Logs](https://gille.ai/en/blog/claude-code-jsonl-logs-undercount-tokens/). gille.ai. Verified accessible (HTTP 200) 2026-07-05. (JSONL `usage.*` field undercounting analysis, streaming-placeholder root cause.)
[^5]: ryoppippi. (2026). [ccusage](https://github.com/ryoppippi/ccusage). GitHub. Verified accessible (HTTP 200) 2026-07-05. (Release v20.0.14, 2026-06-15; JSONL-based cost/usage CLI.)
[^6]: Anthropic. (2026). [Token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting). Claude Platform Docs. Verified accessible (HTTP 200) 2026-07-05. (`/v1/messages/count_tokens` schema, free-but-rate-limited, requires standard Messages API auth.)
[^7]: Maciek-roboblog. (2026). [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor). GitHub. Verified accessible (HTTP 200) 2026-07-05. (v4.0.0, 2026-06-27; statusline-`rate_limits`-as-source-of-truth design, 90th-percentile fallback.)
[^8]: avanrossum. (2026). [claude-context-monitor](https://github.com/avanrossum/claude-context-monitor). GitHub. Verified accessible (HTTP 200) 2026-07-05. (JSONL-parsing context-length checker; low commit/star count, no visible recent-activity dates.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** ISSUE-REF (ad hoc, not aido-registered)
**Model:** Sonnet
**Date:** 2026-07-05

```text
Programmatic access to the agent's own context-window usage percentage — can Claude
self-fetch its context %?

Context/angle: In Claude Code sessions we manage the context window proactively
(save-state at high context %, then compact). Today the % is only visible to the human
in the TUI. We want the AGENT to be able to read its own current context-window
utilization programmatically, so autonomous loops can self-trigger save-state/compaction
at a threshold instead of relying on a human or guesswork.

Research questions:
1. Does Claude Code expose context % to the agent? — Any tool, env var, file, hook
   payload, transcript marker, or MCP endpoint that reports current token usage /
   context-window fraction to the running agent. Check code.claude.com/docs (verify
   against docs, NOT local ~/.claude dirs — built-in features aren't files). Look at:
   the transcript .jsonl (does it carry a running token count?), the
   PreCompact/SessionStart hook payloads, /context command, statusline data (the
   statusline script DOES receive usage data — document exactly what fields), any
   CLAUDE_CODE_* env vars.
2. Statusline as a source — the Claude Code statusline command receives a JSON payload
   on stdin. Does it include token/context usage? If so, that's a channel: a hook or the
   agent could read the same data. Document the exact schema.
3. OSS plugins / community solutions — any GitHub projects, CC plugins, MCP servers, or
   scripts that surface context % to the agent or automate compaction at a threshold
   (e.g. ccusage, claude-code-usage-monitor, autocompact tools). Maintenance status
   2025-2026.
4. Anthropic API token counting — the /v1/messages/count_tokens endpoint and streaming
   usage fields; relevant for the Agent SDK path but note it's a separate API (we avoid
   metered API — flag that).
5. Recommendation — what's the most reliable way TODAY for an autonomous CC loop to know
   its context %, and bake a small mechanism into ai-harness. Rank options
   (statusline-payload read, transcript token-sum, hook-based estimate, external
   monitor). Clear recommendation with reasoning.

Prefer official CC/Anthropic docs, GitHub READMEs, and technical posts. Apply the
retrieval floor (>=1 live fetch per load-bearing claim), failure taxonomy for
unreachable sources, 5-perspective critic pass, and provenance ledger for every
[VERIFIABLE] claim. CRITICAL accuracy note: do NOT assert a CC feature exists based on
inference — verify against code.claude.com/docs; if you can't confirm, tag [NO SOURCE]
or [INFERENCE] honestly. Use WebSearch/WebFetch ONLY — never Gemini, never
`aido research`, never any metered API.

Temporal scope: weight sources by recency (2026 primary); if post-cutoff literature is
genuinely sparse for a subtopic, state "no significant recent developments found" rather
than backfilling with older sources. Backfilling is a failure mode, not a hedge.

Before finalizing, run a Chain-of-Verification (CoVe):
  1. Isolate the core facts required.
  2. Draft a tentative answer.
  3. Hostile cross-examination: flag any claim cited because the prompt implied it,
     rather than because it was verified.
  4. Strip any claim that cannot be empirically verified.

Classify every major claim. Write the rationale BEFORE the tag (tag-first causes
post-hoc rationalization): rationale -> evidence check -> tag.
Hard constraint (overrides all formatting): never invent a citation to satisfy a format
instruction. Accuracy > completeness.
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

| Claim | Source URL | Verbatim quote | Verdict | Live? |
|---|---|---|---|---|
| Statusline payload includes a pre-calculated `context_window.used_percentage` field | https://code.claude.com/docs/en/statusline | "`context_window.used_percentage` \| Pre-calculated percentage of context window used" | SUPPORTED | Yes (HTTP 200, 2026-07-05) |
| Statusline script runs locally, does not consume API tokens | https://code.claude.com/docs/en/statusline | "The status line runs locally and does not consume API tokens." | SUPPORTED | Yes (HTTP 200, 2026-07-05) |
| Statusline updates only on specific harness-driven triggers, debounced 300ms | https://code.claude.com/docs/en/statusline | "Your script runs after each new assistant message, after `/compact` finishes, when the permission mode changes, or when vim mode toggles. Updates are debounced at 300ms" | SUPPORTED | Yes (HTTP 200, 2026-07-05) |
| `context_window.total_input_tokens`/`total_output_tokens` were reported as cumulative session totals, not current-window contents, causing >100% readings | https://github.com/anthropics/claude-code/issues/13783 | "The total_input_tokens and total_output_tokens appear to be session totals (all tokens ever sent/received), not the current context window contents." | SUPPORTED | Yes (HTTP 200, 2026-07-05) |
| Documented hook payloads (SessionStart, PreCompact) carry only session/transcript metadata, no token/context field | https://code.claude.com/docs/en/hooks | Common input fields table lists `session_id`, `prompt_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name` only; no token/usage field documented for any event | SUPPORTED | Yes (HTTP 200, 2026-07-05) |
| JSONL transcript `usage.input_tokens` is a mid-stream placeholder (0 or 1) on ~75% of entries and never corrected, undercounting by ~100-174x | https://gille.ai/en/blog/claude-code-jsonl-logs-undercount-tokens/ | "75% of all JSONL entries have usage.input_tokens of 0 or 1... never updated to the real value" | SUPPORTED | Yes (HTTP 200, 2026-07-05) |
| `cache_read_input_tokens`/`cache_creation_input_tokens` in the JSONL are reliable (~1x) because they arrive pre-stream | https://gille.ai/en/blog/claude-code-jsonl-logs-undercount-tokens/ | "cache_read_input_tokens and cache_creation_input_tokens 'match at roughly 1x' against actual usage because these values arrive in the initial API response before streaming begins" | SUPPORTED | Yes (HTTP 200, 2026-07-05) |
| `ccusage` is actively maintained as of mid-2026 | https://github.com/ryoppippi/ccusage | "v20.0.14 Latest" dated "Jun 15, 2026" with 128 total releases | SUPPORTED | Yes (HTTP 200, 2026-07-05) |
| `Claude-Code-Usage-Monitor` treats official statusline data as the live source of truth, falling back to statistical estimation | https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor | "Official Claude Code statusline rate_limits are treated as the live source of truth when fresh." | SUPPORTED | Yes (HTTP 200, 2026-07-05) |
| `/v1/messages/count_tokens` is free to call but rate-limited under standard Messages API tiers | https://platform.claude.com/docs/en/build-with-claude/token-counting | "Token counting is free to use but subject to requests per minute rate limits based on your usage tier" | SUPPORTED | Yes (HTTP 200, 2026-07-05) |

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

- 2026-07-05: Initial draft via `/research-doc` skill (ad hoc WebSearch/WebFetch, Sonnet), ISSUE-REF. No prior versions.

<!-- /doc:region name="run_history" -->
