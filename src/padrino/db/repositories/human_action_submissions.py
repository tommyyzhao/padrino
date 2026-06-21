"""CRUD helpers for :class:`padrino.db.models.HumanActionSubmission` (US-134).

The authenticated human action channel buffers a validated structured ``Action``
here so the human-aware tick (US-137/138) can resolve the seat's turn from
buffered input. The ``idempotency_key`` dedupes network retries: a row is unique
per ``(game_id, public_player_id, phase, idempotency_key)`` so a retried POST
with the same key returns the already-recorded action instead of double-voting.

This repository imports no clock / RNG (the repository-purity guard forbids
``time`` / ``secrets`` / ``random``): ``created_at`` is passed in from the impure
API shell.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import HumanActionSubmission


async def get_by_idempotency_key(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    public_player_id: str,
    phase: str,
    idempotency_key: str,
) -> HumanActionSubmission | None:
    """Return the submission previously recorded under this exact key, or None."""
    stmt = select(HumanActionSubmission).where(
        HumanActionSubmission.game_id == game_id,
        HumanActionSubmission.public_player_id == public_player_id,
        HumanActionSubmission.phase == phase,
        HumanActionSubmission.idempotency_key == idempotency_key,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def latest_for_phase(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    public_player_id: str,
    phase: str,
) -> HumanActionSubmission | None:
    """Return the newest buffered action for this seat/phase, or None.

    The POST channel is idempotent per key but a human may submit a replacement
    action with a new key before the phase deadline. The adapter consumes the
    latest row by server timestamp, with UUID as a deterministic tie-breaker.
    """
    stmt = (
        select(HumanActionSubmission)
        .where(
            HumanActionSubmission.game_id == game_id,
            HumanActionSubmission.public_player_id == public_player_id,
            HumanActionSubmission.phase == phase,
        )
        .order_by(HumanActionSubmission.created_at.desc(), HumanActionSubmission.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def record(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    public_player_id: str,
    phase: str,
    idempotency_key: str,
    action_type: str,
    target: str | None,
    created_at: datetime,
) -> HumanActionSubmission:
    """Insert a new action submission and return it.

    The caller must first check :func:`get_by_idempotency_key` to honour
    idempotency; this only ever inserts.
    """
    row = HumanActionSubmission(
        game_id=game_id,
        public_player_id=public_player_id,
        phase=phase,
        idempotency_key=idempotency_key,
        action_type=action_type,
        target=target,
        created_at=created_at,
    )
    session.add(row)
    await session.flush()
    return row
