"""CRUD helpers for :class:`padrino.db.models.Game` and game seats."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import Game, GameSeat


async def create(
    session: AsyncSession,
    *,
    ruleset_id: str,
    game_seed: str,
    status: str = "CREATED",
    gauntlet_id: uuid.UUID | None = None,
) -> Game:
    obj = Game(
        gauntlet_id=gauntlet_id,
        ruleset_id=ruleset_id,
        game_seed=game_seed,
        status=status,
    )
    session.add(obj)
    await session.flush()
    return obj


async def get(session: AsyncSession, game_id: uuid.UUID) -> Game | None:
    return await session.get(Game, game_id)


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


async def update_status(
    session: AsyncSession,
    game_id: uuid.UUID,
    *,
    status: str,
    terminal_result: str | None = None,
    terminal_reason: str | None = None,
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
    if terminal_reason is not None:
        game.terminal_reason = terminal_reason
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
