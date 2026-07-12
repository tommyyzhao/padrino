"""Bounded, recoverable operator completion-reserve settlement (US-361).

The append-only operator ledger is the authority for money spent.  This module
uses small mutable cursors only to serialize cap claims across concurrent game
workers, and can reconcile every cursor from that ledger after a restart.  It
never changes a frozen model selection or routing policy.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.enums import FundingSource
from padrino.core.funding_policy import (
    CompletionReserveFacts,
    FundingPolicyReason,
    evaluate_completion_reserve,
)
from padrino.db.models import (
    OperatorCompletionReserveCursor,
    OperatorCompletionReserveLedgerEntry,
    OperatorCompletionReserveSettlement,
    PromotionalCreditReservation,
)
from padrino.db.repositories import promotional_credit_ledger

_GLOBAL_SCOPE = "global"
_USD_MICROS = Decimal(1_000_000)


class CompletionReserveUnfundedPolicy(StrEnum):
    """Persisted game behavior once the operator reserve cannot fund a call."""

    COERCE = "COERCE"
    NO_CONTEST = "NO_CONTEST"


class CompletionReserveSettlementError(ValueError):
    """Raised for malformed or legacy-incomplete completion-reserve inputs."""


class _ConcurrentCapChanged(RuntimeError):
    """Retry an inner savepoint after a competing cap claim won the race."""


@dataclass(frozen=True, slots=True)
class CompletionReserveSettlement:
    """One durable cost split, including the deterministic unfunded outcome."""

    game_id: uuid.UUID
    reservation_id: uuid.UUID
    model_slug: str
    call_cost_micros: int
    promotional_charge_micros: int
    operator_completion_charge_micros: int
    unfunded_micros: int
    policy: CompletionReserveUnfundedPolicy
    allowed: bool
    reason: FundingPolicyReason


def _non_negative_int(value: int, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CompletionReserveSettlementError(f"{field}_invalid")
    return value


def _nonempty(value: str, *, field: str) -> str:
    if not value or value.strip() != value:
        raise CompletionReserveSettlementError(f"{field}_invalid")
    return value


def cost_usd_to_micros(value: object) -> int:
    """Round a provider-reported USD cost up into the ledger's integer unit."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CompletionReserveSettlementError("provider_cost_invalid") from exc
    if not amount.is_finite() or amount < 0:
        raise CompletionReserveSettlementError("provider_cost_invalid")
    return int((amount * _USD_MICROS).to_integral_value(rounding=ROUND_CEILING))


def _policy(value: str | CompletionReserveUnfundedPolicy | None) -> CompletionReserveUnfundedPolicy:
    if isinstance(value, CompletionReserveUnfundedPolicy):
        return value
    if not isinstance(value, str):
        raise CompletionReserveSettlementError("completion_reserve_policy_missing")
    try:
        return CompletionReserveUnfundedPolicy(value)
    except ValueError as exc:
        raise CompletionReserveSettlementError("completion_reserve_policy_missing") from exc


def _game_scope(game_id: uuid.UUID) -> str:
    return f"game:{game_id}"


def _settlement_from_row(
    row: OperatorCompletionReserveSettlement,
) -> CompletionReserveSettlement:
    policy = _policy(row.unfunded_policy)
    reason = (
        FundingPolicyReason.COMPLETION_FUNDED
        if row.unfunded_micros == 0
        else FundingPolicyReason.OPERATOR_COMPLETION_RESERVE_EXHAUSTED
    )
    return CompletionReserveSettlement(
        game_id=row.game_id,
        reservation_id=row.reservation_id,
        model_slug=row.model_slug,
        call_cost_micros=row.call_cost_micros,
        promotional_charge_micros=row.promotional_charge_micros,
        operator_completion_charge_micros=row.completion_reserve_charge_micros,
        unfunded_micros=row.unfunded_micros,
        policy=policy,
        allowed=row.unfunded_micros == 0,
        reason=reason,
    )


def _same_settlement(
    row: OperatorCompletionReserveSettlement,
    *,
    game_id: uuid.UUID,
    reservation_id: uuid.UUID,
    model_slug: str,
    call_cost_micros: int,
    llm_call_id: uuid.UUID | None,
) -> bool:
    return (
        row.game_id == game_id
        and row.reservation_id == reservation_id
        and row.model_slug == model_slug
        and row.call_cost_micros == call_cost_micros
        and row.llm_call_id == llm_call_id
    )


