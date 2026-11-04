---
title: "Diagram render and vision review: archify figures checked as printed"
category: procedure
tags: [procedure, diagrams, archify, rendering, verification, vision, rubric]
status: active
source: "claude-opus driver, example-session, 2026-10-20, from the owner's diagram feedback and the roadmap diagram research"
template_version: "procedure-1.0.0"
---

<!-- doc:region name="overview" kind="replaceable" -->

# Diagram render and vision review

> How an agent renders an archify figure at the document's real page width, looks at it, and checks
> it against the written criteria before the figure lands. Mandatory for every diagram in a design,
> plan, research, audit, analysis, architecture or roadmap document.
>
> Last updated: 2026-10-20

## Table of Contents

- [The Problem](#the-problem)
- [The Flow](#the-flow)
- [Steps](#steps)
  - [Step 1: Choose the notation](#step-1-choose-the-notation)
  - [Step 2: Author the spec from the facts](#step-2-author-the-spec-from-the-facts)
  - [Step 3: Validate and deliver](#step-3-validate-and-deliver)
  - [Step 4: Export the print image](#step-4-export-the-print-image)
  - [Step 5: Measure at the page width](#step-5-measure-at-the-page-width)
  - [Step 6: Render the document and crop the page](#step-6-render-the-document-and-crop-the-page)
  - [Step 7: Look, then have a non-author look](#step-7-look-then-have-a-non-author-look)
  - [Step 8: Record the verdicts](#step-8-record-the-verdicts)
- [Checklist](#checklist)
- [Anti-Patterns](#anti-patterns)
- [Cadence](#cadence)

## The Problem

A diagram that validates can still fail its reader. Archify's showcase checks catch overlaps,
crossings and clipped labels in the browser at desktop sizes, but a figure placed in a PDF is
scaled to the page, and text that reads at 1440 px can print at 4 to 6 px. Flat grids of equal
boxes, centred text broken across lines, one colour for every group, label fills a shade off the
group behind them and a legend that decodes only some encodings all pass a schema and fail a
person. The owner's rule is that every figure is generated with archify, drawn in the standard
notation for its type, and looked at as printed before it lands.

## The Flow

Choose the notation, author the spec, validate and deliver, export the print image, measure it at
the page width, render the document, look at the crop, have a non-author look, record the verdicts.
A failed step goes back to the spec; nothing is fixed in the exported image.

## Steps

### Step 1: Choose the notation

1. Name the question the figure answers and the reader it answers it for.
2. Pick the notation for the diagram type, and name it in the caption:

   | Figure shows | Notation | archify type |
   |---|---|---|
   | Containers, services and stores, and who calls whom | C4 container or context view | `architecture` |
   | Where software runs | UML deployment (nodes, artifacts) | `architecture` |
   | One request over time | UML sequence | `sequence` |
   | A process with roles | UML activity with swimlanes, or BPMN | `workflow` |
   | Data moving between stages | data-flow (stages, stores as cylinders) | `dataflow` |
   | States of one thing | UML state machine | `lifecycle` |
   | Entities and keys | crow's foot ERD | `erd` |
   | Types and their relationships | UML class | `class` |
   | Plan over time | Gantt or swimlane timeline | `workflow` or `timeline` |

3. Use Mermaid only where the fleet's doc-diagrams path rule, which loads on design and plan docs,
   allows it.

### Step 2: Author the spec from the facts

1. Take every node, edge and label from the document's text and its sources, not from memory.
2. Follow the archify skill's fast authoring path: one schema, one example, the candidate written
   first, `meta.quality_profile: "showcase"`.
3. For an `architecture`, `sequence` or `workflow` (schema v2) figure, add the top-level `print`
   block that `archify_print` reads and strips before archify sees the spec (archify's schemas
   refuse unknown keys):
   - `width_px`: the printable width of the document's page in CSS px (703 for A4 portrait with
     12 mm margins); `min_text_px` (default 16) and `header_min_px` (default 18).
   - `components.<id>.shape`: `box`, `cylinder`, `queue`, `actor` or `external`. `database`,
     `messagebus` and `external` types get cylinder, queue and external by default.
   - `components.<id>.status`: `current` or `target`; a target draws a dashed outline.
   - `boundaries.<label>.tone`: a palette name (`blue`, `green`, `amber`, `violet`, `rose`, `teal`,
     `slate`, `olive`) or `{fill, border, title}`; unset groups get distinct tones automatically.
   - `legend.components` and `legend.lines`: the legend wording per component type and line style.
   - For a `sequence`: `segments.<label>.operator` for every segment, the UML operator its
     combined-fragment tab shows (`opt`, `alt`, `loop`, `par`, `break`, `critical` and the rest of
     UML's twelve). The spec also needs `meta.viewBox`, and a message `note` is refused; put it in
     a card. The figure draws synchronous calls solid, replies dashed and asynchronous messages
     with an open head.
   - For a `workflow`: `schema_version: 2`; `components.<id>` takes node ids and
     `boundaries.<label>.tone` takes lane and group labels. The figure draws a header on every
     lane and a header cell over every phase column, with a divider between phases; keep each node
     in a column its phase spans. Size nodes for print (about 150 x 100 units) with `width` and
     `height`.

   Author at print density: about 170-unit boxes in an 860-unit-wide viewBox. A desktop-density
   spec is refused with a diagnostic per element rather than shrunk under the floor. Archify 3.2
   also requires `meta.output` on every spec passed to `deliver`.
4. Build the figure to meet the criteria in [the rubric's section 9](visual-doc-quality-rubric.md#9-diagram-construction)
   from the start: nested groups with headers, a verb-phrase label on every edge, a header and
   detail on every box, a tone per macro group, standard shapes per type, a legend entry for every
   encoding, and the takeaway in an annotation box.

### Step 3: Validate and deliver

For an `architecture`, `sequence` or `workflow` (schema v2) figure with a `print` block,
`archify_print render` (Step 4) runs this step for you. For every other type:

1. `node <archify>/bin/archify.mjs validate <type> <spec.json> --quality showcase --json` after every
   edit. Change only the diagnosed subject. Stop after two rounds without a new minimum error count
   and report what remains.
2. `node <archify>/bin/archify.mjs deliver <type> <spec.json> <out.html> --quality showcase --json`.
   A non-zero exit is never success.
3. `node <archify>/bin/archify.mjs visual-check <out.html> --json` for the browser evidence. On a host
   without a system Chrome, set `ARCHIFY_CHROME` to the Playwright Chromium binary.

### Step 4: Export the print image

1. For an `architecture`, `sequence` or `workflow` (schema v2) figure, from the ai-harness root:

   ```bash
   uv run --project skills/_shared/doc-render python -m archify_print render <spec.json> <slug>.print.svg --html <slug>.html
   ```

   It runs archify's showcase gate, then its own checks (text fit and wrap at print size, label
   and title collisions, group tones, contrast, the size floors), then a Chromium check at the page
   width, and writes the SVG, a PNG capture and a receipt only when all pass. Exit codes: 0
   written, 1 a check failed, 2 the spec or arguments are wrong, 3 node, archify or Chromium is
   missing; nothing is written on a non-zero exit. The SVG is the diagram, its legend and its
   annotation boxes, with no viewer chrome.
2. Other diagram types have no print exporter yet. Deliver the HTML, then run Step 5 on it; until a
   print path exists for the type, a figure that measures under the floor is recorded as a known
   gap in the verdict table, never shipped as passing.
3. Commit the SVG beside the document and embed it as an image (`![<alt text>](<path>.svg)`), so
   GitHub, the VS Code preview and the PDF all show the same figure.

### Step 5: Measure at the page width

1. `render` already measures. For any other SVG or archify HTML:

   ```bash
   uv run --project skills/_shared/doc-render python -m archify_print measure <figure.svg|figure.html> <out.png> --width 703
   ```

   It places the figure alone at the page's printable width, writes the PNG, and reports the
   smallest rendered size of each text role, the count of text elements under the floor, and any
   line escaping its box or overlapping another.
2. The floor is 16 px for every text element and 18 px for headers. R-18 and R-27 are decided by the
   measurement and the contrast check, never by eye.

### Step 6: Render the document and crop the page

1. Render the document with doc-render, then rasterise the page holding the figure
   (`pdftoppm -r 150 -f <page> -l <page> -png <pdf> <out>`).
2. Crop the figure at native size. A vision model reads a cropped region far better than a whole
   page.

### Step 7: Look, then have a non-author look

1. Open the crop and check it against every criterion in [the rubric's section 9](visual-doc-quality-rubric.md#9-diagram-construction),
   plus R-16, R-17 and R-18.
2. Give a fresh reader that did not write the spec the crop, the spec and the criteria, from another
   model family where one is available. It returns pass or fail per criterion with the pixel location
   of each defect.
3. Confirm every geometric claim (an overlap, a clipped label, a contrast near-match) by measurement
   before acting on it: vision models misjudge overlap often enough that a look alone is not
   evidence.
4. Fix in the spec and go back to Step 3.

### Step 8: Record the verdicts

1. Commit the capture, the measurement output and the per-criterion verdict table beside the
   document, under its `evidence/` directory, using the table in
   [Recording a review](visual-doc-quality-rubric.md#recording-a-review).
2. A figure whose capture nobody opened is not done. A criterion is waived only by a recorded
   decision that names its drawback.

## Checklist

- [ ] The caption names the notation and scope.
- [ ] `deliver` exited 0 with all showcase checks passing.
- [ ] The committed image is the clean export, with no viewer chrome.
- [ ] The page-width measurement shows no text under 16 px and no header under 18 px.
- [ ] The contrast check passes.
- [ ] The author and a non-author reader both checked the crop against rubric section 9, R-16, R-17
      and R-18, and every geometric claim was measured.
- [ ] The capture, the measurement and the verdict table are committed on a tracked path.

## Anti-Patterns

- **Validating and not looking:** a showcase pass is desktop evidence. It says nothing about the
  printed size, the legend's completeness or whether the notation fits the question.
- **Screenshotting the viewer:** a browser capture carries the viewer's buttons and zoom dock into
  the document.
- **Fixing the image instead of the spec:** an edited PNG drifts from its source the next time anyone
  regenerates it.
- **Shrinking text to fit:** a figure that only fits by going under the floor has too many boxes for
  one page; split it or cut boxes.

## Cadence

| Activity | Frequency | Trigger |
|----------|-----------|---------|
| The full loop | Every figure | A new figure, or any change to a figure's spec |
| Steps 5 to 7 | Every document render | A layout or page-size change in the document |

<!-- /doc:region name="overview" -->

<!-- doc:region name="steps" kind="replaceable" -->

<!-- /doc:region name="steps" -->

<!-- doc:region name="rationale" kind="replaceable" -->

<!-- /doc:region name="rationale" -->

<!-- doc:region name="revision_log" kind="append_only" -->

## Revision Log

| Date | Change | Notes |
|------|--------|-------|
| 2026-10-20 | First version | From the owner's diagram feedback of 2026-10-20 and the roadmap diagram research |

<!-- /doc:region name="revision_log" -->
