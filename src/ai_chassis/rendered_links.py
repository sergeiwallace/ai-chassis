"""Rendered single files linked to the current generation (ISSUE-REF D-11 (a)).

install.sh's retired Steps 3, 3b and 6 linked this machine's compiled context file, the shared
path-scoped rules and ``keybindings.json`` to the repo's main working tree. The core renders the
same files into a generation instead and links each live path to it, so they share the apply
pipeline's atomic swap, drift quarantine and rollback. ``[install.rendered]`` declares which
families a config installs; the destinations are the ones the shell used:

- ``context_files``: ``CLAUDE.<machine>.md`` as ``<projects root>/CLAUDE.md``, ``AGENTS.md``, and
  ``GEMINI.<machine>.md`` as ``GEMINI.md`` only when gemini is enabled for the machine;
- ``rules``: every ``<rules>/*.md`` into ``~/.claude/rules/``, relative markdown links rewritten
  to absolute main-tree paths, since a copy under the state root cannot resolve them;
- ``keybindings``: the named file as ``~/.claude/keybindings.json``.

Every source is read from the MAIN working tree's checkout of the config root (IN-01's re-root),
never from the linked worktree a command happens to run in.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .apply import ApplyError, InstallTarget
from .config import LoadedConfig, load_config
from .machine_registry import machine_tables
from .render import build_render_plan

# install.sh Step 3's fail-closed arm, kept word for word so an operator recognises it.
STEP3_FATAL = (
    "  [FATAL] AI_HOST='{machine}' matches no known profile, so no profile will be installed.\n"
    "          Refusing to fall back to 'linux'. Profiles are the\n"
    "          [machine.<name>] tables in projects-wide-session-config/ai-harness.toml;\n"
    "          add one: docs/procedures/projects-wide-session-config.md § Adding a New "
    "Machine Target"
)

_FENCE = re.compile(r"^\s*(```|~~~)")
_CODE_SPAN = re.compile(r"(`+)(?:.+?)\1")
_INLINE_LINK = re.compile(r"\]\(([^)\s]+)\)")
_REFERENCE_LINK = re.compile(r"^(\s{0,3}\[[^\]]+\]:\s+)(\S+)(.*)$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_IMPORT = re.compile(r"^\s*@[~./A-Za-z]")
_HOOK_REPOSITORY_VARIABLES = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_PREFIX",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    }
)


class RenderedLinksError(ApplyError):
    """The rendered set cannot be resolved or installed safely; nothing was mutated."""


@dataclass(frozen=True)
class RenderedSet:
    """This machine's rendered outputs and the live paths that link to them."""

    outputs: dict[str, bytes]
    targets: tuple[InstallTarget, ...]
    rules: tuple[str, ...]
    rules_dir: Path | None
    machine: str | None
    source_root: Path
    source_commit: str | None
    gemini_enabled: bool | None
    lines: tuple[str, ...]


