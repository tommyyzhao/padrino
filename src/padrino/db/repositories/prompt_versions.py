"""CRUD helpers for :class:`padrino.db.models.PromptVersion`."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import PromptVersion


async def create(
    session: AsyncSession,
    *,
    ruleset_id: str,
    version: str,
    system_prompt: str,
    developer_prompt: str,
    response_schema: dict[str, Any],
    prompt_hash: str,
) -> PromptVersion:
    obj = PromptVersion(
        ruleset_id=ruleset_id,
        version=version,
        system_prompt=system_prompt,
        developer_prompt=developer_prompt,
        response_schema=response_schema,
        prompt_hash=prompt_hash,
    )
    session.add(obj)
    await session.flush()
    return obj


async def get(session: AsyncSession, prompt_version_id: uuid.UUID) -> PromptVersion | None:
    return await session.get(PromptVersion, prompt_version_id)


async def get_by_hash(session: AsyncSession, prompt_hash: str) -> PromptVersion | None:
    stmt = select(PromptVersion).where(PromptVersion.prompt_hash == prompt_hash)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_(
    session: AsyncSession,
    *,
    ruleset_id: str | None = None,
) -> list[PromptVersion]:
    stmt = select(PromptVersion)
    if ruleset_id is not None:
        stmt = stmt.where(PromptVersion.ruleset_id == ruleset_id)
    stmt = stmt.order_by(PromptVersion.created_at, PromptVersion.id)
    result = await session.execute(stmt)
    return list(result.scalars())
