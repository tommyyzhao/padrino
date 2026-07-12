"""US-349: migration 0070 archives safe OpenRouter routing attribution."""

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
from sqlalchemy import JSON, String, Table, Uuid

from padrino.db.models import LlmCall

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

ADDED_COLUMNS = {
    "openrouter_requested_model_slug",
    "openrouter_actual_model",
    "openrouter_actual_provider",
    "openrouter_actual_endpoint",
    "openrouter_generation_id",
    "openrouter_routing_metadata",
    "openrouter_catalog_snapshot_id",
    "openrouter_routing_policy_hash",
}


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


def _column_info(db_path: Path) -> dict[str, sqlite3.Row]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("PRAGMA table_info(llm_calls)").fetchall()
    return {str(row["name"]): row for row in rows}


def test_0070_follows_0069_linearly_and_round_trips_openrouter_audit_columns(
    sqlite_db: Path,
) -> None:
    config = _alembic_config()
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["0074"]
    assert script.get_revision("0070").down_revision == "0069"

    command.upgrade(config, "0069")
    before = set(_column_info(sqlite_db))
    assert ADDED_COLUMNS.isdisjoint(before)

    command.upgrade(config, "0070")
    after = _column_info(sqlite_db)
    assert set(after) - before == ADDED_COLUMNS
    assert all(after[column]["notnull"] == 0 for column in ADDED_COLUMNS)

    command.downgrade(config, "0069")
    assert set(_column_info(sqlite_db)) == before


def test_llm_call_orm_openrouter_audit_columns_match_0070_schema() -> None:
    table = cast(Table, LlmCall.__table__)

    for name in (
        "openrouter_requested_model_slug",
        "openrouter_actual_model",
        "openrouter_actual_provider",
        "openrouter_actual_endpoint",
        "openrouter_generation_id",
        "openrouter_routing_policy_hash",
    ):
        assert isinstance(table.columns[name].type, String)
        assert table.columns[name].nullable is True
    assert isinstance(table.columns["openrouter_routing_metadata"].type, JSON)
    assert table.columns["openrouter_routing_metadata"].nullable is True
    assert isinstance(table.columns["openrouter_catalog_snapshot_id"].type, Uuid)
    assert table.columns["openrouter_catalog_snapshot_id"].nullable is True
