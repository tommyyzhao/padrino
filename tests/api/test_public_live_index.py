"""Tests for US-090: Live-now and recent public index endpoints.

Drives ``GET /public/live`` and ``GET /public/recent`` and asserts that:
* LIVE entries omit outcome fields (spoiler-safe schema).
* RECENT entries include terminal_result.
* Pagination (limit + cursor) works for /public/recent.
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
from padrino.db.models import Game
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.public.broadcast_index import BroadcastState
from padrino.settings import get_settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_game(
    session: AsyncSession,
    *,
    broadcast_state: str = BroadcastState.LIVE.value,
    terminal_result: dict[str, Any] | None = None,
    current_phase: str | None = None,
    status: str = "RUNNING",
) -> Game:
    g = Game(
        ruleset_id="mini7_v1",
        game_seed=f"seed-{uuid.uuid4()}",
        status=status,
        terminal_result=terminal_result,
        broadcast_state=broadcast_state,
        current_phase=current_phase,
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
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------------------
# /public/live — LIVE game index
# ---------------------------------------------------------------------------


async def test_live_index_returns_live_games(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game(
            session, broadcast_state=BroadcastState.LIVE.value, current_phase="DAY_1"
        )

    r = await client.get("/public/live", headers=_auth(raw))
    assert r.status_code == 200
    game_ids = [item["game_id"] for item in r.json()["items"]]
    assert str(game.id) in game_ids


async def test_live_index_omits_outcome(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """LIVE entries must not expose terminal_result even if stored in the DB."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game(
            session,
            broadcast_state=BroadcastState.LIVE.value,
            terminal_result={"winner": "TOWN"},
        )

    r = await client.get("/public/live", headers=_auth(raw))
    assert r.status_code == 200
    item = next(i for i in r.json()["items"] if i["game_id"] == str(game.id))
    assert "terminal_result" not in item
    assert "winner" not in item


async def test_live_index_excludes_hidden_games(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        hidden = await _make_game(session, broadcast_state=BroadcastState.HIDDEN.value)

    r = await client.get("/public/live", headers=_auth(raw))
    assert r.status_code == 200
    game_ids = [item["game_id"] for item in r.json()["items"]]
    assert str(hidden.id) not in game_ids


async def test_live_index_excludes_recent_games(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        recent = await _make_game(session, broadcast_state=BroadcastState.RECENT.value)

    r = await client.get("/public/live", headers=_auth(raw))
    assert r.status_code == 200
    game_ids = [item["game_id"] for item in r.json()["items"]]
    assert str(recent.id) not in game_ids


async def test_live_index_response_has_players_alive(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game(session, broadcast_state=BroadcastState.LIVE.value)

    r = await client.get("/public/live", headers=_auth(raw))
    assert r.status_code == 200
    item = next(i for i in r.json()["items"] if i["game_id"] == str(game.id))
    assert "players_alive" in item
    assert isinstance(item["players_alive"], int)
    assert item["players_alive"] == 0  # no seats seeded


async def test_live_index_total_matches_item_count(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        for _ in range(3):
            await _make_game(session, broadcast_state=BroadcastState.LIVE.value)

    r = await client.get("/public/live", headers=_auth(raw))
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == len(data["items"])


# ---------------------------------------------------------------------------
# /public/recent — RECENT game index
# ---------------------------------------------------------------------------


async def test_recent_index_returns_recent_games_with_outcome(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game(
            session,
            broadcast_state=BroadcastState.RECENT.value,
            terminal_result={"winner": "TOWN"},
            status="DONE",
        )

    r = await client.get("/public/recent", headers=_auth(raw))
    assert r.status_code == 200
    item = next(i for i in r.json()["items"] if i["game_id"] == str(game.id))
    assert item["terminal_result"] is not None
    assert item["terminal_result"]["winner"] == "TOWN"


async def test_recent_index_excludes_live_games(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        live = await _make_game(session, broadcast_state=BroadcastState.LIVE.value)

    r = await client.get("/public/recent", headers=_auth(raw))
    assert r.status_code == 200
    game_ids = [item["game_id"] for item in r.json()["items"]]
    assert str(live.id) not in game_ids


async def test_recent_index_excludes_hidden_games(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        hidden = await _make_game(session, broadcast_state=BroadcastState.HIDDEN.value)

    r = await client.get("/public/recent", headers=_auth(raw))
    assert r.status_code == 200
    game_ids = [item["game_id"] for item in r.json()["items"]]
    assert str(hidden.id) not in game_ids


async def test_recent_index_pagination_limit(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        for _ in range(5):
            await _make_game(session, broadcast_state=BroadcastState.RECENT.value, status="DONE")

    r = await client.get("/public/recent?limit=2", headers=_auth(raw))
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 2
    assert data["total_estimate"] >= 5
    assert data["next_cursor"] is not None


async def test_recent_index_cursor_no_overlap(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        for _ in range(4):
            await _make_game(session, broadcast_state=BroadcastState.RECENT.value, status="DONE")

    r1 = await client.get("/public/recent?limit=2", headers=_auth(raw))
    d1 = r1.json()
    assert r1.status_code == 200
    assert len(d1["items"]) == 2
    assert d1["next_cursor"] is not None

    r2 = await client.get(f"/public/recent?limit=2&cursor={d1['next_cursor']}", headers=_auth(raw))
    d2 = r2.json()
    assert r2.status_code == 200
    assert len(d2["items"]) == 2
    ids1 = {i["game_id"] for i in d1["items"]}
    ids2 = {i["game_id"] for i in d2["items"]}
    assert ids1.isdisjoint(ids2), "paginated pages must not overlap"


async def test_recent_index_last_page_has_no_cursor(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        for _ in range(2):
            await _make_game(session, broadcast_state=BroadcastState.RECENT.value, status="DONE")

    r = await client.get("/public/recent?limit=10", headers=_auth(raw))
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 2
    assert data["next_cursor"] is None
