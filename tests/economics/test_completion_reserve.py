"""US-361 completion-reserve settlement and breaker coverage."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import (
    Game,
    OperatorCompletionReserveCursor,
    OperatorCompletionReserveLedgerEntry,
    Principal,
    PromotionalCreditReservation,
)
from padrino.economics.completion_reserve import (
    CompletionReserveUnfundedPolicy,
    completion_reserve_global_breaker_open,
    prepare_completion_reserve_cursors,
    reconcile_completion_reserve_cursors,
    settle_platform_completion_reserve,
    settle_platform_completion_reserve_with_retry,
)

_NOW = datetime(2026, 7, 12, 22, 0, tzinfo=UTC)


async def _reservation(
    session: AsyncSession,
    *,
    game_seed: str,
    player_reservation_micros: int,
    per_game_cap_micros: int,
    policy: CompletionReserveUnfundedPolicy = CompletionReserveUnfundedPolicy.COERCE,
) -> tuple[Game, PromotionalCreditReservation]:
    principal = Principal(kind="account")
    game = Game(
        gauntlet_id=None,
        ruleset_id="mini7_v1",
        game_seed=game_seed,
        status="RUNNING",
    )
    session.add_all([principal, game])
    await session.flush()
    reservation = PromotionalCreditReservation(
        principal_id=principal.id,
        lobby_id=None,
        game_id=game.id,
        estimate_hash="a" * 64,
        catalog_hash="b" * 64,
        rules_hash="c" * 64,
        reserved_micros=player_reservation_micros,
        consumed_micros=0,
        released_micros=0,
        status="ACTIVE",
        idempotency_key=f"reservation:{game_seed}",
        reserved_at=_NOW,
        updated_at=_NOW,
        terminal_at=None,
        completion_reserve_per_game_cap_micros=per_game_cap_micros,
        completion_reserve_unfunded_policy=policy.value,
    )
    session.add(reservation)
    await session.flush()
    return game, reservation


async def test_completion_reserve_bounds_player_game_global_and_retries(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An overrun consumes no more player credit and never overspends either cap."""
    async with session_factory() as session, session.begin():
        game_one, reservation_one = await _reservation(
            session,
            game_seed="completion-one",
            player_reservation_micros=7,
            per_game_cap_micros=4,
        )
        first = await settle_platform_completion_reserve(
            session,
            game_id=game_one.id,
            reservation_id=reservation_one.id,
            call_cost_micros=11,
            model_slug="openai/frozen-model",
            idempotency_key="completion:one:call-one",
            global_cap_micros=5,
            now=_NOW,
        )
        retry = await settle_platform_completion_reserve(
            session,
            game_id=game_one.id,
            reservation_id=reservation_one.id,
            call_cost_micros=11,
            model_slug="openai/frozen-model",
            idempotency_key="completion:one:call-one",
            global_cap_micros=5,
            now=_NOW,
        )

        game_two, reservation_two = await _reservation(
            session,
            game_seed="completion-two",
            player_reservation_micros=0,
            per_game_cap_micros=10,
        )
        second = await settle_platform_completion_reserve(
            session,
            game_id=game_two.id,
            reservation_id=reservation_two.id,
            call_cost_micros=4,
            model_slug="openai/frozen-model",
            idempotency_key="completion:two:call-one",
            global_cap_micros=5,
            now=_NOW,
        )

        assert first.promotional_charge_micros == 7
        assert first.operator_completion_charge_micros == 4
        assert first.unfunded_micros == 0
        assert first.policy is CompletionReserveUnfundedPolicy.COERCE
        assert retry == first
        assert reservation_one.consumed_micros == 7
        assert second.promotional_charge_micros == 0
        assert second.operator_completion_charge_micros == 1
        assert second.unfunded_micros == 3
        assert await completion_reserve_global_breaker_open(session, global_cap_micros=5) is True

    async with session_factory() as session:
        entries = list(
            (
                await session.execute(
                    select(OperatorCompletionReserveLedgerEntry).order_by(
                        OperatorCompletionReserveLedgerEntry.idempotency_key
                    )
                )
            ).scalars()
        )
    assert [entry.amount_micros for entry in entries] == [-4, -1]
    assert [entry.model_slug for entry in entries] == ["openai/frozen-model"] * 2


async def test_concurrent_overruns_never_exceed_global_cap_and_reconcile_after_restart(
    tmp_path: Path,
) -> None:
    """Concurrent workers cannot overspend the reserve; cursors recover from ledger."""
    from padrino.db.base import Base, create_engine, create_session_factory

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'completion-reserve.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session, session.begin():
            first_game, first_reservation = await _reservation(
                session,
                game_seed="completion-race-one",
                player_reservation_micros=0,
                per_game_cap_micros=5,
            )
            second_game, second_reservation = await _reservation(
                session,
                game_seed="completion-race-two",
                player_reservation_micros=0,
                per_game_cap_micros=5,
            )
            await prepare_completion_reserve_cursors(
                session,
                game_id=first_game.id,
                now=_NOW,
            )
            await prepare_completion_reserve_cursors(
                session,
                game_id=second_game.id,
                now=_NOW,
            )

        ready = asyncio.Event()

        async def settle_one(
            game: Game,
            reservation: PromotionalCreditReservation,
        ) -> int:
            await ready.wait()
            settlement = await settle_platform_completion_reserve_with_retry(
                session_factory,
                game_id=game.id,
                reservation_id=reservation.id,
                call_cost_micros=5,
                model_slug="openai/frozen-model",
                idempotency_key=f"completion-race:{game.id}",
                global_cap_micros=5,
                now=_NOW,
            )
            return settlement.operator_completion_charge_micros

        tasks = [
            asyncio.create_task(settle_one(first_game, first_reservation)),
            asyncio.create_task(settle_one(second_game, second_reservation)),
        ]
        ready.set()
        charges = await asyncio.gather(*tasks)

        async with session_factory() as session, session.begin():
            global_cursor = await session.get(OperatorCompletionReserveCursor, "global")
            assert global_cursor is not None
            global_cursor.charged_micros = 0
            recovered = await reconcile_completion_reserve_cursors(session, now=_NOW)
            assert recovered["global"] == 5

        assert sorted(charges) == [0, 5]
    finally:
        await engine.dispose()
