"""Explicitly funded OpenRouter adapter turns.

This suite never records cassettes. It runs only with the integration and
live_llm markers selected, requires an operator-provided API key, and
preflights a conservative whole-suite cost ceiling before making any request.
"""

from __future__ import annotations

import os
from decimal import ROUND_CEILING, Decimal

import pytest

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.legal_actions import LegalActions
from padrino.core.enums import ActionType, Faction, Role
from padrino.core.observations import MessageLimits, Observation, YouInfo
from padrino.llm.adapter import (
    AgentBuild,
    OpenRouterMaxPrice,
    OpenRouterProviderPreferences,
    OpenRouterRequestPolicy,
    RoutingPolicy,
)
from padrino.llm.litellm_adapter import LiteLlmAdapter
from padrino.llm.retry import RetryPolicy

pytestmark = [pytest.mark.integration, pytest.mark.live_llm]

_USD_MICROS = Decimal(1_000_000)
_MAX_PROMPT_TOKENS = 2_048
_MAX_OUTPUT_TOKENS = 128
_MAX_PROMPT_USD_PER_MILLION = Decimal("1.00")
_MAX_COMPLETION_USD_PER_MILLION = Decimal("2.00")
_DEFAULT_RUN_COST_CAP_MICROS = 10_000


def _live_models() -> tuple[tuple[str, str, bool], ...]:
    """Return the explicit structured and prompt-only model contract cases."""
    return (
        (
            "structured",
            os.environ.get("PADRINO_OPENROUTER_STRUCTURED_MODEL", "openai/gpt-4o-mini"),
            True,
        ),
        (
            "non_structured",
            os.environ.get(
                "PADRINO_OPENROUTER_NON_STRUCTURED_MODEL",
                "meta-llama/llama-3.3-70b-instruct",
            ),
            False,
        ),
    )


def _run_cost_cap_micros() -> int:
    raw = os.environ.get(
        "PADRINO_OPENROUTER_ADAPTER_LIVE_COST_CAP_MICROS",
        str(_DEFAULT_RUN_COST_CAP_MICROS),
    )
    try:
        value = int(raw)
    except ValueError as exc:
        raise pytest.UsageError(
            "PADRINO_OPENROUTER_ADAPTER_LIVE_COST_CAP_MICROS must be an integer"
        ) from exc
    if value <= 0:
        raise pytest.UsageError("PADRINO_OPENROUTER_ADAPTER_LIVE_COST_CAP_MICROS must be positive")
    return value


def _conservative_suite_cost_micros() -> int:
    """Bound two models across vote and night-action turns before any spend."""
    calls = len(_live_models()) * len(_observations())
    per_call = (
        Decimal(_MAX_PROMPT_TOKENS) * _MAX_PROMPT_USD_PER_MILLION
        + Decimal(_MAX_OUTPUT_TOKENS) * _MAX_COMPLETION_USD_PER_MILLION
    ) / Decimal(1_000_000)
    return int((per_call * Decimal(calls) * _USD_MICROS).to_integral_value(ROUND_CEILING))


def _observations() -> tuple[Observation, Observation]:
    """Return representative legal vote and detective night-action turns."""
    return (
        Observation(
            ruleset_id="openrouter_adapter_contract_v1",
            game_public_id="openrouter-adapter-contract",
            phase="DAY_1_VOTE",
            day=1,
            round=1,
            you=YouInfo(
                player_id="P01",
                alive=True,
                role=Role.VILLAGER,
                faction=Faction.TOWN,
            ),
            alive_players=("P01", "P02", "P03"),
            dead_players=(),
            public_events=(),
            private_events=(),
            legal_actions=LegalActions(
                allowed_action_types=[ActionType.VOTE, ActionType.ABSTAIN],
                legal_targets=["P02", "P03"],
            ),
            your_private_memory="",
            message_limits=MessageLimits(
                public_message_max_chars=600,
                private_message_max_chars=600,
                memory_update_max_chars=1200,
            ),
        ),
        Observation(
            ruleset_id="openrouter_adapter_contract_v1",
            game_public_id="openrouter-adapter-contract",
            phase="NIGHT_1_ACTIONS",
            day=1,
            round=1,
            you=YouInfo(
                player_id="P01",
                alive=True,
                role=Role.DETECTIVE,
                faction=Faction.TOWN,
            ),
            alive_players=("P01", "P02", "P03"),
            dead_players=(),
            public_events=(),
            private_events=(),
            legal_actions=LegalActions(
                allowed_action_types=[ActionType.INVESTIGATE],
                legal_targets=["P02", "P03"],
            ),
            your_private_memory="",
            message_limits=MessageLimits(
                public_message_max_chars=600,
                private_message_max_chars=600,
                memory_update_max_chars=1200,
            ),
        ),
    )


