"""US-362: call-charge settlement migration coverage."""

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


def _indexes(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {str(row[1]) for row in connection.execute(f"PRAGMA index_list({table})")}


def test_0075_binds_settlements_to_calls_and_round_trips(sqlite_db: Path) -> None:
    config = _config()
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["0075"]
    assert script.get_revision("0075").down_revision == "0074"

    command.upgrade(config, "0074")
    assert {"charge_idempotency_key", "charge_amount_micros"}.isdisjoint(
        _columns(sqlite_db, "llm_calls")
    )
    assert "llm_call_id" not in _columns(sqlite_db, "operator_completion_reserve_settlements")

    command.upgrade(config, "0075")
    assert {"charge_idempotency_key", "charge_amount_micros"}.issubset(
        _columns(sqlite_db, "llm_calls")
    )
    assert "llm_call_id" in _columns(sqlite_db, "operator_completion_reserve_settlements")
    assert "uq_llm_calls_charge_idempotency_key" in _indexes(sqlite_db, "llm_calls")
    assert "uq_operator_completion_reserve_settlements_llm_call" in _indexes(
        sqlite_db, "operator_completion_reserve_settlements"
    )

    command.downgrade(config, "0074")
    assert {"charge_idempotency_key", "charge_amount_micros"}.isdisjoint(
        _columns(sqlite_db, "llm_calls")
    )
    assert "llm_call_id" not in _columns(sqlite_db, "operator_completion_reserve_settlements")
