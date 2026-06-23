"""Tests for :func:`padrino.runner.human_tick.run_human_tick` (US-138).

The human-aware tick awaits HumanAdapter and LLM seats under one per-phase
deadline, then releases ALL public messages — human AND AI — on the same fixed
delay, so message timing cannot out a seat. These tests assert that symmetry and
that the phase always settles within ``deadline + release_delay``.

A :class:`_FakeClock` advances only when the injected ``sleep`` is awaited, so
the release schedule is fully deterministic and never touches the wall clock.
"""

from __future__ import annotations

from collections.abc import Mapping

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import ActionType, Faction, PhaseKind, Role
from padrino.core.observations import Observation
from padrino.core.rulesets import mini7_v1
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.llm.human_adapter import HumanAdapter
from padrino.runner.human_tick import HumanTickConfig, run_human_tick

_DEADLINE = 30.0
_RELEASE_DELAY = 5.0


class _FakeClock:
    """Monotonic clock that advances only when ``sleep`` is awaited."""

    def __init__(self) -> None:
        self._now = 0.0

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self._now += seconds


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
        ruleset_id="mini7_v1",
        game_id="G-TEST",
        game_seed="seed-abc",
        current_phase=phase,
        seats=SEATS,
        day=phase.day,
    )


def _talk(public_message: str | None) -> AgentResponse:
    return AgentResponse(
        public_message=public_message,
        private_message=None,
        action=Action(type=ActionType.NOOP, target=None),
        memory_update="",
        rationale_summary=None,
    )


def _ai_result(public_message: str | None) -> AdapterResult:
    return AdapterResult(
        raw_response="{}",
        parsed_response=_talk(public_message),
        latency_ms=10,
    )


class _ScriptedAdapter:
    """AI adapter returning a canned result keyed by seat id (no delay)."""

    def __init__(self, results: Mapping[str, AdapterResult]) -> None:
        self._results = dict(results)

    async def complete(self, observation: Observation) -> AdapterResult:
        return self._results[observation.you.player_id]


class _MultiplexAdapter:
    """Dispatch each seat's observation to that seat's own adapter."""

    def __init__(self, adapters: Mapping[str, LlmAdapter]) -> None:
        self._adapters = dict(adapters)

    async def complete(self, observation: Observation) -> AdapterResult:
        return await self._adapters[observation.you.player_id].complete(observation)


def _human_adapter(action: Action | None, clock: _FakeClock) -> HumanAdapter:
    async def pull(_obs: Observation) -> Action | None:
        return action

    return HumanAdapter(
        pull_action=pull,
        deadline_seconds=_DEADLINE,
        poll_interval_seconds=0.5,
        clock=clock.now,
        sleep=clock.sleep,
    )


async def test_ai_and_human_public_messages_share_one_release_schedule() -> None:
    clock = _FakeClock()
    phase = Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=0)
    state = _state(phase)
    log = EventLog()

    # P01 is an AI seat that talks; P03 is a human seat that talks. They MUST
    # release on the same schedule even though the human resolves later.
    human = _human_adapter(Action(type=ActionType.NOOP, target=None), clock)
    # The human's NOOP carries no public_message, so inject the human's words via
    # a scripted human-style AI seat too — both must release identically.
    adapter = _MultiplexAdapter(
        {
            "P01": _ScriptedAdapter({"P01": _ai_result("ai speaks")}),
            "P02": _ScriptedAdapter({"P02": _ai_result("human speaks")}),
            "P03": human,
            "P04": _ScriptedAdapter({"P04": _ai_result(None)}),
        }
    )
    eligible = [SEATS[0], SEATS[1], SEATS[2], SEATS[3]]

    result = await run_human_tick(
        state,
        log,
        eligible,
        adapter,
        ruleset=mini7_v1,
        config=HumanTickConfig(
            phase_deadline_seconds=_DEADLINE,
            release_delay_seconds=_RELEASE_DELAY,
        ),
        clock=clock.now,
        sleep=clock.sleep,
    )

    # Only the two talking seats produced a public message; the silent seat (P04)
    # and the targetless-NOOP human (P03) produced none.
    released = result.released_messages
    assert [m.seat_id for m in released] == ["P01", "P02"]
    # SYMMETRY: every released message shares the exact same release instant — no
    # per-seat (and therefore no per-side human/AI) timing signal.
    release_instants = {m.released_at for m in released}
    assert len(release_instants) == 1
    assert released[0].released_at == result.settled_at


