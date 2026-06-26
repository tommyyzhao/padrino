"""Tests for the per-human casual stats API projection (US-276)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.analytics.human_stats import refresh_human_player_stats_for_game
from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE, generate_session_token
from padrino.db.models import Game, GameEvent, GameSeat, HumanPlayerStats
from padrino.db.repositories import human_principals as principals_repo

_RULESET = "mini7_v1"
_NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)

_ROLE_ASSIGNMENTS: list[dict[str, str]] = [
    {"public_player_id": "P01", "role": "MAFIA_GOON", "faction": "MAFIA"},
    {"public_player_id": "P02", "role": "MAFIA_GOON", "faction": "MAFIA"},
    {"public_player_id": "P03", "role": "DETECTIVE", "faction": "TOWN"},
    {"public_player_id": "P04", "role": "DOCTOR", "faction": "TOWN"},
    {"public_player_id": "P05", "role": "VILLAGER", "faction": "TOWN"},
    {"public_player_id": "P06", "role": "VILLAGER", "faction": "TOWN"},
    {"public_player_id": "P07", "role": "VILLAGER", "faction": "TOWN"},
]

_TOWN_WIN_GAME: list[dict[str, Any]] = [
    {
        "sequence": 1,
        "event_type": "RolesAssigned",
        "phase": "SETUP",
        "visibility": "SYSTEM",
        "actor_player_id": None,
        "payload": {"assignments": _ROLE_ASSIGNMENTS},
    },
    {
        "sequence": 2,
        "event_type": "VoteSubmitted",
        "phase": "DAY_1_VOTE",
        "visibility": "PUBLIC",
        "actor_player_id": "P03",
        "payload": {"target": "P01", "is_abstain": False},
    },
    {
        "sequence": 3,
        "event_type": "DetectiveResultDelivered",
        "phase": "NIGHT_1_ACTIONS",
        "visibility": "PRIVATE",
        "actor_player_id": "P03",
        "payload": {"target": "P01", "finding": "MAFIA"},
    },
    {
        "sequence": 4,
        "event_type": "DetectiveResultDelivered",
        "phase": "NIGHT_2_ACTIONS",
        "visibility": "PRIVATE",
        "actor_player_id": "P03",
        "payload": {"target": "P04", "finding": "TOWN"},
    },
    {
        "sequence": 5,
        "event_type": "GameTerminated",
        "phase": "DAY_2_VOTE",
        "visibility": "PUBLIC",
        "actor_player_id": None,
        "payload": {"winner": "TOWN", "reason": "all_mafia_dead"},
    },
]


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _human_cookie(resp_headers: object) -> str:
    jar = SimpleCookie()
    for raw in resp_headers.get_list("set-cookie"):  # type: ignore[attr-defined]
        if raw.startswith(f"{HUMAN_SESSION_COOKIE}="):
            jar.load(raw)
    assert HUMAN_SESSION_COOKIE in jar
    return jar[HUMAN_SESSION_COOKIE].value


async def _guest(client: AsyncClient) -> tuple[str, uuid.UUID]:
    resp = await client.post("/human/guest")
    assert resp.status_code == 201
    return _human_cookie(resp.headers), uuid.UUID(resp.json()["principal_id"])


async def _account_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, uuid.UUID]:
    raw = generate_session_token()
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        principal = await principals_repo.create_principal(session, kind="account")
        await principals_repo.create_session(
            session,
            principal_id=principal.id,
            raw_token=raw,
            kind="account",
            issued_at=now,
            expires_at=now + timedelta(hours=1),
        )
        return raw, principal.id


def _game_event(game_id: uuid.UUID, event: dict[str, Any]) -> GameEvent:
    return GameEvent(
        game_id=game_id,
        sequence=event["sequence"],
        event_type=event["event_type"],
        phase=event["phase"],
        visibility=event["visibility"],
        actor_player_id=event["actor_player_id"],
        payload=event["payload"],
        prev_event_hash="0" * 64,
        event_hash=f"{event['sequence']:064x}",
    )


async def _seed_completed_human_game(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> Game:
    game = Game(ruleset_id=_RULESET, game_seed="human-stats-route-seed", status="COMPLETED")
    session.add(game)
    await session.flush()
    for assignment in _ROLE_ASSIGNMENTS:
        is_human = assignment["public_player_id"] == "P03"
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id=assignment["public_player_id"],
                seat_index=int(assignment["public_player_id"][1:]),
                agent_build_id=None,
                seat_kind="HUMAN" if is_human else "AI",
                occupant_principal_id=principal_id if is_human else None,
                role=assignment["role"],
                faction=assignment["faction"],
                alive=True,
            )
        )
    for event in _TOWN_WIN_GAME:
        session.add(_game_event(game.id, event))
    await session.flush()
    return game


@pytest.mark.asyncio
async def test_human_stats_projects_materialized_row_into_client_shape(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, principal_id = await _guest(client)
    async with session_factory() as session, session.begin():
        session.add(
            HumanPlayerStats(
                ruleset_id=_RULESET,
                principal_id=principal_id,
                games=4,
                wins=2,
                draws=1,
                losses=1,
                role_win_rates_json=json.dumps(
                    [
                        {"name": "DETECTIVE", "wins": 1, "games": 2},
                        {"name": "VILLAGER", "wins": 1, "games": 2},
                    ]
                ),
                faction_win_rates_json="[]",
                survived_games=3,
                voting_total_votes=5,
                voting_accurate_votes=4,
                detection_total=3,
                detection_accurate=2,
                computed_at=_NOW,
            )
        )

    resp = await client.get(
        "/human/stats",
        params={"ruleset_id": _RULESET},
        cookies={HUMAN_SESSION_COOKIE: token},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "ruleset_id",
        "principal_id",
        "games",
        "wins",
        "draws",
        "losses",
        "role_win_rates",
        "survival_rate",
        "voting_accuracy",
        "detection_accuracy",
    }
    assert body["principal_id"] == str(principal_id)
    assert body["games"] == 4
    assert body["survival_rate"] == 0.75
    assert body["voting_accuracy"] == {"total_votes": 5, "accurate_votes": 4, "rate": 0.8}
    assert body["detection_accuracy"] == "2/3"
    assert body["role_win_rates"] == [
        {"role": "DETECTIVE", "wins": 1, "games": 2, "rate": 0.5},
        {"role": "VILLAGER", "wins": 1, "games": 2, "rate": 0.5},
    ]
    assert "rating" not in body
    assert "mu" not in body
    assert "sigma" not in body
    assert "conservative_score" not in body


@pytest.mark.asyncio
async def test_human_stats_requires_auth_and_returns_zero_payload_for_no_games(
    client: AsyncClient,
) -> None:
    unauth = await client.get("/human/stats", params={"ruleset_id": _RULESET})
    assert unauth.status_code == 401

    token, principal_id = await _guest(client)
    resp = await client.get(
        "/human/stats",
        params={"ruleset_id": _RULESET},
        cookies={HUMAN_SESSION_COOKIE: token},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "ruleset_id": _RULESET,
        "principal_id": str(principal_id),
        "games": 0,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "role_win_rates": [],
        "survival_rate": 0.0,
        "voting_accuracy": {"total_votes": 0, "accurate_votes": 0, "rate": 0.0},
        "detection_accuracy": "0",
    }


@pytest.mark.asyncio
async def test_human_stats_accepts_account_principals(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, principal_id = await _account_session(session_factory)
    resp = await client.get(
        "/human/stats",
        params={"ruleset_id": _RULESET},
        cookies={HUMAN_SESSION_COOKIE: token},
    )

    assert resp.status_code == 200
    assert resp.json()["principal_id"] == str(principal_id)
    assert resp.json()["games"] == 0


@pytest.mark.asyncio
async def test_human_stats_projects_analytics_materialized_output(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, principal_id = await _guest(client)
    async with session_factory() as session, session.begin():
        game = await _seed_completed_human_game(session, principal_id=principal_id)
        refreshed = await refresh_human_player_stats_for_game(session, game.id, now=_NOW)
        assert len(refreshed) == 1

    resp = await client.get(
        "/human/stats",
        params={"ruleset_id": _RULESET},
        cookies={HUMAN_SESSION_COOKIE: token},
    )

    assert resp.status_code == 200
    body = resp.json()
    async with session_factory() as session:
        row = (
            await session.execute(
                select(HumanPlayerStats).where(HumanPlayerStats.principal_id == principal_id)
            )
        ).scalar_one()

    assert body["games"] == row.games == 1
    assert body["wins"] == row.wins == 1
    assert body["survival_rate"] == row.survived_games / row.games == 1.0
    assert body["voting_accuracy"] == {"total_votes": 1, "accurate_votes": 1, "rate": 1.0}
    assert body["detection_accuracy"] == "1/2"
    assert body["role_win_rates"] == [{"role": "DETECTIVE", "wins": 1, "games": 1, "rate": 1.0}]
