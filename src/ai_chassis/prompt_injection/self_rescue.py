"""The commands and tools that perform or precede a compaction, which no band may block.

Design: ``docs/plans/issue-ref-supervisor-self-rescue-and-boundary-hold.md`` D-1..D-3.
The context supervisor's FORCE gate consults this first; the injectors share the same
notion through their ``self_rescue`` flag. Standard library only (D-10 of the library
design): the supervisor imports this module, never the reverse.

Every public function returns a stable rule name (``self-rescue:<rule>``) or ``None``.

One rule is not a compaction step: ``self-rescue:held-cli`` (ISSUE-REF D-6) permits the
non-publishing held-output verbs, so an output can be held at emit time even at FORCE.
``ship``, ``answer`` and ``retire`` publish or change state and stay denied. The same rule
admits ``scripts/format_decision_check.py`` (ISSUE-REF), which ``add --kind decision`` needs a
body to pass.

Shell parsing is strict on purpose, because a permitted first token must never become a
vehicle for an arbitrary command. Quoting is decided by LEXING, in two passes, because the
two classes of construct are inert under different quoting:

- separators (``;`` ``&&`` ``||`` ``|`` ``&`` ``(`` ``)`` ``<`` ``>``) are inert inside
  single AND double quotes, so :func:`split_command` lexes with both quote styles active;
- expansions (``$`` and a backtick) are inert inside single quotes ONLY, because bash still
  expands ``"$(cat x)"`` inside double quotes, so :func:`_has_live_expansion` walks the
  command tracking quote state as bash does (a second shlex pass with ``'`` as the only quote
  read the apostrophe in ``"it's"`` as a quote, ISSUE-REF).

A newline or carriage return is refused rawly, quoted or not: shlex reads a newline as
whitespace, so ``save-resume\\nrm -rf /`` would lex to one permitted command.
"""

from __future__ import annotations

import os
import re
import shlex
import string
import subprocess
from pathlib import Path, PurePath
from typing import Any

SUPERVISOR_MODULE = "ai_chassis.context_supervisor"
LIFECYCLE_SUBCOMMANDS = frozenset({"compact-lifecycle", "poller-health"})
# Each is its own rule: the wrappers in `.claude/bin/`, plus the two pre-existing helpers
# the save-state skill cannot finish without (it must quote a ctx-pct reading and publish
# its resume artifact through save-resume). None of them can publish anything outward.
SELF_RESCUE_EXECUTABLES = frozenset({*LIFECYCLE_SUBCOMMANDS, "save-resume", "ctx-pct"})
SAVE_STATE_SKILL_RULE = "self-rescue:save-state-skill"
# D-6 (ISSUE-REF): `<python> <path>/held_cli.py <verb> --flags`. The script token must RESOLVE
# (symlinks followed, a relative path taken against the tool call's cwd) to `scripts/held_cli.py`
# inside a registered ai-harness checkout: the main tree or one of its linked worktrees, read from
# `git worktree list` of the checkout this module is installed from, never a path constant. A
# basename match alone let `python3 /tmp/evil/held_cli.py add` through at FORCE (ISSUE-REF, 2026-10-12),
# which is arbitrary code behind a gate whose invariant is that nothing publishing is reachable.
HELD_CLI_SCRIPT = "held_cli.py"
HELD_CLI_SCRIPT_DIR = "scripts"
HELD_CLI_VERBS = frozenset({"add", "list", "show", "validate", "stores"})
HELD_CLI_RULE = "self-rescue:held-cli"
_HELD_CLI_HEAD_TOKENS = 3  # interpreter, script, verb
# ISSUE-REF D-3: `add --kind decision` refuses a body the /format-decision checker rejects, so
# the checker must be reachable wherever `add` is. It only reads files and prints, so the same
# rule admits it with no verb, under the same resolution test.
FORMAT_DECISION_SCRIPT = "format_decision_check.py"
_CHECKER_HEAD_TOKENS = 2  # interpreter, script
# D-3: a chain is permitted iff every segment is. Pipes, redirections, subshells and a
# lone `&` stay refused, because each changes WHAT runs rather than only sequencing it.
CHAIN_OPERATORS = frozenset({"&&", ";", "||"})
REFUSED_CHARACTERS = ("\n", "\r")
EXPANSION_CHARACTERS = frozenset({"$", "`"})
_PYTHON_BASENAME = re.compile(r"python(3(\.\d+)?)?")
# Shell operators, quotes and the escape keep their lexer meaning; `$` and the backtick
# never reach the lexer (refused first) but are excluded so they cannot hide in a word.
_NON_WORD_PUNCTUATION = frozenset("();<>|&'\"\\$`")


