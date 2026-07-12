"""Pure funding, credential, and promotional-credit policy (US-352).

The database/API shells resolve account, credential, catalog, and ledger state
into the compact facts below.  This module chooses a single funding source for
a casual custom lobby and bounds how a platform-funded completion can spill
from the player's reservation into an operator-owned reserve.  It deliberately
does not know credential material, database identifiers, clocks, or providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal, TypeGuard

from padrino.core.enums import FundingSource

PROMOTIONAL_GRANT_MICROS: Final = 5_000_000
"""The one-time USD 5.00 promotional grant, represented in integer micros."""


class FundingMode(StrEnum):
    """The product lane that owns a funding decision."""

    CUSTOM = "custom"
    RANKED = "ranked"
    SCIENTIFIC = "scientific"


class FundingPolicyReason(StrEnum):
    """Stable, machine-readable outcomes from the funding policy."""

    FUNDING_ADMITTED = "funding_admitted"
    FUNDING_FACTS_MISSING = "funding_facts_missing"
    SERVER_OWNED_FUNDING_MODE = "server_owned_funding_mode"
    FUNDING_SOURCE_NOT_SELECTABLE = "funding_source_not_selectable"
    CUSTOM_HOST_REQUIRED = "custom_host_required"
    PROMOTIONAL_ACCOUNT_REQUIRED = "promotional_account_required"
    PROMOTIONAL_EMAIL_UNVERIFIED = "promotional_email_unverified"
    PROMOTIONAL_ACCOUNT_DELETED = "promotional_account_deleted"
    PROMOTIONAL_CREDIT_ABUSE_BANNED = "promotional_credit_abuse_banned"
    PROMOTIONAL_CREDIT_BAN_STATE_UNKNOWN = "promotional_credit_ban_state_unknown"
    PROMOTIONAL_GRANT_ALREADY_ISSUED = "promotional_grant_already_issued"
    PROMOTIONAL_GRANT_STATE_UNKNOWN = "promotional_grant_state_unknown"
    PROMOTIONAL_GRANT_ALLOWED = "promotional_grant_allowed"
    PLATFORM_MODEL_INELIGIBLE = "platform_model_ineligible"
    PLATFORM_RESERVATION_UNKNOWN = "platform_reservation_unknown"
    PROMOTIONAL_BALANCE_UNKNOWN = "promotional_balance_unknown"
    INSUFFICIENT_PROMOTIONAL_CREDIT = "insufficient_promotional_credit"
    BYOK_MODEL_INELIGIBLE = "byok_model_ineligible"
    BYOK_CREDENTIAL_REQUIRED = "byok_credential_required"
    BYOK_CREDENTIAL_OWNER_MISMATCH = "byok_credential_owner_mismatch"
    COMPLETION_COST_UNKNOWN = "completion_cost_unknown"
    PROMOTIONAL_RESERVATION_UNKNOWN = "promotional_reservation_unknown"
    OPERATOR_COMPLETION_RESERVE_UNKNOWN = "operator_completion_reserve_unknown"
    COMPLETION_FUNDED = "completion_funded"
    OPERATOR_COMPLETION_RESERVE_EXHAUSTED = "operator_completion_reserve_exhausted"
    BYOK_OUTSIDE_PROMOTIONAL_LEDGER = "byok_outside_promotional_ledger"


@dataclass(frozen=True, slots=True)
class PromotionalGrantFacts:
    """Resolved account state needed to decide a one-time promotional grant."""

    principal_kind: str | None = None
    email_verified: bool | None = None
    deleted: bool | None = None
    #: This is deliberately independent of ranked-ban eligibility.  The shell
    #: resolves it from operator-owned credit-abuse policy only.
    credit_abuse_banned: bool | None = False
    promotional_grant_issued: bool | None = None


@dataclass(frozen=True, slots=True)
class PromotionalGrantDecision:
    """The exact grant amount and stable eligibility result."""

    allowed: bool
    grant_micros: int
    reason: FundingPolicyReason


@dataclass(frozen=True, slots=True)
class ActiveCredentialFacts:
    """Secret-free ownership and active-state facts for one provider credential."""

    active: bool | None = None
    owner_principal_id: str | None = None


@dataclass(frozen=True, slots=True)
class AiSeatFundingFacts:
    """One selected AI model's already-derived funding eligibility facts."""

    platform_eligible: bool | None = None
    byok_eligible: bool | None = None


