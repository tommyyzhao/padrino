"""US-307: self-serve GDPR delete anonymizes human data without touching replay."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE, generate_session_token
from padrino.core.engine.event_log import EventLog
from padrino.core.gdpr_delete import anonymized_principal_token
from padrino.db.models import (
    AgentBuild,
    Game,
    GameEvent,
    GameSeat,
    HumanChatMessage,
    HumanConsent,
    HumanRating,
    HumanRatingEvent,
    League,
    ModelConfig,
    ModelProvider,
    Principal,
    PromptVersion,
    Rating,
    RatingEvent,
)
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import human_principals as principals_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.export.bundle import EventEnvelope, verify_chain
from padrino.ratings.openskill_service import SCOPE_GLOBAL, SCOPE_VALUE_GLOBAL
from padrino.ratings.public_leaderboard import compute_public_leaderboard, reset_cache

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


def _envelopes(rows: list[GameEvent]) -> list[EventEnvelope]:
    return [
        EventEnvelope(
            sequence=row.sequence,
            event_type=row.event_type,
            phase=row.phase,
            visibility=row.visibility,
            actor_player_id=row.actor_player_id,
            payload=row.payload,
            prev_event_hash=row.prev_event_hash,
            event_hash=row.event_hash,
        )
        for row in rows
    ]


async def _seed_game_log(session: AsyncSession, *, principal_id: uuid.UUID) -> uuid.UUID:
    game = Game(
        ruleset_id="mini7_v1",
        game_seed="gdpr-delete-replay",
        status="COMPLETED",
    )
    session.add(game)
    await session.flush()
    game_id = game.id

    session.add(
        GameSeat(
            game_id=game_id,
            public_player_id="P03",
            seat_index=3,
            agent_build_id=None,
            seat_kind="HUMAN",
            occupant_principal_id=principal_id,
            role="DETECTIVE",
            faction="TOWN",
            alive=True,
        )
    )

    bodies: list[dict[str, Any]] = [
        {
            "event_type": "GameCreated",
            "sequence": 0,
            "phase": "SETUP",
            "visibility": "PUBLIC",
            "actor_player_id": None,
            "payload": {
                "ruleset_id": "mini7_v1",
                "game_id": str(game_id),
                "game_seed": "gdpr-delete-replay",
                "player_count": 7,
            },
        },
        {
            "event_type": "PublicMessageSubmitted",
            "sequence": 1,
            "phase": "DAY_1_DISCUSSION",
            "visibility": "PUBLIC",
            "actor_player_id": "P03",
            "payload": {"text": "", "content_ref": "sha256:caller-chat-ref"},
        },
    ]
    log = EventLog()
    for body in bodies:
        stored = log.append(body)
        await events_repo.append_event(
            session,
            game_id=game_id,
            sequence=stored.sequence,
            event_type=str(body["event_type"]),
            phase=str(body["phase"]),
            visibility=str(body["visibility"]),
            actor_player_id=body["actor_player_id"],
            payload=body["payload"],
            prev_event_hash=stored.prev_event_hash,
            event_hash=stored.event_hash,
        )
    game.event_hash_head = log.head_hash

    session.add(
        HumanChatMessage(
            game_id=game_id,
            sequence=1,
            public_player_id="P03",
            raw_text="caller raw chat must be redacted",
            cleaned_text="caller clean chat must be redacted",
            redacted=False,
            created_at=_NOW,
        )
    )
    return game_id


async def _seed_scientific_rating_rows(session: AsyncSession, *, game_id: uuid.UUID) -> None:
    provider = ModelProvider(name="scientific-provider", auth_secret_ref="env:SCI")
    session.add(provider)
    await session.flush()
    model = ModelConfig(
        provider_id=provider.id,
        model_name="scientific-model",
        default_temperature=0.0,
        default_top_p=1.0,
        default_max_output_tokens=512,
        supports_structured_outputs=True,
    )
    prompt = PromptVersion(
        ruleset_id="mini7_v1",
        version="scientific-v1",
        system_prompt="sys",
        developer_prompt="dev",
        response_schema={},
        prompt_hash="gdpr-delete-scientific-prompt",
    )
    session.add_all([model, prompt])
    await session.flush()
    build = AgentBuild(
        display_name="Scientific Build",
        model_config_id=model.id,
        prompt_version_id=prompt.id,
        adapter_version="test",
        inference_params={},
        active=True,
    )
    league = League(
        name="Scientific League",
        ruleset_id="mini7_v1",
        ranked=True,
        kind="SCIENTIFIC",
    )
    session.add_all([build, league])
    await session.flush()
    session.add_all(
        [
            Rating(
                league_id=league.id,
                agent_build_id=build.id,
                ruleset_id="mini7_v1",
                scope_type=SCOPE_GLOBAL,
                scope_value=SCOPE_VALUE_GLOBAL,
                mu=31.0,
                sigma=4.0,
                conservative_score=19.0,
                games=8,
                updated_at=_NOW,
            ),
            RatingEvent(
                league_id=league.id,
                game_id=game_id,
                agent_build_id=build.id,
                scope_type=SCOPE_GLOBAL,
                scope_value=SCOPE_VALUE_GLOBAL,
                before_mu=30.0,
                before_sigma=4.5,
                after_mu=31.0,
                after_sigma=4.0,
                created_at=_NOW,
            ),
        ]
    )


async def _seed_delete_rows(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> uuid.UUID:
    collision_id = uuid.UUID(anonymized_principal_token(principal_id, collision_index=0))
    session.add(Principal(id=collision_id, kind="account", display_name="Collision Account"))
    game_id = await _seed_game_log(session, principal_id=principal_id)
    session.add_all(
        [
            HumanConsent(
                subject_principal_id=principal_id,
                document_kind="TOS",
                document_version="tos-v1",
                content_hash="tos-hash",
                accepted_at=_NOW,
                source_ip_hash="source-ip-hash-must-null",
            ),
            HumanConsent(
                subject_principal_id=collision_id,
                document_kind="TOS",
                document_version="tos-v1",
                content_hash="tos-hash",
                accepted_at=_NOW,
                source_ip_hash="collision-ip-stays",
            ),
        ]
    )
    league = await leagues_repo.get_or_create_humans_included(
        session,
        ruleset_id="mini7_v1",
        ranked=True,
    )
    session.add(
        HumanRating(
            league_id=league.id,
            human_player_id=str(principal_id),
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            mu=34.0,
            sigma=2.0,
            conservative_score=28.0,
            games=12,
            last_game_at=_NOW,
            updated_at=_NOW,
        )
    )
    await session.flush()
    session.add(
        HumanRatingEvent(
            league_id=league.id,
            game_id=game_id,
            human_player_id=str(principal_id),
            public_player_id="P03",
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            before_mu=25.0,
            before_sigma=8.333333333333334,
            after_mu=34.0,
            after_sigma=2.0,
            created_at=_NOW,
        )
    )
    await _seed_scientific_rating_rows(session, game_id=game_id)
    return game_id


async def _scientific_snapshot(session: AsyncSession) -> tuple[list[tuple[Any, ...]], ...]:
    rating_rows = (
        await session.execute(
            select(
                Rating.id,
                Rating.league_id,
                Rating.agent_build_id,
                Rating.scope_type,
                Rating.scope_value,
                Rating.mu,
                Rating.sigma,
                Rating.conservative_score,
                Rating.games,
                Rating.updated_at,
            ).order_by(Rating.id)
        )
    ).all()
    event_rows = (
        await session.execute(
            select(
                RatingEvent.id,
                RatingEvent.league_id,
                RatingEvent.game_id,
                RatingEvent.agent_build_id,
                RatingEvent.scope_type,
                RatingEvent.scope_value,
                RatingEvent.before_mu,
                RatingEvent.before_sigma,
                RatingEvent.after_mu,
                RatingEvent.after_sigma,
                RatingEvent.created_at,
            ).order_by(RatingEvent.id)
        )
    ).all()
    return ([tuple(row) for row in rating_rows], [tuple(row) for row in event_rows])


@pytest.mark.asyncio
async def test_human_delete_anonymizes_pii_tombstones_ratings_and_preserves_replay(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reset_cache()
    async with session_factory() as session, session.begin():
        raw_token, principal_id = await _account_session(
            session,
            display_name="Alice Delete",
            ranked_handle="AliceRanked",
        )
        game_id = await _seed_delete_rows(session, principal_id=principal_id)

    async with session_factory() as session:
        game = await session.get(Game, game_id)
        assert game is not None
        event_rows_before = await events_repo.list_events(session, game_id)
        event_hashes_before = [(row.sequence, row.event_hash) for row in event_rows_before]
        head_before = game.event_hash_head
        assert verify_chain(_envelopes(event_rows_before)) == head_before
        scientific_before = await _scientific_snapshot(session)
        before_leaderboard = await compute_public_leaderboard(session, ruleset_id="mini7_v1")
        assert [card.entity.display_name for card in before_leaderboard.human_cards] == [
            "AliceRanked"
        ]

    response = await client.post(
        "/human/me/delete",
        cookies={HUMAN_SESSION_COOKIE: raw_token},
    )

    assert response.status_code == 204, response.text

    expected_token = anonymized_principal_token(principal_id, collision_index=1)
    uuid.UUID(expected_token)
    assert expected_token != str(principal_id)

    async with session_factory() as session:
        principal = await session.get(Principal, principal_id)
        assert principal is not None
        assert principal.deleted_at is not None
        assert principal.display_name is None
        assert principal.ranked_handle is None
        assert principal.ranked_handle_normalized is None

        consent = (
            await session.execute(
                select(HumanConsent).where(HumanConsent.subject_principal_id == principal_id)
            )
        ).scalar_one()
        assert consent.source_ip_hash is None

        chat = (
            await session.execute(
                select(HumanChatMessage).where(HumanChatMessage.game_id == game_id)
            )
        ).scalar_one()
        assert chat.raw_text is None
        assert chat.cleaned_text is None
        assert chat.redacted is True

        rating = (
            await session.execute(select(HumanRating).where(HumanRating.league_id.is_not(None)))
        ).scalar_one()
        assert rating.human_player_id == expected_token
        assert rating.quarantined_at is not None
        assert rating.mu == 34.0
        assert rating.sigma == 2.0
        assert rating.conservative_score == 28.0
        assert rating.games == 12

        rating_event = (await session.execute(select(HumanRatingEvent))).scalar_one()
        assert rating_event.human_player_id == expected_token
        assert rating_event.public_player_id == expected_token

        live_collision = await session.get(Principal, uuid.UUID(expected_token))
        assert live_collision is None

        game_after = await session.get(Game, game_id)
        assert game_after is not None
        event_rows_after = await events_repo.list_events(session, game_id)
        assert [(row.sequence, row.event_hash) for row in event_rows_after] == event_hashes_before
        assert game_after.event_hash_head == head_before
        assert verify_chain(_envelopes(event_rows_after)) == head_before
        assert await _scientific_snapshot(session) == scientific_before

        after_leaderboard = await compute_public_leaderboard(session, ruleset_id="mini7_v1")
        assert after_leaderboard.human_cards == ()


@pytest.mark.asyncio
async def test_deleted_principal_is_refused_match_and_ranked_lobby_actions(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        raw_token, principal_id = await _account_session(
            session,
            display_name="Deleted Player",
            ranked_handle="DeletedRanked",
        )

    delete_response = await client.post(
        "/human/me/delete",
        cookies={HUMAN_SESSION_COOKIE: raw_token},
    )
    assert delete_response.status_code == 204, delete_response.text

    match_response = await client.post(
        "/human/match",
        cookies={HUMAN_SESSION_COOKIE: raw_token},
    )
    assert match_response.status_code == 403
    assert match_response.json()["detail"] == "principal_deleted"

    ranked_response = await client.post(
        "/lobbies",
        json={
            "ruleset_id": "mini7_v1",
            "ranked": True,
            "integrity_acknowledged": True,
        },
        cookies={HUMAN_SESSION_COOKIE: raw_token},
    )
    assert ranked_response.status_code == 401

    async with session_factory() as session:
        principal_count = int(
            (await session.execute(select(func.count(Principal.id)))).scalar_one()
        )
        principal = await session.get(Principal, principal_id)
        assert principal is not None
        assert principal.deleted_at is not None
    assert principal_count == 1
