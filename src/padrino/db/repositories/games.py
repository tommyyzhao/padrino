"""CRUD helpers for :class:`padrino.db.models.Game` and game seats."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.game_status import GAME_STATUS_CREATED, GAME_STATUS_FAILED, GAME_STATUS_RUNNING
from padrino.db.models import Game, GameSeat


def _aware(dt: datetime) -> datetime:
    """Treat SQLite's naive datetime reads as UTC for cross-dialect lease checks."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def create(
    session: AsyncSession,
    *,
    ruleset_id: str,
    game_seed: str,
    status: str = GAME_STATUS_CREATED,
    gauntlet_id: uuid.UUID | None = None,
    pair_id: uuid.UUID | None = None,
    pair_leg: int | None = None,
) -> Game:
    obj = Game(
        gauntlet_id=gauntlet_id,
        pair_id=pair_id,
        pair_leg=pair_leg,
        ruleset_id=ruleset_id,
        game_seed=game_seed,
        status=status,
    )
    session.add(obj)
    await session.flush()
    return obj


async def get(session: AsyncSession, game_id: uuid.UUID) -> Game | None:
    return await session.get(Game, game_id)


async def claim_oldest_pending_game(
    session: AsyncSession,
    *,
    now: datetime,
    lease_ttl: timedelta,
    worker_id: str,
) -> Game | None:
    """Claim the oldest runnable game row for one worker.

    Eligible games are newly-created games, expired running games, and running
    rows whose expired lease has already been cleared by ``reset_stale_games``.
    PostgreSQL uses ``FOR UPDATE SKIP LOCKED``; SQLite omits it because the
    supported deployment is single-writer.
    """
    stmt = (
        select(Game)
        .where(
            or_(
                Game.status == GAME_STATUS_CREATED,
                and_(
                    Game.status == GAME_STATUS_RUNNING,
                    or_(
                        Game.lease_expires_at <= now,
                        and_(Game.lease_expires_at.is_(None), Game.leased_by.is_(None)),
                    ),
                ),
            )
        )
        .order_by(Game.created_at, Game.id)
        .limit(1)
    )
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    game = (await session.execute(stmt)).scalars().first()
    if game is None:
        return None
    game.status = GAME_STATUS_RUNNING
    game.leased_by = worker_id
    game.lease_expires_at = now + lease_ttl
    game.attempt_count = (game.attempt_count or 0) + 1
    await session.flush()
    return game


async def list_(
    session: AsyncSession,
    *,
    status: str | None = None,
    gauntlet_id: uuid.UUID | None = None,
    ruleset_id: str | None = None,
) -> list[Game]:
    stmt = select(Game)
    if status is not None:
        stmt = stmt.where(Game.status == status)
    if gauntlet_id is not None:
        stmt = stmt.where(Game.gauntlet_id == gauntlet_id)
    if ruleset_id is not None:
        stmt = stmt.where(Game.ruleset_id == ruleset_id)
    stmt = stmt.order_by(Game.id)
    result = await session.execute(stmt)
    return list(result.scalars())


async def list_by_gauntlet(
    session: AsyncSession,
    gauntlet_id: uuid.UUID,
) -> list[Game]:
    stmt = select(Game).where(Game.gauntlet_id == gauntlet_id).order_by(Game.id)
    result = await session.execute(stmt)
    return list(result.scalars())


async def reset_stale_games(
    session: AsyncSession,
    *,
    now: datetime,
) -> list[uuid.UUID]:
    """Clear expired leases on RUNNING games and return their ids."""
    stmt = (
        select(Game)
        .where(Game.status == GAME_STATUS_RUNNING, Game.lease_expires_at.is_not(None))
        .order_by(Game.created_at, Game.id)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    reset: list[uuid.UUID] = []
    cutoff = _aware(now)
    for game in rows:
        expires_at = game.lease_expires_at
        if expires_at is not None and _aware(expires_at) <= cutoff:
            game.leased_by = None
            game.lease_expires_at = None
            reset.append(game.id)
    if reset:
        await session.flush()
    return reset


async def update_status(
    session: AsyncSession,
    game_id: uuid.UUID,
    *,
    status: str,
    terminal_result: dict[str, Any] | None = None,
    current_phase: str | None = None,
    event_hash_head: str | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> Game | None:
    game = await session.get(Game, game_id)
    if game is None:
        return None
    game.status = status
    if terminal_result is not None:
        game.terminal_result = terminal_result
    if current_phase is not None:
        game.current_phase = current_phase
    if event_hash_head is not None:
        game.event_hash_head = event_hash_head
    if started_at is not None:
        game.started_at = started_at
    if completed_at is not None:
        game.completed_at = completed_at
    await session.flush()
    return game


async def mark_failed(
    session: AsyncSession,
    game_id: uuid.UUID,
    *,
    completed_at: datetime,
) -> Game | None:
    """Mark a child game as terminally failed without a terminal result."""
    game = await session.get(Game, game_id)
    if game is None:
        return None
    game.status = GAME_STATUS_FAILED
    game.terminal_result = None
    game.completed_at = completed_at
    await session.flush()
    return game


async def add_seat(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    public_player_id: str,
    seat_index: int,
    agent_build_id: uuid.UUID,
    role: str,
    faction: str,
    alive: bool = True,
) -> GameSeat:
    seat = GameSeat(
        game_id=game_id,
        public_player_id=public_player_id,
        seat_index=seat_index,
        agent_build_id=agent_build_id,
        role=role,
        faction=faction,
        alive=alive,
    )
    session.add(seat)
    await session.flush()
    return seat


async def list_seats(
    session: AsyncSession,
    game_id: uuid.UUID,
) -> list[GameSeat]:
    stmt = select(GameSeat).where(GameSeat.game_id == game_id).order_by(GameSeat.seat_index)
    result = await session.execute(stmt)
    return list(result.scalars())