async def _append_promotional_charge_pair(
    session: AsyncSession,
    *,
    reservation: PromotionalCreditReservation,
    amount_micros: int,
    idempotency_key: str,
    now: datetime,
) -> None:
    """Convert part of one durable hold into an immutable call charge.

    The original reservation debit remains in the ledger while the game is
    live.  A same-amount release therefore accompanies every call charge: the
    pair leaves the held balance unchanged until terminal settlement releases
    only the unused remainder, while the charge history reports actual spend.
    """
    if amount_micros == 0:
        return
    await promotional_credit_ledger.append_credit_entry(
        session,
        principal_id=reservation.principal_id,
        reservation_id=reservation.id,
        game_id=reservation.game_id,
        entry_type=promotional_credit_ledger.CreditLedgerEntryType.CHARGE,
        amount_micros=-amount_micros,
        idempotency_key=f"{idempotency_key}:promotional_charge",
        reason="custom_game_openrouter_call",
        now=now,
    )
    await promotional_credit_ledger.append_credit_entry(
        session,
        principal_id=reservation.principal_id,
        reservation_id=reservation.id,
        game_id=reservation.game_id,
        entry_type=promotional_credit_ledger.CreditLedgerEntryType.RELEASE,
        amount_micros=amount_micros,
        idempotency_key=f"{idempotency_key}:reservation_consumed",
        reason="reservation_consumed",
        now=now,
    )


async def _existing_settlement(
    session: AsyncSession,
    *,
    row: OperatorCompletionReserveSettlement,
    game_id: uuid.UUID,
    reservation_id: uuid.UUID,
    model_slug: str,
    call_cost_micros: int,
    llm_call_id: uuid.UUID | None,
    idempotency_key: str,
    now: datetime,
) -> CompletionReserveSettlement:
    """Return an exact retry while repairing pre-US-362 ledger pair gaps."""
    if not _same_settlement(
        row,
        game_id=game_id,
        reservation_id=reservation_id,
        model_slug=model_slug,
        call_cost_micros=call_cost_micros,
        llm_call_id=llm_call_id,
    ):
        raise CompletionReserveSettlementError("completion_reserve_idempotency_key_reused")
    reservation = await session.scalar(
        select(PromotionalCreditReservation)
        .where(PromotionalCreditReservation.id == reservation_id)
        .with_for_update()
    )
    if reservation is None:
        raise CompletionReserveSettlementError("completion_reserve_reservation_invalid")
    await _append_promotional_charge_pair(
        session,
        reservation=reservation,
        amount_micros=row.promotional_charge_micros,
        idempotency_key=idempotency_key,
        now=now,
    )
    return _settlement_from_row(row)


async def _charged_from_ledger(
    session: AsyncSession,
    *,
    game_id: uuid.UUID | None,
) -> int:
    statement = select(
        func.coalesce(func.sum(-OperatorCompletionReserveLedgerEntry.amount_micros), 0)
    ).where(
        OperatorCompletionReserveLedgerEntry.entry_type
        == promotional_credit_ledger.CompletionReserveEntryType.CHARGE.value
    )
    if game_id is not None:
        statement = statement.where(OperatorCompletionReserveLedgerEntry.game_id == game_id)
    return int((await session.scalar(statement)) or 0)


