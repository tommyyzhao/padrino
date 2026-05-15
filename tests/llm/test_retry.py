"""Tests for :mod:`padrino.llm.retry` — bounded exponential backoff + dead-letter.

US-053. The helper is provider-agnostic: it accepts an async callable and a
:class:`RetryPolicy` plus an injectable sleeper / RNG so tests pin both time
and jitter deterministically. Exhaustion raises :class:`RetryExhausted`; the
adapter layer translates that into an ``exhausted``-status :class:`AdapterResult`
and ``tick.py`` converts it into a single ``ActionTimedOut`` event.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from litellm.exceptions import (
    APIConnectionError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)
from structlog.testing import LogCapture

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.rng import SeededRng
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import ActionType, Faction, PhaseKind, Role
from padrino.core.observations import Observation, build_observation
from padrino.core.rulesets import mini7_v1
from padrino.llm.adapter import AgentBuild, RoutingPolicy
from padrino.llm.litellm_adapter import LiteLlmAdapter
from padrino.llm.retry import (
    DEFAULT_RETRY_ON,
    LlmCallFailed,
    RetryAttempt,
    RetryExhausted,
    RetryPolicy,
    default_retry_policy,
    with_retry,
)

_AUTH_SECRET_ENV = "PADRINO_TEST_LITELLM_KEY"
ACOMPLETION_PATH = "padrino.llm.litellm_adapter.litellm.acompletion"


@pytest.fixture(autouse=True)
def _set_auth_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_AUTH_SECRET_ENV, "test-key-value")


def _seat(pid: str, idx: int, role: Role, faction: Faction) -> Seat:
    return Seat(
        public_player_id=pid,
        seat_index=idx,
        role=role,
        faction=faction,
        alive=True,
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


def _state(phase: Phase) -> GameState:
    return GameState(
        ruleset_id=mini7_v1.RULESET_ID,
        game_id="G-MOCK",
        game_seed="seed",
        current_phase=phase,
        seats=SEATS,
        day=phase.day,
    )


def _observation(seat: Seat, phase: Phase) -> Observation:
    return build_observation(_state(phase), seat, EventLog(), mini7_v1)


def _valid_response_json(action: Action) -> str:
    return json.dumps(
        {
            "public_message": None,
            "private_message": None,
            "action": {"type": action.type.value, "target": action.target},
            "memory_update": "",
            "rationale_summary": None,
        }
    )


def _fake_completion(content: str) -> SimpleNamespace:
    choices = [SimpleNamespace(message=SimpleNamespace(content=content))]
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=10)
    return SimpleNamespace(choices=choices, usage=usage, id="resp-1")


# --- with_retry unit tests --------------------------------------------------


def test_retry_policy_defaults_cover_litellm_classes() -> None:
    policy = default_retry_policy()
    assert RateLimitError in policy.retry_on
    assert APIConnectionError in policy.retry_on
    assert InternalServerError in policy.retry_on
    assert TimeoutError in policy.retry_on
    # Non-retryable errors must NOT appear.
    assert BadRequestError not in policy.retry_on
    assert AuthenticationError not in policy.retry_on


def test_retry_on_default_export_is_authoritative_tuple() -> None:
    assert isinstance(DEFAULT_RETRY_ON, tuple)
    assert AuthenticationError not in DEFAULT_RETRY_ON


def test_with_retry_success_on_first_attempt() -> None:
    call = AsyncMock(return_value="ok")
    sleeper = AsyncMock()
    rng = SeededRng("call-1")
    policy = RetryPolicy(
        max_attempts=3,
        base_delay_s=0.1,
        max_delay_s=1.0,
        retry_on=(TimeoutError,),
    )
    result, attempts = asyncio.run(with_retry(call, policy, sleeper=sleeper, rng=rng))
    assert result == "ok"
    assert attempts == []
    sleeper.assert_not_awaited()
    assert call.call_count == 1


def test_with_retry_success_on_retry() -> None:
    call = AsyncMock(side_effect=[TimeoutError("boom"), "ok"])
    sleeper = AsyncMock()
    rng = SeededRng("call-2")
    policy = RetryPolicy(
        max_attempts=3,
        base_delay_s=0.05,
        max_delay_s=0.5,
        retry_on=(TimeoutError,),
    )
    result, attempts = asyncio.run(with_retry(call, policy, sleeper=sleeper, rng=rng))
    assert result == "ok"
    assert len(attempts) == 1
    assert attempts[0].attempt_number == 1
    assert attempts[0].error_kind == "TimeoutError"
    assert attempts[0].error_message == "boom"
    assert attempts[0].delay_ms >= 0
    sleeper.assert_awaited_once()
    # Sleeper delay matches the recorded delay_ms.
    awaited_delay = sleeper.await_args_list[0].args[0]
    assert int(awaited_delay * 1000) == attempts[0].delay_ms


def test_with_retry_exhaustion_raises_with_history() -> None:
    err = TimeoutError("nope")
    call = AsyncMock(side_effect=err)
    sleeper = AsyncMock()
    rng = SeededRng("call-3")
    policy = RetryPolicy(
        max_attempts=3,
        base_delay_s=0.01,
        max_delay_s=0.1,
        retry_on=(TimeoutError,),
    )
    with pytest.raises(RetryExhausted) as info:
        asyncio.run(with_retry(call, policy, sleeper=sleeper, rng=rng))
    assert info.value.attempts == 3
    assert info.value.last_error is err
    assert info.value.error_kind == "TimeoutError"
    assert info.value.error_message == "nope"
    # On exhaustion we sleep between attempts but NOT after the last failure.
    assert sleeper.await_count == 2
    assert call.call_count == 3


def test_with_retry_non_retryable_short_circuits() -> None:
    err = BadRequestError(message="bad", model="m", llm_provider="openai", response=None)
    call = AsyncMock(side_effect=err)
    sleeper = AsyncMock()
    rng = SeededRng("call-4")
    policy = default_retry_policy()
    with pytest.raises(BadRequestError):
        asyncio.run(with_retry(call, policy, sleeper=sleeper, rng=rng))
    assert call.call_count == 1
    sleeper.assert_not_awaited()


def test_with_retry_jitter_is_deterministic_given_seed() -> None:
    err = TimeoutError("x")
    policy = RetryPolicy(
        max_attempts=4,
        base_delay_s=0.1,
        max_delay_s=10.0,
        retry_on=(TimeoutError,),
    )

    async def run_once() -> list[float]:
        call = AsyncMock(side_effect=err)
        sleeper = AsyncMock()
        rng = SeededRng("stable-call-id")
        with contextlib.suppress(RetryExhausted):
            await with_retry(call, policy, sleeper=sleeper, rng=rng)
        return [args.args[0] for args in sleeper.await_args_list]

    first = asyncio.run(run_once())
    second = asyncio.run(run_once())
    assert first == second
    # Successive delays grow but are bounded by max_delay_s.
    assert all(d <= policy.max_delay_s for d in first)
    # Jitter should not collapse to zero.
    assert any(d > 0 for d in first)


def test_with_retry_caps_delay_at_max() -> None:
    err = TimeoutError("x")
    policy = RetryPolicy(
        max_attempts=10,
        base_delay_s=1.0,
        max_delay_s=2.0,
        retry_on=(TimeoutError,),
    )

    async def go() -> list[float]:
        call = AsyncMock(side_effect=err)
        sleeper = AsyncMock()
        rng = SeededRng("cap")
        with pytest.raises(RetryExhausted):
            await with_retry(call, policy, sleeper=sleeper, rng=rng)
        return [a.args[0] for a in sleeper.await_args_list]

    delays = asyncio.run(go())
    # Once exponential exceeds max_delay_s the cap binds.
    assert all(d <= policy.max_delay_s + 1e-9 for d in delays)


def test_with_retry_emits_structured_log_event() -> None:
    cap = LogCapture()
    structlog.configure(processors=[cap])
    err = TimeoutError("flap")
    call = AsyncMock(side_effect=[err, "ok"])
    sleeper = AsyncMock()
    rng = SeededRng("logged-call")
    policy = RetryPolicy(
        max_attempts=3,
        base_delay_s=0.01,
        max_delay_s=0.1,
        retry_on=(TimeoutError,),
    )
    try:
        asyncio.run(with_retry(call, policy, sleeper=sleeper, rng=rng))
    finally:
        structlog.reset_defaults()
    retry_entries = [e for e in cap.entries if e.get("event") == "llm.call.retry"]
    assert len(retry_entries) == 1
    entry = retry_entries[0]
    assert entry["attempt_number"] == 1
    assert entry["error_kind"] == "TimeoutError"
    assert entry["delay_ms"] >= 0


def test_with_retry_invalid_max_attempts() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(
            max_attempts=0,
            base_delay_s=0.1,
            max_delay_s=1.0,
            retry_on=(TimeoutError,),
        )


# --- LlmCallFailed structured outcome --------------------------------------


def test_llm_call_failed_carries_error_chain() -> None:
    failure = LlmCallFailed(
        error_kind="RateLimitError",
        error_message="too fast",
        attempts=3,
    )
    assert failure.error_kind == "RateLimitError"
    assert failure.error_message == "too fast"
    assert failure.attempts == 3


# --- LiteLlmAdapter integration --------------------------------------------


def _build_adapter(*, retry_policy: RetryPolicy | None = None) -> LiteLlmAdapter:
    policy = RoutingPolicy(
        primary_model="cerebras/zai-glm-4.7",
        fallback_model=None,
    )
    build = AgentBuild(
        provider="cerebras",
        model_id="zai-glm-4.7",
        prompt_version="prompt_classic15_v1_001",
        inference_params={},
        adapter_version="litellm-1",
    )
    return LiteLlmAdapter(
        routing_policy=policy,
        agent_build=build,
        timeout_s=5.0,
        auth_secret_ref=f"env:{_AUTH_SECRET_ENV}",
        retry_policy=retry_policy
        if retry_policy is not None
        else RetryPolicy(
            max_attempts=3,
            base_delay_s=0.0,
            max_delay_s=0.0,
            retry_on=(TimeoutError, RateLimitError),
        ),
        sleeper=AsyncMock(),
    )


def test_adapter_retries_on_retryable_error_then_succeeds() -> None:
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))
    response = _fake_completion(_valid_response_json(Action(type=ActionType.ABSTAIN)))
    side: list[Any] = [TimeoutError("flap"), response]
    mock = AsyncMock(side_effect=side)
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter()
        result = asyncio.run(adapter.complete(obs))
    assert mock.call_count == 2
    assert result.status == "ok"
    assert isinstance(result.parsed_response, AgentResponse)


def test_adapter_exhaustion_yields_llm_call_failed() -> None:
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))
    mock = AsyncMock(side_effect=TimeoutError("forever"))
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter()
        result = asyncio.run(adapter.complete(obs))
    assert mock.call_count == 3
    assert result.status == "exhausted"
    assert result.failure is not None
    assert result.failure.error_kind == "TimeoutError"
    assert "forever" in result.failure.error_message
    assert result.failure.attempts == 3
    # Coerced safe response remains the parsed_response for the runner downstream.
    assert isinstance(result.parsed_response, AgentResponse)
    assert result.error is not None
    assert "TimeoutError" in result.error


def test_adapter_non_retryable_skips_retry() -> None:
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))
    err = AuthenticationError(message="nope", model="m", llm_provider="openai")
    mock = AsyncMock(side_effect=err)
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter()
        result = asyncio.run(adapter.complete(obs))
    assert mock.call_count == 1
    # Non-retryable but no fallback: status is primary_failed and a coerced
    # safe response stands in for the parsed payload.
    assert result.status == "primary_failed"


# --- tick.py ActionTimedOut once per exhausted call ------------------------


def test_tick_records_exactly_one_action_timed_out_per_exhausted_call() -> None:
    from padrino.core.engine.event_log import EventLog as Log
    from padrino.runner.tick import run_tick

    seat = SEATS[0]
    phase = Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0)
    state = _state(phase)
    log = Log()

    obs = _observation(seat, phase)
    mock = AsyncMock(side_effect=TimeoutError("die"))
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter()
        responses = asyncio.run(
            run_tick(
                state=state,
                event_log=log,
                eligible_seats=[seat],
                adapter=adapter,
                timeout_s=30.0,
                ruleset=mini7_v1,
            )
        )

    assert mock.call_count == 3  # all retries fired inside the adapter
    timed_out_events = [e for e in log.events if e.body.get("event_type") == "ActionTimedOut"]
    assert len(timed_out_events) == 1
    payload = timed_out_events[0].body["payload"]
    assert payload["reason"] == "llm_exhausted"
    assert payload["error_kind"] == "TimeoutError"
    assert payload["attempts"] == 3
    # And the coerced response was returned to the runner.
    assert seat.public_player_id in responses
    _ = obs


# --- AST firewall ----------------------------------------------------------


def test_retry_module_does_not_import_db_or_random() -> None:
    import ast

    src = Path("src/padrino/llm/retry.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    for forbidden in ("padrino.db", "sqlalchemy", "random", "secrets"):
        for n in names:
            assert not n.startswith(forbidden), f"unexpected import {n!r}"


def test_retry_attempt_is_frozen() -> None:
    a = RetryAttempt(attempt_number=1, delay_ms=10, error_kind="X", error_message="y")
    with pytest.raises(AttributeError):
        a.attempt_number = 2  # type: ignore[misc]
