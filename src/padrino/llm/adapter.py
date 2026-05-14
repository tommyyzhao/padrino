"""LLM adapter Protocol and result envelope.

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

This module sits in the impure ``llm`` layer; pure-core code never imports it.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from padrino.core.agents.contract import AgentResponse, ResponseError
from padrino.core.observations import Observation

AdapterStatus = Literal["ok", "invalid_json", "schema_violation", "provider_error"]


class AdapterResult(BaseModel):
    """One adapter call's outcome — the raw provider text plus parsed contract."""

    model_config = ConfigDict(frozen=True)

    raw_response: str
    parsed_response: AgentResponse | ResponseError
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    provider_response_id: str | None = None
    status: AdapterStatus = "ok"
    error: str | None = None


class LlmAdapter(Protocol):
    """Structural Protocol every adapter (mock or real) must satisfy."""

    async def complete(self, observation: Observation) -> AdapterResult: ...


__all__ = ["AdapterResult", "AdapterStatus", "LlmAdapter"]
