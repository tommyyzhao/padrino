"""Canonical event-name constants for Padrino's structured logs.

Every structlog INFO-level event emitted by the runner / gauntlet scheduler /
LLM adapter uses one of these names so log consumers (and the
``tests/observability`` suite) can pattern-match on a stable identifier.
"""

from __future__ import annotations

from typing import Final

EVENT_GAUNTLET_CREATED: Final[str] = "gauntlet.created"
EVENT_GAME_STARTED: Final[str] = "game.started"
EVENT_GAME_COMPLETED: Final[str] = "game.completed"
EVENT_PHASE_STARTED: Final[str] = "phase.started"
EVENT_PHASE_RESOLVED: Final[str] = "phase.resolved"
EVENT_LLM_CALL_STARTED: Final[str] = "llm.call.started"
EVENT_LLM_CALL_COMPLETED: Final[str] = "llm.call.completed"
EVENT_LLM_CALL_TIMEOUT: Final[str] = "llm.call.timeout"
EVENT_LLM_CALL_RETRY: Final[str] = "llm.call.retry"
EVENT_LLM_CALL_EXHAUSTED: Final[str] = "llm.call.exhausted"
EVENT_RATING_UPDATED: Final[str] = "rating.updated"
EVENT_SCHEDULER_TICK: Final[str] = "scheduler.tick"
EVENT_SCHEDULER_GAUNTLET_STARTED: Final[str] = "scheduler.gauntlet.started"
EVENT_SCHEDULER_GAUNTLET_COMPLETED: Final[str] = "scheduler.gauntlet.completed"
EVENT_SCHEDULER_HEARTBEAT: Final[str] = "scheduler.heartbeat"
EVENT_SCHEDULER_STALE_RESET: Final[str] = "scheduler.stale_reset"


__all__ = [
    "EVENT_GAME_COMPLETED",
    "EVENT_GAME_STARTED",
    "EVENT_GAUNTLET_CREATED",
    "EVENT_LLM_CALL_COMPLETED",
    "EVENT_LLM_CALL_EXHAUSTED",
    "EVENT_LLM_CALL_RETRY",
    "EVENT_LLM_CALL_STARTED",
    "EVENT_LLM_CALL_TIMEOUT",
    "EVENT_PHASE_RESOLVED",
    "EVENT_PHASE_STARTED",
    "EVENT_RATING_UPDATED",
    "EVENT_SCHEDULER_GAUNTLET_COMPLETED",
    "EVENT_SCHEDULER_GAUNTLET_STARTED",
    "EVENT_SCHEDULER_HEARTBEAT",
    "EVENT_SCHEDULER_STALE_RESET",
    "EVENT_SCHEDULER_TICK",
]