def _lexer(command: str) -> shlex.shlex:
    """Emit shell operators as their own tokens only when unquoted.

    ``commenters`` is cleared: with shlex's default ``#``, ``bd x #; rm -rf /`` would have
    its ``;`` discarded as a comment before any refusal could see it. Every other ASCII
    punctuation character joins the word characters, so ``--format=%h:%s`` stays one
    argument as bash reads it instead of five tokens.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.commenters = ""
    lexer.wordchars += "".join(
        char for char in string.punctuation if char not in _NON_WORD_PUNCTUATION
    )
    return lexer


_PUNCTUATION = frozenset(_lexer("").punctuation_chars)


def _has_live_expansion(command: str) -> bool:
    """Whether bash would expand a ``$`` or backtick, tracking quote state as bash does.

    A backslash escapes the next character outside single quotes. Errs closed: a ``$`` is
    refused even where bash would leave it literal (``"cost 5$"``).
    """
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            escaped = False
        elif quote == "'":
            if char == "'":
                quote = None
        elif char == "\\":
            escaped = True
        elif char in EXPANSION_CHARACTERS:
            return True
        elif quote == '"':
            if char == '"':
                quote = None
        elif char in "'\"":
            quote = char
    return False


def _segments(tokens: list[str]) -> list[list[str]] | None:
    """Split lexed tokens on chain operators; None when any other operator appears.

    An argument whose ENTIRE value is punctuation (``--notes ';'``) is indistinguishable
    from an operator once lexed, so it is read as one and errs closed.
    """
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= _PUNCTUATION:
            if token not in CHAIN_OPERATORS or not segments[-1]:
                return None
            segments.append([])
        else:
            segments[-1].append(token)
    return segments if segments[-1] else None


def split_command(command: object) -> list[list[str]] | None:
    """The tokens of each chained segment, or None when the command must be refused.

    Refused: a non-string or blank command, a newline or carriage return, unbalanced
    quotes, any unquoted operator other than ``&&`` / ``;`` / ``||``, an empty segment,
    and any live ``$`` or backtick expansion.
    """
    if not isinstance(command, str) or not command.strip():
        return None
    if any(char in command for char in REFUSED_CHARACTERS):
        return None
    if _has_live_expansion(command):
        return None
    try:
        tokens = list(_lexer(command))
    except ValueError:
        return None
    return _segments(tokens)


def _plain_arguments(arguments: list[str]) -> bool:
    """Only ``--flag`` and plain values: no single-dash option such as ``-c``."""
    return all(arg.startswith("--") or not arg.startswith("-") for arg in arguments)


def _module_rule(tokens: list[str]) -> str | None:
    """D-2 module form: ``python[3[.N]] -m ai_chassis.context_supervisor <sub> --flags``."""
    interpreter, *head = tokens[:4]
    if not _PYTHON_BASENAME.fullmatch(PurePath(interpreter).name):
        return None
    if head[:2] != ["-m", SUPERVISOR_MODULE] or head[2:] == []:
        return None
    subcommand, arguments = head[2], tokens[4:]
    if subcommand not in LIFECYCLE_SUBCOMMANDS or not _plain_arguments(arguments):
        return None
    if LIFECYCLE_SUBCOMMANDS & set(arguments):
        return None
    return f"self-rescue:{subcommand}"


def _harness_checkouts() -> frozenset[Path]:
    """Every checkout of the ai-harness repository this module is installed from.

    The main tree and each linked worktree, as ``git worktree list --porcelain`` reports them
    from this file's own checkout (``src/ai_chassis/prompt_injection/`` is three levels below
    the root). Fails CLOSED: when git cannot answer, no checkout is registered and no
    ``held_cli.py`` path qualifies, so a broken enumeration denies rather than widens.
    """
    root = Path(__file__).resolve().parents[3]
    try:
        listing = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if listing.returncode != 0:
        return frozenset()
    return frozenset(
        Path(line[len("worktree ") :]).resolve()
        for line in listing.stdout.splitlines()
        if line.startswith("worktree ")
    )


def _is_registered_held_cli(
    script: str, cwd: str | os.PathLike[str] | None, name: str = HELD_CLI_SCRIPT
) -> bool:
    """Whether ``script`` resolves to ``scripts/<name>`` in a registered checkout."""
    base = Path(cwd) if cwd else Path.cwd()
    try:
        resolved = (base / Path(script).expanduser()).resolve()
    except (OSError, RuntimeError):
        return False
    if resolved.name != name or resolved.parent.name != HELD_CLI_SCRIPT_DIR:
        return False
    return resolved.parent.parent in _harness_checkouts()


def _checker_rule(
    tokens: list[str], cwd: str | os.PathLike[str] | None = None
) -> str | None:
    """``python[3[.N]] <checkout>/scripts/format_decision_check.py --flags <paths>``."""
    if len(tokens) < _CHECKER_HEAD_TOKENS:
        return None
    interpreter, script, *arguments = tokens
    if not _PYTHON_BASENAME.fullmatch(PurePath(interpreter).name):
        return None
    if PurePath(script).name != FORMAT_DECISION_SCRIPT or not _plain_arguments(
        arguments
    ):
        return None
    registered = _is_registered_held_cli(script, cwd, FORMAT_DECISION_SCRIPT)
    return HELD_CLI_RULE if registered else None


def _held_cli_rule(
    tokens: list[str], cwd: str | os.PathLike[str] | None = None
) -> str | None:
    """D-6 form: ``python[3[.N]] <checkout>/scripts/held_cli.py <permitted verb> --flags``.

    The same rule admits the `/format-decision` checker (`_checker_rule`).
    """
    if len(tokens) >= _CHECKER_HEAD_TOKENS and (
        PurePath(tokens[1]).name == FORMAT_DECISION_SCRIPT
    ):
        return _checker_rule(tokens, cwd)
    if len(tokens) < _HELD_CLI_HEAD_TOKENS:
        return None
    interpreter, script, verb, *arguments = tokens
    if not _PYTHON_BASENAME.fullmatch(PurePath(interpreter).name):
        return None
    if verb not in HELD_CLI_VERBS or not _plain_arguments(arguments):
        return None
    if PurePath(script).name != HELD_CLI_SCRIPT:
        return None
    return HELD_CLI_RULE if _is_registered_held_cli(script, cwd) else None


def _segment_rule(
    tokens: list[str], cwd: str | os.PathLike[str] | None = None
) -> str | None:
    """The rule one chained segment matches, by its first token."""
    name = PurePath(tokens[0]).name
    if name in LIFECYCLE_SUBCOMMANDS:
        return f"self-rescue:{name}" if _plain_arguments(tokens[1:]) else None
    if name in SELF_RESCUE_EXECUTABLES:
        return f"self-rescue:{name}"
    return _module_rule(tokens) or _held_cli_rule(tokens, cwd)


def permits_tokens(
    tokens: list[str], cwd: str | os.PathLike[str] | None = None
) -> str | None:
    """The rule a single already-lexed segment matches, or None.

    ``cwd`` is the tool call's working directory (the hook payload's ``cwd``), against which a
    relative ``held_cli.py`` path is resolved; the process cwd when None.
    """
    return _segment_rule(tokens, cwd) if tokens else None


def permits_bash_command(
    command: object, cwd: str | os.PathLike[str] | None = None
) -> str | None:
    """The rule a Bash command matches when EVERY chained segment is self-rescue.

    A chain of several rules reports them joined with ``+`` in first-seen order, so
    ``ctx-pct && ctx-pct --advice`` is ``self-rescue:ctx-pct``.
    """
    segments = split_command(command)
    if segments is None:
        return None
    rules: list[str] = []
    for tokens in segments:
        rule = permits_tokens(tokens, cwd)
        if rule is None:
            return None
        if rule not in rules:
            rules.append(rule)
    return "+".join(rules)


def is_save_state_skill(payload: dict[str, Any]) -> bool:
    """Claude Code's ``Skill(save-state)`` invocation, from its hook payload."""
    if payload.get("tool_name") != "Skill":
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    return any(
        tool_input.get(field) == "save-state" for field in ("skill", "name", "command")
    )


def permits_tool_call(payload: dict[str, Any]) -> str | None:
    """The self-rescue rule a ``PreToolUse`` payload matches, or None."""
    if is_save_state_skill(payload):
        return SAVE_STATE_SKILL_RULE
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    cwd = payload.get("cwd")
    return permits_bash_command(
        tool_input.get("command"), cwd if isinstance(cwd, str) and cwd else None
    )
