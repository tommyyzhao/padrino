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
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sync_create_engine
from sqlalchemy import inspect

from tests.db.test_migrations import EXPECTED_TABLES

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


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


def _column_names_for_url(url: str, table_name: str) -> set[str]:
    sync_url = _sync_url(url)
    engine = sync_create_engine(sync_url)
    try:
        with engine.connect() as conn:
            return {column["name"] for column in inspect(conn).get_columns(table_name)}
    finally:
        engine.dispose()


@pytest.fixture
def sqlite_db_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "padrino_test.db"
        url = f"sqlite+aiosqlite:///{db_path}"
        monkeypatch.setenv("PADRINO_DB_URL", url)
        yield url


def _with_database(url: str, database: str) -> str:
    """Return ``url`` with its path (database name) replaced by ``database``."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


@pytest.fixture
def postgres_db_url(
    postgres_url_for_migrations: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    """Provision a UNIQUE, freshly-created Postgres database for each test.

    US-118 flake burn-down: the container is session-scoped and shared across
    the postgres migration tests, so the previous "reflect + drop every table"
    reset shared a single mutable database between tests. Any leftover object
    (or a concurrent run against the same container) made the upgrade/downgrade
    cycle non-hermetic. Instead, each test gets its own throwaway database
    (``padrino_mig_<uuid>``) created on the maintenance database and dropped in
    teardown, so there is zero shared state to leak between tests.
    """
    sync_admin_url = _sync_url(postgres_url_for_migrations)
    db_name = f"padrino_mig_{uuid.uuid4().hex}"

    # CREATE/DROP DATABASE cannot run inside a transaction block, so use an
    # AUTOCOMMIT connection on the container's maintenance database.
    admin_engine = sync_create_engine(sync_admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{db_name}"')
    finally:
        admin_engine.dispose()

    test_url = _with_database(postgres_url_for_migrations, db_name)
    monkeypatch.setenv("PADRINO_DB_URL", test_url)
    try:
        yield test_url
    finally:
        drop_engine = sync_create_engine(sync_admin_url, isolation_level="AUTOCOMMIT")
        try:
            with drop_engine.connect() as conn:
                # Terminate any lingering backends so DROP DATABASE succeeds.
                conn.exec_driver_sql(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %(db)s AND pid <> pg_backend_pid()",
                    {"db": db_name},
                )
                conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            drop_engine.dispose()


def test_sqlite_upgrade_head_creates_all_tables(sqlite_db_url: str) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    tables = _table_names_for_url(sqlite_db_url)
    assert EXPECTED_TABLES.issubset(tables), f"missing tables: {EXPECTED_TABLES - tables}"
    assert "alembic_version" in tables
    assert "integrity_acknowledged" in _column_names_for_url(sqlite_db_url, "lobbies")


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
    assert "integrity_acknowledged" in _column_names_for_url(postgres_db_url, "lobbies")


@pytest.mark.postgres
def test_postgres_full_cycle_upgrade_downgrade_upgrade(postgres_db_url: str) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    leftover = _table_names_for_url(postgres_db_url) & EXPECTED_TABLES
    assert leftover == set(), f"leftover after downgrade base: {leftover}"
    command.upgrade(cfg, "head")
    assert EXPECTED_TABLES.issubset(_table_names_for_url(postgres_db_url))
