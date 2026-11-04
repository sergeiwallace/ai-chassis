"""Admission, passive metadata and claim-aware snapshot delivery."""

from abc import ABC, abstractmethod
from enum import StrEnum
import json
from pathlib import Path

from ai_chassis import idle_wake

from .events import Event, LIFECYCLE_INPUT_KINDS, digest, sync_directory, write_record
from .route import Receipt
from .prompts import PromptRefused, projection, writer
from .targets import MachineTarget, safe_identity


class Disposition(StrEnum):
    WAKE = "wake"
    PASSIVE = "passive"
    DROP = "drop"


class Admission(ABC):
    @abstractmethod
    def admit(self, event, session) -> Disposition:
        """Apply admission without changing the resolved recipient set."""


class IdleWakeAdmission(Admission):
    def admit(self, event, session):
        adapted = idle_wake.Event(
            source=event.source,
            key=event.key,
            revision=event.revision,
            recipients=(session.slug,),
            scope="machine" if isinstance(event.target, MachineTarget) else event.scope,
            requires_action=event.requires_action,
            terminal_condition="source-terminal",
        )
        result = idle_wake.classify(adapted).for_session(session.slug)
        if result == "wake" and not idle_wake.load_policy().automatic_wakes:
            result = "passive"
        return Disposition.DROP if result == "never" else Disposition(result)


class DeliveryChannel(ABC):
    @abstractmethod
    def deliver(self, session, batch) -> Receipt:
        """Deliver idempotently by event identity; a replay must not repeat effects."""


class MetadataChannel(DeliveryChannel):
    """Private passive metadata, never a model invocation or a shared watched file.

    Automatic turns use the snapshot channel; passive metadata never acquires a
    wake claim or changes a watched readiness path.
    """

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)

    def deliver(self, session, batch):
        root = self.state_dir / "sessions" / safe_identity(session.slug)
        if root.is_symlink() or (root / "passive").is_symlink():
            return Receipt(False, "symlink-target")
        if (self.state_dir / "sessions").is_symlink():
            return Receipt(False, "symlink-target")
        for event in batch:
            write_record(root / "passive" / f"{event.identity}.json", event.as_record())
        return Receipt(True)


