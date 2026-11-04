---
title: "Testing Claude Agent skills that ship Python CLI code: coverage, reusable harnesses, subprocess lifecycle, leak detection, and Anthropic practice"
category: research
tags: [research, testing, ci, coverage, ratchet, baseline, skills, python, cli, pytest, subprocess, resource-leaks, xdist]
status: complete
source: "claude-adhoc-2026-07-09"
template_version: "research-1.2.0"
related_docs:
  - docs/research/python-skill-test-coverage-enforcement.md
---

# Testing Claude Agent skills that ship Python CLI code: coverage, harnesses, and resource lifecycle

**Status:** complete

**Created:** 2026-07-09

<!-- This doc supersedes/consolidates docs/research/python-skill-test-coverage-enforcement.md
     (the earlier single-question ratchet note). -->

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. Enforcing per-skill test coverage: the ratchet/baseline pattern](#1-enforcing-per-skill-test-coverage--the-ratchetbaseline-pattern)
  - [When to use](#when-to-use)
- [2. How to test Python CLI tools](#2-how-to-test-python-cli-tools)
  - [When to use](#when-to-use-2)
- [3. Reducing test redundancy via abstraction: a reusable test harness](#3-reducing-test-redundancy-via-abstraction--a-reusable-test-harness)
  - [When to use](#when-to-use-3)
- [4. How Anthropic tests its own official skills](#4-how-anthropic-tests-its-own-official-skills)
  - [When to use](#when-to-use-4)
- [5. Test-subprocess lifecycle management and automated leak detection](#5-test-subprocess-lifecycle-management-and-automated-leak-detection)
  - [Incident boundary: alive at assertion, dead at teardown](#incident-boundary-alive-at-assertion-dead-at-teardown)
  - [Lifecycle patterns and what they actually guarantee](#lifecycle-patterns-and-what-they-actually-guarantee)
  - [Off-the-shelf leak-detection landscape](#off-the-shelf-leak-detection-landscape)
  - [pytest-xdist: guard inside every worker](#pytest-xdist-guard-inside-every-worker)
  - [Restricted and sandboxed environments](#restricted-and-sandboxed-environments)
  - [Fleet distribution: plugin over copied implementation](#fleet-distribution-plugin-over-copied-implementation)
  - [Beyond processes: one ledger, specialized adapters](#beyond-processes-one-ledger-specialized-adapters)
  - [Gaps, blindspots & emergent findings](#gaps-blindspots--emergent-findings)
  - [When to use](#when-to-use-5)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

<!-- doc:region name="context" kind="immutable" -->

## Context

PR #198 wires the repo's existing Python skill tests into CI, but the gate only runs
tests that already exist: a skill can ship runtime Python (a CLI script) with zero
tests and CI stays green. The research question has four parts: (1) how do mature
teams *force* every Python-shipping unit to carry tests without a big-bang catch-up;
(2) what is the correct technique for testing Python CLI tools; (3) how do you keep
many near-identical per-skill test files DRY via a reusable harness; and (4) how does
Anthropic itself validate the official skills it ships (which this repo vendors). The
output must also propose a concrete, reusable test framework for this repo's skills.

**Primary period:** 2024–2026
**Source weighting:** 2026/2025 primary (current tool docs, live repos); 2024 secondary; the ratchet/boy-scout lineage is foundational background and cited as such.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

Primary focus: the current (2026-07) documentation of the named tools (coverage.py,
pytest, Click, pytest-mock, pytest-subprocess, betterer, mypy-baseline) and the live
`anthropics/skills` `main` branch. The "coverage ratchet / budgeted-violations"
pattern itself is older (the boy-scout rule, warning budgets, SonarQube new-code
gates) and is cited as foundational background rather than a recent development. No
significant post-2024 *conceptual* change to the ratchet pattern was found. The
novelty is entirely in tooling maturity, so recency weighting is applied to tool
versions, not to the idea.

**2026-07-26 refine scope:** §5 adds subprocess ownership, parallel-worker leak
detection, restricted-environment behavior, and adjacent resource leaks. Current
2026 Python/pytest/psutil documentation and 2025–2026 plugin releases are weighted
first. POSIX session/process-group semantics are older but foundational and remain
current. For generic pytest child-process leak detection and cross-platform
containment, no significant post-2024 development was found; that evidence gap is
reported rather than backfilled.

## Executive Summary

1. Every mature "add a quality gate to a codebase with existing gaps" tool uses the
   same shape: snapshot current violations as a **baseline**, fail only on **new**
   violations, and require the baseline to **monotonically shrink** (the boy-scout
   rule made mechanical). betterer, mypy-baseline, jest-ratchet, ESLint
   `--max-warnings`, and coverage.py `--fail-under` are all instances of it. For this
   repo's per-skill-directory layout, a **structural existence ratchet** (a
   `KNOWN_UNTESTED_SKILLS` allow-list that can only shrink) is simpler and more robust
   than coverage-% introspection, and belongs as a follow-up to #198.
2. Test Python CLIs by **separating logic from arg-parsing and testing the functions
   directly**, then adding a *thin* CLI-contract layer that asserts exit codes,
   stdout/stderr, and argparse validation (bad args → exit 2). Mock external
   boundaries (DB/HTTP) with `unittest.mock` / pytest-mock / pytest-subprocess; never
   hit live services. Prefer import-and-call (or Click's `CliRunner`) over subprocess
   for logic; reserve subprocess for the real exit-code contract.
3. Keep the many per-skill test files DRY with **conftest.py shared fixtures +
   `@pytest.mark.parametrize` + a reusable CLI harness** (a helper that runs a skill's
   `scripts/*.py` with argv and asserts the exit-code/stdout/stderr/JSON contract, plus
   a temp-portable-workspace fixture). Prefer composition (fixtures/helpers) over a deep
   `BaseToolTests` inheritance tree; use a thin base class only for the truly-universal
   contract checks.
4. Anthropic's `anthropics/skills` repo ships 72 Python files but **no conventional
   pytest unit tests** for them; the doc-creation skills are **source-available, not
   open source**, and skills are validated behaviorally through the **skill-creator
   trigger-eval harness** (`skill-creator/scripts/run_eval.py` + grader/analyzer/
   comparator sub-agents), not unit tests. Excluding the vendored skills from our
   unit-test requirement is therefore correct; unit-testing *our own* domain Python is
   more rigorous than upstream and is the right call.
5. A test that must prove a process **survives the assertion** still owns that process
   after the assertion. Make ownership structural: launch it through a fixture/factory
   that registers cleanup before returning, isolate it in a process group/session,
   and terminate → bounded-wait → kill → reap that group during teardown. Run a
   per-item detector and a per-worker session backstop inside every xdist worker, but
   treat them as audit controls rather than the primary cleanup path. A worker crash
   bypasses Python finalizers, so CI also needs an outer cgroup (Linux) or Job Object
   (Windows) containment boundary. No maintained general-purpose pytest plugin was
   found that supplies this complete guarantee. [INFERENCE]

## 1. Enforcing per-skill test coverage: the ratchet/baseline pattern

The industry-standard answer to "hold new work to a standard the existing codebase
does not yet meet" is a **ratchet**: snapshot the current violations as a baseline,
fail CI only on *new* violations, and require the baseline to shrink over time. This
is the boy-scout rule ("leave the campground cleaner than you found it") made
mechanical: you adopt the gate today, without a big-bang catch-up, while new work is
held to the target.

The same shape recurs across ecosystems:

- **betterer** (JS/TS) is built entirely on this idea. Its stated purpose is to
  "Prevent regressions … Track the current status and prevent creating new issues"
  and to "Improve, incrementally" [VERIFIABLE][^3]. It records known violations to a
  results snapshot; a run that gets worse fails, a run that improves updates the
  snapshot.
- **mypy-baseline** (Python) makes the mechanism explicit: on first run "it will
  remember all type errors that you already have in the project (generate 'baseline').
  All consecutive runs will ignore these errors and report only ones that you
  introduced after that" [VERIFIABLE][^4]. Its documented philosophy is not to hide
  errors forever but to drive the count to zero.
- **jest-ratchet / jest-coverage-ratchet** (JS) are the auto-raising-threshold
  variant: jest-coverage-ratchet "Ensures code coverage only increases"
  [VERIFIABLE][^5], and jest-ratchet's tagline is "keep test coverage going only one
  direction -- up" [VERIFIABLE][^6].
- **ESLint `--max-warnings`** is the numeric-budget variant: "if `--max-warnings` is
  specified and the total warning count is greater than the specified threshold,
  ESLint exits with an error status" [VERIFIABLE][^7]: freeze the current warning
  count as the budget, then ratchet it down.
- **coverage.py `--fail-under` / `[report] fail_under`** is the coverage-percentage
  floor: "If the total coverage measurement is under this value, then exit with a
  status code of 2" [VERIFIABLE][^1].
- **golangci-lint** offers a `new-from-rev` / new-issues mode (only report issues
  introduced since a git revision): the same "gate new code, grandfather old code"
  idea in the Go ecosystem [HEURISTIC]. (Cited from memory; the golangci-lint docs
  site is a JS-rendered SPA that returned no clean text this session. See the failure
  taxonomy. Not upgraded to VERIFIABLE.)

Two enforcement **layers** exist and answer different questions [INFERENCE, from the
tool set above]:

1. **Existence ratchet**: "does every Python-shipping unit have *any* tests?"
   (structural). This is what our question is really asking.
2. **Coverage-% ratchet**: "for units that already have tests, is line/branch
   coverage not regressing?" (`--fail-under`, jest-ratchet). Heavier; a good second
   phase, not the first.

For *detecting untested modules specifically*, the surveyed techniques are coverage.py
`--source` (untested files surface at 0%) or a dynamic `inspect`-based test that
enumerates modules and asserts a corresponding test exists. For a layout of
independent skill-script directories (not one importable package), a **structural
directory check is simpler and more robust than coverage introspection** [INFERENCE]:
the skill dirs are deliberately *not* a package namespace (several skills ship
same-named modules like `preflight.py`), so a coverage run over the whole tree cannot
even import them together: the repo's own `run-python-tests.sh` runs each skill's
tests in its *own* pytest process for exactly this reason.

> **Contrarian view (recorded from the skeptic/contrarian critic pass):** a
> coverage-% ratchet can be *gamed*: a contributor writes a trivial test that
> imports the module and asserts nothing, clearing the gate while testing nothing. An
> existence ratchet is even easier to game this way. The mitigation is orthogonal to
> the ratchet: the spec-first + mechanical-check rule (§2) is what makes a test
> meaningful; the ratchet only guarantees a test *file* exists. Treat the ratchet as a
> backlog-management device, not a quality guarantee.

### When to use

- **Existence ratchet:** when you have many independent units (skills, packages,
  services), an established gap, and want to force new/changed units to carry tests
  without blocking CI on today's backlog. The `KNOWN_UNTESTED_SKILLS` allow-list *is*
  the visible, shrinking backlog. This is the right first move for this repo.
- **Coverage-% ratchet (`--fail-under`, jest-ratchet):** layer it *after* the
  existence gap is closed, once units reliably have tests and you want to prevent
  line/branch regressions. Global `--fail-under` is blunt (one number for the whole
  tree); per-file or per-package thresholds catch "new file drags the average down but
  stays above the global floor."
- **Avoid** a coverage ratchet as the *first* gate on a young/churning codebase: the
  threshold thrash (files added/removed swinging the global %) generates noise before
  it generates value.

## 2. How to test Python CLI tools

The consensus technique is a **two-layer split**:

**Layer 1: logic, tested directly.** Factor the real work into plain functions that
take arguments and return values (no `argparse`, no `print`, no `sys.exit` inside
them), and unit-test those functions in-memory. This is fast, deterministic, and where
the bulk of assertions live. The repo's existing `cld-reagent-agent` tests are exactly
this: they import `reagent_agent` and call `_read_reagents_csv(...)` /
`_update_chem_lib(...)` directly against a `tmp_path` fixture, asserting the returned
dicts (no subprocess).

**Layer 2: the CLI contract, tested thinly.** A small number of tests assert the
*command-line behavior*: exit codes, stdout/stderr, and argparse validation. Two idioms:

- **Import-and-call / framework runner** for most contract checks. Click ships a
  dedicated helper: "Click provides the `click.testing` module to help you invoke
  command line applications and check their behavior" [VERIFIABLE][^10], via
  `CliRunner().invoke(cmd, [...])` which captures output and the exit code. argparse
  CLIs have no built-in runner but can be driven by calling `main(argv)` and catching
  `SystemExit`.
- **Subprocess** for the true end-to-end exit-code/stream contract when import-and-call
  can't capture it faithfully.

**Assert exit codes as first-class behavior.** Both the stdlib and the coverage tool
treat exit codes as contract: argparse's `error()` "terminates the program with a
status code of 2" [VERIFIABLE][^2], so a "bad args → exit 2" assertion is a real,
stable check. The repo already does the domain equivalent: `cld-reagent-agent` asserts
`SystemExit(3)` for a missing SMILES column.

**Mock external boundaries; never hit live services.** DB (Athena/GOSTAR), HTTP
inference (Triton), and subprocess calls are mocked at the boundary:

- **pytest-mock** provides "a `mocker` fixture which is a thin-wrapper around the
  patching API provided by the mock package" [VERIFIABLE][^11]: the ergonomic way to
  patch without nested `with` blocks.
- **pytest-subprocess** is "a pytest plugin to fake the real subprocess behavior to
  make your tests more independent"; its `fake_process`/`fp` fixture lets you "register
  commands and specify their behavior before they will be executed. This will prevent a
  real subprocess execution" [VERIFIABLE][^12]: ideal for skills that shell out
  (rdkit, chemserver).

**Spec-first + the mechanical check.** Per house rules, tests derive from the skill's
documented contract, not its implementation, and **every test must fail if the function
body is replaced with `pass` / `return None`**: a bare `assert result` is
insufficient. Include ≥1 failure-path test per public function.

### When to use

- **Direct function tests (Layer 1):** the default and the majority of every skill's
  tests: any logic that transforms data (parsing, projection, minting, chemistry).
  Fast, no mocking of your own code.
- **`CliRunner` / import-and-call `main(argv)` (Layer 2a):** when you need to assert
  the *parsed* behavior (flag handling, help text, exit code) without process
  overhead. Use `CliRunner` for Click CLIs; catch `SystemExit` for argparse CLIs.
- **Subprocess (Layer 2b):** only for the genuine end-to-end contract (the script as an
  executable) or when the script mutates process-global state that import-and-call
  can't isolate. Slower; keep to a few per skill.
- **pytest-mock vs pytest-subprocess:** `mocker.patch` for in-process boundaries
  (functions, clients, `os.remove`); pytest-subprocess when the code path actually
  calls `subprocess.run`/`Popen` and you want to fake the external command.

## 3. Reducing test redundancy via abstraction: a reusable test harness

With one test file per skill and a shared CLI contract (every skill: `--help` works,
bad args → exit 2, JSON output validates), most of the boilerplate is identical. Three
DRY mechanisms, in increasing coupling:

**Shared fixtures in `conftest.py` (composition, preferred).** pytest auto-discovers
`conftest.py` fixtures for every test in the directory tree, so a fixture defined once
is available everywhere without imports. Fixtures carry a **scope**: "Possible values
for scope are: `function`, `class`, `module`, `package` or `session`"
[VERIFIABLE][^8], so expensive setup (a seeded temp workspace, a loaded schema) can
be built once per module/session instead of per test. The repo already uses this: the
`cld_agent` suite's `conftest.py` has an `autouse` fixture resetting logging state
between cases. A **reusable CLI harness** fits here naturally as a fixture: a
`run_skill(script, argv, cwd)` helper returning `(exit_code, stdout, stderr)`, plus a
`portable_workspace` `tmp_path`-based fixture seeding the portable workspace template.

**Parametrization for table-driven cases.** "The builtin `pytest.mark.parametrize`
decorator enables parametrization of arguments for a test function" [VERIFIABLE][^9]:
one test body, N input/expected rows. This is how the "every skill: `--help` works /
bad args → exit 2" matrix should be written: parametrize over the discovered skill
scripts rather than copy-pasting a test per skill.

**Base/abstract test classes (`BaseToolTests`, inheritance, use sparingly).** The
xUnit "subclass-per-tool" pattern: an abstract base defines the universal contract
tests and each tool subclasses it, overriding a couple of hooks (the script path, a
sample-args provider). This concentrates the contract in one place, but a deep
inheritance tree is brittle: pytest fixtures already give most of the reuse with less
coupling, and the pytest community broadly favors fixtures/composition over test-class
inheritance [HEURISTIC]. Use a *thin* base only for the handful of truly-universal
checks; put everything skill-specific in fixtures + parametrization.

> **Adjacent-field note (adjacent critic pass):** the "one base class per subject
> under test" idea comes from xUnit/JUnit's class-based world. Porting it wholesale to
> pytest fights the framework: pytest's fixture system is the idiomatic reuse
> mechanism and composes better than inheritance (a fixture can depend on other
> fixtures; a base class can't easily mix in two independent behaviors). Reach for a
> base class only when the shared behavior is genuinely an "is-a" contract every
> subject must satisfy identically.

### When to use

- **conftest fixtures + a `run_skill` harness:** always. This is the backbone. Scope
  the workspace/seed fixtures to `module` or `session` when they're expensive and
  read-only; keep them `function`-scoped when a test mutates them.
- **`@pytest.mark.parametrize`:** for any "same assertion, many inputs" set: the
  universal per-skill contract matrix, input/expected data tables, and failure-mode
  tables.
- **`BaseToolTests` base class:** only for the small, identical, mandatory contract
  (help works, bad-args exit code, JSON-schema validity) shared by *every* CLI skill.
  Do **not** model skill-specific domain logic as base-class methods. That belongs in
  the skill's own test module.

## 4. How Anthropic tests its own official skills

The `anthropics/skills` repo (the source of this repo's vendored `docx`/`pdf`/`pptx`/
`xlsx`/`mcp-builder`/`skill-creator` skills) is described as "a Public repository for
Agent Skills." Its licensing is **split**: "Many skills in this repo are open source
(Apache 2.0)" [VERIFIABLE][^13] (e.g. `algorithmic-art/LICENSE.txt` is a verbatim
Apache License 2.0 [VERIFIABLE][^16]), while the document-creation skills that power
Claude's file capabilities "are source-available, not open source" [VERIFIABLE][^13].
The `docx` skill's own `SKILL.md` frontmatter carries `license: Proprietary. LICENSE.txt
has complete terms` [VERIFIABLE][^14].

These skills **do ship Python**: the repo contains 72 `.py` files (e.g.
`docx/scripts/accept_changes.py`, `docx/scripts/office/unpack.py`), but a recursive
tree walk of `main` finds **no conventional pytest unit tests** for that Python: the
only `test`-named Python files are the `webapp-testing` skill's *examples*
(`console_logging.py`, `element_discovery.py`), not a test suite, and there is **no
`evals.json`** anywhere in the repo [INFERENCE, from the full recursive tree listing
fetched this session: an absence claim, so stated as "none found in `main` as of
2026-07-09" rather than a universal absence].

Instead, Anthropic validates skills **behaviorally**, through the **skill-creator eval
harness**. `skill-creator/scripts/run_eval.py` is documented as: "Run trigger
evaluation for a skill description. Tests whether a skill's description causes Claude to
trigger (read the skill) for a set of queries. Outputs results as JSON"
[VERIFIABLE][^15]. It spawns a fresh `claude -p` subprocess per query and detects
triggering from stream events. The surrounding framework (`run_loop.py`,
`aggregate_benchmark.py`, `generate_report.py`, `quick_validate.py`, plus
`agents/grader.md`, `agents/analyzer.md`, `agents/comparator.md` and an `eval-viewer/`)
is an ISSUE-REF, subagent-per-case behavioral eval, not a unit-test suite.

> **Correction of a prior working note:** an earlier hand-off characterized this as
> "`evals/evals.json`, subagent-per-test-case." The subagent-per-case shape is right,
> but there is **no `evals.json`** in the repo; the actual entry point is
> `skill-creator/scripts/run_eval.py` (which takes an in-memory `eval_set` list). The
> `mcp-builder` skill separately ships `scripts/evaluation.py` for evaluating generated
> MCP servers: a second, skill-specific eval, still behavioral, still not pytest units.

**Implication for this repo:** (1) excluding the vendored Anthropic skills from a
unit-test *requirement* is correct: they are external, and even upstream does not
unit-test their scripts; baseline them with a note. (2) Unit-testing *our own* CLD
skills' Python (real domain logic: minting, chemistry, export reconciliation) is *more*
rigorous than eval-only and is the right call for code we own and must not silently
break. The two approaches are complementary, not competing: behavioral trigger-evals
answer "does the model use the skill correctly," unit tests answer "does the skill's
code compute the right answer."

### When to use

- **Behavioral trigger-eval (the Anthropic pattern):** when the risk is *description/
  prompt quality*: will Claude invoke this skill for the right queries, and does the
  end-to-end behavior look right. Best for skills whose "logic" is mostly prompt +
  orchestration. This repo already has an analog in its e2e scenario framework.
- **Unit tests (our pattern for owned code):** when the skill ships deterministic
  domain logic with a checkable right answer (chemistry math, ID minting, CSV→JSON
  projection). Cheaper, faster, and catches regressions an ISSUE-REF eval would miss
  or flakily pass.
- **Baseline-only (neither required):** vendored/third-party skills we did not author
  and do not modify: track them on the `KNOWN_UNTESTED_SKILLS` list with a note that
  upstream validates them via evals, and revisit only if we fork their Python.

## 5. Test-subprocess lifecycle management and automated leak detection

> **Refine addition (2026-07-26):** This entire section, the lifecycle recommendation
> in §Recommendation, Open Questions 6–9, sources [^17]–[^46], the refine-prompt
> appendix entry, and the 2026-07-26 Run History entry were added by this pass.

### Incident boundary: alive at assertion, dead at teardown

The motivating test deliberately starts a long-lived fake application-server process
whose command line must remain a genuine `pkill` match, proves that the script under
test did **not** kill it, and then leaves it running. The established incident count
was 148 orphans across more than ten pytest runs, with the oldest over two days old;
the fake is a Bash `while true; do sleep 1; done`, so killing only the shell PID does
not reliably dispose of its child. These are internal incident facts supplied by the
refine brief, not independently public evidence. [NO SOURCE]

The survival assertion and teardown are not in conflict. Pytest's yield-fixture
protocol runs the post-`yield` block after the test finishes, including when the test
fails, and finalizers are unwound in reverse order; therefore the process can be alive
at assertion time and be unconditionally disposed of immediately afterward.
[VERIFIABLE][^20] The correct invariant is:

```text
spawn + register ownership
          |
          v
test call: process MUST be alive  ---- assertion fails? ----+
          |                                                  |
          +---------------- item teardown begins <-----------+
                              |
                              v
                 TERM owned group -> bounded wait
                              |
                        still alive?
                         /       \
                       no         yes
                       |           |
                       |     KILL owned group
                       |           |
                       +----- wait/reap direct child
                                      |
                                      v
                           close streams; report cleanup error
```

This test should stay. Its missing operation is not another assertion; it is an
ownership finalizer registered by the same abstraction that creates the process.
[INFERENCE]

### Lifecycle patterns and what they actually guarantee

On POSIX, `start_new_session=True` makes the child call `setsid()`, while the separate
`process_group` option calls `setpgid()`; Python recommends those supported parameters
instead of using unsafe `preexec_fn` hooks for this purpose. A successful `setsid()`
makes the caller both session leader and process-group leader, with its PID as the new
process-group ID. [VERIFIABLE][^17][^19] `os.killpg()` then addresses the entire
process group, whereas `Popen.terminate()` and `Popen.kill()` signal only the process
represented by that `Popen` object. With `shell=True`, that PID is the spawned shell,
not necessarily the program beneath it. [VERIFIABLE][^17][^18]

Those semantics make this the minimum safe POSIX pattern for a deliberately
long-lived test child. [HEURISTIC]

```python
# Illustrative harness API; not drop-in production code.
@pytest.fixture
def owned_processes(request):
    manager = OwnedProcessManager()
    request.addfinalizer(manager.close)  # manager owns every successful spawn
    return manager


def test_script_does_not_kill_a_sibling_app_server_process(
    owned_processes,
):
    sibling = owned_processes.spawn(
        ["bash", "-c", "while true; do sleep 1; done"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    run_script_under_test()

    assert sibling.poll() is None  # the behavior under test
    # Teardown now TERM-waits-KILL-waits the sibling's process group.
```

`OwnedProcessManager.spawn()` must record the handle and immutable identity needed for
cleanup before returning it to test code. On close it should signal the recorded
group, wait for a short bounded grace period, escalate to `SIGKILL`, call `wait()` on
the direct `Popen`, and close owned streams. It must also reject a recorded PGID equal
to the worker's own process group before signaling. That ordering converts
best-effort cleanup into an enforceable ownership protocol. [HEURISTIC]

Several patterns that look sufficient are not:

| Pattern | What it guarantees | Why it is insufficient alone |
|---|---|---|
| `try/finally` in each test | Cleanup runs on ordinary control flow if every author remembers it. [HEURISTIC] | The omission itself is the recurring footgun; it is not structural. |
| `with subprocess.Popen(...) as proc` | On context exit Python closes standard streams and waits for the child. [VERIFIABLE][^17] | It does **not** terminate a long-lived child before waiting, so this fake can hang the suite. |
| `proc.terminate(); proc.wait()` | Terminates and reaps the direct child. [VERIFIABLE][^17] | It does not address a shell's children or other descendants in the group. |
| `start_new_session=True` only | Gives the owned tree a separate session/group address. [VERIFIABLE][^17][^19] | A process group is not self-cleaning; teardown must still signal and reap it. |
| `psutil.Process(pid).children(recursive=True)` cleanup | Can enumerate a currently connected descendant tree and supports TERM/grace/KILL with `wait_procs`. [VERIFIABLE][^21] | If an intermediate process exits, psutil documents that its surviving child may no longer be discoverable through the original parent. |
| fixture/factory + owned group | Keeps the child alive through the assertion and automatically cleans the group after it. [INFERENCE] | Python finalizers still do not run after `SIGKILL`, interpreter crash, or abrupt worker loss. |

Using `PIPE` for an unbounded producer adds a second failure mode: Python warns that
waiting with unread pipe output can deadlock when the OS pipe buffer fills, and
recommends `communicate()` for finite output. A silent infinite fake should therefore
use `DEVNULL`; a finite child whose output matters should use
`communicate(timeout=...)`, followed by kill and another `communicate()` on timeout.
[VERIFIABLE][^17]

`psutil` is useful as a detector and cleanup fallback, not as the ownership primitive.
Its `Popen` wrapper checks PID reuse before signaling, and its APIs surface
`NoSuchProcess`, `ZombieProcess`, and `AccessDenied`, but a live process tree is
inherently racy and a detached descendant can break ancestry. [VERIFIABLE][^21][^22]
Record handles and containment identity at creation; enumerate later only to find
violations of that contract. [INFERENCE]

### Off-the-shelf leak-detection landscape

Multiple searches of pytest's plugin index, PyPI, GitHub, and current pytest
documentation using “child process leak,” “orphan process,” “resource leak,”
“pytest-xdist process cleanup,” and adjacent framings found **no maintained,
general-purpose pytest plugin that fails a run on leaked descendant processes and
also documents xdist behavior**. The official plugin list is explicitly an automated,
uncurated index rather than an endorsement, so search presence is not a quality
signal. [VERIFIABLE][^27] The non-existence conclusion cannot be proved from a
registry search and is therefore reported as an evidence absence. [NO SOURCE]

The closest tools solve narrower problems:

| Tool | Current evidence | Evaluation for this fleet |
|---|---|---|
| `pytest-xprocess` | It manages external processes and its fixture API exposes termination of the managed process tree. Its latest PyPI release is 1.0.2 from 2024-05-19. [VERIFIABLE][^25][^26] | Good opt-in manager for services launched through it; not a generic detector for raw `Popen` calls. [INFERENCE] No documented xdist guarantee was found, and no significant post-2024 development was located. [NO SOURCE] |
| `pytest-subprocess` | It fakes subprocess behavior so tests need not execute real children; PyPI shows release 1.6.0 in May 2026. [VERIFIABLE][^12][^36] | Best structural prevention when the OS process is not part of the contract. It cannot replace this incident's genuine command-line match because that reality is what the test exercises. [INFERENCE] |
| Pytest's internal `pytester --lsof` guard | The disabled-by-default pytester plugin snapshots `lsof` output for the current PID around the full `pytest_runtest_protocol` and emits `PytestFDWarning` for new descriptors. [VERIFIABLE][^46] | It is first-party prior art for after-teardown FD deltas, but it is support code for testing pytest/plugins, requires the external `lsof` executable, warns rather than fails directly, and is not a process/thread/unified guard. [VERIFIABLE][^46] No documented xdist support was found. [NO SOURCE] |
| `pytest-openfiles` | It compares `psutil.Process().open_files()` snapshots and can fail on files left open, but the repository is archived and directs users toward `ResourceWarning`; its last release was 0.6.0 in 2024. [VERIFIABLE][^28][^29] | Do not fleet-standardize an archived plugin. It covers regular files, not a unified process/thread/socket lifecycle, and its source does not convert `AccessDenied` into an explicit degraded mode. [INFERENCE] |
| `pytest-threadleak` | Version 0.5.0 was released in 2022; its hook snapshots threads around `pytest_runtest_call` and fails on survivors. [VERIFIABLE][^30][^31] | It is stale and call-phase-only, so it does not prove that fixture teardown left the worker clean. [INFERENCE] No documented xdist guarantee was found. [NO SOURCE] |
| `pytest-leaks` | Despite its broad name, it repeatedly runs tests and checks CPython reference counts, requiring a debug Python build; its latest PyPI release is from 2019. [VERIFIABLE][^32] | It is a memory/reference-leak tool, not child-process, descriptor, or thread enforcement. [INFERENCE] |
| `pyleak` | The 0.2.0 release from 2026 detects asyncio task leaks, thread leaks, and event-loop blocking. Its pytest plugin is marker-driven and wraps the test call phase. [VERIFIABLE][^33][^34] | It is a promising emerging specialized guard, not a process/FD guard. [INFERENCE] Pilot it for async-heavy repos. [HEURISTIC] No xdist guarantee was documented in the fetched materials. [NO SOURCE] |
| `pytest-timeout` | Its thread mode can terminate the entire pytest process without normal teardown or JUnit reporting; its signal mode can also leave later fixture teardowns unexecuted when teardown itself times out. [VERIFIABLE][^35] | Hang containment, not descendant cleanup. Abruptly losing a worker can make orphan leakage more likely unless an outer supervisor owns the worker tree. [INFERENCE] |

No reliable cross-project adoption numbers were located for these plugins. PyPI
release dates and repository stars can indicate activity, but they do not establish
production adoption or correctness under xdist. [NO SOURCE]

### pytest-xdist: guard inside every worker

xdist's controller creates one or more worker processes, and each worker is a
mini-pytest runner that performs its own collection and test execution.
[VERIFIABLE][^23] A path containing `popen-gw5` therefore identifies resources
created in worker `gw5`; controller-only teardown is at the wrong ownership boundary.
[INFERENCE]

The correct layout is two guards **inside each worker**:

1. A function/item guard owns and attributes resources to one test, then audits only
   after that item's fixture teardown has completed. It reports the node ID, worker
   ID, PID/PGID, command, age, and cleanup outcome. [HEURISTIC]
2. A worker-session backstop snapshots the worker's initial state and checks again at
   worker-session finish. It catches unattributed leftovers, but its report has
   weaker test attribution. [HEURISTIC]

Pytest-xdist explicitly documents that a session-scoped fixture runs once **per
worker**, not once across the entire distributed run. It also exposes `worker_id` and
`PYTEST_XDIST_WORKER`, which should be attached to every ownership record and report.
[VERIFIABLE][^24] A controller plugin may aggregate reports, but it cannot replace
the worker-local acquisition and teardown hooks. [INFERENCE]

Never enumerate or kill all processes matching the repository name, command string,
or CI user. Parallel workers legitimately overlap, so global matching can make one
worker accuse or kill another worker's resource—the exact sibling-safety property the
motivating test protects. Restrict mutation to registered resources or an exact
worker-owned containment boundary; use descendant snapshots only for detection.
[HEURISTIC]

A pure descendant delta has two attribution traps. A long-lived session fixture is
legitimate across item boundaries and would appear as a false positive unless it is
registered with session scope. Conversely, snapshotting only after fixture setup
misses leaks created during setup. A scope-aware ownership ledger is therefore more
reliable than “children before versus after test call”; the session backstop covers
unregistered setup leakage. [INFERENCE]

### Restricted and sandboxed environments

Process observation is a capability, not a certainty. psutil documents that
`AccessDenied` is expected when the OS denies access to process information, and that
callers must handle process disappearance races. [VERIFIABLE][^22] The motivating
environment also established that `ps -ef` can raise `PermissionError` inside the
Codex sandbox. That local behavior was supplied by the brief and was not independently
published. [NO SOURCE]

Pytester's internal `--lsof` guard treats an unavailable or failing `lsof` capability
probe as a platform mismatch and simply does not register its checker.
[VERIFIABLE][^46] That is a sensible default for an optional internal development
aid, but it is not acceptable for a fleet policy claiming leak enforcement because
the loss of assurance is invisible. [INFERENCE]

The guard should separate **control** from **observation**:

- Registered cleanup should still signal the saved process group or close the saved
  platform containment handle; it does not need a system-wide process listing.
  Failure to clean an owned resource is a test/infrastructure failure, not a skip.
  [HEURISTIC]
- Observational enumeration should run a capability probe once per worker. In
  `required` mode (trusted CI), inability to observe is an infrastructure failure
  because the advertised guarantee was not established. In `best-effort` mode
  (documented restricted sandboxes), emit one prominent warning plus a terminal
  summary and continue with registered cleanup. `off` should require explicit
  configuration. [HEURISTIC]
- Never interpret `PermissionError`/`AccessDenied` as “zero leaked children.” That
  produces a green result from missing evidence. [INFERENCE]

Silent skipping is the worst degradation because it preserves the appearance of
enforcement. Unconditionally failing is also wrong for local sandboxes whose security
model intentionally hides unrelated processes. A named mode, visible summary, and CI
policy that requires full observation make the loss of assurance explicit.
[HEURISTIC]

### Fleet distribution: plugin over copied implementation

A root `conftest.py` can deploy the first fix quickly, and pytest automatically makes
its fixtures/hooks available below that directory. But conftest implementations are
repo-local and copier propagation creates many mutable copies. Pytest's supported
distribution mechanism for reusable third-party/internal plugins is an installable
package advertised through the `pytest11` entry point. [VERIFIABLE][^37]

For a multi-repo fleet, the steady-state design should be a **versioned internal
pytest plugin package. [HEURISTIC]

- The package owns `OwnedProcessManager`, platform adapters, item/session audit hooks,
  reporting, and its own xdist/crash/restriction tests.
- Copier-propagated repo config owns only a compatible version pin and policy such as
  `process_leaks = required|best-effort|off`.
- Fleet configuration lists the plugin in pytest's `required_plugins`, which causes
  startup to fail when it is absent or version-incompatible. If entry-point
  autoloading is disabled with `PYTEST_DISABLE_PLUGIN_AUTOLOAD`, load it explicitly
  with `-p`; pytest documents both mechanisms. [VERIFIABLE][^38]
- A small shared `conftest.py` shim is acceptable during rollout, but the
  implementation should not remain copied once the package is available.
  [HEURISTIC]

This keeps policy locally configurable while implementation, fixes, rollback, and
compatibility tests have one release stream. It also prevents an old copied guard
from silently claiming the same guarantee as a newer one. [INFERENCE]

### Beyond processes: one ledger, specialized adapters

Test suites classically leak more than processes: regular-file descriptors and
sockets, network listeners, threads and executors, asyncio tasks, temporary
directories, containers, and remote service/database state. They do not share a safe
universal cleanup primitive:

- Python ignores `ResourceWarning` by default, but warning filters can promote it to
  an error; this is a useful low-cost backstop for objects such as unclosed files and
  subprocesses that emit that warning. [VERIFIABLE][^17][^39]
- `tmp_path` gives each test a unique managed directory, and under xdist pytest places
  worker temp directories beneath a per-run directory. [VERIFIABLE][^40]
- `asyncio.TaskGroup` awaits all tasks when its context exits and cancels remaining
  tasks after the first non-cancellation failure, providing structured ownership for
  async work. [VERIFIABLE][^41]
- `contextlib.ExitStack` can register heterogeneous context managers and callbacks as
  resources are acquired, unwind them in reverse order, and still clean earlier
  acquisitions when a later one fails. [VERIFIABLE][^42]
- Threads cannot be safely killed by a general pytest guard; production code must
  expose cooperative stop/cancel plus bounded `join`, with enumeration serving only
  as detection. [HEURISTIC]
- Containers and remote resources are not necessarily descendants or even local, so
  they need provider-specific IDs, deletion callbacks, TTL/tag sweepers, and separate
  credentials/policy. [HEURISTIC]

The unified pattern is therefore a **resource ownership ledger**, not a universal
enumerator. Every harness acquisition registers an identity, scope, diagnostic
formatter, graceful closer, bounded waiter, and force/abandon policy before returning
the resource. Resource-specific adapters implement process-group kill, socket close,
thread cancellation/join, task cancellation/await, temp-tree cleanup, or remote
deletion. Per-type detectors then audit for resources that bypassed the ledger.
[INFERENCE]

Make the safe path the easy path: expose `owned_process`, `owned_thread`,
`owned_listener`, and `owned_task_group` fixtures; prohibit raw lifecycle primitives
in tests with a narrowly scoped lint/AST rule except inside the harness; and require
an explicit escape hatch with a reason. This removes the “remember a `finally`” choice
from ordinary test authors. [HEURISTIC]

### Gaps, blindspots & emergent findings

The prompt's named tools bias toward in-process pytest hooks. Following the failure
boundary outward surfaced these additional load-bearing findings:

1. **Worker death bypasses every Python fixture and hook.** A Linux cgroup v2
   supervisor can kill all processes in a cgroup and its descendants with
   `cgroup.kill`, including in the presence of concurrent forks/migrations; a Windows
   Job Object can manage a process set as a unit and, with
   `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, terminate members when the final job handle
   closes. [VERIFIABLE][^43][^44] Therefore CI should place the entire pytest run—or
   each worker where feasible—inside an outer containment domain and sweep it when
   the worker/controller exits. The in-pytest guard supplies attribution; the outer
   boundary supplies crash cleanup. [INFERENCE]
2. **A process group is addressability, not confinement.** A descendant can call
   `setsid()` and escape the group. Linux parent-death signals are not a complete
   substitute: `PR_SET_PDEATHSIG` is cleared for a child created by `fork`, so it is
   not automatically transitive through a tree. [VERIFIABLE][^45] Tests that exercise
   daemonization need cgroup/Job containment or an application-specific shutdown
   protocol. [INFERENCE]
3. **An ancestry snapshot has false negatives.** psutil explicitly notes that if an
   intermediate process disappears, a surviving grandchild may no longer be returned
   by recursive child enumeration. [VERIFIABLE][^21] Detection must not be presented
   as proof of absence; creation-time registration or OS containment is stronger.
   [INFERENCE]
4. **Timeout enforcement can amplify leakage.** A timeout plugin may end the worker
   before finalizers complete. [VERIFIABLE][^35] Outer containment is therefore a
   prerequisite for treating worker termination as a safe leak response, not an
   optional hardening layer. [INFERENCE]
5. **Cleanup errors can hide the original failure.** The guard should retain the
   original call outcome, attempt every cleanup, aggregate failures, and attach a
   leak report as teardown diagnostics rather than stopping at the first broken
   closer. [HEURISTIC]
6. **Late spawns and scoped services complicate attribution.** Background code can
   fork after an item snapshot, while module/session fixtures legitimately outlive an
   item. A ledger with explicit scope plus an eventual worker-session check handles
   both more accurately than a fixed post-test sleep. [INFERENCE]
7. **Cross-platform outer containment remains unresolved.** Linux cgroups and Windows
   Job Objects are strong but platform-specific; macOS has no cgroup equivalent, and
   no maintained Python/pytest package providing one uniform, documented abstraction
   was located. [NO SOURCE]
8. **Network/listener and remote-state leakage require separate observability.** A
   port may remain occupied by a process outside the worker ancestry, and remote
   resources are invisible to local psutil. No single local leak detector can prove
   those domains clean. [INFERENCE]
9. **PID/PGID reuse makes delayed raw signaling hazardous.** psutil's `Popen` wrapper
   performs identity checks specifically to avoid signaling a reused PID.
   [VERIFIABLE][^21] A saved integer PGID has no equivalent creation-time identity;
   if its original group has fully exited and the ID is reused before late cleanup,
   `killpg` could address an unrelated group. Cleanup should be prompt, tolerate
   “already gone,” and prefer an unforgeable containment handle where the platform
   supplies one. [INFERENCE]
10. **Outer containment is only as strong as its anti-escape configuration.** Windows
    documents Job breakaway flags, while cgroup delegation controls whether a
    delegatee can move processes outside its subtree. [VERIFIABLE][^43][^44] A CI
    wrapper must disable breakaway and deny migration out; merely creating a
    cgroup/Job without those policy settings does not establish containment.
    [INFERENCE]

Contrarian conclusion: a strict end-of-test descendant scan can create false
confidence **and** false positives. It is valuable as a secondary audit, but the
stronger design is acquisition-time ownership plus an outer failure-domain boundary.
[INFERENCE]

### When to use

- **Owned process fixture/factory:** every real subprocess whose lifetime is bounded
  by a test, including a process that must remain alive through the assertion.
  [HEURISTIC]
- **pytest-subprocess/fakes:** when command interaction is the contract and real
  process identity, signals, ports, or command-line visibility are not. [HEURISTIC]
- **pytest-xprocess:** longer-lived managed local services where its API fits; first
  verify required xdist/platform behavior in the fleet's own plugin tests.
  [HEURISTIC]
- **Per-item + worker-session detection:** in all repos that allow raw OS resources;
  use it as an audit and attribution layer. [HEURISTIC]
- **Outer cgroup/Job/supervisor:** CI and any environment that can kill workers,
  timeout tests, or run untrusted code. It is the only layer here that survives an
  abrupt Python-worker death. [INFERENCE]

## Comparison

| # | Criterion | Existence ratchet (§1) | Coverage-% ratchet (§1) | Behavioral eval (§4) | Unit tests (§2–3) |
|---|-----------|------------------------|-------------------------|----------------------|-------------------|
| 1 | Question answered | Does each unit have *any* tests? | Is coverage not regressing? | Does the model use/behave correctly? | Does the code compute the right answer? |
| 2 | Setup cost | Low (a shell check + allow-list) | Medium (coverage instrumentation) | High (LLM runs, subprocess per case) | Low–medium (pytest) |
| 3 | Big-bang catch-up needed? | No (grandfather via baseline) | No (freeze % then ratchet) | No | Backfill per skill |
| 4 | Fit for independent skill dirs | Excellent (structural, no shared namespace) | Poor (tree not importable as one pkg) | Good (per-skill) | Excellent |
| 5 | Gameable? | Yes (empty test file) | Yes (trivial tests) | Somewhat (flaky/ISSUE-REF) | Least (with mechanical check) |
| 6 | Right role here | First gate (follow-up to #198) | Second phase | Complementary / upstream's method | Core for owned code |

## Recommendation

Adopt a layered approach; do **not** fold new scope into #198 (approved/green, "run the
tests that exist"). Ship the framework as a follow-up.

**A. A reusable per-skill CLI test harness (`conftest`-based, composition-first).**
Add `skills/skills/conftest.py` (or a small importable `tests/harness.py`) providing:

- `run_skill(script_path, argv, cwd)` → `(exit_code, stdout, stderr)`: invokes a
  skill's `scripts/*.py` via `subprocess.run` (or import-and-call `main(argv)` where the
  script exposes one), the single place the exit-code/stdout/stderr contract is
  asserted. Mirror the house `SystemExit(code)` idiom already used in
  `cld-reagent-agent`.
- `portable_workspace` fixture: a `tmp_path`-seeded portable workspace (reuse the
  `workspace-template/cld` seeding the app already does), so skills that read/write the
  workspace get a clean, isolated tree per test (or per module for read-only cases).
- A tiny `SkillCliContract` base class (thin) for the universal checks only: `--help`
  exits 0 and prints usage; unknown/bad args → exit 2; declared JSON output parses and
  matches its schema. Skills subclass it *and/or* compose the fixtures; domain logic
  stays in the skill's own `test_*.py`, tested by direct function calls.

**B. A parametrized universal-contract test.** One module that discovers every skill
shipping a runtime script and, via `@pytest.mark.parametrize`, asserts the three
universal contract items above for each. This is the DRY payoff: new skills are
covered by the matrix automatically.

**C. The existence ratchet wired into `scripts/run-python-tests.sh`.** The script
*already* auto-discovers skill test dirs (`find skills/skills -name 'test_*.py'`) and
already has a floor check that fails if discovery returns nothing. Extend it to:

1. Discover every skill that *ships runtime Python* (a `skills/skills/*/` with a
   non-test `*.py`).
2. Fail if a skill **not** on `KNOWN_UNTESTED_SKILLS` ships Python but has no
   `test_*.py` (the new-untested-skill case we want to force).
3. Fail if a skill **on** the list now *has* tests but wasn't removed (forces the
   baseline to shrink, the boy-scout ratchet).
4. Scope the hard requirement to team-authored skills (`cld-*` + the domain query/
   analysis skills); keep vendored Anthropic skills (`docx`, `pdf`, `pptx`, `xlsx`,
   `mcp-builder`, `skill-creator`) on the baseline with a note that upstream validates
   them via the skill-creator eval harness (§4).

This is the betterer/mypy-baseline model adapted to a per-skill structural check: "every
Python skill must have tests" without blocking CI on today's gaps, with
`KNOWN_UNTESTED_SKILLS` as the visible, shrinking backlog. Layer a coverage-%
ratchet (`--fail-under`, per-skill or aggregate) later, once the existence gap is
closed.

**D. Keep per-skill tests DRY by composition, not deep inheritance.** Each skill's
`test_*.py` imports the harness fixtures and writes direct function tests for its
domain logic; the universal contract lives once in the parametrized module + thin base
class. This matches the existing house style (`cld-reagent-agent`,
`cld-new-loop/scripts/tests/`, `pypkg/cld_agent/tests/conftest.py`).

**E. Make resource ownership part of that harness contract.** Fix the motivating test
without weakening its survival assertion: replace its raw `Popen` with an
`owned_processes.spawn(..., start_new_session=True)` fixture call whose finalizer
TERM-waits-KILL-waits and reaps the process group. Then ship a versioned internal
pytest plugin that provides the same factory fleet-wide, audits after each complete
item teardown, and runs a second backstop at the end of every xdist worker session.
Require the plugin in CI; warn visibly rather than claim success when a local
restricted sandbox cannot enumerate processes. Finally, execute pytest inside a
CI-owned cgroup/Job/supervisor so worker death cannot strand descendants.
[INFERENCE]

The rollout order should be:

1. Patch this test through the owned-process fixture and add a regression test that
   verifies both facts: the fake survives the call assertion and no owned group exists
   after fixture teardown. [HEURISTIC]
2. Add per-worker item/session auditing in `best-effort` mode locally and `required`
   mode in trusted CI; report without automatic broad process-name killing.
   [HEURISTIC]
3. Extract the implementation to a pinned internal plugin, enforce it with
   `required_plugins`, and reduce copied conftest code to configuration. [HEURISTIC]
4. Add outer CI containment and deliberate worker-crash tests; only then claim cleanup
   under abrupt xdist worker loss. [HEURISTIC]
5. Extend the ownership ledger with thread/task/listener/temp/remote adapters based on
   observed fleet failures, keeping specialized detectors separate. [HEURISTIC]

## Open Questions

1. **Team-authored vs vendored classification.** The ratchet's hard-requirement scope
   needs an explicit, maintained list (or a marker in each skill's `SKILL.md`
   frontmatter, e.g. `origin: team|vendored`). Which mechanism does the team prefer:
   an allow-list in the script, or frontmatter the script reads?
2. **Harness invocation mode.** Should `run_skill` prefer subprocess (true contract,
   slower) or import-and-call `main(argv)` (fast, requires each script to expose a
   `main`)? A mixed policy (import-and-call where a `main` exists, else subprocess)
   adds branching: is standardizing on one worth a small refactor of the scripts?
3. **JSON-output schemas.** The universal contract's "JSON output validates" check
   needs each skill to declare an output schema. Do enough skills emit structured JSON
   to make this a universal contract item, or should it be opt-in per skill?
4. **golangci-lint `new-from-rev` details** could not be verified from a clean source
   this session (JS-rendered docs). If the Go-side precedent matters to the writeup,
   confirm the exact flag semantics against the rendered docs or the binary's `--help`.
5. **Coverage-% phase.** When (if ever) do we want the second-phase coverage ratchet,
   and at what granularity (global `--fail-under` vs per-package)? Deferred until the
   existence gap closes.
6. **Restricted-mode policy.** Which local execution environments are expected to
   deny descendant enumeration, and should their default be `best-effort` or require
   an explicit opt-out? Trusted CI should remain `required`.
7. **Allowed higher-scope resources.** Which module/session processes, listeners, or
   containers are legitimate, and how will their ownership/scope be declared so the
   item guard does not report them as leaks?
8. **Outer containment by platform.** Which CI executors can provide a dedicated
   cgroup, Windows Job Object, container, or equivalent run supervisor? macOS needs a
   documented weaker fallback because no equivalent uniform abstraction was found.
9. **Failure and quarantine policy.** Should the first fleet rollout clean-and-warn,
   clean-and-fail only on newly introduced leaks, or fail immediately? A short
   observation phase may be necessary to inventory legitimate long-lived fixtures,
   but it needs a fixed sunset to avoid becoming permanent warning debt.

## Sources

[^1]: Ned Batchelder & coverage.py contributors. (2026). [Configuration reference: `[report] fail_under`](https://coverage.readthedocs.io/en/latest/config.html). coverage.py documentation (Read the Docs). Verified accessible (HTTP 200) 2026-07-09. (CLI/config coverage-% floor; exit status 2 under threshold.)
[^2]: Python Software Foundation. (2026). [`argparse`: Parser for command-line options, arguments and subcommands](https://docs.python.org/3/library/argparse.html). Python 3 documentation. Verified accessible (HTTP 200) 2026-07-09. (`ArgumentParser.error()` terminates with status code 2 → the "bad args → exit 2" contract.)
[^3]: Spence, C. & betterer contributors. (2023–2026). [Betterer: Improve, incrementally](https://phenomnomnominal.github.io/betterer/). Betterer documentation site (pre-rendered homepage HTML). Verified accessible (HTTP 200) 2026-07-09. (Snapshot/baseline test runner; prevents regressions by tracking current status.)
[^4]: Gram (orsinium) & contributors. (2026). [mypy-baseline README](https://raw.githubusercontent.com/orsinium-labs/mypy-baseline/master/README.md). GitHub (orsinium-labs/mypy-baseline). Verified accessible (HTTP 200) 2026-07-09. (Baselines existing type errors; reports only newly introduced ones; drives count to zero.)
[^5]: jest-coverage-ratchet maintainers. (2026). [jest-coverage-ratchet: npm package metadata](https://registry.npmjs.org/jest-coverage-ratchet/latest). npm registry API. Verified accessible (HTTP 200) 2026-07-09. (Description: "Ensures code coverage only increases.")
[^6]: jest-ratchet maintainers. (2026). [jest-ratchet: npm package metadata](https://registry.npmjs.org/jest-ratchet/latest). npm registry API. Verified accessible (HTTP 200) 2026-07-09. (Description: coverage goes only one direction, up.)
[^7]: OpenJS Foundation & ESLint contributors. (2026). [Command Line Interface: `--max-warnings`](https://eslint.org/docs/latest/use/command-line-interface). ESLint documentation. Verified accessible (HTTP 200) 2026-07-09. (Warning-budget gate: exit error when warnings exceed threshold.)
[^8]: pytest-dev team. (2026). [How to use fixtures: scope](https://docs.pytest.org/en/stable/how-to/fixtures.html). pytest documentation. Verified accessible (HTTP 200) 2026-07-09. (Fixture scopes: function/class/module/package/session; conftest.py auto-discovery.)
[^9]: pytest-dev team. (2026). [How to parametrize fixtures and test functions](https://docs.pytest.org/en/stable/how-to/parametrize.html). pytest documentation. Verified accessible (HTTP 200) 2026-07-09. (`@pytest.mark.parametrize` for table-driven tests.)
[^10]: Pallets. (2026). [Testing Click Applications](https://click.palletsprojects.com/en/stable/testing/). Click documentation. Verified accessible (HTTP 200) 2026-07-09. (`click.testing` / `CliRunner` invokes commands and checks behavior/exit code.)
[^11]: pytest-mock maintainers. (2026). [pytest-mock documentation](https://pytest-mock.readthedocs.io/en/latest/). Read the Docs. Verified accessible (HTTP 200) 2026-07-09. (`mocker` fixture is a thin wrapper around the stdlib mock patching API.)
[^12]: Andrzej Klajnert & contributors. (2026). [pytest-subprocess documentation](https://pytest-subprocess.readthedocs.io/en/latest/). Read the Docs. Verified accessible (HTTP 200) 2026-07-09. (Fakes subprocess behavior via `fake_process`/`fp`; prevents real subprocess execution.)
[^13]: Anthropic. (2026). [anthropics/skills README](https://raw.githubusercontent.com/anthropics/skills/main/README.md). GitHub. Verified accessible (HTTP 200) 2026-07-09. (Many skills Apache 2.0; docx/pdf/pptx/xlsx are source-available, not open source.)
[^14]: Anthropic. (2026). [docx skill: SKILL.md frontmatter](https://raw.githubusercontent.com/anthropics/skills/main/skills/docx/SKILL.md). GitHub (anthropics/skills). Verified accessible (HTTP 200) 2026-07-09. (`license: Proprietary. LICENSE.txt has complete terms`.)
[^15]: Anthropic. (2026). [skill-creator: scripts/run_eval.py](https://raw.githubusercontent.com/anthropics/skills/main/skills/skill-creator/scripts/run_eval.py). GitHub (anthropics/skills). Verified accessible (HTTP 200) 2026-07-09. (Trigger-eval harness: tests whether a skill's description causes Claude to read the skill for a query set; outputs JSON.)
[^16]: Anthropic. (2026). [algorithmic-art: LICENSE.txt (Apache 2.0)](https://raw.githubusercontent.com/anthropics/skills/main/skills/algorithmic-art/LICENSE.txt). GitHub (anthropics/skills). Verified accessible (HTTP 200) 2026-07-09. (Corroborates the licensing split: non-doc skills carry Apache License 2.0.)
[^17]: Python Software Foundation. (2026). [`subprocess` — Subprocess management](https://docs.python.org/3/library/subprocess.html). Python 3.14.6 documentation. Verified accessible (HTTP 200) 2026-07-26. (`start_new_session`, `process_group`, signaling, waiting, pipe-deadlock guidance, shell PID semantics, and `Popen` context-manager behavior.)
[^18]: Python Software Foundation. (2026). [`os.killpg`: send a signal to a process group](https://docs.python.org/3/library/os.html#os.killpg). Python 3.14.6 documentation. Verified accessible (HTTP 200) 2026-07-26.
[^19]: Kerrisk, M., & Linux man-pages contributors. (2025). [`setsid(2)` — creates a session and sets the process group ID](https://man7.org/linux/man-pages/man2/setsid.2.html). Linux man-pages 6.15. Verified accessible (HTTP 200) 2026-07-26.
[^20]: pytest-dev team. (2026). [How to use fixtures: teardown/cleanup and safe fixture structure](https://docs.pytest.org/en/stable/how-to/fixtures.html). pytest documentation. Verified accessible (HTTP 200) 2026-07-26.
[^21]: psutil contributors. (2026). [psutil API reference: children, Popen, and `wait_procs`](https://psutil.readthedocs.io/en/stable/index.html). psutil 7.2.2 documentation. Verified accessible (HTTP 200) 2026-07-26.
[^22]: psutil contributors. (2026). [Frequently Asked Questions: process exceptions and race handling](https://psutil.io/faq/). psutil documentation. Verified accessible (HTTP 200) 2026-07-26.
[^23]: pytest-dev team. (2025). [How pytest-xdist works](https://pytest-xdist.readthedocs.io/en/stable/how-it-works.html). pytest-xdist documentation. Verified accessible (HTTP 200) 2026-07-26. (Controller/worker architecture and each worker's mini pytest runner.)
[^24]: pytest-dev team. (2025). [pytest-xdist how-tos: identifying workers and session-scoped fixtures](https://pytest-xdist.readthedocs.io/en/stable/how-to.html). pytest-xdist documentation. Verified accessible (HTTP 200) 2026-07-26.
[^25]: pytest-dev team. (2024). [pytest-xprocess documentation](https://pytest-xprocess.readthedocs.io/en/latest/). Read the Docs. Verified accessible (HTTP 200) 2026-07-26. (External-process management and process-tree termination API.)
[^26]: pytest-xprocess maintainers. (2024). [pytest-xprocess 1.0.2](https://pypi.org/project/pytest-xprocess/). Python Package Index. Verified accessible (HTTP 200) 2026-07-26. (Release date and project status.)
[^27]: pytest-dev team. (2026). [pytest plugin list](https://docs.pytest.org/en/stable/reference/plugin_list.html). pytest documentation. Verified accessible (HTTP 200) 2026-07-26. (Automated, uncurated plugin index.)
[^28]: Astropy Project. (2024). [pytest-openfiles repository](https://github.com/astropy/pytest-openfiles). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Archived status, final release, and migration guidance.)
[^29]: Astropy Project. (2024). [pytest-openfiles plugin implementation](https://raw.githubusercontent.com/astropy/pytest-openfiles/main/pytest_openfiles/plugin.py). GitHub. Verified accessible (HTTP 200) 2026-07-26. (`psutil.Process().open_files()` snapshot implementation.)
[^30]: Soffer, N. (2022). [pytest-threadleak 0.5.0](https://pypi.org/project/pytest-threadleak/). Python Package Index. Verified accessible (HTTP 200) 2026-07-26.
[^31]: Soffer, N. (2022). [pytest-threadleak plugin implementation](https://raw.githubusercontent.com/nirs/pytest-threadleak/master/pytest_threadleak.py). GitHub. Verified accessible (HTTP 200) 2026-07-26. (`pytest_runtest_call` thread snapshots and failure behavior.)
[^32]: pytest-leaks maintainers. (2019). [pytest-leaks 0.3.1](https://pypi.org/project/pytest-leaks/). Python Package Index. Verified accessible (HTTP 200) 2026-07-26. (CPython reference-leak scope and debug-build requirement.)
[^33]: Kumar, D. (2026). [pyleak 0.2.0](https://pypi.org/project/pyleak/). Python Package Index. Verified accessible (HTTP 200) 2026-07-26. (Async-task, thread, and event-loop blocking detection.)
[^34]: Kumar, D. (2026). [pyleak pytest plugin implementation](https://raw.githubusercontent.com/deepankarm/pyleak/main/src/pyleak/pytest_plugin.py). GitHub. Verified accessible (HTTP 200) 2026-07-26. (Marker-driven `pytest_runtest_call` hook.)
[^35]: pytest-timeout maintainers. (2025). [pytest-timeout](https://pypi.org/project/pytest-timeout/). Python Package Index. Verified accessible (HTTP 200) 2026-07-26. (Thread/signal method limitations and teardown consequences.)
[^36]: Klajnert, A., & contributors. (2026). [pytest-subprocess 1.6.0](https://pypi.org/project/pytest-subprocess/). Python Package Index. Verified accessible (HTTP 200) 2026-07-26.
[^37]: pytest-dev team. (2026). [Writing plugins](https://docs.pytest.org/en/stable/how-to/writing_plugins.html). pytest documentation. Verified accessible (HTTP 200) 2026-07-26. (`conftest.py` visibility and `pytest11` entry-point distribution.)
[^38]: pytest-dev team. (2026). [pytest configuration reference: `required_plugins` and plugin autoloading](https://docs.pytest.org/en/stable/reference/reference.html#confval-required_plugins). pytest documentation. Verified accessible (HTTP 200) 2026-07-26.
[^39]: Python Software Foundation. (2026). [`warnings` — Warning control](https://docs.python.org/3/library/warnings.html). Python 3.14.6 documentation. Verified accessible (HTTP 200) 2026-07-26. (`ResourceWarning` default disposition and warning-to-error filters.)
[^40]: pytest-dev team. (2026). [How to use temporary directories and files in tests](https://docs.pytest.org/en/stable/how-to/tmp_path.html). pytest documentation. Verified accessible (HTTP 200) 2026-07-26.
[^41]: Python Software Foundation. (2026). [`asyncio.TaskGroup`](https://docs.python.org/3/library/asyncio-task.html#asyncio.TaskGroup). Python 3.14.6 documentation. Verified accessible (HTTP 200) 2026-07-26.
[^42]: Python Software Foundation. (2026). [`contextlib.ExitStack`](https://docs.python.org/3/library/contextlib.html#contextlib.ExitStack). Python 3.14.6 documentation. Verified accessible (HTTP 200) 2026-07-26.
[^43]: Linux kernel contributors. (2026). [Control Group v2: `cgroup.kill`](https://docs.kernel.org/admin-guide/cgroup-v2.html). Linux kernel documentation. Verified accessible (HTTP 200) 2026-07-26.
[^44]: Microsoft. (2025). [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects). Windows application development documentation. Verified accessible (HTTP 200) 2026-07-26. (Process-set management, inheritance, termination, and kill-on-close behavior.)
[^45]: Kerrisk, M., & Linux man-pages contributors. (2025). [`PR_SET_PDEATHSIG(2const)`](https://man7.org/linux/man-pages/man2/pr_set_pdeathsig.2const.html). Linux man-pages 6.15. Verified accessible (HTTP 200) 2026-07-26. (Parent-death signal semantics and clearing on `fork`.)
[^46]: pytest-dev team. (2026). [`_pytest.pytester` source: `LsofFdLeakChecker`](https://docs.pytest.org/en/stable/_modules/_pytest/pytester.html). pytest documentation. Verified accessible (HTTP 200) 2026-07-26. (Disabled-by-default `--lsof` capability probe and full-protocol current-process FD delta warning.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none: this doc was authored via the research-doc skill, not an aido --refine run)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** n/a (ad-hoc, research-doc skill, not registered to the framework-core DB)
**Model:** Claude ad-hoc web (research-doc skill); Opus driver + Foundry-backed `aido webfetch`/`aido websearch` (Haiku) for retrieval
**Date:** 2026-07-09

```text
You are a principal test/CI engineer who has shipped coverage gates and CLI test
harnesses for multi-tool Python monorepos, and who has read the anthropics/skills repo.
You have evidence-backed opinions; when you cannot find a source you say so.

Research "best practices for testing Claude Agent skills that ship Python CLI code:
coverage enforcement, CLI test technique, test-code abstraction/framework, and how
Anthropic tests its own skills." Cover four numbered sections, each with a
"### When to use":
  1. Enforcing per-skill test coverage: the ratchet/baseline pattern (betterer,
     mypy-baseline, FlakeHell, jest-coverage-ratchet/jest-ratchet, coverage.py
     --cov-fail-under; the general coverage-ratchet / budgeted-violations pattern).
  2. How to test Python CLI tools (separate logic from arg-parsing; thin CLI-contract
     layer for exit codes/stdout/stderr/argparse; mock boundaries with unittest.mock /
     pytest-mock / pytest-subprocess; fixtures + parametrization; spec-first; the
     mechanical check).
  3. Reducing test redundancy via abstraction: a reusable test framework (conftest
     fixtures + scopes; parametrize; BaseToolTests subclass-per-tool; a reusable
     skill-CLI harness + temp-workspace fixture; OO-inheritance vs composition/fixtures
     trade-offs).
  4. How Anthropic tests its own official skills (anthropics/skills: source-available
     doc skills, ships Python, bundles no traditional unit tests, validated via the
     skill-creator eval framework).
Then a Recommendation proposing a concrete reusable test framework for THIS repo's
skills: a SkillCliTestBase/conftest harness (exit code/stdout/stderr/JSON-schema +
temp-portable-workspace fixture); a parametrized "every skill: --help works, bad args →
exit 2" table; the auto-discover + KNOWN_UNTESTED_SKILLS ratchet wired into
scripts/run-python-tests.sh; and how per-skill files subclass/compose the harness to
stay DRY. Reference existing patterns in skills/skills/cld-new-loop/scripts/tests/,
skills/skills/cld-reagent-agent/scripts/tests/, and pypkg/cld_agent/tests/.

## Scope note: questions, examples, and named references are a starting point, not a checklist
The questions, topics, and named examples above are illustrative anchors and a FLOOR for
this research: not an exhaustive list to answer only. Reason independently: survey the
landscape broadly, follow the evidence, expand scope where warranted, and surface
relevant work, factors, and failure modes not named here. An output that merely fills in
the listed items has NOT met the research goal.

<grounding_instructions>
Temporal scope: weight sources by recency (2026/2025 primary); if post-cutoff literature
is genuinely sparse for a subtopic, state "no significant recent developments found"
rather than backfilling with older sources.

Before finalizing, run a Chain-of-Verification (CoVe):
  1. Isolate the core facts required.
  2. Draft a tentative answer.
  3. Hostile cross-examination: flag any claim cited because the prompt implied it,
     rather than because it was verified.
  4. Strip any claim that cannot be empirically verified.

Classify every major claim; write the rationale BEFORE the tag: rationale → evidence
check → tag ([VERIFIABLE] / [HEURISTIC] / [INFERENCE] / [NO SOURCE]). Only [VERIFIABLE]
claims carry an inline [^N] footnote to a URL fetched this session.
Hard constraint (overrides all formatting): never invent a citation to satisfy a format
instruction. Accuracy > completeness.
</grounding_instructions>
```

**Refine pass:** Codex research worker with live primary-source retrieval
**Date:** 2026-07-26

```text
Refine the existing research document at
docs/research/python-skill-testing-and-coverage.md (606 lines, status: complete).
Keep everything currently correct; ADD a new dimension it does not yet cover.
Conform to the canonical research template (docs/research/TEMPLATE.md,
research-1.2.0) — read it first.

## The gap this refine fills

The doc covers coverage enforcement, CLI test technique, and test harness design. It
does NOT cover test-subprocess lifecycle management and automated leak detection,
which just cost this fleet 148 orphaned processes — oldest alive over two days,
accumulated silently across 10+ pytest runs, discovered only by accident.

Concrete incident to ground the research (assume established, do not re-derive):
tests/test_codex_weekly_pct.py::
test_script_does_not_kill_a_sibling_app_server_process deliberately spawns a
long-lived fake process so its command line stays a genuine pkill match, asserts the
process SURVIVED (that is the assertion — the script under test must not kill it),
and never reaps it. The test is legitimate and worth keeping. Its teardown is
missing. `while true; do sleep 1; done` in a bash child, so a bare PID kill would not
even suffice — the process group must die.

## Research questions — a floor, not a ceiling

1. What is current industry practice for managing subprocess lifecycles in test
   suites? Process groups, session leaders, `start_new_session`, psutil child
   enumeration, context managers, pytest fixture patterns. Which of these actually
   hold when the test itself asserts the process is still alive at assertion time?
2. Automated leak detection: what exists off the shelf? Is there a pytest plugin that
   fails a run on leaked child processes, open file descriptors, or threads? Evaluate
   anything found — maintained, adopted, does it work under xdist?
3. pytest-xdist specifics: how do leak guards behave with parallel workers? The
   leaked paths showed `popen-gw5`. What is the correct scope for a teardown guard
   when workers are separate processes?
4. Sandboxed/restricted environments: process enumeration can be denied (inside the
   Codex sandbox, `ps -ef` raises PermissionError). How should a leak guard degrade —
   skip, warn, or fail? What do robust implementations do when they cannot observe?
5. Where should such enforcement LIVE so every repo inherits it — a shared conftest,
   a distributed fixture package, a plugin? What are the tradeoffs for a multi-repo
   fleet where config is copier-propagated?
6. Beyond processes: what other resource leaks do test suites classically produce
   (fds, temp dirs, threads, network listeners, background tasks), and is there a
   unified guard pattern or must each be handled separately?
7. The design principle underneath: how do teams make this class of mistake
   structurally impossible rather than caught by review? Look for prior art on
   test-harness design that removes the footgun instead of documenting it.

## Scope note — questions, examples, and named references are a starting point, not a checklist
The questions and named examples above are illustrative anchors and a FLOOR for this
research — not an exhaustive list to answer only or evaluate only. Reason
independently: survey the landscape broadly, follow the evidence where it leads,
expand scope where warranted, and surface relevant work, factors, and failure modes
not named here. Actively resist answering only the listed questions — an output that
merely fills in the listed items has NOT met the research goal.

## Independent exploration (gaps, blindspots, emergent threads) — required
Treat the question list as a FLOOR, not a ceiling. As you research, actively surface
what this framing may be missing and pursue each promising thread to a logical
conclusion:
- Adjacent or upstream factors the questions don't capture.
- Contrarian / disconfirming evidence — report it even when it challenges the
  premise.
- Emerging 2025-2026 practices, tools, or research not anchored by the named
  examples.
- Known failure modes and second-order effects.
Whenever a load-bearing thread surfaces mid-research, follow it to its conclusion and
report it in a dedicated "Gaps, blindspots & emergent findings" subsection.
Explicitly NAME any blindspot you suspect but cannot resolve (and why) rather than
omitting it. Anchor bias is a known failure mode; counter it deliberately and say
where you did.

## Retrieval requirements (mandatory for a refine)

For every claim you intend to tag [VERIFIABLE], FETCH a source in this session —
cached or remembered URLs are not sufficient evidence for a refine. At least one
web_search or web_fetch per load-bearing claim. Do not stop after one round; if a
sub-question yields nothing, search alternate framings before tagging [NO SOURCE].
That tag is for genuine evidence absence, never retrieval avoidance. Prioritise
pytest's own docs and issue tracker, plugin sources, and primary engineering
write-ups.

Preserve existing correct content and its citations. Mark clearly what this refine
pass added. Update Run History and the Appendix: Research Prompt.

<grounding_instructions>
You are a principal test-infrastructure engineer who has built and maintained large
parallel Python test suites in CI, and who has debugged resource leaks that only
surfaced days later in production infrastructure. You have strong opinions backed by
evidence about which safeguards hold under parallelism and which merely look
reassuring. When you cannot find a source, you say so explicitly rather than filling
the gap with plausible-sounding practice.

Temporal scope: Weight sources by recency — 2026 (primary) → 2025 → 2024. Pre-2024
sources are background context only unless foundational to the topic; POSIX
process-group semantics and long-stable pytest APIs are in scope at any age where
still current. If post-2024 literature is genuinely sparse for a subtopic, state
"[subtopic]: no significant post-2024 developments found" rather than backfilling
with older sources. Backfilling is a failure mode, not a hedge.

Before generating your final output, execute a Chain-of-Verification (CoVe) to ensure
factual fidelity over compliance.

Inside your thought process:
1. Isolate the core facts required.
2. Draft a tentative response.
3. Hostile Cross-Examination: flag any claim where you are citing a source because
   the prompt implied you should, rather than because you verified it.
4. Strip away any claim that cannot be empirically verified.

When generating your final output, classify every major claim. Write your rationale
before appending the tag — writing the tag first causes post-hoc rationalization.
Rationale → evidence check → tag.

- [VERIFIABLE]: backed by documentation, peer-reviewed research, or official tech
  blogs. Carry an inline footnote ref: [VERIFIABLE][^N].
- [HEURISTIC]: widely accepted best practice without a specific citation.
- [INFERENCE]: a logical conclusion drawn from context. Provide reasoning in-text. Do
  not fabricate a source. Tier tag only — NO footnote ref.
- [NO SOURCE]: explicitly state when you cannot find verifiable data. Tier tag only —
  NO footnote ref.

Citation format (mandatory for every externally-sourced claim):
- Inline: append the GFM footnote ref directly after the tier tag:
  [VERIFIABLE][^N]. Multiple sources carry ascending separate refs —
  [VERIFIABLE][^3][^7] (never grouped, never out of order).
- Footnote definitions live once, under ## Sources, in APA form with a clickable URL
  or DOI and an access-verification stamp.
- URL-or-DOI ALWAYS. Only the truly-irreducible case gets an explicit
  [no online source located] marker with a one-line justification.
- Integrity: refs contiguous from [^1], every ref has a definition, every definition
  is referenced.
- [INFERENCE] / [NO SOURCE] carry the tier tag with NO footnote ref.

Hard constraint (overrides all formatting preferences): never invent a citation to
satisfy a formatting instruction. Accuracy > completeness.

Format diagrams using Mermaid.js or ASCII. Format math using LaTeX.
NEVER generate binary images.
</grounding_instructions>
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

Every row below was live-fetched this session (2026-07-09) via Foundry-backed
`aido webfetch --no-summarize` (raw HTML/text) or clean JSON metadata APIs (npm
registry, GitHub API). Quotes are verbatim substrings of the fetched text
(whitespace-normalized). Verdict is a second entailment pass (does the quote *support*
the claim). 16 rows, all SUPPORTED and live.

| # | Claim | Source URL | Verbatim quote (from fetched page) | Verdict | Live? |
|---|-------|-----------|-----------------------------------|---------|-------|
| 1 | coverage.py `fail_under` fails the build under a % floor | https://coverage.readthedocs.io/en/latest/config.html | "If the total coverage measurement is under this value, then exit with a status code of 2." | SUPPORTED | ✅ HTTP 200 |
| 2 | argparse exits 2 on bad args | https://docs.python.org/3/library/argparse.html | "terminates the program with a status code of 2." | SUPPORTED | ✅ HTTP 200 |
| 3 | betterer is a baseline/ratchet that prevents regressions | https://phenomnomnominal.github.io/betterer/ | "Prevent regressionsTrack the current status and prevent creating new issues." | SUPPORTED | ✅ HTTP 200 |
| 4 | mypy-baseline reports only newly-introduced errors | https://raw.githubusercontent.com/orsinium-labs/mypy-baseline/master/README.md | "it will remember all type errors that you already have in the project (generate \"baseline\"). All consecutive runs will ignore these errors and report only ones that you introduced after that." | SUPPORTED | ✅ HTTP 200 |
| 5 | jest-coverage-ratchet auto-raises coverage | https://registry.npmjs.org/jest-coverage-ratchet/latest | "Ensures code coverage only increases" | SUPPORTED | ✅ HTTP 200 |
| 6 | jest-ratchet drives coverage one direction | https://registry.npmjs.org/jest-ratchet/latest | "Ratchet up code coverage - keep test coverage going only one direction -- up" | SUPPORTED | ✅ HTTP 200 |
| 7 | ESLint `--max-warnings` is a warning-budget gate | https://eslint.org/docs/latest/use/command-line-interface | "if --max-warnings is specified and the total warning count is greater than the specified threshold, ESLint exits with an error status." | SUPPORTED | ✅ HTTP 200 |
| 8 | pytest fixtures carry scopes | https://docs.pytest.org/en/stable/how-to/fixtures.html | "Possible values for scope are: function, class, module, package or session." | SUPPORTED | ✅ HTTP 200 |
| 9 | `@pytest.mark.parametrize` gives table-driven tests | https://docs.pytest.org/en/stable/how-to/parametrize.html | "The builtin pytest.mark.parametrize decorator enables parametrization of arguments for a test function." | SUPPORTED | ✅ HTTP 200 |
| 10 | Click ships a CLI test runner | https://click.palletsprojects.com/en/stable/testing/ | "Click provides the click.testing module to help you invoke command line applications and check their behavior." | SUPPORTED | ✅ HTTP 200 |
| 11 | pytest-mock provides a `mocker` fixture | https://pytest-mock.readthedocs.io/en/latest/ | "This pytest plugin provides a mocker fixture which is a thin-wrapper around the patching API provided by the mock package" | SUPPORTED | ✅ HTTP 200 |
| 12 | pytest-subprocess fakes subprocess execution | https://pytest-subprocess.readthedocs.io/en/latest/ | "This is a pytest plugin to fake the real subprocess behavior to make your tests more independent." | SUPPORTED | ✅ HTTP 200 |
| 13a | anthropics/skills: many skills Apache 2.0 | https://raw.githubusercontent.com/anthropics/skills/main/README.md | "Many skills in this repo are open source (Apache 2.0)." | SUPPORTED | ✅ HTTP 200 |
| 13b | doc skills are source-available, not open source | https://raw.githubusercontent.com/anthropics/skills/main/README.md | "These are source-available, not open source" | SUPPORTED | ✅ HTTP 200 |
| 14 | docx skill is proprietary-licensed | https://raw.githubusercontent.com/anthropics/skills/main/skills/docx/SKILL.md | "license: Proprietary. LICENSE.txt has complete terms" | SUPPORTED | ✅ HTTP 200 |
| 15 | skill-creator validates via a trigger-eval harness | https://raw.githubusercontent.com/anthropics/skills/main/skills/skill-creator/scripts/run_eval.py | "Run trigger evaluation for a skill description.\n\nTests whether a skill's description causes Claude to trigger (read the skill)\nfor a set of queries. Outputs results as JSON." | SUPPORTED | ✅ HTTP 200 |
| 16 | non-doc skills carry Apache 2.0 (licensing split) | https://raw.githubusercontent.com/anthropics/skills/main/skills/algorithmic-art/LICENSE.txt | "Apache License\n                           Version 2.0, January 2004" | SUPPORTED | ✅ HTTP 200 |

**Citation failure taxonomy (walked this session, per the skill's ladder):**

- `betterer.dev` (all `/docs/*` pages) and `golangci-lint.run`: JS-rendered Docusaurus
  SPAs; `--no-summarize` raw fetch returned ~100–180 bytes of shell markup, no content.
  *Resolved for betterer* via the pre-rendered github.io homepage HTML (row 3) + the npm
  `@betterer/cli` metadata; *not resolved for golangci-lint* → that claim left
  `[HEURISTIC]`, not upgraded (Open Question 4).
- `pypi.org/project/mypy-baseline/`: Fastly "Client Challenge" bot-block (JS gate).
  *Resolved* via the GitHub raw README (row 4): the metadata/alt-source rung of the
  ladder.
- `registry.npmjs.org/betterer` (unscoped): resolves to an *unrelated* package
  ("Proof of concept for tests that read better(er)"), not the ratchet tool (which is
  the scoped `@betterer/*`). Recorded to prevent a future mis-citation.
- `skills/docx/LICENSE.txt` raw fetch returned empty this session → substituted the
  `docx/SKILL.md` frontmatter `license:` field (row 14) + README characterization
  (rows 13a/13b), which state the same fact from fetched sources.

**Scholarly cross-check note:** this topic has *no* peer-reviewed/scholarly claims:
every claim is backed by official tool documentation or live repository source, so the
Crossref/OpenAlex metadata-API step is N/A. The clean-JSON metadata equivalents used
here are the **npm registry API** (rows 5, 6) and the **GitHub API** (recursive tree +
repo metadata, used to establish the "no unit tests / no evals.json" absence claim in
§4).

**2026-07-26 refine verification ledger:** Sources [^17]–[^46] were live-fetched in
this refine session. The table records the hostile entailment check performed before
finalization. It deliberately does **not** promote search absences (no general process
leak plugin, undocumented xdist support, adoption, or a uniform cross-platform
containment package) to verifiable facts; those remain `[NO SOURCE]` in §5.

| Claim family | Fetched sources | Entailment check | Verdict |
|---|---|---|---|
| POSIX/Python session, group, signal, wait, shell, and pipe behavior | [^17], [^18], [^19] | Official runtime docs and current man-pages state the invoked operations and limits; no claim of automatic group cleanup retained. | SUPPORTED |
| Pytest teardown/finalizer ordering | [^20] | Official fixture docs support post-assertion teardown and LIFO cleanup; abrupt interpreter death is not attributed to fixture docs. | SUPPORTED |
| psutil tree enumeration, PID safety, and observation errors | [^21], [^22] | Docs explicitly support recursive children and document broken ancestry/race/access exceptions; detector conclusions remain inference. | SUPPORTED |
| xdist worker boundary and per-worker session scope | [^23], [^24] | Official xdist docs establish workers as separate runners and session fixtures as once per worker. | SUPPORTED |
| pytest-xprocess feature/release status | [^25], [^26] | Docs/PyPI support managed-tree cleanup and the 2024 release; lack of generic detection/xdist documentation is kept `[NO SOURCE]`. | SUPPORTED |
| Plugin index characterization | [^27] | Official list calls itself automated/uncurated; registry-search absence is not cited as proof. | SUPPORTED |
| pytest-openfiles status and implementation | [^28], [^29] | Archived repository and source support release/status and psutil snapshot mechanics. | SUPPORTED |
| pytest-threadleak status and hook phase | [^30], [^31] | PyPI and plugin source support age and call-phase snapshot behavior. | SUPPORTED |
| pytest-leaks scope | [^32] | PyPI description supports CPython reference-leak/debug-build scope. | SUPPORTED |
| pyleak status, resource types, and hook phase | [^33], [^34] | Current PyPI/source support 2026 release, covered resource types, marker, and call hook; xdist support remains `[NO SOURCE]`. | SUPPORTED |
| pytest-timeout failure modes | [^35] | Project documentation explicitly warns about process termination and incomplete teardown/reporting. | SUPPORTED |
| pytest-subprocess current release/faking | [^12], [^36] | Current docs/PyPI support fake-process purpose and 2026 release. | SUPPORTED |
| Pytester's internal FD detector | [^46] | Current first-party source supports its disabled-by-default lsof probe, full item-protocol snapshot, and warning behavior; no xdist guarantee inferred. | SUPPORTED |
| Plugin distribution and required loading | [^37], [^38] | Official pytest docs support conftest visibility, `pytest11`, `required_plugins`, autoload disablement, and explicit loading. | SUPPORTED |
| Warnings, temp paths, structured tasks, and generic cleanup stacks | [^39], [^40], [^41], [^42] | Official Python/pytest docs support each narrow mechanism; a unified resource ledger is clearly labeled inference. | SUPPORTED |
| Crash containment and parent-death limits | [^43], [^44], [^45] | Kernel/Microsoft/man-pages sources support cgroup kill, Job Object kill-on-close, and non-transitive `PDEATHSIG`; fleet architecture is labeled inference. | SUPPORTED |

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

- **2026-07-09**: Authored via the `research-doc` skill (Claude ad-hoc web workflow;
  Opus driver + Foundry-backed `aido webfetch`/`aido websearch`). 16 citations
  live-validated (`aido webfetch --no-summarize` raw HTML/text + npm/GitHub JSON APIs);
  5-perspective adversarial critic pass run (contrarian/adjacent findings folded into
  §1, §3, §4 as blockquotes); failure taxonomy applied to 4 unreachable/mis-resolving
  sources. Supersedes/consolidates `python-skill-test-coverage-enforcement.md`.
- **2026-07-26**: Refined in place with §5 on subprocess ownership, group teardown,
  automated leak detection, xdist worker scope, restricted observation, fleet plugin
  distribution, non-process resources, and crash containment. Live-fetched and
  entailment-checked 30 new primary/implementation sources ([^17]–[^46]); retained
  unresolved market/xdist/adoption claims as `[NO SOURCE]`. Added a dedicated
  blindspots section and completed a Chain-of-Verification pass before validation.

<!-- /doc:region name="run_history" -->
