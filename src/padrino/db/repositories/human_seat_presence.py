"""CRUD helpers for human seat presence heartbeats (US-162).

Presence is impure transport metadata for human games: routes record a seat's
latest heartbeat, and the human worker lane reads those rows to decide whether a
seat has exceeded its reconnect grace window. This repository never reads a
clock; all timestamps are injected by the caller.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import HumanSeatPresence


async def get(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    public_player_id: str,
) -> HumanSeatPresence | None:
    """Return one seat presence row, or ``None`` if no heartbeat was recorded."""
    stmt = select(HumanSeatPresence).where(
        HumanSeatPresence.game_id == game_id,
        HumanSeatPresence.public_player_id == public_player_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_for_game(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
) -> list[HumanSeatPresence]:
    """Return every presence row for ``game_id`` ordered by seat id."""
    stmt = (
        select(HumanSeatPresence)
        .where(HumanSeatPresence.game_id == game_id)
        .order_by(HumanSeatPresence.public_player_id)
    )
    return list((await session.execute(stmt)).scalars())


async def record_heartbeat(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    public_player_id: str,
    seen_at: datetime,
) -> HumanSeatPresence:
    """Upsert a connected heartbeat for one human seat."""
    row = await get(session, game_id=game_id, public_player_id=public_player_id)
    if row is None:
        row = HumanSeatPresence(
            game_id=game_id,
            public_player_id=public_player_id,
            connected=True,
            last_seen_at=seen_at,
            disconnected_at=None,
            updated_at=seen_at,
        )
        session.add(row)
    else:
        row.connected = True
        row.last_seen_at = seen_at
        row.disconnected_at = None
        row.updated_at = seen_at
    await session.flush()
    return row


async def mark_disconnected(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    public_player_id: str,
    disconnected_at: datetime,
) -> HumanSeatPresence:
    """Upsert an explicit disconnect for one human seat."""
    row = await get(session, game_id=game_id, public_player_id=public_player_id)
    if row is None:
        row = HumanSeatPresence(
            game_id=game_id,
            public_player_id=public_player_id,
            connected=False,
            last_seen_at=None,
            disconnected_at=disconnected_at,
            updated_at=disconnected_at,
        )
        session.add(row)
    else:
        row.connected = False
        row.disconnected_at = disconnected_at
        row.updated_at = disconnected_at
    await session.flush()
    return row


__all__ = [
    "get",
    "list_for_game",
    "mark_disconnected",
    "record_heartbeat",
]
