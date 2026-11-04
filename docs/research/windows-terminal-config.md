---
title: "Windows Terminal configuration for Claude Code TUI input and selection"
category: research
tags: [research, windows-terminal, claude-code]
status: complete
source: "legacy-local-investigation-2026-07-31"
template_version: "research-1.2.0"
---

# Windows Terminal configuration for Claude Code TUI input and selection

## Founding Ask Coverage

`founding_ask_ref`: N/A — this is a migration of a 2026-07-31 local investigation; its original prompt was not retained.

**Status:** complete

**Created:** 2026-07-31

## Table of Contents

- [Context](#context)
- [Temporal Scope](#temporal-scope)
- [Executive Summary](#executive-summary)
- [1. Shift+Enter input](#1-shiftenter-input)
- [2. Copy on select](#2-copy-on-select)
- [3. Mouse reporting and selection](#3-mouse-reporting-and-selection)
- [Recommendation](#recommendation)
- [Open Questions](#open-questions)
- [Sources](#sources)
- [Ambiguous Items from Auto-Remediation (Post-Run Review)](#ambiguous-items-from-auto-remediation-post-run-review)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

<!-- doc:region name="context" kind="immutable" -->

## Context

This local investigation recorded a Windows Terminal configuration intended to let Shift+Enter send an escape-prefixed carriage return to Claude Code and to copy terminal selections automatically. It is a historical configuration note, not a claim that the configuration has been revalidated against current Claude Code releases.

**Primary period:** 2026-07
**Source weighting:** Microsoft Windows Terminal documentation; local observations are marked separately.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Temporal Scope

The configuration and local file observations date to 2026-07-31. Microsoft documentation is cited for Windows Terminal settings syntax and behavior; assertions about Claude Code handling remain unverified unless independently tested.

## Executive Summary

Windows Terminal can send arbitrary text to the shell through `sendInput`, including an ESC-prefixed sequence, and can enable `copyOnSelect`. [VERIFIABLE][^1][^2] The former is a plausible configuration for a TUI that recognizes ESC+CR as a newline gesture, but Claude Code recognition was not verified in the original investigation. [NO SOURCE]

## 1. Shift+Enter input

The recorded keybinding used Windows Terminal's `sendInput` action with an ESC-prefixed carriage return:

```json
{
  "command": {
    "action": "sendInput",
    "input": "\u001b\r"
  },
  "keys": "shift+enter"
}
```

Windows Terminal documents `sendInput` as arbitrary text sent to the shell and documents escape-sequence input. [VERIFIABLE][^1] Whether Claude Code interprets this exact sequence as “insert newline” was inferred from the original task framing and needs an interactive check. [NO SOURCE]

## 2. Copy on select

The recorded global setting was:

```json
"copyOnSelect": true
```

Windows Terminal documents that this setting copies a selection to the clipboard when the selection is created. [VERIFIABLE][^2]

## 3. Mouse reporting and selection

The original investigation expected Shift-drag to permit terminal-level selection while a TUI captures ordinary mouse input. This behavior was not interactively verified in Windows Terminal with Claude Code, so it should be treated as a test instruction rather than an established result. [NO SOURCE]

The local configuration file observed during the investigation was:

```text
/c/Users/example-user/AppData/Local/Packages/Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/settings.json
```

The backup command recorded at the time was:

```bash
cp /c/Users/example-user/AppData/Local/Packages/Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/settings.json \
  /c/Users/example-user/AppData/Local/Packages/Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/settings.json.bak-devenv
```

These paths are historical local observations, not portable defaults. [NO SOURCE]

## Recommendation

Use the two settings as an opt-in starting point, then verify Shift+Enter and Shift-drag in the actual terminal and Claude Code version being used. Preserve the original `settings.json` before editing it. [INFERENCE]

## Open Questions

1. Does the current Claude Code TUI recognize ESC+CR from Windows Terminal as newline insertion?
2. Which modifier, if any, reliably bypasses mouse reporting for selection in the current Windows Terminal and Claude Code versions?

## Sources

[^1]: Microsoft. (2025). [Windows Terminal Actions](https://learn.microsoft.com/en-us/windows/terminal/customize-settings/actions). Microsoft Learn. Verified accessible 2026-07-31. (`sendInput` action and escape-sequence syntax.)
[^2]: Microsoft. (2025). [Windows Terminal Interaction Settings](https://learn.microsoft.com/en-us/windows/terminal/customize-settings/interaction). Microsoft Learn. Verified accessible 2026-07-31. (`copyOnSelect` behavior.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none — this document was migrated manually from a legacy report.)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

This research doc predates the prompt appendix template. The original prompt and model information are not available.

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

| Claim | Verbatim support | Source | Verdict |
|---|---|---|---|
| `sendInput` accepts arbitrary shell input and escape sequences | Windows Terminal Actions documents both behaviors. | [^1] | Supported |
| `copyOnSelect` copies a newly created selection | Windows Terminal Interaction Settings describes that behavior. | [^2] | Supported |
| Claude Code accepts ESC+CR as newline insertion | No original direct test or primary source was retained. | N/A | Unverified |

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

- 2026-07-31 — Original local Windows Terminal investigation.
- 2026-09-17 — Migrated to research-1.2.0 structure; unsupported claims were retained as unverified rather than promoted to cited findings.

<!-- /doc:region name="run_history" -->
