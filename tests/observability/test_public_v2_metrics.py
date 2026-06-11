"""Tests for US-107: broadcast metrics exist and are updated by SSE requests.

Verifies that ``padrino_broadcast_active_streams`` and
``padrino_broadcast_frames_total`` are wired into the Prometheus surface and
updated correctly by the ``GET /public/games/{id}/live`` SSE endpoint.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import SCOPE_SPECTATOR, RateLimiter, generate_raw_key
from padrino.api.routes.public import _live_cadence, _sse_active
from padrino.db.models import Game, GameEvent
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.observability.metrics import (
    broadcast_active_streams,
    record_broadcast_frame,
    render_prometheus_text,
    reset_metrics,
    text_string_to_metric_families,
)
from padrino.public.broadcast_index import BroadcastState
from padrino.public.broadcaster import CadenceConfig
from padrino.settings import get_settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _zero_cadence() -> CadenceConfig:
    return CadenceConfig(chat_ms=0, phase_ms=0, elimination_ms=0, resolution_ms=0, default_ms=0)


def _samples(text_payload: str, name: str) -> list[tuple[dict[str, str], float]]:
    out: list[tuple[dict[str, str], float]] = []
    for family in text_string_to_metric_families(text_payload):
        if family.name != name:
            continue
        for sample in family.samples:
            out.append((dict(sample.labels), sample.value))
    return out


async def _make_live_game(session: AsyncSession, n_events: int = 2) -> Game:
    g = Game(
        ruleset_id="mini7_v1",
        game_seed=f"met-{uuid.uuid4()}",
        status="RUNNING",
        broadcast_state=BroadcastState.LIVE.value,
        is_broadcastable=True,
    )
    session.add(g)
    await session.flush()
    for seq in range(1, n_events + 1):
        session.add(
            GameEvent(
                game_id=g.id,
                sequence=seq,
                event_type="PhaseStarted",
                phase="DAY_1_DISCUSSION_ROUND_1",
                visibility="PUBLIC",
                prev_event_hash="0" * 64,
                event_hash=f"{seq:064x}",
                payload={},
            )
        )
    return g


async def _seed_key(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scopes: list[str],
    label: str,
) -> str:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        await api_keys_repo.create(session, raw_key=raw, scopes=scopes, label=label)
    return raw


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_metrics() -> Iterator[None]:
    reset_metrics()
    _sse_active.clear()
    yield
    reset_metrics()
    _sse_active.clear()


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
    )
    app.dependency_overrides[_live_cadence] = _zero_cadence
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Instrument existence: metrics appear in Prometheus output
# ---------------------------------------------------------------------------


def test_broadcast_active_streams_gauge_exists_in_registry() -> None:
    payload = render_prometheus_text().decode("utf-8")
    names = {f.name for f in text_string_to_metric_families(payload)}
    assert "padrino_broadcast_active_streams" in names


def test_broadcast_frames_total_counter_exists_in_registry() -> None:
    record_broadcast_frame()
    payload = render_prometheus_text().decode("utf-8")
    names = {f.name for f in text_string_to_metric_families(payload)}
    assert "padrino_broadcast_frames" in names


def test_broadcast_metrics_in_prometheus_endpoint_names() -> None:
    """Both broadcast metric names must appear in the full set of instruments."""
    record_broadcast_frame()
    broadcast_active_streams.inc()
    payload = render_prometheus_text().decode("utf-8")
    names = {f.name for f in text_string_to_metric_families(payload)}
    assert "padrino_broadcast_active_streams" in names
    assert "padrino_broadcast_frames" in names


# ---------------------------------------------------------------------------
# Frame counter: incremented per frame emitted
# ---------------------------------------------------------------------------


async def test_broadcast_frames_total_incremented_per_frame(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        game = await _make_live_game(session, n_events=3)

    response = await client.get(f"/public/games/{game.id}/live", headers=_auth(raw))
    assert response.status_code == 200

    payload = render_prometheus_text().decode("utf-8")
    samples = _samples(payload, "padrino_broadcast_frames")
    total = sum(v for _, v in samples)
    assert total == 3.0, f"expected 3 frames; got {total}"


async def test_broadcast_frames_total_accumulates_across_requests(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        game = await _make_live_game(session, n_events=2)

    await client.get(f"/public/games/{game.id}/live", headers=_auth(raw))
    await client.get(f"/public/games/{game.id}/live", headers=_auth(raw))

    payload = render_prometheus_text().decode("utf-8")
    samples = _samples(payload, "padrino_broadcast_frames")
    total = sum(v for _, v in samples)
    assert total == 4.0, f"expected 4 frames (2 per request); got {total}"


# ---------------------------------------------------------------------------
# Active streams gauge: zero after request completes
# ---------------------------------------------------------------------------


async def test_broadcast_active_streams_zero_after_sse_completes(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Gauge must return to 0 once the generator finishes (finally block)."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        game = await _make_live_game(session, n_events=2)

    response = await client.get(f"/public/games/{game.id}/live", headers=_auth(raw))
    assert response.status_code == 200

    payload = render_prometheus_text().decode("utf-8")
    samples = _samples(payload, "padrino_broadcast_active_streams")
    assert all(v == 0.0 for _, v in samples), f"gauge must be 0 after request; got {samples}"


# ---------------------------------------------------------------------------
# reset_metrics clears broadcast instruments
# ---------------------------------------------------------------------------


def test_reset_clears_broadcast_frames_counter() -> None:
    record_broadcast_frame()
    record_broadcast_frame()
    reset_metrics()
    payload = render_prometheus_text().decode("utf-8")
    samples = _samples(payload, "padrino_broadcast_frames")
    # After reset() the _total sample is 0.0; _created is a Unix timestamp.
    assert any(v == 0.0 for _, v in samples), f"total must be 0 after reset; got {samples}"


def test_reset_zeroes_broadcast_active_streams_gauge() -> None:
    broadcast_active_streams.inc()
    broadcast_active_streams.inc()
    reset_metrics()
    payload = render_prometheus_text().decode("utf-8")
    samples = _samples(payload, "padrino_broadcast_active_streams")
    assert all(v == 0.0 for _, v in samples), f"gauge must be 0 after reset; got {samples}"
