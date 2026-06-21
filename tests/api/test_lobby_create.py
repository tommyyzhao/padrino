"""Tests for private friend lobby create/configure (US-147).

Covers ``POST /lobbies`` (host config: ruleset/size, identity mode, theme pack,
bot pre-pick vs curated auto-fill, stakes pinned CASUAL) and ``GET /lobbies/{id}``
(member-scoped, counts-only composition). Also asserts the lobby tables round-trip
through the schema, the lobby references the dormant Humans-Included league (never
a scientific league), and a non-member cannot read the lobby.
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
from padrino.core.enums import LeagueKind, LobbySeatKind
from padrino.db.models import (
    AgentBuild,
    League,
    Lobby,
    LobbyMember,
    LobbySeat,
    ModelConfig,
    ModelProvider,
    PromptVersion,
    Rating,
    RatingEvent,
)


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
    jar = resp.cookies
    return jar[HUMAN_SESSION_COOKIE]


async def _make_agent_build(session: AsyncSession, *, active: bool = True) -> AgentBuild:
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
        prompt_hash="hash-us147",
    )
    session.add(pv)
    await session.flush()
    ab = AgentBuild(
        display_name="cerebras/glm-4.7@v1",
        model_config_id=mc.id,
        prompt_version_id=pv.id,
        adapter_version="2026.05",
        inference_params={"temperature": 0.7},
        active=active,
    )
    session.add(ab)
    await session.flush()
    await session.commit()
    return ab


@pytest.mark.asyncio
async def test_create_lobby_defaults_casual_anonymous_open(client: AsyncClient) -> None:
    token = await _guest_token(client)
    resp = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ruleset_id"] == "mini7_v1"
    assert body["identity_mode"] == "ANONYMOUS"
    assert body["stakes"] == "CASUAL"
    assert body["status"] == "OPEN"
    assert body["game_id"] is None
    assert body["member_count"] == 1
    # mini7_v1 = 7 seats: 1 host HUMAN + 6 AI seats.
    assert body["composition"] == {"human_count": 1, "ai_count": 6, "total": 7}


@pytest.mark.asyncio
async def test_create_lobby_requires_human_session(client: AsyncClient) -> None:
    resp = await client.post("/lobbies", json={"ruleset_id": "mini7_v1"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_lobby_rejects_unknown_ruleset(client: AsyncClient) -> None:
    token = await _guest_token(client)
    resp = await client.post(
        "/lobbies",
        json={"ruleset_id": "not_a_ruleset"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_lobby_bench10_composition(client: AsyncClient) -> None:
    token = await _guest_token(client)
    resp = await client.post(
        "/lobbies",
        json={"ruleset_id": "bench10_v1", "identity_mode": "TRANSPARENT"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["identity_mode"] == "TRANSPARENT"
    assert body["composition"] == {"human_count": 1, "ai_count": 9, "total": 10}


@pytest.mark.asyncio
async def test_create_lobby_with_theme_pack(client: AsyncClient) -> None:
    token = await _guest_token(client)
    resp = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1", "theme_pack_id": "noir_1930s"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["theme_pack_id"] == "noir_1930s"


@pytest.mark.asyncio
async def test_create_lobby_prepick_models(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        build = await _make_agent_build(session)
        build_id = str(build.id)
    token = await _guest_token(client)
    resp = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1", "prepick_agent_build_ids": [build_id]},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 201, resp.text
    lobby_id = uuid.UUID(resp.json()["id"])
    async with session_factory() as session:
        seats = list(
            (
                await session.execute(
                    select(LobbySeat)
                    .where(LobbySeat.lobby_id == lobby_id)
                    .order_by(LobbySeat.seat_index)
                )
            ).scalars()
        )
    assert seats[0].seat_kind == LobbySeatKind.HUMAN.value
    assert seats[0].member_id is not None
    assert seats[1].seat_kind == LobbySeatKind.AI.value
    assert str(seats[1].agent_build_id) == build_id
    # The remaining AI seats are left empty for curated auto-fill (US-149).
    assert seats[2].agent_build_id is None


@pytest.mark.asyncio
async def test_create_lobby_rejects_unknown_prepick_model(client: AsyncClient) -> None:
    token = await _guest_token(client)
    resp = await client.post(
        "/lobbies",
        json={
            "ruleset_id": "mini7_v1",
            "prepick_agent_build_ids": ["00000000-0000-0000-0000-000000000000"],
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "unknown_prepick_model"


@pytest.mark.asyncio
async def test_create_lobby_rejects_inactive_prepick_model(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        build = await _make_agent_build(session, active=False)
        build_id = str(build.id)
    token = await _guest_token(client)
    resp = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1", "prepick_agent_build_ids": [build_id]},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_lobby_rejects_too_many_prepick_models(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        build = await _make_agent_build(session)
        build_id = str(build.id)
    token = await _guest_token(client)
    # mini7_v1 has only 6 AI seats; 7 pre-picks is one too many.
    resp = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1", "prepick_agent_build_ids": [build_id] * 7},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "too_many_prepick_models"


@pytest.mark.asyncio
async def test_lobby_references_humans_included_league(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _guest_token(client)
    resp = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 201
    league_id = uuid.UUID(resp.json()["league_id"])
    async with session_factory() as session:
        league = await session.get(League, league_id)
    assert league is not None
    assert league.kind == LeagueKind.HUMANS_INCLUDED.value
    assert league.ranked is False


@pytest.mark.asyncio
async def test_creating_lobby_writes_zero_scientific_rating_rows(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _guest_token(client)
    resp = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 201
    async with session_factory() as session:
        ratings = (await session.execute(select(func.count()).select_from(Rating))).scalar_one()
        events = (await session.execute(select(func.count()).select_from(RatingEvent))).scalar_one()
    assert ratings == 0
    assert events == 0


@pytest.mark.asyncio
async def test_get_lobby_member_scoped(client: AsyncClient) -> None:
    host_token = await _guest_token(client)
    created = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1"},
        cookies={HUMAN_SESSION_COOKIE: host_token},
    )
    lobby_id = created.json()["id"]

    # The host (a member) can read it.
    got = await client.get(f"/lobbies/{lobby_id}", cookies={HUMAN_SESSION_COOKIE: host_token})
    assert got.status_code == 200
    assert got.json()["id"] == lobby_id
    assert got.json()["composition"] == {"human_count": 1, "ai_count": 6, "total": 7}

    # A different human who is NOT a member gets a 404 (cannot even probe).
    other_token = await _guest_token(client)
    denied = await client.get(f"/lobbies/{lobby_id}", cookies={HUMAN_SESSION_COOKIE: other_token})
    assert denied.status_code == 404


@pytest.mark.asyncio
async def test_get_unknown_lobby_404(client: AsyncClient) -> None:
    token = await _guest_token(client)
    resp = await client.get(
        "/lobbies/00000000-0000-0000-0000-000000000000",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_lobby_member_and_seat_rows_roundtrip(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _guest_token(client)
    resp = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    lobby_id = uuid.UUID(resp.json()["id"])
    async with session_factory() as session:
        lobby = await session.get(Lobby, lobby_id)
        assert lobby is not None
        assert lobby.lobby_seed  # a non-empty deterministic seed is stored
        members = list(
            (
                await session.execute(select(LobbyMember).where(LobbyMember.lobby_id == lobby_id))
            ).scalars()
        )
        seats = list(
            (
                await session.execute(select(LobbySeat).where(LobbySeat.lobby_id == lobby_id))
            ).scalars()
        )
    assert len(members) == 1
    assert members[0].is_host is True
    assert len(seats) == 7
