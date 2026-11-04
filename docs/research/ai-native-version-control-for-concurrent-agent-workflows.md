---
title: AI-Native Version Control for Concurrent Agent Workflows
category: research
tags: [research, version-control, git, jujutsu, pijul, sapling, ai-agents, concurrency]
status: complete
source: "codex-2026-08-29"
template_version: "research-1.2.0"
delegation_provenance:
  version: 2
  contributors: []
---
<!-- Canonical Jinja source: STUB.md.jinja. This direct-copy stub is retained for consumers that have not migrated to rendering it. -->

# AI-Native Version Control for Concurrent Agent Workflows

**Status:** complete

**Created:** 2026-08-29

<!-- doc:region name="context" kind="immutable" -->

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. Git Baseline: What the Coordination Layer Is Compensating For](#1-git-baseline-what-the-coordination-layer-is-compensating-for)
  - [When to use](#when-to-use)
- [2. Jujutsu: The Most Credible Git-Compatible Complement](#2-jujutsu-the-most-credible-git-compatible-complement)
  - [When to use](#when-to-use-1)
- [3. Pijul: The Strongest Architectural Departure, the Weakest Migration Story](#3-pijul-the-strongest-architectural-departure-the-weakest-migration-story)
  - [When to use](#when-to-use-2)
- [4. Sapling: Proven at Monorepo Scale, Not an Agent-Concurrency Redesign](#4-sapling-proven-at-monorepo-scale-not-an-agent-concurrency-redesign)
  - [When to use](#when-to-use-3)
- [5. CRDT and OT Workspaces: Convergence Is Not Code Correctness](#5-crdt-and-ot-workspaces-convergence-is-not-code-correctness)
  - [When to use](#when-to-use-4)
- [6. Agent-Native Filesystems and Versioned Agent State](#6-agent-native-filesystems-and-versioned-agent-state)
  - [When to use](#when-to-use-5)
- [7. Gaps, Blindspots & Emergent Findings](#7-gaps-blindspots--emergent-findings)
  - [When to use this lens](#when-to-use-this-lens)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Ambiguous Items from Auto-Remediation (Post-Run Review)](#ambiguous-items-from-auto-remediation-post-run-review)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

## Context

This session's `ai-harness` fleet runs many autonomous Claude/Codex sessions concurrently,
frequently sharing the same repos and worktrees, coordinating with a hand-built layer atop plain
Git (worktree isolation, `aih-commit` provenance, share-surface manifests, rebase-and-
reconstruct-from discipline for receipted commits, a duplicate-command-hash background-task
guard, etc.). example-user asked whether a fundamentally different, more AI-native version-control
model — designed for or better suited to high-concurrency, multi-agent write patterns — might
reduce that hand-built coordination burden, either as a Git replacement or a complement. This doc
surveys that landscape as of 2026-08-29: what has real adoption or is being actively discussed,
how each candidate's concurrency/conflict model differs from Git's, and whether any of it is
credibly relevant to this fleet's own architecture.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

Primary focus is 2026, then 2025 and 2024. Current official documentation and first-party
engineering reports were preferred; older material appears only where it defines Git or Pijul's
foundational model. Public production evidence is distinguished from vendor capability claims and
research prototypes. **Pijul production adoption: no significant post-2024 developments found.**
The search found continued documentation and a 2025 non-VCS blog post, but no current production
deployment report, customer list, or post-beta release announcement. [NO SOURCE]

## Executive Summary

1. **No researched system eliminates the fleet's coordination layer.** Jujutsu can make local
   repository operations concurrent and conflicts durable, but it does not assign file ownership,
   preserve application invariants, serialize publication, or validate provenance. Its own
   documentation warns that Git-backed, colocated repositories are not fully lock-free under
   distributed-filesystem sharing. [VERIFIABLE][^4]

2. **Jujutsu is the only low-irreversibility candidate worth an operational pilot.** It combines
   automatic working-copy commits, an operation log, first-class conflicted commits, multiple
   workspaces, and a regular Git backend; GitHub can remain authoritative. [VERIFIABLE][^3][^5][^6]
   Because the Git compatibility boundary cannot represent conflicted commits faithfully and omits
   several Git features, this is a complement recommendation, not a replacement recommendation.
   [INFERENCE]

3. **Pijul is a genuine architectural alternative, not marketing.** Independently produced
   changes commute and its graph-based pristine stores conflicts without dropping either side.
   [VERIFIABLE][^8][^9] However, the official install path still requests the 1.0 beta and no
   current production-scale adoption or first-class GitHub round-trip was found. [VERIFIABLE][^10]
   [NO SOURCE]

4. **Sapling has the strongest production proof, but for a different problem.** Meta reports
   Sapling operating its monorepo at tens-of-millions scale; mechanically it remains a
   hash-addressed commit DAG, while its public server/VFS components are unsupported externally.
   [VERIFIABLE][^11][^12][^14] It improves stacks, mutation tracking, and push races, not
   overlapping agent edits inside one tree. [INFERENCE]

5. **The 2025-2026 agent evidence rejects “merge algorithm alone” as the main lever.** AgentRoom
   uses a CRDT workspace plus atomic file claims and messaging, and its authors state that
   coordination—not parallelism or CRDT merge—bears the load; CodeCRDT reports deterministic
   convergence alongside 5-10% semantic conflicts and task-dependent speedups or slowdowns.
   [VERIFIABLE][^15][^16] The better frame is therefore “versioned isolated state plus explicit
   claims and semantic validation,” with Git retained as the publication protocol. [INFERENCE]

## 1. Git Baseline: What the Coordination Layer Is Compensating For

Git stores project history as interrelated snapshots, allows parallel branch histories, and
represents branch tips with refs. [VERIFIABLE][^1] At merge time, Git places ancestor, current,
and other-side trees into index stages and leaves conflicting paths unmerged; a commit cannot be
written from unresolved index entries. [VERIFIABLE][^2] This is a sound distributed-clone model,
but it makes the index, mutable refs, and working-tree state coordination points when autonomous
writers share repository metadata or a checkout.

The fleet's burden separates into four different problems:

1. **Repository-state concurrency:** refs, indexes, operation interruption, and recovery.
2. **Content convergence:** combining overlapping edits without losing either intent.
3. **Work allocation:** ensuring two agents do not unknowingly implement the same or incompatible
   tasks.
4. **Publication integrity:** provenance, CI, review, manifests, and GitHub branch policy.

Only the first two are VCS concerns. A replacement that solves merge algebra but not ownership,
semantic tests, provenance, or publication merely moves the coordination layer. [INFERENCE]

### When to use

Keep plain Git when writers already have isolated worktrees or clones, overlap is limited, and the
existing coordination layer is reliable. Git remains the lowest-risk boundary for GitHub, CI,
security scanning, signing, and third-party tooling. [HEURISTIC]

## 2. Jujutsu: The Most Credible Git-Compatible Complement

### Mechanical model

Jujutsu removes the explicit index workflow: most commands snapshot changed working-copy contents
into a working-copy commit, and multiple working copies can share one repository while checking out
different commits. [VERIFIABLE][^3] Its operation log is a second content-addressed DAG over
repository “views.” Concurrent commands can finish against their starting view; divergent
operation heads are later three-way-merged, including conflicted bookmark targets.
[VERIFIABLE][^4]

Content merging still uses merge algorithms; the architectural difference is that unresolved
results are valid repository data. A rebase that conflicts succeeds by recording a logical
conflict in the rewritten commit, which can be rebased, merged, backed out, or resolved later.
[VERIFIABLE][^5] This decouples “integrate graph history” from “finish resolving text now,” a real
advantage for agents that otherwise stall inside continuation-state commands.

### Git/GitHub complement and migration cost

Jujutsu's Git backend is a regular Git repository. Colocation automatically imports and exports on
each Jujutsu command, so existing Git-aware build tools and GitHub remotes can remain in place.
[VERIFIABLE][^6] The boundary is not lossless:

- Git attributes and hooks are unsupported; submodules, partial clones, and Git LFS are unsupported
  in the documented compatibility matrix. [VERIFIABLE][^6]
- Git cannot represent Jujutsu conflicted commits natively; they use nonstandard trees and a
  `jj:trees` header, and change IDs use another nonstandard header not preserved by every Git
  rewrite. [VERIFIABLE][^6]
- Mixing mutating Git and Jujutsu commands can create divergent change IDs or ref confusion, and
  background IDE fetches can trigger such interleaving. [VERIFIABLE][^6]
- Sharing a Git-backed colocated repo through NFS/Dropbox is explicitly under-tested and can corrupt
  the backend, although the docs describe a reindex recovery path. [VERIFIABLE][^4]

For this fleet, commit provenance attached to immutable Git commit bytes would still need
re-stamping after Jujutsu rewrites. A durable logical change ID can help correlate evolution, but
it cannot replace the receipt unless the receipt scheme is redesigned around evolution rather than
a single commit OID. [INFERENCE]

### Adoption signal and AI-specific evidence

The strongest public signal is engineering investment, not disclosed deployment size: Jujutsu
documents an internal Google database-backed repository/operation-log server and a planned public
analogue. [VERIFIABLE][^7] No public 2025-2026 source located in this run quantified production
users, repositories, or agent fleets adopting Jujutsu because of multi-agent concurrency.
[NO SOURCE] Mesa's agent-oriented service is Jujutsu-based, but that is a vendor architecture
choice rather than evidence that coding-agent fleets have migrated to the Jujutsu CLI.
[VERIFIABLE][^17]

### When to use

Use a Jujutsu pilot when GitHub must remain authoritative and the pain is repeated rebasing,
interrupted conflicts, stack editing, or recovery from agent rewrites. Start with one Git-backed
clone per session or controlled Jujutsu workspaces plus a single serialized Git export/push path;
do not initially share a colocated Git store over a distributed filesystem. [HEURISTIC]

## 3. Pijul: The Strongest Architectural Departure, the Weakest Migration Story

### Mechanical model

Pijul stores changes against a graph whose vertices retain identity by originating change and
position; dependencies capture the context needed to apply a change. Independently produced
patches commute, so applying them in either order yields the same repository version.
[VERIFIABLE][^8] Its conflict-tolerant pristine applies both sides first and discovers conflicts
afterward; conflicting changes themselves commute. [VERIFIABLE][^9]

That is a deeper departure than Jujutsu. Git and Jujutsu remain snapshot-DAG systems whose content
integration uses merge algorithms. Pijul makes the change algebra the durable object and can
unapply an older change without changing later independent change identities. [VERIFIABLE][^9]

### Limits for machine-generated overlap

Commutation guarantees deterministic structural composition, not correct software. Pijul's manual
states that every change should be tested and go through code review. [VERIFIABLE][^9] Two agents
can therefore converge on a repository that has duplicate declarations, inconsistent APIs, or
broken invariants. Pijul reduces history-sensitive merge artifacts; it does not infer program
intent. [INFERENCE]

### Adoption, interop, and migration cost

The official download page still installs `~1.0.0-beta`. [VERIFIABLE][^10] No current primary
source found production deployment counts, named large users, GitHub pull-request integration, or a
supported bidirectional Git bridge. [NO SOURCE] A move would therefore require repository
conversion, a new hosting/review path or a custom Git mirror, CI adaptations, new signing and
provenance semantics, and training around changes/channels. Round-trip history fidelity would be a
project risk, not an implementation detail. [INFERENCE]

### When to use

Use Pijul for an isolated greenfield experiment whose main research question is whether
commutative change identity reduces repeated conflict work. It is not presently justified for a
GitHub-centered production fleet. [INFERENCE]

## 4. Sapling: Proven at Monorepo Scale, Not an Agent-Concurrency Redesign

### Mechanical model

Sapling remains distributed, hash-addressed, and commit-DAG-shaped. It removes the staging area,
tracks commit mutation history, and automatically restacks descendants. With a Sapling-compatible
server, push can rebase onto a target bookmark server-side, allowing concurrent non-conflicting
pushes to avoid a pull/rebase/push race. [VERIFIABLE][^12]

Sapling's server-side push rebasing applies only when the rebase would not require merging file
changes. [VERIFIABLE][^12] It therefore serializes non-conflicting landing races; it does not make
overlapping writes commutative. [INFERENCE]

### Adoption and availability

Meta states that Sapling operates its primary large monorepo and scales with its server and VFS to
tens of millions of files, commits, and branches. [VERIFIABLE][^11] That is the clearest
production signal in this survey. The public repository simultaneously states that Mononoke and
EdenFS are used in production inside Meta but are unsupported for external use.
[VERIFIABLE][^14]

The public CLI supports both `.git` and `.sl` modes and built-in GitHub integration.
[VERIFIABLE][^13] A Git-compatible local adoption can therefore improve stack UX, but it does not
import Meta's server-side concurrency architecture. No primary source found adoption motivated
specifically by ISSUE-REF concurrency. [NO SOURCE]

### When to use

Use Sapling when monorepo scale, stacked-diff UX, mutation tracking, or GitHub PR stacks are the
dominant problems. Do not select it to eliminate shared-tree agent claims or semantic merge review.
[INFERENCE]

## 5. CRDT and OT Workspaces: Convergence Is Not Code Correctness

CRDTs and OT move combination from batch merge time into a replicated or centrally arbitrated edit
stream. Modern text algorithms can merge concurrent edits into one consistent state; that is a
different latency and synchronization model from Git's later three-way merge. [VERIFIABLE][^22]

“Conflict-free” means replicas converge under the datatype's rules, not that every intent survives
as executable code. Automerge, for example, deterministically chooses a winner for concurrent
writes to the same property while retaining other values in conflict metadata.
[VERIFIABLE][^22]

The first directly relevant agent experiments are promising but narrow:

- **CodeCRDT (2025 preprint):** 600 trials over six tasks reported zero merge failures and 100%
  convergence, but 5-10% semantic conflicts, with raw performance ranging from 21.1% faster to
  39.4% slower. [VERIFIABLE][^16]
- **AgentRoom (2026 preprint):** a character-level CRDT was paired with atomic file claims,
  append-only messages, and status tools. Its matched-compute result favored the full coordination
  bundle, and the paper explicitly says coordination—not parallelism or CRDT merge—bears the load.
  [VERIFIABLE][^15] The headline tasks were mostly one Express/TypeScript runtime, used an
  ISSUE-REF rather than held-out execution tests, and included bounded Python/Rust spot checks, so
  this is prototype evidence, not production validation. [VERIFIABLE][^15]

These results disconfirm the premise that a stronger merge primitive alone removes coordination.
A CRDT may be valuable for live scratch documents, plans, manifests, or small jointly edited files,
but code still needs file or symbol claims, API contracts, compilation, tests, and a publication
snapshot. [INFERENCE]

### When to use

Use CRDT/OT for intentionally shared live state where sub-second visibility matters and the data
model has explicit merge semantics. For source code, pair it with claims and semantic validation,
then materialize reviewed snapshots into Git. [HEURISTIC]

## 6. Agent-Native Filesystems and Versioned Agent State

This adjacent field is moving faster than new DVCS adoption.

**Mesa** exposes Jujutsu-like changes, bookmarks, non-blocking conflicts, a mountable filesystem,
and APIs oriented to many machine-created repositories. [VERIFIABLE][^17] It can synchronize to
GitHub or any Git HTTPS server, but the documented default is fast-forward-only in both directions;
diverged refs are skipped. [VERIFIABLE][^18] The design is a credible complement pattern, but the
run found no independent production customer counts or same-file correctness benchmarks.
[NO SOURCE]

**Turso AgentFS** provides copy-on-write isolation, single-SQLite-file storage, auditing, and cloud
sync; its docs label the software beta. [VERIFIABLE][^19] It is principally a sandbox/session
artifact, not a multiwriter source merge engine.

**Redis Agent Filesystem (AFS)** stores workspaces in Redis and provides checkpoints, restore, and
forking. Its own guidance says to use Git and `git worktree` when the need is only source-code
branching and merging. [VERIFIABLE][^20] This is useful evidence against forcing every agent-state
problem into VCS.

**AgentGit** versions LangGraph workflow state—session history, tool records, and alternative
trajectories—not a source tree. Its evaluation concerns paper-abstract workflow exploration.
[VERIFIABLE][^21] The reusable idea is to checkpoint agent state and file state together while
keeping code publication separate. [INFERENCE]

### When to use

Use versioned filesystems or database-backed state for ephemeral sandboxes, prompts, logs,
generated artifacts, resumability, and cheap fork/restore. Keep Git as the reviewed code and
release ledger unless the storage product demonstrates faithful Git round-tripping and semantic
merge behavior under the fleet's workload. [HEURISTIC]

## 7. Gaps, Blindspots & Emergent Findings

1. **Blindspot: public adoption telemetry.** Jujutsu, Pijul, Mesa, AgentFS, CodeCRDT, and AgentRoom
   publish architecture or experiments, but public sources do not expose comparable production
   fleet sizes, conflict rates, or migration outcomes. [NO SOURCE]

2. **Blindspot: provenance under mutable changes.** Jujutsu and Sapling intentionally rewrite and
   track evolving changes. No source found a standard for binding external receipts to a logical
   change across rewrites while retaining GitHub-verifiable commit identity. [NO SOURCE]

3. **Emergent finding: the workload has two consistency planes.** Agents need a fast mutable plane
   for workspaces, claims, messages, and checkpoints, plus a slower immutable plane for reviewed
   commits, CI, provenance, and releases. Trying to make one VCS provide both increases coupling.
   [INFERENCE]

4. **Emergent finding: isolation remains useful even with commutative history.** Build products,
   generated files, package managers, tests, and tools mutate paths outside the VCS merge model.
   Separate working directories or copy-on-write overlays remain necessary. [INFERENCE]

5. **Second-order effect: “never block on conflict” can defer risk.** Durable conflicts improve
   throughput, but unresolved states can travel farther into tests, artifact generation, or
   publication unless gates explicitly reject them. [INFERENCE]

6. **Anchor-bias correction:** The survey expanded beyond the named DVCS candidates to agent
   filesystems and versioned workflow state because the 2025-2026 evidence locates the main gains
   in claims, observation, isolation, and recovery rather than a new commit graph. [INFERENCE]

### When to use this lens

Apply this lens before choosing a tool: classify each coordination incident as repository-state,
content, allocation, semantic, or publication failure. A VCS pilot should be credited only for the
classes it can actually remove. [HEURISTIC]

## Comparison

| # | Candidate | Core concurrency/conflict model | Production signal by 2026-08-29 | GitHub migration | Likely fleet effect |
|---|-----------|---------------------------------|----------------------------------|------------------|---------------------|
| 1 | Git + current coordination | Snapshot DAG, per-worktree index, three-way merge; conflicts block tree write [VERIFIABLE][^1][^2] | Existing ecosystem baseline [HEURISTIC] | None | Known burden, maximum compatibility [HEURISTIC] |
| 2 | Jujutsu over Git | Operation-log concurrency; working-copy commits; conflicts as commit data [VERIFIABLE][^3][^4][^5] | Internal Google server disclosed; no public fleet counts [VERIFIABLE][^7] [NO SOURCE] | Low to moderate; regular Git backend, material feature gaps [VERIFIABLE][^6] | Reduces rebase/recovery friction; does not remove claims or provenance redesign [INFERENCE] |
| 3 | Pijul | Commutative patches and conflict-tolerant graph [VERIFIABLE][^8][^9] | Official beta; no current scale evidence [VERIFIABLE][^10] [NO SOURCE] | High; no verified first-class round-trip [NO SOURCE] | Theoretically cleaner overlapping history, operationally high risk [INFERENCE] |
| 4 | Sapling | Commit DAG, mutation tracking, stacked diffs, server-side rebase-on-push [VERIFIABLE][^12] | Meta monorepo at tens-of-millions scale [VERIFIABLE][^11] | Low for CLI; high for Meta-like server architecture [VERIFIABLE][^13][^14] | Better stacks/scale, little change to same-file agent conflicts [INFERENCE] |
| 5 | CRDT/OT workspace | Continuous deterministic text/data convergence [VERIFIABLE][^22] | Early agent preprints only [VERIFIABLE][^15][^16] | Complementary export to Git | Useful live plane; semantic conflicts and coordination remain [INFERENCE] |
| 6 | Mesa | Hosted Jujutsu-like versioned filesystem with Git sync [VERIFIABLE][^17][^18] | Vendor docs; no independent fleet evidence [NO SOURCE] | Moderate; fast-forward sync boundary | Potential machine-scale workspace service; lock-in and evidence gap [INFERENCE] |
| 7 | AgentFS / Redis AFS / AgentGit | Copy-on-write or checkpoint/fork of filesystem/workflow state [VERIFIABLE][^19][^20][^21] | Beta/prototype signals [VERIFIABLE][^19][^21] | Complement only | Better isolation, replay, and state recovery; not code merge replacement [INFERENCE] |

## Recommendation

**Do not replace Git or remove the existing coordination guards. Run a bounded Jujutsu-as-client
pilot while keeping GitHub, Git commits, CI, and the provenance wrapper authoritative.**
[INFERENCE]

The pilot should have two stages:

1. **Isolated-client stage:** use one colocated Jujutsu/Git clone per agent session. Preserve the
   current work-allocation and publication gates. Measure interrupted-rebase incidents, manual
   reconstruction time, lost/ref-confusion events, and receipt re-stamping.
2. **Controlled shared-repository stage:** only if stage one is positive, test Jujutsu workspaces on
   one local filesystem with each agent owning a distinct working-copy commit and a single process
   responsible for Git import/export/push. Do not use NFS/Dropbox sharing, and reject conflicted
   commits at the publication gate. [HEURISTIC]

Adopt beyond pilot only if measured coordination incidents fall materially without increasing
wrong-ref publication, provenance repair, or semantic test failures. Retain explicit task/file
claims, immutable receipts at publication, manifest freshness checks, per-agent working
directories or overlays, and semantic CI. [HEURISTIC]

Keep Pijul as a research benchmark for patch algebra, Sapling as an option for monorepo/stack scale,
and CRDT workspaces as an experiment for the mutable collaboration plane. None currently supplies a
credible end-to-end replacement for the fleet's coordination plus GitHub workflow. [INFERENCE]

## Open Questions

1. What fraction of recent fleet incidents are Git ref/index failures versus duplicated work,
   semantic incompatibility, stale manifests, or provenance failures?
2. Can the provenance receipt bind both a Jujutsu change ID and the final Git OID without weakening
   tamper evidence?
3. Does a local shared Jujutsu Git backend survive the fleet's real concurrent command schedule
   under fault injection, including cancellation and process death?
4. How should the publication gate detect and reject Jujutsu's nonstandard conflicted-commit
   representation before GitHub push?
5. Would a single-writer Git gateway become a throughput bottleneck, or simply formalize the
   serialization GitHub already imposes on protected branches?
6. Can AST/LSP-aware claims reduce semantic conflicts more effectively than character-level CRDT
   merge?
7. What are Mesa's export fidelity, retention, security, and pricing characteristics under a real
   agent-fleet trace?
8. Is there a current Pijul deployment or maintained Git bridge unavailable to public search that
   would change the migration assessment?

## Sources

[^1]: Git Project. (2026). [Git user manual](https://git-scm.com/docs/user-manual). Git Documentation. Verified accessible (HTTP 200) 2026-08-29. (Snapshot, branches, and refs model.)
[^2]: Git Project. (2026). [git-read-tree](https://git-scm.com/docs/git-read-tree). Git Documentation. Verified accessible (HTTP 200) 2026-08-29. (Index and three-way merge stages.)
[^3]: Jujutsu Contributors. (2026). [Working copy](https://docs.jj-vcs.dev/latest/working-copy/). Jujutsu Documentation. Verified accessible (HTTP 200) 2026-08-29. (Automatic commits and workspaces.)
[^4]: Jujutsu Contributors. (2026). [Concurrency](https://docs.jj-vcs.dev/latest/technical/concurrency/). Jujutsu Documentation. Verified accessible (HTTP 200) 2026-08-29. (Operation-log concurrency and Git-backend caveats.)
[^5]: Jujutsu Contributors. (2026). [First-class conflicts](https://docs.jj-vcs.dev/latest/conflicts/). Jujutsu Documentation. Verified accessible (HTTP 200) 2026-08-29. (Conflicted commits.)
[^6]: Jujutsu Contributors. (2026). [Git compatibility](https://docs.jj-vcs.dev/latest/git-compatibility/). Jujutsu Documentation. Verified accessible (HTTP 200) 2026-08-29. (Backend, colocation, limitations, and conflict encoding.)
[^7]: Jujutsu Contributors. (2026). [Development roadmap](https://www.jj-vcs.dev/latest/roadmap/). Jujutsu Documentation. Verified accessible (HTTP 200) 2026-08-29. (Internal Google server and open-source roadmap.)
[^8]: Meunier, P.-É., & Becker, F. (2026). [Theory](https://pijul.org/manual/theory). Pijul Manual. Verified accessible (HTTP 200) 2026-08-29. (Graph model, dependencies, commutation, and CRDT relation.)
[^9]: Meunier, P.-É., & Becker, F. (2026). [Why Pijul?](https://pijul.org/manual/why_pijul.html). Pijul Manual. Verified accessible (HTTP 200) 2026-08-29. (Patch algebra, conflicts, and semantic caveat.)
[^10]: Pijul. (2026). [Downloads](https://pijul.org/downloads). Pijul. Verified accessible (HTTP 200) 2026-08-29. (Official beta installation channel.)
[^11]: Meta. (2026). [Sapling SCM](https://sapling-scm.com/docs/introduction/). Sapling Documentation. Verified accessible (HTTP 200) 2026-08-29. (Meta use and stated scale.)
[^12]: Meta. (2026). [Differences from Git](https://sapling-scm.com/docs/introduction/differences-git/). Sapling Documentation. Verified accessible (HTTP 200) 2026-08-29. (No staging area, push rebasing, mutation history.)
[^13]: Meta. (2026). [Git integration](https://sapling-scm.com/docs/git/intro/). Sapling Documentation. Verified accessible (HTTP 200) 2026-08-29. (Git modes and GitHub integration.)
[^14]: Meta. (2026). [Sapling SCM repository](https://github.com/facebook/sapling). GitHub. Verified accessible (HTTP 200) 2026-08-29. (Public support status of Mononoke and EdenFS.)
[^15]: Cho, S., & Lee, D. (2026). [AgentRoom: Concurrent multi-agent coding in a CRDT-backed shared workspace](https://arxiv.org/abs/2608.23740). arXiv. Verified accessible (HTTP 200) 2026-08-29. (Preprint architecture, results, and limitations.)
[^16]: Pugachev, S. (2025). [CodeCRDT: Observation-driven coordination for multi-agent LLM code generation](https://arxiv.org/abs/2510.18893). arXiv. Verified accessible (HTTP 200) 2026-08-29. (Preprint convergence, performance, and semantic conflicts.)
[^17]: Mesa. (2026). [Versioning](https://docs.mesa.dev/content/concepts/versioning). Mesa Documentation. Verified accessible (HTTP 200) 2026-08-29. (Jujutsu-based agent versioning model.)
[^18]: Mesa. (2026). [GitHub sync](https://docs.mesa.dev/content/integrations/github/sync). Mesa Documentation. Verified accessible (HTTP 200) 2026-08-29. (Git server and fast-forward sync semantics.)
[^19]: Turso. (2026). [AgentFS](https://docs.turso.tech/agentfs/introduction). Turso Documentation. Verified accessible (HTTP 200) 2026-08-29. (Copy-on-write, SQLite, auditing, beta status.)
[^20]: Redis. (2026). [Agent Filesystem](https://github.com/redis/agent-filesystem). GitHub. Verified accessible (HTTP 200) 2026-08-29. (Checkpoint/fork workspace model and explicit Git boundary.)
[^21]: Li, Y., Ping, S., Chen, X., Qi, X., Wang, Z., Luo, Y., & Zhang, X. (2025). [AgentGit: A version control framework for reliable and scalable ISSUE-REF multi-agent systems](https://arxiv.org/abs/2511.00628). arXiv. Verified accessible (HTTP 200) 2026-08-29. (Versioned workflow state; preprint.)
[^22]: Automerge Contributors. (2026). [Conflicts](https://automerge.org/docs/reference/documents/conflicts/). Automerge Documentation. Verified accessible (HTTP 200) 2026-08-29. (Concurrent merge and deterministic conflict semantics.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** cx delivery wrapper (unregistered)
**Model:** Codex flagship route (exact concrete model ID not exposed to worker)
**Date:** 2026-08-29

```text
Research topic: AI-native, agent-concurrency-friendly version control models as alternatives or
complements to Git.

## Background

A fleet of autonomous Claude/Codex coding sessions works concurrently, often on the same repos
and shared trees, using plain Git plus a hand-built coordination layer on top of it (worktree
isolation per session/agent, a provenance-stamping commit wrapper, rebase-and-reconstruct
discipline for commits carrying provenance metadata, manifest-staleness gates, and various
concurrency guards for background task dispatch). We want to know whether a genuinely different
version-control model — one designed for, or empirically better suited to, high-concurrency
machine-generated write workloads — exists and would meaningfully reduce that hand-built
coordination burden, either as a Git replacement or as a complement layered alongside it.

## Scope note — questions, examples, and named references are a starting point, not a checklist
The questions, topics, and named examples below are illustrative anchors and a FLOOR for this
research — not an exhaustive list to answer only or evaluate only. Reason independently: survey the
landscape broadly, follow the evidence where it leads, expand scope where warranted, and surface
relevant work, factors, and failure modes not named here. Actively resist answering only the listed
questions or evaluating only the named approaches — an output that merely fills in the listed items
has NOT met the research goal.

## Independent exploration (gaps, blindspots, emergent threads) — required
Treat the question list as a FLOOR, not a ceiling. As you research, actively surface what this
framing may be missing and pursue each promising thread to a logical conclusion:
- Adjacent or upstream factors the questions don't capture.
- Contrarian / disconfirming evidence — report it even when it challenges the premise.
- Emerging 2025-2026 practices, tools, or research not anchored by the named examples.
- Known failure modes and second-order effects.
Whenever a load-bearing thread surfaces mid-research, follow it to its conclusion and report it in a
dedicated "Gaps, blindspots & emergent findings" subsection. Explicitly NAME any blindspot you
suspect but cannot resolve (and why) rather than omitting it. Anchor bias -- over-fitting to the
listed questions and example approaches -- is a known failure mode; counter it deliberately and say
where you did.

## Candidate anchors (a floor, not the ceiling)
- Jujutsu (jj) -- Git-compatible backend, first-class conflict-as-data model, no index/staging area
- Pijul -- patch-theory-based, commutative patches, mathematically-grounded conflict resolution
- Sapling (Meta) -- Git-compatible, designed for monorepo scale, strong stacked-diff workflow
- Any Merkle-CRDT, OT (operational-transform), or other CRDT-based version-control/sync system
- Anything gaining real 2025-2026 adoption or active discussion specifically framed around
  AI-agent-native or ISSUE-REF workflows (agentic coding tools, multi-agent
  orchestration frameworks proposing their own state/version layer, etc.)

## What to actually answer (a floor)
1. For each credible candidate: what is its actual concurrency/conflict-resolution model, and how
   does it differ mechanically from Git's (three-way merge + index + refs)? Is it genuinely better
   suited to many concurrent writers producing overlapping machine-generated changes, or is that
   framing marketing rather than a real architectural difference?
2. Real-world adoption signal as of 2026-08-29: who is actually using each candidate in production,
   at what scale, and for what kind of workload? Distinguish "interesting research prototype" from
   "something teams are actually shipping with."
3. Specifically for AI-agent/ISSUE-REF workflows: is there evidence any of these are being adopted
   or discussed *because of* multi-agent concurrency properties, as opposed to unrelated reasons
   (e.g., jj's UX improvements for solo human developers)?
4. What would migrating (fully or partially) actually cost/require for a team currently on Git with
   GitHub as the remote/PR system -- interop, tooling, CI, muscle-memory, and irreversibility risk?
5. Is there a credible "complement, not replacement" pattern -- e.g., running one of these as a
   local/session-scoped layer while still publishing to Git/GitHub as the shared source of truth --
   and does that capture most of the concurrency benefit without the migration cost?
6. Are there non-VCS approaches (CRDT-based document sync, database-backed state instead of a
   file-tree at all) that better solve "many AI agents mutating shared state concurrently" than any
   version-control system, git-shaped or not -- i.e., is "a new git" even the right frame for this
   problem?

<grounding_instructions>
You are a principal distributed-systems engineer who has built and operated version-control
infrastructure for large, highly concurrent engineering organizations, has evaluated Git
alternatives (Jujutsu, Pijul, Sapling, Mercurial-derived tooling) for production adoption, and has
recently been advising on multi-agent AI coding infrastructure specifically. You have strong
opinions backed by evidence. When you cannot find a source, you say so explicitly.

Temporal scope: Weight sources by recency -- 2026 (primary) -> 2025 -> 2024.
Pre-2024 sources are background context only unless foundational to the topic.
If post-2024 literature is genuinely sparse for a subtopic, state
"[subtopic]: no significant post-2024 developments found" rather than
backfilling with older sources. Backfilling is a failure mode, not a hedge.

Before generating your final output, execute a Chain-of-Verification (CoVe)
to ensure factual fidelity over compliance.

Inside your thought process:
1. Isolate the core facts required.
2. Draft a tentative response.
3. Hostile Cross-Examination: flag any claim where you are citing a source because
   the prompt implied you should, rather than because you verified it.
4. Strip away any claim that cannot be empirically verified.

When generating your final output, classify every major claim. Write your rationale
before appending the tag -- writing the tag first causes post-hoc rationalization.
Rationale -> evidence check -> tag.

- [VERIFIABLE]: backed by documentation, peer-reviewed research, or official
  tech blogs (2024-2026). Carry an inline footnote ref to the source: [VERIFIABLE][^N].
- [HEURISTIC]: widely accepted best practice without a specific citation.
- [INFERENCE]: a logical conclusion drawn from context. Provide your reasoning
  in-text. Do not fabricate a source. Tier tag only -- NO footnote ref.
- [NO SOURCE]: explicitly state when you cannot find verifiable data. Tier tag
  only -- NO footnote ref.

Citation format (mandatory for every externally-sourced claim):
- Inline: append the GFM footnote ref directly after the tier tag -- [VERIFIABLE][^N].
  A claim citing multiple sources carries ascending separate refs -- [VERIFIABLE][^3][^7]
  (never grouped [^3,^7]; never out of order).
- Footnote definitions live once, under ## Sources, in APA form with a clickable
  URL or DOI and an access-verification stamp. Worked example:
    [^1]: LangChain. (2026). [Threads](https://docs.langchain.com/langsmith/threads).
    LangSmith Documentation. Verified accessible (HTTP 200) 2026-06-03. (Scope note.)
- URL-or-DOI ALWAYS: every source entry carries a clickable URL or a doi.org link --
  paywalled/gated is fine (link it anyway; stamp the access status). Only the
  truly-irreducible case (no online catalog presence anywhere) gets an explicit
  [no online source located] marker with a one-line justification.
- Integrity: footnote refs are contiguous from [^1], every [^N] ref has a matching
  definition, and every definition is referenced -- no gaps, no orphans.
- [INFERENCE] / [NO SOURCE] claims carry the tier tag with NO footnote ref.

Hard constraint (overrides all formatting preferences): never invent a citation
to satisfy a formatting instruction. Accuracy > completeness.

Format diagrams using Mermaid.js or ASCII. Format math using LaTeX.
NEVER generate binary images.
</grounding_instructions>

## Retrieval enforcement
For each load-bearing claim, run at least one web_search or web_fetch call; do not return
[VERIFIABLE] without a URL you actually fetched in this session. Do not stop after one round. If
no source is found for a sub-question, run additional searches across alternate framings before
tagging it [NO SOURCE] -- that tag is reserved for genuine evidence absence, not retrieval
avoidance.

## Deliverable

Write the document to the target file (already scaffolded from the canonical STUB, template
research-1.2.0). Required structure, in order: frontmatter (status: complete, source:
"codex-2026-08-29", template_version: "research-1.2.0") -> ## Table of Contents -> ## Context
(already frozen -- do not rewrite it, only add sections after it) -> ## Temporal Scope ->
## Executive Summary (3-5 numbered findings) -> topic sections (## 1., ## 2., etc., each with a
### When to use subsection since this is an options/approaches doc) -> ## Comparison (a table
with a numbered # column) -> ## Recommendation -> ## Open Questions -> ## Sources (GFM footnote
definitions, APA-shaped) -> ## Appendix: Research Prompt (Registry ID, Model, Date, and this full
prompt text in a fenced text block) -> ## Appendix: Provenance Ledger (claim -> source URL ->
verbatim quote -> verdict -> live? table for every [VERIFIABLE] claim) -> ## Run History.

Run a 5-perspective adversarial critic pass on your own draft before finalizing (conventional,
contrarian, historical, adjacent, skeptic) and resolve or explicitly concede every high/medium
finding. WebFetch every citation you intend to keep and classify any failure (dead/paywalled/
transient) rather than silently dropping it. This document will be reviewed against the
provenance ledger, so every VERIFIABLE claim's quote must literally appear in the fetched page
text.
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

| Claim | Source URL | Verbatim quote from fetched page | Verdict | Live? |
|---|---|---|---|---|
| Git stores snapshots, parallel branches, and head refs. | https://git-scm.com/docs/user-manual | “It stores the history as a compressed collection of interrelated snapshots of the project’s contents.” / “work may simultaneously proceed along parallel lines of development” / “heads which reference the latest commit on each branch” | SUPPORTED | Yes, HTTP 200 |
| Git's three-way merge operates through the index and leaves conflicts unmerged. | https://git-scm.com/docs/git-read-tree | “Optionally, it can merge a tree into the index, perform a fast-forward (i.e. 2-way) merge, or a 3-way merge” / “Only conflicting paths will be in an unmerged state” | SUPPORTED | Yes, HTTP 200 |
| Jujutsu automatically commits changed working-copy content. | https://docs.jj-vcs.dev/latest/working-copy/ | “Most `jj` commands you run will commit the working-copy changes if they have changed.” | SUPPORTED | Yes, HTTP 200 |
| Jujutsu supports multiple working copies on one repo. | https://docs.jj-vcs.dev/latest/working-copy/ | “You can have multiple working copies backed by a single repo.” | SUPPORTED | Yes, HTTP 200 |
| Jujutsu uses an operation log and merges divergent operations. | https://docs.jj-vcs.dev/latest/technical/concurrency/ | “The most important piece in the lock-free design is the "operation log". That is what allows us to detect and merge divergent operations.” | SUPPORTED | Yes, HTTP 200 |
| Jujutsu's Git backend has concurrency caveats. | https://docs.jj-vcs.dev/latest/technical/concurrency/ | “repository corruption is possible because the backend is not entirely lock-free” / “such use of Jujutsu is not currently thoroughly tested” | SUPPORTED | Yes, HTTP 200 |
| Jujutsu records and carries conflicted commits. | https://docs.jj-vcs.dev/latest/conflicts/ | “the conflict will be recorded in the rebased commit and the rebase operation will succeed” / “Conflicted states can be further rebased, merged, or backed out.” | SUPPORTED | Yes, HTTP 200 |
| Jujutsu can use a regular Git repository and colocate. | https://docs.jj-vcs.dev/latest/git-compatibility/ | “One of them uses a regular Git repo” / “Jujutsu will import and export from and to the Git repo on every `jj` command automatically.” | SUPPORTED | Yes, HTTP 200 |
| Jujutsu's Git compatibility omits material features. | https://docs.jj-vcs.dev/latest/git-compatibility/ | “.gitattributes: No.” / “Hooks: No.” / “Submodules: No.” / “Git LFS: No.” | SUPPORTED | Yes, HTTP 200 |
| Git cannot natively represent Jujutsu conflicted commits. | https://docs.jj-vcs.dev/latest/git-compatibility/ | “Commits with conflicts cannot be represented in Git.” | SUPPORTED | Yes, HTTP 200 |
| Google has an internal database-backed Jujutsu server. | https://www.jj-vcs.dev/latest/roadmap/ | “Google has an internal Jujutsu server backed by a database.” | SUPPORTED | Yes, HTTP 200 |
| Pijul independently produced patches commute. | https://pijul.org/manual/theory | “any two patches c 0 and c 1 that could be produced independently commute, meaning that applying c 1 after c 0 yields the same repository as applying c 0 after c 1” | SUPPORTED | Yes, HTTP 200 |
| Pijul stores both conflict sides before computing conflicts. | https://pijul.org/manual/why_pijul.html | “edits from both sides of a conflict get applied without resolving the conflict” | SUPPORTED | Yes, HTTP 200 |
| Pijul requires semantic validation beyond its merge algebra. | https://pijul.org/manual/why_pijul.html | “Every change should be tested and go through code review.” | SUPPORTED | Yes, HTTP 200 |
| Pijul's official install channel is beta. | https://pijul.org/downloads | “cargo install pijul --version "~1.0.0-beta"” | SUPPORTED | Yes, HTTP 200 |
| Sapling is used at Meta at tens-of-millions scale. | https://sapling-scm.com/docs/introduction/ | “Sapling is a source control system developed and used at Meta” / “scales to repositories with 10’s of millions of files, commits, and branches.” | SUPPORTED | Yes, HTTP 200 |
| Sapling removes staging, tracks mutations, and can rebase pushes server-side. | https://sapling-scm.com/docs/introduction/differences-git/ | “In Sapling, there is no staging area.” / “the record of the operation is kept” / “the server rebases your commit onto the target bookmark” | SUPPORTED | Yes, HTTP 200 |
| Sapling offers Git modes and GitHub integration. | https://sapling-scm.com/docs/git/intro/ | “Sapling supports Git in 2 modes: `.git` mode, and `.sl` mode.” / “Sapling has builtin support for GitHub.” | SUPPORTED | Yes, HTTP 200 |
| Sapling server and VFS are unsupported externally. | https://github.com/facebook/sapling | “While it is used in production within Meta it is not yet supported for external usage.” | SUPPORTED | Yes, HTTP 200 |
| AgentRoom combines CRDT merge with claims and says coordination bears the load. | https://arxiv.org/abs/2608.23740 | “room_claim(path) atomically assigns file ownership” / “Coordination, not parallelism or CRDT-merge, bears the load.” | SUPPORTED | Yes, HTTP 200 |
| AgentRoom's evaluation has narrow correctness limits. | https://arxiv.org/abs/2608.23740 | “All headline cells share one Express.js/TypeScript runtime” / “tasks do not ship a held-out correctness suite” | SUPPORTED | Yes, HTTP 200 |
| CodeCRDT converged but retained semantic conflicts and variable speed. | https://arxiv.org/abs/2510.18893 | “up to 21.1% speedup on some tasks, up to 39.4% slowdown on others, and 100% convergence with zero merge failures” / “semantic conflict rates (5-10%)” | SUPPORTED | Yes, HTTP 200 |
| Mesa uses a Jujutsu-based model with non-blocking conflicts. | https://docs.mesa.dev/content/concepts/versioning | “Mesa’s model is based on Jujutsu VCS” / “conflicts are non-blocking.” | SUPPORTED | Yes, HTTP 200 |
| Mesa Git sync defaults to fast-forward only. | https://docs.mesa.dev/content/integrations/github/sync | “By default, Mesa’s GitHub sync is conservative, and will only update branches and tags that have not diverged (fast-forward only).” | SUPPORTED | Yes, HTTP 200 |
| AgentFS is copy-on-write, SQLite-backed, auditable, and beta. | https://docs.turso.tech/agentfs/introduction | “Copy-on-Write Isolation” / “Everything stored in one portable SQLite database” / “Every file operation is recorded and queryable” / “This software is in BETA.” | SUPPORTED | Yes, HTTP 200 |
| Redis AFS checkpoints/forks workspaces and defers source merge to Git. | https://github.com/redis/agent-filesystem | “letting you checkpoint and restore workspaces” / “letting you fork a workspace for parallel work” / “If you only need source-code branching and merging, use Git and `git worktree`.” | SUPPORTED | Yes, HTTP 200 |
| AgentGit versions MAS workflow state, not source trees. | https://arxiv.org/abs/2511.00628 | “Built as an infrastructure layer on top of LangGraph, AgentGit supports state commit, revert, and branching” / “session history, tool invocation records, environment variables, and intermediate reasoning processes” | SUPPORTED | Yes, HTTP 200 |
| Automerge converges same-position inserts and uses deterministic winners for same-property writes. | https://automerge.org/docs/reference/documents/conflicts/ | “Automerge will ensure that on all nodes the inserted items are placed in the same order.” / “Automerge picks one of the concurrently written values as the ‘winner’” | SUPPORTED | Yes, HTTP 200 |

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

- 2026-08-29 — Initial Codex research synthesis. Ran mainstream, contrarian, adjacent-field, and
  recency-first searches; fetched all 22 retained citations. Five-perspective critic pass:
  conventional (kept Git/GitHub boundary), contrarian (preserved Pijul's real architectural
  advantage), historical (distinguished patch algebra from snapshot merging), adjacent (added
  filesystems and workflow-state versioning), skeptic (downgraded unsupported adoption claims and
  narrowed semantic claims to fetched evidence). All high/medium findings resolved or explicitly
  conceded. Citation spot-check reopened Git, Jujutsu concurrency, Pijul theory, Sapling scale,
  AgentRoom, and Mesa sync pages; all were live. No dead, paywalled, WAF-blocked, or transient
  retained sources.

<!-- /doc:region name="run_history" -->
