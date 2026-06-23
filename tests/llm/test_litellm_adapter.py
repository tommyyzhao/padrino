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
from padrino.llm.adapter import (
    AdapterResult,
    AgentBuild,
    LlmAdapter,
    RoutingPolicy,
    SameModelHost,
)
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


_SAME_MODEL_HOST_ENV = "PADRINO_TEST_ZAI_KEY"


def _build_adapter(
    *,
    fallback: str | None = "deepinfra/deepseek-ai/DeepSeek-V4-Flash",
    same_model_hosts: tuple[SameModelHost, ...] = (),
    api_base: str | None = None,
) -> LiteLlmAdapter:
    policy = RoutingPolicy(
        primary_model="cerebras/zai-glm-4.7",
        fallback_model=fallback,
        same_model_hosts=same_model_hosts,
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
        api_base=api_base,
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
    assert result.model_id == "cerebras/zai-glm-4.7"
    assert result.provider_response_id == "resp-1"
    assert result.error is None


def test_primary_api_base_forwards_endpoint_and_cached_secret() -> None:
    canned_action = Action(type=ActionType.ABSTAIN, target=None)
    response = _fake_completion(content=_valid_response_json(canned_action))
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))

    with patch(ACOMPLETION_PATH, new=AsyncMock(return_value=response)) as mock_acomp:
        adapter = _build_adapter(api_base="https://token-plan-sgp.xiaomimimo.com/v1")
        result = asyncio.run(adapter.complete(obs))

    assert result.status == "ok"
    call_kwargs = mock_acomp.call_args.kwargs
    assert call_kwargs["api_base"] == "https://token-plan-sgp.xiaomimimo.com/v1"
    assert call_kwargs["api_key"] == "test-key-value"


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
    assert result.model_id == "deepinfra/deepseek-ai/DeepSeek-V4-Flash"
    assert len(adapter.last_attempts) == 2
    assert adapter.last_attempts[0].status == "primary_failed"
    assert adapter.last_attempts[0].error is not None
    assert "primary boom" in adapter.last_attempts[0].error
    assert adapter.last_attempts[1].status == "fallback_ok"


def test_both_failures_yield_coerced_safe_response_in_vote_phase() -> None:
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))
    # Both errors are RuntimeError so neither is retryable under the default
    # policy — exhaustion would only fire on litellm.RateLimitError / Timeout
    # and produces the new ``exhausted`` status (covered in test_retry.py).
    mock = AsyncMock(
        side_effect=[
            RuntimeError("primary down"),
            RuntimeError("fallback down"),
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
    assert "fallback down" in result.error
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


@pytest.mark.parametrize(
    "fenced_prefix,fenced_suffix",
    [
        ("```json\n", "\n```"),
        ("```JSON\n", "\n```"),
        ("```\n", "\n```"),
        ("  ```json\n", "\n```  "),  # surrounding whitespace
    ],
)
def test_markdown_code_fence_is_stripped_before_parse(
    fenced_prefix: str, fenced_suffix: str
) -> None:
    canned_action = Action(type=ActionType.NOOP)
    inner = _valid_response_json(canned_action)
    fenced = f"{fenced_prefix}{inner}{fenced_suffix}"
    response = _fake_completion(content=fenced)
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.NIGHT_0_MAFIA_INTRO, day=0, round=0))

    with patch(ACOMPLETION_PATH, new=AsyncMock(return_value=response)):
        adapter = _build_adapter()
        result = asyncio.run(adapter.complete(obs))

    assert result.status == "ok", (
        f"expected fenced JSON to parse cleanly; got status={result.status} "
        f"raw={result.raw_response!r}"
    )
    assert isinstance(result.parsed_response, AgentResponse)
    assert result.parsed_response.action == canned_action


def test_unfenced_text_is_passed_through_unchanged() -> None:
    response = _fake_completion(content="this is not JSON")
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))

    with patch(ACOMPLETION_PATH, new=AsyncMock(return_value=response)):
        adapter = _build_adapter()
        result = asyncio.run(adapter.complete(obs))

    assert result.status == "invalid_json"
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
    assert result.model_id == "cerebras/zai-glm-4.7"


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


# ---------------------------------------------------------------------------
# US-079: same-model multi-host fallback
# ---------------------------------------------------------------------------


@pytest.fixture
def _set_same_model_host_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SAME_MODEL_HOST_ENV, "zai-key-value")


def _zai_glm47_host() -> SameModelHost:
    return SameModelHost(
        provider="zai",
        litellm_model_id="openai/glm-4.7",
        api_base="https://api.z.ai/api/paas/v4/",
        auth_secret_ref=f"env:{_SAME_MODEL_HOST_ENV}",
    )


def _rate_limit_error(model: str = "cerebras/zai-glm-4.7") -> Exception:
    from litellm.exceptions import RateLimitError

    return RateLimitError(message="429 from primary", llm_provider="cerebras", model=model)


