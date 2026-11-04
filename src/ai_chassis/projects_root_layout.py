"""Name every entry under the projects root that is not an allowed shape (ISSUE-REF).

Several scripts list the projects root and treat any child holding a `.git` as a fleet repo, so
a hand-made clone, a flat worktree or a sibling `<repo>.worktrees` directory is enrolled in
install, review and sweeps the day it appears. Nothing named such an entry, so each was found by
a person reading the listing. This module is the one place that lists the root and classifies
what it finds; the contract is `config/projects-root-layout.toml`.

Stdlib only. Every finding is a report, never a mutation: the audit deletes nothing.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO = "repo"
GENERATED_LINK = "generated-link"
NAMED_DIR = "named-dir"
CONTAINER = "container"
RETIRED_PATH = "retired-path"
SIBLING_WORKTREE_ROOT = "sibling-worktree-root"
DUPLICATE_ORIGIN_CLONE = "duplicate-origin-clone"
LINKED_WORKTREE = "linked-worktree"
UNKNOWN = "unknown"

ALLOWED = frozenset({REPO, GENERATED_LINK, NAMED_DIR, CONTAINER})

_WHY = {
    SIBLING_WORKTREE_ROOT: (
        "a worktree root beside its repo; worktrees belong in <repo>/.worktrees "
        "(scripts/worktree_creation_guard.py refuses new ones)"
    ),
    DUPLICATE_ORIGIN_CLONE: "a second checkout of an origin another entry already holds",
    LINKED_WORKTREE: "a linked git worktree standing at the root; it belongs in <repo>/.worktrees",
    UNKNOWN: "not a repo, a generated link or a listed exception; move it into a repo or list it",
    RETIRED_PATH: "a path the harness retired; the install prune removes it, or delete it by hand",
}


class LayoutError(RuntimeError):
    """The root could not be listed, or the contract could not be read."""


@dataclass(frozen=True)
class Contract:
    generated_links: frozenset[str]
    named_dirs: frozenset[str]
    containers: dict[str, frozenset[str]]  # container name -> the children it may hold
    retired_paths: frozenset[str]  # root-relative paths that must not exist


@dataclass(frozen=True)
class Entry:
    path: Path
    kind: str
    detail: str = ""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def allowed(self) -> bool:
        return self.kind in ALLOWED


def load_contract(path: Path) -> Contract:
    """Read the contract. A missing or malformed file is an error, never an empty allowlist."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LayoutError(f"cannot read the layout contract {path}: {exc}") from exc
    fields: dict[str, frozenset[str]] = {}
    for key in ("generated_links", "named_dirs", "retired_paths"):
        fields[key] = _name_list(path, key, data.get(key, []))
    containers = data.get("containers", {})
    if not isinstance(containers, dict):
        raise LayoutError(f"{path}: containers must be a table of name = [children]")
    return Contract(
        containers={
            name: _name_list(path, f"containers.{name}", children)
            for name, children in containers.items()
        },
        **fields,
    )


def _name_list(path: Path, key: str, value: object) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise LayoutError(f"{path}: {key} must be a list of names")
    return frozenset(value)


def list_root(root: Path) -> list[Path]:
    """Every direct child of the root, sorted. An empty or unreadable root is an error.

    A zero over an unlisted haystack reads as a clean root, so the audit refuses to continue
    from one: a mistyped or unmounted root must fail, not pass.
    """
    if not root.is_dir():
        raise LayoutError(f"projects root {root} is not a directory")
    try:
        children = sorted(root.iterdir(), key=lambda child: child.name)
    except OSError as exc:
        raise LayoutError(f"cannot list projects root {root}: {exc}") from exc
    if not children:
        raise LayoutError(
            f"projects root {root} lists no entries; refusing to call it clean"
        )
    return children


def _git_file_gitdir(path: Path) -> str | None:
    """The target of a `.git` FILE (a linked worktree or submodule), None for a `.git` directory."""
    marker = path / ".git"
    if not marker.is_file():
        return None
    try:
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return text[len("gitdir:") :].strip() if text.startswith("gitdir:") else None


def _is_checkout(path: Path) -> bool:
    return path.is_dir() and (path / ".git").exists()


def _git_dir(path: Path) -> Path | None:
    """The directory holding this checkout's config: `.git` itself, or the target of a gitfile."""
    marker = path / ".git"
    if marker.is_dir():
        return marker
    target = _git_file_gitdir(path)
    if target is None:
        return None
    gitdir = Path(target)
    return gitdir if gitdir.is_absolute() else (path / gitdir).resolve()


def _origin_url(path: Path) -> str | None:
    """`remote.origin.url` read from the checkout's `config`, or None.

    Parsed in-process rather than through `git config`: this runs once per entry on every
    doctor, and a worktree's own config does not carry the remote (its common dir does).
    """
    git_dir = _git_dir(path)
    if git_dir is None:
        return None
    for config in (git_dir / "config", git_dir.parent.parent / "config"):
        url = _read_origin(config)
        if url:
            return url
    return None


