"""Human-selector-facing immutable OpenRouter catalog reads (US-344).

The route deliberately reads one persisted, complete catalog snapshot.  A
cursor carries that snapshot's content hash plus the final canonical slug, so a
new synchronizer publication cannot reorder or skip a selector's later pages.
The response is intentionally a small allow-list projection: it never returns
catalog-entry/snapshot UUIDs, raw source metadata, provider configuration, or
credential references.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.auth import _get_auth_settings, _get_rate_limiter
from padrino.api.deps import get_session
from padrino.api.human_auth import HumanPrincipalContext, get_human_context
from padrino.db.models import CatalogSnapshot, ModelCatalogEntry
from padrino.db.repositories.model_catalog_snapshots import get_latest_complete_snapshot
from padrino.settings import Settings

router = APIRouter()

_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_SLUG = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_CURSOR_VERSION = 1
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200

QualificationTier = Literal[
    "unverified",
    "playable",
    "verified_structured",
    "ranked_qualified",
]
PrivacyTier = Literal["zdr", "standard", "unknown"]
Availability = Literal["available", "unavailable", "unknown", "stale", "deprecated"]
CatalogState = Literal["current", "stale", "unavailable"]


class CatalogFreshnessResponse(BaseModel):
    """Safe timestamp/hash facts for the snapshot backing a selector page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content_hash: str | None
    fetched_at: datetime | None
    state: CatalogState
    is_stale: bool


class PlayerModelCatalogEntry(BaseModel):
    """Allow-listed catalog entry that a player can select by canonical slug."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical_slug: str
    provider: str
    display_name: str
    description: str | None
    modalities: list[str]
    context_length: int | None
    pricing_per_million_micros: dict[str, int]
    supported_capabilities: list[str]
    supports_structured_output: bool
    qualification_tier: QualificationTier
    qualification_reasons: list[str]
    privacy: PrivacyTier
    availability: Availability
    availability_reason: str
    expires_at: datetime | None
    platform_eligible: bool
    platform_ineligibility_reasons: list[str]
    catalog_freshness: CatalogFreshnessResponse


class PlayerModelCatalogPage(BaseModel):
    """A keyset page of selector-safe model facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[PlayerModelCatalogEntry]
    next_cursor: str | None
    catalog: CatalogFreshnessResponse


class _ModelCursorError(ValueError):
    """Raised for a malformed or stale model-selector cursor."""


def _now_utc() -> datetime:
    """Return the API-shell clock in UTC (kept injectable in route tests)."""
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))


