---
title: PR Template Best Practices, Industry Standards & Gold-Standard OSS Examples
category: research
tags: [research, pull-request, pr-template, developer-experience, github, open-source, code-review]
status: complete
source: opus-2026-06-25
registry_id: R-1782412662
template_version: "research-1.2.0"
---

# PR Template Best Practices, Industry Standards & Gold-Standard OSS Examples

**Status:** stub

**Created:** 2026-06-16

<!-- doc:region name="context" kind="immutable" -->

## Context

We are standardizing how pull-request bodies are written across our enterprise portable-workspace
repo and our personal tooling repos. Today there is **no active `.github/pull_request_template.md`**
on the default branch — the only PR-body structure is a hand-typed convention captured in a
procedure doc (a `DO NOT MERGE` banner + `## Summary` + `## Test plan`), and a separate, never-merged
draft template (`Summary` / `Validation` / `Risk-Rollout` / `Screenshots`) sits stranded on a side
branch. We want to land one canonical, well-designed template.

Two concrete design questions triggered this research:
1. Whether and how to add an **Action Items** section (open obligations for *this* PR, placed near the
   top) and a separate **Follow-ups** section (work surfaced by this PR but deferred to a later PR,
   placed lower down) — combined vs. separate, and where they belong relative to Summary / Test plan /
   Risk.
2. What the broader **industry standard and gold-standard open-source** PR-template practice looks like,
   so our template reflects community consensus rather than ad-hoc local taste.

This research grounds a forthcoming PR-template (`.github/pull_request_template.md`) plus the
corresponding update to our PR-workflow procedure doc. The output is a reference survey, not an
implementation.

**Prior art (build on, do not duplicate):** portable-workspace `docs/research/cld-33-branch-pr-guidelines-research.md`
(Opus, 2026-05-21, status: complete) already surveyed the surrounding *PR workflow* — Conventional
Commits, branch/tag naming, PR size, label taxonomy, merge strategies, branch protection, changelog
automation, monorepo considerations. Its prompt also asked a "PR template best practices" question, but
that question was **not** synthesized into its findings (the run notes "Compile timed out; synthesis from
critic output"). This research therefore targets the **delta**: PR-template *anatomy and design* (sections,
ordering, the Action-Items-vs-Follow-ups question, gold-standard OSS examples, efficacy evidence, and
anti-patterns) — treating the ISSUE-REF workflow topics as already established.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->
## Temporal Scope

Primary window: 2025–2026, with emphasis on the two 2026-dated empirical papers and the November 2025 Copilot changelog. First-party template files (Kubernetes, React, Angular, Electron, Django, Go, GitLab) were fetched live this run and reflect their current default-branch state. The Zhang et al. large-scale study is treated as background-quality empirical context with an explicit access caveat (full text not first-party fetched). Pre-2024 material is used only as background. Where a subtopic (e.g., monorepo-specific template effects) had no post-2024 evidence, that absence is stated rather than padded.

## Executive Summary

1. **The load-bearing core is small (≈4–7 sections).** The recurring, high-value elements across the strongest fetched templates are *Summary/Why, Linked issue, What changed, Test plan, and a short checklist*, with Screenshots and Breaking-change/Risk as conditional add-ons. React ships only two sections at massive scale — direct disconfirming evidence against "more sections = better" [VERIFIABLE][^1].

2. **"What feedback do you want?" is a high-value but under-used element — with a precision caveat.** Pirouzkhah et al. (arXiv, Feb 2026) report that *purpose and code explanations are most valued by developers for preserving rationale and history*, and that the desired-feedback-type element relates to acceptance and reviewer engagement [VERIFIABLE][^2]. The specific magnitude claims ("64–72% higher merge likelihood") and the strong "best predictor" framing could **not** be confirmed against the fetched abstract and are downgraded accordingly (see Q5).

3. **Action Items and Follow-ups should be separate, both near the bottom.** No fetched gold-standard template ships either by name; the only operational difference between them is *merge-blocking semantics*, so combining them destroys the signal a reviewer/bot needs [INFERENCE]. The community norm for deferred work is to open and link a tracked issue [HEURISTIC].

4. **Templates correlate with better review outcomes, but the evidence is correlational and adoption is low.** Treat "templates measurably help" as an association, not an established cause — both major studies are observational, and the "written when most useful" finding means causality runs partly backwards [VERIFIABLE][^2].

