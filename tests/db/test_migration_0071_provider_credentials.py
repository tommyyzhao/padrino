"""US-353: encrypted provider-credential and OpenRouter PKCE schema."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import LargeBinary, String, Table, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import Game, GameEvent, OpenRouterPkceFlow, Principal, ProviderCredential
from padrino.db.repositories import human_principals, provider_credentials

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

_CREDENTIAL_TABLE = "provider_credentials"
_FLOW_TABLE = "openrouter_pkce_flows"
_CREDENTIAL_COLUMNS = {
    "id",
    "principal_id",
    "provider",
    "ciphertext",
    "nonce",
    "key_version",
    "display_suffix",
    "label",
    "created_at",
    "updated_at",
    "last_validated_at",
    "revoked_at",
}
_FLOW_COLUMNS = {
    "id",
    "principal_id",
    "state_hash",
    "verifier_binding_hash",
    "expires_at",
    "consumed_at",
    "created_at",
}
_NOW = datetime(2026, 7, 12, 19, 30, tzinfo=UTC)


@pytest.fixture
def sqlite_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "padrino_test.db"
        monkeypatch.setenv("PADRINO_DB_URL", f"sqlite+aiosqlite:///{db_path}")
        yield db_path


def _alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(REPO_ROOT / "src/padrino/db/migrations"))
    return config


def _column_info(db_path: Path, table_name: str) -> dict[str, sqlite3.Row]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]): row for row in rows}


def _index_names(db_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(f"PRAGMA index_list({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _unique_index_columns(db_path: Path, table_name: str) -> set[tuple[str, ...]]:
    with sqlite3.connect(db_path) as connection:
        indexes = connection.execute(f"PRAGMA index_list({table_name})").fetchall()
        return {
            tuple(
                str(row[2])
                for row in connection.execute(f"PRAGMA index_info({index[1]})").fetchall()
            )
            for index in indexes
            if bool(index[2])
        }


def test_0071_follows_0070_and_round_trips_credential_and_pkce_tables(sqlite_db: Path) -> None:
    config = _alembic_config()
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["0071"]
    assert script.get_revision("0071").down_revision == "0070"

    command.upgrade(config, "0070")
    with sqlite3.connect(sqlite_db) as connection:
        before = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {_CREDENTIAL_TABLE, _FLOW_TABLE}.isdisjoint(before)

    command.upgrade(config, "0071")
    credentials = _column_info(sqlite_db, _CREDENTIAL_TABLE)
    flows = _column_info(sqlite_db, _FLOW_TABLE)
    assert set(credentials) == _CREDENTIAL_COLUMNS
    assert set(flows) == _FLOW_COLUMNS
    assert all(row["dflt_value"] is None for row in credentials.values())
    assert all(row["dflt_value"] is None for row in flows.values())
    assert "uq_provider_credentials_active_principal_provider" in _index_names(
        sqlite_db, _CREDENTIAL_TABLE
    )
    assert ("state_hash",) in _unique_index_columns(sqlite_db, _FLOW_TABLE)

    command.downgrade(config, "0070")
    with sqlite3.connect(sqlite_db) as connection:
        after = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {_CREDENTIAL_TABLE, _FLOW_TABLE}.isdisjoint(after)


def test_credential_and_pkce_orms_store_only_secret_safe_columns() -> None:
    credential_table = cast(Table, ProviderCredential.__table__)
    flow_table = cast(Table, OpenRouterPkceFlow.__table__)

    assert isinstance(credential_table.columns["ciphertext"].type, LargeBinary)
    assert isinstance(credential_table.columns["nonce"].type, LargeBinary)
    assert isinstance(credential_table.columns["key_version"].type, String)
    assert credential_table.columns["ciphertext"].nullable is True
    assert credential_table.columns["nonce"].nullable is True
    assert credential_table.columns["revoked_at"].nullable is True
    assert isinstance(flow_table.columns["state_hash"].type, String)
    assert isinstance(flow_table.columns["verifier_binding_hash"].type, String)
    assert flow_table.columns["consumed_at"].nullable is True

    all_columns = set(credential_table.columns) | set(flow_table.columns)
    assert {"api_key", "access_token", "plaintext_key", "code_verifier"}.isdisjoint(all_columns)

    row = ProviderCredential(
        principal_id=uuid.uuid4(),
        provider="openrouter",
        ciphertext=b"sentinel-raw-provider-secret",
        nonce=b"sentinel-nonce",
        key_version="master-v1",
        display_suffix="1234",
        label="Personal OpenRouter",
    )
    assert "sentinel-raw-provider-secret" not in repr(row)
    assert "sentinel-nonce" not in repr(row)


async def test_active_credential_uniqueness_and_gdpr_erase_preserve_game_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        principal = Principal(kind="account")
        game = Game(
            gauntlet_id=None,
            ruleset_id="mini7_v1",
            game_seed="credential-gdpr-game",
            status="RUNNING",
        )
        session.add_all([principal, game])
        await session.flush()
        event = GameEvent(
            game_id=game.id,
            sequence=1,
            event_type="GameCreated",
            phase="SETUP",
            visibility="SYSTEM",
            actor_player_id=None,
            payload={},
            prev_event_hash="0" * 64,
            event_hash="a" * 64,
        )
        session.add(event)
        credential = ProviderCredential(
            principal_id=principal.id,
            provider="openrouter",
            ciphertext=b"encrypted-credential",
            nonce=b"nonce",
            key_version="master-v1",
            display_suffix="1234",
            label="Personal OpenRouter",
        )
        session.add(credential)
        await session.flush()
        principal_id = principal.id
        game_id = game.id

    async with session_factory() as session:
        session.add(
            ProviderCredential(
                principal_id=principal_id,
                provider="openrouter",
                ciphertext=b"second-credential",
                nonce=b"second-nonce",
                key_version="master-v1",
                display_suffix="5678",
                label=None,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async with session_factory() as session, session.begin():
        deleted = await human_principals.anonymize_principal_for_delete(
            session,
            principal_id=principal_id,
            now=_NOW,
        )
        assert deleted is not None

    async with session_factory() as session, session.begin():
        erased = (
            (
                await session.execute(
                    select(ProviderCredential).where(
                        ProviderCredential.principal_id == principal_id
                    )
                )
            )
            .scalars()
            .one()
        )
        assert erased.revoked_at is not None
        assert erased.ciphertext is None
        assert erased.nonce is None
        session.add(
            ProviderCredential(
                principal_id=principal_id,
                provider="openrouter",
                ciphertext=b"replacement-credential",
                nonce=b"replacement-nonce",
                key_version="master-v2",
                display_suffix="9012",
                label=None,
            )
        )

    async with session_factory() as session:
        events = list(
            (
                await session.execute(
                    select(GameEvent)
                    .where(GameEvent.game_id == game_id)
                    .order_by(GameEvent.sequence)
                )
            ).scalars()
        )
        assert [(event.sequence, event.event_hash) for event in events] == [(1, "a" * 64)]

    async with session_factory() as session, session.begin():
        stored_principal = await session.get(Principal, principal_id)
        assert stored_principal is not None
        await session.delete(stored_principal)

    async with session_factory() as session:
        assert (
            await session.execute(
                select(ProviderCredential).where(ProviderCredential.principal_id == principal_id)
            )
        ).scalars().all() == []
        events = list(
            (
                await session.execute(
                    select(GameEvent)
                    .where(GameEvent.game_id == game_id)
                    .order_by(GameEvent.sequence)
                )
            ).scalars()
        )
        assert [(event.sequence, event.event_hash) for event in events] == [(1, "a" * 64)]


async def test_pkce_flow_consumption_is_atomic_under_concurrent_callbacks(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'pkce-flow-race.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session, session.begin():
            principal = Principal(kind="account")
            session.add(principal)
            await session.flush()
            session.add(
                OpenRouterPkceFlow(
                    principal_id=principal.id,
                    state_hash="s" * 64,
                    verifier_binding_hash="v" * 64,
                    expires_at=_NOW + timedelta(minutes=5),
                )
            )

        async with session_factory() as session, session.begin():
            assert (
                await provider_credentials.try_consume_openrouter_pkce_flow(
                    session,
                    state_hash="s" * 64,
                    verifier_binding_hash="w" * 64,
                    consumed_at=_NOW,
                )
                is False
            )

        ready = asyncio.Event()

        async def consume() -> bool:
            await ready.wait()
            async with session_factory() as session, session.begin():
                return await provider_credentials.try_consume_openrouter_pkce_flow(
                    session,
                    state_hash="s" * 64,
                    verifier_binding_hash="v" * 64,
                    consumed_at=_NOW,
                )

        tasks = [asyncio.create_task(consume()) for _ in range(6)]
        ready.set()
        assert sum(await asyncio.gather(*tasks)) == 1

        async with session_factory() as session:
            flow = (
                await session.execute(
                    select(OpenRouterPkceFlow).where(OpenRouterPkceFlow.state_hash == "s" * 64)
                )
            ).scalar_one()
            assert flow.consumed_at is not None
    finally:
        await engine.dispose()
