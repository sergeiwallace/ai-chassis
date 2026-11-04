---
title: "Documentation Versioning and Tooling Compatibility for Architecture Docs"
category: research
tags: [research, documentation, versioning, doc-as-code, mermaid, architecture-docs, tooling]
status: draft
source: "Genericized from prior personal research; external claims cited inline."
template_version: "research-1.1.0"
---
<!-- doc:region name="context" kind="immutable" -->

## Context

(none — context not recorded in original doc)

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->


## Temporal Scope

**Primary window:** 2024 through late 2025, with emphasis on documentation tooling and
AI-coding-assistant conventions that crystallized in that period (AGENTS.md spec, MADR
4.0, Claude Code memory model, Anthropic Skills, Atlassian Remote MCP).

**Background:** pre-2024 material used only for foundational frameworks (Diátaxis,
Pandoc, classic Zettelkasten, original ADR concept by Michael Nygard 2011).

**Critical caveat on currency:** the underlying research session did not surface live
retrieval results; findings are based on knowledge current through early-to-mid 2025
plus critic feedback. The most time-sensitive claim — whether Claude Code natively loads
`AGENTS.md` — is explicitly flagged as requiring live confirmation against Anthropic's
current docs before any operational decision. Quantitative figures from benchmarks
(Plot2Code, ChartMimic, Aider) are stated as ranges with explicit uncertainty rather
than precise scores.

Source weighting: vendor primary docs (Anthropic, Atlassian, OpenAI) > spec sites
(agents.md, MADR, Diátaxis) > peer-reviewed/arXiv benchmarks > community implementations
(Aider, log4brains, kovetskiy/mark) > training-data recall for general practice.

---

## Executive Summary

1. **Standardize on a Markdown-first, schema-validated corpus** (CommonMark + GFM, YAML
   frontmatter validated in CI, ATX headings, fenced code with language tags, inline
   links). This single choice maximizes coding-assistant edit reliability, aligns with
   MADR 4.0, and supports a unified ADR / Zettelkasten / Diátaxis corpus in one Git repo.

2. **Use `CLAUDE.md` with an `@AGENTS.md` import as the portable agent-instructions
   pattern**, but verify against Anthropic's current Claude Code documentation before
   publishing — native AGENTS.md support is a moving target and double-loading is a real
   failure mode.

3. **For diagrams, treat Mermaid source in Git as authoritative and publish
   pre-rendered SVG to Confluence.** Confluence Cloud's default editor does not render
   Mermaid; Marketplace apps work but break round-tripping with a Git source-of-truth.
   Pre-rendering at build time avoids vendor lock-in.

4. **Do not bulk-convert legacy SVG diagrams to Mermaid/PlantUML via vision LLMs.**
   Structural fidelity on multi-node architecture diagrams is materially below acceptable
   thresholds (published benchmarks consistently report sub-60% on high-level structure).
   Convert on-demand with human review when a diagram needs editing; otherwise embed
   as-is.

5. **Validate frontmatter on every PR.** Frontmatter drift under LLM editing (key
   reordering, quote flipping, type coercion, field invention/deletion) is the
   highest-frequency silent-corruption mode. JSON-Schema validation in pre-commit and CI
   is the highest-ROI guardrail.

---

## Findings by Question

### Q1. Coding-assistant edit reliability by documentation format

**[INDUSTRY HEURISTIC]** Edit-format choice (whole-file vs. unified diff vs.
SEARCH/REPLACE vs. architect mode) dominates Markdown-dialect choice as a determinant of
edit success. Aider's polyglot leaderboard demonstrates substantial swings on the same
model across edit formats; weaker models benefit disproportionately from whole-file
replacement, while stronger models tolerate unified diffs. Exact percentage-point swings
vary by model and benchmark version and should be read directly from the live leaderboard
rather than cited as a fixed number.

