"""Tests for US-110: public-surface-only API mode.

When ``create_app(public_surface_only=True)`` (or the
``PADRINO_PUBLIC_SURFACE_ONLY`` setting), the internet-facing process mounts
ONLY the public spectator router and the health probes. Every private router
(admin, admin_keys, ingest, games, leagues, gauntlets, scheduled_gauntlets)
and ``/metrics`` are not registered at all, so a request to any private prefix
returns 404 (route does not exist) rather than 401/403. The public surface
(``/public/*``) and ``/healthz`` / ``/readyz`` keep working.

With the flag off (the default) behavior is unchanged: every private route is
still mounted and ``/metrics`` is served.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import RateLimiter
from padrino.api.routes.public import _live_cadence
from padrino.db.models import Game
from padrino.public.broadcast_index import BroadcastState
from padrino.public.broadcaster import CadenceConfig
from padrino.settings import Settings, get_settings

# Concrete paths served by each private router. In public-surface-only mode the
# route does not exist at all (404); with the flag off the route is mounted, so
# it answers with a non-404 status (200/401/403/405/422 depending on the route).
# One representative path per private router:
#   admin_keys   -> /admin/keys
#   leagues      -> /leagues/{id}/leaderboard
#   gauntlets    -> /gauntlets
#   games        -> /games
#   ingest       -> /ingest/game (POST-only; GET 405 when mounted, 404 when not)
#   scheduled_gauntlets -> POST /scheduled-gauntlets (handled separately below)
_PRIVATE_GET_PATHS = (
    "/model-providers",
    "/admin/keys",
    "/leagues/00000000-0000-0000-0000-000000000000/leaderboard",
    "/gauntlets",
    "/games",
    "/ingest/game",
)


def _zero_cadence() -> CadenceConfig:
    return CadenceConfig(chat_ms=0, phase_ms=0, elimination_ms=0, resolution_ms=0, default_ms=0)


def _anon_settings(*, public_surface_only: bool = False) -> Settings:
    """Settings with anonymous public reads so the public surface needs no key."""
    return Settings(
        padrino_public_leaderboard_anonymous=True,
        padrino_rate_limit_anonymous_per_minute=10_000,
        padrino_public_surface_only=public_surface_only,
    )


async def _make_live_game(session: AsyncSession) -> Game:
    g = Game(
        ruleset_id="mini7_v1",
        game_seed=f"surface-{uuid.uuid4()}",
        status="RUNNING",
        broadcast_state=BroadcastState.LIVE.value,
        current_phase="DAY_1",
        is_broadcastable=True,
    )
    session.add(g)
    await session.flush()
    return g


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def surface_only_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """App built in public-surface-only mode with anonymous public reads on."""
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
        public_surface_only=True,
    )
    app.state.auth_settings = _anon_settings(public_surface_only=True)
    app.dependency_overrides[_live_cadence] = _zero_cadence
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest_asyncio.fixture
async def full_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """App built with the flag OFF (default) — full surface mounted."""
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
    )
    app.state.auth_settings = _anon_settings(public_surface_only=False)
    app.dependency_overrides[_live_cadence] = _zero_cadence
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Flag ON: private routes 404, public + health work, /metrics gone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _PRIVATE_GET_PATHS)
async def test_private_routes_404_in_surface_only(
    surface_only_client: AsyncClient,
    path: str,
) -> None:
    """Private prefixes must 404 (route not mounted), never 401/403."""
    r = await surface_only_client.get(path)
    assert r.status_code == 404, f"{path} returned {r.status_code}, expected 404"


async def test_scheduled_gauntlets_router_not_mounted_in_surface_only(
    surface_only_client: AsyncClient,
) -> None:
    """The whole scheduled_gauntlets router is gated off (AC: not mounted at all).

    Its sole public route ``/public/scheduled-gauntlets`` therefore 404s too —
    the public spectator surface that must work is enumerated in the AC and does
    not include this internal scheduling route.
    """
    assert (await surface_only_client.get("/public/scheduled-gauntlets")).status_code == 404


async def test_metrics_not_served_in_surface_only(
    surface_only_client: AsyncClient,
) -> None:
    r = await surface_only_client.get("/metrics")
    assert r.status_code == 404


async def test_health_probes_work_in_surface_only(
    surface_only_client: AsyncClient,
) -> None:
    rz = await surface_only_client.get("/healthz")
    assert rz.status_code == 200
    assert rz.json()["status"] == "ok"

    ready = await surface_only_client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ok"


async def test_public_index_routes_work_in_surface_only(
    surface_only_client: AsyncClient,
) -> None:
    for path in (
        "/public/live",
        "/public/recent",
        "/public/rulesets",
        "/public/ladder?ruleset_id=mini7_v1",
    ):
        r = await surface_only_client.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code}"


async def test_public_sse_route_works_in_surface_only(
    surface_only_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game = await _make_live_game(session)

    r = await surface_only_client.get(f"/public/games/{game.id}/live")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Flag OFF (default): behavior unchanged — private routes mounted, /metrics on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _PRIVATE_GET_PATHS)
async def test_private_routes_mounted_when_flag_off(
    full_client: AsyncClient,
    path: str,
) -> None:
    """With the flag off, private routes exist — they may 401/403/200 but never 404."""
    r = await full_client.get(path)
    assert r.status_code != 404, f"{path} unexpectedly 404 with flag off"


async def test_metrics_served_when_flag_off(
    full_client: AsyncClient,
) -> None:
    r = await full_client.get("/metrics")
    assert r.status_code == 200


async def test_public_and_health_work_when_flag_off(
    full_client: AsyncClient,
) -> None:
    assert (await full_client.get("/healthz")).status_code == 200
    assert (await full_client.get("/public/live")).status_code == 200


async def test_default_settings_flag_is_false() -> None:
    assert Settings().padrino_public_surface_only is False
