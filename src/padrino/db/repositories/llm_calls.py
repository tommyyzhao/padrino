"""CRUD helpers for :class:`padrino.db.models.LlmCall`.

The runner records one row per adapter call (success, schema-violation, or
provider error) so transcripts survive a process restart and audit / cost
analysis can run against persisted history.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import LlmCall
from padrino.openrouter.routing_metadata import sanitize_openrouter_routing_metadata


async def record_call(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    public_player_id: str,
    phase: str,
    request_json: dict[str, Any],
    request_prompt_hash: str,
    status: str,
    event_id: uuid.UUID | None = None,
    agent_build_id: uuid.UUID | None = None,
    raw_response: str | None = None,
    parsed_response: dict[str, Any] | None = None,
    error: str | None = None,
    error_kind: str | None = None,
    error_message: str | None = None,
    latency_ms: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    price_basis: str | None = None,
    price_table_version: str | None = None,
    funding_source: str = "PLATFORM",
    provider_credential_id: uuid.UUID | None = None,
    provider_response_id: str | None = None,
    openrouter_requested_model_slug: str | None = None,
    openrouter_actual_model: str | None = None,
    openrouter_actual_provider: str | None = None,
    openrouter_actual_endpoint: str | None = None,
    openrouter_generation_id: str | None = None,
    openrouter_routing_metadata: dict[str, object] | None = None,
    openrouter_catalog_snapshot_id: uuid.UUID | None = None,
    openrouter_routing_policy_hash: str | None = None,
) -> LlmCall:
    """Insert one llm_call row and return the persisted ORM object."""
    routing_metadata = (
        sanitize_openrouter_routing_metadata(openrouter_routing_metadata)
        if openrouter_routing_metadata is not None
        else {}
    )
    if provider_credential_id is not None:
        routing_metadata["credential_id"] = str(provider_credential_id)
    obj = LlmCall(
        game_id=game_id,
        event_id=event_id,
        agent_build_id=agent_build_id,
        public_player_id=public_player_id,
        phase=phase,
        request_json=request_json,
        request_prompt_hash=request_prompt_hash,
        raw_response=raw_response,
        parsed_response=parsed_response,
        status=status,
        error=error,
        error_kind=error_kind,
        error_message=error_message,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        price_basis=price_basis,
        price_table_version=price_table_version,
        funding_source=funding_source,
        provider_response_id=provider_response_id,
        openrouter_requested_model_slug=openrouter_requested_model_slug,
        openrouter_actual_model=openrouter_actual_model,
        openrouter_actual_provider=openrouter_actual_provider,
        openrouter_actual_endpoint=openrouter_actual_endpoint,
        openrouter_generation_id=openrouter_generation_id,
        openrouter_routing_metadata=routing_metadata or None,
        openrouter_catalog_snapshot_id=openrouter_catalog_snapshot_id,
        openrouter_routing_policy_hash=openrouter_routing_policy_hash,
    )
    session.add(obj)
    await session.flush()
    return obj


async def list_for_game(
    session: AsyncSession,
    game_id: uuid.UUID,
) -> list[LlmCall]:
    """Return all llm_call rows for ``game_id`` in insertion order."""
    stmt = (
        select(LlmCall).where(LlmCall.game_id == game_id).order_by(LlmCall.created_at, LlmCall.id)
    )
    result = await session.execute(stmt)
    return list(result.scalars())
