from pathlib import Path


def make_clean(repo: Path) -> None:
    (repo / "README.md").write_text("clean\n", encoding="utf-8")


def make_violation(repo: Path) -> None:
    (repo / "Cargo.toml").write_text(
        "[package]\nname='bad'\nversion='0.1.0'\n", encoding="utf-8"
    )
