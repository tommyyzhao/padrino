"""Post-terminal spot-the-AI guess flow + reveal gating (US-144).

After a human game terminates each human submits ONE guess assigning HUMAN/AI to
every OTHER seat over the existing human channel; the pure scorer computes their
detection accuracy and the guess + result persist. The reveal endpoint gates the
viewer's own accuracy behind their submitted guess. Covers:

* accept-once + re-submission returns the stored result (a guesser guesses once);
* a guess before the game is terminal is rejected (409);
* a wrong-seat / no-seat submission is rejected (403);
* an invalid guess (missing a seat / bad label) is rejected;
* GET own-result is gated: 404 before a guess, the personal stat after.
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
from padrino.db.models import Game, GameSeat, HumanTuringGuess, Principal
from padrino.db.repositories import events as events_repo

_GAME_SEED = "guess-seed"
_HUMAN_SEAT = "P03"


def _setup_bodies(human_seat: str, *, terminal: bool) -> list[dict[str, Any]]:
    """Setup events; when ``terminal`` append a GameTerminated event."""
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


async def _seed_human_game(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID | None,
    terminal: bool,
    human_seat: str = _HUMAN_SEAT,
) -> uuid.UUID:
    game = Game(
        gauntlet_id=None,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=_GAME_SEED,
        status="COMPLETED" if terminal else "RUNNING",
    )
    session.add(game)
    await session.flush()

    log = EventLog()
    for body in _setup_bodies(human_seat, terminal=terminal):
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


def _full_guess(*, guesser: str = _HUMAN_SEAT, correct: bool = True) -> dict[str, str]:
    """A guess for every OTHER seat. With ``correct=True`` every label is right.

    Only the human seat (``guesser``) is HUMAN; every other seat is AI in this
    setup, so a fully-correct guess labels every other seat ``"AI"``.
    """
    seats = [s.public_player_id for s in assign_roles(_GAME_SEED, mini7_v1)]
    out: dict[str, str] = {}
    for seat in seats:
        if seat == guesser:
            continue
        out[seat] = "AI" if correct else "HUMAN"
    return out


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


async def _principal_id(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as session:
        principal = (await session.execute(select(Principal))).scalars().one()
    return principal.id


@pytest.mark.asyncio
async def test_accept_once_then_replay(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _consenting_guest(client)
    principal_id = await _principal_id(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id, terminal=True)

    body = {"guess": _full_guess(correct=True)}
    first = await client.post(
        f"/human/games/{game_id}/turing-guess",
        json=body,
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert first.status_code == 200
    payload = first.json()
    assert payload["guesser_public_id"] == _HUMAN_SEAT
    assert payload["total"] == mini7_v1.PLAYER_COUNT - 1
    assert payload["correct"] == mini7_v1.PLAYER_COUNT - 1
    assert payload["accuracy"] == "1"
    assert payload["idempotent_replay"] is False

    # A re-submission (even with a DIFFERENT guess) returns the stored result;
    # a guesser guesses once and the personal stat does not change.
    replay = await client.post(
        f"/human/games/{game_id}/turing-guess",
        json={"guess": _full_guess(correct=False)},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["accuracy"] == "1"

    async with session_factory() as session:
        rows = (await session.execute(select(HumanTuringGuess))).scalars().all()
    assert len(rows) == 1
    assert rows[0].correct == mini7_v1.PLAYER_COUNT - 1
    assert rows[0].accuracy == "1"


@pytest.mark.asyncio
async def test_partial_accuracy(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _consenting_guest(client)
    principal_id = await _principal_id(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id, terminal=True)

    # Flip exactly one label to HUMAN (wrong): 5 of 6 correct.
    guess = _full_guess(correct=True)
    first_other = next(iter(guess))
    guess[first_other] = "HUMAN"
    resp = await client.post(
        f"/human/games/{game_id}/turing-guess",
        json={"guess": guess},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 6
    assert payload["correct"] == 5
    assert payload["accuracy"] == "5/6"


@pytest.mark.asyncio
async def test_guess_before_terminal_rejected(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _consenting_guest(client)
    principal_id = await _principal_id(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id, terminal=False)

    resp = await client.post(
        f"/human/games/{game_id}/turing-guess",
        json={"guess": _full_guess()},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "game_not_terminal"

    async with session_factory() as session:
        rows = (await session.execute(select(HumanTuringGuess))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_wrong_seat_rejected(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _consenting_guest(client)
    # The human seat belongs to no principal: the caller occupies no seat.
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=None, terminal=True)

    resp = await client.post(
        f"/human/games/{game_id}/turing-guess",
        json={"guess": _full_guess()},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "wrong_seat"


@pytest.mark.asyncio
async def test_incomplete_guess_rejected(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _consenting_guest(client)
    principal_id = await _principal_id(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id, terminal=True)

    guess = _full_guess()
    guess.pop(next(iter(guess)))  # omit one seat -> incomplete
    resp = await client.post(
        f"/human/games/{game_id}/turing-guess",
        json={"guess": guess},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "invalid_guess"


@pytest.mark.asyncio
async def test_bad_label_rejected_by_schema(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _consenting_guest(client)
    principal_id = await _principal_id(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id, terminal=True)

    guess = _full_guess()
    guess[next(iter(guess))] = "MAYBE"  # not HUMAN / AI
    resp = await client.post(
        f"/human/games/{game_id}/turing-guess",
        json={"guess": guess},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_guess_requires_session(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=None, terminal=True)
    resp = await client.post(f"/human/games/{game_id}/turing-guess", json={"guess": _full_guess()})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_reveal_result_gated_behind_guess(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _consenting_guest(client)
    principal_id = await _principal_id(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=principal_id, terminal=True)

    # Before guessing, the personal accuracy result is not disclosed.
    before = await client.get(
        f"/human/games/{game_id}/turing-guess",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert before.status_code == 404
    assert before.json()["detail"] == "guess_not_found"

    # Submit the guess.
    submit = await client.post(
        f"/human/games/{game_id}/turing-guess",
        json={"guess": _full_guess(correct=True)},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert submit.status_code == 200

    # Now the viewer's own accuracy is disclosed.
    after = await client.get(
        f"/human/games/{game_id}/turing-guess",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert after.status_code == 200
    assert after.json()["accuracy"] == "1"
    assert after.json()["correct"] == mini7_v1.PLAYER_COUNT - 1
