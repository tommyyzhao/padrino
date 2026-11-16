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
import re
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


_THINK_BLOCK = re.compile(r"<(think|thinking)>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_THINK_UNCLOSED = re.compile(r"<(?:think|thinking)>[^{]*", re.IGNORECASE)


def _strip_think(text: str) -> str:
    # ponytail: an unclosed think block is dropped up to the first "{". Reasoning text
    # containing "{" is not removed; it is skipped by the balanced-span scan when invalid.
    text = _THINK_BLOCK.sub("", text)
    return _THINK_UNCLOSED.sub("", text)


def _balanced_end(text: str, start: int) -> int | None:
    """Index just past the object opening at ``start``, or None if never closed."""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def extract_json_object(text: str) -> str | None:
    """Return the first top-level JSON object embedded in model output, or None.

    Removes ``<think>``/``<thinking>`` blocks (closed, or unclosed up to the next ``{``),
    then scans for balanced ``{...}`` spans honoring JSON string literals and escapes.
    Code fence markers and prose are ignored because only braces delimit candidates.
    A balanced span that does not decode to a JSON object is skipped whole; an
    unbalanced ``{`` (stray prose brace or truncated output) is skipped by one char.
    """
    cleaned = _strip_think(text)
    pos = cleaned.find("{")
    while pos != -1:
        end = _balanced_end(cleaned, pos)
        if end is None:
            # A stray "{" in prose never closes; retry from the next one.
            pos = cleaned.find("{", pos + 1)
            continue
        candidate = cleaned[pos:end]
        try:
            if isinstance(json.loads(candidate), dict):
                return candidate
        except ValueError:
            pass
        pos = cleaned.find("{", end)
    return None


def parse_agent_response(raw_text: str) -> AgentResponse | ResponseError:
    """Parse a raw LLM response into an :class:`AgentResponse` or :class:`ResponseError`.

    Never raises. Fast path: the whole text is JSON. Otherwise the first embedded JSON
    object is extracted (see :func:`extract_json_object`) and used instead. Invalid JSON
    with nothing extractable yields ``ResponseError(reason="INVALID_JSON")``; schema
    mismatches yield ``ResponseError(reason="SCHEMA_VIOLATION", details=...)``.
    Over-limit messages are NOT truncated — the sanitizer (US-022) handles that.
    """

    try:
        decoded: Any = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError) as exc:
        extracted = extract_json_object(raw_text)
        if extracted is None:
            return ResponseError(reason=REASON_INVALID_JSON, details=str(exc))
        decoded = json.loads(extracted)

    if not isinstance(decoded, dict):
        return ResponseError(
            reason=REASON_SCHEMA_VIOLATION,
            details=f"expected JSON object at top level, got {type(decoded).__name__}",
        )

    try:
        return AgentResponse.model_validate(decoded)
    except ValidationError as exc:
        return ResponseError(reason=REASON_SCHEMA_VIOLATION, details=exc.json())
