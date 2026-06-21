"""Authenticated POST chat channel into the buffered hold (US-135/US-159).

A human submits a public/private chat message over an authenticated POST. The
message enters the buffer *hold* and is gated by the block-before-release
moderation hook (US-140 lands the verdict; here it is a stub-pass gate) before
any release. On release the raw text is routed to the out-of-band sidecar
(US-123), never inline in a hash-chained payload. Covers:

* over-limit message rejected (422);
* held-after-moderation (stub-pass): the hold row stays HELD after POST and no
  sidecar row exists until the human-aware tick releases the phase;
* idempotent retry: a retry with the same key never inserts a second hold row;
* a stray structured ``action`` field is a 422 (chat firewall — the chat
  channel accepts ONLY chat);
* the channel requires a session (401) and consent (412).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
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
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import SeatKind
from padrino.core.rulesets import mini7_v1
from padrino.db.models import (
    Game,
    GameSeat,
    HumanChatMessage,
    HumanChatSubmission,
    Principal,
)
from padrino.db.repositories import events as events_repo

_GAME_SEED = "chat-seed"
_PHASE = "DAY_1_DISCUSSION_ROUND_1"
_HUMAN_SEAT = "P03"


def _discussion_phase_bodies(human_seat: str) -> list[dict[str, Any]]:
    """A non-terminal human game positioned at DAY_1_DISCUSSION (chat is legal)."""
    seats = assign_roles(_GAME_SEED, mini7_v1)
    assignments = [
        {
            "public_player_id": s.public_player_id,
            "seat_index": s.seat_index,
            "role": s.role.value,
            "faction": s.faction.value,
            "seat_kind": (
                SeatKind.HUMAN.value if s.public_player_id == human_seat else SeatKind.AI.value
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
                "game_id": "g",
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
            "payload": {"phase_kind": "DAY_DISCUSSION", "day": 1, "round": 1},
        },
    ]


async def _seed_human_game(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID | None,
    human_seat: str = _HUMAN_SEAT,
) -> uuid.UUID:
    """Persist a non-terminal human game at DAY_1_DISCUSSION and return its id."""
    game = Game(
        gauntlet_id=None,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=_GAME_SEED,
        status="RUNNING",
    )
    session.add(game)
    await session.flush()

    log = EventLog()
    for body in _discussion_phase_bodies(human_seat):
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

    for s in assign_roles(_GAME_SEED, mini7_v1):
        is_human = s.public_player_id == human_seat
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
                occupant_principal_id=principal_id if is_human else None,
            )
        )
    await session.flush()
    return game.id


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


@pytest.mark.asyncio
async def test_post_holds_approved_message_without_sidecar_release(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _consenting_guest(client)
    principal_id = await _principal_id_for_token(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id)

    body = {
        "channel": "PUBLIC",
        "text": "I think P04 is suspicious.",
        "idempotency_key": "c1",
    }
    resp = await client.post(
        f"/human/games/{game_id}/chat",
        json=body,
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["accepted"] is True
    assert payload["public_player_id"] == _HUMAN_SEAT
    assert payload["channel"] == "PUBLIC"
    # Moderation approves the message, but US-159 requires the POST path to hold
    # it until the human-aware tick's symmetric release delay elapses.
    assert payload["status"] == "HELD"
    assert payload["idempotent_replay"] is False

    async with session_factory() as session:
        holds = (await session.execute(select(HumanChatSubmission))).scalars().all()
        sidecar = (await session.execute(select(HumanChatMessage))).scalars().all()
    assert len(holds) == 1
    assert holds[0].status == "HELD"
    assert holds[0].cleaned_text == "I think P04 is suspicious."
    # Raw text is not visible through the sidecar until the buffered tick release.
    assert sidecar == []


@pytest.mark.asyncio
async def test_over_limit_rejected(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _consenting_guest(client)
    principal_id = await _principal_id_for_token(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id)

    too_long = "x" * (mini7_v1.PUBLIC_MESSAGE_MAX_CHARS + 1)
    body = {"channel": "PUBLIC", "text": too_long, "idempotency_key": "c1"}
    resp = await client.post(
        f"/human/games/{game_id}/chat",
        json=body,
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 422

    async with session_factory() as session:
        holds = (await session.execute(select(HumanChatSubmission))).scalars().all()
        sidecar = (await session.execute(select(HumanChatMessage))).scalars().all()
    assert holds == []
    assert sidecar == []


@pytest.mark.asyncio
async def test_idempotent_retry(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _consenting_guest(client)
    principal_id = await _principal_id_for_token(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id)

    body = {"channel": "PUBLIC", "text": "hello town", "idempotency_key": "c1"}
    first = await client.post(
        f"/human/games/{game_id}/chat",
        json=body,
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert first.status_code == 200
    assert first.json()["idempotent_replay"] is False

    retry = await client.post(
        f"/human/games/{game_id}/chat",
        json=body,
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert retry.status_code == 200
    assert retry.json()["idempotent_replay"] is True

    async with session_factory() as session:
        holds = (await session.execute(select(HumanChatSubmission))).scalars().all()
        sidecar = (await session.execute(select(HumanChatMessage))).scalars().all()
    # A network retry never inserts a second hold row and never releases early.
    assert len(holds) == 1
    assert holds[0].status == "HELD"
    assert sidecar == []


@pytest.mark.asyncio
async def test_wrong_seat_rejected(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _consenting_guest(client)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=None)

    body = {"channel": "PUBLIC", "text": "hello", "idempotency_key": "c1"}
    resp = await client.post(
        f"/human/games/{game_id}/chat",
        json=body,
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "wrong_seat"


@pytest.mark.asyncio
async def test_action_field_rejected_by_schema(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # Chat firewall: the chat channel accepts ONLY chat; a stray structured
    # action field is a 422 (extra='forbid'), never silently parsed.
    token = await _consenting_guest(client)
    principal_id = await _principal_id_for_token(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id)

    body = {
        "channel": "PUBLIC",
        "text": "vote P04",
        "idempotency_key": "c1",
        "action": {"type": "VOTE", "target": "P04"},
    }
    resp = await client.post(
        f"/human/games/{game_id}/chat",
        json=body,
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 422

    async with session_factory() as session:
        holds = (await session.execute(select(HumanChatSubmission))).scalars().all()
    assert holds == []


@pytest.mark.asyncio
async def test_chat_requires_session(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=None)
    body = {"channel": "PUBLIC", "text": "hi", "idempotency_key": "c1"}
    resp = await client.post(f"/human/games/{game_id}/chat", json=body)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_chat_requires_consent(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    resp = await client.post("/human/guest")
    token = _guest_token(resp.headers)
    principal_id = await _principal_id_for_token(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id)

    body = {"channel": "PUBLIC", "text": "hi", "idempotency_key": "c1"}
    gated = await client.post(
        f"/human/games/{game_id}/chat",
        json=body,
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert gated.status_code == 412
    assert gated.json()["detail"] == "consent_required"
