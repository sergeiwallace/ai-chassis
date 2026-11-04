---
title: "Knowledge Ingestion Procedure"
category: procedure
tags: [curation, knowledge-ingestion, bookmarks, repositories, bot-safety, browser-automation]
status: active
template_version: "procedure-1.0.0"
---

<!-- doc:region name="overview" kind="replaceable" -->

# Knowledge Ingestion Procedure

This is a reliable manual bridge for collecting content a user has signalled interest in, reviewing
it in context, and turning the useful results into reviewable backlog items. It is not a substitute
for a future automated curation system.

## Source pulls

Keep a per-source snapshot and surface only material new since the previous pull, except during a
requested backfill. Read local bookmark data directly when possible; parse timestamps, sort newest
first, and target relevant folders rather than indiscriminately scanning the entire collection.

For repository signals, use an authenticated provider CLI to list recent stars and curated lists.
Diff identifiers against the snapshot, then review the description and primary documentation for
new candidates.

For social-content bookmarks, use the provider's supported export or bookmark client first. Before
syncing, confirm that it is pointed at the existing data store; an accidental empty store can turn an
incremental pull into a risky full crawl. Cap each run and resume a bounded run later rather than
starting over.

## Browser fallback and bot safety

Use browser automation only for content the supported client cannot retrieve. Attach to an existing
authorized browser, do not create or foreground tabs unnecessarily, and prefer a public single-item
endpoint over an authenticated API when it is sufficient.

- Make reads sequential and human-paced; never parallelize aggressive collection.
- Stop on rate limiting or an account challenge. Do not retry in a tight loop.
- Cap a session and spread a full-history backfill across small runs.
- Read only: do not post, follow, like, or otherwise mutate an account.
- Review a bookmarked item in its thread or surrounding discussion where available.

## Bounded exploration

For each seed, explore one hop: a discussion and notable replies, a repository's linked material,
or an article's cited sources. Record material dropped or deferred so a bounded pass is not mistaken
for complete coverage.

## Funnel to durable work

1. Capture raw pulls in a dated curation inbox.
2. Classify each item by its practical type, such as tool, idea, repository, paper, or discussion.
3. Draft durable backlog items for the strongest findings; keep them as drafts pending user review.
4. Present a short ranked digest stating what is new, recommended, dropped, and deferred.

Use scheduled source pulls only after their rate limits, state storage, and review boundaries have
been designed and tested.

<!-- /doc:region name="overview" -->

<!-- doc:region name="steps" kind="replaceable" -->
<!-- /doc:region name="steps" -->

<!-- doc:region name="rationale" kind="replaceable" -->
<!-- /doc:region name="rationale" -->

<!-- doc:region name="revision_log" kind="append_only" -->
<!-- /doc:region name="revision_log" -->
