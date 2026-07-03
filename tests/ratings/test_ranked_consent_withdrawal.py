"""US-305: withdrawn ranked consent freezes ratings and hides ladder cards."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api import human_consent
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction
from padrino.core.rulesets import mini7_v1
from padrino.db.models import GameSeat, HumanRating, HumanRatingEvent
from padrino.db.repositories import games, human_principals, leagues
from padrino.db.repositories.human_principals import derive_default_ranked_handle
from padrino.ratings.human_openskill_service import HumanGameResult, update_human_ratings_for_game
from padrino.ratings.openskill_service import SCOPE_GLOBAL, SCOPE_VALUE_GLOBAL
from padrino.ratings.public_leaderboard import compute_public_leaderboard, reset_cache
from padrino.settings import Settings

NOW = datetime(2026, 6, 27, tzinfo=UTC)


async def _seed_principals_and_league(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_factory() as session, session.begin():
        league = await leagues.get_or_create_humans_included(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            ranked=True,
        )
        town = await human_principals.create_principal(
            session,
            kind=human_principals.PRINCIPAL_KIND_ACCOUNT,
            display_name="Town Human",
        )
        mafia = await human_principals.create_principal(
            session,
            kind=human_principals.PRINCIPAL_KIND_ACCOUNT,
            display_name="Mafia Human",
        )
        return league.id, town.id, mafia.id


async def _seed_ranked_game(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    seed_label: str,
    town_principal_id: uuid.UUID,
    mafia_principal_id: uuid.UUID,
) -> uuid.UUID:
    seats = assign_roles(f"seed-{seed_label}", mini7_v1)
    claimed_factions: set[Faction] = set()
    async with session_factory() as session, session.begin():
        game = await games.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=f"seed-{seed_label}",
            status="COMPLETED",
        )
        for seat in seats:
            principal_id: uuid.UUID | None = None
            if seat.faction is Faction.TOWN and Faction.TOWN not in claimed_factions:
                principal_id = town_principal_id
                claimed_factions.add(Faction.TOWN)
            elif seat.faction is Faction.MAFIA and Faction.MAFIA not in claimed_factions:
                principal_id = mafia_principal_id
                claimed_factions.add(Faction.MAFIA)
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
        return game.id


async def _event_count(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(HumanRatingEvent)
                .where(HumanRatingEvent.human_player_id == str(principal_id))
            )
        ).scalar_one()
    )


async def _rating_row(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> HumanRating:
    return (
        await session.execute(
            select(HumanRating).where(HumanRating.human_player_id == str(principal_id))
        )
    ).scalar_one()


async def _record_withdrawal(
    session_factory: async_sessionmaker[AsyncSession],
    principal_id: uuid.UUID,
    *,
    when: datetime,
) -> None:
    async with session_factory() as session, session.begin():
        await human_consent.record_ranked_withdrawal(
            session,
            subject_principal_id=principal_id,
            settings=Settings(_env_file=None),
            withdrawn_at=when,
        )


async def _record_ranked_reacceptance(
    session_factory: async_sessionmaker[AsyncSession],
    principal_id: uuid.UUID,
    *,
    when: datetime,
) -> None:
    async with session_factory() as session, session.begin():
        await human_consent.record_ranked_consent(
            session,
            subject_principal_id=principal_id,
            settings=Settings(_env_file=None),
            accepted_at=when,
        )


async def test_withdrawn_principal_is_frozen_until_ranked_reacceptance(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, town_principal_id, mafia_principal_id = await _seed_principals_and_league(
        session_factory
    )
    first_game_id = await _seed_ranked_game(
        session_factory,
        seed_label="before-withdrawal",
        town_principal_id=town_principal_id,
        mafia_principal_id=mafia_principal_id,
    )

    async with session_factory() as session, session.begin():
        first_events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=first_game_id, winner="TOWN"),
            now=datetime(2026, 6, 1, tzinfo=UTC),
        )
    assert len(first_events) == 2

    await _record_withdrawal(
        session_factory,
        town_principal_id,
        when=datetime(2026, 6, 2, tzinfo=UTC),
    )

    async with session_factory() as session:
        frozen_before = await _rating_row(session, principal_id=town_principal_id)
        frozen_snapshot = (
            frozen_before.mu,
            frozen_before.sigma,
            frozen_before.conservative_score,
            frozen_before.games,
            frozen_before.last_game_at,
        )

    second_game_id = await _seed_ranked_game(
        session_factory,
        seed_label="during-withdrawal",
        town_principal_id=town_principal_id,
        mafia_principal_id=mafia_principal_id,
    )
    async with session_factory() as session, session.begin():
        second_events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=second_game_id, winner="TOWN"),
            now=datetime(2026, 6, 3, tzinfo=UTC),
        )

    assert {event.human_player_id for event in second_events} == {str(mafia_principal_id)}
    async with session_factory() as session:
        frozen_after = await _rating_row(session, principal_id=town_principal_id)
        mafia_after = await _rating_row(session, principal_id=mafia_principal_id)
        frozen_event_count = await _event_count(session, principal_id=town_principal_id)

    assert (
        frozen_after.mu,
        frozen_after.sigma,
        frozen_after.conservative_score,
        frozen_after.games,
        frozen_after.last_game_at,
    ) == frozen_snapshot
    assert frozen_event_count == 1
    assert mafia_after.games == 2

    await _record_ranked_reacceptance(
        session_factory,
        town_principal_id,
        when=datetime(2026, 6, 4, tzinfo=UTC),
    )
    third_game_id = await _seed_ranked_game(
        session_factory,
        seed_label="after-reacceptance",
        town_principal_id=town_principal_id,
        mafia_principal_id=mafia_principal_id,
    )
    async with session_factory() as session, session.begin():
        third_events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=third_game_id, winner="TOWN"),
            now=datetime(2026, 6, 5, tzinfo=UTC),
        )

    assert str(town_principal_id) in {event.human_player_id for event in third_events}
    async with session_factory() as session:
        unfrozen = await _rating_row(session, principal_id=town_principal_id)
        unfrozen_event_count = await _event_count(session, principal_id=town_principal_id)

    assert unfrozen.games == 2
    assert unfrozen_event_count == 2
    assert unfrozen.last_game_at is not None
    assert unfrozen.last_game_at != frozen_snapshot[-1]


async def _seed_ladder_rating(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    display_name: str,
    score: float,
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        league = await leagues.get_or_create_humans_included(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            ranked=True,
        )
        principal = await human_principals.create_principal(
            session,
            kind=human_principals.PRINCIPAL_KIND_ACCOUNT,
            display_name=display_name,
        )
        session.add(
            HumanRating(
                league_id=league.id,
                human_player_id=str(principal.id),
                scope_type=SCOPE_GLOBAL,
                scope_value=SCOPE_VALUE_GLOBAL,
                mu=score + 6.0,
                sigma=2.0,
                conservative_score=score,
                games=12,
                last_game_at=NOW,
                updated_at=NOW,
            )
        )
        return principal.id


@pytest.mark.asyncio
async def test_withdrawn_principal_is_excluded_from_ladder_until_reacceptance(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reset_cache()
    withdrawn_id = await _seed_ladder_rating(
        session_factory,
        display_name="Withdrawn Human",
        score=40.0,
    )
    active_id = await _seed_ladder_rating(
        session_factory,
        display_name="Active Human",
        score=30.0,
    )

    async with session_factory() as session:
        before = await compute_public_leaderboard(session, ruleset_id=mini7_v1.RULESET_ID)

    assert [card.entity.display_name for card in before.human_cards] == [
        derive_default_ranked_handle(withdrawn_id),
        derive_default_ranked_handle(active_id),
    ]

    await _record_withdrawal(
        session_factory,
        withdrawn_id,
        when=datetime(2026, 6, 28, tzinfo=UTC),
    )

    async with session_factory() as session:
        after_withdrawal = await compute_public_leaderboard(session, ruleset_id=mini7_v1.RULESET_ID)

    assert [card.entity.display_name for card in after_withdrawal.human_cards] == [
        derive_default_ranked_handle(active_id)
    ]
    assert after_withdrawal.cache_tag != before.cache_tag

    await _record_ranked_reacceptance(
        session_factory,
        withdrawn_id,
        when=datetime(2026, 6, 29, tzinfo=UTC),
    )

    async with session_factory() as session:
        after_reacceptance = await compute_public_leaderboard(
            session, ruleset_id=mini7_v1.RULESET_ID
        )

    assert [card.entity.display_name for card in after_reacceptance.human_cards] == [
        derive_default_ranked_handle(withdrawn_id),
        derive_default_ranked_handle(active_id),
    ]
    assert after_reacceptance.cache_tag != after_withdrawal.cache_tag
