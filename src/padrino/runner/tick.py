"""Async tick barrier with hard timeout.

`run_tick` builds an :class:`~padrino.core.observations.Observation` for every
eligible seat, dispatches each through the supplied
:class:`~padrino.llm.adapter.LlmAdapter` concurrently under a per-call
``asyncio.wait_for`` timeout, and returns the validated (or coerced)
:class:`~padrino.core.agents.contract.AgentResponse` for each seat once every
call has either completed or timed out — never earlier.

Failures are recorded into the supplied ``event_log`` in seat-order after the
gather completes:

* timeout → :class:`~padrino.core.engine.events.ActionTimedOut`
* parse / schema failure → :class:`~padrino.core.engine.events.OutputInvalid`

For each failing seat the returned mapping carries the coerced safe-action
response (see :mod:`padrino.core.agents.coercion`).

When ``ranked=True`` the observation is screened through
:func:`~padrino.core.observation_privacy.assert_ranked_observation_safe`
*before* it leaves the runner — a guard violation aborts the tick because it
signals an engine-side bug, not an adversarial agent.

Impure runner module; pure-core code does not import it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from padrino.core.agents.coercion import coerce_response_failure, coerce_to_safe_action
from padrino.core.agents.contract import AgentResponse, ResponseError
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.legal_actions import legal_actions_for
from padrino.core.engine.state import GameState, Seat
from padrino.core.enums import ActionType
from padrino.core.observation_privacy import assert_ranked_observation_safe
from padrino.core.observations import Ruleset, build_observation, format_phase_id
from padrino.llm.adapter import LlmAdapter

_REASON_TIMEOUT = "TIMEOUT"


@dataclass(frozen=True, slots=True)
class _SeatOutcome:
    seat_id: str
    response: AgentResponse
    failure_event: dict[str, Any] | None


async def run_tick(
    state: GameState,
    event_log: EventLog,
    eligible_seats: Sequence[Seat],
    adapter: LlmAdapter,
    timeout_s: float,
    ruleset: Ruleset,
    *,
    ranked: bool = False,
    private_memories: Mapping[str, str] | None = None,
) -> dict[str, AgentResponse]:
    """Dispatch one observation per eligible seat and collect coerced responses.

    Returns a mapping from ``public_player_id`` to the validated (or coerced)
    :class:`AgentResponse`. Side effect: failure events (``ActionTimedOut``,
    ``OutputInvalid``) are appended to ``event_log`` in seat order after every
    call has settled.
    """

    phase_id = format_phase_id(state.current_phase)
    memories: Mapping[str, str] = private_memories or {}

    tasks = [
        asyncio.create_task(
            _call_one_seat(
                state=state,
                event_log=event_log,
                seat=seat,
                adapter=adapter,
                timeout_s=timeout_s,
                ruleset=ruleset,
                ranked=ranked,
                private_memory=memories.get(seat.public_player_id, ""),
                phase_id=phase_id,
            )
        )
        for seat in eligible_seats
    ]
    outcomes = await asyncio.gather(*tasks)

    final: dict[str, AgentResponse] = {}
    for outcome in outcomes:
        if outcome.failure_event is not None:
            body = dict(outcome.failure_event)
            body["sequence"] = len(event_log.events)
            event_log.append(body)
        final[outcome.seat_id] = outcome.response
    return final


async def _call_one_seat(
    *,
    state: GameState,
    event_log: EventLog,
    seat: Seat,
    adapter: LlmAdapter,
    timeout_s: float,
    ruleset: Ruleset,
    ranked: bool,
    private_memory: str,
    phase_id: str,
) -> _SeatOutcome:
    observation = build_observation(state, seat, event_log, ruleset, private_memory)
    if ranked:
        assert_ranked_observation_safe(observation)

    expected = _expected_action_type(state, seat)

    try:
        result = await asyncio.wait_for(adapter.complete(observation), timeout=timeout_s)
    except TimeoutError:
        defaulted = coerce_to_safe_action(state.current_phase, _REASON_TIMEOUT).type.value
        return _build_failure_outcome(
            state=state,
            seat=seat,
            phase_id=phase_id,
            reason=_REASON_TIMEOUT,
            event_type="ActionTimedOut",
            payload={"expected_action_type": expected, "defaulted_to": defaulted},
        )

    parsed = result.parsed_response
    if isinstance(parsed, ResponseError):
        return _build_failure_outcome(
            state=state,
            seat=seat,
            phase_id=phase_id,
            reason=parsed.reason,
            event_type="OutputInvalid",
            payload={
                "reason": parsed.reason,
                "validation_errors": (parsed.details,) if parsed.details else (),
            },
        )

    return _SeatOutcome(seat_id=seat.public_player_id, response=parsed, failure_event=None)


def _build_failure_outcome(
    *,
    state: GameState,
    seat: Seat,
    phase_id: str,
    reason: str,
    event_type: str,
    payload: dict[str, Any],
) -> _SeatOutcome:
    response = coerce_response_failure(state.current_phase, reason)
    body: dict[str, Any] = {
        "event_type": event_type,
        "phase": phase_id,
        "visibility": "SYSTEM",
        "actor_player_id": seat.public_player_id,
        "payload": payload,
    }
    return _SeatOutcome(seat_id=seat.public_player_id, response=response, failure_event=body)


def _expected_action_type(state: GameState, seat: Seat) -> str:
    legal = legal_actions_for(state, seat)
    if not legal.allowed_action_types:
        return ActionType.NOOP.value
    return legal.allowed_action_types[0].value


__all__ = ["run_tick"]
