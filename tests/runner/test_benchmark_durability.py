"""US-249: replay-from-events rehydration for benchmark games."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.event_log import EventLog
from padrino.core.engine.replay import ReplayHashMismatchError
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.rulesets import mini7_v1
from padrino.db.game_status import GAME_STATUS_RUNNING
from padrino.db.models import Game
from padrino.db.repositories import events as events_repo
from padrino.runner.benchmark_durability import rehydrate_benchmark_game
from padrino.runner.human_durability import replay_state_from_rows

_GAME_SEED = "benchmark-durability-seed"
_PHASE_ID = "DAY_1_DISCUSSION_ROUND_1"


def _game_created_body(game_id: str) -> dict[str, Any]:
    return {
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
    }


def _roles_assigned_body() -> dict[str, Any]:
    return {
        "event_type": "RolesAssigned",
        "sequence": 1,
        "phase": "SETUP",
        "visibility": "SYSTEM",
        "actor_player_id": None,
        "payload": {
            "assignments": [
                {
                    "public_player_id": seat.public_player_id,
                    "seat_index": seat.seat_index,
                    "role": seat.role.value,
                    "faction": seat.faction.value,
                }
                for seat in assign_roles(_GAME_SEED, mini7_v1)
            ]
        },
    }


def _phase_started_body() -> dict[str, Any]:
    return {
        "event_type": "PhaseStarted",
        "sequence": 2,
        "phase": _PHASE_ID,
        "visibility": "SYSTEM",
        "actor_player_id": None,
        "payload": {"phase_kind": "DAY_DISCUSSION", "day": 1, "round": 1},
    }


def _game_terminated_body() -> dict[str, Any]:
    return {
        "event_type": "GameTerminated",
        "sequence": 3,
        "phase": _PHASE_ID,
        "visibility": "PUBLIC",
        "actor_player_id": None,
        "payload": {"winner": "TOWN", "reason": "NO_MAFIA_ALIVE"},
    }


async def _create_game(session: AsyncSession) -> uuid.UUID:
    game = Game(
        gauntlet_id=None,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=_GAME_SEED,
        status=GAME_STATUS_RUNNING,
    )
    session.add(game)
    await session.flush()
    return game.id


async def _persist_bodies(
    session: AsyncSession,
    game_id: uuid.UUID,
    bodies: list[dict[str, Any]],
) -> None:
    log = EventLog()
    for body in bodies:
        stored = log.append(body)
        await events_repo.append_event(
            session,
            game_id=game_id,
            sequence=stored.sequence,
            event_type=stored.body["event_type"],
            phase=stored.body["phase"],
            visibility=stored.body["visibility"],
            actor_player_id=stored.body["actor_player_id"],
            payload=stored.body["payload"],
            prev_event_hash=stored.prev_event_hash,
            event_hash=stored.event_hash,
        )


async def test_rehydrate_benchmark_returns_none_for_no_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game_id = await _create_game(session)

    async with session_factory() as session:
        assert await rehydrate_benchmark_game(session, game_id) is None


async def test_rehydrate_benchmark_returns_none_for_only_game_created(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id_str = str(uuid.uuid4())
    async with session_factory() as session, session.begin():
        game_id = await _create_game(session)
        await _persist_bodies(session, game_id, [_game_created_body(game_id_str)])

    async with session_factory() as session:
        assert await rehydrate_benchmark_game(session, game_id) is None


async def test_rehydrate_benchmark_returns_none_for_terminal_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id_str = str(uuid.uuid4())
    bodies = [
        _game_created_body(game_id_str),
        _roles_assigned_body(),
        _phase_started_body(),
        _game_terminated_body(),
    ]
    async with session_factory() as session, session.begin():
        game_id = await _create_game(session)
        await _persist_bodies(session, game_id, bodies)

    async with session_factory() as session:
        assert await rehydrate_benchmark_game(session, game_id) is None


async def test_rehydrate_benchmark_rebuilds_resume_from_persisted_tail(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id_str = str(uuid.uuid4())
    bodies = [_game_created_body(game_id_str), _roles_assigned_body(), _phase_started_body()]
    async with session_factory() as session, session.begin():
        game_id = await _create_game(session)
        await _persist_bodies(session, game_id, bodies)

    async with session_factory() as session:
        resume = await rehydrate_benchmark_game(session, game_id)
        rows = await events_repo.list_events(session, game_id)

    assert resume is not None
    expected_state, expected_log = replay_state_from_rows(rows)
    assert resume.state == expected_state
    assert resume.event_log.head_hash == expected_log.head_hash
    assert resume.event_log.events[-1].sequence == 2
    assert resume.phase == _PHASE_ID
    assert resume.deadline_at is None
    assert resume.buffer_snapshot == {}


async def test_rehydrate_benchmark_raises_on_tampered_event_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id_str = str(uuid.uuid4())
    bodies = [_game_created_body(game_id_str), _roles_assigned_body(), _phase_started_body()]
    async with session_factory() as session, session.begin():
        game_id = await _create_game(session)
        await _persist_bodies(session, game_id, bodies)
        rows = await events_repo.list_events(session, game_id)
        rows[1].payload = {"assignments": []}

    async with session_factory() as session:
        with pytest.raises(ReplayHashMismatchError):
            await rehydrate_benchmark_game(session, game_id)
