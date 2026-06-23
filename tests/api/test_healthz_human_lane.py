"""Tests for GET /healthz/human-lane (US-230)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import SeatKind
from padrino.core.rulesets import mini7_v1
from padrino.db.models import Game, GameSeat
from padrino.db.repositories import scheduler_heartbeats as worker_heartbeats_repo


async def _http_client(app: object) -> AsyncClient:
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    return AsyncClient(transport=transport, base_url="http://testserver")


async def _seed_human_lane_game(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: str,
    seed: str,
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        game = Game(
            gauntlet_id=None,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=seed,
            status=status,
        )
        session.add(game)
        await session.flush()
        seats = assign_roles(seed, mini7_v1)
        for seat in seats:
            is_human = seat.public_player_id == "P01"
            session.add(
                GameSeat(
                    game_id=game.id,
                    public_player_id=seat.public_player_id,
                    seat_index=seat.seat_index,
                    agent_build_id=None,
                    seat_kind=SeatKind.HUMAN.value if is_human else SeatKind.AI.value,
                    role=seat.role.value,
                    faction=seat.faction.value,
                    alive=True,
                )
            )
        await session.flush()
        return game.id


async def test_healthz_human_lane_reports_down_when_no_heartbeat(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(session_factory=session_factory)
    client = await _http_client(app)
    async with client:
        response = await client.get("/healthz/human-lane")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "down"
    assert body["last_heartbeat_at"] is None
    assert body["waiting_games"] == 0
    assert body["running_games"] == 0


async def test_healthz_human_lane_reports_ok_with_recent_lane_heartbeat(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    await _seed_human_lane_game(session_factory, status="PENDING", seed="human-health-pending")
    await _seed_human_lane_game(session_factory, status="RUNNING", seed="human-health-running")
    async with session_factory() as session, session.begin():
        await worker_heartbeats_repo.upsert(
            session,
            worker_id="human-lane:test-worker:42",
            beat_at=now - timedelta(seconds=1),
        )

    app = create_app(session_factory=session_factory)
    client = await _http_client(app)
    async with client:
        response = await client.get("/healthz/human-lane")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["last_heartbeat_at"] is not None
    assert body["waiting_games"] == 1
    assert body["running_games"] == 1


async def test_healthz_human_lane_reports_down_when_heartbeat_stale(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    stale = datetime.now(UTC) - timedelta(seconds=120)
    async with session_factory() as session, session.begin():
        await worker_heartbeats_repo.upsert(
            session,
            worker_id="human-lane:stale-worker",
            beat_at=stale,
        )

    app = create_app(session_factory=session_factory)
    client = await _http_client(app)
    async with client:
        response = await client.get("/healthz/human-lane")

    assert response.status_code == 200
    assert response.json()["status"] == "down"


async def test_scheduler_health_ignores_human_lane_heartbeats(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        await worker_heartbeats_repo.upsert(
            session,
            worker_id="human-lane:fresh-worker",
            beat_at=datetime.now(UTC),
        )

    app = create_app(session_factory=session_factory)
    client = await _http_client(app)
    async with client:
        response = await client.get("/healthz/scheduler")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "down"
    assert body["last_heartbeat_at"] is None


async def test_healthz_human_lane_returns_503_when_no_session_factory_configured() -> None:
    app = create_app()
    client = await _http_client(app)
    async with client:
        response = await client.get("/healthz/human-lane")
    assert response.status_code == 503
