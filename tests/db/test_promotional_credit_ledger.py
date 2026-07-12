"""US-357: append-only promotional-credit and completion-reserve persistence."""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Integer, Table, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import (
    Game,
    OperatorCompletionReserveLedgerEntry,
    Principal,
    PromotionalCreditLedgerEntry,
    PromotionalCreditReservation,
)
from padrino.db.repositories import promotional_credit_ledger

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
_NOW = datetime(2026, 7, 12, 21, 0, tzinfo=UTC)

_LEDGER_TABLE = "promotional_credit_ledger_entries"
_RESERVATION_TABLE = "promotional_credit_reservations"
_COMPLETION_TABLE = "operator_completion_reserve_ledger_entries"

_LEDGER_COLUMNS = {
    "id",
    "principal_id",
    "reservation_id",
    "game_id",
    "entry_type",
    "amount_micros",
    "idempotency_key",
    "reason",
    "created_at",
}
_RESERVATION_COLUMNS = {
    "id",
    "principal_id",
    "lobby_id",
    "game_id",
    "estimate_hash",
    "catalog_hash",
    "rules_hash",
    "reserved_micros",
    "consumed_micros",
    "released_micros",
    "status",
    "idempotency_key",
    "reserved_at",
    "updated_at",
    "terminal_at",
}
_COMPLETION_COLUMNS = {
    "id",
    "game_id",
    "entry_type",
    "amount_micros",
    "estimate_miss_micros",
    "model_slug",
    "idempotency_key",
    "reason",
    "created_at",
}


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


def _column_names(db_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})")}


