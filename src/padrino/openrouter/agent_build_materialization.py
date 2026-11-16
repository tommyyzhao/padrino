"""Authoritative, catalog-backed OpenRouter AgentBuild materialization.

Player-facing workflows choose a canonical catalog slug, never an internal
``AgentBuild`` UUID.  This impure service resolves that slug against one
immutable catalog snapshot and uses database uniqueness hashes to make the
result reusable across concurrent lobby requests.
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable

from padrino.clock import canonical_json as _canonical_json
from padrino.core.model_qualification import (
    CompatibilityFacts,
    ModelQualificationPolicy,
    ProbeEvidenceFacts,
    ProviderAvailabilityFacts,
    QualificationTier,
    evaluate_model_qualification,
)
from padrino.db.models import (
    AgentBuild,
    CatalogSnapshot,
    ModelCatalogEntry,
    ModelConfig,
    ModelProbeEvidence,
    ModelProvider,
    PromptVersion,
)

_CANONICAL_SLUG = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
_OPENROUTER_PROVIDER = "openrouter"
_ALLOWED_INFERENCE_PARAMS = frozenset({"temperature", "top_p", "max_tokens"})
_DEFAULT_TEMPERATURE = 0.7
_DEFAULT_TOP_P = 1.0
_DEFAULT_MAX_TOKENS = 4_096
_MAX_MAX_TOKENS = 16_384
_QUALIFICATION_BUCKET_SECONDS = 60 * 60


class OpenRouterAgentBuildMaterializationError(ValueError):
    """Raised when a selected catalog model cannot become an AgentBuild."""


class OpenRouterAgentBuildMaterializationRequest(BaseModel):
    """Trusted server-side selection facts for one OpenRouter materialization."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    provider_id: uuid.UUID
    catalog_snapshot_id: uuid.UUID
    canonical_slug: str = Field(min_length=3, max_length=512)
    prompt_version_id: uuid.UUID
    adapter_version: str = Field(min_length=1, max_length=128)
    inference_params: dict[str, object] = Field(default_factory=dict)

    @field_validator("canonical_slug")
    @classmethod
    def _canonical_slug(cls, value: str) -> str:
        if not _CANONICAL_SLUG.fullmatch(value):
            raise ValueError("canonical_slug_invalid")
        return value

    @field_validator("adapter_version")
    @classmethod
    def _adapter_version(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("adapter_version_invalid")
        return normalized

    @field_validator("inference_params")
    @classmethod
    def _inference_params(cls, value: dict[str, object]) -> dict[str, object]:
        unknown = set(value) - _ALLOWED_INFERENCE_PARAMS
        if unknown:
            raise ValueError("inference_params_allowlist")

        normalized: dict[str, object] = {
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "temperature": _DEFAULT_TEMPERATURE,
            "top_p": _DEFAULT_TOP_P,
        }
        for key, raw_value in value.items():
            if key == "max_tokens":
                if (
                    isinstance(raw_value, bool)
                    or not isinstance(raw_value, int)
                    or not 1 <= raw_value <= _MAX_MAX_TOKENS
                ):
                    raise ValueError("max_tokens_invalid")
                normalized[key] = raw_value
                continue
            if (
                isinstance(raw_value, bool)
                or not isinstance(raw_value, (int, float))
                or not math.isfinite(float(raw_value))
            ):
                raise ValueError(f"{key}_invalid")
            number = float(raw_value)
            if key == "temperature" and not 0 <= number <= 2:
                raise ValueError("temperature_invalid")
            if key == "top_p" and not 0 < number <= 1:
                raise ValueError("top_p_invalid")
            normalized[key] = number
        return {key: normalized[key] for key in sorted(normalized)}


@dataclass(frozen=True, slots=True)
class MaterializedOpenRouterAgentBuild:
    """The persisted AgentBuild chain returned by idempotent materialization."""

    agent_build: AgentBuild
    model_config: ModelConfig
    catalog_snapshot: CatalogSnapshot
    qualification_tier: QualificationTier


def _sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _source_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


def _source_state(entry: ModelCatalogEntry, *states: str) -> bool:
    return any(
        _source_text(source.get("availability") or source.get("status")) in states
        for source in (entry.provider_summary, entry.raw_metadata)
    )


def _source_bool(entry: ModelCatalogEntry, key: str) -> bool | None:
    for source in (entry.provider_summary, entry.raw_metadata):
        value = source.get(key)
        if isinstance(value, bool):
            return value
    return None


def _source_string(entry: ModelCatalogEntry, key: str) -> str | None:
    for source in (entry.provider_summary, entry.raw_metadata):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _int_fact(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _time_bucket(value: datetime) -> int:
    return int(value.timestamp()) // _QUALIFICATION_BUCKET_SECONDS


def _supports_structured_output(entry: ModelCatalogEntry) -> bool:
    return bool(
        {"response_format", "json_schema", "structured_outputs"}
        & {str(parameter).lower() for parameter in entry.supported_parameters}
    )


def _probe_status_succeeded(status_value: str) -> bool:
    return _source_text(status_value) in {"success", "succeeded", "passed", "ok"}


def _probe_facts(record: ModelProbeEvidence) -> ProbeEvidenceFacts:
    payload = record.evidence
    succeeded = _probe_status_succeeded(record.status)
    schema_coercion = payload.get("schema_coercion_succeeded")
    strict_schema = payload.get("strict_schema_observed")
    drift_resolved = payload.get("drift_resolved")
    fingerprint = payload.get("provider_fingerprint")
    return ProbeEvidenceFacts(
        probe_id=str(record.id),
        observed_at_bucket=_time_bucket(record.observed_at),
        schema_coercion_succeeded=schema_coercion is True if succeeded else False,
        strict_schema_observed=strict_schema if isinstance(strict_schema, bool) else None,
        tokens_used=_int_fact(payload.get("tokens_used", payload.get("total_tokens"))),
        latency_ms=_int_fact(payload.get("latency_ms")),
        drift_resolved=drift_resolved if isinstance(drift_resolved, bool) else None,
        provider_fingerprint=(
            fingerprint.strip() if isinstance(fingerprint, str) and fingerprint.strip() else None
        ),
    )


def _qualification_for_entry(
    entry: ModelCatalogEntry,
    probe_records: Sequence[ModelProbeEvidence],
    *,
    policy: ModelQualificationPolicy,
) -> tuple[QualificationTier, tuple[str, ...], bool]:
    availability = _source_state(entry, "available", "active", "enabled")
    decision = evaluate_model_qualification(
        CompatibilityFacts(
            is_text_model=any(str(modality).lower() == "text" for modality in entry.modalities),
            explicitly_disabled=_source_state(entry, "disabled"),
            explicitly_deprecated=_source_state(entry, "deprecated", "retired", "sunset"),
            expires_at_bucket=(
                _time_bucket(entry.expires_at) if entry.expires_at is not None else None
            ),
            advertised_strict_schema=_supports_structured_output(entry),
            prompt_price_micros_per_million=_int_fact(
                entry.pricing_micros.get("prompt_per_million")
            ),
            completion_price_micros_per_million=_int_fact(
                entry.pricing_micros.get("completion_per_million")
            ),
            provider_availability=ProviderAvailabilityFacts(
                pinned=_source_bool(entry, "provider_pinned"),
                available=availability,
                fingerprint=_source_string(entry, "provider_fingerprint"),
            ),
        ),
        (_probe_facts(record) for record in probe_records),
        policy=policy,
    )
    return (
        decision.tier,
        tuple(reason.value for reason in decision.reasons),
        decision.casual_visible,
    )


def _catalog_max_tokens(entry: ModelCatalogEntry) -> int:
    value = _int_fact(entry.provider_summary.get("max_completion_tokens"))
    if value is None or value <= 0:
        return _MAX_MAX_TOKENS
    return min(value, _MAX_MAX_TOKENS)


def _insert_ignore_conflict(
    session: AsyncSession,
    model: type[ModelConfig] | type[AgentBuild],
    *,
    values: Mapping[str, object],
    index_element: str,
) -> Executable:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return (
            sqlite_insert(model)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[index_element])
        )
    if dialect == "postgresql":
        return (
            postgresql_insert(model)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[index_element])
        )
    raise OpenRouterAgentBuildMaterializationError("materialization_dialect_unsupported")