**[INDUSTRY HEURISTIC]** Within Markdown, the structural cues that correlate with edit
success are:
- **ATX headings** (`#`, `##`) outperform Setext (underline) headings for LLM section
  targeting and patch anchoring.
- **Fenced code blocks** with explicit language tags outperform indented code blocks
  (less ambiguity about boundaries).
- **Inline links** survive edits better than reference-style links (LLMs frequently fail
  to update the reference definition when editing the link text).
- **YAML frontmatter** is reliable only when validated in CI (see Q5).
- **HTML-in-Markdown** (other than `<details>` for collapsible content) is a frequent
  corruption site.

**[VERIFIABLE FACT]** SWE-bench Verified measures patch correctness on real Python
repositories but does not isolate documentation-format variables. Failure-mode analyses
associated with the benchmark consistently cite whitespace, fenced-code boundary, and
frontmatter handling as common patch-application failures, but this is qualitative
observation, not an isolated controlled experiment.

**[NO SOURCE FOUND]** A controlled 2024–2025 study comparing CommonMark vs. GFM vs. MyST
vs. AsciiDoc edit-success rates under coding assistants. The Aider polyglot leaderboard
is the closest proxy; treat all dialect-comparison claims as informed inference, not
measured fact.

**Practical implication:** when optimizing a docs corpus for AI editing, the lever
ordering (highest impact first) is: (1) edit-format choice in the agent harness, (2)
consistent ATX heading hierarchy, (3) fenced code with language tags, (4) inline over
reference links, (5) frontmatter discipline with CI validation, (6) avoidance of
HTML-in-Markdown.

---

### Q2. SVG → diagram-as-code conversion via vision LLMs

**[VERIFIABLE FACT]** Three peer/arXiv benchmarks are relevant:
- **Plot2Code** (Wu et al., 2024, arXiv:2405.07990) — vision LLMs converting scientific
  plots to matplotlib code.
- **ChartMimic** (Shi et al., 2024, arXiv:2406.09961) — chart-to-code with both
  high-level structure and fine-detail scoring rubrics.
- **DiagrammerGPT** (Zala et al., 2023, arXiv:2310.12128) — focuses on *generation* of
  diagrams from text, not conversion from images; its rubric is reused in subsequent
  work.

**[SYNTHESIZED INFERENCE]** Across these benchmarks, the directional finding is
consistent: vision-LLM conversion of diagrams to code achieves moderate success on
simple cases but degrades substantially as node count, edge complexity, and
clustering/grouping semantics increase. Specific percentage scores vary by model version
and rubric and should be read from the papers' current tables rather than memorized —
quoted ranges in any synthesis (including this one) should be treated as illustrative,
not authoritative.

**[INDUSTRY HEURISTIC]** For architecture diagrams (boxes-and-arrows, swim lanes,
sequence diagrams):
- Vision-LLM conversion to Mermaid/PlantUML works adequately for small diagrams with
  clean orthogonal layout.
- Above roughly 10 nodes, edge-routing inference degrades, node labels are paraphrased
  (not transcribed), and group/cluster semantics are commonly lost.
- The most reliable workflow is: (a) extract text labels via OCR or vision LLM in
  text-only mode, (b) produce a first-draft Mermaid from the vision LLM, (c) render
  side-by-side with the source for human review, (d) budget meaningful human cleanup per
  non-trivial diagram.

**Anti-pattern (high confidence):** bulk conversion of legacy SVG corpora to
Mermaid/PlantUML without per-diagram human review. Topological errors propagate silently
and produce a corpus that is materially worse than the original SVGs because errors are
now in code that looks authoritative.

**Better pattern:** convert on-demand, when a diagram needs to be edited anyway.
Otherwise embed the SVG as-is; the cost of leaving it as a raster/vector image is lower
than the cost of silent topological drift.

---

### Q3. Claude Code + AGENTS.md handling

