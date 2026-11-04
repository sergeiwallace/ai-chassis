---
title: "[FILL: Meeting Name] - YYYY-MM-DD"
category: meeting-notes
tags: [meeting, FILL-meeting-type, FILL-topic-tags]
status: template
date: YYYY-MM-DD
duration: "MM:SS"
meeting_type: "[FILL: standup | review | workshop | ...]"
attendees: ["Surname, Given", "..."]
sources:
  - "data/<file>.vtt (vendor transcript, N cues)"
  - "data/<file>.pdf (notes export)"
  - "data/<file>.mp4 (MM:SS, WIDTHxHEIGHT) - git-ignored, local only"
  - "derived/asr-*.jsonl (independent local ASR pass, N segments)"
  - "frames/ (N extracted frames) - git-ignored, local only"
  - "data/<file> chat.md (the meeting chat, N messages, N attached files)"
template_version: "meeting-analysis-1.2.0"
---

<!-- WHICH KIND OF DOCUMENT IS THIS? This template is for a RECORDING ANALYSIS: written AFTER the
     meeting, from its transcript, notes export and recording. Its slug is
     `team-standup-analysis-<date>`.

     If you are writing the PRE-MEETING progress report instead - derived from our own repository
     rather than from a recording, with an evidence tier per claim - you want a different template
     and a different slug: `standups/TEMPLATE.md` and `team-standup-update-<date>`. The two are
     not interchangeable. The discriminator is whether a recording exists to analyze; do not fill
     this template's transcript and frame sections with "not applicable" to force a fit.

     FILE NAMING, before you save this anywhere. The path is
     `docs/meetings/<meeting-type>/<author>/<YYYY-MM-DD>/<slug>-<YYYY-MM-DD>.md`, for example
     `docs/meetings/standups/ada-rivera/2026-08-05/team-standup-analysis-2026-08-05.md`.

     The date appears twice on purpose. The directory carries it so a recurring series sorts
     chronologically and so the doc can sit beside its own `data/`, `derived/` and `frames/`
     sources. The filename carries it so the doc stays identifiable once it is opened, linked,
     exported, or attached, at which point its directory is no longer visible.

     Never rename a committed doc to tidy it up: these files are linked from the design doc,
     the audit docs and pull request descriptions, and a rename breaks every inbound link.
     Full convention and directory layout: README.md in this directory. -->

# [FILL: Meeting Name] - YYYY-MM-DD

**When:** YYYY-MM-DD, scheduled HH:MM-HH:MM; the recording runs MM:SS.
**Present:** [FILL: Name (role), ...]
**Shape:** [FILL: one line on the meeting's form - who led, whether it was a screen-share
walkthrough or a discussion, and roughly how the speaking time split. This tells a reader how
much weight to put on the transcript versus the recording.]

> **How this doc was produced, and how much to trust it.** [FILL: which sources were analyzed,
> that they were cross-checked rather than any one trusted, and what was done about disagreement.
> State that quotes are verbatim with timestamps, and where corrected terms are logged. Link the
> provenance section.]

---

## Executive summary

[FILL: 3-5 paragraphs. What the meeting settled, then what it did not. Lead with the decisions
that change what someone does on Monday.

Two things worth stating explicitly if true: the most consequential content may be a set of
self-flagged gaps rather than any decision, and the recording may carry material the transcript
does not. Say which, and point at the section that covers it.]

---

## Decisions

<!-- Row ids D-N are LOCAL to this document: self-defined labels for the rows below, not
     references to any external tracking system. Keep them that way -- a reader must not need
     another system to resolve them. -->

| # | Time | Decision | Owner | Firmness |
|---|------|----------|-------|----------|
| D-1 | MM:SS | **[FILL: the decision in bold.]** [FILL: detail, with a verbatim quote where the exact wording matters.] | [FILL: who proposed, who ratified] | [FILL: Firm / Firm but interim / Firm deferral / Stated but unratified] |

**Firmness is a real field, not decoration.** Distinguish a decision someone ratified from one
stated into silence. "Stated firmly but unratified - no one accepted or contested it before the
meeting ended" is frequently the accurate description and is much more useful than recording it
as agreed.

**Explicitly not decided:** [FILL: list what a reader might assume was settled and was not.
Include topics never raised at all if their absence is itself notable - dates, scope, tracking.]

---

## Action items

<!-- One table per person. A per-person table is what makes this doc usable in a follow-up:
     nobody wants to grep a merged list for their own name. -->

### [FILL: Person Name]

| Time | Commitment | Timing |
|------|-----------|--------|
| MM:SS | [FILL: what they committed to. Mark whether it was self-proposed or assigned.] | [FILL: their words, not an inferred date - "today", "this week", "none"] |

**[FILL: Note explicitly if no action item carries a real date, and whether anything was assigned
to someone who never verbally accepted it. Both are common and both matter for follow-up.]**

### Consolidated action items

<!-- Every action item from EVERY source, after consolidation: the transcript and recording
     (spoken commitments and asks), the Loop notes' Follow-up tasks, the AI Summary and the
     meeting chat. An item may appear in only one source. Owner, source and evidence per row;
     NO task-tracker ids here (this doc is teammate-visible and the gates refuse internal ids):
     the tracker rows live in the tracker and in the review hand-off. Row ids A-N are local. -->

