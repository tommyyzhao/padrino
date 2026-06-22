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
from collections.abc import Awaitable, Callable, Mapping, Sequence
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
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.engine.win_conditions import REASON_MAX_DAYS_REACHED, check_win
from padrino.core.enums import ActionType, Faction, PhaseKind, Role
from padrino.core.observations import Observation, format_phase_id
from padrino.core.rulesets import Ruleset as CoreRuleset
from padrino.core.rulesets import get_ruleset, mini7_v1
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import games as games_repo
from padrino.db.repositories import llm_calls as llm_calls_repo
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.observability.events import (
    EVENT_GAME_COMPLETED,
    EVENT_GAME_STARTED,
    EVENT_PHASE_RESOLVED,
    EVENT_PHASE_STARTED,
    EVENT_PRIVACY_AUDIT_COMPLETED,
    EVENT_PRIVACY_AUDIT_LEAK_DETECTED,
    EVENT_RATING_UPDATED,
)
from padrino.observability.metrics import record_game_completed
from padrino.observability.privacy_audit import audit_ranked_observations
from padrino.observability.timing import time_phase
from padrino.ratings.openskill_service import (
    GameResult,
    update_ratings_for_completed_pair,
    update_ratings_for_game,
)
from padrino.ratings.solo_rate_service import (
    SoloRateAttempt,
    SoloRateGameResult,
    update_solo_rate_ratings_for_game,
)
from padrino.runner.tick import run_tick

_logger = structlog.get_logger("padrino.runner")

MAFIA_CHANNEL_ID: Final[str] = "mafia"
CAUSE_DAY_VOTE: Final[str] = "day_vote"
CAUSE_NIGHT_KILL: Final[str] = "night_kill"
STATUS_COMPLETED: Final[str] = "COMPLETED"
OBSERVATION_FEEDBACK_CODES: Final[frozenset[str]] = frozenset(
    {"ACTION_BLOCKED", "TRACK_RESULT", "WATCH_RESULT"}
)

_RULESETS: dict[str, Any] = {}


def _ruleset_for(ruleset_id: str) -> CoreRuleset:
    override = _RULESETS.get(ruleset_id)
    if override is not None:
        return cast(CoreRuleset, override)
    return get_ruleset(ruleset_id)


@dataclass(frozen=True, slots=True)
class GameResume:
    """Durable state for resuming a previously-started game loop.

    The event log remains authoritative for deterministic core state. ``phase`` /
    ``deadline_at`` / ``buffer_snapshot`` are the impure runtime scaffolding read
    from ``human_game_runtime`` so the human lane can resume a live phase without
    re-emitting setup or losing buffered input.
    """

    state: GameState
    event_log: EventLog
    phase: str
    deadline_at: Any = None
    buffer_snapshot: Mapping[str, object] = field(default_factory=dict)


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
    resume: GameResume | None = None


class GameConfig(BaseModel):
    """Input config for a single game run."""

    model_config = ConfigDict(frozen=True)

    game_id: str
    game_seed: str
    ruleset_id: str = mini7_v1.RULESET_ID
    timeout_s: float = float(mini7_v1.LLM_TIMEOUT_SECONDS)


TickRunner = Callable[
    [GameState, EventLog, Sequence[Seat], LlmAdapter, CoreRuleset, bool, float],
    Awaitable[dict[str, AgentResponse]],
]

