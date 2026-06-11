"""Spoiler-safe public game broadcast index (US-087).

Tracks the broadcast lifecycle of each game independently of the game's
internal ``status`` so live-tension is preserved for viewers even though the
outcome is already committed in the DB.

Broadcast lifecycle:
  HIDDEN  -- default; game is not yet visible on any public surface.
  LIVE    -- game is currently being broadcast as a "live" paced replay.
              Outcome fields (terminal_result, winner) are NEVER exposed while
              in this state.
  RECENT  -- broadcast has completed; outcome is now visible to viewers.

Repository functions operate on a SQLAlchemy :class:`AsyncSession`. They are a
pure query layer — no scheduler coupling, no side-effects beyond the DB flush.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import Game

#: Keys that the public broadcast index MUST NOT expose for LIVE games.
#: Listing them explicitly lets callers assert spoiler-safety exhaustively.
OUTCOME_FIELDS: frozenset[str] = frozenset(
    {"terminal_result", "winner", "ratings_delta", "completed_at"}
)


class BroadcastState(StrEnum):
    HIDDEN = "HIDDEN"
    LIVE = "LIVE"
    RECENT = "RECENT"


@dataclass
class GamePublicEntry:
    """Public-safe summary of a game in the broadcast index.

    For LIVE games, ``terminal_result`` is always ``None`` regardless of what
    the underlying row contains — the spoiler protection is enforced here, not
    by the caller.
    """

    game_id: uuid.UUID
    ruleset_id: str
    broadcast_state: BroadcastState
    current_phase: str | None
    terminal_result: dict[str, Any] | None


def _to_entry(game: Game, *, spoiler_safe: bool) -> GamePublicEntry:
    return GamePublicEntry(
        game_id=game.id,
        ruleset_id=game.ruleset_id,
        broadcast_state=BroadcastState(game.broadcast_state),
        current_phase=game.current_phase,
        terminal_result=None if spoiler_safe else game.terminal_result,
    )


async def list_live(session: AsyncSession) -> list[GamePublicEntry]:
    """Return all games currently in LIVE broadcast state, spoiler-safe.

    Only games with ``is_broadcastable=True`` are returned — the moderation gate
    is enforced at the query layer as defense-in-depth.
    ``terminal_result`` is always ``None`` in the returned entries regardless
    of the stored value — outcome is hidden until broadcast completes.
    """
    stmt = select(Game).where(
        Game.broadcast_state == BroadcastState.LIVE.value,
        Game.is_broadcastable.is_(True),
    )
    result = await session.execute(stmt)
    return [_to_entry(g, spoiler_safe=True) for g in result.scalars()]


async def list_recent(session: AsyncSession, *, limit: int = 20) -> list[GamePublicEntry]:
    """Return recently broadcast games (RECENT state), WITH outcome exposed.

    Only games with ``is_broadcastable=True`` are returned.
    Results are ordered newest-first by ``created_at``.
    """
    stmt = (
        select(Game)
        .where(
            Game.broadcast_state == BroadcastState.RECENT.value,
            Game.is_broadcastable.is_(True),
        )
        .order_by(Game.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [_to_entry(g, spoiler_safe=False) for g in result.scalars()]


async def mark_live(session: AsyncSession, game_id: uuid.UUID) -> Game | None:
    """Transition a game to LIVE broadcast state.

    Returns ``None`` if the game does not exist or is not broadcastable.
    The moderation gate (US-093) must pass before calling this function.
    """
    game = await session.get(Game, game_id)
    if game is None or not game.is_broadcastable:
        return None
    game.broadcast_state = BroadcastState.LIVE.value
    await session.flush()
    return game


async def mark_recent(session: AsyncSession, game_id: uuid.UUID) -> Game | None:
    """Transition a game to RECENT broadcast state (broadcast complete).

    Returns ``None`` if the game does not exist or is not broadcastable.
    """
    game = await session.get(Game, game_id)
    if game is None or not game.is_broadcastable:
        return None
    game.broadcast_state = BroadcastState.RECENT.value
    await session.flush()
    return game


async def mark_hidden(session: AsyncSession, game_id: uuid.UUID) -> Game | None:
    """Transition a game back to HIDDEN (remove from public surfaces)."""
    game = await session.get(Game, game_id)
    if game is None:
        return None
    game.broadcast_state = BroadcastState.HIDDEN.value
    await session.flush()
    return game


__all__ = [
    "OUTCOME_FIELDS",
    "BroadcastState",
    "GamePublicEntry",
    "list_live",
    "list_recent",
    "mark_hidden",
    "mark_live",
    "mark_recent",
]
