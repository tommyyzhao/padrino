"""US-297: migration 0059 adds pseudonymous ranked handles."""

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
from sqlalchemy import Index, String, Table

from padrino.db.models import Principal

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

TABLE_NAME = "principals"
ADDED_COLUMNS = {"ranked_handle", "ranked_handle_normalized"}
UNIQUE_INDEX_NAME = "uq_principals_ranked_handle_normalized"


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


def _column_info(db_path: Path, table_name: str) -> dict[str, sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]): row for row in rows}


def _index_columns(db_path: Path, table_name: str) -> dict[str, tuple[bool, list[str]]]:
    indexes: dict[str, tuple[bool, list[str]]] = {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(f"PRAGMA index_list({table_name})").fetchall():
            name = str(row["name"])
            columns = [
                str(info["name"]) for info in conn.execute(f"PRAGMA index_info({name})").fetchall()
            ]
            indexes[name] = (bool(row["unique"]), columns)
    return indexes


def test_0059_is_linear_head_after_0058() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0064"]
    assert script.get_revision("0061").down_revision == "0060"
    assert script.get_revision("0062").down_revision == "0061"
    assert script.get_revision("0063").down_revision == "0062"
    assert script.get_revision("0060").down_revision == "0059"
    assert script.get_revision("0059").down_revision == "0058"
    assert script.get_revision("0058").down_revision == "0057"


def test_0059_adds_ranked_handle_columns_and_unique_normalized_index(
    sqlite_db: Path,
) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0058")

    before_columns = set(_column_info(sqlite_db, TABLE_NAME))
    assert ADDED_COLUMNS.isdisjoint(before_columns)
    assert UNIQUE_INDEX_NAME not in _index_columns(sqlite_db, TABLE_NAME)

    command.upgrade(cfg, "0059")

    info = _column_info(sqlite_db, TABLE_NAME)
    assert set(info) - before_columns == ADDED_COLUMNS
    assert info["ranked_handle"]["notnull"] == 0
    assert info["ranked_handle_normalized"]["notnull"] == 0
    assert _index_columns(sqlite_db, TABLE_NAME)[UNIQUE_INDEX_NAME] == (
        True,
        ["ranked_handle_normalized"],
    )

    with sqlite3.connect(sqlite_db) as conn:
        now = "2026-06-27 00:00:00"
        conn.execute(
            "INSERT INTO principals "
            "(id, kind, display_name, created_at, updated_at, ranked_handle, "
            "ranked_handle_normalized) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, "account", "one", now, now, "RankedHero", "rankedhero"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO principals "
                "(id, kind, display_name, created_at, updated_at, ranked_handle, "
                "ranked_handle_normalized) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, "account", "two", now, now, "rankedhero", "rankedhero"),
            )

    command.downgrade(cfg, "0058")

    assert set(_column_info(sqlite_db, TABLE_NAME)) == before_columns
    assert UNIQUE_INDEX_NAME not in _index_columns(sqlite_db, TABLE_NAME)


def test_principal_model_matches_0059_ranked_handle_columns(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    table = cast(Table, Principal.__table__)
    assert isinstance(table.columns["ranked_handle"].type, String)
    assert table.columns["ranked_handle"].nullable is True
    assert isinstance(table.columns["ranked_handle_normalized"].type, String)
    assert table.columns["ranked_handle_normalized"].nullable is True
    indexes = {str(index.name): index for index in table.indexes}
    index = indexes[UNIQUE_INDEX_NAME]
    assert isinstance(index, Index)
    assert index.unique is True
    assert [column.name for column in index.columns] == ["ranked_handle_normalized"]
