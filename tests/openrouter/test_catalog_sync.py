"""US-343: OpenRouter catalog synchronization and operator triggers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from padrino.api.app import create_app
from padrino.db.models import CatalogSnapshot, ModelCatalogEntry
from padrino.db.repositories.model_catalog_snapshots import (
    CatalogEntryInput,
    create_or_get_completed_snapshot,
    get_latest_complete_snapshot,
)
from padrino.openrouter.catalog_sync import (
    CatalogFreshness,
    CatalogSynchronizer,
    CatalogSyncResult,
    CatalogSyncState,
)

NOW = datetime(2026, 7, 11, 19, 0, tzinfo=UTC)
MODELS_URL = "https://openrouter.example.test/api/v1/models"


def _text_model(*, slug: str = "openai/gpt-5-mini") -> dict[str, Any]:
    return {
        "id": slug,
        "canonical_slug": slug,
        "name": "GPT-5 Mini",
        "description": "Small text model.",
        "architecture": {
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "modality": "text->text",
        },
        "context_length": 128_000,
        "pricing": {
            "prompt": "0.00000025",
            "completion": "0.000002",
            "request": "0",
        },
        "supported_parameters": ["temperature"],
        "top_provider": {"max_completion_tokens": 16_384},
    }


def _image_model() -> dict[str, Any]:
    return {
        "id": "openai/gpt-image-1",
        "canonical_slug": "openai/gpt-image-1",
        "name": "GPT Image",
        "architecture": {
            "input_modalities": ["text"],
            "output_modalities": ["image"],
            "modality": "text->image",
        },
        "context_length": None,
        "pricing": {"image": "0.02"},
        "supported_parameters": [],
        "top_provider": {},
    }


_SyncHandler = Callable[[httpx.Request], httpx.Response]
_AsyncHandler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]


def _client(handler: _SyncHandler | _AsyncHandler) -> AsyncClient:
    return AsyncClient(transport=httpx.MockTransport(handler))


async def test_sync_validates_full_payload_publishes_only_text_models_and_uses_etag(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url == httpx.URL(f"{MODELS_URL}?output_modalities=all")
        return httpx.Response(
            200,
            headers={"ETag": 'W/"models-v1"', "Last-Modified": "Fri, 11 Jul 2026 19:00:00 GMT"},
            json={"data": [_image_model(), _text_model()]},
        )

    async with _client(handler) as client:
        result = await CatalogSynchronizer(
            session_factory=session_factory,
            client=client,
            models_url=MODELS_URL,
            now=lambda: NOW,
        ).sync()

    assert result.state is CatalogSyncState.SYNCED
    assert result.content_hash is not None
    assert result.model_count == 1
    assert result.fetched_at == NOW
    assert result.freshness.reason == "current"
    assert len(calls) == 1
    assert "if-none-match" not in calls[0].headers

    async with session_factory() as session:
        snapshots = list((await session.execute(select(CatalogSnapshot))).scalars())
        entries = list((await session.execute(select(ModelCatalogEntry))).scalars())
    assert len(snapshots) == 1
    assert snapshots[0].status == "COMPLETE"
    assert snapshots[0].source_etag == 'W/"models-v1"'
    assert snapshots[0].source_version == "Fri, 11 Jul 2026 19:00:00 GMT"
    assert [entry.canonical_slug for entry in entries] == ["openai/gpt-5-mini"]
    assert entries[0].supported_parameters == ["temperature"]
    assert entries[0].pricing_strings == {
        "completion": "0.000002",
        "prompt": "0.00000025",
        "request": "0",
    }
    assert entries[0].pricing_micros == {
        "completion_per_million": 2_000_000,
        "prompt_per_million": 250_000,
        "request": 0,
    }


async def test_sync_not_modified_and_failures_serve_the_last_complete_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        snapshot, _ = await create_or_get_completed_snapshot(
            session,
            content_hash="a" * 64,
            fetched_at=NOW,
            source_etag='W/"models-v1"',
            source_version="v1",
            entries=[
                CatalogEntryInput(
                    canonical_slug="openai/gpt-5-mini",
                    display_name="GPT-5 Mini",
                    description=None,
                    modalities=("text",),
                    context_length=128_000,
                    pricing_strings={"prompt": "0.00000025"},
                    pricing_micros={"prompt_per_million": 250_000},
                    supported_parameters=(),
                    provider_summary={},
                    expires_at=None,
                    raw_metadata={"architecture": {"output_modalities": ["text"]}},
                )
            ],
        )
        await session.commit()
        snapshot_id = snapshot.id

    captured_headers: list[dict[str, str]] = []

    def not_modified(request: httpx.Request) -> httpx.Response:
        captured_headers.append(dict(request.headers))
        return httpx.Response(304)

    async with _client(not_modified) as client:
        unchanged = await CatalogSynchronizer(
            session_factory=session_factory,
            client=client,
            models_url=MODELS_URL,
            now=lambda: NOW,
        ).sync()

    assert unchanged.state is CatalogSyncState.UNCHANGED
    assert unchanged.snapshot_id == snapshot_id
    assert unchanged.content_hash == "a" * 64
    assert unchanged.freshness.reason == "not_modified"
    assert captured_headers[0]["if-none-match"] == 'W/"models-v1"'

    def rate_limited(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    async with _client(rate_limited) as client:
        stale = await CatalogSynchronizer(
            session_factory=session_factory,
            client=client,
            models_url=MODELS_URL,
            now=lambda: NOW,
        ).sync()

    assert stale.state is CatalogSyncState.STALE
    assert stale.snapshot_id == snapshot_id
    assert stale.content_hash == "a" * 64
    assert stale.freshness.reason == "rate_limited"
    assert stale.freshness.is_stale is True


async def test_schema_failure_in_any_source_model_does_not_publish_partial_catalog(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        snapshot, _ = await create_or_get_completed_snapshot(
            session,
            content_hash="b" * 64,
            fetched_at=NOW,
            source_etag=None,
            source_version="v1",
            entries=[
                CatalogEntryInput(
                    canonical_slug="openai/gpt-5-mini",
                    display_name="GPT-5 Mini",
                    description=None,
                    modalities=("text",),
                    context_length=128_000,
                    pricing_strings={"prompt": "0.00000025"},
                    pricing_micros={"prompt_per_million": 250_000},
                    supported_parameters=(),
                    provider_summary={},
                    expires_at=None,
                    raw_metadata={"architecture": {"output_modalities": ["text"]}},
                )
            ],
        )
        await session.commit()
        snapshot_id = snapshot.id

    invalid_image = _image_model()
    invalid_image["context_length"] = "not-an-integer"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [_text_model(), invalid_image]})

    async with _client(handler) as client:
        result = await CatalogSynchronizer(
            session_factory=session_factory,
            client=client,
            models_url=MODELS_URL,
            now=lambda: NOW,
        ).sync()

    assert result.state is CatalogSyncState.STALE
    assert result.snapshot_id == snapshot_id
    assert result.freshness.reason == "schema_invalid"
    async with session_factory() as session:
        snapshots = list((await session.execute(select(CatalogSnapshot))).scalars())
    assert [row.id for row in snapshots] == [snapshot_id]


async def test_separate_synchronizers_deduplicate_one_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [_text_model()]})

    async with _client(handler) as first_client, _client(handler) as second_client:
        first, second = await asyncio.gather(
            CatalogSynchronizer(
                session_factory=session_factory,
                client=first_client,
                models_url=MODELS_URL,
                now=lambda: NOW,
            ).sync(),
            CatalogSynchronizer(
                session_factory=session_factory,
                client=second_client,
                models_url=MODELS_URL,
                now=lambda: NOW,
            ).sync(),
        )

    assert {first.state, second.state} == {CatalogSyncState.SYNCED, CatalogSyncState.UNCHANGED}
    async with session_factory() as session:
        snapshots = list((await session.execute(select(CatalogSnapshot))).scalars())
    assert len(snapshots) == 1
    assert snapshots[0].status == "COMPLETE"


async def test_runtime_lookup_ignores_pending_snapshot_and_sync_calls_serialize(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        complete, _ = await create_or_get_completed_snapshot(
            session,
            content_hash="c" * 64,
            fetched_at=NOW,
            source_etag=None,
            source_version="v1",
            entries=[
                CatalogEntryInput(
                    canonical_slug="openai/gpt-5-mini",
                    display_name="GPT-5 Mini",
                    description=None,
                    modalities=("text",),
                    context_length=128_000,
                    pricing_strings={"prompt": "0.00000025"},
                    pricing_micros={"prompt_per_million": 250_000},
                    supported_parameters=(),
                    provider_summary={},
                    expires_at=None,
                    raw_metadata={"architecture": {"output_modalities": ["text"]}},
                )
            ],
        )
        session.add(
            CatalogSnapshot(
                content_hash="d" * 64,
                fetched_at=NOW,
                source_etag=None,
                source_version=None,
                status="PENDING",
                completed_at=None,
            )
        )
        await session.commit()
        latest = await get_latest_complete_snapshot(session)
    assert latest is not None
    assert latest.id == complete.id

    in_flight = 0
    max_in_flight = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return httpx.Response(200, json={"data": [_text_model(slug="anthropic/claude-4")]})

    async with _client(handler) as client:
        synchronizer = CatalogSynchronizer(
            session_factory=session_factory,
            client=client,
            models_url=MODELS_URL,
            now=lambda: NOW,
        )
        first, second = await asyncio.gather(synchronizer.sync(), synchronizer.sync())

    assert max_in_flight == 1
    assert {first.state, second.state} == {CatalogSyncState.SYNCED, CatalogSyncState.UNCHANGED}
    async with session_factory() as session:
        snapshots = list((await session.execute(select(CatalogSnapshot))).scalars())
    assert len(snapshots) == 3
    assert sum(snapshot.status == "COMPLETE" for snapshot in snapshots) == 2


def _safe_result(state: CatalogSyncState = CatalogSyncState.SYNCED) -> CatalogSyncResult:
    return CatalogSyncResult(
        state=state,
        snapshot_id=None,
        content_hash="e" * 64,
        model_count=2,
        fetched_at=NOW,
        freshness=CatalogFreshness(
            checked_at=NOW,
            last_good_fetched_at=NOW,
            is_stale=state is CatalogSyncState.STALE,
            reason="current",
        ),
    )


def test_catalog_sync_cli_prints_safe_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    from padrino import cli

    async def fake_sync(**_: object) -> CatalogSyncResult:
        return _safe_result()

    monkeypatch.setattr("padrino.openrouter.catalog_sync.sync_openrouter_catalog", fake_sync)
    result = CliRunner().invoke(
        cli.app,
        ["catalog", "sync", "--db-url", "sqlite+aiosqlite:///ignored.db"],
    )

    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body == {
        "content_hash": "e" * 64,
        "fetched_at": "2026-07-11T19:00:00Z",
        "model_count": 2,
        "status": "synced",
    }
    assert "authorization" not in result.output.lower()
    assert "secret" not in result.output.lower()


async def test_admin_catalog_sync_is_admin_only_and_returns_safe_summary(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_sync(**_: object) -> CatalogSyncResult:
        return _safe_result(CatalogSyncState.STALE)

    monkeypatch.setattr("padrino.openrouter.catalog_sync.sync_openrouter_catalog", fake_sync)
    app = create_app(session_factory=session_factory, auth_required=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/admin/openrouter/catalog/sync")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "content_hash": "e" * 64,
        "fetched_at": "2026-07-11T19:00:00Z",
        "model_count": 2,
        "status": "stale",
    }
    assert "authorization" not in response.text.lower()
    assert "secret" not in response.text.lower()
