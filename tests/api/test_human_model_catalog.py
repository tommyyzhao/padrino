"""US-344 selector-facing OpenRouter model-catalog API coverage."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import RateLimiter
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.db.models import ModelCatalogEntry, ModelProbeEvidence
from padrino.db.repositories.model_catalog_snapshots import (
    CatalogEntryInput,
    create_or_get_completed_snapshot,
)
from padrino.settings import get_settings

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    import padrino.api.routes.model_catalog as catalog_routes

    monkeypatch.setenv("PADRINO_HUMAN_SESSION_COOKIE_SECURE", "false")
    monkeypatch.setattr(catalog_routes, "_now_utc", lambda: NOW)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _entry(
    slug: str,
    *,
    display_name: str | None = None,
    context_length: int | None = 128_000,
    prompt_price: int | None = 100,
    completion_price: int | None = 200,
    structured: bool = False,
    privacy: str | None = None,
    availability: str | None = None,
    provider_pinned: bool = False,
    provider_fingerprint: str | None = None,
    expires_at: datetime | None = None,
    secret_marker: str | None = None,
) -> CatalogEntryInput:
    pricing_micros: dict[str, int] = {}
    pricing_strings: dict[str, str] = {}
    if prompt_price is not None:
        pricing_micros["prompt_per_million"] = prompt_price
        pricing_strings["prompt"] = "0.0000001"
    if completion_price is not None:
        pricing_micros["completion_per_million"] = completion_price
        pricing_strings["completion"] = "0.0000002"
    provider_summary: dict[str, object] = {}
    if provider_pinned:
        provider_summary["provider_pinned"] = True
    if provider_fingerprint is not None:
        provider_summary["provider_fingerprint"] = provider_fingerprint
    raw_metadata: dict[str, object] = {"architecture": {"output_modalities": ["text"]}}
    if privacy is not None:
        provider_summary["privacy"] = privacy
    if availability is not None:
        raw_metadata["status"] = availability
    if secret_marker is not None:
        raw_metadata["auth_secret_ref"] = secret_marker
    return CatalogEntryInput(
        canonical_slug=slug,
        display_name=display_name or slug,
        description=f"Description for {slug}",
        modalities=("text",),
        context_length=context_length,
        pricing_strings=pricing_strings,
        pricing_micros=pricing_micros,
        supported_parameters=("response_format", "temperature") if structured else ("temperature",),
        provider_summary=provider_summary,
        expires_at=expires_at,
        raw_metadata=raw_metadata,
    )


async def _seed_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    content_hash: str,
    fetched_at: datetime = NOW,
    entries: list[CatalogEntryInput],
) -> None:
    async with session_factory() as session, session.begin():
        await create_or_get_completed_snapshot(
            session,
            content_hash=content_hash,
            fetched_at=fetched_at,
            source_etag=None,
            source_version=None,
            entries=entries,
        )


async def test_human_models_combines_selector_filters_and_never_leaks_internal_fields(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_snapshot(
        session_factory,
        content_hash="a" * 64,
        entries=[
            _entry(
                "anthropic/claude-safe",
                display_name="Claude Safe",
                context_length=200_000,
                prompt_price=100,
                completion_price=200,
                structured=True,
                privacy="zdr",
                availability="available",
                secret_marker="env:must-never-appear",
            ),
            _entry(
                "anthropic/claude-expensive",
                display_name="Claude Expensive",
                context_length=200_000,
                prompt_price=10_000,
                completion_price=20_000,
                structured=True,
                privacy="zdr",
                availability="available",
            ),
            _entry(
                "openai/gpt-other",
                display_name="GPT Other",
                context_length=200_000,
                prompt_price=100,
                completion_price=200,
                structured=True,
                privacy="zdr",
                availability="available",
            ),
        ],
    )

    response = await client.get(
        "/human/models",
        params={
            "search": "Claude",
            "provider": "anthropic",
            "max_price_micros_per_million": 300,
            "min_context_length": 150_000,
            "requires_structured_output": "true",
            "tier": "unverified",
            "privacy": "zdr",
            "availability": "available",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["canonical_slug"] for item in body["items"]] == ["anthropic/claude-safe"]
    item = body["items"][0]
    assert item == {
        "canonical_slug": "anthropic/claude-safe",
        "provider": "anthropic",
        "display_name": "Claude Safe",
        "description": "Description for anthropic/claude-safe",
        "modalities": ["text"],
        "context_length": 200_000,
        "pricing_per_million_micros": {
            "completion_per_million": 200,
            "prompt_per_million": 100,
        },
        "supported_capabilities": ["response_format", "temperature"],
        "supports_structured_output": True,
        "qualification_tier": "unverified",
        "qualification_reasons": ["probe_evidence_missing"],
        "casual_visible": True,
        "privacy": "zdr",
        "availability": "available",
        "availability_reason": "source_available",
        "expires_at": None,
        "platform_eligible": False,
        "platform_ineligibility_reasons": ["qualification_unverified"],
        "observed_latency_ms": None,
        "observed_throughput_tokens_per_second": None,
        "open_weights": None,
        "frontier": None,
        "requires_byok": False,
        "catalog_freshness": {
            "content_hash": "a" * 64,
            "fetched_at": "2026-07-12T12:00:00Z",
            "state": "current",
            "is_stale": False,
        },
    }
    assert body["catalog"] == item["catalog_freshness"]
    rendered = json.dumps(body, sort_keys=True)
    for forbidden in (
        "must-never-appear",
        "auth_secret_ref",
        "raw_metadata",
        "provider_summary",
        "catalog_snapshot_id",
        '"id"',
    ):
        assert forbidden not in rendered


async def test_human_models_projects_pure_probe_qualification_without_exposing_evidence(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_snapshot(
        session_factory,
        content_hash="d" * 64,
        entries=[
            _entry(
                "openai/qualified",
                structured=True,
                availability="available",
                provider_pinned=True,
                provider_fingerprint="provider-v1",
            )
        ],
    )
    async with session_factory() as session, session.begin():
        entry = await session.scalar(
            select(ModelCatalogEntry).where(ModelCatalogEntry.canonical_slug == "openai/qualified")
        )
        assert entry is not None
        session.add_all(
            [
                ModelProbeEvidence(
                    catalog_snapshot_id=entry.catalog_snapshot_id,
                    model_catalog_entry_id=entry.id,
                    prompt_hash=f"{index:064x}",
                    schema_hash=f"{index + 10:064x}",
                    probe_suite_version=f"v{index}",
                    status="passed",
                    evidence={
                        "schema_coercion_succeeded": True,
                        "strict_schema_observed": True,
                        "tokens_used": 800,
                        "latency_ms": 900,
                        "drift_resolved": True,
                        "provider_fingerprint": "provider-v1",
                    },
                    observed_at=NOW,
                )
                for index in range(1, 4)
            ]
        )

    response = await client.get("/human/models", params={"tier": "ranked_qualified"})

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["canonical_slug"] == "openai/qualified"
    assert item["qualification_tier"] == "ranked_qualified"
    assert item["qualification_reasons"] == []
    assert item["casual_visible"] is True
    rendered = json.dumps(response.json(), sort_keys=True)
    for forbidden in ("schema_coercion_succeeded", "provider_fingerprint", "drift_resolved"):
        assert forbidden not in rendered


async def test_human_models_projects_safe_explorer_facts_and_filters(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Explorer metadata stays bounded to derived facts rather than raw catalog fields."""
    await _seed_snapshot(
        session_factory,
        content_hash="e" * 64,
        entries=[
            _entry(
                "mistral/open-lab",
                structured=True,
                availability="available",
                provider_pinned=True,
                provider_fingerprint="provider-v1",
            ),
            _entry("openai/closed-lab", structured=True, availability="available"),
        ],
    )
    async with session_factory() as session, session.begin():
        entry = await session.scalar(
            select(ModelCatalogEntry).where(ModelCatalogEntry.canonical_slug == "mistral/open-lab")
        )
        assert entry is not None
        entry.raw_metadata = {
            **entry.raw_metadata,
            "open_weights": True,
            "frontier": True,
            "auth_secret_ref": "never-project",
        }
        session.add_all(
            [
                ModelProbeEvidence(
                    catalog_snapshot_id=entry.catalog_snapshot_id,
                    model_catalog_entry_id=entry.id,
                    prompt_hash=f"{index:064x}",
                    schema_hash=f"{index + 20:064x}",
                    probe_suite_version=f"explorer-v{index}",
                    status="passed",
                    evidence={
                        "schema_coercion_succeeded": True,
                        "strict_schema_observed": True,
                        "tokens_used": 900,
                        "latency_ms": latency,
                        "drift_resolved": True,
                        "provider_fingerprint": "provider-v1",
                    },
                    observed_at=NOW,
                )
                for index, latency in enumerate((900, 1_100, 1_300), start=1)
            ]
        )

    response = await client.get(
        "/human/models",
        params={
            "open_weight": "true",
            "frontier": "true",
            "platform_eligible": "true",
        },
    )

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["canonical_slug"] for item in items] == ["mistral/open-lab"]
    assert items[0]["open_weights"] is True
    assert items[0]["frontier"] is True
    assert items[0]["observed_latency_ms"] == 1_100
    assert items[0]["observed_throughput_tokens_per_second"] == 818
    assert items[0]["requires_byok"] is False
    rendered = json.dumps(response.json(), sort_keys=True)
    for forbidden in ("never-project", "auth_secret_ref", "tokens_used", "provider_fingerprint"):
        assert forbidden not in rendered


