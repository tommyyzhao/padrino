"""US-244: migration 0051 adds lobby integrity acknowledgement additively."""

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

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


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


def _lobby_column_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("PRAGMA table_info(lobbies)").fetchall()
    return {str(r[1]) for r in rows}


def _insert_existing_lobby(db_path: Path) -> str:
    principal_id = uuid.uuid4().hex
    league_id = uuid.uuid4().hex
    lobby_id = uuid.uuid4().hex
    now = "2026-06-22 00:00:00"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO principals (id, kind, display_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (principal_id, "guest", "Guest", now, now),
        )
        conn.execute(
            """
            INSERT INTO leagues (id, name, ruleset_id, ranked, created_at, kind)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (league_id, "Humans Included", "mini7_v1", 0, now, "HUMANS_INCLUDED"),
        )
        conn.execute(
            """
            INSERT INTO lobbies (
                id,
                ruleset_id,
                identity_mode,
                theme_pack_id,
                stakes,
                status,
                lobby_seed,
                host_principal_id,
                league_id,
                game_id,
                created_at,
                updated_at,
                invite_token
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lobby_id,
                "mini7_v1",
                "ANONYMOUS",
                None,
                "CASUAL",
                "OPEN",
                "seed",
                principal_id,
                league_id,
                None,
                now,
                now,
                "invite-token",
            ),
        )
    return lobby_id


def test_0051_is_linear_after_0050() -> None:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)

    assert script.get_revision("0051").down_revision == "0050"


def test_0051_adds_integrity_ack_to_existing_0050_lobbies(sqlite_db: Path) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0050")
    assert "integrity_acknowledged" not in _lobby_column_names(sqlite_db)

    lobby_id = _insert_existing_lobby(sqlite_db)

    command.upgrade(cfg, "head")

    assert "integrity_acknowledged" in _lobby_column_names(sqlite_db)
    with sqlite3.connect(sqlite_db) as conn:
        row = conn.execute(
            "SELECT integrity_acknowledged FROM lobbies WHERE id = ?",
            (lobby_id,),
        ).fetchone()
    assert row == (0,)

    command.downgrade(cfg, "0050")

    assert "integrity_acknowledged" not in _lobby_column_names(sqlite_db)
