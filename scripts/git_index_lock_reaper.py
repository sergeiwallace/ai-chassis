#!/usr/bin/env python3
"""Remove a git ``index.lock`` only when it is PROVABLY orphaned (ISSUE-REF).

An orphaned lock is silent and expensive here: while one is present every ``bd`` write's
auto-export fails with ``git add failed: git index is locked``, so the Dolt store advances
while ``.beads/issues.jsonl`` does not, and ordinary ``git reset``/``checkout`` repair in
the affected tree dies outright. Two instances were measured at 9.2 h and 10.5 h old,
silently failing every session's export for that whole window. Nothing on this box reaps
them.

The reason this is a script rather than a one-line ``rm`` is that **both obvious
predicates are wrong in both directions**, each refuted by measurement:

* **Size does not decide.** A 0-byte lock was observed *held by a live git pid* within a
  minute of a genuinely stale 0-byte one. And a 156164-byte lock was equally stale: frozen
  in size and mtime across 505 s, and byte-size-identical to ``.git/index``, so the write
  had finished and only the rename was missing. A guard keyed on emptiness reaps the first
  case wrongly and misses the second entirely.
* **Age does not decide.** A stale lock was seen at 131 s, younger than many genuinely
  live operations on this box, where a single commit's gate chain has run for over two
  hours. (Measured while building this: ``git commit`` does **not** hold the index while
  its pre-commit hook runs — it takes the lock only for the index write itself. So the
  long holds observed here come from bd's own auto-export ``git add``, not from hook
  chains, which is the same producer ISSUE-REF identified.)

What does decide is **freeze plus a proved absence of any holder**. Absence has to be
proved rather than assumed: an unreadable ``/proc`` means "I could not look", which is not
the same observation as "nothing holds it", so it fails closed.

Holder detection deliberately looks wider than the issue's original ``cwd``-only sketch.
A cwd-only scan under-detects, because every fleet script invokes git as
``git -C <repo> ...`` from somewhere else entirely, and because the owner of a commit is
often its **hook chain** (a ``pre-commit`` python process) rather than a git process at
all — both shapes were measured tonight. Argv is matched rather than the environment on
purpose: reading another process's ``environ`` risks printing secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import git_in_flight
from process_liveness import parent_pid

EXIT_OK = 0
EXIT_KEPT = 0
EXIT_INDETERMINATE = 3
EXIT_ERROR = 1

#: Sampling window used to tell a frozen lock from an in-flight write. A real
#: ``git add``/``git commit`` on this filesystem moves size or mtime within seconds.
DEFAULT_SAMPLE_SECONDS = 30

#: Process names that can legitimately own a git index lock. ``git`` is the obvious one;
#: the rest are the hook and wrapper layers measured holding one on this box.
#: Only a process that IS git (or dolt, which drives the beads store) can hold
#: ``.git/index.lock``. This replaced a wider OWNER_HINTS list that also matched hook-chain
#: and wrapper names via argv (ISSUE-REF follow-up, reported by ISSUE-REF 2026-09-07): see
#: :func:`_is_index_holder_comm` for why the wider predicate could not work.
INDEX_HOLDER_COMMS = ("git", "dolt")


class ProcessTableUnreadableError(RuntimeError):
    """The process table could not be read, so absence of a holder is unproved."""


@dataclass(frozen=True)
class ProcessEntry:
    """One process, reduced to what the holder question needs."""

    pid: int
    comm: str
    cwd: Path | None
    argv: tuple[str, ...]
    fds: tuple[Path, ...]


@dataclass(frozen=True)
class LockSample:
    """A single observation of the lock file, identified by inode."""

    inode: int
    device: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class Verdict:
    """``action`` is one of absent | reaped | would-reap | kept | indeterminate."""

    action: str
    reason: str
    path: Path
    holders: tuple[int, ...] = ()

    def as_json(self) -> dict[str, object]:
        return {
            "action": self.action,
            "reason": self.reason,
            "path": str(self.path),
            "holders": list(self.holders),
        }


@dataclass(frozen=True)
class ReapOptions:
    proc: ProcTable | None = None
    sample_seconds: int = DEFAULT_SAMPLE_SECONDS
    sleep: Callable[[float], None] = time.sleep
    dry_run: bool = False
    exclude: set[int] | None = None


class ProcTable:
    """The real ``/proc`` reader.

    ``fuser`` is not installed on this host, so holders come from walking
    ``/proc/<pid>/fd``. Every per-process read is best-effort — a process may exit
    mid-walk, which is normal and not a readability failure — but if ``/proc`` itself
    cannot be listed, that is raised so the caller fails closed.
    """

    def __init__(self, root: Path = Path("/proc")) -> None:
        self.root = root

    def scan(self) -> list[ProcessEntry]:
        try:
            names = os.listdir(self.root)
        except OSError as exc:  # pragma: no cover - exercised by the fake in tests
            raise ProcessTableUnreadableError(
                f"cannot list {self.root}: {exc}"
            ) from exc
        entries: list[ProcessEntry] = []
        for name in names:
            if not name.isdigit():
                continue
            pid = int(name)
            base = self.root / name
            comm = _read_text(base / "comm").strip()
            argv = tuple(
                part for part in _read_text(base / "cmdline").split("\0") if part
            )
            cwd = None
            try:
                cwd = Path(os.readlink(base / "cwd"))
            except OSError:
                cwd = None
            fds: list[Path] = []
            try:
                for fd in os.listdir(base / "fd"):
                    try:
                        fds.append(Path(os.readlink(base / "fd" / fd)))
                    except OSError:
                        continue
            except OSError:
                pass
            entries.append(
                ProcessEntry(pid=pid, comm=comm, cwd=cwd, argv=argv, fds=tuple(fds))
            )
        return entries


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def lock_path(repo: Path) -> Path:
    """The index lock for ``repo``, honouring a linked worktree's own git dir."""
    git = repo / ".git"
    if git.is_file():  # a linked worktree: .git is a file pointing at the real dir
        pointer = _read_text(git).strip()
        prefix = "gitdir:"
        if pointer.startswith(prefix):
            return Path(pointer[len(prefix) :].strip()) / "index.lock"
    return git / "index.lock"


