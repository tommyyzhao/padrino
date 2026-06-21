"""Separate human-game worker lane (US-132).

Human-multiplayer games last minutes to hours, whereas the benchmark scheduler
(``padrino.runner.scheduler``) is sized for ~45s model turns at a small
concurrency cap (``padrino_max_concurrent_games`` = 3). A single minutes-long
human game would otherwise pin a benchmark slot and starve the science queue.

This module runs human-lane games on a *dedicated* worker lane: its own loop,
its own :class:`asyncio.Semaphore` (sized by ``padrino_human_lane_max_concurrent``),
and its own admission accounting. The benchmark scheduler path is untouched —
``run_human_lane`` shares no semaphore, no queue, and no claim path with
``run_scheduler``, so many waiting human lobbies cannot reduce benchmark
concurrency.

Lane membership is a property of the game, not the loop: a game is "human-lane"
when at least one of its seats was ever occupied by a human (``seat_kind`` in
``{HUMAN, AI_TAKEOVER}``). The benchmark lane's AI-only games are never claimed
here, and human-lane games are never claimed by the benchmark scheduler (its
claim path keys off ``Gauntlet`` rows; human games are gauntlet-less).

Impure runner module: it reads the DB and wall-clock and imports ``asyncio``.
``game_runner.py``'s purity-firewall test scans only ``game_runner.py``; sibling
runner modules are exempt (same exemption the scheduler relies on).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.state import GameState, Seat
from padrino.core.enums import ActionType, SeatKind
from padrino.core.observations import Observation, Ruleset
from padrino.db.models import Game, GameSeat
from padrino.db.repositories import games as games_repo
from padrino.db.repositories import human_action_submissions as human_actions_repo
from padrino.economics.human_cost_governance import global_breaker_open
from padrino.gauntlets.heterogeneous import build_heterogeneous_adapter
from padrino.gauntlets.tournament import project_agent_build
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.llm.adapter import AgentBuild as LlmAgentBuild
from padrino.llm.human_adapter import HumanAdapter, PullAction
from padrino.llm.multiplex import SeatMultiplexAdapter
from padrino.runner.game_runner import GameConfig, GamePersistence, drive_game_loop
from padrino.runner.human_tick import HumanTickConfig, run_human_tick
from padrino.settings import Settings, get_settings

_logger = structlog.get_logger("padrino.runner.human_lane")

DEFAULT_POLL_INTERVAL_S: Final[float] = 1.0
HUMAN_ACTION_POLL_INTERVAL_SECONDS: Final[float] = 0.05
STATUS_COMPLETED: Final[str] = "COMPLETED"
STATUS_RUNNING: Final[str] = "RUNNING"

# A seat is "human-lane" when a human ever occupied it (a live human seat or a
# seat an AI silently took over). AI-only benchmark games have neither.
_HUMAN_LANE_SEAT_KINDS: Final[frozenset[str]] = frozenset(
    {SeatKind.HUMAN.value, SeatKind.AI_TAKEOVER.value}
)

# Statuses a human-lane game can be picked up from: not yet started or in
# flight (resumed after a restart — see US-131 rehydration). A COMPLETED game
# is never re-run.
_CLAIMABLE_STATUSES: Final[frozenset[str]] = frozenset({"CREATED", "PENDING", STATUS_RUNNING})

# Type aliases for injectable seams (mirroring scheduler.py).
AdapterFactory = Callable[[], LlmAdapter]
AiAdapterFactory = Callable[[Mapping[str, LlmAgentBuild]], LlmAdapter]
HumanGameExecutor = Callable[[GameConfig, GamePersistence, LlmAdapter], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class HumanLaneAdmission:
    """Admission snapshot for the human lane, independent of the benchmark lane.

    ``in_flight`` counts only human-lane games currently RUNNING; ``capacity``
    is ``padrino_human_lane_max_concurrent``. ``available`` is how many more
    human games may be admitted right now. This accounting never inspects the
    benchmark scheduler's in-flight games, so the two lanes account separately.
    """

    in_flight: int
    waiting: int
    capacity: int

    @property
    def available(self) -> int:
        return max(0, self.capacity - self.in_flight)


class _InjectedExecutorAdapter:
    """Adapter placeholder for tests that inject their own executor.

    ``run_human_lane`` still passes an adapter argument to custom executors for
    backward-compatible test seams. When the executor is injected and no adapter
    factory is supplied, constructing a real production adapter would force
    isolation tests to seed model rows they never use. This placeholder fails
    loudly if a custom executor accidentally calls it.
    """

    async def complete(self, observation: Observation) -> AdapterResult:
        raise RuntimeError(
            "custom human-lane executor received a placeholder adapter; "
            "pass adapter_factory if the executor calls complete()"
        )


def _action_from_submission(action_type: str, target: str | None) -> Action | None:
    try:
        parsed = ActionType(action_type)
    except ValueError:
        return None
    return Action(type=parsed, target=target)


def _db_backed_pull_action(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    public_player_id: str,
) -> PullAction:
    """Poll the authenticated POST action store for one human seat."""

    async def pull(observation: Observation) -> Action | None:
        async with session_factory() as session:
            row = await human_actions_repo.latest_for_phase(
                session,
                game_id=game_id,
                public_player_id=public_player_id,
                phase=observation.phase,
            )
        if row is None:
            return None
        return _action_from_submission(row.action_type, row.target)

    return pull


def _is_human_controlled(seat: GameSeat) -> bool:
    return seat.seat_kind == SeatKind.HUMAN.value


def _agent_build_id_for_ai_seat(seat: GameSeat) -> uuid.UUID | None:
    return seat.takeover_agent_build_id or seat.agent_build_id


async def build_human_lane_adapter(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    settings: Settings,
    ai_adapter_factory: AiAdapterFactory | None = None,
) -> SeatMultiplexAdapter:
    """Build the production per-seat adapter for a human-lane game.

    HUMAN seats are backed by :class:`HumanAdapter` polling the authenticated
    action submission store. AI / AI_TAKEOVER seats keep the curated
    ``agent_build_id`` materialized at lobby launch and are projected into the
    same heterogeneous LiteLLM adapter path used by model-vs-model games.
    """
    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(GameSeat)
                    .where(GameSeat.game_id == game_id)
                    .order_by(GameSeat.seat_index)
                )
            ).scalars()
        )
        if not rows:
            raise ValueError(f"human-lane game {game_id} has no seats")

        ai_assignments: dict[str, LlmAgentBuild] = {}
        for seat in rows:
            if _is_human_controlled(seat):
                continue
            build_id = _agent_build_id_for_ai_seat(seat)
            if build_id is None:
                raise ValueError(
                    f"AI seat {seat.public_player_id!r} in human-lane game {game_id} "
                    "has no agent_build_id"
                )
            ai_assignments[seat.public_player_id] = await project_agent_build(session, build_id)

    ai_adapter: LlmAdapter | None = None
    if ai_assignments:
        ai_adapter = (
            ai_adapter_factory(ai_assignments)
            if ai_adapter_factory is not None
            else build_heterogeneous_adapter(ai_assignments, settings=settings)
        )

    adapters: dict[str, LlmAdapter] = {}
    for seat in rows:
        if _is_human_controlled(seat):
            adapters[seat.public_player_id] = HumanAdapter(
                pull_action=_db_backed_pull_action(
                    session_factory,
                    game_id=game_id,
                    public_player_id=seat.public_player_id,
                ),
                deadline_seconds=settings.padrino_human_phase_deadline_seconds,
                poll_interval_seconds=HUMAN_ACTION_POLL_INTERVAL_SECONDS,
            )
        elif ai_adapter is not None:
            adapters[seat.public_player_id] = ai_adapter

    return SeatMultiplexAdapter(adapters)


async def _run_human_tick_responses(
    state: GameState,
    event_log: EventLog,
    eligible_seats: Sequence[Seat],
    adapter: LlmAdapter,
    ruleset: Ruleset,
    ranked: bool,
    _timeout_s: float,
    *,
    config: HumanTickConfig,
) -> dict[str, AgentResponse]:
    result = await run_human_tick(
        state,
        event_log,
        eligible_seats,
        adapter,
        ruleset,
        config,
        ranked=ranked,
    )
    return result.responses


def _default_human_game_executor(settings: Settings) -> HumanGameExecutor:
    tick_config = HumanTickConfig(
        phase_deadline_seconds=settings.padrino_human_phase_deadline_seconds,
        release_delay_seconds=settings.padrino_human_release_delay_seconds,
    )

    async def execute(
        config: GameConfig,
        persistence: GamePersistence,
        adapter: LlmAdapter,
    ) -> None:
        # Human-lane games are always casual (ranked=False) — they never write the
        # scientific Rating/RatingEvent tables (segregation, hard rule 8).
        async def tick_runner(
            state: GameState,
            event_log: EventLog,
            eligible_seats: Sequence[Seat],
            tick_adapter: LlmAdapter,
            ruleset: Ruleset,
            ranked: bool,
            timeout_s: float,
        ) -> dict[str, AgentResponse]:
            return await _run_human_tick_responses(
                state,
                event_log,
                eligible_seats,
                tick_adapter,
                ruleset,
                ranked,
                timeout_s,
                config=tick_config,
            )

        await drive_game_loop(
            config,
            adapter,
            False,
            persistence=persistence,
            tick_runner=tick_runner,
        )

    return execute


async def _is_human_lane_game(session: AsyncSession, game_id: uuid.UUID) -> bool:
    stmt = select(GameSeat.seat_kind).where(GameSeat.game_id == game_id)
    kinds = (await session.execute(stmt)).scalars().all()
    return any(k in _HUMAN_LANE_SEAT_KINDS for k in kinds)


async def list_human_lane_games(
    session: AsyncSession,
    *,
    statuses: frozenset[str] | None = None,
) -> list[uuid.UUID]:
    """Return human-lane game ids (a HUMAN/AI_TAKEOVER seat) in ``statuses``.

    Ordered by game id for deterministic claim order. The benchmark lane's
    AI-only games are excluded by construction (no human/takeover seat).
    """
    wanted = statuses if statuses is not None else _CLAIMABLE_STATUSES
    human_game_ids = (
        select(GameSeat.game_id).where(GameSeat.seat_kind.in_(_HUMAN_LANE_SEAT_KINDS)).distinct()
    )
    stmt = (
        select(Game.id)
        .where(Game.id.in_(human_game_ids))
        .where(Game.status.in_(wanted))
        .order_by(Game.id)
    )
    return list((await session.execute(stmt)).scalars())


async def human_lane_admission(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    capacity: int,
) -> HumanLaneAdmission:
    """Compute the human lane's admission snapshot from the DB.

    Counts RUNNING human-lane games as in-flight and CREATED/PENDING human-lane
    games as waiting. This is the human lane's OWN accounting — it never reads
    the benchmark scheduler's gauntlet/game queue.
    """
    async with session_factory() as session:
        in_flight = len(await list_human_lane_games(session, statuses=frozenset({STATUS_RUNNING})))
        waiting = len(
            await list_human_lane_games(session, statuses=frozenset({"CREATED", "PENDING"}))
        )
    return HumanLaneAdmission(in_flight=in_flight, waiting=waiting, capacity=capacity)


async def _claim_game(
    session_factory: async_sessionmaker[AsyncSession],
    game_id: uuid.UUID,
) -> tuple[str, str] | None:
    """Atomically flip a claimable human-lane game to RUNNING.

    Returns ``(game_seed, ruleset_id)`` on a successful claim, or ``None`` when
    the game vanished, already completed, or is not a human-lane game (so a
    concurrent worker / the benchmark lane never double-runs it).
    """
    async with session_factory() as session, session.begin():
        game = await games_repo.get(session, game_id)
        if game is None or game.status == STATUS_COMPLETED:
            return None
        if not await _is_human_lane_game(session, game_id):
            return None
        seed = game.game_seed
        ruleset_id = game.ruleset_id
        if game.status != STATUS_RUNNING:
            game.status = STATUS_RUNNING
            await session.flush()
    return seed, ruleset_id


async def _run_one_human_game(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    semaphore: asyncio.Semaphore,
    adapter_factory: AdapterFactory | None,
    ai_adapter_factory: AiAdapterFactory | None,
    game_executor: HumanGameExecutor,
    settings: Settings,
    build_production_adapter: bool,
) -> None:
    async with semaphore:
        claimed = await _claim_game(session_factory, game_id)
        if claimed is None:
            return
        game_seed, ruleset_id = claimed

        if adapter_factory is not None:
            adapter = adapter_factory()
        elif build_production_adapter:
            adapter = await build_human_lane_adapter(
                session_factory,
                game_id=game_id,
                settings=settings,
                ai_adapter_factory=ai_adapter_factory,
            )
        else:
            adapter = _InjectedExecutorAdapter()
        config = GameConfig(game_id=str(game_id), game_seed=game_seed, ruleset_id=ruleset_id)
        # Human seats carry no agent_build_id, so ``agent_builds`` is empty: the
        # rating write path fails closed (segregation) and no scientific row is
        # written for a human-lane game.
        persistence = GamePersistence(
            session_factory=session_factory,
            game_id=game_id,
            agent_builds={},
            league_id=None,
        )
        structlog.contextvars.bind_contextvars(human_lane_game_id=str(game_id))
        try:
            await game_executor(config, persistence, adapter)
        finally:
            structlog.contextvars.unbind_contextvars("human_lane_game_id")


async def _new_turns_halted(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> bool:
    """Return True when the global cost breaker forbids issuing NEW LLM turns.

    A game that has not yet been claimed has issued ZERO LLM turns, so skipping
    its dispatch while the breaker is open is exactly the AC2 contract: STOP new
    lobbies / new LLM turns. Games already RUNNING keep their in-flight task and
    finish to completion — the breaker NEVER kills an active game or boots a
    human (the rejected "AI-only continuation" anti-pattern).
    """
    async with session_factory() as session:
        return await global_breaker_open(session, settings)


async def run_human_lane(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    concurrency: int,
    stop_event: asyncio.Event,
    adapter_factory: AdapterFactory | None = None,
    ai_adapter_factory: AiAdapterFactory | None = None,
    game_executor: HumanGameExecutor | None = None,
    semaphore: asyncio.Semaphore | None = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    settings: Settings | None = None,
) -> None:
    """Drain human-lane games until ``stop_event`` is set.

    Each tick lists claimable human-lane games and dispatches them through a
    dedicated :class:`asyncio.Semaphore` (defaults to ``Semaphore(concurrency)``)
    so no more than ``concurrency`` human games run at once. This lane shares no
    semaphore or claim path with the benchmark scheduler, so a backlog of human
    lobbies cannot reduce benchmark concurrency.

    When the global cost breaker is open (cumulative human-lane spend at the
    configured threshold) the lane STOPS dispatching NEW games — a not-yet-started
    game has issued no LLM turns, so this halts new turns — while every game whose
    task is already in flight runs to completion. The breaker throttles, it never
    kills an active game.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")
    if poll_interval_s <= 0:
        raise ValueError("poll_interval_s must be > 0")

    sem = semaphore or asyncio.Semaphore(concurrency)
    cfg = settings or get_settings()
    use_default_executor = game_executor is None
    executor = game_executor or _default_human_game_executor(cfg)

    tasks: dict[uuid.UUID, asyncio.Task[None]] = {}

    try:
        while not stop_event.is_set():
            async with session_factory() as session:
                candidates = await list_human_lane_games(session)

            # Throttle-not-kill: while the breaker is open, do not START any new
            # game (no new LLM turns). Already-dispatched tasks keep running.
            if await _new_turns_halted(session_factory, cfg):
                _logger.warning("human_lane.breaker.halt_new_turns", in_flight=len(tasks))
                pending = []
            else:
                pending = [gid for gid in candidates if gid not in tasks]
            for game_id in pending:

                def _make_done_cb(gid: uuid.UUID) -> Callable[[asyncio.Task[None]], None]:
                    def _done(_task: asyncio.Task[None]) -> None:
                        tasks.pop(gid, None)

                    return _done

                task = asyncio.create_task(
                    _run_one_human_game(
                        session_factory,
                        game_id=game_id,
                        semaphore=sem,
                        adapter_factory=adapter_factory,
                        ai_adapter_factory=ai_adapter_factory,
                        game_executor=executor,
                        settings=cfg,
                        build_production_adapter=use_default_executor,
                    ),
                    name=f"human-lane-game-{game_id}",
                )
                tasks[game_id] = task
                task.add_done_callback(_make_done_cb(game_id))

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_s)
    finally:
        # Drain in-flight games so no orphan task survives the loop (which would
        # leak a DB session and trip "Task was destroyed but it is pending").
        # Setting ``stop_event`` lets a running game finish — mid-game state is
        # never abandoned; the snapshot/event log makes it rehydratable anyway.
        outstanding = list(tasks.values())
        if outstanding:
            await asyncio.gather(*outstanding, return_exceptions=True)


__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "AdapterFactory",
    "AiAdapterFactory",
    "HumanGameExecutor",
    "HumanLaneAdmission",
    "build_human_lane_adapter",
    "human_lane_admission",
    "list_human_lane_games",
    "run_human_lane",
]
