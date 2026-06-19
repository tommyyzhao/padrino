"""CRUD helpers for the buffered human-chat hold (US-135).

The authenticated human chat channel parks a submitted message here as a *held*
row (``status='HELD'``) until the block-before-release moderation hook (US-140)
verdicts it. The ``idempotency_key`` dedupes network retries: a row is unique per
``(game_id, public_player_id, phase, idempotency_key)`` so a retried POST with the
same key returns the already-held/released message instead of inserting a
duplicate (no double-post).

This repository imports no clock / RNG (the repository-purity guard forbids
``time`` / ``secrets`` / ``random``): ``created_at`` / ``released_at`` are passed
in from the impure API shell.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import HumanChatSubmission

STATUS_HELD = "HELD"
STATUS_RELEASED = "RELEASED"
STATUS_BLOCKED = "BLOCKED"


async def get_by_idempotency_key(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    public_player_id: str,
    phase: str,
    idempotency_key: str,
) -> HumanChatSubmission | None:
    """Return the chat submission recorded under this exact key, or None."""
    stmt = select(HumanChatSubmission).where(
        HumanChatSubmission.game_id == game_id,
        HumanChatSubmission.public_player_id == public_player_id,
        HumanChatSubmission.phase == phase,
        HumanChatSubmission.idempotency_key == idempotency_key,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def record_held(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    public_player_id: str,
    phase: str,
    channel: str,
    idempotency_key: str,
    raw_text: str,
    created_at: datetime,
) -> HumanChatSubmission:
    """Insert a new chat submission into the buffer hold (``status='HELD'``).

    The caller must first check :func:`get_by_idempotency_key` to honour
    idempotency; this only ever inserts.
    """
    row = HumanChatSubmission(
        game_id=game_id,
        public_player_id=public_player_id,
        phase=phase,
        channel=channel,
        idempotency_key=idempotency_key,
        raw_text=raw_text,
        status=STATUS_HELD,
        created_at=created_at,
    )
    session.add(row)
    await session.flush()
    return row


async def next_sidecar_sequence(session: AsyncSession, *, game_id: uuid.UUID) -> int:
    """Return the next per-game sidecar sequence for a released human message.

    The released raw text is paired to the :class:`padrino.db.models.HumanChatMessage`
    sidecar (keyed by ``(game_id, sequence)``). Until the human-aware tick
    (US-138/140) chains the message into a real ``game_events`` sequence, the
    hold allocates a monotonic per-game ordinal from existing sidecar rows so the
    sidecar unique constraint never collides on a single game.
    """
    stmt = select(HumanChatSubmission.sidecar_sequence).where(
        HumanChatSubmission.game_id == game_id,
        HumanChatSubmission.sidecar_sequence.is_not(None),
    )
    existing = [seq for seq in (await session.execute(stmt)).scalars() if seq is not None]
    return (max(existing) + 1) if existing else 0


async def mark_released(
    session: AsyncSession,
    *,
    submission: HumanChatSubmission,
    sidecar_sequence: int,
    released_at: datetime,
) -> HumanChatSubmission:
    """Flip a held submission to ``status='RELEASED'`` after moderation passes."""
    submission.status = STATUS_RELEASED
    submission.sidecar_sequence = sidecar_sequence
    submission.released_at = released_at
    await session.flush()
    return submission


async def mark_blocked(
    session: AsyncSession,
    *,
    submission: HumanChatSubmission,
) -> HumanChatSubmission:
    """Flip a held submission to ``status='BLOCKED'`` — never released/chained."""
    submission.status = STATUS_BLOCKED
    await session.flush()
    return submission
