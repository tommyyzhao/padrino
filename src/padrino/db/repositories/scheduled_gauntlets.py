"""CRUD helpers for :class:`padrino.db.models.ScheduledGauntlet` (US-085)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import ScheduledGauntlet


def _aware(dt: datetime) -> datetime:
    """Coerce a naive datetime (e.g. from aiosqlite) to UTC for comparison."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def create(
    session: AsyncSession,
    *,
    name: str,
    schedule_cron: str,
    roster_spec_json: dict[str, Any],
    n_games: int,
    cost_cap_usd: float,
    enabled: bool = True,
    next_run_at: datetime | None = None,
) -> ScheduledGauntlet:
    obj = ScheduledGauntlet(
        name=name,
        schedule_cron=schedule_cron,
        roster_spec_json=roster_spec_json,
        n_games=n_games,
        cost_cap_usd=cost_cap_usd,
        enabled=enabled,
        next_run_at=next_run_at,
    )
    session.add(obj)
    await session.flush()
    return obj


async def get(session: AsyncSession, schedule_id: uuid.UUID) -> ScheduledGauntlet | None:
    return await session.get(ScheduledGauntlet, schedule_id)


async def get_by_name(session: AsyncSession, name: str) -> ScheduledGauntlet | None:
    stmt = select(ScheduledGauntlet).where(ScheduledGauntlet.name == name)
    return (await session.execute(stmt)).scalars().first()


async def list_all(session: AsyncSession) -> list[ScheduledGauntlet]:
    stmt = select(ScheduledGauntlet).order_by(ScheduledGauntlet.created_at, ScheduledGauntlet.id)
    return list((await session.execute(stmt)).scalars())


async def list_due(session: AsyncSession, *, now: datetime) -> list[ScheduledGauntlet]:
    """Return enabled schedules whose ``next_run_at`` is NULL or already due.

    Filtering happens in Python (after fetching enabled rows) so the
    timezone-aware comparison is correct on both SQLite (naive on read) and
    Postgres (tz-aware).
    """
    stmt = select(ScheduledGauntlet).where(ScheduledGauntlet.enabled.is_(True))
    cutoff = _aware(now)
    due: list[ScheduledGauntlet] = []
    for row in (await session.execute(stmt)).scalars():
        if row.next_run_at is None or _aware(row.next_run_at) <= cutoff:
            due.append(row)
    due.sort(key=lambda r: (r.created_at, str(r.id)))
    return due


async def update(
    session: AsyncSession,
    schedule_id: uuid.UUID,
    *,
    enabled: bool | None = None,
    schedule_cron: str | None = None,
    cost_cap_usd: float | None = None,
    next_run_at: datetime | None = None,
    set_next_run_at: bool = False,
) -> ScheduledGauntlet | None:
    """Update the mutable fields (enabled / schedule_cron / cost_cap_usd).

    ``next_run_at`` is only written when ``set_next_run_at`` is True (so the
    caller can recompute it after a cron change without conflating "leave as-is"
    with "set to None").
    """
    obj = await session.get(ScheduledGauntlet, schedule_id)
    if obj is None:
        return None
    if enabled is not None:
        obj.enabled = enabled
    if schedule_cron is not None:
        obj.schedule_cron = schedule_cron
    if cost_cap_usd is not None:
        obj.cost_cap_usd = cost_cap_usd
    if set_next_run_at:
        obj.next_run_at = next_run_at
    await session.flush()
    return obj


async def disable(session: AsyncSession, schedule_id: uuid.UUID) -> ScheduledGauntlet | None:
    """Soft-delete: set ``enabled=False`` and clear ``next_run_at`` (row kept for audit)."""
    obj = await session.get(ScheduledGauntlet, schedule_id)
    if obj is None:
        return None
    obj.enabled = False
    obj.next_run_at = None
    await session.flush()
    return obj


async def mark_run(
    session: AsyncSession,
    schedule_id: uuid.UUID,
    *,
    last_run_at: datetime,
    last_run_gauntlet_id: uuid.UUID,
    next_run_at: datetime | None,
) -> ScheduledGauntlet | None:
    obj = await session.get(ScheduledGauntlet, schedule_id)
    if obj is None:
        return None
    obj.last_run_at = last_run_at
    obj.last_run_gauntlet_id = last_run_gauntlet_id
    obj.next_run_at = next_run_at
    await session.flush()
    return obj


__all__ = [
    "create",
    "disable",
    "get",
    "get_by_name",
    "list_all",
    "list_due",
    "mark_run",
    "update",
]
