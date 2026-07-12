"""Append-only promotional-credit and operator-reserve persistence (US-357).

The repository accepts only integer USD micros and caller-supplied timestamps.
It never computes an authoritative mutable balance: wallet availability is the
sum of immutable player-ledger movements, while active reservation rows expose
the subset currently held for an admitted game.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import (
    OperatorCompletionReserveLedgerEntry,
    PromotionalCreditLedgerEntry,
    PromotionalCreditReservation,
)


class CreditLedgerEntryType(StrEnum):
    """The fixed meaning and sign of one promotional-credit movement."""

    GRANT = "GRANT"
    RESERVATION = "RESERVATION"
    CHARGE = "CHARGE"
    RELEASE = "RELEASE"
    ADJUSTMENT = "ADJUSTMENT"


class CompletionReserveEntryType(StrEnum):
    """The fixed meaning and sign of one operator completion-reserve movement."""

    ALLOCATION = "ALLOCATION"
    CHARGE = "CHARGE"
    ADJUSTMENT = "ADJUSTMENT"


class LedgerIdempotencyConflict(ValueError):
    """Raised when a retry key is reused for a materially different write."""


class LedgerInputError(ValueError):
    """Raised before a ledger write whose integer or enum facts are invalid."""


@dataclass(frozen=True, slots=True)
class CreditTotals:
    """Derived promotional-credit facts suitable for future wallet projections."""

    granted_micros: int
    available_micros: int
    reserved_micros: int
    spent_micros: int


@dataclass(frozen=True, slots=True)
class CreditLedgerEntrySummary:
    """The bounded immutable facts needed for a private wallet projection."""

    entry_type: str
    amount_micros: int
    reason: str
    created_at: datetime


def _nonempty(value: str, *, field: str) -> str:
    if not value or value.strip() != value:
        raise LedgerInputError(f"{field}_invalid")
    return value


def _micro(value: int, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LedgerInputError(f"{field}_must_be_integer")
    return value


def _hash(value: str, *, field: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise LedgerInputError(f"{field}_invalid")
    return value


def _credit_entry_type(value: CreditLedgerEntryType | str) -> CreditLedgerEntryType:
    try:
        return CreditLedgerEntryType(value)
    except ValueError as exc:
        raise LedgerInputError("credit_entry_type_invalid") from exc


def _completion_entry_type(
    value: CompletionReserveEntryType | str,
) -> CompletionReserveEntryType:
    try:
        return CompletionReserveEntryType(value)
    except ValueError as exc:
        raise LedgerInputError("completion_entry_type_invalid") from exc


def _validate_credit_amount(entry_type: CreditLedgerEntryType, amount_micros: int) -> None:
    amount = _micro(amount_micros, field="amount_micros")
    valid = (
        (entry_type in {CreditLedgerEntryType.GRANT, CreditLedgerEntryType.RELEASE} and amount > 0)
        or (
            entry_type in {CreditLedgerEntryType.RESERVATION, CreditLedgerEntryType.CHARGE}
            and amount < 0
        )
        or (entry_type is CreditLedgerEntryType.ADJUSTMENT and amount != 0)
    )
    if not valid:
        raise LedgerInputError("credit_amount_sign_invalid")


def _validate_completion_amount(
    entry_type: CompletionReserveEntryType,
    amount_micros: int,
    estimate_miss_micros: int,
    game_id: uuid.UUID | None,
    model_slug: str | None,
) -> None:
    amount = _micro(amount_micros, field="amount_micros")
    miss = _micro(estimate_miss_micros, field="estimate_miss_micros")
    valid = (
        (entry_type is CompletionReserveEntryType.ALLOCATION and amount > 0)
        or (entry_type is CompletionReserveEntryType.CHARGE and amount < 0)
        or (entry_type is CompletionReserveEntryType.ADJUSTMENT and amount != 0)
    )
    if not valid:
        raise LedgerInputError("completion_amount_sign_invalid")
    if miss < 0:
        raise LedgerInputError("estimate_miss_micros_invalid")
    if entry_type is CompletionReserveEntryType.CHARGE and (
        game_id is None or model_slug is None or not model_slug.strip() or miss <= 0
    ):
        raise LedgerInputError("completion_charge_attribution_invalid")


def _same_credit_entry(
    row: PromotionalCreditLedgerEntry,
    *,
    principal_id: uuid.UUID,
    reservation_id: uuid.UUID | None,
    game_id: uuid.UUID | None,
    entry_type: CreditLedgerEntryType,
    amount_micros: int,
    reason: str,
) -> bool:
    return (
        row.principal_id == principal_id
        and row.reservation_id == reservation_id
        and row.game_id == game_id
        and row.entry_type == entry_type.value
        and row.amount_micros == amount_micros
        and row.reason == reason
    )


def _same_reservation(
    row: PromotionalCreditReservation,
    *,
    principal_id: uuid.UUID,
    lobby_id: uuid.UUID | None,
    game_id: uuid.UUID | None,
    estimate_hash: str,
    catalog_hash: str,
    rules_hash: str,
    reserved_micros: int,
    completion_reserve_per_game_cap_micros: int | None,
    completion_reserve_unfunded_policy: str | None,
) -> bool:
    return (
        row.principal_id == principal_id
        and row.lobby_id == lobby_id
        and row.game_id == game_id
        and row.estimate_hash == estimate_hash
        and row.catalog_hash == catalog_hash
        and row.rules_hash == rules_hash
        and row.reserved_micros == reserved_micros
        and row.completion_reserve_per_game_cap_micros == completion_reserve_per_game_cap_micros
        and row.completion_reserve_unfunded_policy == completion_reserve_unfunded_policy
    )


def _same_completion_entry(
    row: OperatorCompletionReserveLedgerEntry,
    *,
    game_id: uuid.UUID | None,
    entry_type: CompletionReserveEntryType,
    amount_micros: int,
    estimate_miss_micros: int,
    model_slug: str | None,
    reason: str,
) -> bool:
    return (
        row.game_id == game_id
        and row.entry_type == entry_type.value
        and row.amount_micros == amount_micros
        and row.estimate_miss_micros == estimate_miss_micros
        and row.model_slug == model_slug
        and row.reason == reason
    )


async def append_credit_entry(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    reservation_id: uuid.UUID | None,
    game_id: uuid.UUID | None,
    entry_type: CreditLedgerEntryType | str,
    amount_micros: int,
    idempotency_key: str,
    reason: str,
    now: datetime,
) -> tuple[PromotionalCreditLedgerEntry, bool]:
    """Append one wallet movement, returning an existing identical retry.

    The caller owns the surrounding transaction.  A nested savepoint converts a
    concurrent uniqueness race into a normal idempotent read while preserving
    that outer unit of work.
    """
    kind = _credit_entry_type(entry_type)
    _validate_credit_amount(kind, amount_micros)
    key = _nonempty(idempotency_key, field="idempotency_key")
    safe_reason = _nonempty(reason, field="reason")
    existing = await session.scalar(
        select(PromotionalCreditLedgerEntry).where(
            PromotionalCreditLedgerEntry.idempotency_key == key
        )
    )
    if existing is not None:
        if not _same_credit_entry(
            existing,
            principal_id=principal_id,
            reservation_id=reservation_id,
            game_id=game_id,
            entry_type=kind,
            amount_micros=amount_micros,
            reason=safe_reason,
        ):
            raise LedgerIdempotencyConflict("credit_ledger_idempotency_key_reused")
        return existing, False

    row = PromotionalCreditLedgerEntry(
        principal_id=principal_id,
        reservation_id=reservation_id,
        game_id=game_id,
        entry_type=kind.value,
        amount_micros=amount_micros,
        idempotency_key=key,
        reason=safe_reason,
        created_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        existing = await session.scalar(
            select(PromotionalCreditLedgerEntry).where(
                PromotionalCreditLedgerEntry.idempotency_key == key
            )
        )
        if existing is None:
            raise
        if not _same_credit_entry(
            existing,
            principal_id=principal_id,
            reservation_id=reservation_id,
            game_id=game_id,
            entry_type=kind,
            amount_micros=amount_micros,
            reason=safe_reason,
        ):
            raise LedgerIdempotencyConflict("credit_ledger_idempotency_key_reused") from exc
        return existing, False
    return row, True


async def create_or_get_credit_reservation(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    lobby_id: uuid.UUID | None,
    game_id: uuid.UUID | None,
    estimate_hash: str,
    catalog_hash: str,
    rules_hash: str,
    reserved_micros: int,
    idempotency_key: str,
    now: datetime,
    completion_reserve_per_game_cap_micros: int | None = None,
    completion_reserve_unfunded_policy: str | None = None,
) -> tuple[PromotionalCreditReservation, bool]:
    """Create one game quote allocation or return its exact idempotent retry."""
    amount = _micro(reserved_micros, field="reserved_micros")
    if amount < 0:
        raise LedgerInputError("reserved_micros_invalid")
    key = _nonempty(idempotency_key, field="idempotency_key")
    estimate = _hash(estimate_hash, field="estimate_hash")
    catalog = _hash(catalog_hash, field="catalog_hash")
    rules = _hash(rules_hash, field="rules_hash")
    completion_cap = (
        None
        if completion_reserve_per_game_cap_micros is None
        else _micro(
            completion_reserve_per_game_cap_micros,
            field="completion_reserve_per_game_cap_micros",
        )
    )
    if completion_cap is not None and completion_cap < 0:
        raise LedgerInputError("completion_reserve_per_game_cap_micros_invalid")
    completion_policy = completion_reserve_unfunded_policy
    if (completion_cap is None) != (completion_policy is None):
        raise LedgerInputError("completion_reserve_policy_pair_invalid")
    if completion_policy not in {None, "COERCE", "NO_CONTEST"}:
        raise LedgerInputError("completion_reserve_unfunded_policy_invalid")
    existing = await session.scalar(
        select(PromotionalCreditReservation).where(
            PromotionalCreditReservation.idempotency_key == key
        )
    )
    if existing is not None:
        if not _same_reservation(
            existing,
            principal_id=principal_id,
            lobby_id=lobby_id,
            game_id=game_id,
            estimate_hash=estimate,
            catalog_hash=catalog,
            rules_hash=rules,
            reserved_micros=amount,
            completion_reserve_per_game_cap_micros=completion_cap,
            completion_reserve_unfunded_policy=completion_policy,
        ):
            raise LedgerIdempotencyConflict("credit_reservation_idempotency_key_reused")
        return existing, False

    row = PromotionalCreditReservation(
        principal_id=principal_id,
        lobby_id=lobby_id,
        game_id=game_id,
        estimate_hash=estimate,
        catalog_hash=catalog,
        rules_hash=rules,
        reserved_micros=amount,
        consumed_micros=0,
        released_micros=0,
        status="ACTIVE",
        idempotency_key=key,
        reserved_at=now,
        updated_at=now,
        terminal_at=None,
        completion_reserve_per_game_cap_micros=completion_cap,
        completion_reserve_unfunded_policy=completion_policy,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        existing = await session.scalar(
            select(PromotionalCreditReservation).where(
                PromotionalCreditReservation.idempotency_key == key
            )
        )
        if existing is None:
            raise
        if not _same_reservation(
            existing,
            principal_id=principal_id,
            lobby_id=lobby_id,
            game_id=game_id,
            estimate_hash=estimate,
            catalog_hash=catalog,
            rules_hash=rules,
            reserved_micros=amount,
            completion_reserve_per_game_cap_micros=completion_cap,
            completion_reserve_unfunded_policy=completion_policy,
        ):
            raise LedgerIdempotencyConflict("credit_reservation_idempotency_key_reused") from exc
        return existing, False
    return row, True


async def append_completion_reserve_entry(
    session: AsyncSession,
    *,
    game_id: uuid.UUID | None,
    entry_type: CompletionReserveEntryType | str,
    amount_micros: int,
    estimate_miss_micros: int,
    model_slug: str | None,
    idempotency_key: str,
    reason: str,
    now: datetime,
) -> tuple[OperatorCompletionReserveLedgerEntry, bool]:
    """Append one operator reserve movement without linking a player identity."""
    kind = _completion_entry_type(entry_type)
    _validate_completion_amount(kind, amount_micros, estimate_miss_micros, game_id, model_slug)
    key = _nonempty(idempotency_key, field="idempotency_key")
    safe_reason = _nonempty(reason, field="reason")
    existing = await session.scalar(
        select(OperatorCompletionReserveLedgerEntry).where(
            OperatorCompletionReserveLedgerEntry.idempotency_key == key
        )
    )
    if existing is not None:
        if not _same_completion_entry(
            existing,
            game_id=game_id,
            entry_type=kind,
            amount_micros=amount_micros,
            estimate_miss_micros=estimate_miss_micros,
            model_slug=model_slug,
            reason=safe_reason,
        ):
            raise LedgerIdempotencyConflict("completion_reserve_idempotency_key_reused")
        return existing, False

    row = OperatorCompletionReserveLedgerEntry(
        game_id=game_id,
        entry_type=kind.value,
        amount_micros=amount_micros,
        estimate_miss_micros=estimate_miss_micros,
        model_slug=model_slug,
        idempotency_key=key,
        reason=safe_reason,
        created_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        existing = await session.scalar(
            select(OperatorCompletionReserveLedgerEntry).where(
                OperatorCompletionReserveLedgerEntry.idempotency_key == key
            )
        )
        if existing is None:
            raise
        if not _same_completion_entry(
            existing,
            game_id=game_id,
            entry_type=kind,
            amount_micros=amount_micros,
            estimate_miss_micros=estimate_miss_micros,
            model_slug=model_slug,
            reason=safe_reason,
        ):
            raise LedgerIdempotencyConflict("completion_reserve_idempotency_key_reused") from exc
        return existing, False
    return row, True


async def get_credit_totals(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> CreditTotals:
    """Derive balance, spend, and active holds from durable accounting facts."""
    available = await session.scalar(
        select(func.coalesce(func.sum(PromotionalCreditLedgerEntry.amount_micros), 0)).where(
            PromotionalCreditLedgerEntry.principal_id == principal_id
        )
    )
    granted = await session.scalar(
        select(func.coalesce(func.sum(PromotionalCreditLedgerEntry.amount_micros), 0)).where(
            PromotionalCreditLedgerEntry.principal_id == principal_id,
            PromotionalCreditLedgerEntry.entry_type == CreditLedgerEntryType.GRANT.value,
        )
    )
    spent = await session.scalar(
        select(func.coalesce(func.sum(-PromotionalCreditLedgerEntry.amount_micros), 0)).where(
            PromotionalCreditLedgerEntry.principal_id == principal_id,
            PromotionalCreditLedgerEntry.entry_type == CreditLedgerEntryType.CHARGE.value,
        )
    )
    reserved = await session.scalar(
        select(
            func.coalesce(
                func.sum(
                    PromotionalCreditReservation.reserved_micros
                    - PromotionalCreditReservation.consumed_micros
                    - PromotionalCreditReservation.released_micros
                ),
                0,
            )
        ).where(
            PromotionalCreditReservation.principal_id == principal_id,
            PromotionalCreditReservation.status == "ACTIVE",
        )
    )
    return CreditTotals(
        granted_micros=int(granted or 0),
        available_micros=int(available or 0),
        reserved_micros=int(reserved or 0),
        spent_micros=int(spent or 0),
    )


async def has_promotional_grant(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> bool:
    """Return whether a principal already owns the immutable one-time grant."""
    statement = (
        select(PromotionalCreditLedgerEntry.id)
        .where(
            PromotionalCreditLedgerEntry.principal_id == principal_id,
            PromotionalCreditLedgerEntry.entry_type == CreditLedgerEntryType.GRANT.value,
        )
        .limit(1)
    )
    return (await session.execute(statement)).first() is not None


async def list_recent_credit_entries(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    limit: int,
) -> list[CreditLedgerEntrySummary]:
    """Read a bounded newest-first private ledger projection for one principal."""
    if limit < 1:
        raise LedgerInputError("recent_entry_limit_invalid")
    statement = (
        select(PromotionalCreditLedgerEntry)
        .where(PromotionalCreditLedgerEntry.principal_id == principal_id)
        .order_by(
            desc(PromotionalCreditLedgerEntry.created_at), desc(PromotionalCreditLedgerEntry.id)
        )
        .limit(limit)
    )
    rows = list((await session.execute(statement)).scalars())
    return [
        CreditLedgerEntrySummary(
            entry_type=row.entry_type,
            amount_micros=row.amount_micros,
            reason=row.reason,
            created_at=row.created_at,
        )
        for row in rows
    ]


async def get_completion_subsidy_micros(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> int:
    """Sum operator completion spend for games funded by this private wallet.

    The operator ledger remains free of principal identifiers.  This read-side
    projection joins only through the player's own immutable game-linked ledger
    rows, so it can explain a caller's subsidy without denormalizing identity
    into operator accounting.
    """
    funded_game_ids = (
        select(PromotionalCreditLedgerEntry.game_id)
        .where(
            PromotionalCreditLedgerEntry.principal_id == principal_id,
            PromotionalCreditLedgerEntry.game_id.is_not(None),
        )
        .distinct()
    )
    subsidy = await session.scalar(
        select(
            func.coalesce(
                func.sum(-OperatorCompletionReserveLedgerEntry.amount_micros),
                0,
            )
        ).where(
            OperatorCompletionReserveLedgerEntry.entry_type
            == CompletionReserveEntryType.CHARGE.value,
            OperatorCompletionReserveLedgerEntry.game_id.in_(funded_game_ids),
        )
    )
    return int(subsidy or 0)


__all__ = [
    "CompletionReserveEntryType",
    "CreditLedgerEntrySummary",
    "CreditLedgerEntryType",
    "CreditTotals",
    "LedgerIdempotencyConflict",
    "LedgerInputError",
    "append_completion_reserve_entry",
    "append_credit_entry",
    "create_or_get_credit_reservation",
    "get_completion_subsidy_micros",
    "get_credit_totals",
    "has_promotional_grant",
    "list_recent_credit_entries",
]
