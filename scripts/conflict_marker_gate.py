#!/usr/bin/env python3
"""Reject a commit that stages a git conflict marker in ANY text file.

This is the braces to the belt described in ``development-workflow.md`` §
"Gate the mutation on the guard". A resolver that refuses, or crashes, leaves the
file in its DANGEROUS state; the very next ``git add`` then stages markers, and
every exit code along the way reads fine. Measured 2026-08-19: a guard correctly
declined to write a resolved ``.beads/issues.jsonl``, an unconditional ``git add``
followed on the next line, and **12 conflict markers** were committed.

WHY THE STOCK ``check-merge-conflict`` HOOK DOES NOT COVER THIS.

``.pre-commit-config.yaml`` already runs pre-commit-hooks' ``check-merge-conflict``
(v6.0.0). Read as "markers are handled", it is not: three independent blind spots,
each read out of the installed source on 2026-08-19.

1. **It no-ops unless a merge is IN PROGRESS.** ``main()`` calls ``is_in_merge()``
   and returns 0 immediately when it is false -- that needs ``MERGE_MSG`` *and* one
   of ``MERGE_HEAD``/``rebase-apply``/``rebase-merge`` in the git dir. So markers
   arriving by any other route pass silently: written by a script, left by a
   refused resolver, hand-typed, or simply CARRIED FORWARD by an ordinary commit
   after the merge itself concluded. That last case is the measured one -- the
   ai-harness marker episode traced in ``bd_jsonl_parse_gate.py`` survived 14
   commits, and only the first of them could have been inside a merge.
2. **It reads the WORKING TREE, not the index** (``open(filename, 'rb')``). A
   partially-staged edit is judged on bytes that will not be committed, which is
   the wrong file in both directions.
3. **This repo's top-level ``exclude: ^\\.beads/`` removes it from the beads
   mirrors entirely** -- the file class where the failure actually happened.

This gate fixes all three: unconditional (no merge precondition), index-read, and
scoped by containment rather than by name so nothing is excluded. The overlap with
``bd_jsonl_parse_gate`` on ``.beads/*.jsonl`` is deliberate and cheap; a rule about
belts and braces should not ship with a hole where its own incident occurred.

NOISE. The patterns are pre-commit-hooks' own, byte-exact, and they are well tuned:
seven ``=`` must be followed by a space or an end of line, so a markdown ``=====``
rule, an ``========`` divider comment, and a ``"======="`` string literal all fail
to match. Markers are also only recognised at column 0, where git writes them, so
an indented marker inside a test fixture or a quoted example does not fire.
Measured before shipping: scanning every tracked file in this repo with exactly
these patterns produced ZERO hits, so the gate ships with no exception list and
needs none.

Usage (as a git hook, cwd = the repo, no args):
  conflict_marker_gate.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# pre-commit-hooks' CONFLICT_PATTERNS verbatim (v6.0.0,
# pre_commit_hooks/check_merge_conflict.py). Kept byte-exact rather than
# reimplemented as a regex: the trailing space and the two line-ending forms are
# what hold false positives at zero, and "seven equals signs" written freehand
# loses that.
CONFLICT_PATTERNS = (
    b"<<<<<<< ",
    b"======= ",
    b"=======\r\n",
    b"=======\n",
    b">>>>>>> ",
)

# A NUL in the head of a blob means binary. git's own heuristic, and the reason a
# PNG or a compiled artifact is not line-scanned.
_BINARY_SNIFF_BYTES = 8000


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """Run git against `cwd` as BYTES, immune to an ambient git context.

    Git EXPORTS ``GIT_DIR``/``GIT_INDEX_FILE``/``GIT_WORK_TREE`` into hook
    subprocesses and a child git honours them over ``cwd``, so a hook-invoked child
    would inspect the hook's repo instead of the one being committed. Reading the
    right repo's staged state is this gate's whole job, so strip them (same
    rationale as ``bd_jsonl_parse_gate._git``).

    Bytes, not text: a blob may be binary or invalid UTF-8, and decoding it only to
    scan for ASCII markers can raise on content this gate must simply skip.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, check=False, env=env
    )


