"""Synchronize and safely publish immutable OpenRouter model catalogs.

The OpenRouter Models API is an impure management-plane dependency.  This
module validates an entire response before writing anything, publishes only
complete content-addressed snapshots, and always falls back to the most recent
complete catalog when discovery is unavailable.  It intentionally never makes
game launch depend on a live catalog request.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import CatalogSnapshot, ModelCatalogEntry
from padrino.db.repositories.model_catalog_snapshots import (
    CatalogEntryInput,
    CatalogSnapshotPersistenceError,
    create_or_get_completed_snapshot,
    get_latest_complete_snapshot,
)
from padrino.llm.secrets import SecretResolutionError, resolve_secret

if TYPE_CHECKING:
    from padrino.settings import Settings

_USD_MICROS_PER_USD = Decimal(1_000_000)
_TOKENS_PER_MILLION = Decimal(1_000_000)
_NON_TOKEN_PRICING_KEYS = frozenset({"image", "request", "web_search"})
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


class CatalogSyncState(StrEnum):
    """One safe catalog-sync outcome for operators and runtime readers."""

    SYNCED = "synced"
    UNCHANGED = "unchanged"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class CatalogFreshness(BaseModel):
    """Last-good catalog freshness without transport or credential detail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checked_at: datetime
    last_good_fetched_at: datetime | None
    is_stale: bool
    reason: str


class CatalogOperatorSummary(BaseModel):
    """The credential-free CLI/admin payload for one sync attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    model_count: int
    content_hash: str | None
    fetched_at: str | None


class CatalogSyncResult(BaseModel):
    """Safe result of one discovery attempt.

    The value deliberately carries no request headers, source error text, or
    credential references.  Those values can contain sensitive deployment
    context and are unnecessary for the CLI, API, or catalog browse path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: CatalogSyncState
    snapshot_id: UUID | None
    content_hash: str | None
    model_count: int
    fetched_at: datetime | None
    freshness: CatalogFreshness

    def operator_summary(self) -> CatalogOperatorSummary:
        """Return the intentionally small CLI/admin response contract."""
        return CatalogOperatorSummary(
            status=self.state.value,
            model_count=self.model_count,
            content_hash=self.content_hash,
            fetched_at=_format_datetime(self.fetched_at),
        )


class CatalogPayloadError(ValueError):
    """Raised for an OpenRouter Models API payload that cannot be published."""


class _NormalizedCatalog(BaseModel):
    """Fully validated source response and its text-output play projection."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    content_hash: str
    entries: tuple[CatalogEntryInput, ...]


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _normalized_key(value: object) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _safe_json_value(value: object) -> Any:
    """Copy finite JSON-compatible source metadata and reject unsafe keys."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise CatalogPayloadError("catalog_schema_invalid")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, nested in value.items():
            if not isinstance(key, str) or _normalized_key(key) in _FORBIDDEN_METADATA_KEYS:
                raise CatalogPayloadError("catalog_schema_invalid")
            copied[key] = _safe_json_value(nested)
        return copied
    if isinstance(value, list):
        return [_safe_json_value(item) for item in value]
    raise CatalogPayloadError("catalog_schema_invalid")


