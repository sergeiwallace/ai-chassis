"""Wire ``hooks-manifest.json`` into Claude Code's settings (ISSUE-REF batch C-B2, IN-33).

install.sh's retired Step 7b, ported from ``projects-wide-session-config/apply-hooks.py``, which
is now a thin wrapper over this module. The manifest is the single source of hook wiring and
machine scope. ``apply`` reconciles the managed-hook block of the user-global ``settings.json``
(prune every manifest-managed hook, then add this machine's in-profile user hooks) and regenerates
the project ``settings.local.json`` hooks block wholesale. Non-hook keys and any non-managed
(user-added) hooks are preserved, and a file whose rendered JSON is unchanged is not written.

User-target hooks are wired at ``<user_hooks_dir>/<script>`` (global, every session).
Project-target hooks are wired at ``$CLAUDE_PROJECT_DIR/.claude/hooks/<script>`` and land in the
checkout's gitignored ``.claude/settings.local.json``, so they apply only in that checkout's
sessions on in-profile machines.

A hook's optional ``capability`` is enabled when its provider (``defaults/capabilities.toml``,
else the capability id itself) is on PATH, the probe install.sh ran inline before this step moved.
The quality-steering receipt binding check is not here: it is private, and runs as the
orchestrator's ``hooks-steering-check`` step before this one.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path

from .skills_install import REGISTRY_NAME, skill_enabled

PROJECT_HOOKS_EXPR = "$CLAUDE_PROJECT_DIR/.claude/hooks"
CAPABILITIES_TOML = Path(__file__).resolve().parent / "defaults" / "capabilities.toml"
#: A hook capability ``skill:<name>`` is enabled where the skill install registry (read from
#: beside the manifest) enables ``<name>`` for the machine, rather than by a provider on PATH.
SKILL_CAPABILITY_PREFIX = "skill:"
# _managed_basenames()/_managed_mcp_tools() are derived from the CURRENT manifest
# only, so a hook entry deleted outright (not just moved out-of-profile) drops out
# of the "managed" set on the very next apply and its previously-installed copy is
# no longer recognized as prunable -- it survives in settings.json forever (ISSUE-REF,
# discovered when substrate-recall-telemetry.sh's entry was removed for ISSUE-REF and
# the stale hook persisted across a full reinstall). Retire a basename/tool pair
# here, once, at the same commit that deletes its manifest entry, so this one
# `apply` call still recognizes and prunes the orphaned copy. Safe to leave stale
# entries accumulating in these sets after that -- they only ever widen what gets
# pruned, never narrow it.
# ISSUE-REF (2026-09-11): the agent-ledger pair is the SECOND instance of the exact
# failure the comment above describes. PR #902 deleted both manifest entries and both
# scripts for ISSUE-REF without retiring the basenames here, so a full clean install.sh
# run left them wired in ~/.claude/settings.json pointing at paths that no longer exist.
# They were inert only because `_command_for()` emits an `if [ -f ... ]` guard. The
# converse-direction check that now catches this is
# `tests/hooks/test_manifest_matches_live_settings.py::test_no_wired_hook_points_at_a_script_that_does_not_exist`
# -- the whole file passed 7/7 with both stale entries present, because it only ever
# asserted manifest -> settings and never settings -> manifest.
_RETIRED_MANAGED_SCRIPTS = frozenset(
    {
        "substrate-recall-telemetry.sh",
        "agent-ledger-session.sh",
        "agent-ledger-subagent.sh",
        "launcher-auth-renew.sh",
        # ISSUE-REF retired the ISSUE-REF pair for launch-resume-session-start/-end.
        "reboot-resume-session-start.sh",
        "reboot-resume-session-end.sh",
        # The ISSUE-REF GrowthBook restore hook was removed with the rc-pair workaround (#818)
        # without being retired here, so a Mac that had it installed kept the entry in
        # ~/.claude/settings.json pointing at a script that no longer exists.
        "rc-growthbook-restore.sh",
        "quota-routing-notice.py",
    }
)
_RETIRED_MANAGED_MCP_TOOLS = frozenset({("ai-core", "_hook_adapter_internal")})


class InvalidJsonError(ValueError):
    """A manifest or settings file that is not valid JSON; nothing is overwritten."""

    def __init__(self, path) -> None:
        super().__init__(f"{path} is not valid JSON; refusing to overwrite.")


class HooksManifestError(ValueError):
    """The step's refusal: install.sh stops at the hand-off, as `set -e` did."""


