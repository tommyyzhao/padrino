"""Authenticated POST action channel for human players (US-134).

A human submits a structured ``Action`` (vote / protect / investigate / etc.) for
their own seat over an authenticated POST, validated server-side against
``legal_actions_for`` and the chat firewall. Covers:

* accept-once + idempotent retry (a retry with the same key never double-votes);
* an illegal target is rejected (409);
* an out-of-phase action (the seat cannot act in this phase) is rejected (409);
* a wrong-seat submission (a principal acting for a seat they do not occupy) is
  rejected (403).
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
from padrino.db.models import Game, GameSeat, HumanActionSubmission, Principal
from padrino.db.repositories import events as events_repo

_GAME_SEED = "act-seed"
_PHASE = "DAY_1_VOTE"
# P03 is a TOWN villager for this seed (see role assignment); a clean voter.
_HUMAN_SEAT = "P03"


def _vote_phase_bodies(human_seat: str) -> list[dict[str, Any]]:
    """A non-terminal human game positioned at DAY_1_VOTE (votes are legal)."""
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
            "payload": {"phase_kind": "DAY_VOTE", "day": 1, "round": 1},
        },
    ]


async def _seed_human_game(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID | None,
    human_seat: str = _HUMAN_SEAT,
) -> uuid.UUID:
    """Persist a non-terminal human game at DAY_1_VOTE and return its id.

    The seat ``human_seat`` is linked to ``principal_id`` (if given) via
    ``occupant_principal_id`` so the action channel resolves it for that human.
    """
    game = Game(
        gauntlet_id=None,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=_GAME_SEED,
        status="RUNNING",
    )
    session.add(game)
    await session.flush()

    log = EventLog()
    for body in _vote_phase_bodies(human_seat):
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
    """Create a guest, accept consent, and return the session token."""
    resp = await client.post("/human/guest")
    assert resp.status_code == 201
    token = _guest_token(resp.headers)
    consent = await client.post("/human/consent", cookies={HUMAN_SESSION_COOKIE: token})
    assert consent.status_code == 201
    return token


async def _principal_id_for_token(
    session_factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    """Return the single guest principal id created in this test DB."""
    async with session_factory() as session:
        principal = (await session.execute(select(Principal))).scalars().one()
    return principal.id


@pytest.mark.asyncio
async def test_accept_once_then_idempotent(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _consenting_guest(client)
    principal_id = await _principal_id_for_token(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id)

    body = {"action": {"type": "VOTE", "target": "P04"}, "idempotency_key": "k1"}
    first = await client.post(
        f"/human/games/{game_id}/actions",
        json=body,
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert first.status_code == 200
    payload = first.json()
    assert payload["accepted"] is True
    assert payload["action_type"] == "VOTE"
    assert payload["target"] == "P04"
    assert payload["public_player_id"] == _HUMAN_SEAT
    assert payload["idempotent_replay"] is False

    # A network retry with the SAME idempotency key returns the recorded action
    # without inserting a second row (no double-vote).
    retry = await client.post(
        f"/human/games/{game_id}/actions",
        json=body,
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert retry.status_code == 200
    assert retry.json()["idempotent_replay"] is True

    async with session_factory() as session:
        rows = (await session.execute(select(HumanActionSubmission))).scalars().all()
    assert len(rows) == 1
    assert rows[0].action_type == "VOTE"
    assert rows[0].target == "P04"


@pytest.mark.asyncio
async def test_illegal_target_rejected(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _consenting_guest(client)
    principal_id = await _principal_id_for_token(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id)

    # A player may not vote themselves; P03 -> P03 is not a legal target.
    body = {"action": {"type": "VOTE", "target": _HUMAN_SEAT}, "idempotency_key": "k1"}
    resp = await client.post(
        f"/human/games/{game_id}/actions",
        json=body,
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "illegal_action"

    async with session_factory() as session:
        rows = (await session.execute(select(HumanActionSubmission))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_out_of_phase_action_rejected(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _consenting_guest(client)
    principal_id = await _principal_id_for_token(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id)

    # PROTECT is a night action; it is not legal for a town seat in DAY_VOTE.
    body = {"action": {"type": "PROTECT", "target": "P04"}, "idempotency_key": "k1"}
    resp = await client.post(
        f"/human/games/{game_id}/actions",
        json=body,
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "illegal_action"


@pytest.mark.asyncio
async def test_wrong_seat_rejected(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _consenting_guest(client)
    # Seed a game where the human seat belongs to NO principal: the caller
    # occupies no seat in this game, so any action is a wrong-seat rejection.
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=None)

    body = {"action": {"type": "VOTE", "target": "P04"}, "idempotency_key": "k1"}
    resp = await client.post(
        f"/human/games/{game_id}/actions",
        json=body,
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "wrong_seat"


@pytest.mark.asyncio
async def test_action_requires_session(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=None)
    body = {"action": {"type": "VOTE", "target": "P04"}, "idempotency_key": "k1"}
    resp = await client.post(f"/human/games/{game_id}/actions", json=body)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_action_requires_consent(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # A guest WITHOUT consent is gated before any action is validated.
    resp = await client.post("/human/guest")
    token = _guest_token(resp.headers)
    principal_id = await _principal_id_for_token(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id)

    body = {"action": {"type": "VOTE", "target": "P04"}, "idempotency_key": "k1"}
    gated = await client.post(
        f"/human/games/{game_id}/actions",
        json=body,
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert gated.status_code == 412
    assert gated.json()["detail"] == "consent_required"


@pytest.mark.asyncio
async def test_chat_field_rejected_by_schema(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # Chat firewall: the action channel accepts ONLY the structured action; a
    # stray chat field is a 422 (extra='forbid'), never silently parsed.
    token = await _consenting_guest(client)
    principal_id = await _principal_id_for_token(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id)

    body = {
        "action": {"type": "VOTE", "target": "P04"},
        "idempotency_key": "k1",
        "public_message": "vote P04 with me",
    }
    resp = await client.post(
        f"/human/games/{game_id}/actions",
        json=body,
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 422

    async with session_factory() as session:
        rows = (await session.execute(select(HumanActionSubmission))).scalars().all()
    assert rows == []
