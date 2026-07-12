"""Bounded, secret-free OpenRouter routing metadata extraction.

OpenRouter returns optional router details only when the caller opts into its
metadata header.  Those details are useful audit facts, but may also contain
free-form guardrail or plugin data.  This impure boundary keeps a deliberately
small allowlist before any metadata reaches durable LLM-call storage.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

_MAX_TEXT_LENGTH = 512
_MAX_ATTEMPTS = 16
_MAX_PIPELINE_STAGES = 16
_MAX_COUNT = 1_000_000_000
_PREFERENCE_KEYS = frozenset(
    {
        "allow_fallbacks",
        "max_price",
        "only",
        "order",
        "require_parameters",
        "sort",
        "zdr",
    }
)
_PIPELINE_TEXT_KEYS = ("type", "name", "engine", "input_type", "mode")
_PIPELINE_BOOLEAN_KEYS = ("detected", "flagged", "improved")
_PIPELINE_COUNT_KEYS = (
    "compressed_count",
    "original_count",
    "page_count",
    "result_count",
    "tool_count",
)


class OpenRouterCallAudit(BaseModel):
    """Safe routing attribution copied from one OpenRouter response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_model_slug: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    actual_model: str | None = Field(default=None, max_length=_MAX_TEXT_LENGTH)
    actual_provider: str | None = Field(default=None, max_length=_MAX_TEXT_LENGTH)
    actual_endpoint: str | None = Field(default=None, max_length=_MAX_TEXT_LENGTH)
    generation_id: str | None = Field(default=None, max_length=_MAX_TEXT_LENGTH)
    routing_metadata: dict[str, object] = Field(default_factory=dict)
    catalog_snapshot_id: uuid.UUID | None = None
    routing_policy_hash: str = Field(min_length=64, max_length=64)


def extract_openrouter_call_audit(
    response: object | None,
    *,
    requested_model_slug: str,
    provider_preferences: Mapping[str, object],
    catalog_snapshot_id: uuid.UUID | None,
) -> OpenRouterCallAudit:
    """Extract only bounded routing attribution from an OpenRouter response.

    ``response`` may be a LiteLLM model response or ``None`` after a transport
    error.  The frozen requested slug and policy hash still make a failed call
    auditable, while actual router facts remain nullable until OpenRouter
    supplied them.
    """
    requested = _safe_text(requested_model_slug)
    if requested is None:  # pragma: no cover - RoutingPolicy already validates this invariant.
        raise ValueError("openrouter_requested_model_required")

    metadata = _metadata_mapping(response)
    selected = _selected_endpoint(metadata)
    actual_model = _safe_text(_value(response, "model")) or _safe_text(selected.get("model"))
    actual_provider = _safe_text(selected.get("provider")) or _safe_text(metadata.get("provider"))
    actual_endpoint = _safe_endpoint(selected.get("endpoint")) or _safe_endpoint(
        metadata.get("endpoint")
    )

    routing_metadata = _routing_metadata(metadata, response)
    return OpenRouterCallAudit(
        requested_model_slug=requested,
        actual_model=actual_model,
        actual_provider=actual_provider,
        actual_endpoint=actual_endpoint,
        generation_id=_generation_id(response),
        routing_metadata=routing_metadata,
        catalog_snapshot_id=catalog_snapshot_id,
        routing_policy_hash=openrouter_routing_policy_hash(
            requested_model_slug=requested,
            provider_preferences=provider_preferences,
        ),
    )


