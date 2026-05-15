"""Tests for :class:`padrino.llm.litellm_adapter.LiteLlmAdapter`.

These tests stub :func:`litellm.acompletion` via ``unittest.mock.AsyncMock`` —
no real network calls are made. The integration test against live providers
lives in :mod:`tests.integration.test_real_providers` and is gated by
``@pytest.mark.integration`` (US-036).
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from padrino.core.agents.contract import AgentResponse, ResponseError
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import ActionType, Faction, PhaseKind, Role
from padrino.core.observations import Observation, build_observation
from padrino.core.rulesets import mini7_v1
from padrino.llm.adapter import AdapterResult, AgentBuild, LlmAdapter, RoutingPolicy
from padrino.llm.litellm_adapter import LiteLlmAdapter, build_messages
from padrino.llm.secrets import SecretResolutionError

ACOMPLETION_PATH = "padrino.llm.litellm_adapter.litellm.acompletion"
_AUTH_SECRET_ENV = "PADRINO_TEST_LITELLM_KEY"


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
            "public_message": "thinking out loud",
            "private_message": None,
            "action": {"type": action.type.value, "target": action.target},
            "memory_update": "",
            "rationale_summary": "no strong read yet",
        }
    )


def _fake_completion(
    *,
    content: str,
    response_id: str = "resp-1",
    prompt_tokens: int | None = 100,
    completion_tokens: int | None = 30,
    cost: float | None = 0.0001,
) -> SimpleNamespace:
    """Mimic the relevant subset of a LiteLLM ``ModelResponse``."""
    choices = [SimpleNamespace(message=SimpleNamespace(content=content))]
    usage: SimpleNamespace | None = None
    if prompt_tokens is not None or completion_tokens is not None:
        usage = SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    response = SimpleNamespace(
        choices=choices,
        usage=usage,
        id=response_id,
    )
    if cost is not None:
        response._hidden_params = {"response_cost": cost}
    return response


def _build_adapter(
    *,
    fallback: str | None = "deepinfra/deepseek-ai/DeepSeek-V4-Flash",
) -> LiteLlmAdapter:
    policy = RoutingPolicy(
        primary_model="cerebras/zai-glm-4.7",
        fallback_model=fallback,
    )
    build = AgentBuild(
        provider="cerebras",
        model_id="zai-glm-4.7",
        prompt_version="prompt_classic15_v1_001",
        inference_params={"temperature": 0.7, "top_p": 1.0},
        adapter_version="litellm-1",
    )
    return LiteLlmAdapter(
        routing_policy=policy,
        agent_build=build,
        timeout_s=5.0,
        auth_secret_ref=f"env:{_AUTH_SECRET_ENV}",
    )


def test_litellm_adapter_satisfies_protocol() -> None:
    adapter = _build_adapter()
    assert isinstance(adapter, LlmAdapter)


def test_success_returns_ok_status() -> None:
    canned_action = Action(type=ActionType.ABSTAIN, target=None)
    response = _fake_completion(content=_valid_response_json(canned_action))
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))

    with patch(ACOMPLETION_PATH, new=AsyncMock(return_value=response)) as mock_acomp:
        adapter = _build_adapter()
        result = asyncio.run(adapter.complete(obs))

    assert mock_acomp.call_count == 1
    call_kwargs = mock_acomp.call_args.kwargs
    assert call_kwargs["model"] == "cerebras/zai-glm-4.7"
    assert isinstance(result, AdapterResult)
    assert result.status == "ok"
    assert isinstance(result.parsed_response, AgentResponse)
    assert result.parsed_response.action == canned_action
    assert result.input_tokens == 100
    assert result.output_tokens == 30
    assert result.cost_usd == 0.0001
    assert result.provider_response_id == "resp-1"
    assert result.error is None


def test_success_path_records_single_attempt() -> None:
    response = _fake_completion(content=_valid_response_json(Action(type=ActionType.NOOP)))
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.NIGHT_0_MAFIA_INTRO, day=0, round=0))

    with patch(ACOMPLETION_PATH, new=AsyncMock(return_value=response)):
        adapter = _build_adapter()
        asyncio.run(adapter.complete(obs))

    assert len(adapter.last_attempts) == 1
    assert adapter.last_attempts[0].status == "ok"


def test_primary_failure_falls_back_to_secondary() -> None:
    fallback_response = _fake_completion(
        content=_valid_response_json(Action(type=ActionType.ABSTAIN)),
        response_id="resp-fallback",
        prompt_tokens=80,
        completion_tokens=20,
        cost=0.00005,
    )
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))

    mock = AsyncMock(side_effect=[RuntimeError("primary boom"), fallback_response])
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter()
        result = asyncio.run(adapter.complete(obs))

    assert mock.call_count == 2
    assert mock.call_args_list[0].kwargs["model"] == "cerebras/zai-glm-4.7"
    assert mock.call_args_list[1].kwargs["model"] == "deepinfra/deepseek-ai/DeepSeek-V4-Flash"

    assert result.status == "fallback_ok"
    assert isinstance(result.parsed_response, AgentResponse)
    assert result.provider_response_id == "resp-fallback"
    assert result.input_tokens == 80
    assert result.cost_usd == 0.00005
    assert len(adapter.last_attempts) == 2
    assert adapter.last_attempts[0].status == "primary_failed"
    assert adapter.last_attempts[0].error is not None
    assert "primary boom" in adapter.last_attempts[0].error
    assert adapter.last_attempts[1].status == "fallback_ok"


def test_both_failures_yield_coerced_safe_response_in_vote_phase() -> None:
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))
    mock = AsyncMock(
        side_effect=[
            RuntimeError("primary down"),
            TimeoutError("fallback timeout"),
        ]
    )
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter()
        result = asyncio.run(adapter.complete(obs))

    assert mock.call_count == 2
    assert result.status == "both_failed"
    assert isinstance(result.parsed_response, AgentResponse)
    assert result.parsed_response.action.type is ActionType.ABSTAIN
    assert result.parsed_response.public_message is None
    assert result.parsed_response.private_message is None
    assert result.parsed_response.memory_update == ""
    assert result.error is not None
    assert "fallback timeout" in result.error
    assert len(adapter.last_attempts) == 2
    assert adapter.last_attempts[0].status == "primary_failed"
    assert adapter.last_attempts[1].status == "both_failed"


def test_both_failures_in_night_phase_returns_noop() -> None:
    obs = _observation(SEATS[2], Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0))
    mock = AsyncMock(side_effect=[RuntimeError("a"), RuntimeError("b")])
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter()
        result = asyncio.run(adapter.complete(obs))

    assert result.status == "both_failed"
    assert isinstance(result.parsed_response, AgentResponse)
    assert result.parsed_response.action.type is ActionType.NOOP
    assert result.parsed_response.action.target is None


def test_no_fallback_configured_does_not_make_second_call() -> None:
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))
    mock = AsyncMock(side_effect=RuntimeError("kaboom"))
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter(fallback=None)
        result = asyncio.run(adapter.complete(obs))

    assert mock.call_count == 1
    assert result.status == "primary_failed"
    assert isinstance(result.parsed_response, AgentResponse)
    assert result.parsed_response.action.type is ActionType.ABSTAIN
    assert result.error is not None
    assert "kaboom" in result.error


def test_invalid_json_does_not_trigger_fallback() -> None:
    response = _fake_completion(content="this is not JSON")
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))

    mock = AsyncMock(return_value=response)
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter()
        result = asyncio.run(adapter.complete(obs))

    assert mock.call_count == 1
    assert result.status == "invalid_json"
    assert isinstance(result.parsed_response, ResponseError)
    assert result.parsed_response.reason == "INVALID_JSON"
    assert result.raw_response == "this is not JSON"


def test_schema_violation_does_not_trigger_fallback() -> None:
    bad = json.dumps({"public_message": "hi"})  # missing required fields
    response = _fake_completion(content=bad)
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))

    mock = AsyncMock(return_value=response)
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter()
        result = asyncio.run(adapter.complete(obs))

    assert mock.call_count == 1
    assert result.status == "schema_violation"
    assert isinstance(result.parsed_response, ResponseError)
    assert result.parsed_response.reason == "SCHEMA_VIOLATION"


def test_inference_params_forwarded_to_litellm() -> None:
    response = _fake_completion(content=_valid_response_json(Action(type=ActionType.NOOP)))
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.NIGHT_0_MAFIA_INTRO, day=0, round=0))

    mock = AsyncMock(return_value=response)
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter()
        asyncio.run(adapter.complete(obs))

    kwargs = mock.call_args.kwargs
    assert kwargs["temperature"] == 0.7
    assert kwargs["top_p"] == 1.0
    assert kwargs["timeout"] == 5.0
    messages = kwargs["messages"]
    assert isinstance(messages, list)
    assert messages[0]["role"] == "system"
    assert messages[-1]["role"] == "user"


def test_build_messages_serializes_observation_as_user_payload() -> None:
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))
    messages = build_messages(obs, system_prompt="SYS")

    assert messages[0] == {"role": "system", "content": "SYS"}
    assert messages[-1]["role"] == "user"
    parsed: dict[str, Any] = json.loads(messages[-1]["content"])
    assert parsed["phase"] == "DAY_1_VOTE"
    assert parsed["you"]["player_id"] == "P01"


def test_missing_usage_metadata_yields_none_token_counts() -> None:
    response = _fake_completion(
        content=_valid_response_json(Action(type=ActionType.NOOP)),
        prompt_tokens=None,
        completion_tokens=None,
        cost=None,
    )
    response.usage = None  # ensure attribute exists but is None
    if hasattr(response, "_hidden_params"):
        delattr(response, "_hidden_params")
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.NIGHT_0_MAFIA_INTRO, day=0, round=0))

    with patch(ACOMPLETION_PATH, new=AsyncMock(return_value=response)):
        adapter = _build_adapter()
        result = asyncio.run(adapter.complete(obs))

    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.cost_usd is None


def test_constructor_resolves_auth_secret_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter must resolve ``auth_secret_ref`` at construction, not per call."""
    monkeypatch.setenv("PADRINO_ROTATING_SECRET", "first-value")
    policy = RoutingPolicy(primary_model="x", fallback_model=None)
    build = AgentBuild(
        provider="p",
        model_id="m",
        prompt_version="pv",
        inference_params={},
        adapter_version="a",
    )
    adapter = LiteLlmAdapter(
        routing_policy=policy,
        agent_build=build,
        timeout_s=1.0,
        auth_secret_ref="env:PADRINO_ROTATING_SECRET",
    )
    # Rotating the env after construction must not change the cached value.
    monkeypatch.setenv("PADRINO_ROTATING_SECRET", "second-value")
    assert adapter._auth_secret == "first-value"


