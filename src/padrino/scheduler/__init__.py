"""In-process scheduler jobs (US-085).

``padrino.scheduler`` hosts the recurring jobs the async scheduler fires on
each tick. Jobs are composed into ``run_scheduler`` via
:func:`bootstrap.build_scheduled_gauntlet_tick_hook`.
"""

from __future__ import annotations

__all__: list[str] = []
