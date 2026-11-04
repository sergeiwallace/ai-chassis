# Changelog

All notable changes to ai-chassis are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[semantic versioning](https://semver.org/spec/v2.0.0.html). While the major version is
0, a minor release may add a subsystem and may change interfaces; a patch release only
fixes defects and documentation.

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

[0.1.1]: https://github.com/sergeiwallace/ai-chassis/releases/tag/v0.1.1
[0.1.0]: https://github.com/sergeiwallace/ai-chassis/releases/tag/v0.1.0
