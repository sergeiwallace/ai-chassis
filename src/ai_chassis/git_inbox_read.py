"""`git-inbox read`: print a delivered message's full text and record the read (ISSUE-REF D-9).

The one implementation behind `git-inbox read`/`mark-read` and the git-inbox half of
`agent-mail-notifier read`/`mark-read`. A message is resolved from THIS machine's delivered ledger,
so only a message delivered here can be read, and only by a session it reached (or any session,
for a line on the machine notice). :func:`read` resolves each message's whole file from the blob
the ledger names and marks nothing; the caller prints it, then :func:`record` writes the read mark
(`read_marks.mark`). The daemon's next pass then settles the line, writes the `delivered` record
and the `read` receipt (D-10).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_chassis import read_marks

RUNG = "git-inbox"
VIA = "git-inbox read"
#: Why a read was refused; each is the step it stopped at.
REFUSED_LEDGER = "ledger"
REFUSED_MESSAGE = "message"
REFUSED_RECIPIENT = "recipient"
REFUSED_BODY = "body"


@dataclass(frozen=True)
class ReadOutcome:
    requested: str
    message_id: str = ""
    text: str = ""
    refused: str = ""
    detail: str = ""
    #: A read mark for this session already existed before this read (not yet settled).
    already_marked: bool = False
    has_actions: bool = False

    @property
    def ok(self) -> bool:
        return not self.refused


def _key(message_id: str) -> int:
    from ai_chassis.buzz_channel import event_message_id

    return int(event_message_id(message_id))


def resolve(view: Any, requested: str) -> str | None:
    """The envelope id ``requested`` names: the id itself, or the notifier's integer key for it."""
    if requested in view.files:
        return requested
    if requested.isdigit():
        wanted = int(requested)
        for message_id in sorted(view.files):
            if _key(message_id) == wanted:
                return message_id
    return None


def known_keys(state_root: Path, keys: list[int]) -> set[int]:
    """The integer ``keys`` that name a message in this machine's delivered ledger.

    A git-inbox key is a 48-bit digest (``buzz_channel.EVENT_ID_BITS``), so it does not meet a
    board id in practice; the ledger lookup, not the size, is what decides.
    """
    view = _view(state_root)
    if view is None:
        return set()
    return {key for key in keys if resolve(view, str(key)) is not None}


def _view(state_root: Path) -> Any | None:
    from ai_chassis import agent_mail_notifier as notifier
    from ai_chassis import git_inbox_watch as watch

    path = notifier.StateDir(state_root).git_inbox_ledger
    if not path.exists():
        # Nothing was ever delivered here. Taking the ledger lock would create its directory and
        # lock file, a state change a command must not make before it has resolved who runs it.
        return watch.LedgerView()
    try:
        return watch.Ledger(path).read()
    except watch.LedgerUnreadableError:
        return None


def read(
    *,
    state_root: Path,
    slug: str,
    requested: list[str],
    git: Callable[[list[str]], Any],
) -> list[ReadOutcome]:
    """One outcome per requested id. Marks only what it printed in full."""
    from ai_chassis import git_inbox
    from ai_chassis import git_inbox_watch as watch

    view = _view(state_root)
    if view is None:
        return [
            ReadOutcome(
                requested=r,
                refused=REFUSED_LEDGER,
                detail="this machine's delivered ledger is unreadable; nothing was read",
            )
            for r in requested
        ]
    outcomes = []
    for wanted in requested:
        message_id = resolve(view, wanted)
        if message_id is None:
            outcomes.append(
                ReadOutcome(
                    requested=wanted,
                    refused=REFUSED_MESSAGE,
                    detail=f"{wanted} was never delivered to this machine",
                )
            )
            continue
        reached = view.reached.get(message_id, set())
        if slug not in reached and watch.MACHINE_TARGET not in reached:
            outcomes.append(
                ReadOutcome(
                    requested=wanted,
                    message_id=message_id,
                    refused=REFUSED_RECIPIENT,
                    detail=f"{message_id} is not addressed to {slug}; nothing was printed",
                )
            )
            continue
        source, path, blob = view.files[message_id]
        shown = git(["cat-file", "blob", blob])
        if getattr(shown, "rc", 1) != 0 or not getattr(shown, "out", ""):
            outcomes.append(
                ReadOutcome(
                    requested=wanted,
                    message_id=message_id,
                    refused=REFUSED_BODY,
                    detail=f"blob {blob} of {source}:{path} is not in this checkout",
                )
            )
            continue
        text = shown.out
        has_actions = False
        with contextlib.suppress(git_inbox.EnvelopeError):
            envelope = git_inbox.parse_envelope(
                text + "\n", path=path, source=source, max_bytes=None
            )
            has_actions = bool(getattr(envelope, "actions", ()))
        outcomes.append(
            ReadOutcome(
                requested=wanted,
                message_id=message_id,
                text=text,
                has_actions=has_actions,
                already_marked=(slug, RUNG, _key(message_id))
                in read_marks.marks(state_root),
            )
        )
    return outcomes


def record(state_root: Path, slug: str, outcome: ReadOutcome) -> bool:
    """Write the read mark for an outcome the caller has ALREADY printed; True when new.

    Kept apart from :func:`read` so the order is the caller's to keep: print the text, flush it,
    then record. A print that raised (a closed stdout) never reaches this, so nothing is marked
    that the session was not shown.
    """
    if not outcome.ok:
        return False
    return read_marks.mark(
        state_root,
        slug=slug,
        rung=RUNG,
        key=_key(outcome.message_id),
        via=VIA,
        ledger_id=outcome.message_id,
    )


def render(outcome: ReadOutcome, slug: str) -> str:
    """The block a read prints: the message, then one line saying what the read recorded."""
    after = (
        f"git-inbox: read recorded for {slug}; it stays on your notice until its actions are "
        f"taken: git-inbox take {outcome.message_id}"
        if outcome.has_actions
        else f"git-inbox: read recorded for {slug}; the notice clears on the notifier's next pass"
    )
    return f"{outcome.text.rstrip()}\n\n{after}"
