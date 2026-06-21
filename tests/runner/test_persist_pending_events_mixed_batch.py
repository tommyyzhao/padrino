"""US-189: persist_pending_events mirrors every NON-co-committed event row.

Regression for the max-sequence-threshold skip: in a human tick ``run_tick``
appends ``ActionTimedOut`` / ``OutputInvalid`` failure events to the in-memory
event log WITHOUT co-committing them, then a takeover / chat-release co-commits
its own ``content_ref`` / ``SeatTakenOver`` event row at a HIGHER sequence in the
same tick. A max-based skip (``stored.sequence <= max_persisted``) would drop the
lower un-persisted failure rows — they live below the co-committed sequence — so
a rehydrate from ``game_events`` would reconstruct a DIVERGENT state. The fix
skips the EXACT set of already-persisted sequences, so every event_log sequence
in the batch ends up with a matching ``game_events`` row.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.event_log import EventLog
from padrino.db.models import Game
from padrino.db.repositories import events as events_repo
from padrino.runner.game_runner import (
    GamePersistence,
    _persist_pending_event_rows,
    _persist_stored_event,
)

_PHASE = "DAY_1_VOTE"


async def _seed_game(session: AsyncSession) -> uuid.UUID:
    game = Game(
        gauntlet_id=None,
        ruleset_id="mini7_v1",
        game_seed="us189-mixed-batch",
        status="RUNNING",
    )
    session.add(game)
    await session.flush()
    return game.id


def _failure_body(event_type: str, seat: str) -> dict[str, object]:
    return {
        "event_type": event_type,
        "phase": _PHASE,
        "visibility": "SYSTEM",
        "actor_player_id": seat,
        "payload": {"expected_action_type": "VOTE", "defaulted_to": "ABSTAIN"},
    }


def _co_committed_body(seat: str) -> dict[str, object]:
    return {
        "event_type": "PublicMessageSubmitted",
        "phase": _PHASE,
        "visibility": "PUBLIC",
        "actor_player_id": seat,
        "payload": {"text": "", "round_index": 1, "content_ref": "ref-abc"},
    }


@pytest.mark.asyncio
async def test_persist_pending_events_keeps_failure_rows_below_a_co_committed_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game_id = await _seed_game(session)

    persistence = GamePersistence(session_factory=session_factory, game_id=game_id)

    event_log = EventLog()
    log_before = len(event_log.events)

    # In-tick order mirrors the real human lane: run_tick appends failure events
    # FIRST (lower sequences, NOT co-committed), then the chat-release / takeover
    # co-commits its own event row at a HIGHER sequence.
    timed_out = event_log.append(_failure_body("ActionTimedOut", "P01"))
    output_invalid = event_log.append(_failure_body("OutputInvalid", "P02"))
    co_committed = event_log.append(_co_committed_body("P03"))

    assert [timed_out.sequence, output_invalid.sequence, co_committed.sequence] == [0, 1, 2]

    # The co-commit path persists ONLY its own (higher) sequence in its own txn.
    await _persist_stored_event(persistence, co_committed)

    async with session_factory() as session:
        before = await events_repo.list_events(session, game_id)
    assert sorted(r.sequence for r in before) == [co_committed.sequence]

    # The outer loop now mirrors the remaining pending events.
    await _persist_pending_event_rows(persistence, event_log, log_before)

    # EVERY event_log sequence in the batch has a matching game_events row —
    # including the failure rows that sit BELOW the co-committed sequence.
    async with session_factory() as session:
        rows = await events_repo.list_events(session, game_id)
    by_sequence = {r.sequence: r for r in rows}
    assert sorted(by_sequence) == [s.sequence for s in event_log.events]
    assert by_sequence[timed_out.sequence].event_type == "ActionTimedOut"
    assert by_sequence[output_invalid.sequence].event_type == "OutputInvalid"
    assert by_sequence[co_committed.sequence].event_type == "PublicMessageSubmitted"
    # No duplicate rows (the already-committed sequence was skipped, not re-inserted).
    assert len(rows) == len(event_log.events)


@pytest.mark.asyncio
async def test_persist_pending_events_is_idempotent_on_full_replay(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Re-running persistence over an already-mirrored batch inserts nothing new."""
    async with session_factory() as session, session.begin():
        game_id = await _seed_game(session)

    persistence = GamePersistence(session_factory=session_factory, game_id=game_id)
    event_log = EventLog()
    event_log.append(_failure_body("ActionTimedOut", "P01"))
    event_log.append(_co_committed_body("P03"))

    await _persist_pending_event_rows(persistence, event_log, 0)
    # Second pass over the same range must be a no-op (no uq_game_event_sequence trip).
    await _persist_pending_event_rows(persistence, event_log, 0)

    async with session_factory() as session:
        rows = await events_repo.list_events(session, game_id)
    assert sorted(r.sequence for r in rows) == [0, 1]
    assert len(rows) == 2
