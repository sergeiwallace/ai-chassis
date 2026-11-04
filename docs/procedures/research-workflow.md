---
title: "Research Workflow — Prompt Creation, Execution, and Documentation"
category: procedures
tags: [research, workflow, prompts, documentation, registry]
status: active
source: example-user
---

# Research Workflow

Standard procedure for all research runs across the ai-core ecosystem — prompt creation, execution, documentation, synthesis, and follow-up. Covers aido research runs and Opus researcher agent sessions.

Project-specific procedure docs may provide project-scoped run commands and paths; the authoritative workflow rules live here.

## Which Tool to Use

All research goes through `aido research`. Choose depth by scope:

```text
How broad and deep does this need to be?
├── Quick fact-check, single lookup, fast comparison → aido research -d quick
├── Moderate topic, doesn't need exhaustive coverage → aido research -d normal
├── Most research tasks (DEFAULT) → aido research -d thorough
└── Critical/foundational, 10+ sources, 60+ min → aido research -d deep (HARD GATE)
```

For in-session lookups that don't need a persistent doc, use a CC Agent (Opus) instead of aido.

**Rules of thumb:**

| Scenario | Tool | Example |
| --- | --- | --- |
| Quick fact-check or comparison | `aido research -d quick` | "What's the latest SWE-bench score for Claude?" |
| Moderate topic, multi-source | `aido research -d normal` | "Compare SQLite FTS5 vs pgvector for friction retrieval" |
| Code review | `/review` (Codex by default) | Cross-review of implementation diff |
| Standard research, 3-10 sources | `aido research -d thorough` | "State of the art in agent retry loops" |
| Critical/foundational, 10+ sources | `aido research -d deep` | "Comprehensive SDLC pipeline hardening" |

## 1. Prompt Creation & Review

### Writing the Prompt

- **One doc per research run.** Every research execution produces its own standalone document. Never append results to an existing research doc.
- **Grounding anchor required.** All research prompts must include the full `<grounding_instructions>` CoVe block. Source: `<ai-harness-root>/docs/research/prompts/TEMPLATE.md`.
  - Role identity narrative specific to the research topic
  - 4-step CoVe inner process (isolate -> draft -> hostile cross-exam -> strip)
  - Claim classification tags: `[VERIFIABLE FACT]`, `[INDUSTRY HEURISTIC]`, `[SYNTHESIZED INFERENCE]`, `[NO SOURCE FOUND]`
  - Hard constraint on citation accuracy
- **Depth selection.** Default to `thorough`. Use `quick` for fast lookups. Use `deep` only with explicit user approval.
- **New prompts go at the top** of the Pending/Ready section and status overview table in the registry, not the bottom.

### Registering the Prompt

**Registration is automatic** — `aido research` reads the prompt from the stub doc's `## Appendix: Research Prompt` code block and registers it to the ai-core DB automatically. No separate `aido draft research` step is needed.

Workflow:
1. **CC creates the stub doc** — frontmatter (`status: stub`) + prompt draft in `## Appendix: Research Prompt` section
2. **User reviews prompt** — in chat (or by reading the stub doc path); approves, requests changes, or rejects
3. **Ship the stub doc** — commit and push immediately to prevent data loss
4. **Update related docs and tasks** to reflect that research is about to run
5. **Run `aido research --report <stub-path>`** — registration + execution happen in one step

**Note:** R-N IDs are no longer assigned manually. The ai-core DB auto-assigns a run ID on execution. Reference the stub doc path, not an R-N ID.

The markdown registry at `docs/research/prompts/archive/research-prompt-registry.md` (and per-project equivalents) is a **read-only archive** of pre-ISSUE-REF prompts. Do not add new entries there.

### User Review

- CC drafts the prompt and presents the stub doc path for review before running
- User approves, requests changes, or rejects
- Only fire `aido research --report <stub-path>` after explicit approval

## 2. Execution

### Running the Research

- Launch via `aido research` (background) or CC Agent (Opus) for in-session lookups that don't need a persistent doc
- **Output directly to file** — always instruct research agents to write output directly to the target doc file. This prevents data loss if CC crashes or compacts mid-research. Use append mode in case the target path already exists.
- Research doc output goes to `docs/research/<descriptive-name>.md`
- Include proper frontmatter: title, category, tags, status, source, related_docs
- Include `## Appendix: Research Prompt` section at the end with: registry ID, model, date, reference to prompt in registry

