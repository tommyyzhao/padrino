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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.event_log import EventLog
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
from padrino.core.observations import format_phase_id
from padrino.core.rulesets import mini7_v1
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.runner.tick import run_tick

MAFIA_CHANNEL_ID: Final[str] = "mafia"
CAUSE_DAY_VOTE: Final[str] = "day_vote"
CAUSE_NIGHT_KILL: Final[str] = "night_kill"

_RULESETS: Final[dict[str, Any]] = {mini7_v1.RULESET_ID: mini7_v1}


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
    """Wraps an :class:`LlmAdapter` so every :class:`AdapterResult` is captured."""

    __slots__ = ("_inner", "_sink")

    def __init__(self, inner: LlmAdapter, sink: list[AdapterResult]) -> None:
        self._inner = inner
        self._sink = sink

    async def complete(self, observation: Any) -> AdapterResult:
        result = await self._inner.complete(observation)
        self._sink.append(result)
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
) -> GameOutcome:
    """Run a full game start-to-finish and return the recorded outcome."""
    ruleset = _RULESETS[config.ruleset_id]
    event_log = EventLog()
    llm_calls: list[AdapterResult] = []
    recording: LlmAdapter = _RecordingAdapter(adapter, llm_calls)
    state = initial_state()

    # GameCreated
    state = _emit(
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
        event_log,
    )

    seats = assign_roles(config.game_seed, ruleset)
    state = _emit(
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
        event_log,
    )

    current_phase = next_phase(state.current_phase, ruleset)

    while True:
        if current_phase.kind is PhaseKind.TERMINAL:
            state = _emit(
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
                event_log,
            )
            break

        phase_id = format_phase_id(current_phase)
        state = _emit(
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
            event_log,
        )

        eligible = _eligible_seats(state)
        responses: dict[str, AgentResponse] = {}
        if eligible:
            responses = await run_tick(
                state,
                event_log,
                eligible,
                recording,
                timeout_s=config.timeout_s,
                ruleset=ruleset,
                ranked=ranked,
            )

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
                state = _emit(body, state, event_log)

        if current_phase.kind is PhaseKind.DAY_VOTE:
            for body in _resolve_day_vote_events(state, responses, phase_id):
                state = _emit(body, state, event_log)
        elif current_phase.kind is PhaseKind.NIGHT_ACTIONS:
            for body in _resolve_night_events(state, responses, phase_id):
                state = _emit(body, state, event_log)

        state = _emit(
            {
                "event_type": "PhaseResolved",
                "phase": phase_id,
                "visibility": "SYSTEM",
                "actor_player_id": None,
                "payload": {"resolved_phase": phase_id},
            },
            state,
            event_log,
        )

        win = check_win(state, ruleset)
        if win is not None:
            state = _emit(
                {
                    "event_type": "GameTerminated",
                    "phase": phase_id,
                    "visibility": "PUBLIC",
                    "actor_player_id": None,
                    "payload": {"winner": win.winner, "reason": win.reason},
                },
                state,
                event_log,
            )
            break

        current_phase = next_phase(current_phase, ruleset)
        await asyncio.sleep(0)

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
    "run_game",
]
