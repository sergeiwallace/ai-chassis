---
title: "AI-Native Doc Repository Frameworks — Wiki/Knowledge-Graph Patterns for an Agent-Navigated Markdown Corpus"
category: research
tags: [research, knowledge-graph, documentation, ai-agents, obsidian, graphrag, markdown, ISSUE-REF, graphify, deepwiki, repo-knowledge-graph, agent-memory, graphiti, cognee, mem0, temporal-knowledge-graph]
status: complete
source: "claude-adhoc-2026-07-05"
template_version: "research-1.2.0"
related_docs:
  - docs/README.md
---

# AI-Native Doc Repository Frameworks — Wiki/Knowledge-Graph Patterns for an Agent-Navigated Markdown Corpus

**Status:** complete

**Created:** 2026-07-05

<!--
**Task:** ISSUE-REF
**Related docs:**
- `docs/README.md`
-->

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. Wiki/knowledge-graph doc frameworks (human-first tools)](#1-wikiknowledge-graph-doc-frameworks-human-first-tools)
  - [1.1 Obsidian (wikilinks, Graph View, Bases)](#11-obsidian-wikilinks-graph-view-bases)
  - [1.2 Foam](#12-foam)
  - [1.3 Dendron (discontinued)](#13-dendron-discontinued)
  - [1.4 Logseq](#14-logseq)
  - [1.5 Quartz](#15-quartz)
  - [1.6 TiddlyWiki](#16-tiddlywiki)
  - [1.7 Notion](#17-notion)
  - [1.8 Docs-site generators — Docusaurus, MkDocs-material](#18-docs-site-generators--docusaurus-mkdocs-material)
  - [When to use](#when-to-use)
- [2. AI-native / RAG-oriented approaches](#2-ai-native--rag-oriented-approaches)
  - [2.1 Microsoft GraphRAG](#21-microsoft-graphrag)
  - [2.2 LlamaIndex / LangChain knowledge-graph RAG](#22-llamaindex--langchain-knowledge-graph-rag)
  - [2.3 "Second brain for agents" tooling (Karpathy LLM Wiki pattern)](#23-second-brain-for-agents-tooling-karpathy-llm-wiki-pattern)
  - [When to use](#when-to-use-2)
- [3. Lightweight patterns for a git-tracked Markdown corpus](#3-lightweight-patterns-for-a-git-tracked-markdown-corpus)
  - [3.1 `[[wikilink]]` conventions](#31-wikilink-conventions)
  - [3.2 Frontmatter-based typed relationships](#32-frontmatter-based-typed-relationships)
  - [3.3 Generated backlink/index layer](#33-generated-backlinkindex-layer)
  - [3.4 IWE — inclusion links + CLI retrieval](#34-iwe--inclusion-links--cli-retrieval)
  - [3.5 SQLite/graph sidecar](#35-sqlitegraph-sidecar)
  - [When to use](#when-to-use-3)
- [4. Interaction with format (Markdown assumed)](#4-interaction-with-format-markdown-assumed)
- [5. Agent-native repo-knowledge-graph tools](#5-agent-native-repo-knowledge-graph-tools)
  - [5.1 Graphify](#51-graphify)
  - [5.2 DeepWiki](#52-deepwiki)
  - [5.3 context7](#53-context7)
  - [5.4 supermemory](#54-supermemory)
  - [5.5 fieldtheory `ft` Library](#55-fieldtheory-ft-library)
  - [5.6 Sourcegraph Cody / Code Graph](#56-sourcegraph-cody--code-graph)
  - [5.7 aider repo-map](#57-aider-repo-map)
  - [5.8 Serena (LSP-based semantic retrieval)](#58-serena-lsp-based-semantic-retrieval)
  - [5.9 GraphRAG/LightRAG revisited for code](#59-graphraglightrag-revisited-for-code)
  - [Facet 2 — agent writes memories back](#facet-2--agent-writes-memories-back)
  - [When to use](#when-to-use-4)
- [6. OSS agent-memory knowledge-graph frameworks](#6-oss-agent-memory-knowledge-graph-frameworks)
  - [6.1 Graphiti / Zep — temporal (bi-temporal) knowledge graph](#61-graphiti--zep--temporal-bi-temporal-knowledge-graph)
  - [6.2 Cognee — ECL memory pipeline](#62-cognee--ecl-memory-pipeline)
  - [6.3 mem0 / Mem0g — graph memory layer](#63-mem0--mem0g--graph-memory-layer)
  - [6.4 HippoRAG (2) — PageRank over an open KG](#64-hipporag-2--pagerank-over-an-open-kg)
  - [6.5 fast-graphrag / nano-graphrag — cheap incremental GraphRAG](#65-fast-graphrag--nano-graphrag--cheap-incremental-graphrag)
  - [6.6 txtai — embeddings DB + semantic graph](#66-txtai--embeddings-db--semantic-graph)
  - [6.7 R2R (SciPhi) — production agentic-RAG with KG construction](#67-r2r-sciphi--production-agentic-rag-with-kg-construction)
  - [6.8 Khoj — self-hostable second brain over your docs](#68-khoj--self-hostable-second-brain-over-your-docs)
  - [6.9 Temporal-KG vs static-KG; GraphRAG-vs-vector tradeoff](#69-temporal-kg-vs-static-kg-graphrag-vs-vector-tradeoff)
  - [6.10 IDE/agent codebase indexing (Continue.dev, Neo4j ISSUE-REF)](#610-ideagent-codebase-indexing-continuedev-neo4j-kg-builder)
  - [Adversarial verification of §5 (2026-07-07)](#adversarial-verification-of-5-2026-07-07)
  - [Gaps, blindspots & emergent findings](#gaps-blindspots--emergent-findings)
  - [When to use](#when-to-use-6)
  - [OSS agent-memory KG comparison](#oss-agent-memory-kg-comparison)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

<!-- doc:region name="context" kind="immutable" -->

## Context

Original framing (ISSUE-REF): the ai-harness/example-user ecosystem has a growing Markdown docs corpus (designs, plans, research, roadmaps, procedures, audits) across ~13 projects, authored and navigated primarily by AI agents. Discovery today is by convention: a generated `docs/README.md` index built from YAML frontmatter (`scripts/generate_doc_index.py`), file-naming conventions, an SQLite task/doc index, and grep. The question is whether a more AI-native, wiki-style / knowledge-graph structure — explicit linking so an agent can traverse relationships, find the right doc, and keep the corpus organized as it grows — is worth adopting, and if so, which pattern. A sibling research doc covers HTML-vs-Markdown for AI docs; this doc assumes Markdown and focuses only on the organization/linking/discovery layer.

**Primary period:** 2025–2026
**Source weighting:** 2026 primary (Obsidian Bases, Logseq DB backend, GraphRAG DRIFT search, "LLM Wiki" agent-era tooling), 2025 secondary (GraphRAG academic literature, IWE, obsidiantools), pre-2024 background (Dendron, Foam, TiddlyWiki, MkDocs/Docusaurus baseline).

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

Primary focus: 2025–2026 sources, since the "AI agent navigates a doc corpus" use case is new enough that most relevant tooling (Obsidian's official CLI, Bases, GraphRAG DRIFT search, Karpathy-pattern "LLM Wiki" frameworks) shipped or matured in this window. Older, foundational PKM tools (Foam, TiddlyWiki, MkDocs/Docusaurus core) are cited for their stable linking models, which have not changed materially. No significant post-2024 developments were found for Dendron — it is dead — so it is reported as a cautionary data point rather than a live option.

## Executive Summary

1. **Adopt the lightweight pattern already half-built here, don't adopt a PKM app.** The corpus already has the two ingredients every AI-native wiki tool re-invents — typed YAML frontmatter and a generated index (`docs/README.md` via `scripts/generate_doc_index.py`) [HEURISTIC]. The gap is not "no graph," it's that `related_docs`/`superseded_by` fields exist in the template but the generator does not render them as a backlink graph yet — a ~1-2 hour fix, not a new system [INFERENCE].
2. **Obsidian-family tools (Obsidian, Foam, Quartz, Logseq) are optimized for a human clicking a graph view, not for an agent walking a graph** [VERIFIABLE][^1][^2]. Their machine-navigable layer either requires a third-party Python package (`obsidiantools`, parses vault into a NetworkX graph) [VERIFIABLE][^3] or an MCP/CLI bridge (`tiddly-mcp`, Obsidian's own CLI) [VERIFIABLE][^4] — i.e., you'd be adding a tool dependency to get what a `grep`+frontmatter-parse script gives you today for free.
3. **Dendron is dead** — its maintainers stopped active development in Feb 2023 [VERIFIABLE][^5] — ruling it out entirely; it's included here only as a documented negative result per the retrieval-floor rule.
4. **GraphRAG-style construction (Microsoft GraphRAG, LlamaIndex KnowledgeGraphIndex) is real infrastructure for querying a large, semi-structured, mostly-prose corpus at scale via ISSUE-REF entities/communities** [VERIFIABLE][^6][^7], but it's built to answer "what does this corpus say about X" from unstructured text — not to help an agent that already knows document *types* (design/plan/research/roadmap) and just needs typed navigation between them. Building it now would be solving a retrieval problem we don't have with a system whose main cost (ISSUE-REF entity/community extraction over the whole corpus, plus a graph DB) is not justified by our corpus size or query pattern [INFERENCE].
5. **A named prior-art pattern for exactly our situation exists**: Karpathy's "LLM Wiki" idea — compile knowledge once into interconnected Markdown, keep it current instead of re-deriving it every session — has been operationalized as agent skills (`/wiki-ingest`, `/wiki-query`, `/wiki-lint`) over an Obsidian vault with typed frontmatter and a generated cross-linker [VERIFIABLE][^8]. Our recommendation borrows its *shape* (typed frontmatter → generated cross-link/backlink layer → lint for broken links → agent queries the index, not a vector DB) without importing Obsidian, MCP servers, or a graph database, because our doc types are already fixed by the template and category taxonomy is already fixed by `CATEGORY_ORDER` — we don't need Obsidian's open-ended vault to get the same benefit [INFERENCE].

## 1. Wiki/knowledge-graph doc frameworks (human-first tools)

### 1.1 Obsidian (wikilinks, Graph View, Bases)

Obsidian's linking model is `[[wikilink]]` references between plain Markdown files, rendered as a force-directed Graph View, plus (2026) **Bases** — a core plugin that turns YAML frontmatter properties into database-like, filterable/sortable views (e.g. filter notes by `type: design` and `status: active`) [VERIFIABLE][^9][^1]. Machine access requires a separate layer: the `obsidiantools` Python package parses a vault into a `networkx` graph with wikilink/backlink/broken-link detection [VERIFIABLE][^3], and as of Feb 2026 Obsidian ships an official CLI exposing 100+ commands (search, note creation, content appending) for terminal/agent use [VERIFIABLE][^2]. Community consensus (dsebastien, 2026) is that ISSUE-REF wikis need a *visual* graph layer for **human** curation-awareness (spotting frontier/orphan nodes, unreviewed articles) — a need that's about human oversight of an agent-written corpus, not about the agent's own navigation [VERIFIABLE][^1].

### 1.2 Foam

Foam is VS Code + Markdown + GitHub: `[[wikilink]]`/`[[wikilink|alias]]` linking, a backlinks panel, and a "Show Graph" visualization, explicitly designed so users aren't locked into a proprietary format [VERIFIABLE][^10]. It remains actively maintained (17.3k stars, releases through May 2026) [VERIFIABLE][^10]. Machine-navigability is native only in the editor extension; there's no first-class agent API — an agent would parse the same `[[..]]` regex any tool would.

### 1.3 Dendron (discontinued)

Dendron layered a hierarchical (dot-delimited) note namespace on top of VS Code + Markdown. **Its maintainers ceased active development in February 2023** [VERIFIABLE][^5]; migration scripts exist to move a Dendron vault to Logseq [VERIFIABLE][^11]. [NO SOURCE] on any 2025-2026 revival. Excluded from consideration as a live option — a documented negative result, not an oversight.

### 1.4 Logseq

Logseq is a local-first, block-based outliner with bidirectional links and graph view, and its most consequential 2025-2026 change is a migration from flat Markdown files to a **SQLite-backed "DB version"** to fix file-based performance limits at scale [VERIFIABLE][^12]. This is directly relevant as a cautionary data point: Logseq's own maintainers concluded plain-file linking stops scaling and moved the source of truth into SQLite — which is the direction our existing SQLite doc/task index already points, independent of any wiki tool [INFERENCE].

### 1.5 Quartz

Quartz is a static-site generator purpose-built to publish an Obsidian vault as a public "digital garden," preserving the knowledge graph, rendering backlinks natively, and supporting wikilinks/transclusions out of the box [VERIFIABLE][^13]. It answers a *publishing* need (share the graph as a browsable website) rather than an *agent-navigation* need — useful later if this corpus goes public-facing, irrelevant to internal agent traversal today.

### 1.6 TiddlyWiki

TiddlyWiki's atomic "tiddler" model with tag-based relationships (extended by the TiddlyMap plugin for graph view) now has an MCP bridge (`tiddly-mcp`, `rryan/tiddly-mcp`) that exposes list/read/search/write/delete operations to AI agents, defaulting to read-only [VERIFIABLE][^14]. This is the clearest existing "MCP server in front of a wiki" precedent, but it requires migrating the entire corpus into TiddlyWiki's single-HTML-file or Node-hosted tiddler store — a full format migration, not additive tooling over existing Markdown.

### 1.7 Notion

Not independently re-verified this run beyond general knowledge: Notion offers a proprietary block-relation/database model with an official API, but it is not Markdown-native (round-tripping to Markdown is lossy) and introduces vendor lock-in and a paid-SaaS dependency inconsistent with the ecosystem's git-tracked, offline-first, no-lock-in posture [HEURISTIC].

### 1.8 Docs-site generators — Docusaurus, MkDocs-material

Docusaurus has no first-class backlink/graph feature found in this search [NO SOURCE]. MkDocs has community backlink plugins (`mkdocs-backlinks`, `mkdocs-backlinks-section-plugin`) that inject a rendered "referenced by" section per page at *build* time from static analysis of the Markdown link graph [VERIFIABLE][^15]. These are publishing-time renderers, not agent-queryable APIs — an agent would still need to parse the same source Markdown links directly; the plugin only helps human readers of the built site.

### When to use

- **Never adopt a full PKM app (Obsidian/Foam/Logseq/TiddlyWiki) as the source of truth** for this corpus: every one of them requires migrating off plain git-tracked Markdown+YAML-frontmatter conventions the ecosystem already standardized on (`title/category/tags/status/source`, `aido validate-doc`, per-project `docs/README.md` generation), for a benefit (human-clickable graph view) this ecosystem doesn't need because the audience is agents, not a human clicking notes.
- **Quartz** is worth revisiting only if/when a public-facing "browse our design docs" site is wanted.
- **TiddlyWiki + MCP** is the strongest *precedent* for "expose a doc corpus to an agent via a narrow read/write API" — its shape (narrow, read-default MCP surface over typed content units) is worth copying, its storage format is not.

## 2. AI-native / RAG-oriented approaches

### 2.1 Microsoft GraphRAG

GraphRAG is an indexing + query pipeline: it slices a corpus into text units, uses an LLM to extract entities/relationships/claims, builds a graph, runs hierarchical community detection (Leiden algorithm) over it, and generates bottom-up community summaries [VERIFIABLE][^6][^7]. At query time it offers three modes: **Global Search** (answers holistic "what does this corpus say about X" questions via community summaries), **Local Search** (fans out from a specific entity to its neighbors), and **DRIFT Search** (local search enriched with community context) [VERIFIABLE][^7]. This is genuinely different from wikilink graphs: the graph's nodes/edges are *ISSUE-REF entities*, not author-declared document links, and the indexing cost (LLM calls over the whole corpus, plus reruns as content changes) is real and non-trivial — GraphRAG's own docs warn indexing "can be an expensive operation" and recommend starting small [VERIFIABLE][^6].

### 2.2 LlamaIndex / LangChain knowledge-graph RAG

LlamaIndex's `KnowledgeGraphIndex` / `KnowledgeGraphRAGQueryEngine` build a property graph from documents (entities + relations as node metadata) and combine it with vector retrieval in hybrid patterns: semantic search narrows candidate chunks, then graph traversal pulls in structurally-connected context [VERIFIABLE][^16][^17]. LangGraph is cited as a good fit for orchestrating conditional routing between vector and graph retrieval in more complex pipelines [VERIFIABLE][^16]. As with GraphRAG, this targets *unstructured-corpus question answering at scale* (open-ended natural-language queries over prose), not *typed document discovery* (find the design doc for topic Y, follow it to its plan doc and roadmap entry) — the latter is a much smaller, already-mostly-solved problem for us via frontmatter `category`/`tags`/`related_docs`.

### 2.3 "Second brain for agents" tooling (Karpathy LLM Wiki pattern)

Andrej Karpathy's framing — compile knowledge once into interconnected Markdown and keep it current, instead of re-deriving the same answers from an LLM repeatedly — has been operationalized multiple ways in 2026:

- `ar9av/obsidian-wiki` implements it directly: agent skills (`/wiki-ingest`, `/wiki-query`, `/wiki-update`, `/wiki-lint`) over an Obsidian vault with typed YAML frontmatter (summaries, tags, provenance), a delta-tracking manifest to avoid reprocessing unchanged sources, an auto cross-linker that discovers unlinked mentions and wires them into the graph, and tiered retrieval (scan titles/summaries first, open full pages only when needed) [VERIFIABLE][^8].
- `jamesmcroft/obsidian-ai-second-brain` and related write-ups converge on the same core recipe: **consistent templates + typed frontmatter + predictable section layout so an agent can reliably traverse the graph without parsing prose**, with "links in frontmatter build machine-readable relationships" called out explicitly as the primary mechanism for typed, queryable relationships (versus wikilinks embedded in body text) [VERIFIABLE][^18].
- IWE (`iwe-org/iwe`) is a lower-level, non-Obsidian implementation of the same idea for plain Markdown: **inclusion links** (a link alone on its own line) create parent-child structure with *polyhierarchy* (a note can have multiple parents), plus ordinary inline links for cross-references; an LSP server serves human editors and a CLI (`iwe retrieve --key <topic> --depth <n>`, `iwe tree`, `iwe find`) serves agents with deterministic, depth-bounded graph retrieval instead of semantic-similarity retrieval — explicitly framed as "the agent gets exactly the documents in your knowledge graph that connect to the topic," with `--dry-run` to preview retrieval size before blowing the context window [VERIFIABLE][^19].

### When to use <!-- 2 -->

- **GraphRAG/LlamaIndex-KG**: adopt only if/when the corpus becomes large and heterogeneous enough that "which doc answers this open-ended question" genuinely requires ISSUE-REF entity relationships beyond what typed frontmatter + tags can express — e.g., cross-project synthesis over hundreds of unstructured prose docs where the *topics* aren't already tagged. Not justified for ~368 docs (current example-user count) that are already categorized and frontmatter-tagged [INFERENCE].
- **Karpathy LLM-Wiki pattern (skills + typed frontmatter + generated cross-linker + lint)**: this is the right *shape* for us — see Recommendation. The specific implementation (Obsidian vault + MCP) is more tooling than needed; the pattern (not the app) is the reusable part.
- **IWE-style inclusion links + depth-bounded CLI retrieval**: directly applicable if the corpus ever needs recursive parent/child retrieval (e.g., "give me this design doc plus everything it depends on, 2 levels deep") — currently our doc relationships are mostly flat (a plan links to its design doc, a roadmap entry links to a plan) so simple 1-hop backlinks likely suffice, but IWE's `--depth`/`--dry-run` idea is worth stealing conceptually if that changes [INFERENCE].

## 3. Lightweight patterns for a git-tracked Markdown corpus

### 3.1 `[[wikilink]]` conventions

The de facto standard across every tool surveyed (Obsidian, Foam, Logseq, Dendron, TiddlyWiki-adjacent) [VERIFIABLE][^1][^10]. We already use `[[name]]` syntax in memory files per this session's own context (`~/.claude/.../memory/`), so the syntax is familiar. Cost: needs a resolver (map `[[slug]]` → file path) since plain Markdown renderers (GitHub, most static-site tools) don't natively resolve wikilinks — GitHub renders `[[x]]` as literal text, not a link [HEURISTIC]. For a corpus meant to render correctly on GitHub (PRs, browsing) as well as being agent-parseable, standard Markdown links (`[text](path)`) or frontmatter relationship fields are more robust than bare wikilinks.

### 3.2 Frontmatter-based typed relationships

The pattern converged on independently by both the "second brain for agents" write-ups [VERIFIABLE][^18] and by this ecosystem's own existing `research-1.2.0` template, which already defines `related_docs` (list of paths) and `superseded_by` (single path) as optional frontmatter fields [HEURISTIC — confirmed directly from the template file read in this session, not a web source]. This is the single highest-leverage lightweight primitive: it's typed (the field name *is* the relationship type — `related_docs` vs `superseded_by` vs a future `depends_on`/`blocks`), it's YAML (trivially parseable by any script or agent without a Markdown-link regex), and it requires zero new tooling to *write* — only a generator to *render* it as a graph/backlink view.

### 3.3 Generated backlink/index layer

`scripts/generate_doc_index.py` (already in the example-user repo) parses every doc's frontmatter and renders `docs/README.md`, grouped by `category`, with columns for status/source/tags [confirmed by direct file read this session]. This is structurally identical to what Foam's backlinks panel or a Quartz build does — a build-time pass over frontmatter/links that produces a navigable index — except ours already covers 368 docs across 31 categories in one project alone. **The one missing piece**: the generator currently renders none of `related_docs`/`superseded_by` as a backlinks/graph section, even though the template already carries the field [confirmed by direct grep of the generator source this session — no `related_docs`/`superseded_by`/`backlink` handling found].

### 3.4 IWE — inclusion links + CLI retrieval

Covered in §2.3; repeated here because it is the closest *lightweight, no-lock-in* precedent for depth-bounded graph retrieval directly over plain Markdown (no vector DB, no ISSUE-REF pass, just link-graph traversal) [VERIFIABLE][^19]. Adopting IWE itself would mean converting our directory structure to inclusion-link hierarchies; adopting its *retrieval discipline* (bounded-depth, dry-run-sized fetches, deterministic over semantic) is directly portable to a script over our existing `related_docs` graph.

### 3.5 SQLite/graph sidecar

The ecosystem already runs an SQLite index (example-user's DB layer indexes tasks and, per the platform's dual-authority design, docs) [HEURISTIC — per platform architecture description in loaded context, not independently re-verified this run]. Logseq's own move to a SQLite backend for performance at scale [VERIFIABLE][^12] and Obsidian's `obsidiantools`→`networkx` bridge [VERIFIABLE][^3] both validate the general pattern: keep Markdown as the human/git-diffable source of truth, but materialize the link graph into a queryable sidecar (SQLite table of `doc_path, relation_type, target_path`, or a `networkx`/`graphml` export) for agents that need graph queries (shortest path, all-docs-related-to-X, orphan detection) that a flat generated index table can't answer well.

### When to use <!-- 3 -->

- **Frontmatter relationship fields**: always — lowest cost, already half-adopted, no new syntax to learn or resolver to write.
- **Generated backlink section in `docs/README.md` (or per-doc "Related docs" footer)**: adopt now — closes the gap between what the template promises and what the generator renders, at the cost of one script change.
- **SQLite graph sidecar (edges table)**: adopt only once cross-project graph queries are actually needed (e.g., "show me every doc that depends, transitively, on the architecture doc") — not justified purely to render a Comparison table today; the existing SQLite doc/task index is the natural place to add an edges table when that need arises.
- **IWE-style CLI / bounded-depth retrieval**: revisit if/when agents start needing recursive multi-hop retrieval (design → plan → sub-tasks → audits) rather than one-hop "what links here."
- **Full wiki app migration (Obsidian/Logseq/TiddlyWiki), GraphRAG/LlamaIndex-KG construction**: not now — see Recommendation for the complexity/benefit mismatch.

## 4. Interaction with format (Markdown assumed)

This doc deliberately does not re-litigate HTML-vs-Markdown (covered by a sibling research doc). Every pattern surveyed above — wikilinks, frontmatter relationship fields, generated backlink indices, IWE inclusion links, GraphRAG entity graphs — operates *on top of* plain Markdown source files; none require abandoning Markdown as the storage format. The organization/linking/discovery layer discussed here is orthogonal to and compatible with the existing Markdown-first convention.

## 5. Agent-native repo-knowledge-graph tools

The sections above cover organizing a corpus an agent already knows is *docs*. A different, newer facet (surfaced 2026-07-06 during the Beads/ISSUE-REF design-doc review and the ISSUE-REF AI-tooling pull) is turning a **whole repo — code plus docs** — into a knowledge base an agent uses to understand, navigate, query, and (in some tools) write back to. This is the "agent onboarding to an unfamiliar codebase" problem, not the "organize our own docs corpus" problem from §§1-4, so it gets its own comparison track below rather than folding into the existing table.

### 5.1 Graphify

Graphify (`Graphify-Labs`/`safishamsi`, also mirrored as `rhanka/graphify`) is a `/graphify` skill — not a documentation-organization framework — that AST-parses a codebase with tree-sitter (~40 languages), resolving cross-file `calls`/`imports`/`inherits`/`mixes_in` edges, and merges the result with docs/PDFs/papers/images into a single NetworkX graph, applying Leiden community detection for semantic clustering [VERIFIABLE][^20]. Output is three files: `graph.html` (interactive, click/filter/search in a browser), `GRAPH_REPORT.md` (ISSUE-REF highlights: key concepts, surprising connections, suggested questions), and `graph.json` (the full graph, queryable programmatically) [VERIFIABLE][^20]. It is explicitly local-first — "AST plus a local embedder build the graph with zero LLM credits; your code never leaves your machine" [VERIFIABLE][^20] — and lists 18 supported harnesses (Claude Code, Codex, OpenCode, Cursor, Gemini CLI, GitHub Copilot CLI, Aider, Amp, Devin CLI, Google Antigravity, and others) [VERIFIABLE][^20].

**Skeptic caveat (confirmed directly, not taken at face value):** Graphify is a 3-month-old, YC S26-funded company (graphifylabs.ai) with an unusually large, heavily marketed README (30 translated language links, benchmark tables, Trendshift badge, Discord, a companion Gumroad book) relative to its age [VERIFIABLE][^20]. Its own README reports self-benchmarked numbers against competitors it names directly — "LOCOMO (n=300) recall@10: 0.497 [graphify] vs mem0 0.048, supermemory 0.149" and "LongMemEval-S (n=50) QA accuracy: 76%, tied with dense RAG" [VERIFIABLE][^20] — with no third-party replication cited in the README itself. The ~78k star count is corroborated by independent coverage (Karpathy-post-triggered virality, picked up across LinkedIn/TikTok/blogs) as organic rather than farmed [VERIFIABLE][^20], but star count tracks demo virality (a clickable force-directed graph of your codebase is a good demo), not validated long-term technical depth — treat the benchmark table as a marketing claim pending independent verification, not as settled fact.

### 5.2 DeepWiki

DeepWiki (`deepwiki.com`), built by Cognition (the company behind the autonomous coding agent Devin), lets you swap `github.com` for `deepwiki.com` in any repo URL to get an auto-generated, navigable wiki — architecture diagrams, hierarchical table of contents, module-level pages, source-line-linked cross-references — plus an "Ask Devin" Q&A box, free for public repos (private repos need a Devin account) [VERIFIABLE][^21]. Cognition pre-indexed over 50,000 public repos at launch (**DeepWiki launched May 2025, and the free no-auth MCP server was announced 2025-05-22 — corrected this run; the prior draft's "2026-05-22" was a year off**) [VERIFIABLE][^21][^42].

**Critical caveat (the load-bearing one for this doc — confirmed both from the vendor's own framing and from a direct usage incident):** DeepWiki is **ISSUE-REF from source**, so it is "the map, not the territory" — it can be incomplete or subtly wrong. This was confirmed empirically, not just in theory: during the Beads/ISSUE-REF design-doc review (2026-07-06), DeepWiki correctly mapped Beads' `routing.*`/`federation.*`/`external_projects` config surface and rigs/hydration model at the architecture level, but **could not quote the exact hydration syntax** — accurate on "this feature exists and roughly how it fits," not reliable on exact command/config strings [source: this session's own prior use, per the doc's pre-existing Open Questions note]. Treat it as excellent for fast orientation in an unfamiliar codebase (get the shape of the system in minutes, not hours), but confirm every specific claim (exact syntax, exact behavior) against the repo's own README/docs/source before relying on it for anything load-bearing.

### 5.3 context7

context7 (`upstash/context7`) is an MCP server that solves a narrower, adjacent problem to Graphify/DeepWiki: not "understand this whole repo" but "give me current, version-pinned docs for a third-party library so I stop hallucinating deprecated APIs." Its two-tool flow — `resolve-library-id` (library name + query → ranked Context7-compatible ID) then a docs-retrieval call keyed to that exact ID/version — pulls documentation and code examples "straight from the source" and injects them into the prompt [VERIFIABLE][^26]. This is a general-purpose MCP tool, not infrastructure to build a repo-graph on top of; already tracked for adoption as an available MCP tool for sub-agents doing unfamiliar-library implementation work (ISSUE-REF, per cluster-3 curation) [HEURISTIC — per prior curation pass, this session].

### 5.4 supermemory

supermemory (`supermemoryai/supermemory`) is a hosted-or-self-hostable "memory and context engine" — TypeScript, Cloudflare-Workers-native (Workers/KV/Pages, Drizzle ORM) — that automatically extracts facts from conversations/documents and exposes `client.add()` (store text/conversations/URLs/HTML) and `client.profile()` (retrieve user profile + search in one call) for persistent, cross-conversation memory [VERIFIABLE][^27]. This is a genuine agent-writes-memories-back precedent (see §Facet 2 below) but it duplicates, at much greater infrastructure weight, what ai-harness's own markdown-owns-structure `MEMORY.md`/memory-file system already does for this ecosystem's session memory — a hosted Cloudflare stack is a heavy, vendor-coupled dependency for something that is currently git-tracked plain files any agent can `Read` directly, and adopting it would contradict the foundation-first/no-metered-API/no-vendor-lock-in posture already established for this platform [INFERENCE].

### 5.5 fieldtheory `ft` Library

`ft` (the fieldtheory CLI, installed locally at `/usr/local/bin/fieldtheory`) is this ecosystem's own closest existing analog to an agent-native repo-knowledge-base, confirmed by running `ft --help` / `ft library --help` directly in this session rather than reading about it: it exposes `ft ls`/`ft tree`/`ft find <query>`/`ft grep <query>` for browsing and searching a "Field Theory Library" of markdown documents, `ft cat`/`ft head`/`ft meta` for reading a document or its metadata, and — the write-back half — a full `ft library` sub-command set: `list`, `search`, `show`, **`create`, `update` (replace via stdin), `rename`, `delete`** [VERIFIABLE — confirmed via direct CLI invocation, this session]. This is a working, in-house precedent for "an agent queries a knowledge base and writes memories back to it" via a narrow CLI surface rather than a hosted API or a PKM-app migration — directly relevant to Facet 2 below, and a candidate primitive if/when this ecosystem needs a programmatic write-path into its own doc/memory corpus rather than direct file edits.

### 5.6 Sourcegraph Cody / Code Graph

Cody uses Sourcegraph's "Code Graph" — a codebase-wide analysis of how symbols/components interconnect — combined with the Sourcegraph Search API to pull context (APIs, symbols, usage patterns) from local and remote repositories, and as of 2026.1 supports "Universal Codebase Context" (embeddings + search across multiple repos, up to 1M-token context windows) [VERIFIABLE][^25]. As of July 2025, Sourcegraph discontinued Cody Free/Cody Pro — it is now Enterprise-only at \$59/user/month with an annual contract [VERIFIABLE][^25]. This rules it out on cost/lock-in grounds for an individual/small-team ecosystem regardless of technical merit; noted here as the closest "enterprise-grade" analog to Graphify/DeepWiki, not as an option under live consideration.

### 5.7 aider repo-map

Aider's repo-map is the most directly relevant *lightweight* precedent for token-budgeted, deterministic code-context selection: it tree-sitter-parses every source file into definitions/references, builds a directed graph (files as nodes, references as edges), and ranks files with NetworkX's PageRank — personalized with a 10x weight for identifiers mentioned in the current chat and a 50x weight for files already added to the chat — then binary-searches the token budget to fill in rank order [VERIFIABLE][^24]. Unlike Graphify/DeepWiki, this is not a persistent artifact to browse or query later; it is regenerated per-request as ephemeral, right-sized context for the current turn. Its PageRank-plus-context-boost ranking is a portable idea independent of adopting aider itself — directly applicable to any future "which files/docs are actually relevant to this task" selection logic this ecosystem might build.

### 5.8 Serena (LSP-based semantic retrieval)

Serena (`oraios/serena`) is an MCP toolkit that launches real language servers (the same LSP processes VS Code uses for go-to-definition) across 40+ languages, translating their answers into MCP tool results — giving an agent symbol-level operations (find-symbol, find-references, type-hierarchy, safe rename/refactor) instead of grepping text or dumping whole files [VERIFIABLE][^22]. This is a different axis from Graphify/DeepWiki (both ISSUE-REF): Serena's graph is the *exact, compiler-accurate* symbol graph a language server already maintains, with zero ISSUE-REF cost and zero risk of the "map not territory" hallucination problem DeepWiki has — the tradeoff is it only covers code (no docs/PDFs/prose layer) and requires a working language server per language.

### 5.9 GraphRAG/LightRAG revisited for code

§2.1 already covers Microsoft GraphRAG's cost profile (ISSUE-REF + Leiden clustering, "indexing can be an expensive operation," full rebuild on update) [VERIFIABLE][^6]. LightRAG (`HKUDS/LightRAG`) is the direct efficiency-oriented answer to that cost problem: a dual-layer (knowledge-graph + vector-embedding) architecture that supports **incremental updates** — "new data only needs to go through a standard graph indexing pipeline to generate a local graph, which is then directly integrated into the existing graph via set merging," avoiding GraphRAG's full-rebuild-on-update cost, and it explicitly "does not rely on inefficient community reports or multi-hop reasoning," reducing LLM calls at both index and query time [VERIFIABLE][^23]. Graphify's own graph-construction approach (NetworkX + Leiden, but AST-plus-local-embedder instead of ISSUE-REF) is closer in spirit to LightRAG's efficiency posture than to GraphRAG's ISSUE-REF one — worth noting as the reason Graphify can credibly claim "zero LLM credits" where GraphRAG cannot.

### Facet 2 — agent writes memories back

Every tool above sits somewhere on a spectrum from **read-only knowledge base** to **agent-writable memory**:

- **Read-only / query-time construction**: aider repo-map (rebuilt per-turn, never persisted for writing), Serena (reflects the codebase's actual current state via the language server; nothing to "write back" beyond the code edits themselves).
- **Persisted artifact, human/manual update**: Graphify (`graph.json`/`graph.html`/`GRAPH_REPORT.md` are regenerated by re-running the skill, not incrementally patched by an agent mid-session), DeepWiki (regenerated by Cognition's own re-indexing, not agent-writable at all).
- **Designed for agent read+write**: supermemory (`client.add()` is exactly an agent writing a memory back into a persistent store), LightRAG (incremental graph-merge is architecturally built for exactly this), and — the in-house precedent — **fieldtheory's `ft library create`/`update`/`rename`/`delete`** (§5.5), which already gives an agent a narrow, auditable write-path into a markdown knowledge base without inventing a new hosted API.

This directly ties to two open ai-core initiatives rather than being a standalone concern: **ISSUE-REF** (memory-hierarchy audit — what tiers of memory exist: session `MEMORY.md`, per-project `feedback_*.md` files, this doc corpus itself) and **ISSUE-REF** (memory lifecycle — when a memory gets created, updated, or archived) [INFERENCE — task IDs per this session's loaded context, not independently re-verified against the roadmap file in this run]. The actionable read: if this ecosystem ever wants an agent to autonomously update its own knowledge graph/memory store mid-session (not just at `/persist` time), the `ft library` write-subcommand pattern — narrow CLI verbs (`create`/`update`/`rename`/`delete`), not a hosted memory API — is the shape to extend, not a new supermemory-style service.

### When to use <!-- 5 -->

- **Graphify**: worth a trial run (not yet a standing recommendation) for onboarding to a large, unfamiliar *third-party* codebase where a visual/queryable graph would save real time — treat its own benchmark claims as unverified marketing until spot-checked, and note it targets whole-repo comprehension, not this ecosystem's own doc-organization problem (§§1-4 already cover that with less machinery).
- **DeepWiki**: adopt as a fast-orientation tool for any unfamiliar *public* repo (zero setup, point a browser at `deepwiki.com/<org>/<repo>`) — but never as a source of truth for exact syntax/config; always confirm specifics against the real README/docs before acting on them. Free MCP server makes this a low-cost "try before Graphify" first step.
- **context7**: adopt as a standing MCP tool for any sub-agent implementing against a third-party library's current API surface (already tracked, ISSUE-REF).
- **supermemory**: not for this ecosystem's internal memory (duplicates `MEMORY.md`/`feedback_*.md` at higher infra cost) — revisit only if aido/example-project ships an external-facing product needing a hosted memory API.
- **fieldtheory `ft library`**: already available in-house — the reference implementation to point to if/when ISSUE-REF/ISSUE-REF conclude an agent needs a programmatic write-path into a knowledge base.
- **Sourcegraph Cody**: not viable at current cost/scale (Enterprise-only, \$59/user/month) regardless of technical merit.
- **aider repo-map's PageRank ranking idea**: steal the *idea* (context-boosted PageRank over a reference graph, binary-search token budget) for any future "select the N most relevant docs/files for this task" logic — no need to adopt aider itself.
- **Serena**: adopt as an available MCP tool for any sub-agent doing precise multi-file code navigation/refactoring (compiler-accurate, zero hallucination risk on the code-graph itself) — orthogonal to and compatible with everything else in this doc since it only covers code, not the docs corpus.
- **LightRAG**: not now, same reasoning as GraphRAG in §2.1 (Recommendation) — but the *incremental-update* architecture is the one piece worth remembering if a future graph-sidecar (§3.5) ever needs to avoid GraphRAG's full-rebuild cost.

## 6. OSS agent-memory knowledge-graph frameworks

§5 covered repo-*comprehension* tools (understand an unfamiliar codebase). This section covers the category most relevant to this ecosystem's own memory/harness ambitions (ISSUE-REF memory-hierarchy audit, ISSUE-REF memory lifecycle): **OSS frameworks that build and maintain a knowledge graph as an agent's persistent memory** — store facts across sessions, retrieve them by graph traversal + vectors, and (the load-bearing dimension for us) let the agent *write memories back*. The pivotal 2025-2026 axis dividing this category is **temporal vs static**: whether every fact carries validity time so the graph models how knowledge *changed*, not just what is currently true [VERIFIABLE][^28]. For each tool: what it graphs, construction cost, incremental update, write-back path, license/self-hostability, and an honest fit verdict.

### 6.1 Graphiti / Zep — temporal (bi-temporal) knowledge graph

Graphiti (`getzep/graphiti`) is the reference OSS temporal knowledge graph for agents: it ingests "episodes" (messages/JSON/text), ISSUE-REF entities and relationships, and stores them with **"Explicit bi-temporal tracking with automatic fact invalidation"** — every edge records both when a fact was true in the world and when the system learned it, so superseded facts are invalidated rather than deleted [VERIFIABLE][^29]. Crucially for the update-cost problem that sinks GraphRAG, **"New data integrates immediately without batch recomputation. The graph evolves in real-time as episodes are ingested"** [VERIFIABLE][^29]. It is **Apache-2.0** and ~28k stars, but self-hosting requires standing up and operating a graph database — **"Neo4j 5.26 / FalkorDB 1.1.2 / Amazon Neptune"** or similar [VERIFIABLE][^29]. Zep, the commercial layer, **explicitly stopped maintaining its Community Edition** — "we've decided to stop maintaining and releasing Zep Community Edition… our open-source efforts will concentrate on Graphiti" — so the honest OSS surface here is Graphiti-the-engine, and the turnkey product (entity resolution, managed Neo4j, SOC2/HIPAA) is paid Zep Cloud [VERIFIABLE][^30]. **Construction cost:** ISSUE-REF (entity/relationship extraction per episode). **Write-back:** native and continuous — this is the strongest OSS "agent writes memories back" precedent in the category. **Fit:** this is the tool to study, not adopt wholesale. Its bi-temporal model answers a question our flat `related_docs` graph cannot ("what did we believe about X as of last month"), but it targets *conversational/business fact memory*, needs a graph DB, and carries per-episode LLM extraction cost — heavier than our markdown-owns-structure posture warrants today. **Trial** if/when ISSUE-REF concludes session memory needs point-in-time queries; otherwise its *bi-temporal invalidation idea* (supersede, don't delete — which `superseded_by` already gestures at) is the portable part.

### 6.2 Cognee — ECL memory pipeline

Cognee (`topoteretes/cognee`, Apache-2.0, ~27k stars, \$7.5M seed) is a self-hostable memory engine built on an **ECL (Extract, Cognify, Load)** pipeline: **"Ingest data in any format, and Cognee continuously builds a self-hosted knowledge graph"** that **"combines vector embeddings, graph reasoning, and cognitive-science-grounded ontology generation"** rather than plain RAG [VERIFIABLE][^31]. It exposes a memory *lifecycle* (add / recall / improve / forget) rather than a bare vector wrapper [VERIFIABLE][^28]. **Construction cost:** configurable but ISSUE-REF at the Cognify step. **Incremental:** yes (improve/forget). **Write-back:** yes. **Fit:** ignore as infrastructure (duplicates `MEMORY.md`/`ft library` at far higher weight, adds a graph store + LLM ontology pass), but its **explicit forget/improve verbs** are the single most useful design cue for ISSUE-REF — our current memory system has create/update but no first-class *decay/forget* primitive.

### 6.3 mem0 / Mem0g — graph memory layer

mem0 (`mem0ai/mem0`, already referenced in ISSUE-REF) is a memory layer with a graph-memory mode (Mem0g) storing entities as nodes / relationships as edges. Its headline benchmark is **"92.5 on LoCoMo"** at ~**6,956** mean tokens per retrieval call versus **"25,000+"** for full-context [VERIFIABLE][^32] — a token-efficiency story, not a graph-supremacy story (the graph variant mainly helps temporal/multi-hop reasoning). **Important adversarial note:** these numbers are **not comparable to Graphify's** self-reported "mem0 recall@10 0.048" (§5.1) — mem0's headline metric is ISSUE-REF answer accuracy on LoCoMo, a different quantity than recall@10, so Graphify's table pits its own recall metric against a number that is not mem0's headline result [INFERENCE]. Both cite LoCoMo, whose gold answers are themselves criticized as imperfect and whose "average context length is modest by 2026 standards, and the dataset does not explicitly score knowledge updates" [VERIFIABLE][^43] — i.e. **any single-benchmark memory claim in this category should be discounted**, whoever makes it. **Fit:** the OSS core is real and self-hostable, but same verdict as supermemory — a hosted-flavored memory service duplicating in-house memory; **ignore internally**, watch as a benchmark bellwether.

### 6.4 HippoRAG (2) — PageRank over an open KG

HippoRAG (`OSU-NLP-Group/HippoRAG`, NeurIPS'24) is the academically-grounded entry: it orchestrates an LLM (neocortex), an open knowledge graph (hippocampus), and **Personalized PageRank** to do single-shot multi-hop retrieval, and its follow-up **"remain[s] cost and latency efficient in online processes, while using significantly fewer resources for offline indexing compared to other graph-based solutions such as GraphRAG, RAPTOR, and LightRAG"** [VERIFIABLE][^33]. **Construction cost:** lower than GraphRAG (no community-summary pass). **Fit:** research-grade, not a drop-in product; but it's the third independent data point (with aider's repo-map §5.7 and fast-graphrag §6.5) that **PageRank over a reference graph is the recurring, cheap, deterministic ranking primitive** worth stealing for any future "rank the N most relevant docs/files" logic — no LLM extraction required.

### 6.5 fast-graphrag / nano-graphrag — cheap incremental GraphRAG

`circlemind-ai/fast-graphrag` (MIT) is the efficiency answer to Microsoft GraphRAG: it **"Leverages PageRank-based graph exploration"**, **"Supports real-time updates as your data evolves"**, and reports a **"6x costs saving"** on a fixed corpus (`$0.08` vs `$0.48`) [VERIFIABLE][^34]. `nano-graphrag` is the minimal-implementation sibling (top-k community selection instead of full map-reduce). **Fit:** the same "not now" as GraphRAG for our typed-doc corpus, but fast-graphrag + LightRAG + HippoRAG together establish that **incremental, PageRank-based, low-LLM GraphRAG is now the mature default** — so if a graph sidecar (§3.5) is ever built, the reference architecture is one of these, not classic Microsoft GraphRAG.

### 6.6 txtai — embeddings DB + semantic graph

txtai (`neuml/txtai`, Apache-2.0) is the lightest-weight, most self-hostable option: **"an embeddings database, which is a union of vector indexes (sparse and dense), graph networks and relational databases"** — it can auto-build a semantic graph over a corpus and return a graph of search results, all in a single local Python dependency with no external service [VERIFIABLE][^35]. **Fit:** the one tool in §6 that could plausibly back a future SQLite/graph sidecar (§3.5) *without* contradicting the no-vendor-lock-in / local-first posture — **trial-worthy** specifically as the sidecar engine if cross-project graph queries (§3.5, Open Q2) ever justify one, since it adds graph+vector over our own markdown without a hosted API or a separate graph DB.

### 6.7 R2R (SciPhi) — production agentic-RAG with KG construction

R2R (`SciPhi-AI/R2R`, MIT, ~8k stars) is a production retrieval system exposing **"Automatic entity & relationship extraction"** on ingestion, **"Semantic + keyword search with reciprocal rank fusion"**, an agentic reasoning layer, and a RESTful API, self-hostable via `pip install r2r` or Docker [VERIFIABLE][^36]. **Fit:** overkill for internal docs (it's a full retrieval *server*), but the closest OSS analog to "supermemory but MIT and self-hostable" — the reference if the ecosystem ever ships an *external product* needing a retrieval/memory API (per the supermemory verdict in §5).

### 6.8 Khoj — self-hostable second brain over your docs

Khoj (`khoj-ai/khoj`, AGPL-3.0, ~35k stars, YC W24) is a self-hostable "second brain": **"Get answers from the web or your docs"** across **"PDFs, Markdown, Notion, Word docs, org-mode files"**, with **"advanced semantic search"**, custom agents, and any local or cloud LLM [VERIFIABLE][^37]. It is RAG over your files, not a typed knowledge graph. **Fit:** ignore for the agent-navigation problem (it's semantic search + chat UI for a *human's* notes, AGPL is also a licensing consideration for any embedding) — noted as the mature reference for the human-facing "chat with my docs" adjacent use case, distinct from §§1-5.

### 6.9 Temporal-KG vs static-KG; GraphRAG-vs-vector tradeoff

Two literature threads frame the whole category:

- **Temporal vs static.** The 2025-2026 shift is toward temporal knowledge graphs (Graphiti/Zep, Mem0g's temporal edge) where facts carry validity intervals, precisely because agent memory must model *change* — a static entity graph silently keeps stale facts [VERIFIABLE][^28]. For a docs corpus this maps onto supersession (`superseded_by`) and staleness lint, not full bi-temporality.
- **GraphRAG vs vector RAG.** The consensus 2026 read: graph retrieval wins decisively on *multi-hop / relationship* queries (**"86% accuracy on multi-hop tasks where vector RAG scores 32%"**) but loses on cost and latency for simple semantic lookup; the emerging default is **adaptive routing** (classify the query, send it to the cheap vector path or the graph path) plus **"vector + BM25 + rerank"** as the ~15-25% accuracy uplift to exhaust *before* adding graph infrastructure [VERIFIABLE][^39]. LazyGraphRAG (deferring community summarization to query time) has collapsed the indexing-cost premium to **"under \$5 per corpus"** [VERIFIABLE][^39], weakening — but not eliminating — the "GraphRAG is too expensive" argument in §2.1. The load-bearing conclusion for us is unchanged: our corpus is typed and small, so vector+BM25+rerank (or just the existing frontmatter index) sits below the threshold where graph retrieval earns its cost.

### 6.10 IDE/agent codebase indexing (Continue.dev, Neo4j ISSUE-REF)

Two more reference points from adjacent categories:

- **Continue.dev** is the local-first exemplar of IDE codebase indexing: it **"Calls `this.embeddingsProvider.embed` to generate vectors for chunks"** locally, uses **"Tree-sitter queries (`.scm` files) to extract meaningful code entities"**, persists to **LanceDB** (vectors) + **SQLite** (metadata) on the developer's machine, and combines **"Vector search for semantic similarity"** with **"Keyword-based search"** [VERIFIABLE][^40] — the same tree-sitter-plus-local-store recipe as aider's repo-map (§5.7) and Serena (§5.8), confirming the whole IDE-indexing field has standardized on local, deterministic-plus-embeddings context, *not* hosted ISSUE-REF graphs. (Cline, Cursor, and Windsurf ship analogous proprietary indexers; Continue is the OSS reference implementation.)
- **Neo4j LLM Knowledge Graph Builder** is the canonical "documents → property graph" tool: **"an online application for turning unstructured text into a knowledge graph"** that produces **"a lexical graph of documents and chunks (with embeddings) and an entity graph with nodes and their relationships"** and supports **"GraphRAG, Vector, Text2Cypher"** retrieval, deployable locally via docker compose [VERIFIABLE][^41]. **Fit:** ignore (Neo4j dependency + LLM extraction is the exact heavyweight path we're declining), noted as the reference for what "full property-graph over docs" costs so the decision to *not* build it is grounded.

### Adversarial verification of §5 (2026-07-07)

Re-fetched the load-bearing §5 claims live this run:

- **Graphify — skeptic caveat upheld and strengthened.** The README now shows **78.8k stars** (up from ~78k) and has added a benchmark-methodology line — results are **"scored by a judge blind-validated against a second judge (90.6% agreement, Cohen's kappa 0.81)"** — but this is still Graphify's *own* harness scoring Graphify; **no independent third-party replication of the LOCOMO/LongMemEval numbers was found** in this session [VERIFIABLE][^20]. Independent coverage corroborates *both* the tool's genuine token savings (a reviewer's honest range: **"between 6.8x for code reviews and 49x for daily coding tasks in 500+ file repos"**, versus the headline 71x from one favorable monorepo benchmark) *and* live community criticism — **"The skeptics push back on what they call shady growth tactics around the GitHub star count and missing credit for early PR contributors"** [VERIFIABLE][^38]. Net: the local-first / zero-ISSUE-REF construction claim holds (**"Code is parsed locally with tree-sitter AST: deterministic, no LLM"** [VERIFIABLE][^20]); the *comparative memory-benchmark* claims remain marketing until independently replicated. Verdict unchanged: **trial on a real task, judge on your own repo, discount the benchmark table.**
- **DeepWiki — date correction.** §5.2 previously stated the free MCP server was "added 2026-05-22." It was in fact announced **2025-05-22** ("Like DeepWiki.com, it's completely free with no login or auth required") and DeepWiki itself launched **May 2025**, not 2026 [VERIFIABLE][^42]. Corrected in §5.2. The "map not territory" caveat is unchanged.
- **Serena, context7, Sourcegraph Cody, supermemory** — re-confirmed current as of this run: Serena remains free/open-source (LSP-backed, defaults to free language servers) [VERIFIABLE][^22]; the Cody Enterprise-only \$59/user/mo pricing, context7's free MCP flow, and supermemory's Cloudflare-native hosted stack all still match §5 as written [VERIFIABLE][^25][^26][^27]. No changes needed beyond the DeepWiki date.

### Gaps, blindspots & emergent findings

- **The whole agent-memory-KG category is converging on "temporal graph + vectors + incremental merge + PageRank," and every serious one is ISSUE-REF at construction.** That ISSUE-REF cost — not storage — is the real reason none of them fit a markdown-owns-structure ecosystem that wants zero metered-API dependence. Graphify's and txtai's *local-first, no-LLM* construction is the genuinely differentiating property, and it's the one to demand of anything adopted here.
- **"Map not territory" is a category-wide failure mode, not a DeepWiki quirk.** Any ISSUE-REF graph (GraphRAG, Cognee, mem0g, Neo4j-builder, DeepWiki) can assert a relationship that doesn't hold. Deterministic graphs (frontmatter fields, LSP symbol graphs, tree-sitter AST) cannot. This is the strongest single argument for keeping our structural graph author-declared (frontmatter) rather than ISSUE-REF.
- **Benchmark-farming is real in this space** (Graphify's self-run tables; LoCoMo's contested gold answers). Treat *every* LOCOMO/LongMemEval leaderboard claim, from any vendor, as directional at best.
- **Named-but-shallow (honest disclosure):** SiYuan and Anytype are local-first PKM apps that now ship MCP servers (an agent write-path, cross-referencing the TiddlyWiki-MCP precedent in §1.6), but I could not complete a live fetch of their repos this session to quote exact API surfaces — flagged as an unresolved gap rather than asserted [NO SOURCE]. LangMem, Letta/MemGPT, and Membase surfaced in the memory-framework survey [VERIFIABLE][^28] but were not individually deep-dived; if a memory-framework decision (ISSUE-REF/830) goes deep, Letta/MemGPT (the agent-OS memory model) is the most notable omission to chase next.

### When to use <!-- 6 -->

- **Graphiti**: study now, trial only if ISSUE-REF needs point-in-time/bi-temporal memory; steal "supersede don't delete."
- **Cognee**: ignore as infra; steal the explicit **forget/improve** lifecycle verbs for ISSUE-REF.
- **mem0 / supermemory / R2R**: ignore internally (duplicate in-house memory at higher weight); R2R is the MIT self-hostable reference *if* an external product needs a memory/retrieval API.
- **txtai**: **trial** as the engine for a future graph sidecar (§3.5) — local, Apache-2.0, graph+vector, no hosted dependency.
- **HippoRAG / fast-graphrag / LightRAG**: not now; the shared **PageRank-over-a-reference-graph** ranking primitive is the reusable idea.
- **Continue.dev / Neo4j ISSUE-REF**: reference points, not adoptions — they confirm the local-first (Continue) vs heavyweight (Neo4j) poles of the tradeoff.

### OSS agent-memory KG comparison

| # | Tool | What it graphs | Temporal | Construction cost | Incremental | Write-back | License / self-host | Fit for ai-core |
|---|------|---|---|---|---|---|---|---|
| 1 | Graphiti / Zep | Conversational/business facts | **Bi-temporal** [^29] | ISSUE-REF (per episode) | Yes — no batch recompute [^29] | Native, continuous | Apache-2.0; needs Neo4j/FalkorDB [^29][^30] | Study; trial if ISSUE-REF needs time-travel |
| 2 | Cognee | Any-format → ontology KG | Partial | LLM at Cognify | Yes (improve/forget) | Yes | Apache-2.0; self-host [^31] | Ignore infra; steal forget/improve verbs |
| 3 | mem0 / Mem0g | User/agent/session facts | Mem0g temporal edge | ISSUE-REF | Yes | Yes | OSS core + hosted [^32] | Ignore internally; benchmark bellwether |
| 4 | HippoRAG (2) | Open KG + PPR | No | Lower than GraphRAG [^33] | Partial | No | OSS (NeurIPS) [^33] | Steal PageRank primitive |
| 5 | fast-graphrag | Entity graph + PageRank | No | **6x cheaper than GraphRAG** [^34] | Yes, real-time [^34] | Yes | MIT; self-host [^34] | Reference arch for a future sidecar |
| 6 | txtai | Semantic graph + vectors | No | Local, low | Yes | Yes | Apache-2.0; **local, no service** [^35] | **Trial** as §3.5 sidecar engine |
| 7 | R2R | Auto entity/relationship | No | LLM at ingest | Yes | Yes | MIT; Docker/pip [^36] | Ignore internal; ref for external product |
| 8 | Khoj | RAG over your files (no KG) | No | Embedding index | Yes | Yes | AGPL-3.0; self-host [^37] | Ignore (human "chat with docs") |

## Comparison

| # | Criterion | Frontmatter relationship fields + generated backlinks (recommended) | Full PKM app (Obsidian/Logseq/Foam/TiddlyWiki) | GraphRAG / LlamaIndex-KG entity extraction | IWE inclusion-link CLI |
|---|-----------|---|---|---|---|
| 1 | Storage format | Plain Markdown + YAML, git-native | Vault-specific (Obsidian) or file-based-migrating-to-SQLite (Logseq) or tiddler HTML (TiddlyWiki) | Plain source + separate graph DB/index artifact | Plain Markdown, own indexing |
| 2 | New tooling required | ~1-2 hrs: extend existing `generate_doc_index.py` | New app + vault migration + (for agent access) `obsidiantools`/MCP bridge | LLM extraction pipeline + graph store (Leiden clustering, community summaries) | New CLI/LSP install (Rust binary), restructure into inclusion hierarchy |
| 3 | Agent traversal model | Deterministic: read frontmatter field, follow path | Indirect: regex-parse wikilinks, or install `obsidiantools`/MCP | Semantic + graph: entity/community lookup, 3 search modes | Deterministic: `retrieve --depth N` over inclusion graph |
| 4 | Vendor lock-in risk | None | Low-medium (Markdown underneath, but conventions/plugins are app-specific) | Medium (graph DB + ISSUE-REF pipeline become load-bearing infra) | Low (plain files, but a specific link-syntax convention) |
| 5 | Fits current corpus scale (~368 docs, 1 project; ~13 projects total) | Yes — matches what's already there | Overkill — solves human-clicking-a-graph, not agent-navigation | Overkill — solves open-ended prose QA at scale, not typed-doc discovery | Plausible fit but requires restructuring existing category/folder layout |
| 6 | Ongoing cost as corpus grows | Linear, cheap (regenerate index) | Migration + vault maintenance | Re-run extraction/indexing as docs change (flagged as "expensive") [VERIFIABLE][^6] | Re-run CLI indexing; low but non-zero |


### Agent-native repo-knowledge-graph tools comparison

| # | Criterion | Graphify | DeepWiki | context7 | supermemory | fieldtheory `ft` Library | Serena | aider repo-map |
|---|-----------|---|---|---|---|---|---|---|
| 1 | What it graphs | Code (tree-sitter AST) + docs/PDFs/images, merged | Whole repo, ISSUE-REF into wiki pages | Third-party library docs only (not the user's repo) | Conversations/documents, user-added | This ecosystem's own markdown docs | Code only (compiler-accurate LSP symbol graph) | Code only (per-turn, ephemeral) |
| 2 | Construction cost | Local (tree-sitter + local embedder), "zero LLM credits" per vendor [VERIFIABLE][^20] | Cognition-side LLM generation (~\$300k compute at launch, per vendor) [VERIFIABLE][^21] | None — on-demand doc fetch | LLM extraction per `add()` call | None — hand-authored + existing generator | None — LSP already runs locally | Cheap — PageRank over tree-sitter tags, no LLM |
| 3 | Accuracy risk | Self-reported benchmarks, no third-party replication in README [VERIFIABLE][^20] | ISSUE-REF — can be incomplete/subtly wrong on exact syntax (confirmed by direct use) | Low — served verbatim from source docs | Low for stored facts; extraction quality varies | None — it's our own authored content | None — reflects actual compiler state | None — deterministic ranking, no generation |
| 4 | Agent write-back | No (regenerate-only) | No (Cognition-side only) | N/A (read-only by design) | Yes — `client.add()` | Yes — `library create/update/rename/delete` | N/A (edits are code edits, not graph edits) | No (ephemeral, rebuilt per-turn) |
| 5 | Cost/lock-in | Free CLI, YC-funded co. behind it | Free for public repos; private needs Devin account | Free MCP server | Hosted SaaS or self-host (Cloudflare stack) | Already owned/local | Free, local, open-source | Free, local, open-source |
| 6 | Fit for ai-core today | Trial-worthy for unfamiliar 3rd-party repos, not for our own corpus | Adopt for fast orientation only, verify specifics | Adopt as standing MCP tool | Ignore internally (duplicates `MEMORY.md`) | Already in use — extend, don't replace | Adopt as standing MCP tool | Steal the ranking idea, not the tool |

## Recommendation

**Extend, don't replace.** Adopt frontmatter-based typed relationships + a generated backlink layer over the existing `docs/README.md` index; do not adopt any full wiki/PKM app or a GraphRAG/LlamaIndex-style entity-extraction pipeline at current scale.

Concretely:

1. **Wire up the relationship fields that already exist in the template.** `research-1.2.0` (and by extension design/plan templates should get the same) already defines `related_docs` and `superseded_by`. Extend `scripts/generate_doc_index.py` to (a) render a "Related / superseded by" line under each doc's row or a footer section, and (b) compute reverse edges (backlinks) — "docs that declare this doc as a `related_docs`/`superseded_by` target" — the same information Obsidian's Graph View or Foam's backlinks panel surfaces, generated from data that already exists.
2. **Standardize on frontmatter fields over inline `[[wikilinks]]`** for structural relationships, because (a) they're typed (the field name carries meaning), (b) they render correctly as plain text on GitHub (no broken `[[..]]` literal-text problem), and (c) they need no resolver — just a path string. Keep using standard Markdown `[text](path)` links in body prose for narrative cross-references; reserve frontmatter fields for the load-bearing structural graph (design→plan→roadmap chains, supersession chains).
3. **Add a lightweight orphan/broken-link lint**, borrowed conceptually from the `/wiki-lint` skill in the Karpathy-pattern implementations [VERIFIABLE][^8] — a small script check (could live in `aido validate-doc` or a new `scripts/check_doc_links.py`) that flags: `related_docs`/`superseded_by` paths that don't resolve, and docs with zero inbound or outbound relationships (candidates for either linking up or archiving).
4. **Defer a graph sidecar (SQLite edges table or `networkx`/GraphML export)** until there's an actual query need a flat generated table can't answer — e.g., "everything transitively downstream of the architecture doc." When that need arrives, the natural implementation is one more table in the SQLite index the platform already runs (per the dual-authority markdown-owns-structure/SQLite-owns-state design), not a new graph database.
5. **Do not adopt Obsidian, Foam, Logseq, TiddlyWiki, Quartz, GraphRAG, or LlamaIndex-KG for internal doc navigation now.** Each solves a real problem (human graph-browsing, public digital-garden publishing, open-ended prose QA at scale) that this corpus does not currently have, given docs are already typed, categorized, and frontmatter-tagged. Revisit Quartz if/when docs go public-facing; revisit GraphRAG/LlamaIndex-KG only if the corpus becomes large and unstructured enough that typed frontmatter navigation genuinely stops answering "which doc has the answer" — not likely at ~368 docs/project with an existing category taxonomy.

6. **Adopt a small, targeted subset of the agent-native repo-knowledge-graph tools (§5) as available tools, not as infrastructure to build on.** Add **context7** and **Serena** to the MCP allowlist for sub-agents doing unfamiliar-library implementation or precise multi-file refactoring (both already narrow, drop-in, no architecture change). Treat **DeepWiki** as a standing fast-orientation step whenever an agent needs to onboard to an unfamiliar *public* third-party repo — always followed by verifying exact syntax/config against the real source, never taken as authoritative on its own. **Trial Graphify** on one real unfamiliar-codebase task before deciding further — its benchmark claims are self-reported and unverified, so the trial should judge it on our own task, not its README. **Do not adopt supermemory or Sourcegraph Cody** — supermemory duplicates this ecosystem's own `MEMORY.md`/`feedback_*.md` system at far higher infrastructure cost, and Cody is Enterprise-only at a price point this scale doesn't justify. **Keep `fieldtheory ft library`** as this ecosystem's own reference implementation for "agent writes memories back" (`create`/`update`/`rename`/`delete`) — it already solves, in-house and at zero added cost, the write-back problem supermemory/Graphify solve with heavier infrastructure; extend it rather than replacing it if ISSUE-REF/ISSUE-REF conclude a programmatic write-path is needed beyond direct file edits.

7. **On OSS agent-memory KG frameworks (§6): adopt none as infrastructure; mine three for design cues.** The whole category (Graphiti, Cognee, mem0, HippoRAG, fast-graphrag, R2R) is ISSUE-REF at construction and duplicates, at graph-DB weight, what `MEMORY.md`/`ft library` already do locally — adopting any contradicts the markdown-owns-structure / no-metered-API posture. But three design cues are worth importing regardless: (a) **Graphiti's bi-temporal "supersede, don't delete"** — our `superseded_by` field is the seed; formalize invalidation-over-deletion in ISSUE-REF's lifecycle. (b) **Cognee's explicit forget/improve verbs** — our memory system has create/update but no first-class *decay* primitive; ISSUE-REF should add one. (c) **PageRank over a reference graph** (HippoRAG, fast-graphrag, aider repo-map all independently) — the cheap, deterministic, no-LLM ranking primitive to reuse for any future "rank the N most relevant docs/files" logic. The one tool worth an actual **trial** is **txtai** — Apache-2.0, fully local, graph+vector in a single Python dependency — but only as the *engine* for the graph sidecar deferred in step 4, not as a memory service. Everything ISSUE-REF (GraphRAG, Cognee, Neo4j-builder, DeepWiki-style wikis) inherits the category-wide **"map not territory"** hallucination risk; keep our structural graph **author-declared (frontmatter), not ISSUE-REF**.

This keeps the complexity/benefit ratio proportional per the ecosystem's calibration standard: the existing convention already gets ~80% of the benefit of a full knowledge-graph tool; the 1-2 hour extension in step 1 plus a small lint script in step 3 closes most of the remaining gap without adding a new format, app, or infra dependency.

## Open Questions

1. Should `related_docs`/`superseded_by` (and a future `depends_on`) be promoted from research-doc-only optional fields into the design/plan/roadmap templates too, so the relationship graph spans all doc kinds, not just research docs?
2. Is a per-project generated backlink section sufficient, or does cross-project doc linking (a example-user design doc referencing an aido design doc) need the sidecar/SQLite-edges approach sooner than "not now" suggests, given the platform already indexes tasks cross-project?
3. Once agents are writing more of this corpus than humans read it, does the "human needs a visual graph to spot orphan/frontier nodes" argument from the Obsidian-Bases article [^1] end up mattering here too — i.e., does the lint script from step 3 need a human-facing summary view, or is a CI-style pass/fail check enough?

4. If ISSUE-REF/ISSUE-REF conclude this ecosystem needs an agent to write memories back into a knowledge base mid-session (not just at `/persist` time), should that write-path extend `fieldtheory ft library create`/`update` (§5.5) directly, or does it warrant a purpose-built primitive inside example-user's own SQLite doc/task index — and does the answer differ for session memory (`MEMORY.md`) versus the structural docs corpus (design/plan/research docs)?

5. Should ISSUE-REF's memory lifecycle adopt **bi-temporal invalidation** (Graphiti's supersede-don't-delete, §6.1) and an **explicit forget/decay verb** (Cognee's forget/improve, §6.2)? Our current model has create/update but no first-class decay — is a stale-but-retained memory a bug the lifecycle should actively age out, and if so by what signal (last-referenced time, contradiction, explicit user retraction)?

6. If a graph sidecar (§3.5) is ever built, is **txtai** (§6.6 — local, Apache-2.0, graph+vector, no service) the right engine, or does a plain SQLite edges table over `related_docs`/`superseded_by` suffice? The breakpoint is whether cross-project, multi-hop, or semantic-similarity graph queries are actually needed — txtai only earns its dependency above that line.

> **Update (2026-07-07): deepened** — added §6 (OSS agent-memory KG frameworks: Graphiti/Zep, Cognee, mem0/Mem0g, HippoRAG, fast-graphrag/nano-graphrag, txtai, R2R, Khoj), an adversarial re-verification of §5 (Graphify skeptic caveat strengthened; DeepWiki launch date corrected 2026→2025; Serena/context7/Cody/supermemory re-confirmed current), a wider sweep (temporal-vs-static KG, GraphRAG-vs-vector tradeoff + LazyGraphRAG, Continue.dev local-first IDE indexing, Neo4j LLM-ISSUE-REF), a new OSS-agent-memory comparison table, Recommendation point 7, Open Questions 5-6, and a "Gaps, blindspots & emergent findings" subsection. The "⚠️ Refine pending" note below is resolved.
>
> <details><summary>Original 2026-07-06 refine-pending note (resolved — retained for provenance)</summary>
>
> **⚠️ Refine pending (ISSUE-REF, 2026-07-06)** — this doc surveys human-first wiki tools + RAG + markdown-corpus patterns, but does **not** yet cover the newer **agent-native repo-knowledge-graph tools** that turn a whole repo (code + docs) into a queryable/navigable knowledge base an agent uses to understand, navigate, query, and write memories back. Add in the `/research-doc` refine, each **cross-checked against primary sources** (README/repo docs/user reports), not taken at face value:
> - **Graphify** (Graphify-Labs/safishamsi, 78k★) — tree-sitter AST-parse of codebase + docs/PDFs → `graph.json`/`graph.html`/`GRAPH_REPORT.md`, local-first, `/graphify` skill across 15+ harnesses.
> - **DeepWiki** (deepwiki.com, by Cognition Labs / the Devin team) — points at `deepwiki.com/<org>/<repo>` and ingests the codebase to auto-generate a navigable wiki (architecture overviews, per-subsystem pages) + a Q&A box; free for public repos. **Caveat (map, not territory):** it's ISSUE-REF from source, so it can be **incomplete or subtly wrong** (e.g. it may confirm a feature exists but not quote exact command syntax) — excellent for fast orientation in an unfamiliar codebase, but **confirm every claim against primary sources (the repo README/docs, the author's own writing, real-user reports) before relying on it.** (Surfaced 2026-07-06 during the Beads/ISSUE-REF design-doc review, where it mapped Beads' `routing.*`/`federation.*`/`external_projects` config + rigs/hydration model but couldn't quote exact hydration syntax.)
> - **context7** (library-API-docs MCP, → ISSUE-REF), **supermemory** (memory/context engine), **fieldtheory `ft` Library** (wikis/briefs/`ls`/`tree`/`find`/`grep`); also Sourcegraph/Cody, aider repo-map, Serena/LSP-based context, GraphRAG/LightRAG.
> - **Facet 2 — agent-writes-memories-back:** ties to ISSUE-REF (memory-hierarchy audit) + ISSUE-REF (memory lifecycle). Seeds: `example-user/docs/curation/cluster{2,3}-*.md`.
>
> **Update (2026-07-06):** addressed below in the new `## 5. Agent-native repo-knowledge-graph tools` section (Graphify, DeepWiki, context7, supermemory, fieldtheory `ft` Library, Sourcegraph Cody, aider repo-map, Serena, LightRAG all surveyed and cross-checked against primary sources; Comparison/Recommendation updated accordingly).
>
> </details>

## Sources

[^1]: dsebastien. (2026). [I Built a Graph Explorer for Obsidian Bases. Here's Why LLM Wikis Need a Visual Layer](https://www.dsebastien.net/i-built-a-graph-explorer-for-obsidian-bases-heres-why-llm-wikis-need-a-visual-layer/). dsebastien.net. Verified accessible (HTTP 200) 2026-07-05.
[^2]: Volodymyr Pavlyshyn. (2026). [Obsidian, Supercharged: The AI Revolution in Personal Knowledge Management](https://volodymyrpavlyshyn.substack.com/p/obsidian-supercharged-the-ai-revolution). Substack. Verified accessible (HTTP 200) 2026-07-05. (Cites Obsidian's official CLI, shipped Feb 2026, 100+ commands.)
[^3]: obsidiantools maintainers. (2026). [obsidiantools](https://pypi.org/project/obsidiantools/). PyPI. Verified accessible (HTTP 200) 2026-07-05.
[^4]: rryan. (2026). [tiddly-mcp: Model Context Protocol server for TiddlyWiki](https://github.com/rryan/tiddly-mcp). GitHub. Verified accessible (HTTP 200) 2026-07-05.
[^5]: Brian Lovin (Hacker News discussion, citing Dendron maintainers). (2023/2026 aggregation). [Dendron: A personal knowledge management solution built on VS Code](https://brianlovin.com/hn/29998680). Hacker News mirror. Verified accessible (HTTP 200) 2026-07-05. (Maintainers announced cessation of active development, Feb 2023.)
[^6]: Microsoft. (2026). [GraphRAG](https://microsoft.github.io/graphrag/). Microsoft GitHub Pages (official docs). Verified accessible (HTTP 200) 2026-07-05.
[^7]: Microsoft. (2026). [microsoft/graphrag](https://github.com/microsoft/graphrag). GitHub. Verified accessible (HTTP 200) 2026-07-05.
[^8]: ar9av. (2026). [obsidian-wiki: Framework for AI agents to build and maintain a digital brain through Obsidian wiki using Karpathy's LLM Wiki pattern](https://github.com/ar9av/obsidian-wiki). GitHub. Verified accessible (HTTP 200) 2026-07-05.
[^9]: Obsidian. (2026). Bases (core plugin) — referenced via dsebastien.net analysis, [I Built a Graph Explorer for Obsidian Bases](https://www.dsebastien.net/i-built-a-graph-explorer-for-obsidian-bases-heres-why-llm-wikis-need-a-visual-layer/). Verified accessible (HTTP 200) 2026-07-05. (Describes Bases as query-driven, database-like note views.)
[^10]: foambubble. (2026). [foam](https://github.com/foambubble/foam). GitHub. Verified accessible (HTTP 200) 2026-07-05.
[^11]: fnurl. (2026). [dendron2logseq: Script to turn a dendron vault into a Logseq graph](https://github.com/fnurl/dendron2logseq). GitHub. Verified accessible (HTTP 200) 2026-07-05. (Corroborates Dendron end-of-life via migration tooling's existence.)
[^12]: Matthew Bellringer / aggregated PKM-tools survey. (2026). [Tech: Personal Knowledge Management (PKM) tools](https://www.dawidsblog.com/posts/tech_pkm_tools/). Verified accessible (HTTP 200) 2026-07-05. (Describes Logseq's 2025-2026 SQLite-backed "DB version" migration.)
[^13]: jackyzha0. (2026). [Quartz](https://quartz.jzhao.xyz/) / [quartz GitHub repo](https://github.com/jackyzha0/quartz). Verified accessible (HTTP 200) 2026-07-05.
[^14]: mcpmarket. (2026). [Tiddly: AI Agent Integration for TiddlyWiki Content](https://mcpmarket.com/server/tiddly). Verified accessible (HTTP 200) 2026-07-05.
[^15]: danodic-dev. (2026). [mkdocs-backlinks](https://github.com/danodic-dev/mkdocs-backlinks); six-two. [mkdocs-backlinks-section-plugin](https://github.com/six-two/mkdocs-backlinks-section-plugin). GitHub. Verified accessible (HTTP 200) 2026-07-05.
[^16]: Brian James Curry. (2026). [GraphRAG: The Complete Guide to Graph-Powered Retrieval-Augmented Generation](https://medium.com/@brian-curry-research/graphrag-the-complete-guide-to-graph-powered-retrieval-augmented-generation-eeb58a6bb4d1). Medium. Verified accessible (HTTP 200) 2026-07-05.
[^17]: LlamaIndex. (2026). [Knowledge Graph RAG Query Engine](https://developers.llamaindex.ai/python/examples/query_engine/knowledge_graph_rag_query_engine/). LlamaIndex developer docs. Verified accessible (HTTP 200) 2026-07-05.
[^18]: James Croft / MindStudio. (2026). [Building out your Knowledge Graph in Markdown](https://www.jamescroft.co.uk/building-out-your-knowledge-graph-in-markdown/); [jamesmcroft/obsidian-ai-second-brain](https://github.com/jamesmcroft/obsidian-ai-second-brain). Verified accessible (HTTP 200) 2026-07-05.
[^19]: iwe-org. (2026). [iwe](https://github.com/iwe-org/iwe). GitHub. Verified accessible (HTTP 200) 2026-07-05.
[^20]: Graphify-Labs / safishamsi. (2026). [graphify](https://github.com/safishamsi/graphify). GitHub. Verified accessible (HTTP 200) 2026-07-06. (~78k stars, YC S26; tree-sitter AST, graph.html/GRAPH_REPORT.md/graph.json outputs, "zero LLM credits" claim, 18-harness support list, self-reported LOCOMO/LongMemEval benchmark table.)
[^21]: Cognition. (2026). [DeepWiki: AI docs for any repo](https://cognition.com/blog/deepwiki). Cognition blog (official). Verified accessible (HTTP 200) 2026-07-06. (Built by the Devin team; free for public repos; 50,000+ repos pre-indexed at launch.)
[^22]: oraios. (2026). [serena](https://github.com/oraios/serena). GitHub. Verified accessible (HTTP 200) 2026-07-06. (LSP-backed MCP toolkit, 40+ languages, symbol-level semantic retrieval/editing.)
[^23]: HKUDS. (2025). [LightRAG](https://github.com/HKUDS/LightRAG). GitHub (EMNLP 2025 paper companion repo). Verified accessible (HTTP 200) 2026-07-06. (Dual-layer KG+vector architecture; incremental set-merge updates; lower ISSUE-REF cost than GraphRAG.)
[^24]: aider (Paul Gauthier). (2023). [Building a better repository map with tree sitter](https://aider.chat/2023/10/22/repomap.html). aider.chat (official blog). Verified accessible (HTTP 200) 2026-07-06. (Tree-sitter tags → PageRank-ranked file graph, context-boosted, token-budget binary search.)
[^25]: Sourcegraph. (2026). [How Cody understands your codebase](https://sourcegraph.com/blog/how-cody-understands-your-codebase); [Cody Context docs](https://sourcegraph.com/docs/cody/core-concepts/context). Sourcegraph (official). Verified accessible (HTTP 200) 2026-07-06. (Code Graph + Search API context retrieval; Cody Free/Pro discontinued July 2025, Enterprise-only \$59/user/month.)
[^26]: Upstash. (2026). [context7](https://github.com/upstash/context7). GitHub. Verified accessible (HTTP 200) 2026-07-06. (`resolve-library-id` → version-pinned docs retrieval MCP tool flow.)
[^27]: supermemoryai. (2026). [supermemory](https://github.com/supermemoryai/supermemory). GitHub. Verified accessible (HTTP 200) 2026-07-06. (Cloudflare-Workers-native memory/context engine; `client.add()`/`client.profile()` API.)
[^28]: Graphlit. (2026). [AI Agent Memory Frameworks in 2026: Memory vs. Context](https://www.graphlit.com/blog/survey-of-ai-agent-memory-frameworks). Graphlit Blog. Verified accessible (HTTP 200) 2026-07-07. (Survey table: Graphiti/Zep temporal + OSS, Mem0/supermemory hosted, Cognee lifecycle; "Memory and context are related, but they are not the same layer.")
[^29]: getzep. (2026). [graphiti: Build Real-Time Knowledge Graphs for AI Agents](https://github.com/getzep/graphiti). GitHub. Verified accessible (HTTP 200) 2026-07-07. (~28.4k stars, Apache-2.0; "Explicit bi-temporal tracking with automatic fact invalidation"; "New data integrates immediately without batch recomputation"; requires Neo4j 5.26 / FalkorDB / Amazon Neptune.)
[^30]: Zep. (2026). [Announcing a New Direction for Zep's Open Source Strategy](https://blog.getzep.com/announcing-a-new-direction-for-zeps-open-source-strategy/). Zep Blog. Verified accessible (HTTP 200) 2026-07-07. ("we've decided to stop maintaining and releasing Zep Community Edition… our open-source efforts will concentrate on Graphiti"; Apache 2.0.)
[^31]: topoteretes. (2026). [cognee: open-source AI memory platform](https://github.com/topoteretes/cognee). GitHub. Verified accessible (HTTP 200) 2026-07-07. (~27.3k stars, Apache-2.0; ECL pipeline; "Ingest data in any format, and Cognee continuously builds a self-hosted knowledge graph"; "combines vector embeddings, graph reasoning, and cognitive-science-grounded ontology generation".)
[^32]: Mem0. (2026). [Mem0 Research: Token-Efficient Memory](https://mem0.ai/research). mem0.ai. Verified accessible (HTTP 200) 2026-07-07. ("Mem0's new token-efficient memory algorithm hits 92.5 on LoCoMo" at ~6,956 tokens/retrieval vs "25,000+" for full-context.)
[^33]: OSU-NLP-Group. (2025). [HippoRAG README](https://github.com/OSU-NLP-Group/HippoRAG/blob/main/README.md). GitHub (NeurIPS'24). Verified accessible (HTTP 200) 2026-07-07. (RAG + Knowledge Graphs + Personalized PageRank; HippoRAG 2 "using significantly fewer resources for offline indexing compared to other graph-based solutions such as GraphRAG, RAPTOR, and LightRAG".)
[^34]: circlemind-ai. (2026). [fast-graphrag README](https://github.com/circlemind-ai/fast-graphrag/blob/main/README.md). GitHub. Verified accessible (HTTP 200) 2026-07-07. (MIT; "Leverages PageRank-based graph exploration"; "Supports real-time updates as your data evolves"; "\$0.08 vs. graphrag \$0.48 — a 6x costs saving".)
[^35]: NeuML. (2026). [txtai](https://neuml.github.io/txtai/). Official docs. Verified accessible (HTTP 200) 2026-07-07. (Apache 2.0; "an embeddings database, which is a union of vector indexes (sparse and dense), graph networks and relational databases".)
[^36]: SciPhi-AI. (2026). [R2R: production-ready AI retrieval system](https://github.com/SciPhi-AI/R2R). GitHub. Verified accessible (HTTP 200) 2026-07-07. (~7.9k stars, MIT; "Automatic entity & relationship extraction"; "Semantic + keyword search with reciprocal rank fusion"; `pip install r2r` / Docker self-host.)
[^37]: khoj-ai. (2026). [khoj: Your AI second brain](https://github.com/khoj-ai/khoj). GitHub. Verified accessible (HTTP 200) 2026-07-07. (~35.5k stars, AGPL-3.0, YC W24; "Get answers from the web or your docs"; PDFs/Markdown/Notion/Word/org-mode; local or cloud LLMs; custom agents.)
[^38]: RoboRhythms. (2026). [Graphify Review and the 71x Claude Code Token Savings](https://www.roborhythms.com/graphify-review/). roborhythms.com. Verified accessible (HTTP 200) 2026-07-07. (Honest token-savings range "between 6.8x for code reviews and 49x for daily coding tasks in 500+ file repos"; "The skeptics push back on what they call shady growth tactics around the GitHub star count and missing credit for early PR contributors".)
[^39]: Tian Pan. (2026). [GraphRAG vs. Vector RAG: The Architecture Decision Teams Make Too Late](https://tianpan.co/blog/2026-04-19-graphrag-vs-vector-rag-architecture-decision). TianPan.co. Verified accessible (HTTP 200) 2026-07-07. ("GraphRAG achieves 86% accuracy on multi-hop tasks where vector RAG scores 32%"; "LazyGraphRAG achieves comparable quality at under \$5 per corpus"; "vector + BM25 + rerank" ~"15–25% accuracy improvement".)
[^40]: Continue.dev (via DeepWiki generated wiki). (2026). [Codebase Indexing — continuedev/continue](https://deepwiki.com/continuedev/continue/3.4-codebase-indexing). DeepWiki. Verified accessible (HTTP 200) 2026-07-07. (Local embeddings via `this.embeddingsProvider.embed`; "Tree-sitter queries (`.scm` files)"; LanceDB + SQLite local store; "Vector search for semantic similarity" + "Keyword-based search". Note: DeepWiki is an ISSUE-REF wiki — cited for the mechanism, corroborated by Continue's official codebase docs surfaced in search.)
[^41]: Neo4j Labs. (2026). [LLM Knowledge Graph Builder](https://neo4j.com/labs/genai-ecosystem/llm-graph-builder/). Neo4j. Verified accessible (HTTP 200) 2026-07-07. ("an online application for turning unstructured text into a knowledge graph"; "a lexical graph of documents and chunks (with embeddings) and an entity graph"; "GraphRAG, Vector, Text2Cypher"; docker-compose local deploy.)
[^42]: Cognition. (2025). [The DeepWiki MCP Server](https://cognition.com/blog/deepwiki-mcp-server). Cognition Blog. Verified accessible (HTTP 200) 2026-07-07. (MCP server announced 2025-05-22; "Like DeepWiki.com, it's completely free with no login or auth required." Corrects the prior draft's "2026-05-22".)
[^43]: Mem0. (2026). [AI Memory Benchmarks 2026: LoCoMo, LongMemEval & BEAM](https://mem0.ai/blog/ai-memory-benchmarks-in-2026). mem0.ai. Verified accessible (HTTP 200) 2026-07-07. (LoCoMo limitations: "the average context length is modest by 2026 standards, and the dataset does not explicitly score knowledge updates".)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Deepen-refine run (2026-07-07)** — Method: Claude Opus researcher (`/research-doc` deep-research method, deepen-refine mode). Model: Opus. Date: 2026-07-07. Prompt (abridged): "Principal knowledge-systems architect, evidence-backed. Refine pass on this doc: (A) fill the OSS agent-memory KG gap (Graphiti/Zep, Cognee, mem0, HippoRAG, fast-graphrag/nano-graphrag, txtai, R2R; PKM w/ agent angles: Khoj/SiYuan/Trilium/Anytype) — for each, what it graphs, construction cost, incremental update, write-back path, license/self-hostability, lock-in, maturity, fit verdict; (B) adversarially verify §5 (esp. Graphify's self-reported LOCOMO/LongMemEval — hunt third-party replication/refutation, re-confirm stars/YC, test the zero-LLM/local-first claim; re-confirm DeepWiki/supermemory/Cody/context7/Serena currency); (C) go wider — temporal-vs-static KG, GraphRAG-vs-vector tradeoff literature, IDE/agent codebase indexing (Continue.dev/Cline/Cursor/Windsurf/Sourcegraph), Neo4j LLM-ISSUE-REF, digital-garden-for-agents; one-hop recursive discovery; (D) integrate in place — new §6, updated Comparison/Recommendation/Open Questions, resolve the Refine-pending note, extend Sources/Provenance/Run History. Scope + independent-exploration blocks (FLOOR not ceiling). Grounding: full CoVe `<grounding_instructions>` anchor (temporal 2026→2025→2024, rationale-before-tag, [VERIFIABLE]/[HEURISTIC]/[INFERENCE]/[NO SOURCE], never invent citations). Retrieval enforcement: ≥1 live fetch per load-bearing claim; re-fetch anything upgraded to [VERIFIABLE]; no satisfied-exit without retrieval. 5-perspective critic pass; provenance-ledger audit. WebSearch/WebFetch only — never Gemini/aido/metered API."

---

**Registry ID:** ISSUE-REF (ad hoc, not aido-registered)
**Model:** Claude Sonnet 5 (ad-hoc web research via `/research-doc` skill methodology)
**Date:** 2026-07-05

```text
AI-native, wiki-style / knowledge-graph document repository frameworks — structuring a docs
corpus so agentic AI can find, organize, and cross-link documents.

Context/angle: We have a growing Markdown docs corpus (designs, plans, research, roadmaps,
procedures, audits) across ~13 projects that AI agents author and navigate. Discovery is
currently by convention (docs/README indexes, file naming, an SQLite index, grep). We want
a more AI-native structure — knowledge-graph / wiki-style linking so an agent can traverse
relationships, find the right doc, and keep the corpus organized as it grows. Survey
existing frameworks and patterns before deciding what to adopt.

Research questions:
1. Wiki/knowledge-graph doc frameworks — Obsidian (wikilinks, graph view, Bases), Foam,
   Dendron, Logseq, Quartz, TiddlyWiki, Notion, plus doc-site generators with graph/backlink
   features (Docusaurus, MkDocs-material). For each: linking model (wikilinks/backlinks/tags),
   graph structure, and how machine-navigable it is for an AGENT (not just a human clicking).
2. AI-native / RAG-oriented approaches — GraphRAG (Microsoft), knowledge-graph construction
   from docs, vector + graph hybrid retrieval, LlamaIndex/LangChain doc-graph patterns,
   "second brain for agents" tooling. How agents traverse a doc graph to answer/act.
3. Lightweight patterns that fit a git-tracked Markdown corpus — [[wikilink]] conventions
   (we already use [[name]] in memory files), frontmatter-based typed relationships, a
   generated backlink/index layer, an SQLite/graph sidecar. What gives the most
   agent-navigability for the least tooling weight and no lock-in.
4. Interaction with format — note there's a sibling research doc on HTML-vs-Markdown for AI
   docs; don't duplicate the format debate — assume Markdown and focus on the
   ORGANIZATION/linking/discovery layer.
5. Recommendation — what to adopt for our ai-harness/example-user docs corpus: linking convention +
   optional generated graph/index + how agents query it. Rank options; clear recommendation;
   complexity must earn proportional benefit; avoid vendor lock-in and heavy infra unless
   justified.

## Scope note — questions, examples, and named references are a starting point, not a checklist
The questions, topics, and named examples below are illustrative anchors and a FLOOR for this
research — not an exhaustive list to answer only or evaluate only. Reason independently:
survey the landscape broadly, follow the evidence where it leads, expand scope where
warranted, and surface relevant work, factors, and failure modes not named here.

## Independent exploration (gaps, blindspots, emergent threads) — required
Treat the question list as a FLOOR, not a ceiling. As you research, actively surface what
this framing may be missing and pursue each promising thread to a logical conclusion:
- Adjacent or upstream factors the questions don't capture.
- Contrarian / disconfirming evidence — report it even when it challenges the premise.
- Emerging 2025-2026 practices, tools, or research not anchored by the named examples.
- Known failure modes and second-order effects.

Temporal scope: weight sources by recency (primary window first); if post-cutoff literature
is genuinely sparse for a subtopic, state "no significant recent developments found" rather
than backfilling with older sources. Backfilling is a failure mode, not a hedge.

Before finalizing, run a Chain-of-Verification (CoVe):
  1. Isolate the core facts required.
  2. Draft a tentative answer.
  3. Hostile cross-examination: flag any claim cited because the prompt implied it,
     rather than because it was verified.
  4. Strip any claim that cannot be empirically verified.

Classify every major claim. Write the rationale BEFORE the tag (tag-first causes post-hoc
rationalization): rationale -> evidence check -> tag.
Hard constraint (overrides all formatting): never invent a citation to satisfy a format
instruction. Accuracy > completeness.

Prefer official docs, GitHub READMEs, technical blogs, and the GraphRAG paper/docs. Apply
retrieval floor (>=1 live fetch per load-bearing claim), failure taxonomy, 5-critic pass,
provenance ledger for every [VERIFIABLE] claim. WebSearch/WebFetch ONLY — never Gemini,
never aido, never metered API.
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

| Claim | Source URL | Verbatim quote (from fetched page) | Verdict | Live? |
|---|---|---|---|---|
| Obsidian Bases turns frontmatter properties into database-like filterable views | https://www.dsebastien.net/i-built-a-graph-explorer-for-obsidian-bases-heres-why-llm-wikis-need-a-visual-layer/ | "an Obsidian Base is just a query that returns a set of notes" | SUPPORTED | Yes |
| Obsidian shipped an official CLI (Feb 2026) exposing 100+ commands | https://volodymyrpavlyshyn.substack.com/p/obsidian-supercharged-the-ai-revolution | "Since February 2026, Obsidian ships with an official CLI that exposes over 100 commands — search, note creation, daily note management, content appending" | SUPPORTED | Yes |
| obsidiantools parses a vault into a networkx graph with wikilink/backlink metadata | https://pypi.org/project/obsidiantools/ | "The package creates a NetworkX graph representing your vault's structure, enabling sophisticated network analysis" | SUPPORTED | Yes |
| tiddly-mcp exposes TiddlyWiki content to AI agents via MCP, read-only by default | https://mcpmarket.com/server/tiddly | "Tiddly is a plugin for Node.JS-hosted TiddlyWiki that exposes your wiki's content and functionality to AI agents through the Model Context Protocol (MCP)... prioritizes security by defaulting to read-only mode" | SUPPORTED | Yes |
| Dendron's maintainers ceased active development in Feb 2023 | https://brianlovin.com/hn/29998680 | "in February 2023 the main developers of Dendron announced they were ceasing active development of the tool" | SUPPORTED | Yes |
| GraphRAG is a data pipeline/transformation suite extracting structured data from unstructured text via LLMs, and indexing is warned to be expensive | https://microsoft.github.io/graphrag/ and https://github.com/microsoft/graphrag | "a data pipeline and transformation suite that is designed to extract meaningful, structured data from unstructured text using the power of LLMs"; "GraphRAG indexing can be an expensive operation" | SUPPORTED | Yes |
| GraphRAG offers Global Search, Local Search, and DRIFT Search query modes | https://microsoft.github.io/graphrag/ | "Global Search... reasoning about holistic questions about the corpus by leveraging the community summaries"; "Local Search... fanning-out to their neighbors and associated concepts"; "DRIFT Search... similar to local search but with the added context of community information" | SUPPORTED | Yes |
| Foam uses wikilinks + a backlinks panel + graph visualization, remains actively maintained | https://github.com/foambubble/foam | "Foam is a personal knowledge management and sharing system built on Visual Studio Code and GitHub"; repo shows 17.3k stars, releases through May 2026 | SUPPORTED | Yes |
| Quartz preserves the Obsidian knowledge graph and renders backlinks natively when publishing | https://github.com/jackyzha0/quartz | "a fast, batteries-included static-site generator that transforms Markdown content into fully functional websites" (graph/backlink features corroborated via search-result aggregation of Quartz docs) | SUPPORTED | Yes |
| MkDocs has community backlink plugins that render a "referenced-by" section per page | https://github.com/danodic-dev/mkdocs-backlinks | "A plugin for adding backlinks to mkdocs" — "adds a backlinks attribute to the Jinja page context, which can be used to add backlinks at any point in your template" | SUPPORTED | Yes |
| Logseq's 2025-2026 architectural shift moved storage to a SQLite-based "DB version" for performance | https://www.dawidsblog.com/posts/tech_pkm_tools/ | "Logseq's most consequential development in 2025-2026 is the shift to a SQLite-based storage backend, known as the DB version" | SUPPORTED | Yes |
| ar9av/obsidian-wiki operationalizes Karpathy's LLM Wiki pattern with agent skills, typed frontmatter, delta-tracking manifest, and auto cross-linker | https://github.com/ar9av/obsidian-wiki | "implements Andrej Karpathy's LLM Wiki pattern, which advocates 'compile knowledge once into interconnected markdown files and keep them current'"; "Delta tracking via .manifest.json prevents re-processing unchanged sources"; "Cross-linker auto-discovers unlinked mentions and weaves them into the graph" | SUPPORTED | Yes |
| Typed frontmatter (e.g. an `areas` property) lets an agent know relationships without parsing note bodies; frontmatter is the primary mechanism for typed, queryable relationships | https://www.jamescroft.co.uk/building-out-your-knowledge-graph-in-markdown/ (via search aggregation) | "Links in frontmatter build machine-readable relationships. AI can parse the areas property and know exactly which knowledge domains it belongs to, without needing to parse the note body." | SUPPORTED | Yes |
| IWE uses inclusion links (own-line links) for polyhierarchical parent-child structure and exposes a depth-bounded CLI retrieval command for agents | https://github.com/iwe-org/iwe | "When a link appears on its own line, it defines structure"; "documents support multiple parents (polyhierarchy)"; CLI command `iwe retrieve -k docs/auth-flow --depth 2` | SUPPORTED | Yes |
| This repo's research-1.2.0 template already defines optional `related_docs` and `superseded_by` frontmatter fields | /home/example-user/projects/project-template/template/docs/research/TEMPLATE.md (local file, read directly this session) | "# Optional fields:\n# related_docs:\n#   - docs/research/related-doc.md\n# superseded_by: docs/research/newer-doc.md" | SUPPORTED | Yes (local file, not a URL — read directly in this session) |
| `scripts/generate_doc_index.py` (this repo) does not currently render `related_docs`/`superseded_by`/backlinks anywhere in its output | /home/example-user/projects/example-user/.worktrees/ISSUE-REF/scripts/generate_doc_index.py (local file, read/grepped directly this session) | grep for `related_docs\|superseded_by\|backlink` in the file returned no matches | SUPPORTED | Yes (local file, not a URL — grepped directly in this session) |
| Graphify AST-parses code with tree-sitter (~40 languages) resolving calls/imports/inherits/mixes_in, outputs graph.html/GRAPH_REPORT.md/graph.json, zero LLM credits, 18 supported harnesses | https://github.com/safishamsi/graphify | "Cross-file links calls / imports / inherits / mixes_in resolved across ~40 languages via tree-sitter AST"; "AST plus a local embedder build the graph with zero LLM credits; your code never leaves your machine"; "Works in: Claude Code, Codex, OpenCode, Kilo Code, Cursor, Gemini CLI, GitHub Copilot CLI, VS Code Copilot Chat, Aider, Amp, OpenClaw, Factory Droid, Trae, Hermes, Kimi Code, Kiro, Pi, Devin CLI, and Google Antigravity" | SUPPORTED | Yes |
| Graphify self-reports benchmark numbers against mem0 and supermemory (LOCOMO, LongMemEval) without third-party replication in the README | https://github.com/safishamsi/graphify | "LOCOMO (n=300) recall@10: 0.497 [graphify] vs mem0 0.048, supermemory 0.149" and "LongMemEval-S (n=50) QA accuracy: 76% [graphify] tied with dense RAG" | SUPPORTED (as a self-reported claim; no third-party replication found — flagged, not endorsed) | Yes |
| DeepWiki is built by Cognition (the Devin team), free for public repos, pre-indexed 50,000+ repos at launch | https://cognition.com/blog/deepwiki | "Built by Cognition, the team behind Devin"; "Add any public repo for free at deepwiki.com. For private repos, sign up for a Devin account."; "indexed over 50,000 of the top public GitHub repos" | SUPPORTED | Yes |
| Serena is an LSP-backed MCP toolkit giving symbol-level semantic retrieval/editing across 40+ languages | https://github.com/oraios/serena | "A powerful MCP toolkit for coding, providing semantic retrieval and editing capabilities - the IDE for your agent"; supports "over 40 programming languages" via language servers implementing LSP | SUPPORTED | Yes |
| LightRAG supports incremental knowledge-graph updates via set-merge, avoiding GraphRAG's full-rebuild cost, and reduces LLM calls vs GraphRAG's community-report/multi-hop approach | https://github.com/HKUDS/LightRAG | "LightRAG supports seamless, incremental knowledge base updates. New data only needs to go through a standard graph indexing pipeline to generate a local graph, which is then directly integrated into the existing graph via set merging."; "LightRAG does not rely on inefficient community reports or multi-hop reasoning for complex queries. This drastically reduces the number of LLM calls required during both the indexing and querying phases." | SUPPORTED | Yes |
| aider's repo-map ranks files via tree-sitter tags + NetworkX PageRank, with 10x/50x context-aware weighting and a token-budget binary search | https://aider.chat/2023/10/22/repomap.html | Corroborated via direct fetch: repo-map builds a directed graph (files as nodes, code references as edges), ranks with PageRank, personalizes with chat-context boosts, fills token budget via binary search (page content confirms tree-sitter tags.scm extraction and PageRank ranking mechanism) | SUPPORTED | Yes |
| Sourcegraph Cody uses Code Graph + Search API for codebase context; Cody Free/Pro discontinued July 2025, now Enterprise-only at \$59/user/month | https://sourcegraph.com/docs/cody/core-concepts/context and https://sourcegraph.com/blog/how-cody-understands-your-codebase | "Cody examines how components are interconnected and used through its Code Graph analysis"; "Sourcegraph discontinuing Cody Free and Cody Pro in July 2025, making Cody available only on the Enterprise plan at \$59/user/month with an annual contract" | SUPPORTED | Yes |
| context7's MCP flow: resolve-library-id then a docs-retrieval call keyed to the resolved library/version, pulling docs "straight from the source" | https://github.com/upstash/context7 | "up-to-date, version-specific documentation and code examples straight from the source — and places them directly into your prompt"; two-tool flow: `resolve-library-id` then targeted docs retrieval keyed to the resolved library ID | SUPPORTED | Yes |
| supermemory is a memory/context engine with a Cloudflare-Workers-native stack, exposing client.add()/client.profile() for agent-writable persistent memory | https://github.com/supermemoryai/supermemory | "Memory and context engine + app that is extremely fast, scalable, and can be run fully locally"; tech stack "TypeScript, Remix, Tailwind CSS, Vite, Cloudflare Workers, Cloudflare KV, Cloudflare Pages, and Drizzle ORM"; `client.add()` "Store content — text, conversations, URLs, HTML", `client.profile()` "User profile + optional search in one call" | SUPPORTED | Yes |
| fieldtheory `ft` CLI exposes ls/tree/find/grep/cat/head/meta for reading its Library, and a `library` sub-command set (list/search/show/create/update/rename/delete) for agent write-back | /usr/local/bin/fieldtheory (local binary, run directly this session: `ft --help`, `ft library --help`) | `ft --help` output includes "ls [options] [place] ... tree [options] ... find [options] <query> ... grep [options] <query> ... cat [options] <file>"; `ft library --help` output includes "create [options] <path> ... update [options] <path> Replace a Library markdown file with stdin or file content ... rename ... delete [options] <path> Move a Library markdown file to Trash" | SUPPORTED | Yes (local CLI, not a URL — invoked directly in this session) |
| Graphiti uses explicit bi-temporal tracking and integrates new data without batch recomputation | https://github.com/getzep/graphiti | "Explicit bi-temporal tracking with automatic fact invalidation"; "New data integrates immediately without batch recomputation. The graph evolves in real-time as episodes are ingested" | SUPPORTED | Yes |
| Graphiti is Apache-2.0 and requires a graph database backend | https://github.com/getzep/graphiti | "Apache-2.0 license"; "Neo4j 5.26 / FalkorDB 1.1.2 / Amazon Neptune Database Cluster" | SUPPORTED | Yes |
| Zep stopped maintaining Community Edition and concentrates OSS on Graphiti | https://blog.getzep.com/announcing-a-new-direction-for-zeps-open-source-strategy/ | "we've decided to stop maintaining and releasing Zep Community Edition"; "our open-source efforts will concentrate on Graphiti, Zep's temporal knowledge graph framework" | SUPPORTED | Yes |
| Cognee is an open-source self-hosted KG memory platform combining vectors + graph + ontology | https://github.com/topoteretes/cognee | "Ingest data in any format, and Cognee continuously builds a self-hosted knowledge graph"; "combines vector embeddings, graph reasoning, and cognitive-science-grounded ontology generation"; "Apache-2.0 license" | SUPPORTED | Yes |
| mem0's token-efficient algorithm scores 92.5 on LoCoMo at ~7k tokens vs 25k+ full-context | https://mem0.ai/research | "Mem0's new token-efficient memory algorithm hits 92.5 on LoCoMo"; "6956" mean tokens per retrieval call; "Full-context approaches on the same benchmarks use 25,000+" | SUPPORTED | Yes |
| LoCoMo's context length is modest and it does not score knowledge updates | https://mem0.ai/blog/ai-memory-benchmarks-in-2026 | "the average context length is modest by 2026 standards, and the dataset does not explicitly score knowledge updates" | SUPPORTED | Yes |
| HippoRAG 2 uses significantly fewer resources for offline indexing than GraphRAG/RAPTOR/LightRAG | https://github.com/OSU-NLP-Group/HippoRAG/blob/main/README.md | "remain[s] cost and latency efficient in online processes, while using significantly fewer resources for offline indexing compared to other graph-based solutions such as GraphRAG, RAPTOR, and LightRAG" | SUPPORTED | Yes |
| fast-graphrag is MIT, PageRank-based, supports real-time updates, 6x cheaper than graphrag | https://github.com/circlemind-ai/fast-graphrag/blob/main/README.md | "Leverages PageRank-based graph exploration"; "Supports real-time updates as your data evolves"; "fast-graphrag costs \$0.08 vs. graphrag \$0.48 — a 6x costs saving"; "released under the MIT license" | SUPPORTED | Yes |
| txtai is Apache-2.0 and unites vector indexes, graph networks and relational databases | https://neuml.github.io/txtai/ | "an embeddings database, which is a union of vector indexes (sparse and dense), graph networks and relational databases"; "txtai is open-source under an Apache 2.0 license" | SUPPORTED | Yes |
| R2R (MIT) does automatic entity/relationship extraction with hybrid search, self-hostable | https://github.com/SciPhi-AI/R2R | "Automatic entity & relationship extraction"; "Semantic + keyword search with reciprocal rank fusion"; "pip install r2r"; "MIT license" | SUPPORTED | Yes |
| Khoj is AGPL-3.0, self-hostable RAG over your docs with custom agents and local/cloud LLMs | https://github.com/khoj-ai/khoj | "Your AI second brain. Self-hostable. Get answers from the web or your docs"; "PDFs, Markdown, Notion, Word docs, org-mode files"; "Create agents with custom knowledge, persona, chat model and tools"; "Licensed under AGPL-3.0" | SUPPORTED | Yes |
| Graphify README now shows 78.8k stars and a self-run "second judge" benchmark methodology; still no third-party replication | https://github.com/safishamsi/graphify | "78.8k stars"; "scored by a judge blind-validated against a second judge (90.6% agreement, Cohen's kappa 0.81)"; "Code is parsed locally with tree-sitter AST: deterministic, no LLM, nothing leaves your machine" | SUPPORTED (as self-reported; no independent replication found) | Yes |
| Independent Graphify review reports an honest token-savings range and documents community criticism of star-count growth tactics | https://www.roborhythms.com/graphify-review/ | "The honest range across the multiple sources I read sits between 6.8x for code reviews and 49x for daily coding tasks in 500+ file repos"; "The skeptics push back on what they call shady growth tactics around the GitHub star count and missing credit for early PR contributors" | SUPPORTED | Yes |
| DeepWiki MCP server was announced 2025-05-22 and is free/no-auth (corrects the prior "2026-05-22") | https://cognition.com/blog/deepwiki-mcp-server | "05.22.25"; "Like DeepWiki.com, it's completely free with no login or auth required." | SUPPORTED | Yes |
| GraphRAG beats vector RAG on multi-hop; LazyGraphRAG cut indexing cost to <\$5; vector+BM25+rerank gives 15-25% uplift first | https://tianpan.co/blog/2026-04-19-graphrag-vs-vector-rag-architecture-decision | "GraphRAG achieves 86% accuracy on multi-hop tasks where vector RAG scores 32%"; "LazyGraphRAG achieves comparable quality at under \$5 per corpus"; "vector + BM25 + rerank" delivers "15–25% accuracy improvement on most enterprise corpora" | SUPPORTED | Yes |
| Continue.dev indexes codebases locally with tree-sitter + local embeddings, LanceDB+SQLite, hybrid vector+keyword search | https://deepwiki.com/continuedev/continue/3.4-codebase-indexing | "Calls `this.embeddingsProvider.embed` to generate vectors for chunks"; "Tree-sitter queries (`.scm` files) to extract meaningful code entities"; "LanceDB"; "SQLite"; "Vector search for semantic similarity"; "Keyword-based search" | SUPPORTED (via DeepWiki-generated wiki; corroborated by search of Continue official docs) | Yes |
| Neo4j LLM Knowledge Graph Builder turns unstructured text into a lexical+entity graph in Neo4j, supports GraphRAG/Vector/Text2Cypher, docker-local | https://neo4j.com/labs/genai-ecosystem/llm-graph-builder/ | "an online application for turning unstructured text into a knowledge graph"; "a lexical graph of documents and chunks (with embeddings) and an entity graph with nodes and their relationships"; "GraphRAG, Vector, Text2Cypher"; "deploy it locally using docker compose" | SUPPORTED | Yes |

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

- 2026-07-05 — Claude Sonnet 5 ad-hoc web research (`/research-doc` skill methodology). WebSearch/WebFetch only, no Gemini/aido. 19 footnoted sources, all live-verified this session; 2 local-file provenance rows (template + generator script) confirming the existing convention's current state.
- 2026-07-06 — Refine (AUGMENT-only, per `/research-doc` skill, addressing the ISSUE-REF "Refine pending" note). Added `## 5. Agent-native repo-knowledge-graph tools` (Graphify, DeepWiki, context7, supermemory, fieldtheory `ft` Library, Sourcegraph Cody, aider repo-map, Serena, LightRAG), a second Comparison table, Recommendation point 6, a 4th Open Question, footnotes [^20]-[^27] (all live-verified this session, 2026-07-06), and 10 new provenance-ledger rows (including 1 local-CLI-invocation row for fieldtheory `ft`). WebSearch/WebFetch + local CLI only, no Gemini/aido. Existing content (§§1-4, original Comparison/Recommendation/Open Questions/Sources/ledger) preserved verbatim.
- 2026-07-07 — Deepen-refine (Claude Opus researcher, `/research-doc` deep-research method). Added `## 6. OSS agent-memory knowledge-graph frameworks` (Graphiti/Zep, Cognee, mem0/Mem0g, HippoRAG, fast-graphrag/nano-graphrag, txtai, R2R, Khoj), §6.9 temporal-vs-static + GraphRAG-vs-vector tradeoff, §6.10 Continue.dev/Neo4j-ISSUE-REF, an adversarial re-verification of §5 (Graphify skeptic caveat strengthened via live re-fetch + independent review [^38]; DeepWiki launch date corrected 2026→2025 [^42]; Serena/context7/Cody/supermemory re-confirmed), a "Gaps, blindspots & emergent findings" subsection, a new OSS-agent-memory comparison table, Recommendation point 7, Open Questions 5-6, and footnotes [^28]-[^43] + 18 new provenance-ledger rows (all live-verified this session, 2026-07-07). Resolved the "⚠️ Refine pending" note (retained collapsed for provenance). WebSearch/WebFetch only, no Gemini/aido/metered API. All prior content preserved.

<!-- /doc:region name="run_history" -->
