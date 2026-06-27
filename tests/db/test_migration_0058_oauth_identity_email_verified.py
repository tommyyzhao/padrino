"""US-296: migration 0058 persists OAuth email verification claims."""

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
from sqlalchemy import Boolean, String, Table

from padrino.db.models import OAuthIdentity

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

TABLE_NAME = "oauth_identities"
ADDED_COLUMNS = {"email", "email_verified"}


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


def test_0058_is_linear_head_after_0057() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0063"]
    assert script.get_revision("0062").down_revision == "0060"
    assert script.get_revision("0063").down_revision == "0062"
    assert script.get_revision("0060").down_revision == "0059"
    assert script.get_revision("0059").down_revision == "0058"
    assert script.get_revision("0058").down_revision == "0057"
    assert script.get_revision("0057").down_revision == "0056"


def test_0058_adds_oauth_email_columns_round_trip(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0057")

    before_columns = set(_column_info(sqlite_db, TABLE_NAME))
    assert ADDED_COLUMNS.isdisjoint(before_columns)

    command.upgrade(cfg, "0058")

    info = _column_info(sqlite_db, TABLE_NAME)
    assert set(info) - before_columns == ADDED_COLUMNS
    assert info["email"]["notnull"] == 0
    assert info["email_verified"]["notnull"] == 1
    assert info["email_verified"]["dflt_value"] is not None
    assert all(
        columns != ["email"] or not unique
        for unique, columns in _index_columns(sqlite_db, TABLE_NAME).values()
    )

    command.downgrade(cfg, "0057")

    assert set(_column_info(sqlite_db, TABLE_NAME)) == before_columns


def test_oauth_identity_model_matches_0058_email_columns(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    table = cast(Table, OAuthIdentity.__table__)
    assert isinstance(table.columns["email"].type, String)
    assert table.columns["email"].nullable is True
    assert isinstance(table.columns["email_verified"].type, Boolean)
    assert table.columns["email_verified"].nullable is False
