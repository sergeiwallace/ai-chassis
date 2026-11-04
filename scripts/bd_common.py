#!/usr/bin/env python3
"""Shared repo-root / issue-prefix resolution for the bd dual-ID assigner tools
(ISSUE-REF Phase-4 harness glue, migration-plan D-10).

``bd-new`` (bd_new.py) and ``bdx`` (bdx.py) both need to know which repo's bd
store they are operating against and that repo's issue prefix. Both are
self-contained here — the prefix is read straight from the target repo's own
bd store (``bd config get issue_prefix``), never guessed from a directory name
or a prefix-manifest file that lives in another repo (the maintainer's fleet manifest
is out of scope for ai-harness tools).

See docs/plans/beads-phase3-migration-plan.md D-10 for the
3-layer dual-ID defense this module's callers implement Layer 1/2 of.

Also owns ``row_display_id`` — the ONE accessor that says where a record's
readable display id lives — and ``display_id_migration_hint``, the ONE
compatibility message every "no display id here" failure path prints. The home
is the bd metadata field ``display_id`` and NOTHING ELSE (ISSUE-REF D-1(b), a
hard cutover): ``external_ref`` is no longer read as a display id anywhere, so
it is free to hold a Jira key. A store that has not been migrated therefore
cannot resolve display ids at all, which is why the hint exists and why it names
the migration script by path.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

# Advisory-lock backend, selected by what the interpreter actually provides
# rather than by a platform string. CPython ships ``fcntl`` on POSIX and
# ``msvcrt`` on Windows; neither is importable on the other, so an unguarded
# ``import fcntl`` is an ImportError at module load on Windows -- which is how
# bd-new became unusable there (ISSUE-REF). Probing the module keeps this correct
# on any interpreter/OS combination without a ``sys.platform`` branch.
try:  # POSIX: Linux, macOS
    import fcntl as _fcntl

    _msvcrt = None
except ImportError:  # Windows
    _fcntl = None
    import msvcrt as _msvcrt


#: ISSUE-REF. ``bd``'s auto-export stages the mirror with a ``git add`` under a hardcoded
#: 5s timeout. On EFS that is routinely exceeded, and a killed ``git add`` is exactly what
#: leaves an orphaned ``.git/index.lock`` behind -- after which EVERY later ``bd`` write's
#: export fails, so the Dolt store advances while ``.beads/issues.jsonl`` silently does
#: not. Two such locks were measured 9.2 h and 10.5 h old (ISSUE-REF, closed into
#: ISSUE-REF).
#:
#: This suppresses the STAGING, not the export: ``.beads/issues.jsonl`` is still written on
#: every write, so the mirror converges and the divergence gates keep seeing fresh JSONL.
#: Only the redundant auto-staging goes away, and staging is already explicit everywhere in
#: this fleet by design -- ``bd_jsonl_merge.py`` prints "NOT staged -- `git add` is a
#: separate command, deliberately", and ``bd_prepush_divergence.py`` tells the operator to
#: run ``git add .beads/issues.jsonl``. Disabling ``export.auto`` instead would stop the
#: JSONL being written at all. Mechanism carried from the unlanded
#: ``origin/wt/indexlock-fix``.
BD_GIT_ADD_SUPPRESSION = {"BD_EXPORT_GIT_ADD": "false"}

#: Stops bd deriving a Dolt remote from git origin on its own (ISSUE-REF, ISSUE-REF).
#:
#: bd adopts a remote from git origin when a push or pull finds none configured, and
#: adoption WRITES ``sync.remote`` into the tracked ``.beads/config.yaml`` and commits
#: it -- three such bookkeeping commits per repo were measured on example-client-host-mac 2026-10-08.
#: The correct URL is per-machine (HTTPS-with-a-PAT here, an SSH host alias on
#: example-project-ec2), so a tracked value is wrong on every host but the one that wrote it.
#: Provisioning is instead machine-local, into the server's own ``dolt_remotes`` table
#: (``dolt_server.add_store_remote``), and adoption must never race it.
#:
#: This is the ENV form on purpose. ``bd dolt push`` takes ``--no-adopt``, but ``bd dolt
#: pull`` has no such flag -- measured 2026-10-07, its only flags are ``--remote`` and
#: ``--strategy`` -- so a per-call-site flag cannot close the pull path at all.
#: ``BD_NO_REMOTE_ADOPT=1`` is documented by ``bd dolt push --help`` as disabling
#: adoption entirely, and it covers every subcommand through one funnel.
BD_REMOTE_ADOPT_SUPPRESSION = {"BD_NO_REMOTE_ADOPT": "1"}


def bd_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """``base`` (default ``os.environ``) plus bd's auto-export ``git add`` and Dolt-remote
    adoption suppressions.

    Pass the result as the ``env=`` of every subprocess that runs ``bd``, so a bd write on
    a slow filesystem cannot orphan ``.git/index.lock``. There is no central funnel to put
    this behind -- measured 2026-09-07, only 3 of 19 bd-invoking modules route through
    ``bd_invoke``, and just 1 of 33 direct call sites passed ``env=`` at all -- so
    ``tests/test_git_index_lock_reaper.py`` gates every call site against this helper
    instead, and a new site that forgets it fails the build.

    Returns a copy: mutating the result never touches ``os.environ`` or ``base``.
    """
    environment = dict(os.environ if base is None else base)
    environment.update(BD_GIT_ADD_SUPPRESSION)
    environment.update(BD_REMOTE_ADOPT_SUPPRESSION)
    return environment


#: Substrings ``bd`` uses when it could not stage because the index was locked. Matching the
#: message is deliberate: bd's exit code is wrong here anyway (ISSUE-REF -- it exits non-zero
#: on an auto-export problem while the store write itself landed), so the text is the only
#: signal separating this recoverable failure from a real one.
INDEX_LOCK_MARKERS = ("index is locked", "index.lock", "git add timed out")


def mentions_index_lock(*texts: str | None) -> bool:
    """True when any of ``texts`` names a locked index."""
    haystack = "\n".join(text for text in texts if text).lower()
    return any(marker in haystack for marker in INDEX_LOCK_MARKERS)


def reap_orphaned_index_lock(repo: Path) -> str | None:
    """Remove a PROVABLY orphaned index lock in ``repo``; return the verdict's reason.

    Defined here, and only here, so every bd entry point recovers by the same rule. The
    prevention half (``bd_env``) stops *bd* orphaning a lock, but a killed ``git commit``,
    a killed pre-commit hook or an unrelated tool can still leave one, and then every later
    bd write in that repo fails until someone removes it by hand -- which is the whole cost
    this exists to stop paying.

    Imported lazily and failing quietly by design: recovery is a best-effort add-on to a
    call that has ALREADY failed, so a problem in here must leave the caller's own error as
    the reported one rather than replacing it with a less informative failure. Safety is the
    reaper's, not ours -- it refuses unless the lock is frozen AND no holder can be found AND
    no rebase/merge/cherry-pick is in progress, and an unreadable ``/proc`` reaps nothing.
    """
    try:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import git_index_lock_reaper as reaper
    except ImportError:  # pragma: no cover - the reaper ships alongside this module
        return None
    try:
        verdict = reaper.reap_if_orphaned(repo)
    except (OSError, RuntimeError, ValueError):
        return None
    return verdict.reason if verdict.action == "reaped" else None


#: The bd metadata key holding a record's readable display id. Single-valued by
#: construction, which a label set is not -- a record can carry two ``id:`` labels and
#: there is then no fact of the matter about which is the display id (live defect
#: ISSUE-REF). Set with ``bd update <hash> --set-metadata display_id=<slug>``; filter with
#: ``bd list --metadata-field display_id=<slug>`` (indexed exact match).
DISPLAY_ID_METADATA_KEY = "display_id"


def row_display_id(row: object) -> str | None:
    """One row's readable display id, read from ``metadata['display_id']`` ONLY.

    Returns ``None`` — never ``""``, never a guess — when the row carries no such
    value, so a caller cannot confuse "unset" with a legitimate empty string.
    Tolerates every shape a real ``bd list --json`` / ``bd show --json`` row has
    been seen to take: ``metadata`` absent, ``null``, or not a mapping; the value
    absent, ``null``, non-string, or blank.

    ``external_ref`` is deliberately NOT a fallback rung (ISSUE-REF D-1(b)). A
    fallback would silently keep an un-migrated store working, which is precisely
    how the two homes drifted; and post-cutover ``external_ref`` legally holds a
    Jira key, so reading it as a display id would resolve ``AIHARN-123`` to a
    record whose display id is something else. The compatibility story is the
    explicit, actionable ``display_id_migration_hint`` instead.

    ``bd export``'s JSONL rows carry no ``metadata`` key at all (measured
    2026-10-02, bd 1.3.x — and export has no option to include it), so this
    accessor returns ``None`` for every row read from ``.beads/issues.jsonl``.
    That is correct rather than a bug: a JSONL-sourced caller must fall back to
    the ``id:`` label echo and say so, never report the whole mirror as missing
    its display ids.
    """
    if not isinstance(row, dict):
        return None
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(DISPLAY_ID_METADATA_KEY)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def display_id_migration_hint(repo: Path, *, missing: int | None = None) -> str:
    """The ONE compatibility message for "this store has no ``display_id`` metadata".

    Every failure path in this repo that cannot resolve a readable display id
    prints exactly this (ISSUE-REF D-1(b)'s mandatory mitigation), on any machine
    and for any repo's store, so an operator who hits the hard cutover with an
    un-migrated store is told what to run instead of being left with a bare
    "no issue found". ``missing`` is the record count when the caller counted it;
    callers that only know "at least one" pass ``None`` and get "some".

    The order is load-bearing and is not a style choice: syncing first can make
    the migration unnecessary (another machine may already have migrated and
    published the store), and migrating a stale local copy first creates
    divergence that then has to be merged.

    The repo path is ``shlex.quote``d in the printed commands. These lines exist to be
    COPY-PASTED into a shell, so a path holding a space silently becomes two arguments
    and the reader gets an error about a directory that does not exist -- and a path
    holding a shell metacharacter is worse than broken. Quoting is a no-op on the
    ordinary paths, so the common case reads exactly as before.
    """
    count = "some" if missing is None else str(missing)
    quoted = shlex.quote(str(repo))
    return (
        f"display-id compatibility: {count} record(s) in {repo} have no "
        "`display_id` metadata, so the\n"
        "readable display id cannot be resolved here. Fix in this order:\n"
        "  1. sync this store with the remote beads store on GitHub:\n"
        f"       python3 /workspace/projects --repo {quoted}\n"
        "  2. migrate the store to the display_id metadata schema (dry-run, then apply):\n"
        f"       python3 /workspace/projects --repo {quoted}\n"
        f"       python3 /workspace/projects --repo {quoted} --apply\n"
        "  3. if that script is missing, update the harness checkout first "
        "(it is git-tracked there):\n"
        "       git -C /workspace/projects pull\n"
        "Once migrated, resolve a readable id with "
        "`~/.claude/bin/bdx <display-id>` and read one\n"
        "back with `~/.claude/bin/bdx --display-id <hash>`; never parse `External:` "
        "out of\n"
        "`bd show` — post-cutover that line holds a Jira key, or nothing.\n"
        "Design: ai-harness/docs/designs/issue-ref-display-id-to-metadata-field.md"
    )


class RepoNotFoundError(RuntimeError):
    """No ``.beads/config.yaml`` found walking up from the start directory."""


class StoreTargetConflictError(RepoNotFoundError):
    """An explicit ``--repo`` names a different store than ``BEADS_DIR`` does.

    Subclasses ``RepoNotFoundError`` so every existing caller's ``except``
    already turns this into a clean ``exit 1`` with the message, rather than an
    uncaught traceback.

    This cannot be resolved in the caller's favour, because ``bd`` itself gives
    ``BEADS_DIR`` precedence over its own working directory (verified 2026-08-07
    against bd 1.1.2: ``bd config get issue_prefix`` run *inside* ai-harness
    with ``BEADS_DIR`` pointing at another store returned the other store's
    prefix). So honouring ``--repo`` here would split identity — this module
    would resolve the prefix and lock for repo A while every ``bd`` subprocess
    it spawns writes to repo B. That is exactly the ISSUE-REF failure shape, and
    it was live before this fix: ``bd_new --repo <B>`` with ``BEADS_DIR`` set to
    A created ``ZZT-1`` in A while taking the lock under B.
    """


class BdUnavailableError(RuntimeError):
    """``bd`` is not on PATH, or a required ``bd`` read from the store failed."""


class LockRootError(RuntimeError):
    """The canonical (worktree-invariant) lock root could not be determined."""


class DisplayIdCompatibilityError(RuntimeError):
    """A record has no ``display_id`` metadata where one is required.

    Carries ``display_id_migration_hint``'s full text as its message, so every raising
    site prints the same actionable instructions without composing them. Defined here
    rather than in any one tool because several tools raise it and the operator must not
    be able to tell which one they hit from the wording.
    """


def require_row_display_id(repo: Path, row: dict) -> str:
    """One LIVE-STORE row's display id, or raise the compatibility failure.

    For a consumer reading `bd list/show/ready --json`, where ``metadata`` is present on
    every row and its absence therefore MEANS something. Substituting the hash or a label
    there is the silent-wrong-answer this cutover exists to remove: the caller goes on to
    print or mirror an identifier that is not the record's display id, and nothing errors.

    A row with NO ``metadata`` key at all is a ``bd export`` (JSONL) row, which cannot
    carry the field by construction (D-5) -- that is not this function's case, and such a
    caller must read the ``id:`` label echo and say so.
    """
    found = row_display_id(row)
    if found is not None:
        return found
    raise DisplayIdCompatibilityError(display_id_migration_hint(repo, missing=None))


class BdTooOldError(BdUnavailableError):
    """The ``bd`` on PATH lacks the metadata flags the display-id cutover needs.

    Subclasses ``BdUnavailableError`` so every existing ``except`` already turns
    this into the clean exit-with-message its callers give an unusable ``bd``,
    rather than an uncaught traceback.
    """


#: The ONE argv that makes ``bd list`` answer "every issue in this store", defined
#: here so no caller re-derives it. Prepend ``"bd"`` for a subprocess that takes the
#: program name; append further filters for a narrower predicate.
#:
#: ``--all`` lifts ONLY the closed filter. Three further categories stay hidden
#: unless named, each documented by ``bd list --help`` as excluded by default: gates
#: ("normally hidden"), infrastructure beads (agent/role/message) and template
#: molecules. A read missing them is still a TRUE answer to a NARROWER question than
#: "what is in the store", and bd signals the narrowing nowhere — the ``--json``
#: payload is a bare array with no envelope, and stderr stays silent.
#:
#: That is why this exists rather than the shorter ``--all`` spelling: every harness
#: guard that cross-checks a list read against an aggregate (``verify_full_read``
#: here, ``bd_fleet.verify_full_read``, ``bd_invariants._verify_full_read``,
#: ``bd_store_backup``) compares it against a count that does NOT share the
#: narrowing, so the two disagree on any store holding one of those rows and the
#: guard blames pagination truncation for a read that was complete. Measured with
#: bd 1.3.0 on a synthetic store of three ordinary issues plus one gate: ``bd list
#: --all -n 0 --json`` returned 3 while both ``bd count`` and an offline ``select
#: count(*) from issues`` returned 4; adding ``--include-gates`` restored 4.
#:
#: ``--include-gates`` is the measured-necessary flag. ``--include-infra`` and
#: ``--include-templates`` are included by the same reasoning rather than by
#: measurement — this synthetic store held neither row kind, so they changed nothing
#: here, but bd's own help says both are excluded by default and either would
#: reproduce the identical disagreement. ``tests/test_bd_full_store_read_scope.py``
#: pins the asymmetry, so a bd that stops hiding gates reports itself instead of
#: letting these flags become silent dead weight.
#: The flags that switch off ``bd list``'s three hidden-category filters. Separate
#: from ``--all`` because they are ORTHOGONAL to it: ``--all`` controls the open/closed
#: axis, these control which KINDS of row are eligible at all. A caller that wants an
#: open-only view still needs these to get every open row (``bd_fleet``'s un-``--all``
#: sweep is exactly that caller).
HIDDEN_CATEGORY_LIST_FLAGS = (
    "--include-gates",
    "--include-infra",
    "--include-templates",
)

FULL_STORE_LIST_ARGS = (
    "list",
    "--all",
    *HIDDEN_CATEGORY_LIST_FLAGS,
    "-n",
    "0",
    "--json",
)

#: The EXACT bd surface the ISSUE-REF cutover depends on — nothing more. Stated as
#: data so it can be asserted and quoted rather than remembered: because this is the
#: whole list, syncing the harness main tree is the entire deploy and there is no bd
#: install step anywhere in the cutover.
REQUIRED_BD_CAPABILITIES = (
    (("bd", "update", "--help"), "--set-metadata"),
    (("bd", "list", "--help"), "--metadata-field"),
    (("bd", "list", "--help"), "--has-metadata-key"),
)


def check_bd_capabilities(*, runner=subprocess.run) -> None:
    """Raise ``BdTooOldError`` unless this ``bd`` has the cutover's three flags.

    ``bd`` is a real binary on PATH (``~/.local/bin/bd``), NOT a link into this
    checkout, so a machine that has just synced the harness can still be running an
    older bd — and then every metadata read answers nothing while every metadata
    write fails, which looks exactly like a store that needs migrating. This turns
    that into one instruction that names the actual problem.

    Parses ``--help`` rather than attempting a probe write: a probe would have to
    mutate a real store to learn anything.
    """
    missing: list[str] = []
    for argv, flag in REQUIRED_BD_CAPABILITIES:
        try:
            proc = runner(
                list(argv),
                env=bd_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except FileNotFoundError as exc:
            raise BdTooOldError(f"'bd' is not on PATH: {exc}") from exc
        if flag not in f"{proc.stdout}\n{proc.stderr}":
            missing.append(f"{' '.join(argv[:2])} {flag}")
    if missing:
        raise BdTooOldError(
            "this bd lacks the metadata support the display-id cutover needs "
            f"({', '.join(missing)}). Upgrade bd (1.3.0+ measured working), then "
            "re-run what you were doing. The cutover needs NO other new bd "
            "behaviour, so nothing else on this machine has to change."
        )


class TruncatedReadError(BdUnavailableError):
    """A full-issue-set ``bd list --all -n 0 --json`` read returned a row count
    that disagrees with an independent ``bd count --json`` aggregate for the
    same repo.

    ``bd count`` is a genuinely different code path — a SQL ``COUNT`` query
    against the store, not a paginated ``SELECT ... LIMIT`` scan — so it
    cannot silently inherit the same pagination-truncation bug the read it
    verifies is exposed to (verified live: ``bd count --json`` and ``bd list
    --all -n 0 --json`` agree exactly on both ai-harness (375) and the maintainer
    (558) today). Treat the list read as failed, never as "the store holds
    this many issues."
    """


@contextmanager
def exclusive_file_lock(lock_path: Path, *, blocking: bool = True):
    """Hold a blocking exclusive advisory lock on ``lock_path`` for the ``with`` block.

    The cross-platform serialization primitive behind ``bd-new``'s display-id
    allocation. Both backends give the same guarantee that matters here -- at
    most one holder at a time, and a waiter blocks rather than failing -- so a
    concurrent read-then-create can never compute the same ``N``:

    * POSIX (``fcntl.flock``): whole-file lock, blocking, released on close.
    * Windows (``msvcrt.locking`` with ``LK_LOCK``): byte-range lock over one
      byte at offset 0. ``LK_LOCK`` retries once a second for ~10 attempts and
      then raises ``OSError``, so unlike ``flock`` it is not indefinitely
      blocking; we retry around it to restore blocking semantics. Windows
      byte-range locks are mandatory rather than advisory, which is strictly
      stronger than what the POSIX path provides.

    The lock file is opened for the block's duration and never truncated -- its
    contents are irrelevant, only the lock is load-bearing.

    ``blocking=False`` makes one acquisition attempt. Event receivers retain
    their input on contention instead of waiting behind an active publisher.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as handle:
        if _fcntl is not None:
            _fcntl.flock(
                handle.fileno(),
                _fcntl.LOCK_EX | (0 if blocking else _fcntl.LOCK_NB),
            )
            try:
                yield
            finally:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        else:
            handle.seek(0)
            while True:
                try:
                    _msvcrt.locking(
                        handle.fileno(),
                        _msvcrt.LK_LOCK if blocking else _msvcrt.LK_NBLCK,
                        1,
                    )
                    break
                except OSError:
                    if not blocking:
                        raise
                    # LK_LOCK exhausted its ~10 one-second attempts while
                    # another holder still had it. Keep waiting, so the caller
                    # sees the same blocking contract the POSIX path gives.
                    continue
            try:
                yield
            finally:
                handle.seek(0)
                _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)


