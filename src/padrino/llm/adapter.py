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

import re
import uuid
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from padrino.core.agents.contract import AgentResponse, ResponseError
from padrino.core.observations import Observation
from padrino.llm.retry import LlmCallFailed
from padrino.openrouter.routing_metadata import OpenRouterCallAudit

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


_OPENROUTER_PROVIDER_SLUG = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
_OPENROUTER_MODEL_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:/-]*$")


class OpenRouterMaxPrice(BaseModel):
    """Validated OpenRouter provider-price caps in USD per documented unit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    completion: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    request: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    image: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _requires_at_least_one_limit(self) -> OpenRouterMaxPrice:
        if all(value is None for value in (self.prompt, self.completion, self.request, self.image)):
            raise ValueError("max_price must set at least one price cap")
        return self


class OpenRouterProviderPreferences(BaseModel):
    """Validated provider-routing controls for one canonical OpenRouter model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sort: Literal["price", "throughput", "latency"] | None = None
    order: tuple[str, ...] = ()
    only: tuple[str, ...] = ()
    allow_fallbacks: bool = True
    require_parameters: bool = False
    max_price: OpenRouterMaxPrice | None = None
    zdr: bool = True

    @field_validator("order", "only")
    @classmethod
    def _valid_provider_slugs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            return values
        if len(values) != len(set(values)):
            raise ValueError("provider slugs must not contain duplicates")
        for value in values:
            if not _OPENROUTER_PROVIDER_SLUG.fullmatch(value):
                raise ValueError("provider slugs must be lowercase non-empty routing identifiers")
        return values

    def as_request_dict(self) -> dict[str, Any]:
        """Return the OpenRouter ``provider`` request-body object."""
        payload: dict[str, Any] = {
            "allow_fallbacks": self.allow_fallbacks,
            "require_parameters": self.require_parameters,
            "zdr": self.zdr,
        }
        if self.sort is not None:
            payload["sort"] = self.sort
        if self.order:
            payload["order"] = list(self.order)
        if self.only:
            payload["only"] = list(self.only)
        if self.max_price is not None:
            payload["max_price"] = self.max_price.model_dump(exclude_none=True)
        return payload


class OpenRouterRequestPolicy(BaseModel):
    """OpenRouter-only request metadata and provider-routing preferences."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    app_url: str = Field(min_length=1, max_length=2048)
    app_title: str = Field(min_length=1, max_length=256)
    provider: OpenRouterProviderPreferences = Field(default_factory=OpenRouterProviderPreferences)

    @field_validator("app_url")
    @classmethod
    def _valid_app_url(cls, value: str) -> str:
        normalized = value.strip()
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("OpenRouter app_url must be an HTTPS URL")
        return normalized

    @field_validator("app_title")
    @classmethod
    def _valid_app_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("OpenRouter app_title must not be blank")
        return normalized

    def request_headers(self) -> dict[str, str]:
        """Return mandatory attribution and router-metadata opt-in headers."""
        return {
            "HTTP-Referer": self.app_url,
            "X-OpenRouter-Title": self.app_title,
            "X-OpenRouter-Metadata": "enabled",
        }


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
    openrouter_audit: OpenRouterCallAudit | None = None
    status: AdapterStatus = "ok"
    error: str | None = None
    failure: LlmCallFailed | None = None


class ProviderNoContestError(RuntimeError):
    """Abort a funded game after a persisted provider failure.

    ``result`` remains in impure memory only until the recording layer stores
    the normal call audit.  The exception text is a stable safe reason token,
    never a provider diagnostic or credential value.
    """

    __slots__ = ("reason", "result")

    def __init__(self, *, reason: str, result: AdapterResult) -> None:
        self.reason = reason
        self.result = result
        super().__init__(reason)


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
    openrouter: OpenRouterRequestPolicy | None = None

    @model_validator(mode="after")
    def _requires_canonical_openrouter_slug(self) -> RoutingPolicy:
        if self.openrouter is not None and not _OPENROUTER_MODEL_SLUG.fullmatch(self.primary_model):
            raise ValueError("OpenRouter primary_model must be a canonical provider/model slug")
        return self


class AgentBuild(BaseModel):
    """Pure value-object projection of a deployable agent build.

    Decoupled from :class:`padrino.db.models.AgentBuild` (the SQLAlchemy row) so
    the LLM layer never imports from the DB layer. Adapters consume this object
    to pick the provider/model and inject inference parameters; the ORM row is
    flattened into this shape at the seam where the runner is configured. The
    provider credential remains an opaque reference: its resolved value never
    enters this model and ``auth_secret_ref`` is excluded from serialization.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    model_id: str
    prompt_version: str
    inference_params: dict[str, Any] = Field(default_factory=dict)
    adapter_version: str
    base_url: str | None = None
    auth_secret_ref: str | None = Field(default=None, exclude=True, repr=False)
    catalog_snapshot_id: uuid.UUID | None = None


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
    "OpenRouterMaxPrice",
    "OpenRouterProviderPreferences",
    "OpenRouterRequestPolicy",
    "ProviderNoContestError",
    "RoutingPolicy",
    "SameModelHost",
]
