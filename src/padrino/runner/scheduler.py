"""Async gauntlet scheduler (US-054).

``run_scheduler`` drains ``Gauntlet.status='PENDING'`` rows in order of
``created_at``, flips each to ``RUNNING``, dispatches its child games through
an :class:`asyncio.Semaphore`-bounded executor, writes ``heartbeat_at`` every
``heartbeat_interval_s`` while a gauntlet is in flight, and on completion
flips status to ``COMPLETED`` plus stamps ``completed_at``. Crash recovery
runs once at startup: every ``RUNNING`` row whose heartbeat is older than
``heartbeat_interval_s * stale_factor`` (or NULL) is reset to ``PENDING`` and
picked up by the normal loop.

This module lives in the impure runner layer and is allowed to read wall-clock
and import :mod:`asyncio`. ``game_runner.py``'s purity-firewall test
(``tests/runner/test_game_runner.py::test_game_runner_does_not_import_forbidden_modules``)
scans only ``game_runner.py``; sibling modules are exempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.repositories import games as games_repo
from padrino.db.repositories import gauntlets as gauntlets_repo
from padrino.llm.adapter import LlmAdapter
from padrino.llm.mock import NoopMockAdapter
from padrino.observability.events import (
    EVENT_SCHEDULER_GAUNTLET_COMPLETED,
    EVENT_SCHEDULER_GAUNTLET_STARTED,
    EVENT_SCHEDULER_HEARTBEAT,
    EVENT_SCHEDULER_STALE_RESET,
    EVENT_SCHEDULER_TICK,
)
from padrino.observability.metrics import scheduler_inflight_gauntlets
from padrino.runner.game_runner import GameConfig, GamePersistence, run_game

_logger = structlog.get_logger("padrino.scheduler")

DEFAULT_POLL_INTERVAL_S: Final[float] = 1.0
DEFAULT_HEARTBEAT_INTERVAL_S: Final[float] = 5.0
DEFAULT_STALE_FACTOR: Final[float] = 2.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


# Type aliases for injectable seams.
AdapterFactory = Callable[[], LlmAdapter]
GameExecutor = Callable[[GameConfig, GamePersistence, LlmAdapter, bool], Awaitable[None]]
Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


async def _default_game_executor(
    config: GameConfig,
    persistence: GamePersistence,
    adapter: LlmAdapter,
    ranked: bool,
) -> None:
    await run_game(config, adapter, ranked, persistence=persistence)


@dataclass(frozen=True, slots=True)
class SchedulerOptions:
    """Tunables for :func:`run_scheduler` (test-only injection seam)."""

    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S
    stale_factor: float = DEFAULT_STALE_FACTOR


async def _gauntlet_context(
    session_factory: async_sessionmaker[AsyncSession],
    gauntlet_id: uuid.UUID,
) -> tuple[str, dict[str, uuid.UUID], uuid.UUID, list[uuid.UUID]] | None:
    """Return ``(gauntlet_seed, agent_builds_by_seat, league_id, child_game_ids)``.

    ``agent_builds_by_seat`` maps ``P{slot+1:02d}`` → roster's agent_build_id,
    matching the convention used by ``demo_gauntlet`` and the role assignment
    in ``padrino.core.engine.role_assignment``. ``child_game_ids`` is the
    deterministic list of child game UUIDs ordered by id (matches creation
    order in the API route).
    """
    async with session_factory() as session:
        gauntlet = await gauntlets_repo.get(session, gauntlet_id)
        if gauntlet is None:
            return None
        slots = await gauntlets_repo.list_roster_slots(session, gauntlet_id)
        agent_builds_by_seat = {
            f"P{slot.slot_index + 1:02d}": slot.agent_build_id for slot in slots
        }
        games = await games_repo.list_by_gauntlet(session, gauntlet_id)
        child_ids = [g.id for g in games if g.status != "COMPLETED"]
        return (gauntlet.gauntlet_seed, agent_builds_by_seat, gauntlet.league_id, child_ids)


async def _run_one_game(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    gauntlet_id: uuid.UUID,
    game_id: uuid.UUID,
    agent_builds_by_seat: dict[str, uuid.UUID],
    league_id: uuid.UUID,
    semaphore: asyncio.Semaphore,
    adapter_factory: AdapterFactory,
    game_executor: GameExecutor,
    ranked: bool,
) -> None:
    async with semaphore:
        async with session_factory() as session:
            game = await games_repo.get(session, game_id)
            if game is None:
                return
            game_seed = game.game_seed
            ruleset_id = game.ruleset_id

        adapter = adapter_factory()
        config = GameConfig(
            game_id=str(game_id),
            game_seed=game_seed,
            ruleset_id=ruleset_id,
        )
        persistence = GamePersistence(
            session_factory=session_factory,
            game_id=game_id,
            agent_builds=agent_builds_by_seat,
            league_id=league_id,
        )
        structlog.contextvars.bind_contextvars(
            gauntlet_id=str(gauntlet_id),
            game_id=str(game_id),
        )
        try:
            await game_executor(config, persistence, adapter, ranked)
        finally:
            structlog.contextvars.unbind_contextvars("gauntlet_id", "game_id")


async def _heartbeat_loop(
    session_factory: async_sessionmaker[AsyncSession],
    gauntlet_id: uuid.UUID,
    *,
    interval_s: float,
    clock: Clock,
    sleeper: Sleeper,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            pass
        else:
            return
        now = clock()
        async with session_factory() as session, session.begin():
            await gauntlets_repo.update_heartbeat(session, gauntlet_id, now=now)
        _logger.info(
            EVENT_SCHEDULER_HEARTBEAT,
            gauntlet_id=str(gauntlet_id),
        )


async def _drive_gauntlet(
    session_factory: async_sessionmaker[AsyncSession],
    gauntlet_id: uuid.UUID,
    *,
    semaphore: asyncio.Semaphore,
    adapter_factory: AdapterFactory,
    game_executor: GameExecutor,
    options: SchedulerOptions,
    clock: Clock,
    sleeper: Sleeper,
) -> None:
    """Run every child game of one gauntlet and finalize on success."""
    ctx = await _gauntlet_context(session_factory, gauntlet_id)
    if ctx is None:
        return
    gauntlet_seed, agent_builds_by_seat, league_id, child_game_ids = ctx

    _logger.info(
        EVENT_SCHEDULER_GAUNTLET_STARTED,
        gauntlet_id=str(gauntlet_id),
        games=len(child_game_ids),
        gauntlet_seed=gauntlet_seed,
    )
    scheduler_inflight_gauntlets.inc()

    hb_stop = asyncio.Event()
    hb_task = asyncio.create_task(
        _heartbeat_loop(
            session_factory,
            gauntlet_id,
            interval_s=options.heartbeat_interval_s,
            clock=clock,
            sleeper=sleeper,
            stop=hb_stop,
        ),
        name=f"scheduler-heartbeat-{gauntlet_id}",
    )
    ranked = bool(agent_builds_by_seat)

    try:
        await asyncio.gather(
            *(
                _run_one_game(
                    session_factory,
                    gauntlet_id=gauntlet_id,
                    game_id=gid,
                    agent_builds_by_seat=agent_builds_by_seat,
                    league_id=league_id,
                    semaphore=semaphore,
                    adapter_factory=adapter_factory,
                    game_executor=game_executor,
                    ranked=ranked,
                )
                for gid in child_game_ids
            )
        )
    finally:
        hb_stop.set()
        await hb_task
        scheduler_inflight_gauntlets.dec()

    completed_at = clock()
    async with session_factory() as session, session.begin():
        await gauntlets_repo.mark_completed(session, gauntlet_id, now=completed_at)

    _logger.info(
        EVENT_SCHEDULER_GAUNTLET_COMPLETED,
        gauntlet_id=str(gauntlet_id),
        games=len(child_game_ids),
    )


async def _recover_stale_running(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stale_threshold_s: float,
    clock: Clock,
) -> list[uuid.UUID]:
    older_than = datetime.fromtimestamp(clock().timestamp() - stale_threshold_s, tz=UTC)
    async with session_factory() as session, session.begin():
        reset = await gauntlets_repo.reset_stale_running(session, older_than=older_than)
    if reset:
        _logger.info(
            EVENT_SCHEDULER_STALE_RESET,
            gauntlet_ids=[str(g) for g in reset],
            stale_threshold_s=stale_threshold_s,
        )
    return reset


async def _claim_next_pending(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    clock: Clock,
) -> uuid.UUID | None:
    async with session_factory() as session, session.begin():
        gauntlet = await gauntlets_repo.claim_oldest_pending(session, now=clock())
        if gauntlet is None:
            return None
        return gauntlet.id


async def run_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    concurrency: int,
    stop_event: asyncio.Event,
    adapter_factory: AdapterFactory | None = None,
    game_executor: GameExecutor | None = None,
    semaphore: asyncio.Semaphore | None = None,
    options: SchedulerOptions | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
) -> None:
    """Drain pending gauntlets until ``stop_event`` is set.

    The loop performs crash recovery once at startup, then on each tick claims
    one ``PENDING`` gauntlet (flip to ``RUNNING`` atomically with a heartbeat
    stamp) and awaits its completion before claiming the next. In-flight
    games run concurrently up to the supplied :class:`asyncio.Semaphore`
    (defaults to ``Semaphore(concurrency)``). Setting ``stop_event`` causes
    the loop to finish its current gauntlet and return; mid-gauntlet games
    are NOT aborted — they finish so partial state never lands.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")
    opts = options or SchedulerOptions()
    if opts.heartbeat_interval_s <= 0:
        raise ValueError("heartbeat_interval_s must be > 0")
    if opts.stale_factor <= 0:
        raise ValueError("stale_factor must be > 0")
    if opts.poll_interval_s <= 0:
        raise ValueError("poll_interval_s must be > 0")

    sem = semaphore or asyncio.Semaphore(concurrency)
    executor = game_executor or _default_game_executor
    make_adapter = adapter_factory or NoopMockAdapter
    tick_clock: Clock = clock or _utcnow
    tick_sleeper: Sleeper = sleeper or asyncio.sleep

    stale_threshold_s = opts.heartbeat_interval_s * opts.stale_factor
    await _recover_stale_running(
        session_factory,
        stale_threshold_s=stale_threshold_s,
        clock=tick_clock,
    )

    while not stop_event.is_set():
        _logger.info(EVENT_SCHEDULER_TICK)
        gauntlet_id = await _claim_next_pending(session_factory, clock=tick_clock)
        if gauntlet_id is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=opts.poll_interval_s)
            continue
        await _drive_gauntlet(
            session_factory,
            gauntlet_id,
            semaphore=sem,
            adapter_factory=make_adapter,
            game_executor=executor,
            options=opts,
            clock=tick_clock,
            sleeper=tick_sleeper,
        )


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_STALE_FACTOR",
    "AdapterFactory",
    "Clock",
    "GameExecutor",
    "SchedulerOptions",
    "Sleeper",
    "run_scheduler",
]
