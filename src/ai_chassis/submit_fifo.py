"""The ai-cli-utils submit FIFO: how auto-compact reaches a bare session with no tmux pane.

ISSUE-REF, option (a). ai-cli-utils hosts a bare ``ai c`` session on a pty its
launcher owns and reads a per-session FIFO into that pty as typed input
(``src/ai_cli/pty_host.py`` in ai-cli-utils, ISSUE-REF). The contract this module
relies on, and nothing more:

* the FIFO is ``$XDG_STATE_HOME/ai-cli-utils/pty/<session>.in`` (``~/.local/state``
  when the variable is unset), created mode 0600 by the launcher, and its path is
  exported to the agent as ``AI_CLI_SUBMIT_FIFO``;
* ``<session>`` is the ai-cli session name, which the launcher also exports as
  ``CLAUDE_CODE_TASK_LIST_ID``;
* the launcher keeps a reader open for the life of the session, so a write with no
  reader means the launcher is gone;
* beside the FIFO, ``<session>.input-at`` holds the time of the operator's last
  relayed input (ISSUE-REF, ``pty_host.input_at_path``), and goes with the FIFO.

The FIFO carries input only. There is no screen to read back, so this module never
claims a submission landed; the caller confirms from the session's own transcript.

Standard library only: the context supervisor imports this from hook entry points.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import time
from pathlib import Path

SUBMIT_FIFO_ENV = "AI_CLI_SUBMIT_FIFO"
SESSION_NAME_ENV = "CLAUDE_CODE_TASK_LIST_ID"
#: Overrides the contract directory so tests never resolve a live session's FIFO.
FIFO_DIR_ENV = "AIH_SUBMIT_FIFO_DIR"
FIFO_MODE = 0o600


def contract_path(session: str) -> Path:
    """Where ai-cli-utils puts ``session``'s FIFO (``pty_host.submit_fifo_path``)."""
    override = os.environ.get(FIFO_DIR_ENV)
    if override:
        return Path(override) / f"{session}.in"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ai-cli-utils" / "pty" / f"{session}.in"


def discover() -> Path | None:
    """This process's submit FIFO path, or None when it is not a bare pty session.

    ``AI_CLI_SUBMIT_FIFO`` wins; it is exported only on the pty path, so its presence
    alone selects the FIFO and :func:`refusal` then judges what it names. Without it,
    the contract path counts only when something exists there.
    """
    named = os.environ.get(SUBMIT_FIFO_ENV)
    if named:
        return Path(named)
    session = os.environ.get(SESSION_NAME_ENV)
    if not session or "/" in session or session in {".", ".."}:
        return None
    candidate = contract_path(session)
    return candidate if os.path.lexists(candidate) else None


def _mode_refusal(info: os.stat_result) -> str | None:
    if not stat.S_ISFIFO(info.st_mode):
        return "fifo-not-a-fifo"
    if info.st_uid != os.geteuid():
        return "fifo-wrong-owner"
    if stat.S_IMODE(info.st_mode) != FIFO_MODE:
        return "fifo-wrong-mode"
    return None


