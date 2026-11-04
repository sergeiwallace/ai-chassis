# Contributing

Use this repository's issue tracker for bugs, questions, and feature requests.
Include a small reproduction, expected behavior, and relevant tool versions.
Remove credentials and identifying configuration from examples and logs.
Report security concerns privately as described in [SECURITY.md](SECURITY.md).

Keep pull requests focused. Use synthetic examples and add regression coverage
when behavior changes. From the repository root, install the development tools
with `uv sync --frozen` and run `uv run pytest`. For Python changes, also run
`uv run ruff check` and `uv run ruff format --check` on the changed Python files.

Some tools are experimental. Experimental-lane issues carry no support
commitment. Contributions must preserve existing third-party license notices;
project contributions are provided under Apache-2.0.

## Provenance

This generalized extract begins at a clean root in the public repository. It is
not the original development history; the pre-launch history is retained
privately. Some components of the original system are intentionally not included.
