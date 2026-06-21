"""CRUD helpers for :class:`padrino.db.models.GameEvent`.

The runner appends events here as the game runs, paralleling the in-memory
:class:`padrino.core.engine.event_log.EventLog`. Persisted rows carry the
full hash-chain envelope (``sequence``, ``prev_event_hash``, ``event_hash``)
plus the event body fields, so the DB row alone is sufficient to verify the
chain.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import GameEvent


async def append_event(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    sequence: int,
    event_type: str,
    phase: str,
    visibility: str,
    actor_player_id: str | None,
    payload: dict[str, Any],
    prev_event_hash: str,
    event_hash: str,
) -> GameEvent:
    """Insert one event row and return the persisted ORM object."""
    obj = GameEvent(
        game_id=game_id,
        sequence=sequence,
        event_type=event_type,
        phase=phase,
        visibility=visibility,
        actor_player_id=actor_player_id,
        payload=payload,
        prev_event_hash=prev_event_hash,
        event_hash=event_hash,
    )
    session.add(obj)
    await session.flush()
    return obj


async def list_events(
    session: AsyncSession,
    game_id: uuid.UUID,
    *,
    visibility_filter: str | None = None,
) -> list[GameEvent]:
    """Return all events for ``game_id`` in sequence order, optionally filtered."""
    stmt = select(GameEvent).where(GameEvent.game_id == game_id)
    if visibility_filter is not None:
        stmt = stmt.where(GameEvent.visibility == visibility_filter)
    stmt = stmt.order_by(GameEvent.sequence)
    result = await session.execute(stmt)
    return list(result.scalars())


async def get_event_at_sequence(
    session: AsyncSession,
    game_id: uuid.UUID,
    *,
    sequence: int,
) -> GameEvent | None:
    """Return one event by sequence, or ``None`` when it does not exist."""
    stmt = select(GameEvent).where(
        GameEvent.game_id == game_id,
        GameEvent.sequence == sequence,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_events_after(
    session: AsyncSession,
    game_id: uuid.UUID,
    *,
    after_sequence: int,
) -> list[GameEvent]:
    """Return events whose sequence is greater than ``after_sequence``."""
    stmt = (
        select(GameEvent)
        .where(GameEvent.game_id == game_id, GameEvent.sequence > after_sequence)
        .order_by(GameEvent.sequence)
    )
    result = await session.execute(stmt)
    return list(result.scalars())


async def max_persisted_sequence(
    session: AsyncSession,
    game_id: uuid.UUID,
) -> int | None:
    """Return the highest persisted ``sequence`` for ``game_id`` (``None`` if empty).

    Used to make event persistence idempotent: a paired DB mutation may persist
    its own event row in the same transaction (so the chain never lags the seat /
    sidecar state across a crash), after which the outer loop must not re-insert
    that already-committed row and trip ``uq_game_event_sequence``.
    """
    stmt = select(func.max(GameEvent.sequence)).where(GameEvent.game_id == game_id)
    return (await session.execute(stmt)).scalar_one_or_none()
