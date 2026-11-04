"""Regression tests for projects-wide-session-config/apply-terminal-keybindings.py.

Root cause of the bug these guard (ISSUE-REF): ``merge_vscode`` decided a binding
was already correct by matching only ``key`` + ``command``, never comparing
``args.text``. So any pre-existing ``shift+enter`` -> ``sendSequence`` binding —
including one carrying a *wrong* escape sequence — returned ``"present"``, the
installer reported success, and the broken value was never repaired. Running the
installer could not fix a machine whose keybinding sent the wrong bytes.

Which sequence is correct was verified by reading the Claude Code 2.1.220 binary
directly (see docs/research/claude-code-shift-enter-key-encoding.md):

* ``/terminal-setup`` and Claude Code's own Remote-SSH instructions both write
  ``args.text = ESC CR`` (JSON ``\\u001b\\r``) for VS Code.
* The key decoder maps ``ESC CR`` -> ``{name:"return", meta:true}`` and
  ``ESC [13;2u`` -> ``{name:"return", shift:true}`` (regex
  ``/^\\x1b\\[(\\d+)(?:;(\\d+))?u/``, modifier decode ``wfs``, keyname ``bFu``).
* ``U3o`` — the table of terminals Claude Code treats as natively sending CSI-u —
  is ``{ghostty, kitty, iTerm.app, WezTerm, WarpTerminal, windows-terminal}``.
  ``vscode`` is deliberately absent, and ``shouldOfferTerminalSetup`` lists
  ``vscode`` as needing the written binding.

So the two terminals need *different* encodings, which is why one shared constant
was wrong: Windows Terminal is a native-CSI-u terminal and must emit CSI-u, while
the VS Code integrated terminal gets ESC CR, matching what Claude Code itself
installs. The invariant: an existing binding whose ``args.text`` differs from the
desired value is REPAIRED, and a run against an already-correct file is a no-op.
"""

import importlib
import json
import sys
from pathlib import Path

import pytest

SESSION_CFG = Path(__file__).parent.parent / "projects-wide-session-config"
sys.path.insert(0, str(SESSION_CFG))

atk = importlib.import_module("apply-terminal-keybindings")

merge_vscode = atk.merge_vscode
merge_windows_terminal = atk.merge_windows_terminal
VSCODE_BINDING = atk.VSCODE_BINDING
WT_INPUT = atk.WT_INPUT

# The sequence Claude Code's own /terminal-setup writes for VS Code.
CC_VSCODE_TEXT = "\x1b\r"
# The CSI-u form: correct for Windows Terminal, wrong for the VS Code binding.
CSI_U_TEXT = "\x1b[13;2u"


def _binding(text, when="terminalFocus"):
    return {
        "key": "shift+enter",
        "command": "workbench.action.terminal.sendSequence",
        "args": {"text": text},
        "when": when,
    }


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _vscode_bindings(path):
    return [b for b in _read(path) if b.get("key") == "shift+enter"]


# ---- the encoding constants match the verified Claude Code behavior -----------


def test_vscode_binding_given_cc_terminal_setup_when_compared_then_uses_esc_cr():
    """VS Code is NOT a native-CSI-u terminal; it needs the ESC CR that CC writes."""
    assert VSCODE_BINDING["args"]["text"] == CC_VSCODE_TEXT


def test_wt_input_given_native_csiu_terminal_when_compared_then_uses_csi_u():
    """Windows Terminal is in CC's native-CSI-u table, so it must emit CSI-u."""
    assert WT_INPUT == CSI_U_TEXT


# ---- the repair path (the actual bug) ----------------------------------------


def test_vscode_given_stale_csi_u_text_when_merged_then_repaired_to_esc_cr(tmp_path):
    """A wrong args.text must be REPAIRED, not blessed as already present."""
    path = tmp_path / "keybindings.json"
    path.write_text(json.dumps([_binding(CSI_U_TEXT)]), encoding="utf-8")

    result = merge_vscode(str(path))

    bindings = _vscode_bindings(path)
    assert len(bindings) == 1, "repair must not append a duplicate shift+enter binding"
    assert bindings[0]["args"]["text"] == CC_VSCODE_TEXT
    assert result == "repaired"


