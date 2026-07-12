"""Durable, idempotent funding settlement for persisted LLM calls (US-362)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import FundingSource
from padrino.db.models import LlmCall, PromotionalCreditReservation
from padrino.db.repositories import promotional_credit_ledger
from padrino.economics.completion_reserve import (
    CompletionReserveSettlement,
    CompletionReserveSettlementError,
    CompletionReserveUnfundedPolicy,
    cost_usd_to_micros,
    settle_platform_completion_reserve,
)


class CallSettlementError(ValueError):
    """Raised when persisted call facts cannot yield one exact settlement."""


@dataclass(frozen=True, slots=True)
class CallFundingSettlement:
    """The durable funding attribution for one billable call."""

    call_id: uuid.UUID
    funding_source: FundingSource
    call_cost_micros: int
    promotional_charge_micros: int
    operator_completion_charge_micros: int
    unfunded_micros: int
    policy: CompletionReserveUnfundedPolicy | None

    @property
    def allowed(self) -> bool:
        """Whether the frozen funding policy covered the full provider cost."""
        return self.unfunded_micros == 0


@dataclass(frozen=True, slots=True)
class TerminalReservationSettlement:
    """One reservation's idempotent terminal release state."""

    reservation_id: uuid.UUID
    game_id: uuid.UUID
    consumed_micros: int
    released_micros: int
    status: str


@dataclass(frozen=True, slots=True)
class FundingCostLine:
    """One source's integer-micro cost total for a game."""

    funding_source: str
    call_count: int
    amount_micros: int


@dataclass(frozen=True, slots=True)
class FundingCostReport:
    """Byte-stable per-source call-cost projection for one game."""

    game_id: uuid.UUID
    lines: tuple[FundingCostLine, ...]

    @property
    def total_micros(self) -> int:
        """Return the exact integer sum of the ordered source lines."""
        return sum(line.amount_micros for line in self.lines)


def _funding_source(value: str) -> FundingSource:
    try:
        return FundingSource(value)
    except ValueError as exc:
        raise CallSettlementError("funding_source_invalid") from exc


def _charge_key(call: LlmCall) -> str:
    return f"llm-call-charge:{call.id}"


def _claim_charge_identity(call: LlmCall, *, amount_micros: int) -> str:
    """Write or verify this call's immutable monetary identity in-place."""
    key = _charge_key(call)
    if call.charge_idempotency_key is None and call.charge_amount_micros is None:
        call.charge_idempotency_key = key
        call.charge_amount_micros = amount_micros
        return key
    if call.charge_idempotency_key != key or call.charge_amount_micros != amount_micros:
        raise CallSettlementError("call_charge_identity_conflict")
    return key


def _settlement_from_platform(
    call: LlmCall,
    source: FundingSource,
    settlement: CompletionReserveSettlement,
) -> CallFundingSettlement:
    return CallFundingSettlement(
        call_id=call.id,
        funding_source=source,
        call_cost_micros=settlement.call_cost_micros,
        promotional_charge_micros=settlement.promotional_charge_micros,
        operator_completion_charge_micros=settlement.operator_completion_charge_micros,
        unfunded_micros=settlement.unfunded_micros,
        policy=settlement.policy,
    )


async def settle_persisted_llm_call(
    session: AsyncSession,
    *,
    call: LlmCall,
    global_cap_micros: int,
    now: datetime,
) -> CallFundingSettlement | None:
    """Settle one already-persisted billable call in the caller's transaction.

    The ``LlmCall`` row supplies the durable idempotency key.  Platform calls
    use the active game reservation when one exists; BYOK and legacy/unreserved
    calls retain a cost audit but never write a promotional debit.
    """
    if call.cost_usd is None:
        return None
    amount_micros = cost_usd_to_micros(call.cost_usd)
    key = _claim_charge_identity(call, amount_micros=amount_micros)
    source = _funding_source(call.funding_source)
    if source is not FundingSource.PLATFORM:
        return CallFundingSettlement(
            call_id=call.id,
            funding_source=source,
            call_cost_micros=amount_micros,
            promotional_charge_micros=0,
            operator_completion_charge_micros=0,
            unfunded_micros=0,
            policy=None,
        )

    reservation = await session.scalar(
        select(PromotionalCreditReservation)
        .where(
            PromotionalCreditReservation.game_id == call.game_id,
            PromotionalCreditReservation.status == "ACTIVE",
        )
        .with_for_update()
    )
    if reservation is None:
        return CallFundingSettlement(
            call_id=call.id,
            funding_source=source,
            call_cost_micros=amount_micros,
            promotional_charge_micros=0,
            operator_completion_charge_micros=0,
            unfunded_micros=0,
            policy=None,
        )
    model_slug = call.openrouter_requested_model_slug or call.openrouter_actual_model
    if model_slug is None:
        raise CallSettlementError("platform_model_slug_missing")
    try:
        settlement = await settle_platform_completion_reserve(
            session,
            game_id=call.game_id,
            reservation_id=reservation.id,
            call_cost_micros=amount_micros,
            model_slug=model_slug,
            idempotency_key=key,
            global_cap_micros=global_cap_micros,
            now=now,
            llm_call_id=call.id,
        )
    except CompletionReserveSettlementError as exc:
        raise CallSettlementError(str(exc)) from exc
    return _settlement_from_platform(call, source, settlement)


