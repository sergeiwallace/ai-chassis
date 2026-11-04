---
title: "Visual Doc Quality Rubric: roadmap and architecture documents"
category: procedure
tags: [procedure, rubric, roadmap, architecture, diagrams, review]
status: active
source: "claude-opus engineer, example-session, 2026-10-13, from the roadmap and architecture doc harness design"
template_version: "procedure-1.0.0"
---

<!-- doc:region name="overview" kind="replaceable" -->

# Visual Doc Quality Rubric: roadmap and architecture documents

> The criteria a human reviewer on the dev team, or a technical stakeholder, applies to a roadmap or
> architecture document and its diagrams, each with what to look for, what fails it, a verdict and
> its source.
>
> Last updated: 2026-10-20

Sources are provisional: the harness's research synthesis on visual-first roadmap and architecture docs will refine them and replace `[HEURISTIC]` where it finds evidence.

## Table of Contents

- [Who this is for](#who-this-is-for)
- [How to review](#how-to-review)
- [Verdict scale](#verdict-scale)
- [Criteria](#criteria)
  - [1. Comprehension](#1-comprehension)
  - [2. Hierarchy of attention](#2-hierarchy-of-attention)
  - [3. Current against target](#3-current-against-target)
  - [4. Consistency across levels](#4-consistency-across-levels)
  - [5. Honesty of time](#5-honesty-of-time)
  - [6. Fidelity across surfaces](#6-fidelity-across-surfaces)
  - [7. Accessibility](#7-accessibility)
  - [8. Writing](#8-writing)
  - [9. Diagram construction](#9-diagram-construction)
- [Recording a review](#recording-a-review)
- [Revision Log](#revision-log)

## Who this is for

Reviewers of a document written in the [roadmap](../roadmap/TEMPLATE.md) or
[architecture](../architecture/TEMPLATE.md) doc kind: engineers on the dev team first, then product and
technical stakeholders. You do not need to know how the document was produced. Every criterion is
something you can check by looking at the document, its rendered HTML and PDF, and its diagrams. An
`/audit-auto` round on one of these documents cites the same criteria by id, so its findings and yours
line up.

## How to review

1. Open the document three ways: on GitHub, in the VS Code Markdown preview, and as the rendered HTML
   and PDF the doc-render generator produced. Note the render receipt's warnings (text below the
   minimum size, diagrams over the node cap); they point at R-6 and R-18.
2. Read the first screen cold, before anything else, and write down in one sentence what the system is
   for (R-1). Then read the rest.
3. Mark every criterion below pass, fix or fail. A criterion that does not apply (a document with no
   timeline has no R-12 arrows) is marked n/a with one line saying why.
4. Record the verdicts in the table under [Recording a review](#recording-a-review), with the
   location of every fix or fail (section, diagram id, or line) and the evidence for every
   verdict. A model reviewer is briefed from the Round 1 Reviewer Prompt in
   the audit doc kind's template, which carries the reviewer-family, per-row
   evidence and length rules.

## Verdict scale

| Verdict | Meaning | What happens next |
|---|---|---|
| pass | The criterion holds everywhere in the document | Nothing |
| fix | The criterion fails in a bounded, local way (one diagram, one section) that the author can correct without restructuring | The author fixes it before the document is shared |
| fail | The criterion fails across the document, or in its Executive Summary or map, so a reader would come away wrong | The document goes back to the author; it is not shared until the criterion passes |

A document is ready to share when it has no fail verdicts and every fix is corrected.

## Criteria

Grouped as in the harness design, most important group first.

### 1. Comprehension

#### R-1: A cold reader can say what the system is for after the first screen

- **What you look for:** The first screen (the title, the status lines and the first diagram of the
  Executive Summary, at a 1440 by 900 viewport) is enough to say in one sentence what the system does
  and for whom.
- **What fails it:** After the first screen you cannot write that sentence, or the sentence you write
  is wrong once you read on; the first screen is a provenance block, a background paragraph or a
  table of contents with no diagram after it.
- **Verdict:** pass when your sentence holds after reading the whole document; fix when the sentence
  is possible but the first diagram's lead line does not say what to take from it; fail when the
  first screen carries no diagram or your sentence is wrong.
- **Source:** `[HEURISTIC]`

#### R-2: Every diagram has one obvious main path

- **What you look for:** In each diagram, the eye finds one path from where a request or a fact starts
  to where it ends, and the lead line names it.
- **What fails it:** A diagram where every node connects to several others with no dominant direction,
  where the main path doubles back, or where two equally weighted paths compete and the lead line
  does not say which matters.
- **Verdict:** pass when every diagram has one main path; fix when one or two diagrams lack it; fail
  when the map or the Executive Summary diagrams lack it.
- **Source:** `[HEURISTIC]`

#### R-3: Labels are the words a stakeholder uses, not file names

- **What you look for:** Node and edge labels are nouns and verbs a person on the team would say
  aloud ("ingest", "the consumer API", "promote to the system of record").
- **What fails it:** A label that is a file path, a stack number with no name, a class or function
  name, or an unexplained acronym; an edge with no label where the relationship is not obvious.
- **Verdict:** pass when no such label appears; fix when a few labels need rewording; fail when the
  map or the Executive Summary diagrams are labelled in implementation names.
- **Source:** `[HEURISTIC]`

### 2. Hierarchy of attention

#### R-4: Overview first, detail only in the zoom panels

- **What you look for:** The first diagram is level 0 (the system among its users and sources), the
  next is level 1 (the map of its parts), and component detail appears only in the level-2 zoom
  panels.
- **What fails it:** Component-level detail (individual functions, every stack, every gateway) in the
  Executive Summary or on the map; a zoom panel that appears before the map it zooms from.
- **Verdict:** pass when the order holds; fix when one diagram carries detail a level too deep; fail
  when the Executive Summary opens on a detailed diagram.
- **Source:** Shneiderman, B. (1996), "The Eyes Have It: A Task by Data Type Taxonomy for Information
  Visualizations", IEEE Symposium on Visual Languages ("overview first, zoom and filter, then
  details on demand"); the level names follow the C4 model, Simon Brown, <https://c4model.com>.

#### R-5: Emphasis marks at most one thing per diagram, and a legend names it

- **What you look for:** Where colour, weight or size highlights something, it highlights one class of
  thing (what changes, what is live), and the lead line or a legend sentence says what it means.
- **What fails it:** Several colours or weights with no legend; emphasis used decoratively; a legend
  that does not match the colours actually drawn.
- **Verdict:** pass when every emphasised diagram has one emphasis and a matching legend; fix when a
  legend is missing or wrong in one or two diagrams; fail when colour carries meaning nowhere
  explained.
- **Source:** `[HEURISTIC]`

#### R-6: At most twelve primary nodes per diagram

- **What you look for:** Each diagram has twelve or fewer primary nodes (boxes a reader must read;
  greyed context nodes count only if labelled with more than a name).
- **What fails it:** A diagram over twelve primary nodes that is not explicitly an inventory, or an
  inventory diagram standing in for the map. The render receipt lists diagrams over the configured
  cap.
- **Verdict:** pass when every diagram is at or under twelve; fix when a deliberate inventory exceeds
  it and says so in its lead line; fail when the map or an Executive Summary diagram exceeds it.
- **Source:** `[HEURISTIC]`

### 3. Current against target

#### R-7: One encoding for today against target, stated in one legend

- **What you look for:** Every "today against target" view in the document uses the same encoding:
  the harness delta classes (blue solid today, green change, orange dashed target, grey dotted
  retiring), or solid against dashed lines, never a different scheme per diagram.
- **What fails it:** Two diagrams where the same colour means different things; a target drawn solid
  in one diagram and dashed in another; class definitions whose fills differ from the template's.
- **Verdict:** pass when one encoding holds throughout; fix when one diagram deviates; fail when the
  encoding changes between the Executive Summary, the map and the panels.
- **Source:** `[HEURISTIC]`

#### R-8: A pair of diagrams shares layout and node positions

- **What you look for:** Where today and target are drawn as two diagrams (a `doc:pair`), both use the
  same direction, the same node ids and the same positions for the nodes they share, so the eye
  compares rather than re-reads; a pair is used only where the shapes genuinely differ.
- **What fails it:** A pair whose two diagrams run in different directions or place shared nodes
  differently; a pair where a single delta-highlighted diagram would have shown the same change.
- **Verdict:** pass when every pair compares cleanly; fix when one pair's layouts drift; fail when
  pairs are the default and the document doubles its diagram count.
- **Source:** Tufte, E. R. (1990), Envisioning Information, Graphics Press, chapter "Small Multiples"
  (comparisons are made within one eyespan, on a constant design).

### 4. Consistency across levels

#### R-9: Every zoom panel names its parent, and the parent carries the panel's callout

- **What you look for:** Each level-2 panel says which diagram it zooms from (usually the map), and the
  map shows the same sub-system with a numbered callout "[N]" that matches the panel's heading number.
- **What fails it:** A panel with no parent named; a callout number on the map with no panel, or a
  panel with no callout; panel numbers that do not match the map.
- **Verdict:** pass when every panel and callout match; fix when one or two mismatch; fail when the
  panels cannot be placed on the map.
- **Source:** C4 model, Simon Brown, <https://c4model.com> (each level zooms into one element of the
  level above).

#### R-10: A node that appears at two levels has the same label at both

- **What you look for:** The name of a part is the same on the level-0 diagram, the map, its zoom
  panel and in the prose, and the roadmap uses the architecture document's names.
- **What fails it:** One part under two names ("the API", "the consumer gateway"); a part that appears
  in two panels under different names; a roadmap whose map renames the architecture document's
  containers.
- **Verdict:** pass when every shared node keeps its label; fix when one or two labels drift; fail
  when the same part appears under several names across the document.
- **Source:** C4 model, Simon Brown, <https://c4model.com> (one model, several views of it).

### 5. Honesty of time

#### R-11: No date appears that is not in the tracker or a dated decision

- **What you look for:** Every date, quarter or month in the document traces to a tracker field (a
  sprint, a due date, a release) or to a decision recorded with its date; otherwise the work is
  ordered (Now, Next, Later) and the document says the lanes are not dated.
- **What fails it:** A Gantt chart or a quarter label for work the tracker holds no date for; "by end
  of Q3" with no source; dates that differ from the tracker.
- **Verdict:** pass when every date traces; fix when one or two dates lack a source; fail when the
  timeline itself is invented.
- **Source:** `[HEURISTIC]`

#### R-12: A dependency arrow points from the thing that must land first, and the text says so

- **What you look for:** In every dependency or timeline diagram the arrow direction means "this must
  land before that", consistently, and the lead line states the convention.
- **What fails it:** Arrows that mean "depends on" in one diagram and "comes before" in another; no
  statement of what an arrow means; an external dependency drawn without being marked as one.
- **Verdict:** pass when the convention is stated and held; fix when the statement is missing; fail
  when arrows contradict each other.
- **Source:** `[HEURISTIC]`

### 6. Fidelity across surfaces

#### R-13: The document renders on GitHub, in the VS Code preview and in the PDF, and the reviewer has looked at all three

- **What you look for:** Every Mermaid fence renders on GitHub and in the VS Code preview; the HTML and
  PDF show the same diagrams; no content depends on HTML that GitHub drops.
- **What fails it:** A fence that shows as source text on one surface; a diagram present in the HTML
  but missing or different in the Markdown; a rendered artifact older than the source (the render
  receipt's source digest does not match the file).
- **Verdict:** pass when all three surfaces agree; fix when one diagram fails on one surface; fail when
  the rendered artifacts are stale or the document was reviewed on one surface only.
- **Source:** `[HEURISTIC]`; GitHub's Mermaid support is documented in GitHub Docs, "Creating
  diagrams", <https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/creating-diagrams>.

#### R-14: Every link opens its target from VS Code (local and Remote SSH) and on GitHub

- **What you look for:** Click a sample of links, including every link to a rendered `.html`, `.png`
  or `.pdf` artifact, from the VS Code preview (on a local checkout and over Remote SSH) and on
  GitHub; each opens its target. The exact link form for rendered artifacts is being established by
  the harness's link-fidelity fix and will be stated here when it lands; until then, plain relative
  links with plain link text, and a GitHub URL beside any cross-repository link.
- **What fails it:** A link that opens nothing, opens a blank or phantom file, or opens the raw source
  of an HTML file where the rendered page was meant; a `vscode://file` link; a relative link that
  leaves the repository with no GitHub URL beside it.
- **Verdict:** pass when every sampled link opens on every surface; fix when a few links fail; fail
  when the links to the interactive or rendered versions of the main diagrams fail.
- **Source:** `[HEURISTIC]`

#### R-15: The PDF splits no diagram across pages and has no text below 8 pt

- **What you look for:** In the PDF, every diagram sits whole on one page, and every piece of text,
  diagram labels included, is legible at 100 percent zoom (8 pt or larger).
- **What fails it:** A diagram cut by a page break; a diagram scaled down until its labels are
  unreadable; a table running off the page edge.
- **Verdict:** pass when no split or tiny text appears; fix when one or two diagrams split or shrink;
  fail when the map or an Executive Summary diagram is unreadable in the PDF.
- **Source:** `[HEURISTIC]`

### 7. Accessibility

#### R-16: Colour never carries meaning alone

- **What you look for:** Everything colour distinguishes is also distinguished by line style, weight,
  a label or a symbol: the harness classes pair each colour with a line style (target dashed,
  retiring dotted, change heavier).
- **What fails it:** Two classes that differ only in fill; a legend that says "green means changing"
  with nothing else marking those nodes; the document read in greyscale loses a distinction the text
  relies on.
- **Verdict:** pass when a greyscale print keeps every distinction; fix when one diagram relies on
  colour alone; fail when the document's today against target encoding does.
- **Source:** W3C, Web Content Accessibility Guidelines (WCAG) 2.1, Success Criterion 1.4.1 Use of
  Color, <https://www.w3.org/TR/WCAG21/#use-of-color>.

#### R-17: The palette is colour-blind safe

- **What you look for:** The colours the document uses remain distinguishable under the common
  colour-vision deficiencies, using either the harness delta classes unchanged or a palette drawn
  from a colour-blind-safe set.
- **What fails it:** Red against green as the main distinction; a custom palette with no check; class
  fills changed from the template's.
- **Verdict:** pass when the harness classes are used unchanged or the palette has been checked; fix
  when one diagram uses an unchecked colour; fail when the main encoding is red against green.
  Note: the harness delta palette has not yet been checked with a colour-vision simulator; until it
  is, R-16's line styles are what keep its green and orange apart.
- **Source:** Okabe, M. and Ito, K., "Color Universal Design (CUD): How to make figures and
  presentations that are friendly to colorblind people", <https://jfly.uni-koeln.de/color/>; Brewer,
  C. A., ColorBrewer, <https://colorbrewer2.org>.

#### R-18: Diagram text is at least body size at the documented viewport and in print

- **What you look for:** At a 1440-pixel-wide viewport, diagram labels read at roughly the size of the
  body text. At the printable width of the document's PDF page (703 CSS px for A4 portrait with
  12 mm margins), every text element measures 16 px or more and headers 18 px or more, by
  `archify_print measure` or `archify_print render`'s own page-width gate.
- **What fails it:** A diagram that must be zoomed to be read; a receipt warning on the map or an
  Executive Summary diagram; any text element under the print floor.
- **Verdict:** pass when the measurement reports no text under the floor and labels read without
  zooming; fix when one or two zoom panels fall below; fail when the map or an Executive Summary
  diagram does.
- **Source:** `[HEURISTIC]` for the 1440 viewport; the 16 px print floor is the doc-render
  `min_text_px` default, measured in [the diagram render and vision review](diagram-render-and-vision-review.md).

### 8. Writing

#### R-19: The Executive Summary answers first, with no background paragraph before its first diagram

- **What you look for:** Under the Executive Summary heading, at most one or two framing sentences and
  then the first diagram; the sentences after it state conclusions (what is live, what is missing,
  where it is going), not history.
- **What fails it:** A background, provenance or history paragraph before the first diagram; summary
  sentences that describe the document rather than the system.
- **Verdict:** pass when the summary opens on its diagram and its conclusions; fix when one paragraph
  of background precedes the diagram; fail when the summary is prose with diagrams after it.
- **Source:** Minto, B., The Pyramid Principle: Logic in Writing and Thinking (start with the answer,
  then the support).

#### R-20: Every requested decision carries a recommendation and a named drawback

- **What you look for:** Every question put to the owner, in the decision table and under Open
  Questions, has a recommendation and the drawback of taking it, named in a sentence.
- **What fails it:** A question with options and no recommendation; a recommendation with no drawback,
  or a drawback that is a restatement of the recommendation.
- **Verdict:** pass when every decision request carries both; fix when one or two lack the drawback;
  fail when the decision table carries questions without recommendations.
- **Source:** `[HEURISTIC]`

### 9. Diagram construction

Each criterion here is checked on the figure as printed, at the document's real page width, from the
capture the [diagram render and vision review](diagram-render-and-vision-review.md) loop produces.
Ordered by how often a first draft fails it.

#### R-21: Groups nest where the content has hierarchy

- **What you look for:** Every box sits inside a named group, and where the system has layers or
  owners inside owners, a group contains a sub-group with its own header.
- **What fails it:** A flat grid of equal boxes ("a brick wall"); a layer drawn as a row of boxes with
  no enclosing group; a group header that names nothing the text uses.
- **Verdict:** pass when the nesting matches the hierarchy the text describes; fix when one diagram is
  flatter than its content; fail when the map or an Executive Summary diagram is a flat grid.
- **Source:** Brown, S., The C4 model, Notation, <https://c4model.com/diagrams/notation> (boundaries
  group elements by owner or deployment).

#### R-22: Every edge carries a label stating an action, protocol or cadence

- **What you look for:** Each arrow says what moves along it ("loads nightly", "queries over HTTPS",
  "asks in plain language").
- **What fails it:** An unlabelled arrow; a label that says only "uses", "calls" or "data".
- **Verdict:** pass when every edge is labelled with a verb phrase; fix when a few edges are bare; fail
  when most edges in the map are bare.
- **Source:** Brown, S., The C4 model, Notation, <https://c4model.com/diagrams/notation> (every
  relationship carries a label stating its intent).

#### R-23: Every box has a bold header over smaller, non-bold detail, left-aligned and wrapped

- **What you look for:** A header line in bold, then one to three detail lines in a smaller regular
  weight saying what the box does. Multi-line text starts at the left edge of the box and breaks
  between words, inside the box.
- **What fails it:** Centred multi-line text; a line broken mid-word or ending on one orphan word;
  detail shrunk to fit one line instead of wrapping; a header and detail at the same weight and size;
  text running past the box edge.
- **Verdict:** pass when every box reads header first, then detail, in left-aligned wrapped lines; fix
  when one diagram centres or crams; fail when the renderer's default does it everywhere.
- **Source:** Butterick, M., Practical Typography, Centered text,
  <https://practicaltypography.com/centered-text.html>; W3C, Understanding SC 1.4.8 Visual
  Presentation, <https://www.w3.org/WAI/WCAG22/Understanding/visual-presentation.html> (text aligned
  left or right, not justified or centred).

#### R-24: Commentary sits in annotation boxes outside the nodes, and prints with the figure

- **What you look for:** The takeaway, caveats and "why" notes are in callout or note boxes beside the
  diagram, connected or placed next to what they explain, and they appear in the printed image.
- **What fails it:** Commentary crammed into node detail text; notes that exist only in the
  interactive HTML and vanish from the PNG, SVG or PDF.
- **Verdict:** pass when the figure needs commentary and carries it in print; fix when a note is
  missing from print; fail when the commentary is written inside the nodes or exists only in the
  interactive viewer; n/a when the figure needs none.
- **Source:** `[HEURISTIC]`

#### R-25: The legend decodes every colour, shape, line style and group tone, in print

- **What you look for:** A legend inside the printed figure with one entry for each fill colour, each
  shape, each line style (solid, dashed, dotted, heavy) and each group tone used.
- **What fails it:** No legend; a legend listing node types but not line styles or group colours; a
  colour or shape that appears in the figure with no entry; a legend only in the HTML viewer.
- **Verdict:** pass when every encoding is decoded; fix when one entry is missing; fail when the figure
  has no legend.
- **Source:** Brown, S., The C4 model, Notation, <https://c4model.com/diagrams/notation> (every
  diagram carries a key or legend explaining its notation).

#### R-26: Each macro group has its own tone, and no two neighbours share one

- **What you look for:** Top-level groups (for example sources, the system, its readers) are told
  apart by fill hue, and adjacent groups never share a tone.
- **What fails it:** Every group drawn in the same colour; two touching groups in near-identical
  fills; a tone that changes meaning between figures of the same document.
- **Verdict:** pass when group tones are pairwise distinct and consistent across the document; fix when
  one figure reuses a tone; fail when all groups share one.
- **Source:** `[HEURISTIC]`, with R-16 and R-17 applying to the chosen tones.

#### R-27: Text contrasts with its fill, and every fill contrasts with the fill behind it

- **What you look for:** Text at least 4.5:1 against the fill it sits on. A box, label mask or edge
  label's background either equals the fill behind it or contrasts with it at 3:1 or more.
- **What fails it:** Light grey label boxes on a mid or dark grey group; a node fill a shade off the
  group fill behind it (between about 1.05:1 and 3:1); pale text on a pale fill.
- **Verdict:** pass when the renderer's contrast check passes and the capture confirms it; fix when one
  figure has a near-match; fail when the default palette produces near-matches.
- **Source:** W3C, Understanding SC 1.4.3 Contrast (Minimum),
  <https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html>; W3C, Understanding SC 1.4.11
  Non-text Contrast, <https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html>.

#### R-28: No label is clipped, covered or overlapped

- **What you look for:** Every group header, node label and edge label is whole and free of any other
  shape or text.
- **What fails it:** A group header cut off by a nested box; an edge label sitting on a node; two
  labels touching; text clipped at the figure edge.
- **Verdict:** pass when the renderer's overlap checks pass and the capture shows no collision; fix or
  fail by how many labels collide. Confirm a suspected collision by measurement, not by eye alone.
- **Source:** `[HEURISTIC]`

#### R-29: Shapes and notation follow the standard for the diagram type

- **What you look for:** The figure uses the notation its type calls for, and its caption names it:
  UML (component, deployment, sequence, activity, state, class) for software structure and behaviour;
  C4 levels for architecture views; crow's foot for entity relationships; BPMN or a UML activity
  diagram for processes; a Gantt or swimlane form for timelines. Inside the notation, data stores are
  cylinders, queues are queues, people are actors, external systems are visibly marked as external,
  and planned elements are dashed.
- **What fails it:** Every element a rounded rectangle whatever it is; a database drawn as a box with
  a small icon; a sequence drawn as a flowchart; a caption that does not say which notation it uses.
- **Verdict:** pass when the notation is named and followed; fix when a few shapes are generic; fail
  when an architecture or data-flow figure uses no standard shapes.
- **Source:** Object Management Group, Unified Modeling Language 2.5.1,
  <https://www.omg.org/spec/UML/2.5.1/About-UML>; Brown, S., The C4 model,
  <https://c4model.com/diagrams/notation>; PlantUML, Deployment diagram element types,
  <https://plantuml.com/deployment-diagram>.

#### R-30: The printed image is the diagram alone, with no viewer chrome

- **What you look for:** The image in the document and the PDF shows the diagram, its legend and its
  annotation boxes, and nothing of the interactive viewer.
- **What fails it:** Buttons, a zoom dock, a theme toggle or a scroll bar in the image; a screenshot of
  the browser window instead of an export.
- **Verdict:** pass when the image is the clean export; fix when the export is right but one piece of
  chrome (a caption bar, a stray control) remains; fail when the image is a browser screenshot.
- **Source:** `[HEURISTIC]`

#### R-31: The lead sentence matches the drawing

- **What you look for:** The sentence that introduces the figure states counts, directions and names
  that the figure shows.
- **What fails it:** "Three layers" over a four-layer figure; "flows left to right" over a top-down
  figure; a node named in the text that the figure calls something else.
- **Verdict:** pass when every count and direction word matches; fix when one count or name is off;
  fail when the lead describes a different drawing (another direction, missing groups, a removed node).
- **Source:** `[HEURISTIC]`

#### R-32: Edges route around nodes, and crossings are zero or justified

- **What you look for:** No edge passes through a node, and edges do not cross unless the lead says
  why the crossing is unavoidable.
- **What fails it:** An arrow drawn through a box; several avoidable crossings in the main path.
- **Verdict:** pass when there are no crossings or each is justified; fix when one avoidable crossing
  remains; fail when an edge passes through a node.
- **Source:** Purchase, H. C., "Which aesthetic has the greatest effect on human understanding?",
  Graph Drawing 1997, LNCS 1353 (edge crossings have the largest effect on comprehension).

## Recording a review

Copy this table into the review (an audit document, a pull request comment or the document's Feedback
section), one row per criterion:

| Criterion | Verdict | Where | Evidence | Note |
|---|---|---|---|---|
| R-1 | pass / fix / fail / n/a | [section or diagram id] | [the quote, screenshot path or measurement the verdict rests on, a pass included] | [what to change] |

<!-- /doc:region name="overview" -->

<!-- doc:region name="steps" kind="replaceable" -->

<!-- /doc:region name="steps" -->

<!-- doc:region name="rationale" kind="replaceable" -->

<!-- /doc:region name="rationale" -->

<!-- doc:region name="revision_log" kind="append_only" -->

## Revision Log

| Date | Change | Notes |
|------|--------|-------|
| 2026-10-13 | First version: R-1 to R-20 in eight groups, from the harness design | Sources provisional; the research synthesis refines them |
| 2026-10-20 | Added section 9, R-21 to R-32, and the print floor in R-18 | From the owner's diagram feedback of 2026-10-20 and the roadmap diagram research's 18-item checklist |

<!-- /doc:region name="revision_log" -->
