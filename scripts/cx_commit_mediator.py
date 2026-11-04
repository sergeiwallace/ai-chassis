#!/usr/bin/env python3
"""Narrow stdio MCP commit capability for Codex implementation workers.

The server deliberately has no general Git interface.  Its sole operation validates every
model-provided path before it starts Git, then invokes a provenance constructor -- the target
repository's own when it has one, otherwise the one belonging to the ai-harness checkout hosting
this mediator, so self-commit works in every repository a worker is dispatched into.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Refusal:
    code: str
    message: str

    def result(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": self.message}}


def _refuse_path(path: object, reason: str) -> Refusal:
    return Refusal("invalid_path", f"refused path {path!r}: {reason}")


def resolve_provenance_commit(root: Path) -> Path | None:
    """Return the provenance constructor to build the commit with, or None if unavailable.

    The target repository need not carry the tool.  ``provenance-commit`` is a relocatable
    wrapper that resolves its own ai-harness checkout from its own location and then runs
    ``scripts/provenance_commit.py`` against the caller's cwd, so committing into an arbitrary
    repository only requires *an* installed constructor -- not one vendored per repository.
    Requiring a per-repo copy is what made self-commit silently unavailable in every repo except
    ai-harness itself.

    The target repo's own copy still wins, so a checkout that pins a specific constructor keeps
    it.  The fallback is the constructor belonging to the ai-harness checkout that owns this
    mediator, which ``cx`` always launches this file from; it is therefore present whenever the
    mediator is running at all, and is discovered by location rather than by ``HOME`` or ``PATH``.
    """
    candidates = (
        root / ".claude" / "bin" / "provenance-commit",
        Path(__file__).resolve().parent.parent
        / ".claude"
        / "bin"
        / "provenance-commit",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def validate_paths(  # noqa: PLR0911 - each refusal identifies a distinct unsafe path shape.
    root: Path, paths: object
) -> list[str] | Refusal:
    """Return safe repository-relative paths, without invoking Git.

    Resolving the candidate follows symlinks, so an in-root symlink to an outside file cannot be
    staged.  ``..`` is refused as syntax as well as by the resolved-root containment check.
    """
    if not isinstance(paths, list) or not paths:
        return Refusal("invalid_paths", "paths must be a non-empty list")
    validated: list[str] = []
    for raw_path in paths:
        if not isinstance(raw_path, str) or not raw_path:
            return _refuse_path(raw_path, "must be a non-empty string")
        candidate = Path(raw_path)
        if candidate.is_absolute():
            return _refuse_path(raw_path, "must be repository-relative")
        if any(component in {"..", ".git"} for component in candidate.parts):
            return _refuse_path(raw_path, "contains a forbidden path component")
        resolved = (root / candidate).resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            return _refuse_path(raw_path, "resolves outside the repository root")
        if resolved == root:
            return _refuse_path(raw_path, "must name a path below the repository root")
        validated.append(candidate.as_posix())
    return validated


def _git(
    arguments: Sequence[str], root: Path, *, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout}{result.stderr}"


def _reset_index(
    root: Path, safe_paths: list[str], *, original_head: str | None = None
) -> dict[str, Any]:
    """Advance requested and committed index entries without changing working-tree content."""
    if original_head is not None:
        committed = _git(
            ["diff", "--name-only", "--no-renames", "-z", original_head, "HEAD", "--"],
            root,
        )
        if committed.returncode:
            return Refusal("git_error", _output(committed)).result()
        # Hooks can add paths to the temporary index. Advance those real-index entries too;
        # unrelated staged paths keep their exact prior stage.
        safe_paths = sorted(
            set(safe_paths) | {p for p in committed.stdout.split("\0") if p}
        )
    reset = _git(["reset", "--quiet", "HEAD", "--", *safe_paths], root)
    if reset.returncode:
        return Refusal("git_error", _output(reset)).result()
    return {"ok": True}


def commit(  # noqa: C901, PLR0911 - transactional failure paths must restore the index precisely.
    root: Path, title: str, body: str, paths: object
) -> dict[str, Any]:
    """Stage exactly ``paths`` and create one provenance commit, or return a refusal."""
    if not isinstance(title, str) or not title:
        return Refusal("invalid_title", "title must be a non-empty string").result()
    if not isinstance(body, str):
        return Refusal("invalid_body", "body must be a string").result()
    safe_paths = validate_paths(root, paths)
    if isinstance(safe_paths, Refusal):
        return safe_paths.result()
    original_head = _git(["rev-parse", "HEAD"], root)
    if original_head.returncode:
        return Refusal("git_error", _output(original_head)).result()

    # Build the commit from an index seeded at HEAD, never the caller's real index.  This makes
    # the staged content exactly the requested paths and leaves any unrelated pre-existing stage
    # untouched if the gate rejects.
    with tempfile.TemporaryDirectory(prefix="cx-commit-index-") as temporary:
        temporary_index = str(Path(temporary) / "index")
        environment = {**os.environ, "GIT_INDEX_FILE": temporary_index}
        seeded = _git(["read-tree", "HEAD"], root, environment=environment)
        if seeded.returncode:
            return Refusal("git_error", _output(seeded)).result()
        add = _git(["add", "--", *safe_paths], root, environment=environment)
        if add.returncode:
            return Refusal("git_error", _output(add)).result()
        staged = _git(["diff", "--cached", "--quiet"], root, environment=environment)
        if staged.returncode == 0:
            return Refusal(
                "nothing_staged", "nothing staged for the requested paths"
            ).result()
        if staged.returncode != 1:
            return Refusal("git_error", _output(staged)).result()

        constructor = resolve_provenance_commit(root)
        if constructor is None:
            return Refusal(
                "provenance_commit_missing",
                "no provenance-commit tool in this repository or in the ai-harness checkout "
                "hosting this mediator; leaving the requested paths unstaged for the "
                "orchestrator to commit",
            ).result()
        result = subprocess.run(
            [str(constructor), "--no-delegation", "--title", title, "--body", body],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            env={**environment},
        )
        if result.returncode:
            return Refusal("gate_rejected", _output(result)).result()
    reset = _reset_index(root, safe_paths, original_head=original_head.stdout.strip())
    if not reset["ok"]:
        return reset
    sha = _git(["rev-parse", "HEAD"], root)
    if sha.returncode:
        return Refusal("git_error", _output(sha)).result()
    return {"ok": True, "sha": sha.stdout.strip()}


def dirty_tracked_paths(root: Path) -> list[str] | Refusal:
    """Return the union of staged and unstaged tracked paths, excluding untracked files."""
    paths: set[str] = set()
    for arguments in (["diff", "--name-only"], ["diff", "--cached", "--name-only"]):
        result = _git(arguments, root)
        if result.returncode:
            return Refusal("git_error", _output(result))
        paths.update(path for path in result.stdout.splitlines() if path)
    return sorted(paths)


def path_states(root: Path, paths: list[str]) -> dict[str, list[Any]] | Refusal:
    """Return each path's index entries and working-tree content, comparable across runs."""
    if not paths:
        return {}
    index = _git(["ls-files", "--stage", "-z", "--", *paths], root)
    if index.returncode:
        return Refusal("git_error", _output(index))
    entries: dict[str, list[str]] = {}
    for record in index.stdout.split("\0"):
        if record:
            meta, _, path = record.partition("\t")
            entries.setdefault(path, []).append(meta)
    states: dict[str, list[Any]] = {}
    for path in paths:
        full = root / path
        if full.is_symlink():
            content: str | None = f"link:{os.readlink(full)}"
        elif full.is_file():
            data = full.read_bytes()
            content = f"{oct(full.stat().st_mode)}:{hashlib.sha256(data).hexdigest()}"
        else:
            content = None
        states[path] = [sorted(entries.get(path, [])), content]
    return states


