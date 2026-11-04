"""Install the Claude Code plugins the settings template enables (ISSUE-REF).

Third-party skills reach the fleet as their upstream plugin, never as a copy under
``skills/``: a copy never updates, while a plugin from a marketplace declared with
``autoUpdate`` refreshes at Claude Code startup. Declaring the plugin in settings is not
enough on its own (measured on Claude Code 2.1.296): ``enabledPlugins`` plus
``extraKnownMarketplaces`` installs nothing, and ``claude plugin install`` refuses a
marketplace the CLI has not added. So this step adds the marketplace and installs the
plugin through the CLI when ``claude plugin list`` does not show it.

It then retires every local-scope row of a plugin it installed at user scope. A local row pins
its project to the copy it installed, which never follows the user install: on example-project-ec2 seven
ai-harness worktrees kept beads-ui 0.1.x, which registers ``/beads`` where 0.2.0 registers
``/show-beads-ui`` (measured 2026-10-24). ``claude plugin uninstall --scope local`` retires the
row of the project it runs in, so each runs from that project's directory. A directory that is
gone (a reaped worktree, whose path a new worktree may reuse) is recreated for the call and the
directories created are then removed one level at a time, never recursively.

``claude plugin marketplace add`` rewrites the settings entry without ``autoUpdate``,
which is why install.sh runs this step before the settings merge: the merge then
restores ``autoUpdate`` from the template.

``ensure_slack_plugin`` separately reads the machine's optional Slack policy. It installs one
user copy, then owns only Slack's enablement key in user settings and untracked repository
local settings. Main-checkout local settings also govern Claude Code worktree sessions.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from fnmatch import fnmatchcase
from pathlib import Path

from .rendered_links import _git_run
from .settings_merge import SettingsMergeError, _read_json_object, _write_if_changed

_TIMEOUT_SECONDS = 300
_SLACK = "slack@claude-plugins-official"
_SLACK_MARKETPLACE = "claude-plugins-official"
_SLACK_SOURCE = {
    "source": {"source": "github", "repo": "anthropics/claude-plugins-official"},
    "autoUpdate": True,
}


class PluginInstallError(RuntimeError):
    """A declared plugin could not be installed."""


def _claude(
    binary: str, *args: str, cwd: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [binary, "plugin", *args],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
        cwd=cwd,
    )


def _user_scope(row: Mapping[str, object]) -> bool:
    """A row ``claude plugin list`` reports at user scope; a CLI without scopes reports none.

    The list carries every scope's install, so a local-scope row for some other project (four
    ai-harness worktrees, measured on example-project-ec2 2026-10-23) loads nowhere else and must not
    stand in for the user-scope install every repository's session needs.
    """
    return row.get("scope", "user") == "user"


def _local_projects(rows: list[Mapping[str, object]], plugin_id: str) -> list[str]:
    return sorted(
        str(row["projectPath"])
        for row in rows
        if row["id"] == plugin_id
        and row.get("scope") == "local"
        and isinstance(row.get("projectPath"), str)
    )


def _rows(binary: str, *args: str, key: str) -> list[Mapping[str, object]]:
    result = _claude(binary, *args, "--json")
    if result.returncode != 0:
        raise PluginInstallError(
            f"`claude plugin {' '.join(args)} --json` exited {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError:
        rows = None
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) and isinstance(row.get(key), str) for row in rows
    ):
        raise PluginInstallError(
            f"`claude plugin {' '.join(args)} --json` printed no list of objects with a "
            f"{key!r}: {result.stdout[:200]!r}"
        )
    return rows


def _listed(binary: str, *args: str, key: str) -> set[str]:
    return {str(row[key]) for row in _rows(binary, *args, key=key)}


def _marketplace_source(marketplaces: Mapping[str, object], name: str) -> str:
    entry = marketplaces.get(name)
    source = entry.get("source") if isinstance(entry, dict) else None
    if (
        isinstance(source, dict)
        and source.get("source") == "github"
        and source.get("repo")
    ):
        return str(source["repo"])
    raise PluginInstallError(
        f"marketplace {name!r} has no GitHub source in the settings template's extraKnownMarketplaces"
    )


def _run_or_raise(
    binary: str,
    label: str,
    *args: str,
    flags: dict[tuple[str, ...], tuple[str, ...]],
    scope: str = "user",
    cwd: str | None = None,
) -> None:
    command = args[:2] if args[0] == "marketplace" else args[:1]
    if command not in flags:
        help_result = _claude(binary, *command, "--help")
        if help_result.returncode != 0:
            raise PluginInstallError(
                f"could not read help for plugin {' '.join(command)}"
            )
        # The data directory is per plugin, not per scope: retiring a local row must never take
        # the user install's data with it.
        optional = (
            ("--json", "--keep-data") if command == ("uninstall",) else ("--json",)
        )
        flags[command] = tuple(
            flag
            for flag in optional
            if re.search(rf"(?<!\S){flag}(?=[\s,=]|$)", help_result.stdout)
        )
    result = _claude(binary, *args, "--scope", scope, *flags[command], cwd=cwd)
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
        raise PluginInstallError(
            f"{label} exited {result.returncode}: {detail[-1] if detail else 'no output'}"
        )


def _disabled_in(settings: Mapping[str, object]) -> set[str]:
    enabled = settings.get("enabledPlugins")
    return {
        plugin_id
        for plugin_id, on in (enabled.items() if isinstance(enabled, dict) else ())
        if on is False
    }


def plugin_disabled_by(
    plugin_id: str, profile: Mapping[str, object] | None
) -> str | None:
    """What sets ``plugin_id`` to ``false`` in ``enabledPlugins``, the rule
    ``ensure_template_plugins`` skips by: the machine settings fragment, then
    ``~/.claude/settings.json``. None when neither does, or the settings file is unreadable."""
    if plugin_id in _disabled_in(profile or {}):
        return "the machine settings fragment"
    settings = Path.home() / ".claude" / "settings.json"
    try:
        parsed = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(parsed, dict) and plugin_id in _disabled_in(parsed):
        return str(settings)
    return None


def _without_disabled(wanted: list[str], disabled: set[str], by: str) -> list[str]:
    for plugin_id in sorted(set(wanted) & disabled):
        print(f"  [skip] plugin {plugin_id}: {by} disables it (enabledPlugins false)")
    return [plugin_id for plugin_id in wanted if plugin_id not in disabled]


def _install_missing(
    binary: str, wanted: list[str], known: Mapping[str, object]
) -> int:
    flags: dict[tuple[str, ...], tuple[str, ...]] = {}
    try:
        rows = _rows(binary, "list", key="id")
        added = _listed(binary, "marketplace", "list", key="name")
    except (PluginInstallError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"  [FAIL] plugins: {exc}", file=sys.stderr)
        return 1
    installed = {str(row["id"]) for row in rows if _user_scope(row)}
    failed = 0
    for plugin_id in wanted:
        if plugin_id in installed:
            print(f"  [ok]  plugin {plugin_id} installed at user scope")
            continue
        marketplace = plugin_id.rpartition("@")[2]
        try:
            if marketplace not in added:
                _run_or_raise(
                    binary,
                    f"adding marketplace {marketplace}",
                    "marketplace",
                    "add",
                    _marketplace_source(known, marketplace),
                    flags=flags,
                )
                added.add(marketplace)
            _run_or_raise(
                binary,
                f"installing {plugin_id}",
                "install",
                plugin_id,
                flags=flags,
            )
        except (PluginInstallError, subprocess.TimeoutExpired, OSError) as exc:
            print(f"  [FAIL] plugin {plugin_id} not installed: {exc}", file=sys.stderr)
            failed = 1
            continue
        print(f"  [fixed] plugin {plugin_id} installed at user scope")
        installed.add(plugin_id)
    for plugin_id in wanted:
        # A failed user install leaves each local copy the only one its project loads.
        if plugin_id in installed:
            failed |= _retire_local_rows(
                binary, plugin_id, _local_projects(rows, plugin_id), flags
            )
    return failed


def _retire_local_rows(
    binary: str,
    plugin_id: str,
    projects: list[str],
    flags: dict[tuple[str, ...], tuple[str, ...]],
) -> int:
    if not projects:
        print(f"  [ok]  plugin {plugin_id} has no local-scope install")
        return 0
    failed = 0
    for project in projects:
        gone = not Path(project).is_dir()
        created: list[Path] = []
        try:
            if gone:
                # A reaped worktree's path is reused by leaf name, and a new worktree there would
                # load the stale copy; the CLI retires only the row of the directory it runs in.
                created = _make_missing(Path(project))
            _run_or_raise(
                binary,
                f"retiring {plugin_id} at local scope for {project}",
                "uninstall",
                plugin_id,
                flags=flags,
                scope="local",
                cwd=project,
            )
        except (PluginInstallError, subprocess.TimeoutExpired, OSError) as exc:
            if gone:
                print(
                    f"  [warn] plugin {plugin_id} local-scope install for {project} not "
                    f"retired: the project directory is gone and retiring it from a "
                    f"recreated one failed: {exc}"
                )
            else:
                print(
                    f"  [FAIL] plugin {plugin_id} local-scope install not retired: {exc}",
                    file=sys.stderr,
                )
                failed = 1
            continue
        finally:
            _remove_created(created)
        print(
            f"  [fixed] plugin {plugin_id} local-scope install for {project} retired; "
            "the user-scope install governs"
        )
    return failed


def _make_missing(path: Path) -> list[Path]:
    """Create ``path`` and each missing parent, returning what was created, outermost first."""
    created: list[Path] = []
    try:
        for directory in [*reversed(path.parents), path]:
            if not directory.exists():
                os.mkdir(directory)
                created.append(directory)
    except OSError:
        _remove_created(created)
        raise
    return created


def _remove_created(created: list[Path]) -> None:
    """Remove the directories ``_make_missing`` created, deepest first, never recursively."""
    for directory in reversed(created):
        try:
            os.rmdir(directory)
        except OSError as exc:
            print(
                f"  [warn] {directory} was created to retire a local-scope plugin install "
                f"and is left in place: {exc.strerror}"
            )
            return


def ensure_template_plugins(
    template: Mapping[str, object], profile: Mapping[str, object] | None = None
) -> int:
    """Install at user scope every plugin ``template`` enables that is not installed there,
    then retire each of its local-scope installs so the user-scope one governs every project.

    A plugin the machine's settings fragment (``profile``) or ``~/.claude/settings.json`` sets
    to ``false`` in ``enabledPlugins`` is skipped with its reason named: installing it would
    enable it again.

    Returns 0 when every declared plugin is installed, disabled or ``claude`` is absent, else 1
    with each failure named on stderr.
    """
    enabled = template.get("enabledPlugins")
    marketplaces = template.get("extraKnownMarketplaces")
    wanted = sorted(
        plugin_id
        for plugin_id, on in (enabled.items() if isinstance(enabled, dict) else ())
        if on is True
    )
    if not wanted:
        print("  [skip] plugins: the settings template enables none")
        return 0
    wanted = _without_disabled(
        wanted, _disabled_in(profile or {}), "the machine settings fragment"
    )
    binary = shutil.which("claude")
    if wanted and binary is None:
        print(
            "  [skip] plugins: claude is not on PATH, so not installed: "
            + ", ".join(wanted)
        )
    if not wanted or binary is None:
        return 0
    settings = Path.home() / ".claude" / "settings.json"
    try:
        parsed = (
            json.loads(settings.read_text(encoding="utf-8"))
            if settings.exists()
            else {}
        )
    except (OSError, json.JSONDecodeError):
        parsed = None
    if not isinstance(parsed, dict):
        print(
            f"  [skip] plugins: {settings} is not a JSON object, and the plugin CLI would rewrite it"
        )
        return 0
    wanted = _without_disabled(wanted, _disabled_in(parsed), str(settings))
    if not wanted:
        return 0
    return _install_missing(
        binary, wanted, marketplaces if isinstance(marketplaces, dict) else {}
    )


def _slack_patterns(policy: Mapping[str, object], key: str) -> list[str]:
    default = ["*"] if key == "slack_plugin_include" else []
    value = policy.get(key, default)
    if not isinstance(value, list) or any(
        not isinstance(pattern, str)
        or not pattern
        or pattern in (".", "..")
        or "/" in pattern
        or "\\" in pattern
        for pattern in value
    ):
        raise PluginInstallError(f"{key} must be a list of repository name patterns")
    return value


def _slack_json(path: Path) -> dict[str, object]:
    # Refuse symlinked files or parents before reading or replacing any settings.
    if path.resolve() != path.absolute():
        raise PluginInstallError("Slack settings path is symlinked")
    data = _read_json_object(path, label="Slack settings") if path.exists() else {}
    if not isinstance(data.get("enabledPlugins", {}), dict):
        raise PluginInstallError("enabledPlugins must be an object")
    return data


def _slack_repositories(projects: Path) -> list[tuple[Path, Path]]:
    """Preflight main checkouts only. Claude reads their local settings in worktrees too."""
    repositories = {}
    if not projects.is_dir():
        return []
    for entry in sorted(projects.iterdir()):
        if entry.is_symlink() or not entry.is_dir() or not (entry / ".git").exists():
            continue
        # Reuse the installer's bounded Git reader, which strips hook repository variables.
        common = _git_run(
            entry, "rev-parse", "--path-format=absolute", "--git-common-dir", timeout=10
        )
        if common is None or common.returncode:
            raise PluginInstallError("cannot resolve Slack repository settings")
        git_dir = Path(common.stdout.strip()).resolve()
        repo = git_dir.parent
        if (
            not repo.is_relative_to(projects)
            or repo == projects
            or git_dir.name != ".git"
        ):
            raise PluginInstallError("Slack repository escapes the projects root")
        if repo in repositories:
            continue
        local = repo / ".claude/settings.local.json"
        tracked = _git_run(
            repo, "ls-files", "--", ".claude/settings.local.json", timeout=10
        )
        if tracked is None or tracked.returncode or tracked.stdout.strip():
            raise PluginInstallError("Slack local settings must be untracked")
        _slack_json(local)
        exclude = git_dir / "info/exclude"
        if exclude.resolve() != exclude.absolute():
            raise PluginInstallError("Slack Git exclude path is symlinked")
        if exclude.exists():
            exclude.read_text(encoding="utf-8")
        repositories[repo] = (repo, exclude)
    return list(repositories.values())


def _slack_enable(path: Path, enabled: bool) -> None:
    data = _slack_json(path)
    plugins = data.setdefault("enabledPlugins", {})
    plugins[_SLACK] = enabled
    _write_if_changed(path, json.dumps(data, indent=2) + "\n")


def _slack_local(repo: Path, exclude: Path, enabled: bool) -> None:
    # Only this untracked file is hidden, never a tracked settings file or a whole directory.
    if exclude.resolve() != exclude.absolute():
        raise PluginInstallError("Slack Git exclude path is symlinked")
    current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    pattern = "/.claude/settings.local.json"
    if pattern not in current.splitlines():
        separator = "\n" if current and not current.endswith("\n") else ""
        _write_if_changed(exclude, current + separator + pattern + "\n")
    _slack_enable(repo / ".claude/settings.local.json", enabled)


def _install_slack(binary: str, user_path: Path) -> None:
    flags: dict[tuple[str, ...], tuple[str, ...]] = {}
    try:
        added = _listed(binary, "marketplace", "list", key="name")
        if _SLACK_MARKETPLACE not in added:
            _run_or_raise(
                binary,
                "adding Slack marketplace",
                "marketplace",
                "add",
                "anthropics/claude-plugins-official",
                flags=flags,
            )
        _run_or_raise(binary, "installing Slack", "install", _SLACK, flags=flags)
    finally:
        # CLI install enables user scope implicitly. Restore restricted policy even on refusal.
        # Re-read to preserve marketplace registration and unrelated CLI writes.
        _slack_enable(user_path, False)


def ensure_slack_plugin(policy: Mapping[str, object], projects_root: Path) -> int:
    """Apply the machine table's optional Slack policy without changing template plugins.

    Include/exclude patterns match main repository names, exclude wins, and global sets the
    user default. Local overrides cover existing checkouts under projects_root and their
    worktrees (Claude Code >=2.1.211). Off means skip, not uninstall or disable a manual install.
    """
    on = policy.get("slack_plugin", False)
    if on is False:
        print("Slack plugin: disabled for this machine profile")
        return 0
    try:
        global_on = policy.get("slack_plugin_global", False)
        if on is not True or not isinstance(global_on, bool):
            raise PluginInstallError("Slack switches must be booleans")
        include = _slack_patterns(policy, "slack_plugin_include")
        exclude = _slack_patterns(policy, "slack_plugin_exclude")
        user_path = Path.home() / ".claude/settings.json"
        user = _slack_json(user_path)
        marketplaces = user.get("extraKnownMarketplaces", {})
        if not isinstance(marketplaces, dict):
            raise PluginInstallError("extraKnownMarketplaces must be an object")
        repositories = _slack_repositories(projects_root.resolve())
    except (PluginInstallError, SettingsMergeError, OSError, ValueError):
        print(
            "Slack plugin: policy or local settings refused; no Slack changes made",
            file=sys.stderr,
        )
        return 1
    binary = shutil.which("claude")
    if binary is None:
        print("Slack plugin: install Claude Code and rerun the installer")
        return 1
    try:
        installed = any(
            row["id"] == _SLACK and _user_scope(row)
            for row in _rows(binary, "list", key="id")
        )
        if not installed:
            # Guard all settings first, then keep repo overrides off until installation succeeds.
            for repo, ignore in repositories:
                _slack_local(repo, ignore, False)
            _slack_enable(user_path, False)
            _install_slack(binary, user_path)
        for repo, ignore in repositories:
            selected = global_on or any(
                fnmatchcase(repo.name, pattern) for pattern in include
            )
            denied = any(fnmatchcase(repo.name, pattern) for pattern in exclude)
            _slack_local(repo, ignore, selected and not denied)
        user = _slack_json(user_path)
        user.setdefault("extraKnownMarketplaces", {})[_SLACK_MARKETPLACE] = (
            _SLACK_SOURCE
        )
        user.setdefault("enabledPlugins", {})[_SLACK] = global_on
        _write_if_changed(user_path, json.dumps(user, indent=2) + "\n")
    except (
        PluginInstallError,
        SettingsMergeError,
        OSError,
        ValueError,
        subprocess.TimeoutExpired,
    ):
        # CLI output can contain credentials. Never interpolate it into the install report.
        print(
            "Slack plugin: CLI installation refused or failed; rerun after resolving it",
            file=sys.stderr,
        )
        return 1
    print(
        "Slack plugin: installed with machine repository policy; authorize Slack on a new host if needed"
    )
    return 0
