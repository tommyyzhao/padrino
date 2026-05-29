"""Scheduler bootstrap: register recurring jobs as scheduler tick hooks (US-085).

``run_scheduler`` calls a single ``tick_hook`` once per loop iteration with the
current clock time. :func:`build_scheduled_gauntlet_tick_hook` returns the hook
that fires due ``scheduled_gauntlets`` rows, threading the injected clock so the
job's timing stays deterministic under test.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.gauntlets.tournament import AdapterFactory
from padrino.scheduler.gauntlet_job import run_due_scheduled_gauntlets
from padrino.settings import Settings

TickHook = Callable[[datetime], Awaitable[None]]


def build_scheduled_gauntlet_tick_hook(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    adapter_factory: AdapterFactory | None = None,
) -> TickHook:
    """Return a scheduler tick hook that fires every due scheduled gauntlet."""

    async def _hook(now: datetime) -> None:
        await run_due_scheduled_gauntlets(
            session_factory,
            now=now,
            settings=settings,
            adapter_factory=adapter_factory,
        )
        if settings.padrino_enable_behavioral_evaluation:
            from padrino.ratings.evaluator import run_pending_behavioral_evaluations

            await run_pending_behavioral_evaluations(
                session_factory,
                settings=settings,
            )

    return _hook


__all__ = ["TickHook", "build_scheduled_gauntlet_tick_hook"]
