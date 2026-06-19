"""Tests for US-120: per-game analytics materialized at RECENT promotion.

Asserts:
  * ``mark_recent`` writes a ``MaterializedGameAnalytics`` row computed once.
  * ``GET /public/games/{id}/analytics`` serves the stored row for RECENT games.
  * A stored-missing RECENT game self-heals (on-the-fly compute + persist).
  * LIVE games never read or write the materialized row (spoiler-safe path).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import SCOPE_SPECTATOR, RateLimiter, generate_raw_key
from padrino.db.models import Game, GameEvent, MaterializedGameAnalytics
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.public.broadcast_index import BroadcastState, mark_recent
from padrino.settings import get_settings

_RULESET = "mini7_v1"

_ROLES_ASSIGNED_PAYLOAD = {
    "assignments": [
        {"public_player_id": "P1", "role": "Mafia", "faction": "MAFIA"},
        {"public_player_id": "P2", "role": "Mafia", "faction": "MAFIA"},
        {"public_player_id": "P3", "role": "Detective", "faction": "TOWN"},
        {"public_player_id": "P4", "role": "Doctor", "faction": "TOWN"},
        {"public_player_id": "P5", "role": "Villager", "faction": "TOWN"},
        {"public_player_id": "P6", "role": "Villager", "faction": "TOWN"},
        {"public_player_id": "P7", "role": "Villager", "faction": "TOWN"},
    ]
}


async def _seed_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        await api_keys_repo.create(session, raw_key=raw, scopes=[SCOPE_SPECTATOR], label="viewer")
    return raw


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def _make_event(
    game_id: uuid.UUID,
    *,
    sequence: int,
    event_type: str,
    phase: str,
    visibility: str = "PUBLIC",
    actor_player_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> GameEvent:
    return GameEvent(
        game_id=game_id,
        sequence=sequence,
        event_type=event_type,
        phase=phase,
        visibility=visibility,
        actor_player_id=actor_player_id,
        payload=payload or {},
        prev_event_hash="0" * 64,
        event_hash=f"{sequence:064x}",
    )


async def _make_game_with_events(
    session: AsyncSession,
    *,
    broadcast_state: str,
    winner: str = "TOWN",
) -> Game:
    g = Game(
        ruleset_id=_RULESET,
        game_seed="seed-120",
        status="COMPLETED",
        terminal_result={"winner": winner},
        broadcast_state=broadcast_state,
        is_broadcastable=True,
    )
    session.add(g)
    await session.flush()
    events = [
        _make_event(
            g.id,
            sequence=1,
            event_type="RolesAssigned",
            phase="SETUP",
            visibility="SYSTEM",
            payload=_ROLES_ASSIGNED_PAYLOAD,
        ),
        _make_event(
            g.id,
            sequence=2,
            event_type="VoteSubmitted",
            phase="DAY_1_VOTE",
            actor_player_id="P3",
            payload={"target": "P1", "is_abstain": False},
        ),
        _make_event(
            g.id,
            sequence=3,
            event_type="PlayerEliminated",
            phase="DAY_1_VOTE",
            payload={"public_player_id": "P1"},
        ),
        _make_event(
            g.id,
            sequence=4,
            event_type="GameTerminated",
            phase="GAME_OVER",
            payload={"winner": winner},
        ),
    ]
    for ev in events:
        session.add(ev)
    await session.flush()
    return g


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


async def _count_materialized(
    session_factory: async_sessionmaker[AsyncSession], game_id: uuid.UUID
) -> int:
    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(MaterializedGameAnalytics).where(
                        MaterializedGameAnalytics.game_id == game_id
                    )
                )
            ).scalars()
        )
        return len(rows)


# ---------------------------------------------------------------------------
# mark_recent materializes once
# ---------------------------------------------------------------------------


async def test_mark_recent_creates_materialized_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Promoting a game to RECENT persists exactly one analytics row."""
    async with session_factory() as session, session.begin():
        game = await _make_game_with_events(session, broadcast_state=BroadcastState.LIVE.value)
    game_id = game.id

    assert await _count_materialized(session_factory, game_id) == 0

    async with session_factory() as session, session.begin():
        result = await mark_recent(session, game_id)
        assert result is not None

    assert await _count_materialized(session_factory, game_id) == 1
    async with session_factory() as session:
        row = await session.get(MaterializedGameAnalytics, game_id)
        assert row is not None
        assert row.ruleset_id == _RULESET
        import json

        payload = json.loads(row.analytics_json)
        assert payload["winner"] == "TOWN"
        assert isinstance(payload["role_win_rates"], list)


