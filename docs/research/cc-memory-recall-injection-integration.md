---
title: "Wiring a Memory/Knowledge Recall + Injection Layer Deeply into Claude Code"
category: research
tags: [research, claude-code, hooks, mcp, rag, memory, retrieval, injection]
status: complete
source: "claude-adhoc-2026-07-15"
template_version: "research-1.2.0"
related_docs:
  - docs/research/agent-heartbeat-persistent-memory-survey.md
  - docs/research/memory-hierarchy-audit-and-oss-survey.md
  - docs/research/agent-memory-unification-bd-remember-vs-markdown.md
  - docs/research/cc-session-lifecycle-observability-and-continuity.md
---

# Wiring a Memory/Knowledge Recall + Injection Layer Deeply into Claude Code

**Status:** complete

**Created:** 2026-07-15

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. CC hook injection capabilities (the load-bearing fact-check)](#1-cc-hook-injection-capabilities-the-load-bearing-fact-check)
  - [When to use](#when-to-use)
- [2. MCP tools vs MCP resources for on-demand recall](#2-mcp-tools-vs-mcp-resources-for-on-demand-recall)
  - [When to use](#when-to-use-2)
- [3. RAG-into-agent-context: community & existing implementations](#3-rag-into-agent-context-community--existing-implementations)
  - [When to use](#when-to-use-3)
- [4. Cross-session & cross-repo shared-memory injection](#4-cross-session--cross-repo-shared-memory-injection)
- [5. Retrieval quality & context economy](#5-retrieval-quality--context-economy)
- [6. Transparency & observability of retrieval](#6-transparency--observability-of-retrieval)
- [7. Gaps, blindspots & emergent findings](#7-gaps-blindspots--emergent-findings)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

<!-- doc:region name="context" kind="immutable" -->

## Context

How to wire a live memory/knowledge recall + injection layer deeply into Claude Code (the
concrete CC-integration mechanisms) for a solo developer's ~18-repo CC agent fleet backed by one
Postgres substrate (pgvector semantic + tsvector FTS + Apache AGE graph) that indexes the fleet's
markdown memory + docs. Markdown/files stay authoritative; Postgres is the derived, queryable
recall index. Three injection mechanisms are hypothesized (to verify + refine): (1) per-prompt
auto-recall via a `UserPromptSubmit` hook; (2) session-start priming via a `SessionStart` hook;
(3) agent-driven deep recall via an MCP `recall`/`search` tool. Hard requirement: transparency —
every substrate query is announced in-session with what was retrieved, plus a `recall_log` →
Grafana dashboard + dead-man's switch so the layer can't go dormant unnoticed.

**Primary period:** 2024–2026
**Source weighting:** 2026 primary; 2025 secondary; 2024 background. Official Claude Code docs
(code.claude.com/docs) override any blog on CC-mechanism facts.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

Primary focus: 2026 sources, weighted to the current Claude Code docs (fetched 2026-07-15) because
the hook and MCP surfaces change per-release (the docs carry `v2.1.x` version gates on individual
behaviors). 2025 community projects (`claude-mem`, `basic-memory`, `claude-rag-memory`) cited as
live pattern evidence. GraphRAG/hybrid-retrieval research weighted 2026 → 2025. No significant
pre-2024 developments are relevant: the `UserPromptSubmit`/`additionalContext` injection surface
did not exist before the 2025 hooks system.

## Executive Summary

1. **The load-bearing fact is confirmed: a `UserPromptSubmit` hook CAN inject retrieved context
   into the model before it responds.** On exit 0, stdout from `UserPromptSubmit`,
   `UserPromptExpansion`, and `SessionStart` hooks "is added as context that Claude can see and act
   on"; the structured path is `hookSpecificOutput.additionalContext`, "a string from your hook
   into Claude's context window" that CC "wraps in a system reminder and inserts into the
   conversation at the point where the hook fired." [VERIFIABLE][^1][^2] This makes true RAG-into-context
   (query substrate with the prompt → inject top-K) a first-class, supported CC pattern — not a hack.
2. **The recommended architecture is a three-tier split, each tier mapped to a distinct CC surface:**
   `SessionStart` hook for a lean session-priming recall block; `UserPromptSubmit` hook for
   cheap, non-blocking per-prompt auto-recall (grep/vector, no LLM on the hot path); and an MCP
   `recall` tool for agent-driven deep/multi-hop search the model calls explicitly. This mirrors
   what every working community system converged on. [INFERENCE]
3. **Hard constraints bound the hook tiers.** `additionalContext` (and plain stdout) is **capped at
   10,000 characters** — overflow is spilled to a file reference — and `UserPromptSubmit` **lowers
   the hook timeout to 30 seconds**, so the per-prompt hook must be fast and lean or it stalls every
   turn. [VERIFIABLE][^1] These two limits are the primary design forcing-functions.
4. **The best community pattern to borrow is the "cheap, lean, never-block" capture/inject
   discipline** proven by SuperBrain and `claude-rag-memory`: no LLM call on the hot path, hooks
   exit 0 always (a crashing hook can't break your turn), heavy distillation deferred to detached
   child processes, and injection scoped + size-budgeted. The dominant failure mode is the
   opposite — `claude-mem` v3's automatic injection "caused context pollution," forcing v4 to
   retreat to explicit search. [VERIFIABLE][^5][^6]
5. **Transparency has a native, proven implementation.** `claude-rag-memory` prints a per-response
   token breakdown (input / RAG-injected / similar-history / total / RAG-overhead %), and
   `claude-mem` injects a "progressive disclosure index" table (id, time, type, title, tokens)
   so the human sees exactly what was recalled. Pair this with a `recall_log` table → Grafana +
   dead-man's switch for system telemetry. [VERIFIABLE][^4][^6]

## 1. CC hook injection capabilities (the load-bearing fact-check)

This is the claim everything else rests on, so it is grounded directly in the official Claude Code
hooks docs (fetched 2026-07-15), not blogs.

**Exit-code semantics — the injection channel.** The hooks guide states plainly: "For
`UserPromptSubmit`, `UserPromptExpansion`, and `SessionStart` hooks, anything you write to stdout is
added to Claude's context." [VERIFIABLE][^2] The reference generalizes it: "JSON output is only
processed on exit 0. For most events, stdout is written to the debug log but not shown in the
transcript. The exceptions are `UserPromptSubmit`, `UserPromptExpansion`, and `SessionStart`, where
stdout is added as context that Claude can see and act on." [VERIFIABLE][^1]

**The structured channel — `additionalContext`.** Beyond raw stdout, the clean API is
`hookSpecificOutput.additionalContext`: "The `additionalContext` field passes a string from your
hook into Claude's context window. Claude Code wraps the string in a system reminder and inserts it
into the conversation at the point where the hook fired." [VERIFIABLE][^1] For a retrieval layer,
prefer `additionalContext` over raw stdout: it is explicit, survives future stdout-handling changes,
and its placement semantics are documented. For `UserPromptSubmit` the injected block lands
"alongside the submitted prompt"; for `SessionStart`, "at the start of the conversation, before the
first prompt." [VERIFIABLE][^1] When several hooks fire on one event, "Text from `additionalContext`
is kept from every hook and passed to Claude together." [VERIFIABLE][^2]

**Hard limits (the design forcing-functions).**

- **Size:** "Hook output strings, including `additionalContext`, `systemMessage`, and plain stdout,
  are capped at 10,000 characters. Output that exceeds this limit is saved to a file and replaced
  with a preview and file path." [VERIFIABLE][^1] ~10 KB ≈ 2,000–2,800 tokens is your hard
  per-injection budget.
- **Latency:** default command/http/mcp_tool timeout is 600 s, "but `UserPromptSubmit` lowers the
  `command`, `http`, and `mcp_tool` default to 30" seconds. [VERIFIABLE][^1] The per-prompt hook
  blocks the turn until it returns, so it must be fast.
- **It injects, it can't replace:** "`UserPromptSubmit`: can't replace the prompt; it only injects
  `additionalContext` alongside it." [VERIFIABLE][^1] Exit 2 on `UserPromptSubmit` "blocks prompt
  processing and erases the prompt" — so never let the recall hook exit 2 on a retrieval failure. [VERIFIABLE][^1]
- **Plain text only:** "Text returned via `additionalContext` is injected as a system reminder that
  Claude reads as plain text." Command hooks "can't trigger `/` commands or tool calls." [VERIFIABLE][^2]

**Other hooks usable for retrieval injection.** `SessionStart` (matcher `startup|resume|clear|compact`)
is the canonical priming/re-priming point — the docs' own worked example is "Re-inject context after
compaction" using a `SessionStart` hook with a `compact` matcher. [VERIFIABLE][^2] `PreToolUse`,
`PostToolUse`, `PostToolUseFailure`, `PostToolBatch`, `Stop`, and `SubagentStart` all also support
`additionalContext`, so tool-scoped recall (e.g. inject related docs when the model is about to edit
a file) is possible — though `PreCompact` and `SessionEnd` do NOT inject context (they are
capture/telemetry points). [VERIFIABLE][^1] Note a newer surface: hooks can be
`"type": "mcp_tool"` (call an already-connected MCP server tool) or `"type": "http"` (POST to an
endpoint that returns the same JSON), so the per-prompt recall hook can itself delegate to the
substrate's MCP server or a local HTTP retrieval service rather than shelling out. [VERIFIABLE][^2]

### When to use

Use the **`UserPromptSubmit`** tier for automatic, every-turn recall keyed to the actual prompt —
this is the only hook that runs on every prompt and can add context before the model, so it is where
RAG belongs. Use **`SessionStart`** for stable, once-per-session priming (project index, recent
decisions, resume state) and for re-priming after `compact`. Reserve `PreToolUse`/`PostToolUse`
injection for narrowly-scoped, tool-triggered recall where per-turn injection would be noise.

## 2. MCP tools vs MCP resources for on-demand recall

Two MCP surfaces can carry recall, and they behave differently in context.

**MCP tools (model-decides-to-call).** A `recall`/`search` tool is invoked by the model when it
judges it needs to; results land in context only when called. With **tool search enabled by default**,
"MCP tools are deferred rather than loaded into context upfront, and Claude uses a search tool to
discover relevant ones when a task needs them. Only the tools Claude actually uses enter context." [VERIFIABLE][^3]
A large recall result is subject to the same spill-to-disk behavior as any tool output ("results
that exceed the default threshold are persisted to disk and replaced with a file reference"). [VERIFIABLE][^3]
For a tool the model should always see without a search step, set `alwaysLoad: true` on the server —
but "each upfront tool consumes context," and `alwaysLoad` blocks startup until the server connects. [VERIFIABLE][^3]

**MCP resources (@-mention, user/model-referenced).** Resources are "referenced using @ mentions,
similar to how you reference files"; typing `@` lists them, and "Resources are automatically fetched
and included as attachments when referenced." [VERIFIABLE][^3] They are pull-on-demand context
anchors, not push. They do NOT auto-inject on relevance — someone (usually the human, or the model
via the auto-provided list/read tools) must reference them.

**Tradeoff.** A tool gives the model agency (deep, multi-hop, parameterized recall) at the cost of
the model having to decide to call it — and models under-call memory tools (the SuperBrain author's
core complaint: 8,785 stored observations, "none surfaced automatically"). [VERIFIABLE][^5] Resources
are good for a small, stable set of canonical documents the human wants to pin by name, not for
relevance-driven recall. Neither replaces the hook tiers: hooks are the only mechanism that injects
*without the model choosing to*.

### When to use

Expose an MCP `recall` **tool** for agent-driven deep search (graph traversal, multi-hop, filtered
queries) that the model invokes explicitly — this is tier 3. Do NOT rely on it for baseline recall;
models under-call it. Use MCP **resources** only to pin a handful of always-relevant canonical docs
by `@`-name. Keep the recall tool out of `alwaysLoad` unless it is needed every turn.

## 3. RAG-into-agent-context: community & existing implementations

The 2026 ecosystem has converged, independently, on the same shape. Survey of live implementations:

**`claude-mem`** — five/six lifecycle hooks: `SessionStart` (context injection, silent),
`UserPromptSubmit` (session init), `PostToolUse` (observation capture → enqueue), `Stop` (summary
generation), `SessionEnd` (cleanup). As of CC 2.1.0 "SessionStart hooks no longer display
user-visible messages. Context is silently injected via `hookSpecificOutput.additionalContext`." It
queries SQLite for recent session summaries + observations and formats them as a "progressive
disclosure index." [VERIFIABLE][^4] Notably, `claude-mem`'s **automatic** injection (v3) "caused
context pollution," and v4 retreated to explicit search — the canonical cautionary tale. [VERIFIABLE][^5]

**SuperBrain (El Khoury)** — the sharpest diagnosis: "My AI memory problem wasn't a storage problem.
It was a working-memory problem." Storage is solved; *injection* is the gap. Its design: **project-scoped
`SessionStart` injection** of a "~1,200-token recall block" of top-ranked results, with three hot-path
rules — "No LLM call on the capture path," "Hooks always exit 0 no matter what — a crashing hook is
impossible by construction," and heavy distillation runs "as a _detached_ child process" off the turn.
Markdown vault stays authoritative and inspectable. [VERIFIABLE][^5]

**`claude-rag-memory`** — the closest match to the target design. Three hooks: `UserPromptSubmit`
(every message → save + inject), `SessionStart` (auto-index + inject past summaries), `Stop` (compress
to summary). Storage = SQLite (`inputs.db`) + ChromaDB (semantic history + per-project code). Key
discipline: "only semantically similar past inputs are injected — not the entire history." It also
exposes MCP tools — `rag_query()`, `rag_find_relevant_files()`, `rag_project_summary()` — for manual
deep recall, i.e. the exact hook-tiers + MCP-tool split. And it prints a per-response transparency
breakdown (input tokens / session-RAG context / similar-history injected / total / RAG-overhead %). [VERIFIABLE][^6]

**Basic Memory** — "Plain Markdown you own, with a full-text, semantic, and knowledge-graph index on
top." Three indexes (FTS "find by meaning, not just keywords", "vector and hybrid search," and "typed
relations and observations connect notes into a graph"), MCP read/write, plus a CC plugin providing
"search-before-answer recall and auto-capture" for "Claude, Codex, OpenClaw, and Hermes." [VERIFIABLE][^7]
This is the closest architectural analog to the target substrate (markdown-authoritative + hybrid
index + graph), just SQLite/DuckDB-class rather than one shared Postgres.

**Cross-cutting UX lesson.** Every working system: (a) uses `SessionStart` for priming and
`UserPromptSubmit` for per-prompt recall; (b) keeps the hot path cheap and non-blocking; (c) injects
*selectively*, never the whole store; (d) offers explicit MCP tools for deep recall on top. The ones
that tried to auto-inject aggressively regressed to explicit search.

### When to use

Borrow `claude-rag-memory`'s exact topology (it is the reference implementation of the target design)
but back it with the shared Postgres substrate instead of local ChromaDB, and borrow SuperBrain's
hot-path discipline (never-block, exit-0-always, deferred distillation) as the hard rules for the
`UserPromptSubmit` hook.

## 4. Cross-session & cross-repo shared-memory injection

The fleet runs many concurrent CC sessions across ~18 repos in worktrees. A shared Postgres substrate
is the natural backend (a single index all sessions query), but injection must be **scoped** or it
becomes cross-repo noise.

- **Scope by default, widen deliberately.** SuperBrain's decisive lesson is that recall must be
  "project-scoped" — "Search queries are constrained to the current working directory's project
  scope." [VERIFIABLE][^5] The hook receives `cwd` and `CLAUDE_PROJECT_DIR` in its JSON input, and
  stdio MCP servers receive `CLAUDE_PROJECT_DIR` in their environment, so both tiers can key queries
  to the current repo without extra plumbing. [VERIFIABLE][^2][^3] The default recall filter should be
  `repo = current` with a small global tier for genuinely fleet-wide facts (decisions, conventions).
- **Freshness / notify.** MCP supports `list_changed` notifications so a server can push updated
  capabilities without reconnect [VERIFIABLE][^3], and MCP "channels" (`claude/channel` capability +
  `--channels`) let a server "push messages directly into your session so Claude can react to
  external events." [VERIFIABLE][^3] This is the native hook for a cross-session "another session just
  wrote a decision relevant to you" nudge — event-driven, not polling. Reconciliation
  (markdown → Postgres) is out of scope here but is the prerequisite for freshness.
- **Concurrency.** Because the substrate is read-mostly on the recall path (writes happen on capture
  hooks / reconciliation), many concurrent sessions querying it is a standard read-scaling problem,
  not a coordination one. The write path (capture) should be append/enqueue (as `claude-mem` does:
  "Enqueues observation in observation_queue table … Returns immediately"). [VERIFIABLE][^4]

## 5. Retrieval quality & context economy

The 10K-char cap and 30 s timeout make context economy non-negotiable. Best practices:

- **Relevance thresholding + top-K, not top-everything.** Inject only semantically similar hits
  ("only semantically similar past inputs are injected — not the entire history"). [VERIFIABLE][^6]
  A ~1,200-token recall block is a proven per-session budget; for per-prompt, keep it smaller. [VERIFIABLE][^5]
- **Never dump whole files.** "Inject only the top matches, never dump whole files" is the stated
  design rule of the per-prompt hook school. [VERIFIABLE][^5] (via community synthesis)
- **Dedup against already-loaded context.** Not natively handled by CC — the hook must track what it
  already injected this session (e.g. a session-scoped seen-set keyed by hit id) to avoid re-injecting
  the same memory every turn. [INFERENCE]
- **When NOT to inject.** If no hit clears the relevance threshold, inject nothing and exit 0 silently
  — an empty recall is better than a noisy one, and the hook must never exit 2. [INFERENCE]
- **Hybrid retrieval + reranking is the 2026 default.** The production RAG stack is BM25/FTS + vector
  + a graph layer with a cross-encoder reranker on top. [HEURISTIC] For agent recall specifically,
  the composition pattern is: "Embed the query → ANN vector search to retrieve semantically closest
  entry nodes → Start graph traversal from those entry nodes → Combine the subgraph context with
  semantic chunks." [VERIFIABLE][^8] This is exactly how pgvector (entry nodes) + AGE (traversal) +
  tsvector (keyword recall) compose for the substrate.
- **Graph earns its cost only on multi-hop.** Graph retrieval wins big on connect-the-dots questions
  ("86% accuracy vs. 32% for baseline vector RAG"; Zep temporal graph "63.8% on LongMemEval vs.
  Mem0's 49.0%") but **underperforms vanilla vector by 13.4% on single-hop fact lookup** and carries
  "2.3x higher latency than vector search." [VERIFIABLE][^8] Implication: default per-prompt recall
  should be vector+FTS (fast, cheap, on the 30 s hot path); reserve AGE graph traversal for the MCP
  `recall` tool where the model explicitly wants multi-hop reasoning and can absorb the latency.
- **Retrieval-poisoning is a live 2026 threat.** Knowledge/graph poisoning attacks against RAG are an
  emerging security concern; a self-writing memory that later auto-injects is an injection vector. [HEURISTIC]
  For a solo local fleet the risk is low, but capture-then-auto-inject means a bad captured note can
  resurface as authoritative context — argues for human-inspectable markdown-as-authority (which the
  design already has) and provenance on every injected hit.

## 6. Transparency & observability of retrieval

The hard requirement (announce every query, show what was retrieved, telemetry so it can't go dormant)
has proven native patterns:

- **In-session provenance of injected context.** `claude-mem` injects a "progressive disclosure
  index" — a compact table (`| ID | Time | T | Title | Tokens |`) so the human sees each recalled
  item and can drill in via MCP search. [VERIFIABLE][^4] `claude-rag-memory` prints a per-response
  breakdown: user-input tokens, session-RAG context, similar-history injected, total tokens, and
  RAG-overhead %. [VERIFIABLE][^6] Borrow both: prepend the injected `additionalContext` block with a
  short header — query, top-K hit sources, relevance scores, which were used — so the injection is
  self-documenting inside the very block it injects. (Note: `SessionStart` injection is *silent* since
  CC 2.1.0 [VERIFIABLE][^4], so visible transparency must be carried inside the injected text itself
  or via a separate `MessageDisplay`/stderr channel, not by expecting CC to render it.)
- **System telemetry (dead-man's switch).** The hook/tool writes a `recall_log` row per query
  (timestamp, session, repo, query, hit count, scores, injected bytes, latency). A Grafana panel over
  that table + a dead-man's-switch alert (no recall events in N hours across the active fleet = the
  layer is dormant) directly addresses the "prior memory system died unnoticed for months" failure.
  This is standard for-agent-memory observability and pairs with the fleet's existing
  Prometheus/Grafana + Pushover stack. [HEURISTIC]
- **Failure visibility.** Because the hook exits 0 always (never blocks the turn), a retrieval failure
  is silent by construction — which is exactly why the telemetry + dead-man's switch is mandatory, not
  optional. The hook should log failures to `recall_log` (status=error) even when it injects nothing. [INFERENCE]

## 7. Gaps, blindspots & emergent findings

- **`mcp_tool` and `http` hook types collapse two hypothesized tiers.** The prompt framed hooks and
  MCP as separate mechanisms, but a `"type": "mcp_tool"` hook lets the `UserPromptSubmit` tier *call
  the substrate's MCP server directly* rather than shelling out to a script. [VERIFIABLE][^2] This
  means one MCP recall server can back both the automatic (hook-invoked) and the agent-driven
  (model-invoked) paths — less code, one query surface. This is an emergent design simplification the
  original three-mechanism framing missed.
- **Silent `SessionStart` injection changes the transparency design.** Since CC 2.1.0, `SessionStart`
  hooks inject silently. [VERIFIABLE][^4] Any "show the user what primed the session" UX must be
  embedded in the injected text or emitted on a separate visible channel — you cannot rely on CC
  surfacing it. This is a real, easily-missed constraint.
- **The `additionalContext` 10K cap interacts badly with graph payloads.** Graph subgraphs are verbose;
  a naive AGE traversal result can blow the 10K per-injection cap and spill to a file reference,
  defeating the point. [INFERENCE] Graph recall must be summarized/serialized compactly before
  injection, or kept on the MCP-tool path where spill-to-disk is tolerable.
- **Models under-call memory tools — the central injection blindspot.** The whole reason hooks (push)
  beat MCP tools (pull) for baseline recall is that models don't reliably choose to recall. [VERIFIABLE][^5]
  Any design that leans on "the agent will call `recall` when it needs to" will silently
  under-perform. Push (hooks) must carry the baseline; pull (MCP tool) is the deep-dive supplement.
- **Unresolved: dedup and staleness across a long session.** CC provides no native "what did I already
  inject" or "this injected memory is now stale" signal. A session-scoped seen-set and a freshness
  timestamp on each hit are needed but are the builder's responsibility. [NO SOURCE] — no CC-native
  mechanism found; flagged as a build-time concern.
- **Suspected but unresolved blindspot: injection-order / recency effects on the model.** Whether
  injecting recall "alongside the prompt" every turn degrades instruction-following or dilutes the
  actual user request (a context-rot / lost-in-the-middle effect) is plausible but I found no
  CC-specific measurement. Named here as an open risk to validate empirically, not asserted. [NO SOURCE]

## Comparison

| # | Criterion | `UserPromptSubmit` hook | `SessionStart` hook | MCP `recall` tool | MCP resources |
|---|-----------|-------------------------|---------------------|-------------------|---------------|
| 1 | Injects without model choosing | Yes (every prompt) [VERIFIABLE][^1] | Yes (session start / compact) [VERIFIABLE][^2] | No — model must call [VERIFIABLE][^3] | No — must be `@`-referenced [VERIFIABLE][^3] |
| 2 | Keyed to actual prompt | Yes (receives `prompt`) [VERIFIABLE][^2] | No (fires before first prompt) [VERIFIABLE][^1] | Yes (model passes query) | No (named reference) |
| 3 | Context/size limit | 10K chars/injection [VERIFIABLE][^1] | 10K chars/injection [VERIFIABLE][^1] | Tool-output spill-to-disk threshold [VERIFIABLE][^3] | Fetched as attachment [VERIFIABLE][^3] |
| 4 | Latency budget | 30 s hard (blocks turn) [VERIFIABLE][^1] | Session start only (less hot) | On-demand, latency-tolerant | On-demand |
| 5 | Best-fit recall type | Cheap vector+FTS top-K | Stable priming / resume index | Multi-hop graph / deep search | Pinned canonical docs |
| 6 | Failure mode if misused | Stalls/erases every prompt [VERIFIABLE][^1] | Bloats every session | Under-called by model [VERIFIABLE][^5] | Never surfaces unasked |
| 7 | Transparency | Header inside injected block | Silent since 2.1.0 [VERIFIABLE][^4] | Tool call is visible | `@`-mention is visible |

## Recommendation

Build a **three-tier recall layer, all tiers backed by one MCP recall server over the Postgres
substrate**, with hard hot-path discipline and self-documenting injection.

**Tier 1 — `SessionStart` priming hook** (matcher `startup|resume|clear|compact`). Injects a lean,
project-scoped recall block via `hookSpecificOutput.additionalContext`: recent decisions, active
work, resume state, a progressive-disclosure index of top memories for the repo. Budget ≤ ~1,200
tokens. Re-fires on `compact` to survive compaction. Keyed by `CLAUDE_PROJECT_DIR`. [VERIFIABLE][^1][^2][^5]

**Tier 2 — `UserPromptSubmit` auto-recall hook** (the RAG-into-context tier). On every prompt, query
the substrate (vector + tsvector, **not** graph — stay under the 30 s hot path) with the user's
prompt, rerank, dedup against a session-scoped seen-set, threshold, and inject the top few hits via
`additionalContext`. Hot-path rules (borrowed from SuperBrain): **no LLM on the path, exit 0 always,
never exit 2, defer any heavy work to a detached process.** Prepend the injected block with a
transparency header (query, hit sources, scores, which used). Implement as `"type": "mcp_tool"` so it
calls the same recall server as Tier 3. Budget well under 10K chars. [VERIFIABLE][^1][^5][^6]

**Tier 3 — MCP `recall` tool** the model calls explicitly for deep/multi-hop search: this is where
AGE graph traversal (embed query → ANN entry nodes → graph traverse → combine with FTS/vector chunks)
lives, latency-tolerant and off the hot path. Keep it discoverable via tool search (do not
`alwaysLoad`). Return provenance with every hit. [VERIFIABLE][^3][^8]

**Transparency + observability (mandatory).** Every query (any tier) writes a `recall_log` row
(ts, session, repo, query, hits, scores, injected bytes, latency, status). Injected blocks are
self-documenting (header inside the `additionalContext`, because `SessionStart` injection is silent).
A Grafana panel over `recall_log` + a dead-man's-switch alert (no recall events across the active
fleet in N hours → Pushover) prevents silent dormancy. [VERIFIABLE][^4][^6]

**Context-economy guardrails:** relevance threshold before injecting; top-K small; dedup against the
session seen-set; per-injection budget << 10K chars; inject nothing when nothing clears threshold;
graph payloads compacted before injection (or kept on Tier 3). [VERIFIABLE][^1] Markdown stays
authoritative; Postgres is the derived index; provenance on every injected hit guards against
retrieval-poisoning resurfacing a bad captured note. [HEURISTIC]

## Open Questions

1. Does per-turn `UserPromptSubmit` injection measurably degrade instruction-following or dilute the
   user's actual request (context-rot / lost-in-the-middle)? Needs empirical A/B on the fleet.
2. What relevance threshold + top-K minimizes noise while catching genuinely useful recall? Tune with
   `recall_log` "was it used" signals.
3. Can one `mcp_tool`-typed `UserPromptSubmit` hook meet the 30 s budget when the substrate is remote
   (an example remote database) rather than local? Measure round-trip; may force a local read
   replica or cache.
4. How is the session-scoped "already injected" seen-set best persisted across a `compact` (which
   re-fires `SessionStart` but may reset hook state)?
5. Graph-vs-cost: is AGE traversal worth wiring on day one, or ship vector+FTS recall first and add
   graph only when multi-hop questions demonstrably arise?

## Sources

[^1]: Anthropic. (2026). [Hooks reference](https://code.claude.com/docs/en/hooks). Claude Code Documentation. Verified accessible (HTTP 200) 2026-07-15. (Authoritative source for `additionalContext` mechanism, exit-code semantics, 10,000-character cap, per-event timeouts incl. `UserPromptSubmit` 30 s, which events inject context, and `UserPromptSubmit` exit-2 prompt-erase behavior.)
[^2]: Anthropic. (2026). [Automate actions with hooks (hooks guide)](https://code.claude.com/docs/en/hooks-guide). Claude Code Documentation. Verified accessible (HTTP 200) 2026-07-15. (stdout added to context for `UserPromptSubmit`/`UserPromptExpansion`/`SessionStart` on exit 0; `SessionStart` compact re-inject worked example; `additionalContext` kept from every hook; plain-text system-reminder injection; `mcp_tool`/`http` hook types; hook input includes `cwd`/`CLAUDE_PROJECT_DIR`.)
[^3]: Anthropic. (2026). [Connect Claude Code to tools via MCP](https://code.claude.com/docs/en/mcp). Claude Code Documentation. Verified accessible (HTTP 200) 2026-07-15. (MCP resources via `@` mentions auto-fetched as attachments; tool search deferred-by-default; `alwaysLoad`; large tool output spill-to-disk; `list_changed` notifications; `claude/channel` push; `CLAUDE_PROJECT_DIR` in stdio server env.)
[^4]: docs.claude-mem.ai. (2026). [Hooks architecture — Claude-Mem](https://docs.claude-mem.ai/hooks-architecture). Verified accessible (HTTP 200) 2026-07-15. (Five/six-hook lifecycle; silent `SessionStart` injection via `hookSpecificOutput.additionalContext` since CC 2.1.0; PostToolUse enqueue-and-return capture; progressive-disclosure index transparency table; SQLite storage.)
[^5]: El Khoury, A. (2026). [Claude Code Memory: Storage Is Solved, Injection Isn't (SuperBrain)](https://alexandrekhoury.com/writing/superbrain-session-memory-claude-code). Verified accessible (HTTP 200) 2026-07-15. (Working-memory framing; project-scoped `SessionStart` ~1,200-token recall block; never-block / exit-0-always / detached-distillation hot-path rules; claude-mem v3 context-pollution regression; models under-call memory tools.)
[^6]: Batistoni, N. (2026). [claude-rag-memory](https://github.com/nicolasbatistoni/claude-rag-memory). GitHub. Verified accessible (HTTP 200) 2026-07-15. (UserPromptSubmit + SessionStart + Stop hooks; SQLite + ChromaDB; "only semantically similar past inputs are injected"; per-response token-breakdown transparency; MCP `rag_query`/`rag_find_relevant_files`/`rag_project_summary` tools.)
[^7]: Basic Machines. (2026). [Basic Memory — Open Source AI Memory & Knowledge Base](https://basicmemory.com/). Verified accessible (HTTP 200) 2026-07-15. (Markdown-authoritative with full-text + semantic/vector + knowledge-graph index; hybrid search; MCP read/write; CC drop-in with "search-before-answer recall and auto-capture." Homepage does not disclose backing DB / embedding lib.)
[^8]: AgentMarketCap. (2026). [Graph RAG vs Vector RAG for Agent Memory 2026](https://agentmarketcap.ai/blog/2026/04/07/graph-rag-vs-vector-rag-agent-memory-neo4j-pgvector). Verified accessible (HTTP 200) 2026-07-15. (Hybrid composition pattern embed→ANN entry nodes→graph traversal→combine; graph multi-hop wins 86% vs 32% and Zep 63.8 vs Mem0 49.0 on LongMemEval; graph single-hop underperformance −13.4%; 2.3x latency; staleness −16.6%.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** claude-adhoc (research-doc skill, unmetered subscription bridge)
**Model:** `opus researcher`
**Date:** 2026-07-15

```text
Write a template-conformant research doc on how to wire a memory/knowledge recall + injection
layer deeply into Claude Code — the concrete CC-integration mechanisms — for a solo developer's
~18-repo Claude Code AI-agent fleet backed by a Postgres knowledge/memory substrate (pgvector +
full-text + Apache AGE graph).

OPERATING RULES:
- Read ~/.claude/skills/research-doc/SKILL.md FROM DISK for the method. Scaffold the doc from the
  canonical repository-relative research STUB.md at docs/research/STUB.md (NOT a blank file; copy
  it, keep its frontmatter + aido region markers); conform to
  docs/research/TEMPLATE.md (research-1.2.0).
- Write to docs/research/cc-memory-recall-injection-integration.md with
  frontmatter status: complete, source: "claude-adhoc-2026-07-15", template_version: "research-1.2.0".
- Use WebSearch/WebFetch ONLY. NEVER the metered aido research graph or Gemini. Zero metered cost.
- DO NOT git commit — leave the file on disk for the parent session to review + commit.
- aido validate-doc <path> must pass.

<grounding_instructions>
You are a principal engineer specializing in Claude Code / coding-agent extensibility and
retrieval-augmented agent memory — you have built hook-based and MCP-based context-injection
systems for AI coding agents in production, and you hold evidence-backed opinions on how to wire a
knowledge/memory recall layer into an agent's context loop. When you cannot find a source, you say
so explicitly.

Temporal scope: Weight sources by recency — 2026 (primary) → 2025 → 2024. Claude Code and its
hook/MCP surfaces change fast, so prefer the CURRENT official docs (code.claude.com/docs) over blog
posts; if a blog conflicts with the official docs, the official docs win and you say so. If
post-2024 literature is genuinely sparse for a subtopic, state "[subtopic]: no significant post-2024
developments found" rather than backfilling. Backfilling is a failure mode, not a hedge.

Before generating your final output, execute a Chain-of-Verification (CoVe) to ensure factual
fidelity over compliance. Inside your thought process: (1) Isolate the core facts required. (2)
Draft a tentative response. (3) Hostile Cross-Examination: flag any claim where you are citing a
source because the prompt implied you should, rather than because you verified it. (4) Strip away
any claim that cannot be empirically verified.

When generating your final output, classify every major claim. Write your rationale before appending
the tag. [VERIFIABLE][^N] (docs/peer-review/official tech blogs 2024–2026, with inline footnote ref);
[HEURISTIC] (widely accepted best practice, no citation); [INFERENCE] (logical conclusion from
context, reasoning in-text, tier tag only); [NO SOURCE] (explicitly state when no verifiable data,
tier tag only).

Citation format: inline GFM footnote ref after the tier tag; footnote defs once under ## Sources in
APA form with clickable URL/DOI + access-verification stamp; URL-or-DOI always; refs contiguous from
[^1], no gaps/orphans. Hard constraint: never invent a citation to satisfy a formatting instruction.
Accuracy > completeness. Diagrams: Mermaid/ASCII. Math: LaTeX. Never binary images.
</grounding_instructions>

## Scope note — questions, examples, and named references are a starting point, not a checklist
The questions, topics, and named examples below are illustrative anchors and a FLOOR for this
research — not an exhaustive list to answer only or evaluate only. Reason independently: survey the
landscape broadly, follow the evidence where it leads, expand scope where warranted, and surface
relevant work, factors, and failure modes not named here.

## Independent exploration (gaps, blindspots, emergent threads) — required
Treat the question list as a FLOOR, not a ceiling. Surface what this framing may be missing and
pursue each promising thread to a conclusion: adjacent/upstream factors; contrarian/disconfirming
evidence; emerging 2025–2026 practices/tools/research; known failure modes and second-order effects
(context bloat, latency, retrieval-poisoning, stale injection). Report load-bearing emergent threads
in a dedicated "Gaps, blindspots & emergent findings" subsection. NAME any blindspot you suspect but
cannot resolve.

## Retrieval enforcement
For each load-bearing claim, make at least one web_search/web_fetch. The CC hook-capability claims
MUST be grounded in the official Claude Code docs (fetch code.claude.com/docs hooks + MCP pages
directly), not memory or blogs.

## Background (assume established)
Goal: a live recall layer that injects relevant memory/knowledge/docs into CC sessions — within a
repo, across repos, across concurrent sessions — backed by ONE Postgres substrate (pgvector + tsvector
+ Apache AGE) indexing the fleet's markdown memory + docs. Markdown stays authoritative; Postgres is
the derived, queryable recall index. Three injection mechanisms hypothesized (VERIFY + refine): (1)
per-prompt auto-recall via a UserPromptSubmit hook; (2) session-start priming via SessionStart hook;
(3) agent-driven deep recall via an MCP recall/search tool. Hard requirement — transparency: every
substrate query announced in-session with what was retrieved, plus recall_log → Grafana + dead-man's
switch. Fleet: ai-harness provisions managed CC user-hooks fleet-wide; ai-core runs an MCP server;
sessions launch per-repo in git worktrees.

## Research questions (FLOOR, not ceiling)
1. CC hook injection capabilities (VERIFY against official docs — the load-bearing fact-check). Can a
   UserPromptSubmit hook inject additional context before the model responds, and exactly how (stdout?
   hookSpecificOutput.additionalContext? exit codes?)? Size/latency/blocking constraints? SessionStart
   confirm additionalContext. Other hooks (PreToolUse, PreCompact, Stop) for retrieval injection?
2. MCP tools vs MCP resources for recall — how exposed, how results land in context, tradeoffs, limits.
3. RAG-into-agent-context patterns & community/existing implementations (claude-mem, Basic Memory,
   mem0/OpenMemory, memory-keeper, UserPromptSubmit RAG hooks). UX, what works, what fails.
4. Cross-session & cross-repo shared-memory injection — scope/key per-repo vs global, freshness/notify.
5. Retrieval quality + context economy — thresholding, top-K, dedup, injection budget, when NOT to
   inject, reranking, how AGE graph composes with vector+FTS (GraphRAG).
6. Transparency & observability of retrieval — surface what was retrieved (provenance), logging +
   dashboards + "is it helping" metrics.

## Output requirements
Full research-1.2.0 conformance; 5-perspective adversarial critic pass; live-check every citation
(official CC docs especially); provenance ledger with verbatim quotes; tier tags rationale-before-tag.
End with a concrete recommended CC-integration architecture: which hooks do what (with the verified
injection mechanism), the MCP recall tool shape, the transparency/announce pattern, and the
context-economy guardrails.
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

| Claim | Source URL | Verbatim quote (from fetched page) | Verdict | Live? |
|-------|-----------|-------------------------------------|---------|-------|
| `UserPromptSubmit`/`SessionStart` stdout is added to Claude's context on exit 0 | https://code.claude.com/docs/en/hooks-guide | "For `UserPromptSubmit`, `UserPromptExpansion`, and `SessionStart` hooks, anything you write to stdout is added to Claude's context." | SUPPORTED | Yes |
| `additionalContext` injects a string into the context window as a system reminder at the hook-fire point | https://code.claude.com/docs/en/hooks | "The `additionalContext` field passes a string from your hook into Claude's context window. Claude Code wraps the string in a system reminder and inserts it into the conversation at the point where the hook fired." | SUPPORTED | Yes |
| Hook output incl. `additionalContext` is capped at 10,000 characters | https://code.claude.com/docs/en/hooks | "Hook output strings, including `additionalContext`, `systemMessage`, and plain stdout, are capped at 10,000 characters. Output that exceeds this limit is saved to a file and replaced with a preview and file path" | SUPPORTED | Yes |
| `UserPromptSubmit` lowers the hook timeout to 30 s | https://code.claude.com/docs/en/hooks | "`UserPromptSubmit` lowers the `command`, `http`, and `mcp_tool` default to 30" | SUPPORTED | Yes |
| `UserPromptSubmit` can only inject, not replace, and exit 2 erases the prompt | https://code.claude.com/docs/en/hooks | "UserPromptSubmit: can't replace the prompt; it only injects `additionalContext` alongside it" / "Blocks prompt processing and erases the prompt" | SUPPORTED | Yes |
| SessionStart compact matcher re-injects context after compaction | https://code.claude.com/docs/en/hooks-guide | "Use a `SessionStart` hook with a `compact` matcher to re-inject critical context after every compaction. Any text your command writes to stdout is added to Claude's context." | SUPPORTED | Yes |
| MCP resources are `@`-mentioned and auto-fetched as attachments | https://code.claude.com/docs/en/mcp | "MCP servers can expose resources that you can reference using @ mentions… Resources are automatically fetched and included as attachments when referenced" | SUPPORTED | Yes |
| MCP tool search defers tool defs by default; only used tools enter context | https://code.claude.com/docs/en/mcp | "Tool search is enabled by default. MCP tools are deferred rather than loaded into context upfront… Only the tools Claude actually uses enter context." | SUPPORTED | Yes |
| `alwaysLoad: true` loads all a server's tools upfront at cost of context | https://code.claude.com/docs/en/mcp | "If a server's tools should always be visible to Claude without a search step, set `alwaysLoad` to `true`… each upfront tool consumes context" | SUPPORTED | Yes |
| claude-mem injects silently via additionalContext since CC 2.1.0 with a progressive-disclosure index | https://docs.claude-mem.ai/hooks-architecture | "SessionStart hooks no longer display user-visible messages. Context is silently injected via `hookSpecificOutput.additionalContext`." / "Formats as progressive disclosure index" | SUPPORTED | Yes |
| Storage is solved, injection isn't; project-scoped SessionStart ~1,200-token recall; never-block exit-0 hot path | https://alexandrekhoury.com/writing/superbrain-session-memory-claude-code | "My AI memory problem wasn't a storage problem. It was a working-memory problem." / "~1,200-token recall block" / "No LLM call on the capture path." / "Hooks always exit 0 no matter what — a crashing hook is impossible by construction." | SUPPORTED | Yes |
| claude-rag-memory: UPS+SessionStart+Stop; only semantically similar injected; per-response token breakdown; MCP rag tools | https://github.com/nicolasbatistoni/claude-rag-memory | "only semantically similar past inputs are injected — not the entire history" / tools "rag_query()", "rag_find_relevant_files()", "rag_project_summary()" | SUPPORTED | Yes |
| Basic Memory: markdown-authoritative with FTS + semantic + knowledge-graph index | https://basicmemory.com/ | "Plain Markdown you own, with a full-text, semantic, and knowledge-graph index on top." | SUPPORTED | Yes |
| Hybrid GraphRAG composition: embed → ANN entry nodes → graph traverse → combine; graph wins multi-hop, loses single-hop, 2.3x latency | https://agentmarketcap.ai/blog/2026/04/07/graph-rag-vs-vector-rag-agent-memory-neo4j-pgvector | "Embed the query [→] ANN vector search to retrieve semantically closest entry nodes [→] Start graph traversal from those entry nodes [→] Combine the subgraph context with semantic chunks." / "GraphRAG actually underperforms vanilla RAG by 13.4%" / "2.3x higher latency than vector search" | SUPPORTED | Yes |

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

### 2026-07-15 (Run 1, ad-hoc web, opus researcher)

- Method: research-doc skill (unmetered subscription bridge; WebSearch/WebFetch only; zero metered cost).
- Retrieval: fetched official CC docs (hooks reference, hooks guide, MCP) + community sources
  (claude-mem hooks architecture, SuperBrain/El Khoury, claude-rag-memory, Basic Memory) +
  GraphRAG-vs-Vector agent-memory analysis. 8 distinct source URLs, all live-checked (HTTP 200) 2026-07-15.
- Critic pass (5 perspectives): conventional (aligns with docs); contrarian (surfaced claude-mem v3
  auto-inject regression + models-under-call-tools); historical (pre-2025 surface did not exist);
  adjacent (2026 production GraphRAG/hybrid-rerank stack); skeptic (every VERIFIABLE claim carries a
  fetched-quote provenance row; graph/reranker "2026 default" tagged HEURISTIC, not VERIFIABLE;
  dedup/staleness/injection-order-effects tagged INFERENCE/NO SOURCE).
- Load-bearing fact-check RESOLVED: UserPromptSubmit CAN inject pre-response context via stdout /
  additionalContext (system reminder, 10K-char cap, 30 s timeout). Grounded in official docs.

<!-- /doc:region name="run_history" -->
