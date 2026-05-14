"""US-029: SQLAlchemy async models smoke tests against an in-memory aiosqlite DB."""

from __future__ import annotations

import ast
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import (
    AgentBuild,
    Game,
    GameSeat,
    Gauntlet,
    GauntletRosterSlot,
    League,
    ModelConfig,
    ModelProvider,
    PromptVersion,
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


async def _make_provider(session: AsyncSession) -> ModelProvider:
    p = ModelProvider(name="cerebras", base_url=None, auth_secret_ref="CEREBRAS_API_KEY")
    session.add(p)
    await session.flush()
    return p


async def _make_model_config(session: AsyncSession, provider: ModelProvider) -> ModelConfig:
    mc = ModelConfig(
        provider_id=provider.id,
        model_name="zai-glm-4.7",
        model_version=None,
        default_temperature=0.7,
        default_top_p=1.0,
        default_max_output_tokens=4096,
        supports_structured_outputs=True,
    )
    session.add(mc)
    await session.flush()
    return mc


async def _make_prompt_version(session: AsyncSession, *, prompt_hash: str) -> PromptVersion:
    pv = PromptVersion(
        ruleset_id="mini7_v1",
        version="v1",
        system_prompt="you are a careful player",
        developer_prompt="reply with JSON",
        response_schema={"type": "object"},
        prompt_hash=prompt_hash,
    )
    session.add(pv)
    await session.flush()
    return pv


async def _make_agent_build(
    session: AsyncSession, mc: ModelConfig, pv: PromptVersion
) -> AgentBuild:
    ab = AgentBuild(
        display_name="cerebras/glm-4.7@v1",
        model_config_id=mc.id,
        prompt_version_id=pv.id,
        adapter_version="2026.05",
        inference_params={"temperature": 0.7},
        active=True,
    )
    session.add(ab)
    await session.flush()
    return ab


async def test_model_provider_insert_and_defaults(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        provider = await _make_provider(session)
        await session.commit()
        assert isinstance(provider.id, uuid.UUID)
        assert isinstance(provider.created_at, datetime)
        assert provider.created_at.tzinfo is not None


async def test_model_config_fk_violation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        bogus = uuid.uuid4()
        session.add(
            ModelConfig(
                provider_id=bogus,
                model_name="x",
                default_temperature=0.5,
                default_top_p=0.9,
                default_max_output_tokens=1024,
                supports_structured_outputs=False,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_prompt_version_unique_hash(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _make_prompt_version(session, prompt_hash="abc123")
        await session.commit()
    async with session_factory() as session:
        session.add(
            PromptVersion(
                ruleset_id="mini7_v1",
                version="v2",
                system_prompt="x",
                developer_prompt="y",
                response_schema={},
                prompt_hash="abc123",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_agent_build_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        provider = await _make_provider(session)
        mc = await _make_model_config(session, provider)
        pv = await _make_prompt_version(session, prompt_hash="hash-ab")
        ab = await _make_agent_build(session, mc, pv)
        await session.commit()
        ab_id = ab.id
    async with session_factory() as session:
        loaded = await session.get(AgentBuild, ab_id)
        assert loaded is not None
        assert loaded.display_name == "cerebras/glm-4.7@v1"
        assert loaded.inference_params == {"temperature": 0.7}
        assert loaded.active is True


async def test_agent_build_fk_violation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(
            AgentBuild(
                display_name="orphan",
                model_config_id=uuid.uuid4(),
                prompt_version_id=uuid.uuid4(),
                adapter_version="x",
                inference_params={},
                active=True,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_league_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        league = League(name="ranked-mini7", ruleset_id="mini7_v1", ranked=True)
        session.add(league)
        await session.commit()
        assert league.ranked is True
        assert isinstance(league.id, uuid.UUID)


async def test_gauntlet_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        league = League(name="L", ruleset_id="mini7_v1", ranked=True)
        pv = await _make_prompt_version(session, prompt_hash="ph-gauntlet")
        session.add(league)
        await session.flush()
        g = Gauntlet(
            league_id=league.id,
            ruleset_id="mini7_v1",
            prompt_version_id=pv.id,
            clone_count=7,
            gauntlet_seed="seed-1",
            ranked=True,
            status="PENDING",
        )
        session.add(g)
        await session.commit()
        assert g.completed_at is None


async def test_gauntlet_fk_violation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        pv = await _make_prompt_version(session, prompt_hash="ph-fk")
        await session.commit()
    async with session_factory() as session:
        session.add(
            Gauntlet(
                league_id=uuid.uuid4(),
                ruleset_id="mini7_v1",
                prompt_version_id=pv.id,
                clone_count=7,
                gauntlet_seed="x",
                ranked=False,
                status="PENDING",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_gauntlet_roster_slot_unique(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        provider = await _make_provider(session)
        mc = await _make_model_config(session, provider)
        pv = await _make_prompt_version(session, prompt_hash="slot-ph")
        ab = await _make_agent_build(session, mc, pv)
        league = League(name="L", ruleset_id="mini7_v1", ranked=False)
        session.add(league)
        await session.flush()
        g = Gauntlet(
            league_id=league.id,
            ruleset_id="mini7_v1",
            prompt_version_id=pv.id,
            clone_count=7,
            gauntlet_seed="s",
            ranked=False,
            status="PENDING",
        )
        session.add(g)
        await session.flush()
        session.add(GauntletRosterSlot(gauntlet_id=g.id, slot_index=0, agent_build_id=ab.id))
        await session.commit()
        session.add(GauntletRosterSlot(gauntlet_id=g.id, slot_index=0, agent_build_id=ab.id))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_gauntlet_roster_slot_fk_violation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(
            GauntletRosterSlot(gauntlet_id=uuid.uuid4(), slot_index=0, agent_build_id=uuid.uuid4())
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_game_round_trip_and_nullable_gauntlet(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        game = Game(
            gauntlet_id=None,
            ruleset_id="mini7_v1",
            game_seed="seed-a",
            status="CREATED",
        )
        session.add(game)
        await session.commit()
        assert game.terminal_result is None
        assert game.gauntlet_id is None


async def test_game_fk_violation_when_gauntlet_id_set(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(
            Game(
                gauntlet_id=uuid.uuid4(),
                ruleset_id="mini7_v1",
                game_seed="seed",
                status="CREATED",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_game_seat_uniques(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        provider = await _make_provider(session)
        mc = await _make_model_config(session, provider)
        pv = await _make_prompt_version(session, prompt_hash="seat-ph")
        ab = await _make_agent_build(session, mc, pv)
        game = Game(
            gauntlet_id=None, ruleset_id="mini7_v1", game_seed="seat-seed", status="CREATED"
        )
        session.add(game)
        await session.flush()
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P01",
                seat_index=0,
                agent_build_id=ab.id,
                role="VILLAGER",
                faction="TOWN",
                alive=True,
            )
        )
        await session.commit()
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P01",
                seat_index=1,
                agent_build_id=ab.id,
                role="VILLAGER",
                faction="TOWN",
                alive=True,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_game_seat_unique_seat_index(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        provider = await _make_provider(session)
        mc = await _make_model_config(session, provider)
        pv = await _make_prompt_version(session, prompt_hash="seat-ph2")
        ab = await _make_agent_build(session, mc, pv)
        game = Game(
            gauntlet_id=None,
            ruleset_id="mini7_v1",
            game_seed="seat-seed-2",
            status="CREATED",
        )
        session.add(game)
        await session.flush()
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P01",
                seat_index=0,
                agent_build_id=ab.id,
                role="VILLAGER",
                faction="TOWN",
                alive=True,
            )
        )
        await session.commit()
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P02",
                seat_index=0,
                agent_build_id=ab.id,
                role="VILLAGER",
                faction="TOWN",
                alive=True,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_game_seat_fk_violation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(
            GameSeat(
                game_id=uuid.uuid4(),
                public_player_id="P01",
                seat_index=0,
                agent_build_id=uuid.uuid4(),
                role="VILLAGER",
                faction="TOWN",
                alive=True,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_query_round_trip_uuid_pk(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        league = League(name="alpha", ruleset_id="mini7_v1", ranked=True)
        session.add(league)
        await session.commit()
        lid = league.id
    async with session_factory() as session:
        result = await session.execute(select(League).where(League.id == lid))
        row = result.scalar_one()
        assert row.name == "alpha"
        assert isinstance(row.id, uuid.UUID)


def test_timestamp_columns_declare_timezone_aware() -> None:
    from sqlalchemy import DateTime

    for model in (ModelProvider, ModelConfig, PromptVersion, AgentBuild, League, Gauntlet):
        col = model.__table__.c["created_at"]
        col_type = col.type
        assert isinstance(col_type, DateTime)
        assert col_type.timezone is True, f"{model.__name__}.created_at must be timezone-aware"
    for model_, col_name in (
        (Gauntlet, "completed_at"),
        (Game, "started_at"),
        (Game, "completed_at"),
    ):
        col = model_.__table__.c[col_name]
        col_type = col.type
        assert isinstance(col_type, DateTime)
        assert col_type.timezone is True


async def test_created_at_default_is_set_on_insert(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        provider = await _make_provider(session)
        await session.commit()
        assert isinstance(provider.created_at, datetime)
        assert provider.created_at.tzinfo is not None


def test_models_module_has_no_forbidden_imports() -> None:
    src = Path("src/padrino/db/models.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = {"random", "secrets", "time"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in forbidden, node.module
