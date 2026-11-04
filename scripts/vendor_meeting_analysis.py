#!/usr/bin/env python3
# ruff: noqa: E402
"""Sync a vendored unit (meeting-analysis or jira) into a consumer repo, and verify a copy.

This repo is the canonical copy. A consumer carries a VENDORED copy of a unit plus that
unit's lock, which records the source commit and a sha256 per file, so "is this copy still
the canonical one?" is a question with an answer rather than a guess. Two units, chosen
with `--unit` (default `meeting-analysis`):

* `meeting-analysis`: the analysis scripts and the shared packages they import; lock
  `<skills-root>/vendor-lock.json`.
* `jira`: the generalized Jira toolkit, `skills/jira/scripts/**`; lock
  `<skills-root>/jira/vendor-lock.json`. The consumer keeps its own SKILL.md and
  `jira.toml` beside the copied `scripts/`, so neither is in the unit.

Each unit has its own lock and its own dirty check, so syncing one never rewrites or
invalidates the other.

WHERE THE COPY GOES. Every unit file lives under `skills/` here, and `--skills-root`
(default `skills`) names where that directory lands in the consumer: `.claude/skills`
puts `skills/x` at `<consumer>/.claude/skills/x`, which is where Claude Code loads project
skills from. Lock paths stay relative to the skills root's parent (`skills/x`), so
`meeting_resolve.py vendor-check --target <consumer>/.claude` reads the same lock.

WHY A LOCK FILE RATHER THAN A DRIFT DIFF. The two copies live in repos that are cloned
independently and are frequently not both present on one machine, so a live diff is not
available where the question is asked. The lock travels with the vendored copy: the
consumer can answer "was I modified since I was synced?" from its own tree alone, and
`meeting_resolve.py vendor-check` does exactly that inside the skill.

WHY SYNC REFUSES ON A DIRTY SOURCE. The lock's whole value is that `source_commit`
identifies a tree anyone can check out. Syncing from a dirty tree writes a commit id
that does NOT describe the bytes that were copied, which is worse than no lock at all:
every later check passes against a lie. The dirty check therefore gates the copy -- it
is not a warning printed beside one.

Stdlib only, deliberately: this runs in whatever interpreter a consumer repo happens to
have, before any of that repo's dependencies are installed.
"""

from __future__ import annotations

try:
    from py_interpreter_bootstrap import ensure_supported_python
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):
        """Stand in for the bootstrap when this file was copied without it.

        A partial copy cannot re-exec, because the module that knows how is the one
        missing. Staying silent when the running interpreter ALREADY meets the floor is
        correct rather than lax: there was nothing to enforce. Below the floor there is
        no remedy left to apply, so say so and let the real failure happen where it can
        be read, instead of dying here on an import that is not the problem.
        """
        import sys as _sys

        if _sys.version_info < minimum:
            _sys.stderr.write(
                "warning: {} is running under Python {} without its interpreter "
                "bootstrap, so the {}.{}+ floor could not be enforced\n".format(
                    __file__,
                    ".".join(str(p) for p in _sys.version_info[:3]),
                    minimum[0],
                    minimum[1],
                )
            )


ensure_supported_python()

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

LOCK_RELPATH = "skills/vendor-lock.json"
SOURCE_REPO = "ai-harness"

SKILLS_DIR = "skills"

# The vendoring unit: the shared logic both copies run. The analysis scripts, the shared
# CLI foundation they import through an in-tree `[tool.uv.sources]` path, the guard that
# explains a plain-python run, and the named-meetings front end. Each shared package carries
# its own ruff.toml, so a consumer's root lint config cannot reformat or re-lint the copy and
# fork it from this one. Each copy keeps its own
# SKILL.md and meeting templates, because the consumer's carry content this generic copy
# does not, so neither is in the unit.
UNIT_GLOBS = [
    "skills/meeting-analysis/scripts/**",
    "skills/_shared/pep723_invocation_guard.py",
    "skills/_shared/skill-cli-foundation/pyproject.toml",
    "skills/_shared/skill-cli-foundation/ruff.toml",
    "skills/_shared/skill-cli-foundation/src/**",
    "skills/_shared/meeting-retrieve/pyproject.toml",
    "skills/_shared/meeting-retrieve/ruff.toml",
    "skills/_shared/meeting-retrieve/src/**",
]