def _load(path):
    """The JSON at ``path``; ``{}`` when it is missing, and a refusal when it does not parse."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        # Never clobber an unreadable settings file silently — fail loudly.
        raise InvalidJsonError(path) from None


def _in_profile(hook, machine):
    m = hook.get("machines", "all")
    return m == "all" or (isinstance(m, list) and machine in m)


def _load_capability_states(path):
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _capability_enabled(hook, capability_states):
    """Return whether a hook's optional capability predicate is enabled."""
    capability = hook.get("capability")
    if capability is None:
        return True
    record = capability_states.get(capability)
    return isinstance(record, dict) and record.get("state") == "enabled"


def _declared_capabilities(manifest):
    return {
        hook["capability"]
        for hook in manifest.get("hooks", [])
        if isinstance(hook, dict) and isinstance(hook.get("capability"), str)
    }


def skill_capability_states(manifest, skill_registry: Path | None, profile: str):
    """``{"skill:<name>": {"state": ...}}`` for each manifest capability that names a skill.

    Such a hook is wired only where the skill install registry enables that skill for
    ``profile``, so the registry's ``enable`` list is the one switch for the skill and its
    hooks. No registry, or one that cannot be read, enables none.
    """
    states = {}
    for capability in sorted(_declared_capabilities(manifest)):
        if not capability.startswith(SKILL_CAPABILITY_PREFIX):
            continue
        skill = capability[len(SKILL_CAPABILITY_PREFIX) :]
        on = skill_registry is not None and skill_enabled(
            skill_registry, profile, skill
        )
        states[capability] = {"state": "enabled" if on else "disabled"}
    return states


def probe_capability_states(
    manifest,
    registry_path: Path = CAPABILITIES_TOML,
    which: Callable[[str], str | None] = shutil.which,
):
    """``{id: {"state": "enabled"|"disabled"}}`` for each capability a manifest hook declares.

    A capability is enabled when its registry provider, else the id itself, is on PATH. An
    unreadable registry falls back to the id for every capability, as install.sh's probe did.
    A ``skill:<name>`` capability is the skill registry's to answer (``skill_capability_states``)
    and is not probed here.
    """
    capability_ids = sorted(
        capability
        for capability in _declared_capabilities(manifest)
        if not capability.startswith(SKILL_CAPABILITY_PREFIX)
    )
    providers = dict.fromkeys(capability_ids)
    try:
        with open(registry_path, "rb") as f:
            registry = tomllib.load(f)
        for cap_id in capability_ids:
            entry = registry.get("capabilities", {}).get(cap_id)
            if entry and isinstance(entry.get("provider"), str):
                providers[cap_id] = entry["provider"]
    except Exception:  # noqa: BLE001, S110 - the shell probe's own fallback: the id is the binary
        pass
    return {
        cap_id: {
            "state": "enabled" if which(providers[cap_id] or cap_id) else "disabled"
        }
        for cap_id in capability_ids
    }