def test_vscode_given_arbitrary_wrong_text_when_merged_then_repaired(tmp_path):
    path = tmp_path / "keybindings.json"
    path.write_text(json.dumps([_binding("\x1b\n")]), encoding="utf-8")

    assert merge_vscode(str(path)) == "repaired"
    assert _vscode_bindings(path)[0]["args"]["text"] == CC_VSCODE_TEXT


def test_vscode_given_missing_args_when_merged_then_repaired(tmp_path):
    """A sendSequence binding with no args at all is malformed, not 'present'."""
    path = tmp_path / "keybindings.json"
    broken = {
        "key": "shift+enter",
        "command": "workbench.action.terminal.sendSequence",
        "when": "terminalFocus",
    }
    path.write_text(json.dumps([broken]), encoding="utf-8")

    assert merge_vscode(str(path)) == "repaired"
    assert _vscode_bindings(path)[0]["args"]["text"] == CC_VSCODE_TEXT


# ---- idempotence: a correct file must be left byte-identical -----------------


def test_vscode_given_correct_binding_when_merged_then_present_and_no_write(tmp_path):
    """Idempotent means no-op: an already-correct file is not rewritten (mtime/inode
    stable), per the artifact-writes rule — an unnecessary write is a live event."""
    path = tmp_path / "keybindings.json"
    path.write_text(
        json.dumps([_binding(CC_VSCODE_TEXT)], indent=4) + "\n", encoding="utf-8"
    )
    before = path.stat()
    before_bytes = path.read_bytes()

    assert merge_vscode(str(path)) == "present"

    after = path.stat()
    assert path.read_bytes() == before_bytes
    assert (after.st_mtime_ns, after.st_ino) == (before.st_mtime_ns, before.st_ino)


def test_vscode_given_repaired_file_when_merged_again_then_no_op(tmp_path):
    """Second run after a repair must be a clean no-op."""
    path = tmp_path / "keybindings.json"
    path.write_text(json.dumps([_binding(CSI_U_TEXT)]), encoding="utf-8")
    assert merge_vscode(str(path)) == "repaired"

    before = path.stat()
    assert merge_vscode(str(path)) == "present"
    after = path.stat()
    assert (after.st_mtime_ns, after.st_ino) == (before.st_mtime_ns, before.st_ino)


def test_wt_given_correct_config_when_merged_then_present_and_no_write(tmp_path):
    path = tmp_path / "settings.json"
    atk._write_json(
        str(path),
        {
            "actions": [
                {
                    "command": {"action": "sendInput", "input": WT_INPUT},
                    "id": atk.WT_ACTION_ID,
                }
            ],
            "keybindings": [{"id": atk.WT_ACTION_ID, "keys": "shift+enter"}],
        },
    )
    before = path.stat()

    assert merge_windows_terminal(str(path)) == "present"

    after = path.stat()
    assert (after.st_mtime_ns, after.st_ino) == (before.st_mtime_ns, before.st_ino)


def test_wt_given_stale_input_when_merged_then_repaired(tmp_path):
    """Windows Terminal's sendInput text is subject to the same staleness bug."""
    path = tmp_path / "settings.json"
    atk._write_json(
        str(path),
        {
            "actions": [
                {
                    "command": {"action": "sendInput", "input": "\x1b\r"},
                    "id": atk.WT_ACTION_ID,
                }
            ],
            "keybindings": [{"id": atk.WT_ACTION_ID, "keys": "shift+enter"}],
        },
    )

    assert merge_windows_terminal(str(path)) == "repaired"
    obj = _read(path)
    action = next(a for a in obj["actions"] if a.get("id") == atk.WT_ACTION_ID)
    assert action["command"]["input"] == WT_INPUT


# ---- preserved behavior: add, create, conflict, skip -------------------------