def _git_run(
    directory: Path, *args: str, timeout: float | None = None
) -> subprocess.CompletedProcess[str] | None:
    """``git -C directory ...`` with the repository variables a git hook exports removed.

    The post-merge path runs inside a hook, where ``GIT_DIR`` names the repo that merged and
    would override ``-C``; the question here is always about ``directory`` itself. A git that
    cannot start, or outlives ``timeout``, is None.
    """
    environ = {
        key: value
        for key, value in os.environ.items()
        if key not in _HOOK_REPOSITORY_VARIABLES
    }
    try:
        return subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            text=True,
            check=False,
            env=environ,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git(directory: Path, *args: str) -> str | None:
    result = _git_run(directory, *args)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def main_tree_config_root(config_root: Path) -> Path:
    """The same config directory in the repo's MAIN working tree (install.sh IN-01).

    A config root inside a linked worktree is re-rooted through the shared git dir, so nothing
    rendered from it can track a transient branch. A root outside any checkout, or one whose
    main-tree twin carries no config, is returned unchanged, as the shell fell back to its own
    directory.
    """
    common = _git(
        config_root, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    toplevel = _git(config_root, "rev-parse", "--show-toplevel")
    if common is None or toplevel is None:
        return config_root
    try:
        relative = config_root.resolve().relative_to(Path(toplevel).resolve())
    except ValueError:
        return config_root
    candidate = Path(common).parent / relative
    if (candidate / "ai-harness.toml").is_file():
        return candidate
    return config_root


def checked_out_branch(source_root: Path) -> str | None:
    """The main tree's ``rev-parse --abbrev-ref HEAD``: its branch, ``HEAD`` when detached, and
    None when git is absent or cannot read it (install.sh's ``|| echo '?'``)."""
    return _git(source_root, "rev-parse", "--abbrev-ref", "HEAD")


def source_commit(source_root: Path) -> str | None:
    """The main tree's HEAD, the commit a generation rendered from it records."""
    return _git(source_root, "rev-parse", "HEAD")


def is_ancestor(source_root: Path, older: str, newer: str) -> bool:
    result = _git_run(source_root, "merge-base", "--is-ancestor", older, newer)
    return result is not None and result.returncode == 0


def _rewrite_target(target: str, source_dir: Path) -> tuple[str, Path] | None:
    if target.startswith(("#", "<", "/", "~", "\\")) or _SCHEME.match(target):
        return None
    path, hash_, fragment = target.partition("#")
    if not path:
        return None
    resolved = Path(os.path.normpath(source_dir / path))
    return f"{resolved.as_posix()}{hash_}{fragment}", resolved


def rewrite_relative_links(text: str, source_dir: Path) -> tuple[str, tuple[Path, ...]]:
    """Rewrite each relative markdown link to an absolute path under ``source_dir`` (D-11 (d)).

    Inline ``](x)`` links and reference definitions are rewritten; fenced blocks and code spans
    are left alone, because the rules quote example links there (``](../research/foo.md)``) as
    prose. Anchors, URLs, absolute and ``<placeholder>`` targets are unchanged. Returns the text
    and every path a link was rewritten to.
    """
    rewritten: list[Path] = []
    lines: list[str] = []
    fenced = False
    for line in text.splitlines(keepends=True):
        if _FENCE.match(line):
            fenced = not fenced
            lines.append(line)
            continue
        if fenced:
            lines.append(line)
            continue
        reference = _REFERENCE_LINK.match(line.rstrip("\n"))
        if reference:
            result = _rewrite_target(reference.group(2), source_dir)
            if result is not None:
                rewritten.append(result[1])
                ending = line[len(line.rstrip("\n")) :]
                lines.append(
                    f"{reference.group(1)}{result[0]}{reference.group(3)}{ending}"
                )
                continue
        pieces: list[str] = []
        cursor = 0
        for span in _CODE_SPAN.finditer(line):
            pieces.append(
                _rewrite_plain(line[cursor : span.start()], source_dir, rewritten)
            )
            pieces.append(span.group(0))
            cursor = span.end()
        pieces.append(_rewrite_plain(line[cursor:], source_dir, rewritten))
        lines.append("".join(pieces))
    return "".join(lines), tuple(rewritten)


def _rewrite_plain(segment: str, source_dir: Path, rewritten: list[Path]) -> str:
    def replace(match: re.Match[str]) -> str:
        result = _rewrite_target(match.group(1), source_dir)
        if result is None:
            return match.group(0)
        rewritten.append(result[1])
        return f"]({result[0]})"

    return _INLINE_LINK.sub(replace, segment)


def has_import(text: str) -> bool:
    """Whether a markdown text carries an ``@``-import line, which a copy would resolve wrongly."""
    return any(_IMPORT.match(line) for line in text.splitlines())


def declaration(config: LoadedConfig) -> dict[str, object] | None:
    value = config.install.get("rendered")
    return value if isinstance(value, dict) and value else None


def resolve_machine(
    config: LoadedConfig, machine: str | None
) -> tuple[str, str | None]:
    """The registry machine ``AI_HOST`` names, through its aliases; Step 3's FATAL otherwise.

    Returns the canonical name and a warning line when ``machine`` was an alias (the shell's
    ``example-project`` arm). An unknown or archived name is a refusal, never a fallback to ``linux``.
    """
    tables = machine_tables(config.data)
    if machine and machine in tables:
        return machine, None
    for name, table in tables.items():
        aliases = table.get("aliases") or ()
        if machine and isinstance(aliases, list) and machine in aliases:
            return name, (
                f"  [warn] AI_HOST={machine} is an alias of {name} — using {name}.\n"
                "         Update AI_HOST in your shell profile."
            )
    raise RenderedLinksError(STEP3_FATAL.format(machine=machine or ""))


def _context_files(
    source_config: LoadedConfig,
    machine: str | None,
    projects_root: Path,
    outputs: dict[str, bytes],
    targets: list[InstallTarget],
) -> tuple[str, bool, str | None]:
    """IN-17: this machine's CLAUDE.md, AGENTS.md, and GEMINI.md when gemini is enabled.

    Returns the canonical machine, whether gemini is enabled, and the alias warning, if any.
    """
    canonical, warning = resolve_machine(source_config, machine)
    plan = build_render_plan(source_config.root)
    claude = f"CLAUDE.{canonical}.md"
    if claude not in plan.outputs:
        raise RenderedLinksError(
            f"the render produced no {claude}; is claude enabled for {canonical}?"
        )
    outputs["CLAUDE.md"] = plan.outputs[claude]
    gemini = f"GEMINI.{canonical}.md" in plan.outputs
    if gemini:
        outputs["GEMINI.md"] = plan.outputs[f"GEMINI.{canonical}.md"]
    outputs["AGENTS.md"] = plan.outputs["AGENTS.md"]
    targets.extend(
        InstallTarget(name, projects_root / name)
        for name in ("CLAUDE.md", "GEMINI.md", "AGENTS.md")
        if name in outputs
    )
    return canonical, gemini, warning


def _rules(
    rules_dir: Path,
    home: Path,
    outputs: dict[str, bytes],
    targets: list[InstallTarget],
) -> tuple[str, ...]:
    """IN-18: every ``rules_dir/*.md`` into ``~/.claude/rules/``, relative links rewritten."""
    if not rules_dir.is_dir():
        raise RenderedLinksError(
            f"install.rendered.rules names a missing directory: {rules_dir}"
        )
    names: list[str] = []
    for rule in sorted(rules_dir.glob("[!.]*.md")):
        try:
            text = rule.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RenderedLinksError(
                f"rule {rule.name} is unreadable at its source {rule}: {exc}"
            ) from exc
        rendered, _links = rewrite_relative_links(text, rules_dir)
        outputs[f"rules/{rule.name}"] = rendered.encode("utf-8")
        targets.append(
            InstallTarget(f"rules/{rule.name}", home / ".claude" / "rules" / rule.name)
        )
        names.append(rule.name)
    return tuple(names)


def _keybindings(
    source: Path,
    home: Path,
    outputs: dict[str, bytes],
    targets: list[InstallTarget],
) -> None:
    """IN-31: the declared file as ``~/.claude/keybindings.json``."""
    if not source.is_file():
        raise RenderedLinksError(
            f"install.rendered.keybindings names a missing file: {source}"
        )
    outputs["keybindings.json"] = source.read_bytes()
    targets.append(
        InstallTarget("keybindings.json", home / ".claude" / "keybindings.json")
    )


def resolve(
    config: LoadedConfig, machine: str | None, projects_root: Path, home: Path
) -> RenderedSet | None:
    """This machine's rendered set from the main tree, or None when nothing is declared."""
    if declaration(config) is None:
        return None
    source_root = main_tree_config_root(config.root)
    source_config = config if source_root == config.root else load_config(source_root)
    declared = declaration(source_config) or {}
    outputs: dict[str, bytes] = {}
    targets: list[InstallTarget] = []
    canonical, gemini, warning = (
        _context_files(source_config, machine, projects_root, outputs, targets)
        if declared.get("context_files")
        else (None, None, None)
    )
    rules_dir = (
        _source_path(source_root, str(declared["rules"]))
        if "rules" in declared
        else None
    )
    rules = _rules(rules_dir, home, outputs, targets) if rules_dir else ()
    if "keybindings" in declared:
        _keybindings(
            _source_path(source_root, str(declared["keybindings"])),
            home,
            outputs,
            targets,
        )
    return RenderedSet(
        outputs=outputs,
        targets=tuple(targets),
        rules=rules,
        rules_dir=rules_dir,
        machine=canonical,
        source_root=source_root,
        source_commit=source_commit(source_root),
        gemini_enabled=gemini,
        lines=(warning,) if warning else (),
    )


def _source_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return Path(os.path.normpath(path if path.is_absolute() else root / path))


def _ours(path: Path, state_root: Path, managed: set[str], legacy: Path) -> bool:
    """A user-scope rule this installer placed: a recorded target, a generation link, or the
    shell's own link into the main tree's rules (the state before the first generation)."""
    if str(path) in managed:
        return True
    if not path.is_symlink():
        return False
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    generations = (state_root / "generations").resolve()
    return resolved.is_relative_to(generations) or resolved == legacy.resolve()


def check_rule_collisions(
    rendered: RenderedSet,
    projects_root: Path,
    home: Path,
    state_root: Path,
    managed: set[str],
) -> None:
    """install.sh Step 3b's two refusals, before anything is mutated.

    A same-name rule under any checkout directly below the projects root would change which rule
    Claude loads; a user-scope file that is not this installer's link is the operator's own. Both
    refuse the whole step, naming the path.
    """
    if not rendered.rules or rendered.rules_dir is None:
        return
    repos = (
        sorted(
            child
            for child in projects_root.iterdir()
            if not child.name.startswith(".") and (child / ".git").exists()
        )
        if projects_root.is_dir()
        else []
    )
    for repo in repos:
        for name in rendered.rules:
            local = repo / ".claude" / "rules" / name
            if local.exists() or local.is_symlink():
                raise RenderedLinksError(
                    f"repo-local rule collision: {local} conflicts with shared rule {name}; "
                    "refusing user-scope delivery"
                )
    for name in rendered.rules:
        destination = home / ".claude" / "rules" / name
        if not (destination.exists() or destination.is_symlink()):
            continue
        if not _ours(destination, state_root, managed, rendered.rules_dir / name):
            raise RenderedLinksError(
                f"user-scope rule collision: {destination} already exists and is not this "
                "installer's shared rule; refusing to overwrite it"
            )