def openrouter_routing_policy_hash(
    *,
    requested_model_slug: str,
    provider_preferences: Mapping[str, object],
) -> str:
    """Hash only the known-safe router policy fields, never arbitrary metadata."""
    safe_preferences = {
        key: _json_safe(value)
        for key, value in sorted(provider_preferences.items())
        if key in _PREFERENCE_KEYS and _json_safe(value) is not None
    }
    payload = {
        "provider_preferences": safe_preferences,
        "requested_model_slug": requested_model_slug,
        "schema": "openrouter-routing-policy-v1",
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def sanitize_openrouter_routing_metadata(value: Mapping[str, object]) -> dict[str, object]:
    """Return the persistence-safe allowlist for arbitrary router metadata."""
    return _routing_metadata(value, None)


def _metadata_mapping(response: object | None) -> Mapping[str, object]:
    value = _value(response, "openrouter_metadata")
    if isinstance(value, Mapping):
        return value
    hidden = _value(response, "_hidden_params")
    if isinstance(hidden, Mapping):
        nested = hidden.get("openrouter_metadata")
        if isinstance(nested, Mapping):
            return nested
    return {}


def _value(source: object | None, key: str) -> object | None:
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(key)
    value = getattr(source, key, None)
    if value is not None:
        return cast(object, value)
    getter = getattr(source, "get", None)
    if callable(getter):
        candidate = getter(key)
        return candidate if candidate is not None else None
    return None


def _selected_endpoint(metadata: Mapping[str, object]) -> Mapping[str, object]:
    endpoints = metadata.get("endpoints")
    if not isinstance(endpoints, Mapping):
        return {}
    available = endpoints.get("available")
    if not isinstance(available, list | tuple):
        return {}
    for candidate in available[:_MAX_ATTEMPTS]:
        if isinstance(candidate, Mapping) and candidate.get("selected") is True:
            return candidate
    return {}


def _routing_metadata(
    metadata: Mapping[str, object],
    response: object | None,
) -> dict[str, object]:
    result: dict[str, object] = {}
    strategy = _safe_text(metadata.get("strategy"))
    if strategy is not None:
        result["strategy"] = strategy
    attempt = _safe_count(metadata.get("attempt"))
    if attempt is not None:
        result["attempt"] = attempt
    is_byok = metadata.get("is_byok")
    if not isinstance(is_byok, bool):
        usage = _value(response, "usage")
        is_byok = _value(usage, "is_byok")
    if isinstance(is_byok, bool):
        result["is_byok"] = is_byok
    attempts = _normalized_attempts(metadata.get("attempts"))
    if attempts:
        result["attempts"] = attempts
    pipeline = _normalized_pipeline(metadata.get("pipeline"))
    if pipeline:
        result["pipeline"] = pipeline
    return result


def _normalized_attempts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list | tuple):
        return []
    normalized: list[dict[str, object]] = []
    for item in value[:_MAX_ATTEMPTS]:
        if not isinstance(item, Mapping):
            continue
        entry: dict[str, object] = {}
        for key in ("provider", "model"):
            text = _safe_text(item.get(key))
            if text is not None:
                entry[key] = text
        endpoint = _safe_endpoint(item.get("endpoint"))
        if endpoint is not None:
            entry["endpoint"] = endpoint
        status = _safe_status(item.get("status"))
        if status is not None:
            entry["status"] = status
        if entry:
            normalized.append(entry)
    return normalized


def _normalized_pipeline(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list | tuple):
        return []
    normalized: list[dict[str, object]] = []
    for item in value[:_MAX_PIPELINE_STAGES]:
        if not isinstance(item, Mapping):
            continue
        data = item.get("data")
        data_mapping = data if isinstance(data, Mapping) else {}
        entry: dict[str, object] = {}
        for key in _PIPELINE_TEXT_KEYS:
            text = _safe_text(item.get(key)) or _safe_text(data_mapping.get(key))
            if text is not None:
                entry[key] = text
        for key in _PIPELINE_BOOLEAN_KEYS:
            value_bool = item.get(key)
            if not isinstance(value_bool, bool):
                value_bool = data_mapping.get(key)
            if isinstance(value_bool, bool):
                entry[key] = value_bool
        for key in _PIPELINE_COUNT_KEYS:
            count = _safe_count(item.get(key))
            if count is None:
                count = _safe_count(data_mapping.get(key))
            if count is not None:
                entry[key] = count
        if entry:
            normalized.append(entry)
    return normalized


def _generation_id(response: object | None) -> str | None:
    for headers_name in ("_response_headers", "response_headers", "headers"):
        headers = _value(response, headers_name)
        if isinstance(headers, Mapping):
            for key, value in headers.items():
                if str(key).casefold() == "x-generation-id":
                    generation_id = _safe_text(value)
                    if generation_id is not None:
                        return generation_id
    return _safe_text(_value(response, "id"))


def _safe_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_TEXT_LENGTH:
        return None
    if any(ord(character) < 32 for character in normalized):
        return None
    return normalized


def _safe_endpoint(value: object) -> str | None:
    endpoint = _safe_text(value)
    if endpoint is None:
        return None
    parsed = urlsplit(endpoint)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        if parsed.username is not None or parsed.password is not None:
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    if "?" in endpoint or "#" in endpoint:
        return None
    return endpoint


def _safe_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value <= _MAX_COUNT:
        return None
    return value


def _safe_status(value: object) -> int | str | None:
    count = _safe_count(value)
    if count is not None:
        return count
    return _safe_text(value)


def _json_safe(value: object) -> object | None:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        normalized = [_json_safe(item) for item in value]
        return [item for item in normalized if item is not None]
    if isinstance(value, Mapping):
        return {
            str(key): safe_value
            for key, item in value.items()
            if (safe_value := _json_safe(item)) is not None
        }
    return None


__all__ = [
    "OpenRouterCallAudit",
    "extract_openrouter_call_audit",
    "openrouter_routing_policy_hash",
    "sanitize_openrouter_routing_metadata",
]
