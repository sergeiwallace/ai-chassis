---
title: "AI Development CLI Reference"
category: procedures
tags: [cli, tools, session-management, sync]
status: active
---

# AI Development CLI Reference

Consult `tool --help` before using an unfamiliar command. The examples below describe portable
command shapes; adapt names, paths, credentials, and host choices to the local installation.

## Session management

```text
tool start [name] [flags]
tool list [--all]
tool attach NAME
tool reconnect [NAME...]
```

Sessions may create isolated worktrees and run under a terminal multiplexer. Typical flags resume a
session, run once, use a bare terminal, send a completion notification, disable worktree isolation,
or select a remote project. Do not disable isolation while another worker may be changing the same
repository.

## One-shot model requests

```text
tool model "prompt" --model MODEL --output result.md
tool model --model MODEL < prompt.txt
```

Choose a model and depth proportional to the task. Record output location, timeout, and any approval
gate before starting costly or high-impact work. Keep credentials in the environment or a secret
manager, never in the command transcript.

## Safe propagation and synchronization

Use preview mode before a multi-repository template update. Prefer an isolated temporary worktree;
inspect the resulting diff and tests before integrating it. For state synchronization, list conflicts
first, resolve only understood conflicts, and use dry-run mode for a first pass.

```text
tool template-update --dry-run
tool sync conflicts
tool sync pull --dry-run
```

## Process and browser tooling

```text
tool processes
tool processes clean
tool browser start --port PORT
tool browser status
tool tunnel status
```

Process cleanup should be interactive by default. Browser automation should attach to an existing
authorized browser when possible, avoid stealing focus, and stop the temporary automation endpoint
when finished. Inspect tunnel direction and endpoints before starting one.

## Maintenance

Update a development CLI through its documented installer, then verify its version and help output.
After changing source, run the project's focused checks before installing it for other environments.
