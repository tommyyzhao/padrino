"""Pure conservative whole-game inference cost estimation.

The estimator deliberately accepts only frozen game/catalog facts.  It does
not inspect ORM rows, a provider, or a clock, and every monetary intermediate
is an integer number of USD micros derived from decimal strings with upward
rounding.  The impure admission shell is responsible for translating its
persisted snapshots into these compact values.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal

from padrino.core.cadence import cadence_profile
from padrino.core.enums import Faction
from padrino.core.rulesets.snapshot import GameRulesSnapshot

_USD_MICROS = Decimal(1_000_000)
_TOKENS_PER_MILLION = 1_000_000
_BASIS_POINTS = 10_000


class CostEstimateReason(StrEnum):
    """Stable fail-closed tokens for pricing and platform-policy decisions."""

    UNPRICEABLE_MODEL = "unpriceable_model"
    BYOK_REQUIRED = "byok_required"
    OVER_PLATFORM_PRICE_POLICY = "over_platform_price_policy"
    OVER_PLATFORM_RESERVATION_POLICY = "over_platform_reservation_policy"


def _require_non_negative_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name}_must_be_a_non_negative_integer")


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name}_must_be_a_positive_integer")


def _decimal_or_none(value: str | None, *, field_name: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{field_name}_must_be_a_trimmed_decimal_string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name}_must_be_a_decimal_string") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field_name}_must_be_finite_and_non_negative")
    return parsed


def _micros_for_usd(decimal_usd: Decimal) -> int:
    return int((decimal_usd * _USD_MICROS).to_integral_value(rounding=ROUND_CEILING))


def _micros_for_tokens(decimal_usd_per_token: Decimal, token_count: int) -> int:
    _require_non_negative_int(token_count, field_name="token_count")
    return _micros_for_usd(decimal_usd_per_token * Decimal(token_count))


def _micros_per_million_tokens(decimal_usd_per_token: Decimal) -> int:
    return _micros_for_tokens(decimal_usd_per_token, _TOKENS_PER_MILLION)


@dataclass(frozen=True, slots=True)
class CatalogTokenPrices:
    """Normalized decimal prices for one immutable catalog model.

    Prompt and completion prices are the only required fields.  Optional
    fixed, request, and reasoning charges default to zero and preserve the
    exact source semantics without ever using binary floats.
    """

    prompt_usd_per_token: str | None
    completion_usd_per_token: str | None
    fixed_usd_per_game: str = "0"
    request_usd_per_call: str = "0"
    reasoning_usd_per_token: str = "0"

    def __post_init__(self) -> None:
        _decimal_or_none(self.prompt_usd_per_token, field_name="prompt_usd_per_token")
        _decimal_or_none(self.completion_usd_per_token, field_name="completion_usd_per_token")
        _decimal_or_none(self.fixed_usd_per_game, field_name="fixed_usd_per_game")
        _decimal_or_none(self.request_usd_per_call, field_name="request_usd_per_call")
        _decimal_or_none(self.reasoning_usd_per_token, field_name="reasoning_usd_per_token")

    @property
    def is_priceable(self) -> bool:
        """Return whether both token prices are available for a bounded game."""
        return self.prompt_usd_per_token is not None and self.completion_usd_per_token is not None


@dataclass(frozen=True, slots=True)
class AiSeatSelection:
    """One frozen AI seat's secret-free runtime and price facts."""

    seat_index: int
    canonical_slug: str
    max_output_tokens: int
    catalog_token_prices: CatalogTokenPrices
    funding_source: Literal["platform", "byok"] = "platform"

    def __post_init__(self) -> None:
        _require_non_negative_int(self.seat_index, field_name="seat_index")
        _require_positive_int(self.max_output_tokens, field_name="max_output_tokens")
        if not self.canonical_slug or self.canonical_slug.strip() != self.canonical_slug:
            raise ValueError("canonical_slug_required")


@dataclass(frozen=True, slots=True)
class ContextGrowthBounds:
    """Injected input-context bounds measured in tokens per inference call."""

    low_input_tokens_per_call: int
    expected_initial_input_tokens: int
    expected_growth_tokens_per_call: int
    expected_input_token_cap: int
    reservation_initial_input_tokens: int
    reservation_growth_tokens_per_call: int
    reservation_input_token_cap: int

    def __post_init__(self) -> None:
        for field_name in (
            "low_input_tokens_per_call",
            "expected_initial_input_tokens",
            "expected_growth_tokens_per_call",
            "expected_input_token_cap",
            "reservation_initial_input_tokens",
            "reservation_growth_tokens_per_call",
            "reservation_input_token_cap",
        ):
            _require_non_negative_int(getattr(self, field_name), field_name=field_name)
        if self.expected_initial_input_tokens > self.expected_input_token_cap:
            raise ValueError("expected_initial_input_tokens_exceeds_cap")
        if self.reservation_initial_input_tokens > self.reservation_input_token_cap:
            raise ValueError("reservation_initial_input_tokens_exceeds_cap")


