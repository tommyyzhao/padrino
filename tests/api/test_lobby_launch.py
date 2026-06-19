"""Tests for the lobby launch handoff (US-149).

A host launches a LOCKED lobby: empty AI seats are filled deterministically from
the curated pool, then a ``Game`` + ``GameSeat`` rows are materialized on the
human worker lane (humans -> ``occupant_principal_id`` / ``seat_kind=HUMAN``; AI
-> ``agent_build_id`` / ``seat_kind=AI``). Launch is single-fire / idempotent.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.core.enums import LobbyStatus, SeatKind
from padrino.db.models import (
    AgentBuild,
    Game,
    GameSeat,
    Lobby,
    ModelConfig,
    ModelProvider,
    PromptVersion,
    Rating,
    RatingEvent,
)
from padrino.runner.human_lane import list_human_lane_games


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _guest_token(client: AsyncClient) -> str:
    resp = await client.post("/human/guest")
    assert resp.status_code == 201
    return resp.cookies[HUMAN_SESSION_COOKIE]


async def _seed_curated_builds(session: AsyncSession, *, count: int) -> list[uuid.UUID]:
    """Seed ``count`` active agent builds targeting mini7_v1 (the curated pool)."""
    provider = ModelProvider(name="cerebras", base_url=None, auth_secret_ref="CEREBRAS_API_KEY")
    session.add(provider)
    await session.flush()
    mc = ModelConfig(
        provider_id=provider.id,
        model_name="zai-glm-4.7",
        model_version=None,
        default_temperature=0.7,
        default_top_p=1.0,
        default_max_output_tokens=4096,
        supports_structured_outputs=True,
    )
    session.add(mc)
    pv = PromptVersion(
        ruleset_id="mini7_v1",
        version="v1",
        system_prompt="play",
        developer_prompt="json",
        response_schema={"type": "object"},
        prompt_hash="hash-us149",
    )
    session.add(pv)
    await session.flush()
    ids: list[uuid.UUID] = []
    for i in range(count):
        ab = AgentBuild(
            display_name=f"cerebras/glm-4.7@v1-{i}",
            model_config_id=mc.id,
            prompt_version_id=pv.id,
            adapter_version="2026.05",
            inference_params={"temperature": 0.7},
            active=True,
        )
        session.add(ab)
        await session.flush()
        ids.append(ab.id)
    await session.commit()
    return ids


async def _create_and_lock(client: AsyncClient, token: str) -> uuid.UUID:
    created = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert created.status_code == 201, created.text
    lobby_id = uuid.UUID(created.json()["id"])
    locked = await client.post(f"/lobbies/{lobby_id}/lock", cookies={HUMAN_SESSION_COOKIE: token})
    assert locked.status_code == 200, locked.text
    return lobby_id


@pytest.mark.asyncio
async def test_launch_materializes_game_and_seats(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
    token = await _guest_token(client)
    lobby_id = await _create_and_lock(client, token)

    resp = await client.post(f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] is True
    assert body["status"] == LobbyStatus.LAUNCHED.value
    game_id = uuid.UUID(body["game_id"])

    async with session_factory() as session:
        lobby = await session.get(Lobby, lobby_id)
        assert lobby is not None
        assert lobby.status == LobbyStatus.LAUNCHED.value
        assert lobby.game_id == game_id

        game = await session.get(Game, game_id)
        assert game is not None
        assert game.gauntlet_id is None  # gauntlet-less => benchmark lane never claims it
        assert game.ruleset_id == "mini7_v1"
        assert game.identity_mode == "ANONYMOUS"

        seats = list(
            (
                await session.execute(
                    select(GameSeat)
                    .where(GameSeat.game_id == game_id)
                    .order_by(GameSeat.seat_index)
                )
            ).scalars()
        )
    assert len(seats) == 7
    human_seats = [s for s in seats if s.seat_kind == SeatKind.HUMAN.value]
    ai_seats = [s for s in seats if s.seat_kind == SeatKind.AI.value]
    assert len(human_seats) == 1
    assert len(ai_seats) == 6
    # The host HUMAN seat links a principal and has no agent build.
    assert human_seats[0].occupant_principal_id is not None
    assert human_seats[0].agent_build_id is None
    # Every AI seat is filled with a distinct curated build.
    ai_build_ids = [s.agent_build_id for s in ai_seats]
    assert all(b is not None for b in ai_build_ids)
    assert len(set(ai_build_ids)) == len(ai_build_ids)
    # Roles cover the full mini7 roster (deterministic from the game seed).
    assert {s.public_player_id for s in seats} == {f"P{i:02d}" for i in range(1, 8)}


@pytest.mark.asyncio
async def test_launched_game_is_on_human_worker_lane(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
    token = await _guest_token(client)
    lobby_id = await _create_and_lock(client, token)
    resp = await client.post(f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: token})
    game_id = uuid.UUID(resp.json()["game_id"])

    async with session_factory() as session:
        lane_games = await list_human_lane_games(session)
    assert game_id in lane_games


@pytest.mark.asyncio
async def test_launch_is_idempotent(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
    token = await _guest_token(client)
    lobby_id = await _create_and_lock(client, token)

    first = await client.post(f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: token})
    assert first.status_code == 200
    assert first.json()["created"] is True
    game_id = first.json()["game_id"]

    second = await client.post(f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: token})
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["game_id"] == game_id

    # Exactly one game materialized.
    async with session_factory() as session:
        game_count = (await session.execute(select(func.count()).select_from(Game))).scalar_one()
        seat_count = (
            await session.execute(select(func.count()).select_from(GameSeat))
        ).scalar_one()
    assert game_count == 1
    assert seat_count == 7


@pytest.mark.asyncio
async def test_launch_requires_lock(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
    token = await _guest_token(client)
    created = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    lobby_id = created.json()["id"]
    # Still OPEN, not LOCKED.
    resp = await client.post(f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: token})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "lobby_not_locked"


@pytest.mark.asyncio
async def test_launch_requires_host(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
    host_token = await _guest_token(client)
    lobby_id = await _create_and_lock(client, host_token)
    other_token = await _guest_token(client)
    resp = await client.post(
        f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: other_token}
    )
    # A non-member cannot even probe existence (404).
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_launch_requires_human_session(client: AsyncClient) -> None:
    resp = await client.post("/lobbies/00000000-0000-0000-0000-000000000000/launch")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_launch_fails_when_pool_too_small(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        await _seed_curated_builds(session, count=2)  # 6 empty AI seats, only 2 builds
    token = await _guest_token(client)
    lobby_id = await _create_and_lock(client, token)
    resp = await client.post(f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: token})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "autofill_pool_exhausted"


@pytest.mark.asyncio
async def test_launch_writes_zero_scientific_rating_rows(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
    token = await _guest_token(client)
    lobby_id = await _create_and_lock(client, token)
    resp = await client.post(f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: token})
    assert resp.status_code == 200
    async with session_factory() as session:
        ratings = (await session.execute(select(func.count()).select_from(Rating))).scalar_one()
        events = (await session.execute(select(func.count()).select_from(RatingEvent))).scalar_one()
    assert ratings == 0
    assert events == 0
