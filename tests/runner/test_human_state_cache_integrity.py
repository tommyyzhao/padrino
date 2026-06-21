"""US-194: cache read path preserves tamper detection + complete cache adoption.

US-168's snapshot cache must not weaken the tamper detection the old full
verified replay provided: a tampered pre-head ``game_events`` body (reflected in
the cached prefix) must be detected on the human request read path, never
silently served. This also pins the ``human_game_runtime.upsert`` tri-state so a
cache-omitting partial update never wipes an existing cache.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.event_log import EventLog
from padrino.core.engine.events import EventAdapter
from padrino.core.engine.reducer import apply_event, initial_state
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import SeatKind
from padrino.core.rulesets import mini7_v1
from padrino.db.models import Game
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import human_game_runtime as runtime_repo
from padrino.runner.human_state_cache import (
    build_state_cache,
    resolve_current_human_state,
)

_GAME_SEED = "state-cache-integrity"
_PHASE = "DAY_1_VOTE"
_DEADLINE = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
_UPDATED = datetime(2026, 6, 21, 11, 59, tzinfo=UTC)


def _bodies(game_id: uuid.UUID) -> list[dict[str, Any]]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    assignments = [
        {
            "public_player_id": s.public_player_id,
            "seat_index": s.seat_index,
            "role": s.role.value,
            "faction": s.faction.value,
            "seat_kind": SeatKind.AI.value,
        }
        for s in seats
    ]
    bodies: list[dict[str, Any]] = [
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
            "payload": {"assignments": assignments},
        },
        {
            "event_type": "PhaseStarted",
            "sequence": 2,
            "phase": _PHASE,
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {"phase_kind": "DAY_VOTE", "day": 1, "round": 0},
        },
        {
            "event_type": "PublicMessageSubmitted",
            "sequence": 3,
            "phase": _PHASE,
            "visibility": "PUBLIC",
            "actor_player_id": "P04",
            "payload": {"text": "hello", "round_index": None},
        },
    ]
    return bodies


async def _seed_cached_game(session: AsyncSession) -> tuple[uuid.UUID, EventLog, Any]:
    game = Game(
        gauntlet_id=None,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=_GAME_SEED,
        status="RUNNING",
        current_phase=_PHASE,
    )
    session.add(game)
    await session.flush()

    state = initial_state()
    log = EventLog()
    for body in _bodies(game.id):
        stored = log.append(body)
        state = apply_event(state, EventAdapter.validate_python(body))
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
    game.event_hash_head = log.head_hash
    await session.flush()
    return game.id, log, state


@pytest.mark.asyncio
async def test_tampered_prefix_body_in_cache_is_not_served(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game_id, log, state = await _seed_cached_game(session)
        cache = build_state_cache(state, log)
        # Tamper a PRE-HEAD body in the cached prefix while leaving its stored
        # event_hash (and the cached head hash) untouched, so the head-vs-DB
        # check still passes. The recompute on load must reject this cache.
        cache["event_log"][1]["body"]["payload"]["assignments"][0]["role"] = "MAFIA"
        await runtime_repo.upsert(
            session,
            game_id=game_id,
            phase=_PHASE,
            deadline_at=_DEADLINE,
            buffer_snapshot={"actions": {}, "chat_holds": []},
            state_cache=cache,
            updated_at=_UPDATED,
        )

    async with session_factory() as session:
        resolved = await resolve_current_human_state(session, game_id)

    # The tampered cache is rejected; the read path falls back to the verified
    # full replay of the untampered DB log rather than serving tampered state.
    assert resolved is not None
    assert resolved.used_cache is False


@pytest.mark.asyncio
async def test_upsert_preserves_existing_cache_when_omitted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game_id, log, state = await _seed_cached_game(session)
        cache = build_state_cache(state, log)
        await runtime_repo.upsert(
            session,
            game_id=game_id,
            phase=_PHASE,
            deadline_at=_DEADLINE,
            buffer_snapshot={"actions": {}, "chat_holds": []},
            state_cache=cache,
            updated_at=_UPDATED,
        )

    # A partial update that does not manage the cache (omits state_cache).
    async with session_factory() as session, session.begin():
        await runtime_repo.upsert(
            session,
            game_id=game_id,
            phase=_PHASE,
            deadline_at=_DEADLINE,
            buffer_snapshot={"actions": {}, "chat_holds": ["new"]},
            updated_at=datetime(2026, 6, 21, 12, 5, tzinfo=UTC),
        )

    async with session_factory() as session:
        row = await runtime_repo.get(session, game_id)
        assert row is not None
        assert row.state_cache is not None
        assert row.state_cache["event_hash_head"] == log.head_hash
        # The partial update still applied to the non-cache fields.
        assert row.buffer_snapshot == {"actions": {}, "chat_holds": ["new"]}


@pytest.mark.asyncio
async def test_upsert_explicit_none_clears_cache(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game_id, log, state = await _seed_cached_game(session)
        await runtime_repo.upsert(
            session,
            game_id=game_id,
            phase=_PHASE,
            deadline_at=_DEADLINE,
            buffer_snapshot={"actions": {}, "chat_holds": []},
            state_cache=build_state_cache(state, log),
            updated_at=_UPDATED,
        )

    async with session_factory() as session, session.begin():
        await runtime_repo.upsert(
            session,
            game_id=game_id,
            phase=_PHASE,
            deadline_at=_DEADLINE,
            buffer_snapshot={"actions": {}, "chat_holds": []},
            state_cache=None,
            updated_at=datetime(2026, 6, 21, 12, 5, tzinfo=UTC),
        )

    async with session_factory() as session:
        row = await runtime_repo.get(session, game_id)
        assert row is not None
        assert row.state_cache is None
