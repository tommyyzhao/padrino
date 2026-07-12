"""US-342: migration 0067 reserves immutable OpenRouter catalog tables."""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from padrino.db.models import CatalogSnapshot, ModelCatalogEntry, ModelProbeEvidence

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

CATALOG_SNAPSHOT_COLUMNS = {
    "id",
    "content_hash",
    "fetched_at",
    "source_etag",
    "source_version",
    "status",
    "completed_at",
    "created_at",
}
CATALOG_ENTRY_COLUMNS = {
    "id",
    "catalog_snapshot_id",
    "canonical_slug",
    "display_name",
    "description",
    "modalities",
    "context_length",
    "pricing_strings",
    "pricing_micros",
    "supported_parameters",
    "provider_summary",
    "expires_at",
    "raw_metadata",
    "created_at",
}
PROBE_EVIDENCE_COLUMNS = {
    "id",
    "catalog_snapshot_id",
    "model_catalog_entry_id",
    "prompt_hash",
    "schema_hash",
    "probe_suite_version",
    "status",
    "evidence",
    "observed_at",
    "created_at",
}
FORBIDDEN_COLUMNS = {
    "api_key",
    "authorization",
    "authorization_header",
    "account_credit",
    "player_id",
    "principal_id",
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


def _index_names(db_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA index_list({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def test_0067_follows_0066_linearly() -> None:
    script = ScriptDirectory.from_config(_alembic_config())

    assert script.get_heads() == ["0071"]
    assert script.get_revision("0068").down_revision == "0067"
    assert script.get_revision("0067").down_revision == "0066"
    assert script.get_revision("0066").down_revision == "0065"


def test_0067_adds_catalog_and_probe_evidence_schema_with_browsing_indexes(
    sqlite_db: Path,
) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0066")
    assert _column_names(sqlite_db, "model_catalog_snapshots") == set()

    command.upgrade(cfg, "0067")

    assert _column_names(sqlite_db, "model_catalog_snapshots") == CATALOG_SNAPSHOT_COLUMNS
    assert _column_names(sqlite_db, "model_catalog_entries") == CATALOG_ENTRY_COLUMNS
    assert _column_names(sqlite_db, "model_probe_evidence") == PROBE_EVIDENCE_COLUMNS
    assert FORBIDDEN_COLUMNS.isdisjoint(_column_names(sqlite_db, "model_catalog_snapshots"))
    assert FORBIDDEN_COLUMNS.isdisjoint(_column_names(sqlite_db, "model_catalog_entries"))
    assert FORBIDDEN_COLUMNS.isdisjoint(_column_names(sqlite_db, "model_probe_evidence"))
    assert {
        "ix_model_catalog_entries_canonical_slug",
        "ix_model_catalog_entries_snapshot_expires_slug",
    }.issubset(_index_names(sqlite_db, "model_catalog_entries"))
    assert "ix_catalog_snapshots_status_fetched_at" in _index_names(
        sqlite_db, "model_catalog_snapshots"
    )

    command.downgrade(cfg, "0066")

    assert _column_names(sqlite_db, "model_catalog_snapshots") == set()


def test_catalog_models_match_migration_shape() -> None:
    assert CatalogSnapshot.__tablename__ == "model_catalog_snapshots"
    assert ModelCatalogEntry.__tablename__ == "model_catalog_entries"
    assert ModelProbeEvidence.__tablename__ == "model_probe_evidence"
    assert CatalogSnapshot.__table__.c["content_hash"].unique is True
    assert ModelCatalogEntry.__table__.c["pricing_strings"].type.python_type is dict
    assert ModelCatalogEntry.__table__.c["pricing_micros"].type.python_type is dict
    assert ModelProbeEvidence.__table__.c["evidence"].type.python_type is dict


def test_0067_database_enforces_completed_snapshot_immutability_and_append_only_evidence(
    sqlite_db: Path,
) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0067")

    snapshot_id = uuid.uuid4().hex
    entry_id = uuid.uuid4().hex
    evidence_id = uuid.uuid4().hex
    now = "2026-07-11 18:30:00"
    with sqlite3.connect(sqlite_db) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO model_catalog_snapshots "
            "(id, content_hash, fetched_at, source_etag, source_version, status, completed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (snapshot_id, "a" * 64, now, None, None, "PENDING", None, now),
        )
        conn.execute(
            "INSERT INTO model_catalog_entries "
            "(id, catalog_snapshot_id, canonical_slug, display_name, description, modalities, "
            "context_length, pricing_strings, pricing_micros, supported_parameters, "
            "provider_summary, expires_at, raw_metadata, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry_id,
                snapshot_id,
                "openai/gpt-5-mini",
                "OpenAI: GPT-5 Mini",
                None,
                '["text"]',
                128_000,
                '{"prompt":"0.00000025"}',
                '{"prompt_per_million":250000}',
                '["temperature"]',
                "{}",
                None,
                "{}",
                now,
            ),
        )
        conn.execute(
            "UPDATE model_catalog_snapshots SET status=?, completed_at=? WHERE id=?",
            ("COMPLETE", now, snapshot_id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="catalog_snapshot_immutable"):
            conn.execute(
                "UPDATE model_catalog_snapshots SET source_version=? WHERE id=?",
                ("mutated", snapshot_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="catalog_entry_immutable"):
            conn.execute(
                "UPDATE model_catalog_entries SET display_name=? WHERE id=?",
                ("mutated", entry_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="catalog_entry_immutable"):
            conn.execute(
                "INSERT INTO model_catalog_entries "
                "(id, catalog_snapshot_id, canonical_slug, display_name, description, modalities, "
                "context_length, pricing_strings, pricing_micros, supported_parameters, "
                "provider_summary, expires_at, raw_metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    snapshot_id,
                    "anthropic/claude-4",
                    "Anthropic: Claude 4",
                    None,
                    '["text"]',
                    128_000,
                    '{"prompt":"0.000001"}',
                    '{"prompt_per_million":1000000}',
                    "[]",
                    "{}",
                    None,
                    "{}",
                    now,
                ),
            )
        conn.execute(
            "INSERT INTO model_probe_evidence "
            "(id, catalog_snapshot_id, model_catalog_entry_id, prompt_hash, schema_hash, "
            "probe_suite_version, status, evidence, observed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                evidence_id,
                snapshot_id,
                entry_id,
                "b" * 64,
                "c" * 64,
                "structured-v1",
                "PASSED",
                "{}",
                now,
                now,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="model_probe_evidence_append_only"):
            conn.execute(
                "UPDATE model_probe_evidence SET status=? WHERE id=?",
                ("FAILED", evidence_id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO model_probe_evidence "
                "(id, catalog_snapshot_id, model_catalog_entry_id, prompt_hash, schema_hash, "
                "probe_suite_version, status, evidence, observed_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    uuid.uuid4().hex,
                    entry_id,
                    "d" * 64,
                    "c" * 64,
                    "structured-v1",
                    "PASSED",
                    "{}",
                    now,
                    now,
                ),
            )
