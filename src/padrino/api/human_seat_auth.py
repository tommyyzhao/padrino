"""Shared human-game seat authorization for API route shells."""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import GameSeat

WRONG_SEAT_DETAIL = "wrong_seat"


async def resolve_human_game_seat(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    principal_id: uuid.UUID,
    wrong_seat_detail: str = WRONG_SEAT_DETAIL,
) -> GameSeat:
    """Return the principal's occupied seat for one game, or reject.

    Human action, chat, observation, and turing routes all use the same
    authorization check: a principal may act only through the seat linked by
    ``GameSeat.occupant_principal_id`` for that game.
    """
    stmt = select(GameSeat).where(
        GameSeat.game_id == game_id,
        GameSeat.occupant_principal_id == principal_id,
    )
    seat = (await session.execute(stmt)).scalar_one_or_none()
    if seat is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=wrong_seat_detail)
    return seat


__all__ = ["WRONG_SEAT_DETAIL", "resolve_human_game_seat"]
