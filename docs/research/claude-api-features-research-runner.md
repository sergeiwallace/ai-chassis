---
title: "Claude API Features for Deep Research Runner"
category: research
tags: [research, api, research-runner, claude-api, foundry]
status: complete
source: "claude-opus-4-6-2026-04-30"
registry_id: R-1
template_version: "research-1.0.0"
---
<!-- doc:region name="context" kind="immutable" -->

## Context

(none — context not recorded in original doc)

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->


# Claude API Features for Deep Research Runner

**Status:** complete

**Created:** 2026-04-30

## Executive Summary

The Claude API provides a rich feature surface for building a multi-round research runner. The
highest-impact features are **adaptive thinking with effort parameter** (replaces budget_tokens for
Opus 4.6+), **prompt caching** (critical for multi-turn cost control), **streaming** (for progress
feedback on long responses), and **batch processing** (50% discount for parallel sub-topic research).
Several other features -- citations, web search/fetch, structured outputs, and search results -- are
relevant but carry important compatibility constraints that must be factored into the design.

## Feature Inventory

### 1. Adaptive Thinking + Effort Parameter

**What it does:** Lets Claude dynamically decide when and how much to reason internally before
answering. Replaces the older manual `budget_tokens` approach.

**How to invoke (Python SDK):**

```python
response = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=16000,
    thinking={"type": "adaptive"},
    output_config={"effort": "high"},  # low | medium | high | max
    messages=[...],
)
```

**Relevance for deep research: HIGH** -- Deep research prompts require extensive reasoning. Adaptive
thinking with `effort: "high"` or `effort: "max"` triggers deep reasoning on complex prompts while
allowing the model to skip thinking on simple follow-ups, reducing wasted tokens.

**Key constraints and gotchas:**

- On **Opus 4.7**, adaptive thinking is the ONLY mode; manual `budget_tokens` is rejected (400 error)
- On **Opus 4.6**, adaptive is recommended; manual `budget_tokens` still works but is deprecated
- Effort levels: `low`, `medium`, `high` (default), `max`. Opus 4.7 also supports `xhigh`
- `max_tokens` is the hard cap for total output (thinking + response text); `budget_tokens` is NOT
  needed when using adaptive mode
- Thinking blocks are summarized by default on Claude 4 models (full thinking text requires sales contact)
- Display modes: `"summarized"` (default on Opus 4.6) or `"omitted"` (default on Opus 4.7, faster TTFT)
- **Full thinking tokens are always billed** regardless of display mode
- Thinking from previous turns is automatically stripped from context (does not consume context window)
- Interleaved thinking (thinking between tool calls) is automatic with adaptive mode

[VERIFIABLE FACT - source: platform.claude.com/docs/en/build-with-claude/adaptive-thinking]

### 2. Extended Thinking (Manual Mode -- Legacy)

**What it does:** Explicit thinking budget via `budget_tokens`. Being deprecated on Opus 4.6+.

**How to invoke (Python SDK):**

```python
response = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=16000,
    thinking={"type": "enabled", "budget_tokens": 10000},
    messages=[...],
)
```

**Relevance for deep research: MEDIUM** -- Still functional on Opus 4.6 but deprecated. Use adaptive
thinking instead. Only relevant if the runner needs to target older models.

**Key constraints:**

- `budget_tokens` must be < `max_tokens`
- Rejected on Opus 4.7 with 400 error
- With interleaved thinking and tools, `budget_tokens` can exceed `max_tokens`
- Tool choice is limited: only `auto` and `none` work (not `any` or specific tool)

[VERIFIABLE FACT - source: platform.claude.com/docs/en/build-with-claude/extended-thinking]

### 3. Prompt Caching

**What it does:** Caches prompt prefixes (system prompt, documents, conversation history) so
subsequent requests only pay 10% of the input token price for cached portions.

**How to invoke (Python SDK):**

