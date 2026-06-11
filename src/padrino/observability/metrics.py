"""Prometheus metrics surface for Padrino (US-059).

Defines the canonical :class:`Counter`, :class:`Histogram`, and
:class:`Gauge` instances every observability seam in Padrino updates. The
``/metrics`` endpoint (see :mod:`padrino.api.app`) exposes the current
snapshot in Prometheus text format.

Metric inventory:

* ``padrino_llm_calls_total{provider,model,status}`` — every LLM completion
  attempted by the runner, tagged with the routing status (``ok``,
  ``invalid_json``, ``provider_error``, etc.).
* ``padrino_llm_latency_seconds{provider,model}`` — wall-clock latency of
  every completed LLM call (success or failure).
* ``padrino_phase_duration_seconds{ruleset,phase_kind}`` — wall-clock
  duration of every phase the runner resolves.
* ``padrino_games_total{outcome,ruleset}`` — every completed game (one
  increment at ``GameTerminated``).
* ``padrino_invalid_action_total{reason}`` — every coerced safe action the
  runner falls back to (timeout, schema violation, llm_exhausted).
* ``padrino_scheduler_inflight_gauntlets`` — current count of gauntlets
  being driven by ``padrino.runner.scheduler``.
* ``padrino_api_requests_total{route,method,status}`` — every HTTP request
  served by the FastAPI app, counted by template path + status code.
* ``padrino_broadcast_active_streams`` — current count of SSE live broadcast
  streams open (US-107).
* ``padrino_broadcast_frames_total`` — cumulative count of
  ``public_event_v1`` frames emitted via SSE broadcast (US-107).

The instruments live on a single :class:`CollectorRegistry` exposed through
:data:`REGISTRY` so tests can clear state without touching the global
``prometheus_client.REGISTRY`` shared by other libraries.

Impure module — never imported by pure-core.
"""

from __future__ import annotations

from typing import Final

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.parser import text_string_to_metric_families

CONTENT_TYPE_LATEST: Final[str] = "text/plain; version=0.0.4; charset=utf-8"

UNKNOWN_LABEL: Final[str] = "unknown"

# Per-LLM-call latency buckets in seconds; tuned for the 0.1-60 s envelope
# observed across all five wave-2 providers.
_LLM_LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    20.0,
    45.0,
    60.0,
)

# Phase durations span a wider window — discussion phases finish in milliseconds
# under the mock adapter and can stretch to many seconds under live providers.
_PHASE_DURATION_BUCKETS: Final[tuple[float, ...]] = (
    0.001,
    0.01,
    0.1,
    0.5,
    1.0,
    5.0,
    15.0,
    30.0,
    60.0,
    180.0,
)


REGISTRY: CollectorRegistry = CollectorRegistry()


llm_calls_total = Counter(
    "padrino_llm_calls_total",
    "Count of LLM completion attempts by provider, model, and routing status.",
    labelnames=("provider", "model", "status"),
    registry=REGISTRY,
)

llm_latency_seconds = Histogram(
    "padrino_llm_latency_seconds",
    "Wall-clock latency of LLM completion attempts.",
    labelnames=("provider", "model"),
    buckets=_LLM_LATENCY_BUCKETS,
    registry=REGISTRY,
)

phase_duration_seconds = Histogram(
    "padrino_phase_duration_seconds",
    "Wall-clock duration of resolved game phases.",
    labelnames=("ruleset", "phase_kind"),
    buckets=_PHASE_DURATION_BUCKETS,
    registry=REGISTRY,
)

games_total = Counter(
    "padrino_games_total",
    "Count of completed games by terminal outcome and ruleset.",
    labelnames=("outcome", "ruleset"),
    registry=REGISTRY,
)

invalid_action_total = Counter(
    "padrino_invalid_action_total",
    "Count of seat actions coerced to a safe fallback by the runner.",
    labelnames=("reason",),
    registry=REGISTRY,
)

scheduler_inflight_gauntlets = Gauge(
    "padrino_scheduler_inflight_gauntlets",
    "Number of gauntlets currently being driven by the scheduler loop.",
    registry=REGISTRY,
)

