---
title: "Analysis Document Template"
category: analysis
tags: [analysis]
status: template
template_version: "analysis-1.0.0"
---

# [Analysis Title]

**Status:** DRAFT

**Created:** YYYY-MM-DD

<!-- doc:region name="overview" kind="replaceable" -->

## Table of Contents

<!-- ISSUE-REF / D5 (c): list EVERY `## ` and EVERY `### ` heading in the real doc,
     with GitHub-style anchors (lowercase, spaces→hyphens, punctuation stripped) so they
     navigate in-window (incl. VS Code Remote-SSH). If all-`###` proves too noisy, fall back
     to D5 (a) "meaningful `###`", a deterministic OR-rule: include a `###` when it (1) has
     child `####`, (2) its body ≥ ~8-10 lines, (3) its parent `##` is allowlisted, or (4)
     matches a pattern. Hand-maintained: `aido toc check` (ISSUE-REF) has NOT shipped, so this
     drifts unless you update it when you add a heading; the doc-TOC consistency-check script verifies the
     anchors resolve. Manual `<!-- toc:skip -->` / `<!-- toc:include -->` override. -->

- [Overview](#overview)
- [Context](#context)
- [Options](#options)
- [Recommendation](#recommendation)
- [Revision Log](#revision-log)

## Overview

[What this analysis covers and the question it answers.]

<!-- /doc:region name="overview" -->

<!-- doc:region name="context" kind="replaceable" -->

## Context

[Background/situation motivating the analysis.]

<!-- /doc:region name="context" -->

<!-- doc:region name="options" kind="replaceable" -->

## Options

[Options considered, each with pros/cons.]

<!-- /doc:region name="options" -->

<!-- doc:region name="recommendation" kind="replaceable" -->

## Recommendation

<!-- Recommendation-vs-choice tracking (ISSUE-REF): state the AI's recommended option + why below.
     When the maintainer decides, record his Chosen option and, if it Diverged from the recommendation,
     WHY, right here. If the rec was corrected mid-discussion, keep the original + its reasoning
     (the correction is signal). See ai-harness docs/procedures/decision-framework.md. -->

**Recommended (AI):** [option and why]

**Chosen (the maintainer):** [fill on decision] · **Diverged?** [Yes / No; if Yes, why]

<!-- /doc:region name="recommendation" -->

<!-- doc:region name="revision_log" kind="append_only" -->

## Revision Log

| Date | Change | Notes |
|------|--------|-------|

<!-- /doc:region name="revision_log" -->
