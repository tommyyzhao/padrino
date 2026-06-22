"""US-125: human-lane games are SEGREGATED from the scientific benchmark ELO.

A human game must NEVER touch the sacred scientific ``ratings`` / ``rating_events``
tables, and the dormant sibling ``human_rating`` / ``human_rating_event`` tables
must exist but stay empty in v1 (casual). This module proves all three by driving
a real game to terminal on the humans-included league and asserting ZERO rows
land anywhere ratings could be written.

Two complementary proofs:

* The casual humans-included path (``ranked=False``) writes nothing.
* Even if a future bug set ``ranked=True`` on a human-lane game, the presence of
  a HUMAN seat (a seat with no ``agent_build_id``) makes ``_should_apply_ratings``
  fail closed, so the scientific tables still stay empty.

It also asserts the discriminator + dormant-schema shape: the single
``Humans-Included League`` row is ``ranked=False`` / ``kind=HUMANS_INCLUDED`` and
is queryable apart from scientific leagues, and the sibling tables exist.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, LeagueKind, RatingContextKind, Role
from padrino.core.rulesets import bench10_v1, mini7_v1, roleblock10_v1
from padrino.core.rulesets.canonicality import (
    assert_ruleset_canonical_pure,
    canonical_team_ranks_for_outcome,
)
from padrino.db.models import (
    AgentBuild,
    HumanRating,
    HumanRatingEvent,
    League,
    PlacementRating,
    PlacementRatingEvent,
    Rating,
    RatingContext,
    RatingEvent,
)
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
from padrino.db.repositories import rating_contexts as rating_contexts_repo
from padrino.llm.mock import DeterministicMockAdapter
from padrino.ratings.openskill_service import (
    GameResult,
    PlacementGameResult,
    update_placement_ratings_for_game,
    update_ratings_for_game,
)
from padrino.runner.game_runner import (
    GameConfig,
    GamePersistence,
    run_game,
)
from tests.conftest import make_town_win_script

_GAME_SEED = "seed-segregation-001"


def _split_factions() -> tuple[list[str], list[str], str, str]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return mafia, town, doctor, detective


async def _seed_human_lane_game(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    hash_prefix: str,
    human_seat_ids: set[str],
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    """Seed the humans-included league + a human-lane game.

    ``human_seat_ids`` are NOT given an agent build (they are human-occupied);
    the remaining seats get an AI build. Returns
    ``(league_id, game_id, agent_builds_by_ai_seat)``.
    """
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
        builds: dict[str, uuid.UUID] = {}
        for i in range(mini7_v1.PLAYER_COUNT):
            seat_id = f"P{i + 1:02d}"
            if seat_id in human_seat_ids:
                continue
            pv = await prompt_versions_repo.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                version=f"v{i + 1}",
                system_prompt="sys",
                developer_prompt="dev",
                response_schema={"type": "object"},
                prompt_hash=f"{hash_prefix}-{i}",
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
            builds[seat_id] = ab.id

        league = await leagues_repo.get_or_create_humans_included(
            session, ruleset_id=mini7_v1.RULESET_ID
        )
        game = await games_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_GAME_SEED,
            status="RUNNING",
        )
        league_id = league.id
        game_id = game.id
    return league_id, game_id, builds


async def _count_all_rating_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int, int, int]:
    async with session_factory() as session:
        ratings = (await session.execute(select(func.count()).select_from(Rating))).scalar_one()
        rating_events = (
            await session.execute(select(func.count()).select_from(RatingEvent))
        ).scalar_one()
        human_ratings = (
            await session.execute(select(func.count()).select_from(HumanRating))
        ).scalar_one()
        human_rating_events = (
            await session.execute(select(func.count()).select_from(HumanRatingEvent))
        ).scalar_one()
    return ratings, rating_events, human_ratings, human_rating_events


async def _seed_scientific_all_ai_game(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    hash_prefix: str,
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    """Seed an all-AI scientific mini7 game for canonical rating gates."""
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
        builds: dict[str, uuid.UUID] = {}
        for i in range(mini7_v1.PLAYER_COUNT):
            seat_id = f"P{i + 1:02d}"
            pv = await prompt_versions_repo.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                version=f"canonical-{i + 1}",
                system_prompt="sys",
                developer_prompt="dev",
                response_schema={"type": "object"},
                prompt_hash=f"{hash_prefix}-{i}",
            )
            ab = await agent_builds_repo.create(
                session,
                display_name=f"canonical-build-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="2026.05",
                inference_params={"temperature": 0.7},
                active=True,
            )
            builds[seat_id] = ab.id
        league = await leagues_repo.create(
            session, name="scientific", ruleset_id=mini7_v1.RULESET_ID, ranked=True
        )
        game = await games_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=f"{hash_prefix}-seed",
            status="COMPLETED",
        )
        await rating_contexts_repo.ensure_declared_context(session, ruleset_id=mini7_v1.RULESET_ID)
    return league.id, game.id, builds


async def test_humans_included_league_is_discriminated(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The single humans-included league is ranked=False / kind=HUMANS_INCLUDED.

    It must be queryable apart from scientific leagues, and get-or-create is
    idempotent (one row only).
    """
    async with session_factory() as session, session.begin():
        scientific = await leagues_repo.create(
            session, name="bench", ruleset_id=mini7_v1.RULESET_ID, ranked=True
        )
        human_a = await leagues_repo.get_or_create_humans_included(
            session, ruleset_id=mini7_v1.RULESET_ID
        )
        human_b = await leagues_repo.get_or_create_humans_included(
            session, ruleset_id=mini7_v1.RULESET_ID
        )

    assert human_a.id == human_b.id  # get-or-create is idempotent — single row.
    assert scientific.kind == LeagueKind.SCIENTIFIC.value
    assert human_a.kind == LeagueKind.HUMANS_INCLUDED.value
    assert human_a.ranked is False
    assert human_a.name == leagues_repo.HUMANS_INCLUDED_LEAGUE_NAME

    async with session_factory() as session:
        human_leagues = (
            (
                await session.execute(
                    select(League).where(League.kind == LeagueKind.HUMANS_INCLUDED.value)
                )
            )
            .scalars()
            .all()
        )
        scientific_leagues = (
            (
                await session.execute(
                    select(League).where(League.kind == LeagueKind.SCIENTIFIC.value)
                )
            )
            .scalars()
            .all()
        )
    assert [lg.id for lg in human_leagues] == [human_a.id]
    assert [lg.id for lg in scientific_leagues] == [scientific.id]


