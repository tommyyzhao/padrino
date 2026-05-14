"""CRUD helpers for :class:`padrino.db.models.ModelProvider`."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import ModelProvider


async def create(
    session: AsyncSession,
    *,
    name: str,
    auth_secret_ref: str,
    base_url: str | None = None,
) -> ModelProvider:
    obj = ModelProvider(name=name, base_url=base_url, auth_secret_ref=auth_secret_ref)
    session.add(obj)
    await session.flush()
    return obj


async def get(session: AsyncSession, provider_id: uuid.UUID) -> ModelProvider | None:
    return await session.get(ModelProvider, provider_id)


async def list_(
    session: AsyncSession,
    *,
    name: str | None = None,
) -> list[ModelProvider]:
    stmt = select(ModelProvider)
    if name is not None:
        stmt = stmt.where(ModelProvider.name == name)
    stmt = stmt.order_by(ModelProvider.created_at, ModelProvider.id)
    result = await session.execute(stmt)
    return list(result.scalars())
