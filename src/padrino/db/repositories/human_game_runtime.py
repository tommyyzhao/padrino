"""CRUD helpers for :class:`padrino.db.models.HumanGameRuntime` (US-131).

The human-lane runner persists its live scaffolding here — the current phase,
the phase wall-clock deadline, and a buffer snapshot of in-flight human
submissions — so an in-progress human game survives a process restart. US-168
adds an optional state/log cache for request-path performance; the hash-chained
``game_events`` log stays authoritative and callers validate the cached head
before using it.

This repository never imports a clock or RNG (the repository-purity guard
forbids ``time`` / ``secrets`` / ``random``): the ``deadline_at`` and
``updated_at`` values are passed in from the impure runner shell.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import HumanGameRuntime


async def upsert(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    phase: str,
    deadline_at: datetime | None,
    buffer_snapshot: dict[str, Any],
    updated_at: datetime,
    state_cache: dict[str, Any] | None = None,
) -> HumanGameRuntime:
    """Insert or update the one runtime row for ``game_id`` and return it."""
    row = await session.get(HumanGameRuntime, game_id)
    if row is None:
        row = HumanGameRuntime(
            game_id=game_id,
            phase=phase,
            deadline_at=deadline_at,
            buffer_snapshot=buffer_snapshot,
            state_cache=state_cache,
            updated_at=updated_at,
        )
        session.add(row)
    else:
        row.phase = phase
        row.deadline_at = deadline_at
        row.buffer_snapshot = buffer_snapshot
        row.state_cache = state_cache
        row.updated_at = updated_at
    await session.flush()
    return row


async def get(session: AsyncSession, game_id: uuid.UUID) -> HumanGameRuntime | None:
    """Return the runtime row for ``game_id`` or ``None``."""
    return await session.get(HumanGameRuntime, game_id)


async def list_all(session: AsyncSession) -> list[HumanGameRuntime]:
    """Return every runtime row, ordered by ``game_id`` for determinism."""
    stmt = select(HumanGameRuntime).order_by(HumanGameRuntime.game_id)
    result = await session.execute(stmt)
    return list(result.scalars())


async def delete(session: AsyncSession, game_id: uuid.UUID) -> bool:
    """Drop the runtime row for ``game_id``; return whether a row was removed."""
    row = await session.get(HumanGameRuntime, game_id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
