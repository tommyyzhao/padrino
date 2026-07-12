"""US-347: race-safe OpenRouter AgentBuild materialization coverage."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.model_qualification import ModelQualificationPolicy, QualificationTier
from padrino.db.models import AgentBuild, ModelConfig, ModelProvider, PromptVersion
from padrino.db.repositories.model_catalog_snapshots import (
    CatalogEntryInput,
    create_or_get_completed_snapshot,
)
from padrino.openrouter.agent_build_materialization import (
    OpenRouterAgentBuildMaterializationError,
    OpenRouterAgentBuildMaterializationRequest,
    get_or_create_openrouter_agent_build,
)

NOW = datetime(2026, 7, 12, 15, 0, tzinfo=UTC)


def _policy() -> ModelQualificationPolicy:
    return ModelQualificationPolicy(
        current_time_bucket=497_583,
        max_probe_age_buckets=7 * 24,
        playable_max_tokens=20_000,
        playable_max_latency_ms=8_000,
        ranked_max_latency_ms=2_500,
        ranked_max_price_micros_per_million=5_000_000,
        ranked_min_successful_samples=3,
    )


def _entry(
    *,
    slug: str = "openai/gpt-test",
    display_name: str = "OpenAI: GPT Test",
    modalities: tuple[str, ...] = ("text",),
    availability: str = "available",
) -> CatalogEntryInput:
    return CatalogEntryInput(
        canonical_slug=slug,
        display_name=display_name,
        description="A canonical catalog model.",
        modalities=modalities,
        context_length=128_000,
        pricing_strings={"prompt": "0.0000002", "completion": "0.0000008"},
        pricing_micros={"prompt_per_million": 200_000, "completion_per_million": 800_000},
        supported_parameters=("temperature", "top_p", "max_tokens"),
        provider_summary={"max_completion_tokens": 8_192},
        expires_at=None,
        raw_metadata={"status": availability},
    )


async def _seed_inputs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    entry: CatalogEntryInput | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_factory() as session, session.begin():
        provider = ModelProvider(
            name="openrouter",
            base_url="https://openrouter.example.test/api/v1",
            auth_secret_ref="env:OPENROUTER_TEST_KEY",
        )
        prompt = PromptVersion(
            ruleset_id="mini7_v1",
            version="openrouter-v1",
            system_prompt="system",
            developer_prompt="developer",
            response_schema={"type": "object"},
            prompt_hash=f"{uuid.uuid4().int:064x}",
        )
        session.add_all((provider, prompt))
        await session.flush()
        snapshot, _ = await create_or_get_completed_snapshot(
            session,
            content_hash="a" * 64,
            fetched_at=NOW,
            source_etag=None,
            source_version="catalog-v1",
            entries=[entry or _entry()],
        )
        return provider.id, prompt.id, snapshot.id


def _request(
    *,
    provider_id: uuid.UUID,
    prompt_version_id: uuid.UUID,
    catalog_snapshot_id: uuid.UUID,
    inference_params: dict[str, object] | None = None,
) -> OpenRouterAgentBuildMaterializationRequest:
    return OpenRouterAgentBuildMaterializationRequest(
        provider_id=provider_id,
        catalog_snapshot_id=catalog_snapshot_id,
        canonical_slug="openai/gpt-test",
        prompt_version_id=prompt_version_id,
        adapter_version="openrouter-adapter-v1",
        inference_params=inference_params
        if inference_params is not None
        else {"temperature": 0.35, "top_p": 0.9, "max_tokens": 512},
    )


async def test_materialization_derives_identity_from_the_immutable_catalog_and_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider_id, prompt_version_id, snapshot_id = await _seed_inputs(session_factory)
    request = _request(
        provider_id=provider_id,
        prompt_version_id=prompt_version_id,
        catalog_snapshot_id=snapshot_id,
    )

    async with session_factory() as session, session.begin():
        first = await get_or_create_openrouter_agent_build(
            session,
            request=request,
            qualification_policy=_policy(),
        )
        second = await get_or_create_openrouter_agent_build(
            session,
            request=request.model_copy(
                update={"inference_params": {"max_tokens": 512, "top_p": 0.9, "temperature": 0.35}}
            ),
            qualification_policy=_policy(),
        )

        assert first.agent_build.id == second.agent_build.id
        assert first.model_config.id == second.model_config.id
        assert first.qualification_tier is QualificationTier.UNVERIFIED
        assert first.model_config.model_name == "openai/gpt-test"
        assert first.model_config.litellm_model_id == "openai/gpt-test"
        assert first.model_config.model_version == "catalog-v1"
        assert first.model_config.catalog_snapshot_id == snapshot_id
        assert first.model_config.catalog_canonical_slug == "openai/gpt-test"
        assert first.model_config.catalog_identity_hash is not None
        assert first.agent_build.display_name == "OpenAI: GPT Test"
        assert first.agent_build.catalog_snapshot_id == snapshot_id
        assert first.agent_build.qualification_tier == QualificationTier.UNVERIFIED.value
        assert first.agent_build.inference_params == {
            "max_tokens": 512,
            "temperature": 0.35,
            "top_p": 0.9,
        }
        assert first.agent_build.inference_params_hash is not None
        assert first.agent_build.materialization_identity_hash is not None

    async with session_factory() as session:
        assert len(list((await session.execute(select(ModelConfig))).scalars())) == 1
        assert len(list((await session.execute(select(AgentBuild))).scalars())) == 1


@pytest.mark.parametrize(
    ("entry", "reason"),
    (
        (_entry(modalities=("image",)), "not_text_model"),
        (_entry(availability="disabled"), "model_disabled"),
        (_entry(availability="deprecated"), "model_deprecated"),
    ),
)
async def test_materialization_refuses_catalog_models_that_are_not_casually_eligible(
    session_factory: async_sessionmaker[AsyncSession],
    entry: CatalogEntryInput,
    reason: str,
) -> None:
    provider_id, prompt_version_id, snapshot_id = await _seed_inputs(
        session_factory,
        entry=entry,
    )
    async with session_factory() as session, session.begin():
        with pytest.raises(OpenRouterAgentBuildMaterializationError, match=reason):
            await get_or_create_openrouter_agent_build(
                session,
                request=_request(
                    provider_id=provider_id,
                    prompt_version_id=prompt_version_id,
                    catalog_snapshot_id=snapshot_id,
                ),
                qualification_policy=_policy(),
            )


def test_materialization_inference_params_are_allowlisted_and_bounded() -> None:
    provider_id = uuid.uuid4()
    catalog_snapshot_id = uuid.uuid4()
    prompt_version_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="inference_params_allowlist"):
        OpenRouterAgentBuildMaterializationRequest(
            provider_id=provider_id,
            catalog_snapshot_id=catalog_snapshot_id,
            canonical_slug="openai/gpt-test",
            prompt_version_id=prompt_version_id,
            adapter_version="openrouter-adapter-v1",
            inference_params={"response_format": "json"},
        )
    with pytest.raises(ValidationError, match="max_tokens"):
        OpenRouterAgentBuildMaterializationRequest(
            provider_id=provider_id,
            catalog_snapshot_id=catalog_snapshot_id,
            canonical_slug="openai/gpt-test",
            prompt_version_id=prompt_version_id,
            adapter_version="openrouter-adapter-v1",
            inference_params={"max_tokens": 0},
        )


async def test_materialization_leaves_legacy_admin_and_scientific_rows_untouched(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider_id, prompt_version_id, snapshot_id = await _seed_inputs(session_factory)
    async with session_factory() as session, session.begin():
        legacy_provider = ModelProvider(name="scientific", auth_secret_ref="env:SCIENTIFIC")
        session.add(legacy_provider)
        await session.flush()
        legacy_model = ModelConfig(
            provider_id=legacy_provider.id,
            model_name="legacy-model",
            litellm_model_id="scientific/legacy-model",
            model_version="legacy-v1",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4_096,
            supports_structured_outputs=True,
        )
        session.add(legacy_model)
        await session.flush()
        legacy_build = AgentBuild(
            display_name="scientific/legacy-model",
            model_config_id=legacy_model.id,
            prompt_version_id=prompt_version_id,
            adapter_version="legacy-adapter",
            inference_params={"temperature": 0.7},
            active=True,
        )
        session.add(legacy_build)
        await session.flush()
        legacy_build_id = legacy_build.id
        legacy_model_id = legacy_model.id

        await get_or_create_openrouter_agent_build(
            session,
            request=_request(
                provider_id=provider_id,
                prompt_version_id=prompt_version_id,
                catalog_snapshot_id=snapshot_id,
            ),
            qualification_policy=_policy(),
        )

    async with session_factory() as session:
        loaded_build = await session.get(AgentBuild, legacy_build_id)
        loaded_model = await session.get(ModelConfig, legacy_model_id)
        assert loaded_build is not None
        assert loaded_model is not None
        assert loaded_build.display_name == "scientific/legacy-model"
        assert loaded_build.catalog_snapshot_id is None
        assert loaded_build.materialization_identity_hash is None
        assert loaded_model.model_name == "legacy-model"
        assert loaded_model.catalog_snapshot_id is None
        assert loaded_model.catalog_identity_hash is None


async def test_concurrent_materialization_returns_one_logical_build(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider_id, prompt_version_id, snapshot_id = await _seed_inputs(session_factory)
    request = _request(
        provider_id=provider_id,
        prompt_version_id=prompt_version_id,
        catalog_snapshot_id=snapshot_id,
    )
    ready = asyncio.Event()

    async def materialize() -> uuid.UUID:
        await ready.wait()
        async with session_factory() as session:
            result = await get_or_create_openrouter_agent_build(
                session,
                request=request,
                qualification_policy=_policy(),
            )
            await session.commit()
            return result.agent_build.id

    tasks = [asyncio.create_task(materialize()) for _ in range(6)]
    ready.set()
    identifiers = await asyncio.gather(*tasks)

    assert len(set(identifiers)) == 1
    async with session_factory() as session:
        assert len(list((await session.execute(select(ModelConfig))).scalars())) == 1
        assert len(list((await session.execute(select(AgentBuild))).scalars())) == 1
