"""Pure ranked lobby seating invariant for strangers-only human ELO."""

from __future__ import annotations

from typing import Final

RANKED_REQUIRES_SINGLE_HUMAN_SEAT: Final[str] = "ranked_requires_single_human_seat"


class RankedSeatingViolation(ValueError):
    """Raised when ranked lobby seating violates the v1 strangers-only rule."""

    def __init__(self, reason: str = RANKED_REQUIRES_SINGLE_HUMAN_SEAT) -> None:
        self.reason = reason
        super().__init__(reason)


def assert_strangers_only_ranked(*, lobby_is_ranked: bool, human_seat_count: int) -> None:
    """Assert ranked human-vs-AI seating has exactly one human seat.

    Casual lobbies are private friend lobbies and may carry any human seat
    count. Ranked v1 is strangers-only human-vs-AI, so the materialized layout
    must be exactly one human and the rest AI.
    """
    if not lobby_is_ranked:
        return
    if human_seat_count != 1:
        raise RankedSeatingViolation()


__all__ = [
    "RANKED_REQUIRES_SINGLE_HUMAN_SEAT",
    "RankedSeatingViolation",
    "assert_strangers_only_ranked",
]
