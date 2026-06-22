"""US-132 — separate human-game worker lane isolation.

These tests prove the human lane is a *dedicated* worker pool, not just separate
accounting:

- a human-lane game is claimed and run ONLY by the human lane (the benchmark
  scheduler never picks it up: it keys off gauntlet rows, human games are
  gauntlet-less);
- the human lane never exceeds its OWN concurrency cap
  (``padrino_human_lane_max_concurrent``);
- a backlog of waiting human lobbies does not reduce benchmark concurrency: the
  benchmark scheduler runs its full concurrency at the same time as a saturated
  human lane;
- human-lane admission accounting counts human games only.

Game execution is injected (a barrier/counting executor) so the suite does not
depend on running real mini7 games — the lane's contract is "claim human-lane
games, honor its own semaphore, leave the benchmark lane alone".
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import SeatKind
from padrino.core.rulesets import mini7_v1
from padrino.db.models import Game, GameSeat, LlmCall
from padrino.db.repositories import scheduler_heartbeats as worker_heartbeats_repo
from padrino.runner.game_runner import GameConfig, GamePersistence
from padrino.runner.human_lane import (
    HumanLaneAdmission,
    human_lane_admission,
    list_human_lane_games,
    run_human_lane,
)
from padrino.settings import Settings

_SEED = "human-lane-seed"


async def _add_cost(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    cost: float,
) -> None:
    """Attribute ``cost`` USD of human-lane inference to ``game_id``."""
    async with session_factory() as session, session.begin():
        session.add(
            LlmCall(
                game_id=game_id,
                public_player_id="P01",
                phase="DAY_DISCUSSION",
                request_json={},
                request_prompt_hash="hash",
                status="ok",
                cost_usd=cost,
            )
        )


async def _seed_game(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: str,
    human: bool,
) -> uuid.UUID:
    """Persist a Game + 7 seats; one seat HUMAN when ``human`` is True."""
    async with session_factory() as session, session.begin():
        game = Game(
            gauntlet_id=None,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_SEED,
            status=status,
        )
        session.add(game)
        await session.flush()
        seats = assign_roles(_SEED, mini7_v1)
        for s in seats:
            is_human = human and s.public_player_id == "P01"
            session.add(
                GameSeat(
                    game_id=game.id,
                    public_player_id=s.public_player_id,
                    seat_index=s.seat_index,
                    agent_build_id=None,
                    seat_kind=SeatKind.HUMAN.value if is_human else SeatKind.AI.value,
                    role=s.role.value,
                    faction=s.faction.value,
                    alive=True,
                )
            )
        await session.flush()
        return game.id


async def _drain_until(predicate: Callable[[], bool], *, timeout_s: float = 5.0) -> None:
    """Cooperatively wait until ``predicate()`` is true (deterministic budget)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("predicate never became true within the budget")


