"""Tests for game-inspection routes (US-044)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, Role
from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.repositories import (
    agent_builds as agent_builds_repo,
)
from padrino.db.repositories import (
    games as games_repo,
)
from padrino.db.repositories import (
    leagues as leagues_repo,
)
from padrino.db.repositories import (
    model_configs as model_configs_repo,
)
from padrino.db.repositories import (
    prompt_versions as prompt_versions_repo,
)
from padrino.db.repositories import (
    providers as providers_repo,
)
from padrino.llm.mock import DeterministicMockAdapter
from padrino.runner.game_runner import GameConfig, GamePersistence, run_game
from tests.conftest import make_town_win_script

_ADMIN_TOKEN = "test-admin-token-44"
_GAME_SEED = "seed-us044-001"


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        session_factory=session_factory,
        admin_token=_ADMIN_TOKEN,
        auth_required=False,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest_asyncio.fixture
async def no_token_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        session_factory=session_factory,
        admin_token=None,
        auth_required=False,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _split_factions() -> tuple[list[str], list[str], str, str]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return mafia, town, doctor, detective


async def _seed_completed_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    """Insert a league, agent builds, run a town-win scripted game, return game_id.

    The runner (US-049) writes ``game_seats`` rows itself via the
    ``RolesAssigned`` event so this helper does not pre-populate them.
    """
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)

    async with session_factory() as session, session.begin():
        provider = await providers_repo.create(
            session, name="cerebras", auth_secret_ref="CEREBRAS_API_KEY"
        )
        mc = await model_configs_repo.create(
            session,
            provider_id=provider.id,
            model_name="glm-4.7",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        builds: list[uuid.UUID] = []
        for i in range(mini7_v1.PLAYER_COUNT):
            pv = await prompt_versions_repo.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                version=f"v{i + 1}",
                system_prompt="sys",
                developer_prompt="dev",
                response_schema={"type": "object"},
                prompt_hash=f"us044-{i}",
            )
            ab = await agent_builds_repo.create(
                session,
                display_name=f"build-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="2026.05",
                inference_params={"temperature": 0.7},
                active=True,
            )
            builds.append(ab.id)
        league = await leagues_repo.create(
            session, name="us044", ruleset_id=mini7_v1.RULESET_ID, ranked=False
        )
        game = await games_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_GAME_SEED,
            status="RUNNING",
        )
        league_id = league.id
        game_id = game.id
    agent_builds_by_seat = {f"P{i + 1:02d}": builds[i] for i in range(mini7_v1.PLAYER_COUNT)}

    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=game_id,
        agent_builds=agent_builds_by_seat,
        league_id=league_id,
    )
    await run_game(
        GameConfig(game_id="G-US044", game_seed=_GAME_SEED, timeout_s=1.0),
        DeterministicMockAdapter(script),
        ranked=False,
        persistence=persistence,
    )
    return game_id


async def test_get_game_returns_summary_and_seat_count(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id = await _seed_completed_game(session_factory)
    response = await client.get(f"/games/{game_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(game_id)
    assert body["seat_count"] == mini7_v1.PLAYER_COUNT
    # Runner (US-049) flips status to COMPLETED on GameTerminated.
    assert body["status"] == "COMPLETED"
    assert "current_phase" in body
    assert body["terminal_result"] is not None
    assert body["terminal_result"]["winner"] == "TOWN"
    assert "reason" in body["terminal_result"]
    assert "day_terminated" in body["terminal_result"]


async def test_get_game_not_found(client: AsyncClient) -> None:
    response = await client.get(f"/games/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_get_events_default_returns_public_only(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id = await _seed_completed_game(session_factory)
    response = await client.get(f"/games/{game_id}/events")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["visibility"] == "public"
    assert len(body["events"]) > 0
    for evt in body["events"]:
        assert evt["visibility"] == "PUBLIC"


async def test_get_events_public_explicit(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id = await _seed_completed_game(session_factory)
    response = await client.get(f"/games/{game_id}/events?visibility=public")
    assert response.status_code == 200
    body = response.json()
    assert all(e["visibility"] == "PUBLIC" for e in body["events"])


async def test_get_events_all_without_token_is_forbidden(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id = await _seed_completed_game(session_factory)
    response = await client.get(f"/games/{game_id}/events?visibility=all")
    assert response.status_code == 403


async def test_get_events_all_with_wrong_token_is_forbidden(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id = await _seed_completed_game(session_factory)
    response = await client.get(
        f"/games/{game_id}/events?visibility=all",
        headers={"X-Padrino-Admin-Token": "wrong"},
    )
    assert response.status_code == 403


async def test_get_events_all_with_admin_token_includes_private_and_system(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id = await _seed_completed_game(session_factory)
    response = await client.get(
        f"/games/{game_id}/events?visibility=all",
        headers={"X-Padrino-Admin-Token": _ADMIN_TOKEN},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    visibilities = {e["visibility"] for e in body["events"]}
    assert "PUBLIC" in visibilities
    assert "PRIVATE" in visibilities
    assert "SYSTEM" in visibilities


async def test_get_events_all_blocked_when_admin_token_unset(
    no_token_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A server with no admin token configured must refuse non-public reads."""
    game_id = await _seed_completed_game(session_factory)
    response = await no_token_client.get(
        f"/games/{game_id}/events?visibility=all",
        headers={"X-Padrino-Admin-Token": ""},
    )
    assert response.status_code == 403


