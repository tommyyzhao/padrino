"""US-040: gauntlet completion + provisional flag tests.

Validates :func:`padrino.gauntlets.completion.finalize_gauntlet_if_done`:

* Returns ``None`` when at least one child game is not yet terminal.
* Marks the gauntlet ``COMPLETED`` once every child game has a
  ``GameTerminated`` event.
* Computes the per-agent_build ``provisional`` flag (league-scoped) from the
  thresholds ``total_games >= 30 AND mafia_games >= 5 AND town_games >= 15``.
* Computes aggregate diagnostics over the gauntlet's child games:
  ``games_completed``, ``timeout_rate``, ``invalid_action_rate``, and
  ``average_public_message_chars``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.core.engine.hashing import GENESIS_HASH, compute_event_hash
from padrino.core.enums import Faction
from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import Gauntlet
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
from padrino.gauntlets.completion import finalize_gauntlet_if_done
from padrino.gauntlets.scheduler import create_gauntlet


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


async def _seed_world(
    session: AsyncSession,
    *,
    roster_size: int = mini7_v1.PLAYER_COUNT,
    ph: str = "ph",
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
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
    pv = await prompt_versions_repo.create(
        session,
        ruleset_id=mini7_v1.RULESET_ID,
        version="v1",
        system_prompt="sys",
        developer_prompt="dev",
        response_schema={"type": "object"},
        prompt_hash=f"{ph}-{uuid.uuid4().hex}",
    )
    league = await leagues_repo.create(
        session, name="L", ruleset_id=mini7_v1.RULESET_ID, ranked=True
    )
    roster: list[uuid.UUID] = []
    for i in range(roster_size):
        ab = await agent_builds_repo.create(
            session,
            display_name=f"seat-{i}",
            model_config_id=mc.id,
            prompt_version_id=pv.id,
            adapter_version="2026.05",
            inference_params={},
            active=True,
        )
        roster.append(ab.id)
    return league.id, pv.id, roster


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


def _terminated_body(
    winner: str = "TOWN", reason: str = "town_eliminated_all_mafia"
) -> dict[str, Any]:
    return {
        "event_type": "GameTerminated",
        "phase": "TERMINAL",
        "visibility": "PUBLIC",
        "actor_player_id": None,
        "payload": {"winner": winner, "reason": reason},
    }


async def _seed_seats_for_game(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    roster: list[uuid.UUID],
    mafia_indices: tuple[int, int] = (0, 1),
) -> dict[str, Faction]:
    """Add 7 GameSeat rows. Roster slot i → public_player_id = P{i+1:02d}."""
    factions: dict[str, Faction] = {}
    for i, ab_id in enumerate(roster):
        sid = f"P{i + 1:02d}"
        faction = Faction.MAFIA if i in mafia_indices else Faction.TOWN
        factions[sid] = faction
        await games_repo.add_seat(
            session,
            game_id=game_id,
            public_player_id=sid,
            seat_index=i,
            agent_build_id=ab_id,
            role="MAFIA_GOON" if faction is Faction.MAFIA else "VILLAGER",
            faction=faction.value,
            alive=True,
        )
    return factions


async def _terminate_game(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    winner: str = "TOWN",
) -> None:
    await _append_chained(session, game_id=game_id, bodies=[_terminated_body(winner=winner)])


async def test_returns_none_when_some_games_not_terminal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session, ph="midprog")

    async with session_factory() as session:
        gauntlet = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=3,
            gauntlet_seed="midprog",
            roster=roster,
        )

    # Terminate only the first of the three child games.
    async with session_factory() as session, session.begin():
        await _seed_seats_for_game(session, game_id=gauntlet.game_ids[0], roster=roster)
        await _terminate_game(session, game_id=gauntlet.game_ids[0])

    async with session_factory() as session:
        result = await finalize_gauntlet_if_done(session, gauntlet.gauntlet_id)
    assert result is None

    async with session_factory() as session:
        g = await session.get(Gauntlet, gauntlet.gauntlet_id)
        assert g is not None
        assert g.status == "PENDING"
        assert g.completed_at is None


async def test_returns_none_when_gauntlet_is_unknown(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        result = await finalize_gauntlet_if_done(session, uuid.uuid4())
    assert result is None


async def test_marks_completed_when_all_games_terminal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session, ph="alldone")

    async with session_factory() as session:
        gauntlet = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=2,
            gauntlet_seed="alldone",
            roster=roster,
        )

    async with session_factory() as session, session.begin():
        for gid in gauntlet.game_ids:
            await _seed_seats_for_game(session, game_id=gid, roster=roster)
            await _terminate_game(session, game_id=gid)

    async with session_factory() as session:
        result = await finalize_gauntlet_if_done(session, gauntlet.gauntlet_id)
    assert result is not None
    assert result.gauntlet_id == gauntlet.gauntlet_id
    assert result.status == "COMPLETED"
    assert result.diagnostics.games_completed == 2

    async with session_factory() as session:
        g = await session.get(Gauntlet, gauntlet.gauntlet_id)
        assert g is not None
        assert g.status == "COMPLETED"
        assert g.completed_at is not None


async def test_idempotent_when_already_completed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session, ph="idem")

    async with session_factory() as session:
        gauntlet = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=1,
            gauntlet_seed="idem",
            roster=roster,
        )

    async with session_factory() as session, session.begin():
        for gid in gauntlet.game_ids:
            await _seed_seats_for_game(session, game_id=gid, roster=roster)
            await _terminate_game(session, game_id=gid)

    async with session_factory() as session:
        first = await finalize_gauntlet_if_done(session, gauntlet.gauntlet_id)
    assert first is not None

    async with session_factory() as session:
        second = await finalize_gauntlet_if_done(session, gauntlet.gauntlet_id)
    assert second is None


async def test_provisional_true_when_below_thresholds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session, ph="prov-true")

    async with session_factory() as session:
        gauntlet = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=2,
            gauntlet_seed="prov-true",
            roster=roster,
        )

    async with session_factory() as session, session.begin():
        for gid in gauntlet.game_ids:
            await _seed_seats_for_game(session, game_id=gid, roster=roster)
            await _terminate_game(session, game_id=gid)

    async with session_factory() as session:
        result = await finalize_gauntlet_if_done(session, gauntlet.gauntlet_id)
    assert result is not None
    assert set(result.provisional_by_agent_build) == set(roster)
    for ab_id in roster:
        entry = result.provisional_by_agent_build[ab_id]
        assert entry.total_games == 2
        # First two roster slots are mafia in our test seeding.
        if ab_id in roster[:2]:
            assert entry.mafia_games == 2
            assert entry.town_games == 0
        else:
            assert entry.mafia_games == 0
            assert entry.town_games == 2
        assert entry.provisional is True


async def test_provisional_false_when_thresholds_met(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Single agent_build with enough seats across many games to clear thresholds."""
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session, ph="prov-false")
    # Pick one agent_build to test thresholds against; we'll pad its game count
    # by manufacturing extra terminal games in the same league.
    target_ab = roster[2]

    async with session_factory() as session:
        gauntlet = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=1,
            gauntlet_seed="prov-false",
            roster=roster,
        )

    async with session_factory() as session, session.begin():
        for gid in gauntlet.game_ids:
            await _seed_seats_for_game(session, game_id=gid, roster=roster)
            await _terminate_game(session, game_id=gid)

    # Add 19 more terminal town-faction games + 5 mafia-faction games for target.
    async with session_factory() as session, session.begin():
        for n in range(19):
            game = await games_repo.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                game_seed=f"extra-town-{n}",
                gauntlet_id=gauntlet.gauntlet_id,
            )
            await games_repo.add_seat(
                session,
                game_id=game.id,
                public_player_id="P01",
                seat_index=0,
                agent_build_id=target_ab,
                role="VILLAGER",
                faction=Faction.TOWN.value,
            )
            await _terminate_game(session, game_id=game.id)
        for n in range(5):
            game = await games_repo.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                game_seed=f"extra-mafia-{n}",
                gauntlet_id=gauntlet.gauntlet_id,
            )
            await games_repo.add_seat(
                session,
                game_id=game.id,
                public_player_id="P01",
                seat_index=0,
                agent_build_id=target_ab,
                role="MAFIA_GOON",
                faction=Faction.MAFIA.value,
            )
            await _terminate_game(session, game_id=game.id)

    async with session_factory() as session:
        result = await finalize_gauntlet_if_done(session, gauntlet.gauntlet_id)
    assert result is not None
    entry = result.provisional_by_agent_build[target_ab]
    # 1 (from gauntlet, town) + 19 (extra town) + 5 (extra mafia) = 25 total.
    # Wait: target_ab is roster[2] which is town in the gauntlet game (mafia_indices=(0,1)).
    assert entry.total_games == 25
    assert entry.town_games == 20
    assert entry.mafia_games == 5
    # 25 < 30 → still provisional even though faction floors are met.
    assert entry.provisional is True

    # A peer that played only the original gauntlet game stays provisional too.
    other_entry = result.provisional_by_agent_build[roster[3]]
    assert other_entry.total_games == 1
    assert other_entry.provisional is True


