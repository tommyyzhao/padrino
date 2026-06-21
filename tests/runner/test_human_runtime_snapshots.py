"""US-161: durable human runtime snapshots are wired into the worker lane.

The US-131 table/repository can store a runtime row, but the production lane
must also write and consume those rows:

- the human tick loop upserts phase/deadline/buffer snapshots as it runs;
- worker startup calls rehydration and passes the rebuilt state to the resumed
  game executor;
- a game restarted mid-phase continues from the existing ``PhaseStarted`` row
  rather than duplicating setup or replaying the phase start.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import padrino.runner.human_lane as human_lane
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import ActionType, SeatKind
from padrino.core.rulesets import mini7_v1
from padrino.db.models import (
    Game,
    GameEvent,
    GameSeat,
    HumanActionSubmission,
    HumanChatSubmission,
)
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import human_game_runtime as runtime_repo
from padrino.llm.adapter import LlmAdapter
from padrino.llm.mock import DeterministicMockAdapter
from padrino.runner.game_runner import GameConfig, GamePersistence, GameResume
from padrino.runner.human_durability import rehydrate_active_human_games
from padrino.runner.human_lane import _default_human_game_executor, run_human_lane
from padrino.settings import Settings
from tests.conftest import make_villager_script, mini7_phase_ids

_GAME_SEED = "us161-runtime-snapshot"
_HUMAN_SEAT = "P01"
_VOTE_PHASE = "DAY_1_VOTE"
_DEADLINE = datetime(2026, 6, 20, 12, 30, tzinfo=UTC)
_UPDATED = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


def _assignments() -> list[dict[str, str | int]]:
    return [
        {
            "public_player_id": seat.public_player_id,
            "seat_index": seat.seat_index,
            "role": seat.role.value,
            "faction": seat.faction.value,
            "seat_kind": (
                SeatKind.HUMAN.value if seat.public_player_id == _HUMAN_SEAT else SeatKind.AI.value
            ),
        }
        for seat in assign_roles(_GAME_SEED, mini7_v1)
    ]


def _phase_started_bodies(game_id: uuid.UUID, phase: str) -> list[dict[str, Any]]:
    return [
        {
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
        },
        {
            "event_type": "RolesAssigned",
            "sequence": 1,
            "phase": "SETUP",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {"assignments": _assignments()},
        },
        {
            "event_type": "PhaseStarted",
            "sequence": 2,
            "phase": phase,
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {"phase_kind": "DAY_VOTE", "day": 1, "round": 0},
        },
    ]


async def _persist_bodies(
    session: AsyncSession, game_id: uuid.UUID, bodies: list[dict[str, Any]]
) -> None:
    log = EventLog()
    for body in bodies:
        stored = log.append(body)
        await events_repo.append_event(
            session,
            game_id=game_id,
            sequence=stored.sequence,
            event_type=body["event_type"],
            phase=body["phase"],
            visibility=body["visibility"],
            actor_player_id=body["actor_player_id"],
            payload=body["payload"],
            prev_event_hash=stored.prev_event_hash,
            event_hash=stored.event_hash,
        )


async def _add_seats(session: AsyncSession, game_id: uuid.UUID) -> None:
    for seat in assign_roles(_GAME_SEED, mini7_v1):
        is_human = seat.public_player_id == _HUMAN_SEAT
        session.add(
            GameSeat(
                game_id=game_id,
                public_player_id=seat.public_player_id,
                seat_index=seat.seat_index,
                agent_build_id=None,
                seat_kind=SeatKind.HUMAN.value if is_human else SeatKind.AI.value,
                role=seat.role.value,
                faction=seat.faction.value,
                alive=True,
            )
        )


async def _seed_game_row(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: str = "RUNNING",
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        game = Game(
            gauntlet_id=None,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_GAME_SEED,
            status=status,
        )
        session.add(game)
        await session.flush()
        await _add_seats(session, game.id)
        return game.id


async def _seed_running_vote_phase(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    buffer_snapshot: dict[str, object] | None = None,
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        game = Game(
            gauntlet_id=None,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_GAME_SEED,
            status="RUNNING",
        )
        session.add(game)
        await session.flush()
        await _persist_bodies(session, game.id, _phase_started_bodies(game.id, _VOTE_PHASE))
        await _add_seats(session, game.id)
        await runtime_repo.upsert(
            session,
            game_id=game.id,
            phase=_VOTE_PHASE,
            deadline_at=_DEADLINE,
            buffer_snapshot=buffer_snapshot or {},
            updated_at=_UPDATED,
        )
        return game.id


async def _event_rows(
    session_factory: async_sessionmaker[AsyncSession], game_id: uuid.UUID
) -> list[GameEvent]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(GameEvent)
                    .where(GameEvent.game_id == game_id)
                    .order_by(GameEvent.sequence)
                )
            ).scalars()
        )


def _draw_adapter() -> DeterministicMockAdapter:
    seat_ids = [seat.public_player_id for seat in assign_roles(_GAME_SEED, mini7_v1)]
    return DeterministicMockAdapter(make_villager_script(seat_ids, mini7_phase_ids()))


def _fast_settings() -> Settings:
    return Settings(
        padrino_human_phase_deadline_seconds=0.01,
        padrino_human_release_delay_seconds=0.0,
        padrino_human_global_lobby_cost_breaker_usd=10_000.0,
    )


async def test_runtime_snapshot_captures_buffer_metadata_without_chat_text(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id = await _seed_game_row(session_factory)
    raw_chat = "raw chat must stay out of runtime snapshot"
    cleaned_chat = "cleaned chat must stay out of runtime snapshot"
    phase = "NIGHT_0_MAFIA_INTRO"

    async with session_factory() as session, session.begin():
        session.add(
            HumanActionSubmission(
                game_id=game_id,
                public_player_id=_HUMAN_SEAT,
                phase=phase,
                idempotency_key="action-key",
                action_type=ActionType.NOOP.value,
                target=None,
                created_at=_UPDATED,
            )
        )
        session.add(
            HumanChatSubmission(
                game_id=game_id,
                public_player_id=_HUMAN_SEAT,
                phase=phase,
                channel="PRIVATE",
                idempotency_key="chat-key",
                raw_text=raw_chat,
                cleaned_text=cleaned_chat,
                status="HELD",
                created_at=_UPDATED,
            )
        )

    await human_lane.persist_human_runtime_snapshot(
        session_factory,
        game_id=game_id,
        phase=phase,
        deadline_at=_DEADLINE,
        updated_at=_UPDATED,
        event_log=EventLog(),
    )

    async with session_factory() as session:
        runtime = await runtime_repo.get(session, game_id)

    assert runtime is not None
    assert runtime.phase == phase
    deadline = runtime.deadline_at
    assert deadline is not None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    assert deadline == _DEADLINE
    buffer = runtime.buffer_snapshot
    assert buffer["actions"] == {
        _HUMAN_SEAT: {
            "action_type": ActionType.NOOP.value,
            "target": None,
            "idempotency_key": "action-key",
            "created_at": _UPDATED.isoformat(),
        }
    }
    assert buffer["chat_holds"] == [
        {
            "public_player_id": _HUMAN_SEAT,
            "channel": "PRIVATE",
            "status": "HELD",
            "idempotency_key": "chat-key",
            "created_at": _UPDATED.isoformat(),
            "ready_for_release": True,
        }
    ]
    assert raw_chat not in str(buffer)
    assert cleaned_chat not in str(buffer)


async def test_resumed_human_game_continues_existing_phase_without_duplicate_setup(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id = await _seed_running_vote_phase(session_factory)
    rehydrated = (await rehydrate_active_human_games(session_factory))[0]
    executor = _default_human_game_executor(_fast_settings())

    await executor(
        GameConfig(
            game_id=str(game_id),
            game_seed=_GAME_SEED,
            ruleset_id=mini7_v1.RULESET_ID,
            timeout_s=0.01,
        ),
        GamePersistence(
            session_factory=session_factory,
            game_id=game_id,
            agent_builds={},
            league_id=None,
            resume=GameResume(
                state=rehydrated.state,
                event_log=rehydrated.event_log,
                phase=rehydrated.phase,
                deadline_at=rehydrated.deadline_at,
                buffer_snapshot=rehydrated.buffer_snapshot,
            ),
        ),
        _draw_adapter(),
    )

    rows = await _event_rows(session_factory, game_id)
    assert [row.sequence for row in rows] == list(range(len(rows)))
    assert sum(row.event_type == "GameCreated" for row in rows) == 1
    assert sum(row.event_type == "RolesAssigned" for row in rows) == 1
    assert sum(row.event_type == "PhaseStarted" and row.phase == _VOTE_PHASE for row in rows) == 1
    assert any(row.event_type == "VoteSubmitted" and row.phase == _VOTE_PHASE for row in rows)

    async with session_factory() as session:
        game = await session.get(Game, game_id)
        runtime = await runtime_repo.get(session, game_id)
    assert game is not None
    assert game.status == "COMPLETED"
    assert runtime is not None
    assert runtime.phase != _VOTE_PHASE
    assert runtime.buffer_snapshot == {"actions": {}, "chat_holds": []}


async def test_human_lane_rehydrates_running_games_on_startup(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    buffer: dict[str, object] = {
        "actions": {_HUMAN_SEAT: {"action_type": ActionType.ABSTAIN.value}}
    }
    await _seed_running_vote_phase(session_factory, buffer_snapshot=buffer)
    expected = (await rehydrate_active_human_games(session_factory))[0]
    calls: list[async_sessionmaker[AsyncSession]] = []

    async def fake_rehydrate(
        startup_session_factory: async_sessionmaker[AsyncSession],
    ) -> list[object]:
        calls.append(startup_session_factory)
        return [expected]

    monkeypatch.setattr(human_lane, "rehydrate_active_human_games", fake_rehydrate)

    stop = asyncio.Event()
    seen: list[GameResume | None] = []

    async def executor(
        config: GameConfig, persistence: GamePersistence, adapter: LlmAdapter
    ) -> None:
        seen.append(persistence.resume)
        stop.set()

    await run_human_lane(
        session_factory,
        concurrency=1,
        stop_event=stop,
        game_executor=executor,
        poll_interval_s=0.01,
        settings=_fast_settings(),
    )

    assert calls and calls[0] is session_factory
    assert len(seen) == 1
    resume = seen[0]
    assert resume is not None
    assert resume.phase == _VOTE_PHASE
    assert resume.deadline_at == _DEADLINE
    assert resume.buffer_snapshot == buffer
    assert resume.event_log.head_hash == expected.event_log.head_hash