async def test_humans_included_league_is_scoped_by_ruleset(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Each ruleset gets its own dormant humans-included league."""
    async with session_factory() as session, session.begin():
        mini = await leagues_repo.get_or_create_humans_included(
            session, ruleset_id=mini7_v1.RULESET_ID
        )
        bench = await leagues_repo.get_or_create_humans_included(
            session, ruleset_id=bench10_v1.RULESET_ID
        )
        mini_again = await leagues_repo.get_or_create_humans_included(
            session, ruleset_id=mini7_v1.RULESET_ID
        )

    assert mini.id != bench.id
    assert mini.id == mini_again.id
    assert mini.ruleset_id == mini7_v1.RULESET_ID
    assert bench.ruleset_id == bench10_v1.RULESET_ID
    assert mini.kind == LeagueKind.HUMANS_INCLUDED.value
    assert bench.kind == LeagueKind.HUMANS_INCLUDED.value


async def test_human_lane_game_writes_zero_rating_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A completed casual human-lane game writes ZERO rows to any rating table."""
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    # Two seats are human-occupied (no agent build).
    human_seats = {town[0], mafia[0]}
    league_id, game_id, ai_builds = await _seed_human_lane_game(
        session_factory, hash_prefix="us125-casual", human_seat_ids=human_seats
    )

    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=game_id,
        agent_builds=ai_builds,
        league_id=league_id,
    )
    outcome = await run_game(
        GameConfig(game_id="G-SEG", game_seed=_GAME_SEED, timeout_s=1.0),
        DeterministicMockAdapter(script),
        ranked=False,  # human lane is ALWAYS casual.
        persistence=persistence,
    )
    assert outcome.final_state.terminal_result == "TOWN"

    counts = await _count_all_rating_rows(session_factory)
    assert counts == (0, 0, 0, 0)


