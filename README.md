# ai-chassis

ai-chassis is a collection of reusable tools, skills, document templates, and
Python modules for working with AI coding agents. It comes from a working setup
for running several coding agents in parallel on one codebase, and it covers the
parts that sit around the model: agent skills, session and context configuration,
git and worktree helpers, review and audit evidence, and the tests for them.

This repository is a generalized extract. Some components of the original system
are intentionally not included. It is a source toolkit, with no complete harness
installer or top-level command-line application in this extract.

## What is inside

| Path | What it holds |
| --- | --- |
| `skills/` | Agent skills, each a `SKILL.md` with its helpers. Examples: `save-state` (record a session's state before its context resets), `sync-git` (bring branches and worktrees in line), `commit` (a validated commit), `decision-framework`, `quality-check`, `explain`, `summarize`, `usage-status`. |
| `projects-wide-session-config/` | Shared modules and rules from which per-machine agent context files are composed, with an example config. |
| `src/ai_harness/` | Python modules that compose and apply that configuration: profiles, schema, settings merge, links and generations. |
| `scripts/` | Helpers for session lifecycle, git and worktree safety, review and audit evidence, and quality checks. |
| `docs/` | Procedures, templates for designs, plans, research and audits, and a [documentation guide](docs/README.md). |
| `codex/` | Example role configurations for delegating work to Codex agents. |
| `packages/fleet-testkit/` | Test-support code the suite relies on. |
| `tests/` | The Python test suite. |

## Get started

Download or clone this repository and open its root directory. Install Python 3.11
or newer, Git and [uv](https://docs.astral.sh/uv/), then install the development
tools and run the Python tests (several tests create temporary Git repositories):

```sh
uv sync --frozen
uv run pytest
```

The project metadata configures this source checkout and its test environment; it
does not install a Python distribution or register a console command. Python
modules are available under `src/`, scripts under `scripts/`, agent skills under
`skills/`, and document templates under `docs/`. Individual tools may require
additional dependencies described alongside their source. Review a tool's
instructions before running it.

## Scope

The extract omits personal configuration, deployment-specific integrations, and
parts of the original runtime. It does not provide a turnkey installation of the
original system. Examples and templates are starting points to adapt to your own
project. The Python suite covers the included test modules; it does not establish
support for every tool or operating system.

## Provenance

The public repository begins at a clean root. It is not the original development
history; the pre-launch history is retained privately.

## Contribute and report issues

Use the repository's issue tracker for bugs and feature requests. See
[CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidance and
[SECURITY.md](SECURITY.md) for private security reporting.

The project is licensed under [Apache-2.0](LICENSE). See [NOTICE](NOTICE) and the
accompanying notices for third-party components.
