"""US-295: migration 0057 marks quarantined human-rating rows."""

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
from sqlalchemy import DateTime, Table

from padrino.db.models import HumanRating

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

TABLE_NAME = "human_rating"
ADDED_COLUMNS = {"quarantined_at"}


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


def test_0057_is_linear_head_after_0056() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0060"]
    assert script.get_revision("0060").down_revision == "0059"
    assert script.get_revision("0059").down_revision == "0058"
    assert script.get_revision("0058").down_revision == "0057"
    assert script.get_revision("0057").down_revision == "0056"
    assert script.get_revision("0056").down_revision == "0055"


def test_0057_adds_nullable_human_rating_quarantine_column(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0056")

    before_columns = set(_column_info(sqlite_db, TABLE_NAME))
    assert ADDED_COLUMNS.isdisjoint(before_columns)

    command.upgrade(cfg, "0057")

    info = _column_info(sqlite_db, TABLE_NAME)
    assert set(info) - before_columns == ADDED_COLUMNS
    assert info["quarantined_at"]["notnull"] == 0

    command.downgrade(cfg, "0056")

    assert set(_column_info(sqlite_db, TABLE_NAME)) == before_columns


def test_human_rating_model_matches_0057_quarantine_column(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    table = cast(Table, HumanRating.__table__)
    assert isinstance(table.columns["quarantined_at"].type, DateTime)
    assert table.columns["quarantined_at"].nullable is True