@dataclass(frozen=True, slots=True)
class OutputAndReasoningBounds:
    """Injected low/expected output and reasoning-token assumptions."""

    low_output_tokens_per_call: int
    expected_output_tokens_per_call: int
    low_reasoning_tokens_per_call: int
    expected_reasoning_tokens_per_call: int
    reservation_reasoning_tokens_per_call: int

    def __post_init__(self) -> None:
        for field_name in (
            "low_output_tokens_per_call",
            "expected_output_tokens_per_call",
            "low_reasoning_tokens_per_call",
            "expected_reasoning_tokens_per_call",
            "reservation_reasoning_tokens_per_call",
        ):
            _require_non_negative_int(getattr(self, field_name), field_name=field_name)


@dataclass(frozen=True, slots=True)
class CostEstimationPolicy:
    """Operator-owned, fully injected limits and conservative price overrides."""

    context_growth: ContextGrowthBounds
    output_and_reasoning: OutputAndReasoningBounds
    safety_margin_bps: int
    max_platform_price_micros_per_million: int | None = None
    max_platform_reservation_micros: int | None = None
    operator_price_overrides: tuple[tuple[str, CatalogTokenPrices], ...] = ()

    def __post_init__(self) -> None:
        _require_non_negative_int(self.safety_margin_bps, field_name="safety_margin_bps")
        if self.max_platform_price_micros_per_million is not None:
            _require_non_negative_int(
                self.max_platform_price_micros_per_million,
                field_name="max_platform_price_micros_per_million",
            )
        if self.max_platform_reservation_micros is not None:
            _require_non_negative_int(
                self.max_platform_reservation_micros,
                field_name="max_platform_reservation_micros",
            )
        override_slugs = [slug for slug, _prices in self.operator_price_overrides]
        if len(override_slugs) != len(set(override_slugs)):
            raise ValueError("duplicate_operator_price_override")
        if any(not slug or slug.strip() != slug for slug in override_slugs):
            raise ValueError("operator_price_override_slug_required")


@dataclass(frozen=True, slots=True)
class CallCountAssumptions:
    """Frozen upper bounds derived from the rules/cadence snapshot."""

    cadence_id: str
    max_days: int
    discussion_rounds_per_day: int
    ai_seat_count: int
    discussion_calls_per_ai_seat: int
    day_vote_calls_per_ai_seat: int
    night_action_calls_per_ai_seat: int
    alive_ai_call_upper_bound: int
    mafia_channel_calls_per_mafia_ai_seat: int
    mafia_role_seat_count: int
    mafia_ai_seat_upper_bound: int
    total_calls: int


@dataclass(frozen=True, slots=True)
class CostEstimatorAssumptions:
    """The explicit bounds behind a cost estimate for audit and UI explanation."""

    call_counts: CallCountAssumptions
    context_growth: ContextGrowthBounds
    output_and_reasoning: OutputAndReasoningBounds
    safety_margin_bps: int
    low_input_tokens_per_call: int
    expected_input_tokens_per_call: int
    reservation_input_tokens_per_call: int


@dataclass(frozen=True, slots=True)
class SeatCostEstimate:
    """Per-seat share of the game-wide conservative call allocation."""

    seat_index: int
    canonical_slug: str
    funding_source: Literal["platform", "byok"]
    call_count: int
    low_micros: int | None
    expected_micros: int | None
    reservation_micros: int | None
    pricing_source: Literal["catalog", "operator_override"] | None
    platform_eligible: bool
    byok_eligible: bool
    reasons: tuple[CostEstimateReason, ...]


@dataclass(frozen=True, slots=True)
class WholeGameCostEstimate:
    """Integer-micro cost result used by UI, reservation, and audit surfaces."""

    catalog_content_hash: str
    low_total_micros: int | None
    expected_total_micros: int | None
    reservation_total_micros: int | None
    per_seat: tuple[SeatCostEstimate, ...]
    assumptions: CostEstimatorAssumptions
    platform_eligible: bool
    declared_funding_eligible: bool
    reasons: tuple[CostEstimateReason, ...]

    def totals(self) -> tuple[int | None, int | None, int | None]:
        """Return low, expected, and reservation totals in USD micros."""
        return (
            self.low_total_micros,
            self.expected_total_micros,
            self.reservation_total_micros,
        )


