"""Merge the harness settings template into a user's Claude settings file.

Four layers, weakest first: the fleet template, the machine fragment, the
user's own file, then the fleet-policy keys and the fragment's model pins
forced on top.  Nested objects are
merged key by key rather than replaced, which is what makes a per-machine
default expressible at all.

The merge was once shallow -- every template-owned top-level key replaced the
existing value wholesale.  That silently destroyed two things.  The template
then pinned a fleet default ``model``, so it overwrote whatever the user had chosen
with a bare tier alias, and a bare alias resolves to that tier's ordinary
context window: the session lands on a DEFAULT, not on a downgraded
preference, which is the signature that distinguishes this from a changed
setting.  And ``env`` is a nested object, so replacing it deleted every
machine-local variable inside it -- ``ANTHROPIC_MODEL``, which is the only
layer that outranks ``settings.json``'s ``model``, and the backend switch,
measured missing on a live host.  That also left the machine fragment unable to
express a durable model: the fragment can only supply ``env``, and the next
merge threw that object away.

``install.sh`` reached the same policy by a different route -- an
add-never-remove ``deep_merge`` plus an explicit list of forced keys -- and now
hands its template merge to this module (``ai-harness apply --only settings``).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import TextIO

# Fleet POLICY, not per-machine preference: the managed value must win even over a
# value the user already has, so every session behaves identically wherever it runs.
_POLICY_KEYS = ("tui",)
_POLICY_ENV_KEYS = (
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH",
    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS",
)

# Model pins are owned by the machine FRAGMENT, for the keys it names (ISSUE-REF). Left to
# "user wins", a pin installed once could never move, and a broken value written by another
# writer was permanent: measured on example-project-ec2, the live file held a policy-denied `global.`
# Haiku id while the fragment pinned the `us.` one. A key the fragment does not name is left
# alone, and the top-level `model` (what `/model` writes) is untouched. Mirrors install.sh
# Step 4b; keep the two lists in step.
_FRAGMENT_MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
)

# The command the original settings file shipped with its template token never resolved
# (ISSUE-REF). "User wins" preserved it forever on upgraded installs, invoking a script that
# does not exist; only this known-invalid literal is repaired, a custom statusline is not.
_LEGACY_STATUSLINE_COMMAND = "bash CLAUDE_DIR/statusline-command.sh"


class SettingsMergeError(RuntimeError):
    """The settings template could not be safely merged."""


def _read_json_object(path: Path | Traversable, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SettingsMergeError(f"{label} is not valid JSON: {path}") from exc
    except OSError as exc:
        raise SettingsMergeError(f"could not read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SettingsMergeError(f"{label} must contain a JSON object: {path}")
    return value


def _deep_merge(
    base: dict[str, object], override: dict[str, object]
) -> dict[str, object]:
    """Merge ``override`` onto ``base``, recursing into nested objects.

    ``override`` wins at every leaf.  Two dicts are merged key by key; anything
    else, a list included, is taken from ``override`` whole.
    """
    result = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = value
    return result


def _hook_script_basename(entry: object) -> str | None:
    """The script a hook entry runs, by basename, so per-machine paths still dedupe.

    The first token ending in ``.sh`` wins (so ``bash .../x.sh --flag`` keys on ``x.sh``),
    else the last token; install.sh's retired Step 4 used the same rule.
    """
    if not isinstance(entry, dict):
        return None
    hooks = entry.get("hooks", [])
    for hook in hooks if isinstance(hooks, list) else []:
        command = hook.get("command", "") if isinstance(hook, dict) else ""
        if not isinstance(command, str) or not command:
            continue
        tokens = command.replace("\\", "/").split()
        for token in tokens:
            base = token.rsplit("/", 1)[-1]
            if base.endswith(".sh"):
                return base
        return tokens[-1].rsplit("/", 1)[-1] if tokens else None
    return None


def _merge_hook_events(
    existing: dict[str, object], managed: dict[str, object], report: list[str]
) -> dict[str, object]:
    """Per-event hook merge: append each managed entry whose script the event lacks.

    A user's own entries survive; a managed hook added to the template reaches every
    existing install without duplicating one it already has.
    """
    merged = dict(existing)
    for event, managed_list in managed.items():
        current = merged.get(event)
        if isinstance(current, list) and isinstance(managed_list, list):
            present = {_hook_script_basename(entry) for entry in current}
            present.discard(None)
            appended = []
            for entry in managed_list:
                base = _hook_script_basename(entry)
                if base and base not in present:
                    current.append(entry)
                    present.add(base)
                    appended.append(base)
            if appended:
                report.append(f"  [+ ]  {event}: added {', '.join(appended)}")
        elif event not in merged:
            merged[event] = managed_list
            names = [
                base
                for base in map(
                    _hook_script_basename,
                    managed_list if isinstance(managed_list, list) else [],
                )
                if base
            ]
            if names:
                report.append(
                    f"  [+ ]  {event}: added {', '.join(names)} (new event type)"
                )
    return merged


def _fill_missing(
    existing: dict[str, object], managed: dict[str, object], report: list[str]
) -> dict[str, object]:
    """Add every managed key ``existing`` lacks, recursing into nested objects.

    ``existing`` wins at every leaf, a list included, except a ``hooks`` object, whose
    per-event lists gain the managed entries they lack (``_merge_hook_events``).  Its keys
    keep their order and missing ones follow in managed order, the order install.sh's own
    merge wrote, so a file the shell wrote reads as unchanged here.
    """
    result = dict(existing)
    for key, value in managed.items():
        current = result.get(key)
        if key == "hooks" and isinstance(current, dict) and isinstance(value, dict):
            result[key] = _merge_hook_events(current, value, report)
        elif key not in result:
            result[key] = value
        elif isinstance(current, dict) and isinstance(value, dict):
            result[key] = _fill_missing(current, value, report)
    return result


def _repair_legacy_statusline(
    merged: dict[str, object], template: dict[str, object]
) -> None:
    statusline = merged.get("statusLine")
    template_statusline = template.get("statusLine")
    if (
        isinstance(statusline, dict)
        and statusline.get("command") == _LEGACY_STATUSLINE_COMMAND
        and isinstance(template_statusline, dict)
        and isinstance(template_statusline.get("command"), str)
    ):
        statusline["command"] = template_statusline["command"]


def _scrub_env_keys(merged: dict[str, object], keys: Sequence[str]) -> None:
    env = merged.get("env")
    if isinstance(env, dict):
        for key in keys:
            env.pop(key, None)


def _force_policy_keys(
    merged: dict[str, object], managed: dict[str, object]
) -> dict[str, object]:
    """Restore the fleet-policy keys from the managed layers, over the user's file."""
    for key in _POLICY_KEYS:
        if key in managed:
            merged[key] = managed[key]
    managed_env = managed.get("env")
    if not isinstance(managed_env, dict):
        return merged
    merged_env = merged.setdefault("env", {})
    if not isinstance(merged_env, dict):
        return merged
    for key in _POLICY_ENV_KEYS:
        if key in managed_env:
            merged_env[key] = managed_env[key]
    return merged


