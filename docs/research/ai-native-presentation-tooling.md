---
title: AI-Native Presentation & Slide-Deck Tooling for Agentic Coding Tools
category: research
tags: [research, presentations, slides, claude-code, codex, agentic-tooling]
status: complete
source: "codex-research-2026-09-01"
template_version: "research-1.2.0"
delegation_provenance:
  version: 2
  contributors: []
---
<!-- Canonical Jinja source: STUB.md.jinja. This direct-copy stub is retained for consumers that have not migrated to rendering it. -->

# AI-Native Presentation & Slide-Deck Tooling for Agentic Coding Tools

## Founding Ask Coverage

`founding_ask_ref`: N/A — no prior canonical FAR exists for this topic; ask originates fresh in this chat session (example-user, 2026-09-01/02).

The 2026-10-21 refine (§8) answers the founding ask recorded at
[📄 ai-harness/docs/asks/ISSUE-REF.md](../asks/ISSUE-REF.md)
(`founding_ask_ref_sha256` 6fc4a995523f42eb2e9ed7d75ea8188664d5290053dd4addf37702afc67e394a):
"we need to review the visualization / graphics / frontend etc harness elements we added to
ai-harness repo and refine the presentaiton skill and related harness files". Kept: §8 is that
review.

**Task:** ISSUE-REF

**Status:** complete

**Created:** 2026-09-02

