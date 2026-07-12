"""Opt-in live coverage for the bounded OpenRouter compatibility suite."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.repositories.model_catalog_snapshots import (
    CatalogEntryInput,
    create_or_get_completed_snapshot,
)
from padrino.openrouter.compatibility_probe import (
    CompatibilityProbeRunner,
    HttpxOpenRouterProbeTransport,
    ProbeRunMode,
)

pytestmark = [pytest.mark.integration, pytest.mark.live_llm]


@pytest.mark.live_llm
async def test_live_openrouter_probe_uses_an_explicit_spend_cap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Exercise a real model only when an operator explicitly opts in locally."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        pytest.skip("OPENROUTER_API_KEY is required for a live compatibility probe")
    model = os.environ.get("PADRINO_OPENROUTER_PROBE_MODEL", "openai/gpt-5-mini")
    now = datetime.now(UTC)
    async with session_factory() as session:
        snapshot, _ = await create_or_get_completed_snapshot(
            session,
            content_hash="f" * 64,
            fetched_at=now,
            source_etag=None,
            source_version="live-probe",
            entries=[
                CatalogEntryInput(
                    canonical_slug=model,
                    display_name=model,
                    description=None,
                    modalities=("text",),
                    context_length=128_000,
                    pricing_strings={"prompt": "0.00000025", "completion": "0.000002"},
                    pricing_micros={
                        "prompt_per_million": 250_000,
                        "completion_per_million": 2_000_000,
                    },
                    supported_parameters=("response_format",),
                    provider_summary={},
                    expires_at=None,
                    raw_metadata={"architecture": {"output_modalities": ["text"]}},
                )
            ],
        )
        await session.commit()

    async with httpx.AsyncClient(timeout=45.0) as client:
        result = await CompatibilityProbeRunner(
            session_factory=session_factory,
            transport=HttpxOpenRouterProbeTransport(
                client=client,
                base_url="https://openrouter.ai/api/v1",
                api_key=api_key,
            ),
            now=lambda: now,
            trigger_rate_limit_per_minute=1,
            operator_cost_cap_micros=50_000,
        ).run(
            catalog_content_hash=snapshot.content_hash,
            canonical_slug=model,
            mode=ProbeRunMode.LIVE,
            trigger_key="live-integration",
            spend_cap_micros=50_000,
        )

    rendered = str(result.evidence.evidence)
    assert result.created is True
    assert result.evidence.evidence["estimated_cost_micros"] <= 50_000
    assert api_key not in rendered
    assert "Authorization" not in rendered
