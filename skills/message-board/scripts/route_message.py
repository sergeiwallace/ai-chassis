from __future__ import annotations

from typing import NamedTuple


class RouteDecision(NamedTuple):
    channel: str
    native_delivery: bool
    board_delivery: bool


def select_channel(*, native_reachable: bool, cross_machine: bool) -> RouteDecision:
    """Return exactly one authorized delivery channel for a hand-off."""
    if native_reachable:
        return RouteDecision("native", True, False)
    if cross_machine:
        return RouteDecision("board", False, True)
    raise ValueError(
        "an unreachable same-machine target must not be routed to the board"
    )
