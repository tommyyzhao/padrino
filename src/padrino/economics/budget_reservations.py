"""Shared atomic finite-slot budget reservations."""

from __future__ import annotations

import math
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import BudgetReservationSlot


def _budget_slot_count(budget_usd: float, reserve_usd: float) -> int:
    """Number of whole reservation slots admitted by a budget."""
    if budget_usd <= 0.0 or reserve_usd <= 0.0:
        return 0
    return math.floor(budget_usd / reserve_usd)


def _implicit_budget_used(spent_usd: float, reserve_usd: float) -> int:
    """Slots consumed by already-charged spend, rounded up fail-closed."""
    if spent_usd <= 0.0 or reserve_usd <= 0.0:
        return 0
    return math.ceil(spent_usd / reserve_usd)


def _next_free_index(*, implicit_used: int, physical: set[int]) -> int:
    """Lowest non-negative index not occupied by implicit or physical slots."""
    taken = set(range(max(implicit_used, 0))) | physical
    index = 0
    while index in taken:
        index += 1
    return index


async def _slot_indices(
    session: AsyncSession,
    *,
    scope_key: str,
) -> tuple[set[int], set[int]]:
    """Return ``(live_indices, all_indices)`` for one reservation scope."""
    stmt = select(BudgetReservationSlot.slot_index, BudgetReservationSlot.released_at).where(
        BudgetReservationSlot.scope_key == scope_key
    )
    live: set[int] = set()
    everything: set[int] = set()
    for slot_index, released_at in (await session.execute(stmt)).all():
        everything.add(int(slot_index))
        if released_at is None:
            live.add(int(slot_index))
    return live, everything


async def claim_budget_slot(
    session: AsyncSession,
    *,
    scope_key: str,
    spent_usd: float,
    budget_usd: float,
    reserve_usd: float,
    now: datetime,
    binding_key: str | None = None,
) -> uuid.UUID | None:
    """Atomically reserve one slice of ``scope_key``'s budget.

    The budget is divided into ``floor(budget_usd / reserve_usd)`` finite slots.
    Already-charged spend implicitly consumes the lowest slots; explicit rows
    claim distinct physical indices under a unique constraint. Concurrent
    claimers race inside a nested transaction and retry on the losing
    ``IntegrityError``. ``None`` means the budget is exhausted or disabled.
    """
    cap = _budget_slot_count(budget_usd, reserve_usd)
    if cap <= 0:
        return None

    implicit_used = min(_implicit_budget_used(spent_usd, reserve_usd), cap)
    for _ in range(cap + 1):
        live, everything = await _slot_indices(session, scope_key=scope_key)
        if implicit_used + len(live) >= cap:
            return None

        row = BudgetReservationSlot(
            scope_key=scope_key,
            slot_index=_next_free_index(implicit_used=implicit_used, physical=everything),
            reserved_at=now,
            binding_key=binding_key,
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            continue
        return row.id

    return None


async def release_budget_slot(
    session: AsyncSession,
    slot_id: uuid.UUID,
    *,
    released_at: datetime,
) -> bool:
    """Release one live reservation slot by id.

    Returns ``True`` only when an unreleased row was updated. The physical row is
    retained so the unique ``slot_index`` is never reused.
    """
    row = await session.get(BudgetReservationSlot, slot_id)
    if row is None or row.released_at is not None:
        return False
    row.released_at = released_at
    await session.flush()
    return True
