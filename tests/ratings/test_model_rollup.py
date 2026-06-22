"""US-067: per-model rating rollup tests.

Hand-builds fixtures so the per-model aggregation can be asserted against
a closed-form expectation without driving the full game runner. The story
specifies aggregating across every ``agent_build`` that shares the same
``(provider, model_name, model_version)`` triple; these tests cover:

* Two builds for the same model collapse into one row.
* Two builds for different models stay separate.
* Faction sub-aggregates sum to the global counts.
* Games-weighted aggregation matches the closed-form expectation.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.enums import Faction, Role
from padrino.core.rulesets import mini7_v1
from padrino.db.models import AgentBuild, ModelProvider, Rating
from padrino.db.repositories import (
    agent_builds as agent_builds_repo,
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
from padrino.ratings.model_rollup import (
    detail_for_model,
    entry_to_response,
    model_key_for,
    reset_cache,
    rollup_by_model,
)
from padrino.ratings.openskill_service import (
    INITIAL_MU,
    INITIAL_SIGMA,
    SCOPE_FACTION,
    SCOPE_GLOBAL,
    SCOPE_VALUE_GLOBAL,
)


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    reset_cache()


async def _seed_league(
    session_factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        league = await leagues_repo.create(
            session, name="rollup", ruleset_id=mini7_v1.RULESET_ID, ranked=True
        )
        return league.id


async def _make_provider(session: AsyncSession, name: str) -> ModelProvider:
    return await providers_repo.create(session, name=name, auth_secret_ref=f"env:{name.upper()}")


async def _make_agent_build(
    session: AsyncSession,
    *,
    display_name: str,
    provider_name: str,
    model_name: str,
    model_version: str | None,
    suffix: str,
) -> AgentBuild:
    provider = await _make_provider(session, provider_name)
    mc = await model_configs_repo.create(
        session,
        provider_id=provider.id,
        model_name=model_name,
        model_version=model_version,
        default_temperature=0.7,
        default_top_p=1.0,
        default_max_output_tokens=4096,
        supports_structured_outputs=True,
    )
    pv = await prompt_versions_repo.create(
        session,
        ruleset_id=mini7_v1.RULESET_ID,
        version=f"v-{suffix}",
        system_prompt="sys",
        developer_prompt="dev",
        response_schema={"type": "object"},
        prompt_hash=f"ph-{suffix}-{uuid.uuid4().hex}",
    )
    return await agent_builds_repo.create(
        session,
        display_name=display_name,
        model_config_id=mc.id,
        prompt_version_id=pv.id,
        adapter_version="2026.05",
        inference_params={},
        active=True,
    )


def _insert_rating(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    agent_build_id: uuid.UUID,
    scope_type: str,
    scope_value: str,
    mu: float,
    sigma: float,
    games: int,
) -> None:
    session.add(
        Rating(
            league_id=league_id,
            agent_build_id=agent_build_id,
            scope_type=scope_type,
            scope_value=scope_value,
            mu=mu,
            sigma=sigma,
            conservative_score=mu - 3.0 * sigma,
            games=games,
        )
    )


async def _build_gauntlet_with_seats(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    league_id: uuid.UUID,
    game_outcomes: Sequence[tuple[str, Sequence[tuple[uuid.UUID, Faction | str]]]],
) -> None:
    """Insert one gauntlet + one terminal game per outcome.

    Each ``game_outcomes`` entry is ``(winner, [(agent_build_id, faction), ...])``.
    A fresh gauntlet is created per call (its prompt_version is reused from any
    of the agent builds).
    """

    def _faction_value(faction: Faction | str) -> str:
        return faction.value if isinstance(faction, Faction) else faction

    def _role_for_faction(faction: str) -> str:
        if faction == Faction.MAFIA.value:
            return Role.MAFIA_GOON.value
        if faction == Faction.SERIAL_KILLER.value:
            return Role.SERIAL_KILLER.value
        if faction == Faction.JESTER.value:
            return Role.JESTER.value
        return Role.VILLAGER.value

    async with session_factory() as session, session.begin():
        # Pull one prompt_version_id from an existing agent_build so the
        # gauntlet row passes its FK.
        from sqlalchemy import select

        from padrino.db.models import AgentBuild as _AB

        pv_id = (await session.execute(select(_AB.prompt_version_id).limit(1))).scalar_one_or_none()
        assert pv_id is not None
        gauntlet = await gauntlets_repo.create(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=len(game_outcomes),
            gauntlet_seed="rollup-test-seed",
            ranked=True,
            status="COMPLETED",
        )
        for idx, (winner, seats) in enumerate(game_outcomes):
            game = await games_repo.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                game_seed=f"rollup-{idx}",
                gauntlet_id=gauntlet.id,
                status="COMPLETED",
            )
            await games_repo.update_status(
                session,
                game.id,
                status="COMPLETED",
                terminal_result={"winner": winner, "reason": "scripted", "day_terminated": 2},
            )
            for j, (ab_id, faction) in enumerate(seats):
                faction_value = _faction_value(faction)
                await games_repo.add_seat(
                    session,
                    game_id=game.id,
                    public_player_id=f"P{j + 1:02d}",
                    seat_index=j,
                    agent_build_id=ab_id,
                    role=_role_for_faction(faction_value),
                    faction=faction_value,
                )


async def test_two_builds_for_same_model_collapse_into_one_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id = await _seed_league(session_factory)
    async with session_factory() as session, session.begin():
        # Two builds for the same model identity: provider=p, model=m, version=1.0.
        ab1 = await _make_agent_build(
            session,
            display_name="alpha",
            provider_name="p",
            model_name="m",
            model_version="1.0",
            suffix="a",
        )
        ab2 = await _make_agent_build(
            session,
            display_name="beta",
            provider_name="p",
            model_name="m",
            model_version="1.0",
            suffix="b",
        )
        _insert_rating(
            session,
            league_id=league_id,
            agent_build_id=ab1.id,
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            mu=27.0,
            sigma=4.0,
            games=10,
        )
        _insert_rating(
            session,
            league_id=league_id,
            agent_build_id=ab2.id,
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            mu=29.0,
            sigma=3.0,
            games=20,
        )

    rollup = None
    async with session_factory() as session:
        rollup = await rollup_by_model(session, league_id, mini7_v1.RULESET_ID)

    assert len(rollup.entries) == 1
    entry = rollup.entries[0]
    assert entry.model_key == model_key_for("p", "m", "1.0")
    assert entry.agent_build_count == 2
    # games-weighted mu: (27 * 10 + 29 * 20) / 30 = 28.333...
    expected_mu = (27.0 * 10 + 29.0 * 20) / 30
    assert math.isclose(entry.mu, expected_mu, rel_tol=1e-9)
    # sigma propagation: sqrt((4*10)^2 + (3*20)^2) / 30
    expected_sigma = math.sqrt((4.0 * 10) ** 2 + (3.0 * 20) ** 2) / 30
    assert math.isclose(entry.sigma, expected_sigma, rel_tol=1e-9)
    assert math.isclose(entry.conservative_score, entry.mu - 3.0 * entry.sigma, rel_tol=1e-9)


async def test_two_builds_for_different_models_stay_separate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id = await _seed_league(session_factory)
    async with session_factory() as session, session.begin():
        ab_a = await _make_agent_build(
            session,
            display_name="alpha",
            provider_name="openai",
            model_name="gpt-5",
            model_version=None,
            suffix="a",
        )
        ab_b = await _make_agent_build(
            session,
            display_name="bravo",
            provider_name="anthropic",
            model_name="claude-4.7",
            model_version=None,
            suffix="b",
        )
        # Same mu/sigma so we don't accidentally depend on ordering.
        _insert_rating(
            session,
            league_id=league_id,
            agent_build_id=ab_a.id,
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            mu=27.0,
            sigma=4.0,
            games=10,
        )
        _insert_rating(
            session,
            league_id=league_id,
            agent_build_id=ab_b.id,
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            mu=29.0,
            sigma=3.0,
            games=10,
        )

    async with session_factory() as session:
        rollup = await rollup_by_model(session, league_id, mini7_v1.RULESET_ID)

    assert len(rollup.entries) == 2
    keys = {e.model_key for e in rollup.entries}
    assert keys == {
        model_key_for("openai", "gpt-5", None),
        model_key_for("anthropic", "claude-4.7", None),
    }
    # Higher conservative_score sorts first.
    assert rollup.entries[0].conservative_score >= rollup.entries[1].conservative_score


async def test_faction_subaggregates_sum_to_global_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id = await _seed_league(session_factory)
    async with session_factory() as session, session.begin():
        # Seven seats all share the same model identity so a single bucket
        # aggregates every faction count from the seven seats.
        ab_a = await _make_agent_build(
            session,
            display_name="alpha",
            provider_name="p",
            model_name="m",
            model_version="1.0",
            suffix="a",
        )
        ab_b = await _make_agent_build(
            session,
            display_name="beta",
            provider_name="p",
            model_name="m",
            model_version="1.0",
            suffix="b",
        )
        for ab in (ab_a, ab_b):
            _insert_rating(
                session,
                league_id=league_id,
                agent_build_id=ab.id,
                scope_type=SCOPE_GLOBAL,
                scope_value=SCOPE_VALUE_GLOBAL,
                mu=INITIAL_MU,
                sigma=INITIAL_SIGMA,
                games=1,
            )

    # Game 1: town wins. Seats P01,P02=ab_a (MAFIA), rest=ab_b (TOWN).
    # Game 2: mafia wins. Same layout.
    # Game 3: draw. Same layout.
    async with session_factory() as session:
        # Rebind agent_builds within a fresh session.
        from sqlalchemy import select

        from padrino.db.models import AgentBuild as _AB
        from padrino.db.models import ModelConfig as _MC

        rows = (
            await session.execute(
                select(_AB.id, _AB.display_name)
                .join(_MC, _MC.id == _AB.model_config_id)
                .order_by(_AB.display_name)
            )
        ).all()
        assert len(rows) == 2
        ab_a_id = rows[0][0]
        ab_b_id = rows[1][0]
    assert ab_a_id is not None
    assert ab_b_id is not None

    seats = [
        (ab_a_id, Faction.MAFIA),
        (ab_a_id, Faction.MAFIA),
        (ab_b_id, Faction.TOWN),
        (ab_b_id, Faction.TOWN),
        (ab_b_id, Faction.TOWN),
        (ab_b_id, Faction.TOWN),
        (ab_b_id, Faction.TOWN),
    ]
    await _build_gauntlet_with_seats(
        session_factory,
        league_id=league_id,
        game_outcomes=[
            ("TOWN", seats),
            ("MAFIA", seats),
            ("DRAW", seats),
        ],
    )

    async with session_factory() as session:
        rollup = await rollup_by_model(session, league_id, mini7_v1.RULESET_ID)

    assert len(rollup.entries) == 1
    entry = rollup.entries[0]
    # 2 mafia seats * 3 games + 5 town seats * 3 games = 21 seat-games total.
    assert entry.games == 21
    assert entry.town.games + entry.mafia.games == entry.games
    assert entry.town.games == 15
    assert entry.mafia.games == 6
    # Town: 1 win + 1 loss + 1 draw per seat-game → 5 wins, 5 losses, 5 draws.
    assert entry.town.wins == 5
    assert entry.town.losses == 5
    assert entry.town.draws == 5
    # Mafia: 1 win + 1 loss + 1 draw per seat-game → 2 wins, 2 losses, 2 draws.
    assert entry.mafia.wins == 2
    assert entry.mafia.losses == 2
    assert entry.mafia.draws == 2
    # Global sums match.
    assert entry.wins == entry.town.wins + entry.mafia.wins
    assert entry.losses == entry.town.losses + entry.mafia.losses
    assert entry.draws == entry.town.draws + entry.mafia.draws


async def test_games_weighted_aggregation_matches_closed_form(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Three builds, three different mu/sigma/games triples → known answer."""
    league_id = await _seed_league(session_factory)
    samples = [
        (25.0, 5.0, 4),
        (30.0, 4.0, 8),
        (28.0, 6.0, 3),
    ]
    async with session_factory() as session, session.begin():
        for idx, (mu, sigma, games) in enumerate(samples):
            ab = await _make_agent_build(
                session,
                display_name=f"build-{idx}",
                provider_name="p",
                model_name="m",
                model_version="1.0",
                suffix=f"s{idx}",
            )
            _insert_rating(
                session,
                league_id=league_id,
                agent_build_id=ab.id,
                scope_type=SCOPE_GLOBAL,
                scope_value=SCOPE_VALUE_GLOBAL,
                mu=mu,
                sigma=sigma,
                games=games,
            )

    async with session_factory() as session:
        rollup = await rollup_by_model(session, league_id, mini7_v1.RULESET_ID)

    assert len(rollup.entries) == 1
    entry = rollup.entries[0]
    total_n = sum(n for _, _, n in samples)
    expected_mu = sum(mu * n for mu, _, n in samples) / total_n
    expected_sigma_sq = sum((sigma * n) ** 2 for _, sigma, n in samples)
    expected_sigma = math.sqrt(expected_sigma_sq) / total_n
    assert math.isclose(entry.mu, expected_mu, rel_tol=1e-9)
    assert math.isclose(entry.sigma, expected_sigma, rel_tol=1e-9)
    assert entry.agent_build_count == 3