def snapshot(root: Path) -> dict[str, list[Any]] | Refusal:
    """Record the dirty tracked paths a worker starts with, so the flush can leave them alone.

    A root that is not yet a Git repository has no dirty path for a worker to inherit.
    """
    if _git(["rev-parse", "--is-inside-work-tree"], root).returncode:
        return {}
    paths = dirty_tracked_paths(root)
    if isinstance(paths, Refusal):
        return paths
    return path_states(root, paths)


def worker_paths(
    root: Path, paths: list[str], baseline: dict[str, list[Any]]
) -> list[str] | Refusal:
    """The dirty paths whose state differs from the baseline: the ones the worker changed.

    A path the parent had dirty and the worker then changed again is the worker's, whole file.
    """
    current = path_states(root, paths)
    if isinstance(current, Refusal):
        return current
    return [path for path in paths if baseline.get(path) != current[path]]


def _read_baseline(path: Path) -> dict[str, list[Any]] | Refusal:
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return Refusal(
            "baseline_unavailable",
            f"the worker's starting snapshot {path} is unreadable ({error}); leaving "
            "every dirty path unstaged for the orchestrator",
        )
    if not isinstance(baseline, dict):
        return Refusal(
            "baseline_unavailable", f"{path} does not hold a snapshot object"
        )
    return baseline


def build_server(root: Path) -> Any:
    # Import only while creating the server, so the validation core remains testable without an
    # MCP client installed.  The project dependency supplies this at actual server launch.
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("aih_commit")

    @server.tool(name="commit")
    def commit_tool(title: str, body: str, paths: list[str]) -> dict[str, Any]:
        """Create one scoped provenance commit from repository-relative paths."""
        return commit(root, title, body, paths)

    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument(
        "--flush",
        action="store_true",
        help="commit the remaining dirty tracked paths (only the worker's, with --baseline)",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        help="write the dirty tracked paths' current state to this file and exit",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="a --snapshot file from before the worker started; --flush skips its unchanged paths",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"cx-commit-mediator: root is not a directory: {root}", file=sys.stderr)
        return 2
    if args.snapshot is not None:
        states = snapshot(root)
        if isinstance(states, Refusal):
            print(states.result())
            return 1
        args.snapshot.write_text(json.dumps(states), encoding="utf-8")
        return 0
    if args.flush:
        paths = dirty_tracked_paths(root)
        if not isinstance(paths, Refusal) and paths and args.baseline is not None:
            baseline = _read_baseline(args.baseline)
            paths = (
                baseline
                if isinstance(baseline, Refusal)
                else worker_paths(root, paths, baseline)
            )
        if isinstance(paths, Refusal):
            result = paths.result()
        elif not paths:
            result = {"ok": True, "status": "nothing_staged"}
        else:
            result = commit(
                root,
                "chore: final worker flush",
                "Commit remaining tracked worker changes before cx exits.",
                paths,
            )
            if not result["ok"] and result["error"]["code"] == "nothing_staged":
                # The temporary index proved every requested working-tree entry equals HEAD.
                # Reconcile only then: a real commit failure must leave the index untouched.
                reset = _reset_index(root, paths)
                result = (
                    reset
                    if not reset["ok"]
                    else {"ok": True, "status": "nothing_staged"}
                )
        print(result)
        return 0 if result["ok"] else 1
    build_server(root).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
