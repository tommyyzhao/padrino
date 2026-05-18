"""CRUD helpers for :class:`padrino.db.models.Gauntlet` and roster slots."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import Gauntlet, GauntletRosterSlot


def _aware(dt: datetime) -> datetime:
    """Coerce a naive datetime (e.g. from aiosqlite) to UTC.

    The application always writes timezone-aware UTC values; SQLite drops the
    tz on read, while Postgres preserves it. Treating naive rows as UTC keeps
    cross-dialect comparisons (stale-heartbeat detection) honest.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def create(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    ruleset_id: str,
    prompt_version_id: uuid.UUID,
    clone_count: int,
    gauntlet_seed: str,
    ranked: bool,
    status: str = "PENDING",
) -> Gauntlet:
    obj = Gauntlet(
        league_id=league_id,
        ruleset_id=ruleset_id,
        prompt_version_id=prompt_version_id,
        clone_count=clone_count,
        gauntlet_seed=gauntlet_seed,
        ranked=ranked,
        status=status,
    )
    session.add(obj)
    await session.flush()
    return obj


async def get(session: AsyncSession, gauntlet_id: uuid.UUID) -> Gauntlet | None:
    return await session.get(Gauntlet, gauntlet_id)


async def list_(
    session: AsyncSession,
    *,
    league_id: uuid.UUID | None = None,
    status: str | None = None,
    ranked: bool | None = None,
) -> list[Gauntlet]:
    stmt = select(Gauntlet)
    if league_id is not None:
        stmt = stmt.where(Gauntlet.league_id == league_id)
    if status is not None:
        stmt = stmt.where(Gauntlet.status == status)
    if ranked is not None:
        stmt = stmt.where(Gauntlet.ranked == ranked)
    stmt = stmt.order_by(Gauntlet.created_at, Gauntlet.id)
    result = await session.execute(stmt)
    return list(result.scalars())


async def add_roster_slot(
    session: AsyncSession,
    gauntlet_id: uuid.UUID,
    slot_index: int,
    agent_build_id: uuid.UUID,
) -> GauntletRosterSlot:
    slot = GauntletRosterSlot(
        gauntlet_id=gauntlet_id,
        slot_index=slot_index,
        agent_build_id=agent_build_id,
    )
    session.add(slot)
    await session.flush()
    return slot


async def list_roster_slots(
    session: AsyncSession,
    gauntlet_id: uuid.UUID,
) -> list[GauntletRosterSlot]:
    stmt = (
        select(GauntletRosterSlot)
        .where(GauntletRosterSlot.gauntlet_id == gauntlet_id)
        .order_by(GauntletRosterSlot.slot_index)
    )
    result = await session.execute(stmt)
    return list(result.scalars())


async def claim_oldest_pending(
    session: AsyncSession,
    *,
    now: datetime,
) -> Gauntlet | None:
    """Flip the oldest ``PENDING`` gauntlet to ``RUNNING`` and return it.

    Returns ``None`` when no pending gauntlets exist. The status flip and
    heartbeat stamp happen inside the caller-supplied session; the caller is
    expected to commit. SQLite's lack of ``FOR UPDATE`` is fine here because
    the scheduler is single-writer.
    """
    stmt = (
        select(Gauntlet)
        .where(Gauntlet.status == "PENDING")
        .order_by(Gauntlet.created_at, Gauntlet.id)
        .limit(1)
    )
    obj = (await session.execute(stmt)).scalars().first()
    if obj is None:
        return None
    obj.status = "RUNNING"
    obj.heartbeat_at = now
    await session.flush()
    return obj


async def update_heartbeat(
    session: AsyncSession,
    gauntlet_id: uuid.UUID,
    *,
    now: datetime,
) -> None:
    obj = await session.get(Gauntlet, gauntlet_id)
    if obj is None:
        return
    obj.heartbeat_at = now
    await session.flush()


async def mark_completed(
    session: AsyncSession,
    gauntlet_id: uuid.UUID,
    *,
    now: datetime,
) -> None:
    obj = await session.get(Gauntlet, gauntlet_id)
    if obj is None:
        return
    obj.status = "COMPLETED"
    obj.completed_at = now
    obj.heartbeat_at = None
    await session.flush()


async def count_by_status(session: AsyncSession, status: str) -> int:
    """Return the number of gauntlet rows with the given ``status``."""
    stmt = select(func.count()).select_from(Gauntlet).where(Gauntlet.status == status)
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def oldest_pending_created_at(session: AsyncSession) -> datetime | None:
    """Return ``MIN(created_at)`` over ``PENDING`` rows, or ``None``."""
    stmt = select(func.min(Gauntlet.created_at)).where(Gauntlet.status == "PENDING")
    result = await session.execute(stmt)
    value = result.scalar_one_or_none()
    if value is None:
        return None
    return _aware(value)


async def reset_stale_running(
    session: AsyncSession,
    *,
    older_than: datetime,
) -> list[uuid.UUID]:
    """Flip every ``RUNNING`` gauntlet whose heartbeat is older than ``older_than`` back to ``PENDING``.

    Returns the ids that were reset. Gauntlets with a NULL ``heartbeat_at``
    are also treated as stale because the only way the column is NULL on a
    RUNNING row is a crash between status flip and first heartbeat write.
    """
    stmt = select(Gauntlet).where(Gauntlet.status == "RUNNING")
    rows = list((await session.execute(stmt)).scalars().all())
    reset: list[uuid.UUID] = []
    cutoff = _aware(older_than)
    for obj in rows:
        hb = obj.heartbeat_at
        if hb is None or _aware(hb) < cutoff:
            obj.status = "PENDING"
            obj.heartbeat_at = None
            reset.append(obj.id)
    if reset:
        await session.flush()
    return reset