class LockHeld(RuntimeError):
    """Another holder kept the lock for the whole of a bounded wait."""


def _try_lock(handle) -> bool:
    if _fcntl is not None:
        try:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    handle.seek(0)
    try:
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


@contextmanager
def bounded_exclusive_file_lock(lock_path: Path, wait_seconds: float):
    """``exclusive_file_lock`` for a caller that must STOP rather than queue.

    Same file, same backends, so it excludes every ``exclusive_file_lock`` holder of the
    path and is excluded by them. Polls a non-blocking attempt until ``wait_seconds``
    pass, then raises ``LockHeld`` without ever having held the lock -- so a caller's
    ``with`` body, where its mutations live, is unreachable when the lock is not won.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as handle:
        deadline = time.monotonic() + wait_seconds
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise LockHeld(
                    f"{lock_path} was still held by another holder after "
                    f"{wait_seconds:g}s"
                )
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
            else:
                handle.seek(0)
                _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)


def _beads_dir_root(env: dict[str, str]) -> Path | None:
    """The repo root implied by a usable ``BEADS_DIR``, else ``None``.

    ``BEADS_DIR`` points at the ``.beads`` DIRECTORY, so the repo root is its
    parent. Returns ``None`` — meaning "fall through to the cwd walk" — for
    every unusable value, which is precisely what ``bd`` itself does with the
    same inputs (all verified against bd 1.1.2 on 2026-08-07 via ``bd where
    --json``): unset, empty, nonexistent, and present-but-missing-config.yaml
    each fell back to the cwd-discovered store rather than erroring. Matching
    that keeps the two resolvers pointed at one store instead of disagreeing,
    which is the whole point of ISSUE-REF.

    A relative value is resolved against the process cwd, again matching ``bd``
    (``BEADS_DIR=../../realstore/.beads`` from a store-less directory resolved
    correctly).

    ``config.yaml`` is the required marker, not mere directory existence: it is
    what ``find_repo_root`` has always keyed on, so a directory that happens to
    be named ``.beads`` but holds no store must not shadow a real one.
    """
    raw = env.get("BEADS_DIR", "")
    if not raw.strip():
        return None
    beads_dir = Path(raw).expanduser()
    try:
        beads_dir = beads_dir.resolve()
    except OSError:
        return None
    if not (beads_dir / "config.yaml").is_file():
        return None
    return beads_dir.parent


def find_marker_root(start: Path) -> Path:
    """Walk up from ``start`` to the nearest directory holding ``.beads/config.yaml``.

    The pre-ISSUE-REF ``find_repo_root``, unchanged and deliberately blind to
    ``BEADS_DIR``. Use this — never ``find_repo_root`` — when the question is
    "which working tree am I in?" rather than "which bd store am I talking to?".

    ``bd_precommit_validate`` is exactly that case: it answers the first
    question, runs only ``git`` (never ``bd``), and reads a per-worktree git
    INDEX. Letting ``BEADS_DIR`` redirect it would make the pre-commit dual-ID
    gate validate a different repo's staged content than the one being
    committed, silently passing the commit in front of it.

    Raises ``RepoNotFoundError`` if none is found before the filesystem root —
    AC-7: fail loudly, never silently no-op.
    """
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".beads" / "config.yaml").is_file():
            return candidate
    raise RepoNotFoundError(
        f"no .beads/config.yaml found in {start} or any parent directory"
    )


def find_repo_root(
    start: Path, *, explicit: bool = False, env: dict[str, str] | None = None
) -> Path:
    """Resolve the bd store root for ``start``, honouring ``BEADS_DIR``.

    Precedence, chosen to agree with ``bd`` rather than to look tidy:

    1. an explicit user-supplied target (``--repo``, ``explicit=True``) that
       CONFLICTS with ``BEADS_DIR`` is a hard error, not a silent winner —
       see ``StoreTargetConflictError``.
    2. ``BEADS_DIR`` (when usable) — because ``bd`` gives it precedence over
       its own ``cwd``. Every function in this module resolves a repo only to
       hand it to a ``bd`` subprocess as ``cwd``, so if this module preferred
       the walk it would compute a prefix/lock for one store while ``bd`` wrote
       to another (ISSUE-REF).
    3. the upward walk from ``start`` for ``.beads/config.yaml`` — byte-identical
       to the pre-fix behaviour whenever ``BEADS_DIR`` is unusable, so nothing
       changes for the ordinary single-repo case.

    ``explicit`` exists because this function cannot otherwise tell a
    deliberate ``--repo`` from a default ``Path.cwd()``, and the two must be
    treated differently: a cwd that merely happens to sit in another bd repo
    (an agent working in ai-harness with ``BEADS_DIR`` pointed at the
    example-client-host-workspace store — the fleet's documented topology) must quietly
    defer to ``BEADS_DIR`` exactly as ``bd`` does, while a typed ``--repo``
    naming a different store is a genuine contradiction worth stopping for.

    Raises ``RepoNotFoundError`` if nothing resolves — AC-7: a target with no
    store must fail loudly, never silently no-op.
    """
    env = os.environ if env is None else env
    env_root = _beads_dir_root(env)
    current = start.resolve()
    if env_root is not None:
        if explicit and current != env_root:
            raise StoreTargetConflictError(
                f"--repo names the store in {current} but BEADS_DIR points at "
                f"{env_root}. bd gives BEADS_DIR precedence, so continuing "
                "would compute the display id and lock against one store while "
                "bd wrote to the other. Unset BEADS_DIR or drop --repo."
            )
        return env_root
    try:
        return find_marker_root(current)
    except RepoNotFoundError as exc:
        raw = env.get("BEADS_DIR", "").strip()
        if not raw:
            raise
        raise RepoNotFoundError(
            f"{exc} (BEADS_DIR={raw!r} is set but its directory holds no "
            "config.yaml, so it was ignored)"
        ) from exc


def canonical_lock_root(repo: Path, *, runner=subprocess.run) -> Path:
    """Map ``repo`` to the equivalent path in the repo's MAIN working tree, so
    that a lock taken on it serializes across every git worktree (ISSUE-REF).

    ``find_repo_root`` returns the nearest directory holding
    ``.beads/config.yaml``. That file is git-TRACKED, so every worktree gets
    its own copy and the walk stops at the WORKTREE, not the main tree. A lock
    keyed on that path is therefore a *different physical file* per worktree —
    while the ``bd`` subprocess calls it guards all resolve to the one shared
    Dolt store. Mutual exclusion silently evaporates, and concurrent
    ``bd-new`` invocations from parallel sessions compute the same display id.

    ``--git-common-dir`` is the shared ``.git`` directory (identical from every
    worktree); ``--show-toplevel`` is the CURRENT worktree's root. Re-anchoring
    ``repo`` from the latter onto the former's parent yields a path that is
    identical no matter which worktree the caller started in.

    Used ONLY for lock-path resolution. ``find_repo_root``'s own return value
    must stay worktree-relative: ``bd_precommit_validate`` uses it as the cwd
    for ``git diff --cached`` / ``git show :<path>``, and the git index is
    per-worktree — redirecting that to the main tree would make the pre-commit
    dual-ID gate validate the wrong index.

    Raises ``LockRootError`` rather than falling back to ``repo`` whenever git
    resolution fails unexpectedly: a silent fallback would restore exactly the
    broken per-worktree lock this exists to prevent.

    ``GIT_*`` environment variables are STRIPPED from the child (ISSUE-REF). Since
    ``find_repo_root`` now honours ``BEADS_DIR``, ``repo`` is routinely a repo
    the caller is not standing in — the fleet's documented topology puts the
    store in example-client-host-workspace while the session works in
    example-client-host-semantic-knowledge-graph. An inherited ``GIT_DIR`` overrides ``cwd`` for
    ``git rev-parse``, and it silently wins: verified 2026-08-07 that with
    ``GIT_DIR`` pointing at ai-harness, ``canonical_lock_root(/tmp/.../repoB)``
    returned the *ai-harness* root, so two unrelated stores would share one lock
    file and, worse, a store's real lock would move whenever a hook set
    ``GIT_DIR``. That is the ISSUE-REF collision reintroduced through a side door,
    so the child must see only ``repo``'s own git context.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        proc = runner(
            [
                "git",
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
                "--show-toplevel",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        raise LockRootError(f"'git' is not on PATH: {exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if "not a git repository" in stderr.lower():
            # A repo outside git has no worktree ambiguity by construction.
            return repo
        raise LockRootError(
            f"'git rev-parse' failed in {repo} (exit {proc.returncode}): "
            f"{stderr or '(no stderr)'}"
        )
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise LockRootError(
            f"'git rev-parse' produced incomplete output in {repo}: "
            f"{proc.stdout.strip() or '(no output)'}"
        )
    common_dir, toplevel = (Path(lines[0]), Path(lines[1]))
    try:
        rel = repo.relative_to(toplevel)
    except ValueError:
        # A store above the git root is already shared by every worktree.
        return repo
    return (common_dir.parent / rel).resolve()


def _prefix_from_config_yaml(repo: Path) -> str | None:
    """Read the issue prefix directly from ``.beads/config.yaml`` without
    spawning a bd subprocess — the fast path for ``get_issue_prefix``.

    Handles both ``issue-prefix`` (hyphen, canonical YAML) and ``issue_prefix``
    (underscore, accepted by bd) by normalising the key. Returns ``None`` when
    the file is missing, unreadable, or contains no prefix key so the caller
    can fall through to the bd subprocess.
    """
    config_path = repo / ".beads" / "config.yaml"
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        if key.strip().replace("-", "_") == "issue_prefix":
            prefix = value.strip().strip("\"'").strip()
            if (
                prefix
                and prefix[0].isalpha()
                and all(c.isalnum() or c in "-_" for c in prefix)
            ):
                return prefix
    return None


def get_issue_prefix(repo: Path, *, runner=subprocess.run) -> str:
    """Read ``repo``'s bd issue prefix.

    Fast path: read ``.beads/config.yaml`` directly — no subprocess, ~0s vs
    ~8s for a Dolt startup on slow machines. Falls back to
    ``bd config get issue_prefix`` only when the YAML read yields nothing (file
    absent, no prefix key, env-var or database override needed).

    Raises ``BdUnavailableError`` if ``bd`` is not on PATH, the read fails, or
    the store has no prefix configured — AC-7: fail loudly, never silently
    no-op.
    """
    prefix = _prefix_from_config_yaml(repo)
    if prefix:
        return prefix
    try:
        proc = runner(
            ["bd", "config", "get", "issue_prefix"],
            env=bd_env(),
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise BdUnavailableError(f"'bd' is not on PATH: {exc}") from exc
    if proc.returncode != 0:
        raise BdUnavailableError(
            f"'bd config get issue_prefix' failed in {repo} (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip() or '(no output)'}"
        )
    prefix = proc.stdout.strip()
    if not prefix or "not set" in prefix.lower():
        raise BdUnavailableError(
            f"no issue_prefix configured for the bd store at {repo}"
        )
    return prefix


def _bd_count(repo: Path, extra_args: list[str], *, runner=subprocess.run) -> int:
    """Run ``bd count --json`` (plus ``extra_args``) in ``repo`` and return the count.

    ``bd count`` is a SQL ``COUNT`` against the store rather than a paginated
    ``SELECT ... LIMIT`` scan, which is what makes it usable as an independent
    cross-check of a ``bd list`` read of the same predicate.

    Raises ``BdUnavailableError`` if the call fails or its output cannot be
    parsed — never returns a fabricated 0.
    """
    try:
        proc = runner(
            ["bd", "count", "--json", *extra_args],
            env=bd_env(),
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise BdUnavailableError(f"'bd' is not on PATH: {exc}") from exc
    if proc.returncode != 0:
        raise BdUnavailableError(
            f"'bd count' failed in {repo} (exit {proc.returncode}): "
            f"{proc.stderr.strip() or '(no stderr)'}"
        )
    try:
        return json.loads(proc.stdout)["count"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BdUnavailableError(
            f"'bd count' produced unparseable JSON in {repo}: {exc}"
        ) from exc


def verify_full_read(repo: Path, row_count: int, *, runner=subprocess.run) -> None:
    """Cross-check ``row_count`` — the rows a ``FULL_STORE_LIST_ARGS`` read
    returned — against ``bd count --json``.

    Raises ``TruncatedReadError`` if they disagree; raises
    ``BdUnavailableError`` if the ``bd count`` call itself fails or its
    output can't be parsed. Callers must call this on every full-store read,
    including one that came back empty — an empty result is exactly the
    failure mode this guards against.

    The read MUST be the ``FULL_STORE_LIST_ARGS`` one. ``bd count`` is
    unfiltered, so it is only comparable to a list read that is also
    unfiltered; an earlier version of this guard compared it against ``bd list
    --all -n 0 --json`` and called the difference truncation, which made every
    gate-bearing store fail a complete read. Equal cardinality is the
    precondition for reading a disagreement as truncation at all.
    """
    expected = _bd_count(repo, [], runner=runner)
    if row_count != expected:
        raise TruncatedReadError(
            f"'bd {' '.join(FULL_STORE_LIST_ARGS)}' returned {row_count} "
            f"row(s) but 'bd count' independently reports {expected} issue(s) "
            f"in {repo} — treating the list read as truncated/failed rather "
            "than trusting a short result"
        )


def verify_label_read(
    repo: Path, label: str, row_count: int, *, runner=subprocess.run
) -> None:
    """The ``verify_full_read`` contract, narrowed to one label predicate.

    Cross-checks ``row_count`` — the rows a ``bd list -l <label> --all -n 0
    --json`` call returned — against ``bd count -l <label> --json``, i.e. the
    SAME predicate asked of the independent aggregate code path. The
    no-silent-zero property is therefore preserved at the scope actually being
    read: an empty label read still has to be corroborated before anything may
    treat it as "no such issue".

    This is deliberately NOT a substitute for ``verify_full_read``. A
    whole-corpus discrepancy still means the corpus read is untrustworthy; a
    label read that agrees with its own count only licenses conclusions about
    that label.
    """
    expected = _bd_count(repo, ["-l", label], runner=runner)
    if row_count != expected:
        raise TruncatedReadError(
            f"'bd list -l {label} --all -n 0 --json' returned {row_count} row(s) "
            f"but 'bd count -l {label}' independently reports {expected} issue(s) "
            f"in {repo} — treating the list read as truncated/failed rather than "
            "trusting a short result"
        )
