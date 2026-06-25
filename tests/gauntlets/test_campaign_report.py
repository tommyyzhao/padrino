"""US-268: campaign report cost, progress, ETA, convergence, and CLI JSON."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from padrino.cli import app
from padrino.core.enums import Faction, RatingContextKind, Role
from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import CampaignPairing, GameSeat, LlmCall, Rating, RatingEvent
from padrino.db.repositories import (
    agent_builds,
    campaigns,
    games,
    gauntlets,
    leagues,
    model_configs,
    prompt_versions,
    providers,
    rating_contexts,
)
from padrino.economics.human_cost_governance import (
    PRICE_BASIS_FALLBACK_TABLE,
    fallback_price_table_version,
)
from padrino.gauntlets.campaign_report import build_campaign_report
from padrino.ratings.openskill_service import SCOPE_FACTION, SCOPE_GLOBAL, SCOPE_VALUE_GLOBAL
from padrino.settings import Settings

_NOW = datetime(2026, 6, 24, 12, tzinfo=UTC)


async def _seed_builds(
    session: AsyncSession,
    *,
    model_names: tuple[str, ...] = (
        "atlas",
        "boreal",
        "cygnus",
        "delta",
        "ember",
        "fjord",
        "glyph",
    ),
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    league = await leagues.create(
        session,
        name=f"campaign-report-{uuid.uuid4().hex}",
        ruleset_id=mini7_v1.RULESET_ID,
        ranked=True,
    )
    prompt = await prompt_versions.create(
        session,
        ruleset_id=mini7_v1.RULESET_ID,
        version=f"report-{uuid.uuid4().hex}",
        system_prompt="sys",
        developer_prompt="dev",
        response_schema={"type": "object"},
        prompt_hash=f"campaign-report-{uuid.uuid4().hex}",
    )
    provider = await providers.create(
        session,
        name="report-provider",
        auth_secret_ref="REPORT_PROVIDER_KEY",
    )
    build_ids: list[uuid.UUID] = []
    for name in model_names:
        config = await model_configs.create(
            session,
            provider_id=provider.id,
            model_name=name,
            litellm_model_id=f"test/{name}",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=1024,
            supports_structured_outputs=True,
        )
        build = await agent_builds.create(
            session,
            display_name=f"{name} build",
            model_config_id=config.id,
            prompt_version_id=prompt.id,
            adapter_version="2026.06",
            inference_params={},
            active=True,
        )
        build_ids.append(build.id)
    return league.id, build_ids


async def _add_game_with_cost(
    session: AsyncSession,
    *,
    gauntlet_id: uuid.UUID,
    build_ids: list[uuid.UUID],
    seed_suffix: str,
    started_offset: int,
    duration_seconds: int,
    costs: tuple[float, ...],
) -> uuid.UUID:
    game = await games.create(
        session,
        gauntlet_id=gauntlet_id,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=f"campaign-report-game-{seed_suffix}",
        status="COMPLETED",
    )
    game.created_at = _NOW + timedelta(seconds=started_offset)
    game.started_at = game.created_at
    game.completed_at = game.created_at + timedelta(seconds=duration_seconds)
    game.terminal_result = {"winner": Faction.TOWN.value}
    for index, build_id in enumerate(build_ids):
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id=f"P{index + 1:02d}",
                seat_index=index,
                agent_build_id=build_id,
                role=Role.VILLAGER.value,
                faction=Faction.TOWN.value if index != len(build_ids) - 1 else Faction.MAFIA.value,
                alive=True,
            )
        )
    for index, cost in enumerate(costs):
        session.add(
            LlmCall(
                game_id=game.id,
                agent_build_id=build_ids[index % len(build_ids)],
                public_player_id=f"P{index + 1:02d}",
                phase="DAY_1_DISCUSSION_ROUND_1",
                request_json={"phase": "DAY_1_DISCUSSION_ROUND_1"},
                request_prompt_hash="report-prompt",
                raw_response="{}",
                parsed_response={},
                status="ok",
                input_tokens=100,
                output_tokens=50,
                cost_usd=cost,
                price_basis=PRICE_BASIS_FALLBACK_TABLE,
                price_table_version="historical-table-v1",
                created_at=game.created_at + timedelta(seconds=index),
            )
        )
    return game.id


async def _seed_campaign_report_world(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    async with session_factory() as session, session.begin():
        league_id, build_ids = await _seed_builds(session)
        created = await campaigns.create_campaign_from_matrix(
            session,
            campaign_seed=f"campaign-report-{uuid.uuid4().hex}",
            ruleset_id=mini7_v1.RULESET_ID,
            league_id=league_id,
            model_field=["atlas", "boreal", "cygnus", "delta", "ember", "fjord", "glyph"],
            format="MIRROR",
            per_model_game_target=2,
            sigma_target=2.5,
            rank_stability_k=3,
        )
        campaign_id = created.campaign_id
        cells = list(
            (
                await session.execute(
                    select(CampaignPairing)
                    .where(CampaignPairing.campaign_id == campaign_id)
                    .order_by(CampaignPairing.cell_index)
                )
            )
            .scalars()
            .all()
        )
        first_build = await agent_builds.get(session, build_ids[0])
        assert first_build is not None
        first_gauntlet = await gauntlets.create(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=first_build.prompt_version_id,
            clone_count=2,
            gauntlet_seed="report-gauntlet-completed",
            ranked=True,
            campaign_id=campaign_id,
        )
        second_gauntlet = await gauntlets.create(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=first_build.prompt_version_id,
            clone_count=1,
            gauntlet_seed="report-gauntlet-running",
            ranked=True,
            campaign_id=campaign_id,
        )
        cells[0].status = campaigns.CAMPAIGN_PAIRING_COMPLETED
        cells[0].gauntlet_id = first_gauntlet.id
        cells[1].status = campaigns.CAMPAIGN_PAIRING_DEAD_LETTER
        cells[1].gauntlet_id = second_gauntlet.id
        cells[1].attempt_count = 2
        cells[1].last_error = "provider_transient: retries exhausted"
        cells[2].status = campaigns.CAMPAIGN_PAIRING_MATERIALIZED
        cells[2].gauntlet_id = second_gauntlet.id
        if len(cells) > 3:
            cells[3].status = campaigns.CAMPAIGN_PAIRING_PENDING

        first_game_id = await _add_game_with_cost(
            session,
            gauntlet_id=first_gauntlet.id,
            build_ids=build_ids,
            seed_suffix="a",
            started_offset=0,
            duration_seconds=40,
            costs=(0.50, 0.25),
        )
        await _add_game_with_cost(
            session,
            gauntlet_id=first_gauntlet.id,
            build_ids=build_ids,
            seed_suffix="b",
            started_offset=50,
            duration_seconds=50,
            costs=(0.75,),
        )
        await _add_game_with_cost(
            session,
            gauntlet_id=second_gauntlet.id,
            build_ids=build_ids,
            seed_suffix="c",
            started_offset=120,
            duration_seconds=30,
            costs=(0.40,),
        )

        context = await rating_contexts.get_by_ruleset_kind(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            kind=RatingContextKind.CANONICAL_TEAM,
        )
        assert context is not None
        session.add_all(
            [
                Rating(
                    league_id=league_id,
                    ruleset_id=mini7_v1.RULESET_ID,
                    rating_context_id=context.id,
                    agent_build_id=build_ids[0],
                    scope_type=SCOPE_GLOBAL,
                    scope_value=SCOPE_VALUE_GLOBAL,
                    mu=31.0,
                    sigma=2.0,
                    conservative_score=25.0,
                    games=12,
                    updated_at=_NOW + timedelta(minutes=1),
                ),
                Rating(
                    league_id=league_id,
                    ruleset_id=mini7_v1.RULESET_ID,
                    rating_context_id=context.id,
                    agent_build_id=build_ids[1],
                    scope_type=SCOPE_GLOBAL,
                    scope_value=SCOPE_VALUE_GLOBAL,
                    mu=27.0,
                    sigma=3.0,
                    conservative_score=18.0,
                    games=5,
                    updated_at=_NOW + timedelta(minutes=2),
                ),
                Rating(
                    league_id=league_id,
                    ruleset_id=mini7_v1.RULESET_ID,
                    rating_context_id=context.id,
                    agent_build_id=build_ids[0],
                    scope_type=SCOPE_FACTION,
                    scope_value=Faction.TOWN.value,
                    mu=30.0,
                    sigma=2.4,
                    conservative_score=22.8,
                    games=8,
                    updated_at=_NOW + timedelta(minutes=3),
                ),
            ]
        )
        session.add_all(
            [
                RatingEvent(
                    league_id=league_id,
                    game_id=first_game_id,
                    ruleset_id=mini7_v1.RULESET_ID,
                    rating_context_id=context.id,
                    game_seed="campaign-report-game-a",
                    team_outcome=Faction.TOWN.value,
                    agent_build_id=build_ids[0],
                    public_player_id="P01",
                    scope_type=SCOPE_GLOBAL,
                    scope_value=SCOPE_VALUE_GLOBAL,
                    before_mu=30.0,
                    before_sigma=2.1,
                    after_mu=31.0,
                    after_sigma=2.0,
                    created_at=_NOW + timedelta(seconds=200),
                ),
                RatingEvent(
                    league_id=league_id,
                    game_id=first_game_id,
                    ruleset_id=mini7_v1.RULESET_ID,
                    rating_context_id=context.id,
                    game_seed="campaign-report-game-a",
                    team_outcome=Faction.TOWN.value,
                    agent_build_id=build_ids[1],
                    public_player_id="P02",
                    scope_type=SCOPE_GLOBAL,
                    scope_value=SCOPE_VALUE_GLOBAL,
                    before_mu=26.5,
                    before_sigma=3.1,
                    after_mu=27.0,
                    after_sigma=3.0,
                    created_at=_NOW + timedelta(seconds=210),
                ),
                RatingEvent(
                    league_id=league_id,
                    game_id=first_game_id,
                    ruleset_id=mini7_v1.RULESET_ID,
                    rating_context_id=context.id,
                    game_seed="campaign-report-game-a",
                    team_outcome=Faction.TOWN.value,
                    agent_build_id=build_ids[0],
                    public_player_id="P01",
                    scope_type=SCOPE_FACTION,
                    scope_value=Faction.TOWN.value,
                    before_mu=29.0,
                    before_sigma=2.5,
                    after_mu=30.0,
                    after_sigma=2.4,
                    created_at=_NOW + timedelta(seconds=220),
                ),
            ]
        )
    return campaign_id, league_id, build_ids


async def test_campaign_report_returns_cost_progress_eta_and_dead_letters(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    campaign_id, _league_id, _build_ids = await _seed_campaign_report_world(session_factory)

    async with session_factory() as session:
        report = await build_campaign_report(session, campaign_id)

    assert report.campaign_id == campaign_id
    assert report.total_cost_usd == pytest.approx(1.90)
    assert report.progress.done_cells == 2
    assert report.progress.total_cells >= 4
    assert report.progress.completed == 1
    assert report.progress.dead_letter == 1
    assert report.progress.materialized >= 1
    assert report.progress.pending >= 1
    assert report.eta.remaining_cells == report.progress.total_cells - report.progress.done_cells
    assert report.eta.observed_cells == 2
    assert report.eta.observed_avg_cost_per_cell_usd == pytest.approx(0.95)
    assert report.eta.estimated_remaining_cost_usd == pytest.approx(
        0.95 * report.eta.remaining_cells
    )
    assert report.eta.observed_avg_duration_seconds == pytest.approx(100.0)
    assert report.eta.estimated_remaining_seconds == pytest.approx(
        100.0 * report.eta.remaining_cells
    )
    assert len(report.dead_letters) == 1
    assert report.dead_letters[0].cell_index == 1
    assert report.dead_letters[0].last_error_kind == "provider_transient"
    assert report.dead_letters[0].last_error == "retries exhausted"


async def test_campaign_report_includes_per_scope_convergence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    campaign_id, _league_id, build_ids = await _seed_campaign_report_world(session_factory)

    async with session_factory() as session:
        report = await build_campaign_report(session, campaign_id)

    by_scope = {
        (item.scope_kind, item.entity_id, item.scope_type, item.scope_value): item
        for item in report.convergence
    }
    global_item = by_scope[("ruleset", str(build_ids[0]), SCOPE_GLOBAL, SCOPE_VALUE_GLOBAL)]
    assert global_item.games == 12
    assert global_item.sigma == pytest.approx(2.0)
    assert global_item.provisional is False
    assert global_item.rank == 1
    assert global_item.rank_stability.window_size == 3
    assert global_item.rank_stability.observed_updates == 2
    assert global_item.rank_stability.rank_delta == 0

    faction_item = by_scope[("faction", str(build_ids[0]), SCOPE_FACTION, Faction.TOWN.value)]
    assert faction_item.games == 8
    assert faction_item.provisional is True
    assert faction_item.rank_stability.observed_updates == 1

    model_items = [item for item in report.convergence if item.scope_kind == "model"]
    assert {item.scope_type for item in model_items} == {SCOPE_GLOBAL}
    assert all(item.rank_stability.window_size == 3 for item in model_items)
    assert any(item.entity_label == "atlas" and item.games == 3 for item in model_items)


async def test_campaign_report_uses_stamped_historical_costs_after_rate_change(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    campaign_id, _league_id, _build_ids = await _seed_campaign_report_world(session_factory)

    async with session_factory() as session:
        first = await build_campaign_report(session, campaign_id)

    changed_settings = Settings(
        padrino_human_fallback_token_price_per_1k={
            "test/atlas": (999.0, 999.0),
            "test/boreal": (999.0, 999.0),
        },
    )
    assert fallback_price_table_version(changed_settings) != "historical-table-v1"

    async with session_factory() as session:
        second = await build_campaign_report(session, campaign_id)

    assert second.total_cost_usd == pytest.approx(first.total_cost_usd)
    assert second.eta.observed_avg_cost_per_cell_usd == pytest.approx(
        first.eta.observed_avg_cost_per_cell_usd
    )


async def _copy_fixture_to_sqlite(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    db_url: str,
) -> uuid.UUID:
    campaign_id, _league_id, _build_ids = await _seed_campaign_report_world(session_factory)
    engine = create_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        target_factory = create_session_factory(engine)
        source_tables = Base.metadata.sorted_tables
        async with session_factory() as source, target_factory() as target, target.begin():
            for table in source_tables:
                rows = (await source.execute(select(table))).mappings().all()
                if rows:
                    await target.execute(table.insert(), [dict(row) for row in rows])
    finally:
        await engine.dispose()
    return campaign_id


async def test_campaign_report_cli_outputs_json_and_summary(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'campaign-report.sqlite'}"
    campaign_id = await _copy_fixture_to_sqlite(session_factory, db_url=db_url)
    runner = CliRunner()

    json_result = await asyncio.to_thread(
        runner.invoke,
        app,
        ["campaign", "report", str(campaign_id), "--db-url", db_url, "--format", "json"],
    )

    assert json_result.exit_code == 0, json_result.output
    payload: dict[str, Any] = json.loads(json_result.stdout)
    assert payload["campaign_id"] == str(campaign_id)
    assert payload["total_cost_usd"] == pytest.approx(1.90)
    assert payload["progress"]["completed"] == 1
    assert payload["dead_letters"][0]["last_error"] == "retries exhausted"
    assert payload["convergence"]

    summary_result = await asyncio.to_thread(
        runner.invoke,
        app,
        ["campaign", "report", str(campaign_id), "--db-url", db_url, "--format", "summary"],
    )

    assert summary_result.exit_code == 0, summary_result.output
    assert f"Campaign {campaign_id}" in summary_result.stdout
    assert "cost=$1.9000" in summary_result.stdout
