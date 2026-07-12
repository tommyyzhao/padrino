"""Frozen OpenRouter catalog selections for casual lobby AI seats (US-348).

This is an impure assembly seam: it reads immutable catalog/prompt records and
serializes only the server-derived facts launch needs to materialize a build.
Credentials and raw catalog metadata never enter the selection snapshot.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.lobby.model_selection import (
    ModelAutoFillCandidate,
    ModelAutoFillPolicy,
    NotEnoughEligibleModelsError,
    autofill_model_slugs,
)
from padrino.core.model_qualification import ModelQualificationPolicy
from padrino.db.models import (
    CatalogSnapshot,
    ModelCatalogEntry,
    ModelProvider,
    PromptVersion,
)
from padrino.db.repositories.model_catalog_snapshots import get_latest_complete_snapshot
from padrino.llm.adapter import OpenRouterProviderPreferences
from padrino.openrouter.agent_build_materialization import (
    OpenRouterAgentBuildMaterializationError,
    OpenRouterAgentBuildMaterializationRequest,
    get_or_create_openrouter_agent_build,
)

_OPENROUTER_PROVIDER = "openrouter"
_DEFAULT_ADAPTER_VERSION = "openrouter-adapter-v1"


class LobbyModelSelectionError(ValueError):
    """Raised with a stable token when a lobby selection cannot be frozen."""


class RequestedAiModelSelection(BaseModel):
    """Player-safe canonical model choice for one configured AI seat."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seat_index: int = Field(ge=0)
    model_slug: str = Field(min_length=3, max_length=512)
    inference_params: dict[str, object] = Field(default_factory=dict)
    routing_preferences: OpenRouterProviderPreferences | None = None
    funding_source: Literal["platform", "byok"] = "platform"

    @field_validator("model_slug")
    @classmethod
    def _canonical_slug(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("model_slug_required")
        return normalized


class RequestedAutoFillPolicy(BaseModel):
    """Safe player input for filling every unspecified AI seat."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    funding_source: Literal["platform", "byok"] = "platform"
    max_total_price_micros_per_million: int | None = Field(default=None, ge=0)
    inference_params: dict[str, object] = Field(default_factory=dict)
    routing_preferences: OpenRouterProviderPreferences | None = None


class FrozenLobbyModelSelection(BaseModel):
    """Complete secret-free selection snapshot persisted on ``LobbySeat``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["openrouter-lobby-selection-v1"] = "openrouter-lobby-selection-v1"
    canonical_slug: str
    catalog_snapshot_id: uuid.UUID
    catalog_content_hash: str
    catalog_entry_id: uuid.UUID
    provider_id: uuid.UUID
    prompt_version_id: uuid.UUID
    prompt_hash: str
    schema_hash: str
    adapter_version: str
    inference_params: dict[str, object]
    routing_preferences: OpenRouterProviderPreferences
    funding_source: Literal["platform", "byok"]
    funding_owner_ref: str


@dataclass(frozen=True, slots=True)
class ResolvedLobbyModelSelections:
    """Resolved seat snapshots bound to one immutable catalog snapshot."""

    catalog_snapshot_id: uuid.UUID
    selections_by_seat: dict[int, FrozenLobbyModelSelection]


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _schema_hash(response_schema: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(response_schema).encode("utf-8")).hexdigest()


def _status(entry: ModelCatalogEntry) -> str | None:
    for source in (entry.provider_summary, entry.raw_metadata):
        raw = source.get("availability") or source.get("status")
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower().replace("-", "_").replace(" ", "_")
    return None


def _entry_candidate(entry: ModelCatalogEntry) -> ModelAutoFillCandidate:
    is_text = any(str(modality).lower() == "text" for modality in entry.modalities)
    state = _status(entry)
    available = is_text and state not in {"disabled", "deprecated", "retired", "sunset"}
    price_values = [
        value
        for key, value in entry.pricing_micros.items()
        if key in {"prompt_per_million", "completion_per_million"}
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    ]
    total_price = sum(price_values) if price_values else None
    return ModelAutoFillCandidate(
        canonical_slug=entry.canonical_slug,
        platform_eligible=available,
        byok_eligible=available,
        total_price_micros_per_million=total_price,
    )


def _normalized_inference_params(
    *,
    provider_id: uuid.UUID,
    catalog_snapshot_id: uuid.UUID,
    canonical_slug: str,
    prompt_version_id: uuid.UUID,
    inference_params: dict[str, object],
) -> dict[str, object]:
    request = OpenRouterAgentBuildMaterializationRequest(
        provider_id=provider_id,
        catalog_snapshot_id=catalog_snapshot_id,
        canonical_slug=canonical_slug,
        prompt_version_id=prompt_version_id,
        adapter_version=_DEFAULT_ADAPTER_VERSION,
        inference_params=inference_params,
    )
    return dict(request.inference_params)


async def _server_owned_provider_and_prompt(
    session: AsyncSession, *, ruleset_id: str
) -> tuple[ModelProvider, PromptVersion]:
    provider = await session.scalar(
        select(ModelProvider)
        .where(ModelProvider.name == _OPENROUTER_PROVIDER)
        .order_by(ModelProvider.created_at, ModelProvider.id)
        .limit(1)
    )
    if provider is None:
        raise LobbyModelSelectionError("openrouter_provider_not_configured")
    prompt = await session.scalar(
        select(PromptVersion)
        .where(PromptVersion.ruleset_id == ruleset_id)
        .order_by(PromptVersion.created_at.desc(), PromptVersion.id.desc())
        .limit(1)
    )
    if prompt is None:
        raise LobbyModelSelectionError("openrouter_prompt_not_configured")
    return provider, prompt


def _preferences(
    requested: OpenRouterProviderPreferences | None,
    *,
    default_zdr: bool,
) -> OpenRouterProviderPreferences:
    return requested or OpenRouterProviderPreferences(allow_fallbacks=True, zdr=default_zdr)


def _owner_ref(*, funding_source: Literal["platform", "byok"], principal_id: uuid.UUID) -> str:
    if funding_source == "platform":
        return "platform"
    return f"principal:{principal_id}"


def _freeze_selection(
    *,
    entry: ModelCatalogEntry,
    snapshot: CatalogSnapshot,
    provider: ModelProvider,
    prompt: PromptVersion,
    inference_params: dict[str, object],
    preferences: OpenRouterProviderPreferences,
    funding_source: Literal["platform", "byok"],
    principal_id: uuid.UUID,
) -> FrozenLobbyModelSelection:
    candidate = _entry_candidate(entry)
    if not (
        candidate.platform_eligible if funding_source == "platform" else candidate.byok_eligible
    ):
        raise LobbyModelSelectionError("model_unavailable_before_lock")
    return FrozenLobbyModelSelection(
        canonical_slug=entry.canonical_slug,
        catalog_snapshot_id=snapshot.id,
        catalog_content_hash=snapshot.content_hash,
        catalog_entry_id=entry.id,
        provider_id=provider.id,
        prompt_version_id=prompt.id,
        prompt_hash=prompt.prompt_hash,
        schema_hash=_schema_hash(prompt.response_schema),
        adapter_version=_DEFAULT_ADAPTER_VERSION,
        inference_params=_normalized_inference_params(
            provider_id=provider.id,
            catalog_snapshot_id=snapshot.id,
            canonical_slug=entry.canonical_slug,
            prompt_version_id=prompt.id,
            inference_params=inference_params,
        ),
        routing_preferences=preferences,
        funding_source=funding_source,
        funding_owner_ref=_owner_ref(funding_source=funding_source, principal_id=principal_id),
    )


async def resolve_lobby_model_selections(
    session: AsyncSession,
    *,
    ruleset_id: str,
    principal_id: uuid.UUID,
    lobby_seed: str,
    ai_seat_indices: Sequence[int],
    requested_selections: Sequence[RequestedAiModelSelection],
    autofill_policy: RequestedAutoFillPolicy | None,
    default_zdr: bool,
) -> ResolvedLobbyModelSelections:
    """Resolve explicit/auto-filled slot choices against one latest snapshot."""
    allowed_indices = set(ai_seat_indices)
    by_index: dict[int, RequestedAiModelSelection] = {}
    for selection in requested_selections:
        if selection.seat_index not in allowed_indices:
            raise LobbyModelSelectionError("model_selection_not_an_ai_seat")
        if selection.seat_index in by_index:
            raise LobbyModelSelectionError("duplicate_model_selection_seat")
        by_index[selection.seat_index] = selection

    snapshot = await get_latest_complete_snapshot(session)
    if snapshot is None:
        raise LobbyModelSelectionError("catalog_unavailable_before_lock")
    provider, prompt = await _server_owned_provider_and_prompt(session, ruleset_id=ruleset_id)
    entries = list(
        (
            await session.execute(
                select(ModelCatalogEntry)
                .where(ModelCatalogEntry.catalog_snapshot_id == snapshot.id)
                .order_by(ModelCatalogEntry.canonical_slug)
            )
        ).scalars()
    )
    entry_by_slug = {entry.canonical_slug: entry for entry in entries}
    resolved: dict[int, FrozenLobbyModelSelection] = {}
    for seat_index, requested in by_index.items():
        entry = entry_by_slug.get(requested.model_slug)
        if entry is None:
            raise LobbyModelSelectionError("model_unavailable_before_lock")
        resolved[seat_index] = _freeze_selection(
            entry=entry,
            snapshot=snapshot,
            provider=provider,
            prompt=prompt,
            inference_params=requested.inference_params,
            preferences=_preferences(requested.routing_preferences, default_zdr=default_zdr),
            funding_source=requested.funding_source,
            principal_id=principal_id,
        )

    remaining_indices = sorted(allowed_indices - set(resolved))
    if remaining_indices and autofill_policy is None:
        raise LobbyModelSelectionError("model_selection_or_autofill_required")
    if autofill_policy is not None:
        try:
            auto_slugs = autofill_model_slugs(
                lobby_seed=lobby_seed,
                empty_seat_indices=remaining_indices,
                reserved_model_slugs={
                    seat_index: selection.canonical_slug
                    for seat_index, selection in resolved.items()
                },
                candidates=tuple(_entry_candidate(entry) for entry in entries),
                policy=ModelAutoFillPolicy(
                    funding_source=autofill_policy.funding_source,
                    max_total_price_micros_per_million=(
                        autofill_policy.max_total_price_micros_per_million
                    ),
                ),
            )
        except NotEnoughEligibleModelsError as exc:
            raise LobbyModelSelectionError("autofill_pool_unavailable_before_lock") from exc
        for seat_index, slug in auto_slugs.items():
            entry = entry_by_slug[slug]
            resolved[seat_index] = _freeze_selection(
                entry=entry,
                snapshot=snapshot,
                provider=provider,
                prompt=prompt,
                inference_params=autofill_policy.inference_params,
                preferences=_preferences(
                    autofill_policy.routing_preferences, default_zdr=default_zdr
                ),
                funding_source=autofill_policy.funding_source,
                principal_id=principal_id,
            )
    return ResolvedLobbyModelSelections(
        catalog_snapshot_id=snapshot.id, selections_by_seat=resolved
    )


def selection_json(selection: FrozenLobbyModelSelection) -> dict[str, object]:
    """Return the JSON-safe persisted form of a validated frozen selection."""
    return selection.model_dump(mode="json")


def _qualification_policy(now: datetime) -> ModelQualificationPolicy:
    return ModelQualificationPolicy(
        current_time_bucket=int(now.timestamp()) // (60 * 60),
        max_probe_age_buckets=7 * 24,
        playable_max_tokens=20_000,
        playable_max_latency_ms=8_000,
        ranked_max_latency_ms=2_500,
        ranked_max_price_micros_per_million=5_000_000,
        ranked_min_successful_samples=3,
    )


async def materialize_frozen_lobby_model_selection(
    session: AsyncSession,
    *,
    selection_json_value: Mapping[str, object],
    now: datetime,
) -> uuid.UUID:
    """Materialize exactly the stored selection without consulting live catalog state."""
    selection = FrozenLobbyModelSelection.model_validate(selection_json_value)
    snapshot = await session.get(CatalogSnapshot, selection.catalog_snapshot_id)
    prompt = await session.get(PromptVersion, selection.prompt_version_id)
    if (
        snapshot is None
        or snapshot.status != "COMPLETE"
        or snapshot.content_hash != selection.catalog_content_hash
        or prompt is None
        or prompt.prompt_hash != selection.prompt_hash
        or _schema_hash(prompt.response_schema) != selection.schema_hash
    ):
        raise LobbyModelSelectionError("frozen_model_selection_invalid")
    try:
        materialized = await get_or_create_openrouter_agent_build(
            session,
            request=OpenRouterAgentBuildMaterializationRequest(
                provider_id=selection.provider_id,
                catalog_snapshot_id=selection.catalog_snapshot_id,
                canonical_slug=selection.canonical_slug,
                prompt_version_id=selection.prompt_version_id,
                adapter_version=selection.adapter_version,
                inference_params=selection.inference_params,
            ),
            qualification_policy=_qualification_policy(now),
        )
    except OpenRouterAgentBuildMaterializationError as exc:
        raise LobbyModelSelectionError("frozen_model_unavailable") from exc
    return materialized.agent_build.id


__all__ = [
    "FrozenLobbyModelSelection",
    "LobbyModelSelectionError",
    "RequestedAiModelSelection",
    "RequestedAutoFillPolicy",
    "ResolvedLobbyModelSelections",
    "materialize_frozen_lobby_model_selection",
    "resolve_lobby_model_selections",
    "selection_json",
]
