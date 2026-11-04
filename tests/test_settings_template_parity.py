"""The two copies of the settings template must not drift apart.

The packaged installer resolves ``defaults/settings.template.json`` out of the
wheel, and since ISSUE-REF T-3.4 batch 2 install.sh merges through it too
(``ai-harness apply --only settings``); the repo's own
``.claude/settings.template.json`` is the copy the release policy publishes and
the shell-era tests read.  A fleet-wide default
added to one and not the other is therefore not fleet-wide at all, and it fails
silently -- the machine keeps working, just without the key.

That already happened: a commit defaulting ``crossSessionInbound`` "fleet-wide"
touched only the repo copy, and the only symptom was two installer-parity rows
going red for a reason unrelated to what either row is about.  Comparing the
files directly is what names the cause.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SHELL_INSTALLER_TEMPLATE = REPO_ROOT / ".claude" / "settings.template.json"
PACKAGED_TEMPLATE = (
    REPO_ROOT / "src" / "ai_chassis" / "defaults" / "settings.template.json"
)


def test_given_both_settings_templates_when_compared_then_they_declare_the_same_keys() -> (
    None
):
    shell = json.loads(SHELL_INSTALLER_TEMPLATE.read_text(encoding="utf-8"))
    packaged = json.loads(PACKAGED_TEMPLATE.read_text(encoding="utf-8"))

    assert sorted(shell) == sorted(packaged)


def test_given_both_settings_templates_when_compared_then_every_value_agrees() -> None:
    """Key parity alone would pass while a value silently differed per installer."""
    shell = json.loads(SHELL_INSTALLER_TEMPLATE.read_text(encoding="utf-8"))
    packaged = json.loads(PACKAGED_TEMPLATE.read_text(encoding="utf-8"))

    assert shell == packaged
