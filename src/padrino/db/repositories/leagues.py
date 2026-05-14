"""CRUD helpers for :class:`padrino.db.models.League`."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import League


async def create(
    session: AsyncSession,
    *,
    name: str,
    ruleset_id: str,
    ranked: bool,
) -> League:
    obj = League(name=name, ruleset_id=ruleset_id, ranked=ranked)
    session.add(obj)
    await session.flush()
    return obj


async def get(session: AsyncSession, league_id: uuid.UUID) -> League | None:
    return await session.get(League, league_id)


async def list_(
    session: AsyncSession,
    *,
    ranked: bool | None = None,
    ruleset_id: str | None = None,
) -> list[League]:
    stmt = select(League)
    if ranked is not None:
        stmt = stmt.where(League.ranked == ranked)
    if ruleset_id is not None:
        stmt = stmt.where(League.ruleset_id == ruleset_id)
    stmt = stmt.order_by(League.created_at, League.id)
    result = await session.execute(stmt)
    return list(result.scalars())
