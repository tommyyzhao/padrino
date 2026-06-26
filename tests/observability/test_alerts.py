"""Tests for the operational alert notifier + alert rules (US-113).

All HTTP delivery is exercised through :class:`httpx.MockTransport` so there is
no real network call and no integration marker is needed. The central property
under test is *fire-once-per-transition*: an alert only POSTs when its condition
flips inactive → active, never on every tick while the condition persists.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import SchedulerHeartbeat
from padrino.observability.alerts import (
    ALERT_ADMISSION_DENIED_STREAK,
    ALERT_BUDGET_BURN,
    ALERT_COST_DRIFT,
    ALERT_KEYS,
    ALERT_MODERATION_GUARD_UNAVAILABLE,
    ALERT_SCHEDULER_HEARTBEAT_STALE,
    ALERT_SPEND_CAP_REACHED,
    AlertNotifier,
    build_alert_notifier,
    evaluate_budget_burn_alert,
    evaluate_cost_drift_alert,
)
from padrino.scheduler.continuous_matchmaking import run_continuous_matchmaking_tick
from padrino.settings import Settings

WEBHOOK_URL = "https://hooks.example.test/services/T000/B000/xxxx"


class _Recorder:
    """Records every webhook POST body via an httpx MockTransport."""

    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json

        self.posts.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})


def _notifier(recorder: _Recorder, *, url: str | None = WEBHOOK_URL) -> AlertNotifier:
    transport = httpx.MockTransport(recorder.handler)
    client = httpx.AsyncClient(transport=transport)
    return AlertNotifier(webhook_url=url, client=client)


# ---------------------------------------------------------------------------
# AlertNotifier core
# ---------------------------------------------------------------------------


async def test_fire_posts_json_and_logs() -> None:
    rec = _Recorder()
    notifier = _notifier(rec)
    fired = await notifier.fire(ALERT_SPEND_CAP_REACHED, reason="spend_cap_reached")
    assert fired is True
    assert len(rec.posts) == 1
    assert rec.posts[0]["alert"] == ALERT_SPEND_CAP_REACHED
    assert rec.posts[0]["reason"] == "spend_cap_reached"
    assert notifier.is_active(ALERT_SPEND_CAP_REACHED)
    await notifier.aclose()


async def test_fire_is_deduplicated_until_resolve() -> None:
    rec = _Recorder()
    notifier = _notifier(rec)
    assert await notifier.fire(ALERT_SCHEDULER_HEARTBEAT_STALE) is True
    # Same condition still active across subsequent ticks: no spam.
    assert await notifier.fire(ALERT_SCHEDULER_HEARTBEAT_STALE) is False
    assert await notifier.fire(ALERT_SCHEDULER_HEARTBEAT_STALE) is False
    assert len(rec.posts) == 1
    # Resolving re-arms the alert; the next fire is a fresh transition.
    assert notifier.resolve(ALERT_SCHEDULER_HEARTBEAT_STALE) is True
    assert await notifier.fire(ALERT_SCHEDULER_HEARTBEAT_STALE) is True
    assert len(rec.posts) == 2
    await notifier.aclose()


async def test_resolve_on_inactive_is_noop() -> None:
    rec = _Recorder()
    notifier = _notifier(rec)
    assert notifier.resolve(ALERT_SPEND_CAP_REACHED) is False
    assert rec.posts == []
    await notifier.aclose()


async def test_no_webhook_url_is_log_only() -> None:
    rec = _Recorder()
    notifier = _notifier(rec, url=None)
    # Transition still happens (is_active flips) but no POST is made.
    assert await notifier.fire(ALERT_SPEND_CAP_REACHED) is True
    assert notifier.is_active(ALERT_SPEND_CAP_REACHED)
    assert rec.posts == []
    await notifier.aclose()


async def test_delivery_error_does_not_propagate() -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    notifier = AlertNotifier(webhook_url=WEBHOOK_URL, client=client)
    # Must not raise even though delivery fails; the transition still registers.
    assert await notifier.fire(ALERT_SPEND_CAP_REACHED) is True
    assert notifier.is_active(ALERT_SPEND_CAP_REACHED)
    await notifier.aclose()


async def test_counter_helpers() -> None:
    rec = _Recorder()
    notifier = _notifier(rec)
    assert notifier.increment(ALERT_ADMISSION_DENIED_STREAK) == 1
    assert notifier.increment(ALERT_ADMISSION_DENIED_STREAK) == 2
    notifier.reset_counter(ALERT_ADMISSION_DENIED_STREAK)
    assert notifier.increment(ALERT_ADMISSION_DENIED_STREAK) == 1
    await notifier.aclose()


async def test_budget_burn_alert_key_fires_and_resolves() -> None:
    rec = _Recorder()
    notifier = _notifier(rec)
    assert ALERT_BUDGET_BURN in ALERT_KEYS
    assert await notifier.fire(ALERT_BUDGET_BURN, scope_type="global") is True
    assert notifier.is_active(ALERT_BUDGET_BURN)
    assert notifier.resolve(ALERT_BUDGET_BURN) is True
    assert not notifier.is_active(ALERT_BUDGET_BURN)
    await notifier.aclose()


async def test_cost_drift_alert_key_fires_and_resolves() -> None:
    rec = _Recorder()
    notifier = _notifier(rec)
    assert ALERT_COST_DRIFT in ALERT_KEYS
    assert await notifier.fire(ALERT_COST_DRIFT, model_id="cerebras/zai-glm-4.7") is True
    assert notifier.is_active(ALERT_COST_DRIFT)
    assert notifier.resolve(ALERT_COST_DRIFT) is True
    assert not notifier.is_active(ALERT_COST_DRIFT)
    await notifier.aclose()


def test_build_alert_notifier_unset_is_log_only() -> None:
    settings = Settings(padrino_alert_webhook_url=None)
    notifier = build_alert_notifier(settings)
    assert notifier.is_active(ALERT_SPEND_CAP_REACHED) is False
    assert notifier._webhook_url is None


def test_build_alert_notifier_set_owns_client() -> None:
    settings = Settings(padrino_alert_webhook_url=WEBHOOK_URL)
    notifier = build_alert_notifier(settings)
    assert notifier._webhook_url == WEBHOOK_URL
    assert notifier._owns_client is True


# ---------------------------------------------------------------------------
# Wiring into the continuous matchmaking tick
# ---------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime(2026, 6, 18, 12, 0, 0, tzinfo=dt.UTC)


async def _seed_heartbeat(
    factory: async_sessionmaker[AsyncSession], *, beat_at: dt.datetime
) -> None:
    async with factory() as session, session.begin():
        session.add(SchedulerHeartbeat(worker_id="w1", beat_at=beat_at))


async def test_tick_disabled_does_not_alert(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rec = _Recorder()
    notifier = _notifier(rec)
    settings = Settings(padrino_enable_continuous_matchmaking=False)
    ran = await run_continuous_matchmaking_tick(
        session_factory, settings=settings, now=_now(), guard=None, notifier=notifier
    )
    assert ran is False
    assert rec.posts == []
    await notifier.aclose()


async def test_tick_fires_stale_heartbeat_alert(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = _now()
    await _seed_heartbeat(session_factory, beat_at=now - dt.timedelta(seconds=600))
    rec = _Recorder()
    notifier = _notifier(rec)
    settings = Settings(
        padrino_enable_continuous_matchmaking=True,
        padrino_scheduler_heartbeat_stale_seconds=120.0,
    )
    await run_continuous_matchmaking_tick(
        session_factory, settings=settings, now=now, guard=None, notifier=notifier
    )
    stale = [p for p in rec.posts if p["alert"] == ALERT_SCHEDULER_HEARTBEAT_STALE]
    assert len(stale) == 1
    # Second tick with the same stale condition: no duplicate POST.
    await run_continuous_matchmaking_tick(
        session_factory, settings=settings, now=now, guard=None, notifier=notifier
    )
    stale = [p for p in rec.posts if p["alert"] == ALERT_SCHEDULER_HEARTBEAT_STALE]
    assert len(stale) == 1
    await notifier.aclose()


async def test_tick_fresh_heartbeat_does_not_alert(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = _now()
    await _seed_heartbeat(session_factory, beat_at=now - dt.timedelta(seconds=5))
    rec = _Recorder()
    notifier = _notifier(rec)
    settings = Settings(
        padrino_enable_continuous_matchmaking=True,
        padrino_scheduler_heartbeat_stale_seconds=120.0,
    )
    await run_continuous_matchmaking_tick(
        session_factory, settings=settings, now=now, guard=None, notifier=notifier
    )
    stale = [p for p in rec.posts if p["alert"] == ALERT_SCHEDULER_HEARTBEAT_STALE]
    assert stale == []
    await notifier.aclose()


async def test_tick_fires_guard_unavailable_when_guard_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rec = _Recorder()
    notifier = _notifier(rec)
    settings = Settings(padrino_enable_continuous_matchmaking=True)
    await run_continuous_matchmaking_tick(
        session_factory, settings=settings, now=_now(), guard=None, notifier=notifier
    )
    guard_alerts = [p for p in rec.posts if p["alert"] == ALERT_MODERATION_GUARD_UNAVAILABLE]
    assert len(guard_alerts) == 1
    # Repeat tick: still no roster, guard still None — fires only once.
    await run_continuous_matchmaking_tick(
        session_factory, settings=settings, now=_now(), guard=None, notifier=notifier
    )
    guard_alerts = [p for p in rec.posts if p["alert"] == ALERT_MODERATION_GUARD_UNAVAILABLE]
    assert len(guard_alerts) == 1
    await notifier.aclose()


async def test_tick_fires_admission_denied_streak_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # No roster / no league => admission is allowed but matchmaking no-ops; to
    # force a *denial* streak we set the daily cap to 0 so admit() always denies.
    rec = _Recorder()
    notifier = _notifier(rec)
    settings = Settings(
        padrino_enable_continuous_matchmaking=True,
        padrino_max_games_per_day=0,
        padrino_admission_denied_streak_threshold=3,
    )
    for _ in range(5):
        await run_continuous_matchmaking_tick(
            session_factory, settings=settings, now=_now(), guard=None, notifier=notifier
        )
    streak_alerts = [p for p in rec.posts if p["alert"] == ALERT_ADMISSION_DENIED_STREAK]
    # Threshold crossed once (on the 3rd denial); subsequent denials suppressed.
    assert len(streak_alerts) == 1
    assert streak_alerts[0]["streak"] == 3
    await notifier.aclose()


async def test_tick_fires_spend_cap_alert(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rec = _Recorder()
    notifier = _notifier(rec)
    settings = Settings(
        padrino_enable_continuous_matchmaking=True,
        padrino_global_spend_cap_usd=0.0,  # cumulative spend (0) >= cap (0) => denied
    )
    await run_continuous_matchmaking_tick(
        session_factory, settings=settings, now=_now(), guard=None, notifier=notifier
    )
    spend_alerts = [p for p in rec.posts if p["alert"] == ALERT_SPEND_CAP_REACHED]
    assert len(spend_alerts) == 1
    await notifier.aclose()


async def test_budget_burn_threshold_fires_and_resolves() -> None:
    rec = _Recorder()
    notifier = _notifier(rec)
    settings = Settings(padrino_budget_burn_alert_fraction_threshold=0.8)

    assert (
        await evaluate_budget_burn_alert(
            notifier,
            settings,
            scope_type="campaign",
            scope_id="campaign-1",
            spent_usd=79.0,
            cap_usd=100.0,
        )
        is False
    )
    assert not notifier.is_active(ALERT_BUDGET_BURN)

    assert (
        await evaluate_budget_burn_alert(
            notifier,
            settings,
            scope_type="campaign",
            scope_id="campaign-1",
            spent_usd=80.0,
            cap_usd=100.0,
        )
        is True
    )
    assert notifier.is_active(ALERT_BUDGET_BURN)
    burn_alerts = [p for p in rec.posts if p["alert"] == ALERT_BUDGET_BURN]
    assert len(burn_alerts) == 1
    assert burn_alerts[0]["fraction_of_cap"] == 0.8

    assert (
        await evaluate_budget_burn_alert(
            notifier,
            settings,
            scope_type="campaign",
            scope_id="campaign-1",
            spent_usd=50.0,
            cap_usd=100.0,
        )
        is True
    )
    assert not notifier.is_active(ALERT_BUDGET_BURN)
    await notifier.aclose()


async def test_cost_drift_threshold_fires_and_resolves() -> None:
    rec = _Recorder()
    notifier = _notifier(rec)
    settings = Settings(padrino_cost_drift_alert_fraction_threshold=0.25)

    assert (
        await evaluate_cost_drift_alert(
            notifier,
            settings,
            observed_cost_usd=0.124,
            expected_cost_usd=0.1,
            model_id="cerebras/zai-glm-4.7",
            price_basis="FALLBACK_TABLE",
        )
        is False
    )
    assert not notifier.is_active(ALERT_COST_DRIFT)

    assert (
        await evaluate_cost_drift_alert(
            notifier,
            settings,
            observed_cost_usd=0.14,
            expected_cost_usd=0.1,
            model_id="cerebras/zai-glm-4.7",
            price_basis="FALLBACK_TABLE",
        )
        is True
    )
    assert notifier.is_active(ALERT_COST_DRIFT)
    drift_alerts = [p for p in rec.posts if p["alert"] == ALERT_COST_DRIFT]
    assert len(drift_alerts) == 1
    assert drift_alerts[0]["drift_fraction"] == 0.4

    assert (
        await evaluate_cost_drift_alert(
            notifier,
            settings,
            observed_cost_usd=0.11,
            expected_cost_usd=0.1,
            model_id="cerebras/zai-glm-4.7",
            price_basis="FALLBACK_TABLE",
        )
        is True
    )
    assert not notifier.is_active(ALERT_COST_DRIFT)
    await notifier.aclose()


# ---------------------------------------------------------------------------
# Prometheus alert-rules file
# ---------------------------------------------------------------------------


def test_alert_rules_file_covers_every_condition() -> None:
    path = Path(__file__).resolve().parents[2] / "docs" / "deployment" / "alert-rules.yml"
    assert path.exists(), "alert-rules.yml must ship with the deploy config"
    doc = yaml.safe_load(path.read_text())
    rules = [r for group in doc["groups"] for r in group["rules"]]
    alert_names = {r["alert"] for r in rules}
    # One Prometheus rule per webhook alert condition.
    assert alert_names == {
        "PadrinoSpendCapReached",
        "PadrinoSchedulerHeartbeatStale",
        "PadrinoModerationGuardUnavailable",
        "PadrinoAdmissionDeniedStreak",
        "PadrinoBudgetBurnThreshold",
        "PadrinoCostDriftThreshold",
    }
    for r in rules:
        assert r["expr"], "every rule needs a PromQL expression"
        assert r["annotations"]["summary"], "every rule needs a summary"
    # Sanity: there is exactly one webhook alert key per Prometheus rule.
    assert len(ALERT_KEYS) == len(alert_names)
