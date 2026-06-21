"""Per-seat live observation stream + phase-deadline frame (US-136).

A human player's client receives, over an authenticated seat-scoped live stream:

* its OWN seat observation — the seat's private info (its role/faction, its
  private events, role-conditional fields) plus the legal actions for the phase;
* a transport-only phase-deadline frame carrying the wall-clock deadline.

This covers:

* a seat sees only its own private info + legal actions (never another seat's);
* the deadline frame is present and carries the persisted wall-clock deadline;
* in anonymous mode the stream carries no human-vs-AI / model identity markers;
* a wrong-seat request (a principal that occupies no seat) is rejected (403).
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
from padrino.api.human_observation import DEADLINE_FRAME, OBSERVATION_FRAME
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import IdentityMode, SeatKind
from padrino.core.human_chat import human_chat_content_ref
from padrino.core.observation_privacy import IDENTITY_MARKER_KEYS
from padrino.core.rulesets import mini7_v1
from padrino.db.models import Game, GameSeat, Principal
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import human_chat as human_chat_repo
from padrino.db.repositories import human_game_runtime as runtime_repo
from padrino.runner.human_durability import replay_state_from_rows

_GAME_SEED = "obs-seed"
_PHASE = "DAY_1_VOTE"
_HUMAN_SEAT = "P03"
_DEADLINE = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


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
    identity_mode: str = IdentityMode.ANONYMOUS.value,
    human_seat: str = _HUMAN_SEAT,
    deadline: datetime | None = _DEADLINE,
) -> uuid.UUID:
    """Persist a non-terminal human game at DAY_1_VOTE and return its id."""
    game = Game(
        gauntlet_id=None,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=_GAME_SEED,
        status="RUNNING",
        identity_mode=identity_mode,
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

    await runtime_repo.upsert(
        session,
        game_id=game.id,
        phase=_PHASE,
        deadline_at=deadline,
        buffer_snapshot={},
        updated_at=datetime(2026, 6, 19, 11, 59, 0, tzinfo=UTC),
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


async def _guest(client: AsyncClient) -> str:
    resp = await client.post("/human/guest")
    assert resp.status_code == 201
    return _guest_token(resp.headers)


async def _principal_id(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as session:
        principal = (await session.execute(select(Principal))).scalars().one()
    return principal.id


def _parse_frames(body: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block.startswith("data:"):
            continue
        frames.append(json.loads(block[len("data:") :].strip()))
    return frames


def _has_marker(value: Any) -> bool:
    if isinstance(value, dict):
        return any(k in IDENTITY_MARKER_KEYS for k in value) or any(
            _has_marker(v) for v in value.values()
        )
    if isinstance(value, list | tuple):
        return any(_has_marker(item) for item in value)
    return False


async def _append_released_human_chat(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    actor: str,
    text: str,
) -> None:
    rows = await events_repo.list_events(session, game_id)
    _, log = replay_state_from_rows(rows)
    body: dict[str, Any] = {
        "event_type": "PublicMessageSubmitted",
        "sequence": len(log.events),
        "phase": _PHASE,
        "visibility": "PUBLIC",
        "actor_player_id": actor,
        "payload": {"text": "", "round_index": None, "content_ref": human_chat_content_ref(text)},
    }
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
    await human_chat_repo.append_human_chat(
        session,
        game_id=game_id,
        sequence=stored.sequence,
        public_player_id=actor,
        raw_text=text,
        cleaned_text=text,
    )


@pytest.mark.asyncio
async def test_seat_sees_own_observation_legal_actions_and_deadline(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _guest(client)
    principal_id = await _principal_id(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id)

    resp = await client.get(
        f"/human/games/{game_id}/observation/stream",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = _parse_frames(resp.text)
    types = [f["type"] for f in frames]
    assert OBSERVATION_FRAME in types
    assert DEADLINE_FRAME in types

    obs = next(f for f in frames if f["type"] == OBSERVATION_FRAME)
    # The seat sees its OWN identity (its private info).
    assert obs["you"]["player_id"] == _HUMAN_SEAT
    # Legal actions for the phase are present.
    assert "legal_actions" in obs
    assert obs["phase"] == _PHASE

    # The deadline frame carries the persisted wall-clock deadline (transport
    # only). Its value matches the seeded deadline.
    deadline = next(f for f in frames if f["type"] == DEADLINE_FRAME)
    assert deadline["deadline_at"] == _DEADLINE.isoformat()
    assert deadline["phase"] == _PHASE


@pytest.mark.asyncio
async def test_seat_does_not_see_other_seats_roles(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _guest(client)
    principal_id = await _principal_id(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id)

    resp = await client.get(
        f"/human/games/{game_id}/observation/stream",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    obs = next(f for f in _parse_frames(resp.text) if f["type"] == OBSERVATION_FRAME)

    # Only the seat's OWN role/faction is exposed (in the `you` block). No event
    # entry the seat is entitled to see carries another seat's role/faction.
    for entry in obs["public_events"] + obs["private_events"]:
        assert "role" not in entry["payload"]
        assert "faction" not in entry["payload"]


@pytest.mark.asyncio
async def test_observation_stream_resolves_released_human_chat_from_sidecar(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _guest(client)
    principal_id = await _principal_id(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id)
        await _append_released_human_chat(
            session,
            game_id=game_id,
            actor="P04",
            text="Released human text for the player stream",
        )

    resp = await client.get(
        f"/human/games/{game_id}/observation/stream",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 200
    obs = next(f for f in _parse_frames(resp.text) if f["type"] == OBSERVATION_FRAME)
    messages = [
        entry
        for entry in obs["public_events"]
        if entry["event_type"] == "PublicMessageSubmitted" and entry["actor_player_id"] == "P04"
    ]
    assert len(messages) == 1
    assert messages[0]["payload"]["text"] == "Released human text for the player stream"
    assert messages[0]["payload"]["content_ref"] == human_chat_content_ref(
        "Released human text for the player stream"
    )


@pytest.mark.asyncio
async def test_anonymous_mode_has_no_identity_markers(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _guest(client)
    principal_id = await _principal_id(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(
            session, principal_id=principal_id, identity_mode=IdentityMode.ANONYMOUS.value
        )

    resp = await client.get(
        f"/human/games/{game_id}/observation/stream",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 200
    for frame in _parse_frames(resp.text):
        assert not _has_marker(frame)


@pytest.mark.asyncio
async def test_wrong_seat_rejected(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _guest(client)
    # The human seat belongs to NO principal, so the caller occupies no seat.
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=None)

    resp = await client.get(
        f"/human/games/{game_id}/observation/stream",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "wrong_seat"


@pytest.mark.asyncio
async def test_stream_requires_session(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=None)
    resp = await client.get(f"/human/games/{game_id}/observation/stream")
    assert resp.status_code == 401
