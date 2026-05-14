"""Tests for :mod:`padrino.observability.metrics`.

Seeds a SQLite database via the demo gauntlet (NoopMockAdapter, 2 clones,
ranked=True) and asserts the aggregated summary covers games_completed,
phase durations, per-model latencies, timeout rate, and invalid_json rate.

Also exercises edge cases on an empty database.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.repositories import (
    agent_builds,
    leagues,
    model_configs,
    prompt_versions,
    providers,
)
from padrino.demo_gauntlet import run_demo_gauntlet
from padrino.observability.metrics import (
    UNKNOWN_MODEL,
    LatencyStats,
    _percentile,
    compute_metrics_summary,
    metrics_summary_to_dict,
)


@pytest.fixture
async def empty_engine() -> AsyncIterator[AsyncEngine]:
    eng = create_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def empty_session_factory(
    empty_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(empty_engine)


def test_percentile_nearest_rank() -> None:
    """``_percentile`` uses nearest-rank with clamping at the endpoints."""
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert _percentile([], 0.5) is None
    assert _percentile([42], 0.5) == 42
    assert _percentile([42], 0.95) == 42
    assert _percentile(values, 0.0) == 10
    assert _percentile(values, 1.0) == 100
    assert _percentile(values, 0.5) == 50
    assert _percentile(values, 0.95) == 100


async def test_empty_db_returns_neutral_summary(
    empty_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """All rates are None and counts are 0 when the DB has no rows."""
    async with empty_session_factory() as session:
        summary = await compute_metrics_summary(session)
    assert summary.games_completed == 0
    assert summary.avg_phase_duration_seconds is None
    assert summary.llm_latency == {}
    assert summary.timeout_rate is None
    assert summary.invalid_json_rate is None
    assert summary.total_llm_calls == 0
    assert summary.total_timeouts == 0


async def test_demo_gauntlet_yields_nonzero_metrics(tmp_path: object) -> None:
    """A 2-clone demo gauntlet seeds enough rows to populate every field."""
    db_path = f"{tmp_path}/metrics.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    await run_demo_gauntlet(seed="metrics-seed-001", clones=2, db_url=db_url)

    engine = create_engine(db_url)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            summary = await compute_metrics_summary(session)
    finally:
        await engine.dispose()

    assert summary.games_completed == 2
    assert summary.avg_phase_duration_seconds is not None
    assert summary.avg_phase_duration_seconds >= 0.0
    assert summary.total_llm_calls > 0
    assert summary.total_timeouts == 0  # NoopMockAdapter never times out
    assert summary.timeout_rate == 0.0
    assert summary.invalid_json_rate == 0.0
    assert summary.llm_latency, "expected at least one model bucket"
    for stats in summary.llm_latency.values():
        assert isinstance(stats, LatencyStats)
        assert stats.samples > 0
        assert stats.p50_ms is not None
        assert stats.p95_ms is not None
        assert stats.p50_ms <= stats.p95_ms


async def test_orphan_llm_calls_bucket_as_unknown(
    empty_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``llm_calls`` rows without an agent_build_id roll up under 'unknown'."""
    from padrino.db.repositories import games as games_repo
    from padrino.db.repositories import llm_calls as llm_calls_repo

    async with empty_session_factory() as session, session.begin():
        game = await games_repo.create(session, ruleset_id="mini7_v1", game_seed="orphan-seed")
        for latency in (100, 200, 300):
            await llm_calls_repo.record_call(
                session,
                game_id=game.id,
                public_player_id="P01",
                phase="DAY_1_VOTE",
                request_json={"phase": "DAY_1_VOTE"},
                request_prompt_hash=f"hash-{latency}",
                status="ok",
                latency_ms=latency,
            )

    async with empty_session_factory() as session:
        summary = await compute_metrics_summary(session)

    assert summary.total_llm_calls == 3
    assert UNKNOWN_MODEL in summary.llm_latency
    stats = summary.llm_latency[UNKNOWN_MODEL]
    assert stats.samples == 3
    assert stats.p50_ms is not None
    assert stats.p95_ms is not None


