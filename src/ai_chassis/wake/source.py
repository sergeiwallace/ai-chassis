"""Source lifecycle. Handoff acknowledgement follows durable work persistence."""

from abc import ABC, abstractmethod


class EventSource(ABC):
    @abstractmethod
    def open(self):
        """Establish a subscription before catch-up to avoid a delivery gap."""

    @abstractmethod
    def readable_handles(self) -> tuple:
        """Handles accepted by the host's blocking wait primitive."""

    @abstractmethod
    def drain(self) -> list:
        """Read only after the OS/transport signals input."""

    @abstractmethod
    def catch_up(self, cursor) -> list:
        """Once on start/reconnect/resume; never as an idle backstop."""

    def commit(self, _events):
        """Acknowledge durable handoff and optionally return a JSON replay cursor.

        The host persists returned cursors before any later catch-up. None means
        the source owns its cursor (or uses retained intake files instead).
        """
        return None

    def recovery_blocks(self, _event):
        """Whether incomplete catch-up leaves this event's terminal state unknown.

        Unknown dependencies fail closed. Sources may narrow this per obligation
        only when they cannot supply terminal state for the excluded events.
        """
        return True

    @abstractmethod
    def close(self):
        """Release every owned handle."""
