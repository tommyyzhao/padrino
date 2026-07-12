"""US-342: immutable OpenRouter catalog snapshot persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import CatalogSnapshot, ModelCatalogEntry
from padrino.db.repositories.model_catalog_snapshots import (
    CatalogEntryInput,
    CatalogSnapshotPersistenceError,
    create_or_get_completed_snapshot,
)


def _entry(*, slug: str = "openai/gpt-5-mini") -> CatalogEntryInput:
    return CatalogEntryInput(
        canonical_slug=slug,
        display_name="OpenAI: GPT-5 Mini",
        description="A compact text model.",
        modalities=("text",),
        context_length=128_000,
        pricing_strings={"prompt": "0.00000025", "completion": "0.000002"},
        pricing_micros={"prompt_per_million": 250_000, "completion_per_million": 2_000_000},
        supported_parameters=("temperature", "max_tokens"),
        provider_summary={"max_completion_tokens": 16_384},
        expires_at=None,
        raw_metadata={"architecture": {"modality": "text->text"}},
    )


async def test_completed_snapshot_persists_full_catalog_shape_and_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fetched_at = datetime(2026, 7, 11, 18, 30, tzinfo=UTC)
    content_hash = "a" * 64
    async with session_factory() as session:
        snapshot, created = await create_or_get_completed_snapshot(
            session,
            content_hash=content_hash,
            fetched_at=fetched_at,
            source_etag='W/"catalog-v1"',
            source_version="2026-07-11",
            entries=[_entry()],
        )
        await session.commit()
        snapshot_id = snapshot.id
        assert created is True

    async with session_factory() as session:
        duplicate, created = await create_or_get_completed_snapshot(
            session,
            content_hash=content_hash,
            fetched_at=fetched_at + timedelta(minutes=1),
            source_etag='W/"catalog-v2"',
            source_version="ignored-for-identical-content",
            entries=[_entry(slug="anthropic/claude-4")],
        )
        await session.commit()

        assert created is False
        assert duplicate.id == snapshot_id
        assert duplicate.status == "COMPLETE"
        assert duplicate.fetched_at.replace(tzinfo=UTC) == fetched_at
        assert duplicate.source_etag == 'W/"catalog-v1"'

        entries = list((await session.execute(select(ModelCatalogEntry))).scalars())
        assert len(entries) == 1
        entry = entries[0]
        assert entry.catalog_snapshot_id == snapshot_id
        assert entry.canonical_slug == "openai/gpt-5-mini"
        assert entry.display_name == "OpenAI: GPT-5 Mini"
        assert entry.modalities == ["text"]
        assert entry.context_length == 128_000
        assert entry.pricing_strings == {
            "prompt": "0.00000025",
            "completion": "0.000002",
        }
        assert entry.pricing_micros == {
            "prompt_per_million": 250_000,
            "completion_per_million": 2_000_000,
        }
        assert entry.supported_parameters == ["temperature", "max_tokens"]
        assert entry.provider_summary == {"max_completion_tokens": 16_384}
        assert entry.raw_metadata == {"architecture": {"modality": "text->text"}}


async def test_catalog_repository_rejects_credential_account_and_player_metadata(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        with pytest.raises(
            CatalogSnapshotPersistenceError,
            match="catalog_metadata_forbidden_key",
        ):
            await create_or_get_completed_snapshot(
                session,
                content_hash="f" * 64,
                fetched_at=datetime(2026, 7, 11, tzinfo=UTC),
                source_etag=None,
                source_version=None,
                entries=[
                    _entry(
                        slug="openai/gpt-5-secret",
                    ).model_copy(update={"raw_metadata": {"Authorization": "Bearer secret"}})
                ],
            )
        assert (await session.execute(select(CatalogSnapshot))).scalars().all() == []
