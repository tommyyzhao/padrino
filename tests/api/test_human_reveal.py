"""Participant-gated endgame reveal for private human games (US-163).

Private human games do not enter the public broadcast index, so
``/public/games/{id}/reveal`` correctly stays closed. A seat occupant still
needs the same canonical endgame reveal after the game terminates.
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
from padrino.api.auth import SCOPE_SPECTATOR, RateLimiter, generate_raw_key
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import IdentityMode, SeatKind
from padrino.core.rulesets import mini7_v1
from padrino.db.models import Game, GameSeat, Principal, PromptVersion
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import providers as providers_repo
from padrino.public.broadcast_index import BroadcastState
from padrino.settings import get_settings

_GAME_SEED = "human-reveal-seed"
_HUMAN_SEAT = "P03"


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    get_settings.cache_clear()


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


async def _guest(client: AsyncClient) -> tuple[str, uuid.UUID]:
    resp = await client.post("/human/guest")
    assert resp.status_code == 201
    token = _guest_token(resp.headers)
    return token, uuid.UUID(resp.json()["principal_id"])


async def _seed_key(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    label: str,
) -> str:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        await api_keys_repo.create(session, raw_key=raw, scopes=[SCOPE_SPECTATOR], label=label)
    return raw


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def _make_agent_build(session: AsyncSession) -> uuid.UUID:
    provider = await providers_repo.create(
        session,
        name="cerebras",
        auth_secret_ref="env:MOCK_KEY",
    )
    mc = await model_configs_repo.create(
        session,
        provider_id=provider.id,
        model_name="zai-glm-4.7",
        default_temperature=0.7,
        default_top_p=1.0,
        default_max_output_tokens=512,
        supports_structured_outputs=False,
    )
    pv = PromptVersion(
        ruleset_id=mini7_v1.RULESET_ID,
        version="v1",
        system_prompt="s",
        developer_prompt="d",
        response_schema={},
        prompt_hash=str(uuid.uuid4()),
    )
    session.add(pv)
    await session.flush()
    build = await agent_builds_repo.create(
        session,
        display_name="GLM-Agent",
        model_config_id=mc.id,
        prompt_version_id=pv.id,
        adapter_version="1.0",
        inference_params={},
        active=True,
    )
    return build.id


def _setup_bodies(*, terminal: bool) -> list[dict[str, Any]]:
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
            "phase": "DAY_1_VOTE",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {"phase_kind": "DAY_VOTE", "day": 1, "round": 1},
        },
    ]
    if terminal:
        bodies.append(
            {
                "event_type": "GameTerminated",
                "sequence": 3,
                "phase": "DAY_1_VOTE",
                "visibility": "PUBLIC",
                "actor_player_id": None,
                "payload": {"winner": "TOWN", "reason": "all_mafia_eliminated"},
            }
        )
    return bodies


async def _seed_private_human_game(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    terminal: bool,
) -> uuid.UUID:
    ai_build = await _make_agent_build(session)
    game = Game(
        gauntlet_id=None,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=_GAME_SEED,
        status="COMPLETED" if terminal else "RUNNING",
        terminal_result=None,
        broadcast_state=BroadcastState.HIDDEN.value,
        is_broadcastable=False,
        identity_mode=IdentityMode.ANONYMOUS.value,
    )
    session.add(game)
    await session.flush()

    log = EventLog()
    for body in _setup_bodies(terminal=terminal):
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
        is_human = s.public_player_id == _HUMAN_SEAT
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id=s.public_player_id,
                seat_index=s.seat_index,
                agent_build_id=None if is_human else ai_build,
                seat_kind=SeatKind.HUMAN.value if is_human else SeatKind.AI.value,
                role=s.role.value,
                faction=s.faction.value,
                alive=True,
                occupant_principal_id=principal_id if is_human else None,
            )
        )
    await session.flush()
    return game.id


async def test_private_terminal_human_game_reveals_to_participant(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, principal_id = await _guest(client)
    raw_key = await _seed_key(session_factory, label="spectator")
    async with session_factory() as session, session.begin():
        game_id = await _seed_private_human_game(session, principal_id=principal_id, terminal=True)

    public_resp = await client.get(f"/public/games/{game_id}/reveal", headers=_auth(raw_key))
    assert public_resp.status_code == 404

    human_resp = await client.get(
        f"/human/games/{game_id}/reveal",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert human_resp.status_code == 200, human_resp.text
    body = human_resp.json()
    assert body["game_id"] == str(game_id)
    assert body["ruleset_id"] == mini7_v1.RULESET_ID
    assert body["winner"] == "TOWN"

    seats = {s["public_player_id"]: s for s in body["seats"]}
    assert seats[_HUMAN_SEAT]["is_human"] is True
    assert seats[_HUMAN_SEAT]["model"] is None
    ai_seats = [seat for sid, seat in seats.items() if sid != _HUMAN_SEAT]
    assert len(ai_seats) == mini7_v1.PLAYER_COUNT - 1
    assert all(seat["is_human"] is False for seat in ai_seats)
    assert all(seat["model"]["model_name"] == "zai-glm-4.7" for seat in ai_seats)


async def test_private_reveal_rejects_non_participant(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _owner_token, owner_id = await _guest(client)
    stranger_token, _stranger_id = await _guest(client)
    async with session_factory() as session, session.begin():
        game_id = await _seed_private_human_game(session, principal_id=owner_id, terminal=True)

    resp = await client.get(
        f"/human/games/{game_id}/reveal",
        cookies={HUMAN_SESSION_COOKIE: stranger_token},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "wrong_seat"


async def test_private_reveal_rejects_participant_before_terminal(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, principal_id = await _guest(client)
    async with session_factory() as session, session.begin():
        game_id = await _seed_private_human_game(session, principal_id=principal_id, terminal=False)

    resp = await client.get(
        f"/human/games/{game_id}/reveal",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "game_not_terminal"

    async with session_factory() as session:
        game = await session.get(Game, game_id)
        assert game is not None
        raw_seat = (
            await session.execute(
                select(GameSeat).where(
                    GameSeat.game_id == game_id,
                    GameSeat.occupant_principal_id == principal_id,
                )
            )
        ).scalar_one()
        raw_principal = await session.get(Principal, principal_id)
    assert raw_seat.seat_kind == SeatKind.HUMAN.value
    assert raw_principal is not None


async def test_private_reveal_requires_human_session(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, principal_id = await _guest(client)
    async with session_factory() as session, session.begin():
        game_id = await _seed_private_human_game(session, principal_id=principal_id, terminal=True)

    assert token
    resp = await client.get(f"/human/games/{game_id}/reveal")
    assert resp.status_code == 401