async def test_human_models_keyset_cursor_stays_bound_to_original_snapshot(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_snapshot(
        session_factory,
        content_hash="b" * 64,
        entries=[
            _entry("openai/bravo", availability="available"),
            _entry("openai/charlie", availability="available"),
        ],
    )

    first = await client.get("/human/models", params={"limit": 1})
    assert first.status_code == 200, first.text
    assert [item["canonical_slug"] for item in first.json()["items"]] == ["openai/bravo"]
    cursor = first.json()["next_cursor"]
    assert cursor is not None

    await _seed_snapshot(
        session_factory,
        content_hash="c" * 64,
        fetched_at=NOW + timedelta(seconds=1),
        entries=[
            _entry("openai/alpha", availability="available"),
            _entry("openai/bravo", availability="available"),
            _entry("openai/charlie", availability="available"),
        ],
    )

    second = await client.get("/human/models", params={"limit": 1, "cursor": cursor})
    assert second.status_code == 200, second.text
    assert [item["canonical_slug"] for item in second.json()["items"]] == ["openai/charlie"]
    assert second.json()["catalog"]["content_hash"] == "b" * 64

    newest = await client.get("/human/models", params={"limit": 1})
    assert newest.status_code == 200
    assert [item["canonical_slug"] for item in newest.json()["items"]] == ["openai/alpha"]


async def test_human_models_escapes_free_text_wildcards(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_snapshot(
        session_factory,
        content_hash="d" * 64,
        entries=[
            _entry("openai/literal", display_name="100%_ literal", availability="available"),
            _entry("openai/wildcard", display_name="100ZZ literal", availability="available"),
        ],
    )

    response = await client.get("/human/models", params={"search": "100%_"})

    assert response.status_code == 200, response.text
    assert [item["canonical_slug"] for item in response.json()["items"]] == ["openai/literal"]


async def test_human_models_marks_unknown_stale_and_deprecated_entries_ineligible(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import padrino.api.routes.model_catalog as catalog_routes

    monkeypatch.setattr(catalog_routes, "_now_utc", lambda: NOW)
    await _seed_snapshot(
        session_factory,
        content_hash="e" * 64,
        entries=[
            _entry("openai/unknown", availability=None),
            _entry(
                "openai/deprecated",
                availability="available",
                expires_at=NOW - timedelta(seconds=1),
            ),
        ],
    )

    current = await client.get("/human/models")
    assert current.status_code == 200, current.text
    by_slug = {item["canonical_slug"]: item for item in current.json()["items"]}
    assert by_slug["openai/unknown"]["availability"] == "unknown"
    assert by_slug["openai/deprecated"]["availability"] == "deprecated"
    assert not by_slug["openai/unknown"]["platform_eligible"]
    assert not by_slug["openai/deprecated"]["platform_eligible"]

    monkeypatch.setattr(
        catalog_routes,
        "_now_utc",
        lambda: NOW + timedelta(hours=2),
    )
    stale = await client.get("/human/models", params={"availability": "stale"})
    assert stale.status_code == 200, stale.text
    assert stale.json()["catalog"]["state"] == "stale"
    assert stale.json()["catalog"]["is_stale"] is True
    assert [item["availability"] for item in stale.json()["items"]] == ["stale", "stale"]
    assert all(not item["platform_eligible"] for item in stale.json()["items"])


async def test_human_models_rate_limits_anonymous_reads_but_session_reads_get_identical_facts(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PADRINO_RATE_LIMIT_ANONYMOUS_PER_MINUTE", "1")
    get_settings.cache_clear()
    await _seed_snapshot(
        session_factory,
        content_hash="f" * 64,
        entries=[_entry("openai/one", availability="available")],
    )

    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as anonymous_client:
        first = await anonymous_client.get("/human/models")
        assert first.status_code == 200, first.text
        limited = await anonymous_client.get("/human/models")
        assert limited.status_code == 429
        assert limited.json()["detail"] == "rate_limited"

        guest = await anonymous_client.post("/human/guest")
        assert guest.status_code == 201
        token = guest.cookies.get(HUMAN_SESSION_COOKIE)
        assert token is not None
        anonymous_client.cookies.set(HUMAN_SESSION_COOKIE, token)
        authenticated = await anonymous_client.get("/human/models")
        assert authenticated.status_code == 200, authenticated.text
        assert authenticated.json() == first.json()