@dataclass(frozen=True, slots=True)
class CustomFundingFacts:
    """All externally resolved facts required to fund one custom lobby.

    ``reservation_micros`` is the conservative whole-game platform estimate.
    The policy never treats the later operator completion reserve as player
    balance, so it cannot make an otherwise unaffordable platform lobby admit.
    """

    mode: FundingMode | str | None = None
    requested_source: FundingSource | str | None = None
    lobby_host_principal_id: str | None = None
    ai_seats: tuple[AiSeatFundingFacts, ...] | None = None
    reservation_micros: int | None = None
    available_promotional_micros: int | None = None
    credential: ActiveCredentialFacts | None = None


@dataclass(frozen=True, slots=True)
class CustomFundingDecision:
    """A single source/payer decision for every AI seat in a custom lobby."""

    allowed: bool
    source: FundingSource | None
    payer_principal_id: str | None
    promotional_reservation_micros: int
    promotional_balance_checked: bool
    completion_reserve_owner: Literal["operator"] | None
    reason: FundingPolicyReason


@dataclass(frozen=True, slots=True)
class CompletionReserveFacts:
    """Per-call platform settlement facts after an admitted game starts.

    The promotional reservation is the maximum player amount still chargeable.
    The completion reserve belongs to the operator and is only considered after
    that reservation, never as an extension of player credit.
    """

    source: FundingSource | str | None = None
    call_cost_micros: int | None = None
    promotional_reservation_remaining_micros: int | None = None
    operator_completion_reserve_remaining_micros: int | None = None


@dataclass(frozen=True, slots=True)
class CompletionReserveDecision:
    """Bounded split of one platform call between player and operator money."""

    allowed: bool
    promotional_charge_micros: int
    operator_completion_charge_micros: int
    unfunded_micros: int
    reason: FundingPolicyReason


def evaluate_promotional_grant(
    facts: PromotionalGrantFacts | None,
) -> PromotionalGrantDecision:
    """Grant exactly USD 5.00 only to one verified, non-deleted account.

    Unknown state fails closed.  In particular, a guest cannot receive a grant
    merely because a shell has a verified-looking claim.
    """

    if facts is None or facts.principal_kind != "account":
        return _grant_refused(FundingPolicyReason.PROMOTIONAL_ACCOUNT_REQUIRED)
    if facts.email_verified is not True:
        return _grant_refused(FundingPolicyReason.PROMOTIONAL_EMAIL_UNVERIFIED)
    if facts.deleted is not False:
        return _grant_refused(FundingPolicyReason.PROMOTIONAL_ACCOUNT_DELETED)
    if facts.credit_abuse_banned is True:
        return _grant_refused(FundingPolicyReason.PROMOTIONAL_CREDIT_ABUSE_BANNED)
    if facts.credit_abuse_banned is not False:
        return _grant_refused(FundingPolicyReason.PROMOTIONAL_CREDIT_BAN_STATE_UNKNOWN)
    if facts.promotional_grant_issued is True:
        return _grant_refused(FundingPolicyReason.PROMOTIONAL_GRANT_ALREADY_ISSUED)
    if facts.promotional_grant_issued is not False:
        return _grant_refused(FundingPolicyReason.PROMOTIONAL_GRANT_STATE_UNKNOWN)
    return PromotionalGrantDecision(
        allowed=True,
        grant_micros=PROMOTIONAL_GRANT_MICROS,
        reason=FundingPolicyReason.PROMOTIONAL_GRANT_ALLOWED,
    )


