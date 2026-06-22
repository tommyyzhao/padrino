"""LLM adapter Protocol, routing policy, and agent-build value object.

`LlmAdapter` is the structural contract every provider implementation (real or
mock) must satisfy. The runner's tick barrier (US-024) dispatches each seat's
observation through `adapter.complete(observation)` and consumes the returned
`AdapterResult`. Concrete adapters live elsewhere in this package: a
deterministic mock (US-025) and the LiteLLM-backed implementations (US-035+).

`AdapterResult.parsed_response` is the union of a validated
:class:`~padrino.core.agents.contract.AgentResponse` or a tagged
:class:`~padrino.core.agents.contract.ResponseError` — adapters never raise on
parse failure; they record the error in the result and let the runner coerce
to a safe action via :func:`padrino.core.agents.coercion.coerce_response_failure`.

`RoutingPolicy` and :class:`AgentBuild` are Pydantic value objects that real
adapters (US-035+) consume to drive primary/fallback model selection and
per-build inference parameters. They are deliberately decoupled from the
SQLAlchemy ORM model :class:`padrino.db.models.AgentBuild`; the ORM row is
projected into this value object before being handed to the adapter.

This module sits in the impure ``llm`` layer; pure-core code never imports it.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from padrino.core.agents.contract import AgentResponse, ResponseError
from padrino.core.observations import Observation
from padrino.llm.retry import LlmCallFailed

AdapterStatus = Literal[
    "ok",
    "invalid_json",
    "schema_violation",
    "provider_error",
    "primary_failed",
    "fallback_ok",
    "same_model_fallback_ok",
    "both_failed",
    "exhausted",
]


class AdapterResult(BaseModel):
    """One adapter call's outcome — the raw provider text plus parsed contract."""

    model_config = ConfigDict(frozen=True)

    raw_response: str
    parsed_response: AgentResponse | ResponseError
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    model_id: str | None = None
    provider_response_id: str | None = None
    status: AdapterStatus = "ok"
    error: str | None = None
    failure: LlmCallFailed | None = None


class SameModelHost(BaseModel):
    """One alternate host that serves the SAME model identity as the primary.

    A same-model host is a different provider endpoint (and possibly a
    different ``litellm_model_id`` prefix) that loads the same upstream
    weights as :attr:`RoutingPolicy.primary_model` — e.g. Z.AI's
    ``openai/glm-4.7`` endpoint as a fallback for Cerebras's
    ``cerebras/zai-glm-4.7``. The leaderboard keeps a single row for the
    AgentBuild regardless of which host actually served a given call;
    the per-host distinction is observable only in ``llm_calls`` rows.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    litellm_model_id: str
    auth_secret_ref: str
    api_base: str | None = None


class RoutingPolicy(BaseModel):
    """How an adapter should route a single completion across primary/fallback."""

    model_config = ConfigDict(frozen=True)

    primary_model: str
    fallback_model: str | None = None
    same_model_hosts: tuple[SameModelHost, ...] = ()
    max_retries: int = 0
    retry_on_error_classes: list[str] = Field(default_factory=list)


class AgentBuild(BaseModel):
    """Pure value-object projection of a deployable agent build.

    Decoupled from :class:`padrino.db.models.AgentBuild` (the SQLAlchemy row) so
    the LLM layer never imports from the DB layer. Adapters consume this object
    to pick the provider/model and inject inference parameters; the ORM row is
    flattened into this shape at the seam where the runner is configured.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    model_id: str
    prompt_version: str
    inference_params: dict[str, Any] = Field(default_factory=dict)
    adapter_version: str


@runtime_checkable
class LlmAdapter(Protocol):
    """Structural Protocol every adapter (mock or real) must satisfy.

    Implementations may carry arbitrary internal state (routing policy, agent
    build, provider client) but expose exactly one async method that maps an
    :class:`Observation` to an :class:`AdapterResult`. The runner never inspects
    the implementation type.
    """

    async def complete(self, observation: Observation) -> AdapterResult: ...


__all__ = [
    "AdapterResult",
    "AdapterStatus",
    "AgentBuild",
    "LlmAdapter",
    "RoutingPolicy",
    "SameModelHost",
]
