"""Keep fallback copies of canonical ai-harness files in consumer repositories.

Design: docs/designs/held-doc-review-and-canonical-readme-sync.md, § Canonical-README sync (D-4).
Registry: config/canonical-readme-sync.toml, read on every run.

For each ``[[readme]]`` row the expected consumer copy is the rendered banner, a blank line, then
the canonical body, or the canonical bytes alone when ``banner_template = "none"`` (a rule file,
whose frontmatter must stay on line 1). ``publication_mode`` decides the body: ``verbatim`` and
``exclude-public-consumers`` copy it, ``redact-internal-ids`` replaces private display ids,
owner names, denylisted names and private consumer identifiers and paths, and
``PENDING`` names no rendering. The expected copy is compared with the consumer's
``<remote>/<main_branch>`` after a bounded fetch.

A row whose mode is ``PENDING`` or whose ``publication_hold`` is true compares and reports only
(``in-sync`` or ``drift``) and registers a ``decision-pending`` manual step naming the open
decision; it writes, commits, pushes and propagates nothing. Otherwise a drifted copy is committed
in the worktree ``<consumer>/<worktree>`` on ``<branch_prefix>/<AI_HOST>`` and pushed; where the
registry's forge command is usable the step opens or reuses a PR, squash-merges it once its checks
are green, proves the content landed and fast-forwards the consumer's main tree. Then it registers
the copier propagate as a manual step. It never runs ``propagate_command``.

A failure registers a ``-failed`` manual step carrying only an operation name, an exit code, the
repository name, the relative target path and the commands to finish by hand: never the failing
command's output. The step returns 0 whatever happens.

``python -m ai_chassis.canonical_readme_sync render <name>`` prints the expected consumer copy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from string import Template

from . import manual_steps

LABEL = "canonical-readme-sync"
CONFIG_RELPATH = Path("config") / "canonical-readme-sync.toml"
#: A forge command line (shell-split) used instead of the registry's ``forge_command``.
FORGE_ENV = "AI_HARNESS_README_SYNC_FORGE"
PROVENANCE = "Delegated-to: none"
#: `git ls-remote --exit-code` exits 2 when no ref matched.
LS_REMOTE_NO_MATCH = 2
CANONICAL_REPO = "ai-harness"
HARNESS_ROOT = Path(__file__).resolve().parents[2]
INSTALL_DOC = "docs/procedures/ai-harness-update-and-install.md § Manual steps"
RERUN = "ai-harness apply --only canonical-readme-sync"
NO_BANNER = "none"
PENDING = "PENDING"
REDACT = "redact-internal-ids"
EXCLUDE_PUBLIC = "exclude-public-consumers"
VERBATIM = "verbatim"
PUBLICATION_MODES = frozenset({PENDING, REDACT, EXCLUDE_PUBLIC, VERBATIM})
#: The only operation names a ``-failed`` step may carry.
OPERATIONS = frozenset(
    {
        "read-registry",
        "read-canonical",
        "read-id-prefixes",
        "render",
        "read-harness-head",
        "fetch",
        "ls-remote",
        "list-worktrees",
        "prepare-worktree",
        "write-copy",
        "commit",
        "push",
        "pr-list",
        "pr-create",
        "pr-checks",
        "pr-merge",
        "verify-landed",
        "retire-branch",
        "host-identity",
    }
)

# A git hook (install.sh runs from ai-harness's post-merge hook) exports these for the repository
# being merged; they would override every `git -C <consumer>` below.
_GIT_LOCATION_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
    "GIT_NAMESPACE",
)
_PREFIX_ROW = re.compile(r"\bprefix:\s*([A-Z][A-Z0-9-]*)")


class SyncError(Exception):
    """One sync action failed. Carries no command output, so none can reach the ledger."""

    def __init__(
        self,
        operation: str,
        exit_code: int | None,
        commands: Sequence[str] = (),
        reason: str = "",
        row_name: str = "",
    ) -> None:
        self.operation = operation if operation in OPERATIONS else "unknown"
        self.exit_code = exit_code
        self.commands = list(commands)
        self.reason = reason
        # The registry row the failure belongs to, so a batched publication registers its
        # manual step against that row and not the batch's first one.
        self.row_name = row_name
        super().__init__(self.diagnostic())

    def diagnostic(self) -> str:
        status = "timed out" if self.exit_code is None else f"exit {self.exit_code}"
        return f"{self.operation} failed ({status}){f': {self.reason}' if self.reason else ''}"


@dataclass(frozen=True)
class Readme:
    name: str
    canonical: str
    consumer_repo: str
    consumer_slug: str
    consumer_path: str
    banner_template: str
    propagate_command: str
    follow: str
    publication_mode: str
    publication_hold: bool
    decision: str
    decision_held: str


@dataclass(frozen=True)
class Settings:
    remote: str
    main_branch: str
    worktree: str
    branch_prefix: str
    fetch_timeout_s: int
    push_timeout_s: int
    forge_timeout_s: int
    forge_command: str
    forge_requires: str
    id_prefix_manifest: str
    redaction_replacement: str
    redaction_note: str
    owner_names: tuple[str, ...]
    owner_replacement: str
    denylist_replacement: str
    name_replacements: tuple[tuple[re.Pattern[str], str], ...]
    readmes: tuple[Readme, ...]
    readme_errors: tuple[ReadmeError, ...]


@dataclass(frozen=True)
class ReadmeError:
    """One invalid registry row, isolated so every valid row can still run."""

    name: str
    error_type: str


def _relative_inside(value: object, field: str, name: str) -> str:
    """A registry path field: relative and free of ``..`` segments, so joined onto its root it
    cannot name a file outside that repository. Refused at load, before any read or write."""
    text = str(value)
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise ValueError(
            f"[[readme]] {name!r}: {field} {text!r} must be a relative path with no '..' "
            "segment, so it stays inside its repository"
        )
    return text


def inside(root: Path, rel: str) -> Path:
    """``root / rel`` proved to resolve under ``root`` (symlinks followed); the guard every read
    and the consumer write pass through, so a path that escapes is refused before it is touched."""
    target = (root / rel).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"{rel!r} resolves outside {root}")
    return root / rel


def _readme(row: Mapping[str, object]) -> Readme:
    name = str(row.get("name", ""))
    mode = str(row["publication_mode"])
    if mode not in PUBLICATION_MODES:
        raise ValueError(
            f"[[readme]] {name!r}: publication_mode {mode!r} is not one of "
            f"{', '.join(sorted(PUBLICATION_MODES))}"
        )
    banner = str(row["banner_template"])
    if banner != NO_BANNER:
        banner = _relative_inside(banner, "banner_template", name)
    return Readme(
        name=name,
        canonical=_relative_inside(row["canonical"], "canonical", name),
        consumer_repo=_relative_inside(row["consumer_repo"], "consumer_repo", name),
        consumer_slug=str(row["consumer_slug"]),
        consumer_path=_relative_inside(row["consumer_path"], "consumer_path", name),
        banner_template=banner,
        propagate_command=str(row["propagate_command"]),
        follow=str(row["follow"]),
        publication_mode=mode,
        publication_hold=bool(row.get("publication_hold", False)),
        decision=str(row.get("decision", "")),
        decision_held=str(row.get("decision_held", "")),
    )


def load_settings(harness: Path) -> Settings | None:
    path = harness / CONFIG_RELPATH
    if not path.is_file():
        return None
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    readmes: list[Readme] = []
    readme_errors: list[ReadmeError] = []
    for index, row in enumerate(data.get("readme", []), start=1):
        name = f"row-{index}"
        try:
            if not isinstance(row, Mapping):
                raise TypeError("a [[readme]] row must be a table")
            name = str(row.get("name", "")).strip() or f"row-{index}"
            readmes.append(_readme(row))
        except (KeyError, TypeError, ValueError) as exc:
            readme_errors.append(ReadmeError(name=name, error_type=type(exc).__name__))
    return Settings(
        remote=data["remote"],
        main_branch=data["main_branch"],
        worktree=_relative_inside(data["worktree"], "worktree", "settings"),
        branch_prefix=data["branch_prefix"],
        fetch_timeout_s=int(data["fetch_timeout_s"]),
        push_timeout_s=int(data["push_timeout_s"]),
        forge_timeout_s=int(data["forge_timeout_s"]),
        forge_command=data["forge_command"],
        forge_requires=data.get("forge_requires", ""),
        id_prefix_manifest=_relative_inside(
            data["id_prefix_manifest"], "id_prefix_manifest", "settings"
        ),
        redaction_replacement=data["redaction_replacement"],
        redaction_note=data["redaction_note"],
        owner_names=tuple(data.get("owner_names", ())),
        owner_replacement=data.get("owner_replacement", ""),
        denylist_replacement=data.get("denylist_replacement", "(employer ref)"),
        name_replacements=tuple(
            (re.compile(row["pattern"]), row["replacement"])
            for row in data.get("name_replacement", [])
        ),
        readmes=tuple(readmes),
        readme_errors=tuple(readme_errors),
    )


def id_pattern(harness: Path, settings: Settings) -> re.Pattern[str]:
    """Every ``<PREFIX>-<id>`` token for the prefixes the fleet manifest lists right now."""
    manifest = inside(harness, settings.id_prefix_manifest).read_text(encoding="utf-8")
    prefixes = sorted(set(_PREFIX_ROW.findall(manifest)), key=len, reverse=True)
    if not prefixes:
        raise SyncError(
            "read-id-prefixes",
            1,
            reason=f"no `prefix:` rows in {settings.id_prefix_manifest}",
        )
    alternatives = "|".join(re.escape(prefix) for prefix in prefixes)
    return re.compile(
        rf"(?<![A-Za-z0-9_])(?:{alternatives})-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*"
    )


def redact(text: str, pattern: re.Pattern[str], replacement: str) -> str:
    """Replace every id token. A slug id can be all letters (``ISSUE-REF``), so nothing tells an
    id from prose such as ``ISSUE-REF``; over-redacting is the safe side for a public consumer."""
    return pattern.sub(replacement, text)


def redact_owner(text: str, settings: Settings) -> str:
    """Name the role, never a person (riders D-18): each listed owner name, as a whole word,
    reads ``owner_replacement`` in a redacted copy (held design D-10)."""
    for name in settings.owner_names:
        text = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
            settings.owner_replacement,
            text,
        )
    return text


def redact_names(text: str, settings: Settings) -> str:
    """Each ``[[name_replacement]]`` pattern, in order, reads its replacement: the employer
    infrastructure names the public-repo denylist does not carry."""
    for pattern, replacement in settings.name_replacements:
        text = pattern.sub(replacement, text)
    return text


def redact_denylist(
    text: str,
    harness: Path,
    replacement: str,
    patterns_path: str = "config/public-repo-denylist.txt",
) -> str:
    """Read a publication pattern file at call time (held design D-10 amendment)."""
    # The check is a standalone script, not an installed package module. Mirror its
    # load_patterns() comment/blank-line rules and case-sensitive regex semantics here.
    # Every current pattern is usable for substitution, including whole-word boundaries.
    path = inside(harness, patterns_path)
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            text = re.sub(stripped, lambda _match: replacement, text)
    return text


def render(
    harness: Path, settings: Settings, readme: Readme, mode: str | None = None
) -> str:
    """The consumer copy under ``mode`` (default the row's); ``PENDING`` renders verbatim, the
    founding ask's literal text, so a held comparison still has something to compare."""
    mode = mode or readme.publication_mode
    body = inside(harness, readme.canonical).read_text(encoding="utf-8")
    redacting = mode == REDACT
    if redacting:
        body = redact(
            body, id_pattern(harness, settings), settings.redaction_replacement
        )
        body = redact_owner(body, settings)
        body = redact_names(body, settings)
        body = redact_denylist(body, harness, settings.denylist_replacement)
    if readme.banner_template != NO_BANNER:
        banner = Template(
            inside(harness, readme.banner_template).read_text(encoding="utf-8")
        ).substitute(
            name=readme.name,
            canonical_repo=CANONICAL_REPO,
            canonical_path=readme.canonical,
            consumer_repo=readme.consumer_repo,
            propagate_command=readme.propagate_command,
            redaction_note=settings.redaction_note if redacting else "",
        )
        body = banner.rstrip("\n") + "\n\n" + body
    if redacting and readme.banner_template != NO_BANNER:
        # Consumer rules cover the banner too: it carries repository names and paths. A bannerless
        # row (the held rule) keeps working paths and differs from its source only by the owner's
        # name (held design D-10), so this pass does not apply to it.
        body = redact_denylist(
            body,
            harness,
            settings.redaction_replacement,
            "config/public-copy-redactions.txt",
        )
    return body


def _env() -> dict[str, str]:
    return {
        key: value for key, value in os.environ.items() if key not in _GIT_LOCATION_VARS
    }


def _run(
    argv: Sequence[str],
    *,
    operation: str,
    cwd: Path | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            env=_env(),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SyncError(operation, None, [shlex.join(argv)]) from exc
    except OSError as exc:
        raise SyncError(operation, 127, [shlex.join(argv)]) from exc


def _ok(
    argv: Sequence[str],
    *,
    operation: str,
    cwd: Path | None = None,
    timeout: int | None = None,
) -> str:
    result = _run(argv, operation=operation, cwd=cwd, timeout=timeout)
    if result.returncode:
        raise SyncError(operation, result.returncode, [shlex.join(argv)])
    return result.stdout.strip()


def _git(repo: Path, *args: str) -> list[str]:
    return ["git", "-C", str(repo), *args]


def forge_command(
    settings: Settings, environ: Mapping[str, str] = os.environ
) -> list[str] | None:
    """The forge CLI for a PR, or None when this host has none (a ``-merge`` step instead).

    The registry's ``forge_command`` counts only when it resolves on PATH and its
    ``forge_requires`` path (the wrapper's own precondition) exists.
    """
    override = environ.get(FORGE_ENV, "").strip()
    if override:
        return shlex.split(override)
    command = shlex.split(settings.forge_command)
    requires = settings.forge_requires
    if not command or not shutil.which(command[0]):
        return None
    if requires and not Path(requires).expanduser().exists():
        return None
    return command


@dataclass
class _Row:
    harness: Path
    settings: Settings
    readme: Readme
    consumer: Path
    host: str

    @property
    def worktree(self) -> Path:
        return inside(self.consumer, self.settings.worktree)

    @property
    def branch(self) -> str:
        return f"{self.settings.branch_prefix}/{self.host}"

    @property
    def main_ref(self) -> str:
        # Full ref: a bare `origin/main` resolves a tag or local branch of that name first.
        return f"refs/remotes/{self.settings.remote}/{self.settings.main_branch}"

    def register(
        self, suffix: str, title: str, why: str, commands: list[str], follow: str = ""
    ) -> None:
        step = manual_steps.ManualStep(
            id=f"canonical-readme-{self.readme.name}-{suffix}",
            title=title,
            why=why,
            commands=commands,
            follow=follow or self.readme.follow,
            source_step=LABEL,
        )
        if manual_steps.register(step):
            print(manual_steps.notice(step, manual_steps.ledger_path()))


def _show(row: _Row, ref: str) -> str | None:
    result = _run(
        _git(row.consumer, "show", f"{ref}:{row.readme.consumer_path}"),
        operation="fetch",
    )
    return result.stdout if result.returncode == 0 else None


def _remote_branch_exists(row: _Row) -> bool:
    argv = _git(
        row.consumer,
        "ls-remote",
        "--exit-code",
        "--heads",
        row.settings.remote,
        row.branch,
    )
    result = _run(argv, operation="ls-remote", timeout=row.settings.fetch_timeout_s)
    if result.returncode == LS_REMOTE_NO_MATCH:
        return False
    if result.returncode:
        raise SyncError("ls-remote", result.returncode, [shlex.join(argv)])
    return True


def _registered_worktrees(consumer: Path) -> dict[Path, str]:
    listing = _ok(
        _git(consumer, "worktree", "list", "--porcelain"), operation="list-worktrees"
    )
    worktrees: dict[Path, str] = {}
    current: Path | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree ")).resolve()
            worktrees[current] = ""
        elif line.startswith("branch ") and current is not None:
            worktrees[current] = line.removeprefix("branch refs/heads/")
    return worktrees


def _prepare_worktree(row: _Row, base: str, remote_branch: bool) -> None:
    """The sync worktree on the sync branch, containing ``base``; never discards a commit."""
    path, consumer, branch = row.worktree, row.consumer, row.branch
    op = "prepare-worktree"
    local = _run(
        _git(consumer, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"),
        operation=op,
    )
    has_local = local.returncode == 0
    registered = _registered_worktrees(consumer)
    if path.resolve() in registered:
        if _ok(_git(path, "status", "--porcelain"), operation=op):
            raise SyncError(
                op,
                1,
                [f"git -C {path} status"],
                "the sync worktree holds uncommitted changes from an earlier run",
            )
        if registered[path.resolve()] != branch:
            if has_local:
                _ok(_git(path, "switch", "--quiet", branch), operation=op)
            else:
                _ok(
                    _git(path, "switch", "--quiet", "--no-track", "-c", branch, base),
                    operation=op,
                )
                return
    elif path.exists():
        raise SyncError(
            op, 1, [f"ls -la {path}"], "the worktree path exists and is not a worktree"
        )
    elif has_local:
        _ok(
            _git(consumer, "worktree", "add", "--quiet", str(path), branch),
            operation=op,
        )
    else:
        _ok(
            _git(
                consumer,
                "worktree",
                "add",
                "--quiet",
                "--no-track",
                "-b",
                branch,
                str(path),
                base,
            ),
            operation=op,
        )
        return
    if (
        _run(_git(path, "merge", "--ff-only", "--quiet", base), operation=op).returncode
        == 0
    ):
        return
    if remote_branch:
        raise SyncError(
            op,
            1,
            [f"git -C {path} log --oneline --left-right {branch}...{base}"],
            f"the local sync branch and {base} have diverged",
        )
    # Only this step's own unpushed commit can be here; replay it onto the moved main.
    rebase = _run(_git(path, "rebase", "--quiet", base), operation=op)
    if rebase.returncode:
        _run(_git(path, "rebase", "--abort"), operation=op)
        raise SyncError(op, rebase.returncode, [f"git -C {path} rebase {base}"])


def _restore_written(worktree: Path, written: Sequence[tuple[str, Path]]) -> None:
    """Undo the copies a failed batch already wrote, so the sync worktree is left clean: a
    tracked path goes back to HEAD, an untracked one is removed."""
    for rel, target in written:
        probe = _git(worktree, "ls-files", "--error-unmatch", "--", rel)
        if _run(probe, operation="write-copy").returncode == 0:
            _run(_git(worktree, "checkout", "--", rel), operation="write-copy")
        elif target.is_file():
            target.unlink()


def _commit(rows: Sequence[tuple[_Row, str]], canonical_sha: str) -> None:
    """Write every drifted copy for one consumer and commit them as one publication."""
    # Guard first, for EVERY row: each resolved target must lie inside the sync worktree before
    # any copy is written, so a later row's escape can never leave an earlier row's write behind.
    planned: list[tuple[_Row, str, Path]] = []
    for row, expected in rows:
        try:
            target = inside(row.worktree, row.readme.consumer_path)
        except ValueError as exc:
            raise SyncError("write-copy", 1, [RERUN], row_name=row.readme.name) from exc
        # `inside` follows symlinks, and the worktree's own `.git` file is inside it: a committed
        # symlink on the path would have the write clobber that file or another tracked one.
        parts = Path(row.readme.consumer_path).parts
        if any(
            row.worktree.joinpath(*parts[:n]).is_symlink()
            for n in range(1, len(parts) + 1)
        ):
            raise SyncError(
                "write-copy",
                1,
                [f"ls -la {target}", RERUN],
                "the consumer path is a symlink in the sync worktree",
                row_name=row.readme.name,
            )
        planned.append((row, expected, target))
    # Then mutate; a write that fails restores what the batch had written and names its row.
    written: list[tuple[str, Path]] = []
    paths: list[str] = []
    for row, expected, target in planned:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(expected.encode("utf-8"))
        except OSError as exc:
            _restore_written(row.worktree, written)
            raise SyncError("write-copy", 1, [RERUN], row_name=row.readme.name) from exc
        written.append((row.readme.consumer_path, target))
        paths.append(row.readme.consumer_path)
    primary = rows[0][0]
    _ok(_git(primary.worktree, "add", "--", *paths), operation="commit")
    subject = paths[0] if len(paths) == 1 else f"{len(paths)} canonical copies"
    title = f"docs: sync {subject} from {CANONICAL_REPO} {canonical_sha[:12]} ({LABEL})"
    details = ", ".join(
        f"{row.readme.name}:{row.readme.consumer_path} ({row.readme.publication_mode})"
        for row, _expected in rows
    )
    body = (
        f"Regenerated by `{RERUN}` from {CANONICAL_REPO} at {canonical_sha}. "
        f"Rows: {details}."
    )
    _ok(
        _git(
            primary.worktree,
            "commit",
            "--quiet",
            "-m",
            title,
            "-m",
            body,
            "-m",
            PROVENANCE,
        ),
        operation="commit",
    )


def _forge(
    row: _Row, forge: list[str], operation: str, *args: str
) -> subprocess.CompletedProcess[str]:
    # A neutral cwd: `pr merge --delete-branch` run inside the consumer would also switch and
    # delete the local branch the sync worktree holds.
    with tempfile.TemporaryDirectory(prefix="canonical-readme-sync-") as neutral:
        return _run(
            [*forge, *args],
            operation=operation,
            cwd=Path(neutral),
            timeout=row.settings.forge_timeout_s,
        )


def _forge_ok(row: _Row, forge: list[str], operation: str, *args: str) -> str:
    result = _forge(row, forge, operation, *args)
    if result.returncode:
        raise SyncError(operation, result.returncode, [shlex.join([*forge, *args])])
    return result.stdout.strip()


def _open_pr(
    row: _Row,
    forge: list[str],
    canonical_sha: str,
    rows: Sequence[tuple[_Row, str]],
) -> int:
    slug, main = row.readme.consumer_slug, row.settings.main_branch
    listed = _forge_ok(
        row, forge, "pr-list", "pr", "list", "--repo", slug, "--head", row.branch,
        "--base", main, "--state", "open", "--json", "number,url",
    )  # fmt: skip
    try:
        existing = json.loads(listed or "[]")
    except json.JSONDecodeError as exc:
        raise SyncError("pr-list", 0, reason="the forge printed no JSON") from exc
    if existing:
        return int(existing[0]["number"])
    with tempfile.NamedTemporaryFile(
        "w",
        suffix=".md",
        prefix="canonical-readme-sync-",
        delete=False,
        encoding="utf-8",
    ) as handle:
        paths = ", ".join(f"`{item.readme.consumer_path}`" for item, _ in rows)
        handle.write(
            f"`{RERUN}` regenerated {paths} from `{CANONICAL_REPO}` at {canonical_sha}.\n\n"
            f"After the merge, `{row.readme.propagate_command}` is a manual step for the "
            "installing session.\n"
        )
    try:
        created = _forge_ok(
            row, forge, "pr-create", "pr", "create", "--repo", slug, "--base", main,
            "--head", row.branch, "--title",
            f"docs: sync canonical copies from {CANONICAL_REPO}",
            "--body-file", handle.name,
        )  # fmt: skip
    finally:
        Path(handle.name).unlink(missing_ok=True)
    number = created.splitlines()[-1].rstrip("/").rsplit("/", 1)[-1] if created else ""
    if not number.isdigit():
        raise SyncError("pr-create", 0, reason="the forge printed no PR URL")
    return int(number)


def _checks_green(row: _Row, forge: list[str], number: int) -> bool:
    result = _forge(
        row,
        forge,
        "pr-checks",
        "pr",
        "checks",
        str(number),
        "--repo",
        row.readme.consumer_slug,
    )
    # The fleet runs no forge CI (github-actions-retirement.md): no checks is not a red check.
    return (
        result.returncode == 0 or "no checks reported" in result.stderr + result.stdout
    )


def _fast_forward_main_tree(row: _Row) -> str:
    consumer, main, op = row.consumer, row.settings.main_branch, "verify-landed"
    current = _run(
        _git(consumer, "symbolic-ref", "--quiet", "--short", "HEAD"), operation=op
    ).stdout.strip()
    if current != main:
        return (
            f"not fast-forwarded: it is on {current or 'a detached HEAD'}, not {main}"
        )
    if _ok(
        _git(consumer, "status", "--porcelain", "--untracked-files=no"), operation=op
    ):
        return "not fast-forwarded: it has uncommitted changes"
    if _run(
        _git(consumer, "merge", "--ff-only", "--quiet", row.main_ref), operation=op
    ).returncode:
        return f"not fast-forwarded: {main} has commits {row.main_ref} lacks"
    return "fast-forwarded"


def _propagate_step(
    row: _Row, why: str, rows: Sequence[tuple[_Row, str]] | None = None
) -> None:
    rows = rows or [(row, "")]
    excluded = [
        item
        for item, _expected in rows
        if item.readme.publication_mode == EXCLUDE_PUBLIC
    ]
    if excluded:
        paths = ", ".join(item.readme.consumer_path for item in excluded)
        why += (
            f" Before propagating, add the {row.readme.consumer_repo} copier variable that puts "
            f"{paths} in `_exclude` for public consumers: this mode copies "
            "the canonical text verbatim, and it must not reach a public repository."
        )
    paths = ", ".join(item.readme.consumer_path for item, _expected in rows)
    propagate_commands = list(
        dict.fromkeys(item.readme.propagate_command for item, _expected in rows)
    )
    row.register(
        "propagate",
        f"Propagate the regenerated {paths} to {row.readme.consumer_repo}'s "
        "consumers and review every change",
        why,
        [
            f"git -C {row.consumer} fetch {row.settings.remote} {row.settings.main_branch}",
            f"git -C {row.consumer} merge --ff-only {row.main_ref}",
            *propagate_commands,
        ],
    )


def _decision_pending_step(row: _Row, state: str) -> None:
    readme = row.readme
    reason = (
        f"its publication_mode is {PENDING}"
        if readme.publication_mode == PENDING
        else "its publication_hold is true"
    )
    row.register(
        "decision-pending",
        f"Publication of {readme.consumer_path} waits on {readme.decision or 'an open decision'}",
        f"The {readme.name} row is report-only because {reason}: {readme.decision} is open with "
        f"the maintainer (held item {readme.decision_held}). The consumer copy is {state}; nothing was "
        "written, committed, pushed or propagated. Surface the held item, never answer it.",
        [],
        follow=readme.decision_held or readme.follow,
    )


def _sync_row(row: _Row) -> str | None:
    """Inspect one row and return its expected bytes only when it needs publication."""
    readme, settings, consumer = row.readme, row.settings, row.consumer
    if not (row.harness / readme.canonical).is_file():
        print(
            f"  [skip] {LABEL}: {readme.name}: canonical-missing ({readme.canonical})"
        )
        return None
    if not consumer.is_dir():
        forge = settings.forge_command or "git"
        row.register(
            "consumer-absent",
            f"{readme.consumer_repo} is not on this host",
            f"{LABEL} keeps {readme.consumer_repo}/{readme.consumer_path} in step with "
            f"{CANONICAL_REPO}/{readme.canonical}, and {consumer} does not exist. Clone it, or "
            "confirm with the maintainer that this host is not expected to carry it.",
            [f"{forge} repo clone {readme.consumer_slug} {consumer}", RERUN],
            follow=INSTALL_DOC,
        )
        return None
    held = readme.publication_hold or readme.publication_mode == PENDING
    try:
        expected = render(row.harness, settings, readme)
    except (OSError, KeyError, ValueError) as exc:
        raise SyncError("render", 1, [RERUN]) from exc
    _ok(
        _git(consumer, "fetch", "--quiet", settings.remote, settings.main_branch),
        operation="fetch",
        timeout=settings.fetch_timeout_s,
    )
    current = _show(row, row.main_ref)
    state = "in-sync" if current == expected else "drift"
    where = f"{readme.consumer_repo} {row.main_ref}:{readme.consumer_path}"
    if held:
        print(
            f"  [info] {LABEL}: {readme.name}: {state} ({where}); report-only: publication held"
        )
        _decision_pending_step(row, "in sync" if state == "in-sync" else "drifted")
        return None
    if state == "in-sync":
        print(f"  [ok]  {LABEL}: {readme.name}: in-sync ({where})")
        return None
    return expected


def _publish(rows: Sequence[tuple[_Row, str]]) -> None:
    """Publish every drifted row for one consumer as one ordered branch operation."""
    row = rows[0][0]
    readme, settings, consumer = row.readme, row.settings, row.consumer
    if not row.host:
        raise SyncError("host-identity", 1, [RERUN], "AI_HOST is unset")
    canonical_sha = _ok(
        _git(row.harness, "rev-parse", "HEAD"), operation="read-harness-head"
    )
    remote_branch = _remote_branch_exists(row)
    base = row.main_ref
    if remote_branch:
        _ok(
            _git(consumer, "fetch", "--quiet", settings.remote, row.branch),
            operation="fetch",
            timeout=settings.fetch_timeout_s,
        )
        base = f"refs/remotes/{settings.remote}/{row.branch}"
    _prepare_worktree(row, base, remote_branch)
    changed = [
        (item, expected)
        for item, expected in rows
        if not (item.worktree / item.readme.consumer_path).is_file()
        or (item.worktree / item.readme.consumer_path).read_text(encoding="utf-8")
        != expected
    ]
    if not changed:
        note = "already on the sync branch"
    else:
        _commit(changed, canonical_sha)
        note = f"committed {len(changed)} canonical cop{'y' if len(changed) == 1 else 'ies'}"
    head = _ok(_git(row.worktree, "rev-parse", "HEAD"), operation="commit")
    _ok(
        _git(
            row.worktree,
            "push",
            "--quiet",
            settings.remote,
            f"HEAD:refs/heads/{row.branch}",
        ),
        operation="push",
        timeout=settings.push_timeout_s,
    )
    names = ",".join(item.readme.name for item, _expected in rows)
    print(f"  [ok]  {LABEL}: {names}: {note} {head[:12]} and pushed {row.branch}")
    forge = forge_command(settings)
    merge_commands = [
        f"{settings.forge_command} pr create --repo {readme.consumer_slug} "
        f"--base {settings.main_branch} --head {row.branch} --fill",
        f"{settings.forge_command} pr merge {row.branch} --repo {readme.consumer_slug} "
        "--squash --delete-branch",
    ]
    if forge is None:
        row.register(
            "merge",
            f"Merge {row.branch} into {readme.consumer_repo} {settings.main_branch}",
            f"{LABEL} pushed {head[:12]} with {len(rows)} canonical copy row(s) to "
            f"{row.branch}, and this host has no usable "
            f"`{settings.forge_command}` to open and merge the PR.",
            merge_commands,
        )
        _propagate_step(
            row,
            f"The canonical copies change once {row.branch} is merged.",
            rows,
        )
        return
    _merge_through_forge(row, forge, head, canonical_sha, rows)


def _merge_through_forge(
    row: _Row,
    forge: list[str],
    head: str,
    canonical_sha: str,
    rows: Sequence[tuple[_Row, str]],
) -> None:
    readme, settings, consumer = row.readme, row.settings, row.consumer
    number = _open_pr(row, forge, canonical_sha, rows)
    if not _checks_green(row, forge, number):
        row.register(
            "merge",
            f"Merge PR #{number} ({row.branch}) into {readme.consumer_repo} once its checks pass",
            f"{LABEL} left PR #{number} open because its checks are not green; read them with "
            f"`{settings.forge_command} pr checks {number} --repo {readme.consumer_slug}`.",
            [
                f"{settings.forge_command} pr merge {number} --repo {readme.consumer_slug} "
                "--squash --delete-branch"
            ],
        )
        _propagate_step(
            row, f"The canonical copies change once PR #{number} is merged.", rows
        )
        return
    _forge_ok(
        row, forge, "pr-merge", "pr", "merge", str(number), "--repo", readme.consumer_slug,
        "--squash", "--delete-branch", "--match-head-commit", head,
    )  # fmt: skip
    _ok(
        _git(consumer, "fetch", "--quiet", settings.remote, settings.main_branch),
        operation="fetch",
        timeout=settings.fetch_timeout_s,
    )
    for item, expected in rows:
        if _show(item, item.main_ref) != expected:
            raise SyncError(
                "verify-landed",
                1,
                [f"git -C {consumer} show {item.main_ref}:{item.readme.consumer_path}"],
                f"PR #{number} merged, but {item.main_ref} does not carry the expected copy",
            )
    # Landed by content, so the local sync branch holds nothing unique: retire it, and park the
    # worktree on main for the next run.
    _ok(
        _git(row.worktree, "switch", "--quiet", "--detach", row.main_ref),
        operation="retire-branch",
    )
    _ok(
        _git(consumer, "branch", "--quiet", "-D", row.branch), operation="retire-branch"
    )
    tree = _fast_forward_main_tree(row)
    print(f"  [ok]  {LABEL}: {readme.name}: PR #{number} merged; main tree {tree}")
    _propagate_step(
        row,
        f"PR #{number} merged {len(rows)} regenerated canonical copy row(s) into "
        f"{readme.consumer_repo} {settings.main_branch} (main tree {tree}); consumers receive "
        f"it only through `{readme.propagate_command}`, which a session runs and reviews by hand.",
        rows,
    )


def _register_failure(row: _Row, error: SyncError) -> None:
    readme = row.readme
    print(f"  [warn] {LABEL}: {readme.name}: {error.diagnostic()}")
    row.register(
        "failed",
        f"Finish the {readme.name} sync into {readme.consumer_repo} by hand",
        f"{error.diagnostic()} in repository {readme.consumer_repo} for "
        f"{readme.consumer_path}. Run the commands below to see the cause and finish by hand.",
        [*error.commands, RERUN] if RERUN not in error.commands else error.commands,
    )


def _register_row_error(error: ReadmeError) -> None:
    """Report one malformed row without exposing its values or suppressing valid rows."""
    name = manual_steps.sanitize(error.name)
    print(
        f"  [warn] {LABEL}: {name}: read-registry failed (exit 1): {error.error_type}"
    )
    step = manual_steps.ManualStep(
        id=f"canonical-readme-{name}-registry-failed",
        title=f"Fix the {name} row in {CONFIG_RELPATH}",
        why=f"{LABEL} refused this row ({error.error_type}); other valid rows continued.",
        commands=[RERUN],
        follow=INSTALL_DOC,
        source_step=LABEL,
    )
    if manual_steps.register(step):
        print(manual_steps.notice(step, manual_steps.ledger_path()))


def _inspect_readme(
    harness: Path,
    settings: Settings,
    projects_root: Path,
    host: str,
    readme: Readme,
) -> tuple[Path, tuple[_Row, str]] | None:
    """Run one independently validated row and return a publication candidate, if any."""
    row: _Row | None = None
    try:
        consumer = inside(projects_root, readme.consumer_repo)
        row = _Row(harness, settings, readme, consumer, host)
        expected = _sync_row(row)
        return None if expected is None else (consumer.resolve(), (row, expected))
    except SyncError as exc:
        if row is not None:
            _register_failure(row, exc)
    except (OSError, KeyError, ValueError) as exc:
        if row is not None:
            _register_failure(row, SyncError("unknown", 1, [RERUN], type(exc).__name__))
        else:
            _register_row_error(
                ReadmeError(name=readme.name, error_type=type(exc).__name__)
            )
    return None


def _publish_group(rows: Sequence[tuple[_Row, str]]) -> None:
    try:
        _publish(rows)
    except SyncError as exc:
        # The failing row's own step when the error names one, else the batch's first row.
        failed = next(
            (row for row, _expected in rows if row.readme.name == exc.row_name),
            rows[0][0],
        )
        _register_failure(failed, exc)
    except (OSError, KeyError, ValueError) as exc:
        _register_failure(
            rows[0][0], SyncError("unknown", 1, [RERUN], type(exc).__name__)
        )


def sync(*, harness: Path, projects_root: Path, host: str) -> int:
    try:
        settings = load_settings(harness)
    except (OSError, KeyError, ValueError) as exc:
        print(
            f"  [warn] {LABEL}: {SyncError('read-registry', 1).diagnostic()}: {type(exc).__name__}"
        )
        manual_steps.register(
            manual_steps.ManualStep(
                id="canonical-readme-registry-failed",
                title=f"Fix {CONFIG_RELPATH} so the canonical-README sync can run",
                why=f"{LABEL} could not read its registry ({type(exc).__name__}).",
                commands=[RERUN],
                follow=INSTALL_DOC,
                source_step=LABEL,
            )
        )
        return 0
    if settings is None:
        print(f"  [skip] {LABEL}: no registry at {harness / CONFIG_RELPATH}")
        return 0
    for error in settings.readme_errors:
        _register_row_error(error)
    publications: dict[Path, list[tuple[_Row, str]]] = {}
    for readme in settings.readmes:
        candidate = _inspect_readme(harness, settings, projects_root, host, readme)
        if candidate is not None:
            consumer, row = candidate
            publications.setdefault(consumer, []).append(row)
    for rows in publications.values():
        _publish_group(rows)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ai_chassis.canonical_readme_sync")
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("render", help="print the expected consumer copy of one row")
    show.add_argument("name")
    show.add_argument("--harness", type=Path, default=HARNESS_ROOT)
    show.add_argument(
        "--mode",
        choices=sorted(PUBLICATION_MODES - {PENDING}),
        help="render under this publication mode instead of the row's",
    )
    args = parser.parse_args(argv)
    settings = load_settings(args.harness)
    rows = {readme.name: readme for readme in (settings.readmes if settings else ())}
    if settings is None or args.name not in rows:
        print(f"{LABEL}: no [[readme]] row named {args.name!r}", file=sys.stderr)
        return 2
    sys.stdout.write(render(args.harness, settings, rows[args.name], args.mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
