"""CRUD + redaction helpers for the out-of-band human-chat sidecar (US-123).

Raw human chat text (PII) lives ONLY in ``human_chat_messages``; the paired
hash-chained core event carries only an opaque ``content_ref``. ``redact`` nulls
the raw/cleaned text and flips ``redacted`` WITHOUT touching ``game_events``, so
the event hash chain still verifies after a GDPR erasure.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import GameSeat, HumanChatMessage


async def append_human_chat(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    sequence: int,
    public_player_id: str,
    raw_text: str,
    cleaned_text: str | None = None,
) -> HumanChatMessage:
    """Insert one sidecar row pairing the human message to its event ``sequence``."""
    obj = HumanChatMessage(
        game_id=game_id,
        sequence=sequence,
        public_player_id=public_player_id,
        raw_text=raw_text,
        cleaned_text=cleaned_text,
    )
    session.add(obj)
    await session.flush()
    return obj


async def get_human_chat(
    session: AsyncSession, *, game_id: uuid.UUID, sequence: int
) -> HumanChatMessage | None:
    """Return the sidecar row for (``game_id``, ``sequence``) or None."""
    stmt = select(HumanChatMessage).where(
        HumanChatMessage.game_id == game_id,
        HumanChatMessage.sequence == sequence,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_for_game(session: AsyncSession, game_id: uuid.UUID) -> list[HumanChatMessage]:
    """Return all sidecar rows for ``game_id`` in sequence order."""
    stmt = (
        select(HumanChatMessage)
        .where(HumanChatMessage.game_id == game_id)
        .order_by(HumanChatMessage.sequence)
    )
    return list((await session.execute(stmt)).scalars())


async def list_for_principal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> list[HumanChatMessage]:
    """Return chat authored by a principal via the game-seat ownership join."""
    stmt = (
        select(HumanChatMessage)
        .join(
            GameSeat,
            (GameSeat.game_id == HumanChatMessage.game_id)
            & (GameSeat.public_player_id == HumanChatMessage.public_player_id),
        )
        .where(GameSeat.occupant_principal_id == principal_id)
        .order_by(HumanChatMessage.game_id, HumanChatMessage.sequence, HumanChatMessage.id)
    )
    return list((await session.execute(stmt)).scalars())


async def redact(session: AsyncSession, *, game_id: uuid.UUID, sequence: int) -> int:
    """Null the raw/cleaned text and set ``redacted=True`` for one message.

    Touches ONLY the sidecar row — never ``game_events`` — so the hash chain is
    unchanged and ``verify_chain`` still passes afterward. Returns the number of
    rows affected (0 if no such message).
    """
    count_stmt = select(HumanChatMessage.id).where(
        HumanChatMessage.game_id == game_id,
        HumanChatMessage.sequence == sequence,
    )
    affected = len((await session.execute(count_stmt)).scalars().all())
    await session.execute(
        update(HumanChatMessage)
        .where(
            HumanChatMessage.game_id == game_id,
            HumanChatMessage.sequence == sequence,
        )
        .values(raw_text=None, cleaned_text=None, redacted=True)
    )
    await session.flush()
    return affected
