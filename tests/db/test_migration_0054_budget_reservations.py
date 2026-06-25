"""US-263: migration 0054 adds generic budget reservation slots."""

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
from sqlalchemy import DateTime, Integer, String, Table, UniqueConstraint, Uuid

from padrino.db.models import BudgetReservationSlot

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

TABLE_NAME = "budget_reservation_slots"
UNIQUE_NAME = "uq_budget_reservation_slot"
EXPECTED_COLUMNS = {
    "id",
    "scope_key",
    "slot_index",
    "reserved_at",
    "binding_key",
    "released_at",
}
UNIQUE_COLUMNS = ("scope_key", "slot_index")


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


def _column_names(db_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _column_info(db_path: Path, table_name: str) -> dict[str, sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]): row for row in rows}


def _index_columns(db_path: Path, index_name: str) -> tuple[str, ...]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA index_info({index_name})").fetchall()
    return tuple(str(row[2]) for row in sorted(rows, key=lambda row: row[0]))


def _unique_column_sets(db_path: Path, table_name: str) -> set[tuple[str, ...]]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA index_list({table_name})").fetchall()
        unique_indexes = [str(row[1]) for row in rows if int(row[2]) == 1]
    return {_index_columns(db_path, index_name) for index_name in unique_indexes}


def _model_unique_columns(table: Table) -> dict[str, tuple[str, ...]]:
    return {
        str(constraint.name): tuple(str(column.name) for column in constraint.columns)
        for constraint in table.constraints
        if constraint.name is not None and isinstance(constraint, UniqueConstraint)
    }


def test_0054_is_linear_head_after_0053() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0054"]
    assert script.get_revision("0054").down_revision == "0053"


def test_0054_adds_budget_reservation_slots_table(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0053")

    before_tables = _table_names(sqlite_db)
    assert TABLE_NAME not in before_tables

    command.upgrade(cfg, "head")

    assert _table_names(sqlite_db) - before_tables == {TABLE_NAME}
    assert _column_names(sqlite_db, TABLE_NAME) == EXPECTED_COLUMNS
    assert UNIQUE_COLUMNS in _unique_column_sets(sqlite_db, TABLE_NAME)

    info = _column_info(sqlite_db, TABLE_NAME)
    assert info["scope_key"]["notnull"] == 1
    assert info["slot_index"]["notnull"] == 1
    assert info["reserved_at"]["notnull"] == 1
    assert info["binding_key"]["notnull"] == 0
    assert info["released_at"]["notnull"] == 0

    command.downgrade(cfg, "0053")

    assert _table_names(sqlite_db) == before_tables


def test_budget_reservation_slot_model_matches_0054_migration(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    table = cast(Table, BudgetReservationSlot.__table__)
    column_types = {
        "id": Uuid,
        "scope_key": String,
        "slot_index": Integer,
        "reserved_at": DateTime,
        "binding_key": String,
        "released_at": DateTime,
    }
    for column_name, expected_type in column_types.items():
        assert isinstance(table.columns[column_name].type, expected_type)

    assert table.columns["binding_key"].nullable is True
    assert table.columns["released_at"].nullable is True
    assert _model_unique_columns(table)[UNIQUE_NAME] == UNIQUE_COLUMNS
