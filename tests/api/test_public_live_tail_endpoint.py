"""HTTP-level tests for the US-133 ``?tail=true`` live-tail SSE endpoint.

The default ``/public/games/{id}/live`` path paces a finished log once and
ends. ``?tail=true`` switches to the live tail: it streams the committed prefix
of an IN-PROGRESS (RUNNING) game and keeps polling for newly committed PUBLIC
events. These tests drive the real endpoint with an injected near-instant
:class:`LiveTailConfig` (small idle timeout) so the stream closes deterministically
after draining the committed prefix.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import SCOPE_SPECTATOR, RateLimiter, generate_raw_key
from padrino.api.routes.public import _live_tail_config
from padrino.db.models import Game, GameEvent
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.public.broadcast_index import BroadcastState, mark_live
from padrino.public.live_tail import LiveTailConfig
from padrino.settings import get_settings

pytestmark = pytest.mark.asyncio


def _fast_tail_config() -> LiveTailConfig:
    # Tiny idle timeout so a still-LIVE stream closes promptly once the
    # committed prefix has drained — keeps the HTTP test fast and deterministic.
    return LiveTailConfig(poll_ms=1, heartbeat_ms=1_000_000, idle_timeout_ms=20)


def _parse_sse(body: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in body.strip().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        frame: dict[str, Any] = {}
        for line in block.split("\n"):
            if line.startswith("id:"):
                frame["id"] = int(line[3:].strip())
            elif line.startswith("data:"):
                frame["data"] = json.loads(line[5:].strip())
        if "data" in frame:
            frames.append(frame)
    return frames


async def _seed_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        await api_keys_repo.create(session, raw_key=raw, scopes=[SCOPE_SPECTATOR], label="viewer")
    return raw


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def _event(game_id: uuid.UUID, *, sequence: int, event_type: str = "PhaseStarted") -> GameEvent:
    return GameEvent(
        game_id=game_id,
        sequence=sequence,
        event_type=event_type,
        phase="DAY_1_DISCUSSION_ROUND_1",
        visibility="PUBLIC",
        actor_player_id=None,
        payload={},
        prev_event_hash="0" * 64,
        event_hash=f"{sequence:064x}",
    )


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
    app.dependency_overrides[_live_tail_config] = _fast_tail_config
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def test_tail_streams_in_progress_game_after_midgame_mark_live(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """mark_live mid-game (RUNNING, not completed) then ?tail=true streams it."""
    raw = await _seed_key(session_factory)

    async with session_factory() as session, session.begin():
        # An in-progress game: still RUNNING, broadcast initially HIDDEN.
        game = Game(
            ruleset_id="mini7_v1",
            game_seed="seed-133-http",
            status="RUNNING",
            terminal_result=None,
            broadcast_state=BroadcastState.HIDDEN.value,
            is_broadcastable=True,
        )
        session.add(game)
        await session.flush()
        session.add(_event(game.id, sequence=1, event_type="PhaseStarted"))
        session.add(_event(game.id, sequence=2, event_type="PublicMessageSubmitted"))
        game_id = game.id

    # Mid-game mark_live: the game is still RUNNING, not post-completion.
    async with session_factory() as session, session.begin():
        result = await mark_live(session, game_id)
        assert result is not None
        assert result.broadcast_state == BroadcastState.LIVE.value
        assert result.status == "RUNNING"

    response = await client.get(
        f"/public/games/{game_id}/live",
        params={"tail": "true"},
        headers=_auth(raw),
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    frames = _parse_sse(response.text)
    assert [f["id"] for f in frames] == [1, 2]
    for f in frames:
        assert f["data"]["schema_version"] == "public_event_v1"


async def test_tail_resume_after_cursor_no_dupes(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """?tail=true with ?after= resumes by sequence without re-sending the prefix."""
    raw = await _seed_key(session_factory)

    async with session_factory() as session, session.begin():
        game = Game(
            ruleset_id="mini7_v1",
            game_seed="seed-133-resume",
            status="RUNNING",
            broadcast_state=BroadcastState.LIVE.value,
            is_broadcastable=True,
        )
        session.add(game)
        await session.flush()
        for seq in (1, 2, 3):
            session.add(_event(game.id, sequence=seq))
        game_id = game.id

    response = await client.get(
        f"/public/games/{game_id}/live",
        params={"tail": "true", "after": 1},
        headers=_auth(raw),
    )
    assert response.status_code == 200
    frames = _parse_sse(response.text)
    assert [f["id"] for f in frames] == [2, 3]


async def test_tail_hidden_game_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory)
    async with session_factory() as session, session.begin():
        game = Game(
            ruleset_id="mini7_v1",
            game_seed="seed-133-hidden",
            status="RUNNING",
            broadcast_state=BroadcastState.HIDDEN.value,
            is_broadcastable=True,
        )
        session.add(game)
        await session.flush()
        game_id = game.id

    response = await client.get(
        f"/public/games/{game_id}/live",
        params={"tail": "true"},
        headers=_auth(raw),
    )
    assert response.status_code == 404
