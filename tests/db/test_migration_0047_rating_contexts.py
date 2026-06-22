"""US-171: migration 0047 adds additive rating-context metadata only."""

from __future__ import annotations

import json
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sync_create_engine
from sqlalchemy import inspect, text

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


CANONICAL_FIXTURE_RULESETS: tuple[tuple[str, str], ...] = (
    ("mini7_v1", "TOWN"),
    ("bench10_v1", "MAFIA"),
    ("roleblock10_v1", "TOWN"),
)


def _seed_pre_0047_ratings(url: str) -> dict[str, object]:
    engine = sync_create_engine(_sync_url(url))
    provider_id = uuid.uuid4().hex
    model_config_id = uuid.uuid4().hex
    prompt_version_id = uuid.uuid4().hex
    agent_build_id = uuid.uuid4().hex
    seeded: dict[str, object] = {"ratings": {}, "events": {}}

    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO model_providers "
                    "(id, name, base_url, auth_secret_ref, created_at) "
                    "VALUES (:id, 'provider', NULL, 'secret', '2026-01-01 00:00:00')"
                ),
                {"id": provider_id},
            )
            conn.execute(
                text(
                    "INSERT INTO model_configs "
                    "(id, provider_id, model_name, model_version, default_temperature, "
                    "default_top_p, default_max_output_tokens, supports_structured_outputs, "
                    "created_at) "
                    "VALUES (:id, :provider_id, 'model', NULL, 0.7, 1.0, 256, 1, "
                    "'2026-01-01 00:00:00')"
                ),
                {"id": model_config_id, "provider_id": provider_id},
            )
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
            conn.execute(
                text(
                    "INSERT INTO agent_builds "
                    "(id, display_name, model_config_id, prompt_version_id, adapter_version, "
                    "inference_params, active, created_at) "
                    "VALUES (:id, 'agent', :model_config_id, :prompt_version_id, 'adapter', "
                    "'{}', 1, '2026-01-01 00:00:00')"
                ),
                {
                    "id": agent_build_id,
                    "model_config_id": model_config_id,
                    "prompt_version_id": prompt_version_id,
                },
            )

            for ruleset_id, winner in CANONICAL_FIXTURE_RULESETS:
                league_id = uuid.uuid4().hex
                game_id = uuid.uuid4().hex
                global_rating_id = uuid.uuid4().hex
                faction_rating_id = uuid.uuid4().hex
                global_event_id = uuid.uuid4().hex
                faction_event_id = uuid.uuid4().hex
                conn.execute(
                    text(
                        "INSERT INTO leagues (id, name, ruleset_id, ranked, kind, created_at) "
                        "VALUES (:id, :name, :ruleset_id, 1, 'SCIENTIFIC', "
                        "'2026-01-01 00:00:00')"
                    ),
                    {"id": league_id, "name": f"{ruleset_id}-league", "ruleset_id": ruleset_id},
                )
                conn.execute(
                    text(
                        "INSERT INTO games "
                        "(id, gauntlet_id, ruleset_id, game_seed, status, terminal_result, "
                        "created_at) "
                        "VALUES (:id, NULL, :ruleset_id, :seed, 'COMPLETED', :terminal, "
                        "'2026-01-01 00:00:00')"
                    ),
                    {
                        "id": game_id,
                        "ruleset_id": ruleset_id,
                        "seed": f"{ruleset_id}-seed",
                        "terminal": json.dumps({"winner": winner}),
                    },
                )
                conn.execute(
                    text(
                        "INSERT INTO ratings "
                        "(id, league_id, agent_build_id, scope_type, scope_value, mu, sigma, "
                        "conservative_score, games, updated_at) "
                        "VALUES "
                        "(:global_id, :league_id, :agent_build_id, 'GLOBAL', 'global', "
                        "25.5, 7.5, 3.0, 4, '2026-01-02 00:00:00'), "
                        "(:faction_id, :league_id, :agent_build_id, 'FACTION', :faction, "
                        "26.5, 6.5, 7.0, 5, '2026-01-03 00:00:00')"
                    ),
                    {
                        "global_id": global_rating_id,
                        "faction_id": faction_rating_id,
                        "league_id": league_id,
                        "agent_build_id": agent_build_id,
                        "faction": winner,
                    },
                )
                conn.execute(
                    text(
                        "INSERT INTO rating_events "
                        "(id, league_id, game_id, agent_build_id, public_player_id, "
                        "scope_type, scope_value, before_mu, before_sigma, after_mu, "
                        "after_sigma, created_at) "
                        "VALUES "
                        "(:global_id, :league_id, :game_id, :agent_build_id, 'P01', "
                        "'GLOBAL', 'global', 25.0, 8.0, 25.5, 7.5, "
                        "'2026-01-02 00:00:00'), "
                        "(:faction_id, :league_id, :game_id, :agent_build_id, 'P01', "
                        "'FACTION', :faction, 25.5, 7.5, 26.5, 6.5, "
                        "'2026-01-03 00:00:00')"
                    ),
                    {
                        "global_id": global_event_id,
                        "faction_id": faction_event_id,
                        "league_id": league_id,
                        "game_id": game_id,
                        "agent_build_id": agent_build_id,
                        "faction": winner,
                    },
                )
                ratings = seeded["ratings"]
                events = seeded["events"]
                assert isinstance(ratings, dict)
                assert isinstance(events, dict)
                ratings[global_rating_id] = (ruleset_id, 25.5, 7.5, 3.0, 4)
                ratings[faction_rating_id] = (ruleset_id, 26.5, 6.5, 7.0, 5)
                events[global_event_id] = (ruleset_id, f"{ruleset_id}-seed", winner)
                events[faction_event_id] = (ruleset_id, f"{ruleset_id}-seed", winner)
    finally:
        engine.dispose()
    return seeded