def _append_managed_deny_rules(
    merged: dict[str, object], managed: dict[str, object]
) -> list[object]:
    """Append every managed ``permissions.deny`` rule the merged list lacks; return them.

    The user's file wins the list wholesale in the merge, which would drop a
    fleet guard (``Agent(general-purpose)``, ``Agent(fork)``) on any machine with its
    own deny list.  Append missing, never remove one.
    """
    managed_permissions = managed.get("permissions")
    if not isinstance(managed_permissions, dict):
        return []
    managed_deny = managed_permissions.get("deny")
    if not isinstance(managed_deny, list) or not managed_deny:
        return []
    permissions = merged.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        return []
    deny = permissions.setdefault("deny", [])
    if not isinstance(deny, list):
        return []
    added = [rule for rule in managed_deny if rule not in deny]
    deny.extend(added)
    return added


def _force_fragment_model_keys(
    merged: dict[str, object], fragment: dict[str, object]
) -> dict[str, object]:
    """Restore the model pins the fragment names, over the user's file."""
    fragment_env = fragment.get("env")
    merged_env = merged.get("env")
    if not isinstance(fragment_env, dict) or not isinstance(merged_env, dict):
        return merged
    for key in _FRAGMENT_MODEL_ENV_KEYS:
        if key in fragment_env:
            merged_env[key] = fragment_env[key]
    return merged


