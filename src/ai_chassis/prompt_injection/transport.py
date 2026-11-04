"""How text reaches the composer: staged behind a fingerprint fence, then Enter in
its own tmux call (D-2)."""

from __future__ import annotations

import shlex
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TextIO

from ai_chassis.prompt_injection import _tmux
from ai_chassis.prompt_injection.payload import Payload
from ai_chassis.prompt_injection.targets import SessionTarget


@dataclass
class StageProgress:
    """How far the last ``stage`` got: ``keystrokes`` counts the pane-writing calls
    that landed, so a non-zero count on a failed stage means a partial draft."""

    lines: int
    chunks: int
    lines_typed: int = 0
    keystrokes: int = 0


class Transport(ABC):
    #: Whether ``submit`` can make the session run the text as a turn.
    submits = True
    #: Set by every ``stage`` call that writes to a pane.
    progress: StageProgress | None = None

    @abstractmethod
    def stage(
        self, target: SessionTarget, payload: Payload, *, chunk_pause_seconds: float = 0
    ) -> str:
        """Put the text in the composer without Enter: a ``send_result`` value.
        ``chunk_pause_seconds`` is the pause after each ``multiline`` chunk and
        soft newline."""

    @abstractmethod
    def submit(self, target: SessionTarget) -> bool: ...

    def press(self, _target: SessionTarget, _key: str, *, count: int = 1) -> str:  # noqa: ARG002
        """Send one tmux key name ``count`` times behind the fence: a ``send_result``
        value. A channel with no keys reports ``unsupported``."""
        return "unsupported"

    @abstractmethod
    def capture(self, target: SessionTarget) -> str: ...

    def warn(self, _target: SessionTarget, _message: str) -> None:
        """Tell the human at the pane something without touching the composer."""
        return None


class TmuxTransport(Transport):
    """``TmuxAdapter.fence_and_stage`` / ``send_enter`` (5814-5856).

    ADDED over the legacy shape: ``send-keys -l ... --``. Measured 2026-10-10 on
    tmux 3.7c: without ``-l`` a payload that starts with ``-`` is refused as an
    unknown flag and the text ``Enter`` is pressed as the key; with it, quotes,
    ``;``, ``$HOME``, ``#{...}`` and ``##`` all round-trip byte for byte.

    Empty ``-c`` keeps pane delivery independent of tmux's implicit current
    client, which may be the read-only notification observer. The target pane
    and its fingerprint remain the staging authority.
    """

    def __init__(self, tmux_command: _tmux.Command = _tmux.DEFAULT_COMMAND) -> None:
        self.tmux_command = tmux_command

    def _fenced(self, target: SessionTarget, inner: str) -> str:
        predicate = f"#{{==:{_tmux.fingerprint_format(self.tmux_command)},{target.fingerprint}}}"
        result = _tmux.run(
            self.tmux_command,
            [
                "if-shell",
                "-F",
                "-t",
                target.pane_id,
                predicate,
                inner,
                f"display-message -p {_tmux.FENCE_MISMATCH}",
            ],
        )
        if result is None:
            return "text-send-error"
        if _tmux.FENCE_MISMATCH in result.stdout:
            return "fence-mismatch"
        return "succeeded" if result.returncode == 0 else "text-send-error"

    def stage(self, target, payload, *, chunk_pause_seconds=0):
        pane = shlex.quote(target.pane_id)
        send = shlex.join(_tmux.send_keys_command(self.tmux_command))
        progress = self.progress = StageProgress(
            lines=payload.line_count, chunks=payload.chunk_count
        )
        if payload.mode == "type":
            result = self._fenced(
                target, f"{send} -l -t {pane} -- {shlex.quote(payload.text)}"
            )
            if result == "succeeded":
                progress.lines_typed = progress.keystrokes = 1
            return result
        return self._stage_lines(target, pane, payload.text, chunk_pause_seconds)

    def _stage_lines(
        self, target: SessionTarget, pane: str, text: str, pause: float
    ) -> str:
        """``multiline``: T-1.7 arm A. One fenced ``send-keys -l`` per line, a fenced
        ``S-Enter`` soft newline between lines, a pause after each. The first
        failed call stops typing, so a fence mismatch mid-payload leaves a partial
        draft and presses nothing further."""
        progress = self.progress
        assert progress is not None
        send = shlex.join(_tmux.send_keys_command(self.tmux_command))
        for index, line in enumerate(text.split("\n")):
            keys = [f"{send} -t {pane} S-Enter"] if index else []
            if line:
                keys.append(f"{send} -l -t {pane} -- {shlex.quote(line)}")
            for inner in keys:
                result = self._fenced(target, inner)
                if result != "succeeded":
                    return result
                progress.keystrokes += 1
                time.sleep(pause)
            progress.lines_typed = index + 1
        return "succeeded"

    def press(self, target, key, *, count=1):
        pane = shlex.quote(target.pane_id)
        send = shlex.join(_tmux.send_keys_command(self.tmux_command))
        return self._fenced(
            target, f"{send} -N {int(count)} -t {pane} {shlex.quote(key)}"
        )

    def submit(self, target):
        # Enter MUST be its own tmux call (ISSUE-REF): bundled with the text, a long
        # command's Enter can arrive before the composer processed it.
        result = _tmux.run(
            self.tmux_command,
            [
                *_tmux.send_keys_command(self.tmux_command),
                "-t",
                target.pane_id,
                "Enter",
            ],
        )
        return result is not None and result.returncode == 0

    def warn(self, target, message):
        # TmuxAdapter._status_warning: the status line, never the composer.
        _tmux.run(
            self.tmux_command,
            [
                "display-message",
                "-t",
                target.pane_id,
                "-d",
                str(_tmux.PANE_WARNING_DISPLAY_MS),
                message,
            ],
        )

    def capture(self, target):
        result = _tmux.run(
            self.tmux_command, ["capture-pane", "-p", "-t", target.pane_id]
        )
        return result.stdout if result is not None else ""


class ContextTransport(Transport):
    """The non-submitting channel: a ``<system-reminder>`` block on the calling
    hook's stdout, read by the model on its next turn. It cannot start a turn or
    run a slash command, so ``submit`` always reports False."""

    submits = False

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream

    def stage(self, _target, payload, *, chunk_pause_seconds=0):  # noqa: ARG002
        stream = self.stream or sys.stdout
        try:
            stream.write(f"<system-reminder>\n{payload.text}\n</system-reminder>\n")
            stream.flush()
        except OSError:
            return "text-send-error"
        return "succeeded"

    def submit(self, _target):
        return False

    def capture(self, _target):
        return ""
