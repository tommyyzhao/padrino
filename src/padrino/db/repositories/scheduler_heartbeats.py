"""CRUD helpers for :class:`padrino.db.models.SchedulerHeartbeat` (US-060).

The scheduler worker upserts its row on every tick; the
``/healthz/scheduler`` endpoint reads the maximum ``beat_at`` across all
workers to decide between ``ok`` / ``degraded`` / ``down`` states.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import SchedulerHeartbeat


def _aware(dt: datetime) -> datetime:
    """Coerce a naive datetime (e.g. from aiosqlite) to UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def upsert(session: AsyncSession, *, worker_id: str, beat_at: datetime) -> None:
    """Insert a heartbeat row or update the existing one for ``worker_id``."""
    obj = await session.get(SchedulerHeartbeat, worker_id)
    if obj is None:
        obj = SchedulerHeartbeat(worker_id=worker_id, beat_at=beat_at)
        session.add(obj)
    else:
        obj.beat_at = beat_at
    await session.flush()


async def latest_beat(session: AsyncSession) -> datetime | None:
    """Return the maximum ``beat_at`` across all workers, or ``None``."""
    stmt = select(func.max(SchedulerHeartbeat.beat_at))
    result = await session.execute(stmt)
    value = result.scalar_one_or_none()
    if value is None:
        return None
    return _aware(value)


async def list_(session: AsyncSession) -> list[SchedulerHeartbeat]:
    stmt = select(SchedulerHeartbeat).order_by(SchedulerHeartbeat.worker_id)
    rows = list((await session.execute(stmt)).scalars())
    for row in rows:
        row.beat_at = _aware(row.beat_at)
    return rows


__all__ = ["latest_beat", "list_", "upsert"]
