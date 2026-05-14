"""Tests for :func:`padrino.runner.tick.run_tick`.

Covers the async tick barrier with hard timeout: success path, timeouts,
parse/schema failures, mixed-mode runs, no-early-return semantics, ranked
privacy filter invocation, and event-order determinism.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping

import pytest

from padrino.core.agents.contract import (
    REASON_INVALID_JSON,
    REASON_SCHEMA_VIOLATION,
    AgentResponse,
    ResponseError,
)
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import ActionType, Faction, PhaseKind, Role
from padrino.core.observation_privacy import RankedPrivacyViolation
from padrino.core.observations import Observation
from padrino.core.rulesets import mini7_v1
from padrino.llm.adapter import AdapterResult
from padrino.runner.tick import run_tick


def _seat(
    pid: str,
    idx: int,
    role: Role,
    faction: Faction,
    *,
    alive: bool = True,
) -> Seat:
    return Seat(
        public_player_id=pid,
        seat_index=idx,
        role=role,
        faction=faction,
        alive=alive,
    )


SEATS: tuple[Seat, ...] = (
    _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
    _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
    _seat("P03", 2, Role.DETECTIVE, Faction.TOWN),
    _seat("P04", 3, Role.DOCTOR, Faction.TOWN),
    _seat("P05", 4, Role.VILLAGER, Faction.TOWN),
    _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
    _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
)


def _state(phase: Phase, seats: tuple[Seat, ...] = SEATS) -> GameState:
    return GameState(
        ruleset_id="mini7_v1",
        game_id="G-TEST",
        game_seed="seed-abc",
        current_phase=phase,
        seats=seats,
        day=phase.day,
    )


def _ok(action_type: ActionType, target: str | None = None) -> AgentResponse:
    return AgentResponse(
        public_message=None,
        private_message=None,
        action=Action(type=action_type, target=target),
        memory_update="",
        rationale_summary=None,
    )


def _ok_result(action_type: ActionType, target: str | None = None) -> AdapterResult:
    return AdapterResult(
        raw_response="{}",
        parsed_response=_ok(action_type, target),
        latency_ms=10,
    )


def _err_result(
    reason: str = REASON_SCHEMA_VIOLATION, details: str = "missing field"
) -> AdapterResult:
    return AdapterResult(
        raw_response="{not json",
        parsed_response=ResponseError(reason=reason, details=details),  # type: ignore[arg-type]
        latency_ms=8,
        status="schema_violation",
        error=details,
    )


class _ScriptedAdapter:
    """Adapter that returns canned :class:`AdapterResult` keyed by seat id.

    Each entry may optionally include a delay so we can exercise the
    `asyncio.wait_for` timeout path without sleeping the whole test.
    """

    def __init__(
        self,
        results: Mapping[str, AdapterResult],
        delays: Mapping[str, float] | None = None,
    ) -> None:
        self._results = dict(results)
        self._delays = dict(delays or {})
        self.calls: list[str] = []

    async def complete(self, observation: Observation) -> AdapterResult:
        seat_id = observation.you.player_id
        self.calls.append(seat_id)
        delay = self._delays.get(seat_id, 0.0)
        if delay:
            await asyncio.sleep(delay)
        if seat_id not in self._results:
            raise KeyError(seat_id)
        return self._results[seat_id]


# --- success path -----------------------------------------------------------


async def test_all_seats_succeed_returns_parsed_responses() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))
    log = EventLog()
    adapter = _ScriptedAdapter(
        {
            "P01": _ok_result(ActionType.VOTE, "P03"),
            "P02": _ok_result(ActionType.VOTE, "P04"),
            "P03": _ok_result(ActionType.VOTE, "P01"),
        }
    )
    out = await run_tick(state, log, list(SEATS[:3]), adapter, timeout_s=1.0, ruleset=mini7_v1)
    assert set(out) == {"P01", "P02", "P03"}
    assert out["P01"].action.target == "P03"
    assert out["P02"].action.target == "P04"
    assert out["P03"].action.target == "P01"
    assert log.events == ()  # no failure events emitted on the happy path
    assert sorted(adapter.calls) == ["P01", "P02", "P03"]


# --- timeout path -----------------------------------------------------------


async def test_timeout_records_action_timed_out_and_coerces_to_abstain_in_vote() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))
    log = EventLog()
    adapter = _ScriptedAdapter(
        {"P01": _ok_result(ActionType.VOTE, "P02")},
        delays={"P01": 0.2},
    )

    out = await run_tick(state, log, [SEATS[0]], adapter, timeout_s=0.05, ruleset=mini7_v1)

    assert out["P01"].action.type is ActionType.ABSTAIN
    assert out["P01"].action.target is None
    assert out["P01"].public_message is None
    assert out["P01"].private_message is None
    assert out["P01"].memory_update == ""
    assert out["P01"].rationale_summary is None

    assert len(log.events) == 1
    body = log.events[0].body
    assert body["event_type"] == "ActionTimedOut"
    assert body["actor_player_id"] == "P01"
    assert body["visibility"] == "SYSTEM"
    assert body["phase"] == "DAY_1_VOTE"
    assert body["payload"]["expected_action_type"] == ActionType.VOTE.value
    assert body["payload"]["defaulted_to"] == ActionType.ABSTAIN.value
    assert body["sequence"] == 0


async def test_timeout_at_night_coerces_to_noop() -> None:
    state = _state(Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0))
    log = EventLog()
    # P04 is the doctor — expected action is PROTECT, defaulted to NOOP.
    adapter = _ScriptedAdapter(
        {"P04": _ok_result(ActionType.PROTECT, "P03")},
        delays={"P04": 0.2},
    )

    out = await run_tick(state, log, [SEATS[3]], adapter, timeout_s=0.05, ruleset=mini7_v1)

    assert out["P04"].action.type is ActionType.NOOP
    assert log.events[0].body["payload"]["expected_action_type"] == ActionType.PROTECT.value
    assert log.events[0].body["payload"]["defaulted_to"] == ActionType.NOOP.value


# --- parse / schema failure path -------------------------------------------


async def test_schema_violation_records_output_invalid_and_coerces() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))
    log = EventLog()
    adapter = _ScriptedAdapter(
        {"P01": _err_result(REASON_SCHEMA_VIOLATION, "field 'action' missing")}
    )

    out = await run_tick(state, log, [SEATS[0]], adapter, timeout_s=1.0, ruleset=mini7_v1)

    assert out["P01"].action.type is ActionType.ABSTAIN
    assert len(log.events) == 1
    body = log.events[0].body
    assert body["event_type"] == "OutputInvalid"
    assert body["actor_player_id"] == "P01"
    assert body["payload"]["reason"] == REASON_SCHEMA_VIOLATION
    assert body["payload"]["validation_errors"] == ("field 'action' missing",)


async def test_invalid_json_records_output_invalid() -> None:
    state = _state(Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0))
    log = EventLog()
    adapter = _ScriptedAdapter({"P04": _err_result(REASON_INVALID_JSON, "Expecting value: line 1")})

    out = await run_tick(state, log, [SEATS[3]], adapter, timeout_s=1.0, ruleset=mini7_v1)

    assert out["P04"].action.type is ActionType.NOOP
    body = log.events[0].body
    assert body["event_type"] == "OutputInvalid"
    assert body["payload"]["reason"] == REASON_INVALID_JSON


async def test_invalid_json_with_no_details_records_empty_validation_errors() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))
    log = EventLog()
    adapter = _ScriptedAdapter(
        {
            "P01": AdapterResult(
                raw_response="",
                parsed_response=ResponseError(reason=REASON_INVALID_JSON),
                latency_ms=1,
            )
        }
    )
    await run_tick(state, log, [SEATS[0]], adapter, timeout_s=1.0, ruleset=mini7_v1)
    assert log.events[0].body["payload"]["validation_errors"] == ()


# --- mixed-mode run + no-early-return ---------------------------------------


async def test_mixed_slow_fast_error_no_early_return_and_all_events_recorded() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))
    log = EventLog()
    adapter = _ScriptedAdapter(
        results={
            "P01": _ok_result(ActionType.VOTE, "P02"),  # fast OK
            "P02": _ok_result(ActionType.VOTE, "P01"),  # slow → timeout
            "P03": _err_result(REASON_SCHEMA_VIOLATION, "bad"),  # parse fail
            "P04": _ok_result(ActionType.VOTE, "P05"),  # OK
        },
        delays={"P02": 0.3},
    )

    start = time.monotonic()
    out = await run_tick(
        state,
        log,
        [SEATS[0], SEATS[1], SEATS[2], SEATS[3]],
        adapter,
        timeout_s=0.1,
        ruleset=mini7_v1,
    )
    elapsed = time.monotonic() - start

    # No early return — gather must have waited for the slow agent's timeout.
    assert elapsed >= 0.1

    # All four seats present in the output mapping.
    assert set(out) == {"P01", "P02", "P03", "P04"}

    # Successes pass through; failures coerce to ABSTAIN.
    assert out["P01"].action.target == "P02"
    assert out["P02"].action.type is ActionType.ABSTAIN  # timeout
    assert out["P03"].action.type is ActionType.ABSTAIN  # parse fail
    assert out["P04"].action.target == "P05"

    # Both failure events recorded — in seat order.
    assert [e.body["event_type"] for e in log.events] == ["ActionTimedOut", "OutputInvalid"]
    assert [e.body["actor_player_id"] for e in log.events] == ["P02", "P03"]
    assert [e.sequence for e in log.events] == [0, 1]


# --- ranked privacy filter --------------------------------------------------


class _PoisoningAdapter:
    """Adapter that never gets called; used to assert privacy filter rejects."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(self, observation: Observation) -> AdapterResult:
        self.calls.append(observation.you.player_id)
        return _ok_result(ActionType.NOOP)