async def _get_or_create_cursor(
    session: AsyncSession,
    *,
    scope_key: str,
    game_id: uuid.UUID | None,
    now: datetime,
) -> OperatorCompletionReserveCursor:
    cursor = await session.scalar(
        select(OperatorCompletionReserveCursor)
        .where(OperatorCompletionReserveCursor.scope_key == scope_key)
        .with_for_update()
    )
    if cursor is not None:
        return cursor
    cursor = OperatorCompletionReserveCursor(
        scope_key=scope_key,
        charged_micros=await _charged_from_ledger(session, game_id=game_id),
        updated_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(cursor)
            await session.flush()
    except IntegrityError:
        cursor = await session.scalar(
            select(OperatorCompletionReserveCursor)
            .where(OperatorCompletionReserveCursor.scope_key == scope_key)
            .with_for_update()
        )
        if cursor is None:
            raise
    return cursor


async def _claim_cursor(
    session: AsyncSession,
    *,
    cursor: OperatorCompletionReserveCursor,
    amount_micros: int,
    cap_micros: int,
    now: datetime,
) -> bool:
    if amount_micros == 0:
        return True
    result = await session.execute(
        update(OperatorCompletionReserveCursor)
        .where(
            OperatorCompletionReserveCursor.scope_key == cursor.scope_key,
            OperatorCompletionReserveCursor.charged_micros + amount_micros <= cap_micros,
        )
        .values(
            charged_micros=OperatorCompletionReserveCursor.charged_micros + amount_micros,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    return cast(CursorResult[Any], result).rowcount == 1


async def completion_reserve_global_breaker_open(
    session: AsyncSession,
    *,
    global_cap_micros: int,
) -> bool:
    """Return whether a global hard cap leaves no reserve for new admissions."""
    cap = _non_negative_int(global_cap_micros, field="global_cap_micros")
    if cap == 0:
        return True
    return await _charged_from_ledger(session, game_id=None) >= cap


async def prepare_completion_reserve_cursors(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    now: datetime,
) -> None:
    """Create the durable global/game cap rows when a funded game is admitted."""
    await _get_or_create_cursor(session, scope_key=_GLOBAL_SCOPE, game_id=None, now=now)
    await _get_or_create_cursor(
        session,
        scope_key=_game_scope(game_id),
        game_id=game_id,
        now=now,
    )


async def settle_platform_completion_reserve(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    reservation_id: uuid.UUID,
    call_cost_micros: int,
    model_slug: str,
    idempotency_key: str,
    global_cap_micros: int,
    now: datetime,
    llm_call_id: uuid.UUID | None = None,
) -> CompletionReserveSettlement:
    """Split one actual platform call without exceeding player or operator caps.

    The promotional hold is already in the player's append-only ledger.  The
    reservation's consumed counter is paired with immutable player charge
    and conversion-release entries.  The completion-reserve charge is appended
    immediately because it represents an estimate miss that must constrain
    later games before their admission.
    """
    cost = _non_negative_int(call_cost_micros, field="call_cost_micros")
    cap = _non_negative_int(global_cap_micros, field="global_cap_micros")
    slug = _nonempty(model_slug, field="model_slug")
    key = _nonempty(idempotency_key, field="idempotency_key")

    existing = await session.scalar(
        select(OperatorCompletionReserveSettlement).where(
            OperatorCompletionReserveSettlement.idempotency_key == key
        )
    )
    if existing is not None:
        return await _existing_settlement(
            session,
            row=existing,
            game_id=game_id,
            reservation_id=reservation_id,
            model_slug=slug,
            call_cost_micros=cost,
            llm_call_id=llm_call_id,
            idempotency_key=key,
            now=now,
        )

    for _ in range(4):
        try:
            async with session.begin_nested():
                reservation = await session.scalar(
                    select(PromotionalCreditReservation)
                    .where(PromotionalCreditReservation.id == reservation_id)
                    .with_for_update()
                )
                if reservation is None or reservation.game_id != game_id:
                    raise CompletionReserveSettlementError("completion_reserve_reservation_invalid")
                if reservation.status != "ACTIVE":
                    raise CompletionReserveSettlementError(
                        "completion_reserve_reservation_inactive"
                    )
                policy = _policy(reservation.completion_reserve_unfunded_policy)
                frozen_per_game_cap = reservation.completion_reserve_per_game_cap_micros
                if frozen_per_game_cap is None:
                    raise CompletionReserveSettlementError("completion_reserve_policy_missing")
                per_game_cap = _non_negative_int(
                    frozen_per_game_cap,
                    field="completion_reserve_per_game_cap_micros",
                )
                global_cursor = await _get_or_create_cursor(
                    session,
                    scope_key=_GLOBAL_SCOPE,
                    game_id=None,
                    now=now,
                )
                game_cursor = await _get_or_create_cursor(
                    session,
                    scope_key=_game_scope(game_id),
                    game_id=game_id,
                    now=now,
                )
                player_remaining = max(
                    0,
                    reservation.reserved_micros
                    - reservation.consumed_micros
                    - reservation.released_micros,
                )
                operator_remaining = min(
                    max(0, cap - global_cursor.charged_micros),
                    max(0, per_game_cap - game_cursor.charged_micros),
                )
                decision = evaluate_completion_reserve(
                    CompletionReserveFacts(
                        source=FundingSource.PLATFORM,
                        call_cost_micros=cost,
                        promotional_reservation_remaining_micros=player_remaining,
                        operator_completion_reserve_remaining_micros=operator_remaining,
                    )
                )
                charge = decision.operator_completion_charge_micros
                if not await _claim_cursor(
                    session,
                    cursor=global_cursor,
                    amount_micros=charge,
                    cap_micros=cap,
                    now=now,
                ):
                    raise _ConcurrentCapChanged
                if not await _claim_cursor(
                    session,
                    cursor=game_cursor,
                    amount_micros=charge,
                    cap_micros=per_game_cap,
                    now=now,
                ):
                    raise _ConcurrentCapChanged
                if charge:
                    await promotional_credit_ledger.append_completion_reserve_entry(
                        session,
                        game_id=game_id,
                        entry_type=promotional_credit_ledger.CompletionReserveEntryType.CHARGE,
                        amount_micros=-charge,
                        estimate_miss_micros=cost - decision.promotional_charge_micros,
                        model_slug=slug,
                        idempotency_key=f"{key}:operator_completion",
                        reason="custom_game_estimate_miss",
                        now=now,
                    )
                reservation.consumed_micros += decision.promotional_charge_micros
                reservation.updated_at = now
                await _append_promotional_charge_pair(
                    session,
                    reservation=reservation,
                    amount_micros=decision.promotional_charge_micros,
                    idempotency_key=key,
                    now=now,
                )
                row = OperatorCompletionReserveSettlement(
                    game_id=game_id,
                    reservation_id=reservation.id,
                    llm_call_id=llm_call_id,
                    model_slug=slug,
                    call_cost_micros=cost,
                    promotional_charge_micros=decision.promotional_charge_micros,
                    completion_reserve_charge_micros=charge,
                    unfunded_micros=decision.unfunded_micros,
                    unfunded_policy=policy.value,
                    idempotency_key=key,
                    created_at=now,
                )
                session.add(row)
                await session.flush()
                return _settlement_from_row(row)
        except _ConcurrentCapChanged:
            continue
        except IntegrityError:
            existing = await session.scalar(
                select(OperatorCompletionReserveSettlement).where(
                    OperatorCompletionReserveSettlement.idempotency_key == key
                )
            )
            if existing is not None:
                return await _existing_settlement(
                    session,
                    row=existing,
                    game_id=game_id,
                    reservation_id=reservation_id,
                    model_slug=slug,
                    call_cost_micros=cost,
                    llm_call_id=llm_call_id,
                    idempotency_key=key,
                    now=now,
                )
            raise
    raise CompletionReserveSettlementError("completion_reserve_concurrent_retry_exhausted")


async def reconcile_completion_reserve_cursors(
    session: AsyncSession,
    *,
    now: datetime,
) -> dict[str, int]:
    """Restore cap cursors from the append-only ledger after interruption.

    Callers run this in their own maintenance transaction.  It touches no game
    event, selected model, routing policy, or player ledger row.
    """
    by_game = list(
        (
            await session.execute(
                select(
                    OperatorCompletionReserveLedgerEntry.game_id,
                    func.coalesce(func.sum(-OperatorCompletionReserveLedgerEntry.amount_micros), 0),
                )
                .where(
                    OperatorCompletionReserveLedgerEntry.entry_type
                    == promotional_credit_ledger.CompletionReserveEntryType.CHARGE.value,
                    OperatorCompletionReserveLedgerEntry.game_id.is_not(None),
                )
                .group_by(OperatorCompletionReserveLedgerEntry.game_id)
            )
        ).all()
    )
    desired = {_GLOBAL_SCOPE: await _charged_from_ledger(session, game_id=None)}
    desired.update({_game_scope(game_id): int(charged) for game_id, charged in by_game})
    existing = list((await session.execute(select(OperatorCompletionReserveCursor))).scalars())
    for cursor in existing:
        desired.setdefault(cursor.scope_key, 0)
    for scope_key, charged in desired.items():
        cursor = await _get_or_create_cursor(
            session,
            scope_key=scope_key,
            game_id=None
            if scope_key == _GLOBAL_SCOPE
            else uuid.UUID(scope_key.removeprefix("game:")),
            now=now,
        )
        cursor.charged_micros = charged
        cursor.updated_at = now
    await session.flush()
    return dict(sorted(desired.items()))


async def settle_platform_completion_reserve_with_retry(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    reservation_id: uuid.UUID,
    call_cost_micros: int,
    model_slug: str,
    idempotency_key: str,
    global_cap_micros: int,
    now: datetime,
    llm_call_id: uuid.UUID | None = None,
) -> CompletionReserveSettlement:
    """Run an idempotent settlement in its own retryable transaction.

    SQLite permits one writer at a time, while Postgres can surface a transient
    serialization failure.  The settlement key makes a replay exact, so a
    bounded retry over a fresh session is safe and keeps concurrent workers
    from treating a temporary write lock as an unfunded provider result.
    """
    last_error: OperationalError | None = None
    for _ in range(8):
        try:
            async with session_factory() as session, session.begin():
                return await settle_platform_completion_reserve(
                    session,
                    game_id=game_id,
                    reservation_id=reservation_id,
                    call_cost_micros=call_cost_micros,
                    model_slug=model_slug,
                    idempotency_key=idempotency_key,
                    global_cap_micros=global_cap_micros,
                    now=now,
                    llm_call_id=llm_call_id,
                )
        except OperationalError as exc:
            last_error = exc
            await asyncio.sleep(0)
    assert last_error is not None
    raise last_error


__all__ = [
    "CompletionReserveSettlement",
    "CompletionReserveSettlementError",
    "CompletionReserveUnfundedPolicy",
    "completion_reserve_global_breaker_open",
    "cost_usd_to_micros",
    "prepare_completion_reserve_cursors",
    "reconcile_completion_reserve_cursors",
    "settle_platform_completion_reserve",
    "settle_platform_completion_reserve_with_retry",
]
