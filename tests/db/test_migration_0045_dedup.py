"""US-199: Migration 0045 dedups duplicate dormant HUMANS_INCLUDED leagues.

The pre-0045 ``get_or_create_humans_included`` had no DB constraint, so a deployed
DB can already contain duplicate ``HUMANS_INCLUDED`` leagues for the same
``ruleset_id``. ``CREATE UNIQUE INDEX`` on such a DB would abort the upgrade.
0045 must first collapse duplicates to one keeper per ruleset (earliest
``(created_at, id)``), repoint dependents, then create the index — so the upgrade
succeeds even on a dirty DB.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sync_create_engine
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def sqlite_db_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "padrino_test.db"
        url = f"sqlite+aiosqlite:///{db_path}"
        monkeypatch.setenv("PADRINO_DB_URL", url)
        yield url


def _alembic_config() -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "src/padrino/db/migrations"))
    return cfg


def _sync_url(url: str) -> str:
    return url.replace("sqlite+aiosqlite://", "sqlite://", 1)


def _seed_duplicate_humans_leagues(url: str) -> dict[str, object]:
    """Insert two HUMANS_INCLUDED leagues for the same ruleset plus a dependent.

    Returns the ids needed to assert keeper/loser behaviour after the upgrade.
    """
    engine = sync_create_engine(_sync_url(url))
    # SQLite stores sa.Uuid as 32-char hex, so bind/compare on .hex.
    keeper_id = uuid.uuid4().hex
    loser_id = uuid.uuid4().hex
    other_ruleset_id = uuid.uuid4().hex
    gauntlet_id = uuid.uuid4().hex
    prompt_version_id = uuid.uuid4().hex
    try:
        with engine.begin() as conn:
            # A prompt_version is needed for the gauntlet FK chain.
            conn.execute(
                text(
                    "INSERT INTO prompt_versions "
                    "(id, ruleset_id, version, system_prompt, developer_prompt, "
                    "response_schema, prompt_hash, created_at) "
                    "VALUES (:id, 'mini7_v1', 'v1', 'sys', 'dev', '{}', :hash, "
                    "'2026-01-01 00:00:00')"
                ),
                {"id": prompt_version_id, "hash": uuid.uuid4().hex[:16]},
            )
            # keeper has the earlier created_at -> should survive.
            conn.execute(
                text(
                    "INSERT INTO leagues (id, name, ruleset_id, ranked, kind, created_at) "
                    "VALUES (:id, 'k', 'mini7_v1', 0, 'HUMANS_INCLUDED', '2026-01-01 00:00:00')"
                ),
                {"id": keeper_id},
            )
            conn.execute(
                text(
                    "INSERT INTO leagues (id, name, ruleset_id, ranked, kind, created_at) "
                    "VALUES (:id, 'l', 'mini7_v1', 0, 'HUMANS_INCLUDED', '2026-02-01 00:00:00')"
                ),
                {"id": loser_id},
            )
            # A distinct ruleset with a single league must be untouched.
            conn.execute(
                text(
                    "INSERT INTO leagues (id, name, ruleset_id, ranked, kind, created_at) "
                    "VALUES (:id, 'o', 'bench10_v1', 0, 'HUMANS_INCLUDED', '2026-01-01 00:00:00')"
                ),
                {"id": other_ruleset_id},
            )
            # A dependent row (gauntlet) pointing at the LOSER must be repointed.
            conn.execute(
                text(
                    "INSERT INTO gauntlets "
                    "(id, league_id, ruleset_id, prompt_version_id, clone_count, "
                    "gauntlet_seed, ranked, status, created_at) "
                    "VALUES (:id, :league_id, 'mini7_v1', :pv, 1, 'seed', 0, "
                    "'PENDING', '2026-03-01 00:00:00')"
                ),
                {"id": gauntlet_id, "league_id": loser_id, "pv": prompt_version_id},
            )
    finally:
        engine.dispose()
    return {
        "keeper_id": keeper_id,
        "loser_id": loser_id,
        "other_ruleset_id": other_ruleset_id,
        "gauntlet_id": gauntlet_id,
    }


def test_0045_dedups_duplicate_humans_leagues(sqlite_db_url: str) -> None:
    cfg = _alembic_config()
    # Bring the DB to the pre-0045 state (0044), then seed duplicates.
    command.upgrade(cfg, "0044")
    seeded = _seed_duplicate_humans_leagues(sqlite_db_url)

    # The whole point: upgrade to 0045 must NOT raise on a DB with duplicates.
    command.upgrade(cfg, "0045")

    engine = sync_create_engine(_sync_url(sqlite_db_url))
    try:
        with engine.connect() as conn:
            # Exactly one HUMANS_INCLUDED league per ruleset_id.
            per_ruleset = conn.execute(
                text(
                    "SELECT ruleset_id, COUNT(*) FROM leagues "
                    "WHERE kind = 'HUMANS_INCLUDED' GROUP BY ruleset_id"
                )
            ).all()
            assert {r[0]: r[1] for r in per_ruleset} == {"mini7_v1": 1, "bench10_v1": 1}

            # The keeper (earliest created_at) survived; the loser is gone.
            kept = conn.execute(
                text("SELECT id FROM leagues WHERE ruleset_id = 'mini7_v1'")
            ).scalar_one()
            assert str(kept) == str(seeded["keeper_id"])

            # The dependent gauntlet was repointed from loser -> keeper.
            gl = conn.execute(
                text("SELECT league_id FROM gauntlets WHERE id = :id"),
                {"id": seeded["gauntlet_id"]},
            ).scalar_one()
            assert str(gl) == str(seeded["keeper_id"])

            # The unique index exists.
            idx = conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'index' "
                    "AND name = 'uq_league_humans_included_ruleset'"
                )
            ).scalar_one_or_none()
            assert idx == "uq_league_humans_included_ruleset"
    finally:
        engine.dispose()


def test_0045_clean_db_upgrades_normally(sqlite_db_url: str) -> None:
    """No duplicates -> upgrade is a plain index creation (regression guard)."""
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    engine = sync_create_engine(_sync_url(sqlite_db_url))
    try:
        with engine.connect() as conn:
            idx = conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'index' "
                    "AND name = 'uq_league_humans_included_ruleset'"
                )
            ).scalar_one_or_none()
            assert idx == "uq_league_humans_included_ruleset"
    finally:
        engine.dispose()