def test_vscode_given_empty_file_when_merged_then_binding_added(tmp_path):
    path = tmp_path / "keybindings.json"
    path.write_text("[]", encoding="utf-8")

    assert merge_vscode(str(path)) == "added"
    assert _vscode_bindings(path)[0]["args"]["text"] == CC_VSCODE_TEXT


def test_vscode_given_absent_file_in_existing_dir_when_merged_then_created(tmp_path):
    path = tmp_path / "keybindings.json"

    assert merge_vscode(str(path)) == "added"
    assert _vscode_bindings(path)[0]["args"]["text"] == CC_VSCODE_TEXT


def test_vscode_given_jsonc_comments_when_merged_then_parsed_and_repaired(tmp_path):
    """The live profile file carries // comments; JSONC must still parse."""
    path = tmp_path / "keybindings.json"
    path.write_text(
        "// a comment\n[\n  // another\n" + json.dumps(_binding(CSI_U_TEXT)) + "\n]\n",
        encoding="utf-8",
    )

    assert merge_vscode(str(path)) == "repaired"
    assert _vscode_bindings(path)[0]["args"]["text"] == CC_VSCODE_TEXT


def test_vscode_given_foreign_shift_enter_binding_when_merged_then_preserved(tmp_path):
    """A user's unrelated shift+enter binding must survive the repair."""
    path = tmp_path / "keybindings.json"
    foreign = {"key": "shift+enter", "command": "editor.action.insertLineAfter"}
    path.write_text(json.dumps([foreign, _binding(CSI_U_TEXT)]), encoding="utf-8")

    merge_vscode(str(path))

    assert foreign in _read(path)


def test_vscode_given_missing_parent_dir_when_merged_then_skipped(tmp_path):
    path = tmp_path / "nope" / "keybindings.json"
    assert merge_vscode(str(path)) == "skip-missing"
    assert not path.exists()


def test_vscode_given_non_list_shape_when_merged_then_skipped_without_write(tmp_path):
    path = tmp_path / "keybindings.json"
    path.write_text('{"not": "a list"}', encoding="utf-8")
    before = path.read_bytes()

    assert merge_vscode(str(path)) == "skip-shape"
    assert path.read_bytes() == before


def test_wt_given_missing_file_when_merged_then_skipped(tmp_path):
    assert merge_windows_terminal(str(tmp_path / "settings.json")) == "skip-missing"


def test_wt_given_foreign_shift_enter_keybinding_when_merged_then_conflict(tmp_path):
    path = tmp_path / "settings.json"
    atk._write_json(
        str(path),
        {"actions": [], "keybindings": [{"id": "User.other", "keys": "shift+enter"}]},
    )

    assert merge_windows_terminal(str(path)) == "conflict"


# ---- every result the mergers can return has a user-facing message ----------


def test_messages_given_all_merge_results_when_reported_then_each_has_a_message():
    for result in (
        "added",
        "present",
        "repaired",
        "conflict-added",
        "conflict",
        "skip-missing",
        "skip-shape",
    ):
        assert result in atk._MSG, f"no _MSG entry for merge result {result!r}"


