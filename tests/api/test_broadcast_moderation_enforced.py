"""Tests for US-094: Only broadcastable games reach the public surface.

Asserts that a non-broadcastable game (is_broadcastable=False) is invisible
across all three public surfaces:
  * GET /public/games/{id}/live  -> 404
  * GET /public/live             -> absent from items list
  * GET /public/recent           -> absent from items list

Also asserts that a broadcastable game (is_broadcastable=True) IS visible,
and that mark_live / mark_recent refuse to promote non-broadcastable games.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import SCOPE_SPECTATOR, RateLimiter, generate_raw_key
from padrino.api.routes.public import _live_cadence
from padrino.db.models import Game
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.public.broadcast_index import BroadcastState, mark_live, mark_recent
from padrino.public.broadcaster import CadenceConfig
from padrino.settings import get_settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _zero_cadence() -> CadenceConfig:
    return CadenceConfig(chat_ms=0, phase_ms=0, elimination_ms=0, resolution_ms=0, default_ms=0)


async def _make_game(
    session: AsyncSession,
    *,
    broadcast_state: str = BroadcastState.LIVE.value,
    is_broadcastable: bool = True,
    terminal_result: dict[str, Any] | None = None,
    status: str = "RUNNING",
) -> Game:
    g = Game(
        ruleset_id="mini7_v1",
        game_seed=f"seed-{uuid.uuid4()}",
        status=status,
        terminal_result=terminal_result,
        broadcast_state=broadcast_state,
        is_broadcastable=is_broadcastable,
    )
    session.add(g)
    await session.flush()
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


@pytest_asyncio.fixture
async def spectator_token(session_factory: async_sessionmaker[AsyncSession]) -> str:
    return await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="spectator-094")


# ---------------------------------------------------------------------------
# SSE endpoint: non-broadcastable game -> 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_non_broadcastable_returns_404(
    client: AsyncClient,
    spectator_token: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A LIVE game with is_broadcastable=False returns 404 from the SSE endpoint."""
    async with session_factory() as session, session.begin():
        game = await _make_game(
            session,
            broadcast_state=BroadcastState.LIVE.value,
            is_broadcastable=False,
        )
        game_id = game.id

    resp = await client.get(
        f"/public/games/{game_id}/live",
        headers=_auth(spectator_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_sse_broadcastable_live_game_streams(
    client: AsyncClient,
    spectator_token: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A LIVE game with is_broadcastable=True streams correctly."""
    async with session_factory() as session, session.begin():
        game = await _make_game(
            session,
            broadcast_state=BroadcastState.LIVE.value,
            is_broadcastable=True,
        )
        game_id = game.id

    resp = await client.get(
        f"/public/games/{game_id}/live",
        headers=_auth(spectator_token),
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_sse_recent_non_broadcastable_returns_404(
    client: AsyncClient,
    spectator_token: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A RECENT game with is_broadcastable=False returns 404 from the SSE endpoint."""
    async with session_factory() as session, session.begin():
        game = await _make_game(
            session,
            broadcast_state=BroadcastState.RECENT.value,
            is_broadcastable=False,
            status="COMPLETED",
            terminal_result={"winner": "town"},
        )
        game_id = game.id

    resp = await client.get(
        f"/public/games/{game_id}/live",
        headers=_auth(spectator_token),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /public/live index: non-broadcastable game absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_index_excludes_non_broadcastable(
    client: AsyncClient,
    spectator_token: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A LIVE game with is_broadcastable=False does not appear in /public/live."""
    async with session_factory() as session, session.begin():
        bad_game = await _make_game(
            session,
            broadcast_state=BroadcastState.LIVE.value,
            is_broadcastable=False,
        )
        good_game = await _make_game(
            session,
            broadcast_state=BroadcastState.LIVE.value,
            is_broadcastable=True,
        )
        bad_id = str(bad_game.id)
        good_id = str(good_game.id)

    resp = await client.get("/public/live", headers=_auth(spectator_token))
    assert resp.status_code == 200
    data = resp.json()
    ids = [str(item["game_id"]) for item in data["items"]]
    assert bad_id not in ids
    assert good_id in ids


@pytest.mark.asyncio
async def test_live_index_empty_when_all_non_broadcastable(
    client: AsyncClient,
    spectator_token: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """All LIVE games non-broadcastable -> /public/live returns empty."""
    async with session_factory() as session, session.begin():
        await _make_game(
            session,
            broadcast_state=BroadcastState.LIVE.value,
            is_broadcastable=False,
        )

    resp = await client.get("/public/live", headers=_auth(spectator_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


# ---------------------------------------------------------------------------
# /public/recent index: non-broadcastable game absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_index_excludes_non_broadcastable(
    client: AsyncClient,
    spectator_token: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A RECENT game with is_broadcastable=False does not appear in /public/recent."""
    async with session_factory() as session, session.begin():
        bad_game = await _make_game(
            session,
            broadcast_state=BroadcastState.RECENT.value,
            is_broadcastable=False,
            status="COMPLETED",
            terminal_result={"winner": "mafia"},
        )
        good_game = await _make_game(
            session,
            broadcast_state=BroadcastState.RECENT.value,
            is_broadcastable=True,
            status="COMPLETED",
            terminal_result={"winner": "town"},
        )
        bad_id = str(bad_game.id)
        good_id = str(good_game.id)

    resp = await client.get("/public/recent", headers=_auth(spectator_token))
    assert resp.status_code == 200
    data = resp.json()
    ids = [str(item["game_id"]) for item in data["items"]]
    assert bad_id not in ids
    assert good_id in ids


@pytest.mark.asyncio
async def test_recent_index_empty_when_all_non_broadcastable(
    client: AsyncClient,
    spectator_token: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """All RECENT games non-broadcastable -> /public/recent returns empty."""
    async with session_factory() as session, session.begin():
        await _make_game(
            session,
            broadcast_state=BroadcastState.RECENT.value,
            is_broadcastable=False,
            status="COMPLETED",
            terminal_result={"winner": "mafia"},
        )

    resp = await client.get("/public/recent", headers=_auth(spectator_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []


# ---------------------------------------------------------------------------
# mark_live / mark_recent: refuse non-broadcastable games
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_live_refuses_non_broadcastable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """mark_live returns None and leaves state unchanged for non-broadcastable game."""
    async with session_factory() as session, session.begin():
        game = await _make_game(
            session,
            broadcast_state=BroadcastState.HIDDEN.value,
            is_broadcastable=False,
        )
        game_id = game.id

    async with session_factory() as session, session.begin():
        result = await mark_live(session, game_id)
    assert result is None

    # State must remain HIDDEN
    async with session_factory() as session:
        refreshed = await session.get(Game, game_id)
        assert refreshed is not None
        assert refreshed.broadcast_state == BroadcastState.HIDDEN.value


@pytest.mark.asyncio
async def test_mark_recent_refuses_non_broadcastable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """mark_recent returns None and leaves state unchanged for non-broadcastable game."""
    async with session_factory() as session, session.begin():
        game = await _make_game(
            session,
            broadcast_state=BroadcastState.LIVE.value,
            is_broadcastable=False,
        )
        game_id = game.id

    async with session_factory() as session, session.begin():
        result = await mark_recent(session, game_id)
    assert result is None

    # State must remain LIVE (unchanged)
    async with session_factory() as session:
        refreshed = await session.get(Game, game_id)
        assert refreshed is not None
        assert refreshed.broadcast_state == BroadcastState.LIVE.value


@pytest.mark.asyncio
async def test_mark_live_allows_broadcastable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """mark_live succeeds for a broadcastable game."""
    async with session_factory() as session, session.begin():
        game = await _make_game(
            session,
            broadcast_state=BroadcastState.HIDDEN.value,
            is_broadcastable=True,
        )
        game_id = game.id

    async with session_factory() as session, session.begin():
        result = await mark_live(session, game_id)
    assert result is not None
    assert result.broadcast_state == BroadcastState.LIVE.value


@pytest.mark.asyncio
async def test_mark_recent_allows_broadcastable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """mark_recent succeeds for a broadcastable game."""
    async with session_factory() as session, session.begin():
        game = await _make_game(
            session,
            broadcast_state=BroadcastState.LIVE.value,
            is_broadcastable=True,
        )
        game_id = game.id

    async with session_factory() as session, session.begin():
        result = await mark_recent(session, game_id)
    assert result is not None
    assert result.broadcast_state == BroadcastState.RECENT.value


__all__ = [
    "test_live_index_empty_when_all_non_broadcastable",
    "test_live_index_excludes_non_broadcastable",
    "test_mark_live_allows_broadcastable",
    "test_mark_live_refuses_non_broadcastable",
    "test_mark_recent_allows_broadcastable",
    "test_mark_recent_refuses_non_broadcastable",
    "test_recent_index_empty_when_all_non_broadcastable",
    "test_recent_index_excludes_non_broadcastable",
    "test_sse_broadcastable_live_game_streams",
    "test_sse_non_broadcastable_returns_404",
    "test_sse_recent_non_broadcastable_returns_404",
]