### aido research Runs

**Always run `aido research` as a detached background process.** Research runs take 30–60+ minutes (deep-research Interactions API is the primary researcher for all non-quick depths). A foreground run dies with the CC session. Standard invocation:

```bash
LOGFILE=<aido-root>/runs/$(date +%Y%m%d-%H%M%S)-<topic>.log
PROMPT="<full prompt text>"
nohup bash -c 'cd <aido-root> && aido research "$1" -d thorough --report <output-path>' -- "$PROMPT" > "$LOGFILE" 2>&1 &
echo "PID: $! | Log: $LOGFILE"
```

Monitor with `aido status` or `tail -f <logfile>`. Check `aido status` at session start for any stale or running runs.

**Cost-gate gotcha (enterprise-api hosts — portable-workspace / enterprise-cld-windows / enterprise-windows).** `thorough`/`deep`
depth on a enterprise-api profile triggers an interactive billable-confirm gate
(`machine_profile._prompt_gate`) — billable because these hosts call the Claude API directly. A
detached / CC-launched run has **no TTY**, so the prompt hangs (or aborts on EOF). Set
**`AIDO_ASSUME_YES=1`** in the launch env to clear it non-interactively — opt-in only (aido never
sets it itself, so it cannot silently auto-bill; the auto-confirmation is logged loudly).
`--approved` does **NOT** do this (it only skips loop-1 prompt crafting). **personal-cli hosts
(mac / linux) never hit this gate** (Claude Max, zero marginal cost), so
the env var is a no-op there. Detached enterprise-api invocation:

```bash
AIDO_ASSUME_YES=1 nohup bash -ic 'cd <aido-root> && aido research "$1" -d thorough --raw --report <output-path>' -- "$PROMPT" > "$LOGFILE" 2>&1 &
```

**Launch-hook gotcha (CC sessions).** The aido pre-tool-use hook blocks any Bash command containing the literal string `aido research` unless the command also contains `<grounding_instructions>`. The real run passes because the prompt carries the anchor — but a *monitoring* / status command that merely mentions the phrase in an `echo`/`grep`/`find`/`pgrep` label gets blocked. Never put the literal `aido research` in labels; match the process with `pgrep -fc 'local/bin/aido'` instead.

**Env-contamination gotcha (CC sessions).** A run launched from inside a CC session inherits the session's env, including `ANTHROPIC_DEFAULT_OPUS_MODEL` set to the CC model *selector* (e.g. `claude-opus-4-8[1m]` — a context-variant suffix that is not a real API model id). aido must normalize these (see the ISSUE-REF reference incident below); if a run dies at the first model-calling node with a "not in MODEL_BACKEND_REGISTRY" / unknown-model error, suspect env-leaked model ids and fix the normalization in aido rather than hand-editing env vars.

#### Active monitoring procedure (mandatory for all backgrounded aido runs)

After launching, set up active monitoring before walking away. Without it, silent-degradation bugs (no-retrieval, broken refine_analysis, env-contaminated model ids, etc.) can burn 30+ min producing useless output.

**Scope — this applies to ANY backgrounded aido graph run, not just research.** The launch → monitor → pause-on-first-issue → fix-the-tool-properly → resume discipline below is written with research/`--refine` examples but generalizes verbatim to `aido go`, `aido draft`, and any other long-running aido graph. A failing aido run is NEVER retried blindly — every failure triggers the full root-cause → fix → relaunch loop (the aido-First rule: fix the tool before relying on it). "Rinse-repeat" this loop on every failure.