def _default_template_path() -> Traversable:
    """Resolve the template from packaged resources, for both a built wheel and a local checkout."""
    return files("ai_chassis").joinpath("defaults/settings.template.json")


def _with_overlay(
    template: dict[str, object], overlay_path: Path | None
) -> dict[str, object]:
    if overlay_path is None or not overlay_path.is_file():
        return template
    return _deep_merge(
        template, _read_json_object(overlay_path, label="settings template overlay")
    )


def load_settings_template(overlay_path: Path | None = None) -> dict[str, object]:
    """The packaged fleet settings template, as the merge reads it, with the checkout's optional
    overlay (keys an installer adds for its own fleet, such as a plugin) merged over it."""
    return _with_overlay(
        _read_json_object(_default_template_path(), label="settings template"),
        overlay_path,
    )


def merge_settings_template(  # noqa: PLR0913 - the overlay joins the keyword-only options
    settings_path: Path | None = None,
    *,
    template_path: Path | None = None,
    overlay_path: Path | None = None,
    fragment_path: Path | None = None,
    scrub_env_keys: Sequence[str] = (),
    out: TextIO | None = None,
) -> Path:
    """Merge the template, its optional overlay and optional fragment into ``settings_path``.

    A missing settings file starts as an empty object.  An existing malformed
    JSON file, template, or declared fragment raises ``SettingsMergeError``
    before any write, preserving its bytes unchanged.

    Precedence, weakest first: template, fragment, the user's existing file,
    then the fleet-policy keys and the model pins the fragment names, with every
    managed ``permissions.deny`` rule appended if missing and every
    ``scrub_env_keys`` key removed from ``env``.  So the
    fragment can override a fleet default the way a per-machine setting is
    supposed to, while any other value the user chose for themselves is never
    overwritten by either.

    The file is written only when the result differs from its current bytes: Claude Code
    reloads settings on every change, and the post-merge hook installs after every merge
    (ISSUE-REF).  With ``out``, the installer's report lines are printed to it.
    """
    target = (
        Path.home() / ".claude" / "settings.json"
        if settings_path is None
        else settings_path
    )
    template = _with_overlay(
        _read_json_object(
            _default_template_path() if template_path is None else template_path,
            label="settings template",
        ),
        overlay_path,
    )
    existing = (
        _read_json_object(target, label="existing settings file")
        if target.exists()
        else {}
    )
    managed = template
    fragment: dict[str, object] = {}
    if fragment_path is not None:
        fragment = _read_json_object(fragment_path, label="settings fragment")
        managed = _deep_merge(managed, fragment)
    report: list[str] = []
    merged = _fill_missing(existing, managed, report)
    _repair_legacy_statusline(merged, template)
    merged = _force_fragment_model_keys(_force_policy_keys(merged, managed), fragment)
    _scrub_env_keys(merged, scrub_env_keys)
    added = _append_managed_deny_rules(merged, managed)
    if added:
        report.append(f"  [+ ]  permissions.deny: added {', '.join(map(str, added))}")
    if out is not None:
        for line in report:
            print(line, file=out)

    changed = _write_if_changed(target, json.dumps(merged, indent=2) + "\n")
    if out is not None:
        print(f"  [ok]  {target} ({'merged' if changed else 'unchanged'})", file=out)
    return target


def _write_if_changed(target: Path, rendered: str) -> bool:
    """Atomically replace ``target`` with ``rendered`` unless it already holds exactly that."""
    try:
        current = target.read_text(encoding="utf-8") if target.exists() else None
    except OSError as exc:
        raise SettingsMergeError(
            f"could not read existing settings file {target}: {exc}"
        ) from exc
    if current == rendered:
        return False
    temporary: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{target.name}.", dir=target.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(rendered)
        os.replace(temporary, target)
    except OSError as exc:
        raise SettingsMergeError(
            f"could not write merged settings {target}: {exc}"
        ) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return True
