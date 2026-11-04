"""Slack decoding and the first nonblank line's command grammar."""

import re
from dataclasses import dataclass

from ai_chassis.prompt_injection.payload import Payload, PromptPayload, sha256


@dataclass(frozen=True)
class ParsedReply:
    text: str
    command: dict | None


def command_line(text: str, *, bang_alias: bool = False) -> ParsedReply:
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        first = line.lstrip()
        if not first:
            continue
        if bang_alias and first.startswith("!"):
            first = "/" + first[1:]
        if not first.startswith("/"):
            break
        match = re.match(
            r"/([A-Za-z0-9][A-Za-z0-9:_-]*)(?:[^\S\r\n]+([^\r\n]*))?(\r?\n)?$", first
        )
        if match is None:
            raise ValueError("invalid command")
        tail = "".join(lines[index + 1 :])
        args = (match[2] or "") + ((match[3] or "") if tail else "") + tail
        return ParsedReply(first + tail, {"skill": match[1], "args": args})
    return ParsedReply(text, None)


def parse(
    text: str, *, mentions: dict[str, str] | None = None, bang_alias: bool = False
) -> ParsedReply:
    names = mentions or {}
    text = re.sub(
        r"<@([A-Za-z0-9]+)>", lambda match: names.get(match[1], match[0]), text
    )
    text = re.sub(r"<((?:https?://|mailto:)[^>|]+)(?:\|[^>]+)?>", r"\1", text)
    for escaped, value in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
        text = text.replace(escaped, value)
    return command_line(text, bang_alias=bang_alias)


def payload_for(record: dict) -> Payload:
    """Records are already unescaped. Preserve their arguments, including newlines."""
    parsed = command_line(record["text"])
    if parsed.command != record["command"]:
        raise ValueError("command-text-mismatch")
    mode = "multiline" if "\n" in parsed.text else "type"
    if parsed.command is None:
        return PromptPayload(parsed.text, mode=mode)
    args = parsed.command["args"].strip()
    return Payload(
        parsed.text,
        "slash-command",
        "/" + parsed.command["skill"],
        None,
        sha256(parsed.text),
        sha256(args) if args else None,
        mode,
    )