def in_progress_operation(repo: Path) -> str | None:
    """Name the multi-step git operation currently in flight, or ``None``.

    An in-flight operation means the index belongs to it even if no process is holding the
    lock at this instant -- a rebase between two of its own steps has no live git process
    at all, and removing its lock then is how you corrupt the sequence. The marker set and
    the git-dir resolution both live in :mod:`git_in_flight`; the local copy they replace
    resolved the git dir by hand-parsing ``.git``, which silently reads the caller's CWD
    when a worktree's ``gitdir:`` pointer is relative.

    A probe that cannot answer is reported as an operation rather than as "clear", so the
    lock is KEPT: for this caller the fail-closed direction is to leave the lock alone.
    """
    verdict = git_in_flight.in_flight_operation(repo)
    if verdict.clear:
        return None
    return verdict.operation or "possible (undetermined) operation"


def sample(path: Path) -> LockSample | None:
    """Stat without following a symlink; ``None`` when absent or not a regular file."""
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return LockSample(
        inode=info.st_ino,
        device=info.st_dev,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
    )


def _own_pids() -> set[int]:
    """This process, every ANCESTOR of it, and its process-group leader.

    Parent-only was not enough, and the gap was measured (ISSUE-REF, 2026-09-07): the reaper is
    normally launched from a wrapper shell that is a GRANDparent or further up, and since
    every invocation names the repo path, that shell's argv satisfied the old path test. The
    tool then reported its own caller as the lock holder and refused indefinitely -- pushing
    the operator back to the unguarded ``rm`` this exists to replace. So ancestry is walked
    to the top, and the process-group leader is excluded too, because a backgrounded
    launcher need not be a direct ancestor of the interpreter at all.

    The walk uses :func:`process_liveness.parent_pid` rather than reading
    ``/proc/<pid>/stat`` here, because a ``/proc``-only read answers None on its FIRST
    step on a host without procfs -- which silently restored the original defect
    (ancestors unexcluded) on every Mac while looking like a walk.
    """
    pids = {os.getpid()}
    try:
        pids.add(os.getpgid(0))
    except OSError:  # pragma: no cover - getpgid(0) does not fail on Linux
        pass
    pid = os.getpid()
    # The cycle guard tracks the CHAIN, not ``pids``. Using ``pids`` aborted the walk on its
    # first step whenever the process-group leader happened to be the direct parent -- which
    # is the common case -- so the grandparent launcher was never excluded and the original
    # defect survived the fix. Caught by
    # ``test_every_ancestor_of_the_reaper_is_excluded_not_just_its_parent``.
    walked: set[int] = set()
    # Bounded: a malformed or cyclic /proc must never hang the reaper.
    for _ in range(64):
        parent = parent_pid(pid)
        if parent is None or parent <= 0 or parent in walked:
            break
        walked.add(parent)
        pids.add(parent)
        pid = parent
    return pids


