"""Provider failures end loudly: failure events, per-seat circuit breaker, per-attempt rows."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.agents.contract import AgentResponse, ResponseError
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.observations import Observation
from padrino.core.rulesets import mini7_v1
from padrino.db.repositories import games as games_repo
from padrino.db.repositories import llm_calls as llm_calls_repo
from padrino.llm.adapter import AdapterResult, ProviderNoContestError
from padrino.llm.mock import DeterministicMockAdapter
from padrino.llm.retry import LlmCallFailed
from padrino.runner.game_runner import (
    PERSISTENCE_SLACK_S,
    GameConfig,
    GamePersistence,
    _RecordingAdapter,
    drive_game_loop,
    effective_phase_timeout_s,
    run_game,
)
from padrino.settings import Settings
from tests.conftest import make_villager_script
from tests.runner.test_llm_call_cost_persistence import _observation, _result

_SEED = "seed-breaker"
_PHASES = tuple(
    ["NIGHT_0_MAFIA_INTRO"]
    + [
        pid
        for day in range(1, mini7_v1.MAX_DAYS + 1)
        for pid in (
            *(f"DAY_{day}_DISCUSSION_ROUND_{r}" for r in range(1, 4)),
            f"DAY_{day}_VOTE",
            f"NIGHT_{day}_MAFIA_DISCUSSION",
            f"NIGHT_{day}_ACTIONS",
        )
    ]
)


def _failure(reason: str, response: AgentResponse) -> AdapterResult:
    return AdapterResult(
        raw_response="",
        parsed_response=response,
        latency_ms=1,
        status="exhausted",
        error="APIConnectionError: down",
        failure=LlmCallFailed(error_kind="APIConnectionError", error_message="down", attempts=3),
        failure_reason=reason,  # type: ignore[arg-type]
    )


class _FlakySeatAdapter:
    """Passive villagers, except ``bad_seat`` fails whenever ``fails(call_no)`` says so."""

    def __init__(self, bad_seat: str, fails: Callable[[int], bool]) -> None:
        seats = [s.public_player_id for s in assign_roles(_SEED, mini7_v1)]
        self._inner = DeterministicMockAdapter(make_villager_script(seats, _PHASES))
        self._bad = bad_seat
        self._fails = fails
        self.bad_calls = 0

    async def complete(self, observation: Observation) -> AdapterResult:
        result = await self._inner.complete(observation)
        if observation.you.player_id != self._bad:
            return result
        self.bad_calls += 1
        if self._fails(self.bad_calls):
            assert isinstance(result.parsed_response, AgentResponse)
            return _failure("TIMEOUT", result.parsed_response)
        return result


def _config() -> GameConfig:
    return GameConfig(game_id="G-BREAKER", game_seed=_SEED, timeout_s=1.0)


def _limit(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    monkeypatch.setattr(
        "padrino.runner.game_runner.get_settings",
        lambda: Settings(padrino_llm_seat_failure_limit=limit),
    )


async def test_always_failing_seat_trips_the_breaker_at_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _limit(monkeypatch, 3)
    adapter = _FlakySeatAdapter("P01", lambda _n: True)

    with pytest.raises(ProviderNoContestError) as raised:
        await run_game(_config(), adapter, ranked=False)

    assert raised.value.reason == "seat_unresponsive"
    assert raised.value.seat == "P01"
    assert adapter.bad_calls == 3  # stopped at K, did not play on


async def test_a_good_reply_resets_the_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, 3)
    # fail, fail, ok, fail, fail, ok ... never three in a row
    adapter = _FlakySeatAdapter("P01", lambda n: n % 3 != 0)
    outcome = await run_game(_config(), adapter, ranked=False)
    assert outcome.final_state.terminal_result is not None
    timed_out = [
        s.body
        for s in outcome.event_log.events
        if s.body["event_type"] == "ActionTimedOut" and s.body["actor_player_id"] == "P01"
    ]
    assert timed_out
    assert {b["payload"]["reason"] for b in timed_out} == {"TIMEOUT"}


async def test_limit_zero_disables_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, 0)
    adapter = _FlakySeatAdapter("P01", lambda _n: True)
    outcome = await run_game(_config(), adapter, ranked=False)
    assert outcome.final_state.terminal_result is not None
    # Every P01 turn is a visible failure event rather than a plausible-looking action.
    failures = [
        s.body
        for s in outcome.event_log.events
        if s.body["event_type"] == "ActionTimedOut" and s.body["actor_player_id"] == "P01"
    ]
    assert len(failures) == adapter.bad_calls


async def test_unparseable_replies_never_trip_the_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only provider-side failures count; a model that answers badly still plays."""
    _limit(monkeypatch, 1)

    class _Garbage(_FlakySeatAdapter):
        async def complete(self, observation: Observation) -> AdapterResult:
            result = await super().complete(observation)
            if observation.you.player_id == "P01":
                return AdapterResult(
                    raw_response="nope",
                    parsed_response=ResponseError(reason="INVALID_JSON"),
                    latency_ms=1,
                    status="invalid_json",
                )
            return result

    outcome = await run_game(_config(), _Garbage("P01", lambda _n: False), ranked=False)
    assert outcome.final_state.terminal_result is not None


