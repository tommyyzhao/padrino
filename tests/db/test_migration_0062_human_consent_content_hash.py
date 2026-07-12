"""US-303: migration 0062 binds human consent rows to document hashes."""

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

from padrino.db.models import HumanConsent

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

TABLE_NAME = "human_consents"
ADDED_COLUMNS = {"content_hash"}


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


def test_0062_is_linear_head_after_0061() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0072"]
    assert script.get_revision("0068").down_revision == "0067"
    assert script.get_revision("0067").down_revision == "0066"
    assert script.get_revision("0066").down_revision == "0065"
    assert script.get_revision("0063").down_revision == "0062"
    assert script.get_revision("0062").down_revision == "0061"
    assert script.get_revision("0061").down_revision == "0060"


def test_0062_adds_nullable_human_consent_content_hash(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0060")

    before_columns = set(_column_info(sqlite_db, TABLE_NAME))
    assert ADDED_COLUMNS.isdisjoint(before_columns)

    command.upgrade(cfg, "0062")

    info = _column_info(sqlite_db, TABLE_NAME)
    assert set(info) - before_columns == ADDED_COLUMNS
    assert info["content_hash"]["notnull"] == 0

    command.downgrade(cfg, "0060")

    assert set(_column_info(sqlite_db, TABLE_NAME)) == before_columns


def test_human_consent_model_matches_0062_content_hash(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    table = cast(Table, HumanConsent.__table__)
    assert isinstance(table.columns["content_hash"].type, String)
    assert table.columns["content_hash"].nullable is True
