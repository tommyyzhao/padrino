"""Tests for the leaderboard route (US-045).

Validates ``GET /leagues/{id}/leaderboard``:

* Returns the JSON contract from prd.md §10.4 (leaderboard_id, ruleset_id,
  prompt_version, rating_model, entries[]).
* Entries are sorted by conservative_score desc and include the required
  per-agent_build counters + provisional flag.
* role_family_breakdown is an empty dict in v1.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.core.engine.hashing import GENESIS_HASH, compute_event_hash
from padrino.core.enums import Faction
from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import Rating
from padrino.db.repositories import (
    agent_builds as agent_builds_repo,
)
from padrino.db.repositories import (
    events as events_repo,
)
from padrino.db.repositories import (
    games as games_repo,
)
from padrino.db.repositories import (
    gauntlets as gauntlets_repo,
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
    app = create_app(session_factory=session_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _append_chained(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    bodies: list[dict[str, Any]],
    start_seq: int = 0,
    start_prev: str = GENESIS_HASH,
) -> str:
    prev = start_prev
    for i, body in enumerate(bodies):
        sealed = dict(body)
        sealed["sequence"] = start_seq + i
        ev_hash = compute_event_hash(prev, sealed)
        await events_repo.append_event(
            session,
            game_id=game_id,
            sequence=sealed["sequence"],
            event_type=sealed["event_type"],
            phase=sealed["phase"],
            visibility=sealed["visibility"],
            actor_player_id=sealed.get("actor_player_id"),
            payload=dict(sealed.get("payload", {})),
            prev_event_hash=prev,
            event_hash=ev_hash,
        )
        prev = ev_hash
    return prev


def _terminated_body(winner: str, reason: str = "scripted") -> dict[str, Any]:
    return {
        "event_type": "GameTerminated",
        "phase": "TERMINAL",
        "visibility": "PUBLIC",
        "actor_player_id": None,
        "payload": {"winner": winner, "reason": reason},
    }


async def _build_league_with_two_agents(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    winners: tuple[str, str, str] = ("TOWN", "TOWN", "MAFIA"),
    a_display: str = "alpha-build",
    b_display: str = "bravo-build",
    mu_a: float = 26.0,
    sigma_a: float = 5.0,
    mu_b: float = 30.0,
    sigma_b: float = 4.0,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
    """Seed two agent_builds A and B and three terminal games.

    Returns ``(league_id, agent_build_a_id, agent_build_b_id, prompt_version_str)``.

    Roster layout (slot → agent_build): ``[A, A, A, A, B, B, B]``.
    With ``mafia_indices=(0, 1)`` both mafia seats (P01, P02) belong to A;
    A also has two town seats (P03, P04). B has three town seats (P05-P07).

    Manually inserted Rating rows pin the conservative_score so ordering is
    deterministic regardless of OpenSkill internals.
    """
    async with session_factory() as session, session.begin():
        provider = await providers_repo.create(
            session, name="cerebras", auth_secret_ref="env:CEREBRAS_API_KEY"
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
        pv = await prompt_versions_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            version="v1-test",
            system_prompt="sys",
            developer_prompt="dev",
            response_schema={"type": "object"},
            prompt_hash=f"ph-{uuid.uuid4().hex}",
        )
        league = await leagues_repo.create(
            session, name="Test League", ruleset_id=mini7_v1.RULESET_ID, ranked=True
        )
        ab_a = await agent_builds_repo.create(
            session,
            display_name=a_display,
            model_config_id=mc.id,
            prompt_version_id=pv.id,
            adapter_version="2026.05",
            inference_params={},
            active=True,
        )
        ab_b = await agent_builds_repo.create(
            session,
            display_name=b_display,
            model_config_id=mc.id,
            prompt_version_id=pv.id,
            adapter_version="2026.05",
            inference_params={},
            active=True,
        )
        gauntlet = await gauntlets_repo.create(
            session,
            league_id=league.id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv.id,
            clone_count=len(winners),
            gauntlet_seed="lb-test-seed",
            ranked=True,
            status="QUEUED",
        )
        roster = [ab_a.id, ab_a.id, ab_a.id, ab_a.id, ab_b.id, ab_b.id, ab_b.id]
        for slot_index, ab_id in enumerate(roster):
            await gauntlets_repo.add_roster_slot(session, gauntlet.id, slot_index, ab_id)

        # Inserted ratings (GLOBAL scope) pin ordering: B's conservative_score > A's.
        cs_a = mu_a - 3.0 * sigma_a
        cs_b = mu_b - 3.0 * sigma_b
        session.add(
            Rating(
                league_id=league.id,
                agent_build_id=ab_a.id,
                scope_type="GLOBAL",
                scope_value="global",
                mu=mu_a,
                sigma=sigma_a,
                conservative_score=cs_a,
                games=12,
            )
        )
        session.add(
            Rating(
                league_id=league.id,
                agent_build_id=ab_b.id,
                scope_type="GLOBAL",
                scope_value="global",
                mu=mu_b,
                sigma=sigma_b,
                conservative_score=cs_b,
                games=9,
            )
        )

    for i, winner in enumerate(winners):
        async with session_factory() as session, session.begin():
            game = await games_repo.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                game_seed=f"lb-seed-{i}",
                gauntlet_id=gauntlet.id,
            )
            roster_local = [ab_a.id, ab_a.id, ab_a.id, ab_a.id, ab_b.id, ab_b.id, ab_b.id]
            for j, ab_id in enumerate(roster_local):
                sid = f"P{j + 1:02d}"
                faction = Faction.MAFIA if j in (0, 1) else Faction.TOWN
                await games_repo.add_seat(
                    session,
                    game_id=game.id,
                    public_player_id=sid,
                    seat_index=j,
                    agent_build_id=ab_id,
                    role="MAFIA_GOON" if faction is Faction.MAFIA else "VILLAGER",
                    faction=faction.value,
                )
            # One public message + one timeout per game so the rate metrics are non-zero.
            await _append_chained(
                session,
                game_id=game.id,
                bodies=[
                    {
                        "event_type": "PublicMessageSubmitted",
                        "phase": "DAY_DISCUSSION:1:1",
                        "visibility": "PUBLIC",
                        "actor_player_id": "P03",
                        "payload": {"text": "hello", "round_index": 1},
                    },
                    {
                        "event_type": "VoteSubmitted",
                        "phase": "DAY_VOTE:1:0",
                        "visibility": "PUBLIC",
                        "actor_player_id": "P05",
                        "payload": {"target": "P01", "is_abstain": False},
                    },
                    {
                        "event_type": "ActionTimedOut",
                        "phase": "DAY_VOTE:1:0",
                        "visibility": "SYSTEM",
                        "actor_player_id": "P06",
                        "payload": {"expected_action_type": "VOTE", "defaulted_to": "ABSTAIN"},
                    },
                    _terminated_body(winner),
                ],
            )

    return league.id, ab_a.id, ab_b.id, pv.version


async def test_leaderboard_happy_path_sorted_by_conservative_score(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, ab_a, ab_b, pv_version = await _build_league_with_two_agents(session_factory)

    response = await client.get(f"/leagues/{league_id}/leaderboard")
    assert response.status_code == 200, response.text
    body = response.json()

    assert isinstance(body["leaderboard_id"], str)
    assert body["leaderboard_id"]  # non-empty
    assert body["ruleset_id"] == mini7_v1.RULESET_ID
    assert body["prompt_version"] == pv_version
    assert body["rating_model"] == "openskill_plackett_luce_v1"

    entries = body["entries"]
    assert len(entries) == 2
    assert entries[0]["conservative_score"] >= entries[1]["conservative_score"]

    # B's ratings are stronger (mu=30, sigma=4 → 18.0) than A's (mu=26, sigma=5 → 11.0).
    assert entries[0]["agent_build_id"] == str(ab_b)
    assert entries[1]["agent_build_id"] == str(ab_a)


async def test_leaderboard_entry_fields_and_provisional(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, ab_a, ab_b, _ = await _build_league_with_two_agents(
        session_factory, winners=("TOWN", "TOWN", "MAFIA")
    )

    response = await client.get(f"/leagues/{league_id}/leaderboard")
    assert response.status_code == 200, response.text
    by_ab = {entry["agent_build_id"]: entry for entry in response.json()["entries"]}

    a_entry = by_ab[str(ab_a)]
    b_entry = by_ab[str(ab_b)]

    # Every required field is present.
    required = {
        "agent_build_id",
        "display_name",
        "games",
        "wins",
        "draws",
        "losses",
        "mu",
        "sigma",
        "conservative_score",
        "timeout_rate",
        "invalid_action_rate",
        "public_message_avg_chars",
        "role_family_breakdown",
        "provisional",
    }
    for entry in (a_entry, b_entry):
        assert required.issubset(entry.keys())
        assert entry["role_family_breakdown"] == {}
        # Only 3 games — total below the 30-game threshold for everyone.
        assert entry["provisional"] is True

    # A: 4 seats per game (P01-P04: 2 mafia, 2 town) x 3 games = 12 seat-games.
    # Two games TOWN wins, one MAFIA wins.
    #   Game 1 (TOWN): A mafia seats (P01,P02) lose; A town seats (P03,P04) win  → 2W 2L
    #   Game 2 (TOWN): same                                                       → 2W 2L
    #   Game 3 (MAFIA): A mafia seats win; A town seats lose                      → 2W 2L
    # Totals for A: games=12, wins=6, losses=6, draws=0, mafia=6, town=6.
    assert a_entry["games"] == 12
    assert a_entry["wins"] == 6
    assert a_entry["losses"] == 6
    assert a_entry["draws"] == 0

    # B: 3 seats per game (P05-P07, all town) x 3 games = 9 seat-games.
    #   Two TOWN wins → 6 wins, one MAFIA → 3 losses.
    assert b_entry["games"] == 9
    assert b_entry["wins"] == 6
    assert b_entry["losses"] == 3
    assert b_entry["draws"] == 0

    # Rating fields piped through from the inserted Rating rows.
    assert a_entry["mu"] == 26.0
    assert a_entry["sigma"] == 5.0
    assert a_entry["conservative_score"] == 26.0 - 3.0 * 5.0
    assert b_entry["mu"] == 30.0
    assert b_entry["sigma"] == 4.0
    assert b_entry["conservative_score"] == 30.0 - 3.0 * 4.0

    # Per-game we emit 1 PublicMessage (P03/A) + 1 Vote (P05/B) + 1 ActionTimedOut (P06/B).
    # Submission denominator is per-AB across all games:
    #   A: 1 PublicMessage per game x 3 = 3 attempts, 0 timeouts, 0 invalids.
    #   B: 1 Vote + 1 ActionTimedOut per game = 2 attempts x 3 = 6 attempts, 3 timeouts.
    assert a_entry["timeout_rate"] == 0.0
    assert a_entry["invalid_action_rate"] == 0.0
    assert b_entry["timeout_rate"] == 0.5
    assert b_entry["invalid_action_rate"] == 0.0

    # A made 3 PublicMessages of "hello" (5 chars each) → 5.0 avg.
    # B made no PublicMessages → 0.0.
    assert a_entry["public_message_avg_chars"] == 5.0
    assert b_entry["public_message_avg_chars"] == 0.0


async def test_leaderboard_empty_when_no_terminal_games(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league = await leagues_repo.create(
            session, name="Empty", ruleset_id=mini7_v1.RULESET_ID, ranked=True
        )
        league_id = league.id

    response = await client.get(f"/leagues/{league_id}/leaderboard")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entries"] == []
    assert body["ruleset_id"] == mini7_v1.RULESET_ID
    assert body["rating_model"] == "openskill_plackett_luce_v1"
    # No gauntlet → no prompt version. The contract still returns a string.
    assert body["prompt_version"] == ""


async def test_leaderboard_unknown_league_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/leagues/{uuid.uuid4()}/leaderboard")
    assert response.status_code == 404
