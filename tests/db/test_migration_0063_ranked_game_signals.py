"""US-311: migration 0063 adds ranked-game telemetry signals."""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import JSON, String, Table

from padrino.db.models import Game, RankedGameSignal

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

TABLE_NAME = "ranked_game_signals"
EXPECTED_COLUMNS = {
    "id",
    "game_id",
    "human_principal_id",
    "source_ip_hash",
    "session_fingerprint_hash",
    "winner",
    "faction",
    "signals",
    "created_at",
}
EXPECTED_INDEXES = {
    "uq_ranked_game_signals_game_id",
    "ix_ranked_game_signals_source_ip_hash",
    "ix_ranked_game_signals_human_principal_id",
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


def test_0063_is_linear_head_after_0062() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0071"]
    assert script.get_revision("0068").down_revision == "0067"
    assert script.get_revision("0067").down_revision == "0066"
    assert script.get_revision("0066").down_revision == "0065"
    assert script.get_revision("0063").down_revision == "0062"
    assert script.get_revision("0062").down_revision == "0061"
    assert script.get_revision("0061").down_revision == "0060"


def test_0063_creates_ranked_game_signals_and_game_ip_hash(
    sqlite_db: Path,
) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0062")

    assert TABLE_NAME not in _table_names(sqlite_db)
    game_columns = _column_info(sqlite_db, "games")
    assert "ranked_source_ip_hash" not in game_columns

    command.upgrade(cfg, "0063")

    assert TABLE_NAME in _table_names(sqlite_db)
    info = _column_info(sqlite_db, TABLE_NAME)
    assert set(info) == EXPECTED_COLUMNS
    assert info["id"]["pk"] == 1
    assert info["session_fingerprint_hash"]["notnull"] == 0
    for column in EXPECTED_COLUMNS - {"session_fingerprint_hash"}:
        assert info[column]["notnull"] == 1 or column == "id"
    assert EXPECTED_INDEXES.issubset(_index_names(sqlite_db, TABLE_NAME))

    game_columns = _column_info(sqlite_db, "games")
    assert game_columns["ranked_source_ip_hash"]["notnull"] == 0

    command.downgrade(cfg, "0062")

    assert TABLE_NAME not in _table_names(sqlite_db)
    assert "ranked_source_ip_hash" not in _column_info(sqlite_db, "games")


def test_models_match_0063_ranked_game_signal_schema(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    signals_table = cast(Table, RankedGameSignal.__table__)
    assert isinstance(signals_table.columns["source_ip_hash"].type, String)
    assert signals_table.columns["source_ip_hash"].nullable is False
    assert isinstance(signals_table.columns["session_fingerprint_hash"].type, String)
    assert signals_table.columns["session_fingerprint_hash"].nullable is True
    assert isinstance(signals_table.columns["signals"].type, JSON)

    game_table = cast(Table, Game.__table__)
    assert isinstance(game_table.columns["ranked_source_ip_hash"].type, String)
    assert game_table.columns["ranked_source_ip_hash"].nullable is True
