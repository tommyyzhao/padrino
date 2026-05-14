"""US-030: Alembic initial migration upgrades and downgrades cleanly."""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

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
}


@pytest.fixture
def alembic_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "padrino_test.db"
        monkeypatch.setenv("PADRINO_DB_URL", f"sqlite+aiosqlite:///{db_path}")
        yield db_path


def _alembic_config() -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "src/padrino/db/migrations"))
    return cfg


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {r[0] for r in rows}


def test_upgrade_head_creates_all_tables(alembic_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    tables = _table_names(alembic_db)
    assert EXPECTED_TABLES.issubset(tables), f"missing tables: {EXPECTED_TABLES - tables}"
    assert "alembic_version" in tables


def test_downgrade_base_drops_all_tables(alembic_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    assert EXPECTED_TABLES.issubset(_table_names(alembic_db))

    command.downgrade(cfg, "base")
    remaining = _table_names(alembic_db)
    assert remaining.isdisjoint(EXPECTED_TABLES), f"leftover tables: {remaining & EXPECTED_TABLES}"


def test_upgrade_is_idempotent_at_head(alembic_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    assert EXPECTED_TABLES.issubset(_table_names(alembic_db))


def test_full_cycle_upgrade_downgrade_upgrade(alembic_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    assert EXPECTED_TABLES.issubset(_table_names(alembic_db))
