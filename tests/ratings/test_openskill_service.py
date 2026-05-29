"""US-038: tests for the OpenSkill rating service."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.enums import Faction
from padrino.db.models import AgentBuild, Game, League, Rating, RatingEvent
from padrino.db.repositories import (
    agent_builds,
    games,
    leagues,
    model_configs,
    prompt_versions,
    providers,
)
from padrino.ratings.openskill_service import (
    INITIAL_MU,
    INITIAL_SIGMA,
    GameResult,
    update_ratings_for_game,
)


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    n_builds: int = 7,
    hash_prefix: str = "us038",
) -> tuple[League, list[AgentBuild], Game]:
    """Create a league, ``n_builds`` agent builds, and one game.

    Returns (league, [build_0, build_1, ...], game).
    """
    async with session_factory() as session, session.begin():
        provider = await providers.create(
            session, name="cerebras", auth_secret_ref="CEREBRAS_API_KEY"
        )
        mc = await model_configs.create(
            session,
            provider_id=provider.id,
            model_name="glm-4.7",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        builds: list[AgentBuild] = []
        for i in range(n_builds):
            pv = await prompt_versions.create(
                session,
                ruleset_id="mini7_v1",
                version=f"v{i + 1}",
                system_prompt="sys",
                developer_prompt="dev",
                response_schema={"type": "object"},
                prompt_hash=f"{hash_prefix}-{i}",
            )
            ab = await agent_builds.create(
                session,
                display_name=f"build-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="2026.05",
                inference_params={"temperature": 0.7},
                active=True,
            )
            builds.append(ab)
        league = await leagues.create(session, name="ranked", ruleset_id="mini7_v1", ranked=True)
        game = await games.create(
            session,
            ruleset_id="mini7_v1",
            game_seed="deadbeef",
            status="COMPLETED",
        )
    return league, builds, game


def _seven_seat_layout(
    builds: list[AgentBuild],
) -> tuple[
    dict[str, Faction],
    dict[str, uuid.UUID],
]:
    """Default mini7_v1 layout: P01,P02 = MAFIA; P03..P07 = TOWN."""
    seat_factions: dict[str, Faction] = {
        "P01": Faction.MAFIA,
        "P02": Faction.MAFIA,
        "P03": Faction.TOWN,
        "P04": Faction.TOWN,
        "P05": Faction.TOWN,
        "P06": Faction.TOWN,
        "P07": Faction.TOWN,
    }
    agent_builds_by_seat: dict[str, uuid.UUID] = {f"P{i + 1:02d}": builds[i].id for i in range(7)}
    return seat_factions, agent_builds_by_seat


async def _fetch_ratings(
    session_factory: async_sessionmaker[AsyncSession], league_id: uuid.UUID
) -> dict[tuple[uuid.UUID, str, str], Rating]:
    async with session_factory() as session:
        stmt = select(Rating).where(Rating.league_id == league_id)
        rows = (await session.execute(stmt)).scalars().all()
    return {(r.agent_build_id, r.scope_type, r.scope_value): r for r in rows}


async def test_town_win_winner_mu_up_loser_mu_down(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league, builds, game = await _seed(session_factory, hash_prefix="t-win")
    seat_factions, abs_by_seat = _seven_seat_layout(builds)

    async with session_factory() as session, session.begin():
        events = await update_ratings_for_game(
            session,
            league_id=league.id,
            game_result=GameResult(game_id=game.id, winner="TOWN", seat_factions=seat_factions),
            agent_builds_by_seat=abs_by_seat,
        )

    # 7 seats * 2 scopes (GLOBAL + FACTION) = 14 rating events.
    assert len(events) == 14

    ratings_by_key = await _fetch_ratings(session_factory, league.id)

    # Each agent_build has exactly two scopes recorded.
    by_build: dict[uuid.UUID, list[Rating]] = {}
    for (build_id, _, _), rating in ratings_by_key.items():
        by_build.setdefault(build_id, []).append(rating)
    for build_id in [b.id for b in builds]:
        assert len(by_build[build_id]) == 2

    # GLOBAL scope: town builds mu should rise; mafia builds mu should fall.
    for sid, faction in seat_factions.items():
        ab_id = abs_by_seat[sid]
        global_r = ratings_by_key[(ab_id, "GLOBAL", "global")]
        if faction is Faction.TOWN:
            assert global_r.mu > INITIAL_MU
        else:
            assert global_r.mu < INITIAL_MU
        # Sigma always shrinks after a rated game.
        assert global_r.sigma < INITIAL_SIGMA
        # Conservative score = mu - 3*sigma.
        assert global_r.conservative_score == pytest.approx(global_r.mu - 3.0 * global_r.sigma)
        # Games counter advanced.
        assert global_r.games == 1

    # FACTION scope: town seats live under scope_value='TOWN' with mu↑;
    # mafia seats live under scope_value='MAFIA' with mu↓.
    for sid, faction in seat_factions.items():
        ab_id = abs_by_seat[sid]
        scope_value = "TOWN" if faction is Faction.TOWN else "MAFIA"
        r = ratings_by_key[(ab_id, "FACTION", scope_value)]
        if faction is Faction.TOWN:
            assert r.mu > INITIAL_MU
        else:
            assert r.mu < INITIAL_MU
        assert r.sigma < INITIAL_SIGMA


async def test_mafia_win_winner_mu_up_loser_mu_down(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league, builds, game = await _seed(session_factory, hash_prefix="m-win")
    seat_factions, abs_by_seat = _seven_seat_layout(builds)

    async with session_factory() as session, session.begin():
        await update_ratings_for_game(
            session,
            league_id=league.id,
            game_result=GameResult(game_id=game.id, winner="MAFIA", seat_factions=seat_factions),
            agent_builds_by_seat=abs_by_seat,
        )

    ratings_by_key = await _fetch_ratings(session_factory, league.id)
    for sid, faction in seat_factions.items():
        ab_id = abs_by_seat[sid]
        global_r = ratings_by_key[(ab_id, "GLOBAL", "global")]
        if faction is Faction.MAFIA:
            assert global_r.mu > INITIAL_MU
        else:
            assert global_r.mu < INITIAL_MU
        assert global_r.sigma < INITIAL_SIGMA


async def test_draw_equal_rank_keeps_mu_near_initial(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league, builds, game = await _seed(session_factory, hash_prefix="draw")
    seat_factions, abs_by_seat = _seven_seat_layout(builds)

    async with session_factory() as session, session.begin():
        await update_ratings_for_game(
            session,
            league_id=league.id,
            game_result=GameResult(game_id=game.id, winner="DRAW", seat_factions=seat_factions),
            agent_builds_by_seat=abs_by_seat,
        )

    ratings_by_key = await _fetch_ratings(session_factory, league.id)

    # On equal-rank draw, every team member of the same team gets the same
    # mu/sigma update (all started identical and the rank delta is zero).
    town_mus: set[float] = set()
    mafia_mus: set[float] = set()
    for sid, faction in seat_factions.items():
        ab_id = abs_by_seat[sid]
        r = ratings_by_key[(ab_id, "GLOBAL", "global")]
        assert r.sigma < INITIAL_SIGMA
        if faction is Faction.TOWN:
            town_mus.add(round(r.mu, 9))
        else:
            mafia_mus.add(round(r.mu, 9))
    assert len(town_mus) == 1
    assert len(mafia_mus) == 1


async def test_role_family_scope_not_updated_in_v1(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league, builds, game = await _seed(session_factory, hash_prefix="no-rf")
    seat_factions, abs_by_seat = _seven_seat_layout(builds)

    async with session_factory() as session, session.begin():
        await update_ratings_for_game(
            session,
            league_id=league.id,
            game_result=GameResult(game_id=game.id, winner="TOWN", seat_factions=seat_factions),
            agent_builds_by_seat=abs_by_seat,
        )

    async with session_factory() as session:
        rows = (await session.execute(select(Rating))).scalars().all()

    scope_types = {r.scope_type for r in rows}
    assert scope_types == {"GLOBAL", "FACTION"}
    assert "ROLE_FAMILY" not in scope_types


async def test_rating_events_carry_before_after_values(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league, builds, game = await _seed(session_factory, hash_prefix="audit")
    seat_factions, abs_by_seat = _seven_seat_layout(builds)

    async with session_factory() as session, session.begin():
        events = await update_ratings_for_game(
            session,
            league_id=league.id,
            game_result=GameResult(game_id=game.id, winner="TOWN", seat_factions=seat_factions),
            agent_builds_by_seat=abs_by_seat,
        )

    # Every event records the initial mu/sigma as the "before" snapshot.
    for evt in events:
        assert evt.before_mu == pytest.approx(INITIAL_MU)
        assert evt.before_sigma == pytest.approx(INITIAL_SIGMA)
        assert evt.game_id == game.id
        assert evt.league_id == league.id
        assert evt.scope_type in {"GLOBAL", "FACTION"}

    # And the events landed in rating_events.
    async with session_factory() as session:
        persisted = (await session.execute(select(RatingEvent))).scalars().all()
    assert len(persisted) == len(events)


async def test_second_game_updates_existing_rating_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league, builds, game = await _seed(session_factory, hash_prefix="seq")
    seat_factions, abs_by_seat = _seven_seat_layout(builds)

    async with session_factory() as session, session.begin():
        await update_ratings_for_game(
            session,
            league_id=league.id,
            game_result=GameResult(game_id=game.id, winner="TOWN", seat_factions=seat_factions),
            agent_builds_by_seat=abs_by_seat,
        )

    # Capture intermediate state.
    async with session_factory() as session:
        first_rows = (await session.execute(select(Rating))).scalars().all()
        first_state = {
            (r.agent_build_id, r.scope_type, r.scope_value): (r.mu, r.sigma, r.games)
            for r in first_rows
        }

    # Second game in the same league using the same agent_builds_by_seat.
    async with session_factory() as session, session.begin():
        game_2 = await games.create(
            session,
            ruleset_id="mini7_v1",
            game_seed="cafebabe",
            status="COMPLETED",
        )

    async with session_factory() as session, session.begin():
        await update_ratings_for_game(
            session,
            league_id=league.id,
            game_result=GameResult(game_id=game_2.id, winner="TOWN", seat_factions=seat_factions),
            agent_builds_by_seat=abs_by_seat,
        )

    async with session_factory() as session:
        second_rows = (await session.execute(select(Rating))).scalars().all()

    # Total ratings count unchanged — same scopes, just updated in place.
    assert len(second_rows) == len(first_rows)
    for r in second_rows:
        key = (r.agent_build_id, r.scope_type, r.scope_value)
        prev_mu, prev_sigma, prev_games = first_state[key]
        assert r.games == prev_games + 1
        # Sigma keeps shrinking with more games.
        assert r.sigma < prev_sigma
        # Town built mu rises further; mafia mu drops further.
        seat = next(s for s, ab in abs_by_seat.items() if ab == r.agent_build_id)
        if seat_factions[seat] is Faction.TOWN:
            assert r.mu > prev_mu
        else:
            assert r.mu < prev_mu
