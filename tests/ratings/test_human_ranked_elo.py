"""US-234b: ranked Humans-Included ELO writes only human-rating siblings."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, LeagueKind
from padrino.core.rulesets import mini7_v1
from padrino.db.models import GameSeat, HumanRating, HumanRatingEvent, Rating, RatingEvent
from padrino.db.repositories import games, human_principals, leagues
from padrino.ratings.human_openskill_service import (
    HumanGameResult,
    update_human_ratings_for_game,
)
from padrino.ratings.openskill_service import INITIAL_MU, INITIAL_SIGMA

_GAME_SEED = "seed-human-ranked-elo-001"


async def _seed_human_rating_game(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    league_kind: LeagueKind,
    league_ranked: bool,
    hash_prefix: str,
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = next(seat.public_player_id for seat in seats if seat.faction is Faction.MAFIA)
    town = next(seat.public_player_id for seat in seats if seat.faction is Faction.TOWN)
    human_seats = {mafia, town}

    async with session_factory() as session, session.begin():
        if league_kind is LeagueKind.HUMANS_INCLUDED:
            league = await leagues.get_or_create_humans_included(
                session, ruleset_id=mini7_v1.RULESET_ID, ranked=league_ranked
            )
        else:
            league = await leagues.create(
                session,
                name=f"scientific-{hash_prefix}",
                ruleset_id=mini7_v1.RULESET_ID,
                ranked=league_ranked,
            )
        game = await games.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_GAME_SEED,
            status="COMPLETED",
        )
        principals: dict[str, uuid.UUID] = {}
        for seat in seats:
            principal_id: uuid.UUID | None = None
            if seat.public_player_id in human_seats:
                principal = await human_principals.create_principal(
                    session,
                    kind=human_principals.PRINCIPAL_KIND_GUEST,
                    display_name=None,
                )
                principal_id = principal.id
                principals[seat.public_player_id] = principal.id
            session.add(
                GameSeat(
                    game_id=game.id,
                    public_player_id=seat.public_player_id,
                    seat_index=seat.seat_index,
                    agent_build_id=None,
                    seat_kind="HUMAN" if principal_id is not None else "AI",
                    occupant_principal_id=principal_id,
                    role=seat.role.value,
                    faction=seat.faction.value,
                    alive=True,
                )
            )
    return league.id, game.id, principals


async def test_ranked_humans_included_updates_only_human_global_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, principals = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="ranked-human",
    )

    async with session_factory() as session, session.begin():
        events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
        )

    assert len(events) == 2

    async with session_factory() as session:
        human_rows = (await session.execute(select(HumanRating))).scalars().all()
        human_events = (await session.execute(select(HumanRatingEvent))).scalars().all()
        scientific_rows = (await session.execute(select(Rating))).scalars().all()
        scientific_events = (await session.execute(select(RatingEvent))).scalars().all()

    assert scientific_rows == []
    assert scientific_events == []
    assert len(human_rows) == 2
    assert len(human_events) == 2
    assert {row.scope_type for row in human_rows} == {"GLOBAL"}
    assert {row.scope_value for row in human_rows} == {"global"}
    assert {event.scope_type for event in human_events} == {"GLOBAL"}
    assert {event.scope_value for event in human_events} == {"global"}
    assert {row.human_player_id for row in human_rows} == {
        str(principal_id) for principal_id in principals.values()
    }
    assert {event.public_player_id for event in human_events} == set(principals)

    seats = assign_roles(_GAME_SEED, mini7_v1)
    faction_by_principal = {
        str(principals[seat.public_player_id]): seat.faction
        for seat in seats
        if seat.public_player_id in principals
    }
    by_human = {row.human_player_id: row for row in human_rows}
    for human_id, faction in faction_by_principal.items():
        row = by_human[human_id]
        if faction is Faction.TOWN:
            assert row.mu > INITIAL_MU
        else:
            assert row.mu < INITIAL_MU
        assert row.sigma < INITIAL_SIGMA
        assert row.games == 1
        assert row.conservative_score == pytest.approx(row.mu - 3.0 * row.sigma)


async def test_human_writer_fails_closed_for_casual_scientific_and_missing_league(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    casual_league_id, casual_game_id, _ = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=False,
        hash_prefix="casual-human",
    )
    scientific_league_id, scientific_game_id, _ = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.SCIENTIFIC,
        league_ranked=True,
        hash_prefix="scientific",
    )

    async with session_factory() as session, session.begin():
        casual_events = await update_human_ratings_for_game(
            session,
            league_id=casual_league_id,
            ranked=True,
            game_result=HumanGameResult(game_id=casual_game_id, winner="TOWN"),
        )
        runtime_unranked_events = await update_human_ratings_for_game(
            session,
            league_id=casual_league_id,
            ranked=False,
            game_result=HumanGameResult(game_id=casual_game_id, winner="TOWN"),
        )
        scientific_events = await update_human_ratings_for_game(
            session,
            league_id=scientific_league_id,
            ranked=True,
            game_result=HumanGameResult(game_id=scientific_game_id, winner="TOWN"),
        )
        missing_league_events = await update_human_ratings_for_game(
            session,
            league_id=uuid.uuid4(),
            ranked=True,
            game_result=HumanGameResult(game_id=scientific_game_id, winner="TOWN"),
        )

    assert casual_events == []
    assert runtime_unranked_events == []
    assert scientific_events == []
    assert missing_league_events == []

    async with session_factory() as session:
        counts = (
            (await session.execute(select(func.count()).select_from(Rating))).scalar_one(),
            (await session.execute(select(func.count()).select_from(RatingEvent))).scalar_one(),
            (await session.execute(select(func.count()).select_from(HumanRating))).scalar_one(),
            (
                await session.execute(select(func.count()).select_from(HumanRatingEvent))
            ).scalar_one(),
        )

    assert counts == (0, 0, 0, 0)
