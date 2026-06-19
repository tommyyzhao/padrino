"""Member-scoped lobby state SSE channel (US-148).

Streams the lobby's roster / ready / presence as identity-blind ``lobby_state``
frames: an initial snapshot, then a fresh snapshot whenever it changes, with
keep-alive heartbeats while idle. The channel closes once the lobby leaves the
joinable lifecycle (``LAUNCHED`` / ``CLOSED``) or after ``idle_timeout_ms`` of
no change.

Each frame is counts-only and carries NO per-seat human/AI map, NO principal id,
NO seat_kind — only the canonical :func:`composition_summary` counts and the
identity-blind roster from :mod:`padrino.api.lobby_presence`. Timing is injected
via :class:`LobbyStreamConfig` so tests close the stream near-instantly.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.lobby_presence import roster_view
from padrino.core.composition import composition_summary
from padrino.core.enums import LobbyStatus
from padrino.db.repositories import lobbies as lobbies_repo

LOBBY_STATE_FRAME = "lobby_state"

#: Lifecycle statuses for which the lobby state channel keeps streaming.
_OPEN_STATUSES = frozenset({LobbyStatus.OPEN.value, LobbyStatus.LOCKED.value})


@dataclass(frozen=True)
class LobbyStreamConfig:
    """Injected timing for the lobby state SSE channel."""

    poll_ms: int = 1000
    heartbeat_ms: int = 15000
    idle_timeout_ms: int = 300000
    stale_seconds: float = 60.0


def default_lobby_stream_config() -> LobbyStreamConfig:
    """Build a :class:`LobbyStreamConfig` from application settings."""
    from padrino.settings import get_settings

    s = get_settings()
    return LobbyStreamConfig(
        poll_ms=s.padrino_lobby_stream_poll_ms,
        heartbeat_ms=s.padrino_lobby_stream_heartbeat_ms,
        idle_timeout_ms=s.padrino_lobby_stream_idle_timeout_ms,
        stale_seconds=s.padrino_lobby_presence_stale_seconds,
    )


async def _build_state_frame(
    session: AsyncSession, *, lobby_id: uuid.UUID, now: datetime, stale_seconds: float
) -> dict[str, object] | None:
    lobby = await lobbies_repo.get_lobby(session, lobby_id)
    if lobby is None:
        return None
    members = await lobbies_repo.list_members(session, lobby_id)
    seats = await lobbies_repo.list_seats(session, lobby_id)
    composition = composition_summary({"seat_kind": seat.seat_kind} for seat in seats)
    roster = roster_view(members, now=now, stale_seconds=stale_seconds)
    return {
        "type": LOBBY_STATE_FRAME,
        "lobby_id": str(lobby.id),
        "status": lobby.status,
        "composition": dict(composition),
        "members": [m.as_dict() for m in roster],
    }


async def stream_lobby_state(
    session_factory: async_sessionmaker[AsyncSession],
    lobby_id: uuid.UUID,
    *,
    config: LobbyStreamConfig | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AsyncIterator[str]:
    """Yield identity-blind ``lobby_state`` SSE frames for a lobby.

    Emits an initial snapshot then re-emits on any change (status / roster /
    ready / presence), with keep-alive comments while idle. Closes when the lobby
    leaves the joinable lifecycle (LAUNCHED/CLOSED/vanished) or after
    ``idle_timeout_ms`` of no change. A fresh short-lived session is opened per
    poll (no long-held DB session — friend lobbies can sit open for a while).
    """
    cfg = config or default_lobby_stream_config()

    last_payload: str | None = None
    last_change = clock()
    last_emit = last_change

    while True:
        now = datetime.now(UTC)
        async with session_factory() as session:
            frame = await _build_state_frame(
                session, lobby_id=lobby_id, now=now, stale_seconds=cfg.stale_seconds
            )

        if frame is None:
            return

        payload = json.dumps(frame, sort_keys=True)
        tick = clock()
        if payload != last_payload:
            last_payload = payload
            last_change = tick
            last_emit = tick
            yield f"data: {payload}\n\n"
        else:
            if (tick - last_emit) * 1000 >= cfg.heartbeat_ms:
                last_emit = tick
                yield ": keep-alive\n\n"

        if frame["status"] not in _OPEN_STATUSES:
            return
        if (tick - last_change) * 1000 >= cfg.idle_timeout_ms:
            return

        await asyncio.sleep(cfg.poll_ms / 1000)


__all__ = [
    "LOBBY_STATE_FRAME",
    "LobbyStreamConfig",
    "default_lobby_stream_config",
    "stream_lobby_state",
]
