"""US-033: end-to-end tests for the ratings + rating_events repository."""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import AgentBuild, League
from padrino.db.repositories import (
    agent_builds,
    leagues,
    model_configs,
    prompt_versions,
    providers,
)
from padrino.db.repositories import ratings as ratings_repo


async def _seed_league_and_build(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    prompt_hash: str = "rating-hash",
) -> tuple[League, AgentBuild]:
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
        pv = await prompt_versions.create(
            session,
            ruleset_id="mini7_v1",
            version="v1",
            system_prompt="sys",
            developer_prompt="dev",
            response_schema={"type": "object"},
            prompt_hash=prompt_hash,
        )
        ab = await agent_builds.create(
            session,
            display_name="cerebras/glm-4.7@v1",
            model_config_id=mc.id,
            prompt_version_id=pv.id,
            adapter_version="2026.05",
            inference_params={"temperature": 0.7},
            active=True,
        )
        league = await leagues.create(session, name="ranked", ruleset_id="mini7_v1", ranked=True)
    return league, ab


async def test_get_or_create_rating_inserts_when_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league, ab = await _seed_league_and_build(session_factory, prompt_hash="r-create")

    async with session_factory() as session, session.begin():
        rating = await ratings_repo.get_or_create_rating(
            session,
            league_id=league.id,
            agent_build_id=ab.id,
            scope_type="GLOBAL",
            scope_value="GLOBAL",
            initial_mu=25.0,
            initial_sigma=25.0 / 3.0,
            initial_conservative_score=25.0 - 3 * (25.0 / 3.0),
        )
        rating_id = rating.id
        assert rating.mu == pytest.approx(25.0)
        assert rating.sigma == pytest.approx(25.0 / 3.0)
        assert rating.games == 0

    async with session_factory() as session:
        fetched = await session.get(type(rating), rating_id)
        assert fetched is not None
        assert fetched.scope_type == "GLOBAL"
        assert fetched.scope_value == "GLOBAL"


async def test_get_or_create_rating_returns_existing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league, ab = await _seed_league_and_build(session_factory, prompt_hash="r-idem")

    async with session_factory() as session, session.begin():
        first = await ratings_repo.get_or_create_rating(
            session,
            league_id=league.id,
            agent_build_id=ab.id,
            scope_type="FACTION",
            scope_value="TOWN",
            initial_mu=25.0,
            initial_sigma=8.0,
            initial_conservative_score=1.0,
        )
        first_id = first.id

    async with session_factory() as session, session.begin():
        again = await ratings_repo.get_or_create_rating(
            session,
            league_id=league.id,
            agent_build_id=ab.id,
            scope_type="FACTION",
            scope_value="TOWN",
            initial_mu=30.0,  # would-be different value; must be ignored
            initial_sigma=5.0,
            initial_conservative_score=15.0,
        )
        assert again.id == first_id
        # The existing row is preserved; "initial_*" values do not overwrite.
        assert again.mu == pytest.approx(25.0)
        assert again.sigma == pytest.approx(8.0)


