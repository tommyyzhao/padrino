"""US-054 — async gauntlet scheduler.

Covers the pending → running → completed transition, concurrency cap,
crash-recovery (stale RUNNING rows reset to PENDING on startup), and the
SIGTERM-style graceful shutdown via a stop event. Game execution is injected
so the suite does not depend on the deterministic mock-adapter scripts; the
scheduler's contract is "drain pending gauntlets, flip statuses correctly,
honor the semaphore, and heartbeat while running" — not "run mini7 games".
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.repositories import (
    agent_builds as agent_builds_repo,
)
from padrino.db.repositories import (
    games as games_repo,
)
from padrino.db.repositories import (
    gauntlets as gauntlets_repo,
)
from padrino.db.repositories import (
    leagues as leagues_repo,
)
from padrino.db.repositories import (
    model_configs as model_configs_repo,
)
from padrino.db.repositories import (
    prompt_versions as prompt_versions_repo,
)
from padrino.db.repositories import (
    providers as providers_repo,
)
from padrino.db.repositories import (
    scheduler_heartbeats as scheduler_heartbeats_repo,
)
from padrino.llm.adapter import LlmAdapter
from padrino.llm.mock import NoopMockAdapter
from padrino.runner.game_runner import GameConfig, GamePersistence
from padrino.runner.scheduler import (
    SchedulerOptions,
    run_scheduler,
)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


async def _seed_gauntlet(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    hash_prefix: str,
    clone_count: int = 2,
    status: str = "PENDING",
    heartbeat_at: datetime | None = None,
    created_at: datetime | None = None,
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Insert a gauntlet (with status and roster) plus ``clone_count`` games.

    Returns ``(gauntlet_id, [game_id, ...])``.
    """
    async with session_factory() as session, session.begin():
        provider = await providers_repo.create(session, name="p", auth_secret_ref="env:X")
        mc = await model_configs_repo.create(
            session,
            provider_id=provider.id,
            model_name="m",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        pv = await prompt_versions_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            version="v",
            system_prompt="s",
            developer_prompt="d",
            response_schema={"type": "object"},
            prompt_hash=f"{hash_prefix}-prompt",
        )
        league = await leagues_repo.create(
            session, name="L", ruleset_id=mini7_v1.RULESET_ID, ranked=False
        )
        builds: list[uuid.UUID] = []
        for i in range(mini7_v1.PLAYER_COUNT):
            ab = await agent_builds_repo.create(
                session,
                display_name=f"b-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="v",
                inference_params={},
                active=True,
            )
            builds.append(ab.id)
        gauntlet = await gauntlets_repo.create(
            session,
            league_id=league.id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv.id,
            clone_count=clone_count,
            gauntlet_seed=f"{hash_prefix}-seed",
            ranked=False,
            status=status,
        )
        if heartbeat_at is not None:
            gauntlet.heartbeat_at = heartbeat_at
        if created_at is not None:
            gauntlet.created_at = created_at
        for i, ab_id in enumerate(builds):
            await gauntlets_repo.add_roster_slot(session, gauntlet.id, i, ab_id)
        game_ids: list[uuid.UUID] = []
        for i in range(clone_count):
            g = await games_repo.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                game_seed=f"{hash_prefix}-g{i}",
                gauntlet_id=gauntlet.id,
            )
            game_ids.append(g.id)
        return gauntlet.id, game_ids