async def test_human_seat_fails_closed_even_if_ranked_true(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Defense in depth: a HUMAN seat keeps scientific tables empty even if ranked.

    A human-lane game should never be ranked, but if a future bug set
    ``ranked=True`` the presence of a human seat (not in ``agent_builds``) must
    still keep the scientific rating tables empty — segregation fails closed.
    """
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    human_seats = {town[0]}
    league_id, game_id, ai_builds = await _seed_human_lane_game(
        session_factory, hash_prefix="us125-failclosed", human_seat_ids=human_seats
    )

    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=game_id,
        agent_builds=ai_builds,
        league_id=league_id,
    )
    outcome = await run_game(
        GameConfig(game_id="G-SEG-FC", game_seed=_GAME_SEED, timeout_s=1.0),
        DeterministicMockAdapter(script),
        ranked=True,  # deliberately wrong — must still write nothing.
        persistence=persistence,
    )
    assert outcome.final_state.terminal_result == "TOWN"

    counts = await _count_all_rating_rows(session_factory)
    assert counts == (0, 0, 0, 0)


async def test_non_canonical_context_writes_zero_scientific_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A non-canonical context is scored outside the sacred Rating tables."""
    experimental_ruleset = "experimental_placement_v1"
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
        builds: dict[str, uuid.UUID] = {}
        for seat_index in range(7):
            pv = await prompt_versions_repo.create(
                session,
                ruleset_id=experimental_ruleset,
                version=f"v{seat_index}",
                system_prompt="sys",
                developer_prompt="dev",
                response_schema={"type": "object"},
                prompt_hash=f"noncanonical-{seat_index}",
            )
            ab = await agent_builds_repo.create(
                session,
                display_name=f"build-{seat_index}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="2026.05",
                inference_params={"temperature": 0.7},
                active=True,
            )
            builds[f"P{seat_index + 1:02d}"] = ab.id
        league = await leagues_repo.create(
            session,
            name="experimental",
            ruleset_id=experimental_ruleset,
            ranked=True,
        )
        game = await games_repo.create(
            session,
            ruleset_id=experimental_ruleset,
            game_seed="experimental-seed",
            status="COMPLETED",
        )
        session.add(
            RatingContext(
                kind=RatingContextKind.PLACEMENT.value,
                ruleset_id=experimental_ruleset,
                is_canonical=False,
                display_label="Experimental placement",
            )
        )

    seat_factions = {
        "P01": Faction.MAFIA,
        "P02": Faction.MAFIA,
        "P03": Faction.TOWN,
        "P04": Faction.TOWN,
        "P05": Faction.TOWN,
        "P06": Faction.TOWN,
        "P07": Faction.TOWN,
    }
    async with session_factory() as session, session.begin():
        events = await update_ratings_for_game(
            session,
            league_id=league.id,
            game_result=GameResult(
                game_id=game.id,
                winner="TOWN",
                seat_factions=seat_factions,
            ),
            agent_builds_by_seat=builds,
        )

    assert events == []
    counts = await _count_all_rating_rows(session_factory)
    assert counts == (0, 0, 0, 0)

    async with session_factory() as session, session.begin():
        placement_events = await update_placement_ratings_for_game(
            session,
            game_result=PlacementGameResult(
                game_id=game.id,
                winner="TOWN",
                seat_groups={seat_id: faction.value for seat_id, faction in seat_factions.items()},
            ),
            agent_builds_by_seat=builds,
        )

    assert placement_events
    counts = await _count_all_rating_rows(session_factory)
    assert counts == (0, 0, 0, 0)
    async with session_factory() as session:
        placement_rating_count = (
            await session.execute(select(func.count()).select_from(PlacementRating))
        ).scalar_one()
        placement_event_count = (
            await session.execute(select(func.count()).select_from(PlacementRatingEvent))
        ).scalar_one()
    assert placement_rating_count == 7
    assert placement_event_count == 7


def test_canonical_ruleset_purity_gate_introspects_builtin_rulesets() -> None:
    """Canonical ladders admit exactly Town/Mafia outcomes, with draw as a tie."""
    for ruleset in (mini7_v1, bench10_v1, roleblock10_v1):
        assert_ruleset_canonical_pure(ruleset)

    assert canonical_team_ranks_for_outcome("TOWN") == {"TOWN": 1, "MAFIA": 2}
    assert canonical_team_ranks_for_outcome("MAFIA") == {"TOWN": 2, "MAFIA": 1}
    assert canonical_team_ranks_for_outcome("DRAW") == {"TOWN": 1, "MAFIA": 1}


async def test_missing_canonical_context_writes_zero_scientific_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A missing context is unrated; it never defaults into canonical ELO."""
    league_id, game_id, builds = await _seed_scientific_all_ai_game(
        session_factory, hash_prefix="missing-canonical-context"
    )
    seats = assign_roles(_GAME_SEED, mini7_v1)
    seat_factions = {seat.public_player_id: seat.faction for seat in seats}
    async with session_factory() as session, session.begin():
        await session.execute(
            delete(RatingContext).where(RatingContext.ruleset_id == mini7_v1.RULESET_ID)
        )

    async with session_factory() as session, session.begin():
        events = await update_ratings_for_game(
            session,
            league_id=league_id,
            game_result=GameResult(
                game_id=game_id,
                winner="TOWN",
                seat_factions=seat_factions,
            ),
            agent_builds_by_seat=builds,
        )

    assert events == []
    counts = await _count_all_rating_rows(session_factory)
    assert counts == (0, 0, 0, 0)


async def test_canonical_rating_events_carry_required_metadata(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Canonical audit rows are complete enough to prove context purity."""
    league_id, game_id, builds = await _seed_scientific_all_ai_game(
        session_factory, hash_prefix="canonical-event-metadata"
    )
    seats = assign_roles(_GAME_SEED, mini7_v1)
    seat_factions = {seat.public_player_id: seat.faction for seat in seats}

    async with session_factory() as session, session.begin():
        events = await update_ratings_for_game(
            session,
            league_id=league_id,
            game_result=GameResult(
                game_id=game_id,
                winner="MAFIA",
                seat_factions=seat_factions,
            ),
            agent_builds_by_seat=builds,
        )

    assert events
    async with session_factory() as session:
        context = await rating_contexts_repo.get_by_ruleset_kind(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            kind=RatingContextKind.CANONICAL_TEAM,
        )
        assert context is not None
        rows = (await session.execute(select(RatingEvent))).scalars().all()
        build_rows = (
            (
                await session.execute(
                    select(AgentBuild).where(
                        AgentBuild.id.in_({row.agent_build_id for row in rows})
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == mini7_v1.PLAYER_COUNT * 2
    assert {row.ruleset_id for row in rows} == {mini7_v1.RULESET_ID}
    assert {row.rating_context_id for row in rows} == {context.id}
    assert {row.game_seed for row in rows} == {"canonical-event-metadata-seed"}
    assert {row.team_outcome for row in rows} == {"MAFIA"}
    assert {row.agent_build_id for row in rows} == set(builds.values())
    assert {row.public_player_id for row in rows} == set(builds)
    assert build_rows
    assert all(row.model_config_id is not None for row in build_rows)
    assert all(row.prompt_version_id is not None for row in build_rows)
