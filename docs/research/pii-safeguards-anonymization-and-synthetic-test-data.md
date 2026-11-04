---
title: "PII Safeguards for Test Data: Anonymization, Synthetic Generation, Secure Storage, and Automated Prevention"
category: research
tags: [research, pii, privacy, anonymization, pseudonymization, synthetic-data, test-fixtures, secret-scanning, git-hooks, referential-integrity, gdpr, data-governance, history-rewriting]
status: complete
source: "claude-adhoc-2026-08-05"
template_version: "research-1.2.0"
---

# PII Safeguards for Test Data: Anonymization, Synthetic Generation, Secure Storage, and Automated Prevention

**Status:** complete

**Created:** 2026-08-05

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. Anonymization vs Pseudonymization vs Redaction vs Tokenization](#1-anonymization-vs-pseudonymization-vs-redaction-vs-tokenization)
  - [The four operations are not interchangeable](#the-four-operations-are-not-interchangeable)
  - [What a test corpus actually needs](#what-a-test-corpus-actually-needs)
  - [When to use](#when-to-use)
- [2. Why Naive Masking Fails](#2-why-naive-masking-fails)
  - [Referential integrity is the failure that breaks builds](#referential-integrity-is-the-failure-that-breaks-builds)
  - [Format preservation and the FF1/FF3 story](#format-preservation-and-the-ff1ff3-story)
  - [Re-identification and linkage attacks](#re-identification-and-linkage-attacks)
  - [k-anonymity, l-diversity, t-closeness and their limits](#k-anonymity-l-diversity-t-closeness-and-their-limits)
  - [Differential privacy: where it applies and where it is cargo cult](#differential-privacy-where-it-applies-and-where-it-is-cargo-cult)
  - [When to use](#when-to-use-1)
- [3. Synthetic Data Generation](#3-synthetic-data-generation)
  - [The tooling landscape as of August 2026](#the-tooling-landscape-as-of-august-2026)
  - [Silent coverage loss is the named failure mode](#silent-coverage-loss-is-the-named-failure-mode)
  - [Property-based generation is the underrated option](#property-based-generation-is-the-underrated-option)
  - [When to use](#when-to-use-2)
- [4. Detection and Prevention Automation](#4-detection-and-prevention-automation)
  - [Two different problem classes](#two-different-problem-classes)
  - [Where each control actually binds](#where-each-control-actually-binds)
  - [False-positive economics](#false-positive-economics)
  - [When to use](#when-to-use-3)
- [5. Secure Storage of the Real Data](#5-secure-storage-of-the-real-data)
  - [The three patterns, named](#the-three-patterns-named)
  - [Bucket posture](#bucket-posture)
  - [When to use](#when-to-use-4)
- [6. Remediation of Data Already in History](#6-remediation-of-data-already-in-history)
  - [The tooling](#the-tooling)
  - [What a rewrite does not achieve](#what-a-rewrite-does-not-achieve)
  - [When rewriting is worth it](#when-rewriting-is-worth-it)
  - [When to use](#when-to-use-5)
- [7. Governance and Classification](#7-governance-and-classification)
  - [Deciding what counts as PII](#deciding-what-counts-as-pii)
  - [Recording the decision so it is not re-litigated](#recording-the-decision-so-it-is-not-re-litigated)
  - [When to use](#when-to-use-6)
- [8. Gaps, Blindspots and Emergent Findings](#8-gaps-blindspots-and-emergent-findings)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

<!-- doc:region name="context" kind="immutable" -->

## Context

A team committed real employee usernames into a vendored regression corpus and pushed
them to shared branches. The one-off cleanup is easy. What the team lacked was an
algorithmic, repeatable approach, so the next corpus vendored in would hit the same wall.
This research asks what the industry and open-source landscape actually offer for:
choosing the right de-identification operation, keeping a multi-file fixture corpus
internally consistent after transformation, generating synthetic fixtures without
silently losing regression coverage, automating detection and prevention so PII cannot
reach a repository again, storing the real data somewhere that is not the repository,
remediating data already in history, and recording the classification decision once
rather than per corpus.

The motivating corpus has a shape that matters throughout: a CSV and a derived
RDF/Turtle file that must stay in lockstep, because a parity test compares them. Any
transformation applied to one must be applied identically to the other, and join keys
must survive across files. This is the constraint most masking tools fail.

**Primary period:** 2024-2026
**Source weighting:** 2026 primary, 2025 secondary, 2024 tertiary. Pre-2024 sources are
cited only where foundational (privacy-model papers, regulatory instruments, NIST
cryptographic recommendations), and are stamped with their publication year so the reader
can judge staleness.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

Tool maintenance status, licenses, and version-specific behavior were checked live on
2026-08-05 rather than recalled, because that is precisely the class of fact that goes
stale. Four checks changed the recommendation materially: one widely-cited tool is
archived, one flagship library is not OSI-open-source, one leading library changed
organizational owner, and one scanner is now security-patches-only with its maintainer
moved to a successor project. See sections 3 and 4 and the Comparison table.

Regulatory and cryptographic sources are older by nature and are cited with their year:
the EU General Data Protection Regulation (2016), the Article 29 Working Party opinion on
anonymisation techniques (2014), the k-anonymity family of papers (2006-2007), and the
NIST format-preserving-encryption recommendation (2016, with a 2025 second public draft).
These are current instruments, not backfill: no post-2024 source supersedes them, and the
NIST draft is itself a 2025 document.

Genuine thinness to flag: **RDF/graph-native test-fixture anonymization has no
significant post-2024 tooling development that this research could locate.** The graph
anonymization literature that does exist targets social-network topology (degree
sequences, betweenness centrality, community structure), not semantic-web triple stores
with IRIs as identifiers. Section 8 states this as a blindspot rather than filling it with
adjacent material.

## Executive Summary

1. **A test corpus almost never needs anonymization in the legal sense; it needs
   deterministic pseudonymization with a discarded key.** Anonymization under the GDPR
   framing is irreversible de-identification, and the Article 29 Working Party states flatly
   that "pseudonymisation is not a method of anonymisation" [VERIFIABLE][^1]. But the
   regulation's own test is whether identification is reasonably likely by any means
   [VERIFIABLE][^2] - so a fixture whose real values were replaced by generated ones and
   whose mapping table was destroyed is anonymous in practice, while keeping the mapping
   makes it pseudonymous personal data. **The key-retention decision, not the algorithm, is
   what determines which regime you are in.** Retain the key only if you must trace a fixture
   row back to a production record; for a regression corpus you almost never must.

2. **Referential integrity across files is the crux, and only a handful of tools address
   it - none of them for the multi-format case.** The generic mechanism that works is a
   **deterministic keyed transform**: same input plus same key yields same output, applied
   independently to every file. Greenmask implements this as a hash engine and states "This
   ensures that the same input data will always produce the same output data"
   [VERIFIABLE][^3], but it is PostgreSQL-only [VERIFIABLE][^3]. Presidio can do it but
   only if you supply a stable salt yourself: as of v2.2.361 its hashing is randomly
   salted, so the "Same PII value gets different hashes across different entities and
   calls (unless you provide a salt)," and "If you need referential integrity (same values
   getting the same hash), you must provide a consistent salt" [VERIFIABLE][^4]. **For a
   CSV-plus-derived-serialization corpus, no surveyed off-the-shelf tool handles the
   cross-format case; the correct answer is a ~50-line deterministic mapping applied by
   your own generator, with the parity test as the integrity check.**

3. **Synthetic data is not a privacy free lunch, and the strongest evidence against it is
   empirical.** The USENIX-published "Synthetic Data - Anonymisation Groundhog Day" study
   found that across a broad set of generative models, "synthetic data either does not
   prevent inference attacks or does not retain data utility," and concluded it "does not
   provide a better tradeoff between privacy and utility than traditional anonymisation
   techniques," with a privacy gain that is "hard to predict" [VERIFIABLE][^5]. For test
   fixtures there is a second, sharper cost: **a synthetic corpus that no longer exercises
   the same code paths is a silent loss of regression coverage** - the suite stays green
   while testing less. Statistical fidelity metrics do not detect this; only structural
   assertions and coverage comparison do.

4. **The only prevention control that actually binds is server-side.** Local git hooks are
   bypassable by design: the pre-commit hook "can be bypassed with the `--no-verify`
   option" [VERIFIABLE][^6], and for push, "With `--no-verify`, the hook is bypassed
   completely" [VERIFIABLE][^7]. A local hook is a fast-feedback convenience, not a
   control. What binds is a **required CI status check on a branch ruleset**, where "all
   required status checks must pass before collaborators can merge changes into the branch"
   [VERIFIABLE][^8], combined with restricting direct pushes so "only users with bypass
   permissions can push to branches" matching the pattern [VERIFIABLE][^8]. Platform push
   protection is a useful additional layer but is credential-oriented and explicitly
   bypassable by anyone with write access "by specifying a bypass reason"
   [VERIFIABLE][^9].

5. **On a hosted platform, a history rewrite does not by itself achieve deletion, and the
   honest default for low-sensitivity PII is remediate-forward.** The platform's own
   documentation states that "If you only rewrite your history and force push it, the
   commits with sensitive data may still be accessible elsewhere" - via forks, cached views
   addressable by SHA, and pull requests - and that fully removing them requires contacting
   support [VERIFIABLE][^10]. For a credential the guidance is to rotate first, after which
   "Going through the extra steps to rewrite the history and remove the secret may not be
   warranted" [VERIFIABLE][^10]. **A username is not rotatable, so that escape hatch does
   not apply** - which is exactly why the decision has to be made on sensitivity and
   exposure rather than by reflex. See section 6 for the decision rule.

## 1. Anonymization vs Pseudonymization vs Redaction vs Tokenization

### The four operations are not interchangeable

The distinction is legally load-bearing, not pedantic, and the primary sources are
unusually clear about it.

**Anonymization** is irreversible de-identification. The Article 29 Working Party opinion
frames it as processing "in order to irreversibly prevent identification"
[VERIFIABLE][^1]. The regulation's own standard is contextual rather than absolute:
identifiability is assessed against "all the means reasonably likely to be used, such as
singling out," weighing "the costs of and the amount of time required for identification"
and "the available technology at the time of the processing and technological
developments" [VERIFIABLE][^2]. Two consequences follow. First, anonymity is a moving
target - what is anonymous today may not be in five years. Second, the same opinion warns
that "an anonymised dataset can still present residual risks to data subjects" because
"anonymisation and re-identification are active fields of research" [VERIFIABLE][^1].
There is no certificate of anonymity.

**Pseudonymization** replaces an identifier with a surrogate while retaining, somewhere,
the ability to reverse it. The regulation defines it as processing such that data "can no
longer be attributed to a specific data subject" without additional information kept
separately and protected [VERIFIABLE][^11]. The Working Party is blunt about the common
error: pseudonymization "consists of replacing one attribute (typically a unique
attribute) in a record by another. The natural person is therefore still likely to be
identified indirectly; accordingly, pseudonymisation when used alone will not result in an
anonymous dataset," and it "is a useful security measure but not a method of
anonymisation" [VERIFIABLE][^1]. Crucially it names encryption-with-secret-key as a
pseudonymization technique, since "the holder of the key can trivially re-identify each
data subject through decryption" [VERIFIABLE][^1] - encrypting a column does not
anonymize it.

**Redaction** removes the value rather than substituting one. Presidio implements this as
a distinct operator: `redact` "Remove the PII completely from text," as against `replace`
"Replace the PII with desired value" [VERIFIABLE][^4]. Redaction is the safest operation
and the most destructive to test utility: it changes field width, may change field count,
and typically breaks any parser or schema that expected a well-formed value. For a test
fixture it is usually the wrong tool, because the thing under test is often the parsing.

**Tokenization** substitutes a surrogate that carries no algorithmic relationship to the
original, with the mapping held in a separate vault. Functionally it is pseudonymization
with the reversal capability centralized and access-controlled rather than embedded in a
key. The practically relevant variant for fixtures is **format-preserving encryption**,
which produces a surrogate in the same domain as the input - see section 2.

The operation that most fixture work actually needs sits between these labels:
**deterministic keyed substitution followed by key destruction**. During generation it
behaves like pseudonymization (so cross-file consistency is achievable); after the key is
discarded it behaves like anonymization (no one can reverse it). This is why the
key-retention decision is the real decision.

### What a test corpus actually needs

Work the question backwards from what the corpus is for. A regression corpus exists to
pin behavior. It needs values that are structurally valid, internally consistent, and
stable across runs. It does not need to be traceable to production.

That yields a default: **irreversible in effect, deterministic in mechanism.** Generate
the substitutions deterministically so a CSV and its derived serialization agree, commit
the generator and the substituted corpus, and do not commit or retain the mapping. If a
future corpus refresh needs the same substitutions, re-derive them from the same key held
in a secret manager - which is a deliberate, reviewable act, not a file sitting in the
repository.

Reversible pseudonymization is the right answer in a narrower set of cases: when a test
failure must be diagnosed against the production record that produced it, when a data
subject's deletion request must be propagated into derived datasets, or when a regulated
audit trail requires demonstrable linkage. If none of those apply - and for a regression
corpus they usually do not - retaining the key adds a liability with no offsetting
benefit, because the retained key makes the corpus personal data under the regulation's
own definition [VERIFIABLE][^11].

### When to use

| Situation | Operation |
|---|---|
| Regression corpus, no traceability requirement | Deterministic substitution, key discarded |
| Corpus must be refreshed from source periodically with stable values | Deterministic substitution, key in a secret manager, never in the repo |
| Failure triage must reach the production record | Pseudonymization with a vault; the corpus stays personal data and stays out of the repo |
| The field's content is irrelevant to the test | Redaction, if and only if the parser tolerates it |
| The field's format is load-bearing and the domain is large | Format-preserving encryption (see section 2) |
| Free-text field of unknown content | Detection-then-replace with a scanner, accepting recall limits (see section 4) |

## 2. Why Naive Masking Fails

### Referential integrity is the failure that breaks builds

Naive masking treats each value independently. Randomize a username in the CSV and
randomize it again in the derived Turtle file and you get two different values, the join
breaks, and a parity test fails. Worse, if the same identifier appears twice within one
file, independent randomization destroys the equality relationship that the code under
test may depend on.

The mechanism that solves this is a deterministic keyed transform. Greenmask is the
clearest primary-source statement of the property: it offers "deterministic approach to
data transformation based on the hash functions," and "This ensures that the same input
data will always produce the same output data," with "Almost each transformer" supporting
either a `random` or `hash` engine [VERIFIABLE][^3]. Its scope is the constraint: the
project describes itself as a "PostgreSQL database anonymization and synthetic data
generation tool," and only PostgreSQL is named [VERIFIABLE][^3]. For a file-based
multi-format corpus it does not apply.

Greenmask's subsetting documentation is nonetheless worth reading as a specification of
what full relational integrity requires, because it enumerates the hard parts honestly.
It "builds a table dependency graph based on the introspected schema and existing foreign
keys," generates `LEFT JOIN` queries with integrity conditions for nullable foreign keys,
and handles cycles with recursive queries [VERIFIABLE][^12]. It also states its
limitations: it "does not automatically exclude records with NULL values," it cannot
resolve a strongly connected component containing two groups of vertices, and implicit
references (foreign keys dropped, or living inside JSON) require declaring "virtual
references in the configuration" [VERIFIABLE][^12]. **That last item is the general
lesson: a tool can only preserve the relationships it can see.** A CSV-to-Turtle
derivation relationship is invisible to every schema-introspecting tool, which is why the
cross-format case has no off-the-shelf answer.

Presidio can be made to satisfy cross-file consistency, but only deliberately. Its hash
operator is randomly salted by default as of v2.2.361, so the same value hashes
differently across calls, and the documentation states the requirement explicitly: "If
you need referential integrity (same values getting the same hash), you must provide a
consistent salt" [VERIFIABLE][^4]. It also states that "Presidio does not store or
maintain stateful sessions" [VERIFIABLE][^4] - consistency is entirely the caller's
responsibility. **This is a trap worth naming: a team that adopts Presidio's default hash
operator for a multi-file corpus gets a corpus that looks anonymized and is silently
internally inconsistent.**

Faker deserves a specific caution here. It is the obvious tool for generating replacement
values and it is well maintained (MIT, last push 2026-08-03 [VERIFIABLE][^13]), but its
determinism guarantee is version-scoped: "A Seed produces the same result when the same
methods with the same version of faker are called," and "results are not guaranteed to be
consistent across patch versions," with advice to pin the patch version if results are
hardcoded in tests [VERIFIABLE][^14]. **A seeded-Faker corpus regenerated after a
dependency bump can silently change every value.** Its `.unique` proxy is also not a
collision-free guarantee - the docs warn that per the birthday paradox "collisions are
more likely than you'd think" and that Faker raises `UniquenessException` after a number
of failed attempts [VERIFIABLE][^14]. For a fixture corpus the safe pattern is to
generate values once, commit them, and treat the mapping as data rather than
regenerating on every run.

### Format preservation and the FF1/FF3 story

When a masked value must remain the same shape as the original (a 9-digit identifier
staying 9 digits and passing a checksum), format-preserving encryption is the standardized
answer, and its history is a useful caution about invoking cryptography casually.

NIST SP 800-38G (2016) originally specified two FPE modes, FF1 and FF3 [VERIFIABLE][^15].
Both were then attacked when the input domain was small: researchers "identified
vulnerabilities when the number of possible inputs" was sufficiently limited
[VERIFIABLE][^16]. The 2019 initial public draft revision renamed the fixed variant
("The revised FF3 is named FF3-1") and hardened the domain-size rule from a
recommendation to a requirement - "the minimum domain size for FF1 and FF3-1 in Draft SP
800-38G Revision 1 is one million" [VERIFIABLE][^16]. The 2025 second public draft went
further and dropped the mode entirely: among its main technical changes, "The encryption
method FF3 is no longer specified," attributed to a tweak-schedule weakness
[VERIFIABLE][^17].

Two lessons for test data. First, **FPE over a small domain is broken, not merely
weaker** - masking a two-letter country code or a boolean-ish field with FPE provides
close to no protection, because the attacker can enumerate the domain. Second, as of
August 2026 the revision is still a draft with a closed comment period, so the currently
final standard and the currently recommended practice differ; a team invoking "NIST-
approved FPE" should say which document it means. For most test fixtures FPE is
over-engineering: a deterministic keyed substitution into a generated-value pool gives
format validity without a cryptographic claim you then have to defend.

### Re-identification and linkage attacks

The threat that makes naive masking insufficient is linkage: an attacker joins the masked
dataset against an auxiliary dataset on quasi-identifiers that were left alone. The
canonical demonstration is Narayanan and Shmatikov's "Robust De-anonymization of Large
Sparse Datasets" (IEEE S&P 2008) [VERIFIABLE][^18], which established that
high-dimensional sparse data is re-identifiable from partial, noisy auxiliary knowledge -
removing the direct identifier is not enough when the remaining attributes are nearly
unique per person.

For a code-repository test corpus, the practical threat model is narrower and worth
stating plainly rather than importing the full microdata threat model. The exposure is
usually not a sophisticated linkage attack; it is that a real identifier is now in a
place with broad read access, indexed, cloned, and mirrored, and it associates a named
person with a system, a role, or an activity. That is a disclosure harm and a compliance
problem at a scale where k-anonymity machinery is not the relevant tool. **Invoking
formal privacy models for a fixture corpus is usually the wrong register - but the
linkage literature still matters, because it is why you must transform quasi-identifiers
(free-text comments, timestamps, org paths) and not only the obvious identifier column.**

### k-anonymity, l-diversity, t-closeness and their limits

The formal privacy-model family exists for statistical microdata release, and each member
was published to fix a defect in the previous one:

- **k-anonymity** requires each record to be indistinguishable from at least k-1 others
  on the quasi-identifiers, forming equivalence classes [VERIFIABLE][^19]. Its defect is
  that it constrains identity disclosure but not attribute disclosure.
- **l-diversity** (ICDE 2006) [VERIFIABLE][^20] addresses that by requiring each
  equivalence class to contain at least l "well represented" values of the sensitive
  attribute [VERIFIABLE][^19]. Its defect is that l-diverse values can still be
  skewed relative to the overall distribution.
- **t-closeness** (ICDE 2007) [VERIFIABLE][^21] addresses that by requiring the
  distribution of the sensitive attribute within each class to be within distance t of the
  overall distribution, measured by earth mover's distance [VERIFIABLE][^19].

ARX documents this family, plus delta-disclosure privacy (a "multiplicative definition
which is stricter than the definition used by t-closeness"), beta-likeness (which "aims to
overcome limitations of prior models"), delta-presence for membership disclosure, and
differential privacy [VERIFIABLE][^19]. **The chain of successive fixes is itself the
finding: each model was proposed because the prior one had a demonstrated attack, which
is why treating any of them as a compliance checkbox is unsound.** ARX also names
assumptions that quietly fail in practice: population-uniqueness estimators "assume that
the dataset is a uniform sample of the population," and if not, "results may be
inaccurate" [VERIFIABLE][^19].

ARX itself is Apache-2.0 and not archived, but its last push was 2025-10-01
[VERIFIABLE][^22] - active enough to use, slower-moving than the alternatives. It is a
single-table microdata tool by design; the site does not claim multi-table relational
anonymization [NO SOURCE]. **For a test-fixture corpus these models are almost always the
wrong tool**: generalizing a value into a range or suppressing a cell destroys exactly the
structural precision a regression test asserts on. They belong in analytics data release,
not fixtures.

### Differential privacy: where it applies and where it is cargo cult

Differential privacy is a property of a computation, not of a dataset. ARX states this
directly: with DP "privacy protection is not considered a property of a dataset, but a
property of a data processing method" [VERIFIABLE][^19]. That single sentence resolves
most of the confusion. You cannot make a fixture file differentially private; you can only
make the *generator* that produced it differentially private, and what you get in return
is a bound on how much any single source record influenced the output.

Where it genuinely applies to test data: when you are **training a generative model on
real records and publishing its samples**, DP-SGD or a DP synthesizer bounds the leakage
of any individual training record into the released corpus. That is a real and correct
use. The evaluation literature is candid that measuring what you got is unsettled -
Arnold and Neunhoeffer treat synthetic-data quality as "a subjective concept," assessable
either "against training data or against an underlying population," and call on the field
to "jointly advance the privacy-quality frontier" [VERIFIABLE][^23].

Where invoking it is cargo cult: **when the corpus was produced by substituting values
rather than by a statistical mechanism.** A hand-built or deterministically substituted
fixture set has no epsilon, and asserting one is meaningless. It is also cargo cult when
the privacy goal is "this file must not contain a real person's username" - that is a
categorical requirement satisfied by substitution, and a noise-based mechanism is both
unnecessary and destructive. The mechanics of noise addition also conflict with fixture
requirements directly: DP output is randomized, so regenerating gives different data,
whereas fixtures need byte-stability for diffable failures. **DP earns its place at the
model-training boundary and nowhere else in the fixture pipeline** [INFERENCE - reasoning:
DP's guarantee is defined over a randomized mechanism's output distribution, so a
deterministic substitution has no mechanism for the guarantee to attach to; this follows
from the definition rather than from a source making the fixture-specific claim].

### When to use

| Failure mode | Mitigation that actually works |
|---|---|
| Same identifier masked differently in two files | Deterministic keyed transform, one shared key, applied per file |
| Format/checksum broken by masking | Generated-value pool matching the format; FPE only if the domain exceeds ~1e6 |
| Join key no longer joins | Transform the key once, propagate the mapping; assert with a parity test |
| Quasi-identifiers left untouched | Treat free text, timestamps, and org paths as in-scope, not just the ID column |
| Statistical disclosure in a released analytics extract | k-anonymity family or DP - not applicable to fixtures |
| Leakage from a model trained on real records | DP training, with the caveat that utility measurement is unsettled |

## 3. Synthetic Data Generation

### The tooling landscape as of August 2026

The landscape splits by mechanism, and the live maintenance and license checks changed
the picture materially.

**Structure-preserving / faker-style generators.** Faker generates values per field type
with no cross-field statistical modeling. MIT, actively maintained (last push 2026-08-03)
[VERIFIABLE][^13]. Referential integrity: **not provided** - it is a value generator, so
consistency across files is the caller's job via seeding plus a persisted mapping, subject
to the version-scoped determinism caveat above [VERIFIABLE][^14]. For fixture corpora
this is nonetheless the highest-value tool in the list, because fixture realism is usually
about format validity rather than joint distributions.

**Statistical / copula approaches.** Copulas (part of the SDV ecosystem) models
dependency structure between columns. Actively maintained, last push 2026-07-24
[VERIFIABLE][^24]. **License caution: `NOASSERTION` on the API, and reading the LICENSE
file directly shows Business Source License 1.1 with DataCebo, Inc. as licensor for
SDV and CTGAN** [VERIFIABLE][^25]. BSL 1.1 is a source-available license with a use
restriction, not an OSI-approved open-source license; the SDV grant carves out use in a
"Synthetic Data Service" [VERIFIABLE][^25]. **This is the single most important license
finding in this research**: SDV is routinely described in secondary sources as
open-source, and a team that adopts it on that basis has taken on a license review it does
not know it needs. The fleet rule that dependencies are not a cost does not extend to
license obligations, which are a different category of cost.

**GAN-based tabular synthesis.** CTGAN, also BSL 1.1 [VERIFIABLE][^25], actively
maintained (last push 2026-08-03) [VERIFIABLE][^26]. Its multi-table sibling
HMASynthesizer is the only surveyed synthesizer with an explicit relational story: it
"uses a recursive technique to model the parent-child relationships of a multi-table
datasets" and "models each individual table as well as the connections between them"
[VERIFIABLE][^27]. **But the docs do not claim referential integrity or foreign-key
validity in those terms**, and they state a hard scale limit: "The algorithm is optimized
for smaller datasets with around 5 tables and 1 level of depth," with an enterprise
product pointed to for anything larger [VERIFIABLE][^27]. Treat relational integrity as
a modeling side effect to be verified, not a guarantee.

**Diffusion-based tabular synthesis** exists in the research literature but this research
located no primary source establishing a maintained, licensed, production-usable
implementation aimed at test-fixture generation as of August 2026 [NO SOURCE].

**ISSUE-REF fixtures.** In practice this is now a common approach and it is genuinely
good at the thing that matters most for fixtures - producing structurally valid,
plausibly-shaped records in a specified format with no real person behind them. This
research located no primary-source evaluation of ISSUE-REF test fixtures for
structural fidelity or coverage preservation [NO SOURCE], so what follows is judgment:
the failure mode is regression to the modal case. An LLM asked for sample records tends
to produce well-formed, unsurprising ones, and it is precisely the malformed and
boundary records that a regression corpus exists to pin. Used to generate replacement
*values* inside an existing structure it is low-risk; used to generate the corpus
*structure* it will quietly drop the edge cases [INFERENCE - reasoning: the mode-seeking
behavior of likelihood-trained generators is well established generally, and edge cases
are by construction low-probability, so their omission is the expected outcome absent
explicit prompting for them].

**Property-based generation.** Hypothesis, MPL-2.0 (the LICENSE.txt states the code "is
released under the Mozilla Public License v 2.0") [VERIFIABLE][^28], very actively
maintained (last push 2026-08-05) [VERIFIABLE][^29]. This is the option most often
skipped, and see below for why it deserves better.

**Neosync is archived - do not adopt it.** It appears in current secondary listings as a
recommended open-source anonymization platform, but the live API check shows
`archived: true` with its last push on 2025-08-30 [VERIFIABLE][^30]. **This is exactly
the recalled-fact failure the temporal-scope discipline is for**: a tool that was a
reasonable recommendation a year ago is now unmaintained, and blog-shaped sources have not
caught up.

### Silent coverage loss is the named failure mode

This is the finding to carry forward. A synthetic corpus that no longer exercises the same
code paths is a **silent** loss of coverage: the suite still passes, the diff looks like a
data change, and nothing reports that three error branches are now unreachable.

It is silent because the standard evaluation instruments do not measure it. Synthetic-data
quality metrics measure distributional similarity, and the quality literature itself
frames quality as a subjective, multi-axis question about statistical utility
[VERIFIABLE][^23] - never about branch coverage in the consuming code. A corpus can score
excellently on marginal and joint fidelity while having lost every malformed record,
because malformed records are outliers and generative models smooth outliers away.

Three defenses, in increasing order of strength:

1. **Compare coverage before and after.** Run the suite against the real corpus and the
   synthetic one and diff line and branch coverage. Any drop is a coverage regression and
   should fail the corpus, not the suite. This is the only defense that directly measures
   the failure.
2. **Assert structural invariants explicitly.** Encode what the corpus must contain (a
   row with an empty optional field, a record with a non-ASCII character, one duplicate
   key, one value at the length limit) as tests over the fixture corpus itself. Fixtures
   are inputs and rarely get tested; they should be.
3. **Keep the pathological records hand-authored.** The corpus does not have to be
   uniformly synthetic. Bulk-generate the ordinary rows and hand-write the weird ones,
   which are usually few and are the ones with real diagnostic value.

### Property-based generation is the underrated option

For a regression corpus derived from *structure* rather than from *statistics*,
property-based generation deserves more attention than statistical synthesis, and the
reason is a category difference rather than a quality difference.

Statistical synthesis learns from real data, which means it needs the real data, inherits
its privacy risk, and must be evaluated for leakage. Property-based generation starts from
a declared specification of the domain and never touches real data at all - so the
privacy question does not arise. It also actively seeks boundary values, which is the
opposite of the mode-seeking behavior that causes silent coverage loss, and it shrinks
failures to minimal reproducing cases.

The honest limitation is that generated cases are not stable fixtures by default:
property-based testing explores a different sample each run, which is a feature for bug
discovery and a problem for a corpus that must be diffable. The composition that works is
to use property-based generation to *discover* the interesting cases, then freeze the
discovered ones as committed fixtures [INFERENCE - reasoning: this follows from combining
the exploration property with the byte-stability requirement of a regression corpus; no
surveyed source prescribes this composition explicitly].

### When to use

| Corpus purpose | Approach |
|---|---|
| Regression corpus pinning parser/serializer behavior | Deterministic substitution into the real structure; keep the structure, replace the values |
| Fixtures for a schema with no real data available | Property-based generation, freezing discovered cases |
| Fixtures needing realistic-looking values in a fixed structure | Faker, values generated once and committed |
| Multi-table data where joint distributions matter | HMASynthesizer, within its ~5-table/1-level limit, with FK validity verified not assumed |
| Publishing a dataset derived from real records | DP synthesis, plus a leakage evaluation; expect the tradeoff in the Groundhog Day paper |
| Anything where the license matters | Check it live; BSL 1.1 is not open source |

## 4. Detection and Prevention Automation

### Two different problem classes

Secret detection and PII detection are routinely conflated and are not the same problem.

**Secrets have structure.** An API key has a recognizable prefix, a fixed length, high
entropy, and frequently a verification endpoint. Detection is a pattern-and-entropy
problem with a decidable answer, and the peer-reviewed comparison confirms detection is
achievable at useful rates: across five open-source and four proprietary tools, precision
leaders were "GitHub Secret Scanner (75%), Gitleaks (46%), and Commercial X (25%)" and
recall leaders "Gitleaks (88%), SpectralOps (67%) and TruffleHog (52%)"
[VERIFIABLE][^31]. Note what those numbers mean: **even the best tool in a peer-reviewed
comparison had 25% of its findings be false positives, and the recall leader's precision
was 46%.** The paper attributes false positives to "generic regular expressions and
ineffective entropy calculation" and false negatives to "faulty regular expressions,
skipping specific file types, and insufficient rulesets" [VERIFIABLE][^31].

**PII has no structure in the general case.** A username is a short lowercase string
indistinguishable from a variable name, a column header, or a test value. A person's name
is a word. Detection therefore relies on named-entity recognition with the accuracy
profile that implies, and Presidio's own documentation carries the disclaimer: "because it
is using automated detection mechanisms, there is no guarantee that Presidio will find all
sensitive information," concluding "additional systems and protections should be employed"
[VERIFIABLE][^32]. **A general PII scanner will not reliably catch employee usernames in a
data file. Any control that depends on it doing so is unsound.**

The consequence reframes the whole prevention problem. **The tractable control is not
"detect PII" but "detect these specific known-sensitive strings."** An organization knows
its identifier format and can enumerate its directory. A denylist-plus-format-regex over
known identifiers is checkable, has near-100% recall on the actual risk, and produces
false positives at a rate you control. That is a far stronger control than a
general-purpose NER scanner, and it is the one to build first.

Tool notes with live-checked status:

- **Presidio** - MIT, actively maintained (last push 2026-08-05, release 2.2.364 on
  2026-07-22) [VERIFIABLE][^33]. **It moved organizations**: it is "transitioning from a
  Microsoft-owned project to an independent, community-governed open source project" under
  a new organization, will "continue to be open source under the MIT license," and
  container references must change from the Microsoft registry to the new one
  [VERIFIABLE][^34]. Bookmarks, image pulls, and doc links from before the move need
  updating. Referential integrity: only with a caller-supplied consistent salt
  [VERIFIABLE][^4].
- **Gitleaks** - MIT [VERIFIABLE][^35], last push 2026-07-29 with recent commits
  dominated by dependency bumps and the most recent release v8.30.1 on 2026-03-21
  [VERIFIABLE][^36]. **Its README carries a maintainer warning: "Gitleaks is feature
  complete. I'm not merging new features into Gitleaks. Future releases will be security
  patches only," followed by a statement that the maintainer is shifting focus to a
  successor project, Betterleaks** [VERIFIABLE][^37]. That successor is real and active:
  MIT, not archived, last push 2026-08-05 [VERIFIABLE][^55]. For a detection tool whose
  entire value is a current ruleset, security-patches-only is a material consideration -
  **this is a succession in progress, and a team adopting Gitleaks today should plan the
  migration rather than discover it later.** Gitleaks remains usable and its config format
  and custom TOML rules still work [VERIFIABLE][^37]; the mitigation architecture described
  below (allowlists, baselines, inline suppression) is documented for Gitleaks and is the
  pattern to carry forward regardless of which of the two you run.
- **TruffleHog** - AGPL-3.0, very actively maintained (last push 2026-08-05)
  [VERIFIABLE][^38]. **The AGPL is the thing to check before adopting**, depending on how
  it is invoked and distributed.
- **detect-secrets** - Apache-2.0, last push 2026-04-02 [VERIFIABLE][^39]. Slower cadence
  than the others; its baseline-file workflow is its distinguishing feature.
- **git-secrets** - Apache-2.0, last push 2025-09-17 [VERIFIABLE][^40]. Effectively
  dormant; prefer a maintained alternative.
- **pre-commit** (the framework, not the hook) is the right harness rather than a
  detector: `pre-commit install` writes the git hook script, and the docs explicitly
  endorse running the identical hook set in CI, since "adding `pre-commit run --all-files`
  as a CI step will ensure everything stays in tip-top shape" [VERIFIABLE][^41]. **That
  dual invocation is the key architectural point** - the same configuration runs as
  convenience locally and as an enforced check in CI, so there is one ruleset and no drift.

### Where each control actually binds

This is the section that determines whether a prevention scheme is real. For each control:
where it takes effect, and how a developer bypasses it.

| Control | Binds at | Bypass |
|---|---|---|
| Local `pre-commit` hook | Developer's machine, before commit object creation | `git commit --no-verify`. The hook "can be bypassed with the `--no-verify` option" [VERIFIABLE][^6]. Also: not installing it, since hooks are per-clone and `pre-commit install` is a manual step [VERIFIABLE][^41]. |
| Local `pre-push` hook | Developer's machine, before the push transaction | `git push --no-verify` - "With `--no-verify`, the hook is bypassed completely" [VERIFIABLE][^7] |
| Platform push protection | Platform side, on the push/commit/upload/API path; it "blocks pushes that contain secrets before they reach your repository" [VERIFIABLE][^9] | Anyone with write access can bypass "by specifying a bypass reason"; delegated bypass narrows this to designated approvers [VERIFIABLE][^9]. Also scope: it is credential-oriented, not general PII |
| Platform secret scanning (detection) | Platform side, after the push, over full history [VERIFIABLE][^42] | Nothing to bypass - it does not block. It is detection, not prevention |
| Server-side `pre-receive` hook | The hosting appliance, before refs update; the push is "accepted if the exit status is 0, or rejected if the exit status is non-zero" [VERIFIABLE][^43] | No developer-side bypass documented. **But availability is the catch**: every reference in the documentation is to the self-hosted server product [VERIFIABLE][^43]. Whether the hosted/cloud service offers it is not stated on that page [UNVERIFIED] |
| Branch ruleset: required status check | Platform side, at merge; "all required status checks must pass before collaborators can merge changes into the branch or tag" [VERIFIABLE][^8] | Only actors granted bypass permissions on the ruleset, which "can be users with a certain role, such as repository administrator, or... specific teams or GitHub Apps" [VERIFIABLE][^44] |
| Branch ruleset: restrict updates | Platform side, on direct push; "only users with bypass permissions can push to branches... whose name matches the pattern" [VERIFIABLE][^8] | Same bypass-list caveat |
| CI job running the scanner | The runner, on the pull request | Not bypassable by a developer *if* it is wired as a required status check on the ruleset. **A CI job that is not a required check is advisory, not a control** [INFERENCE - reasoning: an unrequired check reports a result that the merge button does not consult] |

The design that follows is a layered one where the layers have different jobs and only one
of them is the control:

- **Local pre-commit hook: fast feedback, not enforcement.** Its value is catching the
  mistake in two seconds instead of ten minutes. Its bypassability is not a flaw to be
  engineered around; it is the reason it can be fast and noisy. Do not count it.
- **CI check wired as a required status check on a protected-branch ruleset: the
  control.** It runs on infrastructure the developer does not control, and its result gates
  the merge. This is the layer to get right.
- **Restrict direct pushes to protected branches: closes the route around CI.** Without
  it, a required check on merge is irrelevant to someone pushing straight to the branch.
- **Platform push protection: defense in depth**, accepting that it is credential-shaped
  and bypassable-with-a-reason.
- **Platform-side detection over history: the backstop** that finds what the other layers
  missed, with alerting rather than blocking.

**Minimum viable real control**, for a team that will implement exactly one thing: a CI
job that scans changed files for the organization's identifier pattern and known
identifier list, wired as a required status check, with direct pushes to the protected
branch restricted. Everything else is an optimization.

One more binding subtlety worth stating: an audit trail on bypass is not the same as
prevention, but it is not nothing. Platform push protection records the bypass reason and
generates an alert and an audit-log entry [VERIFIABLE][^9]. A control that is bypassable
but *loudly* bypassable behaves quite differently in practice from one bypassable in
silence, because the bypass becomes a deliberate, attributable act.

### False-positive economics

A scanner that cries wolf gets disabled. This is the constraint that decides whether a
control survives its first month, and it is why the recall-maximizing configuration is
usually the wrong one.

The peer-reviewed numbers set the stakes: precision of 75% for the best-performing tool
and 46% for the recall leader, in a controlled benchmark [VERIFIABLE][^31]. On a real
repository the picture is worse, because benchmarks contain a higher density of true
positives than working code does. And a test-fixture repository is close to the worst case
for a PII scanner - it is *full* of things that look like names and identifiers, because
that is what fixtures contain.

Secondary industry sources report large figures for triage time, tool disablement rates,
and trust loss after repeated false positives. This research could not trace those figures
to primary studies; the accessible sources for them are vendor blogs with an interest in
the conclusion. **The direction is well attested and the specific numbers are treated here
as unverified** [UNVERIFIED - vendor-published figures, no primary study located].

What the primary sources do support is the mitigation architecture, and every mature
scanner converges on the same three mechanisms:

1. **Allowlists**, scoped per rule and globally: "You can define multiple allowlists for a
   rule to reduce false positives," matching on commits, paths, regexes, and stopwords
   [VERIFIABLE][^37].
2. **Baselines**, so an existing repository can adopt a scanner without drowning: "When
   using a baseline, gitleaks will ignore any old findings that are present in the
   baseline," and the new report "will only contain new issues" [VERIFIABLE][^37]. **This
   is the mechanism that makes adoption feasible at all** - without it, turning a scanner
   on for the first time on a mature repository produces an unactionable wall of findings
   and the scanner gets turned back off.
3. **Inline and file-level suppression**, a `gitleaks:allow` comment or a
   `.gitleaksignore` fingerprint entry [VERIFIABLE][^37], so a specific known-benign
   finding can be dismissed durably rather than re-triaged every run.

The economics argue for a specific configuration choice, contra the instinct to maximize
recall: **prefer a narrow, high-precision rule over a broad, high-recall one.** A rule
matching the organization's actual identifier format on a known identifier list is nearly
always right when it fires, so it is trusted, so it stays enabled, so it keeps working. A
general NER-based PII scan over a fixture corpus fires constantly, gets allowlisted into
uselessness, and provides false assurance. The high-recall scan has a place, but it is as
a periodic audit whose output a human reviews, not as a merge gate.

Two consequences worth designing in. **Baseline on adoption, always** - never turn a new
scanner on as a blocking gate over unbaselined history. And **make the failure message
actionable**: a gate that says which file, which line, which rule, and what to do next
gets fixed, while one that says "PII scan failed" gets bypassed.

### When to use

| Goal | Control |
|---|---|
| Stop a known identifier list from being committed | Narrow regex/denylist CI check as a required status check; mirror it in a local pre-commit hook for speed |
| Stop credentials from being committed | Gitleaks or TruffleHog in CI as a required check, plus platform push protection |
| Find what is already in history | Platform secret scanning; a full-history scanner run for PII patterns |
| Reduce a legacy repository's noise to zero before enforcing | Baseline file, then enforce on new findings only |
| Detect PII in free text of unknown shape | Presidio, as a periodic human-reviewed audit, never as a blocking gate |

## 5. Secure Storage of the Real Data

The position that object storage is acceptable while the repository is not holds up, and
the reason is more specific than "S3 is more secure." A repository is designed for
unbounded replication and immutable history: every clone is a full copy, every fork
persists independently, and deletion requires rewriting history that other people already
hold. Object storage inverts all three properties - access is centrally revocable, reads
are logged, retention is enforceable, and deletion actually deletes. **The distinction is
not confidentiality but revocability and auditability** [INFERENCE - reasoning: both can
be access-controlled, but only one has a deletion story and a read-access audit trail;
this follows from git's replication model rather than from a source contrasting the two].

### The three patterns, named

**Pattern A: fetch-on-demand.** The real data lives in object storage. Tests that need it
fetch it at runtime with credentials; those tests skip when credentials are absent.
Nothing sensitive is ever in the repository.

*Failure modes.* The suite is no longer hermetic - it needs network and credentials, so it
behaves differently on a developer laptop, in CI, and in a fork's CI (where secrets are
typically unavailable). CI credentials become a standing grant to the sensitive bucket,
which is a bigger blast radius than the fixture itself. The skip path is the real danger:
a test that silently skips when credentials are missing reports green while testing
nothing, and this is how a suite quietly stops covering its most important paths. If you
adopt this pattern, **make the skip loud and assert that the expected number of tests ran
in the environments where they should.**

**Pattern B: committed synthetic stand-in.** A de-identified or synthetic corpus is
committed and is the only corpus the suite uses. Hermetic, fast, forkable, no credentials.

*Failure modes.* Fidelity drift - the real data's shape changes and the stand-in does not,
so the suite passes against a world that no longer exists. And silent coverage loss if the
stand-in was generated rather than derived (section 3).

**Pattern C: two-tier corpus.** Both. A committed de-identified corpus is the default and
gates every merge; the real corpus lives in object storage and is exercised by a separate,
credentialed, non-merge-gating job that runs periodically or on demand. The de-identified
tier is derived from the real tier by a committed, reviewable transformation script.

*Failure modes.* Two corpora to keep in sync, and the sync is exactly where PII leaks -
someone refreshes the committed tier from the real tier and bypasses the transformation.
The mitigations: the transformation is the *only* sanctioned path (script committed, run
in CI, never by hand), and the prevention gate from section 4 runs on the committed tier
so a bad refresh fails the build. Also, the periodic real-data job must be non-blocking or
it reintroduces Pattern A's credential coupling into the merge path.

**Pattern C is the right default for a regression corpus**, because it is the only one
that gets hermetic fast merge-gating tests *and* keeps a real-data check against fidelity
drift. Its failure mode is the one you can automate a defense against; Pattern A's silent
skip and Pattern B's drift are both harder to detect.

This research located no primary-source authority naming or evaluating the two-tier
pattern for test corpora - the pattern is well known in practice but the accessible
write-ups are secondary [NO SOURCE]. It is presented here as an engineering judgment with
its failure modes stated, not as a cited best practice.

### Bucket posture

For the object-storage tier, the platform's own security-best-practices guidance is the
right primary source, and the relevant items map cleanly onto a sensitive-fixture bucket
[VERIFIABLE][^45]:

- **Block public access**, and note the organization-level enforcement option so the
  setting cannot be undone per-account.
- **Disable ACLs** - the bucket-owner-enforced setting is the default for new buckets, and
  policy-only access control "simplifies permissions management and auditing."
- **Encryption at rest is on by default** - all buckets have encryption configured and new
  objects are encrypted at rest with provider-managed keys, with a KMS-key option for
  separate key custody and per-key access policy. For a sensitive corpus, KMS is worth it
  because it gives you a second, independently auditable authorization point.
- **Enforce encryption in transit** with a bucket policy denying non-HTTPS requests.
- **Least privilege, via roles not long-lived keys** - the guidance is explicit that
  long-term credentials "are not automatically rotated and could have a significant
  business impact if they are compromised."
- **Log reads.** Server access logging "provides detailed records of the requests that are
  made to a bucket," and audit-trail data events record object-level operations including
  `GetObject`. **For a sensitive corpus, read logging is the point** - it is what lets you
  answer "who accessed this" later, and it is the capability a repository cannot give you
  at all.
- **Versioning** to recover from mistakes, and **object lock** for a write-once model
  where retention must be provable.
- **Automated sensitive-data discovery** exists as a managed service that "discovers
  sensitive data by using machine learning and pattern matching" and reports findings on
  object storage - useful as a backstop that catches PII that landed in the wrong tier.

Two additions the guidance does not frame in test-data terms but that matter here.
**Retention is a control, not housekeeping**: a lifecycle rule expiring the real corpus
after its useful window shrinks the exposure automatically, and it is the one control that
keeps working when everyone forgets. And **the bucket should be a separate bucket** from
general project data, because the access grant, the logging configuration, and the
retention rule are all bucket-scoped - mixing sensitive fixtures into a shared bucket
forces every consumer of that bucket to inherit the sensitive tier's access posture.

### When to use

| Situation | Pattern |
|---|---|
| Suite must be hermetic and fork-friendly; fidelity drift is tolerable | B |
| Real data is needed by nearly every test; the suite is internal-only | A, with loud skips |
| Regression corpus, merge-gated, with a real-data fidelity check | C (default) |
| Regulated data with provable retention limits | C, real tier with object lock plus a lifecycle expiry |

## 6. Remediation of Data Already in History

### The tooling

**git-filter-repo** is the current recommended tool: it "is now recommended by the git
project" as the alternative to the older `filter-branch`, whose problems "cannot be
backward compatibly fixed" [VERIFIABLE][^46]. Not archived, last push 2026-07-09
[VERIFIABLE][^47], MIT (its COPYING states the tool "and most the files in this repository
... are provided under the MIT license") [VERIFIABLE][^48]. Two design points matter
operationally: it expects a fresh clone, "detecting and bailing if we're not in a fresh
clone" unless overridden with `--force` [VERIFIABLE][^46], and it rewrites all branches
and tags, rewrites commit messages that reference rewritten commit IDs, and repacks
afterwards [VERIFIABLE][^46].

**BFG Repo-Cleaner** is the faster, narrower alternative, claiming "10 - 720x faster than
`git-filter-branch`" [VERIFIABLE][^49], GPL-3.0-or-later [VERIFIABLE][^49]. Its one
behavior that surprises people: "By default the BFG doesn't modify the contents of your
*latest* commit," so "If you want the BFG to delete something **you need to make sure your
current commits are _clean_**" [VERIFIABLE][^49]. **Fix the working tree first, then
rewrite history** - otherwise the rewrite appears to succeed and the data is still in
HEAD.

### What a rewrite does not achieve

This is the part teams get wrong, and the platform's own documentation is unambiguous.

"If you only rewrite your history and force push it, the commits with sensitive data may
still be accessible elsewhere" [VERIFIABLE][^10]. The enumerated survival routes:

- **Clones and forks** - "If the commit that introduced the sensitive data exists in any
  forks, it will continue to be accessible there," and the platform "cannot provide contact
  information for these owners," so notifying them is on you [VERIFIABLE][^10].
- **Cached views addressable by SHA** - the data remains reachable "Directly via their
  SHA-1 hashes in cached views" [VERIFIABLE][^10]. An unreferenced object is still
  fetchable if you know its hash, which anyone who saw the original commit does.
- **Pull requests** - the data persists "Through any pull requests that reference them"
  [VERIFIABLE][^10].
- **Other people's clones**, which are simply out of reach: "You cannot remove sensitive
  data from other users' clones of your repository" [VERIFIABLE][^10].

Full removal therefore requires provider action: you can "permanently remove cached views
and references to the sensitive data in pull requests... by contacting us," and the
provider's own remediation steps include dereferencing or deleting affected pull requests,
removing cached views, and running server-side garbage collection [VERIFIABLE][^10].
**And support is gated on necessity** - it "will only assist in the removal of sensitive
data in cases where we determine that the risk can't be mitigated" by rotating the
affected credentials [VERIFIABLE][^10].

Collateral damage on open pull requests is worth planning for: after a rewrite "you will
no longer be able to see these diffs," "comments on the old PR diff may become invalidated
and lost," and the documented recommendation is to merge or close all open pull requests
before removing files [VERIFIABLE][^10]. A mirror push also "will fail to push any refs
starting with `refs/pull/`" because those are read-only [VERIFIABLE][^10].

### When rewriting is worth it

The primary guidance is built around rotation: revoke or rotate the secret as a first
step, after which "Once the secret is revoked or rotated, it can no longer be used for
access," so the extra steps "may not be warranted" [VERIFIABLE][^10].

**A personal identifier is not rotatable.** You cannot revoke a username; the disclosure
is permanent once made. So the rotate-and-move-on path does not transfer from the secrets
case to the PII case, and the decision has to rest on sensitivity and exposure instead:

**Rewrite is worth it when** the repository is private or has small, enumerable
visibility; forks and clones are few and reachable; open pull requests are few or already
closable; the data is genuinely sensitive (health, financial, credentials, anything whose
disclosure harms a person materially); or a regulatory or contractual obligation requires
demonstrable removal. In the regulated case the rewrite is partly an evidence-production
exercise, and provider action is part of the deliverable, not optional.

**Rewrite is not worth it when** the exposure is a low-sensitivity identifier such as a
work username that is already visible internally; the repository is public or widely
forked, so the data is already irretrievably copied; or open pull-request and collaborator
disruption exceeds the marginal risk reduction. **In that case the honest answer is:
remediate forward.** Remove the data from the current tree, fix the generation process so
it cannot recur, install the prevention gate, and record the decision and its rationale.
The last part is not paperwork - an undocumented decision not to rewrite is
indistinguishable from not having noticed, and will be re-litigated at the next audit.

The judgment that a work username sits in the not-worth-it band is a
sensitivity-versus-cost call, not a documented standard [INFERENCE - reasoning: the
provider gates its own support on whether risk can be mitigated otherwise, which
establishes that proportionality is the operative test; applying that test to a
low-sensitivity identifier is our own step]. **Have the accountable owner make it
explicitly rather than defaulting into it**, and see section 7 for where to record it.

### When to use

| Situation | Action |
|---|---|
| Credential leaked | Rotate immediately. Rewrite only if rotation is impossible or compliance requires it |
| Low-sensitivity identifier, public/widely-forked repository | Remediate forward; record the decision and owner |
| Sensitive personal data, private repository, few forks | Rewrite with git-filter-repo from a fresh clone, close open pull requests first, then request provider cleanup |
| Regulatory obligation to demonstrate removal | Rewrite plus provider action plus a written record; treat notifying fork owners as in scope |
| Large binary blobs alongside the sensitive data | BFG for speed, but clean the working tree first |

## 7. Governance and Classification

### Deciding what counts as PII

There is no universal list, and the authoritative sources are explicit that the decision
is contextual and organizational. The relevant NIST guide offers "practical, context-based
guidance for identifying PII and determining what level of protection is appropriate" for
each instance, and organizations "are encouraged to tailor the recommendations" to their
own requirements [VERIFIABLE][^50]. Note its vintage - published April 2010, still listed
as Final [VERIFIABLE][^50] - which means it predates the modern re-identification
literature and should be used for its process framing rather than as a current threat
model.

The regulatory framing is broader and more useful as a test. Personal data is "any
information relating to an identified or identifiable natural person," where identifiable
means directly or indirectly so, explicitly including "online identifiers"
[VERIFIABLE][^51]. **A work username is an online identifier that identifies a natural
person indirectly, so it is squarely in scope** - which is the answer to the objection
that a username "is not really PII." And because identifiability is assessed against means
"reasonably likely to be used" given "the available technology at the time of the
processing" [VERIFIABLE][^2], the classification cannot be settled once and forgotten in a
literal sense; what can be settled is the *decision procedure* and the *default*.

The practical governance move is to invert the question. Rather than asking "is this
field PII," which invites per-field debate, ask **"is this field on the approved
committable-values list."** A default-deny classification, where any field carrying a
value that originated from a real person's record is out unless explicitly cleared, is
mechanically checkable and does not require re-adjudicating the definition each time.

### Recording the decision so it is not re-litigated

Three artifacts, each doing a different job, and the failure mode of skipping any of them
is a per-corpus re-argument:

1. **A data-classification policy for fixture corpora** stating what may be committed,
   what may not, and the default when a field is unclear. This is the standing rule and it
   should be short enough to be read.
2. **A per-corpus provenance record**, committed alongside the corpus: where the data came
   from, what transformation was applied, who approved it, when, and what the residual
   risk is. This is what makes the corpus auditable a year later when nobody remembers.
   **It also converts the transformation from folklore into a reviewable artifact** - which
   matters most at refresh time, the exact moment section 5's Pattern C leaks.
3. **A decision record for exceptions**, including the decision not to rewrite history.
   Sensitivity judgments are the kind of call that gets silently re-made differently by
   the next person; writing down the reasoning is what prevents that.

On sign-off: the accountable owner should be whoever owns the data's classification, not
the engineer who wants the corpus committed - because the engineer has an interest in the
answer. **The strongest governance mechanism, though, is not a sign-off but a default that
makes the safe path the easy one.** A committed generator plus a required CI gate means
the ordinary way of adding a corpus is the compliant way, and a sign-off is needed only for
the exception. Governance that relies on people remembering to ask fails at the first
deadline; governance embedded in the pipeline does not.

A note on scope creep to resist: classification decisions expand naturally into a
data-governance program. For a fixture corpus the whole apparatus can be three
paragraphs in a policy doc, a small provenance file per corpus, and a decision-record
convention. If it grows past that without a regulatory driver, it has stopped serving the
engineering goal.

### When to use

| Need | Artifact |
|---|---|
| Standing rule for what may be committed | Fixture data-classification policy, default-deny |
| Auditability of one corpus | Committed provenance record, including the transformation and approver |
| A judgment that departs from the standing rule | Decision record naming the accountable owner and the residual risk |
| Preventing re-litigation at refresh time | The transformation as committed code, run in CI, plus the gate from section 4 |

## 8. Gaps, Blindspots and Emergent Findings

Threads that surfaced during the research and are not answered by the framing above.

**RDF and graph-native fixture anonymization is a genuine gap.** Searching for
graph-anonymization work yields the social-network topology literature - the framework
paper located, DUEF-GA, evaluates graph anonymization using "average distance or
betweenness centrality" plus task-specific measures like "community detection or
information flow," and re-identification risk measures [VERIFIABLE][^52]. Useful for
social graphs; not applicable to a triple store where the identifier is an IRI and the
"structure" that must survive is a serialization contract. **No significant post-2024
tooling development for RDF/semantic-web fixture anonymization was located.** The
consequence is practical: for a CSV-plus-Turtle corpus, expect to write the
transformation, and expect the IRI local-part to be the thing that carries the identifier.
Named as a blindspot: it is possible that relevant work exists in the semantic-web venues
under vocabulary this research did not guess.

**Derived and non-obvious carriers of the identifier.** The identifier is rarely only in
the field named after it. It appears in IRI local parts, in filenames, in commit messages
and branch names, in test names and assertion strings, in expected-output files, in
recorded HTTP fixtures, in serialized caches, in coverage and log artifacts. **A
transformation that fixes the data file and leaves the expected-output file is not a fix,
and a corpus scan that reads only data files will not notice.** This deserves to be a
checklist item, and it is not addressed by any surveyed tool.

**Commit metadata is PII that no fixture transformation touches.** Author names and email
addresses are personal data by the regulation's own definition, are in every commit, and
are untouched by any of the tooling here. Rewriting them has the same
history-rewrite economics as section 6. Out of scope for the fixture problem, but worth
knowing it is a separate, unaddressed surface.

**Adjacent field worth borrowing from: supply-chain provenance.** The problem of "prove
what transformation produced this artifact" is solved more maturely in build provenance
than in test-data management. The provenance record in section 7 is a hand-rolled version
of a signed attestation, and a team already running build attestations could plausibly
reuse that machinery for corpus provenance. Not pursued here; flagged as a likely
better-engineered path than a bespoke metadata file [INFERENCE].

**Contrarian evidence reported against our own premise.** Two findings cut against the
instinct that more privacy machinery is better. The Groundhog Day result is that synthetic
data does not dominate traditional anonymization on the privacy-utility frontier
[VERIFIABLE][^5], so reaching for a generative model is not automatically an upgrade over
substitution. And the rotate-and-move-on guidance [VERIFIABLE][^10] means that for
secrets the *correct* answer is often to not clean history at all - the reflex to purge is
frequently wrong on cost-benefit grounds. Both point the same direction: **match the
mechanism to the actual exposure, and prefer the simplest mechanism that closes it.**

**A blindspot named but unresolved: measuring whether de-identification preserved
coverage.** Section 3 recommends comparing coverage before and after, which requires
running the suite against the real corpus - which is exactly what Pattern B forbids. Teams
on a committed-stand-in-only model have no way to detect the coverage loss they may have
already taken. Two-tier storage resolves it in principle, since the real tier is available
to the periodic job, but this research located no tooling that performs coverage-delta
comparison across corpora as a first-class check [NO SOURCE]. That looks like a genuine
gap in the ecosystem rather than an oversight in this survey.

## Comparison

Tools are compared on the criteria the deliverable requires: maintenance status and
license (checked live 2026-08-05), and whether the tool handles cross-file referential
integrity.

| # | Tool | Purpose | License | Maintained (checked 2026-08-05) | Cross-file referential integrity |
|---|---|---|---|---|---|
| 1 | Presidio | PII detection + anonymization in text/structured data | MIT [^33] | Yes; last push 2026-08-05, release 2.2.364. **Moved to a community org** [^34] | Only with a caller-supplied consistent salt; random-salted by default; no session state [^4] |
| 2 | Greenmask | Database anonymization/subsetting | Apache-2.0 [^53] | Yes; last push 2026-08-05 [^53] | **Yes**, via a deterministic hash engine [^3] - but PostgreSQL only [^3] |
| 3 | Neosync | Anonymization + sync platform | Other [^30] | **No - archived**, last push 2025-08-30 [^30] | Not applicable; do not adopt |
| 4 | Faker | Fake value generation | MIT [^13] | Yes; last push 2026-08-03 [^13] | No. Caller's job; determinism is version-scoped [^14] |
| 5 | SDV | Multi-table synthetic data | **Business Source License 1.1**, not OSI open source [^25] | Yes; last push 2026-08-04 [^54] | Partial: HMASynthesizer models parent-child relations [^27], but no explicit FK-integrity claim, and limited to ~5 tables / 1 level [^27] |
| 6 | CTGAN | GAN-based tabular synthesis | **Business Source License 1.1** [^25] | Yes; last push 2026-08-03 [^26] | No - single-table |
| 7 | ARX | k-anonymity family + risk analysis | Apache-2.0 [^22] | Yes, slower; last push 2025-10-01 [^22] | No - single-table microdata [NO SOURCE for multi-table] |
| 8 | Hypothesis | Property-based data generation | MPL-2.0 [^28] | Yes; last push 2026-08-05 [^29] | Not applicable - generates from a spec, touches no real data |
| 9 | Gitleaks | Secret detection | MIT [^35] | **Security patches only; maintainer moved to a successor project** [^37]; last push 2026-07-29 [^36] | Not applicable (detector) |
| 9a | Betterleaks | Secret detection (Gitleaks successor) | MIT [^55] | Yes; last push 2026-08-05 [^55] | Not applicable (detector) |
| 10 | TruffleHog | Secret detection + verification | **AGPL-3.0** [^38] | Yes; last push 2026-08-05 [^38] | Not applicable (detector) |
| 11 | detect-secrets | Secret detection with baselines | Apache-2.0 [^39] | Slower; last push 2026-04-02 [^39] | Not applicable (detector) |
| 12 | git-secrets | Regex secret prevention hook | Apache-2.0 [^40] | **Effectively dormant**; last push 2025-09-17 [^40] | Not applicable (detector) |
| 13 | pre-commit | Hook framework, local + CI | Not stated on the docs site [UNVERIFIED] | Yes (actively documented; version-specific check not performed) | Not applicable (harness) |
| 14 | git-filter-repo | History rewriting | MIT [^48] | Yes; last push 2026-07-09 [^47] | Not applicable (remediation) |
| 15 | BFG Repo-Cleaner | History rewriting, fast | GPL-3.0-or-later [^49] | Not checked in this run [UNVERIFIED] | Not applicable (remediation) |

## Recommendation

**For a multi-file fixture corpus that must stay internally consistent, write the
transformation. Do not adopt a masking platform.** No surveyed tool handles the
cross-format case: Greenmask has the right mechanism but is PostgreSQL-only [^3],
Presidio requires you to supply the consistency yourself anyway [^4], and the synthesis
tools either carry a Business Source License [^25] or are single-table. The thing you
actually need is a deterministic mapping from real value to generated value, applied
identically to every file, with the parity test as the integrity check. That is a small
amount of code, it is reviewable, and it is the only approach that can span a CSV and a
derived serialization.

Concretely, in priority order:

1. **Install the real control before touching the corpus.** A CI job that scans changed
   files for the organization's identifier pattern and a known-identifier denylist, wired
   as a **required status check** on the protected-branch ruleset [^8], with direct pushes
   to that branch restricted [^8]. Baseline existing findings so adoption does not produce
   a wall of noise [^37]. Mirror the same check in a local `pre-commit` hook for fast
   feedback, and treat that copy as convenience only - it is bypassable with `--no-verify`
   [^6] and does not count as a control. Use the pre-commit framework's documented
   local-plus-CI dual invocation so there is one ruleset [^41].
2. **De-identify by deterministic substitution, and discard the key.** Generate
   replacement values once, commit them along with the generator, and do not commit the
   mapping. If refreshes need stable values, hold the key in a secret manager. This makes
   the corpus effectively anonymous while satisfying cross-file consistency - and it avoids
   the pseudonymization trap where a retained key keeps the corpus inside the personal-data
   regime [^1][^11].
3. **Adopt the two-tier corpus.** Committed de-identified tier gates merges; the real
   corpus lives in a dedicated object-storage bucket with public access blocked, KMS
   encryption, HTTPS-only policy, read logging, versioning, and a lifecycle expiry [^45];
   a separate non-blocking job exercises it periodically against fidelity drift. Make the
   transformation script the only sanctioned refresh path.
4. **Defend against silent coverage loss explicitly.** Diff coverage between the real and
   de-identified corpora, assert structural invariants over the corpus itself, and keep the
   pathological records hand-authored. Without this, a de-identified corpus can quietly
   stop exercising the branches it was built to pin.
5. **On history: decide deliberately, and record the decision.** For a low-sensitivity
   work identifier in a widely-cloned repository, remediate forward - the provider's own
   documentation establishes that a rewrite does not achieve deletion without provider
   action [^10], and that its support is gated on the risk not being otherwise mitigable
   [^10]. For genuinely sensitive data in a contained repository, rewrite with
   git-filter-repo from a fresh clone [^46], close open pull requests first [^10], and
   request provider cleanup. Either way, write down who decided and why.
6. **Governance: three short artifacts, default-deny.** A fixture-classification policy, a
   committed per-corpus provenance record, and a decision record for exceptions. A work
   username is an online identifier and therefore in scope [^51] - settle that once so it
   is not re-argued per corpus.

**What not to do**: do not reach for k-anonymity, l-diversity, t-closeness, or
differential privacy for a fixture corpus - they are microdata-release and
statistical-mechanism tools, and generalization or noise destroys exactly the structural
precision a regression test asserts on [^19]. Do not treat synthetic data as automatically
more private than substitution; the empirical result is that it does not dominate
traditional anonymization on the privacy-utility frontier [^5]. Do not adopt Neosync,
which is archived [^30]. Do not adopt Gitleaks for a long-lived gate without knowing it is
security-patches-only and that its maintainer has moved to a successor [^37][^55]. Do not
adopt SDV or CTGAN without a license review, because
Business Source License 1.1 is not open source [^25]. And do not rely on a general PII
scanner to catch employee usernames - its own documentation declines to guarantee recall
[^32], and the tractable control is a narrow denylist on identifiers you can enumerate.

## Open Questions

1. Does the hosted (non-self-hosted) tier of the dominant git platform expose any
   server-side pre-receive hook equivalent, or is a required status check on a ruleset the
   only genuinely non-bypassable control available there? The pre-receive documentation
   references only the self-hosted server product but never states the limitation
   [UNVERIFIED].
2. Is there any maintained tooling that performs coverage-delta comparison between a real
   and a de-identified corpus as a first-class check? This research found none, and it is
   the missing instrument for the silent-coverage-loss failure mode.
3. What is the actual, primary-source false-positive cost of secret and PII scanners on
   real repositories over time? The peer-reviewed benchmark gives precision figures
   [^31], but the operational figures on triage time and tool abandonment trace only to
   vendor sources.
4. For RDF/semantic-web fixture corpora specifically, does relevant anonymization work
   exist in semantic-web venues under terminology this survey did not search? The graph
   anonymization literature located targets network topology, not triple stores.
5. When SP 800-38G Rev. 1 finalizes with FF3 removed [^17], what is the migration
   obligation for systems that deployed FF3 or FF3-1 under the 2016 final standard, and
   does any deployed test-data masking product still ship it?
6. Does the organization's directory expose an enumerable identifier list that a denylist
   check can consume, and how does that list stay current as people join and leave? The
   recommended control depends on it.

## Sources

[^1]: Article 29 Data Protection Working Party. (2014). [Opinion 05/2014 on Anonymisation Techniques (WP216)](https://ec.europa.eu/justice/article-29/documentation/opinion-recommendation/files/2014/wp216_en.pdf). European Commission. Verified accessible (HTTP 200) 2026-08-05; PDF text extracted locally. (Pseudonymisation-is-not-anonymisation, irreversibility, residual risk, encryption-as-pseudonymisation.)
[^2]: European Union. (2016). [Recital 26 - Not Applicable to Anonymous Data](https://gdpr-info.eu/recitals/no-26/). GDPR-Info. Verified accessible (HTTP 200) 2026-08-05. (Means reasonably likely to be used; cost, time, available technology.)
[^3]: Greenmask. (2026). [Greenmask - PostgreSQL database anonymization and synthetic data generation tool](https://docs.greenmask.io/latest/). Greenmask Documentation. Verified accessible (HTTP 200) 2026-08-05. (Deterministic hash engine; PostgreSQL-only scope.)
[^4]: Presidio. (2026). [Presidio Anonymizer](https://presidio.dataprivacystack.org/anonymizer/). Data Privacy Stack. Verified accessible (HTTP 200) 2026-08-05. (Operator list; random salting as of v2.2.361; consistent-salt requirement for referential integrity; no session state.)
[^5]: Stadler, T., Oprisanu, B., & Troncoso, C. (2022). [Synthetic Data - Anonymisation Groundhog Day](https://arxiv.org/abs/2011.07018). arXiv:2011.07018 (v6, USENIX Security 2022). Verified accessible (HTTP 200) 2026-08-05. (Synthetic data does not improve the privacy-utility tradeoff; unpredictable privacy gain.)
[^6]: Git. (2026). [githooks Documentation](https://git-scm.com/docs/githooks). Git SCM. Verified accessible (HTTP 200) 2026-08-05. (pre-commit bypassable with --no-verify; pre-receive/update semantics; hooks directory.)
[^7]: Git. (2026). [git-push Documentation](https://git-scm.com/docs/git-push). Git SCM. Verified accessible (HTTP 200) 2026-08-05. ("With --no-verify, the hook is bypassed completely.")
[^8]: GitHub. (2026). [Available rules for rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets). GitHub Docs. Verified accessible (HTTP 200) 2026-08-05. (Required status checks before merging; restrict updates/creations/deletions; block force pushes.)
[^9]: GitHub. (2026). [About push protection](https://docs.github.com/en/code-security/secret-scanning/introduction/about-push-protection). GitHub Docs. Verified accessible (HTTP 200) 2026-08-05. (Blocks pushes pre-arrival; write-access bypass with a reason; delegated bypass; audit trail.)
[^10]: GitHub. (2026). [Removing sensitive data from a repository](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository). GitHub Docs. Verified accessible (HTTP 200) 2026-08-05. (Rewrite alone is insufficient; forks/cached views/pull requests; support gated on non-mitigable risk; rotate first.)
[^11]: European Union. (2016). [Art. 4 GDPR - Definitions](https://gdpr-info.eu/ISSUE-REF-gdpr/). GDPR-Info. Verified accessible (HTTP 200) 2026-08-05. (Definitions of personal data and pseudonymisation.)
[^12]: Greenmask. (2026). [Database subset](https://docs.greenmask.io/latest/database_subset/). Greenmask Documentation. Verified accessible (HTTP 200) 2026-08-05. (FK dependency graph, LEFT JOIN integrity conditions, recursive cycles, virtual references, stated limitations.)
[^13]: joke2k. (2026). [faker repository metadata](https://github.com/joke2k/faker). GitHub REST API. Verified accessible (HTTP 200) 2026-08-05 via `gh api repos/joke2k/faker`. (MIT; archived=false; pushed_at 2026-08-03.)
[^14]: Faker. (2026). [Faker documentation - Welcome](https://faker.readthedocs.io/en/master/index.html). Read the Docs. Verified accessible (HTTP 200) 2026-08-05. (Seeding and version-scoped determinism; .unique semantics, UniquenessException, birthday-paradox collisions.)
[^15]: Dworkin, M. (2016). [SP 800-38G: Recommendation for Block Cipher Modes of Operation - Methods for Format-Preserving Encryption](https://csrc.nist.gov/pubs/sp/800/38/g/upd1/final). NIST Computer Security Resource Center. Verified accessible (HTTP 200) 2026-08-05. (Specifies FF1 and FF3; Final, updated 2016-08-04.)
[^16]: Dworkin, M. (2019). [SP 800-38G Rev. 1 (Initial Public Draft)](https://csrc.nist.gov/pubs/sp/800/38/g/r1/ipd). NIST Computer Security Resource Center. Verified accessible (HTTP 200) 2026-08-05. (FF3 revised and renamed FF3-1; small-domain vulnerabilities; minimum domain size of one million becomes a requirement. Obsoleted by the 2nd public draft.)
[^17]: Dworkin, M., & Mouha, N. (2025). [SP 800-38G Rev. 1 (Second Public Draft)](https://csrc.nist.gov/pubs/sp/800/38/g/r1/2pd). NIST Computer Security Resource Center. Verified accessible (HTTP 200) 2026-08-05. ("The encryption method FF3 is no longer specified"; FF1 domain size increased; comment period closed 2025-04-04.)
[^18]: Narayanan, A., & Shmatikov, V. (2008). [Robust De-anonymization of Large Sparse Datasets](https://doi.org/10.1109/sp.2008.33). 2008 IEEE Symposium on Security and Privacy. Existence and metadata confirmed via Crossref API 2026-08-05 (DOI 10.1109/sp.2008.33). (Foundational linkage/re-identification result; pre-2024, cited as background.)
[^19]: ARX. (2026). [Privacy models](https://arx.deidentifier.org/overview/privacy-criteria/). ARX Data Anonymization Tool. Verified accessible (HTTP 200) 2026-08-05. (k-anonymity, k-map, l-diversity, t-closeness, delta-disclosure, beta-likeness, delta-presence, differential privacy as a property of a method; stated assumptions and limitations.)
[^20]: Machanavajjhala, A., Kifer, D., Gehrke, J., & Venkitasubramaniam, M. (2006). [L-diversity: privacy beyond k-anonymity](https://doi.org/10.1109/icde.2006.1). 22nd International Conference on Data Engineering. Existence and metadata confirmed via Crossref API 2026-08-05. (Pre-2024, foundational.)
[^21]: Li, N., Li, T., & Venkatasubramanian, S. (2007). [t-Closeness: Privacy Beyond k-Anonymity and l-Diversity](https://doi.org/10.1109/icde.2007.367856). 2007 IEEE 23rd International Conference on Data Engineering. Existence and metadata confirmed via Crossref API 2026-08-05. (Pre-2024, foundational.)
[^22]: arx-deidentifier. (2026). [arx repository metadata](https://github.com/arx-deidentifier/arx). GitHub REST API. Verified accessible (HTTP 200) 2026-08-05 via `gh api repos/arx-deidentifier/arx`. (Apache-2.0; archived=false; pushed_at 2025-10-01.)
[^23]: Arnold, C., & Neunhoeffer, M. (2021). [Really Useful Synthetic Data - A Framework to Evaluate the Quality of Differentially Private Synthetic Data](https://arxiv.org/abs/2004.07740). arXiv:2004.07740 (v2). Verified accessible (HTTP 200) 2026-08-05. (Synthetic-data quality as a subjective, multi-axis question; privacy-quality frontier.)
[^24]: sdv-dev. (2026). [Copulas repository metadata](https://github.com/sdv-dev/Copulas). GitHub REST API. Verified accessible (HTTP 200) 2026-08-05 via `gh api repos/sdv-dev/Copulas`. (archived=false; pushed_at 2026-07-24; license reported as NOASSERTION.)
[^25]: DataCebo, Inc. (2026). [SDV LICENSE file](https://github.com/sdv-dev/SDV/blob/main/LICENSE). GitHub. Verified accessible (HTTP 200) 2026-08-05 via `gh api repos/sdv-dev/SDV/contents/LICENSE` (and the equivalent CTGAN LICENSE). (Business Source License 1.1; Synthetic Data Service use restriction.)
[^26]: sdv-dev. (2026). [CTGAN repository metadata](https://github.com/sdv-dev/CTGAN). GitHub REST API. Verified accessible (HTTP 200) 2026-08-05 via `gh api repos/sdv-dev/CTGAN`. (archived=false; pushed_at 2026-08-03.)
[^27]: SDV. (2026). [HMASynthesizer](https://docs.sdv.dev/sdv/modeling/multi-table-synthesizers/hmasynthesizer.md). SDV Documentation. Verified accessible (HTTP 200) 2026-08-05. (Recursive parent-child modeling; optimized for ~5 tables and 1 level of depth; no conditional sampling.)
[^28]: MacIver, D. R., et al. (2026). [Hypothesis LICENSE.txt](https://github.com/HypothesisWorks/hypothesis/blob/master/LICENSE.txt). GitHub. Verified accessible (HTTP 200) 2026-08-05 via `gh api`. (Mozilla Public License v2.0.)
[^29]: HypothesisWorks. (2026). [hypothesis repository metadata](https://github.com/HypothesisWorks/hypothesis). GitHub REST API. Verified accessible (HTTP 200) 2026-08-05 via `gh api repos/hypothesisworks/hypothesis`. (archived=false; pushed_at 2026-08-05.)
[^30]: nucleuscloud. (2026). [neosync repository metadata](https://github.com/nucleuscloud/neosync). GitHub REST API. Verified accessible (HTTP 200) 2026-08-05 via `gh api repos/nucleuscloud/neosync`. (**archived=true**; pushed_at 2025-08-30.)
[^31]: Basak, S. K., Cox, J., Reaves, B., & Williams, L. (2023). [A Comparative Study of Software Secrets Reporting by Secret Detection Tools](https://arxiv.org/abs/2307.00714). arXiv:2307.00714; ESEM 2023 (DOI 10.1109/esem56168.2023.10304853, confirmed via OpenAlex 2026-08-05). Verified accessible (HTTP 200) 2026-08-05. (Per-tool precision and recall; causes of false positives and negatives.)
[^32]: Presidio. (2026). [Presidio - Data Protection and De-identification SDK](https://presidio.dataprivacystack.org/). Data Privacy Stack. Verified accessible (HTTP 200) 2026-08-05. (Module list; "no guarantee that Presidio will find all sensitive information".)
[^33]: data-privacy-stack. (2026). [presidio repository metadata](https://github.com/data-privacy-stack/presidio). GitHub REST API. Verified accessible (HTTP 200) 2026-08-05 via `gh api`. (MIT; archived=false; pushed_at 2026-08-05; latest release 2.2.364 on 2026-07-22. Note: `repos/microsoft/presidio` now resolves to this repository.)
[^34]: Presidio maintainers. (2026). [Project Transition](https://raw.githubusercontent.com/data-privacy-stack/presidio/main/docs/project_transition.md). GitHub (data-privacy-stack/presidio). Verified accessible (HTTP 200) 2026-08-05. (Microsoft-owned to community-governed; MIT retained; container registry change.)
[^35]: Rice, Z. (2026). [Gitleaks LICENSE](https://github.com/gitleaks/gitleaks/blob/master/LICENSE). GitHub. Verified accessible (HTTP 200) 2026-08-05 via `gh api`. (MIT License, Copyright (c) 2019 Zachary Rice.)
[^36]: gitleaks. (2026). [gitleaks repository metadata and releases](https://github.com/gitleaks/gitleaks). GitHub REST API. Verified accessible (HTTP 200) 2026-08-05 via `gh api`. (archived=false; pushed_at 2026-07-29; latest release v8.30.1 published 2026-03-21; recent commits are dependency bumps.)
[^37]: Gitleaks. (2026). [Gitleaks README](https://github.com/gitleaks/gitleaks/blob/master/README.md). GitHub. Verified accessible (HTTP 200) 2026-08-05. ("Gitleaks is feature complete"; pre-commit and GitHub Action usage; SKIP; custom TOML rules; allowlists, baselines, gitleaks:allow, .gitleaksignore.)
[^38]: trufflesecurity. (2026). [trufflehog repository metadata](https://github.com/trufflesecurity/trufflehog). GitHub REST API. Verified accessible (HTTP 200) 2026-08-05 via `gh api`. (AGPL-3.0; archived=false; pushed_at 2026-08-05.)
[^39]: Yelp. (2026). [detect-secrets repository metadata](https://github.com/Yelp/detect-secrets). GitHub REST API. Verified accessible (HTTP 200) 2026-08-05 via `gh api`. (Apache-2.0; archived=false; pushed_at 2026-04-02.)
[^40]: awslabs. (2026). [git-secrets repository metadata](https://github.com/awslabs/git-secrets). GitHub REST API. Verified accessible (HTTP 200) 2026-08-05 via `gh api`. (Apache-2.0; archived=false; pushed_at 2025-09-17.)
[^41]: pre-commit. (2026). [pre-commit - A framework for managing and maintaining multi-language pre-commit hooks](https://pre-commit.com/). Verified accessible (HTTP 200) 2026-08-05. (`pre-commit install` writes .git/hooks/pre-commit; `pre-commit run --all-files` in CI; per-language isolated environments.)
[^42]: GitHub. (2026). [About secret scanning](https://docs.github.com/en/code-security/secret-scanning/introduction/about-secret-scanning). GitHub Docs. Verified accessible (HTTP 200) 2026-08-05. (Scans full history on all branches for known secret types; credential-oriented; alerting rather than blocking on this page.)
[^43]: GitHub. (2026). [About pre-receive hooks](https://docs.github.com/en/enterprise-server@latest/admin/enforcing-policies/enforcing-policy-with-pre-receive-hooks/about-pre-receive-hooks). GitHub Enterprise Server Docs. Verified accessible (HTTP 200) 2026-08-05. (Runs on the appliance; accept on exit 0, reject on non-zero; every reference is to the self-hosted server product.)
[^44]: GitHub. (2026). [About rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets). GitHub Docs. Verified accessible (HTTP 200) 2026-08-05. (Bypass lists by role, team, or app; most restrictive rule applies across overlapping rulesets.)
[^45]: Amazon Web Services. (2026). [Security best practices for Amazon S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/security-best-practices.html). AWS Documentation. Verified accessible (HTTP 200) 2026-08-05. (Block Public Access, disable ACLs, default encryption and SSE-KMS, HTTPS-only bucket policy, least privilege and roles over long-lived keys, server access logging and audit data events, versioning, Object Lock, managed sensitive-data discovery.)
[^46]: newren. (2026). [git-filter-repo README](https://raw.githubusercontent.com/newren/git-filter-repo/main/README.md). GitHub. Verified accessible (HTTP 200) 2026-08-05. (Recommended by the git project over filter-branch; bails outside a fresh clone unless --force; rewrites all branches and tags and repacks.)
[^47]: newren. (2026). [git-filter-repo repository metadata](https://github.com/newren/git-filter-repo). GitHub REST API. Verified accessible (HTTP 200) 2026-08-05 via `gh api`. (archived=false; pushed_at 2026-07-09.)
[^48]: newren. (2026). [git-filter-repo COPYING](https://github.com/newren/git-filter-repo/blob/main/COPYING). GitHub. Verified accessible (HTTP 200) 2026-08-05 via `gh api`. (MIT for the tool and most repository files, with noted exceptions.)
[^49]: Tyley, R. (2026). [BFG Repo-Cleaner](https://rtyley.github.io/bfg-repo-cleaner/). Verified accessible (HTTP 200) 2026-08-05. (10-720x faster than git-filter-branch; latest commit protected by default; --no-blob-protection not recommended; GPL v3 or later; Java 11+.)
[^50]: McCallister, E., Grance, T., & Scarfone, K. (2010). [SP 800-122: Guide to Protecting the Confidentiality of Personally Identifiable Information (PII)](https://csrc.nist.gov/pubs/sp/800/122/final). NIST Computer Security Resource Center. Verified accessible (HTTP 200) 2026-08-05. (Context-based guidance for identifying PII and determining protection level; organizations tailor the recommendations; Final, April 2010.)
[^51]: European Union. (2016). [Art. 4 GDPR - Definitions, point 1](https://gdpr-info.eu/ISSUE-REF-gdpr/). GDPR-Info. Verified accessible (HTTP 200) 2026-08-05. (Personal data as any information relating to an identified or identifiable natural person, including online identifiers. Same source as [^11]; cited separately for the online-identifier point.)
[^52]: Casas-Roma, J. (2025). [DUEF-GA: Data Utility and Privacy Evaluation Framework for Graph Anonymization](https://arxiv.org/abs/2501.18625). arXiv:2501.18625. Verified accessible (HTTP 200) 2026-08-05. (Graph anonymization evaluation via average distance, betweenness centrality, community detection, information flow; network topology rather than RDF/triple stores.)
[^53]: GreenmaskIO. (2026). [greenmask repository metadata](https://github.com/GreenmaskIO/greenmask). GitHub REST API. Verified accessible (HTTP 200) 2026-08-05 via `gh api`. (Apache-2.0; archived=false; pushed_at 2026-08-05.)
[^54]: sdv-dev. (2026). [SDV repository metadata](https://github.com/sdv-dev/SDV). GitHub REST API. Verified accessible (HTTP 200) 2026-08-05 via `gh api`. (archived=false; pushed_at 2026-08-04; license reported as NOASSERTION, resolved to BSL 1.1 by reading the LICENSE file - see [^25].)
[^55]: betterleaks. (2026). [betterleaks repository metadata](https://github.com/betterleaks/betterleaks). GitHub REST API. Verified accessible (HTTP 200) 2026-08-05 via `gh api repos/betterleaks/betterleaks`. (MIT; archived=false; pushed_at 2026-08-05. Named as the successor project in the Gitleaks README warning - see [^37].)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet - auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** claude-adhoc-2026-08-05 (no ai-core DB on this machine; not registered)
**Model:** `opus researcher` (Claude Opus 5, 1M context, via Bedrock)
**Date:** 2026-08-05

````text
Research the landscape, as of August 2026, for PII safeguards in test data: anonymization,
pseudonymization, synthetic test-data generation, secure storage of the real data, and automated
prevention of PII ever reaching a git repo again. Deliverable is a generically-written research doc,
useful to any future project.

WHY NOW: a vendored regression corpus was committed containing real employee usernames, and pushed
to two shared branches. The one-off fix is easy. What is missing is an ALGORITHMIC, ROBUST approach,
so the next corpus vendored in will hit the same wall. Framing, verbatim: "surely there's a lot of
information online from ai / software experts and industry and open source implementations and
packages and apps and repos etc that we can adopt best practices and industry standards from to
automate this and also have robust checks to avoid ever committing PII data in the future. maybe git
hooks or whatever."

## Scope note - questions, examples, and named references are a starting point, not a checklist
The questions, topics, and named examples below are illustrative anchors and a FLOOR for this
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

SCOPE, breadth AND depth. All of these are in scope as related topics:

1. ANONYMIZATION vs PSEUDONYMIZATION vs REDACTION vs TOKENIZATION. Not synonyms, and the difference
   is legally and practically load-bearing. Which one does a test corpus actually need, and when is
   reversible pseudonymization the right answer versus irreversible anonymization?
2. WHY NAIVE MASKING FAILS. Format-preserving requirements, referential integrity across files
   (the motivating case: a CSV and its derived TTL must stay in lockstep or the parity test breaks),
   join keys that must remain consistent across tables, and re-identification / linkage attacks.
   k-anonymity, l-diversity, t-closeness and their known limits. Differential privacy: where it
   genuinely applies to test data and where invoking it is cargo cult.
3. SYNTHETIC DATA GENERATION. The real tradeoff is fidelity versus privacy versus effort. Survey
   the actual tooling landscape: structure-preserving generators, statistical/copula approaches, GAN
   and diffusion based tabular synthesis, ISSUE-REF fixtures, and property-based generation.
   For each: does it preserve the structural properties a REGRESSION corpus depends on? A synthetic
   corpus that no longer exercises the same code paths is a silent loss of coverage, which is the
   failure mode to name.
4. DETECTION AND PREVENTION AUTOMATION. Named-entity and pattern based PII scanners, entropy and
   secret scanners, and the difference between the two problem classes. Git-native enforcement:
   pre-commit hooks, pre-push hooks, server-side hooks, CI checks, and protected-branch gates.
   Critically: WHERE in the pipeline each control actually binds, and which of them a developer can
   trivially bypass. A local hook that `--no-verify` defeats is not a control. Also cover the
   false-positive economics, because a scanner that cries wolf gets disabled.
5. SECURE STORAGE OF THE REAL DATA. The working position is that S3 or similar is acceptable and the
   repo is not. Research the best practice rather than assuming: bucket policy and encryption
   posture, access logging, retention, and the pattern for a test suite that needs real data
   OCCASIONALLY (fetch-on-demand with credentials, versus a committed synthetic stand-in, versus a
   two-tier corpus). Name the pattern and its failure modes.
6. REMEDIATION OF DATA ALREADY IN HISTORY. Survey the tooling and the operational reality:
   history-rewriting tools, their tradeoffs, what happens to open pull requests, forks, clones, and
   cached CI artifacts, and whether a rewrite actually achieves deletion on a hosted platform
   (it often does not without provider action). State plainly when rewriting is worth it and when
   the honest answer is to rotate/accept and move on.
7. GOVERNANCE AND CLASSIFICATION. How teams decide what counts as PII in the first place, who signs
   off, and how that decision is recorded so it is not re-litigated per corpus.

METHOD REQUIREMENTS

- Citations for every technical claim. Prefer primary sources: the tool's own docs, the paper, the
  repo. Distinguish what was verified from what is a judgment call.
- Survey as of AUGUST 2026. Anything claimed about a tool's current behavior, version, or
  maintenance status must be checked, not recalled: this is exactly the class of fact that goes stale.
- Where a package is recommended, say whether it is actively maintained, what its license is, and
  whether it handles the referential-integrity requirement in item 2. Dependencies are NOT a cost:
  do not down-rank a capable, industry-standard tool merely for being a third-party dependency.
- The doc must be written GENERICALLY, mentioning no proprietary detail: no employee identifiers, no
  internal taxonomy names, no internal ticket ids, no internal hostnames or account numbers. It may
  refer to the motivating case abstractly ("a vendored regression corpus containing employee
  usernames"). If some finding genuinely cannot be stated without proprietary specifics, omit it and
  note the omission.

AC-1 WHEN the doc ships THEN every one of the seven scope areas is covered with cited sources, or is
explicitly marked out of scope with a reason.
AC-2 WHEN a tool or package is recommended THEN its maintenance status, license, and handling of
cross-file referential integrity are stated.
AC-3 WHEN a prevention control is recommended THEN the doc states where it binds in the pipeline and
how a developer could bypass it.
AC-4 IF a claim could not be verified against a primary source THEN it is labelled as unverified
rather than asserted.
AC-5 WHEN the doc ships THEN it contains no proprietary information, verified by a search for
employee identifiers, internal taxonomy names, and internal ticket ids.

<grounding_instructions>
You are a principal engineer specializing in data privacy engineering and test-data
infrastructure. You have built de-identification pipelines for regulated data and have
watched masking schemes break referential integrity in production. You have strong
opinions backed by evidence. When you cannot find a source, you say so explicitly.

Temporal scope: Weight sources by recency - 2026 (primary) -> 2025 -> 2024.
Pre-2024 sources are background context only unless foundational to the topic.
If post-2024 literature is genuinely sparse for a subtopic, state
"[subtopic]: no significant post-2024 developments found" rather than
backfilling with older sources. Backfilling is a failure mode, not a hedge.
Broaden the window where the topic's relevant literature legitimately predates 2024
- privacy-model theory (k-anonymity and successors), regulatory instruments, and NIST
cryptographic recommendations are foundational and should be cited with their year.

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
  tech blogs (2024-2026). Carry an inline footnote ref to the source: [VERIFIABLE][^N].
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
  URL or DOI and an access-verification stamp. Worked example:
    [^1]: LangChain. (2026). [Threads](https://docs.langchain.com/langsmith/threads).
    LangSmith Documentation. Verified accessible (HTTP 200) 2026-06-03. (Scope note.)
- URL-or-DOI ALWAYS: every source entry carries a clickable URL or a doi.org link -
  paywalled/gated is fine (link it anyway; stamp the access status). Only the
  truly-irreducible case (no online catalog presence anywhere) gets an explicit
  [no online source located] marker with a one-line justification.
- Integrity: footnote refs are contiguous from [^1], every [^N] ref has a matching
  definition, and every definition is referenced - no gaps, no orphans.
- [INFERENCE] / [NO SOURCE] claims carry the tier tag with NO footnote ref.

Hard constraint (overrides all formatting preferences): never invent a citation
to satisfy a formatting instruction. Accuracy > completeness.

Format diagrams using Mermaid.js or ASCII. Format math using LaTeX.
NEVER generate binary images.
</grounding_instructions>

RETRIEVAL ENFORCEMENT: For each load-bearing claim, make at least one search or fetch call. Do not
return [VERIFIABLE] without a URL actually fetched in-session. Do not stop after one round. If no
source is found for a sub-question, run additional searches across alternate framings before tagging
it [NO SOURCE] - that tag is reserved for genuine evidence absence, not retrieval avoidance.
````

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

Every row's quote was confirmed to appear literally in the page fetched in-session on
2026-08-05 (whitespace-normalized substring check). URLs are those actually fetched.

| Claim | Source URL | Verbatim quote | Verdict | Live? |
|---|---|---|---|---|
| Pseudonymization is not anonymization | ec.europa.eu/.../wp216_en.pdf | "pseudonymisation is not a method of anonymisation. It merely reduces the linkability of a dataset with the original identity of a data subject" | SUPPORTED | Yes (HTTP 200, text extracted) |
| Pseudonymization alone leaves data non-anonymous | ec.europa.eu/.../wp216_en.pdf | "The natural person is therefore still likely to be identified indirectly; accordingly, pseudonymisation when used alone will not result in an anonymous dataset." | SUPPORTED | Yes |
| Anonymized data retains residual risk | ec.europa.eu/.../wp216_en.pdf | "an anonymised dataset can still present residual risks to data subjects" | SUPPORTED | Yes |
| Anonymization means irreversible prevention of identification | ec.europa.eu/.../wp216_en.pdf | "in order to irreversibly prevent identification" | SUPPORTED | Yes |
| Identifiability is judged on means reasonably likely | gdpr-info.eu/recitals/no-26/ | "all the means reasonably likely to be used, such as singling out" | SUPPORTED | Yes (HTTP 200) |
| Greenmask determinism | docs.greenmask.io/latest/ | "This ensures that the same input data will always produce the same output data." | SUPPORTED | Yes (HTTP 200) |
| Greenmask is PostgreSQL-scoped | docs.greenmask.io/latest/ | "PostgreSQL database anonymization and synthetic data generation tool" | SUPPORTED | Yes |
| Greenmask builds an FK dependency graph | docs.greenmask.io/latest/database_subset/ | "Greenmask builds a table dependency graph based on the introspected schema and existing foreign keys" | SUPPORTED | Yes (HTTP 200) |
| Greenmask needs declared virtual references for implicit FKs | docs.greenmask.io/latest/database_subset/ | "allowing the declaration of virtual references in the configuration" | SUPPORTED | Yes |
| Presidio needs a consistent salt for referential integrity | presidio.dataprivacystack.org/anonymizer/ | "If you need referential integrity (same values getting the same hash), you must provide a consistent salt" | SUPPORTED | Yes (HTTP 200) |
| Presidio is stateless | presidio.dataprivacystack.org/anonymizer/ | "Presidio does not store or maintain stateful sessions." | SUPPORTED | Yes |
| Presidio does not guarantee full PII recall | presidio.dataprivacystack.org/ | "there is no guarantee that Presidio will find all sensitive information" | SUPPORTED | Yes (HTTP 200) |
| Presidio moved to community governance | raw.githubusercontent.com/data-privacy-stack/presidio/main/docs/project_transition.md | "transitioning from a Microsoft-owned project to an independent, community-governed open source project" | SUPPORTED | Yes (HTTP 200) |
| Presidio stays MIT | raw.githubusercontent.com/.../project_transition.md | "Presidio will continue to be open source under the MIT license." | SUPPORTED | Yes |
| Synthetic data does not improve the tradeoff | arxiv.org/abs/2011.07018 | "does not provide a better tradeoff between privacy and utility than traditional anonymisation techniques" | SUPPORTED | Yes (HTTP 200) |
| Synthetic data fails on one axis or the other | arxiv.org/abs/2011.07018 | "synthetic data either does not prevent inference attacks or does not retain data utility" | SUPPORTED | Yes |
| pre-commit hook is bypassable | git-scm.com/docs/githooks | "can be bypassed with the `--no-verify` option" | SUPPORTED | Yes (HTTP 200) |
| pre-push hook is fully bypassable | git-scm.com/docs/git-push | "With `--no-verify`, the hook is bypassed completely." | SUPPORTED | Yes (HTTP 200) |
| Required status checks gate merges | docs.github.com/.../available-rules-for-rulesets | "all required status checks must pass before collaborators can merge changes into the branch or tag" | SUPPORTED | Yes (HTTP 200) |
| Update restriction blocks direct pushes | docs.github.com/.../available-rules-for-rulesets | "only users with bypass permissions can push to branches" | SUPPORTED | Yes |
| Push protection blocks before arrival | docs.github.com/.../about-push-protection | "push protection blocks pushes that contain secrets before they reach your repository" | SUPPORTED | Yes (HTTP 200) |
| Write access can bypass push protection | docs.github.com/.../about-push-protection | "anyone with write access to the repository can bypass push protection by specifying a bypass reason" | SUPPORTED | Yes |
| Rewrite alone leaves data accessible | docs.github.com/.../removing-sensitive-data-from-a-repository | "If you only rewrite your history and force push it, the commits with sensitive data may still be accessible elsewhere" | SUPPORTED | Yes (HTTP 200) |
| Cached SHA-addressable views survive | docs.github.com/.../removing-sensitive-data-from-a-repository | "Directly via their SHA-1 hashes in cached views on GitHub" | SUPPORTED | Yes |
| Forks keep the data | docs.github.com/.../removing-sensitive-data-from-a-repository | "If the commit that introduced the sensitive data exists in any forks, it will continue to be accessible there." | SUPPORTED | Yes |
| Other clones are out of reach | docs.github.com/.../removing-sensitive-data-from-a-repository | "You cannot remove sensitive data from other users' clones of your repository" | SUPPORTED | Yes |
| Rotation may make rewriting unwarranted | docs.github.com/.../removing-sensitive-data-from-a-repository | "Going through the extra steps to rewrite the history and remove the secret may not be warranted." | SUPPORTED | Yes |
| Support is gated on non-mitigable risk | docs.github.com/.../removing-sensitive-data-from-a-repository | "will only assist in the removal of sensitive data in cases where we determine that the risk can't be mitigated" | SUPPORTED | Yes |
| Close pull requests before rewriting | docs.github.com/.../removing-sensitive-data-from-a-repository | "We recommend merging or closing all open pull requests before removing files from your repository." | SUPPORTED | Yes |
| Pre-receive hooks run on the appliance | docs.github.com/en/enterprise-server@latest/.../about-pre-receive-hooks | "Pre-receive hooks are scripts that run on the GitHub Enterprise Server appliance" | SUPPORTED | Yes (HTTP 200) |
| Pre-receive accept/reject is exit-code driven | docs.github.com/.../about-pre-receive-hooks | "accepted if the exit status is 0, or rejected if the exit status is non-zero" | SUPPORTED | Yes |
| Ruleset bypass can include admins/teams/apps | docs.github.com/.../about-rulesets | "This can be users with a certain role, such as repository administrator, or it can be specific teams or GitHub Apps." | SUPPORTED | Yes (HTTP 200) |
| Gitleaks is security-patches-only with a named successor | github.com/gitleaks/gitleaks/blob/master/README.md (also via `gh api .../contents/README.md`) | "Gitleaks is feature complete. I'm not merging new features into Gitleaks. Future releases will be security patches only. I'm shifting my focus to [Betterleaks]" | SUPPORTED | Yes (HTTP 200; re-confirmed by decoding the raw README via the API) |
| Betterleaks is real and active | gh api repos/betterleaks/betterleaks | `"license":"MIT"`, `"archived":false`, `"pushed_at":"2026-08-05T16:57:53Z"` | SUPPORTED | Yes (API 200) |
| Gitleaks allowlists reduce false positives | github.com/gitleaks/gitleaks/blob/master/README.md | "You can define multiple allowlists for a rule to reduce false positives." | SUPPORTED | Yes |
| Gitleaks baselines suppress pre-existing findings | github.com/gitleaks/gitleaks/blob/master/README.md | "When using a baseline, gitleaks will ignore any old findings that are present in the baseline" | SUPPORTED | Yes |
| Secret-tool precision figures | arxiv.org/abs/2307.00714 | "GitHub Secret Scanner (75%), Gitleaks (46%), and Commercial X (25%)" | SUPPORTED | Yes (HTTP 200; DOI cross-checked via OpenAlex) |
| Secret-tool recall figures | arxiv.org/abs/2307.00714 | "Gitleaks (88%), SpectralOps (67%) and TruffleHog (52%)" | SUPPORTED | Yes |
| False positives trace to generic regex and entropy | arxiv.org/abs/2307.00714 | "generic regular expressions and ineffective entropy calculation" | SUPPORTED | Yes |
| pre-commit installs a git hook script | pre-commit.com | "pre-commit installed at .git/hooks/pre-commit" | SUPPORTED | Yes (HTTP 200) |
| pre-commit is endorsed for CI | pre-commit.com | "adding `pre-commit run --all-files` as a CI step will ensure everything stays in tip-top shape" | SUPPORTED | Yes |
| SDV is Business Source Licensed | gh api repos/sdv-dev/SDV/contents/LICENSE | "Business Source License 1.1" / "Licensor: DataCebo, Inc." | SUPPORTED | Yes (API 200, base64 decoded) |
| SDV restricts Synthetic Data Service use | gh api repos/sdv-dev/SDV/contents/LICENSE | "provided that you do not use the Licensed Work, or derivatives of the Licensed Work, for a Synthetic Data Service" | SUPPORTED | Yes |
| Neosync is archived | gh api repos/nucleuscloud/neosync | `"archived":true` with `"pushed_at":"2025-08-30T18:22:04Z"` | SUPPORTED | Yes (API 200) |
| HMASynthesizer models parent-child relations | docs.sdv.dev/.../hmasynthesizer.md | "uses a recursive technique to model the parent-child relationships of a multi-table datasets" | SUPPORTED | Yes (HTTP 200) |
| HMASynthesizer has a scale limit | docs.sdv.dev/.../hmasynthesizer.md | "The algorithm is optimized for smaller datasets with around 5 tables and 1 level of depth" | SUPPORTED | Yes |
| Faker determinism is version-scoped | faker.readthedocs.io/en/master/index.html | "results are not guaranteed to be consistent across patch versions" | SUPPORTED | Yes (HTTP 200) |
| Faker unique can collide | faker.readthedocs.io/en/master/index.html | "collisions are more likely than you'd think" | SUPPORTED | Yes |
| DP is a property of a method not a dataset | arx.deidentifier.org/overview/privacy-criteria/ | "privacy protection is not considered a property of a dataset, but a property of a data processing method" | SUPPORTED | Yes (HTTP 200) |
| t-closeness constrains distributional distance | arx.deidentifier.org/overview/privacy-criteria/ | "the distributions of values of a sensitive attribute within each equivalence class must have a distance of not more than t" | SUPPORTED | Yes |
| Population-uniqueness estimators assume uniform sampling | arx.deidentifier.org/overview/privacy-criteria/ | "assume that the dataset is a uniform sample of the population" | SUPPORTED | Yes |
| FF3 removed in the 2025 draft | csrc.nist.gov/pubs/sp/800/38/g/r1/2pd | "The encryption method FF3 is no longer specified." | SUPPORTED | Yes (HTTP 200) |
| FF3 was renamed FF3-1 after attacks | csrc.nist.gov/pubs/sp/800/38/g/r1/ipd | "The revised FF3 is named FF3-1." | SUPPORTED | Yes (HTTP 200) |
| Small domains were the vulnerability | csrc.nist.gov/pubs/sp/800/38/g/r1/ipd | "several sets of researchers have identified vulnerabilities when the number of possible inputs" | SUPPORTED | Yes |
| SP 800-38G specified FF1 and FF3 | csrc.nist.gov/pubs/sp/800/38/g/upd1/final | "This Recommendation specifies two methods, called FF1 and FF3, for format-preserving encryption." | SUPPORTED | Yes (HTTP 200) |
| BFG protects the latest commit | rtyley.github.io/bfg-repo-cleaner/ | "By default the BFG doesn't modify the contents of your _latest_ commit" | SUPPORTED | Yes (HTTP 200) |
| BFG speed claim | rtyley.github.io/bfg-repo-cleaner/ | "10 - 720x faster than `git-filter-branch`" | SUPPORTED | Yes |
| git-filter-repo is the recommended tool | raw.githubusercontent.com/newren/git-filter-repo/main/README.md | "git filter-repo is now recommended by the git project" | SUPPORTED | Yes (HTTP 200) |
| git-filter-repo expects a fresh clone | raw.githubusercontent.com/newren/git-filter-repo/main/README.md | "detecting and bailing if we're not in a fresh clone" | SUPPORTED | Yes |
| git-filter-repo is MIT | gh api repos/newren/git-filter-repo/contents/COPYING | "are provided under the MIT license" | SUPPORTED | Yes (API 200) |
| Hypothesis is MPL-2.0 | gh api repos/HypothesisWorks/hypothesis/contents/LICENSE.txt | "released under the Mozilla Public License v 2.0" | SUPPORTED | Yes (API 200) |
| Gitleaks is MIT | gh api repos/gitleaks/gitleaks/contents/LICENSE | "MIT License" / "Copyright (c) 2019 Zachary Rice" | SUPPORTED | Yes (API 200) |
| NIST PII guidance is context-based and org-tailored | csrc.nist.gov/pubs/sp/800/122/final | "practical, context-based guidance for identifying PII and determining what level of protection is appropriate" | SUPPORTED | Yes (HTTP 200) |
| Personal data includes indirect identification | gdpr-info.eu/ISSUE-REF-gdpr/ | "any information relating to an identified or identifiable natural person" | SUPPORTED | Yes (HTTP 200) |
| S3 read logging capability | docs.aws.amazon.com/AmazonS3/latest/userguide/security-best-practices.html | "Server access logging provides detailed records of the requests that are made to a bucket" | SUPPORTED | Yes (HTTP 200) |
| Long-lived keys are discouraged | docs.aws.amazon.com/.../security-best-practices.html | "These are long-term credentials that are not automatically rotated and could have a significant business impact if they are compromised." | SUPPORTED | Yes |
| Graph anonymization metrics are topological | arxiv.org/abs/2501.18625 | "average distance or betweenness centrality" | SUPPORTED | Yes (HTTP 200) |
| Synthetic-data quality is subjective | arxiv.org/abs/2004.07740 | "a subjective concept" | SUPPORTED | Yes (HTTP 200) |
| Presidio operator semantics | presidio.dataprivacystack.org/anonymizer/ | "redact" - "Remove the PII completely from text" | SUPPORTED | Yes |
| Narayanan and Shmatikov paper exists | api.crossref.org/works?query.bibliographic=... | "Robust De-anonymization of Large Sparse Datasets" / DOI 10.1109/sp.2008.33 | SUPPORTED | Yes (Crossref API 200) |
| l-diversity paper exists | api.crossref.org/works?query.bibliographic=... | "L-diversity: privacy beyond k-anonymity" / DOI 10.1109/icde.2006.1 | SUPPORTED | Yes (Crossref API 200) |
| t-closeness paper exists | api.crossref.org/works?query.bibliographic=... | "t-Closeness: Privacy Beyond k-Anonymity and l-Diversity" / DOI 10.1109/icde.2007.367856 | SUPPORTED | Yes (Crossref API 200) |

**Downgraded / unverified, recorded rather than dropped:**

| Claim | Status | Action taken |
|---|---|---|
| Operational false-positive figures (triage hours per week, share of teams disabling tooling, trust loss after N false positives) | UNVERIFIABLE - only vendor-blog sources located; no primary study | Labelled [UNVERIFIED] in section 4; the peer-reviewed precision figures are used instead |
| Whether the hosted git platform offers a pre-receive-hook equivalent | UNVERIFIABLE - the documentation references only the self-hosted product and never states the limitation | Labelled [UNVERIFIED] in section 4 and raised as Open Question 1 |
| pre-commit framework license | UNVERIFIABLE from the documentation site fetched | Marked [UNVERIFIED] in the Comparison table rather than asserted |
| BFG maintenance status | Not checked this run | Marked [UNVERIFIED] in the Comparison table |
| GitHub API for `microsoft/presidio` | Resolves (HTTP 200) but returns `data-privacy-stack/presidio` | Recorded in [^33]; treated as evidence of the transfer, not as two repositories |
| `arxdeidentifier/arx` GitHub path | 404 | Corrected to `arx-deidentifier/arx`, which resolves; no claim made from the failed path |
| Article 29 WP216 via the EDPB host | 404 | Fell back to the European Commission host, fetched successfully, text extracted locally |
| Direct GitHub REST calls via WebFetch | 403 (rate limit) | Re-run through authenticated `gh api`; no metadata claim rests on an unfetched source |
| Original SDV docs URL for HMASynthesizer | 307/404 to a not-found page | Corrected path fetched successfully; the error page was not quoted |

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

**Run 1 - 2026-08-05, `opus researcher` (Claude Opus 5, Bedrock), research-doc skill, ad-hoc web method.**
Search route: an MCP-provided web-search tool (native WebSearch unavailable on this host);
deep reads via WebFetch; repository metadata and license files via authenticated `gh api`;
scholarly existence checks via the Crossref and OpenAlex metadata APIs; one PDF fetched with
`curl` and extracted with `pdftotext` after the primary host 404'd.

Five-perspective critic pass ran. Findings resolved:
- *skeptic* (high): operational false-positive figures traced only to vendor blogs. Resolved by
  demoting them to [UNVERIFIED] and substituting the peer-reviewed ESEM precision/recall figures.
- *skeptic* (high): three tool claims rested on recall rather than a live check. Resolved by
  checking every recommended tool's license and `pushed_at`/`archived` via API, which surfaced
  four material corrections (Neosync archived; SDV/CTGAN under Business Source License 1.1 rather
  than an OSI license; Presidio moved to a new organization; Gitleaks security-patches-only with
  a named successor project, found only on a citation spot-check re-read and verified against the
  successor's own repository metadata).
- *conventional* (medium): the mainstream framing treats synthetic data as a privacy upgrade.
  Resolved by leading with the USENIX Groundhog Day result as disconfirming evidence.
- *contrarian* (medium): the premise that history should be cleaned. Resolved in section 6 by
  stating the rotate-and-move-forward case and noting that it does not transfer to non-rotatable
  identifiers.
- *historical* (medium): the k-anonymity family was initially presented as a menu. Resolved by
  presenting it as a chain of successive fixes to demonstrated attacks, and by adding the
  FF1/FF3/FF3-1 history as a parallel caution.
- *adjacent* (low): supply-chain provenance attestation as a better-engineered analogue to the
  corpus provenance record. Recorded in section 8 rather than developed.
Convergence: committed after one round; no high-severity gap remained open.

<!-- /doc:region name="run_history" -->
