"""CRUD helpers for :class:`padrino.db.models.ApiKey` (US-056).

Raw key strings are never persisted. The caller hashes the raw key with
:func:`hash_api_key` and supplies only the digest plus the display prefix.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import ApiKey

KEY_PREFIX_LENGTH: Final[int] = 6


def hash_api_key(raw: str) -> str:
    """Return the sha256 hex digest used to look an api_key up by value."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def display_prefix(raw: str) -> str:
    return raw[:KEY_PREFIX_LENGTH]


async def create(
    session: AsyncSession,
    *,
    raw_key: str,
    scopes: list[str],
    label: str,
) -> ApiKey:
    obj = ApiKey(
        key_hash=hash_api_key(raw_key),
        key_prefix=display_prefix(raw_key),
        scopes=list(scopes),
        label=label,
    )
    session.add(obj)
    await session.flush()
    return obj


async def get_by_raw(session: AsyncSession, raw_key: str) -> ApiKey | None:
    digest = hash_api_key(raw_key)
    stmt = select(ApiKey).where(ApiKey.key_hash == digest)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get(session: AsyncSession, key_id: uuid.UUID) -> ApiKey | None:
    return await session.get(ApiKey, key_id)


async def list_(session: AsyncSession) -> list[ApiKey]:
    stmt = select(ApiKey).order_by(ApiKey.created_at, ApiKey.id)
    return list((await session.execute(stmt)).scalars())


async def disable(session: AsyncSession, key_id: uuid.UUID) -> ApiKey | None:
    obj = await session.get(ApiKey, key_id)
    if obj is None:
        return None
    if obj.disabled_at is None:
        obj.disabled_at = datetime.now(UTC)
    await session.flush()
    return obj


async def mark_used(session: AsyncSession, key_id: uuid.UUID, *, now: datetime) -> None:
    obj = await session.get(ApiKey, key_id)
    if obj is None:
        return
    obj.last_used_at = now
    await session.flush()


__all__ = [
    "KEY_PREFIX_LENGTH",
    "create",
    "disable",
    "display_prefix",
    "get",
    "get_by_raw",
    "hash_api_key",
    "list_",
    "mark_used",
]
