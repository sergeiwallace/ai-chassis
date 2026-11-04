"""WSL mount safety regression coverage using an injected environment."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from ai_harness.apply import ApplyError, InstallTarget, apply


def test_wsl_mnt_target_is_refused_before_apply_mutates_state(tmp_path: Path) -> None:
    patterns, allowlist = tmp_path / "patterns", tmp_path / "allowlist"
    patterns.write_text("never-matches\n", encoding="utf-8")
    allowlist.write_text("output\tnever-matches\n", encoding="utf-8")
    with pytest.raises(ApplyError, match="Windows-mount link semantics"):
        apply(
            {"output": b"managed\n"},
            [InstallTarget("output", Path("/mnt/c/managed-target/target.txt"))],
            tmp_path / "state",
            patterns=patterns,
            allowlist=allowlist,
            environment="wsl",
        )
    assert not (tmp_path / "state").exists()
