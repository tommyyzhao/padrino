"""US-334: migration 0065 persists immutable rules snapshots on lobbies/games."""

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
from sqlalchemy import JSON, String, Table

from padrino.db.models import Game, Lobby

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

ADDED_COLUMNS = {"rules_snapshot_json", "rules_snapshot_hash"}


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


def _table_sql(db_path: Path, table_name: str) -> str:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_0065_is_linear_head_after_0064() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0072"]
    assert script.get_revision("0068").down_revision == "0067"
    assert script.get_revision("0067").down_revision == "0066"
    assert script.get_revision("0066").down_revision == "0065"
    assert script.get_revision("0065").down_revision == "0064"
    assert script.get_revision("0064").down_revision == "0063"


def test_0065_adds_nullable_snapshot_columns_and_constraints(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0064")

    before_games = set(_column_info(sqlite_db, "games"))
    before_lobbies = set(_column_info(sqlite_db, "lobbies"))
    assert ADDED_COLUMNS.isdisjoint(before_games)
    assert ADDED_COLUMNS.isdisjoint(before_lobbies)

    command.upgrade(cfg, "0065")

    for table_name in ("games", "lobbies"):
        info = _column_info(sqlite_db, table_name)
        assert set(info) >= ADDED_COLUMNS
        assert info["rules_snapshot_json"]["notnull"] == 0
        assert info["rules_snapshot_hash"]["notnull"] == 0
        sql = _table_sql(sqlite_db, table_name)
        assert f"ck_{table_name}_rules_snapshot_pair" in sql
        assert f"ck_{table_name}_rules_snapshot_hash_len" in sql

    command.downgrade(cfg, "0064")

    assert set(_column_info(sqlite_db, "games")) == before_games
    assert set(_column_info(sqlite_db, "lobbies")) == before_lobbies


def test_0065_rejects_one_sided_snapshot_pairs(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0065")

    with sqlite3.connect(sqlite_db) as conn:
        now = "2026-07-01 00:00:00"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO games "
                "(id, ruleset_id, game_seed, status, created_at, broadcast_state, "
                "is_broadcastable, identity_mode, rules_snapshot_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    "mini7_v1",
                    "seed",
                    "CREATED",
                    now,
                    "HIDDEN",
                    0,
                    "ANONYMOUS",
                    '{"player_count":7}',
                ),
            )

        principal_id = uuid.uuid4().hex
        league_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO principals (id, kind, display_name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (principal_id, "guest", None, now, now),
        )
        conn.execute(
            "INSERT INTO leagues (id, name, ruleset_id, ranked, kind, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (league_id, "humans", "mini7_v1", 0, "HUMANS_INCLUDED", now),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO lobbies "
                "(id, ruleset_id, identity_mode, invite_token, theme_pack_id, stakes, "
                "integrity_acknowledged, status, lobby_seed, host_principal_id, league_id, "
                "created_at, updated_at, rules_snapshot_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    "mini7_v1",
                    "ANONYMOUS",
                    "invite",
                    None,
                    "CASUAL",
                    0,
                    "OPEN",
                    "seed",
                    principal_id,
                    league_id,
                    now,
                    now,
                    "a" * 64,
                ),
            )


def test_rules_snapshot_models_match_0065(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    for model in (Game, Lobby):
        table = cast(Table, model.__table__)
        assert isinstance(table.columns["rules_snapshot_json"].type, JSON)
        assert table.columns["rules_snapshot_json"].nullable is True
        assert isinstance(table.columns["rules_snapshot_hash"].type, String)
        assert table.columns["rules_snapshot_hash"].nullable is True
        constraint_names = {constraint.name for constraint in table.constraints}
        assert f"ck_{table.name}_rules_snapshot_pair" in constraint_names
        assert f"ck_{table.name}_rules_snapshot_hash_len" in constraint_names
