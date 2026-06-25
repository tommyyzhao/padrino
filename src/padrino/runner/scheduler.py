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
import os
import socket
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.game_status import is_terminal_game_status
from padrino.db.models import Game, GauntletRosterSlot
from padrino.db.repositories import games as games_repo
from padrino.db.repositories import gauntlets as gauntlets_repo
from padrino.db.repositories import scheduler_heartbeats as scheduler_heartbeats_repo
from padrino.llm.adapter import LlmAdapter
from padrino.llm.mock import NoopMockAdapter
from padrino.observability.events import (
    EVENT_SCHEDULER_GAME_FAILED,
    EVENT_SCHEDULER_GAUNTLET_COMPLETED,
    EVENT_SCHEDULER_GAUNTLET_STARTED,
    EVENT_SCHEDULER_HEARTBEAT,
    EVENT_SCHEDULER_STALE_RESET,
    EVENT_SCHEDULER_TICK,
    EVENT_SCHEDULER_WORKER_HEARTBEAT,
)
from padrino.observability.metrics import scheduler_inflight_gauntlets
from padrino.ratings.openskill_service import update_ratings_for_completed_pair
from padrino.runner.benchmark_durability import rehydrate_benchmark_game
from padrino.runner.game_runner import GameConfig, GamePersistence, GameResume, run_game

if TYPE_CHECKING:
    from padrino.settings import Settings

_logger = structlog.get_logger("padrino.scheduler")

DEFAULT_POLL_INTERVAL_S: Final[float] = 1.0
DEFAULT_HEARTBEAT_INTERVAL_S: Final[float] = 5.0
DEFAULT_STALE_FACTOR: Final[float] = 2.0
DEFAULT_GAME_MAX_ATTEMPTS: Final[int] = 2


def _utcnow() -> datetime:
    return datetime.now(UTC)


def default_worker_id() -> str:
    """Return the canonical worker identifier ``"<hostname>:<pid>"``."""
    return f"{socket.gethostname()}:{os.getpid()}"


# Type aliases for injectable seams.
AdapterFactory = Callable[[], LlmAdapter]
GameExecutor = Callable[[GameConfig, GamePersistence, LlmAdapter, bool], Awaitable[None]]
Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]
# Called once per scheduler tick with the current clock time. The US-085
# scheduled-gauntlet job is wired in here via
# ``padrino.scheduler.bootstrap.build_scheduled_gauntlet_tick_hook``.
TickHook = Callable[[datetime], Awaitable[None]]


async def _default_game_executor(
    config: GameConfig,
    persistence: GamePersistence,
    adapter: LlmAdapter,
    ranked: bool,
) -> None:
    await run_game(config, adapter, ranked, persistence=persistence, resume=persistence.resume)


@dataclass(frozen=True, slots=True)
class SchedulerOptions:
    """Tunables for :func:`run_scheduler` (test-only injection seam)."""

    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S
    stale_factor: float = DEFAULT_STALE_FACTOR
    game_max_attempts: int = DEFAULT_GAME_MAX_ATTEMPTS
    enable_game_lease_reaper: bool = False
    game_lease_reaper_interval_s: float = 30.0


def scheduler_options_from_settings(settings: Settings) -> SchedulerOptions:
    """Build scheduler options from operator settings."""
    return SchedulerOptions(
        game_max_attempts=settings.padrino_scheduler_game_max_attempts,
        enable_game_lease_reaper=settings.padrino_enable_game_lease_reaper,
        game_lease_reaper_interval_s=settings.padrino_game_lease_reaper_interval_seconds,
    )


@dataclass(frozen=True, slots=True)
class _ScheduledGame:
    game_id: uuid.UUID
    agent_builds_by_seat: dict[str, uuid.UUID]


@dataclass(frozen=True, slots=True)
class _ScheduledGameFailure:
    game_id: uuid.UUID
    exception: Exception
    attempts: int


async def _gauntlet_context(
    session_factory: async_sessionmaker[AsyncSession],
    gauntlet_id: uuid.UUID,
) -> tuple[str, uuid.UUID, list[_ScheduledGame]] | None:
    """Return ``(gauntlet_seed, league_id, child_games)``.

    Each child carries its own ``agent_builds_by_seat`` mapping. Unpaired games
    use the roster order. Mirror-paired games use the persisted ``pair_leg``:
    leg 0 keeps roster order and leg 1 reverses it while keeping the same
    ``game_seed`` / role layout.
    """
    async with session_factory() as session:
        gauntlet = await gauntlets_repo.get(session, gauntlet_id)
        if gauntlet is None:
            return None
        slots = await gauntlets_repo.list_roster_slots(session, gauntlet_id)
        games = await games_repo.list_by_gauntlet(session, gauntlet_id)
        child_games = [
            _ScheduledGame(
                game_id=g.id,
                agent_builds_by_seat=agent_builds_by_seat_for_game(slots, g),
            )
            for g in games
            if not is_terminal_game_status(g.status)
        ]
        return (gauntlet.gauntlet_seed, gauntlet.league_id, child_games)


