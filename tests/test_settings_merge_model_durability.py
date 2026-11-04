"""A chosen default model must survive ``merge_settings_template``.

The merge used to replace every template-owned top-level key wholesale.  Two
consequences made the per-machine model default unreachable, both reproduced
against the shipped template:

*   the template pins a fleet default ``model``, so the merge overwrote whatever
    the user had picked with a bare tier alias -- which resolves to that tier's
    ordinary context window, not the wide-context variant, so the symptom is a
    session landing on a DEFAULT rather than on a downgraded preference;
*   ``env`` is a nested object, so replacing it wholesale deleted every
    machine-local variable inside it, ``ANTHROPIC_MODEL`` included -- and that
    variable is the only layer that outranks ``settings.json``'s ``model``.

So the machine fragment could not express a durable model either: it can only
supply ``env``, and the very next merge threw that object away.

The merge now layers template < fragment < the user's own file, recursing into
nested objects, with the documented fleet-policy keys still forced on top.
These tests pin each layer, and the positive controls pin that the template is
still actually applied -- a merge that never writes anything would satisfy the
preservation assertions alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_chassis.settings_merge import merge_settings_template

REPO_ROOT = Path(__file__).parent.parent
SHIPPED_TEMPLATE = (
    REPO_ROOT / "src" / "ai_chassis" / "defaults" / "settings.template.json"
)

WIDE_CONTEXT_MODEL = "us.anthropic.claude-opus-5[1m]"


def _write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _merge(
    tmp_path: Path,
    *,
    existing: dict | None = None,
    template: dict | None = None,
    fragment: dict | None = None,
) -> dict:
    settings = tmp_path / ".claude" / "settings.json"
    if existing is not None:
        _write(settings, existing)
    else:
        settings.parent.mkdir(parents=True, exist_ok=True)
    template_path = (
        SHIPPED_TEMPLATE
        if template is None
        else _write(tmp_path / "template.json", template)
    )
    fragment_path = (
        None if fragment is None else _write(tmp_path / "fragment.json", fragment)
    )
    merge_settings_template(
        settings, template_path=template_path, fragment_path=fragment_path
    )
    return json.loads(settings.read_text(encoding="utf-8"))


def test_given_a_user_chosen_model_when_the_template_is_merged_then_it_survives(
    tmp_path: Path,
) -> None:
    merged = _merge(
        tmp_path,
        existing={"model": WIDE_CONTEXT_MODEL},
        template={"model": "sonnet"},
    )

    assert merged["model"] == WIDE_CONTEXT_MODEL


def test_given_a_user_model_env_var_when_the_template_is_merged_then_it_survives(
    tmp_path: Path,
) -> None:
    merged = _merge(
        tmp_path,
        existing={"env": {"ANTHROPIC_MODEL": WIDE_CONTEXT_MODEL}},
        template={"env": {"DISABLE_GROWTHBOOK": ""}},
    )

    assert merged["env"]["ANTHROPIC_MODEL"] == WIDE_CONTEXT_MODEL
    assert merged["env"]["DISABLE_GROWTHBOOK"] == "", (
        "the template's own env keys must still be installed alongside it"
    )


def test_given_the_shipped_template_when_merged_over_a_chosen_model_then_it_survives(
    tmp_path: Path,
) -> None:
    """The defect as it actually shipped -- synthetic templates cannot catch a
    fleet default that changes in the real file.  Since ISSUE-REF the shipped
    template carries no ``model`` at all (install.sh Step 4d sets the alias per
    machine profile), which is the strongest form of not overwriting it."""
    assert "model" not in json.loads(SHIPPED_TEMPLATE.read_text(encoding="utf-8")), (
        "the shipped template pins a model again; Step 4d owns the session default"
    )

    merged = _merge(
        tmp_path,
        existing={
            "model": WIDE_CONTEXT_MODEL,
            "env": {"ANTHROPIC_MODEL": WIDE_CONTEXT_MODEL},
        },
    )

    assert merged["model"] == WIDE_CONTEXT_MODEL
    assert merged["env"]["ANTHROPIC_MODEL"] == WIDE_CONTEXT_MODEL


def test_given_a_machine_local_env_var_when_the_template_is_merged_then_it_survives(
    tmp_path: Path,
) -> None:
    """Replacing ``env`` wholesale also deleted the backend switch, measured on a
    live host: without it the agent takes an auth route it has no credential for."""
    merged = _merge(
        tmp_path,
        existing={"env": {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-east-1"}},
    )

    assert merged["env"]["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert merged["env"]["AWS_REGION"] == "us-east-1"


def test_given_a_machine_fragment_model_when_the_file_is_fresh_then_it_beats_the_template(
    tmp_path: Path,
) -> None:
    """The fragment is the only per-machine hook, so it must outrank the fleet
    default -- otherwise a per-machine model default cannot be expressed at all."""
    merged = _merge(
        tmp_path,
        template={"model": "sonnet"},
        fragment={"model": WIDE_CONTEXT_MODEL},
    )

    assert merged["model"] == WIDE_CONTEXT_MODEL


def test_given_a_machine_fragment_env_when_merged_then_sibling_template_env_survives(
    tmp_path: Path,
) -> None:
    """The fragment supplies ``env`` too, and replacing that object wholesale
    dropped every fleet env key the fragment happens not to mention."""
    merged = _merge(
        tmp_path,
        template={"env": {"DISABLE_GROWTHBOOK": ""}},
        fragment={"env": {"ANTHROPIC_MODEL": WIDE_CONTEXT_MODEL}},
    )

    assert merged["env"]["ANTHROPIC_MODEL"] == WIDE_CONTEXT_MODEL
    assert merged["env"]["DISABLE_GROWTHBOOK"] == ""


def test_given_a_user_value_for_a_fleet_policy_key_when_merged_then_policy_wins(
    tmp_path: Path,
) -> None:
    """NEGATIVE CONTROL for "the user always wins": a few keys are fleet policy,
    and the installer forces those everywhere. Dropping that forcing while making
    the model durable would trade one silent regression for another."""
    merged = _merge(
        tmp_path,
        existing={
            "tui": "default",
            "env": {"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1"},
        },
        template={
            "tui": "fullscreen",
            "env": {"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "3"},
        },
    )

    assert merged["tui"] == "fullscreen"
    assert merged["env"]["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "3"


def test_given_a_stale_model_pin_when_the_fragment_names_it_then_the_fragment_wins(
    tmp_path: Path,
) -> None:
    """The fragment owns the model env keys it names (ISSUE-REF): a denied `global.` Haiku
    id in the live file, measured on example-project-ec2, is replaced by the fragment's `us.` id,
    while a model key the fragment does not name and the top-level `model` both survive."""
    merged = _merge(
        tmp_path,
        existing={
            "model": WIDE_CONTEXT_MODEL,
            "env": {
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "global.anthropic.claude-haiku-5-5",
                "ANTHROPIC_DEFAULT_FABLE_MODEL": "us.anthropic.claude-fable-5",
            },
        },
        template={"model": "sonnet"},
        fragment={
            "env": {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "us.anthropic.claude-haiku-5-5"}
        },
    )

    assert (
        merged["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"]
        == "us.anthropic.claude-haiku-5-5"
    )
    assert (
        merged["env"]["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "us.anthropic.claude-fable-5"
    )
    assert merged["model"] == WIDE_CONTEXT_MODEL


@pytest.mark.parametrize("key", ["mcpServers", "statusLine", "permissions"])
def test_given_a_fresh_file_when_merged_then_every_template_key_is_installed(
    tmp_path: Path, key: str
) -> None:
    """POSITIVE CONTROL: preservation assertions alone would pass for a merge that
    wrote nothing at all."""
    template = json.loads(SHIPPED_TEMPLATE.read_text(encoding="utf-8"))

    merged = _merge(tmp_path)

    assert merged[key] == template[key]


def test_given_an_unrelated_user_key_when_merged_then_it_survives(
    tmp_path: Path,
) -> None:
    merged = _merge(tmp_path, existing={"theme": "dark", "nested": {"kept": [1, 2]}})

    assert merged["theme"] == "dark"
    assert merged["nested"] == {"kept": [1, 2]}
