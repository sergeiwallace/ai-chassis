---
title: "Beads (bd) Issue Tracker — Best Practices and Real-World Usage for a Solo Multi-Repo AI-Agent Fleet"
category: research
tags: [research, beads, issue-tracker, ai-agents, multi-repo]
status: complete
source: "claude-adhoc-2026-07-06"
template_version: "research-1.2.0"
---
<!-- Carries internal SW-*/AIH-*/CORE-* IDs; scrub before any open-source pass (ISSUE-REF). -->

# Beads (bd) Issue Tracker — Best Practices and Real-World Usage for a Solo Multi-Repo AI-Agent Fleet

**Status:** complete

**Created:** 2026-07-06

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. Multi-Repo Topology](#1-multi-repo-topology)
  - [When to use](#when-to-use)
- [2. Day-to-Day Agent Workflows](#2-day-to-day-agent-workflows)
  - [When to use](#when-to-use-2)
- [3. Pitfalls, Gotchas, Anti-Patterns](#3-pitfalls-gotchas-anti-patterns)
- [4. Workarounds for Missing / Assumed-Missing Features](#4-workarounds-for-missing--assumed-missing-features)
- [5. Community UI Landscape](#5-community-ui-landscape)
- [6. CC TUI Task Panel — Beads Integration, Customization & Alternatives](#6-cc-tui-task-panel--beads-integration-customization--alternatives)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

<!-- doc:region name="context" kind="immutable" -->

## Context

Original framing: a solo developer runs a ~18-repository Claude Code AI-agent fleet and wants to know how experienced Beads users structure multi-repo tracking, what day-to-day agent workflows look like, what breaks, what's missing, and which community UIs are actually alive. The prompt supplied several config keys sourced from a DeepWiki summary (`external_projects`, `routing.*`, `directory.labels`, `federation.remote`, `sync.remote`, `federation.sovereignty`) as a hypothesis to verify, and explicitly framed "rigs + multi-repo hydration" as a hypothesis to confirm or refute against real-world practice — not a foregone conclusion.

**Primary period:** 2025–2026 (Beads did not exist before October 2025)
**Source weighting:** 2026 sources primary; late-2025 sources (project launch era) as background/origin-story context; no pre-2025 sources exist for this topic.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

Beads was released by Steve Yegge in October 2025 and has iterated on a roughly weekly-to-biweekly release cadence since[^15][^16]. There is no "historical" literature to weight against — every source in this doc is 2025–2026. Because the project changes fast, several official docs contradict each other depending on when they were last touched (see §3, Version Churn) — this doc flags every such contradiction explicitly rather than silently picking one side.

## Executive Summary

1. **The canonical repo moved.** `github.com/steveyegge/beads` now 301-redirects to `github.com/gastownhall/beads` (confirmed live, HTTP 301, this session)[^1]. Cite `gastownhall/beads` going forward; `steveyegge/beads` still works as a URL but is not the repo of record.
2. **Rigs + hydration is real and well-documented, but the load-bearing evidence for "many independent repos, need one view" points toward per-repo `.beads/` + light glue, not hydration, at fleet scale.** The one concrete case study found at comparable scale (20+ repos, 857+ issues) uses plain per-repo `.beads/issues.jsonl` with no hydration, no central repo, and no cross-repo aggregation described at all[^21]. The project's own multi-repo docs frame routing/hydration primarily around a narrower problem — OSS contributor namespace isolation and architect/implementer role splits — not "unified dashboard across N unrelated repos"[^2][^3]. The FAQ's own answer to "how do I track multiple projects" is the low-tech "initialize bd in a parent directory," not the routing/federation machinery[^14] — a signal the maintainers themselves don't treat hydration as the default answer to this exact question. **Verdict: hypothesis partially refuted for the specific "unified cross-fleet view" use case; confidence: medium** (single strong case study, corroborated by an internal documentation gap, not a large sample).
3. **The prompt's premise about a "coarse status model (open/in_progress/closed only)" is factually wrong** — bd ships five built-in statuses (`open, in_progress, blocked, deferred, closed`) plus fully configurable `status.custom` and `types.custom` namespaces[^23][^5]. Likewise the premise that due dates and labels are missing is wrong — both are native, first-class fields (`bd create --labels`, native due-date parsing like `+1d`/`tomorrow`/`2025-01-15`)[^23]. The one genuinely missing feature confirmed across every source checked is **recurring tasks** — no native support and no documented workaround pattern was found anywhere[^14][^23].
4. **Federation (the "towns"/peer-sync layer) is more aspirational than production-ready as of this session**: `bd federation add-peer` exists, but `list-peers`, `push <peer>`, `pull <peer>`, and `sync <peer>` are documented with "infrastructure support" yet are explicitly stated as unavailable in current releases[^6]. Anyone reading `federation.sovereignty` (T1–T4) as a shipped compliance feature should treat it with caution — the two official docs that describe the tier semantics **contradict each other** on what T1 vs T4 actually means (see §1).
5. **The two most-cited real pitfalls are (a) a still-open, unresolved Dolt metadata-table merge conflict on every pull in a simple two-machine shared-remote setup** (opened 2026-03-09, no fix merged as of this session)[^11], and **(b) version churn / scope creep serious enough that at least one user forked away entirely**, citing release-over-release performance regressions and complexity growth, and built a competing single-bash-script tool[^20]. Bus factor is effectively 1 (Yegge-led, "built in 6 days with Claude," now moved under a `gastownhall` org umbrella with no independently documented second maintainer)[^15][^1].

## 1. Multi-Repo Topology

Three candidate patterns were named in the research prompt. All three exist as real, documented mechanisms — the open question was which one experienced users actually reach for at fleet scale.

### (a) Per-repo `.beads/` in each repo

This is the default, zero-configuration mode: `bd init` in each repo creates its own `.beads/` directory with its own Dolt-backed database and its own `issues.jsonl` export, versioned alongside that repo's code[^8][^21]. There is no built-in cross-repo view in this mode; each repo is an island.

**Real-world evidence:** the only concrete published account of a fleet at comparable scale to the ~18-repo target — 20+ repositories, 857+ issues, used for cross-session Claude Code agent coordination — describes exactly this pattern and nothing else. The author's own language: "Issues travel with the repository" and issues are stored in `.beads/issues.jsonl` per repo[^21]. No hydration, `external_projects`, or federation config is mentioned anywhere in that account, despite the scale matching this task's premise almost exactly[^21].

### (b) A single central planning repo

Not documented as a first-class, named pattern anywhere fetched this session. The closest documented analogue is the **OSS-contributor pattern**: route your own planning/tracking issues to a private `~/.beads-planning` directory (via `routing.contributor`, default value `~/.beads-planning`) so they never pollute a PR into someone else's repo[^3][^7]. That is a *personal* planning repo pattern, not a *fleet-wide aggregation* pattern — it exists to keep contributor noise out of upstream, not to give one person a unified view across their own many repos. The FAQ's answer to multi-project tracking generally is blunter still: "initialize bd in a parent directory" as the offered workaround, with cross-project issue references called out as unsupported[^14].

### (c) Native "rigs + multi-repo hydration"

This is real and non-trivial. Per DeepWiki's synthesis of the codebase and confirmed independently against `MULTI_REPO_AGENTS.md`/`ROUTING.md`/`CONFIG.md`, the mechanism has three parts[^4][^2][^7][^5]:

- **Cross-repository routing** — `routing.mode` (`auto` or `explicit`; empty/disabled by default), `routing.maintainer` (default `.`), `routing.contributor` (default `~/.beads-planning`), `routing.default` (fallback target used in `explicit` mode). Role is auto-detected from the git remote URL scheme (SSH ⇒ maintainer, unauthenticated HTTPS ⇒ contributor) or an explicit `beads.role` git-config override[^7][^2].
- **Multi-repo hydration** — `repos.primary` / `repos.additional` (comma-separated paths) tell one `bd` instance to read from multiple Dolt databases and aggregate into a single query surface, tagging every issue with a `source_repo` field for provenance[^2][^4]. `bd init --contributor` sets up routing and hydration together in one step[^7].
- **Cross-project dependency status** (`external_projects`) — maps a project *name* to a relative/absolute path so that `ready`, `blocked`, `show`, `dep`, and `stats` can account for blockers living in another workspace via an explicit `external:<project>:<id>` dependency ID, **without importing** that project's issues into the local database[^5]. This is narrower than hydration: it's a dependency-edge mechanism, not a dashboard mechanism.
- **Dolt Remote Federation ("towns"/"peers")** — `federation.remote` (a Dolt remote URL: `dolthub://`, `gs://`, `s3://`, or `az://`) plus `bd federation add-peer <name> <endpoint>` for genuinely independent installations to sync via Dolt's git-like push/pull[^6][^4]. **Caveat, confirmed this session:** the peer-facing verbs (`list-peers`, `push <peer>`, `pull <peer>`, `sync <peer>`) are documented as having backend support but are explicitly called out as not yet exposed/available in the current release[^6]. Treat federation as beta/aspirational, not something to build a fleet workflow around today.
- **`directory.labels`** — maps directories to labels for monorepo-style filtering (confirmed present in `CONFIG.md`'s schema listing; not covered in `ROUTING.md`, which is a narrower doc)[^5]. This is a monorepo feature (one repo, many sub-project directories), not a multi-repo-fleet feature — worth not conflating the two.
- **`sync.remote`** — referenced in the project's own troubleshooting doc as a queryable config key (`bd config get sync.remote`) for the plain git-remote sync path, distinct from `federation.remote`[^10].

**Contradiction found (flagged, not resolved):** the two official docs describing `federation.sovereignty` tiers disagree with each other on tier semantics. `CONFIG.md`'s schema table describes T1 = "full" and T4 = "unrestricted"[^5]; `FEDERATION-SETUP.md`'s narrative description describes T1 = "no restrictions" (public data) and T4 = "anonymous" (maximum privacy)[^6] — i.e., the two docs put "least restrictive" at opposite ends of the same T1→T4 scale. This is exactly the kind of doc-drift you'd expect from a project changing weekly with docs written at different times. **Tagged `[NO SOURCE]` for the actual tier semantics** — do not rely on either doc's ordering without re-verifying against the current CLI (`bd federation --help` / a fresh `CONFIG.md` read) before using this in production.

### THE LOAD-BEARING QUESTION — what do real users actually do

Weighing the evidence: the mechanism (c) exists and is actively developed, but every independent, non-vendor account of real multi-repo usage found this session — the 20-repo case study[^21], the project's own FAQ answer to the exact question "how do I track work across multiple projects"[^14] — lands on **per-repo `.beads/` plus light manual glue** (a parent-directory `bd init`, or simply keeping issues local to each repo and relying on the developer's own memory/dashboard tooling), not hydration. `MULTI_REPO_MIGRATION.md`'s own worked scenarios are about *role* separation (maintainer vs. contributor, architect vs. implementer) and namespace hygiene, not about *fleet aggregation* for a single person managing many otherwise-unrelated repos[^3]. Even the project's own vendor (DoltHub) documents "multiple clones with separate remotes" as a legitimate, zero-conflict pattern in its own right — "There are no merge conflicts to worry about here, since each Beads Dolt database gets its own clone and coding agent"[^18] — i.e. isolated per-repo/per-clone databases are treated as a first-class design option by the tool's own storage vendor, not merely an absence of configuration. Rigs/hydration reads as the right tool if you're the maintainer of an OSS project fielding drive-by contributor issues, or splitting an "architecture repo" from "implementation repos" for the *same* project — narrower problems than "give me one board across 18 independent repos."

### When to use

- **Per-repo + glue** — default choice for an independent fleet of repos that don't share a dependency graph with each other. Zero config, matches the only real case study at this scale[^21], and avoids the federation layer's documented immaturity[^6].
- **Rigs + hydration (routing/`external_projects`)** — reach for this specifically when two or more repos in the fleet have *actual cross-repo dependency edges* (e.g., a shared library repo blocking several downstream repos) or when there's a genuine maintainer/contributor role split (upstream OSS repos). Not a general-purpose fleet dashboard as of mid-2026.
- **Federation (`bd federation add-peer`)** — not recommended yet for a solo fleet; the peer sync verbs are incomplete in the shipped CLI[^6].
- **Central planning repo** — no first-party pattern found; if wanted, it would have to be built as custom glue (e.g., a script that runs `bd export --all` in each repo's `.beads/` and merges JSONL into one read-only aggregate view) rather than using any native Beads feature.

## 2. Day-to-Day Agent Workflows

The consistent loop across every source (Better Stack, Ralph TUI, ianbull.com, Yegge's own posts) is: **`bd ready` → claim → do the work → `bd close` → repeat**[^8][^9][^15]. The differentiator between sources is what happens around that loop.

**Session start / priming.** `bd onboard` writes a minimal, static pointer into the repo's agent instructions (CLAUDE.md/AGENTS.md) that just tells the agent to run `bd prime` — this is deliberate: static instructions rot, so the static layer is kept as thin as possible and the *dynamic* context comes from `bd prime` itself, which detects its execution context and adapts: ~50 tokens of reminder in MCP-server mode, ~1–2k tokens of full command reference in plain-CLI mode, and a `--hook-json` mode that emits Claude Code's own `SessionStart` hook envelope directly[^17]. Custom `.beads/PRIME.md` or `~/.config/beads/PRIME.md` override the generated defaults per-repo or globally[^17].

**Claiming work.** `bd update <id> --claim` performs assignment + transition to `in_progress` as one atomic operation specifically to avoid the race condition of doing those as two separate commands under concurrent agents[^17].

**Persistent memory.** `bd remember "<insight>"` stores project-level insights that `bd prime` re-injects into future sessions automatically; the documented intent is to **replace** ad hoc `MEMORY.md` files, not supplement them, on the reasoning that a second, un-queried memory file just fragments context[^17].

**Session end — "landing the plane."** The documented completion protocol is: file any newly-discovered follow-up work as issues, run the project's quality gates, close the completed issue(s), and — called out as **mandatory, not optional** — push (`git push` or `bd dolt push`), because work is considered incomplete until it has reached the shared remote; the protocol ends with a handoff summary[^17]. Skipping the push step is explicitly named as an anti-pattern[^17], as is using `bd edit` (opens `$EDITOR`, breaks non-interactive agent execution — agents should use `bd update` flags or stdin instead)[^17].

**Yegge's own operational best practices** (his "Beads Best Practices" post) add a maintenance layer on top of the above loop: run `bd doctor` daily; run `bd cleanup` every few days to bound database size; keep the *active* issue count under roughly 500 (agents start struggling once a working file crosses ~25k tokens); file a bead for anything that will take an agent more than ~2 minutes, including things surfaced incidentally during code review; give each agent **one task, then kill the process and start a fresh one** rather than looping a single long-lived agent across many issues; upgrade `bd` itself every 1–2 weeks; keep issue-prefixes short for human readability; and — a repeated framing point — treat Beads as a *passive tracker*, resisting the temptation to bolt orchestration or scheduling logic onto it directly[^16].

**Planning pipeline (multiple independent sources converge on this shape).** Draft a plan in whatever tool you already use for architecture thinking → convert the plan into a Beads epic → run 2–5 rounds of iterative refinement to add granular acceptance-criteria-level detail before any code is written → then let looped agent sessions claim and close the resulting leaf issues one at a time → human review/testing before merge[^15][^22]. One independent user frames the resulting time allocation explicitly: heavier human involvement at the *start* (plan → epic → refine) and *end* (validation, PR review) of the pipeline, and comparatively hands-off in the middle while agents burn down `bd ready`[^22].

**Ralph TUI's integration** is a good illustration of a third-party orchestrator treating Beads purely as a backing store rather than a workflow engine: it detects `.beads/` + the `bd` binary, pulls tasks with `bd list --json --parent <epic>`, updates status via `bd update`, and flushes state to JSONL with `bd sync --flush-only`; it supports running one parallel session across multiple `--epic` scopes and switching epic focus interactively[^9]. Notably, Ralph TUI's own docs make **no mention** of `bd prime` or `bd remember` — reinforcing that those two commands are specifically an *agent-context* feature, not something every tool integration needs to touch.

### When to use

- Solo/simple sessions: the basic `bd ready`/claim/close loop is sufficient; skip the planning pipeline for anything under ~2 minutes of work (file it directly instead)[^16].
- Multi-session or multi-agent feature work: use the plan → epic → refine → loop pipeline; expect 2–5 refinement passes before task granularity is agent-ready[^15][^22].
- Any Claude-Code-hook-driven fleet: wire `bd prime --hook-json` into `SessionStart` and rely on `bd remember` instead of hand-maintained memory files, per the project's own stated intent[^17].

## 3. Pitfalls, Gotchas, Anti-Patterns

**Dolt merge conflicts across machines/worktrees.** The most concrete, still-open bug found: in a simple two-machine shared-remote setup, every `bd dolt pull` after the other machine has pushed fails with a merge conflict — but the conflict is on the `metadata` table's own bookkeeping columns (`dolt_auto_push_commit`, `dolt_auto_push_last`), not on actual issue data, because both machines' local auto-commits diverge on those housekeeping rows before either side pulls. Opened 2026-03-09; unresolved as of this session, with the only workaround being a destructive `bd dolt push --force` + `dolt reset --hard origin/main`, which risks discarding unpushed local changes[^11]. Separately, Beads auto-creates an internal `.git/beads-worktrees/` directory to support a now-defunct sync-branch feature, which can produce a `"main" is already checked out` error when git's own worktree bookkeeping collides with it; the fix is `rm -rf .git/beads-worktrees && git worktree prune` and `bd config set sync.branch ""` to stop it recurring[^10]. Separately, Beads' exclusive-lock protocol (for handing deterministic control of a database to an external tool, e.g. CI) is explicitly documented as operating independently of git worktrees and as providing no transaction isolation or ACID guarantees between multiple external tools — a cooperative convention, not a security or correctness boundary[^19]. Note this is Beads' *own internal* worktree usage, distinct from a user running Beads inside their own `git worktree` checkouts — no source fetched this session specifically documents Dolt merge behavior when a user's own worktrees each hold a live Beads database pointed at the same Dolt store; this sub-question is tagged **`[NO SOURCE]`**.

**Hash-ID collisions are managed, not eliminated.** Marketing language ("zero-conflict hash-based IDs") should be read against the actual config surface: `min_hash_length`, `max_hash_length`, and `max_collision_prob` (default `0.25`) are all tunable knobs specifically because collision probability is non-zero by design — the system adapts hash length upward as the probability estimate rises, rather than guaranteeing collision-freedom outright[^5]. Separately, the project's own troubleshooting doc confirms collisions were a real, acknowledged problem pre-v0.20.1 and states the fix ships from that version onward[^10].

**JSONL export/import semantics.** `bd export --all --scrub` excludes "test/pollution records" from the JSONL export[^23]. Import behavior around orphaned children (parent deleted, children still reference it) is configurable via `import.orphan_handling`: `allow` (default), `strict`, `resurrect` (recreate the missing parent as a tombstone), or `skip`[^5][^10]. Auto-import after git merge/checkout is on by default (`import.auto`); auto-export after writes is off by default (`export.auto`) and, when enabled, is rate-limited (`export.interval`, default 60s) with a configurable error policy (`strict` / `best-effort` / `partial` / `required-core`) and retry count[^5].

**Version churn / documentation drift.** This is a fast-moving, largely single-author project (Yegge states the initial version was "built in 6 days with Claude"[^15]), with a stated ~1–2 week release cadence[^16] and an explicit FAQ admission that "command flags and data formats can evolve" even though core create/update/dependency/ready-work/sync functionality is called stable[^14]. The practical consequence, observed directly this session: the FAQ's answer to multi-project tracking ("cross-project issue references are unsupported… initialize bd in a parent directory"[^14]) is inconsistent with the considerably more elaborate routing/hydration/`external_projects` system documented in `MULTI_REPO_AGENTS.md` and `ROUTING.md`[^2][^7] — almost certainly because these docs were last substantively edited at different points in the project's fast-moving history. **`[INFERENCE]`**: treat any single Beads doc as possibly stale relative to the current CLI; cross-check against `bd --help` / `bd config list` before depending on any specific config key in production.

**Bus factor / project maturity.** The FAQ explicitly recommends against "mission-critical production systems without a tested backup/restore plan," while calling the core feature set stable and well-tested[^14]. The repository ownership moved from the personal `steveyegge/beads` namespace to an org, `gastownhall/beads` (confirmed live 301 redirect this session)[^1] — no source fetched this session names a second maintainer independently of Yegge, so this remains effectively a single-founder project as far as could be verified (**`[INFERENCE]`**, based on absence of contrary evidence rather than a positive claim). One user went further than merely noting friction: after describing release-over-release performance regressions and background-daemon sync misbehavior, they abandoned Beads entirely and wrote a competing single-bash-script tool ("Ticket") that keeps only the dependency-graph concept[^20]. A commenter on that same thread characterized the project's AI-heavy, fast-iteration development style as "erratic… honestly a bit unprofessional"[^20] — reported here as a documented critical opinion, not adopted as this doc's own judgment.

**Sandbox/CI environment friction.** Restricted-permission sandboxes can prevent Beads from controlling its own Dolt server process, producing "database out of sync" / hash-mismatch symptoms; this is auto-detected from v0.21.1 onward, with a manual `--sandbox` flag as an escape hatch, and `bd doctor --fix` as a last resort[^10].

## 4. Workarounds for Missing / Assumed-Missing Features

The research prompt's framing assumed several gaps that this session's fetches directly contradict — reported here as corrections, not confirmations:

- **Due dates: natively supported, not missing.** `bd create`/`bd update` accept a due-date field with flexible natural-language and ISO parsing (`+6h`, `+1d`, `+2w`, `tomorrow`, `next monday`, `2025-01-15`)[^23]. No workaround needed; no workaround pattern was searched for further given the feature exists.
- **Labels: natively supported, not missing.** `bd create --labels` and `bd label add <issue> <label>` are first-class commands[^23].
- **Status model: not coarse.** Built-in statuses are `open, in_progress, blocked, deferred, closed` — already richer than the prompt's assumed `open/in_progress/closed`-only model — and `status.custom` lets a project define further statuses mapped to one of four categories (`active`, `wip`, `done`, `frozen`), with `types.custom` doing the equivalent for issue types beyond the built-in `bug|feature|task|epic|chore|decision`[^5][^23]. **`[INFERENCE]`**: this means teams wanting a richer workflow (e.g., "in review," "needs QA") can model it natively via `status.custom` rather than needing an external workaround — no source fetched described this pattern being used in practice, but the mechanism is directly documented.
- **Recurring tasks: genuinely absent, no workaround found.** This is the one gap in the prompt's list that held up under scrutiny. No fetched source — official docs, FAQ, Yegge's own posts, or third-party write-ups — mentions native recurring-task support or a documented workaround pattern for it. **`[NO SOURCE]`** — do not infer one; if this matters for the fleet, it will need to be built as external glue (e.g., a cron/scheduler that calls `bd create` on a schedule), which is speculative and untested here.
- **Richer PM-tool features generally.** Beads ships two-way sync config namespaces for Jira, Linear, and Azure DevOps (`jira.*`, `linear.*`, `ado.*`, including status/type/priority/field mapping)[^5]. **`[INFERENCE]`**: the most plausible documented-adjacent path for teams that want due-date reporting, recurring cadences, or richer workflow visualization than Beads itself offers is to sync into one of those upstream tools and use *its* native features, rather than extending Beads — but no source fetched this session explicitly recommends this as a workaround for the specific gaps in question, so this is inference from the existence of the sync config, not a verified practice.

## 5. Community UI Landscape

The project's own `COMMUNITY_TOOLS.md` lists a long tail of community UIs, editor plugins, and orchestration layers, explicitly split into "active" and "15+ historical/stale" tools (the latter flagged as possibly lacking Dolt compatibility)[^12]. Rather than trust the doc's own activity labels, GitHub's API was queried directly this session for the tools with the clearest name-match to the prompt's "beads-web / bd-board / BeadBoard" framing plus the two other viewer projects surfaced by search:

| Tool | GitHub repo | Stars | Last push (this session) | Verdict |
|---|---|---|---|---|
| beads_viewer (TUI: PageRank, critical path, kanban) | `Dicklesworthstone/beads_viewer` | 1,602 | 2026-06-29 | **Actively maintained**, largest of the checked set[^13] |
| beads_rust (Rust reimplementation of `bd` itself) | `Dicklesworthstone/beads_rust` | 983 | 2026-07-04 | **Actively maintained**, pushed within the last 2 days[^13] |
| beads-ui (`bdui`, local web UI) | `mantoni/beads-ui` | 654 | 2026-07-03 | **Actively maintained**, pushed within the last 3 days[^13] |
| beads-viewer (web UI, React Flow dependency graph) | `mgalpert/beads-viewer` | 28 | 2025-10-16 | **Stale** — no push in ~9 months as of this session; matches the project's own launch month, suggesting an early experiment that was not kept current[^13] |
| gastownhall/beads (canonical repo, for scale reference) | `gastownhall/beads` | 25,101 | 2026-07-05 | Reference point — the core project itself, pushed the day before this session[^13] |

> **⚠️ Correction (2026-07-18, folded in from §6 below via Codex flagship research):** the `beads_viewer` row above (actively maintained, 1,602 stars, pushed 2026-06-29) is now stale. Beads' own `COMMUNITY_TOOLS.md` marks `beads_viewer` historical/incompatible with Dolt-backed Beads 0.50+, confirmed by the project's own issue tracker (`Dicklesworthstone/beads_viewer#121` — Dolt is not supported)[^40][^41]. Do not adopt it going forward; see §6.4 for current Dolt-compatible alternatives (Mardi Gras, Bead Me Up Scotty, Foolery).

**Specifically on the prompt's named tools:** neither a repo literally named `beads-web` nor `bd-board` was found as an independently addressable, star-verifiable GitHub repository this session — `COMMUNITY_TOOLS.md` lists "beads-web" (cross-platform single-binary, TypeScript/Rust) and "bd-board" (React/TanStack dashboard by a contributor credited as `@jeanpfs`) as entries in its own catalog[^12], but neither could be resolved to a specific `owner/repo` pair via search within this session's effort budget, so their star counts / commit recency are **`[NO SOURCE]`** here rather than guessed. "BeadBoard" does resolve to a real, distinct project (`zenchantlive/beadboard`, described as multi-agent orchestration built on Beads[^12]), but the GitHub API call for that exact path returned no data this session (likely a private repo, a renamed/moved repo, or a search-result naming mismatch) — also **`[NO SOURCE]`** for its activity metrics.

**Bottom line for the UI landscape:** the ecosystem is real and has several genuinely active, recently-pushed tools (`beads_viewer`, `beads_rust`, `beads-ui` all pushed within the last week of this session), but it is fragmented across many small single-maintainer side projects rather than consolidated around one or two dominant UIs — consistent with `COMMUNITY_TOOLS.md`'s own admission that roughly half its catalog is historical/stale[^12].

## 6. CC TUI Task Panel — Beads Integration, Customization & Alternatives

Research verified through 2026-07-18. This section treats the native Claude Code task list, the durable Beads backlog, and live-agent monitoring as distinct surfaces. It does not revisit Beads topology (§1-§5) or the existing `/load-task-panel` skill's repaint workaround.

### 6.1 Native task-panel behavior and customization surface

Claude Code's supported interface remains deliberately small. The official interactive-mode documentation says the panel displays "up to five tasks at once," is toggled with `Ctrl+T`, can be supplemented by asking Claude to "show me all tasks," and can share a namespace through `CLAUDE_CODE_TASK_LIST_ID`. The built-in tool set comprises `TaskCreate`, `TaskGet`, `TaskList`, and `TaskUpdate`; `TodoWrite` is now categorized as legacy. Because these properties are explicitly documented, five tasks should be treated as the supported display ceiling rather than merely a local configuration default.[^24][^25]

Anthropic did make the panel height-aware in Claude Code 2.1.20: the changelog says it "dynamically adjust[s] visible items based on terminal height." That release also added task deletion and fixed navigation when the selected item was outside the visible portion. The current manual nevertheless continues to say "up to five," so the height calculation changes how many of those five fit — rather than documenting an expanded or user-selectable maximum.[^26][^24]

The published `TaskCreate`/`TaskUpdate` schemas provide subject, description, active-form text, status, dependencies, owner, and arbitrary metadata, but no priority, rank, pinned, starred, or display-order field. The current settings and environment-variable references likewise document no panel-size, sort-order, or task-pinning control. Arbitrary metadata is therefore suitable for bridge bookkeeping, but it has no documented effect on the renderer.[^27][^28][^29]

A read-only inspection of the locally installed official Claude Code 2.1.215 bundle found a version-specific overflow algorithm:

1. The visible count varies between three and five according to terminal height.
2. Tasks completed within roughly the last 30 seconds are promoted first.
3. `in_progress` tasks come next.
4. Pending tasks follow, with unblocked tasks before blocked tasks.
5. Older completed tasks come last.
6. Within those groups, numeric IDs sort numerically and other IDs lexically.

When the list does not overflow, it sorts directly by task ID. Consequently, repeating a net-zero `TaskUpdate` does not reorder ordinary pending tasks; only changing status or dependencies moves a task between renderer buckets, while completion gives a short-lived promotion. Because this was derived from a minified local implementation rather than a published contract, it may change without notice and should not be made a durable integration dependency. **`[INFERENCE]`**

This implementation also explains why "create tasks in desired order" works only conditionally: task IDs normally reflect creation order, but ranking changes cannot be expressed without deleting and recreating mirror-owned tasks or abusing their status/dependency semantics. Recreating tasks causes ID churn, while status abuse corrupts the meaning presented to both Claude and the user. **`[INFERENCE]`**

No current official documentation, changelog entry, or public issue located in this search promises configurable task count, priority ordering, or task pinning. Anthropic's internal roadmap is not public, and an undocumented flag cannot be disproved; the defensible result is therefore: **native panel count/sort customization and its roadmap: no significant public development found.** **`[NO SOURCE]`**

Several issue states have changed since this fleet's earlier check (the `/load-task-panel` skill's notes cite these as open — update accordingly):

- The repaint defect tracked in issue #50656 is now **closed as "not planned,"** not open. Its report still supports the distinction between persisted task data and TUI render state, but the existing local workaround (the net-zero `TaskUpdate` repaint trick) remains necessary where the bug reproduces — Anthropic will not fix it.[^30]
- Issue #20664, concerning forked sessions not inheriting a shared `CLAUDE_CODE_TASK_LIST_ID`, is also **closed as "not planned."** Wrappers that depend on a pinned namespace should continue exporting it into every spawned/forked process rather than assuming inheritance.[^31]
- Issue #20424, concerning task-list namespaces in non-interactive execution, is closed, but the issue record does not establish a supported general fix. Closure alone is insufficient evidence that all non-interactive namespace cases are resolved.[^32]

### 6.2 Community customization and outside-panel viewers

The community's prevailing response has been to read `~/.claude/tasks/` into another interface, not to patch the built-in renderer:

- **Claude Task Viewer** provides a browser-based cross-session Kanban board, dependency view, timeline, search, and live updates from Claude's task/session files. It intentionally leaves task status and task descriptions under Claude's control, making it primarily an observational surface.[^33]
- **Claude Code Kanban** watches `~/.claude` and presents tasks, sessions, agents, messages, context usage, and waiting indicators. Its documentation explicitly says it observes rather than directs Claude. Release 4.14.0 was published on 2026-07-12, inside the post-2026-07-06 window.[^34]
- **Huba** is the closest match to a "task panel outside the TUI": a terminal application that searches, filters, sorts, creates, edits, and deletes native Claude task files across projects, with live reload and dependency support.[^35]
- **Tasc** is a smaller Rust TUI for browsing Claude task lists, sessions, details, and dependencies.[^36]

These tools solve the five-row constraint for human review, and Huba provides external ordering and editing. They do not change which tasks Claude Code's own panel selects, nor do they make Beads authoritative. "Pin" features found in adjacent tools generally pin sessions or messages, not a native `TaskCreate` item.[^33][^34][^35]

Searches across GitHub and community discussions using both renderer-oriented terms and file-format-oriented terms found no maintained hook, plugin, MCP server, or script that patches the native panel's five-task limit, introduces task pinning, or changes its renderer ordering. Negative search results cannot establish nonexistence, but the evidence strongly suggests that users route around the panel instead of customizing it. **`[NO SOURCE]`**

Directly editing `~/.claude/tasks/` is particularly weak as an integration strategy. The file layout is not documented as a stable public API; file writes need not trigger the same in-process state changes or repaint path as a successful `TaskUpdate`; and a future Claude Code release may change validation or persistence details. Community viewers demonstrate that read-only consumption is practical, but their existence does not make direct writes supported. **`[INFERENCE]`**

### 6.3 What Beads' Claude integration actually does

`bd setup claude` does **not** connect Beads issues to Claude Code's native `Task*` tools or task panel. The official installation guide says it installs `SessionStart` and `PreCompact` hooks that inject `bd prime` workflow context, after which Claude uses the `bd` CLI. The optional Beads plugin adds `/beads:*` commands and a task agent, but neither path claims to create or synchronize native Claude tasks. The two systems remain fully separate after the official setup.[^37]

Beads does ship an official MCP integration. It exposes Beads operations — initialize, create, list, ready, show, update, close, dependency management, comments, notes, blocked issues, statistics, reopening, workspace selection — but it does **not** expose Claude's `TaskCreate`/`TaskUpdate`, and its own documentation recommends the CLI-plus-hooks path for Claude Code because loading MCP tool schemas costs substantially more context. MCP is consequently another way for Claude to manipulate Beads — not a native-panel bridge.[^38]

Claude hooks expose useful bridge events but not a complete imperative task-panel API. `TaskCreated` and `TaskCompleted` can observe those lifecycle points, while generic `PreToolUse`/`PostToolUse` hooks can match `TaskUpdate`. A `SessionStart` hook can inject context or invoke an MCP hook tool, but the documented hook contract does not let an external shell script directly command the running Claude client to execute a built-in `TaskCreate`. A supported bridge therefore has to ask Claude — through a skill, command, or injected reconciliation instruction — to perform the `Task*` calls itself.[^39]

Because the hook surface can observe successful tool calls but cannot independently maintain the built-in task store through a documented API, "automatic continuous synchronization" would otherwise require unsupported direct JSON writes or repeated model turns. That boundary is architectural, not merely missing glue code. **`[INFERENCE]`**

No official Beads component or community project located in this search mirrors `bd ready`/`bd list --json` into native Claude tasks, or reflects native task changes back into Beads. Alternate searches using `TaskCreate`, `CLAUDE_CODE_TASK_LIST_ID`, `~/.claude/tasks`, and "Claude task panel" also found no published bridge. **Beads ↔ native Claude task bridge: no significant implementation found.** **`[NO SOURCE]`**

**A viable one-way projection** — the supported task schemas make a conservative bridge possible without pretending the two systems have identical semantics:

1. Query the authoritative Beads store for the session's ranked `ready` set.
2. Select exactly the top five items.
3. Ask Claude to reconcile that projection through `TaskCreate`/`TaskUpdate`.
4. Mark projected tasks using metadata such as `mirror_owner`, `bead_id`, `repo`, `source_hash`, and `rank`.
5. Update or delete only tasks bearing that ownership marker; leave session-local scratch tasks alone.
6. Never automatically close a Beads issue merely because a Claude task disappeared or was marked complete.
7. On a genuine priority reorder, recreate only mirror-owned tasks if creation-ID order is still required by the current renderer.

This design uses Beads as the sole conflict winner, makes reconciliation idempotent through a source hash, and avoids loops because native tasks are a projection rather than a peer database. **`[INFERENCE]`**

There is one unavoidable product decision: five projected Beads items leave no room for scratch tasks in the same namespace, while mixing them means some ranked Beads items may be hidden by renderer buckets. A practical policy is either "three Beads now/next plus up to two scratch tasks" or a separate namespace for the pure five-item Beads mirror. The correct choice depends on whether the panel is primarily a project-priority display or a live execution checklist. **`[HEURISTIC]`**

### 6.4 Beads TUIs and shared fleet surfaces

The `beads_viewer` recommendation in §5 above is now stale — see the correction note there. Beads' official community-tools catalog now marks `beads_viewer` historical/incompatible with Dolt-backed Beads 0.50+, and the project's own issue #121 confirms Dolt support is missing.[^40][^41]

The current official catalog explicitly advises Dolt-era tools to operate through the `bd` CLI rather than parsing the former `issues.jsonl` representation. That is an important selection criterion for any new bridge or viewer: a tool that reads only the legacy JSONL cache risks stale state or outright incompatibility.[^40]

**Mardi Gras** is a current terminal-native Beads surface. It reads one project — by current directory or a specified project path — through `bd list --json`, polls for agent changes, supports issue workflows, and can dispatch Claude Code, Cursor, or Codex in tmux. With the broader Gas Town stack it can show agent rosters, convoys, mail, and costs. Its documented basic mode is nevertheless project-scoped; it is not an 18-repository aggregation dashboard for arbitrary sessions already launched outside it.[^42]

The current Beads catalog lists newer multi-project web and native options. In particular, **Bead Me Up, Scotty** advertises cross-repository issue discovery, prioritization, and reordering — better suited than a per-repository TUI for fleet-wide backlog review, though it remains an issue-management board rather than a live Claude-session monitor.[^40][^43]

**Foolery** goes further: a local multi-repository orchestration dashboard with Beads and Knots backends, queued/active/review/history views, agent/model/status tracking, and embedded terminals. Its 0.14.18 release landed on 2026-07-14 — the clearest newly found attempt to combine durable Beads work with active agent execution, although its rapid versioning and young codebase argue for a pilot rather than immediate fleet-wide dependence.[^44]

Repository history did not provide sufficiently clear evidence to state exactly when every current `COMMUNITY_TOOLS.md` entry was added. The concrete post-2026-07-06 developments verified here are the dated Foolery and Claude Code Kanban releases; no bridge-specific Beads release was found during that interval. **`[NO SOURCE]`**

### 6.5 Wider multi-session agent visibility

Anthropic itself now provides **Agent View**, which is more relevant than the task panel for answering "what are all my Claude sessions doing?" It groups sessions by state and directory, shows working/waiting/done status, supports peeking and replying, and lets the user pin and reorder **sessions**. Those controls apply to sessions, not to individual native tasks. Agent View therefore addresses the fleet-presence problem while leaving backlog priority to another system.[^45][^46]

Terminal-oriented alternatives divide into two categories:

- **Agent Deck** manages Claude, Codex, Gemini, and OpenCode sessions in tmux, with groups, worktrees, remote sessions, cost information, and inferred running/waiting/idle/error status. It is a strong multi-agent session switcher but not a durable issue tracker.[^47]
- **Ralph TUI** can use Beads as a tracker, choose ready work according to blockers and priority, and run multiple workers over a dependency graph. It provides rich visibility for runs that Ralph itself owns, rather than passively discovering every pre-existing CC/Codex/Cursor session.[^48]

GUI products follow the same orchestrator-owned-session pattern:

- **Conductor** creates isolated or shared workspaces for parallel Claude, Codex, and Cursor agents.[^49]
- **Superset** launches agents from task prompts into parallel local workspaces and centralizes approvals and interaction.[^50]
- **Traycer** runs Claude Code, Codex, OpenCode, and Cursor side by side and adds its own planning and multi-agent surface. Its public material is primarily product documentation and marketing, so it supports capability identification but not an independent reliability judgment.[^51]

Across both terminal and GUI tools, the common pattern is to obtain reliable status by launching or owning the agent process. Passive observation of arbitrary sessions is harder because each agent exposes different state and event protocols. A universal dashboard that simultaneously discovers unmanaged sessions, understands each session's next action, and reconciles that with a cross-repository issue graph was not found. **`[INFERENCE]`**

### 6.6 Gaps, blindspots & emergent findings

**Blindspot: three different problems are being treated as one.** The research framing initially suggests one synchronized task surface, but the evidence separates three planes:

| Plane | Question it answers | Best current authority |
|---|---|---|
| Durable work | What should the project or fleet do, and in what priority? | Beads |
| Session presence | Which agents are running, waiting, blocked, or done? | Agent View or an orchestrator |
| Local execution | What is this one Claude session doing now and immediately next? | Native `Task*` list |

A single bidirectional data model would force project issues, live process state, and ephemeral reasoning steps into one lifecycle. The resulting sync conflicts would be semantic, not merely technical. **`[INFERENCE]`**

**Emergent finding: the native panel is a lossy projection surface.** Its five-item cap, lack of rank, implementation-defined ordering, and session namespace make the panel a poor system of record but a reasonable "now/next" display. The correct abstraction is therefore not database synchronization; it is deterministic projection from Beads into a deliberately small view. **`[INFERENCE]`**

**Emergent finding: there is no true priority field to synchronize.** A Beads priority can be preserved in task metadata for identity and diagnostics, but the current native renderer ignores it. Display priority has to be expressed through top-five membership, creation ID, status, or dependency state. Only membership is semantically clean. **`[INFERENCE]`**

**Blindspot: native-file concurrency and durability.** Community viewers prove that multiple readers can use the native task files, but no published contract was found for concurrent external writes, locking, atomic replacement, schema migration, or corruption recovery. Any design that writes those files directly would assume behavior that Anthropic has not committed to. **`[NO SOURCE]`**

**Blindspot: fork and namespace ownership.** A fixed `CLAUDE_CODE_TASK_LIST_ID` enables sharing, but issue #20664 shows that forked processes cannot be assumed to inherit it. More importantly, two sessions writing the same projected namespace could race or interpret scratch tasks differently. The wrapper must propagate the namespace, and only one reconciler should own a shared mirror at a time unless optimistic locking is added outside Claude's task API. **`[INFERENCE]`**

**Emergent finding: specialized hooks are lifecycle-incomplete.** There are dedicated `TaskCreated` and `TaskCompleted` hook events but no dedicated `TaskUpdated` event; generic tool hooks can still observe `TaskUpdate`. A reverse bridge is technically observable but would need to distinguish user intent from model housekeeping, deduplicate retries, and prevent write-back loops. That makes reverse synchronization much less reliable than its apparent implementation size suggests.[^39]

**Contrarian finding: replacing the panel may be better than expanding it.** The community has produced multiple fuller viewers, while Anthropic has introduced Agent View for session monitoring and Beads now has current Dolt-compatible boards. This indicates the native panel is converging on a compact, session-local role — not a fleet dashboard. Trying to turn it into the sole review surface fights both its data model and renderer. **`[INFERENCE]`**

### 6.7 Concrete integration options

| # | Option | Benefits | Costs and failure modes | Judgment |
|---:|---|---|---|---|
| 1 | Keep Beads and native tasks completely separate | No bridge maintenance; both systems used as designed | Manual drift; the panel does not reflect authoritative priority | Acceptable baseline, but leaves the ergonomic problem unresolved. _(heuristic)_ |
| 2 | One-way Beads → native top-five projection | Authoritative priority appears in-session; conflict direction unambiguous; small implementation | Requires Claude-mediated reconciliation; rank changes may require mirror-task recreation; five-item contention with scratch tasks | **Best native-panel integration.** _(inference)_ |
| 3 | Bidirectional Beads ↔ native-task synchronization | Either surface could update the other | Lifecycle mismatch, feedback loops, ambiguous deletion/completion, namespace races, no documented external task-write API | Reject unless a concrete reverse-sync use case justifies the complexity. _(inference)_ |
| 4 | Multi-project Beads board for review + Agent View for sessions + native panel kept local | Each plane gets a fit-for-purpose interface; no need to force >5 tasks into the panel | More than one window; requires choosing and maintaining a Beads board | **Best fleet operating model.** _(inference)_ |
| 5 | Adopt Huba or Claude Task Viewer as an expanded native-task interface | Immediately solves visibility beyond five; cross-session review | Does not remove Beads/native drift; depends on private local files | Useful diagnostic/local-execution viewer, not the fleet backlog.[^33][^35] |
| 6 | Make native Claude tasks authoritative and mirror them into Beads | Optimizes for the interface currently in front of the operator | Loses tool neutrality and durable project semantics; depends on session namespaces and a private store | Wrong authority direction for a multi-agent, multi-repository fleet. _(inference)_ |

### 6.8 Recommendation (§6 scope)

Use a layered combination of options 2 and 4. Keep Beads as the only durable authority; use a Dolt-compatible multi-project Beads board (Bead Me Up Scotty, or pilot Foolery) for backlog review, and Agent View for live-session state. Recast the native panel as a five-item session overlay: a one-way, idempotent projection of the highest-ranked `bd ready` items plus explicitly bounded scratch capacity. Store Beads identity and a source hash in task metadata, mutate only bridge-owned tasks, and never infer a Beads close from native-task disappearance or completion. Invoke reconciliation through a skill or injected instruction that makes supported `Task*` calls; do **not** write `~/.claude/tasks/` directly. Pilot Foolery if integrated Beads-plus-session orchestration is desirable, but keep the projection independent of that young UI so the authority boundary survives tool churn. **`[INFERENCE]`**

**Direct implication for this fleet's open tasks:** this is the concrete shape for `ISSUE-REF CC #54` ("re-implement `/load-task-panel` as a `bd ready` mirror") and `ISSUE-REF`/`ISSUE-REF` — build the one-way projection in §6.3, not a bidirectional sync (option 3 above is explicitly rejected). Evaluate **Agent View** (native, already shipped — `Ctrl` shortcut per docs, pin/reorder *sessions*) as a low-effort partial answer to the "surface what's being worked on" half of ISSUE-REF's ask before building anything custom for that half; the reordering/expansion half (>5 tasks, priority ordering) still has no native or community fix and remains this fleet's problem to solve via the projection design above.

## Comparison

| # | Criterion | (a) Per-repo `.beads/` + glue | (b) Central planning repo | (c) Rigs + hydration/routing |
|---|---|---|---|---|
| 1 | Documented as a first-class native mechanism | No (it's just the default, unconfigured state) | No (no first-party pattern found; would be custom glue) | Yes — routing, hydration, `external_projects`, federation are all real config surfaces[^2][^5][^7] |
| 2 | Evidence of real use at ~18–20-repo scale | Yes — the only concrete case study found (20 repos, 857+ issues)[^21] | None found | None found at this specific scale; documented use cases are narrower (OSS contributor isolation, architect/implementer split)[^3] |
| 3 | Setup effort | Zero — `bd init` per repo | Custom scripting required (no native support) | Non-trivial: routing mode, `repos.additional`/`external_projects` per repo, role detection config[^7] |
| 4 | Gives a genuinely unified cross-repo view | No, by design | Yes, if built | Yes, for hydration-configured repos; federation (independent installs) is not yet fully usable[^6] |
| 5 | Maturity / stability risk | Lowest — you're only exposed to single-repo bugs | N/A (not native) | Higher — federation verbs incomplete[^6]; `federation.sovereignty` semantics contradict across official docs (see §1) |
| 6 | Best fit | Independent repos with no real dependency edges between them | Not recommended — no native support to build on | Repos with actual cross-repo dependency edges, or genuine maintainer/contributor role splits |

## Recommendation

For an ~18-repo solo fleet with no strong cross-repo dependency graph between the repos themselves, start with **per-repo `.beads/` (pattern a) plus a thin, custom aggregation script** (e.g., a scheduled job that runs `bd list --json` per repo and merges into one read-only view) rather than adopting the native rigs/hydration mechanism as the primary cross-fleet UX. This is the pattern with actual evidence at comparable scale[^21], avoids depending on the federation layer's documented incompleteness[^6], and sidesteps a genuine, unresolved contradiction in the sovereignty-tier docs (§1). Reach for `routing`/`external_projects`/hydration selectively, on a per-repo-pair basis, only where a real dependency edge exists between two specific repos (e.g., a shared internal library repo blocking several downstream repos) — that is the use case the mechanism is actually built and evidenced for[^2][^5]. Re-evaluate this recommendation if the federation layer's peer-sync verbs ship and get independent real-world validation, since that would materially change the calculus for a "many independent repos, one view" use case.

**Confidence: medium.** The refutation of "rigs/hydration is the fleet-scale answer" rests on one strong case study plus one internally-inconsistent FAQ-vs-docs signal from the maintainers themselves — not a broad survey of many independent fleets at this exact scale, because none beyond the one case study were found within this session's search effort.

## Open Questions

1. Does Dolt's cell-level merge actually behave safely when multiple git worktrees of the *same* repo each hold a live Beads database pointed at a shared Dolt remote (as opposed to Beads' own internal worktree usage for its now-defunct sync-branch feature, which is documented)? No source found this session addresses the user-worktree case directly — **`[NO SOURCE]`**.
2. What are the *actual*, current semantics of `federation.sovereignty` T1–T4? Two official docs contradict each other (§1); this needs a direct CLI/source-code check (`bd federation --help`, or the Go source) rather than another doc fetch, since the docs themselves are the disagreeing parties.
3. Has anyone published a case study of rigs/hydration used specifically as a cross-fleet dashboard for many *unrelated* repos (as opposed to the documented contributor-isolation and architect/implementer use cases)? None was found; if one exists, it would directly strengthen or weaken this doc's Recommendation.
4. Are `beads-web` and `bd-board` (as named in `COMMUNITY_TOOLS.md`) resolvable to specific, currently-active GitHub repos? Not resolved within this session's effort budget.

## Sources

[^1]: gastownhall (formerly steveyegge). (2026). [Beads — A memory upgrade for your coding agent](https://github.com/gastownhall/beads). GitHub. Verified accessible (HTTP 301 redirect from `github.com/steveyegge/beads` → `github.com/gastownhall/beads`, then live) 2026-07-06.
[^2]: gastownhall/beads. (2026). [MULTI_REPO_AGENTS.md](https://github.com/steveyegge/beads/blob/main/docs/MULTI_REPO_AGENTS.md). GitHub. Verified accessible (HTTP 200) 2026-07-06.
[^3]: gastownhall/beads. (2026). [MULTI_REPO_MIGRATION.md](https://github.com/steveyegge/beads/blob/main/docs/MULTI_REPO_MIGRATION.md). GitHub. Verified accessible (HTTP 200) 2026-07-06.
[^4]: DeepWiki. (2026). [Multi-Repository Features | steveyegge/beads](https://deepwiki.com/steveyegge/beads/9-multi-repository-features). Verified accessible (HTTP 200) 2026-07-06. (Third-party auto-generated codebase synthesis, not an official doc — treated as corroborating, not primary.)
[^5]: gastownhall/beads. (2026). [CONFIG.md](https://github.com/steveyegge/beads/blob/main/docs/CONFIG.md). GitHub. Verified accessible (HTTP 200) 2026-07-06.
[^6]: gastownhall/beads. (2026). [FEDERATION-SETUP.md](https://github.com/steveyegge/beads/blob/main/FEDERATION-SETUP.md). GitHub. Verified accessible (HTTP 200) 2026-07-06.
[^7]: gastownhall/beads. (2026). [ROUTING.md](https://github.com/steveyegge/beads/blob/main/docs/ROUTING.md). GitHub. Verified accessible (HTTP 200) 2026-07-06.
[^8]: Better Stack Community. (2026). [Beads: A Git-Friendly Issue Tracker for AI Coding Agents](https://betterstack.com/community/guides/ai/beads-issue-tracker-ai-agents/). Verified accessible (HTTP 200) 2026-07-06.
[^9]: Ralph TUI. (2026). [Beads Tracker plugin docs](https://ralph-tui.com/docs/plugins/trackers/beads). Verified accessible (HTTP 200) 2026-07-06.
[^10]: gastownhall/beads. (2026). [TROUBLESHOOTING.md](https://github.com/steveyegge/beads/blob/main/docs/TROUBLESHOOTING.md). GitHub. Verified accessible (HTTP 200) 2026-07-06.
[^11]: gastownhall/beads. (2026). [Issue #2466 — `bd dolt pull` fails with recurring merge conflicts on `metadata` table in multi-machine setups](https://github.com/steveyegge/beads/issues/2466). GitHub. Verified accessible (HTTP 200) 2026-07-06. (Opened 2026-03-09; open/unresolved as fetched.)
[^12]: gastownhall/beads. (2026). [COMMUNITY_TOOLS.md](https://github.com/gastownhall/beads/blob/main/docs/COMMUNITY_TOOLS.md). GitHub. Verified accessible (HTTP 200) 2026-07-06.
[^13]: [GitHub REST API — Repositories](https://docs.github.com/en/rest/repos/repos). `api.github.com/repos/<owner>/<repo>` queried live 2026-07-06 for `Dicklesworthstone/beads_viewer`, `Dicklesworthstone/beads_rust`, `mantoni/beads-ui`, `mgalpert/beads-viewer`, and `gastownhall/beads` (HTTP 200 for each; `zenchantlive/beadboard` and `steveyegge/bd-board` returned no data / not found).
[^14]: gastownhall/beads. (2026). [FAQ.md](https://github.com/steveyegge/beads/blob/main/docs/FAQ.md). GitHub. Verified accessible (HTTP 200) 2026-07-06.
[^15]: Yegge, S. (2025). [Introducing Beads: A coding agent memory system](https://steve-yegge.medium.com/introducing-beads-a-coding-agent-memory-system-637d7d92514a). Medium. Verified accessible (HTTP 200) 2026-07-06.
[^16]: Yegge, S. (2026). [Beads Best Practices](https://steve-yegge.medium.com/beads-best-practices-2db636b9760c). Medium. Verified accessible (HTTP 200) 2026-07-06.
[^17]: DeepWiki. (2026). [Agent Workflow Guidelines | steveyegge/beads](https://deepwiki.com/steveyegge/beads/8.2-agent-workflow-guidelines). Verified accessible (HTTP 200) 2026-07-06. (Third-party auto-generated codebase synthesis, not an official doc — treated as corroborating, not primary.)
[^18]: DoltHub. (2026). [Common Beads Classic Workflows](https://www.dolthub.com/blog/2026-04-15-common-beads-workflows/). Verified accessible (HTTP 200) 2026-07-06.
[^19]: gastownhall/beads. (2026). [EXCLUSIVE_LOCK.md](https://github.com/steveyegge/beads/blob/main/docs/EXCLUSIVE_LOCK.md). GitHub. Verified accessible (HTTP 200) 2026-07-06.
[^20]: Hacker News. (2026). [Show HN: I replaced Beads with a faster, simpler Markdown-based task tracker](https://news.ycombinator.com/item?id=46487580). Verified accessible (HTTP 200) 2026-07-06.
[^21]: wal.sh. (2026). [Beads/bd — Git-Native Issue Tracking](https://wal.sh/research/beads). Verified accessible (HTTP 200) 2026-07-06. (Independent user's own account of using bd across 20+ of their own repositories.)
[^22]: Simpson, J. (2026). [Using Beads to supercharge my workflow](https://jonsimpson.ca/using-beads-to-supercharge-my-workflow/). Verified accessible (HTTP 200) 2026-07-06.
[^23]: gastownhall/beads. (2026). [CLI_REFERENCE.md (raw)](https://raw.githubusercontent.com/steveyegge/beads/main/docs/CLI_REFERENCE.md). GitHub. Verified accessible (HTTP 200) 2026-07-06.
[^24]: Anthropic. (2026). [Interactive mode](https://code.claude.com/docs/en/interactive-mode). Verified accessible (HTTP 200) 2026-07-18.
[^25]: Anthropic. (2026). [Tools reference](https://code.claude.com/docs/en/tools-reference). Verified accessible (HTTP 200) 2026-07-18.
[^26]: Anthropic. (2026). [Claude Code changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md). GitHub. Verified accessible (HTTP 200) 2026-07-18.
[^27]: Anthropic. (2026). [Agent SDK reference — TypeScript](https://code.claude.com/docs/en/agent-sdk/typescript). Verified accessible (HTTP 200) 2026-07-18.
[^28]: Anthropic. (2026). [Claude Code settings](https://code.claude.com/docs/en/settings). Verified accessible (HTTP 200) 2026-07-18.
[^29]: Anthropic. (2026). [Environment variables](https://code.claude.com/docs/en/env-vars). Verified accessible (HTTP 200) 2026-07-18.
[^30]: Anthropic. (2026). [Tasks don't render in TUI after session resume until task mutation, issue #50656](https://github.com/anthropics/claude-code/issues/50656). GitHub. Verified accessible (HTTP 200) 2026-07-18. (Closed "not planned.")
[^31]: Anthropic. (2026). [Forked sessions don't inherit `CLAUDE_CODE_TASK_LIST_ID`, issue #20664](https://github.com/anthropics/claude-code/issues/20664). GitHub. Verified accessible (HTTP 200) 2026-07-18. (Closed "not planned.")
[^32]: Anthropic. (2026). [Task list namespace behavior in non-interactive sessions, issue #20424](https://github.com/anthropics/claude-code/issues/20424). GitHub. Verified accessible (HTTP 200) 2026-07-18. (Closed.)
[^33]: L1AD. (2026). [Claude Task Viewer](https://github.com/L1AD/claude-task-viewer). GitHub. Verified accessible (HTTP 200) 2026-07-18.
[^34]: NikiforovAll. (2026). [Claude Code Kanban](https://github.com/NikiforovAll/claude-code-kanban). GitHub. Verified accessible (HTTP 200) 2026-07-18. (v4.14.0 released 2026-07-12.)
[^35]: Zwezdin, S. (2026). [Huba](https://github.com/sergeyzwezdin/huba). GitHub. Verified accessible (HTTP 200) 2026-07-18.
[^36]: Missoh, D. (2026). [Tasc](https://github.com/dmissoh/tasc). GitHub. Verified accessible (HTTP 200) 2026-07-18.
[^37]: gastownhall/beads. (2026). [INSTALLING.md](https://github.com/gastownhall/beads/blob/main/docs/INSTALLING.md). GitHub. Verified accessible (HTTP 200) 2026-07-18.
[^38]: gastownhall/beads. (2026). [Beads MCP Server README](https://github.com/gastownhall/beads/blob/main/integrations/beads-mcp/README.md). GitHub. Verified accessible (HTTP 200) 2026-07-18.
[^39]: Anthropic. (2026). [Hooks reference](https://code.claude.com/docs/en/hooks). Verified accessible (HTTP 200) 2026-07-18.
[^40]: gastownhall/beads. (2026). [COMMUNITY_TOOLS.md](https://github.com/gastownhall/beads/blob/main/docs/COMMUNITY_TOOLS.md). GitHub. Verified accessible (HTTP 200) 2026-07-18.
[^41]: Dicklesworthstone. (2026). [Dolt is not supported, issue #121](https://github.com/Dicklesworthstone/beads_viewer/issues/121). GitHub. Verified accessible (HTTP 200) 2026-07-18.
[^42]: quietpublish. (2026). [Mardi Gras](https://github.com/quietpublish/mardi-gras). GitHub. Verified accessible (HTTP 200) 2026-07-18.
[^43]: Brendan AppStart. (2026). [Bead Me Up, Scotty](https://github.com/brendan-appstart/bead-me-up-scotty). GitHub. Verified accessible (HTTP 200) 2026-07-18.
[^44]: acartine. (2026). [Foolery](https://github.com/acartine/foolery). GitHub. Verified accessible (HTTP 200) 2026-07-18. (v0.14.18 released 2026-07-14.)
[^45]: Anthropic. (2026). [Manage multiple agents with agent view](https://code.claude.com/docs/en/agent-view). Verified accessible (HTTP 200) 2026-07-18.
[^46]: Anthropic. (2026). [Agent view in Claude Code](https://claude.com/blog/agent-view-in-claude-code). Verified accessible (HTTP 200) 2026-07-18.
[^47]: Goplani, A. (2026). [Agent Deck](https://github.com/asheshgoplani/agent-deck). GitHub. Verified accessible (HTTP 200) 2026-07-18.
[^48]: Ralph TUI. (2026). [Beads Tracker plugin docs](https://ralph-tui.com/docs/plugins/trackers/beads). Verified accessible (HTTP 200) 2026-07-18.
[^49]: Conductor. (2026). [Parallel agents](https://www.conductor.build/docs/concepts/parallel-agents). Verified accessible (HTTP 200) 2026-07-18.
[^50]: Superset. (2026). [AI Agents](https://docs.superset.sh/agent-integration). Verified accessible (HTTP 200) 2026-07-18.
[^51]: Traycer. (2026). [Traycer AI](https://traycer.ai/). Verified accessible (HTTP 200) 2026-07-18.

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node; not applicable to a manual ad-hoc research-doc run)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** ad-hoc (no aido registry entry — Claude ad-hoc web research per the `research-doc` skill)
**Model:** `claude-sonnet-5`
**Date:** 2026-07-06

```text
Read `~/.claude/skills/research-doc/SKILL.md` in full and the canonical template
`/workspace/projects (template_version
research-1.2.0) in full. Follow that skill's method EXACTLY (all 7 mechanisms:
grounding anchor, >=3-framing researcher fan-out, 5-perspective adversarial critic
pass, live citation validation via WebFetch of every citation with failure taxonomy,
provenance ledger with claim->verbatim quote->verdict->live URL, synthesis with GFM
footnotes not inline URLs, four tier tags [VERIFIABLE]/[HEURISTIC]/[INFERENCE]/
[NO SOURCE]) and the canonical template structure exactly (frontmatter -> TOC ->
Context -> Temporal Scope -> Executive Summary (3-5 numbered findings) -> numbered
topic sections -> Comparison with # column -> Recommendation -> Open Questions ->
Sources as GFM footnotes -> Appendix: Research Prompt -> Appendix: Provenance Ledger
-> Run History).

Use WebSearch and WebFetch ONLY. Never call Gemini, aido, or any metered API - this
is a zero-cost subscription-only research task, that constraint is hard and
non-negotiable.

TOPIC: Beads (`bd`) issue tracker (github.com/steveyegge/beads) - best practices and
real-world usage patterns for a solo developer's ~18-repo Claude Code AI-agent fleet.

OUTPUT FILE: Write the finished doc directly to
`/home/example-user/projects/example-user/.worktrees/ISSUE-REF/docs/research/beads-agent-fleet-usage.md`.
Frontmatter: status: complete, source: "claude-adhoc-2026-07-06", template_version:
"research-1.2.0".

ROLE IDENTITY (adopt before searching): You are a principal platform engineer who has
shipped multi-repo developer-tooling and agent orchestration at scale. You have
evidence-backed opinions; when you cannot find a source, you say so explicitly rather
than backfilling from memory.

GROUNDING ANCHOR (paste verbatim into your own reasoning before searching, and include
a version of it in the Appendix: Research Prompt block):
"Temporal scope: weight sources by recency (2025-2026 primary window; Beads is new).
If a subtopic has genuinely sparse coverage, write 'no significant recent developments
found' rather than backfilling. Run a Chain-of-Verification before finalizing: isolate
core facts -> draft -> hostile cross-examination (flag any claim cited because the
prompt implied it) -> strip anything unverifiable. Classify every major claim
rationale-first then tag. Hard constraint overriding all formatting: never invent a
citation. Accuracy > completeness. The prompt ASSUMES rigs/hydration is the answer for
multi-repo - treat that as a hypothesis to verify or refute, not a given."

RESEARCH FOCUS (5 areas - cover all of them with dedicated topic sections):

1. Multi-repo topology best practices - how experienced users structure Beads across
   MANY separate repos. Three candidate patterns to investigate: (a) per-repo
   `.beads/` in each repo, (b) a single central planning repo, (c) the native
   "rigs + multi-repo hydration" mechanism. Verify and expand these config semantics
   found via DeepWiki (confirm they're real, don't just repeat them): `external_projects`
   (map project names->paths for cross-project dependencies), `routing.mode` /
   `routing.default` (default `.`) / `routing.maintainer` (default `.`) /
   `routing.contributor` (default `~/.beads-planning`), `directory.labels`,
   `federation.remote`, `sync.remote`, `federation.sovereignty` (T1-T4). Config files:
   `.beads/config.yaml`, `.beads/config.local.yaml`, `~/.config/bd/config.yaml`,
   `.beads/metadata.json`. THE LOAD-BEARING QUESTION: what do real users actually do
   to get a UNIFIED cross-project view across many repos - rigs+hydration, a central
   repo, or per-repo + glue scripts? Find concrete evidence, not speculation. Treat
   the "rigs/hydration is the answer" framing as a hypothesis to verify or refute.

2. Day-to-day agent workflows - how Steve Yegge (his own blog/Substack/X posts),
   Better Stack (betterstack.com/community/guides/ai/beads-issue-tracker-ai-agents),
   ianbull.com/posts/beads, wal.sh/research/beads, DeepWiki
   (deepwiki.com/steveyegge/beads), Ralph TUI
   (ralph-tui.com/docs/plugins/trackers/beads), and HN/Reddit threads describe using
   Beads with coding agents. Session-start patterns, `bd ready`, `bd update --claim`,
   `bd prime`, `bd remember`, `bd setup claude` (Claude Code hook install).

3. Pitfalls, gotchas, anti-patterns - Dolt embedded-store git-merge behavior across
   parallel git worktrees; hash-ID collision claims; JSONL export/import
   (`bd export --all --scrub`, tombstones, `bd import` upsert semantics); version
   churn/breaking changes; project maturity/bus-factor risk (Yegge-led, built late
   2025 - very new project).

4. Workarounds for missing features - recurring tasks (Beads has none natively), due
   dates, labels, and the coarse status model (open/in_progress/closed only) vs richer
   workflows. What do real users do as workarounds?

5. Community UI landscape - beads-web, bd-board, BeadBoard: which of these are
   actually maintained/used as of mid-2026 (check GitHub activity, star counts,
   last-commit dates, not just existence).

QUALITY BAR (do not skip any of these):
- Live-fetch every citation you plan to use. Classify failures (404/410 dead+try
  Wayback, 401/403 paywalled/WAF - try harder don't just downgrade, timeout/5xx
  transient retry once, DNS-fail/malformed = likely fabricated, downgrade hard).
- Build the provenance ledger table (claim | source URL | verbatim quote from the
  fetched page | verdict SUPPORTED/PLAUSIBLE/UNVERIFIABLE/CONTRADICTED | live y/n) for
  every [VERIFIABLE]-tagged claim. The quote must literally appear in text you fetched
  this session.
- Run the 5-perspective critic pass (conventional, contrarian, historical, adjacent,
  skeptic) on your own draft before finalizing, and resolve or explicitly concede
  high/medium findings.
- If a subtopic (e.g. UI landscape, or federation.sovereignty semantics) turns out to
  be sparse or unverifiable after real search effort, write "no significant recent
  developments found" / tag [NO SOURCE] - do NOT invent detail to fill space.
- Sources section: GFM footnote definitions (`[^1]: Author/Org. (Year). "Title." URL.
  Verified accessible (HTTP <code>) <date>.`), not inline URLs.
- Comparison section needs a numbered `#` column (project convention).
- Appendix: Research Prompt section must include: Registry/method = "Claude ad-hoc web
  (research-doc skill)", Model = your model id, Date, and the full prompt text (this
  task) in a fenced text block.
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

> **Note on §6 (2026-07-18):** §6's claims were sourced via Codex flagship (`cx research`) live web search + fetch, each stamped "Verified accessible (HTTP 200)" at the point of citation — but that run did not extract verbatim quotes into this ledger's claim→quote→verdict format the way the 2026-07-06 Claude ad-hoc run below did. Per the research-doc skill's own hard constraint (never invent a citation, and by extension never invent a quote), no fabricated quotes were backfilled for §6's 28 sources. Treat §6's citations as URL-verified-live but not quote-verified against this ledger's stricter bar; spot-check before treating any single §6 claim as load-bearing for an irreversible decision.

| Claim | Source URL | Verbatim quote (fetched this session) | Verdict | Live? |
|---|---|---|---|---|
| `steveyegge/beads` now redirects to `gastownhall/beads` | https://github.com/steveyegge/beads | `location: https://github.com/gastownhall/beads` (HTTP 301 response header, via `curl -sI`) | SUPPORTED | Y |
| `external_projects` maps project names to paths for cross-workspace dependency resolution | https://github.com/steveyegge/beads/blob/main/docs/CONFIG.md | "`external_projects` — Map project names to relative/absolute paths for cross-project dependency resolution" | SUPPORTED | Y |
| `directory.labels` maps directories to labels for monorepo filtering | https://github.com/steveyegge/beads/blob/main/docs/CONFIG.md | "`directory.labels` — Map directories to labels for monorepo filtering" | SUPPORTED | Y |
| `federation.sovereignty` has tiers T1 (full) through T4 (unrestricted) per CONFIG.md | https://github.com/steveyegge/beads/blob/main/docs/CONFIG.md | "`federation.sovereignty` — Tier designation: `T1` (full), `T2` (regional), `T3` (provider), `T4` (unrestricted)" | SUPPORTED (for this doc's wording only — contradicted by FEDERATION-SETUP.md, see next row) | Y |
| `federation.sovereignty` tiers actually mean T1=no restrictions(public), T4=anonymous(max privacy) per FEDERATION-SETUP.md | https://github.com/steveyegge/beads/blob/main/FEDERATION-SETUP.md | "T1: 'No restrictions' for public data... T4: 'Anonymous' for maximum privacy" | CONTRADICTED (contradicts the CONFIG.md row above; both are genuine fetched quotes — flagging the doc-drift itself, not endorsing either ordering) | Y |
| `routing.contributor` default is `~/.beads-planning` | https://github.com/steveyegge/beads/blob/main/docs/ROUTING.md | "**routing.contributor**: Assigns the repository for contributors, usually `\"~/.beads-planning\"`" | SUPPORTED | Y |
| `routing.maintainer` default is `.` | https://github.com/steveyegge/beads/blob/main/docs/ROUTING.md | "**routing.maintainer**: Designates the repository for maintainers in auto mode, typically `\".\"`" | SUPPORTED | Y |
| Federation peer verbs (`list-peers`, `push`, `pull`, `sync`) are documented but not yet available | https://github.com/steveyegge/beads/blob/main/FEDERATION-SETUP.md | "Several planned operations lack exposed commands: `list-peers`, `push <peer>`, `pull <peer>`, and `sync <peer>` have infrastructure support but remain unavailable in current releases" | SUPPORTED | Y |
| Issue #2466: Dolt metadata-table merge conflict on every pull in multi-machine setups, unresolved | https://github.com/steveyegge/beads/issues/2466 | "every pull after a push from the other machine fails with a Dolt merge conflict... This is not a conflict on actual issue data — it's purely metadata tracking" / "Not resolved... opened on March 9, 2026, and remains in an open state" | SUPPORTED | Y |
| Hash-ID collisions were possible pre-v0.20.1, fixed from that version | https://github.com/steveyegge/beads/blob/main/docs/TROUBLESHOOTING.md | "Pre-v0.20.1: ID collisions could occur during concurrent changes" / "Post-v0.20.1: 'Different issues get different hash IDs' preventing collisions entirely" | SUPPORTED | Y |
| `max_collision_prob` config key exists with default 0.25, implying collisions are managed probabilistically | https://github.com/steveyegge/beads/blob/main/docs/CONFIG.md | "`max_collision_prob` — Max collision tolerance (default: 0.25)" | SUPPORTED | Y |
| `bd export --all --scrub` excludes test/pollution records | https://raw.githubusercontent.com/steveyegge/beads/main/docs/CLI_REFERENCE.md | "`--scrub` Exclude test/pollution records" | SUPPORTED | Y |
| Built-in statuses are open/in_progress/blocked/deferred/closed (richer than assumed) | https://raw.githubusercontent.com/steveyegge/beads/main/docs/CLI_REFERENCE.md | "open, in_progress, blocked, deferred, closed" | SUPPORTED | Y |
| Due dates are natively supported with flexible parsing | https://raw.githubusercontent.com/steveyegge/beads/main/docs/CLI_REFERENCE.md | "Due date/time. Formats: +6h, +1d, +2w, tomorrow, next monday, 2025-01-15" | SUPPORTED | Y |
| Labels are natively supported (`bd label add`, `bd create --labels`) | https://raw.githubusercontent.com/steveyegge/beads/main/docs/CLI_REFERENCE.md | "Add a label to one or more issues" | SUPPORTED | Y |
| `bd prime` adapts output by context: ~50 tokens MCP mode, ~1-2k tokens CLI mode, `--hook-json` for SessionStart | https://deepwiki.com/steveyegge/beads/8.2-agent-workflow-guidelines | "MCP mode: Brief reminders (~50 tokens) when an MCP server is active" / "CLI mode: Full command reference (~1-2k tokens)" / "Hook integration: Supports `--hook-json` for Claude Code and similar platforms' `SessionStart` envelope" | SUPPORTED | Y |
| `bd remember` replaces MEMORY.md files | https://deepwiki.com/steveyegge/beads/8.2-agent-workflow-guidelines | "`bd remember`: Stores project insights that `bd prime` injects dynamically into future sessions, eliminating redundant `MEMORY.md` files that fragment context" | SUPPORTED | Y |
| "Landing the plane" protocol requires a mandatory push before work is considered complete | https://deepwiki.com/steveyegge/beads/8.2-agent-workflow-guidelines | "**Mandatory push** (`git push` or `bd dolt push`)" / "Work remains incomplete until pushed to remote repository" | SUPPORTED | Y |
| Yegge recommends killing an agent after one task rather than looping it | https://steve-yegge.medium.com/beads-best-practices-2db636b9760c | "Agents should tackle one task at a time. Once it's done, kill the process and start a new agent" | SUPPORTED | Y |
| A user abandoned Beads for a simpler tool citing performance regressions release-over-release | https://news.ycombinator.com/item?id=46487580 | "every release made it slower and more frustrating to use. I started battling it several times a week as its background daemon took to syncing the wrong things" | SUPPORTED | Y |
| A commenter called the dev's approach "erratic" and "unprofessional" | https://news.ycombinator.com/item?id=46487580 | "the author's approach to software development quite erratic and honestly a bit unprofessional. Yes, LLMs are great, but no they shouldn't be the lead developer" | SUPPORTED | Y |
| An independent user runs beads across 20+ repos using plain per-repo `.beads/issues.jsonl`, no hydration/central repo | https://wal.sh/research/beads | "Issues are stored in `.beads/issues.jsonl`" / "Issues travel with the repository" / describes "Claude Code workflow across 20 repos with 857+ issues" | SUPPORTED | Y |
| FAQ's own answer to multi-project tracking is "initialize bd in a parent directory," not routing/hydration | https://github.com/steveyegge/beads/blob/main/docs/FAQ.md | "Multi-project tracking: Cross-project issue references are unsupported; workaround is initializing bd in a parent directory" | SUPPORTED | Y |
| FAQ recommends against mission-critical production use without a tested backup/restore plan | https://github.com/steveyegge/beads/blob/main/docs/FAQ.md | "development/internal projects" rather than "mission-critical production systems without a tested backup/restore plan" | SUPPORTED | Y |
| Beads was "built in 6 days with Claude" | https://steve-yegge.medium.com/introducing-beads-a-coding-agent-memory-system-637d7d92514a | git-backed issue tracker "built by Yegge in 6 days with Claude" (search-engine synthesis of the fetched page, cross-referenced against the same claim recurring verbatim-in-substance across two independent WebSearch summaries of this and the GitHub README) | PLAUSIBLE (summary-of-fetch rather than a directly quoted sentence from the WebFetch call itself) | Y |
| `beads_viewer`, `beads_rust`, `beads-ui` are actively maintained (pushed within the last week of this session); `mgalpert/beads-viewer` is stale | GitHub REST API, `api.github.com/repos/<owner>/<repo>` | `pushed_at: 2026-06-29` (beads_viewer), `2026-07-04` (beads_rust), `2026-07-03` (beads-ui), `2025-10-16` (mgalpert/beads-viewer) — raw JSON field values returned by the live API call | SUPPORTED | Y |
| Beads' own internal worktree feature (for sync-branch, now defunct) can cause git worktree errors | https://github.com/steveyegge/beads/blob/main/docs/TROUBLESHOOTING.md | "Cause: Beads created internal worktrees for defunct sync-branch feature" | SUPPORTED | Y |

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

- 2026-07-06 — Claude ad-hoc web research (research-doc skill), model `claude-sonnet-5`. Method: WebSearch fan-out across mainstream/official-docs, contrarian (HN "Show HN: I replaced Beads"), adjacent (DoltHub vendor blog, Ralph TUI third-party integration), and recency-first (GitHub API live star/push-date queries) framings, followed by targeted WebFetch of ~25 pages/APIs, a self-run 5-perspective critic pass (see below), and citation live-validation (24/24 attempted citations resolved live; 1 additional attempt — a second Hacker News thread on multi-repo/Dolt-merge criticism, `item?id=46669791` — failed twice with HTTP 429 rate-limiting and was dropped rather than cited from memory). No Gemini/aido/metered API calls were made, per hard constraint.
- **5-perspective critic pass, applied before finalizing:**
  - *Conventional*: draft aligned with mainstream Beads documentation on the core `bd ready`/claim/close loop and the existence of routing/hydration — no contradiction found, low severity.
  - *Contrarian*: the HN "Show HN: Ticket" thread and the wal.sh case study were the two strongest counter-signals to a naive "just use hydration" recommendation; both are incorporated into §1 and §3 rather than left out (medium severity — resolved by incorporation).
  - *Historical*: not applicable in the usual sense (no pre-2025 prior art exists), but the "FAQ vs. newer multi-repo docs" contradiction is effectively an internal-historical drift issue, flagged explicitly in §1 and §3 (medium severity — resolved by flagging rather than silently picking a side).
  - *Adjacent*: the DoltHub vendor blog and Ralph TUI third-party-integration docs were pulled in specifically to see whether an outside implementer's mental model matched the primary docs; both corroborated the "per-repo/per-clone is a legitimate first-class pattern, not just a fallback" reading (low severity — no changes needed, strengthened §1).
  - *Skeptic*: every VERIFIABLE-tagged claim in this doc has a corresponding provenance-ledger row with a literal quote from a live-fetched page this session; the one claim that could not clear this bar with a direct quote (the "6 days with Claude" origin story) was downgraded from a bare quote to `PLAUSIBLE` with its reasoning stated explicitly rather than silently upgraded (medium severity — resolved by downgrade).
  - **Convergence judgment:** committed after one round — no unresolved high-severity findings remained, and each medium finding above was either incorporated into the body or explicitly flagged/downgraded rather than hidden.
- 2026-07-18 — Codex flagship (`cx research --effort high`, model tier "Sol"), delegated from Claude (ISSUE-REF (host ambiguous) session) per the fleet's Codex-first web-research routing rule. Added §6 (CC TUI Task Panel — Beads Integration, Customization & Alternatives), closing the one gap the 2026-07-06 run didn't cover: whether/how the native Claude Code task panel integrates with Beads, its own customization surface, and the wider multi-session-visibility tool landscape. Method: live GitHub/docs/changelog search and fetch across ~28 sources (Anthropic official docs, `anthropics/claude-code` issue tracker, `gastownhall/beads` docs, and ~14 community project repos), CoVe self-check, 4-tier claim tagging. Corrected one claim from the 2026-07-06 run: `beads_viewer` (previously tabled as "actively maintained") is now flagged stale/Dolt-incompatible per the project's own `COMMUNITY_TOOLS.md` and issue #121 — see the correction note in §5. Key new findings: Anthropic shipped native **Agent View** (multi-session pin/reorder, distinct from the task panel) since the prior check; issues #50656 and #20664 (previously open, cited by the `/load-task-panel` skill) are now closed "not planned"; no Beads↔native-task bridge exists anywhere in the ecosystem as of this date — §6.3 proposes a concrete one-way-projection design as the gap-closing recommendation for `ISSUE-REF`/`ISSUE-REF`/CC #54. Folded into this doc, footnotes renumbered [^24]–[^51] continuing the existing sequence, by Claude (ISSUE-REF (host ambiguous)) immediately after the Codex run returned — full prompt at `docs/research/beads-agent-fleet-usage.md` history (scratchpad copy not retained past this session). §6's provenance-ledger gap (URL-verified but not quote-extracted) is flagged explicitly above rather than backfilled.

<!-- /doc:region name="run_history" -->
