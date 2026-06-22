"""US-184: SOLO_RATE success-rate scoring context."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.enums import Faction, RatingContextKind
from padrino.db.models import (
    AgentBuild,
    Game,
    League,
    PlacementRating,
    PlacementRatingEvent,
    Rating,
    RatingContext,
    RatingEvent,
    SoloRateRating,
    SoloRateRatingEvent,
)
from padrino.db.repositories import (
    agent_builds,
    games,
    leagues,
    model_configs,
    prompt_versions,
    providers,
)
from padrino.ratings.openskill_service import (
    GameResult,
    PlacementGameResult,
    update_placement_ratings_for_game,
    update_ratings_for_game,
)
from padrino.ratings.solo_rate_service import (
    SCOPE_ROLE,
    SoloRateAttempt,
    SoloRateGameResult,
    list_solo_rate_scores,
    update_solo_rate_ratings_for_game,
)

_SOLO_RULESET = "experimental_solo_rate_v1"


async def _seed_solo_rate_context(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    hash_prefix: str,
) -> tuple[list[AgentBuild], list[Game], RatingContext, League]:
    async with session_factory() as session, session.begin():
        provider = await providers.create(
            session, name="cerebras", auth_secret_ref="CEREBRAS_API_KEY"
        )
        mc = await model_configs.create(
            session,
            provider_id=provider.id,
            model_name="glm-4.7",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        builds: list[AgentBuild] = []
        for i in range(2):
            pv = await prompt_versions.create(
                session,
                ruleset_id=_SOLO_RULESET,
                version=f"solo-{i + 1}",
                system_prompt="sys",
                developer_prompt="dev",
                response_schema={"type": "object"},
                prompt_hash=f"{hash_prefix}-{i}",
            )
            builds.append(
                await agent_builds.create(
                    session,
                    display_name=f"solo-build-{i}",
                    model_config_id=mc.id,
                    prompt_version_id=pv.id,
                    adapter_version="2026.05",
                    inference_params={"temperature": 0.7},
                    active=True,
                )
            )
        game_1 = await games.create(
            session,
            ruleset_id=_SOLO_RULESET,
            game_seed=f"{hash_prefix}-seed-1",
            status="COMPLETED",
        )
        game_2 = await games.create(
            session,
            ruleset_id=_SOLO_RULESET,
            game_seed=f"{hash_prefix}-seed-2",
            status="COMPLETED",
        )
        context = RatingContext(
            kind=RatingContextKind.SOLO_RATE.value,
            ruleset_id=_SOLO_RULESET,
            is_canonical=False,
            display_label="Experimental solo rate",
        )
        session.add(context)
        league = await leagues.create(
            session, name="solo-rate-scientific-guard", ruleset_id=_SOLO_RULESET, ranked=True
        )
    return builds, [game_1, game_2], context, league


async def test_solo_rate_context_records_counts_interval_and_min_sample_gate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    builds, game_rows, context, _league = await _seed_solo_rate_context(
        session_factory,
        hash_prefix="solo-rate",
    )
    builds_by_seat = {"P01": builds[0].id, "P02": builds[1].id}
    attempts = (
        SoloRateAttempt(public_player_id="P01", role="JESTER", succeeded=True),
        SoloRateAttempt(public_player_id="P02", role="JESTER", succeeded=False),
    )

    async with session_factory() as session, session.begin():
        events = []
        for game in game_rows:
            events.extend(
                await update_solo_rate_ratings_for_game(
                    session,
                    game_result=SoloRateGameResult(
                        game_id=game.id,
                        outcome_label="JESTER_LYNCH_BAIT",
                        attempts=attempts,
                    ),
                    agent_builds_by_seat=builds_by_seat,
                )
            )

    assert len(events) == 4
    async with session_factory() as session:
        solo_rows = (await session.execute(select(SoloRateRating))).scalars().all()
        solo_events = (await session.execute(select(SoloRateRatingEvent))).scalars().all()
        visible_scores = await list_solo_rate_scores(session, min_attempts=2)
        hidden_scores = await list_solo_rate_scores(session, min_attempts=3)
        canonical_rows = (await session.execute(select(Rating))).scalars().all()
        canonical_events = (await session.execute(select(RatingEvent))).scalars().all()
        placement_rows = (await session.execute(select(PlacementRating))).scalars().all()
        placement_events = (await session.execute(select(PlacementRatingEvent))).scalars().all()

    assert canonical_rows == []
    assert canonical_events == []
    assert placement_rows == []
    assert placement_events == []
    assert len(solo_rows) == 2
    assert len(solo_events) == 4
    assert {row.rating_context_id for row in solo_rows} == {context.id}
    assert {event.rating_context_id for event in solo_events} == {context.id}
    assert {event.game_seed for event in solo_events} == {game.game_seed for game in game_rows}
    assert {event.outcome_label for event in solo_events} == {"JESTER_LYNCH_BAIT"}
    assert {event.scope_type for event in solo_events} == {SCOPE_ROLE}
    assert {event.scope_value for event in solo_events} == {"JESTER"}

    by_build = {row.agent_build_id: row for row in solo_rows}
    winner_row = by_build[builds[0].id]
    loser_row = by_build[builds[1].id]
    assert (winner_row.successes, winner_row.attempts) == (2, 2)
    assert winner_row.posterior_alpha == pytest.approx(3.0)
    assert winner_row.posterior_beta == pytest.approx(1.0)
    assert winner_row.mean_success_rate == pytest.approx(0.75)
    assert (loser_row.successes, loser_row.attempts) == (0, 2)
    assert loser_row.mean_success_rate == pytest.approx(0.25)

    assert hidden_scores == []
    visible_by_build = {score.agent_build_id: score for score in visible_scores}
    assert set(visible_by_build) == {build.id for build in builds}
    winner_score = visible_by_build[builds[0].id]
    assert winner_score.scope_type == SCOPE_ROLE
    assert winner_score.scope_value == "JESTER"
    assert winner_score.successes == 2
    assert winner_score.attempts == 2
    assert winner_score.mean_success_rate == pytest.approx(0.75)
    assert winner_score.credible_interval_low == pytest.approx(0.3704, abs=0.0001)
    assert winner_score.credible_interval_high == pytest.approx(1.0)


async def test_solo_rate_ruleset_fails_closed_for_canonical_and_placement_paths(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    builds, game_rows, _context, league = await _seed_solo_rate_context(
        session_factory,
        hash_prefix="solo-rate-firewall",
    )
    game = game_rows[0]

    async with session_factory() as session, session.begin():
        canonical_events = await update_ratings_for_game(
            session,
            league_id=league.id,
            game_result=GameResult(
                game_id=game.id,
                winner="TOWN",
                seat_factions={"P01": Faction.TOWN, "P02": Faction.MAFIA},
            ),
            agent_builds_by_seat={"P01": builds[0].id, "P02": builds[1].id},
        )
        placement_events = await update_placement_ratings_for_game(
            session,
            game_result=PlacementGameResult(
                game_id=game.id,
                winner="JESTER",
                seat_groups={"P01": "JESTER", "P02": "TOWN"},
            ),
            agent_builds_by_seat={"P01": builds[0].id, "P02": builds[1].id},
        )
        solo_events = await update_solo_rate_ratings_for_game(
            session,
            game_result=SoloRateGameResult(
                game_id=game.id,
                outcome_label="JESTER_LYNCH_BAIT",
                attempts=(SoloRateAttempt(public_player_id="P01", role="JESTER", succeeded=True),),
            ),
            agent_builds_by_seat={"P01": builds[0].id, "P02": builds[1].id},
        )

    assert canonical_events == []
    assert placement_events == []
    assert len(solo_events) == 1
    async with session_factory() as session:
        canonical_rows = (await session.execute(select(Rating))).scalars().all()
        canonical_event_rows = (await session.execute(select(RatingEvent))).scalars().all()
        placement_rows = (await session.execute(select(PlacementRating))).scalars().all()
        placement_event_rows = (await session.execute(select(PlacementRatingEvent))).scalars().all()
        solo_rows = (await session.execute(select(SoloRateRating))).scalars().all()
        solo_event_rows = (await session.execute(select(SoloRateRatingEvent))).scalars().all()

    assert canonical_rows == []
    assert canonical_event_rows == []
    assert placement_rows == []
    assert placement_event_rows == []
    assert len(solo_rows) == 1
    assert len(solo_event_rows) == 1
