"""US-266: migration 0055 stamps immutable LLM-call pricing basis."""

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
from sqlalchemy import String, Table

from padrino.db.models import LlmCall

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

TABLE_NAME = "llm_calls"
ADDED_COLUMNS = {"price_basis", "price_table_version"}


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


def _column_names(db_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _column_info(db_path: Path, table_name: str) -> dict[str, sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]): row for row in rows}


def test_0055_is_linear_head_after_0054() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0055"]
    assert script.get_revision("0055").down_revision == "0054"


def test_0055_adds_nullable_llm_call_pricing_columns(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0054")

    before_columns = _column_names(sqlite_db, TABLE_NAME)
    assert ADDED_COLUMNS.isdisjoint(before_columns)

    command.upgrade(cfg, "head")

    assert _column_names(sqlite_db, TABLE_NAME) - before_columns == ADDED_COLUMNS
    info = _column_info(sqlite_db, TABLE_NAME)
    assert info["price_basis"]["notnull"] == 0
    assert info["price_table_version"]["notnull"] == 0

    command.downgrade(cfg, "0054")

    assert _column_names(sqlite_db, TABLE_NAME) == before_columns


def test_llm_call_model_matches_0055_pricing_columns(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    table = cast(Table, LlmCall.__table__)
    for column_name in ADDED_COLUMNS:
        assert isinstance(table.columns[column_name].type, String)
        assert table.columns[column_name].nullable is True
