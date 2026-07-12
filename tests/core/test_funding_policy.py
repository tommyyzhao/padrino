"""US-352 coverage for pure funding and promotional-credit decisions."""

from __future__ import annotations

import pytest

from padrino.core.enums import FundingSource
from padrino.core.funding_policy import (
    PROMOTIONAL_GRANT_MICROS,
    ActiveCredentialFacts,
    AiSeatFundingFacts,
    CompletionReserveFacts,
    CustomFundingFacts,
    FundingMode,
    FundingPolicyReason,
    PromotionalGrantFacts,
    evaluate_completion_reserve,
    evaluate_custom_funding,
    evaluate_promotional_grant,
)


def _platform_facts(
    *,
    mode: FundingMode | None = FundingMode.CUSTOM,
    requested_source: FundingSource = FundingSource.PLATFORM,
    ai_seats: tuple[AiSeatFundingFacts, ...] = (
        AiSeatFundingFacts(platform_eligible=True, byok_eligible=True),
    ),
    reservation_micros: int | None = 1_250,
    available_promotional_micros: int | None = 1_250,
    credential: ActiveCredentialFacts | None = None,
) -> CustomFundingFacts:
    """Return a complete platform-funded custom-lobby funding request."""
    return CustomFundingFacts(
        mode=mode,
        requested_source=requested_source,
        lobby_host_principal_id="host-1",
        ai_seats=ai_seats,
        reservation_micros=reservation_micros,
        available_promotional_micros=available_promotional_micros,
        credential=credential,
    )


def test_verified_non_deleted_account_receives_exactly_one_five_dollar_grant() -> None:
    decision = evaluate_promotional_grant(
        PromotionalGrantFacts(
            principal_kind="account",
            email_verified=True,
            deleted=False,
            promotional_grant_issued=False,
        )
    )

    assert PROMOTIONAL_GRANT_MICROS == 5_000_000
    assert decision.allowed is True
    assert decision.grant_micros == 5_000_000
    assert decision.reason is FundingPolicyReason.PROMOTIONAL_GRANT_ALLOWED


@pytest.mark.parametrize(
    ("facts", "reason"),
    [
        (None, FundingPolicyReason.PROMOTIONAL_ACCOUNT_REQUIRED),
        (
            PromotionalGrantFacts(
                principal_kind="guest",
                email_verified=True,
                deleted=False,
                promotional_grant_issued=False,
            ),
            FundingPolicyReason.PROMOTIONAL_ACCOUNT_REQUIRED,
        ),
        (
            PromotionalGrantFacts(
                principal_kind="account",
                email_verified=None,
                deleted=False,
                promotional_grant_issued=False,
            ),
            FundingPolicyReason.PROMOTIONAL_EMAIL_UNVERIFIED,
        ),
        (
            PromotionalGrantFacts(
                principal_kind="account",
                email_verified=True,
                deleted=True,
                promotional_grant_issued=False,
            ),
            FundingPolicyReason.PROMOTIONAL_ACCOUNT_DELETED,
        ),
        (
            PromotionalGrantFacts(
                principal_kind="account",
                email_verified=True,
                deleted=False,
                promotional_grant_issued=True,
            ),
            FundingPolicyReason.PROMOTIONAL_GRANT_ALREADY_ISSUED,
        ),
    ],
)
def test_promotional_grant_fails_closed_for_ineligible_or_unknown_account_facts(
    facts: PromotionalGrantFacts | None,
    reason: FundingPolicyReason,
) -> None:
    decision = evaluate_promotional_grant(facts)

    assert decision.allowed is False
    assert decision.grant_micros == 0
    assert decision.reason is reason


def test_platform_custom_admission_reserves_only_available_promotional_credit() -> None:
    decision = evaluate_custom_funding(_platform_facts())

    assert decision.allowed is True
    assert decision.source is FundingSource.PLATFORM
    assert decision.payer_principal_id == "host-1"
    assert decision.promotional_reservation_micros == 1_250
    assert decision.promotional_balance_checked is True
    assert decision.completion_reserve_owner == "operator"
    assert decision.reason is FundingPolicyReason.FUNDING_ADMITTED


def test_custom_host_is_the_single_payer_for_every_ai_seat_and_sponsors_are_not_selectable() -> (
    None
):
    funded = evaluate_custom_funding(
        _platform_facts(
            ai_seats=(
                AiSeatFundingFacts(platform_eligible=True, byok_eligible=True),
                AiSeatFundingFacts(platform_eligible=True, byok_eligible=True),
            )
        )
    )
    refused = evaluate_custom_funding(_platform_facts(requested_source=FundingSource.SPONSOR_POOL))

    assert funded.allowed is True
    assert funded.payer_principal_id == "host-1"
    assert refused.allowed is False
    assert refused.reason is FundingPolicyReason.FUNDING_SOURCE_NOT_SELECTABLE