def test_constructor_fails_loudly_on_unresolvable_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PADRINO_NEVER_SET", raising=False)
    policy = RoutingPolicy(primary_model="x", fallback_model=None)
    build = AgentBuild(
        provider="p",
        model_id="m",
        prompt_version="pv",
        inference_params={},
        adapter_version="a",
    )
    with pytest.raises(SecretResolutionError):
        LiteLlmAdapter(
            routing_policy=policy,
            agent_build=build,
            timeout_s=1.0,
            auth_secret_ref="env:PADRINO_NEVER_SET",
        )


def test_complete_does_not_reread_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """``resolve_secret`` is called exactly once at construction, never per call."""
    monkeypatch.setenv("PADRINO_COUNT_SECRET", "value")
    call_count = {"n": 0}
    import padrino.llm.litellm_adapter as litellm_mod
    from padrino.llm import secrets as secrets_mod

    real_resolve = secrets_mod.resolve_secret

    def counting_resolve(ref: str) -> str:
        call_count["n"] += 1
        return real_resolve(ref)

    monkeypatch.setattr(litellm_mod, "resolve_secret", counting_resolve)

    policy = RoutingPolicy(primary_model="m", fallback_model=None)
    build = AgentBuild(
        provider="p",
        model_id="m",
        prompt_version="pv",
        inference_params={},
        adapter_version="a",
    )
    response = _fake_completion(content=_valid_response_json(Action(type=ActionType.NOOP)))
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.NIGHT_0_MAFIA_INTRO, day=0, round=0))

    with patch(ACOMPLETION_PATH, new=AsyncMock(return_value=response)):
        adapter = LiteLlmAdapter(
            routing_policy=policy,
            agent_build=build,
            timeout_s=1.0,
            auth_secret_ref="env:PADRINO_COUNT_SECRET",
        )
        # Construction reads exactly once.
        assert call_count["n"] == 1
        asyncio.run(adapter.complete(obs))
        asyncio.run(adapter.complete(obs))

    # Subsequent calls never re-read the secret.
    assert call_count["n"] == 1


