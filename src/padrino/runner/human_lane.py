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
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.agents.contract import AgentResponse
from padrino.core.disconnect import SeatPresence, seats_past_grace
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog, StoredEvent
from padrino.core.engine.state import GameState, Seat
from padrino.core.enums import ActionType, SeatKind
from padrino.core.observations import Observation, Ruleset, format_phase_id
from padrino.db.models import Game, GameSeat, HumanActionSubmission, HumanChatSubmission
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import games as games_repo
from padrino.db.repositories import human_action_submissions as human_actions_repo
from padrino.db.repositories import human_game_runtime as runtime_repo
from padrino.db.repositories import human_seat_presence as presence_repo
from padrino.economics.human_cost_governance import global_breaker_open, human_eligible_pool
from padrino.gauntlets.heterogeneous import build_heterogeneous_adapter
from padrino.gauntlets.tournament import project_agent_build
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.llm.adapter import AgentBuild as LlmAgentBuild
from padrino.llm.human_adapter import HumanAdapter, PullAction
from padrino.llm.multiplex import SeatMultiplexAdapter
from padrino.runner.disconnect_takeover import apply_takeover, build_takeover_event
from padrino.runner.game_runner import GameConfig, GamePersistence, GameResume, drive_game_loop
from padrino.runner.human_chat_observation import HumanChatHydratingAdapter
from padrino.runner.human_chat_release import release_held_chat_for_phase
from padrino.runner.human_durability import RehydratedHumanGame, rehydrate_active_human_games
from padrino.runner.human_state_cache import build_state_cache
from padrino.runner.human_tick import Clock, HumanTickConfig, Sleep, run_human_tick
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
HumanChatRelease = Callable[[str, float, EventLog, Sequence[StoredEvent]], Awaitable[None]]
BeforeTakeoverRevalidate = Callable[[], Awaitable[None]]


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


@dataclass(frozen=True, slots=True)
class AppliedTakeover:
    """One disconnect-grace takeover applied by the human worker lane."""

    seat_id: str
    replacement_agent_build_id: uuid.UUID
    event: StoredEvent


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


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _presence_from_row(
    *,
    public_player_id: str,
    row: object | None,
    now: datetime,
    grace_seconds: float,
) -> SeatPresence:
    connected = bool(getattr(row, "connected", False)) if row is not None else False
    last_seen_at = _as_aware(getattr(row, "last_seen_at", None)) if row is not None else None
    disconnected_at = _as_aware(getattr(row, "disconnected_at", None)) if row is not None else None
    if row is None:
        return SeatPresence(public_player_id=public_player_id, connected=False, disconnected_at=now)
    if connected:
        if last_seen_at is None:
            return SeatPresence(public_player_id=public_player_id, connected=True)
        cutoff = now - timedelta(seconds=grace_seconds)
        if last_seen_at >= cutoff:
            return SeatPresence(public_player_id=public_player_id, connected=True)
        return SeatPresence(
            public_player_id=public_player_id,
            connected=False,
            disconnected_at=last_seen_at,
        )
    return SeatPresence(
        public_player_id=public_player_id,
        connected=False,
        disconnected_at=disconnected_at,
    )


async def _human_presence_snapshot(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    now: datetime,
    grace_seconds: float,
) -> list[SeatPresence]:
    seat_rows = list(
        (
            await session.execute(
                select(GameSeat)
                .where(GameSeat.game_id == game_id)
                .where(GameSeat.seat_kind == SeatKind.HUMAN.value)
                .order_by(GameSeat.seat_index)
            )
        ).scalars()
    )
    presence_rows = {
        row.public_player_id: row
        for row in await presence_repo.list_for_game(session, game_id=game_id)
    }
    return [
        _presence_from_row(
            public_player_id=seat.public_player_id,
            row=presence_rows.get(seat.public_player_id),
            now=now,
            grace_seconds=grace_seconds,
        )
        for seat in seat_rows
    ]


