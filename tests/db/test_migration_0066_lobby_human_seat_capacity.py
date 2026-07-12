"""US-337: migration 0066 persists lobby human-seat capacity."""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from padrino.db.models import Lobby

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


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


def test_0066_is_linear_head_after_0065() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0072"]
    assert script.get_revision("0068").down_revision == "0067"
    assert script.get_revision("0067").down_revision == "0066"
    assert script.get_revision("0066").down_revision == "0065"
    assert script.get_revision("0065").down_revision == "0064"


def test_0066_adds_nullable_lobby_human_capacity(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0065")

    before = set(_column_info(sqlite_db, "lobbies"))
    assert "human_seat_capacity" not in before

    command.upgrade(cfg, "0066")

    info = _column_info(sqlite_db, "lobbies")
    assert info["human_seat_capacity"]["notnull"] == 0

    command.downgrade(cfg, "0065")

    assert set(_column_info(sqlite_db, "lobbies")) == before


def test_lobby_model_matches_0066_schema(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    column = Lobby.__table__.columns["human_seat_capacity"]
    assert column.nullable is True