```python
# Automatic caching (simplest)
response = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=16000,
    cache_control={"type": "ephemeral"},
    system="Your research system prompt...",
    messages=[...],
)

# Explicit breakpoints on specific blocks
response = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=16000,
    system=[
        {
            "type": "text",
            "text": "Long system prompt...",
            "cache_control": {"type": "ephemeral"},
        }
    ],
    messages=[...],
)
```

**Relevance for deep research: HIGH** -- Multi-round research reuses the same system prompt and
growing conversation history. Caching saves 90% on repeated input tokens. Critical for cost control
when running Opus at \$5/MTok input.

**Key constraints:**

- **Minimum tokens** before cache breakpoint: 4096 for Opus 4.6/4.7; 2048 for Sonnet 4.6
- Two durations: 5-minute (1.25x write cost) and 1-hour (2x write cost, set `"ttl": "1h"`)
- Cache reads: 0.1x base input price (same for both durations)
- Max 4 explicit cache breakpoints per request
- Lookback window: 20 blocks
- Changing thinking parameters invalidates message-level cache (but not system prompt cache)
- Consecutive requests using same adaptive thinking mode preserve cache breakpoints
- Pre-warming possible with `max_tokens: 0`
- Cache entries only available after first response begins

**Cost math for research runner:**

- System prompt (likely 2000-5000 tokens): cached across all rounds
- Research documents as context: cached
- Growing conversation: auto-cached with each turn
- First request: 1.25x input. Subsequent requests: 0.1x for cached portion

[VERIFIABLE FACT - source: platform.claude.com/docs/en/build-with-claude/prompt-caching]

### 4. Streaming

**What it does:** Incrementally streams response tokens via SSE, including thinking blocks.

**How to invoke (Python SDK):**

```python
with client.messages.stream(
    model="claude-opus-4-6",
    max_tokens=16000,
    thinking={"type": "adaptive"},
    messages=[...],
) as stream:
    for event in stream:
        if event.type == "content_block_delta":
            if event.delta.type == "thinking_delta":
                print(f"[thinking] {event.delta.thinking}", end="")
            elif event.delta.type == "text_delta":
                print(event.delta.text, end="")

# Or simpler text-only streaming:
with client.messages.stream(...) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
```

**Relevance for deep research: HIGH** -- Research responses are 5000-15000+ tokens. Streaming
provides progress feedback and allows early termination. Also enables writing partial results to
disk as they arrive.

**Key constraints:**

- Thinking blocks stream via `thinking_delta` events (if `display: "summarized"`)
- With `display: "omitted"`, no thinking deltas -- faster time to first text token
- Citation deltas arrive via `citations_delta` events
- Web search/fetch tool execution causes pauses in the stream

[VERIFIABLE FACT - source: platform.claude.com/docs/en/build-with-claude/streaming]

### 5. Citations

**What it does:** Claude provides exact references to source document passages when answering
questions. Returns character/page/block indices into source documents.

**How to invoke (Python SDK):**

```python
response = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=4096,
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "text",
                        "media_type": "text/plain",
                        "data": "Research source text...",
                    },
                    "title": "Source Document",
                    "citations": {"enabled": True},
                },
                {"type": "text", "text": "Summarize key findings with citations."},
            ],
        }
    ],
)
```

**Relevance for deep research: MEDIUM** -- Useful if the runner ingests source documents and needs
to trace claims back to specific passages. Not needed for pure generative research where the model
draws on its training knowledge.

**Key constraints:**

- **INCOMPATIBLE with Structured Outputs** (returns 400 error if both enabled)
- Three document types: plain text (sentence-chunked), PDF (page-based), custom content (your chunks)
- `cited_text` does NOT count toward output tokens (cost efficient)
- Slight input token increase for system prompt additions and chunking
- Works with prompt caching (apply `cache_control` on document blocks)
- Works with batch processing and token counting
- Supported on all active models except Haiku 3

[VERIFIABLE FACT - source: platform.claude.com/docs/en/build-with-claude/citations]

### 6. Search Results (RAG Citations)

**What it does:** Provides web-search-quality citations for custom knowledge bases and RAG
applications. Different from Citations -- designed for search-result-shaped data with source URLs.

