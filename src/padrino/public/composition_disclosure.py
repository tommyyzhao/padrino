"""Counts-only composition disclosure for player/spectator/lobby surfaces (US-142).

Every surface that shows a game's composition — lobby, live, spectator, and the
per-player observation client — must disclose *only how many humans vs AI are
present*, never which seat is which. Decision 7: counts only, frozen at game
start, never a per-seat map.

This module is the SINGLE translation seam between the pure canonical counter
(:func:`padrino.core.composition.composition_summary`, US-126) and the API
response shape. Surfaces feed it their seat rows (DB ``GameSeat`` or core
``Seat``); they never count seats themselves and never assemble a per-seat
human/AI map. Because the canonical counter treats a silently-taken-over seat
(``SeatKind.AI_TAKEOVER``) as still human, the disclosed counts do NOT change
on a takeover — the imitation game is preserved mid-play.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from padrino.core.composition import composition_summary


class CompositionCounts(BaseModel):
    """Counts-only composition disclosure response.

    Deliberately carries ONLY aggregate counts — there is no per-seat field, so
    a surface that returns this model cannot leak which seat is human vs AI.
    """

    human_count: int
    ai_count: int
    total: int


def composition_counts(seats: Iterable[Any]) -> CompositionCounts:
    """Project seat rows to the counts-only disclosure via the canonical counter.

    ``seats`` is any iterable of seat-like values accepted by
    :func:`composition_summary` (DB ``GameSeat``, core ``Seat``, a mapping with
    a ``seat_kind`` key, a raw ``SeatKind`` / string). The returned model never
    exposes the per-seat map; a silent takeover does not change the counts.
    """
    summary = composition_summary(seats)
    return CompositionCounts(
        human_count=summary["human_count"],
        ai_count=summary["ai_count"],
        total=summary["total"],
    )


__all__ = ["CompositionCounts", "composition_counts"]
