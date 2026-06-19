"""US-123: out-of-band human-chat sidecar (off the hash chain).

Asserts the core GDPR-vs-replay invariant:

- raw human chat text lives ONLY in ``human_chat_messages``; the paired
  hash-chained ``game_events`` row carries only an opaque ``content_ref``;
- redacting a sidecar row nulls the raw/cleaned text and flips ``redacted``
  WITHOUT touching ``game_events`` — so every ``event_hash`` is unchanged and
  ``verify_chain`` still passes before AND after redaction;
- the raw human text never appears in any ``game_events`` row.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.event_log import EventLog
from padrino.core.human_chat import human_chat_content_ref
from padrino.db.models import Game, GameEvent, HumanChatMessage
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import human_chat as human_chat_repo
from padrino.export.bundle import EventEnvelope, verify_chain

_RAW_HUMAN_TEXT = "Hi I am Alice Smith, call me at 555-0100, I think P03 is mafia"
_HUMAN_SEQUENCE = 2


def _envelopes(rows: list[GameEvent]) -> list[EventEnvelope]:
    return [
        EventEnvelope(
            sequence=row.sequence,
            event_type=row.event_type,
            phase=row.phase,
            visibility=row.visibility,
            actor_player_id=row.actor_player_id,
            payload=row.payload,
            prev_event_hash=row.prev_event_hash,
            event_hash=row.event_hash,
        )
        for row in rows
    ]


async def _seed_game_with_human_message(
    session: AsyncSession,
) -> tuple[uuid.UUID, str]:
    """Build a small hash-chained log whose human message stores only a ref.

    Returns (game_id, head_hash). The raw human text goes ONLY to the sidecar.
    """
    game = Game(gauntlet_id=None, ruleset_id="mini7_v1", game_seed="sidecar-seed", status="CREATED")
    session.add(game)
    await session.flush()

    content_ref = human_chat_content_ref(_RAW_HUMAN_TEXT)
    bodies: list[dict[str, Any]] = [
        {
            "event_type": "GameCreated",
            "sequence": 0,
            "phase": "SETUP",
            "visibility": "PUBLIC",
            "actor_player_id": None,
            "payload": {
                "ruleset_id": "mini7_v1",
                "game_id": str(game.id),
                "game_seed": "sidecar-seed",
                "player_count": 7,
            },
        },
        {
            "event_type": "PhaseStarted",
            "sequence": 1,
            "phase": "DAY_1_DISCUSSION",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {"phase_kind": "DAY_DISCUSSION", "day": 1, "round": 1},
        },
        {
            # Human seat message: text empty, the raw text lives in the sidecar;
            # only the opaque content_ref is in the hash-chained payload.
            "event_type": "PublicMessageSubmitted",
            "sequence": _HUMAN_SEQUENCE,
            "phase": "DAY_1_DISCUSSION",
            "visibility": "PUBLIC",
            "actor_player_id": "P01",
            "payload": {"text": "", "round_index": 0, "content_ref": content_ref},
        },
    ]

    log = EventLog()
    for body in bodies:
        stored = log.append(body)
        await events_repo.append_event(
            session,
            game_id=game.id,
            sequence=stored.sequence,
            event_type=body["event_type"],
            phase=body["phase"],
            visibility=body["visibility"],
            actor_player_id=body["actor_player_id"],
            payload=body["payload"],
            prev_event_hash=stored.prev_event_hash,
            event_hash=stored.event_hash,
        )

    await human_chat_repo.append_human_chat(
        session,
        game_id=game.id,
        sequence=_HUMAN_SEQUENCE,
        public_player_id="P01",
        raw_text=_RAW_HUMAN_TEXT,
        cleaned_text=_RAW_HUMAN_TEXT,
    )
    await session.commit()
    return game.id, log.head_hash


async def test_human_message_event_carries_only_content_ref(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The hash-chained payload holds the content_ref, never the raw text."""
    async with session_factory() as session:
        game_id, _ = await _seed_game_with_human_message(session)

    async with session_factory() as session:
        rows = await events_repo.list_events(session, game_id)
        msg = next(r for r in rows if r.event_type == "PublicMessageSubmitted")
        assert msg.payload["content_ref"] == human_chat_content_ref(_RAW_HUMAN_TEXT)
        assert msg.payload["text"] == ""
        # The PII is absent from EVERY persisted event payload.
        for row in rows:
            assert _RAW_HUMAN_TEXT not in str(row.payload)
            assert "Alice Smith" not in str(row.payload)


async def test_redaction_leaves_hash_chain_unchanged(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Redacting the sidecar nulls the text but never perturbs any event_hash."""
    async with session_factory() as session:
        game_id, head_before = await _seed_game_with_human_message(session)

    # Snapshot every event hash + verify the chain before redaction.
    async with session_factory() as session:
        rows_before = await events_repo.list_events(session, game_id)
        hashes_before = [(r.sequence, r.event_hash) for r in rows_before]
        assert verify_chain(_envelopes(rows_before)) == head_before

    # Redact the human message in the sidecar.
    async with session_factory() as session:
        affected = await human_chat_repo.redact(session, game_id=game_id, sequence=_HUMAN_SEQUENCE)
        await session.commit()
        assert affected == 1

    async with session_factory() as session:
        # The sidecar row is wiped + flagged.
        side = await human_chat_repo.get_human_chat(
            session, game_id=game_id, sequence=_HUMAN_SEQUENCE
        )
        assert side is not None
        assert side.raw_text is None
        assert side.cleaned_text is None
        assert side.redacted is True

        # Every event row + its hash is byte-identical; the chain still verifies.
        rows_after = await events_repo.list_events(session, game_id)
        hashes_after = [(r.sequence, r.event_hash) for r in rows_after]
        assert hashes_after == hashes_before
        assert verify_chain(_envelopes(rows_after)) == head_before


async def test_raw_text_never_in_game_events_table(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The raw human text exists in the sidecar and NOWHERE in game_events."""
    async with session_factory() as session:
        game_id, _ = await _seed_game_with_human_message(session)

    async with session_factory() as session:
        side_rows = (
            (
                await session.execute(
                    select(HumanChatMessage).where(HumanChatMessage.game_id == game_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(side_rows) == 1
        assert side_rows[0].raw_text == _RAW_HUMAN_TEXT

        event_rows = (
            (await session.execute(select(GameEvent).where(GameEvent.game_id == game_id)))
            .scalars()
            .all()
        )
        for row in event_rows:
            assert _RAW_HUMAN_TEXT not in str(row.payload)


def test_content_ref_is_deterministic_sha256() -> None:
    """The content ref is a pure, deterministic sha256 of the message text."""
    ref = human_chat_content_ref("hello")
    assert ref == human_chat_content_ref("hello")
    assert ref.startswith("sha256:")
    assert ref != human_chat_content_ref("HELLO")
    # 'sha256:' + 64 hex chars.
    assert len(ref) == len("sha256:") + 64
