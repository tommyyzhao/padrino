"""Live-tail SSE generator for in-progress games (US-133).

The original broadcast transport (US-089) read a game's committed event log
once, paced it, and ended — a post-hoc replay that only works on a *finished*
game. US-133 adds a true live tail: stream the committed prefix, then keep
polling the still-growing log for newly committed PUBLIC events, emitting them
as they appear, until the game terminates.

Design constraints (from the story):

* **No long-held DB session.** Each poll opens its own short read transaction
  via the injected ``async_sessionmaker`` and closes it immediately, so a
  hours-long human game never pins a connection for its whole lifetime.
* **Identity-blind.** Frames go through the existing ``public_event_v1``
  projection (:func:`padrino.public.projection.to_public_event_v1`), reusing
  ``FORBIDDEN_PAYLOAD_KEYS``; PRIVATE/SYSTEM events are dropped.
* **A LIVE game never exposes ``terminal_result``.** Only projected PUBLIC
  events are emitted; the game's ``terminal_result`` column is never read into
  a frame. The natural close is the committed ``GameTerminated`` PUBLIC event.
* **Resume by sequence.** Callers pass ``after`` (from ``?after=`` or
  ``Last-Event-ID``); the tail only emits sequences strictly greater, so a
  reconnect yields no gaps and no duplicates.
* **Keep-alive heartbeats.** While idle, an SSE comment line is emitted so
  proxies don't drop the connection.

All wall-clock pacing lives here in the impure shell — the core stays pure.
Timing is injected via :class:`LiveTailConfig` so tests run near-instantly and
deterministically.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import Game, GameEvent
from padrino.public.broadcast_index import BroadcastState
from padrino.public.projection import to_public_event_v1

#: Game lifecycle status that means the engine has finished writing events.
_STATUS_COMPLETED = "COMPLETED"

#: SSE keep-alive comment. A line beginning with ``:`` is an SSE comment that
#: clients ignore but proxies/load-balancers see as traffic.
_HEARTBEAT = ": keep-alive\n\n"


@dataclass(frozen=True)
class LiveTailConfig:
    """Wall-clock pacing for the live tail (milliseconds).

    ``poll_ms``       interval between append polls of the growing log.
    ``heartbeat_ms``  maximum idle gap before a keep-alive comment is emitted.
    ``idle_timeout_ms`` how long to wait with no new events on a still-LIVE
                      game before closing the stream (0 disables the timeout).
    """

    poll_ms: int = 1000
    heartbeat_ms: int = 15000
    idle_timeout_ms: int = 300000


def default_live_tail_config() -> LiveTailConfig:
    """Build a :class:`LiveTailConfig` from application settings."""
    from padrino.settings import get_settings

    s = get_settings()
    return LiveTailConfig(
        poll_ms=s.padrino_sse_live_tail_poll_ms,
        heartbeat_ms=s.padrino_sse_live_tail_heartbeat_ms,
        idle_timeout_ms=s.padrino_sse_live_tail_idle_timeout_ms,
    )


@dataclass(frozen=True)
class _TailWindow:
    """Result of one short read transaction over the growing log."""

    events: list[dict[str, Any]]
    is_terminal: bool
    exists: bool


async def _read_window(
    session: AsyncSession,
    game_id: uuid.UUID,
    *,
    after: int,
) -> _TailWindow:
    """Read newly committed events (sequence > ``after``) in one short txn.

    Returns the projected-input event dicts (raw columns; projection happens in
    the generator), whether the game has reached a terminal/non-LIVE state, and
    whether the game still exists and is broadcastable.
    """
    game = await session.get(Game, game_id)
    if game is None or not game.is_broadcastable:
        return _TailWindow(events=[], is_terminal=True, exists=False)

    stmt = (
        select(GameEvent)
        .where(GameEvent.game_id == game_id, GameEvent.sequence > after)
        .order_by(GameEvent.sequence)
    )
    rows = list((await session.execute(stmt)).scalars())
    events = [
        {
            "sequence": e.sequence,
            "event_type": e.event_type,
            "phase": e.phase,
            "visibility": e.visibility,
            "actor_player_id": e.actor_player_id,
            "payload": dict(e.payload) if e.payload else {},
            "prev_event_hash": e.prev_event_hash,
            "event_hash": e.event_hash,
        }
        for e in rows
    ]
    is_terminal = (
        game.status == _STATUS_COMPLETED or game.broadcast_state == BroadcastState.RECENT.value
    )
    return _TailWindow(events=events, is_terminal=is_terminal, exists=True)


def _frame(event: dict[str, Any]) -> str:
    """Render one projected public_event_v1 dict as an SSE ``id:``/``data:`` block."""
    seq = event["sequence"]
    data = json.dumps(event, separators=(",", ":"))
    return f"id: {seq}\ndata: {data}\n\n"


async def stream_live_tail(
    session_factory: async_sessionmaker[AsyncSession],
    game_id: uuid.UUID,
    *,
    after: int | None = None,
    config: LiveTailConfig | None = None,
    on_poll: Callable[[], Awaitable[None]] | None = None,
) -> AsyncGenerator[str, None]:
    """Yield SSE blocks for a live-tailed in-progress game.

    Emits the committed prefix (sequences > ``after``) projected through the
    identity-blind ``public_event_v1`` contract, then polls the growing log for
    newly committed PUBLIC events, emitting keep-alive heartbeats while idle.
    The stream closes when:

    * the game's terminal ``GameTerminated`` PUBLIC frame has been emitted, or
    * the game reaches a terminal/non-LIVE state with no further events, or
    * the game vanishes / becomes non-broadcastable, or
    * ``idle_timeout_ms`` elapses with no new events on a still-LIVE game.

    Each iteration opens its own short read transaction; no DB session is held
    across the inter-poll sleep.

    ``on_poll`` is an optional async hook awaited immediately before each append
    poll. It is the deterministic-test seam: a test can grow the log inside the
    hook so a single-connection (in-memory SQLite) backend never races the tail.
    In production it is unset and the loop simply polls.
    """
    cfg = config or default_live_tail_config()
    cursor = after if after is not None else 0

    poll_s = cfg.poll_ms / 1000.0
    heartbeat_s = cfg.heartbeat_ms / 1000.0
    idle_timeout_s = (cfg.idle_timeout_ms / 1000.0) if cfg.idle_timeout_ms > 0 else None

    loop = asyncio.get_event_loop()
    last_emit = loop.time()
    last_heartbeat = loop.time()

    while True:
        if on_poll is not None:
            await on_poll()

        async with session_factory() as session:
            window = await _read_window(session, game_id, after=cursor)

        if not window.exists:
            return

        emitted_any = False
        for raw in window.events:
            projected = to_public_event_v1(raw)
            # Advance the cursor past every committed event — including dropped
            # PRIVATE/SYSTEM ones — so a resume never re-reads them.
            cursor = max(cursor, int(raw["sequence"]))
            if projected is None:
                continue
            yield _frame(projected)
            emitted_any = True
            if projected.get("event_type") == "GameTerminated":
                # The live game has ended: the terminal frame closes the stream.
                return

        if emitted_any:
            now = loop.time()
            last_emit = now
            last_heartbeat = now

        # The game has finished and we've drained everything we will ever see.
        if window.is_terminal:
            return

        # Idle: optionally emit a heartbeat, honour the idle timeout, then poll.
        now = loop.time()
        if idle_timeout_s is not None and (now - last_emit) >= idle_timeout_s:
            return
        if heartbeat_s > 0 and (now - last_heartbeat) >= heartbeat_s:
            yield _HEARTBEAT
            last_heartbeat = loop.time()

        if poll_s > 0:
            await asyncio.sleep(poll_s)


__all__ = [
    "LiveTailConfig",
    "default_live_tail_config",
    "stream_live_tail",
]
