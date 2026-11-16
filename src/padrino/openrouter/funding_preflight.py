"""Atomic custom-lobby funding quotes and promotional-credit reservations.

This impure boundary translates frozen lobby selections, immutable catalog
entries, and the archived rules snapshot into the pure cost/funding policies.
It deliberately runs before ``Game`` creation: a platform-funded table holds
its whole-game amount in the player ledger first, while a BYOK table records an
explicit maximum-spend confirmation without touching promotional credit.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.clock import canonical_json as _canonical_json
from padrino.core.cost_estimator import (
    AiSeatSelection,
    CatalogTokenPrices,
    ContextGrowthBounds,
    CostEstimateReason,
    CostEstimationPolicy,
    OutputAndReasoningBounds,
    WholeGameCostEstimate,
    estimate_whole_game_cost,
)
from padrino.core.enums import FundingSource, LobbySeatKind
from padrino.core.funding_policy import (
    ActiveCredentialFacts,
    AiSeatFundingFacts,
    CustomFundingFacts,
    FundingMode,
    FundingPolicyReason,
    evaluate_custom_funding,
)
from padrino.core.rulesets.snapshot import GameRulesSnapshot
from padrino.db.models import (
    CatalogSnapshot,
    Lobby,
    LobbySeat,
    ModelCatalogEntry,
    Principal,
    PromotionalCreditReservation,
    ProviderCredential,
)
from padrino.db.repositories import promotional_credit_ledger
from padrino.db.rules_snapshots import runtime_ruleset_from_pair
from padrino.economics.completion_reserve import (
    completion_reserve_global_breaker_open,
    prepare_completion_reserve_cursors,
)
from padrino.openrouter.lobby_model_selection import FrozenLobbyModelSelection
from padrino.settings import Settings

_OPENROUTER_PROVIDER = "openrouter"
_QUOTE_SCHEMA = "custom-funding-quote-v1"


class FundingPreflightReason(StrEnum):
    """Stable rejection tokens owned by the funding admission shell."""

    LEGACY_UNFUNDED_LOBBY = "legacy_unfunded_lobby"
    FUNDING_SNAPSHOT_INVALID = "funding_snapshot_invalid"
    FUNDING_SOURCE_MIXED = "funding_source_mixed"
    FUNDING_CONFIRMATION_REQUIRED = "funding_confirmation_required"
    FUNDING_QUOTE_STALE = "funding_quote_stale"
    BYOK_MAXIMUM_CONFIRMATION_REQUIRED = "byok_maximum_confirmation_required"
    BYOK_MAXIMUM_BELOW_ESTIMATE = "byok_maximum_below_estimate"
    COMPLETION_RESERVE_BREAKER_OPEN = "completion_reserve_breaker_open"


class FundingPreflightError(ValueError):
    """Raised with a stable, player-safe funding refusal token."""

    def __init__(self, reason: str | FundingPreflightReason | FundingPolicyReason) -> None:
        self.reason = reason.value if isinstance(reason, StrEnum) else str(reason)
        super().__init__(self.reason)


@dataclass(frozen=True, slots=True)
class FundingQuote:
    """One immutable calculation over a lobby's frozen funding configuration."""

    funded: bool
    allowed: bool
    source: FundingSource | None
    reason: str
    reasons: tuple[str, ...]
    reservation_micros: int | None
    expected_total_micros: int | None
    catalog_hash: str | None
    rules_hash: str | None
    model_hash: str | None
    funding_hash: str | None
    estimate_hash: str | None
    funding_quote_hash: str | None
    byok_max_estimate_micros: int | None
    estimate: WholeGameCostEstimate | None


@dataclass(frozen=True, slots=True)
class FundingAdmission:
    """Funding facts claimed before a game row can be materialized."""

    quote: FundingQuote
    reservation: PromotionalCreditReservation | None


@dataclass(frozen=True, slots=True)
class _FrozenSeatFundingFact:
    """The validated catalog and funding data for one configured AI slot."""

    seat_index: int
    selection: FrozenLobbyModelSelection
    entry: ModelCatalogEntry
    snapshot: CatalogSnapshot


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _max_output_tokens(selection: FrozenLobbyModelSelection) -> int:
    """Read the server-normalized output bound from one frozen selection."""
    value = selection.inference_params.get("max_tokens")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FundingPreflightError(FundingPreflightReason.FUNDING_SNAPSHOT_INVALID)
    return value