@dataclass(frozen=True, slots=True)
class _ResolvedPrices:
    """Parsed price facts retained only while this pure calculation executes."""

    prompt_usd_per_token: Decimal
    completion_usd_per_token: Decimal
    fixed_usd_per_game: Decimal
    request_usd_per_call: Decimal
    reasoning_usd_per_token: Decimal
    source: Literal["catalog", "operator_override"]


def _resolved_prices(
    selection: AiSeatSelection,
    *,
    overrides: dict[str, CatalogTokenPrices],
) -> _ResolvedPrices | None:
    prices = selection.catalog_token_prices
    source: Literal["catalog", "operator_override"] = "catalog"
    if not prices.is_priceable:
        override = overrides.get(selection.canonical_slug)
        if override is None or not override.is_priceable:
            return None
        prices = override
        source = "operator_override"
    prompt = _decimal_or_none(prices.prompt_usd_per_token, field_name="prompt_usd_per_token")
    completion = _decimal_or_none(
        prices.completion_usd_per_token,
        field_name="completion_usd_per_token",
    )
    fixed = _decimal_or_none(prices.fixed_usd_per_game, field_name="fixed_usd_per_game")
    request = _decimal_or_none(prices.request_usd_per_call, field_name="request_usd_per_call")
    reasoning = _decimal_or_none(
        prices.reasoning_usd_per_token,
        field_name="reasoning_usd_per_token",
    )
    if (
        prompt is None
        or completion is None
        or fixed is None
        or request is None
        or reasoning is None
    ):
        return None
    return _ResolvedPrices(
        prompt_usd_per_token=prompt,
        completion_usd_per_token=completion,
        fixed_usd_per_game=fixed,
        request_usd_per_call=request,
        reasoning_usd_per_token=reasoning,
        source=source,
    )


def _call_counts(snapshot: GameRulesSnapshot, *, ai_seat_count: int) -> CallCountAssumptions:
    profile = cadence_profile(snapshot.cadence_id)
    configured_rounds = len(profile.discussion_rounds[: snapshot.discussion_rounds_per_day])
    if configured_rounds != snapshot.discussion_rounds_per_day:
        raise ValueError("cadence_does_not_cover_discussion_rounds")
    discussion_calls = snapshot.max_days * configured_rounds
    day_vote_calls = snapshot.max_days
    night_action_calls = snapshot.max_days
    alive_upper_bound = ai_seat_count * (discussion_calls + day_vote_calls + night_action_calls)
    mafia_role_seat_count = sum(
        role.count
        for role in snapshot.role_counts
        if snapshot.faction_for(role.role) is Faction.MAFIA
    )
    mafia_channel_calls = snapshot.max_days + 1
    mafia_ai_upper_bound = min(ai_seat_count, mafia_role_seat_count)
    total_calls = alive_upper_bound + mafia_ai_upper_bound * mafia_channel_calls
    return CallCountAssumptions(
        cadence_id=snapshot.cadence_id,
        max_days=snapshot.max_days,
        discussion_rounds_per_day=configured_rounds,
        ai_seat_count=ai_seat_count,
        discussion_calls_per_ai_seat=discussion_calls,
        day_vote_calls_per_ai_seat=day_vote_calls,
        night_action_calls_per_ai_seat=night_action_calls,
        alive_ai_call_upper_bound=alive_upper_bound,
        mafia_channel_calls_per_mafia_ai_seat=mafia_channel_calls,
        mafia_role_seat_count=mafia_role_seat_count,
        mafia_ai_seat_upper_bound=mafia_ai_upper_bound,
        total_calls=total_calls,
    )


def _average_ramped_tokens(
    *,
    call_count: int,
    initial: int,
    growth: int,
    cap: int,
) -> int:
    if call_count == 0:
        return 0
    total = sum(min(cap, initial + growth * call_index) for call_index in range(call_count))
    return (total + call_count - 1) // call_count


def _reservation_context_tokens(
    *,
    call_count: int,
    initial: int,
    growth: int,
    cap: int,
) -> int:
    if call_count == 0:
        return 0
    return min(cap, initial + growth * (call_count - 1))