# The generalized Jira toolkit: launcher, write guard, markup check and config loader. It
# carries its own ruff.toml for the same reason the shared packages do.
JIRA_LOCK_RELPATH = "skills/jira/vendor-lock.json"
JIRA_UNIT_GLOBS = ["skills/jira/scripts/**"]


@dataclass(frozen=True)
class Unit:
    """One vendored unit: the globs that select it and where its lock lives."""

    name: str
    globs: list[str]
    lock_relpath: str


UNITS = {
    "meeting-analysis": Unit("meeting-analysis", UNIT_GLOBS, LOCK_RELPATH),
    "jira": Unit("jira", JIRA_UNIT_GLOBS, JIRA_LOCK_RELPATH),
}
DEFAULT_UNIT = UNITS["meeting-analysis"]


def _is_excluded(relpath: str, lock_relpath: str = LOCK_RELPATH) -> bool:
    """Reject paths that must never enter the lock.

    `__pycache__` and any dot-directory (`.ruff_cache`, `.pytest_cache`) are build
    residue: they are gitignored in both repos, so a lock row naming one is unsatisfiable
    from a fresh clone and every consumer check would fail on a file nobody shipped. The
    lock itself is excluded because it cannot contain its own digest.
    """
    if relpath == lock_relpath:
        return True
    return any(
        part == "__pycache__" or part.startswith(".") for part in Path(relpath).parts
    )


def unit_files(
    root: Path, globs: list[str], lock_relpath: str = LOCK_RELPATH
) -> list[str]:
    """Return every unit file under `root`, as sorted repo-relative POSIX paths."""
    found: set[str] = set()
    for pattern in globs:
        if pattern.endswith("/**"):
            base = root / pattern[: -len("/**")]
            candidates = sorted(base.rglob("*")) if base.is_dir() else []
        else:
            candidates = sorted(root.glob(pattern))
        for path in candidates:
            if not path.is_file():
                continue
            relpath = path.relative_to(root).as_posix()
            if not _is_excluded(relpath, lock_relpath):
                found.add(relpath)
    return sorted(found)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unit_pathspecs(globs: list[str]) -> list[str]:
    """Git pathspecs covering the unit, for the dirty check."""
    return [pattern.removesuffix("/**") for pattern in globs]


def dirty_unit_paths(source: Path, globs: list[str]) -> list[str]:
    """Return the porcelain status lines for unit paths, empty when the unit is clean."""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "status",
            "--porcelain",
            "--",
            *unit_pathspecs(globs),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git status failed in {source}: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def source_commit(source: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse failed in {source}: {result.stderr.strip()}")
    return result.stdout.strip()


def build_lock(source: Path, files: list[str], unit: Unit = DEFAULT_UNIT) -> dict:
    return {
        "source_repo": SOURCE_REPO,
        "source_commit": source_commit(source),
        "generated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "unit_globs": list(unit.globs),
        "files": [
            {"path": relpath, "sha256": sha256_of(source / relpath)}
            for relpath in files
        ],
    }


def stale_target_files(
    target: Path, globs: list[str], keep: set[str], lock_relpath: str = LOCK_RELPATH
) -> list[str]:
    """Unit files present in the target that the source no longer carries."""
    return [
        relpath
        for relpath in unit_files(target, globs, lock_relpath)
        if relpath not in keep
    ]


def lock_base(target: Path, skills_root: Path) -> Path | None:
    """The consumer directory the unit's `skills/...` paths, and the lock, are relative to.

    None when `skills_root` is not a relative path ending in a `skills` directory: the lock
    records `skills/...` paths, so any other final name would write a copy that neither
    `check` nor `meeting_resolve.py vendor-check` could find.
    """
    if (
        skills_root.is_absolute()
        or ".." in skills_root.parts
        or skills_root.name != SKILLS_DIR
    ):
        print(
            f"error: --skills-root must be a relative path ending in '{SKILLS_DIR}' "
            f"(for example '.claude/skills'), not {str(skills_root)!r}"
        )
        return None
    return target / skills_root.parent


def do_sync(
    source: Path,
    target: Path,
    skills_root: Path = Path(SKILLS_DIR),
    unit: Unit = DEFAULT_UNIT,
) -> int:
    base = lock_base(target, skills_root)
    if base is None:
        return 1
    target = base
    if not (source / ".git").exists():
        print(
            f"error: {source} is not a git checkout, so no source_commit can be recorded"
        )
        return 1

    dirty = dirty_unit_paths(source, unit.globs)
    if dirty:
        print(
            "REFUSED: the source tree is dirty for unit paths, so source_commit would not describe"
        )
        print("what was copied. Commit or stash these first:")
        for line in dirty:
            print(f"  {line}")
        return 1

    files = unit_files(source, unit.globs, unit.lock_relpath)
    if not files:
        print(
            f"error: no unit files found under {source}; is it an {SOURCE_REPO} checkout?"
        )
        return 1

    for relpath in stale_target_files(
        target, unit.globs, set(files), unit.lock_relpath
    ):
        (target / relpath).unlink()
        print(f"  removed  {relpath}")

    for relpath in files:
        destination = target / relpath
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relpath, destination)
        print(f"  synced   {relpath}")

    lock = build_lock(source, files, unit)
    lock_path = target / unit.lock_relpath
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(lock, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )

    print(
        f"\nsynced {len(files)} files from {SOURCE_REPO}@{lock['source_commit'][:12]} into {target}"
    )
    print(f"wrote {lock_path}")
    return 0


