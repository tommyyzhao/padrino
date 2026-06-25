"""US-262: campaign tick hook composition and bounded materialization."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.rulesets import mini7_v1
from padrino.db.models import Campaign, CampaignPairing, Game, Gauntlet, LlmCall
from padrino.db.repositories import (
    agent_builds,
    campaigns,
    leagues,
    model_configs,
    prompt_versions,
    providers,
)
from padrino.economics.budget_reservations import claim_budget_slot, release_budget_slot
from padrino.scheduler.bootstrap import build_scheduled_gauntlet_tick_hook
from padrino.scheduler.campaign_tick import run_campaign_tick
from padrino.settings import Settings

_NOW = datetime(2026, 6, 24, 12, tzinfo=UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _seed_campaign(
    session: AsyncSession,
    *,
    campaign_seed: str,
    model_count: int = 14,
    per_model_game_target: int = 8,
) -> tuple[uuid.UUID, int]:
    league = await leagues.create(
        session,
        name=f"campaign-tick-{campaign_seed}",
        ruleset_id=mini7_v1.RULESET_ID,
        ranked=True,
    )
    prompt = await prompt_versions.create(
        session,
        ruleset_id=mini7_v1.RULESET_ID,
        version="v1",
        system_prompt="sys",
        developer_prompt="dev",
        response_schema={"type": "object"},
        prompt_hash=f"campaign-tick-{uuid.uuid4().hex}",
    )
    model_ids: list[str] = []
    for index in range(model_count):
        model_id = f"{campaign_seed}-model-{index:02d}"
        provider = await providers.create(
            session,
            name=f"{campaign_seed}-provider-{index}",
            auth_secret_ref=f"{campaign_seed.upper()}_{index}_KEY",
        )
        config = await model_configs.create(
            session,
            provider_id=provider.id,
            model_name=model_id,
            litellm_model_id=f"litellm/{model_id}",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        await agent_builds.create(
            session,
            display_name=f"{model_id}-active",
            model_config_id=config.id,
            prompt_version_id=prompt.id,
            adapter_version="2026.06",
            inference_params={},
            active=True,
        )
        model_ids.append(model_id)

    created = await campaigns.create_campaign_from_matrix(
        session,
        campaign_seed=campaign_seed,
        ruleset_id=mini7_v1.RULESET_ID,
        league_id=league.id,
        model_field=model_ids,
        format="MIRROR",
        per_model_game_target=per_model_game_target,
        sigma_target=2.5,
        rank_stability_k=10,
    )
    return created.campaign_id, len(created.matrix)


async def _cell_rows(
    session: AsyncSession,
    campaign_id: uuid.UUID,
) -> list[CampaignPairing]:
    result = await session.execute(
        select(CampaignPairing)
        .where(CampaignPairing.campaign_id == campaign_id)
        .order_by(CampaignPairing.cell_index)
    )
    return list(result.scalars())


async def _count_gauntlets(session: AsyncSession) -> int:
    count = await session.scalar(select(func.count()).select_from(Gauntlet))
    assert count is not None
    return count


async def test_composed_bootstrap_hook_runs_campaign_tick_and_existing_tick_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    async with session_factory() as session, session.begin():
        campaign_id, matrix_size = await _seed_campaign(
            session,
            campaign_seed="composed",
        )
    assert matrix_size > 1

    scheduled_calls: list[datetime] = []

    async def _fake_run_due_scheduled_gauntlets(
        passed_session_factory: async_sessionmaker[AsyncSession],
        *,
        now: datetime,
        settings: Settings,
        adapter_factory: Any = None,
    ) -> list[object]:
        assert passed_session_factory is session_factory
        assert settings.padrino_enable_campaign_tick is True
        assert adapter_factory is None
        scheduled_calls.append(now)
        return []

    monkeypatch.setattr(
        "padrino.scheduler.bootstrap.run_due_scheduled_gauntlets",
        _fake_run_due_scheduled_gauntlets,
    )
    hook = build_scheduled_gauntlet_tick_hook(
        session_factory,
        settings=Settings(
            padrino_enable_campaign_tick=True,
            padrino_campaign_materialize_batch_size=1,
        ),
        worker_id="campaign-worker",
    )

    await hook(_NOW)

    assert scheduled_calls == [_NOW]
    async with session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
        cells = await _cell_rows(session, campaign_id)
        gauntlet_count = await _count_gauntlets(session)

    assert campaign is not None
    assert campaign.status == campaigns.CAMPAIGN_STATUS_RUNNING
    assert campaign.leased_by == "campaign-worker"
    assert [cell.status for cell in cells].count(campaigns.CAMPAIGN_PAIRING_MATERIALIZED) == 1
    assert [cell.status for cell in cells].count(campaigns.CAMPAIGN_PAIRING_PENDING) == (
        matrix_size - 1
    )
    assert gauntlet_count == 1


async def test_campaign_tick_is_disabled_by_settings_gate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        campaign_id, matrix_size = await _seed_campaign(
            session,
            campaign_seed="disabled",
        )

    result = await run_campaign_tick(
        session_factory,
        now=_NOW,
        settings=Settings(padrino_enable_campaign_tick=False),
        worker_id="campaign-worker",
    )

    assert result.campaign_id is None
    assert result.materialized == ()
    async with session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
        cells = await _cell_rows(session, campaign_id)
        gauntlet_count = await _count_gauntlets(session)

    assert campaign is not None
    assert campaign.status == campaigns.CAMPAIGN_STATUS_PENDING
    assert [cell.status for cell in cells] == [campaigns.CAMPAIGN_PAIRING_PENDING] * matrix_size
    assert gauntlet_count == 0


async def test_campaign_tick_materializes_bounded_batches_and_finalizes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        campaign_id, matrix_size = await _seed_campaign(
            session,
            campaign_seed="bounded",
        )
    assert matrix_size > 2

    settings = Settings(
        padrino_enable_campaign_tick=True,
        padrino_campaign_materialize_batch_size=1,
    )
    for index in range(matrix_size):
        result = await run_campaign_tick(
            session_factory,
            now=_NOW + timedelta(seconds=index),
            settings=settings,
            worker_id="campaign-worker",
        )
        assert result.campaign_id == campaign_id
        assert [item.cell_index for item in result.materialized] == [index]

        async with session_factory() as session:
            cells = await _cell_rows(session, campaign_id)
            gauntlet_count = await _count_gauntlets(session)
        assert [cell.status for cell in cells].count(campaigns.CAMPAIGN_PAIRING_MATERIALIZED) == (
            index + 1
        )
        assert gauntlet_count == index + 1

    async with session_factory() as session, session.begin():
        cells = await _cell_rows(session, campaign_id)
        for cell in cells:
            cell.status = campaigns.CAMPAIGN_PAIRING_COMPLETED

    result = await run_campaign_tick(
        session_factory,
        now=_NOW + timedelta(seconds=matrix_size + 1),
        settings=settings,
        worker_id="campaign-worker",
    )

    assert result.finalized_campaign_id == campaign_id
    async with session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)

    assert campaign is not None
    assert campaign.status == campaigns.CAMPAIGN_STATUS_COMPLETED
    assert campaign.completed_at is not None
    assert _aware(campaign.completed_at) == _NOW + timedelta(seconds=matrix_size + 1)


async def test_campaign_tick_skips_campaign_at_cap_and_materializes_different_campaign(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        capped_id, _capped_size = await _seed_campaign(
            session,
            campaign_seed="capped",
            per_model_game_target=4,
        )
        open_id, _open_size = await _seed_campaign(
            session,
            campaign_seed="open",
            per_model_game_target=4,
        )
        first = await campaigns.materialize_next_batch(
            session,
            campaign_id=capped_id,
            batch_size=1,
            pair_count=1,
        )
        game = (
            (
                await session.execute(
                    select(Game)
                    .where(Game.gauntlet_id == first.materialized[0].gauntlet_id)
                    .limit(1)
                )
            )
            .scalars()
            .one()
        )
        session.add(
            LlmCall(
                game_id=game.id,
                public_player_id="P01",
                phase="DAY_1_DISCUSSION",
                request_json={},
                request_prompt_hash="prompt",
                status="ok",
                cost_usd=1.0,
            )
        )

    result = await run_campaign_tick(
        session_factory,
        now=_NOW,
        settings=Settings(
            padrino_enable_campaign_tick=True,
            padrino_campaign_materialize_batch_size=1,
            padrino_global_spend_cap_usd=10.0,
            padrino_campaign_spend_cap_usd=1.0,
            padrino_benchmark_admission_reserve_usd=0.5,
        ),
        worker_id="campaign-worker",
    )

    async with session_factory() as session:
        capped_cells = await _cell_rows(session, capped_id)
        open_cells = await _cell_rows(session, open_id)

    assert result.campaign_id == open_id
    assert [item.cell_index for item in result.materialized] == [0]
    assert [cell.status for cell in capped_cells].count(
        campaigns.CAMPAIGN_PAIRING_MATERIALIZED
    ) == 1
    assert [cell.status for cell in open_cells].count(campaigns.CAMPAIGN_PAIRING_MATERIALIZED) == 1


async def test_campaign_tick_resumes_after_budget_slot_is_released(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        campaign_id, _matrix_size = await _seed_campaign(
            session,
            campaign_seed="release-resume",
            per_model_game_target=4,
        )
        slot_id = await claim_budget_slot(
            session,
            scope_key=f"campaign:{campaign_id}",
            spent_usd=0.0,
            budget_usd=0.5,
            reserve_usd=0.5,
            now=_NOW,
        )
        assert slot_id is not None

    settings = Settings(
        padrino_enable_campaign_tick=True,
        padrino_campaign_materialize_batch_size=1,
        padrino_global_spend_cap_usd=10.0,
        padrino_campaign_spend_cap_usd=0.5,
        padrino_benchmark_admission_reserve_usd=0.5,
    )
    blocked = await run_campaign_tick(
        session_factory,
        now=_NOW,
        settings=settings,
        worker_id="campaign-worker",
    )

    async with session_factory() as session, session.begin():
        cells_after_block = await _cell_rows(session, campaign_id)
        released = await release_budget_slot(session, slot_id, released_at=_NOW)
        assert released is True

    resumed = await run_campaign_tick(
        session_factory,
        now=_NOW + timedelta(seconds=1),
        settings=settings,
        worker_id="campaign-worker",
    )

    async with session_factory() as session:
        cells_after_resume = await _cell_rows(session, campaign_id)

    assert blocked.campaign_id is None
    assert [cell.status for cell in cells_after_block] == [
        campaigns.CAMPAIGN_PAIRING_PENDING
    ] * len(cells_after_block)
    assert resumed.campaign_id == campaign_id
    assert [item.cell_index for item in resumed.materialized] == [0]
    assert [cell.status for cell in cells_after_resume].count(
        campaigns.CAMPAIGN_PAIRING_MATERIALIZED
    ) == 1


async def test_campaign_tick_uses_injected_clock_for_heartbeat_and_stale_reset(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        held_id, _held_size = await _seed_campaign(
            session,
            campaign_seed="held",
            per_model_game_target=4,
        )
        stale_id, _stale_size = await _seed_campaign(
            session,
            campaign_seed="stale",
            per_model_game_target=4,
        )
        held = await session.get(Campaign, held_id)
        stale = await session.get(Campaign, stale_id)
        assert held is not None
        assert stale is not None
        held.status = campaigns.CAMPAIGN_STATUS_RUNNING
        held.leased_by = "campaign-worker"
        held.lease_expires_at = _NOW + timedelta(seconds=5)
        held.heartbeat_at = _NOW - timedelta(seconds=10)
        stale.status = campaigns.CAMPAIGN_STATUS_RUNNING
        stale.leased_by = "other-worker"
        stale.lease_expires_at = _NOW + timedelta(seconds=20)
        stale.heartbeat_at = _NOW - timedelta(seconds=10)

    settings = Settings(
        padrino_enable_campaign_tick=True,
        padrino_campaign_materialize_batch_size=1,
        padrino_campaign_lease_ttl_seconds=30.0,
    )

    await run_campaign_tick(
        session_factory,
        now=_NOW,
        settings=settings,
        worker_id="campaign-worker",
    )
    async with session_factory() as session:
        held_after_first = await session.get(Campaign, held_id)
        stale_after_first = await session.get(Campaign, stale_id)

    assert held_after_first is not None
    assert held_after_first.heartbeat_at is not None
    assert _aware(held_after_first.heartbeat_at) == _NOW
    assert held_after_first.lease_expires_at is not None
    assert _aware(held_after_first.lease_expires_at) == _NOW + timedelta(seconds=30)
    assert stale_after_first is not None
    assert stale_after_first.status == campaigns.CAMPAIGN_STATUS_RUNNING
    assert stale_after_first.leased_by == "other-worker"

    advanced_now = _NOW + timedelta(seconds=21)
    await run_campaign_tick(
        session_factory,
        now=advanced_now,
        settings=settings,
        worker_id="campaign-worker",
    )
    async with session_factory() as session:
        held_after_second = await session.get(Campaign, held_id)
        stale_after_second = await session.get(Campaign, stale_id)

    assert held_after_second is not None
    assert held_after_second.heartbeat_at is not None
    assert _aware(held_after_second.heartbeat_at) == advanced_now
    assert held_after_second.lease_expires_at is not None
    assert _aware(held_after_second.lease_expires_at) == advanced_now + timedelta(seconds=30)
    assert stale_after_second is not None
    assert stale_after_second.status == campaigns.CAMPAIGN_STATUS_PENDING
    assert stale_after_second.leased_by is None
    assert stale_after_second.lease_expires_at is None