async def test_get_transcript_after_terminal(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id = await _seed_completed_game(session_factory)
    response = await client.get(f"/games/{game_id}/transcript")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["game_id"] == str(game_id)
    assert body["outcome"]["winner"] == "TOWN"
    assert len(body["outcome"]["reason"]) > 0

    # Seven roles revealed.
    assert len(body["roles"]) == mini7_v1.PLAYER_COUNT
    factions = {r["faction"] for r in body["roles"]}
    assert factions == {"TOWN", "MAFIA"}

    # Action list contains at least one vote (Day-1 town vote eliminates Mafia[0]).
    action_types = {a["event_type"] for a in body["actions"]}
    assert "VoteSubmitted" in action_types
    # The script defines MafiaKill, Protect, Investigate on N1 — they must surface.
    assert "MafiaKillVoteSubmitted" in action_types
    assert "ProtectSubmitted" in action_types
    assert "InvestigateSubmitted" in action_types

    # Public and mafia chat are present as lists (possibly empty for chat).
    assert isinstance(body["public_chat"], list)
    assert isinstance(body["mafia_chat"], list)


async def test_get_transcript_before_terminal_is_conflict(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game = await games_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed="never-started",
            status="CREATED",
        )
        game_id = game.id
    response = await client.get(f"/games/{game_id}/transcript")
    assert response.status_code == 409


async def test_replay_pass(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id = await _seed_completed_game(session_factory)
    response = await client.post(f"/games/{game_id}/replay")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["game_id"] == str(game_id)
    assert body["replay_status"] == "PASS"
    # 64-hex sha256 digest.
    assert len(body["final_event_hash"]) == 64
    int(body["final_event_hash"], 16)


async def test_replay_fail_on_tampered_event(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy import select, update

    from padrino.db.models import GameEvent

    game_id = await _seed_completed_game(session_factory)
    # Mutate one event payload after the fact — chain breaks at that sequence.
    async with session_factory() as session, session.begin():
        stmt = (
            select(GameEvent)
            .where(GameEvent.game_id == game_id, GameEvent.event_type == "PublicMessageSubmitted")
            .order_by(GameEvent.sequence)
            .limit(1)
        )
        row = (await session.execute(stmt)).scalars().first()
        if row is None:
            # If the scripted town-win produced no public messages, tamper with a vote.
            stmt2 = (
                select(GameEvent)
                .where(GameEvent.game_id == game_id, GameEvent.event_type == "VoteSubmitted")
                .order_by(GameEvent.sequence)
                .limit(1)
            )
            row = (await session.execute(stmt2)).scalars().first()
        assert row is not None
        tampered = dict(row.payload)
        # Force a payload that round-trips through JSON differently.
        tampered["__tamper__"] = "yes"
        await session.execute(
            update(GameEvent).where(GameEvent.id == row.id).values(payload=tampered)
        )

    response = await client.post(f"/games/{game_id}/replay")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["replay_status"] == "FAIL"
    assert len(body["final_event_hash"]) == 64


async def test_replay_game_not_found(client: AsyncClient) -> None:
    response = await client.post(f"/games/{uuid.uuid4()}/replay")
    assert response.status_code == 404
