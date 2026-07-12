"""Runtime guard for frozen-model platform completion-reserve funding."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.observations import Observation
from padrino.economics.completion_reserve import (
    CompletionReserveUnfundedPolicy,
    cost_usd_to_micros,
    settle_platform_completion_reserve_with_retry,
)
from padrino.llm.adapter import AdapterResult, LlmAdapter, ProviderNoContestError


class PlatformCompletionReserveAdapter:
    """Settle actual platform cost without ever changing the selected route.

    A successful provider response is passed through byte-for-byte while funded.
    Once the frozen per-game/global reserve is depleted, ``COERCE`` returns an
    ordinary exhausted result so the tick applies its phase-safe action; the
    alternate persisted ``NO_CONTEST`` policy aborts via the existing durable
    provider-no-contest path. Neither path retries with a different model.
    """

    __slots__ = (
        "_game_id",
        "_global_cap_micros",
        "_inner",
        "_now",
        "_reservation_id",
        "_session_factory",
    )

    def __init__(
        self,
        inner: LlmAdapter,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        game_id: uuid.UUID,
        reservation_id: uuid.UUID,
        global_cap_micros: int,
        now: Callable[[], datetime],
    ) -> None:
        self._inner = inner
        self._session_factory = session_factory
        self._game_id = game_id
        self._reservation_id = reservation_id
        self._global_cap_micros = global_cap_micros
        self._now = now

    async def complete(self, observation: Observation) -> AdapterResult:
        """Return the original result unless the persisted reserve is exhausted."""
        result = await self._inner.complete(observation)
        if result.cost_usd is None:
            return result
        model_slug = _model_slug(result)
        if model_slug is None:
            # A non-OpenRouter/legacy result has no frozen canonical model to
            # attribute. It remains outside this Wave-15 settlement path.
            return result
        cost_micros = cost_usd_to_micros(result.cost_usd)
        if cost_micros == 0:
            return result
        settlement = await settle_platform_completion_reserve_with_retry(
            self._session_factory,
            game_id=self._game_id,
            reservation_id=self._reservation_id,
            call_cost_micros=cost_micros,
            model_slug=model_slug,
            idempotency_key=(
                f"platform-completion:{self._game_id}:{observation.phase}:"
                f"{observation.you.player_id}"
            ),
            global_cap_micros=self._global_cap_micros,
            now=self._now(),
        )
        if settlement.unfunded_micros == 0:
            return result
        exhausted = result.model_copy(
            update={
                "status": "exhausted",
                "error": "provider_unfunded",
                "failure": None,
            }
        )
        if settlement.policy is CompletionReserveUnfundedPolicy.NO_CONTEST:
            raise ProviderNoContestError(reason="provider_unfunded", result=exhausted)
        return exhausted


def _model_slug(result: AdapterResult) -> str | None:
    audit = result.openrouter_audit
    if audit is not None and audit.requested_model_slug:
        return audit.requested_model_slug
    if result.model_id is not None and result.model_id.strip() == result.model_id:
        return result.model_id
    return None


__all__ = ["PlatformCompletionReserveAdapter"]
