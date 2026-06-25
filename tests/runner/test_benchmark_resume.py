"""US-250: benchmark games resume from persisted event tails."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.event_log import EventLog
from padrino.core.engine.replay import replay_event_log
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, Role
from padrino.core.rulesets import mini7_v1
from padrino.db.game_status import GAME_STATUS_RUNNING
from padrino.db.models import Game, GameEvent
from padrino.db.repositories import events as events_repo
from padrino.llm.mock import DeterministicMockAdapter
from padrino.runner import game_runner as game_runner_module
from padrino.runner.benchmark_durability import rehydrate_benchmark_game
from padrino.runner.game_runner import (
    GameConfig,
    GameOutcome,
    GamePersistence,
    drive_game_loop,
    run_game,
)
from tests.conftest import make_town_win_script

_GAME_SEED = "us250-benchmark-resume-seed"
_RESUME_PHASE = "DAY_1_DISCUSSION_ROUND_1"
_MID_PHASE = "DAY_1_VOTE"
_SUBMISSION_EVENT_TYPES = frozenset(
    {
        "PublicMessageSubmitted",
        "PrivateMessageSubmitted",
        "VoteSubmitted",
        "MafiaKillVoteSubmitted",
        "ProtectSubmitted",
        "InvestigateSubmitted",
        "RoleblockSubmitted",
        "FrameSubmitted",
        "TrackSubmitted",
        "WatchSubmitted",
        "CleanSubmitted",
        "SerialKillSubmitted",
    }
)


def _game_created_body(game_id: uuid.UUID) -> dict[str, Any]:
    return {
        "event_type": "GameCreated",
        "sequence": 0,
        "phase": "SETUP",
        "visibility": "SYSTEM",
        "actor_player_id": None,
        "payload": {
            "ruleset_id": mini7_v1.RULESET_ID,
            "game_id": str(game_id),
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
        "phase": _RESUME_PHASE,
        "visibility": "SYSTEM",
        "actor_player_id": None,
        "payload": {"phase_kind": "DAY_DISCUSSION", "day": 1, "round": 1},
    }


async def _persist_bodies(
    session: AsyncSession,
    game_id: uuid.UUID,
    bodies: list[dict[str, Any]],
) -> None:
    event_log = EventLog()
    for body in bodies:
        stored = event_log.append(body)
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


def _town_win_adapter() -> DeterministicMockAdapter:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return DeterministicMockAdapter(
        make_town_win_script(
            mafia_ids=mafia,
            town_ids=town,
            doctor_id=doctor,
            detective_id=detective,
        )
    )


def _event_hash_chain_digest(outcome: GameOutcome) -> str:
    joined = "\n".join(stored.event_hash for stored in outcome.event_log.events)
    return hashlib.sha256(joined.encode()).hexdigest()


class _SimulatedCrashAfterPhaseStart(Exception):
    """Raised by the test hook after the phase start row is durable."""


class _SimulatedCrashAfterPartialSubmissions(Exception):
    """Raised after a strict prefix of a phase's submission rows is durable."""


async def test_run_game_resume_continues_persisted_phase_without_duplicate_setup(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game = Game(
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_GAME_SEED,
            status=GAME_STATUS_RUNNING,
        )
        session.add(game)
        await session.flush()
        game_id = game.id
        await _persist_bodies(
            session,
            game_id,
            [_game_created_body(game_id), _roles_assigned_body(), _phase_started_body()],
        )

    async with session_factory() as session:
        resume = await rehydrate_benchmark_game(session, game_id)
    assert resume is not None
    assert resume.phase == _RESUME_PHASE
    prefix_hash = resume.event_log.head_hash

    outcome = await run_game(
        GameConfig(game_id=str(game_id), game_seed=_GAME_SEED, timeout_s=1.0),
        _town_win_adapter(),
        ranked=False,
        persistence=GamePersistence(session_factory=session_factory, game_id=game_id),
        resume=resume,
    )

    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(GameEvent)
                    .where(GameEvent.game_id == game_id)
                    .order_by(GameEvent.sequence)
                )
            )
            .scalars()
            .all()
        )

    assert outcome.final_state.terminal_result == "TOWN"
    assert [row.sequence for row in rows] == list(range(len(rows)))
    assert sum(row.event_type == "GameCreated" for row in rows) == 1
    assert sum(row.event_type == "RolesAssigned" for row in rows) == 1
    assert sum(row.event_type == "PhaseStarted" and row.phase == _RESUME_PHASE for row in rows) == 1
    assert rows[3].prev_event_hash == prefix_hash
    assert rows[3].event_type == "PhaseResolved"
    replayed = replay_event_log(outcome.event_log.events)
    assert replayed.head_hash == outcome.event_log.head_hash


