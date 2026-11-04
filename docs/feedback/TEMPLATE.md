---
title: "Feedback Document Template"
category: feedback
tags: [feedback]
status: template
template_version: "feedback-1.0.0"
---

<!-- doc:region name="summary" kind="replaceable" -->

# [Feedback Title]

**Status:** draft-awaiting-submission | submitted | closed

**Report type:** bug | feature request

**Created:** YYYY-MM-DD

**Route:** feedback | claude | codex

<!-- Optional fields, add as needed:
**Public destination:** `<owner/repository>` (visibility)
**Submission URL:** `<URL>`
**Tracking issue / CC task mirror:** `<identifier(s)>`
-->

## Table of Contents

<!-- List every `##` and meaningful `###` heading in the completed document using GitHub-style
anchors. Keep this list current whenever headings change; verify with
`scripts/check_doc_toc.py <this-doc>`. -->

- [Summary](#summary)
- [Report Details](#report-details)
- [Rendered Drafts](#rendered-drafts)
  - [Public GitHub Draft](#public-github-draft)
  - [Ready-to-Copy `/feedback` Draft](#ready-to-copy-feedback-draft)
- [Submission Tracking](#submission-tracking)
- [Appendix: Evidence](#appendix-evidence)

## Summary

<!-- One paragraph: report type, affected product or workflow, user impact, and requested outcome. -->

<!-- /doc:region name="summary" -->

<!-- doc:region name="report_details" kind="replaceable" -->

## Report Details

<!-- Record observed and expected behavior for bugs; for feature requests, record the requested
outcome and the workflow it improves. Include reproduction steps only for bugs. Record product
version, complete OS version, relevant runtime context, and the redaction check result. -->

<!-- /doc:region name="report_details" -->

<!-- doc:region name="rendered_drafts" kind="replaceable" -->

## Rendered Drafts

### Public GitHub Draft

<!-- Render the selected type-specific report-feedback template's unmarked + github-only sections. Never include
session IDs, account/org identifiers, private telemetry, or `/feedback` content. When a direct
GitHub route is selected, copy this exact approved render to the durable sibling
`docs/feedback/<slug>.github.md` for `gh --body-file`. -->

### Ready-to-Copy `/feedback` Draft

<!-- Render the selected type-specific report-feedback template's unmarked + feedback-only sections. Include
session/account identifiers only when available and relevant. Never place this private content in
the public GitHub render. The skill never invokes `/feedback`; the human pastes it manually. -->

<!-- /doc:region name="rendered_drafts" -->

<!-- doc:region name="submission_tracking" kind="append_only" -->

## Submission Tracking

<!-- Append each delivery/tracking event. Filing a Beads issue plus CC task mirror for the drafted
report awaiting submission is unconditional. If the owning store is unavailable, append the plain
failure instead of implying the tracking step was completed. -->

| Date | Route | Beads issue + CC task mirror | Submission status |
|------|-------|------------------------------|-------------------|
| YYYY-MM-DD | | | draft awaiting submission |

<!-- /doc:region name="submission_tracking" -->

<!-- doc:region name="appendix_evidence" kind="immutable" -->

## Appendix: Evidence

<!-- Frozen sanitized evidence: screenshots, logs, reproduction transcript, or explanation that
evidence is not applicable. -->

<!-- /doc:region name="appendix_evidence" -->
