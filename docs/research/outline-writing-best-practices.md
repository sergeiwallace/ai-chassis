---
title: "Outline-Writing Best Practices — Presentations, Research Docs, Reports, Papers, Guides, Tutorials"
category: research
tags: [research]
status: complete
source: "codex-research-gpt-5.6-sol-2026-09-02"
template_version: "research-1.2.0"
delegation_provenance:
  version: 2
  contributors: []
---
<!-- Canonical Jinja source: STUB.md.jinja. This direct-copy stub is retained for consumers that have not migrated to rendering it. -->

# Outline-Writing Best Practices — Presentations, Research Docs, Reports, Papers, Guides, Tutorials

## Founding Ask Coverage

`founding_ask_ref`: N/A — ad-hoc claude-adhoc run, no framework-core DB registry. The founding ask is the
verbatim block embedded in `## Appendix: Research Prompt` below.

This document covers the founding ask's outline-craft research request specifically: best
practices for writing effective outlines across presentations, research docs, reports, technical
review papers, guides, and tutorials — structure patterns (introductory survey before depth),
progressive disclosure, and audience calibration. It feeds a sibling issue (ISSUE-REF) that writes
the actual ai-harness-engineering presentation outline; that downstream content-authoring work,
and the rest of the multi-stage epic (ISSUE-REF), are out of scope for this document.

**Status:** complete

**Created:** 2026-09-02

<!-- doc:region name="context" kind="immutable" -->

## Table of Contents

<!-- FILL THIS IN as you write. The entries below are this stub's own placeholder sections, so
     they already resolve; as you add, rename, or remove a heading, update this list in the same
     edit. A ToC that lists headings the doc does not have, or omits ones it does, is worse than
     no ToC because it looks like working navigation.
     AIDO 128 / D5 (c): list EVERY `## ` and EVERY `### ` heading in the real doc, with
     GitHub-style anchors (lowercase, spaces→hyphens, punctuation stripped) so they navigate
     in-window (incl. VS Code Remote-SSH). If all-`###` proves too noisy, fall back to D5 (a)
     "meaningful `###`" — a deterministic OR-rule: include a `###` when it (1) has child `####`,
     (2) its body ≥ ~8-10 lines, (3) its parent `##` is allowlisted, or (4) matches a pattern.
     Hand-maintained: `aido toc check` (AIDO 127) has NOT shipped. Verify with
     `scripts/check_doc_toc.py <this-doc>`, which fails on an anchor that does not resolve.
     Manual `<!-- toc:skip -->` / `<!-- toc:include -->` override. -->

