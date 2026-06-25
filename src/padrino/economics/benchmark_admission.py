"""Benchmark game-grain budget admission reservations."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import Game, Gauntlet, LlmCall
from padrino.economics.budget_reservations import claim_budget_slot, release_budget_slot
from padrino.economics.spend_governor import cumulative_spend_usd

GLOBAL_BENCHMARK_SCOPE_KEY = "global:benchmark"
BENCHMARK_GAME_BINDING_PREFIX = "game:"


class BenchmarkBudgetSettings(Protocol):
    """Settings surface required by benchmark budget admission."""

    @property
    def padrino_global_spend_cap_usd(self) -> float:
        """Global benchmark spend cap."""
        ...

    @property
    def padrino_campaign_spend_cap_usd(self) -> float:
        """Per-campaign benchmark spend cap."""
        ...

    @property
    def padrino_benchmark_admission_reserve_usd(self) -> float:
        """Estimated spend reserved by one benchmark game."""
        ...


@dataclasses.dataclass(frozen=True, slots=True)
class BenchmarkBudgetReservation:
    """Budget slots held for one admitted benchmark game."""

    game_id: uuid.UUID | None
    global_slot_id: uuid.UUID
    campaign_slot_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class BenchmarkAdmissionDecision:
    """Result of one benchmark budget admission attempt."""

    allowed: bool
    reason: str
    reservation: BenchmarkBudgetReservation | None = None


def campaign_scope_key(campaign_id: uuid.UUID) -> str:
    """Return the opaque budget-reservation scope for one campaign."""
    return f"campaign:{campaign_id}"


def game_binding_key(game_id: uuid.UUID) -> str:
    """Return the budget-reservation binding key for one benchmark game."""
    return f"{BENCHMARK_GAME_BINDING_PREFIX}{game_id}"


async def cumulative_campaign_spend_usd(
    session: AsyncSession,
    campaign_id: uuid.UUID,
) -> float:
    """Return total LLM spend for games whose gauntlet belongs to ``campaign_id``."""
    stmt = (
        select(func.coalesce(func.sum(LlmCall.cost_usd), 0.0))
        .join(Game, LlmCall.game_id == Game.id)
        .join(Gauntlet, Game.gauntlet_id == Gauntlet.id)
        .where(Gauntlet.campaign_id == campaign_id)
    )
    value = (await session.execute(stmt)).scalar_one()
    return float(value) if value is not None else 0.0


async def campaign_id_for_game(
    session: AsyncSession,
    game_id: uuid.UUID,
) -> uuid.UUID | None:
    """Return the owning campaign id for ``game_id``, if it has one."""
    stmt = (
        select(Gauntlet.campaign_id)
        .join(Game, Game.gauntlet_id == Gauntlet.id)
        .where(Game.id == game_id)
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def reserve_benchmark_budget(
    session: AsyncSession,
    settings: BenchmarkBudgetSettings,
    *,
    now: datetime,
    binding_key: str,
    game_id: uuid.UUID | None = None,
    campaign_id: uuid.UUID | None = None,
) -> BenchmarkAdmissionDecision:
    """Atomically reserve global and optional per-campaign budget for one game."""
    reserve_usd = settings.padrino_benchmark_admission_reserve_usd
    global_slot_id = await claim_budget_slot(
        session,
        scope_key=GLOBAL_BENCHMARK_SCOPE_KEY,
        spent_usd=await cumulative_spend_usd(session),
        budget_usd=settings.padrino_global_spend_cap_usd,
        reserve_usd=reserve_usd,
        now=now,
        binding_key=binding_key,
    )
    if global_slot_id is None:
        return BenchmarkAdmissionDecision(
            allowed=False,
            reason="global_budget_cap_reached",
        )

    campaign_slot_id: uuid.UUID | None = None
    if campaign_id is not None:
        campaign_slot_id = await claim_budget_slot(
            session,
            scope_key=campaign_scope_key(campaign_id),
            spent_usd=await cumulative_campaign_spend_usd(session, campaign_id),
            budget_usd=settings.padrino_campaign_spend_cap_usd,
            reserve_usd=reserve_usd,
            now=now,
            binding_key=binding_key,
        )
        if campaign_slot_id is None:
            await release_budget_slot(session, global_slot_id, released_at=now)
            return BenchmarkAdmissionDecision(
                allowed=False,
                reason="campaign_budget_cap_reached",
            )

    return BenchmarkAdmissionDecision(
        allowed=True,
        reason="admitted",
        reservation=BenchmarkBudgetReservation(
            game_id=game_id,
            global_slot_id=global_slot_id,
            campaign_slot_id=campaign_slot_id,
        ),
    )


async def reserve_benchmark_game_budget(
    session: AsyncSession,
    settings: BenchmarkBudgetSettings,
    *,
    game_id: uuid.UUID,
    now: datetime,
) -> BenchmarkAdmissionDecision:
    """Reserve budget for a concrete benchmark game row."""
    return await reserve_benchmark_budget(
        session,
        settings,
        now=now,
        binding_key=game_binding_key(game_id),
        game_id=game_id,
        campaign_id=await campaign_id_for_game(session, game_id),
    )


async def release_benchmark_budget_reservation(
    session: AsyncSession,
    reservation: BenchmarkBudgetReservation,
    *,
    released_at: datetime,
) -> None:
    """Release live budget slots held for an admitted game."""
    await release_budget_slot(session, reservation.global_slot_id, released_at=released_at)
    if reservation.campaign_slot_id is not None:
        await release_budget_slot(session, reservation.campaign_slot_id, released_at=released_at)


__all__ = [
    "BENCHMARK_GAME_BINDING_PREFIX",
    "GLOBAL_BENCHMARK_SCOPE_KEY",
    "BenchmarkAdmissionDecision",
    "BenchmarkBudgetReservation",
    "BenchmarkBudgetSettings",
    "campaign_id_for_game",
    "campaign_scope_key",
    "cumulative_campaign_spend_usd",
    "game_binding_key",
    "release_benchmark_budget_reservation",
    "reserve_benchmark_budget",
    "reserve_benchmark_game_budget",
]
