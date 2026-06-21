"""Shared human-game seat authorization for API route shells."""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import SeatKind
from padrino.db.models import GameSeat

WRONG_SEAT_DETAIL = "wrong_seat"

#: Seat kinds that a human principal can ever legitimately occupy. A seat in the
#: human lane is either occupied by a live human (``HUMAN``) or carries human
#: provenance after an AI took it over (``AI_TAKEOVER``); a plain ``AI`` seat must
#: never bind to a human principal. Restricting on this set is defense-in-depth:
#: even if an upstream invariant break set ``occupant_principal_id`` on an AI
#: seat, seat-auth still fails closed.
HUMAN_LANE_SEAT_KINDS: frozenset[str] = frozenset(
    {SeatKind.HUMAN.value, SeatKind.AI_TAKEOVER.value}
)


async def resolve_human_game_seat(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    principal_id: uuid.UUID,
    wrong_seat_detail: str = WRONG_SEAT_DETAIL,
    restrict_to_human_lane: bool = False,
) -> GameSeat:
    """Return the principal's occupied seat for one game, or reject.

    Human action, chat, observation, turing, and reveal routes all use the same
    authorization check: a principal may act only through the seat linked by
    ``GameSeat.occupant_principal_id`` for that game.

    When ``restrict_to_human_lane`` is set, the binding additionally requires the
    seat to be a human-lane kind (:data:`HUMAN_LANE_SEAT_KINDS`) so the auth
    cannot be widened by an upstream invariant break that put a principal id on a
    plain AI seat.
    """
    stmt = select(GameSeat).where(
        GameSeat.game_id == game_id,
        GameSeat.occupant_principal_id == principal_id,
    )
    if restrict_to_human_lane:
        stmt = stmt.where(GameSeat.seat_kind.in_(HUMAN_LANE_SEAT_KINDS))
    seat = (await session.execute(stmt)).scalar_one_or_none()
    if seat is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=wrong_seat_detail)
    return seat


__all__ = ["HUMAN_LANE_SEAT_KINDS", "WRONG_SEAT_DETAIL", "resolve_human_game_seat"]