def _adapter(*, model: str, structured: bool) -> LiteLlmAdapter:
    inference_params: dict[str, object] = {
        "temperature": 0,
        "max_tokens": _MAX_OUTPUT_TOKENS,
    }
    if structured:
        inference_params["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "padrino_agent_response",
                "strict": True,
                "schema": AgentResponse.model_json_schema(),
            },
        }
    return LiteLlmAdapter(
        routing_policy=RoutingPolicy(
            primary_model=model,
            fallback_model=None,
            openrouter=OpenRouterRequestPolicy(
                app_url="https://github.com/tommyyzhao/padrino",
                app_title="Padrino adapter contract",
                provider=OpenRouterProviderPreferences(
                    allow_fallbacks=False,
                    require_parameters=structured,
                    max_price=OpenRouterMaxPrice(
                        prompt=float(_MAX_PROMPT_USD_PER_MILLION),
                        completion=float(_MAX_COMPLETION_USD_PER_MILLION),
                    ),
                    zdr=True,
                ),
            ),
        ),
        agent_build=AgentBuild(
            provider="openrouter",
            model_id=model,
            prompt_version="openrouter_adapter_contract_v1",
            inference_params=inference_params,
            adapter_version="openrouter-adapter-contract-v1",
        ),
        timeout_s=45.0,
        auth_secret_ref="env:OPENROUTER_API_KEY",
        api_base="https://openrouter.ai/api/v1",
        retry_policy=RetryPolicy(
            max_attempts=1,
            base_delay_s=0.0,
            max_delay_s=0.0,
            retry_on=(),
        ),
    )


def _cost_micros(cost_usd: float | None) -> int:
    if cost_usd is None:
        return 0
    return int((Decimal(str(cost_usd)) * _USD_MICROS).to_integral_value(rounding=ROUND_CEILING))


@pytest.mark.live_llm
async def test_live_openrouter_models_complete_structured_and_prompt_only_turns() -> None:
    """Two affordable models complete both vote and night-action observations."""
    if os.environ.get("PADRINO_RUN_LIVE_OPENROUTER_ADAPTER") != "1":
        pytest.skip("set PADRINO_RUN_LIVE_OPENROUTER_ADAPTER=1 to opt into funded adapter coverage")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        pytest.skip("OPENROUTER_API_KEY is required for funded adapter coverage")

    cost_cap_micros = _run_cost_cap_micros()
    estimated_micros = _conservative_suite_cost_micros()
    if estimated_micros > cost_cap_micros:
        pytest.skip(
            "live adapter coverage skipped without spend: conservative request bound "
            f"{estimated_micros} micros exceeds cap {cost_cap_micros} micros"
        )

    total_cost_micros = 0
    for _kind, model, structured in _live_models():
        adapter = _adapter(model=model, structured=structured)
        for observation in _observations():
            result = await adapter.complete(observation)
            assert result.status == "ok"
            assert isinstance(result.parsed_response, AgentResponse)
            action = result.parsed_response.action
            assert action.type in observation.legal_actions.allowed_action_types
            if action.type is ActionType.ABSTAIN:
                assert action.target is None
            else:
                assert action.target in observation.legal_actions.legal_targets
            assert result.cost_usd is not None
            total_cost_micros += _cost_micros(result.cost_usd)
            assert total_cost_micros <= cost_cap_micros

    assert total_cost_micros <= cost_cap_micros
