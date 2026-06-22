"""CRUD helpers for :class:`padrino.db.models.SchedulerHeartbeat` (US-060).

The scheduler worker upserts its row on every tick; the
``/healthz/scheduler`` endpoint reads the maximum ``beat_at`` across all
workers to decide between ``ok`` / ``degraded`` / ``down`` states.

US-230 reuses this small heartbeat ledger for the single-host human-lane
worker by reserving the ``human-lane:`` worker-id prefix. Scheduler health
therefore filters that prefix out, while human-lane health filters it in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import SchedulerHeartbeat

HUMAN_LANE_WORKER_PREFIX: Final[str] = "human-lane:"


def _aware(dt: datetime) -> datetime:
    """Coerce a naive datetime (e.g. from aiosqlite) to UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def human_lane_worker_id(worker_id: str) -> str:
    """Return ``worker_id`` with the reserved human-lane prefix."""
    if worker_id.startswith(HUMAN_LANE_WORKER_PREFIX):
        return worker_id
    return f"{HUMAN_LANE_WORKER_PREFIX}{worker_id}"


async def upsert(session: AsyncSession, *, worker_id: str, beat_at: datetime) -> None:
    """Insert a heartbeat row or update the existing one for ``worker_id``."""
    obj = await session.get(SchedulerHeartbeat, worker_id)
    if obj is None:
        obj = SchedulerHeartbeat(worker_id=worker_id, beat_at=beat_at)
        session.add(obj)
    else:
        obj.beat_at = beat_at
    await session.flush()


async def _latest_beat(
    session: AsyncSession,
    *,
    include_worker_id_prefix: str | None = None,
    exclude_worker_id_prefix: str | None = None,
) -> datetime | None:
    """Return the maximum matching ``beat_at`` value, or ``None``."""
    stmt = select(func.max(SchedulerHeartbeat.beat_at))
    if include_worker_id_prefix is not None:
        stmt = stmt.where(SchedulerHeartbeat.worker_id.like(f"{include_worker_id_prefix}%"))
    if exclude_worker_id_prefix is not None:
        stmt = stmt.where(~SchedulerHeartbeat.worker_id.like(f"{exclude_worker_id_prefix}%"))
    result = await session.execute(stmt)
    value = result.scalar_one_or_none()
    if value is None:
        return None
    return _aware(value)


async def latest_beat(session: AsyncSession) -> datetime | None:
    """Return the maximum ``beat_at`` across all workers, or ``None``."""
    return await _latest_beat(session)


async def latest_scheduler_beat(session: AsyncSession) -> datetime | None:
    """Return the latest benchmark scheduler heartbeat, excluding human lane rows."""
    return await _latest_beat(session, exclude_worker_id_prefix=HUMAN_LANE_WORKER_PREFIX)


async def latest_human_lane_beat(session: AsyncSession) -> datetime | None:
    """Return the latest human-lane heartbeat, or ``None``."""
    return await _latest_beat(session, include_worker_id_prefix=HUMAN_LANE_WORKER_PREFIX)


async def list_(session: AsyncSession) -> list[SchedulerHeartbeat]:
    stmt = select(SchedulerHeartbeat).order_by(SchedulerHeartbeat.worker_id)
    rows = list((await session.execute(stmt)).scalars())
    for row in rows:
        row.beat_at = _aware(row.beat_at)
    return rows


__all__ = [
    "HUMAN_LANE_WORKER_PREFIX",
    "human_lane_worker_id",
    "latest_beat",
    "latest_human_lane_beat",
    "latest_scheduler_beat",
    "list_",
    "upsert",
]