async def test_interrupted_and_resumed_benchmark_game_matches_uninterrupted_hash_chain(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game = Game(
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_GAME_SEED,
            status=GAME_STATUS_RUNNING,
        )
        session.add(game)
        await session.flush()
        game_id = game.id

    config = GameConfig(game_id=str(game_id), game_seed=_GAME_SEED, timeout_s=1.0)
    reference = await run_game(config, _town_win_adapter(), ranked=False)
    reference_final_hash = reference.event_log.events[-1].event_hash
    reference_chain_digest = _event_hash_chain_digest(reference)

    crashed = False

    async def crash_after_phase_start(
        _state: Any,
        _event_log: EventLog,
        phase_id: str,
    ) -> None:
        nonlocal crashed
        if not crashed and phase_id == _RESUME_PHASE:
            crashed = True
            raise _SimulatedCrashAfterPhaseStart

    with pytest.raises(_SimulatedCrashAfterPhaseStart):
        await drive_game_loop(
            config,
            _town_win_adapter(),
            ranked=False,
            persistence=GamePersistence(session_factory=session_factory, game_id=game_id),
            phase_snapshot=crash_after_phase_start,
        )

    async with session_factory() as session:
        resume = await rehydrate_benchmark_game(session, game_id)
    assert resume is not None
    assert resume.phase == _RESUME_PHASE

    resumed = await run_game(
        config,
        _town_win_adapter(),
        ranked=False,
        persistence=GamePersistence(session_factory=session_factory, game_id=game_id),
        resume=resume,
    )

    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(GameEvent)
                    .where(GameEvent.game_id == game_id)
                    .order_by(GameEvent.sequence)
                )
            )
            .scalars()
            .all()
        )

    assert resumed.final_state.terminal_result == reference.final_state.terminal_result
    assert resumed.event_log.events[-1].event_hash == reference_final_hash
    assert _event_hash_chain_digest(resumed) == reference_chain_digest
    assert [row.sequence for row in rows] == list(range(len(rows)))
    assert sum(row.event_type == "GameCreated" for row in rows) == 1
    assert sum(row.event_type == "PhaseStarted" and row.phase == _RESUME_PHASE for row in rows) == 1

    persisted_hashes = tuple(row.event_hash for row in rows)
    resumed_hashes = tuple(stored.event_hash for stored in resumed.event_log.events)
    reference_hashes = tuple(stored.event_hash for stored in reference.event_log.events)
    assert persisted_hashes == resumed_hashes == reference_hashes
    assert replay_event_log(resumed.event_log.events).head_hash == reference_final_hash


async def test_mid_phase_crash_truncates_partial_submissions_and_matches_reference(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session, session.begin():
        game = Game(
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_GAME_SEED,
            status=GAME_STATUS_RUNNING,
        )
        session.add(game)
        await session.flush()
        game_id = game.id

    config = GameConfig(game_id=str(game_id), game_seed=_GAME_SEED, timeout_s=1.0)
    reference = await run_game(config, _town_win_adapter(), ranked=False)
    reference_final_hash = reference.event_log.events[-1].event_hash
    reference_chain_digest = _event_hash_chain_digest(reference)

    original_persist_stored_event = game_runner_module._persist_stored_event
    durable_mid_phase_submissions = 0

    async def crash_after_second_vote_submission(
        persistence: GamePersistence,
        stored: Any,
    ) -> None:
        nonlocal durable_mid_phase_submissions
        await original_persist_stored_event(persistence, stored)
        if (
            stored.body.get("phase") == _MID_PHASE
            and stored.body.get("event_type") == "VoteSubmitted"
        ):
            durable_mid_phase_submissions += 1
            if durable_mid_phase_submissions == 2:
                raise _SimulatedCrashAfterPartialSubmissions

    monkeypatch.setattr(
        game_runner_module,
        "_persist_stored_event",
        crash_after_second_vote_submission,
    )
    with pytest.raises(_SimulatedCrashAfterPartialSubmissions):
        await drive_game_loop(
            config,
            _town_win_adapter(),
            ranked=False,
            persistence=GamePersistence(session_factory=session_factory, game_id=game_id),
        )
    monkeypatch.setattr(
        game_runner_module,
        "_persist_stored_event",
        original_persist_stored_event,
    )
    assert 0 < durable_mid_phase_submissions < mini7_v1.PLAYER_COUNT

    async with session_factory() as session:
        pre_rehydrate_rows = await events_repo.list_events(session, game_id)
    assert (
        sum(
            row.event_type == "VoteSubmitted" and row.phase == _MID_PHASE
            for row in pre_rehydrate_rows
        )
        == 2
    )

    async with session_factory() as session:
        resume = await rehydrate_benchmark_game(session, game_id)
        truncated_rows = await events_repo.list_events(session, game_id)
    assert resume is not None
    assert resume.phase == _MID_PHASE
    assert truncated_rows[-1].event_type == "PhaseStarted"
    assert truncated_rows[-1].phase == _MID_PHASE
    assert resume.event_log.head_hash == truncated_rows[-1].event_hash
    assert not any(
        row.event_type == "VoteSubmitted" and row.phase == _MID_PHASE for row in truncated_rows
    )

    resumed = await run_game(
        config,
        _town_win_adapter(),
        ranked=False,
        persistence=GamePersistence(session_factory=session_factory, game_id=game_id),
        resume=resume,
    )

    async with session_factory() as session:
        rows = await events_repo.list_events(session, game_id)

    assert resumed.final_state.terminal_result == reference.final_state.terminal_result
    assert resumed.event_log.events[-1].event_hash == reference_final_hash
    assert _event_hash_chain_digest(resumed) == reference_chain_digest
    submission_keys = [
        (row.phase, row.actor_player_id)
        for row in rows
        if row.event_type in _SUBMISSION_EVENT_TYPES
    ]
    assert len(submission_keys) == len(set(submission_keys))
    persisted_hashes = tuple(row.event_hash for row in rows)
    resumed_hashes = tuple(stored.event_hash for stored in resumed.event_log.events)
    reference_hashes = tuple(stored.event_hash for stored in reference.event_log.events)
    assert persisted_hashes == resumed_hashes == reference_hashes
    assert replay_event_log(resumed.event_log.events).head_hash == reference_final_hash
