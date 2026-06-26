"""US-114: scheduler claim hardening via ``SELECT ... FOR UPDATE SKIP LOCKED``.

Two scheduler replicas that race ``claim_oldest_pending`` must never receive
the same gauntlet row. On PostgreSQL the claim uses
``with_for_update(skip_locked=True)`` so a second concurrent claimer skips the
row the first has row-locked and falls through to the next pending gauntlet (or
``None``). These tests spin up ``postgres:17-alpine`` via testcontainers and are
marked ``@pytest.mark.postgres`` (skipped automatically without a Docker
daemon). The SQLite path is single-writer and exercised by the default suite.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import Campaign
from padrino.db.repositories import (
    agent_builds,
    campaigns,
    games,
    gauntlets,
    leagues,
    model_configs,
    prompt_versions,
    providers,
)

_NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


def _docker_available() -> bool:
    """Return ``True`` when the local Docker daemon answers a ping."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Spin up ``postgres:17-alpine`` once per session and yield an asyncpg URL."""
    if not _docker_available():
        pytest.skip("docker daemon is not reachable")
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - dev dep installed by uv sync
        pytest.skip("testcontainers is not installed")

    container = PostgresContainer("postgres:17-alpine", driver="asyncpg")
    container.start()
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture
async def postgres_engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(postgres_url, pool_size=4, max_overflow=4)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await eng.dispose()


async def _seed_pending_gauntlets(factory: object, count: int) -> list[uuid.UUID]:
    """Create ``count`` PENDING gauntlets and return their ids in creation order."""
    ids: list[uuid.UUID] = []
    async with factory() as session:  # type: ignore[operator]
        provider = await providers.create(
            session, name="cerebras", auth_secret_ref="env:CEREBRAS_API_KEY"
        )
        mc = await model_configs.create(
            session,
            provider_id=provider.id,
            model_name="glm-4.7",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        pv = await prompt_versions.create(
            session,
            ruleset_id="mini7_v1",
            version="v1",
            system_prompt="sys",
            developer_prompt="dev",
            response_schema={"type": "object"},
            prompt_hash=str(uuid.uuid4()),
        )
        await agent_builds.create(
            session,
            display_name="cerebras/glm-4.7@v1",
            model_config_id=mc.id,
            prompt_version_id=pv.id,
            adapter_version="2026.06",
            inference_params={"temperature": 0.7},
            active=True,
        )
        league = await leagues.create(
            session, name="ranked-mini7", ruleset_id="mini7_v1", ranked=True
        )
        for i in range(count):
            g = await gauntlets.create(
                session,
                league_id=league.id,
                ruleset_id="mini7_v1",
                prompt_version_id=pv.id,
                clone_count=1,
                gauntlet_seed=f"seed-{i}",
                ranked=True,
                status="PENDING",
            )
            ids.append(g.id)
        await session.commit()
    return ids


async def _seed_pending_games(factory: object, count: int) -> list[uuid.UUID]:
    """Create ``count`` CREATED games and return their ids."""
    ids: list[uuid.UUID] = []
    async with factory() as session:  # type: ignore[operator]
        for i in range(count):
            game = await games.create(
                session,
                ruleset_id="mini7_v1",
                game_seed=f"game-seed-{i}",
            )
            ids.append(game.id)
        await session.commit()
    return ids


async def _seed_pending_campaigns(factory: object, count: int) -> list[uuid.UUID]:
    """Create ``count`` PENDING campaigns and return their ids."""
    ids: list[uuid.UUID] = []
    async with factory() as session:  # type: ignore[operator]
        league = await leagues.create(
            session, name="campaign-ranked-mini7", ruleset_id="mini7_v1", ranked=True
        )
        for i in range(count):
            campaign = Campaign(
                campaign_seed=f"campaign-seed-{i}",
                ruleset_id="mini7_v1",
                league_id=league.id,
                format="MIRROR",
                player_count=7,
                per_model_game_target=50,
                status=campaigns.CAMPAIGN_STATUS_PENDING,
                sigma_target=2.5,
                rank_stability_k=10,
            )
            session.add(campaign)
            await session.flush()
            ids.append(campaign.id)
        await session.commit()
    return ids


@pytest.mark.postgres
async def test_two_concurrent_claimers_get_distinct_rows(
    postgres_engine: AsyncEngine,
) -> None:
    """Two concurrent claimers each claim a different gauntlet — never the same."""
    factory = create_session_factory(postgres_engine)
    await _seed_pending_gauntlets(factory, count=2)

    async def claim() -> uuid.UUID | None:
        async with factory() as session:
            g = await gauntlets.claim_oldest_pending(session, now=_NOW)
            await session.commit()
            return None if g is None else g.id

    a, b = await asyncio.gather(claim(), claim())

    claimed = {x for x in (a, b) if x is not None}
    # Both ids must be present and distinct: SKIP LOCKED makes the second
    # claimer take the next pending row rather than re-reading the first.
    assert len(claimed) == 2
    assert a != b


@pytest.mark.postgres
async def test_single_pending_row_one_claimer_gets_none(
    postgres_engine: AsyncEngine,
) -> None:
    """With one pending gauntlet, one claimer wins and the other gets None.

    The losing claimer must NOT receive the same row (the failure mode this
    story guards against): it skips the row-locked gauntlet and, finding no
    other pending row, returns ``None``.
    """
    factory = create_session_factory(postgres_engine)
    [only_id] = await _seed_pending_gauntlets(factory, count=1)

    async def claim() -> uuid.UUID | None:
        async with factory() as session:
            g = await gauntlets.claim_oldest_pending(session, now=_NOW)
            await session.commit()
            return None if g is None else g.id

    a, b = await asyncio.gather(claim(), claim())

    results = sorted([a, b], key=lambda x: (x is not None, str(x)))
    assert results[0] is None
    assert results[1] == only_id


@pytest.mark.postgres
async def test_two_concurrent_game_claimers_get_distinct_rows(
    postgres_engine: AsyncEngine,
) -> None:
    """Two concurrent game-grain claimers never receive the same game row."""
    factory = create_session_factory(postgres_engine)
    await _seed_pending_games(factory, count=2)

    async def claim(worker_id: str) -> uuid.UUID | None:
        async with factory() as session:
            game = await games.claim_oldest_pending_game(
                session,
                now=_NOW,
                lease_ttl=timedelta(minutes=5),
                worker_id=worker_id,
            )
            await session.commit()
            return None if game is None else game.id

    a, b = await asyncio.gather(claim("worker-a"), claim("worker-b"))

    claimed = {x for x in (a, b) if x is not None}
    assert len(claimed) == 2
    assert a != b


@pytest.mark.postgres
async def test_two_concurrent_campaign_claimers_get_distinct_rows(
    postgres_engine: AsyncEngine,
) -> None:
    """Two concurrent campaign-grain claimers never receive the same row."""
    factory = create_session_factory(postgres_engine)
    await _seed_pending_campaigns(factory, count=2)

    async def claim(worker_id: str) -> uuid.UUID | None:
        async with factory() as session:
            campaign = await campaigns.claim_campaign(
                session,
                now=_NOW,
                lease_ttl=timedelta(minutes=5),
                worker_id=worker_id,
            )
            await session.commit()
            return None if campaign is None else campaign.id

    a, b = await asyncio.gather(claim("worker-a"), claim("worker-b"))

    claimed = {x for x in (a, b) if x is not None}
    assert len(claimed) == 2
    assert a != b
