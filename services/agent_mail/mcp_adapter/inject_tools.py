"""Prompt-injection tools on the agent-mail FastMCP server (ISSUE-REF D-9).

Thin wrappers over ``python -m ai_chassis.prompt_injection``: the CLI is the
load-bearing surface, so a tool returns exactly what the CLI's ``--json`` prints
and an MCP client sees the same outcomes, refusals and rows a shell does.
Importing this module needs no board configuration and no library import; the
library runs in the child process, so a library failure is a returned error,
never a crash of the agent-mail server.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

#: Long enough for the library's own ready wait (30 s) plus two landing polls.
_CLI_TIMEOUT_SECONDS = 120


def _known_use_cases() -> list[str]:
    from ai_chassis.prompt_injection.injector import INJECTORS

    return sorted(INJECTORS)


def _cli(*args: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ai_chassis.prompt_injection", *args, "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_CLI_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"error": f"prompt-injection CLI did not run: {type(error).__name__}"}
    lines = result.stdout.strip().splitlines()
    try:
        return json.loads(lines[-1]) if lines else {"error": result.stderr.strip()}
    except json.JSONDecodeError:
        return {"error": result.stderr.strip() or result.stdout.strip()}


def inject_prompt(
    use_case: str, target: str, text: str, dry_run: bool = False
) -> dict[str, Any]:
    """Type ``text`` into ``target`` (self | session:<name> | pane:%N | key:<key>)
    as ``use_case`` and return the library's InjectionOutcome JSON."""
    known = _known_use_cases()
    if use_case not in known:
        return {"error": f"unknown use_case {use_case!r}", "known_use_cases": known}
    with tempfile.TemporaryDirectory(prefix="aih-inject-") as directory:
        text_file = Path(directory) / "payload.txt"
        text_file.write_text(text, encoding="utf-8")
        args = ["inject", "--use-case", use_case, "--target", target]
        args += ["--text-file", str(text_file)]
        return _cli(*args, *(["--dry-run"] if dry_run else []))


def injection_status(target: str) -> dict[str, Any]:
    """The status CLI's rows for ``target``: the newest attempt per use case."""
    return _cli("status", "--target", target)


def register_inject_tools(mcp: Any) -> None:
    """Add both tools to ``mcp``, the existing ``FastMCP("secure-agent-mail")``."""
    mcp.tool()(inject_prompt)
    mcp.tool()(injection_status)
