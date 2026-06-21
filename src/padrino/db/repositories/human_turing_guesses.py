"""CRUD helpers for :class:`padrino.db.models.HumanTuringGuess` (US-144).

After a human game terminates each human submits ONE spot-the-AI guess; the pure
scorer computes their detection accuracy and the guess + result persist here.
Exactly one guess per ``(game_id, guesser_public_id)`` - a retry returns the
stored row rather than re-scoring (a guesser guesses once).

This repository imports no clock / RNG (the repository-purity guard forbids
``time`` / ``secrets`` / ``random``): ``created_at`` is passed in from the impure
API shell.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import HumanTuringGuess


async def get_for_guesser(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    guesser_public_id: str,
) -> HumanTuringGuess | None:
    """Return the guess this seat already submitted in this game, or None."""
    stmt = select(HumanTuringGuess).where(
        HumanTuringGuess.game_id == game_id,
        HumanTuringGuess.guesser_public_id == guesser_public_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def record(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    guesser_public_id: str,
    guess: dict[str, Any],
    total: int,
    correct: int,
    accuracy: str,
    created_at: datetime,
) -> HumanTuringGuess:
    """Insert one guesser's guess + score and return it.

    The caller must first check :func:`get_for_guesser` to honour the
    once-per-guesser rule; this only ever inserts.
    """
    row = HumanTuringGuess(
        game_id=game_id,
        guesser_public_id=guesser_public_id,
        guess=guess,
        total=total,
        correct=correct,
        accuracy=accuracy,
        created_at=created_at,
    )
    session.add(row)
    await session.flush()
    return row