async def test_ranked_privacy_filter_blocks_dispatch_when_event_payload_carries_model_id() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1))
    log = EventLog()
    # Poison the event log with a public event carrying a forbidden key.
    log.append(
        {
            "event_type": "PublicMessageSubmitted",
            "sequence": 0,
            "phase": "DAY_1_DISCUSSION_ROUND_1",
            "visibility": "PUBLIC",
            "actor_player_id": "P01",
            "payload": {"text": "hi", "model_id": "leaked-model"},
        }
    )
    adapter = _PoisoningAdapter()
    with pytest.raises(RankedPrivacyViolation):
        await run_tick(
            state, log, [SEATS[0]], adapter, timeout_s=1.0, ruleset=mini7_v1, ranked=True
        )
    assert adapter.calls == []


async def test_ranked_false_skips_privacy_filter() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1))
    log = EventLog()
    log.append(
        {
            "event_type": "PublicMessageSubmitted",
            "sequence": 0,
            "phase": "DAY_1_DISCUSSION_ROUND_1",
            "visibility": "PUBLIC",
            "actor_player_id": "P01",
            "payload": {"text": "hi", "model_id": "leaked-model"},
        }
    )
    adapter = _ScriptedAdapter({"P01": _ok_result(ActionType.NOOP)})
    out = await run_tick(state, log, [SEATS[0]], adapter, timeout_s=1.0, ruleset=mini7_v1)
    assert out["P01"].action.type is ActionType.NOOP


# --- private memory pass-through -------------------------------------------


class _MemoryCapturingAdapter:
    def __init__(self) -> None:
        self.memories: dict[str, str] = {}

    async def complete(self, observation: Observation) -> AdapterResult:
        self.memories[observation.you.player_id] = observation.your_private_memory
        return _ok_result(ActionType.NOOP)


async def test_private_memories_are_passed_through_to_observation() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1))
    log = EventLog()
    adapter = _MemoryCapturingAdapter()
    await run_tick(
        state,
        log,
        list(SEATS[:2]),
        adapter,
        timeout_s=1.0,
        ruleset=mini7_v1,
        private_memories={"P01": "remember P03 voted late"},
    )
    assert adapter.memories["P01"] == "remember P03 voted late"
    assert adapter.memories["P02"] == ""  # default empty memory


# --- empty input edge case --------------------------------------------------


async def test_empty_eligible_seats_returns_empty_mapping() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1))
    log = EventLog()
    adapter = _ScriptedAdapter({})
    out = await run_tick(state, log, [], adapter, timeout_s=1.0, ruleset=mini7_v1)
    assert out == {}
    assert log.events == ()
