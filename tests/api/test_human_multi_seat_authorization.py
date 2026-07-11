"""US-331 multi-human action/chat authorization regressions."""

from __future__ import annotations

import asyncio
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
from padrino.api.auth import RateLimiter
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import LeagueKind, LobbySeatKind, SeatKind
from padrino.core.rulesets import mini7_v1
from padrino.db.models import (
    Game,
    GameSeat,
    HumanActionSubmission,
    HumanChatSubmission,
    League,
    Lobby,
    LobbyMember,
    LobbySeat,
)
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import human_principals as principals_repo

_GAME_SEED = "us331-api-multi-human"
_VOTE_PHASE = "DAY_1_VOTE"
_CHAT_PHASE = "DAY_1_DISCUSSION_ROUND_1"
_HUMAN_SEATS = ("P03", "P04")


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _guest_token(resp_headers: object) -> str:
    jar = SimpleCookie()
    for raw in resp_headers.get_list("set-cookie"):  # type: ignore[attr-defined]
        if raw.startswith(f"{HUMAN_SESSION_COOKIE}="):
            jar.load(raw)
    return jar[HUMAN_SESSION_COOKIE].value


async def _consenting_guest(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, uuid.UUID]:
    response = await client.post("/human/guest")
    assert response.status_code == 201, response.text
    token = _guest_token(response.headers)
    consent = await client.post("/human/consent", cookies={HUMAN_SESSION_COOKIE: token})
    assert consent.status_code == 201, consent.text
    async with session_factory() as session:
        session_row = await principals_repo.get_session_by_token(session, token)
        assert session_row is not None
        return token, session_row.principal_id


def _phase_bodies(game_id: uuid.UUID, phase: str) -> list[dict[str, Any]]:
    assignments = [
        {
            "public_player_id": seat.public_player_id,
            "seat_index": seat.seat_index,
            "role": seat.role.value,
            "faction": seat.faction.value,
            "seat_kind": (
                SeatKind.HUMAN.value if seat.public_player_id in _HUMAN_SEATS else SeatKind.AI.value
            ),
        }
        for seat in assign_roles(_GAME_SEED, mini7_v1)
    ]
    phase_kind = "DAY_VOTE" if phase == _VOTE_PHASE else "DAY_DISCUSSION"
    round_index = 0 if phase == _VOTE_PHASE else 1
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
            "payload": {"assignments": assignments},
        },
        {
            "event_type": "PhaseStarted",
            "sequence": 2,
            "phase": phase,
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {"phase_kind": phase_kind, "day": 1, "round": round_index},
        },
    ]


async def _persist_phase_log(session: AsyncSession, game_id: uuid.UUID, *, phase: str) -> None:
    log = EventLog()
    for body in _phase_bodies(game_id, phase):
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


async def _seed_same_lobby_game(
    session: AsyncSession,
    *,
    principal_ids: tuple[uuid.UUID, uuid.UUID],
    phase: str,
) -> uuid.UUID:
    league = League(
        name=f"US331 Humans {uuid.uuid4()}",
        ruleset_id=mini7_v1.RULESET_ID,
        ranked=False,
        kind=LeagueKind.HUMANS_INCLUDED.value,
    )
    game = Game(
        gauntlet_id=None,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=_GAME_SEED,
        status="RUNNING",
    )
    session.add_all([league, game])
    await session.flush()
    lobby = Lobby(
        ruleset_id=mini7_v1.RULESET_ID,
        identity_mode="ANONYMOUS",
        invite_token=f"us331-{uuid.uuid4()}",
        theme_pack_id=None,
        stakes="CASUAL",
        lobby_seed=f"us331-lobby-{uuid.uuid4()}",
        host_principal_id=principal_ids[0],
        league_id=league.id,
        game_id=game.id,
        status="LAUNCHED",
    )
    session.add(lobby)
    await session.flush()
    members = [
        LobbyMember(
            lobby_id=lobby.id,
            principal_id=principal_ids[0],
            is_host=True,
            ready=True,
            last_seen_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
        ),
        LobbyMember(
            lobby_id=lobby.id,
            principal_id=principal_ids[1],
            is_host=False,
            ready=True,
            last_seen_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
        ),
    ]
    session.add_all(members)
    await session.flush()
    await _persist_phase_log(session, game.id, phase=phase)

    principal_by_seat = dict(zip(_HUMAN_SEATS, principal_ids, strict=True))
    member_by_seat = dict(zip(_HUMAN_SEATS, members, strict=True))
    for seat in assign_roles(_GAME_SEED, mini7_v1):
        is_human = seat.public_player_id in _HUMAN_SEATS
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id=seat.public_player_id,
                seat_index=seat.seat_index,
                agent_build_id=None,
                seat_kind=SeatKind.HUMAN.value if is_human else SeatKind.AI.value,
                occupant_principal_id=principal_by_seat.get(seat.public_player_id),
                role=seat.role.value,
                faction=seat.faction.value,
                alive=True,
            )
        )
        session.add(
            LobbySeat(
                lobby_id=lobby.id,
                seat_index=seat.seat_index,
                seat_kind=LobbySeatKind.HUMAN.value if is_human else LobbySeatKind.AI.value,
                member_id=member_by_seat[seat.public_player_id].id if is_human else None,
                agent_build_id=None,
            )
        )
    await session.flush()
    return game.id


