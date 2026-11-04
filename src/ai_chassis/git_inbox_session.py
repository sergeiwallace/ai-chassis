"""Bounded argv-only execution for machine-targeted session commands.

The receiver owns addressing, sender holds and audit persistence. Audit excerpts
are bounded and withheld when the existing secret-shape check detects sensitive output.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from ai_chassis.git_inbox import secret_shape

SLUG_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
POLICIES = frozenset({"auto", "never", "always"})
COMMAND_TIMEOUT_SECONDS = 30
RELAUNCH_WORDS = 2
POLICY_WORDS = 3
OUTPUT_EXCERPT_CHARS = 1024


@dataclass(frozen=True)
class CommandResult:
    outcome: str
    exit_status: int | None = None
    output: str = ""


def _excerpt(output: str | bytes | None) -> str:
    text = (
        output.decode("utf-8", errors="replace")
        if isinstance(output, bytes)
        else output or ""
    )
    if secret_shape(text) is not None:
        return "[withheld: secret-shape]"
    return text[:OUTPUT_EXCERPT_CHARS]


def apply_command(body: str) -> CommandResult:
    """Return applied, refused, failed or timed-out, without interpreting shell text."""
    lines = body.splitlines()
    words = lines[0].split() if lines else []
    if (
        len(words) < RELAUNCH_WORDS
        or SLUG_RE.fullmatch(words[1]) is None
        or words[1].startswith("-")
    ):
        return CommandResult("refused")
    if words[0] == "relaunch" and len(words) == RELAUNCH_WORDS:
        argv = ["ai", "sessions", "relaunch", words[1]]
    elif (
        words[0] == "relaunch-policy"
        and len(words) == POLICY_WORDS
        and words[2] in POLICIES
    ):
        argv = ["ai", "sessions", "policy", words[1], words[2]]
    else:
        return CommandResult("refused")
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult("timed-out", output=_excerpt(exc.output))
    except OSError:
        return CommandResult("failed")
    return CommandResult(
        "applied" if result.returncode == 0 else "failed",
        exit_status=result.returncode,
        output=_excerpt(result.stdout),
    )
