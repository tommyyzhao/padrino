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

from padrino.core.engine.event_log import EventLog
from padrino.core.human_chat import human_chat_content_ref
from padrino.db.repositories import human_chat as sidecar_repo
from padrino.db.repositories import human_chat_submissions as holds_repo

MAFIA_CHANNEL_ID = "mafia"


@dataclass(frozen=True, slots=True)
class ReleasedHeldChat:
    """One approved held human message released to the sidecar."""

    public_player_id: str
    channel: str
    sidecar_sequence: int
    released_at: datetime
    content_ref: str


def _round_index_for_phase(phase: str) -> int | None:
    """Extract the discussion round index from a canonical DAY phase id."""
    marker = "_DISCUSSION_ROUND_"
    if marker not in phase:
        return None
    try:
        return int(phase.rsplit(marker, 1)[1])
    except ValueError:
        return None


def _message_event_body(
    *,
    sequence: int,
    phase: str,
    public_player_id: str,
    channel: str,
    content_ref: str,
) -> dict[str, object]:
    """Build the ref-only core chat event paired to one sidecar row."""
    if channel == "PRIVATE":
        return {
            "event_type": "PrivateMessageSubmitted",
            "sequence": sequence,
            "phase": phase,
            "visibility": "PRIVATE",
            "actor_player_id": public_player_id,
            "payload": {
                "text": "",
                "channel_id": MAFIA_CHANNEL_ID,
                "content_ref": content_ref,
            },
        }
    return {
        "event_type": "PublicMessageSubmitted",
        "sequence": sequence,
        "phase": phase,
        "visibility": "PUBLIC",
        "actor_player_id": public_player_id,
        "payload": {
            "text": "",
            "round_index": _round_index_for_phase(phase),
            "content_ref": content_ref,
        },
    }


async def release_held_chat_for_phase(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    phase: str,
    released_at: datetime,
    event_log: EventLog,
) -> tuple[ReleasedHeldChat, ...]:
    """Release every approved held human chat row for ``phase``.

    The raw text moves to the out-of-band sidecar only here, never at POST time.
    The paired core event enters ``event_log`` with only ``content_ref`` and an
    empty ``text`` field; the outer game loop persists pending log entries in
    sequence order.
    Rows already released or blocked are ignored, making the helper idempotent
    for retries after a partial runner restart.
    """
    released: list[ReleasedHeldChat] = []
    held = await holds_repo.list_releasable_for_phase(session, game_id=game_id, phase=phase)
    for submission in held:
        cleaned_text = submission.cleaned_text
        if cleaned_text is None:
            continue
        sequence = len(event_log.events)
        content_ref = human_chat_content_ref(submission.raw_text)
        event_log.append(
            _message_event_body(
                sequence=sequence,
                phase=phase,
                public_player_id=submission.public_player_id,
                channel=submission.channel,
                content_ref=content_ref,
            )
        )
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
                content_ref=content_ref,
            )
        )
    return tuple(released)


__all__ = ["ReleasedHeldChat", "release_held_chat_for_phase"]
