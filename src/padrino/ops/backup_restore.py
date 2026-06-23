"""Hash-chain restore verification for database backup runbooks."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.game_status import GAME_STATUS_COMPLETED
from padrino.db.models import Game, GameEvent
from padrino.export.bundle import EventEnvelope, verify_chain

_GAME_TERMINATED_EVENT: Final[str] = "GameTerminated"


class RestoreVerificationError(ValueError):
    """Raised when a restored game cannot be trusted as a complete chain."""


@dataclass(frozen=True, slots=True)
class RestoreVerification:
    """Summary of one verified restored game event chain."""

    game_id: uuid.UUID
    event_count: int
    tip_hash: str
    stored_head_hash: str
    final_event_type: str


def envelope_from_game_event(row: GameEvent) -> EventEnvelope:
    """Convert a persisted ``game_events`` row to the replay verifier shape."""
    return EventEnvelope(
        sequence=row.sequence,
        event_type=row.event_type,
        phase=row.phase,
        visibility=row.visibility,
        actor_player_id=row.actor_player_id,
        payload=dict(row.payload),
        prev_event_hash=row.prev_event_hash,
        event_hash=row.event_hash,
    )


async def verify_restored_game_hash_chain(
    session: AsyncSession,
    game_id: uuid.UUID,
) -> RestoreVerification:
    """Verify that a restored completed game's event chain is intact.

    The verifier reads only the restored ``games`` and ``game_events`` rows,
    reconstructs the canonical hash-chain body for every event, and checks the
    recomputed tip against ``games.event_hash_head``. It is intentionally
    single-game scoped so operators can sample a known completed game after
    restoring a single-host Postgres backup.
    """
    game = await session.get(Game, game_id)
    if game is None:
        raise RestoreVerificationError(f"game {game_id} not found in restored database")
    if game.status != GAME_STATUS_COMPLETED:
        raise RestoreVerificationError(
            f"game {game_id} is not {GAME_STATUS_COMPLETED} (status={game.status!r})"
        )
    if game.event_hash_head is None:
        raise RestoreVerificationError(f"game {game_id} has no stored event_hash_head")

    stmt = select(GameEvent).where(GameEvent.game_id == game_id).order_by(GameEvent.sequence)
    rows = list((await session.execute(stmt)).scalars())
    if not rows:
        raise RestoreVerificationError(f"game {game_id} has no restored game_events rows")
    final_event_type = rows[-1].event_type
    if final_event_type != _GAME_TERMINATED_EVENT:
        raise RestoreVerificationError(
            f"game {game_id} final event is {final_event_type!r}, not {_GAME_TERMINATED_EVENT!r}"
        )

    tip_hash = verify_chain([envelope_from_game_event(row) for row in rows])
    if tip_hash != game.event_hash_head:
        raise RestoreVerificationError(
            f"game {game_id} restored tip {tip_hash} does not match "
            f"games.event_hash_head {game.event_hash_head}"
        )

    return RestoreVerification(
        game_id=game_id,
        event_count=len(rows),
        tip_hash=tip_hash,
        stored_head_hash=game.event_hash_head,
        final_event_type=final_event_type,
    )


__all__ = [
    "RestoreVerification",
    "RestoreVerificationError",
    "envelope_from_game_event",
    "verify_restored_game_hash_chain",
]