**How to invoke (Python SDK):**

```python
# As top-level content
response = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=4096,
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "type": "search_result",
                    "source": "https://example.com/article",
                    "title": "Article Title",
                    "content": [{"type": "text", "text": "Article content..."}],
                    "citations": {"enabled": True},
                },
                {"type": "text", "text": "Summarize this."},
            ],
        }
    ],
)

# Or returned from tool calls (dynamic RAG)
```

**Relevance for deep research: MEDIUM** -- Useful if the runner pre-fetches web sources and feeds
them to Claude with proper attribution. Provides cleaner citation format than document-based
citations for URL-sourced content.

**Key constraints:**

- Requires `source` (URL/identifier), `title`, and `content` array
- Works in tool results (dynamic RAG) or as top-level content
- Citation format matches web search citation quality

[VERIFIABLE FACT - source: platform.claude.com/docs/en/build-with-claude/search-results]

### 7. Structured Outputs

**What it does:** Guarantees JSON schema conformance via constrained sampling. Two modes: JSON
outputs (`output_config.format`) and strict tool use (`strict: true`).

**How to invoke (Python SDK):**

```python
from pydantic import BaseModel


class ResearchFindings(BaseModel):
    summary: str
    key_findings: list[str]
    confidence: str
    open_questions: list[str]


response = client.messages.parse(
    model="claude-opus-4-6",
    max_tokens=8192,
    output_format=ResearchFindings,
    messages=[...],
)
result = response.parsed_output  # Type-safe access
```

**Relevance for deep research: HIGH** -- Guarantees parseable output for automated processing.
Essential for multi-round research where each round's output feeds the next round's prompt
construction. Pydantic integration in Python SDK is excellent.

**Key constraints:**

- **INCOMPATIBLE with Citations** (returns 400 error)
- **INCOMPATIBLE with message prefilling**
- Max 20 strict tools per request; max 24 optional parameters across all schemas
- No recursive schemas
- First request has extra latency (grammar compilation); compiled grammars cached 24h
- Check `stop_reason` -- refusals and max_tokens can produce non-conforming output
- JSON schemas cached 24h (not ZDR eligible for schema definitions)
- Works with batch processing, streaming, and token counting

[VERIFIABLE FACT - source: platform.claude.com/docs/en/build-with-claude/structured-outputs]

### 8. Web Search (Server-Side Tool)

**What it does:** Claude autonomously searches the web for real-time information. Returns results
with encrypted content and automatic citations.

**How to invoke (Python SDK):**

```python
response = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=4096,
    messages=[{"role": "user", "content": "Research the latest on..."}],
    tools=[
        {
            "type": "web_search_20250305",  # or web_search_20260209 for dynamic filtering
            "name": "web_search",
            "max_uses": 5,
            "allowed_domains": ["arxiv.org", "scholar.google.com"],
        }
    ],
)
```

**Relevance for deep research: HIGH** -- Enables the research runner to access real-time information
beyond training data cutoff. Dynamic filtering (v20260209) is particularly relevant for technical
documentation and literature review.

**Key constraints:**

- **Pricing: \$10 per 1,000 searches** plus standard token costs for results
- Dynamic filtering (v20260209) requires code execution tool to also be enabled
- Must be enabled by organization admin in Claude Console
- Results include `encrypted_content` that must be passed back for multi-turn citations
- `max_uses` limits searches per request
- Domain filtering: `allowed_domains` and `blocked_domains`
- Localization via `user_location` parameter
- Citations always enabled for web search results
- Works with batch processing and streaming

[VERIFIABLE FACT - source: platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool]

### 9. Web Fetch (Server-Side Tool)

**What it does:** Retrieves full content from specified URLs (HTML pages or PDFs) for analysis.

**How to invoke (Python SDK):**

