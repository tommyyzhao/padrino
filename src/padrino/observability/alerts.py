"""Operational alerting: webhook notifier + condition-transition alert rules (US-113).

An unattended, money-spending deployment must surface critical conditions
(spend cap reached, scheduler dead, moderation gate degraded, admission
denial streaks) the moment they happen rather than on the provider bill.

:class:`AlertNotifier` is the single delivery seam. It:

  * always emits a structured ``alert.fired`` log (so alerts are visible in
    the log pipeline even with no webhook configured);
  * POSTs the alert as JSON to ``padrino_alert_webhook_url`` when that setting
    is set, using an injected :class:`httpx.AsyncClient` (a no-op when unset);
  * de-duplicates by *condition transition*: an alert key only fires when it
    moves from inactive → active, and re-arms only after :meth:`resolve` is
    called for that key. This prevents per-tick alert spam while a condition
    persists.

The fire/resolve seams are wired at the existing observability points:

  * ``spend.cap.reached``           — spend governor denial.
  * ``scheduler.heartbeat.stale``   — latest scheduler heartbeat older than the
    configured staleness window (checked against the heartbeats table).
  * ``moderation.guard.unavailable``— guard is ``None`` or erroring while
    continuous matchmaking is enabled.
  * ``admission.denied.streak``     — N consecutive admission denials.
  * ``budget.burn.threshold``       — benchmark spend reaches the configured
    fraction of a global/campaign cap.
  * ``cost.drift.threshold``        — observed per-call cost diverges beyond
    the configured threshold from the stamped expectation.

This module lives in the impure observability layer: it performs network I/O
and reads wall-clock-derived inputs supplied by its callers. It is never
imported by pure core.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx
import structlog

from padrino.observability.events import EVENT_ALERT_FIRED
from padrino.observability.metrics import cost_drift_ratio

_logger = structlog.get_logger("padrino.observability.alerts")

# Canonical alert keys. Each maps to exactly one observable condition; callers
# fire and resolve by key so the transition de-duplication is keyed correctly.
ALERT_SPEND_CAP_REACHED: str = "spend.cap.reached"
ALERT_SCHEDULER_HEARTBEAT_STALE: str = "scheduler.heartbeat.stale"
ALERT_MODERATION_GUARD_UNAVAILABLE: str = "moderation.guard.unavailable"
ALERT_ADMISSION_DENIED_STREAK: str = "admission.denied.streak"
ALERT_BUDGET_BURN: str = "budget.burn.threshold"
ALERT_COST_DRIFT: str = "cost.drift.threshold"

#: All known alert keys (used by tests and the Prometheus rules cross-check).
ALERT_KEYS: tuple[str, ...] = (
    ALERT_SPEND_CAP_REACHED,
    ALERT_SCHEDULER_HEARTBEAT_STALE,
    ALERT_MODERATION_GUARD_UNAVAILABLE,
    ALERT_ADMISSION_DENIED_STREAK,
    ALERT_BUDGET_BURN,
    ALERT_COST_DRIFT,
)


class BudgetBurnAlertSettings(Protocol):
    """Settings surface required for budget-burn alert evaluation."""

    padrino_budget_burn_alert_fraction_threshold: float


class CostDriftAlertSettings(Protocol):
    """Settings surface required for cost-drift alert evaluation."""

    padrino_cost_drift_alert_fraction_threshold: float


class AlertNotifier:
    """Condition-transition alert delivery to a webhook + structured logs.

    Construct with the resolved ``webhook_url`` (``None`` => log-only, no-op
    transport) and an optional :class:`httpx.AsyncClient`. Tests inject a
    client backed by :class:`httpx.MockTransport` so no real network call is
    made and the default-suite needs no integration marker.
    """

    __slots__ = ("_active", "_client", "_counters", "_owns_client", "_webhook_url")

    def __init__(
        self,
        *,
        webhook_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._client = client
        # We only construct (and therefore must close) a client we created.
        self._owns_client = False
        # Per-key active flag; True between a firing transition and its resolve.
        self._active: dict[str, bool] = dict.fromkeys(ALERT_KEYS, False)
        # Per-key running counters (e.g. consecutive admission denials) that the
        # notifier carries across ticks so streak detection survives the loop.
        self._counters: dict[str, int] = {}

    def is_active(self, key: str) -> bool:
        """Return True iff ``key`` has fired and not yet been resolved."""
        return self._active.get(key, False)

    def increment(self, key: str) -> int:
        """Increment and return the running counter for ``key``."""
        value = self._counters.get(key, 0) + 1
        self._counters[key] = value
        return value

    def reset_counter(self, key: str) -> None:
        """Reset the running counter for ``key`` to zero."""
        self._counters[key] = 0

    async def fire(self, key: str, **details: Any) -> bool:
        """Fire the alert for ``key`` iff it is transitioning inactive → active.

        Returns True when the alert actually fired (a transition), False when
        the condition was already active (suppressed to avoid per-tick spam).
        On a transition this always emits a structured log and, when a webhook
        URL is configured, POSTs the alert payload as JSON.
        """
        if self._active.get(key, False):
            return False
        self._active[key] = True

        payload: dict[str, Any] = {"alert": key, **details}
        _logger.warning(EVENT_ALERT_FIRED, **payload)
        await self._post(payload)
        return True

    def resolve(self, key: str) -> bool:
        """Clear the active flag for ``key`` so it can fire again.

        Returns True when the key was active (a resolving transition), False
        when it was already inactive. Resolution is intentionally log-only with
        no webhook POST — the active alert system is the source of truth for
        what to re-page on.
        """
        if not self._active.get(key, False):
            return False
        self._active[key] = False
        return True

    async def _post(self, payload: dict[str, Any]) -> None:
        if self._webhook_url is None or self._client is None:
            return
        try:
            await self._client.post(self._webhook_url, json=payload)
        except httpx.HTTPError as exc:
            # Alert delivery must never crash the caller's loop; the structured
            # log above already recorded the underlying condition.
            _logger.warning(
                "alert.delivery.failed",
                alert=payload.get("alert"),
                error=str(exc),
            )

    async def aclose(self) -> None:
        """Close an owned client (no-op when a client was injected)."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()