def _command_for(hook, user_hooks_dir):
    if "inline" in hook:
        return hook["inline"]
    script = hook["script"]
    base = user_hooks_dir if hook["target"] == "user" else PROJECT_HOOKS_EXPR
    # Guard on script existence so a missing/removed/not-installed-on-this-machine
    # hook script degrades to a silent no-op instead of hard-erroring on every event
    # ("No such file or directory"). Defense-in-depth: even if a machine-scoped hook
    # leaks past the manifest gating, or a script is pruned but a stale settings copy
    # lingers, the command can't break the session.
    path = f"{base}/{script}"
    event = hook.get("env")
    command = f'bash "{path}"'
    arguments = hook.get("arguments", [])
    if arguments:
        command += " " + " ".join(f'"{value}"' for value in arguments)
    if isinstance(event, str) and event:
        command = f'BACKGROUND_WAIT_EVENT="{event}" {command}'
    return f'if [ -f "{path}" ]; then {command}; fi'


def _build_group(hook, command=None, *, user_hooks_dir):
    hook_type = hook.get("type", "command")
    entry = {"type": hook_type}
    if hook_type == "command":
        # Exec form for every scripted hook, not just those opting in. The shell-string
        # alternative from _command_for() costs TWO processes per hook firing -- Claude
        # Code starts a shell to interpret `if [ -f "P" ]; then bash "P"; fi`, and that
        # shell then spawns bash -- whereas exec form starts bash once and _exec-guard.sh
        # execs the hook into that same process. It also carries the identical existence
        # guard, so the no-op-on-missing-script behaviour is preserved exactly.
        #
        # This is a per-firing saving on every OS. It is merely unmissable on a host whose
        # process creation is monitored: example-client-host-windows measures ~2s per spawn, so the second
        # process was ~2s added to every hook on every event.
        #
        # Restricted to target == "user" ON PURPOSE. A project-target hook's base is
        # PROJECT_HOOKS_EXPR, the shell expression "$CLAUDE_PROJECT_DIR/.claude/hooks";
        # exec form has no shell, so that would be passed through literally and never
        # expand, leaving the guard unable to find the script and silently no-opping
        # every project hook. Those keep the shell-string form, which is what expands it.
        if hook.get("target") == "user" and "script" in hook:
            # Taken as a parameter, exactly as _command_for() takes it. It used to be
            # smuggled in under a synthetic `_user_hooks_dir` key on the hook dict, which
            # made the requirement invisible at the call site: six tests that build a
            # group from a real manifest entry raised `KeyError: '_user_hooks_dir'`
            # instead of failing to satisfy a signature.
            base = user_hooks_dir
            event = hook.get("env")
            entry["command"] = "bash"
            entry["args"] = [
                f"{base}/_exec-guard.sh",
                *(["--setenv", f"BACKGROUND_WAIT_EVENT={event}"] if event else []),
                f"{base}/{hook['script']}",
                *hook.get("arguments", []),
            ]
        else:
            entry["command"] = command
    elif hook_type == "mcp_tool":
        for field in ("server", "tool", "input"):
            entry[field] = hook[field]
    if "timeout" in hook:
        entry["timeout"] = hook["timeout"]
    for field in ("async", "asyncRewake", "rewakeMessage", "rewakeSummary"):
        if field in hook:
            entry[field] = hook[field]
    return {"matcher": hook.get("matcher", ""), "hooks": [entry]}


def _managed_basenames(manifest):
    return _RETIRED_MANAGED_SCRIPTS | {
        h["script"] for h in manifest["hooks"] if "script" in h
    }


def _managed_mcp_tools(manifest):
    return _RETIRED_MANAGED_MCP_TOOLS | {
        (h["server"], h["tool"])
        for h in manifest["hooks"]
        if h.get("type") == "mcp_tool"
    }


