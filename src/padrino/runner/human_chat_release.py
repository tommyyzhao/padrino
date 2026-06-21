"""Release moderated human chat on the human tick schedule (US-159).

The authenticated POST channel validates, rate-limits, and moderates a human
message, but it must not surface that message immediately. This impure runner
helper is called only after :func:`padrino.runner.human_tick.run_human_tick` has
completed its fixed release delay, so human sidecar visibility follows the same
phase-level schedule as AI chat emitted by the game loop.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.repositories import human_chat as sidecar_repo
from padrino.db.repositories import human_chat_submissions as holds_repo


@dataclass(frozen=True, slots=True)
class ReleasedHeldChat:
    """One approved held human message released to the sidecar."""

    public_player_id: str
    channel: str
    sidecar_sequence: int
    released_at: datetime


async def release_held_chat_for_phase(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    phase: str,
    released_at: datetime,
) -> tuple[ReleasedHeldChat, ...]:
    """Release every approved held human chat row for ``phase``.

    The raw text moves to the out-of-band sidecar only here, never at POST time.
    Rows already released or blocked are ignored, making the helper idempotent
    for retries after a partial runner restart.
    """
    released: list[ReleasedHeldChat] = []
    held = await holds_repo.list_releasable_for_phase(session, game_id=game_id, phase=phase)
    for submission in held:
        cleaned_text = submission.cleaned_text
        if cleaned_text is None:
            continue
        sequence = await holds_repo.next_sidecar_sequence(session, game_id=game_id)
        await sidecar_repo.append_human_chat(
            session,
            game_id=game_id,
            sequence=sequence,
            public_player_id=submission.public_player_id,
            raw_text=submission.raw_text,
            cleaned_text=cleaned_text,
        )
        await holds_repo.mark_released(
            session,
            submission=submission,
            sidecar_sequence=sequence,
            released_at=released_at,
        )
        released.append(
            ReleasedHeldChat(
                public_player_id=submission.public_player_id,
                channel=submission.channel,
                sidecar_sequence=sequence,
                released_at=released_at,
            )
        )
    return tuple(released)


__all__ = ["ReleasedHeldChat", "release_held_chat_for_phase"]