def find_holders(
    repo: Path,
    path: Path,
    *,
    proc: ProcTable | None = None,
    exclude: set[int] | None = None,
) -> tuple[tuple[int, ...], str]:
    """Return the pids that could own ``path``, and a human reason for the first one.

    Raises :class:`ProcessTableUnreadableError` rather than reporting an empty set when
    the table cannot be read.
    """
    table = proc if proc is not None else ProcTable()
    excluded = (exclude or set()) | _own_pids()
    repo_text = str(repo.resolve()) if repo.exists() else str(repo)
    holders: list[int] = []
    reason = ""
    for entry in table.scan():
        if entry.pid in excluded:
            continue
        if any(fd == path for fd in entry.fds):
            holders.append(entry.pid)
            reason = reason or f"pid {entry.pid} holds an open fd on the lock"
            continue
        if not _is_index_holder_comm(entry.comm):
            continue
        in_tree = entry.cwd is not None and _within(entry.cwd, repo)
        targets_repo = any(repo_text in argument for argument in entry.argv)
        if in_tree or targets_repo:
            holders.append(entry.pid)
            where = "cwd is inside the repo" if in_tree else "argv targets the repo"
            reason = reason or f"pid {entry.pid} ({entry.comm}) is live and its {where}"
    return tuple(holders), reason


def _is_index_holder_comm(comm: str) -> bool:
    """Identity, not plausibility: only git or dolt itself can hold the index lock.

    This replaces a predicate that matched ``comm`` OR any whitespace-split argv token
    against a list of hook and wrapper names. That was measured wrong in the ordinary case
    (ISSUE-REF, 2026-09-07): a process whose argv merely *mentions* git counted as a holder, and
    because every invocation of this tool names the repo path, the reaper's own caller
    satisfied both halves and the lock was kept forever.

    Dropping argv loses no real holder. ``.git/index.lock`` is created by git, so a shell or
    hook chain that is genuinely RUNNING git has a child process whose ``comm`` IS ``git``,
    and that child is what this matches. My earlier reasoning for the argv heuristic --
    "a shell-wrapped ``bash -c 'git commit'`` would otherwise be invisible" -- confused the
    wrapper with the holder.

    ``comm`` is truncated to 15 bytes by the kernel, hence the prefix allowance for helpers
    such as ``git-remote-http``.
    """
    return comm in INDEX_HOLDER_COMMS or comm.startswith("git-")


def _within(candidate: Path, repo: Path) -> bool:
    try:
        candidate.relative_to(repo)
    except ValueError:
        return False
    return True


def _initial_sample(repo: Path, path: Path) -> tuple[LockSample | None, Verdict | None]:
    first = sample(path)
    if first is None:
        if path.exists() or path.is_symlink():
            return None, Verdict(
                "kept", "lock is not a regular file, so it is never followed", path
            )
        return None, Verdict("absent", "no index.lock present", path)
    in_progress = in_progress_operation(repo)
    if in_progress is not None:
        return None, Verdict(
            "kept", f"a {in_progress} is in progress, so the index belongs to it", path
        )
    return first, None