```python
response = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=4096,
    messages=[{"role": "user", "content": "Analyze https://example.com/paper"}],
    tools=[
        {
            "type": "web_fetch_20250910",  # or web_fetch_20260209 for dynamic filtering
            "name": "web_fetch",
            "max_uses": 5,
            "citations": {"enabled": True},
            "max_content_tokens": 100000,
        }
    ],
)
```

**Relevance for deep research: MEDIUM** -- Useful for fetching specific papers, documentation pages,
or data sources. Combined with web search, enables search-then-deep-read workflows.

**Key constraints:**

- **No additional cost** beyond standard token pricing for fetched content
- Dynamic filtering (v20260209) requires code execution tool
- `max_content_tokens` parameter to limit ingested content size
- Can only fetch URLs previously seen in conversation (security restriction)
- Typical token usage: web page ~2,500 tokens; large docs ~25,000; PDF papers ~125,000
- Does not support JavaScript-rendered pages
- Optional citations (unlike web search where citations are always on)
- Works with batch processing and streaming

[VERIFIABLE FACT - source: platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool]

### 10. Advisor Tool (Beta)

**What it does:** Pairs a faster executor model (e.g., Sonnet) with a higher-intelligence advisor
model (e.g., Opus) that provides strategic guidance mid-generation.

**How to invoke (Python SDK):**

```python
response = client.beta.messages.create(
    model="claude-sonnet-4-6",  # executor
    max_tokens=4096,
    betas=["advisor-tool-2026-03-01"],
    tools=[
        {
            "type": "advisor_20260301",
            "name": "advisor",
            "model": "claude-opus-4-7",  # advisor
        }
    ],
    messages=[...],
)
```

**Relevance for deep research: LOW** -- The research runner already plans to use Opus directly. The
advisor tool is for cases where you want Sonnet to do mechanical work with occasional Opus guidance.
Could be relevant for a future cost-optimization tier where most rounds use Sonnet with Opus
advisory on complex sub-topics.

**Key constraints:**

- **Beta** -- requires `advisor-tool-2026-03-01` header
- Valid pairs: Haiku/Sonnet/Opus executor + Opus 4.7 advisor
- Advisor output does NOT stream (pause in stream while sub-inference runs)
- Advisor output typically 400-700 text tokens
- Billed at advisor model's rates for sub-inference
- `max_tokens` applies to executor only, not advisor
- Must pass `advisor_tool_result` blocks back verbatim in multi-turn

[VERIFIABLE FACT - source: platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool]

### 11. PDF Support

**What it does:** Process PDFs inline (base64 or URL) as document content blocks. Extracts text
and renders each page as an image for visual analysis.

**How to invoke (Python SDK):**

```python
import base64

with open("paper.pdf", "rb") as f:
    pdf_data = base64.standard_b64encode(f.read()).decode("utf-8")

response = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=4096,
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_data,
                    },
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": "Analyze this paper."},
            ],
        }
    ],
)

# Or via URL:
# "source": {"type": "url", "url": "https://example.com/paper.pdf"}
```

**Relevance for deep research: MEDIUM** -- Useful if the runner needs to analyze PDF papers or
reports as input context. Each page costs 1500-3000 tokens (text) plus image tokens.

**Key constraints:**

- Max 32 MB request size; max 600 pages per request (100 for 200k context models)
- Each page is converted to text + image (dual processing)
- Token cost: 1,500-3,000 tokens per page (text) plus image costs
- Works with citations, prompt caching, batch processing, and tool use
- Can be combined with Files API for repeated use

[VERIFIABLE FACT - source: platform.claude.com/docs/en/build-with-claude/pdf-support]

### 12. Files API (Beta)

**What it does:** Upload files once, reference by `file_id` in subsequent requests. Avoids
re-uploading large documents.

**How to invoke (Python SDK):**

```python
# Upload
with open("paper.pdf", "rb") as f:
    uploaded = client.beta.files.upload(file=("paper.pdf", f, "application/pdf"))

# Reference in messages
response = client.beta.messages.create(
    model="claude-opus-4-6",
    max_tokens=4096,
    betas=["files-api-2025-04-14"],
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {"type": "file", "file_id": uploaded.id},
                },
                {"type": "text", "text": "Analyze this."},
            ],
        }
    ],
)
```

