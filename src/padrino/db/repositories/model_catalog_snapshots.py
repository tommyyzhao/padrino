"""Immutable OpenRouter catalog snapshot persistence helpers.

The OpenRouter management client and catalog normalizer live outside this
repository.  This module accepts only already-normalized discovery output,
rejects any credential/account/player-shaped metadata, and atomically persists
the resulting content-addressed snapshot.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import CatalogSnapshot, ModelCatalogEntry

_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_SLUG = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "authorization_header",
        "request_authorization_header",
        "account_credit",
        "player_id",
        "principal_id",
        "user_id",
    }
)


class CatalogSnapshotPersistenceError(ValueError):
    """Raised when normalized catalog data violates persistence invariants."""


class CatalogEntryInput(BaseModel):
    """One normalized OpenRouter model row ready for immutable persistence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical_slug: str
    display_name: str
    description: str | None
    modalities: tuple[str, ...]
    context_length: int | None
    pricing_strings: dict[str, str]
    pricing_micros: dict[str, int]
    supported_parameters: tuple[str, ...]
    provider_summary: dict[str, Any]
    expires_at: datetime | None
    raw_metadata: dict[str, Any]

    @field_validator("canonical_slug")
    @classmethod
    def _valid_canonical_slug(cls, value: str) -> str:
        if not _CANONICAL_SLUG.fullmatch(value):
            raise ValueError("canonical_slug must be a lowercase OpenRouter slug")
        return value

    @field_validator("display_name")
    @classmethod
    def _nonempty_display_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("display_name must not be blank")
        return value

    @field_validator("context_length")
    @classmethod
    def _valid_context_length(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("context_length must be positive when present")
        return value

    @field_validator("pricing_strings")
    @classmethod
    def _valid_pricing_strings(cls, value: dict[str, str]) -> dict[str, str]:
        for key, decimal_string in value.items():
            if not key or not isinstance(decimal_string, str):
                raise ValueError("pricing_strings must map non-empty keys to decimal strings")
            try:
                decimal = Decimal(decimal_string)
            except InvalidOperation as exc:
                raise ValueError("pricing_strings must contain decimal strings") from exc
            if not decimal.is_finite() or decimal < 0:
                raise ValueError("pricing_strings must contain finite non-negative values")
        return value

    @field_validator("pricing_micros")
    @classmethod
    def _valid_pricing_micros(cls, value: dict[str, int]) -> dict[str, int]:
        for key, micros in value.items():
            if not key or isinstance(micros, bool) or not isinstance(micros, int) or micros < 0:
                raise ValueError("pricing_micros must contain non-negative integer micros")
        return value


def _normalized_key(key: object) -> str:
    return str(key).strip().lower().replace("-", "_").replace(" ", "_")


def _assert_safe_metadata(value: object) -> None:
    """Reject catalog metadata that could carry credentials or player state."""
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _normalized_key(key) in _FORBIDDEN_METADATA_KEYS:
                raise CatalogSnapshotPersistenceError("catalog_metadata_forbidden_key")
            _assert_safe_metadata(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _assert_safe_metadata(nested)


def _validate_snapshot_input(
    *,
    content_hash: str,
    entries: Sequence[CatalogEntryInput],
) -> None:
    if not _CONTENT_HASH.fullmatch(content_hash):
        raise CatalogSnapshotPersistenceError("catalog_content_hash_invalid")
    if not entries:
        raise CatalogSnapshotPersistenceError("catalog_snapshot_entries_required")
    slugs = [entry.canonical_slug for entry in entries]
    if len(slugs) != len(set(slugs)):
        raise CatalogSnapshotPersistenceError("catalog_snapshot_duplicate_slug")
    for entry in entries:
        _assert_safe_metadata(entry.raw_metadata)
        _assert_safe_metadata(entry.provider_summary)


async def create_or_get_completed_snapshot(
    session: AsyncSession,
    *,
    content_hash: str,
    fetched_at: datetime,
    source_etag: str | None,
    source_version: str | None,
    entries: Sequence[CatalogEntryInput],
) -> tuple[CatalogSnapshot, bool]:
    """Persist a complete catalog snapshot or return the same-hash snapshot.

    A catalog source can be polled repeatedly, so uniqueness is on the canonical
    content hash rather than fetch time.  Child rows are inserted while the
    snapshot is ``PENDING`` and the final transition to ``COMPLETE`` occurs in
    the same transaction; the migration's database guards then freeze both
    parent and child content.
    """
    _validate_snapshot_input(content_hash=content_hash, entries=entries)
    existing = await session.scalar(
        select(CatalogSnapshot).where(CatalogSnapshot.content_hash == content_hash)
    )
    if existing is not None:
        return existing, False

    snapshot = CatalogSnapshot(
        content_hash=content_hash,
        fetched_at=fetched_at,
        source_etag=source_etag,
        source_version=source_version,
        status="PENDING",
        completed_at=None,
    )
    try:
        async with session.begin_nested():
            session.add(snapshot)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(CatalogSnapshot).where(CatalogSnapshot.content_hash == content_hash)
        )
        if existing is None:
            raise
        return existing, False

    session.add_all(
        [
            ModelCatalogEntry(
                catalog_snapshot_id=snapshot.id,
                canonical_slug=entry.canonical_slug,
                display_name=entry.display_name,
                description=entry.description,
                modalities=list(entry.modalities),
                context_length=entry.context_length,
                pricing_strings=dict(entry.pricing_strings),
                pricing_micros=dict(entry.pricing_micros),
                supported_parameters=list(entry.supported_parameters),
                provider_summary=dict(entry.provider_summary),
                expires_at=entry.expires_at,
                raw_metadata=dict(entry.raw_metadata),
            )
            for entry in entries
        ]
    )
    await session.flush()
    snapshot.status = "COMPLETE"
    snapshot.completed_at = fetched_at
    await session.flush()
    return snapshot, True


async def get_latest_complete_snapshot(session: AsyncSession) -> CatalogSnapshot | None:
    """Return the newest complete catalog snapshot, never an in-flight row.

    Runtime catalog consumers must only ever resolve an immutable ``COMPLETE``
    snapshot.  A synchronizer is free to be fetching or validating a newer
    source response, but it cannot make that partial result observable through
    this read path.
    """
    return cast(
        CatalogSnapshot | None,
        await session.scalar(
            select(CatalogSnapshot)
            .where(CatalogSnapshot.status == "COMPLETE")
            .order_by(desc(CatalogSnapshot.fetched_at), desc(CatalogSnapshot.id))
            .limit(1)
        ),
    )


__all__ = [
    "CatalogEntryInput",
    "CatalogSnapshotPersistenceError",
    "create_or_get_completed_snapshot",
    "get_latest_complete_snapshot",
]
