"""US-294: migration 0056 adds ranked write-audit provenance."""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

TABLE_NAME = "ranked_write_audit"
EXPECTED_COLUMNS = {
    "id",
    "game_id",
    "human_player_id",
    "league_id",
    "decision",
    "reason",
    "flag_enabled",
    "created_at",
}
EXPECTED_INDEXES = {
    "uq_ranked_write_audit_applied_game_human",
    "uq_ranked_write_audit_refused_game_reason",
}


@pytest.fixture
def sqlite_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
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
    return {str(row[0]) for row in rows}


def _column_info(db_path: Path, table_name: str) -> dict[str, sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]): row for row in rows}


def _index_names(db_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (table_name,),
        ).fetchall()
    return {str(row[0]) for row in rows}


def test_0056_chains_after_0055_and_0057_chains_after_0056() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0060"]
    assert script.get_revision("0056").down_revision == "0055"
    assert script.get_revision("0057").down_revision == "0056"
    assert script.get_revision("0058").down_revision == "0057"
    assert script.get_revision("0059").down_revision == "0058"
    assert script.get_revision("0060").down_revision == "0059"
    assert script.get_revision("0055").down_revision == "0054"


def test_0056_creates_ranked_write_audit_round_trip(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0055")

    assert TABLE_NAME not in _table_names(sqlite_db)

    command.upgrade(cfg, "0056")

    assert TABLE_NAME in _table_names(sqlite_db)
    info = _column_info(sqlite_db, TABLE_NAME)
    assert set(info) == EXPECTED_COLUMNS
    assert info["id"]["pk"] == 1
    assert info["human_player_id"]["notnull"] == 0
    for column in EXPECTED_COLUMNS - {"human_player_id"}:
        assert info[column]["notnull"] == 1 or column == "id"
    assert EXPECTED_INDEXES.issubset(_index_names(sqlite_db, TABLE_NAME))

    command.downgrade(cfg, "0055")

    assert TABLE_NAME not in _table_names(sqlite_db)
