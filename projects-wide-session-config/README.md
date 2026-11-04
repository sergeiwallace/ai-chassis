# Projects-wide session configuration

This directory is the source-checkout compatibility layout for compiled
projects-wide context files. It is separate from the public `ai-harness`
command-line workflow described in the repository root README. Use it when a
checkout needs to assemble context files in place for more than one AI tool or
machine.

## Layering

The compiler assembles one generated file for each configured tool and machine:

```text
modules/agents-base.md
  + modules/{agent}-delta.md
  + modules/{agent}-{machine}.md
  -> compile.py
  -> CLAUDE.{machine}.md or GEMINI.{machine}.md
  -> install.sh
```

`agents-base.md` contains instructions shared by all tools and machines. Each
agent delta contains instructions specific to one tool. A machine overlay holds
only the facts and operating differences that apply to one environment. The
compiler concatenates those layers into self-contained `CLAUDE.{machine}.md`
and `GEMINI.{machine}.md` outputs. `install.sh` selects the output for the
current machine and installs it.

Generated files are outputs, not source files. Edit the modules, run the
compiler, then run the installer. A running session may need to be restarted or
reloaded before it sees the new generated context.

## Add a machine overlay

1. Copy [claude-example.md](claude-example.md) to
   `modules/claude-{machine}.md` and [gemini-example.md](gemini-example.md) to
   `modules/gemini-{machine}.md`.
2. Replace the synthetic examples with the safe, local instructions that differ
   for that machine. Keep shared instructions in `agents-base.md` and tool-only
   instructions in the matching delta.
3. Add the machine and its overlay modules to the local configuration, following
   [config.example.toml](config.example.toml).
4. Run `python3 compile.py`, inspect the generated files, and run
   `python3 check-sizes.py --gate`.
5. Run `bash ../install.sh` from the repository root to install the generated
   result.

Do not put account names, real hostnames, network addresses, internal URLs,
tokens, secrets, customer data, or copied internal runbooks in a committed
overlay. Keep sensitive machine details in local configuration or protected
documentation.
