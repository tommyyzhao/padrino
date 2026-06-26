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
* ``padrino_budget_global_spend_usd`` — current global benchmark spend.
* ``padrino_budget_campaign_spend_usd{campaign_id}`` — current spend for one
  benchmark campaign.
* ``padrino_budget_fraction_of_cap{scope_type,scope_id}`` — current spend as a
  fraction of the configured cap.
* ``padrino_cost_drift_fraction{model,price_basis}`` — relative per-call cost
  divergence from the stamped expected price.

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

budget_global_spend_usd = Gauge(
    "padrino_budget_global_spend_usd",
    "Current global benchmark spend in USD.",
    registry=REGISTRY,
)

budget_campaign_spend_usd = Gauge(
    "padrino_budget_campaign_spend_usd",
    "Current benchmark spend in USD for one campaign.",
    labelnames=("campaign_id",),
    registry=REGISTRY,
)

budget_fraction_of_cap = Gauge(
    "padrino_budget_fraction_of_cap",
    "Current benchmark budget consumption as a fraction of cap.",
    labelnames=("scope_type", "scope_id"),
    registry=REGISTRY,
)

cost_drift_fraction = Gauge(
    "padrino_cost_drift_fraction",
    "Relative divergence between observed per-call cost and expected stamped cost.",
    labelnames=("model", "price_basis"),
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


def _fraction_of_cap(*, spent_usd: float, cap_usd: float) -> float:
    if cap_usd <= 0:
        return 1.0 if spent_usd >= cap_usd else 0.0
    return max(0.0, spent_usd / cap_usd)


def cost_drift_ratio(*, observed_cost_usd: float, expected_cost_usd: float) -> float:
    """Return absolute relative cost drift for one priced call."""
    if expected_cost_usd <= 0:
        return 0.0 if observed_cost_usd <= 0 else 1.0
    return abs(observed_cost_usd - expected_cost_usd) / expected_cost_usd


def record_budget_burn(
    *,
    scope_type: str,
    scope_id: str,
    spent_usd: float,
    cap_usd: float,
) -> None:
    """Update budget-burn gauges for one global or campaign scope."""
    if scope_type == "global":
        budget_global_spend_usd.set(spent_usd)
    elif scope_type == "campaign":
        budget_campaign_spend_usd.labels(campaign_id=scope_id).set(spent_usd)
    budget_fraction_of_cap.labels(scope_type=scope_type, scope_id=scope_id).set(
        _fraction_of_cap(spent_usd=spent_usd, cap_usd=cap_usd)
    )


def record_cost_drift(
    *,
    model_id: str | None,
    price_basis: str | None,
    observed_cost_usd: float,
    expected_cost_usd: float,
) -> None:
    """Update the relative cost-drift gauge for one model/pricing basis."""
    cost_drift_fraction.labels(
        model=model_id or UNKNOWN_LABEL,
        price_basis=price_basis or UNKNOWN_LABEL,
    ).set(
        cost_drift_ratio(
            observed_cost_usd=observed_cost_usd,
            expected_cost_usd=expected_cost_usd,
        )
    )


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
        budget_campaign_spend_usd,
        budget_fraction_of_cap,
        cost_drift_fraction,
    ):
        collector._metrics.clear()
    scheduler_inflight_gauntlets.set(0)
    broadcast_active_streams.set(0)
    broadcast_frames_total.reset()
    budget_global_spend_usd.set(0)


__all__ = [
    "CONTENT_TYPE_LATEST",
    "REGISTRY",
    "UNKNOWN_LABEL",
    "api_requests_total",
    "broadcast_active_streams",
    "broadcast_frames_total",
    "budget_campaign_spend_usd",
    "budget_fraction_of_cap",
    "budget_global_spend_usd",
    "cost_drift_fraction",
    "cost_drift_ratio",
    "games_total",
    "invalid_action_total",
    "llm_calls_total",
    "llm_latency_seconds",
    "phase_duration_seconds",
    "record_broadcast_frame",
    "record_budget_burn",
    "record_cost_drift",
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
