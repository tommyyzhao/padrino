"""Offline audit helpers for ranked human-rating provenance.

The audit is intentionally impure and DB-backed: it runs outside the core engine
to quarantine historical ``HumanRating`` rows that cannot be trusted for the
published human ladder. Quarantine is reversible and only sets
``HumanRating.quarantined_at``; rating values and event rows are never changed.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import LeagueKind
from padrino.core.ranked_eligibility import RankedEligibilityReason
from padrino.db.models import (
    HumanRating,
    HumanRatingEvent,
    League,
    Principal,
    RankedWriteAudit,
)
from padrino.db.repositories.human_principals import PRINCIPAL_KIND_ACCOUNT


@dataclass(frozen=True, slots=True)
class RankedAuditSummary:
    """Counts returned by one ranked human-rating audit pass."""

    scanned: int
    newly_quarantined: int
    missing_provenance: int
    ineligible_principal: int
    dry_run: bool

    def to_dict(self) -> dict[str, int | bool]:
        """Return a JSON-serializable summary payload."""
        return asdict(self)


async def audit_ranked_human_ratings(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> RankedAuditSummary:
    """Quarantine untrusted ranked human-rating rows.

    A row is untrusted when any contributing ``HumanRatingEvent`` lacks a clean
    ``APPLIED`` provenance row from ``ranked_write_audit``, when there are no
    contributing events at all, or when its principal is no longer a live account
    principal. The full legal/ban resolver lands in later stories; this pass
    enforces only the currently available identity fact and the US-294
    provenance trail.
    """

    quarantine_time = now if now is not None else datetime.now(UTC)
    rows = await _candidate_rows(session)
    scanned = len(rows)
    newly_quarantined = 0
    missing_provenance = 0
    ineligible_principal = 0

    for row in rows:
        lacks_provenance = await _lacks_clean_applied_provenance(session, row)
        principal_ineligible = not await _is_live_account_principal(session, row.human_player_id)
        should_quarantine = lacks_provenance or principal_ineligible
        if lacks_provenance:
            missing_provenance += 1
        if principal_ineligible:
            ineligible_principal += 1
        if not should_quarantine:
            continue
        newly_quarantined += 1
        if not dry_run:
            row.quarantined_at = quarantine_time

    if newly_quarantined and not dry_run:
        await session.flush()

    return RankedAuditSummary(
        scanned=scanned,
        newly_quarantined=newly_quarantined,
        missing_provenance=missing_provenance,
        ineligible_principal=ineligible_principal,
        dry_run=dry_run,
    )


async def _candidate_rows(session: AsyncSession) -> list[HumanRating]:
    stmt = (
        select(HumanRating)
        .join(League, League.id == HumanRating.league_id)
        .where(
            League.kind == LeagueKind.HUMANS_INCLUDED.value,
            League.ranked.is_(True),
            HumanRating.quarantined_at.is_(None),
        )
        .order_by(HumanRating.id)
    )
    return list((await session.execute(stmt)).scalars())


async def _is_live_account_principal(session: AsyncSession, human_player_id: str) -> bool:
    try:
        principal_id = uuid.UUID(human_player_id)
    except ValueError:
        return False
    principal = await session.get(Principal, principal_id)
    return (
        principal is not None
        and principal.deleted_at is None
        and principal.kind == PRINCIPAL_KIND_ACCOUNT
    )


async def _lacks_clean_applied_provenance(
    session: AsyncSession,
    row: HumanRating,
) -> bool:
    events_stmt = select(HumanRatingEvent).where(
        HumanRatingEvent.league_id == row.league_id,
        HumanRatingEvent.human_player_id == row.human_player_id,
        HumanRatingEvent.scope_type == row.scope_type,
        HumanRatingEvent.scope_value == row.scope_value,
    )
    events = list((await session.execute(events_stmt)).scalars())
    if not events:
        return True
    for event in events:
        if not await _has_clean_applied_audit(session, event):
            return True
    return False


async def _has_clean_applied_audit(session: AsyncSession, event: HumanRatingEvent) -> bool:
    stmt = (
        select(RankedWriteAudit.id)
        .where(
            RankedWriteAudit.league_id == event.league_id,
            RankedWriteAudit.game_id == event.game_id,
            RankedWriteAudit.human_player_id == event.human_player_id,
            RankedWriteAudit.decision == "APPLIED",
            RankedWriteAudit.reason == RankedEligibilityReason.ELIGIBLE.value,
            RankedWriteAudit.flag_enabled.is_(True),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


__all__ = [
    "RankedAuditSummary",
    "audit_ranked_human_ratings",
]