async def _expired_human_seat_for_update(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    seat_id: str,
    now: datetime,
    grace_seconds: float,
) -> GameSeat | None:
    stmt = (
        select(GameSeat)
        .where(GameSeat.game_id == game_id)
        .where(GameSeat.public_player_id == seat_id)
        .with_for_update()
    )
    seat = (await session.execute(stmt)).scalar_one_or_none()
    if seat is None or seat.seat_kind != SeatKind.HUMAN.value:
        return None
    # US-200: lock the presence row FOR UPDATE so a reconnect heartbeat that
    # commits inside this revalidation transaction's read->commit window is
    # serialized against the takeover. Without the lock, under READ COMMITTED a
    # racing heartbeat is invisible to a plain SELECT and the worker wrongly
    # takes over a LIVE human seat (docstring guarantee at the takeover loop).
    # seats_past_grace is re-evaluated below AFTER the lock is held.
    row = await presence_repo.get(
        session, game_id=game_id, public_player_id=seat_id, for_update=True
    )
    presence = _presence_from_row(
        public_player_id=seat_id,
        row=row,
        now=now,
        grace_seconds=grace_seconds,
    )
    if seats_past_grace([presence], now=now, grace_seconds=grace_seconds) != [seat_id]:
        return None
    return seat


async def _replacement_build_id(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    ruleset_id: str,
    seat: GameSeat,
) -> uuid.UUID | None:
    if seat.takeover_agent_build_id is not None:
        return seat.takeover_agent_build_id

    roster = [uuid.UUID(raw) for raw in await human_eligible_pool(session, ruleset_id)]
    if not roster:
        return None

    current_builds = list(
        (
            await session.execute(
                select(GameSeat.agent_build_id, GameSeat.takeover_agent_build_id).where(
                    GameSeat.game_id == game_id
                )
            )
        ).all()
    )
    used = {bid for pair in current_builds for bid in pair if bid is not None}
    return next((bid for bid in roster if bid not in used), roster[0])


async def _takeover_replacement_adapter(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    settings: Settings,
    seat_id: str,
    build_id: uuid.UUID,
    ai_adapter_factory: AiAdapterFactory | None,
) -> LlmAdapter:
    async with session_factory() as session:
        build = await project_agent_build(session, build_id)
    inner = (
        ai_adapter_factory({seat_id: build})
        if ai_adapter_factory is not None
        else build_heterogeneous_adapter({seat_id: build}, settings=settings)
    )
    return HumanChatHydratingAdapter(
        inner=inner,
        session_factory=session_factory,
        game_id=game_id,
    )


async def _persist_stored_event_row(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    stored: StoredEvent,
) -> None:
    """Append one in-memory event envelope to ``game_events`` in ``session``.

    Used to co-commit a SeatTakenOver / chat content_ref event row with its
    paired DB mutation so the persisted hash chain never lags that state across
    a crash (hard rule 4). The outer loop's ``persist_pending_events`` is
    idempotent against an already-committed sequence, so this row is not
    re-inserted later.
    """
    body = stored.body
    await events_repo.append_event(
        session,
        game_id=game_id,
        sequence=stored.sequence,
        event_type=str(body["event_type"]),
        phase=str(body["phase"]),
        visibility=str(body["visibility"]),
        actor_player_id=body.get("actor_player_id"),
        payload=dict(body.get("payload", {})),
        prev_event_hash=stored.prev_event_hash,
        event_hash=stored.event_hash,
    )


def _seat_has_in_memory_takeover(event_log: EventLog, seat_id: str) -> bool:
    """Return whether ``event_log`` already holds a SeatTakenOver for ``seat_id``.

    The takeover recheck (US-197 AC2) guards against appending a SECOND
    ``SeatTakenOver`` for a seat the in-memory log already took over. The DB-only
    recheck (``_expired_human_seat_for_update``) cannot catch the case where a
    prior commit FAILED mid-takeover: the DB seat may still read HUMAN/expired
    while the in-memory mux already routes the seat to the AI adapter (the swap
    is not reverted on commit failure — AC1). Scanning the in-memory log makes
    the retry idempotent regardless of the DB seat row.
    """
    return any(
        event.body.get("event_type") == "SeatTakenOver"
        and event.body.get("payload", {}).get("public_player_id") == seat_id
        for event in event_log.events
    )


