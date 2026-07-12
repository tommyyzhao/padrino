"""US-301: migration 0061 creates ranked game exclusions and rollback audit."""

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
from sqlalchemy import JSON, DateTime, String, Table

from padrino.db.models import RankedGameExclusion, RankedRatingRollbackAudit

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

EXCLUSION_TABLE = "ranked_game_exclusions"
AUDIT_TABLE = "ranked_rating_rollback_audit"


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


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {str(row[0]) for row in rows}


def _column_info(db_path: Path, table_name: str) -> dict[str, sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]): row for row in rows}


def _index_names(db_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (table_name,),
        ).fetchall()
    return {str(row[0]) for row in rows}


def test_0061_is_inserted_linearly_between_0060_and_0062() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0075"]
    assert script.get_revision("0068").down_revision == "0067"
    assert script.get_revision("0067").down_revision == "0066"
    assert script.get_revision("0066").down_revision == "0065"
    assert script.get_revision("0061").down_revision == "0060"
    assert script.get_revision("0062").down_revision == "0061"
    assert script.get_revision("0060").down_revision == "0059"


def test_0061_creates_exclusion_and_rollback_audit_round_trip(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0060")

    assert EXCLUSION_TABLE not in _table_names(sqlite_db)
    assert AUDIT_TABLE not in _table_names(sqlite_db)

    command.upgrade(cfg, "0061")

    assert EXCLUSION_TABLE in _table_names(sqlite_db)
    assert AUDIT_TABLE in _table_names(sqlite_db)
    exclusion_columns = _column_info(sqlite_db, EXCLUSION_TABLE)
    audit_columns = _column_info(sqlite_db, AUDIT_TABLE)
    assert set(exclusion_columns) == {
        "id",
        "principal_id",
        "human_player_id",
        "game_id",
        "reason",
        "created_by",
        "created_at",
    }
    assert set(audit_columns) == {
        "id",
        "principal_id",
        "human_player_id",
        "reason",
        "excluded_game_ids",
        "exclusion_hash",
        "created_by",
        "created_at",
    }
    assert "uq_ranked_game_exclusions_principal_game" in _index_names(sqlite_db, EXCLUSION_TABLE)
    assert "uq_ranked_rating_rollback_audit_principal_hash" in _index_names(sqlite_db, AUDIT_TABLE)

    with sqlite3.connect(sqlite_db) as conn:
        now = "2026-06-27 00:00:00"
        principal_id = uuid.uuid4().hex
        game_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO principals (id, kind, display_name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (principal_id, "account", "Rollback", now, now),
        )
        conn.execute(
            "INSERT INTO games (id, ruleset_id, game_seed, status, created_at, broadcast_state, "
            "is_broadcastable, identity_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (game_id, "mini7_v1", "seed", "COMPLETED", now, "HIDDEN", 0, "ANONYMOUS"),
        )
        conn.execute(
            "INSERT INTO ranked_game_exclusions "
            "(id, principal_id, human_player_id, game_id, reason, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, principal_id, principal_id, game_id, "test", "admin", now),
        )
        conn.execute(
            "INSERT INTO ranked_rating_rollback_audit "
            "(id, principal_id, human_player_id, reason, excluded_game_ids, exclusion_hash, "
            "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                principal_id,
                principal_id,
                "test",
                f'["{game_id}"]',
                "a" * 64,
                "admin",
                now,
            ),
        )

    command.downgrade(cfg, "0060")

    assert EXCLUSION_TABLE not in _table_names(sqlite_db)
    assert AUDIT_TABLE not in _table_names(sqlite_db)


def test_ranked_game_exclusion_models_match_0061(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    exclusion_table = cast(Table, RankedGameExclusion.__table__)
    audit_table = cast(Table, RankedRatingRollbackAudit.__table__)
    for column_name in {"human_player_id", "reason", "created_by"}:
        assert isinstance(exclusion_table.columns[column_name].type, String)
    assert isinstance(exclusion_table.columns["created_at"].type, DateTime)
    assert isinstance(audit_table.columns["excluded_game_ids"].type, JSON)
    assert isinstance(audit_table.columns["exclusion_hash"].type, String)
    assert isinstance(audit_table.columns["created_at"].type, DateTime)
    assert {
        "uq_ranked_game_exclusions_principal_game",
        "ix_ranked_game_exclusions_game_id",
    }.issubset({str(index.name) for index in exclusion_table.indexes})
    assert {
        "uq_ranked_rating_rollback_audit_principal_hash",
    }.issubset({str(index.name) for index in audit_table.indexes})
