"""Install the skills tree into ``~/.claude/skills`` and ``~/.agents/skills`` (IN-34).

install.sh's retired Steps 8 and 8b (ISSUE-REF batch C-B3, ruling OP-3 (i)). The skill install
registry (``skill-install-registry.toml``, read here as data) gives each machine profile one of two
modes. Whole-tree, the default and every unlisted profile's: each mount is ONE link to the MAIN
working tree's ``skills/`` (D-11), so a skill's in-tree ``../../_shared/...`` paths resolve
through it. Farm: each mount is a real directory of per-skill links, so a profile can keep a skill
repo-local (linked at ``<repo>/.claude/skills`` instead) or take skills from another repo. Under
Git Bash every link is a junction queued into the run's one batched ``cmd.exe`` launch.

The registry also carries per-profile environment switches (non-secret values written to
``settings.json``'s ``env``, the shell profile or the Windows user environment) and secret
expectations, which are checked for presence by name only: a secret's value is never read into
output. The plan is pure; every write is in this module's appliers.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .links import LinkError
from .profile import (
    _is_windows_shell,
    _persist_windows_user_env,
    _points_at,
    persist_exported_var,
    queue_windows_junction,
)
from .rendered_links import _git_run

REGISTRY_NAME = "skill-install-registry.toml"
SETTINGS_TARGET = "claude-settings"
VALID_TARGETS = frozenset({SETTINGS_TARGET, "shell-profile", "windows-user-env"})
_MODES = ("whole-tree", "farm")
#: The bound on each of the self-clobber guard's two git reads.
_GUARD_GIT_TIMEOUT_S = 30


class PlanRecord(NamedTuple):
    """One line of the plan protocol; fields tab-separated on emission."""

    record_type: str
    fields: tuple[str, ...]

    def emit(self) -> str:
        return "\t".join([self.record_type, *self.fields])


def find_skill_entries(skills_dir: Path) -> set[str]:
    """The names of the directories under ``skills_dir`` that carry a ``SKILL.md``."""
    if not skills_dir.is_dir():
        return set()
    return {
        entry.name
        for entry in skills_dir.iterdir()
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    }


def _emit_env_records(
    plan: list[PlanRecord], switches_rows: list, secrets_rows: list
) -> None:
    """ENV-SWITCH and ENV-SECRET-CHECK records, each set sorted by name for determinism."""
    for row in sorted(switches_rows, key=lambda r: r.get("name", "")):
        name = row.get("name", "")
        value = row.get("value", "")
        targets = row.get("targets", [])
        if not name:
            plan.append(PlanRecord("WARN", ("env.switches row missing name field",)))
            continue
        if not targets:
            plan.append(
                PlanRecord("WARN", (f"env.switches row for {name} has no targets",))
            )
            continue
        invalid = [t for t in targets if t not in VALID_TARGETS]
        if invalid:
            plan.append(
                PlanRecord(
                    "WARN",
                    (
                        f"env.switches row for {name} has invalid target(s): {', '.join(invalid)}",
                    ),
                )
            )
            continue
        plan.append(PlanRecord("ENV-SWITCH", (name, value, ",".join(sorted(targets)))))

    for row in sorted(secrets_rows, key=lambda r: r.get("name", "")):
        name = row.get("name", "")
        if not name:
            plan.append(PlanRecord("WARN", ("env.secrets row missing name field",)))
            continue
        plan.append(PlanRecord("ENV-SECRET-CHECK", (name,)))


def _degraded_plan(reason: str) -> list[PlanRecord]:
    """Whole-tree plus one WARN: the plan an unusable registry degrades to.

    MODE stays at index 0, because the mode is read off the first record.
    """
    return [PlanRecord("MODE", ("whole-tree",)), PlanRecord("WARN", (reason,))]


def _load_registry(registry_path: Path) -> tuple[dict | None, list[PlanRecord]]:
    """Read and version-check the registry, or hand back the plan to emit instead.

    A ``None`` registry always arrives with a non-empty degraded plan, so the caller branches
    once rather than re-testing each way the registry can be unusable.
    """
    if not registry_path.is_file():
        return None, _degraded_plan(f"registry file not found: {registry_path}")
    try:
        with open(registry_path, "rb") as f:
            registry = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return None, _degraded_plan(f"registry unparseable: {e}")
    if registry.get("version") != 1:
        return None, _degraded_plan(
            f"registry version != 1 (got {registry.get('version')})"
        )
    return registry, []


def _collect_repo_local(
    repo_local_rows: list, projects_dir: Path, plan: list[PlanRecord]
) -> tuple[set[str], set[str]]:
    """Validate repo-local rows, appending one WARN per unusable row.

    Returns (skills to withhold from the global set, repo paths to emit as REPO-LOCAL). A row
    naming a missing repo or a missing skill leaves its skill in the global set, so a broken row
    costs availability nothing.
    """
    excluded_skills: set[str] = set()
    repo_local_repos: set[str] = set()
    for row in repo_local_rows:
        repo_name = row.get("repo", "")
        skill_name = row.get("skill", "")
        if not repo_name or not skill_name:
            plan.append(
                PlanRecord("WARN", ("repo-local row missing repo or skill field",))
            )
            continue
        repo_path = projects_dir / repo_name
        if not repo_path.is_dir():
            plan.append(
                PlanRecord(
                    "WARN",
                    (f"repo-local: repo {repo_name} not found under {projects_dir}",),
                )
            )
            continue
        if not (repo_path / "skills" / skill_name / "SKILL.md").is_file():
            plan.append(
                PlanRecord(
                    "WARN",
                    (
                        f"repo-local: skill {skill_name} not found in {repo_name}/skills/",
                    ),
                )
            )
            continue
        excluded_skills.add(skill_name)
        repo_local_repos.add(str(repo_path))
    return excluded_skills, repo_local_repos


def _collect_external_repos(
    external_repos_rows: list,
    projects_dir: Path,
    links_to_emit: dict[str, Path],
    plan: list[PlanRecord],
) -> bool:
    """Fold external-repo skills into ``links_to_emit``; report whether any row was SATISFIED.

    An external skill overrides a harness skill of the same name, and that precedence is the dict
    write itself. Rows that all failed leave the result False, and a farm is then not worth
    building.
    """
    external_satisfied = False
    for row in external_repos_rows:
        repo_name = row.get("repo", "")
        skills_subdir = row.get("path", "skills")
        if not repo_name:
            plan.append(PlanRecord("WARN", ("external-repos row missing repo field",)))
            continue
        repo_path = projects_dir / repo_name
        if not repo_path.is_dir():
            plan.append(
                PlanRecord(
                    "WARN",
                    (
                        f"external-repos: repo {repo_name} not found under {projects_dir}",
                    ),
                )
            )
            continue
        repo_skills_dir = repo_path / skills_subdir
        external_skills = find_skill_entries(repo_skills_dir)
        if not external_skills:
            plan.append(
                PlanRecord(
                    "WARN",
                    (
                        f"external-repos: repo {repo_name} has no skills in {skills_subdir}/",
                    ),
                )
            )
            continue
        external_satisfied = True
        for skill_name in external_skills:
            links_to_emit[skill_name] = repo_skills_dir / skill_name
    return external_satisfied


def _name_list(cfg: Mapping, key: str, where: str, plan: list[PlanRecord]) -> set[str]:
    """The string entries of ``cfg[key]``; any other shape is a WARN and contributes nothing."""
    value = cfg.get(key, [])
    if not isinstance(value, list):
        plan.append(PlanRecord("WARN", (f"{where} is not a list; ignored",)))
        return set()
    names = set()
    for entry in value:
        if isinstance(entry, str) and entry:
            names.add(entry)
        else:
            plan.append(
                PlanRecord(
                    "WARN", (f"{where} entry {entry!r} is not a skill name; ignored",)
                )
            )
    return names


def _effective_disabled(
    registry: Mapping, profile_cfg: Mapping, profile: str, harness_skills: set[str]
) -> tuple[set[str], list[PlanRecord]]:
    """The skills this profile must not see, with one WARN per name that is not a skill.

    The top-level ``disabled`` list applies to every profile, listed or not; a profile's
    ``enable`` list re-enables a disabled skill on that profile alone (ISSUE-REF).
    """
    warnings: list[PlanRecord] = []
    disabled = _name_list(registry, "disabled", "disabled", warnings)
    enabled = _name_list(profile_cfg, "enable", f"profiles.{profile}.enable", warnings)
    for where, names in (
        ("disabled", disabled),
        (f"profiles.{profile}.enable", enabled),
    ):
        for name in sorted(names - harness_skills):
            warnings.append(
                PlanRecord(
                    "WARN", (f"{where}: {name} is not a skill directory; ignored",)
                )
            )
    return (disabled - enabled) & harness_skills, warnings


def skill_enabled(registry_path: Path, profile: str, skill: str) -> bool:
    """Whether ``skill`` is on for ``profile``: not in ``disabled``, or re-enabled by the profile.

    The one answer for a skill whose switch gates more than its link (the jira toolkit's context
    module, hooks, links and MCP registration; the registry design's item 6). A registry that
    cannot be read answers False, so a broken file never turns those surfaces on.
    """
    registry, _degraded = _load_registry(registry_path)
    if registry is None:
        return False
    profiles = registry.get("profiles")
    profile_cfg = profiles.get(profile) if isinstance(profiles, Mapping) else None
    if not isinstance(profile_cfg, Mapping):
        profile_cfg = {}
    ignored: list[PlanRecord] = []
    disabled = _name_list(registry, "disabled", "disabled", ignored)
    enabled = _name_list(profile_cfg, "enable", f"profiles.{profile}.enable", ignored)
    return skill not in disabled or skill in enabled


def generate_plan(
    profile: str,
    harness_skills_dir: Path,
    projects_dir: Path,
    registry_path: Path,
) -> list[PlanRecord]:
    """The install plan for ``profile``; the MODE record is always first.

    Reads the registry and the filesystem and mutates nothing.
    """
    registry, degraded = _load_registry(registry_path)
    if registry is None:
        return degraded

    profiles = registry.get("profiles", {})
    profile_cfg = profiles.get(profile)
    harness_skills = find_skill_entries(harness_skills_dir)
    disabled, disabled_warnings = _effective_disabled(
        registry, profile_cfg or {}, profile, harness_skills
    )
    if profile_cfg is None and not disabled:
        # An unlisted profile with nothing disabled gets whole-tree: structural parity.
        return [PlanRecord("MODE", ("whole-tree",)), *disabled_warnings]

    profile_cfg = profile_cfg or {}
    repo_local_rows = profile_cfg.get("repo-local", [])
    external_repos_rows = profile_cfg.get("external-repos", [])
    env_cfg = profile_cfg.get("env", {})
    switches_rows = env_cfg.get("switches", [])
    secrets_rows = env_cfg.get("secrets", [])

    # Env rows never affect the mode.
    if not repo_local_rows and not external_repos_rows and not disabled:
        plan = [PlanRecord("MODE", ("whole-tree",)), *disabled_warnings]
        _emit_env_records(plan, switches_rows, secrets_rows)
        return plan

    # Provisionally farm. Whether any row is SATISFIABLE is known only after the loops below
    # have looked at the filesystem, and the WARNs they emit keep their order, so record 0 is
    # rewritten once, at the mode gate (ISSUE-REF).
    plan = [PlanRecord("MODE", ("farm",)), *disabled_warnings]
    excluded_skills, repo_local_repos = _collect_repo_local(
        repo_local_rows, projects_dir, plan
    )
    links_to_emit: dict[str, Path] = {
        skill_name: harness_skills_dir / skill_name
        for skill_name in sorted(harness_skills)
        if skill_name not in excluded_skills
    }
    # `_shared` is always linked in farm mode, though it carries no SKILL.md.
    links_to_emit["_shared"] = harness_skills_dir / "_shared"
    external_satisfied = _collect_external_repos(
        external_repos_rows, projects_dir, links_to_emit, plan
    )

    # THE MODE GATE (ISSUE-REF). A farm costs one link per skill per mount and demotes the cheap
    # whole-tree link, so it is built only when a row is actually SATISFIED. Whole-tree is then
    # strictly more available, and the WARNs above still explain each row. A disabled skill
    # always needs the farm: whole-tree cannot hide one skill (ISSUE-REF).
    if not excluded_skills and not external_satisfied and not disabled:
        plan[0] = PlanRecord("MODE", ("whole-tree",))
        _emit_env_records(plan, switches_rows, secrets_rows)
        return plan

    for skill_name in sorted(links_to_emit):
        if skill_name in disabled:
            continue
        plan.append(PlanRecord("LINK", (skill_name, str(links_to_emit[skill_name]))))
    for skill_name in sorted(disabled):
        plan.append(PlanRecord("DISABLED", (skill_name,)))
    for repo_path in sorted(repo_local_repos):
        plan.append(PlanRecord("REPO-LOCAL", (repo_path,)))
    _emit_env_records(plan, switches_rows, secrets_rows)
    return plan


def resolved_plan(
    profile: str, harness_skills_dir: Path, projects_dir: Path, registry_path: Path
) -> list[PlanRecord]:
    """The plan the installer acts on: never an exception, and always a known mode.

    install.sh's plan generator could not abort the install (its ``|| true``), so any failure to
    read the registry is a ``[warn]`` and the whole-tree plan. An unusable mode is the same
    fallback, so a truncated plan never installs nothing.
    """
    try:
        plan = generate_plan(
            profile, harness_skills_dir.resolve(), projects_dir.resolve(), registry_path
        )
    except Exception as exc:  # noqa: BLE001 - any reader failure degrades to whole-tree, never aborts
        print(
            f"  [warn] skill registry reader failed ({exc}) — falling back to whole-tree",
            file=sys.stderr,
        )
        return [PlanRecord("MODE", ("whole-tree",))]
    mode = plan[0].fields[0] if plan and plan[0].record_type == "MODE" else ""
    if mode not in _MODES:
        print(
            f"  [warn] unusable skill plan mode '{mode or 'empty'}' — defaulting to whole-tree",
            file=sys.stderr,
        )
        return [PlanRecord("MODE", ("whole-tree",))]
    return plan


def _records(plan: Iterable[PlanRecord], record_type: str) -> list[tuple[str, ...]]:
    return [record.fields for record in plan if record.record_type == record_type]


def _guard_no_self_clobber(link: Path) -> None:
    """install.sh's ISSUE-REF guard: never make a link whose path lands on a tracked file.

    The LINK path's parent is resolved physically, so a link reached through the whole-tree
    skills symlink is seen where it really is. Replacing an existing symlink is normal
    re-linking and never refused. The checks are two read-only git calls.
    """
    try:
        parent = Path(os.path.realpath(link.parent, strict=True))
    except OSError:
        return
    physical = parent / link.name
    if physical.is_symlink():
        return
    toplevel = _git_run(
        parent, "rev-parse", "--show-toplevel", timeout=_GUARD_GIT_TIMEOUT_S
    )
    if toplevel is None or toplevel.returncode != 0:
        return
    repo = toplevel.stdout.strip()
    relative = os.path.relpath(physical, repo)
    tracked = _git_run(
        Path(repo),
        "ls-files",
        "--error-unmatch",
        "--",
        relative,
        timeout=_GUARD_GIT_TIMEOUT_S,
    )
    if tracked is not None and tracked.returncode == 0:
        raise LinkError(
            f"refusing to symlink '{link}' — it resolves to tracked file '{physical}' in repo "
            f"'{repo}'; creating the symlink there would clobber real source (ISSUE-REF "
            "self-clobber guard)"
        )


def _readlink(path: Path) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def _replace_with_link(source: str, link: Path) -> None:
    """``ln -sfn``: a stale link or a regular file at ``link`` is replaced."""
    if link.is_symlink() or link.is_file():
        link.unlink()
    link.symlink_to(source)


def _remove_link(link: Path) -> None:
    """Remove the symlink or junction at ``link``, never what it points at."""
    try:
        link.unlink()
    except (IsADirectoryError, PermissionError):
        os.rmdir(link)  # a Windows directory junction


def _resolves_inside(link: Path, root: Path) -> bool:
    target = os.path.realpath(os.path.join(link.parent, os.readlink(link)))
    root_real = os.path.realpath(root)
    return target == root_real or target.startswith(root_real + os.sep)


def _is_our_farm(mount: Path, harness_skills: Path) -> bool:
    """Whether ``mount`` is a real directory holding only links this installer could have made.

    Provenance must be positive: every entry is a link, every live one resolves into
    ``harness_skills``, and ``_shared`` is a live link to the harness ``_shared``, which every
    farm this installer builds carries. Anything else makes the directory the user's.
    """
    if _readlink(mount) is not None or not mount.is_dir():
        return False
    for entry in mount.iterdir():
        if _readlink(entry) is None:
            return False
        if os.path.exists(entry) and not _resolves_inside(entry, harness_skills):
            return False
    shared = mount / "_shared"
    return (
        _readlink(shared) is not None
        and os.path.exists(shared)
        and os.path.realpath(shared) == os.path.realpath(harness_skills / "_shared")
    )


def _drop_dangling_mount(mount: Path) -> None:
    """A mount that is itself a dangling link owns nothing, so it is removed before installing."""
    if _readlink(mount) is not None and not os.path.exists(mount):
        _remove_link(mount)
        print(f"  [- ]  {mount} removed (dangling)")


def _dismantle_farm(mount: Path) -> None:
    for entry in mount.iterdir():
        _remove_link(entry)
    mount.rmdir()


def prune_farm(mount: Path, keep: set[str], harness_skills: Path) -> None:
    """Remove the links in a farm ``mount`` that the plan does not name, and every dangling one.

    A skill disabled after an earlier install disappears this way (ISSUE-REF). Only links are
    ever removed: a real file or directory is left alone, and a live link that resolves outside
    ``harness_skills`` is someone else's, so it is kept with a ``[warn]``.
    """
    if _readlink(mount) is not None or not mount.is_dir():
        return
    for entry in sorted(mount.iterdir()):
        if _readlink(entry) is None:
            continue
        dangling = not os.path.exists(entry)
        if entry.name in keep and not dangling:
            continue
        if not dangling and not _resolves_inside(entry, harness_skills):
            print(
                f"  [warn] {entry} links outside {harness_skills} and is not in the skill "
                "plan — leaving intact",
                file=sys.stderr,
            )
            continue
        _remove_link(entry)
        reason = "dangling" if dangling else "not in the skill plan"
        print(f"  [- ]  {entry} removed ({reason})")


def link_whole_tree(source: Path, mount: Path) -> None:
    """install.sh's ``link_skills_dir``: ``mount`` becomes one link to ``source``.

    An already-correct link is left ENTIRELY alone (ISSUE-REF): this runs on every merge, under
    live sessions, and a relink would change its inode and leave a window with no mount at all.
    A real directory a retired per-skill install left is replaced.
    """
    _guard_no_self_clobber(mount)
    if _readlink(mount) == str(source):
        return
    if mount.is_symlink():
        mount.unlink()
    elif mount.is_dir():
        shutil.rmtree(mount)
    mount.parent.mkdir(parents=True, exist_ok=True)
    _replace_with_link(str(source), mount)


def link_pi_whole_tree(source: Path, mount: Path) -> None:
    """install.sh's ``link_pi_skills_dir``: link ``~/.agents/skills`` only where nothing is.

    A real directory or a foreign link there is the user's, so it is warned about and kept. A
    directory holding only this installer's farm links is not the user's: it is dismantled and
    replaced, so a skill re-enabled after a farm install reappears here too.
    """
    _guard_no_self_clobber(mount)
    if _readlink(mount) == str(source):
        return
    _drop_dangling_mount(mount)
    if _is_our_farm(mount, source):
        _dismantle_farm(mount)
    if mount.exists() or mount.is_symlink():
        print(
            "  [warn] ~/.agents/skills exists and does not point to ai-harness/skills — "
            "leaving intact",
            file=sys.stderr,
        )
        return
    mount.parent.mkdir(parents=True, exist_ok=True)
    mount.symlink_to(source)


def build_farm(
    mount: Path, links: list[tuple[str, ...]], harness_skills: Path, *, is_pi: bool
) -> None:
    """install.sh's ``_build_skill_farm``: ``mount`` as a real directory of per-skill links.

    Only OUR OWN whole-tree link at ``mount`` is demoted to a directory; a link anywhere else is
    the user's, so the mount is declined with a ``[warn]`` rather than farmed through. A real
    directory at a skill's path is a legacy install on the Claude mount (replaced) and the user's
    on the Pi mount (kept). A mount that is a dangling link is removed and farmed.
    """
    _drop_dangling_mount(mount)
    if mount.is_symlink():
        current = _readlink(mount)
        if current == str(harness_skills):
            mount.unlink()
        else:
            print(
                f"  [warn] {mount} is a symlink to {current}, not {harness_skills} — "
                "declining to build a farm through it",
                file=sys.stderr,
            )
            return
    mount.mkdir(parents=True, exist_ok=True)
    for skill_name, source in links:
        link = mount / skill_name
        _guard_no_self_clobber(link)
        if _readlink(link) == source:
            continue
        if link.is_symlink():
            link.unlink()
        elif link.is_dir():
            if is_pi:
                print(
                    f"  [warn] {link} is a user-owned directory — leaving intact",
                    file=sys.stderr,
                )
                continue
            shutil.rmtree(link)
        _replace_with_link(source, link)
    prune_farm(mount, {skill_name for skill_name, _source in links}, harness_skills)


def build_farm_windows(
    mount: Path, links: list[tuple[str, ...]], harness_skills: Path
) -> None:
    """install.sh's ``_build_skill_farm_win``: queue one junction per skill.

    Our own whole-tree junction at ``mount`` is demoted to a directory first, since a farm
    queued through it would land inside the checkout. The mount directory is made here; each
    junction joins the run's one batched ``cmd.exe`` launch, and a junction already in place
    queues nothing. Junctions the plan no longer names are pruned at once. A mount that is a
    dangling junction is removed first; a live link anywhere else is the user's, so the mount is
    declined with a ``[warn]`` and nothing is queued or pruned through it.
    """
    _drop_dangling_mount(mount)
    if _points_at(mount, harness_skills):
        _remove_link(mount)
    elif _readlink(mount) is not None:
        print(
            f"  [warn] {mount} is a link to {_readlink(mount)}, not {harness_skills} — "
            "declining to build a farm through it",
            file=sys.stderr,
        )
        return
    if not mount.exists() and not mount.is_symlink():
        mount.mkdir(parents=True)
    for skill_name, source in links:
        queue_windows_junction(Path(source), mount / skill_name)
    prune_farm(mount, {skill_name for skill_name, _source in links}, harness_skills)


def apply_repo_local_links(repos: list[tuple[str, ...]]) -> None:
    """install.sh's ``_apply_repo_local_links``: ``<repo>/.claude/skills`` links ``<repo>/skills``.

    A real directory there is refused with a ``[warn]``, never replaced.
    """
    for (repo,) in repos:
        source = Path(repo) / "skills"
        link = Path(repo) / ".claude" / "skills"
        _guard_no_self_clobber(link)
        if _readlink(link) == str(source):
            continue
        if link.is_symlink():
            link.unlink()
        elif link.is_dir():
            print(
                f"  [warn] {link} is a real directory — refusing to replace",
                file=sys.stderr,
            )
            continue
        link.parent.mkdir(parents=True, exist_ok=True)
        _replace_with_link(str(source), link)
        print(f"  [ok]  {repo}/.claude/skills ⇒ {repo}/skills (repo-local)")


def write_settings_switch(settings_path: Path, name: str, value: str) -> None:
    """One ``claude-settings`` switch: ``env.<name>`` in ``settings.json``, read-modify-write.

    Every other key is kept and the file is left byte-identical when the value is already set.
    An absent, corrupt or non-object file still ends as valid JSON carrying the switch
    (the registry design's T-2.1 failure-path criterion); install.sh hands this step off only
    when its settings merge did not refuse the file.
    """
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    env = settings.setdefault("env", {})
    if env.get(name) == value:
        print(f"  [ok]  env.{name}={value} already set in settings.json")
        return
    env[name] = value
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(settings, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise LinkError(
            f"could not write env.{name} to {settings_path}: {exc}"
        ) from exc
    print(f"  [+ ]  env.{name}={value} (claude-settings)")


def apply_settings_switches(plan: list[PlanRecord], settings_path: Path) -> None:
    """Every ENV-SWITCH record's ``claude-settings`` target, in plan order."""
    for name, value, targets in _records(plan, "ENV-SWITCH"):
        if SETTINGS_TARGET in targets.split(","):
            write_settings_switch(settings_path, name, value)


def apply_profile_switches(
    plan: list[PlanRecord],
    home: Path,
    environ: Mapping[str, str],
    *,
    modify_shell_profile: bool,
) -> None:
    """Every ENV-SWITCH record's ``shell-profile`` and ``windows-user-env`` targets.

    Both write the user's shell startup or user environment, so both sit behind
    ``install.modify_shell_profile`` (D-15). The Windows user environment is written through the
    run's one batched ``cmd.exe`` launch, and only under a Windows shell.
    """
    for name, value, targets in _records(plan, "ENV-SWITCH"):
        for target in targets.split(","):
            if target == SETTINGS_TARGET:
                continue
            if target not in VALID_TARGETS:
                print(f"  [warn] unknown env switch target: {target}", file=sys.stderr)
                continue
            if not modify_shell_profile:
                print(
                    f"  [skip] env.{name} ({target}): install.modify_shell_profile is false"
                )
                continue
            if target == "shell-profile":
                persist_exported_var(name, value, home=home, environ=environ)
                print(f"  [ok]  export {name}={value} (shell-profile)")
            elif _is_windows_shell(environ):
                _persist_windows_user_env(name, value)
                print(f"  [ok]  {name}={value} queued for Windows user environment")
            else:
                print(
                    f"  [skip] {name} (windows-user-env): not a Windows shell, so there is "
                    "no Windows user environment to write"
                )


def check_secrets(plan: list[PlanRecord], environ: Mapping[str, str]) -> None:
    """Every ENV-SECRET-CHECK record: whether the variable is set, by its NAME only.

    The value is reduced to a boolean at once and never printed, logged or written.
    """
    for (name,) in _records(plan, "ENV-SECRET-CHECK"):
        if bool(environ.get(name)):
            print(f"  [ok]  {name} is set")
        else:
            print(
                f"  [warn] {name} is not set — provision it via your secret manager",
                file=sys.stderr,
            )


@dataclass(frozen=True)
class Layout:
    """Where one install reads from and writes to.

    ``checkout`` is the MAIN working tree every link points into (D-11), ``registry`` its
    ``skill-install-registry.toml``, ``profile`` the machine profile the registry is keyed by.
    """

    checkout: Path
    registry: Path
    profile: str
    projects_dir: Path
    home: Path

    @property
    def skills(self) -> Path:
        return self.checkout / "skills"

    def plan(self) -> list[PlanRecord]:
        return resolved_plan(
            self.profile, self.skills, self.projects_dir, self.registry
        )


def apply_settings_env(layout: Layout) -> None:
    """The ``claude-settings`` switches alone, for the hand-off inside the settings lock."""
    apply_settings_switches(layout.plan(), layout.home / ".claude" / "settings.json")


def _whole_tree_windows(layout: Layout) -> None:
    pi_mount = layout.home / ".agents" / "skills"
    # A farm an earlier install built is ours to take down; `rmdir` cannot remove a full one.
    for mount in (layout.home / ".claude" / "skills", pi_mount):
        if _is_our_farm(mount, layout.skills):
            _dismantle_farm(mount)
    queue_windows_junction(layout.skills, layout.home / ".claude" / "skills")
    print("  [ok]  ~/.claude/skills/ ⇒ ai-harness/skills/ (junction)")
    if _points_at(pi_mount, layout.skills):
        print(
            "  [ok]  ~/.agents/skills/ ⇒ ai-harness/skills/ (junction, already correct)"
        )
    elif pi_mount.exists():
        print(
            "  [warn] ~/.agents/skills exists and does not point to ai-harness/skills — "
            "leaving intact",
            file=sys.stderr,
        )
    else:
        queue_windows_junction(layout.skills, pi_mount)
        print("  [ok]  ~/.agents/skills/ ⇒ ai-harness/skills/ (junction)")


def _whole_tree_posix(layout: Layout) -> None:
    pi_mount = layout.home / ".agents" / "skills"
    link_whole_tree(layout.skills, layout.home / ".claude" / "skills")
    print("  [ok]  ~/.claude/skills ⇒ ai-harness/skills/ (whole-tree symlink)")
    link_pi_whole_tree(layout.skills, pi_mount)
    if _readlink(pi_mount) == str(layout.skills):
        print("  [ok]  ~/.agents/skills ⇒ ai-harness/skills/ (whole-tree symlink)")


def _farm(layout: Layout, plan: list[PlanRecord], *, windows: bool) -> None:
    claude_mount = layout.home / ".claude" / "skills"
    pi_mount = layout.home / ".agents" / "skills"
    links = _records(plan, "LINK")
    for (skill_name,) in _records(plan, "DISABLED"):
        print(
            f"  [ok]  {skill_name} is disabled by the skill registry, so it is not linked"
        )
    if windows:
        build_farm_windows(claude_mount, links, layout.skills)
        print("  [ok]  ~/.claude/skills/ farm (per-skill junctions)")
        build_farm_windows(pi_mount, links, layout.skills)
        print("  [ok]  ~/.agents/skills/ farm (per-skill junctions)")
        return
    build_farm(claude_mount, links, layout.skills, is_pi=False)
    print("  [ok]  ~/.claude/skills farm (per-skill symlinks)")
    build_farm(pi_mount, links, layout.skills, is_pi=True)
    print("  [ok]  ~/.agents/skills farm (per-skill symlinks)")
    # Repo-local links are POSIX only: Windows is unverified (the registry design's D-4).
    apply_repo_local_links(_records(plan, "REPO-LOCAL"))


def install(
    layout: Layout, environ: Mapping[str, str], *, modify_shell_profile: bool
) -> None:
    """Both skills mounts in the profile's mode, then its env switches and secret checks.

    A link that cannot be made raises. It requires no particular skill: which skills a machine
    must carry is the private installer's policy (IN-77), checked by install.sh after this step.
    """
    if not layout.skills.is_dir():
        print("  [skip] skills/ is not shipped; skills install skipped")
        return
    plan = layout.plan()
    for (warning,) in _records(plan, "WARN"):
        print(f"  [warn] {warning}", file=sys.stderr)
    windows = _is_windows_shell(environ)
    if plan[0].fields[0] == "farm":
        _farm(layout, plan, windows=windows)
    elif windows:
        _whole_tree_windows(layout)
    else:
        _whole_tree_posix(layout)
    apply_profile_switches(
        plan, layout.home, environ, modify_shell_profile=modify_shell_profile
    )
    check_secrets(plan, environ)
