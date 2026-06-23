"""US-239: migration 0050 adds hot-path secondary indexes."""

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
from sqlalchemy import Table

from padrino.db.models import Game, GameEvent, LlmCall

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

EXPECTED_INDEXES: dict[str, dict[str, tuple[str, ...]]] = {
    "games": {
        "ix_games_completed_at_is_broadcastable": ("completed_at", "is_broadcastable"),
    },
    "game_events": {
        "ix_game_events_game_id": ("game_id",),
    },
    "llm_calls": {
        "ix_llm_calls_game_id_agent_build_id_event_id": (
            "game_id",
            "agent_build_id",
            "event_id",
        ),
        "ix_llm_calls_game_id_raw_response_present": ("game_id",),
    },
}


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


def _index_names(db_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (table_name,),
        ).fetchall()
    return {r[0] for r in rows}


def _index_columns(db_path: Path, index_name: str) -> tuple[str, ...]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA index_info({index_name})").fetchall()
    return tuple(r[2] for r in sorted(rows, key=lambda r: r[0]))


def _index_sql(db_path: Path, index_name: str) -> str:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _model_index_columns(table: Table) -> dict[str, tuple[str, ...]]:
    return {
        str(idx.name): tuple(str(col.name) for col in idx.columns)
        for idx in table.indexes
        if idx.name is not None
    }


def test_0050_is_linear_head_after_0049() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0050"]
    assert script.get_revision("0050").down_revision == "0049"


def test_0050_creates_and_downgrades_only_hot_path_indexes(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0049")
    before = {table: _index_names(sqlite_db, table) for table in EXPECTED_INDEXES}

    for table, indexes in EXPECTED_INDEXES.items():
        assert before[table].isdisjoint(indexes)

    command.upgrade(cfg, "head")

    for table, indexes in EXPECTED_INDEXES.items():
        names = _index_names(sqlite_db, table)
        assert set(indexes).issubset(names)
        for index_name, columns in indexes.items():
            assert _index_columns(sqlite_db, index_name) == columns

    assert "WHERE raw_response IS NOT NULL" in _index_sql(
        sqlite_db, "ix_llm_calls_game_id_raw_response_present"
    )

    command.downgrade(cfg, "0049")

    for table in EXPECTED_INDEXES:
        assert _index_names(sqlite_db, table) == before[table]


def test_hot_path_index_metadata_matches_0050_migration(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    model_indexes = {
        "games": _model_index_columns(cast(Table, Game.__table__)),
        "game_events": _model_index_columns(cast(Table, GameEvent.__table__)),
        "llm_calls": _model_index_columns(cast(Table, LlmCall.__table__)),
    }

    for table, indexes in EXPECTED_INDEXES.items():
        for index_name, columns in indexes.items():
            assert model_indexes[table][index_name] == columns
            assert _index_columns(sqlite_db, index_name) == columns
