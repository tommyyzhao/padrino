"""Impure ranked-eligibility fact resolver for API admission paths.

The pure predicate under :mod:`padrino.core.ranked_eligibility` owns decision
ordering. This shell reads database and settings facts, then returns the plain
input object consumed by that predicate.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api import human_consent
from padrino.core.enums import LeagueKind
from padrino.core.ranked_eligibility import RankedConsentFacts, RankedEligibilityInputs
from padrino.db.models import OAuthIdentity, Principal
from padrino.db.repositories import ranked_bans
from padrino.settings import Settings


async def resolve_ranked_eligibility(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    settings: Settings,
    now: datetime | None = None,
) -> RankedEligibilityInputs:
    """Resolve server-authoritative ranked facts for one human principal.

    This resolver performs reads only. Guests and soft-deleted principals fail
    closed before consent, email, or ban facts can accidentally upgrade them.
    """

    enabled = getattr(settings, "padrino_ranked_human_rating_enabled", False) is True
    principal = await session.get(Principal, principal_id)
    if principal is None or principal.deleted_at is not None:
        return _facts(
            ranked_human_rating_enabled=enabled,
            principal_kind=None,
            email_verified=False,
            has_current_base_consent=False,
            has_current_ranked_consent=False,
            ranked_banned=False,
        )

    if principal.kind != "account":
        return _facts(
            ranked_human_rating_enabled=enabled,
            principal_kind=principal.kind,
            email_verified=False,
            has_current_base_consent=False,
            has_current_ranked_consent=False,
            ranked_banned=False,
        )

    resolved_now = now if now is not None else datetime.now(UTC)
    has_base_consent = await human_consent.has_current_consent(
        session,
        subject_principal_id=principal.id,
        settings=settings,
    )
    has_ranked_consent = (
        await human_consent.has_current_ranked_consent(
            session,
            subject_principal_id=principal.id,
            settings=settings,
        )
        if has_base_consent
        else False
    )
    return _facts(
        ranked_human_rating_enabled=enabled,
        principal_kind=principal.kind,
        email_verified=await _has_verified_oauth_email(session, principal.id),
        has_current_base_consent=has_base_consent,
        has_current_ranked_consent=has_ranked_consent,
        ranked_banned=await ranked_bans.is_principal_ranked_banned(
            session,
            principal_id=principal.id,
            now=resolved_now,
        ),
    )


async def _has_verified_oauth_email(session: AsyncSession, principal_id: uuid.UUID) -> bool:
    stmt = (
        select(OAuthIdentity.id)
        .where(
            OAuthIdentity.principal_id == principal_id,
            OAuthIdentity.email_verified.is_(True),
            OAuthIdentity.email.is_not(None),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


def _facts(
    *,
    ranked_human_rating_enabled: bool,
    principal_kind: str | None,
    email_verified: bool,
    has_current_base_consent: bool,
    has_current_ranked_consent: bool,
    ranked_banned: bool,
) -> RankedEligibilityInputs:
    return RankedEligibilityInputs(
        ranked_human_rating_enabled=ranked_human_rating_enabled,
        principal_kind=principal_kind,
        email_verified=email_verified,
        has_current_base_consent=has_current_base_consent,
        ranked_consent=RankedConsentFacts(has_current_ranked_consent=has_current_ranked_consent),
        ranked_banned=ranked_banned,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
    )


__all__ = ["resolve_ranked_eligibility"]
