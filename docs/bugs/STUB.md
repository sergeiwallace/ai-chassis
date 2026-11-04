---
title: [Bug Title]
category: bug
tags: [bug]
status: stub
source: "aido-stub"
template_version: "bug-1.0.0"
---
<!-- Canonical Jinja source: STUB.md.jinja. This direct-copy stub is retained for consumers that have not migrated to rendering it. -->

# [Bug Title]

## Founding Ask Coverage

`founding_ask_ref`: [FILL: exact canonical JSON + digest | N/A with reason]

[FILL: When this `founding_ask_ref` is `N/A` for a Tier 1 dispatch with no `canonical_root_json`, paste the literal `verbatim_exact_ask` from the worker brief directly below in a fenced `text` block. Never replace it with a summary or a reference to the transient brief. Do not add this block for Tier 0 or Tier 2/3.]

[FILL: account for every raw ask entry. This index is non-authoritative; the raw FAR governs.]

**Status:** stub

**Created:** YYYY-MM-DD

<!-- doc:region name="summary" kind="replaceable" -->

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

- [Summary](#summary)
- [Reproduction](#reproduction)
- [Root Cause](#root-cause)
- [Fix Log](#fix-log)
- [Appendix: Evidence](#appendix-evidence)

## Summary

[One-paragraph summary of the bug + user-visible impact.]

<!-- /doc:region name="summary" -->

<!-- doc:region name="reproduction" kind="replaceable" -->

## Reproduction

[Minimal steps to reproduce.]

<!-- /doc:region name="reproduction" -->

<!-- doc:region name="root_cause" kind="replaceable" -->

## Root Cause

(filled after investigation)

<!-- /doc:region name="root_cause" -->

<!-- doc:region name="fix_log" kind="append_only" -->

## Fix Log

| Date | Commit | Notes |
|------|--------|-------|

<!-- /doc:region name="fix_log" -->

<!-- doc:region name="appendix_evidence" kind="immutable" -->

## Appendix: Evidence

[Stack traces, logs, screenshots, repro session transcript; frozen.]

<!-- /doc:region name="appendix_evidence" -->
