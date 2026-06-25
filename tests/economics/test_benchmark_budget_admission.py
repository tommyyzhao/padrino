"""US-264: benchmark budget admission helpers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.rulesets import mini7_v1
from padrino.db.models import (
    BudgetReservationSlot,
    Campaign,
    Game,
    Gauntlet,
    League,
    LlmCall,
    PromptVersion,
)
from padrino.economics.benchmark_admission import (
    GLOBAL_BENCHMARK_SCOPE_KEY,
    campaign_scope_key,
    cumulative_campaign_spend_usd,
    game_binding_key,
    reserve_benchmark_game_budget,
)
from padrino.economics.budget_reservations import claim_budget_slot
from padrino.settings import Settings

_NOW = datetime(2026, 6, 24, 12, tzinfo=UTC)


async def _live_slot_count(session: AsyncSession, *, binding_key: str) -> int:
    value = await session.scalar(
        select(func.count(BudgetReservationSlot.id)).where(
            BudgetReservationSlot.binding_key == binding_key,
            BudgetReservationSlot.released_at.is_(None),
        )
    )
    return int(value or 0)


async def _seed_campaign_game(
    session: AsyncSession,
    *,
    label: str,
    cost_usd: float | None,
) -> uuid.UUID:
    league = League(name=f"league-{label}", ruleset_id=mini7_v1.RULESET_ID, ranked=True)
    prompt = PromptVersion(
        ruleset_id=mini7_v1.RULESET_ID,
        version=f"v-{label}",
        system_prompt="system",
        developer_prompt="developer",
        response_schema={"type": "object"},
        prompt_hash=f"hash-{label}",
    )
    session.add_all([league, prompt])
    await session.flush()

    campaign = Campaign(
        campaign_seed=f"campaign-{label}",
        ruleset_id=mini7_v1.RULESET_ID,
        league_id=league.id,
        format="MIRROR",
        player_count=mini7_v1.PLAYER_COUNT,
        per_model_game_target=1,
        status="RUNNING",
        sigma_target=2.5,
        rank_stability_k=10,
    )
    session.add(campaign)
    await session.flush()

    gauntlet = Gauntlet(
        campaign_id=campaign.id,
        league_id=league.id,
        ruleset_id=mini7_v1.RULESET_ID,
        prompt_version_id=prompt.id,
        clone_count=1,
        gauntlet_seed=f"gauntlet-{label}",
        ranked=True,
        status="COMPLETED",
    )
    session.add(gauntlet)
    await session.flush()

    game = Game(
        gauntlet_id=gauntlet.id,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=f"game-{label}",
        status="COMPLETED",
    )
    session.add(game)
    await session.flush()

    if cost_usd is not None:
        session.add(
            LlmCall(
                game_id=game.id,
                public_player_id="P01",
                phase="DAY_1_DISCUSSION",
                request_json={},
                request_prompt_hash="prompt",
                status="ok",
                cost_usd=cost_usd,
            )
        )
    await session.flush()
    return campaign.id


async def _seed_empty_game(
    session: AsyncSession,
    *,
    label: str,
) -> uuid.UUID:
    game = Game(
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=f"game-{label}",
        status="CREATED",
    )
    session.add(game)
    await session.flush()
    return game.id


async def _seed_empty_campaign_game(
    session: AsyncSession,
    *,
    label: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    league = League(name=f"league-empty-{label}", ruleset_id=mini7_v1.RULESET_ID, ranked=True)
    prompt = PromptVersion(
        ruleset_id=mini7_v1.RULESET_ID,
        version=f"empty-v-{label}",
        system_prompt="system",
        developer_prompt="developer",
        response_schema={"type": "object"},
        prompt_hash=f"empty-hash-{label}",
    )
    session.add_all([league, prompt])
    await session.flush()

    campaign = Campaign(
        campaign_seed=f"empty-campaign-{label}",
        ruleset_id=mini7_v1.RULESET_ID,
        league_id=league.id,
        format="MIRROR",
        player_count=mini7_v1.PLAYER_COUNT,
        per_model_game_target=1,
        status="RUNNING",
        sigma_target=2.5,
        rank_stability_k=10,
    )
    session.add(campaign)
    await session.flush()

    gauntlet = Gauntlet(
        campaign_id=campaign.id,
        league_id=league.id,
        ruleset_id=mini7_v1.RULESET_ID,
        prompt_version_id=prompt.id,
        clone_count=1,
        gauntlet_seed=f"empty-gauntlet-{label}",
        ranked=True,
        status="RUNNING",
    )
    session.add(gauntlet)
    await session.flush()

    game = Game(
        gauntlet_id=gauntlet.id,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=f"empty-game-{label}",
        status="CREATED",
    )
    session.add(game)
    await session.flush()
    return game.id, campaign.id


@pytest.mark.asyncio
async def test_campaign_spend_is_attributed_through_gauntlet_campaign(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        campaign_a = await _seed_campaign_game(session, label="a", cost_usd=1.25)
        campaign_b = await _seed_campaign_game(session, label="b", cost_usd=None)

    async with session_factory() as session:
        spent_a = await cumulative_campaign_spend_usd(session, campaign_a)
        spent_b = await cumulative_campaign_spend_usd(session, campaign_b)

    assert spent_a == 1.25
    assert spent_b == 0.0


@pytest.mark.asyncio
async def test_reserve_benchmark_game_budget_releases_prior_live_binding_slots(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A reaped-then-retried campaign game cannot stack live slots per scope."""
    settings = Settings(
        padrino_global_spend_cap_usd=1.0,
        padrino_campaign_spend_cap_usd=1.0,
        padrino_benchmark_admission_reserve_usd=0.5,
    )
    async with session_factory() as session, session.begin():
        game_id, campaign_id = await _seed_empty_campaign_game(session, label="retry")
        binding_key = game_binding_key(game_id)
        prior_global = await claim_budget_slot(
            session,
            scope_key=GLOBAL_BENCHMARK_SCOPE_KEY,
            spent_usd=0.0,
            budget_usd=1.0,
            reserve_usd=0.5,
            now=_NOW,
            binding_key=binding_key,
        )
        prior_campaign = await claim_budget_slot(
            session,
            scope_key=campaign_scope_key(campaign_id),
            spent_usd=0.0,
            budget_usd=1.0,
            reserve_usd=0.5,
            now=_NOW,
            binding_key=binding_key,
        )
        assert prior_global is not None
        assert prior_campaign is not None

        decision = await reserve_benchmark_game_budget(
            session,
            settings,
            game_id=game_id,
            now=_NOW,
        )
        live_slots = await _live_slot_count(session, binding_key=binding_key)

    assert decision.allowed is True
    assert live_slots == 2
    assert decision.reservation is not None
    assert decision.reservation.global_slot_id != prior_global
    assert decision.reservation.campaign_slot_id != prior_campaign


@pytest.mark.asyncio
async def test_reclaimed_binding_capacity_is_reused_without_overshoot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        padrino_global_spend_cap_usd=0.5,
        padrino_campaign_spend_cap_usd=100.0,
        padrino_benchmark_admission_reserve_usd=0.5,
    )
    async with session_factory() as session, session.begin():
        retry_game_id = await _seed_empty_game(session, label="retry-cap")
        next_game_id = await _seed_empty_game(session, label="next-cap")
        binding_key = game_binding_key(retry_game_id)
        prior = await claim_budget_slot(
            session,
            scope_key=GLOBAL_BENCHMARK_SCOPE_KEY,
            spent_usd=0.0,
            budget_usd=0.5,
            reserve_usd=0.5,
            now=_NOW,
            binding_key=binding_key,
        )
        assert prior is not None

        retry_decision = await reserve_benchmark_game_budget(
            session,
            settings,
            game_id=retry_game_id,
            now=_NOW,
        )
        blocked_decision = await reserve_benchmark_game_budget(
            session,
            settings,
            game_id=next_game_id,
            now=_NOW,
        )

    assert retry_decision.allowed is True
    assert blocked_decision.allowed is False
    assert blocked_decision.reason == "global_budget_cap_reached"