def repo_root(start: Path, *, runner=_git) -> Path:
    """The worktree root containing `start`."""
    top = runner(["rev-parse", "--show-toplevel"], start)
    if top.returncode != 0:
        raise RuntimeError(
            f"'git rev-parse --show-toplevel' failed: {top.stderr.decode(errors='replace').strip()}"
        )
    return Path(top.stdout.decode().strip())


def staged_paths(repo: Path, *, runner=_git) -> list[str]:
    """Every path this commit adds, copies, modifies or renames, in git's order.

    ``--diff-filter=ACMR`` drops deletions: a deleted path has no index blob to
    read, and a file being removed cannot carry a marker into the commit.
    """
    staged = runner(
        ["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], repo
    )
    if staged.returncode != 0:
        raise RuntimeError(
            f"'git diff --cached' failed: {staged.stderr.decode(errors='replace').strip()}"
        )
    return [
        entry.decode("utf-8", errors="replace")
        for entry in staged.stdout.split(b"\0")
        if entry
    ]


def staged_blob(repo: Path, path: str, *, runner=_git) -> bytes:
    """The staged (index) bytes of `path`.

    ``:./`` is not used; pre-commit runs hooks at the repo root and the paths come
    from ``git diff --cached``, which reports them relative to the root.
    """
    show = runner(["show", f":{path}"], repo)
    if show.returncode != 0:
        raise RuntimeError(
            f"'git show :{path}' failed: {show.stderr.decode(errors='replace').strip()}"
        )
    return show.stdout


def find_markers(blob: bytes) -> list[tuple[int, str]]:
    """``(line number, marker)`` for every conflict marker in `blob`; ``[]`` == clean.

    Binary blobs return ``[]`` rather than raising, so one unscannable file cannot
    take the gate down for the whole commit.
    """
    if b"\0" in blob[:_BINARY_SNIFF_BYTES]:
        return []
    found: list[tuple[int, str]] = []
    for line_no, line in enumerate(blob.splitlines(keepends=True), start=1):
        for pattern in CONFLICT_PATTERNS:
            if line.startswith(pattern):
                found.append((line_no, pattern.strip().decode()))
                break
    return found


_REMEDIATION = """
Do NOT resolve this by picking a side, and do NOT stage the file again without
reading it. The two sides of a divergent generated file routinely share no records
at all, so either choice deletes real work.

Reach for the tool that already ships before hand-rolling a resolver:

  .beads/*.jsonl   ->  scripts/bd_jsonl_merge.py resolve <repo> <rel-path>
                       (per-record newest-wins; refuses on id loss or a marker)

And gate the mutation on the guard: `resolver && git add <path>`, never a resolver
on one line and an unconditional `git add` on the next. A refused or crashed
resolver leaves the file in exactly the state you must not commit, which is how
this gate came to exist.

Full rule: docs/procedures/development-workflow.md § Gate the mutation on the guard
"""


def main(argv: list[str] | None = None) -> int:
    try:
        repo = repo_root(Path.cwd())
        paths = staged_paths(repo)
    except RuntimeError as exc:
        print(f"error: conflict-marker-gate: {exc}", file=sys.stderr)
        return 1
    if not paths:
        return 0

    failed: dict[str, list[tuple[int, str]]] = {}
    for path in paths:
        try:
            blob = staged_blob(repo, path)
        except RuntimeError as exc:
            print(f"error: conflict-marker-gate: {exc}", file=sys.stderr)
            return 1
        markers = find_markers(blob)
        if markers:
            failed[path] = markers

    if not failed:
        return 0

    print(
        "conflict-marker-gate: REJECTED -- staged content carries unresolved git conflict markers:",
        file=sys.stderr,
    )
    for path, markers in failed.items():
        print(f"  {path}", file=sys.stderr)
        for line_no, marker in markers:
            print(f"    L{line_no}: {marker!r}", file=sys.stderr)
    print(_REMEDIATION, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
