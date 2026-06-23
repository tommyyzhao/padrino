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
import contextlib
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import LogCapture

from padrino.core.rulesets import mini7_v1
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


class _FailingExecutor(_FakeExecutor):
    """Fake executor that fails exactly one selected child game."""

    def __init__(self, *, failing_game_id: uuid.UUID) -> None:
        super().__init__()
        self.failing_game_id = failing_game_id

    async def __call__(
        self,
        config: GameConfig,
        persistence: GamePersistence,
        adapter: LlmAdapter,
        ranked: bool,
    ) -> None:
        if persistence.game_id == self.failing_game_id:
            self.calls.append((config, persistence, ranked))
            raise RuntimeError("injected child-game failure")
        await super().__call__(config, persistence, adapter, ranked)


def _adapter_factory_noop() -> LlmAdapter:
    return NoopMockAdapter()


# US-118 flake burn-down: the scheduler tests previously polled the gauntlet
# status with a TIGHT iteration budget (e.g. ``for _ in range(200)`` ~= 2s),
# then set the stop event UNCONDITIONALLY. Under load that budget could expire
# before the gauntlet reached its terminal status, tripping ``stop`` early and
# making the post-run status assertions flaky. ``_drive_scheduler_until`` keeps
# the original, deliberately cooperative poll shape (sleep FIRST, then read —
# this yields the event loop / the single shared in-memory SQLite connection to
# the scheduler's drive+write coroutines on every iteration, which is why the
# scheduler actually makes progress) but raises the budget to a generous
# deterministic bound. It also NEVER trips ``stop`` until the target status is
# observed, so a slow-but-correct run finalizes instead of being cut short, and
# fails loudly (rather than hanging) if the bound is genuinely exceeded.

# Cooperative status polling. A coarse 50ms interval (vs the old 10ms) is
# deliberate: every watcher DB read contends for the single shared in-memory
# SQLite connection with the scheduler's drive/write coroutines, so polling less
# often lets the scheduler make progress far faster (a tight 10ms loop could
# starve it into a multi-second-to-stalled drive). 600 polls = ~30s budget:
# orders of magnitude more than a correct run needs (~1.5s) yet still bounded.
_SCHEDULER_POLL_INTERVAL_S = 0.05
_SCHEDULER_MAX_POLLS = 600
# Hard ceiling on the scheduler coroutine itself: even after ``stop`` is set, a
# genuinely wedged run (e.g. stuck inside ``_drive_gauntlet``) must FAIL LOUDLY
# rather than hang the whole suite. Comfortably larger than the watcher budget.
_SCHEDULER_RUN_TIMEOUT_S = 60.0


async def _drive_scheduler_until(
    session_factory: async_sessionmaker[AsyncSession],
    gauntlet_id: uuid.UUID,
    target_status: str,
    *,
    stop: asyncio.Event,
    run: Awaitable[None],
) -> None:
    """Run the scheduler until ``gauntlet_id`` reaches ``target_status``.

    The watcher sets ``stop`` once the gauntlet reaches the target status (or the
    generous poll budget is exhausted), at which point the scheduler returns. The
    scheduler coroutine is wrapped in a hard timeout so a wedged run surfaces as a
    failure instead of hanging; the watcher is bounded and always awaited clean.
    """

    async def _watch() -> None:
        for _ in range(_SCHEDULER_MAX_POLLS):
            await asyncio.sleep(_SCHEDULER_POLL_INTERVAL_S)
            async with session_factory() as session:
                g = await gauntlets_repo.get(session, gauntlet_id)
            if g is not None and g.status == target_status:
                break
        stop.set()

    watcher = asyncio.create_task(_watch())
    try:
        await asyncio.wait_for(run, timeout=_SCHEDULER_RUN_TIMEOUT_S)
    finally:
        stop.set()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