async def test_truncated_replies_never_trip_the_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """finish_reason=length is an output problem, not an unresponsive seat."""
    _limit(monkeypatch, 1)

    class _Truncated(_FlakySeatAdapter):
        async def complete(self, observation: Observation) -> AdapterResult:
            result = await super().complete(observation)
            if observation.you.player_id == "P01":
                return AdapterResult(
                    raw_response="",
                    parsed_response=ResponseError(reason="INVALID_JSON", details="truncated"),
                    latency_ms=1,
                    status="invalid_json",
                    error="truncated",
                )
            return result

    outcome = await run_game(_config(), _Truncated("P01", lambda _n: False), ranked=False)
    assert outcome.final_state.terminal_result is not None
    invalid = [
        e.body
        for e in outcome.event_log.events
        if e.body["event_type"] == "OutputInvalid" and e.body["actor_player_id"] == "P01"
    ]
    assert invalid  # still visible as OutputInvalid


async def test_reply_is_recorded_even_if_deadline_cancels_during_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink: list[AdapterResult] = []
    good = _result(model_id="openai/glm-4.7", input_tokens=1, output_tokens=1, cost_usd=0.1)

    class _Inner:
        async def complete(self, observation: Observation) -> AdapterResult:
            return good

    async def slow_record(
        self: _RecordingAdapter, observation: Observation, result: AdapterResult
    ) -> tuple[AdapterResult, bool]:
        await asyncio.sleep(0.2)
        sink.append(result)
        return result, False

    monkeypatch.setattr(_RecordingAdapter, "_record", slow_record)
    recording = _RecordingAdapter(_Inner(), [], None)
    got = await asyncio.wait_for(recording.complete(_observation()), timeout=0.05)
    assert got is good
    assert sink == [good]  # persistence ran to completion despite the 0.05s deadline


def test_effective_phase_timeout_never_shorter_than_adapter_budget() -> None:
    class _Budgeted:
        budget_s = 100.0

    class _Wrapper:
        def __init__(self, inner: object) -> None:
            self._inner = inner

    assert effective_phase_timeout_s(45.0, _Budgeted()) == 100.0 + PERSISTENCE_SLACK_S
    assert effective_phase_timeout_s(45.0, _Wrapper(_Budgeted())) == 100.0 + PERSISTENCE_SLACK_S
    assert effective_phase_timeout_s(300.0, _Budgeted()) == 300.0  # config may only raise it
    assert effective_phase_timeout_s(45.0, object()) == 45.0


async def test_game_loop_outer_deadline_follows_adapter_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []

    async def spy_tick(*args: Any) -> dict[str, AgentResponse]:
        seen.append(args[-1])
        raise RuntimeError("stop after first tick")

    class _Budgeted(_FlakySeatAdapter):
        budget_s = 20.0

    with pytest.raises(RuntimeError, match="stop after first tick"):
        await drive_game_loop(
            _config(), _Budgeted("P01", lambda _n: False), False, tick_runner=spy_tick
        )
    assert seen == [20.0 + PERSISTENCE_SLACK_S]  # config says 1.0s; the adapter needs more


async def _game_id(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        game = await games_repo.create(
            session, ruleset_id="mini7_v1", game_seed=f"s-{uuid.uuid4()}", status="RUNNING"
        )
        return game.id


async def test_recording_adapter_persists_one_row_per_attempt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id = await _game_id(session_factory)
    persistence = GamePersistence(
        session_factory=session_factory, game_id=game_id, settings=Settings()
    )
    first = _result(model_id="openai/glm-4.7", input_tokens=10, output_tokens=5, cost_usd=0.1)
    first = first.model_copy(update={"status": "invalid_json", "raw_response": "nope"})
    final = _result(
        model_id="openai/glm-4.7", input_tokens=10, output_tokens=5, cost_usd=0.1
    ).model_copy(update={"attempts": (first,)})

    class _Inner:
        async def complete(self, observation: Observation) -> AdapterResult:
            return final

    sink: list[AdapterResult] = []
    await _RecordingAdapter(_Inner(), sink, persistence).complete(_observation())

    async with session_factory() as session:
        rows = await llm_calls_repo.list_for_game(session, game_id)
    assert sorted(r.status for r in rows) == ["invalid_json", "ok"]
    assert len(sink) == 2


async def test_cancelled_call_still_leaves_a_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id = await _game_id(session_factory)
    persistence = GamePersistence(
        session_factory=session_factory, game_id=game_id, settings=Settings()
    )

    class _Hangs:
        async def complete(self, observation: Observation) -> AdapterResult:
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    sink: list[AdapterResult] = []
    recording = _RecordingAdapter(_Hangs(), sink, persistence)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(recording.complete(_observation()), timeout=0.05)

    async with session_factory() as session:
        rows = await llm_calls_repo.list_for_game(session, game_id)
    assert len(rows) == 1
    assert rows[0].status == "provider_error"
    assert rows[0].cost_usd is None
