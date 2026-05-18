"""DB-backed metrics summary for the ``padrino metrics`` CLI.

Reads from ``game_events``, ``llm_calls``, ``agent_builds``, and
``model_configs`` to compute high-level operational metrics:

* ``games_completed`` — count of games with a ``GameTerminated`` row.
* ``avg_phase_duration_seconds`` — mean of ``(PhaseResolved.created_at -
  PhaseStarted.created_at)`` across all phases that have both endpoints.
* ``llm_latency`` — per-model p50 / p95 ``latency_ms`` plus sample count;
  models without an ``agent_build_id`` fall under the ``"unknown"`` bucket.
* ``timeout_rate`` — ``ActionTimedOut`` event count divided by total LLM
  attempts (timeouts + persisted ``llm_calls`` rows).
* ``invalid_json_rate`` — fraction of persisted ``llm_calls`` whose status is
  ``invalid_json`` or ``schema_violation``.

Impure module — uses SQLAlchemy and wall-clock arithmetic.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import AgentBuild, GameEvent, LlmCall, ModelConfig

_PHASE_STARTED: Final[str] = "PhaseStarted"
_PHASE_RESOLVED: Final[str] = "PhaseResolved"
_GAME_TERMINATED: Final[str] = "GameTerminated"
_ACTION_TIMED_OUT: Final[str] = "ActionTimedOut"

_INVALID_JSON_STATUSES: Final[frozenset[str]] = frozenset({"invalid_json", "schema_violation"})

UNKNOWN_MODEL: Final[str] = "unknown"


@dataclass(frozen=True, slots=True)
class LatencyStats:
    """Latency percentiles + sample count for one model."""

    samples: int
    p50_ms: int | None
    p95_ms: int | None


@dataclass(frozen=True, slots=True)
class MetricsSummary:
    """Top-level metrics payload returned by :func:`compute_metrics_summary`."""

    games_completed: int
    avg_phase_duration_seconds: float | None
    llm_latency: dict[str, LatencyStats]
    timeout_rate: float | None
    invalid_json_rate: float | None
    total_llm_calls: int
    total_timeouts: int


def _percentile(sorted_values: list[int], q: float) -> int | None:
    """Return the ``q`` quantile of ``sorted_values`` using nearest-rank.

    ``q`` is in ``[0, 1]``. Returns ``None`` for an empty list.
    """
    if not sorted_values:
        return None
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    rank = max(1, round(q * len(sorted_values)))
    return sorted_values[rank - 1]


async def _games_completed(session: AsyncSession) -> int:
    stmt = select(func.count(func.distinct(GameEvent.game_id))).where(
        GameEvent.event_type == _GAME_TERMINATED
    )
    value = (await session.execute(stmt)).scalar()
    return int(value or 0)


async def _avg_phase_duration_seconds(session: AsyncSession) -> float | None:
    """Pair PhaseStarted / PhaseResolved rows on (game_id, phase) and average.

    Phases without both endpoints are skipped. Returns ``None`` when no paired
    phase exists in the DB.
    """
    stmt = select(
        GameEvent.game_id, GameEvent.phase, GameEvent.event_type, GameEvent.created_at
    ).where(GameEvent.event_type.in_({_PHASE_STARTED, _PHASE_RESOLVED}))
    started: dict[tuple[Any, str], Any] = {}
    resolved: dict[tuple[Any, str], Any] = {}
    for game_id, phase, event_type, created_at in (await session.execute(stmt)).all():
        key = (game_id, phase)
        if event_type == _PHASE_STARTED:
            started[key] = created_at
        else:
            resolved[key] = created_at
    deltas: list[float] = []
    for key, start in started.items():
        end = resolved.get(key)
        if end is None or start is None:
            continue
        delta = (end - start).total_seconds()
        if delta < 0:
            continue
        deltas.append(delta)
    if not deltas:
        return None
    return sum(deltas) / len(deltas)


async def _llm_latency_per_model(session: AsyncSession) -> dict[str, LatencyStats]:
    stmt = (
        select(LlmCall.latency_ms, ModelConfig.model_name)
        .select_from(LlmCall)
        .outerjoin(AgentBuild, AgentBuild.id == LlmCall.agent_build_id)
        .outerjoin(ModelConfig, ModelConfig.id == AgentBuild.model_config_id)
    )
    buckets: dict[str, list[int]] = defaultdict(list)
    for latency_ms, model_name in (await session.execute(stmt)).all():
        if latency_ms is None:
            continue
        bucket = model_name if isinstance(model_name, str) and model_name else UNKNOWN_MODEL
        buckets[bucket].append(int(latency_ms))
    out: dict[str, LatencyStats] = {}
    for name, latencies in buckets.items():
        latencies.sort()
        out[name] = LatencyStats(
            samples=len(latencies),
            p50_ms=_percentile(latencies, 0.50),
            p95_ms=_percentile(latencies, 0.95),
        )
    return out


async def _timeout_and_invalid_rates(
    session: AsyncSession,
) -> tuple[float | None, float | None, int, int]:
    """Compute (timeout_rate, invalid_json_rate, total_llm_calls, total_timeouts)."""
    total_calls = int((await session.execute(select(func.count(LlmCall.id)))).scalar() or 0)
    invalid_count = int(
        (
            await session.execute(
                select(func.count(LlmCall.id)).where(LlmCall.status.in_(_INVALID_JSON_STATUSES))
            )
        ).scalar()
        or 0
    )
    timeout_count = int(
        (
            await session.execute(
                select(func.count(GameEvent.id)).where(GameEvent.event_type == _ACTION_TIMED_OUT)
            )
        ).scalar()
        or 0
    )
    total_attempts = total_calls + timeout_count
    timeout_rate = (timeout_count / total_attempts) if total_attempts else None
    invalid_rate = (invalid_count / total_calls) if total_calls else None
    return timeout_rate, invalid_rate, total_calls, timeout_count


async def compute_metrics_summary(session: AsyncSession) -> MetricsSummary:
    """Aggregate operational metrics across the whole DB."""
    games_completed = await _games_completed(session)
    avg_phase = await _avg_phase_duration_seconds(session)
    latency = await _llm_latency_per_model(session)
    timeout_rate, invalid_rate, total_calls, total_timeouts = await _timeout_and_invalid_rates(
        session
    )
    return MetricsSummary(
        games_completed=games_completed,
        avg_phase_duration_seconds=avg_phase,
        llm_latency=latency,
        timeout_rate=timeout_rate,
        invalid_json_rate=invalid_rate,
        total_llm_calls=total_calls,
        total_timeouts=total_timeouts,
    )


def metrics_summary_to_dict(summary: MetricsSummary) -> dict[str, Any]:
    """Serialize a :class:`MetricsSummary` as a JSON-ready dict."""
    return {
        "games_completed": summary.games_completed,
        "avg_phase_duration_seconds": summary.avg_phase_duration_seconds,
        "llm_latency": {
            name: {
                "samples": stats.samples,
                "p50_ms": stats.p50_ms,
                "p95_ms": stats.p95_ms,
            }
            for name, stats in sorted(summary.llm_latency.items())
        },
        "timeout_rate": summary.timeout_rate,
        "invalid_json_rate": summary.invalid_json_rate,
        "total_llm_calls": summary.total_llm_calls,
        "total_timeouts": summary.total_timeouts,
    }


__all__ = [
    "UNKNOWN_MODEL",
    "LatencyStats",
    "MetricsSummary",
    "compute_metrics_summary",
    "metrics_summary_to_dict",
]
