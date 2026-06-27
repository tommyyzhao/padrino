"""US-295: quarantine pre-gate human ratings and hide them from the ladder."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import HumanRating, HumanRatingEvent, RankedWriteAudit
from padrino.db.repositories import games, human_principals, leagues
from padrino.ratings.openskill_service import SCOPE_GLOBAL, SCOPE_VALUE_GLOBAL
from padrino.ratings.public_leaderboard import compute_public_leaderboard
from padrino.ratings.ranked_audit import audit_ranked_human_ratings


async def _seed_rating(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    display_name: str,
    with_provenance: bool,
    score: float,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as session, session.begin():
        league = await leagues.get_or_create_humans_included(
            session,
            ruleset_id="mini7_v1",
            ranked=True,
        )
        principal = await human_principals.create_principal(
            session,
            kind=human_principals.PRINCIPAL_KIND_ACCOUNT,
            display_name=display_name,
        )
        game = await games.create(
            session,
            ruleset_id="mini7_v1",
            game_seed=f"seed-{display_name}",
            status="COMPLETED",
        )
        rating = HumanRating(
            league_id=league.id,
            human_player_id=str(principal.id),
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            mu=score + 6.0,
            sigma=2.0,
            conservative_score=score,
            games=12,
            last_game_at=datetime(2026, 6, 25, tzinfo=UTC),
            updated_at=datetime(2026, 6, 25, tzinfo=UTC),
        )
        event = HumanRatingEvent(
            league_id=league.id,
            game_id=game.id,
            human_player_id=str(principal.id),
            public_player_id="P01",
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            before_mu=25.0,
            before_sigma=8.333333333333334,
            after_mu=rating.mu,
            after_sigma=rating.sigma,
            created_at=datetime(2026, 6, 25, tzinfo=UTC),
        )
        session.add_all([rating, event])
        await session.flush()
        if with_provenance:
            session.add(
                RankedWriteAudit(
                    league_id=league.id,
                    game_id=game.id,
                    human_player_id=str(principal.id),
                    decision="APPLIED",
                    reason="eligible",
                    flag_enabled=True,
                    created_at=datetime(2026, 6, 25, tzinfo=UTC),
                )
            )
    return rating.id, event.id


async def test_ranked_audit_quarantines_pre_gate_rows_without_mutating_rating_or_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pre_gate_rating_id, pre_gate_event_id = await _seed_rating(
        session_factory,
        display_name="Pre Gate",
        with_provenance=False,
        score=40.0,
    )
    provenanced_rating_id, _provenanced_event_id = await _seed_rating(
        session_factory,
        display_name="Provenanced",
        with_provenance=True,
        score=30.0,
    )
    quarantine_time = datetime(2026, 6, 27, tzinfo=UTC)

    async with session_factory() as session, session.begin():
        first = await audit_ranked_human_ratings(session, now=quarantine_time)
    async with session_factory() as session, session.begin():
        second = await audit_ranked_human_ratings(session, now=datetime(2026, 6, 28, tzinfo=UTC))

    assert first.scanned == 2
    assert first.newly_quarantined == 1
    assert first.missing_provenance == 1
    assert second.newly_quarantined == 0
    assert second.missing_provenance == 0

    async with session_factory() as session:
        pre_gate = await session.get(HumanRating, pre_gate_rating_id)
        provenanced = await session.get(HumanRating, provenanced_rating_id)
        event = await session.get(HumanRatingEvent, pre_gate_event_id)
        event_count = len((await session.execute(select(HumanRatingEvent))).scalars().all())

    assert pre_gate is not None
    assert pre_gate.quarantined_at is not None
    assert pre_gate.quarantined_at.replace(tzinfo=UTC) == quarantine_time
    assert pre_gate.mu == pytest.approx(46.0)
    assert pre_gate.sigma == pytest.approx(2.0)
    assert pre_gate.conservative_score == pytest.approx(40.0)
    assert pre_gate.games == 12
    assert event is not None
    assert event.game_id is not None
    assert event_count == 2
    assert provenanced is not None
    assert provenanced.quarantined_at is None

    async with session_factory() as session:
        leaderboard = await compute_public_leaderboard(session, ruleset_id="mini7_v1")

    assert [card.entity.display_name for card in leaderboard.human_cards] == ["Provenanced"]


async def test_quarantined_human_rating_is_excluded_and_cache_invalidates_on_flip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rating_id, _event_id = await _seed_rating(
        session_factory,
        display_name="Cache Subject",
        with_provenance=True,
        score=35.0,
    )

    async with session_factory() as session:
        before = await compute_public_leaderboard(session, ruleset_id="mini7_v1")

    assert [card.entity.display_name for card in before.human_cards] == ["Cache Subject"]

    async with session_factory() as session, session.begin():
        rating = await session.get(HumanRating, rating_id)
        assert rating is not None
        original_score = rating.conservative_score
        original_updated_at = rating.updated_at
        rating.quarantined_at = datetime(2026, 6, 27, tzinfo=UTC)

    async with session_factory() as session:
        after = await compute_public_leaderboard(session, ruleset_id="mini7_v1")
        reloaded = await session.get(HumanRating, rating_id)

    assert after.human_cards == ()
    assert reloaded is not None
    assert reloaded.conservative_score == original_score
    assert reloaded.updated_at == original_updated_at