def build_alert_notifier(settings: Any) -> AlertNotifier:
    """Construct an :class:`AlertNotifier` from settings.

    When ``padrino_alert_webhook_url`` is set, a real
    :class:`httpx.AsyncClient` is created and owned by the notifier; otherwise
    the notifier is log-only.
    """
    url: str | None = settings.padrino_alert_webhook_url
    if not url:
        return AlertNotifier(webhook_url=None, client=None)
    notifier = AlertNotifier(
        webhook_url=url,
        client=httpx.AsyncClient(timeout=settings.padrino_alert_webhook_timeout_s),
    )
    notifier._owns_client = True
    return notifier


def budget_fraction_of_cap(*, spent_usd: float, cap_usd: float) -> float:
    """Return spend as a fraction of cap, treating zero caps as already consumed."""
    if cap_usd <= 0:
        return 1.0 if spent_usd >= cap_usd else 0.0
    return max(0.0, spent_usd / cap_usd)


async def evaluate_budget_burn_alert(
    notifier: AlertNotifier,
    settings: BudgetBurnAlertSettings,
    *,
    scope_type: str,
    scope_id: str,
    spent_usd: float,
    cap_usd: float,
) -> bool:
    """Fire or resolve the budget-burn alert for one spend/cap observation."""
    threshold = settings.padrino_budget_burn_alert_fraction_threshold
    fraction = budget_fraction_of_cap(spent_usd=spent_usd, cap_usd=cap_usd)
    if fraction >= threshold:
        return await notifier.fire(
            ALERT_BUDGET_BURN,
            scope_type=scope_type,
            scope_id=scope_id,
            spent_usd=round(spent_usd, 6),
            cap_usd=round(cap_usd, 6),
            fraction_of_cap=round(fraction, 6),
            threshold=round(threshold, 6),
        )
    return notifier.resolve(ALERT_BUDGET_BURN)


async def evaluate_cost_drift_alert(
    notifier: AlertNotifier,
    settings: CostDriftAlertSettings,
    *,
    observed_cost_usd: float,
    expected_cost_usd: float,
    model_id: str | None = None,
    price_basis: str | None = None,
) -> bool:
    """Fire or resolve the cost-drift alert for one observed/expected call cost."""
    threshold = settings.padrino_cost_drift_alert_fraction_threshold
    drift = cost_drift_ratio(
        observed_cost_usd=observed_cost_usd,
        expected_cost_usd=expected_cost_usd,
    )
    if drift > threshold:
        return await notifier.fire(
            ALERT_COST_DRIFT,
            model_id=model_id,
            price_basis=price_basis,
            observed_cost_usd=round(observed_cost_usd, 6),
            expected_cost_usd=round(expected_cost_usd, 6),
            drift_fraction=round(drift, 6),
            threshold=round(threshold, 6),
        )
    return notifier.resolve(ALERT_COST_DRIFT)


__all__ = [
    "ALERT_ADMISSION_DENIED_STREAK",
    "ALERT_BUDGET_BURN",
    "ALERT_COST_DRIFT",
    "ALERT_KEYS",
    "ALERT_MODERATION_GUARD_UNAVAILABLE",
    "ALERT_SCHEDULER_HEARTBEAT_STALE",
    "ALERT_SPEND_CAP_REACHED",
    "AlertNotifier",
    "budget_fraction_of_cap",
    "build_alert_notifier",
    "evaluate_budget_burn_alert",
    "evaluate_cost_drift_alert",
]