async def test_llm_calls_grouped_by_model_name(
    empty_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When agent_build_id resolves to a model_config, latencies bucket per model."""
    from padrino.db.repositories import games as games_repo
    from padrino.db.repositories import llm_calls as llm_calls_repo

    async with empty_session_factory() as session, session.begin():
        provider = await providers.create(session, name="p", auth_secret_ref="env:X")
        mc_a = await model_configs.create(
            session,
            provider_id=provider.id,
            model_name="model-a",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        mc_b = await model_configs.create(
            session,
            provider_id=provider.id,
            model_name="model-b",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        pv = await prompt_versions.create(
            session,
            ruleset_id="mini7_v1",
            version="v1",
            system_prompt="s",
            developer_prompt="d",
            response_schema={"type": "object"},
            prompt_hash=f"ph-{uuid.uuid4().hex}",
        )
        ab_a = await agent_builds.create(
            session,
            display_name="A",
            model_config_id=mc_a.id,
            prompt_version_id=pv.id,
            adapter_version="v",
            inference_params={},
            active=True,
        )
        ab_b = await agent_builds.create(
            session,
            display_name="B",
            model_config_id=mc_b.id,
            prompt_version_id=pv.id,
            adapter_version="v",
            inference_params={},
            active=True,
        )
        await leagues.create(session, name="L", ruleset_id="mini7_v1", ranked=True)
        game = await games_repo.create(session, ruleset_id="mini7_v1", game_seed="model-bucket")
        for latency in (50, 70, 110):
            await llm_calls_repo.record_call(
                session,
                game_id=game.id,
                agent_build_id=ab_a.id,
                public_player_id="P01",
                phase="DAY_1_VOTE",
                request_json={"phase": "DAY_1_VOTE"},
                request_prompt_hash=f"a-{latency}",
                status="ok",
                latency_ms=latency,
            )
        for latency in (400, 500):
            await llm_calls_repo.record_call(
                session,
                game_id=game.id,
                agent_build_id=ab_b.id,
                public_player_id="P02",
                phase="DAY_1_VOTE",
                request_json={"phase": "DAY_1_VOTE"},
                request_prompt_hash=f"b-{latency}",
                status="invalid_json",
                latency_ms=latency,
            )

    async with empty_session_factory() as session:
        summary = await compute_metrics_summary(session)

    assert summary.total_llm_calls == 5
    assert summary.invalid_json_rate == pytest.approx(2 / 5)
    assert "model-a" in summary.llm_latency
    assert "model-b" in summary.llm_latency
    assert summary.llm_latency["model-a"].samples == 3
    assert summary.llm_latency["model-b"].samples == 2
    assert summary.llm_latency["model-a"].p50_ms == 70
    assert summary.llm_latency["model-b"].p95_ms == 500


async def test_phase_duration_pairs_started_and_resolved(
    empty_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """avg_phase_duration uses created_at deltas between matched events."""
    from padrino.db.models import Game, GameEvent

    async with empty_session_factory() as session, session.begin():
        game = Game(
            ruleset_id="mini7_v1",
            game_seed="phase-pair",
            status="COMPLETED",
        )
        session.add(game)
        await session.flush()
        base = datetime(2026, 1, 1, tzinfo=UTC)
        rows = [
            GameEvent(
                game_id=game.id,
                sequence=0,
                event_type="PhaseStarted",
                phase="DAY_1_DISCUSSION_ROUND_1",
                visibility="SYSTEM",
                actor_player_id=None,
                payload={},
                prev_event_hash="0" * 64,
                event_hash="a" * 64,
                created_at=base,
            ),
            GameEvent(
                game_id=game.id,
                sequence=1,
                event_type="PhaseResolved",
                phase="DAY_1_DISCUSSION_ROUND_1",
                visibility="SYSTEM",
                actor_player_id=None,
                payload={},
                prev_event_hash="a" * 64,
                event_hash="b" * 64,
                created_at=base + timedelta(seconds=2),
            ),
            GameEvent(
                game_id=game.id,
                sequence=2,
                event_type="PhaseStarted",
                phase="DAY_1_VOTE",
                visibility="SYSTEM",
                actor_player_id=None,
                payload={},
                prev_event_hash="b" * 64,
                event_hash="c" * 64,
                created_at=base + timedelta(seconds=3),
            ),
            GameEvent(
                game_id=game.id,
                sequence=3,
                event_type="PhaseResolved",
                phase="DAY_1_VOTE",
                visibility="SYSTEM",
                actor_player_id=None,
                payload={},
                prev_event_hash="c" * 64,
                event_hash="d" * 64,
                created_at=base + timedelta(seconds=7),
            ),
        ]
        for row in rows:
            session.add(row)

    async with empty_session_factory() as session:
        summary = await compute_metrics_summary(session)

    assert summary.avg_phase_duration_seconds == pytest.approx(3.0)


def test_metrics_summary_to_dict_sorts_model_keys() -> None:
    """``metrics_summary_to_dict`` returns model_name keys in sorted order."""
    from padrino.observability.metrics import MetricsSummary

    summary = MetricsSummary(
        games_completed=1,
        avg_phase_duration_seconds=0.5,
        llm_latency={
            "zebra": LatencyStats(samples=1, p50_ms=10, p95_ms=10),
            "alpha": LatencyStats(samples=2, p50_ms=20, p95_ms=30),
        },
        timeout_rate=0.0,
        invalid_json_rate=0.0,
        total_llm_calls=3,
        total_timeouts=0,
    )
    payload = metrics_summary_to_dict(summary)
    assert list(payload["llm_latency"].keys()) == ["alpha", "zebra"]
