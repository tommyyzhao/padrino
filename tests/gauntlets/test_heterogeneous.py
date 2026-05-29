"""Unit tests for heterogeneous-roster adapter assembly (US-083).

These never touch a provider: ``resolve_secret`` only reads env vars, so
monkeypatched fake keys let us assert per-seat adapter wiring (model id,
api_base, single-host routing) without any network call.
"""

from __future__ import annotations

import pytest

from padrino.gauntlets.heterogeneous import build_heterogeneous_adapter, provider_endpoints
from padrino.llm.adapter import AgentBuild
from padrino.llm.litellm_adapter import LiteLlmAdapter
from padrino.settings import Settings


def _build(provider: str, model_id: str) -> AgentBuild:
    return AgentBuild(
        provider=provider,
        model_id=model_id,
        prompt_version="canonical-v1",
        inference_params={"temperature": 0.7, "top_p": 1.0},
        adapter_version="het-v1",
    )


@pytest.fixture(autouse=True)
def _fake_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk-fake")
    monkeypatch.setenv("DEEPINFRA_API_KEY", "lw-fake")
    monkeypatch.setenv("XIAOMI_API_KEY", "tp-fake")
    monkeypatch.setenv("ZAI_API_KEY", "0123456789abcdef0123456789abcdef.ABCDEFGHIJKLMNO1")


def test_provider_endpoints_native_vs_custom_base() -> None:
    endpoints = provider_endpoints(Settings())
    assert endpoints["cerebras"] == ("env:CEREBRAS_API_KEY", None)
    assert endpoints["deepinfra"] == ("env:DEEPINFRA_API_KEY", None)
    auth, base = endpoints["xiaomi"]
    assert auth == "env:XIAOMI_API_KEY"
    assert base is not None and base.startswith("https://")


def test_builds_one_adapter_per_seat_with_correct_routing() -> None:
    assignments = {
        "P01": _build("cerebras", "cerebras/zai-glm-4.7"),
        "P02": _build("deepinfra", "deepinfra/google/gemma-4-26B-A4B-it"),
        "P03": _build("xiaomi", "openai/mimo-v2.5"),
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


def test_unknown_provider_rejected() -> None:
    assignments = {"P01": _build("madeup", "madeup/model")}
    with pytest.raises(ValueError, match="unknown provider"):
        build_heterogeneous_adapter(assignments, settings=Settings())
