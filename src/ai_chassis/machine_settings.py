"""Merge one machine's settings fragment into ``~/.claude/settings.json`` (install.sh Step 4b).

The ``machine-settings`` step runs after the template merge (``settings_merge``) and adds the
fragment's keys the file lacks, recursing into objects, so a value the user chose is never
overwritten. Three exceptions, the first two carried over from the shell step:

* the model pins the fragment names (``_FRAGMENT_MODEL_ENV_KEYS``) are the fragment's to own
  (ISSUE-REF): an add-only merge made a pin installed once permanent;
* ``env.AWS_PROFILE`` holding the placeholder is resolved at call time from the live AWS config
  or dropped when nothing resolves, and a value the AWS config does not declare at all is a stale
  literal and is replaced (ISSUE-REF);
* a plugin the fragment sets to ``false`` in ``enabledPlugins`` is disabled, since the template
  merge before it has already enabled every fleet plugin (ISSUE-REF).

Where the shell reset a malformed settings file to ``{}``, this refuses before writing, the
template merge's rule (IN-20, IN-21); and the result is written only when its bytes differ.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from .settings_merge import (
    _FRAGMENT_MODEL_ENV_KEYS,
    SettingsMergeError,
    _fill_missing,
    _read_json_object,
    _write_if_changed,
)

AWS_PROFILE_PLACEHOLDER = "__AIH_AWS_SSO_PROFILE__"

#: ``(resolved profile name or "", every profile the AWS config declares)``, asked lazily: only a
#: fragment that leaves ``env.AWS_PROFILE`` in the merged file needs the answer.
AwsProfiles = Callable[[], tuple[str, frozenset[str]]]


def _fragment(
    path: Path, substitutions: Sequence[tuple[str, str]]
) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsMergeError(
            f"could not read settings fragment {path}: {exc}"
        ) from exc
    for token, value in substitutions:
        text = text.replace(token, value)
    try:
        fragment = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsMergeError(
            f"settings fragment is not valid JSON: {path}"
        ) from exc
    if not isinstance(fragment, dict):
        raise SettingsMergeError(
            f"settings fragment must contain a JSON object: {path}"
        )
    return fragment


def read_machine_fragment(path: Path) -> dict[str, object]:
    """The fragment at ``path``, parsed without substitutions (``SettingsMergeError`` if bad)."""
    return _fragment(path, ())


def _own_plugin_disables(
    merged: dict[str, object], fragment: dict[str, object], out: TextIO
) -> None:
    """A plugin the fragment sets to ``false`` stays disabled over the template's ``true``.

    The template merge runs first and enables every fleet plugin, so add-only would leave a
    profile's explicit ``false`` with no effect at all.
    """
    disables = fragment.get("enabledPlugins")
    if not isinstance(disables, dict):
        return
    enabled = merged.setdefault("enabledPlugins", {})
    if not isinstance(enabled, dict):
        return
    for plugin_id, on in disables.items():
        if on is False and enabled.get(plugin_id) is not False:
            enabled[plugin_id] = False
            print(
                f"  [ok]  settings enabledPlugins.{plugin_id} disabled by the machine fragment",
                file=out,
            )


def _own_model_pins(
    merged: dict[str, object], fragment: dict[str, object], out: TextIO
) -> None:
    fragment_env = fragment.get("env")
    merged_env = merged.get("env")
    if not isinstance(fragment_env, dict) or not isinstance(merged_env, dict):
        return
    for key in _FRAGMENT_MODEL_ENV_KEYS:
        if key in fragment_env and merged_env.get(key) != fragment_env[key]:
            merged_env[key] = fragment_env[key]
            print(
                f"  [ok]  settings env.{key} set from the machine fragment "
                "(fragment-owned model pin)",
                file=out,
            )


def _resolve_aws_profile(
    merged: dict[str, object], aws_profiles: AwsProfiles, out: TextIO
) -> None:
    env = merged.get("env")
    if not isinstance(env, dict) or "AWS_PROFILE" not in env:
        return
    resolved, configured = aws_profiles()
    current = str(env["AWS_PROFILE"]).strip()
    if current == AWS_PROFILE_PLACEHOLDER:
        if resolved:
            env["AWS_PROFILE"] = resolved
            print(
                "  [ok]  settings env.AWS_PROFILE resolved from ~/.aws/config at call time",
                file=out,
            )
        else:
            del env["AWS_PROFILE"]
            print(
                "  [warn] settings env.AWS_PROFILE dropped: no SSO profile could be resolved "
                "from ~/.aws/config, and a placeholder would name a profile that cannot exist",
                file=sys.stderr,
            )
    elif resolved and current != resolved and current not in configured:
        env["AWS_PROFILE"] = resolved
        print(
            f"  [ok]  settings env.AWS_PROFILE replaced: {current!r} is in no "
            "~/.aws/config section, so it was a stale literal (ISSUE-REF)",
            file=out,
        )


def merge_machine_fragment(
    settings_path: Path,
    fragment_path: Path,
    *,
    substitutions: Sequence[tuple[str, str]] = (),
    aws_profiles: AwsProfiles = lambda: ("", frozenset()),
    out: TextIO | None = None,
) -> bool:
    """Merge ``fragment_path`` into ``settings_path``; True when the file was rewritten.

    ``substitutions`` are literal ``(token, value)`` replacements applied to the fragment's text
    before it is parsed, in order (install.sh replaced ``CLAUDE_HOOKS_DIR`` before ``CLAUDE_DIR``).
    A malformed fragment or settings file raises ``SettingsMergeError`` before any write.
    """
    out = sys.stdout if out is None else out
    fragment = _fragment(fragment_path, substitutions)
    existing = (
        _read_json_object(settings_path, label="existing settings file")
        if settings_path.exists()
        else {}
    )
    report: list[str] = []
    merged = _fill_missing(existing, fragment, report)
    for line in report:
        print(line, file=out)
    _own_model_pins(merged, fragment, out)
    _own_plugin_disables(merged, fragment, out)
    _resolve_aws_profile(merged, aws_profiles, out)
    changed = _write_if_changed(settings_path, json.dumps(merged, indent=2) + "\n")
    print(
        f"  [ok]  {settings_path} (machine fragment {'merged' if changed else 'unchanged'})",
        file=out,
    )
    return changed