def _cost_policy(settings: Settings) -> CostEstimationPolicy:
    """Translate operator settings into the pure conservative estimator policy."""
    return CostEstimationPolicy(
        context_growth=ContextGrowthBounds(
            low_input_tokens_per_call=settings.padrino_openrouter_cost_low_input_tokens_per_call,
            expected_initial_input_tokens=(
                settings.padrino_openrouter_cost_expected_initial_input_tokens
            ),
            expected_growth_tokens_per_call=(
                settings.padrino_openrouter_cost_expected_growth_tokens_per_call
            ),
            expected_input_token_cap=settings.padrino_openrouter_cost_expected_input_token_cap,
            reservation_initial_input_tokens=(
                settings.padrino_openrouter_cost_reservation_initial_input_tokens
            ),
            reservation_growth_tokens_per_call=(
                settings.padrino_openrouter_cost_reservation_growth_tokens_per_call
            ),
            reservation_input_token_cap=(
                settings.padrino_openrouter_cost_reservation_input_token_cap
            ),
        ),
        output_and_reasoning=OutputAndReasoningBounds(
            low_output_tokens_per_call=settings.padrino_openrouter_cost_low_output_tokens_per_call,
            expected_output_tokens_per_call=(
                settings.padrino_openrouter_cost_expected_output_tokens_per_call
            ),
            low_reasoning_tokens_per_call=(
                settings.padrino_openrouter_cost_low_reasoning_tokens_per_call
            ),
            expected_reasoning_tokens_per_call=(
                settings.padrino_openrouter_cost_expected_reasoning_tokens_per_call
            ),
            reservation_reasoning_tokens_per_call=(
                settings.padrino_openrouter_cost_reservation_reasoning_tokens_per_call
            ),
        ),
        safety_margin_bps=settings.padrino_openrouter_cost_safety_margin_bps,
        max_platform_price_micros_per_million=(
            settings.padrino_openrouter_platform_max_price_micros_per_million
        ),
        max_platform_reservation_micros=(
            settings.padrino_openrouter_platform_max_reservation_micros
        ),
    )


async def _frozen_ai_seat_facts(
    session: AsyncSession,
    *,
    lobby: Lobby,
    seats: list[LobbySeat],
) -> tuple[_FrozenSeatFundingFact, ...] | None:
    """Load only immutable catalog rows named by every frozen AI selection.

    Older curated lobbies carry no selection snapshot and remain outside the
    promotional/BYOK funding lane. Once any selection exists, every AI slot
    must have one; a mixed legacy/frozen table would make the quote incomplete.
    """
    ai_seats = [seat for seat in seats if seat.seat_kind == LobbySeatKind.AI.value]
    if not ai_seats or not any(seat.model_selection is not None for seat in ai_seats):
        return None
    if any(seat.model_selection is None for seat in ai_seats):
        raise FundingPreflightError(FundingPreflightReason.FUNDING_SNAPSHOT_INVALID)

    facts: list[_FrozenSeatFundingFact] = []
    for seat in sorted(ai_seats, key=lambda item: item.seat_index):
        assert seat.model_selection is not None
        try:
            selection = FrozenLobbyModelSelection.model_validate(seat.model_selection)
        except ValueError as exc:
            raise FundingPreflightError(FundingPreflightReason.FUNDING_SNAPSHOT_INVALID) from exc
        snapshot = await session.get(CatalogSnapshot, selection.catalog_snapshot_id)
        entry = await session.scalar(
            select(ModelCatalogEntry).where(
                ModelCatalogEntry.id == selection.catalog_entry_id,
                ModelCatalogEntry.catalog_snapshot_id == selection.catalog_snapshot_id,
            )
        )
        if (
            snapshot is None
            or snapshot.status != "COMPLETE"
            or snapshot.content_hash != selection.catalog_content_hash
            or entry is None
            or entry.canonical_slug != selection.canonical_slug
        ):
            raise FundingPreflightError(FundingPreflightReason.FUNDING_SNAPSHOT_INVALID)
        facts.append(
            _FrozenSeatFundingFact(
                seat_index=seat.seat_index,
                selection=selection,
                entry=entry,
                snapshot=snapshot,
            )
        )
    return tuple(facts)


