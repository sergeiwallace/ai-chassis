"""The one rule for whether a raw string may name a session (ISSUE-REF, ISSUE-REF).

Shared by ``context_supervisor`` and the ``prompt_injection`` library, so a key reaching a
log line or a file path is judged the same way on both sides. A leaf module, standard
library only: importing it costs nothing on a hook's hot path and keeps the library's
import boundary (``tests/prompt_injection/test_import_boundary.py``).
"""

from __future__ import annotations

# C0 controls end below SPACE; DEL and the C1 controls run from 0x7F to below NBSP.
_C0_END = 0x20
_DEL = 0x7F
_C1_END = 0xA0


def refusal(key: str) -> str | None:
    """Why ``key`` cannot name a session, or ``None`` when it can.

    ``resume_derive_key()`` in .claude/bin/resume-lib.sh refuses a key holding
    ``/``, ``\\`` or ``..``. Control characters are refused here too: bash
    rewrites them to ``_``, but a raw Python key carried a newline straight into
    a ``resume.log`` line (ISSUE-REF). The two rules are pinned together in
    tests/hooks/test_supervisor_lifecycle_key_parity.py.
    """
    if "/" in key or "\\" in key:
        return "contains a path separator"
    if ".." in key:
        return "contains '..'"
    if any(ord(char) < _C0_END or _DEL <= ord(char) < _C1_END for char in key):
        return "contains a control character"
    return None
