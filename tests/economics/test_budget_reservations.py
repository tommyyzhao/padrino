"""US-263: shared atomic budget-reservation primitive."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import BudgetReservationSlot
from padrino.economics.budget_reservations import (
    claim_budget_slot,
    release_budget_slot,
    release_budget_slots_by_binding_key,
)

_NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)


async def _live_slot_count(session: AsyncSession, scope_key: str) -> int:
    result = await session.execute(
        select(func.count(BudgetReservationSlot.id)).where(
            BudgetReservationSlot.scope_key == scope_key,
            BudgetReservationSlot.released_at.is_(None),
        )
    )
    return int(result.scalar_one())


@pytest.mark.asyncio
async def test_claim_budget_slot_is_atomic_under_concurrent_claims(tmp_path: Path) -> None:
    """N simultaneous claims against K slots yield at most K successes."""
    from padrino.db.base import Base, create_engine, create_session_factory

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'budget-slots.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)

    try:
        ready = asyncio.Event()

        async def attempt() -> uuid.UUID | None:
            await ready.wait()
            async with session_factory() as session, session.begin():
                return await claim_budget_slot(
                    session,
                    scope_key="global:benchmark",
                    spent_usd=0.0,
                    budget_usd=2.0,
                    reserve_usd=0.5,
                    now=_NOW,
                )

        tasks = [asyncio.create_task(attempt()) for _ in range(12)]
        ready.set()
        claimed = await asyncio.gather(*tasks)

        async with session_factory() as session:
            live = await _live_slot_count(session, "global:benchmark")
    finally:
        await engine.dispose()

    successes = [slot_id for slot_id in claimed if slot_id is not None]
    assert len(successes) == 4
    assert live == 4


@pytest.mark.asyncio
async def test_claim_budget_slot_fails_closed_when_exhausted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Charged spend and undersized budgets create no explicit slot rows."""
    async with session_factory() as session, session.begin():
        exhausted = await claim_budget_slot(
            session,
            scope_key="global:spent",
            spent_usd=2.0,
            budget_usd=2.0,
            reserve_usd=0.5,
            now=_NOW,
        )
        below_reserve = await claim_budget_slot(
            session,
            scope_key="global:below-reserve",
            spent_usd=0.0,
            budget_usd=0.49,
            reserve_usd=0.5,
            now=_NOW,
        )

    async with session_factory() as session:
        rows = (await session.execute(select(BudgetReservationSlot))).scalars().all()

    assert exhausted is None
    assert below_reserve is None
    assert rows == []


@pytest.mark.asyncio
async def test_release_budget_slot_frees_live_capacity_for_reuse(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A released row stops consuming budget, while its physical index stays unique."""
    async with session_factory() as session, session.begin():
        first = await claim_budget_slot(
            session,
            scope_key="campaign:alpha",
            spent_usd=0.0,
            budget_usd=1.0,
            reserve_usd=0.5,
            now=_NOW,
        )
        second = await claim_budget_slot(
            session,
            scope_key="campaign:alpha",
            spent_usd=0.0,
            budget_usd=1.0,
            reserve_usd=0.5,
            now=_NOW,
        )
        blocked = await claim_budget_slot(
            session,
            scope_key="campaign:alpha",
            spent_usd=0.0,
            budget_usd=1.0,
            reserve_usd=0.5,
            now=_NOW,
        )
        assert first is not None
        assert second is not None
        assert blocked is None

        released = await release_budget_slot(session, first, released_at=_NOW)
        reused = await claim_budget_slot(
            session,
            scope_key="campaign:alpha",
            spent_usd=0.0,
            budget_usd=1.0,
            reserve_usd=0.5,
            now=_NOW,
        )

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(
                        BudgetReservationSlot.slot_index,
                        BudgetReservationSlot.released_at,
                    )
                    .where(BudgetReservationSlot.scope_key == "campaign:alpha")
                    .order_by(BudgetReservationSlot.slot_index)
                )
            )
            .tuples()
            .all()
        )

    assert released is True
    assert reused is not None
    assert [row[0] for row in rows] == [0, 1, 2]
    assert sum(1 for _, released_at in rows if released_at is None) == 2


@pytest.mark.asyncio
async def test_budget_slot_scopes_are_independent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Global and per-campaign scope keys consume independent finite slots."""
    async with session_factory() as session, session.begin():
        global_claims = [
            await claim_budget_slot(
                session,
                scope_key="global:benchmark",
                spent_usd=0.0,
                budget_usd=1.0,
                reserve_usd=0.5,
                now=_NOW,
            )
            for _ in range(3)
        ]
        campaign_claims = [
            await claim_budget_slot(
                session,
                scope_key="campaign:123",
                spent_usd=0.0,
                budget_usd=1.0,
                reserve_usd=0.5,
                now=_NOW,
            )
            for _ in range(3)
        ]

    assert [slot is not None for slot in global_claims] == [True, True, False]
    assert [slot is not None for slot in campaign_claims] == [True, True, False]


@pytest.mark.asyncio
async def test_release_budget_slots_by_binding_key_reclaims_all_live_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A crashed game binding can be released across every budget scope."""
    binding_key = "game:crashed"
    async with session_factory() as session, session.begin():
        global_slot = await claim_budget_slot(
            session,
            scope_key="global:benchmark",
            spent_usd=0.0,
            budget_usd=1.0,
            reserve_usd=0.5,
            now=_NOW,
            binding_key=binding_key,
        )
        campaign_slot = await claim_budget_slot(
            session,
            scope_key="campaign:abc",
            spent_usd=0.0,
            budget_usd=1.0,
            reserve_usd=0.5,
            now=_NOW,
            binding_key=binding_key,
        )
        unrelated_slot = await claim_budget_slot(
            session,
            scope_key="global:benchmark",
            spent_usd=0.0,
            budget_usd=1.0,
            reserve_usd=0.5,
            now=_NOW,
            binding_key="game:other",
        )
        assert global_slot is not None
        assert campaign_slot is not None
        assert unrelated_slot is not None

        released = await release_budget_slots_by_binding_key(
            session,
            binding_key,
            released_at=_NOW,
        )

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(
                        BudgetReservationSlot.binding_key,
                        BudgetReservationSlot.released_at,
                    ).order_by(BudgetReservationSlot.binding_key)
                )
            )
            .tuples()
            .all()
        )

    assert released == 2
    assert [
        (binding_key, released_at is None)
        for binding_key, released_at in rows
        if binding_key == "game:crashed"
    ] == [("game:crashed", False), ("game:crashed", False)]
    assert [
        (binding_key, released_at is None)
        for binding_key, released_at in rows
        if binding_key == "game:other"
    ] == [("game:other", True)]