- [Context](#context)
- [Ambiguous Items from Auto-Remediation (Post-Run Review)](#ambiguous-items-from-auto-remediation-post-run-review)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

## Context

Craft-level research commissioned to inform ISSUE-REF (writing the actual
ai-harness-engineering presentation outline, in `/doc --kind analysis` format). Scope: what makes
an outline effective across presentations, research docs, reports, technical review papers,
guides, and tutorials — structural patterns, progressive disclosure, and audience calibration —
not the ai-harness-engineering content itself (that is covered by the epic's other shipped
analysis/synthesis/research docs).

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

This is a durable craft topic, so foundational presentation, learning, and technical-writing
sources are weighted alongside current official guidance. Sources were retrieved and checked on
2026-09-02. Current 2025-2026 guidance is used where available; older research is retained when it
is the direct evidence behind a practice.

Two boundaries matter. First, this report researches outline craft, not the subject matter of the
downstream presentation. Second, the common "10-15 minute attention span" is treated as a claim to
test, not a pacing law. The repository's Table of Contents sits inside an immutable `context`
region, so this run could not replace its stub entries; the body headings below are therefore not
reflected there. [INFERENCE]

## Executive Summary

1. **Begin with an audience-and-outcome contract, not a topic inventory.** Define what the audience
   already knows, what question or task brings them, and what they should understand, decide, or do
   afterward. Google frames useful documentation as the gap between needed and existing knowledge.
   [VERIFIABLE][^1]
2. **Use a message-evidence-dependency outline.** Each beat should state one audience-facing claim or
   task, the evidence/example that earns it, the prerequisite it depends on, the transition that
   motivates the next beat, and its time or space budget. This is a synthesis of sourced practices,
   not a named industry standard. [INFERENCE]
3. **Do not reuse one outline shape across media.** A live talk needs ruthless selection, timing,
   oral transitions, and planned shifts in audience activity; a report must stand alone, expose its
   method and evidence trail, and support nonlinear reading; a tutorial must produce a reliable
   sequence of actions and observable results. [HEURISTIC]
4. **"Survey, then depth" is a good default for mixed or novice audiences, not a universal law.**
   Use the survey to supply a map and vocabulary, then order depth primarily by prerequisites and
   causal logic. Start with the task, result, or controversy instead when the audience is already
   oriented, time is urgent, or learning requires action before explanation. [HEURISTIC]
5. **Treat the outline as a prototype that must be tested.** Review it for claim-to-evidence fit,
   rehearse talks to a clock, have representative readers attempt tutorials, and revise the outline
   when the observed journey differs from the intended one. TED explicitly separates thesis/basic
   outline, detailed outline or script, and repeated rehearsal milestones. [VERIFIABLE][^3]

## 1. Define the Audience, Outcome, and Governing Question

An outline is effective only relative to an audience and a use. Before choosing sections, write a
brief contract:

- **Audience:** roles, domain proximity, likely disagreements, and accessibility needs.
- **Starting state:** terms, tools, and context they already know; assumptions that need validation.
- **Ending state:** one primary outcome—understand, decide, compare, perform, or practice—and at most
  two secondary outcomes.
- **Governing question:** the question every major section helps answer.
- **Scope/non-scope:** expected material included and deliberately excluded.
- **Evidence bar:** what must be demonstrated, cited, reproduced, or qualified.

Google's current technical-writing course says role alone is insufficient: proximity to the
knowledge and time since exposure also affect what an audience needs. [VERIFIABLE][^1] It also
recommends explicit scope, non-scope, audience, prerequisites, and key points at the start of a
technical document. [VERIFIABLE][^2]

Convert the contract into audience questions rather than noun headings. "Tooling" is a bucket;
"Which tool families solve which jobs, and where do they fail?" is an outline unit with a testable
purpose. [HEURISTIC]

### When to use

Use this contract for every format. For a familiar internal team, it may be five lines. For a
public talk, cross-functional report, or tutorial, validate it with a representative non-author;
the author's expertise makes missing prerequisites unusually hard to see. [HEURISTIC]

## 2. Outline Presentations as Timed Audience Journeys

### Build a talk spine before a slide list

A useful talk outline has three levels:

1. **Throughline:** one sentence stating the change in understanding the talk should create.
2. **Movements:** three to five audience questions or claims that advance that throughline.
3. **Beats:** one claim, example, demonstration, contrast, or activity per row, with evidence,
   transition, modality, and estimated duration.

TED's organizer guidance requires a clear beginning, middle, and end, warns that content becomes
cramped when it exceeds the time slot, and asks every point to serve the main idea. [VERIFIABLE][^3]
The TEDx speaker guide further recommends opening from something the audience cares about, ordering
body evidence by what must be understood first, cutting aggressively, and marking outline material
that should be visual. [VERIFIABLE][^4] Those principles corroborate a claim-led outline; they do
not establish one mandatory story formula.

A transition belongs in the outline when it does logical work. Write it as a hinge—"We now have the
map; the next question is where it breaks"—rather than a decorative "next, limitations." The hinge
should close the prior question and create a reason to hear the next one. [HEURISTIC]

### Pace by cognitive action, not slide count

There is no defensible universal attention curve that says audiences stop attending after 10 or 15
minutes. A review of the underlying lecture literature found scant primary investigation,
methodological problems, and no support for a fixed 10-15 minute limit. [VERIFIABLE][^5] Therefore,
do not turn "one slide per minute" or a fixed attention-reset interval into an acceptance rule.
[INFERENCE]

Instead, mark changes in **cognitive action**: orient, predict, inspect evidence, compare, decide,
try, reflect, or ask. Use a new example, question, demonstration, brief recap, or activity when the
argument naturally changes—not as arbitrary entertainment. [HEURISTIC]

For technical slides, outline the assertion before designing the artifact that supports it. In one
controlled study of 110 engineering students, assertion-evidence slides produced better
comprehension, fewer misconceptions, lower perceived cognitive load, and stronger delayed recall
than common-practice slides for the tested instructional presentation. [VERIFIABLE][^6] This is
useful corroboration, not proof that every slide in every genre must use that design. [INFERENCE]

### Calibrate the shape to the event

- **20-30 minute internal engineering talk:** lead with the decision, problem, or payoff; spend only
  enough time on shared vocabulary to prevent confusion; choose two or three deep dives; reserve a
  landing with implications and explicit discussion prompts. A workable 25-minute planning budget
  is 2-3 minutes opening, 4-5 orientation, 11-13 depth/case, 3-4 synthesis, and 2-3 buffer. These are
  starting heuristics, not evidence-based standards. [HEURISTIC]
- **Conference keynote:** optimize for one memorable thesis and a few broad movements that travel
  across subfields. Prefer representative evidence and stories over procedural completeness; put
  technical depth in companion material. The outline should include an emotional/intellectual
  landing, but not manufacture drama. [HEURISTIC]
- **Workshop:** outline participant state changes, not lecture chapters: objective, setup, short
  model, activity, observable result, debrief, transfer, and buffer. USENIX distinguishes workshops
  as many-to-many peer work rather than one-to-many training and expects a tangible result.
  [VERIFIABLE][^7] Adjacent STEM evidence finds better learning outcomes from active learning than
  continuous exposition. [VERIFIABLE][^8] This makes an activity-dominant workshop a reasonable
  inference but does not prove a specific conference agenda. [INFERENCE]

### How much detail belongs in the outline

Record enough detail to test logic and duration: message, evidence/example, transition, visual or
demo need, risk, and budget. Put exact wording, citations, data labels, speaker notes, and asset
specifications in the script/notes or slide-production layer unless wording itself is safety- or
accuracy-critical. [HEURISTIC]

### When to use

Use the timed-journey model for any live or narrated presentation. Use a script selectively for a
high-stakes opening, exact quotation, safety warning, or landing; over-scripting every beat can make
spoken language sound written and hides whether the underlying outline works. [HEURISTIC]

## 3. Outline Research Documents, Reports, and Technical Reviews for Independent Reading

Written research artifacts can be paused, scanned, searched, and revisited. Their outline must
therefore support at least three paths: the executive reader seeking conclusions, the practitioner
seeking implications, and the reviewer auditing methods and evidence. [HEURISTIC]

### Empirical research and technical reports

For empirical work, the familiar IMRaD sequence—Introduction, Methods, Results, Discussion—separates
why the study matters, what was done, what was found, and what it means. [VERIFIABLE][^9] Use that
shape when the research process is itself load-bearing. Do not force exploratory analysis, design
rationale, or a recommendation memo into IMRaD merely because it looks scholarly. [HEURISTIC]

A report outline should normally expose:

1. executive summary with purpose, approach, findings, implications, and recommendation;
2. context, audience, scope, and governing questions;
3. method and source-selection boundaries;
4. findings organized by question or theme, not by the author's research chronology;
5. comparison or synthesis that connects findings;
6. limitations, uncertainty, and counterevidence;
7. recommendation, decision criteria, and next actions;
8. references and technical appendices.

Current UK DWP guidance requires an executive summary that is concise, independently
understandable, and covers purpose, approach, findings, and implications; it places detailed
methodological material in annexes. [VERIFIABLE][^10] This supports a stronger standalone summary
than a talk opening because absent authors cannot repair missing context. [INFERENCE]

### Technical review papers and landscape surveys

Choose the review type before outlining. A systematic review needs explicit objectives,
eligibility, search/selection, synthesis methods, results, limitations, and traceability; PRISMA
2020 supplies a 27-item reporting checklist plus abstract guidance and flow diagrams.
[VERIFIABLE][^11] A broad mapping study instead classifies and structures a field, whereas a
systematic review gathers and synthesizes evidence. [VERIFIABLE][^12]

For a practitioner landscape review that is neither systematic nor exhaustive, say so. Outline the
source window, inclusion logic, and evidence tiers; distinguish "examples found" from "the market"
and "current signals" from "future speculation." [HEURISTIC]

### Citation and evidence architecture

Attach evidence at outline time. Each claim row should name its evidence class—primary data,
official documentation, peer-reviewed synthesis, practitioner report, case study, or inference—and
its strongest counterclaim. This prevents a polished section from outrunning its sources.
[HEURISTIC]

### When to use

Use IMRaD for planned empirical studies, a transparent methods/findings/synthesis shape for research
reports, and review-specific reporting rules only when the work actually followed those methods.
Use thematic or question-led sections for decision reports whose readers need an answer rather than
a chronology. [HEURISTIC]

## 4. Outline Guides and Tutorials Around User State Changes

A guide and a tutorial are not interchangeable. Diátaxis distinguishes a tutorial's learning goal
from a how-to guide's task goal: the tutorial manages a safe, linear learning experience, while the
how-to may branch around real-world conditions for an already competent user. [VERIFIABLE][^13]

### Tutorials

Start with the meaningful thing the learner will build or accomplish, then sequence a golden path:
prerequisites, setup check, first visible success, small action/result loops, a worked example,
guided variation, independent application, recap, cleanup, and next steps. Keep alternatives and
extended theory out of the main path; link them as optional deep dives. [HEURISTIC]

Diátaxis recommends showing the destination, producing visible results early and often, stating
expected outputs, and testing tutorials through observation because authors cannot discover all
failure points themselves. [VERIFIABLE][^14] Put a worked example immediately before or inside the
first task that needs it, then reduce support as the learner succeeds. Exact fading schedules depend
on the skill and learner and are not established here. [HEURISTIC]

### How-to guides

Start with the user's goal and entry state, not a survey. Outline prerequisites and warnings before
irreversible steps; use one numbered action per step; place verification immediately after the
action; branch only where real conditions demand it; finish with success criteria, rollback, and
troubleshooting links. Microsoft's current style guide recommends action-oriented headings,
numbered multi-step procedures, and a separate step for each instruction. [VERIFIABLE][^15]

### Progressive disclosure without burial

High-level-first is useful when it gives readers a map, but "details later" must not hide safety
conditions, prerequisites, or decision-changing caveats. Google's technical-writing guidance
recommends introducing information when relevant, alternating concepts with application, and
sharing outlines with contributors before drafting. [VERIFIABLE][^16]

### When to use

Use a tutorial when the primary outcome is competence gained through a controlled experience. Use a
how-to guide when the reader already has baseline competence and needs a real task completed. Split
reference and conceptual explanation into linked artifacts when either would interrupt the task
path. [HEURISTIC]

## 5. Sequence from Survey to Depth Deliberately

The high-level-to-deep pattern works when the audience lacks a shared map, the field contains many
categories, or later comparisons depend on common terms. The introduction should answer: what is
this landscape, why does it matter, what dimensions organize it, and what will be deliberately left
out? [HEURISTIC]

After orientation, order deep dives with this priority:

1. **Dependency:** concepts needed to understand later concepts.
2. **Decision or task flow:** the order in which the audience must choose or act.
3. **Causal chain:** how inputs, mechanisms, outcomes, and failure modes relate.
4. **Risk:** safety-critical or misconception-correcting material before optional sophistication.
5. **Interest and surprise:** use as a tie-breaker, not a substitute for coherence.
6. **Complexity:** increase gradually only when complexity roughly tracks dependency.

This ordering reconciles TED's dependency-first evidence advice with audience-centered technical
writing. [VERIFIABLE][^1][^4] It is a derived sequencing rule, not a published standard.
[INFERENCE]

### When survey-first is wrong

Do not begin with a landscape survey when:

- an expert audience already shares the map;
- an incident, decision, or operational task makes the answer urgent;
- a tutorial needs concrete action to create the concepts;
- a contrarian result is the reason the audience came;
- the survey would consume the time needed to demonstrate the thesis.

In those cases, use **answer-first then map**, **problem/case first then generalize**, or **do first
then explain**. The tutorial/how-to distinction specifically contradicts the idea that every guide
should begin with conceptual orientation. [VERIFIABLE][^13]

### When to use

Use survey-to-depth for introductory landscape reviews, mixed-audience briefings, and long-form
conceptual guides. Use answer-first for decision memos and internal expert talks; task-first for
how-to guides; case-first when one concrete failure or implementation supplies the need for the
framework. [HEURISTIC]

## 6. Structure a Technical Landscape Review and Integrate Personal Work

### A reusable landscape-review spine

A strong landscape outline can follow this sequence:

1. **Need and scope:** the audience problem, boundary, date, and evidence method.
2. **Orientation:** definitions, history only where explanatory, and an organizing model.
3. **Map:** categories and representative approaches, with explicit selection limits.
4. **Deep dives:** mechanisms ordered by dependency or decision relevance.
5. **Tools, concepts, practices, and standards:** compare on consistent criteria; do not mix
   normative standards with popular implementations.
6. **Trends and future directions:** separate observed trajectories, forecasts, and speculation.
7. **Limitations and challenges:** technical, operational, human, economic, and evidence limits.
8. **Case study:** test the map against one implementation or episode.
9. **Synthesis:** durable principles, contingent choices, disagreements, and gaps.
10. **Recommendation or next questions:** what the audience should do or investigate next.

Mapping research supports using classification to structure a broad field. [VERIFIABLE][^12] A
non-systematic presentation should not claim the completeness or rigor of a systematic map.
[INFERENCE]

### Make personal work evidence, not an autobiographical detour

Place personal work after the audience has the general model and comparison criteria. Introduce it
with a bridge such as, "What happens when these constraints meet a real implementation?" Then use
the same frame as the landscape:

- problem and constraints;
- relevant design choices and alternatives rejected;
- evidence of outcomes, including what failed;
- mapping back to the general categories;
- lessons that may transfer;
- limits that prevent generalization.

TED's guide tells speakers to limit anecdotal evidence and avoid focusing too much on themselves.
[VERIFIABLE][^4] Therefore, a case study earns its place by testing or complicating the thesis, not
by establishing the presenter's credentials. [INFERENCE]

No general empirical rule was found for the case study's share of a technical talk. [NO SOURCE]
For a 20-30 minute landscape talk, one substantive beat—roughly 10-20% of speaking time—is a
reasonable planning heuristic; expand it only when the talk is explicitly a case study.
[HEURISTIC]

### When to use

Use this spine for technology surveys, state-of-practice reports, and introductory technical review
talks. Move the case earlier when it is the hook or problem statement, but return immediately to the
general framework so the audience knows why the example matters. [HEURISTIC]

## 7. Treat the Outline as a Testable Prototype

Use an outline matrix rather than nested bullets alone:

| Beat | Audience question | Claim / task | Evidence / example | Dependency | Transition | Modality | Budget | Risk |
|---:|---|---|---|---|---|---|---:|---|
| 1 | Why care? | Consequence or opportunity | Data, scenario, or demo | None | Creates governing question | Spoken/visual | Time/pages | Overclaim |
| 2 | What is it? | Shared model | Diagram or definition | Beat 1 | Enables comparison | Visual/text | Time/pages | Jargon |
| 3 | What follows? | Decision, task, or deep dive | Evidence/worked example | Beat 2 | Exposes tradeoff | Mixed | Time/pages | Missing counterevidence |

Run four passes:

1. **Logic:** Can every beat be phrased as an answer, action, or necessary question? Does each
   transition explain why the next beat follows?
2. **Evidence:** Does every externally checkable claim have support? Are counterevidence,
   uncertainty, and limitations placed near the claim rather than quarantined at the end?
3. **Budget:** For talks, rehearse aloud and cut from measured duration; for documents, identify the
   short path and move audit detail to linked sections or appendices.
4. **User test:** Ask a representative person to predict the destination from headings, identify
   missing prerequisites, and attempt any procedures. Revise the outline, not only the prose.

Google recommends sharing a high-level outline before drafting; TED uses staged outline, script,
and rehearsal milestones; Diátaxis says tutorial flaws require testing and observation.
[VERIFIABLE][^3][^14][^16] Together these support iterative outline validation across formats,
although no single source defines this four-pass method. [INFERENCE]

### Gaps, blindspots & emergent findings

- **Audience research is upstream of structure.** An elegant sequence can still fail if the assumed
  starting state is wrong; lightweight interviews, prior-question review, or a pilot outline may
  change the architecture before drafting. [HEURISTIC]
- **Nonlinear access changes the unit of coherence.** Reports and reference material need meaningful
  headings, local context, and links because readers may enter mid-document; a live talk can rely
  more heavily on accumulated context. [INFERENCE]
- **Accessibility can change the artifact plan.** If meaning exists only in a visual or live demo,
  the outline should reserve an equivalent verbal or textual path. [HEURISTIC]
- **Exact pacing norms are weakly grounded.** No authoritative cross-format evidence was found for
  universal slide counts, section percentages, or case-study ratios. [NO SOURCE]
- **Outline/version drift is a production risk.** Once drafting begins, claims and assets can move
  without the outline changing; a final outline-to-artifact reconciliation prevents missing
  transitions, orphan evidence, and stale timing. [HEURISTIC]

### When to use

Use the matrix for complex, collaborative, evidence-heavy, or time-bounded work. A short solo memo
may need only audience, outcome, claim order, and evidence, but should still pass logic and scope
checks. [HEURISTIC]

## 8. Anti-Patterns and Failure Modes

1. **Topic-bucket outline:** headings name subjects but make no claim and create no progression.
   *Repair:* rewrite each heading as the audience question or takeaway.
2. **Table-of-contents theater:** hierarchy is polished before purpose, evidence, and dependencies
   are known. *Repair:* build the audience contract and claim map first.
3. **Encyclopedic opening:** history, taxonomy, and definitions consume the attention needed for the
   actual thesis. *Repair:* include only orientation required by later beats.
4. **One-outline-fits-all:** a report is read aloud as slides, or a tutorial becomes a conceptual
   essay. *Repair:* redesign around the medium's user behavior.
5. **False attention engineering:** arbitrary slide changes or fixed 10-minute resets replace
   meaningful variation. *Repair:* pace changes in cognitive action and rehearse with real users.
6. **Evidence afterthought:** citations are added once claims are fixed. *Repair:* attach evidence
   and counterevidence in the outline matrix.
7. **Taxonomy as argument:** listing tools or standards is mistaken for explaining significance.
   *Repair:* compare with stable criteria, then synthesize implications.
8. **Trend laundering:** current observations, forecasts, and aspirations share one section and
   acquire equal certainty. *Repair:* label each evidence class and horizon.
9. **Case-study vanity island:** personal work interrupts the landscape and has unique evaluation
   criteria. *Repair:* use it as a test of the same framework and disclose transfer limits.
10. **Tutorial branch explosion:** options, theory, and edge cases obstruct the first successful
    path. *Repair:* preserve a golden path and link optional depth.
11. **Buried constraints:** prerequisites or limitations appear after the action they govern.
    *Repair:* place decision-changing information at its point of use.
12. **Unrehearsed completeness:** the outline fits on paper but not in time. *Repair:* rehearse,
    reserve buffer, and cut whole claims before compressing every explanation.

These names are synthesis labels for recurring craft failures, not standardized terms.
[INFERENCE]

### When to use

Run this list as a red-team pass after the first complete outline and again after slide, prose, or
exercise production. The second pass catches drift introduced by attractive assets and late
research. [HEURISTIC]

## Comparison

| # | Criterion | 20-30 min internal talk | Conference keynote | Workshop | Research report / review | How-to guide | Tutorial |
|---:|---|---|---|---|---|---|---|
| 1 | Primary outcome | Shared understanding or decision | Memorable thesis and reframing | Participant-produced result | Auditable findings and implications | Complete a real task | Gain competence through a guided experience |
| 2 | Core unit | Timed claim/evidence beat | Narrative movement | Activity/debrief loop | Question, method, finding, synthesis | Action/verification step | Action/result/reflection loop |
| 3 | Default order | Answer → map → selected depth → action | Hook → thesis movements → landing | Orient → do → inspect → adapt | Summary → context/method → findings → limits → recommendation | Goal → prerequisites → steps → verify → troubleshoot | Destination → setup → early win → guided build → independent try |
| 4 | Detail in outline | Claim, evidence, transition, visual, minutes | Thesis, story/evidence, emotional and logical turns | Materials, instructions, outputs, debrief, buffer | Heading claims, sources, method, navigation, appendices | Preconditions, branches, warnings, rollback | Expected outputs, checkpoints, likely failures, optional depth |
| 5 | Evidence handling | Visible but flow-preserving | Representative; companion sources for depth | Evidence embedded in model/debrief | Traceable citations and explicit uncertainty | Evidence subordinate to safe task completion | Explanation at point of need; links for depth |
| 6 | Pacing control | Rehearsed time budget | Movement-level rhythm | Activity time and recovery buffer | Reader-controlled; optimize scan paths | Task latency and step count | Learner feedback and visible progress |
| 7 | Main failure | Too much breadth for the slot | Inspiration without substance | Lecture disguised as participation | Method/evidence buried or summary not standalone | Conceptual detours block the task | Choices and theory break the golden path |

The comparison is a synthesis of the source-specific distinctions above. [INFERENCE]

## Recommendation

Adopt a **message-evidence-dependency outline** as the common intermediate format, then render it
into a medium-specific structure. [HEURISTIC]

For the downstream introductory technical landscape presentation, use this sequence:

1. audience decision and one-sentence throughline;
2. relevant hook or consequence;
3. minimal shared vocabulary and landscape map;
4. two or three dependency-ordered deep dives;
5. consistent comparison of tools, concepts, practices, and standards;
6. observed trends separated from forecasts;
7. limitations and counterarguments placed near the claims they constrain;
8. personal case study framed as a test of the general model;
9. synthesis into durable principles and contingent choices;
10. explicit next action, discussion questions, and source/technical appendix.

For each beat, require: **audience question → claim/task → evidence/example → dependency →
transition → modality → budget → uncertainty**. Cut any beat that does not advance the governing
question. Rehearse the talk to validate time and transitions; separately audit the written outline
for standalone context, citations, and accessible alternatives. [HEURISTIC]

The recommended source format is Markdown for the canonical outline because it is diffable,
searchable, linkable, and compatible with analysis-document structure; generate HTML or slides as
derived presentation artifacts once the outline stabilizes. This format recommendation is based on
workflow fit, not comparative empirical evidence. [INFERENCE]

## Open Questions

1. What is the actual audience mix, prior knowledge, and desired decision for the first delivery?
2. Is the first artifact a 20-30 minute internal talk, a longer workshop, or a reusable written
   landscape report? The outline cannot optimize all three equally.
3. Which claims require live demonstrations, and what static fallback will preserve their meaning?
4. Will the personal case study have outcome evidence and reusable lessons, or should it remain a
   short illustrative example?
5. Which review claim—representative survey, systematic map, or systematic review—can the source
   method honestly support?
6. How will rehearsal and representative-reader feedback be captured and reconciled before the
   outline is converted into slides?
7. Should tooling move the Table of Contents outside the immutable `context` region so a scoped body
   research run can keep navigation synchronized?

## Sources

[^1]: Google. (2025). [Audience](https://developers.google.com/tech-writing/one/audience). Technical Writing for Developers. Verified accessible (HTTP 200) 2026-09-02. (Audience roles, proximity, existing knowledge, and target tasks.)

[^2]: Google. (2025). [Documents](https://developers.google.com/tech-writing/one/documents). Technical Writing for Developers. Verified accessible (HTTP 200) 2026-09-02. (Scope, audience, prerequisites, opening summary, and audience-led organization.)

[^3]: TED. (n.d.). [Outline + script](https://www.ted.com/participate/organize-a-local-tedx-event/tedx-organizer-guide/speakers-program/prepare-your-speaker/outline-script). TEDx Organizer Guide. Verified accessible (HTTP 200) 2026-09-02. (Iterative drafting, time-slot fit, structure, brevity, and purpose.)

[^4]: TED. (n.d.). [TEDx Speaker Guide](https://storage.ted.com/tedx/manuals/tedx_speaker_guide.pdf). TED. Verified accessible (HTTP 200) 2026-09-02. (Audience-relevant openings, dependency ordering, evidence selection, visual planning, conclusions, and limits on anecdote.)

[^5]: Bradbury, N. A. (2016). [Attention span during lectures: 8 seconds, 10 minutes, or more?](https://pubmed.ncbi.nlm.nih.gov/28145268/). *Advances in Physiology Education, 40*(4), 509-513. Verified accessible through PubMed (HTTP 200) 2026-09-02. (Critical review of the claimed 10-15 minute attention limit; DOI metadata: https://doi.org/10.1152/advan.00109.2016.)

[^6]: Garner, J. K., & Alley, M. P. (2013). [How the design of presentation slides affects audience comprehension: A case for the assertion-evidence approach](https://www.ijee.ie/articles/Vol29-6/23_ijee2791ns.pdf). *International Journal of Engineering Education, 29*(6), 1564-1579. Verified accessible (HTTP 200) 2026-09-02. (Controlled comparison involving 110 engineering students.)

[^7]: USENIX. (2016). [Workshopping: What is a workshop?](https://www.usenix.org/conference/lisa16/workshopping-what-is-a-workshop). LISA16. Verified accessible (HTTP 200) 2026-09-02. (Many-to-many workshop form and tangible outputs.)

[^8]: Freeman, S., Eddy, S. L., McDonough, M., Smith, M. K., Okoroafor, N., Jordt, H., & Wenderoth, M. P. (2014). [Active learning increases student performance in science, engineering, and mathematics](https://pmc.ncbi.nlm.nih.gov/articles/PMC4060654/). *Proceedings of the National Academy of Sciences, 111*(23), 8410-8415. Verified through the PubMed Central index 2026-09-02; direct full-page retrieval presented a reCAPTCHA. (Meta-analysis of 225 undergraduate STEM studies; https://doi.org/10.1073/pnas.1319030111.)

[^9]: George Mason University Writing Center. (n.d.). [Scientific (IMRaD) research reports — overview](https://writingcenter.gmu.edu/writing-resources/imrad/writing-an-imrad-report). George Mason University. Verified accessible (HTTP 200) 2026-09-02. (Purpose and sequence of Introduction, Methods, Results, and Discussion.)

[^10]: Department for Work and Pensions. (2026). [DWP research report guidance](https://www.gov.uk/government/publications/dwp-research-reports-style-guide/dwp-research-reports-guidance). GOV.UK. Verified accessible (HTTP 200) 2026-09-02. (Standalone executive summaries, user-centered report structure, and technical annexes.)

[^11]: PRISMA. (2021). [PRISMA 2020 statement](https://www.prisma-statement.org/prisma-2020). PRISMA Statement. Verified accessible (HTTP 200) 2026-09-02. (Systematic-review checklist, abstract checklist, and flow diagrams.)

[^12]: Petersen, K., Vakkalanka, S., & Kuzniarz, L. (2015). [Guidelines for conducting systematic mapping studies in software engineering: An update](https://doi.org/10.1016/j.infsof.2015.03.007). *Information and Software Technology, 64*, 1-18. Abstract and metadata verified via the publisher index 2026-09-02; direct DOI retrieval returned HTTP 403. (Mapping structures a field; systematic review synthesizes evidence.)

[^13]: Procida, D. (n.d.). [The difference between a tutorial and how-to guide](https://www.diataxis.fr/tutorials-how-to/). Diátaxis. Verified accessible (HTTP 200) 2026-09-02. (Learning versus task orientation, linear tutorial path, and real-world how-to branching.)

[^14]: Procida, D. (n.d.). [Tutorials](https://diataxis.fr/tutorials/). Diátaxis. Verified accessible (HTTP 200) 2026-09-02. (Visible outcomes, expected results, concrete steps, and observational testing.)

[^15]: Microsoft. (2026). [Writing step-by-step instructions](https://learn.microsoft.com/en-us/style-guide/procedures-instructions/writing-step-by-step-instructions). Microsoft Writing Style Guide. Verified accessible (HTTP 200) 2026-09-02. (Action headings, numbered procedures, and atomic steps.)

[^16]: Google. (2025). [Organizing large documents](https://developers.google.com/tech-writing/two/large-docs). Technical Writing for Developers. Verified accessible (HTTP 200) 2026-09-02. (High-level outlining, point-of-need information, concept/application alternation, and contributor review.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** N/A -- ad-hoc claude-adhoc run, no framework-core DB registry
**Model:** codex/research (gpt-5.6-sol, flagship, high effort)
**Date:** 2026-09-02

````text
## FOUNDING ASK — fidelity tier 1

verbatim_exact_ask:
```text
do we have a /beads-task and cc task to create the ai harness engineering outline html / markdown doc first? what file format do you recommend? if we haven't done that yet, then go ahead and /delegate /codex to write that outline doc and have it confirm to the /doc --kind analysis doc template. but actually first, can you /delegate to /codex to /research-doc best practices for how to write a good presentation outline and good outlines in general for like research docs and reports and technical review papers and guides and tutorials and any other document formats that are relevant to what we're doing here. i think we're still high level and introductory and do a survey and intro etc and then proceed to go more in depth and into it and how to do it and the various topics and whatnot. and also go over popular open source tools and concepts and best practices and industry standards and the trends and future direction of ai harness engineering and explain limitations and challenges etc. and anything else that we can think of. and also maybe discuss some of the stuff i've built personally. though we'll see if my open sourcing of my ai-chassis repo will be ready in time. hopefully it will be but otherwise we can just summarize what all features and capabililities etc that ive build. so add all these notes to the /beads-task(s) and cc tasks for both the presentation outline and the actual presentation tasks themselves and ship chose changes and then launch this /delegate /codex /research-doc first, then we'll launch the /delegate /codex agent to write the presentation outline based on the general TEMPLATE formatting i have for /doc --kind analysis but with all the findings and info and best practices etc that we've learned from the presentation outline resaerch doc. after that. we'll see if knowledge curation repo is refreshed and if so, we can launch agents for that (wait for me to review what we have for kc repo and i'll tell you how to proceed on that, but just remind me to check in with ISSUE-REF cc session to see where theyre at with everything) and then we can start working on the presentation harness and skill and tooling etc and then we'll work on the presentation itself. so that's the order. update the /beads-task and cc task dependencies (the kc refresh isn't a dependency but we'll just have a checkpoint after we finish the presentation outline to see if that's ready to follow-up on). and ship all those changes and then we'll go with this plan. make sure this plan / order etc is documented with the beads and cc task dependencies and also a comment or updated description etc for the peic /beads-task itself so we remember.
```

## STAGE FRAMING — non-authoritative

## Task

Write your findings directly into the existing research-STUB document at:
docs/research/outline-writing-best-practices.md

That file is scaffolded from ai-harness's canonical research STUB (template_version
research-1.2.0, region markers intact). Fill the `body` region (between
`<!-- doc:region name="body" kind="replaceable" -->` and the matching close tag) following the
canonical structure at docs/research/TEMPLATE.md (read it first): Temporal Scope, Executive
Summary (3-5 numbered findings), topic sections (numbered `## 1.`, `## 2.`, etc., each with a
`### When to use` subsection), `## Comparison` (numbered `#` column) where genuinely
comparison-shaped, `## Recommendation`, `## Open Questions`, `## Sources` (GFM footnote
definitions, APA-shaped). Do not touch the `context`, `appendix_research_prompt`, or
`ambiguous_items` regions -- those are pre-filled/immutable or owned by tooling. Append a
`## Run History` row inside the `run_history` region. Do not remove any region markers.

## Scope

This is craft-level research, not content research: what makes an outline effective, not what an
ai-harness-engineering presentation should say. Cover:

1. Best practices for outlining PRESENTATIONS specifically (slide decks and live talks) --
   pacing, the opening hook, section transitions, how much detail belongs in an outline vs. the
   eventual slide content, audience-attention curves, and how outline structure differs for a
   ~20-30 minute internal engineering team talk vs. a conference keynote or a workshop.
2. Best practices for outlining RESEARCH DOCS, REPORTS, and TECHNICAL REVIEW PAPERS -- how these
   differ from presentation outlines (denser, read at the reader's own pace, need a stronger
   standalone executive summary, citation/evidence structure).
3. Best practices for outlining GUIDES and TUTORIALS -- task-oriented structure, progressive
   skill-building, worked-example placement, where to put prerequisites vs. deep-dive material.
4. The specific pattern of starting HIGH-LEVEL/INTRODUCTORY (a survey and orientation) before
   progressing into DEPTH (the how-to, the various sub-topics, and so on) -- when this pattern is
   the right choice, when it isn't, and how deep-dive material should itself be sequenced once you
   are past the introduction (by increasing complexity? by dependency order? by audience-interest
   ranking? something else?).
5. Techniques for organizing content that covers: an overview/survey of a landscape, existing
   tools/concepts/best-practices/industry-standards, current trends and future direction, and
   known limitations/challenges -- essentially the shape of a technical landscape-review outline
   (survey -> deep dive -> tooling/standards -> trends -> limitations), since that is close to
   the shape of the ai-harness-engineering presentation this research feeds.
6. How to fit a personal-work / case-study section (e.g., "here's what I built myself") into a
   landscape-review outline without it reading as a tangent -- placement, framing, and how much
   depth is appropriate relative to the rest of the material.
7. Common outline-writing anti-patterns and failure modes across these document types (in general
   craft/communication research, not specific to any one company or tool) -- and how to avoid them.

**Read this repo's own shipped analysis/research docs for context on the CONTENT this outline will
eventually organize** (do not re-derive their findings, just skim for orientation):
docs/analysis/2026-09-02-harness-engineering-synthesis.md and
docs/research/harness-engineering-landscape-2026.md. Do not write about ai-harness-engineering
content in this doc -- that belongs to the sibling issue (ISSUE-REF) that will read both this
doc and those two.

The adjacent internal doc docs/research/effective-essay-writing-craft.md (in the example-user repo, not
this one) may be tangentially relevant background on writing craft generally -- skim it if useful,
but this doc's job is outline structure specifically, not essay prose craft.

<grounding_instructions>
You are a technical communication specialist and former conference-talk coach who has structured
hundreds of technical presentations, research reports, and long-form guides for engineering
audiences. You have strong, evidence-backed opinions about outline craft and say so explicitly
when you cannot find a source.

Temporal scope: this is a craft/communication-practices topic, not a fast-moving technology
topic -- there is no hard 2026 cutoff requirement. Prefer well-established, durable guidance
(classic technical-writing and presentation-design sources) over chasing only the newest blog
posts, but do incorporate genuinely current perspectives (2024-2026) where they add real value,
particularly on presenting AI/ML/engineering topics to technical audiences.

Before generating your final output, execute a Chain-of-Verification (CoVe) to ensure factual
fidelity over compliance.

Inside your thought process:
1. Isolate the core facts required.
2. Draft a tentative response.
3. Hostile Cross-Examination: flag any claim where you are citing a source because the prompt
   implied you should, rather than because you verified it.
4. Strip away any claim that cannot be empirically verified.

When generating your final output, classify every major claim. Write your rationale before
appending the tag -- writing the tag first causes post-hoc rationalization. Rationale -> evidence
check -> tag.

- [VERIFIABLE]: backed by documentation, peer-reviewed research, or official tech blogs. Carry an
  inline footnote ref to the source: [VERIFIABLE][^N].
- [HEURISTIC]: widely accepted best practice without a specific citation.
- [INFERENCE]: a logical conclusion drawn from context. Provide your reasoning in-text. Do not
  fabricate a source. Tier tag only -- NO footnote ref.
- [NO SOURCE]: explicitly state when you cannot find verifiable data. Tier tag only -- NO
  footnote ref.

Citation format (mandatory for every externally-sourced claim):
- Inline: append the GFM footnote ref directly after the tier tag -- [VERIFIABLE][^N]. A claim
  citing multiple sources carries ascending separate refs -- [VERIFIABLE][^3][^7] (never grouped
  [^3,^7]; never out of order).
- Footnote definitions live once, under ## Sources, in APA form with a clickable URL or DOI and an
  access-verification stamp. Worked example:
    [^1]: LangChain. (2026). [Threads](https://docs.langchain.com/langsmith/threads). LangSmith
    Documentation. Verified accessible (HTTP 200) 2026-06-03. (Scope note.)
- URL-or-DOI ALWAYS: every source entry carries a clickable URL or a doi.org link -- paywalled/
  gated is fine (link it anyway; stamp the access status). Only the truly-irreducible case (no
  online catalog presence anywhere) gets an explicit [no online source located] marker with a
  one-line justification.
- Integrity: footnote refs are contiguous from [^1], every [^N] ref has a matching definition, and
  every definition is referenced -- no gaps, no orphans.
- [INFERENCE] / [NO SOURCE] claims carry the tier tag with NO footnote ref.

Hard constraint (overrides all formatting preferences): never invent a citation to satisfy a
formatting instruction. Accuracy > completeness.

When evaluating an externally sourced idea, feature, methodology, or pattern for adoption, apply
docs/procedures/source-vetting-and-corroboration.md: record concrete adoption/maintenance signals
or independently corroborate the underlying pattern. If neither route passes, call it exploratory
rather than an established best practice.

Format diagrams using Mermaid.js or ASCII. Format math using LaTeX. NEVER generate binary images.
</grounding_instructions>

## Scope note -- questions, examples, and named references are a starting point, not a checklist
The topics listed above are illustrative anchors and a FLOOR for this research -- not an
exhaustive list to answer only or evaluate only. Reason independently: survey outline-craft
guidance broadly, follow the evidence where it leads, expand scope where warranted, and surface
relevant techniques and failure modes not named here. Actively resist answering only the listed
items -- an output that merely fills in the listed topics has NOT met the research goal.

## Independent exploration (gaps, blindspots, emergent threads) -- required
Treat the topic list as a FLOOR, not a ceiling. As you research, actively surface what this
framing may be missing and pursue each promising thread to a logical conclusion:
- Adjacent or upstream factors the topics don't capture (e.g. audience research, feedback loops,
  iterative outline revision).
- Contrarian / disconfirming evidence -- report it even when it challenges the premise (e.g.
  cases where the "survey then depth" pattern is actively the wrong choice).
- Known failure modes and second-order effects of poor outlining.
Whenever a load-bearing thread surfaces mid-research, follow it to its conclusion and report it in
a dedicated "Gaps, blindspots & emergent findings" subsection.

## Purpose

Feeds ISSUE-REF, which writes the actual ai-harness-engineering presentation outline as a
`/doc --kind analysis`. Extract genuinely actionable structural guidance -- concrete outline
patterns, sequencing heuristics, and named anti-patterns -- not a restated table of contents of
whatever sources you find.


## ALIGNMENT OBLIGATION

Preserve the exact ask and surface any conflict.
````

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

The ledger records the evidence boundary for each externally sourced proposition. Quotes are short
substrings from pages fetched in this run; verdicts assess only the claim stated.

| Claim | Source URL | Verbatim quote | Verdict | Live? |
|---|---|---|---|---|
| Audience design must consider existing knowledge, role, and subject proximity. | https://developers.google.com/tech-writing/one/audience | “knowledge and skills your audience needs to do a task − your audience's current knowledge and skills” / “Roles, by themselves, are insufficient” | SUPPORTED | HTTP 200 |
| Technical documents should expose scope, audience, prerequisites, and essential points early. | https://developers.google.com/tech-writing/one/documents | “A good document begins by defining its scope.” / “ensure that the start of your document answers your readers' essential questions” | SUPPORTED | HTTP 200 |
| Talk development should iterate from thesis/basic outline through detailed outline or script and rehearsal. | https://www.ted.com/participate/organize-a-local-tedx-event/tedx-organizer-guide/speakers-program/prepare-your-speaker/outline-script | “Thesis and basic outline due” / “A script or detailed outline due” / “first rehearsals” | SUPPORTED | HTTP 200 |
| TED recommends audience-relevant openings, dependency ordering, aggressive cutting, and visual planning in the outline. | https://storage.ted.com/tedx/manuals/tedx_speaker_guide.pdf | “Draw in your audience members with something they care about.” / “Order all of the items ... based on what a person needs to know” / “Note anything in your outline that is best expressed visually” | SUPPORTED | HTTP 200 |
| The evidence does not support a universal 10-15 minute lecture-attention limit. | https://pubmed.ncbi.nlm.nih.gov/28145268/ | “the available primary data do not support the concept of a 10- to 15-min attention limit” | SUPPORTED | HTTP 200 via PubMed |
| Assertion-evidence slides improved measured outcomes in the reported 110-student experiment. | https://www.ijee.ie/articles/Vol29-6/23_ijee2791ns.pdf | “110 engineering students” / “superior comprehension and fewer misconceptions” / “lower perceived cognitive load” | SUPPORTED | HTTP 200 |
| USENIX distinguishes workshops from training as many-to-many work and expects tangible output. | https://www.usenix.org/conference/lisa16/workshopping-what-is-a-workshop | “many to many vs one to many presentations” / “produce tangible results” | SUPPORTED | HTTP 200 |
| Active learning outperformed continuous exposition on average in the cited undergraduate STEM meta-analysis. | https://pmc.ncbi.nlm.nih.gov/articles/PMC4060654/ | “metaanalyzed 225 studies” / “student performance increased by just under half a SD” | SUPPORTED | Indexed; direct page reCAPTCHA |
| IMRaD separates Introduction, Methods, Results, and Discussion in planned systematic research reports. | https://writingcenter.gmu.edu/writing-resources/imrad/writing-an-imrad-report | “structured by four main sections: Introduction, Methods, Results, and Discussion” | SUPPORTED | HTTP 200 |
| DWP requires a concise, independently understandable executive summary covering purpose, approach, findings, and implications. | https://www.gov.uk/government/publications/dwp-research-reports-style-guide/dwp-research-reports-guidance | “clear, stand-alone overview” / “purpose, approach, findings, and implications” | SUPPORTED | HTTP 200 |
| PRISMA 2020 provides systematic-review reporting checklists and flow diagrams. | https://www.prisma-statement.org/prisma-2020 | “checklist of items and sub-items” / “abstract checklist” / “template flow diagrams” | SUPPORTED | HTTP 200 |
| Systematic maps structure a research field while systematic reviews gather and synthesize evidence. | https://doi.org/10.1016/j.infsof.2015.03.007 | “Systematic mapping studies are used to structure a research area, while systematic reviews are focused on gathering and synthesizing evidence.” | SUPPORTED | Publisher abstract indexed; DOI HTTP 403 |
| Tutorials are learning-oriented controlled paths; how-to guides are task-oriented and may branch around real conditions. | https://www.diataxis.fr/tutorials-how-to/ | “tutorials are learning-oriented, and how-to guides are task-oriented” / “A how-to guide will typically fork and branch” | SUPPORTED | HTTP 200 |
| Tutorials should show the destination, produce visible results, state expected output, and be tested through observation. | https://diataxis.fr/tutorials/ | “Show the learner where they’ll be going” / “Deliver visible results early and often” / “extensive testing and observation” | SUPPORTED | HTTP 200 |
| Microsoft's procedure guidance recommends action headings, numbered multi-step procedures, and separate instructions. | https://learn.microsoft.com/en-us/style-guide/procedures-instructions/writing-step-by-step-instructions | “Use a numbered list.” / “Use a separate step for each instruction.” | SUPPORTED | HTTP 200 |
| Google recommends high-level outlining, point-of-need information, concept/application alternation, and contributor review. | https://developers.google.com/tech-writing/two/large-docs | “Starting with a structured, high-level outline” / “introduces information when it's most relevant” / “share the outline with your contributors” | SUPPORTED | HTTP 200 |

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

| Date | Model / effort | Scope | Result |
|---|---|---|---|
| 2026-09-02 | codex/research — gpt-5.6-sol, high | Outline craft across talks, reports, reviews, guides, and tutorials; live primary-source validation; five-perspective adversarial check and CoVe | Completed body, comparison, recommendation, contiguous citations, provenance ledger, and explicit evidence gaps; immutable regions preserved. |

<!-- /doc:region name="run_history" -->
