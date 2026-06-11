"""Tests for US-107: anonymous rate limits on new public endpoints + SSE cap.

Asserts that:
* ``/public/live``, ``/public/recent``, ``/public/ladder``, and
  ``/public/games/{id}/live`` all honour the anonymous IP-hash rate limiter
  when ``padrino_public_leaderboard_anonymous`` is enabled.
* The SSE endpoint enforces ``padrino_sse_max_connections_per_ip`` and rejects
  excess connections with 429.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import RateLimiter
from padrino.api.rate_limit_store import InMemoryRateLimitStore
from padrino.api.routes.public import _live_cadence, _sse_active
from padrino.db.models import Game
from padrino.public.broadcast_index import BroadcastState
from padrino.public.broadcaster import CadenceConfig
from padrino.settings import Settings, get_settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _zero_cadence() -> CadenceConfig:
    return CadenceConfig(chat_ms=0, phase_ms=0, elimination_ms=0, resolution_ms=0, default_ms=0)


def _anon_settings(*, rate_limit: int = 1, sse_cap: int = 5) -> Settings:
    """Return settings with anonymous access enabled and a tight rate limit."""
    return Settings(
        padrino_public_leaderboard_anonymous=True,
        padrino_rate_limit_anonymous_per_minute=rate_limit,
        padrino_sse_max_connections_per_ip=sse_cap,
    )


async def _make_live_game(session: AsyncSession) -> Game:
    g = Game(
        ruleset_id="mini7_v1",
        game_seed=f"rl-{uuid.uuid4()}",
        status="RUNNING",
        broadcast_state=BroadcastState.LIVE.value,
        is_broadcastable=True,
    )
    session.add(g)
    await session.flush()
    return g


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_sse_active() -> Iterator[None]:
    _sse_active.clear()
    yield
    _sse_active.clear()


@pytest_asyncio.fixture
async def anon_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """App with anonymous reads enabled and rate limit = 1/min."""
    store = InMemoryRateLimitStore()
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(store=store),
    )
    app.state.auth_settings = _anon_settings(rate_limit=1)
    app.dependency_overrides[_live_cadence] = _zero_cadence
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Anonymous rate limit: /public/live
# ---------------------------------------------------------------------------


async def test_public_live_index_rate_limited(
    anon_client: AsyncClient,
) -> None:
    r1 = await anon_client.get("/public/live")
    assert r1.status_code == 200
    r2 = await anon_client.get("/public/live")
    assert r2.status_code == 429
    assert "Retry-After" in r2.headers


# ---------------------------------------------------------------------------
# Anonymous rate limit: /public/recent
# ---------------------------------------------------------------------------


async def test_public_recent_index_rate_limited(
    anon_client: AsyncClient,
) -> None:
    r1 = await anon_client.get("/public/recent")
    assert r1.status_code == 200
    r2 = await anon_client.get("/public/recent")
    assert r2.status_code == 429
    assert "Retry-After" in r2.headers


# ---------------------------------------------------------------------------
# Anonymous rate limit: /public/ladder
# ---------------------------------------------------------------------------


async def test_public_ladder_rate_limited(
    anon_client: AsyncClient,
) -> None:
    r1 = await anon_client.get("/public/ladder", params={"ruleset_id": "mini7_v1"})
    assert r1.status_code == 200
    r2 = await anon_client.get("/public/ladder", params={"ruleset_id": "mini7_v1"})
    assert r2.status_code == 429
    assert "Retry-After" in r2.headers


# ---------------------------------------------------------------------------
# Anonymous rate limit: /public/games/{id}/live
# ---------------------------------------------------------------------------


async def test_public_live_sse_rate_limited(
    anon_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game = await _make_live_game(session)

    r1 = await anon_client.get(f"/public/games/{game.id}/live")
    assert r1.status_code == 200
    r2 = await anon_client.get(f"/public/games/{game.id}/live")
    assert r2.status_code == 429
    assert "Retry-After" in r2.headers


# ---------------------------------------------------------------------------
# SSE per-IP connection cap
# ---------------------------------------------------------------------------


async def test_sse_cap_allows_connection_within_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A connection that fits within the cap must succeed."""
    store = InMemoryRateLimitStore()
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(store=store),
    )
    app.state.auth_settings = _anon_settings(rate_limit=1000, sse_cap=2)
    app.dependency_overrides[_live_cadence] = _zero_cadence

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        async with session_factory() as session, session.begin():
            game = await _make_live_game(session)

        r = await client.get(
            f"/public/games/{game.id}/live",
            headers={"X-Forwarded-For": "10.0.0.1"},
        )
        assert r.status_code == 200


async def test_sse_cap_rejects_excess_connection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pre-loading _sse_active to the cap causes the next connection to 429."""
    store = InMemoryRateLimitStore()
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(store=store),
    )
    app.state.auth_settings = _anon_settings(rate_limit=1000, sse_cap=1)
    app.dependency_overrides[_live_cadence] = _zero_cadence

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        async with session_factory() as session, session.begin():
            game = await _make_live_game(session)

        test_ip = "10.0.0.2"
        ip_hash = hashlib.sha256(f"ip:{test_ip}".encode()).hexdigest()
        forwarded = {"X-Forwarded-For": test_ip}

        # First connection succeeds and the finally block decrements back to 0.
        r1 = await client.get(f"/public/games/{game.id}/live", headers=forwarded)
        assert r1.status_code == 200
        assert _sse_active.get(ip_hash, 0) == 0

        # Simulate a stuck-open connection from this IP (cap = 1).
        _sse_active[ip_hash] = 1
        try:
            r2 = await client.get(f"/public/games/{game.id}/live", headers=forwarded)
            assert r2.status_code == 429
            assert r2.json()["detail"] == "sse_connection_limit_exceeded"
        finally:
            _sse_active.pop(ip_hash, None)


async def test_sse_cap_decremented_after_stream_completes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """_sse_active must return to 0 after a successful SSE stream finishes."""
    store = InMemoryRateLimitStore()
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(store=store),
    )
    app.state.auth_settings = _anon_settings(rate_limit=1000, sse_cap=5)
    app.dependency_overrides[_live_cadence] = _zero_cadence

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        async with session_factory() as session, session.begin():
            game = await _make_live_game(session)

        test_ip = "10.0.0.3"
        ip_hash = hashlib.sha256(f"ip:{test_ip}".encode()).hexdigest()

        r = await client.get(
            f"/public/games/{game.id}/live",
            headers={"X-Forwarded-For": test_ip},
        )
        assert r.status_code == 200
        # Generator finished; finally block must have decremented to 0.
        assert _sse_active.get(ip_hash, 0) == 0
