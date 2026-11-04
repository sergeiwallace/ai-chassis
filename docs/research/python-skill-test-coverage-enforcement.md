---
title: "Enforcing per-skill Python test coverage — the ratchet/baseline pattern"
category: research
status: draft
source: "Genericized from prior personal research; external claims cited inline."
template_version: "research-1.1.0"
---
<!-- doc:region name="context" kind="immutable" -->

## Context

(none — context not recorded in original doc)

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->


# Enforcing per-skill Python test coverage — the ratchet/baseline pattern

## Question

Suppose a repo has just wired its existing Python tests into CI, but the gate only runs tests that already exist. You want the gate to be **smart**: any skill the team ships that uses Python (a CLI tool / script) should be required to have automated tests, auto-discovered, and resilient to skills being added, renamed, deleted, or consolidated. How do teams do this well?

("Skill" here means an agent skill — a self-contained directory shipping a CLI tool or script. The pattern generalizes to any repo with many independent script/tool directories.)

## Finding: the industry-standard answer is a "ratchet" (a baseline of known gaps that can only shrink)

Every mature tool that adds a quality gate to a codebase with existing gaps uses the same shape: **snapshot the current violations as a baseline, fail only on NEW violations, and require the baseline to monotonically shrink.** This is the "boy-scout rule" made mechanical — you can adopt the gate today without a big-bang catch-up, while new work is held to the standard.

Corroborating tools/patterns:

- **betterer** (JS/TS) — snapshot-based test runner: records known violations to a results file; a run that gets worse fails, a run that improves updates the snapshot. Once a rule reaches zero it graduates into normal linting.
- **mypy-baseline** / **FlakeHell** (Python) — generate a baseline of existing type/lint errors; subsequent runs report only new errors. Explicit philosophy: not to hide existing errors forever but to drive the count to zero over time.
- **jest-ratchet / jest-coverage-ratchet** (JS) — automatically raise the coverage threshold whenever a higher level is reached, so coverage can only go up.
- **coverage.py `--cov-fail-under=N`** (Python) — the coverage-percentage floor; the numeric variant of the same idea.

Two enforcement *layers* exist, and they answer different questions:

1. **Existence ratchet** — "does every Python-shipping unit have *any* tests?" (structural). This is what the question is really asking.
2. **Coverage-% ratchet** — "for units that have tests, is line/branch coverage not regressing?" (`--cov-fail-under`, jest-ratchet). Heavier; a good second phase, not the first.

For detecting untested modules specifically, the surveyed techniques are: coverage.py `--source` (untested files show 0%), or a dynamic `inspect`-based test that enumerates modules/objects and asserts a corresponding test exists. For a layout of independent skill/script dirs (not one package), a **structural directory check is simpler and more robust than coverage introspection**.

## Recommended design for a many-skills layout

A **test-existence ratchet** in the single test-runner entry point (the script that already runs the Python suite):

1. **Auto-discover** every skill that ships runtime Python — a skill dir containing a non-test `*.py`. Churn-resilient: add a skill → picked up; delete/consolidate → drops out; no hardcoded count, no manifest of "expected" skills.
2. **Require** each discovered skill to have ≥1 `test_*.py` (in its dir or a `tests/` subdir).
3. **Baseline / grandfather** the current gaps in a committed, documented `KNOWN_UNTESTED_SKILLS` list. The check **fails** when either:
   - a skill **not** on the list ships Python but has no tests (a new/changed skill without coverage — the case you want to force), **or**
   - a skill **on** the list now *has* tests but wasn't removed from the list (forces the baseline to shrink — the boy-scout ratchet).
4. Optionally scope the hard requirement to team-authored skills, and keep generic/vendor library skills on the baseline with a note, rather than hard-requiring tests for boilerplate you didn't write.

This is the betterer/mypy-baseline model adapted to a per-skill structural check. It gives "every Python skill must have tests" without blocking CI on today's gaps, and the `KNOWN_UNTESTED_SKILLS` list *is* the visible backlog.

Later, layer a coverage-% ratchet (`--cov-fail-under`, per-skill or aggregate) once the existence gap is closed.

## Recommendation

Implement the existence ratchet as a **follow-up** to wiring the existing tests into CI — not folded into it (that first step should stay scoped to "run the tests that exist"). The check + baseline is small; writing the missing skills' tests is the burn-down backlog. Scope the hard requirement to team-authored skills; baseline the vendor ones.

## How to test skills that ship Python (best practices)

From the CLI-testing literature + common house patterns:

1. **Separate logic from arg-parsing; test the functions directly** (in-memory, fast) — the primary layer. Don't route everything through subprocess.
2. **Add a thin CLI-contract layer:** exit codes, stdout/stderr, and argparse validation (bad args → exit 2). Exit codes + stderr are first-class CLI behavior and must be asserted.
3. **Mock external boundaries** — databases, HTTP inference endpoints, third-party APIs — with `unittest.mock` / `pytest-mock` / `pytest-subprocess`. Never hit live services in unit tests (slow, flaky, may cost money).
4. **Fixtures + parametrization** for input/expected pairs; **≥1 failure-path test per public function**.
5. **Spec-first** — derive tests from the skill's documented contract, not the implementation.
6. **Mechanical check** — a test must fail if the function body is replaced with `pass` / `return None` (no tautological `assert result`).

## On vendored/library skills (what to exclude and why)

Vendored document/meta skills (for example, the official document-handling and skill-authoring skills distributed by a model provider) may **ship Python** but **not bundle traditional pytest unit tests** — such skills are often validated through a behavioral eval framework (a subagent per test case) rather than unit tests. Implications: (1) excluding these from a unit-test requirement is correct — they are external, and even upstream doesn't unit-test them; (2) unit-testing your *own* skills' Python (real domain logic) is more rigorous than eval-only and is the right call for code you wrote. Reference-only skills (a doc bundled as a skill, no CLI) are also fair to exclude.

## Sources

- betterer — snapshot/baseline test runner.
- mypy-baseline; FlakeHell — Python type/lint baselining.
- jest-ratchet / jest-coverage-ratchet — auto-raising coverage thresholds.
- coverage.py `--cov-fail-under`, `--source` untested-file detection.
- "Coverage ratchet" / budgeted-violations-that-only-decrease pattern (general CI practice).

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
