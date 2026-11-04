"""Public extension contracts for verified doorbells and transport routes."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class Receipt:
    accepted: bool
    reason: str = "ok"


class DoorbellHandler(ABC):
    @abstractmethod
    def handle(self, doorbell) -> list:
        """Fetch authoritative content and emit only verified, locally addressed facts."""


class Route(ABC):
    @abstractmethod
    def available(self) -> bool:
        """Return observed transport availability without a background probe."""

    @abstractmethod
    def ring(self, doorbell) -> Receipt:
        """Return success only for durable receiving handoff."""