def _index_names(db_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {str(row[1]) for row in connection.execute(f"PRAGMA index_list({table_name})")}


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def test_0072_follows_0071_and_round_trips_credit_tables(sqlite_db: Path) -> None:
    config = _alembic_config()
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["0073"]
    assert script.get_revision("0072").down_revision == "0071"

    command.upgrade(config, "0071")
    assert {_LEDGER_TABLE, _RESERVATION_TABLE, _COMPLETION_TABLE}.isdisjoint(
        _table_names(sqlite_db)
    )

    command.upgrade(config, "0072")
    assert _column_names(sqlite_db, _LEDGER_TABLE) == _LEDGER_COLUMNS
    assert _column_names(sqlite_db, _RESERVATION_TABLE) == _RESERVATION_COLUMNS
    assert _column_names(sqlite_db, _COMPLETION_TABLE) == _COMPLETION_COLUMNS
    assert "uq_promotional_credit_ledger_entries_idempotency_key" in _index_names(
        sqlite_db, _LEDGER_TABLE
    )
    assert "uq_promotional_credit_reservations_active_game" in _index_names(
        sqlite_db, _RESERVATION_TABLE
    )
    assert "uq_operator_completion_reserve_ledger_entries_idempotency_key" in _index_names(
        sqlite_db, _COMPLETION_TABLE
    )

    command.downgrade(config, "0071")
    assert {_LEDGER_TABLE, _RESERVATION_TABLE, _COMPLETION_TABLE}.isdisjoint(
        _table_names(sqlite_db)
    )


def test_monetary_orms_are_integer_micro_ledgers_without_a_mutable_balance() -> None:
    ledger_table = cast(Table, PromotionalCreditLedgerEntry.__table__)
    reservation_table = cast(Table, PromotionalCreditReservation.__table__)
    completion_table = cast(Table, OperatorCompletionReserveLedgerEntry.__table__)

    for table, column_names in (
        (ledger_table, ("amount_micros",)),
        (reservation_table, ("reserved_micros", "consumed_micros", "released_micros")),
        (completion_table, ("amount_micros", "estimate_miss_micros")),
    ):
        for column_name in column_names:
            assert isinstance(table.columns[column_name].type, Integer)
        assert "balance" not in table.columns


async def test_database_rejects_invalid_amounts_duplicate_grants_and_active_game_holds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        principal = Principal(kind="account")
        game = Game(
            gauntlet_id=None,
            ruleset_id="mini7_v1",
            game_seed="promo-ledger-constraints",
            status="RUNNING",
        )
        session.add_all([principal, game])
        await session.flush()
        principal_id = principal.id
        game_id = game.id
        session.add(
            PromotionalCreditLedgerEntry(
                principal_id=principal_id,
                reservation_id=None,
                game_id=None,
                entry_type="GRANT",
                amount_micros=5_000_000,
                idempotency_key="grant:constraint-primary",
                reason="verified_account_promotion",
                created_at=_NOW,
            )
        )

    async with session_factory() as session:
        session.add(
            PromotionalCreditLedgerEntry(
                principal_id=principal_id,
                reservation_id=None,
                game_id=None,
                entry_type="GRANT",
                amount_micros=-1,
                idempotency_key="grant:negative",
                reason="invalid",
                created_at=_NOW,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async with session_factory() as session:
        session.add(
            PromotionalCreditLedgerEntry(
                principal_id=principal_id,
                reservation_id=None,
                game_id=None,
                entry_type="GRANT",
                amount_micros=1,
                idempotency_key="grant:duplicate",
                reason="invalid_second_promotion",
                created_at=_NOW,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async with session_factory() as session:
        session.add_all(
            [
                PromotionalCreditReservation(
                    principal_id=principal_id,
                    lobby_id=None,
                    game_id=game_id,
                    estimate_hash="a" * 64,
                    catalog_hash="b" * 64,
                    rules_hash="c" * 64,
                    reserved_micros=100,
                    consumed_micros=0,
                    released_micros=0,
                    status="ACTIVE",
                    idempotency_key="hold:one",
                    reserved_at=_NOW,
                    updated_at=_NOW,
                    terminal_at=None,
                ),
                PromotionalCreditReservation(
                    principal_id=principal_id,
                    lobby_id=None,
                    game_id=game_id,
                    estimate_hash="d" * 64,
                    catalog_hash="e" * 64,
                    rules_hash="f" * 64,
                    reserved_micros=100,
                    consumed_micros=0,
                    released_micros=0,
                    status="ACTIVE",
                    idempotency_key="hold:two",
                    reserved_at=_NOW,
                    updated_at=_NOW,
                    terminal_at=None,
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async with session_factory() as session:
        session.add(
            OperatorCompletionReserveLedgerEntry(
                game_id=game_id,
                entry_type="CHARGE",
                amount_micros=-100,
                estimate_miss_micros=100,
                model_slug=None,
                idempotency_key="completion:missing-model",
                reason="estimate_miss",
                created_at=_NOW,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


async def test_append_only_credit_ledger_derives_balance_and_retries_idempotently(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        principal = Principal(kind="account")
        game = Game(
            gauntlet_id=None,
            ruleset_id="mini7_v1",
            game_seed="promo-ledger-game",
            status="RUNNING",
        )
        session.add_all([principal, game])
        await session.flush()

        grant, created = await promotional_credit_ledger.append_credit_entry(
            session,
            principal_id=principal.id,
            reservation_id=None,
            game_id=None,
            entry_type=promotional_credit_ledger.CreditLedgerEntryType.GRANT,
            amount_micros=5_000_000,
            idempotency_key="grant:verified-account",
            reason="verified_account_promotion",
            now=_NOW,
        )
        duplicate, duplicate_created = await promotional_credit_ledger.append_credit_entry(
            session,
            principal_id=principal.id,
            reservation_id=None,
            game_id=None,
            entry_type=promotional_credit_ledger.CreditLedgerEntryType.GRANT,
            amount_micros=5_000_000,
            idempotency_key="grant:verified-account",
            reason="verified_account_promotion",
            now=_NOW,
        )
        (
            reservation,
            reservation_created,
        ) = await promotional_credit_ledger.create_or_get_credit_reservation(
            session,
            principal_id=principal.id,
            lobby_id=None,
            game_id=game.id,
            estimate_hash="e" * 64,
            catalog_hash="c" * 64,
            rules_hash="f" * 64,
            reserved_micros=1_500,
            idempotency_key="reservation:promo-ledger-game",
            now=_NOW,
        )
        await promotional_credit_ledger.append_credit_entry(
            session,
            principal_id=principal.id,
            reservation_id=reservation.id,
            game_id=game.id,
            entry_type=promotional_credit_ledger.CreditLedgerEntryType.RESERVATION,
            amount_micros=-1_500,
            idempotency_key="hold:promo-ledger-game",
            reason="game_admission",
            now=_NOW,
        )
        await promotional_credit_ledger.append_credit_entry(
            session,
            principal_id=principal.id,
            reservation_id=reservation.id,
            game_id=game.id,
            entry_type=promotional_credit_ledger.CreditLedgerEntryType.CHARGE,
            amount_micros=-1_000,
            idempotency_key="charge:call-1",
            reason="openrouter_call",
            now=_NOW,
        )
        await promotional_credit_ledger.append_credit_entry(
            session,
            principal_id=principal.id,
            reservation_id=reservation.id,
            game_id=game.id,
            entry_type=promotional_credit_ledger.CreditLedgerEntryType.RELEASE,
            amount_micros=1_000,
            idempotency_key="release:consumed-1",
            reason="reservation_consumed",
            now=_NOW,
        )
        await promotional_credit_ledger.append_credit_entry(
            session,
            principal_id=principal.id,
            reservation_id=reservation.id,
            game_id=game.id,
            entry_type=promotional_credit_ledger.CreditLedgerEntryType.RELEASE,
            amount_micros=500,
            idempotency_key="release:unused",
            reason="terminal_unused_reservation",
            now=_NOW,
        )

        reservation.consumed_micros = 1_000
        reservation.released_micros = 500
        reservation.status = "SETTLED"
        reservation.terminal_at = _NOW
        reservation.updated_at = _NOW
        await session.flush()

        totals = await promotional_credit_ledger.get_credit_totals(
            session, principal_id=principal.id
        )

    assert created is True
    assert duplicate_created is False
    assert duplicate.id == grant.id
    assert reservation_created is True
    assert totals.granted_micros == 5_000_000
    assert totals.spent_micros == 1_000
    assert totals.reserved_micros == 0
    assert totals.available_micros == 4_999_000
    assert totals.available_micros == sum((5_000_000, -1_500, -1_000, 1_000, 500))

    async with session_factory() as session, session.begin():
        with pytest.raises(promotional_credit_ledger.LedgerIdempotencyConflict):
            await promotional_credit_ledger.append_credit_entry(
                session,
                principal_id=principal.id,
                reservation_id=None,
                game_id=None,
                entry_type=promotional_credit_ledger.CreditLedgerEntryType.GRANT,
                amount_micros=4_000_000,
                idempotency_key="grant:verified-account",
                reason="verified_account_promotion",
                now=_NOW,
            )

    async with session_factory() as session:
        entries = list(
            (
                await session.execute(
                    select(PromotionalCreditLedgerEntry).order_by(
                        PromotionalCreditLedgerEntry.created_at,
                        PromotionalCreditLedgerEntry.id,
                    )
                )
            ).scalars()
        )
    assert sorted(entry.amount_micros for entry in entries) == sorted(
        [5_000_000, -1_500, -1_000, 1_000, 500]
    )


async def test_operator_completion_entries_are_separate_and_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game = Game(
            gauntlet_id=None,
            ruleset_id="mini7_v1",
            game_seed="completion-reserve-game",
            status="RUNNING",
        )
        session.add(game)
        await session.flush()

        charge, created = await promotional_credit_ledger.append_completion_reserve_entry(
            session,
            game_id=game.id,
            entry_type=promotional_credit_ledger.CompletionReserveEntryType.CHARGE,
            amount_micros=-175,
            estimate_miss_micros=175,
            model_slug="openai/gpt-5-mini",
            idempotency_key="completion:call-1",
            reason="estimate_miss",
            now=_NOW,
        )
        (
            duplicate,
            duplicate_created,
        ) = await promotional_credit_ledger.append_completion_reserve_entry(
            session,
            game_id=game.id,
            entry_type=promotional_credit_ledger.CompletionReserveEntryType.CHARGE,
            amount_micros=-175,
            estimate_miss_micros=175,
            model_slug="openai/gpt-5-mini",
            idempotency_key="completion:call-1",
            reason="estimate_miss",
            now=_NOW,
        )

    assert created is True
    assert duplicate_created is False
    assert duplicate.id == charge.id
    assert not hasattr(charge, "principal_id")
