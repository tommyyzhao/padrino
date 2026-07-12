"""US-312: migration 0064 adds ranked anti-cheat flags."""

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

from padrino.db.models import RankedFlag

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

TABLE_NAME = "ranked_flags"
EXPECTED_COLUMNS = {
    "id",
    "kind",
    "principal_id",
    "severity",
    "evidence",
    "evidence_hash",
    "created_at",
}
EXPECTED_INDEXES = {
    "uq_ranked_flags_kind_principal_evidence_hash",
    "ix_ranked_flags_kind",
    "ix_ranked_flags_severity",
    "ix_ranked_flags_principal_id",
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


def test_0064_is_linear_head_after_0063() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0070"]
    assert script.get_revision("0068").down_revision == "0067"
    assert script.get_revision("0067").down_revision == "0066"
    assert script.get_revision("0066").down_revision == "0065"
    assert script.get_revision("0064").down_revision == "0063"
    assert script.get_revision("0063").down_revision == "0062"


def test_0064_creates_ranked_flags_round_trip(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0063")

    assert TABLE_NAME not in _table_names(sqlite_db)

    command.upgrade(cfg, "0064")

    assert TABLE_NAME in _table_names(sqlite_db)
    info = _column_info(sqlite_db, TABLE_NAME)
    assert set(info) == EXPECTED_COLUMNS
    assert info["id"]["pk"] == 1
    for column in EXPECTED_COLUMNS:
        assert info[column]["notnull"] == 1 or column == "id"
    assert EXPECTED_INDEXES.issubset(_index_names(sqlite_db, TABLE_NAME))

    command.downgrade(cfg, "0063")

    assert TABLE_NAME not in _table_names(sqlite_db)


def test_ranked_flag_model_matches_0064_schema(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    table = cast(Table, RankedFlag.__table__)
    assert isinstance(table.columns["kind"].type, String)
    assert table.columns["kind"].nullable is False
    assert isinstance(table.columns["principal_id"].type, String)
    assert table.columns["principal_id"].nullable is False
    assert isinstance(table.columns["severity"].type, String)
    assert table.columns["severity"].nullable is False
    assert isinstance(table.columns["evidence"].type, JSON)
    assert isinstance(table.columns["evidence_hash"].type, String)
    assert table.columns["evidence_hash"].nullable is False