1. **Schedule a session-only monitoring cron immediately after launch.** Use `CronCreate` with `recurring: true`, `durable: false`, and an off-minute interval (e.g., `3-58/5 * * * *` — every 5 min, offset so it doesn't pile on at :00 / :30). The cron prompt should be self-contained and check, at minimum: process alive + elapsed, current node + loop, new errors/warnings since last check, **tool_use call count** (`grep -c "tool_use\|server_tool_use" <log>`) to confirm web retrieval is actually firing, output file activity.
2. **Self-cancel rules in the cron prompt.** On successful completion: cron calls `CronDelete`, reports final word count + tool_use count + key deltas vs prior (for refine) + total elapsed. On fatal failure: cron calls `CronDelete`, does NOT auto-restart, surfaces root cause to the user with no hacks/workarounds.
3. **Pause at earliest detected issue — use graceful pause first.** If the cron finds: zero tool_use calls 5+ min into a researcher round, repeated retries on the same error, `refine_analysis_failed`, FATAL preflight, or any unexpected error — pause the run via `aido pause <thread_id>` (graceful, preserves the LangGraph checkpoint state). Only fall back to `aido pause <thread_id> --force` (SIGTERM) or `kill <PID>` if graceful pause fails or hangs. The thread_id is in the run log's "Thread ID:" line.
4. **No hacks or workarounds when fixing the root cause.** Find the actual bug in aido (or wherever). Fix it properly — **reproduction-first (RED→GREEN)**: write a minimal regression test that reproduces the exact failure, confirm it FAILS on current code, then apply the fix, then confirm it PASSES. Run the full hard gate green (`ruff check` + `ruff format --check` + `pytest`) **before** pushing. aido is a personal repo → push a topic branch, open/update a PR, then self-merge after the gate. File the bug as a roadmap entry + CC task like any other tracked fix.
5. **Resume from the paused checkpoint first — this is the default recovery path.** After fixing the root cause, resume the paused run via `aido resume <thread_id> continue --type research`. This picks up from the LangGraph checkpoint and preserves the EXACT graph state (all completed node outputs, accumulated findings, partial round progress). The `--type` flag must match (use `research` for research/refine runs). For more granular control: `--replay NODE` (re-run a specific node with saved input), `--skip NODE` (skip past a node using saved output), `--restore NODE` (rewind graph to a saved snapshot). Resume preserves everything in-place — this is the right answer in almost all cases.
6. **`aido research --refine` is the FALLBACK, not the default — hard human gate required.** Only use refine when graceful resume genuinely cannot continue (e.g., checkpoint corrupted, aido version bump changed the schema, the prior run auto-completed without a pause checkpoint, the failed run was killed before its pause hook could persist state). When proposing refine to the user: (a) explicitly state why resume is not viable, (b) draft the refine command + steering prompt using `<aido-root>/docs/research/refine-after-no-retrieval-template.md` as the basis, (c) wait for explicit user approval before launching. Refine re-executes the graph against the auto-committed prior report — preserves prior content but loses in-flight per-round graph state and burns more tokens than resume.
7. **Restart from scratch is the absolute last resort.** Both resume and refine should fail before considering restart. If restart is genuinely necessary, surface to the user with an explanation of why both resume and refine could not preserve the work — and wait for approval before launching.
8. **Always invoke the same monitoring procedure when resuming or re-launching after a fix.** Don't skip monitoring just because you "fixed it" — verify the fix worked on the live run, and catch any second-order issues that may surface.

Reference incident: the 2026-05-29 ai-ready-repos run caught two distinct silent-failure bugs in aido (no-retrieval on enterprise-api; refine_analysis 400 on empty system) via active monitoring. Both were root-caused, fixed (aido commits `1857b97` / `c498b4e` / `5a42b95`), regression-tested (`7c7dd8b`). The recovery used `aido research --refine` because the first failure auto-completed without leaving a pause checkpoint to resume from — that's the kind of corner case where refine is the right answer. Future incidents where the pause happens mid-run (via the new monitoring procedure) should resume from the checkpoint first.

Reference incident (env-contamination class): the 2026-06-10 loop-driven-agentic-dev-paradigm run (ISSUE-REF) died at the researcher node on round 1 — every model fallback (opus→sonnet) exhausted with `Model 'claude-opus-4-8[1m]' is not in MODEL_BACKEND_REGISTRY`. Root cause: launched from inside a CC session, the run inherited `ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-8[1m]` (Claude Code's 1M-context model *selector*); `resolve_claude_model` returned that literal, which is neither a registry key nor a real API id. Fix (ISSUE-REF — shipped as ISSUE-REF in commit `894cf0e`, since renumbered): `model_registry.canonical_anthropic_id` — the single normalization chokepoint feeding both `select_backend` routing and the API call — now strips a trailing bracketed context-variant suffix, mirroring its existing Vertex `@`-version handling. Reproduction-first tests (`TestContextVariantSuffixNormalization` + a `resolve_claude_model` env-override repro); hard gate green (3314 passed); run relaunched clean and self-monitored. The run had died before synthesis/commit, so a clean restart (not resume/refine) was correct — there was no checkpoint or prior report worth preserving.

Depth selection:

| Depth | Duration | Sources | Use When |
| --- | --- | --- | --- |
| `quick` | ~2 min | 1-2 | Simple topic — uses MCP graph, no deep-research |
| `normal` | ~30 min | 3-5 | Moderate topic |
| `thorough` | ~45 min | 5-10 | **DEFAULT.** Most research topics. Always start here. |
| `deep` | ~60+ min | 10+ | Critical/foundational. **Requires explicit user approval.** |

Duration reflects deep-research Interactions API (30–60 min polling). Always use `thorough` as the default depth. Stack `--refine` runs to deepen existing research rather than jumping to `deep`. Never run `deep` without explicit approval from the user.

Project-specific log paths and `AIDO_PROJECT_ROOT` overrides live in each project's `docs/procedures/research-workflow.md`.

**When to save results.** Not every research session warrants a persistent doc:

- **Save to `docs/research/`**: Results are complex, multi-faceted, and will inform a design doc or implementation decision.
- **Ask the user**: Borderline cases — useful findings but unclear if they'll be referenced again.
- **Don't save**: Trivial fact-checks, simple comparisons, one-off questions with short answers.

### Pre-Submission Checklist

Before submitting any research prompt:

- [ ] **Zero-Slop Anchor appended** — use the full XML `<grounding_instructions>` block. Source: `<ai-harness-root>/docs/research/prompts/TEMPLATE.md`
- [ ] **Stub doc shipped** — committed and pushed; registration happens automatically when `aido research --report <stub-path>` runs
- [ ] **Depth appropriate** — thorough by default, deep only with user approval
- [ ] **Context block present** — the prompt includes enough project context for the model to give specific (not generic) recommendations
- [ ] **Questions are specific** — numbered, concrete questions rather than vague "tell me about X"

### Inline Responses (research returned in-context)

When research output is returned inline (not written to file by the agent):

1. **Write raw output to the target file immediately** — before any editing
2. **Edit the file in place** — add frontmatter, template compliance, cleanup
3. **Save and commit the finalized doc** — push to all 3 branches right away
4. **Then** do related work — registry updates, task linking, synthesis docs

This ensures the research data is on disk before any subsequent processing that could be interrupted by compaction or crash.

### Ad hoc research

Research done in conversation, outside a formal run, is documented too. Fold it into a related
existing `docs/research/*.md`, else create `docs/research/<topic>.md` (`status: draft`) from the
canonical `docs/research/TEMPLATE.md` (`resolve_canonical()`), never a diverged local copy. Never answer
inline-only and lose findings at compaction. The always-loaded context keeps the last sentence and
points here for the rest (ISSUE-REF).

### Multi-Round Research

When a broad topic needs depth:

- **Round 1:** Broad survey (R-N)
- **Round 2:** Gap analysis on weak areas identified in Round 1 (R-N+1)
- **Round 3 (optional):** Competitive deep-dive, specific product analysis, or model cross-validation
- Note the multi-round strategy in the initial prompt's registry entry

## 3. Post-Run Documentation

### Citation Validation (Required Until D11)

After the research doc is written to disk, validate citations before presenting findings:

```bash
python3 <aido-root>/scripts/verify-research-citations.py --path <path/to/doc.md>
```

Triage results and include a validation summary line in the user communication (Section 6):

| Result | Action |
| --- | --- |
| All PASS | Note in summary ("Citation check: all PASS"), proceed normally |
| WARN (body miss) | Note count in summary; most are false positives from NLI body-text retrieval, not fabrications |
| WARN (NLI CONTRADICTION) | **Flag to user** — claim may misrepresent the paper; require manual review before integrating |
| FAIL | **Fix immediately** — find the correct paper, replace the wrong citation in the research doc, update any affected claim text. Then flag the fix to the user in a "⚠️ Citation Issues" subsection. Do not leave FAILs unresolved. |

For any FAIL or CONTRADICTION, add a "⚠️ Citation Issues" block in the user summary with: the specific claim text, the arXiv ID matched, the discrepancy, and what was done to fix it.

### Fixing FAILs

For each FAIL:

1. **Identify the wrong citation** — note the arXiv ID and the claim it was attached to.
2. **Find the correct paper** — search arXiv, Semantic Scholar, or CrossRef for a paper that actually supports the claim. Fetch the HTML and verify the claim is present.
3. **Replace in the research doc** — update the inline citation and the Sources/References section. If no correct paper exists, rewrite the claim as `[SYNTHESIZED INFERENCE]` or remove it.
4. **Re-run citation validation** — confirm the FAIL is resolved before proceeding.

### Follow-up Research After Citation Fixes

After fixing all FAILs, evaluate whether follow-up research is warranted:

- **A follow-up prompt is warranted if:** a fixed or removed citation reveals a genuine gap — the underlying claim was load-bearing for a design decision and now lacks sourcing, or the correct paper contradicts the original finding and changes the design recommendation.
- **Propose the follow-up** in the user summary: draft the prompt, identify the correct registry ID slot, and give a one-sentence rationale ("R-24 claim on X was fabricated; correct source says Y — follow-up needed to verify whether Z still holds").
- **Do not launch** the follow-up without user approval (same gate as any research run).
- If no follow-up is warranted, note "No follow-up research warranted from citation fixes — [one-line reason]" in the chat summary.

**Post-receipt checks before integrating into design docs:**

- [ ] **Citation validation run** — `python3 <aido-root>/scripts/verify-research-citations.py --path <doc.md>`. Include validation summary in user communication. Flag FAILs and NLI CONTRADICTION hits explicitly before integrating any claims.
- [ ] **Source paper verification** — for every constant, benchmark, or specific number: fetch the cited arXiv/publisher URL and confirm the value matches. Never let a model verify its own output.
- [ ] **Citation spot-check** — verify 3–5 citations per doc by fetching actual URLs and confirming author, title, and year
- [ ] **Constant tagging** — every constant in the design doc tagged as `(verified: paper)` or `(heuristic: needs empirical validation)`
- [ ] **Verification layer** — a model from a different family than the drafter performs the cross-check. This is formal policy (2026-03-25).

**This step is temporary** — superseded when D11 (programmatic citation validation in aido research graph) is implemented.

### After Research Completes

1. **Run citation validation** (see above) before updating any downstream docs
2. **Update the research doc** if needed (verify frontmatter, check for truncation)
3. **Update related docs and tasks** (e.g., design doc "Needs Creation First" entry, roadmap task status)

### Linking Related Research Docs

When a research run is related to another (follow-up, gap analysis, model comparison, etc.):

1. **Cross-link in frontmatter:** Both docs include each other in `related_docs`
2. **Create a synthesis doc** (see Section 7 below)
3. **Link all docs in the registry** completed table — individual runs + synthesis
4. **Link to the task:** The task's roadmap entry and any design doc should reference the synthesis doc as the primary source

### Supersession Rules

- **Individual research docs** that have been synthesized get `status: superseded` and `superseded_by: <path>` in their frontmatter
- **The synthesis doc** is the canonical/final reference. Design docs and implementation should cite the synthesis, not the individual runs.
- **Superseded docs are preserved** for historical reference — never delete them. They contain the raw evidence trail.

### Ship All Changes

After all documentation updates:

- **Commit** all changed files (research doc, registry, related docs, roadmap)
- **Push to origin/main** from worktree
- **Sync local main**, fast-forward only (`git -C <project-root> fetch origin main && git -C <project-root> merge --ff-only origin/main`) — never `--rebase` a shared tree (ISSUE-REF)

## 4. Gap Analysis Evaluation

After every completed research run, before presenting findings to the user:

1. **Evaluate the output for gap indicators:** clusters of `[NO SOURCE FOUND]` or high-uncertainty `[SYNTHESIZED INFERENCE]` claims, weak coverage of key topics, core questions left unanswered, or conflicting findings across sources that need resolution
2. **A gap analysis prompt is warranted if any of these are true:**
   - Core architecture or design decisions depend on `[NO SOURCE FOUND]` or `[SYNTHESIZED INFERENCE]` claims
   - A major topic area received only shallow coverage due to insufficient sources
   - The run surfaced an important sub-topic that deserves a focused follow-up
   - Conflicting findings need resolution from a second, targeted run
3. **If warranted:**
   - Draft the gap analysis prompt targeting the specific weak areas identified
   - Register it at the top of `## Pending / Ready` in the registry (and top of status table)
   - **Ship the registry update to all three branches** before presenting findings to user
   - Note the gap analysis entry in the user communication summary with a brief rationale
4. **If not warranted:** note "No gap analysis warranted — [one-line reason]" in the chat summary

---

## 5. Registry Archive

**Post-ISSUE-REF:** The ai-core DB (via `aido draft research`) is the authoritative store for all new research prompts. The markdown registry at `docs/research/prompts/archive/research-prompt-registry.md` (and per-project equivalents) is a **read-only archive** of pre-ISSUE-REF prompts. Do not add new entries there.

When reading the archive for historical reference, be aware that rebase/merge operations may have introduced structural corruption. If you encounter issues:

1. **Duplicate section headers** — merge into the canonical section; preserve all entries.
2. **Git conflict markers** — `<<<<<<<`, `=======`, `>>>>>>>`. Resolve using context; flag ambiguity to user.

The archive uses `DT-N`, `R-N`, and `G-N` ID prefixes. New runs use ai-core DB run IDs assigned automatically by `aido draft research`.

---

## 6. User Communication

### After Research + Documentation Complete

Present to the user in chat:

1. **Summary of findings** — key discoveries, effect sizes, confidence levels
2. **Recommended follow-up research** — any additional runs needed, with draft prompts and recommended models
3. **Design doc status** — if a design doc was written, summarize: number of design decisions, open questions, estimated review time
4. **Decisions and open questions** — present the most important ones that need user input
5. **Next steps** — what to review first, what's ready for implementation

### Design Doc Creation

After research is complete (including synthesis if multi-round):

- Draft the design doc grounded in research findings
- Follow the project's design doc template and conventions
- All design decisions start as "Proposed" — reviewed via chat-first process
- Add the design doc to `doc-review-priorities.md` at the appropriate priority level
- Ship to all three branches before presenting for review

## 7. Synthesis Docs

Research is not an end product. The flow is:

```text
Research (aido research)
  → Research doc (docs/research/{topic-slug}.md)
    → Design doc (docs/designs/{feature-slug}.md)
      → User reviews design doc
```

**Key rule: don't wait for the user to review raw research before drafting the design doc.** Research docs are reference material and appendices. The design doc synthesizes findings into actionable decisions with options, tradeoffs, and recommendations. The user reviews the design doc — that's the approval gate.

### When to Create a Synthesis Doc

Create a synthesis doc when:

- Multiple research runs cover the same topic (e.g., R-15 broad survey + R-16 gap analysis)
- Different models researched the same topic (e.g., two researchers from different families)
- A follow-up run builds on or refines earlier findings

### Synthesis Doc Format

```yaml
---
title: "<Topic> Research Synthesis — R-N + R-M Combined Findings"
category: research
tags: [<topic tags>, synthesis]
status: complete
source: opus-researcher
supersedes:
  - docs/research/<input-doc-1>.md
  - docs/research/<input-doc-2>.md
related_docs:
  - <other relevant docs>
---
```

- Organize by theme, not by which round produced the finding
- Resolve tensions between input docs
- Produce unified recommendations section
- Include combined deduplicated source list
- Should be self-contained — someone reading ONLY the synthesis has everything they need

### After Synthesis

1. Update input docs: `status: superseded`, add `superseded_by:` field
2. Update registry: add synthesis entry to completed table
3. Update task and design doc references to point to synthesis
4. Ship to all three branches

### Multi-Source Design Doc Drafting

When multiple research sources inform one design doc:

1. Complete all relevant research runs
2. Read all research docs and extract the key findings
3. Draft the design doc, citing research docs as references
4. Link both research docs and design doc to the relevant roadmap task (`link_doc`)
5. Present the design doc for review

## 8. Research Reliability — Zero-Slop Standards

Discovered during the G3P-DT translation audit (2026-03-18). See `<aido-root>/docs/research/g3p-dt-translation-audit-summary.md` for the full audit. These failure modes apply to all reasoning models (Gemini, Claude Opus) used for research.

### The "Zero-Slop" Anchor Text

Every research prompt MUST end with the full XML `<grounding_instructions>` anchor block. The canonical source is `<ai-harness-root>/docs/research/prompts/TEMPLATE.md`. Do NOT use the older 4-line citation anchor — it lacks CoVe, claim classification tags, and the explicit abstention pathway that prevent compliance-driven fabrication.

### Deficiencies and Mitigations

**Deficiency 1: Fabrication of constants and benchmarks.**
Research models mix legitimate sourced findings with fabricated-but-plausible engineering defaults, benchmarks, and even framework names — all presented with identical authority.

Mitigation:

- **Never integrate model-generated research findings into design docs without cross-checking against source papers.** Fetch the arXiv HTML and verify every specific number.
- **Use the Zero-Slop Anchor** to force the model to distinguish heuristics from sourced facts.
- **Annotate design docs:** Every constant in the design doc must be tagged as either `(verified: paper)` or `(heuristic: needs empirical validation)`.

**Deficiency 2: Self-confirmation bias.**
When asked to verify its own claims, Gemini fabricates confirmations — invents fake URLs, fake term definitions in papers, and confirms arbitrary values as "widely documented" when they are not.

Mitigation:

- **Never use a model to verify its own generated claims.** The verification loop must use direct source paper fetching (arXiv HTML, publisher pages), not another search pass by the same model.
- **Verify with a different model family than the one that drafted** — the self-confirmation failure mode is per-model.
- **Whichever model is the primary driver:** verify key claims by fetching source URLs directly (`WebFetch` on arXiv/publisher pages), not by asking that model "is this claim correct?"

**Deficiency 3: Citation fabrication.**
Gemini fabricates author names, cites wrong papers, inflates citation years, and points references to unrelated URLs.

Mitigation:

- **Spot-check at least 3-5 citations per research doc** by fetching the actual URL and confirming it matches the claimed content.
- **For key claims that drive design decisions**, always verify the specific paper, author, and finding — don't trust the citation at face value.

## Anti-Patterns

- **Submitting prompts without Zero-Slop Anchor.** Every research prompt must end with the full `<grounding_instructions>` anchor block. No exceptions.
- **Running `aido draft research` manually before firing.** Registration is automatic — `aido research --report <stub-path>` reads the prompt from the stub's Appendix and registers to the DB in one step. No separate draft step needed.
- **Adding entries to the archive markdown registry.** Post-ISSUE-REF, `docs/research/prompts/archive/research-prompt-registry.md` is read-only. New prompts go to the ai-core DB via `aido draft research`.
- **Running deep/deep_max without approval.** Thorough is the default. Deep requires explicit user approval due to cost and time.
- **Running metered research searches in parallel.** Sequential only. Parallel calls saturate quota and cause cascading timeouts across model families.
- **Saving trivial one-off query results as research docs.** Not every query needs a doc. Only save when findings are complex enough to warrant future reference.
- **Skipping citation validation after research completes.** Every research doc must pass `verify-research-citations.py` before findings are integrated into design docs. FAILs and NLI CONTRADICTIONs must be flagged to user. This step is manual until D11.
- **Flagging FAILs without fixing them.** Flagging is not enough — every FAIL must be resolved: find the correct paper, update the citation and claim text, re-run validation to confirm. Do not present findings or update design docs while FAILs remain open.
- **Skipping the follow-up evaluation after citation fixes.** After fixing FAILs, explicitly evaluate whether the correction warrants a follow-up research prompt. If a load-bearing design claim lost its citation or was contradicted by the correct paper, a follow-up run is required. Silence is not an acceptable substitute for this evaluation.

## Quick Reference Checklist

```text
[ ] Prompt drafted and presented for user review
[ ] User approved prompt
[ ] Stub doc committed and pushed immediately (registration is automatic on run)
[ ] Related docs/tasks updated (pre-run status)
[ ] Research run launched (see §2 aido research Runs above for nohup command)
[ ] Research doc written directly to file (not returned inline)
[ ] Raw version committed and pushed immediately after run completes
[ ] Doc edited in place (frontmatter, template, cleanup)
[ ] Finalized doc shipped to all 3 branches immediately
[ ] Citation validation run: python3 <aido-root>/scripts/verify-research-citations.py --path <doc>
[ ] Validation summary line included in user communication
[ ] FAILs: correct paper found, citation + claim text updated in research doc, re-run confirms resolved
[ ] NLI CONTRADICTIONs flagged explicitly to user for manual review
[ ] Follow-up research evaluated after any FAIL fixes — warranted or not warranted noted in chat summary
[ ] If follow-up warranted: prompt drafted, registered via aido draft research, shipped before user summary
[ ] Related research docs cross-linked (if multi-round)
[ ] Synthesis doc created (if multi-round)
[ ] Input docs marked superseded (if synthesis created)
[ ] Related docs/tasks updated (post-run status)
[ ] Design doc drafted (if applicable)
[ ] doc-review-priorities.md updated (if design doc created)
[ ] All changes shipped to all 3 branches
[ ] Gap analysis evaluated — warranted or not warranted documented in chat summary
[ ] If warranted: gap analysis prompt drafted, registered via aido draft research, shipped before user summary
[ ] Summary + follow-ups presented to user in chat
```