async def reconcile_game_call_settlements(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    global_cap_micros: int,
    now: datetime,
) -> tuple[CallFundingSettlement, ...]:
    """Repair charge work left after a committed-call/crash boundary.

    Rows are processed by creation time and UUID, so recovery produces an exact
    ordering for reports and never changes a frozen route or selected model.
    """
    calls = list(
        (
            await session.execute(
                select(LlmCall)
                .where(
                    LlmCall.game_id == game_id,
                    LlmCall.cost_usd.is_not(None),
                    LlmCall.charge_idempotency_key.is_(None),
                )
                .order_by(LlmCall.created_at, LlmCall.id)
                .with_for_update()
            )
        ).scalars()
    )
    settled: list[CallFundingSettlement] = []
    for call in calls:
        outcome = await settle_persisted_llm_call(
            session,
            call=call,
            global_cap_micros=global_cap_micros,
            now=now,
        )
        if outcome is not None:
            settled.append(outcome)
    return tuple(settled)


async def settle_terminal_reservation(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    now: datetime,
) -> TerminalReservationSettlement | None:
    """Release a platform reservation's unused micros exactly once at terminal."""
    reservation = await session.scalar(
        select(PromotionalCreditReservation)
        .where(
            PromotionalCreditReservation.game_id == game_id,
            PromotionalCreditReservation.status == "ACTIVE",
        )
        .with_for_update()
    )
    if reservation is None:
        settled = await session.scalar(
            select(PromotionalCreditReservation)
            .where(
                PromotionalCreditReservation.game_id == game_id,
                PromotionalCreditReservation.status == "SETTLED",
            )
            .with_for_update()
        )
        if settled is None:
            return None
        return TerminalReservationSettlement(
            reservation_id=settled.id,
            game_id=settled.game_id or game_id,
            consumed_micros=settled.consumed_micros,
            released_micros=settled.released_micros,
            status=settled.status,
        )
    unused_micros = (
        reservation.reserved_micros - reservation.consumed_micros - reservation.released_micros
    )
    if unused_micros < 0:
        raise CallSettlementError("reservation_accounting_invalid")
    if unused_micros:
        await promotional_credit_ledger.append_credit_entry(
            session,
            principal_id=reservation.principal_id,
            reservation_id=reservation.id,
            game_id=game_id,
            entry_type=promotional_credit_ledger.CreditLedgerEntryType.RELEASE,
            amount_micros=unused_micros,
            idempotency_key=f"terminal-reservation-release:{reservation.id}",
            reason="terminal_unused_reservation",
            now=now,
        )
        reservation.released_micros += unused_micros
    reservation.status = "SETTLED"
    reservation.terminal_at = now
    reservation.updated_at = now
    return TerminalReservationSettlement(
        reservation_id=reservation.id,
        game_id=game_id,
        consumed_micros=reservation.consumed_micros,
        released_micros=reservation.released_micros,
        status=reservation.status,
    )


async def funding_cost_report(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
) -> FundingCostReport:
    """Return source totals from persisted integer charge amounts in stable order."""
    rows = (
        await session.execute(
            select(
                LlmCall.funding_source,
                func.count(LlmCall.id),
                func.coalesce(func.sum(LlmCall.charge_amount_micros), 0),
            )
            .where(
                LlmCall.game_id == game_id,
                LlmCall.charge_amount_micros.is_not(None),
            )
            .group_by(LlmCall.funding_source)
            .order_by(LlmCall.funding_source)
        )
    ).all()
    return FundingCostReport(
        game_id=game_id,
        lines=tuple(
            FundingCostLine(
                funding_source=str(source),
                call_count=int(count),
                amount_micros=int(amount),
            )
            for source, count, amount in rows
        ),
    )


__all__ = [
    "CallFundingSettlement",
    "CallSettlementError",
    "FundingCostLine",
    "FundingCostReport",
    "TerminalReservationSettlement",
    "funding_cost_report",
    "reconcile_game_call_settlements",
    "settle_persisted_llm_call",
    "settle_terminal_reservation",
]
