"""US-168: human request paths use the runtime state cache, not full replay.

Long human games can accumulate large hash-chained logs. Action POST, chat POST,
and seat-observation polling must not read and re-fold the entire log on every
request; they should start from the durable runtime cache and read only events
committed after that cached head.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.events import EventAdapter
from padrino.core.engine.reducer import apply_event, initial_state
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import SeatKind
from padrino.core.rulesets import mini7_v1
from padrino.db.models import Game, GameEvent, GameSeat, Principal
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import human_game_runtime as runtime_repo
from padrino.runner.human_state_cache import build_state_cache

_GAME_SEED = "state-cache-long-game"
_PHASE = "DAY_1_VOTE"
_HUMAN_SEAT = "P03"
_LONG_PUBLIC_EVENT_COUNT = 250
_DEADLINE = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _guest_token(resp_headers: object) -> str:
    jar = SimpleCookie()
    for raw in resp_headers.get_list("set-cookie"):  # type: ignore[attr-defined]
        if raw.startswith(f"{HUMAN_SESSION_COOKIE}="):
            jar.load(raw)
    return jar[HUMAN_SESSION_COOKIE].value


async def _consenting_guest(client: AsyncClient) -> str:
    resp = await client.post("/human/guest")
    assert resp.status_code == 201
    token = _guest_token(resp.headers)
    consent = await client.post("/human/consent", cookies={HUMAN_SESSION_COOKIE: token})
    assert consent.status_code == 201
    return token


async def _principal_id_for_token(
    session_factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    async with session_factory() as session:
        principal = (await session.execute(select(Principal))).scalars().one()
    return principal.id


def _long_vote_phase_bodies(game_id: uuid.UUID) -> list[dict[str, Any]]:
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
    ]
    for idx in range(_LONG_PUBLIC_EVENT_COUNT):
        bodies.append(
            {
                "event_type": "PublicMessageSubmitted",
                "sequence": len(bodies),
                "phase": _PHASE,
                "visibility": "PUBLIC",
                "actor_player_id": "P04",
                "payload": {"text": f"cached context message {idx}", "round_index": None},
            }
        )
    return bodies


async def _seed_long_cached_game(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> uuid.UUID:
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
    for body in _long_vote_phase_bodies(game.id):
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

    for seat in assign_roles(_GAME_SEED, mini7_v1):
        is_human = seat.public_player_id == _HUMAN_SEAT
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id=seat.public_player_id,
                seat_index=seat.seat_index,
                agent_build_id=None,
                seat_kind=SeatKind.HUMAN.value if is_human else SeatKind.AI.value,
                role=seat.role.value,
                faction=seat.faction.value,
                alive=True,
                occupant_principal_id=principal_id if is_human else None,
            )
        )

    await runtime_repo.upsert(
        session,
        game_id=game.id,
        phase=_PHASE,
        deadline_at=_DEADLINE,
        buffer_snapshot={"actions": {}, "chat_holds": []},
        state_cache=build_state_cache(state, log),
        updated_at=datetime(2026, 6, 21, 11, 59, tzinfo=UTC),
    )
    await session.flush()
    return game.id


def _parse_sse_frames(body: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if block.startswith("data:"):
            frames.append(json.loads(block[len("data:") :].strip()))
    return frames


@pytest.mark.asyncio
async def test_human_requests_use_cached_state_and_bounded_tail_reads(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _consenting_guest(client)
    principal_id = await _principal_id_for_token(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_long_cached_game(session, principal_id=principal_id)

    async def fail_full_replay(*_args: object, **_kwargs: object) -> list[GameEvent]:
        raise AssertionError("human request path performed a full event-log read")

    original_after = events_repo.list_events_after
    tail_reads: list[int] = []

    async def count_tail_read(
        session: AsyncSession,
        game_id: uuid.UUID,
        *,
        after_sequence: int,
    ) -> list[GameEvent]:
        tail_reads.append(after_sequence)
        return await original_after(session, game_id, after_sequence=after_sequence)

    monkeypatch.setattr(events_repo, "list_events", fail_full_replay)
    monkeypatch.setattr(events_repo, "list_events_after", count_tail_read)

    action = await client.post(
        f"/human/games/{game_id}/actions",
        json={"action": {"type": "VOTE", "target": "P04"}, "idempotency_key": "a1"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert action.status_code == 200

    chat = await client.post(
        f"/human/games/{game_id}/chat",
        json={"channel": "PUBLIC", "text": "cached path chat", "idempotency_key": "c1"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert chat.status_code == 200

    observation = await client.get(
        f"/human/games/{game_id}/observation/stream",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert observation.status_code == 200
    frames = _parse_sse_frames(observation.text)
    assert any(frame["type"] == "observation" for frame in frames)

    cached_head = 2 + _LONG_PUBLIC_EVENT_COUNT
    assert tail_reads == [cached_head, cached_head, cached_head]