async def _take_over_expired_human_seats(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    mux: SeatMultiplexAdapter,
    event_log: EventLog,
    state: GameState,
    settings: Settings,
    ai_adapter_factory: AiAdapterFactory | None = None,
    now: datetime | None = None,
    before_revalidate: BeforeTakeoverRevalidate | None = None,
) -> list[AppliedTakeover]:
    """Apply disconnect-grace takeovers before the phase tick dispatches.

    The initial scan is advisory. Each candidate is re-read inside a transaction
    immediately before the adapter swap and DB seat update, so a reconnecting
    heartbeat that races the worker cancels the takeover.
    """
    checked_at = now or datetime.now(UTC)
    grace = settings.padrino_human_reconnect_grace_seconds
    async with session_factory() as session:
        presences = await _human_presence_snapshot(
            session,
            game_id=game_id,
            now=checked_at,
            grace_seconds=grace,
        )
        expired = seats_past_grace(presences, now=checked_at, grace_seconds=grace)

    applied: list[AppliedTakeover] = []
    for seat_id in expired:
        if before_revalidate is not None:
            await before_revalidate()

        async with session_factory() as session, session.begin():
            game = await games_repo.get(session, game_id)
            if game is None or game.status == STATUS_COMPLETED:
                continue
            seat = await _expired_human_seat_for_update(
                session,
                game_id=game_id,
                seat_id=seat_id,
                now=checked_at,
                grace_seconds=grace,
            )
            if seat is None:
                continue
            build_id = await _replacement_build_id(
                session,
                game_id=game_id,
                ruleset_id=game.ruleset_id,
                seat=seat,
            )
            if build_id is None:
                _logger.warning(
                    "human_lane.takeover.no_replacement",
                    game_id=str(game_id),
                    seat_id=seat_id,
                )
                continue

        replacement = await _takeover_replacement_adapter(
            session_factory,
            game_id=game_id,
            settings=settings,
            seat_id=seat_id,
            build_id=build_id,
            ai_adapter_factory=ai_adapter_factory,
        )

        # In-memory recheck (US-197 AC2): if a prior takeover commit FAILED but
        # left the in-memory swap/log in place, the DB seat may still read
        # HUMAN/expired. Skip rather than append a SECOND SeatTakenOver.
        if _seat_has_in_memory_takeover(event_log, seat_id):
            continue

        async with session_factory() as session, session.begin():
            seat = await _expired_human_seat_for_update(
                session,
                game_id=game_id,
                seat_id=seat_id,
                now=checked_at,
                grace_seconds=grace,
            )
            if seat is None:
                continue
            # US-197 AC1: build the SeatTakenOver envelope WITHOUT touching the
            # long-lived in-memory mux / event_log, persist the paired row + seat
            # mutation, and let this block's session.begin() commit. The
            # in-memory swap + log append happen ONLY AFTER the commit succeeds
            # (below), so a flush/commit failure here rolls back the DB and never
            # advances the in-memory log/mux past what is durable in game_events.
            event = build_takeover_event(
                event_log=event_log,
                state=state,
                seat_id=seat_id,
                replacement_agent_build_ref=str(build_id),
            )
            phase = str(event.body["phase"])
            seat.seat_kind = SeatKind.AI_TAKEOVER.value
            seat.taken_over_at_phase = phase
            seat.takeover_agent_build_id = build_id
            # Persist the paired SeatTakenOver event row in the SAME transaction
            # as the seat mutation (hard rule 4): a crash can never leave an
            # AI_TAKEOVER seat without its provenance event in game_events.
            await _persist_stored_event_row(session, game_id=game_id, stored=event)

        # The DB write committed; only now apply the irreversible in-memory swap
        # + log append (US-197 AC1). If the commit above raised, control never
        # reaches here and the in-memory state is untouched.
        result = apply_takeover(
            mux=mux,
            event_log=event_log,
            event=event,
            seat_id=seat_id,
            replacement_adapter=replacement,
        )
        applied.append(
            AppliedTakeover(
                seat_id=seat_id,
                replacement_agent_build_id=build_id,
                event=result.event,
            )
        )

    return applied


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
        ai_adapter = HumanChatHydratingAdapter(
            inner=ai_adapter,
            session_factory=session_factory,
            game_id=game_id,
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
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
    release_chat: HumanChatRelease | None = None,
) -> dict[str, AgentResponse]:
    log_before = len(event_log.events)
    result = await run_human_tick(
        state,
        event_log,
        eligible_seats,
        adapter,
        ruleset,
        config,
        ranked=ranked,
        clock=clock,
        sleep=sleep,
    )
    if release_chat is not None:
        # run_human_tick appends failure events (ActionTimedOut / OutputInvalid)
        # straight to the in-memory event_log, persisted nowhere yet. Hand them to
        # release_chat so it co-commits those LOWER sequences in the SAME
        # transaction as the content_ref chat row it appends above them — closing
        # the crash window where game_events would hold {N-1, N+1} with N only in
        # memory until the post-tick persist_pending_events ran (US-196).
        pending_lower_events = event_log.events[log_before:]
        await release_chat(
            format_phase_id(state.current_phase),
            result.settled_at,
            event_log,
            pending_lower_events,
        )
    return result.responses


