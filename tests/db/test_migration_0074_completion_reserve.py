"""US-361: completion-reserve cap migration coverage."""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def sqlite_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "padrino_test.db"
        monkeypatch.setenv("PADRINO_DB_URL", f"sqlite+aiosqlite:///{db_path}")
        yield db_path


def _config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(REPO_ROOT / "src/padrino/db/migrations"))
    return config


def _columns(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def test_0074_follows_0073_and_round_trips_completion_reserve_state(sqlite_db: Path) -> None:
    config = _config()
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["0074"]
    assert script.get_revision("0074").down_revision == "0073"

    command.upgrade(config, "0073")
    assert "completion_reserve_per_game_cap_micros" not in _columns(
        sqlite_db, "promotional_credit_reservations"
    )
    assert {
        "operator_completion_reserve_cursors",
        "operator_completion_reserve_settlements",
    }.isdisjoint(_tables(sqlite_db))

    command.upgrade(config, "0074")
    assert {
        "completion_reserve_per_game_cap_micros",
        "completion_reserve_unfunded_policy",
    }.issubset(_columns(sqlite_db, "promotional_credit_reservations"))
    assert {
        "operator_completion_reserve_cursors",
        "operator_completion_reserve_settlements",
    }.issubset(_tables(sqlite_db))

    command.downgrade(config, "0073")
    assert "completion_reserve_per_game_cap_micros" not in _columns(
        sqlite_db, "promotional_credit_reservations"
    )
    assert {
        "operator_completion_reserve_cursors",
        "operator_completion_reserve_settlements",
    }.isdisjoint(_tables(sqlite_db))
