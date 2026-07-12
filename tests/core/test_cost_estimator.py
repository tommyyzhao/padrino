"""Tests for the pure, conservative whole-game OpenRouter cost estimator."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from padrino.core.cost_estimator import (
    AiSeatSelection,
    CatalogTokenPrices,
    ContextGrowthBounds,
    CostEstimateReason,
    CostEstimationPolicy,
    OutputAndReasoningBounds,
    estimate_whole_game_cost,
)
from padrino.core.enums import Role
from padrino.core.rulesets import get_ruleset
from padrino.core.rulesets.custom_deck import validate_custom_role_deck
from padrino.core.rulesets.snapshot import snapshot_from_ruleset


def _prices(
    *, prompt: str | None = "0.000001", completion: str | None = "0.000002"
) -> CatalogTokenPrices:
    return CatalogTokenPrices(
        prompt_usd_per_token=prompt,
        completion_usd_per_token=completion,
        fixed_usd_per_game="0.000010",
        request_usd_per_call="0.000001",
        reasoning_usd_per_token="0.000003",
    )


def _policy(
    *,
    discussion_growth: int = 0,
    max_platform_price_micros_per_million: int | None = None,
) -> CostEstimationPolicy:
    return CostEstimationPolicy(
        context_growth=ContextGrowthBounds(
            low_input_tokens_per_call=10,
            expected_initial_input_tokens=20,
            expected_growth_tokens_per_call=discussion_growth,
            expected_input_token_cap=100,
            reservation_initial_input_tokens=30,
            reservation_growth_tokens_per_call=discussion_growth,
            reservation_input_token_cap=100,
        ),
        output_and_reasoning=OutputAndReasoningBounds(
            low_output_tokens_per_call=2,
            expected_output_tokens_per_call=3,
            low_reasoning_tokens_per_call=1,
            expected_reasoning_tokens_per_call=2,
            reservation_reasoning_tokens_per_call=3,
        ),
        safety_margin_bps=1_000,
        max_platform_price_micros_per_million=max_platform_price_micros_per_million,
    )


def _selections(
    count: int,
    *,
    prices: CatalogTokenPrices | None = None,
    max_output_tokens: int = 4,
) -> tuple[AiSeatSelection, ...]:
    return tuple(
        AiSeatSelection(
            seat_index=index,
            canonical_slug=f"example/model-{index}",
            max_output_tokens=max_output_tokens,
            catalog_token_prices=prices or _prices(),
        )
        for index in range(count)
    )


def test_estimate_is_integer_micro_decimal_only_and_pins_mini7_totals() -> None:
    snapshot = snapshot_from_ruleset(get_ruleset("mini7_v1"), cadence_id="casual_standard_v1")

    estimate = estimate_whole_game_cost(
        rules_snapshot=snapshot,
        ai_seat_selections=_selections(7),
        catalog_content_hash="a" * 64,
        policy=_policy(),
    )

    assert estimate.catalog_content_hash == "a" * 64
    assert estimate.low_total_micros == 3_436
    assert estimate.expected_total_micros == 6_241
    assert estimate.reservation_total_micros == 9_951
    assert estimate.platform_eligible is True
    assert estimate.declared_funding_eligible is True
    assert estimate.reasons == ()
    assert estimate.assumptions.call_counts.discussion_calls_per_ai_seat == 15
    assert estimate.assumptions.call_counts.day_vote_calls_per_ai_seat == 5
    assert estimate.assumptions.call_counts.night_action_calls_per_ai_seat == 5
    assert estimate.assumptions.call_counts.mafia_channel_calls_per_mafia_ai_seat == 6
    assert estimate.assumptions.call_counts.mafia_ai_seat_upper_bound == 2
    assert estimate.assumptions.call_counts.total_calls == 187
    assert sum(seat.reservation_micros or 0 for seat in estimate.per_seat) == 9_951
    assert all(isinstance(value, int) for value in estimate.totals())


@pytest.mark.parametrize(
    ("ruleset_id", "ai_seats", "expected_calls"),
    [
        ("mini7_v1", 7, 187),
        ("bench10_v1", 10, 268),
    ],
)
def test_call_bounds_are_pinned_to_frozen_builtin_rules_and_cadence(
    ruleset_id: str,
    ai_seats: int,
    expected_calls: int,
) -> None:
    snapshot = snapshot_from_ruleset(get_ruleset(ruleset_id), cadence_id="casual_fast_v1")

    estimate = estimate_whole_game_cost(
        rules_snapshot=snapshot,
        ai_seat_selections=_selections(ai_seats),
        catalog_content_hash="b" * 64,
        policy=_policy(),
    )

    assert estimate.assumptions.call_counts.cadence_id == "casual_fast_v1"
    assert estimate.assumptions.call_counts.total_calls == expected_calls
    assert estimate.assumptions.call_counts.alive_ai_call_upper_bound == ai_seats * 25


def test_call_bounds_are_pinned_to_a_custom_role_deck() -> None:
    deck = validate_custom_role_deck(
        {
            Role.MAFIA_GOON: 3,
            Role.DETECTIVE: 1,
            Role.DOCTOR: 1,
            Role.VILLAGER: 5,
        },
        player_count=10,
    )
    assert deck.snapshot is not None
    snapshot = deck.snapshot.model_copy(update={"cadence_id": "casual_relaxed_v1"})

    estimate = estimate_whole_game_cost(
        rules_snapshot=snapshot,
        ai_seat_selections=_selections(10),
        catalog_content_hash="c" * 64,
        policy=_policy(),
    )

    assert estimate.assumptions.call_counts.total_calls == 268
    assert estimate.assumptions.call_counts.mafia_ai_seat_upper_bound == 3


def test_unpriceable_model_is_byok_only_without_an_operator_override() -> None:
    snapshot = snapshot_from_ruleset(get_ruleset("mini7_v1"))
    selections = (
        AiSeatSelection(
            seat_index=0,
            canonical_slug="example/unpriced",
            max_output_tokens=4,
            catalog_token_prices=_prices(prompt=None),
            funding_source="platform",
        ),
    )

    estimate = estimate_whole_game_cost(
        rules_snapshot=snapshot,
        ai_seat_selections=selections,
        catalog_content_hash="d" * 64,
        policy=_policy(),
    )

    seat = estimate.per_seat[0]
    assert estimate.totals() == (None, None, None)
    assert estimate.platform_eligible is False
    assert estimate.declared_funding_eligible is False
    assert seat.platform_eligible is False
    assert seat.byok_eligible is True
    assert seat.reasons == (
        CostEstimateReason.UNPRICEABLE_MODEL,
        CostEstimateReason.BYOK_REQUIRED,
    )


def test_unpriceable_byok_selection_remains_admissible_without_a_platform_reservation() -> None:
    snapshot = snapshot_from_ruleset(get_ruleset("mini7_v1"))

    estimate = estimate_whole_game_cost(
        rules_snapshot=snapshot,
        ai_seat_selections=(
            AiSeatSelection(
                seat_index=0,
                canonical_slug="example/unpriced-byok",
                max_output_tokens=4,
                catalog_token_prices=_prices(prompt=None),
                funding_source="byok",
            ),
        ),
        catalog_content_hash="d" * 64,
        policy=_policy(),
    )

    assert estimate.platform_eligible is False
    assert estimate.declared_funding_eligible is True
    assert estimate.reservation_total_micros is None


def test_mafia_only_calls_are_allocated_by_marginal_not_fixed_cost() -> None:
    snapshot = snapshot_from_ruleset(get_ruleset("mini7_v1"))
    cheap_per_call_expensive_fixed = CatalogTokenPrices(
        prompt_usd_per_token="0.000001",
        completion_usd_per_token="0.000001",
        fixed_usd_per_game="1",
        request_usd_per_call="0",
        reasoning_usd_per_token="0",
    )
    expensive_per_call_free_fixed = CatalogTokenPrices(
        prompt_usd_per_token="0.000010",
        completion_usd_per_token="0.000010",
        fixed_usd_per_game="0",
        request_usd_per_call="0",
        reasoning_usd_per_token="0",
    )
    medium_per_call_free_fixed = CatalogTokenPrices(
        prompt_usd_per_token="0.000005",
        completion_usd_per_token="0.000005",
        fixed_usd_per_game="0",
        request_usd_per_call="0",
        reasoning_usd_per_token="0",
    )

    estimate = estimate_whole_game_cost(
        rules_snapshot=snapshot,
        ai_seat_selections=(
            AiSeatSelection(0, "example/fixed", 4, cheap_per_call_expensive_fixed),
            AiSeatSelection(1, "example/marginal", 4, expensive_per_call_free_fixed),
            AiSeatSelection(2, "example/medium", 4, medium_per_call_free_fixed),
        ),
        catalog_content_hash="e" * 64,
        policy=_policy(),
    )

    assert [seat.call_count for seat in estimate.per_seat] == [25, 31, 31]


def test_operator_override_prices_an_otherwise_unpriceable_model_conservatively() -> None:
    snapshot = snapshot_from_ruleset(get_ruleset("mini7_v1"))
    selections = (
        AiSeatSelection(
            seat_index=0,
            canonical_slug="example/unpriced",
            max_output_tokens=4,
            catalog_token_prices=_prices(prompt=None),
        ),
    )

    estimate = estimate_whole_game_cost(
        rules_snapshot=snapshot,
        ai_seat_selections=selections,
        catalog_content_hash="e" * 64,
        policy=CostEstimationPolicy(
            context_growth=_policy().context_growth,
            output_and_reasoning=_policy().output_and_reasoning,
            safety_margin_bps=1_000,
            operator_price_overrides=(
                (
                    "example/unpriced",
                    _prices(prompt="0.000003", completion="0.000004"),
                ),
            ),
        ),
    )

    assert estimate.totals() == (1_312, 2_459, 3_967)
    assert estimate.per_seat[0].pricing_source == "operator_override"
    assert estimate.platform_eligible is True


def test_over_policy_model_has_stable_reason_and_remains_byok_eligible() -> None:
    snapshot = snapshot_from_ruleset(get_ruleset("mini7_v1"))

    estimate = estimate_whole_game_cost(
        rules_snapshot=snapshot,
        ai_seat_selections=_selections(1, prices=_prices(prompt="0.000010")),
        catalog_content_hash="f" * 64,
        policy=_policy(max_platform_price_micros_per_million=5_000_000),
    )

    seat = estimate.per_seat[0]
    assert estimate.platform_eligible is False
    assert estimate.declared_funding_eligible is False
    assert seat.platform_eligible is False
    assert seat.byok_eligible is True
    assert seat.reasons == (CostEstimateReason.OVER_PLATFORM_PRICE_POLICY,)


@given(
    prompt_price_micros=st.integers(min_value=1, max_value=100_000),
    max_output_tokens=st.integers(min_value=1, max_value=256),
    discussion_rounds=st.integers(min_value=0, max_value=50),
    ai_seat_count=st.integers(min_value=1, max_value=7),
)
def test_reservation_total_is_monotonic_across_price_tokens_cycles_and_ai_seats(
    prompt_price_micros: int,
    max_output_tokens: int,
    discussion_rounds: int,
    ai_seat_count: int,
) -> None:
    snapshot = snapshot_from_ruleset(get_ruleset("mini7_v1"))
    base_policy = _policy()
    higher_cycle_policy = CostEstimationPolicy(
        context_growth=ContextGrowthBounds(
            low_input_tokens_per_call=10,
            expected_initial_input_tokens=20,
            expected_growth_tokens_per_call=discussion_rounds,
            expected_input_token_cap=10_000,
            reservation_initial_input_tokens=30,
            reservation_growth_tokens_per_call=discussion_rounds,
            reservation_input_token_cap=10_000,
        ),
        output_and_reasoning=base_policy.output_and_reasoning,
        safety_margin_bps=base_policy.safety_margin_bps,
    )
    price = f"0.{prompt_price_micros:012d}"
    higher_price = f"0.{prompt_price_micros * 2:012d}"
    fewer_discussion_cycles = snapshot.model_copy(update={"discussion_rounds_per_day": 2})

    base = estimate_whole_game_cost(
        rules_snapshot=fewer_discussion_cycles,
        ai_seat_selections=_selections(
            ai_seat_count,
            prices=_prices(prompt=price),
            max_output_tokens=max_output_tokens,
        ),
        catalog_content_hash="1" * 64,
        policy=higher_cycle_policy,
    )
    more_price = estimate_whole_game_cost(
        rules_snapshot=fewer_discussion_cycles,
        ai_seat_selections=_selections(
            ai_seat_count,
            prices=_prices(prompt=higher_price),
            max_output_tokens=max_output_tokens,
        ),
        catalog_content_hash="2" * 64,
        policy=higher_cycle_policy,
    )
    more_tokens = estimate_whole_game_cost(
        rules_snapshot=fewer_discussion_cycles,
        ai_seat_selections=_selections(
            ai_seat_count,
            prices=_prices(prompt=price),
            max_output_tokens=max_output_tokens + 1,
        ),
        catalog_content_hash="3" * 64,
        policy=higher_cycle_policy,
    )
    more_cycles = estimate_whole_game_cost(
        rules_snapshot=snapshot,
        ai_seat_selections=_selections(
            ai_seat_count,
            prices=_prices(prompt=price),
            max_output_tokens=max_output_tokens,
        ),
        catalog_content_hash="4" * 64,
        policy=higher_cycle_policy,
    )
    more_seats = estimate_whole_game_cost(
        rules_snapshot=fewer_discussion_cycles,
        ai_seat_selections=_selections(
            min(ai_seat_count + 1, 7),
            prices=_prices(prompt=price),
            max_output_tokens=max_output_tokens,
        ),
        catalog_content_hash="5" * 64,
        policy=higher_cycle_policy,
    )

    assert base.reservation_total_micros is not None
    assert more_price.reservation_total_micros is not None
    assert more_tokens.reservation_total_micros is not None
    assert more_cycles.reservation_total_micros is not None
    assert more_seats.reservation_total_micros is not None
    assert more_price.reservation_total_micros >= base.reservation_total_micros
    assert more_tokens.reservation_total_micros >= base.reservation_total_micros
    assert more_cycles.reservation_total_micros >= base.reservation_total_micros
    assert more_seats.reservation_total_micros >= base.reservation_total_micros


def test_estimator_module_contains_no_float_literals() -> None:
    module_path = Path(__file__).resolve().parents[2] / "src/padrino/core/cost_estimator.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))

    assert not any(
        isinstance(node, ast.Constant) and isinstance(node.value, float) for node in ast.walk(tree)
    )
