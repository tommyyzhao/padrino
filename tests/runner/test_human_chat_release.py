"""US-160: released human chat enters the chain by content_ref only."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.event_log import EventLog
from padrino.core.engine.events import EventAdapter
from padrino.core.engine.replay import replay_event_log
from padrino.core.human_chat import human_chat_content_ref
from padrino.db.models import Game
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import human_chat as sidecar_repo
from padrino.db.repositories import human_chat_submissions as holds_repo
from padrino.runner.human_chat_release import release_held_chat_for_phase

_PUBLIC_TEXT = "Human P01 says P04 is suspicious"
_PRIVATE_TEXT = "Human mafia P02 says kill P05"


async def _seed_game(session: AsyncSession) -> uuid.UUID:
    game = Game(
        gauntlet_id=None,
        ruleset_id="mini7_v1",
        game_seed="us160-release",
        status="RUNNING",
    )
    session.add(game)
    await session.flush()
    return game.id


async def _approved_hold(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    player_id: str,
    phase: str,
    channel: str,
    text: str,
) -> None:
    row = await holds_repo.record_held(
        session,
        game_id=game_id,
        public_player_id=player_id,
        phase=phase,
        channel=channel,
        idempotency_key=f"{channel}-{player_id}",
        raw_text=text,
        created_at=datetime(2026, 6, 20, tzinfo=UTC),
    )
    await holds_repo.mark_ready_for_release(session, submission=row, cleaned_text=text)


@pytest.mark.asyncio
async def test_public_human_chat_release_appends_content_ref_event_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phase = "DAY_1_DISCUSSION_ROUND_1"
    event_log = EventLog()
    async with session_factory() as session, session.begin():
        game_id = await _seed_game(session)
        await _approved_hold(
            session,
            game_id=game_id,
            player_id="P01",
            phase=phase,
            channel="PUBLIC",
            text=_PUBLIC_TEXT,
        )

        released = await release_held_chat_for_phase(
            session,
            game_id=game_id,
            phase=phase,
            released_at=datetime(2026, 6, 20, 12, tzinfo=UTC),
            event_log=event_log,
        )

    assert len(released) == 1
    event = event_log.events[0].body
    assert event["event_type"] == "PublicMessageSubmitted"
    assert event["phase"] == phase
    assert event["visibility"] == "PUBLIC"
    assert event["actor_player_id"] == "P01"
    assert event["payload"] == {
        "text": "",
        "round_index": 1,
        "content_ref": human_chat_content_ref(_PUBLIC_TEXT),
    }
    assert _PUBLIC_TEXT not in str(event)
    EventAdapter.validate_python(event)
    replay_event_log(event_log.events)

    async with session_factory() as session:
        sidecar = await sidecar_repo.get_human_chat(session, game_id=game_id, sequence=0)
    assert sidecar is not None
    assert sidecar.raw_text == _PUBLIC_TEXT
    assert sidecar.cleaned_text == _PUBLIC_TEXT


@pytest.mark.asyncio
async def test_private_human_chat_release_appends_content_ref_event_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phase = "NIGHT_1_MAFIA_DISCUSSION"
    event_log = EventLog()
    async with session_factory() as session, session.begin():
        game_id = await _seed_game(session)
        await _approved_hold(
            session,
            game_id=game_id,
            player_id="P02",
            phase=phase,
            channel="PRIVATE",
            text=_PRIVATE_TEXT,
        )

        released = await release_held_chat_for_phase(
            session,
            game_id=game_id,
            phase=phase,
            released_at=datetime(2026, 6, 20, 12, tzinfo=UTC),
            event_log=event_log,
        )

    assert len(released) == 1
    event = event_log.events[0].body
    assert event["event_type"] == "PrivateMessageSubmitted"
    assert event["phase"] == phase
    assert event["visibility"] == "PRIVATE"
    assert event["actor_player_id"] == "P02"
    assert event["payload"] == {
        "text": "",
        "channel_id": "mafia",
        "content_ref": human_chat_content_ref(_PRIVATE_TEXT),
    }
    assert _PRIVATE_TEXT not in str(event)
    EventAdapter.validate_python(event)
    replay_event_log(event_log.events)

    async with session_factory() as session:
        sidecar = await sidecar_repo.get_human_chat(session, game_id=game_id, sequence=0)
    assert sidecar is not None
    assert sidecar.raw_text == _PRIVATE_TEXT
    assert sidecar.cleaned_text == _PRIVATE_TEXT


@pytest.mark.asyncio
async def test_chat_release_co_commits_event_row_and_resumes_without_uq_collision(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """US-189: sidecar row + hold flip + content_ref event row are one txn.

    A crash window after the sidecar commit but before the event row would leave
    the DB ahead of the chain and wedge the next release on a sequence collision.
    Co-committing prevents that: after a release the sidecar sequence has a
    matching game_events row, and a fresh release rehydrating the log from
    game_events resumes at the next sequence without a uq collision.
    """
    phase = "DAY_1_DISCUSSION_ROUND_1"
    async with session_factory() as session, session.begin():
        game_id = await _seed_game(session)
        await _approved_hold(
            session,
            game_id=game_id,
            player_id="P01",
            phase=phase,
            channel="PUBLIC",
            text=_PUBLIC_TEXT,
        )
        first = await release_held_chat_for_phase(
            session,
            game_id=game_id,
            phase=phase,
            released_at=datetime(2026, 6, 20, 12, tzinfo=UTC),
            event_log=EventLog(),
        )
    assert len(first) == 1
    first_sequence = first[0].sidecar_sequence

    # The committed sidecar row has a matching game_events row at the same
    # sequence (the chain never lags the sidecar across a crash).
    async with session_factory() as session:
        sidecar = await sidecar_repo.get_human_chat(
            session, game_id=game_id, sequence=first_sequence
        )
        rows = await events_repo.list_events(session, game_id)
    assert sidecar is not None
    event_rows = [r for r in rows if r.sequence == first_sequence]
    assert len(event_rows) == 1
    assert event_rows[0].event_type == "PublicMessageSubmitted"
    assert event_rows[0].payload["content_ref"] == human_chat_content_ref(_PUBLIC_TEXT)

    # A fresh release rehydrates the in-memory log from the persisted event rows
    # and resumes at the next sequence — no uq_human_chat_message_sequence clash.
    async with session_factory() as session, session.begin():
        await _approved_hold(
            session,
            game_id=game_id,
            player_id="P03",
            phase=phase,
            channel="PUBLIC",
            text="second human line",
        )
        resumed_log = EventLog()
        for row in await events_repo.list_events(session, game_id):
            resumed_log.append(
                {
                    "event_type": row.event_type,
                    "sequence": row.sequence,
                    "phase": row.phase,
                    "visibility": row.visibility,
                    "actor_player_id": row.actor_player_id,
                    "payload": dict(row.payload),
                }
            )
        second = await release_held_chat_for_phase(
            session,
            game_id=game_id,
            phase=phase,
            released_at=datetime(2026, 6, 20, 12, 1, tzinfo=UTC),
            event_log=resumed_log,
        )
    assert len(second) == 1
    assert second[0].sidecar_sequence == first_sequence + 1

    async with session_factory() as session:
        rows = await events_repo.list_events(session, game_id)
    sequences = sorted(r.sequence for r in rows)
    assert sequences == [first_sequence, first_sequence + 1]
