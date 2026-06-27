"""US-298: public human ladder uses ranked handles, never raw display names."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import HumanRating
from padrino.db.repositories import human_principals, leagues
from padrino.db.repositories.human_principals import (
    claim_ranked_handle,
    derive_default_ranked_handle,
)
from padrino.ratings.openskill_service import SCOPE_GLOBAL, SCOPE_VALUE_GLOBAL
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
                games=12,
                last_game_at=NOW,
                updated_at=NOW,
            )
        )
        return principal.id


def _stable_human_entity_id(principal_id: uuid.UUID) -> str:
    return hashlib.sha256(f"human|{principal_id}".encode()).hexdigest()[:32]


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
