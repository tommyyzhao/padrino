"""US-294: ranked human-rating write provenance is audited."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction
from padrino.core.ranked_eligibility import RankedEligibilityReason
from padrino.core.rulesets import mini7_v1
from padrino.db.models import GameSeat, HumanRating, HumanRatingEvent, RankedWriteAudit
from padrino.db.repositories import games, human_principals, human_ratings, leagues
from padrino.ratings.human_openskill_service import (
    HumanGameResult,
    update_human_ratings_for_game,
)

_GAME_SEED = "seed-ranked-write-audit-001"


async def _seed_ranked_human_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = next(seat.public_player_id for seat in seats if seat.faction is Faction.MAFIA)
    town = next(seat.public_player_id for seat in seats if seat.faction is Faction.TOWN)
    human_seats = {mafia, town}

    async with session_factory() as session, session.begin():
        league = await leagues.get_or_create_humans_included(
            session, ruleset_id=mini7_v1.RULESET_ID, ranked=True
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


async def test_applied_human_rating_events_have_matching_ranked_write_audit_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, _principals = await _seed_ranked_human_game(session_factory)
    now = datetime(2026, 6, 26, tzinfo=UTC)

    async with session_factory() as session, session.begin():
        events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=now,
        )

    assert len(events) == 2
    async with session_factory() as session:
        persisted_events = (await session.execute(select(HumanRatingEvent))).scalars().all()
        audit_rows = (await session.execute(select(RankedWriteAudit))).scalars().all()

    assert len(audit_rows) == len(persisted_events)
    assert {
        (row.league_id, row.game_id, row.human_player_id)
        for row in audit_rows
        if row.decision == "APPLIED"
    } == {(event.league_id, event.game_id, event.human_player_id) for event in persisted_events}
    assert {row.decision for row in audit_rows} == {"APPLIED"}
    assert {row.reason for row in audit_rows} == {RankedEligibilityReason.ELIGIBLE.value}
    assert {row.reason for row in audit_rows} <= {
        reason.value for reason in RankedEligibilityReason
    }
    assert {row.flag_enabled for row in audit_rows} == {True}
    assert all(row.created_at is not None for row in audit_rows)


async def test_flag_off_ranked_league_writes_refused_audit_without_rating_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, _principals = await _seed_ranked_human_game(session_factory)

    async with session_factory() as session, session.begin():
        events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=False,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
        )

    assert events == []
    async with session_factory() as session:
        audit_rows = (await session.execute(select(RankedWriteAudit))).scalars().all()
        rating_count = (
            await session.execute(select(func.count()).select_from(HumanRating))
        ).scalar_one()
        event_count = (
            await session.execute(select(func.count()).select_from(HumanRatingEvent))
        ).scalar_one()

    assert rating_count == 0
    assert event_count == 0
    assert len(audit_rows) == 1
    audit = audit_rows[0]
    assert audit.league_id == league_id
    assert audit.game_id == game_id
    assert audit.human_player_id is None
    assert audit.decision == "REFUSED"
    assert audit.reason == RankedEligibilityReason.RANKED_DISABLED.value
    assert audit.flag_enabled is False


async def test_ranked_write_audit_is_idempotent_for_duplicate_applied_rating_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, _principals = await _seed_ranked_human_game(session_factory)

    async with session_factory() as session, session.begin():
        first_events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=datetime(2026, 6, 26, tzinfo=UTC),
        )
    async with session_factory() as session, session.begin():
        duplicate_events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=datetime(2026, 6, 27, tzinfo=UTC),
        )

    assert len(first_events) == 2
    assert duplicate_events == []
    async with session_factory() as session:
        event_count = (
            await session.execute(select(func.count()).select_from(HumanRatingEvent))
        ).scalar_one()
        audit_rows = (await session.execute(select(RankedWriteAudit))).scalars().all()

    assert event_count == 2
    assert len(audit_rows) == 2
    assert {row.decision for row in audit_rows} == {"APPLIED"}
    assert len({(row.game_id, row.human_player_id, row.decision) for row in audit_rows}) == 2


async def test_ranked_write_audit_rolls_back_with_rating_write(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    league_id, game_id, _principals = await _seed_ranked_human_game(session_factory)

    async def fail_update_human_rating(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("forced rating update failure")

    monkeypatch.setattr(human_ratings, "update_human_rating", fail_update_human_rating)

    with pytest.raises(RuntimeError, match="forced rating update failure"):
        async with session_factory() as session, session.begin():
            await update_human_ratings_for_game(
                session,
                league_id=league_id,
                ranked=True,
                rating_enabled=True,
                game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
                now=datetime(2026, 6, 26, tzinfo=UTC),
            )

    async with session_factory() as session:
        counts = (
            (await session.execute(select(func.count()).select_from(HumanRating))).scalar_one(),
            (
                await session.execute(select(func.count()).select_from(HumanRatingEvent))
            ).scalar_one(),
            (
                await session.execute(select(func.count()).select_from(RankedWriteAudit))
            ).scalar_one(),
        )

    assert counts == (0, 0, 0)
