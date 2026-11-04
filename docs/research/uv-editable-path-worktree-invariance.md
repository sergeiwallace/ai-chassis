---
title: "uv Editable Path Dependencies and Git Worktree Invariance"
category: research
tags: [uv, dependency-management, git-worktrees, python-packaging]
status: draft
source: "claude-adhoc-2026-07-23"
template_version: "research-1.2.0"
---
<!-- Canonical Jinja source: STUB.md.jinja. This direct-copy stub is retained for consumers that have not migrated to rendering it. -->

# uv Editable Path Dependencies and Git Worktree Invariance

**Status:** draft

**Created:** 2026-07-23

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [Evidence and Corrections to the Starting Premise](#evidence-and-corrections-to-the-starting-premise)
  - [Case 1: two path failures were being conflated](#case-1-two-path-failures-were-being-conflated)
  - [Case 2: a correct global tool can still lose command resolution](#case-2-a-correct-global-tool-can-still-lose-command-resolution)
- [The Missing Design Decision](#the-missing-design-decision)
- [What the Current uv Primitives Actually Guarantee](#what-the-current-uv-primitives-actually-guarantee)
  - [`tool.uv.sources` path sources](#tooluvsources-path-sources)
  - [Git sources](#git-sources)
  - [uv workspaces](#uv-workspaces)
  - [Global uv tools](#global-uv-tools)
- [Required End-State Architecture](#required-end-state-architecture)
  - [One identity contract, three delivery channels](#one-identity-contract-three-delivery-channels)
  - [Source closure must be explicit](#source-closure-must-be-explicit)
  - [Command authority must be explicit](#command-authority-must-be-explicit)
- [Doctor and Enforcement Design](#doctor-and-enforcement-design)
  - [Checks](#checks)
  - [Repair backends](#repair-backends)
- [Comparison with Other Python Tools](#comparison-with-other-python-tools)
- [Options and Tradeoffs](#options-and-tradeoffs)
- [Gaps, Blindspots & Emergent Findings](#gaps-blindspots--emergent-findings)
- [Recommendation](#recommendation)
- [Implications](#implications)
- [Sources](#sources)
- [Ambiguous Items from Auto-Remediation (Post-Run Review)](#ambiguous-items-from-auto-remediation-post-run-review)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

<!-- doc:region name="context" kind="immutable" -->

## Context

This ecosystem chains uv editable path dependencies across three sibling repos (`ai-cli-utils -> aido -> framework-core`, each declaring `{path = "../other-repo", editable = true}`). This breaks under astral-sh/uv#6371 when resolved from inside a git worktree (the relative path resolves one level too shallow, e.g. `repo/.worktrees/framework-core` instead of the sibling repo). Every affected repo currently works around this with ad-hoc or partially-formalized symlink shims (see framework-core's committed `scripts/bootstrap-worktree.sh`, aido's open ISSUE-REF, and a new fix landing in ai-cli-utils same day). Given git worktrees are this ecosystem's universal session-isolation model (every CC session works in its own worktree, `/workspace/projects Git Worktree Model), find the robust, best-practice, durable solution for cross-repo Python dependency sharing that is worktree-invariant by construction, not one more repo-specific patch.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

Primary focus: official uv and Python packaging material current on 2026-08-20, weighted 2026 then
2025 then 2024. PEP 660 (2021) is used only as the foundational editable-install contract. The
installed fleet uv was also inspected locally on 2026-08-20. Poetry/PDM/pip/Rye comparison:
no significant post-2024 mechanism was found that makes editable source identity global across
multiple independently-resolved environments; their current documentation is used to establish
the narrower capabilities they do provide.

## Executive Summary

There is no single dependency mechanism that can mean both “use this mutable checkout immediately”
on a developer machine and “reproduce this immutable input” in a fresh clone with no checkout.
The durable answer is a **single fleet source-identity policy with different, explicit delivery
channels**: editable workspace/path sources for live local development, locked registry wheels (or
commit-pinned Git as an interim) for CI and clean machines, and an isolated editable tool install
for global CLIs. [INFERENCE]

For this fleet, the strongest by-construction option is to co-locate the tightly coupled Python
packages in one uv workspace, because every Git worktree then contains the complete relative source
topology and workspace members share one lock and are editable. uv explicitly positions workspaces
as its primitive for interconnected packages in one repository; it also states that workspace
members share one lockfile and inter-member dependencies are editable. [VERIFIABLE][^1][^2]
That still does **not** settle command ownership: the Case 2 failure is a shell executable-selection
problem, so the design must also reserve fleet CLI names, verify the selected executable and its
`direct_url.json`, and preferably separate reusable libraries from CLI distributions. [INFERENCE]

If repository consolidation is unacceptable, use a generated **fleet development overlay** from one
canonical manifest: promote the entire local fleet dependency closure to direct development
requirements, bind every member to an editable canonical checkout through a centrally maintained
worktree projection, sync every consumer, and fail closed when source identity differs. This is more
machinery than a workspace because uv still lacks a checked-out-but-untracked local source override
layer, while uv's still-open indirect-source issue reports that a `tool.uv.sources` entry is ignored
for a dependency that is only indirect. [VERIFIABLE][^3][^4]

## Evidence and Corrections to the Starting Premise

### Case 1: two path failures were being conflated

The historical fleet incident is established by the immutable Context: a relative sibling path that
is correct from a main checkout points inside `repo/.worktrees/` when evaluated from a linked
worktree, and fleet repos added symlink projections to restore the expected topology. Local history
confirms the aido bootstrap landed on 2026-07-23. The current local aido checkout has since changed
`framework-core` to a Git `main` source with a comment that this works from every worktree; this avoids the
path-topology failure but creates a locked-snapshot channel of the kind seen in Case 2. These local
repositories are not publicly fetchable, so this paragraph is based on direct working-tree and Git
history inspection rather than an external URL. [NO SOURCE]

The prompt's upstream-status premise needs correction. uv issue #6371 was a **transitive lockfile
path-collapse** bug: a nested editable's path was retained relative to the nested project and became
wrong in the top-level lock. The issue is closed. [VERIFIABLE][^5] PR #6490 explicitly says it
fixes #6371 and was merged on 2024-08-24. [VERIFIABLE][^6] GitHub's comparison shows the fixing commit is
after tag 0.3.3, and the commit is an ancestor of the 0.3.4 tag; therefore the first released version
containing it was **uv 0.3.4**. [VERIFIABLE][^7]

The installed fleet version is `uv 0.10.0 (0ba432459 2026-02-05)`, while the current upstream release
on the research date is 0.12.5 (2026-08-14). The release page is externally verifiable; the installed
version is direct local command evidence. [VERIFIABLE][^8] Consequently, the fleet is far past
the #6371 fix. [INFERENCE]

That does not disprove the fleet's worktree incident. It means the incident's **physical topology
failure** (`../framework-core` from `aido/.worktrees/<branch>`) should not be called an unfixed #6371 defect
without a current minimal reproducer. Current uv documentation says a path source is resolved
relative to the declaring project, which naturally changes when the declaring project itself is at a
different filesystem depth. [VERIFIABLE][^1] #6371 fixed transitive lock normalization; it did not
promise that arbitrary sibling-repository layouts remain invariant when a Git worktree moves one
repository deeper. [INFERENCE]

### Case 2: a correct global tool can still lose command resolution

The incident record was read from framework-core's local `.beads/issues.jsonl` because `bd show` could not
open its embedded database lock under this research worker's one-file write sandbox. Direct local
inspection reproduced the decisive facts on 2026-08-20:

- `direnv exec <workspace> command -v core` resolves
  `<workspace>/.venv/bin/core` first; `~/.local/bin/core` is second.
- The project environment's `ai_core-0.1.0.dist-info/direct_url.json` records a Git URL, requested
  revision `main`, and a fixed commit, with no editable flag.
- The uv tool environment's corresponding file records
  `file:///home/example-user/projects/framework-core` and `"editable": true`.
- The separate globally installed aido tool environment contains its own `framework-core` Git snapshot,
  confirming that making the root tool editable does not make its first-party transitive closure
  editable.
- `example-user/.envrc` calls `PATH_add .venv/bin`; its `pyproject.toml` sources `aido` from Git `main`, and
  aido brings `framework-core` transitively.

Those are local primary observations but have no public source URL. [NO SOURCE]

PEP 610/`direct_url.json` is the right machine-readable identity evidence: its schema represents a
Git requested revision and resolved commit, or a local directory with `dir_info.editable=true`.
[VERIFIABLE][^9] PEP 660 additionally requires an editable frontend to record the project directory
as a `file://` URL with that editable flag. [VERIFIABLE][^10] The two observed files therefore prove
that the two `core` commands are backed by different source identities even though both distributions
report version 0.1.0. [INFERENCE]

## The Missing Design Decision

The common root is not “relative paths are bad” or “direnv ordering is bad.” It is that the fleet has
no authoritative answer to all four of these questions:

1. Which checkout or artifact is authoritative for each fleet package in this context?
2. Must the context be **live** (mutable checkout) or **reproducible** (immutable commit/artifact)?
3. Which consumer owns the resolution, including transitive packages?
4. Which executable is authoritative when multiple installed distributions export the same command?

An editable install is an **environment-local pointer**, not a fleet-wide singleton. PEP 660 promises
that Python source edits in the pointed-to local directory become effective without reinstalling;
entry-point, dependency-metadata, and non-Python changes can still require reinstall/build.
[VERIFIABLE][^10] Every venv and every uv tool environment therefore makes a separate resolution
choice. [INFERENCE]

“Always live” and “fresh clone” are mutually exclusive inputs: a clean CI runner cannot import a
checkout it does not possess, while a lock intended to reproduce a build must not silently follow a
moving branch. [INFERENCE] The fleet needs one policy, not one transport.

## What the Current uv Primitives Actually Guarantee

### `tool.uv.sources` path sources

`tool.uv.sources` is uv-only development enrichment layered over standard project dependency
metadata. It supports local path projects and `editable=true`; uv recommends `uv build --no-sources`
to prove that the package still builds when these development overrides are absent.
[VERIFIABLE][^1] This is the correct primitive for live development **when the path has an invariant
anchor**. A bare cross-repo `../sibling` is not such an anchor under this fleet's linked-worktree
layout. [INFERENCE]

Absolute checkout paths are source-correct but machine-specific. Committed relative paths are
portable only across an identical topology. Symlink shims are therefore a topology adapter, not a
source policy. They can be made reliable if generated centrally and verified, but adding one ad hoc
shim per repository scales the number of silent failure points. [INFERENCE]

### Git sources

A Git branch source solves local filesystem topology and works in a clean clone, but it does not
provide live local edits. uv explicitly prefers the commit already recorded in `uv.lock` for a Git
`main` dependency until `--upgrade` or `--upgrade-package` is requested. [VERIFIABLE][^11] Thus
“branch = main” means “resolve a commit, then reuse it,” not “execute today's main on every run.”
[INFERENCE]

Git by full commit is a valid immutable CI/inter-repo channel. A moving branch plus an unowned update
cadence is the worst hybrid here: it looks fresh in configuration while behaving frozen in the
environment. [HEURISTIC]

### uv workspaces

uv workspaces provide one root, one lockfile, root-level source policy, and editable dependencies
between members. uv documents them primarily for multiple interconnected packages in a single Git
repository and warns that they are unsuitable when members require conflicting requirements or
separate venvs. [VERIFIABLE][^2]

A workspace monorepo is worktree-invariant by construction: Git checks out the workspace root and all
member paths together, so moving the whole checkout does not change member-relative topology.
[INFERENCE] It also centralizes transitive source selection and gives each worktree one internally
consistent lock. The tradeoff is real: repository lifecycle, lockfile, Python compatibility
intersection, and default environment become coupled. [VERIFIABLE][^2]

Current 0.12.5 documentation also permits `workspace = "../other-workspace"` to select a member from
an external workspace, with the path resolved relative to the declaring project. [VERIFIABLE][^2]
That is not a magic cross-repo solution for this fleet: the relative external path remains sensitive
to the declaring worktree's depth. The research did not locate a release note establishing which uv
version introduced this syntax, so compatibility with installed 0.10.0 must be tested before use.
[NO SOURCE]

### Global uv tools

`uv tool install` creates an isolated tool environment and links its executables into the uv tool bin
directory. Tool commands ignore project-level uv configuration. [VERIFIABLE][^12][^13] Isolation
prevents dependency contamination inside the tool environment; it does not give its launcher
priority over a same-named script earlier on shell `PATH`. [INFERENCE]

For a tool whose source should track a checkout, `uv tool install --force --editable <checkout>` is
appropriate for the root package, but its first-party transitive closure must be supplied explicitly
with repeated `--with-editable <checkout>` arguments. uv documents `--with-editable` for tool
installation. [VERIFIABLE][^23] This still affects only that tool environment and cannot repair
another project's venv. The tool installer is therefore one repair backend, not the global
source-of-truth mechanism. [INFERENCE]

## Required End-State Architecture

### One identity contract, three delivery channels

Define a versioned fleet manifest containing, for every first-party distribution:

- normalized distribution name, import package(s), and exported console scripts;
- canonical local checkout identity and allowed worktree mapping;
- development mode (`editable` required or immutable allowed);
- release channel and minimum/version compatibility relationship;
- expected source closure (all first-party transitives); and
- executable authority (`project`, `fleet-tool`, or forbidden duplicate).

Then use three explicit channels:

| Context | Delivery | Freshness contract |
|---|---|---|
| Fleet package development | uv workspace member; otherwise generated editable path overlay | Same local source identity; Python edits live |
| Global fleet CLI | isolated `uv tool install --editable` plus manifest-generated `--with-editable` closure | Same local source identities; command authority verified |
| CI, deployment, fresh clone | versioned wheel from an index, or temporary full-SHA Git source, locked | Immutable, rebuildable input; never claims to be live |

This is one **identity and freshness policy** implemented by multiple transports. A single transport
cannot meet all three contracts. [INFERENCE]

### Source closure must be explicit

A downstream root owns its complete resolution. uv issue #9446 reports that, in uv 0.5.4, a
`tool.uv.sources` entry is ignored when the named package is not a direct dependency; the issue
remains open/undecided, and current official documentation does not promise indirect-only source
application. [VERIFIABLE][^3] Therefore adding only
`aido = { git = ..., branch = "main" }` and assuming aido's development source choices propagate is
unsafe without a version-pinned regression test. `tool.uv.sources` is also uv-specific development
configuration rather than published dependency metadata. [VERIFIABLE][^1]

For a multi-repo development overlay, the generator must promote **every first-party package in the
transitive closure** to an explicit direct development requirement at the consuming root, then attach
the canonical editable source. That is why the earlier example-user fix had to list `ai-cli-utils` directly;
the same rule must cover `framework-core`. [INFERENCE]

uv has an open enhancement specifically requesting a local, gitignored source override for developers
working across repositories, and a separate open design issue for switching path sources locally and
Git sources in production. [VERIFIABLE][^4][^14] Until uv ships such a layer, a generated overlay,
workspace façade, or centrally generated symlink projection is unavoidable if repositories remain
separate. [INFERENCE]

### Command authority must be explicit

`direnv`'s `PATH_add` deliberately **prepends** its argument. [VERIFIABLE][^15] This matches standard
Python venv activation, which prepends the venv's script directory so `python` and installed scripts
come from that environment. [VERIFIABLE][^16] Prepending `.venv/bin` is therefore not inherently an
anti-pattern; it is the conventional meaning of activation. [INFERENCE]

The anti-pattern is exporting the same console-script name from a transitive library and a global
tool, then assuming the global one will win. Astral's own setup-uv documentation warns that activating
an environment can shadow an installed executable and recommends `uv run` instead for most CI
workflows. [VERIFIABLE][^17]

Adopt these conventions:

1. **Project-owned command:** invoke with `uv run <command>` and expect the project lock/source policy.
2. **Fleet-global command:** invoke a qualified launcher (for example `fleet core`, or a reserved
   fleet-bin shim that resolves the manifest-owned uv tool), not a bare ambiguous name.
3. **No global PATH reorder:** do not put all of `~/.local/bin` ahead of `.venv/bin`; that would make
   unrelated global tools override legitimate project tools.
4. **Package split where feasible:** make `framework-core` the reusable library and `core-cli` the tool
   distribution. Then a transitive library install cannot emit `core` into every consumer venv.

The package split is the cleanest structural fix for Case 2. Workspaces explicitly cite separating a
core library and CLI as a supported use case. [VERIFIABLE][^2] A selective reserved-command shim is a
practical transitional fix. [HEURISTIC]

## Doctor and Enforcement Design

### Checks

A fleet `doctor` should run both in a plain shell and under each active project/worktree environment:

1. Resolve each reserved command with `command -v`/`whence -a` (or Python `shutil.which`) and compare
   its real path with the manifest. Python documents `shutil.which` as returning the executable that
   would run from `PATH`. [VERIFIABLE][^18]
2. Execute a machine-readable `--provenance` endpoint that emits executable path, `sys.executable`,
   distribution version, `direct_url.json`, editable bit, source root, and Git commit/dirty state.
3. Enumerate every installed copy of every fleet distribution in the active venv and uv tool
   environments; reject multiple source identities unless the manifest explicitly allows them.
4. Verify a live development copy has `url=file://<expected-root>` and
   `dir_info.editable=true`; verify an immutable copy has the expected wheel hash or Git commit.
5. Run `uv lock --check`, then verify the installed environment agrees with the lock. Do not equate a
   green lock check with live source identity.
6. Verify the full first-party source closure, including packages introduced only transitively.
7. In CI, run `uv build --no-sources` and a clean `uv sync --frozen` to prove that local overrides are
   not required for published artifacts. uv documents both the no-sources build check and frozen
   locked behavior. [VERIFIABLE][^1][^11]

`direct_url.json` is preferable to grepping `uv tool list` output because it is standardized and
works for ordinary project environments as well as tool environments. [VERIFIABLE][^9]

### Repair backends

The existing editable-install enforcement pattern generalizes naturally at the **detect, explain,
repair, re-check** control-plane level, but not as one repair command. [INFERENCE]

- **Global tool mismatch:** reinstall the manifest-owned root with
  `uv tool install --force --editable <expected-checkout>` plus generated `--with-editable` arguments
  for every first-party transitive, then re-check launcher and the complete provenance closure.
- **Downstream venv mismatch:** change/apply the root development overlay so the package closure is
  direct and editable, regenerate the lock, `uv sync`, then re-check. Reinstalling the global tool
  cannot affect this venv.
- **Worktree topology mismatch:** materialize/repair the centrally defined workspace or symlink
  projection before resolution, then sync and re-check. A package reinstall cannot make a nonexistent
  relative path correct.
- **CI mismatch:** fail; update the declared version/commit and lock through review. CI must never
  auto-switch to a developer checkout.

Automatic repair should be limited to deterministic local state. Source-policy changes and lock
updates are reviewable repository changes, not silent doctor side effects. [HEURISTIC]

## Comparison with Other Python Tools

The surrounding ecosystem validates the mechanism but does not solve fleet-wide identity:

| Tool | Current local-development mechanism | Does it solve both cases? |
|---|---|---|
| pip | `python -m pip install -e <local-path>`; regular installs recommended for CI/deployment | No lock/workspace policy; explicit absolute paths can avoid topology mistakes, but each environment and PATH still diverges. [VERIFIABLE][^19] |
| Poetry 2.4 | `{ path = "../pkg", develop = true }`; Git branch/rev/tag sources | No. Its relative path is still topology-dependent, and a Git source is still a snapshot in an environment. [VERIFIABLE][^20] |
| PDM | local `${PROJECT_ROOT}` URL encoding; editables only in development groups | Better separation of editable development intent, but the project root is still the current worktree and each venv/command resolves independently. [VERIFIABLE][^21] |
| Rye | Former project/monorepo/tool manager | Not a candidate: Rye says development and security updates have ended and directs users to uv. [VERIFIABLE][^22] |
| uv workspace | editable members, one root lock and source policy | Solves Case 1 when packages share one repository/worktree; command collisions still require Case 2 policy. [VERIFIABLE][^2] |

PEP 660 deliberately standardizes what an editable installation means **inside one environment**;
none of these frontends standardizes a cross-environment singleton or shell command precedence.
[VERIFIABLE][^10] Switching package managers would rename the configuration, not remove the missing
fleet policy. [INFERENCE]

## Options and Tradeoffs

| Criterion | uv workspace monorepo | Multi-repo generated overlay | Git/registry release-first | Current ad hoc shims |
|---|---:|---:|---:|---:|
| Worktree-invariant live source | Strong | Strong if projection/manifest is enforced | No live edits | Conditional |
| Transitive source consistency | One root policy | Generator must promote closure | Lock provides immutable consistency | Per-repo and easy to omit |
| Clean-clone/CI | Build without sources; registry lock | Separate immutable channel required | Strong | Weak without siblings |
| Repository independence | Low | High | High | High |
| Separate venvs/locks | Limited | Yes | Yes | Yes |
| Case 2 command-shadow fix | Still needs command policy/split | Still needs command policy/split | Same version can still collide | No |
| Operational complexity | Migration-heavy, simpler steady state | Moderate/high tooling | Release automation and edit latency | Low upfront, high incident cost |

## Gaps, Blindspots & Emergent Findings

1. **Anchor-bias correction — #6371 is fixed.** The starting frame treated the historical fleet
   worktree failure and upstream #6371 as the same continuing defect. They are adjacent but not
   identical: #6371's lock normalization shipped in 0.3.4; current worktree-relative topology remains
   a design hazard. [VERIFIABLE][^5][^6][^7]
2. **uv still lacks the obvious multi-repo local override layer.** Open issues #12013 and #7945 show
   that “local editable here, Git/registry elsewhere without editing committed config” remains an
   unresolved product gap in 2026. [VERIFIABLE][^4][^14]
3. **Indirect source overrides are a separate trap.** Open #9446 reports that a source entry alone
   was not sufficient for a purely transitive dependency in uv 0.5.4. Because no current guarantee
   to the contrary was found, the safe rule is to promote it to direct or use a workspace/root
   mechanism and keep a regression test. [VERIFIABLE][^3]
4. **CLI/library co-packaging amplifies PATH collisions.** Installing a library transitively also
   installs its declared console scripts. Splitting distributions can remove the collision instead
   of continually repairing it. This follows from the observed incident and packaging shape, but no
   post-2024 comparative study of Python fleet CLI/library splitting was located. [NO SOURCE]
5. **Editable does not mean metadata-live.** Entry-point and dependency changes require reinstall;
   native code requires rebuild. A doctor must distinguish Python-source freshness from installation
   metadata freshness. [VERIFIABLE][^10]
6. **Shared venv as a tempting false shortcut.** Pointing all projects at one
   `UV_PROJECT_ENVIRONMENT` would create sync ownership and concurrency conflicts: an exact sync by
   one project may remove another's packages. It is not a safe fleet singleton. This was reasoned
   from uv's project-environment model; no fleet-scale post-2024 study was found. [NO SOURCE]
7. **External-workspace syntax is unresolved for this fleet.** Current docs expose it, but its
   introduction version and behavior on installed 0.10.0 were not established, and its relative path
   still inherits worktree depth. Test it only as a possible façade after upgrading uv. [NO SOURCE]
8. **Security/supply-chain dimension.** Moving CI to wheels/full SHAs improves input immutability;
   moving development to editable local trees increases the importance of checkout provenance and
   dirty-state reporting. The doctor should report, not silently clean, a dirty authoritative source.
   [HEURISTIC]

## Recommendation

Adopt the two-plane architecture now:

- **Development plane:** prefer a uv workspace monorepo for the tightly coupled
  `ai-cli-utils`/`framework-core`/`aido` set. If organizational boundaries prevent that, implement one
  generated fleet overlay and retire repo-specific hand-authored source/shim rules.
- **Release plane:** publish/version wheels and lock each consumer; use full Git SHAs only as an
  interim. Gate every package with `uv build --no-sources` plus clean frozen sync.
- **Command plane:** split libraries from globally authoritative CLI distributions where practical;
  otherwise reserve fleet command names behind qualified launchers. Enforce selected executable and
  standardized source provenance in every environment.
- **Control plane:** generalize editable-install enforcement into one manifest-driven doctor with
  separate tool, venv, topology, and CI repair/failure backends.

This recommendation makes “fresh” explicit: local development means the expected checkout is
editable; CI means the expected immutable artifact/commit is installed. It never uses “branch main”
as a synonym for either. [INFERENCE]

## Implications

A follow-up design task should choose among these concrete remedies:

1. **Strategic (recommended): uv workspace consolidation plus CLI/library split.** Move the tightly
   coupled Python packages into one Git repository/workspace, keep versioned publishable metadata,
   install global CLIs editable from their workspace members with the complete first-party editable
   closure, and reserve/qualify CLI launchers.
   This removes Case 1's cross-repo topology by construction and Case 2's accidental transitive
   console script structurally.
2. **Pragmatic multi-repo: generated fleet development overlay plus doctor.** Keep repos independent,
   but generate the full direct editable source closure from one fleet manifest, create one verified
   worktree projection, sync each consumer, and enforce command/source identity. This covers both
   cases without waiting for uv's local-source-override feature.
3. **Release-first consistency: internal index and automated lock refresh.** Publish every fleet
   change as a versioned wheel, make all downstreams and global tools consume the same reviewed
   version, and automate updates. This is maximally reproducible but intentionally gives up
   immediate live edits; choose it only if “fresh” can mean a short, measured release SLO.
4. **Containment only: centralize existing shims and add reserved-command preflight.** Replace ad hoc
   scripts with one manifest-driven bootstrap, verify all `direct_url.json` records, and fail direnv
   load or session preflight when a reserved command resolves incorrectly. This closes known holes
   fastest but retains the topology adapter and should have a retirement date.

## Sources

[^1]: Astral. (2026). [Managing dependencies](https://docs.astral.sh/uv/concepts/projects/dependencies/).
    uv Documentation. Verified accessible (HTTP 200) 2026-08-20. (`tool.uv.sources`, editable path
    dependencies, workspace sources, and `uv build --no-sources`.)
[^2]: Astral. (2026). [Using workspaces](https://docs.astral.sh/uv/concepts/projects/workspaces/).
    uv Documentation. Verified accessible (HTTP 200) 2026-08-20. (Single lock, editable members,
    external-workspace syntax, limits, and intended monorepo use.)
[^3]: Astral. (2024–2026). [`tool.uv.sources` should work for indirect dependencies, issue #9446](https://github.com/astral-sh/uv/issues/9446).
    GitHub. Verified accessible (HTTP 200)
    2026-08-20; open. (Current source entries are ignored for packages not directly declared.)
[^4]: Astral. (2025–2026). [Configure dependency sources only locally, issue #12013](https://github.com/astral-sh/uv/issues/12013).
    GitHub. Verified accessible (HTTP 200)
    2026-08-20; open. (Request for a gitignored/local cross-repo source override layer.)
[^5]: Astral. (2024). [`uv lock` collapses relative paths to editable builds in monorepo, issue #6371](https://github.com/astral-sh/uv/issues/6371).
    GitHub. Verified accessible (HTTP 200)
    2026-08-20; closed. (Original transitive relative-lock-path failure.)
[^6]: Marsh, C., et al. (2024). [Only use relative paths in lockfile, PR #6490](https://github.com/astral-sh/uv/pull/6490).
    astral-sh/uv, GitHub. Verified accessible
    (HTTP 200) 2026-08-20; merged 2024-08-24. (Explicitly fixes #6371.)
[^7]: Astral. (2024). [Comparison: uv 0.3.3 to fixing commit f783524](https://github.com/astral-sh/uv/compare/0.3.3...f783524) and [uv 0.3.4 release](https://github.com/astral-sh/uv/releases/tag/0.3.4).
    GitHub. Verified accessible
    (HTTP 200) 2026-08-20. (Establishes the fix landed after 0.3.3 and before/in 0.3.4.)
[^8]: Astral. (2026). [uv 0.12.5 release](https://github.com/astral-sh/uv/releases/tag/0.12.5).
    GitHub. Verified accessible (HTTP 200) 2026-08-20. (Latest release and date on research date.)
[^9]: Python Packaging Authority. (2026). [Direct URL Data Structure](https://packaging.python.org/en/latest/specifications/direct-url-data-structure/).
    Python Packaging User Guide. Verified accessible (HTTP 200) 2026-08-20. (Git commit and editable
    local-directory provenance schema.)
[^10]: Holth, D., & Bidoul, S. (2021; page updated 2025). [PEP 660 — Editable installs for pyproject.toml based builds](https://peps.python.org/pep-0660/).
    Python Enhancement Proposals.
    Verified accessible (HTTP 200) 2026-08-20. (Editable semantics, reinstall limitations, and
    required `direct_url.json`.)
[^11]: Astral. (2026). [Locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/).
    uv Documentation. Verified accessible (HTTP 200) 2026-08-20. (Locked Git branches, explicit
    upgrades, editable workspace sync, and frozen/check behavior.)
[^12]: Astral. (2026). [Tools](https://docs.astral.sh/uv/concepts/tools/). uv Documentation.
    Verified accessible (HTTP 200) 2026-08-20. (Isolated tool environments and executable links.)
[^13]: Astral. (2026). [Configuration files](https://docs.astral.sh/uv/configuration/files/).
    uv Documentation. Verified accessible (HTTP 200) 2026-08-20. (Tool commands ignore local project
    configuration.)
[^14]: Astral. (2024–2026). [Modifying `tool.uv.sources` between production and development, issue #7945](https://github.com/astral-sh/uv/issues/7945).
    GitHub. Verified accessible (HTTP 200)
    2026-08-20; open/needs design. (Path-for-development versus Git-for-production gap.)
[^15]: direnv contributors. (2026). [`PATH_add` — direnv standard library](https://direnv.net/man/direnv-stdlib.1.html#codepath_add-ltpathgtcode).
    direnv
    Documentation. Verified accessible (HTTP 200) 2026-08-20. (`PATH_add` prepends.)
[^16]: Python Software Foundation. (2026). [`venv` — Creation of virtual environments](https://docs.python.org/3/library/venv.html#how-venvs-work).
    Python 3.14
    Documentation. Verified accessible (HTTP 200) 2026-08-20. (Activation prepends the venv script
    directory and venvs are non-portable.)
[^17]: Astral. (2026). [Environment and tools](https://github.com/astral-sh/setup-uv/blob/main/docs/environment-and-tools.md).
    astral-sh/setup-uv Documentation. Verified accessible (HTTP 200) 2026-08-20. (Activation can
    shadow installed tools; recommends `uv run` for most workflows.)
[^18]: Python Software Foundation. (2026). [`shutil.which`](https://docs.python.org/3/library/shutil.html#shutil.which).
    Python 3.14 Documentation. Verified accessible (HTTP 200) 2026-08-20. (Returns the executable
    selected from `PATH`.)
[^19]: Python Packaging Authority. (2026). [Local project installs](https://pip.pypa.io/en/stable/topics/local-project-installs/).
    pip Documentation.
    Verified accessible (HTTP 200) 2026-08-20. (Editable local development versus regular CI and
    deployment installs.)
[^20]: Python Poetry. (2026). [Dependency specification — path dependencies](https://python-poetry.org/docs/dependency-specification/#path-dependencies).
    Poetry 2.4 Documentation. Verified accessible (HTTP 200) 2026-08-20. (Relative path with
    `develop=true` and Git source forms.)
[^21]: PDM contributors. (2026). [Manage dependencies](https://pdm-project.org/latest/usage/dependency/#editable-dependencies).
    PDM Documentation. Verified accessible (HTTP 200) 2026-08-20. (`${PROJECT_ROOT}` local URLs and
    editable dependencies restricted to development groups.)
[^22]: Astral. (2026). [Rye](https://rye.astral.sh/). Rye Documentation. Verified accessible
    (HTTP 200) 2026-08-20. (No longer developed; no further updates including security fixes.)
[^23]: Astral. (2026). [`uv tool install` command reference](https://docs.astral.sh/uv/reference/cli/#uv-tool-install).
    uv Documentation. Verified accessible (HTTP 200) 2026-08-20. (`--editable` for the target and
    `--with-editable` for additional editable requirements.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

```text
You are refining an EXISTING, abandoned research stub — this is a `--refine` leg, not a fresh
research doc. Write the complete research document directly into the target file.

## Objective

Find the robust, durable, best-practice solution for cross-repo/cross-project Python dependency
sharing in an ecosystem where every session works in an isolated git worktree, given that today's
approach (uv editable path dependencies, `{path = "../other-repo", editable = true}`) breaks or
silently drifts under worktree isolation in at least two distinct, confirmed failure modes.

## Background — two concrete, independently-confirmed grounding cases

**Case 1 (original, already scoped in the doc's own Context region — do not re-derive, just use
it):** this ecosystem chains uv editable path dependencies across three sibling repos
(`ai-cli-utils -> aido -> framework-core`, each declaring `{path = "../other-repo", editable = true}`).
This breaks under astral-sh/uv#6371 when resolved from inside a git worktree — the relative path
resolves one level too shallow (e.g. `repo/.worktrees/framework-core` instead of the sibling repo). Every
affected repo currently works around this with ad-hoc or partially-formalized symlink shims (see
framework-core's committed `scripts/bootstrap-worktree.sh`, aido's open ISSUE-REF, and a fix that landed in
ai-cli-utils around the same time).

**Case 2 (new, discovered 2026-08-20/21 — fold this in as a second concrete grounding case, same
underlying category of problem but a different failure mechanism):** a *downstream project's own*
venv can silently shadow a *fixed, editable, globally-installed* tool with a *stale, frozen*
transitive copy of the same package. Specifically: `<workspace>`'s project `.venv` installs
`framework-core` as a non-editable dependency, pulled in transitively because `aido` is pinned via
`[tool.uv.sources]` to `{ git = "...", branch = "main" }` in `example-user/pyproject.toml`, and `framework-core`
is itself `aido`'s own dependency. Meanwhile, the *global* `core` CLI tool
(`uv tool install --force --editable <workspace>/repository`) correctly resolves to the live editable
checkout. The problem: `direnv`'s `PATH_add` in `example-user/.envrc` prepends that project venv's
`bin/` directory ahead of `~/.local/bin` (where the global editable tool's shim lives), so running
`core ...` from any directory where `example-user`'s direnv is active silently resolves to the STALE,
frozen venv copy instead of the correct global editable one — even though the global tool itself
was already fixed. This reproduced the exact same class of bug (mangled paths / stale corpus
queries) that ISSUE-REF and a prior stale-global-tool bug already fixed, purely because of this
PATH-shadowing mechanism. Tracked as bd issue `ISSUE-REF` / `ISSUE-REF` in the
`framework-core` repo — read it (`bd show ISSUE-REF -C <workspace>/repository`) for the full incident
writeup before starting.

Both cases share a root shape worth naming explicitly in your synthesis: **this fleet has multiple,
independently-evolving mechanisms for "make a locally-developed library's live source visible to a
consumer" (uv editable path deps across worktrees; uv git-pinned deps that resolve to a frozen
snapshot; global `uv tool install --editable`; per-project venvs each making their own resolution
choice) with no single canonical policy governing which one a given consumer should use, or how
"freshness" is guaranteed end-to-end.** Do not treat these as two unrelated one-off bugs — they are
two symptoms of the same missing design decision.

## Research questions (a floor, not a ceiling — see the independent-exploration block below)

- What is the current (2025-2026) best-practice pattern for uv-based monorepo/multi-repo Python
  projects to share a locally-developed library's LIVE source across sibling repos, that is
  invariant to whether the consumer is resolved from a git worktree, a main checkout, a downstream
  project's own venv, or a global `uv tool install`? Cover `[tool.uv.sources]` path vs. git vs.
  workspace members, `uv workspace` (uv's native monorepo primitive) as a possible alternative to
  ad-hoc path deps entirely, and how other tools (Poetry path deps, pip `-e`, PDM, Rye) have solved
  or failed to solve the same class of problem.
- For astral-sh/uv#6371 specifically (the worktree-relative-path-resolves-shallow bug): is it fixed
  upstream as of your research date? If not, what's the maintainers' stated direction? If yes, what
  uv version introduced the fix and is this fleet's pinned uv version past it?
  ("`aido: 0.6.0`, `framework-core: 0.1.0`" are the fleet's current versions if a source needs an anchor —
  verify current uv itself via `uv --version` if you have shell access, or search for the latest
  released uv version as of your research date.)
- What's the right way to guarantee "every consumer of a locally-developed library resolves to the
  SAME live source, with no silent frozen-fork drift" across: (a) sibling repos consuming each
  other directly, (b) a downstream project consuming a fleet library transitively through another
  fleet library, (c) a global CLI tool install, (d) CI/fresh-clone environments that have none of
  this local-path context? Is there a single mechanism that covers all four, or does the fleet
  genuinely need different mechanisms per case (and if so, how should they be made mutually
  consistent rather than independently drifting)?
- What's the best-practice guidance on `direnv`'s `PATH_add` ordering specifically — is prepending
  a project venv's `bin/` ahead of `~/.local/bin`/global tool shims itself an anti-pattern, or is
  there a standard, safer ordering/convention that avoids exactly this shadowing failure mode
  without breaking the legitimate reason a project venv wants its OWN tools found first?
   - How do comparable fleets/orgs detect this class of shadowing automatically (e.g., a `doctor`/
    preflight check that verifies "the `core` binary actually resolved is the one you think it is")
    rather than discovering it only when a stale-data symptom appears downstream?
- Given this fleet already has `docs/procedures/editable-install-enforcement.md` (governs `aido`
  and `framework-core-mcp` GLOBAL tool installs specifically) — is there a natural generalization of that
  same enforcement pattern (detect + auto-repair a frozen/non-editable install) that could cover
  BOTH the worktree-relative-path case AND the downstream-venv-shadowing case, or do they need
  genuinely distinct remedies? Read that doc for the existing pattern before answering.

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
- Emerging 2025–2026 practices, tools, or research not anchored by the named examples.
- Known failure modes and second-order effects.
Whenever a load-bearing thread surfaces mid-research, follow it to its conclusion and report it in
a dedicated "Gaps, blindspots & emergent findings" subsection. Explicitly NAME any blindspot you
suspect but cannot resolve (and why) rather than omitting it. Anchor bias — over-fitting to the
listed questions and example approaches — is a known failure mode; counter it deliberately and say
where you did.

<grounding_instructions>
You are a principal engineer specializing in Python packaging and monorepo/multi-repo dependency
architecture, with hands-on production experience migrating fleets of interdependent services onto
uv. You have strong opinions backed by evidence. When you cannot find a source, you say so
explicitly.

Temporal scope: Weight sources by recency — 2026 (primary) → 2025 → 2024. Pre-2024 sources are
background context only unless foundational (e.g., the original editable-install / PEP 660
mechanism). If post-2024 literature is genuinely sparse for a subtopic, state
"[subtopic]: no significant post-2024 developments found" rather than backfilling with older
sources. Backfilling is a failure mode, not a hedge.

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
- [NO SOURCE]: explicitly state when you cannot find verifiable data. Tier tag only — NO footnote
  ref.

Citation format (mandatory for every externally-sourced claim):
- Inline: append the GFM footnote ref directly after the tier tag — [VERIFIABLE][^N]. A claim
  citing multiple sources carries ascending separate refs — [VERIFIABLE][^3][^7] (never grouped
  [^3,^7]; never out of order).
- Footnote definitions live once, under ## Sources, in APA form with a clickable URL or DOI and an
  access-verification stamp. Worked example:
    [^1]: LangChain. (2026). [Threads](https://docs.langchain.com/langsmith/threads).
    LangSmith Documentation. Verified accessible (HTTP 200) 2026-06-03. (Scope note.)
- URL-or-DOI ALWAYS: every source entry carries a clickable URL or a doi.org link — paywalled/
  gated is fine (link it anyway; stamp the access status). Only the truly-irreducible case (no
  online catalog presence anywhere) gets an explicit [no online source located] marker with a
  one-line justification.
- Integrity: footnote refs are contiguous from [^1], every [^N] ref has a matching definition, and
  every definition is referenced — no gaps, no orphans.
- [INFERENCE] / [NO SOURCE] claims carry the tier tag with NO footnote ref.

Hard constraint (overrides all formatting preferences): never invent a citation to satisfy a
formatting instruction. Accuracy > completeness.

Format diagrams using Mermaid.js or ASCII. Format math using LaTeX. NEVER generate binary images.

Retrieval enforcement: for each load-bearing claim, run at least one web_search or web_fetch call
before tagging it [VERIFIABLE]; do not return [VERIFIABLE] without a URL you actually fetched in
this session. Do not stop after one round — if no source is found for a sub-question, run
additional searches across alternate framings before tagging it [NO SOURCE].
</grounding_instructions>

## Output contract

Write the complete research document directly into the target file (see Tools & scope). It already
carries doc-region markers (`<!-- doc:region name="..." kind="..." -->`) — preserve them exactly,
including the immutable `context` region (extend its prose in place if needed to introduce Case 2,
but do not delete or restructure the existing Case-1 framing). Fill:
- The `body` region with your full synthesis (this is the actual research content).
- The `appendix_research_prompt` region with this exact prompt (replace the
  `[Full prompt text here]` placeholder with the real text you were given).
- Update the top-level `**Status:**` line from `stub` to `draft` and `status: draft` in frontmatter
  is already correct; also flip the `template_version`/`source` fields only if the canonical STUB
  requires it (check `docs/research/STUB.md` for the current expected shape — do not guess).
End with a clear "Implications" section: 2-4 concrete candidate remedies, covering both grounding
cases, that a follow-up design/implementation task could choose between.

## Tools & scope

You are a leaf: do NOT spawn sub-agents.

Write target (must sit inside your `-C` root, already exists — refine in place, do not recreate):
`docs/research/uv-editable-path-worktree-invariance.md`

Repo root / your `-C`: `/home/example-user/projects/ai-harness/.worktrees/ISSUE-REF/ISSUE-REF`

## Pipeline declaration

caller: direct
resolve_decisions: false
authority: null
authority_source: absent
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

(none yet — auto-populated at commit from the validated provenance ledger)

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

<!-- /doc:region name="run_history" -->
