"""Terminal Shift+Enter -> newline bindings (install.sh's retired Step 10, IN-39).

A terminal that cannot tell Shift+Enter from Enter sends plain CR for both, and Claude Code
submits. Claude Code's chat prompt inserts a newline when the decoded key carries either modifier
(verified in docs/research/claude-code-shift-enter-key-encoding.md): ``ESC CR`` decodes to
``{name:"return", meta:true}`` and the CSI-u ``ESC [ 13 ; 2 u`` to ``{name:"return", shift:true}``.
Each terminal gets the one Claude Code itself would choose, so ``/terminal-setup`` sees the binding
as already configured:

  * **Windows Terminal** sends plain CR for both keys over ConPTY and Git Bash, and is in Claude
    Code's native-CSI-u table, so it gets CSI-u through a ``sendInput`` action and keybinding in
    its ``settings.json``.
  * **VS Code**'s integrated terminal also sends plain CR and is deliberately absent from that
    table, so it gets exactly what ``/terminal-setup`` writes: ``shift+enter`` ->
    ``workbench.action.terminal.sendSequence`` with text ``ESC CR`` when ``terminalFocus``.

VS Code keybindings are client-side: over Remote-SSH the binding must exist on the machine running
the VS Code window, which is why this runs on the Windows machine itself.

Both merges are idempotent and self-repairing: a run changes nothing when the binding already
matches, and rewrites one whose escape sequence has drifted. Both files are JSONC, parsed
tolerantly, and a file is rewritten only when its content must change (a one-time reformat that
drops comments). Each terminal's config is independent: the Windows Terminal edit cannot affect VS
Code, or the reverse. ``projects-wide-session-config/apply-terminal-keybindings.py`` is the CLI
over this module.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from .profile import _is_windows_shell

# Windows Terminal: CSI-u encoding of Shift+Enter (codepoint 13 = Enter,
# modifier 2 = Shift). json.dumps re-escapes the ESC as a JSON code point on write.
WT_ACTION_ID = "User.sendInput.claudeShiftEnter"
WT_INPUT = "\x1b[13;2u"

# VS Code: the exact binding `/terminal-setup` writes (meta+enter = ESC CR).
VSCODE_BINDING = {
    "key": "shift+enter",
    "command": "workbench.action.terminal.sendSequence",
    "args": {"text": "\x1b\r"},
    "when": "terminalFocus",
}


def _copy_string_literal(text: str, start: int, out: list[str]) -> int:
    """Copy the string literal opening at ``text[start]``; return the index after it.

    Escapes are consumed in pairs, so ``\\"`` does not close the literal and ``\\\\``
    does not escape the quote that follows it. An unterminated literal is copied to
    the end of input, because the caller parses tolerantly rather than raising.
    """
    out.append(text[start])
    i = start + 1
    while i < len(text):
        char = text[i]
        out.append(char)
        i += 1
        if char == "\\" and i < len(text):
            out.append(text[i])
            i += 1
        elif char == '"':
            break
    return i


def _end_of_line_comment(text: str, start: int) -> int:
    """Return the index of the newline ending the ``//`` comment at ``start``."""
    i = start
    while i < len(text) and text[i] not in "\r\n":
        i += 1
    return i


def _end_of_block_comment(text: str, start: int) -> int:
    """Return the index after the ``*/`` closing the comment at ``start``.

    An unclosed block comment consumes the rest of the input.
    """
    i = start + 2
    while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
        i += 1
    return i + 2


