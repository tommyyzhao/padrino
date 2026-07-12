"""Unit tests for heterogeneous-roster adapter assembly (US-083).

These never touch a provider: ``resolve_secret`` only reads env vars, so
monkeypatched fake keys let us assert per-seat adapter wiring (model id,
api_base, single-host routing) without any network call.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from padrino.core.engine.legal_actions import LegalActions
from padrino.core.enums import ActionType, Faction, Role
from padrino.core.observations import MessageLimits, Observation, YouInfo
from padrino.gauntlets.heterogeneous import build_heterogeneous_adapter
from padrino.llm.adapter import AgentBuild
from padrino.llm.litellm_adapter import LiteLlmAdapter
from padrino.settings import Settings


def _build(
    provider: str,
    model_id: str,
    *,
    base_url: str | None = None,
    auth_secret_ref: str | None = None,
) -> AgentBuild:
    return AgentBuild(
        provider=provider,
        model_id=model_id,
        prompt_version="canonical-v1",
        inference_params={"temperature": 0.7, "top_p": 1.0},
        adapter_version="het-v1",
        base_url=base_url,
        auth_secret_ref=auth_secret_ref,
    )


@pytest.fixture(autouse=True)
def _fake_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk-fake")
    monkeypatch.setenv("DEEPINFRA_API_KEY", "lw-fake")
    monkeypatch.setenv("XIAOMI_API_KEY", "tp-fake")
    monkeypatch.setenv("ZAI_API_KEY", "0123456789abcdef0123456789abcdef.ABCDEFGHIJKLMNO1")


def test_builds_one_adapter_per_seat_with_correct_routing() -> None:
    assignments = {
        "P01": _build(
            "cerebras",
            "cerebras/zai-glm-4.7",
            auth_secret_ref="env:CEREBRAS_API_KEY",
        ),
        "P02": _build(
            "deepinfra",
            "deepinfra/google/gemma-4-26B-A4B-it",
            auth_secret_ref="env:DEEPINFRA_API_KEY",
        ),
        "P03": _build(
            "xiaomi",
            "openai/mimo-v2.5",
            base_url="https://token-plan-sgp.xiaomimimo.com/v1",
            auth_secret_ref="env:XIAOMI_API_KEY",
        ),
    }
    mux = build_heterogeneous_adapter(assignments, settings=Settings(), timeout_s=30.0)

    seat_adapters = mux._adapters
    assert set(seat_adapters) == {"P01", "P02", "P03"}
    for adapter in seat_adapters.values():
        assert isinstance(adapter, LiteLlmAdapter)

    # Each seat is single-host: its rated model is exactly the assigned model,
    # with no different-model fallback and no same-model alternate host.
    p01 = seat_adapters["P01"]
    assert isinstance(p01, LiteLlmAdapter)
    assert p01._policy.primary_model == "cerebras/zai-glm-4.7"
    assert p01._policy.fallback_model is None
    assert p01._policy.same_model_hosts == ()
    assert p01._api_base is None

    # Xiaomi seat carries the custom OpenAI-compatible base URL.
    p03 = seat_adapters["P03"]
    assert isinstance(p03, LiteLlmAdapter)
    assert p03._policy.primary_model == "openai/mimo-v2.5"
    assert p03._api_base is not None


def test_empty_assignments_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        build_heterogeneous_adapter({}, settings=Settings())


def test_any_configured_openai_compatible_provider_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNTHETIC_OPENAI_KEY", "synthetic-test-token")
    assignments = {
        "P01": _build(
            "synthetic-openai",
            "openai/synthetic-model",
            base_url="https://synthetic.example/v1",
            auth_secret_ref="env:SYNTHETIC_OPENAI_KEY",
        )
    }

    mux = build_heterogeneous_adapter(assignments, settings=Settings())

    adapter = mux._adapters["P01"]
    assert isinstance(adapter, LiteLlmAdapter)
    assert adapter._api_base == "https://synthetic.example/v1"


def test_openrouter_build_uses_platform_settings_without_a_raw_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PADRINO_OPENROUTER_INFERENCE_KEY", "platform-router-secret")
    settings = Settings(
        _env_file=None,
        padrino_openrouter_inference_key_ref="env:PADRINO_OPENROUTER_INFERENCE_KEY",
        padrino_openrouter_base_url="https://router.example.test/v1",
        padrino_openrouter_app_url="https://padrino.example.test",
        padrino_openrouter_app_title="Padrino Test",
        padrino_openrouter_timeout_seconds=12.5,
    )
    mux = build_heterogeneous_adapter(
        {
            "P01": _build(
                "openrouter",
                "meta-llama/llama-3.3-70b-instruct",
            )
        },
        settings=settings,
    )

    adapter = mux._adapters["P01"]
    assert isinstance(adapter, LiteLlmAdapter)
    assert adapter._api_base == "https://router.example.test/v1"
    assert adapter._timeout_s == 12.5
    assert adapter._auth_secret == "platform-router-secret"
    assert adapter._policy.openrouter is not None
    assert adapter._policy.openrouter.app_url == "https://padrino.example.test"
    assert adapter._policy.openrouter.app_title == "Padrino Test"
    assert adapter._policy.openrouter.provider.allow_fallbacks is True
    assert adapter._policy.openrouter.provider.zdr is True


def test_missing_secret_reference_fails_before_game_start() -> None:
    assignments = {
        "P01": _build(
            "synthetic-openai",
            "openai/synthetic-model",
            base_url="https://synthetic.example/v1",
        )
    }

    with pytest.raises(ValueError, match="credential configuration is missing"):
        build_heterogeneous_adapter(assignments, settings=Settings())


def test_unknown_or_insecure_secret_reference_fails_with_redacted_error(tmp_path: Path) -> None:
    unknown = {
        "P01": _build(
            "synthetic-openai",
            "openai/synthetic-model",
            base_url="https://synthetic.example/v1",
            auth_secret_ref="vault:super-secret-value",
        )
    }
    with pytest.raises(ValueError, match="credential configuration is invalid") as exc_info:
        build_heterogeneous_adapter(unknown, settings=Settings())
    assert "super-secret-value" not in str(exc_info.value)

    secret_path = tmp_path / "provider-secret"
    secret_path.write_text("super-secret-value", encoding="utf-8")
    secret_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    assignments = {
        "P01": _build(
            "synthetic-openai",
            "openai/synthetic-model",
            base_url="https://synthetic.example/v1",
            auth_secret_ref=f"file:{secret_path}",
        )
    }

    with pytest.raises(ValueError, match="credential configuration is invalid") as exc_info:
        build_heterogeneous_adapter(assignments, settings=Settings())

    rendered = str(exc_info.value)
    assert "super-secret-value" not in rendered
    assert str(secret_path) not in rendered


@pytest.mark.asyncio
async def test_synthetic_openai_compatible_provider_completes_stubbed_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNTHETIC_OPENAI_KEY", "synthetic-test-token")
    mux = build_heterogeneous_adapter(
        {
            "P01": _build(
                "synthetic-openai",
                "openai/synthetic-model",
                base_url="https://synthetic.example/v1",
                auth_secret_ref="env:SYNTHETIC_OPENAI_KEY",
            )
        },
        settings=Settings(),
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(
                        {
                            "public_message": "I abstain.",
                            "private_message": None,
                            "action": {"type": "ABSTAIN", "target": None},
                            "memory_update": "",
                            "rationale_summary": None,
                        }
                    )
                )
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        id="synthetic-response-1",
    )
    observation = Observation(
        ruleset_id="mini7_v1",
        game_public_id="synthetic-game",
        phase="DAY_1_VOTE",
        day=1,
        round=0,
        you=YouInfo(
            player_id="P01",
            alive=True,
            role=Role.VILLAGER,
            faction=Faction.TOWN,
        ),
        alive_players=("P01",),
        dead_players=(),
        public_events=(),
        private_events=(),
        legal_actions=LegalActions(
            allowed_action_types=[ActionType.ABSTAIN],
            legal_targets=[],
        ),
        your_private_memory="",
        message_limits=MessageLimits(
            public_message_max_chars=600,
            private_message_max_chars=600,
            memory_update_max_chars=1200,
        ),
    )

    with patch(
        "padrino.llm.litellm_adapter.litellm.acompletion",
        new=AsyncMock(return_value=response),
    ) as completion:
        result = await mux.complete(observation)

    assert result.status == "ok"
    assert completion.call_args.kwargs["api_base"] == "https://synthetic.example/v1"
    assert completion.call_args.kwargs["api_key"] == "synthetic-test-token"