class _FakeExecutor:
    """In-memory game executor: records calls, optionally sleeps, tracks concurrency."""

    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.calls: list[tuple[GameConfig, GamePersistence, bool]] = []
        self._delay = delay_s
        self.in_flight = 0
        self.peak_concurrency = 0
        self._lock = asyncio.Lock()

    async def __call__(
        self,
        config: GameConfig,
        persistence: GamePersistence,
        adapter: LlmAdapter,
        ranked: bool,
    ) -> None:
        async with self._lock:
            self.in_flight += 1
            self.peak_concurrency = max(self.peak_concurrency, self.in_flight)
        try:
            self.calls.append((config, persistence, ranked))
            if self._delay > 0:
                await asyncio.sleep(self._delay)
            # Flip the game row to COMPLETED so subsequent observability /
            # diagnostics behave like a real terminal run.
            async with persistence.session_factory() as session, session.begin():
                await games_repo.update_status(
                    session,
                    persistence.game_id,
                    status="COMPLETED",
                    terminal_result={"winner": "TOWN", "reason": "stub", "day_terminated": 0},
                )
        finally:
            async with self._lock:
                self.in_flight -= 1


def _adapter_factory_noop() -> LlmAdapter:
    return NoopMockAdapter()


async def test_pending_gauntlet_runs_and_finalizes_to_completed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids = await _seed_gauntlet(session_factory, hash_prefix="happy")
    executor = _FakeExecutor()
    stop = asyncio.Event()

    async def _stop_after_one_iter() -> None:
        # Wait for the gauntlet to be marked COMPLETED, then set stop.
        for _ in range(200):
            await asyncio.sleep(0.01)
            async with session_factory() as session:
                g = await gauntlets_repo.get(session, gauntlet_id)
            if g is not None and g.status == "COMPLETED":
                break
        stop.set()

    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)
    waiter = asyncio.create_task(_stop_after_one_iter())
    await run_scheduler(
        session_factory,
        concurrency=4,
        stop_event=stop,
        adapter_factory=_adapter_factory_noop,
        game_executor=executor,
        options=options,
    )
    await waiter

    assert {call[1].game_id for call in executor.calls} == set(game_ids)
    async with session_factory() as session:
        g = await gauntlets_repo.get(session, gauntlet_id)
    assert g is not None
    assert g.status == "COMPLETED"
    assert g.completed_at is not None
    assert g.heartbeat_at is None


async def test_concurrency_cap_honored_via_injected_semaphore(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids = await _seed_gauntlet(session_factory, hash_prefix="cap", clone_count=6)
    executor = _FakeExecutor(delay_s=0.05)
    semaphore = asyncio.Semaphore(2)
    stop = asyncio.Event()

    async def _waiter() -> None:
        for _ in range(500):
            await asyncio.sleep(0.01)
            async with session_factory() as session:
                g = await gauntlets_repo.get(session, gauntlet_id)
            if g is not None and g.status == "COMPLETED":
                break
        stop.set()

    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)
    waiter = asyncio.create_task(_waiter())
    await run_scheduler(
        session_factory,
        concurrency=2,
        stop_event=stop,
        adapter_factory=_adapter_factory_noop,
        game_executor=executor,
        semaphore=semaphore,
        options=options,
    )
    await waiter

    assert len(executor.calls) == len(game_ids)
    assert executor.peak_concurrency <= 2


async def test_crash_recovery_resets_stale_running_gauntlet(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A RUNNING row with a heartbeat older than 2x the interval simulates a
    # process that died mid-gauntlet.
    stale_hb = datetime.now(UTC) - timedelta(seconds=10)
    gauntlet_id, _game_ids = await _seed_gauntlet(
        session_factory,
        hash_prefix="recover",
        status="RUNNING",
        heartbeat_at=stale_hb,
    )
    executor = _FakeExecutor()
    stop = asyncio.Event()

    async def _waiter() -> None:
        for _ in range(200):
            await asyncio.sleep(0.01)
            async with session_factory() as session:
                g = await gauntlets_repo.get(session, gauntlet_id)
            if g is not None and g.status == "COMPLETED":
                break
        stop.set()

    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)
    waiter = asyncio.create_task(_waiter())
    await run_scheduler(
        session_factory,
        concurrency=4,
        stop_event=stop,
        adapter_factory=_adapter_factory_noop,
        game_executor=executor,
        options=options,
    )
    await waiter

    async with session_factory() as session:
        g = await gauntlets_repo.get(session, gauntlet_id)
    assert g is not None
    assert g.status == "COMPLETED"
    # All games were re-executed after the reset.
    assert len(executor.calls) > 0


