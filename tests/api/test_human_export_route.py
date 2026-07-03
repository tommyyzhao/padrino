"""US-306: self-serve GDPR export route for human principals."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE, generate_session_token
from padrino.db.models import (
    Game,
    GameSeat,
    HumanChatMessage,
    HumanConsent,
    HumanRating,
    HumanRatingEvent,
)
from padrino.db.repositories import human_principals as principals_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.ratings.openskill_service import SCOPE_GLOBAL, SCOPE_VALUE_GLOBAL

_NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
        yield ac


async def _account_session(
    session: AsyncSession,
    *,
    display_name: str,
    ranked_handle: str,
) -> tuple[str, uuid.UUID]:
    raw_token = generate_session_token()
    issued_at = datetime.now(UTC)
    principal = await principals_repo.create_principal(
        session,
        kind=principals_repo.PRINCIPAL_KIND_ACCOUNT,
        display_name=display_name,
    )
    await principals_repo.claim_ranked_handle(
        session,
        principal.id,
        candidate=ranked_handle,
        blocklist=(),
        reserved=(),
        now=_NOW,
    )
    await principals_repo.create_session(
        session,
        principal_id=principal.id,
        raw_token=raw_token,
        kind=principals_repo.SESSION_KIND_ACCOUNT,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(hours=1),
    )
    return raw_token, principal.id


async def _seed_export_rows(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    other_principal_id: uuid.UUID,
) -> None:
    game = Game(ruleset_id="mini7_v1", game_seed="gdpr-export-chat", status="COMPLETED")
    other_game = Game(ruleset_id="mini7_v1", game_seed="gdpr-export-other", status="COMPLETED")
    session.add_all([game, other_game])
    await session.flush()

    session.add_all(
        [
            GameSeat(
                game_id=game.id,
                public_player_id="P03",
                seat_index=3,
                agent_build_id=None,
                seat_kind="HUMAN",
                occupant_principal_id=principal_id,
                role="DETECTIVE",
                faction="TOWN",
                alive=True,
            ),
            GameSeat(
                game_id=other_game.id,
                public_player_id="P04",
                seat_index=4,
                agent_build_id=None,
                seat_kind="HUMAN",
                occupant_principal_id=other_principal_id,
                role="DOCTOR",
                faction="TOWN",
                alive=True,
            ),
            HumanChatMessage(
                game_id=game.id,
                sequence=10,
                public_player_id="P03",
                raw_text="caller raw chat",
                cleaned_text="caller clean chat",
                redacted=False,
                created_at=_NOW,
            ),
            HumanChatMessage(
                game_id=game.id,
                sequence=11,
                public_player_id="P03",
                raw_text="redacted raw must not leak",
                cleaned_text="redacted clean must not leak",
                redacted=True,
                created_at=_NOW + timedelta(minutes=1),
            ),
            HumanChatMessage(
                game_id=other_game.id,
                sequence=12,
                public_player_id="P04",
                raw_text="other principal raw chat",
                cleaned_text="other principal clean chat",
                redacted=False,
                created_at=_NOW,
            ),
        ]
    )

    session.add_all(
        [
            HumanConsent(
                subject_principal_id=principal_id,
                document_kind="TOS",
                document_version="tos-v1",
                content_hash="caller-tos-hash",
                accepted_at=_NOW - timedelta(days=2),
                source_ip_hash="caller-ip-hash-must-not-export",
            ),
            HumanConsent(
                subject_principal_id=principal_id,
                document_kind="RANKED_WITHDRAWN",
                document_version="ranked-v1",
                content_hash="ranked-hash",
                accepted_at=_NOW - timedelta(days=1),
                source_ip_hash="withdrawal-ip-hash-must-not-export",
            ),
            HumanConsent(
                subject_principal_id=other_principal_id,
                document_kind="PRIVACY",
                document_version="privacy-v1",
                content_hash="other-privacy-hash",
                accepted_at=_NOW,
                source_ip_hash="other-ip-hash",
            ),
        ]
    )

    league = await leagues_repo.get_or_create_humans_included(
        session,
        ruleset_id="mini7_v1",
        ranked=True,
    )
    rating = HumanRating(
        league_id=league.id,
        human_player_id=str(principal_id),
        scope_type=SCOPE_GLOBAL,
        scope_value=SCOPE_VALUE_GLOBAL,
        mu=28.0,
        sigma=7.5,
        conservative_score=5.5,
        games=2,
        last_game_at=_NOW,
        updated_at=_NOW,
    )
    other_rating = HumanRating(
        league_id=league.id,
        human_player_id=str(other_principal_id),
        scope_type=SCOPE_GLOBAL,
        scope_value=SCOPE_VALUE_GLOBAL,
        mu=40.0,
        sigma=3.0,
        conservative_score=31.0,
        games=10,
        last_game_at=_NOW,
        updated_at=_NOW,
    )
    session.add_all([rating, other_rating])
    await session.flush()

    session.add_all(
        [
            HumanRatingEvent(
                league_id=rating.league_id,
                game_id=game.id,
                human_player_id=str(principal_id),
                public_player_id="P03",
                scope_type=SCOPE_GLOBAL,
                scope_value=SCOPE_VALUE_GLOBAL,
                before_mu=25.0,
                before_sigma=8.333333333333334,
                after_mu=28.0,
                after_sigma=7.5,
                created_at=_NOW,
            ),
            HumanRatingEvent(
                league_id=other_rating.league_id,
                game_id=other_game.id,
                human_player_id=str(other_principal_id),
                public_player_id="P04",
                scope_type=SCOPE_GLOBAL,
                scope_value=SCOPE_VALUE_GLOBAL,
                before_mu=25.0,
                before_sigma=8.333333333333334,
                after_mu=40.0,
                after_sigma=3.0,
                created_at=_NOW,
            ),
        ]
    )


@pytest.mark.asyncio
async def test_human_export_returns_only_callers_full_bundle(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        caller_token, caller_id = await _account_session(
            session,
            display_name="Caller",
            ranked_handle="CallerRanked",
        )
        _other_token, other_id = await _account_session(
            session,
            display_name="Other",
            ranked_handle="OtherRanked",
        )
        await _seed_export_rows(
            session,
            principal_id=caller_id,
            other_principal_id=other_id,
        )

    response = await client.get(
        "/human/me/export",
        cookies={HUMAN_SESSION_COOKIE: caller_token},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == "padrino.gdpr_human_export.v1"
    assert body["principal"]["id"] == str(caller_id)
    assert body["principal"]["display_name"] == "Caller"
    assert body["principal"]["ranked_handle"] == "CallerRanked"
    assert [row["document_kind"] for row in body["consents"]] == [
        "TOS",
        "RANKED_WITHDRAWN",
    ]
    assert [row["raw_text"] for row in body["chat_messages"]] == [
        "caller raw chat",
        None,
    ]
    assert body["chat_messages"][1]["redacted"] is True
    assert body["human_ratings"][0]["human_player_id"] == str(caller_id)
    assert body["human_rating_events"][0]["human_player_id"] == str(caller_id)
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["principal_id"] == str(caller_id)

    rendered = json.dumps(body, sort_keys=True)
    assert str(other_id) not in rendered
    assert "Other" not in rendered
    assert "OtherRanked" not in rendered
    assert "other principal raw chat" not in rendered
    assert "other-privacy-hash" not in rendered
    assert "session_hash" not in rendered
    assert "source_ip_hash" not in rendered
    assert "caller-ip-hash-must-not-export" not in rendered
    assert "redacted raw must not leak" not in rendered


@pytest.mark.asyncio
async def test_human_export_requires_human_session(client: AsyncClient) -> None:
    response = await client.get("/human/me/export")

    assert response.status_code == 401
    assert response.json()["detail"] == "human_authentication_required"