| # | Action item | Owner | Source(s) | Evidence | Consolidation |
|---|-------------|-------|-----------|----------|---------------|
| A-1 | [FILL: what is to be done, as one piece of work] | [FILL: who accepted it, or "unassigned"] | [FILL: transcript / notes follow-up row N / AI Summary / chat] | [FILL: MM:SS, row, or chat time] | [FILL: "merged with the notes' row 3 and the AI Summary's second task: one deliverable, one owner" / "kept apart from A-2: different owner" / "-"] |

**[FILL: one line on how many items each source contributed and how many were merged; name any
merge or split that needed a decision and where it is shown.]**

---

## Proposed issue-tracker issues

<!-- Proposed, NOT created, unless the user asked in this run. One row per issue; several small
     items of one owner may share an issue when they ship together. Name an existing tracker
     issue instead of re-proposing it. Public tracker keys are the only tracker ids allowed here. -->

| # | Summary | Type | Suggested parent epic | Suggested assignee | Covers | Rationale |
|---|---------|------|-----------------------|--------------------|--------|-----------|
| J-1 | [FILL: issue summary as it would read in the tracker] | [FILL: Epic / Story / Task / Bug] | [FILL: the parent key or "new epic: ..."] | [FILL: name] | [FILL: A-1, A-3] | [FILL: why this is one issue, and why it is worth tracking at all] |

---

## Open questions

| Time | Raised by | Question |
|------|-----------|----------|
| MM:SS | [FILL: who] | **[FILL: the question.]** [FILL: what answer was given, if any, verbatim. Note if it was self-flagged more than once - repetition marks it as known-unresolved rather than overlooked.] |

---

## Architecture as described

[FILL: only if the meeting described a system. Assemble it from the walkthrough plus any
read-backs, and cite the timestamps that confirm each piece. Read-backs are the best evidence
that two people actually agreed on the same model.

Flag where the described architecture contradicts a committed document or the live system -- that
contradiction is usually the most actionable thing in the whole analysis.]

---

## What the recording shows that the transcript does not

[FILL: DELETE this whole section if there was no screen share. Where there was one, this is
usually the highest-value section, and it is the part a transcript-only process cannot produce.

Speech during a screen share is heavily deictic -- "this", "here", "this one" -- so the transcript
alone is materially incomplete. Use `###` subsections per finding. For each: what the frames show,
the frame timestamps, and whether it appears in the audio at all.]

### [FILL: finding title]

[FILL: the finding, citing frame timestamps. Where a frame shows an exact figure, quote it
literally rather than paraphrasing.]

### Moments the frames do NOT corroborate

[FILL: transcript claims with no frame sampled at that timestamp. **Do not skip this.** The
temptation is to let everything read as visually verified; separating what was seen from what was
only heard is what makes the rest of the section trustworthy.]

---

## Terminology and transcription corrections

[FILL: one line on overall vendor-transcript accuracy, and what the independent ASR pass was worth.
State the single most important correction and why it matters for a reader of the raw transcript.]

| As transcribed | Actual | Confidence | Basis |
|----------------|--------|-----------|-------|
| [FILL: every variant seen] | **[FILL: the real term]** | [FILL: High / Med / Low] | [FILL: what establishes it - a second ASR pass, on-screen text at a frame, context] |

**Unresolved:** [FILL: terms that could not be recovered from the corpus. Mark them Low
confidence and say they appear in no frame, rather than guessing. A named unknown is more useful
than a plausible wrong answer.]

---

## Follow-ups worth raising

[FILL: numbered list. Each: what it is, why it matters, and who would own it. Distinguish items
the meeting agreed to from items this analysis is surfacing.]

### Defects surfaced by the recording, none of them tracked

[FILL: DELETE if not applicable. Items that came out of a live session and were never restated
aloud, so no action item covers them. Cite file paths and line numbers where the defect is in the
repository, and verify each against the tracked tree before asserting it -- an on-screen claim
about the code can itself be wrong.]

---

## Notes from the vendor recap that this doc does not corroborate

[FILL: DELETE if there was no notes export. An AI-generated recap carries its own disclaimer.
Flag: content belonging to a different meeting in the same file, and decisions asserted more
confidently than the transcript supports.]

---

## Meeting chat

<!-- Every relevant message of the meeting's Teams chat, from `data/<file> chat.md`, in the order
     sent, each with its sender and local time. Keep the message's own formatting: a Teams reply
     quote is a quote OF THE ORIGINAL MESSAGE with its sender (never a bare Markdown quote), a
     link keeps its text and its exact, working URL, emphasis keeps its marks. Each attached file
     is named with its path under data/. Say which messages were left out and why (joins,
     reactions, unrelated chatter). -->

[FILL: one line: N messages on the day, N kept here, N attached files copied to `data/`.]

**HH:MM Sender** -- [FILL: the message, formatting kept. A reply quote reads:]

> Teams reply quote of [FILL: Sender], HH:MM: [FILL: the original message's text]

[FILL: the reply itself.]

- Attachment: [FILL: `data/<file name>`], posted by [FILL: Sender] at HH:MM.

---

## Provenance and method

| Source | What it is | How it was used |
|--------|-----------|-----------------|
| [FILL: path] | [FILL: what it is, with measured size or count] | [FILL: primary / cross-check / treated as untrusted] |

**Method notes, including what did not work:**

[FILL: what earned its cost and what did not, with measurements. Record failed approaches -- they
are the most reusable part of this document, because the next person will otherwise try them again.

Include any probe that misled you and how it was caught. State the cost of expensive steps in
wall-clock terms so the next run can budget.]

[FILL: state where the raw media lives and that it is git-ignored, and confirm no credentials
appeared in any frame.]