@pytest.mark.asyncio
async def test_same_lobby_humans_submit_simultaneous_actions_to_own_seats(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token_a, principal_a = await _consenting_guest(client, session_factory)
    token_b, principal_b = await _consenting_guest(client, session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_same_lobby_game(
            session,
            principal_ids=(principal_a, principal_b),
            phase=_VOTE_PHASE,
        )

    body_a = {"action": {"type": "VOTE", "target": "P04"}, "idempotency_key": "same-key"}
    body_b = {"action": {"type": "VOTE", "target": "P03"}, "idempotency_key": "same-key"}
    first, second = await asyncio.gather(
        client.post(
            f"/human/games/{game_id}/actions",
            json=body_a,
            cookies={HUMAN_SESSION_COOKIE: token_a},
        ),
        client.post(
            f"/human/games/{game_id}/actions",
            json=body_b,
            cookies={HUMAN_SESSION_COOKIE: token_b},
        ),
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["public_player_id"] == "P03"
    assert second.json()["public_player_id"] == "P04"

    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(HumanActionSubmission)
                    .where(HumanActionSubmission.game_id == game_id)
                    .order_by(HumanActionSubmission.public_player_id)
                )
            ).scalars()
        )
    assert [(row.public_player_id, row.target, row.idempotency_key) for row in rows] == [
        ("P03", "P04", "same-key"),
        ("P04", "P03", "same-key"),
    ]


@pytest.mark.asyncio
async def test_same_lobby_humans_submit_chat_to_own_holds_only(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token_a, principal_a = await _consenting_guest(client, session_factory)
    token_b, principal_b = await _consenting_guest(client, session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_same_lobby_game(
            session,
            principal_ids=(principal_a, principal_b),
            phase=_CHAT_PHASE,
        )

    first, second = await asyncio.gather(
        client.post(
            f"/human/games/{game_id}/chat",
            json={"channel": "PUBLIC", "text": "p03 owns this", "idempotency_key": "chat"},
            cookies={HUMAN_SESSION_COOKIE: token_a},
        ),
        client.post(
            f"/human/games/{game_id}/chat",
            json={"channel": "PUBLIC", "text": "p04 owns this", "idempotency_key": "chat"},
            cookies={HUMAN_SESSION_COOKIE: token_b},
        ),
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["public_player_id"] == "P03"
    assert second.json()["public_player_id"] == "P04"

    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(HumanChatSubmission)
                    .where(HumanChatSubmission.game_id == game_id)
                    .order_by(HumanChatSubmission.public_player_id)
                )
            ).scalars()
        )
    assert [(row.public_player_id, row.raw_text, row.idempotency_key) for row in rows] == [
        ("P03", "p03 owns this", "chat"),
        ("P04", "p04 owns this", "chat"),
    ]