def _validate_manifest(manifest):  # noqa: C901, PLR0912 - pre-existing (ISSUE-REF), unrelated to ISSUE-REF
    hooks = manifest.get("hooks")
    if not isinstance(hooks, list):
        raise TypeError("hooks manifest must contain a hooks list")
    for hook in hooks:
        if not isinstance(hook, dict):
            raise TypeError("every hooks manifest entry must be an object")
        hook_type = hook.get("type", "command")
        if hook_type == "command":
            if "inline" not in hook and "script" not in hook:
                raise ValueError("command hooks require script or inline")
        elif hook_type == "mcp_tool":
            for field in ("server", "tool"):
                if not isinstance(hook.get(field), str) or not hook[field]:
                    raise ValueError(f"mcp_tool hooks require non-empty {field}")
            if not isinstance(hook.get("input"), dict):
                raise ValueError("mcp_tool hooks require an input mapping")
        else:
            raise ValueError(f"unsupported hook type: {hook_type}")
        if "capability" in hook and (
            not isinstance(hook["capability"], str) or not hook["capability"]
        ):
            raise ValueError("hook capability must be a non-empty string")
        if "timeout" in hook and (
            not isinstance(hook["timeout"], (int, float)) or hook["timeout"] <= 0
        ):
            raise ValueError("hook timeout must be a positive number")
        if "asyncRewake" in hook and not isinstance(hook["asyncRewake"], bool):
            raise ValueError("asyncRewake must be a boolean")
        if "async" in hook and not isinstance(hook["async"], bool):
            raise ValueError("async must be a boolean")
        for field in ("rewakeMessage", "rewakeSummary"):
            if field in hook and (not isinstance(hook[field], str) or not hook[field]):
                raise ValueError(f"{field} must be a non-empty string")
        if "arguments" in hook and (
            not isinstance(hook["arguments"], list)
            or not all(isinstance(value, str) and value for value in hook["arguments"])
        ):
            raise ValueError("arguments must be a list of non-empty strings")
        if "exec_guard" in hook and not isinstance(hook["exec_guard"], bool):
            raise ValueError("exec_guard must be a boolean")


def hook_entry_script_basenames(hook_entry):
    """Every script basename a single settings hook entry references.

    THE ONE derivation of "which script does this entry run", shared by the prune
    below and by the live-settings audit in
    ``tests/hooks/test_manifest_matches_live_settings.py``. That audit used to
    re-implement it and was wrong twice in ways that both read as "the hook is
    inert": it matched only ``.sh`` (so a wired ``.py`` hook was invisible) and it
    scanned only ``command`` (so an ``exec_guard`` entry, whose script path lives
    in ``args``, was invisible too). Both false alarms named real hooks as never
    running and sent the reader to re-run install.sh, which could not help.

    Scan EVERY whitespace token, not just the last. Two things would otherwise
    leak a duplicate hook past the prune:

    - a trailing argument (``bash .../x.sh --flag``) defeats a "last token"
      check (F-01); and
    - ``_command_for`` wraps the path in a shell existence guard
      (``if [ -f "PATH" ]; then bash "PATH"; fi``), so tokens arrive decorated
      with quotes/brackets/semicolons (``"…/x.sh"];``, ``"…/x.sh";``). Strip that
      shell punctuation before the basename compare, or the prune misses
      ``_command_for``'s OWN format and every install appends a duplicate (F-1).
      ``test_apply_hooks_prune.py`` guards the round-trip.
    """
    tokens = [
        *hook_entry.get("command", "").replace("\\", "/").split(),
        *hook_entry.get("args", []),
    ]
    # Deliberately unfiltered: every token's basename, including shell noise like
    # `bash` or `then`. Callers match against a known-managed set, so noise cannot
    # produce a false positive, whereas a filter (e.g. "must contain a dot") would
    # silently stop recognising a managed script that has no extension.
    return {str(raw).strip("\"';,()[]{}").rsplit("/", 1)[-1] for raw in tokens}


def _group_is_managed(group, managed, managed_mcp_tools=None):
    managed_mcp_tools = (
        _RETIRED_MANAGED_MCP_TOOLS if managed_mcp_tools is None else managed_mcp_tools
    )
    for h in group.get("hooks", []):
        if (
            h.get("type") == "mcp_tool"
            and (h.get("server"), h.get("tool")) in managed_mcp_tools
        ):
            return True
        if hook_entry_script_basenames(h) & set(managed):
            return True
    return False


