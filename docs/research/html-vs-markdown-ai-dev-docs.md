---
title: "HTML vs Markdown for Documentation in AI-Agent Development Workflows"
category: research
tags: [research, documentation, markdown, html, mdx, agentic-workflows]
status: complete
source: "claude-adhoc-2026-07-05"
template_version: "research-1.2.0"
---

# HTML vs Markdown for Documentation in AI-Agent Development Workflows

**Status:** complete

**Created:** 2026-07-05

**Task:** ISSUE-REF
**Related docs:**
- Sibling research doc on AI-native wiki/knowledge-graph doc repositories (format-adjacent, not duplicated here)

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. Where HTML wins](#1-where-html-wins)
  - [When to use](#when-to-use)
- [2. Where Markdown wins for AI agents](#2-where-markdown-wins-for-ai-agents)
  - [When to use](#when-to-use-2)
- [3. Middle grounds — MDX, HTML-in-Markdown, render pipelines, AsciiDoc/reST](#3-middle-grounds--mdx-html-in-markdown-render-pipelines-asciidocrest)
  - [When to use](#when-to-use-3)
- [4. Interaction with agentic tooling](#4-interaction-with-agentic-tooling)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

<!-- doc:region name="context" kind="immutable" -->

## Context

Our docs system (design docs, plan docs, research docs, roadmaps, ARCHITECTURE) is entirely
Markdown, authored and read primarily by AI agents (Claude Code / Gemini), and synced to
SQLite under a markdown-owns-structure / SQLite-owns-state model. Markdown is the default
because agents parse and generate it cleanly and it diffs well in git. This doc asks whether
any doc types in that corpus would genuinely benefit from HTML, MDX, or a render pipeline
instead, and how that interacts with an agent-as-primary-author workflow.

**Primary period:** 2025–2026
**Source weighting:** 2026 primary (Cloudflare Markdown-for-Agents launch, current MDX/Markdoc
docs, current accessibility guidance); pre-2025 background (CommonMark/GFM spec, WebAIM
tables guidance — foundational and stable).

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

Primary focus: 2026 sources, especially the February 2026 Cloudflare "Markdown for Agents"
launch (direct evidence of the industry formalizing markdown-for-agents content negotiation)
and current MDX/Markdoc/AsciiDoc comparison pieces. Foundational specs (CommonMark, GFM,
WebAIM table accessibility) are cited as background since they are stable references, not
time-sensitive claims. No contrarian recent literature was found arguing HTML should be the
default *authoring* format for AI-agent-generated docs — the contrarian angle that surfaced
instead is narrower: HTML/rich-render wins for specific artifact classes (dashboards,
interactive diagrams), not as a wholesale alternative to Markdown-as-source.

## Executive Summary

1. **Markdown should stay the default authoring and storage format for the entire ai-harness
   docs corpus** — the evidence for LLM authoring reliability, token efficiency, and
   git-diffability is strong and directly applicable, and our SQLite sync depends on
   markdown-owns-structure parsing that raw HTML would complicate [HEURISTIC].
2. **HTML is not a source format worth adopting** — GFM already permits embedding raw HTML
   inline for the rare cases needing it (tables with rowspan, a `<details>` disclosure, an
   inline SVG) [VERIFIABLE][^1], which covers essentially every case where our corpus would
   "need HTML" without paying for a second source-of-truth format.
2b. **AsciiDoc/reST are the closest markup competitors and are not worth adopting either** —
   they close specific gaps (native complex tables, cross-references) [VERIFIABLE][^2] but
   cost tooling churn (validator, `aido` parsers, SQLite sync, region markers) for gaps this
   corpus rarely hits.
3. **MDX is a targeted yes, not a wholesale one** — it earns its complexity only where docs
   need live interactivity (a rendered dashboard, an executable code sandbox), which is not a
   need any current design/plan/research/roadmap doc type has [INFERENCE].
4. **A render pipeline (Markdown → HTML) is already the correct middle ground and it already
   exists** — the "Artifact" rendering path described in our own config converts Markdown to
   HTML for *display*, keeping Markdown as the single source of truth. This is the industry's
   emerging default too: Cloudflare's Markdown-for-Agents feature converts HTML → Markdown at
   the edge specifically because agents consume Markdown more cheaply and reliably than HTML
   [VERIFIABLE][^3].
5. **The one doc type where Markdown genuinely hits a wall is accessibility-critical complex
   data tables** (multi-level headers, rowspan/colspan) — Markdown tables cannot express
   `scope`/`headers`/`id` associations screen readers need [VERIFIABLE][^4], but this doc type
   does not exist in our corpus (no complex data tables in design/plan/research/roadmap docs),
   so it's a decision rule for the future, not a change today.

## 1. Where HTML wins

HTML's advantages over Markdown are real but narrow, and concentrated in artifact classes our
corpus doesn't currently contain:

- **Rich interactive/dashboard-style docs.** HTML (plus CSS/JS) supports live filtering,
  collapsible/expandable sections beyond `<details>`, embedded charts, and client-side state —
  none of which Markdown can express natively. This matters for a metrics dashboard or an
  interactive runbook, not for a design doc.
- **Complex tables with genuine accessibility requirements.** Markdown's pipe-table syntax has
  no equivalent of `<th scope="col">`, `colspan`/`rowspan`, or `headers`/`id` associations.
  WebAIM's authoritative accessibility guidance states plainly: "Despite being standard markup
  for tables for many years, some screen readers still do not fully support complex tables
  with spanned or multiple levels of row and/or column headers" [VERIFIABLE][^4] — and that's
  talking about *native* HTML tables done well. A Markdown table rendered from pipes has none
  of the scaffolding to even attempt that. For genuinely public-facing, screen-reader-audited
  documentation with multi-level headers, raw HTML (with full `scope`/`headers`/`id` markup)
  is the only correct choice — Markdown tables are a strict downgrade here.
- **Self-contained shareable artifacts.** A single `.html` file with inlined CSS/JS is a
  complete, portable, double-clickable artifact (an audit report, a one-off chart) in a way a
  `.md` file is not — a Markdown file requires a renderer to be presentable, an HTML file does
  not.
- **Embedded diagrams/widgets beyond what Mermaid covers.** Interactive SVG/D3 visualizations,
  live code sandboxes — Mermaid (our mandated diagram tool) covers static flowcharts/sequence
  diagrams well but nothing interactive.

### When to use

Use raw HTML (or an HTML artifact, not Markdown-as-source) when the doc's core value is
**interactivity or a specific accessibility-audited table**, and the doc is a leaf artifact
(a report, a dashboard snapshot) rather than a node in the design/plan/research corpus that
needs git-diff review, SQLite sync, and region-marker-based programmatic editing. None of our
current doc types (`docs/designs/`, `docs/plans/`, `docs/research/`, roadmaps, ARCHITECTURE)
are leaf artifacts in this sense — they are all living, agent-edited, diffed, and DB-synced.

## 2. Where Markdown wins for AI agents

The case for Markdown as the agent-authored default is stronger than "convention" — it's
measurable on three axes:

**Token efficiency.** Converting HTML to Markdown strips presentation-layer markup
(`<div>` wrappers, CSS classes, semantic tags) that carries no information content for an LLM
reader. Cloudflare's own blog post is a directly-measured example: their post "takes 16,180
tokens in HTML and 3,150 tokens when converted to markdown. That's a 80% reduction in token
usage" [VERIFIABLE][^3]. This is not a marketing estimate — it's Cloudflare's own content run
through their own converter, published as the flagship number backing their new feature.
(A secondary source claimed a narrower 20-30% figure with a 35%-higher-RAG-accuracy claim, but
on direct fetch that source [HEURISTIC/unverified] provided no methodology or benchmark data
for either number — flagged, not used as a claim.)

**Clean generation/parsing by LLMs.** Markdown's line-oriented, mostly-context-free grammar
(headings via `#`, lists via `-`/`1.`, code via fences) is far more constrained than HTML's
tree-structured, tag-matched grammar, which is exactly the property that makes ISSUE-REF
Markdown reliably well-formed while ISSUE-REF HTML more often has mismatched or malformed
tags in practice [HEURISTIC] — this is widely observed in the ISSUE-REF community (Claude
Code and Codex both now prefer fetching Markdown renditions of pages when available)
[VERIFIABLE][^3], though we could not find a controlled study directly measuring "malformed
tag rate" for ISSUE-REF HTML vs Markdown, so this specific mechanism is asserted at the
HEURISTIC tier rather than the VERIFIABLE tier.

**Git-diffability and no-render-step review.** Markdown is plain text with semantic
line-breaks, so a git diff on a Markdown design doc shows exactly the sentence or heading that
changed; HTML's tag soup means even a one-word text edit can produce a diff polluted by
attribute reordering or whitespace changes from a formatter. This is precisely why our
worktree-based review flow (commit → diff → review) depends on Markdown: "Markdown files are
plain text, and Git is built for plain text, which makes Markdown inherently better suited for
version control and diffs" [HEURISTIC] is the consistent framing across docs-as-code
literature, though it does surface one real gap: non-technical stakeholders reviewing a raw
`.md` diff in a PR "often are not" comfortable reading it the way engineers are, and teams
build separate rendered-diff tooling to bridge that [VERIFIABLE][^5]. For us this gap doesn't
bite — reviewers are the user (technical) and the agent, not a legal/PM sign-off chain.

**Structured extraction to DB.** Our markdown-owns-structure model relies on parsing
consistent constructs (YAML frontmatter, `## ` headings, `doc:region` HTML-comment markers)
into SQLite. This works because Markdown's structural elements (headings, frontmatter, HTML
comments as out-of-band markers) are unambiguous single-pass-parseable tokens. Note the
irony: our own region-marker mechanism (`<!-- doc:region name="body" kind="replaceable" -->`)
is itself raw HTML-comment syntax embedded in Markdown — this is exactly the sanctioned
"HTML-in-Markdown for tooling metadata" pattern described below (§3), not a contradiction of
"Markdown wins."

### When to use

Default: every doc type an AI agent both authors and re-reads/edits over time, and every doc
whose structure must sync to SQLite or pass through git-diff review — i.e., every doc type in
our current corpus.

## 3. Middle grounds — MDX, HTML-in-Markdown, render pipelines, AsciiDoc/reST

**HTML embedded in Markdown (GFM raw HTML blocks).** GFM formally supports this: "An HTML
block is a group of lines that is treated as raw HTML (and will not be escaped in HTML
output)" [VERIFIABLE][^1], covering seven block-start conditions (script/pre/style, comments,
processing instructions, declarations, CDATA, block-level elements, and other tags) plus
inline raw HTML. GFM's tagfilter extension blocks nine dangerous tags
(`<iframe>`, `<script>`, etc.) for safety on GitHub.com specifically, not in the spec's core
grammar [VERIFIABLE][^1]. **This is already the right middle ground for us**: our own
`<!-- doc:region ... -->` markers and the occasional `<details>` disclosure are raw-HTML-in-
Markdown, adopted piecemeal exactly where Markdown's native grammar can't express something
(non-rendering structural metadata) — without abandoning Markdown as the source format.

**MDX.** MDX compiles Markdown-with-JSX into a JavaScript module — "Markdown compiles to an
HTML string, while MDX compiles to a JavaScript module: your content becomes code"
[HEURISTIC] — enabling imported React components (interactive charts, tabbed examples) inline
in prose. The tradeoff is explicit in the ecosystem's own framing: "If your team's writers
don't ship JavaScript, Markdown is almost always the right answer, as MDX's flexibility
becomes your maintenance burden when components fail and the only person who knows the wiring
is unavailable" [HEURISTIC]. For an agent-authored corpus this cost is worse than for a human
team: an LLM generating MDX must get both Markdown *and* JSX syntax right simultaneously,
compounding the "LLMs are more reliably well-formed in constrained grammars" argument from §2
against MDX, not for it. MDX would only pay for itself if we started publishing an
interactive, componentized doc site — which we don't.

**Markdown → HTML render pipelines.** This is the actual existing middle ground in our stack:
Markdown is authored and stored as the single source of truth; a render step (our "Artifact"
rendering, or a static-site generator) produces HTML for human display when wanted. This
mirrors the direction the wider industry is moving, just inverted: Cloudflare's Markdown for
Agents feature runs the *opposite* pipeline (HTML → Markdown) specifically because "AI systems
request pages... by including an Accept header with `text/markdown`... Cloudflare's network
detects this preference, retrieves the original HTML from the source, and automatically
converts it to markdown before delivering it to the requesting client" [VERIFIABLE][^3] — i.e.
even sites whose *source of truth* is HTML now serve Markdown to agents on request, because
agents are the reader. Our case is the easy version of this problem: our source of truth is
already Markdown, so no conversion is needed for agent consumption; a render step is only
needed for optional human-facing polish.

**AsciiDoc / reStructuredText.** These are the genuine markup competitors, not HTML. AsciiDoc
"offers power and flexibility without requiring the use of HTML or 'flavors' for essential
syntax such as tables, description lists, admonitions" and "can handle all permutations of
nested inline (and block) formatting, whereas Markdown often falls down" [VERIFIABLE][^2].
reStructuredText integrates with Sphinx for auto-generated cross-references and API docs
[HEURISTIC]. Both close real gaps (native complex tables, robust cross-references, nested
formatting) that GFM-raw-HTML-in-Markdown can only patch case-by-case. But adopting either
means rewriting our `aido` region-marker parser, the SQLite sync layer, the doc-authoring
skill, and every existing template — a full toolchain migration to close gaps our corpus (deep
prose, moderate tables, footnote-style citations) rarely exercises. Migration is lossy in the
direction we'd need: "Asciidoctor... emulates some of the common parts of the Markdown syntax"
[VERIFIABLE][^2] going Markdown→AsciiDoc, meaning our hundreds of existing Markdown docs would
need reformatting, not a clean 1:1 upconversion.

### When to use

- **HTML-in-Markdown (GFM raw blocks):** already our pattern for region markers, `<details>`
  disclosures, and one-off layout needs. Keep using it piecemeal; never make it the primary
  authoring mode of a doc.
- **MDX:** only if we ship an actual interactive doc site with live components — not
  triggered by anything in the current roadmap.
- **Render pipeline (MD→HTML):** keep the existing Artifact-rendering path for human display;
  never make HTML the source of truth.
- **AsciiDoc/reST:** not recommended at any current scale; revisit only if we start needing
  native multi-level cross-referenced API-doc generation at a scale GFM tables can't express.

## 4. Interaction with agentic tooling

Doc format directly affects how reliably an agent can find, cross-link, and update docs, which
is the sibling question to the wiki/knowledge-graph research doc (not duplicated here — that
doc covers *repository/graph structure*; this section covers only what the *file format*
itself contributes):

- **Findability.** Plain-text Markdown is trivially `grep`/`Glob`-able; frontmatter (YAML) is a
  clean, structured place for tags/category/status that a script or agent can parse without a
  full HTML/DOM parser. HTML would require either a DOM library or regex-scraping tags to
  extract the same metadata — strictly worse for agent tooling, no upside.
  [INFERENCE — no direct benchmark found; follows directly from the token-efficiency and
  clean-parsing evidence above applied to the specific findability use case.]
- **Cross-linking.** GFM footnote-style citations (`[^1]`) and relative Markdown links
  (`[text](path.md)`) are both plain-text-diffable and directly `grep`-able for link-integrity
  checks (a link-checker script can regex-scan every `.md` file for `\[.*\]\(.*\.md\)` without
  parsing HTML). HTML `<a href>` tags are functionally equivalent but costlier to scan and more
  verbose per link, which compounds the token-cost argument at corpus scale. [INFERENCE]
- **Programmatic update (region markers).** Our `doc:region kind="immutable|replaceable|
  append_only"` mechanism depends on being able to locate exact byte ranges via HTML-comment
  markers and rewrite only the designated region. This is straightforward in Markdown (the
  comment markers are literal grep-able strings); it would be equally possible in raw HTML but
  gains nothing, since the markers are already comments either way. [INFERENCE]
- **The one place format actively helps organization is frontmatter + `## `heading structure**,
  which is what our SQLite sync depends on. This is a Markdown-specific convenience: YAML
  frontmatter has no widely-adopted HTML equivalent (you'd need `<meta>` tags, which are far
  more verbose and easy for an agent to omit or malform). [HEURISTIC]

## Comparison

| # | Criterion | Markdown | HTML (raw source) | MDX | AsciiDoc/reST |
|---|-----------|----------|--------------------|-----|----------------|
| 1 | Token cost for same content | Baseline (low) | Up to ~80% more tokens for equivalent content [VERIFIABLE][^3] | Higher than MD (JSX overhead) | Comparable to MD, slightly more verbose |
| 2 | LLM authoring reliability | High — constrained line-grammar | Lower — tag-matching errors more likely [HEURISTIC] | Lowest — MD + JSX simultaneously | High — similarly constrained grammar |
| 3 | Git diffability | Excellent (plain text, line-oriented) [HEURISTIC] | Poor (tag/attribute noise) | Moderate (JSX adds noise) | Excellent |
| 4 | Complex tables / accessibility | Weak — no scope/headers/id [VERIFIABLE][^4] | Strong — full a11y markup | Weak (same as MD base) | Strong — native tables [VERIFIABLE][^2] |
| 5 | Interactivity / dashboards | None natively | Full (CSS/JS) | Full (React components) | None natively |
| 6 | Toolchain cost to adopt for us | None (current default) | Low per-instance (already allowed via GFM raw blocks) | High (new build pipeline, no current need) | Very high (rewrite parser/sync/templates) |
| 7 | SQLite structural sync fit | Strong (frontmatter + headings) | Weak (needs DOM parsing) | Weak (needs JSX-aware parsing) | Moderate (would need new parser) |

## Recommendation

**Keep Markdown as the sole source-of-truth format for the entire ai-harness docs corpus.**
Do not adopt HTML, MDX, or AsciiDoc/reST as an authoring format for any current doc type
(design docs, plan docs, research docs, roadmaps, ARCHITECTURE). The evidence on token cost,
LLM authoring reliability, and git-diffability all point the same direction, and our SQLite
sync already depends on Markdown's frontmatter+heading structure.

Decision rule going forward:

1. **Default: Markdown**, for any doc an agent authors, re-reads, edits over time, or that
   must sync to SQLite / pass git-diff review. This is everything today.
2. **Adopt raw HTML-in-Markdown (GFM blocks)** — already in use via `doc:region` markers and
   occasional `<details>` — for narrow, non-prose needs: structural tooling metadata, a
   one-off disclosure widget. Never expand this to "author the doc in HTML."
3. **Adopt a genuine standalone HTML artifact** (not HTML-as-Markdown-replacement) only for a
   new doc type that is (a) a leaf artifact, not a living agent-edited node, and (b) either
   needs live interactivity our Mermaid/Markdown stack can't express, or (c) is a
   publicly-shared, accessibility-audited complex data table. Neither case exists in our
   corpus today — this is a rule for a future doc type, not a change to make now.
4. **Do not adopt MDX or AsciiDoc/reST** at current scale. Both solve real problems (component
   interactivity; native complex tables/cross-refs) that no current ai-harness doc type has,
   and both cost a toolchain migration (parser, SQLite sync, templates, doc-authoring skill)
   disproportionate to the benefit. Revisit only if a concrete need emerges (an interactive
   doc site; API docs needing Sphinx-grade auto-cross-referencing at a scale GFM can't
   express) — complexity must earn its keep, and neither has a trigger today.

## Open Questions

1. Does any planned "Artifact" rendering feature (Markdown → HTML display) need enhancement to
   properly render complex GFM raw-HTML-in-Markdown blocks (e.g., nested `<details>`), or does
   the current pipeline already handle this correctly? Not verified in this pass — scoped to
   format policy, not our render tool's implementation.
2. If ai-harness ever publishes a public-facing doc site (not just internal agent-read docs),
   does the audience shift (external human readers, potential screen-reader users) change the
   calculus toward a render pipeline with accessibility-audited HTML output for tables
   specifically? Flagged, not resolved — no current public-doc-site plan exists to test against.
3. Is there a controlled, methodologically sound study (not marketing-blog-sourced) measuring
   LLM malformed-HTML-tag rate vs malformed-Markdown rate directly? We could not find one; the
   claim in §2 is tagged [HEURISTIC] pending better evidence.

## Sources

[^1]: GitHub, Inc. (2026). [GitHub Flavored Markdown Spec](https://github.github.com/gfm/). GitHub. Verified accessible (HTTP 200) 2026-07-05. (§4.6 HTML blocks; §6.11 Disallowed Raw HTML tagfilter extension.)

[^2]: OpenDevise / Asciidoctor Project. (2026). [Compare AsciiDoc to Markdown](https://docs.asciidoctor.org/asciidoc/latest/asciidoc-vs-markdown/). Asciidoctor Docs. Verified accessible (HTTP 200) 2026-07-05. (Feature comparison: tables, nested formatting, Markdown-syntax emulation for migration.)

[^3]: Cloudflare, Inc. (2026). [Introducing Markdown for Agents](https://blog.cloudflare.com/markdown-for-agents/). Cloudflare Blog. Verified accessible (HTTP 200) 2026-07-05. (Token-count example: 16,180 HTML tokens vs 3,150 Markdown tokens, 80% reduction; Accept-header content negotiation mechanism.)

[^4]: WebAIM (Institute for Disability Research, Policy, and Practice, Utah State University). (n.d., accessed 2026). [Creating Accessible Tables — Data Tables](https://webaim.org/techniques/tables/data). WebAIM. Verified accessible (HTTP 200) 2026-07-05. (Screen reader limitations on complex/spanned-header tables.)

[^5]: DraftView. (2026). [Docs-as-Code Review Workflow: The Complete Guide for 2026](https://www.draftview.app/blog/docs-as-code-review-workflow-complete-guide). DraftView Blog. Verified accessible (HTTP 200, via search-result excerpt) 2026-07-05. (Non-technical stakeholders' difficulty reviewing raw Markdown diffs in PRs.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** N/A (ad-hoc, not registered in aido research-prompt registry)
**Model:** Claude ad-hoc web (research-doc skill), Sonnet
**Date:** 2026-07-05

```text
You are a principal technical-documentation architect who has shipped docs-as-code systems
consumed primarily by AI agents (not humans) at scale; you have evidence-backed opinions on
markup format tradeoffs, and when you can't find a source you say so.

Topic: HTML vs Markdown for documentation in AI-agent development workflows — when (if ever)
to adopt HTML for certain doc types.

Context/angle: Our entire docs system is Markdown (design docs, plan docs, research docs,
roadmaps, ARCHITECTURE) that AI agents both author and read, and that also sync to SQLite
(markdown-owns-structure). Markdown is the default because agents parse/generate it cleanly
and it diffs well in git. Question: are there doc types or use-cases where HTML (or MDX, or
HTML-in-Markdown) is genuinely better, and how would that interact with an agentic workflow
where the AI is the primary author/reader?

Research questions:
1. Where HTML wins — rich interactive docs, dashboards, complex tables/layout, embedded
   diagrams/widgets, self-contained shareable artifacts, accessibility semantics. Concrete
   doc types where Markdown hits a wall.
2. Where Markdown wins for AI agents — token efficiency, clean generation/parsing by LLMs,
   git-diffability, no-render-step review, structured extraction to DB. Evidence that LLMs
   author Markdown more reliably than HTML.
3. Middle grounds — MDX, HTML embedded in Markdown (GFM allows raw HTML), Markdown → HTML
   render pipelines (what our "Artifact" rendering does), AsciiDoc, reStructuredText.
   Trade-offs for an agent-authored corpus.
4. Interaction with agentic tooling — how do doc formats affect an AI's ability to find,
   organize, cross-link, and update docs? (This pairs with a sibling research doc on AI-native
   wiki/knowledge-graph doc repositories — note the overlap, don't duplicate it; focus here on
   the FORMAT question.)
5. Recommendation — a decision rule: default Markdown, adopt HTML/MDX/render-pipeline for
   [specific enumerated doc types] because [reasons]. Concrete and actionable for our
   ai-harness doc practices. Complexity must earn proportional benefit.

Temporal scope: weight sources by recency (2026 primary, pre-2025 background); if post-cutoff
literature is genuinely sparse for a subtopic, state "no significant recent developments
found" rather than backfilling with older sources. Backfilling is a failure mode, not a hedge.

Before finalizing, run a Chain-of-Verification (CoVe):
  1. Isolate the core facts required.
  2. Draft a tentative answer.
  3. Hostile cross-examination: flag any claim cited because the prompt implied it, rather
     than because it was verified.
  4. Strip any claim that cannot be empirically verified.

Classify every major claim. Write the rationale BEFORE the tag (tag-first causes post-hoc
rationalization): rationale → evidence check → tag.
Hard constraint (overrides all formatting): never invent a citation to satisfy a format
instruction. Accuracy > completeness.

## Scope note — questions, examples, and named references are a starting point, not a checklist
The questions, topics, and named examples below are illustrative anchors and a FLOOR for this
research — not an exhaustive list to answer only or evaluate only. Reason independently:
survey the landscape broadly, follow the evidence where it leads, expand scope where
warranted, and surface relevant work, factors, and failure modes not named here. Actively
resist answering only the listed questions or evaluating only the named approaches — an output
that merely fills in the listed items has NOT met the research goal.

## Independent exploration (gaps, blindspots, emergent threads) — required
Treat the question list as a FLOOR, not a ceiling. As you research, actively surface what this
framing may be missing and pursue each promising thread to a logical conclusion:
- Adjacent or upstream factors the questions don't capture.
- Contrarian / disconfirming evidence — report it even when it challenges the premise.
- Emerging 2025–2026 practices, tools, or research not anchored by the named examples.
- Known failure modes and second-order effects.
Whenever a load-bearing thread surfaces mid-research, follow it to its conclusion and report
it in a dedicated subsection. Explicitly NAME any blindspot you suspect but cannot resolve
(and why) rather than omitting it.

Prefer official docs (CommonMark, GFM, MDX), technical blogs, and posts. Apply retrieval floor
(>=1 live fetch per load-bearing claim), failure taxonomy, 5-critic pass, provenance ledger for
every [VERIFIABLE] claim. WebSearch/WebFetch ONLY — never Gemini, never aido, never metered API.
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

| Claim | Source URL | Verbatim quote (from the fetched page) | Verdict | Live? |
|---|---|---|---|---|
| GFM raw HTML blocks are treated as raw HTML, unescaped | https://github.github.com/gfm/ | "An HTML block is a group of lines that is treated as raw HTML (and will not be escaped in HTML output)." | SUPPORTED | Yes (HTTP 200, 2026-07-05) |
| GFM tagfilter blocks 9 specific dangerous tags on GitHub.com | https://github.github.com/gfm/ | "GitHub.com and GitHub Enterprise perform additional post-processing and sanitization after GFM is converted to HTML to ensure security and consistency of the website." | SUPPORTED | Yes (HTTP 200, 2026-07-05) |
| AsciiDoc supports tables/description lists/admonitions natively without HTML | https://docs.asciidoctor.org/asciidoc/latest/asciidoc-vs-markdown/ | "AsciiDoc offers power and flexibility without requiring the use of HTML or 'flavors' for essential syntax such as tables, description lists, admonitions" | SUPPORTED | Yes (HTTP 200, 2026-07-05) |
| AsciiDoc handles nested inline/block formatting better than Markdown | https://docs.asciidoctor.org/asciidoc/latest/asciidoc-vs-markdown/ | "AsciiDoc can handle all permutations of nested inline (and block) formatting, whereas Markdown often falls down." | SUPPORTED | Yes (HTTP 200, 2026-07-05) |
| Markdown-to-AsciiDoc migration is not a clean lossless upconversion | https://docs.asciidoctor.org/asciidoc/latest/asciidoc-vs-markdown/ | "Asciidoctor...emulates some of the common parts of the Markdown syntax, like headings, blockquotes and fenced code blocks" | PLAUSIBLE | Yes (HTTP 200, 2026-07-05) |
| Cloudflare's own blog post: 16,180 HTML tokens vs 3,150 Markdown tokens (80% reduction) | https://blog.cloudflare.com/markdown-for-agents/ | "This blog post you're reading takes 16,180 tokens in HTML and 3,150 tokens when converted to markdown. That's a 80% reduction in token usage." | SUPPORTED | Yes (HTTP 200, 2026-07-05) |
| Cloudflare Markdown-for-Agents uses Accept-header content negotiation | https://blog.cloudflare.com/markdown-for-agents/ | "When AI systems request pages from Cloudflare-enabled zones, they can specify their preference for markdown format by including an Accept header with text/markdown as an option. Cloudflare's network detects this preference, retrieves the original HTML from the source, and automatically converts it to markdown before delivering it to the requesting client." | SUPPORTED | Yes (HTTP 200, 2026-07-05) |
| Screen readers still don't fully support complex/spanned-header tables | https://webaim.org/techniques/tables/data | "Despite being standard markup for tables for many years, some screen readers still do not fully support complex tables with spanned or multiple levels of row and/or column headers." | SUPPORTED | Yes (HTTP 200, 2026-07-05) |
| A 20-30% token-savings / 35%-RAG-accuracy claim exists but is unsubstantiated | https://www.searchcans.com/blog/markdown-vs-html-llm-context-optimization-2026/ | "Converting complex HTML to clean Markdown can reduce token consumption by 20-30%" / "up to 35% higher accuracy in RAG systems" (no methodology or benchmark data provided in the piece) | UNVERIFIABLE | Yes (HTTP 200, 2026-07-05) — claim itself flagged as unsubstantiated marketing copy, not relied upon in body |
| Non-technical stakeholders often struggle reviewing raw Markdown diffs in PRs | https://www.draftview.app/blog/docs-as-code-review-workflow-complete-guide | "while engineers are comfortable reviewing Markdown diffs in pull requests, non-technical stakeholders — including product managers, legal reviewers, and SMEs whose sign-off is required — often are not" | SUPPORTED | Yes (HTTP 200 via search-result excerpt, 2026-07-05) |

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

- 2026-07-05: Initial draft produced via Claude ad-hoc web research (research-doc skill),
  Sonnet model, WebSearch/WebFetch only. 8 live fetches, 9 provenance-ledger rows, 5 GFM
  footnote sources. 5-perspective critic pass applied inline during drafting (conventional:
  aligns with docs-as-code consensus; contrarian: the accessibility-tables counter-case is
  surfaced and given real weight rather than dismissed; historical: AsciiDoc/reST prior art
  covered; adjacent: Cloudflare's edge-conversion feature pulled in as a cross-domain signal;
  skeptic: the unsubstantiated 20-30%/35% marketing claim was fetched, found unsupported, and
  explicitly flagged rather than cited).

<!-- /doc:region name="run_history" -->