def _scenario_cost_micros(
    *,
    prices: _ResolvedPrices,
    call_count: int,
    input_tokens_per_call: int,
    output_tokens_per_call: int,
    reasoning_tokens_per_call: int,
) -> int:
    total = _micros_for_usd(prices.fixed_usd_per_game)
    total += _micros_for_usd(prices.request_usd_per_call * Decimal(call_count))
    total += _micros_for_tokens(
        prices.prompt_usd_per_token,
        call_count * input_tokens_per_call,
    )
    total += _micros_for_tokens(
        prices.completion_usd_per_token,
        call_count * output_tokens_per_call,
    )
    total += _micros_for_tokens(
        prices.reasoning_usd_per_token,
        call_count * reasoning_tokens_per_call,
    )
    return total


def _with_safety_margin(micros: int, *, safety_margin_bps: int) -> int:
    return (micros * (_BASIS_POINTS + safety_margin_bps) + _BASIS_POINTS - 1) // _BASIS_POINTS


def _platform_price_eligible(
    prices: _ResolvedPrices,
    *,
    policy: CostEstimationPolicy,
) -> bool:
    maximum = policy.max_platform_price_micros_per_million
    if maximum is None:
        return True
    return (
        _micros_per_million_tokens(prices.prompt_usd_per_token) <= maximum
        and _micros_per_million_tokens(prices.completion_usd_per_token) <= maximum
    )


def _ordered_reasons(reasons: set[CostEstimateReason]) -> tuple[CostEstimateReason, ...]:
    return tuple(reason for reason in CostEstimateReason if reason in reasons)


