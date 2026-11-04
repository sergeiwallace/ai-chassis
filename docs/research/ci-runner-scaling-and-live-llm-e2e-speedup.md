---
title: "Sustainable Scope-Aware CI Gating for a Live-LLM E2E Pipeline: Blocking Only the PRs a Failure Actually Covers"
category: research
tags: [research, ci, github-actions, required-checks, merge-queue, affected-tests, path-filter, e2e, llm-testing, developer-experience, self-hosted-runners]
status: draft
source: "Genericized from prior personal research; external claims cited inline."
template_version: "research-1.1.0"
---

# Sustainable Scope-Aware CI Gating for a Live-LLM E2E Pipeline: Blocking Only the PRs a Failure Actually Covers

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. Scope-aware test selection: mapping changed files to the tests that cover them](#1-scope-aware-test-selection-mapping-changed-files-to-the-tests-that-cover-them)
- [2. The crux: conditional *required* checks without hanging unrelated PRs](#2-the-crux-conditional-required-checks-without-hanging-unrelated-prs)
- [3. Merge queue: batching expensive checks at merge time](#3-merge-queue-batching-expensive-checks-at-merge-time)
- [4. Required vs advisory: where a live-LLM E2E belongs](#4-required-vs-advisory-where-a-live-llm-e2e-belongs)
- [5. Scoping the live-LLM E2E itself (post-deploy, coarse to map)](#5-scoping-the-live-llm-e2e-itself-post-deploy-coarse-to-map)
- [6. Sustainability and developer experience: what keeps a gate trusted](#6-sustainability-and-developer-experience-what-keeps-a-gate-trusted)
- [7. Runner capacity and concurrency](#7-runner-capacity-and-concurrency)
- [Gaps, blindspots and emergent findings](#gaps-blindspots-and-emergent-findings)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
- [Open Questions](#open-questions)
- [Findings by Question](#findings-by-question)
- [Sources](#sources)

<!-- doc:region name="context" kind="immutable" -->

## Context

This is a general treatment of a recurring CI problem: a single self-hosted runner
serializes the whole delivery path — per-PR verification checks, the container image
build, and a long (40 to 60 minute) live-LLM post-deploy E2E suite all land on one
physical VM that runs one job at a time. Merges queue for hours, and the long live suite
frequently reds the promotion gate.

The generic questions this survey answers: how do mature teams (a) scale runner
capacity, (b) split a fast per-merge smoke from a deep suite, (c) use
`concurrency`/`cancel-in-progress`/`queue` correctly for CI versus deploy, (d) make a
live non-deterministic LLM E2E fast and reliable, and (e) decouple the post-deploy gate
so a failing E2E blocks dev-to-prod promotion without making the dev deploy itself look
failed?

**Typical system profile (the class of setup this addresses):**
- Several workflows share one runner host, so per-PR verification, the image build, and
  the post-deploy pipeline effectively serialize behind one executor.
- The E2E is a real WebSocket chat session against a deployed dev URL. Each turn invokes
  a live model plus real tools, so tests are non-deterministic and re-running has real
  side effects.
- The E2E runner uses a sequential test runner (e.g. vitest with `fileParallelism:
  false`), a multi-minute per-test timeout, and a ~60-minute job timeout. The deep
  ("extended") scenario suite runs by default on the automatic post-deploy-to-dev path.
- The post-deploy workflow already uses a `concurrency` group with
  `cancel-in-progress: false` and gates the `workflow_run` path on deploy success plus
  the target branch.
- Runs on shared cloud infrastructure, so strong isolation and least-privilege matter.

**Decision this research should inform:** a concrete, low-regret path to (1) stop merges
queuing for hours, (2) keep a trustworthy blocking signal, and (3) keep the deep live-LLM
validation without it being the thing that reds every merge.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

Primary: 2024–2026 tooling documentation and vendor guidance (Nx, Bazel, Gradle
Develocity, Azure DevOps TIA, GitHub Actions `dorny/paths-filter`). Foundational
regression-test-selection theory (Rothermel & Harrold; Ekstazi; STARTS; Meta PTS) is
weighted as durable background; the formal vocabulary has not changed since publication
and is cited for its definitional authority, not recency. Decision-economics and
defect-stage-cost sources are treated as stable industry heuristics.

## Executive Summary

1. **A cheap, cached, safety-critical suite is the canonical example of a suite that
   must NOT be selectively scoped.** Where a fast test suite is kept always-on (no paths
   filter) — typically after an escaped regression proved a coverage gap, and kept cheap
   via dependency caching — selection's savings are trivial and its downside (an escaped
   regression) is catastrophic. Selection value concentrates in the **expensive,
   capacity-bound live-LLM E2E suite** [INFERENCE].

2. **Regression test selection (RTS) has a 30-year formal vocabulary.** Rothermel and
   Harrold define selection along **inclusiveness (safety), precision, efficiency,
   generality**; a technique is *safe* iff 100% inclusive [VERIFIABLE][^1]. Real tools
   trade safety for savings: **Ekstazi** (dynamic, file-level) and **STARTS** (static,
   class-firewall) are empirically strong but unsafe under reflection, dynamic dispatch,
   and non-code dependencies [VERIFIABLE][^3][^4]. The honest limits are the load-bearing
   part for earning skeptic trust. (Full texts are paywalled; existence, authorship, and
   venue were confirmed via Crossref.)

3. **Predictive/ML selection (Meta, Gradle Develocity) is real but out of scope at small
   scale.** Meta's learned model halves testing infrastructure cost while still reporting
   over 99.9% of faulty changes and over 95% of individual test failures, but it is
   trained on a large historical dataset from a high-change-rate monorepo and imports a
   bounded probabilistic false-negative by design [VERIFIABLE][^5]. Gradle Develocity
   ships tunable confidence profiles and needs substantial per-target execution history
   to calibrate [VERIFIABLE][^6]. Neither is justifiable for a small polyglot repo
   [INFERENCE].

4. **The change-class robustness matrix is a first-class artifact.** Naive path-globs
   silently under-select on new files with no matching glob, renames (git delete+add),
   shared-util edits, and lockfile bumps [VERIFIABLE][^7]. Graph tools fix transitive
   awareness but over-select on shared libs and fail safe (all-affected) on rename,
   delete, and lockfile changes [VERIFIABLE][^12][^14]. Coverage tools miss reflection
   and non-code inputs [VERIFIABLE][^9][^13].

5. **Robustness = a fallback catalog + selector-auditing loop, not a one-liner.** The
   toolkit is conservative full-suite fallback on unmapped change; run-all on
   selector-config/lockfile change; a cheap *deterministic* periodic full sweep (distinct
   from a rejected live-LLM nightly); always-run labels; flaky quarantine; and — the
   piece skeptics demand — auditing the selector itself via mutation testing
   [VERIFIABLE][^13][^27]. Black-box LLM E2E ownership scoping decays via CODEOWNERS/tag
   rot and needs a lint check plus an always-on smoke floor [VERIFIABLE][^24].

## 1. Scope-aware test selection: mapping changed files to the tests that cover them

**The goal is blast-radius scoping: a failure in area X should only block a PR that
changed area X.** There are three mature mechanisms, in increasing precision and cost.

**Changed-file gating (coarse, cheapest).** Detect which paths a PR touched and run only
the relevant jobs. `dorny/paths-filter` "enables conditional execution of workflow steps
and jobs, based on the files modified by pull request" and exists specifically because
GitHub's built-in path filters "don't work on a level of individual jobs or steps"
[VERIFIABLE][^7]. `tj-actions/changed-files` tracks changed files relative to a target
branch, "scales to handle large/mono repositories," and (important below) "supports
merge queues for pull requests" [VERIFIABLE][^8]. This is directory-granular, not
coverage-accurate, but it is enough to say "this PR is docs-only, skip the Python suite"
or "this PR only touched the frontend, skip the server integration job."

**Coverage-based / affected selection (precise, per-language).** pytest-testmon "is a
pytest plugin which selects and executes only tests you need to run. It does this by
collecting dependencies between tests and all executed code (internally using
Coverage.py) and comparing the dependencies against changes," and it "always re-executes
tests which failed last time" [VERIFIABLE][^9]. pytest-picked runs "the tests related to
the unstaged files or the current branch (according to Git)" [VERIFIABLE][^10]. In the TS
world, Jest's `--findRelatedTests <files>` finds and runs the tests that cover a
space-separated list of source files, and `--changedSince` "runs tests related to the
changes since the provided branch or commit hash" [VERIFIABLE][^11]. (Vitest exposes the
same idea via `--changed` / `--related`; but see Section 5 for why coverage-selection does
not transfer to a live networked suite.)

**Project-graph selection (monorepo-native).** Nx `affected` uses the workspace's
dependency graph to "determine the minimum set of projects that are affected by the
change" and "only runs tasks on those affected projects," which "drastically improves the
speed of your CI" [VERIFIABLE][^12]. The same doctrine is packaged in Azure DevOps Test
Impact Analysis, which "selects and runs only the relevant tests required to validate that
commit," runs "impacted tests, previously failing tests, and newly added tests," and
critically "falls back to running all tests" for code it cannot understand
[VERIFIABLE][^13]. That fallback is the correct default posture: when the mapping is
uncertain, run everything, never silently skip.

**When to use**

- **Changed-file gating (`dorny/paths-filter`, `tj-actions/changed-files`):** the
  cheapest first step; enough to skip a whole suite for an unrelated PR (docs-only,
  frontend-only), but directory-granular, not coverage-accurate [VERIFIABLE][^7][^8].
- **Coverage / affected selection (testmon, jest `--findRelatedTests`, vitest
  `--changed`):** per-language, where the coverage-to-test map is reliable and
  deterministic (unit suites); never for a networked live E2E [VERIFIABLE][^9][^11].
- **Project-graph selection (Nx affected, Test Impact Analysis):** monorepo-native, when
  a real dependency graph exists; always with a run-all fallback on any change it cannot
  map [VERIFIABLE][^12][^13].

## 2. The crux: conditional *required* checks without hanging unrelated PRs

**This is the mechanism that makes scope-aware gating safe, and the single biggest
footgun.** GitHub branch protection lists specific status checks as required. The trap:
"If a workflow is skipped due to path filtering, branch filtering or a commit message,
then checks associated with that workflow will remain in a 'Pending' state. A pull
request that requires those checks to be successful will be blocked from merging"
[VERIFIABLE][^20]. So if you naively add `paths:` to a *required* workflow to skip it for
unrelated PRs, you do the exact opposite of the goal: every unrelated PR hangs on
"Waiting for status to be reported" forever.

The distinction that fixes it: "If, however, a job within a workflow is skipped due to a
conditional, it will report its status as 'Success'," and GitHub counts `success`,
`skipped`, and `neutral` all as successful check statuses [VERIFIABLE][^20]. So the
pattern is:

1. Keep one always-run workflow that is the *only* required check.
2. Inside it, gate the expensive sub-jobs with `if:` conditionals driven by a
   changed-files step, so a skipped sub-job reports success rather than leaving a required
   workflow un-reported.
3. Add a single aggregator "all green" job with `if: always()` and `needs: [all the
   sub-jobs]` that fails unless every voting job succeeded, and make *that* the required
   check.

The aggregator is necessary because it is "mandatory to make the `check` job run always,
not only when all of the needed jobs succeed" (a failed dependency otherwise leaves the
check `skipped` rather than `failed`) [VERIFIABLE][^21]. re-actors/alls-green (or the
step-security fork) implements exactly this; the GitHub docs give the same guidance: use
the `always()` conditional expression in addition to `needs` for a required check on a
job that depends on other jobs [VERIFIABLE][^20]. This is the well-trodden Zuul-style
single-gate pattern that inspired the action [VERIFIABLE][^21].

Applied to a polyglot repo: make a single aggregator job the required check. Under it,
run the Python suite only when Python code or requirements changed, the server
integration tests only when server code changed, the frontend build only when frontend
code changed, and so on, each as an `if:`-gated job that reports success when skipped. A
docs-only or frontend-only PR then never runs (and is never blocked by) the Python suite,
yet the required gate still reports a clean status.

**When to use**

- **One always-run aggregator as the only required check:** whenever any expensive
  sub-job is conditionally skipped, so a skipped sub-job reports success instead of
  leaving a required workflow un-reported [VERIFIABLE][^20].
- **`re-actors/alls-green` (or the step-security fork):** when you want the "list the
  jobs once, protect one check" ergonomics and correct skipped-versus-failed handling
  without hand-rolling `always()` logic [VERIFIABLE][^21].
- **Hand-rolled `if:` job conditionals + `always()` needs:** the equivalent when you
  prefer not to add a marketplace action [VERIFIABLE][^20].
- **Never** path-filter a *required workflow* directly: a skipped required workflow stays
  "Pending" and blocks every unrelated PR forever [VERIFIABLE][^20].

## 3. Merge queue: batching expensive checks at merge time

**A merge queue moves expensive validation from per-PR to per-batch at merge time.**
Instead of every PR paying for the full suite before merge, the queue forms a group of
queued PRs, builds the combined result on a temporary `gh-readonly-queue/<base>` branch,
and merges the batch if the combined checks pass. Two hard requirements: "A merge queue
will wait for required checks to be reported before it can proceed with merging," and you
"must use the `merge_group` event to trigger your GitHub Actions workflow when a pull
request is added to a merge queue" [VERIFIABLE][^22]. The queue throttles CI via **Build
concurrency**, the maximum number of `merge_group` webhooks to dispatch, "between `1` and
`100`" [VERIFIABLE][^22]. It can also be configured to "only merge non-failing pull
requests," letting PRs that failed required checks still ride in a group as long as the
last PR in the group passes, which is useful with intermittent failures you do not want
holding up the queue [VERIFIABLE][^22].

The honest caveat for a single-runner setup: with one runner, build concurrency is
effectively 1, so a merge queue reduces the *number* of expensive runs (one batch instead
of N per-PR runs) but does not remove serialization. And a live-LLM E2E that is
post-deploy, not a pre-merge check, does not fit the `merge_group` pre-merge model without
first deploying the batch somewhere. A merge queue is therefore a "once you have the
second runner, and for the deterministic pre-merge suite" option, not the first move.

**When to use**

- **After a second runner lands, for the deterministic pre-merge suite:** batch the
  verification checks at merge time so N near-simultaneous merges pay once, not N times
  [VERIFIABLE][^22].
- **Only once capacity exists:** on a single runner, build concurrency is effectively 1,
  so a merge queue lowers run count via batching but does not remove serialization
  [VERIFIABLE][^22].
- **With "only merge non-failing" tuned to your flake tolerance** [VERIFIABLE][^22].
- **Not for the live-LLM E2E:** it is post-deploy, not a pre-merge `merge_group` check
  [INFERENCE].

## 4. Required vs advisory: where a live-LLM E2E belongs

**A check is either required (blocks merge/promotion) or advisory (informational).** A
required check must be fast, deterministic enough to trust, and scoped so its failures
implicate the change under test. An advisory check surfaces information without gating.

The live-LLM E2E fails the "deterministic enough and fast enough to be a per-PR required
check" test: it needs a live deployed app, real model turns, and minutes of wall-clock,
and it is legitimately non-deterministic. But making it purely advisory is the other
failure mode: advisory checks rot. Google's data shows exactly this dynamic, where a
history of false signals trains people to "ignore alarms," and flaky results cause teams
to "ignore legitimate failures" [VERIFIABLE][^26]. An advisory live-LLM E2E that nobody is
required to act on will drift into that ignored state.

The resolution: keep the E2E as a *required promotion gate* on the deploy path
(post-deploy, dev to prod), not a per-PR merge gate, and decouple it via GitHub
deployment environments so a red E2E blocks promotion to the prod environment without
marking the dev deploy itself failed [VERIFIABLE][^23]. That gives it teeth (it blocks
something real, promotion) while keeping it off the per-PR critical path. Note the
plan-tier caveat below: on GitHub Free/Pro/Team these environment protection rules are
only available for public repositories [VERIFIABLE][^23].

**When to use**

- **Required per-PR check:** for a fast, deterministic, scoped suite (unit, integration,
  Python) whose failures implicate the change under test [INFERENCE].
- **Required promotion gate (post-deploy, via environments):** for the live-LLM E2E, so
  it blocks dev-to-prod promotion without blocking unrelated merges or marking the dev
  deploy failed [VERIFIABLE][^23].
- **Advisory only sparingly:** an advisory check nobody must act on rots into ignored
  noise [VERIFIABLE][^26].

## 5. Scoping the live-LLM E2E itself (post-deploy, coarse to map)

**Scenario-to-code mapping is fundamentally harder for E2E than for unit tests, so use
ownership, not coverage.** A unit test's covered files are known from an in-process
coverage run; a live E2E scenario exercises a deployed black box over WebSocket, so
testmon-style coverage selection does not apply (testmon tracks executed code via
Coverage.py, not external services reachable through the network) [VERIFIABLE][^9]. The
realistic mechanisms are declarative:

- **Tag / label-driven scenario selection.** Tag each E2E scenario with the subsystem or
  skill it exercises, and select scenarios by the changed-files set (the same
  `paths-filter` / `changed-files` inputs from Section 1). When a PR touches a given
  subsystem, run that subsystem's tagged scenarios; when it touches shared session-layer
  code, run the canonical cross-cutting set.
- **CODEOWNERS-style ownership mapping.** Map path globs to scenario groups so ownership
  of a code area implies which scenarios gate it. This is the same idea Test Impact
  Analysis formalizes, with the same mandatory fallback: run the full suite when the
  change cannot be mapped [VERIFIABLE][^13].
- **Label-scoped deep runs.** Let a maintainer add a label to opt a risky PR (or a model
  upgrade) into the full weighted-N deep suite via `workflow_dispatch`, without imposing
  it on every merge [VERIFIABLE][^19].

The scope-aware change is to drive *which scenarios* run from the merge's changed-files
set (a small always-on smoke for every promotion, plus the owning subsystem's scenarios),
and to reserve the full weighted-N extended matrix for a label-triggered or manual
`workflow_dispatch` run and, later, a merge-queue batch, rather than forcing the full
deep suite on every merge or on an idle schedule.

**When to use**

- **Tag / label-driven scenario selection:** tag each scenario with the subsystem it
  exercises and select by the changed-files set [VERIFIABLE][^7][^8].
- **Always-on smoke floor plus owning-subsystem scenarios:** a small deterministic smoke
  on every promotion, plus the scenarios owned by the changed surface, so a tag-map bug
  can never cause a total skip [INFERENCE].
- **Label-triggered / `workflow_dispatch` deep suite:** for model upgrades, risky
  refactors, and release candidates; not on a schedule and not on every merge
  [VERIFIABLE][^19].

## 6. Sustainability and developer experience: what keeps a gate trusted

**A gate survives its author only if it is trusted, and trust is a false-positive-rate
problem.** A common failure mode is that a team rips out an onerous gate once the person
who championed it moves on — the predictable end state of a noisy gate. Google measured
that "almost 16% of our tests have some level of flakiness," and the behavioral
consequence is that it is common to ignore legitimate failures because of the high number
of false positives, since people "ignore alarms when there is a history of false signals
coming from a system" [VERIFIABLE][^26]. Fowler's older but unsuperseded statement is the
same: quarantine helps reduce a flaky test's damage to other tests, "but you still have to
fix them soon," or the pipeline is compromised [VERIFIABLE][^25].

The levers that keep a gate trusted:

- **Low false-positive rate.** Fix deterministic false-fails first. One recurring
  false-fail teaches the team to ignore the gate.
- **Scoped blast radius.** A failure blocks only PRs that changed the covered area
  (Sections 1 and 2). Blocking an unrelated PR is the fastest way to lose consent.
- **Fast feedback.** Keep the per-PR required set fast and deterministic; keep the slow
  live suite off the per-PR path (Sections 4 and 5).
- **Never silently skip.** Prefer the Test-Impact "fall back to running all tests"
  default over a selector that can hide a real regression [VERIFIABLE][^13].

Sustainability is bought by trustworthiness (low false positives, scoped blast radius,
fast feedback), not by adding a scheduled deep run that spends the scarce runner on idle
days.

## 7. Runner capacity and concurrency

**Parallelism across executors is the direct cure for head-of-line blocking.** When one
VM runs one job at a time, a PR's verification waits behind an in-flight build which waits
behind an in-flight E2E. Distinct labels only help if they map to distinct executors;
registering a second runner lets per-PR verification stop queuing behind the post-deploy
pipeline. Ephemeral / just-in-time runners are the security-correct default on shared
infrastructure: GitHub recommends autoscaling with ephemeral runners and advises against
persistent ones, because ephemeral runners guarantee a clean environment per job
[VERIFIABLE][^16], and a persistent self-hosted runner "can be persistently compromised
by untrusted code in a workflow" [VERIFIABLE][^17]. ARC (Actions Runner Controller) on
Kubernetes is the answer only at Kubernetes scale (its runners are "ephemeral and based on
containers" and scale "rapidly and cleanly") [VERIFIABLE][^18]; do not adopt Kubernetes
to fix a three-job serialization problem.

**Concurrency semantics differ deliberately between CI and deploy.** For PR CI,
`cancel-in-progress: true` supersedes stale runs for fast feedback; for deploys,
`cancel-in-progress: false` never interrupts a release, and GitHub's optional `queue`
property lets a pending run wait in the same concurrency group, which cannot be combined
with `cancel-in-progress: true` [VERIFIABLE][^15]. And the live suite's own reliability
guards still hold: a per-turn watchdog under the per-test timeout so a hung turn fails
only its own test (a sequential runner runs files one at a time under
`fileParallelism: false`) [VERIFIABLE][^28], intent-level assertions that tolerate
legitimate error-path narration, and quarantine of genuine flakes with an owner and
deadline [VERIFIABLE][^25].

**When to use**

- **A second runner:** so PR checks never queue behind the post-deploy pipeline.
- **Ephemeral / JIT runners:** as the security posture for any added capacity on shared
  infra [VERIFIABLE][^16][^17].
- **Split concurrency (`cancel-in-progress` true for PR CI, false plus the optional
  `queue` property for deploy):** keep each lane's semantics correct [VERIFIABLE][^15].
- **ARC on Kubernetes:** only at genuine Kubernetes scale, not to fix a three-job
  serialization problem [VERIFIABLE][^18].

## Gaps, blindspots and emergent findings

- **Coverage-selection is a false friend for E2E.** A blindspot in any "scope the tests"
  framing is assuming the live suite can be coverage-mapped like unit tests. It cannot
  (testmon tracks executed code via Coverage.py, not networked external services)
  [VERIFIABLE][^9]; ownership/tag mapping is the only safe route (Section 5).
- **Merge queue does not add capacity.** On one runner, build concurrency is 1, so a
  merge queue lowers run count via batching but not serialization; pairing it with a
  second runner is what unlocks its benefit [VERIFIABLE][^22].
- **Private-repo environment gates require a higher plan tier.** On GitHub Free/Pro/Team,
  environment protection rules are only available for public repositories
  [VERIFIABLE][^23]; a private repo needs a plan that offers protected environments (or
  an external gate) for the built-in promotion gate.

## Comparison

| # | Criterion | Blanket required E2E on every merge (status quo) | Scope-aware required per-PR checks + aggregator gate | Merge queue (batched checks at merge) | E2E as environment-decoupled promotion gate |
|---|-----------|--------------------------------------------------|------------------------------------------------------|---------------------------------------|---------------------------------------------|
| 1 | Blocks only PRs that touch covered code | No (blocks everyone) | Yes, via changed-files `if:` gating [^7][^8][^12] | Partial (batched, not scoped) | N/A (gates promotion, not PR merge) |
| 2 | Skipped-required-check hang risk | N/A | Eliminated by always-run aggregator [^20][^21] | Handled if `merge_group` reports [^22] | N/A |
| 3 | Fits a live-LLM, post-deploy suite | Forced, reds every merge | No (E2E is not pre-merge) | Poor (pre-merge model) | Yes (post-deploy, blocks promotion) [^23] |
| 4 | Reduces expensive runs | No | Yes (skips unaffected suites) | Yes (one batch vs N) [^22] | Yes (off per-PR path) |
| 5 | Fixes single-runner serialization | No | Partially (fewer jobs) | No (build concurrency 1 on one runner) [^22] | Partially (off PR path) |
| 6 | Sustainability / trust | Low (blocks unrelated PRs) [^26] | High (scoped, fast) [^26] | Medium | High (teeth without per-PR friction) |
| 7 | Setup effort | None | Low to medium (filters + aggregator) | Medium (branch protection + `merge_group`) | Low to medium (environments + plan tier) |

## Recommendation

**Make the gate trustworthy and scope-aware, not scheduled.** In priority order:

1. **Fix the deterministic false-fail first.** One recurring false positive is what
   teaches a team to ignore, then delete, a gate [VERIFIABLE][^26]. This is the cheapest,
   highest-leverage sustainability move.
2. **Make the per-PR required check scope-aware behind one always-run aggregator.**
   Path-filter the sub-jobs of the verification workflow (Python suite, server
   integration, frontend build, architecture invariants) with a changed-files step
   [VERIFIABLE][^7][^8], and gate branch protection on a single `if: always()` + `needs`
   aggregator job [VERIFIABLE][^20][^21]. This lets an unrelated PR merge while keeping
   the check "required": a Python failure blocks only PRs that touched Python. **Never
   path-filter a required workflow directly** [VERIFIABLE][^20].
3. **Use per-language affected-selection inside those sub-jobs where the map is
   reliable.** testmon or a path-filtered pytest job for the Python suite; jest
   `--findRelatedTests` / vitest `--changed` for TS unit tests; always with a full-suite
   fallback for unmapped changes [VERIFIABLE][^9][^11][^13].
4. **Keep the live-LLM E2E as a post-deploy promotion gate, scoped by ownership, never
   coverage.** A small always-on smoke per promotion, plus the owning subsystem's tagged
   scenarios selected from the merge's changed files; reserve the full weighted-N deep
   suite for a label-triggered / `workflow_dispatch` run (model upgrades, release
   candidates), not a schedule and not every merge [VERIFIABLE][^13][^19]. Decouple
   promotion via GitHub environments so a red E2E blocks prod, not the dev deploy
   [VERIFIABLE][^23].
5. **Add a second runner, then consider a merge queue.** A second runner removes
   head-of-line blocking so PR checks stop queuing behind the post-deploy pipeline; keep
   concurrency semantics split (`cancel-in-progress: true` for PR CI, `false` plus the
   optional `queue` property for deploy) [VERIFIABLE][^15][^16][^17]. Only after capacity
   exists does a merge queue pay off, and even then it batches rather than parallelizes on
   a single executor [VERIFIABLE][^22].

**Is scope-aware required checks advisable? Yes for the per-PR deterministic suite
(unit/integration/Python), the canonical low-regret pattern. Partially for the E2E: scope
its *scenarios* by ownership, but keep it a post-deploy promotion gate rather than a
per-PR required check.** The strongest caveat, and the one thing that is actively *not*
advisable: do not path-filter a required *workflow* to achieve scoping; a skipped required
workflow stays "Pending" and blocks every unrelated PR forever [VERIFIABLE][^20]. The
scoping must live in `if:`-gated jobs under a single always-run aggregator, or it
backfires. Also not advisable: coverage-auto-selecting the live E2E scenarios (the mapping
is unsafe for a networked black box) [VERIFIABLE][^9], and parking the E2E as a purely
advisory check (advisory rots into ignored noise) [VERIFIABLE][^26].

## Open Questions

1. What is the actual Python CI definition (runner, trigger, required-check status)? A
   testmon / path-filter recommendation needs to be validated against the real workflow
   before implementation.
2. Is the repo private on a plan tier without protected environments? If so, built-in
   environment protection rules are unavailable and an external promotion gate is required
   [VERIFIABLE][^23].
3. What is the right scenario-to-path ownership map for the E2E, and which small set is
   the always-on smoke that runs for every promotion regardless of changed files?
4. Once a second runner exists, is a merge queue worth the branch-protection and
   `merge_group` wiring for the deterministic pre-merge suite?
5. Do the distinct runner labels actually resolve to one physical VM? The capacity fix
   depends on the answer.

## Findings by Question

### Correcting the contradiction: a cheap safety-critical suite is the always-on exemplar

**The general economic argument for keeping cheap safety-critical suites always-on.**
Skipping a *fast* test does not save meaningful time; it shifts cost downstream, moving
debugging from a CI pipeline (minutes) to production (hours) [INFERENCE]. The whole point
of making a suite fast (e.g. dependency-wheel caching) is to remove every excuse not to
run it: the fastest and most relevant checks are the ones that actually get used as a
real-time safety net [HEURISTIC]. And selective testing is only as good as its impact map:
if that analysis misses an affected component, a defect slips through untested
[VERIFIABLE][^13]. A team that has already suffered an escaped regression through a
coverage gap has the postmortem proving its map was unreliable for that suite [INFERENCE].

**The break-even math confirms the split.** Defect-stage cost is widely held to escalate
by roughly an order of magnitude at each hand-off, cheap in CI, more in QA, most in
production [HEURISTIC]. Selection ROI is approximately
`run-frequency × per-run cost saved − miss-cost × miss-probability`. For a cache-warmed
suite already running in low-minutes, the time-saved numerator is small and the miss-risk
denominator (a corruption-class miss with a known history) is large. For a slow live-LLM
E2E on a serialized self-hosted VM, the time-saved term is enormous and dominates.
Selection **pays where it's expensive and hurts where it's cheap** [INFERENCE].

**Escaped-regression reality when selection is on-but-imperfect:** Test Impact Analysis is
designed to fall back to running all tests for code it cannot map, and prescribes a
periodic full run to catch selection misses, precisely because the risk of a stale or
incomplete map is real [VERIFIABLE][^13].

### RTS formal foundations: safety vs precision

Rothermel and Harrold's evaluation framework defines four axes [VERIFIABLE][^1] (this and
the other regression-test-selection papers cited here are peer-reviewed and paywalled;
existence, authorship, and venue were confirmed via Crossref):

| Axis | Definition | Plain meaning |
|---|---|---|
| **Inclusiveness** | fraction of fault-revealing (modification-traversing) tests the technique selects | "does it catch what could break?" |
| **Precision** | ability to *exclude* tests that cannot change output | "does it avoid wasted runs?" |
| **Efficiency** | space/time cost of the selection computation itself | "is deciding what to run cheap?" |
| **Generality** | how broadly the technique applies in practice | "does it work on real code?" |

A technique is **safe iff it is 100% inclusive**: it never omits a test that could reveal
a fault in the modified program [VERIFIABLE][^1]. Their DejaVu technique selects
modification-traversing tests via control-flow-graph comparison and is *proven* safe under
three assumptions: tests passed on the original program, obsolete tests were removed, and
determinism [VERIFIABLE][^2]. **The third assumption is fatal for the live-LLM E2E:** a
networked black-box LLM app is non-deterministic, so CFG/coverage-style safety proofs do
not transfer. This is the theoretical reason the E2E needs *ownership* scoping, not
coverage scoping [INFERENCE].

**Two canonical tools and their honest limits.**

- **Ekstazi (dynamic, file-level).** Instruments bytecode to record the files each test
  actually touches, stores name plus checksum, and re-selects tests whose dependencies
  changed [VERIFIABLE][^3]. Because it observes actual execution, it captures reflection
  and dynamic dispatch that static analysis misses. Its cost: an instrumented collection
  pass, and it tracks files, not arbitrary external inputs.
- **STARTS (static, class-firewall).** Builds an intertype relation graph over types and
  selects tests in the transitive closure reaching a changed type. **Documented
  unsafety:** class-level static selection can be unsafe if it misses edges, and the
  extensive study found reflection to be the observed cause of unsafety [VERIFIABLE][^4].
  Reflection-aware fixes improve safety but at very high cost.

**Where safety breaks:** non-code inputs (data fixtures, prompts, CI YAML) sit outside
both the type graph and the file-dependency map unless explicitly declared — exactly the
class of change that produces silent-skip incidents [INFERENCE].

### Predictive / ML-based selection at scale (and why it's out of scope here)

**Meta "Predictive Test Selection" (Machalica et al., ICSE-SEIP 2019).** Reframes
selection from "which tests *could* be impacted" to the probability that a given test
finds a regression for a given change, learned from a large dataset of historical test
outcomes [VERIFIABLE][^5]. Deployed in production, the strategy "reduces the total
infrastructure cost of testing code changes by a factor of two, while guaranteeing that
over 95% of individual test failures and over 99.9% of faulty changes are still reported
back to developers" [VERIFIABLE][^5]. It explicitly accounts for the non-determinism of
test outcomes (flakiness) [VERIFIABLE][^5].

**Gradle Develocity PTS (productized).** Ships tunable confidence profiles plus a
simulator that replays the model against Build Scan history to show catch-rate versus
savings before skipping anything [VERIFIABLE][^6]. It always runs tests that are recently
new, changed, failed, or flaky regardless of score, and needs sufficient per-target
execution history before it will select [VERIFIABLE][^6].

**Verdict for a small repo: not applicable** [INFERENCE]. Develocity's per-target
calibration-data requirement cannot be met by a small repo; the training-data volume that
makes Meta's guardrail hold does not exist; and importing a probabilistic miss into a
suite whose whole reason for existing (the E2E) is to catch rare, high-cost regressions is
the wrong risk trade.

### The change-class robustness matrix

Legend: ✅ robust · ⚠️ over-runs (safe-but-wasteful) · ❌ silently under-selects (unsafe).

| Strategy ↓ / Change class → | New file (no matching glob) | Rename / move | Shared / cross-cutting util | Config / dependency / lockfile | Non-code (CI YAML, fixtures, prompts) |
|---|---|---|---|---|---|
| **Path / glob filter** | ❌ silently skipped | ❌ git shows delete+add; large-diff renames escape | ❌ leaf-dir globs under-select | ❌ lockfile bump touches no source glob | ❌ unless a fixture/YAML glob is hand-authored |
| **Build/dependency graph** (Nx, Bazel, Turborepo) | ✅ new file maps to its project | ⚠️→❌ delete/rename marks all projects affected (fail-safe) | ⚠️ transitively correct but over-selects | ⚠️ all-affected fail-safe on lockfile/config change | ✅ *only if* declared as inputs/deps; ❌ if undeclared runtime input |
| **Per-test coverage** (testmon, jest `--findRelatedTests`, Azure TIA) | ⚠️ needs prior run to map | ⚠️ checksum-based; usually re-selects | ✅ observes actual execution paths | ⚠️/❌ TIA falls back to all on unknown file types; misses reflection | ❌ non-code inputs invisible unless declared |
| **Predictive / ML** (Meta, Develocity) | ⚠️ probabilistic | ⚠️ probabilistic | ⚠️ probabilistic; always-runs flaky | ⚠️ "insufficient data" → all-impacted fallback | ❌ opaque; not per-change auditable |

**The four naive-path-glob misfire modes, documented:**

1. **New file, no matching glob, silent skip.** Path filters match file paths only; a
   change unmatched by any include pattern simply doesn't fire its job [VERIFIABLE][^7].
2. **Rename escapes the pattern.** Git does not explicitly track renames; a
   heavily-modified rename is recorded as delete(old) plus add(new) [HEURISTIC].
3. **Shared-util edit under-selects.** Change a shared utility that many packages depend
   on; a path filter on one package won't catch the transitive impact [VERIFIABLE][^7].
4. **Lockfile bump touches no source glob.** A transitive dependency change alters
   behavior without touching any glob-targeted source file. Graph tools compensate by
   marking all projects affected on lockfile change [VERIFIABLE][^12]; Bazel's lockfile
   records the resolution so it re-evaluates what changed [VERIFIABLE][^14]; path globs do
   neither.

### The decision rule: select vs always-run

Formalize per-suite. Let a suite have serial wall-clock `T` (warm-cache-adjusted),
false-negative cost `C_fn` (safety-criticality × miss-history), selection savings fraction
`s`, per-change miss probability under selection `p_miss`, change rate `λ`, selector
upkeep `M_sel`, and runner parallelism `R`.

**Select only when expected time saved exceeds expected risk cost:**

`compute saved/period ( λ·s·T/R )  >  expected escape cost/period ( λ·p_miss·C_fn ) + selector upkeep ( M_sel )`

Equivalently, selection is justified iff `T/R > (p_miss·C_fn)/s + M_sel/(λ·s)`.

**Reading the rule against the two suites:**

- **Cheap + cached + safety-critical + miss-history ⇒ ALWAYS-ON.** `T/R` is small;
  `C_fn` is large; `p_miss` is empirically nonzero. The right side dominates. Skipping
  shifts cost, it does not save it [INFERENCE].
- **Live-LLM E2E (expensive + slow + capacity-bound) ⇒ SELECTION PAYS.** `T` is large and
  `R` is small (serialized self-hosted VM). Even a second runner only halves the
  always-run cost; selection additionally cuts `s` of the remaining work, and the
  token/carbon cost adds a real per-run dollar term. The safe way to select here is
  *ownership* scoping, not coverage, because the determinism assumption fails [INFERENCE].

### Robustness safety-nets & selector auditing

1. **Conservative full-suite fallback on any unmapped change.** Azure TIA's own design:
   for commits and scenarios it can't understand, it falls back to running all tests
   [VERIFIABLE][^13].
2. **Run-all on selector-config or dependency/lockfile change.** Nx marks all projects
   affected whenever the lock file changes [VERIFIABLE][^12]; Bazel's lockfile drives
   re-evaluation [VERIFIABLE][^14]. Mirror this: any edit to the selection logic itself,
   or to a lockfile, triggers a full run.
3. **Cheap deterministic periodic full sweep, distinct from a rejected live-LLM
   nightly.** Impact-analysis vendors recommend a periodic full run to catch selection
   misses [VERIFIABLE][^13]. A periodic *deterministic* full sweep of the cheap suites has
   no live tokens and no flakiness, so it is compatible with rejecting a scheduled
   live-LLM deep run [INFERENCE].
4. **Always-run labels / manual overrides.** TIA exposes a switch to force a full run;
   provide the inverse too (a label that forces the full E2E) [VERIFIABLE][^13].
   Develocity always runs recently-flaky tests regardless of score [VERIFIABLE][^6].
5. **Flaky-test quarantine.** Isolate known-flaky tests so they neither block merges nor
   poison selection signal; Meta explicitly accounts for flaky results so they do not
   corrupt the classifier [VERIFIABLE][^5]. Quarantine, track, and fix is the disciplined
   loop [VERIFIABLE][^25].
6. **Audit the selector itself via mutation testing.** Do not trust that the selector
   re-runs the right tests, *verify* it. Mutation testing "assesses test suite efficacy by
   inserting small faults into programs and measuring the ability of the test suite to
   detect them" [VERIFIABLE][^27]; apply the same idea to the selector by confirming it
   schedules the tests that kill mutants in the changed code.

### Scoping the black-box live-LLM E2E by ownership (not coverage)

Because the determinism assumption fails, coverage- and CFG-based safety proofs do not
transfer to a networked LLM app. The workable scoping primitive is **ownership mapping** —
associate changed paths with the E2E scenarios owned by that surface via CODEOWNERS/tags,
accepting that this is a heuristic, not a safety guarantee [INFERENCE].

**The decay failure mode.** Ownership scoping degrades silently as **CODEOWNERS/tag rot**
sets in: files move, owners change teams, tags go stale, and the map quietly stops routing
changes to the right scenarios [INFERENCE].

**Two mandatory guards:**

1. **A CODEOWNERS/tag lint check** that fails CI when a path has no owner or references a
   non-existent tag/scenario. CODEOWNERS requires each owner to be a valid user or team
   with write access, which such a lint can enforce [VERIFIABLE][^24].
2. **An always-on smoke floor:** a small, cheap, deterministic subset of E2E-adjacent
   smoke checks that runs on *every* PR regardless of ownership routing, so a tag-map bug
   can never cause a total skip. The floor is the backstop; ownership scoping is the
   optimization on top of it [INFERENCE].

### Cost, token, and carbon accounting for the live-LLM E2E

The live-LLM E2E carries a real per-run dollar and carbon term absent from cheap suites:
each run consumes API tokens against a live model plus serialized VM wall-clock. This term
belongs in the left ("compute saved") side of the decision rule and materially strengthens
the case for selection on this suite specifically [INFERENCE]. It is *also* the reason a
scheduled-nightly-deep-E2E is a poor trade: recurring token spend against a
non-deterministic target buys unreliable signal at real cost [INFERENCE]. The
deterministic cheap-suite sweep sidesteps this entirely — no tokens, no flakiness.

## Sources

[^1]: Rothermel, G., & Harrold, M. J. (1996). [Analyzing regression test selection techniques](https://doi.org/10.1109/32.536955). IEEE Transactions on Software Engineering. Peer-reviewed; full text paywalled (IEEE); existence, authorship, and venue verified via Crossref. (Framework: inclusiveness, precision, efficiency, generality; a technique is safe iff 100% inclusive.)
[^2]: Rothermel, G., & Harrold, M. J. (1997). [A safe, efficient regression test selection technique](https://doi.org/10.1145/248233.248262). ACM TOSEM. Peer-reviewed; full text paywalled (ACM); verified via Crossref. (DejaVu; proven safe under three assumptions including determinism.)
[^3]: Gligoric, M., Eloussi, L., & Marinov, D. (2015). [Practical regression test selection with dynamic file dependencies](https://doi.org/10.1145/2771783.2771784). ISSTA (Ekstazi). Peer-reviewed; full text paywalled (ACM); verified via Crossref. (Dynamic, file-level, checksum-based selection; captures reflection and dynamic dispatch.)
[^4]: Legunsen, O., et al. (2016). [An extensive study of static regression test selection in modern software evolution](https://doi.org/10.1145/2950290.2950361). ACM SIGSOFT FSE (STARTS study). Peer-reviewed; full text paywalled (ACM); verified via Crossref. (Class-firewall static selection; reflection observed as the cause of unsafety.)
[^5]: Machalica, M., Samylkin, A., Porth, M., & Chandra, S. (2019). [Predictive Test Selection](https://arxiv.org/abs/1810.05286). arXiv:1810.05286 (ICSE-SEIP). (Reduces total testing infrastructure cost by a factor of two while guaranteeing that over 95% of individual test failures and over 99.9% of faulty changes are still reported; accounts for the non-determinism of test outcomes.)
[^6]: Gradle. [Predictive Test Selection (Develocity)](https://docs.gradle.com/develocity/predictive-test-selection/). Develocity documentation. (Tunable confidence profiles; a simulator over Build Scan history; always runs recently new, changed, failed, or flaky tests.)
[^7]: dorny. [paths-filter](https://github.com/dorny/paths-filter). GitHub. (Conditional execution of workflow steps and jobs based on the files modified; built-in path filters don't work at the level of individual jobs or steps.)
[^8]: tj-actions. [changed-files](https://github.com/tj-actions/changed-files). GitHub. (Track changed files and directories relative to a target branch; scales to handle large/mono repositories; supports merge queues for pull requests.)
[^9]: Arpas, T. [testmon](https://testmon.org/). testmon.org. (Pytest plugin that selects and executes only the tests you need, via Coverage.py dependencies; always re-executes tests that failed last time; tracks executed code, not external networked services.)
[^10]: Gomes, A. P. [pytest-picked](https://github.com/anapaulagomes/pytest-picked). GitHub. (Run the tests related to the unstaged files or the current branch, according to Git.)
[^11]: Jest. [Jest CLI Options](https://jestjs.io/docs/cli). Jest documentation. (--findRelatedTests runs the tests that cover a list of source files; --changedSince runs tests related to changes since a branch or commit.)
[^12]: Nx. [Run Only Tasks Affected by a PR (nx affected)](https://nx.dev/ci/features/affected). Nx documentation. (Determines the minimum set of projects affected by the change and only runs tasks on those projects; marks all projects affected on a lockfile change as a failsafe.)
[^13]: Microsoft. [Test Impact Analysis (TIA) in Azure Pipelines](https://learn.microsoft.com/en-us/azure/devops/pipelines/test/test-impact-analysis). Microsoft Learn. (Selects and runs only the relevant tests required to validate a commit; runs impacted, previously failing, and newly added tests; falls back to running all tests for code it cannot understand.)
[^14]: Bazel. [Bazel lockfile](https://bazel.build/external/lockfile). bazel.build. (Records specific versions and dependencies so Bazel re-evaluates exactly the changed resolution.)
[^15]: GitHub. [Control the concurrency of workflows and jobs](https://docs.github.com/en/actions/using-jobs/using-concurrency). GitHub Docs. (cancel-in-progress: true cancels a running job in the same group; the optional queue property lets a pending run wait; queue cannot be combined with cancel-in-progress: true.)
[^16]: GitHub. [Self-hosted runners reference](https://docs.github.com/en/actions/reference/runners/self-hosted-runners). GitHub Docs. (Autoscaling dynamically adjusts the number of runners by demand; ephemeral and just-in-time runners register per job for a clean environment.)
[^17]: GitHub. [Security hardening for GitHub Actions](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions). GitHub Docs. (A persistent self-hosted runner can be persistently compromised by untrusted code in a workflow; caution on private and internal repositories.)
[^18]: GitHub. [About Actions Runner Controller](https://docs.github.com/en/actions/concepts/runners/about-actions-runner-controller). GitHub Docs. (ARC runners are ephemeral and based on containers, scaling rapidly and cleanly.)
[^19]: GitHub. [Events that trigger workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows). GitHub Docs. (schedule runs on cron; workflow_dispatch enables manual triggering via API, CLI, or UI.)
[^20]: GitHub. [Troubleshooting required status checks](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/troubleshooting-required-status-checks). GitHub Docs. (A workflow skipped by path/branch filtering remains "Pending" and is blocked from merging; a job skipped by a conditional reports "Success"; successful check statuses are success, skipped, and neutral; use always() plus needs for a required check on dependent jobs.)
[^21]: re-actors. [alls-green](https://github.com/re-actors/alls-green). GitHub. (It is mandatory to make the check job run always, not only when all of the needed jobs succeed; Zuul-style single-gate inspiration.)
[^22]: GitHub. [Managing a merge queue](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue). GitHub Docs. (The queue waits for required checks; you must use the merge_group event; Build concurrency is the maximum number of merge_group webhooks, between 1 and 100; only merge non-failing pull requests batch semantics.)
[^23]: GitHub. [Managing environments for deployment](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments). GitHub Docs. (Required reviewers, wait timer, and custom protection rules; on Free/Pro/Team these protection rules are only available for public repositories.)
[^24]: GitHub. [About code owners](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners). GitHub Docs. (Code owners are automatically requested for review; each owner must be a valid user or team with write permission, defined in a CODEOWNERS file.)
[^25]: Fowler, M. (2011). [Eradicating Non-Determinism in Tests](https://martinfowler.com/articles/nonDeterminism.html). martinfowler.com. (Quarantine helps reduce a flaky test's damage to other tests, but you still have to fix them soon.)
[^26]: Micco, J. (2016). [Flaky Tests at Google and How We Mitigate Them](https://testing.googleblog.com/2016/05/flaky-tests-at-google-and-how-we.html). Google Testing Blog. (Almost 16% of tests have some level of flakiness; teams ignore legitimate failures because of high false positives; people ignore alarms when there is a history of false signals.)
[^27]: Google. (2021). [State of Mutation Testing at Google](https://research.google/pubs/state-of-mutation-testing-at-google/). research.google. (Mutation testing assesses test suite efficacy by inserting small faults into programs and measuring the ability of the test suite to detect them.)
[^28]: Vitest. [Improving Performance](https://vitest.dev/guide/improving-performance). Vitest documentation. (Disable parallelism via --no-file-parallelism or test.fileParallelism: false; sequential file execution with per-test timeout isolation.)

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