def evaluate_custom_funding(facts: CustomFundingFacts | None) -> CustomFundingDecision:
    """Choose exactly one allowed source for a custom lobby, or deny it.

    Custom lobbies can select only platform promotional credit or a credential
    owned by their host.  Ranked/scientific modes are intentionally refused:
    their server-owned funding cannot be selected through this casual policy.
    """

    if facts is None:
        return _funding_refused(FundingPolicyReason.FUNDING_FACTS_MISSING)
    if facts.mode != FundingMode.CUSTOM:
        return _funding_refused(FundingPolicyReason.SERVER_OWNED_FUNDING_MODE)
    host = _nonempty_identifier(facts.lobby_host_principal_id)
    if host is None:
        return _funding_refused(FundingPolicyReason.CUSTOM_HOST_REQUIRED)
    source = _selected_source(facts.requested_source)
    if source is None:
        return _funding_refused(FundingPolicyReason.FUNDING_SOURCE_NOT_SELECTABLE)
    if facts.ai_seats is None:
        return _funding_refused(FundingPolicyReason.FUNDING_FACTS_MISSING, source=source)
    if source is FundingSource.PLATFORM:
        return _evaluate_platform_funding(facts, host=host)
    return _evaluate_byok_funding(facts, host=host)


def evaluate_completion_reserve(
    facts: CompletionReserveFacts | None,
) -> CompletionReserveDecision:
    """Bound one platform call without charging beyond the player reservation.

    A BYOK call is outside the promotional ledger and never reads either
    promotional or completion-reserve facts.  For platform calls, a shortfall
    remains explicitly unfunded once the operator reserve is exhausted; the
    player charge remains bounded by the already-held reservation.
    """

    if facts is None:
        return _completion_refused(FundingPolicyReason.FUNDING_FACTS_MISSING)
    source = _selected_source(facts.source)
    if source is None:
        return _completion_refused(FundingPolicyReason.FUNDING_SOURCE_NOT_SELECTABLE)
    if source is FundingSource.BYOK_OWNER:
        return CompletionReserveDecision(
            allowed=True,
            promotional_charge_micros=0,
            operator_completion_charge_micros=0,
            unfunded_micros=0,
            reason=FundingPolicyReason.BYOK_OUTSIDE_PROMOTIONAL_LEDGER,
        )
    if not _non_negative_int(facts.call_cost_micros):
        return _completion_refused(FundingPolicyReason.COMPLETION_COST_UNKNOWN)
    if not _non_negative_int(facts.promotional_reservation_remaining_micros):
        return _completion_refused(FundingPolicyReason.PROMOTIONAL_RESERVATION_UNKNOWN)
    if not _non_negative_int(facts.operator_completion_reserve_remaining_micros):
        return _completion_refused(FundingPolicyReason.OPERATOR_COMPLETION_RESERVE_UNKNOWN)

    cost = facts.call_cost_micros
    player_remaining = facts.promotional_reservation_remaining_micros
    operator_remaining = facts.operator_completion_reserve_remaining_micros
    promotional_charge = min(cost, player_remaining)
    remaining_cost = cost - promotional_charge
    operator_charge = min(remaining_cost, operator_remaining)
    unfunded = remaining_cost - operator_charge
    if unfunded:
        return CompletionReserveDecision(
            allowed=False,
            promotional_charge_micros=promotional_charge,
            operator_completion_charge_micros=operator_charge,
            unfunded_micros=unfunded,
            reason=FundingPolicyReason.OPERATOR_COMPLETION_RESERVE_EXHAUSTED,
        )
    return CompletionReserveDecision(
        allowed=True,
        promotional_charge_micros=promotional_charge,
        operator_completion_charge_micros=operator_charge,
        unfunded_micros=0,
        reason=FundingPolicyReason.COMPLETION_FUNDED,
    )


