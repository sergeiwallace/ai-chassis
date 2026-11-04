from pathlib import Path


def make_clean(repo: Path) -> None:
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude/settings.json").write_text("{}\n", encoding="utf-8")


def make_violation(repo: Path) -> None:
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude/settings.json").write_text(
        '{"hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"missing.sh"}]}]}}\n',
        encoding="utf-8",
    )