**Relevance for deep research: LOW** -- Mostly useful if the same PDF/document is analyzed across
multiple separate API requests. For a single multi-turn research session, inline base64 with prompt
caching is more practical.

**Key constraints:**

- **Beta** -- requires `files-api-2025-04-14` header
- Max 500 MB per file; 500 GB per organization
- Supports PDF, plain text, images
- File operations are free; content in messages billed as input tokens
- Files persist until deleted
- Not ZDR eligible
- Rate limit: ~100 requests/min during beta

[VERIFIABLE FACT - source: platform.claude.com/docs/en/build-with-claude/files]

### 13. Batch Processing (Message Batches API)

**What it does:** Process up to 100,000 requests asynchronously at 50% cost discount.

**How to invoke (Python SDK):**

```python
batch = client.messages.batches.create(
    requests=[
        {
            "custom_id": "subtopic-1",
            "params": {
                "model": "claude-opus-4-6",
                "max_tokens": 16000,
                "thinking": {"type": "adaptive"},
                "messages": [{"role": "user", "content": "Research subtopic 1..."}],
            },
        },
        {
            "custom_id": "subtopic-2",
            "params": {
                "model": "claude-opus-4-6",
                "max_tokens": 16000,
                "thinking": {"type": "adaptive"},
                "messages": [{"role": "user", "content": "Research subtopic 2..."}],
            },
        },
    ]
)
# Poll: client.messages.batches.retrieve(batch.id)
# Results: client.messages.batches.results(batch.id)
```

**Relevance for deep research: HIGH** -- Enables parallel sub-topic research at half cost. The
"branching logic" requirement maps perfectly to batch API. Survey round identifies sub-topics;
follow-up rounds can be batched.

**Key constraints:**

- 50% discount on all token costs (Opus 4.6: \$2.50/\$12.50 per MTok)
- Max 100,000 requests or 256 MB per batch
- Most batches complete within 1 hour; 24h expiry
- Results available for 29 days
- **300k max output tokens** with `output-300k-2026-03-24` beta header (Opus 4.6/4.7, Sonnet 4.6)
- Standard max output: Opus 4.6/4.7 = 128k; Sonnet 4.6 = 64k
- Not ZDR eligible
- Each request independent (no shared state)
- Cannot use `max_tokens: 0` (no cache pre-warming in batch)
- Supports all features: thinking, tools, citations, PDFs, etc.

[VERIFIABLE FACT - source: platform.claude.com/docs/en/build-with-claude/batch-processing]

### 14. Compaction (Beta)

**What it does:** Server-side context summarization that automatically condenses older conversation
when approaching context window limits.

**How to invoke (Python SDK):**

```python
response = client.beta.messages.create(
    model="claude-opus-4-6",
    max_tokens=16000,
    betas=["compact-2026-01-12"],
    # Compaction configuration is automatic
    messages=[...],
)
```

**Relevance for deep research: MEDIUM** -- Useful for very long multi-round research sessions that
approach the 1M token context window. Less critical if the runner manages context explicitly.

**Key constraints:**

- **Beta** -- requires `compact-2026-01-12` header
- Supported on Opus 4.7, Opus 4.6, Sonnet 4.6
- Automatic -- triggers when context approaches limit
- Summarizes older parts of conversation to free space

[VERIFIABLE FACT - source: platform.claude.com/docs/en/build-with-claude/compaction]

### 15. Token Counting

**What it does:** Pre-compute token count for a message before sending it.

**How to invoke (Python SDK):**

```python
count = client.messages.count_tokens(
    model="claude-opus-4-6",
    messages=[...],
    system="...",
    tools=[...],
)
print(count.input_tokens)
```

**Relevance for deep research: MEDIUM** -- Useful for pre-flight checks before sending expensive
Opus requests, and for managing context window budgets in multi-round sessions.

