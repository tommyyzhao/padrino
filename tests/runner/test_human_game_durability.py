"""US-131: durable, rehydratable human-game state across a process restart.

A human-lane game can last minutes to hours, so a process restart must not lose
it. These tests build a partial in-progress human game (persisting its
hash-chained event log + a ``human_game_runtime`` row), then simulate a restart
by calling :func:`rehydrate_active_human_games` against a fresh session and
assert:

- the rehydrated core :class:`GameState` equals the state replayed from the same
  event log (replay equality — the snapshot is NEVER the source of core state);
- the current phase + deadline + buffer snapshot resume from the runtime row;
- no events are lost (the rebuilt log has the same sequence head + hash chain);
- a COMPLETED game and an AI-only benchmark game are NOT rehydrated.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.event_log import EventLog
from padrino.core.engine.events import EventAdapter
from padrino.core.engine.reducer import apply_event, initial_state
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.engine.state import GameState
from padrino.core.enums import SeatKind
from padrino.core.rulesets import mini7_v1
from padrino.db.models import Game, GameSeat
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import human_game_runtime as runtime_repo
from padrino.runner.human_durability import rehydrate_active_human_games

_GAME_SEED = "human-durability-seed"
_PHASE_ID = "DAY_1_DISCUSSION"
_DEADLINE = datetime(2026, 6, 19, 12, 0, 30, tzinfo=UTC)
_HUMAN_SEAT = "P01"


def _partial_human_game_bodies(game_id: str) -> list[dict[str, Any]]:
    """Build a partial (non-terminal) human game's event bodies.

    GameCreated -> RolesAssigned (P01 marked HUMAN) -> PhaseStarted (DAY_1) ->
    one human public message. The game has NOT terminated.
    """
    seats = assign_roles(_GAME_SEED, mini7_v1)
    assignments = [
        {
            "public_player_id": s.public_player_id,
            "seat_index": s.seat_index,
            "role": s.role.value,
            "faction": s.faction.value,
            "seat_kind": (
                SeatKind.HUMAN.value if s.public_player_id == _HUMAN_SEAT else SeatKind.AI.value
            ),
        }
        for s in seats
    ]
    return [
        {
            "event_type": "GameCreated",
            "sequence": 0,
            "phase": "SETUP",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {
                "ruleset_id": mini7_v1.RULESET_ID,
                "game_id": game_id,
                "game_seed": _GAME_SEED,
                "player_count": mini7_v1.PLAYER_COUNT,
            },
        },
        {
            "event_type": "RolesAssigned",
            "sequence": 1,
            "phase": "SETUP",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {"assignments": assignments},
        },
        {
            "event_type": "PhaseStarted",
            "sequence": 2,
            "phase": _PHASE_ID,
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {"phase_kind": "DAY_DISCUSSION", "day": 1, "round": 1},
        },
        {
            "event_type": "PublicMessageSubmitted",
            "sequence": 3,
            "phase": _PHASE_ID,
            "visibility": "PUBLIC",
            "actor_player_id": _HUMAN_SEAT,
            "payload": {"text": "", "round_index": 0, "content_ref": "sha256:deadbeef"},
        },
    ]


def _replay_state(bodies: list[dict[str, Any]]) -> tuple[GameState, EventLog]:
    log = EventLog()
    state = initial_state()
    for body in bodies:
        stored = log.append(body)
        state = apply_event(state, EventAdapter.validate_python(stored.body))
    return state, log


async def _persist_partial_game(
    session: AsyncSession,
    *,
    bodies: list[dict[str, Any]],
    status: str,
    human: bool,
) -> uuid.UUID:
    game = Game(
        gauntlet_id=None,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=_GAME_SEED,
        status=status,
    )
    session.add(game)
    await session.flush()

    log = EventLog()
    for body in bodies:
        body = {**body, "payload": {**body["payload"]}}
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

    seats = assign_roles(_GAME_SEED, mini7_v1)
    for s in seats:
        is_human = human and s.public_player_id == _HUMAN_SEAT
        # agent_build_id is left NULL for every seat: rehydration only reads
        # seat_kind to detect the human lane, and seeding a real agent-build FK
        # chain would add unrelated noise to this durability test.
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id=s.public_player_id,
                seat_index=s.seat_index,
                agent_build_id=None,
                seat_kind=SeatKind.HUMAN.value if is_human else SeatKind.AI.value,
                role=s.role.value,
                faction=s.faction.value,
                alive=True,
            )
        )
    await session.flush()
    return game.id


async def test_rehydrate_resumes_in_progress_human_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id_str = str(uuid.uuid4())
    bodies = _partial_human_game_bodies(game_id_str)
    expected_state, expected_log = _replay_state(bodies)
    buffer = {"P01": {"pending_action": {"type": "VOTE", "target": "P02"}}}

    async with session_factory() as session, session.begin():
        game_id = await _persist_partial_game(session, bodies=bodies, status="RUNNING", human=True)
        await runtime_repo.upsert(
            session,
            game_id=game_id,
            phase=_PHASE_ID,
            deadline_at=_DEADLINE,
            buffer_snapshot=buffer,
            updated_at=datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC),
        )

    # Simulate a process restart: rebuild runner state from the DB alone.
    rehydrated = await rehydrate_active_human_games(session_factory)

    assert len(rehydrated) == 1
    game = rehydrated[0]
    assert game.game_id == game_id

    # Core state is reconstructed from the event log (replay equality), NOT the
    # snapshot — every field matches a fresh in-memory replay of the same log.
    assert game.state == expected_state
    assert game.state.terminal_result is None
    assert game.state.current_phase.kind.value == "DAY_DISCUSSION"
    assert game.state.current_phase.day == 1

    # No events lost: the rebuilt log has the same head sequence + hash chain.
    assert game.event_log.events[-1].sequence == expected_log.events[-1].sequence
    assert game.event_log.head_hash == expected_log.head_hash

    # The impure phase scaffolding resumed from the runtime row.
    assert game.phase == _PHASE_ID
    assert game.deadline_at == _DEADLINE
    assert game.buffer_snapshot == buffer


async def test_rehydrate_skips_completed_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bodies = _partial_human_game_bodies(str(uuid.uuid4()))
    async with session_factory() as session, session.begin():
        game_id = await _persist_partial_game(
            session, bodies=bodies, status="COMPLETED", human=True
        )
        await runtime_repo.upsert(
            session,
            game_id=game_id,
            phase=_PHASE_ID,
            deadline_at=_DEADLINE,
            buffer_snapshot={},
            updated_at=datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC),
        )

    rehydrated = await rehydrate_active_human_games(session_factory)
    assert rehydrated == []


async def test_rehydrate_skips_ai_only_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # An AI-only benchmark game with a (stale) runtime row must NOT be resumed on
    # the human lane.
    seats = assign_roles(_GAME_SEED, mini7_v1)
    ai_assignments = [
        {
            "public_player_id": s.public_player_id,
            "seat_index": s.seat_index,
            "role": s.role.value,
            "faction": s.faction.value,
        }
        for s in seats
    ]
    bodies = _partial_human_game_bodies(str(uuid.uuid4()))
    bodies[1] = {**bodies[1], "payload": {"assignments": ai_assignments}}

    async with session_factory() as session, session.begin():
        game_id = await _persist_partial_game(session, bodies=bodies, status="RUNNING", human=False)
        await runtime_repo.upsert(
            session,
            game_id=game_id,
            phase=_PHASE_ID,
            deadline_at=_DEADLINE,
            buffer_snapshot={},
            updated_at=datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC),
        )

    rehydrated = await rehydrate_active_human_games(session_factory)
    assert rehydrated == []


async def test_rehydrate_multiple_games_deterministic_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids: list[uuid.UUID] = []
    async with session_factory() as session, session.begin():
        for _ in range(3):
            bodies = _partial_human_game_bodies(str(uuid.uuid4()))
            game_id = await _persist_partial_game(
                session, bodies=bodies, status="RUNNING", human=True
            )
            await runtime_repo.upsert(
                session,
                game_id=game_id,
                phase=_PHASE_ID,
                deadline_at=_DEADLINE,
                buffer_snapshot={},
                updated_at=datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC),
            )
            ids.append(game_id)

    rehydrated = await rehydrate_active_human_games(session_factory)
    assert len(rehydrated) == 3
    got = [g.game_id for g in rehydrated]
    assert got == sorted(ids)