**[VERIFIABLE FACT]** AGENTS.md (https://agents.md/) is a cross-vendor convention
published in 2024–2025 with backing from OpenAI Codex, Cursor, Aider, Jules, Sourcegraph
Amp, Factory, RooCode, and others. Its purpose is to unify the proliferating ecosystem
of per-vendor agent-instruction files (`.cursorrules`, `.aider.conf.yml`, etc.) into a
single vendor-neutral file.

**[VERIFIABLE FACT]** Claude Code's documented memory hierarchy uses `CLAUDE.md`
(project-level), `~/.claude/CLAUDE.md` (user-level), and the `@path/to/file` import
syntax for composition.

**[SYNTHESIZED INFERENCE — requires live confirmation]** As of mid-2025, Anthropic had
not made `AGENTS.md` a first-class auto-loaded file in Claude Code; the community
workaround was either a symlink (`ln -s AGENTS.md CLAUDE.md`) or an `@AGENTS.md` import
inside `CLAUDE.md`. Anthropic has been actively iterating on Claude Code memory features,
and native AGENTS.md support may have shipped after this synthesis. **Verify against
https://docs.claude.com/en/docs/claude-code/memory before publishing operational
guidance.**

**Recommended pattern (robust to either outcome):**

```markdown
# CLAUDE.md
@AGENTS.md
<!-- Claude-Code-specific overrides below this line -->
```

This pattern works whether or not Claude Code natively loads `AGENTS.md`, because
Claude's `@import` mechanism is idempotent — re-loading the same file content does not
duplicate instructions in any harmful way. If native auto-loading ships, the `@AGENTS.md`
line becomes redundant but harmless; if it does not, the import is required.

**Risk to verify:** if native auto-loading is implemented as "additionally append
AGENTS.md to context," then explicit `@AGENTS.md` could double-load. Confirm semantics in
current docs.

---

### Q4. Confluence Mermaid native rendering

**[VERIFIABLE FACT]** Confluence Cloud's native editor does not render Mermaid syntax.
The default `code` macro with a `mermaid` language tag displays source as a code block,
not a rendered diagram.

**[VERIFIABLE FACT]** Atlassian's native diagramming options are the Whiteboards feature
and draw.io integration. Mermaid rendering is provided exclusively via Marketplace apps:
- Mermaid Chart for Confluence (by Mermaid Chart, the company stewarding Mermaid.js)
- Mermaid Diagrams for Confluence (Stiltsoft)
- Mermaid Plugin for Confluence (Resolution GmbH)
- Markdown, HTML, PlantUML, LaTeX, Mermaid for Confluence (Stiltsoft, bundled)

Data Center deployments have additional options including ScriptRunner-based custom
macros not available on Cloud.

**[VERIFIABLE FACT]** Mermaid content authored via Marketplace apps is stored in
Confluence's XML storage format as a macro payload, not as portable Markdown. This breaks
round-tripping with a Git-based source of truth: an edit made in Confluence cannot be
cleanly reflected back into the Git repo without custom tooling that understands the
specific app's macro vocabulary.

**[SYNTHESIZED INFERENCE]** For a Markdown-first pipeline syncing to Confluence, three
viable options exist:
- **(a)** Accept rendering loss in Confluence; link to a separately-rendered SVG/PNG.
  Lowest fidelity, highest portability.
- **(b)** Standardize on one Marketplace app and integrate via its API for sync. Higher
  fidelity, vendor lock-in to that app.
- **(c)** Pre-render Mermaid to SVG at build time and publish via Confluence's standard
  image macro. Best portability, lossless visual rendering, no in-Confluence editability
  of the diagram.

Option (c) is the most common production choice when Git is the source of truth, because
it requires no Marketplace dependency and survives any Confluence-side workflow.

---

### Q5. Frontmatter drift under LLM editing

**[INDUSTRY HEURISTIC]** Established frontmatter failure modes under LLM editing, in
approximate order of frequency:
1. **Key reordering** — LLMs re-sort YAML keys alphabetically or to match training-data
   conventions, breaking diffs and human conventions about key order.
2. **Quote-style flipping** — single↔double quotes, especially around ISO dates, version
   strings, and URLs.
3. **List style mutation** — flow style `[a, b]` ↔ block style `- a\n- b\n`.
4. **Type coercion** — `"1.0"` (string) becomes `1.0` (float), losing the trailing zero;
   ISO date strings become YAML native timestamp types; `"true"` becomes boolean.
5. **Field invention** — LLM adds plausible-looking fields (`updated:`, `author:`,
   `tags:`) not present in the project's schema.
6. **Field deletion** — whole-file rewrite mode drops optional fields the LLM doesn't
   model as important.

**[SYNTHESIZED INFERENCE]** Diff-format edits (Aider's `diff` mode, Claude Code's edit
tool with targeted SEARCH/REPLACE) preserve frontmatter materially better than
whole-file rewrites because they don't touch lines they don't intend to modify. This is
the simplest preventive measure.

**[VERIFIABLE FACT]** Available mitigations:
- **JSON Schema validation** applied to YAML frontmatter via tools like
  `remark-lint-frontmatter-schema`
  (https://github.com/JulianCataldo/remark-lint-frontmatter-schema),
  `front-matter-schema`, or equivalent.
- **yamllint** in pre-commit configured for the project's style preferences (quote
  style, indentation, line length).
- **MADR 4.0** ships a canonical frontmatter schema for ADRs, making JSON-Schema
  validation trivial for that subset.

**Operational recommendation:** reject PRs that mutate frontmatter outside the published
schema. This is the single highest-ROI guardrail against ISSUE-REF silent corruption of
metadata.

---

### Q6. Zettelkasten / ADR / Diátaxis convergence

**[VERIFIABLE FACT]** MADR 4.0 (Markdown Architectural Decision Records,
https://github.com/adr/madr) released in 2024 adopted YAML frontmatter with fields
including `status`, `date`, `decision-makers`, `consulted`, `informed`, and links to
superseded/superseding decisions. This standardization makes MADR-format ADRs
structurally identical to evergreen notes in Obsidian/Foam/Dendron vaults.

**[VERIFIABLE FACT]** log4brains (https://github.com/thomvaill/log4brains) renders
MADR-format ADRs as a static site with backlinks, explicitly inspired by Zettelkasten
linking practice.

**[SYNTHESIZED INFERENCE]** A single Markdown corpus can serve ADRs, Zettelkasten-style
notes, and Diátaxis documentation (tutorials, how-tos, reference, explanation) without
format divergence:
- One Git repo of `.md` files with YAML frontmatter
- A `type:` field discriminator
  (`adr | reference | how-to | tutorial | explanation | decision-log | note`)
- Backlinks via `[[wikilinks]]` (Obsidian/Foam-compatible) or standard Markdown links
- Diátaxis quadrant as a tag for documentation pages
- MADR fields for decision pages
- Frontmatter schema branching by `type` (JSON Schema `oneOf`)

**Practical benefit:** AI-assisted maintenance operates on one corpus with one
validation schema instead of three separate systems with three drift surfaces.

---

### Q7. Pandoc fidelity in cross-format conversions

**[VERIFIABLE FACT]** Established Pandoc loss patterns:
- **HTML → Markdown:** class/id attributes dropped by default (recoverable via attribute
  extensions); inline CSS dropped; complex tables degrade to HTML-in-Markdown.
- **DOCX → Markdown:** comments and tracked changes lost unless `--track-changes` is
  used; embedded equations may convert to images; complex numbered lists may renumber.
- **Confluence storage format → Markdown:** macros become HTML comments or are dropped
  entirely; user mentions become plain text; attachments require separate extraction.
- **Markdown → DOCX/PDF round-trip:** frontmatter dropped or rendered as definition list;
  fenced code blocks lose syntax highlighting unless a Lua filter is applied.

**[SYNTHESIZED INFERENCE]** Fidelity ranking (best → worst preservation):
1. Markdown ↔ Markdown across dialects (via `commonmark_x`, `gfm`)
2. Markdown → HTML (effectively lossless when extensions align)
3. DOCX → Markdown (good for prose, poor for layout/styling)
4. HTML → Markdown (good for semantic HTML, poor for div-soup CMS exports)
5. PDF → anything via Pandoc (don't; use `pdftotext` or vision models)
6. Confluence Storage XML → Markdown (macro-heavy pages lose substantial content)

**Practical guidance:**
- **One-way migration:** Pandoc plus a custom Lua filter for project-specific macros
  achieves roughly 80–90% fidelity on prose-heavy content. Acceptable for one-time corpus
  migration.
- **Round-tripping:** do not use Pandoc for ongoing Markdown ↔ Confluence sync. Use a
  format-specific tool such as `mark` (https://github.com/kovetskiy/mark) that
  understands the target's macro vocabulary.

---

### Q8. Cross-cutting recommendations

Distilled from Q1–Q7:

**Corpus design**
- CommonMark + GFM as the base dialect.
- YAML frontmatter validated by JSON Schema in CI.
- ATX headings only; fenced code with language tags; inline links over reference links.
- One unified repo for ADRs (MADR 4.0), notes (Zettelkasten-style), and docs (Diátaxis
  quadrants), discriminated by frontmatter `type`.

**Agent instructions**
- `AGENTS.md` as canonical, vendor-neutral.
- `CLAUDE.md` as thin wrapper: `@AGENTS.md` import + Claude-specific overrides.
- Verify Claude Code's current AGENTS.md handling before relying on the import pattern.

**Diagrams**
- Mermaid source in Git, pre-rendered to SVG at build time.
- Publish SVG to Confluence via standard image macro (no Marketplace dependency).
- Do not bulk-convert legacy SVGs with vision LLMs; convert on-demand with human review.

**Confluence sync**
- One-way migration in: Pandoc with custom Lua filters.
- Ongoing publish: `mark` or direct Confluence REST API.
- Do not attempt Markdown ↔ Confluence round-trip.

**Quality gates**
- JSON-Schema frontmatter validation in pre-commit and CI.
- Prefer diff-format edits over whole-file rewrites in agent harness configuration.
- Reject PRs that mutate frontmatter outside schema.

---

## Gaps and Open Questions

Honest accounting of what this synthesis cannot establish from available inputs:

1. **Claude Code's current AGENTS.md support.** The single most time-sensitive
   operational question. Must be verified live against
   https://docs.claude.com/en/docs/claude-code/memory and current Anthropic release
   notes. If native support has shipped, the recommended `@AGENTS.md` import pattern may
   need revision to avoid double-loading.

2. **Exact benchmark figures.** Quantitative claims about Plot2Code, ChartMimic, and
   Aider polyglot scores were stated as ranges and should be re-derived from current
   paper tables and the live Aider leaderboard rather than cited as fixed numbers. Model
   versions, rubric revisions, and leaderboard updates change these figures regularly.

3. **A controlled study of Markdown-dialect edit reliability.** No known 2024–2025 paper
   isolates CommonMark vs. GFM vs. MyST vs. AsciiDoc as the independent variable in LLM
   edit success. All dialect-level claims here are informed inference from the Aider
   leaderboard plus practitioner heuristic, not measured fact.

4. **Confluence Marketplace app feature parity as of late 2025.** App capabilities
   (Mermaid version support, storage format, API access) change with vendor updates.
   Marketplace listings should be checked directly before standardizing on any specific
   app.

5. **MADR version currency.** MADR 4.0 release is confirmed; minor versions or a 4.x →
   5.x successor may have followed. Check https://github.com/adr/madr/releases.

6. **Information-architecture and records-management perspective.** This synthesis treats
   documentation tooling as a software-engineering problem and underweights
   library-science and technical-communication literature (records management, document
   provenance, rhetoric of technical communication). A complete treatment would include
   those fields, which would likely strengthen the case for frontmatter-as-metadata,
   type-discriminated corpora, and provenance tracking — but would require sources not
   consulted here.

7. **Anthropic Skills and Atlassian Remote MCP Server.** Both are relevant 2025
   developments but are not analyzed in the findings above because the underlying
   retrieval did not return content on them. Both are likely material to a complete
   picture of AI-assisted documentation workflows and warrant a dedicated follow-up
   retrieval pass.

8. **Frontmatter drift quantification.** Failure modes are listed qualitatively from
   practitioner observation. A measured study (frequency of each failure mode by model,
   by edit format, by frontmatter schema complexity) would meaningfully strengthen the
   operational recommendations but is not known to exist.

---

## Sources

All URLs below should be **fetched live** before relying on the corresponding claims,
because the underlying research session did not return retrievable content.

| Topic | URL | Status |
|---|---|---|
| AGENTS.md specification | https://agents.md/ | [VERIFIABLE FACT — needs live confirmation of current vendor list] |
| Claude Code memory documentation | https://docs.claude.com/en/docs/claude-code/memory | [CRITICAL — verify AGENTS.md handling] |
| Aider polyglot benchmark | https://aider.chat/docs/leaderboards/ | [VERIFIABLE FACT — figures change with leaderboard updates] |
| SWE-bench Verified | https://www.swebench.com/ | [VERIFIABLE FACT] |
| MADR 4.0 release | https://github.com/adr/madr/releases | [VERIFIABLE FACT — check for newer versions] |
| log4brains | https://github.com/thomvaill/log4brains | [VERIFIABLE FACT] |
| Mermaid Chart for Confluence | https://marketplace.atlassian.com/apps/1234214/mermaid-chart-for-confluence | [VERIFIABLE FACT — verify app ID and current features] |
| Stiltsoft Mermaid Diagrams | https://marketplace.atlassian.com/apps/1224957/mermaid-diagrams-for-confluence | [VERIFIABLE FACT — verify app ID] |
| Plot2Code paper | https://arxiv.org/abs/2405.07990 | [VERIFIABLE FACT — re-check exact scores] |
| ChartMimic paper | https://arxiv.org/abs/2406.09961 | [VERIFIABLE FACT — re-check exact scores] |
| DiagrammerGPT | https://arxiv.org/abs/2310.12128 | [VERIFIABLE FACT] |
| Diátaxis framework | https://diataxis.fr/ | [VERIFIABLE FACT] |
| Pandoc user guide | https://pandoc.org/MANUAL.html | [VERIFIABLE FACT] |
| Anthropic Skills announcement | https://www.anthropic.com/news/skills | [NOT ANALYZED in this synthesis] |
| Atlassian Remote MCP Server | https://www.atlassian.com/blog/announcements/remote-mcp-server | [NOT ANALYZED in this synthesis] |
| kovetskiy/mark (Markdown→Confluence) | https://github.com/kovetskiy/mark | [VERIFIABLE FACT] |
| remark-lint-frontmatter-schema | https://github.com/JulianCataldo/remark-lint-frontmatter-schema | [VERIFIABLE FACT] |

**Items flagged [NO SOURCE FOUND]:**
- A controlled empirical study comparing CommonMark vs. GFM vs. MyST vs. AsciiDoc LLM
  edit-success rates.
- A measured study quantifying frontmatter drift failure-mode frequencies by model and
  edit format.
- Information-architecture / records-management literature on AI-assisted documentation
  corpora (gap noted by critic; not retrieved).

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

[Original prompt not recorded in this doc]

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

(none yet — auto-populated at commit from the validated provenance ledger)

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

<!-- /doc:region name="run_history" -->