async def test_detail_returns_builds_and_recent_games(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id = await _seed_league(session_factory)
    async with session_factory() as session, session.begin():
        ab_a = await _make_agent_build(
            session,
            display_name="alpha",
            provider_name="p",
            model_name="m",
            model_version="1.0",
            suffix="a",
        )
        ab_b = await _make_agent_build(
            session,
            display_name="beta",
            provider_name="p",
            model_name="m",
            model_version="1.0",
            suffix="b",
        )
        for ab in (ab_a, ab_b):
            _insert_rating(
                session,
                league_id=league_id,
                agent_build_id=ab.id,
                scope_type=SCOPE_GLOBAL,
                scope_value=SCOPE_VALUE_GLOBAL,
                mu=INITIAL_MU,
                sigma=INITIAL_SIGMA,
                games=1,
            )

    async with session_factory() as session:
        from sqlalchemy import select

        from padrino.db.models import AgentBuild as _AB

        rows = (
            await session.execute(select(_AB.id, _AB.display_name).order_by(_AB.display_name))
        ).all()
        ab_a_id = rows[0][0]
        ab_b_id = rows[1][0]

    seats = [
        (ab_a_id, Faction.MAFIA),
        (ab_a_id, Faction.MAFIA),
        (ab_b_id, Faction.TOWN),
        (ab_b_id, Faction.TOWN),
        (ab_b_id, Faction.TOWN),
        (ab_b_id, Faction.TOWN),
        (ab_b_id, Faction.TOWN),
    ]
    await _build_gauntlet_with_seats(
        session_factory,
        league_id=league_id,
        game_outcomes=[("TOWN", seats), ("MAFIA", seats)],
    )

    async with session_factory() as session:
        detail = await detail_for_model(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            model_key=model_key_for("p", "m", "1.0"),
        )
    assert detail is not None
    assert detail.entry.agent_build_count == 2
    build_names = [b.display_name for b in detail.builds]
    assert build_names == ["alpha", "beta"]
    assert len(detail.recent_game_ids) == 2


async def test_detail_returns_none_when_model_unknown(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id = await _seed_league(session_factory)
    async with session_factory() as session:
        detail = await detail_for_model(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            model_key="nope/nope",
        )
    assert detail is None


async def test_recent_games_capped_at_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id = await _seed_league(session_factory)
    async with session_factory() as session, session.begin():
        ab = await _make_agent_build(
            session,
            display_name="alpha",
            provider_name="p",
            model_name="m",
            model_version="1.0",
            suffix="a",
        )
        _insert_rating(
            session,
            league_id=league_id,
            agent_build_id=ab.id,
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            mu=INITIAL_MU,
            sigma=INITIAL_SIGMA,
            games=1,
        )

    async with session_factory() as session:
        from sqlalchemy import select

        from padrino.db.models import AgentBuild as _AB

        ab_id = (await session.execute(select(_AB.id).limit(1))).scalar_one()

    seats = [(ab_id, Faction.TOWN)] * 7
    outcomes = [("TOWN", seats)] * 30
    await _build_gauntlet_with_seats(
        session_factory,
        league_id=league_id,
        game_outcomes=outcomes,
    )

    async with session_factory() as session:
        detail = await detail_for_model(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            model_key=model_key_for("p", "m", "1.0"),
            recent_game_limit=25,
        )
    assert detail is not None
    assert len(detail.recent_game_ids) == 25


async def test_cache_invalidates_on_rating_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id = await _seed_league(session_factory)
    async with session_factory() as session, session.begin():
        ab = await _make_agent_build(
            session,
            display_name="alpha",
            provider_name="p",
            model_name="m",
            model_version="1.0",
            suffix="a",
        )
        _insert_rating(
            session,
            league_id=league_id,
            agent_build_id=ab.id,
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            mu=25.0,
            sigma=5.0,
            games=4,
        )

    async with session_factory() as session:
        first = await rollup_by_model(session, league_id, mini7_v1.RULESET_ID)

    # Add a second build for the same model identity → cache_tag changes.
    async with session_factory() as session, session.begin():
        from sqlalchemy import select

        from padrino.db.models import AgentBuild as _AB
        from padrino.db.models import ModelConfig as _MC

        mc_id = (await session.execute(select(_MC.id).limit(1))).scalar_one()
        pv_id = (await session.execute(select(_AB.prompt_version_id).limit(1))).scalar_one()
        ab2 = await agent_builds_repo.create(
            session,
            display_name="beta",
            model_config_id=mc_id,
            prompt_version_id=pv_id,
            adapter_version="2026.05",
            inference_params={},
            active=True,
        )
        _insert_rating(
            session,
            league_id=league_id,
            agent_build_id=ab2.id,
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            mu=30.0,
            sigma=4.0,
            games=6,
        )

    async with session_factory() as session:
        second = await rollup_by_model(session, league_id, mini7_v1.RULESET_ID)
    assert second.cache_tag != first.cache_tag
    assert second.entries[0].agent_build_count == 2


async def test_faction_scope_ratings_aggregate_independently(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id = await _seed_league(session_factory)
    async with session_factory() as session, session.begin():
        ab = await _make_agent_build(
            session,
            display_name="alpha",
            provider_name="p",
            model_name="m",
            model_version="1.0",
            suffix="a",
        )
        _insert_rating(
            session,
            league_id=league_id,
            agent_build_id=ab.id,
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            mu=25.0,
            sigma=5.0,
            games=4,
        )
        _insert_rating(
            session,
            league_id=league_id,
            agent_build_id=ab.id,
            scope_type=SCOPE_FACTION,
            scope_value=Faction.TOWN.value,
            mu=27.0,
            sigma=4.5,
            games=3,
        )
        _insert_rating(
            session,
            league_id=league_id,
            agent_build_id=ab.id,
            scope_type=SCOPE_FACTION,
            scope_value=Faction.MAFIA.value,
            mu=22.0,
            sigma=5.5,
            games=1,
        )

    async with session_factory() as session:
        rollup = await rollup_by_model(session, league_id, mini7_v1.RULESET_ID)
    entry = rollup.entries[0]
    assert math.isclose(entry.town.mu, 27.0)
    assert math.isclose(entry.mafia.mu, 22.0)
    # Global rating differs from faction-scoped rating — the rollup keeps them
    # separate.
    assert math.isclose(entry.mu, 25.0)
    assert entry.town.mu != entry.mu


async def test_town_mafia_faction_rollup_preserves_legacy_response_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Existing two-faction consumers keep the same Town/Mafia payload shape."""
    league_id = await _seed_league(session_factory)
    async with session_factory() as session, session.begin():
        ab = await _make_agent_build(
            session,
            display_name="alpha",
            provider_name="p",
            model_name="m",
            model_version="1.0",
            suffix="legacy-factions",
        )
        _insert_rating(
            session,
            league_id=league_id,
            agent_build_id=ab.id,
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            mu=25.0,
            sigma=5.0,
            games=4,
        )
        _insert_rating(
            session,
            league_id=league_id,
            agent_build_id=ab.id,
            scope_type=SCOPE_FACTION,
            scope_value=Faction.TOWN.value,
            mu=27.0,
            sigma=4.5,
            games=3,
        )
        _insert_rating(
            session,
            league_id=league_id,
            agent_build_id=ab.id,
            scope_type=SCOPE_FACTION,
            scope_value=Faction.MAFIA.value,
            mu=22.0,
            sigma=5.5,
            games=1,
        )

    await _build_gauntlet_with_seats(
        session_factory,
        league_id=league_id,
        game_outcomes=[
            (
                Faction.TOWN.value,
                [(ab.id, Faction.TOWN), (ab.id, Faction.TOWN), (ab.id, Faction.MAFIA)],
            ),
            (
                Faction.MAFIA.value,
                [(ab.id, Faction.TOWN), (ab.id, Faction.TOWN), (ab.id, Faction.MAFIA)],
            ),
        ],
    )

    async with session_factory() as session:
        rollup = await rollup_by_model(session, league_id, mini7_v1.RULESET_ID)

    payload = entry_to_response(rollup.entries[0])
    assert payload["town"] == {
        "mu": 27.0,
        "sigma": 4.5,
        "conservative_score": 13.5,
        "games": 4,
        "wins": 2,
        "draws": 0,
        "losses": 2,
    }
    assert payload["mafia"] == {
        "mu": 22.0,
        "sigma": 5.5,
        "conservative_score": 5.5,
        "games": 2,
        "wins": 1,
        "draws": 0,
        "losses": 1,
    }


async def test_rollup_includes_synthetic_forward_compatible_faction_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Forward-compat fixture: current writers do not produce this FACTION row."""
    league_id = await _seed_league(session_factory)
    async with session_factory() as session, session.begin():
        ab = await _make_agent_build(
            session,
            display_name="alpha",
            provider_name="p",
            model_name="m",
            model_version="1.0",
            suffix="third-faction",
        )
        _insert_rating(
            session,
            league_id=league_id,
            agent_build_id=ab.id,
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            mu=26.0,
            sigma=4.0,
            games=3,
        )
        for faction, mu, sigma, games in (
            (Faction.TOWN.value, 25.0, 5.0, 1),
            (Faction.MAFIA.value, 24.0, 5.5, 1),
            (Faction.SERIAL_KILLER.value, 31.0, 3.0, 1),
        ):
            _insert_rating(
                session,
                league_id=league_id,
                agent_build_id=ab.id,
                scope_type=SCOPE_FACTION,
                scope_value=faction,
                mu=mu,
                sigma=sigma,
                games=games,
            )

    await _build_gauntlet_with_seats(
        session_factory,
        league_id=league_id,
        game_outcomes=[
            (
                Faction.SERIAL_KILLER.value,
                [
                    (ab.id, Faction.TOWN),
                    (ab.id, Faction.MAFIA),
                    (ab.id, Faction.SERIAL_KILLER.value),
                ],
            )
        ],
    )

    async with session_factory() as session:
        rollup = await rollup_by_model(session, league_id, mini7_v1.RULESET_ID)

    entry = rollup.entries[0]
    assert set(entry.factions) == {
        Faction.TOWN.value,
        Faction.MAFIA.value,
        Faction.SERIAL_KILLER.value,
    }
    assert math.isclose(entry.factions[Faction.SERIAL_KILLER.value].mu, 31.0)
    assert entry.factions[Faction.SERIAL_KILLER.value].games == 1
    assert entry.factions[Faction.SERIAL_KILLER.value].wins == 1
    assert entry.factions[Faction.TOWN.value].losses == 1
    assert entry.factions[Faction.MAFIA.value].losses == 1


async def test_model_rollup_carries_exact_role_sample_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id = await _seed_league(session_factory)
    async with session_factory() as session, session.begin():
        ab = await _make_agent_build(
            session,
            display_name="alpha",
            provider_name="p",
            model_name="m",
            model_version="1.0",
            suffix="role-samples",
        )
        _insert_rating(
            session,
            league_id=league_id,
            agent_build_id=ab.id,
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            mu=25.0,
            sigma=5.0,
            games=6,
        )

    await _build_gauntlet_with_seats(
        session_factory,
        league_id=league_id,
        game_outcomes=[
            (
                Faction.TOWN.value,
                [(ab.id, Faction.MAFIA), (ab.id, Faction.TOWN), (ab.id, Faction.TOWN)],
            ),
            (
                Faction.MAFIA.value,
                [(ab.id, Faction.MAFIA), (ab.id, Faction.TOWN), (ab.id, Faction.TOWN)],
            ),
        ],
    )

    async with session_factory() as session:
        rollup = await rollup_by_model(session, league_id, mini7_v1.RULESET_ID)

    entry = rollup.entries[0]
    assert set(entry.role_breakdown) == {Role.MAFIA_GOON.value, Role.VILLAGER.value}
    assert entry.role_breakdown[Role.MAFIA_GOON.value].games == 2
    assert entry.role_breakdown[Role.MAFIA_GOON.value].wins == 1
    assert entry.role_breakdown[Role.MAFIA_GOON.value].losses == 1
    assert entry.role_breakdown[Role.VILLAGER.value].games == 4
    assert entry.role_breakdown[Role.VILLAGER.value].wins == 2
    assert entry.role_breakdown[Role.VILLAGER.value].losses == 2
