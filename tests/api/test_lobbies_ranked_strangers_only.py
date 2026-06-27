"""US-310: ranked launches require exactly one human seat."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE, generate_session_token
from padrino.api.human_consent import record_consent, record_ranked_consent
from padrino.core.enums import LobbySeatKind, LobbyStatus, SeatKind
from padrino.db.models import (
    AgentBuild,
    Game,
    GameSeat,
    Lobby,
    LobbyMember,
    LobbySeat,
    ModelConfig,
    ModelProvider,
    PromptVersion,
)
from padrino.db.repositories import human_principals, oauth_identities
from padrino.settings import Settings

_NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")


@pytest_asyncio.fixture
async def ranked_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=True)
    app.state.auth_settings = Settings(
        _env_file=None,
        padrino_ranked_human_rating_enabled=True,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
        yield ac


async def _account_with_session(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    subject: str,
) -> str:
    raw_token = generate_session_token()
    settings = Settings(_env_file=None, padrino_ranked_human_rating_enabled=True)
    async with session_factory() as session, session.begin():
        principal = await oauth_identities.find_or_create_account(
            session,
            provider="test-oauth",
            subject=subject,
            display_name="Ranked Player",
            email=f"{subject}@example.com",
            email_verified=True,
            now=_NOW,
        )
        await human_principals.create_session(
            session,
            principal_id=principal.id,
            raw_token=raw_token,
            kind=human_principals.SESSION_KIND_ACCOUNT,
            issued_at=_NOW,
            expires_at=_NOW + timedelta(days=365),
        )
        await record_consent(
            session,
            subject_principal_id=principal.id,
            settings=settings,
            accepted_at=_NOW,
        )
        await record_ranked_consent(
            session,
            subject_principal_id=principal.id,
            settings=settings,
            accepted_at=_NOW,
        )
    return raw_token


async def _guest_token(client: AsyncClient) -> str:
    response = await client.post("/human/guest")
    assert response.status_code == 201, response.text
    return response.cookies[HUMAN_SESSION_COOKIE]


async def _seed_curated_builds(session: AsyncSession, *, count: int) -> None:
    provider = ModelProvider(name="cerebras", base_url=None, auth_secret_ref="CEREBRAS_API_KEY")
    session.add(provider)
    await session.flush()
    model_config = ModelConfig(
        provider_id=provider.id,
        model_name="zai-glm-4.7",
        model_version=None,
        default_temperature=0.7,
        default_top_p=1.0,
        default_max_output_tokens=4096,
        supports_structured_outputs=True,
    )
    session.add(model_config)
    prompt = PromptVersion(
        ruleset_id="mini7_v1",
        version=f"v1-{uuid.uuid4()}",
        system_prompt="play",
        developer_prompt="json",
        response_schema={"type": "object"},
        prompt_hash=f"hash-us310-{uuid.uuid4()}",
    )
    session.add(prompt)
    await session.flush()
    for index in range(count):
        session.add(
            AgentBuild(
                display_name=f"cerebras/glm-4.7@us310-{index}",
                model_config_id=model_config.id,
                prompt_version_id=prompt.id,
                adapter_version="2026.05",
                inference_params={"temperature": 0.7},
                active=True,
            )
        )
    await session.commit()


async def _create_ranked_lobby(client: AsyncClient, token: str) -> uuid.UUID:
    response = await client.post(
        "/lobbies",
        json={
            "ruleset_id": "mini7_v1",
            "ranked": True,
            "integrity_acknowledged": True,
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _make_second_seat_human(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    lobby_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> None:
    async with session_factory() as session, session.begin():
        member = (
            await session.execute(
                select(LobbyMember).where(
                    LobbyMember.lobby_id == lobby_id,
                    LobbyMember.principal_id == principal_id,
                )
            )
        ).scalar_one()
        seat = (
            await session.execute(
                select(LobbySeat).where(
                    LobbySeat.lobby_id == lobby_id,
                    LobbySeat.seat_index == 1,
                )
            )
        ).scalar_one()
        seat.seat_kind = LobbySeatKind.HUMAN.value
        seat.member_id = member.id
        seat.agent_build_id = None


async def _principal_id_for_token(
    session_factory: async_sessionmaker[AsyncSession],
    token: str,
) -> uuid.UUID:
    from padrino.db.models import HumanSession
    from padrino.db.repositories.human_principals import hash_session_token

    async with session_factory() as session:
        row = (
            await session.execute(
                select(HumanSession.principal_id).where(
                    HumanSession.session_hash == hash_session_token(token)
                )
            )
        ).scalar_one()
    return row


async def _row_count(
    session_factory: async_sessionmaker[AsyncSession],
    model: type[Game] | type[GameSeat],
) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


@pytest.mark.asyncio
async def test_ranked_launch_with_multiple_human_seats_rejected_without_game_rows(
    ranked_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    host = await _account_with_session(session_factory, subject=f"host-{uuid.uuid4()}")
    friend = await _account_with_session(session_factory, subject=f"friend-{uuid.uuid4()}")
    friend_principal_id = await _principal_id_for_token(session_factory, friend)
    lobby_id = await _create_ranked_lobby(ranked_client, host)

    async with session_factory() as session:
        lobby = await session.get(Lobby, lobby_id)
        assert lobby is not None
        invite = lobby.invite_token

    joined = await ranked_client.post(
        f"/lobbies/join/{invite}",
        cookies={HUMAN_SESSION_COOKIE: friend},
    )
    assert joined.status_code == 200, joined.text
    await _make_second_seat_human(
        session_factory,
        lobby_id=lobby_id,
        principal_id=friend_principal_id,
    )

    locked = await ranked_client.post(
        f"/lobbies/{lobby_id}/lock",
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["status"] == LobbyStatus.LOCKED.value
    assert locked.json()["composition"] == {"human_count": 2, "ai_count": 5, "total": 7}

    before_games = await _row_count(session_factory, Game)
    before_seats = await _row_count(session_factory, GameSeat)

    launched = await ranked_client.post(
        f"/lobbies/{lobby_id}/launch",
        cookies={HUMAN_SESSION_COOKIE: host},
    )

    assert launched.status_code == 409, launched.text
    assert launched.json()["detail"] == "ranked_requires_single_human_seat"
    assert await _row_count(session_factory, Game) == before_games
    assert await _row_count(session_factory, GameSeat) == before_seats


@pytest.mark.asyncio
async def test_ranked_launch_with_one_host_human_still_succeeds(
    ranked_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
    host = await _account_with_session(session_factory, subject=f"host-{uuid.uuid4()}")
    lobby_id = await _create_ranked_lobby(ranked_client, host)
    locked = await ranked_client.post(
        f"/lobbies/{lobby_id}/lock",
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    assert locked.status_code == 200, locked.text

    launched = await ranked_client.post(
        f"/lobbies/{lobby_id}/launch",
        cookies={HUMAN_SESSION_COOKIE: host},
    )

    assert launched.status_code == 200, launched.text
    game_id = uuid.UUID(launched.json()["game_id"])
    async with session_factory() as session:
        seats = list(
            (
                await session.execute(
                    select(GameSeat)
                    .where(GameSeat.game_id == game_id)
                    .order_by(GameSeat.seat_index)
                )
            ).scalars()
        )
    assert len([seat for seat in seats if seat.seat_kind == SeatKind.HUMAN.value]) == 1
    assert len([seat for seat in seats if seat.seat_kind == SeatKind.AI.value]) == 6


@pytest.mark.asyncio
async def test_casual_launch_with_multiple_human_seats_is_unaffected(
    ranked_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _seed_curated_builds(session, count=5)
    host = await _guest_token(ranked_client)
    friend = await _guest_token(ranked_client)
    friend_principal_id = await _principal_id_for_token(session_factory, friend)
    created = await ranked_client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1"},
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    assert created.status_code == 201, created.text
    lobby_id = uuid.UUID(created.json()["id"])
    joined = await ranked_client.post(
        f"/lobbies/join/{created.json()['invite_token']}",
        cookies={HUMAN_SESSION_COOKIE: friend},
    )
    assert joined.status_code == 200, joined.text
    await _make_second_seat_human(
        session_factory,
        lobby_id=lobby_id,
        principal_id=friend_principal_id,
    )
    locked = await ranked_client.post(
        f"/lobbies/{lobby_id}/lock",
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    assert locked.status_code == 200, locked.text

    launched = await ranked_client.post(
        f"/lobbies/{lobby_id}/launch",
        cookies={HUMAN_SESSION_COOKIE: host},
    )

    assert launched.status_code == 200, launched.text
    game_id = uuid.UUID(launched.json()["game_id"])
    async with session_factory() as session:
        seats = list((await session.execute(select(GameSeat))).scalars())
    assert len([seat for seat in seats if seat.game_id == game_id]) == 7
    assert len([seat for seat in seats if seat.seat_kind == SeatKind.HUMAN.value]) == 2