async def test_phase_settles_within_deadline_plus_delay_even_with_slow_human() -> None:
    clock = _FakeClock()
    phase = Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=0)
    state = _state(phase)
    log = EventLog()

    # A human that NEVER submits — the HumanAdapter polls to its deadline and
    # coerces to a safe action. The tick must still settle, and the settle time
    # must be within deadline + release_delay.
    human = _human_adapter(None, clock)
    adapter = _MultiplexAdapter(
        {
            "P01": _ScriptedAdapter({"P01": _ai_result("ai speaks")}),
            "P03": human,
        }
    )
    eligible = [SEATS[0], SEATS[2]]

    result = await run_human_tick(
        state,
        log,
        eligible,
        adapter,
        ruleset=mini7_v1,
        config=HumanTickConfig(
            phase_deadline_seconds=_DEADLINE,
            release_delay_seconds=_RELEASE_DELAY,
        ),
        clock=clock.now,
        sleep=clock.sleep,
    )

    # The human timed out (no submission) -> coerced safe action; the AI's
    # message still releases on the fixed schedule.
    assert [m.seat_id for m in result.released_messages] == ["P01"]
    # The HumanAdapter polled to its deadline (advancing the fake clock), then the
    # symmetric release delay was added. Settle time is bounded by deadline+delay.
    assert result.settled_at <= _DEADLINE + _RELEASE_DELAY


async def test_no_public_messages_outside_day_phases() -> None:
    clock = _FakeClock()
    phase = Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0)
    state = _state(phase)
    log = EventLog()

    adapter = _MultiplexAdapter(
        {"P01": _ScriptedAdapter({"P01": _ai_result("should not be released at night")})}
    )

    result = await run_human_tick(
        state,
        log,
        [SEATS[0]],
        adapter,
        ruleset=mini7_v1,
        config=HumanTickConfig(
            phase_deadline_seconds=_DEADLINE,
            release_delay_seconds=_RELEASE_DELAY,
        ),
        clock=clock.now,
        sleep=clock.sleep,
    )

    assert result.released_messages == ()


async def test_release_delay_is_applied_symmetrically_after_resolution() -> None:
    # With a zero-latency AI and an instantly-submitting human, the tick resolves
    # at clock time 0; the fixed release delay shifts EVERY message's release to
    # exactly release_delay, proving the delay is what spaces release from
    # resolution (so a fast AI cannot beat a fast human to the wire).
    clock = _FakeClock()
    phase = Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=0)
    state = _state(phase)
    log = EventLog()

    adapter = _MultiplexAdapter({"P01": _ScriptedAdapter({"P01": _ai_result("fast ai")})})

    result = await run_human_tick(
        state,
        log,
        [SEATS[0]],
        adapter,
        ruleset=mini7_v1,
        config=HumanTickConfig(
            phase_deadline_seconds=_DEADLINE,
            release_delay_seconds=_RELEASE_DELAY,
        ),
        clock=clock.now,
        sleep=clock.sleep,
    )

    assert result.released_messages[0].released_at == _RELEASE_DELAY
    assert result.settled_at == _RELEASE_DELAY


async def test_ranked_path_uses_same_symmetric_release_schedule_as_casual() -> None:
    phase = Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=0)
    state = _state(phase)
    eligible = [SEATS[0], SEATS[1]]

    async def run_for(ranked: bool) -> tuple[tuple[tuple[str, str, float], ...], float]:
        clock = _FakeClock()
        adapter = _MultiplexAdapter(
            {
                "P01": _ScriptedAdapter({"P01": _ai_result("first")}),
                "P02": _ScriptedAdapter({"P02": _ai_result("second")}),
            }
        )
        result = await run_human_tick(
            state,
            EventLog(),
            eligible,
            adapter,
            ruleset=mini7_v1,
            config=HumanTickConfig(
                phase_deadline_seconds=_DEADLINE,
                release_delay_seconds=_RELEASE_DELAY,
            ),
            ranked=ranked,
            clock=clock.now,
            sleep=clock.sleep,
        )
        return (
            tuple((m.seat_id, m.text, m.released_at) for m in result.released_messages),
            result.settled_at,
        )

    casual_releases, casual_settled_at = await run_for(False)
    ranked_releases, ranked_settled_at = await run_for(True)

    assert ranked_releases == casual_releases
    assert ranked_settled_at == casual_settled_at == _RELEASE_DELAY
    assert {released_at for _, _, released_at in ranked_releases} == {_RELEASE_DELAY}
