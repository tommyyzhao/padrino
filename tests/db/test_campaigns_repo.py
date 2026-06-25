"""US-259: campaign repository and ledger-based materialization."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.rulesets import mini7_v1
from padrino.core.scheduling.pairing import PairingFormat, derive_campaign_cell_seed
from padrino.db.models import Campaign, CampaignPairing, Game, Gauntlet
from padrino.db.repositories import (
    agent_builds,
    campaigns,
    games,
    gauntlets,
    leagues,
    model_configs,
    prompt_versions,
    providers,
)

_NOW = datetime(2026, 6, 24, 12, tzinfo=UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _seed_campaign_world(
    session: AsyncSession,
    *,
    model_count: int = 10,
) -> tuple[uuid.UUID, uuid.UUID, list[str], dict[str, uuid.UUID]]:
    league = await leagues.create(
        session, name="campaign", ruleset_id=mini7_v1.RULESET_ID, ranked=True
    )
    prompt = await prompt_versions.create(
        session,
        ruleset_id=mini7_v1.RULESET_ID,
        version="v1",
        system_prompt="sys",
        developer_prompt="dev",
        response_schema={"type": "object"},
        prompt_hash=f"campaign-{uuid.uuid4().hex}",
    )
    model_ids: list[str] = []
    active_build_by_model: dict[str, uuid.UUID] = {}
    for index in range(model_count):
        provider = await providers.create(
            session,
            name=f"provider-{index}",
            auth_secret_ref=f"PROVIDER_{index}_KEY",
        )
        model_id = f"model-{index:02d}"
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
        inactive = await agent_builds.create(
            session,
            display_name=f"{model_id}-inactive",
            model_config_id=config.id,
            prompt_version_id=prompt.id,
            adapter_version="2026.06",
            inference_params={},
            active=False,
        )
        active = await agent_builds.create(
            session,
            display_name=f"{model_id}-active",
            model_config_id=config.id,
            prompt_version_id=prompt.id,
            adapter_version="2026.06",
            inference_params={},
            active=True,
        )
        model_ids.append(model_id)
        active_build_by_model[model_id] = active.id
        assert inactive.id != active.id
    return league.id, prompt.id, model_ids, active_build_by_model


async def _campaign_cell_rows(
    session: AsyncSession,
    campaign_id: uuid.UUID,
) -> list[CampaignPairing]:
    result = await session.execute(
        select(CampaignPairing)
        .where(CampaignPairing.campaign_id == campaign_id)
        .order_by(CampaignPairing.cell_index)
    )
    return list(result.scalars())


async def test_create_campaign_from_matrix_persists_cells_without_gauntlets_or_games(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, _prompt_id, model_ids, active_build_by_model = await _seed_campaign_world(
            session
        )

    async with session_factory() as session, session.begin():
        created = await campaigns.create_campaign_from_matrix(
            session,
            campaign_seed="campaign-create",
            ruleset_id=mini7_v1.RULESET_ID,
            league_id=league_id,
            model_field=model_ids,
            format=PairingFormat.MIRROR,
            per_model_game_target=4,
            sigma_target=2.5,
            rank_stability_k=10,
        )
        campaign_id = created.campaign_id
        expected_matrix = created.matrix

    async with session_factory() as session:
        cells = await _campaign_cell_rows(session, campaign_id)
        gauntlet_count = await session.scalar(select(func.count()).select_from(Gauntlet))
        game_count = await session.scalar(select(func.count()).select_from(Game))

    assert len(cells) == len(expected_matrix)
    assert gauntlet_count == 0
    assert game_count == 0
    assert cells
    expected_roster = [str(active_build_by_model[model_id]) for model_id in expected_matrix[0][1]]
    assert cells[0].status == campaigns.CAMPAIGN_PAIRING_PENDING
    assert cells[0].roster_json == expected_roster
    assert all(uuid.UUID(build_id) for build_id in cells[0].roster_json)
    assert set(cells[0].roster_json).isdisjoint(model_ids)


async def test_campaign_claim_heartbeat_and_stale_reset(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, _prompt_id, model_ids, _builds = await _seed_campaign_world(session)
        expired = await campaigns.create_campaign_from_matrix(
            session,
            campaign_seed="expired-campaign",
            ruleset_id=mini7_v1.RULESET_ID,
            league_id=league_id,
            model_field=model_ids,
            format="MIRROR",
            per_model_game_target=4,
            sigma_target=2.5,
            rank_stability_k=10,
        )
        fresh = await campaigns.create_campaign_from_matrix(
            session,
            campaign_seed="fresh-campaign",
            ruleset_id=mini7_v1.RULESET_ID,
            league_id=league_id,
            model_field=model_ids,
            format="MIRROR",
            per_model_game_target=4,
            sigma_target=2.5,
            rank_stability_k=10,
        )
        expired_row = await campaigns.get(session, expired.campaign_id)
        fresh_row = await campaigns.get(session, fresh.campaign_id)
        assert expired_row is not None
        assert fresh_row is not None
        expired_row.status = campaigns.CAMPAIGN_STATUS_RUNNING
        expired_row.leased_by = "dead-worker"
        expired_row.lease_expires_at = _NOW - timedelta(seconds=1)
        expired_row.heartbeat_at = _NOW - timedelta(seconds=30)
        fresh_row.status = campaigns.CAMPAIGN_STATUS_RUNNING
        fresh_row.leased_by = "live-worker"
        fresh_row.lease_expires_at = _NOW + timedelta(seconds=30)
        fresh_row.heartbeat_at = _NOW
        expired_id = expired.campaign_id
        fresh_id = fresh.campaign_id

    async with session_factory() as session, session.begin():
        reset_ids = await campaigns.reset_stale_campaigns(session, now=_NOW)

    assert reset_ids == [expired_id]

    async with session_factory() as session, session.begin():
        claimed = await campaigns.claim_campaign(
            session,
            now=_NOW,
            lease_ttl=timedelta(minutes=5),
            worker_id="worker-a",
        )
        assert claimed is not None
        await campaigns.update_heartbeat(
            session,
            claimed.id,
            now=_NOW + timedelta(seconds=5),
        )
        claimed_id = claimed.id

    assert claimed_id == expired_id
    async with session_factory() as session:
        expired_after = await campaigns.get(session, expired_id)
        fresh_after = await campaigns.get(session, fresh_id)

    assert expired_after is not None
    assert expired_after.status == campaigns.CAMPAIGN_STATUS_RUNNING
    assert expired_after.leased_by == "worker-a"
    assert expired_after.lease_expires_at is not None
    assert _aware(expired_after.lease_expires_at) == _NOW + timedelta(minutes=5)
    assert expired_after.heartbeat_at is not None
    assert _aware(expired_after.heartbeat_at) == _NOW + timedelta(seconds=5)
    assert fresh_after is not None
    assert fresh_after.leased_by == "live-worker"
    assert fresh_after.lease_expires_at is not None
    assert _aware(fresh_after.lease_expires_at) == _NOW + timedelta(seconds=30)


async def test_materialize_next_batch_is_bounded_and_links_cells(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, prompt_id, model_ids, active_build_by_model = await _seed_campaign_world(session)
        created = await campaigns.create_campaign_from_matrix(
            session,
            campaign_seed="campaign-materialize",
            ruleset_id=mini7_v1.RULESET_ID,
            league_id=league_id,
            model_field=model_ids,
            format="MIRROR",
            per_model_game_target=8,
            sigma_target=2.5,
            rank_stability_k=10,
        )
        campaign_id = created.campaign_id
        first_cell_index, first_model_roster = created.matrix[0]

    async with session_factory() as session, session.begin():
        result = await campaigns.materialize_next_batch(
            session,
            campaign_id=campaign_id,
            batch_size=2,
            pair_count=1,
        )

    assert [item.cell_index for item in result.materialized] == [0, 1]

    async with session_factory() as session:
        cells = await _campaign_cell_rows(session, campaign_id)
        first_cell = cells[0]
        assert [cell.status for cell in cells[:2]] == [
            campaigns.CAMPAIGN_PAIRING_MATERIALIZED,
            campaigns.CAMPAIGN_PAIRING_MATERIALIZED,
        ]
        assert all(cell.gauntlet_id is not None for cell in cells[:2])
        assert all(cell.status == campaigns.CAMPAIGN_PAIRING_PENDING for cell in cells[2:])
        gauntlet = await gauntlets.get(session, first_cell.gauntlet_id)  # type: ignore[arg-type]
        assert gauntlet is not None
        slots = await gauntlets.list_roster_slots(session, gauntlet.id)
        child_games = await games.list_by_gauntlet(session, gauntlet.id)

    assert gauntlet.campaign_id == campaign_id
    assert gauntlet.league_id == league_id
    assert gauntlet.ruleset_id == mini7_v1.RULESET_ID
    assert gauntlet.prompt_version_id == prompt_id
    assert gauntlet.gauntlet_seed == derive_campaign_cell_seed(
        "campaign-materialize",
        first_cell_index,
    )
    assert [slot.agent_build_id for slot in slots] == [
        active_build_by_model[model_id] for model_id in first_model_roster
    ]
    assert len(child_games) == 2


async def test_materialize_next_batch_resumes_from_ledger_without_recreating_cells(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, _prompt_id, model_ids, _builds = await _seed_campaign_world(session)
        created = await campaigns.create_campaign_from_matrix(
            session,
            campaign_seed="campaign-resume",
            ruleset_id=mini7_v1.RULESET_ID,
            league_id=league_id,
            model_field=model_ids,
            format="MIRROR",
            per_model_game_target=8,
            sigma_target=2.5,
            rank_stability_k=10,
        )
        campaign_id = created.campaign_id

    async with session_factory() as session, session.begin():
        first = await campaigns.materialize_next_batch(
            session,
            campaign_id=campaign_id,
            batch_size=1,
            pair_count=1,
        )
        first_gauntlet_ids = tuple(item.gauntlet_id for item in first.materialized)

    async with session_factory() as session, session.begin():
        second = await campaigns.materialize_next_batch(
            session,
            campaign_id=campaign_id,
            batch_size=2,
            pair_count=1,
        )

    assert [item.cell_index for item in first.materialized] == [0]
    assert [item.cell_index for item in second.materialized] == [1, 2]

    async with session_factory() as session:
        cells = await _campaign_cell_rows(session, campaign_id)
        all_gauntlet_ids = [
            cell.gauntlet_id
            for cell in cells
            if cell.status == campaigns.CAMPAIGN_PAIRING_MATERIALIZED
        ]
        gauntlet_count = await session.scalar(select(func.count()).select_from(Gauntlet))
        game_count = await session.scalar(select(func.count()).select_from(Game))

    assert first_gauntlet_ids[0] in all_gauntlet_ids
    assert len(all_gauntlet_ids) == len(set(all_gauntlet_ids)) == 3
    assert gauntlet_count == 3
    assert game_count == 6


async def test_campaign_cell_failure_dead_letters_after_bounded_attempts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, _prompt_id, model_ids, _builds = await _seed_campaign_world(session)
        created = await campaigns.create_campaign_from_matrix(
            session,
            campaign_seed="campaign-dead-letter",
            ruleset_id=mini7_v1.RULESET_ID,
            league_id=league_id,
            model_field=model_ids,
            format="MIRROR",
            per_model_game_target=4,
            sigma_target=2.5,
            rank_stability_k=10,
        )
        campaign_id = created.campaign_id

    async with session_factory() as session, session.begin():
        first = await campaigns.materialize_next_batch(
            session,
            campaign_id=campaign_id,
            batch_size=1,
            pair_count=1,
        )
        gauntlet_id = first.materialized[0].gauntlet_id
        first_failure = await campaigns.record_materialized_cell_failure(
            session,
            gauntlet_id=gauntlet_id,
            last_error="provider timeout attempt 1",
            last_error_kind="provider_transient",
            max_attempts=2,
            poison=False,
        )

    assert first_failure is not None
    assert first_failure.status == campaigns.CAMPAIGN_PAIRING_PENDING
    assert first_failure.attempt_count == 1

    async with session_factory() as session, session.begin():
        second = await campaigns.materialize_next_batch(
            session,
            campaign_id=campaign_id,
            batch_size=1,
            pair_count=1,
        )
        second_gauntlet_id = second.materialized[0].gauntlet_id
        assert second_gauntlet_id != gauntlet_id
        second_failure = await campaigns.record_materialized_cell_failure(
            session,
            gauntlet_id=second_gauntlet_id,
            last_error="provider timeout attempt 2",
            last_error_kind="provider_transient",
            max_attempts=2,
            poison=False,
        )

    assert second_failure is not None
    assert second_failure.status == campaigns.CAMPAIGN_PAIRING_DEAD_LETTER
    assert second_failure.attempt_count == 2
    assert second_failure.last_error == "provider_transient: provider timeout attempt 2"

    async with session_factory() as session, session.begin():
        retry = await campaigns.materialize_next_batch(
            session,
            campaign_id=campaign_id,
            batch_size=1,
            pair_count=1,
        )
        cells = await _campaign_cell_rows(session, campaign_id)

    assert retry.materialized[0].cell_index == 1
    assert cells[0].status == campaigns.CAMPAIGN_PAIRING_DEAD_LETTER


async def test_dead_letter_cells_are_excluded_and_queryable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, _prompt_id, model_ids, _builds = await _seed_campaign_world(session)
        created = await campaigns.create_campaign_from_matrix(
            session,
            campaign_seed="campaign-dead-letter-excluded",
            ruleset_id=mini7_v1.RULESET_ID,
            league_id=league_id,
            model_field=model_ids,
            format="MIRROR",
            per_model_game_target=4,
            sigma_target=2.5,
            rank_stability_k=10,
        )
        campaign_id = created.campaign_id
        cells = await _campaign_cell_rows(session, campaign_id)
        cells[0].status = campaigns.CAMPAIGN_PAIRING_DEAD_LETTER
        cells[0].attempt_count = 3
        cells[0].last_error = "replay_hash_mismatch: corrupt event chain"
        total_cells = len(cells)

    async with session_factory() as session, session.begin():
        result = await campaigns.materialize_next_batch(
            session,
            campaign_id=campaign_id,
            batch_size=total_cells,
            pair_count=1,
        )
        failures = await campaigns.list_dead_letter_cells(session, campaign_id)
        cells_after = await _campaign_cell_rows(session, campaign_id)

    assert [item.cell_index for item in result.materialized] == list(range(1, total_cells))
    assert cells_after[0].status == campaigns.CAMPAIGN_PAIRING_DEAD_LETTER
    assert all(cell.status == campaigns.CAMPAIGN_PAIRING_MATERIALIZED for cell in cells_after[1:])
    assert len(failures) == 1
    assert failures[0].cell_index == 0
    assert failures[0].attempt_count == 3
    assert failures[0].last_error == "corrupt event chain"
    assert failures[0].last_error_kind == "replay_hash_mismatch"


async def test_poison_campaign_cell_failure_dead_letters_immediately(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, _prompt_id, model_ids, _builds = await _seed_campaign_world(session)
        created = await campaigns.create_campaign_from_matrix(
            session,
            campaign_seed="campaign-poison-dead-letter",
            ruleset_id=mini7_v1.RULESET_ID,
            league_id=league_id,
            model_field=model_ids,
            format="MIRROR",
            per_model_game_target=4,
            sigma_target=2.5,
            rank_stability_k=10,
        )
        campaign_id = created.campaign_id

    async with session_factory() as session, session.begin():
        materialized = await campaigns.materialize_next_batch(
            session,
            campaign_id=campaign_id,
            batch_size=1,
            pair_count=1,
        )
        failure = await campaigns.record_materialized_cell_failure(
            session,
            gauntlet_id=materialized.materialized[0].gauntlet_id,
            last_error="replay hash mismatch at sequence 3",
            last_error_kind="replay_hash_mismatch",
            max_attempts=3,
            poison=True,
        )

    assert failure is not None
    assert failure.status == campaigns.CAMPAIGN_PAIRING_DEAD_LETTER
    assert failure.attempt_count == 1
    assert failure.last_error == "replay_hash_mismatch: replay hash mismatch at sequence 3"


async def test_campaign_progress_counts_terminal_cells_and_auto_finalizes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, _prompt_id, model_ids, _builds = await _seed_campaign_world(
            session,
            model_count=mini7_v1.PLAYER_COUNT,
        )
        created = await campaigns.create_campaign_from_matrix(
            session,
            campaign_seed="campaign-terminal-progress",
            ruleset_id=mini7_v1.RULESET_ID,
            league_id=league_id,
            model_field=model_ids,
            format="MIRROR",
            per_model_game_target=2,
            sigma_target=2.5,
            rank_stability_k=10,
        )
        campaign_id = created.campaign_id
        cells = await _campaign_cell_rows(session, campaign_id)
        for cell in cells[:-1]:
            cell.status = campaigns.CAMPAIGN_PAIRING_COMPLETED
        cells[0].status = campaigns.CAMPAIGN_PAIRING_DEAD_LETTER

    async with session_factory() as session, session.begin():
        progress = await campaigns.campaign_progress(session, campaign_id)
        finalized = await campaigns.finalize_campaign_if_done(
            session,
            campaign_id,
            now=_NOW,
        )
        row = await session.get(Campaign, campaign_id)

    assert progress is not None
    assert progress.done == len(cells) - 1
    assert progress.total == len(cells)
    assert finalized is None
    assert row is not None
    assert row.status != campaigns.CAMPAIGN_STATUS_COMPLETED
    assert row.completed_at is None

    async with session_factory() as session, session.begin():
        cells = await _campaign_cell_rows(session, campaign_id)
        cells[-1].status = campaigns.CAMPAIGN_PAIRING_COMPLETED
        progress = await campaigns.campaign_progress(session, campaign_id)
        finalized = await campaigns.finalize_campaign_if_done(
            session,
            campaign_id,
            now=_NOW,
        )

    assert progress is not None
    assert progress.done == progress.total == len(cells)
    assert finalized is not None
    assert finalized.status == campaigns.CAMPAIGN_STATUS_COMPLETED
    assert finalized.completed_at is not None
    assert _aware(finalized.completed_at) == _NOW
