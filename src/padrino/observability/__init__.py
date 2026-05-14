"""Structured-logging contextvar bindings, event names, and metrics helpers.

The observability layer owns:

* ``BoundEventLogger`` / ``logger`` — module-level lazy structlog proxies used
  by the runner, gauntlet scheduler, and LLM adapter to emit correlated
  structured events (``game.started``, ``phase.started``, ``llm.call.*``,
  ``phase.resolved``, ``game.completed``, ``rating.updated``).
* :mod:`padrino.observability.metrics` — DB-backed aggregations consumed by
  the ``padrino metrics`` CLI subcommand.

Impure layer: imports from ``padrino.db.*``. Pure-core code never imports
this package.
"""

from __future__ import annotations

from padrino.observability.events import (
    EVENT_GAME_COMPLETED,
    EVENT_GAME_STARTED,
    EVENT_GAUNTLET_CREATED,
    EVENT_LLM_CALL_COMPLETED,
    EVENT_LLM_CALL_STARTED,
    EVENT_LLM_CALL_TIMEOUT,
    EVENT_PHASE_RESOLVED,
    EVENT_PHASE_STARTED,
    EVENT_RATING_UPDATED,
)
from padrino.observability.metrics import (
    LatencyStats,
    MetricsSummary,
    compute_metrics_summary,
    metrics_summary_to_dict,
)

__all__ = [
    "EVENT_GAME_COMPLETED",
    "EVENT_GAME_STARTED",
    "EVENT_GAUNTLET_CREATED",
    "EVENT_LLM_CALL_COMPLETED",
    "EVENT_LLM_CALL_STARTED",
    "EVENT_LLM_CALL_TIMEOUT",
    "EVENT_PHASE_RESOLVED",
    "EVENT_PHASE_STARTED",
    "EVENT_RATING_UPDATED",
    "LatencyStats",
    "MetricsSummary",
    "compute_metrics_summary",
    "metrics_summary_to_dict",
]
