"""CRUD helpers for :class:`padrino.db.models.ModelConfig`."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import ModelConfig


async def create(
    session: AsyncSession,
    *,
    provider_id: uuid.UUID,
    model_name: str,
    default_temperature: float,
    default_top_p: float,
    default_max_output_tokens: int,
    supports_structured_outputs: bool,
    model_version: str | None = None,
) -> ModelConfig:
    obj = ModelConfig(
        provider_id=provider_id,
        model_name=model_name,
        model_version=model_version,
        default_temperature=default_temperature,
        default_top_p=default_top_p,
        default_max_output_tokens=default_max_output_tokens,
        supports_structured_outputs=supports_structured_outputs,
    )
    session.add(obj)
    await session.flush()
    return obj


async def get(session: AsyncSession, model_config_id: uuid.UUID) -> ModelConfig | None:
    return await session.get(ModelConfig, model_config_id)


async def list_(
    session: AsyncSession,
    *,
    provider_id: uuid.UUID | None = None,
    model_name: str | None = None,
) -> list[ModelConfig]:
    stmt = select(ModelConfig)
    if provider_id is not None:
        stmt = stmt.where(ModelConfig.provider_id == provider_id)
    if model_name is not None:
        stmt = stmt.where(ModelConfig.model_name == model_name)
    stmt = stmt.order_by(ModelConfig.created_at, ModelConfig.id)
    result = await session.execute(stmt)
    return list(result.scalars())
