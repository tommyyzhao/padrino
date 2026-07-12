"""US-349: OpenRouter LLM-call audit persistence and retention coverage."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import CatalogSnapshot, Game, LlmCall
from padrino.db.repositories import llm_calls as llm_calls_repo
from padrino.db.retention_executor import run_retention_executor
from padrino.settings import Settings


async def test_openrouter_routing_audit_persists_but_outlives_raw_payload_scrubbing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    game_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            CatalogSnapshot(
                id=snapshot_id,
                content_hash="a" * 64,
                fetched_at=now,
                status="COMPLETE",
                completed_at=now,
            )
        )
        session.add(
            Game(
                id=game_id,
                ruleset_id="mini7_v1",
                game_seed="routing-audit-seed",
                status="COMPLETED",
                completed_at=now - timedelta(days=31),
                is_broadcastable=True,
            )
        )
        await llm_calls_repo.record_call(
            session,
            game_id=game_id,
            public_player_id="P01",
            phase="DAY_1_VOTE",
            request_json={"messages": ["existing retained raw prompt"]},
            request_prompt_hash="b" * 64,
            raw_response="existing retained raw response",
            status="ok",
            openrouter_requested_model_slug="openai/gpt-4o-mini",
            openrouter_actual_model="openai/gpt-4o-mini-2024-07-18",
            openrouter_actual_provider="OpenAI",
            openrouter_actual_endpoint="https://provider.example/v1/chat",
            openrouter_generation_id="gen-safe-123",
            openrouter_routing_metadata={
                "strategy": "auto",
                "attempt": 1,
                "is_byok": True,
                "attempts": [{"provider": "OpenAI", "status": 200}],
                "pipeline": [{"type": "context_compression", "name": "context-compression"}],
                "key_hash": "sha256:human-key-must-not-persist",
            },
            openrouter_catalog_snapshot_id=snapshot_id,
            openrouter_routing_policy_hash="c" * 64,
        )

    result = await run_retention_executor(
        session_factory,
        settings=Settings(
            padrino_enable_retention=True,
            padrino_retention_dry_run=False,
            padrino_raw_payload_ttl_days=30,
            padrino_non_broadcastable_game_ttl_days=7,
        ),
        now=now,
    )

    assert result is not None
    assert result.llm_calls_scrubbed == 1
    async with session_factory() as session:
        call = await session.scalar(select(LlmCall).where(LlmCall.game_id == game_id))
    assert call is not None
    assert call.raw_response is None
    assert call.request_json == {}
    assert call.openrouter_requested_model_slug == "openai/gpt-4o-mini"
    assert call.openrouter_actual_provider == "OpenAI"
    assert call.openrouter_catalog_snapshot_id == snapshot_id
    assert call.openrouter_routing_metadata == {
        "strategy": "auto",
        "attempt": 1,
        "is_byok": True,
        "attempts": [{"provider": "OpenAI", "status": 200}],
        "pipeline": [{"type": "context_compression", "name": "context-compression"}],
    }
