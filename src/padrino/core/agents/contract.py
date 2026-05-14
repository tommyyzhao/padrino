"""Agent response schema and strict JSON validation.

`AgentResponse` is the typed contract every LLM adapter must satisfy. Parsing
incoming raw text goes through :func:`parse_agent_response`, which never raises
and always returns either a validated :class:`AgentResponse` or a tagged
:class:`ResponseError` describing why parsing failed.

This module deliberately performs no truncation or normalization. Over-limit
messages are accepted here and surface to the output sanitizer (US-022).

Pure core: no DB / LLM / clock / network access.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from padrino.core.engine.actions import Action

ReasonInvalidJson = Literal["INVALID_JSON"]
ReasonSchemaViolation = Literal["SCHEMA_VIOLATION"]
ResponseErrorReason = ReasonInvalidJson | ReasonSchemaViolation

REASON_INVALID_JSON: ReasonInvalidJson = "INVALID_JSON"
REASON_SCHEMA_VIOLATION: ReasonSchemaViolation = "SCHEMA_VIOLATION"


class AgentResponse(BaseModel):
    """The structured response a seat must return each prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    public_message: str | None
    private_message: str | None
    action: Action
    memory_update: str
    rationale_summary: str | None


class ResponseError(BaseModel):
    """Tagged failure outcome of :func:`parse_agent_response`."""

    model_config = ConfigDict(frozen=True)

    reason: ResponseErrorReason
    details: str | None = None


def parse_agent_response(raw_text: str) -> AgentResponse | ResponseError:
    """Parse a raw LLM response into an :class:`AgentResponse` or :class:`ResponseError`.

    Never raises. Invalid JSON yields ``ResponseError(reason="INVALID_JSON")``;
    schema mismatches yield ``ResponseError(reason="SCHEMA_VIOLATION", details=...)``.
    Over-limit messages are NOT truncated — the sanitizer (US-022) handles that.
    """

    try:
        decoded: Any = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError) as exc:
        return ResponseError(reason=REASON_INVALID_JSON, details=str(exc))

    if not isinstance(decoded, dict):
        return ResponseError(
            reason=REASON_SCHEMA_VIOLATION,
            details=f"expected JSON object at top level, got {type(decoded).__name__}",
        )

    try:
        return AgentResponse.model_validate(decoded)
    except ValidationError as exc:
        return ResponseError(reason=REASON_SCHEMA_VIOLATION, details=exc.json())
