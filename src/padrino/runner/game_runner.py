"""GameRunner orchestration loop.

`run_game(config, adapter, ranked)` drives one mini-7 game from
``GameCreated`` to ``GameTerminated``. Per phase the loop:

1. emits a ``PhaseStarted`` event,
2. computes the eligible seats from :func:`legal_actions_for`,
3. ticks every eligible seat through the adapter (see :func:`run_tick`),
4. records the per-seat submission events (chat + structured action),
5. dispatches the appropriate phase resolver (day vote or night),
6. emits resolution events (``DayVoteResolved``, ``NightResolved``,
   ``DetectiveResultDelivered``, ``PlayerEliminated``) and a
   ``PhaseResolved`` marker,
7. checks the win condition; if a winner / draw is decided, appends
   ``GameTerminated`` and stops.

If the phase FSM reaches ``TERMINAL`` without a winner the game ends with a
``DRAW`` (``MAX_DAYS_REACHED``).

Every event flows through :func:`apply_event`, so the in-memory
:class:`GameState` stays consistent with the hash-chained event log.

Impure runner module; pure-core code does not import it.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.agents.contract import AgentResponse, ResponseError
from padrino.core.engine.event_log import EventLog, StoredEvent
from padrino.core.engine.events import EventAdapter
from padrino.core.engine.legal_actions import legal_actions_for
from padrino.core.engine.phases import next_phase
from padrino.core.engine.reducer import apply_event, initial_state
from padrino.core.engine.resolvers.day_vote import resolve_day_vote
from padrino.core.engine.resolvers.night import resolve_night
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.engine.state import GameState, Seat
from padrino.core.engine.win_conditions import REASON_MAX_DAYS_REACHED, check_win
from padrino.core.enums import ActionType, Faction, PhaseKind, Role
from padrino.core.observations import Observation, format_phase_id
from padrino.core.rulesets import mini7_v1
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import games as games_repo
from padrino.db.repositories import llm_calls as llm_calls_repo
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.observability.events import (
    EVENT_GAME_COMPLETED,
    EVENT_GAME_STARTED,
    EVENT_PHASE_RESOLVED,
    EVENT_PHASE_STARTED,
    EVENT_RATING_UPDATED,
)
from padrino.ratings.openskill_service import GameResult, update_ratings_for_game
from padrino.runner.tick import run_tick

_logger = structlog.get_logger("padrino.runner")

MAFIA_CHANNEL_ID: Final[str] = "mafia"
CAUSE_DAY_VOTE: Final[str] = "day_vote"
CAUSE_NIGHT_KILL: Final[str] = "night_kill"
STATUS_COMPLETED: Final[str] = "COMPLETED"

_RULESETS: Final[dict[str, Any]] = {mini7_v1.RULESET_ID: mini7_v1}


@dataclass(frozen=True, slots=True)
class GamePersistence:
    """Optional persistence target for :func:`run_game`.

    When passed, every event flowing through ``_emit`` is mirrored to the
    ``game_events`` table and every adapter call is mirrored to the
    ``llm_calls`` table. ``agent_builds`` maps ``public_player_id`` → build
    UUID for llm_call attribution; unmapped seats persist with
    ``agent_build_id = NULL``.

    On the ``RolesAssigned`` event the runner additionally writes one
    ``game_seats`` row per seat sourced from the event payload; this requires
    ``agent_builds`` to cover every seat in the assignment.

    On the ``GameTerminated`` event the runner sets ``Game.status='COMPLETED'``
    and ``Game.terminal_result={winner, reason, day_terminated}`` in the same
    transaction as the event row and (when applicable) the rating updates so
    partial failures roll back together.
    """

    session_factory: async_sessionmaker[AsyncSession]
    game_id: uuid.UUID
    agent_builds: Mapping[str, uuid.UUID] = field(default_factory=dict)
    league_id: uuid.UUID | None = None


class GameConfig(BaseModel):
    """Input config for a single game run."""

    model_config = ConfigDict(frozen=True)

    game_id: str
    game_seed: str
    ruleset_id: str = mini7_v1.RULESET_ID
    timeout_s: float = float(mini7_v1.LLM_TIMEOUT_SECONDS)


@dataclass(frozen=True, slots=True)
class GameOutcome:
    """Outputs returned to the caller after the game terminates."""

    final_state: GameState
    event_log: EventLog
    llm_calls: tuple[AdapterResult, ...]


class _RecordingAdapter:
    """Wraps an :class:`LlmAdapter` so every :class:`AdapterResult` is captured.

    When ``persistence`` is supplied, each call is also mirrored to the
    ``llm_calls`` table inside its own transaction. Failures still produce a
    persisted row so audit logs remain complete.
    """

    __slots__ = ("_inner", "_persistence", "_sink")

    def __init__(
        self,
        inner: LlmAdapter,
        sink: list[AdapterResult],
        persistence: GamePersistence | None,
    ) -> None:
        self._inner = inner
        self._sink = sink
        self._persistence = persistence

    async def complete(self, observation: Observation) -> AdapterResult:
        result = await self._inner.complete(observation)
        self._sink.append(result)
        if self._persistence is not None:
            await _persist_llm_call(self._persistence, observation, result)
        return result


def _emit(
    body: dict[str, Any],
    state: GameState,
    event_log: EventLog,
) -> GameState:
    """Append ``body`` to the chain and fold it through the reducer.

    Returns the next :class:`GameState`. Sequence is auto-assigned from the
    current log length so callers do not need to track it.
    """
    sealed = dict(body)
    sealed["sequence"] = len(event_log.events)
    event_log.append(sealed)
    event = EventAdapter.validate_python(sealed)
    return apply_event(state, event)


async def _append_event_row(
    session: AsyncSession,
    persistence: GamePersistence,
    stored: StoredEvent,
) -> None:
    body = stored.body
    await events_repo.append_event(
        session,
        game_id=persistence.game_id,
        sequence=stored.sequence,
        event_type=body["event_type"],
        phase=body["phase"],
        visibility=body["visibility"],
        actor_player_id=body.get("actor_player_id"),
        payload=dict(body.get("payload", {})),
        prev_event_hash=stored.prev_event_hash,
        event_hash=stored.event_hash,
    )


async def _persist_stored_event(
    persistence: GamePersistence,
    stored: StoredEvent,
) -> None:
    async with persistence.session_factory() as session, session.begin():
        await _append_event_row(session, persistence, stored)


async def _persist_roles_assigned(
    persistence: GamePersistence,
    stored: StoredEvent,
) -> None:
    """Persist the ``RolesAssigned`` event row and one ``game_seats`` row per seat.

    Both writes share one ``session.begin()`` so the event row and the seat
    rows are committed atomically. Skips the seat backfill when
    ``persistence.agent_builds`` does not cover every seat in the payload —
    callers that drive ``run_game`` without per-seat builds (legacy tests
    using ``persistence=None``-style flows) still get the event row.
    """
    assignments = stored.body.get("payload", {}).get("assignments", [])
    seat_specs: list[dict[str, Any]] = []
    for entry in assignments:
        public_id = entry["public_player_id"]
        ab_id = persistence.agent_builds.get(public_id)
        if ab_id is None:
            seat_specs = []
            break
        seat_specs.append(
            {
                "public_player_id": public_id,
                "seat_index": entry["seat_index"],
                "agent_build_id": ab_id,
                "role": entry["role"],
                "faction": entry["faction"],
            }
        )

    async with persistence.session_factory() as session, session.begin():
        await _append_event_row(session, persistence, stored)
        for spec in seat_specs:
            await games_repo.add_seat(
                session,
                game_id=persistence.game_id,
                public_player_id=spec["public_player_id"],
                seat_index=spec["seat_index"],
                agent_build_id=spec["agent_build_id"],
                role=spec["role"],
                faction=spec["faction"],
                alive=True,
            )


def _should_apply_ratings(
    persistence: GamePersistence,
    ranked: bool,
    state: GameState,
) -> bool:
    if not ranked or persistence.league_id is None or not persistence.agent_builds:
        return False
    if state.terminal_result not in ("TOWN", "MAFIA", "DRAW"):
        return False
    return all(s.public_player_id in persistence.agent_builds for s in state.seats)


async def _persist_terminated_event(
    persistence: GamePersistence,
    stored: StoredEvent,
    state: GameState,
    ranked: bool,
    day_terminated: int,
) -> None:
    """Persist the ``GameTerminated`` row, game-row finalize, and ratings atomically.

    Inside one ``session.begin()`` we write the terminal event row, flip
    ``Game.status='COMPLETED'`` with ``Game.terminal_result`` = ``{winner,
    reason, day_terminated}``, and (when applicable) every rating update +
    audit row so partial failures roll back together.
    """
    apply_ratings = _should_apply_ratings(persistence, ranked, state)
    terminal_result_payload: dict[str, Any] = {
        "winner": state.terminal_result,
        "reason": state.terminal_reason,
        "day_terminated": day_terminated,
    }
    async with persistence.session_factory() as session, session.begin():
        await _append_event_row(session, persistence, stored)
        await games_repo.update_status(
            session,
            persistence.game_id,
            status=STATUS_COMPLETED,
            terminal_result=terminal_result_payload,
            current_phase=stored.body["phase"],
            event_hash_head=stored.event_hash,
        )
        if apply_ratings:
            assert persistence.league_id is not None
            winner = cast(Literal["TOWN", "MAFIA", "DRAW"], state.terminal_result)
            seat_factions: dict[str, Faction] = {s.public_player_id: s.faction for s in state.seats}
            agent_builds_by_seat: dict[str, uuid.UUID] = {
                sid: persistence.agent_builds[sid] for sid in seat_factions
            }
            await update_ratings_for_game(
                session,
                league_id=persistence.league_id,
                game_result=GameResult(
                    game_id=persistence.game_id,
                    winner=winner,
                    seat_factions=seat_factions,
                ),
                agent_builds_by_seat=agent_builds_by_seat,
            )
            _logger.info(
                EVENT_RATING_UPDATED,
                league_id=str(persistence.league_id),
                winner=winner,
                seats=len(agent_builds_by_seat),
            )


def _request_prompt_hash(observation: Observation) -> str:
    raw = observation.model_dump_json().encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _parsed_response_to_json(result: AdapterResult) -> dict[str, Any] | None:
    parsed = result.parsed_response
    if isinstance(parsed, AgentResponse):
        return parsed.model_dump(mode="json")
    if isinstance(parsed, ResponseError):
        return parsed.model_dump(mode="json")
    return None


async def _persist_llm_call(
    persistence: GamePersistence,
    observation: Observation,
    result: AdapterResult,
) -> None:
    request_json = observation.model_dump(mode="json")
    failure = result.failure
    async with persistence.session_factory() as session, session.begin():
        await llm_calls_repo.record_call(
            session,
            game_id=persistence.game_id,
            agent_build_id=persistence.agent_builds.get(observation.you.player_id),
            public_player_id=observation.you.player_id,
            phase=observation.phase,
            request_json=request_json,
            request_prompt_hash=_request_prompt_hash(observation),
            status=result.status,
            raw_response=result.raw_response,
            parsed_response=_parsed_response_to_json(result),
            error=result.error,
            error_kind=failure.error_kind if failure is not None else None,
            error_message=failure.error_message if failure is not None else None,
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            provider_response_id=result.provider_response_id,
        )


def _eligible_seats(state: GameState) -> list[Seat]:
    return [
        seat for seat in state.living_seats() if legal_actions_for(state, seat).allowed_action_types
    ]


def _submission_events_for(
    seat: Seat,
    response: AgentResponse,
    phase_kind: PhaseKind,
    phase_id: str,
    discussion_round: int,
) -> list[dict[str, Any]]:
    """Translate one seat's :class:`AgentResponse` into the events it implies.

    Phase-gated:

    * Public messages only emit in DAY_DISCUSSION / DAY_VOTE.
    * Private messages only emit for mafia seats in the mafia-channel phases.
    * Structured actions only emit when the action type matches the phase.
    """
    events: list[dict[str, Any]] = []
    seat_id = seat.public_player_id

    if response.public_message and phase_kind in (PhaseKind.DAY_DISCUSSION, PhaseKind.DAY_VOTE):
        round_index = discussion_round if phase_kind is PhaseKind.DAY_DISCUSSION else None
        events.append(
            {
                "event_type": "PublicMessageSubmitted",
                "phase": phase_id,
                "visibility": "PUBLIC",
                "actor_player_id": seat_id,
                "payload": {"text": response.public_message, "round_index": round_index},
            }
        )

    if (
        response.private_message
        and seat.faction is Faction.MAFIA
        and phase_kind in (PhaseKind.NIGHT_0_MAFIA_INTRO, PhaseKind.NIGHT_MAFIA_DISCUSSION)
    ):
        events.append(
            {
                "event_type": "PrivateMessageSubmitted",
                "phase": phase_id,
                "visibility": "PRIVATE",
                "actor_player_id": seat_id,
                "payload": {
                    "text": response.private_message,
                    "channel_id": MAFIA_CHANNEL_ID,
                },
            }
        )

    action = response.action
    if phase_kind is PhaseKind.DAY_VOTE:
        if action.type is ActionType.VOTE:
            events.append(
                {
                    "event_type": "VoteSubmitted",
                    "phase": phase_id,
                    "visibility": "PUBLIC",
                    "actor_player_id": seat_id,
                    "payload": {"target": action.target, "is_abstain": False},
                }
            )
        elif action.type is ActionType.ABSTAIN:
            events.append(
                {
                    "event_type": "VoteSubmitted",
                    "phase": phase_id,
                    "visibility": "PUBLIC",
                    "actor_player_id": seat_id,
                    "payload": {"target": None, "is_abstain": True},
                }
            )
    elif phase_kind is PhaseKind.NIGHT_ACTIONS:
        if action.type is ActionType.MAFIA_KILL:
            events.append(
                {
                    "event_type": "MafiaKillVoteSubmitted",
                    "phase": phase_id,
                    "visibility": "PRIVATE",
                    "actor_player_id": seat_id,
                    "payload": {"target": action.target},
                }
            )
        elif action.type is ActionType.PROTECT:
            events.append(
                {
                    "event_type": "ProtectSubmitted",
                    "phase": phase_id,
                    "visibility": "PRIVATE",
                    "actor_player_id": seat_id,
                    "payload": {"target": action.target},
                }
            )
        elif action.type is ActionType.INVESTIGATE:
            events.append(
                {
                    "event_type": "InvestigateSubmitted",
                    "phase": phase_id,
                    "visibility": "PRIVATE",
                    "actor_player_id": seat_id,
                    "payload": {"target": action.target},
                }
            )

    return events


def _resolve_day_vote_events(
    state: GameState,
    responses: Mapping[str, AgentResponse],
    phase_id: str,
) -> list[dict[str, Any]]:
    submissions = {sid: r.action for sid, r in responses.items()}
    result = resolve_day_vote(state, submissions)
    events: list[dict[str, Any]] = [
        {
            "event_type": "DayVoteResolved",
            "phase": phase_id,
            "visibility": "PUBLIC",
            "actor_player_id": None,
            "payload": {
                "eliminated": result.eliminated,
                "vote_tally": dict(result.vote_tally),
                "reason": result.reason,
            },
        }
    ]
    if result.eliminated is not None:
        target_seat = state.seat_by_public_id(result.eliminated)
        if target_seat is not None:
            events.append(
                {
                    "event_type": "PlayerEliminated",
                    "phase": phase_id,
                    "visibility": "PUBLIC",
                    "actor_player_id": None,
                    "payload": {
                        "public_player_id": result.eliminated,
                        "role": target_seat.role.value,
                        "faction": target_seat.faction.value,
                        "cause": CAUSE_DAY_VOTE,
                    },
                }
            )
    return events


def _resolve_night_events(
    state: GameState,
    responses: Mapping[str, AgentResponse],
    phase_id: str,
) -> list[dict[str, Any]]:
    submissions = {sid: r.action for sid, r in responses.items()}
    night = resolve_night(state, submissions)
    events: list[dict[str, Any]] = [
        {
            "event_type": "NightResolved",
            "phase": phase_id,
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {
                "eliminated": night.eliminated,
                "protected": night.protected,
                "mafia_kill_target": night.mafia_kill_target,
            },
        }
    ]
    if night.eliminated is not None:
        target_seat = state.seat_by_public_id(night.eliminated)
        if target_seat is not None:
            events.append(
                {
                    "event_type": "PlayerEliminated",
                    "phase": phase_id,
                    "visibility": "PUBLIC",
                    "actor_player_id": None,
                    "payload": {
                        "public_player_id": night.eliminated,
                        "role": target_seat.role.value,
                        "faction": target_seat.faction.value,
                        "cause": CAUSE_NIGHT_KILL,
                    },
                }
            )
    if night.detective_finding is not None:
        detective_seat = next((s for s in state.seats if s.role is Role.DETECTIVE), None)
        if detective_seat is not None:
            target, finding = night.detective_finding
            events.append(
                {
                    "event_type": "DetectiveResultDelivered",
                    "phase": phase_id,
                    "visibility": "PRIVATE",
                    "actor_player_id": detective_seat.public_player_id,
                    "payload": {"target": target, "finding": finding},
                }
            )
    return events


async def run_game(
    config: GameConfig,
    adapter: LlmAdapter,
    ranked: bool,
    *,
    persistence: GamePersistence | None = None,
) -> GameOutcome:
    """Run a full game start-to-finish and return the recorded outcome.

    When ``persistence`` is supplied, every event flowing through the runner
    is also persisted to the ``game_events`` table and every LLM call is
    persisted to ``llm_calls``. The in-memory event log and DB rows stay in
    lockstep — failure to persist propagates as an exception.
    """
    ruleset = _RULESETS[config.ruleset_id]
    event_log = EventLog()
    llm_calls: list[AdapterResult] = []
    recording: LlmAdapter = _RecordingAdapter(adapter, llm_calls, persistence)
    state = initial_state()

    game_ctx_keys = ("game_id", "ruleset_id")
    structlog.contextvars.bind_contextvars(
        game_id=config.game_id,
        ruleset_id=config.ruleset_id,
    )
    _logger.info(
        EVENT_GAME_STARTED,
        ranked=ranked,
        timeout_s=config.timeout_s,
    )

    async def emit_and_persist(
        body: dict[str, Any],
        game_state: GameState,
        *,
        day_terminated: int | None = None,
    ) -> GameState:
        next_state = _emit(body, game_state, event_log)
        if persistence is not None:
            stored = event_log.events[-1]
            event_type = stored.body["event_type"]
            if event_type == "GameTerminated":
                assert day_terminated is not None
                await _persist_terminated_event(
                    persistence, stored, next_state, ranked, day_terminated
                )
            elif event_type == "RolesAssigned":
                await _persist_roles_assigned(persistence, stored)
            else:
                await _persist_stored_event(persistence, stored)
        return next_state

    async def persist_pending_events(log_before: int) -> None:
        if persistence is None:
            return
        for stored in event_log.events[log_before:]:
            await _persist_stored_event(persistence, stored)

    # GameCreated
    state = await emit_and_persist(
        {
            "event_type": "GameCreated",
            "phase": "SETUP",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {
                "ruleset_id": config.ruleset_id,
                "game_id": config.game_id,
                "game_seed": config.game_seed,
                "player_count": ruleset.PLAYER_COUNT,
            },
        },
        state,
    )

    seats = assign_roles(config.game_seed, ruleset)
    state = await emit_and_persist(
        {
            "event_type": "RolesAssigned",
            "phase": "SETUP",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {
                "assignments": [
                    {
                        "public_player_id": s.public_player_id,
                        "seat_index": s.seat_index,
                        "role": s.role.value,
                        "faction": s.faction.value,
                    }
                    for s in seats
                ],
            },
        },
        state,
    )

    current_phase = next_phase(state.current_phase, ruleset)

    try:
        while True:
            if current_phase.kind is PhaseKind.TERMINAL:
                state = await emit_and_persist(
                    {
                        "event_type": "GameTerminated",
                        "phase": "TERMINAL",
                        "visibility": "PUBLIC",
                        "actor_player_id": None,
                        "payload": {
                            "winner": "DRAW",
                            "reason": REASON_MAX_DAYS_REACHED,
                        },
                    },
                    state,
                    day_terminated=current_phase.day,
                )
                break

            phase_id = format_phase_id(current_phase)
            structlog.contextvars.bind_contextvars(phase_id=phase_id)
            state = await emit_and_persist(
                {
                    "event_type": "PhaseStarted",
                    "phase": phase_id,
                    "visibility": "SYSTEM",
                    "actor_player_id": None,
                    "payload": {
                        "phase_kind": current_phase.kind.value,
                        "day": current_phase.day,
                        "round": current_phase.round,
                    },
                },
                state,
            )
            _logger.info(
                EVENT_PHASE_STARTED,
                phase_kind=current_phase.kind.value,
                day=current_phase.day,
                round=current_phase.round,
            )

            eligible = _eligible_seats(state)
            responses: dict[str, AgentResponse] = {}
            if eligible:
                log_before = len(event_log.events)
                responses = await run_tick(
                    state,
                    event_log,
                    eligible,
                    recording,
                    timeout_s=config.timeout_s,
                    ruleset=ruleset,
                    ranked=ranked,
                )
                # run_tick appends failure events (ActionTimedOut / OutputInvalid)
                # directly to event_log without folding through emit_and_persist;
                # mirror them to the DB now so the persisted chain stays complete.
                await persist_pending_events(log_before)

            for seat in eligible:
                response = responses.get(seat.public_player_id)
                if response is None:
                    continue
                for body in _submission_events_for(
                    seat,
                    response,
                    current_phase.kind,
                    phase_id,
                    current_phase.round,
                ):
                    state = await emit_and_persist(body, state)

            if current_phase.kind is PhaseKind.DAY_VOTE:
                for body in _resolve_day_vote_events(state, responses, phase_id):
                    state = await emit_and_persist(body, state)
            elif current_phase.kind is PhaseKind.NIGHT_ACTIONS:
                for body in _resolve_night_events(state, responses, phase_id):
                    state = await emit_and_persist(body, state)

            state = await emit_and_persist(
                {
                    "event_type": "PhaseResolved",
                    "phase": phase_id,
                    "visibility": "SYSTEM",
                    "actor_player_id": None,
                    "payload": {"resolved_phase": phase_id},
                },
                state,
            )
            _logger.info(EVENT_PHASE_RESOLVED)

            win = check_win(state, ruleset)
            if win is not None:
                state = await emit_and_persist(
                    {
                        "event_type": "GameTerminated",
                        "phase": phase_id,
                        "visibility": "PUBLIC",
                        "actor_player_id": None,
                        "payload": {"winner": win.winner, "reason": win.reason},
                    },
                    state,
                    day_terminated=current_phase.day,
                )
                break

            current_phase = next_phase(current_phase, ruleset)
            await asyncio.sleep(0)

        _logger.info(
            EVENT_GAME_COMPLETED,
            winner=state.terminal_result,
            reason=state.terminal_reason,
        )
    finally:
        structlog.contextvars.unbind_contextvars(*game_ctx_keys, "phase_id")

    return GameOutcome(
        final_state=state,
        event_log=event_log,
        llm_calls=tuple(llm_calls),
    )


__all__ = [
    "CAUSE_DAY_VOTE",
    "CAUSE_NIGHT_KILL",
    "MAFIA_CHANNEL_ID",
    "GameConfig",
    "GameOutcome",
    "GamePersistence",
    "run_game",
]