def _aggregate_hashes(
    facts: tuple[_FrozenSeatFundingFact, ...],
    *,
    rules_hash: str,
    policy: CostEstimationPolicy,
) -> tuple[str, str, str, str]:
    """Bind model, catalog, rules, and funding facts into quote identifiers."""
    model_hash = _hash(
        {
            "schema": "custom-funding-models-v1",
            "seats": [
                {
                    "seat_index": fact.seat_index,
                    "selection": fact.selection.model_dump(mode="json"),
                }
                for fact in facts
            ],
        }
    )
    catalog_hash = _hash(
        {
            "schema": "custom-funding-catalog-v1",
            "entries": [
                {
                    "seat_index": fact.seat_index,
                    "snapshot_id": str(fact.snapshot.id),
                    "content_hash": fact.snapshot.content_hash,
                    "entry_id": str(fact.entry.id),
                    "canonical_slug": fact.entry.canonical_slug,
                    "pricing_strings": fact.entry.pricing_strings,
                }
                for fact in facts
            ],
        }
    )
    funding_hash = _hash(
        {
            "schema": "custom-funding-source-v1",
            "seats": [
                {
                    "seat_index": fact.seat_index,
                    "source": fact.selection.funding_source,
                    "owner_ref": fact.selection.funding_owner_ref,
                }
                for fact in facts
            ],
        }
    )
    policy_hash = _hash(
        {
            "schema": "custom-funding-cost-policy-v1",
            "policy": {
                "context_growth": {
                    "low_input_tokens_per_call": policy.context_growth.low_input_tokens_per_call,
                    "expected_initial_input_tokens": (
                        policy.context_growth.expected_initial_input_tokens
                    ),
                    "expected_growth_tokens_per_call": (
                        policy.context_growth.expected_growth_tokens_per_call
                    ),
                    "expected_input_token_cap": policy.context_growth.expected_input_token_cap,
                    "reservation_initial_input_tokens": (
                        policy.context_growth.reservation_initial_input_tokens
                    ),
                    "reservation_growth_tokens_per_call": (
                        policy.context_growth.reservation_growth_tokens_per_call
                    ),
                    "reservation_input_token_cap": (
                        policy.context_growth.reservation_input_token_cap
                    ),
                },
                "output_and_reasoning": {
                    "low_output_tokens_per_call": (
                        policy.output_and_reasoning.low_output_tokens_per_call
                    ),
                    "expected_output_tokens_per_call": (
                        policy.output_and_reasoning.expected_output_tokens_per_call
                    ),
                    "low_reasoning_tokens_per_call": (
                        policy.output_and_reasoning.low_reasoning_tokens_per_call
                    ),
                    "expected_reasoning_tokens_per_call": (
                        policy.output_and_reasoning.expected_reasoning_tokens_per_call
                    ),
                    "reservation_reasoning_tokens_per_call": (
                        policy.output_and_reasoning.reservation_reasoning_tokens_per_call
                    ),
                },
                "safety_margin_bps": policy.safety_margin_bps,
                "max_platform_price_micros_per_million": (
                    policy.max_platform_price_micros_per_million
                ),
                "max_platform_reservation_micros": policy.max_platform_reservation_micros,
            },
        }
    )
    return model_hash, catalog_hash, funding_hash, policy_hash


def _quote_reasons(
    *,
    estimate: WholeGameCostEstimate,
    funding_reason: FundingPolicyReason,
    confirmation_reason: FundingPreflightReason | None,
) -> tuple[str, ...]:
    """Return deterministic machine tokens, retaining BYOK-required evidence."""
    reasons = [reason.value for reason in estimate.reasons]
    if funding_reason.value not in reasons:
        reasons.append(funding_reason.value)
    if confirmation_reason is not None and confirmation_reason.value not in reasons:
        reasons.append(confirmation_reason.value)
    return tuple(reasons)


