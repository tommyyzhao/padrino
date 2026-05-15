"""US-057: schema + CRUD round-trip identically on SQLite and Postgres.

The Postgres path uses ``testcontainers`` to spin up ``postgres:17-alpine`` and
is marked ``@pytest.mark.postgres`` so contributors without a running Docker
daemon can skip it locally (CI always runs it). The SQLite path mirrors the
existing ``tests/db/test_repositories.py`` fixture and runs by default.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import (
    AgentBuild,
    ApiKey,
    Game,
    GameEvent,
    GameSeat,
    Gauntlet,
    GauntletRosterSlot,
    League,
    LlmCall,
    ModelConfig,
    ModelProvider,
    PromptVersion,
    Rating,
    RatingEvent,
)
from padrino.db.repositories import (
    agent_builds,
    games,
    gauntlets,
    leagues,
    model_configs,
    prompt_versions,
    providers,
)

# JSON-backed columns whose dialect mapping has to stay portable: SQLite gets
# the JSON1 text storage; Postgres gets the native JSON type (asyncpg / psycopg
# both speak it). The driver-agnostic ``sa.JSON`` reports ``dict`` as the
# Python-side ``python_type`` regardless of dialect — that's the
# acceptance-criteria contract.
_JSON_COLUMNS: list[tuple[type[Base], str]] = [
    (PromptVersion, "response_schema"),
    (AgentBuild, "inference_params"),
    (Game, "terminal_result"),
    (GameEvent, "payload"),
    (LlmCall, "request_json"),
    (LlmCall, "parsed_response"),
    (ApiKey, "scopes"),
]


def _docker_available() -> bool:
    """Return ``True`` when the local Docker daemon answers a ping."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Spin up ``postgres:17-alpine`` once per session and yield an asyncpg URL."""
    if not _docker_available():
        pytest.skip("docker daemon is not reachable")
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - dev dep installed by uv sync
        pytest.skip("testcontainers is not installed")

    container = PostgresContainer("postgres:17-alpine", driver="asyncpg")
    container.start()
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture
async def sqlite_engine() -> AsyncIterator[AsyncEngine]:
    eng = create_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def postgres_engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(postgres_url, pool_size=2, max_overflow=2)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await eng.dispose()


# ---------- static portability assertions (run by default) ----------


def test_every_json_column_reports_dict_python_type() -> None:
    """``sa.JSON`` is the only JSON shape allowed; ``.python_type`` is ``dict``."""
    for model, name in _JSON_COLUMNS:
        col = model.__table__.c[name]
        assert isinstance(col.type, sa.JSON), f"{model.__name__}.{name} is not sa.JSON"
        assert col.type.python_type is dict, (
            f"{model.__name__}.{name}.python_type expected dict, got {col.type.python_type!r}"
        )


def test_no_dialect_specific_json_in_models() -> None:
    """Reject ``sqlalchemy.dialects.postgresql.JSONB`` and ``...sqlite.JSON`` leaks."""
    from sqlalchemy.dialects import postgresql, sqlite

    forbidden: tuple[type, ...] = (
        getattr(postgresql, "JSON", type("_NA1", (), {})),
        getattr(postgresql, "JSONB", type("_NA2", (), {})),
        getattr(sqlite, "JSON", type("_NA3", (), {})),
    )
    for model, name in _JSON_COLUMNS:
        col = model.__table__.c[name]
        for cls in forbidden:
            assert not isinstance(col.type, cls), (
                f"{model.__name__}.{name} uses dialect-specific {cls.__name__}; "
                "use sa.JSON for cross-dialect portability."
            )


def test_uuid_primary_keys_use_portable_sa_uuid() -> None:
    """Every UUID PK / FK uses ``sa.Uuid`` so SQLite + asyncpg both round-trip."""
    uuid_owners = (
        ModelProvider,
        ModelConfig,
        PromptVersion,
        AgentBuild,
        League,
        Gauntlet,
        GauntletRosterSlot,
        Game,
        GameSeat,
        GameEvent,
        LlmCall,
        Rating,
        RatingEvent,
        ApiKey,
    )
    for model in uuid_owners:
        pk = model.__table__.c["id"]
        assert isinstance(pk.type, sa.Uuid), f"{model.__name__}.id must be sa.Uuid"


# ---------- shared CRUD round-trip (parametrized over dialect) ----------