def _rating_numeric_bytes(url: str) -> list[tuple[str, str, str, str]]:
    engine = sync_create_engine(_sync_url(url))
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, CAST(mu AS TEXT) AS mu, CAST(sigma AS TEXT) AS sigma, "
                    "CAST(conservative_score AS TEXT) AS conservative_score "
                    "FROM ratings ORDER BY id"
                )
            ).all()
            return [
                (
                    str(row.id),
                    str(row.mu),
                    str(row.sigma),
                    str(row.conservative_score),
                )
                for row in rows
            ]
    finally:
        engine.dispose()


def test_0047_stamps_existing_scientific_ratings_without_rerating(
    sqlite_db_url: str,
) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0046")
    seeded = _seed_pre_0047_ratings(sqlite_db_url)

    command.upgrade(cfg, "0047")

    engine = sync_create_engine(_sync_url(sqlite_db_url))
    try:
        with engine.connect() as conn:
            contexts = conn.execute(
                text(
                    "SELECT id, ruleset_id, kind, is_canonical FROM rating_contexts "
                    "WHERE kind = 'CANONICAL_TEAM'"
                )
            ).all()
            context_by_ruleset = {row.ruleset_id: row for row in contexts}
            expected_rulesets = {ruleset_id for ruleset_id, _winner in CANONICAL_FIXTURE_RULESETS}
            assert set(context_by_ruleset) >= expected_rulesets
            assert all(bool(row.is_canonical) for row in context_by_ruleset.values())

            rating_rows = conn.execute(
                text(
                    "SELECT id, ruleset_id, rating_context_id, mu, sigma, "
                    "conservative_score, games FROM ratings"
                )
            ).all()
            seeded_ratings = seeded["ratings"]
            assert isinstance(seeded_ratings, dict)
            assert len(rating_rows) == len(seeded_ratings)
            for row in rating_rows:
                ruleset_id, mu, sigma, conservative_score, games = seeded_ratings[str(row.id)]
                assert row.ruleset_id == ruleset_id
                assert str(row.rating_context_id) == str(context_by_ruleset[ruleset_id].id)
                assert row.mu == pytest.approx(mu)
                assert row.sigma == pytest.approx(sigma)
                assert row.conservative_score == pytest.approx(conservative_score)
                assert row.games == games

            event_rows = conn.execute(
                text(
                    "SELECT id, ruleset_id, rating_context_id, game_seed, team_outcome "
                    "FROM rating_events"
                )
            ).all()
            seeded_events = seeded["events"]
            assert isinstance(seeded_events, dict)
            assert len(event_rows) == len(seeded_events)
            for row in event_rows:
                ruleset_id, game_seed, team_outcome = seeded_events[str(row.id)]
                assert row.ruleset_id == ruleset_id
                assert str(row.rating_context_id) == str(context_by_ruleset[ruleset_id].id)
                assert row.game_seed == game_seed
                assert row.team_outcome == team_outcome
    finally:
        engine.dispose()


def test_0047_downgrade_removes_additive_schema_and_preserves_rating_bytes(
    sqlite_db_url: str,
) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "0046")
    _seed_pre_0047_ratings(sqlite_db_url)
    before = _rating_numeric_bytes(sqlite_db_url)

    command.upgrade(cfg, "0047")
    command.downgrade(cfg, "0046")

    engine = sync_create_engine(_sync_url(sqlite_db_url))
    try:
        with engine.connect() as conn:
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
            assert tables.isdisjoint(
                {
                    "rating_contexts",
                    "placement_ratings",
                    "placement_rating_events",
                    "solo_rate_ratings",
                    "solo_rate_rating_events",
                }
            )
            assert {"ruleset_id", "rating_context_id"}.isdisjoint(
                {column["name"] for column in inspector.get_columns("ratings")}
            )
            assert {
                "ruleset_id",
                "rating_context_id",
                "game_seed",
                "team_outcome",
            }.isdisjoint({column["name"] for column in inspector.get_columns("rating_events")})
    finally:
        engine.dispose()

    assert _rating_numeric_bytes(sqlite_db_url) == before