5. **AI is reshaping the design calculus.** The Copilot coding agent now follows `pull_request_template.md` and picks the best-fit template when several exist (Nov 5, 2025) [VERIFIABLE][^3]. Design implication: use stable, unique, machine-parseable headings (and fenced blocks like Kubernetes' release-note) so agents and CI can target sections deterministically.

## Findings by Question

### Q1 — Anatomy & standard sections

**Sections that recur in high-quality fetched templates:**

| Section | Evidence in the wild |
|---|---|
| Summary / What + **Why** | Kubernetes ("What this PR does / why we need it") [VERIFIABLE][^4]; React ("Summary") [VERIFIABLE][^1]; GitLab ("What does this MR do and why?") [VERIFIABLE][^5]; Element84 ("capture the *why*") [VERIFIABLE][^6] |
| Linked issue / tracker | Kubernetes ("Which issue(s) this PR is related to") [VERIFIABLE][^4]; Go (`Fixes #1234` / `Updates #1234`) [VERIFIABLE][^7]; Element84 ("Connects") [VERIFIABLE][^6] |
| Type of change | Angular ("PR Type": Bugfix/Feature/Refactoring/CI/Docs) [VERIFIABLE][^8]; Kubernetes ("What type of PR is this?") [VERIFIABLE][^4] |
| Test plan / how-tested | React ("How did you test this change?") [VERIFIABLE][^1]; GitLab ("set up and validate locally") [VERIFIABLE][^5] |
| Checklist (`- [ ]`) | Angular, Electron, Django [VERIFIABLE][^8][^9][^10] |
| Screenshots / visual proof | GitLab (Before/After table) [VERIFIABLE][^5]; Django screenshot checklist item [VERIFIABLE][^10] (see access caveat below) |
| Breaking change | Angular (Yes/No) [VERIFIABLE][^8] |
| Release notes | Kubernetes (release-note fenced block) [VERIFIABLE][^4]; Electron ("Release Notes") [VERIFIABLE][^9] |
| Reviewer notes / caveats | Kubernetes ("Special notes for your reviewer") [VERIFIABLE][^4]; Element84 ("Notes") [VERIFIABLE][^6] |

**Essential vs. optional (synthesis):** *Essential* = Summary+Why, Linked issue, Test plan, short checklist. *Conditional* = Screenshots (UI/docs), Breaking-change/Risk-Rollback (services, migrations, auth/billing), Release notes (user-facing). This lean-core-plus-risk-layered-conditionals split is the convergent practitioner recommendation [HEURISTIC].

**Distinctive observations countering anchor bias:**
- **React's template is near-minimal** — only *Summary* + *How did you test this change?* A top-tier project deliberately ships two sections [VERIFIABLE][^1].
- **Go's "template" is not a section scaffold** — it is Gerrit-import instructions, commit-message rules, the `Fixes/Updates #` convention, and an explicit "We do not use Signed-off-by lines in Go" note. It encodes workflow, not sections [VERIFIABLE][^7].
- **Django adds an AI-assistance disclosure** with required checkboxes for whether AI tools were used, plus "I have not requested, and will not request, an automated AI review for this PR" — a genuinely 2025–2026 development [VERIFIABLE][^10].

> **Access caveat on Django's screenshot claim:** the in-session check marked the "screenshots in both light and dark modes" item UNVERIFIABLE despite the file being live. Treat the specific light/dark-mode wording as practitioner-surfaced rather than confirmed; the broader fact that Django uses a long pre-submit checklist with AI disclosure is confirmed [VERIFIABLE][^10].

#### Q1b — Where do "Action Items" vs "Follow-ups" go?

- **First-party finding:** None of the gold-standard templates fetched (Kubernetes, React, Angular, Electron, Go, Django, GitLab) ship a dedicated *"Action Items"* or *"Follow-ups"* section by that name. The closest is Element84's advisory **"Notes"** sub-section — "caveats, alternative strategies that didn't work out, anything else" — which is advisory, not a blocking-obligation tracker [VERIFIABLE][^6].
- **Community norm for deferred work:** open a follow-up issue and link it rather than leaving prose in the PR body [HEURISTIC].
- **Reasoning (why separate, not combined):** The only operational difference between the two categories is *merge-blocking semantics*. Action Items gate the merge of *this* PR; Follow-ups explicitly do not. Merging them into one list means a reviewer (or a bot scanning unchecked boxes) cannot tell which item should block. Separation preserves that signal [INFERENCE].
- **Placement:** Both belong at the bottom, below Test plan/Checklist. The high-value "why/what/how-tested" stays at top because that is what reviewers context-build from; the two task lists concern *future* state, not *understanding this diff* [INFERENCE].
- **Form:** Action Items = checkboxes with owners + ticket links; Follow-ups = prose/links to filed issues (no checkboxes), so they never read as "incomplete work in this PR" [INFERENCE].

#### Q1c — Length / point of diminishing returns

- Practitioner convergence: 5–7 core items; make extra sections conditional via HTML comments; revisit quarterly and drop sections reviewers ignore [HEURISTIC].
- Empirical backstop (with caveat): Zhang et al. report adopters' top complaint is "excessive and irrelevant information requests." This is treated as background-quality evidence — the full text was not first-party fetched (publisher returned HTTP 403); corroborated only via search-surfaced abstract content [HEURISTIC].
- React (two sections) at massive scale is the existence proof that the floor is very low for mature projects [VERIFIABLE][^1].

### Q2 — Gold-standard / widely-cited examples (annotated)

| Project | File (fetched) | Structure / distinctive choice |
|---|---|---|
| **Kubernetes** | `.github/PULL_REQUEST_TEMPLATE.md` | Type-of-PR → What/Why → Related issue → Special notes for reviewer → machine-parsed release-note fenced block. The release-note block is consumed by tooling [VERIFIABLE][^4]. |
| **React** | `.github/PULL_REQUEST_TEMPLATE.md` | *Summary* + *How did you test this change?* Deliberately minimal [VERIFIABLE][^1]. |
| **Angular** | `.github/PULL_REQUEST_TEMPLATE.md` | Checklist → PR Type taxonomy (Bugfix/Feature/Refactoring/CI/Docs) → breaking-change Yes/No. Most "classic" full template [VERIFIABLE][^8]. |
| **Electron** | `.github/PULL_REQUEST_TEMPLATE.md` | Description → checklist incl. an AI-policy "I have reviewed and verified the changes" item → Release Notes section [VERIFIABLE][^9]. |
| **Django** | `.github/pull_request_template.md` | Trac ticket → required AI Assistance Disclosure → long checklist incl. "I will not request an automated AI review." Most explicit AI-era governance [VERIFIABLE][^10]. |
| **Go** | `.github/PULL_REQUEST_TEMPLATE` | Gerrit-import instructions + commit-message rules + `Fixes/Updates #` convention; "We do not use Signed-off-by lines in Go." Workflow-as-template [VERIFIABLE][^7]. |
| **GitLab (gitlabhq)** | `.gitlab/merge_request_templates/Default.md` | What/why (auto-fills via `%{first_multiline_commit}`) → References → Before/After screenshot table → set-up & validate locally → MR acceptance checklist linked (not inlined) [VERIFIABLE][^5]. |
| **Element84 (DX write-up)** | element84.com article | Overview(+Connects) → Notes (caveats/alternatives/questions). Best articulated rationale for *why* each section exists [VERIFIABLE][^6]. |

**Curated lineups:** `devspace/awesome-github-templates` is the most-cited "awesome" list, defining an awesome template as one that "informs contributors how to proceed in a very detailed or unique way" — i.e., it rewards clarity of guidance, not length [VERIFIABLE][^11].

**Honest negatives (anchor-bias counter):** PR templates for **Node.js**, **VS Code**, **Rust**, and **TensorFlow** could not be retrieved at conventional paths (404/load errors). Several likely have no repo-root PR template or relocated guidance into `CONTRIBUTING.md`. Flagged rather than fabricated [NO SOURCE].

### Q3 — Mechanics & platform features

- **Single vs. multiple templates (GitHub):** A single template lives at `.github/pull_request_template.md`. For multiple, use the `.github/PULL_REQUEST_TEMPLATE/` directory and select via the `template` query parameter [VERIFIABLE][^12]. Templates take effect only once merged to the default branch [VERIFIABLE][^12].
- **Org-level defaults:** A default template in an org's `.github` repo applies to owned repos lacking their own [HEURISTIC]; VS Code GitHub Pull Requests extension behavior is extension-specific [HEURISTIC].
- **When multiple templates are worth it:** when distinct change classes (feature/bugfix/docs/release) genuinely need different prompts AND contributors will reliably pick; otherwise a single conditional template (with HTML-comment "delete if N/A" guidance) is lower-friction [HEURISTIC].
- **HTML comments / checklists:** Comments give inline guidance without cluttering the rendered description. `- [ ]` checklists are *not* gated by GitHub natively — enforcement requires CI/Actions or merge-gating bots [INFERENCE].
- **GitLab specifics:** `.gitlab/merge_request_templates/`, `%{first_multiline_commit}` interpolation, Before/After screenshot table, and a linked acceptance checklist [VERIFIABLE][^5].
- **Issue linking:** closing keywords and Go's `Fixes #1234` / `Updates #1234` (partial) convention [VERIFIABLE][^7].
- **Relationship to CONTRIBUTING:** The template is the point-of-action prompt; long rules belong in `CONTRIBUTING.md` — GitLab demonstrates this by linking its acceptance checklist instead of inlining it [VERIFIABLE][^5].

### Q4 — Conventions that interact with the template

- **Type-of-change taxonomies** are common (Angular's Bugfix/Feature/Refactoring/CI/Docs list [VERIFIABLE][^8]; Kubernetes' "What type of PR is this?" [VERIFIABLE][^4]). These dovetail with Conventional Commits / semantic PR titles (assumed established per ISSUE-REF).
- **Semantic title generation:** The specific Feb-2026 Copilot PR-*title* feature was not first-party fetched and is reported as practitioner-surfaced [HEURISTIC]. What is confirmed is the broader direction: Copilot tooling now integrates with PR workflows, including following templates [VERIFIABLE][^3].
- **DCO/sign-off:** Go explicitly forbids `Signed-off-by` lines, enforcing CLA via Gerrit/GitHub bots instead — a reminder that sign-off conventions are project-specific and should live in the template/CONTRIBUTING [VERIFIABLE][^7].

### Q5 — Does it actually work? (evidence, with precision caveats)

**Supporting evidence:**
- **Pirouzkhah et al. (arXiv, Feb 2026):** *purpose and code explanations are valued by developers for preserving the rationale and history of changes* [VERIFIABLE][^2]; descriptions are *more common in mature projects and complex changes, suggesting they are written when most useful rather than as a formality* [VERIFIABLE][^2].
- **Copilot-for-PRs evidence:** PRs enhanced by Copilot for PRs require less review time and have a higher likelihood of being merged [VERIFIABLE][^13].

**Precision and provenance caveats (surfaced deliberately):**
- The strong claim that "stating the desired feedback type *best predicts* change acceptance and reviewer engagement" was marked **UNVERIFIABLE** against the fetched abstract. Downgraded: the desired-feedback element *relates to* acceptance/engagement, but the "best predictor" superlative and any numeric odds ratios (e.g., "64–72%") are **not** confirmed and should not be cited as settled [HEURISTIC].
- Pirouzkhah et al. is a February 2026 arXiv preprint; at draft time it was not yet peer-reviewed. Its regressions are correlational/observational [INFERENCE].
- The Zhang et al. large-scale study (538,864 repos; ~1.2% adoption; "less review time / fewer duplicate PRs / fewer invalid comments"; "excessive and irrelevant information requests" complaint) could **not** be first-party fetched (HTTP 403 from ScienceDirect and ACM). These figures are therefore reported as background-quality, search-surfaced claims — useful directionally, but **not** tagged [NO SOURCE — likely fabricated] [HEURISTIC].

**Disconfirming / contrarian evidence:**
- **Correlation, not causation.** Both major studies are observational. The "written when most useful" finding means causality runs partly backwards — *good projects write descriptions*, not necessarily *descriptions make projects good* [INFERENCE]. The Executive Summary's "templates measurably help" should be read as an association.
- **Over-asking backfires.** Search-surfaced reports of adopters' top complaint being excessive/irrelevant requests suggest templates can suppress contribution [HEURISTIC].
- **Code quality dominates outcomes.** Structured descriptions are necessary-not-sufficient for merges [INFERENCE].

### Q6 — Anti-patterns

| Anti-pattern | Why it fails | Basis |
|---|---|---|
| **Template sprawl** | Over-asking is a recurring adopter complaint; can suppress PR volume | [HEURISTIC] |
| **Checkbox theater** | Vague, unverifiable boxes become ritual; GitHub doesn't gate on them | [HEURISTIC] + non-enforcement [INFERENCE] |
| **Bookkeeping-first ordering** | Buries the context reviewers build from | [INFERENCE] |
| **Restating the ticket title** | No rationale preserved — the element devs most value (purpose/explanation) is missing | [VERIFIABLE][^2] |
| **Requiring info CI already proves** | Redundant friction; move proof to status checks | [HEURISTIC] |
| **Inlining CONTRIBUTING rules into every PR** | Bloats the body; GitLab's "link the checklist" pattern is the fix | [VERIFIABLE][^5] |
| **Combining Action Items with Follow-ups** | Destroys merge-blocking signal | [INFERENCE] |
| **Cargo-culting a big template from another org** | Inherits ceremony without the originating risk context | [HEURISTIC] |

### Emergent thread — AI generation vs. templates

- **Copilot coding agent now follows `pull_request_template.md`** and, when multiple templates exist, picks the one best suited to the task [VERIFIABLE][^3]. Design implication: stable, unique, machine-parseable headings and fenced blocks (Kubernetes' release-note block [VERIFIABLE][^4]; GitLab's `%{first_multiline_commit}` [VERIFIABLE][^5]) let both agents and CI target sections deterministically.
- **AI governance as a new section class:** Electron's AI-policy review checkbox [VERIFIABLE][^9] and Django's AI Assistance Disclosure [VERIFIABLE][^10] show projects encoding AI governance directly into the template — an emergent category not in the original question set.
- The folk claim that Copilot's *interactive* PR summary "ignores existing description content and works best on a blank box" was **not** first-party fetched; reported as practitioner lore only [HEURISTIC].

### Adjacent lens — cognitive load and form design (reviewer-suggested, integrated)

Treating a template as a worked-example scaffold for a communication task suggests two design moves grounded in instructional-design and form-design principles: (1) prefer inline examples / placeholder text over blank fields, since blank fields systematically elicit less effort; (2) minimize the number of simultaneous prompts, since each added prompt raises extraneous cognitive load and pushes contributors toward perfunctory completion. These are theory-grounded design heuristics, not findings from the fetched PR-template sources [HEURISTIC].

## Gaps and Open Questions

- **Pirouzkhah magnitude claims unconfirmed.** Only the abstract was fetched. The "best predictor" superlative and any percentage odds ratios are unverified; the full paper (and its peer-review status) should be re-checked before quoting numbers.
- **Zhang et al. not first-party fetched.** All Zhang-derived figures (adoption ~1.2%, review-time/duplicate-PR/invalid-comment effects, over-asking complaint) rest on search-surfaced abstract content behind a 403 paywall. Confirm against the published DOI before treating as load-bearing.
- **Sample skew toward reachable templates.** Node.js, VS Code, Rust, and TensorFlow templates were unreachable, so the "industry consensus" sample skews toward projects with retrievable templates. A fuller census could shift the "essential sections" weighting.
- **No post-2024 monorepo-specific template study.** The conditional/per-repo guidance is practitioner consensus; no significant post-2024 empirical development was found isolating monorepo template effects [NO SOURCE].
- **Org-level / extension behavior** (org `.github` defaults, VS Code extension honoring owner-level templates) is reported from practitioner knowledge, not a fetched first-party doc.
- **Causal direction unresolved.** No causal (RCT/quasi-experimental) evidence that templates *cause* better outcomes; all in-scope evidence is observational.

## Sources

[^1]: Facebook/React. (n.d.). Pull request template. https://raw.githubusercontent.com/facebook/react/main/.github/PULL_REQUEST_TEMPLATE.md
[^2]: Pirouzkhah, et al. (2026). Pull request descriptions: What developers value and what predicts acceptance. https://arxiv.org/abs/2602.14611
[^3]: GitHub. (2025, November 5). Copilot coding agent now supports pull request templates. https://github.blog/changelog/2025-11-05-copilot-coding-agent-now-supports-pull-request-templates/
[^4]: Kubernetes. (n.d.). Pull request template. https://raw.githubusercontent.com/kubernetes/kubernetes/master/.github/PULL_REQUEST_TEMPLATE.md
[^5]: GitLab. (n.d.). Default merge request template. https://raw.githubusercontent.com/gitlabhq/gitlabhq/master/.gitlab/merge_request_templates/Default.md
[^6]: Element84. (n.d.). GitHub pull request template workflow. https://element84.com/software-engineering/github-pull-request-template-workflow/
[^7]: Go (golang). (n.d.). Pull request template. https://raw.githubusercontent.com/golang/go/master/.github/PULL_REQUEST_TEMPLATE
[^8]: Angular. (n.d.). Pull request template. https://raw.githubusercontent.com/angular/angular/main/.github/PULL_REQUEST_TEMPLATE.md
[^9]: Electron. (n.d.). Pull request template. https://raw.githubusercontent.com/electron/electron/main/.github/PULL_REQUEST_TEMPLATE.md
[^10]: Django. (n.d.). Pull request template. https://raw.githubusercontent.com/django/django/main/.github/pull_request_template.md
[^11]: devspace. (n.d.). Awesome GitHub templates. https://github.com/devspace/awesome-github-templates
[^12]: GitHub. (n.d.). Creating a pull request template for your repository. https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/creating-a-pull-request-template-for-your-repository
[^13]: Xiao, et al. (2024). Automatic pull request description generation with Copilot for PRs. https://arxiv.org/abs/2402.08967
<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

```text
You are a staff-level software engineer and developer-experience lead who has designed
pull-request workflows, contribution guides, and PR/issue templates for large open-source
projects and enterprise monorepos. You have strong, evidence-backed opinions about what makes
a PR template actually improve review quality and merge velocity versus what becomes ignored
boilerplate. When you cannot find a source, you say so explicitly.

## Background (assume established; do not re-derive)
- A PR template = a Markdown file (commonly `.github/pull_request_template.md`, or multiple under
  `.github/PULL_REQUEST_TEMPLATE/`) that GitHub/GitLab auto-loads into the PR description box.
- The goal of this research is a reference survey to ground the design of a single canonical
  template plus a short authoring convention. It is NOT a request to write our template.
- Two specific design questions motivate it: (a) how the community handles an "Action Items"
  section (open obligations for the current PR) versus a "Follow-ups" / "Future work" section
  (work surfaced here but deferred to a later PR) — including whether to combine them and where to
  place them; (b) what the gold-standard examples and overall industry consensus look like.
- A prior internal research doc (ISSUE-REF, 2026-05-21) already covered the surrounding PR *workflow*
  (Conventional Commits, branch naming, PR size, labels, merge strategies, branch protection,
  changelog, monorepo). Assume those established; focus this run on PR-template *design* — do not
  re-derive the workflow topics.

## Research goal
Survey current best practices, industry standards, and the most respected real-world examples of
pull-request templates, and synthesize concrete, evidence-backed design guidance.

## Scope note — questions, examples, and named references are a starting point, not a checklist
The questions, topics, and named examples below are illustrative anchors and a FLOOR for this
research — not an exhaustive list to answer only or evaluate only. Reason independently: survey the
landscape broadly, follow the evidence where it leads, expand scope where warranted, and surface
relevant work, factors, and failure modes not named here. An output that merely fills in the listed
items has NOT met the research goal.

## Questions (a floor, not a ceiling)

### 1. Anatomy & standard sections
- What sections recur across high-quality PR templates (e.g., Summary/Description, Motivation/Why,
  Type of change, How-tested / Test plan, Checklist, Screenshots, Breaking changes, Risk/Rollout,
  Linked issues, Reviewer notes)? Which are considered essential vs. optional?
- Where do "Action Items" (obligations for THIS PR) and "Follow-ups / Future work / Out-of-scope"
  (deferred to a LATER PR) appear in practice? Are they typically present at all, combined, or
  separate? Recommended ordering/placement (top vs. bottom) and why?
- Optimal length / number of sections before a template becomes ignored boilerplate. Evidence on
  the point of diminishing returns.

### 2. Gold-standard / widely-cited examples
- Identify specific, popular, well-regarded PR templates from major OSS projects and orgs
  (e.g., Kubernetes, Rust, React/React Native, VS Code, TensorFlow, Angular, Electron, Node.js,
  Go, Django, GitHub's own repos, Microsoft, Google, GitLab, Atlassian). For each notable example,
  link the actual template file and summarize its structure and any distinctive choices.
- Curated lineups (e.g., "awesome" lists, devrel/DX write-ups) of exemplary PR templates — what do
  they converge on?

### 3. Mechanics & platform features
- Single template vs. multiple templates (GitHub `PULL_REQUEST_TEMPLATE/` directory + `?template=`
  query param; GitLab description templates). When are multiple templates worth it?
- HTML comments as inline guidance, checklists (`- [ ]`) and how/whether they're enforced, default
  labels/assignees/reviewers, and the relationship between PR templates, `CONTRIBUTING.md`, and
  CODEOWNERS.
- Linking to issues / trackers (closing keywords, Jira/Linear conventions) and how templates encode it.

### 4. Conventions that interact with the template
- Conventional Commits / semantic PR titles, "Type of change" taxonomies, size/scope guidance
  (small-PR culture), and DCO/sign-off or "DO NOT MERGE" / draft conventions.

### 5. Does it actually work? (evidence)
- Empirical or well-argued evidence that PR templates (and specific sections/checklists) improve
  review quality, review latency, defect-catch rate, or contributor experience — and any evidence
  of the failure mode where templates are skipped or filled in perfunctorily. What drives adoption
  vs. abandonment?

### 6. Anti-patterns
- Common mistakes: overlong templates, redundant sections, checkbox theater, sections that belong in
  CONTRIBUTING instead, requiring info CI already proves, etc.

## Independent exploration (gaps, blindspots, emergent threads) — required
Treat the question list as a FLOOR, not a ceiling. As you research, actively surface what this
framing may be missing and pursue each promising thread to a logical conclusion:
- Adjacent or upstream factors the questions don't capture (e.g., AI-reviewer-bot interaction with
  template structure; auto-generated PR descriptions; monorepo vs. polyrepo differences).
- Contrarian / disconfirming evidence — report it even when it challenges the premise that templates help.
- Emerging 2025–2026 practices, tools, or research not anchored by the named examples.
- Known failure modes and second-order effects.
Whenever a load-bearing thread surfaces mid-research, follow it to its conclusion and report it in a
dedicated "Gaps, blindspots & emergent findings" subsection. Explicitly NAME any blindspot you
suspect but cannot resolve (and why) rather than omitting it. Anchor bias — over-fitting to the listed
questions and example projects — is a known failure mode; counter it deliberately and say where you did.

## Deliverable
A synthesized reference with: (a) a recommended canonical section set with ordering and rationale;
(b) an explicit recommendation on the Action-Items-vs-Follow-ups question (combine vs. separate,
placement); (c) an annotated table of gold-standard examples with links; (d) evidence on efficacy;
(e) anti-patterns to avoid. Use Mermaid for any diagrams.

## Retrieval enforcement (mandatory)
For each load-bearing claim, perform at least one web_search or web_fetch; do not return a
[VERIFIABLE] tag without a URL you actually fetched in-session. Link the actual template files you
cite. Do not stop after one round — if a sub-question yields nothing, run additional searches across
alternate framings before tagging [NO SOURCE]; that tag is reserved for genuine evidence absence,
not retrieval avoidance.

<grounding_instructions>
You are a staff-level software engineer and developer-experience lead with years of experience
designing PR/issue templates and contribution workflows for large open-source projects and
enterprise monorepos. You have strong opinions backed by evidence. When you cannot find a source,
you say so explicitly.

Temporal scope: Weight sources by recency — 2026 (primary) → 2025 → 2024. Pre-2024 sources are
background context only unless foundational to the topic (the GitHub PR-template feature and several
gold-standard templates predate 2024 and remain authoritative — treat those as current). If post-2024
literature is genuinely sparse for a subtopic, state "[subtopic]: no significant post-2024
developments found" rather than backfilling with older sources. Backfilling is a failure mode, not a hedge.

Before generating your final output, execute a Chain-of-Verification (CoVe) to ensure factual
fidelity over compliance.

Inside your thought process:
1. Isolate the core facts required.
2. Draft a tentative response.
3. Hostile Cross-Examination: flag any claim where you are citing a source because the prompt implied
   you should, rather than because you verified it.
4. Strip away any claim that cannot be empirically verified.

When generating your final output, classify every major claim. Write your rationale before appending
the tag — writing the tag first causes post-hoc rationalization. Rationale → evidence check → tag.

- [VERIFIABLE]: backed by documentation, peer-reviewed research, or official tech blogs (2024–2026),
  or the actual template/source file. Carry an inline footnote ref to the source: [VERIFIABLE][^N].
- [HEURISTIC]: widely accepted best practice without a specific citation.
- [INFERENCE]: a logical conclusion drawn from context. Provide your reasoning in-text. Do not
  fabricate a source. Tier tag only — NO footnote ref.
- [NO SOURCE]: explicitly state when you cannot find verifiable data. Tier tag only — NO footnote ref.

Citation format (mandatory for every externally-sourced claim):
- Inline: append the GFM footnote ref directly after the tier tag — [VERIFIABLE][^N]. A claim citing
  multiple sources carries ascending separate refs — [VERIFIABLE][^3][^7] (never grouped [^3,^7];
  never out of order).
- Footnote definitions live once, under ## Sources, in APA form with a clickable URL or DOI and an
  access-verification stamp. Worked example:
    [^1]: GitHub. (2026). [Creating a pull request template for your repository](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/creating-a-pull-request-template-for-your-repository). GitHub Docs. Verified accessible (HTTP 200) 2026-06-16. (Scope note.)
- URL-or-DOI ALWAYS: every source entry carries a clickable URL or a doi.org link — paywalled/gated is
  fine (link it anyway; stamp the access status). Only the truly-irreducible case (no online catalog
  presence anywhere) gets an explicit [no online source located] marker with a one-line justification.
- Integrity: footnote refs are contiguous from [^1], every [^N] ref has a matching definition, and
  every definition is referenced — no gaps, no orphans.
- [INFERENCE] / [NO SOURCE] claims carry the tier tag with NO footnote ref.

Hard constraint (overrides all formatting preferences): never invent a citation to satisfy a
formatting instruction. Accuracy > completeness.

Format diagrams using Mermaid.js or ASCII. Format math using LaTeX. NEVER generate binary images.
</grounding_instructions>
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->
## Appendix: Provenance Ledger

Full machine-readable ledger: [pr-template-best-practices.provenance.jsonl](pr-template-best-practices.provenance.jsonl)

| Verdict | Claim | Source | Quote |
|---|---|---|---|
| [UNVERIFIABLE] | Pirouzkhah et al. analyzed 80K GitHub PRs across 156 projects and found that stating the desired feedback type best predicts change acceptance and reviewer engagement. | https://arxiv.org/abs/2602.14611 | stating the desired feedback type best predicts change acceptance and reviewer engagement |
| [VERIFIABLE] | Pirouzkhah et al. found that purpose and code explanations are valued by developers for preserving the rationale and history of changes. | https://arxiv.org/abs/2602.14611 | purpose and code explanations are valued by developers for preserving the rationale and history of changes |
| [VERIFIABLE] | PR descriptions are more common in mature projects and complex changes, suggesting they are written when most useful rather than as a formality. | https://arxiv.org/abs/2602.14611 | PR descriptions are also more common in mature projects and complex changes, suggesting they are written when most useful rather than as a formality. |
| [VERIFIABLE] | The Kubernetes PR template includes sections for type of PR, what the PR does/why, related issues, special notes for reviewer, and a release-note fenced block. | https://raw.githubusercontent.com/kubernetes/kubernetes/master/.github/PULL_REQUEST_TEMPLATE.md | #### What type of PR is this?  #### What this PR does / why we need it: #### Which issue(s) this PR is related to:  #### Special notes for your reviewer: #### … |
| [PLAUSIBLE] | React's PR template contains only two sections: Summary and How did you test this change? | https://raw.githubusercontent.com/facebook/react/main/.github/PULL_REQUEST_TEMPLATE.md | ## Summary  ## How did you test this change? |
| [VERIFIABLE] | Angular's PR template includes a PR Type checklist with options including Bugfix, Feature, Refactoring, CI related changes, and Documentation content changes. | https://raw.githubusercontent.com/angular/angular/main/.github/PULL_REQUEST_TEMPLATE.md | \- [ ] Bugfix \- [ ] Feature \- [ ] Code style update (formatting, local variables) \- [ ] Refactoring (no functional changes, no api changes) \- [ ] Build rel… |
| [VERIFIABLE] | Angular's PR template asks whether the PR introduces a breaking change with a Yes/No checkbox. | https://raw.githubusercontent.com/angular/angular/main/.github/PULL_REQUEST_TEMPLATE.md | ## Does this PR introduce a breaking change? \- [ ] Yes \- [ ] No |
| [PLAUSIBLE] | Electron's PR template includes a checklist item for reviewing and verifying changes per an AI policy. | https://raw.githubusercontent.com/electron/electron/main/.github/PULL_REQUEST_TEMPLATE.md | \- [ ] [I have reviewed and verified the changes](https://github.com/electron/governance/blob/main/policy/ai.md) |
| [VERIFIABLE] | Electron's PR template includes a Release Notes section. | https://raw.githubusercontent.com/electron/electron/main/.github/PULL_REQUEST_TEMPLATE.md | #### Release Notes Notes: |
| [VERIFIABLE] | Django's PR template includes a required AI Assistance Disclosure section with checkboxes for whether AI tools were used. | https://raw.githubusercontent.com/django/django/main/.github/pull_request_template.md | #### AI Assistance Disclosure (REQUIRED)  \- [ ] **No AI tools were used** in preparing this PR. \- [ ] **If AI tools were used**, I have disclosed which ones,… |
| [VERIFIABLE] | Django's PR template includes a checklist item stating 'I have not requested, and will not request, an automated AI review for this PR.' | https://raw.githubusercontent.com/django/django/main/.github/pull_request_template.md | \- [ ] I have not requested, and will not request, an automated AI review for this PR. |
| [UNVERIFIABLE] | Django's PR template requires screenshots in both light and dark modes for any UI changes. | https://raw.githubusercontent.com/django/django/main/.github/pull_request_template.md | \- [ ] I have attached screenshots in both light and dark modes for any UI changes. |
| [PLAUSIBLE] | The Go PR template is an instruction sheet for Gerrit import and commit-message formatting rather than a description scaffold, and explicitly states 'We do not use Signed-off-by lines in Go.' | https://raw.githubusercontent.com/golang/go/master/.github/PULL_REQUEST_TEMPLATE | We do not use Signed-off-by lines in Go. Please don't add them. Our Gerrit server & GitHub bots enforce CLA compliance instead. |
| [VERIFIABLE] | The Go PR template instructs contributors to add 'Fixes #1234' or 'Updates #1234' for corresponding issues. | https://raw.githubusercontent.com/golang/go/master/.github/PULL_REQUEST_TEMPLATE | If there is a corresponding issue, add either `Fixes #1234` or `Updates #1234` (the latter if this is not a complete fix) to this comment |
| [PLAUSIBLE] | GitLab's default MR template includes a Before/After screenshot table and uses the %{first_multiline_commit} variable. | https://raw.githubusercontent.com/gitlabhq/gitlabhq/master/.gitlab/merge_request_templates/Default.md | ## What does this MR do and why? %{first_multiline_commit} ## References  ## Screenshots or screen recordings  \| Before \| After \| \| ------ \| ------ \| \| \| \| |
| [VERIFIABLE] | GitLab's MR template offloads the checklist to an external document by linking to the MR acceptance checklist rather than inlining it. | https://raw.githubusercontent.com/gitlabhq/gitlabhq/master/.gitlab/merge_request_templates/Default.md | ## MR acceptance checklist Evaluate this MR against the [MR acceptance checklist](https://docs.gitlab.com/development/code_review/#acceptance-checklist). |
| [VERIFIABLE] | GitHub allows multiple PR templates by placing them in a .github/PULL_REQUEST_TEMPLATE/ directory and selecting via the 'template' query parameter. | https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/creating-a-pull-request-template-for-your-repository | You can create a *PULL_REQUEST_TEMPLATE/* subdirectory in any of the supported folders to contain multiple pull request templates, and use the `template` query… |
| [PLAUSIBLE] | PR templates on GitHub only take effect once merged to the repository's default branch. | https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/creating-a-pull-request-template-for-your-repository | Templates are available to collaborators when they are merged into the repository's default branch. |
| [VERIFIABLE] | If a repository includes multiple pull request templates, the Copilot coding agent will pick the one best suited to the task. | https://github.blog/changelog/2025-11-05-copilot-coding-agent-now-supports-pull-request-templates/ | If your repository includes multiple pull request templates, Copilot will pick the one best suited to the task. |
| [PLAUSIBLE] | Element84's PR template Overview section is intended to capture the 'why' behind changes, with a Connects line linking to issues. | https://element84.com/software-engineering/github-pull-request-template-workflow/ | Just like good commit messages, it should capture the why which might not be apparent from the code itself.  Immediately following the description is a **Conne… |
| [PLAUSIBLE] | Element84's Notes section is described as a place for caveats, alternative strategies that didn't work out, and questions to the reviewer. | https://element84.com/software-engineering/github-pull-request-template-workflow/ | ### Notes Optional. Ancillary topics, caveats, alternative strategies that didn't work out, anything else. |
| [PLAUSIBLE] | The devspace/awesome-github-templates list defines an awesome template as one that 'informs contributors how to proceed in a very detailed or unique way.' | https://github.com/devspace/awesome-github-templates | An awesome template is one that informs contributors how to proceed in a very detailed or unique way. |
| [VERIFIABLE] | PRs enhanced by Copilot for PRs require less review time and have a higher likelihood of being merged. | https://arxiv.org/abs/2402.08967 | PRs enhanced by Copilot for PRs require less review time and have a higher likelihood of being merged. |
<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

### Run 1 — 2026-06-25T19:09:45
- **aido**: aido 0.6.0 (gdd90e14)
- **Config**: `v2:depth:thorough(manual)`
- **Mode**: solo
- **Query**: "You are a staff-level software engineer and developer-experience lead who has designed
pull-request "
- **Loops**: 2 (of 2 max)
- **Claude researcher**: n/a (T-10 pipeline)
- **Gemini researcher**: ? external passes
  - Model: skipped
- **citation_enforcement**: repair_attempts=2, downgrades=1, complete=True
  - [hallucinated] (no url) (HTTP 0): downgraded → [NO SOURCE — likely fabricated]
<!-- /doc:region name="run_history" -->