[VERIFIABLE FACT - source: platform.claude.com/docs/en/api/messages-count-tokens]

## Model-Specific Reference

### Output Token Limits

| Model | Context Window | Max Output (sync) | Max Output (batch, beta) |
|-------|---------------|-------------------|--------------------------|
| Claude Opus 4.7 | 1M tokens | 128k tokens | 300k tokens |
| Claude Opus 4.6 | 1M tokens | 128k tokens | 300k tokens |
| Claude Sonnet 4.6 | 1M tokens | 64k tokens | 300k tokens |
| Claude Haiku 4.5 | 200k tokens | 64k tokens | -- |

### Pricing (per MTok)

| Model | Input | Output | Cache Write (5m) | Cache Read | Batch Input | Batch Output |
|-------|-------|--------|-----------------|------------|-------------|--------------|
| Opus 4.7 | \$5 | \$25 | \$6.25 | \$0.50 | \$2.50 | \$12.50 |
| Opus 4.6 | \$5 | \$25 | \$6.25 | \$0.50 | \$2.50 | \$12.50 |
| Sonnet 4.6 | \$3 | \$15 | \$3.75 | \$0.30 | \$1.50 | \$7.50 |

### Thinking Mode by Model

| Model | Adaptive Thinking | Manual budget_tokens | Effort Levels |
|-------|-------------------|---------------------|---------------|
| Opus 4.7 | ONLY mode | Rejected (400) | low, medium, high, xhigh, max |
| Opus 4.6 | Recommended | Deprecated but works | low, medium, high, max |
| Sonnet 4.6 | Recommended | Deprecated but works | low, medium, high, max |

**Note on Opus 4.7 tokenizer:** Opus 4.7 uses a new tokenizer that may use up to 35% more tokens
for the same text compared to previous models. Factor this into cost estimates.

## Feature Compatibility Matrix

| Feature A | Feature B | Compatible? | Notes |
|-----------|-----------|-------------|-------|
| Citations | Structured Outputs | NO | Returns 400 error |
| Citations | Prompt Caching | YES | Cache document blocks |
| Citations | Batch Processing | YES | |
| Citations | Extended Thinking | YES | |
| Structured Outputs | Batch Processing | YES | |
| Structured Outputs | Streaming | YES | |
| Structured Outputs | Prefilling | NO | |
| Web Search | Extended Thinking | YES | |
| Web Search | Batch Processing | YES | |
| Adaptive Thinking | Prompt Caching | YES | Same mode preserves cache |
| Adaptive Thinking | Tool Use | YES | Interleaved thinking auto |

## Recommendations for Research Runner Design

### Phase 1 (MVP -- Single-Round Research)

1. **Use adaptive thinking** with `effort: "high"` or `effort: "max"` for Opus 4.6
2. **Enable prompt caching** on the system prompt (automatic caching is simplest)
3. **Use streaming** for progress feedback on long research outputs
4. **Use structured outputs** (Pydantic) for parseable research output format
5. **Set `max_tokens: 16000`** minimum (research surveys need room; can go up to 128k)

### Phase 2 (Multi-Round Research)

1. **Build conversation history** across rounds, leveraging prompt caching (each round adds to cached
   prefix, subsequent rounds read from cache)
2. **Token counting** before each round to monitor context budget
3. **Structured outputs** for gap analysis (round 1 output parsed to identify gaps, round 2 prompts
   generated programmatically)
4. Consider **compaction** for sessions exceeding ~500k tokens

### Phase 3 (Parallel Sub-Topic Research)

1. **Batch API** for parallel sub-topic research at 50% cost discount
2. **300k output beta** (`output-300k-2026-03-24`) for extra-long batch responses
3. Consider **web search** for sub-topics requiring real-time data

### Features to Defer

- **Advisor tool**: Not needed when using Opus directly; revisit if adding a Sonnet cost tier
- **Files API**: Not needed unless reusing docs across separate sessions
- **Web fetch**: Only if specific URLs need deep analysis; adds token cost
- **Citations**: Only if source-document grounding is a requirement (incompatible with structured outputs)
- **Search results**: Only if building a RAG pipeline feeding pre-fetched sources

