"""Global spend governor: hard $200 ceiling on cumulative AI spend (US-095).

Reads per-call cost rows from ``LlmCall.cost_usd`` to compute the running
total across all games, then gates new game admission when that total meets
or exceeds ``padrino_global_spend_cap_usd``.

The governor composes the global ceiling with the per-game ``cost_cap_usd``
path already enforced inside ``gauntlets.tournament``: callers should check
``can_start_game()`` before launching, and continue to pass a per-game cap
to the tournament runner for within-game enforcement.
"""

from __future__ import annotations

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import LlmCall
from padrino.settings import Settings

_logger = structlog.get_logger("padrino.economics.spend_governor")


async def cumulative_spend_usd(session: AsyncSession) -> float:
    """Return total USD spent across all LlmCall rows."""
    stmt = select(func.coalesce(func.sum(LlmCall.cost_usd), 0.0))
    result = await session.execute(stmt)
    value: float | None = result.scalar_one()
    return float(value) if value is not None else 0.0


async def can_start_game(session: AsyncSession, settings: Settings) -> bool:
    """Return True when cumulative spend is below the global cap.

    Emits a structured ``spend.cap.reached`` warning and returns False the
    moment cumulative spend meets or exceeds ``padrino_global_spend_cap_usd``.
    """
    cap = settings.padrino_global_spend_cap_usd
    spent = await cumulative_spend_usd(session)
    if spent >= cap:
        _logger.warning(
            "spend.cap.reached",
            cumulative_spend_usd=round(spent, 6),
            cap_usd=cap,
        )
        return False
    return True
