---
name: pull-git
description: Fast-forward every fleet repo AND every git worktree from its remote wherever that is provably safe — one deterministic command instead of clicking the VS Code sync button per repo.
disable-model-invocation: true
---

# pull-git

Sync **every repo the fleet owns and every worktree inside them** from their remotes, in one
command, with no judgement calls. This replaces opening each repo in VS Code and clicking
the Source Control sync button, which never covered the worktrees at all.

**Usage:**

```bash
/pull-git                       # fast-forward everything that provably can be (tier 1)
/pull-git --dry-run             # verdict table only, change nothing
/pull-git --only ai-harness     # narrow to one repo (repeatable)
/pull-git --rebase-local        # ALSO rebase trees holding unique local commits (tier 2)
/pull-git --autostash           # ALSO stash/advance/pop dirty trees (tier 3)
```

The machinery is the git-pull sweep script, which consumes the git-review sweep script for repo
discovery, the fleet project-registry config file for gated branches and the process-cwd liveness
probe, and the worktree-audit script for the worktree list. Every precondition is **in that script
and tested** (the git-pull sweep script's test suite), deliberately:
a proof that exists only as prose in this file cannot be verified.

```bash
uv run --script scripts/git_pull_sweep.py               # sync
uv run --script scripts/git_pull_sweep.py --dry-run     # preview
uv run --script scripts/git_pull_sweep.py --json        # machine-readable
```

## There is no model in the decision path

Tier 1 is a pure function of five git facts. **Do not "help" it**: do not read the table and
then merge, rebase, reset, or resolve anything by hand as part of running this skill. If a
row says a tree was not advanced, the correct response is to report that row, not to act on
it. Anything the script refuses is a decision for the human who reads the table.

## The tiers

| Tier | State | Mechanism | Default |
|---|---|---|---|
| 1 | clean tree, **no** local commits, behind | `git merge --ff-only @{u}` | **on** |
| 2 | clean tree, **has** local commits, behind | `git rebase @{u}` | needs `--rebase-local` |
| 3 | uncommitted changes, behind | `git stash push --include-untracked` → tier 1/2 → `git stash pop` | needs `--autostash` |

Tier 1 acts by default because it provably cannot lose work and because it is exactly the
click being replaced. `--ff-only` either fast-forwards or **refuses**: measured on git 2.43, a
diverged branch exits 128 and a locally-modified colliding file exits 1 with `Aborting`, and in
both cases nothing moved. That refusal is the second line of defence underneath the
preconditions.

A tree that is dirty **and** ahead needs **both** flags. Neither opt-in silently implies the
other.

## What is never touched

- **A gated branch.** `gated_branches` in the fleet project-registry config file is a hard human gate — a
  shared enterprise repo's `main` — and no flag overrides it. Note the distinction from
  `protected_branches`: protected means "never a deletion candidate" and says nothing about
  pulling, so `main` in a personal repo **is** advanced. Blocking protected branches too would
  make this skill a no-op, since `main`/`master` are always protected.
- **A tree with a live session in it.** See below.
- **A branch with no upstream.** An upstream is never inferred. `git worktree add -b` creates
  this shape routinely, and guessing `origin/<same-name>` would advance a local experiment
  toward a ref nobody chose.
- **A detached HEAD**, and any branch held by **two** trees at once (fast-forwarding a shared
  ref leaves the second tree's index describing a commit its files do not match).
- **Anything not behind.** An ahead-only tree is reported, never pushed. This skill has no
  `push` code path at all, and a test asserts that from the AST.

## Liveness: what "live" means here, and why it is not the review sweep's rule

<!-- The sibling skill is deliberately named WITHOUT a leading slash throughout this file:
     a `/<name>` mention is what the LOCKED_SKILLS gate reads as "this skill invokes that
     one", and that would break the sibling's own lock. Nothing here invokes it. -->

The review sweep's veto counts **any** process whose cwd is in the tree, because deleting a
directory under an open shell is unrecoverable. That rule is wrong for advancing a ref, and
measurably so: on this machine it reported **every repo main tree live**, because VS Code
integrated terminals sit at the repo root — permanently disabling the most important target.

<!-- The illustrative process counts in this section were genericized for public release. -->

So this sweep makes two refinements, and reuses the probe itself unchanged:

1. **Attribution.** Linked worktrees live physically *below* the repo root, so a pid in a
   worktree made the main tree look live. Example: 12 pids on the main tree, 9 of them
   belonging to nested worktrees. A main tree's pids have every deeper worktree's pids
   subtracted.
2. **The idle-shell discount.** A process is discounted **only** when it is a known shell
   binary whose argv contains nothing but interactive flags (`-i`, `-l`, `--login`, `-s`,
   `--init-file X`, `--rcfile X`). A `-c` command, a script argument, or any non-shell binary
   counts as live.

**Testing for an agent by process name was measured WRONG** and must not be reintroduced: the
worktree of a live *sub-agent* holds no `claude` process at all — its CC process lives in the
parent session's worktree — so a name test declares an actively-worked tree idle.

**The probe fails closed three ways:** a degraded probe (`probe_ok` false) skips the tree, an
unreadable process counts as live, and an unrecognised argv shape counts as live. Every
discount that authorised an advance is printed in that row's reason, so an advance is never
justified by something the reader cannot see.

## One fetch per repo, never per worktree

Worktrees share `--git-common-dir`, so one `git fetch` refreshes the remote-tracking refs every
worktree reads — measured: a fetch run only in the main tree moved a linked worktree's
behind-count from 0 to 1 and it then fast-forwarded. `git pull` is therefore never used: it
would fetch once per worktree, it cannot be split for a preview, and its exit codes collide
"no upstream" with "your local changes would be overwritten".

A branch tracking a remote other than `origin` is handled by fetching each distinct remote the
repo's targets actually track — never by assuming `origin`.

## A no-op run writes nothing

Idempotent means **no-op**, not convergent. Measured on git 2.43:

- an already-up-to-date `git merge --ff-only` writes **nothing at all**;
- `git status --porcelain` normally **replaces `.git/index`** (inode changes, mtime bumps) to
  refresh its stat cache, so the sweep runs every git command with `GIT_OPTIONAL_LOCKS=0`,
  under which the same command leaves inode and mtime untouched;
- the only path a no-op run still stamps is `.git/FETCH_HEAD`, which `git fetch` truncates in
  place every time. Nothing in this fleet reads or watches it.
- `--dry-run --no-fetch` writes **nothing whatsoever**.

The script writes no file of its own — no report, no cache, no state, no lock.

## Reporting

Report the table as it was printed, including the rows that were **not** advanced and their
reasons. A sweep summarised as "all synced" while five trees were skipped is worse than no
sweep. If the sweep bounded its work — a repo it could not list, a fetch that failed, a
degraded probe — say so as its own line.

Design and the full decision record: the pull-git sync-sweep design doc.