def _holder_verdict(
    repo: Path, path: Path, options: ReapOptions, *, late: bool
) -> Verdict | None:
    try:
        holders, why = find_holders(
            repo, path, proc=options.proc, exclude=options.exclude
        )
    except ProcessTableUnreadableError as exc:
        prefix = "cannot re-prove" if late else "cannot prove"
        return Verdict("indeterminate", f"{prefix} the lock is unheld: {exc}", path)
    if holders:
        fallback = (
            "a holder appeared during the sample"
            if late
            else f"{len(holders)} live holder(s)"
        )
        return Verdict("kept", why or fallback, path, holders)
    return None


def _sample_verdict(
    path: Path, first: LockSample, second: LockSample | None
) -> Verdict | None:
    if second is None:
        return Verdict("absent", "lock disappeared while being measured", path)
    if (second.inode, second.device) != (first.inode, first.device):
        return Verdict(
            "kept",
            "lock was replaced by a different inode, which belongs to an operation this run never observed",
            path,
        )
    if (second.size, second.mtime_ns) != (first.size, first.mtime_ns):
        return Verdict(
            "kept", "lock changed during the sample, so a write is in flight", path
        )
    return None


def reap_if_orphaned(
    repo: Path, options: ReapOptions | None = None, **option_fields: object
) -> Verdict:
    """Prove orphanhood, then remove the exact inode that was proved — or keep it.

    The removal is reachable only from inside the branch that established every
    condition. That is deliberate and is asserted by a test: a refusal followed by an
    unconditional unlink is the measured failure mode, so the guard has to be control
    flow rather than a line that precedes the dangerous call.
    """
    options = options or ReapOptions(**option_fields)
    path = lock_path(repo)
    first, initial = _initial_sample(repo, path)
    if initial:
        return initial
    assert first is not None
    holders = _holder_verdict(repo, path, options, late=False)
    if holders:
        return holders
    if options.sample_seconds:
        options.sleep(options.sample_seconds)
    early = _sample_verdict(path, first, sample(path))
    early = early or (
        Verdict(
            "would-reap", "provably orphaned; not removing in report-only mode", path
        )
        if options.dry_run
        else _holder_verdict(repo, path, options, late=True)
    )
    if early:
        return early
    final = sample(path)
    if (
        final
        and (final.inode, final.device) == (first.inode, first.device)
        and (final.size, final.mtime_ns) == (first.size, first.mtime_ns)
    ):
        path.unlink(missing_ok=True)
        return Verdict("reaped", "provably orphaned: unheld and frozen", path)
    return Verdict("kept", "lock changed between the proof and the removal", path)


def worktree_repos(repo: Path) -> list[Path]:
    """``repo`` plus every checkout registered under ``.git/worktrees``.

    Each linked worktree has its own index and therefore its own lock, and two of the
    measured stale locks lived under ``.git/worktrees/<name>/`` rather than in the main
    tree.
    """
    roots = [repo]
    worktrees = repo / ".git" / "worktrees"
    if not worktrees.is_dir():
        return roots
    for entry in sorted(worktrees.iterdir()):
        pointer = entry / "gitdir"
        if not pointer.is_file():
            continue
        target = _read_text(pointer).strip()
        if not target:
            continue
        checkout = Path(target)
        if checkout.name == ".git":
            checkout = checkout.parent
        roots.append(checkout)
    return roots


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--all-worktrees",
        action="store_true",
        help="also check every checkout registered under .git/worktrees",
    )
    parser.add_argument("--sample-seconds", type=int, default=DEFAULT_SAMPLE_SECONDS)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="prove orphanhood and report it without removing anything",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = args.repo.resolve()
    targets = worktree_repos(repo) if args.all_worktrees else [repo]
    verdicts = [
        reap_if_orphaned(
            target, sample_seconds=args.sample_seconds, dry_run=args.dry_run
        )
        for target in targets
    ]
    if args.as_json:
        print(json.dumps({"verdicts": [v.as_json() for v in verdicts]}, sort_keys=True))
    else:
        for verdict in verdicts:
            print(f"{verdict.action}: {verdict.path} — {verdict.reason}")
    if any(v.action == "indeterminate" for v in verdicts):
        return EXIT_INDETERMINATE
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI
    sys.exit(main())
