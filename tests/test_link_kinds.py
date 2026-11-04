import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from ai_chassis.links import reconcile


def test_symlink_kind_is_recordable_in_a_tmp_path(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_text("fixture")
    assert reconcile(source, target) == "symlink"
    assert target.is_symlink()


def test_copy_fallback_is_reported(monkeypatch, tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_text("fixture")
    monkeypatch.setattr(
        Path,
        "symlink_to",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no links")),
    )
    monkeypatch.setattr(
        os, "link", lambda *_args: (_ for _ in ()).throw(OSError("no hardlinks"))
    )
    assert reconcile(source, target) == "copy"
    assert target.read_text() == "fixture"
