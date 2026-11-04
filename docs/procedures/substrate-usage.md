# Memory Substrate Usage

Human-readable markdown is authoritative memory. Search indexes and databases are rebuildable
retrieval aids, never independent sources of truth.

## Read through the appropriate surface

Use automatic context for routine continuity and on-demand recall for an explicitly requested wider
search. Inspect provenance before using a result and state what was recalled. An honest recall
announcement includes `query`, `tier`, `hits`, `sources`, `scores`, `used`, and `cost`; include
`suppressed` when a retraction filter withheld results.

## Write memory only in markdown

Edit the owning markdown memory file, then let the reconciler index it. Do not write directly to
index tables or a search database: that creates a second authority and will be overwritten by the
next reconciliation.

## Health and degraded operation

Check health once at session start using the supported health command or endpoint. Classify it as
`green`, `degraded`, or `unreachable`. Store only a time-bounded, session-scoped degraded flag; a
healthy result clears an old flag. If retrieval is unavailable, continue from repository files and
say recall is degraded rather than guessing from stale context.

## Retraction convention

Retract a memory by changing its markdown frontmatter and letting reconciliation project it. Query
with an explicit active-status allowlist, not a denylist: include active or unmarked records only.
This keeps a newly retracted record from surfacing before every caller learns a bespoke exclusion.
