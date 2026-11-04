#!/usr/bin/env python3
"""Commit Superwhisper UI changes from the canonical ai-harness checkout."""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[2]
PROVENANCE_COMMIT = HARNESS_ROOT / "scripts" / "provenance_commit.py"

SETTINGS_RELATIVE_PATH = Path("superwhisper/settings/settings.json")


def default_repo() -> Path:
    """The canonical ai-harness main checkout on THIS machine.

    ``$PROJECTS_DIR`` is what install.sh exports for "where the repos live"; ``~/projects``
    is the fallback, not the answer (ISSUE-REF).  Read at call time, because this daemon is
    started by launchd long after import order stops being interesting.

    Deliberately NOT ``HARNESS_ROOT``: that is this FILE's checkout, so a copy running from
    a worktree would adopt the worktree as canonical, which is the opposite of what
    ``canonical_tree_is_valid`` is for.  ``bd_manifest.projects_root`` is the fleet's
    implementation; it is restated here for the reason the ``IN_PROGRESS_MARKERS`` note
    below gives -- this daemon runs standalone from a launchd plist with no access to
    ``scripts/`` on the path.
    """
    return (
        Path(os.environ.get("PROJECTS_DIR") or Path.home() / "projects") / "ai-harness"
    )


DEFAULT_STATE_DIR = (
    Path.home() / "Library/Application Support/ai-harness/superwhisper-autocommit"
)
DEFAULT_LOG_FILE = Path.home() / "Library/Logs/ai-harness/superwhisper-autocommit.log"
COMMIT_TITLE = "chore(superwhisper): sync settings [automated]"
#: Mirrors ``scripts/git_in_flight.IN_PROGRESS_MARKERS`` verbatim; equality is pinned by a
#: canary in ``tests/test_git_in_flight.py``. Duplicated rather than imported because this
#: daemon runs standalone from a launchd plist with no access to ``scripts/`` on the path.
#: The set it replaces was ``(MERGE_HEAD, REBASE_HEAD, CHERRY_PICK_HEAD)``, which missed
#: every rebase stopped by ``--exec``/``break`` -- git writes no ``REBASE_HEAD`` there
#: (measured) -- plus revert, bisect and the cherry-pick sequencer.
GIT_IN_PROGRESS_FILES = (
    "rebase-merge",
    "rebase-apply",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "sequencer",
)
MAX_COMMIT_PASSES = 3


def provenance_interpreter() -> str:
    """The interpreter that can actually run `provenance_commit.py`.

    NOT a bare `python3`. That script imports `datetime.UTC` (Python >= 3.11, which is
    also this repo's `requires-python` floor) and psutil, so resolving it through PATH
    couples the settings commit to whatever `python3` happens to come first -- on a host
    whose system python3 is 3.10 the script dies during import, the commit is dropped,
    and the only signal is one log line. The harness venv is the interpreter the repo
    guarantees those dependencies for; `sys.executable` is the fallback because it is a
    known real interpreter (the one already running this worker) rather than a PATH
    lookup resolved at commit time.
    """
    for relative in ("bin/python", "Scripts/python.exe"):
        candidate = HARNESS_ROOT / ".venv" / relative
        if candidate.is_file():
            return str(candidate)
    return sys.executable


