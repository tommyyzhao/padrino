"""US-298: public human ladder uses ranked handles, never raw display names."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import HumanRating
from padrino.db.repositories import human_principals, leagues
from padrino.db.repositories.human_principals import (
    claim_ranked_handle,
    derive_default_ranked_handle,
)
from padrino.ratings.openskill_service import SCOPE_GLOBAL, SCOPE_VALUE_GLOBAL
from padrino.ratings.provisional_and_decay import DEFAULT_PROVISIONAL_GAMES
from padrino.ratings.public_leaderboard import (
    card_to_response,
    compute_public_leaderboard,
    reset_cache,
)

NOW = datetime(2026, 6, 27, tzinfo=UTC)


async def _seed_ranked_human_rating(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    display_name: str,
    ranked_handle: str | None,
    score: float,
    games: int = 12,
    last_game_at: datetime | None = NOW,
    updated_at: datetime = NOW,
) -> uuid.UUID:
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
        if ranked_handle is not None:
            await claim_ranked_handle(
                session,
                principal.id,
                candidate=ranked_handle,
                blocklist=(),
                reserved=(),
                now=NOW,
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
                games=games,
                last_game_at=last_game_at,
                updated_at=updated_at,
            )
        )
        return principal.id


def _stable_human_entity_id(principal_id: uuid.UUID) -> str:
    return hashlib.sha256(f"human|{principal_id}".encode()).hexdigest()[:32]


async def _load_human_rating(
    session: AsyncSession,
    principal_id: uuid.UUID,
) -> HumanRating:
    rating = (
        await session.execute(
            select(HumanRating).where(HumanRating.human_player_id == str(principal_id))
        )
    ).scalar_one()
    return rating


async def test_provisional_human_cards_do_not_consume_published_rank_slots(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reset_cache()
    await _seed_ranked_human_rating(
        session_factory,
        display_name="Graduated Leader",
        ranked_handle="GraduatedLeader",
        score=40.0,
        games=DEFAULT_PROVISIONAL_GAMES,
    )
    await _seed_ranked_human_rating(
        session_factory,
        display_name="Graduated Chaser",
        ranked_handle="GraduatedChaser",
        score=25.0,
        games=DEFAULT_PROVISIONAL_GAMES + 4,
    )
    await _seed_ranked_human_rating(
        session_factory,
        display_name="Provisional Rocket",
        ranked_handle="ProvisionalRocket",
        score=80.0,
        games=DEFAULT_PROVISIONAL_GAMES - 1,
    )
    await _seed_ranked_human_rating(
        session_factory,
        display_name="Provisional Seed",
        ranked_handle="ProvisionalSeed",
        score=20.0,
        games=1,
    )

    async with session_factory() as session:
        leaderboard = await compute_public_leaderboard(
            session,
            ruleset_id="mini7_v1",
            now=NOW,
        )

    assert [card.entity.display_name for card in leaderboard.human_cards] == [
        "GraduatedLeader",
        "GraduatedChaser",
        "ProvisionalRocket",
        "ProvisionalSeed",
    ]
    by_name = {card.entity.display_name: card for card in leaderboard.human_cards}

    assert by_name["GraduatedLeader"].games == DEFAULT_PROVISIONAL_GAMES
    assert by_name["GraduatedLeader"].sample_count == DEFAULT_PROVISIONAL_GAMES
    assert by_name["GraduatedLeader"].provisional is False
    assert by_name["GraduatedLeader"].provisional_reason is None
    assert by_name["GraduatedLeader"].rank == 1
    assert by_name["GraduatedChaser"].rank == 2

    rocket = by_name["ProvisionalRocket"]
    assert rocket.score > by_name["GraduatedLeader"].score
    assert rocket.games == DEFAULT_PROVISIONAL_GAMES - 1
    assert rocket.sample_count == DEFAULT_PROVISIONAL_GAMES - 1
    assert rocket.provisional is True
    assert rocket.rank is None
    assert rocket.provisional_reason is not None
    assert f"{DEFAULT_PROVISIONAL_GAMES} games" in rocket.provisional_reason

    seed = by_name["ProvisionalSeed"]
    assert seed.provisional is True
    assert seed.rank is None
    assert [card.rank for card in leaderboard.human_cards if not card.provisional] == [1, 2]


async def test_provisional_idle_human_card_keeps_label_while_decay_projects_sigma(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reset_cache()
    idle_id = await _seed_ranked_human_rating(
        session_factory,
        display_name="Idle Placement",
        ranked_handle="IdlePlacement",
        score=40.0,
        games=DEFAULT_PROVISIONAL_GAMES - 1,
        last_game_at=NOW - timedelta(days=26),
    )

    async with session_factory() as session:
        persisted = await _load_human_rating(session, idle_id)
        leaderboard = await compute_public_leaderboard(
            session,
            ruleset_id="mini7_v1",
            now=NOW,
        )

    card = leaderboard.human_cards[0]
    assert card.entity.display_name == "IdlePlacement"
    assert card.provisional is True
    assert card.provisional_reason is not None
    assert card.rank is None
    assert card.games == DEFAULT_PROVISIONAL_GAMES - 1
    assert card.sample_count == DEFAULT_PROVISIONAL_GAMES - 1
    assert card.mu == pytest.approx(persisted.mu)
    assert card.sigma is not None
    assert card.conservative_score is not None
    assert card.sigma > persisted.sigma
    assert card.conservative_score < persisted.conservative_score


async def test_human_cards_project_idle_decay_without_mutating_persisted_rating(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reset_cache()
    idle_id = await _seed_ranked_human_rating(
        session_factory,
        display_name="Idle Realname",
        ranked_handle="IdleHandle",
        score=40.0,
        last_game_at=NOW - timedelta(days=26),
    )
    fresh_id = await _seed_ranked_human_rating(
        session_factory,
        display_name="Fresh Realname",
        ranked_handle="FreshHandle",
        score=30.0,
        last_game_at=NOW,
    )

    async with session_factory() as session:
        idle_before = await _load_human_rating(session, idle_id)
        before_values = (
            idle_before.mu,
            idle_before.sigma,
            idle_before.conservative_score,
            idle_before.games,
            idle_before.last_game_at,
            idle_before.updated_at,
        )
        leaderboard = await compute_public_leaderboard(
            session,
            ruleset_id="mini7_v1",
            now=NOW,
        )
        idle_after = await _load_human_rating(session, idle_id)
        after_values = (
            idle_after.mu,
            idle_after.sigma,
            idle_after.conservative_score,
            idle_after.games,
            idle_after.last_game_at,
            idle_after.updated_at,
        )

    assert after_values == before_values

    by_name = {card.entity.display_name: card for card in leaderboard.human_cards}
    idle = by_name["IdleHandle"]
    fresh = by_name["FreshHandle"]
    assert idle.mu == pytest.approx(idle_before.mu)
    assert idle.sigma is not None
    assert idle.conservative_score is not None
    assert idle.sigma > idle_before.sigma
    assert idle.conservative_score < idle_before.conservative_score
    assert idle.score == pytest.approx(idle.conservative_score)
    assert idle.games == idle_before.games
    assert idle.sample_count == idle_before.games

    async with session_factory() as session:
        fresh_row = await _load_human_rating(session, fresh_id)
    assert fresh.mu == pytest.approx(fresh_row.mu)
    assert fresh.sigma == pytest.approx(fresh_row.sigma)
    assert fresh.conservative_score == pytest.approx(fresh_row.conservative_score)
    assert fresh.score == pytest.approx(fresh_row.conservative_score)


async def test_human_cards_sort_and_rank_by_decayed_score(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reset_cache()
    await _seed_ranked_human_rating(
        session_factory,
        display_name="Idle High Raw",
        ranked_handle="IdleHighRaw",
        score=40.0,
        last_game_at=NOW - timedelta(days=26),
    )
    await _seed_ranked_human_rating(
        session_factory,
        display_name="Active Lower Raw",
        ranked_handle="ActiveLowerRaw",
        score=38.0,
        last_game_at=NOW,
    )

    async with session_factory() as session:
        leaderboard = await compute_public_leaderboard(
            session,
            ruleset_id="mini7_v1",
            now=NOW,
        )

    assert [card.entity.display_name for card in leaderboard.human_cards] == [
        "ActiveLowerRaw",
        "IdleHighRaw",
    ]
    by_name = {card.entity.display_name: card for card in leaderboard.human_cards}
    assert by_name["ActiveLowerRaw"].rank == 1
    assert by_name["IdleHighRaw"].rank == 2
    idle_score = by_name["IdleHighRaw"].conservative_score
    active_score = by_name["ActiveLowerRaw"].conservative_score
    assert idle_score is not None
    assert active_score is not None
    assert idle_score < active_score


async def test_human_cache_key_changes_on_decay_day_boundary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reset_cache()
    await _seed_ranked_human_rating(
        session_factory,
        display_name="Cache Idle",
        ranked_handle="CacheIdle",
        score=40.0,
        games=17,
        last_game_at=NOW - timedelta(days=26),
    )

    async with session_factory() as session:
        before = await compute_public_leaderboard(
            session,
            ruleset_id="mini7_v1",
            now=NOW.replace(hour=23, minute=59),
        )
        after = await compute_public_leaderboard(
            session,
            ruleset_id="mini7_v1",
            now=NOW + timedelta(days=1),
        )

    assert before.cache_tag != after.cache_tag
    assert before.human_cards[0].games == 17
    assert before.human_cards[0].sample_count == 17
    assert after.human_cards[0].games == 17
    assert after.human_cards[0].sample_count == 17


async def test_public_human_cards_use_ranked_handle_or_default_not_display_name(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reset_cache()
    handled_id = await _seed_ranked_human_rating(
        session_factory,
        display_name="Alice Realname",
        ranked_handle="RankedAlice",
        score=40.0,
    )
    defaulted_id = await _seed_ranked_human_rating(
        session_factory,
        display_name="Bob Realname",
        ranked_handle=None,
        score=30.0,
    )

    async with session_factory() as session:
        leaderboard = await compute_public_leaderboard(session, ruleset_id="mini7_v1")

    cards = [card_to_response(card) for card in leaderboard.human_cards]
    by_entity_id = {card["entity_id"]: card for card in cards}
    assert by_entity_id[_stable_human_entity_id(handled_id)]["display_name"] == "RankedAlice"
    assert by_entity_id[_stable_human_entity_id(defaulted_id)][
        "display_name"
    ] == derive_default_ranked_handle(defaulted_id)
    assert by_entity_id[_stable_human_entity_id(handled_id)][
        "entity_id"
    ] == _stable_human_entity_id(handled_id)

    serialized = json.dumps(
        {
            "cache_tag": leaderboard.cache_tag,
            "entries": [],
            "canonical_cards": [],
            "faction_cards": [],
            "experimental_cards": [],
            "human_cards": cards,
        },
        sort_keys=True,
    )
    assert "Alice Realname" not in serialized
    assert "Bob Realname" not in serialized
    assert str(handled_id) not in serialized
    assert str(defaulted_id) not in serialized
    assert handled_id.hex not in serialized
    assert defaulted_id.hex not in serialized


async def test_human_leaderboard_cache_changes_when_ranked_handle_changes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reset_cache()
    principal_id = await _seed_ranked_human_rating(
        session_factory,
        display_name="Cache Raw Name",
        ranked_handle="FirstHandle",
        score=40.0,
    )

    async with session_factory() as session:
        before = await compute_public_leaderboard(session, ruleset_id="mini7_v1")

    assert [card.entity.display_name for card in before.human_cards] == ["FirstHandle"]

    async with session_factory() as session, session.begin():
        updated = await claim_ranked_handle(
            session,
            principal_id,
            candidate="SecondHandle",
            blocklist=(),
            reserved=(),
            now=datetime(2026, 6, 28, tzinfo=UTC),
        )
        assert updated is not None

    async with session_factory() as session:
        after = await compute_public_leaderboard(session, ruleset_id="mini7_v1")

    assert [card.entity.display_name for card in after.human_cards] == ["SecondHandle"]


async def test_deleted_principal_is_excluded_from_human_leaderboard(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reset_cache()
    active_id = await _seed_ranked_human_rating(
        session_factory,
        display_name="Active Realname",
        ranked_handle="ActiveHandle",
        score=30.0,
    )
    deleted_id = await _seed_ranked_human_rating(
        session_factory,
        display_name="Deleted Realname",
        ranked_handle="DeletedHandle",
        score=50.0,
    )
    async with session_factory() as session, session.begin():
        principal = await human_principals.get_principal(session, deleted_id)
        assert principal is not None
        principal.deleted_at = NOW
        principal.updated_at = NOW

    async with session_factory() as session:
        leaderboard = await compute_public_leaderboard(session, ruleset_id="mini7_v1")

    cards = [card_to_response(card) for card in leaderboard.human_cards]
    assert [card["display_name"] for card in cards] == ["ActiveHandle"]
    serialized = json.dumps(cards, sort_keys=True)
    assert "DeletedHandle" not in serialized
    assert "Deleted Realname" not in serialized
    assert str(deleted_id) not in serialized
    assert str(active_id) not in serialized
