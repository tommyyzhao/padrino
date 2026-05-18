"""US-057: Alembic ``upgrade head`` / ``downgrade base`` cycle on every dialect.

Parametrizes the migration smoke against both SQLite (default) and Postgres
(skipped without a Docker daemon — marked ``@pytest.mark.postgres``). The
expected table set is shared with the SQLite-only suite in
``tests/db/test_migrations.py`` so a regression in either dialect is loud.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sync_create_engine
from sqlalchemy import inspect

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

EXPECTED_TABLES = {
    "model_providers",
    "model_configs",
    "prompt_versions",
    "agent_builds",
    "leagues",
    "gauntlets",
    "gauntlet_roster_slots",
    "games",
    "game_seats",
    "game_events",
    "llm_calls",
    "ratings",
    "rating_events",
    "api_keys",
    "scheduler_heartbeats",
}


def _docker_available() -> bool:
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
def postgres_url_for_migrations() -> Iterator[str]:
    """Spin up a Postgres container for migration tests (session scoped)."""
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


def _alembic_config() -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "src/padrino/db/migrations"))
    return cfg


def _sync_url(url: str) -> str:
    """Translate an async SQLAlchemy URL to its sync equivalent for inspector use."""
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if url.startswith("sqlite+aiosqlite://"):
        return url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    return url


def _table_names_for_url(url: str) -> set[str]:
    sync_url = _sync_url(url)
    engine = sync_create_engine(sync_url)
    try:
        with engine.connect() as conn:
            return set(inspect(conn).get_table_names())
    finally:
        engine.dispose()


@pytest.fixture
def sqlite_db_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "padrino_test.db"
        url = f"sqlite+aiosqlite:///{db_path}"
        monkeypatch.setenv("PADRINO_DB_URL", url)
        yield url


@pytest.fixture
def postgres_db_url(
    postgres_url_for_migrations: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    """Reset the container's schema between tests, then expose the URL."""
    # `alembic downgrade base` drops everything Padrino owns; ensure we also
    # remove the alembic_version table so each test starts from a truly empty
    # database (no orphan revision row, no leftover tables from a prior test).
    sync_url = _sync_url(postgres_url_for_migrations)
    engine = sync_create_engine(sync_url)
    try:
        with engine.connect() as conn:
            for table in (*EXPECTED_TABLES, "alembic_version"):
                conn.exec_driver_sql(f'DROP TABLE IF EXISTS "{table}" CASCADE')
            conn.commit()
    finally:
        engine.dispose()

    monkeypatch.setenv("PADRINO_DB_URL", postgres_url_for_migrations)
    yield postgres_url_for_migrations


def test_sqlite_upgrade_head_creates_all_tables(sqlite_db_url: str) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    tables = _table_names_for_url(sqlite_db_url)
    assert EXPECTED_TABLES.issubset(tables), f"missing tables: {EXPECTED_TABLES - tables}"
    assert "alembic_version" in tables


def test_sqlite_full_cycle_upgrade_downgrade_upgrade(sqlite_db_url: str) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    leftover = _table_names_for_url(sqlite_db_url) & EXPECTED_TABLES
    assert leftover == set(), f"leftover after downgrade base: {leftover}"
    command.upgrade(cfg, "head")
    assert EXPECTED_TABLES.issubset(_table_names_for_url(sqlite_db_url))


@pytest.mark.postgres
def test_postgres_upgrade_head_creates_all_tables(postgres_db_url: str) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    tables = _table_names_for_url(postgres_db_url)
    assert EXPECTED_TABLES.issubset(tables), f"missing tables: {EXPECTED_TABLES - tables}"
    assert "alembic_version" in tables


@pytest.mark.postgres
def test_postgres_full_cycle_upgrade_downgrade_upgrade(postgres_db_url: str) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    leftover = _table_names_for_url(postgres_db_url) & EXPECTED_TABLES
    assert leftover == set(), f"leftover after downgrade base: {leftover}"
    command.upgrade(cfg, "head")
    assert EXPECTED_TABLES.issubset(_table_names_for_url(postgres_db_url))
