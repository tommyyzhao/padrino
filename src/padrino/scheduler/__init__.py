"""In-process scheduler jobs (US-085).

``padrino.scheduler`` hosts the recurring jobs the async scheduler fires on
each tick. Today the only job is :func:`gauntlet_job.run_due_scheduled_gauntlets`,
wired into ``run_scheduler`` via :func:`bootstrap.build_scheduled_gauntlet_tick_hook`.
"""

from __future__ import annotations

__all__: list[str] = []