def _read_origin(config: Path) -> str | None:
    try:
        lines = config.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    in_origin = False
    for raw in lines:
        line = raw.strip()
        if line.startswith("["):
            in_origin = line.replace(" ", "").lower() == '[remote"origin"]'
        elif in_origin and "=" in line:
            key, _sep, value = line.partition("=")
            if key.strip().lower() == "url":
                return value.strip().strip('"') or None
    return None


def normalize_origin(url: str) -> str:
    """`host/owner/repo` for the https, ssh and scp forms of one remote, lower-cased."""
    text = url.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    elif ":" in text and "@" in text.split(":", 1)[0]:
        text = text.replace(":", "/", 1)
    text = text.split("@", 1)[-1]
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    return text.lower()


def _repo_basename(normalized: str) -> str:
    return normalized.rsplit("/", 1)[-1]


def _container_children(
    container: Path, listed: frozenset[str]
) -> tuple[list[Entry], list[Path]]:
    """Findings for a container's children, and the listed ones that are checkouts."""
    entries: list[Entry] = []
    checkouts: list[Path] = []
    for inner in sorted(container.iterdir(), key=lambda item: item.name):
        if inner.name not in listed:
            why = f"{_WHY[UNKNOWN]} (not a listed child of container {container.name})"
            entries.append(Entry(inner, UNKNOWN, why))
        elif _is_checkout(inner):
            checkouts.append(inner)
        else:
            why = f"{_WHY[UNKNOWN]} (inside container {container.name})"
            entries.append(Entry(inner, UNKNOWN, why))
    return entries, checkouts


def audit(root: Path, contract: Contract) -> list[Entry]:
    """Classify every entry of the root (and the children of each container), sorted by path."""
    entries: list[Entry] = []
    checkouts: list[Path] = []

    for child in list_root(root):
        name = child.name
        if name in contract.containers and child.is_dir() and not child.is_symlink():
            entries.append(Entry(child, CONTAINER))
            inner_entries, inner_checkouts = _container_children(
                child, contract.containers[name]
            )
            entries.extend(inner_entries)
            checkouts.extend(inner_checkouts)
        elif name in contract.generated_links:
            entries.append(Entry(child, GENERATED_LINK))
        elif name in contract.named_dirs and child.is_dir():
            entries.append(Entry(child, NAMED_DIR))
        elif _is_checkout(child):
            checkouts.append(child)
        elif (
            name.endswith(".worktrees")
            and child.is_dir()
            and (root / name[: -len(".worktrees")]).exists()
        ):
            entries.append(
                Entry(child, SIBLING_WORKTREE_ROOT, _WHY[SIBLING_WORKTREE_ROOT])
            )
        else:
            entries.append(Entry(child, UNKNOWN, _WHY[UNKNOWN]))

    entries.extend(_classify_checkouts(checkouts))
    for relative in sorted(contract.retired_paths):
        retired = root / relative
        if retired.exists() or retired.is_symlink():
            entries.append(Entry(retired, RETIRED_PATH, _WHY[RETIRED_PATH]))
    return sorted(entries, key=lambda entry: str(entry.path))


def _classify_checkouts(checkouts: list[Path]) -> list[Entry]:
    entries: list[Entry] = []
    by_origin: dict[str, list[Path]] = {}
    for path in checkouts:
        gitdir = _git_file_gitdir(path)
        if gitdir is not None and "/worktrees/" in gitdir.replace("\\", "/"):
            entries.append(Entry(path, LINKED_WORKTREE, _WHY[LINKED_WORKTREE]))
            continue
        url = _origin_url(path)
        if url is None:
            entries.append(Entry(path, REPO))
            continue
        by_origin.setdefault(normalize_origin(url), []).append(path)

    for origin, paths in by_origin.items():
        keeper = next(
            (p for p in paths if p.name.lower() == _repo_basename(origin)), paths[0]
        )
        for path in paths:
            if path == keeper or len(paths) == 1:
                entries.append(Entry(path, REPO))
            else:
                entries.append(
                    Entry(
                        path,
                        DUPLICATE_ORIGIN_CLONE,
                        f"{_WHY[DUPLICATE_ORIGIN_CLONE]}: {keeper.name} holds {origin}",
                    )
                )
    return entries


def findings(entries: list[Entry]) -> list[Entry]:
    return [entry for entry in entries if not entry.allowed]


def finding_lines(entries: list[Entry]) -> list[str]:
    return [
        f"projects-root layout: {entry.kind} {entry.path}: {entry.detail}"
        for entry in findings(entries)
    ]


def main(argv: list[str] | None = None) -> int:
    """0 clean, 1 findings, 2 the root or contract could not be read."""
    import argparse

    parser = argparse.ArgumentParser(prog="projects_root_layout", description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / "projects")
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "config"
        / "projects-root-layout.toml",
    )
    args = parser.parse_args(argv)
    try:
        entries = audit(args.root, load_contract(args.contract))
    except LayoutError as exc:
        print(f"projects-root layout: cannot audit: {exc}", file=sys.stderr)
        return 2
    lines = finding_lines(entries)
    if lines:
        print("\n".join(lines))
        return 1
    print(f"projects-root layout: {len(entries)} entries, all allowed shapes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