async def _get_or_create_model_config(
    session: AsyncSession,
    *,
    provider_id: uuid.UUID,
    snapshot: CatalogSnapshot,
    entry: ModelCatalogEntry,
    inference_params: Mapping[str, object],
) -> ModelConfig:
    identity_hash = _sha256(
        {
            "catalog_snapshot_hash": snapshot.content_hash,
            "canonical_slug": entry.canonical_slug,
            "provider_id": str(provider_id),
            "schema": "openrouter-model-config-v1",
        }
    )
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "provider_id": provider_id,
        "model_name": entry.canonical_slug,
        "litellm_model_id": entry.canonical_slug,
        "model_version": snapshot.source_version or snapshot.content_hash,
        "default_temperature": inference_params["temperature"],
        "default_top_p": inference_params["top_p"],
        "default_max_output_tokens": inference_params["max_tokens"],
        "supports_structured_outputs": _supports_structured_output(entry),
        "catalog_snapshot_id": snapshot.id,
        "catalog_canonical_slug": entry.canonical_slug,
        "catalog_identity_hash": identity_hash,
    }
    await session.execute(
        _insert_ignore_conflict(
            session,
            ModelConfig,
            values=values,
            index_element="catalog_identity_hash",
        )
    )
    model_config = await session.scalar(
        select(ModelConfig).where(ModelConfig.catalog_identity_hash == identity_hash)
    )
    if model_config is None:  # pragma: no cover - database atomicity invariant
        raise OpenRouterAgentBuildMaterializationError("model_config_materialization_missing")
    return model_config