async def test_pending_gauntlet_runs_and_finalizes_to_completed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids = await _seed_gauntlet(session_factory, hash_prefix="happy")
    executor = _FakeExecutor()
    stop = asyncio.Event()

    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)
    await _drive_scheduler_until(
        session_factory,
        gauntlet_id,
        "COMPLETED",
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=4,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            options=options,
        ),
    )

    assert {call[1].game_id for call in executor.calls} == set(game_ids)
    async with session_factory() as session:
        g = await gauntlets_repo.get(session, gauntlet_id)
    assert g is not None
    assert g.status == "COMPLETED"
    assert g.completed_at is not None
    assert g.heartbeat_at is None


async def test_child_game_failure_isolated_and_logged(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids = await _seed_gauntlet(
        session_factory,
        hash_prefix="partial",
        clone_count=3,
    )
    failing_game_id = game_ids[1]
    executor = _FailingExecutor(failing_game_id=failing_game_id)
    stop = asyncio.Event()
    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)
    capture = LogCapture()
    original_config = structlog.get_config()
    structlog.reset_defaults()
    structlog.configure(processors=[capture], cache_logger_on_first_use=False)

    try:
        await _drive_scheduler_until(
            session_factory,
            gauntlet_id,
            "COMPLETED",
            stop=stop,
            run=run_scheduler(
                session_factory,
                concurrency=3,
                stop_event=stop,
                adapter_factory=_adapter_factory_noop,
                game_executor=executor,
                options=options,
            ),
        )
    finally:
        structlog.reset_defaults()
        structlog.configure(**original_config)

    assert {call[1].game_id for call in executor.calls} == set(game_ids)
    async with session_factory() as session:
        games = await games_repo.list_by_gauntlet(session, gauntlet_id)
    statuses = {game.id: game.status for game in games}
    assert statuses[failing_game_id] != "COMPLETED"
    assert statuses[game_ids[0]] == "COMPLETED"
    assert statuses[game_ids[2]] == "COMPLETED"

    failure_events = [
        entry for entry in capture.entries if entry["event"] == "scheduler.game.failed"
    ]
    assert len(failure_events) == 1
    assert failure_events[0]["gauntlet_id"] == str(gauntlet_id)
    assert failure_events[0]["game_id"] == str(failing_game_id)
    assert failure_events[0]["error_type"] == "RuntimeError"


async def test_concurrency_cap_honored_via_injected_semaphore(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids = await _seed_gauntlet(session_factory, hash_prefix="cap", clone_count=6)
    executor = _FakeExecutor(delay_s=0.05)
    semaphore = asyncio.Semaphore(2)
    stop = asyncio.Event()

    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)
    await _drive_scheduler_until(
        session_factory,
        gauntlet_id,
        "COMPLETED",
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=2,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            semaphore=semaphore,
            options=options,
        ),
    )

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

    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)
    await _drive_scheduler_until(
        session_factory,
        gauntlet_id,
        "COMPLETED",
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=4,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            options=options,
        ),
    )

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

    # Signal stop as soon as the gauntlet flips to RUNNING (mid-flight).
    await _drive_scheduler_until(
        session_factory,
        gauntlet_id,
        "RUNNING",
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=4,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            options=options,
        ),
    )

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
        # Cooperative poll (sleep first, then read) so the scheduler's drive +
        # heartbeat coroutines get the shared in-memory SQLite connection each
        # iteration. Collect any in-flight heartbeats observed along the way;
        # stop once COMPLETED (or the generous budget is exhausted).
        for _ in range(_SCHEDULER_MAX_POLLS):
            await asyncio.sleep(_SCHEDULER_POLL_INTERVAL_S)
            async with session_factory() as session:
                g = await gauntlets_repo.get(session, gauntlet_id)
            if g is not None:
                if g.status == "RUNNING" and g.heartbeat_at is not None:
                    observed_heartbeats.append(g.heartbeat_at)
                if g.status == "COMPLETED":
                    break
        stop.set()

    waiter = asyncio.create_task(_observe())
    try:
        await asyncio.wait_for(
            run_scheduler(
                session_factory,
                concurrency=1,
                stop_event=stop,
                adapter_factory=_adapter_factory_noop,
                game_executor=executor,
                options=options,
            ),
            timeout=_SCHEDULER_RUN_TIMEOUT_S,
        )
    finally:
        stop.set()
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter

    assert len(observed_heartbeats) >= 1