# ---------------------------------------------------------------------------
# GET serves the stored row for RECENT
# ---------------------------------------------------------------------------


async def test_get_serves_stored_row_for_recent(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A RECENT game returns the materialized analytics with the outcome."""
    raw = await _seed_key(session_factory)
    async with session_factory() as session, session.begin():
        game = await _make_game_with_events(session, broadcast_state=BroadcastState.LIVE.value)
    game_id = game.id
    async with session_factory() as session, session.begin():
        await mark_recent(session, game_id)

    r = await client.get(f"/public/games/{game_id}/analytics", headers=_auth(raw))
    assert r.status_code == 200
    data = r.json()
    assert data["game_id"] == str(game_id)
    assert data["winner"] == "TOWN"
    assert data["role_win_rates"] is not None
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


async def test_get_recent_does_not_recompute_when_events_change(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Once materialized, the served analytics come from the stored row.

    Mutating the underlying event log after materialization (which never happens
    in production — the log is immutable post-terminal) must NOT change the
    served result, proving the stored row is the source of truth.
    """
    raw = await _seed_key(session_factory)
    async with session_factory() as session, session.begin():
        game = await _make_game_with_events(session, broadcast_state=BroadcastState.LIVE.value)
    game_id = game.id
    async with session_factory() as session, session.begin():
        await mark_recent(session, game_id)

    # Corrupt the stored row's winner so we can prove the GET reads the row.
    async with session_factory() as session, session.begin():
        row = await session.get(MaterializedGameAnalytics, game_id)
        assert row is not None
        import json

        payload = json.loads(row.analytics_json)
        payload["winner"] = "MAFIA"
        row.analytics_json = json.dumps(payload)

    r = await client.get(f"/public/games/{game_id}/analytics", headers=_auth(raw))
    assert r.status_code == 200
    assert r.json()["winner"] == "MAFIA", "GET must serve the stored row, not recompute"


# ---------------------------------------------------------------------------
# Backfill: stored-missing RECENT game self-heals
# ---------------------------------------------------------------------------


async def test_recent_without_stored_row_backfills_and_persists(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A RECENT game promoted before US-120 (no stored row) is computed + saved on GET."""
    raw = await _seed_key(session_factory)
    # Insert a RECENT game directly (simulating a pre-US-120 promotion) WITHOUT
    # going through mark_recent, so no materialized row exists.
    async with session_factory() as session, session.begin():
        game = await _make_game_with_events(session, broadcast_state=BroadcastState.RECENT.value)
    game_id = game.id

    assert await _count_materialized(session_factory, game_id) == 0

    r = await client.get(f"/public/games/{game_id}/analytics", headers=_auth(raw))
    assert r.status_code == 200
    assert r.json()["winner"] == "TOWN"

    # The fallback persisted the result (self-healing).
    assert await _count_materialized(session_factory, game_id) == 1


# ---------------------------------------------------------------------------
# LIVE games stay on the on-the-fly spoiler-safe path
# ---------------------------------------------------------------------------


async def test_live_game_not_materialized(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """LIVE games are spoiler-safe and never write a materialized row."""
    raw = await _seed_key(session_factory)
    async with session_factory() as session, session.begin():
        game = await _make_game_with_events(session, broadcast_state=BroadcastState.LIVE.value)
    game_id = game.id

    r = await client.get(f"/public/games/{game_id}/analytics", headers=_auth(raw))
    assert r.status_code == 200
    data = r.json()
    assert data["winner"] is None
    assert data["role_win_rates"] is None
    assert r.headers["cache-control"] == "no-store"
    # No materialized row written for a LIVE game.
    assert await _count_materialized(session_factory, game_id) == 0