# ---- the JSONC scanner, arm by arm (ISSUE-REF) --------------------------------
#
# `_strip_jsonc` is a hand-written scanner over four sub-languages (string
# literals, `//` comments, `/* */` comments, everything else), and until now only
# one coarse test reached it -- through `merge_vscode`, with `//` comments only.
# Its C901 per-file-ignore was removed by splitting the scanner into one helper per
# sub-language, so each arm needs a case that fails if that arm's handling drifts.
# These are characterizations: every expectation below was measured against the
# pre-split implementation first.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # `//` comments: to end of line, and to end of input.
        ('{"a": 1, // note\n "b": 2}', '{"a": 1, \n "b": 2}'),
        ('{"a": 1} // trailing', '{"a": 1} '),
        # A bare CR ends a line comment too: these files come off Windows.
        ('{"a": 1, // note\r\n "b": 2}', '{"a": 1, \r\n "b": 2}'),
        ('{"a": 1, // note\r "b": 2}', '{"a": 1, \r "b": 2}'),
        # `/* */` comments, including one nobody ever closed.
        ('{/* one\ntwo */ "a": 1}', '{ "a": 1}'),
        ('{"a": 1 /* never closed', '{"a": 1 '),
        # A lone slash is not a comment opener.
        ('{"a": 1}/', '{"a": 1}/'),
        # Inside a string literal, nothing is a comment.
        ('{"u": "http://x/y"}', '{"u": "http://x/y"}'),
        ('{"u": "/* not a comment */"}', '{"u": "/* not a comment */"}'),
        ('{"a": "1/2"}', '{"a": "1/2"}'),
        # Escapes are consumed in pairs, so an escaped quote does not close the
        # literal and an escaped backslash does not escape the quote after it.
        #
        # The first three below are NOT discriminating on their own -- measured:
        # deleting the escape handling entirely leaves their output byte-identical,
        # because nothing in them is eligible for REMOVAL either way. The escape
        # state only becomes observable when misjudging where a literal ends puts a
        # `//`, a `/* */` or a `,` on the wrong side of the boundary, which is what
        # the four cases after them do. They are kept as shape documentation, not
        # as the guard.
        ('{"a": "x\\"y"}', '{"a": "x\\"y"}'),
        ('{"a": "x\\\\"}', '{"a": "x\\\\"}'),
        ('{"a": "x\\\\// y"}', '{"a": "x\\\\// y"}'),
        # An escaped quote does not end the literal, so this `//` is string content.
        # Lose the pairing and the tail is eaten as a line comment.
        ('{"a": "x\\" // y"}', '{"a": "x\\" // y"}'),
        ('{"a": "x\\" /* z */ y"}', '{"a": "x\\" /* z */ y"}'),
        ('"\\"/*', '"\\"/*'),
        # An ODD number of backslashes DOES escape the quote after them, so the
        # literal continues and the trailing `//` is a real comment. Treat `\\` as
        # escaping the quote and the `//` survives instead.
        ('{"a": "\\\\\\"}, "b": "//"}', '{"a": "\\\\\\"}, "b": "'),
        # An unterminated literal is copied through rather than raising.
        ('{"a": "x', '{"a": "x'),
        # The Windows Terminal payload must survive verbatim.
        ('{"t": "\\u001b[13;2u"}', '{"t": "\\u001b[13;2u"}'),
        # Trailing commas before a closer are dropped, across a newline too.
        ('{"a": 1,}', '{"a": 1}'),
        ("[1, 2,]", "[1, 2]"),
        ('{"a": 1,\n}', '{"a": 1\n}'),
    ],
)
def test_strip_jsonc_given_each_scanner_arm_when_stripped_then_matches_the_measured_output(
    raw, expected
):
    assert atk._strip_jsonc(raw) == expected


def test_strip_jsonc_given_a_comma_closer_inside_a_string_then_still_drops_it():
    """A DOCUMENTED limitation, pinned so a refactor cannot change it unnoticed.

    The trailing-comma pass is a regex over the whole stripped text, so it also
    fires inside string literals -- the function's own comment says as much
    ("comment stripping already removed the string-preserving concern for the
    common cases in these config files").  `",}"` is therefore corrupted to `"}"`.
    That is pre-existing behaviour, out of scope here, and pinned rather than
    quietly altered: if a later change makes the pass string-aware, this test is
    the one that should be updated deliberately.
    """
    assert atk._strip_jsonc('{"a": ",}"}') == '{"a": "}"}'


def test_strip_jsonc_given_the_live_vscode_shape_when_stripped_then_json_parses():
    raw = (
        "// user keybindings\n"
        "[\n"
        '  { "key": "shift+enter", /* CC */ "args": { "text": "\\u001b\\r" } },\n'
        "]\n"
    )
    assert json.loads(atk._strip_jsonc(raw)) == [
        {"key": "shift+enter", "args": {"text": "\x1b\r"}}
    ]