def _strip_jsonc(text: str) -> str:
    """Remove // line comments, /* */ block comments, and trailing commas from
    JSONC, preserving anything inside string literals.

    One dispatch on the current character, with each of the four sub-languages
    owned by a helper that takes an index and returns the next one -- so the
    cursor is a value passed and returned, never shared mutable scanner state.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == '"':
            i = _copy_string_literal(text, i, out)
        elif char == "/" and text[i + 1 : i + 2] == "/":
            i = _end_of_line_comment(text, i)
        elif char == "/" and text[i + 1 : i + 2] == "*":
            i = _end_of_block_comment(text, i)
        else:
            out.append(char)
            i += 1
    # Drop trailing commas before } or ] (comment stripping already removed the
    # string-preserving concern for the common cases in these config files).
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _load_jsonc(path: str):
    with open(path, "r", encoding="utf-8-sig") as f:
        text = f.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_strip_jsonc(text))


def _write_json(path, obj) -> None:
    # json.dumps always escapes control chars on write, so the ESC is emitted as
    # an escaped code point and the file stays valid JSON regardless of ensure_ascii.
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, indent=4, ensure_ascii=False) + "\n")


def merge_windows_terminal(path: str) -> str:
    """Return one of: added | present | repaired | conflict | skip-missing | skip-shape."""
    if not os.path.isfile(path):
        return "skip-missing"
    obj = _load_jsonc(path)
    if not isinstance(obj, dict):
        return "skip-shape"
    actions = obj.setdefault("actions", [])
    kbs = obj.setdefault("keybindings", [])
    if not isinstance(actions, list) or not isinstance(kbs, list):
        return "skip-shape"

    # Respect a user's own shift+enter binding to a different action.
    foreign = [
        k
        for k in kbs
        if isinstance(k, dict)
        and k.get("keys") == "shift+enter"
        and k.get("id") != WT_ACTION_ID
    ]
    if foreign:
        return "conflict"

    added = repaired = False
    ours = [a for a in actions if isinstance(a, dict) and a.get("id") == WT_ACTION_ID]
    if not ours:
        actions.append(
            {"command": {"action": "sendInput", "input": WT_INPUT}, "id": WT_ACTION_ID}
        )
        added = True
    else:
        # Our action exists but may carry a stale escape sequence from an earlier
        # version of this script; rewrite it rather than reporting it as correct.
        for action in ours:
            cmd = action.get("command")
            if not isinstance(cmd, dict) or cmd.get("input") != WT_INPUT:
                action["command"] = {"action": "sendInput", "input": WT_INPUT}
                repaired = True
    if not any(isinstance(k, dict) and k.get("id") == WT_ACTION_ID for k in kbs):
        kbs.append({"id": WT_ACTION_ID, "keys": "shift+enter"})
        added = True
    if added or repaired:
        _write_json(path, obj)
        return "added" if added else "repaired"
    return "present"


def merge_vscode(path: str) -> str:
    """Return one of: added | present | repaired | conflict-added | skip-missing | skip-shape."""
    if os.path.isfile(path):
        obj = _load_jsonc(path)
    elif os.path.isdir(os.path.dirname(path) or "."):
        obj = []  # user-overrides file is safe to create empty
    else:
        return "skip-missing"
    if not isinstance(obj, list):
        return "skip-shape"

    def is_send_seq(b):
        return (
            isinstance(b, dict)
            and b.get("key") == "shift+enter"
            and b.get("command") == "workbench.action.terminal.sendSequence"
        )

    ours = [b for b in obj if is_send_seq(b)]
    if ours:
        # The binding exists, but an earlier version of this script (or a manual
        # edit) may have left a different escape sequence behind. Compare the
        # payload, not just the key+command, and rewrite when it has drifted.
        wanted = VSCODE_BINDING["args"]
        stale = [b for b in ours if b.get("args") != wanted]
        if not stale:
            return "present"
        for b in stale:
            b["args"] = dict(wanted)
            b["when"] = VSCODE_BINDING["when"]
        _write_json(path, obj)
        return "repaired"
    # A shift+enter bound to a non-sendSequence command with terminalFocus would
    # collide; report it but still add ours (CC needs the newline) - VS Code
    # resolves same-key bindings last-wins, and ours is appended last.
    foreign = any(
        isinstance(b, dict)
        and b.get("key") == "shift+enter"
        and b.get("when") == "terminalFocus"
        for b in obj
    )
    obj.append(dict(VSCODE_BINDING))
    _write_json(path, obj)
    return "conflict-added" if foreign else "added"


# ASCII-only messages: this prints to the cp1252 Windows console, which crashes
# on characters like the arrow (U+2192). Keep it plain ASCII.
_MSG = {
    "added": "[ok]  {label}: Shift+Enter -> newline binding added",
    "present": "[ok]  {label}: Shift+Enter -> newline binding already present",
    "repaired": "[ok]  {label}: Shift+Enter -> newline binding had a stale escape sequence - repaired",
    "conflict-added": "[ok]  {label}: added Shift+Enter -> newline (note: another shift+enter terminal binding exists; ours takes precedence)",
    "conflict": "[warn] {label}: shift+enter already bound to a different action - left as-is (remove it to let CC bind newline)",
    "skip-missing": "[skip] {label}: config not found - skipped",
    "skip-shape": "[warn] {label}: unexpected config shape - skipped (no change)",
}


def _report(label: str, result: str) -> None:
    print("  " + _MSG.get(result, "[warn] {label}: " + result).format(label=label))


#: Windows Terminal's settings.json, Store stable, then Preview, then unpackaged; the first found.
WINDOWS_TERMINAL_CANDIDATES = (
    "AppData/Local/Packages/Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/settings.json",
    "AppData/Local/Packages/Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe/LocalState/settings.json",
    "AppData/Local/Microsoft/Windows Terminal/settings.json",
)
#: VS Code's keybindings.json, stable then Insiders; the first whose User directory exists.
VSCODE_CANDIDATES = (
    "AppData/Roaming/Code/User/keybindings.json",
    "AppData/Roaming/Code - Insiders/User/keybindings.json",
)
NO_CONFIG = "  [skip] Shift+Enter binding: no Windows Terminal / VS Code config found"


class TerminalKeybindingsError(Exception):
    """A terminal config that could not be read, parsed or written, named by path."""


def discover(home: Path) -> tuple[Path | None, Path | None]:
    """The Windows Terminal settings file and the VS Code keybindings file install.sh chose."""
    terminal = next(
        (
            home / name
            for name in WINDOWS_TERMINAL_CANDIDATES
            if (home / name).is_file()
        ),
        None,
    )
    vscode = next(
        (home / name for name in VSCODE_CANDIDATES if (home / name).parent.is_dir()),
        None,
    )
    return terminal, vscode


def install(home: Path, environ: Mapping[str, str]) -> int:
    """The step: on a Windows shell, each discovered terminal config gains the binding once."""
    if not _is_windows_shell(environ):
        return 0
    terminal, vscode = discover(home)
    if terminal is None and vscode is None:
        print(NO_CONFIG)
        return 0
    for label, path, merge in (
        ("Windows Terminal", terminal, merge_windows_terminal),
        ("VS Code", vscode, merge_vscode),
    ):
        if path is None:
            continue
        try:
            result = merge(str(path))
        except (OSError, ValueError) as exc:
            raise TerminalKeybindingsError(
                f"{label} config {path} could not be updated: {exc}"
            ) from exc
        _report(label, result)
    return 0
