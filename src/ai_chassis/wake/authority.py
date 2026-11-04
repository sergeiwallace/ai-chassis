"""One durable SID journal and causal reducer, independent of work/attempt lifetime."""

import json
from pathlib import Path

from . import sessions
from .events import Event, LIFECYCLE_KINDS, LIFECYCLE_INPUT_KINDS, digest, write_record

FACTS = LIFECYCLE_KINDS | {"process-exit"}
CANCELLATIONS = {"user-prompt", "precompact", "session-end", "process-exit"}
FIELDS = {
    "turn_generation",
    "lifecycle_sequence",
    "lifecycle_event",
    "closed_through_turn",
    "event_turn_active",
    "lifecycle_ended",
    "cancellation",
    "stop_owner",
}


class Authority:
    def __init__(self, state_dir):
        self.root = Path(state_dir)

    def observe(self, events, session):
        path = sessions.directory(self.root, session.slug) / "authority.json"
        binding = sessions.directory(self.root.parent, session.slug) / "binding.json"
        stamp = (
            json.loads(binding.read_text()).get("start_stamp")
            if binding.exists()
            else None
        )
        if path.exists():
            journal = json.loads(path.read_text())
            if (
                journal["session_id"] != session.session_id
                or journal.get("start_stamp") != stamp
            ):
                journal = {}
        else:
            journal = {}
        # A journal written before this check may already hold an earlier
        # incarnation's session-end under the current stamp; dropping it here
        # changes the fact set, so the state is folded again without it.
        facts = {
            identity: row
            for identity, row in journal.get("facts", {}).items()
            if not _predates_binding(row, stamp)
        }
        for event in events:
            if _predates_binding(event.as_record(), stamp):
                continue
            if (
                event.source == "process-exit"
                and stamp is not None
                and event.key
                == digest([session.slug, session.session_id, event.revision])
                and event.revision != str(stamp)
            ):
                # ProcessExitSource keys bind the revision to a recorded birth.
                # Other exit facts carry no birth; their arbitrary revision cannot
                # refute retirement (including when the binding is already gone).
                continue
            facts.setdefault(event.identity, event.as_record())
        if facts == journal.get("facts") and "state" in journal:
            return journal["state"]
        # A journal without a folded state is an interrupted reduction. Never reuse
        # the previous state's authority after new facts have been handed off.
        journal = {
            "session_id": session.session_id,
            "start_stamp": stamp,
            "facts": facts,
        }
        write_record(path, journal)
        state = reduce_facts([Event.from_record(row) for row in facts.values()])
        write_record(path, {**journal, "state": state})
        return state

    @staticmethod
    def allows(event, state):
        if state.get("lifecycle_ended"):
            return event.source in {"session-end", "process-exit"} and (
                event.identity == state.get("lifecycle_event")
            )
        if state.get("ambiguous"):
            return False
        if event.source in FACTS:
            return event.identity == state.get("lifecycle_event")
        if (
            event.source in LIFECYCLE_INPUT_KINDS
            and event.source != "compact-expiry"
            and state.get("event_turn_active")
        ):
            return False
        return True


def _predates_binding(record, stamp):
    """A session-end observed before this process started ended an earlier incarnation.

    A resumed or restarted session keeps its session id, and the spool replays the
    earlier incarnation's SessionEnd into a fresh journal. Session-end is absorbing,
    so without this check every later attempt of the live session is folded as
    SESSION_ENDED (ISSUE-REF). The binding's start_stamp and observed_at share the
    epoch clock, but intake epochs have whole-second precision, so an end inside the
    birth second cannot prove an older process and is kept. An undated fact, or a
    binding with no stamp, is kept as before: retirement still fails closed.
    """
    observed = record.get("observed_at") or 0
    return (
        record.get("source") == "session-end"
        and isinstance(stamp, (int, float))
        and isinstance(observed, (int, float))
        and 0 < observed < int(stamp)
    )


def sequence_ambiguous(facts):
    owners = {}
    for event in facts:
        if event.lifecycle_sequence:
            owners.setdefault(event.lifecycle_sequence, set()).add(event.identity)
    return any(len(values) > 1 for values in owners.values())


def reduce_facts(facts):
    """Fold complete immutable facts only. Observation clocks never confer ownership."""
    facts = [event for event in facts if event.source in FACTS]
    ambiguous = sequence_ambiguous(facts)
    state = {"ambiguous": True} if ambiguous else {}
    if ambiguous:
        # Conflicting nonterminal owners confer no authority. Independent SID
        # retirement remains absorbing, with the conflict retained in the journal.
        facts = [e for e in facts if e.source in {"session-end", "process-exit"}]
    for event in sorted(
        facts,
        key=lambda e: (
            e.source in {"session-end", "process-exit"},
            e.turn_generation,
            e.lifecycle_sequence,
            e.source in {"stop", "idle-prompt"},
            e.identity,
        ),
    ):
        if state.get("lifecycle_ended"):
            continue
        turn, sequence = (
            state.get("turn_generation", 0),
            state.get("lifecycle_sequence", 0),
        )
        if _superseded(event, state):
            continue
        state.update(
            turn_generation=max(turn, event.turn_generation),
            lifecycle_sequence=max(sequence, event.lifecycle_sequence),
            lifecycle_event=event.identity,
        )
        if event.source in CANCELLATIONS:
            state["cancellation"] = event.identity
            state.pop("stop_owner", None)
        if event.source == "user-prompt":
            state["event_turn_active"] = True
        elif event.source in {"stop", "idle-prompt"}:
            state["event_turn_active"] = False
            state["stop_owner"] = event.identity
        elif event.source in {"precompact", "session-end", "process-exit"}:
            state["closed_through_turn"] = max(turn, event.turn_generation)
        if event.source in {"session-end", "process-exit"}:
            state["lifecycle_ended"] = True
    return state


def _superseded(event, state):
    if event.source in {"session-end", "process-exit"}:
        return False  # Retirement is absorbing even when a later Stop was journaled.
    if event.turn_generation < state.get(
        "turn_generation", 0
    ) or event.lifecycle_sequence < state.get("lifecycle_sequence", 0):
        return True
    return (
        not event.lifecycle_sequence
        and event.source in {"stop", "idle-prompt"}
        and (state.get("event_turn_active") or "closed_through_turn" in state)
    )
