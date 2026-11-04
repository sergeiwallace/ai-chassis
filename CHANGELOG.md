# Changelog

All notable changes to ai-chassis are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[semantic versioning](https://semver.org/spec/v2.0.0.html). While the major version is
0, a minor release may add a subsystem and may change interfaces; a patch release only
fixes defects and documentation.

## [0.2.0] - 2026-11-04

### Breaking

- The Python import package is now `ai_chassis`. In 0.1.x the modules under `src/` were
  named `ai_harness`; code that imports them must change `ai_harness` to `ai_chassis`.
  The old name is not kept as an alias. The project, the command and the PyPI
  distribution name are `ai-chassis`.

### Added

- The `ai-chassis` command line application, with `plan`, `apply`, `doctor`, `explain`,
  `init` and the other subcommands of the harness it comes from, and the package behind it
  (`pip`-installable from a checkout; the package definition, version file and lock file
  are in the repository root).
- `install.sh` for Linux and macOS and `install.ps1` for Windows. They pick the newest
  release (or a ref you name), clone it, install the command with uv, and create an initial
  configuration file. The checkout, bin and config directories, the repository and a
  directory of templates can each be set by flag or environment variable, and `--dry-run`
  prints the steps without running them. The shell installer has a test suite; the
  PowerShell one has not been run on Windows yet.
- The modules the command needs, selected by following its imports, so the set is derived
  rather than listed: the Python modules the command imports, and the default data files
  they read.

### Changed

- The example configuration now uses the current per-machine tables, so it loads with the
  command.

### Known limits

- `ai-chassis plan` needs install targets that you declare in your own configuration.
- `ai-chassis validate` needs credential pattern files that you supply.
- A wheel installs the command and its package data, but several commands read the
  scripts and configuration beside the package, so the installer, which keeps a checkout,
  is the complete route. There is no PyPI release yet.

### Removed

- The shadow watcher runner module. It loaded a registry module that is not part of this
  repository, so its loader raised `ImportError` whenever it ran. It shipped in 0.1.0,
  0.1.1 and 0.1.2.

## [0.1.2]

### Changed

- Development tools: ruff 0.17.3, with the lock file refreshed (pytest 9.2.0).
- CI actions: `actions/checkout`, `actions/setup-python` and `astral-sh/setup-uv` moved
  to v7.
- Automatic dependency update pull requests are switched off. This repository is
  generated from a private source tree, so a dependency bump merged here would conflict
  with the next release; bumps are made at the source and arrive in a release.

## [0.1.1]

### Fixed

- GitHub links in the documentation no longer contain a space. The release previously
  replaced the owner's name with a placeholder that broke 13 links, and left sentences
  such as "Example User must act on". The owner name is now rewritten by context.
- The workflow files no longer point at a document that is not part of this repository.
- The release builds again from the source's main line: the job pickup hook loads the
  optional quota admission component by name and says plainly when it is absent.

### Added

- A README with architecture and session diagrams, badges, and a list of the skills
  that ship a `SKILL.md` in this release.
- Community files: code of conduct, support guide, this changelog, pull request and
  feature request templates, code owners, and dependency update configuration.
- A continuous integration workflow that installs the locked dependencies and runs the
  Python test suite on GitHub-hosted runners.

## [0.1.0]

First public release: a generalized extract of 536 files (skills, session
configuration modules, Python modules, scripts, documentation templates, Codex role
examples and the test suite), published from a clean root with a signed tag.

[0.1.2]: https://github.com/sergeiwallace/ai-chassis/releases/tag/v0.1.2
[0.1.1]: https://github.com/sergeiwallace/ai-chassis/releases/tag/v0.1.1
[0.1.0]: https://github.com/sergeiwallace/ai-chassis/releases/tag/v0.1.0
