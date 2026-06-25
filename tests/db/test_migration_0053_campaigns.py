"""US-258: migration 0053 persists benchmark campaigns and pairing cells."""

from __future__ import annotations

import json
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
from sqlalchemy import JSON, DateTime, Integer, Numeric, String, Table, Text, UniqueConstraint, Uuid

from padrino.db.models import Campaign, CampaignPairing, Gauntlet

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

CAMPAIGN_TABLE = "campaigns"
PAIRING_TABLE = "campaign_pairings"
GAUNTLET_CAMPAIGN_INDEX = "ix_gauntlets_campaign_id"
PAIRING_CELL_UNIQUE_COLUMNS = ("campaign_id", "cell_index")

CAMPAIGN_COLUMNS = {
    "id",
    "campaign_seed",
    "ruleset_id",
    "league_id",
    "format",
    "player_count",
    "per_model_game_target",
    "status",
    "leased_by",
    "lease_expires_at",
    "heartbeat_at",
    "created_at",
    "completed_at",
    "sigma_target",
    "rank_stability_k",
}
PAIRING_COLUMNS = {
    "id",
    "campaign_id",
    "cell_index",
    "roster_json",
    "status",
    "attempt_count",
    "last_error",
    "gauntlet_id",
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


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {str(row[0]) for row in rows}


def _column_names(db_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


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


def _index_columns(db_path: Path, index_name: str) -> tuple[str, ...]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA index_info({index_name})").fetchall()
    return tuple(str(row[2]) for row in sorted(rows, key=lambda row: row[0]))


def _unique_column_sets(db_path: Path, table_name: str) -> set[tuple[str, ...]]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA index_list({table_name})").fetchall()
        unique_indexes = [str(row[1]) for row in rows if int(row[2]) == 1]
    return {_index_columns(db_path, index_name) for index_name in unique_indexes}


def _foreign_keys(db_path: Path, table_name: str) -> set[tuple[str, str, str]]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
    return {(str(row[3]), str(row[2]), str(row[4])) for row in rows}


def _insert_existing_gauntlet(db_path: Path) -> str:
    league_id = uuid.uuid4().hex
    prompt_version_id = uuid.uuid4().hex
    gauntlet_id = uuid.uuid4().hex
    now = "2026-06-24 00:00:00"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO leagues (id, name, ruleset_id, ranked, created_at, kind)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (league_id, "Scientific", "mini7_v1", 1, now, "SCIENTIFIC"),
        )
        conn.execute(
            """
            INSERT INTO prompt_versions (
                id,
                ruleset_id,
                version,
                system_prompt,
                developer_prompt,
                response_schema,
                prompt_hash,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prompt_version_id,
                "mini7_v1",
                "v1",
                "system",
                "developer",
                json.dumps({"type": "object"}),
                "campaign-prompt-hash",
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO gauntlets (
                id,
                league_id,
                ruleset_id,
                prompt_version_id,
                clone_count,
                gauntlet_seed,
                ranked,
                status,
                created_at,
                completed_at,
                heartbeat_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                gauntlet_id,
                league_id,
                "mini7_v1",
                prompt_version_id,
                7,
                "campaignless-seed",
                1,
                "PENDING",
                now,
                None,
                None,
            ),
        )
    return gauntlet_id


def _insert_campaign(db_path: Path) -> str:
    league_id = uuid.uuid4().hex
    campaign_id = uuid.uuid4().hex
    now = "2026-06-24 00:00:00"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO leagues (id, name, ruleset_id, ranked, created_at, kind)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (league_id, "Scientific Campaign", "mini7_v1", 1, now, "SCIENTIFIC"),
        )
        conn.execute(
            """
            INSERT INTO campaigns (
                id,
                campaign_seed,
                ruleset_id,
                league_id,
                format,
                player_count,
                per_model_game_target,
                status,
                leased_by,
                lease_expires_at,
                heartbeat_at,
                created_at,
                completed_at,
                sigma_target,
                rank_stability_k
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                "campaign-seed",
                "mini7_v1",
                league_id,
                "MIRROR",
                7,
                50,
                "PENDING",
                None,
                None,
                None,
                now,
                None,
                2.5,
                10,
            ),
        )
    return campaign_id


def _model_index_columns(table: Table) -> dict[str, tuple[str, ...]]:
    return {
        str(index.name): tuple(str(column.name) for column in index.columns)
        for index in table.indexes
        if index.name is not None
    }


def _model_unique_columns(table: Table) -> dict[str, tuple[str, ...]]:
    return {
        str(constraint.name): tuple(str(column.name) for column in constraint.columns)
        for constraint in table.constraints
        if constraint.name is not None and isinstance(constraint, UniqueConstraint)
    }


def test_0053_is_linear_head_after_0052() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0053"]
    assert script.get_revision("0053").down_revision == "0052"


def test_0053_adds_campaign_tables_and_gauntlet_campaign_fk(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0052")

    before_tables = _table_names(sqlite_db)
    before_gauntlet_columns = _column_names(sqlite_db, "gauntlets")
    before_gauntlet_indexes = _index_names(sqlite_db, "gauntlets")
    assert CAMPAIGN_TABLE not in before_tables
    assert PAIRING_TABLE not in before_tables
    assert "campaign_id" not in before_gauntlet_columns
    assert GAUNTLET_CAMPAIGN_INDEX not in before_gauntlet_indexes

    gauntlet_id = _insert_existing_gauntlet(sqlite_db)

    command.upgrade(cfg, "head")

    assert _table_names(sqlite_db) - before_tables == {CAMPAIGN_TABLE, PAIRING_TABLE}
    assert _column_names(sqlite_db, CAMPAIGN_TABLE) == CAMPAIGN_COLUMNS
    assert _column_names(sqlite_db, PAIRING_TABLE) == PAIRING_COLUMNS
    assert _column_names(sqlite_db, "gauntlets") - before_gauntlet_columns == {"campaign_id"}
    assert _index_names(sqlite_db, "gauntlets") - before_gauntlet_indexes == {
        GAUNTLET_CAMPAIGN_INDEX
    }
    assert _index_columns(sqlite_db, GAUNTLET_CAMPAIGN_INDEX) == ("campaign_id",)

    campaign_info = _column_info(sqlite_db, CAMPAIGN_TABLE)
    assert campaign_info["leased_by"]["notnull"] == 0
    assert campaign_info["lease_expires_at"]["notnull"] == 0
    assert campaign_info["heartbeat_at"]["notnull"] == 0
    assert campaign_info["completed_at"]["notnull"] == 0
    assert campaign_info["sigma_target"]["notnull"] == 1
    assert campaign_info["rank_stability_k"]["notnull"] == 1

    pairing_info = _column_info(sqlite_db, PAIRING_TABLE)
    assert pairing_info["last_error"]["notnull"] == 0
    assert pairing_info["gauntlet_id"]["notnull"] == 0
    assert pairing_info["attempt_count"]["notnull"] == 1

    assert PAIRING_CELL_UNIQUE_COLUMNS in _unique_column_sets(sqlite_db, PAIRING_TABLE)
    assert ("campaign_id", "campaigns", "id") in _foreign_keys(sqlite_db, PAIRING_TABLE)
    assert ("gauntlet_id", "gauntlets", "id") in _foreign_keys(sqlite_db, PAIRING_TABLE)
    assert ("campaign_id", "campaigns", "id") in _foreign_keys(sqlite_db, "gauntlets")

    with sqlite3.connect(sqlite_db) as conn:
        row = conn.execute(
            "SELECT campaign_id FROM gauntlets WHERE id = ?",
            (gauntlet_id,),
        ).fetchone()
    assert row == (None,)

    command.downgrade(cfg, "0052")

    assert _table_names(sqlite_db) == before_tables
    assert _column_names(sqlite_db, "gauntlets") == before_gauntlet_columns
    assert _index_names(sqlite_db, "gauntlets") == before_gauntlet_indexes


def test_campaign_pairings_unique_constraint_rejects_duplicate_cell(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    campaign_id = _insert_campaign(sqlite_db)
    roster_json = json.dumps(["agent-a", "agent-b", "agent-c"])

    with sqlite3.connect(sqlite_db) as conn:
        conn.execute(
            """
            INSERT INTO campaign_pairings (
                id,
                campaign_id,
                cell_index,
                roster_json,
                status,
                attempt_count,
                last_error,
                gauntlet_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (uuid.uuid4().hex, campaign_id, 0, roster_json, "PENDING", 0, None, None),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO campaign_pairings (
                    id,
                    campaign_id,
                    cell_index,
                    roster_json,
                    status,
                    attempt_count,
                    last_error,
                    gauntlet_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (uuid.uuid4().hex, campaign_id, 0, roster_json, "PENDING", 0, None, None),
            )


def test_campaign_model_metadata_matches_0053_migration(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    campaign_table = cast(Table, Campaign.__table__)
    campaign_types = {
        "campaign_seed": String,
        "ruleset_id": String,
        "league_id": Uuid,
        "format": String,
        "player_count": Integer,
        "per_model_game_target": Integer,
        "status": String,
        "leased_by": String,
        "lease_expires_at": DateTime,
        "heartbeat_at": DateTime,
        "created_at": DateTime,
        "completed_at": DateTime,
        "sigma_target": Numeric,
        "rank_stability_k": Integer,
    }
    for column_name, expected_type in campaign_types.items():
        assert isinstance(campaign_table.columns[column_name].type, expected_type)

    assert campaign_table.columns["leased_by"].nullable is True
    assert campaign_table.columns["lease_expires_at"].nullable is True
    assert campaign_table.columns["heartbeat_at"].nullable is True
    assert campaign_table.columns["completed_at"].nullable is True

    pairing_table = cast(Table, CampaignPairing.__table__)
    pairing_types = {
        "campaign_id": Uuid,
        "cell_index": Integer,
        "roster_json": JSON,
        "status": String,
        "attempt_count": Integer,
        "last_error": Text,
        "gauntlet_id": Uuid,
    }
    for column_name, expected_type in pairing_types.items():
        assert isinstance(pairing_table.columns[column_name].type, expected_type)

    assert pairing_table.columns["last_error"].nullable is True
    assert pairing_table.columns["gauntlet_id"].nullable is True
    assert _model_unique_columns(pairing_table)["uq_campaign_pairing_cell"] == (
        "campaign_id",
        "cell_index",
    )

    gauntlet_table = cast(Table, Gauntlet.__table__)
    assert gauntlet_table.columns["campaign_id"].nullable is True
    assert _model_index_columns(gauntlet_table)[GAUNTLET_CAMPAIGN_INDEX] == ("campaign_id",)
    assert _index_columns(sqlite_db, GAUNTLET_CAMPAIGN_INDEX) == ("campaign_id",)