### Critical Design Constraint

**Citations and Structured Outputs are mutually exclusive.** The research runner must choose:

- **Structured outputs** (recommended for Phase 1-2): Guarantees parseable JSON for multi-round
  chaining. Essential for automated gap analysis and follow-up prompt generation.
- **Citations**: Only if the runner ingests source documents and needs verifiable references.

If both are needed, they must be used in separate API calls (e.g., structured output for the
research analysis, then a citation-enabled call for source verification).

## Open Questions

1. Does the Foundry API gateway support all features listed here (adaptive thinking, effort param,
   batch API, compaction beta)? Need to test feature availability on the gateway.
2. What is the actual token cost for a typical research survey (5000-15000 output tokens) with
   adaptive thinking on Opus 4.6? Thinking tokens are billed but not visible -- need empirical data.
3. Should the runner target Opus 4.6 or Opus 4.7? Opus 4.7 has a new tokenizer (+35% tokens for
   same text) but is more capable. Cost-performance tradeoff needs evaluation.
4. Can batch API requests use prompt caching? Docs suggest 1-hour cache is recommended for batches
   since processing takes >5 minutes.
5. Is the `output-300k-2026-03-24` beta header available on the Foundry gateway?

## Sources

- [Features Overview](https://platform.claude.com/docs/en/build-with-claude/overview) -- Feature catalog and availability
- [Adaptive Thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking) -- Adaptive mode, effort parameter integration
- [Extended Thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking) -- Manual thinking, budget_tokens, streaming, tool use
- [Effort Parameter](https://platform.claude.com/docs/en/build-with-claude/effort) -- Effort levels, model compatibility
- [Citations](https://platform.claude.com/docs/en/build-with-claude/citations) -- Document citations, incompatibility with structured outputs
- [Structured Outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs) -- JSON mode, Pydantic, schema limits
- [PDF Support](https://platform.claude.com/docs/en/build-with-claude/pdf-support) -- PDF processing, token costs
- [Web Search Tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool) -- Server-side search, pricing, dynamic filtering
- [Web Fetch Tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool) -- URL fetching, content limits
- [Advisor Tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool) -- Executor-advisor pairing, beta
- [Search Results](https://platform.claude.com/docs/en/build-with-claude/search-results) -- RAG citations
- [Prompt Caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) -- Cache durations, pricing, automatic mode
- [Batch Processing](https://platform.claude.com/docs/en/build-with-claude/batch-processing) -- 50% discount, limits, 300k output beta
- [Files API](https://platform.claude.com/docs/en/build-with-claude/files) -- Upload/reference files, beta
- [Compaction](https://platform.claude.com/docs/en/build-with-claude/compaction) -- Server-side context summarization, beta
- [Context Windows](https://platform.claude.com/docs/en/build-with-claude/context-windows) -- Context sizes, thinking interaction
- [Streaming](https://platform.claude.com/docs/en/build-with-claude/streaming) -- SSE streaming, SDK usage
- [Models Overview](https://platform.claude.com/docs/en/about-claude/models/overview) -- Model comparison, output limits, 300k beta
- [Pricing](https://platform.claude.com/docs/en/about-claude/pricing) -- Full pricing tables

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** Not registered (ad-hoc research task)
**Model:** `claude-opus-4-6` (Claude Code session)
**Date:** 2026-04-30

```text
Fetch and analyze Claude API documentation to identify all features relevant to building a deep,
multi-round research runner. The starting URL is https://platform.claude.com/docs/en/build-with-claude/overview.

Features to evaluate: Extended thinking / budget_tokens, Citations, Structured output, PDF support,
Search results / web search, Web fetch, Advisor tool, Files API, Prompt caching, Batch API,
Token counting, Streaming, Max tokens limits, model-specific features.

For each feature: what it does, Python SDK invocation, relevance score, constraints/gotchas.
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

<!-- /doc:region name="run_history" -->
