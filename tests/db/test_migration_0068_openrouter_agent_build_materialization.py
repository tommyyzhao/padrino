"""US-347: migration 0068 catalog-backed AgentBuild identity coverage."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from padrino.db.models import AgentBuild, ModelConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

MODEL_CONFIG_MATERIALIZATION_COLUMNS = {
    "catalog_snapshot_id",
    "catalog_canonical_slug",
    "catalog_identity_hash",
}
AGENT_BUILD_MATERIALIZATION_COLUMNS = {
    "catalog_snapshot_id",
    "qualification_tier",
    "inference_params_hash",
    "materialization_identity_hash",
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


def _column_names(db_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _unique_index_columns(db_path: Path, table_name: str) -> set[tuple[str, ...]]:
    with sqlite3.connect(db_path) as conn:
        indexes = conn.execute(f"PRAGMA index_list({table_name})").fetchall()
        return {
            tuple(
                str(column[2])
                for column in conn.execute(f"PRAGMA index_info({index[1]})").fetchall()
            )
            for index in indexes
            if int(index[2]) == 1
        }


def test_0068_follows_0067_linearly() -> None:
    script = ScriptDirectory.from_config(_alembic_config())

    assert script.get_heads() == ["0072"]
    assert script.get_revision("0068").down_revision == "0067"
    assert script.get_revision("0067").down_revision == "0066"


def test_0068_adds_nullable_identity_columns_and_backfills_only_safe_legacy_hashes(
    sqlite_db: Path,
) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0067")
    provider_id = uuid.uuid4().hex
    config_id = uuid.uuid4().hex
    prompt_id = uuid.uuid4().hex
    build_id = uuid.uuid4().hex
    now = "2026-07-12 15:00:00"
    with sqlite3.connect(sqlite_db) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO model_providers (id, name, base_url, auth_secret_ref, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (provider_id, "scientific", None, "env:SCIENTIFIC", now),
        )
        conn.execute(
            "INSERT INTO model_configs "
            "(id, provider_id, model_name, litellm_model_id, model_version, "
            "default_temperature, default_top_p, default_max_output_tokens, "
            "supports_structured_outputs, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                config_id,
                provider_id,
                "legacy-model",
                "scientific/legacy-model",
                "legacy-v1",
                0.7,
                1.0,
                4096,
                True,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO prompt_versions "
            "(id, ruleset_id, version, system_prompt, developer_prompt, response_schema, "
            "prompt_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                prompt_id,
                "mini7_v1",
                "v1",
                "system",
                "developer",
                "{}",
                "a" * 64,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO agent_builds "
            "(id, display_name, model_config_id, prompt_version_id, adapter_version, "
            "inference_params, active, version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                build_id,
                "scientific/legacy-model",
                config_id,
                prompt_id,
                "legacy-adapter",
                '{"temperature":0.7}',
                True,
                "v1",
                now,
            ),
        )

    command.upgrade(cfg, "0068")

    assert MODEL_CONFIG_MATERIALIZATION_COLUMNS.issubset(_column_names(sqlite_db, "model_configs"))
    assert AGENT_BUILD_MATERIALIZATION_COLUMNS.issubset(_column_names(sqlite_db, "agent_builds"))
    assert ("catalog_identity_hash",) in _unique_index_columns(sqlite_db, "model_configs")
    assert ("materialization_identity_hash",) in _unique_index_columns(sqlite_db, "agent_builds")
    with sqlite3.connect(sqlite_db) as conn:
        model_row = conn.execute(
            "SELECT catalog_snapshot_id, catalog_canonical_slug, catalog_identity_hash "
            "FROM model_configs WHERE id=?",
            (config_id,),
        ).fetchone()
        build_row = conn.execute(
            "SELECT catalog_snapshot_id, qualification_tier, inference_params_hash, "
            "materialization_identity_hash FROM agent_builds WHERE id=?",
            (build_id,),
        ).fetchone()
    assert model_row == (None, None, None)
    assert build_row == (
        None,
        None,
        hashlib.sha256(b'{"temperature":0.7}').hexdigest(),
        None,
    )

    command.downgrade(cfg, "0067")
    assert MODEL_CONFIG_MATERIALIZATION_COLUMNS.isdisjoint(
        _column_names(sqlite_db, "model_configs")
    )
    assert AGENT_BUILD_MATERIALIZATION_COLUMNS.isdisjoint(_column_names(sqlite_db, "agent_builds"))


def test_materialization_orm_columns_match_0068_schema() -> None:
    assert ModelConfig.__table__.c["catalog_snapshot_id"].nullable is True
    assert ModelConfig.__table__.c["catalog_identity_hash"].nullable is True
    assert AgentBuild.__table__.c["catalog_snapshot_id"].nullable is True
    assert AgentBuild.__table__.c["inference_params_hash"].nullable is True
    assert AgentBuild.__table__.c["materialization_identity_hash"].nullable is True
