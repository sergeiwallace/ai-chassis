---
title: "Review Depth for AI-Delegated Code Diffs"
category: research
tags: [research, code-review, coding-agents, verification, risk-management]
status: complete
source: "codex-gpt-5-2026-07-26"
template_version: "research-1.2.0"
---

# Review Depth for AI-Delegated Code Diffs

**Status:** complete

**Created:** 2026-07-26

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. The evidence boundary: complete coverage is not uniform depth](#1-the-evidence-boundary-complete-coverage-is-not-uniform-depth)
  - [Definitions](#definitions)
  - [What the classic review-size evidence actually says](#what-the-classic-review-size-evidence-actually-says)
  - [Does the human fatigue curve transfer to an AI reviewer?](#does-the-human-fatigue-curve-transfer-to-an-ai-reviewer)
- [2. AI-generated failure modes and control coverage](#2-ai-generated-failure-modes-and-control-coverage)
  - [What full reading catches](#what-full-reading-catches)
  - [What neither reading strategy catches reliably](#what-neither-reading-strategy-catches-reliably)
  - [Tests are evidence, not an oracle by default](#tests-are-evidence-not-an-oracle-by-default)
- [3. The real cost: context consumption and the crossover question](#3-the-real-cost-context-consumption-and-the-crossover-question)
  - [No defensible universal crossover number](#no-defensible-universal-crossover-number)
  - [A better context architecture](#a-better-context-architecture)
- [4. Risk-weighted complete coverage](#4-risk-weighted-complete-coverage)
  - [Review sequence](#review-sequence)
  - [Risk triggers](#risk-triggers)
  - [The narrow mechanical exception](#the-narrow-mechanical-exception)
- [5. Automate away judgment](#5-automate-away-judgment)
  - [Required gates](#required-gates)
  - [Oracle pinning and red-then-green proof](#oracle-pinning-and-red-then-green-proof)
  - [What automation still misses](#what-automation-still-misses)
- [6. What the fleet evidence changes](#6-what-the-fleet-evidence-changes)
  - [Incident-by-incident interpretation](#incident-by-incident-interpretation)
  - [Verdict on the proposed prior](#verdict-on-the-proposed-prior)
- [7. Frontier-lab and practitioner practice in 2026](#7-frontier-lab-and-practitioner-practice-in-2026)
  - [Convergence](#convergence)
  - [The skeptical evidence](#the-skeptical-evidence)
- [8. Gaps, blindspots & emergent findings](#8-gaps-blindspots--emergent-findings)
  - [Correlated reviewer error](#correlated-reviewer-error)
  - [The diff is not the blast radius](#the-diff-is-not-the-blast-radius)
  - [Review-volume inflation](#review-volume-inflation)
  - [Unresolved blindspots](#unresolved-blindspots)
  - [Anchor-bias countercheck](#anchor-bias-countercheck)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
  - [Policy text to encode](#policy-text-to-encode)
  - [Operational thresholds](#operational-thresholds)
  - [Cost](#cost)
  - [Explicitly what this rule will miss](#explicitly-what-this-rule-will-miss)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Ambiguous Items from Auto-Remediation (Post-Run Review)](#ambiguous-items-from-auto-remediation-post-run-review)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

<!-- doc:region name="context" kind="immutable" -->

## Context

The organization uses a coordinator-plus-workers model: a driver delegates implementation to Claude and Codex workers, reviews returned diffs, and merges them. The current informal practice often spot-checks the largest diff. This research asks whether context files, procedure docs, and path-scoped rules should instead require full review.

The fleet supplied four observations to weigh:

1. A worker falsely called a failure pre-existing and unrelated; the reviewing session's own earlier work caused it.
2. A worker proposed a plausible change that would have reintroduced a same-day bug fixed across 19 repositories; execution, not prose review, caught it.
3. A fabricated “verbatim” citation survived the research agent's provenance self-check; exact text search against the fetched page caught it.
4. A mechanical sweep across 18 repositories was correct in all 18 and was independently verified afterward.

The suggested—but not assumed—prior was that agent reports are weak on absence and mechanism claims but strong on mechanical execution.

**Primary period:** 2024–2026, with foundational human code-review studies from 2006–2019.
**Source weighting:** 2026 primary, then 2025, then 2024; older review literature is used only where foundational and its transfer limits are explicit.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

The AI-review evidence base is moving quickly and remains thinner than the human-review literature. Current vendor practice and 2025–2026 benchmark work are useful for identifying mechanisms and controls, but most do not provide causal estimates of escaped defects from real production merges. The classic human studies predate coding agents and frontier long-context models; they establish that review size, rate, coverage, and expertise matter for humans, not a universal AI threshold. [INFERENCE]

AI-review-depth crossover: no significant post-2024 peer-reviewed evidence was found that maps semantic diff LOC or tokens to escaped-defect probability for current frontier reviewing agents. One small 2026 preprint reported steep degradation on larger diffs, but its sample and publication status are insufficient for a standing fleet threshold. [NO SOURCE]

## Executive Summary

1. **Do not encode “spot-check the largest diff.”** A sample cannot justify an absence claim about uninspected semantic changes. The merge rule should require **complete accounting of every changed file and full reading of every human-authored semantic hunk**, while allowing different scrutiny depth by risk. [INFERENCE]
2. **Do not encode naïve uniform line-by-line treatment either.** Generated artifacts, trusted mechanical transforms, and repetitive data can be covered by deterministic regeneration, invariants, and explicit sampling. In those cases, the gate—not the sample—carries the assurance. [HEURISTIC]
3. **There is no defensible AI-specific LOC crossover.** Use 400 semantic LOC as a conservative operational split trigger, not a scientific law; above it, split the change or review coherent slices in a fresh context rather than silently lowering coverage. [INFERENCE]
4. **Full diff reading is necessary but insufficient.** It catches visible scope creep, suspicious test edits, duplicated logic, and locally implausible APIs. It does not prove runtime behavior, identify omitted changes, supply missing cross-repository context, or establish that tests are strong. [INFERENCE]
5. **The fleet prior needs rewriting.** The supported axis is not “reports bad, mechanics good”; it is **open-ended claims versus constrained work with an independent oracle**. The 18/18 sweep supports a mechanically verified exception, but is far too small to establish general mechanical reliability. [INFERENCE]

## 1. The evidence boundary: complete coverage is not uniform depth

### Definitions

- **Full semantic review:** inspect and understand every human-authored behavior-changing hunk, every deletion, and every test/configuration change; inspect relevant surrounding code when needed. [HEURISTIC]
- **Complete accounting:** enumerate every changed file and classify it as semantic, test/oracle, configuration/security, generated, vendored, lockfile, data, documentation, or deletion. Nothing remains silently unreviewed. [HEURISTIC]
- **Targeted deep review:** give extra reasoning and context retrieval to high-risk hunks after complete accounting and a baseline pass over all semantic hunks. [HEURISTIC]
- **Spot-checking:** inspect only a sample and infer that the uninspected remainder is acceptable. That is defensible only when another control exhaustively covers the remainder. [INFERENCE]

This distinction resolves the false binary. “Read everything” should mean complete semantic coverage, not equal time per line. “Targeted” should mean variable depth and targeted surrounding context, not unrecorded holes in diff coverage. [INFERENCE]

### What the classic review-size evidence actually says

The Cisco case study's most cited size result was observational: reviews below 200 LOC showed higher discovered-defect density, and the analysis explicitly depended on assuming roughly constant underlying defect density across change sizes. [VERIFIABLE][^1] The study therefore supports “large changes are a review risk” more strongly than it supports a universal 200- or 400-line causal cliff. [INFERENCE]

Kemerer and Paulk found that review rate remained associated with defect-removal effectiveness after controlling for developer ability and initial quality, and that quality declined above the recommended 200 LOC/hour. [VERIFIABLE][^2] This is evidence about deliberate human self-review pace, not about a frontier agent that does not become tired. [INFERENCE]

Modern-review evidence adds a different point: across Qt, VTK, and ITK releases, review coverage was negatively associated with post-release defects in three of four releases, but coverage alone did not guarantee low defects. [VERIFIABLE][^3] This directly weakens “spot-checking is enough,” but also blocks the opposite overclaim that full coverage alone makes a patch safe. [INFERENCE]

Google's public review guide states that reviewers should generally inspect every assigned line while permitting scanning for generated/data material. [VERIFIABLE][^4] Its change-size guide calls 100 lines usually reasonable and 1,000 usually too large. [VERIFIABLE][^5] These are mature-practice rules, not controlled experiments; they are best treated as operational priors. [INFERENCE]

The widely repeated “200–400 LOC in 60–90 minutes yields 70–90% defect discovery” number could not be traced to a peer-reviewed experiment establishing that universal yield. The size/rate guidance traces to the Cisco case and related inspection literature; the 70–90% yield is repeated mainly in vendor guidance. This report does not use that yield as a policy premise. [NO SOURCE]

### Does the human fatigue curve transfer to an AI reviewer?

Not directly. An LLM does not experience human fatigue, but it has different failure curves: attention dilution, positional effects, tool-output accumulation, and finite reasoning/context budgets. A 2025 code-judging benchmark found that even state-of-the-art models struggled to decide whether supplied programs were correct. [VERIFIABLE][^6] That result is closer to review than code-generation pass rates, but it still does not map accuracy to diff size in production. [INFERENCE]

The correct transfer is structural, not biological: larger and wider diffs create more relationships to track, more opportunities for a missed hunk, and more context competition. The human LOC curve is therefore a warning and a useful split trigger, not proof of an AI fatigue threshold. [INFERENCE]

## 2. AI-generated failure modes and control coverage

### What full reading catches

| # | Failure mode | Full semantic reading | Spot-checking | Automation | Residual risk |
|---|---|---|---|---|---|
| 1 | Plausible-but-wrong local logic | Often catches contradictory branches, wrong boundaries, or unjustified complexity; not reliably | Only if sampled | Type checks and tests catch exercised behavior | Hidden edge cases and misleadingly clean implementations |
| 2 | Silent scope creep | Catches visible extra files/features and unrelated cleanup | Misses unsampled files by construction | Path allowlist and changed-file manifest can block it | In-scope edits with out-of-scope semantics |
| 3 | Tests weakened to pass | Catches removed assertions, broader mocks, skipped cases, and changed expected values | Frequently missed if tests are not sampled | Test-diff gate, mutation tests, pinned hidden oracle | Subtle oracle weakening and inadequate original tests |
| 4 | Hallucinated package/API | May catch unfamiliar names; cannot prove existence or version compatibility | Only if sampled | Dependency resolution, compilation, API contract tests, registry allowlist | Dynamically resolved or semantically misused real APIs |
| 5 | Duplicated/reinvented logic | Can catch when surrounding code is inspected | Low recall | Clone/search tooling helps | Semantic duplication with different syntax |
| 6 | Deletion/error suppression | Strong visual signal if all deletions are read | Easy to miss outside sample | Deletion budget, exception/swallowing lint rules | Legitimate-looking loss of behavior |
| 7 | Missing required change | Usually not in the diff, so full reading may not catch it | Same | Requirement-to-test trace and blast-radius queries help | Unknown callers, undocumented requirements |
| 8 | Runtime, concurrency, security, or cross-service defect | Sometimes exposes the mechanism | Usually poor | Dynamic tests, sanitizers, scanners, model checking, staging | Environment-specific and emergent behavior |

The table is a control model, not measured detection probabilities. [HEURISTIC]

AI-specific plausibility is real enough to reject stylistic confidence as evidence. In the 2025 Stack Overflow survey, 66% of respondents who answered the frustration question cited solutions that were almost right, and 45% cited more time debugging AI-generated code. [VERIFIABLE][^8] This is perception data, not an escaped-defect study, but it matches the fleet's “reasoning sounded sound” incident. [INFERENCE]

Hallucinated APIs are not merely anecdotes. A USENIX Security 2025 study generated 576,000 code samples across 16 models and found 440,445 of 2.23 million recommended packages—19.7%—were hallucinated under its prompts and detection method. [VERIFIABLE][^7] A full read can notice a suspicious dependency, but only registry resolution and execution can establish whether it exists and works. [INFERENCE]

### What neither reading strategy catches reliably

Full diff review cannot see code the agent should have changed but did not, production-only state, callers in an unavailable repository, undocumented incident history, or a test suite's missing assertions. Those are absence and system-context problems, not line-reading problems. [INFERENCE]

The April 2026 Claude Code postmortem is a useful negative case: a context-management defect passed multiple human and automated code reviews, unit and end-to-end tests, automated verification, and dogfooding. A stronger reviewer found it when given the additional repositories required for complete context. [VERIFIABLE][^18] This destroys any claim that full diff reading or “all tests green” is sufficient. [INFERENCE]

Functionally correct is not equivalent to safe. A NeurIPS 2025 study demonstrated functionally correct yet vulnerable agent patches that passed all test cases. [VERIFIABLE][^11] Security-sensitive paths therefore need separate security oracles and specialist review even after complete diff reading. [INFERENCE]

### Tests are evidence, not an oracle by default

Tests deserve first-class review because an agent can change both the implementation and the standard by which it is judged. Treat tests as maintained code and ask whether they fail when the implementation is broken. [HEURISTIC]

Review order alone is not a silver bullet. A controlled experiment with 93 developers found that test-first review detected the same proportion of production defects and more test defects, but fewer production maintainability issues. [VERIFIABLE][^12] The actionable lesson is to review the oracle and implementation as distinct artifacts, not to assume one fixed reading order dominates. [INFERENCE]

Intrinsic model self-check is also insufficient. ICLR 2024 work found that LLM reasoning self-correction without external feedback could fail or degrade performance, while separate ICLR work found tool-interactive critique improved performance and emphasized external feedback. [VERIFIABLE][^9][^10] The fleet's fabricated quotation surviving a provenance self-check is therefore consistent with broader evidence: verification must query an external artifact, not merely ask the same model whether it was right. [INFERENCE]

## 3. The real cost: context consumption and the crossover question

### No defensible universal crossover number

Long context capacity is not constant reasoning quality. ACL 2024 experiments held tasks constant while padding inputs and observed reasoning degradation at lengths much shorter than models' technical maxima. [VERIFIABLE][^13] That is strong evidence against “it fits, therefore it can be reviewed equally well,” but it is not code-review-specific and gives no LOC threshold for current models. [INFERENCE]

GitHub's July 2026 Copilot review work supplies the most relevant production mechanism found. Broad repository browsing increased cost and caught fewer issues; a diff-anchored workflow that narrowed before focused reads cut average review cost by roughly 20% without a blocking quality regression. [VERIFIABLE][^14] This supports **targeted context retrieval around a completely covered diff**, not targeted sampling of the diff. [INFERENCE]

No universal crossover size is defensible because raw LOC fails to capture:

- code-token density and language;
- number of files, subsystems, and independent concepts;
- deletions versus additions;
- generated versus semantic content;
- reviewer model, effort, tools, and prior session history;
- required surrounding context and cross-repository dependencies. [INFERENCE]

Any numeric rule should therefore be labeled an operational circuit breaker and calibrated against this fleet's own escaped-defect and review-cost data. [HEURISTIC]

### A better context architecture

Do not force the implementation-heavy driver session to ingest a large diff at the end of a long task. Use a fresh review context with: the requirement, changed-file manifest, complete diff in coherent slices, relevant path-scoped rules, test evidence, and only the surrounding code needed to answer specific review questions. [HEURISTIC]

For a large but indivisible change, preserve complete coverage by partitioning the review—not by partitioning assurance:

```text
changed-file inventory
        |
        v
semantic slices -----> generated/mechanical slices
        |                       |
        v                       v
full read + risk depth      deterministic verifier + sample
        |                       |
        +-----------+-----------+
                    v
          cross-slice invariant pass
                    |
                    v
          independent high-risk review
```

The second-order context effect is real: every extra repository dump competes with the diff, requirement, and earlier findings. The mitigation is fresh context, focused retrieval, and persistent review notes—not skipping uninspected semantic hunks. [INFERENCE]

## 4. Risk-weighted complete coverage

### Review sequence

1. **Inventory before interpretation.** Produce a machine-generated list of every changed path, status, additions/deletions, and classification. Reject undeclared paths. [HEURISTIC]
2. **Check intent and blast radius.** Compare the changed-file set to the task, path rules, public interfaces, callers, and expected tests. Ask “what should have changed but did not?” [HEURISTIC]
3. **Read all semantic hunks.** Understand each human-authored behavior change, deletion, test change, and configuration change. Record any excluded generated/mechanical portion and its verifier. [HEURISTIC]
4. **Deepen by risk.** Trace callers, invariants, error paths, concurrency, authorization, migrations, and rollback behavior where triggers fire. [HEURISTIC]
5. **Run independent evidence.** Execute pinned tests, build/type/lint/security checks, red-green proof, and artifact regeneration in a clean environment. [HEURISTIC]
6. **Reconcile the whole.** Re-read the changed-file inventory, test diff, and unresolved findings; verify no later fix invalidated an earlier approval. [HEURISTIC]

This sequence resembles Google's complete-line rule plus GitHub's diff-anchored, narrow-evidence retrieval. Those organizations' practices support the components, but the combined sequence is this report's synthesis. [INFERENCE]

### Risk triggers

Any one of the following overrides a low LOC count and requires deeper or specialist review:

- authentication, authorization, secrets, cryptography, privacy, or untrusted input;
- CI/CD, hooks, agent instructions, repository rules, or security configuration;
- test deletion, assertion weakening, skip/xfail changes, broad mocks, snapshot churn, or coverage exclusions;
- dependencies, lockfiles, install scripts, package names, or network behavior;
- schema/data migrations, destructive operations, rollback paths, or irreversible state;
- concurrency, caching, retries, timeouts, error suppression, or cleanup;
- public APIs, serialization formats, cross-repository contracts, or shared templates;
- large deletions, file moves, generated output without a reproducible generator, or changes outside the declared path set. [HEURISTIC]

GitHub can already enforce parts of this model: rulesets support required status checks and path-specific required reviewers. [VERIFIABLE][^22] The organization should encode risk ownership in repository controls where possible rather than relying on a reviewer's memory. [INFERENCE]

### The narrow mechanical exception

Targeted sampling is defensible only when **all** of these hold:

1. The transformation was declared before execution and has low semantic degrees of freedom.
2. The allowed path set and expected cardinality are machine-checkable.
3. A trusted deterministic tool can regenerate the output, or a verifier checks every transformed instance.
4. Tests, review rules, CI, security configuration, and the verifier itself are outside the worker's writable scope—or receive full independent review.
5. The sample is stratified: first, last, boundary/outlier, one per transformation shape, and every exceptional case.
6. The reviewer records exactly what was sampled and what exhaustive gate covered the remainder.
7. Any mismatch collapses the exception: review the full semantic output or rerun the transform. [HEURISTIC]

Generated files, vendored data, and trusted automated refactors are the natural use cases. A repetitive hand-authored agent edit is not automatically “mechanical”; it earns the exception only through exhaustive verification. [INFERENCE]

The key sentence is: **sampling supplies diagnosis; the deterministic verifier supplies coverage.** [INFERENCE]

## 5. Automate away judgment

### Required gates

| # | Gate | What it catches | What it does not establish |
|---|---|---|---|
| 1 | Changed-path allowlist plus expected file count | Visible blast-radius expansion, missing/extra generated targets | Whether in-scope semantics are correct |
| 2 | Clean-base build, typecheck, lint, format | Syntax, types, known static rules | Requirements or runtime behavior |
| 3 | Pinned fail-to-pass and pass-to-pass tests | Regression reproduction and preservation of known behavior | Unknown cases or weak assertions |
| 4 | Test-integrity diff policy | Deleted/weakened assertions, skips, coverage exclusions, mock expansion | Subtle semantic weakening |
| 5 | Diff coverage plus mutation testing on critical logic | Unexecuted changed lines and tests that do not detect mutations | Complete specifications |
| 6 | Dependency resolution, lockfile consistency, license/CVE/secret scans | Hallucinated packages and known supply-chain/security issues | Safe semantic use of a real dependency |
| 7 | Generator/refactor replay and zero-diff check | Drift in generated/mechanical output | Correctness of the generator |
| 8 | Schema/migration dry run plus rollback/replay | Non-idempotence and obvious destructive paths | Production data distributions |
| 9 | Cross-repository contract/invariant tests | Shared-template and interface drift | Undocumented consumers |
| 10 | Exact provenance check (`grep -F`, digest, or parser) | Fabricated “verbatim” text and wrong source artifacts | Whether a true quote supports the claim |
| 11 | Required review/status rules with no self-bypass | Missing owners and skipped checks | Reviewer quality |

Anything deterministic enough to be a gate should stop consuming reviewer judgment. This does not make reading optional; it reserves reasoning for controls that cannot be reduced to equality, parsing, compilation, or execution. [HEURISTIC]

### Oracle pinning and red-then-green proof

The worker must not be the sole author of both the implementation and its acceptance oracle. Keep acceptance tests or invariant checks outside its writable scope where feasible, or apply them after the worker patch from a trusted source. SWE-bench's harness illustrates the separation by applying a test patch during evaluation before running tests. [VERIFIABLE][^23] The benchmark has had its own oracle weaknesses, so the architecture is an example, not proof of sufficiency. [INFERENCE]

A credible bug-fix gate records:

1. the reproduction fails on the clean base;
2. the same reproduction passes with the candidate patch;
3. preservation tests pass on the candidate;
4. the candidate did not alter the pinned reproduction or gate;
5. the result is rerun from a clean checkout/environment. [HEURISTIC]

For a same-day regression across repositories, pin the regression fixture centrally and run it against every target. Do not accept a worker's narrative that a failure is pre-existing; compare the clean base, candidate, and relevant earlier change directly. [INFERENCE]

### What automation still misses

Deterministic gates encode known requirements. They will not find a missing requirement, a wrong product decision, an undocumented operational constraint, or a novel exploit outside the scanner/test model. The gate suite itself is production code and must have ownership, negative controls, and review. [HEURISTIC]

Anthropic's automated security review documentation explicitly says automated review should complement rather than replace manual security practice. [VERIFIABLE][^17] GitHub similarly layers agent self-review, code/secret/dependency scanning, and a subsequent human PR review. [VERIFIABLE][^16] Current serious practice is layered, not “AI reviewer or human reviewer.” [INFERENCE]

## 6. What the fleet evidence changes

### Incident-by-incident interpretation

| Fleet observation | What it supports | What it does not support |
|---|---|---|
| False “pre-existing/unrelated” claim | Causal attribution and absence claims need timeline/diff execution, not a report | All agent explanations are false |
| Plausible proposal would reintroduce a 19-repo bug | Dynamic regression oracles dominate persuasive mechanism prose | Reading never catches regressions |
| Fabricated quote survived provenance self-check | Same-agent self-attestation is not independent; literal source checks work | Every citation needs human manual checking if exact checks can automate it |
| 18/18 sweep correct under independent verification | Constrained repeated transforms can be efficient with exhaustive verification | Mechanical work has a negligible general error rate |

These are four operational observations, not a randomized comparison. Their value is causal detail: each identifies a control that did or did not catch a concrete failure. [INFERENCE]

The 18/18 result is weaker than it feels. With zero observed failures in 18 independent trials, the one-sided 95% upper bound on a constant failure probability is:

\[
p_{upper}=1-0.05^{1/18}\approx 15.3\%.
\]

The trials may also be correlated because they shared a transformation, model, prompt, and verifier. The sweep is positive evidence for that exact controlled workflow, not a fleet-wide reliability estimate. [INFERENCE]

### Verdict on the proposed prior

**Destroyed in its categorical form.** No comparative study was found that establishes “absence and mechanism claims are unreliable while mechanical execution is reliable” across coding agents. [NO SOURCE]

**Preserved as a narrower operational heuristic.** Absence and mechanism claims require completeness or causal reasoning over evidence that may not be present; unconstrained self-check provides no independent signal. Mechanical execution can be reliable when the task has low degrees of freedom and every output is checked by an independent deterministic oracle. [INFERENCE]

The better prior is:

> Trust should scale with **independent verifiability and constrained degrees of freedom**, not with how mechanical the work sounds or how confident the report reads. [INFERENCE]

This formulation also explains the disconfirming 18-repository sweep instead of treating it as an exception to a story about unreliable agents.

## 7. Frontier-lab and practitioner practice in 2026

### Convergence

OpenAI's Codex launch says users must manually review and validate all agent-generated code before integration and execution; it also describes repetitive, well-scoped tasks such as refactoring, renaming, and writing tests as common internal uses. [VERIFIABLE][^15] That is a broad vendor safety rule, not evidence that every line deserves equal depth. [INFERENCE]

GitHub's 2026 coding-agent workflow has the agent self-review, run code/secret/dependency checks, and then request human review. [VERIFIABLE][^16] Its code-review product separately introduced higher review effort for complex, security-sensitive, and cross-service changes while keeping a cheaper tier for straightforward work. [VERIFIABLE][^19] This is direct evidence that scaled practice is converging on complete automated first passes plus risk-scaled reasoning, not random spot-checking. [INFERENCE]

Anthropic's internal postmortem adds the hard limit: multiple human and automated reviews plus extensive tests still missed a cross-context bug, and added repository context changed whether the AI reviewer found it. [VERIFIABLE][^18] Anthropic's usage analysis also found that domain expertise improved success and recovery from errors even as the agent made most implementation decisions. [VERIFIABLE][^21] Reviewer selection and context are controls, not incidental details. [INFERENCE]

Google's public human-review guidance remains unusually explicit: inspect every assigned human-written line, allow scanning for generated/data material, and bring in qualified reviewers for specialist concerns. [VERIFIABLE][^4] The organization can adopt that semantic distinction without pretending an AI reviewer shares human fatigue.

### The skeptical evidence

METR's randomized trial of 16 experienced open-source developers completing 246 real tasks found early-2025 AI tools increased completion time by 19%, even though developers believed the tools had made them faster. [VERIFIABLE][^20] This does not measure review accuracy and newer models may differ, but it is strong evidence that subjective speed impressions are not a safe policy metric. [INFERENCE]

The 2025 Stack Overflow survey's widespread “almost right” frustration is compatible with, rather than canceled by, perceived productivity gains elsewhere in the same survey. [INFERENCE] The organization should therefore measure review escapes, revert rate, oracle failures, and review cost—not accepted diffs or perceived speed alone. [INFERENCE]

AI-assisted code review itself is not a solved reviewer. Code-judging research found state-of-the-art models still struggled, and long-input research found degradation below advertised context limits. [VERIFIABLE][^6][^13] An AI second pass is valuable, but a same-model “LGTM” is not an independent proof. [INFERENCE]

## 8. Gaps, blindspots & emergent findings

### Correlated reviewer error

Using the same model family, prompt, context, and artifacts for author and reviewer can preserve shared blindspots. NeurIPS 2024 experiments found self-preference when an LLM evaluated its own generations relative to human judgments. [VERIFIABLE][^24] The study evaluated text rather than code, so it does not prove same-model code-review bias; it does justify treating model diversity and fresh context as risk reducers, not guarantees. [INFERENCE]

For high-risk diffs, prefer at least one independence dimension: different model family, fresh context, different prompt/checklist, domain-owner review, or deterministic oracle. [HEURISTIC]

### The diff is not the blast radius

A perfect read of the entire diff can still miss:

- absent changes;
- unchanged callers whose assumptions were invalidated;
- cross-repository templates and consumers;
- base-branch changes after review;
- generated artifacts not refreshed;
- operational state and rollout interactions. [INFERENCE]

This is why the correct unit of assurance is **change intent + diff + affected system + execution evidence**, not the diff alone. [INFERENCE]

### Review-volume inflation

Agent throughput can make the review queue the scarce resource. If a standing full-review rule is added without smaller tasks, deterministic gates, and review partitioning, reviewers may comply nominally while skimming. That recreates spot-checking under a more reassuring label. [INFERENCE]

The control is to cap semantic change size at delegation time, not merely complain at merge time. Ask workers for stacked, behavior-preserving increments, separate refactors from features, and keep generated/mechanical output isolated. [HEURISTIC]

### Unresolved blindspots

1. **AI-specific diff-size calibration:** no peer-reviewed 2025–2026 study found that provides a robust LOC/token crossover for current frontier reviewer agents. [NO SOURCE]
2. **Escaped-defect comparison:** no credible causal field study found comparing full AI diff reading with risk-weighted spot-checking on the same production changes. [NO SOURCE]
3. **Absence-versus-mechanism-versus-mechanics reliability:** no direct comparative calibration found; the fleet evidence is too small to generalize. [NO SOURCE]
4. **Context-history effect:** long-context degradation is established, but the marginal harm of giving a reviewer the builder's full session history versus a fresh review packet is not quantified. [NO SOURCE]
5. **Adversarial workers:** this report primarily models benign error and optimization toward visible tests. A malicious or compromised agent can target the gates themselves, requiring stronger isolation, signing, and provenance. [HEURISTIC]
6. **Reviewer-skill substitution:** frontier vendors report persistent value from domain expertise, but no evidence found that establishes when a high-capability general reviewer can replace a path owner. [NO SOURCE]

### Anchor-bias countercheck

The prompt offered two anchors: a full-review standing rule and a prior favoring mechanical execution. The evidence changed both. The recommendation is not uniform full reading; it is complete semantic coverage with mechanically verified exceptions and risk-weighted depth. The proposed reliability prior was rejected as a category claim and replaced with a verifiability/degrees-of-freedom model. Contrarian evidence—the 18/18 sweep, focused context retrieval reducing cost, and the failure of multiple full/automated reviews in Anthropic's postmortem—is retained rather than forced into a pro-full-review narrative. [INFERENCE]

## Comparison

| # | Criterion | Uniform full reading | Largest-diff spot-check | Complete coverage, variable depth |
|---|---|---|---|---|
| 1 | Coverage of visible semantic changes | High if genuinely completed | Unknown outside sample | High |
| 2 | Context cost | Highest | Lowest | Moderate and controllable |
| 3 | Generated/mechanical changes | Wastes attention | Cheap but weak without a gate | Regeneration/verifier carries coverage |
| 4 | Scope creep | Strong | Weak | Strong via inventory + full semantic pass |
| 5 | Test weakening | Strong if tests read carefully | Weak unless targeted | Strong via test classification + gate |
| 6 | Cross-system/absence failures | Still weak | Weakest | Best of the three only when paired with blast-radius analysis |
| 7 | Large indivisible changes | Context-degradation risk | Silent assurance holes | Partitioned review with cross-slice reconciliation |
| 8 | Ease of encoding | Simple slogan, hard to perform honestly | Simple but unsafe | More procedure, auditable |
| 9 | Defensible merge rule | Too blunt | No, except verified mechanical output | Yes |

The comparison is a synthesis of failure mechanisms and controls, not a measured head-to-head trial. [INFERENCE]

## Recommendation

### Policy text to encode

> **AI-delegated diff review rule:** Before merge, the reviewing agent must account for every changed file and read every human-authored semantic hunk, deletion, test change, and configuration change. Review depth must increase for risk-triggered paths and behaviors. Generated, vendored, or truly mechanical portions may be sampled only when a deterministic, independent verifier exhaustively covers the omitted remainder and the review records both the sample and the verifier. A sampled diff alone may never support an “all clear” or “no other changes” claim. If complete semantic review does not fit a fresh review context, split the change or partition the review; do not silently reduce coverage. [HEURISTIC]

Also encode:

- Worker summaries, causal explanations, “pre-existing” claims, test results, and provenance statements are leads, not evidence, until checked against artifacts or execution. [INFERENCE]
- A reviewer must state its coverage: files/hunks reviewed, portions excluded, gates run, failures observed, and residual risks. [HEURISTIC]
- The authoring agent cannot approve its own high-risk change; self-review is a pre-filter. [HEURISTIC]
- Path-scoped rules should name owners and mandatory checks for security, CI, tests/oracles, migrations, shared templates, and agent instructions. [HEURISTIC]

### Operational thresholds

These thresholds are deliberately conservative policy heuristics derived from foundational human evidence and mature practice; they are not presented as AI defect-detection constants. [INFERENCE]

| Semantic change size | Required treatment |
|---|---|
| **0–400 semantic LOC and ≤10 files** | One fresh review context may perform complete semantic review. Risk triggers still require deep/specialist review. |
| **401–1,000 semantic LOC or 11–20 files** | Split before merge where possible. If indivisible, use coherent review slices in a fresh context, persistent coverage notes, cross-slice reconciliation, and a second pass on risk-triggered areas. |
| **>1,000 semantic LOC or >20 files** | Do not authorize merge from a single reviewer pass. Split the change, or document why indivisible and require independent reviewers by subsystem plus deterministic gates. |
| **Generated/mechanical bulk** | Exclude from semantic LOC only when the narrow mechanical exception is satisfied. Otherwise count it normally. |

400 LOC is a **split trigger**, not permission to skim line 401 onward. File count and risk override LOC because a 20-line authorization change can be more consequential than a 2,000-line generated snapshot. [INFERENCE]

Run the policy for 60–90 days and record:

- semantic LOC/files per change;
- review tokens/time and number of contexts;
- percent of diff covered semantically versus mechanically;
- findings by source: reading, gate, execution, specialist;
- post-merge reverts, regressions, and escaped defects;
- false “pre-existing,” “no other impact,” and “all tests pass” claims;
- exception frequency and verifier failures. [HEURISTIC]

Recalibrate thresholds from fleet evidence. If no coverage artifact exists, treat the review as incomplete regardless of the final prose. [HEURISTIC]

### Cost

The cost is additional review compute, more task splitting, and occasional second reviewers. The largest savings come from moving style, path, generation, dependency, and oracle-integrity checks into gates and using fresh review contexts instead of carrying the implementation transcript. [INFERENCE]

The rule will slow large, entangled changes. That is partly intended: generation made authoring cheap without making ownership, rollback, or incident response cheap. The policy should optimize cost per safely merged semantic change, not approvals per hour. [INFERENCE]

### Explicitly what this rule will miss

Even when followed perfectly, the rule can miss:

1. requirements and affected code absent from the diff;
2. weak, incomplete, flaky, or compromised oracles;
3. production-only timing, state, load, data, and cross-service failures;
4. vulnerabilities outside scanner/checklist knowledge;
5. shared blindspots between builder, reviewer, and test generator;
6. malicious changes designed to evade the known gates;
7. base-branch or dependency drift after review;
8. maintainability debt that is locally reasonable but accumulates across many small changes. [INFERENCE]

These residuals require system-level tests, staged rollout, monitoring, ownership, incident feedback, and periodic audits. Full diff reading is one layer, not a warranty. [HEURISTIC]

## Open Questions

1. What are this fleet's actual defect yield and context cost by semantic LOC, file count, model, and risk class?
2. Can the harness emit a machine-verifiable review coverage artifact at file/hunk granularity?
3. Which acceptance tests and cross-repository invariants can be made unwritable by workers without making normal development impractical?
4. Which path classes require a different-model reviewer, a human owner, or both?
5. Can generated/mechanical transformations expose a canonical manifest and replay command so their exception is automatic and auditable?
6. What merge-base freshness rule ensures approvals and test evidence still apply to the exact merged state?

## Sources

[^1]: Cohen, J., Brown, E., & Teleki, S. (2006). [Best Kept Secrets of Peer Code Review: Code Review at Cisco Systems](https://static0.smartbear.co/support/media/resources/cc/book/code-review-cisco-case-study.pdf). SmartBear Software. Verified accessible (HTTP 200) 2026-07-26. (Foundational observational case study; predates AI-generated code.)
[^2]: Kemerer, C. F., & Paulk, M. C. (2009). [The Impact of Design and Code Reviews on Software Quality: An Empirical Study Based on PSP Data](https://sites.pitt.edu/~ckemerer/PSP_Data.pdf). IEEE Transactions on Software Engineering. Verified accessible (HTTP 200) 2026-07-26. (Foundational individual-review-rate evidence; predates AI-generated code.)
[^3]: McIntosh, S., et al. (2016). [An Empirical Study of the Impact of Modern Code Review Practices on Software Quality](https://rebels.cs.uwaterloo.ca/papers/emse2016_mcintosh.pdf). Empirical Software Engineering. Verified accessible (HTTP 200) 2026-07-26. (Review coverage, participation, expertise, and post-release defects.)
[^4]: Google. (n.d.). [What to look for in a code review](https://google.github.io/eng-practices/review/reviewer/looking-for.html). Google Engineering Practices Documentation. Verified accessible (HTTP 200) 2026-07-26. (Complete-line guidance, generated/data exceptions, tests, and specialist review.)
[^5]: Google. (n.d.). [Small CLs](https://google.github.io/eng-practices/review/developer/small-cls.html). Google Engineering Practices Documentation. Verified accessible (HTTP 200) 2026-07-26. (Operational change-size guidance.)
[^6]: Zhao, Y., et al. (2025). [CodeJudge-Eval: Can Large Language Models be Good Judges in Code Understanding?](https://aclanthology.org/2025.coling-main.7/). Proceedings of COLING 2025. Verified accessible (HTTP 200) 2026-07-26. (Peer-reviewed code-judging benchmark.)
[^7]: Spracklen, J., et al. (2025). [We Have a Package for You! A Comprehensive Analysis of Package Hallucinations by Code Generating LLMs](https://www.usenix.org/system/files/usenixsecurity25-spracklen.pdf). 34th USENIX Security Symposium. Verified accessible (HTTP 200) 2026-07-26. (Peer-reviewed package-hallucination experiment.)
[^8]: Stack Overflow. (2025). [AI: 2025 Developer Survey](https://survey.stackoverflow.co/2025/ai). Stack Overflow Developer Survey. Verified accessible (HTTP 200) 2026-07-26. (Developer-reported AI frustrations; perception data.)
[^9]: Huang, J., et al. (2024). [Large Language Models Cannot Self-Correct Reasoning Yet](https://proceedings.iclr.cc/paper_files/paper/2024/hash/8b4add8b0aa8749d80a34ca5d941c355-Abstract-Conference.html). ICLR 2024. Verified accessible (HTTP 200) 2026-07-26. (Peer-reviewed intrinsic self-correction evidence.)
[^10]: Gou, Z., et al. (2024). [CRITIC: Large Language Models Can Self-Correct with Tool-Interactive Critiquing](https://proceedings.iclr.cc/paper_files/paper/2024/hash/fef126561bbf9d4467dbb8d27334b8fe-Abstract-Conference.html). ICLR 2024. Verified accessible (HTTP 200) 2026-07-26. (Peer-reviewed external-tool feedback evidence.)
[^11]: Peng, Y., et al. (2025). [When “Correct” Is Not Safe: Can We Trust Functionally Correct Patches Generated by Code Agents?](https://neurips.cc/virtual/2025/133169). NeurIPS 2025. Verified accessible (HTTP 200) 2026-07-26. (Peer-reviewed functionally-correct-yet-vulnerable patch evidence.)
[^12]: Spadini, D., et al. (2019). [Test-Driven Code Review: An Empirical Study](https://sback.it/publications/icse2019a.pdf). ICSE 2019. Verified accessible (HTTP 200) 2026-07-26. (Controlled review-order experiment; predates current coding agents.)
[^13]: Levy, M., Jacoby, A., & Goldberg, Y. (2024). [Same Task, More Tokens: The Impact of Input Length on the Reasoning Performance of Large Language Models](https://aclanthology.org/2024.acl-long.818/). ACL 2024. Verified accessible (HTTP 200) 2026-07-26. (Peer-reviewed controlled input-length study.)
[^14]: Klicius, N. (2026). [Better tools made Copilot code review worse. Here's how we actually improved it](https://github.blog/ai-and-ml/github-copilot/better-tools-made-copilot-code-review-worse-heres-how-we-actually-improved-it/). GitHub Engineering Blog. Verified accessible (HTTP 200) 2026-07-26. (Official production review-workflow benchmark report.)
[^15]: OpenAI. (2025). [Introducing Codex](https://openai.com/index/introducing-codex/). OpenAI. Verified accessible (HTTP 200) 2026-07-26. (Official manual-review and internal-use guidance.)
[^16]: Griffiths, A. (2026). [What's new with GitHub Copilot coding agent](https://github.blog/ai-and-ml/github-copilot/whats-new-with-github-copilot-coding-agent/). GitHub Blog. Verified accessible (HTTP 200) 2026-07-26. (Official layered self-review, scanning, and human-review workflow.)
[^17]: Anthropic. (2026). [Automated Security Reviews in Claude Code](https://support.claude.com/en/articles/11932705-automated-security-reviews-in-claude-code). Claude Help Center. Verified accessible (HTTP 200) 2026-07-26. (Official limits and deployment guidance.)
[^18]: Anthropic. (2026). [An update on recent Claude Code quality reports](https://www.anthropic.com/engineering/april-23-postmortem). Anthropic Engineering. Verified accessible (HTTP 200) 2026-07-26. (Official postmortem showing layered-control and missing-context failure.)
[^19]: GitHub. (2026). [Shape Copilot code review around your team](https://github.blog/changelog/2026-06-02-shape-copilot-code-review-around-your-team/). GitHub Changelog. Verified accessible (HTTP 200) 2026-07-26. (Official risk-scaled AI review depth.)
[^20]: Becker, J., Rush, N., Barnes, B., & Rein, D. (2025). [Measuring the Impact of Early-2025 AI on Experienced Open-Source Developer Productivity](https://metr.org/Early_2025_AI_Experienced_OS_Devs_Study-paper.pdf). METR. Verified accessible (HTTP 200) 2026-07-26. (Randomized controlled field trial.)
[^21]: Anthropic. (2026). [Agentic coding and persistent returns to expertise](https://www.anthropic.com/research/claude-code-expertise). Anthropic Research. Verified accessible (HTTP 200) 2026-07-26. (Privacy-preserving analysis of approximately 400,000 Claude Code sessions.)
[^22]: GitHub. (2026). [Available rules for rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets). GitHub Documentation. Verified accessible (HTTP 200) 2026-07-26. (Required checks and path-specific reviewers.)
[^23]: SWE-bench. (2026). [Harness API](https://www.swebench.com/SWE-bench/api/harness/). SWE-bench Documentation. Verified accessible (HTTP 200) 2026-07-26. (Evaluation test-patch application mechanism.)
[^24]: Panickssery, A., Bowman, S. R., & Feng, S. (2024). [LLM Evaluators Recognize and Favor Their Own Generations](https://papers.nips.cc/paper_files/paper/2024/hash/7f1f0218e45f5414c79c0679633e47bc-Abstract-Conference.html). NeurIPS 2024. Verified accessible (HTTP 200) 2026-07-26. (Peer-reviewed self-preference evidence outside code review.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** cx-research ad hoc, no ai-core DB
**Model:** `Codex GPT-5`
**Date:** 2026-07-26

```text
A previous run of this exact research (session `diff-review-policy`) completed the investigation but could not persist it — the sandbox was read-only and the apply_patch was rejected. This run has scoped write access to the target file, which now exists as a canonical research STUB.

Redo the research to the same standard and WRITE IT to:
`docs/research/ai-delegated-diff-review-depth.md` (relative to this worktree). It is currently the canonical STUB with placeholder frontmatter — fill it in completely. `aido validate-doc` must pass, template_version research-1.2.0.

## The question

**When a sub-agent (or a delegated AI coder) returns a diff, should the reviewing agent read the ENTIRE diff every time, or is targeted spot-checking defensible? Under what conditions does each win?**

The asking organization runs a coordinator-plus-workers model: a driver session delegates implementation to sub-agents (Claude Sonnet, Codex tiers), then reviews and merges their work. Today the driver often spot-checks "the largest diff" before merging. The question is whether a standing rule of full review should instead be encoded in context files, procedure docs, and path-scoped rules.

## Real evidence from this fleet — weigh it, don't just cite it

- A sub-agent's "this failure is pre-existing and unrelated" claim was FALSE; the failure was caused by the reviewing session's own earlier work that day.
- A sub-agent proposed a change that would have reintroduced a bug fixed the same day across 19 repos. Its reasoning read as sound; only executing it caught the error.
- A fabricated "verbatim" citation survived a research agent's own provenance self-check, found only by grep-ing the quote against the fetched page.
- Against that: one sweep across 18 repos produced correct work in all 18, independently verified afterward.

The suggested (NOT assumed) prior is that agent reports are unreliable specifically on ABSENCE and MECHANISM claims and reliable on MECHANICAL EXECUTION. Confirm or destroy that with evidence; do not adopt it because the prompt offered it.

## Questions — a FLOOR, not a checklist

1. What does the empirical literature say about review effectiveness vs. diff size (the classic defect-detection-rate-vs-LOC findings)? Do those transfer to an AI reviewer with a large context window and no fatigue, or is the fatigue curve simply the wrong model? Trace the widely-repeated numbers to a primary source or mark them as secondhand.
2. What is specific to reviewing AI-GENERATED code: plausible-but-wrong, silent scope creep, tests weakened to pass, hallucinated APIs, changes outside the stated blast radius. Which does full reading catch that spot-checking misses — and which does NEITHER catch?
3. The real cost of full review here: context-window consumption and the second-order effect of a reviewer whose context is exhausted by diffs. Is there a crossover size past which full reading degrades judgment on the parts that matter? A number, if one is defensible.
4. Targeting heuristics that beat both extremes: risk-weighted review (touched-file criticality, test-file changes, config/security paths, deletions vs additions), diff coverage, semantic diff, mechanical pre-filters that make full reading cheaper rather than optional.
5. What can be AUTOMATED so the judgment call shrinks: oracle pinning so an agent cannot weaken its own tests, blast-radius assertions, required red-then-green proof, deterministic verifiers. Anything a gate can catch should not depend on reading.
6. What frontier labs and serious practitioners actually do as of 2026 for agent-produced diffs at scale — including the skeptics.
7. A concrete decision rule this organization can encode: the rule, its thresholds, its cost, and explicitly what it will MISS.

## Scope note — questions, examples, and named references are a starting point, not a checklist

The questions above are illustrative anchors and a FLOOR, not an exhaustive list to answer only. Reason independently: survey broadly, follow the evidence, expand scope where warranted, and surface factors and failure modes not named here. An output that merely fills in the listed items has NOT met the goal.

## Independent exploration (gaps, blindspots, emergent threads) — required

Surface what this framing misses and pursue each promising thread to a conclusion: adjacent or upstream factors; contrarian or disconfirming evidence, reported even when it challenges the premise; emerging 2025-2026 practice; known failure modes and second-order effects. Report these in a dedicated "Gaps, blindspots & emergent findings" subsection and NAME any blindspot you suspect but cannot resolve. Anchor bias is a known failure mode; counter it deliberately and say where you did.

## Retrieval enforcement (mandatory)

For each load-bearing claim, make at least one live web_search or web_fetch call. Do not return [VERIFIABLE] for any claim without a URL you actually fetched in this session. Do not stop after one round; search alternate framings before tagging [NO SOURCE] — that tag is for genuine evidence absence, not retrieval avoidance.

<grounding_instructions>
You are a principal engineer who has run code review at scale and has personally shipped a bug that passed review. You have strong opinions backed by evidence, you distrust practices justified only by tradition, and you insist on knowing whether a control actually catches the failure it claims to. When you cannot find a source, you say so explicitly.

Temporal scope: Weight sources by recency — 2026 (primary), then 2025, then 2024. Pre-2024 sources are background only unless foundational; the human code-review literature IS foundational here and may be cited from its original period, but say plainly where it predates AI-generated code and where that limits transfer. If post-2024 literature is genuinely sparse for a subtopic, state "[subtopic]: no significant post-2024 developments found" rather than backfilling. Backfilling is a failure mode, not a hedge.

Before generating your final output, execute a Chain-of-Verification (CoVe) to ensure factual fidelity over compliance.

Inside your thought process:
1. Isolate the core facts required.
2. Draft a tentative response.
3. Hostile Cross-Examination: flag any claim where you are citing a source because the prompt implied you should, rather than because you verified it.
4. Strip away any claim that cannot be empirically verified.

When generating your final output, classify every major claim. Write your rationale before appending the tag — writing the tag first causes post-hoc rationalization. Rationale, then evidence check, then tag.

- [VERIFIABLE]: backed by documentation, peer-reviewed research, or official tech blogs (2024-2026). Carry an inline footnote ref: [VERIFIABLE][^N].
- [HEURISTIC]: widely accepted best practice without a specific citation.
- [INFERENCE]: a logical conclusion drawn from context. Give reasoning in-text. Do not fabricate a source. Tier tag only, NO footnote ref.
- [NO SOURCE]: explicitly state when you cannot find verifiable data. Tier tag only, NO footnote ref.

Citation format (mandatory for every externally-sourced claim):
- Inline: append the GFM footnote ref directly after the tier tag. Multiple sources carry ascending separate refs, never grouped, never out of order.
- Footnote definitions live once under ## Sources, APA form, clickable URL or DOI plus an access-verification stamp:
  [^1]: Author. (2026). [Title](https://example.com). Publication. Verified accessible (HTTP 200) 2026-07-26.
- URL-or-DOI ALWAYS; paywalled is fine, link it and stamp the status. Only a truly-irreducible case gets [no online source located] with a one-line justification.
- Integrity: refs contiguous from [^1], every ref has a definition, every definition referenced.
- [INFERENCE] / [NO SOURCE] carry the tier tag with NO footnote ref.

Hard constraint (overrides all formatting preferences): never invent a citation to satisfy a formatting instruction. Accuracy over completeness.

Format diagrams using Mermaid.js or ASCII. Format math using LaTeX. NEVER generate binary images.
</grounding_instructions>

## Provenance ledger (mandatory)

For every [VERIFIABLE] claim add a row to `## Appendix: Provenance Ledger`: claim, source URL you fetched, a VERBATIM quote that literally appears on that page, entailment verdict (SUPPORTED / PLAUSIBLE / UNVERIFIABLE / CONTRADICTED), live status. Only tag [VERIFIABLE] when quote-presence AND entailment=SUPPORTED AND the URL is live. When unsure default to UNVERIFIABLE — never a false SUPPORTED. **Your quotes WILL be spot-checked with `grep -F` against the fetched page; a paraphrase presented as verbatim is treated as a fabricated citation.**

## Do not

- Do not run any git command. Do not commit.
- Do not modify any file other than the target research doc.
- Do not print environment variables, secrets, or shell-profile contents.
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

| # | Claim | Source URL fetched in this session | Verbatim quote present on source | Entailment | Live status |
|---|---|---|---|---|---|
| 1 | The Cisco case found higher discovered-defect density in smaller reviews. | https://static0.smartbear.co/support/media/resources/cc/book/code-review-cisco-case-study.pdf | “Anything below 200 lines produces a relatively high rate of defects” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 2 | The Cisco size analysis assumed constant true defect density. | https://static0.smartbear.co/support/media/resources/cc/book/code-review-cisco-case-study.pdf | “assuming a constant true number of defects per kLOC” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 3 | PSP review quality declined above the recommended 200 LOC/hour rate. | https://sites.pitt.edu/~ckemerer/PSP_Data.pdf | “review quality declines when the review rate exceeds the recommended maximum of 200 LOC/hour” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 4 | Review coverage was negatively associated with post-release defects in three of four studied releases. | https://rebels.cs.uwaterloo.ca/papers/emse2016_mcintosh.pdf | “review coverage is negatively associated with the incidence of post-release defects in three of the four studied releases” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 5 | Google's general rule is to inspect every assigned line. | https://google.github.io/eng-practices/review/reviewer/looking-for.html | “look at every line of code” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 6 | Google's operational guide calls 100 lines usually reasonable and 1,000 usually too large. | https://google.github.io/eng-practices/review/developer/small-cls.html | “100 lines is usually a reasonable size for a CL, and 1000 lines is usually too large” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 7 | State-of-the-art models struggled on the CodeJudge-Eval correctness task. | https://aclanthology.org/2025.coling-main.7/ | “Evaluation of 12 well-known LLMs on CJ-Eval reveals that even state-of-the-art models struggle” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 8 | The USENIX experiment observed 440,445 hallucinated packages out of 2.23 million. | https://www.usenix.org/system/files/usenixsecurity25-spracklen.pdf | “2.23 million packages in response to our prompts, of which 440,445 (19.7%) were determined to be hallucinations” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 9 | The 2025 Stack Overflow survey reports “almost right” as the most common frustration. | https://survey.stackoverflow.co/2025/ai | “AI solutions that are almost right, but not quite,” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 10 | Intrinsic LLM reasoning self-correction can struggle or degrade without external feedback. | https://proceedings.iclr.cc/paper_files/paper/2024/hash/8b4add8b0aa8749d80a34ca5d941c355-Abstract-Conference.html | “LLMs struggle to self-correct their responses without external feedback” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 11 | Tool-interactive critique improved LLM performance in the CRITIC study. | https://proceedings.iclr.cc/paper_files/paper/2024/hash/fef126561bbf9d4467dbb8d27334b8fe-Abstract-Conference.html | “CRITIC consistently enhances the performance of LLMs” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 12 | Functionally correct agent patches can pass tests while remaining vulnerable. | https://neurips.cc/virtual/2025/133169 | “Functionally Correct yet Vulnerable (FCV) patches, which pass all test cases but contain vulnerable code.” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 13 | Test-first review traded more test-defect findings for fewer production maintainability findings. | https://sback.it/publications/icse2019a.pdf | “more in test code, at the expenses of fewer maintainability issues in production code” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 14 | Controlled padding degraded reasoning below models' maximum context lengths. | https://aclanthology.org/2024.acl-long.818/ | “a notable degradation in LLMs' reasoning performance at much shorter input lengths than their technical maximum” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 15 | GitHub's diff-anchored focused workflow reduced average review cost without a blocking quality regression. | https://github.blog/ai-and-ml/github-copilot/better-tools-made-copilot-code-review-worse-heres-how-we-actually-improved-it/ | “roughly 20% lower average review cost, while maintaining the same review quality” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 16 | OpenAI says manual review and validation remain essential before integrating agent code. | https://openai.com/index/introducing-codex/ | “essential for users to manually review and validate all agent-generated code before integration and execution” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 17 | GitHub's workflow retains human pull-request review after agent iteration. | https://github.blog/ai-and-ml/github-copilot/whats-new-with-github-copilot-coding-agent/ | “Review the pull request” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 18 | Anthropic says automated security review complements rather than replaces manual review. | https://support.claude.com/en/articles/11932705-automated-security-reviews-in-claude-code | “they should complement, not replace, your existing security practices and manual code reviews” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 19 | Anthropic's context bug passed multiple review and test layers. | https://www.anthropic.com/engineering/april-23-postmortem | “made it past multiple human and automated code reviews, as well as unit tests, end-to-end tests, automated verification, and dogfooding” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 20 | GitHub routes complex, security-sensitive, and cross-service changes to a deeper review tier. | https://github.blog/changelog/2026-06-02-shape-copilot-code-review-around-your-team/ | “purpose-built for deeper analysis of complex logic, security-sensitive code, and cross-service changes” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 21 | METR's RCT found early-2025 AI increased completion time by 19%. | https://metr.org/Early_2025_AI_Experienced_OS_Devs_Study-paper.pdf | “allowing AI actually increases completion time by 19%” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 22 | Anthropic's usage study found domain expertise improved success and error recovery. | https://www.anthropic.com/research/claude-code-expertise | “domain experts succeed more often, and more easily recover from errors and misunderstandings” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 23 | GitHub rulesets can require status checks and path-specific team reviews. | https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets | “require review or approval from specific teams when a pull request changes certain files or directories” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 24 | SWE-bench's harness applies a separate test patch and runs tests. | https://www.swebench.com/SWE-bench/api/harness/ | “Applies the test patch and runs the tests.” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 25 | LLM evaluators can prefer their own generations relative to human judgments. | https://papers.nips.cc/paper_files/paper/2024/hash/7f1f0218e45f5414c79c0679633e47bc-Abstract-Conference.html | “an LLM evaluator scores its own outputs higher than others’ while human annotators consider them of equal quality” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 26 | Kemerer and Paulk's association controlled for developer ability and initial quality. | https://sites.pitt.edu/~ckemerer/PSP_Data.pdf | “while controlling for developer ability and the initial work product quality” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 27 | McIntosh et al. warn that review coverage alone is insufficient. | https://rebels.cs.uwaterloo.ca/papers/emse2016_mcintosh.pdf | “review coverage alone does not guarantee” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 28 | Google permits scanning some generated/data material. | https://google.github.io/eng-practices/review/reviewer/looking-for.html | “data files, generated code, or large data structures you can scan over sometimes” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 29 | The package-hallucination experiment covered 16 models. | https://www.usenix.org/system/files/usenixsecurity25-spracklen.pdf | “using 16 models” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 30 | The package-hallucination experiment generated 576,000 code samples. | https://www.usenix.org/system/files/usenixsecurity25-spracklen.pdf | “576,000 code samples” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 31 | Debugging AI-generated code was the survey's second-largest frustration. | https://survey.stackoverflow.co/2025/ai | “Debugging AI-generated code is more time-consuming” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 32 | Test-first review found the same production-defect proportion. | https://sback.it/publications/icse2019a.pdf | “find the same proportion of defects in production code” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 33 | GitHub first observed higher review cost and fewer caught issues from broad browsing. | https://github.blog/ai-and-ml/github-copilot/better-tools-made-copilot-code-review-worse-heres-how-we-actually-improved-it/ | “the cost of reviews was higher and fewer issues were being caught” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 34 | GitHub's coding agent self-reviews. | https://github.blog/ai-and-ml/github-copilot/whats-new-with-github-copilot-coding-agent/ | “reviews its own changes” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 35 | GitHub's coding-agent workflow includes three automated security scans. | https://github.blog/ai-and-ml/github-copilot/whats-new-with-github-copilot-coding-agent/ | “code scanning, secret scanning, and dependency vulnerability checks” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 36 | Anthropic's stronger reviewer needed additional repository context to find the postmortem bug. | https://www.anthropic.com/engineering/april-23-postmortem | “provided the code repositories necessary” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 37 | OpenAI reports repetitive, well-scoped tasks as common Codex uses. | https://openai.com/index/introducing-codex/ | “repetitive, well-scoped tasks, like refactoring, renaming, and writing tests” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 38 | METR participants perceived a 20% speedup after the study. | https://metr.org/Early_2025_AI_Experienced_OS_Devs_Study-paper.pdf | “developers estimate that allowing AI reduced completion time by 20%” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 39 | METR's RCT involved 16 developers and 246 tasks. | https://metr.org/Early_2025_AI_Experienced_OS_Devs_Study-paper.pdf | “16 developers with moderate AI experience complete 246 tasks” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 40 | Anthropic observed that people usually plan while Claude executes. | https://www.anthropic.com/research/claude-code-expertise | “People decide what to build, and the agent decides how to build it.” | SUPPORTED | HTTP 200, fetched 2026-07-26 |
| 41 | GitHub rulesets can require status checks. | https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets | “Require status checks to pass before merging” | SUPPORTED | HTTP 200, fetched 2026-07-26 |

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

- 2026-07-26 — Initial complete synthesis by Codex GPT-5 from live primary-source retrieval. The run explicitly tested the prompt's proposed prior, traced classic LOC guidance to primary/foundational sources, searched alternate framings before recording evidence gaps, and performed a hostile citation/entailment pass.
- 2026-07-26 — Verification pass: `aido validate-doc` passed at `research-1.2.0`; all 24 footnote definitions are referenced and contiguous, and all 41 provenance rows are contiguous.

<!-- /doc:region name="run_history" -->