def _required_string(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CatalogPayloadError("catalog_schema_invalid")
    return value.strip()


def _optional_string(row: Mapping[str, Any], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CatalogPayloadError("catalog_schema_invalid")
    return value


def _string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CatalogPayloadError("catalog_schema_invalid")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise CatalogPayloadError("catalog_schema_invalid")
        normalized.append(item.strip().lower())
    if len(normalized) != len(set(normalized)):
        raise CatalogPayloadError("catalog_schema_invalid")
    return tuple(sorted(normalized))


def _optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CatalogPayloadError("catalog_schema_invalid")
    return value


def _parse_expiry(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CatalogPayloadError("catalog_schema_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CatalogPayloadError("catalog_schema_invalid") from exc
    if parsed.tzinfo is None:
        raise CatalogPayloadError("catalog_schema_invalid")
    return parsed.astimezone(UTC)


def _normalize_pricing(value: object) -> tuple[dict[str, str], dict[str, int]]:
    if value is None:
        return {}, {}
    if not isinstance(value, Mapping):
        raise CatalogPayloadError("catalog_schema_invalid")
    strings: dict[str, str] = {}
    micros: dict[str, int] = {}
    for raw_key, raw_price in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise CatalogPayloadError("catalog_schema_invalid")
        if isinstance(raw_price, bool) or not isinstance(raw_price, (str, int, float)):
            raise CatalogPayloadError("catalog_schema_invalid")
        try:
            decimal_price = Decimal(str(raw_price))
        except InvalidOperation as exc:
            raise CatalogPayloadError("catalog_schema_invalid") from exc
        if not decimal_price.is_finite() or decimal_price < 0:
            raise CatalogPayloadError("catalog_schema_invalid")
        key = raw_key.strip()
        strings[key] = format(decimal_price, "f")
        multiplier = (
            _USD_MICROS_PER_USD
            if key in _NON_TOKEN_PRICING_KEYS
            else _USD_MICROS_PER_USD * _TOKENS_PER_MILLION
        )
        stored_key = key if key in _NON_TOKEN_PRICING_KEYS else f"{key}_per_million"
        micros[stored_key] = int((decimal_price * multiplier).to_integral_value(ROUND_CEILING))
    return dict(sorted(strings.items())), dict(sorted(micros.items()))


def _normalize_model(row: object) -> tuple[str, CatalogEntryInput | None, dict[str, Any]]:
    if not isinstance(row, Mapping):
        raise CatalogPayloadError("catalog_schema_invalid")
    raw = _safe_json_value(row)
    if not isinstance(raw, dict):  # Defensive; mappings normalize to dictionaries above.
        raise CatalogPayloadError("catalog_schema_invalid")
    _required_string(raw, "id")
    slug = _required_string(raw, "canonical_slug")
    if not _CANONICAL_SLUG.fullmatch(slug):
        raise CatalogPayloadError("catalog_schema_invalid")
    name = _required_string(raw, "name")
    description = _optional_string(raw, "description")
    architecture = raw.get("architecture")
    if not isinstance(architecture, Mapping):
        raise CatalogPayloadError("catalog_schema_invalid")
    output_modalities = _string_sequence(architecture.get("output_modalities"))
    input_modalities = architecture.get("input_modalities")
    if input_modalities is not None:
        _string_sequence(input_modalities)
    pricing_strings, pricing_micros = _normalize_pricing(raw.get("pricing"))
    context_length = _optional_positive_int(raw.get("context_length"))
    expires_at = _parse_expiry(raw.get("expires_at"))
    supported = raw.get("supported_parameters", [])
    supported_parameters = _string_sequence(supported)
    top_provider = raw.get("top_provider", {})
    if top_provider is None:
        top_provider = {}
    if not isinstance(top_provider, Mapping):
        raise CatalogPayloadError("catalog_schema_invalid")
    provider_summary = _safe_json_value(top_provider)
    if not isinstance(
        provider_summary, dict
    ):  # Defensive; mappings normalize to dictionaries above.
        raise CatalogPayloadError("catalog_schema_invalid")
    entry: CatalogEntryInput | None = None
    if "text" in output_modalities:
        entry = CatalogEntryInput(
            canonical_slug=slug,
            display_name=name,
            description=description,
            modalities=output_modalities,
            context_length=context_length,
            pricing_strings=pricing_strings,
            pricing_micros=pricing_micros,
            supported_parameters=supported_parameters,
            provider_summary=provider_summary,
            expires_at=expires_at,
            raw_metadata=raw,
        )
    return slug, entry, raw


def normalize_openrouter_catalog(payload: object) -> _NormalizedCatalog:
    """Validate every source model before deriving the text-only play catalog."""
    if not isinstance(payload, Mapping):
        raise CatalogPayloadError("catalog_schema_invalid")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise CatalogPayloadError("catalog_schema_invalid")
    source_models: list[tuple[str, dict[str, Any]]] = []
    text_entries: list[CatalogEntryInput] = []
    seen_slugs: set[str] = set()
    for row in data:
        slug, entry, raw = _normalize_model(row)
        if slug in seen_slugs:
            raise CatalogPayloadError("catalog_schema_invalid")
        seen_slugs.add(slug)
        source_models.append((slug, raw))
        if entry is not None:
            text_entries.append(entry)
    if not text_entries:
        raise CatalogPayloadError("catalog_no_text_models")
    canonical_payload = {
        "models": [
            {"canonical_slug": slug, "metadata": raw}
            for slug, raw in sorted(source_models, key=lambda item: item[0])
        ]
    }
    try:
        encoded = json.dumps(
            canonical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:  # pragma: no cover - source was copied above
        raise CatalogPayloadError("catalog_schema_invalid") from exc
    return _NormalizedCatalog(
        content_hash=hashlib.sha256(encoded).hexdigest(),
        entries=tuple(sorted(text_entries, key=lambda entry: entry.canonical_slug)),
    )


async def _last_good_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
) -> CatalogSnapshot | None:
    async with session_factory() as session:
        return await get_latest_complete_snapshot(session)


def _result_from_snapshot(
    *,
    state: CatalogSyncState,
    snapshot: CatalogSnapshot | None,
    model_count: int,
    checked_at: datetime,
    reason: str,
) -> CatalogSyncResult:
    fetched_at = _as_utc(snapshot.fetched_at) if snapshot is not None else None
    return CatalogSyncResult(
        state=state,
        snapshot_id=snapshot.id if snapshot is not None else None,
        content_hash=snapshot.content_hash if snapshot is not None else None,
        model_count=model_count if snapshot is not None else 0,
        fetched_at=fetched_at,
        freshness=CatalogFreshness(
            checked_at=checked_at,
            last_good_fetched_at=fetched_at,
            is_stale=state in {CatalogSyncState.STALE, CatalogSyncState.UNAVAILABLE},
            reason=reason,
        ),
    )


async def _last_good_result(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    checked_at: datetime,
    reason: str,
) -> CatalogSyncResult:
    snapshot = await _last_good_snapshot(session_factory)
    if snapshot is None:
        return _result_from_snapshot(
            state=CatalogSyncState.UNAVAILABLE,
            snapshot=None,
            model_count=0,
            checked_at=checked_at,
            reason=reason,
        )
    async with session_factory() as session:
        model_count = int(
            await session.scalar(
                select(func.count())
                .select_from(ModelCatalogEntry)
                .where(ModelCatalogEntry.catalog_snapshot_id == snapshot.id)
            )
            or 0
        )
    return _result_from_snapshot(
        state=CatalogSyncState.STALE,
        snapshot=snapshot,
        model_count=model_count,
        checked_at=checked_at,
        reason=reason,
    )


class CatalogSynchronizer:
    """One serialized async synchronizer backed by a shared session factory."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
        models_url: str,
        management_key: str | None = None,
        now: Callable[[], datetime] = _now_utc,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._models_url = models_url.rstrip("/")
        self._management_key = management_key
        self._now = now
        self._lock = asyncio.Lock()

    async def sync(self) -> CatalogSyncResult:
        """Fetch, validate, and atomically publish one source snapshot.

        Calls sharing a synchronizer serialize before they fetch.  Separate
        processes are deduplicated by the database's unique content hash; a
        short deterministic retry handles SQLite's writer-lock window.
        """
        async with self._lock:
            return await self._sync_locked()

    async def _sync_locked(self) -> CatalogSyncResult:
        checked_at = _as_utc(self._now())
        last_good = await _last_good_snapshot(self._session_factory)
        headers = {"Accept": "application/json"}
        if last_good is not None and last_good.source_etag:
            headers["If-None-Match"] = last_good.source_etag
        if self._management_key is not None:
            headers["Authorization"] = f"Bearer {self._management_key}"
        try:
            response = await self._client.get(
                self._models_url,
                params={"output_modalities": "all"},
                headers=headers,
            )
        except httpx.HTTPError:
            return await _last_good_result(
                self._session_factory,
                checked_at=checked_at,
                reason="network_error",
            )
        if response.status_code == 304:
            if last_good is None:
                return await _last_good_result(
                    self._session_factory,
                    checked_at=checked_at,
                    reason="not_modified_without_snapshot",
                )
            return _result_from_snapshot(
                state=CatalogSyncState.UNCHANGED,
                snapshot=last_good,
                model_count=await self._count_entries(last_good.id),
                checked_at=checked_at,
                reason="not_modified",
            )
        if response.status_code == 429:
            return await _last_good_result(
                self._session_factory,
                checked_at=checked_at,
                reason="rate_limited",
            )
        if response.status_code < 200 or response.status_code >= 300:
            return await _last_good_result(
                self._session_factory,
                checked_at=checked_at,
                reason="source_http_error",
            )
        try:
            normalized = normalize_openrouter_catalog(response.json())
        except (CatalogPayloadError, CatalogSnapshotPersistenceError, ValueError):
            return await _last_good_result(
                self._session_factory,
                checked_at=checked_at,
                reason="schema_invalid",
            )
        source_etag = response.headers.get("etag")
        source_version = response.headers.get("x-openrouter-version") or response.headers.get(
            "last-modified"
        )
        return await self._publish(
            normalized,
            checked_at=checked_at,
            source_etag=source_etag,
            source_version=source_version,
        )

    async def _publish(
        self,
        normalized: _NormalizedCatalog,
        *,
        checked_at: datetime,
        source_etag: str | None,
        source_version: str | None,
    ) -> CatalogSyncResult:
        for attempt in range(3):
            try:
                async with self._session_factory() as session:
                    async with session.begin():
                        snapshot, created = await create_or_get_completed_snapshot(
                            session,
                            content_hash=normalized.content_hash,
                            fetched_at=checked_at,
                            source_etag=source_etag,
                            source_version=source_version,
                            entries=normalized.entries,
                        )
                    return _result_from_snapshot(
                        state=CatalogSyncState.SYNCED if created else CatalogSyncState.UNCHANGED,
                        snapshot=snapshot,
                        model_count=len(normalized.entries),
                        checked_at=checked_at,
                        reason="current" if created else "content_unchanged",
                    )
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 2:
                    break
                await asyncio.sleep(0.05 * (attempt + 1))
            except CatalogSnapshotPersistenceError:
                break
        return await _last_good_result(
            self._session_factory,
            checked_at=checked_at,
            reason="publish_failed",
        )

    async def _count_entries(self, snapshot_id: UUID) -> int:
        async with self._session_factory() as session:
            value = await session.scalar(
                select(func.count())
                .select_from(ModelCatalogEntry)
                .where(ModelCatalogEntry.catalog_snapshot_id == snapshot_id)
            )
        return int(value or 0)


async def sync_openrouter_catalog(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    now: Callable[[], datetime] = _now_utc,
) -> CatalogSyncResult:
    """Run a settings-backed sync while keeping management credentials opaque."""
    checked_at = _as_utc(now())
    management_key: str | None = None
    if settings.padrino_openrouter_management_key_ref is not None:
        try:
            management_key = resolve_secret(settings.padrino_openrouter_management_key_ref)
        except SecretResolutionError:
            return await _last_good_result(
                session_factory,
                checked_at=checked_at,
                reason="credential_unavailable",
            )
    owns_client = client is None
    resolved_client = client or httpx.AsyncClient(
        timeout=settings.padrino_openrouter_timeout_seconds
    )
    try:
        return await CatalogSynchronizer(
            session_factory=session_factory,
            client=resolved_client,
            models_url=f"{settings.padrino_openrouter_base_url}/models",
            management_key=management_key,
            now=now,
        ).sync()
    finally:
        if owns_client:
            await resolved_client.aclose()


__all__ = [
    "CatalogFreshness",
    "CatalogOperatorSummary",
    "CatalogPayloadError",
    "CatalogSyncResult",
    "CatalogSyncState",
    "CatalogSynchronizer",
    "normalize_openrouter_catalog",
    "sync_openrouter_catalog",
]
