"""US-251: migration 0052 adds per-game lease metadata additively."""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import DateTime, Integer, String, Table, Text

from padrino.db.models import Game

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

LEASE_COLUMNS = {
    "leased_by",
    "lease_expires_at",
    "attempt_count",
    "last_error",
    "last_error_kind",
}
LEASE_INDEX = "ix_games_status_lease_expires_at"
LEASE_INDEX_COLUMNS = ("status", "lease_expires_at")


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


def _game_column_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("PRAGMA table_info(games)").fetchall()
    return {str(row[1]) for row in rows}


def _game_column_info(db_path: Path) -> dict[str, sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("PRAGMA table_info(games)").fetchall()
    return {str(row["name"]): row for row in rows}


def _index_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='games'"
        ).fetchall()
    return {str(row[0]) for row in rows}


def _index_columns(db_path: Path, index_name: str) -> tuple[str, ...]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA index_info({index_name})").fetchall()
    return tuple(str(row[2]) for row in sorted(rows, key=lambda row: row[0]))


def _insert_existing_game(db_path: Path) -> str:
    game_id = uuid.uuid4().hex
    now = "2026-06-24 00:00:00"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO games (
                id,
                gauntlet_id,
                pair_id,
                pair_leg,
                ruleset_id,
                game_seed,
                status,
                terminal_result,
                created_at,
                started_at,
                completed_at,
                current_phase,
                event_hash_head,
                broadcast_state,
                is_broadcastable,
                identity_mode
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                game_id,
                None,
                None,
                None,
                "mini7_v1",
                "lease-seed",
                "RUNNING",
                None,
                now,
                None,
                None,
                "DAY_1",
                None,
                "HIDDEN",
                0,
                "ANONYMOUS",
            ),
        )
    return game_id


def _model_index_columns(table: Table) -> dict[str, tuple[str, ...]]:
    return {
        str(index.name): tuple(str(column.name) for column in index.columns)
        for index in table.indexes
        if index.name is not None
    }


def test_0052_is_linear_after_0051() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_revision("0052").down_revision == "0051"


def test_0052_adds_nullable_game_lease_columns_and_index(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0051")

    before_columns = _game_column_names(sqlite_db)
    before_indexes = _index_names(sqlite_db)
    assert before_columns.isdisjoint(LEASE_COLUMNS)
    assert LEASE_INDEX not in before_indexes

    game_id = _insert_existing_game(sqlite_db)

    command.upgrade(cfg, "head")

    after_columns = _game_column_names(sqlite_db)
    after_indexes = _index_names(sqlite_db)
    assert after_columns - before_columns == LEASE_COLUMNS
    assert after_indexes - before_indexes == {LEASE_INDEX}
    assert _index_columns(sqlite_db, LEASE_INDEX) == LEASE_INDEX_COLUMNS

    column_info = _game_column_info(sqlite_db)
    for column_name in LEASE_COLUMNS:
        assert column_info[column_name]["notnull"] == 0
        assert column_info[column_name]["dflt_value"] is None

    with sqlite3.connect(sqlite_db) as conn:
        row = conn.execute(
            """
            SELECT leased_by, lease_expires_at, attempt_count, last_error, last_error_kind
            FROM games
            WHERE id = ?
            """,
            (game_id,),
        ).fetchone()
    assert row == (None, None, None, None, None)

    command.downgrade(cfg, "0051")

    assert _game_column_names(sqlite_db) == before_columns
    assert _index_names(sqlite_db) == before_indexes


def test_game_model_metadata_matches_0052_migration(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    table = cast(Table, Game.__table__)
    expected_types = {
        "leased_by": String,
        "lease_expires_at": DateTime,
        "attempt_count": Integer,
        "last_error": Text,
        "last_error_kind": String,
    }

    for column_name, expected_type in expected_types.items():
        column = table.columns[column_name]
        assert column.nullable is True
        assert isinstance(column.type, expected_type)

    assert _model_index_columns(table)[LEASE_INDEX] == LEASE_INDEX_COLUMNS
    assert _index_columns(sqlite_db, LEASE_INDEX) == LEASE_INDEX_COLUMNS