def estimate_whole_game_cost(
    *,
    rules_snapshot: GameRulesSnapshot,
    ai_seat_selections: tuple[AiSeatSelection, ...] | list[AiSeatSelection],
    catalog_content_hash: str,
    policy: CostEstimationPolicy,
) -> WholeGameCostEstimate:
    """Estimate a bounded whole game from frozen rules, cadence, and model facts.

    Every selected AI seat receives the full alive-seat phase bound.  Mafia-only
    intro/discussion calls are then assigned to the most expensive priceable
    seats, which bounds any hidden seeded role assignment without needing to
    know it.  An unknown prompt/completion price leaves monetary totals absent
    and permits BYOK only; platform admission must therefore fail closed.
    """
    if not catalog_content_hash or catalog_content_hash.strip() != catalog_content_hash:
        raise ValueError("catalog_content_hash_required")
    selections = tuple(sorted(ai_seat_selections, key=lambda selection: selection.seat_index))
    if len({selection.seat_index for selection in selections}) != len(selections):
        raise ValueError("duplicate_ai_seat_index")
    if len(selections) > rules_snapshot.player_count:
        raise ValueError("ai_seat_count_exceeds_player_count")

    call_counts = _call_counts(rules_snapshot, ai_seat_count=len(selections))
    context = policy.context_growth
    usage = policy.output_and_reasoning
    expected_input = _average_ramped_tokens(
        call_count=call_counts.total_calls,
        initial=context.expected_initial_input_tokens,
        growth=context.expected_growth_tokens_per_call,
        cap=context.expected_input_token_cap,
    )
    reservation_input = _reservation_context_tokens(
        call_count=call_counts.total_calls,
        initial=context.reservation_initial_input_tokens,
        growth=context.reservation_growth_tokens_per_call,
        cap=context.reservation_input_token_cap,
    )
    assumptions = CostEstimatorAssumptions(
        call_counts=call_counts,
        context_growth=context,
        output_and_reasoning=usage,
        safety_margin_bps=policy.safety_margin_bps,
        low_input_tokens_per_call=context.low_input_tokens_per_call,
        expected_input_tokens_per_call=expected_input,
        reservation_input_tokens_per_call=reservation_input,
    )
    overrides = dict(policy.operator_price_overrides)
    resolved = {
        selection.seat_index: _resolved_prices(selection, overrides=overrides)
        for selection in selections
    }
    base_calls_per_seat = (
        call_counts.discussion_calls_per_ai_seat
        + call_counts.day_vote_calls_per_ai_seat
        + call_counts.night_action_calls_per_ai_seat
    )
    special_call_slots = call_counts.mafia_ai_seat_upper_bound
    reservation_unit_cost: dict[int, int] = {}
    for selection in selections:
        prices = resolved[selection.seat_index]
        if prices is None:
            continue
        reservation_unit_cost[selection.seat_index] = _scenario_cost_micros(
            prices=prices,
            call_count=1,
            input_tokens_per_call=reservation_input,
            output_tokens_per_call=selection.max_output_tokens,
            reasoning_tokens_per_call=usage.reservation_reasoning_tokens_per_call,
        ) - _micros_for_usd(prices.fixed_usd_per_game)
    extra_call_seats = {
        selection.seat_index
        for selection in sorted(
            selections,
            key=lambda selection: (
                -reservation_unit_cost.get(selection.seat_index, -1),
                selection.seat_index,
            ),
        )[:special_call_slots]
    }

    per_seat: list[SeatCostEstimate] = []
    for selection in selections:
        prices = resolved[selection.seat_index]
        call_count = base_calls_per_seat + (
            call_counts.mafia_channel_calls_per_mafia_ai_seat
            if selection.seat_index in extra_call_seats
            else 0
        )
        reasons: set[CostEstimateReason] = set()
        platform_eligible = True
        byok_eligible = True
        low: int | None = None
        expected: int | None = None
        reservation: int | None = None
        pricing_source: Literal["catalog", "operator_override"] | None = None
        if prices is None:
            platform_eligible = False
            reasons.update({CostEstimateReason.UNPRICEABLE_MODEL, CostEstimateReason.BYOK_REQUIRED})
        else:
            pricing_source = prices.source
            low = _scenario_cost_micros(
                prices=prices,
                call_count=call_count,
                input_tokens_per_call=context.low_input_tokens_per_call,
                output_tokens_per_call=min(
                    usage.low_output_tokens_per_call,
                    selection.max_output_tokens,
                ),
                reasoning_tokens_per_call=usage.low_reasoning_tokens_per_call,
            )
            expected = _scenario_cost_micros(
                prices=prices,
                call_count=call_count,
                input_tokens_per_call=expected_input,
                output_tokens_per_call=min(
                    usage.expected_output_tokens_per_call,
                    selection.max_output_tokens,
                ),
                reasoning_tokens_per_call=usage.expected_reasoning_tokens_per_call,
            )
            reservation = _with_safety_margin(
                _scenario_cost_micros(
                    prices=prices,
                    call_count=call_count,
                    input_tokens_per_call=reservation_input,
                    output_tokens_per_call=selection.max_output_tokens,
                    reasoning_tokens_per_call=usage.reservation_reasoning_tokens_per_call,
                ),
                safety_margin_bps=policy.safety_margin_bps,
            )
            if not _platform_price_eligible(prices, policy=policy):
                platform_eligible = False
                reasons.add(CostEstimateReason.OVER_PLATFORM_PRICE_POLICY)
        per_seat.append(
            SeatCostEstimate(
                seat_index=selection.seat_index,
                canonical_slug=selection.canonical_slug,
                funding_source=selection.funding_source,
                call_count=call_count,
                low_micros=low,
                expected_micros=expected,
                reservation_micros=reservation,
                pricing_source=pricing_source,
                platform_eligible=platform_eligible,
                byok_eligible=byok_eligible,
                reasons=_ordered_reasons(reasons),
            )
        )

    unpriceable = any(seat.reservation_micros is None for seat in per_seat)
    low_total = None if unpriceable else sum(seat.low_micros or 0 for seat in per_seat)
    expected_total = None if unpriceable else sum(seat.expected_micros or 0 for seat in per_seat)
    reservation_total = (
        None if unpriceable else sum(seat.reservation_micros or 0 for seat in per_seat)
    )
    result_reasons = {reason for seat in per_seat for reason in seat.reasons}
    if (
        reservation_total is not None
        and policy.max_platform_reservation_micros is not None
        and reservation_total > policy.max_platform_reservation_micros
    ):
        result_reasons.add(CostEstimateReason.OVER_PLATFORM_RESERVATION_POLICY)
    platform_eligible = not result_reasons and all(seat.platform_eligible for seat in per_seat)
    declared_funding_eligible = all(
        seat.platform_eligible if seat.funding_source == "platform" else seat.byok_eligible
        for seat in per_seat
    )
    if CostEstimateReason.OVER_PLATFORM_RESERVATION_POLICY in result_reasons:
        declared_funding_eligible = False
    return WholeGameCostEstimate(
        catalog_content_hash=catalog_content_hash,
        low_total_micros=low_total,
        expected_total_micros=expected_total,
        reservation_total_micros=reservation_total,
        per_seat=tuple(per_seat),
        assumptions=assumptions,
        platform_eligible=platform_eligible,
        declared_funding_eligible=declared_funding_eligible,
        reasons=_ordered_reasons(result_reasons),
    )


__all__ = [
    "AiSeatSelection",
    "CallCountAssumptions",
    "CatalogTokenPrices",
    "ContextGrowthBounds",
    "CostEstimateReason",
    "CostEstimationPolicy",
    "CostEstimatorAssumptions",
    "OutputAndReasoningBounds",
    "SeatCostEstimate",
    "WholeGameCostEstimate",
    "estimate_whole_game_cost",
]