@pytest.mark.parametrize(
    ("facts", "reason"),
    [
        (
            _platform_facts(
                ai_seats=(AiSeatFundingFacts(platform_eligible=None, byok_eligible=True),)
            ),
            FundingPolicyReason.PLATFORM_MODEL_INELIGIBLE,
        ),
        (
            _platform_facts(available_promotional_micros=1_249),
            FundingPolicyReason.INSUFFICIENT_PROMOTIONAL_CREDIT,
        ),
        (
            _platform_facts(reservation_micros=None),
            FundingPolicyReason.PLATFORM_RESERVATION_UNKNOWN,
        ),
    ],
)
def test_platform_admission_fails_closed_for_model_or_reservation_facts(
    facts: CustomFundingFacts,
    reason: FundingPolicyReason,
) -> None:
    decision = evaluate_custom_funding(facts)

    assert decision.allowed is False
    assert decision.source is FundingSource.PLATFORM
    assert decision.promotional_reservation_micros == 0
    assert decision.reason is reason


def test_byok_requires_an_active_host_owned_credential_and_never_reads_promo_balance() -> None:
    decision = evaluate_custom_funding(
        _platform_facts(
            requested_source=FundingSource.BYOK_OWNER,
            available_promotional_micros=None,
            credential=ActiveCredentialFacts(active=True, owner_principal_id="host-1"),
        )
    )

    assert decision.allowed is True
    assert decision.source is FundingSource.BYOK_OWNER
    assert decision.payer_principal_id == "host-1"
    assert decision.promotional_balance_checked is False
    assert decision.promotional_reservation_micros == 0
    assert decision.reason is FundingPolicyReason.FUNDING_ADMITTED


@pytest.mark.parametrize(
    ("credential", "reason"),
    [
        (None, FundingPolicyReason.BYOK_CREDENTIAL_REQUIRED),
        (
            ActiveCredentialFacts(active=None, owner_principal_id="host-1"),
            FundingPolicyReason.BYOK_CREDENTIAL_REQUIRED,
        ),
        (
            ActiveCredentialFacts(active=True, owner_principal_id="another-host"),
            FundingPolicyReason.BYOK_CREDENTIAL_OWNER_MISMATCH,
        ),
    ],
)
def test_byok_credential_facts_fail_closed(
    credential: ActiveCredentialFacts | None,
    reason: FundingPolicyReason,
) -> None:
    decision = evaluate_custom_funding(
        _platform_facts(
            requested_source=FundingSource.BYOK_OWNER,
            credential=credential,
        )
    )

    assert decision.allowed is False
    assert decision.promotional_balance_checked is False
    assert decision.promotional_reservation_micros == 0
    assert decision.reason is reason


@pytest.mark.parametrize("mode", (FundingMode.RANKED, FundingMode.SCIENTIFIC, None))
def test_only_custom_lobbies_can_select_a_player_funding_source(mode: FundingMode | None) -> None:
    decision = evaluate_custom_funding(_platform_facts(mode=mode))

    assert decision.allowed is False
    assert decision.source is None
    assert decision.payer_principal_id is None
    assert decision.reason is FundingPolicyReason.SERVER_OWNED_FUNDING_MODE


def test_completion_reserve_covers_only_a_platform_estimate_miss_without_overdrawing_player() -> (
    None
):
    decision = evaluate_completion_reserve(
        CompletionReserveFacts(
            source=FundingSource.PLATFORM,
            call_cost_micros=11,
            promotional_reservation_remaining_micros=7,
            operator_completion_reserve_remaining_micros=4,
        )
    )

    assert decision.allowed is True
    assert decision.promotional_charge_micros == 7
    assert decision.operator_completion_charge_micros == 4
    assert decision.unfunded_micros == 0
    assert decision.reason is FundingPolicyReason.COMPLETION_FUNDED


def test_completion_reserve_is_bounded_operator_money_and_byok_never_uses_it() -> None:
    platform = evaluate_completion_reserve(
        CompletionReserveFacts(
            source=FundingSource.PLATFORM,
            call_cost_micros=11,
            promotional_reservation_remaining_micros=7,
            operator_completion_reserve_remaining_micros=3,
        )
    )
    byok = evaluate_completion_reserve(CompletionReserveFacts(source=FundingSource.BYOK_OWNER))

    assert platform.allowed is False
    assert platform.promotional_charge_micros == 7
    assert platform.operator_completion_charge_micros == 3
    assert platform.unfunded_micros == 1
    assert platform.reason is FundingPolicyReason.OPERATOR_COMPLETION_RESERVE_EXHAUSTED
    assert byok.allowed is True
    assert byok.promotional_charge_micros == 0
    assert byok.operator_completion_charge_micros == 0
    assert byok.reason is FundingPolicyReason.BYOK_OUTSIDE_PROMOTIONAL_LEDGER