async def test_update_rating_persists_new_values(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league, ab = await _seed_league_and_build(session_factory, prompt_hash="r-update")

    async with session_factory() as session, session.begin():
        rating = await ratings_repo.get_or_create_rating(
            session,
            league_id=league.id,
            agent_build_id=ab.id,
            scope_type="GLOBAL",
            scope_value="GLOBAL",
            initial_mu=25.0,
            initial_sigma=25.0 / 3.0,
            initial_conservative_score=0.0,
        )
        rating_id = rating.id

    bumped_at = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    async with session_factory() as session, session.begin():
        updated = await ratings_repo.update_rating(
            session,
            rating_id,
            mu=27.3,
            sigma=6.8,
            conservative_score=6.9,
            games=1,
            updated_at=bumped_at,
        )
        assert updated is not None
        assert updated.mu == pytest.approx(27.3)
        assert updated.sigma == pytest.approx(6.8)
        assert updated.conservative_score == pytest.approx(6.9)
        assert updated.games == 1

    async with session_factory() as session:
        fetched = await session.get(type(rating), rating_id)
        assert fetched is not None
        assert fetched.mu == pytest.approx(27.3)
        assert fetched.games == 1


async def test_update_rating_missing_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        result = await ratings_repo.update_rating(
            session,
            uuid.uuid4(),
            mu=25.0,
            sigma=8.0,
            conservative_score=1.0,
            games=0,
        )
        assert result is None


async def test_record_rating_event_audit_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league, ab = await _seed_league_and_build(session_factory, prompt_hash="r-audit")

    # Seed a game row to satisfy the FK.
    from padrino.db.repositories import games as games_repo

    async with session_factory() as session, session.begin():
        gm = await games_repo.create(session, ruleset_id="mini7_v1", game_seed="audit-seed")
        game_id = gm.id

    async with session_factory() as session, session.begin():
        evt = await ratings_repo.record_rating_event(
            session,
            league_id=league.id,
            game_id=game_id,
            agent_build_id=ab.id,
            scope_type="GLOBAL",
            scope_value="GLOBAL",
            before_mu=25.0,
            before_sigma=25.0 / 3.0,
            after_mu=27.3,
            after_sigma=6.8,
        )
        evt_id = evt.id
        assert evt.before_mu == pytest.approx(25.0)
        assert evt.after_mu == pytest.approx(27.3)

    async with session_factory() as session:
        rows = await ratings_repo.list_rating_events(session, league_id=league.id)
        assert [r.id for r in rows] == [evt_id]
        scoped = await ratings_repo.list_rating_events(session, game_id=game_id)
        assert [r.id for r in scoped] == [evt_id]
        none_match = await ratings_repo.list_rating_events(session, agent_build_id=uuid.uuid4())
        assert none_match == []


async def test_rating_unique_constraint(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league, ab = await _seed_league_and_build(session_factory, prompt_hash="r-unique")

    async with session_factory() as session, session.begin():
        await ratings_repo.get_or_create_rating(
            session,
            league_id=league.id,
            agent_build_id=ab.id,
            scope_type="ROLE_FAMILY",
            scope_value="MAFIA_TEAM",
            initial_mu=25.0,
            initial_sigma=8.0,
            initial_conservative_score=1.0,
        )

    # Direct INSERT bypassing the get-or-create path must hit the UNIQUE.
    from padrino.db.models import Rating

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            session.add(
                Rating(
                    league_id=league.id,
                    agent_build_id=ab.id,
                    scope_type="ROLE_FAMILY",
                    scope_value="MAFIA_TEAM",
                    mu=10.0,
                    sigma=5.0,
                    conservative_score=-5.0,
                    games=0,
                )
            )
            await session.flush()


async def test_rating_event_ordering(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league, ab = await _seed_league_and_build(session_factory, prompt_hash="r-order")
    from padrino.db.repositories import games as games_repo

    async with session_factory() as session, session.begin():
        g1 = await games_repo.create(session, ruleset_id="mini7_v1", game_seed="o1")
        g2 = await games_repo.create(session, ruleset_id="mini7_v1", game_seed="o2")
        game_ids = [g1.id, g2.id]

    async with session_factory() as session, session.begin():
        await ratings_repo.record_rating_event(
            session,
            league_id=league.id,
            game_id=game_ids[0],
            agent_build_id=ab.id,
            scope_type="GLOBAL",
            scope_value="GLOBAL",
            before_mu=25.0,
            before_sigma=8.0,
            after_mu=26.0,
            after_sigma=7.5,
        )

    # Force a clear time gap so created_at differs even on SQLite's seconds-ish clock.
    async with session_factory() as session, session.begin():
        first = await ratings_repo.list_rating_events(session, league_id=league.id)
        assert len(first) == 1
        first[0].created_at = datetime.now(UTC) - timedelta(seconds=10)

    async with session_factory() as session, session.begin():
        await ratings_repo.record_rating_event(
            session,
            league_id=league.id,
            game_id=game_ids[1],
            agent_build_id=ab.id,
            scope_type="GLOBAL",
            scope_value="GLOBAL",
            before_mu=26.0,
            before_sigma=7.5,
            after_mu=27.0,
            after_sigma=7.0,
        )

    async with session_factory() as session:
        rows = await ratings_repo.list_rating_events(session, league_id=league.id)
        assert [r.game_id for r in rows] == game_ids


async def test_ratings_repo_has_no_forbidden_imports() -> None:
    forbidden = {"random", "secrets", "time", "litellm", "httpx"}
    path = Path("src/padrino/db/repositories/ratings.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in forbidden
