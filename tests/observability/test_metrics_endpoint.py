"""Tests for the Prometheus ``/metrics`` endpoint (US-059).

Exercises the metric instruments via the public helpers, asserts the
endpoint output parses with :func:`prometheus_client.parser.text_string_to_metric_families`,
and confirms ``Settings.padrino_metrics_require_auth`` gates the route
behind the spectator scope when flipped.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Iterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import RateLimiter, generate_raw_key
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.observability.metrics import (
    CONTENT_TYPE_LATEST,
    api_requests_total,
    games_total,
    invalid_action_total,
    llm_calls_total,
    llm_latency_seconds,
    phase_duration_seconds,
    record_game_completed,
    record_invalid_action,
    record_llm_call,
    record_phase_duration,
    render_prometheus_text,
    reset_metrics,
    scheduler_inflight_gauntlets,
    split_litellm_model_id,
    text_string_to_metric_families,
)


@pytest.fixture(autouse=True)
def _clean_metrics() -> Iterator[None]:
    """Each test starts with a freshly reset metrics registry."""
    reset_metrics()
    yield
    reset_metrics()


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


async def _client(app: object) -> AsyncClient:
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    return AsyncClient(transport=transport, base_url="http://testserver")


def _samples(text_payload: str, name: str) -> list[tuple[dict[str, str], float]]:
    """Return ``[(labels, value)]`` for every sample of ``name`` in the payload."""
    out: list[tuple[dict[str, str], float]] = []
    for family in text_string_to_metric_families(text_payload):
        if family.name != name:
            continue
        for sample in family.samples:
            out.append((dict(sample.labels), sample.value))
    return out


def _sum_with_labels(
    samples: Iterable[tuple[dict[str, str], float]],
    required: dict[str, str],
) -> float:
    total = 0.0
    for labels, value in samples:
        if all(labels.get(k) == v for k, v in required.items()):
            total += value
    return total


# --- split_litellm_model_id -------------------------------------------------


def test_split_provider_and_model() -> None:
    assert split_litellm_model_id("cerebras/zai-glm-4.7") == ("cerebras", "zai-glm-4.7")
    assert split_litellm_model_id("anthropic/claude-haiku-4-5") == (
        "anthropic",
        "claude-haiku-4-5",
    )


def test_split_without_provider_buckets_as_unknown() -> None:
    assert split_litellm_model_id("gpt-4o-mini") == ("unknown", "gpt-4o-mini")


def test_split_empty_buckets_as_unknown() -> None:
    assert split_litellm_model_id(None) == ("unknown", "unknown")
    assert split_litellm_model_id("") == ("unknown", "unknown")


def test_split_handles_deepinfra_nested_path() -> None:
    """``deepinfra/deepseek-ai/DeepSeek-V4-Flash`` keeps everything after the first slash."""
    assert split_litellm_model_id("deepinfra/deepseek-ai/DeepSeek-V4-Flash") == (
        "deepinfra",
        "deepseek-ai/DeepSeek-V4-Flash",
    )


# --- counter / histogram helpers --------------------------------------------


def test_record_llm_call_increments_counter_and_histogram() -> None:
    record_llm_call(model_id="cerebras/zai-glm-4.7", status="ok", latency_ms=240)
    record_llm_call(model_id="cerebras/zai-glm-4.7", status="ok", latency_ms=120)
    record_llm_call(model_id="cerebras/zai-glm-4.7", status="invalid_json", latency_ms=80)

    payload = render_prometheus_text().decode("utf-8")
    counts = _samples(payload, "padrino_llm_calls")
    ok = _sum_with_labels(counts, {"provider": "cerebras", "model": "zai-glm-4.7", "status": "ok"})
    invalid = _sum_with_labels(
        counts, {"provider": "cerebras", "model": "zai-glm-4.7", "status": "invalid_json"}
    )
    assert ok == 2.0
    assert invalid == 1.0

    histo = _samples(payload, "padrino_llm_latency_seconds")
    sample_count = _sum_with_labels(histo, {"provider": "cerebras", "model": "zai-glm-4.7"})
    # `_count` aggregates 3 observations; `_sum` adds latencies; bucket samples
    # appear once per cumulative bound. We pull the total observation count.
    counted = [
        v
        for labels, v in histo
        if labels.get("provider") == "cerebras" and labels.get("model") == "zai-glm-4.7"
    ]
    assert any(v == 3.0 for v in counted), counted
    assert sample_count > 0


def test_record_phase_duration_observes_histogram() -> None:
    record_phase_duration(ruleset="mini7_v1", phase_kind="DAY_VOTE", duration_s=1.25)
    record_phase_duration(ruleset="mini7_v1", phase_kind="DAY_VOTE", duration_s=2.5)
    record_phase_duration(ruleset="mini7_v1", phase_kind="DAY_VOTE", duration_s=-1.0)

    payload = render_prometheus_text().decode("utf-8")
    samples = _samples(payload, "padrino_phase_duration_seconds")
    counted = [
        v
        for labels, v in samples
        if labels.get("ruleset") == "mini7_v1" and labels.get("phase_kind") == "DAY_VOTE"
    ]
    # Two valid observations; the negative one is ignored.
    assert any(v == 2.0 for v in counted), counted


def test_record_game_completed_counts_by_outcome() -> None:
    record_game_completed(outcome="TOWN", ruleset="mini7_v1")
    record_game_completed(outcome="TOWN", ruleset="mini7_v1")
    record_game_completed(outcome="MAFIA", ruleset="mini7_v1")

    payload = render_prometheus_text().decode("utf-8")
    counts = _samples(payload, "padrino_games")
    assert _sum_with_labels(counts, {"outcome": "TOWN", "ruleset": "mini7_v1"}) == 2.0
    assert _sum_with_labels(counts, {"outcome": "MAFIA", "ruleset": "mini7_v1"}) == 1.0


def test_record_invalid_action_counts_by_reason() -> None:
    record_invalid_action(reason="TIMEOUT")
    record_invalid_action(reason="INVALID_JSON")
    record_invalid_action(reason="TIMEOUT")

    payload = render_prometheus_text().decode("utf-8")
    counts = _samples(payload, "padrino_invalid_action")
    assert _sum_with_labels(counts, {"reason": "TIMEOUT"}) == 2.0
    assert _sum_with_labels(counts, {"reason": "INVALID_JSON"}) == 1.0


def test_scheduler_inflight_gauge_inc_dec() -> None:
    scheduler_inflight_gauntlets.inc()
    scheduler_inflight_gauntlets.inc()
    payload = render_prometheus_text().decode("utf-8")
    samples = _samples(payload, "padrino_scheduler_inflight_gauntlets")
    assert any(v == 2.0 for _, v in samples), samples
    scheduler_inflight_gauntlets.dec()
    payload = render_prometheus_text().decode("utf-8")
    samples = _samples(payload, "padrino_scheduler_inflight_gauntlets")
    assert any(v == 1.0 for _, v in samples), samples


def test_reset_clears_every_collector() -> None:
    record_llm_call(model_id="x/y", status="ok", latency_ms=10)
    record_phase_duration(ruleset="mini7_v1", phase_kind="DAY_VOTE", duration_s=0.5)
    record_game_completed(outcome="TOWN", ruleset="mini7_v1")
    record_invalid_action(reason="TIMEOUT")
    scheduler_inflight_gauntlets.inc()

    reset_metrics()

    payload = render_prometheus_text().decode("utf-8")
    assert not _samples(payload, "padrino_llm_calls")
    assert not _samples(payload, "padrino_phase_duration_seconds")
    assert not _samples(payload, "padrino_games")
    assert not _samples(payload, "padrino_invalid_action")
    gauge_samples = _samples(payload, "padrino_scheduler_inflight_gauntlets")
    assert all(v == 0.0 for _, v in gauge_samples), gauge_samples


# --- endpoint integration ---------------------------------------------------


async def test_metrics_endpoint_returns_prometheus_text() -> None:
    app = create_app()
    record_llm_call(model_id="cerebras/zai-glm-4.7", status="ok", latency_ms=42)
    client = await _client(app)
    async with client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "padrino_llm_calls_total" in response.text
    # The payload must parse with the official prometheus parser.
    families = list(text_string_to_metric_families(response.text))
    names = {f.name for f in families}
    assert "padrino_llm_calls" in names
    assert "padrino_api_requests" in names


async def test_metrics_endpoint_records_self_request() -> None:
    """The middleware should count the /metrics request itself."""
    app = create_app()
    client = await _client(app)
    async with client:
        await client.get("/metrics")
        response = await client.get("/metrics")
    families = list(text_string_to_metric_families(response.text))
    counts = [f for f in families if f.name == "padrino_api_requests"]
    assert counts, "expected padrino_api_requests samples"
    found = False
    for family in counts:
        for sample in family.samples:
            if (
                sample.labels.get("route") == "/metrics"
                and sample.labels.get("method") == "GET"
                and sample.labels.get("status") == "200"
            ):
                # First request was observed by the middleware before the second
                # GET captured the payload; second GET hasn't been counted yet.
                assert sample.value >= 1.0
                found = True
    assert found


async def test_metrics_endpoint_open_by_default() -> None:
    """Without ``metrics_require_auth`` the endpoint accepts unauthenticated GETs."""
    app = create_app(auth_required=True, metrics_require_auth=False)
    client = await _client(app)
    async with client:
        response = await client.get("/metrics")
    assert response.status_code == 200


async def test_metrics_endpoint_requires_spectator_when_gated(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When ``metrics_require_auth=True`` only valid Bearer tokens get through."""
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        admin_token="legacy",
        rate_limiter=RateLimiter(),
        metrics_require_auth=True,
    )
    client = await _client(app)
    async with client:
        # No credentials → 401.
        response = await client.get("/metrics")
        assert response.status_code == 401

        # Seed a spectator key, retry.
        raw = generate_raw_key()
        async with session_factory() as session, session.begin():
            await api_keys_repo.create(
                session,
                raw_key=raw,
                scopes=["spectator"],
                label="metrics-test",
            )
        ok = await client.get("/metrics", headers={"Authorization": f"Bearer {raw}"})
        assert ok.status_code == 200
        assert ok.headers["content-type"].startswith("text/plain")


