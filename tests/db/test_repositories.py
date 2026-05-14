"""US-031: end-to-end CRUD tests for the async repository layer."""

from __future__ import annotations

import ast
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import AgentBuild, ModelConfig, ModelProvider, PromptVersion
from padrino.db.repositories import (
    agent_builds,
    games,
    gauntlets,
    leagues,
    model_configs,
    prompt_versions,
    providers,
)


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


async def _seed_chain(
    session: AsyncSession,
    *,
    provider_name: str = "cerebras",
    prompt_hash: str = "h-default",
) -> tuple[ModelProvider, ModelConfig, PromptVersion, AgentBuild]:
    provider = await providers.create(
        session,
        name=provider_name,
        auth_secret_ref=f"{provider_name.upper()}_API_KEY",
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
        display_name=f"{provider_name}/glm-4.7@v1",
        model_config_id=mc.id,
        prompt_version_id=pv.id,
        adapter_version="2026.05",
        inference_params={"temperature": 0.7},
        active=True,
    )
    return provider, mc, pv, ab


# ---------- providers ----------


async def test_providers_create_get_list(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        p1 = await providers.create(session, name="cerebras", auth_secret_ref="CEREBRAS_API_KEY")
        p2 = await providers.create(
            session, name="deepinfra", auth_secret_ref="DEEPINFRA_API_KEY", base_url="https://x"
        )
        await session.commit()
        p1_id = p1.id
        p2_id = p2.id

    async with session_factory() as session:
        got = await providers.get(session, p1_id)
        assert got is not None and got.name == "cerebras"
        all_ = await providers.list_(session)
        assert {p.id for p in all_} == {p1_id, p2_id}
        filtered = await providers.list_(session, name="deepinfra")
        assert [p.id for p in filtered] == [p2_id]
        missing = await providers.get(session, uuid.uuid4())
        assert missing is None


# ---------- model_configs ----------


async def test_model_configs_create_get_list(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        provider = await providers.create(
            session, name="cerebras", auth_secret_ref="CEREBRAS_API_KEY"
        )
        await session.commit()
        provider_id = provider.id

    async with session_factory() as session:
        mc1 = await model_configs.create(
            session,
            provider_id=provider_id,
            model_name="glm-4.7",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        mc2 = await model_configs.create(
            session,
            provider_id=provider_id,
            model_name="glm-3.5",
            default_temperature=0.4,
            default_top_p=0.9,
            default_max_output_tokens=2048,
            supports_structured_outputs=False,
        )
        await session.commit()
        mc1_id, mc2_id = mc1.id, mc2.id

    async with session_factory() as session:
        got = await model_configs.get(session, mc1_id)
        assert got is not None and got.model_name == "glm-4.7"
        all_ = await model_configs.list_(session, provider_id=provider_id)
        assert {m.id for m in all_} == {mc1_id, mc2_id}
        by_name = await model_configs.list_(session, model_name="glm-3.5")
        assert [m.id for m in by_name] == [mc2_id]
        assert await model_configs.get(session, uuid.uuid4()) is None


async def test_model_configs_fk_violation_surfaces(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await model_configs.create(
                session,
                provider_id=uuid.uuid4(),
                model_name="orphan",
                default_temperature=0.5,
                default_top_p=0.9,
                default_max_output_tokens=1024,
                supports_structured_outputs=False,
            )


# ---------- prompt_versions ----------


async def test_prompt_versions_create_get_list(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        pv1 = await prompt_versions.create(
            session,
            ruleset_id="mini7_v1",
            version="v1",
            system_prompt="sys",
            developer_prompt="dev",
            response_schema={"type": "object"},
            prompt_hash="hash-1",
        )
        pv2 = await prompt_versions.create(
            session,
            ruleset_id="mini9_v1",
            version="v1",
            system_prompt="sys2",
            developer_prompt="dev2",
            response_schema={"type": "object"},
            prompt_hash="hash-2",
        )
        await session.commit()
        pv1_id, pv2_id = pv1.id, pv2.id

    async with session_factory() as session:
        assert (await prompt_versions.get(session, pv1_id)) is not None
        by_hash = await prompt_versions.get_by_hash(session, "hash-2")
        assert by_hash is not None and by_hash.id == pv2_id
        miss = await prompt_versions.get_by_hash(session, "missing")
        assert miss is None
        all_mini7 = await prompt_versions.list_(session, ruleset_id="mini7_v1")
        assert [p.id for p in all_mini7] == [pv1_id]
        all_ = await prompt_versions.list_(session)
        assert {p.id for p in all_} == {pv1_id, pv2_id}


async def test_prompt_versions_unique_hash_violation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await prompt_versions.create(
            session,
            ruleset_id="mini7_v1",
            version="v1",
            system_prompt="s",
            developer_prompt="d",
            response_schema={},
            prompt_hash="dup",
        )
        await session.commit()
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await prompt_versions.create(
                session,
                ruleset_id="mini7_v1",
                version="v2",
                system_prompt="s2",
                developer_prompt="d2",
                response_schema={},
                prompt_hash="dup",
            )


# ---------- agent_builds ----------


async def test_agent_builds_create_get_list(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        _, mc, pv, ab1 = await _seed_chain(session, prompt_hash="ab1")
        ab2 = await agent_builds.create(
            session,
            display_name="alt",
            model_config_id=mc.id,
            prompt_version_id=pv.id,
            adapter_version="2026.05",
            inference_params={},
            active=False,
        )
        await session.commit()
        ab1_id, ab2_id = ab1.id, ab2.id
        mc_id, pv_id = mc.id, pv.id

    async with session_factory() as session:
        got = await agent_builds.get(session, ab1_id)
        assert got is not None and got.display_name.endswith("@v1")
        active_only = await agent_builds.list_(session, active=True)
        assert [b.id for b in active_only] == [ab1_id]
        by_mc = await agent_builds.list_(session, model_config_id=mc_id)
        assert {b.id for b in by_mc} == {ab1_id, ab2_id}
        by_pv = await agent_builds.list_(session, prompt_version_id=pv_id)
        assert {b.id for b in by_pv} == {ab1_id, ab2_id}
        none_match = await agent_builds.list_(session, prompt_version_id=uuid.uuid4())
        assert none_match == []


# ---------- leagues ----------


async def test_leagues_create_get_list(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        ranked = await leagues.create(session, name="ranked", ruleset_id="mini7_v1", ranked=True)
        unranked = await leagues.create(
            session, name="exhibition", ruleset_id="mini7_v1", ranked=False
        )
        await session.commit()
        ranked_id, unranked_id = ranked.id, unranked.id

    async with session_factory() as session:
        assert (await leagues.get(session, ranked_id)) is not None
        rs = await leagues.list_(session, ranked=True)
        assert [r.id for r in rs] == [ranked_id]
        us = await leagues.list_(session, ranked=False)
        assert [u.id for u in us] == [unranked_id]
        by_ruleset = await leagues.list_(session, ruleset_id="mini7_v1")
        assert {x.id for x in by_ruleset} == {ranked_id, unranked_id}


# ---------- gauntlets ----------


async def test_gauntlets_create_get_list(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        _, _, pv, _ = await _seed_chain(session, prompt_hash="g-1")
        league = await leagues.create(session, name="L", ruleset_id="mini7_v1", ranked=True)
        await session.commit()
        league_id, pv_id = league.id, pv.id

    async with session_factory() as session:
        g1 = await gauntlets.create(
            session,
            league_id=league_id,
            ruleset_id="mini7_v1",
            prompt_version_id=pv_id,
            clone_count=7,
            gauntlet_seed="s1",
            ranked=True,
        )
        g2 = await gauntlets.create(
            session,
            league_id=league_id,
            ruleset_id="mini7_v1",
            prompt_version_id=pv_id,
            clone_count=7,
            gauntlet_seed="s2",
            ranked=True,
            status="RUNNING",
        )
        await session.commit()
        g1_id, g2_id = g1.id, g2.id

    async with session_factory() as session:
        got = await gauntlets.get(session, g1_id)
        assert got is not None and got.gauntlet_seed == "s1"
        all_ = await gauntlets.list_(session, league_id=league_id)
        assert {g.id for g in all_} == {g1_id, g2_id}
        pending = await gauntlets.list_(session, status="PENDING")
        assert [g.id for g in pending] == [g1_id]
        running = await gauntlets.list_(session, status="RUNNING")
        assert [g.id for g in running] == [g2_id]
        ranked_only = await gauntlets.list_(session, ranked=True)
        assert {g.id for g in ranked_only} == {g1_id, g2_id}


async def test_gauntlets_add_roster_slot_and_list(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        _, _, pv, ab = await _seed_chain(session, prompt_hash="g-roster")
        league = await leagues.create(session, name="L", ruleset_id="mini7_v1", ranked=False)
        await session.flush()
        g = await gauntlets.create(
            session,
            league_id=league.id,
            ruleset_id="mini7_v1",
            prompt_version_id=pv.id,
            clone_count=7,
            gauntlet_seed="seed",
            ranked=False,
        )
        await session.commit()
        g_id, ab_id = g.id, ab.id

    async with session_factory() as session:
        await gauntlets.add_roster_slot(session, g_id, 0, ab_id)
        await gauntlets.add_roster_slot(session, g_id, 1, ab_id)
        await session.commit()

    async with session_factory() as session:
        slots = await gauntlets.list_roster_slots(session, g_id)
        assert [s.slot_index for s in slots] == [0, 1]
        assert all(s.agent_build_id == ab_id for s in slots)


async def test_gauntlets_add_roster_slot_duplicate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        _, _, pv, ab = await _seed_chain(session, prompt_hash="g-dup")
        league = await leagues.create(session, name="L", ruleset_id="mini7_v1", ranked=False)
        await session.flush()
        g = await gauntlets.create(
            session,
            league_id=league.id,
            ruleset_id="mini7_v1",
            prompt_version_id=pv.id,
            clone_count=7,
            gauntlet_seed="seed",
            ranked=False,
        )
        await session.commit()
        g_id, ab_id = g.id, ab.id

    async with session_factory() as session:
        await gauntlets.add_roster_slot(session, g_id, 0, ab_id)
        await session.commit()
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await gauntlets.add_roster_slot(session, g_id, 0, ab_id)


# ---------- games ----------


async def test_games_create_get_list(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        _, _, pv, _ = await _seed_chain(session, prompt_hash="games-1")
        league = await leagues.create(session, name="L", ruleset_id="mini7_v1", ranked=True)
        await session.flush()
        g = await gauntlets.create(
            session,
            league_id=league.id,
            ruleset_id="mini7_v1",
            prompt_version_id=pv.id,
            clone_count=7,
            gauntlet_seed="seed",
            ranked=True,
        )
        await session.commit()
        g_id = g.id

    async with session_factory() as session:
        gm1 = await games.create(session, ruleset_id="mini7_v1", game_seed="g1", gauntlet_id=g_id)
        gm2 = await games.create(
            session, ruleset_id="mini7_v1", game_seed="g2", gauntlet_id=g_id, status="RUNNING"
        )
        gm3 = await games.create(session, ruleset_id="mini7_v1", game_seed="g3")
        await session.commit()
        gm1_id, gm2_id, gm3_id = gm1.id, gm2.id, gm3.id

    async with session_factory() as session:
        got = await games.get(session, gm1_id)
        assert got is not None and got.game_seed == "g1"
        all_ = await games.list_(session)
        assert {x.id for x in all_} == {gm1_id, gm2_id, gm3_id}
        by_status = await games.list_(session, status="RUNNING")
        assert [x.id for x in by_status] == [gm2_id]
        by_g = await games.list_by_gauntlet(session, g_id)
        assert {x.id for x in by_g} == {gm1_id, gm2_id}


async def test_games_list_by_gauntlet_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        result = await games.list_by_gauntlet(session, uuid.uuid4())
        assert result == []


async def test_games_update_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        game = await games.create(session, ruleset_id="mini7_v1", game_seed="upd")
        await session.commit()
        game_id = game.id

    completed_at = datetime.now(UTC)
    async with session_factory() as session:
        updated = await games.update_status(
            session,
            game_id,
            status="COMPLETED",
            terminal_result="TOWN",
            terminal_reason="ALL_MAFIA_ELIMINATED",
            current_phase="TERMINAL",
            event_hash_head="a" * 64,
            completed_at=completed_at,
        )
        assert updated is not None
        await session.commit()

    async with session_factory() as session:
        loaded = await games.get(session, game_id)
        assert loaded is not None
        assert loaded.status == "COMPLETED"
        assert loaded.terminal_result == "TOWN"
        assert loaded.event_hash_head == "a" * 64

        miss = await games.update_status(session, uuid.uuid4(), status="X")
        assert miss is None


async def test_games_add_seat_and_list(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        _, _, _, ab = await _seed_chain(session, prompt_hash="seats-ph")
        gm = await games.create(session, ruleset_id="mini7_v1", game_seed="seat-seed")
        await session.commit()
        game_id, ab_id = gm.id, ab.id

    async with session_factory() as session:
        await games.add_seat(
            session,
            game_id=game_id,
            public_player_id="P01",
            seat_index=0,
            agent_build_id=ab_id,
            role="VILLAGER",
            faction="TOWN",
        )
        await games.add_seat(
            session,
            game_id=game_id,
            public_player_id="P02",
            seat_index=1,
            agent_build_id=ab_id,
            role="DETECTIVE",
            faction="TOWN",
        )
        await session.commit()

    async with session_factory() as session:
        seats = await games.list_seats(session, game_id)
        assert [s.seat_index for s in seats] == [0, 1]
        assert [s.public_player_id for s in seats] == ["P01", "P02"]


# ---------- module purity ----------


def test_repository_modules_have_no_forbidden_imports() -> None:
    forbidden = {"random", "secrets", "time", "litellm", "httpx"}
    repo_dir = Path("src/padrino/db/repositories")
    files = sorted(repo_dir.glob("*.py"))
    # Make sure we are actually scanning real modules.
    assert {f.name for f in files} >= {
        "__init__.py",
        "providers.py",
        "model_configs.py",
        "prompt_versions.py",
        "agent_builds.py",
        "leagues.py",
        "gauntlets.py",
        "games.py",
    }
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden, (path.name, alias.name)
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root not in forbidden, (path.name, node.module)
