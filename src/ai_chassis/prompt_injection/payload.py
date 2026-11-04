"""What gets delivered: the exact text, its kind, and the digests that let a
transcript record be attributed to the attempt that sent it."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

Kind = Literal["slash-command", "prompt"]
Mode = Literal["type", "multiline"]
MODES = ("type", "multiline")


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _split_command(text: str) -> tuple[str, str]:
    # Any whitespace ends the command token: `/goal` followed by a newline is still
    # `/goal`, so a multi-line condition reaches PayloadTypable instead of failing
    # command resolution as a nonsense command name.
    parts = text.strip().split(maxsplit=1)
    return parts[0], parts[1] if len(parts) > 1 else ""


@dataclass(frozen=True)
class Payload:
    text: str
    kind: Kind
    command: str | None
    sentinel: str | None
    digest: str
    args_digest: str | None = None
    mode: Mode = "type"

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if not self.text.strip():
            raise ValueError("payload text is empty")

    @property
    def line_count(self) -> int:
        return self.text.count("\n") + 1

    @property
    def chunk_count(self) -> int:
        """The ``send-keys -l`` calls staging takes: one per non-empty line (a
        ``type`` payload is one line)."""
        return sum(1 for line in self.text.split("\n") if line)


@dataclass(frozen=True, init=False)
class SlashCommandPayload(Payload):
    """``SlashCommandPayload("/compact")`` or ``SlashCommandPayload("/goal", args)``.

    The transcript's ``<command-args>`` is matched against ``args_digest`` so a
    landing is attributed to THIS condition, not any ``/goal`` a human typed.
    """

    def __init__(self, command: str, args: str | None = None, *, mode: Mode = "type"):
        if args is None:
            command, args = _split_command(command)
        command = command.strip()
        if not command.startswith("/") or command == "/" or " " in command:
            raise ValueError(f"not a slash command: {command!r}")
        args = args.strip()
        text = f"{command} {args}" if args else command
        Payload.__init__(
            self,
            text=text,
            kind="slash-command",
            command=command,
            sentinel=None,
            digest=sha256(text),
            args_digest=sha256(args) if args else None,
            mode=mode,
        )

    @classmethod
    def goal(cls, condition: str, *, mode: Mode = "type") -> SlashCommandPayload:
        return cls("/goal", condition, mode=mode)


@dataclass(frozen=True, init=False)
class PromptPayload(Payload):
    """A plain prompt. ``sentinel``, when given, must appear in the text: it is
    how a ``prompt-record`` landing is told apart from any other user turn."""

    def __init__(self, text: str, sentinel: str | None = None, *, mode: Mode = "type"):
        if sentinel is not None and sentinel not in text:
            raise ValueError("the sentinel must appear in the prompt text")
        Payload.__init__(
            self,
            text=text,
            kind="prompt",
            command=None,
            sentinel=sentinel,
            digest=sha256(text),
            mode=mode,
        )