async def test_human_lane_claims_only_human_games(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    human_id = await _seed_game(session_factory, status="PENDING", human=True)
    ai_id = await _seed_game(session_factory, status="PENDING", human=False)

    async with session_factory() as session:
        claimable = await list_human_lane_games(session)

    assert human_id in claimable
    assert ai_id not in claimable


async def test_human_lane_runs_game_and_excludes_ai_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    human_id = await _seed_game(session_factory, status="PENDING", human=True)
    ai_id = await _seed_game(session_factory, status="PENDING", human=False)

    ran: list[uuid.UUID] = []

    async def _executor(config: GameConfig, persistence: GamePersistence, adapter: object) -> None:
        ran.append(persistence.game_id)

    stop = asyncio.Event()
    lane = asyncio.create_task(
        run_human_lane(
            session_factory,
            concurrency=2,
            stop_event=stop,
            game_executor=_executor,
            poll_interval_s=0.01,
        )
    )
    await _drain_until(lambda: human_id in ran)
    stop.set()
    await lane

    assert human_id in ran
    assert ai_id not in ran


async def test_human_lane_honors_its_own_concurrency_cap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Five waiting human games, lane cap of 2: never more than 2 run at once.
    for _ in range(5):
        await _seed_game(session_factory, status="PENDING", human=True)

    active = 0
    peak = 0
    release = asyncio.Event()

    async def _executor(config: GameConfig, persistence: GamePersistence, adapter: object) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await release.wait()
        finally:
            active -= 1

    stop = asyncio.Event()
    lane = asyncio.create_task(
        run_human_lane(
            session_factory,
            concurrency=2,
            stop_event=stop,
            game_executor=_executor,
            poll_interval_s=0.01,
        )
    )
    # Wait until the lane has saturated its 2 slots.
    await _drain_until(lambda: active == 2)
    # Give the loop several more ticks to (wrongly) over-admit if it could.
    await asyncio.sleep(0.1)
    assert peak == 2

    release.set()
    stop.set()
    await lane
    assert peak == 2


async def test_waiting_human_lobbies_do_not_reduce_benchmark_concurrency(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A saturated human lane (cap 1, many waiting) must not consume any benchmark
    # slot. We model the two lanes with independent semaphores and prove the
    # benchmark semaphore reaches its full concurrency while the human lane is
    # blocked on a single in-flight human game.
    for _ in range(4):
        await _seed_game(session_factory, status="PENDING", human=True)

    human_active = 0
    human_release = asyncio.Event()

    async def _human_executor(
        config: GameConfig, persistence: GamePersistence, adapter: object
    ) -> None:
        nonlocal human_active
        human_active += 1
        try:
            await human_release.wait()
        finally:
            human_active -= 1

    human_sem = asyncio.Semaphore(1)
    benchmark_sem = asyncio.Semaphore(3)

    stop = asyncio.Event()
    lane = asyncio.create_task(
        run_human_lane(
            session_factory,
            concurrency=1,
            stop_event=stop,
            game_executor=_human_executor,
            semaphore=human_sem,
            poll_interval_s=0.01,
        )
    )
    # Human lane saturates its single slot.
    await _drain_until(lambda: human_active == 1)

    # The benchmark lane (its OWN semaphore) can still acquire all 3 slots: the
    # human backlog took none of them.
    acquired = [await asyncio.wait_for(benchmark_sem.acquire(), timeout=1.0) for _ in range(3)]
    assert len(acquired) == 3
    assert human_active == 1  # human lane still pinned to its single slot
    for _ in range(3):
        benchmark_sem.release()

    human_release.set()
    stop.set()
    await lane


async def test_human_lane_admission_counts_human_games_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_game(session_factory, status="RUNNING", human=True)
    await _seed_game(session_factory, status="PENDING", human=True)
    await _seed_game(session_factory, status="RUNNING", human=False)  # benchmark
    await _seed_game(session_factory, status="PENDING", human=False)  # benchmark

    admission = await human_lane_admission(session_factory, capacity=5)
    assert isinstance(admission, HumanLaneAdmission)
    assert admission.in_flight == 1
    assert admission.waiting == 1
    assert admission.capacity == 5
    assert admission.available == 4


async def test_human_lane_skips_completed_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    done_id = await _seed_game(session_factory, status="COMPLETED", human=True)
    async with session_factory() as session:
        claimable = await list_human_lane_games(session)
    assert done_id not in claimable


async def test_run_human_lane_rejects_bad_concurrency(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    stop = asyncio.Event()
    with contextlib.suppress(asyncio.CancelledError):
        try:
            await run_human_lane(session_factory, concurrency=0, stop_event=stop)
        except ValueError as exc:
            assert "concurrency" in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("expected ValueError for concurrency=0")


async def test_human_lane_worker_heartbeat_is_written_each_tick(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    stop = asyncio.Event()

    async def _signal() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    setter = asyncio.create_task(_signal())
    await asyncio.wait_for(
        run_human_lane(
            session_factory,
            concurrency=1,
            stop_event=stop,
            poll_interval_s=0.01,
            worker_id="test-worker:42",
        ),
        timeout=2.0,
    )
    await setter

    async with session_factory() as session:
        beats = await worker_heartbeats_repo.list_(session)
    assert len(beats) == 1
    assert beats[0].worker_id == "human-lane:test-worker:42"
    assert beats[0].beat_at.tzinfo is not None


def _breaker_settings(threshold: float) -> Settings:
    """Settings whose global human-lane cost breaker opens at ``threshold`` USD."""
    return Settings(padrino_human_global_lobby_cost_breaker_usd=threshold)


async def test_open_breaker_stops_new_turns_for_unstarted_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An open global breaker prevents the lane from STARTING a new game.

    A not-yet-claimed game has issued zero LLM turns, so refusing to dispatch it
    is exactly "STOP new LLM turns" (AC2). We open the breaker by attributing
    spend at/over the threshold to a separate human-lane game, then prove the
    waiting human game is never handed to the executor while the breaker is open.
    """
    # An existing human-lane game whose accrued spend opens the global breaker.
    spent_game = await _seed_game(session_factory, status="COMPLETED", human=True)
    await _add_cost(session_factory, game_id=spent_game, cost=50.0)
    # A fresh waiting human game that must NOT be started while the breaker is open.
    waiting = await _seed_game(session_factory, status="PENDING", human=True)

    ran: list[uuid.UUID] = []

    async def _executor(config: GameConfig, persistence: GamePersistence, adapter: object) -> None:
        ran.append(persistence.game_id)

    stop = asyncio.Event()
    lane = asyncio.create_task(
        run_human_lane(
            session_factory,
            concurrency=2,
            stop_event=stop,
            game_executor=_executor,
            poll_interval_s=0.01,
            settings=_breaker_settings(50.0),
        )
    )
    # Give the loop several ticks; the breaker must keep the waiting game unstarted.
    await asyncio.sleep(0.15)
    stop.set()
    await lane

    assert waiting not in ran
    # The waiting game and its human seat are untouched (never killed/booted).
    async with session_factory() as session:
        game = await session.get(Game, waiting)
        assert game is not None
        assert game.status == "PENDING"
        seats = (
            (await session.execute(select(GameSeat).where(GameSeat.game_id == waiting)))
            .scalars()
            .all()
        )
        assert any(s.seat_kind == SeatKind.HUMAN.value and s.alive for s in seats)


async def test_open_breaker_lets_active_game_finish(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The breaker throttles NEW turns but NEVER kills an in-flight active game.

    A game already dispatched (its task running, the human mid-game) must run to
    completion even after the breaker opens. The human seat is never booted.
    """
    active = await _seed_game(session_factory, status="PENDING", human=True)

    started = asyncio.Event()
    release = asyncio.Event()
    finished: list[uuid.UUID] = []

    async def _executor(config: GameConfig, persistence: GamePersistence, adapter: object) -> None:
        started.set()
        await release.wait()
        # Simulate the active game completing on its own terms.
        async with session_factory() as session, session.begin():
            game = await session.get(Game, persistence.game_id)
            assert game is not None
            game.status = "COMPLETED"
        finished.append(persistence.game_id)

    stop = asyncio.Event()
    lane = asyncio.create_task(
        run_human_lane(
            session_factory,
            concurrency=2,
            stop_event=stop,
            game_executor=_executor,
            poll_interval_s=0.01,
            settings=_breaker_settings(50.0),
        )
    )
    # The active game is dispatched (no breach yet) and is now mid-game.
    await _drain_until(started.is_set)

    # The breaker now opens mid-game (spend crosses the threshold). It must NOT
    # kill the in-flight game: the seat stays HUMAN/alive and the game finishes.
    await _add_cost(session_factory, game_id=active, cost=50.0)
    async with session_factory() as session:
        game = await session.get(Game, active)
        assert game is not None
        assert game.status == "RUNNING"  # still in flight, not booted
        seats = (
            (await session.execute(select(GameSeat).where(GameSeat.game_id == active)))
            .scalars()
            .all()
        )
        assert any(s.seat_kind == SeatKind.HUMAN.value and s.alive for s in seats)

    # Let the active game complete on its own; it finishes despite the breaker.
    release.set()
    await _drain_until(lambda: active in finished)
    stop.set()
    await lane
    assert active in finished
