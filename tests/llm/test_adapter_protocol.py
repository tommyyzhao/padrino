"""Tests for the :class:`LlmAdapter` Protocol and its value objects."""

from __future__ import annotations

import ast
import inspect
import typing
from pathlib import Path

import pytest
from pydantic import ValidationError

from padrino.llm.adapter import (
    AdapterResult,
    AgentBuild,
    LlmAdapter,
    RoutingPolicy,
)
from padrino.llm.mock import DeterministicMockAdapter


def test_llm_adapter_is_runtime_checkable_protocol() -> None:
    # The runtime_checkable decorator allows isinstance() against the Protocol.
    assert hasattr(LlmAdapter, "_is_runtime_protocol") or hasattr(LlmAdapter, "_is_protocol")
    # isinstance should not raise — runtime_checkable Protocols permit it.
    adapter = DeterministicMockAdapter({})
    assert isinstance(adapter, LlmAdapter)


def test_deterministic_mock_adapter_conforms_to_protocol() -> None:
    adapter = DeterministicMockAdapter({})
    complete = adapter.complete
    assert inspect.iscoroutinefunction(complete)
    sig = inspect.signature(complete)
    params = list(sig.parameters)
    assert params == ["observation"]


def test_llm_adapter_protocol_signature_is_explicit() -> None:
    # The Protocol must declare exactly one method with the documented shape.
    members = {
        name
        for name, value in inspect.getmembers(LlmAdapter)
        if not name.startswith("_") and callable(value)
    }
    assert "complete" in members
    sig = inspect.signature(LlmAdapter.complete)
    params = list(sig.parameters)
    assert params == ["self", "observation"]
    hints = typing.get_type_hints(LlmAdapter.complete)
    assert hints["return"] is AdapterResult


def test_routing_policy_defaults() -> None:
    policy = RoutingPolicy(primary_model="cerebras/zai-glm-4.7")
    assert policy.primary_model == "cerebras/zai-glm-4.7"
    assert policy.fallback_model is None
    assert policy.max_retries == 0
    assert policy.retry_on_error_classes == []


def test_routing_policy_full_payload() -> None:
    policy = RoutingPolicy(
        primary_model="cerebras/zai-glm-4.7",
        fallback_model="deepinfra/deepseek-ai/DeepSeek-V4-Flash",
        max_retries=2,
        retry_on_error_classes=["TimeoutError", "RateLimitError"],
    )
    assert policy.fallback_model == "deepinfra/deepseek-ai/DeepSeek-V4-Flash"
    assert policy.max_retries == 2
    assert policy.retry_on_error_classes == ["TimeoutError", "RateLimitError"]


def test_routing_policy_is_frozen() -> None:
    policy = RoutingPolicy(primary_model="m")
    with pytest.raises(ValidationError):
        policy.primary_model = "other"  # type: ignore[misc]


def test_agent_build_value_object_round_trip() -> None:
    build = AgentBuild(
        provider="cerebras",
        model_id="zai-glm-4.7",
        prompt_version="v1.0.0",
        inference_params={"temperature": 0.7, "top_p": 1.0},
        adapter_version="litellm-1",
    )
    assert build.provider == "cerebras"
    assert build.model_id == "zai-glm-4.7"
    assert build.prompt_version == "v1.0.0"
    assert build.inference_params == {"temperature": 0.7, "top_p": 1.0}
    assert build.adapter_version == "litellm-1"


def test_agent_build_default_inference_params() -> None:
    build = AgentBuild(
        provider="cerebras",
        model_id="m",
        prompt_version="v1",
        adapter_version="a1",
    )
    assert build.inference_params == {}


def test_agent_build_is_frozen() -> None:
    build = AgentBuild(
        provider="p",
        model_id="m",
        prompt_version="v",
        adapter_version="a",
    )
    with pytest.raises(ValidationError):
        build.provider = "other"  # type: ignore[misc]


def test_agent_build_value_object_does_not_import_db_or_sqlalchemy() -> None:
    # Inspect the AST so docstring mentions of "padrino.db" don't false-positive.
    source = Path("src/padrino/llm/adapter.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            module_names.add(node.module)
    for mod in module_names:
        assert not mod.startswith("padrino.db"), f"unexpected import {mod!r}"
        assert not mod.startswith("sqlalchemy"), f"unexpected import {mod!r}"


class _MinimalAdapter:
    async def complete(
        self, observation: object
    ) -> AdapterResult:  # pragma: no cover - protocol probe
        raise NotImplementedError


def test_protocol_accepts_arbitrary_conforming_classes() -> None:
    assert isinstance(_MinimalAdapter(), LlmAdapter)


class _NonConformingAdapter:
    def respond(self) -> None:  # pragma: no cover - intentional mismatch
        pass


def test_protocol_rejects_non_conforming_classes() -> None:
    assert not isinstance(_NonConformingAdapter(), LlmAdapter)
