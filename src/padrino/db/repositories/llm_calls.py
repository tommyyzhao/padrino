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
    provider_response_id: str | None = None,
) -> LlmCall:
    """Insert one llm_call row and return the persisted ORM object."""
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
        provider_response_id=provider_response_id,
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
