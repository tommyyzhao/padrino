"""CRUD helpers for :class:`padrino.db.models.Gauntlet` and roster slots."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import Gauntlet, GauntletRosterSlot


async def create(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    ruleset_id: str,
    prompt_version_id: uuid.UUID,
    clone_count: int,
    gauntlet_seed: str,
    ranked: bool,
    status: str = "PENDING",
) -> Gauntlet:
    obj = Gauntlet(
        league_id=league_id,
        ruleset_id=ruleset_id,
        prompt_version_id=prompt_version_id,
        clone_count=clone_count,
        gauntlet_seed=gauntlet_seed,
        ranked=ranked,
        status=status,
    )
    session.add(obj)
    await session.flush()
    return obj


async def get(session: AsyncSession, gauntlet_id: uuid.UUID) -> Gauntlet | None:
    return await session.get(Gauntlet, gauntlet_id)


async def list_(
    session: AsyncSession,
    *,
    league_id: uuid.UUID | None = None,
    status: str | None = None,
    ranked: bool | None = None,
) -> list[Gauntlet]:
    stmt = select(Gauntlet)
    if league_id is not None:
        stmt = stmt.where(Gauntlet.league_id == league_id)
    if status is not None:
        stmt = stmt.where(Gauntlet.status == status)
    if ranked is not None:
        stmt = stmt.where(Gauntlet.ranked == ranked)
    stmt = stmt.order_by(Gauntlet.created_at, Gauntlet.id)
    result = await session.execute(stmt)
    return list(result.scalars())


async def add_roster_slot(
    session: AsyncSession,
    gauntlet_id: uuid.UUID,
    slot_index: int,
    agent_build_id: uuid.UUID,
) -> GauntletRosterSlot:
    slot = GauntletRosterSlot(
        gauntlet_id=gauntlet_id,
        slot_index=slot_index,
        agent_build_id=agent_build_id,
    )
    session.add(slot)
    await session.flush()
    return slot


async def list_roster_slots(
    session: AsyncSession,
    gauntlet_id: uuid.UUID,
) -> list[GauntletRosterSlot]:
    stmt = (
        select(GauntletRosterSlot)
        .where(GauntletRosterSlot.gauntlet_id == gauntlet_id)
        .order_by(GauntletRosterSlot.slot_index)
    )
    result = await session.execute(stmt)
    return list(result.scalars())
