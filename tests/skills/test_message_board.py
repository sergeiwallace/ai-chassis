from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path("skills/message-board/scripts/route_message.py")


def _router():
    spec = importlib.util.spec_from_file_location("message_board_router", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_reachability_selects_only_native_channel():
    decision = _router().select_channel(native_reachable=True, cross_machine=True)
    assert decision.channel == "native"
    assert decision.board_delivery is False


def test_unreachable_cross_machine_target_selects_only_board():
    decision = _router().select_channel(native_reachable=False, cross_machine=True)
    assert decision.channel == "board"
    assert decision.native_delivery is False


def test_unreachable_same_machine_target_does_not_misuse_board():
    with pytest.raises(ValueError, match="same-machine"):
        _router().select_channel(native_reachable=False, cross_machine=False)


def test_skill_warns_that_board_content_is_untrusted_and_requires_local_authorization():
    text = Path("skills/message-board/SKILL.md").read_text()
    assert "untrusted data" in text
    assert "local authorization" in text
    assert "Never deliver the same hand-off on both channels" in text