@dataclass
class AutocommitService:
    repo: Path = field(default_factory=default_repo)
    state_dir: Path = DEFAULT_STATE_DIR
    log_file: Path = DEFAULT_LOG_FILE
    debounce_seconds: float = 2
    require_canonical_repo: bool = True
    enable_push: bool = True
    receipt_store: Path | None = None

    def __post_init__(self) -> None:
        self.repo = self.repo.expanduser().resolve()
        self.state_dir = self.state_dir.expanduser()
        self.log_file = self.log_file.expanduser()
        self.receipt_store = (
            self.receipt_store.expanduser() if self.receipt_store else None
        )
        self.lock_file = self.state_dir / "run.lock"
        self.events_file = self.state_dir / "events"
        self.logger = logging.getLogger(f"superwhisper-autocommit.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(self.log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(handler)

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=self.repo, text=True, capture_output=True, check=False
        )

    def log_git_failure(
        self, action: str, result: subprocess.CompletedProcess[str]
    ) -> None:
        detail = (result.stderr or result.stdout).strip().replace("\n", " | ")
        self.logger.info("%s failed (exit %s): %s", action, result.returncode, detail)

    def record_event(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # O_APPEND makes each small event line atomic even when launchd starts peers.
        with self.events_file.open("a", encoding="utf-8") as events:
            events.write(f"{time.time()}\n")

    def take_events(self) -> int:
        pending = self.events_file.with_name("events.pending")
        try:
            os.replace(self.events_file, pending)
        except FileNotFoundError:
            return 0
        try:
            return len(pending.read_text(encoding="utf-8").splitlines())
        finally:
            pending.unlink(missing_ok=True)

    def canonical_tree_is_valid(self) -> bool:
        if self.require_canonical_repo and self.repo != default_repo().resolve():
            self.logger.info(
                "skip: repository is not the canonical main checkout: %s", self.repo
            )
            return False
        top_level = self.git("rev-parse", "--show-toplevel")
        if (
            top_level.returncode
            or Path(top_level.stdout.strip()).resolve() != self.repo
        ):
            self.log_git_failure("repository check", top_level)
            return False
        branch = self.git("symbolic-ref", "--quiet", "--short", "HEAD")
        if branch.returncode or branch.stdout.strip() != "main":
            self.logger.info("skip: current branch is not main")
            return False
        return True

    def git_operation_is_in_progress(self) -> bool:
        """True when a multi-step git operation holds the tree, or when that is unknown.

        A failed probe answers "I could not look", which is not "nothing is in flight", so
        it reports True and the commit is skipped. The previous form ignored a non-zero
        ``rev-parse`` and fell through to ``return False``, i.e. it committed on top of an
        operation it had failed to rule out.
        """
        for marker in GIT_IN_PROGRESS_FILES:
            marker_path = self.git("rev-parse", "--git-path", marker)
            if marker_path.returncode:
                self.logger.info(
                    "skip: cannot rule out a git operation in progress (%s probe failed)",
                    marker,
                )
                return True
            path = Path(marker_path.stdout.strip())
            if not path.is_absolute():
                path = self.repo / path
            if path.exists():
                self.logger.info("skip: git operation in progress (%s)", marker)
                return True
        return False

    def settings_changed(self) -> bool:
        status = self.git("status", "--porcelain", "--", str(SETTINGS_RELATIVE_PATH))
        if status.returncode:
            self.log_git_failure("settings status", status)
            return False
        return bool(status.stdout.strip())

    def commit_settings(self) -> bool:
        add = self.git("add", "--", str(SETTINGS_RELATIVE_PATH))
        if add.returncode:
            self.log_git_failure("stage settings", add)
            return False

        for attempt in (1, 2):
            commit = subprocess.run(
                [
                    provenance_interpreter(),
                    str(PROVENANCE_COMMIT),
                    "--title",
                    COMMIT_TITLE,
                    "--non-ai",
                    "--no-delegation",
                ],
                cwd=self.repo,
                text=True,
                capture_output=True,
                check=False,
            )
            if commit.returncode == 0:
                self.logger.info("commit succeeded (attempt %s)", attempt)
                return True
            # A file-mutating pre-commit hook leaves an unstaged settings diff.
            unstaged = self.git("diff", "--quiet", "--", str(SETTINGS_RELATIVE_PATH))
            if attempt == 1 and unstaged.returncode == 1:
                self.logger.info(
                    "commit hook changed settings; re-staging and retrying once"
                )
                restage = self.git("add", "--", str(SETTINGS_RELATIVE_PATH))
                if restage.returncode == 0:
                    continue
                self.log_git_failure("re-stage settings", restage)
            self.log_git_failure("commit settings", commit)
            return False
        return False

    def push_if_safe(self) -> None:
        if not self.enable_push:
            self.logger.info("push skipped: disabled")
            return
        fetched = self.git("fetch", "origin", "main")
        if fetched.returncode:
            self.log_git_failure("push skipped: fetch origin/main", fetched)
            return
        counts = self.git("rev-list", "--left-right", "--count", "HEAD...origin/main")
        if counts.returncode:
            self.log_git_failure("push skipped: compare origin/main", counts)
            return
        try:
            ahead, behind = (int(value) for value in counts.stdout.split())
        except ValueError:
            self.logger.info(
                "push skipped: invalid ahead/behind result: %r", counts.stdout.strip()
            )
            return
        if behind or not ahead:
            self.logger.info(
                "push skipped: local main is not a clean fast-forward (ahead=%s behind=%s)",
                ahead,
                behind,
            )
            return
        pushed = self.git("push", "origin", "main")
        if pushed.returncode:
            self.log_git_failure("push", pushed)
        else:
            self.logger.info("push succeeded")

    def run(self) -> int:
        self.record_event()
        self.logger.info("trigger fired")
        with self.lock_file.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.logger.info("skip: another instance holds the lock")
                return 0
            time.sleep(self.debounce_seconds)
            events = self.take_events()
            self.logger.info("debounced %s event(s) into one commit attempt", events)
            if (
                not self.canonical_tree_is_valid()
                or self.git_operation_is_in_progress()
            ):
                return 0
            commit_passes = 0
            while self.settings_changed():
                commit_passes += 1
                if not self.commit_settings():
                    break
                self.push_if_safe()
                if commit_passes == MAX_COMMIT_PASSES:
                    if self.events_file.exists() or self.settings_changed():
                        self.logger.info(
                            "commit pass cap (%s) reached; leaving later changes for "
                            "a future trigger",
                            MAX_COMMIT_PASSES,
                        )
                    break
                events = self.take_events()
                if events:
                    self.logger.info(
                        "processing %s event(s) received during commit work", events
                    )
            if not commit_passes:
                self.logger.info("skip: settings.json has no changes")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=default_repo())
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG_FILE)
    parser.add_argument("--debounce-seconds", type=float, default=2)
    args = parser.parse_args(argv)
    return AutocommitService(
        repo=args.repo,
        state_dir=args.state_dir,
        log_file=args.log_file,
        debounce_seconds=args.debounce_seconds,
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