def _encode_cursor(*, content_hash: str, canonical_slug: str) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "content_hash": content_hash,
        "canonical_slug": canonical_slug,
    }
    return _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        decoded = json.loads(_b64decode(cursor).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise _ModelCursorError("cursor encoding invalid") from exc
    if not isinstance(decoded, dict):
        raise _ModelCursorError("cursor payload invalid")
    if decoded.get("v") != _CURSOR_VERSION:
        raise _ModelCursorError("cursor version invalid")
    content_hash = decoded.get("content_hash")
    canonical_slug = decoded.get("canonical_slug")
    if not isinstance(content_hash, str) or not _CONTENT_HASH.fullmatch(content_hash):
        raise _ModelCursorError("cursor hash invalid")
    if not isinstance(canonical_slug, str) or not _CANONICAL_SLUG.fullmatch(canonical_slug):
        raise _ModelCursorError("cursor slug invalid")
    return content_hash, canonical_slug


def _invalid_cursor() -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_cursor")


def _provider_from_slug(canonical_slug: str) -> str:
    provider, separator, _ = canonical_slug.partition("/")
    if not separator:
        return "unknown"
    return provider


def _source_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


def _privacy_from_entry(entry: ModelCatalogEntry) -> PrivacyTier:
    """Project only a conservative privacy tier from source-safe metadata."""
    sources = (entry.provider_summary, entry.raw_metadata)
    for source in sources:
        if source.get("zdr") is True or source.get("zero_data_retention") is True:
            return "zdr"
        for key in ("privacy", "privacy_tier", "data_policy"):
            value = _source_text(source.get(key))
            if value in {"zdr", "zero_data_retention", "zero_retention"}:
                return "zdr"
            if value in {"standard", "retained", "provider_default"}:
                return "standard"
    return "unknown"


def _source_availability(entry: ModelCatalogEntry) -> tuple[Availability, str]:
    """Read an explicit source status, leaving absent/unrecognized data unknown."""
    for source in (entry.raw_metadata, entry.provider_summary):
        value = _source_text(source.get("availability") or source.get("status"))
        if value in {"available", "active", "enabled"}:
            return "available", "source_available"
        if value in {"unavailable", "disabled", "offline", "inactive"}:
            return "unavailable", "source_unavailable"
        if value in {"deprecated", "retired", "sunset"}:
            return "deprecated", "source_deprecated"
    return "unknown", "source_status_unknown"


def _availability_for_entry(
    entry: ModelCatalogEntry,
    *,
    freshness: CatalogFreshnessResponse,
    now: datetime,
) -> tuple[Availability, str]:
    """Prioritize stale catalog evidence over any older per-entry source fact."""
    if freshness.is_stale:
        return "stale", "catalog_stale"
    if entry.expires_at is not None and _as_utc(entry.expires_at) <= now:
        return "deprecated", "expired"
    return _source_availability(entry)


def _supports_structured_output(entry: ModelCatalogEntry) -> bool:
    return bool(
        {"response_format", "json_schema", "structured_outputs"}
        & {str(parameter).lower() for parameter in entry.supported_parameters}
    )


def _qualification_for_entry(entry: ModelCatalogEntry) -> tuple[QualificationTier, list[str]]:
    """Return the safe pre-probe default until US-345/346 publish evidence policy."""
    del entry
    return "unverified", ["no_compatibility_evidence"]


def _pricing_per_million(entry: ModelCatalogEntry) -> dict[str, int]:
    return {
        key: value
        for key, value in sorted(entry.pricing_micros.items())
        if key.endswith("_per_million") and isinstance(value, int) and not isinstance(value, bool)
    }


def _platform_eligibility(
    *,
    availability: Availability,
    qualification_tier: QualificationTier,
) -> tuple[bool, list[str]]:
    """Fail closed while discovery/probe state is unknown, stale, or deprecated."""
    reasons: list[str] = []
    if availability != "available":
        reasons.append(f"availability_{availability}")
    if qualification_tier == "unverified":
        reasons.append("qualification_unverified")
    return not reasons, reasons


def _project_entry(
    entry: ModelCatalogEntry,
    *,
    freshness: CatalogFreshnessResponse,
    now: datetime,
) -> PlayerModelCatalogEntry:
    availability, availability_reason = _availability_for_entry(
        entry,
        freshness=freshness,
        now=now,
    )
    qualification_tier, qualification_reasons = _qualification_for_entry(entry)
    platform_eligible, platform_ineligibility_reasons = _platform_eligibility(
        availability=availability,
        qualification_tier=qualification_tier,
    )
    return PlayerModelCatalogEntry(
        canonical_slug=entry.canonical_slug,
        provider=_provider_from_slug(entry.canonical_slug),
        display_name=entry.display_name,
        description=entry.description,
        modalities=list(entry.modalities),
        context_length=entry.context_length,
        pricing_per_million_micros=_pricing_per_million(entry),
        supported_capabilities=sorted(str(value) for value in entry.supported_parameters),
        supports_structured_output=_supports_structured_output(entry),
        qualification_tier=qualification_tier,
        qualification_reasons=qualification_reasons,
        privacy=_privacy_from_entry(entry),
        availability=availability,
        availability_reason=availability_reason,
        expires_at=entry.expires_at,
        platform_eligible=platform_eligible,
        platform_ineligibility_reasons=platform_ineligibility_reasons,
        catalog_freshness=freshness,
    )


def _freshness(
    snapshot: CatalogSnapshot | None,
    *,
    now: datetime,
    settings: Settings,
) -> CatalogFreshnessResponse:
    if snapshot is None:
        return CatalogFreshnessResponse(
            content_hash=None,
            fetched_at=None,
            state="unavailable",
            is_stale=True,
        )
    fetched_at = _as_utc(snapshot.fetched_at)
    stale_after = timedelta(seconds=settings.padrino_openrouter_catalog_sync_interval_seconds)
    is_stale = now > fetched_at + stale_after
    return CatalogFreshnessResponse(
        content_hash=snapshot.content_hash,
        fetched_at=fetched_at,
        state="stale" if is_stale else "current",
        is_stale=is_stale,
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _matches_filters(
    item: PlayerModelCatalogEntry,
    *,
    max_price_micros_per_million: int | None,
    min_context_length: int | None,
    requires_structured_output: bool | None,
    tier: QualificationTier | None,
    privacy: PrivacyTier | None,
    availability: Availability | None,
) -> bool:
    if min_context_length is not None and (
        item.context_length is None or item.context_length < min_context_length
    ):
        return False
    if requires_structured_output is not None and (
        item.supports_structured_output is not requires_structured_output
    ):
        return False
    if tier is not None and item.qualification_tier != tier:
        return False
    if privacy is not None and item.privacy != privacy:
        return False
    if availability is not None and item.availability != availability:
        return False
    if max_price_micros_per_million is not None:
        token_prices = (
            item.pricing_per_million_micros.get("prompt_per_million"),
            item.pricing_per_million_micros.get("completion_per_million"),
        )
        if any(price is None or price > max_price_micros_per_million for price in token_prices):
            return False
    return True


async def _enforce_anonymous_catalog_rate_limit(request: Request) -> None:
    """Limit unauthenticated browsing by an opaque IP-derived namespace."""
    settings = _get_auth_settings(request)
    forwarded_for = request.headers.get("X-Forwarded-For")
    client_ip = (
        forwarded_for.split(",")[0].strip()
        if forwarded_for
        else (request.client.host if request.client is not None else "unknown")
    )
    key_hash = hashlib.sha256(f"human-model-catalog:{client_ip}".encode()).hexdigest()
    allowed, retry_after = await _get_rate_limiter(request).hit(
        key_hash,
        limit_per_minute=settings.padrino_rate_limit_anonymous_per_minute,
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limited",
            headers={"Retry-After": str(max(1, round(retry_after)))},
        )


@router.get("/human/models", response_model=PlayerModelCatalogPage)
async def list_human_models(
    request: Request,
    session: AsyncSession = Depends(get_session),
    human: HumanPrincipalContext | None = Depends(get_human_context),
    search: Annotated[str | None, Query(min_length=1, max_length=160)] = None,
    provider: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
    max_price_micros_per_million: Annotated[int | None, Query(ge=0)] = None,
    min_context_length: Annotated[int | None, Query(ge=1)] = None,
    requires_structured_output: bool | None = None,
    tier: QualificationTier | None = None,
    privacy: PrivacyTier | None = None,
    availability: Availability | None = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> PlayerModelCatalogPage:
    """Return player-selectable facts from one immutable complete snapshot."""
    if human is None:
        await _enforce_anonymous_catalog_rate_limit(request)
    if provider is not None and not _PROVIDER.fullmatch(provider):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid_provider"
        )

    cursor_hash: str | None = None
    cursor_slug: str | None = None
    if cursor is not None:
        try:
            cursor_hash, cursor_slug = _decode_cursor(cursor)
        except _ModelCursorError as exc:
            raise _invalid_cursor() from exc
        snapshot = await session.scalar(
            select(CatalogSnapshot).where(
                CatalogSnapshot.content_hash == cursor_hash,
                CatalogSnapshot.status == "COMPLETE",
            )
        )
        if snapshot is None:
            raise _invalid_cursor()
    else:
        snapshot = await get_latest_complete_snapshot(session)

    settings = _get_auth_settings(request)
    now = _now_utc()
    freshness = _freshness(snapshot, now=now, settings=settings)
    if snapshot is None:
        return PlayerModelCatalogPage(items=[], next_cursor=None, catalog=freshness)

    statement = select(ModelCatalogEntry).where(
        ModelCatalogEntry.catalog_snapshot_id == snapshot.id
    )
    if cursor_slug is not None:
        statement = statement.where(ModelCatalogEntry.canonical_slug > cursor_slug)
    if search is not None:
        pattern = f"%{_escape_like(search.strip())}%"
        statement = statement.where(
            or_(
                ModelCatalogEntry.canonical_slug.ilike(pattern, escape="\\"),
                ModelCatalogEntry.display_name.ilike(pattern, escape="\\"),
            )
        )
    if provider is not None:
        statement = statement.where(
            ModelCatalogEntry.canonical_slug.like(f"{provider.lower()}/%", escape="\\")
        )
    entries = list(
        (await session.execute(statement.order_by(ModelCatalogEntry.canonical_slug))).scalars()
    )
    filtered = [
        projected
        for entry in entries
        if _matches_filters(
            projected := _project_entry(entry, freshness=freshness, now=now),
            max_price_micros_per_million=max_price_micros_per_million,
            min_context_length=min_context_length,
            requires_structured_output=requires_structured_output,
            tier=tier,
            privacy=privacy,
            availability=availability,
        )
    ]
    page = filtered[:limit]
    next_cursor = (
        _encode_cursor(content_hash=snapshot.content_hash, canonical_slug=page[-1].canonical_slug)
        if len(filtered) > limit and page
        else None
    )
    return PlayerModelCatalogPage(items=page, next_cursor=next_cursor, catalog=freshness)


__all__ = [
    "CatalogFreshnessResponse",
    "PlayerModelCatalogEntry",
    "PlayerModelCatalogPage",
    "list_human_models",
    "router",
]