async def _exercise_full_crud(factory: async_sessionmaker[AsyncSession]) -> None:
    """End-to-end CRUD: provider → config → prompt → agent_build → league →
    gauntlet → game + seats, plus FK enforcement on a bogus FK.
    """
    async with factory() as session:
        provider = await providers.create(
            session, name="cerebras", auth_secret_ref="env:CEREBRAS_API_KEY"
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
            response_schema={"type": "object", "nested": {"k": [1, 2, 3]}},
            prompt_hash="dialect-hash",
        )
        ab = await agent_builds.create(
            session,
            display_name="cerebras/glm-4.7@v1",
            model_config_id=mc.id,
            prompt_version_id=pv.id,
            adapter_version="2026.05",
            inference_params={"temperature": 0.7, "top_p": 1.0},
            active=True,
        )
        league = await leagues.create(
            session, name="ranked-mini7", ruleset_id="mini7_v1", ranked=True
        )
        gauntlet = await gauntlets.create(
            session,
            league_id=league.id,
            ruleset_id="mini7_v1",
            prompt_version_id=pv.id,
            clone_count=7,
            gauntlet_seed="dialect-seed",
            ranked=True,
        )
        game = await games.create(
            session,
            ruleset_id="mini7_v1",
            game_seed="g-seed",
            gauntlet_id=gauntlet.id,
        )
        await games.add_seat(
            session,
            game_id=game.id,
            public_player_id="P01",
            seat_index=0,
            agent_build_id=ab.id,
            role="VILLAGER",
            faction="TOWN",
        )
        await session.commit()
        game_id = game.id

    async with factory() as session:
        roundtrip = await games.get(session, game_id)
        assert roundtrip is not None
        assert roundtrip.ruleset_id == "mini7_v1"
        seats = await games.list_seats(session, game_id)
        assert [s.public_player_id for s in seats] == ["P01"]

        loaded_pv = (
            await session.execute(select(PromptVersion).where(PromptVersion.id == pv.id))
        ).scalar_one()
        assert loaded_pv.response_schema == {
            "type": "object",
            "nested": {"k": [1, 2, 3]},
        }
        loaded_ab = (
            await session.execute(select(AgentBuild).where(AgentBuild.id == ab.id))
        ).scalar_one()
        assert loaded_ab.inference_params == {"temperature": 0.7, "top_p": 1.0}

    # FK enforcement must fire on both dialects.
    async with factory() as session:
        with pytest.raises(IntegrityError):
            await games.add_seat(
                session,
                game_id=uuid.uuid4(),
                public_player_id="P99",
                seat_index=99,
                agent_build_id=uuid.uuid4(),
                role="VILLAGER",
                faction="TOWN",
            )


async def test_sqlite_full_crud_round_trip(sqlite_engine: AsyncEngine) -> None:
    factory = create_session_factory(sqlite_engine)
    await _exercise_full_crud(factory)


@pytest.mark.postgres
async def test_postgres_full_crud_round_trip(postgres_engine: AsyncEngine) -> None:
    factory = create_session_factory(postgres_engine)
    await _exercise_full_crud(factory)


@pytest.mark.postgres
async def test_postgres_fk_enforcement_is_on_by_default(
    postgres_engine: AsyncEngine,
) -> None:
    """Postgres enforces foreign keys without any per-connection setup."""
    factory = create_session_factory(postgres_engine)
    async with factory() as session:
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


@pytest.mark.postgres
async def test_postgres_schema_matches_orm_metadata(
    postgres_engine: AsyncEngine,
) -> None:
    """Every model table exists in the Postgres schema after ``create_all``."""

    def _inspect(conn: sa.Connection) -> set[str]:
        return set(inspect(conn).get_table_names())

    async with postgres_engine.connect() as conn:
        tables = await conn.run_sync(_inspect)

    expected = {table.name for table in Base.metadata.sorted_tables}
    assert expected.issubset(tables), f"missing tables: {expected - tables}"


def test_create_engine_applies_pool_kwargs_only_for_postgres() -> None:
    """SQLite uses StaticPool / aiosqlite — pool kwargs must be Postgres-only."""
    pg_engine = create_engine(
        "postgresql+asyncpg://u:p@h:5432/db",
        pool_size=7,
        max_overflow=3,
    )
    try:
        assert pg_engine.pool.size() == 7  # type: ignore[attr-defined]
    finally:
        pg_engine.sync_engine.dispose()

    sqlite_engine_local = create_engine(
        "sqlite+aiosqlite:///:memory:",
        pool_size=42,
        max_overflow=42,
    )
    try:
        # Asserting the pool size knob did NOT take effect: StaticPool ignores it.
        # If the kwarg leaked through, asyncpg-style pool behavior would surface
        # here and StaticPool's API would differ.
        pool = sqlite_engine_local.pool
        sizer = getattr(pool, "size", None)
        if callable(sizer):
            assert sizer() == 1
    finally:
        sqlite_engine_local.sync_engine.dispose()
