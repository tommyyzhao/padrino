"""US-300: migration 0060 creates ranked_ban."""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import DateTime, String, Table

from padrino.db.models import RankedBan

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

TABLE_NAME = "ranked_ban"
EXPECTED_COLUMNS = {
    "id",
    "principal_id",
    "email_hash",
    "kind",
    "reason",
    "created_by",
    "created_at",
    "expires_at",
    "lifted_at",
}
EXPECTED_INDEXES = {
    "ix_ranked_ban_principal_id",
    "ix_ranked_ban_email_hash",
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


def test_0060_is_linear_head_after_0059() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0073"]
    assert script.get_revision("0068").down_revision == "0067"
    assert script.get_revision("0067").down_revision == "0066"
    assert script.get_revision("0066").down_revision == "0065"
    assert script.get_revision("0061").down_revision == "0060"
    assert script.get_revision("0062").down_revision == "0061"
    assert script.get_revision("0063").down_revision == "0062"
    assert script.get_revision("0060").down_revision == "0059"
    assert script.get_revision("0059").down_revision == "0058"


def test_0060_creates_ranked_ban_round_trip(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0059")

    assert TABLE_NAME not in _table_names(sqlite_db)

    command.upgrade(cfg, "0060")

    assert TABLE_NAME in _table_names(sqlite_db)
    info = _column_info(sqlite_db, TABLE_NAME)
    assert set(info) == EXPECTED_COLUMNS
    assert "email" not in info
    assert info["id"]["pk"] == 1
    assert info["email_hash"]["notnull"] == 0
    assert info["expires_at"]["notnull"] == 0
    assert info["lifted_at"]["notnull"] == 0
    for column in EXPECTED_COLUMNS - {"id", "email_hash", "expires_at", "lifted_at"}:
        assert info[column]["notnull"] == 1
    assert EXPECTED_INDEXES.issubset(_index_names(sqlite_db, TABLE_NAME))

    with sqlite3.connect(sqlite_db) as conn:
        now = "2026-06-27 00:00:00"
        principal_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO principals (id, kind, display_name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (principal_id, "account", "Banned", now, now),
        )
        conn.execute(
            "INSERT INTO ranked_ban "
            "(id, principal_id, email_hash, kind, reason, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, principal_id, "a" * 64, "ban", "test", "admin", now),
        )

    command.downgrade(cfg, "0059")

    assert TABLE_NAME not in _table_names(sqlite_db)


def test_ranked_ban_model_matches_0060(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    table = cast(Table, RankedBan.__table__)
    for column_name in {"kind", "reason", "created_by", "email_hash"}:
        assert isinstance(table.columns[column_name].type, String)
    for column_name in {"created_at", "expires_at", "lifted_at"}:
        assert isinstance(table.columns[column_name].type, DateTime)
    assert table.columns["email_hash"].nullable is True
    assert table.columns["expires_at"].nullable is True
    assert table.columns["lifted_at"].nullable is True
    indexes = {str(index.name): index for index in table.indexes}
    assert EXPECTED_INDEXES.issubset(indexes)