def _primary_refusal_reason(
    *,
    estimate: WholeGameCostEstimate,
    funding_reason: FundingPolicyReason,
    confirmation_reason: FundingPreflightReason | None,
) -> str:
    """Prefer a direct price/BYOK explanation over a generic policy denial."""
    if confirmation_reason is not None:
        return confirmation_reason.value
    if CostEstimateReason.BYOK_REQUIRED in estimate.reasons:
        return CostEstimateReason.BYOK_REQUIRED.value
    if estimate.reasons:
        return estimate.reasons[0].value
    return funding_reason.value


async def _active_host_credential(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> ProviderCredential | None:
    return cast(
        ProviderCredential | None,
        await session.scalar(
            select(ProviderCredential)
            .where(
                ProviderCredential.principal_id == principal_id,
                ProviderCredential.provider == _OPENROUTER_PROVIDER,
                ProviderCredential.revoked_at.is_(None),
            )
            .limit(1)
        ),
    )


async def build_lobby_funding_quote(
    session: AsyncSession,
    *,
    lobby: Lobby,
    settings: Settings,
    byok_max_estimate_micros: int | None = None,
) -> FundingQuote:
    """Resolve one current, secret-free funding quote from frozen lobby facts."""
    seats = await session.scalars(
        select(LobbySeat).where(LobbySeat.lobby_id == lobby.id).order_by(LobbySeat.seat_index)
    )
    facts = await _frozen_ai_seat_facts(session, lobby=lobby, seats=list(seats))
    if facts is None:
        return FundingQuote(
            funded=False,
            allowed=True,
            source=None,
            reason=FundingPreflightReason.LEGACY_UNFUNDED_LOBBY.value,
            reasons=(FundingPreflightReason.LEGACY_UNFUNDED_LOBBY.value,),
            reservation_micros=None,
            expected_total_micros=None,
            catalog_hash=None,
            rules_hash=None,
            model_hash=None,
            funding_hash=None,
            estimate_hash=None,
            funding_quote_hash=None,
            byok_max_estimate_micros=None,
            estimate=None,
        )

    sources = {fact.selection.funding_source for fact in facts}
    if len(sources) != 1:
        raise FundingPreflightError(FundingPreflightReason.FUNDING_SOURCE_MIXED)
    selected_source = next(iter(sources))
    source = FundingSource.PLATFORM if selected_source == "platform" else FundingSource.BYOK_OWNER
    if lobby.rules_snapshot_json is None or lobby.rules_snapshot_hash is None:
        raise FundingPreflightError(FundingPreflightReason.FUNDING_SNAPSHOT_INVALID)
    try:
        runtime_ruleset_from_pair(
            ruleset_id=lobby.ruleset_id,
            rules_snapshot_json=lobby.rules_snapshot_json,
            rules_snapshot_hash=lobby.rules_snapshot_hash,
        )
        rules_snapshot = GameRulesSnapshot.model_validate(lobby.rules_snapshot_json)
    except (ValueError, TypeError) as exc:
        raise FundingPreflightError(FundingPreflightReason.FUNDING_SNAPSHOT_INVALID) from exc

    policy = _cost_policy(settings)
    model_hash, catalog_hash, funding_hash, policy_hash = _aggregate_hashes(
        facts,
        rules_hash=lobby.rules_snapshot_hash,
        policy=policy,
    )
    estimate = estimate_whole_game_cost(
        rules_snapshot=rules_snapshot,
        ai_seat_selections=tuple(
            AiSeatSelection(
                seat_index=fact.seat_index,
                canonical_slug=fact.selection.canonical_slug,
                max_output_tokens=_max_output_tokens(fact.selection),
                catalog_token_prices=CatalogTokenPrices(
                    prompt_usd_per_token=fact.entry.pricing_strings.get("prompt"),
                    completion_usd_per_token=fact.entry.pricing_strings.get("completion"),
                ),
                funding_source=fact.selection.funding_source,
            )
            for fact in facts
        ),
        catalog_content_hash=catalog_hash,
        policy=policy,
    )
    estimate_hash = _hash(
        {
            "schema": _QUOTE_SCHEMA,
            "model_hash": model_hash,
            "catalog_hash": catalog_hash,
            "rules_hash": lobby.rules_snapshot_hash,
            "funding_hash": funding_hash,
            "policy_hash": policy_hash,
            "reservation_total_micros": estimate.reservation_total_micros,
            "expected_total_micros": estimate.expected_total_micros,
            "reasons": [reason.value for reason in estimate.reasons],
        }
    )
    maximum = byok_max_estimate_micros if source is FundingSource.BYOK_OWNER else None
    confirmation_reason: FundingPreflightReason | None = None
    if source is FundingSource.BYOK_OWNER:
        if (
            maximum is None
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or maximum < 0
        ):
            confirmation_reason = FundingPreflightReason.BYOK_MAXIMUM_CONFIRMATION_REQUIRED
        elif (
            estimate.reservation_total_micros is not None
            and maximum < estimate.reservation_total_micros
        ):
            confirmation_reason = FundingPreflightReason.BYOK_MAXIMUM_BELOW_ESTIMATE

    available = None
    if source is FundingSource.PLATFORM:
        totals = await promotional_credit_ledger.get_credit_totals(
            session,
            principal_id=lobby.host_principal_id,
        )
        available = totals.available_micros
    credential = (
        await _active_host_credential(session, principal_id=lobby.host_principal_id)
        if source is FundingSource.BYOK_OWNER
        else None
    )
    decision = evaluate_custom_funding(
        CustomFundingFacts(
            mode=FundingMode.CUSTOM,
            requested_source=source,
            lobby_host_principal_id=str(lobby.host_principal_id),
            ai_seats=tuple(
                AiSeatFundingFacts(
                    platform_eligible=seat.platform_eligible,
                    byok_eligible=seat.byok_eligible,
                )
                for seat in estimate.per_seat
            ),
            reservation_micros=estimate.reservation_total_micros,
            available_promotional_micros=available,
            credential=ActiveCredentialFacts(
                active=credential is not None,
                owner_principal_id=str(credential.principal_id) if credential is not None else None,
            )
            if source is FundingSource.BYOK_OWNER
            else None,
        )
    )
    admission_reason = confirmation_reason
    if source is FundingSource.PLATFORM and await completion_reserve_global_breaker_open(
        session,
        global_cap_micros=settings.padrino_openrouter_completion_reserve_global_micros,
    ):
        admission_reason = FundingPreflightReason.COMPLETION_RESERVE_BREAKER_OPEN
    allowed = decision.allowed and admission_reason is None
    quote_hash = _hash(
        {
            "schema": _QUOTE_SCHEMA,
            "estimate_hash": estimate_hash,
            "model_hash": model_hash,
            "catalog_hash": catalog_hash,
            "rules_hash": lobby.rules_snapshot_hash,
            "funding_hash": funding_hash,
            "byok_max_estimate_micros": maximum,
        }
    )
    reason = (
        FundingPolicyReason.FUNDING_ADMITTED.value
        if allowed
        else _primary_refusal_reason(
            estimate=estimate,
            funding_reason=decision.reason,
            confirmation_reason=admission_reason,
        )
    )
    return FundingQuote(
        funded=True,
        allowed=allowed,
        source=source,
        reason=reason,
        reasons=_quote_reasons(
            estimate=estimate,
            funding_reason=decision.reason,
            confirmation_reason=admission_reason,
        ),
        # Show the conservative platform hold even when the wallet cannot
        # currently afford it. The subsequent owner-locked admission still
        # relies on ``decision.allowed`` before it can create that reservation.
        reservation_micros=(
            estimate.reservation_total_micros if source is FundingSource.PLATFORM else None
        ),
        expected_total_micros=estimate.expected_total_micros,
        catalog_hash=catalog_hash,
        rules_hash=lobby.rules_snapshot_hash,
        model_hash=model_hash,
        funding_hash=funding_hash,
        estimate_hash=estimate_hash,
        funding_quote_hash=quote_hash,
        byok_max_estimate_micros=maximum,
        estimate=estimate,
    )


async def admit_lobby_funding_before_game(
    session: AsyncSession,
    *,
    lobby: Lobby,
    settings: Settings,
    funding_quote_hash: str | None,
    byok_max_estimate_micros: int | None,
    now: datetime,
) -> FundingAdmission:
    """Recompute, confirm, and atomically reserve funding before game creation."""
    initial_quote = await build_lobby_funding_quote(
        session,
        lobby=lobby,
        settings=settings,
        byok_max_estimate_micros=byok_max_estimate_micros,
    )
    if not initial_quote.funded:
        return FundingAdmission(quote=initial_quote, reservation=None)
    if funding_quote_hash is None:
        raise FundingPreflightError(FundingPreflightReason.FUNDING_CONFIRMATION_REQUIRED)

    # A principal-row lock serializes all wallet admission reads and writes for
    # one owner on Postgres. SQLite follows the same transaction ordering in
    # tests; its writer lock still makes the subsequent ledger insertion atomic.
    principal = await session.scalar(
        select(Principal).where(Principal.id == lobby.host_principal_id).with_for_update()
    )
    if principal is None:
        raise FundingPreflightError(FundingPreflightReason.FUNDING_SNAPSHOT_INVALID)
    quote = await build_lobby_funding_quote(
        session,
        lobby=lobby,
        settings=settings,
        byok_max_estimate_micros=byok_max_estimate_micros,
    )
    if quote.funding_quote_hash != funding_quote_hash:
        raise FundingPreflightError(FundingPreflightReason.FUNDING_QUOTE_STALE)
    if not quote.allowed:
        raise FundingPreflightError(quote.reason)
    if quote.source is FundingSource.BYOK_OWNER:
        return FundingAdmission(quote=quote, reservation=None)
    if quote.source is not FundingSource.PLATFORM:
        raise FundingPreflightError(FundingPreflightReason.FUNDING_SNAPSHOT_INVALID)
    assert quote.reservation_micros is not None
    assert quote.estimate_hash is not None
    assert quote.catalog_hash is not None
    assert quote.rules_hash is not None
    reservation, _ = await promotional_credit_ledger.create_or_get_credit_reservation(
        session,
        principal_id=lobby.host_principal_id,
        lobby_id=lobby.id,
        game_id=None,
        estimate_hash=quote.estimate_hash,
        catalog_hash=quote.catalog_hash,
        rules_hash=quote.rules_hash,
        reserved_micros=quote.reservation_micros,
        idempotency_key=f"custom-funding:reservation:{lobby.id}:{quote.estimate_hash}",
        now=now,
        completion_reserve_per_game_cap_micros=(
            settings.padrino_openrouter_completion_reserve_per_game_micros
        ),
        completion_reserve_unfunded_policy=(
            settings.padrino_openrouter_completion_reserve_unfunded_policy
        ),
    )
    if quote.reservation_micros:
        await promotional_credit_ledger.append_credit_entry(
            session,
            principal_id=lobby.host_principal_id,
            reservation_id=reservation.id,
            game_id=None,
            entry_type=promotional_credit_ledger.CreditLedgerEntryType.RESERVATION,
            amount_micros=-quote.reservation_micros,
            idempotency_key=f"custom-funding:hold:{lobby.id}:{quote.estimate_hash}",
            reason="custom_game_admission",
            now=now,
        )
    return FundingAdmission(quote=quote, reservation=reservation)


async def bind_funding_reservation_to_game(
    session: AsyncSession,
    *,
    reservation: PromotionalCreditReservation | None,
    game_id: uuid.UUID,
    now: datetime,
) -> None:
    """Bind a just-admitted mutable reservation to its newly-created game."""
    if reservation is None:
        return
    if reservation.game_id is not None and reservation.game_id != game_id:
        raise FundingPreflightError(FundingPreflightReason.FUNDING_SNAPSHOT_INVALID)
    reservation.game_id = game_id
    if (
        reservation.completion_reserve_per_game_cap_micros is not None
        and reservation.completion_reserve_unfunded_policy is not None
    ):
        await prepare_completion_reserve_cursors(session, game_id=game_id, now=now)
    await session.flush()


__all__ = [
    "FundingAdmission",
    "FundingPreflightError",
    "FundingPreflightReason",
    "FundingQuote",
    "admit_lobby_funding_before_game",
    "bind_funding_reservation_to_game",
    "build_lobby_funding_quote",
]
