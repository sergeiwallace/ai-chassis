#!/usr/bin/env python3
"""Fail-closed structural carrier gate for live cx and Claude Agent prompts."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

MODULE = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "_shared"
    / "ask-fidelity"
    / "ask_fidelity.py"
)
SPEC = importlib.util.spec_from_file_location("launch_ask_fidelity", MODULE)
if SPEC is None or SPEC.loader is None:
    print("ask-fidelity gate unavailable", file=sys.stderr)
    raise SystemExit(2)
af = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = af
SPEC.loader.exec_module(af)


def _read_stdin_utf8() -> str:
    """Read stdin as UTF-8 regardless of the platform's default encoding.

    `sys.stdin.read()` decodes with the locale encoding, which is cp1252 on Windows
    (measured on example-client-host-windows: CPython 3.14.7 reports `sys.stdin.encoding == 'cp1252'`
    while `getfilesystemencoding()` is already 'utf-8'). Every carrier this gate
    validates contains U+2014 EM DASH, because its own regexes require the literal
    heading `## FOUNDING ASK — fidelity tier N`. A correctly UTF-8-encoded prompt
    therefore arrived as mojibake and the gate rejected it with "prompt lacks
    canonical FOUNDING ASK fidelity-tier block" -- blocking every Codex delegation
    from that host, while a *wrongly* cp1252-encoded prompt passed because the two
    mis-encodings cancelled out. Reading bytes and decoding explicitly is a no-op on
    POSIX, where stdin is UTF-8 already.
    """
    return sys.stdin.buffer.read().decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    try:
        if argv == ["--agent-payload"]:
            payload = json.loads(_read_stdin_utf8())
            prompt = payload["tool_input"]["prompt"]
            repo_root = Path(payload["cwd"])
        elif len(argv) == 2 and argv[0] == "--repo-root":
            prompt = _read_stdin_utf8()
            repo_root = Path(argv[1])
        elif not argv:
            prompt = _read_stdin_utf8()
            repo_root = Path.cwd()
        else:
            raise af.FidelityError("unsupported launch-gate arguments")
        if not isinstance(prompt, str):
            raise af.FidelityError("launch prompt must be a string")
        # `repo_root` is still read above for diagnostics and caller compatibility,
        # but a Tier 2/3 root is resolved from the carrier's own allowlisted
        # repository field, not from where the launcher happened to be standing.
        tier = af.validate_launch_prompt(prompt, repo_root=repo_root)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ask-fidelity gate BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(f"ask-fidelity gate passed: tier {tier}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