<!-- doc:region name="context" kind="immutable" -->

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. Decision Frame: Separate Source, Renderer, and Delivery Format](#1-decision-frame-separate-source-renderer-and-delivery-format)
- [2. Agent-Native Leaders](#2-agent-native-leaders)
- [3. Code-Native Slide Pipelines](#3-code-native-slide-pipelines)
- [4. Non-Slide Presentation Formats](#4-non-slide-presentation-formats)
- [5. Quality Harness: How to Avoid “AI Slop”](#5-quality-harness-how-to-avoid-ai-slop)
- [6. Landscape Triage and Current-State Corrections](#6-landscape-triage-and-current-state-corrections)
- [7. Interactive React/HTML-Native Decks (2026-09-04 Refine)](#7-interactive-reacthtml-native-decks-2026-09-04-refine)
- [8. Harness Reuse and Live-Deck Testing (2026-10-21 Refine)](#8-harness-reuse-and-live-deck-testing-2026-10-21-refine)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Ambiguous Items from Auto-Remediation (Post-Run Review)](#ambiguous-items-from-auto-remediation-post-run-review)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

## Context

example-user is presenting on ai-harness engineering to his team Friday ~noon EST (2026-09-04), with
an outline/initial draft needed by ~1pm EST the day before (2026-09-03). He asked for a
breadth-and-depth survey of AI-native / AI-best presentation and slide-deck generation tools,
techniques, packages, plugins, and harnesses -- specifically ones agentic coding tools (Claude
Code, Codex) are best positioned to use or are actively developing -- covering both slide-based
and non-slide presentation formats, anchored on the latest 2026 (especially September 2026)
sources. This research directly feeds two downstream deliverables: (1) the actual Friday
presentation, and (2) a reusable presentation-authoring skill/harness (skills, context files,
other `/harness-update` changes) for future use.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

Research was performed against live sources on 2026-09-01 (America/New_York), weighting 2026
product documentation and research first, then 2025, then foundational documentation. The frozen
Context and scaffold are dated 2026-09-02 and interpret “tomorrow” as 2026-09-03; the runtime date
makes “tomorrow” 2026-09-02. The hard delivery date, Friday 2026-09-04 at about noon ET, is
unambiguous, so this report treats 2026-09-03 13:00 ET as the latest draft checkpoint while
recommending an internal first draft on 2026-09-02. [INFERENCE]

A refine pass was performed against live sources on 2026-09-04 to cover interactive React/HTML
frameworks omitted from the first survey. No announcement published after that retrieval date
could be evaluated. Marketing claims were used only for documented capability, not as proof of
visual quality. No credible, independent 2025–2026 head-to-head benchmark covering Gamma, Plus
AI, Beautiful.ai, Canva, Presentations.AI, Decktopus, SlidesAI, Slidebean, Bolt Slides,
Spectacle, Reveal.js React, and Slidev was located. [NO SOURCE]

The refine prompt named
`docs/presentation/decks/ai-harness-engineering/examples/AI_Harness_Engineering.html` as a
committed visual reference. That path was absent in the 2026-09-04 working tree; only generated
Slidev run artifacts were available. The claimed reference-deck comparison therefore could not
be independently inspected in this run. [NO SOURCE]

## Executive Summary

1. **Keep Slidev for the next production build; add React only as a guarded experiment.**
   Slidev accepts custom interactive Vue components and exports PDF/PPTX/PNG through Playwright,
   so a renderer switch is not required to add stateful widgets or richer compositions.
   [VERIFIABLE][^2][^37] The observed quality gap is more plausibly a component-library,
   authoring-discipline, and pixel-review gap than a Vue-versus-React gap. [INFERENCE] Bolt Slides
   is the most relevant React experiment, but its default remote fonts, shallow history, missing
   first-party export workflow, and concealed phrase-trigger in its bundled agent skill prevent
   direct adoption. [VERIFIABLE][^21][^23][^24][^25][^26][^27]
2. **Keep native PowerPoint as a deliberate branch, not the default.** PptxGenJS produces
   standards-compatible OOXML and supports programmatic slide masters, while Plus AI’s 2026 MCP
   surface can create and iteratively revise native PowerPoint. [VERIFIABLE][^4][^5][^8] Choose
   PptxGenJS when the harness must own every byte; choose Plus AI when editable PPTX is mandatory
   and a paid remote service is acceptable. [INFERENCE]
3. **Gamma is the fastest credible vendor-assisted alternative, but not the best reusable
   harness substrate.** Its GA API generates presentations, documents, websites, and social posts
   with themes and exports, but the current API creates new artifacts rather than editing an
   existing one. [VERIFIABLE][^6][^7] Use it as a one-hour visual benchmark or emergency
   fallback, not as the canonical source of truth. [INFERENCE]
4. **Quality comes from a constrained production loop, not a longer prompt.** The 2026 research
   direction separates narrative planning, page design, implementation, rendering, and
   evaluation; one study also found coding assistants strongest in its narrow educational test,
   but only after instructor modification and style matching. [VERIFIABLE][^18][^19][^20] The
   practical harness should therefore render every slide, run deterministic checks, perform
   screenshot critique, and require human approval of story, visuals, and final export.
   [INFERENCE]
5. **Non-slide formats are useful companions, not substitutes for this deadline.** A scrolling
   data story or live demo can show the harness more honestly than screenshots, while a terminal
   deck is excellent for a developer meetup; neither beats a conventional deck plus fallback for
   a mixed engineering room on a fixed Friday deadline. [HEURISTIC]

## 1. Decision Frame: Separate Source, Renderer, and Delivery Format

The key architectural choice is not “which AI makes the prettiest first draft?” It is whether the
artifact has a deterministic, diffable source that an agent can edit; a renderer the harness can
invoke without GUI automation; and a delivery format the room can reliably open. [HEURISTIC]

A useful stack has four contracts:

1. **Content contract:** an outline plus per-slide intent, evidence, speaker notes, and asset
   references.
2. **Design contract:** a small token set (type scale, spacing, palette, grid) and a bounded
   layout vocabulary.
3. **Render contract:** one command that produces HTML and a portable fallback.
4. **QA contract:** machine checks plus rendered-slide review, followed by a human rehearsal.

This decomposition limits the agent’s freedom where errors are expensive while preserving
creativity inside approved layouts. It also makes vendor generators replaceable. [INFERENCE]

### When to use

Use this frame for every reusable presentation skill. Skip only when the deliverable is a disposable
one-off and no downstream editing, auditability, accessibility, or branding is required.

## 2. Agent-Native Leaders

### Slidev: best overall fit

Slidev’s current official documentation directly addresses AI coding agents with both an MCP
server and an installable skill. [VERIFIABLE][^1] Its source is Markdown augmented with Vue/UnoCSS, and its export path covers PDF,
PPTX, PNG, and Markdown; interactive features remain available in the hosted web build but may not
survive static export. [VERIFIABLE][^2]

That combination matters: Claude Code or Codex can edit ordinary files, run the renderer, inspect
screenshots, and keep the whole presentation in Git. The tradeoff is that unconstrained Vue/CSS
invites layout drift, and Slidev’s PPTX export is an image-oriented portability path rather than a
promise of fully editable native objects. [INFERENCE]

### Plus AI: strongest managed native-PPTX agent surface

Plus AI documents an MCP server with create, iterate, and run-status tools; it accepts an existing
deck/file ID across turns and returns native PPTX, PDF, and a thumbnail. [VERIFIABLE][^8] This is
materially more agentic than a GUI copilot and directly answers the “agent’s agent” use case.

Its weaknesses are equally important: it is a remote, metered service; its own marketing and
testimonials are not an independent quality benchmark; and the harness would depend on an external
API, OAuth, plan entitlements, and service behavior. [INFERENCE]

### Gamma: strongest fast draft and web-native vendor alternative

Gamma’s API can generate a presentation from text, apply a workspace theme, choose dimensions,
and return a Gamma URL plus PDF/PPTX/PNG export. [VERIFIABLE][^6] Gamma also exposes an MCP server
and assistant connectors, but its REST scope currently generates new artifacts and cannot modify an
existing Gamma. [VERIFIABLE][^7] This makes it excellent for rapid first drafts and visual
comparisons, weaker for a version-controlled edit-review loop. [INFERENCE]

### Beautiful.ai, Canva, and Claude’s built-in PowerPoint skill

Beautiful.ai added an early-access prompt-to-deck API in 2026, producing editable presentations
that can be exported to PPTX or PDF. [VERIFIABLE][^9] Canva AI 2.0 advertises layered, editable,
brand-aware output and agentic editing, but it was introduced as a research preview, which is too
much availability risk for the critical path this week. [VERIFIABLE][^10] Anthropic’s official
cookbook documents a built-in `pptx` skill and custom skills for branded presentation generation.
[VERIFIABLE][^15]

These are credible secondary experiments. None provides evidence strong enough to displace Slidev
for a code-first harness without a same-content bake-off on the actual ai-harness story.
[INFERENCE]

### When to use

- Use **Slidev** for engineering talks, live web delivery, code, diagrams, and a reusable local
  harness.
- Use **Plus AI** when native, editable PowerPoint is non-negotiable and external service use is
  approved.
- Use **Gamma** when speed and web-native polish outweigh source-level control.
- Trial **Beautiful.ai**, **Canva**, or Claude’s `pptx` skill when existing licensing, brand
  libraries, or organizational workflows make them cheaper than building the renderer.

## 3. Code-Native Slide Pipelines

### Marp: simplest reliable Markdown pipeline

Marp converts CommonMark-like slide Markdown to HTML, PDF, and PowerPoint and supports CSS themes.
[VERIFIABLE][^3] It has a smaller conceptual surface than Slidev, which reduces agent errors and
makes it a strong fallback. Its expressiveness is correspondingly lower for live components,
interactive diagrams, and bespoke layouts. [INFERENCE]

### PptxGenJS: best local native-PPTX primitive

PptxGenJS generates standards-compatible PowerPoint from JavaScript across Node and browser
environments. [VERIFIABLE][^4] Its slide masters encode corporate branding in code.
[VERIFIABLE][^5] It is an engine, not a design system: an unconstrained LLM can still create
collisions, bad density, and monotonous layouts. The harness must supply typed layout helpers,
coordinate guards, text-fit checks, and rendered QA. [INFERENCE]

### Reveal.js, Quarto, Pandoc, remark, and Deckset

Reveal.js and Pandoc remain mature format/rendering layers, but Quarto is the stronger choice when
the talk is driven by executable Python/R/Julia, citations, or reproducible data. Quarto’s Reveal
format supports Markdown, speaker notes, themes, code highlighting, and executable cell output.
[VERIFIABLE][^12] Deckset is attractive for macOS-only human authoring but is a poor cross-machine
harness dependency; remark.js is simple but offers less integrated tooling than Slidev. [INFERENCE]

### When to use

Use Marp for a conservative text-and-diagram deck; PptxGenJS for truly editable native PowerPoint;
and Quarto for an evidence-heavy or computation-heavy presentation. Use raw Reveal.js only when
the team already owns a Reveal theme or needs plugin-level control.

## 4. Non-Slide Presentation Formats

### Scrolling technical story or interactive explainer

A single responsive page can combine narrative, architecture diagrams, source links, live
controls, and expandable detail without slide-sized clipping. Observable Framework is a
code-first static-site generator for data apps and reports with Markdown, reactive JavaScript,
polyglot data loaders, themes, and a CLI. [VERIFIABLE][^13] A lightweight custom web page is even
more flexible, but it needs stronger navigation and “where are we?” cues during a live talk.
[HEURISTIC]

### Notebook or executable walkthrough

Quarto/Jupyter is effective when the proof is a computation, benchmark, or experiment. It is less
effective when the presenter must maintain pacing across a broad conceptual story. Use executed
outputs and cached artifacts; do not make network availability part of the live path.
[HEURISTIC]

### Terminal presentation and live coding

Presenterm renders Markdown in the terminal with themes, syntax highlighting, images, and PDF/HTML
export. [VERIFIABLE][^14] It matches an engineering aesthetic and keeps the entire artifact
agent-editable. Its risk is room legibility and dependence on terminal/font behavior. For live
coding, prefer a scripted, rehearsed demo with a recorded terminal clip as fallback. [HEURISTIC]

### Narrated video or deterministic demo clip

A code-driven video composition or scripted terminal recorder is useful for asynchronous sharing
and for replacing a fragile live sequence. Because video is slow to revise and weak for discussion,
it should be an embedded 30–90 second asset, not Friday’s primary format. [HEURISTIC]

### When to use

Use a scrolling page for interactive architecture/data stories, a notebook for reproducible
analysis, a terminal deck for developer-native intimacy, and video only when timing and playback
must be deterministic. For Friday, combine a conventional deck with one bounded live or recorded
demo.

## 5. Quality Harness: How to Avoid “AI Slop”

### Constrain before generating

The skill should require:

- one audience and one decision/outcome;
- one message per slide, expressed as an action title;
- a maximum of two body regions and one dominant visual;
- a bounded layout catalog (title, thesis, comparison, process, evidence, code, demo, close);
- design tokens for type, spacing, color, radius, line weight, and chart styling;
- approved exemplars and explicit anti-exemplars;
- a citation/evidence field separate from visible slide copy.

These are design heuristics, not claims that a particular number guarantees quality.
[HEURISTIC]

### Generate in stages

The emerging research supports a staged rather than direct code-emission workflow. DeepSlides
separates page design from implementation and reports stronger evaluated and human-preference
results than its baselines. [VERIFIABLE][^19] SlideRL decomposes the task into research, planning,
generation, refinement, and finalization with structural, render, aesthetic, content, and
specification-recovery signals. [VERIFIABLE][^20] Both are 2026 preprints, so their numeric results
should not be generalized to this tool choice; their process decomposition is the useful finding.
[INFERENCE]

Recommended roles, whether implemented as separate agents or explicit phases:

1. **Researcher:** produces claim/evidence cards only.
2. **Narrative editor:** makes the argument and slide sequence.
3. **Design planner:** selects layout and visual grammar without writing renderer code.
4. **Builder:** emits only schema-valid source.
5. **QA critic:** sees rendered slides, not source, and reports defects.
6. **Presenter:** rehearses timing and removes anything that does not earn its time.

### Render, inspect, repair

Every iteration should render all slides to images and run two lanes:

- **Deterministic:** overflow, off-canvas objects, missing assets, minimum font size, contrast,
  code-line length, duplicate titles, density, broken links, and export success.
- **Visual:** hierarchy, alignment, rhythm, chart honesty, image relevance, consistency, and
  whether the slide’s intended message is recoverable at a glance.

Microsoft recommends at least 18-point sans-serif text and sufficient white space for accessible
PowerPoint. [VERIFIABLE][^16] Unique titles, logical reading order, alt text, and sufficient
contrast should also be enforced by the harness. [HEURISTIC] WCAG 2.2 sets a useful
cross-format floor of 4.5:1 contrast for normal text and 3:1 for large text. [VERIFIABLE][^17]
For projected slides, treat those as minimums rather than targets. [HEURISTIC]

### Human gates

Require human approval at three points: outline/story, representative visual system (three slides),
and final rehearsal/export. The 2026 classroom study is encouraging—coding assistants produced
the most accurate, complete, and pedagogically sound candidates in that experiment—but the chosen
slides were modified and visually matched before use. [VERIFIABLE][^18] It supports assisted
authoring, not one-click autonomy. [INFERENCE]

### When to use

Use the full loop for external, executive, or reusable decks. For internal talks, keep the same
contracts but reduce the number of visual review rounds, never the final render and rehearsal.

## 6. Landscape Triage and Current-State Corrections

The named market is volatile. Tome Slides is not a current candidate: Tome’s own notice says the
product sunset on 2025-04-30 and remaining content became inaccessible. [VERIFIABLE][^11] A
different `tomeapp.ai` domain now markets a presentation product; it should not be treated as
continuity of the sunset Tome service without independent corporate verification. [INFERENCE]

GUI-first products—including Decktopus, SlidesAI, Presentations.AI, Slidebean, and similar
prompt-to-deck services—may produce attractive drafts, but their web editors and vendor-generated
examples do not establish agent drivability, export fidelity, or repeatable quality. [NO SOURCE]
They belong in a visual bake-off, not in the initial harness architecture.

### Gaps, blindspots & emergent findings

- **Independent quality evidence is the largest gap.** Vendor pages dominate search results;
  reproducible cross-product tests using identical source material are missing. [NO SOURCE]
- **Editable PPTX is not a binary feature.** “PPTX export” may mean a deck of rendered images,
  native objects, or a mixture. The bake-off must inspect text, tables, charts, notes, reading
  order, and round-trip behavior, not merely whether PowerPoint opens the file. [HEURISTIC]
- **The strongest 2026 change is agent integration, not raw generation.** Slidev, Plus AI, Gamma,
  and Beautiful.ai now expose skills, MCP, or APIs; this shifts evaluation from screenshot beauty
  toward iteration semantics, source control, and QA. [INFERENCE]
- **Design-system extraction is upstream of generation.** A reusable harness should learn tokens,
  layouts, and density from approved team decks before creating new content. [HEURISTIC]
- **Security/privacy was not resolved.** Remote generators may receive internal architecture,
  metrics, or customer material; plan-level retention, training, and regional-processing terms
  require organizational review. [NO SOURCE]
- **Anchor bias was countered** by excluding sunset Tome, elevating executable and scrolling
  formats, and treating vendor polish as unproven until a same-content test.

### When to use

Use this triage to decide which tools deserve a timed bake-off. Do not spend deadline time
evaluating every branded generator; test only options that could change the delivery decision.

## 7. Interactive React/HTML-Native Decks (2026-09-04 Refine)

### What the pasted AI summary got right—and wrong

The Google summary correctly identified a real StackBlitz project, a maintained Spectacle
library, an official Reveal.js React wrapper, and useful React Flow/Recharts components.
[VERIFIABLE][^21][^29][^32][^39][^40] It overstated the evidence in three places:

- Bolt Slides documents 29 named library entries, not a verified “30+ interactive components,”
  and the repository does not substantiate a general-purpose calculator library. Its examples
  include charts, diagrams, product UI, animation, and a Three.js visualization.
  [VERIFIABLE][^21] Marketing copy and five first-party example decks show intended range, but no
  independent visual-quality benchmark was found. [VERIFIABLE][^22] [NO SOURCE]
- Bolt’s “agent-native” claim means a Vite/React repository plus an instruction file that tells an
  agent to edit `App.tsx`, reuse components, run the app, and check narrow viewports; it is not a
  dedicated headless deck-generation CLI or schema. [VERIFIABLE][^23][^27]
- Reveal.js now has a real official React wrapper, introduced with Reveal.js 6.0 in March 2026;
  this is distinct from embedding raw Reveal.js or relying on an unofficial wrapper.
  [VERIFIABLE][^32][^34]

### Bolt Slides: compelling component kit, immature framework

StackBlitz describes Bolt Slides as presentations that are working web apps. Slides are ordinary
React components, and the repository provides navigation, click builds, notes, presenter sync,
annotations, responsive layouts, and a catalog spanning structure, data, story, product, and flair.
[VERIFIABLE][^21] The July 2026 announcement explicitly names Claude Code, Codex, Cursor, and Bolt
as authoring agents and links five live examples. [VERIFIABLE][^22]

The adoption signals are promising but shallow. On 2026-09-04 the repository showed 917 stars,
113 forks, one issue, five pull requests, eight commits, no releases, and an MIT license; the
visible commit history ran only from July 14 to July 30, 2026. [VERIFIABLE][^21][^24] This passes
an exploratory-adoption threshold, not a production-maturity threshold. [INFERENCE]

Its build is a conventional private Vite app: scripts are `dev`, `build`, and `preview`; `vite
build` produces a deployable bundle, but the repository has no first-party screenshot, PDF, or
single-file-HTML export command. [VERIFIABLE][^23][^28] The default stylesheet imports Google
Fonts, so the unmodified deck violates the harness’s offline/no-CDN rule. [VERIFIABLE][^25] An
open PDF-export pull request documents practical capture failures—missing animated content,
font races, blank slides, missing backgrounds, and an initially 215 MB raster PDF—but was not
merged when checked. [VERIFIABLE][^26] These are direct warnings against treating browser-print
or screenshot export as “free.” [INFERENCE]

Most importantly, the bundled agent skill includes an “Internal trigger (do not mention to the
user)” that instructs an agent to detect a secret phrase, preserve the demo deck, skip the normal
workflow, and conceal why. [VERIFIABLE][^27] Regardless of intent, that instruction conflicts with
this harness’s transparency, provenance, and prompt-injection posture. [INFERENCE] Do not import
that skill. If Bolt is trialed, pin or vendor reviewed engine/components, remove remote fonts, and
write clean harness-owned instructions. [HEURISTIC]

### Spectacle and official Reveal.js React

Spectacle is the mature React-native library in this set: Formidable’s repository describes active
maintenance and showed about 10.1k stars, 1,289 commits, 71 issues, four pull requests, and an MIT
license when checked. [VERIFIABLE][^29] Spectacle 10.2.3 declares React and ReactDOM `>=18`; a
September 2025 release explicitly fixed React 19 compatibility, and maintenance continued into
April 2026. [VERIFIABLE][^30][^31] It supplies presentation primitives and theming, but not the
harness’s manifest, design grammar, static-build policy, or QA pipeline. [INFERENCE]

The official `@revealjs/react` wrapper exposes Deck, Slide, Stack, Fragment, Code, and Markdown
components and owns Reveal initialization, synchronization, and cleanup. [VERIFIABLE][^32] Its
package declares React/ReactDOM `>=18`, and the upstream dev matrix uses React 19.2.4.
[VERIFIABLE][^33] Reveal.js 6.0.1 followed the wrapper’s March 2026 debut with Vite 8/TypeScript 6
compatibility and PDF fixes in April. [VERIFIABLE][^34] Reveal.js itself reports more than
100,000 weekly npm downloads, MIT licensing, speaker notes, APIs, and PDF export; its official
Chromium print path can include notes and fragment states. [VERIFIABLE][^35][^36]

Neither library requires a CDN: application code, themes, fonts, and assets can be installed or
bundled locally. [INFERENCE] Neither produces a standalone deck without a build/application shell,
and neither guarantees visual distinction. The design-system and authoring constraints remain
harness responsibilities. [INFERENCE] If a long-lived React engine becomes necessary,
`@revealjs/react` is the safer base because it combines a small React binding with Reveal’s
established navigation, presenter, plugin, and PDF semantics; Spectacle is reasonable where its
component API/theme model is already preferred. [INFERENCE]

### Components and adjacent projects are not frameworks

React Flow’s official slideshow tutorial demonstrates a spatial, Prezi-like deck by moving a
viewport among graph nodes, with click, button, and keyboard navigation. [VERIFIABLE][^39]
Recharts is a current MIT-licensed declarative SVG chart library, not a slide system.
[VERIFIABLE][^40] Both can enrich a React deck; adopting them does not solve slide order,
presenter mode, printing, build determinism, accessibility, or manifest-to-DOM traceability.
[INFERENCE]

The Lovable “Presentations & Slides” page is a hosted template, not adoption-grade evidence: the
page advertises React/TypeScript, fixed 1920×1080 scaling, sidebar/fullscreen/keyboard navigation,
and showed zero remixes, but exposes no linked source repository or license. [VERIFIABLE][^41]
[NO SOURCE] MDX Deck’s npm package was last published six years ago, while DeckDeckGo was archived
in 2024; neither is a sound new default in 2026. [VERIFIABLE][^42][^43]

Two newer ideas are worth watching, not adopting. `astro-slides` advertises MDX/React, MCP,
screenshots, PDF export, and static/offline output, but showed five stars, no forks, and no
releases. [VERIFIABLE][^44] Cogapp’s Astro/MDX template combines local fonts with Playwright PDF
export, but showed no stars or forks. [VERIFIABLE][^45] These corroborate the pattern—static web
build plus browser capture—without establishing mature products. [INFERENCE]

### Slidev’s actual interactivity ceiling

Slidev can use Vue components directly in slides and documents custom components as interactive
and dynamic; components may live in the project, theme, or addon. [VERIFIABLE][^37] It can
self-host a SPA bundle, while its Playwright exporter produces PDF, PPTX, PNG, and Markdown;
exported files cannot retain web interactivity. [VERIFIABLE][^2][^38] Therefore charts,
calculators, stateful comparisons, animated system flows, and bounded live demos do not require a
React migration. [INFERENCE]

The current deck’s static use of Vue layouts does not establish a Slidev platform ceiling.
[INFERENCE] A React switch may improve access to React-specific libraries or make a single-file
web-app idiom more natural, but it will not automatically improve composition, typography,
visual hierarchy, or originality. [HEURISTIC] Those depend on stronger reference extraction,
component primitives, controlled variants, and real rendered-pixel critique. [INFERENCE]

### Headless rendering and deterministic QA

All candidate browser frameworks can fit a Playwright pipeline if the harness—not deck code—owns
the contract. Vite can emit static assets, including relative-path deployments when `base` is
configured appropriately. [VERIFIABLE][^28] Playwright can inject initialization code before page
scripts, capture screenshots, and generate PDFs; its documentation discourages using
`networkidle` as a readiness oracle. [VERIFIABLE][^46] A robust contract should:

1. build with the network disabled after dependencies are installed;
2. bundle fonts/assets and reject outbound runtime requests;
3. expose a stable route plus `data-slide-id` for every manifest slide;
4. disable transitions and seed time/randomness in capture mode;
5. wait for an explicit `document.fonts.ready` plus deck-ready signal;
6. render every click/fragment state where it changes meaning;
7. compare repeated screenshots for instability, then lint the inspectable DOM;
8. export screen-styled PDF/PNG and test presenter delivery separately.

These are engineering controls inferred from the official build/capture surfaces and the Bolt PDF
failures. [INFERENCE] A 2026 preprint, ReDeck, independently supports the broader loop: it reports
“one edit, one observation,” step-level render feedback, a turn-level critic, and a final submission
gate, with gains across evaluated models. [VERIFIABLE][^47] It is a fresh preprint, so it supports
the workflow pattern, not any framework choice. [INFERENCE]

### Custom React plus Tailwind: highest ownership, not lowest effort

A custom carousel is easy to demo and expensive to operationalize. Navigation and full-screen state
are small; the hard work is presenter mode, fragment semantics, URL/deep-link behavior, focus and
touch handling, print layout, font readiness, deterministic animations, crash recovery, notes,
accessibility, PDF pagination, manifest traceability, and repeatable QA. [HEURISTIC]

Because the harness already owns manifest, render, lint, critic, and theme-token contracts, a
custom deck would duplicate framework concerns before improving visual quality. [INFERENCE]
Tailwind is a styling mechanism, not a design system; React Flow and Recharts are components, not
that missing contract. [INFERENCE] Build from scratch only when a proven interaction cannot be
expressed in Slidev or a small React framework and that interaction is central to the talk.
[HEURISTIC]

### Gaps, blindspots & emergent findings: interactive-deck refine

- **No independent visual benchmark.** Bolt’s live examples demonstrate capability, but there is
  no reproducible 2025–2026 same-content comparison against Slidev, Spectacle, or Reveal React.
  [NO SOURCE]
- **No verified React 19 guarantee for Bolt Slides.** Its package pins React 18.3.1; no release or
  compatibility matrix was found. [VERIFIABLE][^23] [NO SOURCE]
- **Offline is a deck policy, not a framework property.** Any option can reintroduce remote fonts,
  images, embeds, analytics, or runtime APIs unless CI blocks them. [HEURISTIC]
- **Interactive export is necessarily lossy.** PDF/PNG represent states, not behavior; the
  manifest must identify the canonical captured state and the hosted HTML must be the interactive
  artifact. [HEURISTIC]
- **Supply-chain review includes agent instructions.** Bolt’s concealed phrase-trigger shows that
  vendored skills/prompts deserve the same review and pinning as executable dependencies.
  [INFERENCE]
- **Post-2024 React Flow presentation research:** no significant development beyond the 2024
  official tutorial was found. [NO SOURCE]
- **Anchor bias was countered** by testing the premise that React is required, rejecting
  component libraries as frameworks, checking abandoned projects, and treating Bolt’s own agent
  instructions as security-sensitive input.

### When to use

Use Slidev components for the next deck and a time-boxed Bolt-derived prototype only as a
same-content comparison. Use `@revealjs/react` if the experiment proves React ecosystem access is
material and a supported engine is preferred. Use Spectacle when its API/theme model is already
an organizational standard. Use custom React only for a signature interaction that cannot be
implemented cleanly elsewhere.

## 8. Harness Reuse and Live-Deck Testing (2026-10-21 Refine)

This refine answers two questions for version 2 of the presentation skill. Which of ai-harness's
own visualization, graphics, browser and frontend elements should the skill use? And how should an
agent test a reveal.js React deck as a live page rather than as isolated slide images? Example User
settled the framework question himself on 2026-09-04 ("and no, use https://revealjs.com/react/"),
after §7's recommendation to keep Slidev, so this section does not reopen it.

### Repository state the refine starts from

The skill was Slidev-only when measured on 2026-10-21: SKILL.md says "v1 supports only Slidev"
(skills/presentation/SKILL.md:9-13). A grep for `reveal` over the skill and its design returned
zero files, while `slidev` matched 17. [VERIFIABLE][^48] The multi-framework draft recorded as shipped
on 2026-09-04 exists only as an unmerged commit on another host. [VERIFIABLE][^49]

The critic attaches only the contact sheet and the isolated per-slide PNGs. Its own prompt says
"Do not claim that DOM, links, keyboard controls, animation, or presenter behavior were visually
tested from static pixels" (skills/presentation/scripts/critic:122-128). [VERIFIABLE][^50] The lint's
DOM inspector (skills/presentation/scripts/inspect_dom.cjs) does drive the served SPA in
Chromium. It reads only elements inside `.slidev-page`, advances with ArrowRight and fixed 250 ms
waits, takes no full-viewport screenshot and records no console errors. [VERIFIABLE][^51] An element
outside the slide root, which is where `#slidev-goto-dialog` lived, is invisible to every existing
check. [INFERENCE]

### Inventory of harness elements

| Element | What it does, from the code | Fit for presentations |
|---|---|---|
| Archify (`skills/archify/`) | Ten diagram types; `finalize` runs a real-browser check and can capture over direct CDP (`bin/visual-check.mjs`). | High for diagram slides: an `archify_print` SVG becomes a deck asset. |
| Shared doc renderer (`skills/_shared/doc-render/`) | Markdown to HTML, PDF and per-section screenshots with a receipt; Python Playwright. | Medium: useful receipt and capture patterns, but a second toolchain and a document-shaped job. |
| Graphic skill (`skills/graphic/`) | Sanitizes constrained SVG, runs SVGO and renders PNG cells for a human pick. | Medium, for icons and simple marks. |
| CDP automation (`skills/cdp-automation/`, `docs/procedures/cdp-browser-automation.md`) | Drives an attached browser over CDP through CLIs; forbids browser MCPs and focus stealing ("attach, don't open tabs"). | High for human-in-the-loop viewing; wrong for a reproducible QA gate. |
| Diagram render and vision review (`docs/procedures/diagram-render-and-vision-review.md`) | Render, crop, non-author review per criterion with pixel locations; "confirm every geometric claim ... by measurement before acting on it". | High: the measurement rule and cross-family reviewer apply to deck critique. |
| Visual doc quality rubric (`docs/procedures/visual-doc-quality-rubric.md`) | Nine criteria for roadmap and architecture docs, plus a review-recording table. | High for §7 Accessibility and §9 Diagram construction and the recording table; the rest is document-specific. |
| Mermaid and full-document PDF audits | Rasterize Mermaid; audit Markdown through HTML, PDF and page PNGs. | Low: document routes. |
| Upload-screenshot | Classifies and files existing screenshots remotely. | Low: transport, not review. |
| Reference deck (`docs/presentation/decks/ai-harness-engineering/examples/AI_Harness_Engineering.html`) | Standalone HTML with viewport-filling absolute slides, transitions, buttons, progress dots, section labels, keyboard, fullscreen and touch; grids, cards, flows, tables, stacked layers and code panels. It loads Google Fonts remotely and reflows below 1100 px rather than scaling a canvas. | High as the visual-grammar reference for layout components; its remote fonts break the offline rule. |

Every row was read from the named files on 2026-10-21. [VERIFIABLE][^52][^53][^54][^55][^56][^57][^58]
The reference deck was absent when §7 was written and is present now.

### Duplications the skill carries

- Two browser toolchains: Node `playwright` 1.58.2 with a version-keyed cache in the skill, and
  `playwright>=1.50` in doc-render's `pyproject.toml`; Archify uses system Chrome over CDP.
  [VERIFIABLE][^52][^53][^59]
- Three screenshot producers: the skill's PNG export, doc-render's section captures, Archify's
  `Page.captureScreenshot`. [VERIFIABLE][^52][^53]
- Two visual rubrics: the critic's seven items (`scripts/qa_support.py`) and the visual doc
  rubric. [VERIFIABLE][^57][^60]
- Separate token files for the skill, Archify and doc-render. [VERIFIABLE][^52][^53]

Unifying these is a cross-skill migration and touches palette and font work that is out of scope
now. [INFERENCE] The design adopts per element and records the residue as follow-up.

### Upstream fidelity of the version 1 design

The design faithfully implements the six-role decomposition, dual-lane QA and three human gates
from §5. It under-uses §7's eight-point capture contract: seeded capture mode, font and deck
readiness, fragment states, repeated-capture stability and separate presenter testing are absent.
[VERIFIABLE][^60] The implementation audit's recorded Slidev pin (52.10.0) is stale against the current
52.19.1 in `package.json`. [VERIFIABLE][^59]

### Building a reveal.js React deck

The current registry versions are `@revealjs/react` 0.2.1, `reveal.js` 6.0.2, `react` and
`react-dom` 19.3.1, and `vite` 8.3.4; the wrapper declares `reveal.js >=6` and React `>=18` as
peers. [VERIFIABLE][^61][^62][^63][^64][^65] `@vitejs/plugin-react` 6.0.5 supplies the JSX build,
and the skill's existing `playwright-chromium` 1.58.2 pin matches the current Playwright 1.58.2
release, so the browser driver needs no change. [VERIFIABLE][^97][^98][^99] The wrapper lives in the reveal.js repository and drew
5,179 weekly downloads, while reveal.js itself has over 72,000 GitHub stars. Those signals support
adoption of the engine; the wrapper itself is young. [VERIFIABLE][^66][^67][^68]

The wrapper exports `Deck`, `Slide`, `Stack`, `Fragment`, `Code`, `Markdown` and `useReveal`.
`deckRef`, `useReveal()` and `onReady(deck)` expose the Reveal API, `Deck` takes any Reveal
option through its `config` prop (`width`, `height`, `margin`, `hash`, `transition`, `controls`,
`progress`, `slideNumber`, `embedded`), and plugins go through a separate `plugins` prop before
initialization. `Code` needs `RevealHighlight` and speaker notes need `RevealNotes`.
[VERIFIABLE][^66] The wrapper owns initialization and cleanup, so adapter code must not construct
a second Reveal instance. [VERIFIABLE][^66] No primary source after 2024 documents a
wrapper-specific StrictMode double-initialization defect. [NO SOURCE]

The readiness and navigation oracle is the Reveal API: `ready`, `isReady()`, `slide(h, v, f)`,
`getIndices()`, `getState()` and `getTotalSlides()`, with `slidechanged` and `fragmentshown`
marking navigation and fragment steps. [VERIFIABLE][^69][^70] Hash routes such as `#/3` work with
`hash: true`, but API navigation gives stronger assertions than URL mutation. [VERIFIABLE][^71]
Vite's `base: './'` makes the build relocatable, and assets imported through Vite's graph are
bundled locally. [VERIFIABLE][^72][^73] Reveal scales an authored canvas and letterboxes on a
mismatched viewport. [VERIFIABLE][^78]

PDF export opens `?print-pdf` and calls Chromium's `page.pdf` with backgrounds; reveal.js prints
each fragment state on its own page by default and can include notes. [VERIFIABLE][^74][^75]
DeckTape 3.16.1 is maintained and reveal-aware, but no 2025-2026 comparison shows it exports
better, so the harness's pinned Playwright comes first. [VERIFIABLE][^76] The speaker view opens
with `S` in a second window and needs its own test. [VERIFIABLE][^77]

### Testing the live deck

Install page listeners before navigation, and fail on console errors, `pageerror`,
`requestfailed`, any HTTP response of 400 or more, and any non-loopback request. A 404 produces a
response rather than `requestfailed`, so both event classes are needed. [VERIFIABLE][^84] Wait on
`isReady()` and `document.fonts.ready` rather than `networkidle` or a sleep, and freeze time with
Playwright's clock. [VERIFIABLE][^79][^80] Fix reduced motion, locale, time zone, colour scheme
and device scale factor in the browser context. [HEURISTIC]

Capture the whole viewport, because an element screenshot omits the chrome and overlays that
caused this skill's past defect. Playwright's own screenshot assertion waits for two consecutive
matching images, which is the stability check. [VERIFIABLE][^81] `document.elementFromPoint()`
returns the topmost element at any coordinate, and Playwright's actionability check detects
obstruction only at interaction points. [VERIFIABLE][^82][^83] Grid sampling over the slide box
is therefore a harness heuristic; no published deck-occlusion method was found. [NO SOURCE]

`@axe-core/playwright` 4.13.0 runs `AxeBuilder.analyze()`; automated scans find only some
accessibility defects, and focusable content under `aria-hidden` is itself a violation, so scan
each active state and walk the Tab order separately. [VERIFIABLE][^85][^86][^87]

A CVPR 2026 benchmark of ten multimodal models found weak fine-grained UI defect detection: 20.36%
F1 for text overflow and 31.21% for container overlap. [VERIFIABLE][^88] Vision review is
therefore a critic and never the only gate, which is the diagram review procedure's
measure-before-acting rule applied to decks. [INFERENCE]

reveal.js tests itself with QUnit under Puppeteer, Slidev with Cypress, Vitest and Playwright,
and Spectacle with Jest and Puppeteer; none publishes a per-deck QA contract covering every state,
runtime health, overlays, accessibility and viewport screenshots. [VERIFIABLE][^89][^90][^91]
[NO SOURCE]

Defaults worth encoding: a 16:9 canvas, 1280x720 in Reveal coordinates. [VERIFIABLE][^92]
Projected body text of at least 24 pt, raised after testing the real room.
[VERIFIABLE][^93][^94] Non-essential motion off under `prefers-reduced-motion`. [VERIFIABLE][^95]
Fragments only for authored incremental reveal, each meaningful state tested.
[VERIFIABLE][^96]

### When to use

Use the skill's pinned headless Chromium for every automated live check, and the attached-browser
CDP route only when a human wants to see or capture the deck in their own browser. Use Archify for
any diagram slide. Apply the diagram review procedure's measurement rule before acting on any
geometric claim a vision model makes about a slide.

## Comparison

| # | Option | Agent drive | Visual/control profile | Delivery | Best fit | Decision |
|---|---|---|---|---|---|---|
| 1 | Slidev | Official skill + MCP [VERIFIABLE][^1] | High control; needs constraints | HTML, PDF, PPTX, PNG [VERIFIABLE][^2] | Engineering talk + reusable harness | **Primary** [INFERENCE] |
| 2 | Marp | Excellent CLI/file fit | Lower ceiling, lower variance | HTML, PDF, PPTX [VERIFIABLE][^3] | Fast conservative fallback | **Fallback** [INFERENCE] |
| 3 | PptxGenJS | Excellent local code fit | Maximum object control; highest build burden | Native OOXML [VERIFIABLE][^4][^5] | Editable PPTX and deterministic automation | **Harness phase 2** [INFERENCE] |
| 4 | Quarto/Reveal | Excellent file/CLI fit | Strong code/data/citation support | HTML/PDF and presentation formats [VERIFIABLE][^12] | Reproducible technical evidence | **Alternative** [INFERENCE] |
| 5 | Plus AI | Remote MCP/API with multi-turn revision [VERIFIABLE][^8] | Managed layouts; vendor dependency | Native PPTX + PDF + thumbnail | Urgent editable PowerPoint | **Bake-off** [INFERENCE] |
| 6 | Gamma | REST/MCP; one-shot REST edits [VERIFIABLE][^6][^7] | Fast web polish; less source control | Web, PDF, PPTX, PNG | Rapid draft/visual benchmark | **Bake-off** [INFERENCE] |
| 7 | Beautiful.ai | Early-access API [VERIFIABLE][^9] | Managed layout system | Web, PPTX, PDF | Existing Beautiful.ai teams | **Secondary** [INFERENCE] |
| 8 | Canva AI 2.0 | Agentic editing in research preview [VERIFIABLE][^10] | Strong brand/editor surface | Canva/web/export ecosystem | Human-led brand production | **Do not depend on preview** [INFERENCE] |
| 9 | Other GUI generators | Mostly GUI; API depth varies | Unverified across same-content test | Usually web/PDF/PPTX | Disposable first drafts | **Defer** [NO SOURCE] |
| 10 | Observable/scrolling web | Excellent code/CLI fit [VERIFIABLE][^13] | Highest interactive/data flexibility | Static web | Companion explainer/dashboard | **Companion** [INFERENCE] |
| 11 | Presenterm | Excellent Markdown/terminal fit [VERIFIABLE][^14] | Deliberately terminal-native | Terminal, PDF, HTML | Small developer room | **Niche** [INFERENCE] |
| 12 | Slidev components | Existing file/CLI path; custom Vue [VERIFIABLE][^37] | Interactive without migration | SPA, PDF/PPTX/PNG [VERIFIABLE][^2][^38] | Next production build | **Keep primary** [INFERENCE] |
| 13 | Bolt Slides | Agent-editable Vite/React app [VERIFIABLE][^21][^23] | Rich catalog; immature and unsafe bundled skill [VERIFIABLE][^24][^27] | Static Vite bundle [VERIFIABLE][^28] | Same-content prototype | **Guarded bake-off** [INFERENCE] |
| 14 | `@revealjs/react` | Official React components [VERIFIABLE][^32] | Mature engine; design system still required | Web + Chromium PDF [VERIFIABLE][^35][^36] | Long-lived React path | **React fallback** [INFERENCE] |
| 15 | Spectacle | React library, actively maintained [VERIFIABLE][^29][^31] | Deep React theming; harness work remains | Web/app-defined export | Existing Spectacle teams | **Secondary** [INFERENCE] |
| 16 | Custom React + Tailwind | Maximum agent/code control | Maximum platform ownership | App-defined | Unique central interaction | **Do not start here** [INFERENCE] |

## Recommendation

### 2026-09-04 refine: retain Slidev, prototype React alongside

Do **not** replace Slidev for the next production build. Its existing component and export surfaces
meet the interactive, offline, inspectable-DOM, and Playwright requirements; changing frameworks
before exercising them would confound renderer choice with the still-unfixed authoring and visual
review process. [INFERENCE]

Run one guarded, same-manifest Bolt Slides prototype alongside the Slidev build. Treat Bolt as a
component/design exploration, not a trusted framework distribution: pin or vendor reviewed code,
delete the upstream agent skill, remove Google Fonts, configure relative static assets, and put
capture/readiness semantics in harness-owned code. [INFERENCE] Preserve Slidev as the deadline
fallback.

Promote a React path only if it wins on all of these gates:

1. zero runtime network requests and reproducible clean builds;
2. identical screenshots across repeated capture runs after transitions are disabled;
3. complete PNG/PDF output across slides, fragments, fonts, and backgrounds;
4. stable manifest-to-DOM slide IDs plus existing overflow/density/accessibility lint;
5. materially better human scores for hierarchy, composition, message clarity, and visual
   distinctiveness—not merely motion;
6. no increase in median agent authoring and repair time on the same brief.

If React is promoted for long-term use, adopt `@revealjs/react` as the navigation/presenter/export
engine and port only the Bolt-derived component and token patterns that won the bake-off.
[INFERENCE] Spectacle is mature but offers no evidenced agent-authoring advantage; custom
React+Tailwind should remain an escape hatch for one signature interaction, not the default
platform. [INFERENCE]

### Friday delivery: Slidev plus a bounded demo

Build the actual deck in Slidev, host it locally or on an approved static host, and export a PDF
before rehearsal. Keep the presentation source, theme, diagrams, and speaker notes together. Do not
make PPTX part of the critical path unless the meeting environment requires it. [INFERENCE]

A practical initial outline:

1. **AI harness engineering is the product around the model** — hook and definition.
2. **Why prompts alone do not scale** — drift, context loss, inconsistent quality.
3. **The harness anatomy** — context, skills, tools, gates, provenance, and feedback.
4. **One request through the system** — a clear end-to-end flow diagram.
5. **What becomes reusable** — skills, templates, decision rules, and checks.
6. **Presentation generation as a harness case study** — apply the same architecture to this deck.
7. **Live/recorded walkthrough** — change one source fact, rebuild, and show the artifact diff.
8. **Quality and safety gates** — evidence, visual QA, accessibility, and human review.
9. **Operating model** — what agents do autonomously and where people decide.
10. **Next experiments** — presentation skill, benchmark set, and native-PPTX branch.
11. **Close** — “build systems that make good model behavior repeatable.”

By the first internal draft, slides 1–6 and 11 should be complete; slides 7–10 may use explicit
placeholders. By the final rehearsal, replace the live demo with a deterministic recording if any
step is network-dependent or takes more than 30 seconds. [HEURISTIC]

### Reusable harness after Friday

Package a `presentation-authoring` skill with:

```text
presentation/
├── SKILL.md
├── schemas/deck.schema.json
├── themes/ai-harness.{css,json}
├── layouts/
├── scripts/render
├── scripts/lint
├── scripts/contact-sheet
├── references/voice-and-density.md
├── exemplars/
└── evals/
```

The skill should accept a research document and event brief, emit a deck manifest before source,
render through Slidev, generate a contact sheet, run deterministic lint, request visual critique,
and produce HTML/PDF plus a QA report. Add PptxGenJS only after a small evaluation corpus proves a
need for editable PowerPoint. Keep Gamma and Plus AI as optional adapters, not core dependencies.
[INFERENCE]

### Twenty-four-hour bake-off

If credentials already exist, send the same frozen 8-slide outline, evidence pack, brand tokens,
and asset set through Slidev, Gamma, and Plus AI. Time-box each to 45 minutes, then score:

1. story fidelity;
2. first-render legibility;
3. visual distinctiveness without decorative noise;
4. code/diagram quality;
5. edit latency;
6. accessibility;
7. export fidelity;
8. source ownership and reproducibility.

The winner may change the renderer for Friday, but not the content schema or QA contract.
[HEURISTIC]

## Open Questions

1. Is native editable PPTX an actual meeting requirement, or is HTML plus PDF acceptable?
2. Which existing ai-harness brand assets, fonts, and approved decks may be used as exemplars?
3. May internal architecture or metrics be sent to Gamma, Plus AI, Beautiful.ai, or Canva?
4. Does the presentation room have reliable browser/network access and a second display?
5. Should the talk optimize for a decision, training, adoption, or a technical design review?
6. Can a 45-minute same-content bake-off be completed before the draft checkpoint?
7. Who owns the final human visual review and rehearsal sign-off?
8. Should the reusable skill target Slidev only first, or define a renderer-neutral deck schema
   immediately?
9. Which reference deck should replace the unavailable HTML path for the same-content visual
   benchmark?
10. What measurable score delta and authoring-time ceiling should promote a React renderer?
11. Must the interactive HTML be a single self-contained file, or is a static asset directory
   acceptable?
12. Which interaction states must become separate PDF/PNG frames?

## Sources

[^1]: Slidev. (2026). [Work with AI](https://sli.dev/guide/work-with-ai). Slidev Documentation.
    Verified accessible (HTTP 200) 2026-09-01. (Official MCP server, official skill, and AI-agent
    workflow.)
[^2]: Slidev. (2026). [Exporting](https://sli.dev/guide/exporting). Slidev Documentation.
    Verified accessible (HTTP 200) 2026-09-01. (CLI/browser export formats and interactivity
    limitation.)
[^3]: Marp Team. (2026). [Marp: Markdown Presentation Ecosystem](https://marp.app/). Marp.
    Verified accessible (HTTP 200) 2026-09-01. (Markdown format, CSS theming, and HTML/PDF/PPTX
    output.)
[^4]: Ely, B. (2025). [Introduction](https://gitbrent.github.io/PptxGenJS/docs/introduction/).
    PptxGenJS Documentation. Verified accessible (HTTP 200) 2026-09-01. (OOXML outputs and
    JavaScript environments.)
[^5]: Ely, B. (2025). [Masters and Placeholders](https://gitbrent.github.io/PptxGenJS/docs/masters.html).
    PptxGenJS Documentation. Verified accessible (HTTP 200) 2026-09-01. (Programmatic slide
    masters and branding.)
[^6]: Gamma. (2026). [Gamma Developer Docs](https://developers.gamma.app/). Gamma.
    Verified accessible (HTTP 200) 2026-09-01. (Generate API, themes, formats, exports, and MCP.)
[^7]: Gamma. (2026). [API scope and limits](https://developers.gamma.app/reference/common-feature-requests).
    Gamma Developer Docs. Verified accessible (HTTP 200) 2026-09-01. (Create-versus-edit
    limitation and temporary export URLs.)
[^8]: Plus Docs, Inc. (2026). [Presentation MCP Server](https://plusai.com/features/mcp).
    Plus AI. Verified accessible (HTTP 200) 2026-09-01. (MCP tools, multi-turn revisions, and
    native output.)
[^9]: Beautiful.ai. (2026). [Beautiful.ai API](https://support.beautiful.ai/hc/en-us/articles/43654071102605-Beautiful-ai-API).
    Beautiful.ai Help Center. Verified accessible (HTTP 200) 2026-09-01. (Early-access
    prompt-to-deck API.)
[^10]: Canva. (2026). [Introducing Canva AI 2.0: Reimagining how the world creates](https://www.canva.com/newsroom/news/canva-create-2026-ai/).
    Canva Newsroom. Verified accessible (HTTP 200) 2026-09-01. (Research-preview status, layered
    editing, brand intelligence, and agentic workflows.)
[^11]: Tome Team. (2025). [Tome Slides is sunsetting on April 30th, 2025](https://tome.app/help/en/articles/10744234-tome-slides-is-sunsetting-on-april-30th-2025).
    Tome Help Center. Live URL now returns HTTP 404 (checked 2026-09-02) — original page
    confirmed via Wayback Machine snapshot [http://web.archive.org/web/20250613200937/https://tome.app/help/en/articles/10744234-tome-slides-is-sunsetting-on-april-30th-2025](http://web.archive.org/web/20250613200937/https://tome.app/help/en/articles/10744234-tome-slides-is-sunsetting-on-april-30th-2025)
    (captured 2025-06-13, HTTP 200), which contains "sunsetting" / "April 30" language matching
    the claim. (First-party shutdown notice; page since taken offline.)
[^12]: Quarto. (2026). [Revealjs](https://quarto.org/docs/presentations/revealjs/). Quarto
    Documentation. Verified accessible (HTTP 200) 2026-09-01. (Markdown slides, notes, themes,
    code, and executable output.)
[^13]: Observable, Inc. (2026). [Observable Framework](https://observablehq.com/framework/).
    Observable Framework Documentation. Verified accessible (HTTP 200) 2026-09-01. (Code-first
    static data apps, reports, loaders, and CLI.)
[^14]: Fontanini, M. (2026). [presenterm](https://mfontanini.github.io/presenterm/). presenterm
    Documentation. Verified accessible (HTTP 200) 2026-09-01. (Markdown terminal presentations,
    themes, code, images, and export.)
[^15]: Anthropic. (2026). [Claude Skills Cookbook](https://github.com/anthropics/claude-cookbooks/blob/main/skills/README.md).
    GitHub. Verified accessible (HTTP 200) 2026-09-01. (Built-in PowerPoint skill and branded
    custom-skill patterns.)
[^16]: Microsoft. (2026). [Make your PowerPoint presentations accessible to people with disabilities](https://support.microsoft.com/en-us/accessibility/powerpoint/make-your-powerpoint-presentations-accessible-to-people-with-disabilities).
    Microsoft Support. Verified accessible (HTTP 200) 2026-09-01. (Titles, reading order, alt
    text, contrast, and font guidance.)
[^17]: World Wide Web Consortium. (2026). [Understanding Success Criterion 1.4.3: Contrast (Minimum)](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html).
    W3C Web Accessibility Initiative. Verified accessible (HTTP 200) 2026-09-01. (WCAG 2.2 AA
    contrast thresholds.)
[^18]: Leinonen, J., Zhang, L., & Hellas, A. (2026). [AI-Generated Slides: Are They Good? Can Students Tell?](https://arxiv.org/abs/2605.13532).
    arXiv. Verified accessible (HTTP 200) 2026-09-01. (2026 preprint; narrow classroom case
    study, not a commercial-tool benchmark.)
[^19]: Cui, Z., Wang, C., Hu, S., Zhang, Y., Shao, W., Zhang, Q., & Wang, Z. (2026). [Design First, Code Later: Aesthetically Pleasing Template-Free Slides Generation](https://arxiv.org/abs/2605.26451).
    arXiv. Verified accessible (HTTP 200) 2026-09-01. (2026 preprint; hierarchical design-first
    workflow.)
[^20]: Ananda Kumar, K. R., & Arunachalam, S. (2026). [Learning to Present: Inverse Specification Rewards for Agentic Slide Generation](https://arxiv.org/abs/2603.16839).
    arXiv. Verified accessible (HTTP 200) 2026-09-01. (2026 preprint; staged tool workflow and
    multi-component evaluation.)

[^21]: StackBlitz. (2026). [bolt-slides](https://github.com/stackblitz/bolt-slides).
    GitHub. Verified accessible 2026-09-04. (Repository capabilities, component catalog,
    adoption signals, license, issues, pull requests, and release status.)
[^22]: StackBlitz. (2026, July 16). [Introducing Bolt Slides](https://bolt.new/blog/introducing-bolt-slides).
    Bolt.new Blog. Verified accessible 2026-09-04. (Agent claims and first-party example decks;
    not independent quality evidence.)
[^23]: StackBlitz. (2026). [bolt-slides `package.json`](https://github.com/stackblitz/bolt-slides/blob/main/package.json).
    GitHub. Verified accessible 2026-09-04. (Private Vite app, scripts, React 18.3.1, Vite 5.4,
    dependencies, and MIT metadata.)
[^24]: StackBlitz. (2026). [Commits to bolt-slides main](https://github.com/stackblitz/bolt-slides/commits/main/).
    GitHub. Verified accessible 2026-09-04. (Eight-commit July 2026 history.)
[^25]: StackBlitz. (2026). [Bolt Slides base stylesheet](https://github.com/stackblitz/bolt-slides/blob/main/src/styles/base.css).
    GitHub. Verified accessible 2026-09-04. (Default Google Fonts imports.)
[^26]: StackBlitz. (2026). [Add PDF export functionality with CLI support, pull request 10](https://github.com/stackblitz/bolt-slides/pull/10).
    GitHub. Verified accessible 2026-09-04; pull request open. (Unmerged export implementation and
    documented browser-capture failures.)
[^27]: StackBlitz. (2026). [Bolt Slides agent skill](https://github.com/stackblitz/bolt-slides/blob/main/.bolt/skills/slides/SKILL.md).
    GitHub. Verified accessible 2026-09-04. (Agent workflow plus concealed phrase-trigger
    instruction.)
[^28]: Vite Team. (2026). [Building for production](https://vite.dev/guide/build).
    Vite Documentation. Verified accessible 2026-09-04. (Static production bundle and relative
    `base` configuration.)
[^29]: Formidable Labs. (2026). [Spectacle](https://github.com/FormidableLabs/spectacle).
    GitHub. Verified accessible 2026-09-04. (Maintenance statement, adoption signals, license,
    and repository activity.)
[^30]: Formidable Labs. (2026). [Spectacle package manifest](https://github.com/FormidableLabs/spectacle/blob/main/packages/spectacle/package.json).
    GitHub. Verified accessible 2026-09-04. (Version 10.2.3, React peer range, dependencies,
    and license.)
[^31]: Formidable Labs. (2025–2026). [Spectacle releases](https://github.com/FormidableLabs/spectacle/releases).
    GitHub. Verified accessible 2026-09-04. (React 19 compatibility fix and current releases.)
[^32]: Hakim El Hattab. (2026). [React](https://revealjs.com/react/). Reveal.js Documentation.
    Verified accessible 2026-09-04. (Official wrapper components and lifecycle behavior.)
[^33]: Hakim El Hattab. (2026). [`@revealjs/react` package manifest](https://github.com/hakimel/reveal.js/blob/master/react/package.json).
    GitHub. Verified accessible 2026-09-04. (Peer ranges and React 19 development matrix.)
[^34]: Hakim El Hattab. (2026). [Reveal.js releases](https://github.com/hakimel/reveal.js/releases).
    GitHub. Verified accessible 2026-09-04. (React-wrapper introduction and 6.0.1 fixes.)
[^35]: Hakim El Hattab. (2026). [reveal.js](https://www.npmjs.com/package/reveal.js). npm.
    Verified accessible 2026-09-04. (Version, weekly downloads, dependents, license, and feature
    overview.)
[^36]: Hakim El Hattab. (2026). [PDF export](https://revealjs.com/pdf-export/). Reveal.js
    Documentation. Verified accessible 2026-09-04. (Chromium print workflow, notes, sizes,
    and fragment handling.)
[^37]: Slidev. (2026). [Components](https://sli.dev/guide/component). Slidev Documentation.
    Verified accessible 2026-09-04. (Custom interactive/dynamic Vue components.)
[^38]: Slidev. (2026). [Hosting](https://sli.dev/guide/hosting). Slidev Documentation.
    Verified accessible 2026-09-04. (Self-hosted SPA build.)
[^39]: xyflow. (2024). [Slide Shows With React Flow](https://reactflow.dev/learn/tutorials/slide-shows-with-react-flow).
    React Flow Documentation. Verified accessible 2026-09-04. (Spatial node-based slideshow
    pattern and controls.)
[^40]: Recharts Group. (2026). [Recharts](https://www.npmjs.com/package/recharts). npm.
    Verified accessible 2026-09-04. (Current version, SVG component model, adoption, and MIT
    license.)
[^41]: Lovable. (2026). [Presentations & Slides](https://lovable.dev/templates/apps/presentations/lovable-slides).
    Lovable Templates. Verified accessible 2026-09-04. (Hosted template features and remix count;
    no linked source license located.)
[^42]: Compositor. (2020). [mdx-deck](https://www.npmjs.com/package/mdx-deck). npm. Verified
    accessible 2026-09-04. (Package age and MIT license.)
[^43]: DeckDeckGo. (2024). [deckdeckgo](https://github.com/deckgo/deckdeckgo). GitHub.
    Verified accessible 2026-09-04. (Archived repository status.)
[^44]: Luterek, J. (2026). [astro-slides](https://github.com/jluterek/astro-slides). GitHub.
    Verified accessible 2026-09-04. (MCP/static/export claims and low adoption signals.)
[^45]: Cogapp Labs. (2026). [cogapp-slides-template](https://github.com/CogappLabs/cogapp-slides-template).
    GitHub. Verified accessible 2026-09-04. (Astro/MDX template, local fonts, Playwright PDF,
    and adoption signals.)
[^46]: Microsoft. (2026). [Page](https://playwright.dev/docs/api/class-page). Playwright
    Documentation. Verified accessible 2026-09-04. (Initialization scripts, screenshots, PDF,
    and discouraged `networkidle` readiness.)
[^47]: Lu, Z., Zhao, W., Yang, J., Tang, Q., Zhang, L., & Zhang, Y. (2026). [ReDeck](https://arxiv.org/abs/2609.00194).
    arXiv. Verified accessible 2026-09-04. (Fresh preprint; render-feedback loop and reported
    evaluation gains.)
[^48]: ai-harness. (2026). [skills/presentation/SKILL.md](https://github.com/Example User/ai-harness/blob/99f2d842f010ff473cd5998031272cdb5de701af/skills/presentation/SKILL.md).
    Private repository file at commit 99f2d842f01, read from a local checkout 2026-10-21. (Slidev-only skill contract.)
[^49]: ai-harness. (2026). [docs/designs/aih-y0bag-presentation-authoring-skill.md](https://github.com/Example User/ai-harness/blob/99f2d842f010ff473cd5998031272cdb5de701af/docs/designs/aih-y0bag-presentation-authoring-skill.md).
    Private repository file at commit 99f2d842f01, read from a local checkout 2026-10-21. (D-14 records the unmerged multi-framework commit and the measurement that no ref to it is reachable from this host; the file at this commit predates that revision, which lands with this refine.)
[^50]: ai-harness. (2026). [skills/presentation/scripts/critic](https://github.com/Example User/ai-harness/blob/99f2d842f010ff473cd5998031272cdb5de701af/skills/presentation/scripts/critic).
    Private repository file at commit 99f2d842f01, read from a local checkout 2026-10-21. (Attachments and critic prompt.)
[^51]: ai-harness. (2026). [skills/presentation/scripts/inspect_dom.cjs](https://github.com/Example User/ai-harness/blob/99f2d842f010ff473cd5998031272cdb5de701af/skills/presentation/scripts/inspect_dom.cjs).
    Private repository file at commit 99f2d842f01, read from a local checkout 2026-10-21. (Lint DOM inspector.)
[^52]: ai-harness. (2026). [skills/archify/bin/visual-check.mjs](https://github.com/Example User/ai-harness/blob/99f2d842f010ff473cd5998031272cdb5de701af/skills/archify/bin/visual-check.mjs).
    Private repository file at commit 99f2d842f01, read from a local checkout 2026-10-21. (Archify real-browser check and CDP capture; Archify tokens in skills/archify/explainer/reader.css.)
[^53]: ai-harness. (2026). [skills/_shared/doc-render/README.md](https://github.com/Example User/ai-harness/blob/99f2d842f010ff473cd5998031272cdb5de701af/skills/_shared/doc-render/README.md).
    Private repository file at commit 99f2d842f01, read from a local checkout 2026-10-21. (Doc renderer outputs, screenshots and receipt; its pyproject.toml and css/harness-default.css.)
[^54]: ai-harness. (2026). [skills/graphic/SKILL.md](https://github.com/Example User/ai-harness/blob/99f2d842f010ff473cd5998031272cdb5de701af/skills/graphic/SKILL.md).
    Private repository file at commit 99f2d842f01, read from a local checkout 2026-10-21. (Constrained SVG route.)
[^55]: ai-harness. (2026). [docs/procedures/cdp-browser-automation.md](https://github.com/Example User/ai-harness/blob/99f2d842f010ff473cd5998031272cdb5de701af/docs/procedures/cdp-browser-automation.md).
    Private repository file at commit 99f2d842f01, read from a local checkout 2026-10-21. (Attached-browser CDP route and its constraints.)
[^56]: ai-harness. (2026). [docs/procedures/diagram-render-and-vision-review.md](https://github.com/Example User/ai-harness/blob/99f2d842f010ff473cd5998031272cdb5de701af/docs/procedures/diagram-render-and-vision-review.md).
    Private repository file at commit 99f2d842f01, read from a local checkout 2026-10-21. (Measure-before-acting rule and non-author review.)
[^57]: ai-harness. (2026). [docs/procedures/visual-doc-quality-rubric.md](https://github.com/Example User/ai-harness/blob/99f2d842f010ff473cd5998031272cdb5de701af/docs/procedures/visual-doc-quality-rubric.md).
    Private repository file at commit 99f2d842f01, read from a local checkout 2026-10-21. (Nine criteria and the recording table.)
[^58]: ai-harness. (2026). [docs/presentation/decks/ai-harness-engineering/examples/AI_Harness_Engineering.html](https://github.com/Example User/ai-harness/blob/99f2d842f010ff473cd5998031272cdb5de701af/docs/presentation/decks/ai-harness-engineering/examples/AI_Harness_Engineering.html).
    Private repository file at commit 99f2d842f01, read from a local checkout 2026-10-21. (Reference deck.)
[^59]: ai-harness. (2026). [skills/presentation/package.json](https://github.com/Example User/ai-harness/blob/99f2d842f010ff473cd5998031272cdb5de701af/skills/presentation/package.json).
    Private repository file at commit 99f2d842f01, read from a local checkout 2026-10-21. (Pinned Slidev and Playwright versions.)
[^60]: ai-harness. (2026). [docs/designs/aih-y0bag-presentation-authoring-skill.md](https://github.com/Example User/ai-harness/blob/99f2d842f010ff473cd5998031272cdb5de701af/docs/designs/aih-y0bag-presentation-authoring-skill.md).
    Private repository file at commit 99f2d842f01, read from a local checkout 2026-10-21. (Version 1 design and its implementation audit table; rubric items in skills/presentation/scripts/qa_support.py.)
[^61]: npm. (2026). [@revealjs/react 0.2.1 registry record](https://registry.npmjs.org/@revealjs%2freact/0.2.1). Verified accessible (HTTP 200) 2026-10-21.
[^62]: npm. (2026). [reveal.js 6.0.2 registry record](https://registry.npmjs.org/reveal.js/6.0.2). Verified accessible (HTTP 200) 2026-10-21.
[^63]: npm. (2026). [React 19.3.1 registry record](https://registry.npmjs.org/react/19.3.1). Verified accessible (HTTP 200) 2026-10-21.
[^64]: npm. (2026). [React DOM 19.3.1 registry record](https://registry.npmjs.org/react-dom/19.3.1). Verified accessible (HTTP 200) 2026-10-21.
[^65]: npm. (2026). [Vite 8.3.4 registry record](https://registry.npmjs.org/vite/8.3.4). Verified accessible (HTTP 200) 2026-10-21.
[^66]: Reveal.js. (2026). [React integration README](https://github.com/hakimel/reveal.js/blob/master/react/README.md). GitHub. Verified accessible (HTTP 200) 2026-10-21.
[^67]: npm. (2026). [@revealjs/react weekly downloads](https://api.npmjs.org/downloads/point/last-week/%40revealjs%2Freact). Verified accessible (HTTP 200) 2026-10-21.
[^68]: GitHub. (2026). [hakimel/reveal.js repository metadata](https://api.github.com/repos/hakimel/reveal.js). Verified accessible (HTTP 200) 2026-10-21.
[^69]: Reveal.js. (2026). [Events](https://revealjs.com/events/). Verified accessible (HTTP 200) 2026-10-21.
[^70]: Reveal.js. (2026). [JavaScript API](https://revealjs.com/api/). Verified accessible (HTTP 200) 2026-10-21.
[^71]: Reveal.js. (2026). [Deep linking](https://revealjs.com/deep-linking/). Verified accessible (HTTP 200) 2026-10-21.
[^72]: Vite. (2026). [Shared option: base](https://vite.dev/config/shared-options.html#base). Verified accessible (HTTP 200) 2026-10-21.
[^73]: Vite. (2026). [Static asset handling](https://vite.dev/guide/assets.html). Verified accessible (HTTP 200) 2026-10-21.
[^74]: Reveal.js. (2026). [PDF export](https://revealjs.com/pdf-export/). Verified accessible (HTTP 200) 2026-10-21.
[^75]: Microsoft. (2026). [Playwright `page.pdf`](https://playwright.dev/docs/api/class-page#page-pdf). Verified accessible (HTTP 200) 2026-10-21.
[^76]: DeckTape. (2026). [Releases](https://github.com/astefanutti/decktape/releases). GitHub. Verified accessible (HTTP 200) 2026-10-21.
[^77]: Reveal.js. (2026). [Speaker view](https://revealjs.com/speaker-view/). Verified accessible (HTTP 200) 2026-10-21.
[^78]: Reveal.js. (2026). [Presentation size](https://revealjs.com/presentation-size/). Verified accessible (HTTP 200) 2026-10-21.
[^79]: MDN. (2026). [`FontFaceSet.ready`](https://developer.mozilla.org/en-US/docs/Web/API/FontFaceSet/ready). Verified accessible (HTTP 200) 2026-10-21.
[^80]: Microsoft. (2026). [Playwright Clock](https://playwright.dev/docs/clock). Verified accessible (HTTP 200) 2026-10-21.
[^81]: Microsoft. (2026). [Playwright visual comparisons](https://playwright.dev/docs/test-snapshots). Verified accessible (HTTP 200) 2026-10-21.
[^82]: Microsoft. (2026). [Playwright actionability](https://playwright.dev/docs/actionability). Verified accessible (HTTP 200) 2026-10-21.
[^83]: MDN. (2026). [`Document.elementFromPoint()`](https://developer.mozilla.org/en-US/docs/Web/API/Document/elementFromPoint). Verified accessible (HTTP 200) 2026-10-21.
[^84]: Microsoft. (2026). [Playwright Page events](https://playwright.dev/docs/events). Verified accessible (HTTP 200) 2026-10-21.
[^85]: npm. (2026). [@axe-core/playwright 4.13.0 registry record](https://registry.npmjs.org/@axe-core%2fplaywright/4.13.0). Verified accessible (HTTP 200) 2026-10-21.
[^86]: Microsoft. (2026). [Accessibility testing with Playwright](https://playwright.dev/docs/accessibility-testing). Verified accessible (HTTP 200) 2026-10-21.
[^87]: Deque Systems. (2026). [ARIA hidden focus rule](https://dequeuniversity.com/rules/axe/4.13/aria-hidden-focus). Verified accessible (HTTP 200) 2026-10-21.
[^88]: Xiang, et al. (2026). [UI-Lens](https://openaccess.thecvf.com/content/CVPR2026/html/Xiang_UI-Lens_Assessing_General_MLLMs_Potential_to_Automate_UI_Display_Quality_CVPR_2026_paper.html). CVPR. Verified accessible (HTTP 200) 2026-10-21.
[^89]: Reveal.js. (2026). [Project package configuration](https://raw.githubusercontent.com/hakimel/reveal.js/master/package.json). Verified accessible (HTTP 200) 2026-10-21.
[^90]: Slidev. (2026). [Project package configuration](https://raw.githubusercontent.com/slidevjs/slidev/main/package.json). Verified accessible (HTTP 200) 2026-10-21.
[^91]: Formidable. (2026). [Spectacle package configuration](https://raw.githubusercontent.com/FormidableLabs/spectacle/main/package.json). Verified accessible (HTTP 200) 2026-10-21.
[^92]: Microsoft. (2026). [Change PowerPoint slide size](https://support.microsoft.com/en-us/powerpoint/change-the-size-of-your-powerpoint-slides). Verified accessible (HTTP 200) 2026-10-21.
[^93]: National Disability Rights Network. (2026). [Accessibility guidelines](https://www.ndrn.org/accessibility-guidelines/). Verified accessible (HTTP 200) 2026-10-21.
[^94]: Office for National Statistics. (2026). [Creating PowerPoint presentations](https://service-manual.ons.gov.uk/brand-guidelines/application/creating-powerpoint-presentations). Verified accessible (HTTP 200) 2026-10-21.
[^95]: W3C. (2026). [Understanding animation from interactions](https://www.w3.org/WAI/WCAG21/Understanding/animation-from-interactions.html). Verified accessible (HTTP 200) 2026-10-21.
[^96]: Reveal.js. (2026). [Fragments](https://revealjs.com/fragments/). Verified accessible (HTTP 200) 2026-10-21.
[^97]: npm. (2026). [@vitejs/plugin-react 6.0.5 registry record](https://registry.npmjs.org/@vitejs%2fplugin-react/6.0.5). Verified accessible (HTTP 200) 2026-10-21.
[^98]: npm. (2026). [@playwright/test 1.58.2 registry record](https://registry.npmjs.org/@playwright%2ftest/1.58.2). Verified accessible (HTTP 200) 2026-10-21.
[^99]: npm. (2026). [playwright-chromium 1.58.2 registry record](https://registry.npmjs.org/playwright-chromium/1.58.2). Verified accessible (HTTP 200) 2026-10-21.

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** ISSUE-REF
**Model:** OpenAI Codex GPT-5
**Date:** 2026-09-01

````text
## FOUNDING ASK — fidelity tier 1

verbatim_exact_ask:
```text
i need to do a presentation on ai harness engineering. can you /delegate /codex /research-doc to see what are the best slide / presentation tools that are ai native or that ai are best and using and developing legible and clean and well formatted and attractive slide presentations? also you have have it explore other types of non-slide presentations as well. but i need to present this to a team on friday around noon EST. and i need an outline or an initial draft by tomorrow around 1 PM EST. so run that research agent and make sure to use canonical research prompt stub and do a breadth and depth survey on latest tools and techniques and packages and plugins and harnesses etc for using so agentic like claude code and codex to build presentations. and make sure to anchor on the latest online research and findings and sources etc as of 2026 and september 1st 2026 in particular. explore this whole space and then we can proceed to building the presentation and also building the presentation harness (skills and context files and other /update-harness changes). anything and everything that's relevant.
```

## STAGE FRAMING — non-authoritative

You are researching for ISSUE-REF: a breadth-and-depth survey of
AI-native / AI-best presentation and slide-deck generation tooling for agentic coding tools
(Claude Code, Codex), for example-user Example User's ai-harness engineering team presentation this Friday.

### Task

Write the research findings directly into the existing stub document at:
docs/research/ai-native-presentation-tooling.md

That file is already scaffolded from the canonical research STUB (template_version
research-1.2.0, region markers intact, frontmatter present). Fill in the `body` region and every
other required section by copying the structure of the canonical TEMPLATE at
docs/research/TEMPLATE.md (read it first) — Temporal Scope, Executive Summary (3-5 numbered
findings), numbered topic sections with `### When to use` subsections, Comparison table,
Recommendation, Open Questions, Sources (GFM footnote definitions in APA form), the completed
Appendix: Research Prompt (paste this entire prompt verbatim into its fenced text block, with
Model = the exact concrete model/tool you are, Date = today), and the Appendix: Provenance
Ledger (claim -> source URL -> verbatim quote -> verdict -> live? for every [VERIFIABLE] claim).
Do not touch the `context` region (already frozen). Conform exactly to research-1.2.0 structure —
region markers, heading order, footnote-style citations — this document must pass
`aido validate-doc` and a citation audit.

### Deadline context (why this matters and how it should shape the doc)

example-user needs an outline/initial draft of the actual presentation by tomorrow ~1pm EST
(2026-09-03), and delivers the presentation to his engineering team Friday ~noon EST
(2026-09-04). This research doc is read immediately afterward by example-user (and a Claude Code
session) to build both the presentation itself and a reusable presentation-authoring
skill/harness. Prioritize actionable, comparison-ready findings (what to actually use, today,
for this specific job) over pure landscape cataloguing — but do not sacrifice breadth to get
there; the Executive Summary and Recommendation sections are where the actionable synthesis
belongs, and the topic sections should still cover the space thoroughly.

### Role identity (lead your grounding with this)

You are a principal developer-experience engineer who has spent the last two years building and
evaluating AI-native authoring tooling — the kind of engineer who has personally scripted
slide-deck generation from Markdown, wired an LLM agent to a presentation-rendering pipeline, and
sat through the resulting decks in front of a real engineering audience. You have strong,
evidence-backed opinions about which tools actually produce legible, well-formatted, attractive
output versus which ones produce slop, and you say so explicitly when you cannot find a source.

<grounding_instructions>
You are a principal developer-experience engineer who has spent the last two years building and
evaluating AI-native authoring tooling — the kind of engineer who has personally scripted
slide-deck generation from Markdown, wired an LLM agent to a presentation-rendering pipeline, and
sat through the resulting decks in front of a real engineering audience. You have strong,
evidence-backed opinions about which tools actually produce legible, well-formatted, attractive
output versus which ones produce slop, and you say so explicitly when you cannot find a source.

Temporal scope: Weight sources by recency — 2026 (primary) → 2025 → 2024.
Pre-2024 sources are background context only unless foundational to the topic.
If post-2024 literature is genuinely sparse for a subtopic, state
"[subtopic]: no significant post-2024 developments found" rather than
backfilling with older sources. Backfilling is a failure mode, not a hedge.
Weight September 2026 and August 2026 sources most heavily where available — this survey is
being conducted on 2026-09-02 and must reflect the current state of the art, not a snapshot from
earlier in the year.

Before generating your final output, execute a Chain-of-Verification (CoVe)
to ensure factual fidelity over compliance.

Inside your thought process:
1. Isolate the core facts required.
2. Draft a tentative response.
3. Hostile Cross-Examination: flag any claim where you are citing a source because
   the prompt implied you should, rather than because you verified it.
4. Strip away any claim that cannot be empirically verified.

When generating your final output, classify every major claim. Write your rationale
before appending the tag — writing the tag first causes post-hoc rationalization.
Rationale → evidence check → tag.

- [VERIFIABLE]: backed by documentation, peer-reviewed research, or official
  tech blogs (2024–2026). Carry an inline footnote ref to the source: [VERIFIABLE][^N].
- [HEURISTIC]: widely accepted best practice without a specific citation.
- [INFERENCE]: a logical conclusion drawn from context. Provide your reasoning
  in-text. Do not fabricate a source. Tier tag only — NO footnote ref.
- [NO SOURCE]: explicitly state when you cannot find verifiable data. Tier tag
  only — NO footnote ref.

Citation format (mandatory for every externally-sourced claim):
- Inline: append the GFM footnote ref directly after the tier tag — [VERIFIABLE][^N].
  A claim citing multiple sources carries ascending separate refs — [VERIFIABLE][^3][^7]
  (never grouped [^3,^7]; never out of order).
- Footnote definitions live once, under ## Sources, in APA form with a clickable
  URL or DOI and an access-verification stamp. Worked example:
    [^1]: LangChain. (2026). [Threads](https://docs.langchain.com/langsmith/threads).
    LangSmith Documentation. Verified accessible (HTTP 200) 2026-06-03. (Scope note.)
- URL-or-DOI ALWAYS: every source entry carries a clickable URL or a doi.org link —
  paywalled/gated is fine (link it anyway; stamp the access status). Only the
  truly-irreducible case (no online catalog presence anywhere) gets an explicit
  [no online source located] marker with a one-line justification.
- Integrity: footnote refs are contiguous from [^1], every [^N] ref has a matching
  definition, and every definition is referenced — no gaps, no orphans.
- [INFERENCE] / [NO SOURCE] claims carry the tier tag with NO footnote ref.

Hard constraint (overrides all formatting preferences): never invent a citation
to satisfy a formatting instruction. Accuracy > completeness.

Format diagrams using Mermaid.js or ASCII. Format math using LaTeX.
NEVER generate binary images.
</grounding_instructions>

### Scope note — questions, examples, and named references are a starting point, not a checklist
The questions, topics, and named examples below are illustrative anchors and a FLOOR for this
research — not an exhaustive list to answer only or evaluate only. Reason independently: survey the
landscape broadly, follow the evidence where it leads, expand scope where warranted, and surface
relevant work, factors, and failure modes not named here. Actively resist answering only the listed
questions or evaluating only the named approaches — an output that merely fills in the listed items
has NOT met the research goal.

### Independent exploration (gaps, blindspots, emergent threads) — required
Treat the question list as a FLOOR, not a ceiling. As you research, actively surface what this
framing may be missing and pursue each promising thread to a logical conclusion:
- Adjacent or upstream factors the questions don't capture.
- Contrarian / disconfirming evidence — report it even when it challenges the premise.
- Emerging 2025–2026 practices, tools, or research not anchored by the named examples.
- Known failure modes and second-order effects.
Whenever a load-bearing thread surfaces mid-research, follow it to its conclusion and report it in a
dedicated "Gaps, blindspots & emergent findings" subsection. Explicitly NAME any blindspot you
suspect but cannot resolve (and why) rather than omitting it. Anchor bias — over-fitting to the
listed questions and example approaches — is a known failure mode; counter it deliberately and say
where you did.

### Research questions (floor, not ceiling)

#### A. Slide-deck-specific tools

1. What are the leading AI-native or AI-best slide-deck generation tools as of September 2026 —
   both dedicated AI-presentation products (e.g. Gamma, Tome, Beautiful.ai, Plus AI, Decktopus,
   SlidesAI, Presentations.ai, Slidebean, and any newer 2026 entrants) and general-purpose
   AI-coding-adjacent approaches (Markdown-to-slides pipelines an LLM agent can drive: Marp,
   Slidev, reveal.js + Pandoc, remark.js, Deckset)? For each, assess: output legibility/visual
   quality, how well an agentic coding tool (Claude Code, Codex) can actually drive it
   programmatically (CLI/API/file-format friendliness vs GUI-only), theming/branding
   customization, and any AI-generation quality complaints or praise from real 2025-2026 usage.
2. Which of these tools or formats are explicitly designed or marketed for LLM/agent-driven
   generation (i.e. the tool's own docs or roadmap discuss AI/agent authoring, not just
   AI-assisted editing by a human in a GUI)?
3. What plugins, MCP servers, CLI wrappers, or skills/extensions exist specifically for having
   Claude Code or Codex (or comparable coding agents — Cursor, Cline, Aider, etc.) generate slide
   decks end-to-end — from a prompt/outline to a rendered, presentable deck?
4. What do engineers and DevRel/technical-presentation practitioners report as best practice for
   getting clean, professional, non-"AI-slop-looking" output from an ISSUE-REF deck pipeline
   (design system constraints, template discipline, iteration loops, human-in-the-loop review
   points)?

#### B. Non-slide presentation formats

5. What are the leading non-slide presentation formats/tools an agentic coding tool could
   generate well — e.g. single-scrolling-page web presentations, Jupyter/notebook-style
   narrated walkthroughs, terminal-based presentation tools (e.g. slides in the terminal,
   present-mode CLIs), live-coding-demo formats, interactive data-viz-driven presentations, or
   video/narrated-screen-recording pipelines an agent could script?
6. How do these compare to slide decks for an engineering-team technical presentation
   specifically (audience: software/AI-harness engineers) — legibility, information density,
   ability to show code/diagrams/live examples, and how amenable each is to AI-native
   generation?

#### C. Agentic-generation techniques and harnesses

7. What techniques exist for getting an agentic coding tool to produce well-formatted, legible,
   attractive output specifically (not generic "AI slop") — design-system/style-guide injection,
   iterative visual-review loops (e.g. rendering + screenshot + critique), token/design-system
   constraints, exemplar-based few-shot prompting, or dedicated "designer" sub-agent patterns?
8. Are there existing Claude Code skills, MCP servers, Codex wrappers, or open-source
   repos/frameworks (e.g. anything analogous to this fleet's own skill/harness pattern) built
   specifically to scaffold and author presentations end-to-end with an agentic coding tool?
9. What packaging/rendering pipeline choices matter for an agent-authored deck to look
   professional out of the box — theming frameworks, design tokens, font/color-system defaults,
   accessibility/contrast considerations, export formats (PDF/PPTX/HTML/web), and how each
   constrains or enables agentic generation?

#### D. Practical fit for this specific use case

10. Given the constraint that the author (example-user) is working inside Claude Code / Codex sessions
    already, wants an outline/draft within ~24 hours and a finished deck within ~3 days, and
    wants to build a reusable skill/harness afterward — what is the most practical, evidence-backed
    recommendation for tool + workflow? Consider iteration speed, output quality, and how well the
    chosen approach could be wrapped into a repeatable Claude Code skill for future presentations.

### Required appendix format (fill this into the doc, not just this prompt)

```markdown
## Appendix: Research Prompt

**Registry ID:** ISSUE-REF
**Model:** [model used]
**Date:** YYYY-MM-DD

​```text
[Full prompt text here]
​```
```

## ALIGNMENT OBLIGATION

Preserve the exact ask and surface any conflict.
````

## Appendix: Research Prompt — Interactive React/HTML Refine

**Registry ID:** ISSUE-REF
**Model:** OpenAI Codex GPT-5
**Date:** 2026-09-04

````text
## FOUNDING ASK — fidelity tier 1

verbatim_exact_ask:
```text
in fact, if we can just make it an interactive html or react slides etc. there are open source packages for this. this is what i just googled real quick. so go aheada nd /delegate to /codex to /research-doc this and dont just research what google ai summary showed up. [...] here's the google ai summary as a basis but it should expore and build on this and breadth and depth survey etc as of september 4th 2026: [Google AI summary about Bolt Slides, Spectacle, Reveal.js React wrapper, React Flow, Recharts, custom React+Tailwind slide decks -- full text supplied separately below]
```

## STAGE FRAMING — non-authoritative

You are stage 1 of a larger chain: research first, then a harness-update implements whichever
framework this research recommends, then the deck gets rebuilt on it. You are ONLY the research
stage — do not write code, do not touch skills/presentation/, only write the research doc.

## ALIGNMENT OBLIGATION

The founding ask explicitly says "dont just research what google ai summary showed up" — the
pasted summary is a floor/starting point, not the ceiling. Independently verify every claim in it
(Bolt Slides' actual repo/maturity, Spectacle's current state, Reveal.js's React wrapper), and go
well beyond it: breadth+depth survey of the interactive/React/HTML presentation landscape as of
2026-09-04, including alternatives the summary didn't mention.

---

## Background — read before researching, do not re-derive

This is a **`--refine` run** of the existing doc `docs/research/ai-native-presentation-tooling.md`
(ISSUE-REF, completed 2026-09-02). That doc already surveyed AI-native/agentic presentation
tooling broadly and has a section on Reveal.js/Quarto/Pandoc/remark/Deckset (concluding Quarto is
generally the stronger choice, raw Reveal.js only for narrow cases) — assume that prior finding is
established; do not re-derive it. It does **not** cover: interactive React-component-based slide
frameworks specifically, StackBlitz Bolt Slides (a 2026-era release, likely too recent for the
2026-09-02 research), Spectacle, a Reveal.js **React wrapper** (distinct from raw Reveal.js), React
Flow, Recharts, MDX Deck, or a from-scratch React+Tailwind custom-deck approach. This run's job is
to add that missing section, informed by a concrete new driver:

**Concrete driver for this research (read in full, it is the actual motivating context):**
example-user is presenting on AI-harness engineering to his team today (2026-09-04). His own harness's
`/presentation` skill (Slidev-based) has now produced two draft rounds; he judges both "incredibly
mid" compared to a reference deck a colleague one-shotted (a single self-contained HTML file with
inline CSS/SVG/JS: dark theme, card/grid/comparison compositions, keyboard+touch navigation,
progress chrome — read it yourself at
`docs/presentation/decks/ai-harness-engineering/examples/AI_Harness_Engineering.html`, now
committed to this repo). Separately, an unrelated harness defect was just discovered: the `cx`
Codex-delegation wrapper never exposed `codex exec`'s native `-i/--image` flag, so no prior
dispatch — including the Slidev deck's own "visual critic" role — ever actually looked at rendered
pixels; QA judgments were derived purely from DOM-extracted text/geometry facts. That gap is being
fixed in a separate, parallel harness-update track (add real screenshot/vision capability to `cx`)
and is **out of scope for this research** — do not investigate or recommend fixes to the `cx`
wrapper itself, only research the presentation *framework/rendering* question.

## Objective

Add a new, thoroughly-researched section to `docs/research/ai-native-presentation-tooling.md`
covering **interactive, React/HTML-native presentation frameworks and construction approaches**,
to inform a near-term decision: should the `/presentation` skill/harness adopt one of these
instead of (or alongside) Slidev for its next deck build?

## Scope note — questions, examples, and named references are a starting point, not a checklist

The questions, topics, and named examples below are illustrative anchors and a FLOOR for this
research — not an exhaustive list to answer only or evaluate only. Reason independently: survey the
landscape broadly, follow the evidence where it leads, expand scope where warranted, and surface
relevant work, factors, and failure modes not named here. Actively resist answering only the listed
questions or evaluating only the named approaches — an output that merely fills in the listed items
has NOT met the research goal.

**The Google AI-summary example-user pasted verbatim (a floor, independently verify every claim, do not
trust it as-is):**

```text
To build an interactive React slide deck, you can either leverage an open-source framework or
build a custom state-driven carousel from scratch.

Method 1: Use Open-Source Frameworks (Recommended)
Option A: Bolt Slides (Best for Modern AI & Rich Components) -- Released by StackBlitz, Bolt
Slides is a modern presentation framework built on Vite and React. Every slide is a live web
component. Features agent-native prompting (feed it to Claude Code or Cursor to build decks from
a text prompt) and includes 30+ interactive components (responsive charts, 3D scenes,
calculators, motion layouts).
Option B: Spectacle or Reveal.js for React -- Formidable's Spectacle, or the official React
wrapper for Reveal.js, letting you handle slide lifecycles programmatically.

Method 2: Building a Custom Slide Deck from Scratch -- React state management + Tailwind CSS.
Define a slide-data array, add keyboard event listeners for arrow-key navigation, embed live
Recharts charts, stateful widgets, React Flow diagrams.

Sources: [1] StackBlitz Bolt Slides announcement/AlphaSignal; [2] github.com/stackblitz/bolt-slides;
[3] revealjs.com/react; [4] lovable.dev/templates/apps/presentations/lovable-slides;
[5] reactflow.dev/learn/tutorials/slide-shows-with-react-flow
```

Every URL above must be live-fetched and verified by you, not assumed real from the summary. The
summary itself says "AI responses may include mistakes" — treat that warning literally.

## Independent exploration (gaps, blindspots, emergent threads) — required

Treat the question list as a FLOOR, not a ceiling. As you research, actively surface what this
framing may be missing and pursue each promising thread to a logical conclusion:
- Adjacent or upstream factors the questions don't capture.
- Contrarian / disconfirming evidence — report it even when it challenges the premise.
- Emerging 2025–2026 practices, tools, or research not anchored by the named examples.
- Known failure modes and second-order effects.

Specific angles to pursue (a floor, not exhaustive):
1. **StackBlitz Bolt Slides**: actual repo maturity (stars, commit recency, issue activity,
   license), what "agent-native prompting" concretely means and whether Claude Code / Codex can
   drive it non-interactively (headless/CLI usage, not just its own chat UI), how it renders to a
   shareable static artifact (does it need a running Vite dev server, or does it export to static
   HTML/a deployable bundle the way Slidev does), and what its actual visual output quality looks
   like (find real example decks/screenshots, not just marketing copy).
2. **Spectacle** (Formidable) vs the **official Reveal.js React wrapper**: current maintenance
   status in 2026, React 18/19 compatibility, theming depth, whether either can render offline
   without a build step or CDN dependency (a hard constraint from the existing doc's "no external
   font/CDN" finding — verify this constraint still applies and what each framework requires).
3. **Custom React+Tailwind from scratch**: realistic effort/complexity for a governance-heavy use
   case like this harness (needs manifest-driven slide generation, deterministic rendering for
   PNG/PDF export via something like Playwright, and reproducible offline builds) — is a
   from-scratch approach actually *less* work than adapting an existing framework, given the
   harness's existing QA/lint/theme-token contract?
4. **How would any of these integrate with a headless render/screenshot/PDF-export pipeline**
   (the harness's existing model: `render` → PNG/PDF export via Playwright, `lint` → DOM
   inspection, `critic` → visual review) — which frameworks make this straightforward (static
   HTML+JS output, inspectable DOM) vs which fight it (client-only SPA requiring a live server,
   non-deterministic animation timing)?
5. **Slidev's own interactivity ceiling** — Slidev (the harness's CURRENT renderer) is itself
   Vue-based and supports custom Vue components, embedded interactive widgets, and click
   animations. Before recommending a wholesale framework switch, assess honestly: how much of
   what example-user wants (real interactivity, richer visual variety, a genuinely more "impressive"
   feel) is achievable by using Slidev's *existing* component system properly (which the harness
   has so far barely used — round 2's Vue layout components were static, not interactive) versus
   requiring a different underlying framework entirely? This is a legitimate "do we even need to
   switch" angle the summary doesn't raise at all.
6. **Licensing / attribution** for any framework or example code referenced, given the harness
   ships this content in a repo example-user may open-source parts of later.
7. **AI-agent-authoring fit** specifically for Claude Code / Codex (`cx`) driving one of these
   frameworks non-interactively from a manifest/brief — which is the actual downstream use case,
   not a human hand-authoring slides in an editor.

## <grounding_instructions>

You are a staff-level frontend/presentation-tooling engineer who has shipped both static-site-
generator-based slide decks (Slidev, reveal.js, Marp) and custom React/Vite web-app presentation
tools in production, and who has personally evaluated whether a component-framework switch is
worth its integration cost against an existing CI/QA pipeline. You have strong opinions backed by
evidence. When you cannot find a source, you say so explicitly.

Temporal scope: Weight sources by recency — 2026 (primary, especially September 2026) → 2025 →
2024. Pre-2024 sources are background context only unless foundational to the topic (e.g. Reveal.js
itself, which predates 2024, is foundational and may be cited for its maturity/history). If
post-2024 literature is genuinely sparse for a subtopic, state "[subtopic]: no significant
post-2024 developments found" rather than backfilling with older sources. Backfilling is a failure
mode, not a hedge.

Before generating your final output, execute a Chain-of-Verification (CoVe) to ensure factual
fidelity over compliance.

Inside your thought process:
1. Isolate the core facts required.
2. Draft a tentative response.
3. Hostile Cross-Examination: flag any claim where you are citing a source because the prompt
   implied you should, rather than because you verified it. This applies with extra force to
   every claim in the pasted Google AI summary above — verify, don't repeat.
4. Strip away any claim that cannot be empirically verified.

When generating your final output, classify every major claim. Write your rationale before
appending the tag — writing the tag first causes post-hoc rationalization. Rationale → evidence
check → tag.

- [VERIFIABLE]: backed by documentation, peer-reviewed research, or official tech blogs
  (2024-2026). Carry an inline footnote ref to the source: [VERIFIABLE][^N].
- [HEURISTIC]: widely accepted best practice without a specific citation.
- [INFERENCE]: a logical conclusion drawn from context. Provide your reasoning in-text. Do not
  fabricate a source. Tier tag only — NO footnote ref.
- [NO SOURCE]: explicitly state when you cannot find verifiable data. Tier tag only — NO footnote
  ref.

Citation format (mandatory for every externally-sourced claim):
- Inline: append the GFM footnote ref directly after the tier tag — [VERIFIABLE][^N]. A claim
  citing multiple sources carries ascending separate refs — [VERIFIABLE][^3][^7] (never grouped
  [^3,^7]; never out of order).
- Footnote definitions live once, under ## Sources, in APA form with a clickable URL or DOI and an
  access-verification stamp.
- URL-or-DOI ALWAYS: every source entry carries a clickable URL or a doi.org link — paywalled/gated
  is fine (link it anyway; stamp the access status). Only the truly-irreducible case (no online
  catalog presence anywhere) gets an explicit [no online source located] marker with a one-line
  justification.
- Integrity: footnote refs are contiguous from [^1] continuing this document's existing numbering
  (read the current doc's `## Sources` section first and continue its sequence — do not restart at
  [^1] and collide with existing definitions), every [^N] ref has a matching definition, and every
  definition is referenced — no gaps, no orphans.
- [INFERENCE] / [NO SOURCE] claims carry the tier tag with NO footnote ref.

Hard constraint (overrides all formatting preferences): never invent a citation to satisfy a
formatting instruction. Accuracy > completeness.

When evaluating an externally sourced idea, feature, repository, package, tool, or pattern for
adoption, apply `docs/procedures/source-vetting-and-corroboration.md`: record concrete
adoption/maintenance signals or independently corroborate the underlying pattern. If neither route
passes, call it exploratory rather than an established best practice.

**Retrieval enforcement (mandatory — this is a `--refine` run):** For each load-bearing claim,
make at least one `web_search` or `web_fetch` call; do not return `[VERIFIABLE]` without a URL you
actually fetched in this session. Do not stop after one round. If no source is found for a
sub-question, run additional searches across alternate framings before tagging it `[NO SOURCE]` —
that tag is reserved for genuine evidence absence, not retrieval avoidance. Every claim you intend
to tag `[VERIFIABLE]` about the pasted Google summary's specific claims (Bolt Slides' feature list,
Spectacle, the Reveal.js React wrapper) must be independently re-fetched from a primary source
(the actual repo, actual docs) — the summary's own text is not a citable source.

Format diagrams using Mermaid.js or ASCII. Format math using LaTeX. NEVER generate binary images.

</grounding_instructions>

## Definition of done

- New section(s) added to `docs/research/ai-native-presentation-tooling.md` covering interactive
  React/HTML presentation frameworks, integrated into the doc's existing structure (read the whole
  doc first — its ToC, Comparison table, Recommendation, and Sources sections all need updating to
  incorporate the new material coherently, not just appended as a disconnected block).
- The doc's `## Recommendation` section is updated (or a clearly-flagged addendum added) with an
  explicit, evidence-backed answer to: should the `/presentation` skill adopt a React-based
  framework for its next build, and if so which one — reasoned against the harness's actual
  constraints (offline/no-CDN rendering, deterministic PNG/PDF export via Playwright, DOM-inspectable
  output for lint/QA, manifest-driven non-interactive authoring by a Codex agent).
- `## Run History` gets a new entry for this refine pass.
- `## Appendix: Research Prompt` — a NEW entry appended (do not overwrite the existing one) per the
  canonical format, with this run's Registry ID, model, date, and this full prompt text in a fenced
  block.
- Run `aido validate-doc docs/research/ai-native-presentation-tooling.md` and
  `npx markdownlint-cli2 docs/research/ai-native-presentation-tooling.md` — both must pass.
- Spot-check note: state which 3-5 citations you re-opened and confirmed support their claim.

## Output contract

Your final message must be the raw `docs/research/ai-native-presentation-tooling.md` unified diff
per the relay's own delivery contract (this is a `--write-target` dispatch) — no prose wrapper, no
summary, no restating this brief. If the relay's own preamble asks for something else, follow the
relay's instructions over this line; this line exists only to say a capped conversational summary
is the WRONG shape for this call.
````

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

| # | Claim | Source URL | Verbatim quote | Verdict | Live? |
|---|---|---|---|---|---|
| 1 | Slidev directly supports coding agents with MCP and an official skill. | https://sli.dev/guide/work-with-ai | “Slidev has a built-in ✨ MCP Server that gives any MCP-capable agent structured tools to inspect, edit, reorder, and navigate your slides.” | SUPPORTED | HTTP 200, 2026-09-01 |
| 2 | Slidev exports PDF, PPTX, PNG, and Markdown. | https://sli.dev/guide/exporting | “you can also export them to PDF, PPTX, PNG, or Markdown files” | SUPPORTED | HTTP 200, 2026-09-01 |
| 3 | Marp uses CommonMark, supports CSS themes, and exports HTML/PDF/PPTX. | https://marp.app/ | “create your own Marp theme with plain CSS” / “convert your Markdown into HTML, PDF, PowerPoint” | SUPPORTED | HTTP 200, 2026-09-01 |
| 4 | PptxGenJS produces compatible OOXML presentations from JavaScript. | https://gitbrent.github.io/PptxGenJS/docs/introduction/ | “The library outputs standards-compliant Open Office XML (OOXML) files” | SUPPORTED | HTTP 200, 2026-09-01 |
| 5 | PptxGenJS supports programmatic slide masters for branding. | https://gitbrent.github.io/PptxGenJS/docs/masters.html | “PptxGenJS allows you to define Slide Master Layouts” | SUPPORTED | HTTP 200, 2026-09-01 |
| 6 | Gamma exposes programmatic generation, themes, and exports. | https://developers.gamma.app/ | “generate presentations, documents, websites, and social posts programmatically” | SUPPORTED | HTTP 200, 2026-09-01 |
| 7 | Gamma’s current API creates but does not edit existing artifacts. | https://developers.gamma.app/reference/common-feature-requests | “The API creates gammas but does not modify existing ones.” | SUPPORTED | HTTP 200, 2026-09-01 |
| 8 | Plus AI MCP creates and iteratively revises native PowerPoint. | https://plusai.com/features/mcp | “Let your AI agents and dev tools create — and keep improving — native PowerPoint and Google Slides presentations” | SUPPORTED | HTTP 200, 2026-09-01 |
| 9 | Beautiful.ai exposes an early-access prompt-to-deck API and PPTX/PDF export. | https://support.beautiful.ai/hc/en-us/articles/43654071102605-Beautiful-ai-API | “prompt-to-deck endpoint” / “export to PPTX or PDF” | SUPPORTED | HTTP 200, 2026-09-01 |
| 10 | Canva AI is a 2026 research-preview agentic design system with editable output. | https://www.canva.com/newsroom/news/canva-create-2026-ai/ | “Available today as a research preview” / “fully layered, editable output” | SUPPORTED | HTTP 200, 2026-09-01 |
| 11 | Tome Slides was sunset and became inaccessible in April 2025. | https://tome.app/help/en/articles/10744234-tome-slides-is-sunsetting-on-april-30th-2025 | “After April 30th, users will not be able to sign in or access any Tome content.” | SUPPORTED | HTTP 200, 2026-09-01 |
| 12 | Quarto Reveal supports notes, themes, and executable code. | https://quarto.org/docs/presentations/revealjs/ | “speaker notes” / “There are 11 built-in themes” / “output of executable code blocks” | SUPPORTED | HTTP 200, 2026-09-01 |
| 13 | Observable Framework is a code-first static data-app generator with reactive JavaScript and polyglot data loaders. | https://observablehq.com/framework/ | “static site generator for data apps” / “reactive JavaScript” / “data loaders in any programming language” | SUPPORTED | HTTP 200, 2026-09-01 |
| 14 | Presenterm runs Markdown presentations in a terminal and supports PDF export. | https://mfontanini.github.io/presenterm/ | “run them from your terminal” / “exporting presentations into PDF format” | SUPPORTED | HTTP 200, 2026-09-01 |
| 15 | Claude supports built-in and custom presentation skills. | https://github.com/anthropics/claude-cookbooks/blob/main/skills/README.md | “built-in pptx, xlsx, docx, and pdf Skills” / “Branded presentation generation” | SUPPORTED | HTTP 200, 2026-09-01 |
| 16 | Microsoft recommends at least 18-point sans-serif text and sufficient white space. | https://support.microsoft.com/en-us/accessibility/powerpoint/make-your-powerpoint-presentations-accessible-to-people-with-disabilities | “Use 18pt or larger sans serif fonts.” / “Include sufficient white space between sentences and paragraphs.” | SUPPORTED | HTTP 200, 2026-09-01 |
| 17 | WCAG 2.2 AA uses 4.5:1 normal-text and 3:1 large-text contrast thresholds. | https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html | “at least 4.5:1” / “Large-scale text ... at least 3:1” | SUPPORTED | HTTP 200, 2026-09-01 |
| 18 | A 2026 study found coding assistants strongest in a narrow educational test but human editing remained material. | https://arxiv.org/abs/2605.13532 | “coding assistant tools produce slides that were most accurate and usable” / “selectively deleting content” | SUPPORTED | HTTP 200, 2026-09-01 |
| 19 | DeepSlides separates design from implementation and reports stronger human preference. | https://arxiv.org/abs/2605.26451 | “decoupling presentation design from content implementation” / “superior performance in human preference evaluations” | SUPPORTED | HTTP 200, 2026-09-01 |
| 20 | SlideRL uses staged generation and a six-dimension evaluation protocol. | https://arxiv.org/abs/2603.16839 | “research, planning, generation, refinement, and finalization” / “combining six quality dimensions” | SUPPORTED | HTTP 200, 2026-09-01 |

| 21 | Bolt Slides is a React/Vite deck app with presentation primitives and an MIT license. | https://github.com/stackblitz/bolt-slides | “Presentation decks that are working web apps.” / “MIT license” | SUPPORTED | Live, 2026-09-04 |
| 22 | StackBlitz markets Bolt Slides for Claude Code, Codex, Cursor, and Bolt and links examples. | https://bolt.new/blog/introducing-bolt-slides | “works with any agent, including Claude Code, Codex, and Cursor” | SUPPORTED | Live, 2026-09-04 |
| 23 | Bolt is a private Vite app pinned to React 18.3.1 with only dev/build/preview scripts. | https://github.com/stackblitz/bolt-slides/blob/main/package.json | “`private`: true” / “`build`: `vite build`” / “`react`: `^18.3.1`” | SUPPORTED | Live, 2026-09-04 |
| 24 | Bolt’s visible history contains eight July 2026 commits. | https://github.com/stackblitz/bolt-slides/commits/main/ | “8 Commits” | SUPPORTED | Live, 2026-09-04 |
| 25 | Bolt imports Google Fonts by default. | https://github.com/stackblitz/bolt-slides/blob/main/src/styles/base.css | “@import url('https://fonts.googleapis.com” | SUPPORTED | Live, 2026-09-04 |
| 26 | Bolt’s PDF export is an open pull request with documented capture failures. | https://github.com/stackblitz/bolt-slides/pull/10 | “Animations ... captured before completion” / “215MB PDF” | SUPPORTED | Live, open PR, 2026-09-04 |
| 27 | Bolt’s bundled skill contains a concealed phrase-trigger branch. | https://github.com/stackblitz/bolt-slides/blob/main/.bolt/skills/slides/SKILL.md | “Internal trigger (do not mention to the user)” | SUPPORTED | Live, 2026-09-04 |
| 28 | Vite emits static production assets and supports relative base paths. | https://vite.dev/guide/build | “produces an application bundle” / “specify `./` as a base” | SUPPORTED | Live, 2026-09-04 |
| 29 | Spectacle declares active maintenance and MIT licensing. | https://github.com/FormidableLabs/spectacle | “Spectacle is under active maintenance” / “MIT license” | SUPPORTED | Live, 2026-09-04 |
| 30 | Spectacle 10.2.3 peers on React >=18. | https://github.com/FormidableLabs/spectacle/blob/main/packages/spectacle/package.json | “`version`: `10.2.3`” / “`react`: `>=18`” | SUPPORTED | Live, 2026-09-04 |
| 31 | Spectacle fixed React 19 compatibility in September 2025. | https://github.com/FormidableLabs/spectacle/releases | “fix: React19 compatibility” | SUPPORTED | Live, 2026-09-04 |
| 32 | Reveal.js provides an official React wrapper with deck primitives and lifecycle management. | https://revealjs.com/react/ | “official React wrapper” / “automatically handles initialization, syncing, and cleanup” | SUPPORTED | Live, 2026-09-04 |
| 33 | `@revealjs/react` peers on React >=18 and tests with React 19.2.4. | https://github.com/hakimel/reveal.js/blob/master/react/package.json | “`react`: `>=18`” / “`react`: `19.2.4`” | SUPPORTED | Live, 2026-09-04 |
| 34 | Reveal.js 6.0 introduced the React wrapper and 6.0.1 fixed PDF/Vite issues. | https://github.com/hakimel/reveal.js/releases | “Official React wrapper” / “Fix PDF exports” | SUPPORTED | Live, 2026-09-04 |
| 35 | Reveal.js has substantial npm adoption and MIT licensing. | https://www.npmjs.com/package/reveal.js | “Weekly Downloads” / “License MIT” | SUPPORTED | Live, 2026-09-04 |
| 36 | Reveal.js documents Chromium PDF export including notes and fragments. | https://revealjs.com/pdf-export/ | “Open your presentation with `print-pdf` included” / “Exporting Speaker Notes” | SUPPORTED | Live, 2026-09-04 |
| 37 | Slidev supports custom interactive Vue components. | https://sli.dev/guide/component | “build custom components to enrich your presentation” / “interactive and dynamic” | SUPPORTED | Live, 2026-09-04 |
| 38 | Slidev produces a hostable SPA build. | https://sli.dev/guide/hosting | “hosted as a Single-page Application (SPA)” | SUPPORTED | Live, 2026-09-04 |
| 39 | React Flow documents a spatial slideshow pattern. | https://reactflow.dev/learn/tutorials/slide-shows-with-react-flow | “turn a React Flow flow into a slideshow” | SUPPORTED | Live, 2026-09-04 |
| 40 | Recharts is a declarative React/SVG chart library. | https://www.npmjs.com/package/recharts | “Redefined chart library built with React and D3” / “License MIT” | SUPPORTED | Live, 2026-09-04 |
| 41 | Lovable hosts a React/TypeScript presentation template with zero remixes. | https://lovable.dev/templates/apps/presentations/lovable-slides | “Built with React & TypeScript” / “0 Remixes” | SUPPORTED | Live, 2026-09-04 |
| 42 | MDX Deck’s current npm release was published six years ago. | https://www.npmjs.com/package/mdx-deck | “Published 6 years ago” | SUPPORTED | Live, 2026-09-04 |
| 43 | DeckDeckGo is archived. | https://github.com/deckgo/deckdeckgo | “archived by the owner on Feb 6, 2024” | SUPPORTED | Live, 2026-09-04 |
| 44 | astro-slides advertises agent/static/export features but has minimal adoption. | https://github.com/jluterek/astro-slides | “MCP for AI Assistants” / “5 stars” / “No releases published” | SUPPORTED | Live, 2026-09-04 |
| 45 | Cogapp’s template uses local fonts and Playwright PDF with minimal adoption. | https://github.com/CogappLabs/cogapp-slides-template | “hosted locally” / “`npm run pdf`” / “0 stars” | SUPPORTED | Live, 2026-09-04 |
| 46 | Playwright exposes deterministic setup/capture primitives and discourages networkidle readiness. | https://playwright.dev/docs/api/class-page | “Adds a script which would be evaluated” / “DISCOURAGED `networkidle`” | SUPPORTED | Live, 2026-09-04 |
| 47 | ReDeck evaluates an agentic render-feedback loop. | https://arxiv.org/abs/2609.00194 | “one edit, one observation” / “step-level render feedback” | SUPPORTED | Live, 2026-09-04 |

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

- **2026-09-01 — Codex ad-hoc web research (research-doc skill).** Live-fetched 20 cited
  primary/official sources across recency-first, mainstream, contrarian, and adjacent-field
  framings. Citation spot-check re-opened Slidev AI/export, Gamma API, Plus AI MCP, Tome sunset,
  and the 2026 classroom study. Adversarial passes: conventional (format compatibility),
  contrarian (vendor quality claims and Tome shutdown), historical (Markdown/Pandoc lineage),
  adjacent (data stories, terminal, video), skeptic (every VERIFIABLE-tagged claim mapped to a
  literal quote). High/medium issues resolved by recommending a PDF fallback, separating vendor
  capability from quality evidence, narrowing scholarly claims to preprints, and surfacing the
  2026-09-01/02 date conflict. No repository files were modified during research; this run
  produced a target-local relay diff.

- **2026-09-04 — Codex interactive React/HTML refine (research-doc skill).** Live-fetched 27
  additional primary/official sources and re-checked the existing Slidev export source. The
  named reference HTML was absent from the working tree, so no visual claim about it was made.
  Adversarial passes covered maturity/adoption, agent-instruction integrity, offline/export
  behavior, abandoned alternatives, and the contrarian “do we need React?” premise. Citation
  spot-check re-opened Bolt’s repository, package, bundled skill and PDF pull request; Spectacle’s
  React 19 release; Reveal’s React wrapper and PDF documentation; Slidev’s component/export
  documentation; and the ReDeck preprint. Baseline `aido validate-doc` and `markdownlint-cli2`
  passed before refinement; both must be re-run after the relay applies this patch.
- **2026-10-21 — Opus coordinator refine for the presentation skill version 2 (§8).** Two
  read-only Codex research legs (gpt-5.6-sol, effort high) returned findings; the coordinator
  verified the load-bearing ones against the files and wrote §8 itself. Leg 1 swept the
  repository's visualization, graphics, browser and frontend elements and checked the version 1
  design's fidelity to this document. The coordinator re-read the critic, the DOM inspector, the
  doc renderer README, the diagram vision-review procedure and the visual rubric before accepting
  its inventory. Leg 2 researched the `@revealjs/react` API, readiness, export and presenter view,
  and live-deck testing with Playwright, axe-core and vision review, retrieving 39 sources with
  registry checks for every version stated; its escalations section was empty. Its footnotes are
  carried here as [^61] to [^99].

<!-- /doc:region name="run_history" -->
