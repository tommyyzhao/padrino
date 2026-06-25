"""US-264: benchmark budget admission helpers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.rulesets import mini7_v1
from padrino.db.models import Campaign, Game, Gauntlet, League, LlmCall, PromptVersion
from padrino.economics.benchmark_admission import cumulative_campaign_spend_usd

_NOW = datetime(2026, 6, 24, 12, tzinfo=UTC)


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