def test_same_model_fallback_routes_to_alternate_host(
    _set_same_model_host_env: None,
) -> None:
    """Primary 429 routes to the same-model alternate host; different-model fallback untouched."""
    fallback_response = _fake_completion(
        content=_valid_response_json(Action(type=ActionType.ABSTAIN)),
        response_id="resp-zai",
    )
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))

    # Primary raises three times (the default retry policy yields three
    # attempts, so RateLimitError exhausts the primary retries) before the
    # same-model host wins on its first attempt.
    side_effects: list[Any] = [_rate_limit_error() for _ in range(3)]
    side_effects.append(fallback_response)

    mock = AsyncMock(side_effect=side_effects)
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter(same_model_hosts=(_zai_glm47_host(),))
        result = asyncio.run(adapter.complete(obs))

    # Three primary attempts + one same-model host attempt; NEVER touches the
    # different-model fallback ("deepinfra/...").
    assert mock.call_count == 4
    models_called = [c.kwargs["model"] for c in mock.call_args_list]
    assert models_called[:3] == ["cerebras/zai-glm-4.7"] * 3
    assert models_called[3] == "openai/glm-4.7"
    # The same-model attempt receives the Z.AI api_base and credential
    # explicitly (the primary call sent neither — its provider defaults apply).
    primary_call_kwargs = mock.call_args_list[0].kwargs
    assert "api_base" not in primary_call_kwargs
    assert "api_key" not in primary_call_kwargs
    same_model_kwargs = mock.call_args_list[3].kwargs
    assert same_model_kwargs["api_base"] == "https://api.z.ai/api/paas/v4/"
    assert same_model_kwargs["api_key"] == "zai-key-value"

    assert result.status == "same_model_fallback_ok"
    assert isinstance(result.parsed_response, AgentResponse)
    assert result.provider_response_id == "resp-zai"
    # last_attempts: primary attempt (demoted to primary_failed) + the
    # successful same-model attempt.
    assert len(adapter.last_attempts) == 2
    assert adapter.last_attempts[0].status == "primary_failed"
    assert adapter.last_attempts[1].status == "same_model_fallback_ok"


def test_same_model_exhaustion_still_triggers_different_model_fallback(
    _set_same_model_host_env: None,
) -> None:
    """Primary + every same-model host fail; fall through to the different-model fallback."""
    fallback_response = _fake_completion(
        content=_valid_response_json(Action(type=ActionType.ABSTAIN)),
        response_id="resp-deepinfra",
    )
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))

    side_effects: list[Any] = []
    side_effects.extend(_rate_limit_error() for _ in range(3))  # primary exhausts
    side_effects.extend(_rate_limit_error("openai/glm-4.7") for _ in range(3))  # zai exhausts
    side_effects.append(fallback_response)

    mock = AsyncMock(side_effect=side_effects)
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter(same_model_hosts=(_zai_glm47_host(),))
        result = asyncio.run(adapter.complete(obs))

    assert mock.call_count == 7
    models_called = [c.kwargs["model"] for c in mock.call_args_list]
    assert models_called[:3] == ["cerebras/zai-glm-4.7"] * 3
    assert models_called[3:6] == ["openai/glm-4.7"] * 3
    assert models_called[6] == "deepinfra/deepseek-ai/DeepSeek-V4-Flash"

    assert result.status == "fallback_ok"
    assert isinstance(result.parsed_response, AgentResponse)
    assert result.provider_response_id == "resp-deepinfra"
    statuses = [a.status for a in adapter.last_attempts]
    assert statuses == ["primary_failed", "primary_failed", "fallback_ok"]


def test_same_model_host_resolves_secret_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured same-model host fails loudly at boot, not on first call."""
    monkeypatch.delenv(_SAME_MODEL_HOST_ENV, raising=False)
    with pytest.raises(SecretResolutionError):
        _build_adapter(same_model_hosts=(_zai_glm47_host(),))


def test_same_model_fallback_without_different_model_fallback_coerces(
    _set_same_model_host_env: None,
) -> None:
    """No different-model fallback; same-model exhaustion coerces to a safe action."""
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))
    side_effects: list[Any] = []
    side_effects.extend(_rate_limit_error() for _ in range(3))
    side_effects.extend(_rate_limit_error("openai/glm-4.7") for _ in range(3))
    mock = AsyncMock(side_effect=side_effects)
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter(
            fallback=None,
            same_model_hosts=(_zai_glm47_host(),),
        )
        result = asyncio.run(adapter.complete(obs))

    assert mock.call_count == 6
    assert result.status == "exhausted"
    assert isinstance(result.parsed_response, AgentResponse)
    assert result.parsed_response.action.type is ActionType.ABSTAIN
