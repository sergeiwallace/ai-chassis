---
title: Best Practices for Public-Repo Release-Readiness Auditing and Prep
category: research
tags: [research, release-engineering, security, devsecops, open-source]
status: complete
source: "claude-adhoc-2026-09-01"
template_version: "research-1.2.0"
delegation_provenance:
  version: 2
  contributors: []
---
<!-- Canonical Jinja source: STUB.md.jinja. This direct-copy stub is retained for consumers that have not migrated to rendering it. -->

# Best Practices for Public-Repo Release-Readiness Auditing and Prep

**Status:** complete

**Created:** 2026-09-01

<!-- doc:region name="context" kind="immutable" -->

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. Defining release readiness](#1-defining-release-readiness)
- [2. Security review and repository hygiene](#2-security-review-and-repository-hygiene)
  - [2.1 Layered deterministic review](#21-layered-deterministic-review)
  - [2.2 Secrets, credentials, and personal data](#22-secrets-credentials-and-personal-data)
  - [2.3 History rewriting is incident response](#23-history-rewriting-is-incident-response)
- [3. Python supply-chain hardening](#3-python-supply-chain-hardening)
  - [3.1 Dependency policy](#31-dependency-policy)
  - [3.2 SBOM, provenance, reproducibility, and publishing identity](#32-sbom-provenance-reproducibility-and-publishing-identity)
  - [3.3 PyPI-specific abuse and recovery](#33-pypi-specific-abuse-and-recovery)
- [4. Documentation, changelog, and governance readiness](#4-documentation-changelog-and-governance-readiness)
- [5. Test-suite health and release gates](#5-test-suite-health-and-release-gates)
- [6. Automation and standardized posture tools](#6-automation-and-standardized-posture-tools)
  - [6.1 OpenSSF Scorecard and Allstar](#61-openssf-scorecard-and-allstar)
  - [6.2 Versioning, notes, hooks, and publication](#62-versioning-notes-hooks-and-publication)
- [7. AI-agent-driven audit patterns](#7-ai-agent-driven-audit-patterns)
- [8. Gaps, blindspots & emergent findings](#8-gaps-blindspots--emergent-findings)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
  - [Operating model for `/release-prep`](#operating-model-for-release-prep)
  - [Verdict and policy model](#verdict-and-policy-model)
  - [Delegated-agent topology](#delegated-agent-topology)
  - [Finding and evidence contract](#finding-and-evidence-contract)
  - [Human gates and failure policy](#human-gates-and-failure-policy)
  - [Five-perspective adversarial critic](#five-perspective-adversarial-critic)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Ambiguous Items from Auto-Remediation (Post-Run Review)](#ambiguous-items-from-auto-remediation-post-run-review)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

## Context

This report surveys current release-engineering, DevSecOps, Python-packaging, open-source governance,
and agent-safety practice to provide concrete design input for an autonomous `/release-prep` CLI
skill. The target is a repository approaching a public release, including one whose history may have
been adjacent to private workflows.

The design problem is not merely which scanners to run. It is how heterogeneous and sometimes
incomplete evidence becomes a defensible go/no-go decision without letting a score, tool, or language
model claim more assurance than it provides.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

Primary evidence was weighted 2026 → 2025 → 2024. Continuously maintained official documentation
without a publication date was used only after a successful live fetch on 2026-09-01. Pre-2024
material appears only where foundational, principally Keep a Changelog. Tool claims come from official
documentation; scholarly AI claims come from publisher-hosted papers. No universal post-2024 coverage
or mutation threshold, comprehensive repository-PII standard, or mature end-to-end LLM release
authority was located; those absences are named rather than backfilled. [NO SOURCE]

## Executive Summary

1. **Treat readiness as a state machine, not a score.** Mature projects gate supported behavior,
   persistent failures, exact artifacts, and publication authority. OpenSSF Scorecard documents
   detection limits, so its aggregate score cannot substitute for a release decision.
   [VERIFIABLE][^1][^2] A good overall posture must never suppress an underlying blocker. [INFERENCE]

2. **Run deterministic tools first; use agents to interpret the residue.** Secret scanning, SAST,
   SCA, license checks, tests, builds, metadata validation, SBOM generation, and attestation
   verification should produce machine evidence. Agents add value in cross-file contradiction,
   narrative privacy, release-impact analysis, and false-positive challenge—not as scanner
   replacements. [INFERENCE]

3. **Separate source readiness, artifact readiness, and permission to publish.** PyPA recommends
   separate build and publish jobs, a publish job that only retrieves prebuilt artifacts, Trusted
   Publishing, and manual approval for the production PyPI environment. [VERIFIABLE][^3] The reviewed
   bytes, hashes, SBOM, attestations, and uploaded bytes must be one candidate. [INFERENCE]

4. **Rotate credentials before considering history surgery, and never automate shared-history
   rewriting.** GitHub puts revocation/rotation first and documents changed hashes, invalidated
   signatures, disrupted pull requests, and recontamination risks. [VERIFIABLE][^4] The skill should
   prepare evidence and a plan but never rotate, rewrite, or force-push. [INFERENCE]

5. **Provenance and coverage are bounded signals.** PyPI says an attestation establishes origin, not
   trust; SLSA says reproducible builds do not cover source, dependency, or distribution threats.
   [VERIFIABLE][^5][^6] Coverage.py can enforce a configured number but prescribes no universal ship
   threshold. [VERIFIABLE][^7] Policy must combine independent signals and expose uncertainty.
   [INFERENCE]

## 1. Defining release readiness

Release readiness is a set of explicit claims: promised platforms and behaviors work; package,
documentation, changelog, tag, and version agree; no unwaived blocker remains; the released artifact
is the reviewed artifact; and publication is controlled and recoverable. [HEURISTIC]

The strongest public examples make readiness continuous. pip says its main branch should stay
releasable, while CPython requires supported stable builders to be free of persistent failures when
a release is cut. [VERIFIABLE][^2][^8] A bounded pre-release sweep should therefore diagnose missing
continuous controls rather than normalize a last-minute cleanup ritual. [INFERENCE]

A defensible gate has a named policy source, observable evidence, a verdict, an exception owner, and a
recheck point. [HEURISTIC] “Tool did not run,” “manifest unsupported,” and “dependency graph missing”
are inconclusive, never implicit passes. [HEURISTIC]

## 2. Security review and repository hygiene

### 2.1 Layered deterministic review

No single scanner spans code, dependency, workflow, history, and artifact risk. A practical stack is:

1. **Full-history secret scanning:** hosted GitHub scanning plus a local Gitleaks or TruffleHog pass.
   GitHub documents history-wide scanning, while Gitleaks explicitly scans code “past or present.”
   [VERIFIABLE][^9][^10]
2. **SAST:** CodeQL for repository-wide analysis plus Python-specific Bandit and/or Semgrep rules where
   useful. CodeQL default setup scans supported languages on protected/default-branch pushes,
   qualifying pull requests, and weekly, but excludes fork pull requests. [VERIFIABLE][^11]
3. **SCA and dependency review:** `pip-audit` and/or OSV-Scanner for known vulnerabilities, plus
   dependency-diff review. GitHub’s action fails by default on discovered vulnerable packages and
   needs dependency submission ordered in the same workflow to avoid snapshot races.
   [VERIFIABLE][^12]
4. **Workflow/repository controls:** inspect token permissions, untrusted checkout patterns, branch
   protection, action pinning, review policy, and actual recent CI. Scorecard can surface these, but
   explicitly says low CI/SAST scores may reflect detection limits. [VERIFIABLE][^1]
5. **Artifact inspection:** validate wheel and sdist contents, metadata, entry points, license files,
   SBOM, hashes, clean installation, and CLI/runtime smoke tests. Source-tree success cannot prove
   packaging correctness. [INFERENCE]

The read-only lanes can run mostly in parallel after inventory. High-impact findings need a common
schema and an independent verification pass before blocking release. [INFERENCE]

Tool choice should follow repository and policy coverage, not brand accumulation. Bandit is a
Python-specific checker for common security issues; Semgrep offers code, supply-chain, and secret
analysis and now exposes a workflow framework that can combine deterministic and ISSUE-REF steps.
[VERIFIABLE][^40][^41] Snyk Open Source is a commercial SCA option spanning vulnerability and license
risk, while current TruffleHog distinguishes verified, unknown, and unverified credential results.
[VERIFIABLE][^42][^43] These can replace or complement the baseline tools, but adding overlapping
scanners without a deduplication and disposition policy mostly adds alert volume. [INFERENCE]

### 2.2 Secrets, credentials, and personal data

Secret scanning is narrower than privacy review. Narrative or identifying material can appear in
commit metadata, comments, docs, fixtures, logs, screenshots, examples, absolute paths, internal
hostnames, generated pages, and package archives. [HEURISTIC] Changing Git configuration does not
retroactively replace email addresses on old commits. [VERIFIABLE][^13]

The public-transition playbook is:

1. Inventory the current tree, reachable refs, large files, submodules, LFS objects, prior release
   assets, and generated distributions. [HEURISTIC]
2. Scan current content and full history locally and correlate with hosted secret-scanning results.
   [HEURISTIC]
3. Run contextual passes for people/customer data, private URLs, machine paths, internal names,
   issue excerpts, and non-secret identifiers. [HEURISTIC]
4. For each true secret, record provider, validity, scope, affected objects, and rotation status.
   GitHub validity checks can determine whether supported detected credentials remain exploitable.
   [VERIFIABLE][^14]
5. Reinspect package archives and rendered docs: generation can expose content a source-only grep
   misses. [INFERENCE]

Repeated searches found no authoritative 2024–2026 standard for comprehensive narrative PII review
of a software repository comparable to secret-scanning standards. [NO SOURCE] This is a useful agent
lane, but every finding must carry a repository object, context, and reason; an agent’s “PII” label is
not evidence. [INFERENCE]

### 2.3 History rewriting is incident response

GitHub’s procedure is to rewrite locally with `git-filter-repo`, update the remote, coordinate
affected clones/forks, and prevent recurrence; it warns that force-pushing alone does not necessarily
make old sensitive commits inaccessible. [VERIFIABLE][^4] `git-filter-repo` calls rewriting a
destructive operation that creates replacement objects and removes originals locally.
[VERIFIABLE][^15]

BFG Repo-Cleaner is a valid alternative for replacing passwords/private data or removing troublesome
blobs, but it is still a history-rewrite engine and inherits the same authorization, coordination,
backup, and recontamination concerns. [VERIFIABLE][^44]

```text
Sensitive value found
  -> contain exposure and rotate/revoke credentials
  -> assess residual confidentiality, legal, and privacy harm
  -> if rotation fully mitigates risk: preserve history and document response
  -> if residual harm remains: prepare a reviewed rewrite plan
  -> human authorization + owner coordination + recovery references
  -> rewrite/force-push outside /release-prep
  -> fresh clone and rescan retained refs and published artifacts
```

History surgery is warranted for residual harmful disclosure; it is usually excessive for a revoked
test credential whose text has no continuing confidentiality or privacy impact. [HEURISTIC]

## 3. Python supply-chain hardening

### 3.1 Dependency policy

Applications and release tooling should use reproducible, reviewed dependency closures. pip’s secure
install guidance says hash-checking mode requires every dependency, including transitives, to be
pinned and hashed. [VERIFIABLE][^16] PEP 751 standardized `pylock.toml` in 2025, and OSV-Scanner
currently recognizes `pylock.toml`, `uv.lock`, `pdm.lock`, `poetry.lock`, `Pipfile.lock`,
and requirements files. [VERIFIABLE][^17][^18]

Libraries need a different policy: keep runtime ranges only as narrow as compatibility requires while
pinning and hashing development, test, build, and release environments. [HEURISTIC] Over-pinning
runtime dependencies transfers conflicts to consumers; under-pinning the release toolchain lets
reviewed builds drift. [HEURISTIC]

A clean CVE scan is not proof that a dependency is benign. `pip-audit` says it analyzes dependency
trees rather than source and must not be assumed to defend against malicious packages.
[VERIFIABLE][^19] New or materially changed dependencies require judgment over name similarity,
expected index, ownership changes, maintainer/activity signals, install-time behavior, binaries,
package contents, licenses, and necessity. [INFERENCE]

Block verified exploitable vulnerabilities under project policy, unexpected indexes/sources, lock or
hash mismatches, unresolved dependency confusion, and unreviewed high-risk additions. [HEURISTIC]
Do not block merely because a scanner emitted a severity without showing the package/version is
present and relevant. [HEURISTIC]

### 3.2 SBOM, provenance, reproducibility, and publishing identity

An SBOM inventories components; provenance describes how an artifact was built. Scorecard prefers
SBOMs as release artifacts, and SLSA distinguishes component detail from build provenance.
[VERIFIABLE][^1][^6] They are complementary, not substitutes. [INFERENCE]

For a PyPI release:

- build wheel and sdist once in a dedicated build job;
- inspect and test those exact files;
- produce SPDX and/or CycloneDX inventory associated with each artifact;
- record digests;
- publish through a least-privileged OIDC Trusted Publishing job;
- verify PyPI hashes, attestations, and installation after upload. [INFERENCE]

PyPA separates build from publish, requires production-environment approval, and notes that
`pypa/gh-action-pypi-publish` generates PEP 740 attestations by default. [VERIFIABLE][^3] PyPI
nevertheless cautions that attestations establish origin rather than trust. [VERIFIABLE][^5] Trusted
Publishing removes stored upload tokens but moves the decisive trust boundary to repository,
workflow, environment, tag, and reviewer configuration. [INFERENCE]

Reproducible builds help detect drift but are not comprehensive security gates: SLSA v1.2 says they
do not address source, dependency, or distribution threats, and Build levels impose no transitive
dependency requirements. [VERIFIABLE][^6] Report reproducibility separately from provenance, SBOM
completeness, source controls, and dependency review. [INFERENCE]

When consumers need a separate signature channel, Sigstore Cosign can verify blobs using an identity,
OIDC issuer, and bundle; its bundle can carry the signature, certificate, and transparency-log proof.
[VERIFIABLE][^45] For a normal PyPI package already using PEP 740, extra Cosign material should be
added only when an actual consumer or policy will verify it; redundant signatures without a verifier
increase ceremony rather than assurance. [INFERENCE]

Release-time actions are dependencies too. Full commit-SHA pinning is the strongest immutability
control for third-party GitHub Actions, but pinned actions must be updated promptly through review.
Scorecard documents both the security benefit and staleness tradeoff. [VERIFIABLE][^1]

### 3.3 PyPI-specific abuse and recovery

PyPI requires two-factor authentication and recommends multiple authentication methods, recovery
codes, minimum-scope tokens when needed, and Trusted Publishing where supported.
[VERIFIABLE][^20] A distribution name need not match the import name, so automation must not infer
what to install solely from `import foo`. [VERIFIABLE][^21]

The audit should verify the normalized distribution/import names, index, project URLs, owners,
maintainers, Trusted Publishers, protected tags/environments, minimal workflow permissions, unexpected
executables/build hooks/network access, wheel coverage, and a working sdist where promised.
[HEURISTIC]

PyPI supports yanking as a non-destructive alternative to deletion for broken, incompatible, or
vulnerable releases, but warns that it can disrupt downstream users. [VERIFIABLE][^22] Owners,
credentials, decision criteria, and communication channels for rollback must exist before
publication. [INFERENCE]

## 4. Documentation, changelog, and governance readiness

A public release needs correct content, not just present files. GitHub recognizes README,
CONTRIBUTING, LICENSE, SECURITY, CODE_OF_CONDUCT, support, and issue-template artifacts; SECURITY.md
is specifically for responsible vulnerability-reporting instructions. [VERIFIABLE][^23]

Validate:

- README quick start from a clean install, CLI help, supported versions/platforms, stability, links;
- agreement among package metadata, docs, CLI `--version`, tag, and changelog;
- LICENSE and third-party notices for shipped source, vendored/generated files, examples, and assets;
- CONTRIBUTING setup/test/style/review instructions;
- SECURITY supported versions, private channel, response expectations, and disclosure process;
- governance, maintainership, support boundary, release procedure, and rollback owner;
- prose, examples, fixtures, comments, and generated pages for internal/private narrative.
  [HEURISTIC]

For mixed-origin repositories, file-level licensing can use SPDX identifiers or REUSE 3.3, which
requires valid SPDX expressions in licensing information. [VERIFIABLE][^24] This catches uncertainty
that presence-only top-level license checks miss. [INFERENCE]

Keep a Changelog’s durable value is editorial: maintain an `Unreleased` section and write notable
changes for humans rather than dumping commits. [VERIFIABLE][^25] Generated notes are useful input,
not final narrative. Agents can compare changes, issues/PRs, docs, and compatibility signals to draft
missing entries and migration guidance; maintainers should approve public wording. [INFERENCE]

## 5. Test-suite health and release gates

“Green enough” depends on the support contract. CPython requires Tier 1 and Tier 2 stable builders to
be free of persistent failures at release. [VERIFIABLE][^2] The defensible default is “all promised
environments and release-critical behaviors pass, with every exception explicit,” not “all
conceivable environments pass.” [INFERENCE]

Recommended sequence:

1. Run the repository-owned test command from a clean checkout and record environment, duration,
   selection, skips, xfails, warnings, and failures. [HEURISTIC]
2. Run the supported Python/OS matrix and classify persistent versus flaky failures. Reruns must not
   erase the first failure; pytest warns against permanent quarantine. [VERIFIABLE][^26]
3. Enforce an existing coverage policy and report total/changed-code deltas. Coverage.py provides
   `--fail-under` as a configurable CI mechanism. [VERIFIABLE][^7]
4. Inspect untested release-critical paths: publishing, migrations, destructive commands, auth,
   filesystem/network boundaries, error paths, and installed entry points. [HEURISTIC]
5. Apply mutation testing to changed/high-risk modules under a budget; keep it advisory unless the
   repository already owns a mutation policy. [HEURISTIC]
6. Build wheel and sdist, run `twine check --strict`, inspect file lists/metadata, install each in
   clean environments, and smoke-test `--help`, `--version`, imports, and core commands. PyPA
   recommends exercising the sdist in the release workflow. [VERIFIABLE][^27][^28]

No authoritative post-2024 source found here prescribes a universal line-coverage or mutation-score
release threshold. [NO SOURCE] Respect repository-defined numbers; otherwise report deltas and
uncovered risk rather than inventing “80%.” [INFERENCE]

A quarantine needs an owner, reason, affected capability, expiry, and tracking reference.
[HEURISTIC] If a flaky test covers promised behavior, the release needs a fix, support-scope change,
or explicit waiver—not silent retry-until-green. [INFERENCE]

## 6. Automation and standardized posture tools

### 6.1 OpenSSF Scorecard and Allstar

Scorecard checks branch protection, code review, contributors, CI, dangerous workflows, dependency
updates, fuzzing, licenses, maintenance, packaging, pinned dependencies, SAST, SBOM, security policy,
signed releases, token permissions, and vulnerabilities. [VERIFIABLE][^1] It is a standardized
inventory and regression sensor, not release certification. [INFERENCE]

The project says a low score is not definitive where alternative controls escape detection; its CI
and SAST checks recognize known signals, and its SBOM check primarily verifies existence/location.
[VERIFIABLE][^1] Import per-check evidence and limitations, never only the aggregate. [INFERENCE]

Allstar continuously monitors selected GitHub controls such as branch protection, CODEOWNERS,
outside collaborators, SECURITY.md, and dangerous workflows. Policies are opt-in and their action
mode must be inspected rather than inferred from installation. [VERIFIABLE][^29] Use it for
continuous posture while the release sweep verifies actual execution and candidate coverage.
[INFERENCE]

### 6.2 Versioning, notes, hooks, and publication

- release-please automates version/changelog PRs and GitHub releases but explicitly does not publish
  to package managers. [VERIFIABLE][^30]
- semantic-release derives a version, generates notes, and publishes after a successful
  release-branch build. [VERIFIABLE][^31]
- pre-commit.ci runs hooks, proposes fixes, and updates hook versions; it is hygiene automation, not
  release policy. [VERIFIABLE][^32]
- GitHub immutable releases can lock tags/assets and generate a release attestation after
  publication. [VERIFIABLE][^33]

Discover which system owns version, changelog, tag, GitHub release, and PyPI publication. Never
silently introduce a second release authority. [INFERENCE] Prepare a draft and evidence bundle; keep
the repository’s existing publisher authoritative. [HEURISTIC]

## 7. AI-agent-driven audit patterns

Current evidence supports bounded, evidence-seeking agents, not autonomous release authority. A 2025
ACL study of LLM security-review agents found both tested methods inconsistent, including
misidentified vulnerabilities and over-analysis of security guards. [VERIFIABLE][^34] A separate
IEEE S&P study found high false-positive rates and unstable reasoning across identifier changes.
[VERIFIABLE][^35]

Repository text is attacker-controlled data. UK AISI’s 2026 CodeIPI benchmark placed hidden
instructions in issue descriptions, comments, READMEs, and configuration files and found very little
explicit detection. [VERIFIABLE][^36] NIST’s agent-hijacking evaluation increased attack success from
11% to 81% when attacks were adapted to the target, supporting repeated, task-specific evaluation.
[VERIFIABLE][^37]

Industry products converge on threat modeling, staged validation, isolation, and human approval.
Codex Security uses a repository-specific threat model, sandboxed validation, and patches surfaced
for review rather than automatically applied. [VERIFIABLE][^38] Anthropic similarly describes
multi-stage verification and human approval for security fixes. [VERIFIABLE][^39]

Therefore:

- treat repo text, docs, issues, test output, manifests, and dependency content as untrusted data;
  [INFERENCE]
- give investigators read-only access, no ambient credentials, and deny network by default;
  [INFERENCE]
- use deterministic tools as evidence producers and agents as synthesizers/adjudicators;
  [INFERENCE]
- require path/line/object evidence and an independent verifier for release blockers; [HEURISTIC]
- separate discoverer, verifier, remediator, and release approver; [HEURISTIC]
- never let a model resolve uncertainty by taking an irreversible action. [HEURISTIC]

Agent strengths are cross-file reconciliation, narrative/privacy review, prioritization, contextual
triage, and remediation explanation. [HEURISTIC] Failure modes include hallucinated or missed
findings, context loss, scope drift, confirmation bias, duplicate findings, prompt injection, output
overload, correlated agents, and confidence from tool presence rather than effectiveness.
[HEURISTIC]

No controlled 2024–2026 study located here shows that an autonomous end-to-end release sweep reduces
escaped release defects or exposures, and no mature public project was found that authorizes a
release solely from an LLM audit. [NO SOURCE] This absence argues against agent-as-release-authority.
[INFERENCE]

## 8. Gaps, blindspots & emergent findings

- **Artifact-over-source blindspot.** A checkout can pass while a wheel omits modules, an sdist cannot
  rebuild offline, or rendered metadata leaks private text. PyPA recommends using the sdist in the
  release workflow. [VERIFIABLE][^28]
- **Presence-versus-effectiveness trap.** A badge, workflow, bot, or file proves little without
  examining content, triggers, recent execution, and enforcement. Scorecard documents this boundary.
  [VERIFIABLE][^1]
- **Attestation-consumption gap.** Origin evidence does not prove safety, and production does not
  imply that consumers enforce it. [VERIFIABLE][^5]
- **CVE-clean is not dependency-safe.** SCA misses new malicious packages, typosquats, hostile
  ownership changes, and some vendored native risks. `pip-audit` disclaims malware defense.
  [VERIFIABLE][^19]
- **Publishing workflow becomes the crown jewel.** OIDC removes stored upload tokens but transfers
  trust to triggers, environment protection, tag control, repository ownership, and reviewers.
  [INFERENCE]
- **History cleanup can weaken evidence.** Rewrites can invalidate signatures and disrupt audit
  links; rotation comes first and residual harm must justify rewriting. [VERIFIABLE][^4]
- **Licensing/IP is a release gate.** A top-level license may not cover copied, vendored, generated,
  or binary content. File-level provenance exposes this. [INFERENCE]
- **Support claims define test scope.** Without promised versions, platforms, integrations, and
  stability tiers, an agent cannot know which failures block release. [INFERENCE]
- **Rollback readiness is upstream.** Yanking exists but is disruptive; authority and communication
  need prepublication rehearsal. [VERIFIABLE][^22]
- **The auditor has its own attack surface.** Public repo text can attempt to redirect agents, so
  instruction/data separation, sandboxing, least privilege, and adaptive injection tests are part of
  release-prep security. [VERIFIABLE][^36][^37]
- **Approval fatigue is a blindspot.** A “human in the loop” is weak if routine prompts train
  reflexive approval; containment should handle routine work and humans should decide consequential
  transitions. [INFERENCE]
- **Narrative PII standard unresolved:** no significant post-2024 comprehensive repository standard
  found. [NO SOURCE]
- **Universal numeric quality gates unresolved:** no significant post-2024 consensus found.
  [NO SOURCE]
- **Comparative bundle effectiveness unresolved:** no current controlled study located here shows one
  named tool bundle minimizes escaped release defects. [NO SOURCE]
- **Anchor-bias correction.** Independent framings added licensing provenance, artifact/source parity,
  offline builds, support-tier discovery, rollback, action pinning, prompt injection, and
  evidence-volume control beyond the named checklist. [INFERENCE]

## Comparison

| # | Mechanism | Strongest contribution | Material boundary | `/release-prep` role |
|---:|---|---|---|---|
| 1 | GitHub secret scanning + Gitleaks/TruffleHog | Hosted plus local history scan [VERIFIABLE][^9][^10] | Pattern/hosting coverage; not narrative PII | Run both; contextual privacy pass |
| 2 | CodeQL + Bandit/Semgrep | Semantic and Python/policy SAST [VERIFIABLE][^11] | Language/query/event limits | Tool evidence, agent triage, verifier |
| 3 | `pip-audit` + OSV-Scanner | Known-vulnerability analysis [VERIFIABLE][^18][^19] | Not malware/provenance analysis | Scan closure; review new/high-risk deps |
| 4 | Dependency review/Dependabot | PR dependency change control [VERIFIABLE][^12] | Snapshot/order and response gaps | Verify config, ordering, and diff |
| 5 | Scorecard | Standard posture vocabulary [VERIFIABLE][^1] | Heuristics; no certification | Import per-check evidence |
| 6 | Allstar | Continuous GitHub policy monitoring [VERIFIABLE][^29] | Opt-in and enforcement mode | Maintain controls between releases |
| 7 | SPDX/CycloneDX | Component inventory | Completeness; no build story | Generate for exact candidate |
| 8 | SLSA/Sigstore/PyPI attestations | Origin/build evidence [VERIFIABLE][^5][^6] | Provenance ≠ trust | Bind and verify exact candidate |
| 9 | coverage.py | Measured execution/configured gate [VERIFIABLE][^7] | No universal threshold/effectiveness proof | Honor policy; report delta/risk |
| 10 | flaky/mutation tools | Reliability and test-strength probes [VERIFIABLE][^26] | Reruns hide failure; mutation cost | Preserve first failure; target mutation |
| 11 | release-please | Reviewable version/changelog/release PR [VERIFIABLE][^30] | No registry publication | Preserve as existing authority |
| 12 | semantic-release | Version, notes, tag, publication [VERIFIABLE][^31] | Commit/config discipline | Validate inputs; do not duplicate |
| 13 | pre-commit.ci | Autofix and hook updates [VERIFIABLE][^32] | Hygiene only | Run locally on candidate |
| 14 | PyPA build/Twine/Trusted Publishing | Exact-artifact validation/publication [VERIFIABLE][^3][^27] | No semantic go/no-go | Required artifact lane + human gate |
| 15 | AI reviewer agents | Synthesis, narrative/privacy, contextual triage | Non-determinism, injection, context limits | Bounded read-only review + verification |

## Recommendation

### Operating model for `/release-prep`

```text
0. Discover policy and release authority
   -> repository type, support matrix, version source, publisher, waivers
1. Establish audit identity
   -> commit SHA, refs, tool versions, environment, policy snapshot
2. Run deterministic read-only lanes in parallel
   -> secrets/history | SAST/workflows | dependencies/licenses | tests/docs
3. Run contextual agent lanes
   -> narrative privacy | changelog/API | dependency risk | test sufficiency
4. Verify and reconcile
   -> reproduce high-impact findings; dedupe; preserve inconclusive results
5. Propose reversible remediation
   -> isolated branch/patch plan; rerun affected and dependent gates
6. Build candidate once
   -> wheel + sdist + inventory + metadata + hashes + SBOM
7. Test exact artifacts
   -> clean install; supported matrix; offline build if promised; CLI smoke
8. Produce release dossier
   -> blockers, warnings, waivers, residual risk, provenance, rollback
9. Human go/no-go
   -> approve waivers, history response, notes, exact digests, publication
10. Existing publisher uploads exact bytes
    -> OIDC, protected environment, attestation
11. Post-publication verification
    -> index hashes, install, attestation, tag/assets, monitoring
```

Rebuilding after artifact approval changes evidence identity and must return the run to step 6.
[INFERENCE]

### Verdict and policy model

| Verdict | Meaning | Default consequence |
|---|---|---|
| `PASS` | Required evidence ran and satisfied named policy | Continue |
| `WARN` | Bounded non-blocking debt | Continue; record owner |
| `BLOCK` | Verified policy violation | Stop until fixed or formally waived |
| `NEEDS_HUMAN` | Judgment or authority required | Pause dependent path |
| `INCONCLUSIVE` | Evidence missing, unsupported, timed out, or conflicted | Never coerce to pass |
| `NOT_APPLICABLE` | Policy deliberately excluded, with reason | Continue; preserve reason |

Default blockers should include verified live credentials, workflow privilege escalation,
artifact/hash/provenance mismatch, failing promised-platform tests, invalid package metadata, missing
required licensing, and unreviewed critical dependency findings. [HEURISTIC] Scanner severity is an
input, not a verdict. Waivers need owner, rationale, scope, expiry, compensating control, and approving
identity. [HEURISTIC]

### Delegated-agent topology

1. **Security investigator:** source, SAST, workflows, secrets; read-only.
2. **Supply-chain investigator:** manifests, locks, new dependencies, licenses, SBOM/provenance.
3. **Quality investigator:** tests, flakes, coverage, support matrix, artifact tests.
4. **Documentation/privacy investigator:** public narrative, community health, changelog, metadata,
   identities, internal references.
5. **Adversarial verifier:** receives claims/evidence and attempts to falsify blockers and sample
   passes.
6. **Synthesizer:** maps verified findings to policy and produces the dossier; no release credentials.

Read-only discovery can parallelize. Remediation, artifact construction, approval, and publication
must serialize wherever one step changes the identity reviewed by the next. [INFERENCE]

### Finding and evidence contract

Each finding should contain: stable ID; dimension; affected release claim; severity; verdict;
confidence; commit/ref and artifact digest; path/line/object; command and tool version; concise raw
evidence; policy source; reproduction status; false-positive analysis; reversible action; owner;
waiver/expiry; and verifier identity. [HEURISTIC]

Keep raw outputs outside model context and supply normalized summaries with drill-down handles.
[HEURISTIC] Redact secret values in reports, logs, prompts, and errors; retain only detector ID,
location, provider/type, validity, and a non-secret fingerprint. [HEURISTIC]

### Human gates and failure policy

The skill must not autonomously rotate/revoke credentials; rewrite/force-push history; change
rulesets, PyPI owners, or Trusted Publishers; lower tests/security/review policy; publish/tag/upload/
yank/delete releases; merge remediation; or accept security, privacy, legal, or compatibility
waivers. [HEURISTIC]

Failure handling is fail-explicit, not universally fail-closed: a missing full-history secret scan
before first publication is a block; an unavailable optional mutation service may be inconclusive or
warning under policy. [HEURISTIC] Missing evidence must never masquerade as clean evidence.
[HEURISTIC]

### Five-perspective adversarial critic

1. **Conventional:** Does this duplicate CI? Response: reuse CI, add cross-domain reconciliation and
   exact-artifact validation, and return missing continuous controls as follow-up. **Resolved.**
2. **Contrarian:** Are coverage, Scorecard, SBOMs, signatures, and attestations theater? Response:
   each remains a bounded signal; no aggregate or provenance signal clears unrelated risk.
   **Resolved.**
3. **Historical:** Will history cleanup and generated notes damage auditability? Response: rotation
   precedes rewriting; shared history is human-coordinated; release prose remains editorial.
   **Resolved.**
4. **Adjacent-domain:** What did the prompt omit? Response: licensing, artifact/source parity, offline
   build, support scope, rollback, approval fatigue, and auditor prompt injection became explicit
   gates. **Resolved.**
5. **Skeptical integrator:** What about missing results, competing publishers, uncontrolled
   parallelism, and context overload? Response: add `INCONCLUSIVE`, discover authority first,
   serialize identity-changing steps, and keep raw evidence out of model context. **Resolved.**

No critic identified an unresolved issue that changes the architecture. Universal thresholds,
narrative-PII standards, and empirical tool-bundle effectiveness remain named gaps rather than
assertions. [INFERENCE]

## Open Questions

1. What repository-local policy format should define support, blockers, tools, waivers, artifact
   types, and release authority without duplicating CI?
2. Should first-public-release history scanning cover all reachable refs or only refs to be retained,
   and how are excluded refs proven absent from mirrors/assets?
3. Which personal/private data categories are prohibited, and who approves disclosure?
4. How should maintainer-takeover/typosquat risk be assessed without turning reputation signals into
   accusations?
5. Which SBOM and artifact-binding convention should be mandatory for pure-Python versus native
   releases?
6. What time budget applies to full matrices, flake classification, mutation, and reproducibility?
7. How are waivers signed, expired, and compared across releases?
8. What external state may be inspected read-only—rulesets, secret-scanning, PyPI publishers, prior
   releases—and what happens offline?
9. Who owns post-release verification and yanking?
10. What benchmark measures hallucinated blockers, missed secrets/PII, prompt injection, and
    artifact/source mismatch before this skill is trusted?

## Sources

[^1]: Open Source Security Foundation. (2026). [Scorecard checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md). OpenSSF Scorecard documentation. Verified accessible (HTTP 200) 2026-09-01. (Dimensions and documented detection limits.)

[^2]: Python Software Foundation. (2026). [Buildbots](https://devguide.python.org/buildbots/). Python Developer’s Guide. Verified accessible (HTTP 200) 2026-09-01. (Persistent-failure release criterion.)

[^3]: Python Packaging Authority. (2026). [Publishing package distribution releases using GitHub Actions CI/CD workflows](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/). Python Packaging User Guide. Verified accessible (HTTP 200) 2026-09-01. (Build/publish separation, approval, Trusted Publishing, attestations.)

[^4]: GitHub. (2026). [Removing sensitive data from a repository](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository). GitHub Docs. Verified accessible (HTTP 200) 2026-09-01. (Rotation-first response and rewrite risks.)

[^5]: Python Package Index. (2026). [Digital attestations: security model and considerations](https://docs.pypi.org/attestations/security-model/). PyPI Documentation. Verified accessible (HTTP 200) 2026-09-01. (Origin/trust boundary.)

[^6]: Open Source Security Foundation. (2025). [SLSA v1.2 FAQ](https://slsa.dev/spec/v1.2/faq). SLSA specification. Verified accessible (HTTP 200) 2026-09-01. (SBOM/provenance distinction and build limits.)

[^7]: Coverage.py maintainers. (2025). [Reporting](https://coverage.readthedocs.io/en/7.12.0/commands/cmd_reporting.html). Coverage.py 7.12 documentation. Verified accessible (HTTP 200) 2026-09-01. (Configurable failure threshold.)

[^8]: Python Packaging Authority. (2026). [Release process](https://pip.pypa.io/en/latest/development/release-process/). pip documentation. Verified accessible (HTTP 200) 2026-09-01. (Continuous releasability.)

[^9]: GitHub. (2026). [Secret scanning](https://docs.github.com/en/code-security/concepts/secret-security/secret-scanning). GitHub Docs. Verified accessible (HTTP 200) 2026-09-01. (History-wide hosted scanning.)

[^10]: Gitleaks contributors. (2026). [Gitleaks](https://github.com/gitleaks/gitleaks). GitHub repository documentation. Verified accessible (HTTP 200) 2026-09-01. (Current and historical scanning.)

[^11]: GitHub. (2026). [About setup types for code scanning](https://docs.github.com/en/code-security/concepts/code-scanning/setup-types). GitHub Docs. Verified accessible (HTTP 200) 2026-09-01. (CodeQL triggers and limits.)

[^12]: GitHub. (2026). [Dependency review](https://docs.github.com/en/code-security/concepts/supply-chain-security/dependency-review). GitHub Docs. Verified accessible (HTTP 200) 2026-09-01. (Default failure and ordering.)

[^13]: GitHub. (2026). [Email addresses](https://docs.github.com/en/account-and-profile/concepts/email-addresses). GitHub Docs. Verified accessible (HTTP 200) 2026-09-01. (Historical commit email persistence.)

[^14]: GitHub. (2026). [Validity checks](https://docs.github.com/en/code-security/concepts/secret-security/validity-checks). GitHub Docs. Verified accessible (HTTP 200) 2026-09-01. (Supported-secret activity checks.)

[^15]: Newren, E. (2026). [git-filter-repo manual](https://github.com/newren/git-filter-repo/blob/main/Documentation/git-filter-repo.txt). GitHub repository documentation. Verified accessible (HTTP 200) 2026-09-01. (Destructive rewrite semantics.)

[^16]: Python Packaging Authority. (2026). [Secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/). pip documentation. Verified accessible (HTTP 200) 2026-09-01. (Hash-checking requirements.)

[^17]: Python Packaging Authority. (2025). [PEP 751 — A file format to record Python dependencies for installation reproducibility](https://peps.python.org/pep-0751/). Python Enhancement Proposals. Verified accessible (HTTP 200) 2026-09-01. (Final lockfile standard.)

[^18]: Google. (2026). [Supported artifacts and manifests](https://google.github.io/osv-scanner/supported-languages-and-lockfiles/). OSV-Scanner documentation. Verified accessible (HTTP 200) 2026-09-01. (Supported Python manifests.)

[^19]: Python Packaging Authority. (2026). [pip-audit](https://github.com/pypa/pip-audit). GitHub repository documentation. Verified accessible (HTTP 200) 2026-09-01. (SCA capability and limitations.)

[^20]: Python Package Index. (2026). [Help](https://pypi.org/help/). PyPI. Verified accessible (HTTP 200) 2026-09-01. (2FA, recovery, token scope, Trusted Publishing.)

[^21]: Python Packaging Authority. (2026). [Distribution package vs. import package](https://packaging.python.org/en/latest/discussions/distribution-package-vs-import-package/). Python Packaging User Guide. Verified accessible (HTTP 200) 2026-09-01. (Name-mapping risk.)

[^22]: Python Package Index. (2026). [Yanking](https://docs.pypi.org/project-management/yanking/). PyPI Documentation. Verified accessible (HTTP 200) 2026-09-01. (Recovery behavior.)

[^23]: GitHub. (2026). [Creating a default community health file](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file). GitHub Docs. Verified accessible (HTTP 200) 2026-09-01. (Community-health documents.)

[^24]: Free Software Foundation Europe. (2026). [REUSE Specification 3.3](https://reuse.software/spec-3.3/). REUSE. Verified accessible (HTTP 200) 2026-09-01. (File-level SPDX information.)

[^25]: Dubost, O., & Keep a Changelog contributors. (2019; maintained through 2024). [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/). Verified accessible (HTTP 200) 2026-09-01. (Foundational changelog convention.)

[^26]: pytest developers. (2026). [Flaky tests](https://docs.pytest.org/en/stable/explanation/flaky.html). pytest documentation. Verified accessible (HTTP 200) 2026-09-01. (Rerun/quarantine limits.)

[^27]: Python Packaging Authority. (2026). [Making a PyPI-friendly README](https://packaging.python.org/en/latest/guides/making-a-pypi-friendly-readme/). Python Packaging User Guide. Verified accessible (HTTP 200) 2026-09-01. (`twine check` validation.)

[^28]: Python Packaging Authority. (2026). [Supporting downstream packaging](https://packaging.python.org/en/latest/discussions/downstream-packaging/). Python Packaging User Guide. Verified accessible (HTTP 200) 2026-09-01. (Sdist and offline-build considerations.)

[^29]: Open Source Security Foundation. (2026). [Allstar](https://github.com/ossf/allstar). GitHub repository documentation. Verified accessible (HTTP 200) 2026-09-01. (Continuous policy scope.)

[^30]: Google. (2026). [release-please](https://github.com/googleapis/release-please). GitHub repository documentation. Verified accessible (HTTP 200) 2026-09-01. (Automation boundary.)

[^31]: semantic-release contributors. (2026). [semantic-release](https://github.com/semantic-release/semantic-release). GitHub repository documentation. Verified accessible (HTTP 200) 2026-09-01. (Versioning, notes, publication.)

[^32]: pre-commit.ci. (2026). [pre-commit.ci](https://pre-commit.ci/). Project documentation. Verified accessible (HTTP 200) 2026-09-01. (Autofix and updates.)

[^33]: GitHub. (2025, October 28). [Immutable releases are now generally available](https://github.blog/changelog/2025-10-28-immutable-releases-are-now-generally-available/). GitHub Changelog. Verified accessible (HTTP 200) 2026-09-01. (Tag/asset immutability.)

[^34]: Yildiz, A., Teo, S. G., Lou, Y., Feng, Y., Wang, C., & Divakaran, D. M. (2025). [Benchmarking LLMs and ISSUE-REF agents in practical vulnerability detection for code repositories](https://aclanthology.org/2025.acl-long.1490/). *Proceedings of ACL 2025*, 30848–30865. https://doi.org/10.18653/v1/2025.acl-long.1490. Verified accessible (HTTP 200) 2026-09-01. (Inconsistent agent review.)

[^35]: Ullah, S., et al. (2024). [LLMs cannot reliably identify and reason about security vulnerabilities](https://doi.org/10.1109/SP54263.2024.00210). *2024 IEEE Symposium on Security and Privacy*. OpenAlex metadata and DOI page verified accessible 2026-09-01. (False-positive and reasoning instability.)

[^36]: UK AI Security Institute. (2026). [Inspect CodeIPI: indirect prompt injection in coding agents](https://ukgovernmentbeis.github.io/inspect_evals/evals/ipi_coding_agent/). Inspect Evals. Verified accessible (HTTP 200) 2026-09-01. (Repository-borne indirect injection.)

[^37]: National Institute of Standards and Technology. (2025, January 17). [Strengthening AI agent hijacking evaluations](https://www.nist.gov/news-events/news/2025/01/technical-blog-strengthening-ai-agent-hijacking-evaluations). NIST CAISI. Verified accessible (HTTP 200) 2026-09-01. (Adaptive attack evaluation.)

[^38]: OpenAI. (2026). [Codex Security](https://help.openai.com/en/articles/20001107-codex-security). OpenAI Help Center. Verified accessible (HTTP 200) 2026-09-01. (Threat modeling, sandbox validation, reviewed patches.)

[^39]: Anthropic. (2026, February 20). [Claude Code Security](https://www.anthropic.com/news/claude-code-security?use_case=ea). Anthropic. Verified accessible (HTTP 200) 2026-09-01. (Multi-stage verification and approval.)

[^40]: Bandit maintainers. (2024). [Bandit documentation](https://bandit.readthedocs.io/en/1.8.0/). Verified accessible (HTTP 200) 2026-09-01. (Python-focused common security issue detection; no significant post-2024 scope change found.)

[^41]: Semgrep. (2026). [Workflows overview](https://semgrep.dev/docs/workflows/overview). Semgrep documentation. Verified accessible (HTTP 200) 2026-09-01. (Beta workflow composition across deterministic tools and ISSUE-REF analysis.)

[^42]: Snyk. (2026). [Snyk Open Source](https://docs.snyk.io/scan-with-snyk/snyk-open-source). Snyk User Docs. Verified accessible (HTTP 200) 2026-09-01. (Dependency vulnerability and license analysis.)

[^43]: Truffle Security. (2026). [TruffleHog](https://github.com/trufflesecurity/trufflehog). GitHub repository documentation. Verified accessible (HTTP 200) 2026-09-01. (Credential scanning and verification-result classes.)

[^44]: Tyley, R. (2026). [BFG Repo-Cleaner](https://github.com/rtyley/bfg-repo-cleaner). GitHub repository documentation. Verified accessible (HTTP 200) 2026-09-01. (History cleaning for private data and large blobs.)

[^45]: Sigstore. (2026). [Verifying signatures](https://docs.sigstore.dev/cosign/verifying/verify/). Cosign documentation. Verified accessible (HTTP 200) 2026-09-01. (OIDC identity and bundle-based blob verification.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

None. No auto-remediation was performed; this was a research-only run.

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** unregistered ad-hoc research run (`claude-adhoc-2026-09-01`)

**Model:** Codex GPT-5 (deployment identifier not exposed to the worker)

**Date:** 2026-09-01

```text
## Research goal

Survey industry best practices, standards, open-source tooling/projects, and expert engineering
writing on **how to audit and prepare a public code repository for a new public release** —
covering security review, dependency/supply-chain hygiene, secrets/personal-data scrubbing,
documentation and changelog readiness, test-suite health/coverage gating, and release-automation
tooling. This research will directly inform the design of a new autonomous CLI skill
(`/release-prep`) that runs this entire sweep end-to-end via delegated AI agents, so the output
should be usable as concrete design input, not just a literature summary.

## Scope note — questions, examples, and named references are a starting point, not a checklist
The questions, topics, and named examples below are illustrative anchors and a FLOOR for this
research — not an exhaustive list to answer only or evaluate only. Reason independently: survey the
landscape broadly, follow the evidence where it leads, expand scope where warranted, and surface
relevant work, factors, and failure modes not named here. Actively resist answering only the listed
questions or evaluating only the named approaches — an output that merely fills in the listed items
has NOT met the research goal.

## Questions / topics to cover (floor, not ceiling)

1. **Security review practices for public releases**: what do mature open-source projects and
   security teams actually check before a public release — SAST (e.g. CodeQL, Bandit, Semgrep),
   dependency/SCA scanning (Dependabot, Snyk, OSV-Scanner, pip-audit), secret scanning (gitleaks,
   trufflehog, GitHub secret scanning), SBOM generation (SPDX/CycloneDX), and how these are
   sequenced/gated in CI.
2. **Personal-data / credential scrubbing** for a repo transitioning from private-workflow-adjacent
   development to public release: what's the standard playbook (git-filter-repo / BFG for history
   scrubbing when needed, pre-commit hygiene hooks, hard-coded-secret detection) and what's
   considered best practice vs. overkill.
3. **Supply-chain hardening**: pinned dependencies / lockfiles, provenance attestation (SLSA,
   Sigstore/cosign), reproducible builds, dependency review workflows, typosquat/maintainer-takeover
   risk mitigation for a PyPI package specifically.
4. **Documentation/changelog readiness**: Keep a Changelog / semantic-release conventions,
   README/CONTRIBUTING/SECURITY.md standards, what a "release-readiness checklist" looks like in
   mature OSS projects (e.g. how major Python packaging projects, CNCF projects, or well-regarded
   CLI tools structure their pre-release process).
5. **Test-suite health gating**: coverage thresholds as a release gate, mutation testing, flaky-test
   detection/quarantine, what "green enough to ship" means in practice across different project
   maturity levels.
6. **Automation and tooling landscape**: existing OSS/commercial tools that automate parts of this
   sweep end-to-end (release-please, semantic-release, pre-commit.ci, OpenSSF Scorecard, Allstar,
   GitHub's own release/security features) — what they cover, what they don't, and where an
   AI-agent-driven approach could add value beyond static tooling (judgment calls, narrative
   scrubbing of docs/comments, synthesizing findings across dimensions).
7. **AI-agent-driven code review/audit patterns specifically**: any existing prior art, both
   academic and industry blog/engineering-writing, on using LLM agents for security review,
   release-readiness audits, or repo hygiene sweeps — strengths, known failure modes
   (hallucinated findings, missed context, over/under-triggering), and how serious practitioners
   mitigate those failure modes (multi-pass verification, citation requirements, human-in-the-loop
   gates for irreversible actions).
8. **OpenSSF Scorecard and similar standardized checklists**: what dimensions they score and how
   that maps onto (or diverges from) what a thorough manual pre-release audit would check.

## Independent exploration (gaps, blindspots, emergent threads) — required
Treat the question list as a FLOOR, not a ceiling. As you research, actively surface what this
framing may be missing and pursue each promising thread to a logical conclusion:
- Adjacent or upstream factors the questions don't capture.
- Contrarian / disconfirming evidence — report it even when it challenges the premise.
- Emerging 2025–2026 practices, tools, or research not anchored by the named examples.
- Known failure modes and second-order effects.
Whenever a load-bearing thread surfaces mid-research, follow it to its conclusion and report it in a
dedicated "Gaps, blindspots & emergent findings" subsection. Explicitly NAME any blindspot you
suspect but cannot resolve (and why) rather than omitting it. Anchor bias — over-fitting to the
listed questions and example approaches — is a known failure mode; counter it deliberately and say
where you did.

<grounding_instructions>
You are a principal software engineer and release engineering / DevSecOps specialist who has
personally shipped and maintained widely-used open-source Python CLI tools and packages, run
pre-release security/hygiene sweeps under real deadline pressure, and evaluated both manual and
automated approaches to release-readiness auditing. You have strong opinions backed by evidence
about what actually catches real problems versus what's theater. When you cannot find a source,
you say so explicitly rather than waving past it.

Temporal scope: Weight sources by recency — 2026 (primary) → 2025 → 2024. Pre-2024 sources are
background context only unless foundational to the topic (e.g. Keep a Changelog's original
convention, SLSA's foundational spec). If post-2024 literature is genuinely sparse for a
subtopic, state "[subtopic]: no significant post-2024 developments found" rather than
backfilling with older sources. Backfilling is a failure mode, not a hedge.

Before generating your final output, execute a Chain-of-Verification (CoVe) to ensure factual
fidelity over compliance.

Inside your thought process:
1. Isolate the core facts required.
2. Draft a tentative response.
3. Hostile Cross-Examination: flag any claim where you are citing a source because the prompt
   implied you should, rather than because you verified it.
4. Strip away any claim that cannot be empirically verified.

When generating your final output, classify every major claim. Write your rationale before
appending the tag — writing the tag first causes post-hoc rationalization. Rationale → evidence
check → tag.

- [VERIFIABLE]: backed by documentation, peer-reviewed research, or official tech blogs
  (2024–2026). Carry an inline footnote ref to the source: [VERIFIABLE][^N].
- [HEURISTIC]: widely accepted best practice without a specific citation.
- [INFERENCE]: a logical conclusion drawn from context. Provide your reasoning in-text. Do not
  fabricate a source. Tier tag only — NO footnote ref.
- [NO SOURCE]: explicitly state when you cannot find verifiable data. Tier tag only — NO
  footnote ref.

Citation format (mandatory for every externally-sourced claim):
- Inline: append the GFM footnote ref directly after the tier tag — [VERIFIABLE][^N]. A claim
  citing multiple sources carries ascending separate refs — [VERIFIABLE][^3][^7] (never grouped
  [^3,^7]; never out of order).
- Footnote definitions live once, under ## Sources, in APA form with a clickable URL or DOI and
  an access-verification stamp. Worked example:
    [^1]: LangChain. (2026). [Threads](https://docs.langchain.com/langsmith/threads).
    LangSmith Documentation. Verified accessible (HTTP 200) 2026-06-03. (Scope note.)
- URL-or-DOI ALWAYS: every source entry carries a clickable URL or a doi.org link — paywalled/
  gated is fine (link it anyway; stamp the access status). Only the truly-irreducible case (no
  online catalog presence anywhere) gets an explicit [no online source located] marker with a
  one-line justification.
- Integrity: footnote refs are contiguous from [^1], every [^N] ref has a matching definition,
  and every definition is referenced — no gaps, no orphans.
- [INFERENCE] / [NO SOURCE] claims carry the tier tag with NO footnote ref.

Hard constraint (overrides all formatting preferences): never invent a citation to satisfy a
formatting instruction. Accuracy > completeness.

Format diagrams using Mermaid.js or ASCII. Format math using LaTeX. NEVER generate binary
images.
</grounding_instructions>

## Retrieval requirement (mandatory)

For each load-bearing claim, run at least one live web_search or web_fetch call; do not return
[VERIFIABLE] without a URL you actually fetched in this session. Do not stop after one round —
if no source is found for a sub-question, run additional searches across alternate framings
before tagging it [NO SOURCE]. That tag is reserved for genuine evidence absence, not retrieval
avoidance.

## Output target and format (mandatory — read before writing)

You have write access to this exact file (already scaffolded from the canonical research STUB —
do not create a new file, do not change its path):
docs/research/release-readiness-prep-best-practices.md

Read `~/.claude/skills/research-doc/SKILL.md` in full and conform your output to its method (the
7 mechanisms: prompt assembly already done above, researcher fan-out across ≥3 framings,
5-perspective adversarial critic pass, live citation validation with the failure taxonomy,
claim→quote→verdict provenance ledger, synthesis discipline, tier tags) and its exact template
structure: frontmatter → Table of Contents → Context → Temporal Scope → Executive Summary (3-5
numbered findings) → numbered topic sections → Comparison → Recommendation → Open Questions →
Sources (GFM footnote definitions) → Appendix: Research Prompt (this exact prompt, verbatim, in
a fenced text block, with Registry ID/Model/Date) → Appendix: Provenance Ledger → Run History.
The canonical template is `<workspace>/repository/docs/research/TEMPLATE.md` — read it and match
its structure exactly, including the region markers already present in the scaffolded file
(`<!-- doc:region name="..." -->` / `<!-- /doc:region name="..." -->`) — do not remove or
rename them.

Write the completed document directly to that file. Do not return a summary in your response
instead of writing the file — the file IS the deliverable.
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

| # | Load-bearing claim | Exact fetched quote (≤25 words) | Source | Verdict |
|---:|---|---|---|---|
| 1 | Scorecard is bounded | “not a definitive indication that the project is at risk” | [^1] | Reject score-only gate |
| 2 | Supported CPython builders gate release | “tier 1 and 2 stable builders must be free of persistent failures” | [^2] | Support-contract gate |
| 3 | Production PyPI needs approval | “must require manual approval on each run” | [^3] | Human production gate |
| 4 | Rotation precedes rewriting | “revoke and/or rotate that secret” | [^4] | Rotation-first response |
| 5 | Attestation does not prove safety | “where a PyPI package came from, but not whether you should trust it” | [^5] | Provenance ≠ trust |
| 6 | Reproducibility is bounded | “do not address source, dependency, or distribution threats” | [^6] | Separate gates required |
| 7 | Coverage threshold is configurable | “exit with a status code of 2” | [^7] | Capability, no universal value |
| 8 | pip favors continuous readiness | “main is always in a releasable state” | [^8] | Continuous controls |
| 9 | Hosted scan covers history | “entire Git history on all branches” | [^9] | Full-history requirement |
| 10 | Gitleaks covers past code | “past or present” | [^10] | Local complementary scan |
| 11 | CodeQL has event boundaries | “excluding pull requests from forks” | [^11] | Inspect actual coverage |
| 12 | Dependency review is ordered | “same GitHub Actions workflow” | [^12] | Missing snapshot inconclusive |
| 13 | Commit identity persists | “still associated with your previous email address” | [^13] | Metadata privacy review |
| 14 | Secret validity is testable | “still active and could be exploited” | [^14] | Response priority evidence |
| 15 | Rewrite is destructive | “should not be used lightly” | [^15] | Human authorization |
| 16 | Hash mode covers closure | “all requirements are specified as a `==` match” | [^16] | Controlled-environment policy |
| 17 | Standard Python lock exists | “installation reproducibility” | [^17] | Recognize `pylock.toml` |
| 18 | OSV accepts custom locks | “Custom Lockfiles” | [^18] | Unsupported ≠ silent skip |
| 19 | SCA is not code analysis | “analyzes dependency trees, not code” | [^19] | Add dependency judgment |
| 20 | PyPI requires 2FA | “Two-factor authentication is required” | [^20] | Harden publisher accounts |
| 21 | Imports do not identify distributions | “cannot blindly install” | [^21] | Explicit name/source review |
| 22 | Yanking is non-destructive | “alternative to deletion” | [^22] | Preplan rollback |
| 23 | SECURITY.md guides reporting | “responsibly report a security vulnerability” | [^23] | Validate content |
| 24 | REUSE uses SPDX expressions | “valid SPDX License Expression” | [^24] | File-level provenance |
| 25 | Changelogs serve humans | “Changelogs are for humans, not machines.” | [^25] | Reject commit dumps |
| 26 | Permanent quarantine is risky | “dangerous to use permanently” | [^26] | Owner and expiry |
| 27 | Twine checks built files | “Run `twine check` on the sdist and wheel” | [^27] | Artifact metadata gate |
| 28 | Test the sdist | “use your source distribution in the release workflow” | [^28] | Artifact/source parity |
| 29 | Allstar monitors continuously | “continuously monitors GitHub organizations or repositories” | [^29] | Complements sweep |
| 30 | release-please does not publish packages | “does not handle publication to package managers” | [^30] | Automation boundary |
| 31 | semantic-release publishes | “publishing the package” | [^31] | Discover authority |
| 32 | pre-commit.ci updates hooks | “periodically autoupdate your configuration” | [^32] | Hygiene only |
| 33 | Immutable releases lock state | “cannot be changed after publication” | [^33] | Finalize before publish |
| 34 | Agent review is inconsistent | “Both methods show inconsistencies” | [^34] | Independent verification |
| 35 | LLM security reasoning is unreliable | “high false positive rate” | [^35] | Reproduce blockers |
| 36 | Repo text carries injections | “issue descriptions, code comments, README files, and configuration files” | [^36] | Treat content as data |
| 37 | Adaptive attacks matter | “from 11% ... to 81%” | [^37] | Repeated red-team tests |
| 38 | Security patches stay reviewable | “does not automatically modify your code” | [^38] | Proposal-only authority |
| 39 | Product security uses staged verification | “multi-stage verification process” | [^39] | Separate discovery/verification |
| 40 | Bandit is Python-focused SAST | “find common security issues in Python code” | [^40] | Optional Python-specific lane |
| 41 | Semgrep composes hybrid workflows | “deterministic tools ... as well as Semgrep Multimodal” | [^41] | Alternative orchestration component |
| 42 | Snyk combines SCA and license checks | “vulnerabilities and license risks” | [^42] | Commercial alternative |
| 43 | TruffleHog distinguishes result certainty | “verified ... unknown ... unverified” | [^43] | Preserve validity state |
| 44 | BFG rewrites private data | “Removing Passwords, Credentials & other Private data” | [^44] | Same human gate as filter-repo |
| 45 | Cosign verifies blob bundles | “contains the certificate and signature” | [^45] | Optional consumer-verifiable signature |

Validation notes: all sources were fetched live by the primary run or one of its three delegated
research framings. Scholarly metadata for [^35] was cross-checked through OpenAlex; [^34] was checked
against the ACL publisher page and DOI after Crossref returned a transient internal error. An
official CISA SBOM PDF returned HTTP 403 and was excluded from load-bearing claims. No inaccessible
or search-snippet-only source supports an externally sourced major claim.

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

- **2026-09-01 — Initial synthesis.** Canonical `research-doc` method; three delegated framings
  (security/supply chain, release quality/automation, AI audit prior art); live primary-source
  retrieval; claim→quote→verdict ledger; five-perspective adversarial critic; hostile citation
  cross-examination. The optional prompt resolver could not write its package cache under the
  sandbox, so normalization was performed manually. No repository file other than this designated
  target was modified.

<!-- /doc:region name="run_history" -->
