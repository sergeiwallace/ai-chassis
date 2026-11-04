---
title: [Handoff Title]
category: handoff
tags: [handoff]
status: stub
source: "aido-stub"
template_version: "handoff-1.0.0"
---
<!-- Canonical Jinja source: STUB.md.jinja. This direct-copy stub is retained for consumers that have not migrated to rendering it. -->

# [Handoff Title]

## Founding Ask Coverage

`founding_ask_ref`: [FILL: exact `canonical_root_json` + `founding_ask_ref_sha256` | N/A with reason]

[FILL: When this `founding_ask_ref` is `N/A` for a Tier 1 dispatch with no `canonical_root_json`, paste the literal `verbatim_exact_ask` from the worker brief directly below in a fenced `text` block. Never replace it with a summary or a reference to the transient brief. Do not add this block for Tier 0 or Tier 2/3.]

**Status:** stub

**Created:** YYYY-MM-DD

<!-- doc:region name="overview" kind="replaceable" -->

## Table of Contents

<!-- FILL THIS IN as you write. The entries below are this stub's own placeholder sections, so
     they already resolve; as you add, rename, or remove a heading, update this list in the same
     edit. A ToC that lists headings the doc does not have, or omits ones it does, is worse than
     no ToC because it looks like working navigation.
     The doc's D5 (c) decision: list EVERY `## ` and EVERY `### ` heading in the real doc, with
     GitHub-style anchors (lowercase, spaces→hyphens, punctuation stripped) so they navigate
     in-window (incl. VS Code Remote-SSH). If all-`###` proves too noisy, fall back to D5 (a)
     "meaningful `###`", a deterministic OR-rule: include a `###` when it (1) has child `####`,
     (2) its body ≥ ~8-10 lines, (3) its parent `##` is allowlisted, or (4) matches a pattern.
     Hand-maintained: the not-yet-shipped `aido toc check` tool. Verify with
     `scripts/check_doc_toc.py <this-doc>`, which fails on an anchor that does not resolve.
     Manual `<!-- toc:skip -->` / `<!-- toc:include -->` override. -->

- [Overview](#overview)
- [Current State](#current-state)
- [Context](#context)
- [Next Steps](#next-steps)
- [Open Items](#open-items)
- [Handoff Log](#handoff-log)

## Overview

[What is being handed off and to whom.]

<!-- /doc:region name="overview" -->

<!-- doc:region name="current_state" kind="replaceable" -->

## Current State

[Where things stand right now.]

<!-- /doc:region name="current_state" -->

<!-- doc:region name="context" kind="replaceable" -->

## Context

[Background needed to pick this up.]

<!-- /doc:region name="context" -->

<!-- doc:region name="next_steps" kind="replaceable" -->

## Next Steps

1. [First next step]
2. [Second next step]

<!-- /doc:region name="next_steps" -->

<!-- doc:region name="open_items" kind="replaceable" -->

## Open Items

- [ ] [Unresolved item / decision]

<!-- /doc:region name="open_items" -->

<!-- doc:region name="handoff_log" kind="append_only" -->

## Handoff Log

| Date | From → To | Notes |
|------|-----------|-------|

<!-- /doc:region name="handoff_log" -->
