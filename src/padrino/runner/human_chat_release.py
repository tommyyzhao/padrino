"""Release moderated human chat on the human tick schedule (US-159).

The authenticated POST channel validates, rate-limits, and moderates a human
message, but it must not surface that message immediately. This impure runner
helper is called only after :func:`padrino.runner.human_tick.run_human_tick` has
completed its fixed release delay, so human sidecar visibility follows the same
phase-level schedule as AI chat emitted by the game loop.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.engine.event_log import EventLog, StoredEvent
from padrino.core.human_chat import human_chat_content_ref
from padrino.db.repositories import events as events_repo
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


async def _persist_event_row(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    stored: StoredEvent,
) -> None:
    """Append one already-sealed event row to ``game_events`` within ``session``."""
    body = stored.body
    await events_repo.append_event(
        session,
        game_id=game_id,
        sequence=stored.sequence,
        event_type=str(body["event_type"]),
        phase=str(body["phase"]),
        visibility=str(body["visibility"]),
        actor_player_id=body.get("actor_player_id"),
        payload=dict(body.get("payload", {})),
        prev_event_hash=stored.prev_event_hash,
        event_hash=stored.event_hash,
    )


async def release_held_chat_for_phase(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    phase: str,
    released_at: datetime,
    event_log: EventLog,
    pending_lower_events: Sequence[StoredEvent] = (),
) -> tuple[ReleasedHeldChat, ...]:
    """Release every approved held human chat row for ``phase``.

    The raw text moves to the out-of-band sidecar only here, never at POST time.
    The paired core event enters ``event_log`` with only ``content_ref`` and an
    empty ``text`` field; the outer game loop persists pending log entries in
    sequence order.
    Rows already released or blocked are ignored, making the helper idempotent
    for retries after a partial runner restart.

    ``pending_lower_events`` are not-yet-persisted in-memory event_log entries
    (typically ``ActionTimedOut`` / ``OutputInvalid`` failure rows appended
    earlier in the SAME tick) whose sequences sit BELOW the content_ref chat row
    this helper appends. They are co-committed in this single transaction BEFORE
    the chat row so a crash can never leave ``game_events`` holding ``{N-1,
    N+1}`` with ``N`` only in memory — which would re-seal non-contiguously on
    rehydrate and raise ``ReplayHashMismatchError`` (US-196). Each is skipped if
    its sequence is already persisted, keeping the outer loop's
    ``persist_pending_events`` idempotent.
    """
    released: list[ReleasedHeldChat] = []
    held = await holds_repo.list_releasable_for_phase(session, game_id=game_id, phase=phase)
    if not held:
        return ()
    pending_by_sequence = {stored.sequence: stored for stored in pending_lower_events}
    if pending_by_sequence:
        already = await events_repo.persisted_sequences_from(
            session,
            game_id,
            from_sequence=min(pending_by_sequence),
        )
        for sequence in sorted(pending_by_sequence):
            if sequence in already:
                continue
            await _persist_event_row(session, game_id=game_id, stored=pending_by_sequence[sequence])
    for submission in held:
        cleaned_text = submission.cleaned_text
        if cleaned_text is None:
            continue
        sequence = len(event_log.events)
        content_ref = human_chat_content_ref(submission.raw_text)
        stored = event_log.append(
            _message_event_body(
                sequence=sequence,
                phase=phase,
                public_player_id=submission.public_player_id,
                channel=submission.channel,
                content_ref=content_ref,
            )
        )
        # Co-commit the content_ref event row with the sidecar row + hold flip in
        # this single transaction (hard rule 4): a crash can never leave a sidecar
        # row at sequence N without its game_events row, which would otherwise let
        # the next release re-derive sequence N and collide on
        # uq_human_chat_message_sequence. The outer loop's persist_pending_events
        # is idempotent against an already-committed sequence.
        await _persist_event_row(session, game_id=game_id, stored=stored)
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