def test_litellm_adapter_does_not_import_db_or_random() -> None:
    source = Path("src/padrino/llm/litellm_adapter.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            module_names.add(node.module)
    for forbidden in ("padrino.db", "sqlalchemy", "random", "secrets"):
        for mod in module_names:
            assert not mod.startswith(forbidden), f"unexpected import {mod!r}"


def test_complete_propagates_attempts_through_repeated_calls() -> None:
    obs1 = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))
    obs2 = _observation(SEATS[0], Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0))

    response = _fake_completion(content=_valid_response_json(Action(type=ActionType.NOOP)))

    async def run_two(adapter: LiteLlmAdapter) -> tuple[AdapterResult, AdapterResult]:
        first = await adapter.complete(obs1)
        second = await adapter.complete(obs2)
        return first, second

    mock = AsyncMock(side_effect=[RuntimeError("p1"), response, response])
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter()
        first, second = asyncio.run(run_two(adapter))

    # After the second call, last_attempts should reflect the second call only.
    assert first.status == "fallback_ok"
    assert second.status == "ok"
    assert len(adapter.last_attempts) == 1
    assert adapter.last_attempts[0].status == "ok"


@pytest.mark.parametrize(
    "phase",
    [
        Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1),
        Phase(kind=PhaseKind.NIGHT_MAFIA_DISCUSSION, day=1, round=0),
        Phase(kind=PhaseKind.NIGHT_ACTIONS, day=2, round=0),
    ],
)
def test_non_vote_phases_coerce_to_noop_on_both_failures(phase: Phase) -> None:
    obs = _observation(SEATS[0], phase)
    mock = AsyncMock(side_effect=[RuntimeError("a"), RuntimeError("b")])
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter()
        result = asyncio.run(adapter.complete(obs))
    assert result.status == "both_failed"
    assert isinstance(result.parsed_response, AgentResponse)
    assert result.parsed_response.action.type is ActionType.NOOP
