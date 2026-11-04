---
title: "Hosted CI Retirement"
category: procedure
tags: [ci, hosted-actions, self-hosted-runners, verification]
status: active
template_version: "procedure-1.0.0"
---

<!-- doc:region name="overview" kind="replaceable" -->

# Hosted CI Retirement

When hosted CI has been deliberately retired, its checks are not a code-verification signal. Record
the decision, the temporary verification authority, and the conditions for restoring automation.

## Authoritative verification

Until a self-hosted runner is confirmed live, run and report the repository's local hard gate—for
example lint, format verification, and the full test suite. A hook that only collects tests proves imports and fixture wiring, not that any test body passed.

If a gate uses a cached sentinel, inspect its mode before citing it. `collection-only` is not full
verification; a mode that records executed test bodies may be. Re-run ambiguous or legacy records
rather than inferring stronger evidence from a file name or a commit hash.

## Migration status and workflow handling

Keep the self-hosted CI design as the authority for runner security, operations, and rollout. Track
deployment separately from code readiness. Some actions—such as creating an application integration
or granting organization permissions—may require an authorized human.

Retained workflow files should be manual-only while hosted CI is retired. They must not trigger on
push or pull-request events unless that decision is explicitly reversed. A manual canary becomes a
verification signal only after the runner is confirmed live and trustworthy.

## Session checklist

1. Read the self-hosted CI design before changing CI architecture.
2. Run the local hard gate until live self-hosted verification is confirmed.
3. Classify a hosted-provider failure as infrastructure evidence, not automatically as a code failure.
4. Keep retained workflows manual-only until a documented decision changes that policy.

<!-- /doc:region name="overview" -->

<!-- doc:region name="steps" kind="replaceable" -->
<!-- /doc:region name="steps" -->

<!-- doc:region name="rationale" kind="replaceable" -->
<!-- /doc:region name="rationale" -->

<!-- doc:region name="revision_log" kind="append_only" -->
<!-- /doc:region name="revision_log" -->
