"""Tests for US-133: live-tail SSE for in-progress games.

The pre-US-133 ``/public/games/{id}/live`` endpoint reads the committed log
once and ends — it is a post-hoc paced replay. US-133 adds a ``?tail=true``
live-tail mode that streams the committed prefix then continues emitting newly
committed PUBLIC events as they are produced on a still-growing log, with
keep-alive heartbeats and ``?after=`` / ``Last-Event-ID`` resume, closing the
stream on the terminal frame.

These tests drive the live-tail loop directly (``stream_live_tail``) with an
injected, near-zero-delay config and a session factory backed by the test DB,
so the suite stays fast and deterministic without real wall-clock waits.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import Game, GameEvent
from padrino.public.broadcast_index import BroadcastState
from padrino.public.live_tail import LiveTailConfig, stream_live_tail
from padrino.public.projection import PUBLIC_EVENT_FORBIDDEN_KEYS

pytestmark = pytest.mark.asyncio


def _fast_config() -> LiveTailConfig:
    """A near-instant live-tail config so the loop spins quickly in tests."""
    return LiveTailConfig(
        poll_ms=1,
        heartbeat_ms=1_000_000,  # effectively never during a fast test
        idle_timeout_ms=5_000,
    )


async def _make_game(
    session: AsyncSession,
    *,
    broadcast_state: str = BroadcastState.LIVE.value,
    status: str = "RUNNING",
) -> Game:
    g = Game(
        ruleset_id="mini7_v1",
        game_seed="test-seed-133",
        status=status,
        terminal_result=None,
        broadcast_state=broadcast_state,
        is_broadcastable=True,
    )
    session.add(g)
    await session.flush()
    return g


def _event(
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


async def _append(
    session_factory: async_sessionmaker[AsyncSession],
    game_id: uuid.UUID,
    *events: GameEvent,
) -> None:
    async with session_factory() as session, session.begin():
        for e in events:
            session.add(e)


async def _terminate(
    session_factory: async_sessionmaker[AsyncSession],
    game_id: uuid.UUID,
    *,
    last_sequence: int,
) -> None:
    """Append the terminal GameTerminated event and flip the game RECENT."""
    async with session_factory() as session, session.begin():
        session.add(
            _event(
                game_id,
                sequence=last_sequence,
                event_type="GameTerminated",
                phase="GAME_OVER",
                payload={"winner": "TOWN", "reason": "VOTE"},
            )
        )
        game = await session.get(Game, game_id)
        assert game is not None
        game.status = "COMPLETED"
        game.broadcast_state = BroadcastState.RECENT.value
        game.terminal_result = {"winner": "TOWN", "reason": "VOTE"}


def _parse_sse(chunks: list[str]) -> list[dict[str, Any]]:
    """Parse SSE blocks (data frames only) from a list of emitted strings."""
    body = "".join(chunks)
    frames: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
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


def _count_heartbeats(chunks: list[str]) -> int:
    return "".join(chunks).count(": keep-alive")


@pytest_asyncio.fixture
async def live_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        session.add(_event(game.id, sequence=1, event_type="PhaseStarted"))
        session.add(
            _event(game.id, sequence=2, event_type="PublicMessageSubmitted", actor_player_id="P01")
        )
        return game.id


async def _collect(
    session_factory: async_sessionmaker[AsyncSession],
    game_id: uuid.UUID,
    *,
    after: int | None = None,
    config: LiveTailConfig | None = None,
    steps: list[Callable[[], Awaitable[None]]] | None = None,
) -> list[str]:
    """Drive the tail, optionally growing the log deterministically per poll.

    ``steps`` is a queue of async callbacks; one is run before each append poll
    (via the ``on_poll`` seam), so the log can grow between polls WITHOUT racing
    the single-connection in-memory SQLite backend.
    """
    queue = list(steps or [])

    async def _on_poll() -> None:
        if queue:
            await queue.pop(0)()

    chunks: list[str] = []
    async for chunk in stream_live_tail(
        session_factory,
        game_id,
        after=after,
        config=config or _fast_config(),
        on_poll=_on_poll if steps is not None else None,
    ):
        chunks.append(chunk)
    return chunks


async def test_tail_streams_prefix_then_newly_committed_events(
    session_factory: async_sessionmaker[AsyncSession],
    live_game: uuid.UUID,
) -> None:
    """The committed prefix streams first, then events appended mid-stream."""

    async def _append_three() -> None:
        await _append(
            session_factory,
            live_game,
            _event(live_game, sequence=3, event_type="PhaseResolved"),
        )

    async def _close() -> None:
        await _terminate(session_factory, live_game, last_sequence=4)

    chunks = await asyncio.wait_for(
        _collect(session_factory, live_game, steps=[_append_three, _close]),
        timeout=10.0,
    )

    frames = _parse_sse(chunks)
    seqs = [f["id"] for f in frames]
    # Prefix (1, 2), the mid-stream append (3) and the terminal frame (4).
    assert seqs == [1, 2, 3, 4]
    assert frames[-1]["data"]["event_type"] == "GameTerminated"


async def test_resume_by_sequence_no_gaps_or_dupes(
    session_factory: async_sessionmaker[AsyncSession],
    live_game: uuid.UUID,
) -> None:
    """A reconnect with ?after= yields no gaps and no duplicates."""

    async def _close() -> None:
        await _terminate(session_factory, live_game, last_sequence=3)

    # First connection: read the whole stream to completion.
    first = await asyncio.wait_for(
        _collect(session_factory, live_game, steps=[_close]),
        timeout=10.0,
    )
    first_seqs = [f["id"] for f in _parse_sse(first)]
    assert first_seqs == [1, 2, 3]

    # Reconnect after sequence 2: only 3 should be (re)delivered — no dupes of
    # 1/2, no gap.
    resumed = await asyncio.wait_for(
        _collect(session_factory, live_game, after=2),
        timeout=10.0,
    )
    resumed_seqs = [f["id"] for f in _parse_sse(resumed)]
    assert resumed_seqs == [3]


async def test_terminal_frame_closes_the_stream(
    session_factory: async_sessionmaker[AsyncSession],
    live_game: uuid.UUID,
) -> None:
    """Once the game terminates, the stream ends (the generator returns)."""
    await _terminate(session_factory, live_game, last_sequence=3)

    # Game is already RECENT with a committed GameTerminated frame: the tail
    # drains the committed log including the terminal frame and then closes.
    chunks = await asyncio.wait_for(_collect(session_factory, live_game), timeout=10.0)
    frames = _parse_sse(chunks)
    assert [f["id"] for f in frames] == [1, 2, 3]
    assert frames[-1]["data"]["event_type"] == "GameTerminated"


async def test_heartbeats_emitted_while_idle(
    session_factory: async_sessionmaker[AsyncSession],
    live_game: uuid.UUID,
) -> None:
    """A still-growing LIVE log with no new events emits keep-alive heartbeats."""
    config = LiveTailConfig(poll_ms=1, heartbeat_ms=1, idle_timeout_ms=1_000_000)

    polls = 0

    async def _idle_then_close() -> None:
        nonlocal polls
        polls += 1
        # Stay idle for the first few polls (heartbeats accrue), then terminate.
        if polls >= 3:
            await _terminate(session_factory, live_game, last_sequence=3)

    chunks = await asyncio.wait_for(
        _collect(
            session_factory,
            live_game,
            config=config,
            steps=[_idle_then_close, _idle_then_close, _idle_then_close],
        ),
        timeout=10.0,
    )
    assert _count_heartbeats(chunks) >= 1
    # The data frames are still correct around the heartbeats.
    assert [f["id"] for f in _parse_sse(chunks)] == [1, 2, 3]


async def test_idle_timeout_closes_a_still_live_stream(
    session_factory: async_sessionmaker[AsyncSession],
    live_game: uuid.UUID,
) -> None:
    """A LIVE game that produces nothing new closes after the idle timeout."""
    config = LiveTailConfig(poll_ms=1, heartbeat_ms=1_000_000, idle_timeout_ms=50)
    chunks = await asyncio.wait_for(
        _collect(session_factory, live_game, config=config),
        timeout=10.0,
    )
    # Only the committed prefix; the stream closes without a terminal frame.
    frames = _parse_sse(chunks)
    assert [f["id"] for f in frames] == [1, 2]


async def test_no_forbidden_keys_and_no_private_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Tail reuses the identity-blind projection: PRIVATE dropped, keys stripped."""
    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        session.add(
            _event(
                game.id,
                sequence=1,
                event_type="RolesAssigned",
                visibility="PRIVATE",
                payload={"assignments": [{"public_player_id": "P01", "role": "MAFIOSO"}]},
            )
        )
        session.add(
            _event(
                game.id,
                sequence=2,
                event_type="PublicMessageSubmitted",
                actor_player_id="P03",
                payload={"text": "hi", "role": "VILLAGER", "faction": "TOWN"},
            )
        )
        game_id = game.id

    config = LiveTailConfig(poll_ms=1, heartbeat_ms=1_000_000, idle_timeout_ms=50)
    chunks = await asyncio.wait_for(
        _collect(session_factory, game_id, config=config),
        timeout=10.0,
    )
    frames = _parse_sse(chunks)
    # PRIVATE RolesAssigned dropped; only the PUBLIC chat survives.
    assert [f["id"] for f in frames] == [2]
    payload = frames[0]["data"]["payload"]
    for key in PUBLIC_EVENT_FORBIDDEN_KEYS:
        assert key not in payload


async def test_live_game_never_exposes_terminal_result(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """While LIVE the tail never emits the game's terminal_result column."""
    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        # Defensive: even if a terminal_result somehow sat on a LIVE row.
        game.terminal_result = {"winner": "TOWN"}
        session.add(_event(game.id, sequence=1, event_type="PhaseStarted"))
        game_id = game.id

    config = LiveTailConfig(poll_ms=1, heartbeat_ms=1_000_000, idle_timeout_ms=50)
    chunks = await asyncio.wait_for(
        _collect(session_factory, game_id, config=config),
        timeout=10.0,
    )
    body = "".join(chunks)
    assert "winner" not in body
    assert "terminal_result" not in body