async def _get_or_create_agent_build(
    session: AsyncSession,
    *,
    provider_id: uuid.UUID,
    snapshot: CatalogSnapshot,
    entry: ModelCatalogEntry,
    prompt_version: PromptVersion,
    model_config: ModelConfig,
    adapter_version: str,
    inference_params: dict[str, object],
    qualification_tier: QualificationTier,
) -> AgentBuild:
    inference_params_hash = _sha256(inference_params)
    identity_hash = _sha256(
        {
            "adapter_version": adapter_version,
            "catalog_snapshot_hash": snapshot.content_hash,
            "canonical_slug": entry.canonical_slug,
            "inference_params_hash": inference_params_hash,
            "prompt_hash": prompt_version.prompt_hash,
            "prompt_version_id": str(prompt_version.id),
            "provider_id": str(provider_id),
            "schema": "openrouter-agent-build-v1",
        }
    )
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "display_name": entry.display_name,
        "model_config_id": model_config.id,
        "prompt_version_id": prompt_version.id,
        "adapter_version": adapter_version,
        "inference_params": inference_params,
        "active": True,
        "version": snapshot.source_version or snapshot.content_hash,
        "catalog_snapshot_id": snapshot.id,
        "qualification_tier": qualification_tier.value,
        "inference_params_hash": inference_params_hash,
        "materialization_identity_hash": identity_hash,
    }
    await session.execute(
        _insert_ignore_conflict(
            session,
            AgentBuild,
            values=values,
            index_element="materialization_identity_hash",
        )
    )
    agent_build = await session.scalar(
        select(AgentBuild).where(AgentBuild.materialization_identity_hash == identity_hash)
    )
    if agent_build is None:  # pragma: no cover - database atomicity invariant
        raise OpenRouterAgentBuildMaterializationError("agent_build_materialization_missing")
    return agent_build


async def get_or_create_openrouter_agent_build(
    session: AsyncSession,
    *,
    request: OpenRouterAgentBuildMaterializationRequest,
    qualification_policy: ModelQualificationPolicy,
) -> MaterializedOpenRouterAgentBuild:
    """Resolve one immutable catalog selection into a reusable OpenRouter build.

    The catalog snapshot and prompt version are server-owned references. Player
    input reaches this function only after schema validation; display, version,
    capability, and qualification facts always come from the immutable catalog.
    """
    provider = await session.get(ModelProvider, request.provider_id)
    if provider is None or provider.name.casefold() != _OPENROUTER_PROVIDER:
        raise OpenRouterAgentBuildMaterializationError("openrouter_provider_required")
    prompt_version = await session.get(PromptVersion, request.prompt_version_id)
    if prompt_version is None:
        raise OpenRouterAgentBuildMaterializationError("prompt_version_not_found")
    snapshot = await session.get(CatalogSnapshot, request.catalog_snapshot_id)
    if snapshot is None or snapshot.status != "COMPLETE":
        raise OpenRouterAgentBuildMaterializationError("catalog_snapshot_not_available")
    entry = await session.scalar(
        select(ModelCatalogEntry).where(
            ModelCatalogEntry.catalog_snapshot_id == snapshot.id,
            ModelCatalogEntry.canonical_slug == request.canonical_slug,
        )
    )
    if entry is None:
        raise OpenRouterAgentBuildMaterializationError("catalog_model_not_found")
    probes = list(
        (
            await session.execute(
                select(ModelProbeEvidence).where(
                    ModelProbeEvidence.catalog_snapshot_id == snapshot.id,
                    ModelProbeEvidence.model_catalog_entry_id == entry.id,
                )
            )
        ).scalars()
    )
    qualification_tier, reasons, casual_visible = _qualification_for_entry(
        entry,
        probes,
        policy=qualification_policy,
    )
    if not casual_visible:
        reason = reasons[0] if reasons else "catalog_model_not_eligible"
        raise OpenRouterAgentBuildMaterializationError(reason)

    inference_params = dict(request.inference_params)
    if cast(int, inference_params["max_tokens"]) > _catalog_max_tokens(entry):
        raise OpenRouterAgentBuildMaterializationError("max_tokens_exceeds_catalog_limit")
    model_config = await _get_or_create_model_config(
        session,
        provider_id=provider.id,
        snapshot=snapshot,
        entry=entry,
        inference_params=inference_params,
    )
    agent_build = await _get_or_create_agent_build(
        session,
        provider_id=provider.id,
        snapshot=snapshot,
        entry=entry,
        prompt_version=prompt_version,
        model_config=model_config,
        adapter_version=request.adapter_version,
        inference_params=inference_params,
        qualification_tier=qualification_tier,
    )
    return MaterializedOpenRouterAgentBuild(
        agent_build=agent_build,
        model_config=model_config,
        catalog_snapshot=snapshot,
        qualification_tier=qualification_tier,
    )


__all__ = [
    "MaterializedOpenRouterAgentBuild",
    "OpenRouterAgentBuildMaterializationError",
    "OpenRouterAgentBuildMaterializationRequest",
    "get_or_create_openrouter_agent_build",
]