async def test_provisional_threshold_boundary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Exactly 30 games, 5 mafia, 15 town → not provisional. Drop one → provisional."""
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session, ph="prov-bound")
    target_ab = roster[0]

    async with session_factory() as session:
        gauntlet = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=1,
            gauntlet_seed="prov-bound",
            roster=roster,
        )

    # Manually terminate the lone gauntlet game — but with target_ab in slot 0
    # they are mafia in our seeding (mafia_indices=(0,1)).
    async with session_factory() as session, session.begin():
        for gid in gauntlet.game_ids:
            await _seed_seats_for_game(session, game_id=gid, roster=roster)
            await _terminate_game(session, game_id=gid)

    # Goal for target_ab: 5 mafia (1 from gauntlet + 4 extras) and 15 town.
    async with session_factory() as session, session.begin():
        for n in range(15):
            game = await games_repo.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                game_seed=f"b-town-{n}",
                gauntlet_id=gauntlet.gauntlet_id,
            )
            await games_repo.add_seat(
                session,
                game_id=game.id,
                public_player_id="P01",
                seat_index=0,
                agent_build_id=target_ab,
                role="VILLAGER",
                faction=Faction.TOWN.value,
            )
            await _terminate_game(session, game_id=game.id)
        for n in range(4):
            game = await games_repo.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                game_seed=f"b-mafia-{n}",
                gauntlet_id=gauntlet.gauntlet_id,
            )
            await games_repo.add_seat(
                session,
                game_id=game.id,
                public_player_id="P01",
                seat_index=0,
                agent_build_id=target_ab,
                role="MAFIA_GOON",
                faction=Faction.MAFIA.value,
            )
            await _terminate_game(session, game_id=game.id)
        # Add 10 more town games to reach 30 total (15 town + 5 mafia + 10 town = 30).
        for n in range(10):
            game = await games_repo.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                game_seed=f"b-town2-{n}",
                gauntlet_id=gauntlet.gauntlet_id,
            )
            await games_repo.add_seat(
                session,
                game_id=game.id,
                public_player_id="P01",
                seat_index=0,
                agent_build_id=target_ab,
                role="VILLAGER",
                faction=Faction.TOWN.value,
            )
            await _terminate_game(session, game_id=game.id)

    async with session_factory() as session:
        result = await finalize_gauntlet_if_done(session, gauntlet.gauntlet_id)
    assert result is not None
    entry = result.provisional_by_agent_build[target_ab]
    assert entry.total_games == 30
    assert entry.mafia_games == 5
    assert entry.town_games == 25
    assert entry.provisional is False


async def test_diagnostics_aggregate_over_child_games(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session, ph="diag")

    async with session_factory() as session:
        gauntlet = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=2,
            gauntlet_seed="diag",
            roster=roster,
        )

    async with session_factory() as session, session.begin():
        await _seed_seats_for_game(session, game_id=gauntlet.game_ids[0], roster=roster)
        await _seed_seats_for_game(session, game_id=gauntlet.game_ids[1], roster=roster)
        # Game 0: 1 PublicMessage("hello"), 1 Vote, 1 ActionTimedOut.
        await _append_chained(
            session,
            game_id=gauntlet.game_ids[0],
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
                    "actor_player_id": "P03",
                    "payload": {"target": "P01", "is_abstain": False},
                },
                {
                    "event_type": "ActionTimedOut",
                    "phase": "DAY_VOTE:1:0",
                    "visibility": "SYSTEM",
                    "actor_player_id": "P04",
                    "payload": {
                        "expected_action_type": "VOTE",
                        "defaulted_to": "ABSTAIN",
                    },
                },
                _terminated_body(),
            ],
        )
        # Game 1: 1 PublicMessage("hi!!"), 1 OutputInvalid.
        await _append_chained(
            session,
            game_id=gauntlet.game_ids[1],
            bodies=[
                {
                    "event_type": "PublicMessageSubmitted",
                    "phase": "DAY_DISCUSSION:1:1",
                    "visibility": "PUBLIC",
                    "actor_player_id": "P05",
                    "payload": {"text": "hi!!", "round_index": 1},
                },
                {
                    "event_type": "OutputInvalid",
                    "phase": "DAY_DISCUSSION:1:1",
                    "visibility": "SYSTEM",
                    "actor_player_id": "P06",
                    "payload": {
                        "reason": "schema_violation",
                        "validation_errors": ["bad json"],
                    },
                },
                _terminated_body(winner="MAFIA"),
            ],
        )

    async with session_factory() as session:
        result = await finalize_gauntlet_if_done(session, gauntlet.gauntlet_id)
    assert result is not None
    diag = result.diagnostics
    assert diag.games_completed == 2
    # Submission/failure denominator across both games:
    #   game0: PublicMessage + Vote + ActionTimedOut = 3
    #   game1: PublicMessage + OutputInvalid = 2
    #   total = 5
    assert diag.timeout_rate == pytest.approx(1 / 5)
    assert diag.invalid_action_rate == pytest.approx(1 / 5)
    # Public messages: "hello"(5) + "hi!!"(4) → 9 chars over 2 messages.
    assert diag.average_public_message_chars == pytest.approx(9 / 2)


async def test_diagnostics_zero_when_no_action_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session, ph="diag-empty")

    async with session_factory() as session:
        gauntlet = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=1,
            gauntlet_seed="diag-empty",
            roster=roster,
        )

    async with session_factory() as session, session.begin():
        for gid in gauntlet.game_ids:
            await _seed_seats_for_game(session, game_id=gid, roster=roster)
            await _terminate_game(session, game_id=gid)

    async with session_factory() as session:
        result = await finalize_gauntlet_if_done(session, gauntlet.gauntlet_id)
    assert result is not None
    diag = result.diagnostics
    assert diag.games_completed == 1
    assert diag.timeout_rate == 0.0
    assert diag.invalid_action_rate == 0.0
    assert diag.average_public_message_chars == 0.0