def _evaluate_platform_funding(
    facts: CustomFundingFacts,
    *,
    host: str,
) -> CustomFundingDecision:
    if any(seat.platform_eligible is not True for seat in facts.ai_seats or ()):
        return _funding_refused(
            FundingPolicyReason.PLATFORM_MODEL_INELIGIBLE, source=FundingSource.PLATFORM
        )
    if not _non_negative_int(facts.reservation_micros):
        return _funding_refused(
            FundingPolicyReason.PLATFORM_RESERVATION_UNKNOWN,
            source=FundingSource.PLATFORM,
        )
    if not _non_negative_int(facts.available_promotional_micros):
        return _funding_refused(
            FundingPolicyReason.PROMOTIONAL_BALANCE_UNKNOWN,
            source=FundingSource.PLATFORM,
        )
    if facts.reservation_micros > facts.available_promotional_micros:
        return _funding_refused(
            FundingPolicyReason.INSUFFICIENT_PROMOTIONAL_CREDIT,
            source=FundingSource.PLATFORM,
        )
    return CustomFundingDecision(
        allowed=True,
        source=FundingSource.PLATFORM,
        payer_principal_id=host,
        promotional_reservation_micros=facts.reservation_micros,
        promotional_balance_checked=True,
        completion_reserve_owner="operator",
        reason=FundingPolicyReason.FUNDING_ADMITTED,
    )


def _evaluate_byok_funding(
    facts: CustomFundingFacts,
    *,
    host: str,
) -> CustomFundingDecision:
    credential = facts.credential
    if credential is None or credential.active is not True:
        return _funding_refused(
            FundingPolicyReason.BYOK_CREDENTIAL_REQUIRED, source=FundingSource.BYOK_OWNER
        )
    if _nonempty_identifier(credential.owner_principal_id) != host:
        return _funding_refused(
            FundingPolicyReason.BYOK_CREDENTIAL_OWNER_MISMATCH,
            source=FundingSource.BYOK_OWNER,
        )
    if any(seat.byok_eligible is not True for seat in facts.ai_seats or ()):
        return _funding_refused(
            FundingPolicyReason.BYOK_MODEL_INELIGIBLE, source=FundingSource.BYOK_OWNER
        )
    return CustomFundingDecision(
        allowed=True,
        source=FundingSource.BYOK_OWNER,
        payer_principal_id=host,
        promotional_reservation_micros=0,
        promotional_balance_checked=False,
        completion_reserve_owner=None,
        reason=FundingPolicyReason.FUNDING_ADMITTED,
    )


def _grant_refused(reason: FundingPolicyReason) -> PromotionalGrantDecision:
    return PromotionalGrantDecision(allowed=False, grant_micros=0, reason=reason)


def _funding_refused(
    reason: FundingPolicyReason,
    *,
    source: FundingSource | None = None,
) -> CustomFundingDecision:
    return CustomFundingDecision(
        allowed=False,
        source=source,
        payer_principal_id=None,
        promotional_reservation_micros=0,
        promotional_balance_checked=False,
        completion_reserve_owner=None,
        reason=reason,
    )


def _completion_refused(reason: FundingPolicyReason) -> CompletionReserveDecision:
    return CompletionReserveDecision(
        allowed=False,
        promotional_charge_micros=0,
        operator_completion_charge_micros=0,
        unfunded_micros=0,
        reason=reason,
    )


def _selected_source(value: FundingSource | str | None) -> FundingSource | None:
    if (
        value is FundingSource.PLATFORM
        or value == FundingSource.PLATFORM.value
        or value == "platform"
    ):
        return FundingSource.PLATFORM
    if (
        value is FundingSource.BYOK_OWNER
        or value == FundingSource.BYOK_OWNER.value
        or value == "byok"
    ):
        return FundingSource.BYOK_OWNER
    return None


def _nonempty_identifier(value: str | None) -> str | None:
    if not isinstance(value, str) or not value or value.strip() != value:
        return None
    return value


def _non_negative_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


__all__ = [
    "PROMOTIONAL_GRANT_MICROS",
    "ActiveCredentialFacts",
    "AiSeatFundingFacts",
    "CompletionReserveDecision",
    "CompletionReserveFacts",
    "CustomFundingDecision",
    "CustomFundingFacts",
    "FundingMode",
    "FundingPolicyReason",
    "PromotionalGrantDecision",
    "PromotionalGrantFacts",
    "evaluate_completion_reserve",
    "evaluate_custom_funding",
    "evaluate_promotional_grant",
]
