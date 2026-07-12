"""US-348: persisted secret-free lobby AI model-selection snapshots."""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from padrino.db.models import LobbySeat

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def sqlite_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "padrino_test.db"
        monkeypatch.setenv("PADRINO_DB_URL", f"sqlite+aiosqlite:///{db_path}")
        yield db_path


def _alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(REPO_ROOT / "src/padrino/db/migrations"))
    return config


def _column_names(db_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})")}


def test_0069_follows_0068_linearly_and_round_trips_lobby_model_selection(
    sqlite_db: Path,
) -> None:
    config = _alembic_config()
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["0069"]
    assert script.get_revision("0069").down_revision == "0068"

    command.upgrade(config, "0069")
    assert "model_selection" in _column_names(sqlite_db, "lobby_seats")

    command.downgrade(config, "0068")
    assert "model_selection" not in _column_names(sqlite_db, "lobby_seats")


def test_lobby_seat_orm_model_selection_is_nullable_json() -> None:
    column = LobbySeat.__table__.c["model_selection"]
    assert column.nullable is True
    assert column.type.__class__.__name__ == "JSON"
