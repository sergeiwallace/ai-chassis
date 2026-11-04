# Task Store and Context Ownership

Keep task-tracker state, repository instructions, generated context, and exports under their owners.
A tracker store is authoritative; checked-in exports are derived artifacts, not a second editable
source of truth.

## Store-only initialization

Before enabling a tracker, inspect existing files, hooks, and ignore rules. Use its supported
initialization command or repository wrapper. Do not overwrite existing context, hooks, or
configuration merely because a tool offers to generate one. Keep private task state in an approved
private store and export only fields safe to share.

## Synchronize derived exports

When the store and a versioned export diverge, determine authority before editing:

```bash
tracker status
tracker export --check
git diff -- path/to/derived-export
```

Regenerate from the store when it is authoritative. Do not hand-edit a derived file to silence a
diff; the next export overwrites it. If synchronization rejects a change, distinguish an old local
base from concurrent divergence, reconcile with the supported command, then regenerate and read
the export back.

After initialization, migration, or re-sync, verify both directions: intended records are in the
store and the export represents them. Keep a recoverable backup before migration or resolution.
