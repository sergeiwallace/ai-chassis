#!/usr/bin/env python3
"""Remove closed pull-request links from Claude Code job state conservatively.

Claude Code's Agents panel stores a job's PR links in ``state.json`` as ``fan``
entries.  This tool deliberately knows only that narrow schema; it never rewrites
any other job state and it has no control over the native footer's in-process PR
poller.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

PR_URL = re.compile(r"^https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9][0-9]*$")
CLOSED_STATES = frozenset({"CLOSED", "MERGED"})


@dataclass(frozen=True)
class SweepResult:
    path: Path
    removed: int = 0
    skipped_changed: bool = False
    error: str | None = None


def is_pr_association(entry: object) -> bool:
    """Return true only for the observed, minimal Claude job PR-link schema."""
    return (
        isinstance(entry, dict)
        and set(entry) == {"id", "href", "kind"}
        and entry.get("kind") == "pr"
        and isinstance(entry.get("id"), str)
        and isinstance(entry.get("href"), str)
        and PR_URL.fullmatch(entry["href"]) is not None
    )


def job_state_paths(claude_dir: Path, session_id: str | None) -> Iterable[Path]:
    """Yield readable state files, optionally limited to the owning CC session."""
    for path in sorted((claude_dir / "jobs").glob("*/state.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if session_id and not (
            state.get("sessionId") == session_id
            or state.get("resumeSessionId") == session_id
        ):
            continue
        yield path


def gh_state(url: str) -> str | None:
    """Read a PR's state without treating an unavailable API as a closed PR."""
    try:
        completed = subprocess.run(
            ["gh", "pr", "view", url, "--json", "state", "--jq", ".state"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip().upper() or None


def _atomic_write(path: Path, text: str) -> None:
    """Atomically replace one state file while preserving its permissions."""
    mode = path.stat().st_mode & 0o777
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temporary:
        temporary.write(text)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    try:
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def sweep_state_file(
    path: Path, state_lookup: Callable[[str], str | None] = gh_state
) -> SweepResult:
    """Remove only confirmed closed PR-link entries from one unchanged state file."""
    try:
        original = path.read_text(encoding="utf-8")
        state = json.loads(original)
    except (OSError, json.JSONDecodeError) as error:
        return SweepResult(path, error=str(error))

    fan = state.get("fan")
    if not isinstance(fan, list):
        return SweepResult(path)

    closed_urls = {
        entry["href"]
        for entry in fan
        if is_pr_association(entry) and state_lookup(entry["href"]) in CLOSED_STATES
    }
    if not closed_urls:
        return SweepResult(path)

    state["fan"] = [
        entry
        for entry in fan
        if not (is_pr_association(entry) and entry["href"] in closed_urls)
    ]
    rendered = json.dumps(state, indent=2, ensure_ascii=False) + "\n"

    # Claude Code does not expose a lock protocol for state.json.  Re-read just
    # before replacing so a concurrent writer wins safely; callers can retry later.
    try:
        if path.read_text(encoding="utf-8") != original:
            return SweepResult(path, skipped_changed=True)
        _atomic_write(path, rendered)
    except OSError as error:
        return SweepResult(path, error=str(error))
    return SweepResult(path, removed=len(closed_urls))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--claude-dir",
        type=Path,
        default=Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")),
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--session-id", help="sweep only this Claude Code session")
    scope.add_argument(
        "--all-sessions", action="store_true", help="sweep every job state"
    )
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    session_id = None if args.all_sessions else args.session_id
    results = [
        sweep_state_file(path) for path in job_state_paths(args.claude_dir, session_id)
    ]
    errors = [result for result in results if result.error]
    if not args.quiet:
        print(
            "Claude PR hygiene: "
            f"removed {sum(result.removed for result in results)} association(s) "
            f"from {sum(result.removed > 0 for result in results)} job(s); "
            f"skipped {sum(result.skipped_changed for result in results)} concurrent change(s)."
        )
        for result in errors:
            print(f"warning: {result.path}: {result.error}", file=sys.stderr)
    # An unavailable job or GitHub query is fail-closed per association, not a hook failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
