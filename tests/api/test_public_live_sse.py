"""Tests for US-089: SSE live endpoint, resumable by sequence cursor.

Drives the ``GET /public/games/{game_id}/live`` endpoint with a zero-delay
cadence override so the suite stays fast. Asserts frame order, cursor resume
correctness, forbidden-key exclusion, and correct 404 behaviour for hidden /
missing games.
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
from padrino.api.routes.public import _live_cadence
from padrino.db.models import Game, GameEvent
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.public.broadcast_index import BroadcastState
from padrino.public.broadcaster import CadenceConfig
from padrino.public.projection import PUBLIC_EVENT_FORBIDDEN_KEYS
from padrino.settings import get_settings

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _zero_cadence() -> CadenceConfig:
    """Zero-delay cadence so the SSE stream is instant in tests."""
    return CadenceConfig(chat_ms=0, phase_ms=0, elimination_ms=0, resolution_ms=0, default_ms=0)


async def _make_game(
    session: AsyncSession,
    *,
    broadcast_state: str = BroadcastState.LIVE.value,
    status: str = "RUNNING",
    terminal_result: dict[str, Any] | None = None,
) -> Game:
    g = Game(
        ruleset_id="mini7_v1",
        game_seed="test-seed-089",
        status=status,
        terminal_result=terminal_result,
        broadcast_state=broadcast_state,
    )
    session.add(g)
    await session.flush()
    return g


def _make_event(
    game_id: uuid.UUID,
    *,
    sequence: int,
    event_type: str = "PhaseStarted",
    phase: str = "DAY_1_DISCUSSION_ROUND_1",
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


async def _seed_key(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scopes: list[str],
    label: str,
) -> tuple[str, uuid.UUID]:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        obj = await api_keys_repo.create(session, raw_key=raw, scopes=scopes, label=label)
        return raw, obj.id


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def _parse_sse(body: str) -> list[dict[str, Any]]:
    """Parse an SSE stream body into a list of dicts with ``id`` and ``data``."""
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


# ---------------------------------------------------------------------------
# Happy path: LIVE game streams events
# ---------------------------------------------------------------------------


async def test_live_streams_public_events_in_order(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        session.add(_make_event(game.id, sequence=1, event_type="PhaseStarted"))
        session.add(
            _make_event(
                game.id, sequence=2, event_type="PublicMessageSubmitted", actor_player_id="P01"
            )
        )
        session.add(_make_event(game.id, sequence=3, event_type="PhaseResolved"))

    response = await client.get(
        f"/public/games/{game.id}/live",
        headers=_auth(raw),
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]

    frames = _parse_sse(response.text)
    assert len(frames) == 3
    seqs = [f["id"] for f in frames]
    assert seqs == [1, 2, 3], "frames must arrive in sequence order"
    for f in frames:
        assert f["data"]["schema_version"] == "public_event_v1"


async def test_sse_id_matches_sequence_field(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        for seq in (5, 10, 15):
            session.add(_make_event(game.id, sequence=seq))

    response = await client.get(f"/public/games/{game.id}/live", headers=_auth(raw))
    assert response.status_code == 200
    frames = _parse_sse(response.text)
    for f in frames:
        assert f["id"] == f["data"]["sequence"], "SSE id: must equal the event sequence"


# ---------------------------------------------------------------------------
# Resume: ?after= cursor
# ---------------------------------------------------------------------------


async def test_after_cursor_skips_earlier_frames(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        for seq in (1, 2, 3, 4):
            session.add(_make_event(game.id, sequence=seq))

    response = await client.get(
        f"/public/games/{game.id}/live",
        params={"after": 2},
        headers=_auth(raw),
    )
    assert response.status_code == 200
    frames = _parse_sse(response.text)
    seqs = [f["id"] for f in frames]
    assert seqs == [3, 4], "cursor=2 must skip sequences 1 and 2"


async def test_after_zero_returns_all_frames(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        for seq in (1, 2, 3):
            session.add(_make_event(game.id, sequence=seq))

    response = await client.get(
        f"/public/games/{game.id}/live",
        params={"after": 0},
        headers=_auth(raw),
    )
    assert response.status_code == 200
    frames = _parse_sse(response.text)
    assert [f["id"] for f in frames] == [1, 2, 3]


async def test_after_beyond_last_sequence_returns_empty_stream(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        session.add(_make_event(game.id, sequence=1))

    response = await client.get(
        f"/public/games/{game.id}/live",
        params={"after": 99},
        headers=_auth(raw),
    )
    assert response.status_code == 200
    assert _parse_sse(response.text) == []


# ---------------------------------------------------------------------------
# Resume: Last-Event-ID header
# ---------------------------------------------------------------------------


async def test_last_event_id_header_overrides_after_param(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        for seq in (1, 2, 3, 4, 5):
            session.add(_make_event(game.id, sequence=seq))

    # after=1 would give sequences 2-5; Last-Event-ID=3 should give 4-5
    response = await client.get(
        f"/public/games/{game.id}/live",
        params={"after": 1},
        headers={**_auth(raw), "Last-Event-ID": "3"},
    )
    assert response.status_code == 200
    frames = _parse_sse(response.text)
    assert [f["id"] for f in frames] == [4, 5], "Last-Event-ID header must override ?after="


async def test_last_event_id_header_used_when_no_after_param(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        for seq in (1, 2, 3):
            session.add(_make_event(game.id, sequence=seq))

    response = await client.get(
        f"/public/games/{game.id}/live",
        headers={**_auth(raw), "Last-Event-ID": "2"},
    )
    assert response.status_code == 200
    frames = _parse_sse(response.text)
    assert [f["id"] for f in frames] == [3]


# ---------------------------------------------------------------------------
# Broadcast state: only LIVE and RECENT are served
# ---------------------------------------------------------------------------


async def test_hidden_game_returns_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        game = await _make_game(session, broadcast_state=BroadcastState.HIDDEN.value)
        session.add(_make_event(game.id, sequence=1))

    response = await client.get(f"/public/games/{game.id}/live", headers=_auth(raw))
    assert response.status_code == 404


async def test_unknown_game_returns_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    response = await client.get(f"/public/games/{uuid.uuid4()}/live", headers=_auth(raw))
    assert response.status_code == 404


async def test_recent_game_streams_events(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        game = await _make_game(
            session,
            broadcast_state=BroadcastState.RECENT.value,
            status="COMPLETED",
            terminal_result={"winner": "TOWN", "cause": "VOTE"},
        )
        session.add(_make_event(game.id, sequence=1))
        session.add(_make_event(game.id, sequence=2))

    response = await client.get(f"/public/games/{game.id}/live", headers=_auth(raw))
    assert response.status_code == 200
    frames = _parse_sse(response.text)
    assert len(frames) == 2


# ---------------------------------------------------------------------------
# Privacy: forbidden keys must not appear in streamed frames
# ---------------------------------------------------------------------------


async def test_no_forbidden_keys_in_streamed_frames(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        session.add(
            _make_event(
                game.id,
                sequence=1,
                event_type="PublicMessageSubmitted",
                actor_player_id="P03",
                payload={"text": "hello", "role": "VILLAGER", "faction": "TOWN"},
            )
        )

    response = await client.get(f"/public/games/{game.id}/live", headers=_auth(raw))
    assert response.status_code == 200
    frames = _parse_sse(response.text)
    # PublicMessageSubmitted is PUBLIC so the frame appears; forbidden keys stripped
    assert len(frames) == 1
    payload = frames[0]["data"]["payload"]
    for key in PUBLIC_EVENT_FORBIDDEN_KEYS:
        assert key not in payload, f"forbidden key '{key}' leaked into SSE frame payload"


# ---------------------------------------------------------------------------
# Privacy: PRIVATE events must not appear in stream
# ---------------------------------------------------------------------------


async def test_private_events_excluded_from_stream(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        # PRIVATE RolesAssigned must be dropped
        session.add(
            _make_event(
                game.id,
                sequence=1,
                event_type="RolesAssigned",
                visibility="PRIVATE",
                payload={"assignments": [{"public_player_id": "P01", "role": "MAFIOSO"}]},
            )
        )
        # PUBLIC PhaseStarted must survive
        session.add(_make_event(game.id, sequence=2, event_type="PhaseStarted"))

    response = await client.get(f"/public/games/{game.id}/live", headers=_auth(raw))
    assert response.status_code == 200
    frames = _parse_sse(response.text)
    assert len(frames) == 1, "only PUBLIC events survive the stream"
    assert frames[0]["data"]["event_type"] == "PhaseStarted"
    assert frames[0]["id"] == 2


# ---------------------------------------------------------------------------
# Schema version present on every frame
# ---------------------------------------------------------------------------


async def test_schema_version_on_every_frame(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        for seq in (1, 2):
            session.add(_make_event(game.id, sequence=seq))

    response = await client.get(f"/public/games/{game.id}/live", headers=_auth(raw))
    assert response.status_code == 200
    for frame in _parse_sse(response.text):
        assert frame["data"]["schema_version"] == "public_event_v1"