def _json_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


async def _runtime_buffer_snapshot(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    phase: str,
) -> dict[str, Any]:
    action_rows = list(
        (
            await session.execute(
                select(HumanActionSubmission)
                .where(HumanActionSubmission.game_id == game_id)
                .where(HumanActionSubmission.phase == phase)
                .order_by(
                    HumanActionSubmission.public_player_id,
                    HumanActionSubmission.created_at.desc(),
                    HumanActionSubmission.id.desc(),
                )
            )
        ).scalars()
    )
    actions: dict[str, dict[str, object]] = {}
    for row in action_rows:
        if row.public_player_id in actions:
            continue
        actions[row.public_player_id] = {
            "action_type": row.action_type,
            "target": row.target,
            "idempotency_key": row.idempotency_key,
            "created_at": _json_timestamp(row.created_at),
        }

    chat_rows = list(
        (
            await session.execute(
                select(HumanChatSubmission)
                .where(HumanChatSubmission.game_id == game_id)
                .where(HumanChatSubmission.phase == phase)
                .where(HumanChatSubmission.status == "HELD")
                .order_by(
                    HumanChatSubmission.public_player_id,
                    HumanChatSubmission.created_at,
                    HumanChatSubmission.id,
                )
            )
        ).scalars()
    )
    chat_holds = [
        {
            "public_player_id": row.public_player_id,
            "channel": row.channel,
            "status": row.status,
            "idempotency_key": row.idempotency_key,
            "created_at": _json_timestamp(row.created_at),
            "ready_for_release": row.cleaned_text is not None,
        }
        for row in chat_rows
    ]
    return {"actions": actions, "chat_holds": chat_holds}


async def persist_human_runtime_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    phase: str,
    deadline_at: datetime | None,
    updated_at: datetime,
    state: GameState | None = None,
    event_log: EventLog,
) -> None:
    """Persist the current human-lane runtime scaffold for one phase.

    The snapshot contains only transport/runtime metadata. Raw or cleaned chat
    text stays in the chat hold / sidecar tables and is not duplicated here.
    """
    async with session_factory() as session, session.begin():
        buffer_snapshot = await _runtime_buffer_snapshot(session, game_id=game_id, phase=phase)
        await runtime_repo.upsert(
            session,
            game_id=game_id,
            phase=phase,
            deadline_at=deadline_at,
            buffer_snapshot=buffer_snapshot,
            state_cache=build_state_cache(state, event_log) if state is not None else None,
            updated_at=updated_at,
        )
        if event_log.events:
            game = await games_repo.get(session, game_id)
            if game is not None and game.status != STATUS_COMPLETED:
                game.current_phase = phase
                game.event_hash_head = event_log.head_hash


