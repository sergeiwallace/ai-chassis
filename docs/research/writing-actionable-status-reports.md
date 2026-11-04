---
title: "Writing status reports a reader can act on: rules with checkable symptoms"
category: research
tags: [research, writing, reporting, communication, concision, status-reports]
status: complete
source: "ISSUE-REF"
template_version: "research-1.2.0"
related_docs: []
# The derived house-style rulebook and the stand-up template this research feeds live in the
# example-project repository, not here. They are named rather than linked, because a
# relative link across repositories resolves in neither a local checkout nor on the web.
---

# Writing status reports a reader can act on: rules with checkable symptoms

**Status:** complete

**Created:** 2026-08-11

**Related docs:**

- [Weekly goal-oriented progress reporting](weekly-goal-oriented-progress-reporting.md), which asks
  the adjacent question of whether a reporting habit survives at all and what this repository can
  honestly derive. This document assumes a report will be written and asks how to write it.
- [The stand-up prep template (the stand-up prep template in the example-project repository, docs/meetings/standups/TEMPLATE.md), which is where the rules derived
  here are enforced.

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. Answer first: the one convention every tradition shares](#1-answer-first-the-one-convention-every-tradition-shares)
- [2. How much to cut, and what to cut first](#2-how-much-to-cut-and-what-to-cut-first)
- [3. Making a claim actionable rather than merely true](#3-making-a-claim-actionable-rather-than-merely-true)
- [4. Named failure modes, with checkable symptoms](#4-named-failure-modes-with-checkable-symptoms)
- [5. Where the detail should go instead](#5-where-the-detail-should-go-instead)
- [6. Attributing a slip: the dependency, not the person](#6-attributing-a-slip-the-dependency-not-the-person)
- [7. The counter-case against concision](#7-the-counter-case-against-concision)
- [8. Gaps, blindspots and emergent findings](#8-gaps-blindspots-and-emergent-findings)
- [9. Where blockers belong, and how to label their severity](#9-where-blockers-belong-and-how-to-label-their-severity)
  - [Where blockers belong in the document](#where-blockers-belong-in-the-document)
  - [The dividing line between a blocker and an open item](#the-dividing-line-between-a-blocker-and-an-open-item)
  - [Severity and priority are different axes](#severity-and-priority-are-different-axes)
  - [Which label system, and why not the others](#which-label-system-and-why-not-the-others)
  - [How many bands the evidence supports](#how-many-bands-the-evidence-supports)
  - [What could not be retrieved for this pass](#what-could-not-be-retrieved-for-this-pass)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

<!-- doc:region name="context" kind="immutable" -->

## Context

This research exists because a real status document failed in front of its reader. The project owner
read it and said the information in it did not need to be communicated, that it was not useful,
actionable, digestible or concise, that it contained unnecessary detail and over-elaboration, and
that there was simply too much of it. He asked for rules, principles, rules of thumb and guidelines
covering what to do and what not to do.

The deliverable is therefore not an essay about writing. It is a set of rules concrete enough to
apply and to check, biased deliberately toward rules with an observable symptom over rules that are a
matter of taste. A rule someone can grep for survives an author who disagrees with it; a rule that
says "be concise" does not.

Scope covers status reporting to managers and executives, engineering-specific written reporting
(incidents, design documents, change descriptions, handoffs), the mechanics of concision, and the
credible case against concision.

**Primary period:** 1985-2026
**Source weighting:** primary standards and specifications first (regulations, government guidance,
published engineering practice); measured usability research second, with its age stated; single
author opinion third and labelled as such.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

This topic's foundational literature predates 2024 and the window is deliberately broad. The
answer-first convention is documented in a US Army correspondence regulation and in a 1985 business
communication text; the strongest measured evidence on concision is a 1997 usability study. Treating
those as background only would have discarded the best available evidence, so they are cited as
primary with their dates stated so a reader can discount them.

Current engineering practice (the Google SRE material and the Google engineering practices guide) is
maintained documentation rather than dated publication, retrieved 2026-08-11. Government plain
language guidance rests on a 2010 statute and was retrieved in its current 2026 form, which has
migrated hosts since publication.

No post-2024 empirical replication of the 1997 concision study was located, and that gap is a
finding rather than an omission: the most-cited quantitative claim in this field rests on a single
study of 51 users reading tourism web pages, not on executive documents.

## Executive Summary

Every writing tradition surveyed independently arrived at the same structural rule and disagrees
about almost everything else. Answer first, then support, is mandated by US Army correspondence
regulation, is the organizing claim of the dominant business-communication framework, and is the
first structural instruction in government plain language guidance [VERIFIABLE][^1][^2][^3]. That
convergence is the strongest finding here, and it is the rule most often broken by the documents this
project produces.

The strongest quantitative claim is narrower than it is usually reported. Cutting a page to roughly
half its word count measured 58 percent better on a composite usability score, and combining that cut
with scannable formatting and neutral language measured 124 percent better [VERIFIABLE][^4]. That is
a real measurement, and it is one 1997 study of 51 people reading a tourism website, not evidence
about status reports.

The most useful discovery for this project was not about concision at all. Blameless incident
practice supplies a principled basis for attributing a delay to the dependency that caused it rather
than to the person nearest the decision, and it supplies the reason: blame drives concealment, so a
document that indicts an individual degrades the honesty of every future report [VERIFIABLE][^5].

There is a genuine, unresolved conflict at the centre of this field, and it should not be smoothed
over. Measured usability research recommends bulleted lists and scannable formatting; the best-known
critique of presentation culture argues that bullet points destroy the reasoning they claim to
summarize. Both positions are credibly held and this document does not resolve them.

## 1. Answer first: the one convention every tradition shares

Four independent traditions, with no shared lineage, prescribe the same structure.

**Military correspondence.** US Army Regulation 25-50 states that "Army writing will be concise,
organized, and to the point" and names two essential requirements, the first being "putting the main
point at the beginning of the correspondence (bottom line up front)" [VERIFIABLE][^1]. The practice
is known as BLUF. A rescinded Army pamphlet gave the diagnosis behind it: "the greatest weakness in
ineffective writing is that it doesn't quickly transmit a focused message" [VERIFIABLE][^1]. The US
Department of Defense extended it to congressional correspondence in 2017, directing staff to "Give
members of Congress the Bottom Line Up Front; be direct and to the point" [VERIFIABLE][^1].

**Business consulting.** Barbara Minto, the first woman MBA hired by McKinsey, published the Pyramid
Principle in 1985. Its structure is that "ideas are organized top-down, starting with a main idea
that is a high-level summary of supporting key ideas" [VERIFIABLE][^2]. The argument for it is
cognitive rather than stylistic: people structure incoming information hierarchically anyway, so a
message shaped that way lands more clearly [VERIFIABLE][^2]. Minto also originated the MECE
constraint, that groupings be mutually exclusive and collectively exhaustive, and held that you
"can't derive an idea from a grouping unless the ideas in the grouping are logically the same, and in
logical order" [VERIFIABLE][^2].

**Government plain language.** The Plain Writing Act of 2010 established that content for the public
must be written for its specific audience [VERIFIABLE][^3]. The derived guidance instructs writers to
"Organize the information. Prepare readers for what to expect. Summarize lengthy documents up-front"
[VERIFIABLE][^3].

**Engineering change descriptions.** Google's engineering practices guide requires the first line of
a change description to be a "Short summary of what is being done", a "Complete sentence, written as
though it was an order", followed by an empty line, and states that "the first line should stand
alone, allowing readers to skim through code history much faster" [VERIFIABLE][^6].

The consequence for a status document is that section order is not a presentation choice. A document
whose decision appears after its evidence is wrong by the standard of four separate traditions, and
the symptom is mechanically checkable: find the line that states the decision or the recommendation
and count the lines above it.

## 2. How much to cut, and what to cut first

The measured evidence is a single 1997 study, and it is worth stating precisely because it is
routinely overstated.

Nielsen and colleagues built a seven-page tourism site in five versions, identical in hypertext
structure, and tested 51 experienced web users in a between-subjects design [VERIFIABLE][^4]. The
concise version cut word count to roughly half the control. Against a control normalized to 100, the
composite usability scores were: concise 158, scannable 147, objective 127, and all three combined
224 [VERIFIABLE][^4]. Reported as improvements, that is 58 percent, 47 percent, 27 percent and 124
percent [VERIFIABLE][^4]. Nineteen of twenty normalized mean scores beat the control
[VERIFIABLE][^4].

Two facts about the same body of work bear on what to cut. First, users mostly do not read: "79
percent of our test users always scanned any new page they came across; only 16 percent read
word-by-word" [VERIFIABLE][^7]. Second, the recommended target is explicit and aggressive, "half the
word count" or less compared with conventional prose [VERIFIABLE][^7].

The scannable condition is the operative detail for what to cut first. It was not achieved by
shortening sentences. It was achieved by adding bulleted lists, bold keywords, captions and headings
to the same content [VERIFIABLE][^4]. The concise condition, separately, halved the word count
[VERIFIABLE][^4]. So the two levers are structural and volumetric, and neither is sentence polishing.
Nothing in the study measured a benefit from tightening prose at constant structure and constant
length.

That is the empirical basis for a rule this project had already derived from experience: removing
whole sections is what shortens a document, and prose polishing is not. The study supports the
stronger version, that the gain comes from halving volume and from imposing scannable structure, and
supports neither the belief that careful sentences substitute for cutting nor any specific target
other than roughly half.

A caveat the authors themselves raised: the three dimensions were tested as either/or conditions
rather than gradations, and they called for follow-up varying conciseness in smaller increments
[VERIFIABLE][^4]. No such follow-up was located.

## 3. Making a claim actionable rather than merely true

The engineering literature is more concrete here than the management literature, because it names bad
examples.

Google's guide rejects "Fix bug", "Fix build.", "Add patch.", "Moving code from A to B.", "Phase 1.",
"Add convenience functions." and "kill weird URLs." with the verdict that "Although short, they do not
provide enough useful information" [VERIFIABLE][^6]. This is the crucial refinement: every one of
those is short, and every one fails. Brevity is not the property being asked for. The named property
is that "The first few words describe what the CL actually does" [VERIFIABLE][^6].

The prescribed body content is a decision aid rather than a record of work: "a brief description of
the problem that's being solved, and why this is the best approach", and, importantly, "If there are
any shortcomings to the approach, they should be mentioned" [VERIFIABLE][^6]. A description that
omits the shortcoming is not concise, it is incomplete.

Incident practice supplies the complementary test at the document level. The SRE postmortem review
criteria are a list of questions a reader must be able to answer: "Is the action plan appropriate and
are resulting bug fixes at appropriate priority?", "Are the impact assessments complete?", "Was the
root cause sufficiently deep?" [VERIFIABLE][^5]. Each is a question about consequence, not about
activity.

The same chapter supplies the blunt statement of why a report exists at all: "An unreviewed
postmortem might as well never have existed" [VERIFIABLE][^5]. A document is an instrument for
producing a reader action. That is the origin of the per-line test this project already uses, would
the reader act differently for having read this line, and the literature supports it as a document
level test as well as a line level one.

## 4. Named failure modes, with checkable symptoms

Collected here because a named failure mode is easier to avoid than an abstraction, and because a
symptom is what makes a rule enforceable. Where a name is established in a cited source it is marked;
where the name is this project's own coinage for an observed failure, that is stated rather than
dressed up as established terminology.

| # | Failure mode | Symptom you can check | Basis |
|---|---|---|---|
| 1 | Burying the decision | The recommendation or the ask appears below the first screen, or after the evidence supporting it | Contradicts the answer-first rule of four traditions [^1][^2][^3][^6] |
| 2 | Narrating activity instead of outcome | The first sentence names a file, branch, API, count or byte size before it contains a verb describing a capability | Google requires the first words to describe what the change does [^6] |
| 3 | Short but uninformative | The line is brief and still does not say what changed; "Fix bug" is the canonical instance | Named and rejected explicitly [^6] |
| 4 | Leading with method | The first clause describes how something was verified rather than what is now true; "Verified four ways: ..." | This project's coinage; consistent with [^6] |
| 5 | Omitted shortcoming | The document recommends an approach and names no drawback of it | Google requires shortcomings to be mentioned [^6] |
| 6 | Hedging away the recommendation | A decision is requested with no recommendation attached, or with a recommendation qualified into unfalsifiability | Consistent with the DoD directive to be direct and to the point [^1]; the specific rule is this project's |
| 7 | Person-attributed delay | A slip is explained by naming who had not decided, rather than the prerequisite that was not met | Blameless practice: identify contributing causes "without indicting any individual or team" [^5] |
| 8 | Detail that demonstrates effort | A passage's only function is to show that work was done; removing it changes no reader action | This project's coinage; the per-line test derives from [^5] |
| 9 | Unreviewed report | The document was written and no reader acted on it | "An unreviewed postmortem might as well never have existed" [^5] |
| 10 | Describing instead of naming | A significant thing appears without its official name, identifier or location, so it cannot be looked up | This project's coinage, from a measured failure in front of a stakeholder |

Failure modes 2, 4 and 10 were derived on this project from documents that had to be rewritten, and
they are recorded in
[the stand-up prep template (the stand-up prep template in the example-project repository, docs/meetings/standups/TEMPLATE.md) with the verbatim before and after
text. They are listed here as observed rather than as established terminology because no external
source naming them was located.

## 5. Where the detail should go instead

The instruction to cut is not an instruction to destroy evidence, and the literature is unusually
direct about the tension.

Google's guide recommends linking out: "include background information such as bug numbers, benchmark
results, and links to design documents" [VERIFIABLE][^6]. In the same passage it warns against
relying on that link, because external links may later become inaccessible, so a writer should embed
enough context in the description itself [VERIFIABLE][^6].

Those two instructions pull against each other and the guide does not resolve the tension. It is a
real constraint rather than a contradiction to be argued away: a link relocates detail without
guaranteeing it stays reachable. The practical reading, which is inference rather than a sourced
claim, is that the load-bearing fact belongs in the document and the reproduction of it belongs in
the link. A measurement that a decision turns on is not detail; the method by which it was measured
is [INFERENCE].

Plain language guidance adds two structural devices for holding detail without spending prose on it.
Lists "can help you convey detailed information quickly without overloading your readers", and tables
"can be effective for more than just showing numerical data" [VERIFIABLE][^3]. A table is the cheapest
way to preserve a set of measurements while removing the paragraphs that narrated them.

## 6. Attributing a slip: the dependency, not the person

This section was added mid-research because the question arose live, and the answer turned out to be
better sourced than expected.

Google's SRE postmortem chapter states that to be blameless a document "must focus on identifying the
contributing causes of the incident without indicting any individual or team" [VERIFIABLE][^5]. Its
justification is not politeness. It is that blame changes what gets written down: the risk of a
blame-oriented culture is "a culture in which incidents and issues are swept under the rug"
[VERIFIABLE][^5]. And it draws the operative distinction plainly: "You can't 'fix' people, but you can
fix systems and processes" [VERIFIABLE][^5].

Two further clauses matter because they block the obvious misreading. Blamelessness is explicitly not
softness: a postmortem "should call out where and how services can be improved", and it "assumes that
everyone involved in an incident had good intentions and did the right thing with the information they
had" [VERIFIABLE][^5]. So the rule is not to omit the failure. It is to attribute it to the mechanism.

Applied to status reporting, this yields a rule with a testable symptom and a real justification. A
delay attributed to a person is usually an unverifiable claim about intent or attention; the
prerequisite that was not met is usually a checkable fact about sequencing. Preferring the checkable
attribution is better evidence and better practice at the same time, which is why this is not a
politeness rule wearing a citation.

The honest limitation: the cited source is about incidents, not status reports, and it addresses
naming individuals as causes rather than naming decision-holders as accountable owners. Management
literature on accountability plausibly cuts the other way, and no source was located that resolves
the two. A status document still has to name who owns an open decision, because otherwise nobody
does. The distinction this research supports is between naming an owner of a live decision, which is
necessary, and explaining a past slip by who had not decided, which is both unkind and weaker
evidence than the dependency account.

## 7. The counter-case against concision

Stated as strongly as the sources support, because a document that surveys only the pro-brevity side
would be an instance of the failure it is trying to prevent.

**The measured study recommends the thing the best-known critique condemns.** Nielsen's scannable
condition improved usability 47 percent by adding bulleted lists and bold keywords [VERIFIABLE][^4].
Tufte's argument runs the opposite way, that the bulleted, hierarchical presentation format degrades
the reasoning it purports to convey. The two are not reconcilable by careful reading; they are
different claims about different artifacts measured different ways, and this document leaves the
conflict open rather than picking the side that suits its thesis. See the gaps section for the
sourcing status of the Tufte half.

**Concision and auditability are in genuine tension, and the tension is sourced.** Google requires
shortcomings to be stated [VERIFIABLE][^6] and requires enough embedded context to survive a dead link
[VERIFIABLE][^6]. Both instructions make documents longer. A document optimized purely for a reader
who stops after one screen will fail a later reader who needs to know why a decision was made.

**The empirical base is thin and old.** The most-quoted numbers in this field come from one 1997 study
of 51 participants reading a purpose-built tourism website, with professional writers and designers
screened out, and with the conditions tested as either/or rather than as gradations
[VERIFIABLE][^4]. Generalizing from that to an engineering status document read by a project owner is
an extrapolation across audience, genre, stakes and thirty years. It is the best available evidence
and it is weak evidence.

**Nothing in the sourced record supports a length target for a status report.** The "half the word
count" figure is advice for web prose [VERIFIABLE][^7]. No source located prescribes a line count, a
page count or a bullet count for a status document. Any such number in this project's rules is a local
convention, and should be labelled as one rather than attributed to research.

**Brevity is not the property that made the good examples good.** This is the counter-case with the
most direct evidence behind it: every rejected change description in Google's guide is short
[VERIFIABLE][^6]. If concision were the operative virtue, they would pass. The operative virtue is
that the reader learns what changed and why it matters, and that sometimes costs words.

## 8. Gaps, blindspots and emergent findings

Recorded as null and partial results rather than smoothed over.

**Three sources could not be retrieved and their claims are consequently unsourced.** The Chronicle of
Higher Education article carrying Geoffrey Pullum's critique of "omit needless words" returned HTTP
403 to an unauthenticated fetch; the critique is real and well known but no quotation from it is made
here. Wikipedia's page on the Pyramid Principle returned HTTP 404 under the title tried, and the
Minto material cited here comes from her biography page instead, which does not cover SCQA. SCQA is
therefore named nowhere in this document's sourced claims. Two UK government pages resolved only to
redirect stubs and section landing pages, so no GDS numeric limit on sentence length is cited.

**The Tufte and Amazon counter-arguments are named without being quoted.** Tufte's "The Cognitive
Style of PowerPoint" and Amazon's narrative memo practice are both real and both material to the
bullets-versus-prose conflict in section 7. Neither was retrieved in this session, so section 7
describes the shape of the disagreement without quoting either side. A reader should treat the Tufte
half of that conflict as unverified here.

**No empirical work on status report structure was located at all.** The measured evidence found is
about web pages and about clinical handoffs; the executive-reporting literature located is
prescriptive, not tested. This is the largest gap and it means most of the confident advice in
circulation on this topic, including some in this document, is HEURISTIC rather than VERIFIABLE.

**The strongest sources are for adjacent genres.** Every primary source cited here governs something
other than a project status report: military correspondence, code change descriptions, incident
postmortems, public-facing government content. The transfer is plausible and it is a transfer.

**A blindspot this research cannot resolve.** Whether the owner's objection was really about length is
not answerable from the literature. It is equally consistent with a document of the right length whose
ordering forced him to read everything to find the three things he needed. The rules derived here
address both readings, which is a hedge, and it is stated rather than hidden.

## 9. Where blockers belong, and how to label their severity

Added in a second pass, to answer two questions the first pass did not cover: whether a dedicated
blockers section at the top of a status document has support, and which categorical severity ladder to
use.

### Where blockers belong in the document

The habit of reporting blockers comes from Scrum, and the current Scrum Guide supports it far more
weakly than the habit's popularity suggests.

The 2020 Scrum Guide mentions impediments exactly twice: the Scrum Master is accountable for "Causing
the removal of impediments to the Scrum Team's progress", and Daily Scrums "improve communications,
identify impediments, promote quick decision-making" [VERIFIABLE][^8]. Note the wording is *causing*
removal rather than removing.

**The three questions are gone.** The 2020 guide does not prescribe the "what is blocking me" question
at all; it states that "The Developers can select whatever structure and techniques they want", subject
only to a 15-minute timebox and a focus on Sprint Goal progress [VERIFIABLE][^8]. So the widely-taught
third question is inherited from an earlier version and is not current prescribed practice. Anyone
defending a blockers section by appeal to "the three questions" is citing a document that no longer says
it.

**No retrieved source places a blockers section at the top of a written status report.** Scrum's
impediments live in a meeting, not in a document. Traffic-light reporting is about aggregate status
rather than individual obstacles: a RAG rating is applied "to indicate how on track or at risk is the
project, its deliverables or tasks" [VERIFIABLE][^13].

Top placement is therefore an **inference from the answer-first rule in section 1, not a directly
sourced practice** [INFERENCE]. The reasoning is short and I think sound: four traditions require the
reader's required action to come before the evidence, and a blocker is the strongest form of required
action, because it is the one item where nothing moves until the reader or someone they can reach acts.
That makes it the highest-value content in the document, so answer-first puts it first. But it is a
derivation, and it should be labelled as one rather than presented as established practice.

### The dividing line between a blocker and an open item

The working definition under test was that a blocker is work that cannot proceed until someone outside
our control acts, whereas a still-open item is unresolved but progressable.

**The sources do not support that line, and it is worth adopting anyway.** Scrum's "impediment" is
broader: anything impeding the team's progress, with the Scrum Master accountable for causing removal
[VERIFIABLE][^8]. That plainly includes obstacles internal to the team. So the external-dependency test
is a deliberate **narrowing** of the established term, and it is local convention rather than sourced
practice [INFERENCE].

The justification for narrowing it is about what the section is for. A blockers section addressed to a
reader exists to collect the things that reader can act on. A section that also lists our own unfinished
work asks the reader for nothing and dilutes the items that do need them, which is the
narrating-activity failure mode from section 4 applied at section scale.

The consequence has to be stated plainly, because it is counterintuitive: **an item can be severe, real
and entirely ours, and still not be a blocker.** Those belong in Risks or Still open. A blockers section
that quietly absorbs our own backlog stops being a list of asks.

### Severity and priority are different axes

They are routinely used interchangeably and the sources say they are not the same thing.

Severity "is a measure of the impact the bug has" [VERIFIABLE][^11]. Priority "describes the importance
of resolving the bug in relation to other bugs", and the same source states directly that "priority is a
different aspect" [VERIFIABLE][^11]. The mechanism of divergence is that priority folds in cost: "A bug
with low severity but easy to fix may get a higher priority than a bug with moderate severity"
[VERIFIABLE][^11].

For a blockers section the reader is being asked to weigh consequence, not our sequencing. **So label
severity, defined as the consequence if nothing is done, and do not label priority**, because priority
would mix in our effort estimates and our ordering preferences, neither of which the reader is deciding
[INFERENCE].

This also means the interchangeable vocabulary in circulation, priority and impact and significance and
severity and criticality treated as synonyms, is wrong on the sources. Pick one axis and name it.

### Which label system, and why not the others

| # | System | Bands | What it is actually for | Verdict |
|---|--------|-------|-------------------------|---------|
| 1 | MoSCoW | 4 | Requirements prioritization, reaching stakeholder agreement on what ships in a timebox [^9] | **Reject.** Wrong domain, and criticized for giving no rationale why an item is a Must rather than a Should, and no way to order within a band [^9] |
| 2 | Jira default priority | 5 | Relative importance between issues in a tracker [^12] | **Reject.** Atlassian ships the five names with no descriptions at all, and defines priority as importance "in relation to other issues" [^12]. Undefined relative bands cannot be assigned consistently by two people |
| 3 | RAG / traffic light | 3 | Aggregate project, milestone or portfolio status [^13] | **Reject as the per-item ladder.** It answers how on track the whole thing is, not how bad one obstacle is. Fine for an overall header, wrong for a per-blocker label |
| 4 | PagerDuty SEV-1..SEV-5 | 5 | Live incident response, with published consequence-based definitions [^10] | **Adapt, do not adopt.** The best-defined ladder retrieved, but built for active outages: its bands are about customer-facing impact right now, not about a decision nobody has made |
| 5 | Critical / High / Medium / Low | 4 | Common defect-tracking ladder [^11] | **Chosen**, with the caveat below |

The caveat on the chosen system is important and the source is explicit: "Severity levels are not
standardized, but differ by context, such as industry and tracking tool" [VERIFIABLE][^11]. The four
familiar words therefore carry no definition on their own. They are only usable if the document defines
each band by consequence, which is what the writing standard now does.

Two devices worth taking from PagerDuty rather than reinventing. First, definitions must be concrete:
readers are "encouraged to make your definitions very specific, usually referring to a % of
users/accounts affected", and "You will usually want your severity definitions to be metric driven"
[VERIFIABLE][^10]. Second, and more useful, ambiguity is resolved by a **tie-break rule** rather than by
finer definitions: when unsure, "treat it as the higher one", on the reasoning that "During an incident
is not the time to discuss or litigate severities" [VERIFIABLE][^10]. The status-report analogue is that
the stand-up is not the time to litigate a label.

### How many bands the evidence supports

**The evidence does not settle this, and the honest answer is that no retrieved source supports a
specific number.** The closest measured literature is on survey response categories, where scales
"typically range from 2 to 10 - with 3, 5, or, 7 being the most common", and the retrieved material
contains no finding on reliability as a function of the number of categories [VERIFIABLE][^14]. Nothing
was located on inter-rater agreement degrading as bands are added, which is the question that actually
matters here.

What the evidence does support is that **band definitions carry the consistency, not band count**
[VERIFIABLE][^10]. A four-band scale with vague definitions is worse than a five-band scale with
metric-driven ones.

Four bands is therefore a local convention, chosen for two stated reasons rather than a cited one
[INFERENCE]. A blocker is never cosmetic, so PagerDuty's SEV-5 equivalent would always be empty and an
always-empty band teaches nobody anything. And an even number has no midpoint for an undecided item to
default into, which is the failure the tie-break rule otherwise has to catch.

One consequence worth stating: an empty band is informative. A blockers list with nothing in Critical is
making a claim, that nothing is currently causing loss or outage, and that claim is worth reading.

### What could not be retrieved for this pass

- **ITIL and ITSM priority as impact times urgency.** The widely-repeated formulation could not be
  sourced. Four attempts failed: two Atlassian ITSM pages returned HTTP 404, an Atlassian severity page
  and an Atlassian priority page returned only navigation with the body truncated, IBM's control-desk
  documentation returned HTTP 403, and Wikipedia's ITIL article contains no definition of priority,
  impact or urgency at all. The impact-times-urgency claim is therefore **not** asserted anywhere in
  this document [NO SOURCE].
- **PMI and PMBOK on issue versus risk versus blocker.** Not retrieved. On the material seen, "blocker"
  reads as agile and engineering vocabulary rather than PMI terminology, but that is an impression from
  absence and is not a finding [NO SOURCE].
- **RAID logs.** Wikipedia returned HTTP 404 for the article title tried, so nothing is claimed about
  RAID structure [NO SOURCE].
- **Band descriptions for Jira's five priorities.** These do not appear to exist: the administration
  page states the five names and supplies no descriptions [VERIFIABLE][^12]. That is a positive finding
  about the tool rather than a retrieval failure.

## Comparison

The three candidate rule styles, judged by whether they survive an author who disagrees.

| # | Criterion | Taste rules ("be concise") | Structural rules (section order, caps) | Symptom rules (a checkable string) |
|---|-----------|---------------------------|---------------------------------------|-----------------------------------|
| 1 | Survives a disagreeing author | no | partly | yes |
| 2 | Can be checked without judgement | no | mostly | yes |
| 3 | Can be automated | no | partly | yes |
| 4 | Catches the failure this project had | no | yes | yes |
| 5 | Risk of false positives | none | low | real, needs an escape hatch |
| 6 | Cost to write the rule | trivial | low | high, needs a measured example |
| 7 | Teaches the underlying judgement | no | partly | yes, via the example |

## Recommendation

Write rules of the third kind wherever a symptom exists, and admit when one does not.

The concrete rule set the sourced evidence supports, in the order it should be applied:

1. **Answer first.** The decision, the ask and the risk appear before the evidence. Symptom: count the
   lines above the recommendation. Basis: four independent traditions [^1][^2][^3][^6].
2. **Lead with what it does, not how it was done or how it was checked.** Symptom: a first sentence
   naming a file, branch, API, count or verification method before a verb describing a capability.
   Basis: Google's first-line rule [^6], plus three measured rewrites on this project.
3. **Cut sections, not sentences, and expect to halve the volume.** Symptom: a section no reader would
   act on. Basis: the concise condition halved word count for the largest single gain, and the
   scannable gain came from structure, not prose [^4].
4. **Every requested decision carries a recommendation and a named drawback.** Symptom: a decisions
   table row with an empty recommendation, or a recommendation with no stated cost. Basis: shortcomings
   must be mentioned [^6].
5. **Attribute a slip to the prerequisite, not to the person.** Symptom: a delay explained by who had
   not decided. Basis: blameless practice, and the concealment cost of blame [^5].
6. **Preserve the load-bearing fact in the document; move the method to a link.** Symptom: a
   measurement a decision turns on that exists only behind a link. Basis: the linking recommendation
   and its own dead-link warning [^6].
7. **Name every significant thing officially, with its identifier and a link.** Symptom: a noun phrase
   describing a system without naming it. Basis: this project's measured failure; no external source.
8. **State partial findings as both halves.** Symptom: a claim that a question is open when part of it
   is answered, or closed when part is not. Basis: the completeness criteria in postmortem review [^5].

9. **Blockers come first, and only externally-blocked work qualifies.** Symptom: a blockers item whose
   unblock is owned by us. Basis: top placement is derived from the answer-first rule [^1][^2][^3][^6];
   the external-dependency test is a deliberate narrowing of Scrum's broader "impediment" [^8] and is
   local convention.
10. **Label severity by consequence if nothing is done, in four defined bands, and never label
    priority.** Symptom: a band assigned with no stated consequence, or the words priority and severity
    used interchangeably. Basis: severity and priority are different axes [^11]; definitions must be
    specific and metric-driven, and ambiguity is settled by taking the higher band [^10]. The band count
    is local convention, since no source supports a number [^14].

Rules 7 and 8 are local conventions with no external citation and should be labelled as such wherever
they are recorded, as are the narrowing in rule 9 and the band count in rule 10. Do not attribute a
length target to research; none exists for this genre.

## Open Questions

1. Was the objection to the failed document about volume or about ordering? The two have different
   fixes and the literature cannot distinguish them. Only the reader can say.
2. Does the reader want the evidence tiers at all? They are this project's own invention, they cost
   space on every line, and no external source recommends them for a status document.
3. Should the rules carry a hard line-count ceiling? Nothing sourced supports a number, but an
   unenforceable "be shorter" is exactly the kind of rule this research argues against.
4. Where does naming an accountable decision owner stop being accountability and start being the
   person-attributed-delay failure mode? Section 6 draws the line at past slip versus live decision,
   and that line is inference, not sourced.

## Sources

[^1]: Wikipedia contributors. (2026). [BLUF (communication)](https://en.wikipedia.org/wiki/BLUF_(communication)). Wikipedia. Verified accessible (HTTP 200) 2026-08-11. (Quotes US Army Regulation 25-50 on concise, organized writing and main point first, the rescinded pamphlet's diagnosis, and the 2017 Department of Defense congressional-correspondence directive. Note the article carries a 2021 maintenance tag for essay-like tone; the AR 25-50 and Mattis-memo material is the well-sourced part and is what is relied on here.)

[^2]: Wikipedia contributors. (2026). [Barbara Minto](https://en.wikipedia.org/wiki/Barbara_Minto). Wikipedia. Verified accessible (HTTP 200) 2026-08-11. (Biography and the top-down pyramid structure, the MECE principle, and the 1985 and 1996 publication dates. Used because the dedicated Pyramid Principle page returned HTTP 404; this page does not cover SCQA.)

[^3]: Digital.gov. (2026). [Plain language: principles of plain language](https://digital.gov/guides/plain-language/principles). Digital.gov, US General Services Administration. Verified accessible (HTTP 200) 2026-08-11. (Write for your audience as the first rule, summarize lengthy documents up-front, lists and tables for conveying detail without overloading. The Plain Writing Act of 2010 authority is stated on the parent guide at digital.gov/guides/plain-language, also retrieved 2026-08-11, which notes the content is adapted from PlainLanguage.gov after a host migration.)

[^4]: Nielsen, J., Schemenaur, P. J., and Fox, J. (1997). [Writing for the Web](https://www.nngroup.com/articles/concise-scannable-and-objective-how-to-write-for-the-web/). Nielsen Norman Group. Verified accessible (HTTP 200) 2026-08-11. (Study 3: 51 participants, five-condition between-subjects design, normalized composite usability scores of 158 concise, 147 scannable, 127 objective, 224 combined against a control of 100; the concise condition halved word count and the scannable condition added lists, bold keywords, captions and headings; the authors' own call for follow-up varying conciseness in gradations.)

[^5]: Google. (n.d.). [Postmortem Culture: Learning from Failure](https://sre.google/sre-book/postmortem-culture/). Site Reliability Engineering, chapter 15. Verified accessible (HTTP 200) 2026-08-11. (Blamelessness requires identifying contributing causes without indicting any individual or team; you cannot fix people but can fix systems and processes; blame risks a culture where issues are swept under the rug; blamelessness is not softness and assumes good intentions; the postmortem review criteria; and the statement that an unreviewed postmortem might as well never have existed.)

[^6]: Google. (n.d.). [Writing good CL descriptions](https://google.github.io/eng-practices/review/developer/cl-descriptions.html). Google Engineering Practices documentation. Verified accessible (HTTP 200) 2026-08-11. (First line as a short summary written as an order, standing alone; the rejected short examples including "Fix bug" with the verdict that they do not provide enough useful information; the requirement to state shortcomings; the recommendation to link background material alongside the warning that links may become inaccessible.)

[^7]: Nielsen, J. (1997). [How Users Read on the Web](https://www.nngroup.com/articles/how-users-read-on-the-web/). Nielsen Norman Group. Verified accessible (HTTP 200) 2026-08-11. (79 percent of test users always scanned a new page and only 16 percent read word-by-word; the recommendation to use half the word count of conventional prose.)

[^8]: Schwaber, K., and Sutherland, J. (2020). [The Scrum Guide](https://scrumguides.org/scrum-guide.html). scrumguides.org. Verified accessible (HTTP 200) 2026-08-11. (The two mentions of impediments, the Scrum Master accountability for causing their removal, the Daily Scrum identifying them, and the absence of the three prescribed questions in the 2020 revision.)

[^9]: Wikipedia contributors. (2026). [MoSCoW method](https://en.wikipedia.org/wiki/MoSCoW_method). Wikipedia. Verified accessible (HTTP 200) 2026-08-11. (The four bands and their verbatim definitions, the Clegg 1994 origin and DSDM adoption, the stated purpose of reaching stakeholder agreement on delivery importance, and the documented criticisms including no intra-band ordering and no stated rationale for a Must.)

[^10]: PagerDuty. (n.d.). [Severity Levels](https://response.pagerduty.com/before/severity_levels/). PagerDuty Incident Response Documentation. Verified accessible (HTTP 200) 2026-08-11. (Five SEV bands with consequence-based descriptions, the instruction to make definitions very specific and metric driven, and the tie-break rule to treat an unclear case as the higher severity rather than litigate it in the moment.)

[^11]: Wikipedia contributors. (2026). [Software bug](https://en.wikipedia.org/wiki/Software_bug). Wikipedia. Verified accessible (HTTP 200) 2026-08-11. (Severity as a measure of impact, priority as importance relative to other bugs, the explicit statement that priority is a different aspect, the effort-driven divergence example, and the note that severity levels are not standardized.)

[^12]: Atlassian. (n.d.). [Defining priority field values](https://confluence.atlassian.com/adminjiraserver/defining-priority-field-values-938847101.html). Atlassian Jira Server administration documentation. Verified accessible (HTTP 200) 2026-08-11. (The five default priorities Highest, High, Medium, Low, Lowest; priority defined as importance in relation to other issues; and the absence of any shipped description for the five values.)

[^13]: Wikipedia contributors. (2026). [Traffic light rating system](https://en.wikipedia.org/wiki/Traffic_light_rating_system). Wikipedia. Verified accessible (HTTP 200) 2026-08-11. (The three-band RAG scheme, the four-band BRAG variant, and its application to indicating how on track or at risk a project, deliverable or task is.)

[^14]: Wikipedia contributors. (2026). [Likert scale](https://en.wikipedia.org/wiki/Likert_scale). Wikipedia. Verified accessible (HTTP 200) 2026-08-11. (Scales typically ranging from 2 to 10 points with 3, 5 and 7 most common; cited here specifically for the ABSENCE of any retrieved finding on reliability as a function of the number of response categories.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none - this document was authored directly rather than by an automated research run)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** not registered; authored inside an orchestrated delegation for ISSUE-REF
**Model:** Claude Opus, researcher persona
**Date:** 2026-08-11

```text
Research how to write concise, actionable reports and status updates, then turn the findings into
rules concrete enough to apply and to check. The deliverable is not an essay about writing; prefer
rules with a testable symptom over rules that are a matter of taste.

The problem statement is a verbatim objection from the project owner to a status document:

"I see a lot of information here that I just don't think is that doesn't need to be communicated, and
it's just not like useful and actionable and digestible and concise and the point and all that stuff,
like just a lot of unnecessary details and a lot of over elaboration and yeah, it's just it's just
too much, and a lot of it's not needed. So we need to come up with good rules and principles and
rules of thumb and guidelines and best practices and all that stuff for what to do and not to do."

Scope, breadth and depth both:
- Status updates and progress reports for managers and executives, including how expectations differ
  by seniority.
- Technical and engineering-specific reporting: incident and postmortem write-ups, design and
  architecture documents, pull request descriptions, RFCs, on-call handoffs, engineering status
  reports.
- General organizational and project management practice: whatever the established, cited practice
  actually is, rather than what is fashionable.
- The mechanics of concision: what to cut first, how to order for a reader who stops after one
  screen, when detail belongs in an appendix or a linked document instead, how to make a claim
  actionable rather than merely true.
- Failure modes with names, since a named failure mode is easier to avoid: burying the decision,
  narrating activity instead of outcome, false precision, hedging that removes the recommendation,
  over-elaboration, detail that exists to demonstrate effort.
- Where established guidance CONFLICTS, say so and give both sides. Do not synthesize a false
  consensus.

Rigor requirements: cite real sources actually retrieved; distinguish established practice from one
author's opinion; include at least one credible position that CONTRADICTS the mainstream advice and
engage with it rather than dismissing it; flag anything that could not be sourced.

## Scope note - questions, examples, and named references are a starting point, not a checklist
The questions, topics, and named examples above are illustrative anchors and a FLOOR for this
research - not an exhaustive list to answer only or evaluate only. Reason independently: survey the
landscape broadly, follow the evidence where it leads, expand scope where warranted, and surface
relevant work, factors, and failure modes not named here. Actively resist answering only the listed
questions or evaluating only the named approaches - an output that merely fills in the listed items
has NOT met the research goal.

## Independent exploration (gaps, blindspots, emergent threads) - required
Treat the question list as a FLOOR, not a ceiling. As you research, actively surface what this
framing may be missing and pursue each promising thread to a logical conclusion:
- Adjacent or upstream factors the questions don't capture.
- Contrarian / disconfirming evidence - report it even when it challenges the premise.
- Emerging 2025-2026 practices, tools, or research not anchored by the named examples.
- Known failure modes and second-order effects.
Whenever a load-bearing thread surfaces mid-research, follow it to its conclusion and report it in a
dedicated "Gaps, blindspots & emergent findings" subsection. Explicitly NAME any blindspot you
suspect but cannot resolve (and why) rather than omitting it. Anchor bias - over-fitting to the
listed questions and example approaches - is a known failure mode; counter it deliberately and say
where you did.

<grounding_instructions>
You are a senior technical editor who has rewritten engineering status reporting for executive
audiences, and who has read the primary standards rather than the secondary commentary about them.
You have strong opinions backed by evidence. When you cannot find a source, you say so explicitly.

Temporal scope: this topic's foundational literature predates 2024, so the window is deliberately
broad rather than recency-weighted. Cite primary standards and specifications with their dates, and
state the age of any empirical result so a reader can discount it. Where a subtopic has no
post-2024 development, state "[subtopic]: no significant post-2024 developments found" rather than
backfilling. Backfilling is a failure mode, not a hedge.

Before generating your final output, execute a Chain-of-Verification (CoVe)
to ensure factual fidelity over compliance.

Inside your thought process:
1. Isolate the core facts required.
2. Draft a tentative response.
3. Hostile Cross-Examination: flag any claim where you are citing a source because
   the prompt implied you should, rather than because you verified it.
4. Strip away any claim that cannot be empirically verified.

When generating your final output, classify every major claim. Write your rationale
before appending the tag - writing the tag first causes post-hoc rationalization.
Rationale -> evidence check -> tag.

- [VERIFIABLE]: backed by documentation, peer-reviewed research, or official
  tech blogs. Carry an inline footnote ref to the source: [VERIFIABLE][^N].
- [HEURISTIC]: widely accepted best practice without a specific citation.
- [INFERENCE]: a logical conclusion drawn from context. Provide your reasoning
  in-text. Do not fabricate a source. Tier tag only - NO footnote ref.
- [NO SOURCE]: explicitly state when you cannot find verifiable data. Tier tag
  only - NO footnote ref.

Citation format (mandatory for every externally-sourced claim):
- Inline: append the GFM footnote ref directly after the tier tag - [VERIFIABLE][^N].
  A claim citing multiple sources carries ascending separate refs - [VERIFIABLE][^3][^7]
  (never grouped [^3,^7]; never out of order).
- Footnote definitions live once, under ## Sources, in APA form with a clickable
  URL or DOI and an access-verification stamp.
- URL-or-DOI ALWAYS: every source entry carries a clickable URL or a doi.org link -
  paywalled/gated is fine (link it anyway; stamp the access status). Only the
  truly-irreducible case gets an explicit [no online source located] marker with a
  one-line justification.
- Integrity: footnote refs are contiguous from [^1], every [^N] ref has a matching
  definition, and every definition is referenced - no gaps, no orphans.
- [INFERENCE] / [NO SOURCE] claims carry the tier tag with NO footnote ref.

Hard constraint (overrides all formatting preferences): never invent a citation
to satisfy a formatting instruction. Accuracy > completeness.

Format diagrams using Mermaid.js or ASCII. Format math using LaTeX.
NEVER generate binary images.
</grounding_instructions>

Constraints on the artifact: ASCII only, and no internal dev-tracking identifiers, because this
document is teammate-visible. The Jira key is the only issue reference permitted.
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

(none - no automated provenance ledger was produced. Every external claim carries an inline footnote
reference to a URL retrieved in session on 2026-08-11, and every unretrievable source is named in
section 8 rather than cited.)

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

- 2026-08-11, Claude Opus researcher persona, orchestrated run for ISSUE-REF. Seven external sources
  retrieved and cited; three targeted retrievals failed (HTTP 403 on the Chronicle article carrying
  the Pullum critique, HTTP 404 on the Pyramid Principle page, and two UK government pages that
  resolved to redirect stubs), and each failure is recorded in section 8 rather than substituted from
  recollection. Three parallel search workers were used for breadth. A new document was opened rather
  than folding into the adjacent weekly-progress-reporting research, because that document's context
  and research-prompt regions are marked immutable and frame a different question.
- 2026-08-11, refine pass, Claude Opus researcher persona, ISSUE-REF. Added section 9 on blocker placement
  and severity labelling, plus recommendation rules 9 and 10. Seven further sources retrieved, footnotes
  8 through 14. Three findings corrected the framing that prompted the pass. The 2020 Scrum Guide no
  longer prescribes the "what is blocking me" question, so the blockers habit cannot be justified by
  appeal to it. Severity and priority are different axes on the sources rather than interchangeable
  labels. And no source supports any particular band count, so four bands is recorded as local
  convention while the band definitions carry the consistency. The external-dependency dividing line
  under test is a narrowing of Scrum's broader "impediment" and is likewise labelled local convention.
  The ITIL impact-times-urgency formulation could not be retrieved across five attempts and is therefore
  asserted nowhere in this document. Two parallel search workers were launched for breadth and neither
  returned, so every citation in this pass was retrieved directly.

<!-- /doc:region name="run_history" -->