def refusal(path: Path) -> str | None:
    """Why ``path`` must not be written, or None when it is a FIFO this user owns at 0600.

    ``lstat``, so a symlink is ``fifo-not-a-fifo`` rather than followed.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "fifo-missing"
    except OSError:
        return "fifo-unreadable"
    return _mode_refusal(info)


#: ``open(O_WRONLY | O_NONBLOCK)`` on a FIFO fails ENXIO when nothing reads it.
_OPEN_REFUSALS = {
    errno.ENXIO: "fifo-no-reader",
    errno.ENOENT: "fifo-missing",
    errno.ELOOP: "fifo-not-a-fifo",
}


def _write(fd: int, data: bytes) -> str | None:
    try:
        written = os.write(fd, data)
    except BlockingIOError:
        return "fifo-write-error"
    except OSError as error:
        return "fifo-no-reader" if error.errno == errno.EPIPE else "fifo-write-error"
    return None if written == len(data) else "fifo-write-error"


def submit(
    path: Path,
    text: str,
    *,
    enter_delay_seconds: float,
) -> str | None:
    """Write ``text``, wait, then a lone carriage return: None on success, else a reason.

    The text and the Enter are two writes for the reason the tmux path sends Enter in
    its own call (ISSUE-REF): bundled, Enter can reach the composer before it has
    taken the text. Each write is under ``PIPE_BUF``, so it is atomic.

    The descriptor is re-checked with ``fstat`` after the open, so a path swapped
    between :func:`refusal` and the open is still refused.
    """
    reason = refusal(path)
    if reason is not None:
        return reason
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as error:
        return _OPEN_REFUSALS.get(error.errno or 0, "fifo-write-error")
    try:
        reason = _mode_refusal(os.fstat(fd)) or _write(fd, text.encode("utf-8"))
        if reason is not None:
            return reason
        time.sleep(max(0.0, enter_delay_seconds))
        return _write(fd, b"\r")
    finally:
        os.close(fd)


def write_part(path: Path, data: bytes) -> str | None:
    """One checked nonblocking write for the event-driven text/Enter states."""
    reason = refusal(path)
    if reason is not None:
        return reason
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as error:
        return _OPEN_REFUSALS.get(error.errno or 0, "fifo-write-error")
    try:
        return _mode_refusal(os.fstat(fd)) or _write(fd, data)
    finally:
        os.close(fd)


#: Exactly what ai-cli-utils' ``pty_host._record_input`` writes: ``f"{t:.6f}\n"``.
_INPUT_AT_LINE = re.compile(rb"[0-9]+\.[0-9]{6}\n")
_INPUT_AT_MAX_BYTES = 64


def input_at_path(fifo: Path) -> Path | None:
    """The launcher's last-input record beside ``fifo`` (``pty_host.input_at_path``).

    ``<session>.in`` pairs with ``<session>.input-at``; a FIFO not named that way has
    no record this module can find.
    """
    if fifo.suffix != ".in" or not fifo.stem:
        return None
    return fifo.with_name(f"{fifo.stem}.input-at")


_INPUT_AT_OPEN_REFUSALS = {
    errno.ENOENT: "input-at-missing",
    errno.ELOOP: "input-at-not-regular",
}


def _input_at_refusal(info: os.stat_result) -> str | None:
    if not stat.S_ISREG(info.st_mode):
        return "input-at-not-regular"
    if info.st_uid != os.geteuid():
        return "input-at-wrong-owner"
    if stat.S_IMODE(info.st_mode) != FIFO_MODE:
        return "input-at-wrong-mode"
    return None


def _read_input_at(path: Path) -> tuple[bytes, str | None]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        return b"", _INPUT_AT_OPEN_REFUSALS.get(error.errno or 0, "input-at-unreadable")
    try:
        reason = _input_at_refusal(os.fstat(fd))
        if reason is not None:
            return b"", reason
        return os.read(fd, _INPUT_AT_MAX_BYTES + 1), None
    except OSError:
        return b"", "input-at-unreadable"
    finally:
        os.close(fd)


def last_operator_input(fifo: Path) -> tuple[float | None, str | None]:
    """When the operator last typed into ``fifo``'s session: (time, None) or (None, why).

    ISSUE-REF. ai-cli-utils' launcher writes the time of every operator keystroke
    chunk it relays to the agent into ``<session>.input-at`` (one line, epoch seconds
    to six places, renamed over the path, mode 0600), and writes the launch time there
    before the agent starts. FIFO submissions never move it, so it measures the human
    alone. The terminal's own access time is not a substitute: Linux moves it in
    8-second steps, which hid a draft typed seconds after a submit.

    Held to the FIFO's own rules: a regular file (``O_NOFOLLOW``, so a symlink is
    refused), this user's, mode 0600, all checked by ``fstat`` on the open descriptor.
    """
    path = input_at_path(fifo)
    if path is None:
        return None, "input-at-unnamed"
    data, reason = _read_input_at(path)
    if reason is not None:
        return None, reason
    if not _INPUT_AT_LINE.fullmatch(data):
        return None, "input-at-malformed"
    return float(data), None
