"""Pure ranked human-eligibility predicate (US-290).

The API, consent, ban, and rating-write shells resolve facts from storage and
configuration, then pass those plain facts here. This module owns only the
composition order, so it stays deterministic and side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from padrino.core.enums import LeagueKind


class RankedEligibilityReason(StrEnum):
    """Machine-readable eligibility decision tokens."""

    ELIGIBLE = "eligible"
    RANKED_DISABLED = "ranked_disabled"
    GUEST_PRINCIPAL = "guest_principal"
    EMAIL_UNVERIFIED = "email_unverified"
    BASE_CONSENT_MISSING = "base_consent_missing"
    RANKED_CONSENT_MISSING = "ranked_consent_missing"
    RANKED_BANNED = "ranked_banned"
    LEAGUE_NOT_HUMANS_INCLUDED_RANKED = "league_not_humans_included_ranked"


@dataclass(frozen=True, slots=True)
class RankedConsentFacts:
    """Resolved ranked-consent facts owned by the legal/consent shell.

    Keeping ranked consent in a typed sub-object gives later stories room to add
    purpose-segmented consent facts without changing
    :func:`evaluate_ranked_eligibility`'s signature.
    """

    has_current_ranked_consent: bool | None = None


@dataclass(frozen=True, slots=True)
class RankedEligibilityInputs:
    """Already-resolved facts consumed by the pure ranked predicate."""

    ranked_human_rating_enabled: bool | None = None
    principal_kind: str | None = None
    email_verified: bool | None = None
    has_current_base_consent: bool | None = None
    ranked_consent: RankedConsentFacts | None = None
    ranked_banned: bool | None = None
    league_kind: LeagueKind | str | None = None
    league_ranked: bool | None = None


@dataclass(frozen=True, slots=True)
class RankedDecision:
    """Ranked eligibility result with a stable reason token."""

    allowed: bool
    reason: RankedEligibilityReason


def evaluate_ranked_eligibility(facts: RankedEligibilityInputs | None) -> RankedDecision:
    """Return the server-authoritative ranked-eligibility decision.

    The predicate fails closed: ``None`` or unknown facts are ineligible, and the
    first failing precondition determines the reason token. The order is pinned
    by tests because downstream audit rows and API responses depend on it.
    """

    if facts is None:
        return _refused(RankedEligibilityReason.RANKED_DISABLED)
    if facts.ranked_human_rating_enabled is not True:
        return _refused(RankedEligibilityReason.RANKED_DISABLED)
    if facts.principal_kind != "account":
        return _refused(RankedEligibilityReason.GUEST_PRINCIPAL)
    if facts.email_verified is not True:
        return _refused(RankedEligibilityReason.EMAIL_UNVERIFIED)
    if facts.has_current_base_consent is not True:
        return _refused(RankedEligibilityReason.BASE_CONSENT_MISSING)
    if _has_current_ranked_consent(facts.ranked_consent) is not True:
        return _refused(RankedEligibilityReason.RANKED_CONSENT_MISSING)
    if facts.ranked_banned is not False:
        return _refused(RankedEligibilityReason.RANKED_BANNED)
    if not _is_humans_included_ranked(facts.league_kind, facts.league_ranked):
        return _refused(RankedEligibilityReason.LEAGUE_NOT_HUMANS_INCLUDED_RANKED)
    return RankedDecision(allowed=True, reason=RankedEligibilityReason.ELIGIBLE)


def _refused(reason: RankedEligibilityReason) -> RankedDecision:
    return RankedDecision(allowed=False, reason=reason)


def _has_current_ranked_consent(facts: RankedConsentFacts | None) -> bool:
    return facts is not None and facts.has_current_ranked_consent is True


def _is_humans_included_ranked(kind: LeagueKind | str | None, ranked: bool | None) -> bool:
    if ranked is not True:
        return False
    return kind == LeagueKind.HUMANS_INCLUDED or kind == LeagueKind.HUMANS_INCLUDED.value


__all__ = [
    "RankedConsentFacts",
    "RankedDecision",
    "RankedEligibilityInputs",
    "RankedEligibilityReason",
    "evaluate_ranked_eligibility",
]