class SnapshotWake(DeliveryChannel):
    """Publish admitted work through the existing immutable snapshot/claim protocol.

    The dispatch ledger retains outstanding items across individual deliveries
    and restarts. Hook consumers share their existing per-event claims and shown
    receipts; publishing a replay never invents another wake identity.
    """

    def __init__(self, state_dir, dispatcher):
        self.root = Path(state_dir).parent
        self.dispatcher = dispatcher
        self.repairs = Path(state_dir) / "snapshot-repairs"

    def deliver(self, session, batch):
        self.reconcile()
        return self._publish(session, batch)

    def _publish(self, session, batch):  # noqa: PLR0911 - fail-closed registration, payload and publication refusals
        from ai_chassis import idle_wake_snapshots as snapshots
        from ai_chassis.agent_mail_notifier import write_if_changed
        from . import sessions

        if not idle_wake.load_policy().automatic_wakes:
            return Receipt(False, "automatic-wakes-disabled")
        try:
            target = sessions.directory(self.root, session.slug)
            binding = json.loads((target / "binding.json").read_text())
            if binding["session_id"] != session.session_id or not sessions.registered(
                self.root, session.slug, session.session_id
            ):
                return Receipt(False, "watch-registration-pending")
            if (
                snapshots.registered(self.root, slug=session.slug).get(session.slug)
                != session.session_id
            ):
                return Receipt(False, "snapshot-registration-pending")
            paths = (
                self.root / "delivery-seen",
                self.root / "sessions" / f"{session.slug}.snapshots",
            )
            if any(path.is_symlink() for path in paths):
                return Receipt(False, "symlink-target")
            paths[0].mkdir(parents=True, exist_ok=True, mode=0o700)
            items = self._items(session, batch)
            items, prompts = projection(
                items, self.dispatcher.obligations, admit=self._admit_prompt
            )

            blocked = snapshots.publish(
                self.root,
                session.slug,
                session.session_id,
                digest(items),
                items,
                (0, 0),
                writer(write_if_changed, prompts),
                wake_gains={item[0] for item in items},
            )
            if batch:
                receipts = {snapshots.digest(event.identity) for event in batch}
                # Outstanding older items share this snapshot, but cannot refuse
                # a newly admitted batch whose own publication is not blocked.
                blocked = [row for row in blocked if row["receipt"] in receipts]
            return Receipt(
                not blocked, "uncertain-delivery-exhausted" if blocked else "ok"
            )
        except PromptRefused as exc:
            return Receipt(False, str(exc))
        except (OSError, ValueError, TypeError, KeyError):
            return Receipt(False, "snapshot-publication-pending")

    def _admit_prompt(self, token):
        row = self.dispatcher.obligations[token]
        self.dispatcher._save_obligation(
            {**row, "launch": {**row["launch"], "prompt_admitted": True}}
        )

    def _items(self, session, batch):
        events = {
            key: Event.from_record(row["event"])
            for key, row in self.dispatcher.obligations.items()
            if row["state"]
            not in {
                "held",
                "cancelled",
                "launch-reserved",
                "launch-waiting",
                "launch-failed",
            }
        }
        events.update((event.identity, event) for event in batch)
        directory = frozenset(self.dispatcher.directory())
        admission = getattr(
            self.dispatcher.admission, "admit", self.dispatcher.admission
        )
        return sorted(
            (event.identity, "wake", self._text(event))
            for event in events.values()
            if event.identity not in getattr(self.dispatcher, "cancelled", ())
            and self.dispatcher.obligations.get(event.identity, {}).get("state")
            not in {
                "held",
                "cancelled",
                "launch-reserved",
                "launch-waiting",
                "launch-failed",
            }
            and event.source not in LIFECYCLE_INPUT_KINDS
            and session in self._targets(event, directory)
            and (event.deadline is None or event.deadline <= self.dispatcher.clock())
            and not isinstance(event.target, MachineTarget)
            and event.scope != "machine"
            and admission(event, session) == Disposition.WAKE
        )

    def _targets(self, event, directory):
        row = self.dispatcher.obligations.get(event.identity, {})
        if "launch" in row:
            from .launches import targets

            return targets(self.dispatcher, row, directory)
        return event.target.resolve(directory)

    def refresh(self, event):
        # Persist the repair before attempting publication: terminal intake may
        # acknowledge even when publication fails, and delivered rows do not replay.
        write_record(self.repairs / f"{event.identity}.json", event.as_record())
        sync_directory(self.repairs.parent)
        self.reconcile()

    def reconcile(self):
        """Consume pending repairs on start/source events or channel activity only."""
        for path in sorted(self.repairs.glob("*.json")):
            try:
                record = json.loads(path.read_text())
                event = Event.from_record(record)
                if path.stem != event.identity:
                    raise ValueError("snapshot-repair-identity")
                if self._refresh(event):
                    # Confirm this exact repair before retiring its durable marker.
                    if json.loads(path.read_text()) != record:
                        raise ValueError("snapshot-repair-changed")
                    path.unlink()
                    sync_directory(self.repairs)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self.dispatcher._degraded("snapshot-refresh", type(exc).__name__)

    def _refresh(self, event):
        directory = frozenset(self.dispatcher.directory())
        delivered = self.dispatcher.obligations.get(event.identity, {}).get(
            "delivered", ()
        )
        affected = event.target.resolve(directory) | {
            session for session in directory if session.slug in delivered
        }
        repaired = True
        for session in affected & directory:
            receipt = self._publish(session, [])
            if not receipt.accepted:
                repaired = False
                self.dispatcher._degraded("snapshot-refresh", receipt.reason)
        return repaired

    def _text(self, event):
        launch = self.dispatcher.obligations.get(event.identity, {}).get("launch")
        if launch is not None:
            return launch["message"]
        if event.source == "git-inbox":
            return f"git-inbox action {event.key}: read and take the addressed inbox message."
        if event.source == "assignment":
            return f"Beads assignment {event.key}: read the issue and act on the assignment."
        if event.source == "harness-sync":
            return f"ai-harness install {event.key}: run /sync-git and /install-harness as installer owner."
        if event.source == "buzz":
            from ai_chassis.buzz_channel import event_message_id

            message_id = event_message_id(event.key)
            return f"Buzz message {message_id}: read with agent-mail-notifier read {message_id}."
        return f"{event.source}: pending action {event.key}."
