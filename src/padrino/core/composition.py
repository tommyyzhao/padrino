"""Canonical game-composition counting (Wave 9, US-126).

Four surfaces — lobby, live, spectator, and observation — must disclose a
game's composition as *counts only* ("N humans, M AI"), never as a per-seat
human/AI map. Before this module each surface computed those counts on its own,
so the "counts only, never the seat map" rule could drift between them.

:func:`composition_summary` is the SINGLE, pure (data-in / no IO) producer of
composition counts. Every surface MUST consume it rather than counting seats
itself.

Counts are frozen at game start: a ``HUMAN`` seat that is later silently taken
over by an AI keeps its ``AI_TAKEOVER`` provenance, and that seat still counts
as a human here, so a takeover never changes the disclosed counts (US-142).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, TypedDict

from padrino.core.enums import SeatKind


class CompositionSummary(TypedDict):
    """Counts-only composition of a game's seats."""

    human_count: int
    ai_count: int
    total: int


#: Seat kinds that count as a *human* in the disclosed composition. ``HUMAN`` is
#: an occupied human seat; ``AI_TAKEOVER`` is a seat that *started* as a human
#: and was silently taken over, so it still counts as human to keep the
#: start-frozen counts stable across a takeover (US-142).
_HUMAN_SEAT_KINDS: frozenset[SeatKind] = frozenset({SeatKind.HUMAN, SeatKind.AI_TAKEOVER})


def _seat_kind_of(seat: Any) -> SeatKind | None:
    """Extract a :class:`SeatKind` from a seat-like value, fail-closed to AI.

    Accepts a :class:`SeatKind`, a raw string, a mapping with a ``seat_kind``
    key, or any object exposing a ``seat_kind`` attribute. An unknown / missing
    value resolves to ``None`` (counted as AI) so a malformed seat can never be
    mistaken for a human and inflate the human count.
    """
    if isinstance(seat, SeatKind):
        raw: Any = seat
    elif isinstance(seat, str):
        raw = seat
    elif isinstance(seat, Mapping):
        raw = seat.get("seat_kind")
    else:
        raw = getattr(seat, "seat_kind", None)

    if raw is None:
        return None
    if isinstance(raw, SeatKind):
        return raw
    try:
        return SeatKind(str(raw))
    except ValueError:
        return None


def composition_summary(seats: Iterable[Any]) -> CompositionSummary:
    """Return the counts-only composition of ``seats``.

    ``seats`` is any iterable of seat-like values (core ``Seat``, DB
    ``GameSeat``, a mapping, a raw ``SeatKind`` / string). A seat whose kind is
    one of :data:`_HUMAN_SEAT_KINDS` counts as human; every other seat (``AI``,
    unknown, or missing kind) counts as AI. ``total`` is always
    ``human_count + ai_count``.
    """
    human_count = 0
    ai_count = 0
    for seat in seats:
        if _seat_kind_of(seat) in _HUMAN_SEAT_KINDS:
            human_count += 1
        else:
            ai_count += 1
    return CompositionSummary(
        human_count=human_count,
        ai_count=ai_count,
        total=human_count + ai_count,
    )