def do_check(
    target: Path, skills_root: Path = Path(SKILLS_DIR), unit: Unit = DEFAULT_UNIT
) -> int:
    base = lock_base(target, skills_root)
    if base is None:
        return 1
    target = base
    lock_relpath = unit.lock_relpath
    lock_path = target / lock_relpath
    if not lock_path.is_file():
        print(f"error: no {lock_relpath} in {target}; nothing to check against")
        return 1

    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    globs = lock.get("unit_globs") or list(unit.globs)
    locked = {entry["path"]: entry["sha256"] for entry in lock.get("files", [])}

    problems: list[str] = []
    for relpath, expected in sorted(locked.items()):
        path = target / relpath
        if not path.is_file():
            problems.append(f"MISSING  {relpath}")
        elif sha256_of(path) != expected:
            problems.append(f"MODIFIED {relpath}")

    for relpath in unit_files(target, globs, lock_relpath):
        if relpath not in locked:
            problems.append(f"UNLOCKED {relpath}")

    if problems:
        print(
            f"vendored copy does NOT match {lock_relpath} ({len(problems)} problems):"
        )
        for problem in problems:
            print(f"  {problem}")
        print(f"\nre-sync from {SOURCE_REPO} rather than editing the copy in place.")
        return 1

    commit = lock.get("source_commit", "unknown")
    print(
        f"vendored copy matches {lock_relpath}: {len(locked)} files from {SOURCE_REPO}@{commit[:12]}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser(
        "sync", help="copy the unit into a consumer repo and write its lock"
    )
    sync.add_argument(
        "--source",
        required=True,
        type=Path,
        help=f"the canonical {SOURCE_REPO} checkout",
    )
    sync.add_argument(
        "--target", required=True, type=Path, help="the consumer repo root"
    )

    check = sub.add_parser("check", help="verify a vendored copy against its lock")
    check.add_argument(
        "--target", required=True, type=Path, help="the consumer repo root"
    )

    for command in (sync, check):
        command.add_argument(
            "--skills-root",
            type=Path,
            default=Path(SKILLS_DIR),
            help=f"where the unit's {SKILLS_DIR}/ lands in the consumer, relative to its root "
            f"(default {SKILLS_DIR}; Claude Code loads project skills from .claude/skills)",
        )
        command.add_argument(
            "--unit",
            choices=sorted(UNITS),
            default=DEFAULT_UNIT.name,
            help=f"which unit to sync or check (default {DEFAULT_UNIT.name})",
        )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    unit = UNITS[args.unit]
    if args.command == "sync":
        source, target = args.source.resolve(), args.target.resolve()
        if source == target:
            print("error: --source and --target are the same tree")
            return 1
        return do_sync(source, target, args.skills_root, unit)
    return do_check(args.target.resolve(), args.skills_root, unit)


if __name__ == "__main__":
    sys.exit(main())
