#!/usr/bin/env python3
"""Idempotently add the Claude Code "Shift+Enter inserts a newline" binding to
the local terminal configs that need it.

The logic is the public core's ``ai_chassis.terminal_keybindings``, which
install.sh reaches through ``ai-harness apply --only terminal-keybindings``
(ISSUE-REF batch C-C2); this script is the CLI over it. Why each terminal
gets the escape sequence it does is in that module's docstring.

Usage:
    apply-terminal-keybindings.py [--windows-terminal PATH] [--vscode PATH]

Each path is optional; only the ones provided (and applicable) are processed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_chassis.terminal_keybindings import (  # noqa: E402, F401 - re-exported for this script's readers
    _MSG,
    VSCODE_BINDING,
    WT_ACTION_ID,
    WT_INPUT,
    _copy_string_literal,
    _end_of_block_comment,
    _end_of_line_comment,
    _load_jsonc,
    _report,
    _strip_jsonc,
    _write_json,
    merge_vscode,
    merge_windows_terminal,
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Add Claude Code Shift+Enter -> newline terminal bindings (idempotent)."
    )
    ap.add_argument("--windows-terminal", help="path to Windows Terminal settings.json")
    ap.add_argument("--vscode", help="path to VS Code keybindings.json")
    args = ap.parse_args()

    if args.windows_terminal:
        _report("Windows Terminal", merge_windows_terminal(args.windows_terminal))
    if args.vscode:
        _report("VS Code", merge_vscode(args.vscode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
