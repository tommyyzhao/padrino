"""CRUD helpers for :class:`padrino.db.models.AgentBuild`."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import AgentBuild


async def create(
    session: AsyncSession,
    *,
    display_name: str,
    model_config_id: uuid.UUID,
    prompt_version_id: uuid.UUID,
    adapter_version: str,
    inference_params: dict[str, Any],
    active: bool = True,
) -> AgentBuild:
    obj = AgentBuild(
        display_name=display_name,
        model_config_id=model_config_id,
        prompt_version_id=prompt_version_id,
        adapter_version=adapter_version,
        inference_params=inference_params,
        active=active,
    )
    session.add(obj)
    await session.flush()
    return obj


async def get(session: AsyncSession, agent_build_id: uuid.UUID) -> AgentBuild | None:
    return await session.get(AgentBuild, agent_build_id)


async def list_(
    session: AsyncSession,
    *,
    active: bool | None = None,
    model_config_id: uuid.UUID | None = None,
    prompt_version_id: uuid.UUID | None = None,
) -> list[AgentBuild]:
    stmt = select(AgentBuild)
    if active is not None:
        stmt = stmt.where(AgentBuild.active == active)
    if model_config_id is not None:
        stmt = stmt.where(AgentBuild.model_config_id == model_config_id)
    if prompt_version_id is not None:
        stmt = stmt.where(AgentBuild.prompt_version_id == prompt_version_id)
    stmt = stmt.order_by(AgentBuild.created_at, AgentBuild.id)
    result = await session.execute(stmt)
    return list(result.scalars())
