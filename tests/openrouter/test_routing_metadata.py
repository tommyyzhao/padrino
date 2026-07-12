"""US-349: bounded OpenRouter routing metadata extraction coverage."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from padrino.openrouter.routing_metadata import extract_openrouter_call_audit


def test_router_metadata_keeps_safe_routing_facts_and_discards_sensitive_freeform_data() -> None:
    snapshot_id = uuid.uuid4()
    response = SimpleNamespace(
        id="chatcmpl-provider-id",
        model="openai/gpt-4o-mini-2024-07-18",
        usage=SimpleNamespace(is_byok=True),
        _response_headers={"X-Generation-Id": "gen-safe-123"},
        openrouter_metadata={
            "requested": "openai/gpt-4o-mini",
            "strategy": "auto",
            "attempt": 2,
            "is_byok": True,
            "endpoints": {
                "available": [
                    {
                        "provider": "OpenAI",
                        "model": "openai/gpt-4o-mini-2024-07-18",
                        "endpoint": "https://provider.example/v1/chat?api_key=must-not-persist",
                        "selected": True,
                    }
                ]
            },
            "attempts": [
                {
                    "provider": "OpenAI",
                    "model": "openai/gpt-4o-mini-2024-07-18",
                    "endpoint": "https://provider.example/v1/chat?authorization=never",
                    "status": 503,
                    "error": "Authorization: Bearer should-not-persist",
                },
                {
                    "provider": "OpenAI",
                    "model": "openai/gpt-4o-mini-2024-07-18",
                    "status": 200,
                },
            ],
            "pipeline": [
                {
                    "type": "context_compression",
                    "name": "context-compression",
                    "summary": "compressed the private prompt: do not persist",
                    "data": {
                        "engine": "middle-out",
                        "original_count": 42,
                        "compressed_count": 30,
                        "patterns": ["private prompt text"],
                    },
                }
            ],
            "authorization": "Bearer credential-never-persisted",
            "key_hash": "sha256:human-credential-never-persisted",
        },
    )

    audit = extract_openrouter_call_audit(
        response,
        requested_model_slug="openai/gpt-4o-mini",
        provider_preferences={"allow_fallbacks": True, "zdr": True},
        catalog_snapshot_id=snapshot_id,
    )

    assert audit is not None
    assert audit.requested_model_slug == "openai/gpt-4o-mini"
    assert audit.actual_model == "openai/gpt-4o-mini-2024-07-18"
    assert audit.actual_provider == "OpenAI"
    assert audit.actual_endpoint == "https://provider.example/v1/chat"
    assert audit.generation_id == "gen-safe-123"
    assert audit.catalog_snapshot_id == snapshot_id
    assert audit.routing_metadata == {
        "strategy": "auto",
        "attempt": 2,
        "is_byok": True,
        "attempts": [
            {
                "provider": "OpenAI",
                "model": "openai/gpt-4o-mini-2024-07-18",
                "endpoint": "https://provider.example/v1/chat",
                "status": 503,
            },
            {
                "provider": "OpenAI",
                "model": "openai/gpt-4o-mini-2024-07-18",
                "status": 200,
            },
        ],
        "pipeline": [
            {
                "type": "context_compression",
                "name": "context-compression",
                "engine": "middle-out",
                "original_count": 42,
                "compressed_count": 30,
            }
        ],
    }
    rendered = json.dumps(audit.model_dump(mode="json"), sort_keys=True)
    for forbidden in (
        "must-not-persist",
        "should-not-persist",
        "credential-never-persisted",
        "human-credential-never-persisted",
        "private prompt text",
        "authorization",
    ):
        assert forbidden not in rendered


def test_router_metadata_is_absent_when_openrouter_did_not_supply_any_routing_facts() -> None:
    response = SimpleNamespace(id="provider-call", model="other-provider/model")

    audit = extract_openrouter_call_audit(
        response,
        requested_model_slug="openai/gpt-4o-mini",
        provider_preferences={"allow_fallbacks": True, "zdr": True},
        catalog_snapshot_id=None,
    )

    assert audit is not None
    assert audit.actual_model == "other-provider/model"
    assert audit.generation_id == "provider-call"
    assert audit.routing_metadata == {}
