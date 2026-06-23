"""US-234a: Migration 0049 allows casual and ranked Humans-Included leagues."""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

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


def test_0049_allows_ranked_and_casual_humans_included_leagues(
    sqlite_db: Path,
) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    with sqlite3.connect(sqlite_db) as conn:
        conn.execute(
            "INSERT INTO leagues (id, name, ruleset_id, ranked, kind, created_at) "
            "VALUES (?, 'casual', 'mini7_v1', 0, 'HUMANS_INCLUDED', "
            "'2026-06-22 00:00:00')",
            (uuid.uuid4().hex,),
        )
        conn.execute(
            "INSERT INTO leagues (id, name, ruleset_id, ranked, kind, created_at) "
            "VALUES (?, 'ranked', 'mini7_v1', 1, 'HUMANS_INCLUDED', "
            "'2026-06-22 00:00:00')",
            (uuid.uuid4().hex,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO leagues (id, name, ruleset_id, ranked, kind, created_at) "
                "VALUES (?, 'ranked-dup', 'mini7_v1', 1, 'HUMANS_INCLUDED', "
                "'2026-06-22 00:00:00')",
                (uuid.uuid4().hex,),
            )
        conn.execute(
            "INSERT INTO leagues (id, name, ruleset_id, ranked, kind, created_at) "
            "VALUES (?, 'scientific-a', 'mini7_v1', 1, 'SCIENTIFIC', "
            "'2026-06-22 00:00:00')",
            (uuid.uuid4().hex,),
        )
        conn.execute(
            "INSERT INTO leagues (id, name, ruleset_id, ranked, kind, created_at) "
            "VALUES (?, 'scientific-b', 'mini7_v1', 1, 'SCIENTIFIC', "
            "'2026-06-22 00:00:00')",
            (uuid.uuid4().hex,),
        )

        count = conn.execute(
            "SELECT COUNT(*) FROM leagues WHERE kind = 'HUMANS_INCLUDED' "
            "AND ruleset_id = 'mini7_v1'"
        ).fetchone()

    assert count == (2,)
