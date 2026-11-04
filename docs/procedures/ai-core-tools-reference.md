---
title: "MCP and CLI Tool Reference"
category: procedures
tags: [mcp, cli, token-efficiency, tools]
status: active
---

# MCP and CLI Tool Reference

Use in-context tools for decisions, small reads, writes requiring confirmation, and semantic
search. Prefer a command-line interface for large, scriptable reads that do not need to occupy the
model context. Keep this reference aligned with the actual tool surface.

## Tool-selection guide

| Work | Prefer | Why |
|---|---|---|
| Small status, one record, dependency graph | MCP | Structured results are useful in context. |
| Broad queues, digests, or unconstrained task queries | CLI | Bound and filter output before reading it. |
| Semantic or cross-project search | MCP | Results directly support current reasoning. |
| State-changing task operations | MCP | The caller can confirm the exact mutation. |
| Ad-hoc database investigation | Local script | It avoids a broad tool response. |

Always constrain broad queries with a project, status, and limit. An unconstrained query can return
an entire task database and conceal the result that matters.

## Common capability families

Read tools commonly provide project lists, health summaries, active sessions, stale tasks,
dependency graphs, convention drift, roadmaps, queues, and daily digests. Search tools commonly
provide keyword, semantic, durable-learning, cross-project, and code search. Write tools commonly
link tasks, update task fields, toggle completion, store a durable learning, or refresh an index.

For any write, state the target and consequence before calling it. A durable learning with a broad
scope can affect future work outside the current repository.

## CLI conventions

Use the tool's help output first, then select a narrow command such as:

```text
tool tasks --project PROJECT --limit N
tool priorities --brief
tool digest --brief
tool health
tool stale
tool roadmap PROJECT
```

Keep the CLI/MCP parity matrix current: a high-volume read may merit a CLI equivalent; a low-volume,
semantic, or confirmation-gated operation usually remains better as an MCP tool.

## Maintenance

When a tool changes:

1. Classify it as low-volume read, heavy read, semantic search, or write.
2. Decide whether a CLI equivalent would make bounded scripted use safer.
3. Update this reference and its parity record.
4. Reconnect the client after updating a locally started MCP server; registration alone does not
   prove that an existing session is using the new executable.
