---
title: "Writing standard for teammate-visible documents"
category: procedures
tags: [writing, standard, reporting, status, blockers, documentation]
status: active
source: claude-opus-5
---

# Writing standard for teammate-visible documents

Applies to anything a teammate or stakeholder reads: stand-up progress reports, status reviews,
analyses, audit summaries, pull request descriptions and announcements. It does not apply to internal
working notes or commit bodies.

**This is not a public-relations or external-communications standard.** It governs the *structure and
substance* of a status document written for colleagues. The separate outward-facing prose rule in
the shared agent guidance (no AI tells, no overused words, no rule-of-three, no private dev-tracking
IDs, with an avoid-list for common writing failures) governs *how the sentences
read* in anything others see. The two are complementary and neither replaces the other: this standard
will not stop you writing slop, and that one will not stop you burying the ask on page three.

Each rule below states what to do and, where one exists, a **symptom** that can be checked without
judgement. That is deliberate. A rule someone can check survives an author who disagrees with it; a
rule that says "be concise" does not. Where a rule has no external basis it is marked **local
convention**, so nobody mistakes house style for established practice.

The sourced basis, the conflicts in the literature, and what could not be sourced:
The actionable-status-report writing-standards research doc.

## Table of Contents

- [The order is not a preference](#the-order-is-not-a-preference)
- [Blockers, and how to label their severity](#blockers-and-how-to-label-their-severity)
  - [What counts as a blocker](#what-counts-as-a-blocker)
  - [The severity bands](#the-severity-bands)
  - [What every blocker item must carry](#what-every-blocker-item-must-carry)
- [Failure modes and their symptoms](#failure-modes-and-their-symptoms)
- [Length: cut sections, not sentences](#length-cut-sections-not-sentences)
- [Make the claim actionable](#make-the-claim-actionable)
- [Attribute a slip to the prerequisite, not the person](#attribute-a-slip-to-the-prerequisite-not-the-person)
- [Cut elaboration, never evidence](#cut-elaboration-never-evidence)
- [Name things, and link them so both renderers resolve](#name-things-and-link-them-so-both-renderers-resolve)
- [Pre-ship checklist](#pre-ship-checklist)
- [What is local convention and what is not](#what-is-local-convention-and-what-is-not)

## The order is not a preference

Answer first, then the evidence for it. Four independent traditions require this: US Army
correspondence regulation, Minto's pyramid, federal plain language guidance, and Google's rule that
the first line of a change description must stand alone. Section order is therefore a standard, not a
matter of taste.

The order for a status document:

1. **Blockers**, if there are any, ordered by severity.
2. **Summary**, at most six items, most consequential first.
3. **Decisions needed from the reader**, each with a recommendation and a named cost.
4. **Risks**, ordered by severity.
5. **Since the last stand-up**, outcome first, and **window-scoped only**.
6. **Still open**, with who unblocks each item.
7. **Detail and evidence**, everything a reader may want to verify but does not need to read.
8. **Revision log.**

Section 5 is news and section 6 is standing state, and each fact belongs in exactly one of them.
Conflating the two is how these documents double in length: everything ever built accumulates in the
window section because nobody deletes last week's entry. **Test: would the item still appear next
week?** If it would, it is standing state and belongs in section 6.

Every multi-item section is a **numbered** list with sub-bullets, never a paragraph and never a flat
bullet list, and states its ordering in one line under the heading. Numbering gives the meeting a way
to say "point 3" and forces a visible ordering decision instead of letting items accumulate in arrival
order. One exception: when sequence **is** the content (an incident timeline, reproduction steps, a
migration order) order chronologically and say so.

**Symptom: count the lines above the decisions table.** If an evidence or background section sits
between the summary and the decisions, the document is ordered backwards. This is the most common
structural failure, and it happens by accretion rather than by choice: a section gets added where
the writer was working rather than where the reader needs it. Background belongs in section 7.

**Symptom: a reader who stops after the first screen cannot act.** If the ask, the risk and the
recommendation are not all above the fold, reorder.

## Blockers, and how to label their severity

Blockers go **first**, above the summary. Be honest about the basis: no source places a blockers section
at the top of a written status report, so this is derived from the answer-first rule rather than
inherited from practice. The derivation is that a blocker is the strongest form of reader-required
action, because it is the one item where nothing moves until the reader or someone they can reach acts,
which makes it the highest-value content on the page.

Note also what does **not** support it, so nobody defends the section with a bad argument: the 2020
Scrum Guide does not prescribe the "what is blocking me" question at all. It mentions impediments twice
and leaves the Daily Scrum's structure to the team. Citing the three questions is citing a document that
no longer says it.

### What counts as a blocker

**A blocker is work that cannot proceed until someone outside our control acts.** Everything else that
is unresolved goes in Still open, and everything else that threatens us goes in Risks.

This is deliberately narrower than Scrum's "impediment", which covers anything impeding the team,
including obstacles the team can clear itself. The narrowing is house style, not sourced practice, and
the reason is what the section is for: a blockers list addressed to a reader collects the things that
reader can act on. Add our own unfinished work and the section stops being a list of asks.

**The consequence is counterintuitive and must not be quietly ignored: an item can be severe, real, and
entirely ours, and still not be a blocker.** When that happens, say so where the reader would expect to
find it rather than forcing it into the list. A blockers section that absorbs our own backlog is no
longer usable as an escalation list.

Test each candidate with one question: **if we did everything right from now on, would this clear?** If
yes, it is not a blocker.

### The severity bands

Label **severity**, meaning the consequence if nothing is done. Do **not** label priority. They are
different axes: severity measures impact, while priority folds in cost to fix and our own sequencing,
neither of which the reader is deciding. Using the two words interchangeably is the common error.

Four bands. The count is house style, since no source supports a particular number; what the evidence
does support is that the definitions carry the consistency, so these are written to be assignable rather
than evocative.

| Band | Assign it when | Not this band if |
|---|---|---|
| **Critical** | Something is losing data, is down, or is exposing data to someone who should not see it, **right now**. | The harm is potential rather than occurring. |
| **High** | No loss yet, but either the exposure or the risk **grows on its own** while nobody acts, or the blocker makes an action we will certainly take unsafe. | Nothing worsens by waiting. |
| **Medium** | Real work is stopped or unverifiable, and the consequence is bounded and static. | The stopped work has a route around it. |
| **Low** | Inconvenient, with a working route around it today. | There is no workaround. |

Two rules that make the bands usable:

- **State the consequence in the item itself.** A band with no stated consequence is a feeling. If the
  item does not say what happens if nobody acts, the label cannot be checked and is not earned.
- **When an item sits between two bands, take the higher one and move on.** The stand-up is not the
  place to litigate a label. Revisit it next week if it mattered.

An **empty band is informative**. A blockers list with nothing in Critical is making the claim that
nothing is currently causing loss, outage or exposure, and that claim is worth a reader's attention. Do
not invent a Critical to fill the band, and do not hide an empty one.

### What every blocker item must carry

Five things, all of them, or it is not actionable:

1. **What is blocked** - the work that cannot proceed, led with the function, not the mechanism.
2. **The severity band**, with the consequence that justifies it.
3. **What would unblock it** - the specific act, not "a decision".
4. **Who owns the unblock** - a named person, team or role. "TBD" here means the item is not ready.
5. **The evidence tier.**

Symptom: a blocker with no named owner, or whose owner is us.

## Failure modes and their symptoms

| # | Failure mode | Symptom |
|---|---|---|
| 1 | Burying the decision | The recommendation or ask appears after the evidence supporting it |
| 2 | Narrating activity instead of outcome | The first sentence names a file, branch, API, count or byte size before a verb describing a capability |
| 3 | Short but uninformative | The line is brief and still does not say what changed. "Fix bug" is the canonical case |
| 4 | Leading with method | The first clause says how something was verified rather than what is now true. Grep for a sentence opening "Verified", "Confirmed", "Checked" |
| 5 | Omitted drawback | An approach is recommended and no cost of it is named |
| 6 | Hedging away the recommendation | A decisions row with an empty recommendation, or one qualified until it commits to nothing |
| 7 | Person-attributed delay | A slip explained by who had not decided rather than by the prerequisite not met |
| 8 | Detail that demonstrates effort | Removing the passage changes no reader action. Its only function is to show work happened |
| 9 | Describing instead of naming | A system referred to by description with no official name, identifier or link |
| 10 | False precision | A number carried to a precision the measurement does not support, or an estimate presented without its basis |
| 11 | Two documents in one | The document serves both a decision-maker and a future auditor in the same prose, so it satisfies neither |

Failure mode 3 is the one that corrects the naive reading of this standard. Every change description
Google rejects as inadequate is short. Brevity is not the property being asked for; the reader
learning what changed and why it matters is, and that sometimes costs words.

## Length: cut sections, not sentences

Removing whole sections is what shortens a document. Prose polishing is not, and the measured evidence
supports that distinction rather than merely asserting it: in the study behind the usual "be concise"
advice, the gain came from halving the word count and from imposing scannable structure, and nothing
in it measured a benefit from tightening prose at constant length and constant structure.

What to cut, in order:

1. Anything a reader would not act differently for having read. Apply per line.
2. Verification method, where the result is what matters. Move it to a sub-bullet or to the detail
   section.
3. Narration of process: what was tried, in what order, and how long it took.
4. Restatement. A fact stated in the summary does not need restating in the body in the same words.
5. Counts of internal artifacts, unless the count is itself the finding.

**Symptom: a section longer than a screen that contains no decision, no risk and no number a decision
turns on.** Cut it or move it to the detail section.

No length target is set here, because no source supports one for this genre. "Halve it" is a useful
instinct borrowed from web-page research, not a standard for a status report. Any ceiling a given repo
adopts is local convention and should say so.

## Make the claim actionable

- **Lead with what it functionally does or fixes.** The first sentence states the change and why it
  deserves attention. Mechanism, API names, file paths, branch names, commit counts, byte sizes and
  verification method follow, and only if they earn a line.
- **Every requested decision carries a recommendation and a named drawback.** An unrecommended
  decision costs the reader more time than it saved the writer. A recommendation with no stated cost is
  not a recommendation, it is advocacy.
- **State partial findings as both halves**: what is established, and what is not. Half an answer
  reported as none wastes work already done; half reported as whole misleads. Where a signal points at
  a person or team without proving it, say exactly what it does and does not establish.
- **Give a number its basis.** A figure with no stated measurement is asserted, not measured, and
  should be tagged accordingly.

## Attribute a slip to the prerequisite, not the person

Where a document reports a delay, describe the sequencing and the prerequisite that was not met.
Do not explain a slip by naming who had not decided, and do not tally who decided what.

Two reasons, and the first is evidentiary rather than social. The dependency is a checkable fact about
sequencing; an account that turns on a person's attention usually is not, so the dependency version is
better evidence as well as fairer. The second is the blameless-postmortem finding: blame changes what
gets written down, and a culture that indicts individuals is one where problems get concealed. You
cannot fix people, but you can fix systems and sequencing.

This is not permission to omit the failure. Blameless does not mean soft: name the slip, name what it
cost, and name the mechanism.

**The boundary, which is judgement and not a symptom.** Naming who owns a *live, open* decision is
required, because otherwise nobody owns it. Explaining a *past* slip by who had not decided is the
failure mode. Owner of an open decision: yes. Cause of a closed delay: the prerequisite.

## Cut elaboration, never evidence

Concision and auditability genuinely conflict, and the conflict is not resolvable by writing more
carefully. Google's own guidance both recommends linking background material and warns that links
become inaccessible. So:

- **A fact a decision turns on stays in the document.** It is not detail.
- **The method by which that fact was established can move** to a sub-bullet, to the detail section, or
  to a linked document.
- **If you move something, link it.** Evidence relocated without a link has been deleted.
- **Prefer a table to paragraphs** when preserving a set of measurements. A table keeps every value and
  removes the prose that narrated them. This is the cheapest way to shorten a document without losing
  anything.
- **Keep the evidence tier** on any claim that carries one, wherever the claim ends up.

**Symptom: a measurement that appears in a commit message or a linked document but not in the
document that asks the reader to decide on it.**

## Name things, and link them so both renderers resolve

- Use the **official name** as the owning system spells it, plus the **identifier** where one exists
  (stack name, api id, resource name, version), plus a **link** to where it can be read.
- **Never wrap link text in backticks.** A code span overrides link styling, so it renders as
  monospace and reads as unclickable.
- **Always give display text.** Link the name, not the address.
- **Cross-repo links carry both destinations**, because a relative path that works locally is dead on
  GitHub: `[name (local)](../../other-repo/path.md) ([GitHub](https://github.com/owner/other-repo/blob/main/path.md))`.
- **Verify a sample link from the linking document's own directory.** Checking from the repository root
  gives a false negative, and has done so in practice.

## Pre-ship checklist

Mechanical, in order. Each item is checkable rather than a matter of opinion.

1. The ask, the risk and the recommendation are all above the first screen.
2. Every blockers item is genuinely blocked on someone outside our control, carries a severity band with
   its consequence, names what would unblock it, and names an owner who is not us.
3. No background or evidence section sits between the summary and the decisions.
4. Every decisions row has a recommendation and a named drawback.
5. No first sentence opens with a file, branch, API, count, or the words Verified, Confirmed or
   Checked.
6. Every delay is attributed to a prerequisite, not a person.
7. Every load-bearing measurement is in the document, and everything moved out is linked.
8. Every significant thing has its official name, its identifier and a link.
9. ASCII only, and no private dev-tracking identifiers. Reference work by the tracker key the reader's
   own organisation uses.
10. A table of contents lists every `##` and every meaningful `###`, and its anchors resolve
    (the doc-TOC consistency-check script).
11. A sample relative link resolves from this document's own directory.
12. The revision log has a new row.

## What is local convention and what is not

Honesty about provenance, so nobody defends house style as though it were research.

**Externally supported:** answer-first ordering; leading with what a change does rather than how it was
done; cutting volume and imposing structure rather than polishing prose; naming drawbacks alongside a
recommendation; attributing a failure to the mechanism rather than the individual; the observation that
short and uninformative is a real and common failure; that severity and priority are distinct axes; that
band definitions must be specific and consequence-based; and the take-the-higher-band tie-break.

**Local convention, no external citation:** the six-item summary cap; the specific section order above;
the evidence tiers (executed / read / asserted / blocked); the official-name-plus-identifier-plus-link
rule; the both-halves rule for partial findings; every symptom string in the tables, which were derived
from real documents that had to be rewritten after failing in front of a reader; **the blockers-first
placement**, which is derived from the answer-first rule rather than found in any source; **the
external-dependency definition of a blocker**, which narrows Scrum's broader "impediment"; and **the
four-band count**, since no source supports any particular number.

**Explicitly not supported by anything we could retrieve:** the ITIL formulation that priority equals
impact times urgency. Five retrieval attempts failed, so the standard does not rest on it anywhere. If
someone cites it, they should produce the source.

**Unresolved in the literature, so decided here by fiat:** whether scannable bullets help or harm. The
measured usability research recommends them; the best-known critique of presentation culture argues
that bullets degrade the reasoning they summarize. This standard chooses bullets with substantive
sub-bullets, on the grounds that our readers demonstrably skim, and records that the choice is not
settled.