def _apply_hook_block(  # noqa: PLR0913, PLR0917 - pre-existing (ISSUE-REF), unrelated to ISSUE-REF
    existing_hooks,
    manifest,
    machine,
    target,
    user_hooks_dir,
    managed,
    managed_mcp_tools=None,
    capability_states=None,
):
    """Return a reconciled hooks dict for the given target.

    For 'user': prune managed groups from the caller's existing block, then add
    in-profile user hooks (preserves non-managed groups + event ordering).
    For 'project': build fresh (caller passes an empty dict).
    """
    hooks = dict(existing_hooks)
    # Prune managed groups from every event.
    for event in list(hooks.keys()):
        arr = hooks[event]
        if not isinstance(arr, list):
            continue
        kept = [g for g in arr if not _group_is_managed(g, managed, managed_mcp_tools)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    # Add in-profile hooks for this target, in manifest order (deterministic).
    states = {} if capability_states is None else capability_states
    for hook in manifest["hooks"]:
        if (
            hook["target"] != target
            or not _in_profile(hook, machine)
            or not _capability_enabled(hook, states)
        ):
            continue
        command = (
            _command_for(hook, user_hooks_dir)
            if hook.get("type", "command") == "command"
            else None
        )
        group = _build_group(hook, command, user_hooks_dir=user_hooks_dir)
        hooks.setdefault(hook["event"], []).append(group)
    return hooks


# ISSUE-REF: Claude Code kills each SessionEnd hook at this env value, 1.5 s when it is unset; a
# hook's own `timeout` only sets the outer bound on the whole SessionEnd phase (read from the
# 2.1.300 binary: the per-hook `timeoutMs` is `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS ?? 1500`).
# So every SessionEnd budget the manifest declares was fictional until this key carried it. It is
# derived, never hand-set: the largest timeout of any SessionEnd hook wired on this machine, user or
# project, managed or the user's own, the same set Claude Code scans for its outer bound.
SESSION_END_TIMEOUT_ENV = "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS"


def _session_end_budget_ms(*hook_blocks):
    """The largest SessionEnd hook timeout across ``hook_blocks``, in ms, or None if none is set."""
    budget = 0
    for block in hook_blocks:
        for group in block.get("SessionEnd", []) if isinstance(block, dict) else []:
            for entry in group.get("hooks", []) if isinstance(group, dict) else []:
                timeout = entry.get("timeout") if isinstance(entry, dict) else None
                if isinstance(timeout, (int, float)) and timeout > 0:
                    budget = max(budget, int(timeout * 1000))
    return budget or None


def _apply_session_end_budget(settings, budget_ms):
    env = settings.get("env")
    if budget_ms is None:
        if isinstance(env, dict):
            env.pop(SESSION_END_TIMEOUT_ENV, None)
        return
    if not isinstance(env, dict):
        env = settings["env"] = {}
    env[SESSION_END_TIMEOUT_ENV] = str(budget_ms)


def _write(path, data):
    """Write the settings file only when the rendered JSON actually differs.

    ISSUE-REF (false-event class): ~/.claude/settings.json and .claude/settings.local.json
    are watched — Claude Code re-reads them on change. The post-merge install hook
    re-runs this reconciler on every merge, so an unconditional write fired a settings
    reload on every machine, every merge, even when the reconciled hook block was
    byte-identical. Idempotent means no-op, not merely convergent.
    """
    rendered = json.dumps(data, indent=2) + "\n"
    p = Path(path)
    try:
        if p.read_text(encoding="utf-8") == rendered:
            return
    except OSError:
        pass
    # Stage and rename (ISSUE-REF): an in-place write truncates first, so a session
    # reloading mid-write read an empty file. Same pattern as settings_merge.
    try:
        mode = stat.S_IMODE(p.stat().st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    descriptor, temporary_name = tempfile.mkstemp(prefix=f"{p.name}.", dir=p.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, p)
    finally:
        temporary.unlink(missing_ok=True)


def reconcile(  # noqa: PLR0913, PLR0917 - the six inputs apply-hooks.py's `apply` mode always took
    machine,
    user_settings,
    local_settings,
    user_hooks_dir,
    manifest,
    capability_states,
):
    """Write both settings files for ``machine`` from a validated ``manifest``; return the report.

    Both files are read before either is written, so an unreadable one leaves both untouched.
    """
    managed = _managed_basenames(manifest)
    managed_mcp_tools = _managed_mcp_tools(manifest)

    # --- user-global settings.json: reconcile (prune managed + add in-profile) ---
    settings = _load(user_settings)
    reconciled = _apply_hook_block(
        settings.get("hooks", {}),
        manifest,
        machine,
        "user",
        user_hooks_dir,
        managed,
        managed_mcp_tools,
        capability_states,
    )
    if reconciled:
        settings["hooks"] = reconciled
    elif "hooks" in settings:
        del settings["hooks"]

    # --- project settings.local.json: regenerate hooks wholesale ---
    local = _load(local_settings)
    project_hooks = _apply_hook_block(
        {},
        manifest,
        machine,
        "project",
        user_hooks_dir,
        managed,
        managed_mcp_tools,
        capability_states,
    )
    _apply_session_end_budget(
        settings, _session_end_budget_ms(reconciled, project_hooks)
    )
    _write(user_settings, settings)
    if project_hooks:
        local["hooks"] = project_hooks
    elif "hooks" in local:
        del local["hooks"]
    _write(local_settings, local)

    n_user = sum(
        1
        for h in manifest["hooks"]
        if h["target"] == "user"
        and _in_profile(h, machine)
        and _capability_enabled(h, capability_states)
    )
    n_proj = sum(
        1
        for h in manifest["hooks"]
        if h["target"] == "project"
        and _in_profile(h, machine)
        and _capability_enabled(h, capability_states)
    )
    return f"  [ok]  hooks applied (machine={machine}): {n_user} user, {n_proj} project"


def user_scripts(machine, manifest, capability_states):
    """The basenames of in-profile user-target hook scripts, in manifest order."""
    return [
        hook["script"]
        for hook in manifest["hooks"]
        if hook.get("target") == "user"
        and "script" in hook
        and _in_profile(hook, machine)
        and _capability_enabled(hook, capability_states)
    ]


def apply(  # noqa: PLR0913 - the step's five paths and its machine
    *,
    machine: str,
    manifest_path: Path,
    user_settings: Path,
    local_settings: Path,
    user_hooks_dir: str,
    registry_path: Path = CAPABILITIES_TOML,
) -> str:
    """The core step: probe the capabilities, then reconcile; return the line it reports.

    A missing manifest is the shell's ``[WARN]`` and writes nothing. A malformed manifest, an
    unreadable settings file or a failed write raises ``HooksManifestError`` naming it, with both
    files left as found.
    """
    if not manifest_path.is_file():
        return (
            f"  [WARN] hooks-manifest not applied ({manifest_path} does not exist)"
            " — hooks unwired."
        )
    try:
        manifest = _load(manifest_path)
        try:
            _validate_manifest(manifest)
        except (AttributeError, TypeError, ValueError) as exc:
            raise HooksManifestError(
                f"hooks manifest {manifest_path} is malformed: {exc}"
            ) from exc
        # install.sh made ~/.claude before any settings writer ran; the step stands alone now.
        for directory in (user_settings.parent, local_settings.parent):
            directory.mkdir(parents=True, exist_ok=True)
        states = probe_capability_states(manifest, registry_path)
        states.update(
            skill_capability_states(
                manifest, manifest_path.parent / REGISTRY_NAME, machine
            )
        )
        return reconcile(
            machine,
            user_settings,
            local_settings,
            user_hooks_dir,
            manifest,
            states,
        )
    except InvalidJsonError as exc:
        raise HooksManifestError(str(exc)) from exc
    except OSError as exc:
        raise HooksManifestError(f"hooks manifest not applied: {exc}") from exc
