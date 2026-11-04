---
title: Repo Structure and Navigation Guidance for Coding Agents
category: research
tags: [research]
status: complete
source: "codex-cx-research-2026-07-26"
template_version: "research-1.2.0"
task: ISSUE-REF
---

# Repo Structure and Navigation Guidance for Coding Agents

**Status:** complete

**Created:** 2026-07-26

**Task:** ISSUE-REF

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. What the major agent vendors specify](#1-what-the-major-agent-vendors-specify)
  - [The conflict is in discovery and scope, not the basic content](#the-conflict-is-in-discovery-and-scope-not-the-basic-content)
- [2. What AGENTS.md standardizes—and what it does not](#2-what-agentsmd-standardizesand-what-it-does-not)
  - [Adoption evidence is directional, not a census](#adoption-evidence-is-directional-not-a-census)
- [3. What mature open-source context files do](#3-what-mature-open-source-context-files-do)
  - [Large-repository pattern](#large-repository-pattern)
- [4. Static guidance, generated indexes, and context economics](#4-static-guidance-generated-indexes-and-context-economics)
  - [Generated indexes versus maintained prose](#generated-indexes-versus-maintained-prose)
  - [Staleness and drift](#staleness-and-drift)
- [5. Retrieval and navigation tools](#5-retrieval-and-navigation-tools)
  - [When retrieval beats a static description](#when-retrieval-beats-a-static-description)
- [6. Per-directory READMEs and machine-readable manifests](#6-per-directory-readmes-and-machine-readable-manifests)
  - [Per-directory READMEs](#per-directory-readmes)
  - [Machine-readable layout manifests](#machine-readable-layout-manifests)
- [7. What code-localization research changes](#7-what-code-localization-research-changes)
  - [Gaps, blindspots & emergent findings](#gaps-blindspots--emergent-findings)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
  - [Recommended information architecture](#recommended-information-architecture)
  - [Recommended root contract](#recommended-root-contract)
  - [Placement and maintenance rules](#placement-and-maintenance-rules)
  - [Acceptance criteria](#acceptance-criteria)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Ambiguous Items from Auto-Remediation (Post-Run Review)](#ambiguous-items-from-auto-remediation-post-run-review)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

<!-- doc:region name="context" kind="immutable" -->

## Context

**Primary period:** 2024–2026

**Source weighting:** 2026 primary → 2025 → 2024; pre-2024 material is used only where foundational.

This research asks how an unfamiliar codebase should tell coding agents where things live, where new files belong, and how to navigate. It compares always-loaded agent instructions, nested rules, human documentation, generated maps, machine-readable manifests, and tool-mediated retrieval, with special attention to monorepos, context cost, drift, and observed task performance.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

The evidence cutoff is 2026-07-26. Current vendor documentation and 2026 empirical work receive the most weight; 2025–2024 sources provide supporting or foundational evidence. Several 2026 papers cited here are preprints or vendor evaluations rather than replicated peer-reviewed results, and are identified as such. Machine-readable repository-layout manifests: no significant post-2024 standardization or adoption evidence was found.

## Executive Summary

The state of the art is a layered system, not a comprehensive map checked into one file. Put a short, portable orientation and file-placement contract in root agent instructions; add local instructions only at genuine subsystem boundaries; keep durable human explanations in component documentation; and let agents discover the changing file/symbol/dependency graph through live search, language intelligence, or a task-ranked repository map. This follows the vendors’ shared preference for concise, scoped instructions and is reinforced by 2026 evidence that broad generated context can increase cost without improving success. [VERIFIABLE][^1][^2][^3]

The durable dividing line is semantic versus observable information. “Handlers for this protocol belong under `src/gateway/handlers`, while generated clients must never be edited” is a worthwhile permanent rule because a directory listing cannot reliably infer it; a recursive tree, symbol inventory, or list of current files is better queried at task time. [INFERENCE]

No evidence located supports a universal machine-readable repository-layout manifest, a README in every directory, or a committed generated tree as the default. [NO SOURCE] AGENTS.md is the strongest cross-tool portable convention, but it deliberately standardizes Markdown discovery and directory scope rather than a schema for layout. [VERIFIABLE][^4]

## 1. What the major agent vendors specify

Anthropic explicitly names project architecture as appropriate `CLAUDE.md` content, says specific and concise instructions are followed more reliably, recommends keeping each file under roughly 200 lines, and advises moving task-specific material into path-scoped rules or on-demand skills. Its own example of useful structure guidance is a placement rule—“API handlers live in `src/api/handlers/`”—while its help guidance rejects exhaustive file trees and full API documentation. [VERIFIABLE][^1][^2]

OpenAI Codex constructs an instruction chain before work: global guidance, then repository-root-to-working-directory `AGENTS.md` files, with nearer files taking precedence and a default aggregate limit of 32 KiB. OpenAI’s published internal guidance says AGENTS files should capture conventions, business logic, quirks, and dependencies that Codex cannot infer, while prompts should name relevant components or paths when known. [VERIFIABLE][^3][^5]

GitHub Copilot accepts repository-wide instructions, path-specific instruction files, and `AGENTS.md`; its documented instruction-generation prompt asks for high-level repository information and validated build/test commands rather than a complete file catalog. Copilot also accepts root `CLAUDE.md` and `GEMINI.md` as alternatives in some surfaces. [VERIFIABLE][^6]

Gemini CLI discovers hierarchical `GEMINI.md` files and concatenates them into context; its just-in-time context mechanism can add relevant directory context when tools access a path. Devin auto-generates repository “Knowledge” from README files, file structure, and file contents, retrieves that knowledge by triggers, and tells users to review it for accuracy. [VERIFIABLE][^7][^8]

Cursor’s rule model supports always-on, automatically attached, agent-requested, and manually invoked project rules, which is the clearest vendor expression of “pay permanent context only for permanent rules.” [VERIFIABLE][^9] Cursor also provides automatic codebase retrieval rather than asking users to encode the whole repository in rules. Its January 2026 engineering report says semantic search improved response accuracy by 12.5% in an internal evaluation and describes syntactic chunking, embeddings, and incremental Merkle-tree synchronization. [VERIFIABLE][^10]

### The conflict is in discovery and scope, not the basic content

The vendors agree more than they disagree about content: include non-obvious commands, architectural boundaries, conventions, and constraints; keep instructions concise; and scope specialized material. [VERIFIABLE][^1][^2][^3][^6] They conflict operationally:

| Tool family | Portable/root file | Nested or path scope | Notable behavior |
|---|---|---|---|
| Claude Code | `CLAUDE.md` | Ancestor/child `CLAUDE.md`; `.claude/rules` path globs | Does not natively read `AGENTS.md`; Anthropic recommends importing or symlinking it. |
| Codex | `AGENTS.md` | Root-to-CWD hierarchy | Nearer file overrides; aggregate byte cap. |
| Cursor | `.cursor/rules`; `AGENTS.md` compatibility | Rule types and nested project rules | Retrieval/indexing is a separate context channel. |
| GitHub Copilot | `.github/copilot-instructions.md`; `AGENTS.md`; other vendor files on some surfaces | Path-specific instructions; nearest `AGENTS.md` | Multiple formats can be combined, so conflicting duplicates are hazardous. |
| Gemini CLI | `GEMINI.md` | Hierarchical and just-in-time directory context | Files are concatenated into prompt context. |
| Devin | Repository Knowledge; `AGENTS.md` | Triggered knowledge; nested `AGENTS.md` | Can bootstrap knowledge from current docs and structure. |

These are verified product behaviors rather than evidence that any one naming convention improves task success. [VERIFIABLE][^1][^3][^6][^7][^8][^9]

The practical portability pattern in July 2026 is therefore a canonical `AGENTS.md` plus the thinnest required vendor adapters—for example, a `CLAUDE.md` that imports `AGENTS.md`—not independently maintained copies. This minimizes contradictory instructions while accommodating tools that have not converged on one discovery protocol. [INFERENCE]

## 2. What AGENTS.md standardizes—and what it does not

AGENTS.md describes itself as a “README for agents.” It recommends project overview, build/test commands, code style, testing, and security considerations; permits nested files in monorepos; uses ordinary Markdown; and defines no required fields. The nearest file in the directory tree wins for the files an agent edits. [VERIFIABLE][^4]

Consequently, AGENTS.md standardizes a discoverable, human-readable instruction location and a directory-scoping convention. It does **not** standardize a repository graph, path taxonomy, ownership model, symbol index, build graph, placement-decision schema, freshness protocol, or machine API. [VERIFIABLE][^4] Layout guidance is permitted content, not a required or interoperable data model. [INFERENCE]

That narrow standard is a feature for portability but a limitation for automation. Markdown can express why a new provider belongs in one package rather than another, but tools cannot safely assume a fixed heading, parse a complete topology, or verify referential integrity across repositories. [INFERENCE]

### Adoption evidence is directional, not a census

The convention’s site reports use in “over 60k open-source projects” and cites examples across major ecosystems. Because that number is the project’s own GitHub-search estimate rather than a reproducible longitudinal census, it establishes visible traction but not market share, compliance quality, or causal benefit. [VERIFIABLE][^4]

Searches for a recent independent cross-tool census found exploratory datasets, but no accessible source with sufficiently clear denominators and sampling methodology to support a stronger July 2026 adoption percentage. [NO SOURCE] In particular, repository counts cannot show whether agents actually loaded the file, whether the contents were maintained, or whether tasks improved. [INFERENCE]

## 3. What mature open-source context files do

A purposive—not representative—review of mature public repositories found a recurring pattern:

- OpenAI’s Codex repository uses local placement and boundary rules, including where general documentation should not be added and how `codex-core` responsibilities are separated. [VERIFIABLE][^11]
- Apache Airflow provides a compact key-path map and architectural boundaries alongside commands and workflow guidance, although its root file is substantially longer than Anthropic’s under-200-line recommendation. [VERIFIABLE][^12]
- Temporal’s Java SDK gives a short module-layout explanation, then concentrates on build, test, and repository-specific rules. [VERIFIABLE][^13]
- Kubernetes keeps its root file very short and spends scarce space on a counterintuitive source-of-truth rule: code under `staging/src/k8s.io` is authoritative while related locations are generated. [VERIFIABLE][^14]

These examples do not prove that context files caused better agent outcomes. They do show what maintainers judged worth encoding: stable module roles, non-obvious source-of-truth boundaries, and placement constraints—not exhaustive inventories of files. [INFERENCE]

### Large-repository pattern

In a monorepo, useful granularity follows architectural and workflow boundaries rather than directory count. A root file should name the small number of stable domains and routing rules; a nested file earns its existence when a subtree has different build/test commands, generated-code rules, ownership constraints, or placement semantics. [HEURISTIC]

Putting an instruction file in every directory recreates the tree in prose, multiplies review surfaces, and can cause overlapping instruction chains. No controlled evidence was found that directory-by-directory agent files improve navigation. [NO SOURCE] Vendor support for nested/path-scoped instructions instead favors selective overlays loaded only when a task enters that area. [VERIFIABLE][^1][^3][^6]

## 4. Static guidance, generated indexes, and context economics

The strongest direct 2026 test is disconfirming. Gloaguen et al.’s CTX-Bench study evaluated generated and developer-committed context across multiple agents and models; providing context did not generally improve task success, while generated context raised inference cost by more than 20% on average. [VERIFIABLE][^15]

The authors found that context instructions were followed but repository overviews were not helpful; their ablation removing project documentation suggests generated context can substitute for missing documentation, although no content category produced a significant accuracy gain. [VERIFIABLE][^15]

A second 2026 workshop preprint reports a different result: across 124 pull requests in ten repositories, AGENTS.md reduced median runtime by 28.64% and output tokens by 16.58% with comparable completion. The result concerns efficiency, uses a smaller observational setup, and does not establish that larger structural maps improve correctness. [VERIFIABLE][^16]

Taken together, the evidence rejects both “context files never help” and “more map is better.” Their defensible permanent budget is the smallest set of non-inferable requirements and routing cues; task-specific repository state should be loaded only when relevant. [INFERENCE]

Anthropic’s product guidance reaches the same operational conclusion from a different direction: every `CLAUDE.md` consumes startup context, longer files reduce adherence, path-scoped rules load conditionally, and topic memory can be indexed then read on demand. [VERIFIABLE][^1] Prompt caching may lower repeated input billing, but it does not remove a long file from the model’s attention or prevent context dilution. [INFERENCE]

### Generated indexes versus maintained prose

No controlled study found in this review isolates a committed flat file tree against a concise hand-written placement guide on realistic coding tasks. [NO SOURCE] CTX-Bench is the closest evidence, but its generated files are broader context artifacts rather than a pure directory-listing intervention. [VERIFIABLE][^15]

Generated data is appropriate when it is fresh, queryable, and filtered by the current task. Aider’s repository map analyzes the codebase for compact, task-relevant context; its documentation warns that adding many irrelevant files distracts models and that weaker models can be confused by the map or even try to edit it. [VERIFIABLE][^17] This is evidence for ranking and token budgets, not for committing the generated map as documentation.

A checked-in generated index has an awkward ownership model: humans are unlikely to review it semantically, agents may treat it as authoritative, and its main advantage—derivability—means it can be regenerated or queried instead. Prefer ephemeral generation at session/task time, or CI-produced artifacts that are clearly marked generated and never treated as the semantic source of truth. [HEURISTIC]

### Staleness and drift

Staleness is not hypothetical. A 2024 empirical study of more than 3,000 GitHub projects found that 28.9% of highly popular projects currently had at least one outdated README reference and 82.3% had experienced one; outdated references could persist for years. [VERIFIABLE][^18]

A 2026 preprint focused on agent context files reports stale path references in 23% of a representative sample of 356 repositories. Its manual validation sample classified 64% of detected cases as genuine, but used one annotator and no inter-rater agreement, so the exact prevalence should be treated as preliminary rather than definitive. [VERIFIABLE][^19]

Generation prevents manual transcription drift only for facts the generator can derive. It cannot decide whether `legacy/` is intentionally frozen, whether a package is the canonical extension point, or where a new abstraction should live unless those semantics already exist in another machine-readable source. [INFERENCE]

Mechanisms with a plausible maintenance payoff are:

1. Derive observable facts from package/build manifests and current search instead of copying them into prose. [HEURISTIC]
2. Make every checked-in path reference lintable; fail CI when referenced paths disappear. [HEURISTIC]
3. Review instruction changes alongside the code move that invalidates them, with ownership at the relevant subtree. [HEURISTIC]
4. Delete instructions that merely restate visible code. [HEURISTIC]
5. Exercise a few navigation tasks in agent evaluations and record which context was loaded, rather than equating “file exists” with “file works.” [HEURISTIC]

No comparative study was found showing which of these maintenance mechanisms most improves agent task success or has the lowest maintainer burden. [NO SOURCE]

## 5. Retrieval and navigation tools

Repository navigation is a ranking problem: given a task and a budget, identify the files, symbols, relationships, and line ranges most likely to matter. A static map is task-independent, so it necessarily spends tokens on unrelated areas or omits detail that a particular task needs. [INFERENCE]

Current tools use several complementary signals:

- Lexical search (`rg`, indexed regex/substring search) is exact, transparent, branch-current, and strong when the agent can hypothesize names. [HEURISTIC]
- Semantic retrieval ranks syntactically chunked code by meaning and can bridge vocabulary mismatch. Cursor reports a 12.5% average response-accuracy improvement in an internal evaluation, but the benchmark details linked from its blog are vendor-controlled. [VERIFIABLE][^10]
- Aider-style repository maps provide compact repository context under a configurable token budget; the tool’s own caution about weaker models shows that even compact structure is model- and task-dependent. [VERIFIABLE][^17]
- LSP/code-navigation interfaces expose definition and reference relationships; the Language Server Protocol explicitly standardizes features such as go-to-definition and find-references. [VERIFIABLE][^20]
- Graph and history-aware retrieval can supply signals that text similarity omits. Microsoft’s Repository Memory work combines commit history with module-level summaries for code localization. [VERIFIABLE][^21]

### When retrieval beats a static description

Retrieval is the better default when the answer changes with the branch, depends on symbol-level or cross-file relationships, or would require enumerating more than a handful of paths. Static guidance remains better for normative facts: forbidden dependencies, canonical extension points, generated/source boundaries, ownership rules, and the reasons two similar directories are not interchangeable. [INFERENCE]

SWE-Explore’s 2026 benchmark covers 848 issues across 203 repositories and ten languages; it finds agentic explorers outperform classical retrieval and identifies line-level coverage plus efficient ranking as differentiators. [VERIFIABLE][^22] FastContext separately reported that a dedicated on-demand explorer improved resolution by as much as 5.5% and reduced coding-agent tokens by as much as 60%; however, the paper was withdrawn on June 30 for stated product-IP review, so it is a provisional signal rather than reliable confirmation. [VERIFIABLE][^23]

A 2026 FSE study comparing similarity, static-analysis, and navigation-based context reports that static analysis offered the best effectiveness/efficiency balance, while navigation had the highest ceiling at roughly 10–20× the computation. [VERIFIABLE][^24] This argues for a cascade: cheap lexical search first, precise structural navigation next, semantic/graph expansion when vocabulary or hidden dependencies demand it. [INFERENCE]

Retrieval does not eliminate documentation. It changes the documentation’s job from carrying the answer to teaching the agent where and how to look, including the canonical commands and which generated or vendored regions to exclude. [INFERENCE]

## 6. Per-directory READMEs and machine-readable manifests

### Per-directory READMEs

Per-directory READMEs are established human documentation. Google’s documentation style guide recommends README files as directory landing pages, with at least purpose, contact, status, and pointers to more information. [VERIFIABLE][^25] Devin’s auto-generated Knowledge explicitly consumes repository READMEs, showing that human docs can become an agent retrieval substrate without being copied into an always-loaded instruction file. [VERIFIABLE][^8]

They are useful where a directory is a user- or contributor-facing component with a durable purpose, public workflow, or operational contract. They are a poor default for every directory: thin wrapper folders, generated directories, and implementation-only partitions rarely justify a separate narrative. [HEURISTIC]

No controlled agent study found in this review measures per-directory READMEs against root instructions, live retrieval, or no README. [NO SOURCE] README staleness evidence also cautions against multiplying manually maintained landing pages. [VERIFIABLE][^18] Use them for explanations humans also need, then link to them from agent context on demand; do not create agent-only duplicate READMEs. [HEURISTIC]

### Machine-readable layout manifests

No established cross-agent machine-readable manifest for repository layout was found after searches for repository manifests, agent navigation schemas, `agents.json`, architecture maps, and codebase maps. [NO SOURCE] AGENTS.md expressly chooses unconstrained Markdown with no required fields, which is the opposite of a parseable layout schema. [VERIFIABLE][^4]

`manifest.yaml` is a concrete April 2026 proposal with fields for project focus, stack, invariants, and a file map, plus an MCP interface. Its own site presents only a small set of examples, and this review found no independent adoption study, vendor commitment, or task-success benchmark. [VERIFIABLE][^26] It is therefore an experiment, not a standard.

A layout manifest becomes attractive only if multiple consumers need the same structured facts and there is a reliable generator or validator against authoritative build/package metadata. Creating one solely for agents adds a second architecture model whose drift must be policed. [INFERENCE]

Repositories that already have workspace manifests, build graphs, package metadata, ownership files, or code-search indexes should expose those sources through tools rather than invent a parallel universal schema. A thin tool can normalize answers such as “which package owns this path?” without making the normalized cache the source of truth. [HEURISTIC]

## 7. What code-localization research changes

The academic and industry evidence increasingly treats navigation as an agent-computer-interface and context-selection problem, not a prompt-writing problem. SWE-agent showed in 2024 that a deliberately designed interface for viewing, searching, editing, and testing materially changes agent performance; its tool design limits file views and concise search output to avoid confusing the model. [VERIFIABLE][^27]

CodeStruct’s ACL 2026 results report that AST-structured read/edit interfaces improved Pass@1 by 1.2–5% and reduced token use by 12–38% for most evaluated models. [VERIFIABLE][^28] Together with SWE-Explore, FastContext, and the FSE comparison, this supports investing in navigable tools and bounded observations rather than ever-larger startup prose. [INFERENCE]

The evidence has important limits. Benchmarks often provide issue text and a known repository snapshot; they do not fully represent proprietary monorepos, polyglot generated-code estates, sparse tests, access-controlled subtrees, or weeks-long architectural changes. Vendor evaluations may measure satisfaction or retained edits rather than reproducible issue resolution. [HEURISTIC]

### Gaps, blindspots & emergent findings

This investigation deliberately countered anchor bias by following four threads not implied by the file-format comparison: instruction files as a security boundary, branch freshness, whether agents actually invoke available retrieval tools, and commit-history-based localization.

**Blindspot 1—untrusted instruction files are executable context.** GitInject demonstrates attacks in live GitHub coding-agent workflows by placing malicious `CLAUDE.md`, `AGENTS.md`, or `GEMINI.md` files in a pull-request branch; all four tested providers were susceptible to at least one attack. [VERIFIABLE][^29] GitHub’s own Copilot documentation says pull-request review uses instructions from the head branch. [VERIFIABLE][^6] Therefore nested context discovery crosses a trust boundary: CI/review agents should identify instruction changes, constrain tools and credentials, and avoid granting untrusted branch text authority over secrets or irreversible actions. [INFERENCE]

**Blindspot 2—availability is not use.** In the narrow CodeCompass preprint, a graph-backed MCP tool greatly helped hidden-dependency tasks, yet 58% of trials with graph access made zero graph calls when the prompt did not explicitly encourage it. [VERIFIABLE][^30] A repository can buy or build excellent retrieval and still fail if the agent does not know when to invoke it; the root orientation should name the preferred discovery tools and trigger conditions. [INFERENCE]

**Blindspot 3—branch freshness can dominate retrieval quality.** Devin warns that its planning index follows the main branch and may miss recent changes or other branches. [VERIFIABLE][^31] Any server-side index, generated map, or cached embedding store needs a visible snapshot identity and a local-search fallback. [INFERENCE]

**Emergent contradiction—retrieval practice is moving faster than documentation.** Cursor’s January 2026 blog calls semantic search a major performance driver and documents embedding indexes. [VERIFIABLE][^10] In a July 22, 2026 staff forum response, Cursor said semantic/embedding indexing was being turned down in favor of repository-local grep retrieval. [VERIFIABLE][^32] This may describe a staged rollout rather than a completed global product change; the current scope and permanence could not be independently resolved. [NO SOURCE] The durable lesson is to specify capabilities (“search current code; follow definitions”) rather than bind repository guidance to one vendor’s retrieval implementation. [INFERENCE]

**Emergent signal—history is part of structure.** Repository Memory uses commit history as non-parametric memory and combines it with module summaries for localization. [VERIFIABLE][^21] File placement is partly social and temporal—what changed together and which module usually owns similar changes—not merely spatial. Static directory prose cannot capture that signal economically. [INFERENCE]

**Unresolved blindspots.** No public evidence located isolates navigation outcomes for very large private monorepos, measures the maintenance cost of nested agent files, compares CI staleness mechanisms, or quantifies how often agents obey versus ignore placement guidance. [NO SOURCE] No robust study found tests a universal layout manifest, per-directory agent READMEs, or a committed generated directory tree. [NO SOURCE]

## Comparison

The table is a synthesis of the evidence above; its judgments are recommendations rather than independently measured rankings. [INFERENCE]

| Approach | Best information | Startup cost | Freshness | Monorepo scaling | Main failure mode | Recommended role |
|---|---|---:|---|---|---|---|
| Lean root agent file | Stable topology, placement decisions, hard constraints, canonical discovery commands | Low | Manual | Good if it routes rather than catalogs | Becomes a handbook | Always-on orientation contract |
| Nested/path-scoped rules | Subsystem-specific constraints and commands | Conditional | Manual | Good at real boundaries | Proliferation and contradictory inheritance | Selective local overlay |
| Component README/docs | Rationale, concepts, workflows humans also need | On demand | Manual | Good with ownership | Silent stale references | Durable explanatory source |
| Committed generated tree/map | Current paths at generation time | Medium–high if loaded | Regeneration required | Poor without task ranking | Context dilution; false authority | Avoid as default |
| Live lexical/semantic/LSP/graph retrieval | Current files, symbols, dependencies, references | Task-dependent | Potentially branch-current | Best when ranked and bounded | Missed calls, stale remote index, compute | Primary navigation mechanism |
| Machine-readable layout manifest | Normalized facts for multiple consumers | Low–medium | Requires validation/generation | Potentially good | Second source of truth; no standard | Only when existing consumers justify it |

## Recommendation

Adopt a **thin contract plus live graph** architecture. The checked-in contract should answer “what is non-obvious and normative?”; tools should answer “what exists here now?” [INFERENCE]

### Recommended information architecture

```text
authoritative code/build/package/ownership metadata
                 │
                 ├── live lexical search, LSP/static analysis, history/graph
                 │        └── task-ranked observations for the agent
                 │
                 ├── component README / architecture docs (human + agent, on demand)
                 │
                 └── lean root AGENTS.md (portable orientation contract)
                          ├── thin CLAUDE.md import/adapters where required
                          └── selective nested AGENTS.md or path-scoped rules
```

This design keeps fast-changing topology queryable, durable rationale readable, and non-inferable constraints permanently visible. [INFERENCE]

### Recommended root contract

Keep the root file short enough to review in one sitting and include only:

1. **Purpose and shape:** two or three sentences naming the major runtime/build domains.
2. **Stable routing table:** roughly 5–12 canonical paths, each with a role—not a recursive tree.
3. **Placement decisions:** explicit “new X goes in Y” rules, including where similar-looking alternatives are wrong.
4. **Authority boundaries:** generated versus source directories, vendored/frozen areas, canonical schemas, dependency-direction rules.
5. **Discovery commands:** the repository’s fastest commands for packages, targets, symbols, ownership, references, tests, and generated sources.
6. **Pointers:** links to component docs or local instructions, with a sentence saying when to load each.

The exact item count is a heuristic, not a benchmark-derived threshold. [HEURISTIC] Anthropic’s under-200-line target and the CTX-Bench cost result support a small budget but do not establish a universal optimum. [VERIFIABLE][^1][^15]

A useful shape is:

```markdown
# Repository orientation

<purpose and three-sentence architecture>

## Where work belongs
- New public API surface: `packages/api/...`; update `schemas/...` first.
- Runtime implementations: `services/<name>/...`; shared code requires two consumers.
- Generated clients: `gen/...`; never edit directly—run `<command>`.

## Find the current structure
- Packages/targets: `<authoritative build query>`
- Symbols/references: `<repo-supported search or LSP command>`
- Owners: `<ownership query>`

## Local guidance
- When editing `<subtree>`, read `<local instruction or README>`.
```

Do not include an exhaustive tree, full API catalog, changelog, style rules already enforced by tools, or descriptions that merely restate filenames. [HEURISTIC]

### Placement and maintenance rules

- Make `AGENTS.md` the portable canonical text where supported; use generated symlinks/import wrappers or very small adapters for tools with different discovery names. [INFERENCE]
- Add nested guidance only when crossing a build, ownership, language, generated-code, or architectural boundary. [HEURISTIC]
- Prefer decision rules over descriptions: “put migrations beside the owning service because deploys are isolated” survives more refactors than a file list. [HEURISTIC]
- Link to existing human documentation instead of duplicating it into startup context. [HEURISTIC]
- Generate/query observable maps on demand, attach a commit/snapshot identity to remote indexes, and fall back to local search when the branch differs. [INFERENCE]
- Lint checked-in path references and have code owners review relevant context when directories move. [HEURISTIC]
- Treat instruction changes from untrusted branches as security-sensitive inputs. [INFERENCE]
- Measure navigation with repository-specific tasks: correct target file, unnecessary files opened, tokens, elapsed time, and whether prohibited/generated paths were modified. [HEURISTIC]

### Acceptance criteria

A repository guidance system is fit for purpose when:

1. **Orientation:** When an agent begins at the repository root, it can name the major domains and the authoritative discovery commands without receiving a recursive file list.
2. **Placement:** When asked to add each representative artifact type, the agent selects the intended package/directory and cites the applicable source-of-truth or local rule.
3. **Scope:** When work enters a subsystem with distinct rules, the relevant local guidance is loaded; unrelated subsystem guidance is not always loaded.
4. **Freshness:** When a referenced path is moved or removed, validation fails or the reference is updated in the same change.
5. **Retrieval:** When a task depends on current symbols or dependencies, the agent queries current code/build/LSP data rather than relying on a static snapshot.
6. **Generated code:** When a task touches derived artifacts, the agent edits the authoritative source and runs the documented generator.
7. **Security:** When a pull request changes agent instructions, reviewers and automation surface the change and do not grant the branch new secrets or irreversible tool authority.
8. **Economy:** In sampled tasks, the guidance reduces wrong-directory edits or discovery work without materially increasing total tokens or irrelevant file reads.

These are testable EARS-style operational criteria proposed from the evidence, not published benchmark thresholds. [HEURISTIC]

## Open Questions

1. What is the causal effect of a **placement-only** root file versus no file, holding retrieval tools constant?
2. At what repository size and modularity does nested guidance outperform one root file plus retrieval?
3. Which maintenance control—path linting, ownership review, generation, or task evaluation—best reduces drift per maintainer-hour?
4. Can a portable capability protocol expose package graphs, owners, generated-source provenance, and snapshot identity without creating a new source of truth?
5. How should agents resolve conflicting instructions when several vendor files are loaded and no precedence is defined?
6. How should hosted review agents authenticate trusted instructions while still evaluating changes to those instructions?
7. Do history-aware and graph retrieval results transfer to polyglot private monorepos with sparse public-like issue descriptions?
8. Will Cursor’s July 2026 shift toward grep retrieval persist, and under what repository shapes does lexical retrieval replace rather than complement embeddings?

## Sources

[^1]: Anthropic. (n.d.). [Manage Claude’s memory](https://code.claude.com/docs/en/memory). Claude Code Documentation. Verified accessible (HTTP 200) 2026-07-26. (Current official discovery, hierarchy, imports, scoping, and context-budget guidance.)

[^2]: Anthropic. (2026, April 15). [Give Claude context: CLAUDE.md and better prompts](https://support.claude.com/en/articles/14553240-give-claude-context-claude-md-and-better-prompts). Claude Help Center. Verified accessible (HTTP 200) 2026-07-26. (Official advice on structure content, length, exclusions, and maintenance.)

[^3]: OpenAI. (n.d.). [Custom instructions with AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md). OpenAI Developers. Verified accessible (HTTP 200 after redirect) 2026-07-26. (Current official Codex instruction discovery, hierarchy, precedence, and size limit.)

[^4]: AGENTS.md Project. (n.d.). [AGENTS.md](https://agents.md/). AGENTS.md. Verified accessible (HTTP 200) 2026-07-26. (Current convention scope, suggested content, nested files, syntax, and self-reported adoption.)

[^5]: OpenAI. (2026). [How OpenAI uses Codex](https://cdn.openai.com/pdf/6a2631dc-783e-479b-b1a4-af0cfbd38630/how-openai-uses-codex.pdf). OpenAI. Verified accessible (HTTP 200) 2026-07-26. (Published internal practices for prompts, repository exploration, and AGENTS.md.)

[^6]: GitHub. (n.d.). [Adding repository custom instructions for GitHub Copilot](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/add-custom-instructions/add-repository-instructions). GitHub Docs. Verified accessible (HTTP 200) 2026-07-26. (Current supported formats, path scope, generation guidance, and pull-request instruction source.)

[^7]: Google. (n.d.). [Provide context with GEMINI.md files](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/gemini-md.md). Gemini CLI Documentation. Verified accessible (HTTP 200) 2026-07-26. (Current hierarchical and just-in-time context discovery.)

[^8]: Cognition. (n.d.). [Knowledge onboarding](https://docs.devin.ai/onboard-devin/knowledge-onboarding). Devin Documentation. Verified accessible (HTTP 200) 2026-07-26. (Current auto-generated, trigger-retrieved repository knowledge and review requirement.)

[^9]: Cursor. (n.d.). [Rules for AI](https://docs.cursor.com/context/rules-for-ai). Cursor Documentation. Verified accessible (HTTP 200) 2026-07-26. (Current project rule types, scoping, and AGENTS.md compatibility.)

[^10]: Stribling, J. (2026, January 27). [Securely indexing large codebases](https://cursor.com/blog/secure-codebase-indexing). Cursor. Verified accessible (HTTP 200) 2026-07-26. (Vendor evaluation and implementation of semantic codebase indexing.)

[^11]: OpenAI. (n.d.). [AGENTS.md](https://github.com/openai/codex/blob/main/AGENTS.md). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Current public repository example of placement, documentation, and architectural-boundary rules.)

[^12]: Apache Software Foundation. (n.d.). [AGENTS.md](https://github.com/apache/airflow/blob/main/AGENTS.md). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Current public monorepo example with key-path map and architecture guidance.)

[^13]: Temporal Technologies. (n.d.). [AGENTS.md](https://github.com/temporalio/sdk-java/blob/main/AGENTS.md). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Current public multi-module SDK example.)

[^14]: Kubernetes Authors. (n.d.). [AGENTS.md](https://github.com/kubernetes/kubernetes/blob/master/AGENTS.md). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Current concise public monorepo example with source/generated boundary.)

[^15]: Gloaguen, T., et al. (2026). [Evaluating AGENTS.md: Are repository-level context files helpful for coding agents?](https://arxiv.org/abs/2602.11988). arXiv. Verified accessible (HTTP 200) 2026-07-26. (CTX-Bench controlled study of task success, behavior, and cost.)

[^16]: Lulla, J. L., et al. (2026). [On the impact of AGENTS.md files on the efficiency of AI coding agents](https://arxiv.org/abs/2601.20404). arXiv. Verified accessible (HTTP 200) 2026-07-26. (Five-page preprint reporting runtime and token effects.)

[^17]: Aider. (n.d.). [FAQ](https://aider.chat/docs/faq.html). Aider Documentation. Verified accessible (HTTP 200) 2026-07-26. (Current repository-map behavior, context dilution, and weaker-model warning.)

[^18]: Tan, W. S., Wagner, M., & Treude, C. (2024). [Detecting outdated code element references in software repository documentation](https://link.springer.com/article/10.1007/s10664-023-10397-6). Empirical Software Engineering, 29, Article 5. Verified accessible (HTTP 200) 2026-07-26. (Large-scale repository-documentation staleness study; published online in 2023 in the 2024 volume.)

[^19]: Treude, C., & Baltes, S. (2026). [Context rot in AI-assisted software development: Repurposing documentation consistency for AI configuration artifacts](https://arxiv.org/abs/2606.09090). arXiv. Verified accessible (HTTP 200) 2026-07-26. (Preprint on stale context-file path references and validation limits.)

[^20]: Microsoft. (n.d.). [Language Server Protocol specification](https://microsoft.github.io/language-server-protocol/). Microsoft. Verified accessible (HTTP 200) 2026-07-26. (Current standard code-navigation capabilities.)

[^21]: Microsoft Research. (2026). [Improving code localization with repository memory](https://www.microsoft.com/en-us/research/publication/improving-code-localization-with-repository-memory/). Microsoft Research. Verified accessible (HTTP 200) 2026-07-26. (Commit-history and module-summary retrieval for localization.)

[^22]: Zhang, S., et al. (2026). [SWE-Explore: Benchmarking how coding agents explore repositories](https://arxiv.org/abs/2606.07297). arXiv. Verified accessible (HTTP 200) 2026-07-26. (Repository-exploration benchmark across languages and repositories.)

[^23]: Zhang, S., et al. (2026). [FastContext: Training efficient repository explorer for coding agents](https://arxiv.org/abs/2606.14066). arXiv. Verified accessible (HTTP 200; withdrawn version) 2026-07-26. (Withdrawn preprint; the page says the article requires withdrawal and re-approval because of product-IP issues.)

[^24]: Li, Y., et al. (2026). [One size does not fit all: Revisiting code context engineering for repository-level code generation](https://conf.researchr.org/details/fse-2026/fse-2026-research-papers/65/One-Size-Does-Not-Fit-All-Revisiting-Code-Context-Engineering-for-Repository-Level-C). ACM FSE 2026. Verified accessible (HTTP 200) 2026-07-26. (Comparison of similarity, static-analysis, and navigation context paradigms.)

[^25]: Google. (n.d.). [README.md files](https://android.googlesource.com/platform/external/google-styleguide/+/edccafae5a784a99ef0e7d548f7586272a724087/docguide/READMEs.md). Google Documentation Style Guide. Verified accessible (HTTP 200) 2026-07-26. (Commit-pinned directory README purpose and recommended minimum content.)

[^26]: Manifest Project. (2026). [Manifest](https://manifestfile.com/). Manifest. Verified accessible (HTTP 200) 2026-07-26. (Nascent `manifest.yaml` repository-context proposal.)

[^27]: Yang, J., et al. (2024). [SWE-agent: Agent-computer interfaces enable automated software engineering](https://papers.nips.cc/paper_files/paper/2024/hash/5a7c947568c1b1328ccc5230172e1e7c-Abstract-Conference.html). NeurIPS 2024. Verified accessible (HTTP 200) 2026-07-26. (Foundational ACI and bounded repository-navigation interface.)

[^28]: Kim, M., Hsu, C.-C., Wang, D., Garg, S., Kumar, V., & Ramanathan, M. K. (2026). [CODESTRUCT: Code agents over structured action spaces](https://aclanthology.org/2026.acl-long.607/). Proceedings of ACL 2026. Verified accessible (HTTP 200) 2026-07-26. (AST-structured interaction results.)

[^29]: Isbarov, J., Suleymanov, U., Shumailov, I., & Kantarcioglu, M. (2026). [GitInject: Real-world prompt injection attacks in AI-powered CI/CD pipelines](https://arxiv.org/abs/2606.09935). arXiv. Verified accessible (HTTP 200) 2026-07-26. (Live-workflow attacks through repository instruction files.)

[^30]: Paipuru, T. (2026). [CodeCompass: Navigating the navigation paradox in agentic code intelligence](https://arxiv.org/abs/2602.20048). arXiv. Verified accessible (HTTP 200) 2026-07-26. (Single-author preprint reporting 258 trials on one FastAPI repository.)

[^31]: Cognition. (n.d.). [Interactive planning](https://docs.devinenterprise.com/work-with-devin/interactive-planning). Devin Enterprise Documentation. Verified accessible (HTTP 200) 2026-07-26. (Current index-based planning and branch-freshness limitation.)

[^32]: Cursor Staff. (2026, July 22). [What do you think about Cursor removing the codebase indexing settings?](https://forum.cursor.com/t/what-do-you-think-about-cursor-removing-the-codebase-indexing-settings/165899/10). Cursor Forum. Verified accessible (HTTP 200) 2026-07-26. (First-party staff statement on migration from embedding indexing to grep retrieval.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** ISSUE-REF

**Model:** `GPT-5 Codex`

**Date:** 2026-07-26

```text
Author a research document into docs/research/agent-context-repo-structure-navigation.md (a scaffolded stub already at that path — fill and expand it, keep its frontmatter region markers intact, set status: complete and source: "codex-cx-research-2026-07-26"). It must conform to the canonical research template at docs/research/TEMPLATE.md (template_version research-1.2.0) — read that file first and follow its section order exactly.

# Research question

How should a codebase expose its structure, layout, and "how to navigate this repo" guidance to CODING AGENTS (Claude Code, OpenAI Codex, Cursor, Devin, Aider, and comparable tools) as of July 2026?

Concretely: when an agent starts a session in an unfamiliar repo and needs to know where things live, where a new file belongs, and how the repo is organized — what is the state of the art for supplying that? Is the right home an agent context file (CLAUDE.md / AGENTS.md / .cursorrules and successors), a generated index, per-directory README files, a machine-readable manifest, retrieval over the codebase, or something else entirely?

## Scope note — questions, examples, and named references are a starting point, not a checklist
The questions, topics, and named examples below are illustrative anchors and a FLOOR for this research — not an exhaustive list to answer only or evaluate only. Reason independently: survey the landscape broadly, follow the evidence where it leads, expand scope where warranted, and surface relevant work, factors, and failure modes not named here. Actively resist answering only the listed questions or evaluating only the named approaches — an output that merely fills in the listed items has NOT met the research goal.

## Independent exploration (gaps, blindspots, emergent threads) — required
Treat the question list as a FLOOR, not a ceiling. As you research, actively surface what this framing may be missing and pursue each promising thread to a logical conclusion:
- Adjacent or upstream factors the questions don't capture.
- Contrarian / disconfirming evidence — report it even when it challenges the premise.
- Emerging 2025–2026 practices, tools, or research not anchored by the named examples.
- Known failure modes and second-order effects.
Whenever a load-bearing thread surfaces mid-research, follow it to its conclusion and report it in a dedicated "Gaps, blindspots & emergent findings" subsection. Explicitly NAME any blindspot you suspect but cannot resolve (and why) rather than omitting it. Anchor bias — over-fitting to the listed questions and example approaches — is a known failure mode; counter it deliberately and say where you did.

# Anchor questions (a floor, not a ceiling)

1. What do the major agent vendors actually specify and recommend? Anthropic (CLAUDE.md), OpenAI (AGENTS.md), Cursor, and others — what do their own docs, engineering blogs, and published internal practices say about repo-structure guidance? Where do their recommendations conflict?
2. What does the AGENTS.md convention actually standardize, how widely is it adopted, and does it address repo layout/navigation or only conventions and commands?
3. Survey real open-source repositories with mature agent context files. What do high-quality ones do about structure guidance? Include large monorepos, since navigation cost scales there.
4. Generated indexes vs. hand-maintained prose: is there evidence either way on staleness, agent task success, or token cost? Has anyone measured whether a file listing helps an agent at all versus hurting via context dilution?
5. Retrieval and tooling alternatives: repo maps (aider's repomap / tree-sitter approaches), semantic code search, MCP servers exposing structure, LSP-backed navigation. When does retrieval beat a static description?
6. Per-directory README files as a pattern: prior art, evidence, failure modes.
7. Machine-readable manifests describing repo layout — do any exist as a real convention, and did they get traction?
8. Context-window economics: what is known about the cost of always-loaded structural guidance versus loading it on demand? Any published guidance on what earns permanent context budget?
9. Staleness and drift: what mechanisms do people use to keep structural docs true, and what evidence exists about which mechanisms actually work rather than creating maintenance burden with no reader?
10. Academic and industry-lab research on agent codebase comprehension, context engineering, and navigation — SWE-bench-adjacent work, agent-computer-interface research, anything measuring how agents locate code.

# Retrieval requirements (mandatory)

For each load-bearing claim, make at least one web_search or web_fetch call. Do NOT return [VERIFIABLE] for any claim without a URL you actually fetched in this session — cached or remembered URLs are not sufficient evidence. Do not stop after one round: if no source is found for a sub-question, run additional searches across alternate framings before tagging it [NO SOURCE]. That tag is reserved for genuine evidence absence, never for retrieval avoidance. Do not exit "satisfied" after zero retrieval.

# Required appendix

End the document with `## Appendix: Research Prompt` containing Registry ID (use ISSUE-REF), Model, Date (2026-07-26), and the full prompt text in a fenced text block. Then `## Appendix: Provenance Ledger` — a table of claim → source URL → verbatim quote from the fetched page → verdict (SUPPORTED / PLAUSIBLE / UNVERIFIABLE / CONTRADICTED) → live?. Only tag a claim [VERIFIABLE] when the quote literally appears in the page you fetched, the entailment verdict is SUPPORTED, and the URL is live.

<grounding_instructions>
You are a principal developer-experience engineer who has designed and maintained agent-facing documentation systems for large production monorepos, and who has watched several such systems rot into unread artifacts. You have strong opinions backed by evidence about what documentation actually gets consumed versus what merely gets maintained. When you cannot find a source, you say so explicitly rather than filling the gap with plausible-sounding practice.

Temporal scope: Weight sources by recency — 2026 (primary) → 2025 → 2024. Pre-2024 sources are background context only unless foundational to the topic. If post-2024 literature is genuinely sparse for a subtopic, state "[subtopic]: no significant post-2024 developments found" rather than backfilling with older sources. Backfilling is a failure mode, not a hedge.

Before generating your final output, execute a Chain-of-Verification (CoVe) to ensure factual fidelity over compliance.

Inside your thought process:
1. Isolate the core facts required.
2. Draft a tentative response.
3. Hostile Cross-Examination: flag any claim where you are citing a source because the prompt implied you should, rather than because you verified it.
4. Strip away any claim that cannot be empirically verified.

When generating your final output, classify every major claim. Write your rationale before appending the tag — writing the tag first causes post-hoc rationalization. Rationale → evidence check → tag.

- [VERIFIABLE]: backed by documentation, peer-reviewed research, or official tech blogs (2024–2026). Carry an inline footnote ref to the source: [VERIFIABLE][^N].
- [HEURISTIC]: widely accepted best practice without a specific citation.
- [INFERENCE]: a logical conclusion drawn from context. Provide your reasoning in-text. Do not fabricate a source. Tier tag only — NO footnote ref.
- [NO SOURCE]: explicitly state when you cannot find verifiable data. Tier tag only — NO footnote ref.

Citation format (mandatory for every externally-sourced claim):
- Inline: append the GFM footnote ref directly after the tier tag — [VERIFIABLE][^N]. A claim citing multiple sources carries ascending separate refs — [VERIFIABLE][^3][^7] (never grouped [^3,^7]; never out of order).
- Footnote definitions live once, under ## Sources, in APA form with a clickable URL or DOI and an access-verification stamp. Worked example:
    [^1]: LangChain. (2026). [Threads](https://docs.langchain.com/langsmith/threads). LangSmith Documentation. Verified accessible (HTTP 200) 2026-06-03. (Scope note.)
- URL-or-DOI ALWAYS: every source entry carries a clickable URL or a doi.org link — paywalled/gated is fine (link it anyway; stamp the access status). Only the truly-irreducible case (no online catalog presence anywhere) gets an explicit [no online source located] marker with a one-line justification.
- Integrity: footnote refs are contiguous from [^1], every [^N] ref has a matching definition, and every definition is referenced — no gaps, no orphans.
- [INFERENCE] / [NO SOURCE] claims carry the tier tag with NO footnote ref.

Hard constraint (overrides all formatting preferences): never invent a citation to satisfy a formatting instruction. Accuracy > completeness.

Format diagrams using Mermaid.js or ASCII. Format math using LaTeX.
NEVER generate binary images.
</grounding_instructions>
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

| Claim | Source URL | Verbatim quote from fetched page | Verdict | Live? |
|---|---|---|---|---|
| Claude guidance favors concise architecture and scoped rules. | https://code.claude.com/docs/en/memory | “More specific instructions typically work better.” / “Each CLAUDE.md file consumes context at startup.” / “Target under 200 lines per CLAUDE.md file.” | SUPPORTED | yes |
| Anthropic excludes exhaustive trees from permanent context. | https://support.claude.com/en/articles/14553240-give-claude-context-claude-md-and-better-prompts | “What does not belong in CLAUDE.md: … Full file trees or descriptions of every module” | SUPPORTED | yes |
| Codex loads a root-to-CWD AGENTS.md chain with nearer precedence and a byte cap. | https://learn.chatgpt.com/docs/agent-configuration/agents-md | “Codex builds an instruction chain once per run.” / “Instructions closer to your current working directory take precedence” / “Codex stops adding files once the combined size reaches 32 KiB” | SUPPORTED | yes |
| AGENTS.md is Markdown without required fields and supports nested scope. | https://agents.md/ | “No required fields. Just standard Markdown.” / “For large monorepos, place nested AGENTS.md files in each package.” | SUPPORTED | yes |
| The AGENTS.md project self-reports visible open-source adoption. | https://agents.md/ | “Used by over 60k open-source projects.” | SUPPORTED | yes |
| OpenAI says AGENTS should capture non-inferable repository knowledge. | https://cdn.openai.com/pdf/6a2631dc-783e-479b-b1a4-af0cfbd38630/how-openai-uses-codex.pdf | “AGENTS.md files typically name conventions, explain business logic, call out quirks, and document dependencies that Codex cannot infer on its own.” | SUPPORTED | yes |
| Copilot supports repo-wide, path-specific, and AGENTS.md instruction forms. | https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/add-custom-instructions/add-repository-instructions | “You can create one or more `AGENTS.md` files anywhere within the repository.” / “Path-specific instructions apply to files that match a specified path.” | SUPPORTED | yes |
| Copilot pull-request review reads instructions from the proposed branch. | https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/add-custom-instructions/add-repository-instructions | “Copilot uses the version of the custom instructions file from the head branch of the pull request.” | SUPPORTED | yes |
| Gemini CLI loads hierarchical and just-in-time directory context. | https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/gemini-md.md | “The CLI discovers `GEMINI.md` files hierarchically” / “Just-in-time (JIT) context discovery” | SUPPORTED | yes |
| Devin generates repository knowledge from README, structure, and contents. | https://docs.devin.ai/onboard-devin/knowledge-onboarding | “Devin automatically generates Repository Knowledge by analyzing your repository's README files, file structure, and file contents.” | SUPPORTED | yes |
| Cursor rules have multiple attachment modes. | https://docs.cursor.com/context/rules-for-ai | “Always” / “Auto Attached” / “Agent Requested” / “Manual” | SUPPORTED | yes |
| Cursor’s January evaluation reports a semantic-search accuracy improvement. | https://cursor.com/blog/secure-codebase-indexing | “Semantic search … improved response accuracy by 12.5% on average” | SUPPORTED | yes |
| OpenAI Codex’s public AGENTS file contains placement and boundary rules. | https://github.com/openai/codex/blob/main/AGENTS.md | “Do not add general documentation to `docs/`.” / “The `codex-core` crate … contains the business logic” | SUPPORTED | yes |
| Airflow’s AGENTS file supplies a key-path repository map. | https://github.com/apache/airflow/blob/main/AGENTS.md | “## Repository Structure” / “Here is a high-level overview of the key directories” | SUPPORTED | yes |
| Temporal’s AGENTS file supplies a concise module layout. | https://github.com/temporalio/sdk-java/blob/main/AGENTS.md | “## Repository Layout” / “This is a multi-module Gradle project.” | SUPPORTED | yes |
| Kubernetes documents a non-obvious source/generated boundary. | https://github.com/kubernetes/kubernetes/blob/master/AGENTS.md | “The source of truth for staging repos is under `staging/src/k8s.io/`.” | SUPPORTED | yes |
| CTX-Bench found no general success gain and more than 20% average cost from context. | https://arxiv.org/abs/2602.11988 | “providing context files does not generally improve task success rates, while increasing inference cost by over 20% on average” / “repository overviews … are not helpful” | SUPPORTED | yes |
| A smaller 2026 study reports lower runtime and output tokens with AGENTS.md. | https://arxiv.org/abs/2601.20404 | “median runtime reduction of 28.64%” / “output token reduction of 16.58%” | SUPPORTED | yes |
| Aider warns irrelevant context and repo maps can confuse models. | https://aider.chat/docs/faq.html | “Adding a bunch of files that are mostly irrelevant … will often distract or confuse the LLM.” / “weaker models get easily overwhelmed and confused by the content of the repo map” | SUPPORTED | yes |
| README references are commonly stale. | https://link.springer.com/article/10.1007/s10664-023-10397-6 | “28.9% of the popular projects currently contain at least one outdated reference” / “82.3% … have had at least one outdated reference” | SUPPORTED | yes |
| A 2026 preprint reports context-file path staleness and its validation limits. | https://arxiv.org/abs/2606.09090 | “23% of repositories contain at least one stale reference” / “64% were genuine” | SUPPORTED | yes |
| LSP standardizes definition/reference navigation. | https://microsoft.github.io/language-server-protocol/ | “programming language specific features like auto complete, go to definition, or find all references” | SUPPORTED | yes |
| Repository Memory combines history and module summaries for localization. | https://www.microsoft.com/en-us/research/publication/improving-code-localization-with-repository-memory/ | “leverages commit history as a non-parametric memory and combines it with module-level summaries” | SUPPORTED | yes |
| SWE-Explore is a broad repository-exploration benchmark. | https://arxiv.org/abs/2606.07297 | “848 real-world GitHub issues spanning 10 programming languages and 203 repositories” / “agentic explorers consistently outperform classical retrieval baselines” | SUPPORTED | yes |
| FastContext reports better resolution and fewer tokens, but the report is withdrawn. | https://arxiv.org/abs/2606.14066 | “improves end-to-end resolution rates up to 5.5% while reducing coding-agent token consumption up to 60%” / “This paper has been withdrawn” | SUPPORTED | yes |
| The FSE comparison finds different context paradigms have different tradeoffs. | https://conf.researchr.org/details/fse-2026/fse-2026-research-papers/65/One-Size-Does-Not-Fit-All-Revisiting-Code-Context-Engineering-for-Repository-Level-C | “static analysis achieves the best balance of effectiveness and efficiency” / “navigation-based method offers the highest performance ceiling but incurs 10–20× higher computational cost” | SUPPORTED | yes |
| Google treats READMEs as directory landing pages. | https://android.googlesource.com/platform/external/google-styleguide/+/edccafae5a784a99ef0e7d548f7586272a724087/docguide/READMEs.md | “README.md files are intended to provide orientation for engineers browsing your code” / “The README.md file is the landing page for the directory.” | SUPPORTED | yes |
| Manifest is a nascent structured repository-context proposal. | https://manifestfile.com/ | “Manifest launched April 17, 2026.” / “A `manifest.yaml` file at your project root” | SUPPORTED | yes |
| SWE-agent attributes performance gains to its repository interaction interface. | https://papers.nips.cc/paper_files/paper/2024/hash/5a7c947568c1b1328ccc5230172e1e7c-Abstract-Conference.html | “We design a custom agent-computer interface (ACI) that significantly improves agents' ability to browse the repository, view and edit code files, and execute tests.” | SUPPORTED | yes |
| CodeStruct reports accuracy and token improvements from structured interaction. | https://aclanthology.org/2026.acl-long.607/ | “improves Pass@1 by 1.2–5.0%” / “reducing token usage by 12–38% for most models” | SUPPORTED | yes |
| GitInject demonstrates repository-instruction prompt injection in live workflows. | https://arxiv.org/abs/2606.09935 | “all four providers are susceptible to at least one attack” / “malicious configuration files such as CLAUDE.md, AGENTS.md, and GEMINI.md” | SUPPORTED | yes |
| CodeCompass observed that agents often did not call an available graph tool. | https://arxiv.org/abs/2602.20048 | “58% of trials with graph access made zero graph calls” | SUPPORTED | yes |
| Devin’s planning index can be stale relative to a branch. | https://docs.devinenterprise.com/work-with-devin/interactive-planning | “the index is based on the main branch and may not reflect recent changes or other branches” | SUPPORTED | yes |
| Cursor staff described a July migration toward grep retrieval. | https://forum.cursor.com/t/what-do-you-think-about-cursor-removing-the-codebase-indexing-settings/165899/10 | “semantic/embeddings based indexing … is being turned down in favor of grep-based retrieval” | SUPPORTED | yes |

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

- 2026-07-26 — Authored initial complete research synthesis with live-source retrieval, claim tiers, source footnotes, provenance ledger, and Chain-of-Verification review (`codex-cx-research-2026-07-26`).

<!-- /doc:region name="run_history" -->
