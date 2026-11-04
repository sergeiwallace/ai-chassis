"""Check the ai-core commits required by enabled harness configuration keys."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from platformdirs import user_state_path

from .checkpoint_activation import write_activation

REQUIREMENTS = "config/ai-core-requirements.toml"
CAPABILITY = "ai-core-config"


def _has_key(value: object, parts: list[str]) -> bool:
    if not parts:
        return True
    if not isinstance(value, dict):
        return False
    key, *tail = parts
    if key == "*":
        return any(_has_key(child, tail) for child in value.values())
    return key in value and _has_key(value[key], tail)


def requirements(harness: Path) -> list[dict[str, str]]:
    """Read declarations and select keys that are present, including empty values."""
    try:
        document = tomllib.loads((harness / REQUIREMENTS).read_text())
    except FileNotFoundError:
        return []
    rows = document.get("requirement", [])
    if not isinstance(rows, list):
        raise ValueError(f"invalid {REQUIREMENTS}: requirement must be an array")
    active = []
    for row in rows:
        if not isinstance(row, dict) or any(
            not isinstance(row.get(field), str) or not row[field].strip()
            for field in ("config", "key", "minimum_commit", "reason")
        ):
            raise ValueError(f"invalid requirement row in {REQUIREMENTS}")
        if not re.fullmatch(r"[0-9a-fA-F]{10,40}", row["minimum_commit"]):
            raise ValueError(
                f"invalid minimum_commit in {REQUIREMENTS}: {row['config']}"
            )
        config = (harness / row["config"]).resolve()
        if not config.is_relative_to(harness.resolve()):
            raise ValueError(f"config escapes harness: {row['config']}")
        if config.exists() and _has_key(
            tomllib.loads(config.read_text()), row["key"].split(".")
        ):
            active.append(row)
    return active


def _git_env() -> dict[str, str]:
    """A post-merge hook's Git variables must not redirect the ai-core operations."""
    return {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }


def _git(args: list[str], core: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed executable and argv, no shell
        ["git", "-C", str(core), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=_git_env(),
    )


def _ancestor(core: Path, commit: str, target: str) -> bool:
    return _git(["merge-base", "--is-ancestor", commit, target], core).returncode == 0


def _fetch(core: Path, target: str) -> str:
    remote_branch = target.removeprefix("refs/remotes/origin/")
    try:
        fetched = (
            _git(
                [
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    "origin",
                    f"refs/heads/{remote_branch}:{target}",
                ],
                core,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        fetched = False
    return "" if fetched else "fetch failed; using local objects; "


def _satisfy(harness: Path, core: Path, rows: list[dict[str, str]]) -> str:
    missing = [
        row for row in rows if not _ancestor(core, row["minimum_commit"], "HEAD")
    ]
    if not missing:
        return "already contains required commits"
    # Refuse before fetching or moving when the checkout is unsafe.
    status = _git(["status", "--porcelain=v1", "--untracked-files=all"], core)
    if status.returncode:
        raise ValueError(f"ai-core checkout unavailable at {core}")
    if status.stdout:
        raise ValueError("ai-core tree has uncommitted changes")
    common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], core)
    directory = _git(["rev-parse", "--absolute-git-dir"], core)
    if common.returncode or directory.returncode or common.stdout != directory.stdout:
        raise ValueError("ai-core path is not its main working tree")
    default = _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], core)
    target = (
        default.stdout.strip()
        if default.returncode == 0
        else "refs/remotes/origin/main"
    )
    branch = _git(["symbolic-ref", "--quiet", "HEAD"], core).stdout.strip()
    expected = target.replace("refs/remotes/origin/", "refs/heads/", 1)
    if branch != expected:
        raise ValueError(f"ai-core must be on its default branch {expected}")
    note = _fetch(core, target)
    if not all(_ancestor(core, row["minimum_commit"], target) for row in missing):
        raise ValueError(f"{note}required commit unreachable from {target}")
    if not _ancestor(core, "HEAD", target) or _ancestor(core, target, "HEAD"):
        raise ValueError(
            f"{note}ai-core has local-only commits or is not strictly behind {target}"
        )
    # The guard owns the mutation, so refusal makes the merge unreachable.
    result = subprocess.run(  # noqa: S603 -- repository script and validated Git ref
        [
            sys.executable,
            str(harness / "scripts/main_tree_sync_guard.py"),
            "fast-forward",
            str(core),
            target,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=_git_env(),
    )
    if result.returncode:
        raise ValueError(
            f"{note}sync guard refused or fast-forward failed (exit {result.returncode})"
        )
    if not all(_ancestor(core, row["minimum_commit"], "HEAD") for row in rows):
        raise ValueError(f"{note}required commit still absent after fast-forward")
    return f"{note}fast-forwarded through main_tree_sync_guard.py to {target}"


def activate(harness: Path, core: Path) -> int:
    """Converge safely and record this attempt in the install activation envelope."""
    if not (harness / REQUIREMENTS).exists():
        # Nothing declared: leave no record, and drop one a previous declaration left.
        (Path(user_state_path("ai-harness")) / f"{CAPABILITY}-activation.json").unlink(
            missing_ok=True
        )
        return 0
    rows: list[dict[str, str]] = []
    try:
        rows = requirements(harness)
        detail = _satisfy(harness, core, rows) if rows else "no applicable requirements"
        passed = True
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        detail, passed = str(exc), False
    labels = tuple(
        f"{row['config']}:{row['key']} requires {row['minimum_commit']} ({row['reason']})"
        for row in rows
    )
    reason = "; ".join((*labels, detail))
    if not passed:
        reason += "; remedy: resolve ai-core dirt, branch or divergence without discarding work, fetch origin with the required commit, then rerun install.sh"
    write_activation(
        passed,
        reason,
        capability=CAPABILITY,
        failing=labels or (REQUIREMENTS,),
        declared=bool(rows) or not passed,
    )
    if rows or not passed:
        print(f"{CAPABILITY}: status={'pass' if passed else 'fail'} {reason}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(activate(Path(sys.argv[1]), Path(sys.argv[2])))
