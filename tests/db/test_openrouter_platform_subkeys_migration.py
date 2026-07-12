"""US-359 migration coverage for encrypted OpenRouter platform subkeys."""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Boolean, Integer, LargeBinary, String, Table

from padrino.db.models import OpenRouterPlatformSubkey

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
_TABLE = "openrouter_platform_subkeys"
_COLUMNS = {
    "id",
    "principal_id",
    "ciphertext",
    "nonce",
    "key_version",
    "upstream_key_hash",
    "key_name",
    "display_suffix",
    "label",
    "limit_micros",
    "expires_at",
    "allowed_models",
    "allowlist_enforced_upstream",
    "request_hash",
    "status",
    "provisioned_at",
    "disabled_at",
    "revoked_at",
}


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


def _columns(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({_TABLE})")}


def _indexes(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {str(row[1]) for row in connection.execute(f"PRAGMA index_list({_TABLE})")}


def test_0073_follows_0072_and_round_trips_the_secret_safe_subkey_table(sqlite_db: Path) -> None:
    config = _config()
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["0074"]
    assert script.get_revision("0073").down_revision == "0072"
    command.upgrade(config, "0072")
    with sqlite3.connect(sqlite_db) as connection:
        before = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}
    assert _TABLE not in before

    command.upgrade(config, "0073")
    assert _columns(sqlite_db) == _COLUMNS
    assert {
        "uq_openrouter_platform_subkeys_active_principal",
        "ix_openrouter_platform_subkeys_principal_status",
    }.issubset(_indexes(sqlite_db))

    command.downgrade(config, "0072")
    with sqlite3.connect(sqlite_db) as connection:
        after = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}
    assert _TABLE not in after


def test_platform_subkey_orm_has_no_plaintext_or_management_credential_column() -> None:
    table = cast(Table, OpenRouterPlatformSubkey.__table__)
    assert isinstance(table.columns["ciphertext"].type, LargeBinary)
    assert isinstance(table.columns["nonce"].type, LargeBinary)
    assert isinstance(table.columns["key_version"].type, String)
    assert isinstance(table.columns["upstream_key_hash"].type, String)
    assert isinstance(table.columns["limit_micros"].type, Integer)
    assert isinstance(table.columns["allowlist_enforced_upstream"].type, Boolean)
    assert {"api_key", "plaintext_key", "management_key", "management_key_ref"}.isdisjoint(
        table.columns
    )

    row = OpenRouterPlatformSubkey(
        principal_id=uuid.uuid4(),
        ciphertext=b"sentinel-subkey-plaintext-must-not-render",
        nonce=b"sentinel-nonce-must-not-render",
        key_version="v1",
        upstream_key_hash="a" * 64,
        key_name="padrino-promo-safe",
        display_suffix="1234",
        label="safe",
        limit_micros=5_000_000,
        expires_at=datetime.now(UTC),
        allowed_models=["openai/gpt-5-mini"],
        allowlist_enforced_upstream=False,
        request_hash="b" * 64,
        status="ACTIVE",
        provisioned_at=datetime.now(UTC),
    )
    assert "sentinel-subkey-plaintext-must-not-render" not in repr(row)
    assert "sentinel-nonce-must-not-render" not in repr(row)
