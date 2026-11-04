from pathlib import Path


def make_clean(repo: Path) -> None:
    (repo / "notes.md").write_text("CC task ISSUE-REF#42\n", encoding="utf-8")


def make_violation(repo: Path) -> None:
    (repo / "notes.md").write_text("CC task " + "#42\n", encoding="utf-8")
