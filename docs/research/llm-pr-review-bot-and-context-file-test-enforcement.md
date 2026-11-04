---
title: "Designing an LLM PR-Review Bot and Agent Context Files to Enforce Test and Doc Sync"
category: research
status: draft
source: "Genericized from prior personal research; external claims cited inline."
template_version: "research-1.1.0"
---

# Designing an LLM PR-Review Bot and Agent Context Files to Enforce Test and Doc Sync

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [Findings by Question](#findings-by-question)
- [Gaps and Open Questions](#gaps-and-open-questions)
- [Sources](#sources)

<!-- doc:region name="context" kind="immutable" -->

## Context

A common setup: a repository runs an LLM PR-review bot (for example, GitHub Actions with an agentic code-review action) that reviews diffs for correctness, security, and operational risk, but does not flag missing test or doc updates. The project's agent context file (a `CLAUDE.md` / `AGENTS.md`-style file) carries an unenforced, path-scoped rule asking contributors to update the relevant tests and docs in the same change when behavior changes; real PRs still drift. This note determines how to design (a) the review-bot prompt and (b) the agent context files and linked procedure docs so that code changes keep their test suites and docs in sync — in a way that is effective, low-noise, and durable enough that a team keeps it rather than ripping it out. The approach is belt-and-suspenders: an advisory bot check plus a documented expectation. Scope-aware CI required checks and regression-test selection are covered separately and are out of scope here.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

Primary sources are weighted to 2025 and 2026: a public production-review write-up (Cloudflare), GitHub Copilot Code Review documentation and a GitHub blog metrics post, CodeRabbit pre-merge-check docs, agentic code-review action configuration references, Codecov patch-gate docs, Claude Code memory/`@import` docs, the AGENTS.md spec, and an ETH Zurich context-files study (arXiv 2602.11988, v2 Jun 2026). Community and survey signals (the Stack Overflow 2025 developer survey; public developer commentary on bot noise) are 2025-dated. Pre-2024 material (Danger JS/Ruby patterns, older PR-template conventions) is used only as durable background where the mechanism has not changed.

## Executive Summary

1. **Lead with an explicit "What NOT to flag" section, not a longer checklist.** The highest-leverage prompt technique in production low-noise reviewers is negative scoping. One public production system averages 1.2 findings per review by design and credits the low number directly to its "What NOT to Flag" prompt sections [VERIFIABLE][^1]; GitHub reports Copilot code review stays silent in about 29% of reviews by design, to keep signal high [VERIFIABLE][^10]. Alert fatigue from low-value comments is the most commonly cited reason teams mute or remove review bots. [HEURISTIC]
2. **Make the LLM advisory and put the deterministic floor in a non-LLM check.** "Did source ship without a matching test/doc change?" is a file-pattern predicate best answered by a rule engine (Danger) and a coverage gate (Codecov `patch`): cheap, reproducible, no hallucination. Reserve the LLM for the judgment layer — whether this is the *kind* of change that needs a test/doc update and whether the existing test actually asserts the new path. The deterministic side draws on Codecov's patch-coverage semantics [VERIFIABLE][^5]; CodeRabbit's own docs note the LLM sandbox cannot run your suite and reasons over the diff only [VERIFIABLE][^4]. [INFERENCE]
3. **Shrink and imperative-ize context files; don't grow them.** The ETH Zurich study found that providing context files did not generally improve task success and raised inference cost by over 20% on average, and that repository overviews are not helpful, while specific imperative instructions are well followed [VERIFIABLE][^6]. Keep effective files short (the Claude Code docs target under 200 lines) and keep the test/doc-sync rule as a tight, path-scoped imperative. [VERIFIABLE][^7]
4. **Use one canonical checklist referenced everywhere.** Point the context file, the review-bot prompt, and (optionally) the PR template at the same file. In Claude Code, a bare text mention does not load a file into context; the `@path` import syntax does. A single canonical source eliminates drift in one move. [VERIFIABLE][^7]
5. **Durability comes from a false-positive budget, advisory framing, an escape hatch, and a warning-first rollout.** One production system's `break glass` override was needed in only 0.6% of merge requests [VERIFIABLE][^1]; CodeRabbit recommends introducing a check in `warning` before moving it to `error` [VERIFIABLE][^4]; Copilot only ever leaves a non-blocking "Comment" review [VERIFIABLE][^2]. Making the test/doc-sync bot blocking on day one is the fastest path to it being removed. [INFERENCE]

## Findings by Question

### RQ1: Prompt engineering for a low-noise test/doc-sync reviewer

**Structure findings, don't free-form.** In one public production system, sub-reviewers return their findings as structured XML rather than prose, and a coordinator judges severity and posts a single structured review comment, so downstream dedup, approve, and block logic operates on data [VERIFIABLE][^1]. Agentic code-review actions can enforce the same discipline: pass a JSON schema (for example, via a `--json-schema` flag) and gate on the validated structured output. [VERIFIABLE][^3]

**"What NOT to flag" is the load-bearing lever.** The public production reviewer pairs a "What to Flag" list with an explicit "What NOT to Flag" list (for example, defense-in-depth suggestions when primary defenses are adequate, and "consider using library X" style suggestions). The stated finding: telling an LLM what not to do is where the actual prompt-engineering value resides; without those boundaries you get a firehose of speculative warnings. The low volume (about 1.2 findings per review) is attributed to these sections rather than the 10-plus low-quality findings a naive prompt produces. [VERIFIABLE][^1]

For a **test/doc-sync agent**, the exclusion list is the whole game. It should explicitly *not* flag:
- private/internal helpers, getters/setters, pure refactors with no behavior change;
- generated code, lockfiles, and vendored dependencies; [HEURISTIC]
- changes where a test/doc update *is* present (avoid the classic false "you didn't add tests" on a PR that did);
- trivial changes (typo fixes, comment-only edits).

**Severity calibration and advisory framing.** GitHub's numbers are the cleanest public evidence that silence beats volume: about 71% of reviews get actionable feedback, about 29% get nothing, and the system averages roughly 5.1 comments per review when it speaks [VERIFIABLE][^10]. Copilot is also advisory by construction: it always leaves a "Comment" review, so its reviews do not count toward required approvals and do not block merging [VERIFIABLE][^2]. For test/doc-sync, the correct default severity is `warning` or `suggestion`, never `critical`. [INFERENCE]

**Make the bot use repo context**, in descending reliability:
1. **Import the canonical checklist** into the context file so it is already in the agent's context. [VERIFIABLE][^7]
2. **Reference an existing risk map** (a file marking high-materiality paths) and CODEOWNERS so the agent knows which paths matter most. [INFERENCE]
3. **Let the agent read files.** In the public production system, sub-reviewers are free to read source files, grep, or search the codebase before flagging [VERIFIABLE][^1]; agentic review actions likewise have Read and Grep. This is what lets the bot check whether an *existing* test file actually asserts the new branch rather than only whether a test file exists. [INFERENCE]

**Self-consistency and verification pass.** A coordinator that deduplicates findings and judges their actual severity before posting drops noise [VERIFIABLE][^1]. A cheaper single-agent analog: instruct the reviewer to re-read each candidate finding and drop it unless it can name the specific untested branch or the specific doc that is now stale. Some review actions can also buffer inline comments and classify them (real review vs test/probe) with a small model before posting, suppressing subagent noise. [VERIFIABLE][^3]

**Constrain the bot's tools and treat PR content as untrusted.** A review bot reads attacker-influenceable text: the diff, the PR title and body, and any linked content. That content can attempt prompt injection against the reviewer (for example, a comment in a diff instructing the bot to approve or to skip the test check). Scope the review agent's tool permissions to read-only review needs (Read, Grep, repo search) and never grant write or arbitrary-exec tools in the review workflow, so a successful injection cannot mutate the repo. Keep the "What NOT to flag" and severity rules in the system prompt, where PR content is less able to override them. [INFERENCE]

**The top reason review bots get muted is noise and false positives.** Developer sentiment is directional: the Stack Overflow 2025 survey found more developers actively distrust the accuracy of AI tools (46%) than trust it (33%), so a noisy bot spends scarce trust fast [VERIFIABLE][^8]. A common practitioner remedy is to track per-category dismissal rates and disable or retune any comment category that exceeds roughly a 10% dismissal rate; that threshold is a heuristic, not a measured cutoff. [HEURISTIC]

### RQ2: Comparative practice across review tools

| Tool | Test/doc-sync-relevant capability | What transfers |
|---|---|---|
| **Agentic code-review action** (e.g. Claude Code Action) | `prompt` + args (system-prompt override, JSON-schema structured output), sticky comments, inline-comment classification, progress tracking; supports multiple LLM API backends. [VERIFIABLE][^3] | This is your engine; everything below is a pattern to port into the prompt. |
| **CodeRabbit** | Pre-merge checks with `off`/`warning`/`error` modes; built-in docstring coverage; natural-language custom checks. Custom checks run in a read-only sandbox over the PR diff and linked issues, so the LLM does not execute your suite. [VERIFIABLE][^4] | The warning-to-error escalation ladder, the natural-language pass/fail pattern, and the honest limit that the LLM does not run tests. |
| **GitHub Copilot Code Review** | Advisory-only ("Comment" review, non-blocking); intentional silence in about 29% of reviews, averaging about 5.1 comments when it does speak. [VERIFIABLE][^2][^10] | Silence-as-design and biasing toward high-signal comments over volume. |
| **A public production reviewer** (OpenCode-based, internal to one org) | Specialized parallel reviewers including a Documentation Reviewer and an "AI-instructions" (AGENTS.md) reviewer that flags stale AI instructions; a coordinator that deduplicates findings and judges severity; risk tiers; a `break glass` override. [VERIFIABLE][^1] | The materiality-tiered documentation and AI-instructions reviewer is nearly the exact problem, plus the "What NOT to Flag" structure. |
| **Qodo / PR-Agent** (OSS) | Multi-agent, including a test-coverage agent that generates missing tests; rules need ongoing config ownership or they drift into noise. [HEURISTIC] | Proposing a test skeleton alongside a finding (reduces "AI toil"), and the warning that rules need an owner. |
| **Sourcery** | Python-focused; one-click test generation; adapts to dismissals. [HEURISTIC] | Dismissal-learning as a durability mechanism. |
| **Greptile / Diamond** | Full-repo indexing (higher recall and noise) versus low-noise designs; neither is test-coverage-specialized. [HEURISTIC] | The recall-versus-noise trade-off framing; not a direct fit. |
| **Danger JS/Ruby** | Deterministic `warn`/`fail` when modified files show source changed without a matching test path, with `trivial` / "skip new tests" escape hatches read from the PR body. [HEURISTIC] | The deterministic floor (RQ4): do the presence check here, not in the LLM. |
| **Codecov** | `codecov/patch` measures only the lines adjusted in the PR; `informational: true` allows a reporting-only rollout; blocking requires the check in branch protection. [VERIFIABLE][^5] | Patch coverage is the deterministic answer to whether the new lines are tested, more reliable than an LLM guess. |

**Transferable pattern set for a review-action prompt:** (a) an explicit "What NOT to flag" list; (b) a fixed severity vocabulary plus structured output; (c) sticky and clustered comments; (d) advisory framing plus an escape hatch; (e) materiality tiering so trivial diffs get a light touch; (f) a self-verification pass. [VERIFIABLE][^1][^2][^3]

### RQ3: Agent context-file best practices for durable PROCESS rules

**Context files are easy to get wrong and are often net-negative.** The ETH Zurich study (arXiv 2602.11988, v2 Jun 2026) evaluated multiple frontier LLMs and coding agents on a benchmark of developer-committed context files and found that providing context files did not generally improve task success rates while raising inference cost by over 20% on average, across both ISSUE-REF and developer-committed files. Instructions in the files are well followed, but repository overviews (popular and provider-recommended) are not helpful. Its conclusion: context files are useful for specifying non-standard practices, and any performance claim should be evaluated before deployment. [VERIFIABLE][^6]

**What makes a rule actually followed** (converging across the ETH study, Claude's memory docs, and the AGENTS.md spec):
- **Imperative, specific, and verifiable.** The Claude Code docs give "Run `npm test` before committing" as better than "Test your changes", state that Claude treats the context file as context and not enforced configuration, and note that more specific and concise instructions are followed more consistently. [VERIFIABLE][^7]
- **Short.** The docs target under 200 lines per context file and note that longer files consume more context and reduce adherence, recommending path-scoped rules (under `.claude/rules/`) so a rule loads only when matching files are touched. [VERIFIABLE][^7]
- **Path-scoped.** Scope a rule to the directories it applies to, matching the path-glob mechanism, so it loads only when relevant. [VERIFIABLE][^7]
- **Single source of truth via import.** A bare mention wrapped in backticks stays literal text; an `@path` import outside backticks expands the file into context at launch, recursing to a maximum depth of four hops. To share with other agents, have the context file import a shared AGENTS.md. [VERIFIABLE][^7]
- **Advisory is not enforcement.** The docs are explicit that to block an action regardless of what the agent decides you use a PreToolUse hook instead; context-file prose cannot enforce anything. [VERIFIABLE][^7]

**Strong canonical example:** the official AGENTS.md sample includes the line "Add or update tests for the code you change, even if nobody asked" — a one-line imperative process rule in exactly the register to emulate. [VERIFIABLE][^9]

**Context-file rot is real and named.** The public production system built an AI-instructions reviewer whose job is to flag stale AI instructions after high-materiality changes, because these files rot quickly [VERIFIABLE][^1]. The general lesson for a context file: avoid generic filler and pair any tool name with a runnable command. [HEURISTIC]

### RQ4: Non-LLM enforcement that complements or beats the bot

**For "did a source change ship without a test/doc change?", a deterministic rule beats an LLM.** It is a file-pattern predicate: cheap, 100% reproducible, no hallucination, no prompt-injection surface, no token cost. [INFERENCE]

- **Danger JS/Ruby**: `warn`/`fail` when modified files show source changed without a matching test-path change, with `trivial`/`skip new tests` escape hatches read from the PR body. This is the right home for the *presence* check. [HEURISTIC]
- **Codecov `patch` gate**: answers "are the *new lines* covered?", strictly better than an LLM guess. Best practice: strict patch gate + lenient project gate; roll out with `informational: true` first, then require the check in branch protection. Note fork-PR token limitations. [VERIFIABLE][^5]
- **PR-template checkboxes**: weak alone (honor-system, trivially gamed). Strengthen by (a) demanding *evidence* not a tick ("link to the updated doc / test file"), (b) removing any box CI already verifies, and (c) optionally enforcing with a checklist-enforcement Action. Do not rely on them as the primary mechanism. [HEURISTIC]
- **CODEOWNERS**: routes the human judgment — require a docs/test owner on paths that historically drift. [INFERENCE]

**Division of labor:** the deterministic layer answers *presence and coverage of changed lines*; the LLM answers *materiality and adequacy* — "this looks like a behavior change, and the existing scenario doesn't touch the new path" — which no regex can judge. This is the belt-and-suspenders split. [INFERENCE]

### RQ5: Durability and trust

**Why these mechanisms get disabled:** noise/alert fatigue (dominant cause), "AI toil" (findings without fixes create research tasks), context-switching across siloed dashboards, and decision fatigue from an unfiltered stream where real and hallucinated findings get the same dismiss-click. Once trust erodes, teams mute the bot entirely and lose the legitimate findings too. [VERIFIABLE][^8]

**What keeps them trusted** (all from systems in production):
1. **Signal over volume.** ~1.2 findings/review by design in one system; Copilot silent ~29% of the time. Bias toward saying nothing. [VERIFIABLE][^1][^10]
2. **Advisory first, escalate later.** CodeRabbit's `warning`→`error` ladder; Copilot advisory-only. Don't block on day one. [VERIFIABLE][^1][^2][^4]
3. **Escape hatch.** A tracked `break glass` override (0.6% usage in one system) prevents the bot from becoming a hostage situation during hotfixes. [VERIFIABLE][^1]
4. **False-positive budget + measurement.** Track dismissal/thumbs-down per comment category and disable or reconfigure any category over roughly a 10% dismissal rate; that threshold is a practitioner heuristic, not a measured cutoff. [VERIFIABLE][^8] [HEURISTIC]
5. **Ownership.** Documented failure mode: rules go stale/noisy without an owner. Assign one. [HEURISTIC]
6. **Comment hygiene.** One sticky comment instead of per-run comment spam that floods notifications. [VERIFIABLE][^3]

### RQ6: Concrete recommendation

**Design principle: three layers, one checklist.** A deterministic layer (Danger presence check + Codecov patch gate), an advisory LLM layer (a review action posting a sticky comment at warning/suggestion severity), and a single canonical checklist that all three reference — imported into the context file, cited by the review prompt, optionally mirrored in the PR template. A human decides in all cases.

**Do NOT make the bot blocking, and do NOT expand the context file.** Both are evidence-backed anti-moves: blocking an advisory-quality LLM judgment on test/doc sync will generate false blocks and get the whole bot removed (RQ5); a longer context file *reduces* adherence to the rule you actually care about (RQ3). [VERIFIABLE][^1][^2][^6][^7]

**Part A — the canonical checklist (author once, reference everywhere).** Keep it in one procedure doc. Make it tight, imperative, path-scoped:

```markdown
## Change-sync checklist
When a change alters BEHAVIOR, the SAME PR must:
- [ ] Update or add the end-to-end scenario covering the changed behavior.
- [ ] Update or add unit/integration tests asserting the new/changed path.
- [ ] Wire any NEW test surface into the CI workflow.
- [ ] Update the docs describing the behavior.
Exempt (no update needed): pure refactors with no behavior change, typo/comment-only edits, generated files.
Escape hatch: if genuinely N/A, write `no-behavior-change` or `docs-follow-up: <link>` in the PR body.
```

**Part B — context-file changes (subtractive, then one import).**
1. Replace the current inline test/doc paragraph with a single imperative line + import so bot and human agents share the same source:
   ```markdown
   ## Keeping tests & docs in sync
   When you change behavior, follow the change-sync checklist in the SAME PR.
   @docs/procedures/change-sync-checklist.md
   ```
   The import guarantees the checklist is loaded into any agent's context; a bare mention would not be. [VERIFIABLE][^7]
2. Trim elsewhere to stay under ~200 lines and keep this rule salient — delete linter-duplicating style rules and repository overviews (ETH: overviews don't help). If the file is large, move this into a path-scoped rule (`.claude/rules/...`) so it loads only when relevant. [VERIFIABLE][^6][^7]
3. If you maintain AGENTS.md for other tools, put the rule there and have the context file import it — one source, all tools. [VERIFIABLE][^7]

   > Mechanism note: an `@import` pulls the *entire* linked file into every agent's launch context. If the goal is a terse in-context rule plus a pointer to detail, a plain relative link is the better context-budget tradeoff. The finding (import loads it, a bare mention does not) is sound; whether to import or link is a per-repo budget decision.

**Part C — the review-bot prompt additions.** Add a dedicated, tightly scoped "Test & Doc Sync" section to the review prompt (via a system-prompt append or the slash command you invoke), placed *after* the existing correctness/security/ops sections:

```
## Test & Doc Sync review (ADVISORY: never block, never mark critical)

Read the canonical change-sync checklist and treat it as the rubric.

### What to flag (severity: warning or suggestion only)
- A BEHAVIOR change with no corresponding change to its end-to-end scenario,
  unit/integration tests, OR docs in this same PR.
- A NEW test file/surface that is not wired into the CI workflow.
- An existing test file that was touched but does NOT assert the new/changed path
  (verify by reading the test; do not assume presence == coverage).

For each finding output: {path, behavior_changed, which_artifact_is_missing
(e2e|unit|integration|docs|ci-wiring), one-line reason, suggested next step}.

### What NOT to flag
- Pure refactors, renames, or formatting with no behavior change.
- PRs that already updated the relevant test/doc (say nothing).
- Private/internal helpers, generated files, lockfiles, vendored code.
- Typo/comment-only or docs-only PRs.
- PR body contains `no-behavior-change` or `docs-follow-up:`.
```

Enable sticky comments and inline-comment classification to keep the timeline clean, and gate any downstream automation on structured (JSON-schema) output rather than parsing prose. [VERIFIABLE][^3]

**Rollout order (durability-first).** (1) Ship Danger + Codecov `informational: true` — a deterministic, silent floor. (2) Ship the LLM section advisory-only with sticky comments. (3) Instrument dismissal rate per finding category for ~4 weeks; retune or disable any category over ~10% dismissal. (4) Only once patch coverage is trusted, make the Codecov check required in branch protection. Keep the LLM layer advisory indefinitely. [INFERENCE]

## Gaps and Open Questions

- **False-positive-driven muting** is supported by surveys and community reports, not a controlled study. The ~10% dismissal threshold is a practitioner heuristic, not an empirically derived cutoff. [HEURISTIC]
- **No public head-to-head benchmark** isolates test/doc-sync flagging accuracy across bots; the recall-vs-noise framing is directional, not measured.
- **The ETH ">20% inference cost" and "overviews don't help" findings** are from a single (recent, well-scoped) study; independent replication was not located. Validate context-file effects on your own repo before assuming transfer.
- **Copilot's exact silence/comment percentages** reflect vendor telemetry, not an independent audit, and may shift with model updates.

## Sources

[^1]: Cloudflare. (2026). [Orchestrating AI code review at scale](https://blog.cloudflare.com/ai-code-review/). The Cloudflare Blog. (Production AI review system: "What NOT to Flag" prompt sections, ~1.2 findings per review, break-glass override used in 0.6% of merge requests, a Documentation reviewer and an AI-instructions reviewer, sub-reviewers that grep and read source and return structured XML, a coordinator that deduplicates and judges severity.)
[^2]: GitHub. (2025). [Using GitHub Copilot code review](https://docs.github.com/en/copilot/using-github-copilot/code-review/using-copilot-code-review). GitHub Docs. (Copilot always leaves a non-blocking "Comment" review, so its reviews do not count toward required approvals and do not block merging.)
[^3]: Anthropic. (2026). [claude-code-action: usage and configuration](https://github.com/anthropics/claude-code-action). GitHub. (`prompt` and args including system-prompt override and JSON-schema structured output, sticky comments, inline-comment classification, progress tracking; multiple LLM API backends.)
[^4]: CodeRabbit. (2025). [Pre-merge checks](https://docs.coderabbit.ai/pr-reviews/pre-merge-checks). CodeRabbit Docs. (Enforcement modes `off`/`warning`/`error` with `warning` the default that does not block; built-in docstring coverage; natural-language custom checks that run in a read-only sandbox over the PR diff and linked issues, and cannot run your test suite.)
[^5]: Codecov. (2025). [Status checks](https://docs.codecov.com/docs/commit-status). Codecov Docs. (`codecov/patch` measures only lines adjusted in the pull request; `informational` mode defaults to `false` and, when `true`, passes regardless of coverage for a reporting-only rollout.)
[^6]: Gloaguen, T., Mündler, N., Müller, M., Raychev, V., & Vechev, M. (2026). [Evaluating AGENTS.md: Are Repository-Level Context Files Helpful for Coding Agents?](https://arxiv.org/abs/2602.11988) (arXiv:2602.11988v2). (Providing context files does not generally improve task success rates while raising inference cost by over 20% on average; repository overviews are not helpful, but instructions in the files are well followed.)
[^7]: Anthropic. (2026). [Manage Claude's memory](https://docs.anthropic.com/en/docs/claude-code/memory). Claude Code Docs. ("Run `npm test` before committing" beats "Test your changes"; `@path` imports expand into context at launch to a maximum depth of four hops and skip Markdown code spans; the context file is context, not enforced configuration, so use a PreToolUse hook to block; more specific and concise instructions are followed more consistently; `.claude/rules/` holds path-specific rules.)
[^8]: Stack Overflow. (2025). [2025 Developer Survey](https://survey.stackoverflow.co/2025). Stack Overflow. (More developers actively distrust the accuracy of AI tools, 46%, than trust it, 33%.)
[^9]: OpenAI and contributors. (2025). [AGENTS.md](https://agents.md). (The sample file includes the one-line imperative "Add or update tests for the code you change, even if nobody asked.")
[^10]: GitHub. (2026). [60 million Copilot code reviews and counting](https://github.blog/ai-and-ml/github-copilot/60-million-copilot-code-reviews-and-counting/). The GitHub Blog. (In 71% of reviews Copilot surfaces actionable feedback and in the remaining 29% it says nothing, now averaging about 5.1 comments per review.)

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
