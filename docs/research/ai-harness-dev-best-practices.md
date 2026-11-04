# Development practices for an AI harness

Keep behavior explicit: a harness should make setup repeatable, not hide machine
state. Store portable defaults in version control and keep credentials, local
paths, and account preferences in ignored files.

Test the boundaries that matter. Exercise a fresh clone, an empty local config,
and a changed profile. Prefer small scripts with clear inputs and outputs over
instructions that depend on an unstated shell history.

For AI-assisted work, ask the tool to inspect the relevant files first, make a
focused change, run a proportionate check, and report what remains uncertain.
Review generated text before it leaves the machine; a prompt, log, or example can
contain more context than its author intended to share.