def agent_builds_by_seat_for_game(
    roster_slots: Sequence[GauntletRosterSlot],
    game: Game,
) -> dict[str, uuid.UUID]:
    """Return the agent-build seat map for ``game``.

    The mapping convention is ``P{seat_index+1:02d}`` -> agent build id.
    ``pair_leg=1`` mirrors placement by reversing the roster while preserving
    seat ids and therefore preserving the board RNG / role layout.
    """
    ordered = sorted(roster_slots, key=lambda slot: slot.slot_index)
    if game.pair_id is not None and game.pair_leg == 1:
        ordered = list(reversed(ordered))
    return {f"P{i + 1:02d}": slot.agent_build_id for i, slot in enumerate(ordered)}


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
    resume: GameResume | None = None,
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
            resume=resume,
        )
        structlog.contextvars.bind_contextvars(
            gauntlet_id=str(gauntlet_id),
            game_id=str(game_id),
        )
        try:
            await game_executor(config, persistence, adapter, ranked)
        finally:
            structlog.contextvars.unbind_contextvars("gauntlet_id", "game_id")


async def _run_child_games(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    gauntlet_id: uuid.UUID,
    child_games: Sequence[_ScheduledGame],
    league_id: uuid.UUID,
    semaphore: asyncio.Semaphore,
    adapter_factory: AdapterFactory,
    game_executor: GameExecutor,
    ranked: bool,
    max_attempts: int,
    clock: Clock,
) -> list[_ScheduledGameFailure]:
    async def _run_with_retry(child: _ScheduledGame) -> _ScheduledGameFailure | None:
        last_exception: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                async with session_factory() as session:
                    resume = await rehydrate_benchmark_game(session, child.game_id)
                await _run_one_game(
                    session_factory,
                    gauntlet_id=gauntlet_id,
                    game_id=child.game_id,
                    agent_builds_by_seat=child.agent_builds_by_seat,
                    league_id=league_id,
                    semaphore=semaphore,
                    adapter_factory=adapter_factory,
                    game_executor=game_executor,
                    ranked=ranked,
                    resume=resume,
                )
                return None
            except Exception as exc:
                last_exception = exc
                if attempt < max_attempts:
                    _logger.warning(
                        "scheduler.game.retry",
                        gauntlet_id=str(gauntlet_id),
                        game_id=str(child.game_id),
                        attempt=attempt,
                        max_attempts=max_attempts,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    continue

        assert last_exception is not None
        async with session_factory() as session, session.begin():
            await games_repo.mark_failed(session, child.game_id, completed_at=clock())
        return _ScheduledGameFailure(
            game_id=child.game_id,
            exception=last_exception,
            attempts=max_attempts,
        )

    results = await asyncio.gather(
        *(_run_with_retry(child) for child in child_games),
        return_exceptions=True,
    )
    failures: list[_ScheduledGameFailure] = []
    for child, result in zip(child_games, results, strict=True):
        if result is None:
            continue
        if isinstance(result, _ScheduledGameFailure):
            failures.append(result)
            continue
        if isinstance(result, Exception):
            async with session_factory() as session, session.begin():
                await games_repo.mark_failed(session, child.game_id, completed_at=clock())
            failures.append(
                _ScheduledGameFailure(
                    game_id=child.game_id,
                    exception=result,
                    attempts=max_attempts,
                )
            )
    return failures


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
    gauntlet_seed, league_id, child_games = ctx

    _logger.info(
        EVENT_SCHEDULER_GAUNTLET_STARTED,
        gauntlet_id=str(gauntlet_id),
        games=len(child_games),
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
    ranked = any(child.agent_builds_by_seat for child in child_games)

    try:
        failures = await _run_child_games(
            session_factory,
            gauntlet_id=gauntlet_id,
            child_games=child_games,
            league_id=league_id,
            semaphore=semaphore,
            adapter_factory=adapter_factory,
            game_executor=game_executor,
            ranked=ranked,
            max_attempts=options.game_max_attempts,
            clock=clock,
        )
    finally:
        hb_stop.set()
        await hb_task
        scheduler_inflight_gauntlets.dec()

    for failure in failures:
        _logger.error(
            EVENT_SCHEDULER_GAME_FAILED,
            gauntlet_id=str(gauntlet_id),
            game_id=str(failure.game_id),
            error_type=type(failure.exception).__name__,
            error=str(failure.exception),
            attempts=failure.attempts,
        )

    await _rate_completed_pairs_for_gauntlet(
        session_factory,
        gauntlet_id=gauntlet_id,
        league_id=league_id,
    )

    completed_at = clock()
    async with session_factory() as session, session.begin():
        await gauntlets_repo.mark_completed(session, gauntlet_id, now=completed_at)

    _logger.info(
        EVENT_SCHEDULER_GAUNTLET_COMPLETED,
        gauntlet_id=str(gauntlet_id),
        games=len(child_games),
        failed_games=[str(failure.game_id) for failure in failures],
        failed_game_count=len(failures),
    )


async def _rate_completed_pairs_for_gauntlet(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    gauntlet_id: uuid.UUID,
    league_id: uuid.UUID,
) -> None:
    async with session_factory() as session, session.begin():
        pair_ids = list(
            (
                await session.execute(
                    select(Game.pair_id)
                    .where(Game.gauntlet_id == gauntlet_id, Game.pair_id.is_not(None))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        for pair_id in pair_ids:
            if pair_id is None:
                continue
            await update_ratings_for_completed_pair(
                session,
                league_id=league_id,
                pair_id=pair_id,
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


async def _reap_stale_games(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
) -> list[uuid.UUID]:
    async with session_factory() as session, session.begin():
        reset = await games_repo.reset_stale_games(session, now=now)
    if reset:
        _logger.info(
            EVENT_SCHEDULER_STALE_RESET,
            game_ids=[str(g) for g in reset],
        )
    return reset


def _game_reaper_due(
    *,
    enabled: bool,
    last_reaped_at: datetime | None,
    now: datetime,
    interval_s: float,
) -> bool:
    if not enabled:
        return False
    if last_reaped_at is None:
        return True
    return (now - last_reaped_at).total_seconds() >= interval_s


async def _write_worker_heartbeat(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    worker_id: str,
    beat_at: datetime,
) -> None:
    async with session_factory() as session, session.begin():
        await scheduler_heartbeats_repo.upsert(
            session,
            worker_id=worker_id,
            beat_at=beat_at,
        )
    _logger.info(
        EVENT_SCHEDULER_WORKER_HEARTBEAT,
        worker_id=worker_id,
    )


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
    worker_id: str | None = None,
    tick_hook: TickHook | None = None,
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
    if options is None:
        from padrino.settings import get_settings

        opts = scheduler_options_from_settings(get_settings())
    else:
        opts = options
    if opts.heartbeat_interval_s <= 0:
        raise ValueError("heartbeat_interval_s must be > 0")
    if opts.stale_factor <= 0:
        raise ValueError("stale_factor must be > 0")
    if opts.poll_interval_s <= 0:
        raise ValueError("poll_interval_s must be > 0")
    if opts.game_max_attempts < 1:
        raise ValueError("game_max_attempts must be >= 1")
    if opts.enable_game_lease_reaper and opts.game_lease_reaper_interval_s <= 0:
        raise ValueError("game_lease_reaper_interval_s must be > 0")

    sem = semaphore or asyncio.Semaphore(concurrency)
    executor = game_executor or _default_game_executor
    make_adapter = adapter_factory or NoopMockAdapter
    tick_clock: Clock = clock or _utcnow
    tick_sleeper: Sleeper = sleeper or asyncio.sleep
    wid = worker_id or default_worker_id()

    stale_threshold_s = opts.heartbeat_interval_s * opts.stale_factor
    await _recover_stale_running(
        session_factory,
        stale_threshold_s=stale_threshold_s,
        clock=tick_clock,
    )

    last_game_reaped_at: datetime | None = None
    while not stop_event.is_set():
        tick_now = tick_clock()
        _logger.info(EVENT_SCHEDULER_TICK, worker_id=wid)
        await _write_worker_heartbeat(
            session_factory,
            worker_id=wid,
            beat_at=tick_now,
        )
        if tick_hook is not None:
            await tick_hook(tick_now)
        if _game_reaper_due(
            enabled=opts.enable_game_lease_reaper,
            last_reaped_at=last_game_reaped_at,
            now=tick_now,
            interval_s=opts.game_lease_reaper_interval_s,
        ):
            await _reap_stale_games(session_factory, now=tick_now)
            last_game_reaped_at = tick_now
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
    "DEFAULT_GAME_MAX_ATTEMPTS",
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_STALE_FACTOR",
    "AdapterFactory",
    "Clock",
    "GameExecutor",
    "SchedulerOptions",
    "Sleeper",
    "TickHook",
    "agent_builds_by_seat_for_game",
    "default_worker_id",
    "run_scheduler",
    "scheduler_options_from_settings",
]
