"""Tests for GET /healthz/scheduler (US-060).

The endpoint surfaces the scheduler worker's last heartbeat, queue depth,
and oldest pending age so operators can alert on a stuck worker. We seed
heartbeats and gauntlet rows directly instead of running the scheduler so
each ok / degraded / down branch is asserted in isolation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.core.rulesets import mini7_v1
from padrino.db.repositories import gauntlets as gauntlets_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import prompt_versions as prompt_versions_repo
from padrino.db.repositories import scheduler_heartbeats as scheduler_heartbeats_repo


async def _http_client(app: object) -> AsyncClient:
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    return AsyncClient(transport=transport, base_url="http://testserver")


async def _seed_prompt_and_league(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    prompt_hash: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as session, session.begin():
        league = await leagues_repo.create(
            session, name="L", ruleset_id=mini7_v1.RULESET_ID, ranked=False
        )
        pv = await prompt_versions_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            version="v",
            system_prompt="s",
            developer_prompt="d",
            response_schema={"type": "object"},
            prompt_hash=prompt_hash,
        )
        return league.id, pv.id


async def _insert_gauntlet(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    league_id: uuid.UUID,
    prompt_version_id: uuid.UUID,
    status: str,
    created_at: datetime,
    seed: str,
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        g = await gauntlets_repo.create(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=prompt_version_id,
            clone_count=1,
            gauntlet_seed=seed,
            ranked=False,
            status=status,
        )
        g.created_at = created_at
        if status == "RUNNING":
            g.heartbeat_at = created_at
        await session.flush()
        return g.id


async def test_healthz_scheduler_reports_down_when_no_heartbeat(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(session_factory=session_factory)
    client = await _http_client(app)
    async with client:
        response = await client.get("/healthz/scheduler")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "down"
    assert body["last_heartbeat_at"] is None
    assert body["pending_gauntlets"] == 0
    assert body["running_gauntlets"] == 0
    assert body["oldest_pending_age_s"] is None


async def test_healthz_scheduler_reports_down_when_heartbeat_stale(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    stale = datetime.now(UTC) - timedelta(seconds=120)
    async with session_factory() as session, session.begin():
        await scheduler_heartbeats_repo.upsert(session, worker_id="host-a:1", beat_at=stale)

    app = create_app(session_factory=session_factory)
    client = await _http_client(app)
    async with client:
        response = await client.get("/healthz/scheduler")
    body = response.json()
    assert body["status"] == "down"
    assert body["last_heartbeat_at"] is not None


async def test_healthz_scheduler_reports_degraded_when_pending_too_old(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    league_id, pv_id = await _seed_prompt_and_league(session_factory, prompt_hash="deg")
    await _insert_gauntlet(
        session_factory,
        league_id=league_id,
        prompt_version_id=pv_id,
        status="PENDING",
        created_at=now - timedelta(seconds=180),
        seed="deg-seed",
    )
    async with session_factory() as session, session.begin():
        await scheduler_heartbeats_repo.upsert(
            session, worker_id="host-a:1", beat_at=now - timedelta(seconds=2)
        )

    app = create_app(session_factory=session_factory)
    client = await _http_client(app)
    async with client:
        response = await client.get("/healthz/scheduler")
    body = response.json()
    assert body["status"] == "degraded"
    assert body["pending_gauntlets"] == 1
    assert body["running_gauntlets"] == 0
    assert body["oldest_pending_age_s"] is not None
    assert body["oldest_pending_age_s"] > 60


async def test_healthz_scheduler_reports_ok_when_recent_heartbeat_and_queue_fresh(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    league_id, pv_id = await _seed_prompt_and_league(session_factory, prompt_hash="ok")
    await _insert_gauntlet(
        session_factory,
        league_id=league_id,
        prompt_version_id=pv_id,
        status="PENDING",
        created_at=now - timedelta(seconds=5),
        seed="ok-seed-pending",
    )
    await _insert_gauntlet(
        session_factory,
        league_id=league_id,
        prompt_version_id=pv_id,
        status="RUNNING",
        created_at=now - timedelta(seconds=10),
        seed="ok-seed-running",
    )
    async with session_factory() as session, session.begin():
        await scheduler_heartbeats_repo.upsert(
            session, worker_id="host-a:1", beat_at=now - timedelta(seconds=1)
        )

    app = create_app(session_factory=session_factory)
    client = await _http_client(app)
    async with client:
        response = await client.get("/healthz/scheduler")
    body = response.json()
    assert body["status"] == "ok"
    assert body["pending_gauntlets"] == 1
    assert body["running_gauntlets"] == 1
    assert body["oldest_pending_age_s"] is not None
    assert body["oldest_pending_age_s"] < 60


async def test_healthz_scheduler_uses_latest_beat_across_multiple_workers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    stale = now - timedelta(seconds=120)
    fresh = now - timedelta(seconds=1)
    async with session_factory() as session, session.begin():
        await scheduler_heartbeats_repo.upsert(session, worker_id="host-a:1", beat_at=stale)
        await scheduler_heartbeats_repo.upsert(session, worker_id="host-b:2", beat_at=fresh)

    app = create_app(session_factory=session_factory)
    client = await _http_client(app)
    async with client:
        response = await client.get("/healthz/scheduler")
    body = response.json()
    # Latest beat across workers wins — endpoint reports ok even with one stale worker.
    assert body["status"] == "ok"
    last = datetime.fromisoformat(body["last_heartbeat_at"].replace("Z", "+00:00"))
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    assert abs((last - fresh).total_seconds()) < 1.0


async def test_healthz_scheduler_returns_503_when_no_session_factory_configured() -> None:
    app = create_app()
    client = await _http_client(app)
    async with client:
        response = await client.get("/healthz/scheduler")
    assert response.status_code == 503