async def test_metrics_endpoint_content_type_matches_prometheus_spec() -> None:
    app = create_app()
    client = await _client(app)
    async with client:
        response = await client.get("/metrics")
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST


# --- module-level smoke -----------------------------------------------------


def test_metrics_module_exposes_canonical_instruments() -> None:
    """Sanity-check: the instruments imported above are the registered ones."""
    payload = render_prometheus_text().decode("utf-8")
    # Counters / histograms appear once they have at least one sample; this
    # smoke ensures the imports above are the canonical instances by touching
    # each and asserting the names show up.
    llm_calls_total.labels(provider="p", model="m", status="ok").inc()
    llm_latency_seconds.labels(provider="p", model="m").observe(0.01)
    phase_duration_seconds.labels(ruleset="r", phase_kind="DAY_VOTE").observe(0.01)
    games_total.labels(outcome="DRAW", ruleset="r").inc()
    invalid_action_total.labels(reason="REASON").inc()
    api_requests_total.labels(route="/x", method="GET", status="200").inc()
    payload = render_prometheus_text().decode("utf-8")
    names = {f.name for f in text_string_to_metric_families(payload)}
    assert {
        "padrino_llm_calls",
        "padrino_llm_latency_seconds",
        "padrino_phase_duration_seconds",
        "padrino_games",
        "padrino_invalid_action",
        "padrino_api_requests",
        "padrino_scheduler_inflight_gauntlets",
    }.issubset(names)