async def test_stop_event_finishes_in_flight_gauntlet_before_returning(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids = await _seed_gauntlet(
        session_factory, hash_prefix="sigterm", clone_count=2
    )
    executor = _FakeExecutor(delay_s=0.1)
    stop = asyncio.Event()
    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)

    async def _set_stop_mid_flight() -> None:
        # Wait until the gauntlet has flipped to RUNNING, then signal stop.
        for _ in range(200):
            await asyncio.sleep(0.005)
            async with session_factory() as session:
                g = await gauntlets_repo.get(session, gauntlet_id)
            if g is not None and g.status == "RUNNING":
                break
        stop.set()

    setter = asyncio.create_task(_set_stop_mid_flight())
    await run_scheduler(
        session_factory,
        concurrency=4,
        stop_event=stop,
        adapter_factory=_adapter_factory_noop,
        game_executor=executor,
        options=options,
    )
    await setter

    assert len(executor.calls) == len(game_ids)
    async with session_factory() as session:
        g = await gauntlets_repo.get(session, gauntlet_id)
    assert g is not None
    assert g.status == "COMPLETED"


async def test_no_pending_gauntlets_returns_when_stop_set(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The loop must wake on stop_event even when no work is queued."""
    stop = asyncio.Event()
    options = SchedulerOptions(poll_interval_s=0.5, heartbeat_interval_s=1.0, stale_factor=2.0)

    async def _signal() -> None:
        await asyncio.sleep(0.02)
        stop.set()

    setter = asyncio.create_task(_signal())
    await asyncio.wait_for(
        run_scheduler(
            session_factory,
            concurrency=1,
            stop_event=stop,
            options=options,
        ),
        timeout=2.0,
    )
    await setter


async def test_worker_heartbeat_is_written_each_tick(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The scheduler must write its per-worker heartbeat to ``scheduler_heartbeats``."""
    stop = asyncio.Event()
    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)

    async def _signal() -> None:
        # Allow a couple of tick iterations so the heartbeat row exists.
        await asyncio.sleep(0.05)
        stop.set()

    setter = asyncio.create_task(_signal())
    await asyncio.wait_for(
        run_scheduler(
            session_factory,
            concurrency=1,
            stop_event=stop,
            options=options,
            worker_id="test-worker:42",
        ),
        timeout=2.0,
    )
    await setter

    async with session_factory() as session:
        beats = await scheduler_heartbeats_repo.list_(session)
    assert len(beats) == 1
    assert beats[0].worker_id == "test-worker:42"
    assert beats[0].beat_at.tzinfo is not None


async def test_heartbeat_is_written_while_gauntlet_in_flight(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, _game_ids = await _seed_gauntlet(session_factory, hash_prefix="hb", clone_count=1)
    # Use enough delay so the heartbeat task fires at least once.
    executor = _FakeExecutor(delay_s=0.2)
    stop = asyncio.Event()
    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)

    observed_heartbeats: list[datetime] = []

    async def _observe() -> None:
        for _ in range(300):
            await asyncio.sleep(0.01)
            async with session_factory() as session:
                g = await gauntlets_repo.get(session, gauntlet_id)
            if g is None:
                continue
            if g.status == "RUNNING" and g.heartbeat_at is not None:
                observed_heartbeats.append(g.heartbeat_at)
            if g.status == "COMPLETED":
                break
        stop.set()

    waiter = asyncio.create_task(_observe())
    await run_scheduler(
        session_factory,
        concurrency=1,
        stop_event=stop,
        adapter_factory=_adapter_factory_noop,
        game_executor=executor,
        options=options,
    )
    await waiter

    assert len(observed_heartbeats) >= 1