api_requests_total = Counter(
    "padrino_api_requests_total",
    "Count of HTTP requests served by the FastAPI app.",
    labelnames=("route", "method", "status"),
    registry=REGISTRY,
)

broadcast_active_streams = Gauge(
    "padrino_broadcast_active_streams",
    "Number of SSE live broadcast streams currently active.",
    registry=REGISTRY,
)

broadcast_frames_total = Counter(
    "padrino_broadcast_frames_total",
    "Total count of public_event_v1 frames emitted via SSE broadcast.",
    registry=REGISTRY,
)


def split_litellm_model_id(model_id: str | None) -> tuple[str, str]:
    """Return ``(provider, model)`` parsed from a litellm-style model id.

    LiteLLM model ids carry an optional ``<provider>/<model>`` prefix
    (e.g. ``"cerebras/zai-glm-4.7"`` → ``("cerebras", "zai-glm-4.7")``).
    Strings without a prefix bucket under ``("unknown", <model_id>)``;
    empty / ``None`` inputs return ``("unknown", "unknown")``.
    """
    if not model_id:
        return UNKNOWN_LABEL, UNKNOWN_LABEL
    if "/" in model_id:
        provider, _, model = model_id.partition("/")
        provider = provider or UNKNOWN_LABEL
        model = model or UNKNOWN_LABEL
        return provider, model
    return UNKNOWN_LABEL, model_id


def record_llm_call(
    *,
    model_id: str | None,
    status: str,
    latency_ms: int | None,
) -> None:
    """Increment the call counter and observe latency for one LLM attempt."""
    provider, model = split_litellm_model_id(model_id)
    llm_calls_total.labels(provider=provider, model=model, status=status).inc()
    if latency_ms is not None and latency_ms >= 0:
        llm_latency_seconds.labels(provider=provider, model=model).observe(latency_ms / 1000.0)


def record_phase_duration(*, ruleset: str, phase_kind: str, duration_s: float) -> None:
    """Observe the wall-clock duration of one resolved phase."""
    if duration_s < 0:
        return
    phase_duration_seconds.labels(ruleset=ruleset, phase_kind=phase_kind).observe(duration_s)


def record_game_completed(*, outcome: str, ruleset: str) -> None:
    """Increment the games counter for one terminal game."""
    games_total.labels(outcome=outcome, ruleset=ruleset).inc()


def record_invalid_action(*, reason: str) -> None:
    """Increment the invalid-action counter for one coerced safe action."""
    invalid_action_total.labels(reason=reason).inc()


def record_broadcast_frame() -> None:
    """Increment the broadcast frame counter by one."""
    broadcast_frames_total.inc()


def render_prometheus_text() -> bytes:
    """Return the current snapshot serialized as Prometheus text exposition."""
    return generate_latest(REGISTRY)


def reset_metrics() -> None:
    """Reset every collector in :data:`REGISTRY` to its initial value.

    Test-only seam: ``prometheus_client`` does not ship a public ``reset()``,
    but iterating ``_metrics`` (the per-label-set child cache) is the
    documented workaround used by upstream tests.
    """
    for collector in (
        llm_calls_total,
        llm_latency_seconds,
        phase_duration_seconds,
        games_total,
        invalid_action_total,
        api_requests_total,
    ):
        collector._metrics.clear()
    scheduler_inflight_gauntlets.set(0)
    broadcast_active_streams.set(0)
    broadcast_frames_total.reset()


__all__ = [
    "CONTENT_TYPE_LATEST",
    "REGISTRY",
    "UNKNOWN_LABEL",
    "api_requests_total",
    "broadcast_active_streams",
    "broadcast_frames_total",
    "games_total",
    "invalid_action_total",
    "llm_calls_total",
    "llm_latency_seconds",
    "phase_duration_seconds",
    "record_broadcast_frame",
    "record_game_completed",
    "record_invalid_action",
    "record_llm_call",
    "record_phase_duration",
    "render_prometheus_text",
    "reset_metrics",
    "scheduler_inflight_gauntlets",
    "split_litellm_model_id",
    "text_string_to_metric_families",
]
