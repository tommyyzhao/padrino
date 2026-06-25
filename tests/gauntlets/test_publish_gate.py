"""US-269: uncertainty-based campaign publish gate."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.event_log import EventLog
from padrino.core.enums import Faction, RatingContextKind, Role
from padrino.core.rulesets import mini7_v1
from padrino.db.models import (
    Campaign,
    CampaignPairing,
    GameEvent,
    GameSeat,
    LlmCall,
    PlacementRating,
    Rating,
    RatingContext,
    RatingEvent,
)
from padrino.db.repositories import (
    agent_builds,
    games,
    gauntlets,
    leagues,
    model_configs,
    prompt_versions,
    providers,
    rating_contexts,
)
from padrino.economics.human_cost_governance import PRICE_BASIS_FALLBACK_TABLE
from padrino.gauntlets.publish_gate import evaluate_publish_gate
from padrino.ratings.openskill_service import SCOPE_GLOBAL, SCOPE_VALUE_GLOBAL

_NOW = datetime(2026, 6, 24, 13, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class PublishGateWorld:
    campaign_id: uuid.UUID
    league_id: uuid.UUID
    build_ids: tuple[uuid.UUID, ...]
    game_id: uuid.UUID
    event_ids: tuple[uuid.UUID, ...]
    llm_call_ids: tuple[uuid.UUID, ...]


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
        name=f"publish-gate-{uuid.uuid4().hex}",
        ruleset_id=mini7_v1.RULESET_ID,
        ranked=True,
    )
    prompt = await prompt_versions.create(
        session,
        ruleset_id=mini7_v1.RULESET_ID,
        version=f"publish-gate-{uuid.uuid4().hex}",
        system_prompt="sys",
        developer_prompt="dev",
        response_schema={"type": "object"},
        prompt_hash=f"publish-gate-{uuid.uuid4().hex}",
    )
    provider = await providers.create(
        session,
        name=f"publish-gate-provider-{uuid.uuid4().hex}",
        auth_secret_ref="PUBLISH_GATE_PROVIDER_KEY",
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


def _append_event_row(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    event_log: EventLog,
    event_type: str,
    visibility: str = "PUBLIC",
    payload: dict[str, object] | None = None,
) -> GameEvent:
    body: dict[str, Any] = {
        "event_type": event_type,
        "sequence": len(event_log.events),
        "phase": "DAY_1_DISCUSSION_ROUND_1",
        "visibility": visibility,
        "actor_player_id": None,
        "payload": payload or {"message": event_type},
    }
    stored = event_log.append(body)
    row = GameEvent(
        game_id=game_id,
        sequence=stored.sequence,
        event_type=event_type,
        phase=str(body["phase"]),
        visibility=visibility,
        actor_player_id=None,
        payload=dict(body["payload"]),
        prev_event_hash=stored.prev_event_hash,
        event_hash=stored.event_hash,
        created_at=_NOW + timedelta(seconds=stored.sequence),
    )
    session.add(row)
    return row


async def _seed_ready_world(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    campaign_status: str = "COMPLETED",
) -> PublishGateWorld:
    async with session_factory() as session, session.begin():
        league_id, build_ids = await _seed_builds(session)
        campaign = Campaign(
            campaign_seed=f"publish-gate-{uuid.uuid4().hex}",
            ruleset_id=mini7_v1.RULESET_ID,
            league_id=league_id,
            format="MIRROR",
            player_count=mini7_v1.PLAYER_COUNT,
            per_model_game_target=1,
            status=campaign_status,
            completed_at=_NOW if campaign_status == "COMPLETED" else None,
            sigma_target=2.5,
            rank_stability_k=1,
        )
        session.add(campaign)
        await session.flush()

        first_build = await agent_builds.get(session, build_ids[0])
        assert first_build is not None
        gauntlet = await gauntlets.create(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=first_build.prompt_version_id,
            clone_count=1,
            gauntlet_seed=f"publish-gate-gauntlet-{uuid.uuid4().hex}",
            ranked=True,
            status="COMPLETED",
            campaign_id=campaign.id,
        )
        session.add(
            CampaignPairing(
                campaign_id=campaign.id,
                cell_index=0,
                roster_json=[str(build_id) for build_id in build_ids],
                status="COMPLETED",
                attempt_count=1,
                gauntlet_id=gauntlet.id,
            )
        )
        game = await games.create(
            session,
            gauntlet_id=gauntlet.id,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=f"publish-gate-game-{uuid.uuid4().hex}",
            status="COMPLETED",
        )
        game.created_at = _NOW
        game.started_at = _NOW
        game.completed_at = _NOW + timedelta(seconds=45)
        game.terminal_result = {"winner": Faction.TOWN.value}

        for index, build_id in enumerate(build_ids):
            session.add(
                GameSeat(
                    game_id=game.id,
                    public_player_id=f"P{index + 1:02d}",
                    seat_index=index,
                    agent_build_id=build_id,
                    role=Role.VILLAGER.value,
                    faction=(
                        Faction.TOWN.value if index != len(build_ids) - 1 else Faction.MAFIA.value
                    ),
                    alive=True,
                )
            )

        llm_call_ids: list[uuid.UUID] = []
        for index, build_id in enumerate(build_ids):
            call = LlmCall(
                game_id=game.id,
                agent_build_id=build_id,
                public_player_id=f"P{index + 1:02d}",
                phase="DAY_1_DISCUSSION_ROUND_1",
                request_json={"phase": "DAY_1_DISCUSSION_ROUND_1"},
                request_prompt_hash="publish-gate-prompt",
                raw_response="{}",
                parsed_response={},
                status="ok",
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.01,
                price_basis=PRICE_BASIS_FALLBACK_TABLE,
                price_table_version="publish-gate-table-v1",
                created_at=_NOW + timedelta(seconds=index),
            )
            session.add(call)
            await session.flush()
            llm_call_ids.append(call.id)

        event_log = EventLog()
        first_event = _append_event_row(
            session,
            game_id=game.id,
            event_log=event_log,
            event_type="GameCreated",
            visibility="SYSTEM",
            payload={"ruleset_id": mini7_v1.RULESET_ID},
        )
        second_event = _append_event_row(
            session,
            game_id=game.id,
            event_log=event_log,
            event_type="PublicMessage",
            payload={"message": "ready"},
        )
        await session.flush()

        context = await rating_contexts.ensure_declared_context(
            session, ruleset_id=mini7_v1.RULESET_ID
        )
        assert context is not None
        for index, build_id in enumerate(build_ids):
            score = 35.0 - index
            session.add(
                Rating(
                    league_id=league_id,
                    ruleset_id=mini7_v1.RULESET_ID,
                    rating_context_id=context.id,
                    agent_build_id=build_id,
                    scope_type=SCOPE_GLOBAL,
                    scope_value=SCOPE_VALUE_GLOBAL,
                    mu=score,
                    sigma=2.0,
                    conservative_score=score - 6.0,
                    games=1,
                    updated_at=_NOW + timedelta(minutes=index),
                )
            )
        session.add(
            RatingEvent(
                league_id=league_id,
                game_id=game.id,
                ruleset_id=mini7_v1.RULESET_ID,
                rating_context_id=context.id,
                game_seed=game.game_seed,
                team_outcome=Faction.TOWN.value,
                agent_build_id=build_ids[0],
                public_player_id="P01",
                scope_type=SCOPE_GLOBAL,
                scope_value=SCOPE_VALUE_GLOBAL,
                before_mu=34.5,
                before_sigma=2.0,
                after_mu=35.0,
                after_sigma=2.0,
                created_at=_NOW + timedelta(minutes=20),
            )
        )
        return PublishGateWorld(
            campaign_id=campaign.id,
            league_id=league_id,
            build_ids=tuple(build_ids),
            game_id=game.id,
            event_ids=(first_event.id, second_event.id),
            llm_call_ids=tuple(llm_call_ids),
        )


def _messages(result: object) -> list[str]:
    return [blocker.message for blocker in result.blockers]  # type: ignore[attr-defined]


async def test_publish_gate_ready_when_all_checks_pass(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _seed_ready_world(session_factory)

    async with session_factory() as session:
        result = await evaluate_publish_gate(session, world.campaign_id)

    assert result.ready_to_publish is True
    assert result.blockers == ()
    assert result.documented_holes == ()


async def test_publish_gate_ready_with_mixed_billed_and_unbilled_llm_calls(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _seed_ready_world(session_factory)

    async with session_factory() as session, session.begin():
        call = await session.get(LlmCall, world.llm_call_ids[0])
        assert call is not None
        call.status = "exhausted"
        call.raw_response = ""
        call.parsed_response = None
        call.input_tokens = None
        call.output_tokens = None
        call.cost_usd = None
        call.price_basis = None
        call.price_table_version = None

    async with session_factory() as session:
        result = await evaluate_publish_gate(session, world.campaign_id)

    assert result.ready_to_publish is True
    assert result.blockers == ()


async def test_publish_gate_still_blocks_incomplete_llm_cost_stamps(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _seed_ready_world(session_factory)

    async with session_factory() as session, session.begin():
        billed_without_basis = await session.get(LlmCall, world.llm_call_ids[0])
        assert billed_without_basis is not None
        billed_without_basis.cost_usd = 0.01
        billed_without_basis.price_basis = None
        billed_without_basis.price_table_version = None

        basis_without_cost = await session.get(LlmCall, world.llm_call_ids[1])
        assert basis_without_cost is not None
        basis_without_cost.cost_usd = None
        basis_without_cost.price_basis = PRICE_BASIS_FALLBACK_TABLE
        basis_without_cost.price_table_version = "publish-gate-table-v1"

    async with session_factory() as session:
        result = await evaluate_publish_gate(session, world.campaign_id)

    assert result.ready_to_publish is False
    messages = _messages(result)
    assert any("missing price_basis" in message for message in messages)
    assert any("missing cost_usd" in message for message in messages)
    assert all(blocker.code == "cost_stamp_missing" for blocker in result.blockers)


async def test_publish_gate_reports_specific_blockers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _seed_ready_world(session_factory)

    async with session_factory() as session, session.begin():
        campaign = await session.get(Campaign, world.campaign_id)
        assert campaign is not None
        campaign.per_model_game_target = 2
        event = await session.get(GameEvent, world.event_ids[1])
        assert event is not None
        event.payload = {"message": "leak", "model_name": "atlas"}
        call = await session.get(LlmCall, world.llm_call_ids[0])
        assert call is not None
        call.price_table_version = None
        rating = (
            await session.execute(select(Rating).where(Rating.agent_build_id == world.build_ids[0]))
        ).scalar_one()
        rating.sigma = 3.1
        rating.conservative_score = rating.mu - 9.3
        rating_event = (
            await session.execute(
                select(RatingEvent).where(RatingEvent.agent_build_id == world.build_ids[0])
            )
        ).scalar_one()
        rating_event.before_mu = 10.0
        rating_event.before_sigma = 2.0

    async with session_factory() as session:
        result = await evaluate_publish_gate(session, world.campaign_id)

    assert result.ready_to_publish is False
    messages = _messages(result)
    assert any("atlas under-sampled: 1 < 2" in message for message in messages)
    assert any("atlas sigma 3.100 > target 2.500" in message for message in messages)
    assert any("atlas rank moved" in message for message in messages)
    assert any("event 1 leaks forbidden key 'model_name'" in message for message in messages)
    assert any("missing fallback price_table_version" in message for message in messages)
    assert any("hash verification failed" in message for message in messages)


async def test_publish_gate_documents_dead_letters_and_excludes_failed_games(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _seed_ready_world(session_factory)

    async with session_factory() as session, session.begin():
        campaign = await session.get(Campaign, world.campaign_id)
        assert campaign is not None
        first_build = await agent_builds.get(session, world.build_ids[0])
        assert first_build is not None
        failed_gauntlet = await gauntlets.create(
            session,
            league_id=world.league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=first_build.prompt_version_id,
            clone_count=1,
            gauntlet_seed=f"publish-gate-failed-{uuid.uuid4().hex}",
            ranked=True,
            status="FAILED",
            campaign_id=world.campaign_id,
        )
        session.add(
            CampaignPairing(
                campaign_id=world.campaign_id,
                cell_index=1,
                roster_json=[str(build_id) for build_id in world.build_ids],
                status="DEAD_LETTER",
                attempt_count=2,
                last_error="provider_transient: retries exhausted",
                gauntlet_id=failed_gauntlet.id,
            )
        )
        failed_game = await games.create(
            session,
            gauntlet_id=failed_gauntlet.id,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=f"publish-gate-failed-game-{uuid.uuid4().hex}",
            status="FAILED",
        )
        failed_game.completed_at = _NOW + timedelta(minutes=5)
        for index, build_id in enumerate(world.build_ids):
            session.add(
                GameSeat(
                    game_id=failed_game.id,
                    public_player_id=f"F{index + 1:02d}",
                    seat_index=index,
                    agent_build_id=build_id,
                    role=Role.VILLAGER.value,
                    faction=Faction.TOWN.value,
                    alive=True,
                )
            )

    async with session_factory() as session:
        result = await evaluate_publish_gate(session, world.campaign_id)

    assert result.ready_to_publish is True
    assert result.blockers == ()
    assert len(result.documented_holes) == 1
    assert result.documented_holes[0].cell_index == 1
    assert result.documented_holes[0].last_error == "retries exhausted"


async def test_publish_gate_ignores_placement_results_for_canonical_publishability(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _seed_ready_world(session_factory)

    async with session_factory() as session, session.begin():
        canonical_ratings = (
            await session.execute(select(Rating).where(Rating.league_id == world.league_id))
        ).scalars()
        for row in list(canonical_ratings):
            await session.delete(row)
        placement_context = RatingContext(
            kind=RatingContextKind.PLACEMENT.value,
            ruleset_id=mini7_v1.RULESET_ID,
            is_canonical=False,
            display_label="Placement test",
        )
        session.add(placement_context)
        await session.flush()
        for build_id in world.build_ids:
            session.add(
                PlacementRating(
                    rating_context_id=placement_context.id,
                    agent_build_id=build_id,
                    scope_type=SCOPE_GLOBAL,
                    scope_value=SCOPE_VALUE_GLOBAL,
                    mu=40.0,
                    sigma=1.0,
                    conservative_score=37.0,
                    games=99,
                    updated_at=_NOW,
                )
            )

    async with session_factory() as session:
        result = await evaluate_publish_gate(session, world.campaign_id)

    assert result.ready_to_publish is False
    assert any("missing canonical rating" in message for message in _messages(result))


async def test_publish_gate_reports_not_ready_without_blocking_finalization(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _seed_ready_world(session_factory, campaign_status="COMPLETED")

    async with session_factory() as session, session.begin():
        campaign = await session.get(Campaign, world.campaign_id)
        assert campaign is not None
        campaign.per_model_game_target = 3

    async with session_factory() as session:
        result = await evaluate_publish_gate(session, world.campaign_id)
        campaign = await session.get(Campaign, world.campaign_id)

    assert campaign is not None
    assert campaign.status == "COMPLETED"
    assert campaign.completed_at is not None
    completed_at = (
        campaign.completed_at
        if campaign.completed_at.tzinfo is not None
        else campaign.completed_at.replace(tzinfo=UTC)
    )
    assert completed_at == _NOW
    assert result.ready_to_publish is False
    assert any("under-sampled" in message for message in _messages(result))