class _RuntimeSnapshotter:
    """Phase hook that writes human runtime snapshots from the game loop."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        game_id: uuid.UUID,
        phase_deadline_seconds: float,
        resume: GameResume | None,
    ) -> None:
        self._session_factory = session_factory
        self._game_id = game_id
        self._phase_deadline_seconds = phase_deadline_seconds
        self._deadlines: dict[str, datetime] = {}
        if resume is not None and resume.deadline_at is not None:
            self._deadlines[resume.phase] = resume.deadline_at

    async def __call__(self, _state: GameState, event_log: EventLog, phase: str) -> None:
        updated_at = datetime.now(UTC)
        deadline_at = self._deadlines.get(phase)
        if deadline_at is None:
            deadline_at = updated_at + timedelta(seconds=self._phase_deadline_seconds)
            self._deadlines[phase] = deadline_at
        await persist_human_runtime_snapshot(
            self._session_factory,
            game_id=self._game_id,
            phase=phase,
            deadline_at=deadline_at,
            updated_at=updated_at,
            state=_state,
            event_log=event_log,
        )


def _game_resume_from_rehydrated(rehydrated: RehydratedHumanGame) -> GameResume:
    return GameResume(
        state=rehydrated.state,
        event_log=rehydrated.event_log,
        phase=rehydrated.phase,
        deadline_at=rehydrated.deadline_at,
        buffer_snapshot=rehydrated.buffer_snapshot,
    )


def _default_human_game_executor(
    settings: Settings,
    *,
    ai_adapter_factory: AiAdapterFactory | None = None,
) -> HumanGameExecutor:
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
        async def release_chat(
            phase: str,
            _settled_at: float,
            release_log: EventLog,
            pending_lower_events: Sequence[StoredEvent],
        ) -> None:
            async with persistence.session_factory() as session, session.begin():
                await release_held_chat_for_phase(
                    session,
                    game_id=persistence.game_id,
                    phase=phase,
                    released_at=datetime.now(UTC),
                    event_log=release_log,
                    pending_lower_events=pending_lower_events,
                )

        async def tick_runner(
            state: GameState,
            event_log: EventLog,
            eligible_seats: Sequence[Seat],
            tick_adapter: LlmAdapter,
            ruleset: Ruleset,
            ranked: bool,
            timeout_s: float,
        ) -> dict[str, AgentResponse]:
            if isinstance(adapter, SeatMultiplexAdapter):
                await _take_over_expired_human_seats(
                    persistence.session_factory,
                    game_id=persistence.game_id,
                    mux=adapter,
                    event_log=event_log,
                    state=state,
                    settings=settings,
                    ai_adapter_factory=ai_adapter_factory,
                )
            return await _run_human_tick_responses(
                state,
                event_log,
                eligible_seats,
                tick_adapter,
                ruleset,
                ranked,
                timeout_s,
                config=tick_config,
                release_chat=release_chat,
            )

        snapshotter = _RuntimeSnapshotter(
            persistence.session_factory,
            game_id=persistence.game_id,
            phase_deadline_seconds=settings.padrino_human_phase_deadline_seconds,
            resume=persistence.resume,
        )
        await drive_game_loop(
            config,
            adapter,
            False,
            persistence=persistence,
            tick_runner=tick_runner,
            resume=persistence.resume,
            phase_snapshot=snapshotter,
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
) -> tuple[str, str, str] | None:
    """Atomically flip a claimable human-lane game to RUNNING.

    Returns ``(game_seed, ruleset_id, prior_status)`` on a successful claim, or
    ``None`` when the game vanished, already completed, or is not a human-lane
    game (so a concurrent worker / the benchmark lane never double-runs it).
    """
    async with session_factory() as session, session.begin():
        game = await games_repo.get(session, game_id)
        if game is None or game.status == STATUS_COMPLETED:
            return None
        if not await _is_human_lane_game(session, game_id):
            return None
        seed = game.game_seed
        ruleset_id = game.ruleset_id
        prior_status = game.status
        if game.status != STATUS_RUNNING:
            game.status = STATUS_RUNNING
            await session.flush()
    return seed, ruleset_id, prior_status


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
    resume: GameResume | None,
) -> None:
    async with semaphore:
        claimed = await _claim_game(session_factory, game_id)
        if claimed is None:
            return
        game_seed, ruleset_id, prior_status = claimed
        if prior_status == STATUS_RUNNING and resume is None:
            _logger.warning(
                "human_lane.running_game_missing_runtime_snapshot",
                game_id=str(game_id),
            )
            return

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
            resume=resume,
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
    executor = game_executor or _default_human_game_executor(
        cfg,
        ai_adapter_factory=ai_adapter_factory,
    )

    tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
    rehydrated_by_game = {
        item.game_id: _game_resume_from_rehydrated(item)
        for item in await rehydrate_active_human_games(session_factory)
    }

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

                resume = rehydrated_by_game.pop(game_id, None)
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
                        resume=resume,
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
