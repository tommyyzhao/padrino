"""Impure promotional-grant and private-wallet orchestration (US-358).

The pure funding policy owns grant eligibility.  This module resolves only the
required database facts, appends the immutable grant through the ledger
repository, and projects a bounded private wallet.  It never handles provider
credentials or any raw OpenRouter value.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.funding_policy import (
    PromotionalGrantDecision,
    PromotionalGrantFacts,
    evaluate_promotional_grant,
)
from padrino.db.models import OAuthIdentity, Principal
from padrino.db.repositories import promotional_credit_ledger


@dataclass(frozen=True, slots=True)
class WalletSnapshot:
    """Integer-micro wallet facts and bounded immutable entries for one caller."""

    granted_micros: int
    available_micros: int
    reserved_micros: int
    spent_micros: int
    completion_subsidy_micros: int
    recent_entries: tuple[promotional_credit_ledger.CreditLedgerEntrySummary, ...]


@dataclass(frozen=True, slots=True)
class PromotionalGrantResult:
    """The policy decision and whether this request appended the grant row."""

    decision: PromotionalGrantDecision
    created: bool


async def grant_promotional_credit(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    credit_abuse_banned: bool | None,
    now: datetime,
) -> PromotionalGrantResult:
    """Lazily issue the one-time grant or return its stable refusal decision.

    The fixed principal-scoped idempotency key and the partial one-grant unique
    index make the append safe across concurrent SQLite/Postgres requests.  A
    losing concurrent request re-reads as an already-issued grant rather than
    creating a second ledger movement.
    """
    facts = await _grant_facts(
        session,
        principal_id=principal_id,
        credit_abuse_banned=credit_abuse_banned,
    )
    decision = evaluate_promotional_grant(facts)
    if not decision.allowed:
        return PromotionalGrantResult(decision=decision, created=False)

    _entry, created = await promotional_credit_ledger.append_credit_entry(
        session,
        principal_id=principal_id,
        reservation_id=None,
        game_id=None,
        entry_type=promotional_credit_ledger.CreditLedgerEntryType.GRANT,
        amount_micros=decision.grant_micros,
        idempotency_key=f"promotional-grant:{principal_id}",
        reason="verified_account_promotion",
        now=now,
    )
    if created:
        return PromotionalGrantResult(decision=decision, created=True)

    issued = PromotionalGrantFacts(
        principal_kind=facts.principal_kind,
        email_verified=facts.email_verified,
        deleted=facts.deleted,
        credit_abuse_banned=facts.credit_abuse_banned,
        promotional_grant_issued=True,
    )
    return PromotionalGrantResult(
        decision=evaluate_promotional_grant(issued),
        created=False,
    )


async def get_wallet_snapshot(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    recent_limit: int = 20,
) -> WalletSnapshot:
    """Return a safe, bounded integer-micro wallet projection for one principal."""
    totals = await promotional_credit_ledger.get_credit_totals(session, principal_id=principal_id)
    entries = await promotional_credit_ledger.list_recent_credit_entries(
        session,
        principal_id=principal_id,
        limit=recent_limit,
    )
    subsidy = await promotional_credit_ledger.get_completion_subsidy_micros(
        session,
        principal_id=principal_id,
    )
    return WalletSnapshot(
        granted_micros=totals.granted_micros,
        available_micros=totals.available_micros,
        reserved_micros=totals.reserved_micros,
        spent_micros=totals.spent_micros,
        completion_subsidy_micros=subsidy,
        recent_entries=tuple(entries),
    )


async def _grant_facts(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    credit_abuse_banned: bool | None,
) -> PromotionalGrantFacts:
    principal = await session.get(Principal, principal_id)
    if principal is None:
        return PromotionalGrantFacts(credit_abuse_banned=credit_abuse_banned)
    email_verified = await _has_verified_email(session, principal_id=principal.id)
    issued = await promotional_credit_ledger.has_promotional_grant(
        session,
        principal_id=principal.id,
    )
    return PromotionalGrantFacts(
        principal_kind=principal.kind,
        email_verified=email_verified,
        deleted=principal.deleted_at is not None,
        credit_abuse_banned=credit_abuse_banned,
        promotional_grant_issued=issued,
    )


async def _has_verified_email(session: AsyncSession, *, principal_id: uuid.UUID) -> bool:
    statement = (
        select(OAuthIdentity.id)
        .where(
            OAuthIdentity.principal_id == principal_id,
            OAuthIdentity.email_verified.is_(True),
            OAuthIdentity.email.is_not(None),
        )
        .limit(1)
    )
    return (await session.execute(statement)).first() is not None


__all__ = [
    "PromotionalGrantResult",
    "WalletSnapshot",
    "get_wallet_snapshot",
    "grant_promotional_credit",
]
