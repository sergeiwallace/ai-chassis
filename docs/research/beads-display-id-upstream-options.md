---
title: "Beads dual-ID capability and upstream options"
category: research
tags: [research, beads, golang, upstream]
status: complete
source: "codex-research-2026-07-26"
template_version: "research-1.2.0"
---

# Beads dual-ID capability and upstream options

**Status:** complete

**Created:** 2026-07-26

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [Q1. Existing capability](#q1-existing-capability)
  - [The shipped extensibility boundary](#the-shipped-extensibility-boundary)
  - [Configuration, hooks, templates, and direct SQL](#configuration-hooks-templates-and-direct-sql)
  - [Zero-change verdict](#zero-change-verdict)
- [Q2. `external_ref` indexing, uniqueness, and ownership](#q2-external_ref-indexing-uniqueness-and-ownership)
  - [Schema and duplicate behavior](#schema-and-duplicate-behavior)
  - [Beads itself writes this field](#beads-itself-writes-this-field)
- [Q3. Upstream change candidates](#q3-upstream-change-candidates)
  - [Candidate i: generic user-defined fields](#candidate-i-generic-user-defined-fields)
  - [Candidate ii: first-class `display_id`](#candidate-ii-first-class-display_id)
  - [Candidate iii: auto-sequenced `external_ref`](#candidate-iii-auto-sequenced-external_ref)
  - [Ranking](#ranking)
- [Q4. Prior art and contribution posture](#q4-prior-art-and-contribution-posture)
  - [Shipped, proposed, and rejected](#shipped-proposed-and-rejected)
  - [Maintenance and contribution posture](#maintenance-and-contribution-posture)
- [Q5. Concurrency inside Beads](#q5-concurrency-inside-beads)
- [Q6. Fork-avoidance decision](#q6-fork-avoidance-decision)
- [Recommendation](#recommendation)
- [Gaps and Open Questions](#gaps-and-open-questions)
- [Sources](#sources)
- [Ambiguous Items from Auto-Remediation (Post-Run Review)](#ambiguous-items-from-auto-remediation-post-run-review)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

<!-- doc:region name="context" kind="immutable" -->

## Context

This research evaluates whether Beads (`bd`) can maintain both its hash primary ID and a per-repository, sequential, human-readable display ID that is scalar, sortable, indexed, uniquely constrained, and assigned on every create path. The current fleet workaround stores `PREFIX-N` in `external_ref`, mirrors it as an `id:PREFIX-N` label, and allocates it in an opt-in wrapper protected by `flock`; a direct `bd create` bypassed that wrapper.

**Primary period:** 2025–2026

**Source weighting:** Beads v1.1.0 tagged source is authoritative for shipped behavior; current GitHub issues, discussions, PRs, and contribution guidance establish upstream posture; older SQLite behavior is historical context only.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

[VERIFIABLE] The installed Homebrew version in scope, v1.1.0, is also the latest stable GitHub release as of 2026-07-26; GitHub records it as released on 2026-07-04. Findings labeled “v1.1.0” below come from the immutable tag, not `main`.[^1]

[VERIFIABLE] The canonical repository is now `github.com/gastownhall/beads`; v1.1.0 source still uses the historical Go import path `github.com/steveyegge/beads`, and old GitHub URLs redirect. That naming difference does not indicate two codebases.[^2]

[HEURISTIC] Source retrieval used the live tagged GitHub tree and raw files because shell network access was unavailable. Every cited URL was fetched successfully in-session. No cited URL returned 404, 403, or DNS failure; the GitHub recursive-tree API URL was rejected by the browsing safety layer and was not used as evidence.

## Executive Summary

[VERIFIABLE] **No shipped, supported Beads mechanism meets the full need.** v1.1.0 has a JSON `metadata` extension point with typed validation and equality/existence filtering, but not metadata sorting, a metadata index, a per-key unique constraint, or a create-time sequence generator. `types.custom` and `status.custom` extend enum values only, and hooks fire after the mutation.[^3][^4][^7][^10]

[VERIFIABLE] **`external_ref` is indexed but is not uniquely constrained in the v1.1.0 Dolt schema.** Two issues in a v1.1.0 Dolt store can carry the same non-NULL value, and tracker pull/push code actively assigns and overwrites this field. It is therefore unsafe as the fleet’s canonical display-ID namespace.[^4]

[INFERENCE] The fork-avoidance answer is **(b): this needs an upstream PR**. The smallest complete, coherent PR is candidate (ii): an opt-in, first-class numeric `display_id`, rendered as `PREFIX-N`, allocated in the same database transaction as issue creation, backed by a unique index, while retaining the hash primary ID. Generic custom fields fit the project’s extensibility direction better but are substantially larger; sequencing `external_ref` is semantically incompatible with shipped tracker behavior.

## Q1. Existing capability

### The shipped extensibility boundary

[VERIFIABLE] The v1.1.0 `Issue` model has fixed Go fields. Its two potentially relevant extension fields are `ExternalRef *string` and `Metadata json.RawMessage`; the latter is documented in code as arbitrary JSON and validation only establishes that it is well-formed JSON.[^5]

```go
// internal/types/types.go:47-53
ExternalRef *string         `json:"external_ref,omitempty"`
SourceSystem string         `json:"source_system,omitempty"`
Metadata     json.RawMessage `json:"metadata,omitempty"`
```

[VERIFIABLE] Metadata schema enforcement is shipped. A repository YAML schema can declare metadata keys of type `string`, `int`, `float`, `bool`, or `enum`, mark them required, and apply value bounds. The create path invokes that validator before storage.[^6]

[VERIFIABLE] Metadata is queryable only in a limited sense: `bd list` exposes top-level equality and key-existence filters, implemented with `JSON_EXTRACT`/`JSON_UNQUOTE`. The v1.1.0 schema has only the JSON column and no metadata index, and the CLI sort allow-list contains fixed native fields rather than metadata paths.[^7]

```sql
-- internal/storage/sqlbuild/filter.go:223-239
JSON_EXTRACT(metadata, ?) IS NOT NULL
JSON_UNQUOTE(JSON_EXTRACT(metadata, ?)) = ?
```

[INFERENCE] A repository could declare a required integer metadata key such as `display_id`; that would prevent a plain create that omits it rather than silently accepting a missing ID. It still would not auto-assign the number, sort by it through the normal CLI, index it, or enforce uniqueness. This is a useful guardrail but not a solution.

[VERIFIABLE] `types.custom` and `status.custom` extend enum **values**, not issue fields. Migration `0024` creates normalized `custom_types` and `custom_statuses` lookup tables; it does not alter the `issues` table or the `Issue` struct.[^8]

### Configuration, hooks, templates, and direct SQL

[VERIFIABLE] The implemented `bd config set` recognizer accepts these namespaces: `export.`, `import.`, `dolt.`, `jira.`, `linear.`, `github.`, `custom.`, `status.`, `doctor.suppress.`, `routing.`, `sync.`, `git.`, `directory.`, `repos.`, `external_projects.`, `validation.`, `hierarchy.`, `ai.`, `backup.`, `federation.`, and `metrics.`. It also recognizes a small fixed scalar-key list. Unknown keys emit a warning but are still stored; storage of an arbitrary config value does not create an issue field, index, constraint, or create interceptor.[^9]

[INFERENCE] The `custom.*` namespace is an integration-configuration escape hatch, not a custom-field declaration language. Repository searches found consumers for tracker configuration, custom type/status values, validation, and metadata schemas, but no consumer that maps an arbitrary config key to a new issue field.

[VERIFIABLE] The general hook system cannot assign this invariant atomically. `HookFiringStore.CreateIssue` first calls the underlying store and fires hooks only after success; transaction hooks are delayed until after commit. Hook execution is asynchronous/fire-and-forget, and failures are intentionally non-blocking.[^10]

[INFERENCE] A post-create hook could race to issue an update, but `bd create` would already have returned successfully with no display ID and a hook failure would not roll back the issue. That reproduces, rather than repairs, the fleet’s invariant gap.

[VERIFIABLE] `issue_id_mode=counter` is a shipped, under-promoted adjacent feature. It uses the existing `issue_counter` table to generate `PREFIX-N` in a transaction, but it **replaces the primary hash ID**. It cannot provide both `ISSUE-REF` and `ISSUE-REF` on one issue.[^11]

[INFERENCE] No v1.1.0 formula, issue template, plugin registry, tracker hook, database trigger installer, or pre-create middleware was found that runs for every generic create path. Tracker `PullHooks` are internal callbacks used only by tracker sync; they are not repository-supplied plugins and do not cover ordinary `bd create`.

[VERIFIABLE] `bd sql` can execute arbitrary write SQL only when connected to a Dolt SQL server; it rejects embedded mode. Manually adding a trigger or unique index would be an out-of-band schema fork that Beads migrations, doctor checks, JSON export/import, and CLI resolution do not own.[^12]

### Zero-change verdict

[INFERENCE] **Q1 answer: no.** The nearest shipped composition—required integer metadata plus a wrapper—can detect omission, but it fails the full scalar/sortable/indexed/unique/auto-assigned requirement. The shipped primary counter fails the “both IDs” requirement, and post-create hooks fail atomicity.

## Q2. `external_ref` indexing, uniqueness, and ownership

### Schema and duplicate behavior

[VERIFIABLE] In the canonical v1.1.0 Dolt migration, `external_ref` is nullable `VARCHAR(255)` and has an ordinary secondary index.[^4]

```sql
-- internal/storage/schema/migrations/0001_create_issues.up.sql:19,58
external_ref VARCHAR(255),
INDEX idx_issues_external_ref (external_ref)
```

[VERIFIABLE] There is no `UNIQUE` keyword on the column or index, and the complete tagged migration directory has no later migration that adds such a constraint.[^4]

[VERIFIABLE] The create implementation inserts the caller’s `external_ref` with the rest of the fixed columns. Its only collision-retry logic concerns the primary issue ID; it does not perform an `external_ref` uniqueness query or validation before insert.[^13]

[VERIFIABLE] The field is genuinely queryable: tracker pull performs an exact `GetIssueByExternalRef` lookup, and general issue search includes `external_ref`. The ordinary schema index can support exact database lookups, but it supplies no cardinality guarantee.[^4][^15]

[INFERENCE] Consequently, two normal v1.1.0 Dolt issue rows can carry the same non-NULL `external_ref`. The fleet’s `flock` prevents two cooperating wrapper processes from choosing the same next number, but it cannot reject a duplicate from plain `bd create`, an API client, raw SQL, an import path, another clone, or tracker sync.

[VERIFIABLE] This differs from historical SQLite behavior: issue #3902 demonstrates an `external_ref` UNIQUE constraint on SQLite v0.47.0. That issue is not evidence of v1.1.0 Dolt uniqueness; the tagged Dolt DDL above is controlling for v1.1.0.[^14]

[NO SOURCE] Whether every fleet store upgraded from SQLite has exactly the canonical v1.1.0 Dolt index shape is not established without inspecting each store. The decisive audit is `SHOW CREATE TABLE issues` (or `SHOW INDEX FROM issues`) against every repository store; do not infer it from upgrade history.

[INFERENCE] Dolt can technically enforce a manually added unique index, but Beads v1.1.0 has no supported config or migration for it. Such a local DDL modification is schema drift, not a portable fleet capability.

### Beads itself writes this field

[VERIFIABLE] The shared tracker engine treats `external_ref` as Beads’ tracker-link field. Pull computes `BuildExternalRef`, looks up an existing issue by it, assigns it on new local issues, and updates a mismatched value. Push writes the tracker-created reference back to the local issue.[^15]

```go
// internal/tracker/engine.go:482-488
conv.Issue.ExternalRef = strPtr(ref)
if err := e.Store.CreateIssue(ctx, conv.Issue, e.Actor); err != nil { ... }

// internal/tracker/engine.go:900-903
ref := e.Tracker.BuildExternalRef(created)
updates := map[string]interface{}{"external_ref": ref}
```

[VERIFIABLE] Jira’s adapter renders the value as `<jira-url>/browse/<key>`, demonstrating that at least one shipped integration derives tracker-owned content for `external_ref`; the shared engine performs the write-back for registered trackers.[^16]

[INFERENCE] A fleet value such as `ISSUE-REF` can therefore block or confuse tracker linkage even without a literal string collision: tracker push treats a value that is not recognized as that tracker’s reference as “unlinked” and may replace it after creating the external issue. Overloading this field also prevents one issue from simultaneously holding a display ID and its intended Jira/Linear/GitHub link.

## Q3. Upstream change candidates

### Candidate i: generic user-defined fields

[INFERENCE] **Design:** keep `metadata` as canonical storage, extend the repository metadata schema with `indexed`, `unique`, `sortable`, and an optional `default: sequence`/`generator: counter`; materialize typed values into a Dolt-owned index table (or generated columns if Dolt’s supported DDL proves adequate). This avoids runtime Go struct generation and follows the already-shipped metadata direction.

[HEURISTIC] **Likely code surface:** `internal/storage/metadata.go` and `dolt/metadata_schema.go`; `internal/storage/schema/migrations/`; `issueops/{create,update,helpers}.go`; `sqlbuild/filter.go`; `internal/types/types.go`; `cmd/bd/{create,list,update}.go`; import/export and doctor/schema-skew code; and tests across embedded/server/merge paths. Estimate: **1,000–2,000 changed lines across roughly 20–30 files**.

[HEURISTIC] **Migration:** add versioned tables such as `issue_metadata_index(issue_id, key, value_type, value_*)` and `metadata_counters(key, last_value)`, with indexes and per-type unique keys; backfill from `issues.metadata`; retain the JSON as source of truth. Dynamic per-repository DDL would be smaller at runtime but harder to make deterministic across clones with different config revisions.

[INFERENCE] **Compatibility question:** what happens when two clones create the same sequential custom value while offline, or when a config change makes existing values invalid/duplicate? A generic design must define merge repair, tombstone reuse, schema-declaration versioning, and whether undeclared metadata remains legal.

[HEURISTIC] **Acceptance/design fit:** highest of the three because it solves a class of integration problems and follows explicit prior maintainer direction toward Dolt-native metadata indexing. **Implementation size:** largest. It is probably too broad for a first contribution unless split behind an accepted design issue.

### Candidate ii: first-class `display_id`

[INFERENCE] **Design:** add nullable numeric `display_id BIGINT` as the canonical scalar; render/resolve it as `<issue_prefix>-<display_id>`; add `display_id.mode=counter` (default off); and allocate the number in the same transaction as the hash primary ID and issue insert. Keep it immutable after assignment except for explicit import/backfill tooling.

[HEURISTIC] **Likely code surface:** one up/down migration under `internal/storage/schema/migrations/`; `internal/types/types.go`; `issueops/{create,helpers,update}.go`; shared issue select/scan/insert code; `cmd/bd/{create,list,show,config}.go`; resolver/search code; import/export representation; and concurrency/migration tests. Estimate: **500–900 changed lines across roughly 12–20 files**.

[HEURISTIC] **Migration:** add nullable `display_id BIGINT`, `UNIQUE INDEX ... (display_id)`, and either a dedicated one-row/per-prefix counter or a namespaced entry in the existing counter table. Existing rows remain NULL, so the schema migration itself is non-destructive. Enabling the feature should require an explicit one-time backfill/import command that validates all proposed values before activating auto-assignment.

[INFERENCE] **Compatibility question:** the maintainer must decide behavior for offline divergent clones, tombstones, prefix renames, explicit imports, counter-mode primary IDs (where `PREFIX-N` is ambiguous), wisps, and whether `display_id` participates in content hashing. A robust rule is never reuse numbers and treat duplicate display IDs as a merge conflict that must be resolved without changing the hash primary ID.

[HEURISTIC] **Acceptance/design fit:** second. It is a clean issue-tracking primitive and the smallest candidate that fully solves this need, but contribution guidance warns against expanding the schema when metadata suffices. The PR rationale must demonstrate that metadata currently does **not** suffice for uniqueness, sorting, resolution, or atomic generation.

### Candidate iii: auto-sequenced `external_ref`

[INFERENCE] **Design:** add an opt-in `external_ref.mode=counter` and reuse the existing transactional counter allocator. To meet the stated invariant, also change the ordinary index to UNIQUE and define import/sync conflict behavior.

[HEURISTIC] **Likely code surface:** `cmd/bd/{config,create}.go`; `issueops/{create,helpers}.go`; the issues-table migration; `internal/tracker/engine.go`; importer compatibility; and tests. Estimate: **250–500 changed lines across roughly 8–12 files**—the smallest raw diff.

[INFERENCE] **Migration:** a UNIQUE-index migration must first detect and report existing duplicates. Historical tombstones, blank strings, imported references, and tracker-linked issues make automatic cleanup unsafe.

[INFERENCE] **Compatibility question:** how can tracker integrations continue to own `external_ref` if the field is also a display sequence? There is no satisfactory answer without adding another tracker-reference field, which simply turns this candidate into a more disruptive version of candidate (ii).

[HEURISTIC] **Acceptance/design fit:** lowest. It contradicts the field’s documented generic external-tracker purpose and actual tracker-engine ownership. Its small diff is misleading because compatibility fallout is the core cost.

### Ranking

| Candidate | Maintainer acceptance | Implementation size | Existing-design fit | Solves full need |
|---|---:|---:|---:|---:|
| (i) Generic metadata-backed custom fields | **1st** [HEURISTIC] | **3rd / largest** [HEURISTIC] | **1st** [HEURISTIC] | Yes, only if sequence generation is included [INFERENCE] |
| (ii) First-class numeric `display_id` | **2nd** [HEURISTIC] | **2nd** [HEURISTIC] | **2nd** [HEURISTIC] | **Yes** [INFERENCE] |
| (iii) Sequence `external_ref` | **3rd** [HEURISTIC] | **1st / smallest** [HEURISTIC] | **3rd** [HEURISTIC] | Superficially; breaks tracker semantics [INFERENCE] |

[INFERENCE] Overall rank for this concrete contribution: **(ii) first, (i) second, (iii) third**. Candidate (i) wins generality and architectural direction but is not the smallest complete PR; candidate (ii) is the best balance of completeness and reviewable scope.

## Q4. Prior art and contribution posture

### Shipped, proposed, and rejected

[VERIFIABLE] **SHIPPED:** optional per-issue JSON metadata landed in PR #1407, and config-driven typed metadata-schema enforcement later shipped before v1.0.0. This is real in v1.1.0 source, not merely the earlier proposal in issue #1416.[^17]

[VERIFIABLE] **PROPOSED, NOT SHIPPED:** discussion #1588 proposed “metadata as canonical storage + derived index” for typed, queryable custom fields. Its follow-up issue #1589 scoped a metadata index, but the v1.1.0 schema contains no such table/index.[^18]

[VERIFIABLE] **CLOSED IMPLEMENTATION, IDEA NOT REJECTED:** PR #1590 attempted the metadata index on SQLite and was closed during the SQLite-to-Dolt transition. The maintainer said the concept remained valuable and explicitly welcomed a PR using Dolt-native indexing. This is the strongest direct signal about likely acceptance.[^19]

[VERIFIABLE] **SHIPPED ADJACENT FEATURE:** issue #2002 requested `issue_id_mode=counter` for sequential primary IDs and is closed as completed; release history includes the implementation. It does not request or implement a second display ID.[^20]

[VERIFIABLE] Searches of issues and discussions for exact “display id” found no direct request, while “custom fields” and “sequential IDs” found the items above. No source located a merged or explicitly rejected dual hash-plus-sequential display-ID proposal as of 2026-07-26.[^18][^20][^21]

[INFERENCE] The absence of an exact prior request means acceptance cannot be inferred from precedent. The custom-field discussion and the maintainer’s Dolt-indexing comment are more probative than issue counts or speculative taste.

### Maintenance and contribution posture

[VERIFIABLE] CONTRIBUTING requires reading the project charter before adding feature surface, says Beads owns issue-tracking primitives, warns against schema expansion when metadata is sufficient, asks for one issue per PR, tests, backward-compatibility analysis, and at least one maintainer approval.[^22]

[VERIFIABLE] Release cadence is active: v1.0.0 was released 2026-04-03, v1.0.4 on 2026-05-09, and v1.1.0 on 2026-07-04. PR #1407 was merged promptly, while #1590 received a concrete transition-related closure rationale rather than silence.[^1][^17][^19][^23]

[HEURISTIC] The project is actively maintained and responsive, but also high-churn and scope-conscious. A focused issue demonstrating the invariant failure and a bounded PR with migration/concurrency tests has a plausible path upstream; a large unsolicited framework is less likely to be reviewable.

## Q5. Concurrency inside Beads

[VERIFIABLE] Beads already has the needed transaction primitive. `GenerateIssueIDInTable` runs inside `*sql.Tx`; counter mode calls `NextCounterIDTx`, whose update increments `issue_counter.last_id` and whose read returns the allocated value. The issue insert occurs in the same transaction.[^11][^24]

```go
// internal/storage/issueops/helpers.go:251-267
UPDATE issue_counter SET last_id = last_id + 1 WHERE prefix = ?
SELECT last_id FROM issue_counter WHERE prefix = ?
```

[VERIFIABLE] Embedded Dolt’s `RunInTransaction` begins a SQL transaction, invokes the callback, commits it, and then records the Dolt version-control commit.[^25]

[INFERENCE] The separately inspected server-mode path also uses SQL transactions. Thus both supported storage modes expose the database transaction boundary needed for in-create allocation; embedded mode alone is already sufficient to establish that the mechanism exists.

[INFERENCE] If display assignment moves into the create transaction, no external `flock` or `max(N)+1` scan is needed for concurrent writers against the same database/session. The existing counter path is a direct implementation template, so local race-free allocation is straightforward rather than a new database research problem.

[VERIFIABLE] Beads’ own counter documentation limits the guarantee: concurrent creates within one Dolt session are safe, but parallel branches/clones can diverge and collide. Hash primary IDs are the project’s answer to distributed, offline creation.[^26]

[INFERENCE] A second sequential display ID therefore cannot promise collision-free independent offline allocation. The honest contract is central/session-local monotonic allocation plus a UNIQUE constraint that exposes divergent-clone collisions at merge. That tradeoff is acceptable only if the fleet’s per-repo stores have an authoritative writer/session or an explicit merge-repair policy.

## Q6. Fork-avoidance decision

[INFERENCE] **Decision: (b) it needs a PR.** It cannot be done today with a supported zero-change composition; no evidence says upstream has rejected the need; and keeping it outside Beads preserves the exact opt-in bypass that already failed. A permanent fork is premature because the closest prior maintainer statement welcomes a Dolt-native indexing contribution.

[INFERENCE] The PR to write is: **“Add opt-in, transactionally allocated numeric display IDs while retaining hash primary IDs.”** Scope it to a nullable `display_id`, unique index, counter allocation during every persistent issue create path, exact `PREFIX-N` resolution, numeric filter/sort, JSON/export/import preservation, and migration/concurrency tests. Do not bundle generic custom fields or change `external_ref`.

## Recommendation

[INFERENCE] Submit candidate (ii) upstream, preceded by the project-required focused issue if maintainers want design agreement. Use the fleet incident as the motivating invariant: a post-create or wrapper-only mechanism cannot guarantee every issue receives the second ID. Point reviewers to the already-shipped transactional counter and make the feature opt-in/default-off so existing repositories and hash-ID semantics do not change.

[INFERENCE] Until that PR ships, keep the wrapper but add a repository-level audit or metadata-schema requirement that makes a missing display ID fail loudly. Also audit every fleet store with `SHOW CREATE TABLE issues`; do not rely on `external_ref` uniqueness, and do not enable tracker sync on repositories where `external_ref` holds display IDs. These are interim containment steps, not the recommended end state.

## Gaps and Open Questions

1. [NO SOURCE] **Fleet schema state:** upgraded databases may not all match the fresh v1.1.0 DDL. Run `SHOW CREATE TABLE issues` and `SHOW INDEX FROM issues` in each store.
2. [NO SOURCE] **Writer topology:** determine whether each repository has one authoritative Dolt session or permits offline independent creation. This decides whether sequential display IDs can be monotonic in practice or only unique after merge resolution.
3. [NO SOURCE] **Migration corpus:** count missing, malformed, and duplicate `external_ref` display values before proposing a backfill command. No safe automatic policy can be chosen without those data.
4. [NO SOURCE] **Maintainer preference between (i) and (ii):** the prior comment favors Dolt-native custom-field indexing, but no maintainer has evaluated this exact dual-ID invariant. A focused upstream issue would settle whether `display_id` is considered a core primitive or metadata.
5. [VERIFIABLE] **Unreleased changes:** issue #4341 (“`bd list --external-ref` filter”) closed after v1.1.0’s release. Any post-2026-07-04 improvement on `main` must not be treated as available in Homebrew v1.1.0 until a later release contains it.[^27]

## Sources

[^1]: Gastown Hall. (2026). [Release v1.1.0](https://github.com/gastownhall/beads/releases/tag/v1.1.0). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Stable release date, tag, and Homebrew installation.)

[^2]: Gastown Hall. (2026). [Jira tracker source at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/internal/jira/tracker.go#L10-L18). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Canonical repository with historical Go module import path.)

[^3]: Gastown Hall. (2026). [Issue filters at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/internal/types/types.go#L1098-L1190). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Metadata filters and fixed sort/filter model.)

[^4]: Gastown Hall. (2026). [Initial issues-table migration at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/schema/migrations/0001_create_issues.up.sql#L1-L60). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Column definitions and ordinary indexes; checked against the [complete tagged migration directory](https://github.com/gastownhall/beads/tree/v1.1.0/internal/storage/schema/migrations).)

[^5]: Gastown Hall. (2026). [Issue type at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/internal/types/types.go#L13-L98). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Fixed issue fields, `external_ref`, and JSON metadata.)

[^6]: Gastown Hall. (2026). [Metadata validator at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/metadata.go#L35-L190). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Supported types and validation constraints; the [create call site](https://github.com/gastownhall/beads/blob/v1.1.0/cmd/bd/create.go#L378-L410) invokes repository-configured validation.)

[^7]: Gastown Hall. (2026). [SQL metadata filter builder at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/sqlbuild/filter.go#L214-L246). GitHub. Verified accessible (HTTP 200) 2026-07-26. (JSON equality/existence predicates.)

[^8]: Gastown Hall. (2026). [Custom status/type tables migration at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/schema/migrations/0024_create_custom_status_type_tables.up.sql). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Normalized enum-value tables.)

[^9]: Gastown Hall. (2026). [`bd config` implementation at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/cmd/bd/config.go). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Unknown-key behavior at lines 101–121; recognized namespaces and keys at lines 803–846.)

[^10]: Gastown Hall. (2026). [Hook-firing storage decorator at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/hook_decorator.go#L1361-L1382). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Post-success and post-commit hook semantics.)

[^11]: Gastown Hall. (2026). [Transactional issue-ID generation at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/issueops/helpers.go#L145-L275). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Hash/counter selection and counter allocation.)

[^12]: Gastown Hall. (2026). [`bd sql` implementation at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/cmd/bd/sql.go#L13-L42). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Direct SQL and embedded-mode restriction.)

[^13]: Gastown Hall. (2026). [Issue creation at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/issueops/create.go#L86-L130). GitHub. Verified accessible (HTTP 200) 2026-07-26. (ID generation, insert, and event flow.)

[^14]: Wildcard. (2026). [`external_ref` empty-string UNIQUE failure on SQLite v0.47.0](https://github.com/gastownhall/beads/issues/3902). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Historical SQLite constraint behavior; not v1.1.0 Dolt DDL.)

[^15]: Gastown Hall. (2026). [Tracker synchronization engine at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/internal/tracker/engine.go#L382-L496). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Pull-time lookup, create, and `external_ref` assignment.)

[^16]: Gastown Hall. (2026). [Jira external-reference builder at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/internal/jira/tracker.go#L303-L312). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Tracker-owned external-reference format.)

[^17]: Adamson, T. (2026). [Add optional per-issue metadata field, PR #1407](https://github.com/gastownhall/beads/pull/1407). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Merged JSON metadata implementation.)

[^18]: Turian, J. (2026). [Architecture for custom fields: metadata indexing vs. dedicated table, discussion #1588](https://github.com/gastownhall/beads/discussions/1588). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Proposed custom-field/index architecture.)

[^19]: Turian, J. (2026). [Add metadata index table for fast custom field queries, PR #1590](https://github.com/gastownhall/beads/pull/1590). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Closed SQLite implementation and maintainer’s Dolt-native follow-up guidance.)

[^20]: Gm2211. (2026). [Support `issue_id_mode=counter` for sequential IDs, issue #2002](https://github.com/gastownhall/beads/issues/2002). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Completed sequential-primary-ID request.)

[^21]: Gastown Hall. (2026). [Issue search for exact “display id”](https://github.com/gastownhall/beads/issues?q=is%3Aissue+%22display+id%22). GitHub. Verified accessible (HTTP 200) 2026-07-26. (No exact issue result at access time.)

[^22]: Gastown Hall. (2026). [Contributing to bd at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/CONTRIBUTING.md#L83-L123). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Scope, workflow, PR hygiene, and compatibility expectations.)

[^23]: Gastown Hall. (2026). [Release v1.0.0](https://github.com/gastownhall/beads/releases/tag/v1.0.0). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Release date and change volume; cadence cross-check: [v1.0.4](https://github.com/gastownhall/beads/releases/tag/v1.0.4).)

[^24]: Gastown Hall. (2026). [Counter table migration at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/schema/migrations/0013_create_issue_counter.up.sql). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Per-prefix last-ID state.)

[^25]: Gastown Hall. (2026). [Embedded Dolt transaction implementation at v1.1.0](https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/embeddeddolt/transaction.go#L683-L739). GitHub. Verified accessible (HTTP 200) 2026-07-26. (SQL transaction and Dolt commit sequence.)

[^26]: Gastown Hall. (2026). [Counter-ID configuration](https://github.com/gastownhall/beads/blob/v1.1.0/docs/CONFIG.md). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Single-session guarantee and divergent-branch tradeoff.)

[^27]: Lushpenko, M. (2026). [Add `bd list --external-ref` filter, issue #4341](https://github.com/gastownhall/beads/issues/4341). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Post-v1.1.0 issue status.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Model:** `Codex / GPT-5`
**Date:** 2026-07-26

```text
You are a senior Go engineer and open-source contributor evaluating whether a needed capability already exists in a codebase, and if not, what the smallest well-fitting upstream change would be.

TARGET REPO TO STUDY: Beads (`bd`) — MIT licensed, Go. Homebrew formula `beads`, currently v1.1.0. Upstream: https://github.com/steveyegge/beads (also referenced as github.com/gastownhall/beads). READ THE ACTUAL SOURCE. Do not infer behavior from `bd --help`; `--help` is exactly what made this look impossible already.

WRITE YOUR FINDINGS INTO the research doc passed as the write target, conforming to the canonical research template (research-1.2.0) at /home/example-user/projects/ai-harness/docs/research/TEMPLATE.md: tier tags on every major claim ([VERIFIABLE] with a GFM footnote, [HEURISTIC], [INFERENCE], [NO SOURCE]), a provenance ledger row (claim -> verbatim quote -> verdict -> live URL) for each [VERIFIABLE] claim, APA-shaped footnotes under Sources, and an Appendix: Research Prompt recording this prompt.

## The concrete need

A fleet of ~20 repos each runs its own bd store. Every issue must carry BOTH bd's content-hash primary id (e.g. `ISSUE-REF`) AND a human-readable, per-repo sequential display id (`ISSUE-REF`). The display id must be: a scalar (sortable), indexed/queryable, and uniquely constrainable per repo.

bd offers no user-defined field, so the fleet OVERLOADED `external_ref` — a field bd documents generically as "External reference (e.g. 'gh-9', 'jira-ABC', Linear URL)" — and additionally mirrors the value into a label `id:PREFIX-N` for exact CLI resolution. Assignment happens OUTSIDE bd, in a 231-line wrapper that holds `flock .beads/.displayid.lock`, computes `max(N)+1` by scanning existing `external_ref` values, then calls `bd create --external-ref PREFIX-N --labels id:PREFIX-N`.

Because that wrapper is a SEPARATE OPT-IN COMMAND, a plain `bd create` silently produces an issue with no display id. That happened and went undetected until a later audit.

## Questions, in priority order

Q1. **Does bd ALREADY support this, undocumented or under-documented?** Read the source for: the issue schema and its migrations; any user-defined/custom/extra field mechanism; how `types.custom` and `status.custom` are implemented (these prove bd already has a per-repo extensibility pattern — establish whether that pattern extends to FIELDS or only to enum VALUES); the full `bd config` key surface as implemented, not as documented; whether any hook, plugin, trigger, or template mechanism could assign a field at create time. If a way exists to do this today with zero upstream change, that is the single most valuable finding — lead with it.

Q2. **Is `external_ref` genuinely indexed and uniquely constrainable?** Find its column definition, indexes, and any UNIQUE constraint. Establish whether two issues in one store can hold the same `external_ref` (the fleet assumes not, and built a flock on that assumption). Also determine whether bd itself ever writes/derives `external_ref` (e.g. during import, sync, or a tracker integration) in a way that would collide with a fleet using it as a display id.

Q3. **What is the smallest well-fitting upstream change?** Evaluate these THREE candidates against the actual code, and rank them by (a) likelihood a maintainer accepts it, (b) implementation size, (c) how well it fits bd's existing design:
   (i) **Generic user-defined fields** — let a repo declare additional typed, indexed, optionally-unique fields via config. Most general: it solves a class of problem (anyone syncing an external tracker, wanting a sortable sprint number, or a queryable component hits the same wall and ends up overloading external_ref). Hardest to reject on design grounds, since types.custom/status.custom already establish per-repo extensibility.
   (ii) **A first-class `display_id` field** with optional per-repo auto-sequencing. Narrower, still semantically clean.
   (iii) **Auto-sequencing `external_ref`** itself. Smallest diff, but fights that field's documented generic purpose and is the most likely to be rejected.
   For each: what files/packages change, roughly how many lines, what migration is needed for existing stores, and what backward-compatibility question a maintainer would raise.

Q4. **Is anyone already asking for this?** Search the upstream issue tracker, discussions, PRs, and changelog for: user-defined/custom fields, display ids, human-readable ids, sequential ids, external_ref semantics, multi-repo id schemes. Report anything open, merged, or explicitly rejected — a prior rejection with a maintainer's stated reasoning is worth more than any amount of speculation about what they'd accept. Also report the project's contribution posture: CONTRIBUTING guidance, PR responsiveness, release cadence, whether it is actively maintained.

Q5. **What does the concurrency story look like inside bd?** The fleet's wrapper uses an flock because it computes max(N)+1 outside the database. If assignment moved inside bd, does bd already have a transaction/locking primitive that makes sequential assignment race-free (it is Dolt-backed — does it expose transactions)? Establish this from the source; it determines whether an upstream implementation is simple or genuinely hard.

Q6. **Fork-avoidance check.** The requester explicitly does NOT want to maintain a fork indefinitely. Given Q1-Q5, state plainly which of these is true: (a) it can be done today with no upstream change; (b) it needs a PR, and here is the specific one to write; (c) it needs a fork because upstream would not take it, and here is why; (d) the need should be met outside bd entirely. Give ONE recommendation with reasoning, not a menu.

## Method requirements
- Clone or browse the actual repository. Quote real code with file paths and line numbers. A claim about the schema without the schema definition quoted is not established.
- Distinguish SHIPPED from PROPOSED from REJECTED. This fleet has already been burned by treating an open proposal as a feature.
- Verify every citation is live; classify failures (404/403/DNS) rather than dropping them silently.
- Where the source is ambiguous or you cannot reach it, write "not established" and say exactly what would settle it. This feeds a decision about whether to write an upstream PR; a confident wrong answer wastes real contributor effort.
- Note the installed version is 1.1.0 via Homebrew — flag any finding that applies only to a newer/unreleased upstream state.
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

| Claim | Verbatim source quote | Verdict | Live URL |
|---|---|---|---|
| V1 — v1.1.0 is the 2026-07-04 stable release | “v1.1.0”; “released this 04 Jul” | ENTAILED | https://github.com/gastownhall/beads/releases/tag/v1.1.0 |
| V2 — canonical owner and historical Go path | `github.com/steveyegge/beads/internal/config` | ENTAILED | https://github.com/gastownhall/beads/blob/v1.1.0/internal/jira/tracker.go#L10-L18 |
| V3 — no shipped composition meets all requirements | `MetadataFields map[string]string`; “Hooks fire AFTER” | ENTAILED BY CONJUNCTION | https://github.com/gastownhall/beads/blob/v1.1.0/internal/types/types.go#L1098-L1190 |
| V4 — v1.1.0 external ref is indexed, non-unique, and tracker-written | `INDEX idx_issues_external_ref (external_ref)`; `ExternalRef = strPtr(ref)` | ENTAILED BY CONJUNCTION | https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/schema/migrations/0001_create_issues.up.sql#L52-L59 |
| V5 — `Issue` has fixed external-ref and metadata fields | `Metadata json.RawMessage` | ENTAILED | https://github.com/gastownhall/beads/blob/v1.1.0/internal/types/types.go#L47-L53 |
| V6 — typed metadata validation runs on create | `Type string`; `ValidateMetadataIfConfigured` | ENTAILED | https://github.com/gastownhall/beads/blob/v1.1.0/cmd/bd/create.go#L378-L410 |
| V7 — metadata query is JSON equality/existence without metadata index/sort | `JSON_EXTRACT(metadata, ?)` | ENTAILED BY CONJUNCTION | https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/sqlbuild/filter.go#L214-L246 |
| V8 — custom types/statuses are lookup values | `CREATE TABLE IF NOT EXISTS custom_types` | ENTAILED | https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/schema/migrations/0024_create_custom_status_type_tables.up.sql |
| V9 — config recognizes prefixes but stores unknown keys after warning | `Warning: Unrecognized config key`; `HasPrefix(key, "custom.")` | ENTAILED | https://github.com/gastownhall/beads/blob/v1.1.0/cmd/bd/config.go |
| V10 — hooks are post-success/post-commit | “Hooks fire AFTER successful mutations” | ENTAILED | https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/hook_decorator.go#L1361-L1382 |
| V11 — counter mode replaces the generated issue ID | `case "counter": return NextCounterIDTx(...)` | ENTAILED | https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/issueops/helpers.go#L188-L231 |
| V12 — arbitrary `bd sql` writes reject embedded mode | “Direct SQL access is not available in embedded mode” | ENTAILED | https://github.com/gastownhall/beads/blob/v1.1.0/cmd/bd/sql.go#L35-L42 |
| V13 — `external_ref` is nullable and ordinarily indexed | `external_ref VARCHAR(255)`; `INDEX idx_issues_external_ref` | ENTAILED | https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/schema/migrations/0001_create_issues.up.sql#L19-L58 |
| V14 — no v1.1.0 migration adds external-ref uniqueness | migration listing contains no later external-ref DDL | ENTAILED BY EXHAUSTIVE LISTING | https://github.com/gastownhall/beads/tree/v1.1.0/internal/storage/schema/migrations |
| V15 — create has no external-ref uniqueness check | `GenerateIssueIDInTable`; `InsertIssue` | ENTAILED BY CONTROL FLOW | https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/issueops/create.go#L86-L130 |
| V16 — external ref supports exact lookup and general search | `GetIssueByExternalRef(ctx, ref)` | ENTAILED | https://github.com/gastownhall/beads/blob/v1.1.0/internal/tracker/engine.go#L390-L399 |
| V17 — historical SQLite v0.47 had a UNIQUE constraint | `UNIQUE constraint failed: issues.external_ref` | ENTAILED | https://github.com/gastownhall/beads/issues/3902 |
| V18 — tracker pull and push own `external_ref` | `conv.Issue.ExternalRef = strPtr(ref)`; `{"external_ref": ref}` | ENTAILED | https://github.com/gastownhall/beads/blob/v1.1.0/internal/tracker/engine.go#L482-L488 |
| V19 — Jira derives a tracker URL | `fmt.Sprintf("%s/browse/%s"` | ENTAILED | https://github.com/gastownhall/beads/blob/v1.1.0/internal/jira/tracker.go#L303-L312 |
| V20 — issue metadata is shipped, not merely proposed | PR status: “Merged” | ENTAILED | https://github.com/gastownhall/beads/pull/1407 |
| V21 — metadata-backed custom indexing was proposed, not shipped | “Metadata as canonical storage + derived index” | ENTAILED | https://github.com/gastownhall/beads/discussions/1588 |
| V22 — SQLite metadata-index PR closed; Dolt PR welcomed | “welcome a PR leveraging Dolt's built-in indexing” | ENTAILED | https://github.com/gastownhall/beads/pull/1590 |
| V23 — sequential primary IDs shipped | “Support issue_id_mode=counter for sequential IDs” | ENTAILED | https://github.com/gastownhall/beads/issues/2002 |
| V24 — no exact dual display-ID request was located | “No results” | ENTAILED FOR RECORDED SEARCH | https://github.com/gastownhall/beads/issues?q=is%3Aissue+%22display+id%22 |
| V25 — contribution policy limits schema growth and PR scope | “expand the database schema when issue metadata is sufficient” | ENTAILED | https://github.com/gastownhall/beads/blob/v1.1.0/CONTRIBUTING.md#L83-L123 |
| V26 — active releases and concrete PR handling | v1.0.0 “03 Apr”; v1.1.0 “04 Jul” | ENTAILED | https://github.com/gastownhall/beads/releases |
| V27 — existing counter allocation and insert share a transaction | `UPDATE issue_counter SET last_id = last_id + 1` | ENTAILED | https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/issueops/helpers.go#L251-L267 |
| V28 — embedded Dolt exposes transactional create work | `BeginTx`; `Commit` | ENTAILED | https://github.com/gastownhall/beads/blob/v1.1.0/internal/storage/embeddeddolt/transaction.go#L683-L739 |
| V29 — counter safety is session-local, not branch-global | “within a single Dolt session are safe” | ENTAILED | https://github.com/gastownhall/beads/blob/v1.1.0/docs/CONFIG.md |
| V30 — external-ref list-filter work is post-v1.1.0 | issue closed “Jul 14, 2026” | ENTAILED | https://github.com/gastownhall/beads/issues/4341 |

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

- 2026-07-26 — Codex source audit of Beads v1.1.0 tagged schema, model, config, metadata, hooks, tracker integrations, migrations, create transactions, issue/discussion/PR history, releases, and contribution guidance. Wrote scoped result to this document only. One GitHub API tree URL was blocked by the browser safety layer; equivalent tagged directory and raw-file URLs were fetched successfully.

<!-- /doc:region name="run_history" -->
