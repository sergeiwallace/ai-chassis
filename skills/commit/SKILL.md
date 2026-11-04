---
name: commit
description: Create a provenance-validated commit through the sole supported author-facing procedure.
---

# commit

`/commit` is the sole author-facing commit procedure. Do not hand-assemble trailers or use
`git commit -m`. A genuine launcher publishes only an opaque `AIH_RUN_HANDLE`; the constructor
derives the signed, tree-bound receipt from that handle at the final staged tree:

```sh
provenance-commit --title "type: summary" --body "optional detail" \
  --no-delegation
```

**For anything longer than a line or two, use `--body-file` instead of `--body` (ISSUE-REF).**

```sh
provenance-commit --title "type: summary" --body-file <path> --no-delegation
```

`--body "$(cat <path>)"` is **not** an alternative: it is command substitution feeding a
write-capable tool, and a worktree-isolated agent's runtime refuses it with a message about git
that names no offending token, so the author bisects the body and finds nothing. A spelled-out
path verifies at any size. It also keeps the body off the shell, so apostrophes, backticks and
`$` survive instead of forcing censored prose. Cause, measurements and remedies:
[📄 ai-harness/docs/procedures/worktree-workflow.md](../../docs/procedures/worktree-workflow.md).

For delegated work, replace `--no-delegation` with one or more
`--delegation-receipt "$AIH_DELEGATION_RECEIPT"` arguments. Receipts must be in the user-private provenance store;
capture the exact path that `cx` prints on stderr at delegation exit
(`cx: delegation-receipt=<path>`) and pass that path to `provenance-commit`. The authoring-receipt
constructor checks signed run context, launcher ancestry, repository/worktree/tree binding, issuer
signature, and immutable owner-only (`0400`) permissions. A delegation-receipt constructor instead
checks its signature, repository, repository-relative logical worktree, 24-hour freshness, and an
atomic single-use claim; launcher ancestry is checked when `cx` issues the receipt, not when it is
redeemed. Exactly one of racing constructors can redeem a receipt for one commit, and a failed
commit releases its claim for retry. Hand-assembled `Delegation-*` trailers remain forbidden: use a
real receipt or `--no-delegation`. A delegation trailer records that a delegation happened; it never
means that the output was reviewed or accepted. A successful Git commit consumes its receipt.

When recreating a commit during a rebase, construct every recreated commit with current receipts
and include its original Git object ID:

```sh
provenance-commit --title "..." --no-delegation --reconstruct-from <original-commit-oid>
```

`--reconstruct-from` is required for every recreated object; `--rewritten-from` is not an
author-facing bypass. This applies equally to sources created before and after the v2 cutoff.

Use `--dry-run` to inspect rendered bytes before committing. It validates but does not consume the
receipt.