PhaseSnapshotHook = Callable[[GameState, EventLog, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class GameOutcome:
    """Outputs returned to the caller after the game terminates."""

    final_state: GameState
    event_log: EventLog
    llm_calls: tuple[AdapterResult, ...]

    @property
    def seat_assignments(self) -> tuple[Seat, ...]:
        """Convenience alias for ``final_state.seats`` — the post-replay seat roster.

        Useful for callers that want to pass the seat assignments into the
        US-078 privacy auditor (``audit_ranked_observations``) without
        reaching through ``final_state``.
        """
        return self.final_state.seats


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


async def _persist_pending_event_rows(
    persistence: GamePersistence,
    event_log: EventLog,
    log_before: int,
) -> None:
    """Mirror every not-yet-persisted ``event_log`` row from ``log_before`` to the DB.

    ``run_tick`` appends failure events (``ActionTimedOut`` / ``OutputInvalid``)
    straight to the in-memory ``event_log`` without folding them through
    ``emit_and_persist``; this is the only place they are mirrored to
    ``game_events``. A paired DB mutation (human seat takeover / chat release)
    may co-commit its OWN event row in the same transaction as the seat/sidecar
    write so the chain never lags that state across a crash — and that
    co-committed event can carry a HIGHER sequence than the failure events
    appended earlier in the same tick. We therefore skip the EXACT set of
    sequences already committed (not a max-sequence threshold, which would drop
    the lower un-persisted failure rows that live below a co-committed row),
    persisting every remaining event and never re-inserting a committed row
    (which would trip ``uq_game_event_sequence``).
    """
    pending = event_log.events[log_before:]
    if not pending:
        return
    async with persistence.session_factory() as session:
        already_persisted = await events_repo.persisted_sequences_from(
            session, persistence.game_id, from_sequence=log_before
        )
    for stored in pending:
        if stored.sequence in already_persisted:
            continue
        await _persist_stored_event(persistence, stored)


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


def _should_apply_solo_rate(
    persistence: GamePersistence,
    ranked: bool,
    state: GameState,
) -> bool:
    if not ranked or not persistence.agent_builds:
        return False
    if state.terminal_result is None:
        return False
    if not any(s.role is Role.JESTER for s in state.seats):
        return False
    return all(s.public_player_id in persistence.agent_builds for s in state.seats)


def _solo_rate_result_for(state: GameState, game_id: uuid.UUID) -> SoloRateGameResult | None:
    jester_attempts = tuple(
        SoloRateAttempt(
            public_player_id=seat.public_player_id,
            role=Role.JESTER.value,
            succeeded=state.terminal_result == "JESTER",
        )
        for seat in state.seats
        if seat.role is Role.JESTER
    )
    if not jester_attempts:
        return None
    return SoloRateGameResult(
        game_id=game_id,
        outcome_label="JESTER_LYNCH_BAIT",
        attempts=jester_attempts,
    )


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
    apply_solo_rate = _should_apply_solo_rate(persistence, ranked, state)
    terminal_result_payload: dict[str, Any] = {
        "winner": state.terminal_result,
        "reason": state.terminal_reason,
        "day_terminated": day_terminated,
    }
    async with persistence.session_factory() as session, session.begin():
        game = await games_repo.get(session, persistence.game_id)
        if game is not None and game.status == STATUS_COMPLETED:
            _logger.info(
                "Game already terminated and completed; skipping rating application and status updates.",
                game_id=str(persistence.game_id),
            )
            return

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
            if game is not None and game.pair_id is not None:
                rating_events = await update_ratings_for_completed_pair(
                    session,
                    league_id=persistence.league_id,
                    pair_id=game.pair_id,
                )
                rated_seat_count = len(persistence.agent_builds)
                winner = cast(Literal["TOWN", "MAFIA", "DRAW"], state.terminal_result)
            else:
                winner = cast(Literal["TOWN", "MAFIA", "DRAW"], state.terminal_result)
                seat_factions: dict[str, Faction] = {
                    s.public_player_id: s.faction for s in state.seats
                }
                agent_builds_by_seat: dict[str, uuid.UUID] = {
                    sid: persistence.agent_builds[sid] for sid in seat_factions
                }
                rating_events = await update_ratings_for_game(
                    session,
                    league_id=persistence.league_id,
                    game_result=GameResult(
                        game_id=persistence.game_id,
                        winner=winner,
                        seat_factions=seat_factions,
                    ),
                    agent_builds_by_seat=agent_builds_by_seat,
                )
                rated_seat_count = len(agent_builds_by_seat)
            if rating_events:
                _logger.info(
                    EVENT_RATING_UPDATED,
                    league_id=str(persistence.league_id),
                    winner=winner,
                    seats=rated_seat_count,
                )
        if apply_solo_rate:
            solo_result = _solo_rate_result_for(state, persistence.game_id)
            if solo_result is not None:
                await update_solo_rate_ratings_for_game(
                    session,
                    game_result=solo_result,
                    agent_builds_by_seat=persistence.agent_builds,
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


def _phase_started_without_resolution(event_log: EventLog, phase_id: str) -> bool:
    """Return whether ``phase_id`` has a committed start but no resolution yet.

    Rehydrated human games may restart after ``PhaseStarted`` has already been
    persisted. In that case the loop must resume the tick for that phase, not
    append a second ``PhaseStarted`` row at the existing hash-chain head.
    """
    for stored in reversed(event_log.events):
        body = stored.body
        if body.get("phase") != phase_id:
            continue
        event_type = body.get("event_type")
        if event_type == "PhaseResolved":
            return False
        if event_type == "PhaseStarted":
            return True
    return False


def _resume_phase(state: GameState, event_log: EventLog, ruleset: Any) -> tuple[Phase, bool]:
    """Return ``(phase, already_started)`` for a rehydrated game state."""
    if state.current_phase.kind is PhaseKind.TERMINAL:
        return state.current_phase, True
    phase_id = format_phase_id(state.current_phase)
    if _phase_started_without_resolution(event_log, phase_id):
        return state.current_phase, True
    return next_phase(state.current_phase, ruleset), False


async def _default_tick_runner(
    state: GameState,
    event_log: EventLog,
    eligible_seats: Sequence[Seat],
    adapter: LlmAdapter,
    ruleset: CoreRuleset,
    ranked: bool,
    timeout_s: float,
) -> dict[str, AgentResponse]:
    """Run the standard benchmark tick barrier."""
    return await run_tick(
        state,
        event_log,
        eligible_seats,
        adapter,
        timeout_s=timeout_s,
        ruleset=ruleset,
        ranked=ranked,
    )


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
        elif action.type is ActionType.ROLEBLOCK:
            events.append(
                {
                    "event_type": "RoleblockSubmitted",
                    "phase": phase_id,
                    "visibility": "PRIVATE",
                    "actor_player_id": seat_id,
                    "payload": {"target": action.target},
                }
            )
        elif action.type is ActionType.FRAME:
            events.append(
                {
                    "event_type": "FrameSubmitted",
                    "phase": phase_id,
                    "visibility": "PRIVATE",
                    "actor_player_id": seat_id,
                    "payload": {"target": action.target},
                }
            )
        elif action.type is ActionType.TRACK:
            events.append(
                {
                    "event_type": "TrackSubmitted",
                    "phase": phase_id,
                    "visibility": "PRIVATE",
                    "actor_player_id": seat_id,
                    "payload": {"target": action.target},
                }
            )
        elif action.type is ActionType.WATCH:
            events.append(
                {
                    "event_type": "WatchSubmitted",
                    "phase": phase_id,
                    "visibility": "PRIVATE",
                    "actor_player_id": seat_id,
                    "payload": {"target": action.target},
                }
            )
        elif action.type is ActionType.CLEAN:
            events.append(
                {
                    "event_type": "CleanSubmitted",
                    "phase": phase_id,
                    "visibility": "PRIVATE",
                    "actor_player_id": seat_id,
                    "payload": {"target": action.target},
                }
            )
        elif action.type is ActionType.SERIAL_KILL:
            events.append(
                {
                    "event_type": "SerialKillSubmitted",
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
    night_payload: dict[str, Any] = {
        "eliminated": night.eliminated,
        "protected": night.protected,
        "mafia_kill_target": night.mafia_kill_target,
    }
    if night.cleaned_deaths:
        night_payload["cleaned_deaths"] = night.cleaned_deaths
        night_payload["clean_spent_actor_ids"] = night.clean_spent_actor_ids
    if night.framed_targets:
        night_payload["framed_targets"] = night.framed_targets
        night_payload["frame_spent_actor_ids"] = night.frame_spent_actor_ids
    if night.serial_kill_target is not None:
        night_payload["serial_kill_target"] = night.serial_kill_target
        night_payload["eliminated_player_ids"] = night.eliminated_player_ids
    elif len(night.eliminated_player_ids) > 1:
        night_payload["eliminated_player_ids"] = night.eliminated_player_ids
    events: list[dict[str, Any]] = [
        {
            "event_type": "NightResolved",
            "phase": phase_id,
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": night_payload,
        }
    ]
    for eliminated in night.eliminated_player_ids:
        death_reveal = next(
            (reveal for reveal in night.death_reveals if reveal.public_player_id == eliminated),
            None,
        )
        if death_reveal is not None:
            payload: dict[str, Any] = {
                "public_player_id": death_reveal.public_player_id,
                "cause": CAUSE_NIGHT_KILL,
            }
            if death_reveal.role is not None:
                payload["role"] = death_reveal.role.value
            if death_reveal.faction is not None:
                payload["faction"] = death_reveal.faction.value
            events.append(
                {
                    "event_type": "PlayerEliminated",
                    "phase": phase_id,
                    "visibility": "PUBLIC",
                    "actor_player_id": None,
                    "payload": payload,
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
    for feedback in night.feedback:
        if feedback.code not in OBSERVATION_FEEDBACK_CODES:
            continue
        events.append(
            {
                "event_type": "NightFeedbackDelivered",
                "phase": phase_id,
                "visibility": "PRIVATE",
                "actor_player_id": feedback.recipient,
                "payload": {
                    "code": feedback.code,
                    "target": feedback.target,
                    "finding": feedback.finding,
                    "visited_player_ids": feedback.visited_player_ids,
                    "visitor_player_ids": feedback.visitor_player_ids,
                },
            }
        )
    return events


async def drive_game_loop(
    config: GameConfig,
    adapter: LlmAdapter,
    ranked: bool,
    *,
    persistence: GamePersistence | None = None,
    tick_runner: TickRunner | None = None,
    resume: GameResume | None = None,
    phase_snapshot: PhaseSnapshotHook | None = None,
) -> GameOutcome:
    """Run a full game start-to-finish with an injectable per-phase tick runner.

    When ``persistence`` is supplied, every event flowing through the runner
    is also persisted to the ``game_events`` table and every LLM call is
    persisted to ``llm_calls``. The in-memory event log and DB rows stay in
    lockstep — failure to persist propagates as an exception.

    ``tick_runner`` defaults to the benchmark tick barrier. The human lane uses
    the same deterministic game loop but supplies a human-aware tick wrapper.
    """
    ruleset = _ruleset_for(config.ruleset_id)
    event_log = resume.event_log if resume is not None else EventLog()
    llm_calls: list[AdapterResult] = []
    recording: LlmAdapter = _RecordingAdapter(adapter, llm_calls, persistence)
    state = resume.state if resume is not None else initial_state()
    run_phase_tick = tick_runner or _default_tick_runner

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
        await _persist_pending_event_rows(persistence, event_log, log_before)

    async def snapshot_phase(game_state: GameState, phase_id: str) -> None:
        if phase_snapshot is not None:
            await phase_snapshot(game_state, event_log, phase_id)

    if resume is None:
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
        phase_started_from_resume = False
    else:
        current_phase, phase_started_from_resume = _resume_phase(state, event_log, ruleset)

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
            with time_phase(config.ruleset_id, current_phase.kind.value):
                if phase_started_from_resume:
                    phase_started_from_resume = False
                else:
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
                await snapshot_phase(state, phase_id)

                eligible = _eligible_seats(state)
                responses: dict[str, AgentResponse] = {}
                if eligible:
                    log_before = len(event_log.events)
                    responses = await run_phase_tick(
                        state,
                        event_log,
                        eligible,
                        recording,
                        ruleset,
                        ranked,
                        config.timeout_s,
                    )
                    # run_tick appends failure events (ActionTimedOut / OutputInvalid)
                    # directly to event_log without folding through emit_and_persist;
                    # mirror them to the DB now so the persisted chain stays complete.
                    await persist_pending_events(log_before)
                    await snapshot_phase(state, phase_id)

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
                await snapshot_phase(state, phase_id)
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
        record_game_completed(
            outcome=state.terminal_result or "UNKNOWN",
            ruleset=config.ruleset_id,
        )

        audit_report = audit_ranked_observations(event_log, state.seats)
        _logger.info(
            EVENT_PRIVACY_AUDIT_COMPLETED,
            finding_count=audit_report.finding_count,
        )
        if audit_report.finding_count > 0:
            _logger.info(
                EVENT_PRIVACY_AUDIT_LEAK_DETECTED,
                finding_count=audit_report.finding_count,
            )
    finally:
        structlog.contextvars.unbind_contextvars(*game_ctx_keys, "phase_id")

    return GameOutcome(
        final_state=state,
        event_log=event_log,
        llm_calls=tuple(llm_calls),
    )


async def run_game(
    config: GameConfig,
    adapter: LlmAdapter,
    ranked: bool,
    *,
    persistence: GamePersistence | None = None,
) -> GameOutcome:
    """Run a benchmark game with the standard tick barrier.

    This preserves the historical public runner API used by gauntlets,
    scheduler jobs, and tests. Human-lane games call :func:`drive_game_loop`
    with their own tick runner instead of using this benchmark shortcut.
    """
    return await drive_game_loop(
        config,
        adapter,
        ranked,
        persistence=persistence,
        tick_runner=_default_tick_runner,
    )


__all__ = [
    "CAUSE_DAY_VOTE",
    "CAUSE_NIGHT_KILL",
    "MAFIA_CHANNEL_ID",
    "GameConfig",
    "GameOutcome",
    "GamePersistence",
    "GameResume",
    "PhaseSnapshotHook",
    "TickRunner",
    "drive_game_loop",
    "run_game",
]
