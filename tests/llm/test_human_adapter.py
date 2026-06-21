"""Tests for :class:`padrino.llm.human_adapter.HumanAdapter` (US-137).

A human seat is driven through the exact same :class:`LlmAdapter` contract as an
LLM seat: ``complete(observation)`` resolves the seat's turn from buffered human
input (the POST channels) within the per-phase deadline, or coerces to a safe
action (NOOP / ABSTAIN) on timeout — exactly like a misbehaving LLM. Illegal
submissions coerce to a safe action against ``legal_actions_for`` the same way a
malformed LLM response does.

The clock and sleep are injected so these tests are deterministic (no wall-clock
sleeping, no flakiness).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import ActionType, Faction, PhaseKind, Role
from padrino.core.observations import Observation, build_observation
from padrino.core.rulesets import mini7_v1
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.llm.human_adapter import HumanAdapter


def _seat(pid: str, idx: int, role: Role, faction: Faction) -> Seat:
    return Seat(public_player_id=pid, seat_index=idx, role=role, faction=faction, alive=True)


SEATS: tuple[Seat, ...] = (
    _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
    _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
    _seat("P03", 2, Role.DETECTIVE, Faction.TOWN),
    _seat("P04", 3, Role.DOCTOR, Faction.TOWN),
    _seat("P05", 4, Role.VILLAGER, Faction.TOWN),
    _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
    _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
)


def _state(phase: Phase) -> GameState:
    return GameState(
        ruleset_id=mini7_v1.RULESET_ID,
        game_id="G-HUMAN",
        game_seed="seed",
        current_phase=phase,
        seats=SEATS,
        day=phase.day,
    )


def _observation(seat: Seat, phase: Phase) -> Observation:
    return build_observation(_state(phase), seat, EventLog(), mini7_v1)


class _FakeClock:
    """A monotonic clock + async sleep that advance only when sleep is called.

    Polling never blocks on the wall clock: each ``sleep(dt)`` advances ``now``
    by ``dt``. A scripted list of buffered-input answers is consumed one per
    ``pull`` call, so the test fully controls when input "arrives".
    """

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class _ScriptedSource:
    """Returns the next buffered action per ``pull`` call, then ``None`` forever."""

    def __init__(self, answers: Sequence[Action | None]) -> None:
        self._answers = list(answers)
        self.pulls = 0

    async def pull(self, observation: Observation) -> Action | None:
        self.pulls += 1
        if self._answers:
            return self._answers.pop(0)
        return None


def _vote_obs() -> Observation:
    return _observation(SEATS[2], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))


def test_human_adapter_conforms_to_llm_adapter_protocol() -> None:
    adapter = HumanAdapter(pull_action=_ScriptedSource([]).pull)
    assert isinstance(adapter, LlmAdapter)


def test_buffered_legal_action_resolves_to_agent_response() -> None:
    clock = _FakeClock()
    source = _ScriptedSource([None, Action(type=ActionType.VOTE, target="P05")])
    adapter = HumanAdapter(
        pull_action=source.pull,
        deadline_seconds=10.0,
        poll_interval_seconds=1.0,
        clock=clock.now,
        sleep=clock.sleep,
    )

    result = asyncio.run(adapter.complete(_vote_obs()))

    assert isinstance(result, AdapterResult)
    assert isinstance(result.parsed_response, AgentResponse)
    assert result.parsed_response.action == Action(type=ActionType.VOTE, target="P05")
    assert result.status == "ok"
    # It polled, slept once (first None), then resolved on the second pull.
    assert source.pulls == 2


def test_timeout_with_no_input_coerces_to_safe_action() -> None:
    clock = _FakeClock()
    source = _ScriptedSource([])  # never any buffered input
    adapter = HumanAdapter(
        pull_action=source.pull,
        deadline_seconds=3.0,
        poll_interval_seconds=1.0,
        clock=clock.now,
        sleep=clock.sleep,
    )

    result = asyncio.run(adapter.complete(_vote_obs()))

    # DAY_VOTE coerces to ABSTAIN, exactly like a misbehaving LLM timeout.
    assert isinstance(result.parsed_response, AgentResponse)
    assert result.parsed_response.action == Action(type=ActionType.ABSTAIN, target=None)
    assert result.status == "provider_error"
    assert result.error is not None
    # The deadline was actually honoured (no infinite poll loop).
    assert clock.now() >= 3.0


def test_timeout_night_actions_coerces_to_noop() -> None:
    clock = _FakeClock()
    adapter = HumanAdapter(
        pull_action=_ScriptedSource([]).pull,
        deadline_seconds=2.0,
        poll_interval_seconds=1.0,
        clock=clock.now,
        sleep=clock.sleep,
    )
    obs = _observation(SEATS[3], Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0))

    result = asyncio.run(adapter.complete(obs))

    assert isinstance(result.parsed_response, AgentResponse)
    assert result.parsed_response.action == Action(type=ActionType.NOOP, target=None)


def test_illegal_action_type_coerces_to_safe_action() -> None:
    clock = _FakeClock()
    # PROTECT is not legal for a detective on DAY_VOTE.
    source = _ScriptedSource([Action(type=ActionType.PROTECT, target="P05")])
    adapter = HumanAdapter(
        pull_action=source.pull,
        deadline_seconds=10.0,
        poll_interval_seconds=1.0,
        clock=clock.now,
        sleep=clock.sleep,
    )

    result = asyncio.run(adapter.complete(_vote_obs()))

    assert isinstance(result.parsed_response, AgentResponse)
    assert result.parsed_response.action == Action(type=ActionType.ABSTAIN, target=None)
    assert result.status == "schema_violation"


def test_illegal_target_coerces_to_safe_action() -> None:
    clock = _FakeClock()
    # VOTE for self is not a legal target on DAY_VOTE.
    source = _ScriptedSource([Action(type=ActionType.VOTE, target="P03")])
    adapter = HumanAdapter(
        pull_action=source.pull,
        deadline_seconds=10.0,
        poll_interval_seconds=1.0,
        clock=clock.now,
        sleep=clock.sleep,
    )

    result = asyncio.run(adapter.complete(_vote_obs()))

    assert isinstance(result.parsed_response, AgentResponse)
    assert result.parsed_response.action == Action(type=ActionType.ABSTAIN, target=None)
    assert result.status == "schema_violation"


def test_targetless_action_with_target_is_illegal() -> None:
    clock = _FakeClock()
    # ABSTAIN must not carry a target.
    source = _ScriptedSource([Action(type=ActionType.ABSTAIN, target="P05")])
    adapter = HumanAdapter(
        pull_action=source.pull,
        deadline_seconds=10.0,
        poll_interval_seconds=1.0,
        clock=clock.now,
        sleep=clock.sleep,
    )

    result = asyncio.run(adapter.complete(_vote_obs()))

    assert isinstance(result.parsed_response, AgentResponse)
    assert result.parsed_response.action == Action(type=ActionType.ABSTAIN, target=None)
    assert result.status == "schema_violation"


def test_legal_abstain_passes_through() -> None:
    clock = _FakeClock()
    source = _ScriptedSource([Action(type=ActionType.ABSTAIN, target=None)])
    adapter = HumanAdapter(
        pull_action=source.pull,
        deadline_seconds=10.0,
        poll_interval_seconds=1.0,
        clock=clock.now,
        sleep=clock.sleep,
    )

    result = asyncio.run(adapter.complete(_vote_obs()))

    assert isinstance(result.parsed_response, AgentResponse)
    assert result.parsed_response.action == Action(type=ActionType.ABSTAIN, target=None)
    assert result.status == "ok"


def test_immediate_buffered_input_does_not_sleep() -> None:
    clock = _FakeClock()
    source = _ScriptedSource([Action(type=ActionType.VOTE, target="P05")])
    adapter = HumanAdapter(
        pull_action=source.pull,
        deadline_seconds=10.0,
        poll_interval_seconds=1.0,
        clock=clock.now,
        sleep=clock.sleep,
    )

    result = asyncio.run(adapter.complete(_vote_obs()))

    assert result.status == "ok"
    assert source.pulls == 1
    assert clock.now() == 0.0  # resolved before any sleep
