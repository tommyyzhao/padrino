"""Tests for the solo instant-match endpoint (US-277)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from http.cookies import SimpleCookie
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.core.enums import LobbySeatKind, LobbyStatus, SeatKind
from padrino.db.models import (
    AgentBuild,
    Game,
    GameSeat,
    HumanCostAdmission,
    League,
    LlmCall,
    Lobby,
    LobbySeat,
    ModelConfig,
    ModelProvider,
    Principal,
    PromptVersion,
    Rating,
    RatingEvent,
)
from padrino.db.repositories import human_principals as principals_repo
from padrino.settings import Settings


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")


@pytest_asyncio.fixture
async def app_and_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[Any, AsyncClient]]:
    app = create_app(session_factory=session_factory, auth_required=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield app, ac


def _pin_settings(
    app: Any,
    *,
    games_per_day: int = 50,
    inference_per_day: float = 1000.0,
    global_breaker: float = 1000.0,
    reserve_usd: float = 0.5,
) -> None:
    app.state.auth_settings = Settings(
        padrino_human_max_games_per_user_per_day=games_per_day,
        padrino_human_max_inference_usd_per_user_per_day=inference_per_day,
        padrino_human_global_lobby_cost_breaker_usd=global_breaker,
        padrino_human_admission_inference_reserve_usd=reserve_usd,
    )


def _cookie_value(headers: Any, name: str) -> str:
    jar = SimpleCookie()
    for raw in headers.get_list("set-cookie"):
        if raw.startswith(f"{name}="):
            jar.load(raw)
    assert name in jar
    return jar[name].value


async def _guest_token(client: AsyncClient) -> str:
    resp = await client.post("/human/guest")
    assert resp.status_code == 201
    return resp.cookies[HUMAN_SESSION_COOKIE]


async def _consent(client: AsyncClient, token: str) -> None:
    resp = await client.post("/human/consent", cookies={HUMAN_SESSION_COOKIE: token})
    assert resp.status_code == 201, resp.text


async def _principal_id_for_token(
    session_factory: async_sessionmaker[AsyncSession], token: str
) -> uuid.UUID:
    async with session_factory() as session:
        record = await principals_repo.get_session_by_token(session, token)
        assert record is not None
        return record.principal_id


async def _seed_curated_builds(session: AsyncSession, *, count: int) -> list[uuid.UUID]:
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
        prompt_hash="hash-us277",
    )
    session.add(pv)
    await session.flush()
    ids: list[uuid.UUID] = []
    for i in range(count):
        build = AgentBuild(
            display_name=f"cerebras/glm-4.7@us277-{i}",
            model_config_id=mc.id,
            prompt_version_id=pv.id,
            adapter_version="2026.05",
            inference_params={"temperature": 0.7},
            active=True,
        )
        session.add(build)
        await session.flush()
        ids.append(build.id)
    await session.commit()
    return ids


async def _count_rows(
    session_factory: async_sessionmaker[AsyncSession],
    model: type[object],
) -> int:
    async with session_factory() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def _live_game_admission_count(
    session_factory: async_sessionmaker[AsyncSession], principal_id: uuid.UUID
) -> int:
    async with session_factory() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(HumanCostAdmission)
                    .where(
                        HumanCostAdmission.principal_id == principal_id,
                        HumanCostAdmission.bucket == "game",
                        HumanCostAdmission.released_at.is_(None),
                    )
                )
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_match_mints_guest_but_refuses_without_consent_before_rows(
    app_and_client: tuple[Any, AsyncClient],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _app, client = app_and_client

    resp = await client.post("/human/match")

    assert resp.status_code == 412, resp.text
    assert resp.json()["detail"] == "consent_required"
    token = _cookie_value(resp.headers, HUMAN_SESSION_COOKIE)
    assert token
    async with session_factory() as session:
        principal_count = (
            await session.execute(select(func.count()).select_from(Principal))
        ).scalar_one()
        session_record = await principals_repo.get_session_by_token(session, token)
    assert principal_count == 1
    assert session_record is not None
    assert await _count_rows(session_factory, Lobby) == 0
    assert await _count_rows(session_factory, Game) == 0


@pytest.mark.asyncio
async def test_match_composes_single_human_lobby_and_launches_game(
    app_and_client: tuple[Any, AsyncClient],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _app, client = app_and_client
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
    token = await _guest_token(client)
    principal_id = await _principal_id_for_token(session_factory, token)
    await _consent(client, token)

    resp = await client.post("/human/match", cookies={HUMAN_SESSION_COOKIE: token})

    assert resp.status_code == 201, resp.text
    assert set(resp.json()) == {"game_id"}
    game_id = uuid.UUID(resp.json()["game_id"])
    async with session_factory() as session:
        lobby = (await session.execute(select(Lobby))).scalar_one()
        assert lobby.status == LobbyStatus.LAUNCHED.value
        assert lobby.game_id == game_id
        assert lobby.stakes == "CASUAL"
        assert lobby.identity_mode == "ANONYMOUS"

        lobby_seats = list(
            (
                await session.execute(
                    select(LobbySeat)
                    .where(LobbySeat.lobby_id == lobby.id)
                    .order_by(LobbySeat.seat_index)
                )
            ).scalars()
        )
        game = await session.get(Game, game_id)
        assert game is not None
        game_seats = list(
            (
                await session.execute(
                    select(GameSeat)
                    .where(GameSeat.game_id == game_id)
                    .order_by(GameSeat.seat_index)
                )
            ).scalars()
        )

    assert [seat.seat_kind for seat in lobby_seats] == [
        LobbySeatKind.HUMAN.value,
        *[LobbySeatKind.AI.value for _ in range(6)],
    ]
    assert len(game_seats) == 7
    human_seats = [seat for seat in game_seats if seat.seat_kind == SeatKind.HUMAN.value]
    ai_seats = [seat for seat in game_seats if seat.seat_kind == SeatKind.AI.value]
    assert len(human_seats) == 1
    assert human_seats[0].seat_index == 0
    assert human_seats[0].occupant_principal_id == principal_id
    assert len(ai_seats) == 6
    assert all(seat.agent_build_id is not None for seat in ai_seats)


@pytest.mark.asyncio
async def test_match_autofill_is_deterministic_for_same_lobby_seed(
    app_and_client: tuple[Any, AsyncClient],
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, client = app_and_client
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
    monkeypatch.setattr("padrino.api.routes.human.secrets.token_hex", lambda _n: "same-seed")

    assignments: list[list[uuid.UUID | None]] = []
    for _ in range(2):
        token = await _guest_token(client)
        await _consent(client, token)
        resp = await client.post("/human/match", cookies={HUMAN_SESSION_COOKIE: token})
        assert resp.status_code == 201, resp.text
        game_id = uuid.UUID(resp.json()["game_id"])
        async with session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(GameSeat)
                        .where(
                            GameSeat.game_id == game_id,
                            GameSeat.seat_kind == SeatKind.AI.value,
                        )
                        .order_by(GameSeat.seat_index)
                    )
                ).scalars()
            )
        assignments.append([row.agent_build_id for row in rows])

    assert assignments[0] == assignments[1]


@pytest.mark.asyncio
async def test_match_refuses_cap_and_consumes_one_game_slot(
    app_and_client: tuple[Any, AsyncClient],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app, client = app_and_client
    _pin_settings(app, games_per_day=1)
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
    token = await _guest_token(client)
    principal_id = await _principal_id_for_token(session_factory, token)
    await _consent(client, token)

    ok = await client.post("/human/match", cookies={HUMAN_SESSION_COOKIE: token})
    assert ok.status_code == 201, ok.text
    assert await _live_game_admission_count(session_factory, principal_id) == 1

    denied = await client.post("/human/match", cookies={HUMAN_SESSION_COOKIE: token})
    assert denied.status_code == 429, denied.text
    assert denied.json()["detail"] == "daily_game_cap_reached"
    assert await _live_game_admission_count(session_factory, principal_id) == 1
    assert await _count_rows(session_factory, Game) == 1


@pytest.mark.asyncio
async def test_match_refuses_global_breaker_before_materializing_game(
    app_and_client: tuple[Any, AsyncClient],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app, client = app_and_client
    _pin_settings(app, global_breaker=5.0)
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
        async with session.begin():
            game = Game(ruleset_id="mini7_v1", game_seed=f"seed-{uuid.uuid4()}", status="RUNNING")
            session.add(game)
            await session.flush()
            session.add(
                GameSeat(
                    game_id=game.id,
                    public_player_id="P00",
                    seat_index=0,
                    seat_kind=SeatKind.HUMAN.value,
                    role="VILLAGER",
                    faction="TOWN",
                    alive=True,
                )
            )
            session.add(
                LlmCall(
                    game_id=game.id,
                    public_player_id="P01",
                    phase="DAY_DISCUSSION",
                    request_json={},
                    request_prompt_hash="hash",
                    status="ok",
                    cost_usd=5.0,
                )
            )
    token = await _guest_token(client)
    await _consent(client, token)

    resp = await client.post("/human/match", cookies={HUMAN_SESSION_COOKIE: token})

    assert resp.status_code == 429, resp.text
    assert resp.json()["detail"] == "breaker_open"
    assert await _count_rows(session_factory, Lobby) == 0
    assert await _count_rows(session_factory, Game) == 1


@pytest.mark.asyncio
async def test_match_is_anonymous_counts_only_and_writes_zero_scientific_ratings(
    app_and_client: tuple[Any, AsyncClient],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _app, client = app_and_client
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
    token = await _guest_token(client)
    await _consent(client, token)

    resp = await client.post("/human/match", cookies={HUMAN_SESSION_COOKIE: token})

    assert resp.status_code == 201, resp.text
    assert set(resp.json()) == {"game_id"}
    game_id = uuid.UUID(resp.json()["game_id"])
    async with session_factory() as session:
        game = await session.get(Game, game_id)
        assert game is not None
        league = (await session.execute(select(League))).scalar_one()
        seats = list(
            (await session.execute(select(GameSeat).where(GameSeat.game_id == game_id))).scalars()
        )
        ratings = (await session.execute(select(func.count()).select_from(Rating))).scalar_one()
        rating_events = (
            await session.execute(select(func.count()).select_from(RatingEvent))
        ).scalar_one()

    assert game.identity_mode == "ANONYMOUS"
    assert league.kind == "HUMANS_INCLUDED"
    assert league.ranked is False
    assert {
        "human_count": sum(seat.seat_kind == SeatKind.HUMAN.value for seat in seats),
        "ai_count": sum(seat.seat_kind == SeatKind.AI.value for seat in seats),
        "total": len(seats),
    } == {"human_count": 1, "ai_count": 6, "total": 7}
    assert ratings == 0
    assert rating_events == 0
